from __future__ import annotations

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest

from realm_scripts.release_contract import (
    ContractError,
    expected_source_contract,
    historical_input,
    validate_manifest_data,
    validate_source_contract,
)

HISTORICAL_INPUTS = {
    "WOW_CPI223_AUTHORITY": ("--authority", "research/cpi-225/cpi225-reconciled-disposition-20260903.json"),
    "WOW_CPI223_SURFACE_BUNDLE": ("--surface-bundle", "research/evidence/cpi-225/cpi225-release-surface-20260903"),
    "WOW_CPI223_ENGINE_PATCH": ("--engine-patch", "research/cpi-223/patches/mod-python-engine-hardening.patch"),
}


def historical_input_paths(project: Path) -> dict[str, Path]:
    paths = {flag: Path(os.environ.get(name, project / default)) for name, (flag, default) in HISTORICAL_INPUTS.items()}
    for name, archive in (("WOW_CPI223_SURFACE_BUNDLE", "cpi225-release-surface-20260903"),
                          ("WOW_CPI223_ENGINE_PATCH", "mod-python-engine-hardening.patch")):
        flag = HISTORICAL_INPUTS[name][0]
        paths[flag] = historical_input(project, paths[flag], archive, paths[flag] if name in os.environ else None)
    return paths


def test_historical_input_paths_default_and_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in HISTORICAL_INPUTS:
        monkeypatch.delenv(name, raising=False)
    default = historical_input_paths(tmp_path)
    assert default["--authority"] == tmp_path / HISTORICAL_INPUTS["WOW_CPI223_AUTHORITY"][1]
    for name in HISTORICAL_INPUTS:
        monkeypatch.setenv(name, str(tmp_path / name))
    overridden = historical_input_paths(tmp_path)
    for name, (flag, _) in HISTORICAL_INPUTS.items():
        assert overridden[flag] == tmp_path / name

def test_source_contract_rejects_authority_mutation() -> None:
    candidate = deepcopy(expected_source_contract())
    candidate["authority_commit"] = "0" * 40

    with pytest.raises(ContractError, match="authority_commit"):
        validate_source_contract(candidate)


def test_manifest_rejects_enabled_embedded_python() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "manifests" / "empty.json").read_text(encoding="utf-8"))
    cast(dict[str, object], cast(dict[str, object], manifest)["embedded_python"])["enabled_by_default"] = True

    with pytest.raises(ContractError, match="embedded Python defaults"):
        validate_manifest_data(manifest)


def test_repository_validator_passes_against_cpi225_evidence() -> None:
    repository_root = Path(os.environ.get("WOW_PROJECT_ROOT", Path(__file__).resolve().parents[3]))
    validator = Path(__file__).resolve().parents[1] / "devtools/validate_cpi223_contract.py"
    arguments = [sys.executable, str(validator), "--repo-root", str(repository_root)]
    for name, (flag, _) in HISTORICAL_INPUTS.items():
        if name in os.environ:
            arguments.extend((flag, os.environ[name]))
    result = subprocess.run(
        arguments,
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.startswith("PASS: CPI-223 offline/static contract")


def test_validator_project_root_override_and_cli_precedence(tmp_path: Path) -> None:
    validator = Path(__file__).resolve().parents[1] / "devtools/validate_cpi223_contract.py"
    env = dict(os.environ, WOW_PROJECT_ROOT=str(tmp_path / "external"))
    code = (
        "import runpy,sys; m=runpy.run_path(sys.argv[1]); "
        "print(m['REPOSITORY_ROOT']); sys.argv=['validator','--repo-root',sys.argv[2]]; "
        "print(m['parse_args']().repo_root)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(validator), str(tmp_path / "explicit")],
        env=env, check=True, capture_output=True, text=True,
    )
    assert result.stdout.splitlines() == [str(tmp_path / "external"), str(tmp_path / "explicit")]


@pytest.mark.parametrize(
    "case", ["valid", "missing", "authority", "surface", "patch", "symlink", "authority-symlink", "bundle-symlink"]
)
def test_external_historical_inputs_are_exact_and_fail_closed(tmp_path: Path, case: str) -> None:
    import shutil

    project = Path(os.environ.get("WOW_PROJECT_ROOT", Path(__file__).resolve().parents[3]))
    validator = Path(__file__).resolve().parents[1] / "devtools/validate_cpi223_contract.py"
    authority = tmp_path / "authority.json"
    bundle = tmp_path / "surface"
    patch = tmp_path / "engine.patch"
    inputs = historical_input_paths(project)
    shutil.copyfile(inputs["--authority"], authority)
    shutil.copytree(inputs["--surface-bundle"], bundle)
    shutil.copyfile(inputs["--engine-patch"], patch)
    if case == "missing":
        patch.unlink()
    elif case == "authority":
        authority.write_bytes(authority.read_bytes() + b" ")
    elif case == "surface":
        (bundle / "release-surface.json").write_bytes(b"{}")
    elif case == "patch":
        patch.write_bytes(patch.read_bytes() + b" ")
    elif case == "symlink":
        real = tmp_path / "real.patch"
        patch.rename(real)
        patch.symlink_to(real)
    elif case == "authority-symlink":
        real = tmp_path / "real-authority.json"
        authority.rename(real)
        authority.symlink_to(real)
    elif case == "bundle-symlink":
        real = tmp_path / "real-surface"
        bundle.rename(real)
        bundle.symlink_to(real, target_is_directory=True)
    result = subprocess.run(
        [sys.executable, str(validator), "--repo-root", str(tmp_path), "--authority", str(authority),
         "--surface-bundle", str(bundle), "--engine-patch", str(patch)],
        capture_output=True, text=True,
    )
    assert (result.returncode == 0) == (case == "valid"), result.stderr
    if case == "valid":
        assert result.stdout.startswith("PASS: CPI-223 offline/static contract")


def test_historical_archive_fallback_preserves_explicit_and_original_priority(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    original = project / "surface"
    archived = (tmp_path / "wow-project-archive/cpi420-codegraph-20261028/"
                "cpi223-historical-contract-inputs-20261029/surface")
    assert historical_input(project, original, "surface") == original
    archived.parent.mkdir(parents=True)
    archived.write_bytes(b"archived")
    assert historical_input(project, original, "surface") == archived
    explicit = project / "missing-explicit"
    assert historical_input(project, original, "surface", explicit) == explicit
    original.write_bytes(b"original")
    assert historical_input(project, original, "surface") == original
    original.unlink()
    original.symlink_to(explicit)
    assert historical_input(project, original, "surface") == original
