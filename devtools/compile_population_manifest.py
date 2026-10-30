#!/usr/bin/env python3
"""Compile one canonical immutable population identity manifest offline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from realm_scripts.population_policy import (
    PopulationPolicyError,
    canonical_json,
    compile_population_manifest,
)


def _read_object(path: Path, name: str) -> dict[str, object]:
    raw = cast(Any, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise PopulationPolicyError(f"{name} must be a JSON object")
    return cast(dict[str, object], raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    schedule_source = parser.add_mutually_exclusive_group(required=True)
    schedule_source.add_argument("--schedule", type=Path, help="standalone compiled schedule")
    schedule_source.add_argument("--policy", type=Path, help="compiled policy containing its schedule")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        manifest_input = _read_object(args.input, "manifest input")
        if args.policy is not None:
            policy = _read_object(args.policy, "compiled policy")
            schedule_value = policy.get("schedule")
            if not isinstance(schedule_value, dict):
                raise PopulationPolicyError("compiled policy schedule must be a JSON object")
            schedule = cast(dict[str, object], schedule_value)
        else:
            if args.schedule is None:
                raise AssertionError("argparse did not provide a schedule source")
            schedule = _read_object(args.schedule, "compiled schedule")
        manifest = compile_population_manifest(manifest_input, schedule)
        content = canonical_json(manifest) + b"\n"
        if args.output is None:
            sys.stdout.buffer.write(content)
        else:
            with args.output.open("xb") as output:
                output.write(content)
    except (OSError, json.JSONDecodeError, PopulationPolicyError) as exc:
        print(f"population manifest: FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
