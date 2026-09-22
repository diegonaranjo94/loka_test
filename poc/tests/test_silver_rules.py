"""Unit tests for the Silver defect rules.

Each test reproduces one defect from eda/findings.md in miniature, so the
rules are verifiable without a 387 MB download and a rule regression fails in
milliseconds rather than at the next full run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from etl import config as C  # noqa: E402
from etl import silver  # noqa: E402


def _steps(rows):
    return pd.DataFrame(rows, columns=["participant_id", "timestamp", "steps", "unit"])


def _hr(rows):
    return pd.DataFrame(rows, columns=["participant_id", "timestamp", "heart_rate_bpm"])


# --------------------------------------------------------------------------
# Rule 1 - timezone
# --------------------------------------------------------------------------
def test_z_suffixed_rows_are_pulled_back_to_study_local_time():
    df = _steps([
        ("P018", "2026-01-15T05:00:00Z", 10, None),   # 00:00 local
        ("P018", "2026-01-15T00:00:00", 10, None),    # the same minute, naive
    ])
    out, checks = silver.normalise_timestamps(df, "t")
    assert out["timestamp"].nunique() == 1, "UTC row must land on the same local minute"
    assert out["timestamp"].iloc[0] == pd.Timestamp("2026-01-15 00:00:00")
    assert any(c.rule == "timezone_normalised" and c.n_affected == 1 for c in checks)


def test_dedupe_only_works_after_timezone_normalisation():
    """The ordering constraint from findings.md, asserted rather than assumed."""
    df = _steps([
        ("P018", "2026-01-15T05:00:00Z", 10, None),
        ("P018", "2026-01-15T00:00:00", 10, None),
    ])
    # wrong order: dedupe on the raw strings sees two distinct keys
    naive_dupes = df.duplicated(["participant_id", "timestamp"]).sum()
    assert naive_dupes == 0
    # right order: normalise first, then dedupe collapses them
    out, _ = silver.normalise_timestamps(df, "t")
    out, checks = silver.deduplicate(out, ["participant_id", "timestamp"], "t")
    assert len(out) == 1
    assert any(c.rule == "duplicate_rows_removed" for c in checks)


# --------------------------------------------------------------------------
# Rule 2 - off-minute timestamps
# --------------------------------------------------------------------------
def test_off_minute_rows_snap_and_then_collapse():
    df = _steps([
        ("P005", "2026-01-09T10:00:00", 5, None),
        ("P005", "2026-01-09T10:00:37", 5, None),   # near-duplicate of the minute beside it
    ])
    out, checks = silver.normalise_timestamps(df, "t")
    assert (out["timestamp"].dt.second == 0).all()
    assert any(c.rule == "timestamp_snapped_to_minute" and c.n_affected == 1 for c in checks)
    out, _ = silver.deduplicate(out, ["participant_id", "timestamp"], "t")
    assert len(out) == 1


# --------------------------------------------------------------------------
# Rule 3 - units
# --------------------------------------------------------------------------
def test_steps_per_hour_is_divided_onto_the_minute_grid():
    df = _steps([
        ("P005", "2026-01-09T10:00:00", 600, "steps_per_hour"),
        ("P001", "2026-01-09T10:00:00", 12, None),
    ])
    out, checks = silver.rescale_units(df, "steps", "t")
    assert out.loc[0, "steps"] == 10, "600 steps/hour is 10 steps in this minute"
    assert out.loc[1, "steps"] == 12, "unlabelled rows are already per-minute"
    assert any(c.rule == "unit_rescaled" and c.n_affected == 1 for c in checks)


def test_unknown_unit_is_nulled_not_guessed():
    df = _steps([("P005", "2026-01-09T10:00:00", 600, "steps_per_fortnight")])
    out, checks = silver.rescale_units(df, "steps", "t")
    assert pd.isna(out.loc[0, "steps"])
    assert any(c.rule == "unit_unknown" and c.severity == "error" for c in checks)


def test_a_day_of_hourly_rates_does_not_inflate_the_daily_total():
    """The 60x regression, end to end on one participant-day."""
    minutes = pd.date_range("2026-01-09", periods=1440, freq="min")
    df = _steps([("P005", t.isoformat(), 60, "steps_per_hour") for t in minutes])
    out, _ = silver.build_steps(df)
    assert out["steps"].sum() == 1440, "60 steps/hour for 24h is 1 step per minute"


# --------------------------------------------------------------------------
# Rule 4 - sentinels and range
# --------------------------------------------------------------------------
@pytest.mark.parametrize("bad", C.STEPS_SENTINELS)
def test_step_sentinels_are_nulled_but_the_minute_survives(bad):
    df = _steps([("P029", "2026-01-09T10:00:00", bad, None)])
    out, checks = silver.null_invalid_values(df, "steps", "t", sentinels=C.STEPS_SENTINELS)
    assert pd.isna(out.loc[0, "steps"])
    assert len(out) == 1, "the minute is real even though the value is not"
    assert any(c.rule == "value_out_of_domain" for c in checks)


@pytest.mark.parametrize("bad", [-10, 999, 0])
def test_impossible_heart_rates_are_nulled(bad):
    df = _hr([("P037", "2026-01-09T10:00:00", bad)])
    out, _ = silver.null_invalid_values(df, "heart_rate_bpm", "t", valid_range=C.HR_VALID_RANGE)
    assert pd.isna(out.loc[0, "heart_rate_bpm"])


def test_plausible_heart_rate_is_untouched():
    df = _hr([("P001", "2026-01-09T10:00:00", 72)])
    out, checks = silver.null_invalid_values(df, "heart_rate_bpm", "t", valid_range=C.HR_VALID_RANGE)
    assert out.loc[0, "heart_rate_bpm"] == 72
    assert not checks


# --------------------------------------------------------------------------
# Rule 5 - duplicates
# --------------------------------------------------------------------------
def test_a_whole_duplicated_day_collapses_to_one_copy():
    minutes = pd.date_range("2026-01-10", periods=1440, freq="min")
    rows = [("P010", t.isoformat(), 3, None) for t in minutes]
    out, _ = silver.build_steps(_steps(rows + rows))
    assert len(out) == 1440
    assert out["steps"].sum() == 1440 * 3


# --------------------------------------------------------------------------
# Rule 6 - stuck sensor
# --------------------------------------------------------------------------
def test_flatline_is_flagged_not_deleted():
    run = pd.date_range("2026-01-09T04:44", periods=C.HR_STUCK_MIN_RUN_MINUTES + 5, freq="min")
    normal = pd.date_range("2026-01-09T12:00", periods=30, freq="min")
    df = _hr(
        [("P003", t, 60) for t in run] + [("P003", t, 60 + i % 7) for i, t in enumerate(normal)]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    out, checks = silver.flag_stuck_sensor(df, "heart_rate_bpm", "t")
    assert len(out) == len(df), "flagged, never deleted"
    assert out["is_stuck"].sum() == len(run)
    assert not out.loc[out["timestamp"].isin(normal), "is_stuck"].any()
    assert any(c.rule == "stuck_sensor_flagged" for c in checks)


def test_a_gap_breaks_a_run():
    """Identical values either side of a data gap are not one stuck run."""
    half = C.HR_STUCK_MIN_RUN_MINUTES - 5
    a = pd.date_range("2026-01-09T00:00", periods=half, freq="min")
    b = pd.date_range("2026-01-09T06:00", periods=half, freq="min")
    df = _hr([("P003", t, 60) for t in list(a) + list(b)])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    out, _ = silver.flag_stuck_sensor(df, "heart_rate_bpm", "t")
    assert not out["is_stuck"].any()


# --------------------------------------------------------------------------
# Sleep
# --------------------------------------------------------------------------
def _session(pid="P001", date="2026-01-08", stated_end=None, deep_stated=None):
    onset = pd.Timestamp(f"{date}T23:00:00") - pd.Timedelta(days=1)
    spans = [("light", 60), ("deep", 60), ("rem", 60), ("light", 60), ("awake", 20)]
    stages, t = [], onset
    for idx, (stage, mins) in enumerate(spans):
        end = t + pd.Timedelta(minutes=mins)
        stated = mins if not (stage == "deep" and deep_stated) else deep_stated
        stages.append({
            "session_key": f"{pid}|{date}", "participant_id": pid, "date": date,
            "stage_idx": idx, "stage": stage,
            "start_time": t.isoformat(timespec="milliseconds"),
            "end_time": end.isoformat(timespec="milliseconds"),
            "duration_min": stated,
        })
        t = end
    last_asleep_end = onset + pd.Timedelta(minutes=240)
    sessions = [{
        "session_key": f"{pid}|{date}", "participant_id": pid, "date": date,
        "sleep_onset": onset.isoformat(timespec="milliseconds"),
        "sleep_end": (stated_end or last_asleep_end).isoformat(timespec="milliseconds"),
        "efficiency_pct": 100.0, "restlessness": 0.1, "n_stages": len(spans),
    }]
    return pd.DataFrame(sessions), pd.DataFrame(stages)


def test_efficiency_comes_from_stages_not_from_the_source_field():
    s, st = _session()
    sessions, _, _, _ = silver.build_sleep(s, st)
    row = sessions.iloc[0]
    assert row["total_sleep_min"] == 240
    assert row["time_in_bed_min"] == 260, "time in bed runs to the end of the trailing awake stage"
    assert row["sleep_efficiency_pct"] == pytest.approx(100 * 240 / 260)
    assert row["efficiency_pct_stated"] == 100.0, "the unusable source value is kept for audit"
    assert row["sleep_efficiency_pct"] < row["efficiency_pct_stated"]


def test_deep_pct_uses_timestamps_and_an_inflated_duration_is_rejected():
    s, st = _session(deep_stated=99)   # stated 99, timestamps say 60
    sessions, _, quarantined, checks = silver.build_sleep(s, st)
    assert len(sessions) == 0
    assert quarantined.iloc[0]["reject_reason"] == "stage_duration_mismatch"
    assert any(c.rule == "stage_duration_mismatch" for c in checks)


def test_corrupt_sleep_end_is_quarantined():
    s, st = _session(stated_end=pd.Timestamp("2026-01-08T18:00:00"))
    sessions, _, quarantined, checks = silver.build_sleep(s, st)
    assert len(sessions) == 0
    assert quarantined.iloc[0]["reject_reason"] == "sleep_end_corrupt"
    assert any(c.rule == "sleep_end_corrupt" for c in checks)


def test_mid_sleep_hour_is_continuous_across_midnight():
    s, st = _session()
    sessions, _, _, _ = silver.build_sleep(s, st)
    mid = sessions.iloc[0]["mid_sleep_hour_signed"]
    assert -12 <= mid <= 12
    assert mid == pytest.approx(1.0), "23:00 -> 03:00 has a 01:00 midpoint"


# --------------------------------------------------------------------------
# Completeness
# --------------------------------------------------------------------------
def test_row_count_cannot_hide_a_missing_day():
    """P019's signature defect: a missing day plus a duplicated day nets to the
    nominal row count, so only the grid catches it."""
    day1 = pd.date_range("2026-01-09", periods=1440, freq="min")   # 01-08 never arrives
    day2 = pd.date_range("2026-01-10", periods=1440, freq="min")
    rows = ([("P019", t.isoformat(), 1, None) for t in day1]
            + [("P019", t.isoformat(), 1, None) for t in day2] * 2)
    assert len(rows) == 4320, "row count looks like three full days"

    steps, _ = silver.build_steps(_steps(rows))
    hr, _ = silver.build_heart_rate(
        _hr([(p, t, 60) for p, t, _s, _u in rows])
    )
    participants = pd.DataFrame({"participant_id": ["P019"]})
    empty = pd.DataFrame(columns=["participant_id", "date", "timestamp"])
    cov, checks = silver.build_coverage(participants, steps, hr, empty, empty)

    assert len(cov) == C.STUDY_DAYS
    missing = cov.loc[cov["is_missing_day"], "date"].astype(str).tolist()
    assert "2026-01-08" in missing
    assert int(cov["is_complete_day"].sum()) == 2
    assert any(c.rule == "participant_day_missing" for c in checks)
