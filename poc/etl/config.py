"""Central configuration.

Every URI comes from poc/.env (or the environment, or the CLI) - nothing is
hardcoded in the transform code. Constants that encode a *decision about the
data* (sentinel values, the study window, the local timezone) live here too, so
a reviewer can find every magic number in one file and every one of them traces
back to a line in eda/findings.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
POC_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = POC_DIR.parent
SQL_DIR = POC_DIR / "sql"

# poc/.env is the canonical location; a repo-root .env is accepted as a fallback
# so the pipeline behaves the same whether it is driven from the root (the
# normal case, via ./poc_run_etl.sh) or from inside poc/. First file wins -
# load_dotenv does not override values that are already set.
load_dotenv(POC_DIR / ".env")
load_dotenv(REPO_DIR / ".env")


def _drop_blank_aws_vars() -> None:
    """Remove AWS_* variables that .env left set-but-empty.

    `AWS_PROFILE=` in a .env file is the natural way to write "no profile,
    use the default credential chain". python-dotenv faithfully sets
    os.environ["AWS_PROFILE"] = "", and botocore keys off the variable's
    *presence*, not its value - so it looks for a profile literally named
    empty string and dies with:

        ProfileNotFound: The config profile () could not be found

    ...on a machine where `aws s3 sync` works perfectly, because the CLI never
    reads this .env. Unset rather than blank is the only thing botocore reads
    as "no profile".
    """
    for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_REGION",
                "AWS_DEFAULT_REGION", "AWS_ENDPOINT_URL"):
        if var in os.environ and not os.environ[var].strip().strip("'\""):
            del os.environ[var]


_drop_blank_aws_vars()


# --------------------------------------------------------------------------
# Study constants (data_dictionary.pdf)
# --------------------------------------------------------------------------
STUDY_START = "2026-01-08"
STUDY_END = "2026-02-04"
STUDY_DAYS = 28
EXPECTED_PARTICIPANTS = 50
MINUTES_PER_DAY = 1440

# The export is naive local time. A subset of heart_rate rows arrives as
# Z-suffixed UTC (findings.md: P018, P044) and has to be pulled back to the
# study's local clock before anything else touches it.
STUDY_TZ = timezone(timedelta(hours=-5))
STUDY_TZ_LABEL = "UTC-05:00"


# --------------------------------------------------------------------------
# Defect rules (each traces to a bullet in eda/findings.md)
# --------------------------------------------------------------------------

# Impossible step values used as null markers by the exporter.
STEPS_SENTINELS = (-1, 1_000_000)

# Physiologically impossible heart rates. Catches the -10 / 999 sentinels
# without hardcoding them, so a new sentinel value is caught too.
HR_VALID_RANGE = (20, 250)

# The unit field is undocumented and does not even have a stable name: the
# export ships it as `_unit` on P005/P033/P038 and omits it everywhere else.
# Resolve it by candidate rather than by a hardcoded name, so a rename in the
# next export does not silently reintroduce the 60x inflation.
UNIT_COLUMN_CANDIDATES = ("unit", "_unit", "units", "_units")

# Fields the data dictionary documents. Anything else arriving in the export is
# surfaced as a check rather than ignored - an undocumented field is how the
# unit bug got in, and the next one will arrive the same way.
DOCUMENTED_FIELDS = {
    "steps": {"timestamp", "steps", "participant_id"},
    "heart_rate": {"timestamp", "heart_rate_bpm", "participant_id"},
}

# The values are hourly rates sitting on a per-minute grid, so they must be
# divided down before any sum. Any unit seen that is NOT in this map is a hard
# failure - we refuse to guess.
UNIT_RESCALE_DIVISOR = {
    "steps_per_minute": 1,
    "steps": 1,
    "steps_per_hour": 60,
}

# A real heart rate does not repeat the identical integer for this many
# consecutive minutes. Flag, do not delete: the minutes are real, the values
# are not.
HR_STUCK_MIN_RUN_MINUTES = 60

# Tolerance when comparing a stated timestamp against one derived from the
# stage array. Anything beyond this is treated as corrupt, not as rounding.
SLEEP_ANCHOR_TOLERANCE_SECONDS = 60

# A participant-day is "complete" only if the minute grid is full. Row counts
# lie (findings.md: duplicate days cancel out missing days), so completeness is
# always measured against 1440.
COMPLETE_DAY_MIN_COVERAGE_PCT = 99.9

# Canonical stage vocabulary.
SLEEP_STAGES = ("light", "deep", "rem", "awake")
NON_SLEEP_STAGES = ("awake",)

# firmware_version is blank on some devices but always recoverable from the
# free-text device_label.
FIRMWARE_REGEX = r"fw(\d+\.\d+\.\d+)"
DEVICE_SERIAL_REGEX = r"SN:([A-Z]{3}-\d{4}-[0-9A-F]{8})"
PARTICIPANT_ID_REGEX = r"^P\d{3}$"
DEVICE_ID_REGEX = r"^FBT-\d{4}-[0-9A-F]{8}$"


# --------------------------------------------------------------------------
# Lake layout
# --------------------------------------------------------------------------
BRONZE = "bronze"
SILVER = "silver"
GOLD = "gold"
QUARANTINE = "_quarantine"
DQ = "_dq"

# Source object layout inside SOURCE_URI.
SRC_PARTICIPANTS = "participants/participants.csv"
SRC_DEVICE_METADATA = "health_summaries/device_metadata.csv"
SRC_WELLNESS_SURVEY = "health_summaries/wellness_survey.csv"
SRC_EVENTS_DIR = "wearable_events"
SRC_EVENT_FILES = {
    "steps": "steps.json",
    "heart_rate": "heart_rate.json",
    "sleep": "sleep.json",
}


# --------------------------------------------------------------------------
# Runtime settings
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    source_uri: str
    target_uri: str
    output_dir: Path
    compression: str
    aws_region: str
    aws_profile: str | None
    online: bool = False

    @property
    def is_remote_target(self) -> bool:
        return "://" in self.target_uri and not self.target_uri.startswith("file://")

    @property
    def mode(self) -> str:
        return "online (remote URIs from .env)" if self.online else "offline (local mirror)"


def _resolve(uri: str) -> str:
    """Leave real URIs alone; anchor bare/relative paths at poc/."""
    uri = uri.strip().rstrip("/")
    if "://" in uri:
        return uri
    path = Path(uri).expanduser()
    if not path.is_absolute():
        path = (POC_DIR / path).resolve()
    return str(path)


def load_settings(
    source: str | None = None,
    target: str | None = None,
    online: bool = False,
) -> Settings:
    """CLI argument > .env > built-in default.

    The local mirror is the default and `--online` opts in to the remote URIs.
    Defaulting to local is a safety choice as much as a convenience one: a
    mistyped or reflexive run cannot spend money, cannot mutate a shared
    bucket, and cannot publish participant-level data. Reaching S3 should be
    a thing you asked for.

    Either way it is the same code path - fsspec dispatches on the URI scheme,
    which is what makes the transforms testable with no S3 egress at all.
    """
    if online:
        # *_REMOTE is the current spelling; the bare names are the older one
        # and still honoured so an existing .env keeps working.
        env_source = os.getenv("SOURCE_URI_REMOTE") or os.getenv("SOURCE_URI", "")
        env_target = os.getenv("TARGET_URI_REMOTE") or os.getenv("TARGET_URI", "")
        missing = [
            name for name, val in (("SOURCE_URI_REMOTE", env_source),
                                   ("TARGET_URI_REMOTE", env_target)) if not val
        ]
        if missing and not (source and target):
            raise SystemExit(
                f"!! --online needs {' and '.join(missing)} set in poc/.env "
                "(or pass --source/--target explicitly)."
            )
    else:
        env_source = os.getenv("SOURCE_URI_LOCAL", "./data/raw")
        env_target = os.getenv("TARGET_URI_LOCAL", "./data/lake")

    _drop_blank_aws_vars()   # again: a caller may have re-loaded a .env
    profile = (os.getenv("AWS_PROFILE") or "").strip() or None
    if profile:
        os.environ["AWS_PROFILE"] = profile
    else:
        os.environ.pop("AWS_PROFILE", None)
    region = (os.getenv("AWS_REGION") or "us-east-1").strip()
    os.environ.setdefault("AWS_DEFAULT_REGION", region)

    return Settings(
        source_uri=_resolve(source or env_source),
        target_uri=_resolve(target or env_target),
        output_dir=Path(_resolve(os.getenv("OUTPUT_DIR", "./outputs"))),
        compression=os.getenv("PARQUET_COMPRESSION", "zstd"),
        aws_region=region,
        aws_profile=profile,
        online=online,
    )
