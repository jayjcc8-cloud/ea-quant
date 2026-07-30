# ADR 0017: Deterministic Strategy Signal and Portfolio Planning

- Status: Proposed
- Date: 2026-07-30
- Decision owners: Architecture, Strategy/Portfolio, Runtime, Risk, Execution
- Related: ADR 0003, ADR 0004, ADR 0006, ADR 0008, ADR 0010, ADR 0011, ADR 0012,
  ADR 0013, ADR 0014, ADR 0016, Issue #55

## Context

Accepted ADR 0003 requires every trading workflow to use one mode-neutral chain:

`market event -> strategy -> portfolio -> risk -> shared execution/OMS -> venue`

The repository now has deterministic economic values and instrument specifications, canonical
execution identities/messages, a Fill ledger and immutable portfolio snapshots, pre-trade risk and
Order authorities, trusted execution-fact processing, strict bounded historical data, and a
run-wide historical frontier/dispatcher. The missing policy boundary before risk is material:

- no canonical strategy `Signal` exists;
- no canonical `PortfolioTarget` exists;
- no portfolio owner converts desired state and the latest canonical position into an
  `OrderIntent`;
- the already reserved `STRATEGY_SIGNAL`, `PORTFOLIO_TARGET`, and `PORTFOLIO_INTENT` identity
  owners have no issuing authorities;
- `TargetLineageRef` and `OrderIntent.correlation_id` accept lineage that no current component is
  authorized to produce; and
- a future coordinator cannot prove that an intent came from one visible market root, one strategy
  decision, one portfolio policy, and the latest ledger snapshot.

Allowing a strategy to construct a target or `OrderIntent` directly would collapse the required
strategy/portfolio boundary. Accepting a caller-supplied market root/sequence without proving the
exact active dispatch would let a strategy issue an authoritative signal for future, stale, copied,
or never-admitted data. Allowing runtime to select a caller-supplied snapshot would permit planning
against stale or foreign state. Treating “already at target” as absence would erase an economically
relevant decision from replay and audit evidence. Retrying a callback without stable issuance
state could duplicate targets or intents.

Issue #55 owns the smallest complete policy bridge from a strategy direction to a target and a
planning result. It does not implement a concrete strategy, matcher, venue, full lifecycle/audit
coordinator, result adapter, or backtest CLI.

## Reuse decision

The implementation reuses existing EA primitives and the Python 3.12 standard library:

- `EconomicId`, with the already accepted `STRATEGY_SIGNAL`, `PORTFOLIO_TARGET`, and
  `PORTFOLIO_INTENT` owners;
- `MarketDataEnvelope` and its canonical record codec;
- `CanonicalDecimal`, `InstrumentExecutionSpecSet`, exact quantity grids, and spec-set digest;
- `PortfolioLedger` and immutable `PortfolioSnapshot`;
- `TargetLineageRef`, `ExecutionPolicyRef`, and canonical `OrderIntent`; and
- the existing risk and execution consumer contracts.

NautilusTrader v1.230.0 is active and mature but would introduce an LGPL Rust/Python engine,
native/transitive runtime, and competing event, portfolio, clock, and execution authorities.
QuantConnect LEAN is an active Apache-2.0 architecture reference, but adopting its .NET engine or a
cross-runtime model adapter would add a second scheduler, portfolio/accounting model, object graph,
and packaging/runtime surface. Neither replaces the accepted EA lineage and authority contracts.

No dependency or lockfile change is permitted for this decision.

## Decision

### Boundary and ownership

The logical flow is:

```text
runtime-admitted market root + exact dispatch sequence
  -> strategy callback returns zero or one Phase 1 direction proposal
  -> runtime-owned verifier proves the exact root/sequence is the current active dispatch
  -> StrategySignalAuthority issues or replays one canonical StrategySignal
  -> PortfolioPlanningAuthority reads its bound ledger's latest snapshot exactly once
  -> authority issues one canonical PortfolioTarget
  -> authority records one explicit PlanningOutcome
  -> authority optionally issues one canonical OrderIntent
  -> result retains the exact snapshot object
  -> runtime later sends OrderIntent + result.portfolio_snapshot directly to risk
```

The strategy owns only strategy-local decision logic and direction proposals. The signal authority
owns `STRATEGY_SIGNAL` sequence allocation and canonical signal issuance. Portfolio owns policy,
target and intent allocation, target-current delta calculation, and planning replay/conflict state.
Runtime owns callback sequencing, dispatch, audit routing, and later risk delivery. Risk and
execution retain their existing exclusive authorities.

No public strategy or portfolio API accepts configuration snapshots, filesystem paths, data-source
ports, clocks with advance authority, runtime dispatchers, risk/execution implementations, venue
ports, audit stores, result stores, SDK values, or secrets. The one runtime dependency is a narrow
consumer-owned read-only active-dispatch verifier port; it cannot pop, acknowledge, advance time,
inspect future roots, or expose a dispatcher/lease object.

### Phase 1 strategy proposal

`SignalDirection` is a closed `StrEnum`:

```text
long
flat
short
```

Phase 1 permits at most one direction proposal for one runtime-admitted market root. A concrete
strategy callback is outside this Issue, but its narrow future port returns exactly
`SignalDirection | None`; it cannot construct a `StrategySignal`, target, intent, Order, or Fill.
`None` means that no signal issuance call occurs. The later runtime audit contract records callback
completion/no-signal; this ADR does not fabricate a signal merely to represent absence.

### Canonical StrategySignal

`StrategySignal` is an exact, immutable, factory-only value with:

```text
run_id
signal_id
instrument
direction
causal_market_sha256
causal_root_available_at
dispatch_sequence
```

Requirements:

- `signal_id` is an exact `EconomicId` owned by `STRATEGY_SIGNAL` and bound to `run_id`;
- the cause is one exact `MarketDataEnvelope` with an initial or correction bar for the same
  instrument;
- `causal_market_sha256` is authority-derived from the verifier-approved exact canonical
  market-data record bytes;
- `causal_root_available_at` equals the causal envelope's canonical `available_at`;
- `dispatch_sequence` is the positive run-wide sequence from the exact live market lease; and
- the caller cannot provide or override any digest, time, instrument, or identity lineage derived
  from the market root.

The canonical signal schema is `ea.phase1-strategy-signal.v1`, canonicalization identifier is
`ea-canonical-json-v1`, and digest domain is:

```text
b"ea.phase1-strategy-signal.v1\0"
```

The causal market digest domain is:

```text
b"ea.phase1-strategy-causal-market.v1\0"
```

Its exact preimage is the domain followed by the canonical market-data record byte length as one
unsigned eight-byte big-endian integer, followed by the exact
`canonical_market_data_record_bytes(market_root)` bytes. The digest is lowercase SHA-256. No JSON
reparse, object `repr`, path, source iterator, or caller-provided digest enters this preimage.

The canonical JSON object has exactly:

```json
{
  "canonicalization": "ea-canonical-json-v1",
  "causal_market_sha256": "<lowercase sha256>",
  "causal_root_available_at": "YYYY-MM-DDTHH:MM:SS.ffffffZ",
  "direction": "long|flat|short",
  "dispatch_sequence": 1,
  "instrument": {"symbol": "AAPL", "venue": "XNAS"},
  "run_id": "<uuid>",
  "schema": "ea.phase1-strategy-signal.v1",
  "signal_id": {
    "owner_kind": "strategy.signal",
    "owner_sequence": 1,
    "run_id": "<uuid>"
  }
}
```

JSON is UTF-8, ASCII-safe, key-sorted, compact, and rejects NaN or implicit coercion.

### Active market dispatch proof

`ActiveMarketDispatchVerifierPort` is a consumer-owned Protocol in `ea.core.strategy`:

```python
verify_active_market_dispatch(
    market_root: MarketDataEnvelope,
    *,
    dispatch_sequence: int,
) -> ActiveMarketDispatchProof
```

`ActiveMarketDispatchProof` is an exact, immutable, factory-only, process-local capability in
`ea.core.runtime`. It is not canonical evidence and cannot be serialized. It retains:

```text
run_id
exact market_root object identity
exact canonical market bytes
causal market digest
dispatch_sequence
private factory seal
```

The concrete implementation is issued by `ea.runtime` and is bound to one run-wide dispatcher.
Verification succeeds only while:

- the dispatcher has one active `RuntimeDispatchLease`;
- the active root is the exact same `market_root` object, not an equal copy;
- the active root is a `MarketDataEnvelope`;
- the live lease sequence exactly equals `dispatch_sequence`; and
- the verifier/dispatcher/run binding and canonical root bytes remain unchanged.

Stale, copied, foreign-run, foreign-verifier, already-acknowledged, future, and never-admitted roots
fail closed. The verifier never pops or acknowledges a lease and cannot advance the clock.
`ea.runtime` alone can construct the sealed proof after checking its private live state; strategy,
portfolio, composition callers, and structural fake ports cannot mint it.

The exact active proof is required only for a new signal. Signal-authority exact replay is resolved
from already retained canonical input/result before calling the verifier, so a legitimate retry
after runtime acknowledgement returns the original signal without requiring the dispatch to remain
active. A conflicting reuse of an already occupied dispatch sequence is also classified from
retained evidence before verifier access and monotonically halts; it cannot probe or replace the
active dispatch.

### StrategySignalAuthority

`StrategySignalAuthority` is factory-only, single-run, single-use mutable issuance state. Its
public operation is:

```python
issue(
    market_root: MarketDataEnvelope,
    *,
    dispatch_sequence: int,
    direction: SignalDirection,
) -> StrategySignal
```

The authority is constructed with one exact `run_id` and one
`ActiveMarketDispatchVerifierPort`. For a new issuance it requires the verifier's exact sealed
proof and rechecks the proof's run, root object, canonical bytes, digest, and sequence before
building the signal. The authority:

- starts `signal_next` at one and never emits owner sequence zero;
- accepts only positive exact uint64 dispatch sequences;
- requires every new signal's dispatch sequence to be strictly greater than the last newly issued
  signal's dispatch sequence; skipped runtime sequences are valid because a strategy may emit no
  signal for intervening roots;
- permits at most one signal identity per dispatch sequence;
- indexes replay by dispatch sequence and retains exact canonical market bytes, direction, signal
  bytes/digest, and result object;
- returns the exact prior object for exact root/direction replay without consuming a sequence;
- treats reuse of a dispatch sequence with different root bytes, instrument, time, or direction as
  a conflict and enters a monotone halted state; and
- rejects all later new issuance while halted, while exact replay of an already recorded signal
  remains available.

Validation, next identity, canonical signal bytes/digest, replay record, and complete immutable
next state are precomputed before one state-reference publication. Unexpected canonicalization or
validation failure leaves all state unchanged. Sequence exhaustion fails before publication.

### Canonical Phase1PortfolioPolicy

`PortfolioPolicyId` is a canonical non-empty ASCII identifier matching
`[a-z][a-z0-9._-]{0,127}`.

One `Phase1PortfolioPolicyEntry` contains:

```text
instrument
target_quantity
```

`target_quantity` is an exact strictly positive canonical position quantity on the bound
instrument's quantity grid. Entries form one non-empty tuple in canonical instrument order with no
duplicates. The policy is bound to one exact `InstrumentExecutionSpecSet` by ID and digest.

The policy maps direction to an absolute signed desired position:

```text
long  -> +target_quantity
flat  -> exact canonical zero on the instrument quantity grid
short -> -target_quantity
```

The policy does not inspect cash, prices, risk limits, leverage, margin, or venue state. Risk
remains responsible for allow/resize/reject. Phase 1 deliberately supports symmetric long/short
targets; a strategy/configuration that must forbid short proposals does so before signal issuance,
and risk remains the final mandatory gate.

The canonical policy schema is `ea.phase1-portfolio-policy.v1`; digest domain is:

```text
b"ea.phase1-portfolio-policy.v1\0"
```

The canonical document contains policy ID, spec-set ID/digest, and the ordered instrument/target
quantity entries. Its exact closed JSON object is:

```json
{
  "canonicalization": "ea-canonical-json-v1",
  "entries": [
    {
      "instrument": {"symbol": "AAPL", "venue": "XNAS"},
      "target_quantity": "100"
    }
  ],
  "instrument_spec_set_id": "<canonical id>",
  "instrument_spec_set_sha256": "<lowercase sha256>",
  "policy_id": "<canonical id>",
  "schema": "ea.phase1-portfolio-policy.v1"
}
```

Decimal fields are exact `CanonicalDecimal.text` JSON strings, matching the existing canonical
decimal contract; JSON numbers and coefficient/scale objects are forbidden. The policy factory
validates all entries against the supplied spec set and derives its digest; callers cannot provide
it.

### Canonical PortfolioTarget

`PortfolioTarget` is an exact, immutable, authority-issued value with:

```text
run_id
target_id
correlation_id
causation_id
signal_sha256
instrument
target_position
portfolio_policy_id
portfolio_policy_sha256
portfolio_snapshot_version
portfolio_snapshot_sha256
causal_root_available_at
dispatch_sequence
instrument_specification_id
instrument_spec_set_id
instrument_spec_set_sha256
```

`correlation_id` and `causation_id` both name the exact originating `STRATEGY_SIGNAL` in Phase 1.
`target_id` is owned by `PORTFOLIO_TARGET`. The authority derives every digest and specification
field from its bound policy, spec set, signal, and latest ledger snapshot. `target_position` is the
policy's absolute signed desired position, not an order quantity.

The canonical target schema is `ea.phase1-portfolio-target.v1`; digest domain is:

```text
b"ea.phase1-portfolio-target.v1\0"
```

Canonical bytes include every field above using the existing canonical identity, decimal,
instrument, digest, and UTC representations. The exact closed JSON object is:

```json
{
  "canonicalization": "ea-canonical-json-v1",
  "causal_root_available_at": "YYYY-MM-DDTHH:MM:SS.ffffffZ",
  "causation_id": {
    "owner_kind": "strategy.signal",
    "owner_sequence": 1,
    "run_id": "<uuid>"
  },
  "correlation_id": {
    "owner_kind": "strategy.signal",
    "owner_sequence": 1,
    "run_id": "<uuid>"
  },
  "dispatch_sequence": 1,
  "instrument": {"symbol": "AAPL", "venue": "XNAS"},
  "instrument_spec_set_id": "<canonical id>",
  "instrument_spec_set_sha256": "<lowercase sha256>",
  "instrument_specification_id": "<canonical id>",
  "portfolio_policy_id": "<canonical id>",
  "portfolio_policy_sha256": "<lowercase sha256>",
  "portfolio_snapshot_sha256": "<lowercase sha256>",
  "portfolio_snapshot_version": 0,
  "run_id": "<uuid>",
  "schema": "ea.phase1-portfolio-target.v1",
  "signal_sha256": "<lowercase sha256>",
  "target_id": {
    "owner_kind": "portfolio.target",
    "owner_sequence": 1,
    "run_id": "<uuid>"
  },
  "target_position": "100"
}
```

Identity and instrument objects are the exact parsed documents from their existing canonical
encoders; `target_position` is the exact signed `CanonicalDecimal.text` JSON string.
`TargetLineageRef` is constructed only from the authority-issued target ID and derived target
digest.

### Latest snapshot authority

`PortfolioPlanningAuthority` is constructed with the exact factory-issued `PortfolioLedger` for
its run and spec set. It does not accept a snapshot argument in `plan()`.

For a new signal, it reads `ledger.snapshot` exactly once and retains that immutable object for the
entire planning transaction. The snapshot must:

- be an exact `PortfolioSnapshot`;
- match the authority run ID;
- match the exact instrument spec-set ID/digest; and
- be canonically encodable with a derived digest.

This binding makes the latest ledger snapshot at the serialized call boundary authoritative and
prevents a caller from selecting a stale snapshot. The later runtime coordinator must serialize
ledger application and planning callbacks; concurrent ledger mutation is outside Phase 1 and
forbidden by the graph contract.

Exact replay is resolved before reading the ledger. A redelivered signal therefore returns its
original result even if the ledger has since advanced; it never replans or emits a second intent.
The exact retained immutable `PortfolioSnapshot` is included in the planning result and is returned
by object identity on replay. Runtime must pass that object directly to
`risk.evaluate(result.intent, result.portfolio_snapshot)` and must not reread the ledger. Before
risk delivery it rechecks exact object identity, canonical bytes/digest, snapshot version, run,
spec-set binding, and equality with the target/outcome/result evidence. A same-version snapshot
with different balances/digest is a conflict, not an interchangeable snapshot.

### Target-current calculation

The current position is the exact position balance for the signal instrument in the retained
snapshot, or canonical grid-aligned zero when absent. Duplicate position entries are already
forbidden by `PortfolioSnapshot`.

The authority computes:

```text
delta = target_position - current_position
```

using exact coefficient/scale integer arithmetic, never `float` or ambient `Decimal`. It validates
the target, current position, delta, and absolute delta on the instrument quantity grid.

- `delta > 0` produces `OrderSide.BUY` and positive quantity `delta`;
- `delta < 0` produces `OrderSide.SELL` and positive quantity `abs(delta)`;
- exact zero produces no intent.

There is no tolerance, epsilon, implicit rounding, lot aggregation, cash reservation, or price
lookup. Off-grid or structurally invalid state fails before target/result publication.

### Planning outcomes and result

`PlanningOutcomeKind` is closed:

```text
intent_emitted
already_at_target
blocked_unresolved_fills
```

Every authority-issued target produces exactly one immutable `PlanningOutcome`:

- `intent_emitted`: exactly one canonical `OrderIntent` is present;
- `already_at_target`: delta is exact zero and no intent is present; or
- `blocked_unresolved_fills`: the retained snapshot contains one or more unresolved Fill
  references and no intent is present.

Unresolved Fills make current economic ownership incomplete. Phase 1 records the target but blocks
new intent issuance until later reconciliation resolves them; it does not guess exposure.

The outcome records run, target ID/digest, signal ID/digest, policy ID/digest, snapshot
version/digest, before/after target and intent sequence positions, kind, optional intent
ID/digest, and exact delta. `PortfolioPlanningResult` contains the exact signal, target, retained
portfolio snapshot, outcome, and optional intent objects. The result is immutable and factory-only.

Canonical outcome/result schemas are:

```text
ea.phase1-planning-outcome.v1
ea.phase1-planning-result.v1
```

with digest domains:

```text
b"ea.phase1-planning-outcome.v1\0"
b"ea.phase1-planning-result.v1\0"
```

The result document embeds canonical component documents rather than relying on object identity.

The exact closed outcome JSON object is:

```json
{
  "canonicalization": "ea-canonical-json-v1",
  "delta": "100",
  "intent_id": null,
  "intent_next_after": 1,
  "intent_next_before": 1,
  "intent_sha256": null,
  "kind": "already_at_target",
  "portfolio_policy_id": "<canonical id>",
  "portfolio_policy_sha256": "<lowercase sha256>",
  "portfolio_snapshot_sha256": "<lowercase sha256>",
  "portfolio_snapshot_version": 0,
  "run_id": "<uuid>",
  "schema": "ea.phase1-planning-outcome.v1",
  "signal_id": {
    "owner_kind": "strategy.signal",
    "owner_sequence": 1,
    "run_id": "<uuid>"
  },
  "signal_sha256": "<lowercase sha256>",
  "target_id": {
    "owner_kind": "portfolio.target",
    "owner_sequence": 1,
    "run_id": "<uuid>"
  },
  "target_next_after": 2,
  "target_next_before": 1,
  "target_sha256": "<lowercase sha256>"
}
```

`intent_id` and `intent_sha256` are both `null` unless kind is `intent_emitted`; for that kind both
are present exact values. `intent_next_before`/`intent_next_after` are exact positive uint64 or
`null` when exhausted. For no-intent outcomes they are identical, including `null`; for
`intent_emitted`, `after` is `before + 1` or `null` when `before` was uint64 max.
`target_next_before` is always a positive uint64 for a successful new result;
`target_next_after` is `before + 1` or `null` at uint64 max. Exact replay returns the recorded
positions unchanged. `delta` is signed target-minus-current; outcome coupling is:

```text
intent_emitted          -> delta != 0, intent fields present, intent sequence consumed
already_at_target       -> delta == 0, intent fields null, intent sequence unchanged
blocked_unresolved_fills -> any delta, intent fields null, intent sequence unchanged
```

The exact closed result document is constructed as follows; `parse` means strict JSON decoding of
the named canonical bytes, with no substitution, omission, or re-encoding of a nested document:

```python
{
    "canonicalization": "ea-canonical-json-v1",
    "intent": (
        None
        if result.intent is None
        else parse(canonical_order_intent_bytes(result.intent))
    ),
    "outcome": parse(canonical_planning_outcome_bytes(result.outcome)),
    "portfolio_snapshot": parse(
        canonical_portfolio_snapshot_bytes(result.portfolio_snapshot)
    ),
    "schema": "ea.phase1-planning-result.v1",
    "signal": parse(canonical_strategy_signal_bytes(result.signal)),
    "target": parse(canonical_portfolio_target_bytes(result.target)),
}
```

Thus the snapshot value is the complete existing `ea-portfolio-snapshot-v1` document with
`canonicalization`, balances, spec-set lineage, ledger lineage, run, version, and unresolved Fill
references. The intent value is `null` exactly when the outcome has no intent; otherwise it is the
complete existing `ea-order-intent-v1` document. An abbreviated schema marker is never valid. The
result digest covers all nested evidence, including balances and unresolved Fill references, not
only snapshot version.

### OrderIntent issuance

For `intent_emitted`, the authority calls the existing `create_order_intent` with:

- a newly allocated `PORTFOLIO_INTENT` ID from `intent_next`;
- `correlation_id` equal to the originating signal ID;
- `TargetLineageRef` derived from the exact target;
- instrument, side, and positive exact delta quantity;
- the retained snapshot version;
- causal root availability and dispatch sequence copied from the signal;
- the authority's exact spec set; and
- its exact `ExecutionPolicyRef`.

The existing factory derives `causation_id=target_id`, market order kind,
`good_for_next_eligible_market_event`, specification lineage, and execution policy lineage.
Portfolio cannot override those derived fields.

One Phase 1 target emits at most one intent. Multi-instrument/basket target allocation requires a
later versioned contract rather than overloading this result.

### PortfolioPlanningAuthority state and replay

The authority is bound at construction to exact:

```text
run_id
PortfolioLedger object identity
InstrumentExecutionSpecSet
Phase1PortfolioPolicy
ExecutionPolicyRef
```

Its public operation is:

```python
plan(signal: StrategySignal) -> PortfolioPlanningResult
```

State contains:

```text
halted
last_new_signal_dispatch_sequence
target_next
intent_next
replay_index_by_signal_id
conflict_evidence
```

`signal_next`, `target_next`, and `intent_next` are `int | None`: issuance at exact uint64 max is
valid and publishes `None`; any later new issuance requiring that owner fails without mutation.
`last_new_*` is `None` before the first issuance/result. Existing exact replay never changes any
next/last/halt field.

Both owner sequences start at one. Rules:

1. exact signal runtime type, canonical bytes/digest, run, instrument, policy membership, spec
   binding, positive dispatch sequence, and signal owner are validated;
2. an existing signal ID with identical canonical bytes returns the exact prior result without
   reading the ledger or consuming any sequence;
3. an existing signal ID with different bytes, or a new signal whose dispatch sequence is not
   strictly greater than the last newly planned signal, records deterministic conflict evidence
   and monotonically halts the authority;
4. a halted authority rejects later new signals but continues exact replay;
5. target sequence exhaustion fails before snapshot read or mutation;
6. the exact latest snapshot is read once and validated;
7. target, target digest/ref, delta, outcome, optional intent, all canonical bytes/digests, replay
   record, conflict-free indexes, and complete immutable next state are precomputed;
8. intent sequence is consumed only for `intent_emitted`; target sequence is consumed for every
   successful new planning result; and
9. one state-reference assignment publishes the result. Any exception before publication leaves
   ledger and authority state unchanged.

The planner never mutates the ledger. No audit, runtime acknowledgement, risk call, Order creation,
or venue effect occurs in `plan()`.

### Public authority state and conflict evidence

Both authorities expose an immutable exact state snapshot and read-only lookup:

```python
authority.state -> StrategySignalAuthorityState | PortfolioPlanningAuthorityState
signal_authority.lookup_by_dispatch_sequence(sequence) -> StrategySignal | None
planning_authority.lookup_by_signal_id(signal_id) -> PortfolioPlanningResult | None
```

Lookups perform no allocation or state change. They return the exact retained object.

`AuthorityConflictKind` is closed:

```text
identity_reuse
non_monotone_dispatch
```

`AuthorityKind` is closed:

```text
strategy_signal
portfolio_planning
```

One factory-only immutable `AuthorityConflictEvidence` records:

```text
authority_kind
conflict_kind
run_id
occupied_owner_id (EconomicId | null)
dispatch_sequence
last_new_dispatch_sequence (int | null)
existing_sha256 (Sha256Digest | null)
submitted_sha256
```

For occupied identity/dispatch reuse, occupied ID and existing digest are present. For a previously
unoccupied non-monotone dispatch, occupied ID and existing digest are null. Submitted digest is
derived from the complete exact submitted signal input (signal authority: verified/canonical market
bytes + direction + sequence; planning authority: canonical StrategySignal bytes). The first
conflict is retained forever. Calls while halted resolve exact replay first; every other call fails
with the existing halt and never overwrites evidence.

The signal-authority submitted-input digest preimage is
`b"ea.phase1-strategy-signal-submission.v1\0"`, followed in order by the canonical market-record
byte length as unsigned eight-byte big-endian, those exact bytes, the dispatch sequence as unsigned
eight-byte big-endian, the direction ASCII byte length as unsigned eight-byte big-endian, and the
exact enum-value ASCII bytes. The planning-authority submitted-input digest preimage is
`b"ea.phase1-portfolio-planning-submission.v1\0"`, followed by the canonical signal byte length as
unsigned eight-byte big-endian and those exact bytes.

The exact closed conflict JSON object is:

```json
{
  "authority_kind": "strategy_signal|portfolio_planning",
  "canonicalization": "ea-canonical-json-v1",
  "conflict_kind": "identity_reuse|non_monotone_dispatch",
  "dispatch_sequence": 1,
  "existing_sha256": null,
  "last_new_dispatch_sequence": null,
  "occupied_owner_id": null,
  "run_id": "<uuid>",
  "schema": "ea.phase1-authority-conflict.v1",
  "submitted_sha256": "<lowercase sha256>"
}
```

Every absent optional is JSON `null`; a present `occupied_owner_id` is the complete canonical
EconomicId object and present digests are lowercase SHA-256 strings. Its digest domain is:

```text
b"ea.phase1-authority-conflict.v1\0"
```

The exact closed `StrategySignalAuthorityState` JSON object is:

```json
{
  "canonicalization": "ea-canonical-json-v1",
  "conflict": null,
  "halted": false,
  "issuance_count": 0,
  "last_new_dispatch_sequence": null,
  "run_id": "<uuid>",
  "schema": "ea.phase1-strategy-signal-authority-state.v1",
  "signal_next": 1
}
```

Its digest domain is
`b"ea.phase1-strategy-signal-authority-state.v1\0"`.

The exact closed `PortfolioPlanningAuthorityState` JSON object is:

```json
{
  "canonicalization": "ea-canonical-json-v1",
  "conflict": null,
  "halted": false,
  "intent_next": 1,
  "last_new_signal_dispatch_sequence": null,
  "result_count": 0,
  "run_id": "<uuid>",
  "schema": "ea.phase1-portfolio-planning-authority-state.v1",
  "target_next": 1
}
```

Its digest domain is
`b"ea.phase1-portfolio-planning-authority-state.v1\0"`.

Sequence and last-dispatch fields are exact positive uint64 or JSON `null`; counts are exact
non-negative uint64 and count successful new issuance/results only. `conflict` is either JSON
`null` or the complete exact conflict document above. State snapshots, conflict bytes/digests,
submitted input digest, replay record, and next state are precomputed before one publication.
Canonical helpers and state/conflict properties are public read-only evidence for the later
coordinator; private mappings and capability seals are not serialized.

### Failure taxonomy

Public contract/factory errors map to existing closed `OutcomeCode` values:

| Condition | Code | Mutation |
|---|---|---|
| wrong exact carrier/runtime type | `invalid_type` | none |
| empty/invalid identifier, non-UTC time, non-positive/out-of-range sequence, invalid decimal/grid | `out_of_range` | none |
| foreign run/spec/instrument/policy/ledger identity or derived-field mismatch | `conflicting_id` | none |
| stale/copied/foreign/acknowledged/unadmitted dispatch or verifier/proof mismatch | `conflicting_id` | none |
| verifier unexpected exception or incomplete proof contract | deterministic runtime/strategy boundary failure | none |
| conflicting signal replay or non-monotone new signal dispatch | `conflicting_id` | publish monotone halt/conflict evidence only |
| target or required intent owner sequence exhausted | `out_of_range` | none |
| unexpected ledger snapshot/canonicalization failure | matching structural code or deterministic wrapper | none |

Already-at-target and unresolved-Fill blocking are successful explicit planning outcomes, not
exceptions. A later runtime decides how to audit and continue after them.

### Public API and dependency direction

New public modules:

```text
ea.core.strategy
  SignalDirection
  StrategySignal
  ActiveMarketDispatchVerifierPort
  canonical_strategy_signal_bytes
  strategy_signal_digest

ea.core.runtime
  ActiveMarketDispatchProof

ea.core.portfolio_planning
  PortfolioPolicyId
  Phase1PortfolioPolicyEntry
  Phase1PortfolioPolicy
  PortfolioTarget
  PlanningOutcomeKind
  PlanningOutcome
  PortfolioPlanningResult
  canonical/digest helpers

ea.strategy
  StrategySignalAuthority
  create_strategy_signal_authority

ea.portfolio
  PortfolioPlanningAuthority
  create_portfolio_planning_authority

ea.runtime
  create_active_market_dispatch_verifier
```

`ea.core.*` remains dependency-neutral and imports only other core contracts/stdlib.
`ea.strategy` may import core only. `ea.runtime` may implement the core verifier port but cannot
import strategy implementation state. `ea.portfolio.planning` may import core contracts and the
same-package `PortfolioLedger`; it cannot import runtime, data, risk, execution implementations,
composition, experiments, CLI, adapters, or SDKs. Risk/execution consume existing core messages
and do not import strategy/portfolio implementation modules.

### Evidence retained for the later coordinator

The later runtime coordinator must route/audit:

- causal market root digest and dispatch sequence;
- exact signal bytes/digest;
- exact target bytes/digest and portfolio policy digest;
- exact retained snapshot version/digest;
- exact retained immutable snapshot object/document passed directly to risk;
- explicit planning outcome bytes/digest;
- optional intent bytes/digest; and
- canonical authority state and first-conflict/halt evidence.

This Issue exposes deterministic evidence but does not persist it. Runtime acknowledgement of the
causal market lease remains later and may occur only after every consequence selected by the full
coordinator succeeds.

## Verification

Implementation requires:

- unit tests for exact types, factory seals, enums, identifiers, UTC, dispatch/owner sequence
  bounds, policy/spec grids, canonical bytes/digests, and public constructor rejection;
- signal-authority tests for initial issuance, skipped dispatch sequences, exact replay, changed
  root/direction conflict, non-monotone dispatch, halt, exact replay while halted, exhaustion, and
  failure-atomic state;
- active-dispatch verifier tests for exact live root/lease identity and rejection of stale, equal
  copy, foreign-run, foreign-verifier, already-acknowledged, future, and never-admitted roots;
- planning tests for long/flat/short target mapping, absent/current positive/current negative
  positions, buy/sell/zero delta, exact grids, policy/spec/run mismatch, latest bound ledger
  snapshot, unresolved-Fill blocking, target/intent sequence consumption, and exhaustion;
- malicious tests for forged signal/target/policy/snapshot carriers, payload mutation outside
  identity fields, structural fake verifier/proof, caller-supplied stale snapshot attempts,
  same-version/different-balance replacement, foreign ledger identity, canonical byte mismatch, and
  unexpected verifier/snapshot/canonicalization failure;
- replay/property tests proving exact replay consumes no sequence, conflicting reuse halts, no-op
  cannot disappear, each emitted intent carries exact target/snapshot/policy/spec/root lineage, and
  each positive/negative delta maps to exactly one correct side/absolute quantity;
- boundary tests proving planning replay after ledger progress returns the exact retained snapshot
  and result, runtime-to-risk handoff uses that object without rereading ledger, and current risk
  accepts/replays only the matching intent/snapshot pair;
- canonical authority state/conflict tests covering first-conflict retention, repeat precedence,
  lookup membership, counts, and every uint64-max -> `null` transition;
- cross-process byte-golden tests across hash seed, timezone, locale, current directory, input
  container order, and ambient Decimal context;
- import-boundary tests proving strategy/portfolio policy cannot import runtime/data/risk/execution
  implementations, adapters, composition, experiments, CLI, or SDKs;
- full lint, strict typing, tests, coverage, reproducible wheels, exact-head CI, and independent
  exact-SHA Verification Owner evidence; and
- README/architecture updates that distinguish this policy slice from a concrete strategy,
  matcher, full coordinator, result/report, and complete backtest.

## Consequences

Positive:

- The previously reserved Signal/Target/Intent lineage becomes authority-backed and replay-stable.
- Portfolio, not strategy or runtime, owns desired position and target-current conversion.
- A signal can be issued only for the exact current active market dispatch.
- Planning cannot be induced to use a caller-selected stale snapshot.
- Risk receives the exact immutable snapshot that planning used, even after later ledger progress.
- Every target has explicit evidence, including no-op and unresolved-Fill blocking.
- Existing risk and OMS authorities receive their expected canonical `OrderIntent` without any
  new mode-specific path.
- The historical matcher and full coordinator can be implemented against closed, testable inputs.

Costs:

- Two small mutable issuance authorities, one read-only runtime verifier seam, and additional
  canonical schemas must be maintained.
- A planner is bound to one in-process ledger object, so restart reconstruction remains a future
  durable runtime concern.
- One signal maps to one instrument and one target/intent in Phase 1; basket allocation requires a
  new versioned contract.
- Exact replay retains result objects and canonical evidence for the run lifetime.

## Alternatives rejected

### Let strategy create PortfolioTarget or OrderIntent

Rejected because it collapses the strategy/portfolio boundary, lets strategy choose sizing and
snapshot lineage, and can bypass portfolio replay and the mandatory risk path.

### Pass PortfolioSnapshot into every plan call

Rejected because a caller could select an older valid snapshot. Binding the authority to the exact
ledger and reading its immutable current snapshot once makes latest-state ownership explicit.

### Trust a caller-provided market root and sequence

Rejected because exact values alone do not prove runtime admission or active-lease authority. The
runtime verifier must bind object identity and sequence to current live dispatcher state.

### Derive intents directly from Signal without a target

Rejected because it erases desired-state evidence, makes no-op invisible, and breaks the accepted
Signal -> PortfolioTarget -> OrderIntent lineage.

### Treat already-at-target as no return value

Rejected because absence cannot distinguish a successful no-op from skipped, failed, or lost
planning.

### Replan exact signal redelivery against the current snapshot

Rejected because it can create a different target outcome or second intent after ledger progress.
Exact replay returns the original result.

### Use float tolerances or Decimal ambient context for delta

Rejected because order quantity and target position require exact grid semantics and reproducible
cross-process results.

### Import NautilusTrader or LEAN portfolio models

Rejected because the broad engines and competing identities/state authorities cost more to adapt
than the bounded local contracts and would undermine the already accepted EA pipeline.

## Deferred work

- concrete strategy implementations and strategy-local state/checkpoint policy;
- optional feature stage and timer-originated signals;
- multi-instrument/basket targets, allocation, cash reservation, leverage and optimization;
- historical matcher, submission and execution-fact production;
- full lifecycle/audit coordinator, stop/failure drain and reconciliation integration;
- durable authority reconstruction/recovery;
- deterministic result/P&L/report adapters and backtest CLI;
- paper/live strategy scheduling and external venue behavior.
