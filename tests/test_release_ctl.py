from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_release_ctl() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "devtools" / "release_ctl.py"
    spec = importlib.util.spec_from_file_location("release_ctl", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_ctl = _load_release_ctl()


def _bundle(tmp_path: Path, manifest: str) -> Path:
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / manifest.removesuffix(".json")
    subprocess.run(
        [
            sys.executable,
            str(root / "devtools" / "build_release.py"),
            "--manifest",
            str(root / "manifests" / manifest),
            "--output",
            str(output),
        ],
        check=True,
    )
    return output


def test_install_activate_and_rollback(tmp_path: Path) -> None:
    empty = _bundle(tmp_path, "empty.json")
    smoke = _bundle(tmp_path, "controlled-smoke.json")
    root = tmp_path / "runtime" / "python"

    assert release_ctl.install(empty, root) == "cpi223-empty-v1"
    assert release_ctl.install(smoke, root) == "cpi223-controlled-smoke-v1"
    release_ctl.activate("cpi223-empty-v1", root)
    release_ctl.activate("cpi223-controlled-smoke-v1", root)
    assert (root / "current").resolve().name == "cpi223-controlled-smoke-v1"
    assert (root / "previous").resolve().name == "cpi223-empty-v1"

    release_ctl.rollback(root)
    assert (root / "current").resolve().name == "cpi223-empty-v1"
    assert (root / "previous").resolve().name == "cpi223-controlled-smoke-v1"


def test_verify_rejects_tampered_file(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, "empty.json")
    manifest = bundle / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["release"] = "tampered"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(release_ctl.ReleaseError, match="mismatch"):
        release_ctl.verify(bundle)
