#!/usr/bin/env python3
"""Compile or evaluate an offline population-capacity policy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from realm_scripts.population_capacity_policy import (
    classify_capacity_observation,
    compile_capacity_policy,
)


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if args.policy is None:
        result = compile_capacity_policy(_load(args.input))
    else:
        result = classify_capacity_observation(_load(args.input), _load(args.policy))
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
