from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import cast

import pytest

from realm_scripts.population_capacity_policy import (
    classify_capacity_observation,
    compile_capacity_policy,
)
from realm_scripts.population_policy import PopulationPolicyError, digest


def policy_input() -> dict[str, object]:
    return {
        "policy_schema": 1,
        "policy_version": "candidate-home-server-v1",
        "load_ladder": [0, 50, 100, 200, 400, 600, 800],
        "stage_minimum_observation_seconds": {
            "0": 1800,
            "50": 3600,
            "100": 3600,
            "200": 5400,
            "400": 7200,
            "600": 14400,
            "800": 86400,
        },
        "pause_confirmation_windows": 2,
        "recovery_confirmation_windows": 3,
        "thresholds": {
            "cpu_psi_some_avg60_basis_points": {"warning": 1000, "pause": 2000, "backoff": 3500},
            "host_memory_available_mib": {"warning": 6144, "pause": 4096, "backoff": 3072},
            "login_p95_ms": {"warning": 1000, "pause": 3000, "backoff": 10000},
            "mysql_query_p95_ms": {"warning": 20, "pause": 50, "backoff": 100},
            "mysql_threads_running": {"warning": 4, "pause": 8, "backoff": 16},
            "pathfinding_stuck_per_1000": {"warning": 5, "pause": 10, "backoff": 25},
            "swap_in_mib_per_minute": {"warning": 1, "pause": 16, "backoff": 64},
            "world_update_p95_ms": {"warning": 50, "pause": 100, "backoff": 200},
            "worldserver_cgroup_memory_mib": {"warning": 4608, "pause": 5120, "backoff": 5632},
            "worst_map_update_p95_ms": {"warning": 50, "pause": 100, "backoff": 200},
        },
    }


def observation_input(**metric_overrides: int) -> dict[str, object]:
    metrics = {
        "cpu_psi_some_avg60_basis_points": 200,
        "fatal_error_count": 0,
        "host_memory_available_mib": 8192,
        "login_p95_ms": 100,
        "mysql_query_p95_ms": 5,
        "mysql_threads_running": 1,
        "observation_window_seconds": 3600,
        "online_ambient_sessions": 50,
        "pathfinding_stuck_per_1000": 0,
        "permanent_roster_count": 329,
        "swap_in_mib_per_minute": 0,
        "world_update_p95_ms": 20,
        "worldserver_cgroup_memory_mib": 4096,
        "worst_map_update_p95_ms": 20,
    }
    metrics.update(metric_overrides)
    return {
        "observation_schema": 1,
        "observation_id": "stage-50-window-3",
        "ladder_stage": 50,
        "consecutive_pause_windows": 0,
        "consecutive_recovery_windows": 3,
        "metrics": metrics,
    }


def test_policy_compiles_strict_ladder_thresholds_and_semantics() -> None:
    result = compile_capacity_policy(policy_input())
    assert result["policy_digest"] == digest(
        {key: value for key, value in result.items() if key != "policy_digest"}
    )
    semantics = cast(dict[str, object], result["semantics"])
    assert semantics["runtime_authority"] == "native_cpp"
    assert semantics["permanent_roster_is_not_online_cap"] is True


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda raw: cast(list[int], raw["load_ladder"]).__setitem__(0, 1), "start at zero"),
        (lambda raw: cast(list[int], raw["load_ladder"]).__setitem__(2, 50), "strictly increasing"),
        (
            lambda raw: cast(dict[str, dict[str, int]], raw["thresholds"])[
                "world_update_p95_ms"
            ].__setitem__("pause", 50),
            "strictly increase",
        ),
        (
            lambda raw: cast(dict[str, dict[str, int]], raw["thresholds"])[
                "host_memory_available_mib"
            ].__setitem__("pause", 7000),
            "strictly decrease",
        ),
    ],
)
def test_policy_rejects_invalid_ladder_or_threshold_order(
    mutator: Callable[[dict[str, object]], None], message: str
) -> None:
    raw = policy_input()
    mutator(raw)
    with pytest.raises(PopulationPolicyError, match=message):
        compile_capacity_policy(raw)


def test_normal_observation_is_eligible_only_after_dwell_and_recovery() -> None:
    policy = compile_capacity_policy(policy_input())
    result = classify_capacity_observation(observation_input(), policy)
    assert result["decision"] == "normal"
    assert result["eligible_to_advance"] is True

    short = observation_input(observation_window_seconds=3599)
    assert classify_capacity_observation(short, policy)["eligible_to_advance"] is False


def test_warning_pause_confirmation_and_backoff_are_fail_closed() -> None:
    policy = compile_capacity_policy(policy_input())
    warning = classify_capacity_observation(observation_input(world_update_p95_ms=50), policy)
    assert warning["decision"] == "warning"
    assert warning["eligible_to_advance"] is False

    pause_raw = observation_input(world_update_p95_ms=100)
    pending = classify_capacity_observation(pause_raw, policy)
    assert pending["decision"] == "warning_pending_pause_confirmation"
    pause_raw["consecutive_pause_windows"] = 2
    paused = classify_capacity_observation(pause_raw, policy)
    assert paused["decision"] == "pause"

    backoff = classify_capacity_observation(observation_input(fatal_error_count=1), policy)
    assert backoff["decision"] == "backoff"
    assert backoff["fatal_error"] is True


def test_minimum_memory_threshold_and_digest_tamper_are_enforced() -> None:
    policy = compile_capacity_policy(policy_input())
    paused = observation_input(host_memory_available_mib=4096)
    paused["consecutive_pause_windows"] = 2
    assert classify_capacity_observation(paused, policy)["decision"] == "pause"

    tampered = deepcopy(policy)
    cast(dict[str, dict[str, int]], tampered["thresholds"])["world_update_p95_ms"]["pause"] = 999
    with pytest.raises(PopulationPolicyError, match="digest mismatch"):
        classify_capacity_observation(observation_input(), tampered)


def test_observation_rejects_online_count_outside_ladder_or_roster() -> None:
    policy = compile_capacity_policy(policy_input())
    wrong_stage = observation_input(online_ambient_sessions=49)
    with pytest.raises(PopulationPolicyError, match="equal the selected ladder stage"):
        classify_capacity_observation(wrong_stage, policy)

    over_roster = observation_input(permanent_roster_count=40)
    with pytest.raises(PopulationPolicyError, match="exceed the permanent roster"):
        classify_capacity_observation(over_roster, policy)
