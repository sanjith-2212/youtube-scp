import os
import subprocess
import random
from pathlib import Path

from .utils import ytdlp_bin


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.0; rv:121.0) Gecko/20100101 Firefox/121.0",
]

DENO_PATH = "/home/ubuntu/.deno/bin"

# Substrings that mean "the IP is throttled", as opposed to "this video is
# genuinely unavailable". Only these trigger a global backoff. Matched against
# lowercased stderr, and kept apostrophe-free because yt-dlp emits a curly one.
RATE_LIMIT_MARKERS = (
    "not a bot",
    "http error 429",
    "too many requests",
)


class RateLimitedError(Exception):
    """YouTube served a bot check or 429 instead of the video."""


class AudioDownloader:
    def __init__(self, config, logger, proxy=None, sample_rate=None):
        self.config = config
        self.logger = logger
        self.proxy = proxy
        # The AV pipeline only needs a 16 kHz copy to run VAD over -- the clips
        # it keeps are cut from the muxed video later -- and a multi-hour film
        # at 48 kHz is a ~800 MB WAV, so it overrides this.
        self.sample_rate = sample_rate or config["audio"]["sample_rate"]
        self.env = os.environ.copy()
        self.env["PATH"] = f"{DENO_PATH}:{self.env.get('PATH', '')}"

    def download_audio(self, video_info, output_dir):
        video_id = video_info["video_id"]
        url = video_info["url"]
        output_path = output_dir / f"{video_id}.wav"

        if output_path.exists():
            self.logger.debug(f"Already downloaded: {video_id}")
            return output_path

        dl_template = output_dir / f"{video_id}.%(ext)s"

        strategies = [
            {
                "name": "web_embedded",
                "args": ["--extractor-args", "youtube:player_client=web_embedded", "-f", "bestaudio"],
            },
            {
                "name": "m3u8_fallback",
                "args": ["-f", "91/92/93/94/95/96/best[protocol~=m3u8]/bestaudio[protocol~=m3u8]"],
            },
        ]

        for strategy in strategies:
            downloaded = self._try_download(video_id, url, dl_template, output_dir, strategy)
            if downloaded:
                break
        else:
            self.logger.warning(f"All download strategies failed for {video_id}")
            return None

        return self._convert_to_wav(downloaded, output_path, video_id)

    def _try_download(self, video_id, url, dl_template, output_dir, strategy):
        cmd = [
            ytdlp_bin(),
            "--no-playlist",
            "--js-runtimes", f"deno:{DENO_PATH}/deno",
            *strategy["args"],
            "-o", str(dl_template),
            "--no-overwrites",
            "--socket-timeout", "30",
            "--retries", str(self.config["processing"]["max_retries"]),
            "--user-agent", random.choice(USER_AGENTS),
            "--no-check-certificates",
            "--geo-bypass",
            "--no-warnings",
            "--quiet",
        ]

        if self.proxy:
            cmd.extend(["--proxy", self.proxy])

        cmd.append(url)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
                env=self.env,
            )

            if result.returncode != 0:
                stderr = result.stderr or ""
                lowered = stderr.lower()
                if any(marker in lowered for marker in RATE_LIMIT_MARKERS):
                    # Every strategy will hit the same wall, so surface it now
                    # rather than burning the fallback on a throttled IP.
                    raise RateLimitedError(f"{video_id}: {self._first_error_line(stderr)}")
                self.logger.debug(f"Strategy '{strategy['name']}' failed for {video_id}")
                return None

            # Find the downloaded file
            for ext in [".webm", ".m4a", ".mp4", ".mkv", ".opus"]:
                candidate = output_dir / f"{video_id}{ext}"
                if candidate.exists():
                    return candidate

            self.logger.debug(f"No output file found for {video_id} with strategy '{strategy['name']}'")
            return None

        except subprocess.TimeoutExpired:
            self.logger.debug(f"Download timed out for {video_id} with strategy '{strategy['name']}'")
            return None
        except RateLimitedError:
            raise
        except Exception as e:
            self.logger.error(f"Download exception for {video_id}: {e}")
            return None

    @staticmethod
    def _first_error_line(stderr):
        for line in stderr.splitlines():
            if line.startswith("ERROR:"):
                return line.strip()[:200]
        return stderr.strip().splitlines()[-1][:200] if stderr.strip() else "unknown error"

    def _convert_to_wav(self, source_path, output_path, video_id):
        ffmpeg_cmd = [
            "ffmpeg",
            "-i", str(source_path),
            "-vn",
            "-ar", str(self.sample_rate),
            "-ac", "1",
            "-acodec", "pcm_s16le",
            "-y",
            str(output_path),
        ]

        try:
            ffmpeg_result = subprocess.run(
                ffmpeg_cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        finally:
            self._cleanup(source_path)

        if ffmpeg_result.returncode != 0:
            self.logger.warning(f"FFmpeg conversion failed for {video_id}")
            self._cleanup(output_path)
            return None

        if output_path.exists() and output_path.stat().st_size > 1000:
            self.logger.debug(f"Downloaded and converted: {video_id}")
            return output_path

        self.logger.warning(f"Output too small or missing for {video_id}")
        self._cleanup(output_path)
        return None

    def _cleanup(self, path):
        try:
            if path and Path(path).exists():
                Path(path).unlink()
        except Exception:
            pass
