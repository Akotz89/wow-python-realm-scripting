# Playerbots realm Python development layer

This workspace is the governed development surface for Linear **CPI-223**.  It
targets the exact runtime tuple below and deliberately refuses moving pins:

- AzerothCore/Playerbots core `9fb906bb7296212ff42fc95ff73a92aaf8554f0d`
- `mod-playerbots` `2f7d9f774987d0157c6a0d0cc08c40bec3db3945`
- `privatecore/mod-python-engine` `700a4f75a2e550f17022665d7b5b009c24e46a40`
- Fedora 44 Python 3.14 with Boost.Python 3.14

The engine is a convenience layer, not a second game server architecture.
Playerbots remains responsible for movement, combat, pathfinding, quests,
inventory actions, grouping, gearing, and encounters.  Python is only eligible
for a verified uncovered gap when the exact authoritative operations already
exist in the reviewed binding policy.

For a relocated checkout, set `WOW_PYTHON_TOOL_ROOT` to this package directory
and `WOW_PROJECT_ROOT` to the operations repository before invoking the wrapper.
The project root supplies the pinned authority and release-surface inputs; it
does not authorize deployment. Without these variables, the existing layout is used.
Native source integration tests also accept `WOW_POPULATION_SOURCE_ROOT` pointing
to the actual candidate source tree. Without it they use the existing project
work path; missing source fails those checks. Passing a path does not prove the
candidate is accepted or deployed.

## Development workflow

Create the isolated tool environment and run every local gate:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --requirement requirements-dev.lock
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/pytest
```

The repository wrapper performs the same operations from any working
directory: `scripts/wow-python-dev bootstrap`, `status`, `check`,
`build-empty`, or `build-smoke`.

Validate the offline contract against the current CPI-225 authority artifact
and generated release surface without configuring or building AzerothCore:

```bash
scripts/wow-python-dev validate
```

For an external historical evidence bundle, pass `--repo-root`, `--authority`,
`--surface-bundle` and `--engine-patch` paths to `scripts/wow-python-dev validate`
or directly to the validator.
The authority remains pinned to its original commit/path and SHA-256, and the
bundle and patch retain their existing integrity checks. Missing or changed
inputs fail validation; copying them to a source repository is not required.
When the original bundle or patch is absent, the default validator and tests
use the retained sibling `wow-project-archive` CPI-223 historical-input package
if available. Explicit input paths take precedence. A clean clone without the
archive or explicit inputs fails validation rather than substituting data.

For the release-contract tests, set `WOW_CPI223_AUTHORITY` to the authority
JSON file, `WOW_CPI223_SURFACE_BUNDLE` to the release-surface directory, and
`WOW_CPI223_ENGINE_PATCH` to the engine hardening patch. Unset variables use
their existing locations under `WOW_PROJECT_ROOT`. These test-only overrides
still validate the actual inputs and fail if they are missing or changed; they
do not alter the validator's command-line defaults.

Build an immutable no-feature release without contacting the live realm:

```bash
.venv/bin/python devtools/build_release.py \
  --manifest manifests/empty.json \
  --output dist/cpi223-empty-v1
```

Verify, install, and atomically select a release (the live target remains a
separate maintenance/promotion gate):

```bash
.venv/bin/python devtools/release_ctl.py verify dist/cpi223-empty-v1
sudo -n .venv/bin/python devtools/release_ctl.py \
  install dist/cpi223-empty-v1 --root /srv/wow/python
sudo -n .venv/bin/python devtools/release_ctl.py \
  activate cpi223-empty-v1 --root /srv/wow/python
```

`activate` preserves the old target as `previous`; `release_ctl.py rollback`
atomically swaps the two links.  Installation refuses symlinks, unexpected
files, hash/size mismatches, unsafe release names, and overwriting an existing
release directory.

For the controlled gameplay gate, substitute
`manifests/controlled-smoke.json`.  That manifest registers exactly one
`PLAYER_ON_LOGIN` callback and sends a visible PASS message.  It is a test
release, not the production default.

## Runtime layout

The intended installed layout is:

```text
/srv/wow/python/
  releases/<release>/
    manifest.json
    release-files.json
    engine-bootstrap/00_realm_bootstrap.py
    lib/realm_scripts/...
  current -> releases/<release>
  previous -> releases/<prior-release>
```

The worldserver configuration must use the absolute path
`Python.ScriptsPath = "/srv/wow/python/current/engine-bootstrap"`.  Its systemd environment
sets `WOW_PYTHON_RELEASE_ROOT=/srv/wow/python/current`.  The engine recursively
executes all Python files below `Python.ScriptsPath`, so feature packages,
tests, scratch code, and virtual environments must never live under the
engine-bootstrap directory.

Every feature is named explicitly in a sorted schema-2 release manifest.  Each
manifest binds the exact CPI-225 authority commit, generated release-surface
digest, exact core/Playerbots/engine tuple, engine source overlay, and disabled
embedded-runtime defaults.  The loader verifies the complete release inventory
before importing a feature or registering a hook, rejects unapproved hooks,
and reloads feature modules instead of silently reusing cached code.  A
generated `release-files.json` records every shipped file's SHA-256 and size;
the builder emits canonical JSON so equivalent inputs produce identical
manifest and inventory bytes.

## Current safety boundary

The upstream engine compiles and links on the exact stack, but the current pin
is not approved for arbitrary scripts:

- its hook registry advertises many events that have no forwarding script;
- all item callbacks are incorrectly dispatched as `ITEM_ON_USE`;
- GameObject damaged/destroyed callbacks are dispatched as add/remove-world;
- callback return values do not control the underlying C++ boolean hooks;
- recursive loading order is filesystem-dependent and shares one namespace;
- reload is non-transactional after hook clearing;
- the administrator `.python exec` command runs arbitrary code in worldserver;
- callbacks run in-process under the GIL and can stall or crash gameplay.

Accordingly, `policy.py` allows only a small set of low-frequency hooks whose
forwarding is correct at the pinned revision.  Adding a hook or mutation method
is a reviewed contract change, never an ad-hoc import.  Production remains
`Python.Enabled = 0` and `Python.AllowExecCommand = 0`.  The CPI-225 release
surface keeps `mod-python-engine` deferred/unloaded and SQL unapplied; the
standalone package cannot promote that engine or bind a PopulationDirector.
The controlled-smoke manifest is a test artifact only.

## Adding a feature

1. Confirm the upstream/community-first audit proves no accepted module owns
   the behavior.
2. Map every required authority operation to the pinned engine source.  If any
   operation is absent, stop and add a reviewed C++ binding or implement the
   behavior natively; do not approximate it in Python.
3. Add one module under `src/realm_scripts/features/` with a side-effect-free
   `bindings()` function returning declarative `Binding` objects.
4. Add focused fake-API unit tests, update the sorted release manifest, and run
   Ruff, mypy, and pytest.
5. Build a hash-inventoried release.  Promotion, enablement, hot reload,
   gameplay evidence, and rollback are separate operator gates.

## Offline population-policy compiler

The CPI-194/CPI-196 pure-policy lane lives in
`realm_scripts.population_policy`. It intentionally registers no AzerothCore
hooks. It currently provides:

- normalized-exponential wave allocation with exact integer total and a final
  deterministic monotonicity repair;
- canonical JSON and SHA-256 policy/schedule digests;
- exact epoch-fixed band-capacity apportionment;
- validation that ordered band ceilings cannot exceed the global phase cap;
- ordered, phase-bounded owner-level authorization calculations; and
- earned-only deterministic character ranking and dynamic band slicing.

The same module also validates a complete pre-authored identity manifest:

- case-insensitive unique manifest IDs and character names, plus canonical
  account-name spelling;
- one deterministic account name per ambient identity, with up to the exact
  nine non-Death-Knight class identities sharing each dedicated bot account;
- exact schedule wave counts and contiguous per-wave ordinals;
- non-Death-Knight WotLK race/class, faction, and starting-region legality;
- explicit appearance fields, mandatory `outfit_id=0` for the pinned core, and
  admission eligibility;
- reviewed spec-template catalogue matching each class and role intent;
- exact configured faction and role shares plus class/region floors;
- reports by faction, class, role, spec, region, wave, and eligibility; and
- a separate binding artifact that preserves unique identity/GUID pairs while
  requiring every shared manifest account name to resolve to one account ID;
  database IDs never mutate the immutable pre-authored manifest.

The pinned legality matrix was checked against
`data/sql/base/db_world/playercreateinfo.sql` at the exact core commit. Exact
appearance combinations and spec-template gameplay quality still require
database/client and focused gameplay validation; the compiler does not pretend
that numeric shape validation proves either.

The test fixture reproduces the contract's `N=10000`, `M=80`, `decay=0.97`
worked vector, but those numbers and its sample band shares/ceilings are **test
vectors, not approved production tuning**.

The v1 wire format remains a **non-production historical prototype** because it
embeds a positive `owner_anchor_guid` in the schedule, snapshot, installation
plan, SQL schedule row, and wave idempotency keys. The v2 implementation in
`realm_scripts.population_policy_v2` removes owner identity from all immutable
artifacts, defines each wave key from exactly `realm_id`, `owner_level`, and
`schedule_digest`, installs wave 1 with owner state explicitly `unbound`, and
compiles bind/rebind as a separate audited compare-and-swap transition that
authorizes zero waves. Later-wave reconciliation fails closed while unbound.
The v2 offline and disposable-MySQL gates do not constitute AzerothCore runtime
integration, gameplay acceptance, load acceptance, or live promotion.

Compile canonical JSON without registering or enabling an embedded feature:

```bash
PYTHONPATH=src .venv/bin/python devtools/compile_population_policy.py \
  <reviewed-policy-input.json> --output <policy-snapshot.json>

PYTHONPATH=src .venv/bin/python devtools/compile_population_manifest.py \
  <reviewed-manifest-input.json> --policy <compiled-policy.json> \
  --output <compiled-manifest.json>

PYTHONPATH=src .venv/bin/python devtools/compile_population_bindings.py \
  <sorted-database-binding-rows.json> \
  --manifest <compiled-manifest.json> \
  --output <compiled-bindings.json>

PYTHONPATH=src .venv/bin/python devtools/compile_population_runtime_snapshot.py \
  <reviewed-runtime-input.json> \
  --policy <compiled-policy.json> \
  --manifest <compiled-manifest.json> \
  --bindings <compiled-bindings.json> \
  --output <runtime-snapshot.json>

PYTHONPATH=src .venv/bin/python devtools/compile_population_install_plan.py \
  <reviewed-installation-version.json> \
  --snapshot <empty-binding-runtime-snapshot.json> \
  --policy <compiled-policy.json> \
  --manifest <compiled-manifest.json> \
  --output <population-install-plan.json>

PYTHONPATH=src .venv/bin/python devtools/render_population_install_sql.py \
  <population-install-plan.json> \
  --output <population-install.sql>

PYTHONPATH=src .venv/bin/python devtools/compile_population_admission_reconciliation.py \
  <reviewed-owner-and-capacity-observation.json> \
  --snapshot <runtime-snapshot.json> \
  --manifest <compiled-manifest.json> \
  --bindings <compiled-bindings.json> \
  --output <admission-reconciliation.json>

PYTHONPATH=src .venv/bin/python devtools/compile_population_admission_recovery.py \
  <reviewed-ledger-and-cross-pool-observation.json> \
  --reconciliation <admission-reconciliation.json> \
  --manifest <compiled-manifest.json> \
  --output <admission-recovery.json>

PYTHONPATH=src .venv/bin/python devtools/compile_population_owner_binding.py \
  <reviewed-owner-selection-and-current-state.json> \
  --installation <population-install-plan.json> \
  --ambient-bindings <compiled-bindings.json> \
  --output <owner-binding-transition.json>

PYTHONPATH=src .venv/bin/python devtools/compile_population_first_wave_coupling.py \
  <reviewed-factory-prediction-or-observation.json> \
  --schedule <compiled-schedule.json> \
  --manifest <compiled-manifest.json> \
  --output <first-wave-coupling.json>

PYTHONPATH=src .venv/bin/python devtools/compile_population_capacity_policy.py \
  <reviewed-capacity-policy.json> \
  --output <compiled-capacity-policy.json>

PYTHONPATH=src .venv/bin/python devtools/compile_population_capacity_policy.py \
  <measured-capacity-observation.json> \
  --policy <compiled-capacity-policy.json> \
  --output <classified-capacity-observation.json>

PYTHONPATH=src .venv/bin/python devtools/compile_population_band_policy.py \
  <reviewed-owner-relative-band-policy.json> \
  --output <compiled-band-policy.json>

PYTHONPATH=src .venv/bin/python devtools/compile_population_band_policy.py \
  <measured-band-transition.json> \
  --policy <compiled-band-policy.json> \
  --output <resolved-band-transition.json>
```

The shared commands select v2 when their input schema field is `2`; v1 remains
available only for reproducible historical tests. Production inputs must use
v2.

The first-wave coupling compiler closes only the offline equality proof. It
requires `schedule W[1] == wave-one manifest identity count == exact-manifest
factory prediction`, forbids account packing across creation waves, keeps the
initial online target below both W1 and a separately measured concurrent cap,
and at observation time requires the exact wave-one binding set. It explicitly
rejects the stock bulk-random factory as production authority. It does not
choose between the current 329 and 801 candidate counts or create a character.

The capacity-policy compiler validates a strictly increasing load ladder,
per-stage dwell, hysteresis, and ordered warning/pause/backoff thresholds. Its
classifier consumes already-measured metrics and emits a deterministic,
digest-bound recommendation. It never samples the host or controls a session;
the native C++ director remains the runtime authority. The current conservative
candidate is documented in
`research/cpi-194/capacity-thresholds-and-load-ladder-v1.md`.

The runtime snapshot compiler verifies all four source artifact digests before
binding them together. It rejects unknown input fields and invalid capacity
relationships, then records:

- the policy, schedule, immutable-manifest, and admitted-binding digests;
- the global phase and exact `min(global_phase_cap,dynamic_band_ceiling)` rule;
- ordered band definitions and every fixed owner-level epoch capacity;
- permanent identity and currently bound identity counts;
- distinct server-session, safety-reserve, concurrent-active, creation-batch,
  login-batch, map, zone, and starting-region budgets;
- the explicit held-bot activity whitelist;
- an immutable denial set covering XP, banked XP, and every quest mutation; and
- reject/fail-closed/retain-last-verified behavior for invalid, missing, or
failed-reload snapshots.

The installation-plan compiler closes the previously missing handoff between
that hash-only envelope and runtime admission. It requires a fresh snapshot
with zero existing bindings, independently re-verifies the policy, schedule,
manifest, and snapshot digests, and emits the complete immutable schedule,
owner state, all wave rows, and every pre-authored identity. Only wave 1 is
initially authorized; later waves remain planned. The artifact contains no
mutable recovery state and performs no database writes. Its normative wire
shape is `schemas/population-install-plan-v2.schema.json`; a separately
reviewed renderer emits one `START TRANSACTION` / `COMMIT` seed with plain
`INSERT` statements and no overwrite/upsert path. Reapplication therefore
fails on existing schedule state instead of silently changing an installed
realm. The Playerbots migration must be installed before applying that DML,
and director mode must remain disabled until installation read-back succeeds.

The normative immutable wire shapes are
`schemas/population-runtime-snapshot-v2.schema.json` and
`schemas/population-install-plan-v2.schema.json`. The independent C++20 proof
at `cpp/owner_independent_v2_verifier.cpp` canonicalizes and verifies the
Python-produced policy, snapshot, installation, reconciliation, and optional
binding transition. It rejects owner identity injected into any immutable
artifact, a non-v2 schema, a wrong schedule binding, an owner-dependent wave
key, any initial authorization beyond wave 1, an invalid unbound
reconciliation, and any binding transition that authorizes a wave.

This closes the offline wire-contract gap only. The future C++ director must
still persist the accepted version, cache per-character decisions, and remain
the synchronous authority for XP, quest, lifecycle, activity, and database
enforcement. The standalone verifier is not an AzerothCore module, runtime
enablement, or gameplay proof.

The offline reconciliation oracle additionally binds the exact runtime
snapshot, immutable manifest, and current per-character database bindings to one
owner-level observation. It calculates the monotonic high-water transition,
queues every newly crossed owner level in ascending order, and proposes at
most one configured creation batch from the earliest incomplete wave. A pause,
zero creation capacity, an ineligible due identity, a future-wave binding, or
an owner observation above the global phase cap fails closed or returns a
non-mutating hold decision. In v2, an unbound observation contains null owner
identity and owner level, keeps high-water at 1, and can admit only wave 1. Its
output contract is
`schemas/population-admission-reconciliation-v2.schema.json`.

The independent C++20 consumer proof at
`cpp/admission_reconciliation_verifier.cpp` verifies the canonical plan digest,
exact wire fields, source digests, monotonic owner high-water transition,
contiguous wave state and idempotency keys, derived pending totals, decision
priority, and bounded earliest-wave-only candidate batch. Its test accepts a
Python-produced plan and rejects both an undigested mutation and a
digest-valid semantic contradiction.

This planner is deliberately not a transaction coordinator. Its
`runtime_commit_required=true` marker means the future C++ director must commit
each legal director-ledger transition and verify auth-account,
character-database, and Playerbots-binding facts before activation. Those facts
span separate AC worker pools, so the full creation sequence is an idempotent,
crash-recoverable state machine rather than one cross-pool transaction.
Re-running the same observation and bindings is byte-stable and never invents
a wall-clock, login, restart, shelf-fill, or phase-promotion wave.

The per-identity recovery compiler is the next layer of that contract. It emits
exactly one local transition or one external side effect per pass, preserves
the ledger generation across external effects, requires read-back, and blocks
conflicting or unreserved external state. Its wire schema is
`schemas/population-admission-recovery-v2.schema.json`; the matching idempotent
MySQL 8.4 ledger schema is
`sql/playerbots_population_director_ledger_v2.sql`. It deliberately omits
`IF NOT EXISTS`, so applying it over v1 or a partial installation fails instead
of silently retaining owner-coupled columns. The immutable schedule contains
no owner ID. Mutable owner state stores an `unbound`/`bound` discriminator and
a nullable account/GUID pair protected by a check constraint; an append-only
audit table records every later bind/rebind generation. The v2 recovery
compiler reuses the forward-only v1 recovery state machine only after the
reconciliation artifact proves schema 2, then emits and re-hashes schema 2.

The pre-integration contract is hardened against the exact core behavior. Wave
rows durably store constrained admitted counts. Identity rows and recovery
observations bind exact gender and appearance. Account observations distinguish
a missing current-realm `realmcharacters` row from a present row and report its
character count. Character observations include persisted appearance, zero
played-time counters, and zero counts across all six quest-status tables. The
state machine can retry current-realm row initialization after account creation
and count refresh after character creation without advancing its CAS generation;
later missing, extra, dirty, or mismatched state fails closed.

The standalone C++20 consumer at `cpp/admission_recovery_verifier.cpp`
independently canonicalizes the recovery artifact, verifies its digest and
source bindings, reconstructs conflicts from the exact ledger/account/
character/RNDBOT observations, and derives the only legal next action. Its
cross-language tests accept Python-produced exact-account and clean-character
plans and reject an undigested mutation, a re-signed semantic contradiction,
and a re-signed rewarded-quest mutation.

D-051 packages the first review-only AzerothCore integration slice: the
versioned migration, narrow prepared-statement registry seam, asynchronous
schedule/active-binding cache, stock-factory suppression, and exact
account/GUID final login gate. It does not yet execute the recovery decisions;
account/character creation, generation CAS, audit and cross-pool read-back,
activation, coordinator merge/build, and crash-injection acceptance remain
pending. See the
[content-addressed patch manifest](../../research/cpi-194/review-patches/d051/MANIFEST.md).

The post-D-051 contract correction permits the accepted nine non-DK characters
per dedicated RNDBOT account. Account creation and RNDBOT classification are
shared account-level prerequisites; character identity, GUID, ledger state,
and activation remain independently verified per manifest identity.
