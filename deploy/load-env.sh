#!/usr/bin/env bash
# file: deploy/load-env.sh
# Source this from other deploy scripts. It loads deploy/fxtrader.env and derives common AWS values.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${FXTRADER_ENV_FILE:-${PROJECT_ROOT}/deploy/fxtrader.env}"

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
else
  echo "Missing env file: $ENV_FILE" >&2
  echo "Copy deploy/fxtrader.env.example to deploy/fxtrader.env and edit it." >&2
  return 1 2>/dev/null || exit 1
fi

export AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_PAGER="${AWS_PAGER:-}"
export CLUSTER_NAME="${CLUSTER_NAME:-fxtrader}"
export REPO_NAME="${REPO_NAME:-fxtrader}"
export IMAGE_TAG="${IMAGE_TAG:-latest}"
export SECRET_ID="${SECRET_ID:-fxtrader/oanda}"
export OANDA_ENV="${OANDA_ENV:-practice}"
export ENABLE_LIVE_TRADING="${ENABLE_LIVE_TRADING:-false}"
export SCHEDULE_NAME="${SCHEDULE_NAME:-fxtrader-m-f-0258-ny}"
export SCHEDULE_TIMEZONE="${SCHEDULE_TIMEZONE:-America/New_York}"
export SCHEDULE_CRON="${SCHEDULE_CRON:-cron(58 2 ? * MON-FRI *)}"

export AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
export IMAGE_URI="${IMAGE_URI:-${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${REPO_NAME}:${IMAGE_TAG}}"
export SECRET_ARN="${SECRET_ARN:-$(aws secretsmanager describe-secret --secret-id "$SECRET_ID" --region "$AWS_REGION" --query ARN --output text)}"

if [[ -z "${TASK_EXECUTION_ROLE_ARN:-}" ]]; then
  export TASK_EXECUTION_ROLE_ARN="$(aws iam get-role --role-name ecsTaskExecutionRole --query Role.Arn --output text)"
fi
