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
ea.core.audit                 immutable records, acknowledgements, codecs, digests
ea.core.lifecycle             dependency-neutral lifecycle ports and values
ea.runtime.coordinator        serialized lifecycle and audit-gate policy over core ports
ea.runtime.authorization      dormant/active submission-authorization authority
ea.experiments.audit          concrete local POSIX journal adapter
ea.composition.lifecycle      sealed joint construction of concrete/inner owners
```

`ea.runtime` may import `ea.core` and consumer-owned protocols. It must not import
`ea.execution`, `ea.strategy`, `ea.portfolio`, `ea.risk`, `ea.experiments`, configuration,
composition, a filesystem adapter, or a vendor SDK. `ea.core.lifecycle` defines structural
`HistoricalMatcherPort`, `ExecutionFactAuthorityPort`, `ExecutionEvidenceResolverPort`,
`PortfolioFreshnessPort`, `RiskFreshnessPort`, `GlobalHaltFreshnessPort`,
`InstrumentGateFreshnessPort`, and
`RuntimeLifecyclePort`. Their arguments and results are focused immutable `ea.core` values.
Execution, portfolio, risk, and runtime authorities satisfy these protocols structurally or
through sealed composition-owned adapters; the coordinator never checks or imports their concrete
classes.

`ea.experiments.audit` may import the core audit/run values and the store capability, but no
strategy, portfolio, risk, execution, matcher, or runtime policy. Composition is the only layer
that imports concrete execution/runtime authorities, the journal, and the coordinator.

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

Every payload below is exact UTF-8, ASCII-safe, key-sorted compact JSON using
`"canonicalization":"ea-canonical-json-v1"`. IDs, instruments, runtime root keys, policies, and
UTC values reuse their existing accepted canonical JSON projections; they are never `repr` or
free-form strings. Unknown, missing, extra, duplicate, coerced, or non-canonical fields fail.

The seven schemas and subject digests are normative:

| Record kind | Exact payload fields in addition to `schema` and `canonicalization` | Subject digest |
|---|---|---|
| `run.prepared` | `run_id`, `lineage_sha256`, `manifest_sha256` | the exact plain persisted `manifest_sha256` |
| `matcher.dispatch_batch` | `run_id`, `dispatch_kind`, `dispatch_sequence`, `trigger_root_key`, `trigger_root_sha256`, `batch_sha256`, `ingress_count`, `ordered_ingress_sha256s_sha256` | existing `historical_matcher_dispatch_batch_digest(batch)` |
| `execution.fact_processing_outcome` | the existing complete canonical `ExecutionFactProcessingOutcome` object, unchanged | existing `execution_fact_processing_outcome_digest(outcome)` |
| `submission.pre_effect_authorization` | `run_id`, `order_id`, `order_sha256`, `execution_request_sha256`, `causal_market_sha256`, `causal_root_key`, `dispatch_sequence`, `portfolio_snapshot_version`, `risk_state_version`, `global_halt_epoch`, `risk_halt_epoch`, `held_for_order_id`, `instrument_gate_id`, `instrument_gate_version`, `authorization_state_version`, `instrument_spec_set_id`, `instrument_spec_set_sha256`, `execution_policy_id`, `execution_policy_sha256` | SHA-256 of `b"ea.audit-subject.submission-authorization.v1\0" + payload_length_u64 + payload` |
| `runtime.failing_safety_transition` | `run_id`, `previous_state_sha256`, `failing_state_sha256`, `failure_code`, `failed_record_kind`, `failed_subject_kind`, `failed_subject_sha256`, `dispatch_sequence`, `trigger_root_sha256` | SHA-256 of `b"ea.audit-subject.failing-safety.v1\0" + payload_length_u64 + payload` |
| `runtime.dispatch_completed` | `run_id`, `dispatch_kind`, `dispatch_sequence`, `trigger_root_key`, `trigger_root_sha256`, `batch_sha256`, `outcome_count`, `ordered_outcome_ack_sha256s_sha256`, `pre_ack_state_sha256` | SHA-256 of `b"ea.audit-subject.dispatch-completed.v1\0" + payload_length_u64 + payload` |
| `run.terminal` | `run_id`, `terminal_kind`, `last_dispatch_sequence`, `last_trigger_root_sha256`, `pre_terminal_state_sha256`, `previous_chain_head_sha256` | SHA-256 of `b"ea.audit-subject.run-terminal.v1\0" + payload_length_u64 + payload` |

Their schema literals are respectively `ea.audit-run-prepared.v1`,
`ea.audit-matcher-dispatch-batch.v1`, the already accepted execution-outcome schema,
`ea.audit-submission-authorization.v1`, `ea.audit-failing-safety.v1`,
`ea.audit-dispatch-completed.v1`, and `ea.audit-run-terminal.v1`. `terminal_kind` is exactly
`success|failed`.

An ordered digest tuple aggregate is exactly:

```text
SHA-256(domain || count_u64 || digest_1_raw_32 || ... || digest_count_raw_32)
```

The domains are `b"ea.audit-ordered-ingress-digests.v1\0"` and
`b"ea.audit-ordered-outcome-ack-digests.v1\0"`. Count zero is allowed and hashes the domain plus
eight zero bytes, producing respectively
`2438409ca5926710ca25fea083cf1a53c582ed679a2bdb84fdfa3ce62dab8027` and
`6a2fb6988624cc65253db3cf79f653c16a6f2ad11aa7a1a1ca785f82bc8995a1`.
Count must equal the authoritative tuple length. Batch and completion payloads use only count plus
this aggregate, so record size does not grow with pending Orders; recovery must reproduce the
complete tuple from matcher/fact-owner histories.

For schema-wire evidence, the exact empty-batch audit payload projection is:

```json
{"batch_sha256":"3333333333333333333333333333333333333333333333333333333333333333","canonicalization":"ea-canonical-json-v1","dispatch_kind":"market","dispatch_sequence":1,"ingress_count":0,"ordered_ingress_sha256s_sha256":"2438409ca5926710ca25fea083cf1a53c582ed679a2bdb84fdfa3ce62dab8027","run_id":"123e4567-e89b-42d3-a456-426614174000","schema":"ea.audit-matcher-dispatch-batch.v1","trigger_root_key":{"adjustment":"raw","available_at":"2026-01-02T09:31:00.000000Z","domain_rank":30,"event_time":"2026-01-02T09:31:00.000000Z","instrument":{"symbol":"AAPL","venue":"XNAS"},"interval_end":"2026-01-02T09:31:00.000000Z","interval_start":"2026-01-02T09:30:00.000000Z","kind_rank":0,"revision":0,"root_domain":"market_data","source":"primary.raw","source_sequence":0},"trigger_root_sha256":"4444444444444444444444444444444444444444444444444444444444444444"}
```

It is 854 bytes with plain payload SHA-256
`3844a34eae4ec7ea554295efac053e296c867e2e0f94fb2aba7fbb414926e670`. The `batch_sha256`
field remains subject to independent reconstruction from a real canonical matcher batch.

The exact pre-terminal payload projection is:

```json
{"canonicalization":"ea-canonical-json-v1","last_dispatch_sequence":2,"last_trigger_root_sha256":"3333333333333333333333333333333333333333333333333333333333333333","pre_terminal_state_sha256":"4444444444444444444444444444444444444444444444444444444444444444","previous_chain_head_sha256":"5555555555555555555555555555555555555555555555555555555555555555","run_id":"123e4567-e89b-42d3-a456-426614174000","schema":"ea.audit-run-terminal.v1","terminal_kind":"success"}
```

It is 465 bytes and its domain-separated terminal subject digest is
`b1f57635f5e910d7c64f2caee133efe8c388ff5f357a2e1325b51f0f10b23514`.

Audit identity reuses the existing `EconomicId` with
`owner_kind == EconomicOwnerKind.AUDIT_RECORD`. Its owner sequence equals the journal sequence.
The adapter also indexes the logical key `(record_kind, subject_kind, subject_sha256)`. That key
excludes journal sequence and payload so an exact logical retry finds the original assigned
`EconomicId` after a crash before the caller received its acknowledgement. The same logical key
plus different canonical payload is a conflict.

`AuditRecord` is factory-only and immutable. Its canonical header is the exact JSON object:

```text
schema = "ea.audit-record-header.v1"
canonicalization = "ea-canonical-json-v1"
run_id
lineage_sha256
manifest_sha256
record_id = (run_id, audit.record, owner_sequence)
record_kind
subject_kind
subject_sha256
payload_sha256
previous_record_sha256
previous_chain_head_sha256
```

The record ID owner sequence is in `1..2**64-1`, begins at one, and is contiguous. Sequence one
uses these literal constants:

```text
empty_record_sha256 = SHA-256(b"ea.audit-empty-record.v1\0")
                    = 4af7f9585d80e83ffefb82fd9993e42a0e5bcca05a957c2d66f7e6ec5af602cd
empty_chain_head_sha256 = SHA-256(b"ea.audit-empty-chain.v1\0")
                        = daf430a5dce8e5d21acb79e2c4aa92b0da9f42108d96847e3da260cdbf271b75
```

The raw canonical payload is stored separately from the header. Its plain SHA-256 must equal the
header field. The canonical audit-record byte sequence is exact binary framing, not JSON hex:

```text
header_length_u64 || canonical_header_json || payload_length_u64 || canonical_payload
```

Header length is in `1..4_096`. Payload length is constrained by kind: `run.prepared`, batch,
failing-safety, completion, and terminal are at most 4,096 bytes; fact outcome and submission
authorization are at most 16,384 bytes. The record digest is:

```text
SHA-256(b"ea.audit-record.v1\0" || canonical_audit_record_bytes)
```

The next chain head is SHA-256 over:

```text
b"ea.audit-chain.v1\0" + previous_chain_head_raw_32 + record_sha256_raw_32
```

Wall time, monotonic time, process ID, thread ID, filesystem inode, Python hash, object identity,
and exception text never enter canonical bytes or ordering.

One normative sequence-one vector uses run ID `123e4567-e89b-42d3-a456-426614174000`, lineage
`11` repeated 32 bytes, and manifest digest `22` repeated 32 bytes. Its exact payload is:

```json
{"canonicalization":"ea-canonical-json-v1","lineage_sha256":"1111111111111111111111111111111111111111111111111111111111111111","manifest_sha256":"2222222222222222222222222222222222222222222222222222222222222222","run_id":"123e4567-e89b-42d3-a456-426614174000","schema":"ea.audit-run-prepared.v1"}
```

Payload length is `296` and payload SHA-256 is
`6186ae20684529abcb8b9008e94548235cf9979f76ec72f1aab50455c2db44a1`. Its exact header is:

```json
{"canonicalization":"ea-canonical-json-v1","lineage_sha256":"1111111111111111111111111111111111111111111111111111111111111111","manifest_sha256":"2222222222222222222222222222222222222222222222222222222222222222","payload_sha256":"6186ae20684529abcb8b9008e94548235cf9979f76ec72f1aab50455c2db44a1","previous_chain_head_sha256":"daf430a5dce8e5d21acb79e2c4aa92b0da9f42108d96847e3da260cdbf271b75","previous_record_sha256":"4af7f9585d80e83ffefb82fd9993e42a0e5bcca05a957c2d66f7e6ec5af602cd","record_id":{"owner_kind":"audit.record","owner_sequence":1,"run_id":"123e4567-e89b-42d3-a456-426614174000"},"record_kind":"run.prepared","run_id":"123e4567-e89b-42d3-a456-426614174000","schema":"ea.audit-record-header.v1","subject_kind":"run_manifest","subject_sha256":"2222222222222222222222222222222222222222222222222222222222222222"}
```

Header length is `821`; record digest is
`d8a3dce4093e148449b1c07b66b99c640732adb49446135be12645b64875b1b8`; resulting chain head is
`3fca3a84a1cf211e48b674197476396fc4be8184551c9a5f6fbf6f4c4200aa76`; and frame checksum is
`578e6e89482f060c57521d2b1c22379004c7d8125d3d38480bc7fac6de09fe7b`.

### Canonical acknowledgement

`AuditAppendAcknowledgement` is a factory-only immutable value whose exact canonical JSON fields
are:

```text
schema = "ea.audit-append-acknowledgement.v1"
canonicalization = "ea-canonical-json-v1"
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

Its digest is:

```text
SHA-256(b"ea.audit-append-acknowledgement.v1\0" + canonical_acknowledgement_json)
```

For the sequence-one vector, the exact acknowledgement JSON is:

```json
{"canonicalization":"ea-canonical-json-v1","chain_head_sha256":"3fca3a84a1cf211e48b674197476396fc4be8184551c9a5f6fbf6f4c4200aa76","lineage_sha256":"1111111111111111111111111111111111111111111111111111111111111111","manifest_sha256":"2222222222222222222222222222222222222222222222222222222222222222","payload_sha256":"6186ae20684529abcb8b9008e94548235cf9979f76ec72f1aab50455c2db44a1","record_id":{"owner_kind":"audit.record","owner_sequence":1,"run_id":"123e4567-e89b-42d3-a456-426614174000"},"record_kind":"run.prepared","record_sha256":"d8a3dce4093e148449b1c07b66b99c640732adb49446135be12645b64875b1b8","run_id":"123e4567-e89b-42d3-a456-426614174000","schema":"ea.audit-append-acknowledgement.v1","subject_kind":"run_manifest","subject_sha256":"2222222222222222222222222222222222222222222222222222222222222222"}
```

Its acknowledgement digest is
`d9c5b61de36ae0a60c57d8975239470af0c99d211d9fcd736b1decbbd35dbc4d`.

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

Fresh construction remains possible only from the exact `AuditRunBinding` issued by
`LocalResultStore.prepare`. The adapter factory receives that opaque binding, not a path. It
resolves the fixed audit child through the originating store registry and transfers the already
held writer lease described below.

ADR 0006's prohibition on adopting or retrying an existing reservation remains the default, but
this ADR narrowly supersedes it for recovery of the same incomplete attempt. A new outer
`verify_incomplete_run_recovery` operation must consume a tracked-launcher preflight, no-follow
open the configured result root and exact run-ID child, verify manifest bytes/digest and current
code/configuration/data/runtime lineage, prove that no terminal audit record exists, and issue a
factory-only `VerifiedRecoveryBinding`. Then and only then:

```python
LocalResultStore.recover_incomplete_attempt(
    verified: VerifiedRecoveryBinding,
) -> RecoveredRun
```

reissues new process-local audit/output/manifest capabilities for the same `RunBinding`. It does
not generate a run ID, reserve a directory, rewrite the manifest, adopt a different lineage, or
permit recovery after terminal completion.

### POSIX v1 journal format

The concrete adapter owns one file at the fixed store-controlled name:

```text
<attempt>/audit/audit-v1.journal
```

During initial attempt reservation the store exclusively creates
`<attempt>/audit/writer-v1.lock` with `O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC`, mode `0600`, and takes
one non-blocking POSIX exclusive `flock` before the prepared attempt can escape. The store retains
that descriptor across manifest publication and transfers its lease to the audit adapter. A
recovery process no-follow opens the verified existing lock file and must acquire the same lock
before capabilities are reissued. Failure to acquire it returns `validation.conflicting_id`; it
never waits, steals, or guesses. Process exit releases the OS lease. Advisory locking is the
project's accidental-concurrent-writer boundary, not a defense against a malicious process that
ignores it.

The journal is created exactly once with `O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC`, mode `0600`,
beneath already verified no-follow directory descriptors. Recovery opens only that existing file.
There is no caller-supplied path or filename. The preamble is the exact ASCII bytes
`EA-AUDIT-V1\n`.

Every frame is:

```text
header_length_u64
canonical AuditRecord header JSON
payload_length_u64
raw canonical payload
32 raw bytes: SHA-256(b"ea.audit-frame.v1\0" + canonical_audit_record_bytes)
```

The two lengths obey the kind-specific limits above. A complete frame with a bad checksum,
non-canonical header/payload, invalid sequence, duplicate/conflicting ID, broken record link,
broken chain head, or wrong binding is committed corruption. Reopen fails closed and does not
append, truncate, skip, resynchronize, or guess.

While the exclusive writer lease is held, EOF before all bytes declared by the final frame lengths
or before its complete checksum is the one mechanically identifiable uncommitted torn suffix. All
remaining bytes are part of that incomplete suffix; v1 makes no impossible claim that a scanner
can identify separately appended garbage inside it. Recovery may truncate exactly from the first
byte of that incomplete final frame to the last verified offset, `fsync` the journal, and `fsync`
the audit directory. It records no synthetic business event. A wrong/partial preamble or any
complete bad frame is corruption. A preamble-only journal or recovered torn first frame remains
incomplete and cannot admit runtime until an exact `run.prepared` retry is acknowledged.

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
at or after step 4 returns `durability.audit_ack_mismatch`, enters a monotone failed state, and
never authorizes an effect. If rescan finds a complete valid uncertain frame, an exact retry must
perform a new successful journal `fsync`, independent positioned read-back, full verification,
and acknowledgement reconstruction before returning that acknowledgement. Merely observing bytes
written by the failed attempt never restores effect authority.

The initial file creation writes and `fsync`s the preamble and then `fsync`s the audit directory
before the first record. The journal file identity and mode are rebound before every append and
reopen. Replacement, symlink, hard-link count other than one, non-regular type, permission drift,
or directory identity drift fails closed.

### Reopen and bounded resources

Reopen first holds the writer lease, then scans from the preamble to EOF, verifies every frame,
reconstructs the sequence/index/chain, and returns no acknowledgement until scanning completes. It
enforces:

- at most 400,005 records and contiguous audit owner sequences;
- the 4,096-byte header and kind-specific 4,096/16,384-byte payload limits;
- one `run.prepared` at sequence one and no second preparation record;
- at most one terminal record, after which only exact replay is allowed; and
- no duplicate logical record ID with different bytes.

ADR 0015 still permits up to 1,000,000 market events as a data-source capability. This executable
coordinator profile deliberately admits only `N <= 100_000` market events. ADR 0017 permits at
most one new signal and one Order chain per market dispatch. The coordinator therefore admits at
most `N` authorization records, at most `N` matcher outcomes, and exactly `N + 1`
batch/completion pairs including bounded end. Preparation, at most one failing transition, and one
terminal record give `4*N + 5 <= 400_005` frames.

A small frame is at most `4_096 + 4_096 + 48 = 8_240` bytes; an outcome/authorization frame is at
most `4_096 + 16_384 + 48 = 20_528` bytes. The exact worst-case journal bound including the
12-byte preamble is:

```text
12 + (2*N + 5) * 8_240 + (2*N) * 20_528
= 5_753_641_212 bytes when N = 100_000
```

Composition requires at least 6 GiB currently free on the verified result filesystem before
runtime admission, sets a hard 6 GiB journal limit, and rejects a proposed frame before writing if
it would cross that limit. The reopen index is capped at 400,005 entries and 256 MiB measured
resident memory; construction fails before runtime if the selected implementation cannot honor
that budget. Scanning must be linear in verified bytes plus records, with no per-record historical
rescan. Later external facts or a larger executable profile require a new bounded decision.

The implementation may keep an in-memory record-ID to verified-offset index for the current
process. That index is a cache; reopen derives authority only from the journal. Iteration order of
a dictionary never selects canonical order.

### Coordinator bindings and public operations

`Phase1HistoricalLifecycleCoordinator` is factory-only and bound by identity to:

- one `RunBinding` and admitted `AuditAppendPort`;
- one dependency-neutral `RuntimeLifecyclePort`;
- one `HistoricalMatcherPort`;
- one descendant verification port already bound to those authorities;
- one `ExecutionFactAuthorityPort`;
- one `ExecutionEvidenceResolverPort` that resolves the exact historical Fill/projection named by
  an outcome; and
- one dormant-then-active `HistoricalSubmissionAuthorizationAuthority` plus its independent
  portfolio, risk, global-halt, and instrument-gate freshness ports.

The matcher port exposes only `run_id`, `spec_set`, `source_namespace`,
`match_active_market_root`, `expire_at_active_end`, and read-only exact batch resolution by
dispatch sequence/root digest. The fact port exposes only `run_id`, `spec_set`,
`process_ingress`, exact outcome lookup by ingress identity, and read-only Fill/projection
resolution. The runtime port exposes exact run/spec bindings, `active_lease`, `pop`,
`acknowledge`, `terminal_acknowledged`, and retained trace evidence. These structural protocols are
defined in `ea.core.lifecycle`; they do not reveal private authority state or concrete modules.

#### Joint construction and authorization cycle

One outer factory is the only production construction surface:

```python
create_phase1_historical_lifecycle(...) -> Phase1HistoricalLifecycle
```

It executes in one stack frame, publishes nothing until complete, and performs exactly:

1. create a dormant authorization authority with static run/spec/policy/audit/runtime and
   freshness-port bindings; its public binding properties are readable, but authorization and
   proof verification always fail `durability.audit_append_failed` while dormant;
2. create the matcher with that dormant object as its required structural verifier;
3. create descendant verification and the fact authority from runtime/matcher/order issuance;
4. create the coordinator over only the dependency-neutral ports;
5. consume a private one-use factory seal to bind the dormant authorization authority to the exact
   coordinator identity and a private preparation capability, revalidate every static binding,
   and atomically activate it; and
6. return one immutable `Phase1HistoricalLifecycle` bundle containing the coordinator and
   intentionally public read-only views, while keeping the preparation capability private.

No dormant or partially constructed object is stored globally, passed to a callback, or returned.
Any exception clears the local one-use seal and abandons all objects; none can later activate. A
second activation, foreign coordinator, equal clone, changed binding, or verification call during
construction fails closed. This is the only two-phase construction in the lifecycle and does not
permit general mutable rebinding.

Construction verifies exact run/spec/policy/source bindings and performs no pop, append, match,
fact processing, handoff, or acknowledgement. The authority exposes:

```python
coordinator.process_next_dispatch() -> CoordinatorDispatchOutcome
coordinator.retry_active_dispatch() -> CoordinatorDispatchOutcome
coordinator.retry_terminalization() -> CoordinatorTerminalOutcome
coordinator.prepare_submission_authorization(
    order: Order,
    *,
    causal_market_root: MarketDataEnvelope,
    dispatch_sequence: int,
) -> AuditAppendAcknowledgement
```

`process_next_dispatch` requires no active lease, pops exactly one runtime root, and then executes
that active causal unit. `retry_active_dispatch` requires the exact retained active lease and
replays only missing stages. Neither operation loops over an unbounded run.

The separate authorization authority structurally satisfies the execution-owned
`HistoricalSubmissionAuthorizationVerifier`; runtime does not import that Protocol. Only the
coordinator owns its private preparation capability. The matcher receives only the verifier
surface, and no other production object can append authorization or mint the opaque proof.

### Coordinator state

`CoordinatorRunState` is closed and monotone:

```text
admitted -> running -> draining -> terminalizing -> terminal
                    \-> failing -> draining -> terminalizing -> terminal_failed
```

`failing` blocks new strategy, portfolio, risk, Order creation, and submission authorization. It
does not discard already admitted roots or real matcher facts. `terminalizing` has no active lease,
admits no root or submission, and permits only exact `retry_terminalization`. `terminal` and
`terminal_failed` never reopen.

The canonical non-terminal coordinator state records the run binding, state version, current/last
dispatch sequence and root digest, matcher batch digest, ordered ingress/outcome/audit
acknowledgement digests, missing audit logical keys, monotone halt/failing reason code, and the
last already acknowledged audit chain head. A missing append has no record ID; its logical key is
replaced by the assigned `EconomicId` only after a verified acknowledgement.

`PreTerminalCoordinatorState` is constructed only after the bounded-end dispatch-completion
record is acknowledged and the runtime terminal lease is acknowledged. It contains the intended
terminal kind and audit chain head before `run.terminal`; it contains no terminal-record ID,
terminal acknowledgement, or final chain head. The terminal payload binds its digest. After the
terminal record is acknowledged, `TerminalCoordinatorState` adds that record ID,
acknowledgement digest, and resulting final chain head. The terminal record never contains the
final state digest, so no digest cycle exists. All state codecs are strict and versioned. Same
state version plus different bytes is a conflict.

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
   `ExecutionFactAuthorityPort.process_ingress`;
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
runtime-acknowledgement order. The unique terminal choreography is:

1. verify the exact bounded-end matcher batch and every outcome acknowledgement;
2. append/verify `runtime.dispatch_completed` while the terminal lease remains active;
3. rebind and call `runtime.acknowledge(lease)`;
4. require `active_lease is None` and `terminal_acknowledged is True`, then publish the exact
   `PreTerminalCoordinatorState` and enter `terminalizing`;
5. append/verify `run.terminal` whose subject is that pre-terminal-state digest and whose payload
   binds the audit chain head before the terminal record; and
6. construct the final `TerminalCoordinatorState` from the verified terminal acknowledgement and
   enter `terminal` or `terminal_failed`.

If step 3 raises while the exact active lease remains, `retry_active_dispatch` repeats only the
runtime acknowledgement after rebinding the completion record. If the runtime reports no active
lease and exact terminal acknowledgement despite an exceptional return, the coordinator verifies
the retained trace and proceeds to step 4; any contradictory state is a monotone conflict. Failure
at step 5 retains `terminalizing`, no active lease, and the exact pre-terminal state;
`retry_terminalization` retries only the same logical terminal record. No operation pops another
root in either case.

Terminal audit closes this coordinator slice but does not claim that result/report durability or
the complete reproducible-run terminal verifier exists.

### Closed failure and retry matrix

Every stage has one controlling recovery operation:

| Failed boundary | Authoritative retained evidence | State and permitted drain | Only retry path |
|---|---|---|---|
| matcher call before batch publication | active lease plus matcher state; no assumed batch | enter `failing`; query exact batch resolution; no handoff/submission | `retry_active_dispatch`, which reuses a resolved batch or repeats the failure-atomic matcher call |
| `matcher.dispatch_batch` append/ack | matcher-issued batch and all ingresses | enter `failing`; process every already issued ingress and attempt its outcome audit, but expose no handoff | `retry_active_dispatch` exact logical batch append |
| one outcome append/ack | fact authority's ingress/outcome/Fill/projection | enter `failing`; continue later ingresses in the same batch and attempt their audits; expose only independently acknowledged handoffs after the batch ack also exists | `retry_active_dispatch` exact logical outcome append |
| failing-safety append/ack | monotone in-memory failing state and failed logical key | remain `failing`; drain issued ingresses; no submission or runtime ack | `retry_active_dispatch` first retries the one failing-safety logical key |
| dispatch-completion append/ack | batch plus every required outcome acknowledgement | remain current `running|failing`; no runtime ack | `retry_active_dispatch` exact completion append |
| runtime/source acknowledgement | durable completion ack and exact active lease, or authoritative terminal-ack/trace evidence | no new root; submission remains blocked when failing/end | `retry_active_dispatch` rebinds and repeats only runtime ack, or validates the already committed terminal transition |
| terminal append/ack | no active lease plus exact `PreTerminalCoordinatorState` | remain `terminalizing`; no root, handoff, or submission | `retry_terminalization` exact logical terminal append |

Any structural/canonical conflict in retained evidence enters monotone `failing` and is not
automatically repaired. An unallocated failed append is tracked only by logical key. A verified
acknowledgement replaces it with the assigned record ID; later records cannot steal that identity
because the journal sequence is derived only from verified prefix order.

The Phase 1 coordinator accepts only the official historical runtime/source commit contract from
ADR 0016: a declared acknowledgement failure leaves the exact active lease and source candidate
unchanged. After any unexpected exception it accepts only those same unchanged bindings, or for
bounded end the independently verifiable `active_lease is None`, terminal acknowledgement, and
matching retained trace. Any third state is a conflict and no later root is admitted.

### Inbound audit failure and mandatory drain

If a batch or one outcome append/acknowledgement fails:

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
uses stable logical keys to fill only missing audit records, reconstructs the audited
handoffs, writes dispatch completion, and acknowledges the same lease. Failing state remains
monotone. Later roots may continue mandatory matcher/fact drain, but no new submission is allowed.

An audit failure never changes a fact outcome to `invalid`, removes a Fill, invents a ledger
transaction, or treats an unavailable acknowledgement as permission.

### Pre-effect submission authorization

The later strategy/portfolio/risk extension calls
`prepare_submission_authorization` only while the same causal market lease remains active. The
authorization authority is statically bound to these independent read-only ports:

```python
portfolio.current_snapshot() -> PortfolioSnapshot
risk.current_state() -> RiskStateSnapshot
global_halt.current_state() -> GlobalHaltSnapshot
instrument_gate.current_for(instrument: Instrument) -> InstrumentGateSnapshot
```

`GlobalHaltSnapshot` contains exact `run_id`, `halted`, and `global_halt_epoch`.
`InstrumentGateSnapshot` contains exact `run_id`, instrument, `held_for_order_id|None`,
`instrument_gate_id`, `instrument_gate_version`, and `halted`. These factory-only core values have
strict canonical codecs. The concrete existing ledger/risk authorities and later gate authority
are adapted in composition; no caller supplies their versions or epochs.

The preparation method accepts only the exact Order, active causal root, and dispatch sequence.
It reads and validates in this fixed order: active runtime lease/root, Order canonical evidence,
portfolio snapshot, risk state, global halt, instrument gate, then the coordinator's private
authorization-state version. It then re-reads all six bindings after audit append and before
retaining the acknowledgement. The canonical payload binds:

- exact Order and execution-request bytes/digests;
- causal market bytes/digest/root key and dispatch sequence;
- portfolio snapshot and risk-state versions;
- global/risk halt epochs;
- held instrument gate identity/version; and
- coordinator authorization-state version.

The method appends `submission.pre_effect_authorization` and retains the resulting exact
acknowledgement keyed by the domain-separated authorization subject digest specified above. It
performs no matcher call. If the Order's portfolio/risk versions are already stale, a halt is set,
the gate is not held for that Order, or any binding changes during the reads, preparation rejects
before append with `risk.stale_approval` or `submission.blocked_by_halt` as applicable. Thus
ordinary freshness drift never becomes same-logical-key/different-payload journal conflict.

The matcher immediately calls `verify_authorized_historical_submission`. That operation re-reads
the active lease and all current halt, instrument, portfolio, risk, Order, request, causal-root,
and coordinator bindings. It returns the existing ADR 0018 opaque proof only when the retained
acknowledgement binds the exact request and every state remains fresh. Drift returns the existing
`risk.stale_approval`, `durability.audit_append_failed`, or
`durability.audit_ack_mismatch` outcome as applicable and consumes no matcher sequence.

The error priority is structural/canonical conflict, stale portfolio/risk state, active halt or
lost instrument gate, missing acknowledgement, then acknowledgement mismatch. An exact
authorization retry may reuse an identical acknowledged record only after the same immediate
freshness reads pass again. A changed valid Order flow has a new Order/request and authorization
subject digest. State drift on the old Order rejects before append; it neither reuses the prior
acknowledgement nor poisons the global audit journal.

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
- unknown/missing/extra fields, non-canonical JSON/raw payload, wrong binding, and malformed
  carriers.

### Filesystem and failure injection

- no-follow creation, exclusive collision, modes, file/directory identity drift, symlink,
  hard-link, replacement, short write, zero-progress write, `fsync`, positioned read, close, and
  directory-`fsync` failures;
- crash/torn tail at every header/payload/checksum byte boundary;
- complete checksum, canonical-record, sequence, record-link, chain-head, ID, and payload
  corruption with no automatic repair;
- failure before and after journal `fsync`, lost acknowledgement, reopen, exact retry, and
  deterministic recovery;
- fresh/recovery capability issuance, live-writer lease collision, stale/foreign recovery
  evidence, second recovery, and post-terminal recovery rejection; and
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
  mismatched ack, exact retry, and zero matcher sequence on failure;
- dormant verifier, joint-factory activation failure at every step, no partial escape, and exact
  coordinator binding; and
- terminal success/failure, runtime-ack exceptional seams, terminal append failure, and
  no-active-lease `retry_terminalization`.

### Properties and boundaries

- accepted audit sequence is contiguous and its chain is prefix deterministic;
- exact logical retries are idempotent and conflicting retries fail closed;
- no audited handoff exists without the exact outcome acknowledgement;
- no runtime acknowledgement exists before every required causal record is durable;
- independently supplied input container order cannot change matcher batch or descendant order;
- `N=100_000` admission, 6 GiB disk, 400,005-entry/256 MiB index, per-kind payload, and linear
  reopen bounds fail before runtime or before an overflowing frame write;
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
