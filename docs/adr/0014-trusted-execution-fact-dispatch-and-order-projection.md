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

> cumulative **projected executed** quantity never exceeds Order quantity.

Observed economic Fill history is never truncated and may exceed Order quantity when the trusted
source reports an overfill. The distinction is normative.

This ADR also extends ADR 0010's future integration requirement. An applied Fill requires
reconciliation when either its ancestry is incomplete **or** its bound fact-processing
action/anomaly tuple requires reconciliation. Issue #49 creates the authoritative classification
but does not change or call the ledger. A later runtime integration must carry that evidence into
the ledger before applying anomalous Fills; it must not infer safety solely from non-null ancestry.

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
2. materializes the kind-independent canonical ingress and nested fact bytes and digests;
3. validates exact carrier types, ingress/fact source equality, and kind-independent canonical
   representability;
4. classifies ingress identity as absent, exact replay, or conflict;
5. stores a non-evicting record of ingress identity, ingress bytes, and fact bytes; and
6. publishes once.

Exact registration replay returns the original issued ingress. The same ingress identity with
different canonical bytes returns `validation.conflicting_id` with no mutation. Malformed wire
never reaches this operation.

Registration proves that the bound trusted receive capability issued those exact canonical bytes.
It deliberately does not accept caller-supplied semantic context, prove local run/specification or
Order correlation, or reconstruct a submission-query fact against a caller-supplied Order. Those
context-dependent checks belong to the fact authority and can produce a canonical `fact.invalid`
or `fact.unresolved` outcome after ordered dispatch.

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

The public source factory and operation are exactly:

```text
create_phase1_execution_fact_ingress_authority(
    *,
    run_id,
    spec_set,
    source_namespace,
) -> Phase1ExecutionFactIngressAuthority

Phase1ExecutionFactIngressAuthority.register_ingress(
    ingress,
) -> ExecutionFactIngress
```

Direct `Phase1ExecutionFactIngressAuthority()` construction raises `TypeError`.

The source-authority error matrix is:

| Operation/condition | Result |
|---|---|
| construction argument is not its exact declared runtime type | `RuntimeOrderingError(validation.invalid_type)` |
| exact ingress/fact carrier is structurally incomplete or canonicalization returns a type failure | `RuntimeOrderingError(validation.invalid_type)` |
| ingress/fact source conflicts with the authority source binding | `RuntimeOrderingError(validation.conflicting_id)` |
| exact ingress identity is absent | publish one issued record and return the exact ingress |
| exact ingress identity and canonical bytes replay | return the original issued ingress object; no publication |
| occupied ingress identity has different ingress or fact bytes | `RuntimeOrderingError(validation.conflicting_id)`; no publication |
| a canonical core operation returns another declared validation code | preserve that exact code in `RuntimeOrderingError`; no publication |
| any other unexpected exception | propagate unchanged; no publication |

All copy, insertion, freeze, digest, and final-preflight failures occur before the single state
assignment and leave registry object identity/content unchanged.

`DeterministicRootQueue` becomes factory-only and is bound to:

- one exact `RunId`;
- one exact instrument-specification set ID and digest;
- one exact sealed `BoundedRuntimeRootPlan`; and
- an exact tuple of `ExecutionFactIssuanceVerifier` capabilities for the source namespaces needed
  by fact roots in that plan.

The public queue factory is exactly:

```text
create_deterministic_root_queue(
    *,
    run_id,
    spec_set,
    plan,
    fact_issuance_verifiers,
) -> DeterministicRootQueue
```

`fact_issuance_verifiers` is an exact tuple containing one structural verifier for each distinct
fact-root `SourceNamespace` in the plan. Construction derives and freezes the private source map,
requires the tuple to be strictly ascending by canonical source-namespace text, proves every
verifier's exact run/specification/source binding, and rejects missing, extra, duplicate,
out-of-order, or mismatched source capabilities. The capabilities are private and non-replaceable.
Direct
`DeterministicRootQueue()` construction raises `TypeError`.

The queue owns one immutable aggregate containing:

- current plan cursor;
- next positive dispatch sequence;
- zero or one active dispatch;
- non-evicting acknowledged fact-dispatch records.

`peek()` never creates issuance or dispatch evidence.

`pop()` is a begin-dispatch operation and returns a factory-only ephemeral
`RuntimeDispatchLease`:

1. it fails while another root remains active;
2. it selects exactly the current sealed-plan root;
3. for a fact root, it materializes exact ingress/fact bytes and requires exact issuance
   membership from the verifier bound to that source;
4. it constructs the active dispatch record and lease with the next positive global dispatch
   sequence;
5. it advances the cursor and sequence and publishes once; and
6. it returns the lease, whose read-only `root` and `dispatch_sequence` properties expose the
   selected root and exact sequence.

Direct `RuntimeDispatchLease()` construction raises `TypeError`; its root and sequence properties
are read-only, and it exposes no acknowledgement or mutation method.

An issuance mismatch, malformed verifier result, verifier exception, sequence exhaustion, copy,
freeze, or preflight failure leaves cursor, sequence, active state, and history unchanged.

The runtime calls `acknowledge(lease)` only after `lease.root` and all of its serialized causal
descendants complete. Acknowledgement requires `type(lease) is RuntimeDispatchLease` and the exact
live lease object stored in the active state, moves a fact dispatch into non-evicting history,
clears the active slot, and publishes once. No later `pop` succeeds before that acknowledgement.
A processing failure leaves the lease active, preventing another root from overtaking it; the
runtime enters its failure path instead of acknowledging.

Object identity is intentionally used only for this non-serializable, in-process, one-use lease
capability. It is not issuance, canonical-value, replay, or recovery evidence. The active record
itself retains canonical fact bytes and the deterministic sequence. There is no API that
acknowledges by caller-supplied root bytes, root identity, or sequence.

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

### Exact queue error rules

The queue matrix is normative:

| Operation/condition | Result |
|---|---|
| queue construction argument is not its exact declared runtime type | `RuntimeOrderingError(validation.invalid_type)` |
| verifier collection is not an exact tuple, or an operation/binding is absent, non-callable, or wrong exact type | `RuntimeOrderingError(validation.invalid_type)` |
| verifier binding is well typed but run/specification/source differs, or the source tuple is missing/extra/duplicate/out of order | `RuntimeOrderingError(validation.conflicting_id)` |
| unexpected exception while reading a verifier construction binding | propagate unchanged; no queue exists |
| `peek()` or `pop()` on an exhausted queue | `RuntimeOrderingError(validation.out_of_range)` |
| `pop()` while a lease is active | `RuntimeOrderingError(validation.conflicting_id)`; no mutation |
| fact-source verifier returns exact `False` | `RuntimeOrderingError(validation.conflicting_id)`; no mutation |
| fact-source verifier returns anything other than exact `bool` | `RuntimeOrderingError(validation.invalid_type)`; no mutation |
| fact-source verifier raises `AttributeError`/`TypeError` for its operation contract | `RuntimeOrderingError(validation.invalid_type)`; no mutation |
| fact-source verifier raises another exception | propagate unchanged; no mutation |
| dispatch sequence is exhausted | `RuntimeOrderingError(validation.out_of_range)`; no mutation |
| `acknowledge` argument is not the exact lease type | `RuntimeOrderingError(validation.invalid_type)`; no mutation |
| `acknowledge` has no active lease, receives a different exact lease, or repeats an acknowledged lease | `RuntimeOrderingError(validation.conflicting_id)`; no mutation |
| successful `acknowledge` | publish acknowledged fact history when applicable, clear active lease, return `None` |

Canonicalization, registry lookup, copy, insertion, freeze, lease construction, history
construction, and final-preflight failures preserve cursor, sequence, active lease, and every
history/index identity and content.

### Exact fact-authority construction and port error rules

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
- `projected_executed_quantity`;
- optional source-scoped venue order ID;
- the stable fact key and fact digest that caused the latest projection change.

Projected executed quantity is non-negative, quantized, and no greater than Order quantity. It is
the bounded execution quantity asserted by the current observation-derived Order projection. A
trade-derived projection uses the bounded cumulative coherent Fill quantity. A
query-confirmed-filled projection may assert the full Order quantity without creating or
pretending to be a canonical Fill. Exact observed economic quantity is derived only from complete
accepted Fill history for that resolved Order and is never rounded, capped, or rewritten.

The per-Order coherent observed total includes a created Fill exactly when one Order is
unambiguously selected and `order_binding_conflict` is absent. It includes missing-ancestry,
overfill, and late-terminal Fills; it excludes unknown-Order and binding-conflicting Fills. The
global Fill history retains all of them.

Projection version starts at one on the first projection and increments by one only when a
projection field changes. Compatible additional evidence that causes no projection change creates
no new snapshot. Terminal projections never change or reopen.

The optional `venue_order` is learned from the first coherent selected-Order fact that presents
one. A later absent venue value retains it; the same scoped value is compatible; a different
scoped value for that Order adds `order_binding_conflict` and leaves projection/mappings
unchanged. V1 therefore never silently replaces or chooses among multiple venue identities.

The literal canonical JSON document is:

```text
{
  "canonicalization": "ea-order-projection-snapshot-v1",
  "client_submission_key": lowercase_sha256_text,
  "last_fact_key": {
    "dedup_identity": {
      "kind": "external_id" | "source_native_sequence",
      "value": canonical_ASCII_text | non_negative_integer
    },
    "source_namespace": canonical_ASCII_text
  },
  "last_fact_sha256": lowercase_sha256_text,
  "message_type": "order_projection_snapshot",
  "order_id": economic_id_document,
  "order_sha256": lowercase_sha256_text,
  "projected_executed_quantity": canonical_decimal_text,
  "projection_state": closed_OrderProjectionState_text,
  "projection_version": positive_integer,
  "run_id": canonical_run_id_text,
  "schema_version": 1,
  "venue_order": null | {
    "source_namespace": canonical_ASCII_text,
    "venue_order_id": canonical_visible_ASCII_text
  }
}
```

`economic_id_document` uses the existing canonical keys `owner_kind`, `owner_sequence`, and
`run_id`. Canonical JSON uses UTF-8, sorted keys, no insignificant whitespace, and the existing
strict integer/string rules. The document does not embed its own digest.

The exact public functions are:

```text
create_order_projection_snapshot(
    *,
    order,
    spec_set,
    projection_version,
    projection_state,
    projected_executed_quantity,
    venue_source_namespace,
    venue_order_id,
    last_fact_key,
    last_fact_sha256,
) -> OrderProjectionSnapshot

canonical_order_projection_snapshot_bytes(snapshot) -> bytes
order_projection_snapshot_digest(snapshot) -> Sha256Digest
decode_order_projection_snapshot(payload, *, order, spec_set) -> OrderProjectionSnapshot
```

The digest is `SHA-256(b"ea.execution-order-projection.v1\0" + canonical_bytes)`. Strict
contextual decoding requires the exact canonical issued Order and frozen specification set,
reconstructs through the factory, rejects unknown/additional/missing keys and enum values, and
requires byte-for-byte round trip.

### Closed processing action, anomalies, and outcome

One singular classification value cannot preserve simultaneous missing ancestry, overfill, and late
terminal evidence. The outcome therefore uses one closed primary action and one canonical ordered
anomaly tuple.

`ExecutionFactAction` is the closed v1 enum:

```text
accepted
duplicate
conflict
invalid
unresolved
```

`ExecutionFactAnomaly` is the closed v1 enum with frozen canonical rank:

```text
0  context_invalid
10 order_binding_conflict
20 unknown_order
30 missing_ancestry
40 insufficient_projection_evidence
50 confirmed_fill_without_trade
60 overfill
70 late_after_terminal
80 projection_transition_conflict
90 terminal_state_conflict
```

An anomaly tuple is unique and strictly ascending by this rank. Classification never depends on
discovery order, mapping iteration, or exception order.

The primary action and anomaly tuple obey:

- `accepted`: empty anomalies;
- `duplicate`: empty anomalies and stable-fact identical-byte short circuit;
- `conflict`: empty anomalies and stable-fact different-byte short circuit;
- `invalid`: exactly `(context_invalid,)`; and
- `unresolved`: one or more anomalies, excluding `context_invalid`.

All applicable unresolved anomalies are retained. For example, a complete trade may record
`(missing_ancestry, overfill, late_after_terminal)`; selecting the late-terminal primary behavior
does not erase the other two facts.

`ExecutionFactProcessingOutcome` is factory-only and contains exactly:

- run ID;
- positive runtime dispatch sequence;
- ingress identity, canonical ingress digest, stable fact key, and canonical fact digest;
- closed `ExecutionFactAction`, canonical anomaly tuple, and derived `OutcomeCode`;
- the Order ID reported by the fact, when present;
- the reported client submission key, when present;
- the reported source-scoped venue order ID, when present;
- a canonical tuple of per-key Order-resolution bindings;
- the one authoritatively selected resolved Order ID, when unambiguous;
- optional created Fill ID and Fill digest;
- optional projection-before digest and projection-after digest;
- exact `requires_reconciliation` and `halt_requested` booleans derived from the action.

The booleans are canonical audit conveniences, not independent caller inputs. Factories and readers
derive them from this exhaustive table and reject any mismatch:

| Action | Outcome code | Reconciliation | Halt request |
|---|---|---:|---:|
| `accepted` | `fact.accepted` | false | false |
| `duplicate` | `fact.duplicate` | false | false |
| `conflict` | `fact.conflict` | true | true |
| `invalid` | `fact.invalid` | true | true |
| `unresolved` | `fact.unresolved` | true | true |

Each presented correlation key has one immutable `OrderResolutionBinding`:

```text
{
  "key_kind": "order_id" | "client_submission_key" | "venue_order_id",
  "resolved_order_id": economic_id_document | null
}
```

Bindings include only presented keys and are canonically ordered by the literal key-kind order
above. Their presented values are the outcome's reported Order ID, client key, and scoped venue
object. `resolved_order_id` in the outcome is:

- null when no binding resolves;
- the one Order ID when every non-null binding resolves to that one Order, even if another
  presented key is unresolved and therefore contradictory; or
- null when non-null bindings resolve to more than one Order.

The full binding tuple therefore preserves a multi-Order conflict without choosing or hiding one
candidate.

The literal canonical JSON document is:

```text
{
  "action": closed_ExecutionFactAction_text,
  "anomalies": [closed_ExecutionFactAnomaly_text, ...],
  "canonicalization": "ea-execution-fact-processing-outcome-v1",
  "client_submission_key": lowercase_sha256_text | null,
  "fact_key": {
    "dedup_identity": {
      "kind": "external_id" | "source_native_sequence",
      "value": canonical_ASCII_text | non_negative_integer
    },
    "source_namespace": canonical_ASCII_text
  },
  "fact_sha256": lowercase_sha256_text,
  "fill": null | {
    "fill_id": economic_id_document,
    "fill_sha256": lowercase_sha256_text
  },
  "halt_requested": boolean,
  "ingress_identity": {
    "ingress_sequence": non_negative_integer,
    "source_namespace": canonical_ASCII_text
  },
  "ingress_sha256": lowercase_sha256_text,
  "message_type": "execution_fact_processing_outcome",
  "order_resolutions": [order_resolution_binding_document, ...],
  "outcome_code": closed_OutcomeCode_text,
  "projection_after_sha256": lowercase_sha256_text | null,
  "projection_before_sha256": lowercase_sha256_text | null,
  "reported_order_id": economic_id_document | null,
  "reported_venue_order": null | {
    "source_namespace": canonical_ASCII_text,
    "venue_order_id": canonical_visible_ASCII_text
  },
  "requires_reconciliation": boolean,
  "resolved_order_id": economic_id_document | null,
  "run_id": canonical_run_id_text,
  "runtime_dispatch_sequence": positive_integer,
  "schema_version": 1
}
```

The document does not embed its own digest. The exact public functions are:

```text
create_order_resolution_binding(
    *,
    key_kind,
    resolved_order,
) -> OrderResolutionBinding

create_execution_fact_processing_outcome(
    *,
    run_id,
    runtime_dispatch_sequence,
    ingress,
    action,
    anomalies,
    order_resolutions,
    resolved_order,
    fill,
    projection_before,
    projection_after,
) -> ExecutionFactProcessingOutcome

canonical_execution_fact_processing_outcome_bytes(outcome) -> bytes
execution_fact_processing_outcome_digest(outcome) -> Sha256Digest
decode_execution_fact_processing_outcome(
    payload,
    *,
    ingress,
    fill,
    projection_before,
    projection_after,
    resolved_orders,
) -> ExecutionFactProcessingOutcome
```

The digest is
`SHA-256(b"ea.execution-fact-processing-outcome.v1\0" + canonical_bytes)`.
`fill`, both projections, and `resolved_orders` are exact contextual values or exact `None`/tuple
as declared. `resolved_orders` contains each distinct non-null binding result exactly once,
strictly ascending by `(run_id, owner_kind, owner_sequence)`. The reader reconstructs through
factories, rejects unknown/additional/missing keys, invalid enum/rank/order combinations, and
requires byte-for-byte round trip.

Field presence is exhaustive:

| Condition | Resolution bindings / selected Order | Fill | Projection before / after |
|---|---|---|---|
| `duplicate`, `conflict`, or `invalid` action | empty / null | null | null / null |
| first context-valid fact, no presented correlation key | empty / null | trade rule | null / null |
| first context-valid fact with presented keys | exactly one binding per presented key / rule above | trade rule | selected-Order rule |

The trade rule requires a Fill if and only if this is a first stable, context-valid, economically
complete current-run/spec trade and the action is `accepted` or `unresolved`. This includes
unknown Order, unique-Order binding conflict, multi-Order conflict, missing ancestry, overfill,
and late-terminal trades. Lifecycle and query facts never carry a Fill. Duplicate, conflict, and
invalid actions never carry a Fill.

The selected-Order projection rule is:

- when selected Order is null, both projection digests are null;
- otherwise, `projection_before_sha256` is the pre-call projection digest or null when absent;
- `projection_after_sha256` is the post-call projection digest or null when absent;
- create is `before=null, after=present`;
- update is `before!=after`, both present;
- retain is `before==after`, both present;
- no projection before or after is `null/null`; and
- a binding, transition, terminal, or insufficient-evidence anomaly never changes projection
  unless the separate projection matrix explicitly permits the same fact to create or advance it.

Because duplicate, stable conflict, and contextual invalidity short-circuit before Order
resolution, their projection digests are always null even if the original fact's Order currently
has a projection. A compatible resolved fact with an existing projection records equal
before/after digests. `still_unknown` retains an existing projection with equal digests and uses
null/null only when none existed. A multi-Order resolution conflict has no selected Order and
therefore null/null projection digests. Exact ingress replay returns the original outcome object,
dispatch sequence, binding tuple, anomaly tuple, and presence pattern.

A cross-run local economic ID or incompatible instrument-specification lineage cannot be
normalized into a current-run canonical Fill. Such an exact active fact is retained as
action `invalid` with anomaly `(context_invalid,)`, creates no Fill, and requests
reconciliation/halt. This is not permission to erase or rewrite its evidence.

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
3. Record one `OrderResolutionBinding` for every presented key before choosing a candidate.
4. If no presented key resolves, add anomaly `unknown_order`.
5. If two presented keys resolve to different Orders, add anomaly `order_binding_conflict` and
   select no Order.
6. For one resolved Order, require exact run, specification-set ID/digest, client key, Order ID,
   instrument, trade side, correlation, and permitted causation equality for every field actually
   presented by the fact.
7. A well-typed presented value that contradicts the resolved Order or an occupied venue mapping
   adds anomaly `order_binding_conflict`.
8. Missing identifiers remain absent. Resolution through one known key may drive projection but
   never writes a missing Order/correlation/causation ID into the Fact or Fill.
9. A venue mapping is learned only from a source-issued coherent fact that resolves exactly one
   Order. Conflict, invalid, unknown, or insufficient evidence never creates or changes it.

For a coherent resolved trade whose Fill has missing Order, correlation, or causation ancestry,
the full Fill is still created and projection may advance from the proved resolved Order, but the
outcome adds anomaly `missing_ancestry` and requests halt/reconciliation.

For an economically complete trade that contradicts a resolved Order, the full Fill preserves
the identifiers and economics reported by the trusted fact. It is not relabelled, detached,
truncated, or applied to normal projection quantity. The outcome adds
`order_binding_conflict`.

Unknown lifecycle facts create no Fill, mapping, or projection. They are retained as
action `unresolved` with anomaly `unknown_order`.

### Exhaustive anomaly predicates and primary behavior

After duplicate/conflict short circuits, the authority evaluates these predicates in canonical
rank order. Every true compatible predicate is retained:

| Anomaly | Exact predicate |
|---|---|
| `context_invalid` | the kind-independent carrier/digest contract or bound run/specification context is invalid before Order resolution; query-to-Order field contradictions are instead `order_binding_conflict` |
| `order_binding_conflict` | non-null resolution bindings select different Orders, or a presented run/client/Order/instrument/side/correlation/causation/occupied-venue value contradicts the one selected Order |
| `unknown_order` | no presented Order ID, client key, or learned venue key resolves |
| `missing_ancestry` | a created Fill has null Order ID, correlation ID, or causation ID |
| `insufficient_projection_evidence` | a context-valid lifecycle/query observation cannot prove one declared target; v1 includes `submission.still_unknown` |
| `confirmed_fill_without_trade` | a query proves filled while coherent observed Fill total for the selected Order is below Order quantity |
| `overfill` | a coherent selected-Order trade makes exact observed Fill total exceed Order quantity |
| `late_after_terminal` | a complete coherent selected-Order trade arrives while its before-projection is terminal |
| `projection_transition_conflict` | context-valid evidence requests a target forbidden from the non-terminal before-state |
| `terminal_state_conflict` | a non-trade fact/query contradicts the terminal before-state |

`context_invalid` is exclusive and stops before Order resolution, Fill allocation, or projection
lookup. Stable duplicate/conflict actions also stop before those operations.

For a context-valid first fact:

1. collect every resolution binding;
2. create a Fill whenever the exhaustive trade-presence rule requires it, even if Order
   resolution is unknown or conflicting;
3. choose no projection when no unique Order is selected;
4. retain projection unchanged for binding conflict;
5. for a coherent selected Order, apply the projection matrix unless the before-state is
   terminal;
6. collect every anomaly produced by those operations; and
7. derive action `unresolved` when the tuple is non-empty, otherwise `accepted`.

Thus anomaly classification never suppresses truthful Fill construction. Projection behavior is
selected independently and deterministically: late terminal retains; non-terminal overfill may
advance to bounded `filled`; missing ancestry does not prevent a transition proved through another
trusted key; query-only filled may transition but remains anomalous until Fill detail catches up.

### Projection transition matrix

Projection is observed venue/simulator state only. It never claims that local pre-effect audit
authorization or a local venue call occurred. A source-issued coherent acknowledgement,
rejection, expiry, cancellation, submission-query result, or trade can establish observed state
without fabricating independent audit/submission-authority state.

The first coherent evidence for a resolved Order maps as follows:

| Fact evidence | First projection |
|---|---|
| acknowledgement | `acknowledged`, projected quantity zero |
| rejection | `rejected`, projected quantity zero |
| expiry | `expired`, projected quantity zero |
| cancellation | `cancelled`, projected quantity zero |
| trade with observed total `< Order quantity` | `partially_filled`, projected observed total |
| trade with observed total `== Order quantity` | `filled`, projected Order quantity |
| trade with observed total `> Order quantity` | `filled`, projected Order quantity; add `overfill` |
| query `confirmed_submitted` | `submitted`, projected quantity zero |
| query `confirmed_not_submitted` | `definitely_not_submitted`, projected quantity zero |
| query `confirmed_rejected` | `rejected`, projected quantity zero |
| query `confirmed_filled` | `filled`, projected Order quantity; add `confirmed_fill_without_trade` while coherent observed Fill total is below Order quantity |
| query `still_unknown` | retain existing projection or none; add `insufficient_projection_evidence` |

For an existing non-terminal projection, only these state changes are permitted:

```text
submitted
  -> acknowledged | rejected | partially_filled | filled | expired | cancelled

acknowledged
  -> partially_filled | filled | expired | cancelled

partially_filled
  -> partially_filled | filled | expired | cancelled
```

A repeated state or later compatible evidence that does not change a field uses action
`accepted` and equal before/after projection digests. Any other attempted transition from a
non-terminal state adds anomaly `projection_transition_conflict` and leaves projection unchanged.

`filled`, `rejected`, `expired`, `cancelled`, and `definitely_not_submitted` are terminal.
A non-trade fact compatible with the same terminal meaning is retained without a new projection.
A contradictory lifecycle fact adds anomaly `terminal_state_conflict`.

Query-confirmed-filled is exhaustive:

| Before projection | Projection behavior | Anomalies |
|---|---|---|
| absent, `submitted`, `acknowledged`, or `partially_filled` | create/update `filled` at projected Order quantity | add `confirmed_fill_without_trade` iff coherent observed Fill total `< Order quantity` |
| `filled` | retain byte-identical projection | add `confirmed_fill_without_trade` iff coherent observed Fill total `< Order quantity` |
| `rejected`, `expired`, `cancelled`, or `definitely_not_submitted` | retain byte-identical terminal projection | add `terminal_state_conflict`; also add `confirmed_fill_without_trade` iff observed total `< Order quantity` |

`still_unknown` always adds `insufficient_projection_evidence`; it creates no projection and
retains any existing projection byte-identically.

Every complete current-run/spec trade creates its truthful Fill before projection classification:

- a coherent trade after any terminal projection adds `late_after_terminal`;
- the terminal projection digest remains unchanged;
- the Fill preserves full quantity and ancestry;
- reconciliation and halt are requested.

For a first or non-terminal overfill:

- one full canonical Fill is created;
- observed economic quantity is the exact sum of accepted coherent Fills and may exceed the Order;
- projected executed quantity becomes exactly the Order quantity;
- projection becomes `filled` if it was not already terminal; and
- the outcome includes anomaly `overfill`, requesting reconciliation and halt.

If an overfill is also late after a terminal projection, both anomalies are present and the
projection remains byte-identical. Missing ancestry is likewise retained alongside either
anomaly.

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
  publishes action `duplicate` with no anomaly, Fill, resolution, or projection context.
- A distinct active ingress with the same stable fact key and different canonical fact bytes
  atomically publishes the conflicting ingress evidence, action `conflict`, and a monotone local
  halt request. It creates no anomaly, second accepted fact, Fill, resolution, projection, venue
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

1. require exact ingress/fact carriers and materialize their context-independent canonical
   bytes/digests;
2. classify exact ingress replay or identity conflict;
3. require the bound queue's exact current source-issued dispatch proof;
4. validate exact ingress/fact source equality and fact digest;
5. classify stable fact duplicate or conflict before new Order/projection context is read;
6. validate kind-independent carrier/payload shape plus bound run/specification context;
7. record every Order-resolution binding and contradiction through the bound Order verifier and
   derived
   venue mapping;
8. compare every presented submission-query field with the selected issued Order when one exists;
   unresolved query evidence remains retained without a fabricated decode context;
9. for a complete current-run/spec trade, allocate one `EXECUTION_FILL` ID and construct the full
   canonical Fill;
10. collect every applicable anomaly in frozen rank order and classify projection behavior;
11. derive the primary action, outcome code, reconciliation, and halt request;
12. construct exact outcome/projection values, copied registries and histories;
13. canonicalize and digest every candidate public value and preflight the complete candidate
    aggregate; and
14. publish with one `_state` assignment.

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

Published duplicate, conflict, invalid, and unresolved actions, including overlapping anomaly
tuples, are successful domain transitions, not failure-injection cases.

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
- a second `pop` is impossible until acknowledgement with the exact live dispatch lease;
- source/queue construction, registration, replay/conflict, verifier false/malformed/exception,
  exhaustion, wrong/repeated lease, and acknowledgement failures match every normative error row;
- source issuance, queue dispatch, and fact-authority registries do not evict;
- exact ingress replay, distinct stable-fact redelivery, stable-key conflict, and ingress-identity
  conflict have the declared independent outcomes;
- conflict publishes one outcome/halt request and consumes no Fill ID;
- every declared projection transition and forbidden transition is covered;
- unknown trade, binding contradiction, missing ancestry, first overfill, overfill after filled,
  and late trade after every terminal state preserve full Fill economics and known identifiers;
- projected executed quantity never exceeds Order quantity while observed Fill history remains
  exact;
- terminal projection digests never change or reopen;
- every action/anomaly tuple derives exactly one outcome/reconciliation/halt tuple;
- overlapping missing-ancestry, overfill, late-terminal, query-without-trade, and transition
  anomalies remain complete and canonically ordered;
- query-confirmed-filled is covered against absent, partial, filled-from-Fill, filled-from-query,
  rejected, expired, cancelled, and definitely-not-submitted projections, followed by later trade
  detail;
- `still_unknown` is covered with and without an existing projection;
- Order ID, client key, and venue mapping resolving to different Orders preserve every binding;
- every outcome field has one required/forbidden presence result, including multi-Order resolution
  conflict, stable duplicate/conflict, contextual invalidity, and retained projection;
- literal projection/outcome schemas and readers reject altered keys, nesting, ranks, enum values,
  derived booleans, contextual digests, and noncanonical bytes;
- malformed wire is rejected outside the fact authority, while contextual invalidity uses
  `fact.invalid`;
- authority construction and verifier error matrices are exhaustive;
- maximum owner/dispatch/projection sequences and Fill exhaustion are atomic;
- injected failure at every pre-publication operation preserves exact aggregate/nested identities;
- public APIs have no prohibited escape hatch;
- source-policy tests preserve the acyclic import graph;
- canonical outcome, projection, Fill, and dispatch evidence are identical across changed
  `PYTHONHASHSEED`, timezone, locale, current directory, ambient decimal context, and independent
  processes, including overlapping anomalies and multi-Order resolution; and
- a fresh or unreconstructed source/queue/authority cannot authorize historical processing.

## Design-finding traceability

- `ARCH49-001`: closed by requiring this ADR to be Accepted before implementation.
- `ARCH49-002`: closed by exact run/specification construction binding, source-issued
  single-active dispatch, atomic `pop`, acknowledgement, non-eviction, and fail-closed recovery.
- `ARCH49-003` / `EXEC49-002`: closed by full truthful overfill Fill, separately bounded
  projected executed quantity, unchanged terminal projection, and explicit ledger integration
  obligation.
- `ARCH49-004`: closed by the exact Order-resolution port, absence/contradiction precedence,
  canonical return validation, and derived-cache ownership.
- `ARCH49-005` / `EXEC49-003/004/005`: closed by the literal canonical schemas, primary-action and
  ordered-anomaly model, exhaustive presence rules, malformed-wire boundary, conflict
  publish-and-return rule, and local halt-request ownership.
- `EXEC49-001`: closed by source-bound issuance registry membership incorporated into the active
  runtime dispatch proof.
- `BACKTEST49-001`: closed by one active dispatch lease, no second pop before acknowledgement,
  positive dispatch sequence, and current-active proof for every new ingress.

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
- Later ledger integration must bind the fact action/anomaly tuple so a fully identified
  anomalous Fill is still marked for reconciliation.
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
applied once; only projected executed quantity is capped.

### Let terminal projection reopen for a late trade

Rejected by ADR 0008. The Fill is retained, the projection digest stays terminal, and
reconciliation/halt is requested.

### Publish conflict and then raise

Rejected because it makes replay and result durability asymmetric. Conflict is a canonical
published terminal action returned to Runtime.

### Represent malformed wire with fabricated fact identity

Rejected because undecodable input may have no canonical fact key or digest. Decode/quarantine is
a separate receive-boundary result.

### Let the fact authority mutate the global halt

Rejected because Runtime/Risk own global safety state. The fact authority publishes a monotone
halt request while Runtime controls submission blocking and safe inbound drain.
