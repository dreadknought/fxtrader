#!/usr/bin/env bash
# file: deploy/check-local-tools.sh
set -euo pipefail

command -v aws >/dev/null || { echo "Missing aws CLI" >&2; exit 1; }
command -v docker >/dev/null || { echo "Missing docker" >&2; exit 1; }

aws --version
docker --version
aws sts get-caller-identity

echo "OK: aws CLI and docker are available, and AWS credentials work."
