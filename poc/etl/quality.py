"""Data quality: the checks the pipeline emits, and the invariants it asserts.

Two different things live here and the distinction matters:

  * an **error** is a defect *found in the source data* and handled by a Silver
    rule. Finding 5 767 duplicate rows is the pipeline working, not failing.
  * an **assert** is an invariant *the pipeline itself guarantees* - Gold is
    unique on its key, the grid is 50 x 28, no step value survived un-rescaled.
    A failed assert means our code is wrong, and `--strict` exits non-zero on
    it so CI can gate a deploy.

Everything lands in `_dq/dq_checks` partitioned by run date, so the quality of
a past run stays auditable after the fact.
"""

from __future__ import annotations

import logging

import pandas as pd

from . import config as C
from .silver import Check

log = logging.getLogger(__name__)

SEV_ORDER = {"assert_fail": 0, "error": 1, "warn": 2, "assert_pass": 3, "info": 4}


def _assert(name: str, table: str, ok: bool, detail: str, n: int = 0) -> Check:
    return Check(table, name, "assert_pass" if ok else "assert_fail", 0 if ok else (n or 1), detail)


def pipeline_assertions(
    silver: dict[str, pd.DataFrame], gold: dict[str, pd.DataFrame]
) -> list[Check]:
    checks: list[Check] = []

    participants = silver["silver_participants"]
    n_part = len(participants)
    expected_days = C.STUDY_DAYS

    # --- uniqueness of every published key --------------------------------
    for table, keys in (
        ("silver_steps_minute", ["participant_id", "timestamp"]),
        ("silver_heart_rate_minute", ["participant_id", "timestamp"]),
        ("silver_sleep_sessions", ["session_key"]),
        ("silver_wellness_survey", ["participant_id", "date"]),
        ("silver_participant_day_coverage", ["participant_id", "date"]),
    ):
        df = silver[table]
        dupes = int(df.duplicated(keys).sum())
        checks.append(_assert("unique_key", table, dupes == 0, f"key {tuple(keys)}", dupes))

    for table, keys in (
        ("gold_daily_participant_metrics", ["participant_id", "date"]),
        ("gold_weekly_participant_summary", ["participant_id", "study_week"]),
        ("gold_participant_devices", ["participant_id"]),
        ("gold_chronotype_cohort_metrics", ["period_type", "study_week", "chronotype"]),
    ):
        df = gold[table]
        dupes = int(df.duplicated(keys, keep="first").sum())
        checks.append(_assert("unique_key", table, dupes == 0, f"key {tuple(keys)}", dupes))

    # --- the grid is the grid ----------------------------------------------
    daily = gold["gold_daily_participant_metrics"]
    expected_rows = n_part * expected_days
    checks.append(
        _assert("row_count_equals_grid", "gold_daily_participant_metrics",
                len(daily) == expected_rows,
                f"expected {n_part} participants x {expected_days} days = {expected_rows}, got {len(daily)}",
                abs(len(daily) - expected_rows))
    )
    checks.append(
        _assert("one_row_per_participant", "gold_participant_devices",
                len(gold["gold_participant_devices"]) == n_part,
                f"expected {n_part}, got {len(gold['gold_participant_devices'])}")
    )

    # --- no participant silently lost between layers -------------------------
    missing = set(participants["participant_id"]) - set(daily["participant_id"])
    checks.append(
        _assert("no_participant_dropped", "gold_daily_participant_metrics",
                not missing, f"missing: {sorted(missing)}" if missing else "all present", len(missing))
    )

    # --- the unit bug cannot survive ------------------------------------------
    steps = silver["silver_steps_minute"]
    implausible = int((steps["steps"] > 600).sum())
    checks.append(
        _assert("no_implausible_step_minute", "silver_steps_minute", implausible == 0,
                "a minute above 600 steps would mean an hourly rate escaped rescaling",
                implausible)
    )
    hr = silver["silver_heart_rate_minute"]
    lo, hi = C.HR_VALID_RANGE
    out_of_range = int(((hr["heart_rate_bpm"] < lo) | (hr["heart_rate_bpm"] > hi)).sum())
    checks.append(
        _assert("heart_rate_in_range", "silver_heart_rate_minute", out_of_range == 0,
                f"all surviving values within {lo}-{hi} bpm", out_of_range)
    )

    # --- derived sleep metrics are self-consistent ----------------------------
    sleep = silver["silver_sleep_sessions"]
    bad_eff = int(((sleep["sleep_efficiency_pct"] < 0) | (sleep["sleep_efficiency_pct"] > 100)).sum())
    checks.append(
        _assert("sleep_efficiency_in_range", "silver_sleep_sessions", bad_eff == 0,
                "stage-derived efficiency within 0-100", bad_eff)
    )
    bad_tib = int((sleep["total_sleep_min"] > sleep["time_in_bed_min"] + 1e-6).sum())
    checks.append(
        _assert("sleep_not_longer_than_bed", "silver_sleep_sessions", bad_tib == 0,
                "total_sleep_min <= time_in_bed_min", bad_tib)
    )

    # --- referential integrity -------------------------------------------------
    roster = set(participants["participant_id"])
    for table in ("silver_steps_minute", "silver_heart_rate_minute",
                  "silver_sleep_sessions", "silver_wellness_survey"):
        orphans = int((~silver[table]["participant_id"].isin(roster)).sum())
        checks.append(
            _assert("fk_participant_id", table, orphans == 0, "every row joins to the roster", orphans)
        )
    return checks


def to_frame(checks: list[Check], run_id: str, run_started: pd.Timestamp) -> pd.DataFrame:
    df = pd.DataFrame([c.as_row() for c in checks])
    if df.empty:
        df = pd.DataFrame(columns=["table", "rule", "severity", "n_affected", "detail", "participants"])
    df["run_id"] = run_id
    df["run_at"] = run_started
    df["run_date"] = run_started.date()
    df["_sev_order"] = df["severity"].map(SEV_ORDER).fillna(9)
    df = df.sort_values(["_sev_order", "table", "rule"]).drop(columns="_sev_order").reset_index(drop=True)
    return df


def summarise(df: pd.DataFrame) -> dict[str, int]:
    counts = df["severity"].value_counts().to_dict()
    return {k: int(counts.get(k, 0)) for k in ("assert_fail", "error", "warn", "assert_pass", "info")}


def report(df: pd.DataFrame, run_id: str, stats: dict[str, object]) -> str:
    s = summarise(df)
    lines = [
        "# Pipeline run report",
        "",
        f"- run id: `{run_id}`",
        f"- source: `{stats.get('source_uri')}`",
        f"- target: `{stats.get('target_uri')}`",
        f"- wall clock: {stats.get('elapsed_s')}s",
        "",
        "## Check summary",
        "",
        f"| failed assertions | source defects handled | warnings | assertions passed |",
        "| ---: | ---: | ---: | ---: |",
        f"| {s['assert_fail']} | {s['error']} | {s['warn']} | {s['assert_pass']} |",
        "",
        "A *source defect* is a problem found in the export and corrected by a Silver "
        "rule - a non-zero count there is the pipeline working. A *failed assertion* "
        "is an invariant this pipeline guarantees being violated, and is a bug.",
        "",
        "## Rows written",
        "",
        "| table | rows |",
        "| --- | ---: |",
    ]
    for name, n in (stats.get("row_counts") or {}).items():
        lines.append(f"| `{name}` | {n:,} |")
    lines += ["", "## Checks", "", df.drop(columns=["run_at", "run_date"], errors="ignore").to_markdown(index=False), ""]
    return "\n".join(lines)
