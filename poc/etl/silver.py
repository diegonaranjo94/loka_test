"""Silver - one function per documented defect, each with a check it emits.

Rule order matters (see eda/findings.md):

  1. normalise timezone   - the Z-suffixed rows are the *same* minutes as their
                            naive twins; they only collide once both are on the
                            study clock, so this has to precede dedupe.
  2. snap to minute       - off-minute rows are near-duplicates of the minute
                            beside them; snapping first turns them into exact
                            duplicates the dedupe step can see.
  3. rescale by unit      - before any arithmetic, or a summed day is 60x high.
  4. null sentinels       - -1 / 1000000 / -10 / 999 are null markers, not data.
  5. de-duplicate         - now, and only now, is (participant, minute) unique.
  6. flag stuck sensor    - in-range values, so only run-length encoding finds
                            them. Flag, never delete: the minutes are real.

Everything returns (dataframe, checks) so the data-quality report is produced
by the same code path that does the cleaning - it cannot drift out of date.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from . import config as C

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Check plumbing
# --------------------------------------------------------------------------
@dataclass
class Check:
    table: str
    rule: str
    severity: str  # error | warn | info
    n_affected: int
    detail: str = ""
    participants: str = ""

    def as_row(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "rule": self.rule,
            "severity": self.severity,
            "n_affected": int(self.n_affected),
            "detail": self.detail,
            "participants": self.participants,
        }


def _pids(series: pd.Series, limit: int = 12) -> str:
    vals = sorted(pd.unique(series.dropna()))
    shown = ", ".join(map(str, vals[:limit]))
    return shown + (f", +{len(vals) - limit} more" if len(vals) > limit else "")


# --------------------------------------------------------------------------
# Rule 1+2 - timezone normalisation and minute snapping
# --------------------------------------------------------------------------
def normalise_timestamps(df: pd.DataFrame, table: str) -> tuple[pd.DataFrame, list[Check]]:
    """Put every row on the study's local clock, on a whole-minute boundary."""
    checks: list[Check] = []
    raw = df["timestamp"]
    if not pd.api.types.is_string_dtype(raw):
        raw = raw.astype("string")

    is_utc = raw.str.endswith("Z").fillna(False).astype(bool)
    naive = pd.to_datetime(raw.where(~is_utc), format="ISO8601", errors="coerce")
    aware = pd.to_datetime(raw.where(is_utc), format="ISO8601", utc=True, errors="coerce")
    converted = aware.dt.tz_convert(C.STUDY_TZ).dt.tz_localize(None)

    ts = naive.fillna(converted)
    unparsed = int(ts.isna().sum())

    out = df.copy()
    out["timestamp"] = ts
    out["was_utc"] = is_utc

    if is_utc.any():
        checks.append(
            Check(table, "timezone_normalised", "error", int(is_utc.sum()),
                  f"rows exported as Z-suffixed UTC, pulled back to {C.STUDY_TZ_LABEL}",
                  _pids(out.loc[is_utc, "participant_id"]))
        )
    if unparsed:
        checks.append(Check(table, "timestamp_unparseable", "error", unparsed, "left null"))

    off_minute = out["timestamp"].notna() & (out["timestamp"].dt.second != 0)
    if off_minute.any():
        checks.append(
            Check(table, "timestamp_snapped_to_minute", "error", int(off_minute.sum()),
                  "seconds != 00; floored to the minute before de-duplication",
                  _pids(out.loc[off_minute, "participant_id"]))
        )
    out["was_off_minute"] = off_minute
    out["timestamp"] = out["timestamp"].dt.floor("min")
    return out, checks


# --------------------------------------------------------------------------
# Rule 3 - unit rescaling
# --------------------------------------------------------------------------
def rescale_units(df: pd.DataFrame, value_col: str, table: str) -> tuple[pd.DataFrame, list[Check]]:
    """Divide hourly-rate rows down onto the per-minute grid.

    An unknown unit is an error, not a silent pass-through: guessing here is
    how a 60x inflation ships to a clinician.
    """
    checks: list[Check] = []
    out = df.copy()

    # The export does not agree with itself on the field's name (`_unit` here),
    # so resolve it by candidate instead of trusting one spelling.
    unit_col = next((c for c in C.UNIT_COLUMN_CANDIDATES if c in out.columns), None)
    if unit_col is None:
        out["unit"] = pd.NA
    else:
        out["unit"] = out[unit_col]
        if unit_col != "unit":
            checks.append(
                Check(table, "unit_field_nonstandard_name", "warn",
                      int(out[unit_col].notna().sum()),
                      f"unit arrives as `{unit_col}`, not `unit`; resolved by candidate lookup")
            )

    unit = out["unit"].astype("string").str.strip().str.lower()
    known = unit.isna() | unit.isin(C.UNIT_RESCALE_DIVISOR)
    if (~known).any():
        bad = sorted(pd.unique(unit[~known].dropna()))
        checks.append(
            Check(table, "unit_unknown", "error", int((~known).sum()),
                  f"unrecognised unit(s) {bad}; rows nulled rather than guessed",
                  _pids(out.loc[~known, "participant_id"]))
        )

    divisor = unit.map(C.UNIT_RESCALE_DIVISOR).astype("float64").fillna(1.0)
    rescaled = divisor > 1
    out["unit_raw"] = out["unit"]
    out["was_rescaled"] = rescaled
    out[value_col] = pd.to_numeric(out[value_col], errors="coerce")
    out.loc[~known, value_col] = np.nan
    out[value_col] = out[value_col] / divisor

    if rescaled.any():
        checks.append(
            Check(table, "unit_rescaled", "error", int(rescaled.sum()),
                  "rows carried an hourly rate on a per-minute grid; divided by 60",
                  _pids(out.loc[rescaled, "participant_id"]))
        )
    return out, checks


# --------------------------------------------------------------------------
# Rule 4 - sentinels / out of range
# --------------------------------------------------------------------------
def null_invalid_values(
    df: pd.DataFrame, value_col: str, table: str,
    sentinels: tuple[int, ...] = (), valid_range: tuple[float, float] | None = None,
) -> tuple[pd.DataFrame, list[Check]]:
    checks: list[Check] = []
    out = df.copy()
    vals = pd.to_numeric(out[value_col], errors="coerce")

    bad = pd.Series(False, index=out.index)
    if sentinels:
        bad |= vals.isin(list(sentinels))
    if valid_range is not None:
        lo, hi = valid_range
        bad |= vals.notna() & ((vals < lo) | (vals > hi))

    if bad.any():
        offending = [
            int(v) if float(v).is_integer() else float(v)
            for v in sorted(pd.unique(vals[bad].dropna()))[:8]
        ]
        checks.append(
            Check(table, "value_out_of_domain", "error", int(bad.sum()),
                  f"nulled {offending} (sentinels/impossible values); the minute is kept",
                  _pids(out.loc[bad, "participant_id"]))
        )
    out[value_col] = vals.mask(bad)
    out["value_was_invalid"] = bad
    return out, checks


# --------------------------------------------------------------------------
# Rule 5 - de-duplication
# --------------------------------------------------------------------------
def deduplicate(df: pd.DataFrame, keys: list[str], table: str) -> tuple[pd.DataFrame, list[Check]]:
    """First-seen wins.

    The export carries no ingest timestamp, so there is no defensible basis for
    last-write-wins; first-seen is deterministic given a stable read order,
    which is what keeps the pipeline idempotent. Recorded as an assumption in
    the README.
    """
    checks: list[Check] = []
    dupe = df.duplicated(subset=keys, keep="first")
    if dupe.any():
        affected = df.loc[dupe]
        days = affected["timestamp"].dt.date.nunique() if "timestamp" in affected else 0
        checks.append(
            Check(table, "duplicate_rows_removed", "error", int(dupe.sum()),
                  f"duplicate {tuple(keys)} across {days} participant-day(s); kept first-seen",
                  _pids(affected["participant_id"]))
        )
    return df.loc[~dupe].reset_index(drop=True), checks


# --------------------------------------------------------------------------
# Rule 6 - stuck sensor
# --------------------------------------------------------------------------
def flag_stuck_sensor(df: pd.DataFrame, value_col: str, table: str) -> tuple[pd.DataFrame, list[Check]]:
    """Run-length encode contiguous identical values; flag long runs."""
    out = df.sort_values(["participant_id", "timestamp"], kind="stable").reset_index(drop=True)

    # Arrow-backed columns yield Arrow booleans, which have no cumsum kernel -
    # pull the masks down to plain numpy before the run-length encoding.
    def _mask(s: pd.Series) -> np.ndarray:
        return s.fillna(False).to_numpy(dtype=bool, na_value=False)

    same_value = _mask(out[value_col].eq(out[value_col].shift()))
    same_person = _mask(out["participant_id"].eq(out["participant_id"].shift()))
    adjacent = _mask(out["timestamp"].diff().eq(pd.Timedelta(minutes=1)))

    run_id = pd.Series(np.cumsum(~(same_value & same_person & adjacent)), index=out.index)
    run_len = out.groupby(run_id)[value_col].transform("size")
    stuck = (run_len >= C.HR_STUCK_MIN_RUN_MINUTES) & out[value_col].notna()
    stuck = pd.Series(_mask(stuck), index=out.index)

    out["is_stuck"] = stuck
    checks: list[Check] = []
    if stuck.any():
        n_runs = int(run_id[stuck].nunique())
        checks.append(
            Check(table, "stuck_sensor_flagged", "error", int(stuck.sum()),
                  f"{n_runs} run(s) of >= {C.HR_STUCK_MIN_RUN_MINUTES} identical consecutive "
                  "minutes; flagged not deleted - the minutes are real, the values are not",
                  _pids(out.loc[stuck, "participant_id"]))
        )
    return out, checks


# --------------------------------------------------------------------------
# Minute-grain pipelines
# --------------------------------------------------------------------------
_LINEAGE_COLS = ["_source_uri", "_ingested_at", "_run_id", "_source_participant_dir"]


def _slim(df: pd.DataFrame) -> pd.DataFrame:
    """Lineage belongs in Bronze; carrying it through Silver just costs memory."""
    return df.drop(columns=[c for c in _LINEAGE_COLS if c in df.columns])


def build_steps(bronze: pd.DataFrame) -> tuple[pd.DataFrame, list[Check]]:
    table = "silver_steps_minute"
    checks: list[Check] = []

    df, c = normalise_timestamps(_slim(bronze), table); checks += c
    df, c = rescale_units(df, "steps", table); checks += c
    df, c = null_invalid_values(df, "steps", table, sentinels=C.STEPS_SENTINELS); checks += c
    df, c = deduplicate(df, ["participant_id", "timestamp"], table); checks += c

    df["steps"] = df["steps"].round().astype("Int32")
    df["date"] = df["timestamp"].dt.date
    out = df[[
        "participant_id", "timestamp", "date", "steps",
        "unit_raw", "was_rescaled", "was_utc", "was_off_minute", "value_was_invalid",
    ]].rename(columns={"value_was_invalid": "was_sentinel"})
    return out, checks


def build_heart_rate(bronze: pd.DataFrame) -> tuple[pd.DataFrame, list[Check]]:
    table = "silver_heart_rate_minute"
    checks: list[Check] = []

    df, c = normalise_timestamps(_slim(bronze), table); checks += c
    df, c = rescale_units(df, "heart_rate_bpm", table); checks += c
    df, c = null_invalid_values(
        df, "heart_rate_bpm", table, valid_range=C.HR_VALID_RANGE
    ); checks += c
    df, c = deduplicate(df, ["participant_id", "timestamp"], table); checks += c
    df, c = flag_stuck_sensor(df, "heart_rate_bpm", table); checks += c

    df["heart_rate_bpm"] = df["heart_rate_bpm"].round().astype("Int16")
    df["date"] = df["timestamp"].dt.date
    out = df[[
        "participant_id", "timestamp", "date", "heart_rate_bpm",
        "is_stuck", "was_utc", "was_off_minute", "value_was_invalid",
    ]].rename(columns={"value_was_invalid": "was_out_of_range"})
    return out, checks


# --------------------------------------------------------------------------
# Sleep - every metric derived from stages[], none trusted from the header
# --------------------------------------------------------------------------
def build_sleep(
    bronze_sessions: pd.DataFrame, bronze_stages: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[Check]]:
    """Returns (sessions, stages, quarantined_sessions, checks).

    `efficiency_pct` in the source is 100.0 on 81% of sessions and correlates
    0.15 with the stage-derived value - it is unusable, so every sleep metric
    here comes from the stage array.
    """
    table = "silver_sleep_sessions"
    checks: list[Check] = []

    st = bronze_stages.copy()
    st["date"] = pd.to_datetime(st["date"], errors="coerce").dt.date
    st["start_time"] = pd.to_datetime(st["start_time"], format="ISO8601", errors="coerce")
    st["end_time"] = pd.to_datetime(st["end_time"], format="ISO8601", errors="coerce")
    st["duration_min_stated"] = pd.to_numeric(st["duration_min"], errors="coerce")
    st["duration_min_derived"] = (st["end_time"] - st["start_time"]).dt.total_seconds() / 60.0
    st["duration_mismatch"] = (
        (st["duration_min_stated"] - st["duration_min_derived"]).abs() > 0.5
    ).fillna(True)
    st["is_asleep"] = ~st["stage"].isin(C.NON_SLEEP_STAGES)

    if st["duration_mismatch"].any():
        bad = st.loc[st["duration_mismatch"]]
        checks.append(
            Check("silver_sleep_stages", "stage_duration_mismatch", "error", int(len(bad)),
                  f"duration_min disagrees with the timestamps on stage(s) "
                  f"{sorted(pd.unique(bad['stage']))}; whole session quarantined",
                  _pids(bad["participant_id"]))
        )

    asleep = st[st["is_asleep"]]
    agg = st.groupby("session_key", observed=True).agg(
        stage_first_start=("start_time", "min"),
        stage_last_end=("end_time", "max"),
        n_stages=("stage_idx", "count"),
        any_duration_mismatch=("duration_mismatch", "any"),
    ).reset_index()
    agg_asleep = asleep.groupby("session_key", observed=True).agg(
        sleep_end_derived=("end_time", "max"),
        total_sleep_min=("duration_min_derived", "sum"),
    ).reset_index()
    by_stage = (
        st.groupby(["session_key", "stage"], observed=True)["duration_min_derived"]
        .sum().unstack(fill_value=0.0)
    )
    for stage in C.SLEEP_STAGES:
        if stage not in by_stage.columns:
            by_stage[stage] = 0.0
    by_stage = by_stage[list(C.SLEEP_STAGES)].add_suffix("_min").reset_index()

    # `n_stages` is recomputed from the stage rows below; drop the header's copy
    # so the merge does not produce n_stages_x / n_stages_y.
    s = bronze_sessions.drop(columns=["n_stages"], errors="ignore").copy()
    s["date"] = pd.to_datetime(s["date"], errors="coerce").dt.date
    s["sleep_onset_stated"] = pd.to_datetime(s["sleep_onset"], format="ISO8601", errors="coerce")
    s["sleep_end_stated"] = pd.to_datetime(s["sleep_end"], format="ISO8601", errors="coerce")
    s["efficiency_pct_stated"] = pd.to_numeric(s["efficiency_pct"], errors="coerce")
    s["restlessness"] = pd.to_numeric(s["restlessness"], errors="coerce")
    s = s.merge(agg, on="session_key", how="left")
    s = s.merge(agg_asleep, on="session_key", how="left")
    s = s.merge(by_stage, on="session_key", how="left")

    # --- derived metrics ---------------------------------------------------
    # Time in bed runs to the end of the *last* stage: the trailing `awake`
    # stage extends past the stated sleep_end, and ignoring it overstates
    # efficiency by ~12 percentage points.
    s["time_in_bed_min"] = (s["stage_last_end"] - s["stage_first_start"]).dt.total_seconds() / 60.0
    s["sleep_onset"] = s["stage_first_start"]
    s["sleep_end"] = s["sleep_end_derived"]
    s["sleep_efficiency_pct"] = 100.0 * s["total_sleep_min"] / s["time_in_bed_min"]
    s["deep_sleep_pct"] = 100.0 * s["deep_min"] / s["total_sleep_min"]
    s["rem_sleep_pct"] = 100.0 * s["rem_min"] / s["total_sleep_min"]

    # Mid-sleep, the standard chronotype axis. Expressed as hours either side
    # of midnight (-6 = 18:00, +6 = 06:00) so evening and morning types sit on
    # one continuous line instead of wrapping at 24.
    mid = s["sleep_onset"] + (s["sleep_end"] - s["sleep_onset"]) / 2
    s["mid_sleep_time"] = mid
    hour = mid.dt.hour + mid.dt.minute / 60.0 + mid.dt.second / 3600.0
    s["mid_sleep_hour_signed"] = ((hour + 12.0) % 24.0) - 12.0
    onset_h = s["sleep_onset"].dt.hour + s["sleep_onset"].dt.minute / 60.0
    s["sleep_onset_hour_signed"] = ((onset_h + 12.0) % 24.0) - 12.0

    # --- rejection rules ---------------------------------------------------
    anchor_gap = (s["sleep_end_stated"] - s["sleep_end_derived"]).dt.total_seconds().abs()
    s["sleep_end_corrupt"] = anchor_gap > C.SLEEP_ANCHOR_TOLERANCE_SECONDS
    s["stage_order_canonical"] = True  # filled below

    order = (
        st.sort_values(["session_key", "stage_idx"])
        .groupby("session_key", observed=True)["stage"]
        .apply(lambda x: "->".join(x))
    )
    canonical = "light->deep->rem->light->awake"
    s = s.merge(order.rename("stage_order").reset_index(), on="session_key", how="left")
    s["stage_order_canonical"] = s["stage_order"].eq(canonical)

    reject = (
        s["any_duration_mismatch"].fillna(True)
        | s["sleep_end_corrupt"].fillna(True)
        | s["total_sleep_min"].isna()
        | (s["time_in_bed_min"] <= 0)
    )
    s["reject_reason"] = np.select(
        [s["any_duration_mismatch"].fillna(False), s["sleep_end_corrupt"].fillna(False)],
        ["stage_duration_mismatch", "sleep_end_corrupt"],
        default="incomplete_stages",
    )

    if s["sleep_end_corrupt"].fillna(False).any():
        bad = s.loc[s["sleep_end_corrupt"].fillna(False)]
        checks.append(
            Check(table, "sleep_end_corrupt", "error", int(len(bad)),
                  "stated sleep_end disagrees with the last non-awake stage end by "
                  f"> {C.SLEEP_ANCHOR_TOLERANCE_SECONDS}s; session quarantined",
                  _pids(bad["participant_id"]))
        )
    if (~s["stage_order_canonical"]).any():
        bad = s.loc[~s["stage_order_canonical"]]
        checks.append(
            Check(table, "stage_order_non_canonical", "warn", int(len(bad)),
                  f"stage order != {canonical}", _pids(bad["participant_id"]))
        )

    unusable = (s["efficiency_pct_stated"] == 100.0).sum()
    checks.append(
        Check(table, "source_efficiency_ignored", "warn", int(unusable),
              f"{unusable}/{len(s)} sessions report efficiency_pct exactly 100.0; the field "
              "is not used - efficiency is derived from stages[]")
    )
    if reject.any():
        checks.append(
            Check(table, "sessions_quarantined", "error", int(reject.sum()),
                  "rejected rather than repaired - the correct value is unknowable",
                  _pids(s.loc[reject, "participant_id"]))
        )

    cols = [
        "session_key", "participant_id", "date", "sleep_onset", "sleep_end", "mid_sleep_time",
        "mid_sleep_hour_signed", "sleep_onset_hour_signed", "total_sleep_min", "time_in_bed_min",
        "sleep_efficiency_pct", "deep_sleep_pct", "rem_sleep_pct",
        "light_min", "deep_min", "rem_min", "awake_min", "restlessness",
        "efficiency_pct_stated", "n_stages", "stage_order_canonical",
    ]
    sessions = s.loc[~reject, cols].reset_index(drop=True)
    quarantined = s.loc[reject, cols + ["reject_reason"]].reset_index(drop=True)

    stage_cols = [
        "session_key", "participant_id", "date", "stage_idx", "stage", "is_asleep",
        "start_time", "end_time", "duration_min_stated", "duration_min_derived",
        "duration_mismatch",
    ]
    stages = st.loc[st["session_key"].isin(sessions["session_key"]), stage_cols].reset_index(drop=True)
    return sessions, stages, quarantined, checks


# --------------------------------------------------------------------------
# Reference / survey
# --------------------------------------------------------------------------
def build_participants(bronze: pd.DataFrame) -> tuple[pd.DataFrame, list[Check]]:
    table = "silver_participants"
    checks: list[Check] = []
    df = bronze.copy()
    for col in ("age", "height_cm", "max_heart_rate"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int32")

    bad_pid = ~df["participant_id"].astype("string").str.match(C.PARTICIPANT_ID_REGEX).fillna(False)
    bad_did = ~df["device_id"].astype("string").str.match(C.DEVICE_ID_REGEX).fillna(False)
    if bad_pid.any():
        checks.append(Check(table, "participant_id_malformed", "error", int(bad_pid.sum())))
    if bad_did.any():
        checks.append(Check(table, "device_id_malformed", "error", int(bad_did.sum())))

    dup = df.duplicated("participant_id")
    if dup.any():
        checks.append(Check(table, "duplicate_participant", "error", int(dup.sum())))
    df = df.loc[~dup]

    n_dev = df["device_id"].nunique()
    checks.append(
        Check(table, "participant_device_one_to_one",
              "info" if n_dev == len(df) else "error",
              0 if n_dev == len(df) else len(df) - n_dev,
              f"{len(df)} participants / {n_dev} distinct devices")
    )
    cols = ["participant_id", "device_id", "age", "height_cm", "gender", "chronotype", "max_heart_rate"]
    return df[cols].reset_index(drop=True), checks


def build_devices(
    bronze_devices: pd.DataFrame, participants: pd.DataFrame
) -> tuple[pd.DataFrame, list[Check]]:
    table = "silver_devices"
    checks: list[Check] = []
    df = bronze_devices.copy()

    label = df["device_label"].astype("string")
    parsed_fw = label.str.extract(C.FIRMWARE_REGEX, expand=False)
    stated_fw = df["firmware_version"].astype("string").str.strip().replace("", pd.NA)

    conflict = stated_fw.notna() & parsed_fw.notna() & (stated_fw != parsed_fw)
    if conflict.any():
        checks.append(
            Check(table, "firmware_label_conflict", "error", int(conflict.sum()),
                  "device_label firmware disagrees with firmware_version",
                  _pids(df.loc[conflict, "participant_id"]))
        )
    backfilled = stated_fw.isna() & parsed_fw.notna()
    if backfilled.any():
        checks.append(
            Check(table, "firmware_backfilled_from_label", "warn", int(backfilled.sum()),
                  "firmware_version blank; recovered by regex from device_label",
                  _pids(df.loc[backfilled, "participant_id"]))
        )

    df["firmware_version"] = stated_fw.fillna(parsed_fw)
    df["firmware_source"] = np.where(backfilled, "device_label", "firmware_version")
    df["device_model"] = label.str.split("·").str[0].str.strip()
    df["calibration_date"] = pd.to_datetime(df["calibration_date"], errors="coerce").dt.date

    late = pd.to_datetime(df["calibration_date"]) >= pd.Timestamp(C.STUDY_START)
    if late.any():
        checks.append(
            Check(table, "calibration_after_study_start", "error", int(late.sum()), "",
                  _pids(df.loc[late, "participant_id"]))
        )

    orphan = ~df["participant_id"].isin(participants["participant_id"])
    if orphan.any():
        checks.append(
            Check(table, "device_orphan_participant", "error", int(orphan.sum()), "",
                  _pids(df.loc[orphan, "participant_id"]))
        )

    cols = ["participant_id", "device_id", "wear_site", "calibration_date",
            "firmware_version", "firmware_source", "device_model", "device_label"]
    return df.loc[~orphan, cols].reset_index(drop=True), checks


def build_survey(
    bronze: pd.DataFrame, participants: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame, list[Check]]:
    table = "silver_wellness_survey"
    checks: list[Check] = []
    df = bronze.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    for col in ("fatigue_score", "stress_score", "readiness_score", "sleep_quality_score"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int16")

    ranges = {"fatigue_score": (1, 5), "stress_score": (1, 5),
              "readiness_score": (0, 10), "sleep_quality_score": (1, 5)}
    for col, (lo, hi) in ranges.items():
        bad = df[col].notna() & ((df[col] < lo) | (df[col] > hi))
        if bad.any():
            checks.append(Check(table, f"{col}_out_of_range", "error", int(bad.sum()), f"expected {lo}-{hi}"))

    orphan = ~df["participant_id"].isin(participants["participant_id"])
    if orphan.any():
        checks.append(
            Check(table, "survey_orphan_participant", "error", int(orphan.sum()),
                  "participant_id not in the roster; quarantined, not silently dropped",
                  _pids(df.loc[orphan, "participant_id"]))
        )
    dup = df.duplicated(["participant_id", "date"])
    if dup.any():
        checks.append(Check(table, "duplicate_survey_response", "error", int(dup.sum())))

    cols = ["participant_id", "date", "fatigue_score", "stress_score",
            "readiness_score", "sleep_quality_score"]
    clean = df.loc[~orphan & ~dup, cols].reset_index(drop=True)
    quarantined = df.loc[orphan, cols].assign(reject_reason="orphan_participant").reset_index(drop=True)
    return clean, quarantined, checks


# --------------------------------------------------------------------------
# Completeness on the participant-day x minute grid
# --------------------------------------------------------------------------
def build_coverage(
    participants: pd.DataFrame, steps: pd.DataFrame, hr: pd.DataFrame,
    sleep: pd.DataFrame, survey: pd.DataFrame,
) -> tuple[pd.DataFrame, list[Check]]:
    """The spine. Row counts lie; this is the honest completeness measure.

    Every participant x study-day exists here whether or not a single row
    arrived for it, so a whole missing day shows up as coverage 0 instead of
    silently vanishing from a GROUP BY.
    """
    checks: list[Check] = []
    dates = pd.date_range(C.STUDY_START, C.STUDY_END, freq="D").date
    grid = pd.MultiIndex.from_product(
        [sorted(participants["participant_id"]), dates], names=["participant_id", "date"]
    ).to_frame(index=False)

    def _minutes(df: pd.DataFrame, name: str) -> pd.DataFrame:
        return (
            df.groupby(["participant_id", "date"], observed=True)["timestamp"]
            .nunique().rename(name).reset_index()
        )

    cov = (
        grid.merge(_minutes(steps, "steps_minutes"), on=["participant_id", "date"], how="left")
        .merge(_minutes(hr, "hr_minutes"), on=["participant_id", "date"], how="left")
        .merge(
            sleep.groupby(["participant_id", "date"], observed=True).size().rename("sleep_sessions").reset_index(),
            on=["participant_id", "date"], how="left",
        )
        .merge(
            survey.groupby(["participant_id", "date"], observed=True).size().rename("survey_responses").reset_index(),
            on=["participant_id", "date"], how="left",
        )
    )
    for col in ("steps_minutes", "hr_minutes", "sleep_sessions", "survey_responses"):
        cov[col] = cov[col].fillna(0).astype("int32")

    cov["steps_coverage_pct"] = 100.0 * cov["steps_minutes"] / C.MINUTES_PER_DAY
    cov["hr_coverage_pct"] = 100.0 * cov["hr_minutes"] / C.MINUTES_PER_DAY
    cov["coverage_pct"] = cov[["steps_coverage_pct", "hr_coverage_pct"]].min(axis=1)
    cov["is_complete_day"] = cov["coverage_pct"] >= C.COMPLETE_DAY_MIN_COVERAGE_PCT
    cov["is_missing_day"] = cov["coverage_pct"] == 0

    n_missing = int(cov["is_missing_day"].sum())
    n_partial = int((~cov["is_complete_day"] & ~cov["is_missing_day"]).sum())
    if n_missing:
        checks.append(
            Check("silver_participant_day_coverage", "participant_day_missing", "error", n_missing,
                  "no minute-grain data arrived for this participant-day",
                  _pids(cov.loc[cov["is_missing_day"], "participant_id"]))
        )
    if n_partial:
        checks.append(
            Check("silver_participant_day_coverage", "participant_day_partial", "error", n_partial,
                  "minute grid incomplete (device off / outage); excluded from daily "
                  "aggregates via is_complete_day",
                  _pids(cov.loc[~cov["is_complete_day"] & ~cov["is_missing_day"], "participant_id"]))
        )
    checks.append(
        Check("silver_participant_day_coverage", "grid_size", "info", len(cov),
              f"{participants['participant_id'].nunique()} participants x {len(dates)} days")
    )
    missing_survey = int((cov["survey_responses"] == 0).sum())
    if missing_survey:
        checks.append(
            Check("silver_participant_day_coverage", "survey_missing_day", "warn", missing_survey)
        )
    return cov, checks
