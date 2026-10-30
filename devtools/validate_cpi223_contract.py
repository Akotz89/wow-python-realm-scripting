#!/usr/bin/env python3
"""Validate the CPI-223 offline/static contract against CPI-225 evidence.

This validator is deliberately read-only.  It checks the exact source tuple,
the deferred/disabled embedded-engine posture, the generated CPI-225 release
surface, and the two standalone release manifests.  It never configures or
builds AzerothCore, applies SQL, starts a service, or touches a live realm.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = Path(os.environ.get("WOW_PROJECT_ROOT", PACKAGE_ROOT.parents[1]))
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from realm_scripts.release_contract import (  # noqa: E402
    CANONICAL_MODULE_MANIFEST_SHA256,
    CORE_COMMIT,
    CPI225_AUTHORITY_COMMIT,
    CPI225_AUTHORITY_ISSUE,
    CPI225_AUTHORITY_PATH,
    CPI225_RELEASE_SURFACE_PATH,
    CPI225_RELEASE_SURFACE_SHA256,
    ENGINE_COMMIT,
    ENGINE_CONFIG_SHA256,
    ENGINE_SOURCE_DIFF_SHA256,
    PLAYERBOTS_COMMIT,
    canonical_json,
    historical_input,
    read_manifest_file,
)


class ValidationError(RuntimeError):
    """Raised when an exact CPI-223 input is missing or has drifted."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValidationError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValidationError(f"JSON root must be an object: {path}")
    return cast(dict[str, object], raw)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def _verify_checksums(bundle: Path) -> int:
    sums_path = bundle / "SHA256SUMS"
    try:
        lines = sums_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError(f"cannot read {sums_path}: {exc}") from exc

    expected: dict[str, str] = {}
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        fields = line.split(maxsplit=1)
        if len(fields) != 2 or len(fields[0]) != 64 or any(char not in "0123456789abcdef" for char in fields[0]):
            raise ValidationError(f"invalid checksum line {sums_path}:{line_number}")
        relative = fields[1].strip()
        if not relative or relative in expected or "\\" in relative or Path(relative).is_absolute():
            raise ValidationError(f"invalid or duplicate checksum path {relative!r}")
        if ".." in Path(relative).parts or "." in Path(relative).parts:
            raise ValidationError(f"unsafe checksum path {relative!r}")
        expected[relative] = fields[0]

    actual_paths = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file() and not path.is_symlink() and path.name != "SHA256SUMS"
    }
    _require(set(expected) == actual_paths, "CPI-225 SHA256SUMS does not cover exactly the evidence files")
    for relative, wanted in expected.items():
        path = bundle / relative
        _require(not path.is_symlink(), f"evidence file is a symlink: {relative}")
        _require(_sha256(path) == wanted, f"CPI-225 evidence checksum mismatch: {relative}")
    return len(expected)


def _row(path: Path, key: str) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError(f"cannot read tabular evidence {path}: {exc}") from exc
    for line in lines[1:]:
        fields = line.split("\t")
        if fields and fields[0] == key:
            return fields
    raise ValidationError(f"missing {key!r} row in {path}")


def validate(
    repo_root: Path = REPOSITORY_ROOT,
    *,
    authority_path: Path | None = None,
    surface_bundle: Path | None = None,
    patch_path: Path | None = None,
) -> int:
    """Run all read-only checks and return the number of successful checks."""

    authority_path = authority_path or repo_root / CPI225_AUTHORITY_PATH
    surface_bundle = historical_input(
        repo_root, repo_root / "research/evidence/cpi-225/cpi225-release-surface-20260903",
        "cpi225-release-surface-20260903", surface_bundle,
    )
    patch_path = historical_input(
        repo_root, repo_root / "research/cpi-223/patches/mod-python-engine-hardening.patch",
        "mod-python-engine-hardening.patch", patch_path,
    )
    for path in (authority_path, surface_bundle, patch_path):
        _require(not path.is_symlink(), f"input is a symlink: {path}")
    _require(
        _sha256(authority_path) == "4d4eedef40b6c8e239dd13290ce209f1bcb8b97af29e22dfb18e2b6a6b9675ea",
        f"authority artifact differs from {CPI225_AUTHORITY_COMMIT}:{CPI225_AUTHORITY_PATH}",
    )
    authority = _read_json(authority_path)
    _require(authority.get("schema") == "cpi225-reconciled-disposition/v1", "unexpected CPI-225 authority schema")

    surface_path = surface_bundle / Path(CPI225_RELEASE_SURFACE_PATH).name
    _require(_sha256(surface_path) == CPI225_RELEASE_SURFACE_SHA256, "CPI-225 release-surface JSON hash drifted")
    checksum_count = _verify_checksums(surface_bundle)
    surface = _read_json(surface_path)
    _require(
        surface.get("schema") == "cpi225-release-surface-manifest/v1",
        "unexpected CPI-225 release-surface schema",
    )
    _require(
        surface.get("status") == "UNACCEPTED_OFFLINE_EVIDENCE",
        "release surface is not offline-only evidence",
    )

    expected_tuple = {
        "core": CORE_COMMIT,
        "playerbots": PLAYERBOTS_COMMIT,
        "python_engine": ENGINE_COMMIT,
        "canonical_manifest_sha256": CANONICAL_MODULE_MANIFEST_SHA256,
    }
    _require(surface.get("current_tuple") == expected_tuple, "CPI-225 exact tuple drifted")
    _require(
        surface.get("governing")
        == {
            "authority_commit": CPI225_AUTHORITY_COMMIT,
            "authority_path": CPI225_AUTHORITY_PATH,
            "hindsight_page": "kp-01b2837123b841c3ad37c4d1db02c54a",
            "linear_issue": CPI225_AUTHORITY_ISSUE,
        },
        "CPI-225 governing provenance drifted",
    )

    invariants = surface.get("runtime_policy_invariants")
    if not isinstance(invariants, dict):
        raise ValidationError("CPI-225 runtime_policy_invariants is missing")
    invariant_map = cast(dict[str, object], invariants)
    _require(invariant_map.get("python_enabled") == 0, "embedded Python is not disabled in the release surface")
    _require(invariant_map.get("sql_apply") is False, "release surface permits SQL application")
    _require(invariant_map.get("live_deployment") is False, "release surface permits live deployment")
    _require(invariant_map.get("population_admission") == 0, "release surface permits population admission")
    _require(
        invariant_map.get("native_population_director")
        == "not bound; no native C++ PopulationDirector implementation is authorized in this lane",
        "release surface does not preserve the no-PopulationDirector boundary",
    )

    policy_entries = surface.get("module_policy")
    if not isinstance(policy_entries, list):
        raise ValidationError("CPI-225 module_policy is missing")
    python_policy = next(
        (
            cast(dict[str, object], item)
            for item in policy_entries
            if isinstance(item, dict) and item.get("module") == "mod-python-engine"
        ),
        None,
    )
    if python_policy is None:
        raise ValidationError("CPI-225 mod-python-engine policy row is missing")
    _require(python_policy.get("revision") == ENGINE_COMMIT, "Python engine source revision drifted")
    _require(python_policy.get("disposition") == "deferred", "Python engine is not deferred")
    _require(
        python_policy.get("loader_action") == "deferred-not-loaded"
        and python_policy.get("config_action") == "deferred-config"
        and python_policy.get("sql_action") == "forbidden-unapplied",
        "Python loader/config/SQL posture drifted",
    )
    _require(
        python_policy.get("current_dirty_diff_sha256") == ENGINE_SOURCE_DIFF_SHA256,
        "Python engine source overlay hash drifted",
    )

    module_head = _row(surface_bundle / "source/module-heads.tsv", "mod-python-engine")
    _require(module_head[1] == ENGINE_COMMIT, "module-heads.tsv Python revision drifted")
    loader_row = _row(surface_bundle / "loader/loader-manifest.tsv", "mod-python-engine")
    _require(
        loader_row[1:7]
        == [
            ENGINE_COMMIT,
            "deferred",
            "excluded-deferred",
            "deferred-not-loaded",
            "deferred-config",
            "forbidden-unapplied",
        ],
        "loader manifest does not keep Python deferred and unloaded",
    )
    _require(loader_row[8] == ENGINE_SOURCE_DIFF_SHA256, "loader manifest source hash drifted")
    config_row = _row(surface_bundle / "config/config-manifest.tsv", "mod-python-engine")
    _require(
        config_row[3] == ENGINE_CONFIG_SHA256 and config_row[5] == ENGINE_CONFIG_SHA256,
        "Python config hash drifted",
    )
    _require(
        config_row[7] == "Python.AllowExecCommand=0;Python.Enabled=0",
        "Python config does not fail closed",
    )

    _require(_sha256(patch_path) == ENGINE_SOURCE_DIFF_SHA256, "CPI-223 engine hardening patch hash drifted")

    manifest_checks = 0
    for filename, expected_features in (
        ("empty.json", ()),
        ("controlled-smoke.json", ("realm_scripts.features.engine_smoke",)),
    ):
        manifest_path = PACKAGE_ROOT / "manifests" / filename
        manifest, release, features = read_manifest_file(manifest_path)
        _require(manifest_path.read_bytes() == canonical_json(manifest), f"manifest is not canonical: {filename}")
        _require(features == expected_features, f"unexpected feature set in {filename}")
        _require(release.startswith("cpi223-"), f"unexpected release name in {filename}")
        manifest_checks += 3

    return 15 + checksum_count + manifest_checks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--authority", type=Path)
    parser.add_argument("--surface-bundle", type=Path)
    parser.add_argument("--engine-patch", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        count = validate(args.repo_root.resolve(strict=True), authority_path=args.authority,
                         surface_bundle=args.surface_bundle, patch_path=args.engine_patch)
    except (OSError, ValidationError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"PASS: CPI-223 offline/static contract ({count} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
