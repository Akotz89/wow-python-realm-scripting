from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from realm_scripts.contracts import Binding
from realm_scripts.loader import ManifestError, load_manifest
from realm_scripts.policy import CORE_COMMIT, ENGINE_COMMIT, PLAYERBOTS_COMMIT
from realm_scripts.release_contract import canonical_json, expected_embedded_python_policy, expected_source_contract


class FakeAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, int | None]] = []

    def Register(self, event_name: str, callback: object, entry_id: int | None = None) -> None:
        self.calls.append((event_name, callback, entry_id))


def _manifest(tmp_path: Path, features: list[str], **overrides: Any) -> Path:
    data: dict[str, Any] = {
        "schema": 2,
        "release": "test-release",
        "source_contract": expected_source_contract(),
        "embedded_python": expected_embedded_python_policy(),
        "engine_commit": ENGINE_COMMIT,
        "core_commit": CORE_COMMIT,
        "playerbots_commit": PLAYERBOTS_COMMIT,
        "features": features,
    }
    data.update(overrides)
    path = tmp_path / "manifest.json"
    path.write_bytes(canonical_json(data))
    manifest_bytes = path.read_bytes()
    inventory = {
        "schema": 1,
        "release": data["release"],
        "files": [
            {
                "path": "manifest.json",
                "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "size": len(manifest_bytes),
            }
        ],
    }
    (tmp_path / "release-files.json").write_bytes(canonical_json(inventory))
    return path


def test_empty_release_loads_without_registrations(tmp_path: Path) -> None:
    api = FakeAPI()
    report = load_manifest(api, _manifest(tmp_path, []))
    assert report.release == "test-release"
    assert report.binding_count == 0
    assert api.calls == []


def test_controlled_smoke_registers_exact_login_hook(tmp_path: Path) -> None:
    api = FakeAPI()
    report = load_manifest(api, _manifest(tmp_path, ["realm_scripts.features.engine_smoke"]))
    assert report.binding_count == 1
    assert [(event, entry) for event, _, entry in api.calls] == [("PLAYER_ON_LOGIN", None)]


def test_wrong_engine_pin_fails_before_registration(tmp_path: Path) -> None:
    api = FakeAPI()
    with pytest.raises(ManifestError, match="engine_commit"):
        load_manifest(api, _manifest(tmp_path, [], engine_commit="moving-main"))
    assert api.calls == []


def test_feature_order_must_be_deterministic(tmp_path: Path) -> None:
    api = FakeAPI()
    with pytest.raises(ManifestError, match="sorted"):
        load_manifest(
            api,
            _manifest(tmp_path, ["realm_scripts.features.z_feature", "realm_scripts.features.a_feature"]),
        )
    assert api.calls == []


def test_duplicate_or_unapproved_binding_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class BadFeature:
        __name__ = "realm_scripts.features.bad"

        @staticmethod
        def bindings() -> tuple[Binding, ...]:
            return (Binding("WORLD_ON_UPDATE", lambda: None),)

    monkeypatch.setattr("realm_scripts.loader._import_feature", lambda _name: BadFeature)
    api = FakeAPI()
    with pytest.raises(ManifestError, match="not approved"):
        load_manifest(api, _manifest(tmp_path, ["realm_scripts.features.bad"]))
    assert api.calls == []


def test_missing_release_inventory_fails_before_import_or_registration(tmp_path: Path) -> None:
    api = FakeAPI()
    manifest = _manifest(tmp_path, [])
    (tmp_path / "release-files.json").unlink()

    with pytest.raises(ManifestError, match="missing release-files.json"):
        load_manifest(api, manifest)
    assert api.calls == []
