from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import cast

import pytest

from realm_scripts.population_adapter import (
    CPI196_MANIFEST_SHA256,
    CPI196_SCHEDULE_SHA256,
    IdentityIndependentPopulationPlan,
    PopulationManifestAdapter,
)
from realm_scripts.population_policy import PopulationPolicyError, compile_population_manifest, digest
from realm_scripts.population_policy_v2 import compile_wave_schedule_v2
from realm_scripts.release_contract import CPI225_RELEASE_SURFACE_SHA256, expected_source_contract

REPOSITORY_ROOT = Path(os.environ.get("WOW_PROJECT_ROOT", Path(__file__).resolve().parents[3]))
CPI196_CANDIDATE_PATH = (
    REPOSITORY_ROOT
    / "integration/cpi-196/contracts/identity-independent-manifest-candidate-v1/candidate-manifest.json"
)


def _identity_independent_fixture() -> tuple[
    dict[str, object], dict[str, object], dict[str, object]
]:
    candidate = cast(dict[str, object], json.loads(CPI196_CANDIDATE_PATH.read_text(encoding="utf-8")))
    schedule = cast(dict[str, object], candidate["schedule"])
    return candidate, schedule, expected_source_contract()


def test_projects_cpi196_candidate_as_deterministic_pass_no_bind_plan() -> None:
    candidate, schedule, release_surface = _identity_independent_fixture()

    plan = PopulationManifestAdapter.consume_identity_independent_candidate(
        candidate,
        schedule,
        release_surface,
        manifest_sha256=CPI196_MANIFEST_SHA256,
    )

    assert isinstance(plan, IdentityIndependentPopulationPlan)
    assert plan.status == "PASS_NO_BIND"
    assert plan.realm_id == 1
    assert plan.permanent_roster_target == 10_000
    assert plan.schedule_sha256 == CPI196_SCHEDULE_SHA256
    assert len(plan.wave_counts) == 80
    assert plan.wave_counts[0] == 329
    assert plan.wave_counts[-1] == 30
    assert sum(plan.wave_counts) == 10_000
    assert plan.global_phase_cap == 19
    assert plan.band_names == ("pioneer", "scout", "forward", "main", "trailing")
    assert plan.band_offsets == (0, 1, 2, 4, 7)
    assert plan.region_order == (
        "blood_elf",
        "dwarf_gnome",
        "draenei",
        "human",
        "night_elf",
        "orc_troll",
        "tauren",
        "undead",
    )
    assert plan.login_rate_sessions == 5
    assert plan.login_rate_window_seconds == 20
    assert plan.max_active_per_spawn_cell == 4
    assert plan.creation_batch_size is None
    assert plan.simultaneous_online_cap is None
    assert plan.release_surface.release_surface_sha256 == CPI225_RELEASE_SURFACE_SHA256

    first_wave = plan.wave(1)
    final_wave = plan.wave(80)
    assert first_wave.planned_count == 329
    assert final_wave.planned_count == 30
    assert first_wave.admission_authorized is False
    assert final_wave.admission_authorized is False
    assert first_wave.wave_idempotency_key == plan.wave(1).wave_idempotency_key
    assert first_wave.wave_idempotency_key != plan.wave(2).wave_idempotency_key
    assert all(left >= right for left, right in zip(plan.wave_counts, plan.wave_counts[1:], strict=False))

    # The projection has policy categories and source provenance only.  It
    # cannot carry an account, character, GUID, fixed-party, or runtime field.
    serialized = json.dumps(asdict(plan), sort_keys=True)
    for forbidden in (
        "account_name",
        "character_name",
        "account_id",
        "character_guid",
        "owner_guid",
        "Yuna",
        "yuna",
    ):
        assert forbidden not in serialized
    assert plan.owner_party_included is False
    assert plan.admission_authorized is False
    assert plan.runtime_bound is False
    assert plan.database_writes_authorized is False
    assert plan.native_population_director_bound is False
    assert plan.embedded_python_enabled is False


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("manifest_digest", "manifest.sha256"),
        ("schedule_digest", "schedule.sha256"),
        ("identity", "runtime identity field"),
        ("phase", "global_phase_cap"),
        ("band", "level-band entry"),
        ("pacing", "wall-clock time"),
        ("release", "verified CPI-225 release surface"),
    ],
)
def test_rejects_manifest_release_and_policy_drift(
    mutation: str, message: str
) -> None:
    candidate, schedule, release_surface = _identity_independent_fixture()
    manifest_sha256 = CPI196_MANIFEST_SHA256

    if mutation == "manifest_digest":
        manifest_sha256 = "0" * 64
    elif mutation == "schedule_digest":
        schedule["sha256"] = "0" * 64
        cast(dict[str, object], candidate["schedule"])["sha256"] = "0" * 64
    elif mutation == "identity":
        candidate["owner_character_guid"] = 142
    elif mutation == "phase":
        cast(dict[str, object], candidate["level_band_policy"])["global_phase_cap"] = 20
    elif mutation == "band":
        bands = cast(list[dict[str, object]], cast(dict[str, object], candidate["level_band_policy"])["bands"])
        bands[1]["owner_level_offset"] = 0
    elif mutation == "pacing":
        cast(dict[str, object], candidate["starter_zone_pacing"])["time_based_wave_authorization"] = True
    elif mutation == "release":
        release_surface["release_surface_sha256"] = "0" * 64

    with pytest.raises((PopulationPolicyError, ValueError), match=message):
        PopulationManifestAdapter.consume_identity_independent_candidate(
            candidate,
            schedule,
            release_surface,
            manifest_sha256=manifest_sha256,
        )


def _compiled_fixture() -> tuple[dict[str, object], dict[str, object]]:
    schedule = compile_wave_schedule_v2(
        {
            "schema_version": 2,
            "schedule_version": "cpi-196-adapter-test-v1",
            "realm_id": "fresh-playerbots-realm",
            "maximum_owner_level": 3,
            "permanent_roster_target": 6,
            "curve_kind": "normalized_exponential",
            "decay": "1",
        }
    )
    identity_inputs = [
        ("alliance", 1, 1, "human", "Aldra"),
        ("alliance", 3, 3, "dwarf_gnome", "Belric"),
        ("alliance", 11, 7, "draenei", "Corin"),
        ("horde", 2, 3, "orc_troll", "Dessa"),
        ("horde", 8, 8, "orc_troll", "Elric"),
        ("horde", 5, 5, "undead", "Fara"),
    ]
    identities: list[dict[str, object]] = []
    for index, (faction, race, class_id, region, name) in enumerate(identity_inputs):
        identities.append(
            {
                "manifest_identity": f"ambient-{index + 1:04d}",
                "account_name": f"RNDBOT{index + 1:04d}",
                "character_name": name,
                "faction": faction,
                "race": race,
                "class": class_id,
                "role_intent": "damage",
                "spec_template": f"t{class_id:02d}-damage",
                "starting_region": region,
                "creation_wave_owner_level": index // 2 + 1,
                "wave_ordinal": index % 2 + 1,
                "admission_eligible": True,
                "appearance": {
                    "gender": 0,
                    "skin": 0,
                    "face": 0,
                    "hair_style": 0,
                    "hair_color": 0,
                    "facial_hair": 0,
                    "outfit_id": 0,
                },
            }
        )
    raw_manifest: dict[str, object] = {
        "manifest_schema": 1,
        "manifest_version": "cpi-196-adapter-test-v1",
        "realm_id": schedule["realm_id"],
        "schedule_digest": schedule["schedule_digest"],
        "composition": {
            "faction_shares": {"alliance": "0.5", "horde": "0.5"},
            "role_shares": {"tank": "0", "healer": "0", "damage": "1"},
            "required_class_floor_per_faction": 0,
            "required_region_floor": 0,
        },
        "spec_templates": [
            {
                "template_id": f"t{class_id:02d}-damage",
                "class": class_id,
                "role_intent": "damage",
                "minimum_functional_level": 1,
            }
            for class_id in (1, 3, 5, 7, 8)
        ],
        "identities": identities,
    }
    return schedule, compile_population_manifest(raw_manifest, schedule)


def _acceptance(manifest: dict[str, object], schedule: dict[str, object]) -> dict[str, object]:
    return {
        "status": "ACCEPTED",
        "acceptance_id": "cpi-196-adapter-test-acceptance-v1",
        "approver": "test-owner",
        "approved_at_utc": "2026-09-03T00:00:00Z",
        "manifest_digest": manifest["manifest_digest"],
        "schedule_digest": schedule["schedule_digest"],
    }


def test_consumes_accepted_v2_manifest_as_unbound_projection() -> None:
    schedule, manifest = _compiled_fixture()

    contract = PopulationManifestAdapter.consume_accepted_manifest(
        manifest,
        schedule,
        _acceptance(manifest, schedule),
        expected_target=6,
    )

    assert contract.realm_id == "fresh-playerbots-realm"
    assert contract.durable_target == 6
    assert contract.wave_counts == (2, 2, 2)
    assert len(contract.identity_references) == 6
    assert [item.manifest_identity for item in contract.identities_for_wave(2)] == [
        "ambient-0003",
        "ambient-0004",
    ]
    assert contract.wave_idempotency_key(1) == digest(
        {
            "realm_id": "fresh-playerbots-realm",
            "owner_level": 1,
            "schedule_digest": schedule["schedule_digest"],
        }
    )

    # These are deliberate contract invariants, not a claim that runtime
    # admission or database binding has occurred.
    assert contract.admission_authorized is False
    assert contract.account_binding_count == 0
    assert contract.character_binding_count == 0
    assert all(not hasattr(contract, field) for field in ("account_id", "character_guid", "guid"))


def test_rejects_unresolved_candidate_without_digest_or_rows() -> None:
    schedule, manifest = _compiled_fixture()
    unresolved_candidate = {
        "status": "UNRESOLVED",
        "authority": "NON_AUTHORITATIVE",
        "row_generation": "FORBIDDEN_UNTIL_DECISIONS_ACCEPTED",
        "rows": [],
    }

    with pytest.raises(PopulationPolicyError, match="manifest_digest"):
        PopulationManifestAdapter.consume_accepted_manifest(
            unresolved_candidate,
            schedule,
            _acceptance(manifest, schedule),
        )


def test_rejects_runtime_identity_fields_even_when_digest_is_recomputed() -> None:
    schedule, manifest = _compiled_fixture()
    tampered = deepcopy(manifest)
    identities = cast(list[dict[str, object]], tampered["identities"])
    identities[0]["character_guid"] = 1001
    payload = {key: value for key, value in tampered.items() if key != "manifest_digest"}
    tampered["manifest_digest"] = digest(payload)
    acceptance = _acceptance(tampered, schedule)

    with pytest.raises(PopulationPolicyError, match="runtime identity field"):
        PopulationManifestAdapter.consume_accepted_manifest(tampered, schedule, acceptance)


def test_requires_explicit_matching_acceptance_receipt() -> None:
    schedule, manifest = _compiled_fixture()
    acceptance = _acceptance(manifest, schedule)
    acceptance["status"] = "UNRESOLVED"

    with pytest.raises(PopulationPolicyError, match="status ACCEPTED"):
        PopulationManifestAdapter.consume_accepted_manifest(manifest, schedule, acceptance)

    mismatched = _acceptance(manifest, schedule)
    mismatched["schedule_digest"] = "0" * 64
    with pytest.raises(PopulationPolicyError, match="does not bind the supplied schedule"):
        PopulationManifestAdapter.consume_accepted_manifest(manifest, schedule, mismatched)


def test_requires_owner_independent_schedule_schema() -> None:
    schedule, manifest = _compiled_fixture()
    legacy_schedule = {**schedule, "schema_version": 1}
    legacy_schedule["schedule_digest"] = digest(
        {key: value for key, value in legacy_schedule.items() if key != "schedule_digest"}
    )
    legacy_manifest = deepcopy(manifest)
    legacy_manifest["schedule_digest"] = legacy_schedule["schedule_digest"]
    legacy_manifest["manifest_digest"] = digest(
        {key: value for key, value in legacy_manifest.items() if key != "manifest_digest"}
    )
    with pytest.raises(PopulationPolicyError, match="schedule.schema_version must be 2"):
        PopulationManifestAdapter.consume_accepted_manifest(
            legacy_manifest,
            legacy_schedule,
            _acceptance(legacy_manifest, legacy_schedule),
        )
