"""Deterministic, fail-closed feature loader used by the one engine bootstrap."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import cast

from realm_scripts.contracts import AzerothCoreAPI, Binding, LoadReport
from realm_scripts.policy import (
    SAFE_ENTRY_HOOKS,
    SAFE_GLOBAL_HOOKS,
    SAFE_HOOKS,
)
from realm_scripts.release_contract import ContractError, verify_release_bundle


class ManifestError(RuntimeError):
    """The release manifest or a feature registration violated policy."""


def _read_manifest(path: Path) -> tuple[str, tuple[str, ...]]:
    """Verify the complete immutable bundle before importing any feature."""

    if path.name != "manifest.json":
        raise ManifestError("release manifest must be named manifest.json")
    try:
        release, manifest = verify_release_bundle(path.parent)
    except ContractError as exc:
        raise ManifestError(str(exc)) from exc

    # ``verify_release_bundle`` reads the canonical manifest at the bundle
    # root.  Refuse a caller-supplied alternate path rather than allowing a
    # verified inventory to be paired with different manifest bytes.
    try:
        if path.resolve(strict=True) != (path.parent / "manifest.json").resolve(strict=True):
            raise ManifestError("release manifest path is not the verified bundle manifest")
    except OSError as exc:
        raise ManifestError(f"cannot resolve release manifest {path}: {exc}") from exc

    feature_value = manifest.get("features")
    if not isinstance(feature_value, list) or not all(isinstance(item, str) for item in feature_value):
        raise ManifestError("verified release manifest features are invalid")
    features = tuple(cast(list[str], feature_value))
    return release, features


def _import_feature(name: str) -> ModuleType:
    importlib.invalidate_caches()
    if name in sys.modules:
        return importlib.reload(sys.modules[name])
    return importlib.import_module(name)


def _bindings(module: ModuleType) -> tuple[Binding, ...]:
    factory = getattr(module, "bindings", None)
    if not callable(factory):
        raise ManifestError(f"feature {module.__name__!r} must define callable bindings()")
    value = factory()
    if not isinstance(value, Iterable):
        raise ManifestError(f"feature {module.__name__!r} bindings() must return an iterable")
    result = tuple(value)
    if not all(isinstance(item, Binding) for item in result):
        raise ManifestError(f"feature {module.__name__!r} returned a non-Binding value")
    return cast(tuple[Binding, ...], result)


def _validate(bindings: tuple[Binding, ...]) -> None:
    seen: set[tuple[str, int | None]] = set()
    for binding in bindings:
        if binding.event not in SAFE_HOOKS:
            raise ManifestError(f"hook is not approved at the pinned engine revision: {binding.event}")
        if not callable(binding.callback):
            raise ManifestError(f"callback for {binding.event} is not callable")
        if binding.event in SAFE_GLOBAL_HOOKS and binding.entry_id is not None:
            raise ManifestError(f"global hook {binding.event} cannot have entry_id")
        if binding.event in SAFE_ENTRY_HOOKS and binding.entry_id is not None and binding.entry_id <= 0:
            raise ManifestError(f"entry_id for {binding.event} must be positive or omitted")
        key = (binding.event, binding.entry_id)
        if key in seen:
            raise ManifestError(f"duplicate binding for {binding.event} entry {binding.entry_id}")
        seen.add(key)


def load_manifest(api: AzerothCoreAPI, manifest_path: Path) -> LoadReport:
    """Load, validate, then register a complete release manifest.

    Imports and validation occur before the first engine registration.  This
    prevents syntax errors, missing modules, invalid pins, and policy violations
    from creating a partial hook set.  The upstream engine must still be fixed
    to clear registrations if an engine call itself fails mid-registration.
    """

    release, features = _read_manifest(manifest_path)
    all_bindings: list[Binding] = []
    for feature_name in features:
        try:
            all_bindings.extend(_bindings(_import_feature(feature_name)))
        except Exception as exc:
            raise ManifestError(f"feature {feature_name!r} failed before registration: {exc}") from exc
    registrations = tuple(all_bindings)
    _validate(registrations)

    for binding in registrations:
        if binding.entry_id is None:
            api.Register(binding.event, binding.callback)
        else:
            api.Register(binding.event, binding.callback, binding.entry_id)

    return LoadReport(release=release, features=features, binding_count=len(registrations))
