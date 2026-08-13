# ADR 0021: Staged Active-Dispatch Authorization Window

- Status: Accepted
- Date: 2026-08-11
- Decision owners: Architecture, Runtime, Execution, Backtest, Durability
- Related: ADR 0008, ADR 0014, ADR 0016, ADR 0018, ADR 0020, Issue #61,
  PR #62 review thread `PRRT_kwDOTVD3mc6YMAX-`

## Context

ADR 0020 requires a later strategy/portfolio/risk extension to prepare the pre-effect submission
authorization after every causal descendant and audited economic handoff for the current market
root, while that root's runtime lease is still active. It also defines
`process_next_dispatch() -> CoordinatorDispatchOutcome` as one non-reentrant call that writes the
dispatch-completion record, acknowledges the runtime lease, clears the active dispatch, and only
then returns.

The first implementation made both operations individually fail closed but did not provide a
state in which they can be used together. The coordinator sets its active dispatch only while
`process_next_dispatch` or `retry_active_dispatch` holds the non-reentrant mutation lock. A call to
`prepare_submission_authorization` during that interval is rejected as reentrant. After a normal
return the runtime lease and coordinator active dispatch have already been cleared. Ordinary
failures retain the lease but enter `failing`, which correctly blocks authorization. Only an
incomplete restarted run can accidentally expose an unlocked `running` coordinator with an active
lease, so authorization is restart-only instead of replay-equivalent.

This was detected by the exact-head Ready review on PR #62 and confirmed as
`ARCH61-FINAL-AUTH-WINDOW-001` and `EXEC61-READY-AUTH-001`. It is a contract conflict, not a lock
implementation detail. Releasing the mutation lock opportunistically, changing it to an
unrestricted reentrant lock, or allowing matcher/fact callbacks to reenter would expose an
authorization before the complete audited handoff frontier and weaken the single-writer boundary.

Accepted ADRs are immutable. This ADR narrowly supersedes ADR 0020's active-dispatch public
operations and completion choreography. ADR 0020's audit vocabulary, frame format, acknowledgement,
POSIX durability, resource limits, failing safety, terminalization, recovery authority, package
boundaries, and all unrelated behavior remain unchanged.

## Scope

This decision freezes:

- the pause point after all matcher descendants and audited handoffs but before dispatch completion;
- a factory-issued, restart-reissuable active-dispatch window;
- begin, resume, authorization, coordinator-owned submission, and completion operations over that
  exact window;
- authorization-attempt evidence that cannot be lost when an append commits before denial;
- a durable completion frontier that binds authorization attempts and matcher receipts;
- runtime acknowledgement, retry, crash recovery, and concurrency rules around the window; and
- the minimum public evidence needed by later ledger/reconciliation and strategy iterations.

## Non-goals

This decision does not add or change:

- ledger mutation, reconciliation correction, concrete strategy, portfolio planning, risk policy,
  Order creation policy, or result/report behavior;
- matcher fill economics, market-data visibility, runtime root ordering, or execution-fact meaning;
- a broker, credential, paper/live path, real external order write, release, deployment, tag, or
  package publication;
- multi-process coordinator writers, distributed continuations, or a serializable user token; or
- a dependency or lockfile entry.

The current iteration implements the window and proves it with test-owned deterministic Orders and
freshness ports. Later Issues decide which ledger, strategy, portfolio, and risk stages execute
between begin and completion.

## Reuse decision

The selected design reuses the existing runtime active lease, `CoordinatorRunState`, audited
handoffs, authorization authority, matcher replay, audit logical records, recovery scan, and
factory-only value pattern. The window is a small local capability and adds no dependency.

A general workflow engine or event-sourcing framework is rejected for the same reasons recorded in
ADR 0020: it would introduce a second scheduler, state encoding, recovery policy, and dependency
closure without removing EA's exact identity and durability obligations. `contextlib`, futures,
async tasks, and coroutine suspension are also rejected because their process-local execution
state is not restart evidence. An injected strategy callback is not selected: it would make the
runtime coordinator own an unbounded foreign stage while holding its mutation lock and would leave
callback identity and replay inputs undefined after restart.

## Decision

### Narrow supersession

ADR 0020's single-call market/end operation pair:

```python
process_next_dispatch() -> CoordinatorDispatchOutcome
retry_active_dispatch() -> CoordinatorDispatchOutcome
```

is superseded by a staged continuation:

```python
begin_next_dispatch() -> ActiveDispatchWindow
resume_active_dispatch() -> ActiveDispatchWindow
prepare_submission_authorization(
    window: ActiveDispatchWindow,
    order: Order,
    *,
    causal_market_root: MarketDataEnvelope,
    dispatch_sequence: int,
) -> AuditAppendAcknowledgement
submit_authorized_order(
    window: ActiveDispatchWindow,
    order: Order,
) -> HistoricalSubmissionReceipt
complete_active_dispatch(window: ActiveDispatchWindow) -> CoordinatorDispatchOutcome
retry_active_dispatch_completion() -> CoordinatorDispatchOutcome
retry_terminalization() -> CoordinatorTerminalOutcome
```

There is no convenience operation that silently begins and completes a market dispatch in one
call. Composition that has no ledger/strategy stage explicitly calls begin followed by complete.
`retry_active_dispatch_completion` is not an authorization-capable resume operation: it exists only
when durable completion evidence has already closed the window. That visible call boundary prevents
a future caller from accidentally acknowledging the causal lease before its planning stage.

### ActiveDispatchWindow

`ActiveDispatchWindow` is a factory-only immutable process capability and canonical evidence value.
It is not caller-constructible, copyable, picklable, or valid for another coordinator instance. At
most one is live for one coordinator. Its canonical schema is
`ea.coordinator-active-dispatch-window.v1` with exact fields:

```text
schema
canonicalization
run_id
lineage_sha256
manifest_sha256
coordinator_state_version
dispatch_kind
dispatch_sequence
trigger_root_key
trigger_root_sha256
batch_sha256
batch_ack_sha256
handoff_count
ordered_handoff_sha256s_sha256
audited_handoff_chain_head_sha256
authorization_allowed
```

Its framed digest domain is `b"ea.coordinator-active-dispatch-window.v1\0"`. The canonical bytes
contain no object identity, thread ID, process ID, wall time, or random nonce. Capability authority
still requires exact factory seal and coordinator-retained object identity. Recovery may therefore
reissue a new process-local object with identical canonical bytes and digest without accepting a
caller clone.

`audited_handoff_chain_head_sha256` is the fixed chain anchor at which the audited economic handoff
frontier opened. Authorization appends do not mutate the canonical window or replace this field.
The coordinator privately retains the evolving pre-completion audit head and binds that head in the
durable completion evidence. The immutable window bytes and identity are therefore stable across an
exact authorization or submission retry.

`authorization_allowed` is true only for an active market root in `running` after the complete
audited handoff frontier. It is false for bounded end, `failing`, and `draining`. One market window
admits at most one distinct canonical Order/execution-request chain, one acknowledged authorization
record, and one authoritative matcher receipt. Exact retries reuse those objects and allocate no
new record or receipt. A second distinct Order or request is rejected before audit or matcher
mutation with `OutcomeCode.RISK_STALE_APPROVAL`.

The coordinator privately advances the live window through monotone stages:

```text
open -> completion_frozen -> completion_acknowledged -> completed
```

`completion_frozen` is a provisional in-memory barrier: no new authorization or submission is
admitted, but the same exact window may retry completion. `completion_acknowledged` means the exact
completion record is durable and authorization is permanently closed; only completion/runtime-ack
reconciliation remains. `completed` consumes the window after the runtime acknowledgement or exact
committed-transition proof. "Second use" means use after `completed`, not an exact retry while
frozen or acknowledged.

### Begin and resume

`begin_next_dispatch` requires no active dispatch or terminal state. Under the non-reentrant
coordinator lock it pops one runtime lease and performs ADR 0020 market/end steps through creation
of every required `AuditedExecutionFactHandoff`. It does not append
`runtime.dispatch_completed`, acknowledge the runtime lease, or clear the active dispatch. It then
creates and retains the exact window, releases the lock, and returns it.

`resume_active_dispatch` requires the exact retained runtime lease, no terminal state, and no
durable completion record. It
replays only missing matcher, batch-audit, fact-processing, outcome-audit, handoff, or failing-safety
steps. When the audited frontier is complete it returns the already retained window by identity;
after restart it issues one replacement window with the same canonical bytes. It never performs
runtime completion or acknowledgement.

If recovery finds no completion record and no durable failing transition, an in-memory
`completion_frozen` stage was not committed and recovery reissues an `open` window. If recovery
finds a completion record, it issues no authorization-capable window and requires
`retry_active_dispatch_completion`. If it finds a durable failing transition, authorization remains
closed and mandatory drain resumes.

If the coordinator is `failing`, resume continues mandatory drain and returns a window with
`authorization_allowed=false` when completion can safely proceed. It never reopens authorization.

### Authorization-attempt outcome

`SubmissionAuthorizationAttemptOutcome` is a factory-only canonical value in `ea.core.lifecycle`.
Its schema is `ea.submission-authorization-attempt-outcome.v1`, its framed digest domain is
`b"ea.submission-authorization-attempt-outcome.v1\0"`, and it has exactly these fields:

```text
schema
canonicalization
run_id
lineage_sha256
manifest_sha256
dispatch_sequence
trigger_root_sha256
order_id
execution_request_sha256
authorization_payload_sha256
status
logical_key
acknowledgement_sha256
error_code
```

The strict status/nullability table is:

| Status | Logical key | Acknowledgement | Error | Meaning |
|---|---|---|---|---|
| `denied` | null | null | required | policy/freshness rejected before append |
| `unresolved` | required | null | required | append durability is uncertain; no later stage may run |
| `authorized` | required | required | null | exact authorization is durable and usable |
| `burned` | required | required | required | append is durable but post-append freshness closed its use |
| `failed` | required | null | required | authoritative rescan proved no record; coordinator enters failing |

Unexpected structural, identity, or tamper conflicts may raise, but the coordinator must invoke the
attempt resolver after every return or exception before advancing state. Expected policy and
durability paths return and retain an outcome before the public method translates it to the exact
domain result or error. An `unresolved` outcome is settled only by ADR 0020's authoritative journal
rescan, exact retry, fresh fsync, and independent readback; it becomes `authorized`, `burned`, or
`failed`, never an assumed acknowledgement. Completion is prohibited while it remains unresolved.

### Authorization inside the window

`prepare_submission_authorization` independently acquires the coordinator lock. Before every
inner call and after every callback/append return or exception it requires:

- the supplied window is the exact retained live object;
- the coordinator state version and canonical window evidence are unchanged;
- the runtime retains the identical active market lease/root/sequence;
- every batch, outcome, Fill, projection, handoff, and current audit acknowledgement resolves to
  the authoritative evidence captured by the window;
- `authorization_allowed` is true and phase is exactly `running`; and
- the Order, causal market root, and dispatch sequence are exact and mutually bound.

The method delegates the existing coordinator-private preparation capability. A successful exact
retry returns the original acknowledgement. A stale/foreign/consumed window, bounded end, wrong
root/sequence, failing/draining/terminal phase, incomplete frontier, or evidence drift rejects
before a new append.

The preparation port returns or resolves the closed `SubmissionAuthorizationAttemptOutcome` so the
coordinator can retain the exact acknowledgement even when the audit append committed but the
attempt became burned during post-append freshness validation. The outcome binds the Order/request
identity, canonical payload digest, logical key, acknowledgement when present, and
the strict status table above. Expected denial without an append has no acknowledgement. An
exceptional return cannot hide a committed authorization record.

The coordinator retains the attempt outcome for the live window. It does not change the
authorization state version used by exact attempt replay. The latest acknowledged attempt chain
head, or the audited-handoff anchor when there is no attempt, is coordinator-private evolving
pre-completion state.

### Coordinator-owned matcher submission

`submit_authorized_order` is the only composed public path that may mutate matcher submission
state. It independently acquires the same coordinator lock and requires the exact live `open`
window, the identical occupied Order/request chain, an exact `authorized` outcome and
acknowledgement, the identical active runtime root/sequence, and the complete authoritative handoff
frontier. It revalidates those bindings before and after invoking a dependency-neutral matcher
submission port. It retains the exact `HistoricalSubmissionReceipt`; an exact retry returns that
receipt without a second mutation.

The composition bundle keeps the mutable matcher and authorization authority private. It may expose
read-only history/resolver views, but no raw `matcher.submit`, preparation capability, activation
seal, or mutable authority. A durable authorization without a matcher receipt is safe and
retryable, but completion rejects it: every `authorized` attempt must have exactly one matching
authoritative receipt. `denied`, `burned`, `failed`, and absent attempts have no receipt, and an
orphan receipt is always a conflict.

### Completion and runtime acknowledgement

`complete_active_dispatch` acquires the non-reentrant coordinator lock and consumes only the exact
retained window. It performs in order:

1. revalidate the window, runtime lease, batch, every outcome, Fill, projection, handoff, retained
   authorization attempt, and matcher submission derived from this causal dispatch;
2. enter provisional `completion_frozen` and freeze the authorization/submission frontier;
3. construct the pre-ack state using the latest acknowledged audit chain head, including any
   authorization record;
4. append and verify versioned `runtime.dispatch_completed` evidence that binds the exact
   authorization/submission frontier, then enter `completion_acknowledged`;
5. revalidate every authoritative binding again;
6. call `runtime.acknowledge` for the identical lease;
7. publish `CoordinatorDispatchOutcome`, enter `completed`, consume the window, and clear the active
   dispatch; and
8. for bounded end, continue ADR 0020 terminalization.

No authorization or matcher submission is admitted after completion starts. A foreign, stale, or
second-use window is rejected without an append or runtime acknowledgement. While a window is live,
`begin_next_dispatch` rejects and the runtime cannot expose another root.

This branch has not merged ADR 0020's v1 completion implementation, so this ADR replaces it with
`ea.audit-dispatch-completed.v2`. In addition to ADR 0020's fields, the payload has exactly:

```text
authorization_attempt_count                 # uint64, exactly 0 or 1
authorization_attempt_outcome                # null or the strict canonical outcome object
authorization_attempt_outcome_sha256         # null iff count == 0; verifies the object
submission_count                            # uint64, exactly 0 or 1
ordered_submission_receipt_sha256s_sha256   # digest of the ordered receipt-digest tuple
```

The ordered aggregate uses domain
`b"ea.coordinator-ordered-submission-receipt-digests.v1\0"`; the zero-receipt digest is a defined
domain-separated empty tuple, not null. The completion pre-ack state binds the latest private audit
head, while the v2 payload independently fixes the exact attempt outcome and receipt frontier.
Completion requires the bijection above and rejects `authorized` without a receipt, any receipt
without `authorized`, an unresolved attempt, count/digest mismatch, or a matcher history beyond the
frontier. Recovery compares these durable fields with exact authoritative authorization and matcher
history; later receipt injection cannot alter a completed dispatch.

A definite completion-append failure with authoritative proof that no record exists either restores
the same window to `open` when no failing transition was made, or follows the durable failing path
with authorization closed. A committed-before-raised append is resolved to
`completion_acknowledged`; no window is reissued and only completion/runtime acknowledgement is
retried.

### Failure and recovery matrix

| Boundary | Retained evidence | Retry/recovery behavior |
|---|---|---|
| crash before audited frontier | active lease plus partial batch/outcomes | `resume_active_dispatch` reuses authoritative inner evidence and fills only missing audit stages |
| frontier complete before window return | complete batch/outcomes/handoffs, no completion | reissue the canonical-equivalent window; no inner mutation repeats |
| window returned before authorization | same as above | reissue the window; zero authorization exists |
| authorization append fsynced before acknowledgement return | exact audit record and authority attempt | reconstruct attempt outcome; exact prepare retry returns the original ack or the same burned result |
| authorization succeeds before matcher submission | durable authorization, no receipt | reissue window; exact matcher submit may occur once |
| matcher receipt published before completion | authorization plus authoritative receipt | reissue window; exact submit is replay and completion may proceed |
| authorized attempt without receipt at completion | durable authorization, no receipt | reject completion; keep the same open window so exact submit can finish |
| crash after provisional freeze before completion append | no completion record and no durable failing transition | discard the process-local freeze and reissue the open canonical-equivalent window |
| definite completion append failure with no record | retained open frontier | reopen the same window, or enter durable failing with authorization closed |
| completion append commits before acknowledgement return | exact completion record | no window; recover `completion_acknowledged` and retry completion/runtime acknowledgement only |
| completion record exists and runtime remains active | completion ack plus identical active lease | no window; retry only runtime acknowledgement |
| runtime acknowledgement raises before commit | completion ack plus identical active lease | retry only runtime acknowledgement |
| runtime acknowledgement commits before raising | completion ack plus authoritative runtime trace | recognize committed transition exactly as ADR 0020 requires |

Recovery groups physical audit records by logical dispatch identity rather than assuming that every
retry record is physically adjacent. Within one dispatch it requires batch/outcomes before any
authorization and every authorization before completion. It reconstructs the authorization
authority and matcher history at exactly the last incomplete/completed frontier before reissuing a
window. It rejects a future authorization attempt or matcher receipt not fixed by that frontier.
Uninterrupted and restarted execution therefore expose the same operations at the same logical
frontier.

### Package direction and composition boundary

The window, attempt outcome, strict codecs, domains, and dependency-neutral authorization and
matcher submission protocols live in `ea.core.lifecycle`. Those protocols may refer only to core
Order, root, acknowledgement, proof, and `HistoricalSubmissionReceipt` values. The runtime
coordinator imports core protocols and values only; it does not import an `ea.execution` concrete
class. The historical matcher structurally implements the submission port. Composition owns the
concrete matcher, authorization authority, factory seals, and preparation capability and exposes
only the staged coordinator facade plus read-only history/resolver views.

### Concurrency and capability safety

The coordinator mutation lock is never released inside one operation and is never made generally
reentrant. It is released only at the explicit window boundary. Every public operation reacquires
it and revalidates exact retained evidence. Concurrent begin/resume/prepare/complete calls serialize;
only one can consume or advance a window. No caller receives the authorization authority's private
preparation capability, audit path/file descriptor, runtime lease mutation, or a general callback
into the coordinator.

The window is O(1) plus the already bounded tuple of audited handoffs and the single authorization
attempt/receipt belonging to one active market dispatch. An end/failing/draining window admits zero
Orders, authorization records, or receipts. Exact retries allocate none. This preserves ADR 0020's
worst-case `4N + 5` audit records and historical runtime admission for `N <= 100,000`; it does not
retain historical journal payloads or
change ADR 0020's 100,000-market-record, 400,005-audit-record, 6 GiB, or 256 MiB compact-index
bounds.

## Required evidence

Implementation acceptance requires:

- a composed public-path healthy authorization and coordinator-owned matcher submission between
  begin and complete, with no mutable matcher/authority bypass exposed by composition;
- exact audit order `batch/outcomes -> authorization -> completion` and runtime acknowledgement last;
- strict attempt-outcome codec/status/nullability tests, including uncertain append rescan and
  acknowledged-but-burned retention;
- one-Order/one-request/one-authorization/one-receipt admission, exact retry without allocation,
  and pre-mutation rejection of a second distinct chain;
- completion-v2 count/digest/bijection validation and rejection of authorized-without-receipt,
  orphan/future receipt, unresolved attempt, and aggregate mismatch;
- zero-append rejection before begin, after completion, for end/failing, and for wrong,
  foreign, stale, cloned, or second-use windows;
- begin/resume identity replay in one process and canonical-equivalent window reissuance after
  restart;
- exact authorization retry, expected denial, post-append freshness burn, append failure, and
  committed-before-raised evidence retention;
- crash/restart at every row in the failure matrix, including provisional freeze and
  committed-before-raised completion, without duplicate inner mutation, append,
  matcher receipt, or runtime acknowledgement;
- concurrency/reentrancy tests proving a second mutation cannot enter the live window;
- property and cross-process tests for canonical window bytes/digest and ambient-context
  independence;
- AST import tests preserving the existing package direction; and
- focused, official full, coverage, reproducible-wheel, clean-install, exact-head CI, and
  independent exact-SHA expert verification.

## Consequences

The coordinator becomes an explicitly staged state machine instead of pretending a later planning
stage can fit inside an already completed call. Runtime acknowledgement remains fail closed and
last. The same active lease is visible to ledger/strategy integration without exposing it as a
mutation capability. Recovery has one more derived process-local capability to reissue, but no new
durable record kind or external dependency.

Callers that only drain historical roots must make the begin/complete boundary explicit. This is a
small API migration now and prevents a larger hidden ordering break when the next ledger and
strategy slices arrive.

## Alternatives rejected

### Treat authorization as deliberately unreachable until the strategy Issue

Rejected. The public coordinator API and recovery logic already claim this seam, and restart-only
reachability makes behavior depend on whether the process crashed.

### Release the coordinator lock around the existing public method

Rejected. There is no durable pause state or capability, so another root/completion call could race
the authorization and the caller could act before the full audited frontier.

### Replace `Lock` with `RLock`

Rejected. It would allow unrelated coordinator mutations to reenter from matcher, fact, audit, or
future stage callbacks and would not itself create a correctly ordered call site.

### Inject and execute a foreign planning callback while holding the lock

Rejected. Callback identity, deterministic retry inputs, exceptional exits, and reconstruction
would be implicit, and runtime would own a strategy/portfolio stage outside its package boundary.

### A random or serialized continuation token

Rejected. Randomness is not needed for an in-process factory capability and would contaminate
canonical evidence. A serialized token could be copied or replayed by callers and would weaken the
coordinator-owned one-window invariant.
