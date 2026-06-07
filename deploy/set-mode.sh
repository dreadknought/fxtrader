#!/usr/bin/env bash
# file: deploy/set-mode.sh
# Switches the scheduled ECS task between practice and live mode by registering a new task definition revision.
set -euo pipefail

MODE="${1:-}"
if [[ "$MODE" != "practice" && "$MODE" != "live" ]]; then
  echo "Usage: $0 practice|live" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${FXTRADER_ENV_FILE:-${SCRIPT_DIR}/fxtrader.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE" >&2
  exit 1
fi

if [[ "$MODE" == "live" ]]; then
  echo "You are about to enable LIVE OANDA trading for the scheduled Fargate task." >&2
  echo "Type exactly: ENABLE LIVE" >&2
  read -r CONFIRM
  if [[ "$CONFIRM" != "ENABLE LIVE" ]]; then
    echo "Aborted." >&2
    exit 1
  fi
  NEW_OANDA_ENV="live"
  NEW_ENABLE_LIVE_TRADING="true"
else
  NEW_OANDA_ENV="practice"
  NEW_ENABLE_LIVE_TRADING="false"
fi

python3 - "$ENV_FILE" "$NEW_OANDA_ENV" "$NEW_ENABLE_LIVE_TRADING" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
oanda_env = sys.argv[2]
enable_live = sys.argv[3]
text = path.read_text()

def upsert_export(src: str, name: str, value: str) -> str:
    line = f'export {name}={value}'
    pattern = rf'^export\s+{re.escape(name)}=.*$'
    if re.search(pattern, src, flags=re.MULTILINE):
        return re.sub(pattern, line, src, flags=re.MULTILINE)
    return src.rstrip() + "\n" + line + "\n"

text = upsert_export(text, "OANDA_ENV", oanda_env)
text = upsert_export(text, "ENABLE_LIVE_TRADING", enable_live)
path.write_text(text)
PY

# shellcheck source=deploy/load-env.sh
source "${SCRIPT_DIR}/load-env.sh"

TASK_DEF_ARN="$(${SCRIPT_DIR}/register-task-definition.sh)"
echo "Registered task definition: $TASK_DEF_ARN"

if [[ "${UPDATE_SCHEDULE:-true}" == "true" ]]; then
  "${SCRIPT_DIR}/create-or-update-schedule.sh"
fi

echo "Mode is now: $NEW_OANDA_ENV / ENABLE_LIVE_TRADING=$NEW_ENABLE_LIVE_TRADING"
