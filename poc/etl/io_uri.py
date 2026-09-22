"""URI-agnostic IO.

One rule: nothing downstream of this module knows whether it is talking to S3
or to a local directory. Everything goes through fsspec, which dispatches
`s3://` to boto3 and a bare path to the local filesystem, so the same
transform code is exercised by the offline test run and the real S3 run.

The second job of this module is the *write contract* that makes the pipeline
idempotent:

  <base>/<table>/[<col>=<val>/...]/part-0.parquet

A fixed `part-0` filename plus a delete-the-partition-then-write step means
re-running produces byte-for-byte the same object set. PyArrow's default
`write_to_dataset` naming (a fresh UUID per file) would instead *accumulate*
duplicate row sets on every re-run, which is exactly the failure mode the
brief calls out.
"""

from __future__ import annotations

import io
import json
import logging
import os
from typing import Any, Iterable, Sequence

import fsspec
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger(__name__)

_DATA_FILE = "part-0.parquet"


# --------------------------------------------------------------------------
# URI helpers
# --------------------------------------------------------------------------
def join(base: str, *parts: str) -> str:
    out = base.rstrip("/")
    for p in parts:
        p = str(p).strip("/")
        if p:
            out = f"{out}/{p}"
    return out


def _storage_options(uri: str) -> dict:
    """Backend options. Only S3 needs any."""
    if not uri.startswith("s3://"):
        return {}
    opts: dict[str, Any] = {
        # fsspec caches directory listings per filesystem instance. This
        # pipeline deletes a partition prefix and immediately writes into it,
        # so a stale listing is a *correctness* bug rather than a performance
        # trade-off: `exists()` could still report the deleted objects and a
        # re-run could skip an overwrite. Listing costs nothing at this scale
        # (hundreds of objects), so the cache is off.
        "use_listings_cache": False,
    }
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if region:
        opts["client_kwargs"] = {"region_name": region}
    # Only pass a profile if one is actually named. An empty string here means
    # "look up the profile called ''", which botocore reports as
    # ProfileNotFound - see config._drop_blank_aws_vars.
    profile = (os.getenv("AWS_PROFILE") or "").strip()
    if profile:
        opts["profile"] = profile
    return opts


def get_fs(uri: str):
    """Return (filesystem, path) for any URI. `s3://` needs s3fs installed."""
    fs, path = fsspec.core.url_to_fs(uri, **_storage_options(uri))
    return fs, path


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------
def check_readable(uri: str, label: str) -> None:
    """Fail in a second, not after downloading 387 MB."""
    try:
        fs, path = get_fs(uri)
        if not fs.exists(path):
            raise FileNotFoundError(path)
        fs.ls(path, detail=False)
    except ImportError as exc:  # s3fs not installed
        raise SystemExit(f"!! {label} {uri}: {exc}") from exc
    except Exception as exc:
        raise SystemExit(
            f"!! cannot read {label} {uri}\n"
            f"   {type(exc).__name__}: {exc}\n"
            "   For s3:// check credentials (`aws sts get-caller-identity`) and the region."
        ) from exc


def check_writable(uri: str, label: str, run_id: str) -> None:
    """Round-trip a marker object. Listing a bucket does not prove write access,
    and finding that out after the whole ingest has run is the expensive way."""
    probe = join(uri, "_ops", "preflight.txt")
    try:
        write_text(f"run_id={run_id}\n", probe)
        fs, path = get_fs(probe)
        fs.rm(path)
    except Exception as exc:
        raise SystemExit(
            f"!! cannot write to {label} {uri}\n"
            f"   {type(exc).__name__}: {exc}\n"
            "   For s3:// the caller needs s3:PutObject/s3:DeleteObject on this bucket."
        ) from exc


def exists(uri: str) -> bool:
    fs, path = get_fs(uri)
    return fs.exists(path)


def list_dirs(uri: str) -> list[str]:
    """Immediate subdirectory names under `uri` (sorted)."""
    fs, path = get_fs(uri)
    if not fs.exists(path):
        return []
    names = []
    for entry in fs.ls(path, detail=True):
        if entry.get("type") == "directory":
            names.append(entry["name"].rstrip("/").rsplit("/", 1)[-1])
    return sorted(names)


# --------------------------------------------------------------------------
# Readers
# --------------------------------------------------------------------------
def read_json(uri: str) -> Any:
    fs, path = get_fs(uri)
    with fs.open(path, "rb") as fh:
        return json.load(fh)


def read_csv(uri: str, **kwargs) -> pd.DataFrame:
    fs, path = get_fs(uri)
    with fs.open(path, "rb") as fh:
        return pd.read_csv(io.BytesIO(fh.read()), **kwargs)


def read_table(base: str, table: str) -> pd.DataFrame:
    """Read every part file of a (possibly partitioned) table back into pandas.

    Hive partition columns are recovered from the key path, so a table written
    with `partition_cols=["date"]` round-trips with its `date` column intact.
    """
    root = join(base, table)
    fs, path = get_fs(root)
    if not fs.exists(path):
        raise FileNotFoundError(f"no such table: {root}")

    files = sorted(fs.glob(join(path, "**", _DATA_FILE)))
    if not files:
        files = sorted(fs.glob(join(path, _DATA_FILE)))
    if not files:
        raise FileNotFoundError(f"table {root} has no {_DATA_FILE} files")

    frames = []
    for f in files:
        with fs.open(f, "rb") as fh:
            df = pq.read_table(fh).to_pandas()
        for segment in f[len(path):].strip("/").split("/"):
            if "=" in segment:
                col, _, val = segment.partition("=")
                if col not in df.columns:
                    df[col] = val
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]


# --------------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------------
def _overwrite_prefix(fs, path: str) -> None:
    """Clear a partition directory so a re-run replaces rather than appends.

    Falling back to an in-place overwrite is safe because every partition
    holds exactly one deterministically named object: if the delete is refused
    (a read-only or delete-restricted mount), re-writing `part-0.parquet` still
    replaces the partition's contents rather than adding to it.
    """
    if not fs.exists(path):
        return
    try:
        fs.rm(path, recursive=True)
    except (PermissionError, OSError) as exc:
        log.debug("cannot clear %s (%s); overwriting in place", path, exc)


def _write_parquet(fs, path: str, table: pa.Table, compression: str) -> None:
    parent = path.rsplit("/", 1)[0]
    try:
        fs.makedirs(parent, exist_ok=True)
    except (NotImplementedError, FileExistsError):
        pass  # object stores have no real directories
    with fs.open(path, "wb") as fh:
        pq.write_table(table, fh, compression=compression)


def write_table(
    df: pd.DataFrame,
    base: str,
    table: str,
    partition_cols: Sequence[str] = (),
    compression: str = "zstd",
) -> int:
    """Write `df` to <base>/<table>, overwriting only the partitions touched.

    Returns the number of objects written. Partitioning to an empty tuple
    writes a single deterministic object.
    """
    root = join(base, table)
    fs, root_path = get_fs(root)
    partition_cols = [c for c in partition_cols if c in df.columns]

    if not partition_cols:
        _overwrite_prefix(fs, root_path)
        _write_parquet(
            fs, join(root_path, _DATA_FILE), pa.Table.from_pandas(df, preserve_index=False), compression
        )
        return 1

    written = 0
    for keys, group in df.groupby(partition_cols, observed=True, sort=True, dropna=False):
        keys = keys if isinstance(keys, tuple) else (keys,)
        segments = [f"{col}={_partition_value(val)}" for col, val in zip(partition_cols, keys)]
        part_dir = join(root_path, *segments)
        _overwrite_prefix(fs, part_dir)
        payload = group.drop(columns=list(partition_cols))
        _write_parquet(
            fs, join(part_dir, _DATA_FILE), pa.Table.from_pandas(payload, preserve_index=False), compression
        )
        written += 1
    return written


def _partition_value(val: Any) -> str:
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d")
    return str(val)


def write_text(text: str, uri: str) -> None:
    fs, path = get_fs(uri)
    parent = path.rsplit("/", 1)[0]
    try:
        fs.makedirs(parent, exist_ok=True)
    except (NotImplementedError, FileExistsError):
        pass
    with fs.open(path, "w") as fh:
        fh.write(text)
