"""Deterministic owner-relative band policy for the native population director.

This compiler is offline policy evidence. The future native C++ director owns
ranking, transition timers, cached enforcement, durable audit, and hot-path
quest/XP veto decisions.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from .population_policy import (
    PopulationPolicyError,
    _decimal_text,
    _require_decimal,
    _require_exact_keys,
    _require_int,
    _require_token,
    digest,
)


def _effective_ceiling(owner_level: int, owner_offset: int, global_phase_cap: int) -> int:
    return max(1, min(global_phase_cap, owner_level - owner_offset))


def compile_band_policy(raw: Mapping[str, object]) -> dict[str, object]:
    """Validate and digest an owner-relative, earned-state band policy."""

    _require_exact_keys(
        raw,
        {
            "policy_schema",
            "policy_version",
            "maximum_owner_level",
            "global_phase_cap",
            "transition_dwell_seconds",
            "bands",
        },
        "band policy",
    )
    if _require_int(raw.get("policy_schema"), "policy_schema", 1, 1) != 1:
        raise AssertionError("band policy schema validation drift")
    policy_version = _require_token(raw.get("policy_version"), "policy_version")
    maximum_owner_level = _require_int(
        raw.get("maximum_owner_level"), "maximum_owner_level", 1, 80
    )
    global_phase_cap = _require_int(raw.get("global_phase_cap"), "global_phase_cap", 1, 80)
    transition_dwell_seconds = _require_int(
        raw.get("transition_dwell_seconds"), "transition_dwell_seconds", 1
    )

    bands_raw = raw.get("bands")
    if not isinstance(bands_raw, list) or not bands_raw:
        raise PopulationPolicyError("bands must be a non-empty ordered list")
    bands: list[dict[str, object]] = []
    names: set[str] = set()
    total_share = Decimal(0)
    previous_offset = -1
    for index, band_raw in enumerate(bands_raw):
        if not isinstance(band_raw, dict):
            raise PopulationPolicyError(f"bands[{index}] must be an object")
        _require_exact_keys(
            band_raw, {"name", "share", "owner_level_offset"}, f"bands[{index}]"
        )
        name = _require_token(band_raw.get("name"), f"bands[{index}].name")
        if name in names:
            raise PopulationPolicyError(f"duplicate band name: {name}")
        names.add(name)
        share = _require_decimal(band_raw.get("share"), f"bands[{index}].share")
        if not Decimal(0) < share < Decimal(1):
            raise PopulationPolicyError(f"bands[{index}].share must satisfy 0 < share < 1")
        owner_offset = _require_int(
            band_raw.get("owner_level_offset"), f"bands[{index}].owner_level_offset", 0
        )
        if owner_offset < previous_offset:
            raise PopulationPolicyError("band owner-level offsets must be non-decreasing")
        previous_offset = owner_offset
        total_share += share
        bands.append(
            {
                "name": name,
                "share": _decimal_text(share),
                "owner_level_offset": owner_offset,
            }
        )
    if total_share != Decimal(1):
        raise PopulationPolicyError("band shares must sum exactly to 1")
    if bands[0]["owner_level_offset"] != 0:
        raise PopulationPolicyError("highest band must have owner-level offset zero")

    ceiling_epochs = [
        {
            "owner_level": owner_level,
            "effective_ceilings": {
                str(band["name"]): _effective_ceiling(
                    owner_level,
                    _require_int(band["owner_level_offset"], "compiled owner_level_offset", 0),
                    global_phase_cap,
                )
                for band in bands
            },
        }
        for owner_level in range(1, maximum_owner_level + 1)
    ]
    payload: dict[str, object] = {
        "policy_schema": 1,
        "policy_version": policy_version,
        "maximum_owner_level": maximum_owner_level,
        "global_phase_cap": global_phase_cap,
        "transition_dwell_seconds": transition_dwell_seconds,
        "bands": bands,
        "ceiling_rule": "max(1,min(global_phase_cap,owner_highest_earned_level-owner_level_offset))",
        "transition_rule": {
            "promotion_pending": "retain_current_ceiling_until_dwell",
            "demotion_pending": "enforce_lower_candidate_ceiling_immediately",
            "commit_membership_after_dwell": True,
            "delevel_forbidden": True,
            "runtime_authority": "native_cpp",
        },
        "ceiling_epochs": ceiling_epochs,
    }
    return {**payload, "policy_digest": digest(payload)}


def resolve_band_transition(
    raw: Mapping[str, object], policy: Mapping[str, object]
) -> dict[str, object]:
    """Resolve one transition observation with conservative pending enforcement."""

    supplied_digest = policy.get("policy_digest")
    if not isinstance(supplied_digest, str):
        raise PopulationPolicyError("compiled band policy is missing policy_digest")
    payload = {key: value for key, value in policy.items() if key != "policy_digest"}
    if digest(payload) != supplied_digest:
        raise PopulationPolicyError("compiled band policy digest mismatch")

    _require_exact_keys(
        raw,
        {
            "transition_schema",
            "current_band",
            "candidate_band",
            "candidate_age_seconds",
            "owner_highest_earned_level",
            "character_earned_level",
        },
        "band transition",
    )
    if _require_int(raw.get("transition_schema"), "transition_schema", 1, 1) != 1:
        raise AssertionError("band transition schema validation drift")
    current_band = _require_token(raw.get("current_band"), "current_band")
    candidate_band = _require_token(raw.get("candidate_band"), "candidate_band")
    candidate_age_seconds = _require_int(
        raw.get("candidate_age_seconds"), "candidate_age_seconds", 0
    )
    owner_level = _require_int(
        raw.get("owner_highest_earned_level"), "owner_highest_earned_level", 1
    )
    character_level = _require_int(raw.get("character_earned_level"), "character_earned_level", 1)

    bands_raw = policy.get("bands")
    if not isinstance(bands_raw, list):
        raise PopulationPolicyError("compiled band policy bands are missing")
    offsets: dict[str, int] = {}
    order: dict[str, int] = {}
    for index, band in enumerate(bands_raw):
        if not isinstance(band, dict):
            raise PopulationPolicyError("compiled band policy contains an invalid band")
        name = _require_token(band.get("name"), f"bands[{index}].name")
        offsets[name] = _require_int(
            band.get("owner_level_offset"), f"bands[{index}].owner_level_offset", 0
        )
        order[name] = index
    if current_band not in offsets or candidate_band not in offsets:
        raise PopulationPolicyError("transition references an unknown band")

    dwell = _require_int(policy.get("transition_dwell_seconds"), "transition_dwell_seconds", 1)
    global_phase_cap = _require_int(policy.get("global_phase_cap"), "global_phase_cap", 1)
    committed = current_band == candidate_band or candidate_age_seconds >= dwell
    membership_band = candidate_band if committed else current_band
    enforcement_band = (
        membership_band
        if committed or order[candidate_band] < order[current_band]
        else candidate_band
    )
    ceiling = _effective_ceiling(owner_level, offsets[enforcement_band], global_phase_cap)
    return {
        "transition_schema": 1,
        "policy_digest": supplied_digest,
        "membership_band": membership_band,
        "enforcement_band": enforcement_band,
        "effective_ceiling": ceiling,
        "transition_committed": committed,
        "progression_hold_required": character_level >= ceiling,
        "delevel_required": False,
    }
