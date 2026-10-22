# %% [markdown]
# # Sample every table in the lake
#
# One query per published table — Bronze, Silver, Gold, the Gold analytics
# results, quarantine, and the data-quality log — each returning the first
# `SAMPLE_ROWS` rows, so you can see what every table actually looks like
# without writing any code yourself.
#
# Where the data comes from is the same single switch `explore_lake` uses,
# `LAKE_SOURCE` in `lake_client.py`:
#
# ```python
# LAKE_SOURCE = "local"   # poc/data/lake, the offline mirror — no AWS
# LAKE_SOURCE = "s3"      # the bucket in poc/.env (TARGET_URI_REMOTE)
# ```
#
# **Prereq:** run the pipeline at least once first — `./poc_run_etl.sh`
# (offline) or `./poc_run_etl.sh --online` — or there is nothing to sample.
#
# **How each cell works:** load one table with the pipeline's own reader
# (`etl.io_uri.read_table`, via `lake_client`), then query it with DuckDB.
# The whole lake is ~18 MB, so loading a full table to show ten rows costs
# nothing here; at real scale you'd push the `LIMIT` down into the Parquet
# scan instead of loading first. Swap `LIMIT {SAMPLE_ROWS}` for
# `USING SAMPLE {SAMPLE_ROWS} ROWS` in any cell for random rows instead of
# the first ones.

# %%
from __future__ import annotations

import duckdb
import pandas as pd

from lake_client import describe, list_tables, load_table
from etl import config as C

SAMPLE_ROWS = 10  # rows returned by every query below
ANALYTICS = f"{C.GOLD}/_analytics"  # where run_etl.py publishes the analytics query results

pd.set_option("display.max_columns", 60)
pd.set_option("display.width", 200)

_sampled: set[tuple[str, str]] = set()


def load(layer: str, name: str) -> pd.DataFrame:
    """Load one published table and print its size.

    A thin wrapper over `lake_client.load_table` (same reader, same
    `LAKE_SOURCE`) that also records the table as sampled, so the last cell
    can flag any published table this notebook doesn't cover yet.
    """
    df = load_table(layer, name)
    _sampled.add((layer, name))
    print(f"{layer}/{name}: {len(df):,} rows x {df.shape[1]} columns")
    return df


describe()

# %% [markdown]
# ## Bronze — verbatim + lineage
#
# Exactly what the export contained, plus three lineage columns on every row
# (`_source_uri`, `_ingested_at`, `_run_id`). Nothing is cleaned yet:
# timestamps are still strings and defects are still in the data.

# %% [markdown]
# **`bronze_participants`** — the participant roster as exported, one row per
# participant.

# %%
bronze_participants = load(C.BRONZE, "bronze_participants")
duckdb.sql(f"SELECT * FROM bronze_participants LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# **`bronze_device_metadata`** — one row per device as exported. Firmware
# gaps are still gaps here; Silver backfills them from `device_label`.

# %%
bronze_device_metadata = load(C.BRONZE, "bronze_device_metadata")
duckdb.sql(f"SELECT * FROM bronze_device_metadata LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# **`bronze_steps`** — minute-level step events, one row per event in the
# export. `_unit` is the undocumented unit field carried through verbatim;
# partitioned by `_source_participant_dir`.

# %%
bronze_steps = load(C.BRONZE, "bronze_steps")
duckdb.sql(f"SELECT * FROM bronze_steps LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# **`bronze_heart_rate`** — minute-level heart-rate events, same layout as
# `bronze_steps`.

# %%
bronze_heart_rate = load(C.BRONZE, "bronze_heart_rate")
duckdb.sql(f"SELECT * FROM bronze_heart_rate LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# **`bronze_sleep_sessions`** — the session level of the nested sleep JSON,
# flattened: one row per sleep session. Partitioned by `participant_id`.

# %%
bronze_sleep_sessions = load(C.BRONZE, "bronze_sleep_sessions")
duckdb.sql(f"SELECT * FROM bronze_sleep_sessions LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# **`bronze_sleep_stages`** — the stage list exploded out of each session:
# one row per stage, joinable back to its session on `session_key`.

# %%
bronze_sleep_stages = load(C.BRONZE, "bronze_sleep_stages")
duckdb.sql(f"SELECT * FROM bronze_sleep_stages LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# **`bronze_wellness_survey`** — daily self-reported scores as exported,
# including rows for participant IDs that aren't in the roster (Silver
# quarantines those as `orphan_participant`).

# %%
bronze_wellness_survey = load(C.BRONZE, "bronze_wellness_survey")
duckdb.sql(f"SELECT * FROM bronze_wellness_survey LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# ## Silver — cleaned, typed, flagged
#
# Every correction leaves a flag column behind (`was_rescaled`, `was_utc`,
# `was_sentinel`, `is_stuck`, ...) instead of disappearing silently.

# %% [markdown]
# **`silver_participants`** — the cleaned roster, one row per participant.

# %%
silver_participants = load(C.SILVER, "silver_participants")
duckdb.sql(f"SELECT * FROM silver_participants LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# **`silver_devices`** — one row per device. `firmware_source` says whether
# the firmware came from the metadata column or was recovered from
# `device_label`; `device_model` is parsed out of the label.

# %%
silver_devices = load(C.SILVER, "silver_devices")
duckdb.sql(f"SELECT * FROM silver_devices LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# **`silver_steps_minute`** — one row per participant-minute, deduplicated
# and on study-local time. `unit_raw` keeps the original unit; the `was_*`
# columns flag which rules touched the row. Partitioned by `date`.

# %%
silver_steps_minute = load(C.SILVER, "silver_steps_minute")
duckdb.sql(f"SELECT * FROM silver_steps_minute LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# **`silver_heart_rate_minute`** — the same minute grain for heart rate, with
# `is_stuck` (flat-lined sensor) and `was_out_of_range` alongside the
# timezone/off-minute flags. Partitioned by `date`.

# %%
silver_heart_rate_minute = load(C.SILVER, "silver_heart_rate_minute")
duckdb.sql(f"SELECT * FROM silver_heart_rate_minute LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# **`silver_sleep_sessions`** — one row per valid night. Sleep metrics are
# re-derived from the stages (`sleep_efficiency_pct`, `total_sleep_min`, ...)
# with the device's own figure kept as `efficiency_pct_stated` for
# comparison; `*_hour_signed` puts bedtimes on one axis around midnight.

# %%
silver_sleep_sessions = load(C.SILVER, "silver_sleep_sessions")
duckdb.sql(f"SELECT * FROM silver_sleep_sessions LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# **`silver_sleep_stages`** — one row per stage, with the stated duration
# next to the one derived from start/end times and a `duration_mismatch`
# flag where they disagree.

# %%
silver_sleep_stages = load(C.SILVER, "silver_sleep_stages")
duckdb.sql(f"SELECT * FROM silver_sleep_stages LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# **`silver_wellness_survey`** — one row per participant-day, typed, with
# the orphan-participant rows moved to quarantine.

# %%
silver_wellness_survey = load(C.SILVER, "silver_wellness_survey")
duckdb.sql(f"SELECT * FROM silver_wellness_survey LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# **`silver_participant_day_coverage`** — the spine: one row per
# participant-day in the study grid, with how many minutes of each signal
# arrived and the `is_complete_day` / `is_missing_day` flags Gold filters on.

# %%
silver_participant_day_coverage = load(C.SILVER, "silver_participant_day_coverage")
duckdb.sql(f"SELECT * FROM silver_participant_day_coverage LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# ## Gold — the four analysis-ready tables

# %% [markdown]
# **`gold_participant_devices`** — one row per participant linking
# `participant_id` to `device_id`, with the device and demographic
# attributes alongside.

# %%
gold_participant_devices = load(C.GOLD, "gold_participant_devices")
duckdb.sql(f"SELECT * FROM gold_participant_devices LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# **`gold_daily_participant_metrics`** — the clinical team's daily grain: one
# row per participant-day with activity, heart-rate, sleep and survey
# metrics plus the quality flags and per-day correction counts. Partitioned
# by `date`.

# %%
gold_daily_participant_metrics = load(C.GOLD, "gold_daily_participant_metrics")
duckdb.sql(f"SELECT * FROM gold_daily_participant_metrics LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# **`gold_weekly_participant_summary`** — the biostatistics team's weekly
# grain: one row per participant-week with means, SDs and completeness
# counts.

# %%
gold_weekly_participant_summary = load(C.GOLD, "gold_weekly_participant_summary")
duckdb.sql(f"SELECT * FROM gold_weekly_participant_summary LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# **`gold_chronotype_cohort_metrics`** — cohort level: one row per
# chronotype per period (`period_type` separates the whole-study rows from
# the per-week ones).

# %%
gold_chronotype_cohort_metrics = load(C.GOLD, "gold_chronotype_cohort_metrics")
duckdb.sql(f"SELECT * FROM gold_chronotype_cohort_metrics LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# ## Gold analytics — the analytical query results
#
# The published output of `poc/sql/analytics_*.sql`, rendered together in
# `poc/outputs/analytical_query_output.md`.

# %% [markdown]
# **`analytics_01_cohort_comparison`** — morning (A) vs evening (B) cohort,
# one row per metric.

# %%
analytics_01_cohort_comparison = load(ANALYTICS, "analytics_01_cohort_comparison")
duckdb.sql(f"SELECT * FROM analytics_01_cohort_comparison LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# **`analytics_02_chronotype_validation`** — declared vs observed chronotype.

# %%
analytics_02_chronotype_validation = load(ANALYTICS, "analytics_02_chronotype_validation")
duckdb.sql(f"SELECT * FROM analytics_02_chronotype_validation LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# **`analytics_03_weekly_trend`** — the cohort comparison per study week.

# %%
analytics_03_weekly_trend = load(ANALYTICS, "analytics_03_weekly_trend")
duckdb.sql(f"SELECT * FROM analytics_03_weekly_trend LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# **`analytics_04_cohort_effect_size`** — one row per participant: the
# participant-level inputs the effect-size test (Cohen's d, Welch's t) in
# `analytical_query_output.md` is computed from.

# %%
analytics_04_cohort_effect_size = load(ANALYTICS, "analytics_04_cohort_effect_size")
duckdb.sql(f"SELECT * FROM analytics_04_cohort_effect_size LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# ## Quarantine — rejected, not dropped
#
# Rows that couldn't be repaired, with `reject_reason` attached.

# %% [markdown]
# **`sleep_sessions`** (quarantine) — sleep sessions rejected from Silver
# (e.g. `stage_duration_mismatch`, `sleep_end_corrupt`).

# %%
quarantine_sleep_sessions = load(C.QUARANTINE, "sleep_sessions")
duckdb.sql(f"SELECT * FROM quarantine_sleep_sessions LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# **`wellness_survey`** (quarantine) — survey rows rejected from Silver
# (`orphan_participant`: no such participant in the roster).

# %%
quarantine_wellness_survey = load(C.QUARANTINE, "wellness_survey")
duckdb.sql(f"SELECT * FROM quarantine_wellness_survey LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# ## Data quality — the check log
#
# **`dq_checks`** — one row per rule per table per run: `severity`,
# `n_affected`, and which participants were hit. Partitioned by `run_date`.

# %%
dq_checks = load(C.DQ, "dq_checks")
duckdb.sql(f"SELECT * FROM dq_checks LIMIT {SAMPLE_ROWS}").df()

# %% [markdown]
# ## Coverage check
#
# Compares what this notebook sampled against what is actually published,
# so a new table added to the pipeline shows up here instead of being
# silently skipped.

# %%
published = {
    (layer, name)
    for layer in (C.BRONZE, C.SILVER, C.GOLD, ANALYTICS, C.QUARANTINE, C.DQ)
    for name in list_tables(layer)
    if not name.startswith("_")  # gold/_analytics is sampled as its own layer
}
missing = sorted(published - _sampled)
print(f"{len(_sampled)} of {len(published)} published tables sampled")
print("nothing missing" if not missing else f"not sampled yet: {missing}")
