#!/usr/bin/env python3
"""
YouTube AI-generated video scraper -- the fake half of the dataset.

Pulls paired audio+video clips from channels that publish AI-generated video and
uploads them to Azure under <container>/<prefix>/{audio,video}/<lang>/.

Crash-proof: both enumeration and per-video progress are checkpointed, so a
restart picks up where it stopped instead of re-downloading.

  ./run_fake.py --languages en
  ./run_fake.py --languages en --smoke-test        # 1 video, 2 clips
  ./run_fake.py --languages en --dry-run           # enumerate only
"""
import sys
import json
import argparse
import signal
import time
from pathlib import Path
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import set_start_method

sys.path.insert(0, str(Path(__file__).parent))

from src.utils import load_config, setup_logger
from src.av_pipeline import run_language
from src.notify import notify, notify_done, notify_error

CHANNELS_FILE = "ai_channels.yaml"

shutdown_requested = False


def signal_handler(signum, frame):
    global shutdown_requested
    shutdown_requested = True
    print(f"\n[SIGNAL {signum}] Graceful shutdown requested. Finishing current videos...")


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


def load_ai_channels():
    import yaml

    path = Path(__file__).parent / "config" / CHANNELS_FILE
    with open(path) as f:
        return yaml.safe_load(f) or {}


def parse_args():
    p = argparse.ArgumentParser(description="YouTube AI-video scraper (fake dataset)")
    p.add_argument("--languages", nargs="*", default=None,
                   help=f"Languages to process (default: all in {CHANNELS_FILE})")
    p.add_argument("--parallel-languages", type=int, default=None,
                   help="How many language pipelines to run at once (default 2)")
    p.add_argument("--max-videos-per-channel", type=int, default=None,
                   help="Override processing.max_videos_per_channel")
    p.add_argument("--clips-per-video", type=int, default=None,
                   help="Override av_processing.clips_per_video (speech mode)")
    p.add_argument("--clip-mode", choices=("speech", "long"), default=None,
                   help="speech = 1-8s VAD windows; long = fixed-length spans")
    p.add_argument("--clip-duration", type=float, default=None,
                   help="Long-mode clip length in seconds")
    p.add_argument("--long-clips", type=int, default=None,
                   help="Long-mode clips per video")
    p.add_argument("--fetch-workers", type=int, default=None,
                   help="Videos fetched concurrently")
    p.add_argument("--upload-workers", type=int, default=None,
                   help="Concurrent blob uploads")
    p.add_argument("--no-transcribe", action="store_true",
                   help="Skip Whisper transcription (keeps the language check)")
    p.add_argument("--requests-per-min", type=int, default=None,
                   help="Override the global download budget")
    p.add_argument("--force", action="store_true",
                   help="Ignore the language-level progress file and run anyway. "
                        "Per-video resume still applies, so finished videos are "
                        "skipped -- use this after adding channels or raising "
                        "--max-videos-per-channel")
    p.add_argument("--smoke-test", action="store_true",
                   help="1 video per channel, 2 clips per video")
    p.add_argument("--dry-run", action="store_true",
                   help="Enumerate videos only, no download/upload")
    return p.parse_args()


def dry_run(config, channels_config, languages, logger):
    """Enumerate only, and cost the run out before spending bandwidth on it."""
    from src.enumerator import VideoEnumerator
    from src.av_pipeline import AVLanguagePipeline

    enumerator = VideoEnumerator(config, logger)
    av = config["av_processing"]
    long_mode = av.get("clip_mode") == "long"
    clip_len = float(av.get("long_clip_duration_sec", 120))

    # Borrow the pipeline's own sizing so the estimate cannot drift from it.
    sizer = AVLanguagePipeline.__new__(AVLanguagePipeline)
    sizer.long_clips_per_video = av.get("long_clips_per_video") or None
    sizer.clip_interval = float(av.get("clip_interval_sec", 900))
    sizer.min_clips = int(av.get("min_clips_per_video", 2))
    sizer.max_clips = int(av.get("max_clips_per_video", 10))

    # Measured on this channel set: ~29 MB per 120s 720p clip pair.
    mb_per_clip_sec = 29.0 / 120.0

    for lang in languages:
        print(f"\n=== {lang} ===")
        print(f"{'channel':<34} {'vids':>5} {'runtime':>9} {'longest':>8} {'clips':>6} {'GB':>6}")
        tot_v = tot_c = 0
        tot_runtime = tot_mb = 0.0
        for channel in channels_config[lang]:
            name = channel.rsplit("/", 1)[-1]
            try:
                videos = enumerator.list_videos(channel)
            except Exception as e:
                print(f"{name:<34} FAILED ({type(e).__name__})")
                continue
            durs = [v["duration"] for v in videos if v.get("duration")]
            clips = sum(sizer._clips_for_duration(d) for d in durs) if long_mode \
                else len(videos) * int(av.get("clips_per_video", 15))
            mb = clips * clip_len * mb_per_clip_sec
            runtime = sum(durs)
            tot_v += len(videos); tot_c += clips
            tot_runtime += runtime; tot_mb += mb
            print(f"{name:<34} {len(videos):>5} {runtime/3600:>8.1f}h "
                  f"{(max(durs)/3600 if durs else 0):>7.1f}h {clips:>6} {mb/1000:>6.2f}")
        print(f"{'-'*34} {'-'*5} {'-'*9} {'-'*8} {'-'*6} {'-'*6}")
        print(f"{'TOTAL':<34} {tot_v:>5} {tot_runtime/3600:>8.1f}h {'':>8} "
              f"{tot_c:>6} {tot_mb/1000:>6.2f}")
        kept_h = tot_c * clip_len / 3600
        print(f"\n  -> {tot_c} clip pairs = {kept_h:.1f}h of clips from "
              f"{tot_runtime/3600:.0f}h of source, ~{tot_mb/1000:.1f} GB")


def main():
    set_start_method("spawn", force=True)
    args = parse_args()
    config = load_config()
    channels_config = load_ai_channels()
    logger = setup_logger("main_fake", config)

    logger.info(f"AI-video scraper starting at {datetime.utcnow().isoformat()}")
    logger.info(f"Available languages: {list(channels_config.keys())}")

    if not channels_config:
        logger.error(f"No channels configured in config/{CHANNELS_FILE}")
        return

    if args.languages:
        languages = [l for l in args.languages if l in channels_config]
        missing = set(args.languages) - set(languages)
        if missing:
            logger.warning(f"Languages not in {CHANNELS_FILE}: {missing}")
    else:
        languages = list(channels_config.keys())

    if not languages:
        logger.error("No languages to process")
        return

    if args.max_videos_per_channel is not None:
        config["processing"]["max_videos_per_channel"] = args.max_videos_per_channel
    if args.clips_per_video is not None:
        config["av_processing"]["clips_per_video"] = args.clips_per_video
    if args.clip_mode is not None:
        config["av_processing"]["clip_mode"] = args.clip_mode
    if args.clip_duration is not None:
        config["av_processing"]["long_clip_duration_sec"] = args.clip_duration
    if args.long_clips is not None:
        config["av_processing"]["long_clips_per_video"] = args.long_clips
    if args.fetch_workers is not None:
        config["av_processing"]["fetch_workers"] = args.fetch_workers
    if args.upload_workers is not None:
        config["av_processing"]["upload_workers"] = args.upload_workers
    if args.no_transcribe:
        config["av_processing"]["transcribe_clips"] = False
    if args.requests_per_min is not None:
        config["processing"]["global_requests_per_min"] = args.requests_per_min

    if args.smoke_test:
        config["processing"]["max_videos_per_channel"] = 1
        config["av_processing"]["clips_per_video"] = 2
        logger.info("SMOKE TEST: 1 video per channel, 2 clips per video")

    if args.dry_run:
        dry_run(config, channels_config, languages, logger)
        return

    logger.info(f"Processing languages: {languages}")

    # The download budget in ratelimit.py is global, so extra language
    # pipelines do not buy throughput past it -- they only add GPU contention.
    parallel_langs = min(args.parallel_languages or 2, len(languages))
    logger.info(f"Running {parallel_langs} language pipeline(s) in parallel")

    progress_file = Path(config["local_staging"]["dir"]) / "fake" / "run_progress.json"
    progress_file.parent.mkdir(parents=True, exist_ok=True)
    completed_langs = set()
    if progress_file.exists():
        try:
            with open(progress_file) as f:
                completed_langs = set(json.load(f).get("completed", []))
            if completed_langs:
                logger.info(
                    f"Resuming: {len(completed_langs)} already completed: "
                    f"{sorted(completed_langs)}"
                )
        except Exception:
            pass

    if args.force and completed_langs:
        print(f"--force: ignoring progress file (had {sorted(completed_langs)} as done)")
        logger.info(f"--force: ignoring progress for {sorted(completed_langs)}")
        completed_langs = set()

    pending_langs = [l for l in languages if l not in completed_langs]
    logger.info(
        f"Languages to process: {len(pending_langs)} "
        f"(skipping {len(completed_langs)} already done)"
    )
    if not pending_langs:
        # Loud on stdout: this is the one outcome where the run exits in a
        # second having done nothing, and a message buried in the log file
        # reads as a silent failure.
        print(
            f"Nothing to do: {sorted(set(languages) & completed_langs)} already marked "
            f"complete in {progress_file}.\n"
            f"Re-run with --force to collect more (finished videos are still skipped)."
        )
        logger.info("Nothing to do")
        return

    az = config["azure"]
    notify(
        config,
        "AI-video scrape started",
        f"{len(pending_langs)} language(s): {', '.join(pending_langs)}\n"
        f"{config['av_processing']['clips_per_video']} clips/video at "
        f"{config['video']['max_height']}p\n"
        f"-> {az['container']}/{az['prefix']}",
        tags=["arrow_forward"],
    )

    t_start = time.time()
    results = []
    with ProcessPoolExecutor(max_workers=parallel_langs) as executor:
        futures = {
            executor.submit(run_language, lang, config, channels_config[lang]): lang
            for lang in pending_langs
        }

        for future in as_completed(futures):
            lang = futures[future]
            try:
                result = future.result()
                results.append(result)
                completed_langs.add(lang)
                logger.info(f"Completed {lang}: {result}")
                notify_done(
                    config,
                    f"fake/{lang} complete",
                    f"{result['total_clips']} clip pairs, "
                    f"{result['total_hours']:.2f} hours\n"
                    f"{result.get('rate_limited_drops', 0)} dropped to rate limiting",
                )
            except Exception as e:
                logger.error(f"Pipeline failed for {lang}: {e}", exc_info=True)
                results.append({"language": lang, "error": str(e)})
                notify_error(config, f"fake/{lang} FAILED", f"{type(e).__name__}: {e}")

            with open(progress_file, "w") as f:
                json.dump({"completed": sorted(completed_langs),
                           "last_update": datetime.utcnow().isoformat()}, f)

    logger.info("=" * 60)
    logger.info("FINAL SUMMARY:")
    total_clips = 0
    total_hours = 0.0
    for r in results:
        if "error" not in r:
            total_clips += r.get("total_clips", 0)
            total_hours += r.get("total_hours", 0.0)
        logger.info(f"  {r}")
    logger.info(f"TOTAL: {total_clips} clip pairs, {total_hours:.2f} hours")
    logger.info("=" * 60)

    for r in results:
        bench = r.get("benchmark")
        if not bench:
            continue
        print(f"\n--- {r['language']} throughput ({bench['clip_mode']} mode, "
              f"{bench['fetch_workers']} fetch workers) ---")
        print(f"  {bench['videos']} videos / {bench['clips']} clips in "
              f"{bench['elapsed_min']:.1f} min   ({bench['gb']:.2f} GB)")
        print(f"  rate: {bench['videos_per_hour']:.1f} videos/h, "
              f"{bench['clips_per_hour']:.0f} clips/h, {bench['gb_per_hour']:.1f} GB/h")
        print(f"  gpu busy: {bench['gpu_seconds']:.0f}s of "
              f"{bench['elapsed_min']*60:.0f}s wall")
        pv = bench.get("per_video")
        if pv:
            print(f"  per video (mean, stages overlap across workers): "
                  f"select {pv['select']:.1f}s + fetch {pv['clip_fetch']:.1f}s + "
                  f"demux {pv['demux']:.1f}s = {pv['wall']:.1f}s, "
                  f"{pv['clips']:.1f} clips, {pv['mb']:.1f} MB")

    elapsed = (time.time() - t_start) / 3600
    failed = [r["language"] for r in results if "error" in r]
    send = notify_error if failed else notify_done
    send(
        config,
        "AI-video run finished" if not failed else f"Run finished, {len(failed)} failed",
        f"{total_clips} clip pairs, {total_hours:.2f} hours\n"
        f"elapsed {elapsed:.1f} h"
        + (f"\nfailed: {', '.join(failed)}" if failed else ""),
    )


if __name__ == "__main__":
    main()
