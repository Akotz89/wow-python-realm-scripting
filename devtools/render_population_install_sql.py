#!/usr/bin/env python3
"""Render one fail-if-present MySQL transaction from a compiled install plan."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from realm_scripts.population_policy import PopulationPolicyError, render_population_install_sql
from realm_scripts.population_policy_v2 import render_population_install_sql_v2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path, help="compiled immutable population installation plan")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        raw = cast(Any, json.loads(args.plan.read_text(encoding="utf-8")))
        if not isinstance(raw, dict):
            raise PopulationPolicyError("installation plan must be a JSON object")
        plan = cast(dict[str, object], raw)
        content = (
            render_population_install_sql_v2(plan)
            if plan.get("installation_schema") == 2
            else render_population_install_sql(plan)
        )
        if args.output is None:
            sys.stdout.buffer.write(content)
        else:
            with args.output.open("xb") as output:
                output.write(content)
    except (OSError, json.JSONDecodeError, PopulationPolicyError) as exc:
        print(f"population install SQL: FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
