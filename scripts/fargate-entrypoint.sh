#!/usr/bin/env bash
# file: scripts/fargate-entrypoint.sh
set -euo pipefail

echo "===== $(date -Is) fxtrader fargate task start ====="
echo "Python: $(python --version)"
echo "OANDA_ENV=${OANDA_ENV:-practice}"
echo "FXTRADER_ENTRYPOINT=${FXTRADER_ENTRYPOINT:-src.trade_stream}"

# Use exec so Python receives container stop signals directly.
exec python -u -m "${FXTRADER_ENTRYPOINT:-src.trade_stream}"
