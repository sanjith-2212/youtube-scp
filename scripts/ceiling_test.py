#!/usr/bin/env python3
"""Measure how many videos an IP can extract before YouTube's bot check fires,
with and without an authenticated session.

Design notes
------------
The three production hosts share a /24 and YouTube throttles them as one
client, so a parallel A/B on that subnet is worthless -- one arm's throttle
lands on the other. This runs both arms on a single *fresh* IP instead,
sequentially, using the throttle itself as the transition:

  Arm A   no cookies, full speed, until the bot check fires   -> N_a
  RESCUE  immediately retry that same video WITH cookies
            success => auth overrides an already-throttled IP
            failure => the IP is the binding constraint, cookies are useless
  Arm B   continue with cookies until the bot check fires again -> N_b

Same IP, same minute, same video across the rescue boundary, so nothing varies
but the cookies. No cooldown wait is needed between arms because the rescue
*is* the boundary.

We only extract metadata (--simulate). The bot check is an extraction-phase
error in every sample we have, so this reproduces it without moving ~10 MB per
video. Caveat: a separate ceiling on downloaded *bytes* would not show up here.
"""

import argparse
import json
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DENO = "/home/ubuntu/.deno/bin/deno"
YTDLP = str(ROOT / "venv" / "bin" / "yt-dlp")

# Substrings meaning "throttled", as opposed to "this video is unavailable".
# Kept apostrophe-free: yt-dlp emits a curly one.
BOT_MARKERS = ("not a bot", "http error 429", "too many requests")

# Reasons a single video legitimately fails. These must not be mistaken for a
# throttle or the run ends early on an unrelated fault.
BENIGN_MARKERS = (
    "video unavailable", "private video", "members-only",
    "removed by the uploader", "age-restricted", "not available in your country",
    "this live event", "premieres in",
)


def load_pool(limit, seed):
    """Video IDs from the enumeration checkpoints, shuffled and deduped."""
    ids, seen = [], set()
    for f in sorted((ROOT / "output").glob("*/enumerated_videos.json")):
        try:
            for v in json.loads(f.read_text()):
                vid = v.get("video_id")
                if vid and vid not in seen:
                    seen.add(vid)
                    ids.append(vid)
        except Exception:
            continue
    random.Random(seed).shuffle(ids)
    return ids[:limit]


def classify(returncode, stderr):
    """-> 'ok' | 'botcheck' | 'benign' | 'error'"""
    if returncode == 0:
        return "ok"
    low = stderr.lower()
    if any(m in low for m in BOT_MARKERS):
        return "botcheck"
    if any(m in low for m in BENIGN_MARKERS):
        return "benign"
    return "error"


def attempt(video_id, proxy, cookies, timeout):
    cmd = [
        YTDLP, "--no-playlist", "--js-runtimes", f"deno:{DENO}",
        "--extractor-args", "youtube:player_client=web_embedded",
        "-f", "bestaudio", "--simulate", "--print", "%(id)s",
        "--socket-timeout", "20", "--retries", "0", "--no-warnings",
    ]
    if proxy:
        cmd += ["--proxy", proxy]
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(f"https://www.youtube.com/watch?v={video_id}")

    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return classify(r.returncode, r.stderr or ""), round(time.time() - t0, 2), (r.stderr or "").strip()[-300:]
    except subprocess.TimeoutExpired:
        return "error", round(time.time() - t0, 2), "timeout"


def run_arm(name, pool, start, proxy, cookies, log, delay, timeout, max_n):
    """Consume the pool until the bot check fires. Returns (verdict, index, counts)."""
    counts = {"ok": 0, "benign": 0, "error": 0}
    i = start
    while i < len(pool) and sum(counts.values()) < max_n:
        vid = pool[i]
        i += 1
        outcome, secs, err = attempt(vid, proxy, cookies, timeout)
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(), "arm": name,
            "video_id": vid, "outcome": outcome, "secs": secs,
            "n_before": sum(counts.values()),
        }
        if outcome != "ok":
            rec["stderr"] = err
        log.write(json.dumps(rec) + "\n")
        log.flush()

        if outcome == "botcheck":
            print(f"\n  [{name}] BOT CHECK after {sum(counts.values())} attempts "
                  f"({counts['ok']} ok) on {vid}")
            return "botcheck", i - 1, counts

        counts[outcome] += 1
        n = sum(counts.values())
        if n % 10 == 0:
            print(f"  [{name}] {n} attempts, {counts['ok']} ok, "
                  f"{counts['benign']} unavailable, {counts['error']} error", flush=True)
        if delay:
            time.sleep(delay)

    return "exhausted", i, counts


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--proxy", default="socks5h://127.0.0.1:1080")
    p.add_argument("--cookies", default=str(ROOT / "cookies" / "account_1.txt"))
    p.add_argument("--out", default=str(ROOT / "logs" / "ceiling_test.jsonl"))
    p.add_argument("--pool-size", type=int, default=3000)
    p.add_argument("--max-per-arm", type=int, default=800)
    p.add_argument("--delay", type=float, default=0.0)
    p.add_argument("--timeout", type=int, default=90)
    p.add_argument("--seed", type=int, default=42)
    a = p.parse_args()

    pool = load_pool(a.pool_size, a.seed)
    if len(pool) < 200:
        sys.exit(f"pool too small ({len(pool)}) -- need enumerated_videos.json under output/")

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    print(f"pool={len(pool)} proxy={a.proxy} cookies={Path(a.cookies).name}")
    print(f"log -> {a.out}\n")

    results = {}
    t_start = time.time()
    with open(a.out, "a") as log:
        # --- Arm A: anonymous -------------------------------------------------
        print("ARM A: no cookies, full speed")
        verdict_a, idx, counts_a = run_arm(
            "A_nocookies", pool, 0, a.proxy, None, log, a.delay, a.timeout, a.max_per_arm)
        results["arm_a"] = {"verdict": verdict_a, "attempts": sum(counts_a.values()), **counts_a}

        if verdict_a != "botcheck":
            print(f"\nArm A never tripped the bot check in {sum(counts_a.values())} attempts.")
            print("The fresh IP tolerates at least this much anonymously; "
                  "raise --max-per-arm to find the ceiling.")
            results["rescue"] = None
        else:
            # --- Rescue: same video, same second, cookies only difference -----
            blocked_video = pool[idx]
            print(f"\nRESCUE: retrying {blocked_video} WITH cookies")
            outcome, secs, err = attempt(blocked_video, a.proxy, a.cookies, a.timeout)
            log.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(), "arm": "RESCUE",
                "video_id": blocked_video, "outcome": outcome, "secs": secs,
                "stderr": err if outcome != "ok" else None,
            }) + "\n")
            log.flush()
            print(f"  -> {outcome}" + (f" ({err[:120]})" if outcome != "ok" else ""))

            # A second, different video rules out a fault specific to the first.
            outcome2, _, err2 = attempt(pool[idx + 1], a.proxy, a.cookies, a.timeout)
            print(f"  -> control video: {outcome2}")
            log.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(), "arm": "RESCUE_CTRL",
                "video_id": pool[idx + 1], "outcome": outcome2,
                "stderr": err2 if outcome2 != "ok" else None,
            }) + "\n")
            log.flush()

            rescued = outcome == "ok" or outcome2 == "ok"
            results["rescue"] = {"same_video": outcome, "other_video": outcome2, "rescued": rescued}

            # --- Arm B: authenticated, only if the rescue worked --------------
            if rescued:
                print("\nARM B: cookies, full speed (auth overrode the throttle)")
                verdict_b, _, counts_b = run_arm(
                    "B_cookies", pool, idx + 2, a.proxy, a.cookies, log,
                    a.delay, a.timeout, a.max_per_arm)
                results["arm_b"] = {"verdict": verdict_b, "attempts": sum(counts_b.values()), **counts_b}
            else:
                print("\nCookies did NOT clear the throttle -- the IP is the binding "
                      "constraint. Skipping arm B.")
                results["arm_b"] = None

    results["elapsed_min"] = round((time.time() - t_start) / 60, 1)
    print("\n" + "=" * 62)
    print(json.dumps(results, indent=2))
    Path(a.out).with_suffix(".summary.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
