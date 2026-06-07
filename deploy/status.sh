#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/fxtrader.env"

: "${AWS_REGION:?AWS_REGION is required}"
: "${CLUSTER_NAME:?CLUSTER_NAME is required}"
: "${REPO_NAME:=fxtrader}"
: "${IMAGE_TAG:=latest}"
: "${OANDA_ENV:=practice}"
: "${ENABLE_LIVE_TRADING:=false}"

AWS_ACCOUNT_ID="$(
  aws sts get-caller-identity \
    --query Account \
    --output text
)"

IMAGE_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${REPO_NAME}:${IMAGE_TAG}"

echo "AWS_ACCOUNT_ID=$AWS_ACCOUNT_ID"
echo "AWS_REGION=$AWS_REGION"
echo "CLUSTER_NAME=$CLUSTER_NAME"
echo "IMAGE_URI=$IMAGE_URI"
echo "OANDA_ENV=$OANDA_ENV"
echo "ENABLE_LIVE_TRADING=$ENABLE_LIVE_TRADING"

echo
echo "Latest task definition:"
aws ecs describe-task-definition \
  --region "$AWS_REGION" \
  --task-definition fxtrader \
  --query '{
    arn: taskDefinition.taskDefinitionArn,
    image: taskDefinition.containerDefinitions[0].image,
    env: taskDefinition.containerDefinitions[0].environment
  }' \
  --output json

echo
echo "Schedule:"
aws scheduler get-schedule \
  --region "$AWS_REGION" \
  --name fxtrader-m-f-0258-ny \
  --query '{
    name: Name,
    state: State,
    expression: ScheduleExpression,
    timezone: ScheduleExpressionTimezone,
    targetTask: Target.EcsParameters.TaskDefinitionArn
  }' \
  --output json 2>/dev/null || echo "WARNING: Schedule not found or unreadable"

echo
echo "Scheduler IAM policy:"
SCHEDULER_POLICY_JSON="$(
  aws iam get-role-policy \
    --role-name fxtrader-eventbridge-scheduler-role \
    --policy-name fxtrader-run-ecs-task \
    --query PolicyDocument \
    --output json 2>/dev/null || true
)"

if [[ -z "$SCHEDULER_POLICY_JSON" || "$SCHEDULER_POLICY_JSON" == "null" ]]; then
  echo "WARNING: Could not read scheduler IAM policy fxtrader-run-ecs-task"
else
  echo "$SCHEDULER_POLICY_JSON" | jq .

  EXPECTED_TASK_RESOURCE="arn:aws:ecs:${AWS_REGION}:${AWS_ACCOUNT_ID}:task-definition/fxtrader:*"

  ACTUAL_TASK_RESOURCES="$(
    echo "$SCHEDULER_POLICY_JSON" \
      | jq -r '.Statement[]
        | select(
            (.Action == "ecs:RunTask")
            or
            ((.Action | type) == "array" and (.Action | index("ecs:RunTask")))
          )
        | .Resource'
  )"

  if echo "$ACTUAL_TASK_RESOURCES" | grep -Fxq "$EXPECTED_TASK_RESOURCE"; then
    echo "Scheduler RunTask permission: OK"
  else
    echo "WARNING: Scheduler RunTask permission may be pinned to a specific task revision."
    echo "Expected: $EXPECTED_TASK_RESOURCE"
    echo "Actual:"
    echo "$ACTUAL_TASK_RESOURCES"
  fi
fi

echo
echo "Recent stopped tasks:"
TASKS="$(
  aws ecs list-tasks \
    --region "$AWS_REGION" \
    --cluster "$CLUSTER_NAME" \
    --desired-status STOPPED \
    --family fxtrader \
    --max-results 10 \
    --query 'taskArns' \
    --output text 2>/dev/null || true
)"

if [[ -z "$TASKS" ]]; then
  echo "No recent stopped fxtrader tasks found."
else
  aws ecs describe-tasks \
    --region "$AWS_REGION" \
    --cluster "$CLUSTER_NAME" \
    --tasks $TASKS \
    --query 'tasks[].{
      created: createdAt,
      stopped: stoppedAt,
      last: lastStatus,
      exit: containers[0].exitCode,
      reason: stoppedReason,
      task: taskArn
    }' \
    --output table
fi
