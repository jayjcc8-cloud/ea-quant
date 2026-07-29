# ADR 0016: Deterministic Historical Runtime Frontier and Virtual Clock

Date: 2026-07-29

## Status

Proposed

## Context

Accepted ADR 0003 requires Phase 1 backtests to use one mode-neutral runtime path, a deterministic
queue, and an injected virtual clock. Accepted ADR 0004 defines knowledge-time visibility and
canonical admission order. Accepted ADR 0008 defines one global root key and requires serialized
root dispatch. Accepted ADR 0009 implements factory-sealed bounded plans and a single-active
dispatch lease, while explicitly deferring incremental source frontiers, lifecycle, dispatch
identity, and audit gates. Accepted ADR 0015 now implements a bounded historical source that:

- privately owns the selected future payloads;
- reveals only the next availability timestamp as scheduling metadata;
- admits only events visible at the injected clock;
- returns a source-issued candidate cursor without committing it; and
- requires the runtime to commit that cursor only after successful processing.

The repository still has no owner for virtual-time advancement or for the transaction spanning
next-availability discovery, admission, root dispatch, acknowledgement, and cursor commit.
Materializing the entire history before dispatch would require a planning clock to run ahead of
the decision clock or would expose later payloads prematurely. Recreating one bounded queue per
event would restart `dispatch_sequence` at one. Committing a multi-event candidate cursor before
every event is acknowledged could skip data after a partial failure.

Issue #53 therefore owns the smallest historical coordination boundary. It does not implement the
complete ADR 0003 lifecycle/audit coordinator or any strategy, portfolio, risk, matcher, ledger,
result, paper, or live behavior.

## Reuse assessment

The needed capability is project-specific coordination among the already accepted source cursor,
root key, sealed plan, and live dispatch capability.

The selected approach reuses `Phase1HistoricalMarketDataSource`,
`Phase1HistoricalSourceCursor`, `RuntimeRootOrderKey`, `BoundedRuntimeRootPlan`,
`RuntimeDispatchLease`, and the Python 3.12 standard library. It introduces no dependency,
transitive package, native runtime, lockfile change, deployment burden, or third-party scheduler
authority.

Alternatives inspected on 2026-07-29:

- `transitions` v0.9.3, MIT, latest release 2025-07-02, unarchived with its latest observed
  default-branch commit on 2025-09-09. It can encode a lifecycle state machine, but it does not
  provide knowledge-time source admission, canonical heterogeneous root ordering, cursor commit,
  live dispatch capabilities, or no-look-ahead proofs.
- NautilusTrader v1.230.0, LGPL-3.0, released 2026-06-29 and active on 2026-07-29. It provides a
  mature event engine and backtester, but adopting its clock, engine, message, portfolio, and
  execution ownership would displace rather than implement the Accepted EA contracts and would
  add substantial native/transitive, migration, operational, and lock-in cost.

Both alternatives require an adapter around the exact EA semantics; neither removes the material
logic in this slice. The local standard-library-first implementation is smaller and safer.

## Decision

### Scope and ownership

The mode-neutral `ea.runtime` package owns:

- the read-only virtual-clock capability seen by consumers;
- the sole capability that advances that clock;
- incremental historical frontier preparation;
- the continuous run-scoped dispatch sequence;
- one exact live dispatch lease at a time;
- acknowledgement of that lease; and
- commit of the matching source cursor only after acknowledgement.

`ea.data` continues to own source payload storage, visibility, and source-issued cursor
validation. `ea.core` continues to own canonical roots, root keys, and dependency-neutral values.
The historical frontier coordinates those owners but does not duplicate their validation or
inspect source-private state.

The source, mutable clock authority, candidate cursor, and queue/frontier state are never supplied
to strategy, feature, portfolio, risk, execution, matcher, or result code. A later coordinator
will receive only the frontier's current `RuntimeDispatchLease` and the read-only clock contract.

### Factory-only single-use frontier

The public construction direction is:

```python
create_phase1_historical_runtime_frontier(
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    source: Phase1HistoricalMarketDataSource,
) -> Phase1HistoricalRuntimeFrontier
```

The exact public name may change before implementation review, but the contract may not. Direct
construction of the frontier or its clock raises `TypeError`. The factory requires exact canonical
types and creates a single-use run-bound graph. It does not accept a raw event iterable, clock,
cursor, replay window, initial dispatch sequence, end root, producer namespace, or future
iterator.

The factory:

1. binds the frontier to the exact `run_id`, instrument-spec set identity and digest, source
   replay window, source semantic fingerprint, and fixed Phase 1 profile;
2. initializes committed source progress to `None`;
3. initializes `dispatch_sequence` to one;
4. initializes the virtual clock to `source.replay_window.start_inclusive`;
5. fixes the end producer namespace to `runtime.phase1.historical` and its producer sequence to
   zero; and
6. creates no root and calls neither source operation during construction.

The factory is side-effect free apart from allocating in-memory state. A frontier cannot be
rebound, reset, rewound, cloned, restarted, or reused for another run.

### Read-only virtual clock

The issued clock satisfies the existing `Clock` protocol:

```python
clock.now() -> datetime
```

`now()` returns the exact current canonical UTC value and has no side effect. It never reads wall
time. The public clock has no `advance`, `set`, `tick`, `sleep`, iterator, callback, scheduler, or
mutable attribute.

Only the bound frontier holds the private in-process authority needed to advance the clock.
Advancement accepts one exact canonical UTC target:

- a target earlier than the current value is rejected before mutation;
- a target equal to the current value is a valid no-op;
- a later target replaces the current value exactly; and
- a target at or after replay-window end is allowed only for the canonical terminal transition
  defined below.

No event source or downstream consumer can advance the clock. The clock is advanced only between
dispatch units and never while a lease is active.

### One-event candidate frontier

The historical frontier deliberately admits at most one market event per candidate. This makes
source progress and root acknowledgement one transaction and removes partial-batch commit
ambiguity.

When no candidate, live lease, or acknowledged terminal is present, preparing the next root does
exactly:

1. call `source.next_available_at(committed_cursor)` once;
2. if a timestamp is returned, require it to be canonical UTC, not earlier than `clock.now()`, and
   strictly earlier than `replay_window.end_exclusive`;
3. advance the private clock authority to that timestamp;
4. call `source.admit(clock=read_only_clock, cursor=committed_cursor, limit=1)` once;
5. require one exact `MarketDataEnvelope`, a source-issued candidate cursor whose `as_of` equals
   `clock.now()`, and a candidate last event byte-identical to the admitted event;
6. require the event to be visible at the clock and its complete runtime root key to be strictly
   greater than the last acknowledged root key, when one exists;
7. pass only that event through `prepare_bounded_runtime_roots`; and
8. retain the sealed one-root plan and candidate cursor privately without committing either
   source progress or a dispatch sequence.

An empty admission after a non-null `next_available_at`, more than one event despite `limit=1`,
cursor/event mismatch, time mismatch, non-monotone root key, or unsealed/invalid plan fails closed.
The candidate is not published and the committed cursor and dispatch sequence do not change.

When the next source event shares the current availability timestamp, step 3 is a no-op and the
next canonical event is drained through another one-event transaction. Consequently equal-time
events retain the full ADR 0004/0008 order without batching or cursor ambiguity.

`peek` may prepare and return the current candidate root but does not create dispatch evidence,
advance `dispatch_sequence`, or commit the cursor. Repeated `peek` returns the same object while
the candidate remains pending. There is no public operation to request or inspect any later root.

### Dispatch and source commit transaction

`pop` returns one factory-issued `RuntimeDispatchLease` for the current candidate and consumes the
current positive uint64 `dispatch_sequence`. Exactly one lease may be active. The sequence starts
at one and advances continuously across every replenished market root and the terminal root; it
never restarts at a frontier boundary.

While a lease is active:

- `peek`, another `pop`, frontier preparation, and clock advancement are rejected;
- only the exact same live lease can be acknowledged;
- no cursor is committed; and
- the current root, candidate cursor, clock, and sequence binding remain stable.

Acknowledging the exact live market lease is one preflighted state transition:

1. verify lease object identity, root object identity, root canonical key, and dispatch sequence;
2. verify the privately retained candidate cursor still binds the source and byte-identical root;
3. compute the complete next state without mutating the current state;
4. clear the live lease and current candidate;
5. set the committed cursor to that exact candidate cursor;
6. retain the acknowledged root key and dispatch evidence; and
7. expose the next sequence only if the prior sequence was below `2**64 - 1`.

If any preflight fails, none of those fields changes. A stale, forged, repeated, or foreign lease
cannot acknowledge a root or commit source progress.

The acknowledgement means that the later full runtime coordinator has completed every consequence
it owns for the dispatch unit. Issue #53 does not define or call those consequences. If a consumer
callback fails, it must not acknowledge; the live lease remains active and no later source payload
can be admitted. Stop/failure abandonment and audited safety drain remain a later lifecycle issue.

If sequence `2**64 - 1` is acknowledged before the terminal root can be dispatched, the frontier
is deterministically exhausted for dispatch identity and fails closed with no cursor skip or
terminal acknowledgement. Sequence zero is never issued.

### Incremental queue clarification

ADR 0009's factory-only `BoundedRuntimeRootPlan` remains unchanged. A plan is still immutable,
non-empty, completely validated, and canonically sorted. Issue #53 permits the runtime frontier to
consume a sequence of sealed one-root plans instead of only one lifetime plan.

This is not a public mutable priority queue:

- replenishment is private to the factory-issued historical frontier;
- it occurs only with no candidate or live lease;
- every replenished root key must be strictly greater than the last acknowledged key;
- the historical source operation and candidate cursor prove which market root is next;
- only the fixed terminal root may follow source exhaustion; and
- no arbitrary insertion, removal, cancellation, reschedule, seek, or rewind API exists.

The existing bounded `DeterministicRootQueue` API may remain for callers that already possess a
complete sealed plan. Implementation may extract shared private dispatch-state logic, but it must
not weaken the existing constructor seal, fact-ingress issuance checks, or exact acknowledgement
contract.

This ADR supersedes ADR 0009 only to authorize the above private incremental frontier. It does not
authorize a generic growing-source frontier or mutable queue.

### Exhaustion and the terminal root

When `source.next_available_at(committed_cursor)` returns `None`, the frontier:

1. requires at least one market cursor to have been committed, because ADR 0015 selections are
   non-empty;
2. advances the clock exactly to `replay_window.end_exclusive`;
3. constructs exactly one:

```python
EndOfRunRoot(
    available_at=replay_window.end_exclusive,
    kind=EndOfRunKind.BOUNDED_SOURCE_EXHAUSTED,
    producer_namespace=SourceNamespace("runtime.phase1.historical"),
    producer_sequence=0,
    run_id=run_id,
)
```

4. requires its key to be strictly greater than the last acknowledged market key; and
5. passes it through the same sealed one-root plan, live lease, continuous sequence, and exact
   acknowledgement path.

The terminal root has no source candidate cursor. Acknowledging it marks the frontier completed.
After completion, `peek` and `pop` deterministically report exhaustion, `acknowledge` rejects every
lease, `clock.now()` remains `end_exclusive`, and the source is never called again. Repeated
preparation before terminal `pop` or acknowledgement returns the same retained terminal object;
it cannot create a second terminal root.

The replay-window end is chosen instead of the last event time so the terminal root is strictly
later than every selected event (`available_at < end_exclusive`) and has a stable value independent
of file row order or wall time.

### Observable state and evidence

The frontier exposes only:

- exact run and spec-set binding;
- read-only `clock`;
- current committed-event count;
- current positive next dispatch sequence or `None` after uint64 exhaustion;
- whether a lease is active;
- whether the terminal root is acknowledged; and
- current-root `peek`, `pop`, and exact-lease `acknowledge` operations.

It does not expose a source, source cursor, candidate cursor, root plan, future count, collection,
iterator, replay window mutation, or acknowledged payload history. A minimal immutable snapshot
may expose canonical scalar evidence needed by audit/tests: run ID, source fingerprint, replay
window, clock value, committed event count, last committed market key encoding or digest, next
dispatch sequence, active dispatch identity, and terminal state. It must not expose a future
payload.

Canonical golden trace evidence for each acknowledged unit contains:

```text
run_id
data_sha256
clock_now
dispatch_sequence
runtime_root_order_key
root canonical bytes or an explicit canonical terminal encoding
committed_event_count
committed_cursor_as_of or null
terminal_acknowledged
```

Serialization uses existing canonical UTC, integer, identity, root-record, and sorted compact JSON
rules. No Python `repr`, object address, hash randomization, locale, filesystem path, wall time, or
exception message enters the evidence.

### Failures and atomicity

Existing exact validation failures continue to use their owning closed types:

- source scheduling, cursor, and admission failures remain `HistoricalMarketDataError`;
- root, plan, key, queue, lease, clock, and frontier validation failures use
  `RuntimeOrderingError` with `validation.invalid_type`, `validation.out_of_range`, or
  `validation.conflicting_id` according to ADR 0009;
- instrument-spec/run binding mismatches fail before state publication.

The implementation never selects a code by parsing exception text. Unexpected exceptions from a
structural operation are translated at the owning boundary without leaking a partially mutated
state.

Candidate preparation is atomic for cursor, root, and sequence state. Clock advancement occurs
before source admission because visibility requires the advanced time; if admission then fails,
the clock may remain at that scheduling timestamp, but no candidate or cursor is committed.
Repeating preparation uses the same committed cursor and same clock, so the source must replay the
same admission or the same deterministic failure. The clock can never regress to conceal that
attempt.

Acknowledgement is fully preflighted and atomic. There is no best-effort cursor commit. A process
crash persistence/recovery protocol is outside this in-memory Phase 1 slice; cross-process tests
mean independent processes produce identical evidence from identical inputs, not that an
in-process object survives a crash.

### Lifecycle slice

Issue #53 implements only:

```text
open -> terminal_pending -> completed
```

with a mutually exclusive fail-closed inability to advance when a live lease or sequence
exhaustion blocks progress. It does not claim the ADR 0003 graph lifecycle
`constructed -> starting -> running -> stopping -> stopped` or
`starting | running | stopping -> failing -> failed`.

The later full coordinator must:

- own component start/stop order and audit/result lifetimes;
- call frontier acknowledgement only after all consequences of the current unit succeed;
- add mandatory inbound and outbound audit acknowledgement gates;
- implement stop/failure cutover, audited abandonment, and safety drain;
- integrate strategy, portfolio, risk, OMS, matcher, ledger, reconciliation, and result adapters;
  and
- preserve this frontier's clock, ordering, dispatch, and cursor transaction.

## Verification

Implementation requires:

- unit tests for factory seals, exact types, clock initialization/read/advance authority,
  same-time no-op, regression, and replay-window bounds;
- unit and property tests for one-event admission, equal-time events, revisions, canonical order,
  candidate replay, live-lease exclusion, exact acknowledgement, stale/forged/foreign leases,
  cursor commit, and failure atomicity;
- sequence tests spanning multiple frontiers, uint64 edge behavior, and proof that zero/restart is
  impossible;
- exhaustion tests proving one terminal root at exact replay end and no later source call;
- malicious fake/source tests for empty/multiple admission, cursor mismatch, future event, clock
  mismatch, non-monotone key, forged plan, and structural exceptions;
- cross-process golden tests proving identical clock/root/dispatch/cursor/terminal evidence;
- import-boundary tests proving `core` and policy packages do not depend on runtime/data adapters;
- full lint, strict typing, tests, coverage, and independent exact-SHA verification; and
- README and architecture updates that state this slice is not the complete runtime or backtest.

## Consequences

- A bounded source can drive time and dispatch incrementally without exposing its future payloads.
- Source cursor commit and one market-root acknowledgement become the same deterministic
  transaction.
- Dispatch identities remain continuous even though the source frontier is replenished.
- Equal-time events are drained in canonical order without partial-batch ambiguity.
- The terminal timestamp and root are deterministic and independent of wall time.
- The design intentionally pays one source admission call per market root; Phase 1 favors clear
  correctness over batching throughput.
- The complete lifecycle, audit gates, policy chain, matcher, ledger integration, and results
  remain explicit later work.

## Rejected alternatives

### Materialize the entire source before dispatch

Rejected because admission needs a clock. Advancing the decision clock to the end before the first
dispatch creates look-ahead, while a second hidden planning clock creates two competing time
truths and weakens cursor-commit semantics.

### Commit a multi-event cursor and dispatch its roots afterward

Rejected because a failure after a strict subset of roots would either skip the remaining events
or require a second partial-progress journal not present in ADR 0015.

### Recreate the current queue for every event

Rejected because the current factory starts `dispatch_sequence` at one, breaking run-scoped
dispatch identity and downstream provenance.

### Expose `advance_to` on the clock

Rejected because a policy, matcher, or adapter could move knowledge time and reveal future data.

### Let the source advance the clock

Rejected because the source owns visibility, not scheduling or runtime sequencing, and ADR 0003
assigns clock/event-source coordination to the runtime.

### Admit every event sharing one availability timestamp as one candidate

Rejected for this slice because cursor commit would no longer align with one exact root
acknowledgement. Later optimization requires an equally strong atomic progress contract and a
separate decision.

### Put future roots in a public mutable priority queue

Rejected because arbitrary insertion cannot prove that a newly discovered root does not precede
already acknowledged work and would bypass source-issued frontier evidence.

### Use wall-clock sleep or an async scheduler

Rejected because bounded historical replay needs deterministic virtual time, not elapsed-process
time, concurrency timing, or platform scheduling.

### Adopt a third-party event engine

Rejected because the accepted EA root, authority, cursor, audit, and pipeline contracts remain the
normative semantics. A broad engine would add another owner and substantial integration surface
without replacing the project-specific proof obligations.
