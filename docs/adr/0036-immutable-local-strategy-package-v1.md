# ADR 0036: Immutable Local Strategy Package V1

Date: 2026-09-08

## Status

Proposed — Issue #185

## Decision

Research Foundation accepts one explicitly trusted local Python implementation per immutable
`.eastrategy` artifact. The deterministic ZIP contains only canonical `manifest.json` and UTF-8
`strategy.py`, in that order, with fixed 1980 timestamp, Unix regular-file mode 0444, stored
compression and empty metadata. Each member is limited to 1 MiB, artifacts to 2 MiB + 4096 bytes,
and explicitly configured absolute roots to 100 immediate artifacts. No recursive discovery,
symlinks, extra members, remote URLs, upload, dependencies or installation is supported.

`ea.strategy.sdk_v1` exposes immutable admitted-bar projections with exact Decimal prices/volume,
read-only validation facts, canonical parameter values, and a single target-quantity decision.
The artifact exports fixed `validate_parameters` and `create_logic` symbols. The existing
ParameterV1 schema supplies generic controls. Local decisions traverse existing active-market,
signal, risk, order, matcher, Fill, ledger, audit and report authorities. They cannot request exits,
shorts or multiple trades through this seam. Invalid decisions fail before signal/order effects.

This is trusted executable local Python, not a sandbox. The SDK intentionally exposes no runtime,
portfolio, data stream or filesystem authority; Python itself is not restricted. Users must trust
every artifact placed in the configured root.

StrategyPackageIdentityV1 binds package ID and SHA-256 of the entire container. The additive
ResearchStrategyCatalogV1 rejects duplicate package/strategy IDs and built-in shadowing. It resolves
local implementations only by exact package ID, digest, strategy ID and version; no latest fallback.
Built-in registry authority is unchanged.

Scenario V3 binds that source identity and the entire canonical parameter map in its own digest
domain. V1/V2 serialization is unchanged. Existing lineage and semantic report chains include
the scenario and complete strategy document, thereby binding code bytes without a new authority.

Loading captures bytes once and validates/hash-binds those bytes. Web jobs preserve the exact
artifact as read-only input evidence before dispatch; attempts preserve it before execution.
Execution compiles captured bytes, never rereads the external artifact. Reopen/report reconstructs
from frozen attempt evidence. Holdout obtains the source job's frozen artifact, validates target
configuration and chronology, and freezes all source parameters. Its relationship remains the
minimal ADR0034 relation; target defaults never override source parameters. Current external
artifact changes/deletion cannot upgrade or replace the source implementation.

History identifies package and digest. Parameter reuse carries an expected source identity and
rejects replacement. Batches bind one base scenario including one code digest. Comparison presents
an implementation change instead of parameter deltas when code digests differ.

## Validation

Deterministic pack, hostile container/path rejection, exact identity, SDK failures, unchanged
V1/V2, preserved bytes, frozen Holdout and restart tests accompany checkout-external non-editable
wheel CLI and real Chromium acceptance. Existing commercial Chromium remains bounded by 120s.
One independent T1 review and CI are required. No AI, dataset/candidate persistence, optimizer,
new economic/governance/recovery authority, #125 activation, release or deployment is included.
