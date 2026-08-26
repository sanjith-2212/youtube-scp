# Real speech data catalog

**Bucket:** `mlaad-audio-dataset-239861160828-ap-south-1-an` (ap-south-1)

**Prefixes:** `real/` (supervised / eval bonafide) and `pretrain-real/` (non-overlapping pretrain top-up, cap 8000/lang, blocklisted vs `real/` stems).

**Generated from:** S3 object listing + each language `meta.csv`.

## Grand totals

| Corpus | Languages | Wav files | Hours | S3 size |
|---|---:|---:|---:|---:|
| `real/` | 27 | 130,327 | 361.1 | 42.44 GB |
| `pretrain-real/` | 31 | 202,050 | 375.1 | 43.23 GB |
| **Combined** | — | **332,377** | **736.2** | **85.67 GB** |

### Design notes

- `real/` ≈ evaluation / downstream bonafide speech; many langs ~5k clips.
- `pretrain-real/` ≈ larger in-the-wild + crowd mix for SSL pretraining; dominated by **YODAS** (YouTube), then Common Voice 21, FLEURS, VoxPopuli.
- Speaker counts use `reference_speaker` when present; some corpora (e.g. LibriSpeech-style EN) leave it blank → shown as —.
- Machine-readable companion: `artifacts/real_data_catalog.json`.

## `real/`

- **Languages:** 27
- **Wav files:** 130,327
- **Hours:** 361.1 (from `meta.csv` duration when present; else size estimate at 16 kHz mono PCM)
- **S3 size:** 42.44 GB

### Source families (clip counts)

| Source family | Clips |
|---|---:|
| `fleurs` | 46,255 |
| `audiobook_mls_libri` | 27,859 |
| `commonvoice` | 19,516 |
| `sea_spoof_bonafide` | 19,485 |
| `voxpopuli` | 5,029 |
| `mailabs` | 3,122 |
| `arabic_corpora` | 2,750 |
| `indicvoices` | 2,250 |
| `zeroth` | 2,086 |
| `trvoice` | 1,000 |
| `nst` | 975 |

### Per language

| Lang | Wavs | Meta rows | Hours | GB | Speakers | Mean dur (s) | Dur min–max | Sources (clips) |
|---|---:|---:|---:|---:|---:|---:|---|---|
| `en` | 12,519 | 12,519 | 44.99 | 5.183 | — | 12.94 | 1.41–17.27 | audiobook_mls_libri (12,519) |
| `fr` | 5,000 | 5,000 | 20.78 | 2.394 | — | 14.96 | 10.0–20.0 | audiobook_mls_libri (5,000) |
| `ms` | 5,000 | 5,000 | 20.40 | 2.351 | 3,529 | 14.69 | 1.0–30.0 | sea_spoof_bonafide (3,250), fleurs (1,750) |
| `pl` | 5,000 | 5,000 | 15.90 | 1.832 | 3,950 | 11.45 | 1.21–29.52 | fleurs (3,937), audiobook_mls_libri (793), mailabs (270) |
| `nl` | 5,000 | 5,000 | 15.65 | 1.804 | 3,102 | 11.27 | 2.52–23.7 | fleurs (3,453), audiobook_mls_libri (1,547) |
| `zh-cn` | 5,000 | 5,000 | 15.32 | 1.770 | 4,514 | 11.03 | 2.04–28.68 | fleurs (5,000) |
| `ko` | 5,000 | 5,000 | 15.03 | 1.735 | 2,584 | 10.82 | 3.58–28.5 | fleurs (2,914), zeroth (2,086) |
| `uk` | 4,998 | 4,998 | 14.94 | 1.723 | 3,459 | 10.76 | 1.38–29.94 | fleurs (3,879), mailabs (1,119) |
| `ru` | 4,999 | 4,999 | 14.31 | 1.651 | 2,985 | 10.3 | 1.23–27.84 | fleurs (3,266), mailabs (1,733) |
| `bn` | 5,000 | 5,000 | 13.29 | 1.531 | 3,220 | 9.57 | 1.91–30.0 | indicvoices (2,250), commonvoice (1,500), fleurs (1,250) |
| `ta` | 5,000 | 5,000 | 12.50 | 1.441 | 4,869 | 9.0 | 1.78–29.88 | sea_spoof_bonafide (3,250), fleurs (1,750) |
| `ar` | 5,000 | 5,000 | 12.41 | 1.430 | 4,135 | 8.94 | 1.0–30.0 | arabic_corpora (2,750), commonvoice (1,250), fleurs (1,000) |
| `ro` | 5,000 | 5,000 | 11.90 | 1.371 | 1,651 | 8.57 | 1.02–29.7 | commonvoice (2,016), voxpopuli (1,717), fleurs (1,267) |
| `fi` | 5,000 | 5,000 | 11.85 | 1.365 | 2,265 | 8.53 | 1.06–30.0 | commonvoice (2,219), fleurs (2,060), voxpopuli (721) |
| `hu` | 5,000 | 5,000 | 11.68 | 1.346 | 2,660 | 8.41 | 1.16–29.64 | commonvoice (2,258), fleurs (1,408), voxpopuli (1,334) |
| `ja` | 4,965 | 4,965 | 11.19 | 1.286 | 4,818 | 8.11 | 1.84–29.52 | commonvoice (2,927), fleurs (2,038) |
| `id` | 5,000 | 5,000 | 11.17 | 1.287 | 4,369 | 8.04 | 1.0–29.88 | sea_spoof_bonafide (3,250), fleurs (1,750) |
| `th` | 5,000 | 5,000 | 11.10 | 1.279 | 4,832 | 7.99 | 1.2–28.98 | sea_spoof_bonafide (3,250), fleurs (1,750) |
| `hi` | 4,986 | 4,986 | 10.99 | 1.266 | 4,851 | 7.93 | 1.22–29.25 | sea_spoof_bonafide (3,235), fleurs (1,751) |
| `cs` | 5,000 | 5,000 | 10.88 | 1.254 | 2,043 | 7.83 | 1.02–28.92 | commonvoice (2,297), fleurs (1,446), voxpopuli (1,257) |
| `tr` | 5,000 | 5,000 | 10.13 | 1.167 | 3,099 | 7.29 | 1.15–26.82 | commonvoice (2,500), fleurs (1,500), trvoice (1,000) |
| `vi` | 5,000 | 5,000 | 9.31 | 1.073 | 3,593 | 6.7 | 1.31–29.94 | sea_spoof_bonafide (3,250), fleurs (1,750) |
| `pt` | 2,000 | 2,000 | 8.61 | 0.992 | — | 15.5 | 10.01–20.0 | audiobook_mls_libri (2,000) |
| `es` | 2,000 | 2,000 | 8.48 | 0.977 | — | 15.26 | 10.0–20.0 | audiobook_mls_libri (2,000) |
| `de` | 2,000 | 2,000 | 8.30 | 0.956 | — | 14.94 | 10.0–20.0 | audiobook_mls_libri (2,000) |
| `it` | 2,000 | 2,000 | 8.28 | 0.954 | — | 14.91 | 10.0–19.99 | audiobook_mls_libri (2,000) |
| `sv` | 4,860 | 4,860 | 1.73 | 1.026 | 246 | 5.22 | 1.25–25.5 | commonvoice (2,549), fleurs (1,336), nst (975) |

## `pretrain-real/`

- **Languages:** 31
- **Wav files:** 202,050
- **Hours:** 375.1 (from `meta.csv` duration when present; else size estimate at 16 kHz mono PCM)
- **S3 size:** 43.23 GB

### Source families (clip counts)

| Source family | Clips |
|---|---:|
| `yodas` | 83,741 |
| `commonvoice` | 58,732 |
| `fleurs` | 42,365 |
| `voxpopuli` | 15,687 |
| `english_dialects` | 1,408 |
| `peoples_speech` | 1 |

### Per language

| Lang | Wavs | Meta rows | Hours | GB | Speakers | Mean dur (s) | Dur min–max | Sources (clips) |
|---|---:|---:|---:|---:|---:|---:|---|---|
| `it` | 8,000 | 8,000 | 19.19 | 2.211 | 4,220 | 8.64 | 1.0–30.0 | voxpopuli (2,000), fleurs (2,000), yodas (2,000), commonvoice (2,000) |
| `es` | 8,000 | 8,000 | 18.18 | 2.095 | 4,220 | 8.18 | 1.0–29.74 | fleurs (2,000), commonvoice (2,000), voxpopuli (2,000), yodas (2,000) |
| `pt` | 8,000 | 8,000 | 17.85 | 2.056 | 6,076 | 8.03 | 1.0–29.94 | commonvoice (3,077), fleurs (3,061), yodas (1,862) |
| `fr` | 8,000 | 8,000 | 17.64 | 2.032 | 4,388 | 7.94 | 1.0–29.52 | voxpopuli (2,074), fleurs (2,057), commonvoice (2,052), yodas (1,817) |
| `de` | 8,000 | 8,000 | 16.57 | 1.909 | 4,543 | 7.45 | 1.0–29.8 | voxpopuli (2,145), fleurs (2,141), commonvoice (2,121), yodas (1,593) |
| `sk` | 7,380 | 7,380 | 15.86 | 1.827 | 2,865 | 7.73 | 1.0–30.0 | fleurs (2,400), yodas (2,115), commonvoice (2,107), voxpopuli (758) |
| `pl` | 8,000 | 8,000 | 14.84 | 1.710 | 3,276 | 6.68 | 1.0–29.5 | commonvoice (3,155), voxpopuli (3,000), yodas (1,845) |
| `el` | 7,527 | 7,527 | 14.77 | 1.701 | 3,613 | 7.06 | 1.0–29.88 | fleurs (3,166), commonvoice (2,445), yodas (1,916) |
| `da` | 7,412 | 7,412 | 14.41 | 1.660 | 3,461 | 7.0 | 1.0–27.18 | fleurs (3,151), yodas (2,327), commonvoice (1,934) |
| `sw` | 3,770 | 3,770 | 13.93 | 1.604 | 3,265 | 13.3 | 1.0–30.0 | fleurs (3,166), yodas (604) |
| `en` | 6,396 | 6,396 | 13.07 | 1.506 | 2,396 | 7.36 | 1.0–29.62 | yodas (1,745), voxpopuli (1,414), fleurs (1,414), english_dialects (1,408), commonvoice (414), peoples_speech (1) |
| `ro` | 5,677 | 5,677 | 12.68 | 1.461 | 2,188 | 8.04 | 1.0–30.0 | yodas (2,499), fleurs (1,636), commonvoice (1,542) |
| `nl` | 7,714 | 7,714 | 12.14 | 1.398 | 2,000 | 5.66 | 1.0–28.48 | commonvoice (3,166), voxpopuli (2,296), yodas (2,146), fleurs (106) |
| `ru` | 7,748 | 7,748 | 11.95 | 1.377 | 2,937 | 5.55 | 1.0–25.56 | commonvoice (4,961), yodas (2,513), fleurs (274) |
| `ja` | 7,189 | 7,189 | 11.53 | 1.329 | 2,979 | 5.77 | 1.0–29.28 | yodas (4,437), commonvoice (1,376), fleurs (1,376) |
| `tr` | 6,304 | 6,304 | 11.33 | 1.305 | 3,363 | 6.47 | 1.0–28.92 | yodas (2,550), commonvoice (1,915), fleurs (1,839) |
| `hi` | 6,236 | 6,236 | 10.99 | 1.267 | 1,098 | 6.35 | 1.0–29.88 | yodas (3,476), commonvoice (2,389), fleurs (371) |
| `ta` | 4,605 | 4,540 | 10.60 | 1.235 | 1,267 | 8.41 | 1.0–30.0 | yodas (3,725), fleurs (815) |
| `id` | 6,735 | 6,735 | 10.42 | 1.201 | 1,084 | 5.57 | 1.0–30.0 | commonvoice (3,811), yodas (2,450), fleurs (474) |
| `bn` | 6,815 | 6,815 | 10.41 | 1.200 | 3,295 | 5.5 | 1.0–30.0 | yodas (4,810), commonvoice (1,045), fleurs (960) |
| `uk` | 6,572 | 6,572 | 10.33 | 1.191 | 2,054 | 5.66 | 1.0–29.96 | yodas (3,906), commonvoice (1,722), fleurs (944) |
| `ar` | 7,251 | 7,251 | 10.26 | 1.182 | 2,241 | 5.09 | 1.0–29.86 | yodas (5,371), commonvoice (1,045), fleurs (835) |
| `th` | 7,533 | 7,533 | 9.92 | 1.143 | 5,041 | 4.74 | 1.0–29.26 | commonvoice (4,679), yodas (2,606), fleurs (248) |
| `zh-cn` | 7,198 | 7,198 | 9.83 | 1.133 | 4,689 | 4.92 | 1.0–25.17 | commonvoice (4,700), yodas (2,288), fleurs (210) |
| `cs` | 6,409 | 6,409 | 9.02 | 1.039 | 1,411 | 5.06 | 1.0–27.15 | yodas (5,082), commonvoice (682), fleurs (645) |
| `ms` | 4,712 | 4,661 | 8.96 | 1.038 | 1,861 | 6.92 | 1.0–29.64 | yodas (2,937), fleurs (1,724) |
| `hu` | 4,995 | 4,995 | 8.48 | 0.977 | 2,497 | 6.11 | 1.0–30.0 | yodas (2,355), commonvoice (1,332), fleurs (1,308) |
| `vi` | 4,929 | 4,929 | 8.04 | 0.926 | 1,512 | 5.87 | 1.0–30.0 | yodas (2,884), fleurs (1,127), commonvoice (918) |
| `sv` | 4,889 | 4,889 | 8.01 | 0.923 | 1,827 | 5.9 | 1.0–29.22 | yodas (2,834), fleurs (1,040), commonvoice (1,015) |
| `ko` | 4,686 | 4,686 | 7.33 | 0.844 | 959 | 5.63 | 1.0–27.06 | yodas (3,135), commonvoice (844), fleurs (707) |
| `fi` | 3,368 | 3,368 | 6.53 | 0.753 | 1,380 | 6.98 | 1.0–29.82 | yodas (1,913), fleurs (1,170), commonvoice (285) |

## Language coverage overlap

- In both: `ar, bn, cs, de, en, es, fi, fr, hi, hu, id, it, ja, ko, ms, nl, pl, pt, ro, ru, sv, ta, th, tr, uk, vi, zh-cn`
- Only in `real/`: `(none)`
- Only in `pretrain-real/`: `da, el, sk, sw`
