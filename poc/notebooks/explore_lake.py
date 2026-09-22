# # Explore the lake (Loka data engineering test)
#
# Browse whatever `poc_run_etl.sh` has published — Bronze, Silver, Gold,
# quarantine, and the data-quality checks — without re-running any of it.
#
# ```python
# LAKE_SOURCE = "local"   # poc/data/lake, the offline mirror — no AWS
# LAKE_SOURCE = "s3"      # the bucket in poc/.env (TARGET_URI_REMOTE)
# ```
#
# **Prereq:** run the pipeline at least once first — `./poc_run_etl.sh`
# (offline) or `./poc_run_etl.sh --online` — or there is nothing published
# yet to look at.

# %%
from __future__ import annotations

import duckdb
import pandas as pd

from lake_client import LAKE_SOURCE, describe, list_tables, load_table
from etl import config as C

pd.set_option("display.max_columns", 60)
pd.set_option("display.width", 200)

describe()

# %% [markdown]
# ## Bronze — verbatim + lineage
#
# Nothing is cleaned here. Timestamps are still strings, sentinels are still
# in the data, the undocumented unit field (if present) is still whatever it
# was called in the export. The three lineage columns show where every row
# came from and which run landed it.

# %%
bronze_steps = load_table(C.BRONZE, "bronze_steps")
print(bronze_steps.shape)
bronze_steps.head()

# %%
bronze_steps[["_source_uri", "_ingested_at", "_run_id"]].drop_duplicates().head()

# %% [markdown]
# ## Silver — cleaned, typed, flagged
#
# Every correction leaves a boolean flag column behind rather than
# disappearing silently — `was_rescaled`, `was_sentinel`, `is_stuck`, etc.

# %%
silver_hr = load_table(C.SILVER, "silver_heart_rate_minute")
print(silver_hr.shape)
silver_hr.head()

# %%
# How many minutes were actually touched by each rule, on this run:
silver_hr[["was_utc", "was_off_minute", "was_out_of_range", "is_stuck"]].sum()

# %% [markdown]
# ## Quarantine — rejected, not dropped
#
# Sleep sessions and survey rows that couldn't be repaired land here instead
# of Silver, with the reason attached, instead of silently vanishing.

# %%
if "sleep_sessions" in list_tables(C.QUARANTINE):
    quarantined_sleep = load_table(C.QUARANTINE, "sleep_sessions")
    print(quarantined_sleep.shape)
    quarantined_sleep["reject_reason"].value_counts()
else:
    print("no quarantined sleep sessions on this run")

# %% [markdown]
# ## Gold — the four analysis-ready tables
#
# All four are chained SQL views over Silver (see `poc/sql/`), so they are
# always consistent with each other on a given run.

# %%
gold_tables = {
    name: load_table(C.GOLD, name)
    for name in (
        "gold_participant_devices",
        "gold_daily_participant_metrics",
        "gold_weekly_participant_summary",
        "gold_chronotype_cohort_metrics",
    )
}
for name, df in gold_tables.items():
    print(f"{name:<34} {df.shape}")

# %%
gold_tables["gold_daily_participant_metrics"].head()

# %% [markdown]
# ## Data quality — every `Check` emitted on this run
#
# One row per rule per table; `severity` is `error` (a handled source
# defect), `warn`, `info`, or `assert_fail` (a pipeline invariant broken —
# the thing `--strict` fails the run on).

# %%
dq = load_table(C.DQ, "dq_checks")
dq["severity"].value_counts()

# %%
dq.sort_values("n_affected", ascending=False)[
    ["table", "rule", "severity", "n_affected", "detail"]
].head(15)

# %% [markdown]
# ## Ad-hoc SQL over what's loaded
#
# DuckDB queries straight over the DataFrames already in this notebook — no
# registration step needed, same engine `gold.py` builds the tables with.
# `is_complete_day` is the flag to filter on for volume metrics (steps); it
# travels with the row rather than being applied at write time, so it's a
# query-time decision here too.

# %%
daily = gold_tables["gold_daily_participant_metrics"]
devices = gold_tables["gold_participant_devices"]

duckdb.sql(
    """
    SELECT d.wear_site,
           count(*)                          AS participant_days,
           round(avg(m.steps_total), 0)      AS avg_daily_steps,
           round(avg(m.hr_resting_bpm), 1)   AS avg_resting_hr
    FROM daily m
    JOIN devices d USING (participant_id)
    WHERE m.is_complete_day
    GROUP BY 1
    ORDER BY avg_daily_steps DESC
    """
)

# %% [markdown]
# The query above answers one number per group. This one just hands back
# rows to actually look at — the ten highest-activity complete participant-
# days on record, unaggregated:

# %%
duckdb.sql(
    """
    SELECT participant_id, date, steps_total, active_minutes,
           hr_resting_bpm, sleep_efficiency_pct, coverage_pct
    FROM daily
    WHERE is_complete_day
    ORDER BY steps_total DESC
    LIMIT 10
    """
).df()
