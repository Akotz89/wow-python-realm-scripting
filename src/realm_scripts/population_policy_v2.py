"""Owner-independent v2 contracts for the persistent RandomBot population.

Version 1 remains available as historical prototype code.  Version 2 removes
owner identity from every immutable artifact.  The mutable owner binding is a
separate compare-and-swap state machine and wave one is the only wave that can
be admitted while that binding is unbound.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from decimal import Decimal, localcontext
from typing import cast

from .population_policy import (
    APPEARANCE_FIELDS,
    DENIED_PROGRESSION_MUTATIONS,
    HELD_ACTIVITY_TYPES,
    NON_DK_CLASSES,
    PopulationPolicyError,
    _apportion,
    _compile_bands,
    _decimal_text,
    _repair_monotone,
    _require_bool,
    _require_decimal,
    _require_digest,
    _require_exact_keys,
    _require_int,
    _require_token,
    _verify_compiled_artifact,
    compile_admission_recovery,
    digest,
)


def compile_wave_schedule_v2(raw: Mapping[str, object]) -> dict[str, object]:
    """Compile a deterministic owner-identity-free wave schedule."""

    _require_exact_keys(
        raw,
        {
            "schema_version",
            "schedule_version",
            "realm_id",
            "maximum_owner_level",
            "permanent_roster_target",
            "curve_kind",
            "decay",
        },
        "schedule",
    )
    schema_version = _require_int(raw.get("schema_version"), "schema_version", 2, 2)
    schedule_version = _require_token(raw.get("schedule_version"), "schedule_version")
    realm_id = _require_token(raw.get("realm_id"), "realm_id")
    maximum_owner_level = _require_int(raw.get("maximum_owner_level"), "maximum_owner_level", 1, 80)
    permanent_roster_target = _require_int(raw.get("permanent_roster_target"), "permanent_roster_target", 1)
    if raw.get("curve_kind") != "normalized_exponential":
        raise PopulationPolicyError("curve_kind must be 'normalized_exponential'")
    decay = _require_decimal(raw.get("decay"), "decay")
    if not Decimal(0) < decay <= Decimal(1):
        raise PopulationPolicyError("decay must satisfy 0 < decay <= 1")

    with localcontext() as context:
        context.prec = 80
        weights = tuple(decay**index for index in range(maximum_owner_level))
        allocations = list(_apportion(permanent_roster_target, weights))
    _repair_monotone(allocations)
    if sum(allocations) != permanent_roster_target:
        raise AssertionError("monotonicity repair changed the roster total")
    if any(left < right for left, right in zip(allocations, allocations[1:], strict=False)):
        raise AssertionError("monotonicity repair did not converge")

    payload: dict[str, object] = {
        "schema_version": schema_version,
        "schedule_version": schedule_version,
        "realm_id": realm_id,
        "maximum_owner_level": maximum_owner_level,
        "permanent_roster_target": permanent_roster_target,
        "wave_curve": {"kind": "normalized_exponential", "decay": _decimal_text(decay)},
        "allocation": {"kind": "deterministic_monotone_integer"},
        "waves": [
            {"owner_level": owner_level, "count": count}
            for owner_level, count in enumerate(allocations, start=1)
        ],
    }
    return {**payload, "schedule_digest": digest(payload)}


def compile_policy_v2(raw: Mapping[str, object]) -> dict[str, object]:
    """Compile the v2 schedule and dynamic-band capacity epochs."""

    _require_exact_keys(
        raw,
        {"policy_schema", "policy_version", "global_phase_cap", "schedule", "bands"},
        "policy",
    )
    policy_schema = _require_int(raw.get("policy_schema"), "policy_schema", 2, 2)
    policy_version = _require_token(raw.get("policy_version"), "policy_version")
    schedule_raw = raw.get("schedule")
    if not isinstance(schedule_raw, dict):
        raise PopulationPolicyError("schedule must be an object")
    schedule = compile_wave_schedule_v2(schedule_raw)
    global_phase_cap = _require_int(raw.get("global_phase_cap"), "global_phase_cap", 1, 80)
    names, shares, ceilings = _compile_bands(raw.get("bands"), global_phase_cap)

    cumulative = 0
    epochs: list[dict[str, object]] = []
    for wave in cast(list[dict[str, int]], schedule["waves"]):
        cumulative += wave["count"]
        capacities = _apportion(cumulative, shares)
        epochs.append(
            {
                "owner_level": wave["owner_level"],
                "scheduled_cumulative_population": cumulative,
                "capacities": dict(zip(names, capacities, strict=True)),
            }
        )

    payload: dict[str, object] = {
        "policy_schema": policy_schema,
        "policy_version": policy_version,
        "global_phase_cap": global_phase_cap,
        "schedule": schedule,
        "bands": [
            {"name": name, "share": _decimal_text(share), "ceiling": ceilings[name]}
            for name, share in zip(names, shares, strict=True)
        ],
        "band_capacity_epochs": epochs,
    }
    return {**payload, "policy_digest": digest(payload)}


def _validate_capacity_limits(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        raise PopulationPolicyError("capacity_limits must be an object")
    fields = {
        "server_session_budget",
        "safety_reserve",
        "concurrent_active_target",
        "creation_batch_size",
        "login_batch_size",
        "max_active_per_map",
        "max_active_per_zone",
        "max_active_per_starting_region",
    }
    _require_exact_keys(raw, fields, "capacity_limits")
    capacities = {field: _require_int(raw[field], f"capacity_limits.{field}", 1) for field in sorted(fields)}
    if capacities["safety_reserve"] < 5:
        raise PopulationPolicyError("capacity_limits.safety_reserve must preserve at least the five-character party")
    if capacities["concurrent_active_target"] + capacities["safety_reserve"] > capacities["server_session_budget"]:
        raise PopulationPolicyError("concurrent_active_target plus safety_reserve exceeds server_session_budget")
    if capacities["creation_batch_size"] > capacities["concurrent_active_target"]:
        raise PopulationPolicyError("creation_batch_size must not exceed concurrent_active_target")
    if capacities["login_batch_size"] > capacities["concurrent_active_target"]:
        raise PopulationPolicyError("login_batch_size must not exceed concurrent_active_target")
    density_chain = (
        capacities["max_active_per_starting_region"],
        capacities["max_active_per_zone"],
        capacities["max_active_per_map"],
        capacities["concurrent_active_target"],
    )
    if any(left > right for left, right in zip(density_chain, density_chain[1:], strict=False)):
        raise PopulationPolicyError("starting-region zone map and global active limits must be non-decreasing")
    return capacities


def _validate_activity_whitelist(raw: object) -> list[str]:
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise PopulationPolicyError("held_activity_whitelist must be a sorted list of activity tokens")
    whitelist = cast(list[str], raw)
    if whitelist != sorted(set(whitelist)):
        raise PopulationPolicyError("held_activity_whitelist must be sorted and contain no duplicates")
    unknown = sorted(set(whitelist) - set(HELD_ACTIVITY_TYPES))
    if unknown:
        raise PopulationPolicyError(f"held_activity_whitelist contains unknown activities: {', '.join(unknown)}")
    return whitelist


def compile_runtime_snapshot_v2(
    raw: Mapping[str, object],
    policy: Mapping[str, object],
    manifest: Mapping[str, object],
    bindings: Mapping[str, object],
) -> dict[str, object]:
    """Bind immutable v2 artifacts without embedding an owner identity."""

    _require_exact_keys(
        raw,
        {"snapshot_schema", "snapshot_version", "capacity_limits", "held_activity_whitelist"},
        "runtime snapshot input",
    )
    snapshot_schema = _require_int(raw.get("snapshot_schema"), "snapshot_schema", 2, 2)
    snapshot_version = _require_token(raw.get("snapshot_version"), "snapshot_version")
    policy_digest = _verify_compiled_artifact(policy, "policy_digest", "policy")
    manifest_digest = _verify_compiled_artifact(manifest, "manifest_digest", "manifest")
    binding_digest = _verify_compiled_artifact(bindings, "binding_digest", "bindings")

    schedule = policy.get("schedule")
    if not isinstance(schedule, dict):
        raise PopulationPolicyError("compiled policy schedule must be an object")
    schedule_digest = _verify_compiled_artifact(schedule, "schedule_digest", "policy.schedule")
    if schedule.get("schema_version") != 2 or policy.get("policy_schema") != 2:
        raise PopulationPolicyError("runtime snapshot v2 requires policy and schedule schema 2")
    if manifest.get("schedule_digest") != schedule_digest:
        raise PopulationPolicyError("manifest does not bind the compiled policy schedule")
    if bindings.get("manifest_digest") != manifest_digest:
        raise PopulationPolicyError("bindings do not bind the compiled manifest")

    realm_id = _require_token(schedule.get("realm_id"), "realm_id")
    if manifest.get("realm_id") != realm_id:
        raise PopulationPolicyError("policy schedule and manifest realm_id values must match")
    maximum_owner_level = _require_int(schedule.get("maximum_owner_level"), "maximum_owner_level", 1, 80)
    permanent_roster_target = _require_int(schedule.get("permanent_roster_target"), "permanent_roster_target", 1)
    global_phase_cap = _require_int(policy.get("global_phase_cap"), "global_phase_cap", 1, 80)
    if global_phase_cap > maximum_owner_level:
        raise PopulationPolicyError("global_phase_cap must not exceed maximum_owner_level")

    identities = manifest.get("identities")
    binding_rows = bindings.get("bindings")
    if not isinstance(identities, list) or len(identities) != permanent_roster_target:
        raise PopulationPolicyError("compiled manifest identity count must equal permanent_roster_target")
    if not isinstance(binding_rows, list) or len(binding_rows) > len(identities):
        raise PopulationPolicyError("compiled binding count must not exceed manifest identity count")

    capacities = _validate_capacity_limits(raw.get("capacity_limits"))
    whitelist = _validate_activity_whitelist(raw.get("held_activity_whitelist"))
    bands = policy.get("bands")
    epochs = policy.get("band_capacity_epochs")
    if not isinstance(bands, list) or not isinstance(epochs, list):
        raise PopulationPolicyError("compiled policy must contain bands and band_capacity_epochs lists")
    if len(epochs) != maximum_owner_level:
        raise PopulationPolicyError("band_capacity_epochs must cover every owner level")

    payload: dict[str, object] = {
        "snapshot_schema": snapshot_schema,
        "snapshot_version": snapshot_version,
        "realm_id": realm_id,
        "source_digests": {
            "binding_digest": binding_digest,
            "manifest_digest": manifest_digest,
            "policy_digest": policy_digest,
            "schedule_digest": schedule_digest,
        },
        "maximum_owner_level": maximum_owner_level,
        "permanent_roster_target": permanent_roster_target,
        "manifest_identity_count": len(identities),
        "binding_count": len(binding_rows),
        "global_phase_cap": global_phase_cap,
        "effective_ceiling_rule": "min(global_phase_cap,dynamic_band_ceiling)",
        "bands": bands,
        "band_capacity_epochs": epochs,
        "capacity_limits": capacities,
        "progression_hold": {
            "allowed_activities": whitelist,
            "bank_xp": False,
            "delevel_on_demotion": False,
            "denied_progression_mutations": list(DENIED_PROGRESSION_MUTATIONS),
            "unknown_activity": "deny",
        },
        "failure_policy": {
            "invalid_snapshot": "reject",
            "missing_snapshot": "fail_closed",
            "runtime_reload_failure": "retain_last_verified_or_fail_closed",
        },
    }
    return {**payload, "snapshot_digest": digest(payload)}


def _wave_key(realm_id: str, owner_level: int, schedule_digest: str) -> str:
    return digest(
        {
            "realm_id": realm_id,
            "owner_level": owner_level,
            "schedule_digest": schedule_digest,
        }
    )


def compile_population_install_plan_v2(
    raw: Mapping[str, object],
    snapshot: Mapping[str, object],
    policy: Mapping[str, object],
    manifest: Mapping[str, object],
) -> dict[str, object]:
    """Compile the v2 immutable seed with wave one and no owner binding."""

    _require_exact_keys(raw, {"installation_schema", "installation_version"}, "installation input")
    installation_schema = _require_int(raw.get("installation_schema"), "installation_schema", 2, 2)
    installation_version = _require_token(raw.get("installation_version"), "installation_version")
    snapshot_digest = _verify_compiled_artifact(snapshot, "snapshot_digest", "snapshot")
    policy_digest = _verify_compiled_artifact(policy, "policy_digest", "policy")
    manifest_digest = _verify_compiled_artifact(manifest, "manifest_digest", "manifest")
    if snapshot.get("snapshot_schema") != 2:
        raise PopulationPolicyError("installation v2 requires snapshot schema 2")

    source_digests = snapshot.get("source_digests")
    if not isinstance(source_digests, dict):
        raise PopulationPolicyError("snapshot.source_digests must be an object")
    _require_exact_keys(
        source_digests,
        {"binding_digest", "manifest_digest", "policy_digest", "schedule_digest"},
        "snapshot.source_digests",
    )
    if source_digests.get("policy_digest") != policy_digest:
        raise PopulationPolicyError("snapshot does not bind the supplied policy")
    if source_digests.get("manifest_digest") != manifest_digest:
        raise PopulationPolicyError("snapshot does not bind the supplied manifest")
    if snapshot.get("binding_count") != 0:
        raise PopulationPolicyError("fresh installation requires an empty database-binding artifact")

    schedule = policy.get("schedule")
    if not isinstance(schedule, dict):
        raise PopulationPolicyError("compiled policy schedule must be an object")
    schedule_digest = _verify_compiled_artifact(schedule, "schedule_digest", "policy.schedule")
    if schedule.get("schema_version") != 2 or source_digests.get("schedule_digest") != schedule_digest:
        raise PopulationPolicyError("installation v2 requires and must bind schedule schema 2")
    realm_id = _require_token(snapshot.get("realm_id"), "snapshot.realm_id")
    if schedule.get("realm_id") != realm_id or manifest.get("realm_id") != realm_id:
        raise PopulationPolicyError("installation realm_id values do not match")
    if manifest.get("schedule_digest") != schedule_digest:
        raise PopulationPolicyError("manifest does not bind the supplied schedule")

    maximum_owner_level = _require_int(snapshot.get("maximum_owner_level"), "maximum_owner_level", 1, 80)
    permanent_roster_target = _require_int(snapshot.get("permanent_roster_target"), "permanent_roster_target", 1)
    global_phase_cap = _require_int(snapshot.get("global_phase_cap"), "global_phase_cap", 1, 80)
    schedule_version = _require_token(schedule.get("schedule_version"), "schedule.schedule_version")
    waves_raw = schedule.get("waves")
    identities_raw = manifest.get("identities")
    if not isinstance(waves_raw, list) or len(waves_raw) != maximum_owner_level:
        raise PopulationPolicyError("schedule waves must cover every owner level")
    if not isinstance(identities_raw, list) or len(identities_raw) != permanent_roster_target:
        raise PopulationPolicyError("manifest identity count must equal permanent_roster_target")

    wave_rows: list[dict[str, object]] = []
    expected_wave_counts: dict[int, int] = {}
    for ordinal, wave_raw in enumerate(waves_raw):
        if not isinstance(wave_raw, dict):
            raise PopulationPolicyError(f"schedule waves[{ordinal}] must be an object")
        owner_level = _require_int(wave_raw.get("owner_level"), f"schedule waves[{ordinal}].owner_level", 1, 80)
        count = _require_int(wave_raw.get("count"), f"schedule waves[{ordinal}].count", 0)
        if owner_level != ordinal + 1:
            raise PopulationPolicyError("schedule waves must be sorted and contiguous")
        expected_wave_counts[owner_level] = count
        wave_rows.append(
            {
                "owner_level": owner_level,
                "wave_idempotency_key": _wave_key(realm_id, owner_level, schedule_digest),
                "planned_count": count,
                "initial_state": "authorized" if owner_level == 1 else "planned",
            }
        )

    account_sizes = Counter(
        _require_token(item.get("account_name"), "identity.account_name").casefold()
        for item in identities_raw
        if isinstance(item, dict)
    )
    manifest_rows: list[dict[str, object]] = []
    actual_wave_counts: Counter[int] = Counter()
    for ordinal, identity_raw in enumerate(identities_raw):
        if not isinstance(identity_raw, dict):
            raise PopulationPolicyError(f"manifest identities[{ordinal}] must be an object")
        identity = _require_token(identity_raw.get("manifest_identity"), "identity.manifest_identity")
        account_name = _require_token(identity_raw.get("account_name"), f"identity {identity}.account_name")
        wave_level = _require_int(
            identity_raw.get("creation_wave_owner_level"),
            f"identity {identity}.creation_wave_owner_level",
            1,
            maximum_owner_level,
        )
        appearance = identity_raw.get("appearance")
        if not isinstance(appearance, dict):
            raise PopulationPolicyError(f"identity {identity}.appearance must be an object")
        _require_exact_keys(appearance, set(APPEARANCE_FIELDS), f"identity {identity}.appearance")
        account_manifest_size = account_sizes[account_name.casefold()]
        if not 1 <= account_manifest_size <= len(NON_DK_CLASSES):
            raise PopulationPolicyError(f"identity {identity} account manifest size is outside 1..9")
        actual_wave_counts[wave_level] += 1
        manifest_rows.append(
            {
                "manifest_identity": identity,
                "wave_idempotency_key": cast(str, wave_rows[wave_level - 1]["wave_idempotency_key"]),
                "owner_level": wave_level,
                "wave_ordinal": _require_int(identity_raw.get("wave_ordinal"), f"identity {identity}.wave_ordinal", 1),
                "account_name": account_name,
                "account_manifest_size": account_manifest_size,
                "character_name": _require_token(
                    identity_raw.get("character_name"), f"identity {identity}.character_name"
                ),
                "faction": _require_token(identity_raw.get("faction"), f"identity {identity}.faction"),
                "race": _require_int(identity_raw.get("race"), f"identity {identity}.race", 1, 11),
                "class": _require_int(identity_raw.get("class"), f"identity {identity}.class", 1, 11),
                "role_intent": _require_token(identity_raw.get("role_intent"), f"identity {identity}.role_intent"),
                "spec_template": _require_token(
                    identity_raw.get("spec_template"), f"identity {identity}.spec_template"
                ),
                "starting_region": _require_token(
                    identity_raw.get("starting_region"), f"identity {identity}.starting_region"
                ),
                "admission_eligible": _require_bool(
                    identity_raw.get("admission_eligible"), f"identity {identity}.admission_eligible"
                ),
                "appearance": {
                    field: _require_int(
                        appearance.get(field),
                        f"identity {identity}.appearance.{field}",
                        0,
                        1 if field == "gender" else 255,
                    )
                    for field in sorted(APPEARANCE_FIELDS)
                },
            }
        )

    if [cast(str, item["manifest_identity"]) for item in manifest_rows] != sorted(
        cast(str, item["manifest_identity"]) for item in manifest_rows
    ):
        raise PopulationPolicyError("installation manifest identities must be sorted")
    if any(actual_wave_counts[level] != count for level, count in expected_wave_counts.items()):
        raise PopulationPolicyError("installation manifest wave counts do not match the schedule")

    payload: dict[str, object] = {
        "installation_schema": installation_schema,
        "installation_version": installation_version,
        "realm_id": realm_id,
        "source_digests": {
            "schedule_digest": schedule_digest,
            "policy_digest": policy_digest,
            "manifest_digest": manifest_digest,
            "snapshot_digest": snapshot_digest,
        },
        "schedule": {
            "schedule_version": schedule_version,
            "maximum_owner_level": maximum_owner_level,
            "permanent_roster_target": permanent_roster_target,
            "global_phase_cap": global_phase_cap,
        },
        "owner_state": {
            "binding_state": "unbound",
            "highest_owner_level_seen": 1,
            "current_epoch": 1,
            "generation": 1,
        },
        "waves": wave_rows,
        "manifest_identities": manifest_rows,
    }
    return {**payload, "installation_digest": digest(payload)}


def _require_nullable_positive_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _require_int(value, name, 1)


def _require_rfc3339_utc(value: object, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z",
        value,
    ):
        raise PopulationPolicyError(f"{name} must be an RFC3339 UTC timestamp ending in Z")
    return value


def compile_owner_binding_transition_v2(
    raw: Mapping[str, object],
    installation: Mapping[str, object],
    ambient_bindings: Mapping[str, object],
) -> dict[str, object]:
    """Compile one audited owner bind/rebind CAS without authorizing a wave."""

    _require_exact_keys(
        raw,
        {
            "binding_schema",
            "binding_version",
            "realm_id",
            "expected_generation",
            "current_owner_state",
            "selection",
        },
        "owner binding input",
    )
    binding_schema = _require_int(raw.get("binding_schema"), "binding_schema", 2, 2)
    binding_version = _require_token(raw.get("binding_version"), "binding_version")
    realm_id = _require_token(raw.get("realm_id"), "realm_id")
    expected_generation = _require_int(raw.get("expected_generation"), "expected_generation", 1)
    installation_digest = _verify_compiled_artifact(installation, "installation_digest", "installation plan")
    binding_digest = _verify_compiled_artifact(ambient_bindings, "binding_digest", "ambient bindings")
    if installation.get("installation_schema") != 2 or installation.get("realm_id") != realm_id:
        raise PopulationPolicyError("owner binding requires a matching installation schema 2 realm")

    source_digests = installation.get("source_digests")
    if not isinstance(source_digests, dict):
        raise PopulationPolicyError("installation source_digests must be an object")
    schedule_digest = _require_digest(source_digests.get("schedule_digest"), "schedule_digest")
    manifest_digest = _require_digest(source_digests.get("manifest_digest"), "manifest_digest")
    if ambient_bindings.get("manifest_digest") != manifest_digest:
        raise PopulationPolicyError("ambient bindings do not bind the installation manifest")

    state_raw = raw.get("current_owner_state")
    if not isinstance(state_raw, dict):
        raise PopulationPolicyError("current_owner_state must be an object")
    _require_exact_keys(
        state_raw,
        {
            "binding_state",
            "owner_account_id",
            "owner_character_guid",
            "highest_owner_level_seen",
            "current_epoch",
            "generation",
        },
        "current_owner_state",
    )
    current_state = state_raw.get("binding_state")
    if current_state not in {"unbound", "bound"}:
        raise PopulationPolicyError("current_owner_state.binding_state must be unbound or bound")
    current_account_id = _require_nullable_positive_int(state_raw.get("owner_account_id"), "owner_account_id")
    current_character_guid = _require_nullable_positive_int(
        state_raw.get("owner_character_guid"), "owner_character_guid"
    )
    if current_state == "unbound":
        if current_account_id is not None or current_character_guid is not None:
            raise PopulationPolicyError("unbound owner state requires null identity")
    elif current_account_id is None or current_character_guid is None:
        raise PopulationPolicyError("bound owner state requires both identity IDs")
    current_generation = _require_int(state_raw.get("generation"), "current_owner_state.generation", 1)
    if current_generation != expected_generation:
        raise PopulationPolicyError("expected_generation does not match current_owner_state.generation")
    highest_owner_level_seen = _require_int(
        state_raw.get("highest_owner_level_seen"), "highest_owner_level_seen", 1, 80
    )
    current_epoch = _require_int(state_raw.get("current_epoch"), "current_epoch", 1, 80)
    install_schedule = installation.get("schedule")
    if not isinstance(install_schedule, dict):
        raise PopulationPolicyError("installation schedule must be an object")
    maximum_owner_level = _require_int(
        install_schedule.get("maximum_owner_level"), "maximum_owner_level", 1, 80
    )
    global_phase_cap = _require_int(install_schedule.get("global_phase_cap"), "global_phase_cap", 1, 80)
    if highest_owner_level_seen > maximum_owner_level:
        raise PopulationPolicyError("owner high-water exceeds the installed maximum owner level")
    expected_epoch = min(highest_owner_level_seen, maximum_owner_level, global_phase_cap)
    if current_epoch != expected_epoch:
        raise PopulationPolicyError("current_epoch must equal the capped owner high-water")

    selection_raw = raw.get("selection")
    if not isinstance(selection_raw, dict):
        raise PopulationPolicyError("selection must be an object")
    _require_exact_keys(
        selection_raw,
        {"account_id", "character_guid", "actor", "selected_at_utc", "resolution_evidence_digest"},
        "selection",
    )
    selected_account_id = _require_int(selection_raw.get("account_id"), "selection.account_id", 1)
    selected_character_guid = _require_int(selection_raw.get("character_guid"), "selection.character_guid", 1)
    actor = _require_token(selection_raw.get("actor"), "selection.actor")
    selected_at = _require_rfc3339_utc(selection_raw.get("selected_at_utc"), "selection.selected_at_utc")
    resolution_digest = _require_digest(
        selection_raw.get("resolution_evidence_digest"), "selection.resolution_evidence_digest"
    )
    if current_state == "bound" and (current_account_id, current_character_guid) == (
        selected_account_id,
        selected_character_guid,
    ):
        raise PopulationPolicyError("owner binding transition must not be a no-op")
    ambient_rows = ambient_bindings.get("bindings")
    if not isinstance(ambient_rows, list):
        raise PopulationPolicyError("ambient bindings must contain a bindings list")
    for ordinal, row in enumerate(ambient_rows):
        if not isinstance(row, dict):
            raise PopulationPolicyError(f"ambient bindings[{ordinal}] must be an object")
        if row.get("account_id") == selected_account_id or row.get("character_guid") == selected_character_guid:
            raise PopulationPolicyError("owner selection overlaps the autonomous RandomBot population")

    next_generation = current_generation + 1
    transition: dict[str, object] = {
        "binding_schema": binding_schema,
        "binding_version": binding_version,
        "realm_id": realm_id,
        "source_digests": {
            "ambient_binding_digest": binding_digest,
            "installation_digest": installation_digest,
            "manifest_digest": manifest_digest,
            "schedule_digest": schedule_digest,
        },
        "operation": "bind" if current_state == "unbound" else "rebind",
        "expected_generation": expected_generation,
        "next_generation": next_generation,
        "previous_owner_state": {
            "binding_state": current_state,
            "owner_account_id": current_account_id,
            "owner_character_guid": current_character_guid,
            "highest_owner_level_seen": highest_owner_level_seen,
            "current_epoch": current_epoch,
        },
        "selected_owner": {
            "account_id": selected_account_id,
            "character_guid": selected_character_guid,
            "actor": actor,
            "selected_at_utc": selected_at,
            "resolution_evidence_digest": resolution_digest,
        },
        "resulting_owner_state": {
            "binding_state": "bound",
            "owner_account_id": selected_account_id,
            "owner_character_guid": selected_character_guid,
            "highest_owner_level_seen": highest_owner_level_seen,
            "current_epoch": current_epoch,
        },
        "authorized_wave_levels": [],
    }
    transition["event_key"] = digest(
        {
            "realm_id": realm_id,
            "installation_digest": installation_digest,
            "expected_generation": expected_generation,
            "next_generation": next_generation,
            "selected_account_id": selected_account_id,
            "selected_character_guid": selected_character_guid,
        }
    )
    return {**transition, "transition_digest": digest(transition)}


def compile_admission_reconciliation_v2(
    raw: Mapping[str, object],
    snapshot: Mapping[str, object],
    manifest: Mapping[str, object],
    bindings: Mapping[str, object],
) -> dict[str, object]:
    """Compile a bounded admission decision with an explicit owner-binding state."""

    _require_exact_keys(
        raw,
        {
            "reconciliation_schema",
            "reconciliation_version",
            "owner_binding_state",
            "owner_binding_generation",
            "owner_account_id",
            "owner_character_guid",
            "observed_owner_level",
            "highest_owner_level_seen",
            "admissions_paused",
            "pause_reason",
            "available_creation_slots",
        },
        "admission reconciliation input",
    )
    reconciliation_schema = _require_int(
        raw.get("reconciliation_schema"), "reconciliation_schema", 2, 2
    )
    reconciliation_version = _require_token(raw.get("reconciliation_version"), "reconciliation_version")
    binding_state = raw.get("owner_binding_state")
    if binding_state not in {"unbound", "bound"}:
        raise PopulationPolicyError("owner_binding_state must be unbound or bound")
    binding_generation = _require_int(raw.get("owner_binding_generation"), "owner_binding_generation", 1)
    owner_account_id = _require_nullable_positive_int(raw.get("owner_account_id"), "owner_account_id")
    owner_character_guid = _require_nullable_positive_int(raw.get("owner_character_guid"), "owner_character_guid")
    highest_owner_level_seen = _require_int(
        raw.get("highest_owner_level_seen"), "highest_owner_level_seen", 1, 80
    )
    if binding_state == "unbound":
        if owner_account_id is not None or owner_character_guid is not None:
            raise PopulationPolicyError("unbound reconciliation requires null owner identity")
    elif owner_account_id is None or owner_character_guid is None:
        raise PopulationPolicyError("bound reconciliation requires both owner identity IDs")

    observed_raw = raw.get("observed_owner_level")
    if binding_state == "unbound":
        if observed_raw is not None:
            raise PopulationPolicyError("observed_owner_level must be null while owner binding is unbound")
        if highest_owner_level_seen != 1:
            raise PopulationPolicyError("unbound owner state can authorize only wave 1")
        observed_owner_level: int | None = None
    else:
        observed_owner_level = _require_int(observed_raw, "observed_owner_level", 1, 80)

    admissions_paused = _require_bool(raw.get("admissions_paused"), "admissions_paused")
    pause_reason_raw = raw.get("pause_reason")
    if admissions_paused:
        pause_reason: str | None = _require_token(pause_reason_raw, "pause_reason")
    elif pause_reason_raw is not None:
        raise PopulationPolicyError("pause_reason must be null when admissions are not paused")
    else:
        pause_reason = None
    available_creation_slots = _require_int(raw.get("available_creation_slots"), "available_creation_slots", 0)

    snapshot_digest = _verify_compiled_artifact(snapshot, "snapshot_digest", "snapshot")
    manifest_digest = _verify_compiled_artifact(manifest, "manifest_digest", "manifest")
    binding_digest = _verify_compiled_artifact(bindings, "binding_digest", "bindings")
    if snapshot.get("snapshot_schema") != 2:
        raise PopulationPolicyError("reconciliation v2 requires snapshot schema 2")
    source_digests = snapshot.get("source_digests")
    if not isinstance(source_digests, dict):
        raise PopulationPolicyError("snapshot.source_digests must be an object")
    _require_exact_keys(
        source_digests,
        {"binding_digest", "manifest_digest", "policy_digest", "schedule_digest"},
        "snapshot.source_digests",
    )
    schedule_digest = _require_digest(source_digests.get("schedule_digest"), "schedule_digest")
    policy_digest = _require_digest(source_digests.get("policy_digest"), "policy_digest")
    if source_digests.get("manifest_digest") != manifest_digest:
        raise PopulationPolicyError("snapshot does not bind the supplied manifest")
    if source_digests.get("binding_digest") != binding_digest:
        raise PopulationPolicyError("snapshot does not bind the supplied database bindings")
    if bindings.get("manifest_digest") != manifest_digest:
        raise PopulationPolicyError("database bindings do not bind the supplied manifest")

    realm_id = _require_token(snapshot.get("realm_id"), "snapshot.realm_id")
    if manifest.get("realm_id") != realm_id:
        raise PopulationPolicyError("snapshot and manifest realm_id values do not match")
    maximum_owner_level = _require_int(snapshot.get("maximum_owner_level"), "maximum_owner_level", 1, 80)
    global_phase_cap = _require_int(snapshot.get("global_phase_cap"), "global_phase_cap", 1, 80)
    if highest_owner_level_seen > maximum_owner_level:
        raise PopulationPolicyError("highest_owner_level_seen exceeds maximum_owner_level")
    if observed_owner_level is not None and observed_owner_level > maximum_owner_level:
        raise PopulationPolicyError("observed_owner_level exceeds maximum_owner_level")

    ambient_rows = bindings.get("bindings")
    identities_raw = manifest.get("identities")
    if not isinstance(ambient_rows, list) or not isinstance(identities_raw, list):
        raise PopulationPolicyError("manifest identities and database bindings must be lists")
    if binding_state == "bound":
        for row in ambient_rows:
            if isinstance(row, dict) and (
                row.get("account_id") == owner_account_id or row.get("character_guid") == owner_character_guid
            ):
                raise PopulationPolicyError("owner binding overlaps the autonomous RandomBot population")

    identities: dict[str, dict[str, object]] = {}
    identities_by_wave: dict[int, list[dict[str, object]]] = {}
    for ordinal, identity_raw in enumerate(identities_raw):
        if not isinstance(identity_raw, dict):
            raise PopulationPolicyError(f"manifest identities[{ordinal}] must be an object")
        identity = _require_token(identity_raw.get("manifest_identity"), f"identities[{ordinal}].manifest_identity")
        if identity in identities:
            raise PopulationPolicyError(f"duplicate manifest identity: {identity}")
        wave_level = _require_int(
            identity_raw.get("creation_wave_owner_level"),
            f"identity {identity}.creation_wave_owner_level",
            1,
            maximum_owner_level,
        )
        _require_int(identity_raw.get("wave_ordinal"), f"identity {identity}.wave_ordinal", 1)
        _require_bool(identity_raw.get("admission_eligible"), f"identity {identity}.admission_eligible")
        identities[identity] = identity_raw
        identities_by_wave.setdefault(wave_level, []).append(identity_raw)
    for wave_level, rows in identities_by_wave.items():
        rows.sort(key=lambda item: (cast(int, item["wave_ordinal"]), cast(str, item["manifest_identity"])))
        if [cast(int, item["wave_ordinal"]) for item in rows] != list(range(1, len(rows) + 1)):
            raise PopulationPolicyError(f"wave {wave_level} ordinals are not contiguous")

    bound_identities: set[str] = set()
    for ordinal, row in enumerate(ambient_rows):
        if not isinstance(row, dict):
            raise PopulationPolicyError(f"bindings[{ordinal}] must be an object")
        identity = _require_token(row.get("manifest_identity"), f"bindings[{ordinal}].manifest_identity")
        if identity not in identities or identity in bound_identities:
            raise PopulationPolicyError("ambient bindings must uniquely reference manifest identities")
        if not cast(bool, identities[identity]["admission_eligible"]):
            raise PopulationPolicyError(f"ineligible identity is already admitted: {identity}")
        bound_identities.add(identity)

    resulting_high_water = (
        1 if observed_owner_level is None else max(highest_owner_level_seen, observed_owner_level)
    )
    previous_epoch = min(highest_owner_level_seen, maximum_owner_level, global_phase_cap)
    resulting_epoch = min(resulting_high_water, maximum_owner_level, global_phase_cap)
    newly_authorized_levels = (
        [] if resulting_epoch <= previous_epoch else list(range(previous_epoch + 1, resulting_epoch + 1))
    )
    for identity in sorted(bound_identities):
        wave_level = cast(int, identities[identity]["creation_wave_owner_level"])
        if wave_level > resulting_epoch:
            raise PopulationPolicyError(f"binding for {identity} belongs to future wave {wave_level}")

    wave_states: list[dict[str, object]] = []
    pending_wave_levels: list[int] = []
    remaining_by_wave: dict[int, list[dict[str, object]]] = {}
    total_due = 0
    for wave_level in range(1, resulting_epoch + 1):
        rows = identities_by_wave.get(wave_level, [])
        remaining = [item for item in rows if cast(str, item["manifest_identity"]) not in bound_identities]
        ineligible = [
            cast(str, item["manifest_identity"])
            for item in remaining
            if not cast(bool, item["admission_eligible"])
        ]
        admitted_count = len(rows) - len(remaining)
        state = (
            "active"
            if not remaining
            else "blocked"
            if ineligible
            else "admitting"
            if admitted_count
            else "authorized"
        )
        if remaining:
            pending_wave_levels.append(wave_level)
            remaining_by_wave[wave_level] = remaining
            total_due += len(remaining)
        wave_states.append(
            {
                "owner_level": wave_level,
                "planned_count": len(rows),
                "admitted_count": admitted_count,
                "remaining_count": len(remaining),
                "ineligible_remaining": ineligible,
                "state": state,
                "idempotency_key": _wave_key(realm_id, wave_level, schedule_digest),
            }
        )

    earliest_wave = pending_wave_levels[0] if pending_wave_levels else None
    blocked_identities: list[str] = []
    candidate_rows: list[dict[str, object]] = []
    if earliest_wave is None:
        decision = "noop"
    else:
        earliest_remaining = remaining_by_wave[earliest_wave]
        blocked_identities = [
            cast(str, item["manifest_identity"])
            for item in earliest_remaining
            if not cast(bool, item["admission_eligible"])
        ]
        if blocked_identities:
            decision = "blocked_ineligible"
        elif admissions_paused:
            decision = "paused"
        elif available_creation_slots == 0:
            decision = "capacity_hold"
        else:
            decision = "admit_batch"
            capacity_limits = snapshot.get("capacity_limits")
            if not isinstance(capacity_limits, dict):
                raise PopulationPolicyError("snapshot.capacity_limits must be an object")
            creation_batch_size = _require_int(
                capacity_limits.get("creation_batch_size"), "creation_batch_size", 1
            )
            batch_limit = min(creation_batch_size, available_creation_slots)
            candidate_rows = [
                {
                    "manifest_identity": cast(str, item["manifest_identity"]),
                    "creation_wave_owner_level": cast(int, item["creation_wave_owner_level"]),
                    "wave_ordinal": cast(int, item["wave_ordinal"]),
                }
                for item in earliest_remaining[:batch_limit]
            ]

    capacity_limits = snapshot.get("capacity_limits")
    if not isinstance(capacity_limits, dict):
        raise PopulationPolicyError("snapshot.capacity_limits must be an object")
    payload: dict[str, object] = {
        "reconciliation_schema": reconciliation_schema,
        "reconciliation_version": reconciliation_version,
        "realm_id": realm_id,
        "source_digests": {
            "binding_digest": binding_digest,
            "manifest_digest": manifest_digest,
            "policy_digest": policy_digest,
            "schedule_digest": schedule_digest,
            "snapshot_digest": snapshot_digest,
        },
        "owner_state": {
            "binding_state": binding_state,
            "binding_generation": binding_generation,
            "owner_account_id": owner_account_id,
            "owner_character_guid": owner_character_guid,
            "observed_owner_level": observed_owner_level,
            "previous_highest_owner_level_seen": highest_owner_level_seen,
            "resulting_highest_owner_level_seen": resulting_high_water,
            "newly_authorized_levels": newly_authorized_levels,
            "current_epoch": resulting_epoch,
        },
        "admission_control": {
            "admissions_paused": admissions_paused,
            "pause_reason": pause_reason,
            "available_creation_slots": available_creation_slots,
            "creation_batch_size": _require_int(
                capacity_limits.get("creation_batch_size"), "creation_batch_size", 1
            ),
        },
        "binding_count": len(bound_identities),
        "total_due_count": total_due,
        "pending_wave_levels": pending_wave_levels,
        "earliest_pending_wave": earliest_wave,
        "wave_states": wave_states,
        "decision": decision,
        "blocked_identities": blocked_identities,
        "candidate_batch": candidate_rows,
        "runtime_commit_required": True,
    }
    return {**payload, "reconciliation_digest": digest(payload)}


def _quoted_sql_token(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise PopulationPolicyError("SQL installation strings must be stable ASCII tokens")
    return f"'{value}'"


def render_population_install_sql_v2(plan: Mapping[str, object]) -> bytes:
    """Render a fail-if-present v2 seed transaction for a fresh schema."""

    installation_digest = _verify_compiled_artifact(plan, "installation_digest", "installation plan")
    _require_exact_keys(
        plan,
        {
            "installation_schema",
            "installation_version",
            "installation_digest",
            "realm_id",
            "source_digests",
            "schedule",
            "owner_state",
            "waves",
            "manifest_identities",
        },
        "installation plan",
    )
    _require_int(plan.get("installation_schema"), "installation_schema", 2, 2)
    _require_token(plan.get("installation_version"), "installation_version")
    realm_id = _require_token(plan.get("realm_id"), "realm_id")
    source_digests = plan.get("source_digests")
    schedule = plan.get("schedule")
    owner_state = plan.get("owner_state")
    waves = plan.get("waves")
    identities = plan.get("manifest_identities")
    if not isinstance(source_digests, dict) or not isinstance(schedule, dict) or not isinstance(owner_state, dict):
        raise PopulationPolicyError("installation source_digests schedule and owner_state must be objects")
    if not isinstance(waves, list) or not isinstance(identities, list):
        raise PopulationPolicyError("installation waves and manifest_identities must be lists")
    _require_exact_keys(
        source_digests,
        {"schedule_digest", "policy_digest", "manifest_digest", "snapshot_digest"},
        "installation source_digests",
    )
    digests = {
        key: _require_digest(source_digests.get(key), f"source_digests.{key}")
        for key in ("schedule_digest", "policy_digest", "manifest_digest", "snapshot_digest")
    }
    _require_exact_keys(
        schedule,
        {"schedule_version", "maximum_owner_level", "permanent_roster_target", "global_phase_cap"},
        "installation schedule",
    )
    schedule_version = _require_token(schedule.get("schedule_version"), "schedule.schedule_version")
    maximum_owner_level = _require_int(schedule.get("maximum_owner_level"), "maximum_owner_level", 1, 80)
    permanent_roster_target = _require_int(schedule.get("permanent_roster_target"), "permanent_roster_target", 1)
    global_phase_cap = _require_int(schedule.get("global_phase_cap"), "global_phase_cap", 1, 80)
    _require_exact_keys(
        owner_state,
        {"binding_state", "highest_owner_level_seen", "current_epoch", "generation"},
        "installation owner_state",
    )
    if owner_state.get("binding_state") != "unbound":
        raise PopulationPolicyError("fresh installation owner state must be unbound")
    highest_owner_level_seen = _require_int(
        owner_state.get("highest_owner_level_seen"), "highest_owner_level_seen", 1, 1
    )
    current_epoch = _require_int(owner_state.get("current_epoch"), "current_epoch", 1, 1)
    generation = _require_int(owner_state.get("generation"), "generation", 1, 1)
    q = _quoted_sql_token

    statements = [
        "START TRANSACTION;",
        "INSERT INTO population_director_schedule ",
        "  (realm_id, schedule_version, schedule_digest, policy_digest, manifest_digest, ",
        "   snapshot_digest, installation_digest, maximum_owner_level, global_phase_cap, ",
        "   permanent_roster_target) VALUES",
        "  ("
        + ", ".join(
            (
                q(realm_id),
                q(schedule_version),
                q(digests["schedule_digest"]),
                q(digests["policy_digest"]),
                q(digests["manifest_digest"]),
                q(digests["snapshot_digest"]),
                q(installation_digest),
                str(maximum_owner_level),
                str(global_phase_cap),
                str(permanent_roster_target),
            )
        )
        + ");",
        "INSERT INTO population_director_owner_state ",
        "  (realm_id, binding_state, owner_account_id, owner_character_guid, ",
        "   highest_owner_level_seen, current_epoch, generation) VALUES",
        f"  ({q(realm_id)}, 'unbound', NULL, NULL, {highest_owner_level_seen}, {current_epoch}, {generation});",
    ]

    wave_values: list[str] = []
    for ordinal, raw_wave in enumerate(waves):
        if not isinstance(raw_wave, dict):
            raise PopulationPolicyError(f"waves[{ordinal}] must be an object")
        _require_exact_keys(
            raw_wave,
            {"owner_level", "wave_idempotency_key", "planned_count", "initial_state"},
            f"waves[{ordinal}]",
        )
        owner_level = _require_int(raw_wave.get("owner_level"), f"waves[{ordinal}].owner_level", 1, 80)
        if owner_level != ordinal + 1:
            raise PopulationPolicyError("installation waves must be contiguous")
        wave_key = _require_digest(raw_wave.get("wave_idempotency_key"), f"waves[{ordinal}].wave_idempotency_key")
        if wave_key != _wave_key(realm_id, owner_level, digests["schedule_digest"]):
            raise PopulationPolicyError("wave idempotency key is not owner independent")
        planned_count = _require_int(raw_wave.get("planned_count"), f"waves[{ordinal}].planned_count", 0)
        initial_state = _require_token(raw_wave.get("initial_state"), f"waves[{ordinal}].initial_state")
        expected_state = "authorized" if owner_level == 1 else "planned"
        if initial_state != expected_state:
            raise PopulationPolicyError("only wave 1 may be initially authorized")
        authorized_at = "CURRENT_TIMESTAMP(6)" if initial_state == "authorized" else "NULL"
        wave_values.append(
            f"({q(realm_id)}, {owner_level}, {q(wave_key)}, {planned_count}, 0, "
            f"{q(initial_state)}, {authorized_at}, 1)"
        )
    if len(wave_values) != maximum_owner_level:
        raise PopulationPolicyError("installation wave count does not equal maximum_owner_level")
    statements.extend(
        (
            "INSERT INTO population_director_wave_state ",
            "  (realm_id, owner_level, wave_idempotency_key, planned_count, admitted_count, ",
            "   state, authorized_at, generation) VALUES",
            "  " + ",\n  ".join(wave_values) + ";",
        )
    )

    identity_values: list[str] = []
    for ordinal, raw_identity in enumerate(identities):
        if not isinstance(raw_identity, dict):
            raise PopulationPolicyError(f"manifest_identities[{ordinal}] must be an object")
        identity = _require_token(raw_identity.get("manifest_identity"), f"identities[{ordinal}].manifest_identity")
        account_name = _require_token(raw_identity.get("account_name"), f"identity {identity}.account_name")
        character_name = _require_token(raw_identity.get("character_name"), f"identity {identity}.character_name")
        faction = _require_token(raw_identity.get("faction"), f"identity {identity}.faction")
        role_intent = _require_token(raw_identity.get("role_intent"), f"identity {identity}.role_intent")
        spec_template = _require_token(raw_identity.get("spec_template"), f"identity {identity}.spec_template")
        starting_region = _require_token(raw_identity.get("starting_region"), f"identity {identity}.starting_region")
        wave_key = _require_digest(raw_identity.get("wave_idempotency_key"), f"identity {identity}.wave_key")
        owner_level = _require_int(raw_identity.get("owner_level"), f"identity {identity}.owner_level", 1, 80)
        if wave_key != _wave_key(realm_id, owner_level, digests["schedule_digest"]):
            raise PopulationPolicyError("manifest identity has a mismatched wave key")
        wave_ordinal = _require_int(raw_identity.get("wave_ordinal"), f"identity {identity}.wave_ordinal", 1)
        account_size = _require_int(raw_identity.get("account_manifest_size"), "account_manifest_size", 1, 9)
        race = _require_int(raw_identity.get("race"), f"identity {identity}.race", 1, 11)
        class_id = _require_int(raw_identity.get("class"), f"identity {identity}.class", 1, 11)
        eligible = int(_require_bool(raw_identity.get("admission_eligible"), "admission_eligible"))
        appearance = raw_identity.get("appearance")
        if not isinstance(appearance, dict):
            raise PopulationPolicyError(f"identity {identity}.appearance must be an object")
        appearance_values = [
            _require_int(appearance.get(field), f"identity {identity}.appearance.{field}", 0, 255)
            for field in ("gender", "skin", "face", "hair_style", "hair_color", "facial_hair", "outfit_id")
        ]
        identity_values.append(
            "("
            + ", ".join(
                (
                    q(realm_id),
                    q(identity),
                    q(digests["manifest_digest"]),
                    q(wave_key),
                    str(owner_level),
                    str(wave_ordinal),
                    q(account_name),
                    str(account_size),
                    q(character_name),
                    q(faction),
                    str(race),
                    str(class_id),
                    q(role_intent),
                    q(spec_template),
                    q(starting_region),
                    str(eligible),
                    *(str(value) for value in appearance_values),
                )
            )
            + ")"
        )
    if len(identity_values) != permanent_roster_target:
        raise PopulationPolicyError("manifest identity count does not equal permanent_roster_target")
    columns = (
        "  (realm_id, manifest_identity, manifest_digest, wave_idempotency_key, owner_level, wave_ordinal, "
        "account_name, account_manifest_size, character_name, faction, race, class, role_intent, spec_template, "
        "starting_region, admission_eligible, gender, skin, face, hair_style, hair_color, facial_hair, outfit_id) "
        "VALUES"
    )
    for start in range(0, len(identity_values), 250):
        statements.extend(
            (
                "INSERT INTO population_director_manifest_identity ",
                columns,
                "  " + ",\n  ".join(identity_values[start : start + 250]) + ";",
            )
        )
    statements.extend(("COMMIT;", ""))
    return "\n".join(statements).encode("ascii")


def compile_admission_recovery_v2(
    raw: Mapping[str, object],
    reconciliation: Mapping[str, object],
    manifest: Mapping[str, object],
) -> dict[str, object]:
    """Use the proven forward-only recovery state machine with v2 digests."""

    if reconciliation.get("reconciliation_schema") != 2:
        raise PopulationPolicyError("recovery v2 requires reconciliation schema 2")
    _require_exact_keys(
        raw,
        {
            "recovery_schema",
            "recovery_version",
            "manifest_identity",
            "ledger",
            "observed_account",
            "observed_character",
            "observed_rndbot",
        },
        "admission recovery input",
    )
    _require_int(raw.get("recovery_schema"), "recovery_schema", 2, 2)
    compatibility_input = dict(raw)
    compatibility_input["recovery_schema"] = 1
    compiled = compile_admission_recovery(compatibility_input, reconciliation, manifest)
    compiled.pop("recovery_digest")
    compiled["recovery_schema"] = 2
    return {**compiled, "recovery_digest": digest(compiled)}
