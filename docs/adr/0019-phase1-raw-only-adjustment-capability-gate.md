# ADR 0019: Phase 1 Raw-Only Adjustment Capability Gate

- Status: Accepted
- Date: 2026-08-01
- Decision owners: Architecture, Data, Execution, Backtest, Runtime
- Related: ADR 0004, ADR 0008, ADR 0015, ADR 0016, ADR 0018, Issue #59

This ADR narrowly supersedes ADR 0018 only where ADR 0018 says that a valid non-raw market root is
observable by the Phase 1 matcher and therefore must produce a replay-stable empty dispatch batch.
All other ADR 0018 decisions remain Accepted and unchanged.

## Context

Accepted ADR 0004 deliberately defines the canonical `Adjustment` vocabulary as exactly `RAW`.
It prohibits an ambiguous `ADJUSTED` value and permits a future adjustment mode only after its
factor, basis date, OHLC transformation, volume transformation, and reproducibility lineage are
defined. It also makes adjustment part of logical market-record identity rather than a revision.

Accepted ADR 0018 correctly requires `adjustment == raw` for a Phase 1 historical Fill. It also
says that correction and non-raw roots are observed as replay-stable empty matcher batches and
requires a non-raw no-fill test. Correction roots are representable because revision is an open
non-negative integer. A valid non-raw root is not representable: `Adjustment` is a closed enum with
only `RAW`, and canonical construction and decoding reject every other value before runtime
admission or matcher dispatch.

The implementation review for Issue #59 identified this as `EXEC59-IMPL-006`. Adding a placeholder
enum member merely to make the test constructible would silently define new market-data identity
without the semantics required by ADR 0004. Treating a mutated string as a valid non-raw root would
instead weaken canonical validation. Neither is an acceptable way to satisfy an executable test.

## Scope

This decision freezes:

- the Phase 1 adjustment capability set;
- the boundary at which unsupported adjustment values fail;
- the narrow interpretation of ADR 0018 eligibility and verification while the vocabulary is
  raw-only; and
- the contract required before a future adjusted series can enter canonical market data or the
  matcher.

## Non-goals

This decision does not define:

- a split-, dividend-, total-return-, back-, forward-, or continuously adjusted series;
- a corporate-action event, factor store, basis date, transformation algorithm, or source adapter;
- adjusted OHLC, volume, availability, revision, provenance, or reconciliation semantics;
- coexistence or selection policy between raw and adjusted series;
- a dependency, lockfile change, migration, or external data integration; or
- any matcher price, fill, expiry, identity, sequencing, replay, or halt behavior other than the
  unsupported-adjustment boundary clarified here.

## Reuse decision

The needed capability is a closed capability gate, not an adjusted-data engine. The Issue #59
reuse assessment was extended on 2026-08-01 by inspecting Accepted ADR 0004, the existing
`Adjustment` enum and canonical market-data codec, ADR 0015 source decoding, ADR 0016 runtime
admission, and ADR 0018 matcher eligibility.

1. **Selected: reuse the existing closed `Adjustment.RAW` vocabulary and canonical validators.**
   This preserves the accepted identity, source, runtime, and matcher boundaries without a new
   dependency, migration, or supply-chain surface.
2. **Rejected: add a semantic-free `ADJUSTED` or `NON_RAW` enum member.** It would create a durable
   identity value without factor, basis, transformation, or lineage semantics and would contradict
   ADR 0004.
3. **Rejected: adopt a corporate-action or dataframe adjustment library for this Issue.** A library
   cannot choose EA's basis date, point-in-time factor availability, canonical identity, revision,
   and provenance policy. Such adoption would expand Issue #59 beyond a matcher and still require a
   separate Tier 2 contract and adapter.

Decision: reuse the existing closed capability locally. No dependency or lockfile changes.

## Decision

### Closed Phase 1 capability

For Phase 1, the complete valid canonical adjustment set is:

```text
Adjustment = { raw }
```

`Adjustment.RAW` means the source/venue prices and instrument-native traded quantity described by
ADR 0004. There is no canonical non-raw sentinel, placeholder, alias, or forward-compatible
unknown value. Enum names and wire values outside this set are invalid canonical market data.

The closed set is a capability statement, not a claim that adjusted data is equivalent to raw
data. Absence of an adjusted mode must never be encoded as `raw`, revision greater than zero, a
source-name convention, provenance text, or an untyped string.

### Fail-closed boundary

Unsupported adjustment values fail during canonical market-envelope construction or decoding.
They cannot receive a canonical market-record digest, enter the historical source selection,
become a runtime root, obtain an active-dispatch proof, or reach a matcher observation call.

If an in-memory object is maliciously mutated after canonical construction, every public decoder,
runtime verifier, and matcher boundary that receives it must reject it as invalid canonical
evidence. Rejection publishes no receipt, dispatch batch, fact, ingress, conflict, sequence
advance, or pending-order mutation. It is not an observed empty batch and cannot be retained as a
replay result.

### Narrow supersession of ADR 0018

ADR 0018 eligibility item 5 remains unchanged: a Fill requires `adjustment == raw`.

Until a later Accepted ADR expands the canonical adjustment set and explicitly declares matcher
compatibility, ADR 0018's statements about non-raw roots are narrowed as follows:

- “non-raw roots are observed and recorded in an empty dispatch batch” is not an executable Phase 1
  case because no such valid root exists;
- the Phase 1 non-raw verification obligation is satisfied by proving the vocabulary is exhaustively
  raw-only and that every unsupported or mutated adjustment fails closed before matcher publication;
- no test may bypass canonical construction, forge an active proof, or weaken a decoder merely to
  manufacture a nominally valid non-raw root; and
- correction roots remain valid and continue to produce replay-stable empty batches exactly as ADR
  0018 requires.

This supersession removes no valid Phase 1 behavior. It aligns the matcher contract with the
canonical market-data universe that ADR 0018 already names as an upstream authority.

### Future adjusted-data contract

A future ADR may add one or more explicit adjustment modes only if it defines, for each mode:

1. the factor source, formula, precision, effective time, knowledge/availability time, and basis
   date or direction;
2. exact open/high/low/close and volume transformations, including rounding and missing actions;
3. canonical wire value, logical record identity, digest, provenance, source selection, revision,
   and correction behavior;
4. coexistence, deduplication, and reconciliation rules between raw and every adjusted series;
5. bounded no-look-ahead behavior when factor knowledge changes after the market event;
6. whether the historical runtime admits that mode and whether the matcher rejects it before
   dispatch or observes it as a replay-stable empty batch; and
7. migration, compatibility, golden-vector, property, cross-process, and exact-SHA review evidence.

Adding an enum member before that ADR is Accepted is prohibited. A later ADR that authorizes an
adjusted mode must state whether it supersedes this closed capability set and the exact affected
ADR 0018 clauses.

## Verification

Issue #59 evidence must prove on the final exact SHA:

- `Adjustment` enumerates exactly `RAW`, with wire value `raw`;
- canonical construction and decoding reject unknown strings and wrong runtime types;
- malicious post-construction adjustment mutation is rejected before any matcher publication or
  state/sequence change;
- no adapter, source, runtime, or matcher code constructs an adjustment outside the closed enum;
- raw revision-zero roots retain the ADR 0018 first-eligible Fill behavior;
- raw correction roots retain the ADR 0018 replay-stable empty-batch behavior; and
- Architecture, Data/Execution/Backtest, and Verification Owners bind their verdicts to the exact
  candidate SHA.

## Consequences

- Phase 1 remains honest about supporting raw market data only.
- Issue #59 can close without inventing corporate-action semantics or accepting malformed market
  roots as valid matcher input.
- Unsupported adjusted data fails earlier than matching and therefore cannot create an ambiguous
  empty-batch audit record.
- A future adjusted-data implementation has an explicit Tier 2 design and migration gate.
- ADR 0018's remaining matcher ownership, eligibility, price, fact, expiry, replay, and coordinator
  contracts are unchanged.

## Alternatives rejected

### Add one adjusted enum member only for testing

An enum value is canonical identity, not a test fixture. Without transformation and lineage
semantics it would create apparently valid but uninterpretable market records.

### Let the matcher accept arbitrary adjustment strings

This would bypass ADR 0004 construction and digest invariants, make active-root proofs forgeable,
and turn invalid evidence into durable replay state.

### Remove adjustment from matcher eligibility

Raw and future adjusted series have distinct economics and identity. Removing the predicate would
allow a future vocabulary expansion to change fills silently.

### Implement adjusted market data inside the matcher

Adjustment belongs to canonical data production and point-in-time lineage. A simulated venue must
not own corporate-action transforms or inspect factor sources.
