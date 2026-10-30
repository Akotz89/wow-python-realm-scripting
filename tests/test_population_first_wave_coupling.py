from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest

from realm_scripts.population_first_wave_coupling import compile_first_wave_coupling
from realm_scripts.population_policy import PopulationPolicyError, compile_identity_bindings, digest
from test_population_policy import v2_fixture

SCHEMA = Path(__file__).parents[1] / "schemas/population-first-wave-coupling-v2.schema.json"


def coupling_input(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "coupling_schema": 2,
        "coupling_version": "candidate-first-wave-v2",
        "stage": "prediction",
        "initial_online_target": 5,
        "measured_concurrent_capacity_ceiling": 10,
        "factory_prediction": {
            "mode": "exact_manifest_wave",
            "predicted_account_count": 5,
            "predicted_character_count": 5,
        },
        "factory_observation": None,
    }
    raw.update(overrides)
    return raw


def test_prediction_binds_exact_wave_one_and_capacity_without_owner_identity() -> None:
    policy, manifest, _, _, _ = v2_fixture()
    schedule = cast(dict[str, object], policy["schedule"])
    result = compile_first_wave_coupling(coupling_input(), schedule, manifest)
    assert result["result"] == "PASS_PREDICTION"
    equality = cast(dict[str, object], result["equality"])
    assert equality == {
        "scheduled_character_count": 5,
        "manifest_character_count": 5,
        "manifest_account_count": 5,
        "factory_predicted_character_count": 5,
        "factory_predicted_account_count": 5,
        "factory_observed_character_count": None,
        "factory_observed_account_count": None,
    }
    assert result["coupling_digest"] == digest(
        {key: value for key, value in result.items() if key != "coupling_digest"}
    )
    text = json.dumps(result, sort_keys=True)
    assert "owner_anchor_guid" not in text
    assert "owner_account_id" not in text
    assert "owner_character_guid" not in text


def test_observation_requires_exact_wave_one_binding_set_and_account_count() -> None:
    policy, manifest, _, _, _ = v2_fixture()
    schedule = cast(dict[str, object], policy["schedule"])
    identities = cast(list[dict[str, object]], manifest["identities"])
    wave_one = [identity for identity in identities if identity["creation_wave_owner_level"] == 1]
    bindings = compile_identity_bindings(
        [
            {
                "manifest_identity": identity["manifest_identity"],
                "account_id": 100 + ordinal,
                "character_guid": 1000 + ordinal,
            }
            for ordinal, identity in enumerate(wave_one, start=1)
        ],
        manifest,
    )
    raw = coupling_input(
        stage="observation",
        factory_observation={"observed_account_count": 5, "observed_character_count": 5},
    )
    result = compile_first_wave_coupling(raw, schedule, manifest, bindings)
    assert result["result"] == "PASS_OBSERVATION"
    equality = cast(dict[str, object], result["equality"])
    assert equality["factory_observed_character_count"] == 5
    assert equality["factory_observed_account_count"] == 5
    assert cast(dict[str, object], result["source_digests"])["binding_digest"] == bindings["binding_digest"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {
                "factory_prediction": {
                    "mode": "exact_manifest_wave",
                    "predicted_account_count": 5,
                    "predicted_character_count": 4,
                }
            },
            "predicted character",
        ),
        (
            {
                "factory_prediction": {
                    "mode": "exact_manifest_wave",
                    "predicted_account_count": 4,
                    "predicted_character_count": 5,
                }
            },
            "predicted account",
        ),
        ({"initial_online_target": 5, "measured_concurrent_capacity_ceiling": 4}, "capacity ceiling"),
        ({"initial_online_target": 6, "measured_concurrent_capacity_ceiling": 10}, "admitted wave-one"),
        (
            {
                "factory_prediction": {
                    "mode": "stock_bulk_random",
                    "predicted_account_count": 5,
                    "predicted_character_count": 5,
                }
            },
            "exact_manifest_wave",
        ),
    ],
)
def test_prediction_fails_closed_on_count_capacity_or_factory_mode_drift(
    overrides: dict[str, object], message: str
) -> None:
    policy, manifest, _, _, _ = v2_fixture()
    with pytest.raises(PopulationPolicyError, match=message):
        compile_first_wave_coupling(
            coupling_input(**overrides), cast(dict[str, object], policy["schedule"]), manifest
        )


def test_observation_rejects_partial_or_later_wave_bindings() -> None:
    policy, manifest, _, _, _ = v2_fixture()
    schedule = cast(dict[str, object], policy["schedule"])
    identities = cast(list[dict[str, object]], manifest["identities"])
    wave_one = [identity for identity in identities if identity["creation_wave_owner_level"] == 1]
    partial = compile_identity_bindings(
        [
            {
                "manifest_identity": identity["manifest_identity"],
                "account_id": 100 + ordinal,
                "character_guid": 1000 + ordinal,
            }
            for ordinal, identity in enumerate(wave_one[:-1], start=1)
        ],
        manifest,
    )
    raw = coupling_input(
        stage="observation",
        factory_observation={"observed_account_count": 4, "observed_character_count": 4},
    )
    with pytest.raises(PopulationPolicyError, match="exact wave-one identity set"):
        compile_first_wave_coupling(raw, schedule, manifest, partial)

    later = identities[5]
    mixed = compile_identity_bindings(
        [
            {
                "manifest_identity": identity["manifest_identity"],
                "account_id": 100 + ordinal,
                "character_guid": 1000 + ordinal,
            }
            for ordinal, identity in enumerate([*wave_one[:-1], later], start=1)
        ],
        manifest,
    )
    with pytest.raises(PopulationPolicyError, match="non-wave-one"):
        compile_first_wave_coupling(raw, schedule, manifest, mixed)


def test_prediction_rejects_account_packing_across_creation_waves() -> None:
    policy, manifest, _, _, _ = v2_fixture()
    schedule = cast(dict[str, object], policy["schedule"])
    tampered = deepcopy(manifest)
    identities = cast(list[dict[str, object]], tampered["identities"])
    identities[5]["account_name"] = identities[0]["account_name"]
    tampered["reports"] = deepcopy(cast(dict[str, object], manifest["reports"]))
    payload = {key: value for key, value in tampered.items() if key != "manifest_digest"}
    tampered["manifest_digest"] = digest(payload)
    with pytest.raises(PopulationPolicyError, match="multiple creation waves"):
        compile_first_wave_coupling(coupling_input(), schedule, tampered)


def test_prediction_rejects_same_wave_account_packing() -> None:
    policy, manifest, _, _, _ = v2_fixture()
    schedule = cast(dict[str, object], policy["schedule"])
    tampered = deepcopy(manifest)
    identities = cast(list[dict[str, object]], tampered["identities"])
    identities[1]["account_name"] = identities[0]["account_name"]
    payload = {key: value for key, value in tampered.items() if key != "manifest_digest"}
    tampered["manifest_digest"] = digest(payload)
    with pytest.raises(PopulationPolicyError, match="one RNDBOT account per character"):
        compile_first_wave_coupling(coupling_input(), schedule, tampered)


def test_schema_is_strict_and_encodes_nonrandom_level_one_contract() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    factory = schema["properties"]["factory_contract"]["properties"]
    assert factory["mode"]["const"] == "exact_manifest_wave"
    assert factory["stock_bulk_factory_enabled"]["const"] is False
    assert factory["runtime_identity_randomization_enabled"]["const"] is False
    assert factory["creation_level"]["const"] == 1
    assert factory["creation_xp"]["const"] == 0
    assert factory["cross_wave_account_packing"]["const"] is False
    assert factory["one_account_per_character"]["const"] is True
