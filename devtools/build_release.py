#!/usr/bin/env python3
"""Build a reproducible, hash-inventoried realm-script release directory."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from realm_scripts.release_contract import canonical_json, read_manifest_file  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = PACKAGE_ROOT
    manifest = args.manifest.resolve(strict=True)
    output = args.output.resolve()

    data, release, _ = read_manifest_file(manifest)
    if output.exists():
        raise SystemExit(f"refusing to overwrite existing output: {output}")

    (output / "lib").mkdir(parents=True)
    shutil.copytree(
        root / "src" / "realm_scripts",
        output / "lib" / "realm_scripts",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (output / "engine-bootstrap").mkdir()
    shutil.copy2(root / "bootstrap" / "00_realm_bootstrap.py", output / "engine-bootstrap")
    (output / "manifest.json").write_bytes(canonical_json(data))

    files = sorted(path for path in output.rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    inventory = {
        "schema": 1,
        "release": release,
        "files": [
            {"path": path.relative_to(output).as_posix(), "sha256": sha256(path), "size": path.stat().st_size}
            for path in files
        ],
    }
    (output / "release-files.json").write_bytes(canonical_json(inventory))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
