"""``settings.read@1`` — a local skill that reads one of its settings from the environment."""

import os
from typing import Any


def run(inputs: dict[str, Any]) -> dict[str, Any]:
    return {"value": os.environ.get(str(inputs["name"]))}
