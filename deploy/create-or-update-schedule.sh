#!/usr/bin/env bash
# file: deploy/create-or-update-schedule.sh
# Creates or updates the EventBridge Scheduler entry for the latest fxtrader task definition.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/load-env.sh
source "${SCRIPT_DIR}/load-env.sh"

: "${SUBNET_IDS:?Set SUBNET_IDS in deploy/fxtrader.env}"
: "${SECURITY_GROUP_ID:?Set SECURITY_GROUP_ID in deploy/fxtrader.env}"

if [[ -z "${SCHEDULER_ROLE_ARN:-}" ]]; then
  export SCHEDULER_ROLE_ARN="$(aws iam get-role --role-name fxtrader-eventbridge-scheduler-role --query Role.Arn --output text)"
fi

TASK_DEFINITION_ARN="$(aws ecs describe-task-definition \
  --region "$AWS_REGION" \
  --task-definition fxtrader \
  --query 'taskDefinition.taskDefinitionArn' \
  --output text)"

CLUSTER_ARN="$(aws ecs describe-clusters \
  --region "$AWS_REGION" \
  --clusters "$CLUSTER_NAME" \
  --query 'clusters[0].clusterArn' \
  --output text)"

SUBNETS_JSON="$(printf '%s' "$SUBNET_IDS" | awk -F, '{ printf "["; for (i=1;i<=NF;i++) { printf "%s\"%s\"", (i>1?",":""), $i } printf "]" }')"
TARGET_JSON="${TMPDIR:-/tmp}/fxtrader-scheduler-target.json"

cat > "$TARGET_JSON" <<EOF_TARGET
{
  "Arn": "$CLUSTER_ARN",
  "RoleArn": "$SCHEDULER_ROLE_ARN",
  "EcsParameters": {
    "TaskDefinitionArn": "$TASK_DEFINITION_ARN",
    "LaunchType": "FARGATE",
    "NetworkConfiguration": {
      "awsvpcConfiguration": {
        "Subnets": $SUBNETS_JSON,
        "SecurityGroups": ["$SECURITY_GROUP_ID"],
        "AssignPublicIp": "ENABLED"
      }
    }
  }
}
EOF_TARGET

if aws scheduler get-schedule --region "$AWS_REGION" --name "$SCHEDULE_NAME" >/dev/null 2>&1; then
  aws scheduler update-schedule \
    --region "$AWS_REGION" \
    --name "$SCHEDULE_NAME" \
    --schedule-expression "$SCHEDULE_CRON" \
    --schedule-expression-timezone "$SCHEDULE_TIMEZONE" \
    --flexible-time-window '{"Mode":"OFF"}' \
    --target "file://${TARGET_JSON}" >/dev/null
  echo "Updated schedule $SCHEDULE_NAME -> $TASK_DEFINITION_ARN"
else
  aws scheduler create-schedule \
    --region "$AWS_REGION" \
    --name "$SCHEDULE_NAME" \
    --schedule-expression "$SCHEDULE_CRON" \
    --schedule-expression-timezone "$SCHEDULE_TIMEZONE" \
    --flexible-time-window '{"Mode":"OFF"}' \
    --target "file://${TARGET_JSON}" >/dev/null
  echo "Created schedule $SCHEDULE_NAME -> $TASK_DEFINITION_ARN"
fi
