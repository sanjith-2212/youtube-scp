import os
import re
import random
import subprocess
from pathlib import Path

from .utils import ytdlp_bin
from .downloader import USER_AGENTS, DENO_PATH, RATE_LIMIT_MARKERS, RateLimitedError


CLIP_PREFIX = "clip_"
_CLIP_RE = re.compile(rf"^{CLIP_PREFIX}(?P<start>[0-9]+(?:\.[0-9]+)?)$")


class VideoClipFetcher:
    """Pulls short video clips out of a long video without downloading it whole.

    The AI-film channels publish 1-2.5 hour videos; a full 720p copy of one is
    ~300-900 MB, and we only ever keep ~15 clips of a few seconds each. yt-dlp's
    --download-sections fetches just the byte ranges covering those spans, which
    measured at ~5s per clip against ~334 MB and minutes for the whole file.

    Every requested window is passed to a single yt-dlp invocation: extraction
    (player response, nsig) costs ~8s and is the dominant per-call overhead, so
    amortising it across all clips of a video roughly halves wall time.
    """

    def __init__(self, config, logger, proxy=None, force_keyframes=None):
        self.config = config
        self.logger = logger
        self.proxy = proxy

        v = config["video"]
        self.format_selector = v["format_selector"]
        self.container = v.get("container", "mp4")
        # Caller can override: frame-exact cuts cost a full re-encode, which is
        # worth it for a 6s window and actively harmful for a 2 minute one.
        self.force_keyframes = (
            v.get("force_keyframes", True) if force_keyframes is None else force_keyframes
        )
        self.concurrent_fragments = int(v.get("concurrent_fragments", 4))
        # The timeout has to scale with how many clips were asked for: one
        # invocation downloads them sequentially, so a flat ceiling silently
        # truncates long videos. Measured 2026-11-02: 10x120s clips take
        # ~735s, and a flat 900s cut three of thirteen videos short.
        self.timeout_base = int(v.get("clip_timeout_base_sec", 300))
        self.timeout_per_clip = int(v.get("clip_timeout_per_clip_sec", 240))
        self.clip_audio_sample_rate = int(v.get("clip_audio_sample_rate", 48000))

        self.env = os.environ.copy()
        self.env["PATH"] = f"{DENO_PATH}:{self.env.get('PATH', '')}"

    # ------------------------------------------------------------------ clips

    def fetch_clips(self, video_info, windows, output_dir):
        """Download one clip per window. Returns [{index, path, ...}, ...].

        Windows that yt-dlp could not produce are simply absent from the
        result: a clip is only useful here if it has a matching audio half, so
        a partial failure drops that pair rather than desynchronising them.
        """
        if not windows:
            return []

        video_id = video_info["video_id"]
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            ytdlp_bin(),
            "--no-playlist",
            "--js-runtimes", f"deno:{DENO_PATH}/deno",
            "-f", self.format_selector,
            "--merge-output-format", self.container,
            "-N", str(self.concurrent_fragments),
            "-o", str(output_dir / f"{CLIP_PREFIX}%(section_start)s.%(ext)s"),
            "--socket-timeout", "30",
            "--retries", str(self.config["processing"]["max_retries"]),
            "--user-agent", random.choice(USER_AGENTS),
            "--no-check-certificates",
            "--geo-bypass",
            "--no-warnings",
            "--quiet",
        ]

        # Re-encode around the cut points. Without this the clip starts at the
        # preceding keyframe, which on 24fps DASH video can be seconds early and
        # would break alignment with the paired audio clip.
        if self.force_keyframes:
            cmd.append("--force-keyframes-at-cuts")

        for w in windows:
            cmd.extend(["--download-sections", f"*{w['start_time_sec']}-{w['end_time_sec']}"])

        if self.proxy:
            cmd.extend(["--proxy", self.proxy])

        cmd.append(video_info["url"])

        timeout = self.timeout_base + self.timeout_per_clip * len(windows)

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, env=self.env
            )
        except subprocess.TimeoutExpired:
            # Whatever finished is still usable, but say how much was lost --
            # a silent partial result here looks identical to a short video.
            got = len(self._collect(video_id, windows, output_dir))
            self.logger.warning(
                f"Clip fetch timed out for {video_id} after {timeout}s "
                f"({got}/{len(windows)} clips done)"
            )
            return self._collect(video_id, windows, output_dir)

        if result.returncode != 0:
            stderr = result.stderr or ""
            if any(m in stderr.lower() for m in RATE_LIMIT_MARKERS):
                raise RateLimitedError(f"{video_id}: {self._first_error_line(stderr)}")
            self.logger.debug(f"Clip fetch returned {result.returncode} for {video_id}")

        return self._collect(video_id, windows, output_dir)

    def _collect(self, video_id, windows, output_dir):
        """Match produced files back to the windows that asked for them.

        yt-dlp names each file after the *requested* section start, so the match
        is on that value rather than on ordering, which is not guaranteed.
        """
        produced = {}
        for path in output_dir.glob(f"{CLIP_PREFIX}*"):
            if path.suffix == ".part" or not path.is_file():
                continue
            m = _CLIP_RE.match(path.stem)
            if m:
                produced[float(m.group("start"))] = path

        clips = []
        for w in windows:
            start = float(w["start_time_sec"])
            match = produced.get(start)
            if match is None:
                # Tolerate float formatting differences in the filename.
                for got, path in produced.items():
                    if abs(got - start) < 0.01:
                        match = path
                        break
            if match is None:
                self.logger.debug(f"No clip produced for {video_id} @ {start}")
                continue
            if match.stat().st_size < 2048:
                self.logger.debug(f"Clip too small for {video_id} @ {start}")
                self._cleanup(match)
                continue

            clips.append({**w, "video_path": match})

        if len(clips) < len(windows):
            self.logger.debug(
                f"{video_id}: {len(clips)}/{len(windows)} clips produced"
            )
        return clips

    # ------------------------------------------------------------------ audio

    def extract_audio(self, clip_path, out_wav):
        """Pull the audio half of a clip out of the muxed mp4.

        Taking the audio from the same file as the video guarantees the pair
        covers an identical span -- cutting them from two separate downloads
        leaves them a frame or two apart.
        """
        cmd = [
            "ffmpeg", "-v", "error",
            "-i", str(clip_path),
            "-vn",
            "-ar", str(self.clip_audio_sample_rate),
            "-ac", str(self.config["audio"]["channels"]),
            "-acodec", "pcm_s16le",
            "-y", str(out_wav),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            self.logger.warning(f"Audio extraction timed out for {clip_path.name}")
            return None

        if result.returncode != 0:
            self.logger.warning(f"Audio extraction failed for {clip_path.name}")
            self._cleanup(out_wav)
            return None

        out_wav = Path(out_wav)
        if out_wav.exists() and out_wav.stat().st_size > 1000:
            return out_wav

        self._cleanup(out_wav)
        return None

    # ------------------------------------------------------------------ probe

    def probe(self, clip_path):
        """Width/height/fps/codec of a clip, recorded in the metadata."""
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate,codec_name",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=0",
            str(clip_path),
        ]
        info = {}
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                return info
            for line in result.stdout.splitlines():
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip()
        except Exception:
            return info

        fps = info.get("r_frame_rate", "")
        if "/" in fps:
            num, den = fps.split("/", 1)
            try:
                info["fps"] = round(float(num) / float(den), 3) if float(den) else None
            except (ValueError, ZeroDivisionError):
                info["fps"] = None
        return info

    def duration(self, path):
        """Container duration in seconds, or None.

        Worth measuring per file rather than assuming the requested window: a
        video track can only end on a frame boundary (42ms at 24fps) and an AAC
        audio track on a packet boundary (~23ms), so the two halves of a pair
        differ by a few tens of ms at the tail even though they start together.
        Recording both lets a loader take the exact min instead of trusting a
        nominal length -- padding them to match would stamp a block of digital
        silence onto every clip, which is exactly the kind of artefact a
        detector learns instead of the thing we want it to learn.
        """
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1",
            str(path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                return None
            return round(float(result.stdout.strip()), 3)
        except (ValueError, Exception):
            return None

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _first_error_line(stderr):
        for line in stderr.splitlines():
            if line.startswith("ERROR:"):
                return line.strip()[:200]
        return stderr.strip().splitlines()[-1][:200] if stderr.strip() else "unknown error"

    def _cleanup(self, path):
        try:
            if path and Path(path).exists():
                Path(path).unlink()
        except Exception:
            pass
