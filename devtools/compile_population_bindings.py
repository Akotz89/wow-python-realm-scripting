#!/usr/bin/env python3
"""Compile admitted character bindings with deterministic shared account IDs offline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from realm_scripts.population_policy import (
    PopulationPolicyError,
    canonical_json,
    compile_identity_bindings,
)


def _read_object(path: Path, name: str) -> dict[str, object]:
    raw = cast(Any, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise PopulationPolicyError(f"{name} must be a JSON object")
    return cast(dict[str, object], raw)


def _read_bindings(path: Path) -> list[dict[str, object]]:
    raw = cast(Any, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
        raise PopulationPolicyError("bindings input must be a JSON array of objects")
    return cast(list[dict[str, object]], raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="sorted admitted-identity binding rows")
    parser.add_argument("--manifest", type=Path, required=True, help="compiled immutable identity manifest")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        bindings_input = _read_bindings(args.input)
        manifest = _read_object(args.manifest, "compiled manifest")
        bindings = compile_identity_bindings(bindings_input, manifest)
        content = canonical_json(bindings) + b"\n"
        if args.output is None:
            sys.stdout.buffer.write(content)
        else:
            with args.output.open("xb") as output:
                output.write(content)
    except (OSError, json.JSONDecodeError, PopulationPolicyError) as exc:
        print(f"population bindings: FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
