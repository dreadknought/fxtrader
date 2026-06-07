#!/usr/bin/env bash
# file: deploy/deploy-image.sh
# Build/push image, register a new task definition, and update the schedule to use it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_URI="$(${SCRIPT_DIR}/build-and-push-ecr.sh)"
export IMAGE_URI
TASK_DEF_ARN="$(${SCRIPT_DIR}/register-task-definition.sh)"
echo "Registered task definition: $TASK_DEF_ARN"
"${SCRIPT_DIR}/create-or-update-schedule.sh"
