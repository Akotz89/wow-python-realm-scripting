from __future__ import annotations

from copy import deepcopy
from typing import cast

import pytest

from realm_scripts.population_band_policy import compile_band_policy, resolve_band_transition
from realm_scripts.population_policy import PopulationPolicyError, digest


def policy_input() -> dict[str, object]:
    return {
        "policy_schema": 1,
        "policy_version": "owner-relative-candidate-v1",
        "maximum_owner_level": 80,
        "global_phase_cap": 19,
        "transition_dwell_seconds": 1800,
        "bands": [
            {"name": "pioneer", "share": "0.05", "owner_level_offset": 0},
            {"name": "scout", "share": "0.15", "owner_level_offset": 1},
            {"name": "forward", "share": "0.25", "owner_level_offset": 2},
            {"name": "main", "share": "0.35", "owner_level_offset": 4},
            {"name": "trailing", "share": "0.20", "owner_level_offset": 7},
        ],
    }


def transition_input(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "transition_schema": 1,
        "current_band": "pioneer",
        "candidate_band": "scout",
        "candidate_age_seconds": 1799,
        "owner_highest_earned_level": 13,
        "character_earned_level": 13,
    }
    value.update(overrides)
    return value


def test_candidate_policy_compiles_exact_shares_offsets_and_phase_clamp() -> None:
    result = compile_band_policy(policy_input())
    assert result["policy_digest"] == digest(
        {key: value for key, value in result.items() if key != "policy_digest"}
    )
    epochs = cast(list[dict[str, object]], result["ceiling_epochs"])
    level_13 = cast(dict[str, int], epochs[12]["effective_ceilings"])
    assert level_13 == {"pioneer": 13, "scout": 12, "forward": 11, "main": 9, "trailing": 6}
    level_80 = cast(dict[str, int], epochs[79]["effective_ceilings"])
    assert set(level_80.values()) == {19}


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ((1, "share", "0.14"), "sum exactly to 1"),
        ((0, "owner_level_offset", 1), "highest band"),
        ((2, "owner_level_offset", 0), "non-decreasing"),
    ],
)
def test_policy_rejects_invalid_shares_or_offsets(
    mutation: tuple[int, str, object], message: str
) -> None:
    raw = policy_input()
    bands = cast(list[dict[str, object]], raw["bands"])
    index, key, value = mutation
    bands[index][key] = value
    with pytest.raises(PopulationPolicyError, match=message):
        compile_band_policy(raw)


def test_pending_demotion_enforces_lower_ceiling_without_delevel() -> None:
    policy = compile_band_policy(policy_input())
    pending = resolve_band_transition(transition_input(), policy)
    assert pending["membership_band"] == "pioneer"
    assert pending["enforcement_band"] == "scout"
    assert pending["effective_ceiling"] == 12
    assert pending["progression_hold_required"] is True
    assert pending["delevel_required"] is False

    committed = resolve_band_transition(transition_input(candidate_age_seconds=1800), policy)
    assert committed["membership_band"] == "scout"
    assert committed["transition_committed"] is True


def test_pending_promotion_retains_current_lower_ceiling_until_dwell() -> None:
    policy = compile_band_policy(policy_input())
    pending = resolve_band_transition(
        transition_input(
            current_band="main",
            candidate_band="forward",
            character_earned_level=9,
        ),
        policy,
    )
    assert pending["membership_band"] == "main"
    assert pending["enforcement_band"] == "main"
    assert pending["effective_ceiling"] == 9


def test_transition_rejects_tampered_policy_or_unknown_band() -> None:
    policy = compile_band_policy(policy_input())
    tampered = deepcopy(policy)
    tampered["transition_dwell_seconds"] = 1
    with pytest.raises(PopulationPolicyError, match="digest mismatch"):
        resolve_band_transition(transition_input(), tampered)
    with pytest.raises(PopulationPolicyError, match="unknown band"):
        resolve_band_transition(transition_input(candidate_band="fixed_party"), policy)
