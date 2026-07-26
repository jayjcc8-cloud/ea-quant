# ADR 0011: Deterministic Pre-Trade Risk Authority

Date: 2026-07-26

## Status

Proposed

## Context

Accepted ADR 0003 requires every `OrderIntent` to pass risk before execution, and Accepted
ADR 0008 requires exactly one replay-stable allow, resize, reject, or evaluation-failed decision.
Issues #35, #37, and #39 implemented exact economics, owner-scoped identities, outcome codes, and
the immutable `OrderIntent -> RiskDecision -> ExecutionApproval` messages. Issue #43 implemented
the canonical portfolio ledger and immutable `PortfolioSnapshot` that risk must consume.

There is still no state owner that binds an intent to the exact portfolio snapshot, applies a
closed Phase 1 policy, allocates decision and approval IDs, returns an earlier decision on exact
replay, or halts after an identity conflict. Creating an `ExecutionApproval` directly through the
low-level message factory is therefore possible in tests but is not an executable production
path.

The current portfolio snapshot intentionally contains exact cash and position balances but no
funding authority, mark prices, margin, or P&L. A Phase 1 risk policy cannot honestly claim
cash-availability, notional, leverage, drawdown, or mark-to-market limits. It can freeze a smaller
position-and-order-quantity policy without inventing prices or maintaining a second ledger.

NautilusTrader 1.227.0 Beta and VeighNa/vn.py 4.2.0 provide mature risk-engine and rule-manager
patterns, but direct reuse would import their broader cache, event-engine, account, order, adapter,
plugin, UI, native-runtime, and persistence ownership. Neither can preserve this project's exact
snapshot digest, owner IDs, approval lineage, replay contract, and single-ledger rule through a
small adapter. The selected approach reuses existing project values and Python 3.12 only.

This decision covers the inner pre-trade risk authority. Runtime audit authorization, OMS
approval consumption, matcher behavior, execution facts, reconciliation, strategy, portfolio
planning, and backtest composition remain separate iterations.

## Decision

### Ownership and dependency direction

`ea.core.risk` defines dependency-neutral immutable values exchanged by portfolio, risk,
execution, runtime, audit, and result boundaries:

- the closed Phase 1 risk policy and per-instrument limits;
- the immutable run-scoped risk-state snapshot;
- closed risk reason and halt reason codes;
- the evaluation evidence that binds an existing `RiskDecision` to exact policy, intent, and
  portfolio-snapshot digests; and
- canonical bytes and domain-separated SHA-256 digests for those values.

The risk domain remains their semantic owner. They live in `core` only to avoid peer-stage
imports under ADR 0003.

`ea.risk.authority` owns the sole stateful pre-trade evaluator, replay registry, owner-ID
allocation, and monotone halt transition. It may import `ea.core` only. It does not import the
portfolio ledger implementation, execution/OMS implementation, runtime, configuration loaders,
composition, experiments, CLI, data/backtest/paper packages, audit or persistence
implementations, result adapters, SDKs, filesystem, network, or wall-clock code.

Risk never creates an `Order`, calls a venue, writes an audit sink, or mutates a portfolio
snapshot. The future OMS boundary MUST accept one exact `RiskEvaluationResult`, not a bare
`OrderIntent`, `RiskDecision`, or `ExecutionApproval`. It must verify the result's decision and
authority evidence before consuming the nested approval.

### Exact construction boundary

One authority is factory-created for exactly:

- one exact `RunId`;
- one exact `InstrumentExecutionSpecSet`, including ID and canonical digest;
- one exact `ExecutionPolicyRef`, including ID and canonical digest; and
- one exact immutable `Phase1RiskPolicy`.

The risk policy binds the same run-independent instrument specification set and execution-policy
lineage. Construction validates and freezes all policy values before version-zero state exists.
It is side-effect free and accepts no caller-supplied mutable mapping, ID counter, replay index,
state version, halt flag, clock, callback, or persistence handle.

The authority never replaces these construction bindings. A different policy or specification
set requires a different run/authority and reproducible-run evidence.

### Phase 1 policy values

`RiskPolicyId.value` has exact runtime type `str` and must match the ASCII regular expression
`[a-z][a-z0-9._-]{0,127}`. No normalization, case folding, trimming, subclass, or coercion is
accepted.

`InstrumentRiskLimit` contains:

- one exact canonical `Instrument`;
- a strictly positive `maximum_order_quantity`; and
- a strictly positive `maximum_absolute_position`.

Both quantities are `CanonicalDecimal` values and exact multiples of that instrument's quantity
quantum. A limit cannot name an instrument absent from the bound specification set.

`Phase1RiskPolicy` contains:

- schema and canonicalization versions;
- one `RiskPolicyId`;
- the exact instrument-specification-set ID and digest;
- the exact `ExecutionPolicyRef`;
- an immutable tuple of `InstrumentRiskLimit`; and
- a closed default of deny for any instrument not in that tuple.

Limits are unique and sorted by canonical `(venue, symbol)`. Caller order does not affect
canonical bytes or evaluation. Empty policy tuples are valid and deny every instrument.

The Phase 1 policy does not evaluate cash availability, mark-to-market notional, P&L, margin,
leverage, drawdown, volatility, correlation, or liquidity. Those require new canonical input and
separately accepted policy. It does not silently read market data or interpret negative cash as
approval.

### Immutable risk state

`RiskStateSnapshot` contains:

- the exact run ID;
- the exact policy ID and policy digest;
- a non-negative exact `risk_state_version`;
- `halted`;
- an optional closed `RiskHaltReason`;
- optional halt causal-root time and dispatch sequence; and
- the existing and submitted intent digests whose collision engaged a conflict halt, when
  applicable.

The field matrix is literal:

| State | version | reason | causal time/dispatch | existing/submitted conflict digests |
|---|---:|---|---|---|
| initial | `0` | `null` | both `null` | both `null` |
| public halt | `1` | non-conflict reason | both present | both `null` |
| identity-conflict halt | `1` | `intent_identity_conflict` | both present from submitted intent | both present; equality is permitted |

`halted` is `False` only for the initial row and `True` for both halted rows. No other combination
is constructible. The causal time is exact UTC with microsecond precision. Dispatch sequence is an
exact unsigned 64-bit integer.

Risk-state version changes only when the semantic risk state changes. Evaluating an intent,
allocating IDs, or extending replay indexes against unchanged policy/halt state does not increment
it. Engaging halt changes a non-halted state exactly once and advances the version by one.

Halt is monotone for one authority. Phase 1 exposes no clear, resume, replace-policy, or
state-version override. The public command identity is the exact tuple
`(reason, causal_root_available_at, dispatch_sequence)`. On the initial state it creates the public
halt row and changes version `0 -> 1`. On an already halted authority, an identical command and a
different command both return the existing state byte-for-byte; later causes are supplemental
runtime evidence and cannot rewrite the first canonical halt cause.

The public command rejects `intent_identity_conflict`. That reason is internal-only and is created
atomically by the identity-conflict evaluation path using:

- the digest already bound to the occupied intent ID;
- the submitted intent digest, which may equal the existing digest when distinct canonical bytes
  collide under SHA-256; and
- the submitted intent's causal-root time and dispatch sequence.

The replay index retains the existing canonical intent bytes and digest. Conflict classification
compares those authoritative bytes with the submitted canonical bytes before transition; digest
equality never converts different bytes into an exact replay. Both digests remain mandatory
conflict evidence even when they are equal.

Passing the internal-only reason to the public command returns
`RiskAuthorityError(OutcomeCode.OUT_OF_RANGE)` with no mutation. Wrong argument runtime types return
`INVALID_TYPE`; a non-UTC time or dispatch outside uint64 returns `OUT_OF_RANGE`.

Closed halt reasons are:

- `kill_switch`;
- `intent_identity_conflict`;
- `risk_evaluation_failure`; and
- `external_safety_halt`.

### Evaluation input and canonical binding

The evaluation boundary accepts only:

- one exact `OrderIntent`;
- one exact `PortfolioSnapshot`; and
- the authority's already-bound exact specification set, execution policy, policy, and state.

For a new intent identity, all of these bindings must agree before policy mathematics:

- intent, snapshot, specification set, and authority have the same run ID where applicable;
- intent `portfolio_snapshot_version` equals snapshot `snapshot_version`;
- snapshot specification-set ID/digest equals the authority binding;
- intent specification ID and set ID/digest equal the bound specification;
- intent execution-policy ID/digest equals the authority binding;
- intent quantity is positive and exact-grid valid for that specification;
- snapshot positions are canonical, unique, sorted, exact-grid valid, and immutable; and
- intent causal-root time and dispatch sequence remain the values already frozen in the intent.

Risk derives the current position only by exact instrument lookup in the supplied snapshot. An
absent position means exact zero. It does not cache cash or position balances and does not maintain
a writable shadow ledger.

### Closed reason and evaluation evidence

The existing broad `OutcomeCode` embedded in canonical `RiskDecision` remains ADR 0008's stable
decision-bound reason. ADR 0011 does not change the accepted v1 `RiskDecision` fields, canonical
bytes, or digest. `RiskReasonCode` is supplemental authority evidence with a narrower explanation.
It records one of:

- `within_limits`;
- `resized_order_limit`;
- `resized_position_limit`;
- `resized_order_and_position_limits`;
- `halted`;
- `instrument_not_configured`;
- `no_position_capacity`;
- `stale_portfolio_snapshot`;
- `lineage_mismatch`;
- `arithmetic_failure`; or
- `approval_sequence_exhausted`.

Reason/decision coupling is closed:

| Decision kind | Allowed supplemental reasons |
|---|---|
| `allow` | `within_limits` |
| `resize` | `resized_order_limit`, `resized_position_limit`, `resized_order_and_position_limits` |
| `reject` | `halted`, `instrument_not_configured`, `no_position_capacity` |
| `evaluation_failed` | `stale_portfolio_snapshot`, `lineage_mismatch`, `arithmetic_failure`, `approval_sequence_exhausted` |

`RiskEvaluationEvidence` binds:

- the canonical intent ID and digest;
- the submitted portfolio snapshot version and digest;
- the policy ID and digest;
- the risk-state version used;
- the resulting canonical `RiskDecision` and decision digest;
- the approval digest, present exactly for allow/resize and `null` for reject/evaluation-failed;
- the closed reason code; and
- before/after decision and approval next-sequence states.

Evidence is immutable, canonically encoded, and digestible. It cannot replace or modify the
`RiskDecision`; it explains which exact authority inputs produced it. Approval digest presence
must agree with decision kind, and a present value must equal the digest of the exact nested
approval.

`RiskEvaluationResult` is the exact public return carrier:

```text
RiskEvaluationResult(
    decision: RiskDecision,
    evidence: RiskEvaluationEvidence,
)
```

It is a frozen, slots-based, factory-only pair. Its evidence decision digest must equal the digest
of `decision`; every intent, run, decision, approval, policy, state-version, snapshot, and sequence
binding must agree. The pair adds no second canonical document: the decision and evidence retain
their already frozen canonical documents and digests. Exact replay returns the original result
object.

### Literal canonical contracts

All new canonical JSON uses the existing strict project encoder:

- ASCII bytes with non-ASCII escaped by JSON;
- object keys sorted lexicographically at every level;
- no insignificant whitespace;
- exact base-10 integers with no leading zero except `0`;
- lowercase `true`, `false`, and `null`;
- enum `.value`, `CanonicalDecimal.text`, and lowercase 64-character SHA-256 text;
- UTC timestamps exactly `%Y-%m-%dT%H:%M:%S.%fZ`;
- lists in their already validated canonical tuple order; and
- every listed field present, including optional fields encoded as `null`.

Digests are exactly `sha256(domain_bytes + canonical_bytes).hexdigest()`.

Literal constants are:

| Value | schema | canonicalization | digest domain bytes |
|---|---:|---|---|
| `Phase1RiskPolicy` | `1` | `ea-risk-policy-v1` | `b"ea.risk-policy.v1\0"` |
| `RiskStateSnapshot` | `1` | `ea-risk-state-v1` | `b"ea.risk-state.v1\0"` |
| `RiskEvaluationEvidence` | `1` | `ea-risk-evaluation-evidence-v1` | `b"ea.risk-evaluation-evidence.v1\0"` |

The nested instrument document is exactly
`{"symbol": instrument.symbol, "venue": instrument.venue.code}`. The nested execution-policy
document is exactly
`{"execution_policy_id": ref.identifier.value, "execution_policy_sha256": ref.sha256.value}`.
The nested economic-ID document is exactly
`{"owner_kind": id.owner_kind.value, "owner_sequence": id.owner_sequence, "run_id": id.run_id.value}`.
Nested documents do not add their own canonicalization/schema fields.

The exact `Phase1RiskPolicy` document is:

```json
{
  "canonicalization": "ea-risk-policy-v1",
  "default_action": "deny",
  "execution_policy": {
    "execution_policy_id": "<token>",
    "execution_policy_sha256": "<sha256>"
  },
  "instrument_limits": [
    {
      "instrument": {"symbol": "<symbol>", "venue": "<venue>"},
      "maximum_absolute_position": "<canonical-decimal>",
      "maximum_order_quantity": "<canonical-decimal>"
    }
  ],
  "instrument_spec_set_id": "<token>",
  "instrument_spec_set_sha256": "<sha256>",
  "message_type": "phase1_risk_policy",
  "policy_id": "<token>",
  "schema_version": 1
}
```

The exact `RiskStateSnapshot` document is:

```json
{
  "canonicalization": "ea-risk-state-v1",
  "conflict_existing_intent_sha256": null,
  "conflict_submitted_intent_sha256": null,
  "halt_causal_root_available_at": null,
  "halt_dispatch_sequence": null,
  "halt_reason": null,
  "halted": false,
  "message_type": "risk_state_snapshot",
  "policy_id": "<token>",
  "policy_sha256": "<sha256>",
  "risk_state_version": 0,
  "run_id": "<uuid>",
  "schema_version": 1
}
```

Halted documents replace the five halt/conflict `null` values exactly according to the field
matrix. They do not add or omit fields.

The exact `RiskEvaluationEvidence` document is:

```json
{
  "approval_next_after": null,
  "approval_next_before": null,
  "approval_sha256": null,
  "canonicalization": "ea-risk-evaluation-evidence-v1",
  "decision_id": {
    "owner_kind": "risk.decision",
    "owner_sequence": 1,
    "run_id": "<uuid>"
  },
  "decision_next_after": 2,
  "decision_next_before": 1,
  "decision_sha256": "<sha256>",
  "intent_id": {
    "owner_kind": "portfolio.intent",
    "owner_sequence": 1,
    "run_id": "<uuid>"
  },
  "intent_sha256": "<sha256>",
  "message_type": "risk_evaluation_evidence",
  "policy_id": "<token>",
  "policy_sha256": "<sha256>",
  "portfolio_snapshot_sha256": "<sha256>",
  "portfolio_snapshot_version": 0,
  "reason_code": "<risk-reason-code>",
  "risk_state_version": 0,
  "run_id": "<uuid>",
  "schema_version": 1
}
```

The numeric/null sequence values in that example are illustrative; the field set and encodings
are normative. Allow/resize requires non-null `approval_sha256` and uses the exact approval
before/after states. Reject/evaluation-failed requires `approval_sha256 == null`; its approval
before/after states are equal. No new canonical document embeds complete decision, approval,
intent, snapshot, or policy bytes; it references their exact IDs/digests to avoid two competing
encodings.

### Exact position-capacity mathematics

Let:

- `p` be the exact current signed position from the supplied snapshot;
- `q` be the strictly positive requested quantity;
- `O` be the positive maximum order quantity;
- `M` be the positive maximum absolute position; and
- `s` be `+1` for buy and `-1` for sell.

The side-specific capacity is:

```text
buy_capacity  = max(0, M - p)
sell_capacity = max(0, M + p)
```

The chosen quantity is the greatest exact quantity:

```text
a = min(q, O, side_capacity)
```

All operations use canonical integer-coefficient/scale-aligned arithmetic independent of ambient
Decimal context. Because `q`, `O`, `M`, and `p` are exact multiples of the instrument quantity
quantum, `a` is already quantized; the authority never rounds.

This definition is intentionally side-symmetric:

- an in-limit position can grow only to the same-side absolute limit;
- an out-of-limit long position rejects further buys but permits sells up to crossing to the
  short limit;
- an out-of-limit short position rejects further sells but permits buys up to crossing to the
  long limit;
- a reducing order may remain outside the limit while moving toward zero; and
- no approved order can cross beyond the opposite absolute limit.

If `a == q`, the decision is `allow`. If `0 < a < q`, the decision is `resize`. If `a == 0`, the
decision is `reject`. Resize uses the greatest permitted quantity and never silently chooses a
smaller value.

If both `O` and side capacity constrain the original quantity to the same `a`, the reason is
`resized_order_and_position_limits`. Otherwise the binding minimum selects the single resize
reason. Exact equality at a limit is allowed.

### Phase 1 outstanding-intent boundary

This authority does not reserve exposure for multiple concurrently outstanding approvals.
The Phase 1 runtime owns one per-instrument orchestration gate, while portfolio owns emission
cardinality. The gate lifecycle is literal:

1. Portfolio may emit at most one new intent in one serialized dispatch.
2. Runtime acquires the instrument gate before first risk evaluation. An already-held gate means
   the new intent is not evaluated or assigned any risk ID.
3. Exact risk replay leaves the current gate state unchanged. If the original path still holds the
   gate, replay keeps it held; if a terminal original path already released it, replay does not
   reacquire it.
4. Risk reject releases the gate after its semantic outcome is routed.
5. Risk evaluation failure or identity conflict retains the gate until the run's failing/halt
   transition is recorded; no later intent may be evaluated in that run.
6. Allow/resize retains the gate through mandatory pre-effect audit and OMS handling.
7. Audit failure or acknowledgement mismatch occurs before a venue call, retains the gate through
   the recorded run halt, and permits no later evaluation.
8. Allow/resize followed by `submission.blocked_by_halt` retains the gate through the recorded
   halt and permits no later intent evaluation in that run.
9. `submission.definitely_not_submitted` releases the gate only after its terminal OMS outcome is
   recorded; a later intent, if the run is still allowed to continue, is a fresh intent/risk
   decision/order/client key.
10. `submission.uncertain` retains the gate through query/reconciliation. Only
   `confirmed_not_submitted` or `confirmed_rejected` can release it; `confirmed_filled` retains it
   until the discovered Fill is ledger-applied; `still_unknown` retains it through terminal run
   failure.
11. In the Phase 1 historical matcher, submitted market Orders either full-fill on the first later
    eligible bar or expire only at end-of-run for no eligible data. Full fill releases the gate
    only after the canonical Fill is applied and the newer portfolio snapshot is published before
    strategy runs. End-of-run expiry needs no release usable by another intent.
12. Duplicate fact, Fill, ledger, decision, approval, or terminal-outcome replay leaves the
    current gate state unchanged: a held gate stays held and an already released gate is not
    reacquired.
13. Any late real Fill is ordered before later market strategy work, is economically applied, and
    engages the reconciliation/global halt path before another intent evaluation.

These are executable profile constraints, not assumptions that disappear from evidence. Runtime
and portfolio implementation Issues must test them before the backtest MVP can be complete. A
future multi-intent or asynchronous profile requires immutable reservation messages, approval
release/consume outcomes, and a new risk-state transition contract; it cannot reuse this
snapshot-only calculation silently.

### Replay, conflict, and owner-ID allocation

Risk owns two independent, run-scoped, monotone sequences:

- `EconomicOwnerKind.RISK_DECISION`; and
- `EconomicOwnerKind.RISK_APPROVAL`.

Private state stores `decision_next` and `approval_next`, each as `int | None`:

- initial value is exact integer `1`;
- an integer value is the next owner sequence to allocate and must be in
  `1..18_446_744_073_709_551_615`;
- allocating a value below the uint64 maximum changes it to `value + 1`;
- allocating the uint64 maximum changes it to `None`; and
- `None` is the only exhausted sentinel and is never passed to `EconomicId`.

Every newly registered intent consumes exactly one decision ID. Only allow and resize consume
exactly one approval ID. Reject and evaluation-failed consume no approval ID.

Evidence `*_next_before` and `*_next_after` fields contain these exact integer-or-null private
states, not the last allocated ID. Their transitions are:

| Path | decision before/after | approval before/after |
|---|---|---|
| exact replay | original evidence values | original evidence values |
| identity conflict / structural error | no evidence; current state unchanged | no evidence; current state unchanged |
| decision exhausted (`before == null`) | no evidence; `null -> null` | unchanged |
| reject / ordinary evaluation-failed | allocate once | unchanged and equal in evidence |
| allow / resize | allocate once | allocate once |
| executable policy result with approval exhausted | allocate decision once for `evaluation_failed` | `null -> null` |

When allocation consumes the uint64 maximum, the evidence after field is `null`. Exact replay
returns the originally recorded before/after values even when the authority's current counters
have since advanced.

The replay index is keyed by exact intent economic identity and retains:

- canonical intent digest and bytes;
- the original canonical portfolio snapshot digest used for evaluation;
- the original `RiskDecision`;
- the original `RiskEvaluationEvidence`; and
- the original `RiskEvaluationResult`.

It never evicts.

Evaluation begins by exact-type validating the intent and producing its canonical bytes/digest.
The intent identity index is then consulted before validating a new portfolio snapshot or
re-evaluating policy:

- same intent ID and identical canonical intent bytes returns the original decision/evidence
  byte-for-byte, even if the supplied current snapshot or risk halt state is newer;
- same intent ID and different canonical intent bytes is `intent_identity_conflict`;
- a conflict creates no decision or approval, engages halt atomically when not already halted,
  and raises `RiskAuthorityError(OutcomeCode.CONFLICTING_ID)`; and
- a new identity continues through full new-input validation.

Exact replay still requires an exact `PortfolioSnapshot` runtime type at the public boundary, but
its content is not used after an exact intent replay is identified. This prevents a replay from
being re-risked while retaining a closed API.

### Structural errors and mismatch matrix

`RiskAuthorityError` is a `ValueError` carrying exactly one `OutcomeCode` from:

- `validation.invalid_type`;
- `validation.out_of_range`;
- `validation.not_quantized`; or
- `validation.conflicting_id`.

No other outcome code is constructible on this error. Exact-type violations map to
`validation.invalid_type`; invalid grammar, unsigned-range, canonical-decimal representability,
or exhausted decision sequence maps to `validation.out_of_range`; quantity-grid failure maps to
`validation.not_quantized`; foreign run/spec identity and intent-ID byte collision map to
`validation.conflicting_id`.

The authority exhaustively translates subordinate helper outcomes before they cross its boundary:

- `OutcomeCode.INVALID_TYPE` remains `OutcomeCode.INVALID_TYPE`;
- `OutcomeCode.NOT_QUANTIZED` remains `OutcomeCode.NOT_QUANTIZED`;
- `OutcomeCode.CONFLICTING_ID` remains `OutcomeCode.CONFLICTING_ID`; and
- `OutcomeCode.OUT_OF_RANGE`, `OutcomeCode.NON_FINITE`, `OutcomeCode.PRICE_DOMAIN`, or
  `OutcomeCode.ARITHMETIC_OVERFLOW` maps to `OutcomeCode.OUT_OF_RANGE`.

No subordinate outcome outside those seven existing canonical validation codes is accepted.
Programmer assertions are not domain outcomes and are not swallowed.

After replay/conflict classification, a new identity follows this field-by-field matrix in row
order. “Register failure” means allocate one decision ID, create
`RiskDecisionKind.EVALUATION_FAILED` with no approval, record the listed supplemental reason, and
atomically register the result. If the decision sequence is exhausted, the exhaustion error wins
and no result is registered.

| Check | Exact condition | Result | reason | IDs/state |
|---|---|---|---|---|
| intent run | intent run differs from authority run | structural error `validation.conflicting_id` | none | no IDs/state |
| intent spec-set identity | intent set ID/digest differs from bound set | structural error `validation.conflicting_id` | none | no IDs/state |
| intent specification | instrument absent or specification ID differs | structural error `validation.conflicting_id` | none | no IDs/state |
| intent canonical economics | quantity/type/grid is not valid under the bound spec | structural error `validation.invalid_type`, `validation.out_of_range`, or `validation.not_quantized` under the exhaustive mapping above | none | no IDs/state |
| decision sequence | `decision_next is None` | structural error `validation.out_of_range` | none | no IDs/state |
| intent execution policy | ID or digest differs from bound execution policy | register failure | `lineage_mismatch` | decision only |
| snapshot run | snapshot run differs from authority/intent run | register failure | `lineage_mismatch` | decision only |
| snapshot spec set | snapshot set ID/digest differs from authority | register failure | `lineage_mismatch` | decision only |
| snapshot behind | snapshot version `<` intent bound version | register failure | `stale_portfolio_snapshot` | decision only |
| intent behind | snapshot version `>` intent bound version | register failure | `lineage_mismatch` | decision only |
| snapshot balance lineage | target position has wrong quantum or any balance conflicts with bound spec/currency registry | register failure | `lineage_mismatch` | decision only |
| snapshot canonical digest | canonical snapshot encoding/digest fails | raised closed structural error | none | no IDs/state |
| risk halt | current state halted | reject | `halted` | decision only |
| configured limit | instrument absent from policy | reject | `instrument_not_configured` | decision only |
| policy arithmetic | exact capacity calculation cannot be represented | register failure | `arithmetic_failure` | decision only |
| capacity | chosen quantity is zero | reject | `no_position_capacity` | decision only |
| approval sequence | executable result and `approval_next is None` | register failure | `approval_sequence_exhausted` | decision only |
| capacity equals request | all prior checks pass | allow | `within_limits` | decision + approval |
| positive partial capacity | all prior checks pass | resize | closed resize reason | decision + approval |

An exact `PortfolioSnapshot` constructor already rejects malformed tuple types/order/duplicates.
The balance-lineage row is the additional authority-to-bound-spec validation. Foreign
execution-policy lineage is a registered failure rather than a structural error because the
intent is still canonical under this authority's spec and can bind an ADR 0008
evaluation-failed decision. A foreign run or spec cannot acquire a local decision ID.

### Failure and outcome precedence

For a new intent identity, the normative order is:

1. exact public argument runtime types;
2. canonical intent bytes/digest;
3. intent replay or identity-conflict classification;
4. structural intent run/spec/grid binding;
5. decision-sequence availability;
6. execution-policy and complete snapshot mismatch classification;
7. current halt and configured-instrument checks;
8. exact position-capacity arithmetic;
9. approval-sequence availability when the policy result would be executable;
10. complete decision, approval, evaluation evidence, replay index, and next private state;
11. canonical bytes/digests for policy, risk state, decision, approval when present, and evidence;
12. one aggregate private-state reference publication.

The following outcomes are literal:

| Condition | Result | State effect |
|---|---|---|
| exact replay | original decision/evidence | none |
| same intent ID, different bytes | `CONFLICTING_ID` error | engage halt once; no IDs |
| malformed type or intent not valid under bound run/spec | closed `RiskAuthorityError` | none |
| policy/snapshot mismatch classified by matrix | `evaluation_failed` decision | register decision only |
| arithmetic/representability failure | `evaluation_failed` decision | register decision only |
| decision sequence exhausted | closed `RiskAuthorityError(OUT_OF_RANGE)` | none |
| halted or instrument absent from policy | `reject` decision | register decision only |
| zero side capacity | `reject` decision | register decision only |
| approval sequence exhausted | `evaluation_failed` decision | register decision only |
| full capacity | `allow` decision | register decision and approval |
| positive partial capacity | `resize` decision | register decision and approval |
| canonicalization failure before publication | raised error | no state/index/counter change |

An evaluation-failed decision is created only after the exact canonical intent can be validated
under the authority's bound specification set. A malformed or foreign-spec intent cannot acquire
a local risk decision ID merely by reaching the wrong authority.

Evaluation failure does not silently engage the risk authority's canonical halt because runtime
owns the lifecycle failure path. Runtime MUST engage the halt explicitly with
`risk_evaluation_failure` before any later evaluation. Identity conflict is the exception: the
authority itself has sufficient canonical evidence to engage halt in the same atomic transition.

### Atomic publication

All changing private data lives in one frozen aggregate state:

- risk-state snapshot;
- next decision and approval sequences;
- immutable replay maps; and
- immutable decision/evidence history tuples in allocation order.

For success, the authority constructs copied candidate maps and complete next values locally.
It canonicalizes and digests every public value before one assignment publishes the new aggregate
state reference. No public object exposes a mutable mapping or list.

Failure injection at policy, state, decision, approval, evidence, copied-index, or digest
construction must demonstrate:

- identical aggregate state object identity;
- byte-identical public state, histories, counters, and indexes; and
- no consumed decision or approval sequence.

The identity-conflict transition follows the same rule: the complete halted state and unchanged
replay histories are built and preflighted before one reference swap.

### Public safety boundary

The public Phase 1 API exposes only:

- a factory for one bound authority;
- immutable policy and risk-state inspection;
- `evaluate(intent, portfolio_snapshot) -> RiskEvaluationResult`;
- replay-safe decision/evidence history inspection; and
- monotone `engage_halt(reason, causal_root_available_at, dispatch_sequence)`.

It does not expose arbitrary decision or approval creation, ID allocation, replay-map mutation,
risk-state replacement, halt clear, position/cash mutation, reservation injection, audit or
persistence callbacks, runtime mode, venue submission, SDK objects, clocks, global registries, or
external effects.

Low-level immutable message factories in `ea.core.execution_messages` remain construction
primitives, not the production risk authority. The composition/runtime path must receive the
stateful authority interface and cannot call those factories to bypass policy.

The future OMS callable boundary MUST accept the exact `RiskEvaluationResult`. Before creating an
Order it must verify:

- decision kind is allow or resize and the broad outcome code agrees;
- evidence decision digest equals the supplied decision digest;
- evidence approval digest is present and equals the nested approval digest;
- decision, approval, evidence, intent, run, portfolio-snapshot version, risk-state version, and
  dispatch bindings agree;
- evidence policy ID/digest equals the execution profile's frozen risk policy; and
- the exact approval has not already been consumed.

A byte-valid `RiskDecision` or `ExecutionApproval` without matching authority evidence is rejected.
This mandatory future OMS acceptance work preserves the accepted v1 messages while closing the
low-level-factory bypass. It is not an optional adapter check.

## Required implementation evidence

The implementation Issue must include:

- golden allow, resize, reject, evaluation-failed, replay, conflict, and halt traces;
- buy/sell and long/short symmetry across zero and both position limits;
- exact-boundary and one-quantum-inside/outside cases;
- current positions already beyond either limit and risk-reducing behavior;
- property tests proving every executable quantity is no greater than request/order limits and:
  an initially in-limit position remains in `[-M, M]`; an out-of-limit position cannot increase
  its same-side breach, may move toward the interval while remaining outside it, and never crosses
  beyond the opposite limit;
- exact replay after newer snapshot and halt state with no new IDs;
- same-ID/different-bytes conflict with one atomic halt transition;
- decision/approval sequence exhaustion at each precedence point;
- stale snapshot, foreign spec/policy, invalid grid, overflow, and injected canonicalization
  failures with byte-identical state where required;
- cross-process equality under different hash seeds, locale, timezone, and Decimal context;
- AST import-boundary and public-API escape-hatch tests; and
- full repository verification plus exact-head CI.

The later Phase 1 runtime/portfolio Issues must additionally prove every acquisition/release row
of the one-outstanding-intent lifecycle. The future OMS Issue must prove the mandatory
decision-plus-evidence acceptance checks. Neither gate may be omitted before an end-to-end
backtest is accepted.

## Design-finding traceability

- `ARCH45-DESIGN-001` / `RISK45-DESIGN-001`: resolved by the literal carrier, enum, JSON,
  schema/canonicalization/domain, null, timestamp, nested-reference, result-carrier, and exact
  four-code structural-error contract with exhaustive subordinate-outcome mapping.
- `ARCH45-DESIGN-002`: resolved by preserving existing `RiskDecision.outcome_code` as ADR 0008's
  decision-bound reason and defining `RiskReasonCode` as supplemental evidence without changing
  the v1 message digest.
- `ARCH45-DESIGN-003` / `RISK45-DESIGN-003`: resolved by the integer-or-null next-sequence model
  and exact uint64-boundary transition table.
- `ARCH45-DESIGN-004`: resolved by the halt field matrix, internal-only conflict transition,
  byte-authoritative collision handling with equal digests permitted, public command identity,
  exact version transition, and repeat behavior.
- `ARCH45-DESIGN-005`: resolved by the thirteen-row per-instrument gate lifecycle assigned to
  portfolio emission and runtime orchestration, including gate-neutral replay and
  `submission.blocked_by_halt`.
- `RISK45-DESIGN-002`: resolved by the piecewise in-limit/out-of-limit property requirement.
- `RISK45-DESIGN-004`: resolved by the ordered field-by-field mismatch matrix and closed
  `RiskAuthorityError` mapping.
- `RISK45-DESIGN-005`: resolved by making exact `RiskEvaluationResult` the mandatory future OMS
  input and requiring decision/evidence/approval/policy/snapshot verification.
- `RISK45-DESIGN-006`: resolved by using the existing `validation.*` `OutcomeCode` literals and
  exhaustively mapping every subordinate helper outcome, including `validation.not_quantized`.

## Consequences

- The first executable risk layer is intentionally narrow but honest about the canonical data it
  has.
- Risk approval is deterministic, replay-safe, bound to one ledger snapshot, and incapable of
  mutating a shadow portfolio.
- A monotone halt and identity-conflict path exist before OMS and venue work begins.
- Position and order-quantity limits can be tested without market prices, funding, or float.
- Cash, notional, P&L, margin, drawdown, and concurrent-reservation policies remain visibly
  deferred rather than being approximated.
- Phase 1 runtime and portfolio planning inherit an explicit serialization constraint.

## Rejected alternatives

### Import a complete trading risk engine

Rejected because NautilusTrader and vn.py bring broader state, execution, adapter, event, and
operational ownership than this dependency-neutral inner authority.

### Treat the low-level RiskDecision factory as the risk engine

Rejected because it has no policy, snapshot binding, replay registry, ID authority, conflict
halt, or atomic state.

### Enforce cash or notional limits without canonical prices/funding

Rejected because it would fabricate valuation authority or read ambient market data.

### Keep a mutable shadow position inside risk

Rejected because Accepted ADR 0003 and ADR 0010 make the portfolio ledger and immutable snapshot
the sole position authority.

### Re-risk an exact replay against the newest snapshot

Rejected because ADR 0008 requires exact replay to return the original decision and approval.

### Silently support concurrent approvals without reservations

Rejected because multiple approvals against one snapshot can exceed limits. Phase 1 instead
freezes serialization, and a later profile must add explicit reservation lifecycle semantics.
