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

The factory validates the three verifier operation surfaces and their complete static bindings.
It retains each verifier by identity so an opaque proof can be checked against the verifier that
issued it. Before every genuinely new submission or dispatch, it re-reads and validates the owned
specification/policy bytes and every verifier binding against the construction-time scalar,
canonical-byte, and digest baselines. Exact retained replay precedes this live validation.

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
verifier.instrument_spec_set_id -> InstrumentSpecSetId
verifier.instrument_spec_set_sha256 -> Sha256Digest
verifier.execution_policy -> ExecutionPolicyRef
verifier.verify_authorized_historical_submission(
    *,
    order_id: EconomicId,
    canonical_order_bytes: bytes,
    canonical_execution_request_bytes: bytes,
    canonical_causal_market_bytes: bytes,
    causal_market_sha256: Sha256Digest,
    causal_root_key: RuntimeRootOrderKey,
    dispatch_sequence: int,
) -> HistoricalSubmissionAuthorizationProof
```

The later coordinator will implement this port from its persisted audit-acknowledgement,
portfolio/risk freshness, halt, and instrument-gate state. The verifier performs one atomic
read-only pre-effect decision immediately before the simulated venue effect.

An authorized result is one sealed, process-local `HistoricalSubmissionAuthorizationProof`
containing:

- issuing verifier identity and a private core factory seal;
- exact run, specification-set, execution-policy, Order ID, Order digest, execution-request
  digest, causal market digest/root key, and dispatch sequence;
- the persisted audit acknowledgement ID and digest for the exact execution request;
- current portfolio snapshot version equal to `order.portfolio_snapshot_version`;
- current risk-state version equal to `order.risk_state_version`;
- exact global- and Risk-halt epochs with both states proved not halted;
- the Phase 1 instrument-gate identity/version and `held_for_order_id == order.order_id`; and
- one verifier-state version so a proof cannot be replayed as authority for a different new
  effect.

The matcher calls the core proof validator with every submitted value and the retained verifier
identity. A wrong type, fabricated proof, cross-verifier proof, changed field, or binding mismatch
publishes no receipt, sequence, or pending Order.

The verifier raises only `HistoricalPreEffectAuthorizationError` for a normal denied gate:

| Gate result | Exact code |
|---|---|
| global halt or Risk halt is currently active | `submission.blocked_by_halt` |
| portfolio or risk version is no longer current | `risk.stale_approval` |
| persisted audit acknowledgement is absent | `durability.audit_append_failed` |
| acknowledgement exists but does not bind the exact request | `durability.audit_ack_mismatch` |
| instrument gate is absent, released, or held for another Order | `validation.conflicting_id` |
| proof/binding carrier has a wrong exact type | `validation.invalid_type` |

Those errors propagate with their exact code before any venue state mutation. Any other verifier
exception propagates unchanged through the existing private-sentinel pattern.

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
8. one sealed immediate pre-effect authorization proof for the exact Order, request, persisted
   audit acknowledgement, current portfolio/risk versions, clear halt states, held instrument
   gate, active market bytes/root key/digest, and dispatch sequence;
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

The next-submission pointer is `int | None`: initial value `1`; issuing a value below
`2**64-1` advances by one; issuing `2**64-1` changes it to `None`; and `None` is the sole
exhausted representation. A genuinely new submission at `None` fails
`validation.arithmetic_overflow` before authorization, receipt precomputation, or state
publication. Exact retained replay still succeeds after exhaustion and consumes nothing.

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
audit_acknowledgement_id
audit_acknowledgement_sha256
global_halt_epoch
risk_halt_epoch
instrument_gate_id
instrument_gate_version
authorization_state_version
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

It first performs only the minimal exact type/range/canonical encoding needed to derive the
dispatch sequence and submitted root digest. It then resolves an exact retained replay or an
occupied dispatch identity with conflicting bytes. Exact replay returns the retained batch even
after the parent dispatch was acknowledged, the matcher later halted, or bounded end completed;
it does not call the active verifier or any other live port.

Only a genuinely new dispatch validates the exact active market proof. Its dispatch sequence must
be strictly greater than the last new matcher dispatch sequence. Same sequence with different root
bytes, a non-monotone new sequence, or root identity conflict publishes the first matcher conflict
and halts.

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

The matcher therefore satisfies the complete existing issuance shape:

```python
matcher.run_id -> RunId
matcher.spec_set -> InstrumentExecutionSpecSet
matcher.source_namespace -> SourceNamespace
matcher.has_issued_ingress(...) -> bool
```

The returned specification set is the authority-owned projection. Every membership call
revalidates retained ingress/fact bytes and construction bindings.

### Causal-descendant fact dispatch

This decision narrowly amends ADR 0014's trusted dispatch boundary. Matcher facts remain causal
descendants and are not inserted into the root plan, but they still require a non-forgeable
runtime dispatch proof before `Phase1ExecutionFactAuthority.process_ingress`.

Runtime constructs one `CausalDescendantFactDispatchVerifier` bound by identity to:

- the exact `Phase1HistoricalMarketRuntime` dispatcher/queue;
- the exact matcher issuance capability and its run/spec/source bindings;
- the exact matcher batch lookup capability; and
- immutable run/spec baselines and one closed source registry covering the direct root plan and
  every descendant adapter.

There is no fact-authority object identity dependency and no construction cycle. Runtime first
constructs the queue, matcher, closed source registry, and adapter; it then passes that same
adapter to the `Phase1ExecutionFactAuthority` factory, which validates the adapter's run/spec
bindings. Construction fails atomically if the matcher namespace occurs in any direct-plan fact
source or any other descendant binding. Thus one `(source_namespace, ingress_sequence)` can have
only one issuance authority.

It implements the existing complete `RuntimeFactDispatchVerifier` shape:

```python
verifier.run_id -> RunId
verifier.spec_set -> InstrumentExecutionSpecSet
verifier.resolve_active_issued_fact_dispatch(
    *,
    ingress_identity: IngressIdentity,
    canonical_ingress_bytes: bytes,
    canonical_fact_bytes: bytes,
) -> int | None
```

For each lookup it:

1. first delegates to the existing queue verifier so an independently queued active fact root
   retains byte-identical behavior;
2. if no direct fact root matches, asks the matcher for one immutable descendant binding by exact
   ingress identity and bytes;
3. proves matcher issuance membership, exact batch membership/index, batch digest, parent
   root kind/digest/key, and parent dispatch sequence;
4. proves that exact parent market/end root is still the runtime's active dispatch with retained
   canonical bytes unchanged; and
5. returns the parent dispatch sequence.

`HistoricalMatcherDescendantBinding` is factory-only and retained in matcher state. It contains
the ingress identity/digests, batch digest, zero-based batch index, parent kind/digest/key, and
parent dispatch sequence. A matcher lookup returns exact `False`/`None` for an absent or
byte-mismatched ingress and performs no mutation.

The fact authority already resolves exact processed-ingress replay before calling its dispatch
verifier. Therefore post-ack replay of an already processed descendant returns the retained
`ExecutionFactProcessingOutcome`. A previously unseen or byte-conflicting ingress after the parent
acknowledgement receives no dispatch sequence and fails `validation.conflicting_id`; it cannot be
retroactively processed.

The adapter does not acknowledge the parent, mutate the matcher, or insert a root. The independent
root queue continues to require source registration for every fact ingress present in its sealed
plan; the matcher source is registered exactly once on the closed descendant registry and must be
absent from that root-plan registry. This is the only source-registration amendment.

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
next_fact_sequence_before
next_fact_sequence_after
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

`next_fact_sequence_before/after` is `int | None`, with the same meaning as existing authority
sequence pointers:

- initial next value is `1`;
- an integer is the next fact/ingress sequence available in `1..2**64-1`;
- issuing a value below the maximum advances it by one;
- issuing the maximum changes the pointer to `None`; and
- `None` is the only exhausted representation.

An empty batch has equal before/after pointers. A one-fact batch at initial state has `1/2`. A
two-fact batch has `n/n+2`. A final one-fact batch has `2**64-1/None`. A request for two facts when
the pointer is `2**64-1`, or any facts when it is `None`, fails atomically before provenance or
fact publication.

### Literal canonical encodings

All new public canonical values use the existing closed JSON encoder:

```python
json.dumps(
    document,
    ensure_ascii=True,
    allow_nan=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
```

Unknown, missing, extra, duplicate, non-exact, or non-canonical fields fail. UTC text uses
`YYYY-MM-DDTHH:MM:SS.ffffffZ`. Decimal text uses `ea-decimal-v1`. Economic IDs use the existing
literal object:

```json
{"owner_kind":"...","owner_sequence":1,"run_id":"..."}
```

Instruments use `{"symbol":"...","venue":"..."}`. An optional value is JSON `null`; an empty
tuple is `[]`. No absent-field convention exists.

Every new digest is:

```python
sha256(domain + len(payload).to_bytes(8, "big") + payload).hexdigest()
```

where `payload` is the exact canonical byte string and length is an unsigned big-endian uint64.
The receipt and batch domains named above use this formula. The remaining domains are:

```text
b"ea.phase1-historical-matcher-state.v1\0"
b"ea.phase1-historical-matcher-conflict.v1\0"
b"ea.phase1-historical-matcher-market-root.v1\0"
b"ea.phase1-historical-matcher-end-root.v1\0"
b"ea.phase1-historical-matcher-observation.v1\0"
```

The exact root digest inputs are:

- market: existing `canonical_market_data_record_bytes(root)` unchanged; and
- end: canonical bytes of:

  ```json
  {
    "available_at":"...",
    "kind":"bounded_source_exhausted",
    "producer_namespace":"...",
    "producer_sequence":1,
    "run_id":"...",
    "type":"end_of_run"
  }
  ```

The market matcher digest deliberately does not reuse the strategy-specific causal-market digest
domain. The active proof contains both its existing strategy causal digest and the exact market
bytes; the matcher derives and validates its own root digest from those bytes.

`trigger_root_key` and `causal_root_key` are one of exactly two tagged documents:

```json
{
  "adjustment":"raw",
  "available_at":"...",
  "domain_rank":30,
  "event_time":"...",
  "instrument":{"symbol":"...","venue":"..."},
  "interval_end":"...",
  "interval_start":"...",
  "kind_rank":0,
  "revision":0,
  "root_domain":"market_data",
  "source":"...",
  "source_sequence":1
}
```

```json
{
  "available_at":"...",
  "domain_rank":50,
  "kind":"bounded_source_exhausted",
  "kind_rank":0,
  "producer_namespace":"...",
  "producer_sequence":1,
  "root_domain":"end_of_run",
  "run_id":"..."
}
```

The numeric ranks must equal the current accepted runtime rank registries. Decoder reconstruction
must produce a `RuntimeRootOrderKey` whose `as_tuple()` equals the supplied root's key exactly.

The literal receipt document is:

```json
{
  "audit_acknowledgement_id":"...",
  "audit_acknowledgement_sha256":"<64 lowercase hex>",
  "authorization_state_version":1,
  "canonicalization":"ea-phase1-historical-submission-receipt-v1",
  "causal_market_sha256":"<64 lowercase hex>",
  "causal_root_key":{},
  "client_submission_key":"<64 lowercase hex>",
  "dispatch_sequence":1,
  "eligible_after_available_at":"...",
  "execution_policy_id":"...",
  "execution_policy_sha256":"<64 lowercase hex>",
  "execution_request_sha256":"<64 lowercase hex>",
  "global_halt_epoch":0,
  "instrument":{"symbol":"...","venue":"..."},
  "instrument_gate_id":"...",
  "instrument_gate_version":1,
  "instrument_spec_set_id":"...",
  "instrument_spec_set_sha256":"<64 lowercase hex>",
  "message_type":"historical_submission_receipt",
  "order_id":{"owner_kind":"execution_order","owner_sequence":1,"run_id":"..."},
  "order_sha256":"<64 lowercase hex>",
  "outcome_code":"submission.submitted",
  "quantity":"1",
  "risk_halt_epoch":0,
  "run_id":"...",
  "schema_version":1,
  "side":"buy",
  "source_namespace":"...",
  "submission_sequence":1
}
```

`causal_root_key` contains the complete market-key document, not `{}`. Halt epochs and versions are
exact non-negative uint64; submission/dispatch/gate versions are positive uint64.

The literal dispatch batch document is:

```json
{
  "canonicalization":"ea-phase1-historical-matcher-dispatch-batch-v1",
  "dispatch_kind":"market",
  "dispatch_sequence":2,
  "ingresses":[
    {
      "ingress_identity":{"ingress_sequence":1,"source_namespace":"..."},
      "ingress_sha256":"<64 lowercase hex>"
    }
  ],
  "message_type":"historical_matcher_dispatch_batch",
  "next_fact_sequence_after":2,
  "next_fact_sequence_before":1,
  "order_ids":[
    {"owner_kind":"execution_order","owner_sequence":1,"run_id":"..."}
  ],
  "run_id":"...",
  "schema_version":1,
  "source_namespace":"...",
  "submission_sequences":[1],
  "trigger_root_key":{},
  "trigger_root_sha256":"<64 lowercase hex>"
}
```

`dispatch_kind` is exactly `market` or `end_of_run`; its root-key tag must agree. The five ordered
arrays (`ingresses`, `order_ids`, `submission_sequences`, retained public ingress tuple, and
descendant bindings) have equal length and aligned indexes. For an empty batch all are empty and
the before/after pointers are equal.

The literal public state document is:

```json
{
  "canonicalization":"ea-phase1-historical-matcher-state-v1",
  "conflict_sha256":null,
  "dispatch_batch_sha256s":[],
  "end_batch_sha256":null,
  "ended":false,
  "execution_policy_id":"...",
  "execution_policy_sha256":"<64 lowercase hex>",
  "halted":false,
  "instrument_spec_set_id":"...",
  "instrument_spec_set_sha256":"<64 lowercase hex>",
  "issued_ingresses":[],
  "last_new_dispatch_sequence":null,
  "message_type":"historical_matcher_state",
  "next_fact_sequence":1,
  "next_submission_sequence":1,
  "pending_order_ids":[],
  "provenance_id":"...",
  "receipt_sha256s":[],
  "run_id":"...",
  "schema_version":1,
  "source_namespace":"..."
}
```

Each `issued_ingresses` item has the exact two-field identity/digest shape used by a batch.
Receipt and batch digest arrays are append order. `None` sequence exhaustion and nullable
last/end/conflict fields are encoded as JSON `null`.

The literal conflict document is:

```json
{
  "canonicalization":"ea-phase1-historical-matcher-conflict-v1",
  "conflict_kind":"...",
  "existing_sha256":null,
  "last_successful_dispatch_sequence":null,
  "message_type":"historical_matcher_conflict",
  "next_fact_sequence":1,
  "next_submission_sequence":1,
  "occupied_identity":null,
  "pending_count":0,
  "run_id":"...",
  "schema_version":1,
  "submitted_dispatch_sequence":null,
  "submitted_sha256":null,
  "trigger_root_sha256":null
}
```

`conflict_kind` is a closed enum:

```text
submission_identity
client_submission_key
dispatch_identity
non_monotone_dispatch
retained_binding_drift
```

`occupied_identity` is either `null` or one tagged object:

```json
{"kind":"order_id","order_id":{}}
{"kind":"client_submission_key","sha256":"<64 lowercase hex>"}
{"dispatch_sequence":1,"kind":"dispatch_sequence"}
```

The object payload must agree with the conflict kind. Optional digests/sequences are present as
`null` when they cannot be safely derived.

The exact matcher-observation preimage is:

```json
{
  "available_at":"...",
  "canonicalization":"ea-phase1-historical-matcher-observation-v1",
  "execution_policy_id":"...",
  "execution_policy_sha256":"<64 lowercase hex>",
  "expiry_outcome_code":null,
  "fact_kind":"trade",
  "fact_sequence":1,
  "instrument":{"symbol":"...","venue":"..."},
  "instrument_spec_set_id":"...",
  "instrument_spec_set_sha256":"<64 lowercase hex>",
  "occurred_at":"...",
  "order_sha256":"<64 lowercase hex>",
  "price":"10.5",
  "provenance_id":"...",
  "quantity":"1",
  "schema_version":1,
  "side":"buy",
  "source_namespace":"...",
  "submission_receipt_sha256":"<64 lowercase hex>",
  "trigger_dispatch_sequence":2,
  "trigger_root_key":{},
  "trigger_root_kind":"market",
  "trigger_root_sha256":"<64 lowercase hex>"
}
```

For trade, `price` is canonical text and `expiry_outcome_code` is `null`. For expiry, `price` is
`null`, `expiry_outcome_code` is `order.expired.no_eligible_market_data`,
`trigger_root_kind` is `end_of_run`, and occurred/available times are equal. No other combination
is valid.

The matcher never preserves a negative-zero distinction. Python binary64 `+0.0` and `-0.0` both
produce the exact rational `(0, 1)` from `as_integer_ratio()`, both select tick index zero on BUY
and SELL, and both canonicalize to price text `"0"`. `PriceDomain.POSITIVE` rejects that price;
`PriceDomain.NON_NEGATIVE` and `PriceDomain.SIGNED` accept the same byte-identical `"0"`. A minus
sign is never emitted for zero.

Public APIs are exact and named:

```python
canonical_historical_submission_receipt_bytes
historical_submission_receipt_digest
decode_historical_submission_receipt
canonical_historical_matcher_dispatch_batch_bytes
historical_matcher_dispatch_batch_digest
decode_historical_matcher_dispatch_batch
canonical_historical_matcher_state_bytes
historical_matcher_state_digest
decode_historical_matcher_state
canonical_historical_matcher_conflict_bytes
historical_matcher_conflict_digest
decode_historical_matcher_conflict
```

Each decoder requires an exact `HistoricalMatcherDecodeContext` bound to run, owned
specification/policy, source/provenance, and read-only canonical Order/receipt/batch/ingress
lookups as required by that value. The state decoder requires `provenance_id` to equal its context
binding. Each decoder reconstructs through factories, rejects unknown fields and context
substitution, recomputes every nested digest, and requires the re-encoded bytes to equal the
submitted bytes exactly.

The normative cross-process fixture is
[`../fixtures/adr0018-historical-matcher-v1.json`](../fixtures/adr0018-historical-matcher-v1.json).
It records real canonical spec-set, policy-source, target-lineage, OrderIntent, RiskDecision,
ExecutionApproval, Order, execution-request, audit-acknowledgement, and root context bytes/digests,
plus the complete canonical UTF-8 bytes and digest for one delayed-availability trade path and one
bounded-end expiry path. Both paths use the matcher's single bound `provenance_id`. The expiry
Order is submitted on dispatch 8 after that dispatch's matching stage, so it is not eligible for
the already-observed Bar and remains pending until bounded end dispatch 9. The existing fact
digest is over the fact document with `fact_sha256` absent; the existing ingress digest is over
its complete canonical document. All other new artifact digests use the domains and length
framing above.

The fixture is normative. Its context artifacts must decode through current frozen factories and
re-encode byte-for-byte; future receipt, observation, batch, fact, ingress, and matcher decoders
must do the same as they are implemented. Every supported process environment must reproduce all
literal bytes and digests.

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

The matcher minimally materializes the end root/sequence and resolves exact retained replay or
occupied-identity conflict before checking halt, end state, or a live port. Only a genuinely new
end dispatch validates the sealed active end proof, run ID, monotone dispatch, root bytes/key, and
complete sequence capacity before publication. It emits one expiry fact for every still-pending
Order in submission order:

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

An exact end retry returns the same retained batch with no sequence use and no active-end proof,
including after the parent dispatch was acknowledged.

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
- acknowledgement, rejection, and cancellation reject every explicit override;
- expiry accepts only the explicit override `order.expired.no_eligible_market_data`; and
- an explicit generic code or any other exact code/kind combination fails
  `validation.out_of_range` before fact construction.

The decoder validates the closed kind/code pair. For the existing generic code corresponding to
its kind it calls the factory with `outcome_code=None`; only
`order.expired.no_eligible_market_data` is passed as an explicit override. Every other pair fails
`validation.out_of_range`. Existing generic fact bytes and factory calls therefore remain
byte-identical, while the new expiry code round-trips canonically.

The closed compatibility mapping is:

| Kind | `outcome_code is None` | Allowed explicit override |
|---|---|---|
| acknowledgement | `order.acknowledged` | none |
| rejection | `order.rejected` | none |
| cancellation | `order.cancelled` | none |
| expiry | `order.expired` | `order.expired.no_eligible_market_data` only |

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
risk.stale_approval
durability.audit_append_failed
durability.audit_ack_mismatch
```

`submission.blocked_by_halt` is used only when an otherwise well-formed new submission reaches an
already halted matcher. Existing exact replay still succeeds. New match/end operations on halt
use `validation.conflicting_id`, because they do not represent a venue submission outcome.

The three authorization-proof codes are exposed only by a genuinely new, otherwise well-formed
submission after exact replay and structural validation: stale portfolio/risk state uses
`risk.stale_approval`; an absent persisted pre-effect audit acknowledgement uses
`durability.audit_append_failed`; and an acknowledgement that does not bind the exact request
uses `durability.audit_ack_mismatch`. They consume no submission sequence and publish no receipt.

Errors never use exception text for control flow.

### Later coordinator contract

For one active market root the future coordinator calls, in this order:

1. `match_active_market_root`;
2. for every returned ingress in batch order, use the causal-descendant adapter to verify matcher
   issuance and exact active-parent membership, then process the fact;
3. persist and verify inbound audit acknowledgement of the exact immutable
   `ExecutionFactProcessingOutcome` before dispatching its Fill or projection to another owner;
4. only after that acknowledgement, apply any Fill to the ledger and publish the newer immutable
   portfolio/risk view;
5. run strategy, portfolio, and risk;
6. create an Order from an authority-issued executable risk result;
7. persist and verify the exact pre-effect audit acknowledgement;
8. perform the immediate freshness/halt/instrument-gate authorization read and call `submit` while
   the same causal market dispatch remains active; and
9. acknowledge the runtime dispatch only after required stage outcomes are durable.

Inbound audit append/acknowledgement failure blocks ledger/portfolio/risk dispatch for that
outcome, records the failing-safety transition when durability is available, and blocks every new
submission. The already accepted fact, Fill, projection, and processing outcome remain retained
by the fact authority; they are not discarded or fabricated. Runtime continues the mandatory
inbound drain for later real facts under the failing state and retries/recovers the missing
audit-before-ledger stage only from authoritative retained evidence. The complete retry and
durability choreography remains coordinator scope, but no path may mutate ledger state before the
exact inbound outcome acknowledgement.

Matcher facts are causal descendants. They are not reinserted as independently competing roots
ahead of their causal market root.

At bounded source exhaustion the coordinator calls `expire_at_active_end`, processes every expiry
fact through the same descendant-verifier and inbound-audit-before-projection order, and then
completes result durability.

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
- property tests for the bounded permutation and prefix properties below;
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

The order of public `submit`, `match_active_market_root`, and `expire_at_active_end` calls is
normative and is not permutation invariant. The bounded permutation property applies only when
the receipt sequence and dispatched root sequence are held fixed: reordering instrument-spec
construction input, immutable lookup-map insertion, or presentation order of an internal pending
container cannot change canonical state, batches, facts, or ingresses. Pending economic emission
always follows retained submission sequence.

The no-look-ahead property is a prefix property. For any cutoff dispatch sequence `n`, appending
any valid market or end roots strictly after `n` cannot change any receipt, observation, batch,
fact, ingress, state digest, error, or sequence pointer produced through `n`. The matcher API
contains no source, iterator, cursor, callback, or `next` capability; its only market/end input is
the one exact active root supplied to the current call.

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
