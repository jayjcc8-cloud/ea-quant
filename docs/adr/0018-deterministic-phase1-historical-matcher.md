# ADR 0018: Deterministic Phase 1 Historical Matcher

- Status: Proposed
- Date: 2026-07-30
- Decision owners: Architecture, Execution, Backtest, Runtime, Data
- Related: ADR 0003, ADR 0004, ADR 0006, ADR 0008, ADR 0010, ADR 0011,
  ADR 0012, ADR 0013, ADR 0014, ADR 0016, ADR 0017, Issue #59

## Context

Accepted ADR 0008 freezes the first historical matching policy:

- only market Orders with `good_for_next_eligible_market_event`;
- a full Fill on the first later eligible initial raw Bar for the same instrument;
- exact next-Bar-close quantization with an adverse half-tick tie;
- zero fee, slippage, and latency; and
- `order.expired.no_eligible_market_data` when the bounded source ends first.

The repository now has canonical market roots and ordering, a bounded historical source/runtime,
an active market-dispatch proof, portfolio/risk/Order authorities, canonical execution facts and
ingresses, a fact authority, a Fill ledger, and strategy/portfolio planning. It does not have a
simulated venue or matcher.

Implementing the matcher as an unconstrained helper would create three unsafe gaps:

1. a caller could register an arbitrary or unaudited Order;
2. the matcher could inspect a future source row instead of the exact active runtime root; and
3. converting a binary64 close through `Decimal(float)` or ambient decimal context could change
   the selected tick.

There is also a compatibility gap in the existing execution-fact vocabulary. The outcome registry
already reserves `order.expired.no_eligible_market_data`, and a lifecycle fact already stores an
outcome code, but lifecycle construction and decoding accept only the generic `order.expired`.

[Issue #59](https://github.com/jayjcc8-cloud/ea-quant/issues/59) owns this decision and its first
implementation. The complete lifecycle coordinator remains later work.

## Scope

This decision freezes:

- matcher/simulated-venue ownership and dependency direction;
- proof required before simulated submission;
- canonical submission receipts and dispatch batches;
- pending-Order ordering, replay, conflict, and sequence behavior;
- current-active-root verification and no-look-ahead eligibility;
- exact binary64-to-price-grid quantization;
- canonical trade/expiry fact and ingress issuance;
- bounded-end expiry;
- the narrow lifecycle-fact compatibility extension; and
- the handoff contract for the later coordinator.

## Non-goals

This decision does not implement:

- the complete lifecycle/stage coordinator or audit store;
- a real venue, broker adapter, credential, paper/live submission, release, or deployment;
- portfolio ledger application, reconciliation correction, or result durability;
- a concrete strategy, optimizer, result/report adapter, or backtest CLI;
- partial fills, cancellation, amendment, stop/limit orders, volume participation, liquidity,
  calendars, margin, settlement timing, corporate actions, non-zero fees, slippage, latency, or
  market impact;
- source/frontier lookahead, clock advancement, filesystem access, or network access from the
  matcher; or
- a dependency or lockfile change.

## Reuse decision

The bounded reuse assessment is recorded in Issue #59.

The selected implementation reuses the repository's `Order`, instrument specification,
`MarketDataEnvelope`, runtime root ordering, active-dispatch proof, execution-fact/ingress,
canonical codec, and exact-economic contracts plus Python 3.12
`float.as_integer_ratio()` and unbounded integers.

NautilusTrader and QuantConnect LEAN remain scenario and architecture references. Direct reuse
would import a competing runtime, clock, order model, matching state, operational closure, and
either native Rust/Python or .NET packaging. It would not remove the need to prove this project's
root ordering, audit authorization, fact identity, canonical bytes, or ledger lineage. No new
dependency earns its integration, supply-chain, migration, or lock-in cost.

## Decision

### Ownership and dependency direction

Execution owns `Phase1HistoricalMatcher` because it is the Phase 1 simulated venue and owns
definitive simulated submission, pending venue state, fact identity, and fact issuance.

Runtime owns:

- root admission and ordering;
- the active dispatch;
- persisted audit acknowledgement;
- the decision to call the venue;
- causal-descendant processing; and
- lifecycle sequencing.

The matcher does not own or persist audit records. It accepts a consumer-owned read-only
authorization verifier and fails closed when that verifier cannot prove that the exact request was
authorized.

The implementation lives in:

```text
ea.core.historical_matching     immutable public receipts, batches, state, conflicts, codecs
ea.execution.matcher            the sole mutable matcher/simulated-venue authority
ea.runtime.matcher              runtime-owned active market/end-dispatch verifier adapter
```

`ea.execution.matcher` may import only focused `ea.core` modules. It must not import `ea.runtime`,
`ea.strategy`, `ea.portfolio`, `ea.risk`, configuration, composition, experiments, adapters,
vendor SDKs, filesystem, subprocess, socket, or network modules.

The runtime adapter may import `ea.runtime.historical` and focused core contracts. Composition
will later bind the concrete authority and verifiers.

### Bound construction

The factory is:

```python
create_phase1_historical_matcher(
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    execution_policy: ExecutionPolicyRef,
    source_namespace: SourceNamespace,
    provenance_id: FactProvenanceId,
    order_issuance_verifier: HistoricalOrderIssuanceVerifier,
    submission_authorization_verifier: HistoricalSubmissionAuthorizationVerifier,
    active_dispatch_verifier: HistoricalMatcherDispatchVerifier,
) -> Phase1HistoricalMatcher
```

Every argument has its exact runtime type. The factory reconstructs an authority-owned
instrument-specification set and execution-policy reference from validated scalar fields. It
proves that the reconstructed canonical bytes/digests equal the submitted evidence. All future
economics and lineage use only those owned projections.

The source namespace is the fact and ingress source. The provenance ID identifies this exact
matcher policy. Both are immutable construction bindings.

The factory validates the three verifier operation surfaces and their available static bindings.
It retains each verifier by identity so an opaque proof can be checked against the verifier that
issued it.

Construction performs no submission, sequence allocation, fact issuance, or external effect.

### Order issuance and audit authorization ports

`HistoricalOrderIssuanceVerifier` is execution-owned structurally:

```python
verifier.run_id -> RunId
verifier.spec_set -> InstrumentExecutionSpecSet
verifier.execution_policy -> ExecutionPolicyRef
verifier.resolve_issued_order_by_id(order_id: EconomicId) -> Order | None
```

`Phase1OrderAuthority` already satisfies this shape. The matcher calls the resolver and requires
an exact `Order` whose complete canonical bytes equal the submitted Order.

`HistoricalSubmissionAuthorizationVerifier` is consumer-owned:

```python
verifier.run_id -> RunId
verifier.has_authorized_historical_submission(
    *,
    order_id: EconomicId,
    canonical_order_bytes: bytes,
    canonical_execution_request_bytes: bytes,
    canonical_causal_market_bytes: bytes,
    causal_market_sha256: Sha256Digest,
    causal_root_key: RuntimeRootOrderKey,
    dispatch_sequence: int,
) -> bool
```

The later coordinator will implement this port from its persisted audit-acknowledgement state.
The matcher requires exact `bool`. `False`, a wrong type, an exception, missing operation, or
binding mismatch produces no receipt, sequence use, or pending Order.

The verifier does not pass an audit object into execution and does not let execution append,
amend, or interpret audit storage. It proves membership only.

### Active matcher dispatch proof

`HistoricalMatcherDispatchVerifier` exposes:

```python
verify_active_market_dispatch(
    market_root: MarketDataEnvelope,
    *,
    dispatch_sequence: int,
) -> ActiveMarketDispatchProof

verify_active_end_of_run_dispatch(
    end_root: EndOfRunRoot,
    *,
    dispatch_sequence: int,
) -> ActiveEndOfRunDispatchProof
```

`ActiveMarketDispatchProof` remains the Accepted ADR 0017 opaque, process-local proof.
`ActiveEndOfRunDispatchProof` is an equivalent sealed core value containing:

- exact run ID;
- exact `EndOfRunRoot` by identity;
- canonical terminal-root bytes;
- terminal-root digest;
- dispatch sequence;
- issuing verifier identity; and
- a private factory seal.

The runtime adapter reads only the exact currently active dispatch, obtains its retained
pre-dispatch canonical bytes, rereads those bytes, rejects change, and returns the sealed proof.
The matcher recomputes submitted bytes/digest and calls the core proof validator with the retained
verifier identity. A caller-created or cross-verifier proof fails.

The matcher receives no source, cursor, plan, next root, clock, queue state, or acknowledgement
operation.

### Simulated submission

The public operation is:

```python
matcher.submit(
    order: Order,
    *,
    causal_market_root: MarketDataEnvelope,
    dispatch_sequence: int,
) -> HistoricalSubmissionReceipt
```

The operation validates, in order:

1. exact carrier types and the minimum canonical fields needed for identity lookup;
2. exact submission replay or identity conflict;
3. existing halt/end state;
4. the closed Phase 1 Order profile;
5. authority-issued Order membership and complete canonical equality;
6. the current active causal market proof;
7. `order.run_id`, `order.dispatch_sequence`, `order.eligible_after_available_at`, instrument,
   specification-set, and execution-policy equality with the bound authority and active root;
8. audit authorization membership for the exact Order, execution request, active market bytes,
   root key, digest, and dispatch sequence;
9. sequence availability; and
10. complete receipt/pending-state precomputation before one atomic publication.

The accepted profile is exactly:

```text
order_kind == market
time_in_force == good_for_next_eligible_market_event
price_constraint is None
quantity is positive and on the bound quantity grid
causal_market_root.payload.instrument == order.instrument
causal_market_root.available_at == order.eligible_after_available_at
dispatch_sequence == order.dispatch_sequence
```

Submission sequence starts at one and is an unsigned 64-bit owner-local sequence. Zero is never
issued. The receipt carries `submission.submitted`; this is a definitive simulated-venue outcome.

The matcher reconstructs the Order through its canonical decoder and retains:

- the owned Order;
- canonical Order and execution-request bytes/digests;
- client submission key;
- submission sequence;
- exact causal market bytes/digest;
- exact causal `RuntimeRootOrderKey`;
- dispatch sequence; and
- canonical receipt.

Pending order is submission order. A multi-Order match therefore emits by ascending submission
sequence. Order ID is an asserted unique secondary identity, not an alternative ordering source.

### Canonical submission receipt

`HistoricalSubmissionReceipt` is factory-only, immutable, and version one. Its canonical fields
are:

```text
schema_version
canonicalization
run_id
source_namespace
outcome_code = submission.submitted
submission_sequence
order_id
order_sha256
execution_request_sha256
client_submission_key
instrument
side
quantity
causal_market_sha256
causal_root_key
dispatch_sequence
eligible_after_available_at
instrument_spec_set_id
instrument_spec_set_sha256
execution_policy_id
execution_policy_sha256
```

`causal_root_key` is encoded as one tagged, type-preserving JSON projection of
`RuntimeRootOrderKey.as_tuple()`. UTC times use the existing canonical UTC form. No Python tuple
repr, hash, or object address enters the bytes.

Receipt bytes use canonical sorted compact JSON. Its digest domain is:

```text
b"ea.phase1-historical-submission-receipt.v1\0"
```

Exact Order redelivery with identical submitted inputs returns the same receipt object and
consumes nothing. Reuse of Order ID, client key, or submission identity with different canonical
evidence publishes the first immutable matcher conflict, enters a monotone halt, and raises
`validation.conflicting_id`.

### Market-root observation

The public operation is:

```python
matcher.match_active_market_root(
    market_root: MarketDataEnvelope,
    *,
    dispatch_sequence: int,
) -> HistoricalMatcherDispatchBatch
```

It first validates the exact active market proof. For a new dispatch, dispatch sequence must be
strictly greater than the last new matcher dispatch sequence. Exact retry of an already retained
dispatch with identical canonical root bytes returns the retained batch before halt/end checks.
Same sequence with different root bytes, a non-monotone new sequence, or root identity conflict
publishes the first matcher conflict and halts.

The matcher retains only the submitted current root bytes, digest, key, and result. It cannot ask
for another root.

### Eligibility

A pending Order is eligible on a market root only when all conditions hold:

1. exact same instrument;
2. `order_kind == market`;
3. `time_in_force == good_for_next_eligible_market_event`;
4. `price_constraint is None`;
5. `market_root.payload.adjustment == raw`;
6. `market_root.revision == 0`;
7. current `RuntimeRootOrderKey` is strictly greater than the retained causal root key; and
8. `market_root.event_time > order.eligible_after_available_at`.

The event-time condition plus the market invariant
`available_at >= event_time` also makes candidate availability strictly later than the causal
availability. The explicit root-key comparison remains mandatory evidence.

Correction roots, non-raw roots, other instruments, and late initial Bars whose event time is not
strictly later are observed and recorded in an empty dispatch batch but cannot execute an Order.
They do not remove, reprioritize, or extend a pending Order.

Every eligible pending Order fills on this first eligible root. The operation precomputes the
entire ordered batch and next state. If any price, identity, provenance, fact, ingress, sequence,
or canonical preflight fails, no Order is removed and no sequence or dispatch result is published.

### Exact binary64 next-Bar-close price

For each eligible Order:

1. require the Bar close to have exact runtime type `float` and be finite;
2. call `close.as_integer_ratio()` to obtain exact integers `p, q` with `q > 0`;
3. read price quantum coefficient `c > 0` and scale `s` from the authority-owned canonical
   specification, so the quantum is `c / 10**s`;
4. form the exact tick ratio:

   ```text
   N = p * 10**s
   D = q * c
   ```

5. compute floor quotient and non-negative remainder with `k, r = divmod(N, D)`;
6. select:

   ```text
   2*r < D  -> k
   2*r > D  -> k + 1
   2*r == D and BUY  -> k + 1
   2*r == D and SELL -> k
   ```

7. form exact coefficient `ticks * c` at scale `s`, remove only canonical fractional trailing
   zeroes, and construct `CanonicalDecimal` from the resulting plain decimal text; and
8. apply the bound instrument `PriceDomain` and existing price-grid validator.

This is numerical upward rounding for BUY and downward rounding for SELL, including signed prices.
It never calls `Decimal(float)`, `str(float)` for economics, `round`, a fixed-precision Decimal
operation, or an ambient decimal context.

If the normalized result cannot satisfy the existing 38 significant/20 integer/18 fractional
limits, the operation returns `validation.arithmetic_overflow`. A forbidden positive,
non-negative, or signed-domain value returns `validation.price_domain`. A malformed quantum or
off-grid internal result is `validation.conflicting_id`, because construction already bound a
valid specification.

### Trade fact and ingress issuance

For every eligible Order the matcher creates exactly one `trade` fact and one ingress:

- fact source and ingress source equal the bound source namespace;
- `SourceNativeSequence(fact_sequence)` is the fact dedup identity;
- ingress sequence equals the same positive `fact_sequence`;
- fact `occurred_at` equals the triggering Bar `event_time`;
- ingress `available_at` equals the triggering market root `available_at`;
- instrument, side, and full quantity equal the Order;
- price is the exact quantized close;
- the existing trade factory supplies one zero commission entry in settlement currency;
- client submission key, Order ID, correlation ID, and causation ID are all known and exact;
- venue Order ID remains absent; and
- the retained pending Order is removed only in the same atomic publication as the issued ingress.

Fact sequence starts at one and is unsigned 64-bit. It advances once per emitted fact in
submission order.

`FactProvenance.source_payload_sha256` is the domain-separated digest of this canonical matcher
observation:

```text
schema/canonicalization
fact_sequence
fact_kind
source_namespace
provenance_id
submission_receipt_sha256
order_sha256
trigger_root_kind
trigger_root_sha256
trigger_root_key
trigger_dispatch_sequence
occurred_at
available_at
instrument
side
quantity
price or expiry outcome
instrument_spec_set_id/digest
execution_policy_id/digest
```

Its domain is:

```text
b"ea.phase1-historical-matcher-observation.v1\0"
```

The matcher retains canonical fact and ingress bytes and implements the runtime queue's existing
`ExecutionFactIssuanceVerifier` shape:

```python
has_issued_ingress(
    *,
    ingress_identity: IngressIdentity,
    canonical_ingress_bytes: bytes,
    canonical_fact_bytes: bytes,
) -> bool
```

It returns exact `False` for absent or byte-mismatched evidence and performs no mutation.

### Canonical dispatch batch

`HistoricalMatcherDispatchBatch` is a factory-only immutable version-one result for either a
market or end-of-run dispatch. It carries:

```text
run_id
source_namespace
dispatch_kind = market | end_of_run
dispatch_sequence
trigger_root_sha256
trigger_root_key
fact_sequence_before
fact_sequence_after
submission_sequences
order_ids
ingress_identities
ingress_sha256 values
```

The ordered ingress tuple is retained by identity as a public property but canonical batch bytes
contain each ingress digest rather than recursively duplicating complete ingress documents.
Every digest is revalidated against retained ingress bytes before replay.

An empty market observation is a canonical empty batch with equal before/after sequence and empty
tuples. It is not `None` and cannot silently disappear from replay evidence.

The batch digest domain is:

```text
b"ea.phase1-historical-matcher-dispatch-batch.v1\0"
```

### Bounded-end expiry

The public operation is:

```python
matcher.expire_at_active_end(
    end_root: EndOfRunRoot,
    *,
    dispatch_sequence: int,
) -> HistoricalMatcherDispatchBatch
```

Only `EndOfRunKind.BOUNDED_SOURCE_EXHAUSTED` is accepted by this Phase 1 operation. Requested or
failure cutovers are coordinator policy, not fabricated no-data evidence.

The matcher validates the sealed active end proof, run ID, monotone dispatch, root bytes/key, end
state, and complete sequence capacity before publication. It emits one expiry fact for every
still-pending Order in submission order:

- fact kind `expiry`;
- outcome `order.expired.no_eligible_market_data`;
- fact and ingress sequence as above;
- occurred and available time equal `end_root.available_at`;
- exact Order instrument, client key, Order ID, correlation, and causation;
- no venue Order ID; and
- provenance binds the receipt, Order, terminal root, and exact expiry code.

No Fill exists. All pending Orders are removed atomically and the matcher becomes ended. After
that, new submission, market matching, or a distinct end root fails closed. Exact replay of any
retained submission or dispatch remains available.

An exact end retry returns the same retained batch with no sequence use.

### Lifecycle-fact compatibility extension

The existing v1 lifecycle payload already contains `outcome_code`, and ADR 0008 already reserves
`order.expired.no_eligible_market_data`. Therefore no message schema or digest-domain version is
changed.

The implementation extends lifecycle construction to accept an optional exact `outcome_code`:

```python
create_lifecycle_execution_fact(..., outcome_code: OutcomeCode | None = None)
```

Rules:

- absent uses the existing kind-to-generic-code mapping;
- acknowledgement, rejection, and cancellation accept only their existing generic code;
- expiry accepts `order.expired` or `order.expired.no_eligible_market_data`; and
- any other exact code/kind combination fails before fact construction.

The decoder parses the lifecycle payload code and passes it into the factory. Existing generic
fact bytes and factory calls remain byte-identical. The new expiry code now round-trips
canonically.

Order projection continues to map fact kind `expiry` to projection state `expired`. The more
specific outcome remains in the immutable fact payload and provenance.

### Replay, conflict, halt, and state

The matcher exposes one immutable `HistoricalMatcherState` with:

- run/spec/policy/source/provenance bindings and digests;
- next submission and fact sequence, or exhausted;
- ordered submission receipts;
- ordered pending Order IDs;
- ordered issued ingresses;
- last new dispatch sequence;
- ended flag and retained end batch digest;
- halted flag; and
- first immutable conflict evidence, if any.

Read-only lookup operations expose exact retained receipt, batch, and issued-ingress membership.
They never allocate, match, expire, or change halt state.

The first valid identity conflict publishes `HistoricalMatcherConflictEvidence`, preserving:

- conflict kind;
- occupied identity;
- existing and submitted canonical digests;
- submitted dispatch sequence when known;
- last successful dispatch sequence;
- pending count;
- next sequences; and
- causal trigger digest when safely derivable.

Malformed carriers that fail before safe canonical identity extraction do not publish conflict
evidence. After the first conflict, later failures cannot overwrite it.

Validation and mutation precedence is:

1. minimal exact carrier/range/canonical-encodability validation for safe replay keys;
2. exact retained replay or occupied-identity conflict;
3. halt/end check;
4. remaining new-input profile and binding validation;
5. active proof and issuance/authorization verifier calls;
6. sequence capacity;
7. full batch/state precomputation and internal canonical preflight; and
8. one state assignment.

Unexpected verifier exceptions propagate unchanged only when wrapped in a private sentinel, as in
the existing Order authority. Other structural port failures become `validation.invalid_type`.

### Public errors

`HistoricalMatcherError` is a `ValueError` with one exact `OutcomeCode` from:

```text
validation.invalid_type
validation.out_of_range
validation.not_quantized
validation.price_domain
validation.arithmetic_overflow
validation.conflicting_id
submission.blocked_by_halt
```

`submission.blocked_by_halt` is used only when an otherwise well-formed new submission reaches an
already halted matcher. Existing exact replay still succeeds. New match/end operations on halt
use `validation.conflicting_id`, because they do not represent a venue submission outcome.

Errors never use exception text for control flow.

### Later coordinator contract

For one active market root the future coordinator calls, in this order:

1. `match_active_market_root`;
2. for every returned ingress in batch order, verify matcher issuance, process the fact, apply any
   Fill to the ledger, and publish updated portfolio/risk state;
3. run strategy, portfolio, and risk;
4. create an Order from an authority-issued executable risk result;
5. persist and verify the exact pre-effect audit acknowledgement;
6. call `submit` while the same causal market dispatch remains active; and
7. acknowledge the runtime dispatch only after required stage outcomes are durable.

Matcher facts are causal descendants. They are not reinserted as independently competing roots
ahead of their causal market root.

At bounded source exhaustion the coordinator calls `expire_at_active_end`, processes every expiry
fact in batch order, and then completes result durability.

This Issue tests the port contract with sealed/malicious verifier doubles. It does not claim the
coordinator exists.

## Verification

Implementation evidence must include:

- existing generic lifecycle facts remain byte-identical;
- the specific no-data expiry fact and decode round-trip;
- authority-owned spec, policy, Order, receipt, root, fact, and ingress evidence;
- arbitrary/foreign/forged/unaudited/stale Order rejection with no sequence use;
- same-root, non-raw, correction, other-instrument, and event-time cutoff non-fill cases;
- first later eligible full fill and no later duplicate;
- BUY/SELL half-tick, above/below-half, exact-tick, negative, zero, positive, price-domain, digit
  boundary, and ambient-context vectors;
- multi-Order and multi-instrument deterministic emission ordering;
- exact submission/root/end replay and conflict halt;
- uint64 submission/fact/dispatch boundaries and atomic multi-fact exhaustion failure;
- malicious verifier return types, exceptions, fabricated proof, cross-verifier proof, caller
  mutation, retained-state mutation, and failure-atomic publication;
- end-of-source expiry with no Fill;
- property tests for permutation invariance and no-look-ahead;
- cross-process golden bytes under changed hash seed, timezone, locale, current directory, and
  ambient decimal context;
- AST import boundaries;
- focused tests, official quality/full profiles, coverage floors, byte-identical wheel builds,
  clean-wheel install/doctor, and exact-head CI on macOS and Linux.

## Consequences

- Phase 1 receives an intentionally narrow deterministic simulated venue.
- Audit authorization remains a runtime/coordinator responsibility but cannot be bypassed at the
  matcher boundary.
- The matcher cannot observe future data because its only data input is an opaque-proof-bound
  active root.
- Binary64 input remains exact without becoming floating-point execution accounting.
- Empty match observations and bounded expiry become replay-stable evidence.
- The future coordinator can compose existing OMS, matcher, fact, ledger, strategy, portfolio, and
  risk authorities without changing their ownership.

## Alternatives rejected

### Let the matcher read the historical source

Rejected because a source cursor or `next()` capability is look-ahead authority. The matcher
receives only the exact active root.

### Accept any canonical-looking Order

Rejected because canonical bytes do not prove OMS issuance, risk approval consumption, or audit
authorization.

### Make the matcher append audit records

Rejected because venue behavior and audit durability have different owners. A read-only
authorization membership proof preserves the boundary.

### Convert close through Decimal or formatted text

Rejected because `Decimal(float)`, `str(float)`, formatting, ambient context, and binary rounding
can select different ticks or exceed canonical limits differently.

### Return Fill directly

Rejected because Execution/OMS alone creates Fill from an accepted trade fact. The matcher emits
source facts; the existing fact authority owns deduplication, correlation, projection, and Fill
creation.

### Use generic order.expired only

Rejected because it loses the stable Phase 1 terminal reason already frozen by ADR 0008.

### Delay expiry until a fabricated market event

Rejected because bounded exhaustion is explicit runtime evidence and no market event may be
invented.
