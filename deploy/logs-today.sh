#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/fxtrader.env"

: "${AWS_REGION:?AWS_REGION is required}"
: "${LOG_GROUP_NAME:=/ecs/fxtrader}"

# At 9 AM ET, 12h safely covers today's 2:58 AM run.
# This is simpler and avoids GNU date/timezone weirdness.
aws logs tail "$LOG_GROUP_NAME" \
  --region "$AWS_REGION" \
  --since 12h
