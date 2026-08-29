# ADR 0027: Phase 1 Offline Backtest Product Boundary

Date: 2026-08-29

## Status

Accepted

This ADR supersedes earlier Phase 1 exit assumptions only where they require automatic
reconciliation correction, ancestry repair, or exhaustive recovery combinations before the first
usable release. It does not weaken the accepted time-visibility, canonical identity, audit,
ledger, idempotency, durability, or fail-closed invariants in ADRs 0008, 0020, 0022, and 0024.
Already implemented canonical contracts remain valid; product capabilities not selected below are
dormant and move to Phase 1.1.

## Context

The repository has a deep deterministic execution, audit, ledger, and recovery foundation but no
installed end-to-end backtest product. The only public command is `ea doctor`. Phase 1 work became
serialized behind increasingly exhaustive reconciliation and recovery proofs, including Issue #97
and its long-lived PR #105 candidate, while initial funding, scenario input, installed execution,
and a user report remained absent.

Phase 1 now needs a mature offline backtest product delivered as an installed distribution: a user can install a wheel, validate one
bounded scenario, run or resume it, and receive deterministic economic and audit evidence. The
product must fail closed at evidence conflicts, but it need not automatically repair every
conflict or enumerate every theoretical recovery interleaving before v0.2.0.

## Decision

### Product outcome

Phase 1 delivers one deterministic, mature offline backtest product with:

- strict local OHLCV input and UTC replay visibility;
- one instrument specification and settlement currency;
- `always-flat-v1` and `bounded-long-v1` sample strategies;
- strictly positive, quantized initial cash and no initial positions;
- bounded order, position, and notional risk;
- canonical Order, Fill, ledger, audit, risk, and terminal evidence;
- installed-distribution execution outside a Git checkout;
- supported interruption recovery at the declared durable boundaries; and
- a stable JSON and text result report.

The public product surface is `ea backtest validate`, `ea backtest run`, and
`ea backtest resume`. The existing Git launcher remains development evidence and is not the
installed product boundary.

### Manifest and funding

New product runs use `RunManifest v2`. It binds installed-distribution provenance, the strict
scenario and data lineage, instrument specification, master seed, and the canonical initial
funding transition. The funding transition is a manifest-bound genesis transaction owned by the
ledger, is applied exactly once, and returns the original result on exact replay. Different input
for an occupied identity is a conflict and fails closed.

RunManifest v1 remains supported for read-only verification of historical evidence. New CLI runs
and resume operations never create, adopt, or mutate v1 attempts. There is no implicit v1-to-v2
migration.

### Reconciliation and failure boundary

The Phase 1 product reconciliation rule is **detect, audit, report, and stop**. A mismatch,
unresolved Fill, duplicate/conflicting economic fact, ledger conflict, incomplete audit chain,
corrupt journal, or manifest/scenario drift produces a stable failure code and cannot create a
successful terminal state or success report.

There is no automatic reconciliation correction, balance adjustment, ancestry repair, silent
ledger overwrite, or retry after an ambiguous effect in the Phase 1 product. Existing dormant
adjustment contracts are not exposed by the CLI. Their production authorization and product
behavior belong to Phase 1.1.

### Recovery support

Phase 1 supports recovery only for the declared v2 installed-product path and its explicitly
tested durable interruption boundaries. Recovery re-verifies the manifest, scenario, data,
funding, journal, ledger, audit, risk state, and terminal frontier before continuing. A supported
resume must finish with ledger, audit chain, risk state, and terminal semantics equivalent to the
uninterrupted run.

Unknown, inconsistent, corrupt, v1, or unsupported recovery state fails closed with retained
evidence. It is not automatically repaired, deleted, overwritten, or retried. Exhaustive recovery
of all dormant correction and ancestry combinations is Phase 1.1.

### Capability confinement

Phase 1 contains no live or paper trading, broker/exchange adapter, network order write,
credential resolution, automatic retry, adjustment command, ancestry-repair command, API service,
or real Web adapter. The Web UI remains a deterministic Mock Adapter. Release tag and publication
require a separate explicit Human Owner authorization after clean-main acceptance.

### Ordered delivery

The implementation is split into four ordered deliveries:

1. authority, workflow, and CI convergence;
2. initial funding, RunManifest v2, and fail-closed kernel;
3. sample strategies, strict scenario v1, and end-to-end CLI; and
4. deterministic reporting, isolated wheel acceptance, and v0.2.0 release readiness.

The older #76/#97/#98/#99 topology is preserved as history and superseded for Phase 1. Its
unimplemented automatic correction and complex recovery scope moves to Phase 1.1. PR #105 is not
rebased or merged; later deliveries may reuse only independently selected code that is re-proven
against their own exact candidates.

## Validation

Phase 1 acceptance must prove:

- funding quantization, currency, exact replay, conflict, audit failure, and recovery replay;
- flat and bounded-long successful paths with point-in-time data and deterministic economics;
- rejection of future data, risk violations, unresolved ancestry, duplicate Fill, ledger
  conflict, corrupt journal, and manifest/scenario drift without a success terminal;
- installed wheel execution from a temporary directory with no Git checkout;
- exact-run byte-stable reports and cross-run semantic outcome equality; and
- resumed and uninterrupted supported runs have equivalent economic and audit outcomes.

## Consequences

- Phase 1 can converge on user-visible value without weakening its safety floor.
- Failure evidence becomes a supported product outcome instead of a reason to silently repair.
- Automatic correction, ancestry repair, and broader recovery research remain explicit Phase 1.1
  work rather than hidden v0.2.0 blockers.
- Initial funding and installed provenance become first-class canonical inputs.
- The release remains offline-only and cannot be interpreted as paper/live readiness.

## Rejected Alternatives

### Finish every reconciliation adjustment before a usable backtest

Rejected because dormant repair authority is not required to safely detect and stop, while the
product lacks funding, a CLI, and a report.

### Treat a mismatch as warning-only

Rejected because it could emit a successful result from an economically untrusted ledger.

### Require a Git checkout for provenance

Rejected because the deliverable is an installed product. RunManifest v2 records distribution
provenance without treating repository presence as a runtime requirement.

### Rewrite or merge PR #105

Rejected because it is divergent, oversized, and bound to superseded Phase 1 scope. History is
retained; reuse requires new task authority and fresh evidence.
