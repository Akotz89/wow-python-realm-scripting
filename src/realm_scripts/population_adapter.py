"""Fail-closed, read-only handoff for an accepted CPI-196 population manifest.

This module is deliberately a standalone policy boundary.  It consumes already
compiled immutable artifacts and an explicit acceptance receipt, then exposes
planned identity references and wave metadata to future code.  It does not
import an embedded-engine module, allocate database identifiers, create or bind
accounts or characters, admit a session, or write state.

The acceptance receipt is a local handoff contract for the future CPI-196 gate;
the current repository has no accepted receipt or exact identity manifest.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal, cast

from .population_band_policy import compile_band_policy
from .population_policy import (
    PopulationPolicyError,
    _require_bool,
    _require_decimal,
    _require_digest,
    _require_exact_keys,
    _require_int,
    _require_token,
    _verify_compiled_artifact,
    compile_population_manifest,
    digest,
)
from .release_contract import validate_source_contract

CPI196_MANIFEST_SHA256 = "3528d9395fd9612bc70c52274e8083344d9a398c9d3469b664acccdea3359821"
CPI196_SCHEDULE_SHA256 = "60b9a1a4ba8d7d526d318bc9ad9772baa4e8f9ba28fe5495ff288c2099408377"
CPI196_PHASE_AUTHORITY_RECEIPT_SHA256 = "f12edd99318bfbd00ebaf3f2f9950b54490e920683b4dd0b78040e02da13966d"
CPI196_REALM_ID = 1
CPI196_TARGET = 10_000
CPI196_WAVE_COUNT = 80
CPI196_WAVE_ONE = 329
CPI196_FINAL_WAVE = 30
CPI196_GLOBAL_PHASE_CAP = 19
CPI196_TRANSITION_DWELL_SECONDS = 1800
_CPI196_REGION_ORDER = (
    "blood_elf",
    "dwarf_gnome",
    "draenei",
    "human",
    "night_elf",
    "orc_troll",
    "tauren",
    "undead",
)
_CPI196_BANDS = (
    ("pioneer", Decimal("0.05"), 0),
    ("scout", Decimal("0.15"), 1),
    ("forward", Decimal("0.25"), 2),
    ("main", Decimal("0.35"), 4),
    ("trailing", Decimal("0.20"), 7),
)

_MANIFEST_FIELDS = {
    "manifest_schema",
    "manifest_version",
    "realm_id",
    "schedule_digest",
    "composition",
    "spec_templates",
    "identities",
    "reports",
    "manifest_digest",
}
_SCHEDULE_FIELDS = {
    "schema_version",
    "schedule_version",
    "realm_id",
    "maximum_owner_level",
    "permanent_roster_target",
    "wave_curve",
    "allocation",
    "waves",
    "schedule_digest",
}
_WAVE_FIELDS = {"owner_level", "count"}
_IDENTITY_FIELDS = {
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
_ACCEPTANCE_FIELDS = {
    "status",
    "acceptance_id",
    "approver",
    "approved_at_utc",
    "manifest_digest",
    "schedule_digest",
}
_RUNTIME_ID_FIELDS = {
    "account_id",
    "account_guid",
    "character_id",
    "character_guid",
    "database_id",
    "guid",
    "low_guid",
    "owner_account_id",
    "owner_account_guid",
    "owner_character_id",
    "owner_character_guid",
    "owner_guid",
    "owner_anchor_guid",
}
_CPI196_MANIFEST_FIELDS = {
    "schema_version",
    "authority",
    "status",
    "row_generation",
    "reason",
    "realm_id",
    "identity_independent",
    "owner_party_included",
    "admission_policy",
    "composition_policy",
    "level_band_policy",
    "starter_zone_pacing",
    "schedule",
    "input_receipts",
    "unresolved_decisions",
    "candidate_row_contract",
    "rows",
    "runtime_binding",
    "acceptance_gates_remaining",
}
_CPI196_SCHEDULE_FIELDS = {
    "format",
    "version",
    "permanent_roster_target",
    "wave_count",
    "wave_counts",
    "sha256",
}
_CPI196_ADMISSION_FIELDS = {
    "schema_version",
    "state",
    "enabled",
    "runtime_bound",
    "database_writes_authorized",
    "native_population_director_bound",
    "owner_identity_required_for_policy",
    "wave_authorization",
    "reason",
}
_CPI196_COMPOSITION_FIELDS = {
    "schema_version",
    "status",
    "accepted",
    "allocation",
    "faction_shares",
    "starter_region_shares",
    "role_shares",
    "class_policy",
    "template_policy",
    "account_policy",
    "row_policy",
}
_CPI196_ALLOCATION_FIELDS = {"algorithm", "tie_break", "per_wave", "runtime_reroll"}
_CPI196_BAND_POLICY_FIELDS = {
    "schema_version",
    "policy_version",
    "status",
    "maximum_owner_level",
    "global_phase_cap",
    "transition_dwell_seconds",
    "ratio_basis",
    "membership",
    "ceiling_rule",
    "bands",
}
_CPI196_BAND_FIELDS = {"name", "share", "owner_level_offset"}
_CPI196_PACING_FIELDS = {
    "schema_version",
    "status",
    "entry_mode",
    "wave_authorization",
    "time_based_wave_authorization",
    "wave_size_frozen_at_authorization",
    "region_count",
    "region_order",
    "max_sessions_per_region_before_all_regions_served",
    "login_rate_limit",
    "per_region_online_cap",
    "max_active_per_spawn_cell",
    "creation_batch_size",
    "creation_batch_size_source",
    "simultaneous_online_cap",
    "simultaneous_online_cap_source",
    "runtime_enforcement",
}
_CPI196_RECEIPT_FIELDS = {
    "decisions",
    "appearance_catalogue",
    "template_catalogue",
    "phase_authority",
    "build_receipt",
}
_CPI196_RUNTIME_BINDING_FIELDS = {"status", "manifest_consumer", "required_operations", "prohibited"}
_CPI196_CANDIDATE_ROW_FIELDS = {
    "required_count",
    "canonical_order",
    "immutable_fields",
    "derived_fields",
    "runtime_outputs_not_guessed",
}
_CPI196_REQUIRED_OPERATIONS = (
    "reserve_identity_once",
    "ensure_account_idempotently",
    "ensure_level_one_character_idempotently",
    "bind_identity_once_with_compare_and_set_and_readback",
    "admit_only_after_exact_binding",
    "request_sessions_under_separate_capacity_generation",
)
_CPI196_PROHIBITED_OPERATIONS = frozenset(
    {
        "database_writes_from_this_candidate",
        "guessed_account_ids_or_character_guids",
        "stock_bulk_random_factory_substitution",
        "owner_party_identifiers",
        "identity_reroll_or_wave_reassignment",
    }
)


@dataclass(frozen=True, slots=True)
class CPI225ReleaseSurfaceBinding:
    """The exact CPI-225 source tuple carried into an offline plan."""

    release_surface_sha256: str
    core: str
    playerbots: str
    python_engine: str
    canonical_manifest_sha256: str


@dataclass(frozen=True, slots=True)
class PopulationWavePlan:
    """One deterministic wave plan with admission deliberately disabled."""

    owner_level: int
    planned_count: int
    global_phase_cap: int
    band_ceilings: tuple[int, ...]
    wave_idempotency_key: str
    admission_authorized: Literal[False] = False

    def __post_init__(self) -> None:
        if self.admission_authorized:
            raise PopulationPolicyError("population wave plans cannot authorize admission")


@dataclass(frozen=True, slots=True)
class IdentityIndependentPopulationPlan:
    """A policy-only CPI-196 projection with a PASS_NO_BIND disposition."""

    status: Literal["PASS_NO_BIND"]
    manifest_sha256: str
    schedule_sha256: str
    phase_authority_sha256: str
    release_surface: CPI225ReleaseSurfaceBinding
    realm_id: int
    schedule_version: str
    permanent_roster_target: int
    wave_counts: tuple[int, ...]
    global_phase_cap: int
    transition_dwell_seconds: int
    band_names: tuple[str, ...]
    band_shares: tuple[str, ...]
    band_offsets: tuple[int, ...]
    band_ceilings: tuple[tuple[int, ...], ...]
    region_order: tuple[str, ...]
    max_sessions_per_region_before_all_regions_served: int
    login_rate_sessions: int
    login_rate_window_seconds: int
    max_active_per_spawn_cell: int
    creation_batch_size: None = None
    simultaneous_online_cap: None = None
    owner_party_included: Literal[False] = False
    admission_authorized: Literal[False] = False
    runtime_bound: Literal[False] = False
    database_writes_authorized: Literal[False] = False
    native_population_director_bound: Literal[False] = False
    embedded_python_enabled: Literal[False] = False

    def __post_init__(self) -> None:
        if self.status != "PASS_NO_BIND":
            raise PopulationPolicyError("identity-independent population plans must be PASS_NO_BIND")
        if (
            self.owner_party_included
            or self.admission_authorized
            or self.runtime_bound
            or self.database_writes_authorized
            or self.native_population_director_bound
            or self.embedded_python_enabled
        ):
            raise PopulationPolicyError("identity-independent population plan must remain unbound")

    def wave(self, owner_level: int) -> PopulationWavePlan:
        """Return one deterministic wave plan without authorizing it."""

        _require_int(owner_level, "owner_level", 1, len(self.wave_counts))
        index = owner_level - 1
        return PopulationWavePlan(
            owner_level=owner_level,
            planned_count=self.wave_counts[index],
            global_phase_cap=self.global_phase_cap,
            band_ceilings=self.band_ceilings[index],
            wave_idempotency_key=digest(
                {
                    "realm_id": self.realm_id,
                    "owner_level": owner_level,
                    "schedule_sha256": self.schedule_sha256,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class _IdentityIndependentFacts:
    manifest_sha256: str
    schedule_sha256: str
    phase_authority_sha256: str
    realm_id: int
    schedule_version: str
    permanent_roster_target: int
    wave_counts: tuple[int, ...]
    global_phase_cap: int
    transition_dwell_seconds: int
    band_names: tuple[str, ...]
    band_shares: tuple[str, ...]
    band_offsets: tuple[int, ...]
    band_ceilings: tuple[tuple[int, ...], ...]
    region_order: tuple[str, ...]
    max_sessions_per_region_before_all_regions_served: int
    login_rate_sessions: int
    login_rate_window_seconds: int
    max_active_per_spawn_cell: int


def _as_mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise PopulationPolicyError(f"{context} must be a JSON object with string field names")
    return value


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise PopulationPolicyError(f"{name} must be a non-empty string")
    return value


def _require_candidate_value(value: object, expected: object, name: str) -> None:
    if value != expected:
        raise PopulationPolicyError(f"{name} differs from the CPI-196 candidate policy")


def _candidate_share_map(
    value: object,
    expected_names: tuple[str, ...],
    expected_values: tuple[Decimal, ...],
    name: str,
) -> tuple[Decimal, ...]:
    raw = _as_mapping(value, name)
    _require_exact_keys(raw, set(expected_names), name)
    shares = tuple(
        _require_decimal(raw[item], f"{name}.{item}") for item in expected_names
    )
    if shares != expected_values:
        raise PopulationPolicyError(f"{name} differs from the CPI-196 candidate policy")
    if sum(shares, Decimal(0)) != Decimal(1):
        raise PopulationPolicyError(f"{name} shares must sum exactly to 1")
    return shares


def _validate_candidate_schedule(value: object) -> tuple[int, str, int, tuple[int, ...], str]:
    schedule = _as_mapping(value, "CPI-196 schedule")
    _require_exact_keys(schedule, _CPI196_SCHEDULE_FIELDS, "CPI-196 schedule")
    _require_candidate_value(
        schedule.get("format"),
        "cpi-196-immutable-integer-schedule-v1",
        "CPI-196 schedule.format",
    )
    schedule_version = _require_text(schedule.get("version"), "CPI-196 schedule.version")
    _require_candidate_value(
        schedule_version,
        "cpi-196-rndbot-wave-policy-candidate-v1",
        "CPI-196 schedule.version",
    )
    target = _require_int(
        schedule.get("permanent_roster_target"),
        "CPI-196 schedule.permanent_roster_target",
        1,
    )
    _require_candidate_value(target, CPI196_TARGET, "CPI-196 schedule.permanent_roster_target")
    wave_count = _require_int(schedule.get("wave_count"), "CPI-196 schedule.wave_count", 1)
    _require_candidate_value(wave_count, CPI196_WAVE_COUNT, "CPI-196 schedule.wave_count")
    raw_counts = schedule.get("wave_counts")
    if not isinstance(raw_counts, list):
        raise PopulationPolicyError("CPI-196 schedule.wave_counts must be a list")
    wave_counts = tuple(
        _require_int(count, f"CPI-196 schedule.wave_counts[{index}]", 1)
        for index, count in enumerate(raw_counts)
    )
    if len(wave_counts) != CPI196_WAVE_COUNT or len(wave_counts) != wave_count:
        raise PopulationPolicyError("CPI-196 schedule must contain exactly 80 positive waves")
    if sum(wave_counts) != target:
        raise PopulationPolicyError("CPI-196 wave counts must equal the 10000 identity target")
    if any(left < right for left, right in zip(wave_counts, wave_counts[1:], strict=False)):
        raise PopulationPolicyError("CPI-196 wave counts must be non-increasing")
    _require_candidate_value(wave_counts[0], CPI196_WAVE_ONE, "CPI-196 schedule W1")
    _require_candidate_value(wave_counts[-1], CPI196_FINAL_WAVE, "CPI-196 schedule W80")
    schedule_sha256 = _require_digest(schedule.get("sha256"), "CPI-196 schedule.sha256")
    _require_candidate_value(schedule_sha256, CPI196_SCHEDULE_SHA256, "CPI-196 schedule.sha256")
    return CPI196_REALM_ID, schedule_version, target, wave_counts, schedule_sha256


def _validate_candidate_admission(value: object) -> None:
    admission = _as_mapping(value, "CPI-196 admission_policy")
    _require_exact_keys(admission, _CPI196_ADMISSION_FIELDS, "CPI-196 admission_policy")
    _require_candidate_value(
        admission.get("schema_version"),
        "cpi-196-admission-posture-v1",
        "CPI-196 admission_policy.schema_version",
    )
    _require_candidate_value(admission.get("state"), "NO-BIND", "CPI-196 admission_policy.state")
    for field in (
        "enabled",
        "runtime_bound",
        "database_writes_authorized",
        "native_population_director_bound",
        "owner_identity_required_for_policy",
    ):
        if _require_bool(admission.get(field), f"CPI-196 admission_policy.{field}"):
            raise PopulationPolicyError(f"CPI-196 admission_policy.{field} must remain false")
    _require_candidate_value(
        admission.get("wave_authorization"),
        "DISABLED_UNTIL_LATER_RUNTIME_GATES",
        "CPI-196 admission_policy.wave_authorization",
    )
    _require_text(admission.get("reason"), "CPI-196 admission_policy.reason")


def _validate_candidate_composition(value: object) -> None:
    composition = _as_mapping(value, "CPI-196 composition_policy")
    _require_exact_keys(composition, _CPI196_COMPOSITION_FIELDS, "CPI-196 composition_policy")
    _require_candidate_value(
        composition.get("schema_version"),
        "cpi-196-identity-independent-composition-v1",
        "CPI-196 composition_policy.schema_version",
    )
    _require_candidate_value(composition.get("status"), "CANDIDATE_ONLY", "CPI-196 composition_policy.status")
    if _require_bool(composition.get("accepted"), "CPI-196 composition_policy.accepted"):
        raise PopulationPolicyError("CPI-196 composition policy must remain unaccepted")

    allocation = _as_mapping(composition.get("allocation"), "CPI-196 composition_policy.allocation")
    _require_exact_keys(allocation, _CPI196_ALLOCATION_FIELDS, "CPI-196 composition_policy.allocation")
    _require_candidate_value(allocation.get("algorithm"), "largest_remainder", "CPI-196 allocation.algorithm")
    _require_candidate_value(allocation.get("tie_break"), "canonical_order", "CPI-196 allocation.tie_break")
    _require_candidate_value(
        allocation.get("per_wave"),
        "derive_from_scheduled_wave_count",
        "CPI-196 allocation.per_wave",
    )
    if _require_bool(allocation.get("runtime_reroll"), "CPI-196 allocation.runtime_reroll"):
        raise PopulationPolicyError("CPI-196 composition policy must forbid runtime rerolls")

    _candidate_share_map(
        composition.get("faction_shares"),
        ("alliance", "horde"),
        (Decimal("0.5"), Decimal("0.5")),
        "CPI-196 composition_policy.faction_shares",
    )
    _candidate_share_map(
        composition.get("starter_region_shares"),
        _CPI196_REGION_ORDER,
        (Decimal("0.125"),) * len(_CPI196_REGION_ORDER),
        "CPI-196 composition_policy.starter_region_shares",
    )
    _candidate_share_map(
        composition.get("role_shares"),
        ("tank", "healer", "damage"),
        (Decimal("0.2"), Decimal("0.2"), Decimal("0.6")),
        "CPI-196 composition_policy.role_shares",
    )
    _require_candidate_value(
        composition.get("class_policy"),
        "legal_non_death_knight_pairs_only",
        "CPI-196 composition_policy.class_policy",
    )
    _require_candidate_value(
        composition.get("template_policy"),
        "accepted_template_set_required_before_rows",
        "CPI-196 composition_policy.template_policy",
    )
    _require_candidate_value(
        composition.get("account_policy"),
        "identity_and_account_namespace_unresolved",
        "CPI-196 composition_policy.account_policy",
    )
    _require_candidate_value(
        composition.get("row_policy"),
        "exact_rows_required_before_acceptance",
        "CPI-196 composition_policy.row_policy",
    )


def _validate_candidate_bands(value: object) -> tuple[
    tuple[str, ...], tuple[str, ...], tuple[int, ...], tuple[tuple[int, ...], ...]
]:
    policy = _as_mapping(value, "CPI-196 level_band_policy")
    _require_exact_keys(policy, _CPI196_BAND_POLICY_FIELDS, "CPI-196 level_band_policy")
    _require_candidate_value(policy.get("schema_version"), 1, "CPI-196 level_band_policy.schema_version")
    _require_candidate_value(
        policy.get("policy_version"),
        "owner-relative-candidate-v1",
        "CPI-196 level_band_policy.policy_version",
    )
    _require_candidate_value(policy.get("status"), "CANDIDATE_ONLY", "CPI-196 level_band_policy.status")
    maximum_owner_level = _require_int(
        policy.get("maximum_owner_level"), "CPI-196 level_band_policy.maximum_owner_level", 1, 80
    )
    _require_candidate_value(maximum_owner_level, CPI196_WAVE_COUNT, "CPI-196 level_band_policy.maximum_owner_level")
    global_phase_cap = _require_int(
        policy.get("global_phase_cap"), "CPI-196 level_band_policy.global_phase_cap", 1, 80
    )
    _require_candidate_value(global_phase_cap, CPI196_GLOBAL_PHASE_CAP, "CPI-196 level_band_policy.global_phase_cap")
    transition_dwell_seconds = _require_int(
        policy.get("transition_dwell_seconds"),
        "CPI-196 level_band_policy.transition_dwell_seconds",
        1,
    )
    _require_candidate_value(
        transition_dwell_seconds,
        CPI196_TRANSITION_DWELL_SECONDS,
        "CPI-196 level_band_policy.transition_dwell_seconds",
    )
    _require_candidate_value(
        policy.get("ratio_basis"),
        "scheduled_cumulative_population",
        "CPI-196 level_band_policy.ratio_basis",
    )
    _require_candidate_value(
        policy.get("membership"),
        "earned_progress_rank_not_identity_field",
        "CPI-196 level_band_policy.membership",
    )
    _require_candidate_value(
        policy.get("ceiling_rule"),
        "max(1,min(global_phase_cap,owner_level-owner_level_offset))",
        "CPI-196 level_band_policy.ceiling_rule",
    )

    raw_bands = policy.get("bands")
    if not isinstance(raw_bands, list) or len(raw_bands) != len(_CPI196_BANDS):
        raise PopulationPolicyError("CPI-196 level-band policy must contain the five ordered bands")
    normalized_bands: list[dict[str, object]] = []
    band_names: list[str] = []
    band_shares: list[str] = []
    band_offsets: list[int] = []
    for index, (expected_name, expected_share, expected_offset) in enumerate(_CPI196_BANDS):
        band = _as_mapping(raw_bands[index], f"CPI-196 level_band_policy.bands[{index}]")
        _require_exact_keys(band, _CPI196_BAND_FIELDS, f"CPI-196 level_band_policy.bands[{index}]")
        name = _require_text(band.get("name"), f"CPI-196 level_band_policy.bands[{index}].name")
        share = _require_decimal(band.get("share"), f"CPI-196 level_band_policy.bands[{index}].share")
        offset = _require_int(
            band.get("owner_level_offset"),
            f"CPI-196 level_band_policy.bands[{index}].owner_level_offset",
            0,
        )
        if (name, share, offset) != (expected_name, expected_share, expected_offset):
            raise PopulationPolicyError(f"CPI-196 level-band entry {index} differs from the candidate policy")
        normalized_bands.append(
            {"name": name, "share": str(share), "owner_level_offset": offset}
        )
        band_names.append(name)
        band_shares.append(str(share))
        band_offsets.append(offset)

    compiled = compile_band_policy(
        {
            "policy_schema": 1,
            "policy_version": policy["policy_version"],
            "maximum_owner_level": maximum_owner_level,
            "global_phase_cap": global_phase_cap,
            "transition_dwell_seconds": transition_dwell_seconds,
            "bands": normalized_bands,
        }
    )
    raw_epochs = compiled.get("ceiling_epochs")
    if not isinstance(raw_epochs, list) or len(raw_epochs) != maximum_owner_level:
        raise PopulationPolicyError("CPI-196 level-band ceilings must cover every owner level")
    band_ceilings: list[tuple[int, ...]] = []
    for index, raw_epoch in enumerate(raw_epochs):
        epoch = _as_mapping(raw_epoch, f"CPI-196 level-band ceiling epoch {index}")
        owner_level = _require_int(epoch.get("owner_level"), f"level-band ceiling epoch {index}.owner_level", 1, 80)
        if owner_level != index + 1:
            raise PopulationPolicyError("CPI-196 level-band ceiling epochs must be contiguous")
        ceilings = _as_mapping(epoch.get("effective_ceilings"), f"level-band ceiling epoch {index}.effective_ceilings")
        _require_exact_keys(ceilings, set(band_names), f"level-band ceiling epoch {index}.effective_ceilings")
        band_ceilings.append(
            tuple(
                _require_int(ceilings[name], f"level-band ceiling epoch {index}.{name}", 1, global_phase_cap)
                for name in band_names
            )
        )
    return (
        tuple(band_names),
        tuple(band_shares),
        tuple(band_offsets),
        tuple(band_ceilings),
    )


def _validate_candidate_pacing(value: object) -> tuple[tuple[str, ...], int, int, int, int]:
    pacing = _as_mapping(value, "CPI-196 starter_zone_pacing")
    _require_exact_keys(pacing, _CPI196_PACING_FIELDS, "CPI-196 starter_zone_pacing")
    _require_candidate_value(
        pacing.get("schema_version"),
        "cpi-196-starter-zone-pacing-v1",
        "CPI-196 starter_zone_pacing.schema_version",
    )
    _require_candidate_value(pacing.get("status"), "CANDIDATE_ONLY", "CPI-196 starter_zone_pacing.status")
    _require_candidate_value(
        pacing.get("entry_mode"),
        "bounded_region_round_robin",
        "CPI-196 starter_zone_pacing.entry_mode",
    )
    _require_candidate_value(
        pacing.get("wave_authorization"),
        "owner_level_triggered_only",
        "CPI-196 starter_zone_pacing.wave_authorization",
    )
    if _require_bool(
        pacing.get("time_based_wave_authorization"),
        "CPI-196 starter_zone_pacing.time_based_wave_authorization",
    ):
        raise PopulationPolicyError("CPI-196 pacing must not authorize waves by wall-clock time")
    if not _require_bool(
        pacing.get("wave_size_frozen_at_authorization"),
        "CPI-196 starter_zone_pacing.wave_size_frozen_at_authorization",
    ):
        raise PopulationPolicyError("CPI-196 wave size must be frozen at authorization")
    region_count = _require_int(pacing.get("region_count"), "CPI-196 starter_zone_pacing.region_count", 1)
    _require_candidate_value(region_count, len(_CPI196_REGION_ORDER), "CPI-196 starter_zone_pacing.region_count")
    region_order = pacing.get("region_order")
    if not isinstance(region_order, list) or tuple(region_order) != _CPI196_REGION_ORDER:
        raise PopulationPolicyError("CPI-196 starter-zone region order differs from the candidate policy")
    round_robin_limit = _require_int(
        pacing.get("max_sessions_per_region_before_all_regions_served"),
        "CPI-196 starter_zone_pacing.max_sessions_per_region_before_all_regions_served",
        1,
    )
    _require_candidate_value(round_robin_limit, 1, "CPI-196 starter-zone round-robin limit")
    login_rate = _as_mapping(pacing.get("login_rate_limit"), "CPI-196 starter_zone_pacing.login_rate_limit")
    _require_exact_keys(login_rate, {"sessions", "window_seconds"}, "CPI-196 starter_zone_pacing.login_rate_limit")
    login_sessions = _require_int(
        login_rate.get("sessions"), "CPI-196 starter_zone_pacing.login_rate_limit.sessions", 1
    )
    login_window = _require_int(
        login_rate.get("window_seconds"), "CPI-196 starter_zone_pacing.login_rate_limit.window_seconds", 1
    )
    _require_candidate_value(login_sessions, 5, "CPI-196 starter-zone login rate")
    _require_candidate_value(login_window, 20, "CPI-196 starter-zone login window")
    _require_candidate_value(
        pacing.get("per_region_online_cap"),
        "min(12,ceil(configured_online_target/8))",
        "CPI-196 starter_zone_pacing.per_region_online_cap",
    )
    spawn_cap = _require_int(
        pacing.get("max_active_per_spawn_cell"),
        "CPI-196 starter_zone_pacing.max_active_per_spawn_cell",
        1,
    )
    _require_candidate_value(spawn_cap, 4, "CPI-196 starter-zone spawn-cell cap")
    if pacing.get("creation_batch_size") is not None:
        raise PopulationPolicyError("CPI-196 creation batch size must remain unresolved until CPI-202")
    _require_candidate_value(
        pacing.get("creation_batch_size_source"),
        "CPI-202_REQUIRED",
        "CPI-196 starter_zone_pacing.creation_batch_size_source",
    )
    if pacing.get("simultaneous_online_cap") is not None:
        raise PopulationPolicyError("CPI-196 simultaneous online cap must remain unresolved")
    _require_candidate_value(
        pacing.get("simultaneous_online_cap_source"),
        "CPI-202_REQUIRED",
        "CPI-196 starter_zone_pacing.simultaneous_online_cap_source",
    )
    _require_candidate_value(
        pacing.get("runtime_enforcement"),
        "NOT_BOUND",
        "CPI-196 starter_zone_pacing.runtime_enforcement",
    )
    return tuple(region_order), round_robin_limit, login_sessions, login_window, spawn_cap


def _validate_candidate_receipts(value: object) -> str:
    receipts = _as_mapping(value, "CPI-196 input_receipts")
    _require_exact_keys(receipts, _CPI196_RECEIPT_FIELDS, "CPI-196 input_receipts")
    for name, raw_receipt in receipts.items():
        receipt = _as_mapping(raw_receipt, f"CPI-196 input_receipts.{name}")
        _require_digest(receipt.get("sha256"), f"CPI-196 input_receipts.{name}.sha256")

    decisions = _as_mapping(receipts["decisions"], "CPI-196 decisions receipt")
    _require_candidate_value(decisions.get("status"), "UNRESOLVED", "CPI-196 decisions receipt.status")
    appearance = _as_mapping(receipts["appearance_catalogue"], "CPI-196 appearance receipt")
    _require_candidate_value(appearance.get("status"), "UNRESOLVED", "CPI-196 appearance receipt.status")
    _require_candidate_value(appearance.get("row_count"), 0, "CPI-196 appearance receipt.row_count")
    templates = _as_mapping(receipts["template_catalogue"], "CPI-196 template receipt")
    _require_candidate_value(
        templates.get("status"),
        "AVAILABLE_NOT_ACCEPTED",
        "CPI-196 template receipt.status",
    )
    _require_candidate_value(templates.get("row_count"), 29, "CPI-196 template receipt.row_count")
    phase = _as_mapping(receipts["phase_authority"], "CPI-191 phase receipt")
    phase_digest = _require_digest(phase.get("sha256"), "CPI-191 phase receipt.sha256")
    _require_candidate_value(
        phase_digest,
        CPI196_PHASE_AUTHORITY_RECEIPT_SHA256,
        "CPI-191 phase receipt.sha256",
    )
    _require_candidate_value(
        phase.get("status"),
        "STATIC_AUTHORITY_BOUND_RUNTIME_UNACCEPTED",
        "CPI-191 phase receipt.status",
    )
    build = _as_mapping(receipts["build_receipt"], "CPI-225 build receipt")
    _require_candidate_value(build.get("status"), "BUILD_PACKAGE_ONLY", "CPI-225 build receipt.status")
    return phase_digest


def _validate_candidate_no_bind_fields(value: object) -> None:
    row_contract = _as_mapping(value, "CPI-196 candidate_row_contract")
    _require_exact_keys(row_contract, _CPI196_CANDIDATE_ROW_FIELDS, "CPI-196 candidate_row_contract")
    _require_candidate_value(row_contract.get("required_count"), CPI196_TARGET, "CPI-196 candidate row target")
    _require_candidate_value(
        row_contract.get("canonical_order"),
        "wave_owner_level,wave_ordinal",
        "CPI-196 candidate row order",
    )
    runtime_outputs = row_contract.get("runtime_outputs_not_guessed")
    if not isinstance(runtime_outputs, list) or tuple(runtime_outputs) != (
        "actual_account_id",
        "actual_character_guid",
    ):
        raise PopulationPolicyError("CPI-196 candidate must leave runtime IDs unguessed")


def _validate_candidate_runtime_binding(value: object) -> None:
    runtime = _as_mapping(value, "CPI-196 runtime_binding")
    _require_exact_keys(runtime, _CPI196_RUNTIME_BINDING_FIELDS, "CPI-196 runtime_binding")
    _require_candidate_value(runtime.get("status"), "NOT_AUTHORIZED", "CPI-196 runtime_binding.status")
    _require_candidate_value(
        runtime.get("manifest_consumer"),
        "future accepted Python-engine capability path",
        "CPI-196 runtime_binding.manifest_consumer",
    )
    required_operations = runtime.get("required_operations")
    if not isinstance(required_operations, list) or tuple(required_operations) != _CPI196_REQUIRED_OPERATIONS:
        raise PopulationPolicyError("CPI-196 runtime binding operations differ from the no-bind contract")
    prohibited = runtime.get("prohibited")
    if not isinstance(prohibited, list) or not _CPI196_PROHIBITED_OPERATIONS.issubset(set(prohibited)):
        raise PopulationPolicyError("CPI-196 runtime binding is missing a no-bind safeguard")


def _validate_identity_independent_candidate(
    value: object,
    schedule: object,
    manifest_sha256: str,
) -> _IdentityIndependentFacts:
    manifest = _as_mapping(value, "CPI-196 identity-independent manifest")
    _reject_runtime_identifiers(manifest, "CPI-196 manifest")
    _require_exact_keys(manifest, _CPI196_MANIFEST_FIELDS, "CPI-196 identity-independent manifest")
    supplied_manifest_sha256 = _require_digest(manifest_sha256, "CPI-196 manifest.sha256")
    _require_candidate_value(supplied_manifest_sha256, CPI196_MANIFEST_SHA256, "CPI-196 manifest.sha256")
    _require_candidate_value(
        manifest.get("schema_version"),
        "cpi-196-identity-independent-manifest-candidate-v1",
        "CPI-196 manifest.schema_version",
    )
    _require_candidate_value(manifest.get("authority"), "NON_AUTHORITATIVE", "CPI-196 manifest.authority")
    _require_candidate_value(manifest.get("status"), "UNRESOLVED", "CPI-196 manifest.status")
    _require_candidate_value(
        manifest.get("row_generation"),
        "FORBIDDEN_UNTIL_DECISIONS_ACCEPTED",
        "CPI-196 manifest.row_generation",
    )
    _require_text(manifest.get("reason"), "CPI-196 manifest.reason")
    realm_id = _require_int(manifest.get("realm_id"), "CPI-196 manifest.realm_id", 1)
    _require_candidate_value(realm_id, CPI196_REALM_ID, "CPI-196 manifest.realm_id")
    if not _require_bool(manifest.get("identity_independent"), "CPI-196 manifest.identity_independent"):
        raise PopulationPolicyError("CPI-196 manifest must remain identity independent")
    if _require_bool(manifest.get("owner_party_included"), "CPI-196 manifest.owner_party_included"):
        raise PopulationPolicyError("CPI-196 owner party must remain outside ambient allocation")
    if manifest.get("rows") != []:
        raise PopulationPolicyError("CPI-196 unresolved identity-independent candidate must contain zero rows")

    _validate_candidate_admission(manifest.get("admission_policy"))
    _validate_candidate_composition(manifest.get("composition_policy"))
    band_names, band_shares, band_offsets, band_ceilings = _validate_candidate_bands(
        manifest.get("level_band_policy")
    )
    region_order, round_robin_limit, login_sessions, login_window, spawn_cap = _validate_candidate_pacing(
        manifest.get("starter_zone_pacing")
    )
    realm_id_from_schedule, schedule_version, target, wave_counts, schedule_sha256 = _validate_candidate_schedule(
        schedule
    )
    nested_schedule = _as_mapping(manifest.get("schedule"), "CPI-196 manifest.schedule")
    external_schedule = _as_mapping(schedule, "CPI-196 schedule")
    if dict(nested_schedule) != dict(external_schedule):
        raise PopulationPolicyError("CPI-196 manifest and supplied schedule differ")
    if realm_id_from_schedule != realm_id:
        raise PopulationPolicyError("CPI-196 manifest and schedule realms differ")
    _validate_candidate_receipts(manifest.get("input_receipts"))
    _validate_candidate_no_bind_fields(manifest.get("candidate_row_contract"))
    _validate_candidate_runtime_binding(manifest.get("runtime_binding"))

    unresolved = manifest.get("unresolved_decisions")
    if not isinstance(unresolved, list) or not unresolved:
        raise PopulationPolicyError("CPI-196 unresolved_decisions must remain non-empty")
    if not any(
        isinstance(item, Mapping) and item.get("current_status") == "UNRESOLVED"
        for item in unresolved
    ):
        raise PopulationPolicyError("CPI-196 unresolved_decisions must retain an unresolved decision")
    gates = manifest.get("acceptance_gates_remaining")
    if not isinstance(gates, list) or not gates or any(not isinstance(item, str) for item in gates):
        raise PopulationPolicyError("CPI-196 acceptance gates must remain explicit")
    gate_text = " ".join(cast(list[str], gates))
    for fragment in ("CPI-202", "CPI-194", "CPI-223", "gameplay"):
        if fragment not in gate_text:
            raise PopulationPolicyError(f"CPI-196 acceptance gates are missing {fragment}")

    return _IdentityIndependentFacts(
        manifest_sha256=supplied_manifest_sha256,
        schedule_sha256=schedule_sha256,
        phase_authority_sha256=CPI196_PHASE_AUTHORITY_RECEIPT_SHA256,
        realm_id=realm_id,
        schedule_version=schedule_version,
        permanent_roster_target=target,
        wave_counts=wave_counts,
        global_phase_cap=CPI196_GLOBAL_PHASE_CAP,
        transition_dwell_seconds=CPI196_TRANSITION_DWELL_SECONDS,
        band_names=band_names,
        band_shares=band_shares,
        band_offsets=band_offsets,
        band_ceilings=band_ceilings,
        region_order=region_order,
        max_sessions_per_region_before_all_regions_served=round_robin_limit,
        login_rate_sessions=login_sessions,
        login_rate_window_seconds=login_window,
        max_active_per_spawn_cell=spawn_cap,
    )


def _validate_release_surface_binding(value: object) -> CPI225ReleaseSurfaceBinding:
    release = _as_mapping(value, "CPI-225 release surface contract")
    _reject_runtime_identifiers(release, "CPI-225 release surface")
    validated = validate_source_contract(release)
    exact_tuple = _as_mapping(validated.get("exact_tuple"), "CPI-225 exact tuple")
    return CPI225ReleaseSurfaceBinding(
        release_surface_sha256=cast(str, validated["release_surface_sha256"]),
        core=cast(str, exact_tuple["core"]),
        playerbots=cast(str, exact_tuple["playerbots"]),
        python_engine=cast(str, exact_tuple["python_engine"]),
        canonical_manifest_sha256=cast(str, exact_tuple["canonical_manifest_sha256"]),
    )


def _reject_runtime_identifiers(value: object, path: str) -> None:
    """Reject database/runtime identity fields anywhere in an artifact."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise PopulationPolicyError(f"{path} contains a non-string field name")
            normalized = key.casefold().replace("-", "_")
            account_or_character_id = (
                normalized.startswith(("account_", "character_", "owner_"))
                and normalized.endswith(("_id", "_guid"))
            )
            if normalized in _RUNTIME_ID_FIELDS or account_or_character_id:
                raise PopulationPolicyError(
                    f"{path}.{key} is a runtime identity field; manifest binding is not authorized"
                )
            _reject_runtime_identifiers(child, f"{path}.{key}")
    elif isinstance(value, list):
        for ordinal, child in enumerate(value):
            _reject_runtime_identifiers(child, f"{path}[{ordinal}]")


def _validate_acceptance(
    acceptance: Mapping[str, object],
    manifest_digest: str,
    schedule_digest: str,
) -> tuple[str, str, str]:
    """Validate the explicit future-gate receipt without changing any state."""

    _require_exact_keys(acceptance, _ACCEPTANCE_FIELDS, "CPI-196 acceptance receipt")
    if acceptance.get("status") != "ACCEPTED":
        raise PopulationPolicyError("CPI-196 acceptance receipt must have status ACCEPTED")
    acceptance_id = _require_token(acceptance.get("acceptance_id"), "acceptance_id")
    approver = acceptance.get("approver")
    if not isinstance(approver, str) or not approver.strip() or len(approver) > 128:
        raise PopulationPolicyError("approver must be a non-empty string of at most 128 characters")

    approved_at = acceptance.get("approved_at_utc")
    if not isinstance(approved_at, str) or not approved_at:
        raise PopulationPolicyError("approved_at_utc must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(approved_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PopulationPolicyError("approved_at_utc must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise PopulationPolicyError("approved_at_utc must include the UTC offset")

    accepted_manifest_digest = _require_digest(
        acceptance.get("manifest_digest"), "acceptance.manifest_digest"
    )
    accepted_schedule_digest = _require_digest(
        acceptance.get("schedule_digest"), "acceptance.schedule_digest"
    )
    if accepted_manifest_digest != manifest_digest:
        raise PopulationPolicyError("acceptance receipt does not bind the supplied manifest")
    if accepted_schedule_digest != schedule_digest:
        raise PopulationPolicyError("acceptance receipt does not bind the supplied schedule")
    return acceptance_id, approver, approved_at


@dataclass(frozen=True, slots=True)
class PopulationIdentityReference:
    """An immutable manifest key and wave location, never a runtime binding."""

    manifest_identity: str
    creation_wave_owner_level: int
    wave_ordinal: int
    admission_eligible: bool


@dataclass(frozen=True, slots=True)
class PopulationManifestContract:
    """Read-only metadata handed from accepted policy to a future adapter."""

    realm_id: str
    manifest_digest: str
    schedule_digest: str
    schedule_version: str
    durable_target: int
    wave_counts: tuple[int, ...]
    identity_references: tuple[PopulationIdentityReference, ...]
    acceptance_id: str
    approver: str
    approved_at_utc: str
    admission_authorized: Literal[False] = False
    account_binding_count: Literal[0] = 0
    character_binding_count: Literal[0] = 0

    def __post_init__(self) -> None:
        if self.admission_authorized or self.account_binding_count != 0 or self.character_binding_count != 0:
            raise PopulationPolicyError("read-only population contract cannot authorize or bind identities")

    def identities_for_wave(self, owner_level: int) -> tuple[PopulationIdentityReference, ...]:
        """Return planned references for one owner-level wave without authorizing it."""

        _require_int(owner_level, "owner_level", 1, len(self.wave_counts))
        return tuple(
            reference
            for reference in self.identity_references
            if reference.creation_wave_owner_level == owner_level
        )

    def wave_idempotency_key(self, owner_level: int) -> str:
        """Return an owner-identity-free deterministic key for a planned wave."""

        _require_int(owner_level, "owner_level", 1, len(self.wave_counts))
        return digest(
            {
                "realm_id": self.realm_id,
                "owner_level": owner_level,
                "schedule_digest": self.schedule_digest,
            }
        )


class PopulationManifestAdapter:
    """Pure CPI-196 consumer with no runtime or persistence side effects."""

    @staticmethod
    def consume_identity_independent_candidate(
        manifest: Mapping[str, object],
        schedule: Mapping[str, object],
        release_surface: Mapping[str, object],
        *,
        manifest_sha256: str,
    ) -> IdentityIndependentPopulationPlan:
        """Project the CPI-196 policy candidate without binding identities.

        The candidate is intentionally still unresolved and contains no rows.
        This method verifies the sidecar digest supplied by the caller, the
        exact frozen schedule, phase/band/pacing policy, and the CPI-225 source
        tuple.  It returns a deterministic plan only; no account, character,
        GUID, session, database, or native capability operation is possible.
        """

        facts = _validate_identity_independent_candidate(manifest, schedule, manifest_sha256)
        release_binding = _validate_release_surface_binding(release_surface)
        return IdentityIndependentPopulationPlan(
            status="PASS_NO_BIND",
            manifest_sha256=facts.manifest_sha256,
            schedule_sha256=facts.schedule_sha256,
            phase_authority_sha256=facts.phase_authority_sha256,
            release_surface=release_binding,
            realm_id=facts.realm_id,
            schedule_version=facts.schedule_version,
            permanent_roster_target=facts.permanent_roster_target,
            wave_counts=facts.wave_counts,
            global_phase_cap=facts.global_phase_cap,
            transition_dwell_seconds=facts.transition_dwell_seconds,
            band_names=facts.band_names,
            band_shares=facts.band_shares,
            band_offsets=facts.band_offsets,
            band_ceilings=facts.band_ceilings,
            region_order=facts.region_order,
            max_sessions_per_region_before_all_regions_served=(
                facts.max_sessions_per_region_before_all_regions_served
            ),
            login_rate_sessions=facts.login_rate_sessions,
            login_rate_window_seconds=facts.login_rate_window_seconds,
            max_active_per_spawn_cell=facts.max_active_per_spawn_cell,
        )

    @staticmethod
    def consume_accepted_manifest(
        manifest: Mapping[str, object],
        schedule: Mapping[str, object],
        acceptance: Mapping[str, object],
        *,
        expected_target: int | None = None,
    ) -> PopulationManifestContract:
        """Validate and project an accepted manifest without binding identities.

        ``expected_target`` is optional so a future accepted CPI-196 target can
        be supplied by its governing decision record.  When omitted, the
        compiled schedule's target is authoritative.  The adapter never
        generates missing rows, fills IDs, or calls an external capability.
        """

        manifest_digest = _verify_compiled_artifact(manifest, "manifest_digest", "manifest")
        schedule_digest = _verify_compiled_artifact(schedule, "schedule_digest", "schedule")
        _reject_runtime_identifiers(manifest, "manifest")
        _reject_runtime_identifiers(schedule, "schedule")
        _reject_runtime_identifiers(acceptance, "acceptance")

        _require_exact_keys(manifest, _MANIFEST_FIELDS, "compiled population manifest")
        _require_exact_keys(schedule, _SCHEDULE_FIELDS, "compiled CPI-196 schedule")

        _require_int(manifest.get("manifest_schema"), "manifest_schema", 1, 1)
        _require_int(schedule.get("schema_version"), "schedule.schema_version", 2, 2)
        _require_token(manifest.get("manifest_version"), "manifest_version")
        schedule_version = _require_token(schedule.get("schedule_version"), "schedule_version")
        realm_id = _require_token(schedule.get("realm_id"), "schedule.realm_id")
        if manifest.get("realm_id") != realm_id:
            raise PopulationPolicyError("manifest and schedule realm_id values must match")
        if manifest.get("schedule_digest") != schedule_digest:
            raise PopulationPolicyError("manifest does not bind the supplied schedule")

        target = _require_int(schedule.get("permanent_roster_target"), "schedule.permanent_roster_target", 1)
        if expected_target is not None:
            _require_int(expected_target, "expected_target", 1)
            if target != expected_target:
                raise PopulationPolicyError("compiled schedule target differs from expected target")

        maximum_owner_level = _require_int(
            schedule.get("maximum_owner_level"), "schedule.maximum_owner_level", 1, 80
        )
        raw_waves = schedule.get("waves")
        if not isinstance(raw_waves, list) or len(raw_waves) != maximum_owner_level:
            raise PopulationPolicyError("compiled schedule waves must cover every owner level")
        wave_counts: list[int] = []
        for ordinal, raw_wave in enumerate(raw_waves):
            if not isinstance(raw_wave, dict):
                raise PopulationPolicyError(f"schedule.waves[{ordinal}] must be an object")
            _require_exact_keys(raw_wave, _WAVE_FIELDS, f"schedule.waves[{ordinal}]")
            owner_level = _require_int(
                raw_wave.get("owner_level"), f"schedule.waves[{ordinal}].owner_level", 1, 80
            )
            if owner_level != ordinal + 1:
                raise PopulationPolicyError("compiled schedule waves must be sorted and contiguous")
            wave_counts.append(_require_int(raw_wave.get("count"), f"schedule.waves[{ordinal}].count", 0))
        if sum(wave_counts) != target:
            raise PopulationPolicyError("compiled schedule wave counts must equal permanent_roster_target")
        if any(left < right for left, right in zip(wave_counts, wave_counts[1:], strict=False)):
            raise PopulationPolicyError("compiled schedule wave counts must be non-increasing")

        acceptance_id, approver, approved_at_utc = _validate_acceptance(
            acceptance, manifest_digest, schedule_digest
        )

        # Re-run the existing immutable compiler over the digest-verified
        # payload.  This checks legality/composition/wave ordinals without
        # introducing a second identity or persistence implementation.
        manifest_payload = {
            key: value for key, value in manifest.items() if key not in {"manifest_digest", "reports"}
        }
        recompiled = compile_population_manifest(manifest_payload, schedule)
        if recompiled.get("manifest_digest") != manifest_digest:
            raise PopulationPolicyError("manifest does not match the pinned immutable compiler")

        raw_identities = manifest.get("identities")
        if not isinstance(raw_identities, list) or len(raw_identities) != target:
            raise PopulationPolicyError("compiled manifest identity count must equal permanent_roster_target")

        references: list[PopulationIdentityReference] = []
        for ordinal, raw_identity in enumerate(raw_identities):
            if not isinstance(raw_identity, dict):
                raise PopulationPolicyError(f"manifest.identities[{ordinal}] must be an object")
            _require_exact_keys(raw_identity, _IDENTITY_FIELDS, f"manifest.identities[{ordinal}]")
            identity = _require_token(
                raw_identity.get("manifest_identity"), f"manifest.identities[{ordinal}].manifest_identity"
            )
            wave_level = _require_int(
                raw_identity.get("creation_wave_owner_level"),
                f"manifest.identities[{ordinal}].creation_wave_owner_level",
                1,
                maximum_owner_level,
            )
            wave_ordinal = _require_int(
                raw_identity.get("wave_ordinal"), f"manifest.identities[{ordinal}].wave_ordinal", 1
            )
            eligible = _require_bool(
                raw_identity.get("admission_eligible"), f"manifest.identities[{ordinal}].admission_eligible"
            )
            references.append(
                PopulationIdentityReference(
                    manifest_identity=identity,
                    creation_wave_owner_level=wave_level,
                    wave_ordinal=wave_ordinal,
                    admission_eligible=eligible,
                )
            )

        return PopulationManifestContract(
            realm_id=realm_id,
            manifest_digest=manifest_digest,
            schedule_digest=schedule_digest,
            schedule_version=schedule_version,
            durable_target=target,
            wave_counts=tuple(wave_counts),
            identity_references=tuple(references),
            acceptance_id=acceptance_id,
            approver=approver,
            approved_at_utc=approved_at_utc,
        )


__all__ = [
    "CPI196_GLOBAL_PHASE_CAP",
    "CPI196_MANIFEST_SHA256",
    "CPI196_SCHEDULE_SHA256",
    "CPI225ReleaseSurfaceBinding",
    "IdentityIndependentPopulationPlan",
    "PopulationIdentityReference",
    "PopulationManifestAdapter",
    "PopulationManifestContract",
    "PopulationWavePlan",
]
