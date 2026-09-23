# PMData Synthetic Dataset — Data Dictionary

Study window: **2026-01-08 → 2026-02-04** (28 days)
Participants: **50** (P001–P050)
Devices: Fitbit wristband (per-minute step resolution, nightly sleep staging)

---

## data/participants/participants.csv

One row per participant enrolled in the study.

| Column | Type | Description |
|---|---|---|
| `participant_id` | string | Unique participant identifier (`P001`–`P050`) |
| `device_id` | string | Fitbit device serial number (`FBT-NNNN-XXXXXXXX`) |
| `age` | integer | Age in years at study enrolment |
| `height_cm` | integer | Height in centimetres |
| `gender` | string | `male` or `female` |
| `chronotype` | string | Sleep chronotype: `A` = morning type, `B` = evening type |
| `max_heart_rate` | integer | Estimated maximum heart rate (bpm); derived as 220 − age + individual offset |

---

## data/health_summaries/device_metadata.csv

One row per device, describing calibration context.

| Column | Type | Description |
|---|---|---|
| `participant_id` | string | Foreign key → participants.csv |
| `device_id` | string | Foreign key → participants.csv; matches device serial |
| `wear_site` | string | Wrist placement: `wrist_dominant` or `wrist_nondominant` |
| `calibration_date` | date (ISO 8601) | Date the device was calibrated before deployment |
| `firmware_version` | string | Firmware version string installed at calibration (e.g. `2.2.1`) |
| `device_label` | string | Free-text device description, e.g. `Fitbit Charge 5 · fw2.2.1 · SN:FBT-0001-9D79801C`. Always carries the true firmware version — including for rows where `firmware_version` is blank — but it has to be regex-parsed out. |

---

## data/health_summaries/wellness_survey.csv

Daily self-reported wellness scores collected each morning via in-app survey.

| Column | Type | Description |
|---|---|---|
| `participant_id` | string | Foreign key → participants.csv |
| `date` | date (ISO 8601) | Date survey was submitted |
| `fatigue_score` | integer | Perceived fatigue level (1 = exhausted, 5 = fully rested) |
| `stress_score` | integer | Perceived stress level (1 = highly stressed, 5 = no stress) |
| `readiness_score` | integer | Readiness to train (0 = not ready, 10 = peak readiness) |
| `sleep_quality_score` | integer | Subjective sleep quality (1 = very poor, 5 = excellent) |

---

## data/wearable_events/{participant_id}/sleep.json

Array of nightly sleep session objects. One file per participant; 28 entries per file
(one per study night).

```json
[
  {
    "participant_id": "P001",
    "date": "2026-01-08",
    "sleep_onset":  "2026-01-07T23:04:11.000",
    "sleep_end":    "2026-01-08T06:51:11.000",
    "efficiency_pct": 88.4,
    "restlessness": 0.213,
    "stages": [
      { "stage": "light", "start_time": "...", "end_time": "...", "duration_min": 42 },
      { "stage": "deep",  "start_time": "...", "end_time": "...", "duration_min": 81 },
      { "stage": "rem",   "start_time": "...", "end_time": "...", "duration_min": 53 },
      { "stage": "light", "start_time": "...", "end_time": "...", "duration_min": 90 },
      { "stage": "awake", "start_time": "...", "end_time": "...", "duration_min": 19 }
    ]
  }
]
```

| Field | Type | Description |
|---|---|---|
| `participant_id` | string | Matches participants.csv |
| `date` | date string | Wake-up date (session spans the night before `date`) |
| `sleep_onset` | ISO 8601 + `.000` | Timestamp when the participant fell asleep |
| `sleep_end` | ISO 8601 + `.000` | Timestamp when the participant woke up |
| `efficiency_pct` | float | Session-level sleep efficiency |
| `restlessness` | float | Session-level restlessness index (0–1) |
| `stages[].stage` | string | `light`, `deep`, `rem`, or `awake` |
| `stages[].start_time` | ISO 8601 + `.000` | Stage start |
| `stages[].end_time` | ISO 8601 + `.000` | Stage end (= next stage's start_time) |
| `stages[].duration_min` | integer | Stage duration in minutes (should equal `end_time − start_time` in whole minutes) |

Stage order within every session: `light → deep → rem → light → awake`.

**Derived metrics:** compute `total_sleep_min` (sum of non-awake stage `duration_min`),
`deep_sleep_pct`, and `sleep_efficiency_pct` from the `stages` array — there is no
pre-computed summary CSV.

---

## data/wearable_events/{participant_id}/steps.json

Array of per-minute step-count records. One file per participant;
28 days × 1 440 minutes = **40 320 records** per file (nominal).

```json
[
  { "timestamp": "2026-01-08T00:00:00", "steps": 0, "participant_id": "P001" },
  { "timestamp": "2026-01-08T00:01:00", "steps": 0, "participant_id": "P001" },
  ...
]
```

| Field | Type | Description |
|---|---|---|
| `timestamp` | ISO 8601 (no ms) | Minute-boundary timestamp for this record |
| `steps` | integer | Step count recorded during this minute |
| `participant_id` | string | Matches participants.csv |

Expected step rate: **0–30 steps/minute** at rest through moderate activity;
burst windows may reach higher values. All timestamps should fall on whole-minute
boundaries (seconds = `00`).

---

## data/wearable_events/{participant_id}/heart_rate.json

Array of per-minute heart-rate records, on the same per-minute grid as
`steps.json`. One file per participant; 40 320 records per file (nominal).

```json
[
  { "timestamp": "2026-01-08T00:00:00", "heart_rate_bpm": 59, "participant_id": "P001" },
  { "timestamp": "2026-01-08T00:01:00", "heart_rate_bpm": 58, "participant_id": "P001" },
  ...
]
```

| Field | Type | Description |
|---|---|---|
| `timestamp` | ISO 8601 (no ms) | Minute-boundary timestamp for this record. Almost always naive local time, matching `steps.json` — but see the timestamp-format note below. |
| `heart_rate_bpm` | integer | Heart rate in beats per minute recorded during this minute |
| `participant_id` | string | Matches participants.csv |

Expected range: **40–200 bpm**. Values track activity (correlated with `steps.json`
bursts) and sleep stage (dips during deep/REM sleep, from `sleep.json`).

**Timestamp format note:** the study's fixed local timezone is **UTC-05:00**.
Almost all timestamps in this file are naive local ISO 8601 (no offset, no `Z`),
matching every other file in the dataset — but a device can occasionally emit
timestamps as explicit UTC (`Z`-suffixed) instead. Don't assume a single format
without checking.

**Data quality note:** don't assume every in-range value is trustworthy —
optical heart-rate sensors are prone to signal loss, where a stuck sensor
repeats its last reading for hours.
