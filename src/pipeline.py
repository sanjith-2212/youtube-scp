import os
import csv
import json
import time
import random
import shutil
import traceback
from collections import deque
from pathlib import Path
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

from .utils import load_config, load_channels, setup_logger, get_proxy_list
from .enumerator import VideoEnumerator
from .downloader import AudioDownloader, RateLimitedError
from .ratelimit import GlobalRateLimiter
from .segmenter import AudioSegmenter
from .transcriber import Transcriber
from .uploader import S3Uploader


METADATA_FIELDS = [
    "filename",
    "duration_sec",
    "source_video_id",
    "source_channel",
    "source_url",
    "start_time_sec",
    "end_time_sec",
    "language",
    "speech_ratio",
    "sample_rate",
    "bit_depth",
    "scraped_at",
    "video_title",
    "transcript",
]


class LanguagePipeline:
    """Processes all channels for a single language."""

    def __init__(self, lang, config, channels):
        self.lang = lang
        self.config = config
        self.channels = channels
        self.logger = setup_logger(f"pipeline.{lang}", config, lang=lang)

        self.staging_dir = Path(config["local_staging"]["dir"]) / lang
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.segments_dir = self.staging_dir / "segments"
        self.segments_dir.mkdir(exist_ok=True)
        self.raw_dir = self.staging_dir / "raw"
        self.raw_dir.mkdir(exist_ok=True)

        self.metadata_path = self.staging_dir / "metadata.csv"
        self.processed_videos_path = self.staging_dir / "processed_videos.txt"

        self.proxies = get_proxy_list(config)
        self.limiter = GlobalRateLimiter(config, self.logger)

    def get_processed_videos(self):
        if self.processed_videos_path.exists():
            with open(self.processed_videos_path) as f:
                return set(f.read().strip().split("\n"))
        return set()

    def mark_video_processed(self, video_id):
        with open(self.processed_videos_path, "a") as f:
            f.write(f"{video_id}\n")

    def run(self):
        self.logger.info(f"Starting pipeline for language: {self.lang}")
        self.logger.info(f"Channels to process: {len(self.channels)}")

        # Phase 1: Enumerate videos (with checkpoint for resume)
        all_videos = self._enumerate_with_checkpoint()
        self.logger.info(f"Total videos enumerated for {self.lang}: {len(all_videos)}")

        # Filter already processed
        processed = self.get_processed_videos()
        pending = [v for v in all_videos if v["video_id"] not in processed]
        self.logger.info(f"Videos to process (after filtering): {len(pending)}")

        # Organize by channel for round-robin diversity
        # Strategy: take 5 segments from each channel's video, cycle through all channels, repeat
        by_channel = {}
        for v in pending:
            by_channel.setdefault(v["channel"], []).append(v)

        # Interleave: one video per channel per round
        interleaved = []
        channel_queues = {ch: list(vids) for ch, vids in by_channel.items()}
        while channel_queues:
            empty = []
            for ch in list(channel_queues.keys()):
                if channel_queues[ch]:
                    interleaved.append(channel_queues[ch].pop(0))
                else:
                    empty.append(ch)
            for ch in empty:
                del channel_queues[ch]

        self.logger.info(f"Interleaved {len(interleaved)} videos across {len(by_channel)} channels")

        # Phase 2: Download, segment, transcribe, upload
        uploader = S3Uploader(self.config, self.logger)
        segmenter = AudioSegmenter(self.config, self.logger)
        transcriber = Transcriber(self.config, self.logger)

        existing_files = uploader.check_existing(self.lang)
        self.logger.info(f"Already uploaded for {self.lang}: {len(existing_files)} files")

        # Initialize or append metadata CSV
        csv_exists = self.metadata_path.exists()
        csv_file = open(self.metadata_path, "a", newline="", encoding="utf-8")
        csv_writer = csv.DictWriter(csv_file, fieldnames=METADATA_FIELDS)
        if not csv_exists:
            csv_writer.writeheader()

        total_segments = 0
        total_duration = 0.0
        batch_for_upload = []
        videos_attempted = 0
        videos_succeeded = 0
        lang_mismatches = 0
        segs_per_video = self.config["processing"].get("max_segments_per_video", 5)

        queue = deque(interleaved)
        rl_retries = {}
        max_rl_retries = self.config["processing"].get("max_ratelimit_retries", 3)
        rate_limited_drops = 0

        while queue:
            video = queue.popleft()
            video_id = video["video_id"]
            videos_attempted += 1

            try:
                # Wait for a slot in the global budget shared by all languages.
                self.limiter.acquire()

                # Download
                proxy = random.choice(self.proxies)
                downloader = AudioDownloader(self.config, self.logger, proxy=proxy)
                audio_path = downloader.download_audio(video, self.raw_dir)

                if not audio_path:
                    self.mark_video_processed(video_id)
                    continue

                # Segment with VAD
                segments = segmenter.segment_audio(audio_path, self.segments_dir)

                if not segments:
                    self.logger.debug(f"No valid segments from {video_id}")
                    self._cleanup_raw(audio_path)
                    self.mark_video_processed(video_id)
                    continue

                # Take up to segs_per_video segments for diversity
                segments = segments[:segs_per_video]

                # Language verification: check first segment matches target language
                detected = transcriber.detect_language(segments[0]["path"])
                expected = self.lang if self.lang != "zh-cn" else "zh"
                if detected and detected != expected:
                    self.logger.info(
                        f"Language mismatch for {video_id} (channel: {video['channel']}): "
                        f"expected {expected}, detected {detected}. Skipping."
                    )
                    lang_mismatches += 1
                    self._cleanup_raw(audio_path)
                    for s in segments:
                        self._cleanup_raw(s["path"])
                    self.mark_video_processed(video_id)
                    continue

                # Transcribe segments
                seg_paths = [s["path"] for s in segments]
                transcripts = transcriber.transcribe_batch(seg_paths, language=self.lang)

                # Write metadata and queue for upload
                for seg, transcript in zip(segments, transcripts):
                    if seg["filename"] in existing_files:
                        continue

                    row = {
                        "filename": seg["filename"],
                        "duration_sec": seg["duration_sec"],
                        "source_video_id": video_id,
                        "source_channel": video["channel"],
                        "source_url": video["url"],
                        "start_time_sec": seg["start_time_sec"],
                        "end_time_sec": seg["end_time_sec"],
                        "language": self.lang,
                        "speech_ratio": seg["speech_ratio"],
                        "sample_rate": self.config["audio"]["sample_rate"],
                        "bit_depth": self.config["audio"]["bit_depth"],
                        "scraped_at": datetime.utcnow().isoformat(),
                        "video_title": video["title"],
                        "transcript": transcript,
                    }
                    csv_writer.writerow(row)
                    csv_file.flush()

                    batch_for_upload.append((seg["path"], seg["filename"]))
                    total_segments += 1
                    total_duration += seg["duration_sec"]

                # Upload in batches
                if len(batch_for_upload) >= self.config["processing"]["batch_upload_size"]:
                    self._upload_batch(uploader, batch_for_upload)
                    batch_for_upload = []
                    csv_file.flush()
                    uploader.upload_metadata(self.metadata_path, self.lang)

                # Cleanup raw file to save disk
                self._cleanup_raw(audio_path)
                self.mark_video_processed(video_id)
                videos_succeeded += 1

                # Progress log every 10 videos
                if videos_attempted % 10 == 0:
                    self.logger.info(
                        f"[{self.lang}] Progress: {videos_attempted}/{len(interleaved)} videos, "
                        f"{videos_succeeded} ok, {lang_mismatches} lang_mismatch, "
                        f"{total_segments} segments, {total_duration/3600:.3f}h"
                    )

            except RateLimitedError as e:
                # Not this video's fault -- the IP is throttled. Put it back and
                # stall every worker, otherwise we burn the whole pending pool
                # marking good videos as processed during the block.
                videos_attempted -= 1
                attempts = rl_retries.get(video_id, 0) + 1
                rl_retries[video_id] = attempts
                cooldown = self.limiter.penalize()

                if attempts <= max_rl_retries:
                    queue.appendleft(video)
                    self.logger.warning(
                        f"[{self.lang}] Rate limited ({e}); attempt {attempts}/{max_rl_retries}, "
                        f"global cooldown {cooldown/60:.1f} min"
                    )
                else:
                    rate_limited_drops += 1
                    self.logger.error(
                        f"[{self.lang}] Giving up on {video_id} after {attempts} rate-limited attempts"
                    )
                    self.mark_video_processed(video_id)
                continue

            except Exception as e:
                self.logger.error(f"Error processing video {video_id}: {e}")
                self.mark_video_processed(video_id)
                continue

        # Upload remaining batch
        if batch_for_upload:
            self._upload_batch(uploader, batch_for_upload)

        # Upload metadata CSV
        csv_file.close()
        uploader.upload_metadata(self.metadata_path, self.lang)

        self.logger.info(
            f"Pipeline complete for {self.lang}: "
            f"{total_segments} segments, {total_duration/3600:.2f} hours, "
            f"{lang_mismatches} lang mismatches, {rate_limited_drops} dropped to rate limiting"
        )

        return {
            "language": self.lang,
            "total_segments": total_segments,
            "total_hours": total_duration / 3600,
            "rate_limited_drops": rate_limited_drops,
        }

    def _enumerate_with_checkpoint(self):
        """Enumerate videos with checkpoint so we don't redo on crash resume."""
        checkpoint_path = self.staging_dir / "enumerated_videos.json"

        if checkpoint_path.exists():
            try:
                with open(checkpoint_path) as f:
                    data = json.load(f)
                self.logger.info(f"Resuming from checkpoint: {len(data)} videos already enumerated")
                return data
            except (json.JSONDecodeError, Exception):
                pass

        enumerator = VideoEnumerator(self.config, self.logger)
        all_videos = []
        for channel in self.channels:
            try:
                videos = enumerator.list_videos(channel)
                all_videos.extend(videos)
            except Exception as e:
                self.logger.error(f"Failed to enumerate channel '{channel}': {e}")
                continue

        # Save checkpoint
        with open(checkpoint_path, "w") as f:
            json.dump(all_videos, f)

        return all_videos

    def _upload_batch(self, uploader, batch):
        success, failed = uploader.upload_batch(batch, self.lang)
        if self.config["local_staging"]["cleanup_after_upload"] and success > 0:
            for path, _ in batch:
                try:
                    if Path(path).exists():
                        Path(path).unlink()
                except Exception:
                    pass

    def _cleanup_raw(self, audio_path):
        try:
            if audio_path and Path(audio_path).exists():
                Path(audio_path).unlink()
        except Exception:
            pass


def run_language(lang, config, channels):
    """Entry point for running a single language pipeline (used by multiprocessing)."""
    pipeline = LanguagePipeline(lang, config, channels)
    return pipeline.run()
