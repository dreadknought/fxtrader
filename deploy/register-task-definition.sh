#!/usr/bin/env bash
# file: deploy/register-task-definition.sh
# Renders and registers a new ECS task definition revision.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/load-env.sh
source "${SCRIPT_DIR}/load-env.sh"
cd "${SCRIPT_DIR}/.."

TMP_JSON="${TMPDIR:-/tmp}/fxtrader-task-definition.json"
python deploy/render-task-definition.py > "$TMP_JSON"

TASK_DEFINITION_ARN="$(aws ecs register-task-definition \
  --region "$AWS_REGION" \
  --cli-input-json "file://${TMP_JSON}" \
  --query 'taskDefinition.taskDefinitionArn' \
  --output text)"

printf '%s\n' "$TASK_DEFINITION_ARN"
