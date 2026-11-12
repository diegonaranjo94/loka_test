#!/usr/bin/env bash
# Deploy the MCP side project. Run from the repo root.
#
#   poc/scripts/deploy_mcp.sh ecr       # stack 1: ECR repository (once)
#   poc/scripts/deploy_mcp.sh push      # build + push the image, tag = git sha
#   poc/scripts/deploy_mcp.sh server    # stack 2: role, secret, Lambda, Function URL
#   poc/scripts/deploy_mcp.sh all       # ecr -> push -> server
#   poc/scripts/deploy_mcp.sh url       # print the connector URL (contains the token)
#   poc/scripts/deploy_mcp.sh destroy   # delete server then ECR stacks (bucket untouched)
#
# Config via environment (or poc/.env):
#   DATA_BUCKET   required for `server` - pipeline output bucket name
#   AWS_REGION    default us-east-1
#   AWS_PROFILE   optional
#   PROJECT_NAME  default loka-test-mcp
#   ARCH          default arm64 (must match the template's Architecture)
#   IMAGE_TAG     default: git short sha (+ timestamp if the tree is dirty)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INFRA="$ROOT/poc/infra"
APP_DIR="$ROOT/poc/mcp"          # Dockerfile + server code (next step)

# Load poc/.env without letting blank AWS_* vars break the CLI
# (same gotcha the ETL hit with botocore).
if [[ -f "$ROOT/poc/.env" ]]; then
  set -a; source "$ROOT/poc/.env"; set +a
  for v in AWS_PROFILE AWS_REGION AWS_DEFAULT_REGION; do
    [[ -z "${!v-}" ]] && unset "$v"
  done
fi

REGION="${AWS_REGION:-us-east-1}"
PROJECT="${PROJECT_NAME:-loka-test-mcp}"
ARCH="${ARCH:-arm64}"
ECR_STACK="${PROJECT}-ecr"
SERVER_STACK="${PROJECT}-server"
export AWS_REGION="$REGION" AWS_PAGER=""

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

stack_output() {  # stack key
  aws cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text
}

image_tag() {
  if [[ -n "${IMAGE_TAG:-}" ]]; then echo "$IMAGE_TAG"; return; fi
  local sha; sha="$(git -C "$ROOT" rev-parse --short=12 HEAD 2>/dev/null || echo nogit)"
  # Tags are immutable in ECR: a dirty tree gets a unique suffix so a rebuild
  # of uncommitted changes never collides with an existing tag.
  if [[ -n "$(git -C "$ROOT" status --porcelain 2>/dev/null)" ]]; then
    sha="${sha}-dirty-$(date -u +%Y%m%d%H%M%S)"
  fi
  echo "$sha"
}

deploy_ecr() {
  log "Deploying $ECR_STACK ($REGION)"
  aws cloudformation deploy \
    --stack-name "$ECR_STACK" \
    --template-file "$INFRA/mcp-ecr.yaml" \
    --parameter-overrides ProjectName="$PROJECT" \
    --tags Project="$PROJECT" Purpose=mcp-side-project ManagedBy=cloudformation \
    --no-fail-on-empty-changeset
}

push_image() {
  [[ -f "$APP_DIR/Dockerfile" ]] || die "no Dockerfile in $APP_DIR yet - server code is the next step"
  local repo tag registry
  repo="$(stack_output "$ECR_STACK" RepositoryUri)"
  [[ -n "$repo" && "$repo" != "None" ]] || die "ECR stack not found - run: $0 ecr"
  tag="$(image_tag)"
  registry="${repo%%/*}"

  log "Logging in to $registry"
  aws ecr get-login-password | docker login --username AWS --password-stdin "$registry" >/dev/null

  # --provenance=false: Lambda rejects the OCI image index buildx creates for
  # attestations; it needs a single-platform Docker v2 manifest.
  log "Building $repo:$tag (linux/$ARCH)"
  docker buildx build \
    --platform "linux/$ARCH" \
    --provenance=false \
    --tag "$repo:$tag" \
    --push \
    "$APP_DIR"

  echo "$tag" > "$INFRA/.mcp-image-tag"
  log "Pushed $repo:$tag"
}

deploy_server() {
  [[ -n "${DATA_BUCKET:-}" ]] || die "DATA_BUCKET is not set"
  local tag="${IMAGE_TAG:-}"
  [[ -z "$tag" && -f "$INFRA/.mcp-image-tag" ]] && tag="$(cat "$INFRA/.mcp-image-tag")"
  [[ -n "$tag" ]] || die "no image tag - run: $0 push (or set IMAGE_TAG)"

  log "Deploying $SERVER_STACK with image tag $tag"
  aws cloudformation deploy \
    --stack-name "$SERVER_STACK" \
    --template-file "$INFRA/mcp-server.yaml" \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides \
        ProjectName="$PROJECT" \
        ImageTag="$tag" \
        DataBucketName="$DATA_BUCKET" \
        Architecture="$ARCH" \
    --tags Project="$PROJECT" Purpose=mcp-side-project ManagedBy=cloudformation \
    --no-fail-on-empty-changeset

  log "Function URL: $(stack_output "$SERVER_STACK" FunctionUrl)"
  log "Connector URL (with token): $0 url"
}

print_url() {
  local base secret token
  base="$(stack_output "$SERVER_STACK" FunctionUrl)"
  secret="$(stack_output "$SERVER_STACK" UrlTokenSecretArn)"
  token="$(aws secretsmanager get-secret-value --secret-id "$secret" --query SecretString --output text)"
  # base already ends with "/"
  echo "${base}${token}/mcp"
}

destroy() {
  log "Deleting $SERVER_STACK"
  aws cloudformation delete-stack --stack-name "$SERVER_STACK"
  aws cloudformation wait stack-delete-complete --stack-name "$SERVER_STACK"
  log "Deleting $ECR_STACK"
  aws cloudformation delete-stack --stack-name "$ECR_STACK"
  aws cloudformation wait stack-delete-complete --stack-name "$ECR_STACK"
  rm -f "$INFRA/.mcp-image-tag"
  log "Done. The data bucket was not touched."
}

case "${1:-}" in
  ecr)     deploy_ecr ;;
  push)    push_image ;;
  server)  deploy_server ;;
  all)     deploy_ecr; push_image; deploy_server ;;
  url)     print_url ;;
  destroy) destroy ;;
  *) sed -n '2,16p' "$0"; exit 1 ;;
esac
