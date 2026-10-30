#!/usr/bin/env python3
"""Compile one audited v2 owner binding compare-and-swap transition."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from realm_scripts.population_policy import PopulationPolicyError, canonical_json
from realm_scripts.population_policy_v2 import compile_owner_binding_transition_v2


def _read_object(path: Path, name: str) -> dict[str, object]:
    raw = cast(Any, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise PopulationPolicyError(f"{name} must be a JSON object")
    return cast(dict[str, object], raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="reviewed owner selection and current owner state")
    parser.add_argument("--installation", type=Path, required=True, help="compiled v2 installation plan")
    parser.add_argument("--ambient-bindings", type=Path, required=True, help="current RandomBot identity bindings")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        binding_input = _read_object(args.input, "owner binding input")
        installation = _read_object(args.installation, "installation plan")
        ambient_bindings = _read_object(args.ambient_bindings, "ambient bindings")
        transition = compile_owner_binding_transition_v2(binding_input, installation, ambient_bindings)
        content = canonical_json(transition) + b"\n"
        if args.output is None:
            sys.stdout.buffer.write(content)
        else:
            with args.output.open("xb") as output:
                output.write(content)
    except (OSError, json.JSONDecodeError, PopulationPolicyError) as exc:
        print(f"population owner binding: FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
