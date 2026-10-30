"""Fail-closed first-wave schedule, manifest, factory, and capacity coupling.

The population schedule is authoritative.  A factory may create exactly the
level-one identities assigned to wave one; it may not choose the population
size, cross-pack accounts between waves, or use stock random generation.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import cast

from .population_policy import (
    PopulationPolicyError,
    _require_exact_keys,
    _require_int,
    _require_token,
    _verify_compiled_artifact,
    digest,
)


def _require_factory_prediction(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise PopulationPolicyError("factory_prediction must be an object")
    _require_exact_keys(
        raw,
        {"mode", "predicted_account_count", "predicted_character_count"},
        "factory_prediction",
    )
    if raw.get("mode") != "exact_manifest_wave":
        raise PopulationPolicyError("factory_prediction.mode must be 'exact_manifest_wave'")
    return {
        "mode": "exact_manifest_wave",
        "predicted_account_count": _require_int(
            raw.get("predicted_account_count"), "factory_prediction.predicted_account_count", 1
        ),
        "predicted_character_count": _require_int(
            raw.get("predicted_character_count"), "factory_prediction.predicted_character_count", 1
        ),
    }


def _require_factory_observation(raw: object, stage: str) -> dict[str, int] | None:
    if stage == "prediction":
        if raw is not None:
            raise PopulationPolicyError("factory_observation must be null during prediction")
        return None
    if not isinstance(raw, dict):
        raise PopulationPolicyError("factory_observation must be an object during observation")
    _require_exact_keys(raw, {"observed_account_count", "observed_character_count"}, "factory_observation")
    return {
        "observed_account_count": _require_int(
            raw.get("observed_account_count"), "factory_observation.observed_account_count", 1
        ),
        "observed_character_count": _require_int(
            raw.get("observed_character_count"), "factory_observation.observed_character_count", 1
        ),
    }


def compile_first_wave_coupling(
    raw: Mapping[str, object],
    schedule: Mapping[str, object],
    manifest: Mapping[str, object],
    bindings: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Compile proof that the factory is subordinate to exact wave-one inputs."""

    _require_exact_keys(
        raw,
        {
            "coupling_schema",
            "coupling_version",
            "stage",
            "initial_online_target",
            "measured_concurrent_capacity_ceiling",
            "factory_prediction",
            "factory_observation",
        },
        "first-wave coupling input",
    )
    coupling_schema = _require_int(raw.get("coupling_schema"), "coupling_schema", 2, 2)
    coupling_version = _require_token(raw.get("coupling_version"), "coupling_version")
    stage = raw.get("stage")
    if stage not in {"prediction", "observation"}:
        raise PopulationPolicyError("stage must be 'prediction' or 'observation'")
    initial_online_target = _require_int(raw.get("initial_online_target"), "initial_online_target", 1)
    capacity_ceiling = _require_int(
        raw.get("measured_concurrent_capacity_ceiling"),
        "measured_concurrent_capacity_ceiling",
        1,
    )
    prediction = _require_factory_prediction(raw.get("factory_prediction"))
    observation = _require_factory_observation(raw.get("factory_observation"), stage)

    schedule_digest = _verify_compiled_artifact(schedule, "schedule_digest", "schedule")
    manifest_digest = _verify_compiled_artifact(manifest, "manifest_digest", "manifest")
    if schedule.get("schema_version") != 2:
        raise PopulationPolicyError("first-wave coupling requires schedule schema 2")
    realm_id = _require_token(schedule.get("realm_id"), "schedule.realm_id")
    if manifest.get("realm_id") != realm_id or manifest.get("schedule_digest") != schedule_digest:
        raise PopulationPolicyError("manifest does not bind the supplied schedule and realm")

    waves_raw = schedule.get("waves")
    if not isinstance(waves_raw, list) or not waves_raw or not isinstance(waves_raw[0], dict):
        raise PopulationPolicyError("schedule must contain a wave-one row")
    first_wave = cast(dict[str, object], waves_raw[0])
    if first_wave.get("owner_level") != 1:
        raise PopulationPolicyError("the first schedule row must be owner level 1")
    scheduled_character_count = _require_int(first_wave.get("count"), "schedule wave one count", 1)

    identities_raw = manifest.get("identities")
    if not isinstance(identities_raw, list):
        raise PopulationPolicyError("manifest.identities must be a list")
    wave_by_account: dict[str, set[int]] = defaultdict(set)
    wave_one_identities: set[str] = set()
    wave_one_accounts: set[str] = set()
    account_by_identity: dict[str, str] = {}
    for ordinal, identity_raw in enumerate(identities_raw):
        if not isinstance(identity_raw, dict):
            raise PopulationPolicyError(f"manifest.identities[{ordinal}] must be an object")
        identity = _require_token(
            identity_raw.get("manifest_identity"), f"manifest.identities[{ordinal}].manifest_identity"
        )
        account = _require_token(identity_raw.get("account_name"), f"identity {identity}.account_name").casefold()
        owner_level = _require_int(
            identity_raw.get("creation_wave_owner_level"), f"identity {identity}.creation_wave_owner_level", 1, 80
        )
        wave_by_account[account].add(owner_level)
        account_by_identity[identity] = account
        if owner_level == 1:
            if identity_raw.get("admission_eligible") is not True:
                raise PopulationPolicyError(f"wave-one identity {identity} is not admission eligible")
            wave_one_identities.add(identity)
            wave_one_accounts.add(account)

    cross_wave_accounts = sorted(account for account, levels in wave_by_account.items() if len(levels) != 1)
    if cross_wave_accounts:
        raise PopulationPolicyError("manifest accounts must not contain identities from multiple creation waves")
    manifest_character_count = len(wave_one_identities)
    manifest_account_count = len(wave_one_accounts)
    if manifest_character_count != scheduled_character_count:
        raise PopulationPolicyError("wave-one manifest identity count does not equal schedule W[1]")
    if manifest_account_count != manifest_character_count:
        raise PopulationPolicyError("wave-one production manifest requires one RNDBOT account per character")
    if prediction["predicted_character_count"] != scheduled_character_count:
        raise PopulationPolicyError("factory predicted character count does not equal schedule W[1]")
    if prediction["predicted_account_count"] != manifest_account_count:
        raise PopulationPolicyError(
            "factory predicted account count does not equal the wave-one manifest account count"
        )
    if initial_online_target > scheduled_character_count:
        raise PopulationPolicyError("initial_online_target exceeds the admitted wave-one identity count")
    if initial_online_target > capacity_ceiling:
        raise PopulationPolicyError("initial_online_target exceeds the measured concurrent capacity ceiling")

    binding_digest: str | None = None
    observed_account_count: int | None = None
    observed_character_count: int | None = None
    if stage == "prediction":
        if bindings is not None:
            raise PopulationPolicyError("bindings must be absent during prediction")
    else:
        if bindings is None:
            raise PopulationPolicyError("compiled bindings are required during observation")
        binding_digest = _verify_compiled_artifact(bindings, "binding_digest", "bindings")
        if bindings.get("manifest_digest") != manifest_digest:
            raise PopulationPolicyError("bindings do not bind the supplied manifest")
        binding_rows = bindings.get("bindings")
        if not isinstance(binding_rows, list):
            raise PopulationPolicyError("bindings.bindings must be a list")
        observed_identities: set[str] = set()
        observed_account_ids: set[int] = set()
        account_ids_by_name: dict[str, int] = {}
        for ordinal, binding_raw in enumerate(binding_rows):
            if not isinstance(binding_raw, dict):
                raise PopulationPolicyError(f"bindings.bindings[{ordinal}] must be an object")
            identity = _require_token(
                binding_raw.get("manifest_identity"), f"bindings.bindings[{ordinal}].manifest_identity"
            )
            if identity not in wave_one_identities:
                raise PopulationPolicyError("observed bindings contain a non-wave-one identity")
            account_id = _require_int(binding_raw.get("account_id"), f"binding {identity}.account_id", 1)
            account_name = account_by_identity[identity]
            previous = account_ids_by_name.setdefault(account_name, account_id)
            if previous != account_id:
                raise PopulationPolicyError("one wave-one manifest account maps to multiple observed account IDs")
            observed_identities.add(identity)
            observed_account_ids.add(account_id)
        if observed_identities != wave_one_identities:
            raise PopulationPolicyError("observed bindings are not the exact wave-one identity set")
        observed_character_count = len(observed_identities)
        observed_account_count = len(observed_account_ids)
        if observation is None:
            raise AssertionError("observation stage lost its required observation")
        if observation["observed_character_count"] != observed_character_count:
            raise PopulationPolicyError("reported factory observed character count does not match exact bindings")
        if observation["observed_account_count"] != observed_account_count:
            raise PopulationPolicyError("reported factory observed account count does not match exact bindings")
        if observed_character_count != prediction["predicted_character_count"]:
            raise PopulationPolicyError("factory observed and predicted character counts differ")
        if observed_account_count != prediction["predicted_account_count"]:
            raise PopulationPolicyError("factory observed and predicted account counts differ")

    payload: dict[str, object] = {
        "coupling_schema": coupling_schema,
        "coupling_version": coupling_version,
        "stage": stage,
        "realm_id": realm_id,
        "source_digests": {
            "schedule_digest": schedule_digest,
            "manifest_digest": manifest_digest,
            "binding_digest": binding_digest,
        },
        "factory_contract": {
            "mode": "exact_manifest_wave",
            "stock_bulk_factory_enabled": False,
            "runtime_identity_randomization_enabled": False,
            "creation_owner_level": 1,
            "creation_level": 1,
            "creation_xp": 0,
            "cross_wave_account_packing": False,
            "one_account_per_character": True,
        },
        "equality": {
            "scheduled_character_count": scheduled_character_count,
            "manifest_character_count": manifest_character_count,
            "manifest_account_count": manifest_account_count,
            "factory_predicted_character_count": prediction["predicted_character_count"],
            "factory_predicted_account_count": prediction["predicted_account_count"],
            "factory_observed_character_count": observed_character_count,
            "factory_observed_account_count": observed_account_count,
        },
        "capacity": {
            "initial_online_target": initial_online_target,
            "measured_concurrent_capacity_ceiling": capacity_ceiling,
        },
        "result": "PASS_PREDICTION" if stage == "prediction" else "PASS_OBSERVATION",
    }
    return {**payload, "coupling_digest": digest(payload)}
