"""Exact, fail-closed contract for CPI-223 standalone releases.

The standalone package is intentionally tied to the CPI-225 offline release
surface that selected its source tuple.  This module keeps that provenance in
one place so a release cannot silently accept a moving engine, a different
module manifest, or an enabled embedded runtime by omission.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

MANIFEST_SCHEMA = 2
SOURCE_CONTRACT_SCHEMA = 1

ENGINE_COMMIT = "700a4f75a2e550f17022665d7b5b009c24e46a40"
CORE_COMMIT = "9fb906bb7296212ff42fc95ff73a92aaf8554f0d"
PLAYERBOTS_COMMIT = "2f7d9f774987d0157c6a0d0cc08c40bec3db3945"

CPI225_AUTHORITY_COMMIT = "729504842db296c5230884d2ed64ec61c2549c3d"
CPI225_AUTHORITY_ISSUE = "CPI-225"
CPI225_AUTHORITY_PATH = "research/cpi-225/cpi225-reconciled-disposition-20260903.json"
CPI225_RELEASE_SURFACE_PATH = (
    "research/evidence/cpi-225/cpi225-release-surface-20260903/release-surface.json"
)
CPI225_RELEASE_SURFACE_SHA256 = "ac3ca5352da58dcdb53666466dc1bd2c5415c2b3e5fc3972cfbdcc499fd8f8d2"
CANONICAL_MODULE_MANIFEST_SHA256 = "1845582e8b3fa432840979cc467bbe7b884797ac25b60c6f5f118aef2f99995e"
ENGINE_SOURCE_DIFF_SHA256 = "36994d26a8998f38bbaa39a806859675dfc31afe1618298912f1028dac6ed779"
ENGINE_CONFIG_SHA256 = "a485df867fc7a8953cc7e37f034d0caef582922ffd6072fa4f9be7639fe8dcc3"

CPI223_ARCHIVE = Path("cpi420-codegraph-20261028/cpi223-historical-contract-inputs-20261029")


def historical_input(project: Path, default: Path, archive_name: str, explicit: Path | None = None) -> Path:
    """Use the retained archive only if the original path is absent."""
    if explicit is not None:
        return explicit
    if default.exists() or default.is_symlink():
        return default
    archived = project.parent / "wow-project-archive" / CPI223_ARCHIVE / archive_name
    return archived if archived.exists() or archived.is_symlink() else default


SAFE_RELEASE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")

_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "release",
        "source_contract",
        "embedded_python",
        "engine_commit",
        "core_commit",
        "playerbots_commit",
        "features",
    }
)
_SOURCE_CONTRACT_FIELDS = frozenset(
    {
        "schema",
        "authority_issue",
        "authority_commit",
        "authority_path",
        "release_surface_path",
        "release_surface_sha256",
        "engine_source_diff_sha256",
        "engine_config_sha256",
        "exact_tuple",
    }
)
_SOURCE_TUPLE_FIELDS = frozenset({"core", "playerbots", "python_engine", "canonical_manifest_sha256"})
_EMBEDDED_PYTHON_FIELDS = frozenset({"enabled_by_default", "allow_exec_by_default"})
_INVENTORY_FIELDS = frozenset({"schema", "release", "files"})
_INVENTORY_ENTRY_FIELDS = frozenset({"path", "sha256", "size"})


class ContractError(ValueError):
    """Raised when a CPI-223 artifact is incomplete or inconsistent."""


def expected_source_contract() -> dict[str, object]:
    """Return the immutable CPI-225 provenance required by every release."""

    return {
        "schema": SOURCE_CONTRACT_SCHEMA,
        "authority_issue": CPI225_AUTHORITY_ISSUE,
        "authority_commit": CPI225_AUTHORITY_COMMIT,
        "authority_path": CPI225_AUTHORITY_PATH,
        "release_surface_path": CPI225_RELEASE_SURFACE_PATH,
        "release_surface_sha256": CPI225_RELEASE_SURFACE_SHA256,
        "engine_source_diff_sha256": ENGINE_SOURCE_DIFF_SHA256,
        "engine_config_sha256": ENGINE_CONFIG_SHA256,
        "exact_tuple": {
            "core": CORE_COMMIT,
            "playerbots": PLAYERBOTS_COMMIT,
            "python_engine": ENGINE_COMMIT,
            "canonical_manifest_sha256": CANONICAL_MODULE_MANIFEST_SHA256,
        },
    }


def expected_embedded_python_policy() -> dict[str, bool]:
    """Return the only embedded-runtime defaults allowed in this slice."""

    return {"enabled_by_default": False, "allow_exec_by_default": False}


def canonical_json(value: object) -> bytes:
    """Encode JSON with stable ordering and a trailing newline."""

    return (json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _as_object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{context} must be a JSON object")
    result = dict(value)
    if not all(isinstance(key, str) for key in result):
        raise ContractError(f"{context} field names must be strings")
    return result


def _require_exact_keys(value: Mapping[str, object], expected: frozenset[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ContractError(f"{context} keys differ; missing={missing}, extra={extra}")


def _require_string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{context} must be a non-empty string")
    return value


def _require_sha256(value: object, context: str) -> str:
    result = _require_string(value, context)
    if SHA256.fullmatch(result) is None:
        raise ContractError(f"{context} must be a lowercase SHA-256 digest")
    return result


def _require_commit(value: object, context: str) -> str:
    result = _require_string(value, context)
    if COMMIT.fullmatch(result) is None:
        raise ContractError(f"{context} must be a lowercase Git commit")
    return result


def validate_source_contract(value: object) -> dict[str, object]:
    """Validate the exact CPI-225 source/provenance contract."""

    source = _as_object(value, "manifest source_contract")
    _require_exact_keys(source, _SOURCE_CONTRACT_FIELDS, "manifest source_contract")
    if source.get("schema") != SOURCE_CONTRACT_SCHEMA:
        raise ContractError(f"manifest source_contract.schema must be {SOURCE_CONTRACT_SCHEMA}")
    if source.get("authority_issue") != CPI225_AUTHORITY_ISSUE:
        raise ContractError("manifest source_contract.authority_issue must be CPI-225")
    if source.get("authority_commit") != CPI225_AUTHORITY_COMMIT:
        raise ContractError("manifest source_contract.authority_commit is not the CPI-225 authority commit")
    if source.get("authority_path") != CPI225_AUTHORITY_PATH:
        raise ContractError("manifest source_contract.authority_path is not the CPI-225 authority artifact")
    if source.get("release_surface_path") != CPI225_RELEASE_SURFACE_PATH:
        raise ContractError("manifest source_contract.release_surface_path is not the CPI-225 release surface")
    _require_sha256(source.get("release_surface_sha256"), "manifest source_contract.release_surface_sha256")
    _require_sha256(source.get("engine_source_diff_sha256"), "manifest source_contract.engine_source_diff_sha256")
    _require_sha256(source.get("engine_config_sha256"), "manifest source_contract.engine_config_sha256")

    tuple_value = _as_object(source.get("exact_tuple"), "manifest source_contract.exact_tuple")
    _require_exact_keys(tuple_value, _SOURCE_TUPLE_FIELDS, "manifest source_contract.exact_tuple")
    for key in ("core", "playerbots", "python_engine"):
        _require_commit(tuple_value.get(key), f"manifest source_contract.exact_tuple.{key}")
    _require_sha256(
        tuple_value.get("canonical_manifest_sha256"),
        "manifest source_contract.exact_tuple.canonical_manifest_sha256",
    )

    expected = expected_source_contract()
    if source != expected:
        raise ContractError("manifest source_contract does not match the verified CPI-225 release surface")
    return source


def validate_manifest_data(value: object) -> tuple[str, tuple[str, ...]]:
    """Validate and project a release manifest without loading or registering code."""

    manifest = _as_object(value, "release manifest")
    _require_exact_keys(manifest, _MANIFEST_FIELDS, "release manifest")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ContractError(f"release manifest.schema must be {MANIFEST_SCHEMA}")
    release = _require_string(manifest.get("release"), "release manifest.release")
    if SAFE_RELEASE_NAME.fullmatch(release) is None:
        raise ContractError("release manifest.release contains unsafe characters")
    validate_source_contract(manifest.get("source_contract"))

    embedded = _as_object(manifest.get("embedded_python"), "release manifest.embedded_python")
    _require_exact_keys(embedded, _EMBEDDED_PYTHON_FIELDS, "release manifest.embedded_python")
    if embedded != expected_embedded_python_policy():
        raise ContractError("release manifest embedded Python defaults must remain disabled")

    if manifest.get("engine_commit") != ENGINE_COMMIT:
        raise ContractError(f"release manifest.engine_commit must equal {ENGINE_COMMIT!r}")
    if manifest.get("core_commit") != CORE_COMMIT:
        raise ContractError(f"release manifest.core_commit must equal {CORE_COMMIT!r}")
    if manifest.get("playerbots_commit") != PLAYERBOTS_COMMIT:
        raise ContractError(f"release manifest.playerbots_commit must equal {PLAYERBOTS_COMMIT!r}")

    raw_features = manifest.get("features")
    if not isinstance(raw_features, list) or not all(isinstance(item, str) for item in raw_features):
        raise ContractError("release manifest.features must be a list of module names")
    features = tuple(cast(list[str], raw_features))
    if len(features) != len(set(features)):
        raise ContractError("release manifest feature names must be unique")
    if features != tuple(sorted(features)):
        raise ContractError("release manifest features must be sorted for deterministic loading")
    for name in features:
        if not name.startswith("realm_scripts.features.") or name.count(".") != 2:
            raise ContractError(f"feature module outside governed namespace: {name!r}")
    return release, features


def read_manifest_file(path: Path) -> tuple[dict[str, object], str, tuple[str, ...]]:
    """Read and validate a manifest before any release files are copied."""

    if path.is_symlink() or not path.is_file():
        raise ContractError(f"release manifest must be a regular file: {path}")
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read release manifest {path}: {exc}") from exc
    manifest = _as_object(raw, "release manifest")
    release, features = validate_manifest_data(manifest)
    return manifest, release, features


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_release_bundle(bundle: Path) -> tuple[str, dict[str, object]]:
    """Verify a release directory and its manifest before it is consumed."""

    try:
        bundle = bundle.resolve(strict=True)
    except OSError as exc:
        raise ContractError(f"release bundle cannot be resolved: {bundle}: {exc}") from exc
    if not bundle.is_dir() or bundle.is_symlink():
        raise ContractError(f"release bundle is not a regular directory: {bundle}")

    inventory_path = bundle / "release-files.json"
    if inventory_path.is_symlink() or not inventory_path.is_file():
        raise ContractError("release bundle is missing release-files.json")
    try:
        raw_inventory: Any = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid release inventory: {exc}") from exc
    inventory = _as_object(raw_inventory, "release inventory")
    _require_exact_keys(inventory, _INVENTORY_FIELDS, "release inventory")
    if inventory.get("schema") != 1:
        raise ContractError("release inventory.schema must be 1")
    release = _require_string(inventory.get("release"), "release inventory.release")
    if SAFE_RELEASE_NAME.fullmatch(release) is None:
        raise ContractError("release inventory contains an unsafe release name")

    entries = inventory.get("files")
    if not isinstance(entries, list):
        raise ContractError("release inventory.files must be a list")
    expected: set[str] = set()
    for ordinal, raw_entry in enumerate(entries):
        entry = _as_object(raw_entry, f"release inventory.files[{ordinal}]")
        _require_exact_keys(entry, _INVENTORY_ENTRY_FIELDS, f"release inventory.files[{ordinal}]")
        relative = _require_string(entry.get("path"), f"release inventory.files[{ordinal}].path")
        if relative == "release-files.json" or "\\" in relative:
            raise ContractError(f"unsafe inventory path: {relative!r}")
        path_value = Path(relative)
        if path_value.is_absolute() or ".." in path_value.parts or "." in path_value.parts:
            raise ContractError(f"unsafe inventory path: {relative!r}")
        if relative in expected:
            raise ContractError(f"duplicate inventory path: {relative}")
        expected.add(relative)
        wanted_hash = _require_sha256(entry.get("sha256"), f"release inventory.files[{ordinal}].sha256")
        wanted_size = entry.get("size")
        if not isinstance(wanted_size, int) or isinstance(wanted_size, bool) or wanted_size < 0:
            raise ContractError(f"release inventory.files[{ordinal}].size must be a non-negative integer")
        path = bundle / path_value
        if not path.is_file() or path.is_symlink():
            raise ContractError(f"missing regular release file: {relative}")
        if path.stat().st_size != wanted_size:
            raise ContractError(f"size mismatch: {relative}")
        if _sha256(path) != wanted_hash:
            raise ContractError(f"SHA-256 mismatch: {relative}")

    for path in bundle.rglob("*"):
        if path.is_symlink():
            raise ContractError("release bundle must not contain symlinks")
    actual = {path.relative_to(bundle).as_posix() for path in bundle.rglob("*") if path.is_file()}
    if actual != expected | {"release-files.json"}:
        unexpected = sorted(actual - expected - {"release-files.json"})
        missing = sorted(expected - actual)
        raise ContractError(f"release file set mismatch; unexpected={unexpected}, missing={missing}")

    manifest, manifest_release, _ = read_manifest_file(bundle / "manifest.json")
    if manifest_release != release:
        raise ContractError("manifest and inventory release names differ")
    return release, manifest


__all__ = [
    "CANONICAL_MODULE_MANIFEST_SHA256",
    "CORE_COMMIT",
    "CPI225_AUTHORITY_COMMIT",
    "CPI225_AUTHORITY_ISSUE",
    "CPI225_AUTHORITY_PATH",
    "CPI225_RELEASE_SURFACE_PATH",
    "CPI225_RELEASE_SURFACE_SHA256",
    "ENGINE_COMMIT",
    "ENGINE_CONFIG_SHA256",
    "ENGINE_SOURCE_DIFF_SHA256",
    "MANIFEST_SCHEMA",
    "PLAYERBOTS_COMMIT",
    "ContractError",
    "canonical_json",
    "expected_embedded_python_policy",
    "expected_source_contract",
    "read_manifest_file",
    "validate_manifest_data",
    "validate_source_contract",
    "verify_release_bundle",
]
