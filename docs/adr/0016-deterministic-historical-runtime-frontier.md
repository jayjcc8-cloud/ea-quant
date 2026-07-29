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
- a consumer-owned historical source port and incremental market-root producer;
- run-wide current-candidate and next-time arbitration;
- one run-wide root dispatcher that is not owned by any producer;
- the continuous run-scoped dispatch sequence;
- one exact live dispatch lease at a time;
- acknowledgement of that lease; and
- the authority to request commit of the matching opaque source candidate only after
  acknowledgement preflight.

`ea.data` continues to own source payload storage, visibility, concrete
`Phase1HistoricalMarketDataSource`, concrete source cursors, and a bridge implementing the
runtime-owned port. `ea.core` continues to own canonical roots, root keys, and dependency-neutral
values. `ea.runtime` imports no `ea.data` module or concrete adapter type. The outer composition
root validates and wraps the exact Phase 1 source before passing only the structural capability
inward.

The concrete source, mutable clock authority, source cursor, opaque source token, producer state,
and dispatcher state are never supplied to strategy, feature, portfolio, risk, execution, matcher,
or result code. A later coordinator consumes the run-wide dispatch lease and read-only clock; it
does not receive a market-owned sequence or queue.

### Runtime-owned source port and outer bridge

`ea.runtime` defines a structural `HistoricalMarketSourcePort` whose exact operation contract is:

```python
source.binding -> HistoricalMarketSourceBinding
source.next_available_at() -> datetime | None
source.admit_one(*, clock: Clock) -> HistoricalMarketCandidate
source.prepare_commit(
    candidate: HistoricalMarketCandidate,
) -> HistoricalMarketPreparedCommit
source.commit(prepared: HistoricalMarketPreparedCommit) -> None
```

The names above are normative. The port and its carrier protocols live in `ea.runtime`, depend
only on `ea.core`, and expose no concrete cursor type. `HistoricalMarketSourceBinding` is an exact
frozen runtime value containing the literal profile `ea-phase1-ohlcv-csv-v1`, `ReplayWindow`, and
`DataFingerprint`.

`HistoricalMarketCandidate` is an opaque source-issued object. Its public read-only values are one
exact `MarketDataEnvelope`, `scheduled_at`, and `cursor_as_of`; its private token/cursor cannot be
read or constructed by runtime. `HistoricalMarketPreparedCommit` is another opaque, single-use
source-issued object bound by object identity to that exact candidate and candidate cursor. A
source-port implementation must reject a foreign, forged, stale, repeated, or non-current
candidate/prepared commit before mutation.

`next_available_at` and `admit_one` operate from the bridge's private committed cursor.
`admit_one` invokes the ADR 0015 source with `limit=1`, validates the returned concrete cursor and
event, and retains the candidate cursor privately. It does not commit it. `prepare_commit`
performs every remaining source-side validation and builds the complete next immutable bridge
state without mutation. `commit` accepts only that exact prepared object and is atomic: a declared
failure occurs before mutation; after all preconditions pass, installing the already-built
in-memory state cannot raise a declared failure.

While one candidate is uncommitted, `next_available_at` returns that candidate's exact
`scheduled_at` without calling the concrete source, and `admit_one` returns the exact same candidate
object after reading `clock.now()` once and rechecking the equality contract. The bridge cannot
admit a second event, replace the candidate, or advance its private committed cursor until exact
prepared commit succeeds.

The concrete `ea.data` bridge factory accepts only an exact
`Phase1HistoricalMarketDataSource`. It binds the source's replay window and fingerprint, owns the
concrete committed/candidate cursor state, and implements this port. The outer composition root is
the only production location importing both the bridge and runtime factory. Passing the concrete
source directly to runtime is forbidden.

### Factory-only single-use runtime

The normative inner factory is:

```python
create_phase1_historical_market_runtime(
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    source: HistoricalMarketSourcePort,
) -> Phase1HistoricalMarketRuntime
```

Direct construction of the runtime, its market producer, dispatcher, dispatch lease, or clock
raises `TypeError`. The factory requires exact canonical `run_id` and `spec_set` values plus the
complete port shape and exact binding field values. It does not accept a concrete data adapter,
raw event iterable, cursor, replay window override, clock, initial dispatch sequence, end root,
producer namespace, or future iterator.

The factory:

1. binds the frontier to the exact `run_id`, instrument-spec set identity and digest, source
   replay window, source semantic fingerprint, and fixed Phase 1 profile;
2. requires `source.binding.data_fingerprint.record_count + 1 <= 2**64 - 1`, reserving one
   dispatch identity per selected market event plus the terminal root;
3. initializes the historical producer with no candidate and zero acknowledged market events;
4. initializes the virtual clock to `source.binding.replay_window.start_inclusive`;
5. fixes the end producer namespace to `runtime.phase1.historical` and its producer sequence to
   zero; and
6. creates one run-wide dispatcher whose next dispatch sequence is one and whose only enabled root
   producer in this Issue is the historical market producer.

It calls no source operation during construction. The runtime is side-effect free apart from
allocating in-memory state and reading immutable binding properties. It cannot be rebound, reset,
rewound, cloned, restarted, or reused for another run.

### Read-only virtual clock

The issued clock satisfies the existing `Clock` protocol:

```python
clock.now() -> datetime
```

`now()` returns the exact current canonical UTC value and has no side effect. It never reads wall
time. The public clock has no `advance`, `set`, `tick`, `sleep`, iterator, callback, scheduler, or
mutable attribute.

Only the bound run-wide runtime owns the private in-process authority needed to advance the clock.
Advancement accepts one exact canonical UTC target:

- a target earlier than the current value is rejected before mutation;
- a target equal to the current value is a valid no-op;
- a later target replaces the current value exactly; and
- a target at or after replay-window end is allowed only for the canonical terminal transition
  defined below.

No event source, source bridge, root producer, dispatcher, or downstream consumer can advance the
clock. The run-wide runtime advances it only between dispatch units. Issue #53 has one scheduling
producer, but a later multi-producer coordinator must first drain every current candidate and then
compare every non-exhausted producer's next-time metadata before advancing the same clock to the
minimum. The historical producer can never advance time independently of that arbitration.

### One-event candidate frontier

The historical producer deliberately requests one market event per opaque candidate. This makes
source progress and one market-root acknowledgement one transaction and removes partial-batch
commit ambiguity.

When no candidate, live lease, or acknowledged terminal is present, preparing the next root does
exactly:

1. call `source.next_available_at()` once;
2. if a timestamp is returned, require it to be canonical UTC, not earlier than `clock.now()`, and
   strictly earlier than `replay_window.end_exclusive`;
3. require the run-wide next-time arbiter to select that timestamp, then advance the private clock
   authority to it;
4. call `source.admit_one(clock=read_only_clock)` once;
5. require one opaque candidate with one exact `MarketDataEnvelope` and exact UTC
   `scheduled_at`/`cursor_as_of`;
6. require the invariant
   `scheduled_timestamp == candidate.scheduled_at == candidate.event.available_at ==
   candidate.cursor_as_of == clock.now()`;
7. require the event's complete runtime root key to be strictly greater than the last
   acknowledged market-root key, when one exists;
8. pass only that event through `prepare_bounded_runtime_roots`; and
9. retain the sealed one-root plan and opaque candidate privately without committing source
   progress or allocating a dispatch sequence.

The bridge converts ADR 0015's empty or multi-event result, cursor/event mismatch, or cursor cutoff
mismatch into a port-contract conflict before returning a candidate. Runtime rejects a delayed
wake, future event, time mismatch, non-monotone root key, or invalid plan. The candidate is not
published by runtime and no source commit or dispatch allocation occurs. A bridge that has already
created its opaque uncommitted candidate may retain only that same candidate; it cannot admit or
commit a later event after runtime rejection.

When the next source event shares the current availability timestamp, step 3 is a no-op and the
next canonical event is drained through another one-event transaction. Consequently equal-time
events retain the full ADR 0004/0008 order without batching or cursor ambiguity.

The market producer returns only its current root to the run-wide dispatcher. It exposes no public
payload-peek operation to downstream code and cannot request a later source candidate until the
current candidate is committed.

### One run-wide dispatcher

The historical producer never assigns a dispatch sequence or issues a lease. One run-wide
dispatcher is the sole authority for all root domains. Before a `pop`, the later full coordinator
must obtain the current candidate and lower-bound proof from every enabled root producer, compare
all complete ADR 0008 keys, and ask this dispatcher to lease only the minimum. The enabled producer
set is fixed before the first `pop`; it cannot be changed mid-run.

The internal consumer-owned producer contract returns exactly one of:

- a sealed `RuntimeRootOffer` containing that producer's least unacknowledged root, opaque commit
  token, and complete root key;
- a canonical next-availability timestamp proving that producer has no root before that time; or
- an exact exhausted proof.

An offer is retained until its exact lease is acknowledged. A producer cannot replace an offer or
later emit a root whose key precedes its last acknowledged key. The dispatcher refuses `pop`
unless every enabled producer has supplied an offer or an exhausted proof at the current clock.
If no offer exists, the run-wide time arbiter compares every next-availability timestamp, advances
to the minimum, asks every producer at that timestamp to prepare its current offer, and repeats
selection. This prevents the market producer from advancing past a current safety/fact/timer root
or from assigning the global order itself.

Issue #53 implements the dispatcher seam and configures exactly one producer: historical market
data plus its terminal root. Later lifecycle/fact/reconciliation/timer Issues must register their
producers with this same run-wide dispatcher before the run starts. They may not create another
sequence, lease, queue, or market-specific dispatcher. This Issue makes no claim that those future
producer frontiers are already implemented.

`pop` returns one factory-issued `RuntimeDispatchLease` for the selected current root and allocates
the current positive uint64 sequence. Exactly one lease may be active. The sequence starts at one
and remains continuous across every producer and frontier; it never restarts at a market boundary.
The producer and its opaque candidate are retained privately with the live lease.

While a lease is active:

- another `pop`, producer advancement, candidate replacement, and clock advancement are rejected;
- only the exact same live lease can be acknowledged;
- no source candidate is committed; and
- the current root, opaque candidate, clock, producer identity, and sequence binding remain stable.

Acknowledging the exact live market lease is one preflighted state transition:

1. verify lease object identity, root object identity, root canonical key, and dispatch sequence;
2. ask the selected producer to validate the retained candidate;
3. call `source.prepare_commit(candidate)` and require an opaque prepared commit for that exact
   candidate, with no source mutation;
4. compute and validate the complete next dispatcher and producer states without mutation;
5. call `source.commit(prepared)`, whose port contract is atomic and already preflighted;
6. install the precomputed producer state, clearing its candidate and increasing committed count;
7. install the precomputed dispatcher state, clearing the live lease while preserving the next
   sequence allocated at `pop`; and
8. retain the acknowledged root key and canonical dispatch evidence.

If any step through 4 fails, no state changes. If step 5 raises, the port contract requires the
bridge to remain unchanged and runtime state also remains unchanged. Steps 6 and 7 are installation
of already-built in-memory values and have no declared failure. A stale, forged, repeated, or
foreign lease/candidate/prepared commit cannot acknowledge a root or commit source progress.

The acknowledgement means that the later full runtime coordinator has completed every consequence
it owns for the dispatch unit. Issue #53 does not define or call those consequences. If a consumer
callback fails, it must not acknowledge; the live lease remains active and no later source payload
can be admitted. Stop/failure abandonment and audited safety drain remain a later lifecycle issue.

### Incremental queue clarification

ADR 0009's factory-only `BoundedRuntimeRootPlan` remains unchanged. A plan is still immutable,
non-empty, completely validated, and canonically sorted. Issue #53 permits the historical producer
to offer sealed one-root plans to the one run-wide dispatcher.

This is not a public mutable priority queue:

- market replenishment is private to the factory-issued historical producer;
- it occurs only with no market candidate or live lease;
- every replenished market key must be strictly greater than the last acknowledged market key;
- the opaque source candidate proves which market root is next;
- the dispatcher selects across every enabled producer rather than trusting market order alone;
- only the fixed terminal root may follow source exhaustion; and
- no arbitrary insertion, removal, cancellation, reschedule, seek, or rewind API exists.

The existing bounded `DeterministicRootQueue` API may remain for callers that already possess a
complete sealed plan. Implementation may extract shared private dispatch-state logic, but it must
not weaken the existing constructor seal, fact-ingress issuance checks, or exact acknowledgement
contract.

This ADR supersedes ADR 0009 only to authorize sealed current-root offers from a fixed producer set
to one run-wide dispatcher. It does not authorize a generic growing-source frontier, late producer
registration, or public mutable queue.

### Exhaustion and the terminal root

When `source.next_available_at()` returns `None`, the historical producer:

1. requires at least one market candidate to have been committed, because ADR 0015 selections are
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
5. offers it through the same sealed one-root plan to the run-wide dispatcher.

The terminal root has no source candidate and performs no source prepare/commit call.
Acknowledging its exact run-wide lease marks the historical producer completed.
After completion, `peek` and `pop` deterministically report exhaustion, `acknowledge` rejects every
lease, `clock.now()` remains `end_exclusive`, and the source is never called again. Repeated
preparation before terminal `pop` or acknowledgement returns the same retained terminal object;
it cannot create a second terminal root.

The replay-window end is chosen instead of the last event time so the terminal root is strictly
later than every selected event (`available_at < end_exclusive`) and has a stable value independent
of file row order or wall time.

### Observable state and evidence

The runtime exposes only:

- exact run and spec-set binding;
- read-only `clock`;
- current committed-event count;
- current dispatcher `next_dispatch_sequence`;
- whether a lease is active;
- whether the terminal root is acknowledged; and
- current-root `peek`, `pop`, and exact-lease `acknowledge` operations.

It does not expose a concrete source, source port, source cursor, opaque candidate/token, root
plan, future count, collection,
iterator, replay window mutation, or acknowledged payload history. A minimal immutable snapshot
may expose canonical scalar evidence needed by audit/tests: run ID, source fingerprint, replay
window, clock value, committed event count, last committed market key encoding or digest, next
dispatch sequence, active dispatch identity, and terminal state. It must not expose a future
payload.

The normative trace schema name is `ea.phase1-historical-runtime-trace.v1`. Each acknowledged unit
is encoded as one compact, sorted-key, ASCII JSON object with exactly:

```json
{
  "clock_now": "YYYY-MM-DDTHH:MM:SS.ffffffZ",
  "committed_cursor_as_of": "YYYY-MM-DDTHH:MM:SS.ffffffZ or null",
  "committed_event_count": 1,
  "data_sha256": "64 lowercase hex",
  "dispatch_sequence": 1,
  "root": {},
  "root_order_key": [],
  "run_id": "canonical UUID4",
  "schema": "ea.phase1-historical-runtime-trace.v1",
  "terminal_acknowledged": false
}
```

UTC values use exactly six fractional digits and `Z`. Integers are JSON integers. For a market
root, `root` is the JSON object obtained by decoding the exact existing
`canonical_market_data_record_bytes(event)`; no field is added or removed. For the terminal root,
`root` is exactly:

```json
{
  "available_at": "YYYY-MM-DDTHH:MM:SS.ffffffZ",
  "kind": "bounded_source_exhausted",
  "producer_namespace": "runtime.phase1.historical",
  "producer_sequence": 0,
  "run_id": "canonical UUID4",
  "type": "end_of_run"
}
```

`root_order_key` is a JSON list of the exact `RuntimeRootOrderKey.as_tuple()` fields after replacing
every datetime with the same canonical UTC string. A market list therefore has exactly twelve
items:

```text
[available_at, 30, event_time, 0, source, source_sequence, venue, symbol,
 interval_start, interval_end, adjustment, revision]
```

The terminal list has exactly six items:

```text
[available_at, 50, 0, producer_namespace, producer_sequence, run_id]
```

`committed_cursor_as_of` equals `clock_now` for an acknowledged market root and is null for the
terminal record. JSON bytes use UTF-8, `ensure_ascii=True`, `allow_nan=False`, `sort_keys=True`,
and separators `(",", ":")`, followed by no newline. A complete trace digest starts with
`b"ea.phase1-historical-runtime-trace.v1\0"` and appends each record as unsigned uint64
big-endian byte length plus record bytes, then an unsigned uint64 record count.

No Python `repr`, object address, hash randomization, locale, filesystem path, wall time, exception
message, alternative terminal encoding, or unspecified root serializer enters the evidence.

### Failures and atomicity

The first matching row in each operation is normative.

#### Outer bridge

| Ordered condition | Failure/result | Allowed mutation |
|---|---|---|
| bridge factory receives a non-exact `Phase1HistoricalMarketDataSource` | `HistoricalMarketDataError(invalid_type)` | none |
| exact source has a malformed binding despite its factory seal | `HistoricalMarketDataError(invalid_dataset)` | none |
| valid source | create bridge with null committed cursor and no candidate | new bridge |
| `next_available_at` with no candidate invokes concrete source and it raises | propagate unchanged | none |
| `next_available_at` with a candidate | return retained `scheduled_at`; no source call | none |
| `admit_one` with no candidate invokes concrete `admit(limit=1)` and it raises | propagate unchanged | none |
| concrete admission returns empty/multiple events or cursor/event/cutoff conflict | `RuntimeOrderingError(validation.conflicting_id)` | none |
| valid concrete admission | issue and retain one opaque candidate | uncommitted candidate only |
| `admit_one` with a candidate and clock read raises | propagate unchanged | none |
| `admit_one` with a candidate and clock differs from its schedule | `RuntimeOrderingError(validation.conflicting_id)` | none |
| `admit_one` with matching clock | return exact retained candidate; no source call | none |
| wrong prepared-commit carrier | `RuntimeOrderingError(validation.invalid_type)` | none |
| stale/foreign/repeated candidate or prepared commit | `RuntimeOrderingError(validation.conflicting_id)` | none |
| exact `prepare_commit` | return exact prepared object with complete next state | none |
| exact `commit` | atomically install prepared cursor and clear candidate | committed cursor |

#### Factory

| Condition | Failure | Published state |
|---|---|---|
| wrong exact `run_id`/`spec_set`, incomplete port shape, or wrong binding field type/profile | `RuntimeOrderingError(validation.invalid_type)` | none |
| invalid replay window/fingerprint value already contradicts its exact core type | original core constructor defect propagates | none |
| source binding cannot reserve `record_count + 1` positive uint64 identities, or run/spec/binding conflicts | `RuntimeOrderingError(validation.conflicting_id)` | none |
| valid input | construct runtime without calling a source operation | new runtime |

#### Candidate preparation

| Ordered condition | Failure | Allowed mutation |
|---|---|---|
| runtime completed, lease active, or dispatcher/producer state structurally invalid | `RuntimeOrderingError(validation.conflicting_id)` | none |
| retained candidate exists | return that exact candidate | none |
| `next_available_at()` raises declared `HistoricalMarketDataError` or another exception | propagate unchanged | none |
| returned value is neither `None` nor exact datetime/UTC | `RuntimeOrderingError(validation.invalid_type)` | none |
| returned timestamp is earlier than clock or not before replay end | `RuntimeOrderingError(validation.out_of_range)` | none |
| valid timestamp | advance clock to it | clock only |
| `admit_one(clock=clock)` raises declared `HistoricalMarketDataError` or another exception | propagate unchanged | advanced clock remains |
| candidate carrier/property has wrong type or shape | `RuntimeOrderingError(validation.invalid_type)` | advanced clock remains |
| candidate/event/schedule/cursor-as-of/clock equality fails, candidate repeats or does not increase the prior market key, or sealed-plan validation conflicts | `RuntimeOrderingError(validation.conflicting_id)` | advanced clock remains; bridge may retain only the rejected uncommitted candidate |
| all checks pass | retain exact candidate and sealed root offer | candidate publication |

An exception raised by source/port code is never reclassified by message text. In particular an
exception from executing ADR 0015 `clock.now()` propagates unchanged through the bridge. A bridge
may translate the ADR 0015 *declared empty/multiple/cursor mismatch result* into its own explicit
`RuntimeOrderingError(validation.conflicting_id)` only after the concrete source call returns; it
does not recast unexpected implementation exceptions as data failures.

#### Pop and sequence state

| State/action | `next_dispatch_sequence` | Active sequence | Result |
|---|---:|---:|---|
| constructed/candidate pending before first pop | 1 | null | no allocation |
| valid `pop` at sequence `n < 2**64-1` | `n + 1` | `n` | exact live lease |
| valid `pop` at sequence `2**64-1` | null | `2**64-1` | exact live lease |
| another pop or preparation while active | unchanged | unchanged | `validation.conflicting_id` |
| failed/stale/foreign acknowledgement | unchanged | unchanged | `validation.invalid_type` for wrong carrier, otherwise `validation.conflicting_id` |
| successful acknowledgement | unchanged | null | exact root and source/producer progress committed |
| pop with null next sequence | null | null | `validation.out_of_range` |
| pop after completed terminal | unchanged | null | `validation.out_of_range` |

The factory reservation check makes uint64 exhaustion unreachable before the terminal root for a
valid ADR 0015 selection, but the run-wide dispatcher state remains closed at the boundary.
Sequence zero is never issued. This table deliberately matches the current queue's advancement at
`pop`, not at acknowledgement.

The complete Issue #53 state transitions are:

| State | Producer offer | Active lease | Source progress | Clock | Allowed successful transition |
|---|---|---|---|---|---|
| `open_idle` | none | none | last committed candidate or null | start or last schedule | prepare current market offer or terminal |
| `market_pending` | exact market offer | none | unchanged | event `available_at` | pop exact minimum offer |
| `market_active` | same market offer | exact lease | unchanged | unchanged | acknowledge exact lease and commit exact candidate |
| `terminal_pending` | exact terminal offer | none | final market committed | replay end | pop terminal |
| `terminal_active` | same terminal offer | exact lease | final market committed | replay end | acknowledge exact lease |
| `completed` | none | none | final market committed | replay end | none |

Every failure leaves the row unchanged except the explicitly documented pre-admission clock
advance and possible retained uncommitted bridge candidate. No transition skips from
`market_pending`/`market_active` to a later market or terminal state.

#### Acknowledgement and terminal preparation

| Ordered condition | Failure | Allowed mutation |
|---|---|---|
| wrong lease carrier | `RuntimeOrderingError(validation.invalid_type)` | none |
| no active lease or identity/root/key/sequence/producer mismatch | `RuntimeOrderingError(validation.conflicting_id)` | none |
| producer validation or `prepare_commit` raises | propagate declared `RuntimeOrderingError`, `HistoricalMarketDataError`, or unexpected exception unchanged | none |
| prepared carrier does not bind the exact candidate | `RuntimeOrderingError(validation.conflicting_id)` | none |
| atomic `source.commit(prepared)` raises | propagate unchanged; port contract requires no source mutation | none |
| market commit succeeds | install precomputed producer and dispatcher states | source cursor, committed count/key, live lease, trace |
| `next_available_at()` returns `None` before any committed event | `RuntimeOrderingError(validation.conflicting_id)` | none |
| valid source exhaustion | advance clock to replay end and retain one terminal offer | clock and terminal candidate |
| exact terminal acknowledgement | install completed producer/dispatcher state; no source call | live lease, terminal flag, trace |

Candidate preparation is atomic for source progress, root offer, and dispatch allocation. Clock
advancement occurs before source admission because visibility requires the advanced time; if
admission then fails, the clock remains at that exact scheduling timestamp, but no candidate or
cursor is committed. Repeating preparation uses the same bridge-owned committed cursor and clock,
so the source must replay the same event/failure. The clock cannot regress to conceal the attempt.

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
- configure every root producer before the first pop and use the same run-wide time arbiter,
  dispatcher, lease, and continuous sequence;
- call run-wide dispatch acknowledgement only after all consequences of the current unit succeed;
- add mandatory inbound and outbound audit acknowledgement gates;
- implement stop/failure cutover, audited abandonment, and safety drain;
- integrate strategy, portfolio, risk, OMS, matcher, ledger, reconciliation, and result adapters;
  and
- preserve this runtime's clock arbitration, global ordering, dispatch, and opaque source-commit
  transaction.

## Verification

Implementation requires:

- unit tests for factory seals, exact types, clock initialization/read/advance authority,
  same-time no-op, regression, and replay-window bounds;
- unit and property tests for one-event admission, equal-time events, revisions, canonical order,
  candidate replay, live-lease exclusion, exact acknowledgement, stale/forged/foreign leases,
  cursor commit, prepared-commit atomicity, and failure atomicity;
- dispatcher contract tests with multiple fake root producers proving fixed registration,
  current-root minimum selection, next-time minimum selection, and one global sequence;
- sequence tests spanning multiple producers/frontiers, uint64 edge behavior, and proof that
  zero/restart is impossible;
- exhaustion tests proving one terminal root at exact replay end and no later source call;
- malicious port/source tests for empty/multiple admission, cursor mismatch, delayed wake,
  `scheduled_at != event.available_at`, future event, clock mismatch, non-monotone key, forged
  plan/candidate/prepared commit, and unexpected exceptions;
- cross-process byte-golden tests proving the exact versioned trace record and digest for
  clock/root/dispatch/source-commit/terminal evidence;
- import-boundary tests proving `ea.runtime` does not import `ea.data`, concrete adapters remain
  outer, and `core`/policy packages do not depend on runtime/data adapters;
- full lint, strict typing, tests, coverage, and independent exact-SHA verification; and
- README and architecture updates that state this slice is not the complete runtime or backtest.

## Consequences

- A bounded source can offer scheduling metadata and current market roots without exposing its
  future payloads or owning time/dispatch.
- Source cursor commit and one market-root acknowledgement become the same deterministic
  transaction.
- One run-wide authority can preserve dispatch identities across market replenishment and later
  root producers.
- Equal-time events are drained in canonical order without partial-batch ambiguity.
- The terminal timestamp and root are deterministic and independent of wall time.
- The design intentionally pays one source admission call per market root; Phase 1 favors clear
  correctness over batching throughput.
- The complete lifecycle, audit gates, policy chain, matcher, ledger integration, and results
  remain explicit later work.

## Rejected alternatives

### Pass the concrete Phase 1 source into `ea.runtime`

Rejected because the mode-neutral kernel would depend on an outer `ea.data` adapter and concrete
cursor type. The runtime-owned structural port plus outer bridge preserves ADR 0003 dependency
direction while retaining exact source validation.

### Let the historical frontier own dispatch leases and sequence

Rejected because safety, execution-fact, reconciliation, and timer roots must share one ADR 0008
global order. A market producer offers its current root and opaque commit token; the run-wide
dispatcher alone compares producers and assigns the lease/sequence.

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
