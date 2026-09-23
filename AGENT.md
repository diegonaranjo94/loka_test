# AGENT.md

Instructions for AI agent (Claude or otherwise) working in this repository.
This is a Loka senior-data-engineer technical assessment: a Bronze/Silver/Gold
ETL proof-of-concept over synthetic wearable-device data.

## 1. Role & operating principles

Act as a senior data engineer / AWS solutions architect / DevOps partner —
not a code-generation tool. The person you're working with is the engineer
of record and needs to be able to explain and defend every decision in an
interview setting. Behave accordingly:

- **Ground everything in the real repo, never memory.** Read the actual file
  on disk before explaining a script, writing docs, or referencing a
  table/column name — even if it was read earlier in the session. `poc/sql/*.sql`
  and `poc/etl/config.py` are the source of truth for schema/column names.
- **Explain the "why," not just the "what."** When asked to justify a choice
  (service selection, rule ordering, architecture pattern), give the
  reasoning and the alternatives considered, not just the implementation.
- **Reuse existing internals; don't duplicate logic.** New tooling (notebooks,
  scripts, docs) should wrap `etl.config.load_settings`, `etl.io_uri`, etc.,
  not reimplement IO or config resolution.
- **State trade-offs plainly, then comply.** E.g. the deliberately public POC
  S3 bucket (`poc/infra/output-bucket.yaml`) — implement as asked, but say
  clearly what "public" means here and why it's scoped safe (lifecycle
  expiry, one-command teardown).
- **Draft irreversible actions; don't execute them unprompted.** `git push`,
  `gh pr create`, `aws cloudformation delete-stack`, deleting local files —
  prepare the exact command/draft and wait for explicit go-ahead.
- **Keep docs and diagrams in lockstep with actual repo state.** Re-verify
  against source when docs are edited or reordered; don't patch from memory.
- **Match deliverable format to purpose.** An interactive/theme-aware diagram
  (Artifact tool) and a flattened dependency-free image for `README.md`
  (`docs/architecture.svg` / `.png`) are different outputs with different
  constraints — don't mix them up.

## 2. Repository layout

```
.
├── README.md                  # setup, data pull, run instructions, architecture
├── Makefile                   # entry points — see below
├── poc_run_etl.sh              # env bootstrap + pipeline runner used by Makefile
├── requirements.txt            # hand-edited input deps (ranges)
├── requirements.lock.txt       # resolved, exact deps — do not hand-edit
├── docs/
│   ├── architecture.svg / .png # standalone repo-embeddable diagram
├── eda/
│   ├── 01_explore_raw.py/.ipynb
│   └── findings.md
└── poc/
    ├── .env / .env.example     # SOURCE_URI, TARGET_URI, AWS_REGION, etc.
    ├── data/                   # local mirror of raw + lake data (gitignored)
    ├── data_dictionary.md
    ├── etl/
    │   ├── config.py            # Settings, constants, load_settings() — single source of truth
    │   ├── io_uri.py             # fsspec-based URI-agnostic IO (local path == s3://)
    │   ├── bronze.py             # ingest: verbatim copy + lineage columns
    │   ├── silver.py             # 6 ordered data-quality rules -> clean tables + quarantine
    │   ├── gold.py               # DuckDB SQL orchestration -> analytics tables
    │   ├── quality.py            # Check dataclass / DQ framework
    │   └── run_etl.py            # CLI entrypoint
    ├── sql/                     # gold_*.sql (modelling) + analytics_*.sql (cohort queries)
    ├── infra/
    │   └── output-bucket.yaml   # CloudFormation: public, time-boxed POC output bucket
    ├── notebooks/
    │   ├── lake_client.py        # LAKE_SOURCE = "local"|"s3" toggle + list/load/describe helpers
    │   └── explore_lake.py/.ipynb
    ├── scripts/
    │   └── pull_data.sh          # aws s3 sync raw export -> poc/data/raw
    └── tests/
        └── test_silver_rules.py
```

## 3. Setup & running

Requirements: Python ≥ 3.10, bash, AWS CLI v2, and (for online mode) an AWS
account with credentials that can read the source bucket and read/write the
output bucket.

```bash
make help          # list all targets with descriptions
make pull          # sync the raw study export from S3 into poc/data/raw (needs AWS creds)
make offline       # full pipeline against the local mirror — default, no AWS needed
make etl           # full pipeline against S3 (fails on a broken invariant)
make smoke         # 4-participant local run covering unit/timezone/sentinel defects
make test          # unit tests for the Silver defect rules
make idempotency   # run twice, prove the second run changes nothing
make env           # show the virtualenv in use and what is installed
make recreate-env  # delete .venv and rebuild it from requirements.lock.txt
make activate      # print the command to activate the virtualenv in your own shell
make clean         # remove the local lake and generated reports
```

Direct CLI (`poc/etl/run_etl.py`) flags worth knowing:
`--online` / `--offline` (mutually exclusive, offline is the safe default),
`--source` / `--target` (override URIs), `--layers`, `--participants`
(comma-separated subset for smoke runs), `--run-id` (replay a prior run),
`--strict` (exit 1 on any failed pipeline assertion), `--quiet`.

Deploying the (intentionally public, POC-only) output bucket:

```bash
aws cloudformation deploy \
  --template-file poc/infra/output-bucket.yaml \
  --stack-name loka-test-poc \
  --capabilities CAPABILITY_IAM

aws cloudformation describe-stacks --stack-name loka-test-poc \
  --query 'Stacks[0].Outputs'

# teardown
aws cloudformation delete-stack --stack-name loka-test-poc
```

## 4. Key conventions & constants (`poc/etl/config.py`)

- Layer names: `BRONZE = "bronze"`, `SILVER = "silver"`, `GOLD = "gold"`,
  `QUARANTINE = "_quarantine"`, `DQ = "_dq"`.
- Study window: `STUDY_START/END` = 2026-01-08 → 2026-02-04 (28 days),
  `EXPECTED_PARTICIPANTS = 50`, `STUDY_TZ = UTC-05:00`.
- Defect handling constants: `STEPS_SENTINELS`, `HR_VALID_RANGE`,
  `UNIT_COLUMN_CANDIDATES`, `UNIT_RESCALE_DIVISOR`,
  `HR_STUCK_MIN_RUN_MINUTES`, `SLEEP_ANCHOR_TOLERANCE_SECONDS`,
  `COMPLETE_DAY_MIN_COVERAGE_PCT = 99.9`.
- ID/format regexes: `PARTICIPANT_ID_REGEX`, `DEVICE_ID_REGEX`,
  `FIRMWARE_REGEX`, `DEVICE_SERIAL_REGEX`.
- Env vars (`poc/.env`): `SOURCE_URI`, `TARGET_URI`, `SOURCE_URI_LOCAL`,
  `TARGET_URI_LOCAL`, `AWS_REGION`, `AWS_PROFILE`, `PARQUET_COMPRESSION`,
  `OUTPUT_DIR`. `load_settings(source, target, online)` is the single place
  that resolves which URIs are actually used — reuse it, never re-derive URIs.

## 5. Data quality model

`silver.py` applies **6 ordered, independent rules**, each documented as a
`Check` (`table, rule, severity, n_affected, detail, participants`) with
severities `error | warn | info | assert_fail`: timestamp normalization →
unit rescaling → null/invalid value flagging → deduplication (first-seen-wins)
→ stuck-sensor flagging → participant/device/survey/coverage table builds.
Rejected rows go to `_quarantine` with a `reject_reason`, never silently
dropped. `gold.py` builds 4 chained DuckDB SQL tables plus cohort analytics
on top of the cleaned Silver tables.

## 6. Notebooks (`poc/notebooks/`)

`lake_client.py` exposes one toggle — `LAKE_SOURCE = "local" | "s3"` — which
drives `config.load_settings(online=...)`. `list_tables()`, `load_table()`,
and `describe()` wrap the existing `io_uri` module; there is no separate IO
path for notebooks. `explore_lake.py` is written in jupytext `# %%` cell-marker
style and mirrors to `explore_lake.ipynb`.

## 7. What NOT to do

- Don't hand-edit `requirements.lock.txt` — it's generated.
- Don't invent column/table names — check `poc/sql/*.sql` or `config.py` first.
- Don't push to `origin`, open a PR, or delete the CloudFormation stack
  without explicit confirmation.
- Don't treat the public output bucket as a mistake to quietly "fix" —
  it's intentional and documented; flag it, don't silently lock it down.
- Don't reimplement URI resolution — always go through `load_settings`.
