#!/usr/bin/env python3
"""Compile a fail-closed first-wave schedule/manifest/factory equality proof."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from realm_scripts.population_first_wave_coupling import compile_first_wave_coupling
from realm_scripts.population_policy import PopulationPolicyError, canonical_json


def _read_object(path: Path, name: str) -> dict[str, object]:
    raw = cast(Any, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise PopulationPolicyError(f"{name} must be a JSON object")
    return cast(dict[str, object], raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="reviewed coupling input")
    parser.add_argument("--schedule", type=Path, required=True, help="compiled schema-2 schedule")
    parser.add_argument("--manifest", type=Path, required=True, help="compiled population manifest")
    parser.add_argument("--bindings", type=Path, help="compiled bindings, required only for observation")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        raw = _read_object(args.input, "coupling input")
        schedule = _read_object(args.schedule, "compiled schedule")
        manifest = _read_object(args.manifest, "compiled manifest")
        bindings = _read_object(args.bindings, "compiled bindings") if args.bindings is not None else None
        result = compile_first_wave_coupling(raw, schedule, manifest, bindings)
        content = canonical_json(result) + b"\n"
        if args.output is None:
            sys.stdout.buffer.write(content)
        else:
            with args.output.open("xb") as output:
                output.write(content)
    except (OSError, json.JSONDecodeError, PopulationPolicyError) as exc:
        print(f"population first-wave coupling: FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
