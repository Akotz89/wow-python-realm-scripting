#!/usr/bin/env python3
"""Compile one canonical offline population-policy snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from realm_scripts.population_policy import PopulationPolicyError, canonical_json, compile_policy
from realm_scripts.population_policy_v2 import compile_policy_v2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        raw = cast(Any, json.loads(args.input.read_text(encoding="utf-8")))
        if not isinstance(raw, dict):
            raise PopulationPolicyError("policy input must be a JSON object")
        snapshot = compile_policy_v2(raw) if raw.get("policy_schema") == 2 else compile_policy(raw)
    except (OSError, json.JSONDecodeError, PopulationPolicyError) as exc:
        print(f"population policy: FAIL: {exc}", file=sys.stderr)
        return 1

    content = canonical_json(snapshot) + b"\n"
    try:
        if args.output is None:
            sys.stdout.buffer.write(content)
        else:
            with args.output.open("xb") as output:
                output.write(content)
    except OSError as exc:
        print(f"population policy: FAIL: cannot write output: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
