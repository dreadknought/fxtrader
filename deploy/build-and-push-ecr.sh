#!/usr/bin/env bash
# file: deploy/build-and-push-ecr.sh
# Builds the Docker image and pushes it to ECR. Only the final IMAGE_URI is printed to stdout.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/load-env.sh
source "${SCRIPT_DIR}/load-env.sh"
cd "${SCRIPT_DIR}/.."

aws ecr describe-repositories --repository-names "$REPO_NAME" --region "$AWS_REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$REPO_NAME" --region "$AWS_REGION" >/dev/null

aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com" >&2

docker build -t "${REPO_NAME}:${IMAGE_TAG}" . >&2
docker tag "${REPO_NAME}:${IMAGE_TAG}" "$IMAGE_URI"
docker push "$IMAGE_URI" >&2

printf '%s\n' "$IMAGE_URI"
