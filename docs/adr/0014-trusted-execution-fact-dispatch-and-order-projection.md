# ADR 0014: Trusted Execution-Fact Dispatch and Order Projection

Date: 2026-07-28

## Status

Proposed

## Context

Accepted ADR 0008 defines one deterministic
`Order -> ExecutionFactIngress -> ExecutionFact -> Fill` path, independent ingress and stable-fact
identities, explicit fact outcomes, terminal Order projections, truthful late and unresolved
economic evidence, and a total runtime root order. Accepted ADR 0009 fixes the exact root-ordering
contract. Issues #35 through #47 implemented the dependency-neutral messages, identities,
ordering plan and queue, portfolio ledger, Risk authority, and Order authority.

Issue #49 closes the next stateful gap: a shared Execution/OMS authority must process each trusted,
currently dispatchable fact ingress exactly once, allocate canonical Fills, and maintain an
observation-derived Order projection. A canonically coherent low-level carrier is insufficient
proof. Existing factories are intentionally dependency-neutral, and any caller can construct a
valid-looking fact and include it in a bounded root plan. The current `DeterministicRootQueue`
also proves only plan order at construction: after two roots are popped, unordered historical
membership cannot prove which one is currently permitted to mutate Execution.

The first Issue #49 design package proposed a separate fact authority bound to the existing Order
authority and root queue through read-only structural ports. Architecture and Execution/Backtest
review approved that ownership direction but returned `HOLD`:

- `ARCH49-001`: the new durable schemas and authorities require an Accepted ADR;
- `ARCH49-002` / `BACKTEST49-001`: admission must be run-bound, source-issued, ordered, active,
  non-evicting, and fail closed on unreconstructed history;
- `ARCH49-003` / `EXEC49-002`: a real overfill must preserve its full Fill and known ancestry
  while normal projection quantity remains bounded;
- `ARCH49-004` / `EXEC49-001`: Order resolution and source issuance need exact authority proofs,
  not object identity or coherent bytes;
- `ARCH49-005` / `EXEC49-003/004/005`: the outcome/projection schemas, invalid boundary, conflict
  publication, and derived halt/reconciliation matrix must be closed.

This ADR records those contracts before executable implementation.

## Decision

This ADR extends ADR 0008 and ADR 0009 and narrowly clarifies one ADR 0008 property:

> cumulative **projection-applied** Fill quantity never exceeds Order quantity.

Observed economic Fill history is never truncated and may exceed Order quantity when the trusted
source reports an overfill. The distinction is normative.

This ADR also extends ADR 0010's future integration requirement. An applied Fill requires
reconciliation when either its ancestry is incomplete **or** its bound fact-processing
disposition requires reconciliation. Issue #49 creates the authoritative disposition but does
not change or call the ledger. A later runtime integration must carry that evidence into the
ledger before applying anomalous Fills; it must not infer safety solely from non-null ancestry.

### Singular owner with two replay domains

Execution/OMS remains one semantic owner with two independent mutable state components:

1. `Phase1OrderAuthority` alone consumes Risk approvals and creates canonical Orders.
2. `Phase1ExecutionFactAuthority` alone classifies trusted active fact ingresses, allocates Fill
   IDs, creates canonical Fills, and maintains observation-derived Order projection.

The split is intentional. Approval consumption and fact ingress have independent replay keys,
allocation counters, and recovery inputs. Combining them would increase coupling without adding
cross-domain atomicity. One `process_ingress` call publishes only the fact-authority aggregate.

The fact authority reads issued Orders through `OrderResolutionVerifier` and reads source-issued
active dispatch evidence through `RuntimeFactDispatchVerifier`. It never imports a peer
implementation or receives a mutable peer registry.

The permitted dependency directions remain:

```text
ea.execution -> ea.core
ea.runtime   -> ea.core
composition -> ea.execution + ea.runtime
```

`ea.execution` does not import `ea.runtime`; `ea.runtime` does not import `ea.execution`.
Consumer-owned structural protocols and trusted outer composition provide the bindings.

### Trusted source issuance

Canonical provenance fields are immutable source description, not proof that a trusted source
actually produced or received the fact. Runtime therefore owns a serialized, source-bound
issuance boundary before root planning.

One `Phase1ExecutionFactIngressAuthority` is bound at construction to:

- one exact `RunId`;
- one exact instrument-specification set ID and digest; and
- one exact `SourceNamespace`.

Its only state-changing operation registers one already-decoded exact
`ExecutionFactIngress`. Registration:

1. requires the exact source namespace;
2. materializes exact canonical ingress and nested fact bytes;
3. reconstructs both values under the frozen specification set;
4. classifies ingress identity as absent, exact replay, or conflict;
5. stores a non-evicting record of ingress identity, ingress bytes, and fact bytes; and
6. publishes once.

Exact registration replay returns the original issued ingress. The same ingress identity with
different canonical bytes returns `validation.conflicting_id` with no mutation. Malformed wire
never reaches this operation.

The authority structurally implements a read-only issuance verifier:

```text
ExecutionFactIssuanceVerifier:
    run_id
    spec_set
    source_namespace

    has_issued_ingress(
        *,
        ingress_identity,
        canonical_ingress_bytes,
        canonical_fact_bytes,
    ) -> bool
```

The query returns exact `True` only for exact membership in that authority's non-evicting registry.
It returns exact `False` for absence or any byte mismatch and performs no registration,
allocation, dispatch, callback, I/O, or other mutation.

Each production adapter or deterministic simulator receives only its own source-bound registration
capability. Trusted composition supplies the real source authority to the runtime queue. A fake
structural object or a different source authority can be used in tests but does not establish a
production claim. Python object identity is a frozen capability binding, not historical issuance
evidence; registry membership is the evidence.

### Run-bound single-active runtime dispatch

`DeterministicRootQueue` becomes factory-only and is bound to:

- one exact `RunId`;
- one exact instrument-specification set ID and digest;
- one exact sealed `BoundedRuntimeRootPlan`; and
- an immutable source-namespace map of exact `ExecutionFactIssuanceVerifier` capabilities needed
  by fact roots in that plan.

Construction copies and freezes the map, proves every verifier's run, specification-set, and
source binding, and rejects missing, extra, duplicate, or mismatched source capabilities. The
capabilities are private and non-replaceable.

The queue owns one immutable aggregate containing:

- current plan cursor;
- next positive dispatch sequence;
- zero or one active dispatch;
- non-evicting acknowledged fact-dispatch records.

`peek()` never creates issuance or dispatch evidence.

`pop()` is a begin-dispatch operation:

1. it fails while another root remains active;
2. it selects exactly the current sealed-plan root;
3. for a fact root, it materializes exact ingress/fact bytes and requires exact issuance
   membership from the verifier bound to that source;
4. it constructs the active dispatch record with the next positive global dispatch sequence;
5. it advances the cursor and sequence and publishes once; and
6. it returns the selected root.

An issuance mismatch, malformed verifier result, verifier exception, sequence exhaustion, copy,
freeze, or preflight failure leaves cursor, sequence, active state, and history unchanged.

The runtime calls `acknowledge(root)` only after the root and all of its serialized causal
descendants complete. Acknowledgement requires the exact active root capability, moves a fact
dispatch into non-evicting history, clears the active slot, and publishes once. No later `pop`
succeeds before that acknowledgement. A processing failure leaves the root active, preventing
another root from overtaking it; the runtime enters its failure path instead of acknowledging.

The queue structurally implements the fact authority's consumer-owned port:

```text
RuntimeFactDispatchVerifier:
    run_id
    spec_set

    resolve_active_issued_fact_dispatch(
        *,
        ingress_identity,
        canonical_ingress_bytes,
        canonical_fact_bytes,
    ) -> positive dispatch_sequence | None
```

The query returns the exact positive dispatch sequence only when all supplied values match the one
current active fact dispatch whose source issuance was proved before `pop` published it. It
returns exact `None` for an acknowledged, future, absent, mismatched, or non-fact root. It is
read-only. The caller cannot propose, copy, or select a dispatch sequence.

The dispatch sequence is recorded in every first processing outcome. It cannot choose root order;
the sealed ADR 0008/0009 key already did that.

Exact ingress replay in the fact authority returns its original immutable outcome before consulting
current dispatch state. A genuinely new ingress must prove the current active dispatch. This
preserves replay after acknowledgement without letting a later admitted root overtake the current
one.

The bounded Phase 1 profile makes no durable recovery claim. Registries do not evict during the
run. A future restart must reconstruct source issuance, root cursor, active/acknowledged dispatch,
fact outcomes, Fill allocation, projection, and halt-request state from authoritative durable
records before processing can resume. An unconsumed plan, coherent bytes, or a fresh queue is not
recovery evidence; missing history fails closed.

### Exact construction and port error rules

`create_phase1_execution_fact_authority` binds:

```text
create_phase1_execution_fact_authority(
    *,
    run_id,
    spec_set,
    order_verifier,
    dispatch_verifier,
) -> Phase1ExecutionFactAuthority
```

`OrderResolutionVerifier` is owned by `ea.execution` and structurally implemented by the real
`Phase1OrderAuthority`:

```text
OrderResolutionVerifier:
    run_id
    spec_set

    resolve_issued_order_by_id(order_id) -> Order | None
    resolve_issued_order_by_client_submission_key(client_submission_key) -> Order | None
```

Both operations return the exact canonical issued Order from the authority's non-evicting
registry or exact `None`. They do not create, register, consume, or mutate an Order. The Order
authority remains authoritative; any fact-authority indexes are derived caches only.

Construction requires both verifiers to expose the exact structural operations and proves exact
run ID, specification-set ID, and specification-set digest equality. A protocol property or
operation that is absent, non-callable, wrong exact type, or raises `AttributeError`/`TypeError`
maps to `ExecutionFactAuthorityError(validation.invalid_type)`. A well-typed unequal binding maps
to `validation.conflicting_id`. Any other unexpected exception propagates unchanged. Construction
publishes no partial authority.

At processing time:

- an Order resolver must return exact `Order` or exact `None`;
- a dispatch verifier must return exact positive `int` or exact `None`;
- an absent active-dispatch result maps to `validation.conflicting_id`;
- a returned Order is canonically reconstructed and must match the frozen run/specification set;
- protocol shape/type failure maps to `validation.invalid_type`; and
- any other verifier exception propagates before allocation or publication.

Both capabilities are private, exact construction bindings with no optional fallback,
caller-supplied boolean, mutable registry, post-construction replacement, or arbitrary
Order/Fact/Fill insertion.

### Dependency-neutral projection schema

`ea.core.execution_state` owns the immutable values shared by Execution, Runtime, audit, result,
and later recovery. It imports only dependency-neutral `ea.core` modules.

`OrderProjectionState` is the closed v1 enum:

```text
submitted
acknowledged
rejected
partially_filled
filled
expired
cancelled
definitely_not_submitted
```

`OrderProjectionSnapshot` is factory-only and contains exactly:

- run ID;
- canonical Order ID and Order digest;
- stable client submission key;
- exact positive projection version;
- closed projection state;
- `projection_applied_fill_quantity`;
- optional source-scoped venue order ID;
- the stable fact key and fact digest that caused the latest projection change.

Projection-applied quantity is non-negative, quantized, and no greater than Order quantity.
It is not observed economic quantity. Observed quantity is derived only from the complete accepted
Fill history for that resolved Order and is never rounded, capped, or rewritten.

Projection version starts at one on the first projection and increments by one only when a
projection field changes. Compatible additional evidence that causes no projection change creates
no new snapshot. Terminal projections never change or reopen.

The canonical document is versioned and the digest domain is:

```text
ea.execution-order-projection.v1\0
```

Strict contextual decoding requires the exact canonical issued Order and frozen specification set,
reconstructs the value through its factory, rejects unknown/additional/missing keys and enum
values, and requires byte-for-byte round trip.

### Closed processing disposition and outcome

`ExecutionFactDisposition` is a closed v1 enum:

```text
accepted_transition
accepted_compatible_evidence
stable_fact_duplicate
stable_fact_conflict
context_invalid
unknown_order
missing_ancestry
order_binding_conflict
insufficient_projection_evidence
confirmed_fill_without_trade
overfill
late_after_terminal
projection_transition_conflict
terminal_state_conflict
```

`ExecutionFactProcessingOutcome` is factory-only and contains exactly:

- run ID;
- positive runtime dispatch sequence;
- ingress identity, canonical ingress digest, stable fact key, and canonical fact digest;
- closed `ExecutionFactDisposition` and its derived `OutcomeCode`;
- the Order ID reported by the fact, when present;
- the Order ID resolved authoritatively, when present;
- the reported client submission key, when present;
- optional created Fill ID and Fill digest;
- optional projection-before digest and projection-after digest;
- exact `requires_reconciliation` and `halt_requested` booleans derived from the disposition.

The booleans are canonical audit conveniences, not independent caller inputs. Factories and readers
derive them from this exhaustive table and reject any mismatch:

| Disposition | Outcome code | Reconciliation | Halt request |
|---|---|---:|---:|
| `accepted_transition` | `fact.accepted` | false | false |
| `accepted_compatible_evidence` | `fact.accepted` | false | false |
| `stable_fact_duplicate` | `fact.duplicate` | false | false |
| `stable_fact_conflict` | `fact.conflict` | true | true |
| `context_invalid` | `fact.invalid` | true | true |
| `unknown_order` | `fact.unresolved` | true | true |
| `missing_ancestry` | `fact.unresolved` | true | true |
| `order_binding_conflict` | `fact.unresolved` | true | true |
| `insufficient_projection_evidence` | `fact.unresolved` | true | true |
| `confirmed_fill_without_trade` | `fact.unresolved` | true | true |
| `overfill` | `fact.unresolved` | true | true |
| `late_after_terminal` | `fact.unresolved` | true | true |
| `projection_transition_conflict` | `fact.unresolved` | true | true |
| `terminal_state_conflict` | `fact.unresolved` | true | true |

The canonical document is versioned and the digest domain is:

```text
ea.execution-fact-processing-outcome.v1\0
```

Strict readers require exact canonical ingress/fact context and, when present, exact Fill,
before-projection, after-projection, and issued-Order context. They reconstruct through factories,
validate the complete presence matrix and derived booleans, and require byte-for-byte round trip.

Fill fields are present if and only if that ingress created a Fill. Projection-before is present
if and only if a resolved Order had a projection before processing. Projection-after is present
if and only if a projection exists after processing. Equal before/after digests mean retained
terminal or compatible state, not a mutation. Duplicate and conflict outcomes never carry a
new Fill. Exact ingress replay returns the original outcome object and dispatch sequence.

The minimum presence matrix is:

| Disposition/fact | Fill | Projection effect |
|---|---|---|
| accepted coherent trade | required | create or advance |
| `missing_ancestry` coherent trade | required | may create or advance from resolved Order |
| `unknown_order` complete trade | required | none |
| `order_binding_conflict` complete current-run/spec trade | required | none |
| `overfill` | required | create/advance to bounded `filled`, or retain terminal |
| `late_after_terminal` trade | required | retain identical terminal digest |
| lifecycle/query fact | absent | matrix-defined create, advance, retain, or none |
| duplicate, conflict, or contextual invalid | absent | none |

A cross-run local economic ID or incompatible instrument-specification lineage cannot be
normalized into a current-run canonical Fill. Such an exact active fact is retained as
`context_invalid`, creates no Fill, and requests reconciliation/halt. This is not permission to
erase or rewrite its evidence.

### Malformed wire and authority-level invalidity

`Phase1ExecutionFactAuthority.process_ingress` accepts only an exact already-decoded
`ExecutionFactIngress`. Malformed JSON, duplicate keys, noncanonical bytes, absent stable dedup
identity, or an undecodable nested fact can lack a canonical fact key/digest and therefore cannot
be represented by the authority outcome schema.

Malformed wire is retained and classified at the future adapter/runtime receive boundary using
recoverable ingress identity, raw-byte digest, and a separate closed decode/quarantine result. It
is not in Issue #49 scope and cannot enter the root plan or create a Fill.

`fact.invalid` is reserved for a source-issued, active, canonically representable ingress whose
exact fact is contextually invalid for the bound run/specification set. It never fabricates
identity merely to fit an outcome.

### Order resolution and contradiction precedence

For a first stable fact, the fact authority collects every presented Order ID, client submission
key, and previously learned `(source_namespace, venue_order_id)` mapping.

Resolution follows this closed precedence:

1. Resolve each presented Order ID and client key independently through the bound Order authority.
2. Resolve a venue key only from the fact authority's own previously learned mapping.
3. If no presented key resolves, classify `unknown_order`.
4. If two presented keys resolve to different Orders, classify `order_binding_conflict`.
5. For one resolved Order, require exact run, specification-set ID/digest, client key, Order ID,
   instrument, trade side, correlation, and permitted causation equality for every field actually
   presented by the fact.
6. A well-typed presented value that contradicts the resolved Order or an occupied venue mapping
   is `order_binding_conflict`.
7. Missing identifiers remain absent. Resolution through one known key may drive projection but
   never writes a missing Order/correlation/causation ID into the Fact or Fill.
8. A venue mapping is learned only from a source-issued coherent fact that resolves exactly one
   Order. Conflict, invalid, unknown, or insufficient evidence never creates or changes it.

For a coherent resolved trade whose Fill has missing Order, correlation, or causation ancestry,
the full Fill is still created and projection may advance from the proved resolved Order, but the
outcome is `missing_ancestry` and requests halt/reconciliation.

For an economically complete trade that contradicts a resolved Order, the full Fill preserves
the identifiers and economics reported by the trusted fact. It is not relabelled, detached,
truncated, or applied to normal projection quantity. The outcome is `order_binding_conflict`.

Unknown lifecycle facts create no Fill, mapping, or projection. They are retained as
`unknown_order`.

### Projection transition matrix

Projection is observed venue/simulator state only. It never claims that local pre-effect audit
authorization or a local venue call occurred. A source-issued coherent acknowledgement,
rejection, expiry, cancellation, submission-query result, or trade can establish observed state
without fabricating independent audit/submission-authority state.

The first coherent evidence for a resolved Order maps as follows:

| Fact evidence | First projection |
|---|---|
| acknowledgement | `acknowledged`, applied quantity zero |
| rejection | `rejected`, applied quantity zero |
| expiry | `expired`, applied quantity zero |
| cancellation | `cancelled`, applied quantity zero |
| trade with observed total `< Order quantity` | `partially_filled`, applied observed total |
| trade with observed total `== Order quantity` | `filled`, applied Order quantity |
| trade with observed total `> Order quantity` | `filled`, applied Order quantity, `overfill` |
| query `confirmed_submitted` | `submitted`, applied quantity zero |
| query `confirmed_not_submitted` | `definitely_not_submitted`, applied quantity zero |
| query `confirmed_rejected` | `rejected`, applied quantity zero |
| query `confirmed_filled` | `filled`, applied Order quantity, `confirmed_fill_without_trade` |
| query `still_unknown` | no projection, `insufficient_projection_evidence` |

For an existing non-terminal projection, only these state changes are permitted:

```text
submitted
  -> acknowledged | rejected | partially_filled | filled | expired | cancelled

acknowledged
  -> partially_filled | filled | expired | cancelled

partially_filled
  -> partially_filled | filled | expired | cancelled
```

A repeated state or later compatible evidence that does not change a field is
`accepted_compatible_evidence`. Any other attempted transition from a non-terminal state is
`projection_transition_conflict` and leaves projection unchanged.

`filled`, `rejected`, `expired`, `cancelled`, and `definitely_not_submitted` are terminal.
A non-trade fact compatible with the same terminal meaning is retained without a new projection.
A contradictory lifecycle fact is `terminal_state_conflict`.

Every complete trade creates its truthful Fill before projection classification:

- a coherent trade after any terminal projection is `late_after_terminal`;
- the terminal projection digest remains unchanged;
- the Fill preserves full quantity and ancestry;
- reconciliation and halt are requested.

For a first or non-terminal overfill:

- one full canonical Fill is created;
- observed economic quantity is the exact sum of accepted coherent Fills and may exceed the Order;
- projection-applied quantity becomes exactly the Order quantity;
- projection becomes `filled` if it was not already terminal; and
- the outcome is `overfill`, requesting reconciliation and halt.

No projection or ledger path may cap the Fill, erase its known Order ID, fabricate missing
ancestry, or discard it.

### Stable fact identity, conflict, and publication

Ingress identity and stable fact identity remain independent.

- Exact ingress identity plus identical canonical ingress bytes returns the original processing
  outcome object without consulting current dispatch or consuming an ID.
- Occupied ingress identity plus different canonical ingress bytes returns
  `validation.conflicting_id` before publication.
- A distinct active ingress with an absent stable fact key is processed as the first observation.
- A distinct active ingress with the same stable fact key and identical canonical fact bytes
  publishes `stable_fact_duplicate` with no Fill or projection mutation.
- A distinct active ingress with the same stable fact key and different canonical fact bytes
  atomically publishes the conflicting ingress evidence, `stable_fact_conflict` outcome, and a
  monotone local halt request. It creates no second accepted fact, Fill, projection, venue
  mapping, or Fill ID.

Conflict publishes and returns its outcome. It does not publish and then raise. Runtime consumes
the closed halt request and enters its failure/halt path. “No second mutation” in ADR 0008 means
no second economic or Order-state mutation; recording the conflicting delivery, outcome, and
halt request is the required intentional transition.

### Processing precedence and aggregate ownership

The fact authority owns one immutable aggregate containing:

- next positive Fill sequence;
- monotone local `halt_requested`;
- non-evicting ingress and stable-fact indexes;
- Fill ID index;
- derived Order ID, client-key, and source-scoped venue-key indexes;
- per-Order projection and exact observed coherent Fill quantity;
- ingress, outcome, first-fact, Fill, and projection histories.

All maps are copied and defensively frozen. Private records retain exact canonical bytes and
digests. Public inspection returns immutable values and tuples only.

For one call, processing order is:

1. require exact ingress/fact carriers and materialize their canonical bytes/digests;
2. classify exact ingress replay or identity conflict;
3. require the bound queue's exact current source-issued dispatch proof;
4. validate exact source, fact digest, run, and specification context;
5. classify stable fact duplicate or conflict;
6. resolve every Order identity and contradiction through the bound Order verifier and derived
   venue mapping;
7. for a complete trade, allocate one `EXECUTION_FILL` ID and construct the full canonical Fill;
8. classify projection, late, overfill, missing-ancestry, or unresolved semantics;
9. construct exact outcome/projection values, copied registries and histories;
10. canonicalize and digest every candidate public value and preflight the complete candidate
    aggregate; and
11. publish with one `_state` assignment.

Exact ingress replay precedes current active dispatch. For new ingresses, dispatch proof precedes
fact deduplication and Fill allocation. Stable duplicates/conflicts consume no Fill ID. A complete
trade requiring a Fill fails atomically if the Fill sequence is exhausted.

The private halt request is not global Runtime or Risk halt authority. It is a monotone fact-owner
condition carried by the outcome. Runtime must consume it and block every new submission while
continuing the safe inbound drain required by ADR 0008.

### Failure atomicity

Every unexpected or pre-publication failure leaves the complete aggregate object identity and
every nested state identity/content unchanged, including:

- Fill counter and halt request;
- ingress, fact, Fill, projection, Order, client, and venue indexes;
- observed quantity;
- ingress, outcome, fact, Fill, and projection histories.

Failure injection covers independently:

- exact canonical materialization and reconstruction;
- every source-issuance, active-dispatch, and Order-resolution call and return type;
- every Order-binding matrix row;
- Fill and projection construction;
- every integer/quantity operation;
- every index copy and insertion;
- every canonical encoding and digest;
- every defensive map freeze;
- every tuple/history construction; and
- final aggregate preflight.

Published duplicate, conflict, invalid, unresolved, overfill, late, and terminal-conflict outcomes
are successful domain transitions, not failure-injection cases.

### Public boundary and non-goals

The public API contains no:

- arbitrary Order, Fact, Fill, projection, dispatch receipt, registry, or counter injection;
- reset, reopen, eviction, correction, or post-construction capability replacement;
- ledger application or portfolio/risk mutation;
- venue call, query orchestration, retry, audit acknowledgement, or submission authorization;
- runtime dispatch loop or global halt mutation;
- malformed-wire decoder/quarantine store;
- filesystem, network, SDK, credential, release, or deployment effect; or
- durable recovery claim.

Historical matching, source adapters, ledger/runtime integration, strategy, PortfolioTarget
planning, backtest orchestration, and result artifacts remain later Phase 1 iterations.

## Required Issue #49 evidence

Issue #49 must prove:

- exact source-issued active facts process; canonical but unissued, wrong-source, wrong-run,
  wrong-specification, future, acknowledged, or out-of-order facts fail before mutation;
- `peek` and plan membership do not create dispatch evidence;
- a second `pop` is impossible until exact acknowledgement of the active root;
- source issuance, queue dispatch, and fact-authority registries do not evict;
- exact ingress replay, distinct stable-fact redelivery, stable-key conflict, and ingress-identity
  conflict have the declared independent outcomes;
- conflict publishes one outcome/halt request and consumes no Fill ID;
- every declared projection transition and forbidden transition is covered;
- unknown trade, binding contradiction, missing ancestry, first overfill, overfill after filled,
  and late trade after every terminal state preserve full Fill economics and known identifiers;
- projection-applied quantity never exceeds Order quantity while observed Fill history remains
  exact;
- terminal projection digests never change or reopen;
- every outcome disposition derives exactly one outcome/reconciliation/halt tuple;
- malformed wire is rejected outside the fact authority, while contextual invalidity uses
  `fact.invalid`;
- authority construction and verifier error matrices are exhaustive;
- maximum owner/dispatch/projection sequences and Fill exhaustion are atomic;
- injected failure at every pre-publication operation preserves exact aggregate/nested identities;
- public APIs have no prohibited escape hatch;
- source-policy tests preserve the acyclic import graph;
- canonical outcome, projection, Fill, and dispatch evidence are identical across changed
  `PYTHONHASHSEED`, timezone, locale, current directory, ambient decimal context, and independent
  processes; and
- a fresh or unreconstructed source/queue/authority cannot authorize historical processing.

## Design-finding traceability

- `ARCH49-001`: closed by requiring this ADR to be Accepted before implementation.
- `ARCH49-002`: closed by exact run/specification construction binding, source-issued
  single-active dispatch, atomic `pop`, acknowledgement, non-eviction, and fail-closed recovery.
- `ARCH49-003` / `EXEC49-002`: closed by full truthful overfill Fill, separately bounded
  projection-applied quantity, unchanged terminal projection, and explicit ledger integration
  obligation.
- `ARCH49-004`: closed by the exact Order-resolution port, absence/contradiction precedence,
  canonical return validation, and derived-cache ownership.
- `ARCH49-005` / `EXEC49-003/004/005`: closed by the canonical schemas, derived truth table,
  malformed-wire boundary, conflict publish-and-return rule, and local halt-request ownership.
- `EXEC49-001`: closed by source-bound issuance registry membership incorporated into the active
  runtime dispatch proof.
- `BACKTEST49-001`: closed by one active root, no second pop before acknowledgement, positive
  dispatch sequence, and current-active proof for every new ingress.

## Consequences

- Canonical validity, trusted source issuance, runtime ordering, and current dispatch are distinct
  proofs.
- A low-level caller cannot mutate the fact authority merely by manufacturing coherent bytes or
  placing them in a plan.
- Order creation and fact processing remain independently replayable without peer imports or
  shared mutable registries.
- Real economic facts are retained in full even when ancestry, binding, terminal state, or
  quantity is anomalous.
- Normal Order projection remains bounded and never reopens.
- Every admitted ingress has one replay-stable canonical outcome, including duplicate, conflict,
  invalid, and unresolved cases.
- Runtime gains an explicit single-active dispatch lifecycle and must acknowledge every root
  after its causal unit completes.
- Later ledger integration must bind the fact disposition so a fully identified anomalous Fill is
  still marked for reconciliation.
- Durable recovery, malformed-wire quarantine, submission/audit authority, reconciliation
  correction, and external effects remain explicitly unimplemented.

## Rejected alternatives

### Treat canonical `FactProvenance` as issuance proof

Rejected because dependency-neutral source description can be constructed by an arbitrary caller.
It proves what bytes claim, not which trusted authority registered them.

### Treat inclusion in a sealed root plan as issuance proof

Rejected because the plan validates shape and total order, not trusted producer history.

### Keep only unordered popped-ingress membership

Rejected because after A and B are both popped, membership permits B to process before A. One
active dispatch prevents overtaking and binds accepted-first conflict semantics to root order.

### Pass a forgeable dispatch token into `process_ingress`

Rejected because a caller-supplied value can be copied or mismatched. The fact authority queries
the frozen real queue capability for the exact current active record.

### Merge Order and fact state into one aggregate

Rejected because the domains have independent replay identities and no operation needs to mutate
both. A larger aggregate adds coupling without useful atomicity.

### Discard, detach, delay, or truncate an overfill

Rejected because it erases or falsifies trusted economic evidence. The full Fill is retained and
applied once; only normal projection-applied quantity is capped.

### Let terminal projection reopen for a late trade

Rejected by ADR 0008. The Fill is retained, the projection digest stays terminal, and
reconciliation/halt is requested.

### Publish conflict and then raise

Rejected because it makes replay and result durability asymmetric. Conflict is a canonical
published terminal disposition returned to Runtime.

### Represent malformed wire with fabricated fact identity

Rejected because undecodable input may have no canonical fact key or digest. Decode/quarantine is
a separate receive-boundary result.

### Let the fact authority mutate the global halt

Rejected because Runtime/Risk own global safety state. The fact authority publishes a monotone
halt request while Runtime controls submission blocking and safe inbound drain.
