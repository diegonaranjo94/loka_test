#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Loka data engineering test - wearable pipeline - the single entry point, run from the repo root.
#
#   ./poc_run_etl.sh                            local mirror -> local lake (DEFAULT)
#   ./poc_run_etl.sh --online                   S3 -> S3, the real run
#   ./poc_run_etl.sh --participants P001,P005,P018,P029             smoke run
#   ./poc_run_etl.sh --layers gold              rebuild Gold from published Silver
#   ./poc_run_etl.sh --strict                   exit 1 if a pipeline invariant fails
#   ./poc_run_etl.sh --test                     run the Silver rule unit tests
#   ./poc_run_etl.sh --env-info                 show the environment, run nothing
#   ./poc_run_etl.sh --recreate-env             tear the environment down and rebuild
#
# Local is the default; --online opts in to the S3 URIs in poc/.env. Reaching a
# shared bucket that holds participant-level data should be something you asked
# for, not what happens when you hit up-arrow.
#
# Configuration lives in poc/.env (URIs only, no secrets). Dependencies live in
# a standard virtualenv at ./.venv, built by `python3 -m venv` from
# requirements.txt. Created on the first run, reused on every run after.
#
# Reproducibility without a package manager: requirements.txt holds the direct
# dependencies with ranges; the first install resolves them and freezes the
# full transitive set to requirements.lock.txt, and every later install uses
# that lock verbatim. Edit requirements.txt to change a dependency - the script
# notices it is newer than the lock and re-resolves.
#
# Overrides:
#   PYTHON=python3.12     interpreter used to create the virtualenv
#   VENV_DIR=/path/.venv  where the virtualenv lives
#   ETL_RUNNER="..."      skip the virtualenv entirely and use this command
#                         (ETL_RUNNER="" for a container or an already active
#                         environment; used by CI)
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

POC_DIR="$ROOT/poc"
# The pipeline is a package under poc/ but is driven from the repo root, so put
# poc/ on the import path rather than cd-ing into it. Every path the code
# resolves is anchored to the package location, not to the working directory,
# so `python -m etl.run_etl` behaves identically from anywhere.
export PYTHONPATH="$POC_DIR${PYTHONPATH:+:$PYTHONPATH}"

VENV_DIR="${VENV_DIR:-$ROOT/.venv}"
VENV_NAME="${VENV_NAME:-loka_test}"     # shows up in the shell prompt when activated
PYTHON_BIN="${PYTHON:-python3}"
REQ="$ROOT/requirements.txt"
LOCK="$ROOT/requirements.lock.txt"
PY_MIN="3.10"                            # pandas 2.2 / pyarrow 16 / duckdb 1.x floor

if [[ ! -f "$POC_DIR/.env" ]]; then
  echo "!! poc/.env not found - copy poc/.env.example to poc/.env and set" >&2
  echo "   SOURCE_URI / TARGET_URI, then re-run." >&2
  exit 1
fi

# Mirror the pipeline's own --quiet so the bootstrap does not chatter over a
# deliberately silent run.
QUIET=0
for _arg in "$@"; do [[ "$_arg" == "--quiet" ]] && QUIET=1; done
say() { (( QUIET )) || echo "$@"; }

sha()   { shasum -a 256 "$1" 2>/dev/null || sha256sum "$1"; }
pyver() { "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo unknown; }

# --------------------------------------------------------------------------
# Resolve how we execute Python
# --------------------------------------------------------------------------
# RUNNER is always a non-empty array - an empty array under `set -u` is an
# unbound-variable error on the bash 3.2 that ships with macOS, which is
# exactly where this script runs.
if [[ -n "${ETL_RUNNER+x}" ]]; then
  # shellcheck disable=SC2206
  RUNNER=(${ETL_RUNNER} python)
  say ">> env: ETL_RUNNER override, virtualenv bootstrap skipped"
else
  VPY="$VENV_DIR/bin/python"

  # --- explicit teardown ----------------------------------------------------
  if [[ "${1:-}" == "--recreate-env" ]]; then
    shift
    if [[ -d "$VENV_DIR" ]]; then
      say ">> removing virtualenv at $VENV_DIR"
      rm -rf "$VENV_DIR"
    else
      say ">> no virtualenv at $VENV_DIR to remove"
    fi
  fi

  # A virtualenv whose base interpreter has been upgraded away (a Homebrew
  # python bump is enough) leaves a .venv that exists but cannot run. Detect
  # that and rebuild rather than failing with an obscure dyld error.
  if [[ -d "$VENV_DIR" && ! -x "$VPY" ]] || { [[ -x "$VPY" ]] && ! "$VPY" -c '' 2>/dev/null; }; then
    say ">> virtualenv at $VENV_DIR is broken (its base interpreter is gone) - rebuilding"
    rm -rf "$VENV_DIR"
  fi

  # --- create ---------------------------------------------------------------
  FRESH=0
  if [[ ! -x "$VPY" ]]; then
    if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
      echo "!! '$PYTHON_BIN' not found. Install Python >= $PY_MIN, or point at one:" >&2
      echo "   PYTHON=/opt/homebrew/bin/python3.12 ./poc_run_etl.sh $*" >&2
      exit 1
    fi
    HOST_PY="$(pyver "$PYTHON_BIN")"
    # Sort-based version compare: portable, and no arithmetic on a version
    # string that might be "unknown".
    if [[ "$HOST_PY" == "unknown" ]] || \
       [[ "$(printf '%s\n%s\n' "$PY_MIN" "$HOST_PY" | sort -t. -k1,1n -k2,2n | head -1)" != "$PY_MIN" ]]; then
      echo "!! $PYTHON_BIN is python $HOST_PY; this pipeline needs >= $PY_MIN." >&2
      echo "   Point at a newer one: PYTHON=python3.12 ./poc_run_etl.sh $*" >&2
      exit 1
    fi
    say ">> creating virtualenv '$VENV_NAME' at $VENV_DIR (python $HOST_PY)"
    "$PYTHON_BIN" -m venv --prompt "$VENV_NAME" "$VENV_DIR"
    "$VPY" -m pip install --quiet --upgrade pip
    FRESH=1
  fi

  # --- install ---------------------------------------------------------------
  # requirements.txt is the input (ranges); requirements.lock.txt is the
  # resolved output (exact). Re-resolve only when the input moves ahead of it.
  if [[ ! -f "$LOCK" || "$REQ" -nt "$LOCK" ]]; then
    say ">> resolving dependencies from requirements.txt"
    "$VPY" -m pip install --quiet --upgrade -r "$REQ"
    "$VPY" -m pip freeze --exclude-editable > "$LOCK"
    say ">> froze $(grep -c . "$LOCK") packages to requirements.lock.txt"
    sha "$LOCK" > "$VENV_DIR/.requirements.sha"
  else
    # The stamp lives inside the virtualenv, so deleting the virtualenv
    # invalidates it too - a stamp can never describe an environment that is
    # no longer there.
    WANT="$(sha "$LOCK")"
    HAVE="$(cat "$VENV_DIR/.requirements.sha" 2>/dev/null || true)"
    if (( FRESH )) || [[ "$WANT" != "$HAVE" ]]; then
      say ">> installing from requirements.lock.txt"
      "$VPY" -m pip install --quiet -r "$LOCK"
      printf '%s\n' "$WANT" > "$VENV_DIR/.requirements.sha"
    fi
  fi

  say ">> env: $VENV_NAME (python $(pyver "$VPY")) at $VENV_DIR $( ((FRESH)) && echo '[created]' || echo '[reused]' )"
  RUNNER=("$VPY")
fi

# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------
if [[ "${1:-}" == "--env-info" ]]; then
  # Import each module AND read its installed distribution version - a package
  # that imports but is not the version the lock pinned is exactly the drift
  # this command exists to surface. (Some distributions, python-dotenv among
  # them, expose no __version__, so the version comes from the metadata.)
  exec "${RUNNER[@]}" -c 'import sys, importlib, importlib.metadata as md
print("python      ", sys.version.split()[0])
print("interpreter ", sys.executable)
for mod, dist in [("pandas","pandas"), ("numpy","numpy"), ("pyarrow","pyarrow"),
                  ("duckdb","duckdb"), ("fsspec","fsspec"), ("s3fs","s3fs"),
                  ("boto3","boto3"), ("dotenv","python-dotenv"),
                  ("tabulate","tabulate"), ("pytest","pytest")]:
    try:
        importlib.import_module(mod)
        print("%-12s %s" % (dist, md.version(dist)))
    except Exception as exc:
        print("%-12s MISSING (%s)" % (dist, type(exc).__name__))'
fi

if [[ "${1:-}" == "--test" ]]; then
  shift
  exec "${RUNNER[@]}" -m pytest "$POC_DIR/tests" -q "$@"
fi

exec "${RUNNER[@]}" -m etl.run_etl "$@"
