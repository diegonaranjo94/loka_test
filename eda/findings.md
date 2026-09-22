# Raw data findings

Profiling of `s3://de-tech-assessment-396587179375-us-east-1-an/data/` as landed in `poc/data/raw/`
(387 MB, 154 objects: 3 CSV + 150 JSON + `data_dictionary.md`).

Produced by `eda/01_explore_raw.ipynb`; machine-readable register in
`eda/outputs/raw_findings.csv` (21 errors / 10 warnings / 11 passing checks) and per-participant
coverage in `eda/outputs/coverage_matrix.csv`.

Volumes profiled: 50 participants × 28 days (2026-01-08 → 2026-02-04) — steps 2 014 950 rows,
heart_rate 2 014 950 rows, sleep 1 400 sessions / 7 000 stages, wellness survey 1 414 rows.

## Assumptions
- An average human being is not able to walk more than 300 steps per minute https://www.runnersworld.com/es/training/a60494879/esta-es-la-cantidad-exacta-de-pasos-por-minuto-que-potencia-la-eficacia-de-caminar-para-estar-en-forma-pasados-los-50/
- Firmware version on device_metadata.csv file came from device_label field
- The study's fixed local timezone is UTC-05:00; every naive timestamp is local time, and any `Z`-suffixed timestamp is UTC and must be shifted before it can be compared with the rest
- The nominal grid is 1 440 minutes/day × 28 days = 40 320 records per participant per event file, and 28 sleep sessions per participant
- Stage objects tile the session: each stage's `end_time` is the next stage's `start_time`
- Plausible resting-to-active heart rate is 40–200 bpm; a reading that never changes for hours is sensor failure, not physiology
- Nothing in the dictionary is taken on trust — every claim it makes (firmware in the label, mixed timestamp formats, stuck sensors) was verified against the data before being treated as fact

## Participants (`participants/participants.csv`)
- 50 rows, `participant_id` unique, all matching `P###` — passes
- `device_id` unique per participant and matching `FBT-NNNN-XXXXXXXX` — 1:1, so it can feed `gold_participant_devices` directly
- `age`, `height_cm`, `gender` (male/female), `chronotype` (A/B) all inside their documented domains, no nulls
- `max_heart_rate` sits more than 15 bpm away from `220 − age` for 12 participants (offsets from −27 to +37). The dictionary does say "+ individual offset", so this is flagged, not corrected — worth confirming with the study team before using MHR to normalise heart rate

## Device metadata (`health_summaries/device_metadata.csv`)
- 50 rows, one per device, `device_id` unique, joins 1:1 to participants with no `device_id` disagreement between the two files
- `firmware_version` is blank on 15/50 rows (30%): P006, P008, P015, P016, P021, P022, P027, P031, P032, P039, P040, P041, P045, P048, P050
  - `device_label` yields the firmware for all 15 via regex (`fw(\d+\.\d+\.\d+)`), and never disagrees with the 35 populated rows → parse the label as the authoritative source, keep the raw column for lineage
  - the serial embedded in `device_label` also matches `device_id` in every row, so the label can be used as a cross-check
- `wear_site` only ever `wrist_dominant` (30) / `wrist_nondominant` (20) — clean
- `calibration_date` parses for all 50 and all fall before the study start (2025-12-04 → 2026-01-07) — clean

## Wellness survey (`health_summaries/wellness_survey.csv`)
- 1 414 rows; `(participant_id, date)` is unique; every date falls inside the study window; no nulls
- All scores inside their documented domains (`fatigue`/`stress`/`sleep_quality` 1–5, `readiness` 0–10)
- **14 rows belong to `P999`, a participant that does not exist in `participants.csv`** → orphan foreign key; reject to a quarantine table rather than letting it into a `LEFT JOIN` and materialise as a null-dimension row
- Excluding P999, all 50 real participants have the full 28 days — the survey is the cleanest source in the study

## Steps (`wearable_events/{pid}/steps.json`)
- There shouldn't be negative values on steps
  - 2 rows at `-1`: P029 2026-02-04T00:30, P037 2026-01-16T00:42
- The range of steps per minute is between 0 - 300
- More than 300 steps per minute should be avoided
  - 2 sentinel spikes of `1000000`: P029 2026-02-04T05:17, P037 2026-02-03T22:05 → same two participants as the negatives, and the same two that carry the impossible heart-rate values. The corruption is device-scoped, not random
  - the remaining 45 020 values above 300 are the mislabelled-unit rows below, not real outliers
- There should not be duplicates timestamps
  - 5 767 duplicate `(participant_id, timestamp)` rows, of two very different kinds:
  - **a whole day ingested twice** — 2026-01-10 appears twice for P010, P019, P022, P025 (1 440 minutes duplicated each), and P034 (2026-01-26, 2026-02-02), P040 (2026-01-09), P050 (2026-01-17)
  - **near-duplicate records with drifted clocks** — 28 timestamps whose seconds are not `00` (e.g. `2026-01-10T13:55:22`), each sitting next to the whole-minute record it duplicates, on P005, P010, P034, P040, P050
- Timestamps must fall on whole-minute boundaries (seconds = 00) — see the 28 above → snap to the minute, then de-duplicate; do not de-duplicate first, or the drifted copies survive as distinct keys
- An undocumented `unit` field appears on **120 963 rows** (P005, P033, P038) carrying `"steps_per_hour"`
  - **76 045 of 76 045** non-zero values on those rows are exact multiples of 60 → they are hourly rates written onto the per-minute grid
  - summed naively, those three participants look ~60× more active than everyone else; rescale (`/60`) or resample before any daily aggregate, and keep the unit in the Bronze schema so the rule is data-driven rather than a hard-coded participant list
- Row counts alone do not prove completeness:
  - P019, P022, P025 land exactly 40 320 rows — the nominal number — yet **2026-01-08 is entirely absent** and 2026-01-10 is duplicated, so the two defects cancel out in the row count
  - P010 is also missing 2026-01-08; P005, P034, P040, P050 land 40 326 (the 6 drifted extras)
  - P020 (2026-01-27), P028 (2026-01-15), P041 (2026-01-29) each lose a **contiguous 6-hour block** — 1 080 rows instead of 1 440, 1 080 missing minutes in total
  - → completeness has to be asserted per participant-day against the expected 1 440-minute grid, never by counting rows

## Heart rate (`wearable_events/{pid}/heart_rate.json`)
- Expected range is 40–200 bpm
  - 4 rows outside it: `-10` and `999`, again only on **P029 and P037**
- A reading that repeats unchanged for hours is a stuck optical sensor, not a heart rate
  - run-length encoding of consecutive identical values finds 2 runs of **180 minutes**: P003 on 2026-01-09 04:44→07:43 at 60 bpm, and P042 on 2026-01-24 09:16→12:15 at 73 bpm
  - both sit comfortably inside 40–200 bpm, so a plain range check never sees them — this is the check that has to be written deliberately
  - 30 consecutive identical minutes is the threshold used; it is a tunable, and worth revisiting against night-time data where variability is genuinely lower
- Timestamps must be a single format
  - **2 880 rows are `Z`-suffixed UTC** — 1 440 for P018 and 1 440 for P044 (one full day each) — while every other row, and every other file in the study, is naive local time
  - parsed naively those two participants are shifted 5 hours against their own step and sleep data, which silently corrupts any chronotype or activity-window analysis
  - → parse offset-aware, convert to the study timezone, and de-duplicate **after** normalisation (naive and `Z` copies of the same minute collide only once they are on the same clock)
- The same duplication pattern as steps, row for row: 5 767 duplicate keys, 589 fully duplicated rows, the same 28 off-minute timestamps, the same participants and the same 6-hour gaps
  - → steps and heart rate come out of one export process; the quality rules belong in one shared per-minute-event contract, not written twice

## Sleep (`wearable_events/{pid}/sleep.json`)
- Sleep session range should be between 2 - 15 hous
  - 3 sessions fall outside it (168 min to 1 328 min)
- 1 400 sessions, 28 per participant, `(participant_id, date)` unique, stage labels always in the canonical `light → deep → rem → light → awake` order, stages always contiguous — structurally the JSON is sound
- Verified anchors, since the derived metrics depend on them:
  - `sleep_onset` equals the first stage's `start_time` in 100% of sessions
  - `sleep_end` equals the end of the **last non-awake stage** in 99.8% — the trailing `awake` stage runs *past* `sleep_end`
  - → **time in bed = first stage start → last stage end**, not `sleep_onset → sleep_end`. Getting this wrong shifts every efficiency figure by roughly 12 percentage points
  - the 3 exceptions are P026 2026-01-23, P035 2026-01-20, P039 2026-01-14 — the same 3 sessions as the implausible spans above, so `sleep_end` is the corrupted field there
- `efficiency_pct` is not usable
  - it reads exactly `100.0` on **1 132 of 1 400 sessions (81%)** and its correlation with the stage-derived value is **0.15**
  - → ignore the pre-computed column and derive `total_sleep_min`, `deep_sleep_pct` and `sleep_efficiency_pct` from `stages[]`, exactly as the dictionary instructs
- `duration_min` disagrees with `end_time − start_time` on 4 stages — every one of them a **`deep`** stage, every one inflated: P007 2026-01-21 (372 vs 194), P010 2026-01-20 (307 vs 144), P024 2026-01-11 (394 vs 200), P035 2026-01-17 (302 vs 150)
  - these 4 also break the tiling of their session
  - → trust the timestamps over `duration_min`; an inflated deep-sleep figure lands directly on the chronotype cohort comparison, which is the analytical deliverable
- `restlessness` is inside 0–1 everywhere; no nulls anywhere in the source

## Cross-cutting
- Corruption clusters by participant, which points at the export/device rather than at random noise: sentinels on **P029, P037**; timezone drift on **P018, P044**; unit mislabelling on **P005, P033, P038**; stuck sensor on **P003, P042**; duplicated/missing days on **P010, P019, P022, P025** and **P020, P028, P041**
- Only 8 of the 50 participants deviate from the nominal per-minute row count, but 12 deviate once completeness is measured on the day grid — the row count is the misleading metric
- `participant_id` inside every JSON payload matches the folder it sits in — the directory layout can be trusted as a partition key
- All 50 participants have all 3 event files; no file is missing or empty

## Implications for the pipeline
- Bronze keeps the raw record verbatim plus lineage (source file, ingest timestamp) — the sentinels, the duplicates and the `unit` field must survive the landing so the fixes stay auditable
- Silver enforces one rule per defect above, each with a test: snap timestamps to the minute → normalise timezone → de-duplicate on `(participant_id, timestamp)` → rescale by `unit` → null out sentinels → flag stuck-sensor windows rather than deleting them (the minutes are real, the values are not)
- Reject rather than repair where intent is unknowable: orphan `P999`, the 3 corrupted `sleep_end` sessions, the 4 inflated `deep` stages
- Completeness is asserted on the participant-day grid (1 440 minutes, 1 survey, 1 sleep session), not on row counts
- Idempotency is not optional here: a re-ingested day already exists in the source data, so the pipeline has to be able to absorb the same file twice without changing its output

## Open questions for the study team
- Is the `max_heart_rate` offset intentional per participant, or a data-entry issue?
- Should the 6-hour gaps (P020, P028, P041) be treated as device-off (exclude the day from daily aggregates) or as zero activity?
- Is `P999` a test record or a participant missing from the roster?
- What is the expected behaviour for a day that arrives twice — last-write-wins, or first-seen?
