#!/usr/bin/env python3
"""Compile one deterministic per-identity admission recovery action."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from realm_scripts.population_policy import (
    PopulationPolicyError,
    canonical_json,
    compile_admission_recovery,
)
from realm_scripts.population_policy_v2 import compile_admission_recovery_v2


def _read_object(path: Path, name: str) -> dict[str, object]:
    raw = cast(Any, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise PopulationPolicyError(f"{name} must be a JSON object")
    return cast(dict[str, object], raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="reviewed ledger and cross-pool observation")
    parser.add_argument("--reconciliation", type=Path, required=True, help="compiled admission reconciliation")
    parser.add_argument("--manifest", type=Path, required=True, help="compiled immutable identity manifest")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        recovery_input = _read_object(args.input, "admission recovery input")
        reconciliation = _read_object(args.reconciliation, "compiled admission reconciliation")
        manifest = _read_object(args.manifest, "compiled manifest")
        recovery = (
            compile_admission_recovery_v2(recovery_input, reconciliation, manifest)
            if recovery_input.get("recovery_schema") == 2
            else compile_admission_recovery(recovery_input, reconciliation, manifest)
        )
        content = canonical_json(recovery) + b"\n"
        if args.output is None:
            sys.stdout.buffer.write(content)
        else:
            with args.output.open("xb") as output:
                output.write(content)
    except (OSError, json.JSONDecodeError, PopulationPolicyError) as exc:
        print(f"population admission recovery: FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
