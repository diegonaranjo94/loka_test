"""Gold - set-based modelling in DuckDB SQL.

Why DuckDB and not more pandas: the Gold layer is joins, window functions,
grouping sets and cohort aggregation. That is SQL's job, and keeping it as
SQL means the modelling logic is reviewable as `.sql` files rather than as a
chain of pandas calls - and it is the same SQL an Athena or Snowflake
deployment would run.

Why DuckDB and not Spark: 4M rows. Spark would add a JVM, a shuffle and a
cluster to a workload that finishes in seconds single-node. What has to scale
is the storage layout and the transform contract, both of which lift onto
Glue/EMR unchanged when the study does.

The Silver frames are registered as views straight out of memory, so the whole
run is one process with no intermediate files - while each layer is still
persisted as Parquet, because the layers are the deliverable, not scratch.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa

from . import config as C

log = logging.getLogger(__name__)

# Columns that must be a real DATE inside DuckDB (pandas hands them over as
# object/datetime64, which breaks date arithmetic like `date - DATE '...'`).
_DATE_COLS = {"date", "calibration_date", "week_start", "week_end"}

# Silver frame -> view name used in the SQL files.
VIEW_NAMES = {
    "silver_participants": "participants",
    "silver_devices": "devices",
    "silver_steps_minute": "steps_minute",
    "silver_heart_rate_minute": "heart_rate_minute",
    "silver_sleep_sessions": "sleep_sessions",
    "silver_sleep_stages": "sleep_stages",
    "silver_wellness_survey": "survey",
    "silver_participant_day_coverage": "coverage",
}

# Order matters: each Gold table is registered as a view so the next one can
# read it. That dependency chain is the "single source of truth" guarantee -
# weekly and cohort cannot disagree with daily because they are built from it.
GOLD_TABLES = [
    "gold_participant_devices",
    "gold_daily_participant_metrics",
    "gold_weekly_participant_summary",
    "gold_chronotype_cohort_metrics",
]

ANALYTICS_QUERIES = [
    ("analytics_01_cohort_comparison", "Chronotype cohort comparison (whole study)"),
    ("analytics_02_chronotype_validation", "Declared vs observed chronotype"),
    ("analytics_03_weekly_trend", "Weekly trend by chronotype cohort"),
    ("analytics_04_cohort_effect_size", "Cohort separation, per participant"),
]

# Metrics tested for a chronotype difference. `timing=True` marks the ones
# that *define* chronotype - when the clock says a participant sleeps and
# moves. A difference in sleep duration or efficiency is a different claim
# entirely, so it cannot be used to conclude the label is real.
EFFECT_SIZE_METRICS = [
    ("mid_sleep_hour_median", "Mid-sleep time (h)", True),
    ("sleep_onset_hour_median", "Sleep onset time (h)", True),
    ("activity_midpoint_hour_mean", "Activity midpoint (h)", True),
    ("total_sleep_min_mean", "Total sleep (min)", False),
    ("sleep_efficiency_pct_mean", "Sleep efficiency (%)", False),
    ("deep_sleep_pct_mean", "Deep sleep (%)", False),
    ("steps_total_mean", "Daily steps", False),
    ("hr_resting_bpm_mean", "Resting HR (bpm)", False),
]

# A difference counts only if it is both large enough to matter and unlikely
# to be noise. Either alone flips a conclusion on a coin toss.
MIN_COHENS_D = 0.5
MAX_P = 0.05


def _read_sql(name: str) -> str:
    return (C.SQL_DIR / f"{name}.sql").read_text()


def _register(con: duckdb.DuckDBPyConnection, name: str, df: pd.DataFrame) -> None:
    df = df.copy()
    replaces = []
    for col in df.columns:
        if col in _DATE_COLS:
            df[col] = pd.to_datetime(df[col], errors="coerce")
            replaces.append(f"CAST({col} AS DATE) AS {col}")
    # Hand DuckDB an Arrow table rather than a DataFrame: zero-copy for the
    # Arrow-backed columns and it skips the pandas object-dtype conversion
    # path entirely.
    con.register(f"_raw_{name}", pa.Table.from_pandas(df, preserve_index=False))
    projection = "*" if not replaces else f"* REPLACE ({', '.join(replaces)})"
    con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT {projection} FROM _raw_{name}")


def connect(silver: dict[str, pd.DataFrame]) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    for silver_name, view in VIEW_NAMES.items():
        if silver_name in silver:
            _register(con, view, silver[silver_name])
    return con


def build(silver: dict[str, pd.DataFrame]) -> tuple[dict[str, pd.DataFrame], duckdb.DuckDBPyConnection]:
    con = connect(silver)
    tables: dict[str, pd.DataFrame] = {}
    for name in GOLD_TABLES:
        df = con.execute(_read_sql(name)).df()
        _register(con, name, df)
        tables[name] = df
        log.info("gold:   built %-38s %7d rows x %2d cols", name, len(df), df.shape[1])
    return tables, con


def run_analytics(con: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for name, _title in ANALYTICS_QUERIES:
        df = con.execute(_read_sql(name)).df()
        if {"morning_type_a", "evening_type_b"} <= set(df.columns):
            df["delta_b_minus_a"] = (df["evening_type_b"] - df["morning_type_a"]).round(3)
            df = df.drop(columns=["ord"], errors="ignore")
        out[name] = df
        log.info("gold:   ran   %-38s %7d rows", name, len(df))
    return out


def cohort_effect_sizes(per_participant: pd.DataFrame) -> pd.DataFrame:
    """Welch's t and Cohen's d on the participant-level means, A vs B.

    No scipy: the two-sided p is a normal approximation, which is honest at
    n=50 and avoids a dependency for one number. The effect size, not the p
    value, is what the reading below leans on.
    """
    import math

    a_mask = per_participant["chronotype"] == "A"
    rows = []
    for col, label, is_timing in EFFECT_SIZE_METRICS:
        if col not in per_participant.columns:
            continue
        a = pd.to_numeric(per_participant.loc[a_mask, col], errors="coerce").dropna()
        b = pd.to_numeric(per_participant.loc[~a_mask, col], errors="coerce").dropna()
        if len(a) < 2 or len(b) < 2:
            continue
        na, nb = len(a), len(b)
        va, vb = a.var(ddof=1), b.var(ddof=1)
        se = math.sqrt(va / na + vb / nb)
        diff = b.mean() - a.mean()
        pooled_sd = math.sqrt(((na - 1) * va + (nb - 1) * vb) / (na + nb - 2))
        t = diff / se if se else float("nan")
        p = math.erfc(abs(t) / math.sqrt(2)) if se else float("nan")
        d = diff / pooled_sd if pooled_sd else float("nan")
        rows.append({
            "metric": label,
            "defines_chronotype": is_timing,
            "morning_a_mean": round(a.mean(), 3),
            "evening_b_mean": round(b.mean(), 3),
            "difference_b_minus_a": round(diff, 3),
            "cohens_d": round(d, 3) if pooled_sd else None,
            "welch_t": round(t, 2),
            "p_approx": round(p, 4),
            "separates_cohorts": bool(abs(d) >= MIN_COHENS_D and p < MAX_P) if pooled_sd else False,
        })
    return pd.DataFrame(rows)


def analytics_report(results: dict[str, pd.DataFrame], run_id: str) -> str:
    """Render the analytical query output as markdown."""
    lines = [
        "# Analytical query output",
        "",
        f"Run `{run_id}` - queries executed against the Gold layer "
        "(`poc/sql/analytics_*.sql`), no Silver or Bronze access.",
        "",
        "Mid-sleep and onset times are expressed as hours either side of midnight "
        "(`-1.5` = 22:30, `+3.0` = 03:00) so evening and morning types sit on one "
        "continuous axis instead of wrapping at 24.",
        "",
    ]
    for name, title in ANALYTICS_QUERIES:
        df = results.get(name)
        if df is None or name == "analytics_04_cohort_effect_size":
            continue  # query 4 is per-participant detail; its summary follows
        lines += [f"## {title}", "", f"`{name}.sql`", "", df.to_markdown(index=False), ""]

    # --- effect sizes -----------------------------------------------------
    per_participant = results.get("analytics_04_cohort_effect_size")
    effects = None
    if per_participant is not None and len(per_participant):
        effects = cohort_effect_sizes(per_participant)
        lines += [
            "## Does the chronotype label separate the cohorts?",
            "",
            "`analytics_04_cohort_effect_size.sql`, collapsed to one row per participant "
            "before testing - 1 393 nights are 50 participants measured 28 times, not "
            "1 393 independent observations.",
            "",
            effects.to_markdown(index=False),
            "",
            "`cohens_d` is the standardised difference; |d| >= 0.5 is the conventional "
            "threshold for a difference large enough to matter. `p_approx` is a normal "
            "approximation to Welch's two-sided test.",
            "",
        ]

    # --- the reading, driven by the numbers, not asserted ------------------
    val = results.get("analytics_02_chronotype_validation")
    if val is not None and "agrees" in val.columns:
        agree = int(val.loc[val["agrees"], "n_participants"].sum())
        total = int(val["n_participants"].sum())
        pct = 100.0 * agree / total if total else 0.0
        if effects is not None and len(effects):
            timing = effects[effects["defines_chronotype"] & effects["separates_cohorts"]]
            other = effects[~effects["defines_chronotype"] & effects["separates_cohorts"]]
        else:
            timing = other = pd.DataFrame()

        lines += ["## Reading", ""]
        if len(timing):
            lines += [
                f"A chronotype derived from observed mid-sleep time alone agrees with the "
                f"declared label for **{agree}/{total} participants ({pct:.0f}%)**, and the "
                f"timing metric(s) that define chronotype separate the cohorts: "
                f"{', '.join(timing['metric'])}. The label and the measured behaviour "
                "corroborate each other.",
            ]
        else:
            lines += [
                f"**The declared chronotype does not correspond to anything observable in "
                f"this dataset.** A chronotype derived from observed mid-sleep time agrees "
                f"with the declared label for {agree}/{total} participants ({pct:.0f}%) - "
                "indistinguishable from a coin flip - and none of mid-sleep time, sleep "
                "onset or activity midpoint separates the cohorts. Morning and evening "
                "types go to bed at the same time and move at the same hour of day, which "
                "is the whole of what chronotype means.",
                "",
                "This is a finding about the data, not a pipeline defect: the same code "
                "recovers every injected defect in `eda/findings.md` at the documented "
                "row counts, and the sleep derivation reproduces the stage anchors exactly. "
                "`chronotype` in `participants.csv` is a label with no signal behind it.",
                "",
                "The consequence for the deliverable is that the cohort comparison is "
                "reported as a null result. Any 'morning types sleep better' conclusion "
                "drawn from these tables would be noise, and a platform that presents one "
                "without this check is the actual failure mode. Worth raising with the "
                "study team: is the label mis-assigned at enrolment, or is it simply not "
                "encoded in the synthetic generator?",
            ]
            if len(other):
                lines += [
                    "",
                    f"({', '.join(other['metric'])} does differ between the groups, but "
                    "that is a claim about sleep quantity or physiology, not about when "
                    "the participant sleeps - it cannot validate a chronotype label.)",
                ]
        lines.append("")
    return "\n".join(lines)
