"""Pure deterministic policy compiler for the fresh-realm population director.

This module performs no AzerothCore registration and owns no runtime state.  It
compiles and validates low-frequency policy that a future C++ director can
consume as a versioned, hashed snapshot.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, InvalidOperation, localcontext
from typing import cast


class PopulationPolicyError(ValueError):
    """Population policy input violates a fail-closed invariant."""


@dataclass(frozen=True, slots=True)
class ProgressionRecord:
    """Earned-only state used to rank one admitted ambient character."""

    character_guid: int
    level: int
    xp_within_level: int
    threshold_reached_at: int


FACTIONS = ("alliance", "horde")
ROLES = ("tank", "healer", "damage")
RACE_RULES: dict[int, tuple[str, str, frozenset[int]]] = {
    1: ("alliance", "human", frozenset({1, 2, 4, 5, 8, 9})),
    2: ("horde", "orc_troll", frozenset({1, 3, 4, 7, 9})),
    3: ("alliance", "dwarf_gnome", frozenset({1, 2, 3, 4, 5})),
    4: ("alliance", "night_elf", frozenset({1, 3, 4, 5, 11})),
    5: ("horde", "undead", frozenset({1, 4, 5, 8, 9})),
    6: ("horde", "tauren", frozenset({1, 3, 7, 11})),
    7: ("alliance", "dwarf_gnome", frozenset({1, 4, 8, 9})),
    8: ("horde", "orc_troll", frozenset({1, 3, 4, 5, 7, 8})),
    10: ("horde", "blood_elf", frozenset({2, 3, 4, 5, 8, 9})),
    11: ("alliance", "draenei", frozenset({1, 2, 3, 5, 7, 8})),
}
NON_DK_CLASSES = frozenset({1, 2, 3, 4, 5, 7, 8, 9, 11})
STARTING_REGIONS = frozenset(rule[1] for rule in RACE_RULES.values())
APPEARANCE_FIELDS = frozenset({"gender", "skin", "face", "hair_style", "hair_color", "facial_hair", "outfit_id"})
PERSISTED_APPEARANCE_FIELDS = frozenset(APPEARANCE_FIELDS - {"outfit_id"})
QUEST_STATE_FIELDS = frozenset(
    {
        "status_count",
        "rewarded_count",
        "daily_count",
        "weekly_count",
        "monthly_count",
        "seasonal_count",
    }
)
HELD_ACTIVITY_TYPES = (
    "combat_farming",
    "crafting",
    "eligible_dungeon",
    "fishing",
    "gathering",
    "gear_improvement",
    "mail",
    "phase_safe_travel",
    "profession_training",
    "repair",
    "social",
    "trade",
)
DENIED_PROGRESSION_MUTATIONS = (
    "banked_xp",
    "quest_accept",
    "quest_complete",
    "quest_objective_credit",
    "quest_reward",
    "quest_script_complete",
    "quest_turn_in",
    "xp_award",
)


def _require_exact_keys(raw: Mapping[str, object], expected: set[str], name: str) -> None:
    missing = sorted(expected - set(raw))
    unknown = sorted(set(raw) - expected)
    if missing:
        raise PopulationPolicyError(f"{name} missing fields: {', '.join(missing)}")
    if unknown:
        raise PopulationPolicyError(f"{name} has unknown fields: {', '.join(unknown)}")


def canonical_json(value: object) -> bytes:
    """Return the one byte representation used for all policy digests."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _require_digest(value: object, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise PopulationPolicyError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def _verify_compiled_artifact(
    artifact: Mapping[str, object],
    digest_field: str,
    name: str,
) -> str:
    actual = _require_digest(artifact.get(digest_field), f"{name}.{digest_field}")
    payload = dict(artifact)
    del payload[digest_field]
    if digest(payload) != actual:
        raise PopulationPolicyError(f"{name}.{digest_field} does not match artifact content")
    return actual


def _require_int(value: object, name: str, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PopulationPolicyError(f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        suffix = f"..{maximum}" if maximum is not None else " or greater"
        raise PopulationPolicyError(f"{name} must be {minimum}{suffix}")
    return value


def _require_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise PopulationPolicyError(f"{name} must be boolean")
    return value


def _require_token(value: object, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise PopulationPolicyError(f"{name} must be a non-empty stable token")
    return value


def _require_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int)):
        raise PopulationPolicyError(f"{name} must be a decimal string or integer, never a float")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise PopulationPolicyError(f"{name} is not a valid decimal") from exc
    if not result.is_finite():
        raise PopulationPolicyError(f"{name} must be finite")
    return result


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _repair_monotone(allocations: list[int]) -> None:
    """Move slots toward earlier levels until the sequence is non-increasing."""

    while True:
        changed = False
        for index in range(len(allocations) - 1):
            if allocations[index] >= allocations[index + 1]:
                continue
            transfer = (allocations[index + 1] - allocations[index] + 1) // 2
            allocations[index] += transfer
            allocations[index + 1] -= transfer
            changed = True
        if not changed:
            return


def _apportion(total: int, weights: Sequence[Decimal]) -> tuple[int, ...]:
    if total < 0:
        raise PopulationPolicyError("apportionment total must not be negative")
    if not weights or any(weight < 0 for weight in weights):
        raise PopulationPolicyError("apportionment weights must be a non-empty non-negative sequence")
    weight_sum = sum(weights, Decimal(0))
    if weight_sum <= 0:
        raise PopulationPolicyError("at least one apportionment weight must be positive")

    exact = [Decimal(total) * weight / weight_sum for weight in weights]
    allocations = [int(value.to_integral_value(rounding=ROUND_FLOOR)) for value in exact]
    remaining = total - sum(allocations)
    remainder_order = sorted(
        range(len(weights)),
        key=lambda index: (-(exact[index] - Decimal(allocations[index])), index),
    )
    for index in remainder_order[:remaining]:
        allocations[index] += 1
    if sum(allocations) != total:
        raise AssertionError("internal apportionment error")
    return tuple(allocations)


def compile_wave_schedule(raw: Mapping[str, object]) -> dict[str, object]:
    """Compile normalized-exponential owner-level waves with an exact total."""

    _require_exact_keys(
        raw,
        {
            "schema_version",
            "schedule_version",
            "realm_id",
            "owner_anchor_guid",
            "maximum_owner_level",
            "permanent_roster_target",
            "curve_kind",
            "decay",
        },
        "schedule",
    )
    schema_version = _require_int(raw.get("schema_version"), "schema_version", 1, 1)
    schedule_version = _require_token(raw.get("schedule_version"), "schedule_version")
    realm_id = _require_token(raw.get("realm_id"), "realm_id")
    owner_anchor_guid = _require_int(raw.get("owner_anchor_guid"), "owner_anchor_guid", 1)
    maximum_owner_level = _require_int(raw.get("maximum_owner_level"), "maximum_owner_level", 1, 80)
    permanent_roster_target = _require_int(raw.get("permanent_roster_target"), "permanent_roster_target", 1)
    curve_kind = raw.get("curve_kind")
    if curve_kind != "normalized_exponential":
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
        "owner_anchor_guid": owner_anchor_guid,
        "maximum_owner_level": maximum_owner_level,
        "permanent_roster_target": permanent_roster_target,
        "wave_curve": {"kind": curve_kind, "decay": _decimal_text(decay)},
        "allocation": {"kind": "deterministic_monotone_integer"},
        "waves": [
            {"owner_level": owner_level, "count": count} for owner_level, count in enumerate(allocations, start=1)
        ],
    }
    return {**payload, "schedule_digest": digest(payload)}


def _compile_bands(
    raw_bands: object,
    global_phase_cap: int,
) -> tuple[tuple[str, ...], tuple[Decimal, ...], dict[str, int]]:
    if not isinstance(raw_bands, list) or not raw_bands:
        raise PopulationPolicyError("bands must be a non-empty ordered list")
    names: list[str] = []
    shares: list[Decimal] = []
    ceilings: dict[str, int] = {}
    for ordinal, raw_band in enumerate(raw_bands):
        if not isinstance(raw_band, dict):
            raise PopulationPolicyError(f"bands[{ordinal}] must be an object")
        _require_exact_keys(raw_band, {"name", "share", "ceiling"}, f"bands[{ordinal}]")
        name = _require_token(raw_band.get("name"), f"bands[{ordinal}].name")
        if name in names:
            raise PopulationPolicyError(f"duplicate band name: {name}")
        share = _require_decimal(raw_band.get("share"), f"bands[{ordinal}].share")
        if share < 0:
            raise PopulationPolicyError(f"bands[{ordinal}].share must not be negative")
        ceiling = _require_int(raw_band.get("ceiling"), f"bands[{ordinal}].ceiling", 1, global_phase_cap)
        names.append(name)
        shares.append(share)
        ceilings[name] = ceiling
    if sum(shares, Decimal(0)) != Decimal(1):
        raise PopulationPolicyError("band shares must sum exactly to 1")
    if any(ceilings[left] < ceilings[right] for left, right in zip(names, names[1:], strict=False)):
        raise PopulationPolicyError("band ceilings must be non-increasing in configured rank order")
    return tuple(names), tuple(shares), ceilings


def compile_policy(raw: Mapping[str, object]) -> dict[str, object]:
    """Compile a complete offline schedule and epoch capacity snapshot."""

    _require_exact_keys(
        raw,
        {"policy_schema", "policy_version", "global_phase_cap", "schedule", "bands"},
        "policy",
    )
    policy_schema = _require_int(raw.get("policy_schema"), "policy_schema", 1, 1)
    policy_version = _require_token(raw.get("policy_version"), "policy_version")
    schedule_raw = raw.get("schedule")
    if not isinstance(schedule_raw, dict):
        raise PopulationPolicyError("schedule must be an object")
    schedule = compile_wave_schedule(schedule_raw)
    global_phase_cap = _require_int(raw.get("global_phase_cap"), "global_phase_cap", 1, 80)
    names, shares, ceilings = _compile_bands(raw.get("bands"), global_phase_cap)

    waves_raw = cast(list[dict[str, int]], schedule["waves"])
    cumulative = 0
    epochs: list[dict[str, object]] = []
    for wave in waves_raw:
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


def _compile_share_map(raw: object, ordered_names: tuple[str, ...], name: str) -> tuple[Decimal, ...]:
    if not isinstance(raw, dict):
        raise PopulationPolicyError(f"{name} must be an object")
    _require_exact_keys(raw, set(ordered_names), name)
    shares = tuple(_require_decimal(raw[item], f"{name}.{item}") for item in ordered_names)
    if any(share < 0 for share in shares) or sum(shares, Decimal(0)) != Decimal(1):
        raise PopulationPolicyError(f"{name} shares must be non-negative and sum exactly to 1")
    return shares


def _counter_report(counter: Counter[object]) -> dict[str, int]:
    return {str(key): counter[key] for key in sorted(counter, key=str)}


def compile_population_manifest(
    raw: Mapping[str, object],
    schedule: Mapping[str, object],
) -> dict[str, object]:
    """Validate immutable pre-authored identities against one compiled schedule."""

    compiled_schedule_digest = _verify_compiled_artifact(schedule, "schedule_digest", "schedule")

    _require_exact_keys(
        raw,
        {
            "manifest_schema",
            "manifest_version",
            "realm_id",
            "schedule_digest",
            "composition",
            "spec_templates",
            "identities",
        },
        "population manifest",
    )
    manifest_schema = _require_int(raw.get("manifest_schema"), "manifest_schema", 1, 1)
    manifest_version = _require_token(raw.get("manifest_version"), "manifest_version")
    realm_id = _require_token(raw.get("realm_id"), "realm_id")
    if realm_id != schedule.get("realm_id"):
        raise PopulationPolicyError("manifest realm_id does not match schedule")
    schedule_digest = _require_digest(raw.get("schedule_digest"), "schedule_digest")
    if schedule_digest != compiled_schedule_digest:
        raise PopulationPolicyError("manifest schedule_digest does not match compiled schedule")

    composition = raw.get("composition")
    if not isinstance(composition, dict):
        raise PopulationPolicyError("composition must be an object")
    _require_exact_keys(
        composition,
        {"faction_shares", "role_shares", "required_class_floor_per_faction", "required_region_floor"},
        "composition",
    )
    faction_shares = _compile_share_map(composition["faction_shares"], FACTIONS, "faction_shares")
    role_shares = _compile_share_map(composition["role_shares"], ROLES, "role_shares")
    class_floor = _require_int(
        composition["required_class_floor_per_faction"],
        "required_class_floor_per_faction",
        0,
    )
    region_floor = _require_int(composition["required_region_floor"], "required_region_floor", 0)

    raw_templates = raw.get("spec_templates")
    if not isinstance(raw_templates, list) or not raw_templates:
        raise PopulationPolicyError("spec_templates must be a non-empty sorted list")
    templates: dict[str, tuple[int, str]] = {}
    normalized_templates: list[dict[str, object]] = []
    for ordinal, raw_template in enumerate(raw_templates):
        if not isinstance(raw_template, dict):
            raise PopulationPolicyError(f"spec_templates[{ordinal}] must be an object")
        _require_exact_keys(
            raw_template,
            {"template_id", "class", "role_intent", "minimum_functional_level"},
            f"spec_templates[{ordinal}]",
        )
        template_id = _require_token(raw_template["template_id"], f"spec_templates[{ordinal}].template_id")
        class_id = _require_int(raw_template["class"], f"spec_templates[{ordinal}].class", 1, 11)
        role = raw_template["role_intent"]
        if role not in ROLES:
            raise PopulationPolicyError(f"spec_templates[{ordinal}].role_intent is invalid")
        functional_level = _require_int(
            raw_template["minimum_functional_level"],
            f"spec_templates[{ordinal}].minimum_functional_level",
            1,
            80,
        )
        if class_id == 6 or class_id not in NON_DK_CLASSES:
            raise PopulationPolicyError(f"spec template {template_id} uses an excluded class")
        if template_id in templates:
            raise PopulationPolicyError(f"duplicate spec template: {template_id}")
        templates[template_id] = (class_id, cast(str, role))
        normalized_templates.append(
            {
                "template_id": template_id,
                "class": class_id,
                "role_intent": role,
                "minimum_functional_level": functional_level,
            }
        )
    template_order = [cast(str, item["template_id"]) for item in normalized_templates]
    if template_order != sorted(template_order):
        raise PopulationPolicyError("spec_templates must be sorted by template_id")

    target = _require_int(schedule.get("permanent_roster_target"), "schedule permanent_roster_target", 1)
    maximum_level = _require_int(schedule.get("maximum_owner_level"), "schedule maximum_owner_level", 1, 80)
    schedule_waves = cast(list[dict[str, int]], schedule.get("waves"))
    expected_waves = {item["owner_level"]: item["count"] for item in schedule_waves}
    if set(expected_waves) != set(range(1, maximum_level + 1)):
        raise PopulationPolicyError("compiled schedule has incomplete owner levels")

    raw_identities = raw.get("identities")
    if not isinstance(raw_identities, list) or len(raw_identities) != target:
        raise PopulationPolicyError(f"identities must contain exactly {target} entries")
    normalized_identities: list[dict[str, object]] = []
    unique_ids: set[str] = set()
    account_names: dict[str, str] = {}
    account_classes: dict[str, set[int]] = {}
    unique_names: set[str] = set()
    wave_ordinals: set[tuple[int, int]] = set()
    faction_counts: Counter[object] = Counter()
    role_counts: Counter[object] = Counter()
    class_by_faction: Counter[object] = Counter()
    region_counts: Counter[object] = Counter()
    spec_counts: Counter[object] = Counter()
    wave_counts: Counter[object] = Counter()
    eligibility_counts: Counter[object] = Counter()

    identity_fields = {
        "manifest_identity",
        "account_name",
        "character_name",
        "faction",
        "race",
        "class",
        "role_intent",
        "spec_template",
        "starting_region",
        "creation_wave_owner_level",
        "wave_ordinal",
        "admission_eligible",
        "appearance",
    }
    for ordinal, raw_identity in enumerate(raw_identities):
        if not isinstance(raw_identity, dict):
            raise PopulationPolicyError(f"identities[{ordinal}] must be an object")
        _require_exact_keys(raw_identity, identity_fields, f"identities[{ordinal}]")
        identity = _require_token(raw_identity["manifest_identity"], f"identities[{ordinal}].manifest_identity")
        account = _require_token(raw_identity["account_name"], f"identities[{ordinal}].account_name")
        character_name = raw_identity["character_name"]
        if not isinstance(character_name, str) or not re.fullmatch(r"[A-Za-z]{2,12}", character_name):
            raise PopulationPolicyError(f"identities[{ordinal}].character_name must be 2-12 ASCII letters")
        identity_key = identity.casefold()
        account_key = account.casefold()
        name_key = character_name.casefold()
        if identity_key in unique_ids:
            raise PopulationPolicyError(f"duplicate manifest identity: {identity}")
        canonical_account = account_names.get(account_key)
        if canonical_account is not None and canonical_account != account:
            raise PopulationPolicyError(
                f"account binding differs only by case: {canonical_account}/{account}"
            )
        if name_key in unique_names:
            raise PopulationPolicyError(f"duplicate character name: {character_name}")
        unique_ids.add(identity_key)
        account_names.setdefault(account_key, account)
        unique_names.add(name_key)

        race = _require_int(raw_identity["race"], f"identities[{ordinal}].race", 1, 11)
        class_id = _require_int(raw_identity["class"], f"identities[{ordinal}].class", 1, 11)
        if race not in RACE_RULES or class_id not in RACE_RULES[race][2]:
            raise PopulationPolicyError(f"identity {identity} has an illegal or excluded WotLK race/class pair")
        classes = account_classes.setdefault(account_key, set())
        if class_id in classes:
            raise PopulationPolicyError(
                f"account {account} has more than one character for class {class_id}"
            )
        if len(classes) >= len(NON_DK_CLASSES):
            raise PopulationPolicyError(
                f"account {account} exceeds the nine non-Death-Knight character slots"
            )
        classes.add(class_id)
        expected_faction, expected_region, _ = RACE_RULES[race]
        faction = raw_identity["faction"]
        region = raw_identity["starting_region"]
        if faction != expected_faction:
            raise PopulationPolicyError(f"identity {identity} has a faction/race mismatch")
        if region != expected_region:
            raise PopulationPolicyError(f"identity {identity} has a starting-region/race mismatch")

        role = raw_identity["role_intent"]
        if role not in ROLES:
            raise PopulationPolicyError(f"identity {identity} has an invalid role intent")
        template = raw_identity["spec_template"]
        if not isinstance(template, str) or template not in templates:
            raise PopulationPolicyError(f"identity {identity} references an unknown spec template")
        if templates[template] != (class_id, role):
            raise PopulationPolicyError(f"identity {identity} spec template does not match class and role intent")

        wave_level = _require_int(
            raw_identity["creation_wave_owner_level"],
            f"identities[{ordinal}].creation_wave_owner_level",
            1,
            maximum_level,
        )
        wave_ordinal = _require_int(raw_identity["wave_ordinal"], f"identities[{ordinal}].wave_ordinal", 1)
        wave_key = (wave_level, wave_ordinal)
        if wave_key in wave_ordinals:
            raise PopulationPolicyError(f"duplicate wave ordinal: {wave_level}/{wave_ordinal}")
        wave_ordinals.add(wave_key)
        eligible = raw_identity["admission_eligible"]
        if not isinstance(eligible, bool):
            raise PopulationPolicyError(f"identities[{ordinal}].admission_eligible must be boolean")

        appearance = raw_identity["appearance"]
        if not isinstance(appearance, dict):
            raise PopulationPolicyError(f"identity {identity} appearance must be an object")
        _require_exact_keys(appearance, set(APPEARANCE_FIELDS), f"identity {identity} appearance")
        normalized_appearance: dict[str, int] = {}
        for field in sorted(APPEARANCE_FIELDS):
            maximum = 1 if field == "gender" else 255
            normalized_appearance[field] = _require_int(
                appearance[field],
                f"identity {identity} appearance.{field}",
                0,
                maximum,
            )
        if normalized_appearance["outfit_id"] != 0:
            raise PopulationPolicyError(
                f"identity {identity} appearance.outfit_id must be 0 because the pinned core ignores it"
            )

        normalized_identities.append(
            {
                "manifest_identity": identity,
                "account_name": account,
                "character_name": character_name,
                "faction": faction,
                "race": race,
                "class": class_id,
                "role_intent": role,
                "spec_template": template,
                "starting_region": region,
                "creation_wave_owner_level": wave_level,
                "wave_ordinal": wave_ordinal,
                "admission_eligible": eligible,
                "appearance": normalized_appearance,
            }
        )
        faction_counts[faction] += 1
        role_counts[role] += 1
        class_by_faction[(faction, class_id)] += 1
        region_counts[region] += 1
        spec_counts[template] += 1
        wave_counts[wave_level] += 1
        eligibility_counts[str(eligible).lower()] += 1

    identity_order = [cast(str, item["manifest_identity"]) for item in normalized_identities]
    if identity_order != sorted(identity_order):
        raise PopulationPolicyError("identities must be sorted by manifest_identity")
    for level, expected_count in expected_waves.items():
        if wave_counts[level] != expected_count:
            raise PopulationPolicyError(
                f"wave {level} has {wave_counts[level]} identities; schedule requires {expected_count}"
            )
        expected_ordinals = set(range(1, expected_count + 1))
        actual_ordinals = {item_ordinal for item_level, item_ordinal in wave_ordinals if item_level == level}
        if actual_ordinals != expected_ordinals:
            raise PopulationPolicyError(f"wave {level} ordinals must be exactly 1..{expected_count}")

    expected_factions = dict(zip(FACTIONS, _apportion(target, faction_shares), strict=True))
    expected_roles = dict(zip(ROLES, _apportion(target, role_shares), strict=True))
    if any(faction_counts[name] != expected for name, expected in expected_factions.items()):
        raise PopulationPolicyError("manifest does not match configured faction shares")
    if any(role_counts[name] != expected for name, expected in expected_roles.items()):
        raise PopulationPolicyError("manifest does not match configured role shares")
    for faction in FACTIONS:
        for class_id in sorted(NON_DK_CLASSES):
            if class_by_faction[(faction, class_id)] < class_floor:
                raise PopulationPolicyError(f"{faction} class {class_id} is below the configured floor")
    for region in sorted(STARTING_REGIONS):
        if region_counts[region] < region_floor:
            raise PopulationPolicyError(f"starting region {region} is below the configured floor")

    normalized_composition: dict[str, object] = {
        "faction_shares": dict(zip(FACTIONS, (_decimal_text(share) for share in faction_shares), strict=True)),
        "role_shares": dict(zip(ROLES, (_decimal_text(share) for share in role_shares), strict=True)),
        "required_class_floor_per_faction": class_floor,
        "required_region_floor": region_floor,
    }
    reports: dict[str, object] = {
        "account": {
            "account_count": len(account_names),
            "maximum_characters_per_account": len(NON_DK_CLASSES),
            "characters_per_account": {
                account_names[key]: len(classes)
                for key, classes in sorted(account_classes.items())
            },
        },
        "faction": _counter_report(faction_counts),
        "role_intent": _counter_report(role_counts),
        "class_by_faction": _counter_report(class_by_faction),
        "starting_region": _counter_report(region_counts),
        "spec_template": _counter_report(spec_counts),
        "creation_wave": _counter_report(wave_counts),
        "admission_eligible": _counter_report(eligibility_counts),
    }
    payload: dict[str, object] = {
        "manifest_schema": manifest_schema,
        "manifest_version": manifest_version,
        "realm_id": realm_id,
        "schedule_digest": schedule_digest,
        "composition": normalized_composition,
        "spec_templates": normalized_templates,
        "identities": normalized_identities,
        "reports": reports,
    }
    return {**payload, "manifest_digest": digest(payload)}


def compile_identity_bindings(
    raw_bindings: Sequence[Mapping[str, object]],
    manifest: Mapping[str, object],
) -> dict[str, object]:
    """Validate an admitted subset's database IDs without mutating the manifest."""

    manifest_digest = _verify_compiled_artifact(manifest, "manifest_digest", "manifest")
    identities = cast(list[dict[str, object]], manifest.get("identities"))
    account_names_by_identity = {
        cast(str, item["manifest_identity"]): cast(str, item["account_name"])
        for item in identities
    }
    known = set(account_names_by_identity)
    rows: list[dict[str, object]] = []
    seen_identities: set[str] = set()
    seen_guids: set[int] = set()
    account_ids_by_name: dict[str, int] = {}
    account_names_by_id: dict[int, str] = {}
    for ordinal, binding in enumerate(raw_bindings):
        _require_exact_keys(binding, {"manifest_identity", "account_id", "character_guid"}, f"bindings[{ordinal}]")
        identity = _require_token(binding.get("manifest_identity"), f"bindings[{ordinal}].manifest_identity")
        account_id = _require_int(binding.get("account_id"), f"bindings[{ordinal}].account_id", 1)
        character_guid = _require_int(binding.get("character_guid"), f"bindings[{ordinal}].character_guid", 1)
        if identity not in known:
            raise PopulationPolicyError(f"binding references unknown identity: {identity}")
        if identity in seen_identities or character_guid in seen_guids:
            raise PopulationPolicyError("identity and character GUID bindings must be one-to-one")
        account_name = account_names_by_identity[identity].casefold()
        bound_account_id = account_ids_by_name.get(account_name)
        bound_account_name = account_names_by_id.get(account_id)
        if bound_account_id is not None and bound_account_id != account_id:
            raise PopulationPolicyError("one manifest account name maps to multiple account IDs")
        if bound_account_name is not None and bound_account_name != account_name:
            raise PopulationPolicyError("one account ID maps to multiple manifest account names")
        seen_identities.add(identity)
        seen_guids.add(character_guid)
        account_ids_by_name[account_name] = account_id
        account_names_by_id[account_id] = account_name
        rows.append({"manifest_identity": identity, "account_id": account_id, "character_guid": character_guid})
    if [cast(str, item["manifest_identity"]) for item in rows] != sorted(seen_identities):
        raise PopulationPolicyError("identity bindings must be sorted by manifest_identity")
    payload: dict[str, object] = {"manifest_digest": manifest_digest, "bindings": rows}
    return {**payload, "binding_digest": digest(payload)}


def compile_runtime_snapshot(
    raw: Mapping[str, object],
    policy: Mapping[str, object],
    manifest: Mapping[str, object],
    bindings: Mapping[str, object],
) -> dict[str, object]:
    """Bind compiled policy artifacts to one fail-closed C++ runtime envelope."""

    _require_exact_keys(
        raw,
        {"snapshot_schema", "snapshot_version", "capacity_limits", "held_activity_whitelist"},
        "runtime snapshot input",
    )
    snapshot_schema = _require_int(raw.get("snapshot_schema"), "snapshot_schema", 1, 1)
    snapshot_version = _require_token(raw.get("snapshot_version"), "snapshot_version")

    policy_digest = _verify_compiled_artifact(policy, "policy_digest", "policy")
    manifest_digest = _verify_compiled_artifact(manifest, "manifest_digest", "manifest")
    binding_digest = _verify_compiled_artifact(bindings, "binding_digest", "bindings")

    schedule = policy.get("schedule")
    if not isinstance(schedule, dict):
        raise PopulationPolicyError("compiled policy schedule must be an object")
    schedule_digest = _verify_compiled_artifact(schedule, "schedule_digest", "policy.schedule")
    if manifest.get("schedule_digest") != schedule_digest:
        raise PopulationPolicyError("manifest does not bind the compiled policy schedule")
    if bindings.get("manifest_digest") != manifest_digest:
        raise PopulationPolicyError("bindings do not bind the compiled manifest")

    realm_id = _require_token(policy.get("realm_id", schedule.get("realm_id")), "realm_id")
    if manifest.get("realm_id") != realm_id or schedule.get("realm_id") != realm_id:
        raise PopulationPolicyError("policy schedule and manifest realm_id values must match")
    owner_anchor_guid = _require_int(schedule.get("owner_anchor_guid"), "owner_anchor_guid", 1)
    maximum_owner_level = _require_int(
        schedule.get("maximum_owner_level"),
        "maximum_owner_level",
        1,
        80,
    )
    permanent_roster_target = _require_int(
        schedule.get("permanent_roster_target"),
        "permanent_roster_target",
        1,
    )
    global_phase_cap = _require_int(policy.get("global_phase_cap"), "global_phase_cap", 1, 80)
    if global_phase_cap > maximum_owner_level:
        raise PopulationPolicyError("global_phase_cap must not exceed maximum_owner_level")

    identities = manifest.get("identities")
    binding_rows = bindings.get("bindings")
    if not isinstance(identities, list) or len(identities) != permanent_roster_target:
        raise PopulationPolicyError("compiled manifest identity count must equal permanent_roster_target")
    if not isinstance(binding_rows, list) or len(binding_rows) > len(identities):
        raise PopulationPolicyError("compiled binding count must not exceed manifest identity count")

    raw_capacities = raw.get("capacity_limits")
    if not isinstance(raw_capacities, dict):
        raise PopulationPolicyError("capacity_limits must be an object")
    capacity_fields = {
        "server_session_budget",
        "safety_reserve",
        "concurrent_active_target",
        "creation_batch_size",
        "login_batch_size",
        "max_active_per_map",
        "max_active_per_zone",
        "max_active_per_starting_region",
    }
    _require_exact_keys(raw_capacities, capacity_fields, "capacity_limits")
    normalized_capacities = {
        field: _require_int(raw_capacities[field], f"capacity_limits.{field}", 1) for field in sorted(capacity_fields)
    }
    session_budget = normalized_capacities["server_session_budget"]
    safety_reserve = normalized_capacities["safety_reserve"]
    active_target = normalized_capacities["concurrent_active_target"]
    if safety_reserve < 5:
        raise PopulationPolicyError("capacity_limits.safety_reserve must preserve at least the five-character party")
    if active_target + safety_reserve > session_budget:
        raise PopulationPolicyError("concurrent_active_target plus safety_reserve exceeds server_session_budget")
    if normalized_capacities["creation_batch_size"] > active_target:
        raise PopulationPolicyError("creation_batch_size must not exceed concurrent_active_target")
    if normalized_capacities["login_batch_size"] > active_target:
        raise PopulationPolicyError("login_batch_size must not exceed concurrent_active_target")
    density_chain = (
        normalized_capacities["max_active_per_starting_region"],
        normalized_capacities["max_active_per_zone"],
        normalized_capacities["max_active_per_map"],
        active_target,
    )
    if any(left > right for left, right in zip(density_chain, density_chain[1:], strict=False)):
        raise PopulationPolicyError("starting-region zone map and global active limits must be non-decreasing")

    raw_whitelist = raw.get("held_activity_whitelist")
    if not isinstance(raw_whitelist, list) or any(not isinstance(item, str) for item in raw_whitelist):
        raise PopulationPolicyError("held_activity_whitelist must be a sorted list of activity tokens")
    whitelist = cast(list[str], raw_whitelist)
    if whitelist != sorted(set(whitelist)):
        raise PopulationPolicyError("held_activity_whitelist must be sorted and contain no duplicates")
    unknown_activities = sorted(set(whitelist) - set(HELD_ACTIVITY_TYPES))
    if unknown_activities:
        raise PopulationPolicyError(
            f"held_activity_whitelist contains unknown activities: {', '.join(unknown_activities)}"
        )

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
        "owner_anchor_guid": owner_anchor_guid,
        "maximum_owner_level": maximum_owner_level,
        "permanent_roster_target": permanent_roster_target,
        "manifest_identity_count": len(identities),
        "binding_count": len(binding_rows),
        "global_phase_cap": global_phase_cap,
        "effective_ceiling_rule": "min(global_phase_cap,dynamic_band_ceiling)",
        "bands": bands,
        "band_capacity_epochs": epochs,
        "capacity_limits": normalized_capacities,
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


def compile_population_install_plan(
    raw: Mapping[str, object],
    snapshot: Mapping[str, object],
    policy: Mapping[str, object],
    manifest: Mapping[str, object],
) -> dict[str, object]:
    """Compile the immutable database seed consumed by the runtime director.

    The plan performs no database work. It binds the reviewed policy, schedule,
    runtime envelope, and every pre-authored manifest identity into one
    canonical artifact suitable for a separately reviewed transactional
    installer. Mutable admission/recovery rows are deliberately excluded.
    """

    _require_exact_keys(raw, {"installation_schema", "installation_version"}, "installation input")
    installation_schema = _require_int(raw.get("installation_schema"), "installation_schema", 1, 1)
    installation_version = _require_token(raw.get("installation_version"), "installation_version")
    snapshot_digest = _verify_compiled_artifact(snapshot, "snapshot_digest", "snapshot")
    policy_digest = _verify_compiled_artifact(policy, "policy_digest", "policy")
    manifest_digest = _verify_compiled_artifact(manifest, "manifest_digest", "manifest")

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
    if source_digests.get("schedule_digest") != schedule_digest:
        raise PopulationPolicyError("snapshot does not bind the supplied schedule")
    realm_id = _require_token(snapshot.get("realm_id"), "snapshot.realm_id")
    if schedule.get("realm_id") != realm_id or manifest.get("realm_id") != realm_id:
        raise PopulationPolicyError("installation realm_id values do not match")
    if manifest.get("schedule_digest") != schedule_digest:
        raise PopulationPolicyError("manifest does not bind the supplied schedule")

    owner_anchor_guid = _require_int(snapshot.get("owner_anchor_guid"), "owner_anchor_guid", 1)
    maximum_owner_level = _require_int(snapshot.get("maximum_owner_level"), "maximum_owner_level", 1, 80)
    permanent_roster_target = _require_int(
        snapshot.get("permanent_roster_target"),
        "permanent_roster_target",
        1,
    )
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
        wave_key = digest(
            {
                "realm_id": realm_id,
                "owner_anchor_guid": owner_anchor_guid,
                "owner_level": owner_level,
                "schedule_digest": schedule_digest,
            }
        )
        expected_wave_counts[owner_level] = count
        wave_rows.append(
            {
                "owner_level": owner_level,
                "wave_idempotency_key": wave_key,
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
        character_name = _require_token(identity_raw.get("character_name"), f"identity {identity}.character_name")
        wave_level = _require_int(
            identity_raw.get("creation_wave_owner_level"),
            f"identity {identity}.creation_wave_owner_level",
            1,
            maximum_owner_level,
        )
        wave_ordinal = _require_int(identity_raw.get("wave_ordinal"), f"identity {identity}.wave_ordinal", 1)
        appearance = identity_raw.get("appearance")
        if not isinstance(appearance, dict):
            raise PopulationPolicyError(f"identity {identity}.appearance must be an object")
        _require_exact_keys(appearance, set(APPEARANCE_FIELDS), f"identity {identity}.appearance")
        account_manifest_size = account_sizes[account_name.casefold()]
        if account_manifest_size < 1 or account_manifest_size > len(NON_DK_CLASSES):
            raise PopulationPolicyError(f"identity {identity} account manifest size is outside 1..9")
        actual_wave_counts[wave_level] += 1
        manifest_rows.append(
            {
                "manifest_identity": identity,
                "wave_idempotency_key": cast(str, wave_rows[wave_level - 1]["wave_idempotency_key"]),
                "owner_level": wave_level,
                "wave_ordinal": wave_ordinal,
                "account_name": account_name,
                "account_manifest_size": account_manifest_size,
                "character_name": character_name,
                "faction": _require_token(identity_raw.get("faction"), f"identity {identity}.faction"),
                "race": _require_int(identity_raw.get("race"), f"identity {identity}.race", 1, 11),
                "class": _require_int(identity_raw.get("class"), f"identity {identity}.class", 1, 11),
                "role_intent": _require_token(identity_raw.get("role_intent"), f"identity {identity}.role_intent"),
                "spec_template": _require_token(
                    identity_raw.get("spec_template"),
                    f"identity {identity}.spec_template",
                ),
                "starting_region": _require_token(
                    identity_raw.get("starting_region"),
                    f"identity {identity}.starting_region",
                ),
                "admission_eligible": _require_bool(
                    identity_raw.get("admission_eligible"),
                    f"identity {identity}.admission_eligible",
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
            "owner_anchor_guid": owner_anchor_guid,
            "maximum_owner_level": maximum_owner_level,
            "permanent_roster_target": permanent_roster_target,
            "global_phase_cap": global_phase_cap,
        },
        "owner_state": {
            "highest_owner_level_seen": 1,
            "current_epoch": 1,
            "generation": 1,
        },
        "waves": wave_rows,
        "manifest_identities": manifest_rows,
    }
    return {**payload, "installation_digest": digest(payload)}


def render_population_install_sql(plan: Mapping[str, object]) -> bytes:
    """Render one fail-if-present transaction for an already migrated database."""

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
    _require_int(plan.get("installation_schema"), "installation_schema", 1, 1)
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
        {
            "schedule_version",
            "owner_anchor_guid",
            "maximum_owner_level",
            "permanent_roster_target",
            "global_phase_cap",
        },
        "installation schedule",
    )
    schedule_version = _require_token(schedule.get("schedule_version"), "schedule.schedule_version")
    owner_anchor_guid = _require_int(schedule.get("owner_anchor_guid"), "schedule.owner_anchor_guid", 1)
    maximum_owner_level = _require_int(schedule.get("maximum_owner_level"), "schedule.maximum_owner_level", 1, 80)
    permanent_roster_target = _require_int(
        schedule.get("permanent_roster_target"),
        "schedule.permanent_roster_target",
        1,
    )
    global_phase_cap = _require_int(schedule.get("global_phase_cap"), "schedule.global_phase_cap", 1, 80)
    _require_exact_keys(
        owner_state,
        {"highest_owner_level_seen", "current_epoch", "generation"},
        "installation owner_state",
    )
    highest_owner_level_seen = _require_int(
        owner_state.get("highest_owner_level_seen"),
        "owner_state.highest_owner_level_seen",
        1,
        80,
    )
    current_epoch = _require_int(owner_state.get("current_epoch"), "owner_state.current_epoch", 1, 80)
    owner_generation = _require_int(owner_state.get("generation"), "owner_state.generation", 1)

    def quoted(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise PopulationPolicyError("SQL installation strings must be stable ASCII tokens")
        return f"'{value}'"

    statements = [
        "START TRANSACTION;",
        "INSERT INTO population_director_schedule ",
        "  (realm_id, schedule_version, schedule_digest, policy_digest, manifest_digest, ",
        "   snapshot_digest, installation_digest, owner_anchor_guid, maximum_owner_level, ",
        "   global_phase_cap, permanent_roster_target) VALUES",
        "  ("
        + ", ".join(
            (
                quoted(realm_id),
                quoted(schedule_version),
                quoted(digests["schedule_digest"]),
                quoted(digests["policy_digest"]),
                quoted(digests["manifest_digest"]),
                quoted(digests["snapshot_digest"]),
                quoted(installation_digest),
                str(owner_anchor_guid),
                str(maximum_owner_level),
                str(global_phase_cap),
                str(permanent_roster_target),
            )
        )
        + ");",
        "INSERT INTO population_director_owner_state ",
        "  (realm_id, owner_anchor_guid, highest_owner_level_seen, current_epoch, generation) VALUES",
        "  ("
        f"{quoted(realm_id)}, {owner_anchor_guid}, {highest_owner_level_seen}, "
        f"{current_epoch}, {owner_generation});",
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
        wave_key = _require_digest(raw_wave.get("wave_idempotency_key"), f"waves[{ordinal}].wave_idempotency_key")
        planned_count = _require_int(raw_wave.get("planned_count"), f"waves[{ordinal}].planned_count", 0)
        initial_state = _require_token(raw_wave.get("initial_state"), f"waves[{ordinal}].initial_state")
        if initial_state not in {"planned", "authorized"}:
            raise PopulationPolicyError("wave initial_state must be planned or authorized")
        authorized_at = "CURRENT_TIMESTAMP(6)" if initial_state == "authorized" else "NULL"
        wave_values.append(
            f"({quoted(realm_id)}, {owner_level}, {quoted(wave_key)}, {planned_count}, 0, "
            f"{quoted(initial_state)}, {authorized_at}, 1)"
        )
    statements.extend(
        (
            "INSERT INTO population_director_wave_state ",
            "  (realm_id, owner_level, wave_idempotency_key, planned_count, admitted_count, "
            "state, authorized_at, generation) VALUES",
            "  " + ",\n  ".join(wave_values) + ";",
        )
    )

    identity_values: list[str] = []
    for ordinal, raw_identity in enumerate(identities):
        if not isinstance(raw_identity, dict):
            raise PopulationPolicyError(f"manifest_identities[{ordinal}] must be an object")
        identity = _require_token(
            raw_identity.get("manifest_identity"),
            f"manifest_identities[{ordinal}].manifest_identity",
        )
        account_name = _require_token(raw_identity.get("account_name"), f"identity {identity}.account_name")
        character_name = _require_token(raw_identity.get("character_name"), f"identity {identity}.character_name")
        faction = _require_token(raw_identity.get("faction"), f"identity {identity}.faction")
        role_intent = _require_token(raw_identity.get("role_intent"), f"identity {identity}.role_intent")
        spec_template = _require_token(raw_identity.get("spec_template"), f"identity {identity}.spec_template")
        starting_region = _require_token(raw_identity.get("starting_region"), f"identity {identity}.starting_region")
        wave_key = _require_digest(
            raw_identity.get("wave_idempotency_key"),
            f"identity {identity}.wave_idempotency_key",
        )
        owner_level = _require_int(raw_identity.get("owner_level"), f"identity {identity}.owner_level", 1, 80)
        wave_ordinal = _require_int(raw_identity.get("wave_ordinal"), f"identity {identity}.wave_ordinal", 1)
        account_size = _require_int(
            raw_identity.get("account_manifest_size"),
            f"identity {identity}.account_manifest_size",
            1,
            9,
        )
        race = _require_int(raw_identity.get("race"), f"identity {identity}.race", 1, 11)
        class_id = _require_int(raw_identity.get("class"), f"identity {identity}.class", 1, 11)
        eligible = int(_require_bool(raw_identity.get("admission_eligible"), f"identity {identity}.admission_eligible"))
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
                    quoted(realm_id), quoted(identity), quoted(digests["manifest_digest"]), quoted(wave_key),
                    str(owner_level), str(wave_ordinal), quoted(account_name), str(account_size),
                    quoted(character_name), quoted(faction), str(race), str(class_id), quoted(role_intent),
                    quoted(spec_template), quoted(starting_region), str(eligible),
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
        "starting_region, admission_eligible, gender, skin, face, hair_style, hair_color, "
        "facial_hair, outfit_id) VALUES"
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


def levels_to_authorize(
    highest_owner_level_seen: int,
    observed_owner_level: int,
    maximum_owner_level: int,
    global_phase_cap: int,
) -> tuple[int, ...]:
    """Return newly crossed phase-valid owner levels in durable queue order."""

    for name, value in (
        ("highest_owner_level_seen", highest_owner_level_seen),
        ("observed_owner_level", observed_owner_level),
        ("maximum_owner_level", maximum_owner_level),
        ("global_phase_cap", global_phase_cap),
    ):
        _require_int(value, name, 0 if name == "highest_owner_level_seen" else 1, 80)
    effective_observed = min(observed_owner_level, maximum_owner_level, global_phase_cap)
    if effective_observed <= highest_owner_level_seen:
        return ()
    return tuple(range(highest_owner_level_seen + 1, effective_observed + 1))


def compile_admission_reconciliation(
    raw: Mapping[str, object],
    snapshot: Mapping[str, object],
    manifest: Mapping[str, object],
    bindings: Mapping[str, object],
) -> dict[str, object]:
    """Compile one deterministic, bounded admission decision for a C++ runtime.

    This is an offline reconciliation oracle.  It neither advances the durable
    cursor nor creates an account: C++ must commit the owner high-water mark,
    wave state, database bindings, and audit record transactionally.
    """

    _require_exact_keys(
        raw,
        {
            "reconciliation_schema",
            "reconciliation_version",
            "observed_owner_level",
            "highest_owner_level_seen",
            "admissions_paused",
            "pause_reason",
            "available_creation_slots",
        },
        "admission reconciliation input",
    )
    reconciliation_schema = _require_int(
        raw.get("reconciliation_schema"),
        "reconciliation_schema",
        1,
        1,
    )
    reconciliation_version = _require_token(
        raw.get("reconciliation_version"),
        "reconciliation_version",
    )
    admissions_paused = _require_bool(raw.get("admissions_paused"), "admissions_paused")
    pause_reason_raw = raw.get("pause_reason")
    if admissions_paused:
        pause_reason: str | None = _require_token(pause_reason_raw, "pause_reason")
    elif pause_reason_raw is not None:
        raise PopulationPolicyError("pause_reason must be null when admissions are not paused")
    else:
        pause_reason = None
    available_creation_slots = _require_int(
        raw.get("available_creation_slots"),
        "available_creation_slots",
        0,
    )

    snapshot_digest = _verify_compiled_artifact(snapshot, "snapshot_digest", "snapshot")
    manifest_digest = _verify_compiled_artifact(manifest, "manifest_digest", "manifest")
    binding_digest = _verify_compiled_artifact(bindings, "binding_digest", "bindings")

    source_digests = snapshot.get("source_digests")
    if not isinstance(source_digests, dict):
        raise PopulationPolicyError("snapshot.source_digests must be an object")
    _require_exact_keys(
        source_digests,
        {"binding_digest", "manifest_digest", "policy_digest", "schedule_digest"},
        "snapshot.source_digests",
    )
    schedule_digest = _require_digest(
        source_digests.get("schedule_digest"),
        "snapshot.source_digests.schedule_digest",
    )
    policy_digest = _require_digest(
        source_digests.get("policy_digest"),
        "snapshot.source_digests.policy_digest",
    )
    if source_digests.get("manifest_digest") != manifest_digest:
        raise PopulationPolicyError("snapshot does not bind the supplied manifest")
    if source_digests.get("binding_digest") != binding_digest:
        raise PopulationPolicyError("snapshot does not bind the supplied database bindings")
    if bindings.get("manifest_digest") != manifest_digest:
        raise PopulationPolicyError("database bindings do not bind the supplied manifest")

    realm_id = _require_token(snapshot.get("realm_id"), "snapshot.realm_id")
    if manifest.get("realm_id") != realm_id:
        raise PopulationPolicyError("snapshot and manifest realm_id values do not match")
    owner_anchor_guid = _require_int(snapshot.get("owner_anchor_guid"), "owner_anchor_guid", 1)
    maximum_owner_level = _require_int(
        snapshot.get("maximum_owner_level"),
        "maximum_owner_level",
        1,
        80,
    )
    global_phase_cap = _require_int(snapshot.get("global_phase_cap"), "global_phase_cap", 1, 80)
    observed_owner_level = _require_int(
        raw.get("observed_owner_level"),
        "observed_owner_level",
        1,
        maximum_owner_level,
    )
    highest_owner_level_seen = _require_int(
        raw.get("highest_owner_level_seen"),
        "highest_owner_level_seen",
        0,
        maximum_owner_level,
    )
    if observed_owner_level > global_phase_cap:
        raise PopulationPolicyError("observed_owner_level exceeds the global phase cap")
    if highest_owner_level_seen > global_phase_cap:
        raise PopulationPolicyError("highest_owner_level_seen exceeds the global phase cap")

    capacity_limits = snapshot.get("capacity_limits")
    if not isinstance(capacity_limits, dict):
        raise PopulationPolicyError("snapshot.capacity_limits must be an object")
    creation_batch_size = _require_int(
        capacity_limits.get("creation_batch_size"),
        "snapshot.capacity_limits.creation_batch_size",
        1,
    )

    identities_raw = manifest.get("identities")
    binding_rows_raw = bindings.get("bindings")
    if not isinstance(identities_raw, list) or not isinstance(binding_rows_raw, list):
        raise PopulationPolicyError("manifest identities and database bindings must be lists")

    identities: dict[str, dict[str, object]] = {}
    identities_by_wave: dict[int, list[dict[str, object]]] = {}
    for ordinal, identity_raw in enumerate(identities_raw):
        if not isinstance(identity_raw, dict):
            raise PopulationPolicyError(f"manifest identities[{ordinal}] must be an object")
        identity = _require_token(
            identity_raw.get("manifest_identity"),
            f"manifest identities[{ordinal}].manifest_identity",
        )
        if identity in identities:
            raise PopulationPolicyError(f"duplicate manifest identity: {identity}")
        wave_level = _require_int(
            identity_raw.get("creation_wave_owner_level"),
            f"manifest identity {identity} creation_wave_owner_level",
            1,
            maximum_owner_level,
        )
        _require_int(
            identity_raw.get("wave_ordinal"),
            f"manifest identity {identity} wave_ordinal",
            1,
        )
        _require_bool(
            identity_raw.get("admission_eligible"),
            f"manifest identity {identity} admission_eligible",
        )
        identities[identity] = identity_raw
        identities_by_wave.setdefault(wave_level, []).append(identity_raw)

    for wave_level, rows in identities_by_wave.items():
        rows.sort(
            key=lambda item: (
                cast(int, item["wave_ordinal"]),
                cast(str, item["manifest_identity"]),
            )
        )
        actual_ordinals = [cast(int, item["wave_ordinal"]) for item in rows]
        if actual_ordinals != list(range(1, len(rows) + 1)):
            raise PopulationPolicyError(f"wave {wave_level} ordinals are not contiguous")

    bound_identities: set[str] = set()
    for ordinal, binding_raw in enumerate(binding_rows_raw):
        if not isinstance(binding_raw, dict):
            raise PopulationPolicyError(f"bindings[{ordinal}] must be an object")
        identity = _require_token(
            binding_raw.get("manifest_identity"),
            f"bindings[{ordinal}].manifest_identity",
        )
        if identity not in identities:
            raise PopulationPolicyError(f"binding references unknown identity: {identity}")
        if identity in bound_identities:
            raise PopulationPolicyError(f"duplicate bound identity: {identity}")
        bound_identities.add(identity)
        if not cast(bool, identities[identity]["admission_eligible"]):
            raise PopulationPolicyError(f"ineligible identity is already admitted: {identity}")

    newly_authorized_levels = levels_to_authorize(
        highest_owner_level_seen,
        observed_owner_level,
        maximum_owner_level,
        global_phase_cap,
    )
    resulting_high_water = max(highest_owner_level_seen, observed_owner_level)
    for identity in sorted(bound_identities):
        wave_level = cast(int, identities[identity]["creation_wave_owner_level"])
        if wave_level > resulting_high_water:
            raise PopulationPolicyError(f"binding for {identity} belongs to future wave {wave_level}")

    wave_states: list[dict[str, object]] = []
    pending_wave_levels: list[int] = []
    remaining_by_wave: dict[int, list[dict[str, object]]] = {}
    total_due = 0
    for wave_level in range(1, resulting_high_water + 1):
        wave_rows = identities_by_wave.get(wave_level, [])
        remaining = [item for item in wave_rows if cast(str, item["manifest_identity"]) not in bound_identities]
        ineligible = [
            cast(str, item["manifest_identity"]) for item in remaining if not cast(bool, item["admission_eligible"])
        ]
        admitted_count = len(wave_rows) - len(remaining)
        if not remaining:
            state = "active"
        elif ineligible:
            state = "blocked"
        elif admitted_count:
            state = "admitting"
        else:
            state = "authorized"
        if remaining:
            pending_wave_levels.append(wave_level)
            remaining_by_wave[wave_level] = remaining
            total_due += len(remaining)
        idempotency_payload = {
            "realm_id": realm_id,
            "owner_anchor_guid": owner_anchor_guid,
            "owner_level": wave_level,
            "schedule_digest": schedule_digest,
        }
        wave_states.append(
            {
                "owner_level": wave_level,
                "planned_count": len(wave_rows),
                "admitted_count": admitted_count,
                "remaining_count": len(remaining),
                "ineligible_remaining": ineligible,
                "state": state,
                "idempotency_key": digest(idempotency_payload),
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
            batch_limit = min(creation_batch_size, available_creation_slots)
            candidate_rows = [
                {
                    "manifest_identity": cast(str, item["manifest_identity"]),
                    "creation_wave_owner_level": cast(int, item["creation_wave_owner_level"]),
                    "wave_ordinal": cast(int, item["wave_ordinal"]),
                }
                for item in earliest_remaining[:batch_limit]
            ]

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
        "owner_anchor_guid": owner_anchor_guid,
        "owner_state": {
            "observed_owner_level": observed_owner_level,
            "previous_highest_owner_level_seen": highest_owner_level_seen,
            "resulting_highest_owner_level_seen": resulting_high_water,
            "newly_authorized_levels": list(newly_authorized_levels),
            "current_epoch": resulting_high_water,
        },
        "admission_control": {
            "admissions_paused": admissions_paused,
            "pause_reason": pause_reason,
            "available_creation_slots": available_creation_slots,
            "creation_batch_size": creation_batch_size,
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


RECOVERY_STATES = (
    "reserved",
    "account_pending",
    "account_verified",
    "character_pending",
    "character_verified",
    "rndbot_pending",
    "binding_verified",
    "active",
    "blocked_conflict",
)


def compile_admission_recovery(
    raw: Mapping[str, object],
    reconciliation: Mapping[str, object],
    manifest: Mapping[str, object],
) -> dict[str, object]:
    """Derive one fail-closed recovery action for one planned identity.

    The compiler performs no database work. It emits at most one local ledger
    transition or one external side effect so a C++ owner can read back facts
    between steps and resume safely after a crash.
    """

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
    recovery_schema = _require_int(raw.get("recovery_schema"), "recovery_schema", 1, 1)
    recovery_version = _require_token(raw.get("recovery_version"), "recovery_version")
    identity_name = _require_token(raw.get("manifest_identity"), "manifest_identity")
    reconciliation_digest = _verify_compiled_artifact(
        reconciliation,
        "reconciliation_digest",
        "reconciliation",
    )
    manifest_digest = _verify_compiled_artifact(manifest, "manifest_digest", "manifest")

    sources = reconciliation.get("source_digests")
    if not isinstance(sources, dict) or sources.get("manifest_digest") != manifest_digest:
        raise PopulationPolicyError("reconciliation does not bind the supplied manifest")
    snapshot_digest = _require_digest(sources.get("snapshot_digest"), "reconciliation snapshot_digest")
    realm_id = _require_token(reconciliation.get("realm_id"), "reconciliation.realm_id")
    if manifest.get("realm_id") != realm_id:
        raise PopulationPolicyError("reconciliation and manifest realm_id values do not match")

    identities_raw = manifest.get("identities")
    if not isinstance(identities_raw, list):
        raise PopulationPolicyError("manifest.identities must be a list")
    identity_rows = [
        item for item in identities_raw if isinstance(item, dict) and item.get("manifest_identity") == identity_name
    ]
    if len(identity_rows) != 1:
        raise PopulationPolicyError("manifest_identity must resolve to exactly one manifest row")
    identity = identity_rows[0]
    if identity.get("admission_eligible") is not True:
        raise PopulationPolicyError("manifest identity is not admission eligible")
    wave_level = _require_int(identity.get("creation_wave_owner_level"), "creation_wave_owner_level", 1, 80)
    wave_ordinal = _require_int(identity.get("wave_ordinal"), "wave_ordinal", 1)

    candidates = reconciliation.get("candidate_batch")
    if not isinstance(candidates, list):
        raise PopulationPolicyError("reconciliation.candidate_batch must be a list")
    is_candidate = any(
        isinstance(item, dict)
        and item.get("manifest_identity") == identity_name
        and item.get("creation_wave_owner_level") == wave_level
        and item.get("wave_ordinal") == wave_ordinal
        for item in candidates
    )

    wave_states = reconciliation.get("wave_states")
    if not isinstance(wave_states, list):
        raise PopulationPolicyError("reconciliation.wave_states must be a list")
    matching_waves = [item for item in wave_states if isinstance(item, dict) and item.get("owner_level") == wave_level]
    if len(matching_waves) != 1:
        raise PopulationPolicyError("identity wave must resolve to exactly one reconciliation wave")
    wave_key = _require_digest(matching_waves[0].get("idempotency_key"), "wave idempotency_key")

    expected = {
        "account_name": _require_token(identity.get("account_name"), "identity.account_name"),
        "account_manifest_size": sum(
            1
            for item in identities_raw
            if isinstance(item, dict) and item.get("account_name") == identity.get("account_name")
        ),
        "character_name": _require_token(identity.get("character_name"), "identity.character_name"),
        "race": _require_int(identity.get("race"), "identity.race", 1, 11),
        "class": _require_int(identity.get("class"), "identity.class", 1, 11),
        "appearance": identity.get("appearance"),
        "creation_wave_owner_level": wave_level,
        "wave_ordinal": wave_ordinal,
        "wave_idempotency_key": wave_key,
    }
    expected_appearance = expected["appearance"]
    if not isinstance(expected_appearance, dict):
        raise PopulationPolicyError("identity.appearance must be an object")
    _require_exact_keys(expected_appearance, set(APPEARANCE_FIELDS), "identity.appearance")
    if expected_appearance.get("outfit_id") != 0:
        raise PopulationPolicyError("identity.appearance.outfit_id must be 0")

    ledger_raw = raw.get("ledger")
    ledger: dict[str, object] | None
    if ledger_raw is None:
        ledger = None
        state = "absent"
        generation = 0
        ledger_account_id = None
        ledger_character_guid = None
    else:
        if not isinstance(ledger_raw, dict):
            raise PopulationPolicyError("ledger must be null or an object")
        _require_exact_keys(
            ledger_raw,
            {
                "state",
                "generation",
                "manifest_digest",
                "wave_idempotency_key",
                "expected_identity",
                "account_id",
                "character_guid",
            },
            "ledger",
        )
        state = _require_token(ledger_raw.get("state"), "ledger.state")
        if state not in RECOVERY_STATES:
            raise PopulationPolicyError("ledger.state is unsupported")
        generation = _require_int(ledger_raw.get("generation"), "ledger.generation", 1)
        if ledger_raw.get("manifest_digest") != manifest_digest:
            raise PopulationPolicyError("ledger manifest digest conflicts with the supplied manifest")
        if ledger_raw.get("wave_idempotency_key") != wave_key:
            raise PopulationPolicyError("ledger wave idempotency key conflicts with the reconciliation")
        if ledger_raw.get("expected_identity") != expected:
            raise PopulationPolicyError("ledger expected identity conflicts with the supplied manifest")
        ledger_account_id = ledger_raw.get("account_id")
        if ledger_account_id is not None:
            ledger_account_id = _require_int(ledger_account_id, "ledger.account_id", 1)
        ledger_character_guid = ledger_raw.get("character_guid")
        if ledger_character_guid is not None:
            ledger_character_guid = _require_int(ledger_character_guid, "ledger.character_guid", 1)
        ledger = {
            "state": state,
            "generation": generation,
            "manifest_digest": manifest_digest,
            "wave_idempotency_key": wave_key,
            "expected_identity": expected,
            "account_id": ledger_account_id,
            "character_guid": ledger_character_guid,
        }

    account_raw = raw.get("observed_account")
    account: dict[str, object] | None = None
    if account_raw is not None:
        if not isinstance(account_raw, dict):
            raise PopulationPolicyError("observed_account must be null or an object")
        _require_exact_keys(
            account_raw,
            {
                "account_id",
                "account_name",
                "character_database_numchars",
                "realmcharacters_row_present",
                "realmcharacters_numchars",
            },
            "observed_account",
        )
        realmcharacters_row_present = _require_bool(
            account_raw.get("realmcharacters_row_present"),
            "observed_account.realmcharacters_row_present",
        )
        realmcharacters_numchars_raw = account_raw.get("realmcharacters_numchars")
        realmcharacters_numchars: int | None
        if realmcharacters_numchars_raw is None:
            realmcharacters_numchars = None
        else:
            realmcharacters_numchars = _require_int(
                realmcharacters_numchars_raw,
                "observed_account.realmcharacters_numchars",
                0,
                255,
            )
        if realmcharacters_row_present != (realmcharacters_numchars is not None):
            raise PopulationPolicyError(
                "observed_account realmcharacters presence and numchars must agree"
            )
        account = {
            "account_id": _require_int(account_raw.get("account_id"), "observed_account.account_id", 1),
            "account_name": _require_token(account_raw.get("account_name"), "observed_account.account_name"),
            "character_database_numchars": _require_int(
                account_raw.get("character_database_numchars"),
                "observed_account.character_database_numchars",
                0,
                255,
            ),
            "realmcharacters_row_present": realmcharacters_row_present,
            "realmcharacters_numchars": realmcharacters_numchars,
        }

    character_raw = raw.get("observed_character")
    character: dict[str, object] | None = None
    if character_raw is not None:
        if not isinstance(character_raw, dict):
            raise PopulationPolicyError("observed_character must be null or an object")
        _require_exact_keys(
            character_raw,
            {
                "character_guid",
                "account_id",
                "character_name",
                "race",
                "class",
                "appearance",
                "level",
                "xp",
                "total_time",
                "level_time",
                "quest_state",
            },
            "observed_character",
        )
        observed_appearance_raw = character_raw.get("appearance")
        if not isinstance(observed_appearance_raw, dict):
            raise PopulationPolicyError("observed_character.appearance must be an object")
        _require_exact_keys(
            observed_appearance_raw,
            set(PERSISTED_APPEARANCE_FIELDS),
            "observed_character.appearance",
        )
        observed_appearance: dict[str, int] = {}
        for field in sorted(PERSISTED_APPEARANCE_FIELDS):
            observed_appearance[field] = _require_int(
                observed_appearance_raw.get(field),
                f"observed_character.appearance.{field}",
                0,
                1 if field == "gender" else 255,
            )
        quest_state_raw = character_raw.get("quest_state")
        if not isinstance(quest_state_raw, dict):
            raise PopulationPolicyError("observed_character.quest_state must be an object")
        _require_exact_keys(quest_state_raw, set(QUEST_STATE_FIELDS), "observed_character.quest_state")
        quest_state = {
            field: _require_int(
                quest_state_raw.get(field),
                f"observed_character.quest_state.{field}",
                0,
            )
            for field in sorted(QUEST_STATE_FIELDS)
        }
        character = {
            "character_guid": _require_int(character_raw.get("character_guid"), "observed_character.character_guid", 1),
            "account_id": _require_int(character_raw.get("account_id"), "observed_character.account_id", 1),
            "character_name": _require_token(character_raw.get("character_name"), "observed_character.character_name"),
            "race": _require_int(character_raw.get("race"), "observed_character.race", 1, 11),
            "class": _require_int(character_raw.get("class"), "observed_character.class", 1, 11),
            "appearance": observed_appearance,
            "level": _require_int(character_raw.get("level"), "observed_character.level", 1, 80),
            "xp": _require_int(character_raw.get("xp"), "observed_character.xp", 0),
            "total_time": _require_int(character_raw.get("total_time"), "observed_character.total_time", 0),
            "level_time": _require_int(character_raw.get("level_time"), "observed_character.level_time", 0),
            "quest_state": quest_state,
        }

    rndbot_raw = raw.get("observed_rndbot")
    rndbot: dict[str, object] | None = None
    if rndbot_raw is not None:
        if not isinstance(rndbot_raw, dict):
            raise PopulationPolicyError("observed_rndbot must be null or an object")
        _require_exact_keys(rndbot_raw, {"account_id", "account_type"}, "observed_rndbot")
        rndbot = {
            "account_id": _require_int(rndbot_raw.get("account_id"), "observed_rndbot.account_id", 1),
            "account_type": _require_int(rndbot_raw.get("account_type"), "observed_rndbot.account_type", 0, 2),
        }

    conflicts: list[str] = []
    if account is not None and account["account_name"] != expected["account_name"]:
        conflicts.append("account_name_conflict")
    if character is not None:
        expected_persisted_appearance = {
            field: expected_appearance[field] for field in sorted(PERSISTED_APPEARANCE_FIELDS)
        }
        if (
            character["character_name"] != expected["character_name"]
            or character["race"] != expected["race"]
            or character["class"] != expected["class"]
            or character["appearance"] != expected_persisted_appearance
            or character["level"] != 1
            or character["xp"] != 0
            or character["total_time"] != 0
            or character["level_time"] != 0
            or any(cast(dict[str, int], character["quest_state"]).values())
        ):
            conflicts.append("character_identity_conflict")
        if account is None or character["account_id"] != account["account_id"]:
            conflicts.append("character_account_conflict")
    if rndbot is not None:
        if account is None or rndbot["account_id"] != account["account_id"]:
            conflicts.append("rndbot_account_conflict")
        if rndbot["account_type"] != 1:
            conflicts.append("rndbot_type_conflict")
    if ledger_account_id is not None and (account is None or account["account_id"] != ledger_account_id):
        conflicts.append("ledger_account_conflict")
    if ledger_character_guid is not None and (
        character is None or character["character_guid"] != ledger_character_guid
    ):
        conflicts.append("ledger_character_conflict")
    if account is not None:
        row_present = cast(bool, account["realmcharacters_row_present"])
        numchars = cast(int | None, account["realmcharacters_numchars"])
        character_database_numchars = cast(int, account["character_database_numchars"])
        if character_database_numchars > cast(int, expected["account_manifest_size"]):
            conflicts.append("account_character_count_exceeds_manifest")
        if state not in {"absent", "reserved", "account_pending"} and not row_present:
            conflicts.append("realmcharacters_row_missing")
        if (
            row_present
            and numchars != character_database_numchars
            and state not in {"account_pending", "character_pending"}
        ):
            conflicts.append("realmcharacters_count_conflict")

    def decision(
        action: str,
        to_state: str,
        domain: str,
        reason: str,
        *,
        read_back: bool = False,
        mutate_ledger: bool = True,
    ) -> dict[str, object]:
        resulting_generation = generation + 1 if mutate_ledger else generation
        payload: dict[str, object] = {
            "recovery_schema": recovery_schema,
            "recovery_version": recovery_version,
            "realm_id": realm_id,
            "manifest_identity": identity_name,
            "source_digests": {
                "manifest_digest": manifest_digest,
                "reconciliation_digest": reconciliation_digest,
                "snapshot_digest": snapshot_digest,
            },
            "expected_identity": expected,
            "ledger_observation": ledger,
            "external_observation": {
                "account": account,
                "character": character,
                "rndbot": rndbot,
            },
            "decision": {
                "action": action,
                "from_state": state,
                "to_state": to_state,
                "effect_domain": domain,
                "reason": reason,
                "expected_generation": generation,
                "resulting_generation": resulting_generation,
                "read_back_required": read_back,
            },
            "runtime_commit_required": action not in {"noop_active", "noop_blocked"},
        }
        return {**payload, "recovery_digest": digest(payload)}

    if conflicts:
        return decision("block_conflict", "blocked_conflict", "director_ledger", sorted(set(conflicts))[0])
    if state not in {"active", "blocked_conflict"} and not is_candidate:
        raise PopulationPolicyError("non-active identity is not in the reconciliation candidate batch")
    if state == "absent":
        if character is not None or (rndbot is not None and account is None):
            return decision("block_conflict", "blocked_conflict", "director_ledger", "unreserved_external_state")
        return decision("reserve_identity", "reserved", "director_ledger", "legal_transition")
    if state == "blocked_conflict":
        return decision("noop_blocked", state, "none", "operator_repair_required", mutate_ledger=False)
    if state == "reserved":
        if character is not None or (rndbot is not None and account is None):
            return decision("block_conflict", "blocked_conflict", "director_ledger", "external_state_before_intent")
        return decision("mark_account_pending", "account_pending", "director_ledger", "legal_transition")
    if state == "account_pending":
        if character is not None:
            return decision("block_conflict", "blocked_conflict", "director_ledger", "external_state_ahead_of_ledger")
        if account is None:
            return decision(
                "create_account",
                state,
                "login_database",
                "expected_account_absent",
                read_back=True,
                mutate_ledger=False,
            )
        if not cast(bool, account["realmcharacters_row_present"]):
            return decision(
                "initialize_realmcharacters",
                state,
                "login_database",
                "current_realm_row_absent",
                read_back=True,
                mutate_ledger=False,
            )
        if account["realmcharacters_numchars"] != account["character_database_numchars"]:
            return decision(
                "refresh_realmcharacters_count",
                state,
                "login_database",
                "current_realm_count_stale",
                read_back=True,
                mutate_ledger=False,
            )
        return decision("mark_account_verified", "account_verified", "director_ledger", "exact_account_observed")
    if account is None:
        return decision("block_conflict", "blocked_conflict", "director_ledger", "verified_account_missing")
    if state == "account_verified":
        if character is not None:
            return decision("block_conflict", "blocked_conflict", "director_ledger", "external_state_before_intent")
        return decision("mark_character_pending", "character_pending", "director_ledger", "legal_transition")
    if state == "character_pending":
        if character is None:
            return decision(
                "create_character",
                state,
                "character_database",
                "expected_character_absent",
                read_back=True,
                mutate_ledger=False,
            )
        if account["realmcharacters_numchars"] != account["character_database_numchars"]:
            return decision(
                "refresh_realmcharacters_count",
                state,
                "login_database",
                "current_realm_count_stale",
                read_back=True,
                mutate_ledger=False,
            )
        return decision("mark_character_verified", "character_verified", "director_ledger", "exact_character_observed")
    if character is None:
        return decision("block_conflict", "blocked_conflict", "director_ledger", "verified_character_missing")
    if state == "character_verified":
        return decision("mark_rndbot_pending", "rndbot_pending", "director_ledger", "legal_transition")
    if state == "rndbot_pending":
        if rndbot is None:
            return decision(
                "install_rndbot_classification",
                state,
                "playerbots_database",
                "expected_rndbot_classification_absent",
                read_back=True,
                mutate_ledger=False,
            )
        return decision("mark_binding_verified", "binding_verified", "director_ledger", "exact_rndbot_observed")
    if rndbot is None:
        return decision("block_conflict", "blocked_conflict", "director_ledger", "verified_rndbot_missing")
    if state == "binding_verified":
        return decision("activate_identity", "active", "director_ledger", "all_external_facts_verified")
    return decision("noop_active", "active", "none", "identity_already_active", mutate_ledger=False)


def rank_and_assign(
    records: Sequence[ProgressionRecord],
    ordered_capacities: Sequence[tuple[str, int]],
) -> tuple[tuple[int, int, str], ...]:
    """Rank earned state and slice it into dynamic bands deterministically."""

    if not ordered_capacities:
        raise PopulationPolicyError("ordered_capacities must not be empty")
    seen_names: set[str] = set()
    capacity_total = 0
    for name, capacity in ordered_capacities:
        _require_token(name, "band name")
        if name in seen_names:
            raise PopulationPolicyError(f"duplicate capacity band: {name}")
        seen_names.add(name)
        capacity_total += _require_int(capacity, f"capacity for {name}", 0)

    guids: set[int] = set()
    for record in records:
        _require_int(record.character_guid, "character_guid", 1)
        _require_int(record.level, "level", 1, 80)
        _require_int(record.xp_within_level, "xp_within_level", 0)
        _require_int(record.threshold_reached_at, "threshold_reached_at", 0)
        if record.character_guid in guids:
            raise PopulationPolicyError(f"duplicate character GUID: {record.character_guid}")
        guids.add(record.character_guid)
    if capacity_total < len(records):
        raise PopulationPolicyError("band capacity is smaller than the admitted record count")

    ranked = sorted(
        records,
        key=lambda record: (
            -record.level,
            -record.xp_within_level,
            record.threshold_reached_at,
            record.character_guid,
        ),
    )
    assignments: list[tuple[int, str]] = []
    cursor = 0
    for band, capacity in ordered_capacities:
        for record in ranked[cursor : cursor + capacity]:
            assignments.append((record.character_guid, band))
        cursor += capacity
        if cursor >= len(ranked):
            break

    band_by_guid = dict(assignments)
    return tuple(
        (record.character_guid, rank, band_by_guid[record.character_guid]) for rank, record in enumerate(ranked, 1)
    )
