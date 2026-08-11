# ADR 0021: Staged Active-Dispatch Authorization Window

- Status: Proposed
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
- begin, resume, authorization, and completion operations over that exact window;
- authorization-attempt evidence that cannot be lost when an append commits before denial;
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
complete_active_dispatch(window: ActiveDispatchWindow) -> CoordinatorDispatchOutcome
retry_terminalization() -> CoordinatorTerminalOutcome
```

There is no convenience operation that silently begins and completes a market dispatch in one
call. Composition that has no ledger/strategy stage explicitly calls begin followed by complete.
That visible call boundary prevents a future caller from accidentally acknowledging the causal
lease before its planning stage.

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
audit_chain_head_sha256
authorization_allowed
```

Its framed digest domain is `b"ea.coordinator-active-dispatch-window.v1\0"`. The canonical bytes
contain no object identity, thread ID, process ID, wall time, or random nonce. Capability authority
still requires exact factory seal and coordinator-retained object identity. Recovery may therefore
reissue a new process-local object with identical canonical bytes and digest without accepting a
caller clone.

`authorization_allowed` is true only for an active market root in `running` after the complete
audited handoff frontier. It is false for bounded end, `failing`, and `draining`. A window remains
open across zero or more authorization attempts and matcher submissions and is consumed exactly
once by completion.

### Begin and resume

`begin_next_dispatch` requires no active dispatch or terminal state. Under the non-reentrant
coordinator lock it pops one runtime lease and performs ADR 0020 market/end steps through creation
of every required `AuditedExecutionFactHandoff`. It does not append
`runtime.dispatch_completed`, acknowledge the runtime lease, or clear the active dispatch. It then
creates and retains the exact window, releases the lock, and returns it.

`resume_active_dispatch` requires the exact retained runtime lease and no terminal state. It
replays only missing matcher, batch-audit, fact-processing, outcome-audit, handoff, or failing-safety
steps. When the audited frontier is complete it returns the already retained window by identity;
after restart it issues one replacement window with the same canonical bytes. It never performs
runtime completion or acknowledgement.

If the coordinator is `failing`, resume continues mandatory drain and returns a window with
`authorization_allowed=false` when completion can safely proceed. It never reopens authorization.

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

The preparation port returns or resolves one closed `SubmissionAuthorizationAttemptOutcome` so the
coordinator can retain the exact acknowledgement even when the audit append committed but the
attempt became burned during post-append freshness validation. The outcome binds the Order/request
identity, canonical payload digest, logical key, acknowledgement when present, and
`authorized|denied|burned`. Expected denial without an append has no acknowledgement. An
exceptional return cannot hide a committed authorization record.

The coordinator retains acknowledged attempt outcomes in audit owner-sequence order for the live
window. They do not change the authorization state version used by exact attempt replay. The latest
acknowledged attempt chain head, or the last outcome/batch acknowledgement when there is no attempt,
is the window's current pre-completion audit chain head.

Matcher submission remains a distinct execution-owned mutation. The public lifecycle bundle may
submit only while the same window remains live; the matcher independently requires the existing
opaque proof from the authorization authority. A durable authorization without a matcher receipt
is safe and retryable; it is never treated as a submission.

### Completion and runtime acknowledgement

`complete_active_dispatch` acquires the non-reentrant coordinator lock and consumes only the exact
retained window. It performs in order:

1. revalidate the window, runtime lease, batch, every outcome, Fill, projection, handoff, retained
   authorization attempt, and matcher submission derived from this causal dispatch;
2. freeze the authorization/submission frontier for that window;
3. construct the pre-ack state using the latest acknowledged audit chain head, including any
   authorization record;
4. append and verify `runtime.dispatch_completed`;
5. revalidate every authoritative binding again;
6. call `runtime.acknowledge` for the identical lease;
7. publish `CoordinatorDispatchOutcome`, consume the window, and clear the active dispatch; and
8. for bounded end, continue ADR 0020 terminalization.

No authorization or matcher submission is admitted after completion starts. A foreign, stale, or
second-use window is rejected without an append or runtime acknowledgement. While a window is live,
`begin_next_dispatch` rejects and the runtime cannot expose another root.

The dispatch-completion payload schema remains unchanged. Audit sequence and chain position prove
that every retained authorization record precedes completion. The pre-ack state digest binds the
latest pre-completion chain head. Recovery additionally requires every matcher submission whose
causal dispatch is at or before the completion frontier to have exact durable authorization
evidence, as ADR 0020 already requires.

### Failure and recovery matrix

| Boundary | Retained evidence | Retry/recovery behavior |
|---|---|---|
| crash before audited frontier | active lease plus partial batch/outcomes | `resume_active_dispatch` reuses authoritative inner evidence and fills only missing audit stages |
| frontier complete before window return | complete batch/outcomes/handoffs, no completion | reissue the canonical-equivalent window; no inner mutation repeats |
| window returned before authorization | same as above | reissue the window; zero authorization exists |
| authorization append fsynced before acknowledgement return | exact audit record and authority attempt | reconstruct attempt outcome; exact prepare retry returns the original ack or the same burned result |
| authorization succeeds before matcher submission | durable authorization, no receipt | reissue window; exact matcher submit may occur once |
| matcher receipt published before completion | authorization plus authoritative receipt | reissue window; exact submit is replay and completion may proceed |
| completion append/ack failure | retained window plus completion logical evidence | exact completion retry; do not authorize or submit anything new |
| runtime acknowledgement raises before commit | completion ack plus identical active lease | retry only runtime acknowledgement |
| runtime acknowledgement commits before raising | completion ack plus authoritative runtime trace | recognize committed transition exactly as ADR 0020 requires |

Recovery groups physical audit records by logical dispatch identity rather than assuming that every
retry record is physically adjacent. Within one dispatch it requires batch/outcomes before any
authorization and every authorization before completion. It reconstructs the authorization
authority and matcher history before reissuing a window. Uninterrupted and restarted execution
therefore expose the same operations at the same logical frontier.

### Concurrency and capability safety

The coordinator mutation lock is never released inside one operation and is never made generally
reentrant. It is released only at the explicit window boundary. Every public operation reacquires
it and revalidates exact retained evidence. Concurrent begin/resume/prepare/complete calls serialize;
only one can consume or advance a window. No caller receives the authorization authority's private
preparation capability, audit path/file descriptor, runtime lease mutation, or a general callback
into the coordinator.

The window is O(1) plus the already bounded tuple of audited handoffs and acknowledged authorization
attempts belonging to the one active dispatch. It does not retain historical journal payloads or
change ADR 0020's 100,000-market-record, 400,005-audit-record, 6 GiB, or 256 MiB compact-index
bounds.

## Required evidence

Implementation acceptance requires:

- a composed public-path healthy authorization and matcher submission between begin and complete;
- exact audit order `batch/outcomes -> authorization -> completion` and runtime acknowledgement last;
- zero-append rejection before begin, after completion, for end/failing, and for wrong,
  foreign, stale, cloned, or second-use windows;
- begin/resume identity replay in one process and canonical-equivalent window reissuance after
  restart;
- exact authorization retry, expected denial, post-append freshness burn, append failure, and
  committed-before-raised evidence retention;
- crash/restart at every row in the failure matrix without duplicate inner mutation, append,
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
