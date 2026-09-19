#!/usr/bin/env bash
# Pull the raw study data from S3 to the local POC workspace.
# Run this on your Mac (the sandbox/VM has no egress to *.amazonaws.com):
#   bash poc/scripts/pull_data.sh
# Idempotent: `aws s3 sync` only transfers what changed.
set -euo pipefail

BUCKET="de-tech-assessment-396587179375-us-east-1-an"
PREFIX="data/"
POC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RAW_DIR="$POC_DIR/data/raw"
MANIFEST="$POC_DIR/data/bucket_manifest.txt"

mkdir -p "$RAW_DIR"

echo ">> [1/4] identity check"
aws sts get-caller-identity --output text || {
  echo "!! no usable AWS credentials; configure a profile and re-run" >&2; exit 1; }

echo ">> [2/4] full bucket manifest -> $MANIFEST"
aws s3 ls "s3://${BUCKET}/" --recursive --human-readable --summarize | tee "$MANIFEST" | tail -5

echo ">> [3/4] sync s3://${BUCKET}/${PREFIX} -> $RAW_DIR"
aws s3 sync "s3://${BUCKET}/${PREFIX}" "$RAW_DIR/" --exact-timestamps

echo ">> [4/4] grab any markdown docs shipped in the bucket (data dictionary)"
# every *.md key in the bucket, flattened into poc/
awk '{print $NF}' "$MANIFEST" | grep -i '\.md$' | while read -r key; do
  echo "   cp $key"
  aws s3 cp "s3://${BUCKET}/${key}" "$POC_DIR/$(basename "$key")"
done || true

echo
echo "== local landing summary =="
find "$RAW_DIR" -type f | sed "s|$RAW_DIR/||" | awk -F/ '{print $1"/"$2}' | sort | uniq -c | sort -rn | head -20
echo "files: $(find "$RAW_DIR" -type f | wc -l | tr -d ' ')   size: $(du -sh "$RAW_DIR" | cut -f1)"
