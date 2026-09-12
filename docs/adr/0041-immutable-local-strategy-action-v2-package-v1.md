# ADR 0041: Immutable Local Strategy Action V2 Package V1

Date: 2026-09-12

## Status

Accepted — Issue #198, implementing the Product Owner's EA_NEXT_PHASE_PLAN_V1.

## Decision

Add independent `StrategyPackageV2`, identified as `ea-strategy-package-v2` in CLI inspection,
with manifest `schema_version: 2`. Preserve Package V1 and SDK V1. Reuse ADR 0036's exact
container encoding, size/path restrictions, captured source bytes, full artifact SHA-256 and
`StrategyPackageIdentityV1`. Container identity remains version-neutral; the hashed manifest
binds the package version. No new store or identity authority is needed.

The closed manifest contains `schema_version`, `package_id`, and `strategy`. Strategy contains
`id`, positive integer `version`, `display_name`, generic ParameterV1 `parameters`,
`action_contract: V2`, and `position_lifecycle: single-long-round-trip-v1`. It excludes
`outcome_mode`. Reject unknown fields, action contracts, lifecycles, noncanonical JSON/container
bytes, duplicate/shadowed IDs (including built-in V2), and version/route mismatches.

The trusted local module exports `validate_parameters(parameters, context)` and
`create_logic(parameters)`. Reuse the immutable SDK V1 admitted bar and validation context,
and existing SDK V2 `PositionViewV2`, `StrategyActionV2` and closed action decoder.
`logic.on_bar(bar, position)` returns HOLD, ENTER_LONG with positive canonical quantity, or
EXIT_LONG with no quantity. Parameter names are strategy-defined; no reserved quantity name.
Captured bytes are executed, never the latest source file. This is trusted Python, not a sandbox.

Only Scenario V4 accepts Package V2. Its optional local `source` binds package ID and artifact
SHA alongside strategy/version/complete normalized parameters/action/lifecycle. Omit `source`
entirely for built-in V4, preserving its canonical bytes. Scenario V3 accepts Package V1 only;
Scenario V1/V2/V3 and built-in V1/V2 retain their existing behavior.

For local V2 entry, the validated action quantity becomes the existing portfolio policy target,
following the established local V1 route. Existing planning, risk, Order, matcher, Fill, ledger,
commission and reconciliation owners still decide and settle execution. Preserve V2 invocation
timing, pending-order suppression, one entry/full exit, and no reentry. No new recovery frontier.
Local V2 report verification reconstructs the existing closed risk decision from authorized Order
fields and checks its pinned risk-decision hash, including allow/resize. It cannot infer requested
quantity from a fixed parameter name. Built-in verification remains unchanged. No result/report
schema or economic authority is added.

Existing Web registration lists V1/V2 strategies through their registered scenarios; generic
controls use descriptors. Choosing a Local V2 scenario selects V4 automatically. Preserve declared
integer maxima while applying the Web safe-integer ceiling. No strategy IDE or implicit scanning.
Jobs and attempts freeze exact artifact bytes; Batch remains membership over ordinary jobs.
Holdout loads the source job's frozen package, retains complete normalized parameters and V2
lifecycle, and changes only the allowed later data/window. Target defaults and external package
replacement cannot override it. Persisted report/path/job evidence reopens after source removal.

## Validation and scope

Tests exercise deterministic/tampered artifacts, invalid routes/actions, stateful moving-average
crossover flat/open/closed outcomes, quantity precision, risk resize/hash mismatch, identities,
frozen Batch/Holdout and restart. The example tracks only bars supplied under the existing V2
callback timing; pending Fill roots are not callbacks. It is an authoring example, not a claim
about profitability or a new built-in mechanism.

One final full verifier, an external non-editable wheel Chromium flow, and one independent T1
review accompany this single implementation PR. Preserve legacy tests. #125 is closed not-planned
and remains historical provenance; PR #105 is unmerged. No Candidate, optimizer, multi-round-trip,
new metrics, short/partial orders or exits, dependency bundle/install, remote code, sandbox,
generic runtime, recovery system, governance expansion, Paper/Live, release or deployment.
