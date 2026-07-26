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
snapshot. Execution accepts only the `ExecutionApproval` carried by an authority-produced
`RiskDecision`; it never accepts a bare `OrderIntent`.

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

`RiskPolicyId` is a canonical non-empty ASCII token.

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
- the digest of the intent whose conflict engaged the halt, when applicable.

Version zero is literal:

- `risk_state_version == 0`;
- `halted is False`; and
- every halt-evidence field is absent.

Risk-state version changes only when the semantic risk state changes. Evaluating an intent,
allocating IDs, or extending replay indexes against unchanged policy/halt state does not increment
it. Engaging halt changes a non-halted state exactly once and advances the version by one.

Halt is monotone for one authority. Phase 1 exposes no clear, resume, replace-policy, or
state-version override. An exact repeat of the same halt command is a no-op returning the existing
snapshot. A later different halt cause is retained as non-authoritative supplemental runtime
evidence outside this state; it cannot rewrite the first canonical halt cause.

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

`RiskReasonCode` is separate from the broad `OutcomeCode` carried by `RiskDecision`. It records one
of:

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

`RiskEvaluationEvidence` binds:

- the canonical intent ID and digest;
- the submitted portfolio snapshot version and digest;
- the policy ID and digest;
- the risk-state version used;
- the resulting canonical `RiskDecision` and decision digest;
- the closed reason code; and
- before/after decision and approval sequence positions.

Evidence is immutable, canonically encoded, and digestible. It cannot replace or modify the
`RiskDecision`; it explains which exact authority inputs produced it. Reject and
evaluation-failed evidence carries no approval digest. Allow and resize evidence carries the
digest of the exact approval already nested in the decision.

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
The Phase 1 runtime profile MUST serialize one strategy-originated intent through terminal
execution/Fill-ledger processing before it evaluates another intent that can affect the same
instrument. The Phase 1 portfolio planner MUST emit at most one intent per serialized dispatch.

These are executable profile constraints, not assumptions that disappear from evidence. Runtime
and portfolio implementation Issues must test them before the backtest MVP can be complete. A
future multi-intent or asynchronous profile requires immutable reservation messages, approval
release/consume outcomes, and a new risk-state transition contract; it cannot reuse this
snapshot-only calculation silently.

### Replay, conflict, and owner-ID allocation

Risk owns two independent, run-scoped, monotone sequences:

- `EconomicOwnerKind.RISK_DECISION`; and
- `EconomicOwnerKind.RISK_APPROVAL`.

The first allocated sequence for each owner is `1`. Every newly registered intent consumes
exactly one decision ID. Only allow and resize consume exactly one approval ID. Reject and
evaluation-failed consume no approval ID.

The replay index is keyed by exact intent economic identity and retains:

- canonical intent digest and bytes;
- the original canonical portfolio snapshot digest used for evaluation;
- the original `RiskDecision`; and
- the original `RiskEvaluationEvidence`.

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

### Failure and outcome precedence

For a new intent identity, the normative order is:

1. exact public argument runtime types;
2. canonical intent bytes/digest;
3. intent replay or identity-conflict classification;
4. complete run, snapshot, specification, execution-policy, and grid binding;
5. decision-sequence availability;
6. current halt and configured-instrument checks;
7. exact position-capacity arithmetic;
8. approval-sequence availability when the policy result would be executable;
9. complete decision, approval, evaluation evidence, replay index, and next private state;
10. canonical bytes/digests for policy, risk state, decision, approval when present, and evidence;
11. one aggregate private-state reference publication.

The following outcomes are literal:

| Condition | Result | State effect |
|---|---|---|
| exact replay | original decision/evidence | none |
| same intent ID, different bytes | `CONFLICTING_ID` error | engage halt once; no IDs |
| malformed type or intent not valid under bound spec | closed `RiskAuthorityError` | none |
| stale/mismatched otherwise-valid snapshot | `evaluation_failed` decision | register decision only |
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
- `evaluate(intent, portfolio_snapshot)`;
- replay-safe decision/evidence history inspection; and
- monotone `engage_halt(reason, causal_root_available_at, dispatch_sequence)`.

It does not expose arbitrary decision or approval creation, ID allocation, replay-map mutation,
risk-state replacement, halt clear, position/cash mutation, reservation injection, audit or
persistence callbacks, runtime mode, venue submission, SDK objects, clocks, global registries, or
external effects.

Low-level immutable message factories in `ea.core.execution_messages` remain construction
primitives, not the production risk authority. The composition/runtime path must receive the
stateful authority interface and cannot call those factories to bypass policy.

## Required implementation evidence

The implementation Issue must include:

- golden allow, resize, reject, evaluation-failed, replay, conflict, and halt traces;
- buy/sell and long/short symmetry across zero and both position limits;
- exact-boundary and one-quantum-inside/outside cases;
- current positions already beyond either limit and risk-reducing behavior;
- property tests proving every executable decision satisfies order and projected-position limits;
- exact replay after newer snapshot and halt state with no new IDs;
- same-ID/different-bytes conflict with one atomic halt transition;
- decision/approval sequence exhaustion at each precedence point;
- stale snapshot, foreign spec/policy, invalid grid, overflow, and injected canonicalization
  failures with byte-identical state where required;
- cross-process equality under different hash seeds, locale, timezone, and Decimal context;
- AST import-boundary and public-API escape-hatch tests; and
- full repository verification plus exact-head CI.

The later Phase 1 runtime/portfolio Issues must additionally prove the one-outstanding-intent
profile constraint before an end-to-end backtest can be accepted.

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
