from __future__ import annotations

import argparse
import logging
import sys
import time

import pandas as pd

from . import bronze, config as C, gold, io_uri as io, quality, silver

LOG_FORMAT = "%(asctime)s  %(levelname)-5s  %(message)s"
ALL_LAYERS = ("bronze", "silver", "gold", "analytics")


# --------------------------------------------------------------------------
def build_silver(
    bronze_tables: dict[str, pd.DataFrame], settings: C.Settings, persist: bool
) -> tuple[dict[str, pd.DataFrame], list[silver.Check]]:
    checks: list[silver.Check] = []
    tables: dict[str, pd.DataFrame] = {}
    quarantine: dict[str, pd.DataFrame] = {}

    participants, c = silver.build_participants(bronze_tables["bronze_participants"]); checks += c
    tables["silver_participants"] = participants

    devices, c = silver.build_devices(bronze_tables["bronze_device_metadata"], participants); checks += c
    tables["silver_devices"] = devices

    steps, c = silver.build_steps(bronze_tables.pop("bronze_steps")); checks += c
    tables["silver_steps_minute"] = steps

    hr, c = silver.build_heart_rate(bronze_tables.pop("bronze_heart_rate")); checks += c
    tables["silver_heart_rate_minute"] = hr

    sessions, stages, sleep_q, c = silver.build_sleep(
        bronze_tables.pop("bronze_sleep_sessions"), bronze_tables.pop("bronze_sleep_stages")
    ); checks += c
    tables["silver_sleep_sessions"] = sessions
    tables["silver_sleep_stages"] = stages
    quarantine["sleep_sessions"] = sleep_q

    survey, survey_q, c = silver.build_survey(bronze_tables["bronze_wellness_survey"], participants); checks += c
    tables["silver_wellness_survey"] = survey
    quarantine["wellness_survey"] = survey_q

    coverage, c = silver.build_coverage(participants, steps, hr, sessions, survey); checks += c
    tables["silver_participant_day_coverage"] = coverage

    if persist:
        partitioned = {"silver_steps_minute": ["date"], "silver_heart_rate_minute": ["date"]}
        for name, df in tables.items():
            n = io.write_table(df, io.join(settings.target_uri, C.SILVER), name,
                               partitioned.get(name, ()), settings.compression)
            logging.info("silver: wrote %-34s %9d rows -> %3d object(s)", name, len(df), n)
        for name, df in quarantine.items():
            if len(df):
                io.write_table(df, io.join(settings.target_uri, C.QUARANTINE), name,
                               (), settings.compression)
                logging.info("silver: quarantined %-27s %9d rows", name, len(df))
    return tables, checks


def load_silver(settings: C.Settings) -> dict[str, pd.DataFrame]:
    base = io.join(settings.target_uri, C.SILVER)
    return {name: io.read_table(base, name) for name in gold.VIEW_NAMES}


# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="run_etl", description="Loka data engineering test - wearable pipeline")
    p.add_argument("--source", help="override the source URI (s3:// or a local path)")
    p.add_argument("--target", help="override the target URI (s3:// or a local path)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--online", action="store_true",
                      help="use SOURCE_URI_REMOTE / TARGET_URI_REMOTE from .env (S3)")
    mode.add_argument("--offline", action="store_true",
                      help="the default: SOURCE_URI_LOCAL / TARGET_URI_LOCAL, no AWS")
    p.add_argument("--layers", default=",".join(ALL_LAYERS),
                   help=f"comma separated subset of {ALL_LAYERS}")
    p.add_argument("--participants", help="comma separated subset, e.g. P001,P002 (smoke run)")
    p.add_argument("--run-id", help="reuse a run id to replay a previous run")
    p.add_argument("--strict", action="store_true", help="exit 1 if any pipeline assertion fails")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format=LOG_FORMAT, datefmt="%H:%M:%S", stream=sys.stdout)

    settings = C.load_settings(args.source, args.target, online=args.online)
    layers = [l.strip() for l in args.layers.split(",") if l.strip()]
    pids = [x.strip() for x in args.participants.split(",")] if args.participants else None
    started = pd.Timestamp.utcnow().tz_localize(None)
    run_id = args.run_id or f"run_{started.strftime('%Y%m%dT%H%M%SZ')}"
    t0 = time.perf_counter()

    logging.info("=" * 78)
    logging.info("run id : %s", run_id)
    logging.info("mode   : %s", settings.mode)
    logging.info("source : %s", settings.source_uri)
    logging.info("target : %s", settings.target_uri)
    logging.info("layers : %s", ", ".join(layers))
    logging.info("=" * 78)

    # Preflight before any heavy IO: a missing credential or a bucket typo
    # should cost a second, not a full ingest.
    if "bronze" in layers:
        io.check_readable(settings.source_uri, "source")
    if {"bronze", "silver", "gold"} & set(layers):
        io.check_writable(settings.target_uri, "target", run_id)
    logging.info("preflight: source readable, target writable")

    checks: list[silver.Check] = []
    row_counts: dict[str, int] = {}

    # --- Bronze -----------------------------------------------------------
    bronze_tables: dict[str, pd.DataFrame] = {}
    if "bronze" in layers:
        bronze_tables, c = bronze.ingest(settings, run_id, participants=pids, persist=True)
        checks += c
        row_counts |= {k: len(v) for k, v in bronze_tables.items()}
    elif "silver" in layers:
        base = io.join(settings.target_uri, C.BRONZE)
        for name in ("bronze_participants", "bronze_device_metadata", "bronze_wellness_survey",
                     "bronze_steps", "bronze_heart_rate", "bronze_sleep_sessions", "bronze_sleep_stages"):
            bronze_tables[name] = io.read_table(base, name)
        logging.info("bronze: re-read %d published tables", len(bronze_tables))

    # --- Silver -----------------------------------------------------------
    if "silver" in layers:
        silver_tables, c = build_silver(bronze_tables, settings, persist=True)
        checks += c
    elif "gold" in layers:
        silver_tables = load_silver(settings)
        logging.info("silver: re-read %d published tables", len(silver_tables))
    else:
        silver_tables = {}
    row_counts |= {k: len(v) for k, v in silver_tables.items()}
    del bronze_tables

    # --- Gold + analytics ---------------------------------------------------
    gold_tables: dict[str, pd.DataFrame] = {}
    con = None
    if "gold" in layers and silver_tables:
        gold_tables, con = gold.build(silver_tables)
        partitioned = {"gold_daily_participant_metrics": ["date"]}
        for name, df in gold_tables.items():
            n = io.write_table(df, io.join(settings.target_uri, C.GOLD), name,
                               partitioned.get(name, ()), settings.compression)
            logging.info("gold:   wrote %-36s %7d rows -> %3d object(s)", name, len(df), n)
        row_counts |= {k: len(v) for k, v in gold_tables.items()}
        checks += quality.pipeline_assertions(silver_tables, gold_tables)

    if "analytics" in layers and con is not None:
        results = gold.run_analytics(con)
        md = gold.analytics_report(results, run_id)
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        (settings.output_dir / "analytical_query_output.md").write_text(md)
        for name, df in results.items():
            df.to_csv(settings.output_dir / f"{name}.csv", index=False)
            io.write_table(df, io.join(settings.target_uri, C.GOLD, "_analytics"),
                           name, (), settings.compression)
        io.write_text(md, io.join(settings.target_uri, C.GOLD, "_analytics",
                                  "analytical_query_output.md"))
        logging.info("gold:   analytical query output -> %s", settings.output_dir)

    # --- Data quality --------------------------------------------------------
    elapsed = round(time.perf_counter() - t0, 1)
    dq = quality.to_frame(checks, run_id, started)
    if len(dq):
        io.write_table(dq, io.join(settings.target_uri, C.DQ), "dq_checks",
                       ["run_date"], settings.compression)
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        dq.to_csv(settings.output_dir / "dq_checks.csv", index=False)
        report = quality.report(dq, run_id, {
            "source_uri": settings.source_uri, "target_uri": settings.target_uri,
            "elapsed_s": elapsed, "row_counts": row_counts,
        })
        (settings.output_dir / "run_report.md").write_text(report)
        io.write_text(report, io.join(settings.target_uri, C.DQ, f"run_report_{run_id}.md"))

    s = quality.summarise(dq) if len(dq) else {k: 0 for k in
                                               ("assert_fail", "error", "warn", "assert_pass", "info")}
    logging.info("=" * 78)
    logging.info("done in %ss  |  source defects handled: %d   warnings: %d   "
                 "assertions passed: %d   FAILED: %d",
                 elapsed, s["error"], s["warn"], s["assert_pass"], s["assert_fail"])
    logging.info("lake   : %s", settings.target_uri)
    logging.info("reports: %s", settings.output_dir)
    logging.info("=" * 78)

    if s["assert_fail"]:
        for _, row in dq[dq["severity"] == "assert_fail"].iterrows():
            logging.error("ASSERT FAILED  %s.%s  %s", row["table"], row["rule"], row["detail"])
        if args.strict:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
