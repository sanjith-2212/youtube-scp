import os
import csv
import json
import queue
import threading
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
from .notify import notify, notify_error


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

# Pushed onto a queue to tell a consumer there is no more work.
DONE = object()


class LanguagePipeline:
    """Processes all channels for a single language.

    Three stages run concurrently, connected by bounded queues:

        fetch (N threads)  -> gpu_q -> transcribe (1 thread) -> upload_q -> upload (M threads)

    Splitting them matters because the stages contend for different resources
    and, run serially, each idles while the other works: measured on English
    videos the network legs take ~4.5s and VAD ~2.7s while the GPU sits unused,
    then Whisper takes ~3.7s while the network sits unused.

    VAD lives in the fetch stage rather than next to Whisper. It is CPU work,
    so pinning it to the single serialized GPU thread would have kept ~40% of
    that thread's time off the GPU; in the fetch threads it overlaps everything
    else and scales with the worker count.

    Retries stay simple because a video is downloaded whole before it is split.
    The unit crossing the queue is therefore self-contained: a download that
    fails or gets throttled is requeued by the fetch stage and never reaches
    the GPU, so no downstream stage can be left holding half a video.

    The queues are bounded, which is what keeps disk in check -- each queued
    video is ~25 MB of WAV, so an unbounded prefetch would fill the disk during
    any GPU stall.
    """

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

        p = config["processing"]
        self.fetch_workers = int(p.get("fetch_workers", 4))
        self.upload_workers = int(p.get("upload_workers", 4))
        self.prefetch = int(p.get("prefetch_videos", 8))
        self.segs_per_video = int(p.get("max_segments_per_video", 5))

        # Guards the work deque, the retry counters and the shared tallies.
        self._lock = threading.Lock()
        # Serialises appends to processed_videos.txt from fetch and GPU stages.
        self._processed_lock = threading.Lock()
        self._rl_alerted = False

        self.stats = {
            "attempted": 0,
            "succeeded": 0,
            "download_failed": 0,
            "no_segments": 0,
            "lang_mismatches": 0,
            "segments": 0,
            "duration": 0.0,
            "rate_limited_drops": 0,
            "uploaded": 0,
            "upload_failed": 0,
        }

    # ------------------------------------------------------------------ util

    def get_processed_videos(self):
        if self.processed_videos_path.exists():
            with open(self.processed_videos_path) as f:
                return set(f.read().strip().split("\n"))
        return set()

    def mark_video_processed(self, video_id):
        with self._processed_lock:
            with open(self.processed_videos_path, "a") as f:
                f.write(f"{video_id}\n")

    def _healthy_proxies(self):
        """Drop proxies that are not answering before the run starts.

        A dead proxy does not fail fast: yt-dlp burns its retries and then the
        m3u8 fallback burns more, so a single bad entry in the list quietly
        costs minutes per video it is picked for.
        """
        import urllib.request

        healthy, dead = [], []
        for p in self.proxies:
            if not p:
                healthy.append(p)  # direct egress, nothing to probe
                continue
            try:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": p, "https": p})
                )
                opener.open("https://www.youtube.com/", timeout=10).read(1)
                healthy.append(p)
            except Exception as e:
                dead.append(f"{p} ({type(e).__name__})")

        if dead:
            msg = f"{len(dead)} proxy/proxies unreachable, running without them:\n" + "\n".join(dead)
            self.logger.warning(f"[{self.lang}] {msg}")
            notify_error(self.config, f"{self.lang}: dead proxies", msg)
        if not healthy:
            self.logger.error("Every proxy is dead and direct egress is not configured")
            healthy = [None]
        self.logger.info(f"[{self.lang}] Using {len(healthy)} egress route(s)")
        return healthy

    def _bump(self, key, amount=1):
        with self._lock:
            self.stats[key] += amount

    def _cleanup_raw(self, audio_path):
        try:
            if audio_path and Path(audio_path).exists():
                Path(audio_path).unlink()
        except Exception:
            pass

    def _pick_spread(self, segments, n):
        """n segments sampled evenly across the video; the rest are deleted."""
        if len(segments) <= n:
            return segments
        step = len(segments) / n
        keep_idx = {int(i * step) for i in range(n)}
        keep = [s for i, s in enumerate(segments) if i in keep_idx]
        for i, s in enumerate(segments):
            if i not in keep_idx:
                self._cleanup_raw(s["path"])
        return keep

    # ----------------------------------------------------------- stage: fetch

    def _fetch_worker(self, work, gpu_q, rl_retries, max_rl_retries, stop):
        """Download -> WAV -> VAD. One AudioSegmenter per thread: the Silero
        model is small, and torch drops the GIL during inference so the VAD
        legs genuinely overlap."""
        segmenter = AudioSegmenter(self.config, self.logger)

        while not stop.is_set():
            with self._lock:
                if not work:
                    return
                video = work.popleft()
            video_id = video["video_id"]

            try:
                # Wait for a slot in the budget shared by every language.
                self.limiter.acquire()
                if stop.is_set():
                    return

                proxy = random.choice(self.proxies)
                downloader = AudioDownloader(self.config, self.logger, proxy=proxy)
                audio_path = downloader.download_audio(video, self.raw_dir)

                if not audio_path:
                    self._bump("attempted")
                    self._bump("download_failed")
                    self.mark_video_processed(video_id)
                    continue

                segments = segmenter.segment_audio(audio_path, self.segments_dir)
                self._cleanup_raw(audio_path)

                if not segments:
                    self._bump("attempted")
                    self._bump("no_segments")
                    self.logger.debug(f"No valid segments from {video_id}")
                    self.mark_video_processed(video_id)
                    continue

                segments = self._pick_spread(segments, self.segs_per_video)

                # Blocks once the GPU is `prefetch` videos behind, which is the
                # backpressure that keeps staging from filling the disk. Done on
                # a timeout so a dead GPU stage cannot wedge this thread here.
                while not stop.is_set():
                    try:
                        gpu_q.put((video, segments), timeout=5)
                        break
                    except queue.Full:
                        continue
                else:
                    return
                self._bump("attempted")

                with self._lock:
                    self._rl_alerted = False  # episode over

            except RateLimitedError as e:
                # Not this video's fault -- the IP is throttled. Put it back and
                # stall every worker, otherwise we burn the whole pending pool
                # marking good videos as processed during the block.
                cooldown = self.limiter.penalize()
                with self._lock:
                    attempts = rl_retries.get(video_id, 0) + 1
                    rl_retries[video_id] = attempts
                    should_alert = not self._rl_alerted
                    if should_alert:
                        self._rl_alerted = True

                if attempts <= max_rl_retries:
                    with self._lock:
                        work.appendleft(video)
                    self.logger.warning(
                        f"[{self.lang}] Rate limited ({e}); attempt {attempts}/{max_rl_retries}, "
                        f"global cooldown {cooldown/60:.1f} min"
                    )
                    if should_alert:
                        notify_error(
                            self.config,
                            f"{self.lang}: rate limited",
                            f"YouTube bot check after {self.stats['succeeded']} videos.\n"
                            f"All fetch workers stalled {cooldown/60:.1f} min.\n"
                            f"{self.stats['segments']} segments banked, "
                            f"{self.stats['uploaded']} uploaded.",
                        )
                else:
                    self._bump("rate_limited_drops")
                    self.logger.error(
                        f"[{self.lang}] Giving up on {video_id} after {attempts} rate-limited attempts"
                    )
                    self.mark_video_processed(video_id)
                continue

            except Exception as e:
                self.logger.error(f"Fetch error for {video_id}: {e}")
                self.mark_video_processed(video_id)
                continue

    # ------------------------------------------------------- stage: transcribe

    def _gpu_stage(self, gpu_q, upload_q, csv_writer, csv_file, existing_files, total_videos):
        """The only stage touching the GPU, so it stays single-threaded."""
        transcriber = Transcriber(self.config, self.logger)

        while True:
            item = gpu_q.get()
            if item is DONE:
                return
            video, segments = item
            video_id = video["video_id"]

            try:
                # Language verification: the first segment stands in for the video.
                detected = transcriber.detect_language(segments[0]["path"])
                expected = self.lang if self.lang != "zh-cn" else "zh"
                if detected and detected != expected:
                    self.logger.info(
                        f"Language mismatch for {video_id} (channel: {video['channel']}): "
                        f"expected {expected}, detected {detected}. Skipping."
                    )
                    self._bump("lang_mismatches")
                    for s in segments:
                        self._cleanup_raw(s["path"])
                    self.mark_video_processed(video_id)
                    continue

                seg_paths = [s["path"] for s in segments]
                transcripts = transcriber.transcribe_batch(seg_paths, language=self.lang)

                for seg, transcript in zip(segments, transcripts):
                    if seg["filename"] in existing_files:
                        # Already in S3 from an earlier run; nothing will pick
                        # this file up downstream, so drop it here.
                        self._cleanup_raw(seg["path"])
                        continue

                    csv_writer.writerow({
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
                    })
                    csv_file.flush()

                    upload_q.put((seg["path"], seg["filename"]))
                    self._bump("segments")
                    self._bump("duration", seg["duration_sec"])

                self.mark_video_processed(video_id)
                self._bump("succeeded")

                done = self.stats["succeeded"]
                if done % 10 == 0:
                    self.logger.info(
                        f"[{self.lang}] {self.stats['attempted']}/{total_videos} fetched, "
                        f"{done} ok, {self.stats['lang_mismatches']} lang_mismatch, "
                        f"{self.stats['segments']} segments "
                        f"({self.stats['duration']/3600:.3f}h), "
                        f"{self.stats['uploaded']} uploaded, "
                        f"gpu_q={gpu_q.qsize()} up_q={upload_q.qsize()}"
                    )

            except Exception as e:
                self.logger.error(f"Transcribe error for {video_id}: {e}")
                self.mark_video_processed(video_id)
                continue
            finally:
                gpu_q.task_done()

    # ---------------------------------------------------------- stage: upload

    def _upload_worker(self, upload_q, uploader):
        """Uploads land in S3 while the other stages keep running; the local
        WAV is dropped as soon as it is safely stored."""
        while True:
            item = upload_q.get()
            if item is DONE:
                return
            path, filename = item
            try:
                if uploader.upload_file(path, self.lang, filename):
                    self._bump("uploaded")
                    if self.config["local_staging"]["cleanup_after_upload"]:
                        self._cleanup_raw(path)
                else:
                    self._bump("upload_failed")
            except Exception as e:
                self._bump("upload_failed")
                self.logger.error(f"Upload error for {filename}: {e}")

    def _metadata_worker(self, uploader, stop):
        """metadata.csv is small and rewritten constantly, so it is refreshed on
        a timer rather than once per segment."""
        while not stop.wait(60):
            try:
                if self.metadata_path.exists():
                    uploader.upload_metadata(self.metadata_path, self.lang)
            except Exception as e:
                self.logger.debug(f"Periodic metadata upload failed: {e}")

    # -------------------------------------------------------------------- run

    def run(self):
        self.logger.info(f"Starting pipeline for language: {self.lang}")
        self.logger.info(f"Channels to process: {len(self.channels)}")

        all_videos = self._enumerate_with_checkpoint()
        self.logger.info(f"Total videos enumerated for {self.lang}: {len(all_videos)}")

        processed = self.get_processed_videos()
        pending = [v for v in all_videos if v["video_id"] not in processed]
        self.logger.info(f"Videos to process (after filtering): {len(pending)}")

        # Round-robin across channels so an interrupted run still leaves a
        # spread of sources rather than everything from the first few channels.
        by_channel = {}
        for v in pending:
            by_channel.setdefault(v["channel"], []).append(v)

        interleaved = []
        channel_queues = {ch: list(vids) for ch, vids in by_channel.items()}
        while channel_queues:
            for ch in list(channel_queues.keys()):
                if channel_queues[ch]:
                    interleaved.append(channel_queues[ch].pop(0))
                else:
                    del channel_queues[ch]

        self.logger.info(
            f"Interleaved {len(interleaved)} videos across {len(by_channel)} channels"
        )

        if not interleaved:
            self.logger.info(f"Nothing to do for {self.lang}")
            return {"language": self.lang, "total_segments": 0, "total_hours": 0.0,
                    "rate_limited_drops": 0}

        notify(
            self.config,
            f"{self.lang}: enumeration done",
            f"{len(interleaved)} videos queued across {len(by_channel)} channels\n"
            f"{len(all_videos) - len(pending)} already processed, skipped\n"
            f"{self.fetch_workers} fetch / 1 gpu / {self.upload_workers} upload threads",
            tags=["mag"],
        )

        self.proxies = self._healthy_proxies()

        uploader = S3Uploader(self.config, self.logger)
        existing_files = uploader.check_existing(self.lang)
        self.logger.info(f"Already uploaded for {self.lang}: {len(existing_files)} files")

        csv_exists = self.metadata_path.exists()
        csv_file = open(self.metadata_path, "a", newline="", encoding="utf-8")
        csv_writer = csv.DictWriter(csv_file, fieldnames=METADATA_FIELDS)
        if not csv_exists:
            csv_writer.writeheader()

        work = deque(interleaved)
        rl_retries = {}
        max_rl_retries = self.config["processing"].get("max_ratelimit_retries", 3)

        gpu_q = queue.Queue(maxsize=self.prefetch)
        upload_q = queue.Queue(maxsize=self.prefetch * self.segs_per_video * 2)
        stop = threading.Event()

        t_start = time.time()

        fetchers = [
            threading.Thread(
                target=self._fetch_worker,
                args=(work, gpu_q, rl_retries, max_rl_retries, stop),
                name=f"fetch-{self.lang}-{i}",
                daemon=True,
            )
            for i in range(self.fetch_workers)
        ]
        uploaders = [
            threading.Thread(
                target=self._upload_worker,
                args=(upload_q, uploader),
                name=f"upload-{self.lang}-{i}",
                daemon=True,
            )
            for i in range(self.upload_workers)
        ]
        meta_thread = threading.Thread(
            target=self._metadata_worker, args=(uploader, stop),
            name=f"meta-{self.lang}", daemon=True,
        )

        for t in fetchers + uploaders:
            t.start()
        meta_thread.start()

        # Nothing else can join the fetchers -- this thread is about to block on
        # the GPU loop -- so a watcher closes the queue when they are all done.
        def close_gpu_q():
            for t in fetchers:
                t.join()
            gpu_q.put(DONE)

        watcher = threading.Thread(target=close_gpu_q, name=f"watch-{self.lang}", daemon=True)
        watcher.start()

        try:
            self._gpu_stage(gpu_q, upload_q, csv_writer, csv_file,
                            existing_files, len(interleaved))
        finally:
            stop.set()
            watcher.join(timeout=30)
            # Drain the uploads already queued before retiring the workers.
            for _ in uploaders:
                upload_q.put(DONE)
            for t in uploaders:
                t.join(timeout=300)
            csv_file.close()
            try:
                uploader.upload_metadata(self.metadata_path, self.lang)
            except Exception as e:
                self.logger.warning(f"Final metadata upload failed: {e}")

        elapsed = time.time() - t_start
        s = self.stats
        self.logger.info(
            f"Pipeline complete for {self.lang}: {s['segments']} segments, "
            f"{s['duration']/3600:.2f} hours, {s['succeeded']} videos ok, "
            f"{s['lang_mismatches']} lang mismatches, "
            f"{s['download_failed']} download failures, "
            f"{s['no_segments']} yielded no segments, "
            f"{s['rate_limited_drops']} dropped to rate limiting, "
            f"{s['uploaded']} uploaded ({s['upload_failed']} failed), "
            f"in {elapsed/60:.1f} min"
        )

        return {
            "language": self.lang,
            "total_segments": s["segments"],
            "total_hours": s["duration"] / 3600,
            "rate_limited_drops": s["rate_limited_drops"],
            "videos_ok": s["succeeded"],
            "uploaded": s["uploaded"],
            "upload_failed": s["upload_failed"],
            "elapsed_min": round(elapsed / 60, 1),
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

        with open(checkpoint_path, "w") as f:
            json.dump(all_videos, f)

        return all_videos


def run_language(lang, config, channels):
    """Entry point for running a single language pipeline (used by multiprocessing)."""
    pipeline = LanguagePipeline(lang, config, channels)
    return pipeline.run()
