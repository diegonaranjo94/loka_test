.DEFAULT_GOAL := help
SHELL := /bin/bash

help:  ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS=":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

pull:  ## sync the raw study export from S3 into poc/data/raw (needs AWS creds)
	bash poc/scripts/pull_data.sh

etl:  ## THE command: full pipeline against S3, fails on a broken invariant
	./poc_run_etl.sh --online --strict

offline:  ## full pipeline against the local mirror (the default), no AWS
	./poc_run_etl.sh --offline --strict

smoke:  ## 4-participant local run covering the unit, timezone and sentinel defects
	./poc_run_etl.sh --participants P001,P005,P018,P029 --target ./data/lake_smoke

test:  ## unit tests for the Silver defect rules
	./poc_run_etl.sh --test

env:  ## show the virtualenv in use and what is installed in it
	./poc_run_etl.sh --env-info

recreate-env:  ## delete .venv and rebuild it from requirements.lock.txt
	./poc_run_etl.sh --recreate-env --env-info

activate:  ## print the command to activate the virtualenv in your own shell
	@echo "source .venv/bin/activate"

idempotency:  ## run twice, prove the second run changes nothing
	./poc_run_etl.sh --quiet
	find poc/data/lake/silver poc/data/lake/gold -name '*.parquet' | sort | xargs shasum -a 256 > /tmp/lake.1
	./poc_run_etl.sh --quiet
	find poc/data/lake/silver poc/data/lake/gold -name '*.parquet' | sort | xargs shasum -a 256 > /tmp/lake.2
	@diff /tmp/lake.1 /tmp/lake.2 && echo "OK: silver+gold byte-identical after re-run"

clean:  ## remove the local lake and generated reports
	rm -rf poc/data/lake poc/data/lake_smoke \
	       poc/outputs/analytical_query_output.md poc/outputs/dq_checks.csv \
	       poc/outputs/run_report.md poc/outputs/analytics_*.csv

.PHONY: help pull etl offline smoke test env recreate-env activate idempotency clean
