#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/root/dev/fxtrader"
ENV_FILE="$REPO_DIR/.env.trade"
VENV_PY="$REPO_DIR/.venv/bin/python"
LOG_DIR="$REPO_DIR/out/logs"
LOCK_FILE="/tmp/fxtrader_trade_stream.lock"

mkdir -p "$LOG_DIR"
cd "$REPO_DIR"

# Load env explicitly (cron/task scheduler safe)
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
else
  echo "$(date -Is) ERROR: env file missing: $ENV_FILE" >&2
  exit 2
fi

# Prevent overlap
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "$(date -Is) another fxtrader instance is running; exiting."
  exit 0
fi

{
  echo
  echo "===== $(date -Is) fxtrader run start ====="
  "$VENV_PY" -m src.trade_stream
  echo "===== $(date -Is) fxtrader run end ====="
} 2>&1 | tee -a "$LOG_DIR/trade_stream_aggregated.log" >> "$LOG_DIR/trade_stream_$(date +%F).log"
