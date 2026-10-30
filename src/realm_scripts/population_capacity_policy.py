"""Deterministic offline capacity policy and observation classifier.

This module does not sample the host or control Playerbots. It validates a
reviewed threshold policy and classifies one already-measured observation for
the future native population director. Runtime sampling, hysteresis state,
session backoff, and admission holds remain authoritative C++ work.
"""

from __future__ import annotations

from collections.abc import Mapping

from .population_policy import (
    PopulationPolicyError,
    _require_exact_keys,
    _require_int,
    _require_token,
    digest,
)

MAXIMUM_METRICS = (
    "cpu_psi_some_avg60_basis_points",
    "login_p95_ms",
    "mysql_query_p95_ms",
    "mysql_threads_running",
    "pathfinding_stuck_per_1000",
    "swap_in_mib_per_minute",
    "world_update_p95_ms",
    "worldserver_cgroup_memory_mib",
    "worst_map_update_p95_ms",
)

MINIMUM_METRICS = ("host_memory_available_mib",)

OBSERVATION_FIELDS = {
    *MAXIMUM_METRICS,
    *MINIMUM_METRICS,
    "fatal_error_count",
    "observation_window_seconds",
    "online_ambient_sessions",
    "permanent_roster_count",
}


def _require_threshold_triplet(raw: object, name: str, *, minimum: bool) -> dict[str, int]:
    if not isinstance(raw, dict):
        raise PopulationPolicyError(f"threshold {name} must be an object")
    _require_exact_keys(raw, {"warning", "pause", "backoff"}, f"threshold {name}")
    result = {
        level: _require_int(raw[level], f"thresholds.{name}.{level}", 0)
        for level in ("warning", "pause", "backoff")
    }
    ordered = (result["warning"], result["pause"], result["backoff"])
    if minimum:
        if not ordered[0] > ordered[1] > ordered[2]:
            raise PopulationPolicyError(
                f"minimum threshold {name} must strictly decrease from warning to pause to backoff"
            )
    elif not ordered[0] < ordered[1] < ordered[2]:
        raise PopulationPolicyError(
            f"maximum threshold {name} must strictly increase from warning to pause to backoff"
        )
    return result


def compile_capacity_policy(raw: Mapping[str, object]) -> dict[str, object]:
    """Validate and digest a threshold policy without inventing runtime values."""

    _require_exact_keys(
        raw,
        {
            "policy_schema",
            "policy_version",
            "load_ladder",
            "stage_minimum_observation_seconds",
            "pause_confirmation_windows",
            "recovery_confirmation_windows",
            "thresholds",
        },
        "capacity policy",
    )
    if _require_int(raw.get("policy_schema"), "policy_schema", 1, 1) != 1:
        raise AssertionError("capacity policy schema validation drift")
    policy_version = _require_token(raw.get("policy_version"), "policy_version")
    pause_confirmation_windows = _require_int(
        raw.get("pause_confirmation_windows"), "pause_confirmation_windows", 1
    )
    recovery_confirmation_windows = _require_int(
        raw.get("recovery_confirmation_windows"), "recovery_confirmation_windows", 1
    )

    ladder_raw = raw.get("load_ladder")
    if not isinstance(ladder_raw, list) or len(ladder_raw) < 2:
        raise PopulationPolicyError("load_ladder must contain baseline zero and at least one positive stage")
    load_ladder = [
        _require_int(value, f"load_ladder[{index}]", 0)
        for index, value in enumerate(ladder_raw)
    ]
    if load_ladder[0] != 0:
        raise PopulationPolicyError("load_ladder must start at zero")
    if any(
        current <= previous
        for previous, current in zip(load_ladder, load_ladder[1:], strict=False)
    ):
        raise PopulationPolicyError("load_ladder must be strictly increasing")
    stage_minimum_raw = raw.get("stage_minimum_observation_seconds")
    if not isinstance(stage_minimum_raw, dict):
        raise PopulationPolicyError("stage_minimum_observation_seconds must be an object")
    expected_stage_keys = {str(stage) for stage in load_ladder}
    _require_exact_keys(
        stage_minimum_raw,
        expected_stage_keys,
        "stage_minimum_observation_seconds",
    )
    stage_minimum_observation_seconds = {
        str(stage): _require_int(
            stage_minimum_raw[str(stage)],
            f"stage_minimum_observation_seconds.{stage}",
            60,
        )
        for stage in load_ladder
    }

    thresholds_raw = raw.get("thresholds")
    if not isinstance(thresholds_raw, dict):
        raise PopulationPolicyError("thresholds must be an object")
    threshold_fields = {*MAXIMUM_METRICS, *MINIMUM_METRICS}
    _require_exact_keys(thresholds_raw, threshold_fields, "thresholds")
    thresholds: dict[str, dict[str, int]] = {}
    for name in sorted(threshold_fields):
        thresholds[name] = _require_threshold_triplet(
            thresholds_raw[name], name, minimum=name in MINIMUM_METRICS
        )

    payload: dict[str, object] = {
        "policy_schema": 1,
        "policy_version": policy_version,
        "load_ladder": load_ladder,
        "stage_minimum_observation_seconds": stage_minimum_observation_seconds,
        "pause_confirmation_windows": pause_confirmation_windows,
        "recovery_confirmation_windows": recovery_confirmation_windows,
        "thresholds": thresholds,
        "semantics": {
            "permanent_roster_is_not_online_cap": True,
            "pressure_pauses_future_admission": True,
            "pressure_never_deletes_or_rebuilds_identity": True,
            "runtime_authority": "native_cpp",
        },
    }
    return {**payload, "policy_digest": digest(payload)}


def _metric_level(value: int, thresholds: Mapping[str, int], *, minimum: bool) -> str:
    if minimum:
        if value <= thresholds["backoff"]:
            return "backoff"
        if value <= thresholds["pause"]:
            return "pause"
        if value <= thresholds["warning"]:
            return "warning"
        return "normal"
    if value >= thresholds["backoff"]:
        return "backoff"
    if value >= thresholds["pause"]:
        return "pause"
    if value >= thresholds["warning"]:
        return "warning"
    return "normal"


def classify_capacity_observation(
    raw: Mapping[str, object],
    policy: Mapping[str, object],
) -> dict[str, object]:
    """Classify one observation against a compiled, digest-verified policy."""

    supplied_digest = policy.get("policy_digest")
    if not isinstance(supplied_digest, str):
        raise PopulationPolicyError("compiled capacity policy is missing policy_digest")
    payload = {key: value for key, value in policy.items() if key != "policy_digest"}
    if digest(payload) != supplied_digest:
        raise PopulationPolicyError("compiled capacity policy digest mismatch")

    _require_exact_keys(
        raw,
        {
            "observation_schema",
            "observation_id",
            "ladder_stage",
            "consecutive_pause_windows",
            "consecutive_recovery_windows",
            "metrics",
        },
        "capacity observation",
    )
    if _require_int(raw.get("observation_schema"), "observation_schema", 1, 1) != 1:
        raise AssertionError("capacity observation schema validation drift")
    observation_id = _require_token(raw.get("observation_id"), "observation_id")
    ladder_stage = _require_int(raw.get("ladder_stage"), "ladder_stage", 0)
    consecutive_pause_windows = _require_int(
        raw.get("consecutive_pause_windows"), "consecutive_pause_windows", 0
    )
    consecutive_recovery_windows = _require_int(
        raw.get("consecutive_recovery_windows"), "consecutive_recovery_windows", 0
    )

    load_ladder = policy.get("load_ladder")
    if not isinstance(load_ladder, list) or ladder_stage not in load_ladder:
        raise PopulationPolicyError("ladder_stage is not present in the compiled load ladder")
    thresholds = policy.get("thresholds")
    if not isinstance(thresholds, dict):
        raise PopulationPolicyError("compiled capacity policy thresholds are missing")

    metrics_raw = raw.get("metrics")
    if not isinstance(metrics_raw, dict):
        raise PopulationPolicyError("metrics must be an object")
    _require_exact_keys(metrics_raw, OBSERVATION_FIELDS, "metrics")
    metrics = {
        name: _require_int(metrics_raw[name], f"metrics.{name}", 0)
        for name in sorted(OBSERVATION_FIELDS)
    }
    if metrics["online_ambient_sessions"] != ladder_stage:
        raise PopulationPolicyError("online_ambient_sessions must equal the selected ladder stage")
    if metrics["online_ambient_sessions"] > metrics["permanent_roster_count"]:
        raise PopulationPolicyError("online ambient sessions exceed the permanent roster count")

    severity_order = {"normal": 0, "warning": 1, "pause": 2, "backoff": 3}
    metric_results: dict[str, dict[str, object]] = {}
    raw_level = "normal"
    for name in sorted((*MAXIMUM_METRICS, *MINIMUM_METRICS)):
        threshold_raw = thresholds.get(name)
        if not isinstance(threshold_raw, dict):
            raise PopulationPolicyError(f"compiled threshold {name} is invalid")
        threshold = {
            level: _require_int(threshold_raw.get(level), f"thresholds.{name}.{level}", 0)
            for level in ("warning", "pause", "backoff")
        }
        level = _metric_level(metrics[name], threshold, minimum=name in MINIMUM_METRICS)
        metric_results[name] = {
            "value": metrics[name],
            "level": level,
            "direction": "minimum" if name in MINIMUM_METRICS else "maximum",
        }
        if severity_order[level] > severity_order[raw_level]:
            raw_level = level

    fatal_error = metrics["fatal_error_count"] > 0
    if fatal_error:
        raw_level = "backoff"

    pause_confirmation = _require_int(
        policy.get("pause_confirmation_windows"), "pause_confirmation_windows", 1
    )
    recovery_confirmation = _require_int(
        policy.get("recovery_confirmation_windows"), "recovery_confirmation_windows", 1
    )
    stage_minimum_raw = policy.get("stage_minimum_observation_seconds")
    if not isinstance(stage_minimum_raw, dict):
        raise PopulationPolicyError("compiled stage observation minimums are missing")
    minimum_observation = _require_int(
        stage_minimum_raw.get(str(ladder_stage)),
        f"stage_minimum_observation_seconds.{ladder_stage}",
        60,
    )
    if raw_level == "backoff":
        decision = "backoff"
    elif raw_level == "pause" and consecutive_pause_windows >= pause_confirmation:
        decision = "pause"
    elif raw_level == "pause":
        decision = "warning_pending_pause_confirmation"
    elif raw_level == "warning":
        decision = "warning"
    else:
        decision = "normal"

    eligible_to_advance = (
        decision == "normal"
        and metrics["observation_window_seconds"] >= minimum_observation
        and consecutive_recovery_windows >= recovery_confirmation
    )
    result: dict[str, object] = {
        "observation_schema": 1,
        "observation_id": observation_id,
        "policy_digest": supplied_digest,
        "ladder_stage": ladder_stage,
        "raw_level": raw_level,
        "decision": decision,
        "eligible_to_advance": eligible_to_advance,
        "fatal_error": fatal_error,
        "metric_results": metric_results,
        "metrics": metrics,
    }
    return {**result, "observation_digest": digest(result)}
