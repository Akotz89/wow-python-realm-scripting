from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest

from realm_scripts.population_policy import (
    PopulationPolicyError,
    ProgressionRecord,
    canonical_json,
    compile_admission_reconciliation,
    compile_admission_recovery,
    compile_identity_bindings,
    compile_policy,
    compile_population_install_plan,
    compile_population_manifest,
    compile_runtime_snapshot,
    compile_wave_schedule,
    digest,
    levels_to_authorize,
    rank_and_assign,
    render_population_install_sql,
)
from realm_scripts.population_policy_v2 import (
    compile_admission_reconciliation_v2,
    compile_admission_recovery_v2,
    compile_owner_binding_transition_v2,
    compile_policy_v2,
    compile_population_install_plan_v2,
    compile_runtime_snapshot_v2,
    compile_wave_schedule_v2,
    render_population_install_sql_v2,
)

CPP_VERIFIER = Path(__file__).parents[1] / "cpp/runtime_snapshot_verifier.cpp"
CPP_ADMISSION_VERIFIER = Path(__file__).parents[1] / "cpp/admission_reconciliation_verifier.cpp"
CPP_RECOVERY_VERIFIER = Path(__file__).parents[1] / "cpp/admission_recovery_verifier.cpp"
CPP_V2_OWNER_INDEPENDENCE_VERIFIER = Path(__file__).parents[1] / "cpp/owner_independent_v2_verifier.cpp"
CPP_NATIVE_OWNER_EPOCH_REGRESSION = Path(__file__).parents[1] / "cpp/native_owner_epoch_regression.cpp"
SCHEMA_DIR = Path(__file__).parents[1] / "schemas"
V2_LEDGER_SQL = Path(__file__).parents[1] / "sql/playerbots_population_director_ledger_v2.sql"
OWNER_EPOCH_MIGRATION_NAME = "2026_10_11_00_population_director_owner_epoch_highwater.sql"
PACKAGE_OWNER_EPOCH_MIGRATION = (
    Path(__file__).parents[1] / "sql/pending_db_playerbots" / OWNER_EPOCH_MIGRATION_NAME
)
MODULE_SOURCE_ROOT = Path(
    os.environ.get(
        "WOW_POPULATION_SOURCE_ROOT",
        Path(os.environ.get("WOW_PROJECT_ROOT", Path(__file__).resolve().parents[3]))
        / "work/cpi225-integration-20260912/source/work/cpi194-owner-binding-20260918/source",
    )
)
MODULE_OWNER_EPOCH_MIGRATION = (
    MODULE_SOURCE_ROOT
    / "modules/mod-playerbots/data/sql/playerbots/updates/pending_db_playerbots"
    / OWNER_EPOCH_MIGRATION_NAME
)
MODULE_V2_LEDGER_UPDATE = (
    MODULE_SOURCE_ROOT
    / "modules/mod-playerbots/data/sql/playerbots/updates/2026_09_18_00_population_director_ledger_v2.sql"
)


def require_build_host() -> None:
    project_root = Path(os.environ.get("WOW_PROJECT_ROOT", Path(__file__).resolve().parents[3]))
    subprocess.run([str(project_root / "scripts/wow-build-host-guard")], check=True, capture_output=True, text=True)


def candidate_schedule(**overrides: Any) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "schedule_version": "candidate-test-v1",
        "realm_id": "fresh-playerbots-realm",
        "owner_anchor_guid": 1,
        "maximum_owner_level": 80,
        "permanent_roster_target": 10_000,
        "curve_kind": "normalized_exponential",
        "decay": "0.97",
    }
    value.update(overrides)
    return value


def candidate_policy(**overrides: Any) -> dict[str, object]:
    value: dict[str, object] = {
        "policy_schema": 1,
        "policy_version": "candidate-test-v1",
        "global_phase_cap": 19,
        "schedule": candidate_schedule(),
        "bands": [
            {"name": "pioneer", "share": "0.05", "ceiling": 19},
            {"name": "scout", "share": "0.10", "ceiling": 15},
            {"name": "forward", "share": "0.15", "ceiling": 10},
            {"name": "main", "share": "0.50", "ceiling": 5},
            {"name": "trailing", "share": "0.20", "ceiling": 2},
        ],
    }
    value.update(overrides)
    return value


def small_manifest_fixture() -> tuple[dict[str, object], dict[str, object]]:
    schedule = compile_wave_schedule(candidate_schedule(maximum_owner_level=4, permanent_roster_target=20, decay="1"))
    templates = [
        {"template_id": "t01-tank", "class": 1, "role_intent": "tank", "minimum_functional_level": 10},
        {"template_id": "t02-healer", "class": 2, "role_intent": "healer", "minimum_functional_level": 10},
        {"template_id": "t03-damage", "class": 3, "role_intent": "damage", "minimum_functional_level": 1},
        {"template_id": "t04-damage", "class": 4, "role_intent": "damage", "minimum_functional_level": 1},
        {"template_id": "t05-healer", "class": 5, "role_intent": "healer", "minimum_functional_level": 10},
        {"template_id": "t07-damage", "class": 7, "role_intent": "damage", "minimum_functional_level": 1},
        {"template_id": "t08-damage", "class": 8, "role_intent": "damage", "minimum_functional_level": 1},
        {"template_id": "t09-damage", "class": 9, "role_intent": "damage", "minimum_functional_level": 1},
        {"template_id": "t11-tank", "class": 11, "role_intent": "tank", "minimum_functional_level": 10},
    ]
    roster = [
        ("alliance", 1, 1, "tank", "human"),
        ("alliance", 1, 2, "healer", "human"),
        ("alliance", 3, 3, "damage", "dwarf_gnome"),
        ("alliance", 7, 4, "damage", "dwarf_gnome"),
        ("alliance", 1, 5, "healer", "human"),
        ("alliance", 11, 7, "damage", "draenei"),
        ("alliance", 7, 8, "damage", "dwarf_gnome"),
        ("alliance", 7, 9, "damage", "dwarf_gnome"),
        ("alliance", 4, 11, "tank", "night_elf"),
        ("alliance", 4, 3, "damage", "night_elf"),
        ("horde", 2, 1, "tank", "orc_troll"),
        ("horde", 10, 2, "healer", "blood_elf"),
        ("horde", 2, 3, "damage", "orc_troll"),
        ("horde", 8, 4, "damage", "orc_troll"),
        ("horde", 5, 5, "healer", "undead"),
        ("horde", 6, 7, "damage", "tauren"),
        ("horde", 8, 8, "damage", "orc_troll"),
        ("horde", 5, 9, "damage", "undead"),
        ("horde", 6, 11, "tank", "tauren"),
        ("horde", 10, 3, "damage", "blood_elf"),
    ]
    names = [
        "Aldra",
        "Belric",
        "Corin",
        "Dessa",
        "Elric",
        "Fara",
        "Galen",
        "Helia",
        "Ivara",
        "Joren",
        "Korin",
        "Letha",
        "Marek",
        "Nalia",
        "Orin",
        "Pella",
        "Quorin",
        "Ressa",
        "Soren",
        "Talia",
    ]
    identities: list[dict[str, object]] = []
    for index, ((faction, race, class_id, role, region), name) in enumerate(zip(roster, names, strict=True)):
        identities.append(
            {
                "manifest_identity": f"ambient-{index + 1:04d}",
                "account_name": f"RNDBOT{index + 1:04d}",
                "character_name": name,
                "faction": faction,
                "race": race,
                "class": class_id,
                "role_intent": role,
                "spec_template": f"t{class_id:02d}-{role}",
                "starting_region": region,
                "creation_wave_owner_level": index // 5 + 1,
                "wave_ordinal": index % 5 + 1,
                "admission_eligible": True,
                "appearance": {
                    "gender": index % 2,
                    "skin": 0,
                    "face": 0,
                    "hair_style": 0,
                    "hair_color": 0,
                    "facial_hair": 0,
                    "outfit_id": 0,
                },
            }
        )
    manifest: dict[str, object] = {
        "manifest_schema": 1,
        "manifest_version": "small-test-v1",
        "realm_id": schedule["realm_id"],
        "schedule_digest": schedule["schedule_digest"],
        "composition": {
            "faction_shares": {"alliance": "0.5", "horde": "0.5"},
            "role_shares": {"tank": "0.2", "healer": "0.2", "damage": "0.6"},
            "required_class_floor_per_faction": 1,
            "required_region_floor": 1,
        },
        "spec_templates": templates,
        "identities": identities,
    }
    return schedule, manifest


def runtime_snapshot_fixture() -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    _, raw_manifest = small_manifest_fixture()
    policy = compile_policy(
        candidate_policy(
            global_phase_cap=4,
            bands=[
                {"name": "pioneer", "share": "0.05", "ceiling": 4},
                {"name": "scout", "share": "0.10", "ceiling": 3},
                {"name": "forward", "share": "0.15", "ceiling": 2},
                {"name": "main", "share": "0.50", "ceiling": 2},
                {"name": "trailing", "share": "0.20", "ceiling": 1},
            ],
            schedule=candidate_schedule(
                maximum_owner_level=4,
                permanent_roster_target=20,
                decay="1",
            ),
        )
    )
    schedule = cast(dict[str, object], policy["schedule"])
    manifest = compile_population_manifest(raw_manifest, schedule)
    bindings = compile_identity_bindings(
        [
            {"manifest_identity": "ambient-0001", "account_id": 101, "character_guid": 1001},
            {"manifest_identity": "ambient-0002", "account_id": 102, "character_guid": 1002},
        ],
        manifest,
    )
    runtime_input: dict[str, object] = {
        "snapshot_schema": 1,
        "snapshot_version": "candidate-runtime-v1",
        "capacity_limits": {
            "server_session_budget": 100,
            "safety_reserve": 10,
            "concurrent_active_target": 50,
            "creation_batch_size": 5,
            "login_batch_size": 5,
            "max_active_per_map": 40,
            "max_active_per_zone": 20,
            "max_active_per_starting_region": 10,
        },
        "held_activity_whitelist": ["crafting", "gathering", "social"],
    }
    return runtime_input, policy, manifest, bindings


def reconciliation_input(**overrides: Any) -> dict[str, object]:
    value: dict[str, object] = {
        "reconciliation_schema": 1,
        "reconciliation_version": "candidate-reconciliation-v1",
        "observed_owner_level": 1,
        "highest_owner_level_seen": 1,
        "admissions_paused": False,
        "pause_reason": None,
        "available_creation_slots": 10,
    }
    value.update(overrides)
    return value


def recovery_input(**overrides: Any) -> dict[str, object]:
    value: dict[str, object] = {
        "recovery_schema": 1,
        "recovery_version": "candidate-recovery-v1",
        "manifest_identity": "ambient-0003",
        "ledger": None,
        "observed_account": None,
        "observed_character": None,
        "observed_rndbot": None,
    }
    value.update(overrides)
    return value


def recovery_fixture(
    available_creation_slots: int = 3,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    runtime_input, policy, manifest, bindings = runtime_snapshot_fixture()
    snapshot = snapshot_for_bindings(runtime_input, policy, manifest, bindings)
    plan = compile_admission_reconciliation(
        reconciliation_input(available_creation_slots=available_creation_slots),
        snapshot,
        manifest,
        bindings,
    )
    identity = next(
        item
        for item in cast(list[dict[str, object]], manifest["identities"])
        if item["manifest_identity"] == "ambient-0003"
    )
    return plan, manifest, identity


def recovery_ledger(
    plan: dict[str, object],
    manifest: dict[str, object],
    state: str,
    generation: int,
    *,
    account_id: int | None = None,
    character_guid: int | None = None,
) -> dict[str, object]:
    wave = cast(list[dict[str, object]], plan["wave_states"])[0]
    identity = next(
        item
        for item in cast(list[dict[str, object]], manifest["identities"])
        if item["manifest_identity"] == "ambient-0003"
    )
    return {
        "state": state,
        "generation": generation,
        "manifest_digest": manifest["manifest_digest"],
        "wave_idempotency_key": wave["idempotency_key"],
        "expected_identity": {
            "account_name": identity["account_name"],
            "account_manifest_size": sum(
                1
                for item in cast(list[dict[str, object]], manifest["identities"])
                if item["account_name"] == identity["account_name"]
            ),
            "character_name": identity["character_name"],
            "race": identity["race"],
            "class": identity["class"],
            "appearance": identity["appearance"],
            "creation_wave_owner_level": identity["creation_wave_owner_level"],
            "wave_ordinal": identity["wave_ordinal"],
            "wave_idempotency_key": wave["idempotency_key"],
        },
        "account_id": account_id,
        "character_guid": character_guid,
    }


def recovery_observed_account(
    identity: dict[str, object],
    *,
    account_id: int = 103,
    row_present: bool = True,
    numchars: int | None = 0,
    character_database_numchars: int | None = None,
) -> dict[str, object]:
    if character_database_numchars is None:
        character_database_numchars = numchars if numchars is not None else 0
    return {
        "account_id": account_id,
        "account_name": identity["account_name"],
        "character_database_numchars": character_database_numchars,
        "realmcharacters_row_present": row_present,
        "realmcharacters_numchars": numchars,
    }


def recovery_observed_character(
    identity: dict[str, object],
    *,
    account_id: int = 103,
    character_guid: int = 1003,
) -> dict[str, object]:
    appearance = cast(dict[str, int], identity["appearance"])
    return {
        "character_guid": character_guid,
        "account_id": account_id,
        "character_name": identity["character_name"],
        "race": identity["race"],
        "class": identity["class"],
        "appearance": {key: value for key, value in appearance.items() if key != "outfit_id"},
        "level": 1,
        "xp": 0,
        "total_time": 0,
        "level_time": 0,
        "quest_state": {
            "status_count": 0,
            "rewarded_count": 0,
            "daily_count": 0,
            "weekly_count": 0,
            "monthly_count": 0,
            "seasonal_count": 0,
        },
    }


def snapshot_for_bindings(
    runtime_input: dict[str, object],
    policy: dict[str, object],
    manifest: dict[str, object],
    bindings: dict[str, object],
) -> dict[str, object]:
    return compile_runtime_snapshot(runtime_input, policy, manifest, bindings)


def v2_fixture(
    maximum_owner_level: int = 4,
    global_phase_cap: int = 4,
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    _, raw_manifest = small_manifest_fixture()
    raw_schedule: dict[str, object] = {
        "schema_version": 2,
        "schedule_version": "candidate-test-v2",
        "realm_id": "fresh-playerbots-realm",
        "maximum_owner_level": maximum_owner_level,
        "permanent_roster_target": 20,
        "curve_kind": "normalized_exponential",
        "decay": "1",
    }
    policy = compile_policy_v2(
        {
            "policy_schema": 2,
            "policy_version": "candidate-test-v2",
            "global_phase_cap": global_phase_cap,
            "schedule": raw_schedule,
            "bands": [
                {"name": "pioneer", "share": "0.05", "ceiling": 4},
                {"name": "scout", "share": "0.10", "ceiling": 3},
                {"name": "forward", "share": "0.15", "ceiling": 2},
                {"name": "main", "share": "0.50", "ceiling": 2},
                {"name": "trailing", "share": "0.20", "ceiling": 1},
            ],
        }
    )
    schedule = cast(dict[str, object], policy["schedule"])
    scheduled_waves = cast(list[dict[str, int]], schedule["waves"])
    identities = cast(list[dict[str, object]], raw_manifest["identities"])
    identity_index = 0
    for wave in scheduled_waves:
        for wave_ordinal in range(1, wave["count"] + 1):
            identities[identity_index]["creation_wave_owner_level"] = wave["owner_level"]
            identities[identity_index]["wave_ordinal"] = wave_ordinal
            identity_index += 1
    assert identity_index == len(identities)
    raw_manifest["schedule_digest"] = schedule["schedule_digest"]
    manifest = compile_population_manifest(raw_manifest, schedule)
    bindings = compile_identity_bindings([], manifest)
    runtime_input: dict[str, object] = {
        "snapshot_schema": 2,
        "snapshot_version": "candidate-runtime-v2",
        "capacity_limits": {
            "server_session_budget": 100,
            "safety_reserve": 10,
            "concurrent_active_target": 50,
            "creation_batch_size": 5,
            "login_batch_size": 5,
            "max_active_per_map": 40,
            "max_active_per_zone": 20,
            "max_active_per_starting_region": 10,
        },
        "held_activity_whitelist": ["crafting", "gathering", "social"],
    }
    snapshot = compile_runtime_snapshot_v2(runtime_input, policy, manifest, bindings)
    installation = compile_population_install_plan_v2(
        {"installation_schema": 2, "installation_version": "candidate-install-v2"},
        snapshot,
        policy,
        manifest,
    )
    return policy, manifest, bindings, snapshot, installation


def _recursive_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for child in value.values() for key in _recursive_keys(child)}
    if isinstance(value, list):
        return {key for child in value for key in _recursive_keys(child)}
    return set()


def test_candidate_schedule_matches_normative_worked_example() -> None:
    compiled = compile_wave_schedule(candidate_schedule())
    wave_rows = cast(list[dict[str, int]], compiled["waves"])
    waves = {wave["owner_level"]: wave["count"] for wave in wave_rows}
    assert {level: waves[level] for level in (1, 2, 3, 5, 10, 20, 30, 40, 50, 60, 70, 80)} == {
        1: 329,
        2: 319,
        3: 309,
        5: 291,
        10: 250,
        20: 184,
        30: 136,
        40: 100,
        50: 74,
        60: 55,
        70: 40,
        80: 30,
    }
    counts = list(waves.values())
    assert sum(counts) == 10_000
    assert all(left >= right for left, right in zip(counts, counts[1:], strict=False))


@pytest.mark.parametrize("target", [1, 2, 7, 79, 80, 81, 600, 10_000, 10_003])
@pytest.mark.parametrize("decay", ["1", "0.999", "0.97", "0.5", "0.01"])
def test_wave_apportionment_is_exact_and_monotone(target: int, decay: str) -> None:
    compiled = compile_wave_schedule(candidate_schedule(permanent_roster_target=target, decay=decay))
    wave_rows = cast(list[dict[str, int]], compiled["waves"])
    counts = [wave["count"] for wave in wave_rows]
    assert sum(counts) == target
    assert all(count >= 0 for count in counts)
    assert all(left >= right for left, right in zip(counts, counts[1:], strict=False))


def test_float_input_and_invalid_decay_fail_closed() -> None:
    with pytest.raises(PopulationPolicyError, match="never a float"):
        compile_wave_schedule(candidate_schedule(decay=0.97))
    with pytest.raises(PopulationPolicyError, match="0 < decay <= 1"):
        compile_wave_schedule(candidate_schedule(decay="1.01"))


def test_missing_and_unknown_fields_fail_closed() -> None:
    missing = candidate_schedule()
    del missing["realm_id"]
    with pytest.raises(PopulationPolicyError, match="missing fields: realm_id"):
        compile_wave_schedule(missing)

    with pytest.raises(PopulationPolicyError, match="unknown fields: typo"):
        compile_wave_schedule(candidate_schedule(typo="ignored-is-unsafe"))


def test_full_policy_is_byte_stable_and_epoch_capacities_are_exact() -> None:
    first = compile_policy(candidate_policy())
    second = compile_policy(candidate_policy())
    assert first == second
    assert first["policy_digest"] == second["policy_digest"]
    epochs = first["band_capacity_epochs"]
    assert isinstance(epochs, list)
    first_epoch = epochs[0]
    assert sum(first_epoch["capacities"].values()) == first_epoch["scheduled_cumulative_population"]
    final_epoch = epochs[-1]
    assert sum(final_epoch["capacities"].values()) == 10_000


def test_band_shares_and_ceiling_order_fail_closed() -> None:
    wrong_sum = candidate_policy(
        bands=[
            {"name": "pioneer", "share": "0.4", "ceiling": 19},
            {"name": "main", "share": "0.5", "ceiling": 5},
        ]
    )
    with pytest.raises(PopulationPolicyError, match="sum exactly"):
        compile_policy(wrong_sum)

    wrong_order = candidate_policy(
        bands=[
            {"name": "pioneer", "share": "0.5", "ceiling": 10},
            {"name": "main", "share": "0.5", "ceiling": 11},
        ]
    )
    with pytest.raises(PopulationPolicyError, match="non-increasing"):
        compile_policy(wrong_order)


def test_owner_level_authorization_is_ordered_idempotent_and_phase_bounded() -> None:
    assert levels_to_authorize(1, 5, 80, 19) == (2, 3, 4, 5)
    assert levels_to_authorize(5, 5, 80, 19) == ()
    assert levels_to_authorize(5, 4, 80, 19) == ()
    assert levels_to_authorize(18, 25, 80, 19) == (19,)


def test_earned_progression_rank_changes_band_without_permanent_assignment() -> None:
    capacities = (("pioneer", 1), ("scout", 1), ("main", 2))
    original = (
        ProgressionRecord(101, 8, 10, 200),
        ProgressionRecord(102, 8, 10, 100),
        ProgressionRecord(103, 7, 500, 90),
        ProgressionRecord(104, 6, 900, 80),
    )
    assert rank_and_assign(original, capacities) == (
        (102, 1, "pioneer"),
        (101, 2, "scout"),
        (103, 3, "main"),
        (104, 4, "main"),
    )

    passed = (*original[:2], ProgressionRecord(103, 9, 0, 300), original[3])
    assert rank_and_assign(passed, capacities)[0] == (103, 1, "pioneer")
    assert rank_and_assign(passed, capacities)[2] == (101, 3, "main")


def test_duplicate_guid_and_under_capacity_fail_closed() -> None:
    duplicate = (ProgressionRecord(1, 1, 0, 1), ProgressionRecord(1, 1, 0, 1))
    with pytest.raises(PopulationPolicyError, match="duplicate character GUID"):
        rank_and_assign(duplicate, (("main", 2),))
    with pytest.raises(PopulationPolicyError, match="smaller"):
        rank_and_assign((ProgressionRecord(1, 1, 0, 1),), (("main", 0),))


def test_population_manifest_validates_identity_composition_and_wave_totals() -> None:
    schedule, raw = small_manifest_fixture()
    compiled = compile_population_manifest(raw, schedule)
    assert compiled["manifest_digest"] == compile_population_manifest(raw, schedule)["manifest_digest"]
    reports = cast(dict[str, dict[str, int]], compiled["reports"])
    assert reports["faction"] == {"alliance": 10, "horde": 10}
    assert reports["role_intent"] == {"damage": 12, "healer": 4, "tank": 4}
    assert reports["creation_wave"] == {"1": 5, "2": 5, "3": 5, "4": 5}


def test_population_manifest_rejects_illegal_pair_template_mismatch_and_duplicate_name() -> None:
    schedule, raw = small_manifest_fixture()
    illegal = deepcopy(raw)
    cast(list[dict[str, object]], illegal["identities"])[10]["race"] = 10
    with pytest.raises(PopulationPolicyError, match="illegal or excluded"):
        compile_population_manifest(illegal, schedule)

    mismatch = deepcopy(raw)
    cast(list[dict[str, object]], mismatch["identities"])[0]["spec_template"] = "t03-damage"
    with pytest.raises(PopulationPolicyError, match="does not match"):
        compile_population_manifest(mismatch, schedule)

    duplicate = deepcopy(raw)
    identities = cast(list[dict[str, object]], duplicate["identities"])
    identities[1]["character_name"] = identities[0]["character_name"]
    with pytest.raises(PopulationPolicyError, match="duplicate character name"):
        compile_population_manifest(duplicate, schedule)


def test_population_manifest_rejects_wrong_wave_count_and_composition() -> None:
    schedule, raw = small_manifest_fixture()
    wrong_wave = deepcopy(raw)
    moved = cast(list[dict[str, object]], wrong_wave["identities"])[0]
    moved["creation_wave_owner_level"] = 2
    moved["wave_ordinal"] = 6
    with pytest.raises(PopulationPolicyError, match="wave 1"):
        compile_population_manifest(wrong_wave, schedule)

    wrong_role = deepcopy(raw)
    identity = cast(list[dict[str, object]], wrong_role["identities"])[0]
    identity["role_intent"] = "damage"
    identity["spec_template"] = "t01-tank"
    with pytest.raises(PopulationPolicyError, match="does not match"):
        compile_population_manifest(wrong_role, schedule)


def test_population_manifest_rejects_tampered_schedule_content() -> None:
    schedule, raw = small_manifest_fixture()
    tampered = deepcopy(schedule)
    tampered["permanent_roster_target"] = 21
    with pytest.raises(PopulationPolicyError, match="schedule_digest does not match artifact content"):
        compile_population_manifest(raw, tampered)


def test_population_manifest_rejects_nonzero_outfit_ignored_by_pinned_core() -> None:
    schedule, raw = small_manifest_fixture()
    identities = cast(list[dict[str, object]], raw["identities"])
    appearance = cast(dict[str, int], identities[0]["appearance"])
    appearance["outfit_id"] = 1

    with pytest.raises(PopulationPolicyError, match="appearance.outfit_id must be 0"):
        compile_population_manifest(raw, schedule)


def test_database_bindings_preserve_unique_characters_and_shared_manifest_accounts() -> None:
    schedule, raw = small_manifest_fixture()
    manifest = compile_population_manifest(raw, schedule)
    bindings = [
        {"manifest_identity": "ambient-0001", "account_id": 101, "character_guid": 1001},
        {"manifest_identity": "ambient-0002", "account_id": 102, "character_guid": 1002},
    ]
    compiled = compile_identity_bindings(bindings, manifest)
    assert compiled["binding_digest"] == compile_identity_bindings(bindings, manifest)["binding_digest"]

    mismatched_account = deepcopy(bindings)
    mismatched_account[1]["account_id"] = 101
    with pytest.raises(PopulationPolicyError, match="one account ID maps"):
        compile_identity_bindings(mismatched_account, manifest)

    shared_raw = deepcopy(raw)
    shared_identities = cast(list[dict[str, object]], shared_raw["identities"])
    shared_identities[1]["account_name"] = shared_identities[0]["account_name"]
    shared_manifest = compile_population_manifest(shared_raw, schedule)
    shared_bindings = deepcopy(bindings)
    shared_bindings[1]["account_id"] = 101
    compiled_shared = compile_identity_bindings(shared_bindings, shared_manifest)
    assert cast(list[dict[str, object]], compiled_shared["bindings"])[1]["account_id"] == 101
    report = cast(dict[str, object], shared_manifest["reports"])
    account_report = cast(dict[str, object], report["account"])
    assert account_report["account_count"] == 19
    assert cast(dict[str, int], account_report["characters_per_account"])["RNDBOT0001"] == 2

    duplicate_class = deepcopy(shared_raw)
    duplicate_class_identities = cast(list[dict[str, object]], duplicate_class["identities"])
    duplicate_class_identities[1]["class"] = duplicate_class_identities[0]["class"]
    duplicate_class_identities[1]["role_intent"] = duplicate_class_identities[0]["role_intent"]
    duplicate_class_identities[1]["spec_template"] = duplicate_class_identities[0]["spec_template"]
    with pytest.raises(PopulationPolicyError, match="more than one character for class"):
        compile_population_manifest(duplicate_class, schedule)

    unsorted = list(reversed(bindings))
    with pytest.raises(PopulationPolicyError, match="sorted"):
        compile_identity_bindings(unsorted, manifest)

    tampered_manifest = deepcopy(manifest)
    tampered_manifest["manifest_version"] = "tampered"
    with pytest.raises(PopulationPolicyError, match="manifest_digest does not match artifact content"):
        compile_identity_bindings(bindings, tampered_manifest)


def test_runtime_snapshot_binds_all_digests_capacity_and_hold_policy() -> None:
    runtime_input, policy, manifest, bindings = runtime_snapshot_fixture()
    first = compile_runtime_snapshot(runtime_input, policy, manifest, bindings)
    second = compile_runtime_snapshot(runtime_input, policy, manifest, bindings)
    assert first == second
    assert first["snapshot_digest"] == second["snapshot_digest"]
    assert first["effective_ceiling_rule"] == "min(global_phase_cap,dynamic_band_ceiling)"
    assert first["manifest_identity_count"] == 20
    assert first["binding_count"] == 2
    hold = cast(dict[str, object], first["progression_hold"])
    assert hold["bank_xp"] is False
    assert hold["delevel_on_demotion"] is False
    assert hold["unknown_activity"] == "deny"


def test_population_install_plan_is_complete_hash_bound_and_initializes_only_wave_one() -> None:
    runtime_input, policy, manifest, _ = runtime_snapshot_fixture()
    empty_bindings = compile_identity_bindings([], manifest)
    snapshot = snapshot_for_bindings(runtime_input, policy, manifest, empty_bindings)
    raw = {"installation_schema": 1, "installation_version": "candidate-install-v1"}
    first = compile_population_install_plan(raw, snapshot, policy, manifest)
    second = compile_population_install_plan(raw, snapshot, policy, manifest)

    assert canonical_json(first) == canonical_json(second)
    assert first["installation_digest"] == second["installation_digest"]
    assert first["realm_id"] == "fresh-playerbots-realm"
    assert cast(dict[str, object], first["owner_state"]) == {
        "highest_owner_level_seen": 1,
        "current_epoch": 1,
        "generation": 1,
    }
    waves = cast(list[dict[str, object]], first["waves"])
    assert len(waves) == 4
    assert waves[0]["initial_state"] == "authorized"
    assert all(item["initial_state"] == "planned" for item in waves[1:])
    assert sum(cast(int, item["planned_count"]) for item in waves) == 20
    identities = cast(list[dict[str, object]], first["manifest_identities"])
    assert len(identities) == 20
    assert identities[0]["manifest_identity"] == "ambient-0001"
    assert identities[0]["wave_idempotency_key"] == waves[0]["wave_idempotency_key"]


def test_population_install_plan_preserves_shared_account_size_and_rejects_existing_bindings() -> None:
    runtime_input, policy, _, _ = runtime_snapshot_fixture()
    schedule, raw_manifest = small_manifest_fixture()
    identities = cast(list[dict[str, object]], raw_manifest["identities"])
    identities[1]["account_name"] = identities[0]["account_name"]
    manifest = compile_population_manifest(raw_manifest, schedule)
    empty_bindings = compile_identity_bindings([], manifest)
    snapshot = snapshot_for_bindings(runtime_input, policy, manifest, empty_bindings)
    raw = {"installation_schema": 1, "installation_version": "candidate-install-v1"}
    installation = compile_population_install_plan(raw, snapshot, policy, manifest)
    installed_identities = cast(list[dict[str, object]], installation["manifest_identities"])
    assert installed_identities[0]["account_manifest_size"] == 2
    assert installed_identities[1]["account_manifest_size"] == 2

    nonempty_bindings = compile_identity_bindings(
        [{"manifest_identity": "ambient-0001", "account_id": 101, "character_guid": 1001}],
        manifest,
    )
    bound_snapshot = snapshot_for_bindings(runtime_input, policy, manifest, nonempty_bindings)
    with pytest.raises(PopulationPolicyError, match="empty database-binding"):
        compile_population_install_plan(raw, bound_snapshot, policy, manifest)


def test_population_install_plan_rejects_tampered_manifest_and_snapshot_sources() -> None:
    runtime_input, policy, manifest, _ = runtime_snapshot_fixture()
    empty_bindings = compile_identity_bindings([], manifest)
    snapshot = snapshot_for_bindings(runtime_input, policy, manifest, empty_bindings)
    raw = {"installation_schema": 1, "installation_version": "candidate-install-v1"}

    tampered_manifest = deepcopy(manifest)
    cast(list[dict[str, object]], tampered_manifest["identities"])[0]["character_name"] = "Changed"
    with pytest.raises(PopulationPolicyError, match="manifest_digest does not match"):
        compile_population_install_plan(raw, snapshot, policy, tampered_manifest)

    tampered_snapshot = deepcopy(snapshot)
    cast(dict[str, object], tampered_snapshot["source_digests"])["manifest_digest"] = "0" * 64
    with pytest.raises(PopulationPolicyError, match="snapshot_digest does not match"):
        compile_population_install_plan(raw, tampered_snapshot, policy, manifest)


def test_population_install_sql_is_deterministic_transactional_and_fail_if_present() -> None:
    runtime_input, policy, manifest, _ = runtime_snapshot_fixture()
    empty_bindings = compile_identity_bindings([], manifest)
    snapshot = snapshot_for_bindings(runtime_input, policy, manifest, empty_bindings)
    plan = compile_population_install_plan(
        {"installation_schema": 1, "installation_version": "candidate-install-v1"},
        snapshot,
        policy,
        manifest,
    )
    first = render_population_install_sql(plan)
    second = render_population_install_sql(plan)
    assert first == second
    assert first.startswith(b"START TRANSACTION;\nINSERT INTO population_director_schedule")
    assert first.endswith(b"COMMIT;\n")
    assert first.count(b"INSERT INTO population_director_schedule") == 1
    assert first.count(b"INSERT INTO population_director_owner_state") == 1
    assert first.count(b"INSERT INTO population_director_wave_state") == 1
    assert first.count(b"INSERT INTO population_director_manifest_identity") == 1
    assert b"ON DUPLICATE KEY" not in first
    assert b"REPLACE INTO" not in first

    tampered = deepcopy(plan)
    cast(dict[str, object], tampered["schedule"])["global_phase_cap"] = 20
    with pytest.raises(PopulationPolicyError, match="installation_digest does not match"):
        render_population_install_sql(tampered)


@pytest.mark.parametrize("artifact_name", ["policy", "manifest", "bindings"])
def test_runtime_snapshot_rejects_tampered_compiled_artifact(artifact_name: str) -> None:
    runtime_input, policy, manifest, bindings = runtime_snapshot_fixture()
    artifacts = {"policy": policy, "manifest": manifest, "bindings": bindings}
    tampered = deepcopy(artifacts[artifact_name])
    tampered["tampered"] = True
    artifacts[artifact_name] = tampered
    with pytest.raises(PopulationPolicyError, match="does not match artifact content"):
        compile_runtime_snapshot(
            runtime_input,
            artifacts["policy"],
            artifacts["manifest"],
            artifacts["bindings"],
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("safety_reserve", 4, "five-character party"),
        ("concurrent_active_target", 95, "exceeds server_session_budget"),
        ("creation_batch_size", 51, "must not exceed"),
        ("max_active_per_starting_region", 21, "must be non-decreasing"),
    ],
)
def test_runtime_snapshot_capacity_relations_fail_closed(field: str, value: int, message: str) -> None:
    runtime_input, policy, manifest, bindings = runtime_snapshot_fixture()
    capacities = cast(dict[str, object], runtime_input["capacity_limits"])
    capacities[field] = value
    with pytest.raises(PopulationPolicyError, match=message):
        compile_runtime_snapshot(runtime_input, policy, manifest, bindings)


def test_runtime_snapshot_held_activity_whitelist_is_explicit_sorted_and_closed() -> None:
    runtime_input, policy, manifest, bindings = runtime_snapshot_fixture()
    runtime_input["held_activity_whitelist"] = ["social", "gathering"]
    with pytest.raises(PopulationPolicyError, match="sorted"):
        compile_runtime_snapshot(runtime_input, policy, manifest, bindings)

    runtime_input["held_activity_whitelist"] = ["questing"]
    with pytest.raises(PopulationPolicyError, match="unknown activities: questing"):
        compile_runtime_snapshot(runtime_input, policy, manifest, bindings)


def test_cpp_runtime_verifier_accepts_python_snapshot_and_rejects_mutation(tmp_path: Path) -> None:
    compiler = shutil.which("g++")
    if compiler is None or not Path("/usr/include/boost/json.hpp").is_file():
        pytest.skip("C++ cross-language verifier dependencies are unavailable")
    require_build_host()
    binary = tmp_path / "runtime_snapshot_verifier"
    build = subprocess.run(
        [
            compiler,
            "-std=c++20",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(CPP_VERIFIER),
            "-lboost_json",
            "-lcrypto",
            "-o",
            str(binary),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if build.returncode != 0 and "cannot find -lboost_json" in build.stderr:
        pytest.skip("Boost.JSON development library is unavailable")
    assert build.returncode == 0, build.stderr

    runtime_input, policy, manifest, bindings = runtime_snapshot_fixture()
    snapshot = compile_runtime_snapshot(runtime_input, policy, manifest, bindings)
    valid_path = tmp_path / "valid.json"
    valid_path.write_bytes(canonical_json(snapshot) + b"\n")
    accepted = subprocess.run([binary, valid_path], check=False, capture_output=True, text=True)
    assert accepted.returncode == 0, accepted.stderr
    assert "PASS" in accepted.stdout

    mutated = deepcopy(snapshot)
    capacity = cast(dict[str, object], mutated["capacity_limits"])
    capacity["concurrent_active_target"] = 51
    mutated_path = tmp_path / "mutated.json"
    mutated_path.write_bytes(canonical_json(mutated) + b"\n")
    rejected = subprocess.run([binary, mutated_path], check=False, capture_output=True, text=True)
    assert rejected.returncode == 1
    assert "snapshot_digest does not match" in rejected.stderr

    semantically_invalid = deepcopy(snapshot)
    invalid_capacity = cast(dict[str, object], semantically_invalid["capacity_limits"])
    invalid_capacity["concurrent_active_target"] = 95
    del semantically_invalid["snapshot_digest"]
    semantically_invalid["snapshot_digest"] = digest(semantically_invalid)
    invalid_path = tmp_path / "valid-digest-invalid-capacity.json"
    invalid_path.write_bytes(canonical_json(semantically_invalid) + b"\n")
    invalid = subprocess.run([binary, invalid_path], check=False, capture_output=True, text=True)
    assert invalid.returncode == 1
    assert "active target plus safety reserve exceeds session budget" in invalid.stderr


def test_cpp_admission_verifier_accepts_python_plan_and_rejects_invalid_state(tmp_path: Path) -> None:
    compiler = shutil.which("g++")
    if compiler is None or not Path("/usr/include/boost/json.hpp").is_file():
        pytest.skip("C++ cross-language verifier dependencies are unavailable")
    require_build_host()
    binary = tmp_path / "admission_reconciliation_verifier"
    build = subprocess.run(
        [
            compiler,
            "-std=c++20",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(CPP_ADMISSION_VERIFIER),
            "-lboost_json",
            "-lcrypto",
            "-o",
            str(binary),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if build.returncode != 0 and "cannot find -lboost_json" in build.stderr:
        pytest.skip("Boost.JSON development library is unavailable")
    assert build.returncode == 0, build.stderr

    runtime_input, policy, manifest, bindings = runtime_snapshot_fixture()
    snapshot = snapshot_for_bindings(runtime_input, policy, manifest, bindings)
    plan = compile_admission_reconciliation(
        reconciliation_input(observed_owner_level=3, highest_owner_level_seen=1, available_creation_slots=3),
        snapshot,
        manifest,
        bindings,
    )
    valid_path = tmp_path / "valid-admission-plan.json"
    valid_path.write_bytes(canonical_json(plan) + b"\n")
    accepted = subprocess.run([binary, valid_path], check=False, capture_output=True, text=True)
    assert accepted.returncode == 0, accepted.stderr
    assert "PASS" in accepted.stdout

    mutated = deepcopy(plan)
    mutated["total_due_count"] = 12
    mutated_path = tmp_path / "mutated-admission-plan.json"
    mutated_path.write_bytes(canonical_json(mutated) + b"\n")
    rejected = subprocess.run([binary, mutated_path], check=False, capture_output=True, text=True)
    assert rejected.returncode == 1
    assert "reconciliation_digest does not match" in rejected.stderr

    semantically_invalid = deepcopy(plan)
    semantically_invalid["decision"] = "capacity_hold"
    del semantically_invalid["reconciliation_digest"]
    semantically_invalid["reconciliation_digest"] = digest(semantically_invalid)
    invalid_path = tmp_path / "valid-digest-invalid-admission-plan.json"
    invalid_path.write_bytes(canonical_json(semantically_invalid) + b"\n")
    invalid = subprocess.run([binary, invalid_path], check=False, capture_output=True, text=True)
    assert invalid.returncode == 1
    assert "decision does not match derived admission state" in invalid.stderr


def test_cpp_recovery_verifier_accepts_python_plan_and_rejects_invalid_action(tmp_path: Path) -> None:
    compiler = shutil.which("g++")
    if compiler is None or not Path("/usr/include/boost/json.hpp").is_file():
        pytest.skip("C++ cross-language verifier dependencies are unavailable")
    require_build_host()
    binary = tmp_path / "admission_recovery_verifier"
    build = subprocess.run(
        [
            compiler,
            "-std=c++20",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(CPP_RECOVERY_VERIFIER),
            "-lboost_json",
            "-lcrypto",
            "-o",
            str(binary),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if build.returncode != 0 and "cannot find -lboost_json" in build.stderr:
        pytest.skip("Boost.JSON development library is unavailable")
    assert build.returncode == 0, build.stderr

    reconciliation, manifest, identity = recovery_fixture()
    ledger = recovery_ledger(reconciliation, manifest, "account_pending", 2)
    recovery = compile_admission_recovery(
        recovery_input(ledger=ledger),
        reconciliation,
        manifest,
    )
    valid_path = tmp_path / "valid-recovery-plan.json"
    valid_path.write_bytes(canonical_json(recovery) + b"\n")
    accepted = subprocess.run([binary, valid_path], check=False, capture_output=True, text=True)
    assert accepted.returncode == 0, accepted.stderr
    assert "PASS" in accepted.stdout

    exact_character = compile_admission_recovery(
        recovery_input(
            ledger=recovery_ledger(reconciliation, manifest, "character_pending", 4, account_id=103),
            observed_account=recovery_observed_account(identity, numchars=1),
            observed_character=recovery_observed_character(identity),
        ),
        reconciliation,
        manifest,
    )
    exact_character_path = tmp_path / "valid-exact-character-plan.json"
    exact_character_path.write_bytes(canonical_json(exact_character) + b"\n")
    exact_character_accepted = subprocess.run(
        [binary, exact_character_path],
        check=False,
        capture_output=True,
        text=True,
    )
    assert exact_character_accepted.returncode == 0, exact_character_accepted.stderr

    mutated = deepcopy(recovery)
    mutated["runtime_commit_required"] = False
    mutated_path = tmp_path / "mutated-recovery-plan.json"
    mutated_path.write_bytes(canonical_json(mutated) + b"\n")
    rejected = subprocess.run([binary, mutated_path], check=False, capture_output=True, text=True)
    assert rejected.returncode == 1
    assert "recovery_digest does not match" in rejected.stderr

    semantically_invalid = deepcopy(recovery)
    invalid_decision = cast(dict[str, object], semantically_invalid["decision"])
    invalid_decision.update(
        {
            "action": "noop_active",
            "to_state": "active",
            "effect_domain": "none",
            "read_back_required": False,
        }
    )
    semantically_invalid["runtime_commit_required"] = False
    del semantically_invalid["recovery_digest"]
    semantically_invalid["recovery_digest"] = digest(semantically_invalid)
    invalid_path = tmp_path / "valid-digest-invalid-recovery-plan.json"
    invalid_path.write_bytes(canonical_json(semantically_invalid) + b"\n")
    invalid = subprocess.run([binary, invalid_path], check=False, capture_output=True, text=True)
    assert invalid.returncode == 1
    assert "decision does not match derived recovery state" in invalid.stderr

    dirty_character = deepcopy(exact_character)
    external = cast(dict[str, object], dirty_character["external_observation"])
    character = cast(dict[str, object], external["character"])
    quest_state = cast(dict[str, int], character["quest_state"])
    quest_state["rewarded_count"] = 1
    del dirty_character["recovery_digest"]
    dirty_character["recovery_digest"] = digest(dirty_character)
    dirty_path = tmp_path / "valid-digest-dirty-character-plan.json"
    dirty_path.write_bytes(canonical_json(dirty_character) + b"\n")
    dirty_rejected = subprocess.run([binary, dirty_path], check=False, capture_output=True, text=True)
    assert dirty_rejected.returncode == 1
    assert "decision does not match derived recovery state" in dirty_rejected.stderr


def test_admission_reconciliation_is_byte_stable_and_uses_only_earliest_wave() -> None:
    runtime_input, policy, manifest, bindings = runtime_snapshot_fixture()
    snapshot = snapshot_for_bindings(runtime_input, policy, manifest, bindings)
    request = reconciliation_input(observed_owner_level=3, highest_owner_level_seen=1)

    first = compile_admission_reconciliation(request, snapshot, manifest, bindings)
    second = compile_admission_reconciliation(request, snapshot, manifest, bindings)

    assert canonical_json(first) == canonical_json(second)
    assert first["reconciliation_digest"] == second["reconciliation_digest"]
    assert first["decision"] == "admit_batch"
    assert first["pending_wave_levels"] == [1, 2, 3]
    assert first["earliest_pending_wave"] == 1
    assert first["total_due_count"] == 13
    owner_state = cast(dict[str, object], first["owner_state"])
    assert owner_state["newly_authorized_levels"] == [2, 3]
    assert owner_state["current_epoch"] == 3
    assert owner_state["resulting_highest_owner_level_seen"] == 3
    assert first["candidate_batch"] == [
        {"manifest_identity": "ambient-0003", "creation_wave_owner_level": 1, "wave_ordinal": 3},
        {"manifest_identity": "ambient-0004", "creation_wave_owner_level": 1, "wave_ordinal": 4},
        {"manifest_identity": "ambient-0005", "creation_wave_owner_level": 1, "wave_ordinal": 5},
    ]


def test_admission_reconciliation_advances_to_next_wave_after_durable_bindings() -> None:
    runtime_input, policy, manifest, _ = runtime_snapshot_fixture()
    bindings = compile_identity_bindings(
        [
            {
                "manifest_identity": f"ambient-{ordinal:04d}",
                "account_id": 100 + ordinal,
                "character_guid": 1000 + ordinal,
            }
            for ordinal in range(1, 6)
        ],
        manifest,
    )
    snapshot = snapshot_for_bindings(runtime_input, policy, manifest, bindings)
    plan = compile_admission_reconciliation(
        reconciliation_input(observed_owner_level=3, highest_owner_level_seen=3),
        snapshot,
        manifest,
        bindings,
    )

    assert plan["pending_wave_levels"] == [2, 3]
    assert plan["earliest_pending_wave"] == 2
    assert plan["candidate_batch"] == [
        {
            "manifest_identity": f"ambient-{ordinal:04d}",
            "creation_wave_owner_level": 2,
            "wave_ordinal": ordinal - 5,
        }
        for ordinal in range(6, 11)
    ]


@pytest.mark.parametrize(
    ("overrides", "decision"),
    [
        ({"admissions_paused": True, "pause_reason": "operator_hold"}, "paused"),
        ({"available_creation_slots": 0}, "capacity_hold"),
    ],
)
def test_admission_reconciliation_holds_without_changing_due_work(
    overrides: dict[str, object],
    decision: str,
) -> None:
    runtime_input, policy, manifest, bindings = runtime_snapshot_fixture()
    snapshot = snapshot_for_bindings(runtime_input, policy, manifest, bindings)
    plan = compile_admission_reconciliation(
        reconciliation_input(**overrides),
        snapshot,
        manifest,
        bindings,
    )

    assert plan["decision"] == decision
    assert plan["total_due_count"] == 3
    assert plan["candidate_batch"] == []
    assert plan["binding_count"] == 2


def test_admission_reconciliation_does_not_reauthorize_after_owner_delevel() -> None:
    runtime_input, policy, manifest, bindings = runtime_snapshot_fixture()
    snapshot = snapshot_for_bindings(runtime_input, policy, manifest, bindings)
    plan = compile_admission_reconciliation(
        reconciliation_input(observed_owner_level=1, highest_owner_level_seen=3),
        snapshot,
        manifest,
        bindings,
    )

    owner_state = cast(dict[str, object], plan["owner_state"])
    assert owner_state["newly_authorized_levels"] == []
    assert owner_state["resulting_highest_owner_level_seen"] == 3
    assert plan["pending_wave_levels"] == [1, 2, 3]


def test_admission_reconciliation_rejects_future_or_ineligible_existing_binding() -> None:
    runtime_input, policy, manifest, _ = runtime_snapshot_fixture()
    future_bindings = compile_identity_bindings(
        [{"manifest_identity": "ambient-0006", "account_id": 106, "character_guid": 1006}],
        manifest,
    )
    future_snapshot = snapshot_for_bindings(runtime_input, policy, manifest, future_bindings)
    with pytest.raises(PopulationPolicyError, match="future wave"):
        compile_admission_reconciliation(
            reconciliation_input(),
            future_snapshot,
            manifest,
            future_bindings,
        )

    _, raw_manifest = small_manifest_fixture()
    raw_identities = cast(list[dict[str, object]], raw_manifest["identities"])
    raw_identities[0]["admission_eligible"] = False
    schedule = cast(dict[str, object], policy["schedule"])
    ineligible_manifest = compile_population_manifest(raw_manifest, schedule)
    ineligible_bindings = compile_identity_bindings(
        [{"manifest_identity": "ambient-0001", "account_id": 101, "character_guid": 1001}],
        ineligible_manifest,
    )
    ineligible_snapshot = snapshot_for_bindings(
        runtime_input,
        policy,
        ineligible_manifest,
        ineligible_bindings,
    )
    with pytest.raises(PopulationPolicyError, match="ineligible identity is already admitted"):
        compile_admission_reconciliation(
            reconciliation_input(),
            ineligible_snapshot,
            ineligible_manifest,
            ineligible_bindings,
        )


def test_admission_reconciliation_blocks_uninstantiated_ineligible_identity() -> None:
    runtime_input, policy, _, _ = runtime_snapshot_fixture()
    schedule, raw_manifest = small_manifest_fixture()
    raw_identities = cast(list[dict[str, object]], raw_manifest["identities"])
    raw_identities[2]["admission_eligible"] = False
    manifest = compile_population_manifest(raw_manifest, schedule)
    bindings = compile_identity_bindings(
        [
            {"manifest_identity": "ambient-0001", "account_id": 101, "character_guid": 1001},
            {"manifest_identity": "ambient-0002", "account_id": 102, "character_guid": 1002},
        ],
        manifest,
    )
    snapshot = snapshot_for_bindings(runtime_input, policy, manifest, bindings)
    plan = compile_admission_reconciliation(
        reconciliation_input(),
        snapshot,
        manifest,
        bindings,
    )

    assert plan["decision"] == "blocked_ineligible"
    assert plan["blocked_identities"] == ["ambient-0003"]
    assert plan["candidate_batch"] == []


def test_admission_reconciliation_rejects_phase_violation_and_pause_mismatch() -> None:
    runtime_input, policy, manifest, bindings = runtime_snapshot_fixture()
    snapshot = snapshot_for_bindings(runtime_input, policy, manifest, bindings)

    phase_limited_policy = compile_policy(
        candidate_policy(
            global_phase_cap=3,
            bands=[
                {"name": "pioneer", "share": "0.05", "ceiling": 3},
                {"name": "scout", "share": "0.10", "ceiling": 3},
                {"name": "forward", "share": "0.15", "ceiling": 2},
                {"name": "main", "share": "0.50", "ceiling": 2},
                {"name": "trailing", "share": "0.20", "ceiling": 1},
            ],
            schedule=candidate_schedule(
                maximum_owner_level=4,
                permanent_roster_target=20,
                decay="1",
            ),
        )
    )
    phase_limited_snapshot = snapshot_for_bindings(
        runtime_input,
        phase_limited_policy,
        manifest,
        bindings,
    )
    with pytest.raises(PopulationPolicyError, match="exceeds the global phase cap"):
        compile_admission_reconciliation(
            reconciliation_input(observed_owner_level=4),
            phase_limited_snapshot,
            manifest,
            bindings,
        )
    with pytest.raises(PopulationPolicyError, match="must be null"):
        compile_admission_reconciliation(
            reconciliation_input(pause_reason="stale_reason"),
            snapshot,
            manifest,
            bindings,
        )


def test_admission_recovery_starts_with_one_durable_reservation() -> None:
    plan, manifest, _ = recovery_fixture()
    first = compile_admission_recovery(recovery_input(), plan, manifest)
    second = compile_admission_recovery(recovery_input(), plan, manifest)

    assert canonical_json(first) == canonical_json(second)
    decision = cast(dict[str, object], first["decision"])
    assert decision == {
        "action": "reserve_identity",
        "from_state": "absent",
        "to_state": "reserved",
        "effect_domain": "director_ledger",
        "reason": "legal_transition",
        "expected_generation": 0,
        "resulting_generation": 1,
        "read_back_required": False,
    }
    assert first["runtime_commit_required"] is True


def test_admission_recovery_external_steps_hold_generation_and_require_readback() -> None:
    plan, manifest, identity = recovery_fixture()
    account_pending = recovery_ledger(plan, manifest, "account_pending", 2)
    account_plan = compile_admission_recovery(
        recovery_input(ledger=account_pending),
        plan,
        manifest,
    )
    account_decision = cast(dict[str, object], account_plan["decision"])
    assert account_decision["action"] == "create_account"
    assert account_decision["effect_domain"] == "login_database"
    assert account_decision["expected_generation"] == 2
    assert account_decision["resulting_generation"] == 2
    assert account_decision["read_back_required"] is True

    character_pending = recovery_ledger(plan, manifest, "character_pending", 4, account_id=103)
    character_plan = compile_admission_recovery(
        recovery_input(
            ledger=character_pending,
            observed_account=recovery_observed_account(identity),
        ),
        plan,
        manifest,
    )
    character_decision = cast(dict[str, object], character_plan["decision"])
    assert character_decision["action"] == "create_character"
    assert character_decision["effect_domain"] == "character_database"
    assert character_decision["resulting_generation"] == 4
    assert character_decision["read_back_required"] is True


def test_admission_recovery_repairs_realmcharacters_at_both_async_boundaries() -> None:
    plan, manifest, identity = recovery_fixture()
    account_pending = recovery_ledger(plan, manifest, "account_pending", 2)
    initialize = compile_admission_recovery(
        recovery_input(
            ledger=account_pending,
            observed_account=recovery_observed_account(identity, row_present=False, numchars=None),
        ),
        plan,
        manifest,
    )
    initialize_decision = cast(dict[str, object], initialize["decision"])
    assert initialize_decision == {
        "action": "initialize_realmcharacters",
        "from_state": "account_pending",
        "to_state": "account_pending",
        "effect_domain": "login_database",
        "reason": "current_realm_row_absent",
        "expected_generation": 2,
        "resulting_generation": 2,
        "read_back_required": True,
    }

    character_pending = recovery_ledger(plan, manifest, "character_pending", 4, account_id=103)
    refresh = compile_admission_recovery(
        recovery_input(
            ledger=character_pending,
            observed_account=recovery_observed_account(
                identity,
                numchars=0,
                character_database_numchars=1,
            ),
            observed_character=recovery_observed_character(identity),
        ),
        plan,
        manifest,
    )
    refresh_decision = cast(dict[str, object], refresh["decision"])
    assert refresh_decision == {
        "action": "refresh_realmcharacters_count",
        "from_state": "character_pending",
        "to_state": "character_pending",
        "effect_domain": "login_database",
        "reason": "current_realm_count_stale",
        "expected_generation": 4,
        "resulting_generation": 4,
        "read_back_required": True,
    }


def test_admission_recovery_reuses_shared_account_and_rndbot_then_repairs_exact_count() -> None:
    runtime_input, policy, _, _ = runtime_snapshot_fixture()
    schedule, raw_manifest = small_manifest_fixture()
    raw_identities = cast(list[dict[str, object]], raw_manifest["identities"])
    raw_identities[2]["account_name"] = raw_identities[1]["account_name"]
    manifest = compile_population_manifest(raw_manifest, schedule)
    bindings = compile_identity_bindings(
        [
            {"manifest_identity": "ambient-0001", "account_id": 101, "character_guid": 1001},
            {"manifest_identity": "ambient-0002", "account_id": 102, "character_guid": 1002},
        ],
        manifest,
    )
    snapshot = snapshot_for_bindings(runtime_input, policy, manifest, bindings)
    plan = compile_admission_reconciliation(
        reconciliation_input(available_creation_slots=3),
        snapshot,
        manifest,
        bindings,
    )
    identity = next(
        item
        for item in cast(list[dict[str, object]], manifest["identities"])
        if item["manifest_identity"] == "ambient-0003"
    )

    shared_account = recovery_observed_account(
        identity,
        numchars=1,
        character_database_numchars=1,
        account_id=102,
    )
    shared_rndbot = {"account_id": 102, "account_type": 1}
    reserve = compile_admission_recovery(
        recovery_input(observed_account=shared_account, observed_rndbot=shared_rndbot),
        plan,
        manifest,
    )
    assert cast(dict[str, object], reserve["decision"])["action"] == "reserve_identity"

    account_pending = recovery_ledger(plan, manifest, "account_pending", 2)
    verify_account = compile_admission_recovery(
        recovery_input(
            ledger=account_pending,
            observed_account=shared_account,
            observed_rndbot=shared_rndbot,
        ),
        plan,
        manifest,
    )
    assert cast(dict[str, object], verify_account["decision"])["action"] == "mark_account_verified"

    character_pending = recovery_ledger(plan, manifest, "character_pending", 4, account_id=102)
    character = recovery_observed_character(identity, account_id=102)
    stale_after_create = recovery_observed_account(
        identity,
        numchars=1,
        character_database_numchars=2,
        account_id=102,
    )
    repair = compile_admission_recovery(
        recovery_input(
            ledger=character_pending,
            observed_account=stale_after_create,
            observed_character=character,
            observed_rndbot=shared_rndbot,
        ),
        plan,
        manifest,
    )
    repair_decision = cast(dict[str, object], repair["decision"])
    assert repair_decision["action"] == "refresh_realmcharacters_count"
    assert repair_decision["resulting_generation"] == 4


def test_admission_recovery_blocks_account_character_count_above_manifest_size() -> None:
    plan, manifest, identity = recovery_fixture()
    account_pending = recovery_ledger(plan, manifest, "account_pending", 2)
    overflow = compile_admission_recovery(
        recovery_input(
            ledger=account_pending,
            observed_account=recovery_observed_account(
                identity,
                numchars=2,
                character_database_numchars=2,
            ),
        ),
        plan,
        manifest,
    )
    decision = cast(dict[str, object], overflow["decision"])
    assert decision["action"] == "block_conflict"
    assert decision["reason"] == "account_character_count_exceeds_manifest"


def test_admission_recovery_requires_exact_appearance_clean_quests_and_ledger_identity() -> None:
    plan, manifest, identity = recovery_fixture()
    character_pending = recovery_ledger(plan, manifest, "character_pending", 4, account_id=103)
    dirty_character = recovery_observed_character(identity)
    dirty_quest_state = cast(dict[str, int], dirty_character["quest_state"])
    dirty_quest_state["status_count"] = 1
    dirty = compile_admission_recovery(
        recovery_input(
            ledger=character_pending,
            observed_account=recovery_observed_account(identity, numchars=1),
            observed_character=dirty_character,
        ),
        plan,
        manifest,
    )
    dirty_decision = cast(dict[str, object], dirty["decision"])
    assert dirty_decision["action"] == "block_conflict"
    assert dirty_decision["reason"] == "character_identity_conflict"

    appearance_character = recovery_observed_character(identity)
    appearance = cast(dict[str, int], appearance_character["appearance"])
    appearance["hair_style"] = 7
    mismatch = compile_admission_recovery(
        recovery_input(
            ledger=character_pending,
            observed_account=recovery_observed_account(identity, numchars=1),
            observed_character=appearance_character,
        ),
        plan,
        manifest,
    )
    mismatch_decision = cast(dict[str, object], mismatch["decision"])
    assert mismatch_decision["action"] == "block_conflict"
    assert mismatch_decision["reason"] == "character_identity_conflict"

    changed_ledger = recovery_ledger(plan, manifest, "account_pending", 2)
    expected_identity = cast(dict[str, object], changed_ledger["expected_identity"])
    expected_identity["race"] = 2
    with pytest.raises(PopulationPolicyError, match="ledger expected identity conflicts"):
        compile_admission_recovery(
            recovery_input(ledger=changed_ledger),
            plan,
            manifest,
        )


def test_admission_recovery_adopts_exact_commits_only_after_readback() -> None:
    plan, manifest, identity = recovery_fixture()
    account_before_character = recovery_observed_account(identity)
    account = recovery_observed_account(identity, numchars=1)
    character = recovery_observed_character(identity)
    rndbot = {"account_id": 103, "account_type": 1}

    cases = [
        ("account_pending", 2, account_before_character, None, None, "mark_account_verified", "account_verified", 3),
        ("character_pending", 4, account, character, None, "mark_character_verified", "character_verified", 5),
        ("rndbot_pending", 6, account, character, rndbot, "mark_binding_verified", "binding_verified", 7),
        ("binding_verified", 7, account, character, rndbot, "activate_identity", "active", 8),
        ("active", 8, account, character, rndbot, "noop_active", "active", 8),
    ]
    for state, generation, observed_account, observed_character, observed_rndbot, action, to_state, result in cases:
        ledger = recovery_ledger(
            plan,
            manifest,
            state,
            generation,
            account_id=103 if state not in {"account_pending"} else None,
            character_guid=1003
            if state in {"character_verified", "rndbot_pending", "binding_verified", "active"}
            else None,
        )
        recovery = compile_admission_recovery(
            recovery_input(
                ledger=ledger,
                observed_account=observed_account,
                observed_character=observed_character,
                observed_rndbot=observed_rndbot,
            ),
            plan,
            manifest,
        )
        decision = cast(dict[str, object], recovery["decision"])
        assert (decision["action"], decision["to_state"], decision["resulting_generation"]) == (
            action,
            to_state,
            result,
        )


def test_admission_recovery_conflicts_fail_closed_without_skipping_identity() -> None:
    plan, manifest, identity = recovery_fixture()
    conflict = compile_admission_recovery(
        recovery_input(
            observed_character=recovery_observed_character(identity, account_id=999),
        ),
        plan,
        manifest,
    )
    decision = cast(dict[str, object], conflict["decision"])
    assert decision["action"] == "block_conflict"
    assert decision["to_state"] == "blocked_conflict"
    assert decision["reason"] == "character_account_conflict"
    assert decision["effect_domain"] == "director_ledger"

    verified = recovery_ledger(plan, manifest, "account_verified", 3, account_id=103)
    missing = compile_admission_recovery(recovery_input(ledger=verified), plan, manifest)
    missing_decision = cast(dict[str, object], missing["decision"])
    assert missing_decision["action"] == "block_conflict"
    assert missing_decision["reason"] == "ledger_account_conflict"


def test_admission_recovery_rejects_non_candidate_and_digest_mismatch() -> None:
    plan, manifest, _ = recovery_fixture(available_creation_slots=1)
    with pytest.raises(PopulationPolicyError, match="not in the reconciliation candidate batch"):
        compile_admission_recovery(
            recovery_input(manifest_identity="ambient-0004"),
            plan,
            manifest,
        )

    mutated = deepcopy(plan)
    mutated["total_due_count"] = 999
    with pytest.raises(PopulationPolicyError, match="reconciliation_digest does not match"):
        compile_admission_recovery(recovery_input(), mutated, manifest)


def test_v2_immutable_artifacts_are_owner_identity_independent() -> None:
    policy, _, _, snapshot, installation = v2_fixture()
    forbidden = {"owner_anchor_guid", "owner_account_id", "owner_character_guid"}
    assert forbidden.isdisjoint(_recursive_keys(policy))
    assert forbidden.isdisjoint(_recursive_keys(snapshot))
    assert forbidden.isdisjoint(_recursive_keys(installation))
    assert cast(dict[str, object], installation["owner_state"])["binding_state"] == "unbound"

    schedule = cast(dict[str, object], policy["schedule"])
    expected_first_key = digest(
        {
            "realm_id": schedule["realm_id"],
            "owner_level": 1,
            "schedule_digest": schedule["schedule_digest"],
        }
    )
    waves = cast(list[dict[str, object]], installation["waves"])
    assert waves[0]["wave_idempotency_key"] == expected_first_key
    assert waves[0]["initial_state"] == "authorized"
    assert all(wave["initial_state"] == "planned" for wave in waves[1:])


def test_v2_schedule_rejects_every_owner_identity_field() -> None:
    raw: dict[str, object] = {
        "schema_version": 2,
        "schedule_version": "candidate-v2",
        "realm_id": "fresh-playerbots-realm",
        "maximum_owner_level": 80,
        "permanent_roster_target": 10_000,
        "curve_kind": "normalized_exponential",
        "decay": "0.97",
    }
    compiled = compile_wave_schedule_v2(raw)
    assert sum(wave["count"] for wave in cast(list[dict[str, int]], compiled["waves"])) == 10_000
    for field, value in (
        ("owner_anchor_guid", 1),
        ("owner_account_id", 1),
        ("owner_character_guid", 1),
        ("owner_character_name", "NeverAllowed"),
    ):
        invalid = {**raw, field: value}
        with pytest.raises(PopulationPolicyError, match=f"unknown fields: {field}"):
            compile_wave_schedule_v2(invalid)


def test_v2_unbound_reconciliation_admits_only_wave_one() -> None:
    _, manifest, bindings, snapshot, _ = v2_fixture()
    raw: dict[str, object] = {
        "reconciliation_schema": 2,
        "reconciliation_version": "candidate-reconciliation-v2",
        "owner_binding_state": "unbound",
        "owner_binding_generation": 1,
        "owner_account_id": None,
        "owner_character_guid": None,
        "observed_owner_level": None,
        "highest_owner_level_seen": 1,
        "admissions_paused": False,
        "pause_reason": None,
        "available_creation_slots": 10,
    }
    plan = compile_admission_reconciliation_v2(raw, snapshot, manifest, bindings)
    owner_state = cast(dict[str, object], plan["owner_state"])
    assert owner_state["newly_authorized_levels"] == []
    assert plan["pending_wave_levels"] == [1]
    assert all(
        item["creation_wave_owner_level"] == 1
        for item in cast(list[dict[str, object]], plan["candidate_batch"])
    )

    invalid = {**raw, "observed_owner_level": 2}
    with pytest.raises(PopulationPolicyError, match="must be null"):
        compile_admission_reconciliation_v2(invalid, snapshot, manifest, bindings)
    invalid = {**raw, "highest_owner_level_seen": 2}
    with pytest.raises(PopulationPolicyError, match="only wave 1"):
        compile_admission_reconciliation_v2(invalid, snapshot, manifest, bindings)

    recovery = compile_admission_recovery_v2(
        recovery_input(recovery_schema=2, recovery_version="candidate-recovery-v2", manifest_identity="ambient-0001"),
        plan,
        manifest,
    )
    assert recovery["recovery_schema"] == 2
    assert recovery["recovery_digest"] == digest(
        {key: value for key, value in recovery.items() if key != "recovery_digest"}
    )


def test_v2_binding_and_later_level_authorization_are_separate() -> None:
    _, manifest, empty_bindings, snapshot, installation = v2_fixture()
    binding_input: dict[str, object] = {
        "binding_schema": 2,
        "binding_version": "owner-binding-v2",
        "realm_id": "fresh-playerbots-realm",
        "expected_generation": 1,
        "current_owner_state": {
            "binding_state": "unbound",
            "owner_account_id": None,
            "owner_character_guid": None,
            "highest_owner_level_seen": 1,
            "current_epoch": 1,
            "generation": 1,
        },
        "selection": {
            "account_id": 5001,
            "character_guid": 9001,
            "actor": "owner-console",
            "selected_at_utc": "2026-09-02T20:00:00Z",
            "resolution_evidence_digest": "1" * 64,
        },
    }
    transition = compile_owner_binding_transition_v2(binding_input, installation, empty_bindings)
    assert transition["operation"] == "bind"
    assert transition["authorized_wave_levels"] == []
    assert transition["next_generation"] == 2

    reconciliation = compile_admission_reconciliation_v2(
        {
            "reconciliation_schema": 2,
            "reconciliation_version": "candidate-reconciliation-v2",
            "owner_binding_state": "bound",
            "owner_binding_generation": 2,
            "owner_account_id": 5001,
            "owner_character_guid": 9001,
            "observed_owner_level": 3,
            "highest_owner_level_seen": 1,
            "admissions_paused": False,
            "pause_reason": None,
            "available_creation_slots": 10,
        },
        snapshot,
        manifest,
        empty_bindings,
    )
    owner_state = cast(dict[str, object], reconciliation["owner_state"])
    assert owner_state["newly_authorized_levels"] == [2, 3]


def test_v2_binding_rejects_stale_generation_noop_and_ambient_overlap() -> None:
    _, manifest, empty_bindings, _, installation = v2_fixture()
    raw: dict[str, object] = {
        "binding_schema": 2,
        "binding_version": "owner-binding-v2",
        "realm_id": "fresh-playerbots-realm",
        "expected_generation": 2,
        "current_owner_state": {
            "binding_state": "bound",
            "owner_account_id": 5001,
            "owner_character_guid": 9001,
            "highest_owner_level_seen": 1,
            "current_epoch": 1,
            "generation": 2,
        },
        "selection": {
            "account_id": 5002,
            "character_guid": 9002,
            "actor": "owner-console",
            "selected_at_utc": "2026-09-02T20:01:00Z",
            "resolution_evidence_digest": "2" * 64,
        },
    }
    stale = deepcopy(raw)
    stale["expected_generation"] = 1
    with pytest.raises(PopulationPolicyError, match="does not match"):
        compile_owner_binding_transition_v2(stale, installation, empty_bindings)

    noop = deepcopy(raw)
    cast(dict[str, object], noop["selection"])["account_id"] = 5001
    cast(dict[str, object], noop["selection"])["character_guid"] = 9001
    with pytest.raises(PopulationPolicyError, match="must not be a no-op"):
        compile_owner_binding_transition_v2(noop, installation, empty_bindings)

    ambient_bindings = compile_identity_bindings(
        [{"manifest_identity": "ambient-0001", "account_id": 5002, "character_guid": 9002}],
        manifest,
    )
    with pytest.raises(PopulationPolicyError, match="overlaps"):
        compile_owner_binding_transition_v2(raw, installation, ambient_bindings)

    partial = deepcopy(raw)
    cast(dict[str, object], partial["current_owner_state"])["owner_character_guid"] = None
    with pytest.raises(PopulationPolicyError, match="requires both identity IDs"):
        compile_owner_binding_transition_v2(partial, installation, empty_bindings)

    impossible_high_water = deepcopy(raw)
    cast(dict[str, object], impossible_high_water["current_owner_state"])["highest_owner_level_seen"] = 5
    cast(dict[str, object], impossible_high_water["current_owner_state"])["current_epoch"] = 4
    with pytest.raises(PopulationPolicyError, match="exceeds the installed maximum"):
        compile_owner_binding_transition_v2(impossible_high_water, installation, empty_bindings)


@pytest.mark.parametrize("owner_level", [20, 80])
def test_v2_owner_binding_preserves_earned_high_water_above_phase_cap(owner_level: int) -> None:
    _, _, empty_bindings, _, installation = v2_fixture(maximum_owner_level=80, global_phase_cap=19)
    raw: dict[str, object] = {
        "binding_schema": 2,
        "binding_version": "owner-binding-v2",
        "realm_id": "fresh-playerbots-realm",
        "expected_generation": 2,
        "current_owner_state": {
            "binding_state": "bound",
            "owner_account_id": 5001,
            "owner_character_guid": 9001,
            "highest_owner_level_seen": owner_level,
            "current_epoch": 19,
            "generation": 2,
        },
        "selection": {
            "account_id": 5002,
            "character_guid": 9002,
            "actor": "owner-console",
            "selected_at_utc": "2026-09-02T20:01:00Z",
            "resolution_evidence_digest": "2" * 64,
        },
    }

    transition = compile_owner_binding_transition_v2(raw, installation, empty_bindings)
    previous = cast(dict[str, object], transition["previous_owner_state"])
    resulting = cast(dict[str, object], transition["resulting_owner_state"])
    assert previous["highest_owner_level_seen"] == owner_level
    assert previous["current_epoch"] == 19
    assert resulting["highest_owner_level_seen"] == owner_level
    assert resulting["current_epoch"] == 19
    assert transition["authorized_wave_levels"] == []


def test_v2_owner_binding_rejects_high_water_above_declared_owner_maximum() -> None:
    _, _, empty_bindings, _, installation = v2_fixture(maximum_owner_level=80, global_phase_cap=19)
    raw: dict[str, object] = {
        "binding_schema": 2,
        "binding_version": "owner-binding-v2",
        "realm_id": "fresh-playerbots-realm",
        "expected_generation": 2,
        "current_owner_state": {
            "binding_state": "bound",
            "owner_account_id": 5001,
            "owner_character_guid": 9001,
            "highest_owner_level_seen": 81,
            "current_epoch": 19,
            "generation": 2,
        },
        "selection": {
            "account_id": 5002,
            "character_guid": 9002,
            "actor": "owner-console",
            "selected_at_utc": "2026-09-02T20:01:00Z",
            "resolution_evidence_digest": "2" * 64,
        },
    }
    with pytest.raises(PopulationPolicyError, match="highest_owner_level_seen must be 1..80"):
        compile_owner_binding_transition_v2(raw, installation, empty_bindings)


def test_v2_owner_binding_rejects_uncapped_current_epoch() -> None:
    _, _, empty_bindings, _, installation = v2_fixture(maximum_owner_level=80, global_phase_cap=19)
    raw: dict[str, object] = {
        "binding_schema": 2,
        "binding_version": "owner-binding-v2",
        "realm_id": "fresh-playerbots-realm",
        "expected_generation": 2,
        "current_owner_state": {
            "binding_state": "bound",
            "owner_account_id": 5001,
            "owner_character_guid": 9001,
            "highest_owner_level_seen": 20,
            "current_epoch": 20,
            "generation": 2,
        },
        "selection": {
            "account_id": 5002,
            "character_guid": 9002,
            "actor": "owner-console",
            "selected_at_utc": "2026-09-02T20:01:00Z",
            "resolution_evidence_digest": "2" * 64,
        },
    }
    with pytest.raises(PopulationPolicyError, match="current_epoch must equal the capped"):
        compile_owner_binding_transition_v2(raw, installation, empty_bindings)


@pytest.mark.parametrize("owner_level", [3, 19, 20, 80])
def test_v2_reconciliation_separates_raw_high_water_from_capped_epoch(owner_level: int) -> None:
    _, manifest, bindings, snapshot, _ = v2_fixture(maximum_owner_level=80, global_phase_cap=19)
    raw: dict[str, object] = {
        "reconciliation_schema": 2,
        "reconciliation_version": "candidate-reconciliation-v2",
        "owner_binding_state": "bound",
        "owner_binding_generation": 2,
        "owner_account_id": 5001,
        "owner_character_guid": 9001,
        "observed_owner_level": owner_level,
        "highest_owner_level_seen": 1,
        "admissions_paused": False,
        "pause_reason": None,
        "available_creation_slots": 10,
    }

    plan = compile_admission_reconciliation_v2(raw, snapshot, manifest, bindings)
    owner_state = cast(dict[str, object], plan["owner_state"])
    authorized_epoch = min(owner_level, 19)
    assert owner_state["resulting_highest_owner_level_seen"] == owner_level
    assert owner_state["current_epoch"] == authorized_epoch
    assert owner_state["newly_authorized_levels"] == list(range(2, authorized_epoch + 1))
    assert all(level <= 19 for level in cast(list[int], plan["pending_wave_levels"]))
    assert all(
        cast(int, wave["owner_level"]) <= 19
        for wave in cast(list[dict[str, object]], plan["wave_states"])
    )


def test_v2_reconciliation_does_not_reauthorize_after_raw_high_water_exceeds_phase_cap() -> None:
    _, manifest, bindings, snapshot, _ = v2_fixture(maximum_owner_level=80, global_phase_cap=19)
    raw: dict[str, object] = {
        "reconciliation_schema": 2,
        "reconciliation_version": "candidate-reconciliation-v2",
        "owner_binding_state": "bound",
        "owner_binding_generation": 2,
        "owner_account_id": 5001,
        "owner_character_guid": 9001,
        "observed_owner_level": 80,
        "highest_owner_level_seen": 20,
        "admissions_paused": False,
        "pause_reason": None,
        "available_creation_slots": 10,
    }

    plan = compile_admission_reconciliation_v2(raw, snapshot, manifest, bindings)
    owner_state = cast(dict[str, object], plan["owner_state"])
    assert owner_state["previous_highest_owner_level_seen"] == 20
    assert owner_state["resulting_highest_owner_level_seen"] == 80
    assert owner_state["current_epoch"] == 19
    assert owner_state["newly_authorized_levels"] == []


def test_v2_reconciliation_rejects_owner_level_above_declared_maximum() -> None:
    _, manifest, bindings, snapshot, _ = v2_fixture(maximum_owner_level=80, global_phase_cap=19)
    raw: dict[str, object] = {
        "reconciliation_schema": 2,
        "reconciliation_version": "candidate-reconciliation-v2",
        "owner_binding_state": "bound",
        "owner_binding_generation": 2,
        "owner_account_id": 5001,
        "owner_character_guid": 9001,
        "observed_owner_level": 81,
        "highest_owner_level_seen": 1,
        "admissions_paused": False,
        "pause_reason": None,
        "available_creation_slots": 10,
    }
    with pytest.raises(PopulationPolicyError, match="observed_owner_level must be 1..80"):
        compile_admission_reconciliation_v2(raw, snapshot, manifest, bindings)


def test_native_owner_binding_keeps_raw_high_water_and_capped_epoch_contract() -> None:
    candidate_root = MODULE_SOURCE_ROOT
    bridge = " ".join(
        (candidate_root / "modules/mod-playerbots/src/Guide/PythonPopulationBridge.cpp")
        .read_text(encoding="utf-8")
        .split()
    )
    commands = " ".join(
        (candidate_root / "modules/mod-playerbots/src/Script/PlayerbotCommandScript.cpp")
        .read_text(encoding="utf-8")
        .split()
    )

    assert "uint8 effectiveEpoch = PlayerbotsPopulation::CappedOwnerEpoch(" in bridge
    assert (
        "level, effectiveEpoch, PopulationRealmId(), state.accountId, state.characterGuid, state.generation, level);"
        in bridge
    )
    assert "result.highWater > maximumOwnerLevel" in bridge
    assert "result.currentEpoch != PlayerbotsPopulation::CappedOwnerEpoch(" in bridge
    assert "if (!result.available || (result.reason != \"bound\" && result.reason != \"unbound\"))" in bridge
    assert "earned high-water {}, authorized epoch {}" in commands


def test_cpp_native_capped_epoch_helper_regressions(tmp_path: Path) -> None:
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("C++ compiler is unavailable")
    require_build_host()
    candidate_root = MODULE_SOURCE_ROOT
    binary = tmp_path / "native_owner_epoch_regression"
    build = subprocess.run(
        [
            compiler,
            "-std=c++20",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            f"-I{candidate_root / 'modules/mod-playerbots/src'}",
            f"-I{candidate_root / 'src/common'}",
            str(CPP_NATIVE_OWNER_EPOCH_REGRESSION),
            "-o",
            str(binary),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stderr
    result = subprocess.run([binary], check=False, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_v2_install_sql_is_owner_unbound_and_fail_if_present() -> None:
    _, _, _, _, installation = v2_fixture()
    first = render_population_install_sql_v2(installation)
    second = render_population_install_sql_v2(installation)
    assert first == second
    assert b"owner_anchor_guid" not in first
    assert b"'unbound', NULL, NULL, 1, 1, 1" in first
    assert first.count(b"'authorized'") == 1
    assert b"ON DUPLICATE KEY" not in first
    assert b"REPLACE INTO" not in first
    assert first.startswith(b"START TRANSACTION;")
    assert first.endswith(b"COMMIT;\n")


def test_cpp_v2_verifier_accepts_owner_free_artifacts_and_rejects_identity_injection(tmp_path: Path) -> None:
    compiler = shutil.which("g++")
    if compiler is None or not Path("/usr/include/boost/json.hpp").is_file():
        pytest.skip("C++ cross-language verifier dependencies are unavailable")
    require_build_host()
    binary = tmp_path / "owner_independent_v2_verifier"
    build = subprocess.run(
        [
            compiler,
            "-std=c++20",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(CPP_V2_OWNER_INDEPENDENCE_VERIFIER),
            "-lboost_json",
            "-lcrypto",
            "-o",
            str(binary),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if build.returncode != 0 and "cannot find -lboost_json" in build.stderr:
        pytest.skip("Boost.JSON development library is unavailable")
    assert build.returncode == 0, build.stderr

    policy, manifest, empty_bindings, snapshot, installation = v2_fixture()
    binding_input: dict[str, object] = {
        "binding_schema": 2,
        "binding_version": "owner-binding-v2",
        "realm_id": "fresh-playerbots-realm",
        "expected_generation": 1,
        "current_owner_state": {
            "binding_state": "unbound",
            "owner_account_id": None,
            "owner_character_guid": None,
            "highest_owner_level_seen": 1,
            "current_epoch": 1,
            "generation": 1,
        },
        "selection": {
            "account_id": 5001,
            "character_guid": 9001,
            "actor": "owner-console",
            "selected_at_utc": "2026-09-02T20:00:00Z",
            "resolution_evidence_digest": "1" * 64,
        },
    }
    transition = compile_owner_binding_transition_v2(binding_input, installation, empty_bindings)
    reconciliation = compile_admission_reconciliation_v2(
        {
            "reconciliation_schema": 2,
            "reconciliation_version": "candidate-reconciliation-v2",
            "owner_binding_state": "unbound",
            "owner_binding_generation": 1,
            "owner_account_id": None,
            "owner_character_guid": None,
            "observed_owner_level": None,
            "highest_owner_level_seen": 1,
            "admissions_paused": False,
            "pause_reason": None,
            "available_creation_slots": 10,
        },
        snapshot,
        manifest,
        empty_bindings,
    )
    paths = []
    for name, artifact in (
        ("policy", policy),
        ("snapshot", snapshot),
        ("installation", installation),
        ("reconciliation", reconciliation),
        ("binding", transition),
    ):
        path = tmp_path / f"{name}.json"
        path.write_bytes(canonical_json(artifact) + b"\n")
        paths.append(path)
    accepted = subprocess.run([binary, *paths], check=False, capture_output=True, text=True)
    assert accepted.returncode == 0, accepted.stderr
    assert "PASS" in accepted.stdout

    injected = deepcopy(installation)
    cast(dict[str, object], injected["schedule"])["owner_anchor_guid"] = 123
    del injected["installation_digest"]
    injected["installation_digest"] = digest(injected)
    injected_path = tmp_path / "installation-injected.json"
    injected_path.write_bytes(canonical_json(injected) + b"\n")
    rejected = subprocess.run(
        [binary, paths[0], paths[1], injected_path, paths[3]],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 1
    assert "embeds owner identity key" in rejected.stderr


def test_v2_schemas_and_sql_preserve_owner_independent_immutable_boundary() -> None:
    schema_names = (
        "population-runtime-snapshot-v2.schema.json",
        "population-install-plan-v2.schema.json",
        "population-owner-binding-v2.schema.json",
        "population-admission-reconciliation-v2.schema.json",
        "population-admission-recovery-v2.schema.json",
    )
    schemas = {
        name: cast(dict[str, object], json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8")))
        for name in schema_names
    }
    assert all(schema["$schema"] == "https://json-schema.org/draft/2020-12/schema" for schema in schemas.values())
    forbidden = {"owner_anchor_guid", "owner_account_id", "owner_character_guid", "owner_character_name"}
    for name in ("population-runtime-snapshot-v2.schema.json", "population-install-plan-v2.schema.json"):
        assert forbidden.isdisjoint(_recursive_keys(schemas[name]))

    sql = V2_LEDGER_SQL.read_text(encoding="utf-8")
    schedule_definition = sql.split("CREATE TABLE `population_director_owner_state`", 1)[0]
    assert "owner_anchor_guid" not in schedule_definition
    assert "CREATE TABLE IF NOT EXISTS" not in sql
    assert "`binding_state` enum('unbound','bound')" in sql
    assert "`owner_account_id` int unsigned NULL" in sql
    assert "`owner_character_guid` int unsigned NULL" in sql
    assert "population_director_owner_binding_audit" in sql
    assert "never authorizes a wave" in sql


def test_owner_epoch_migration_replaces_equality_with_raw_highwater_ceiling() -> None:
    package_base = V2_LEDGER_SQL.read_text(encoding="utf-8")
    module_base = MODULE_V2_LEDGER_UPDATE.read_text(encoding="utf-8")
    assert "CHECK (`current_epoch` = `highest_owner_level_seen`)" in package_base
    assert "CHECK (`current_epoch` = `highest_owner_level_seen`)" in module_base

    package_migration = PACKAGE_OWNER_EPOCH_MIGRATION.read_bytes()
    assert MODULE_OWNER_EPOCH_MIGRATION.read_bytes() == package_migration
    migration_sql = package_migration.decode("utf-8")
    assert MODULE_OWNER_EPOCH_MIGRATION.name > MODULE_V2_LEDGER_UPDATE.name
    assert "ALTER TABLE `population_director_owner_state`" in migration_sql
    assert (
        "DROP CHECK `chk_population_director_owner_epoch`," in migration_sql
        and "ADD CONSTRAINT `chk_population_director_owner_epoch`" in migration_sql
    )
    assert not re.search(r"\b(?:INSERT|UPDATE|DELETE|REPLACE)\b", migration_sql, re.IGNORECASE)

    check = re.search(
        r"ADD CONSTRAINT `chk_population_director_owner_epoch`\s+CHECK\s*\(([^()]*)\)",
        migration_sql,
        re.IGNORECASE,
    )
    assert check is not None
    predicate = " ".join(check.group(1).split())
    assert predicate == "`current_epoch` BETWEEN 1 AND `highest_owner_level_seen`"

    with sqlite3.connect(":memory:") as connection:
        connection.execute(
            "CREATE TABLE owner_state ("
            "highest_owner_level_seen INTEGER NOT NULL, "
            "current_epoch INTEGER NOT NULL, "
            f"CONSTRAINT owner_epoch CHECK ({predicate}))"
        )
        connection.executemany(
            "INSERT INTO owner_state (highest_owner_level_seen, current_epoch) VALUES (?, ?)",
            ((1, 1), (17, 17), (20, 19), (80, 19)),
        )
        for highest_owner_level, current_epoch in ((0, 19), (19, 0), (19, 20)):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO owner_state (highest_owner_level_seen, current_epoch) VALUES (?, ?)",
                    (highest_owner_level, current_epoch),
                )
