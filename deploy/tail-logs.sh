#!/usr/bin/env bash
# file: deploy/tail-logs.sh
# Follows recent CloudWatch logs. Usage: SINCE=6h ./deploy/tail-logs.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/load-env.sh
source "${SCRIPT_DIR}/load-env.sh"

LOG_GROUP="${LOG_GROUP:-/ecs/fxtrader}"
SINCE="${SINCE:-3h}"

aws logs tail "$LOG_GROUP" \
  --region "$AWS_REGION" \
  --since "$SINCE" \
  --follow
