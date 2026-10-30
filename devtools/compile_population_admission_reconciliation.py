#!/usr/bin/env python3
"""Compile one deterministic offline population-admission reconciliation plan."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from realm_scripts.population_policy import (
    PopulationPolicyError,
    canonical_json,
    compile_admission_reconciliation,
)
from realm_scripts.population_policy_v2 import compile_admission_reconciliation_v2


def _read_object(path: Path, name: str) -> dict[str, object]:
    raw = cast(Any, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise PopulationPolicyError(f"{name} must be a JSON object")
    return cast(dict[str, object], raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="reviewed durable owner and capacity observation")
    parser.add_argument("--snapshot", type=Path, required=True, help="compiled runtime snapshot")
    parser.add_argument("--manifest", type=Path, required=True, help="compiled immutable identity manifest")
    parser.add_argument("--bindings", type=Path, required=True, help="compiled current database bindings")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        reconciliation_input = _read_object(args.input, "admission reconciliation input")
        snapshot = _read_object(args.snapshot, "compiled runtime snapshot")
        manifest = _read_object(args.manifest, "compiled manifest")
        bindings = _read_object(args.bindings, "compiled bindings")
        plan = (
            compile_admission_reconciliation_v2(reconciliation_input, snapshot, manifest, bindings)
            if reconciliation_input.get("reconciliation_schema") == 2
            else compile_admission_reconciliation(reconciliation_input, snapshot, manifest, bindings)
        )
        content = canonical_json(plan) + b"\n"
        if args.output is None:
            sys.stdout.buffer.write(content)
        else:
            with args.output.open("xb") as output:
                output.write(content)
    except (OSError, json.JSONDecodeError, PopulationPolicyError) as exc:
        print(f"population admission reconciliation: FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
