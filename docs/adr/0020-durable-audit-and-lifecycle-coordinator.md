# ADR 0020: Durable Audit and Phase 1 Lifecycle Coordinator

- Status: Proposed
- Date: 2026-08-09
- Decision owners: Architecture, Runtime, Execution, Backtest, Durability
- Related: ADR 0003, ADR 0004, ADR 0006, ADR 0008, ADR 0009, ADR 0010,
  ADR 0014, ADR 0016, ADR 0018, ADR 0019, Issue #61

## Context

The repository now has deterministic market-data admission, one globally ordered historical
runtime, a simulated venue, causal-descendant fact verification, canonical fact processing,
portfolio/risk/Order authorities, a ledger, and strategy/portfolio planning. Accepted ADR 0008
and ADR 0018 already require two durability gates:

1. an inbound `ExecutionFactProcessingOutcome` must be durably acknowledged before its Fill or
   projection can reach another owner; and
2. an executable request must be durably acknowledged immediately before simulated submission.

They also require the runtime root to remain active until every required stage outcome is durable.
The current `BoundAuditPort` proves only that arbitrary bytes used the prepared run binding. It
returns no record identity, sequence, digest, chain position, or replay evidence. There is no
concrete audit adapter and no owner that calls the runtime, matcher, fact authority, and audit
boundary in the required order.

Leaving these details to an implementation would permit incompatible meanings of “persisted” and
“acknowledged”. A caller could reuse an acknowledgement for another subject, acknowledge a root
before all descendants are durable, silently discard a torn tail, or expose a Fill before the
exact processing outcome is recorded.

[Issue #61](https://github.com/jayjcc8-cloud/ea-quant/issues/61) owns this decision and its first
implementation. Later Issues extend the coordinator after the audited fact-outcome handoff to the
ledger/reconciliation, concrete strategy, deterministic result/report, and backtest CLI.

## Scope

This decision freezes:

- dependency-neutral audit records and acknowledgements;
- the POSIX v1 append-only journal format and durability choreography;
- exact retry, conflict, corruption, torn-tail, and reopen behavior;
- the bounded Phase 1 coordinator state machine through an audited fact-outcome handoff;
- active-market and bounded-end stage ordering;
- failing-safe drain and runtime acknowledgement rules;
- the pre-effect submission-authorization seam required by ADR 0018;
- recovery ownership and the evidence required from later extensions; and
- package and resource boundaries.

## Non-goals

This decision does not implement or change:

- ledger mutation or reconciliation correction;
- portfolio planning, risk evaluation, concrete strategy, or the complete
  `Signal -> Intent -> RiskDecision -> Order` loop;
- result/report durability, a backtest CLI, release, deployment, tag, or package publication;
- matcher economics, market-data ordering, risk policy, or ledger accounting;
- a broker, credential, paper/live path, or real external order write;
- Windows durability equivalence, network/object storage, multi-process writers, log compaction,
  deletion, or in-place repair of a committed frame; or
- a new dependency or lockfile change.

## Reuse decision

The bounded assessment is recorded in Issue #61. The selected design reuses `RunBinding`, the
store-owned `AuditCapability`, canonical SHA-256 values, historical runtime leases, matcher
batches, descendant verification, and `ExecutionFactProcessingOutcome`, plus Python 3.12 POSIX
filesystem primitives. It adds no dependency or transitive supply-chain surface.

SQLite is rejected for this bounded journal. It would add a second schema and record encoding,
journal-mode and migration policy, and operational inspection surface without removing the need
for canonical identities, explicit acknowledgements, read-back verification, corruption rules,
or replay proof. LEAN, NautilusTrader, and general event-sourcing frameworks remain architecture
and failure-scenario references; their clocks, persistence formats, order/account ownership, and
runtime closure do not fit this repository's accepted boundaries.

## Decision

### Ownership and dependency direction

Ownership is fixed:

```text
ea.core.audit                immutable records, acknowledgements, codecs, digests
ea.runtime.coordinator       serialized lifecycle and audit-gate policy
ea.experiments.audit         concrete local POSIX journal adapter
ea.composition               construction and binding of all concrete/inner owners
```

`ea.runtime` may import `ea.core` and consumer-owned protocols. It must not import
`ea.experiments`, configuration, composition, a filesystem adapter, or a vendor SDK.
`ea.experiments.audit` may import the core audit/run values and the store capability, but no
strategy, portfolio, risk, execution, matcher, or runtime policy. Composition is the only layer
that imports both the concrete journal and the runtime coordinator.

The audit authority owns durable audit identity and order. It does not decide whether a strategy,
risk decision, Order, Fill, or ledger mutation is correct. The coordinator owns stage order and
decides which exact canonical subject must be durable before a handoff or effect.

### Canonical audit vocabulary

`AuditRecordKind` is a closed version-one enum in canonical rank order:

```text
run.prepared
matcher.dispatch_batch
execution.fact_processing_outcome
submission.pre_effect_authorization
runtime.failing_safety_transition
runtime.dispatch_completed
run.terminal
```

The first record is always `run.prepared`. It binds the exact durable manifest and replaces the
current untyped `b"run.prepared"` convention. The remaining kinds are admitted only after that
record exists.

`AuditSubjectKind` is also closed:

```text
run_manifest
historical_matcher_dispatch_batch
execution_fact_processing_outcome
historical_execution_request
coordinator_state
runtime_dispatch
run_terminal_state
```

Each record kind maps to exactly one subject kind. Unknown values fail; human text never selects
control flow.

Every kind also owns one closed canonical payload schema:

- `run.prepared` carries the exact `RunBinding` and manifest-file digest;
- `matcher.dispatch_batch` carries dispatch kind/sequence, trigger-root key/digest, canonical
  batch digest, ingress count, and the domain-separated digest of the ordered ingress-digest
  tuple; it does not duplicate an arbitrarily large batch body;
- `execution.fact_processing_outcome` carries the complete canonical outcome bytes;
- `submission.pre_effect_authorization` carries the exact Order/request/root and current
  portfolio/risk/halt/instrument-gate/authorization-state bindings defined below;
- `runtime.failing_safety_transition` carries the prior/new coordinator-state digests, one closed
  failure code, failed logical record key, and active dispatch identity;
- `runtime.dispatch_completed` carries the root and batch digests, outcome count, the
  domain-separated digest of the ordered outcome-acknowledgement digest tuple, and resulting
  coordinator-state digest; and
- `run.terminal` carries the terminal coordinator-state digest, last dispatch identity, and audit
  chain head before the terminal record.

The batch and dispatch-completion schemas use count plus ordered aggregate digest so record size
does not grow with the number of pending Orders. The authoritative matcher/fact-owner histories
retain the complete canonical subjects and must reproduce the aggregates during recovery.

Audit identity reuses the existing `EconomicId` with
`owner_kind == EconomicOwnerKind.AUDIT_RECORD`. Its owner sequence equals the journal sequence.
The adapter also indexes the logical key `(record_kind, subject_kind, subject_sha256)`. That key
excludes journal sequence and payload so an exact logical retry finds the original assigned
`EconomicId` after a crash before the caller received its acknowledgement. The same logical key
plus different canonical payload is a conflict.

`AuditRecord` is factory-only, immutable, schema version one, and contains:

```text
schema_version
canonicalization = "ea-audit-record-v1"
run_id
lineage_sha256
manifest_sha256
record_id = (run_id, audit.record, owner_sequence)
record_kind
subject_kind
subject_sha256
payload_sha256
payload_hex
previous_record_sha256
previous_chain_head_sha256
```

The record ID owner sequence is in `1..2**64-1`, begins at one, and is contiguous. Sequence one
uses the domain-defined empty record digest and empty chain head. `payload_hex` is the lowercase,
even-length encoding of exact canonical payload bytes; the decoded payload is non-empty and at
most 786,432 bytes. The plain payload SHA-256 is checked before record construction.

Record bytes are sorted compact UTF-8 JSON with no insignificant whitespace. UTC time does not
appear in the envelope. The record digest domain is:

```text
b"ea.audit-record.v1\0"
```

The next chain head is SHA-256 over:

```text
b"ea.audit-chain.v1\0" + previous_chain_head_raw_32 + record_sha256_raw_32
```

Wall time, monotonic time, process ID, thread ID, filesystem inode, Python hash, object identity,
and exception text never enter canonical bytes or ordering.

### Canonical acknowledgement

`AuditAppendAcknowledgement` is a factory-only immutable value containing:

```text
schema_version
canonicalization = "ea-audit-append-acknowledgement-v1"
run_id
lineage_sha256
manifest_sha256
record_id = (run_id, audit.record, owner_sequence)
record_kind
subject_kind
subject_sha256
payload_sha256
record_sha256
chain_head_sha256
```

Its canonical digest domain is:

```text
b"ea.audit-append-acknowledgement.v1\0"
```

An acknowledgement is valid only when reconstructed from a frame read back from the journal and
all fields bind the requested record. A run-binding-only response, caller-constructed value,
wrong exact carrier type, different record ID/sequence/kind/subject/payload, or stale chain head
returns `durability.audit_ack_mismatch`.

The matcher-facing `audit_acknowledgement_id` is the exact canonical ASCII projection
`audit.record/<run_id>/<owner_sequence>`. Its
`audit_acknowledgement_sha256` is the canonical acknowledgement digest, not the record or payload
digest. Venue acknowledgement remains a distinct type.

### Consumer-owned audit port

The runtime-facing protocol is dependency-neutral:

```python
class AuditAppendPort(Protocol):
    @property
    def binding(self) -> RunBinding: ...

    def append(
        self,
        *,
        record_kind: AuditRecordKind,
        subject_kind: AuditSubjectKind,
        subject_sha256: Sha256Digest,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement: ...
```

The port assigns the next audit-record owner sequence only for a genuinely new logical record. It
returns the exact original acknowledgement for an exact retry. The first conflict is monotone and
blocks every later append. There is one writer and one serialized append call at a time;
concurrent or reentrant append is rejected before allocation.

The existing `RawAuditPort`/`BoundAuditPort` boundary evolves to return the typed acknowledgement
and verify the complete `RunBinding`. The store capability remains pathless outside
`ea.experiments`; no runtime caller receives an audit directory or file descriptor.

### POSIX v1 journal format

The concrete adapter owns one file at the fixed store-controlled name:

```text
<attempt>/audit/audit-v1.journal
```

It is created exactly once with `O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC`, mode `0600`, beneath
already verified no-follow directory descriptors. There is no caller-supplied path or filename.
The preamble is the exact ASCII bytes `EA-AUDIT-V1\n`.

Every frame is:

```text
8-byte unsigned big-endian payload length
canonical AuditRecord bytes
32 raw bytes: SHA-256(b"ea.audit-frame.v1\0" + length_bytes + record_bytes)
```

The length is in `1..1_048_576`. A complete frame with a bad checksum, non-canonical record,
invalid sequence, duplicate/conflicting ID, broken record link, broken chain head, wrong binding,
or bytes after an invalid frame is committed corruption. Reopen fails closed and does not append,
truncate, skip, resynchronize, or guess.

An EOF inside only the final header, final payload, or final checksum is an uncommitted torn tail.
Because no acknowledgement can precede a complete read-back and `fsync`, reopen may truncate
exactly that mechanically identified incomplete suffix to the last verified frame, `fsync` the
journal, and `fsync` the audit directory. It records no synthetic business event. Any incomplete
region followed by other bytes is not a tail and is corruption. An empty file, wrong/partial
preamble, or torn first frame before a durable `run.prepared` acknowledgement leaves the attempt
failed/incomplete and cannot admit runtime.

This narrow suffix recovery is the only automatic destructive operation. A complete but corrupt
frame is never repaired in place.

### Durable append choreography

For one genuinely new record the adapter:

1. validates exact carrier types, run binding, kind/subject mapping, payload size/digest, current
   state, and sequence capacity;
2. constructs the complete record, frame, next index, and acknowledgement in memory;
3. seeks to the verified EOF and writes the entire frame with forward-progress checks;
4. calls `fsync` on the journal;
5. reads the exact appended frame back through an independent positioned read;
6. re-decodes and verifies the frame, record, digest links, chain head, and acknowledgement;
7. publishes the in-memory index and returns the acknowledgement.

Failure before step 4 returns `durability.audit_append_failed` and publishes no in-memory record.
The next operation must rescan/recover the physical tail before proceeding. Failure or mismatch
after step 4 returns `durability.audit_ack_mismatch`, enters a monotone failed state, and never
authorizes an effect. Recovery may later discover a complete valid record and make an exact retry
return its acknowledgement; it may not assume success from the previous exception.

The initial file creation writes and `fsync`s the preamble and then `fsync`s the audit directory
before the first record. The journal file identity and mode are rebound before every append and
reopen. Replacement, symlink, hard-link count other than one, non-regular type, permission drift,
or directory identity drift fails closed.

### Reopen and bounded resources

Reopen scans from the preamble to EOF, verifies every frame, reconstructs the sequence/index/chain,
and returns no acknowledgement until scanning completes. It enforces:

- at most `2**64-1` records and contiguous sequences;
- maximum 786,432-byte payload and 1,048,576-byte canonical record;
- one `run.prepared` at sequence one and no second preparation record;
- at most one terminal record, after which only exact replay is allowed; and
- no duplicate logical record ID with different bytes.

For the Phase 1 historical profile let `N <= 1_000_000` be the admitted market-event count from
ADR 0015. ADR 0017 permits at most one new signal and therefore at most one new Order chain per
market dispatch. The coordinator admits at most `N` submission authorizations, at most `N`
matcher fact outcomes, and exactly `N + 1` matcher-batch/dispatch-completion pairs including the
bounded end. With preparation, at most one failing transition, and one terminal record, the
journal limit is exactly `4*N + 5`, no more than `4_000_005` frames. The maximum file size is
therefore the preamble length plus `4_000_005 * (8 + 1_048_576 + 32)` bytes. Construction or
reopen rejects an authority history that already exceeds the applicable count; later external
fact sources require a new bounded profile rather than silently increasing it.

The implementation may keep an in-memory record-ID to verified-offset index for the current
process. That index is a cache; reopen derives authority only from the journal. Iteration order of
a dictionary never selects canonical order.

### Coordinator bindings and public operations

`Phase1HistoricalLifecycleCoordinator` is factory-only and bound by identity to:

- one `RunBinding` and admitted `AuditAppendPort`;
- one `Phase1HistoricalMarketRuntime`;
- one `Phase1HistoricalMatcher`;
- one `CausalDescendantFactDispatchVerifier` already bound to those authorities;
- one `Phase1ExecutionFactAuthority`; and
- focused read-only resolver ports needed to reconstruct a Fill or projection named by an
  `ExecutionFactProcessingOutcome`.

Construction verifies exact run/spec/policy/source bindings and performs no pop, append, match,
fact processing, handoff, or acknowledgement. The authority exposes:

```python
coordinator.process_next_dispatch() -> CoordinatorDispatchOutcome
coordinator.retry_active_dispatch() -> CoordinatorDispatchOutcome
coordinator.prepare_submission_authorization(...) -> AuditAppendAcknowledgement
coordinator.verify_authorized_historical_submission(...) -> HistoricalSubmissionAuthorizationProof
```

`process_next_dispatch` requires no active lease, pops exactly one runtime root, and then executes
that active causal unit. `retry_active_dispatch` requires the exact retained active lease and
replays only missing stages. Neither operation loops over an unbounded run.

The coordinator itself satisfies `HistoricalSubmissionAuthorizationVerifier`. No other production
object can mint the opaque matcher proof.

### Coordinator state

`CoordinatorRunState` is closed and monotone:

```text
admitted -> running -> draining -> terminal
                    \-> failing -> draining -> terminal_failed
```

`failing` blocks new strategy, portfolio, risk, Order creation, and submission authorization. It
does not discard already admitted roots or real matcher facts. `terminal` and `terminal_failed`
never reopen.

The canonical coordinator state records the run binding, state version, current/last dispatch
sequence and root digest, matcher batch digest, ordered ingress/outcome/audit acknowledgement
digests, missing audit record IDs, monotone halt/failing reason code, and terminal audit chain
head. Its codec is strict and versioned. Same state version plus different bytes is a conflict.

`CoordinatorDispatchOutcome` is immutable and binds the active root, matcher batch, every ordered
audited handoff, dispatch-completion acknowledgement, runtime acknowledgement state, and the
resulting coordinator state digest. Exact retry returns canonical-equivalent evidence and makes no
second inner mutation.

### Active market dispatch order

For one active market root the coordinator performs exactly:

1. capture and validate the exact runtime lease and canonical root evidence;
2. call `match_active_market_root(root, dispatch_sequence=lease.dispatch_sequence)`;
3. append/verify `matcher.dispatch_batch` for the exact canonical batch;
4. for each returned ingress in batch order, call the bound descendant verifier through
   `Phase1ExecutionFactAuthority.process_ingress`;
5. append/verify `execution.fact_processing_outcome` for that exact immutable outcome;
6. construct an `AuditedExecutionFactHandoff` only from the outcome plus its exact
   acknowledgement and authority-resolved Fill/projection evidence;
7. after every required batch/outcome record is durable, append/verify
   `runtime.dispatch_completed` binding the root, batch, ordered acknowledgement tuple, and
   resulting coordinator state; and
8. immediately rebind the active lease and call `runtime.acknowledge(lease)`.

The fact authority may retain a real Fill before step 5. No Fill, projection, or outcome becomes
available through the coordinator handoff until step 5 succeeds. Later ledger integration may
consume only the sealed `AuditedExecutionFactHandoff`, never a raw Fill from the authority.

Matcher facts are causal descendants and are never inserted into the independently ordered root
queue. Strategy does not run in this first coordinator slice. A later extension may run strategy
only after every descendant and audited economic handoff for the active root has completed.

### Bounded end order

For an active `EndOfRunRoot`, step 2 calls
`expire_at_active_end(end_root, dispatch_sequence=lease.dispatch_sequence)`. Every expiry ingress
uses the identical descendant, fact-processing, audit-before-handoff, dispatch-completion, and
runtime-acknowledgement order. Only after all expiry outcomes are durable may the coordinator
append `run.terminal` and enter `terminal` or `terminal_failed`.

Terminal audit closes this coordinator slice but does not claim that result/report durability or
the complete reproducible-run terminal verifier exists.

### Inbound audit failure and mandatory drain

If one outcome append or acknowledgement fails:

- the fact authority's already accepted ingress, fact, Fill, projection, and processing outcome
  remain authoritative and are never rolled back or fabricated;
- no audited handoff exists for that outcome;
- no downstream ledger/portfolio/risk/strategy call is permitted;
- the coordinator enters monotone `failing`, attempts one
  `runtime.failing_safety_transition` append when the journal is available, and blocks every new
  submission authorization;
- every remaining ingress already returned in the same matcher batch is still verified and
  processed in order, and its audit is attempted, so real observations are drained; and
- the runtime lease is not acknowledged until every required missing audit record and the exact
  dispatch-completion record are durable.

After durability recovers, `retry_active_dispatch` replays the matcher batch and fact outcomes,
uses stable logical record IDs to fill only missing audit records, reconstructs the audited
handoffs, writes dispatch completion, and acknowledges the same lease. Failing state remains
monotone. Later roots may continue mandatory matcher/fact drain, but no new submission is allowed.

An audit failure never changes a fact outcome to `invalid`, removes a Fill, invents a ledger
transaction, or treats an unavailable acknowledgement as permission.

### Pre-effect submission authorization

The later strategy/portfolio/risk extension calls
`prepare_submission_authorization` only while the same causal market lease remains active. The
canonical payload binds:

- exact Order and execution-request bytes/digests;
- causal market bytes/digest/root key and dispatch sequence;
- portfolio snapshot and risk-state versions;
- global/risk halt epochs;
- held instrument gate identity/version; and
- coordinator authorization-state version.

The method appends `submission.pre_effect_authorization` and retains the resulting exact
acknowledgement keyed by Order ID and request digest. It performs no matcher call.

The matcher immediately calls `verify_authorized_historical_submission`. That operation re-reads
the active lease and all current halt, instrument, portfolio, risk, Order, request, causal-root,
and coordinator bindings. It returns the existing ADR 0018 opaque proof only when the retained
acknowledgement binds the exact request and every state remains fresh. Drift returns the existing
`risk.stale_approval`, `durability.audit_append_failed`, or
`durability.audit_ack_mismatch` outcome as applicable and consumes no matcher sequence.

An exact authorization retry may reuse an identical acknowledged record. Any changed economic or
state-version field produces a different subject/payload and requires a new Order flow; it cannot
reuse the prior acknowledgement.

### Recovery boundary

The journal is durable evidence, not a serialized Python heap. Recovery proceeds from immutable
manifest/data plus canonical authority histories:

1. reopen and fully verify the audit journal;
2. reconstruct the historical runtime and consumer-owned authorities from their canonical
   inputs/history at the last completed dispatch;
3. replay exact records in sequence, verifying every inner canonical result against its audit
   payload and digest;
4. leave the first incomplete dispatch active and replay only its missing deterministic stages;
5. resume new root admission only after all reconstructed bindings agree.

This Issue implements journal reopen and coordinator recovery against injected authoritative
runtime/matcher/fact-authority evidence. Later ledger/strategy/result Issues extend the same replay
without changing audit identity or weakening a gate. A process restart is not allowed to resume
effects if any required inner authority cannot yet be reconstructed and rebound.

### Error and conflict rules

Stable outcomes use the existing registry:

- `validation.invalid_type`, `validation.out_of_range`, and `validation.conflicting_id` for
  structural, range, replay, and identity conflicts;
- `durability.audit_append_failed` for a definite failure before a verified acknowledgement;
- `durability.audit_ack_mismatch` for read-back, binding, chain, or acknowledgement conflict; and
- existing fact/submission outcomes for domain behavior.

The first audit/coordinator conflict is retained and monotone. Exceptions may contain diagnostic
text, but control flow and canonical evidence use only closed codes and structured fields.

### Failure atomicity and callback isolation

Every public mutation precomputes canonical candidate bytes/digests before publication. A
reentrant coordinator call is rejected. Caller-owned mutable containers are never retained.

External port calls may fail or attempt callback reentry. The coordinator captures exact
pre-callback owned state, exposes only the declared port arguments, rebinds all live authorities
and the active lease after return/exception, and publishes from trusted pre-callback evidence.
Unknown exceptions propagate only after the coordinator has entered the appropriate safe state
when durability permits. A callback never receives coordinator-private state or journal indexes.

## Alternatives rejected

### Treat `fsync` return as the acknowledgement

Rejected because it does not bind the exact record read back from storage and cannot prove the
record ID, subject, payload, sequence, or chain head used by the effect.

### One JSON file per record

Rejected because a bounded Phase 1 replay may produce enough dispatch records for per-record
directory and inode cost to dominate. Framing preserves one ordered stream while retaining exact
torn-tail detection.

### Silently ignore or truncate any bad final frame

Rejected because a complete bad frame may be committed evidence. Only an objectively incomplete
EOF suffix that could not have produced an acknowledgement is recoverable automatically.

### Put the filesystem journal in runtime

Rejected because it reverses the accepted dependency direction and couples deterministic policy
to one adapter. Runtime consumes a port; experiments/composition own the concrete path.

### Acknowledge the runtime root before audit completion

Rejected because source cursor advancement would make a missing inbound outcome or matcher batch
unrecoverable and could expose later strategy decisions atop undurable economic evidence.

### Roll back a real Fill when audit fails

Rejected because a real accepted fact remains authoritative. Losing or fabricating economic
evidence to make durability appear clean is a reconciliation and live-safety failure.

## Required implementation evidence

### Canonical and golden evidence

- literal record, acknowledgement, frame, chain-head, coordinator-state, dispatch-outcome, and
  audited-handoff bytes/digests;
- first record, exact retry, conflicting retry, uint64 sequence seam, record-size seam, and
  terminal replay;
- cross-process equality under changed hash seed, timezone, locale, current directory, and
  ambient Decimal context; and
- unknown/missing/extra fields, non-canonical JSON/hex, wrong binding, and malformed carriers.

### Filesystem and failure injection

- no-follow creation, exclusive collision, modes, file/directory identity drift, symlink,
  hard-link, replacement, short write, zero-progress write, `fsync`, positioned read, close, and
  directory-`fsync` failures;
- crash/torn tail at every header/payload/checksum byte boundary;
- complete checksum, canonical-record, sequence, record-link, chain-head, ID, and payload
  corruption with no automatic repair;
- failure before and after journal `fsync`, lost acknowledgement, reopen, exact retry, and
  deterministic recovery; and
- two writers, reentrancy, callback exception, and callback state-drift rejection.

### Coordinator traces

- market with no matcher facts;
- later eligible trade -> fact outcome -> inbound audit -> sealed handoff -> dispatch completion
  -> runtime acknowledgement;
- bounded-end expiry through the identical order;
- multi-Order batch order and one accepted plus one duplicate/conflicting ingress;
- audit failure before the first outcome, between outcomes, at dispatch completion, and during
  failing-safety recording;
- real Fill retained while audit is unavailable, with zero ledger/strategy calls;
- exact active-dispatch retry and restart with no duplicate inner mutation;
- submission authorization success, stale portfolio/risk/halt/gate/root state, missing ack,
  mismatched ack, exact retry, and zero matcher sequence on failure; and
- terminal success/failure with no reopen.

### Properties and boundaries

- accepted audit sequence is contiguous and its chain is prefix deterministic;
- exact logical retries are idempotent and conflicting retries fail closed;
- no audited handoff exists without the exact outcome acknowledgement;
- no runtime acknowledgement exists before every required causal record is durable;
- independently supplied input container order cannot change matcher batch or descendant order;
- resource limits remain bounded by the Phase 1 source and closed stage vocabulary;
- AST imports enforce the declared dependency direction; and
- focused, official quality/full, coverage, reproducible-wheel, clean-install/doctor, and exact-head
  CI profiles pass on macOS and Linux before independent exact-SHA Verification.

## Consequences

- Phase 1 gains one explicit durability authority and one executable causal stage owner.
- An audit acknowledgement becomes effect-authorizing evidence rather than a run-binding-only
  callback response.
- Crash-before-ack recovery is deterministic without silently repairing committed corruption.
- Real facts remain retained during durability failure while downstream economic mutation remains
  blocked.
- The next ledger/reconciliation and strategy iterations can consume narrow audited handoffs
  without changing the journal identity or runtime acknowledgement rule.
- The journal is intentionally local POSIX v1; Windows and remote storage require separate
  durability decisions.
