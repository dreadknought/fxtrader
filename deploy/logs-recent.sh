#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/fxtrader.env"

: "${AWS_REGION:?AWS_REGION is required}"
: "${LOG_GROUP_NAME:=/ecs/fxtrader}"

SINCE="${1:-30m}"

aws logs tail "$LOG_GROUP_NAME" \
  --region "$AWS_REGION" \
  --since "$SINCE"
