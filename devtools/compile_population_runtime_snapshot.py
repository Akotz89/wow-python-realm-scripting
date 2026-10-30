#!/usr/bin/env python3
"""Compile the versioned policy envelope consumed by the future C++ director."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from realm_scripts.population_policy import (
    PopulationPolicyError,
    canonical_json,
    compile_runtime_snapshot,
)
from realm_scripts.population_policy_v2 import compile_runtime_snapshot_v2


def _read_object(path: Path, name: str) -> dict[str, object]:
    raw = cast(Any, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise PopulationPolicyError(f"{name} must be a JSON object")
    return cast(dict[str, object], raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="reviewed runtime limits and hold whitelist")
    parser.add_argument("--policy", type=Path, required=True, help="compiled policy snapshot")
    parser.add_argument("--manifest", type=Path, required=True, help="compiled immutable identity manifest")
    parser.add_argument("--bindings", type=Path, required=True, help="compiled admitted-identity DB bindings")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        runtime_input = _read_object(args.input, "runtime snapshot input")
        policy = _read_object(args.policy, "compiled policy")
        manifest = _read_object(args.manifest, "compiled manifest")
        bindings = _read_object(args.bindings, "compiled bindings")
        snapshot = (
            compile_runtime_snapshot_v2(runtime_input, policy, manifest, bindings)
            if runtime_input.get("snapshot_schema") == 2
            else compile_runtime_snapshot(runtime_input, policy, manifest, bindings)
        )
        content = canonical_json(snapshot) + b"\n"
        if args.output is None:
            sys.stdout.buffer.write(content)
        else:
            with args.output.open("xb") as output:
                output.write(content)
    except (OSError, json.JSONDecodeError, PopulationPolicyError) as exc:
        print(f"population runtime snapshot: FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
