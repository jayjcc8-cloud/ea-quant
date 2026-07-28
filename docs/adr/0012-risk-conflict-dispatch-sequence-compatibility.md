# ADR 0012: Risk Conflict Dispatch-Sequence Compatibility

Date: 2026-07-26

## Status

Proposed

## Context

Accepted ADR 0008 establishes the general exact-non-negative sequence convention and allows the
runtime to assign a dispatch sequence after choosing a causal root. The shipped v1
`create_order_intent` factory and canonical decoder concretely apply that convention to
`OrderIntent.dispatch_sequence` with no uint64 upper bound. Accepted ADR 0011 freezes those
canonical values for replay and requires the risk authority to classify exact replay or
same-ID/different-bytes conflict before validating a new input. A conflict must engage the
monotone halt atomically and copy the submitted intent's causal-root time and dispatch sequence
into `RiskStateSnapshot`.

ADR 0011 also describes `RiskStateSnapshot.halt_dispatch_sequence` and the public
`engage_halt` command as uint64. Those requirements cannot all hold for an already-valid intent
whose dispatch sequence is greater than `2^64 - 1`.

Issue #45 implementation review exposed the concrete failure. Candidate `5113b95` checked the
uint64 range before replay lookup. That caused an exact replay with a larger canonical dispatch
sequence to fail and let a same-ID/different-bytes submission return `OUT_OF_RANGE` without the
mandatory identity-conflict halt. `ARCH45-IMPL-001` and `RISK45-IMPL-001` classify this as a
blocker.

Changing the accepted v1 `OrderIntent` domain would affect every message producer and reader,
reject values that are canonical today, and require a versioned compatibility decision. The risk
state's canonical integer encoder already supports the complete existing non-negative domain, so
the smaller compatible correction is at the risk halt boundary.

## Decision

This ADR narrowly supersedes ADR 0011 wherever it restricts a halt dispatch sequence to uint64.
It does not change any other ADR 0011 policy, schema field, canonicalization identifier, digest
domain, state transition, or precedence rule.

### Exact dispatch domain

`RiskStateSnapshot.halt_dispatch_sequence` is either `null` in version-zero state or an exact
non-negative Python `int` in a halted state. It has no artificial uint64 upper bound. Exact runtime
type remains mandatory: `bool`, integer subclasses, strings, floats, and coercion are rejected.

The public
`engage_halt(reason, causal_root_available_at, dispatch_sequence)` command accepts the same exact
non-negative integer domain. A negative value returns
`RiskAuthorityError(OutcomeCode.OUT_OF_RANGE)`; a non-exact-int value returns
`RiskAuthorityError(OutcomeCode.INVALID_TYPE)`.

The identity-conflict transition copies the submitted canonical `OrderIntent.dispatch_sequence`
exactly, regardless of magnitude. No truncation, clamping, modulo operation, hash projection,
sentinel, or replacement sequence is permitted.

### Replay and conflict precedence

`evaluate()` performs:

1. exact public `OrderIntent` and `PortfolioSnapshot` runtime-type validation;
2. canonical intent bytes and digest;
3. replay-index lookup and byte-authoritative exact-replay or identity-conflict classification;
4. structural validation only for a new intent identity.

There is no dispatch-range check between canonical encoding and replay lookup. Exact replay returns
the original `RiskEvaluationResult` for every dispatch value already accepted by `OrderIntent`.
Same ID with different canonical bytes always enters the atomic identity-conflict halt path,
including when the submitted dispatch sequence is unusually large or the two colliding digests
are equal.

Only after a lookup proves that the identity is new does the authority revalidate
`dispatch_sequence` as an exact non-negative integer before ordinary structural and policy
evaluation. This protects the public boundary from forged objects without permitting invalid
new-input structure to bypass byte-authoritative conflict classification.

### Canonical compatibility

The `RiskStateSnapshot` JSON field set, schema version `1`, canonicalization
`ea-risk-state-v1`, and digest domain `b"ea.risk-state.v1\0"` remain unchanged. This Issue has not
merged or released the new risk-state schema: no supported v1 risk-state writer or reader has
shipped, and no supported persisted v1 risk-state artifact exists. That is a precondition for
retaining version `1`. The existing strict canonical integer encoder already produces the
normative base-10 representation for every accepted non-negative integer. This correction aligns
the first shipped v1 risk-state domain with the pre-existing v1 `OrderIntent` domain; it does not
reinterpret any previously published risk-state artifact.

### Required regression and atomicity evidence

Issue #45 must add:

- a new intent with dispatch `2^64` that evaluates under the ordinary policy;
- generated intents and risk states with a much larger exact non-negative dispatch value, not only
  the `2^64` boundary, to demonstrate the unbounded domain;
- exact replay of that intent after newer risk/snapshot state with the same original object and no
  allocation;
- an occupied intent ID followed by a differing submitted intent whose dispatch is `2^64`, proving
  that one atomic identity-conflict halt records the submitted integer exactly;
- an unusually large conflict submitted after the authority is already halted, proving that the
  first halt state remains byte-identical;
- new-identity rejection of a forged negative or non-exact dispatch only after replay/conflict
  lookup, without mutation;
- public halt at dispatch `2^64` and a much larger value, plus rejection without coercion of
  negative integers, `bool`, integer subclasses, floats, and strings with the specified outcome
  codes;
- canonical risk-state bytes and digest golden vectors above uint64, including cross-process
  equality with `2^64` and a much larger exact non-negative value;
- pre-publication failure injection across intent and portfolio-snapshot encoding and digest,
  policy, risk state, decision, approval, evidence, evaluation result, copied replay/aggregate
  state, and every public-value canonical-byte and digest construction boundary;
- failure injection on ordinary result registration, public halt, and identity-conflict halt,
  proving identical aggregate-state identity, counters, indexes, and histories whenever
  publication does not complete and proving that the injected exception propagates;
- exact-candidate-SHA Architecture Owner and Risk Owner re-review that explicitly closes
  `ARCH45-IMPL-001` and `RISK45-IMPL-001`; owner-ID uint64 behavior remains out of scope and
  unchanged.

## Consequences

- Every canonical v1 `OrderIntent` remains evaluable, replayable, and conflict-halting.
- Identity conflict cannot bypass halt through a dispatch-range mismatch.
- Risk-state canonical bytes remain deterministic across process integer limits, locale, timezone,
  hash seed, and ambient Decimal context.
- Owner-ID sequences remain uint64 with the existing `int | None` exhaustion model; this ADR does
  not broaden economic ID counters.
- Runtime ordering and dispatch allocation policy remain future lifecycle work. This ADR only
  preserves the domain already carried by canonical messages.

## Rejected alternatives

### Narrow `OrderIntent.dispatch_sequence` to uint64

Rejected for this iteration because it changes an accepted v1 message domain and every producer,
codec, test vector, and downstream consumer to solve a risk-state mismatch.

### Validate dispatch range before replay lookup

Rejected because it violates byte-authoritative replay/conflict precedence and creates an
identity-conflict halt bypass.

### Clamp or hash a large dispatch sequence in risk state

Rejected because risk-state evidence must retain the exact submitted causal identity and cannot
silently replace it with a lossy projection.
