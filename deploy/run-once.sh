#!/usr/bin/env bash
# file: deploy/run-once.sh
# Starts one Fargate task immediately and prints its task ARN.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/load-env.sh
source "${SCRIPT_DIR}/load-env.sh"

: "${SUBNET_IDS:?Set SUBNET_IDS in deploy/fxtrader.env}"
: "${SECURITY_GROUP_ID:?Set SECURITY_GROUP_ID in deploy/fxtrader.env}"

TASK_DEF="${TASK_DEF:-fxtrader}"

aws ecs run-task \
  --region "$AWS_REGION" \
  --cluster "$CLUSTER_NAME" \
  --launch-type FARGATE \
  --task-definition "$TASK_DEF" \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET_IDS],securityGroups=[$SECURITY_GROUP_ID],assignPublicIp=ENABLED}" \
  --count 1 \
  --query 'tasks[0].taskArn' \
  --output text
