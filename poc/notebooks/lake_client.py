"""Thin data-access helper for browsing the lake from a notebook.

One constant controls where every read in this module (and in
explore_lake.py) comes from:

    LAKE_SOURCE = "local"   # poc/data/lake — the offline mirror, no AWS calls
    LAKE_SOURCE = "s3"      # the bucket in poc/.env (TARGET_URI_REMOTE)

Flip it, re-run a cell, done — nothing else in the notebook needs to change.

This deliberately does not reimplement any IO: it calls the exact same
etl.config.load_settings() / etl.io_uri.read_table() the pipeline itself
uses, so a notebook read is guaranteed to see the same tables, at the same
paths, that poc_run_etl.sh produced — local and S3 are the same code path
there too (fsspec dispatches on the URI scheme).
"""

from __future__ import annotations

import sys
from pathlib import Path

# poc/notebooks/ -> poc/ on sys.path, so `from etl import ...` resolves the
# same way it does for poc_run_etl.sh, regardless of the notebook's cwd.
_POC_DIR = Path(__file__).resolve().parent.parent
if str(_POC_DIR) not in sys.path:
    sys.path.insert(0, str(_POC_DIR))

import pandas as pd

from etl import config as C
from etl import io_uri as io

# --------------------------------------------------------------------------
LAKE_SOURCE = "s3"  # "local" | "s3" -- the one thing to flip in this file
# --------------------------------------------------------------------------


def _settings() -> C.Settings:
    if LAKE_SOURCE not in ("local", "s3"):
        raise ValueError(f"LAKE_SOURCE must be 'local' or 's3', got {LAKE_SOURCE!r}")
    return C.load_settings(online=(LAKE_SOURCE == "s3"))


def list_tables(layer: str) -> list[str]:
    """Table names published under a layer: bronze | silver | gold | _quarantine | _dq."""
    settings = _settings()
    return io.list_dirs(io.join(settings.target_uri, layer))


def load_table(layer: str, name: str) -> pd.DataFrame:
    """Read one published table back into pandas, wherever LAKE_SOURCE points."""
    settings = _settings()
    return io.read_table(io.join(settings.target_uri, layer), name)


def describe() -> None:
    """Print the resolved mode/URI and every table currently published, per layer."""
    settings = _settings()
    print(f"LAKE_SOURCE = {LAKE_SOURCE!r}  ->  {settings.mode}")
    print(f"target_uri  = {settings.target_uri}\n")
    for layer in (C.BRONZE, C.SILVER, C.GOLD, C.QUARANTINE, C.DQ):
        tables = list_tables(layer)
        label = ", ".join(tables) if tables else "(nothing published yet)"
        print(f"  {layer:<12} {len(tables)} table(s): {label}")
