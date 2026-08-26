#!/usr/bin/env python3
"""
YouTube Audio Scraper for Deepfake Detection Dataset
Main entry point - orchestrates parallel language pipelines.
Crash-proof: resumes from where it left off on restart.
"""
import sys
import os
import json
import argparse
import signal
import time
from pathlib import Path
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import set_start_method

sys.path.insert(0, str(Path(__file__).parent))

from src.utils import load_config, load_channels, setup_logger
from src.pipeline import run_language

shutdown_requested = False


def signal_handler(signum, frame):
    global shutdown_requested
    shutdown_requested = True
    print(f"\n[SIGNAL {signum}] Graceful shutdown requested. Finishing current videos...")


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


def parse_args():
    parser = argparse.ArgumentParser(description="YouTube Audio Scraper")
    parser.add_argument(
        "--languages",
        nargs="*",
        default=None,
        help="Specific languages to process (default: all in channels.yaml)",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="Override number of parallel workers",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Run smoke test: 2 videos per channel, 3 languages only",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Enumerate videos only, no download/processing",
    )
    return parser.parse_args()


def run_smoke_test(config, channels_config, languages=None):
    """Smoke test: 1 video per channel, specified or all languages, in parallel."""
    logger = setup_logger("smoke_test", config)
    logger.info("=" * 60)
    logger.info("SMOKE TEST MODE - all channels, 1 video each")
    logger.info("=" * 60)

    if languages:
        channels_config = {k: v for k, v in channels_config.items() if k in languages}

    config["processing"]["max_videos_per_channel"] = 1
    config["processing"]["max_segments_per_video"] = 1
    config["processing"]["batch_upload_size"] = 1

    total_langs = len(channels_config)
    total_channels = sum(len(v) for v in channels_config.values())
    logger.info(f"Testing {total_channels} channels across {total_langs} languages")

    # 2 parallel languages to avoid rate limiting
    parallel_langs = min(2, total_langs)
    logger.info(f"Running {parallel_langs} languages in parallel")

    results = []
    with ProcessPoolExecutor(max_workers=parallel_langs) as executor:
        futures = {}
        for lang in sorted(channels_config.keys()):
            channels = channels_config[lang]
            future = executor.submit(run_language, lang, config, channels)
            futures[future] = lang

        for future in as_completed(futures):
            lang = futures[future]
            try:
                result = future.result()
                results.append(result)
                logger.info(f"  -> {lang}: {result.get('total_segments', 0)} segments, {result.get('total_hours', 0):.3f}h")
            except Exception as e:
                logger.error(f"Smoke test failed for {lang}: {e}", exc_info=True)
                results.append({"language": lang, "total_segments": 0, "total_hours": 0, "error": str(e)})

    logger.info("=" * 60)
    logger.info("SMOKE TEST RESULTS:")
    total_segs = 0
    total_hrs = 0
    failed_langs = []
    for r in sorted(results, key=lambda x: x.get("language", "")):
        segs = r.get("total_segments", 0)
        hrs = r.get("total_hours", 0)
        total_segs += segs
        total_hrs += hrs
        status = f"{segs} segs, {hrs:.3f}h"
        if "error" in r:
            status += f" [ERROR: {r['error'][:50]}]"
            failed_langs.append(r["language"])
        logger.info(f"  {r['language']}: {status}")
    logger.info(f"TOTAL: {total_segs} segments, {total_hrs:.2f} hours")
    if failed_langs:
        logger.info(f"FAILED: {failed_langs}")
    logger.info("=" * 60)

    return results


def main():
    set_start_method("spawn", force=True)
    args = parse_args()
    config = load_config()
    channels_config = load_channels()
    logger = setup_logger("main", config)

    logger.info(f"YouTube Scraper starting at {datetime.utcnow().isoformat()}")
    logger.info(f"Available languages: {list(channels_config.keys())}")

    if args.smoke_test:
        results = run_smoke_test(config, channels_config, languages=args.languages)
        print("\nSmoke test complete. Results:")
        for r in results:
            print(f"  {r}")
        return

    # Determine languages to process
    if args.languages:
        languages = [l for l in args.languages if l in channels_config]
        missing = set(args.languages) - set(languages)
        if missing:
            logger.warning(f"Languages not in config: {missing}")
    else:
        languages = list(channels_config.keys())

    logger.info(f"Processing languages: {languages}")

    # Determine parallelism
    # We allocate workers across languages, but each language pipeline
    # is sequential (download -> segment -> transcribe -> upload) because
    # GPU transcription is the bottleneck and must be serialized per-GPU.
    # Multiple language pipelines run in parallel on CPU-bound work.
    max_workers = args.max_workers or min(
        config["processing"]["num_workers"],
        len(languages),
    )
    # Reserve cores: don't exceed available - 3
    max_workers = min(max_workers, 24)

    # Since we have 1 GPU, we process languages sequentially for transcription
    # but parallelize download/segmentation across multiple languages
    # 3 parallel language pipelines (GPU memory constraint)
    parallel_langs = min(3, max_workers, len(languages))

    logger.info(f"Running {parallel_langs} language pipelines in parallel")

    # Track completed languages for resume
    progress_file = Path(config["local_staging"]["dir"]) / "run_progress.json"
    completed_langs = set()
    if progress_file.exists():
        try:
            with open(progress_file) as f:
                progress = json.load(f)
            completed_langs = set(progress.get("completed", []))
            if completed_langs:
                logger.info(f"Resuming: {len(completed_langs)} languages already completed: {sorted(completed_langs)}")
        except Exception:
            pass

    pending_langs = [l for l in languages if l not in completed_langs]
    logger.info(f"Languages to process: {len(pending_langs)} (skipping {len(completed_langs)} already done)")

    results = []
    with ProcessPoolExecutor(max_workers=parallel_langs) as executor:
        futures = {}
        for lang in pending_langs:
            channels = channels_config[lang]
            future = executor.submit(run_language, lang, config, channels)
            futures[future] = lang

        for future in as_completed(futures):
            lang = futures[future]
            try:
                result = future.result()
                results.append(result)
                completed_langs.add(lang)
                logger.info(f"Completed {lang}: {result}")
            except Exception as e:
                logger.error(f"Pipeline failed for {lang}: {e}", exc_info=True)
                results.append({"language": lang, "error": str(e)})

            # Save progress after each language completes
            with open(progress_file, "w") as f:
                json.dump({"completed": sorted(completed_langs), "last_update": datetime.utcnow().isoformat()}, f)

    # Summary
    logger.info("=" * 60)
    logger.info("FINAL SUMMARY:")
    total_hours = 0
    total_segments = 0
    for r in results:
        if "error" not in r:
            total_hours += r.get("total_hours", 0)
            total_segments += r.get("total_segments", 0)
        logger.info(f"  {r}")
    logger.info(f"TOTAL: {total_segments} segments, {total_hours:.2f} hours")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
