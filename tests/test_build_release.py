from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

from realm_scripts.release_contract import canonical_json


def test_release_builder_excludes_bytecode_and_inventories_every_file(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "release"
    subprocess.run(
        [
            sys.executable,
            str(root / "devtools" / "build_release.py"),
            "--manifest",
            str(root / "manifests" / "empty.json"),
            "--output",
            str(output),
        ],
        check=True,
    )

    shipped = sorted(path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file())
    assert not any("__pycache__" in path or path.endswith(".pyc") for path in shipped)

    raw: Any = json.loads((output / "release-files.json").read_text(encoding="utf-8"))
    inventory = cast(dict[str, Any], raw)
    inventoried = sorted(item["path"] for item in inventory["files"])
    assert shipped == sorted([*inventoried, "release-files.json"])

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert (output / "manifest.json").read_bytes() == canonical_json(manifest)
