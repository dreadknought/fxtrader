#!/usr/bin/env python3
# file: deploy/render-task-definition.py
# Renders an ECS task definition from deploy/*.template.json using environment variables.

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from string import Template

REQUIRED_ENV_VARS = [
    "AWS_REGION",
    "IMAGE_URI",
    "SECRET_ARN",
    "TASK_EXECUTION_ROLE_ARN",
    "OANDA_ENV",
    "ENABLE_LIVE_TRADING",
]


def require_env() -> dict[str, str]:
    missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        print("Missing required env vars: " + ", ".join(missing), file=sys.stderr)
        sys.exit(1)
    return {name: os.environ[name] for name in REQUIRED_ENV_VARS}


def main() -> None:
    root = Path(__file__).resolve().parent
    values = require_env()

    container_template = (root / "container-definitions.template.json").read_text()
    container_text = Template(container_template).substitute(values)
    container_definitions = json.loads(container_text)

    task_template = (root / "task-definition.template.json").read_text()
    task_text = (
        task_template
        .replace("__TASK_EXECUTION_ROLE_ARN__", values["TASK_EXECUTION_ROLE_ARN"])
        .replace("__CONTAINER_DEFINITIONS__", json.dumps(container_definitions, indent=2))
    )
    task_definition = json.loads(task_text)
    print(json.dumps(task_definition, indent=2))


if __name__ == "__main__":
    main()
