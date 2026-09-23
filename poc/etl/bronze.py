"""
Ingest data from remote bucket to own bucket

No parsing, no cleaning, no business logic
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Iterable

import pandas as pd

from . import config as C
from . import io_uri as io
from .silver import Check

log = logging.getLogger(__name__)

LINEAGE = ["_source_uri", "_ingested_at", "_run_id"]


def _stamp(df: pd.DataFrame, source_uri: str, run_id: str, ingested_at: pd.Timestamp) -> pd.DataFrame:
    """
    Add lineage columns to a DataFrame. The source_uri is the object that was read,
    run_id is the current ETL run,
    and ingested_at is the timestamp of this ingestion.
    """
    df = df.copy()
    df["_source_uri"] = pd.array([source_uri] * len(df), dtype="string[pyarrow]")
    df["_ingested_at"] = ingested_at
    df["_run_id"] = pd.array([run_id] * len(df), dtype="string[pyarrow]")
    return df


def discover_participants(source_uri: str) -> list[str]:
    """Participant folders present in the export."""
    return io.list_dirs(io.join(source_uri, C.SRC_EVENTS_DIR))


def _read_event_file(source_uri: str, pid: str, kind: str) -> tuple[list, str]:
    uri = io.join(source_uri, C.SRC_EVENTS_DIR, pid, C.SRC_EVENT_FILES[kind])
    return io.read_json(uri), uri


def _explode_sleep(records: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """Split nested sleep JSON into a session row and its stage rows."""
    sessions, stages = [], []
    for rec in records:
        session_key = f"{rec.get('participant_id')}|{rec.get('date')}"
        sessions.append(
            {
                "session_key": session_key,
                "participant_id": rec.get("participant_id"),
                "date": rec.get("date"),
                "sleep_onset": rec.get("sleep_onset"),
                "sleep_end": rec.get("sleep_end"),
                "efficiency_pct": rec.get("efficiency_pct"),
                "restlessness": rec.get("restlessness"),
                "n_stages": len(rec.get("stages") or []),
            }
        )
        for idx, st in enumerate(rec.get("stages") or []):
            stages.append(
                {
                    "session_key": session_key,
                    "participant_id": rec.get("participant_id"),
                    "date": rec.get("date"),
                    "stage_idx": idx,
                    "stage": st.get("stage"),
                    "start_time": st.get("start_time"),
                    "end_time": st.get("end_time"),
                    "duration_min": st.get("duration_min"),
                }
            )
    return sessions, stages


def ingest(
    settings: C.Settings,
    run_id: str,
    participants: list[str] | None = None,
    persist: bool = True,
) -> tuple[dict[str, pd.DataFrame], list[Check]]:
    """Read every source object into memory and land it as Bronze Parquet."""
    src, tgt = settings.source_uri, settings.target_uri
    ingested_at = pd.Timestamp.utcnow().tz_localize(None)

    pids = participants or discover_participants(src)
    log.info("bronze: %d participant folders under %s", len(pids), src)

    checks: list[Check] = []
    undocumented: dict[str, set[str]] = defaultdict(set)
    undocumented_pids: dict[str, set[str]] = defaultdict(set)
    steps_parts, hr_parts, sess_parts, stage_parts = [], [], [], []
    for n, pid in enumerate(pids, 1):
        for kind, bucket in (("steps", steps_parts), ("heart_rate", hr_parts)):
            recs, uri = _read_event_file(src, pid, kind)
            df = pd.DataFrame.from_records(recs)
            # Whatever arrives, arrives - including fields the data dictionary
            # never mentions. They are recorded as a check below, not dropped.
            extra = set(df.columns) - C.DOCUMENTED_FIELDS.get(kind, set())
            if extra:
                undocumented[kind].update(extra)
                undocumented_pids[kind].add(pid)
            # Arrow-backed strings. 2M timestamps as Python str objects is most
            # of this pipeline's peak memory; as an Arrow string array it is a
            # contiguous buffer roughly 5x smaller.
            for col in ("timestamp", "participant_id", *C.UNIT_COLUMN_CANDIDATES):
                if col in df.columns:
                    df[col] = df[col].astype("string[pyarrow]")
            df["_source_participant_dir"] = pid
            bucket.append(_stamp(df, uri, run_id, ingested_at))

        recs, uri = _read_event_file(src, pid, "sleep")
        sessions, stages = _explode_sleep(recs)
        sess_parts.append(_stamp(pd.DataFrame(sessions), uri, run_id, ingested_at))
        stage_parts.append(_stamp(pd.DataFrame(stages), uri, run_id, ingested_at))

        if n % 10 == 0 or n == len(pids):
            log.info("bronze: read %d/%d participants", n, len(pids))

    for kind, fields in undocumented.items():
        checks.append(
            Check(f"bronze_{kind}", "undocumented_source_field", "error",
                  len(undocumented_pids[kind]),
                  f"field(s) {sorted(fields)} are not in the data dictionary; "
                  "landed verbatim and interpreted in Silver",
                  ", ".join(sorted(undocumented_pids[kind])[:12]))
        )

    tables: dict[str, pd.DataFrame] = {
        "bronze_steps": pd.concat(steps_parts, ignore_index=True),
        "bronze_heart_rate": pd.concat(hr_parts, ignore_index=True),
        "bronze_sleep_sessions": pd.concat(sess_parts, ignore_index=True),
        "bronze_sleep_stages": pd.concat(stage_parts, ignore_index=True),
    }

    for name, rel in (
        ("bronze_participants", C.SRC_PARTICIPANTS),
        ("bronze_device_metadata", C.SRC_DEVICE_METADATA),
        ("bronze_wellness_survey", C.SRC_WELLNESS_SURVEY),
    ):
        uri = io.join(src, rel)
        tables[name] = _stamp(io.read_csv(uri, dtype=str), uri, run_id, ingested_at)

    if persist:
        partitioned = {
            "bronze_steps": ["_source_participant_dir"],
            "bronze_heart_rate": ["_source_participant_dir"],
            "bronze_sleep_sessions": ["participant_id"],
            "bronze_sleep_stages": ["participant_id"],
        }
        for name, df in tables.items():
            n_obj = io.write_table(
                df,
                io.join(tgt, C.BRONZE),
                name,
                partition_cols=partitioned.get(name, ()),
                compression=settings.compression,
            )
            log.info("bronze: wrote %-24s %9d rows -> %3d object(s)", name, len(df), n_obj)

    return tables, checks
