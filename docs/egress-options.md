# Getting to 2,000 Hours: Egress Options, Costs, Timelines

Written 2026-08-26. Target: 2,000 hours of real human speech across 30 languages
for deepfake-detection training, stored at `s3://detectifai-data/real/v1/{lang}/`.

## How to read this document

Numbers here fall into three confidence tiers, labelled throughout:

- **[MEASURED]** — from this project's own logs and runs. Trustworthy.
- **[EXTERNAL]** — third-party pricing and free-tier terms, from memory as of
  roughly mid-2025. Directionally right, but **verify current prices before
  buying**. Proxy pricing in particular changes often.
- **[GUESS]** — estimates with no measurement behind them, mine or anyone's.
  The per-account YouTube rate ceilings are all in this tier. They drive most
  of the timelines below, so treat those timelines as scenarios, not forecasts.

An earlier version of this analysis presented [GUESS]-tier account limits in the
same tables as [MEASURED] throughput constants, which made them look better
supported than they are. That is corrected here.

---

## 1. Measured baseline

Everything below is from this project's runs.

| Quantity | Value | Source |
|---|---|---|
| Avg kept segment duration | 5.08 s | 327 segments |
| Segment keep rate | 63% | 466 videos (33% wrong language, 5% download fail) |
| Whisper medium on L4, per segment | 1.43 s | 1.19 s transcribe + 0.24 s `detect_language` |
| Unauthenticated ceiling, current subnet | ~230 videos/hour before bot check | first throttle event |
| Sustained rate with global limiter @2.5/min | 13.8 usable videos/hour | 11:15–13:17 window |
| Wall clock lost to cooldowns at that rate | 74% | same window |

**Infrastructure:** three hosts, all in a single /24 at one small hosting
provider, no IPv6 on any of them.

| Host | Egress |
|---|---|
| local | host-A |
| jarvis-server-1 | host-B |
| jarvis-server-2 | host-C |

YouTube throttles this as **one client**, not three. Evidence: with random proxy
selection, per-IP blocking would leave ~2/3 of downloads working; we observed 0%
success across 300+ consecutive videos, with a sharp time-bucketed cliff
(40 ok / 0 fail at 07:10 → 11 ok / 304 fail at 07:20 → 0 ok / 379 fail at 07:30).

**GPU ceiling** [MEASURED], one L4, 3 parallel language workers:

| Segments/video | Max videos/day | Max hours audio/day |
|---|---|---|
| 5 | 14,257 | 63 h |
| 15 | 7,334 | 98 h |
| 40 | 3,313 | 118 h |

The GPU is not the bottleneck today — egress is, by roughly a factor of 80.

---

## 2. The single highest-leverage free change: segments per video

Independent of any proxy or account decision. When downloads are the scarce
resource, take more audio from each one. We currently keep ~16 seconds of a
video that may be 10+ minutes long.

| Segments/video | Speech needed per video | Audio/day at current egress | Days to 2,000 h |
|---|---|---|---|
| 5 (current) | ~40 s | 1.5 h | 1,363 |
| 15 | ~2 min | 4.4 h | 454 |
| 40 | ~5 min | 11.7 h | 171 |

Cost: zero. Risk: **speaker duplication** — 40 clips from one video is 40 clips
of one voice. For deepfake detection this matters, since the model should learn
"real human speech" broadly rather than memorising a few hundred speakers. 15 is
the reasonable middle: ~2 minutes of speech per video, feasible for nearly all
talk/news/podcast content, ~3x the yield of today.

**This change should be made regardless of which option below you pick.**

---

## 3. Option catalogue

### 3.1 Do nothing — current /24, unauthenticated

| | |
|---|---|
| Cost | $0 |
| Timeline to 2,000 h | 454 days at 15 seg/video [MEASURED base rate] |
| Risk | None |
| Verdict | Not viable for a 2,000-hour target |

The global rate limiter and escalating backoff (already implemented, in
`src/ratelimit.py`) work correctly — throttled videos are requeued rather than
burned — but 2.5 req/min still trips the subnet limit. The sustainable rate is
lower still. This path works only if you have a year.

### 3.2 AWS EC2 proxy fleet

| | |
|---|---|
| Cost | ~$45/month for 6 × t2.micro beyond free tier [EXTERNAL] |
| Timeline | Unknown, plausibly **worse than current** |
| Risk | Wasted spend |
| Verdict | **Do not pursue** |

Three independent problems:

1. **Access.** The `sanjith_access` IAM user is S3-scoped. `ec2:DescribeInstances`,
   `ec2:DescribeRegions`, `ec2:DescribeImages` are all denied; only
   `ec2:DescribeVpcs` succeeds. You cannot launch or manage instances without an
   IAM policy change from an account admin. (An earlier note in this project
   claimed "EC2 API confirmed reachable" — that was generalised from the one
   call that happened to work, and is wrong.)

2. **Free tier does not give a fleet.** 750 instance-hours/month is *aggregated*,
   i.e. one t2.micro running 24/7. Six instances = 4,380 h/month, so ~3,630 h
   billable. Separately, AWS replaced the 12-month free tier in July 2025 with a
   credit model ($100, 6 months) [EXTERNAL]; the oldest bucket in this account
   dates to 2026-01-17, so if the account is that age it is on the new model and
   the credits have likely lapsed. `freetier:GetFreeTierUsage` is also denied, so
   this could not be verified.

3. **AWS IPs are among the worst possible for YouTube.** AS16509 is the most
   heavily abused and most aggressively filtered cloud range. Community reports
   in the yt-dlp tracker consistently show cloud IPs hitting bot checks far
   faster than residential, and often faster than smaller hosting providers.
   The current subnet belongs to a small hosting provider — already datacenter,
   but less notorious. Moving to AWS plausibly makes throughput *worse*.

### 3.3 Oracle Cloud always-free proxies

| | |
|---|---|
| Cost | $0 [EXTERNAL] |
| Timeline | Unknown, likely poor |
| Risk | Instance reclamation; probably blocked |
| Verdict | Low priority; test before relying on it |

Oracle's always-free tier is genuinely generous — 2 AMD micro VMs plus 4 ARM
Ampere OCPUs / 24 GB RAM, no 12-month expiry [EXTERNAL]. It is the only truly
free cloud egress on this list.

The problem is that *because* it is free and generous, it is heavily used for
exactly this purpose, so Oracle's ranges (AS31898) are well known to scrapers
and to the platforms defending against them. Expect blocking comparable to AWS.
Oracle also reclaims idle always-free instances, which is disruptive for a
multi-week job.

Gives at most 2–4 new IPs in 1–2 subnets. Even if unblocked, that roughly
doubles or triples current capacity — 150–230 days to 2,000 h. Not sufficient
alone, and not worth much effort.

### 3.4 Datacenter proxies (Webshare and similar)

| | |
|---|---|
| Cost | ~$3–15/month for 100 IPs [EXTERNAL] |
| Timeline | Highly uncertain — depends entirely on subnet spread |
| Risk | May not work at all for YouTube |
| Verdict | Cheap enough to test; do not assume it works |

Webshare's entry plans are very cheap for 100 datacenter IPs. Two unknowns
decide whether this helps:

- **Subnet spread.** 100 IPs across 80 distinct /24s gives ~80 independent
  throttle buckets. 100 IPs across 5 /24s gives ~5. Providers do not publish
  this, and it varies by plan and by luck of allocation.
- **Whether YouTube blocks the range outright.** Datacenter proxy providers'
  ranges are widely known and frequently blanket-blocked.

If it works and the spread is good, this is by far the cheapest path to the GPU
ceiling. If it doesn't, you've lost $15. The asymmetry favours testing it — but
buy the smallest plan first and measure before scaling.

### 3.5 Static residential proxies (ISP proxies)

| | |
|---|---|
| Cost | ~$2–5 per IP/month [EXTERNAL]; 20 IPs ≈ $40–100/month |
| Timeline | ~20–30 days at 15 seg/video, if the ceiling per IP is decent |
| Risk | Low |
| Verdict | Reasonable middle option |

Real ISP-assigned addresses that you hold persistently. Treated as residential
by YouTube (good) while being stable and unmetered by bandwidth (unlike
rotating). Cheaper than rotating for high-volume work, since you pay per IP per
month rather than per GB.

20 static residential IPs across 20 subnets, at even a conservative 100
videos/hour each, saturates the GPU. Over a ~1 month project that's ~$40–100
total.

### 3.6 Rotating residential proxies

| | |
|---|---|
| Cost | ~$1.75–8.00/GB [EXTERNAL] — see table below |
| Timeline | **17–20 days**, GPU-bound |
| Risk | None operationally |
| Verdict | Most reliable paid option; highest confidence in the timeline |

Millions of real ISP IPs, rotated per request. Effectively removes egress as a
constraint — the job becomes GPU-bound, which is a *measured* ceiling rather
than a guessed one. This is the one option whose timeline does not depend on any
[GUESS]-tier number.

Data volume [MEASURED inputs, arithmetic]: at 15 seg/video and 63% keep rate,
2,000 hours needs ~150,000 videos. At ~5 MB of audio per 10-minute video, that's
roughly **730 GB**. At 40 seg/video it drops to ~56,000 videos ≈ **275 GB**.

| Provider | ~$/GB [EXTERNAL] | 275 GB (40 seg) | 730 GB (15 seg) |
|---|---|---|---|
| IPRoyal | 1.75 | $481 | $1,278 |
| Smartproxy / Decodo | 2.80 | $770 | $2,044 |
| Webshare | 2.99 | $822 | $2,183 |
| Bright Data | 4.00 | $1,100 | $2,920 |
| Oxylabs | 8.00 | $2,196 | $5,840 |

Note the interaction: **rotating residential is the one option where higher
segments-per-video saves real money**, because you pay per GB and fewer videos
means less transfer. If you go this route, 40 seg/video is worth the diversity
tradeoff — it's a ~$800 swing.

### 3.7 Authenticated Google accounts (cookies)

| | |
|---|---|
| Cost | $0–5 (phone verification) |
| Timeline | **25–100 days** [GUESS] — see caveat |
| Risk | Account suspension; ToS violation |
| Verdict | Highest value-per-dollar, but the timeline is unmeasured |

The bot check literally names the fix: `Sign in to confirm you're not a bot`.
Authenticated sessions get a substantially higher ceiling than anonymous ones —
that much is well established in community practice. **How much higher is not
publicly known**, and that is the crux.

Scenarios at 15 seg/video, 6 accounts, 2 per IP across the existing three hosts:

| Per-account/day [GUESS] | Total/day | Audio/day | Days to 2,000 h |
|---|---|---|---|
| 500 | 3,000 | 40 h | 50 |
| 1,000 | 6,000 | 80 h | 25 |
| 2,000+ | GPU-capped | 98 h | 20 |

**Why these are guesses.** The real ceiling depends on account age, watch
history, whether a payment method is on file, and the reputation of the IP the
session is used from. Notably, a brand-new throwaway with no history is treated
*more* suspiciously than an aged account — so fresh accounts may land well below
the 500/day bottom of that range. Nobody outside Google knows the function.

**IP distribution is mandatory, not optional.** Six authenticated sessions all
egressing from one IP is a stronger bot signal than no authentication at all: it
tells YouTube that six "different people" share an address and behave
identically. Spread them:

| IP | Accounts | Videos/day/IP |
|---|---|---|
| host-A | 1, 2 | 1/3 of total |
| host-B | 3, 4 | 1/3 of total |
| host-C | 5, 6 | 1/3 of total |

Two accounts per IP is defensible as household sharing. All six from one is not.
Note also that all three IPs are in the same /24, so YouTube may still correlate
them — this reduces the signal but does not eliminate it.

**Risks, plainly:**
- Google suspends accounts for automated access. Use throwaways, never a
  personal or work account.
- Suspension can cascade if accounts share a creation IP, phone number, or
  recovery address. Vary these.
- Cookies expire in ~30–60 days and must be re-exported.
- A cookies file is a full account credential. Anything on the VM can use it.
- This violates YouTube's Terms of Service. That's a fact to plan around
  (account loss is a real cost), not a reason the technical analysis changes.

---

## 4. Combinations

These stack. The two axes are *egress diversity* and *authentication*, and they
multiply rather than add.

| Combination | Cost | Days to 2,000 h | Confidence |
|---|---|---|---|
| Current /24 only, 5 seg | $0 | 1,363 | High [MEASURED] |
| Current /24 only, 15 seg | $0 | 454 | High [MEASURED] |
| + Oracle free (2 IPs), 15 seg | $0 | 150–450 | Low |
| + 6 accounts (2/IP), 15 seg | ~$5 | 25–100 | Low [GUESS] |
| + Webshare datacenter 100 IPs, 15 seg | ~$15/mo | 20–450 | Very low |
| + Webshare DC + 6 accounts | ~$20/mo | 20–50 | Low |
| + Static residential 20 IPs, 15 seg | ~$60/mo | 20–30 | Medium |
| + Static residential + accounts | ~$65/mo | ~20 (GPU cap) | Medium-high |
| Rotating residential, 40 seg | ~$500–1,100 one-off | 17 | High [MEASURED] |
| Rotating residential + accounts, 40 seg | ~$500–1,100 | 17 (GPU cap) | High |

Note the convergence: **everything above about 3,300 videos/day hits the same
GPU wall at ~118 h/day.** Past that point, spending more on egress buys nothing
without a second GPU. The realistic floor for 2,000 hours on current hardware is
**~17 days**, and several very different-cost paths reach it.

The corollary is that the expensive options are not buying speed, they're buying
*certainty*. Rotating residential costs ~$800 and gets you 17 days with high
confidence. Accounts cost ~$5 and get you somewhere between 20 and 100 days with
low confidence. That's the actual trade.

---

## 5. What people actually do

Based on general practice in speech-dataset collection and the yt-dlp community,
rather than anything measured here [EXTERNAL, treat as informed context]:

- **Cookies are the standard first answer to bot checks.** It is the fix yt-dlp's
  own error message and FAQ recommend. Most people scraping at modest scale stop
  here.
- **Residential proxies are the standard answer at scale.** Anyone running
  sustained six-figure-video pipelines is paying for residential egress. Nobody
  successfully runs that volume from a handful of datacenter IPs.
- **Cloud IPs (AWS/GCP/Azure/Oracle) are widely reported as poor for YouTube.**
  This is consistent enough across reports to treat as settled.
- **PO tokens are mandatory now** — you already have bgutil running on :4416,
  which is correct and current.
- **Rotating user agents matters far less than IP and auth.** Already done.

### The option that isn't on the list: don't scrape it

For a deepfake-detection training corpus of *real human speech*, existing public
datasets may cover a large fraction of the requirement at zero cost and zero
risk:

- **YODAS** (CMU WAVLab, on HuggingFace as `espnet/yodas` / `yodas2`) —
  reportedly ~500,000 hours of YouTube speech across 100+ languages, already
  segmented and transcribed, with a CC-BY licensed subset. This is *the same
  source material this pipeline is collecting*, already collected.
- **VoxPopuli** — ~400k hours unlabelled, 23 European languages, EU Parliament.
- **Common Voice** (Mozilla) — 100+ languages, CC0, crowd-sourced read speech.
- **MLS** (Multilingual LibriSpeech) — 8 languages, audiobooks.
- **FLEURS** — 102 languages, smaller but very broad coverage.

[EXTERNAL — I have not verified these are currently available under these terms;
check before relying on it.]

Caveats worth weighing: read/parliamentary speech has different acoustics from
the conversational YouTube content this pipeline targets, licensing must be
checked per dataset, and there is a real risk of train/test contamination if a
public dataset overlaps whatever you evaluate against. But 2,000 hours is small
relative to YODAS, and an afternoon checking coverage for your 30 languages
could plausibly replace weeks of scraping and hundreds of dollars of proxies.

The scraping pipeline still has value for languages these datasets cover poorly,
and for content types they lack — but it may not need to carry all 2,000 hours.

---

## 6. Recommended sequence

Ordered by cost and by how much each step de-risks the next.

**Step 0 — check the public datasets (1 afternoon, $0).**
Pull the YODAS language manifest and compare against your 30 target languages.
If it covers even half, the scale of everything below drops accordingly. Highest
expected value per hour spent of anything in this document.

**Step 1 — raise `max_segments_per_video` to 15 (10 minutes, $0).**
3x yield from every download. Correct under every scenario. Do this now.

**Step 2 — measure one account's real ceiling (a few hours, $0).**
Create one throwaway account, export cookies, run downloads at a fixed rate
through one IP, and log exactly where the bot check appears. This replaces the
entire [GUESS] tier in section 3.7 with a measured number, and it decides
whether the 6-account plan is a 25-day path or a 100-day one. **Do this before
creating five more accounts or buying anything.**

**Step 3 — branch on what step 2 measures.**

- If one account sustains **≥1,000/day**: build out to 6 accounts, 2 per IP.
  ~$5, ~25 days, done. No proxies needed.
- If it sustains **200–1,000/day**: 6 accounts gets you 25–50 days. Probably
  still the right answer. Consider adding ~$15 of Webshare datacenter IPs and
  measuring whether they help before spending more.
- If it sustains **<200/day** or accounts get flagged quickly: authentication
  isn't the lever here. Go to static residential (~$60/month) or rotating
  residential (~$500–1,100 one-off). At that point buy the certainty.

**Step 4 — only if you need to beat 17 days.**
Add a second GPU. Past ~3,300 videos/day the L4 is the constraint and no amount
of egress spending helps.

---

## 7. Open items in the current codebase

- `max_segments_per_video: 5` in `config/settings.yaml` — raise to 15.
- `global_requests_per_min: 2.5` — still above what the /24 tolerates
  [MEASURED: 2 strikes in a 2-hour window]. Lower to ~1.0 until authentication
  or new egress is in place.
- The rate limiter reads `global_requests_per_min` once at construction, so
  calibration currently requires a restart. Making it re-read per `acquire()`
  would allow live tuning.
- `fa`, `hi`, `ru` have only 1 WAV each from the pre-plugin-fix batch and need
  re-running.
- `src/utils.py:rate_limit_delay()` is now unused — the global limiter replaced
  it.
