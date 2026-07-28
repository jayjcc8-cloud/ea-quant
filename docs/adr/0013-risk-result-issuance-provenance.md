# ADR 0013: Risk-Result Issuance Provenance at Order Creation

Date: 2026-07-28

## Status

Proposed

## Context

Accepted ADR 0011 makes an exact `RiskEvaluationResult` mandatory at the future OMS boundary and
requires Execution to prove its decision, approval, evidence, policy, snapshot-version, risk-state,
dispatch, and allocation bindings before creating an `Order`. Issue #47 implemented those
canonical coherence checks in a deterministic `Phase1OrderAuthority`.

The checks are necessary but do not prove that the bound `Phase1RiskAuthority` actually issued the
result. The dependency-neutral low-level factories can construct an internally coherent
`RiskDecision`, `RiskEvaluationEvidence`, and `RiskEvaluationResult` with valid-looking owner
sequences and the exact frozen policy digest. For example, a forged `ALLOW / WITHIN_LIMITS` result
can approve more than the policy's `maximum_order_quantity`. More generally, a forged result can
claim an arbitrary portfolio-snapshot digest and a position-derived reason that Execution cannot
recalculate because it does not receive the evaluation-time position.

PR #48 automated review exposed this gap as `EXEC47-REVIEW-003`. The final pre-merge refresh found
the new unresolved thread, invalidated the previous Merge-gate approval, and prevented the merge.
Architecture review classified the missing authority provenance and the missing governing
cross-stage contract as `ARCH47-REVIEW-001/002`.

Re-evaluating only the frozen order limit would close the reported example but not the authority
bypass. Risk computes:

```text
approved_quantity = min(
    intent.quantity,
    maximum_order_quantity,
    side_capacity(evaluation_time_position, maximum_absolute_position, side),
)
```

Execution owns neither the evaluation-time portfolio snapshot nor Risk's owner-ID/replay state. A
snapshot parameter would permit duplicate historical policy calculation but still would not prove
that Risk registered the result. Importing `ea.risk` from `ea.execution`, sharing a mutable
registry, or moving current-state freshness into Order creation would violate the stage boundary
or take ownership assigned to the future immediate pre-submission gate.

This ADR records the smallest missing trust boundary: a read-only, consumer-owned issuance
verifier backed by Risk's non-evicting canonical replay registry.

## Decision

This ADR narrowly extends ADR 0003 and ADR 0011. It does not change their canonical message
schemas, policy mathematics, state ownership, or deferred pre-submission freshness rules.

### Consumer-owned verification port

`ea.execution` defines the structural read-only `RiskResultIssuanceVerifier` protocol. It is owned
by the consumer because it expresses exactly the proof Execution requires. `ea.risk.authority`
implements the protocol structurally and does not import `ea.execution`.

The protocol exposes immutable construction bindings and one pure membership query:

```text
RiskResultIssuanceVerifier:
    run_id
    spec_set
    execution_policy
    policy

    has_issued_result(
        *,
        intent_id,
        canonical_intent_bytes,
        canonical_decision_bytes,
        canonical_evidence_bytes,
    ) -> bool
```

The query accepts exact canonical bytes already materialized by Execution. It returns exact
`True` only when the verifier's bound Risk authority has a non-evicted replay record under
`intent_id` whose intent, decision, and evidence bytes all match. It returns exact `False` for an
absent identity or any byte mismatch. It performs no evaluation, allocation, state transition,
consumption, current-state comparison, I/O, callback, or external effect.

Python object identity is not evidence. A reconstructed exact value with identical canonical bytes
must match; an object sharing IDs but differing in any canonical byte must not match.

### Construction binding

`create_phase1_order_authority` gains one mandatory `risk_result_verifier` argument. Construction
requires the verifier to expose the exact structural protocol and proves:

- verifier run ID equals the Order authority run ID;
- verifier specification-set ID and digest equal the bound specification set;
- verifier execution-policy ID and digest equal the bound execution policy; and
- verifier risk-policy ID and digest equal the exact frozen risk policy.

Construction has one closed public error matrix:

| Condition | Result |
|---|---|
| required protocol property is absent, the membership operation is absent/non-callable, a binding has the wrong exact runtime type, or binding access raises `AttributeError`/`TypeError` | `ExecutionAuthorityError(OutcomeCode.INVALID_TYPE)` |
| run, specification-set ID/digest, execution-policy ID/digest, or risk-policy ID/digest is well-typed but unequal | `ExecutionAuthorityError(OutcomeCode.CONFLICTING_ID)` |
| any other unexpected exception occurs while reading a binding | propagate the original exception unchanged |

Every construction failure occurs before an authority aggregate exists and therefore publishes no
state. Structural typing proves the port's shape, not its trust. The production claim depends on
the trusted composition root supplying the actual Risk-owned verifier; tests may deliberately
compose a fake verifier.

The verifier is stored as a private, non-replaceable construction binding. There is no optional
verifier, permissive fallback, caller-supplied boolean, registry injection, or post-construction
replacement.

Production composition must pass the verifier implemented by the exact `Phase1RiskAuthority`
wired for that run. Constructing an unrelated test authority or a fake structural implementation
does not create a production claim; the future composition root must bind the real risk owner just
as it binds the real ledger, audit, and result capabilities.

`Phase1OrderAuthority.create_order(intent, result) -> Order` remains unchanged.

### Registry ownership and lifecycle

Risk remains the sole owner of its replay registry. Its private immutable record stores or can
reproduce the exact canonical intent, decision, and evidence bytes for each registered result.
Execution receives neither that mapping nor a mutable view of it.

Issued records do not evict during a run. New evaluations, later portfolio snapshots, a newer
risk-state version, or a monotone halt do not invalidate historical issuance membership. This is
required because ADR 0008 makes exact Order replay stable and forbids silently re-risking an
already-issued result.

Durable recovery is not implemented in Issue #47. A future recovery path must reconstruct Risk's
issuance registry from authoritative durable evidence before it reconstructs or accepts new
Execution approval consumption. A registry that cannot prove historical issuance fails closed; it
must not infer membership from policy-compatible bytes alone.

### New-input validation and precedence

For one `create_order` call, Execution performs:

1. exact carrier validation and canonical intent, decision, evidence, and approval materialization;
2. existing approval/intent/decision triple-index replay or conflict classification;
3. canonical reconstruction and all ADR 0011 decision/evidence/approval/policy/lineage checks;
4. the exact defense-in-depth truth table below for frozen-policy facts that require no portfolio
   snapshot;
5. exact canonical issuance membership through the bound verifier;
6. Order-sequence availability and allocation;
7. canonical Order/request construction, copied replay indexes, complete preflight, and one
   aggregate-state publication.

An existing exact Execution replay returns the original `Order` before consulting current
provenance or current state. An occupied execution identity with different bytes remains a
conflict before provenance lookup.

A new absent or byte-mismatched issuance record returns
`ExecutionAuthorityError(OutcomeCode.CONFLICTING_ID)` and consumes no Order ID. A verifier that
returns a non-exact boolean violates the port and returns `INVALID_TYPE`. An unexpected verifier
exception propagates before allocation and publication; failure atomicity remains mandatory.

Static checks are defense in depth, not an alternative authority proof. In particular, Execution
does not attempt to reconstruct `side_capacity` without the historical portfolio position.

Let `I` be the requested intent quantity, `Q` the approved quantity,
`M` the configured `maximum_order_quantity`, and `C` the historical side capacity calculated only
by Risk from the evaluation-time position. The mandatory table is:

| Executable result | Required static proof in Execution | Historical fact proved only by issuance membership |
|---|---|---|
| `ALLOW / WITHIN_LIMITS` | `Q == I` and `I <= M` | `C >= I` |
| `RESIZE / RESIZED_ORDER_LIMIT` | `0 < Q < I`, `I > M`, and `Q == M` | `C > M` |
| `RESIZE / RESIZED_POSITION_LIMIT` | `0 < Q < I` and `Q < M` | `C == Q` |
| `RESIZE / RESIZED_ORDER_AND_POSITION_LIMITS` | `0 < Q < I`, `I > M`, and `Q == M` | `C == Q` |
| instrument absent from the frozen policy | no executable result is valid | not applicable |

Failure of a required static predicate returns
`ExecutionAuthorityError(OutcomeCode.CONFLICTING_ID)` before provenance lookup, Order allocation,
or publication. Execution must not require `Q <= maximum_absolute_position`: a valid
risk-reducing or crossing order from an already out-of-limit position can have historical side
capacity greater than that absolute-position limit.

### Explicit freshness exclusion

Issuance membership proves a historical fact: the bound Risk authority emitted the exact result
for the snapshot digest, policy, and risk-state version recorded in its evidence.

It does not prove that any of the following are current:

- portfolio snapshot or position;
- risk-state version or halt state;
- outstanding exposure or reservation state;
- audit acknowledgement;
- submission authorization.

Accepted ADR 0008's future immediate pre-submission gate must still recheck current portfolio/risk
versions, global halt, persisted audit acknowledgement, and every other pre-effect condition. It
may return `risk.stale_approval` or `submission.blocked_by_halt`. Order creation does not return
those outcomes and does not call a venue.

### Dependency direction

The permitted dependency remains:

```text
ea.execution -> ea.core
ea.risk      -> ea.core
```

`ea.execution` must not import `ea.risk`; `ea.risk` must not import `ea.execution`. Structural
protocol satisfaction lets the trusted outer composition bind the two stages without a peer
import. No shared mutable registry moves into `ea.core`.

### Required Issue #47 evidence

Issue #47 must prove:

- exact Risk-issued allow and every resize reason create deterministic Orders;
- exact Order replay still returns the original object after newer Risk records and halt state;
- a coherent forged allow above the order limit fails before allocation;
- a coherent forged allow within the order limit but inconsistent with the claimed snapshot
  position also fails because it was not issued;
- forged results for an unconfigured instrument and every resize-reason variant fail;
- arbitrary well-formed snapshot digest/version substitution fails;
- valid-looking policy digests and allocation transitions without a Risk registry record fail;
- an exact registered identity with different intent, decision, or evidence bytes fails;
- construction binding rejects a verifier for another run, specification set, execution policy,
  or risk policy;
- a result absent from the bound authority's registry fails membership even if another authority
  issued it; if the bound registry independently contains the exact same canonical tuple,
  membership succeeds regardless of Python process-object origin;
- canonical-value membership does not depend on object identity;
- a valid risk-reducing or crossing order is not rejected by an invalid
  `approved_quantity <= maximum_absolute_position` shortcut;
- provenance false, malformed return, and injected exception paths publish no Execution state;
- provenance is checked before Order-sequence exhaustion for a new identity;
- risk-state advancement or halt after issuance does not erase historical membership;
- import-boundary tests retain the two independent stage-to-core directions; and
- cross-process Order/request bytes and digests remain unchanged.

## Consequences

- A canonically coherent low-level factory result is no longer sufficient to create an Order.
- Execution proves both value coherence and actual issuance by the bound Risk authority.
- Risk keeps sole ownership of policy evaluation, snapshot interpretation, ID allocation, halt,
  and replay registration.
- Execution keeps sole ownership of approval consumption, Order IDs, Order replay/conflict, and
  canonical execution requests.
- No canonical document, digest domain, dependency, lockfile, configuration schema, persistence
  format, venue effect, or current-state freshness rule changes.
- The outer composition root gains one explicit wiring obligation for the future complete runtime
  profile.

## Rejected alternatives

### Check only `maximum_order_quantity`

Rejected because it catches the reported oversized allow but cannot prove position-derived
capacity, reason truth, snapshot identity, Risk halt/version, owner-ID allocation, or actual
authority issuance.

### Re-evaluate the complete risk policy inside Execution

Rejected because it duplicates Risk ownership and requires the historical portfolio position.
Two policy evaluators can drift and would violate ADR 0003's single stage authority.

### Pass `PortfolioSnapshot` to `create_order`

Rejected because a snapshot permits historical recalculation but does not prove that Risk
registered the result. It also expands the Order callable with a value it does not own and does
not solve current freshness.

### Import `Phase1RiskAuthority` from `ea.execution`

Rejected because peer-stage imports violate ADR 0003 and make Execution depend on one concrete
Risk implementation rather than its required proof.

### Share Risk's mutable replay mapping

Rejected because it leaks Risk-owned state, permits accidental mutation, and couples Execution to
private storage layout. The query is read-only and exposes only membership.

### Use Python object identity as provenance

Rejected because deterministic replay and recovery are canonical-value contracts, not
process-object contracts.

### Add a cryptographic signature

Rejected for the in-process Phase 1 boundary because key generation, custody, rotation, recovery,
and secret handling add unrelated security and operational ownership. Canonical membership in the
already-authoritative non-evicting Risk registry is sufficient.
