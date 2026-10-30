#!/usr/bin/env python3
"""Compile the immutable hash-bound database seed for the population director."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from realm_scripts.population_policy import (
    PopulationPolicyError,
    canonical_json,
    compile_population_install_plan,
)
from realm_scripts.population_policy_v2 import compile_population_install_plan_v2


def _read_object(path: Path, name: str) -> dict[str, object]:
    raw = cast(Any, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise PopulationPolicyError(f"{name} must be a JSON object")
    return cast(dict[str, object], raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="reviewed installation version input")
    parser.add_argument("--snapshot", type=Path, required=True, help="compiled empty-binding runtime snapshot")
    parser.add_argument("--policy", type=Path, required=True, help="compiled policy and schedule")
    parser.add_argument("--manifest", type=Path, required=True, help="compiled immutable identity manifest")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        installation_input = _read_object(args.input, "installation input")
        snapshot = _read_object(args.snapshot, "compiled runtime snapshot")
        policy = _read_object(args.policy, "compiled policy")
        manifest = _read_object(args.manifest, "compiled manifest")
        plan = (
            compile_population_install_plan_v2(installation_input, snapshot, policy, manifest)
            if installation_input.get("installation_schema") == 2
            else compile_population_install_plan(installation_input, snapshot, policy, manifest)
        )
        content = canonical_json(plan) + b"\n"
        if args.output is None:
            sys.stdout.buffer.write(content)
        else:
            with args.output.open("xb") as output:
                output.write(content)
    except (OSError, json.JSONDecodeError, PopulationPolicyError) as exc:
        print(f"population install plan: FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
