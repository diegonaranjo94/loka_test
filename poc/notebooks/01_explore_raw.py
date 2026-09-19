# %% [markdown]
# # 01 — Raw data exploration (Meridian wearable study)
#
# **Goal of this notebook:** land eyes on every raw source *before* designing any layer or schema.
# Nothing is cleaned here — this notebook only *profiles* and *flags*. The output is a findings
# table (`poc/outputs/raw_findings.csv`) that drives the Bronze→Silver contract later.
#
# Sources (per the data dictionary, 50 participants × 28 days, 2026-01-08 → 2026-02-04):
#
# | path | grain | format |
# |---|---|---|
# | `data/participants/participants.csv` | 1 row / participant | CSV |
# | `data/health_summaries/device_metadata.csv` | 1 row / device | CSV |
# | `data/health_summaries/wellness_survey.csv` | 1 row / participant / day | CSV |
# | `data/wearable_events/{pid}/sleep.json` | 1 obj / night (nested `stages[]`) | JSON |
# | `data/wearable_events/{pid}/steps.json` | 1 obj / minute | JSON |
# | `data/wearable_events/{pid}/heart_rate.json` | 1 obj / minute | JSON |
#
# **Prereq:** run `bash poc/scripts/pull_data.sh` on a machine with AWS credentials first.
#
# Conventions used below:
# - `flag(...)` appends to a global `FINDINGS` list — every anomaly ends up in one table.
# - Nothing is mutated in place; raw frames keep their original dtypes (timestamps stay **strings**
#   until we have explicitly inspected their formats).

# %%
from __future__ import annotations

import json
import re
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
pd.set_option("display.max_columns", 60)
pd.set_option("display.width", 200)
pd.set_option("display.max_rows", 120)


def _resolve_poc() -> Path:
    """Works whether the kernel cwd is poc/notebooks or the repo root."""
    here = Path.cwd()
    for cand in [here, *here.parents]:
        if (cand / "data" / "raw").exists() and cand.name == "poc":
            return cand
        if (cand / "poc" / "data" / "raw").exists():
            return cand / "poc"
    raise FileNotFoundError("could not locate poc/data/raw — run poc/scripts/pull_data.sh first")


POC = _resolve_poc()
RAW = POC / "data" / "raw"
# the sync may or may not keep the leading data/ prefix; normalise
if (RAW / "data").is_dir() and not (RAW / "participants").is_dir():
    RAW = RAW / "data"
INTERIM = POC / "data" / "interim"
OUT = POC / "outputs"
INTERIM.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

print("python :", sys.version.split()[0], "| pandas:", pd.__version__)
print("POC    :", POC)
print("RAW    :", RAW)
print("exists :", RAW.exists(), "| top level:", sorted(p.name for p in RAW.iterdir()) if RAW.exists() else [])

# %%
# --- findings collector -------------------------------------------------------------------
FINDINGS: list[dict] = []


def flag(source: str, check: str, severity: str, n: int, detail: str = "") -> None:
    """Record one profiling observation. severity: info | warn | error"""
    FINDINGS.append(
        {"source": source, "check": check, "severity": severity, "n_rows": int(n), "detail": str(detail)[:400]}
    )
    if severity != "info":
        print(f"  [{severity.upper():5}] {source:22} {check:38} n={n:<9} {detail}")


def profile(df: pd.DataFrame, name: str, key: list[str] | None = None) -> pd.DataFrame:
    """Compact per-column profile + structural checks pushed into FINDINGS."""
    print(f"\n=== {name} ===  shape={df.shape}  mem={df.memory_usage(deep=True).sum()/1e6:.1f} MB")
    rows = []
    for c in df.columns:
        s = df[c]
        nn = s.notna().sum()
        row = {
            "column": c,
            "dtype": str(s.dtype),
            "non_null": nn,
            "null_pct": round(100 * (1 - nn / max(len(s), 1)), 2),
            "n_unique": s.nunique(dropna=True),
        }
        if pd.api.types.is_numeric_dtype(s):
            row |= {"min": s.min(), "max": s.max(), "mean": round(float(s.mean()), 2) if nn else np.nan}
        else:
            vals = s.dropna().astype(str)
            row |= {"min": vals.min() if nn else None, "max": vals.max() if nn else None,
                    "mean": vals.value_counts().index[0] if nn else None}
        rows.append(row)
        if s.isna().any():
            flag(name, f"nulls in `{c}`", "warn", s.isna().sum(), f"{row['null_pct']}% of rows")
        if s.dtype == object:
            raw = s.dropna().astype(str)
            ws = (raw != raw.str.strip()).sum()
            if ws:
                flag(name, f"leading/trailing whitespace in `{c}`", "warn", ws)
    prof = pd.DataFrame(rows).rename(columns={"mean": "mean_or_mode"})

    dup_all = df.duplicated().sum()
    if dup_all:
        flag(name, "fully duplicated rows", "error", dup_all)
    if key:
        dup_key = df.duplicated(subset=key).sum()
        if dup_key:
            flag(name, f"duplicate business key {key}", "error", dup_key)
        else:
            flag(name, f"business key {key} unique", "info", 0)
    return prof


def range_check(df: pd.DataFrame, name: str, col: str, lo, hi, severity="error") -> None:
    s = pd.to_numeric(df[col], errors="coerce")
    bad = s.notna() & ((s < lo) | (s > hi))
    if bad.any():
        flag(name, f"`{col}` outside [{lo},{hi}]", severity, bad.sum(),
             f"offending values: {sorted(s[bad].unique())[:12]}")
    nonnum = df[col].notna() & s.isna()
    if nonnum.any():
        flag(name, f"`{col}` non-numeric values", "error", nonnum.sum(),
             f"examples: {df.loc[nonnum, col].astype(str).unique()[:8].tolist()}")


# %% [markdown]
# ## 0 — File inventory
# What actually landed, how big it is, and whether the folder layout matches the dictionary.

# %%
files = []
for p in sorted(RAW.rglob("*")):
    if p.is_file():
        rel = p.relative_to(RAW)
        files.append({"path": str(rel), "top": rel.parts[0], "name": p.name,
                      "ext": p.suffix.lower(), "size_mb": round(p.stat().st_size / 1e6, 3)})
inv = pd.DataFrame(files)
print(f"total files: {len(inv)}   total size: {inv.size_mb.sum():.1f} MB\n")
display(inv.groupby(["top", "ext"]).agg(n_files=("path", "size"),
                                        size_mb=("size_mb", "sum"),
                                        min_mb=("size_mb", "min"),
                                        max_mb=("size_mb", "max")).sort_values("size_mb", ascending=False))
display(inv[inv.top != "wearable_events"])

# participant folders + per-participant file completeness
pdirs = sorted([d for d in (RAW / "wearable_events").iterdir() if d.is_dir()])
print(f"\nparticipant folders under wearable_events: {len(pdirs)}  -> {[d.name for d in pdirs[:5]]} ... {[d.name for d in pdirs[-2:]]}")
stems = (inv[inv.top == "wearable_events"]
         .assign(pid=lambda d: d.path.str.split("/").str[1], stem=lambda d: d.name.str.replace(".json", "", regex=False)))
display(stems.pivot_table(index="stem", values="size_mb", aggfunc=["count", "sum", "mean"]).round(2))
missing = stems.pivot_table(index="pid", columns="stem", values="size_mb", aggfunc="size").fillna(0)
if (missing == 0).any().any():
    flag("wearable_events", "participant missing an event file", "error", int((missing == 0).sum().sum()),
         str(missing[(missing == 0).any(axis=1)].index.tolist()))
else:
    flag("wearable_events", "all participants have all 3 event files", "info", 0)

# %% [markdown]
# ## 1 — `participants/participants.csv`
# Dimension table. Everything else foreign-keys to `participant_id`; `device_id` is the link to
# device metadata (and the basis for the required `gold_participant_devices` table).

# %%
participants = pd.read_csv(RAW / "participants" / "participants.csv", dtype=str, keep_default_na=False, na_values=[""])
for c in ["age", "height_cm", "max_heart_rate"]:
    if c in participants.columns:
        participants[c] = pd.to_numeric(participants[c], errors="coerce")

display(participants.head())
display(profile(participants, "participants", key=["participant_id"]))

print("\n-- categorical value counts --")
for c in ["gender", "chronotype"]:
    if c in participants.columns:
        display(participants[c].value_counts(dropna=False).to_frame("n"))

# %%
# domain checks
name = "participants"
bad_pid = ~participants.participant_id.fillna("").str.fullmatch(r"P\d{3}")
if bad_pid.any():
    flag(name, "participant_id not P###", "error", bad_pid.sum(), participants.loc[bad_pid, "participant_id"].tolist()[:10])
bad_dev = ~participants.device_id.fillna("").str.fullmatch(r"FBT-\d{4}-[0-9A-Fa-f]{8}")
if bad_dev.any():
    flag(name, "device_id not FBT-NNNN-XXXXXXXX", "error", bad_dev.sum(), participants.loc[bad_dev, "device_id"].tolist()[:10])

flag(name, "n participants", "info", participants.participant_id.nunique(), "dictionary says 50")
if participants.participant_id.nunique() != 50:
    flag(name, "participant count != 50", "error", participants.participant_id.nunique())
if participants.device_id.duplicated().any():
    flag(name, "device_id shared by >1 participant", "error", participants.device_id.duplicated().sum(),
         participants.loc[participants.device_id.duplicated(keep=False)].sort_values("device_id").head(6).to_dict("records"))

range_check(participants, name, "age", 18, 90)
range_check(participants, name, "height_cm", 120, 220)
range_check(participants, name, "max_heart_rate", 120, 220)
for c in ["gender", "chronotype"]:
    vals = set(participants[c].dropna().unique())
    expected = {"male", "female"} if c == "gender" else {"A", "B"}
    if not vals <= expected:
        flag(name, f"`{c}` unexpected labels", "warn", int(participants[c].isin(vals - expected).sum()),
             f"got {sorted(vals)} expected {sorted(expected)}")

# max_heart_rate should be ~ 220 - age + small offset
participants["mhr_offset"] = participants.max_heart_rate - (220 - participants.age)
display(participants.mhr_offset.describe().to_frame().T)
off = participants.mhr_offset.abs() > 15
if off.any():
    flag(name, "max_heart_rate far from 220-age", "warn", off.sum(),
         participants.loc[off, ["participant_id", "age", "max_heart_rate", "mhr_offset"]].to_dict("records")[:8])

# %% [markdown]
# ## 2 — `health_summaries/device_metadata.csv`
# The dictionary already telegraphs one defect: `firmware_version` can be blank while
# `device_label` always carries the true value — so the label has to be regex-parsed and used as
# the authoritative source. We verify that claim rather than trusting it.

# %%
devmeta = pd.read_csv(RAW / "health_summaries" / "device_metadata.csv", dtype=str, keep_default_na=False, na_values=[""])
display(devmeta.head())
display(profile(devmeta, "device_metadata", key=["device_id"]))

name = "device_metadata"
display(devmeta.wear_site.value_counts(dropna=False).to_frame("n"))
display(devmeta.firmware_version.value_counts(dropna=False).to_frame("n"))

# firmware parsed out of the free-text label
lab_fw = devmeta.device_label.str.extract(r"fw\s*([0-9]+\.[0-9]+\.[0-9]+)", flags=re.I)[0]
lab_sn = devmeta.device_label.str.extract(r"SN:\s*(FBT-\d{4}-[0-9A-Fa-f]{8})", flags=re.I)[0]
devmeta["fw_from_label"] = lab_fw
devmeta["sn_from_label"] = lab_sn

blank_fw = devmeta.firmware_version.isna()
flag(name, "firmware_version blank (recoverable from label)", "warn", blank_fw.sum(),
     f"{100*blank_fw.mean():.1f}% of rows; label yields fw for {lab_fw[blank_fw].notna().sum()} of them")
unparsable = lab_fw.isna()
if unparsable.any():
    flag(name, "device_label firmware not parsable", "error", unparsable.sum(),
         devmeta.loc[unparsable, "device_label"].head(5).tolist())
mismatch = devmeta.firmware_version.notna() & lab_fw.notna() & (devmeta.firmware_version.str.strip() != lab_fw)
if mismatch.any():
    flag(name, "firmware_version disagrees with device_label", "error", mismatch.sum(),
         devmeta.loc[mismatch, ["device_id", "firmware_version", "device_label"]].head(5).to_dict("records"))
sn_mismatch = lab_sn.notna() & (devmeta.device_id.str.strip() != lab_sn)
if sn_mismatch.any():
    flag(name, "device_id disagrees with SN in device_label", "error", sn_mismatch.sum(),
         devmeta.loc[sn_mismatch, ["device_id", "device_label"]].head(5).to_dict("records"))

cal = pd.to_datetime(devmeta.calibration_date, errors="coerce", format="mixed")
flag(name, "calibration_date range", "info", cal.notna().sum(), f"{cal.min()} → {cal.max()}")
if cal.isna().any():
    flag(name, "calibration_date unparsable", "error", cal.isna().sum(),
         devmeta.loc[cal.isna(), "calibration_date"].dropna().unique()[:6].tolist())
late = cal > pd.Timestamp("2026-01-08")
if late.any():
    flag(name, "calibrated after study start", "warn", late.sum())

# %%
# --- referential integrity: participants <-> device_metadata --------------------------------
m = participants.merge(devmeta, on="participant_id", how="outer", suffixes=("_p", "_d"), indicator=True)
display(m._merge.value_counts().to_frame("n"))
for side, label in [("left_only", "participant with no device_metadata row"),
                    ("right_only", "device_metadata row with unknown participant")]:
    n = (m._merge == side).sum()
    if n:
        flag("join participants×device_metadata", label, "error", n,
             m.loc[m._merge == side, "participant_id"].tolist()[:10])
both = m[m._merge == "both"]
dev_mismatch = both.device_id_p.str.strip() != both.device_id_d.str.strip()
if dev_mismatch.any():
    flag("join participants×device_metadata", "device_id differs between the two files", "error", dev_mismatch.sum(),
         both.loc[dev_mismatch, ["participant_id", "device_id_p", "device_id_d"]].head(8).to_dict("records"))
# one row per participant? (feeds gold_participant_devices)
dup_dev = devmeta.groupby("participant_id").size()
if (dup_dev > 1).any():
    flag("device_metadata", "participant with >1 device row", "warn", int((dup_dev > 1).sum()),
         dup_dev[dup_dev > 1].to_dict())

# %% [markdown]
# ## 3 — `health_summaries/wellness_survey.csv`
# Daily self-report, grain = participant × date. Expect 50 × 28 = **1 400** rows if complete.

# %%
survey = pd.read_csv(RAW / "health_summaries" / "wellness_survey.csv", dtype=str, keep_default_na=False, na_values=[""])
display(survey.head())
SCORES = [c for c in survey.columns if c.endswith("_score")]
display(profile(survey, "wellness_survey", key=["participant_id", "date"]))

name = "wellness_survey"
print("\n-- raw score value counts (before coercion) --")
for c in SCORES:
    display(survey[c].value_counts(dropna=False).head(15).to_frame("n").T)

# %%
# score domains per the dictionary
DOMAINS = {"fatigue_score": (1, 5), "stress_score": (1, 5), "sleep_quality_score": (1, 5), "readiness_score": (0, 10)}
for c, (lo, hi) in DOMAINS.items():
    if c in survey.columns:
        range_check(survey, name, c, lo, hi)

# dates
d = pd.to_datetime(survey.date, errors="coerce", format="mixed")
if d.isna().any():
    flag(name, "unparsable date", "error", d.isna().sum(), survey.loc[d.isna(), "date"].unique()[:8].tolist())
survey["_date"] = d
flag(name, "date range", "info", d.notna().sum(), f"{d.min().date()} → {d.max().date()} ({d.dt.date.nunique()} distinct days)")
STUDY_START, STUDY_END = pd.Timestamp("2026-01-08"), pd.Timestamp("2026-02-04")
oob = d.notna() & ((d < STUDY_START) | (d > STUDY_END))
if oob.any():
    flag(name, "date outside study window", "error", oob.sum(), sorted(d[oob].dt.date.unique().tolist())[:10])

# completeness matrix: expect 28 rows per participant
per = survey.groupby("participant_id")._date.agg(n_rows="size", n_days="nunique", first="min", last="max")
display(per.describe().T)
display(per[(per.n_days != 28) | (per.n_rows != per.n_days)].head(20))
short = per[per.n_days < 28]
if len(short):
    flag(name, "participants with < 28 survey days", "warn", len(short),
         f"missing days total = {int((28 - short.n_days).sum())}")
dupe_days = per[per.n_rows > per.n_days]
if len(dupe_days):
    flag(name, "participants with same-day duplicate surveys", "error", len(dupe_days), dupe_days.index.tolist()[:10])

# FK
unknown = ~survey.participant_id.isin(participants.participant_id)
if unknown.any():
    flag(name, "participant_id not in participants.csv", "error", unknown.sum(),
         survey.loc[unknown, "participant_id"].unique()[:10].tolist())
never = set(participants.participant_id) - set(survey.participant_id)
if never:
    flag(name, "participants with zero survey rows", "warn", len(never), sorted(never)[:10])

# %% [markdown]
# ## 4 — `wearable_events/` loader
# Per-participant JSON, read once and cached to parquet in `poc/data/interim/` so re-running the
# notebook is cheap. **Timestamps are deliberately kept as strings** — their format is itself one
# of the things under inspection (the dictionary warns some heart-rate files emit `Z`-suffixed UTC).

# %%
def _kind_of(fname: str) -> str | None:
    f = fname.lower()
    if "heart" in f:
        return "heart_rate"
    if "sleep" in f:
        return "sleep"
    if "step" in f:
        return "steps"
    return None


def load_flat(kind: str, force: bool = False) -> pd.DataFrame:
    """steps / heart_rate: flat arrays of records."""
    cache = INTERIM / f"raw_{kind}.parquet"
    if cache.exists() and not force:
        return pd.read_parquet(cache)
    parts = []
    for i, d in enumerate(pdirs, 1):
        hits = [f for f in d.iterdir() if f.suffix == ".json" and _kind_of(f.name) == kind]
        if not hits:
            continue
        for f in hits:
            with open(f) as fh:
                payload = json.load(fh)
            df = pd.json_normalize(payload)
            df["_dir_participant"] = d.name
            df["_file"] = f.name
            parts.append(df)
        if i % 10 == 0:
            print(f"  {kind}: {i}/{len(pdirs)} participants")
    out = pd.concat(parts, ignore_index=True)
    out.to_parquet(cache, index=False)
    return out


def load_sleep(force: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """sleep: sessions + exploded stages."""
    cs, cg = INTERIM / "raw_sleep_sessions.parquet", INTERIM / "raw_sleep_stages.parquet"
    if cs.exists() and cg.exists() and not force:
        return pd.read_parquet(cs), pd.read_parquet(cg)
    sess, stg = [], []
    for d in pdirs:
        hits = [f for f in d.iterdir() if f.suffix == ".json" and _kind_of(f.name) == "sleep"]
        for f in hits:
            with open(f) as fh:
                payload = json.load(fh)
            for si, s in enumerate(payload):
                base = {k: v for k, v in s.items() if k != "stages"}
                base |= {"_dir_participant": d.name, "_file": f.name, "_session_ix": si}
                sess.append(base)
                for gi, g in enumerate(s.get("stages") or []):
                    stg.append(g | {"participant_id": s.get("participant_id"), "date": s.get("date"),
                                    "_dir_participant": d.name, "_session_ix": si, "_stage_ix": gi})
    sdf, gdf = pd.DataFrame(sess), pd.DataFrame(stg)
    sdf.to_parquet(cs, index=False)
    gdf.to_parquet(cg, index=False)
    return sdf, gdf


import time as _t

_t0 = _t.time(); steps = load_flat("steps"); print(f"steps loaded in {_t.time()-_t0:.1f}s")
_t0 = _t.time(); hr = load_flat("heart_rate"); print(f"heart_rate loaded in {_t.time()-_t0:.1f}s")
_t0 = _t.time(); sleep_sessions, sleep_stages = load_sleep(); print(f"sleep loaded in {_t.time()-_t0:.1f}s")
print("\nsteps:", steps.shape, "| heart_rate:", hr.shape, "| sleep sessions:", sleep_sessions.shape, "| stages:", sleep_stages.shape)
display(steps.head(3)); display(hr.head(3)); display(sleep_sessions.head(3)); display(sleep_stages.head(3))

# %% [markdown]
# ### 4a — `steps.json` (per-minute)
# Nominal: 50 × 40 320 = **2 016 000** rows.

# %%
name = "steps"
display(profile(steps.drop(columns=["_file"]), name, key=["participant_id", "timestamp"]))

# id integrity: the participant_id inside the payload vs the folder it came from
mismatch = steps.participant_id.astype(str).str.strip() != steps._dir_participant
if mismatch.any():
    flag(name, "participant_id in file != folder name", "error", mismatch.sum(),
         steps.loc[mismatch, ["_dir_participant", "participant_id"]].drop_duplicates().head(8).to_dict("records"))

range_check(steps, name, "steps", 0, 300, severity="warn")
sv = pd.to_numeric(steps.steps, errors="coerce")
flag(name, "steps distribution", "info", len(sv),
     f"p50={sv.quantile(.5):.0f} p99={sv.quantile(.99):.0f} max={sv.max():.0f} zeros={100*(sv==0).mean():.1f}%")
if (sv < 0).any():
    flag(name, "negative step counts", "error", int((sv < 0).sum()), f"values {sorted(sv[sv<0].unique())[:8]}")
if (sv > 10000).any():
    flag(name, "physiologically impossible per-minute value (sentinel?)", "error", int((sv > 10000).sum()),
         f"values {sorted(sv[sv>10000].unique())[:8]} — no human takes 10k steps in one minute")

# %%
# --- schema drift: payload fields the dictionary never mentions -----------------------------
DOC_COLS = {"steps": {"timestamp", "steps", "participant_id"},
            "heart_rate": {"timestamp", "heart_rate_bpm", "participant_id"}}
INTERNAL = {"_dir_participant", "_file", "_ts", "_ts_fmt", "_ts_local", "_ts_utcaware", "_date"}
extra = sorted(set(steps.columns) - DOC_COLS["steps"] - INTERNAL)
if extra:
    present = steps[extra].notna().any(axis=1)
    flag(name, "undocumented field(s) in payload", "error", int(present.sum()),
         f"fields={extra}; carried by participants {sorted(steps.loc[present, '_dir_participant'].unique())}")
    for c in extra:
        display(steps[c].value_counts(dropna=False).to_frame("n"))

if "_unit" in steps.columns:
    u = steps["_unit"].fillna("(absent)")
    display(steps.assign(_u=u.values, _v=sv.values).groupby("_u")._v
                 .describe()[["count", "50%", "75%", "max"]].round(1))
    ph = (u == "steps_per_hour").values
    mult60 = ph & (sv % 60 == 0).values & (sv > 0).values
    flag(name, "unit=steps_per_hour rows on a per-minute grid", "error", int(ph.sum()),
         f"participants {sorted(steps.loc[ph, '_dir_participant'].unique())}; "
         f"{int(mult60.sum())}/{int((ph & (sv>0).values).sum())} non-zero values are exact multiples of 60 "
         f"→ these are hourly rates mislabelled as per-minute counts; rescale (/60) before any daily sum")
    display(steps.loc[ph & (sv > 0).values, ["_dir_participant", "timestamp", "steps", "_unit"]].head(8))

# %%
# timestamp hygiene
ts_raw = steps.timestamp.astype(str)
fmt = pd.Series(np.select(
    [ts_raw.str.endswith("Z"), ts_raw.str.contains(r"[+-]\d{2}:\d{2}$", regex=True), ts_raw.str.contains(r"\.\d+$")],
    ["utc_z", "offset", "naive_with_ms"], default="naive"), index=ts_raw.index)
display(fmt.value_counts().to_frame("n"))
if fmt.nunique() > 1:
    flag(name, "mixed timestamp formats", "error", int((fmt != fmt.mode()[0]).sum()), fmt.value_counts().to_dict())

not_minute = ~ts_raw.str.contains(r"T\d{2}:\d{2}:00", regex=True)
if not_minute.any():
    flag(name, "timestamp not on a whole-minute boundary", "error", not_minute.sum(),
         ts_raw[not_minute].unique()[:8].tolist())

sts = pd.to_datetime(ts_raw, errors="coerce", format="mixed", utc=False)
steps["_ts"] = sts
if sts.isna().any():
    flag(name, "unparsable timestamp", "error", sts.isna().sum(), ts_raw[sts.isna()].unique()[:6].tolist())

dups = steps.duplicated(subset=["_dir_participant", "timestamp"]).sum()
if dups:
    ex = steps[steps.duplicated(subset=["_dir_participant", "timestamp"], keep=False)].sort_values(["_dir_participant", "timestamp"])
    flag(name, "duplicate (participant, timestamp)", "error", dups,
         f"e.g. {ex.head(4)[['_dir_participant','timestamp','steps']].to_dict('records')}")
    display(ex.head(10))

cov = steps.groupby("_dir_participant").agg(n_rows=("timestamp", "size"), n_ts=("timestamp", "nunique"),
                                            ts_min=("_ts", "min"), ts_max=("_ts", "max"))
cov["expected_min"] = ((cov["ts_max"] - cov["ts_min"]).dt.total_seconds() // 60 + 1).astype("Int64")
cov["gap_minutes"] = cov.expected_min - cov.n_ts
display(cov.describe().T)
display(cov[(cov.n_rows != 40320) | (cov.gap_minutes != 0)].head(20))
if (cov.n_rows != 40320).any():
    flag(name, "participants with != 40 320 rows", "warn", int((cov.n_rows != 40320).sum()),
         f"min={cov.n_rows.min()} max={cov.n_rows.max()} (nominal 40320)")
if (cov.gap_minutes > 0).any():
    flag(name, "missing minutes inside participant's own range", "warn", int((cov.gap_minutes > 0).sum()),
         f"total missing minutes = {int(cov.gap_minutes.clip(lower=0).sum())}")

# %% [markdown]
# ### 4b — `heart_rate.json` (per-minute)
# Two things the dictionary explicitly warns about, both verified here:
# 1. some files emit **`Z`-suffixed UTC** while everything else is naive local (UTC-05:00);
# 2. **stuck sensor** — a frozen optical reading repeated for hours, which sits *inside* the valid
#    40–200 bpm range and therefore survives a naive range check.

# %%
name = "heart_rate"
display(profile(hr.drop(columns=["_file"]), name, key=["participant_id", "timestamp"]))

mismatch = hr.participant_id.astype(str).str.strip() != hr._dir_participant
if mismatch.any():
    flag(name, "participant_id in file != folder name", "error", mismatch.sum(),
         hr.loc[mismatch, ["_dir_participant", "participant_id"]].drop_duplicates().head(8).to_dict("records"))

range_check(hr, name, "heart_rate_bpm", 40, 200)
bv = pd.to_numeric(hr.heart_rate_bpm, errors="coerce")
flag(name, "bpm distribution", "info", len(bv),
     f"p01={bv.quantile(.01):.0f} p50={bv.quantile(.5):.0f} p99={bv.quantile(.99):.0f} min={bv.min():.0f} max={bv.max():.0f}")

# %%
# timestamp format split — per participant, since it is a device-level quirk
ts_raw = hr.timestamp.astype(str)
hr["_ts_fmt"] = np.select(
    [ts_raw.str.endswith("Z"), ts_raw.str.contains(r"[+-]\d{2}:\d{2}$", regex=True)],
    ["utc_z", "offset"], default="naive")
display(hr._ts_fmt.value_counts().to_frame("n"))
by_p = hr.pivot_table(index="_dir_participant", columns="_ts_fmt", values="timestamp", aggfunc="size").fillna(0).astype(int)
display(by_p[by_p.get("utc_z", pd.Series(0, index=by_p.index)) > 0])
if hr._ts_fmt.nunique() > 1:
    aff = by_p[by_p.get("utc_z", pd.Series(0, index=by_p.index)) > 0].index.tolist()
    flag(name, "mixed timestamp formats (naive local vs UTC Z)", "error", int((hr._ts_fmt != "naive").sum()),
         f"affected participants: {aff} — needs offset-aware parse + shift to study TZ UTC-05:00")

hr["_ts_utcaware"] = pd.to_datetime(ts_raw, errors="coerce", format="mixed", utc=True)
# naive strings are *local* (UTC-05:00); Z strings are already UTC -> normalise everything to local
local = hr._ts_utcaware.dt.tz_convert("UTC") - pd.Timedelta(hours=5)
hr["_ts_local"] = np.where(hr._ts_fmt == "naive", pd.to_datetime(ts_raw, errors="coerce", format="mixed").values,
                           local.dt.tz_localize(None).values)
hr["_ts_local"] = pd.to_datetime(hr["_ts_local"])
if hr._ts_utcaware.isna().any():
    flag(name, "unparsable timestamp", "error", hr._ts_utcaware.isna().sum(), ts_raw[hr._ts_utcaware.isna()].unique()[:6].tolist())

not_minute = ~ts_raw.str.contains(r"T\d{2}:\d{2}:00", regex=True)
if not_minute.any():
    flag(name, "timestamp not on a whole-minute boundary", "error", not_minute.sum(), ts_raw[not_minute].unique()[:8].tolist())

dups = hr.duplicated(subset=["_dir_participant", "_ts_local"]).sum()
if dups:
    flag(name, "duplicate (participant, local timestamp)", "error", dups,
         "note: naive-vs-Z rows can collide once normalised — check before de-duping")

cov_hr = hr.groupby("_dir_participant").agg(n_rows=("timestamp", "size"), n_ts=("_ts_local", "nunique"),
                                            ts_min=("_ts_local", "min"), ts_max=("_ts_local", "max"))
cov_hr["expected_min"] = ((cov_hr["ts_max"] - cov_hr["ts_min"]).dt.total_seconds() // 60 + 1).astype("Int64")
cov_hr["gap_minutes"] = cov_hr.expected_min - cov_hr.n_ts
display(cov_hr.describe().T)
display(cov_hr[(cov_hr.n_rows != 40320) | (cov_hr.gap_minutes != 0)].head(20))
if (cov_hr.n_rows != 40320).any():
    flag(name, "participants with != 40 320 rows", "warn", int((cov_hr.n_rows != 40320).sum()),
         f"min={cov_hr.n_rows.min()} max={cov_hr.n_rows.max()}")

# %%
# --- stuck-sensor detection: run-length encoding of identical consecutive bpm ---------------
g = hr[["_dir_participant", "_ts_local", "heart_rate_bpm"]].sort_values(["_dir_participant", "_ts_local"]).copy()
g["heart_rate_bpm"] = pd.to_numeric(g.heart_rate_bpm, errors="coerce")
new_run = (g.heart_rate_bpm != g.groupby("_dir_participant").heart_rate_bpm.shift()) | \
          (g._dir_participant != g._dir_participant.shift())
g["run_id"] = new_run.cumsum()
runs = (g.groupby(["_dir_participant", "run_id"])
          .agg(bpm=("heart_rate_bpm", "first"), n_min=("heart_rate_bpm", "size"),
               start=("_ts_local", "min"), end=("_ts_local", "max"))
          .reset_index())
display(runs.n_min.describe().to_frame().T)
print("\nrun-length histogram (minutes of an unchanging reading):")
display(pd.cut(runs.n_min, [0, 2, 5, 10, 30, 60, 120, 360, 10**9]).value_counts().sort_index().to_frame("n_runs"))

STUCK_MIN = 30  # >=30 identical consecutive minutes is not physiological while awake
stuck = runs[runs.n_min >= STUCK_MIN].sort_values("n_min", ascending=False)
display(stuck.head(20))
if len(stuck):
    flag(name, f"stuck sensor: >= {STUCK_MIN} identical consecutive minutes", "error", int(stuck.n_min.sum()),
         f"{len(stuck)} runs across {stuck._dir_participant.nunique()} participants; "
         f"longest {int(stuck.n_min.max())} min ({stuck.n_min.max()/60:.1f} h) @ {stuck.iloc[0].bpm:.0f} bpm")
runs.to_parquet(INTERIM / "hr_flatline_runs.parquet", index=False)

# %% [markdown]
# ### 4c — `sleep.json` (nightly sessions + nested stages)
# Nominal: 50 × 28 = **1 400** sessions, 5 stages each (`light → deep → rem → light → awake`).
# The dictionary says derived metrics must come from `stages[]`, so the stage array's internal
# consistency is what matters most here.

# %%
name = "sleep_sessions"
display(profile(sleep_sessions.drop(columns=["_file"]), name, key=["participant_id", "date"]))
display(profile(sleep_stages, "sleep_stages"))

mismatch = sleep_sessions.participant_id.astype(str).str.strip() != sleep_sessions._dir_participant
if mismatch.any():
    flag(name, "participant_id in file != folder name", "error", mismatch.sum(),
         sleep_sessions.loc[mismatch, ["_dir_participant", "participant_id"]].drop_duplicates().head(8).to_dict("records"))

nights = sleep_sessions.groupby("_dir_participant").agg(n_sessions=("date", "size"), n_dates=("date", "nunique"))
display(nights.describe().T)
display(nights[(nights.n_sessions != 28) | (nights.n_dates != nights.n_sessions)].head(20))
if (nights.n_sessions != 28).any():
    flag(name, "participants with != 28 nights", "warn", int((nights.n_sessions != 28).sum()),
         f"min={nights.n_sessions.min()} max={nights.n_sessions.max()}")
if (nights.n_dates != nights.n_sessions).any():
    flag(name, "duplicate night for same date", "error", int((nights.n_sessions - nights.n_dates).sum()))

range_check(sleep_sessions, name, "efficiency_pct", 0, 100)
range_check(sleep_sessions, name, "restlessness", 0, 1)

# %%
# session timestamps + duration sanity
onset = pd.to_datetime(sleep_sessions.sleep_onset, errors="coerce", format="mixed")
end = pd.to_datetime(sleep_sessions.sleep_end, errors="coerce", format="mixed")
sleep_sessions["_onset"], sleep_sessions["_end"] = onset, end
sleep_sessions["_span_min"] = (end - onset).dt.total_seconds() / 60
display(sleep_sessions._span_min.describe().to_frame().T)

if onset.isna().any() or end.isna().any():
    flag(name, "unparsable sleep_onset/sleep_end", "error", int(onset.isna().sum() + end.isna().sum()))
neg = sleep_sessions._span_min <= 0
if neg.any():
    flag(name, "sleep_end <= sleep_onset", "error", neg.sum(),
         sleep_sessions.loc[neg, ["participant_id", "date", "sleep_onset", "sleep_end"]].head(5).to_dict("records"))
odd = (sleep_sessions._span_min < 120) | (sleep_sessions._span_min > 900)
if odd.any():
    flag(name, "session span outside 2–15 h", "warn", odd.sum(),
         f"min={sleep_sessions._span_min.min():.0f}min max={sleep_sessions._span_min.max():.0f}min")

# does the session land on the night *before* `date`?
sd = pd.to_datetime(sleep_sessions.date, errors="coerce", format="mixed")
wrong_day = sd.notna() & end.notna() & (end.dt.normalize() != sd)
if wrong_day.any():
    flag(name, "sleep_end date != `date` field", "warn", wrong_day.sum(),
         sleep_sessions.loc[wrong_day, ["participant_id", "date", "sleep_end"]].head(5).to_dict("records"))

# %%
# stage-array integrity
name = "sleep_stages"
display(sleep_stages.stage.value_counts(dropna=False).to_frame("n"))
EXPECTED = {"light", "deep", "rem", "awake"}
bad_stage = ~sleep_stages.stage.isin(EXPECTED)
if bad_stage.any():
    flag(name, "unexpected stage label", "error", bad_stage.sum(), sleep_stages.loc[bad_stage, "stage"].unique()[:10].tolist())

st = pd.to_datetime(sleep_stages.start_time, errors="coerce", format="mixed")
et = pd.to_datetime(sleep_stages.end_time, errors="coerce", format="mixed")
sleep_stages["_computed_min"] = (et - st).dt.total_seconds() / 60
sleep_stages["_delta"] = pd.to_numeric(sleep_stages.duration_min, errors="coerce") - sleep_stages._computed_min
display(sleep_stages._delta.describe().to_frame().T)
bad_dur = sleep_stages._delta.abs() > 0.5
if bad_dur.any():
    flag(name, "duration_min != end_time - start_time", "error", bad_dur.sum(),
         f"{100*bad_dur.mean():.2f}% of stages; max |delta| = {sleep_stages._delta.abs().max():.1f} min")
    display(sleep_stages[bad_dur].head(10))

# contiguity: each stage's end_time should equal the next stage's start_time
sleep_stages["_st"], sleep_stages["_et"] = st, et
ss = sleep_stages.sort_values(["_dir_participant", "_session_ix", "_stage_ix"])
nxt = ss.groupby(["_dir_participant", "_session_ix"])._st.shift(-1)
gap = (nxt - ss._et).dt.total_seconds() / 60
noncontig = gap.notna() & (gap.abs() > 0.5)
if noncontig.any():
    flag(name, "gap/overlap between consecutive stages", "error", noncontig.sum(),
         f"max gap {gap.max():.1f} min, min gap {gap.min():.1f} min")

# stage ordering
order = ss.groupby(["_dir_participant", "_session_ix"]).stage.apply(lambda s: "→".join(s))
display(order.value_counts().head(10).to_frame("n_sessions"))
CANON = "light→deep→rem→light→awake"
off_pattern = (order != CANON).sum()
if off_pattern:
    flag(name, "stage sequence != canonical light→deep→rem→light→awake", "warn", int(off_pattern),
         order[order != CANON].value_counts().head(5).to_dict())

# %%
# --- derived metrics from stages, and what the session-level fields are actually worth -------
# Anchors observed in the data (verified below, not assumed):
#   sleep_onset == first stage start;  sleep_end == end of the LAST NON-AWAKE stage.
# The trailing `awake` stage therefore runs *past* sleep_end — so time-in-bed is
# first stage start → last stage end, NOT sleep_onset → sleep_end.
name = "sleep_sessions"
d = sleep_stages.copy()
d["duration_min"] = pd.to_numeric(d.duration_min, errors="coerce")
GK = ["_dir_participant", "_session_ix"]
agg = d.groupby(GK).agg(tib_start=("_st", "min"), tib_end=("_et", "max"), stage_min_total=("duration_min", "sum"))
agg = agg.join(d[d.stage != "awake"].groupby(GK).agg(total_sleep_min=("duration_min", "sum"),
                                                     last_sleep_end=("_et", "max")))
agg = agg.join(d[d.stage == "deep"].groupby(GK).duration_min.sum().rename("deep_min")).reset_index()
agg["time_in_bed_min"] = (agg.tib_end - agg.tib_start).dt.total_seconds() / 60
agg["deep_sleep_pct"] = 100 * agg.deep_min / agg.total_sleep_min
agg["sleep_efficiency_pct"] = 100 * agg.total_sleep_min / agg.time_in_bed_min

chk = sleep_sessions.merge(agg, on=GK, how="left")
chk["_eff_delta"] = pd.to_numeric(chk.efficiency_pct, errors="coerce") - chk.sleep_efficiency_pct
display(chk[["total_sleep_min", "time_in_bed_min", "deep_sleep_pct", "sleep_efficiency_pct",
             "efficiency_pct", "_eff_delta"]].describe().T.round(2))

# structural anchors
n = int((chk._onset != chk.tib_start).sum())
flag(name, "sleep_onset == first stage start", "info" if n == 0 else "error", n)
n = int((chk._end != chk.last_sleep_end).sum())
if n:
    flag(name, "sleep_end != end of last non-awake stage", "error", n,
         chk.loc[chk._end != chk.last_sleep_end, ["participant_id", "date", "sleep_end"]].head(5).to_dict("records"))
n = int(((chk.stage_min_total - chk.time_in_bed_min).abs() > 1).sum())
if n:
    flag("sleep_stages", "stage durations don't tile the session span", "error", n)

# is the pre-computed efficiency_pct usable at all?
rep = pd.to_numeric(chk.efficiency_pct, errors="coerce")
clipped = int((rep == 100).sum())
corr = rep.corr(chk.sleep_efficiency_pct)
flag(name, "efficiency_pct pinned at exactly 100.0", "error", clipped,
     f"{100*clipped/len(chk):.0f}% of sessions; corr(reported, stage-derived) = {corr:.2f} "
     f"→ field is saturated/unreliable, derive efficiency from stages[] as the dictionary instructs")
bad_eff = chk._eff_delta.abs() > 1
if bad_eff.any():
    flag(name, "efficiency_pct disagrees with stage-derived efficiency (>1pp)", "error", int(bad_eff.sum()),
         f"{100*bad_eff.mean():.1f}% of sessions; median delta = {chk._eff_delta.median():.1f} pp")
display(rep.value_counts().head(8).to_frame("n_sessions"))
chk.to_parquet(INTERIM / "sleep_sessions_derived.parquet", index=False)

# %% [markdown]
# ## 5 — Cross-source coverage
# One row per participant: does every source actually cover the same 50 × 28 grid?

# %%
steps["_date"] = steps._ts.dt.date
hr["_date"] = hr._ts_local.dt.date
cover = pd.DataFrame({"in_participants": participants.set_index("participant_id").index.to_series().notna()})
cover = (participants[["participant_id"]].set_index("participant_id")
         .join(devmeta.groupby("participant_id").size().rename("device_rows"))
         .join(survey.groupby("participant_id")._date.nunique().rename("survey_days"))
         .join(sleep_sessions.groupby("participant_id").size().rename("sleep_nights"))
         .join(steps.groupby("participant_id")._date.nunique().rename("steps_days"))
         .join(steps.groupby("participant_id").size().rename("steps_rows"))
         .join(hr.groupby("participant_id")._date.nunique().rename("hr_days"))
         .join(hr.groupby("participant_id").size().rename("hr_rows")))
display(cover.describe().T)
incomplete = cover[(cover.fillna(0) != cover.mode().iloc[0]).any(axis=1)]
print(f"\nparticipants deviating from the modal coverage profile: {len(incomplete)}")
display(incomplete.head(25))
cover.to_csv(OUT / "coverage_matrix.csv")
for c in cover.columns:
    miss = cover[c].isna().sum()
    if miss:
        flag("coverage", f"participants with no `{c}`", "error", int(miss), cover[cover[c].isna()].index.tolist()[:10])

# %% [markdown]
# ## 6 — Findings register
# Everything the notebook flagged, ranked. This is the input to the Silver-layer contract:
# each `error` needs an explicit rule (reject / quarantine / repair) and a test.

# %%
fdf = pd.DataFrame(FINDINGS)
fdf["severity"] = pd.Categorical(fdf.severity, ["error", "warn", "info"], ordered=True)
fdf = fdf.sort_values(["severity", "source", "n_rows"], ascending=[True, True, False]).reset_index(drop=True)
display(fdf.severity.value_counts().to_frame("n"))
pd.set_option("display.max_colwidth", 160)
display(fdf)
fdf.to_csv(OUT / "raw_findings.csv", index=False)
print(f"\nwrote {OUT/'raw_findings.csv'} ({len(fdf)} findings) and {OUT/'coverage_matrix.csv'}")
print("cached parquet in", INTERIM, ":", sorted(p.name for p in INTERIM.glob('*.parquet')))
