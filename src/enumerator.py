import subprocess
import json
import time
import random
import os

from .utils import ytdlp_bin

DENO_PATH = "/home/ubuntu/.deno/bin"


class VideoEnumerator:
    def __init__(self, config, logger):
        self.logger = logger
        self.max_videos = config["processing"]["max_videos_per_channel"]
        # Bounds on source length. The ceiling used to be a hardcoded 2 hours,
        # which silently dropped every multi-hour upload -- on the AI audiobook
        # channels that is most of the catalogue, and those are the longest and
        # most speech-dense sources available.
        self.min_duration = float(config["processing"].get("min_video_duration_sec", 30))
        self.max_duration = float(config["processing"].get("max_video_duration_sec", 36000))
        # Enumeration is flat-playlist metadata, far cheaper than extraction,
        # so it gets its own small jitter rather than the global download budget.
        self.delay_min = config["processing"].get("request_delay_min", 1.0)
        self.delay_max = config["processing"].get("request_delay_max", 3.0)
        self.env = os.environ.copy()
        self.env["PATH"] = f"{DENO_PATH}:{self.env.get('PATH', '')}"

    def list_videos(self, channel_name):
        """List videos from a channel using yt-dlp search + channel extraction."""
        # First, try to find the channel URL via yt-dlp search
        channel_url = self._resolve_channel(channel_name)
        if not channel_url:
            return []

        # Extract video list from channel
        videos = self._extract_channel_videos(channel_url, channel_name)
        return videos

    def _resolve_channel(self, channel_name):
        """Resolve channel name to a channel/playlist URL."""
        # An @handle or URL identifies one channel exactly, so skip the search:
        # for generic names ytsearch can resolve to a different channel that
        # merely mentions the term, which would quietly mix in the wrong source.
        direct = self._direct_channel_url(channel_name)
        if direct:
            self.logger.info(f"Using '{channel_name}' directly -> {direct}")
            return direct

        # Try direct ytsearch for the channel
        cmd = [
            ytdlp_bin(),
            "--flat-playlist",
            "--print", "%(channel_url)s",
            "--playlist-items", "1",
            f"ytsearch1:{channel_name} channel",
            "--no-warnings",
        ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, env=self.env
            )
            if result.returncode == 0 and result.stdout.strip():
                url = result.stdout.strip().split("\n")[0]
                if "youtube.com" in url:
                    self.logger.info(f"Resolved '{channel_name}' -> {url}")
                    return url
        except (subprocess.TimeoutExpired, Exception) as e:
            self.logger.warning(f"Search failed for '{channel_name}': {e}")

        # Fallback: try common URL patterns
        for pattern in [
            f"https://www.youtube.com/@{channel_name.replace(' ', '')}/videos",
            f"https://www.youtube.com/c/{channel_name.replace(' ', '')}/videos",
            f"https://www.youtube.com/{channel_name.replace(' ', '')}/videos",
        ]:
            if self._check_url_exists(pattern):
                self.logger.info(f"Resolved '{channel_name}' -> {pattern}")
                return pattern

        # Last resort: search and get the channel from first result
        search_url = f"https://www.youtube.com/results?search_query={channel_name.replace(' ', '+')}"
        cmd = [
            ytdlp_bin(),
            "--flat-playlist",
            "--print", "%(url)s %(channel_url)s",
            "--playlist-items", "1:1",
            f"ytsearch1:{channel_name}",
            "--no-warnings",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split()
                for part in parts:
                    if "youtube.com/" in part and "/watch" not in part:
                        return part + "/videos"
        except Exception:
            pass

        self.logger.warning(f"Could not resolve channel: {channel_name}")
        return None

    @staticmethod
    def _direct_channel_url(channel):
        """Channel URL for an @handle or URL entry, else None for a plain name."""
        entry = channel.strip()
        if entry.startswith("@") and " " not in entry:
            return f"https://www.youtube.com/{entry}/videos"
        if entry.startswith(("http://", "https://")) or entry.startswith("www.youtube.com"):
            url = entry if entry.startswith("http") else f"https://{entry}"
            # /watch and /playlist URLs are not channels; leave those alone.
            if "/watch" in url or "/playlist" in url:
                return None
            return url.rstrip("/")
        return None

    def _check_url_exists(self, url):
        cmd = [
            ytdlp_bin(),
            "--flat-playlist",
            "--print", "%(id)s",
            "--playlist-items", "1",
            url,
            "--no-warnings",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=20, env=self.env)
            return result.returncode == 0 and result.stdout.strip()
        except Exception:
            return False

    def _extract_channel_videos(self, channel_url, channel_name):
        """Extract video IDs and titles from a channel."""
        # Ensure we're hitting the videos tab
        if "/videos" not in channel_url:
            channel_url = channel_url.rstrip("/") + "/videos"

        cmd = [
            ytdlp_bin(),
            "--flat-playlist",
            "--print", "%(id)s\t%(title)s\t%(duration)s",
            "--playlist-items", f"1:{self.max_videos}",
            "--no-warnings",
            "--socket-timeout", "30",
            channel_url,
        ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120, env=self.env
            )

            if result.returncode != 0:
                self.logger.warning(
                    f"yt-dlp failed for {channel_name}: {result.stderr[:200]}"
                )
                return []

            videos = []
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) >= 2:
                    video_id = parts[0]
                    title = parts[1]
                    duration = parts[2] if len(parts) > 2 else None

                    # Skip very short videos (likely intros) and anything past
                    # the configured ceiling (live streams, 24/7 loops).
                    if duration and duration != "NA":
                        try:
                            dur = float(duration)
                            if dur < self.min_duration or dur > self.max_duration:
                                continue
                        except ValueError:
                            pass

                    # Duration is kept, not just used as a filter: the AV
                    # pipeline's long-clip mode places its clips at fractions
                    # of the runtime, and looking it up again per video would
                    # cost an extra extraction against a throttled IP.
                    try:
                        duration_val = float(duration) if duration and duration != "NA" else None
                    except ValueError:
                        duration_val = None

                    videos.append({
                        "video_id": video_id,
                        "title": title,
                        "channel": channel_name,
                        "url": f"https://www.youtube.com/watch?v={video_id}",
                        "duration": duration_val,
                    })

            self.logger.info(f"Enumerated {len(videos)} videos from '{channel_name}'")
            return videos

        except subprocess.TimeoutExpired:
            self.logger.warning(f"Timeout enumerating {channel_name}")
            return []
        except Exception as e:
            self.logger.error(f"Error enumerating {channel_name}: {e}")
            return []
