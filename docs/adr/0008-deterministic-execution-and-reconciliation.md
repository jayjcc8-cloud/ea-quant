# ADR 0008: Deterministic Execution and Reconciliation Contract

Date: 2026-07-24

## Status

Accepted

## Context

Accepted ADR 0003 defines one mode-neutral path from strategy through portfolio, risk, shared
execution/OMS, audit, and the canonical portfolio ledger. ADR 0004 freezes point-in-time market
visibility and deterministic market admission. ADR 0005 supplies one immutable configuration
snapshot, and ADR 0006 binds a bounded Phase 1 backtest to clean code, normalized configuration,
point-in-time data, runtime evidence, effective parameters, and seeded randomness.

Those decisions deliberately do not define an executable order vocabulary, economic
quantization, order and submission state, duplicate fact behavior, uncertain submission,
matching, ledger application, or reconciliation authority. Without one contract, a backtest could
fill against the same bar that caused a decision, retry an order after an ambiguous timeout,
silently round economics, apply the same trade twice, or overwrite its ledger from a broker
snapshot. Those are correctness and future live-safety failures, not adapter details.

[Issue #15](https://github.com/jayjcc8-cloud/ea-quant/issues/15) owns this contract. This ADR freezes
the vocabulary and invariants required before a runtime kernel, matcher, OMS, ledger, risk layer,
or strategy is implemented. It does not claim that any of those components currently exists.

## Scope

This decision defines:

- singular ownership and dependency direction for execution messages and ports;
- the canonical `OrderIntent -> RiskDecision -> Order -> ExecutionFact -> Fill` path;
- identifiers, correlation, canonical bytes, and idempotency;
- exact decimal, instrument-grid, and accounting rules;
- global root ordering and Phase 1 no-look-ahead matching;
- independent risk, audit/submission, order, fact, uncertainty, and reconciliation state;
- submission ambiguity and the prohibition of blind retry;
- canonical ledger authority and reconciliation precedence;
- stable machine-readable outcome codes;
- the POSIX v1 platform boundary; and
- evidence required from later implementation Issues.

## Non-goals

This ADR does not implement:

- a runtime kernel, historical feed, matcher, OMS, ledger, portfolio, risk engine, strategy, or
  broker adapter;
- live-trading enablement, credentials, secret resolution, or a real external order path;
- venue-specific order types, calendars, margin, corporate actions, or settlement;
- cancellation, amendment, stop/limit behavior, volume participation, or a partial-fill model for
  the Phase 1 matcher;
- non-zero fees, rebates, slippage, latency, liquidity, or market impact;
- Windows durability equivalence; or
- a new dependency, manifest schema, release, or deployment.

## Reuse decision

The bounded review inspected QuantConnect LEAN, NautilusTrader, and vn.py at the references
recorded in Issue #15.

- LEAN provides mature transaction, brokerage, fill-model, and order-event patterns, but direct
  reuse imports a large C#/.NET engine and its ownership model.
- NautilusTrader provides strong execution, state-machine, reconciliation, and backtest/live
  parity patterns, but direct reuse imports a broad Rust/Python runtime, native build closure, and
  LGPL distribution obligations.
- vn.py provides useful gateway, event-engine, OMS, order/trade normalization, and risk-routing
  patterns, but its application and gateway ownership is broader than this local contract.

The decision is to reuse their semantics and failure scenarios as references, not as runtime
dependencies. The project will define a small local standard-library-first contract. A future
adapter may wrap an external capability only after proving that it implements this contract
without exporting vendor state or changing ownership.

## Decision

### Singular ownership and dependency direction

Ownership is fixed:

| Owner | Owns | Must not own |
|---|---|---|
| Portfolio | `OrderIntent`, account, position, cash, immutable snapshots, canonical append-only ledger | venue state, order state, risk approval |
| Risk | `RiskDecision`, limits, halt state, risk-state version, approval proof | ledger, venue submission, order/fill creation |
| Execution/OMS | `Order`, canonical execution request, order/fact state, `Fill`, correlation, deduplication, reconciliation classification | portfolio ledger, risk policy, audit persistence |
| Runtime | root admission, total ordering, serialized dispatch, lifecycle, audit gates, safety drain | venue protocol, ledger, order-policy semantics |
| Venue adapter | vendor encoding and secret-free canonical `ExecutionFact` observations | canonical `Fill`, ledger mutation, risk mutation, silent economic adjustment |
| Composition | construction and binding of inner components to concrete adapters | domain decisions or runtime state |

The dependency graph is acyclic:

```text
focused ea.core immutable values
        ^
strategy / portfolio / risk / execution public contracts
        ^
mode-neutral runtime kernel
        ^
outer composition root

concrete adapters --implement--> consumer-owned inner ports
```

Future dependency-neutral execution values belong in a focused module such as
`ea.core.execution`; they do not accumulate in `ea.core.models`. Callable OMS and venue protocols
belong to future `ea.execution`. Coordination, clock, event-source, audit, and result protocols
belong to future `ea.runtime`. Execution owns venue submission and venue-reconciliation query
ports; runtime does not become a miscellaneous port registry. Composition is the only production
layer that imports both inner packages and concrete adapters.

An AST-based import-boundary test in the first runtime implementation MUST enforce these
directions. `ea.runtime` MUST NOT import `ea.config`, `ea.experiments`, `ea.composition`, a concrete
adapter, or a vendor SDK. Inner execution, portfolio, and risk packages MUST NOT import
composition, configuration loaders, experiments, or adapters.

### Canonical immutable messages

All messages are closed, versioned, immutable values with canonical bytes. Unknown fields or enum
values fail. Human descriptions are non-normative.

#### `OrderIntent`

Portfolio alone creates an `OrderIntent`. It contains:

- `run_id`, `intent_id`, `correlation_id`, and known `causation_id`;
- canonical instrument identity;
- side, strictly positive quantity, order kind, and time in force;
- an optional price constraint when the selected order kind requires one;
- the portfolio snapshot version and original target lineage; and
- the instrument-spec-set and execution-policy identifiers.

V1 Phase 1 accepts only `market` plus `good_for_next_eligible_market_event`. Other values are
reserved and rejected by the Phase 1 profile.

#### `RiskDecision`

Risk returns exactly one variant:

- `allow`: approved economics equal the original intent;
- `resize`: approved quantity is positive, quantized, no greater than the original quantity, and
  does not change instrument, side, order kind, or time in force;
- `reject`: no executable approval exists; or
- `evaluation_failed`: no executable approval exists and the runtime enters the failure path.

Every decision binds the exact canonical intent digest, portfolio snapshot version, risk-state
version, decision ID, stable reason code, and current serialized dispatch unit. Only `allow` and
`resize` carry a one-time `ExecutionApproval`.

#### `Order`

Execution/OMS alone consumes one valid approval and creates one `Order`. The Order preserves the
original and effective intent digests, approval identity, portfolio/risk versions, canonical
economics, and a stable client submission key. A second consumption attempt is an exact replay or
a conflict; it never creates another Order.

Execution rechecks the global halt, approval binding, portfolio/risk versions, persisted
pre-effect audit acknowledgement, and canonical request digest immediately before the venue call.
Any intervening economic or state-version change returns through portfolio/risk and requires a new
pre-effect audit acknowledgement. Adapter wire encoding cannot change economics.

#### `ExecutionFactIngress` and `ExecutionFact`

A venue or simulator adapter produces a secret-free `ExecutionFactIngress` root containing:

- `available_at`;
- a canonical source namespace;
- an exact non-negative `ingress_sequence`, unique for every delivery occurrence in that source
  namespace; and
- one immutable `ExecutionFact`.

The ingress sequence comes from a stable upstream delivery sequence or is assigned at one
serialized adapter receive boundary before parallel decoding. Scheduler completion cannot assign
it. A redelivery receives a new ingress sequence even when it carries the same economic fact.
Ingress identity is `(source_namespace, ingress_sequence)` and is validated for uniqueness before
root sorting.

The nested `ExecutionFact` contains:

- the same source namespace and one stable deduplication identity;
- fact kind and `occurred_at`;
- canonical instrument identity when known;
- every truly known client, venue, order, correlation, and causation identifier;
- canonical economic payload and stable source provenance; and
- `fact_sha256`, a domain-separated digest of the other canonical fact fields and excluded from
  its own preimage.

Its source namespace MUST equal the enclosing ingress namespace; a mismatch returns
`fact.invalid` before deduplication.

The deduplication identity is a closed tagged union:

```text
("external_id", canonical_non_empty_ascii_external_id)
("source_native_sequence", exact_non_negative_integer_stable_across_redelivery)
```

Transport receipt time, ingress sequence, `available_at`, socket/session metadata, and retry count
belong only to the ingress envelope and are excluded from canonical fact bytes. When a source
cannot provide either stable fact identity, the ingress is retained and returns
`fact.invalid.missing_dedup_identity`; it cannot create a Fill or mutate the ledger. Hashing the
economic payload is not a substitute because conflict detection requires a stable identity
independent of content.

Missing ancestry remains absent. It is never fabricated. Supported fact kinds include venue
acknowledgement, rejection, trade, expiry, cancellation, and explicit submission-query evidence.
A vendor object never crosses the adapter.

#### `Fill`

Execution/OMS alone creates a canonical `Fill` from an accepted trade fact. A Fill preserves the
fact identity and provenance, instrument, side, exact quantity, exact price, exact fee entries,
and every truly known order/correlation identifier. A complete real trade fact can produce a Fill
even when order ancestry is unresolved. The unresolved correlation opens a reconciliation halt;
it does not justify discarding economic reality.

### Identifiers, correlation, and canonical identity

Owner-created economic IDs are tuples of:

```text
(run_id, owner_kind, unsigned_64_bit_owner_sequence)
```

The owner sequence is allocated in serialized causal order and persisted before any effect that
uses it. Random UUIDs, wall time, Python hashes, container iteration, scheduler completion, and
opaque vendor identifiers never select economic order. `run_id` scopes identity but does not
break an ordering tie.

Strategy-originated chains preserve one `correlation_id`; each child records its immediate
`causation_id`. External facts record only known ancestry. The client submission key is the
domain-separated SHA-256 of canonical Order bytes and is stable across an exact retry or
reconstruction.

Every canonical type defines one versioned byte encoding and content digest before implementation.
Same typed ID plus identical canonical bytes means exact replay. Same typed ID or deduplication key
plus different canonical bytes means conflict.

### Exact Decimal and instrument quantization

All order prices, quantities, fees, cash values, positions, risk limits, and ledger amounts use
finite `Decimal` values. Construction from `float`, `Decimal(float)`, NaN, infinity, signed zero,
exponent notation, implicit coercion, or ambient decimal context is forbidden.

`ea-decimal-v1` canonical text uses:

```text
0|-?(?:[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9])
```

The grammar itself excludes negative zero. A leading plus, unnecessary leading zeroes, and
trailing fractional zeroes fail. A value has at most 38 significant digits, at most 20 integer
digits, and at most 18 fractional digits. Parsing and validation use an explicit local context
with traps. Economic arithmetic converts canonical values to sign, integer coefficient, and scale
and uses exact unbounded integer operations; it does not multiply under a fixed-precision Decimal
context. Ambient context changes cannot affect results. Rounding is permitted only at the named
settlement boundary below.

Every tradable instrument has one immutable execution specification:

- `price_quantum`;
- `quantity_quantum`;
- settlement currency and `currency_quantum`;
- contract multiplier; and
- price domain, exactly `positive`, `non_negative`, or `signed`; and
- immutable specification/version identity.

Price quantum, quantity quantum, currency quantum, quantity, and contract multiplier are strictly
positive. Price is an exact quantum multiple and is accepted only by its lineage-bound price
domain. A negative market observation can become an execution price only for a `signed`
instrument; otherwise it returns `validation.price_domain`. No portfolio, risk, OMS, matcher, or
adapter silently rounds a value. An invalid grid value returns `validation.not_quantized`.

Phase 1 accepts only a versioned, code-defined instrument-spec set selected by
`execution.instrument_spec_set_id` and
`execution.instrument_spec_set_sha256` effective parameters. The clean code commit and digest bind
the complete set under ADR 0006 without changing its closed manifest schema. External or mutable
instrument metadata requires a future Accepted lineage extension.

Notional is computed by exact integer coefficient multiplication and scale addition for
`price * quantity * contract_multiplier`; three valid 38-digit operands therefore cannot round
before settlement. The exact result is divided by `currency_quantum` with integer quotient and
remainder and quantized once using `ROUND_HALF_EVEN`. A result outside the canonical output bounds
returns `validation.arithmetic_overflow` before a ledger mutation. Any non-zero rounding residual
is an explicit balanced ledger posting to a versioned rounding account; it never disappears.
Failure to represent that posting exactly returns `ledger.rounding_unrepresentable` and performs
no partial append. V1 matcher fees are exactly zero with explicit currency and fee code. Rebates
and non-zero fee models require a future versioned policy.

### Deterministic root ordering and causal stages

Runtime admits independent roots by the ascending key:

```text
(
  available_at,
  stable_domain_rank,
  domain_specific_suffix,
)
```

`occurred_at` is provenance, not admission time. Frozen ranks are:

| Rank | Root kind |
|---:|---|
| 0 | safety cutover, halt, or failure |
| 10 | execution fact |
| 20 | reconciliation observation |
| 30 | market data |
| 40 | timer |
| 50 | end of run |

Every suffix is a closed tuple of canonical comparable values:

| Rank | Closed `domain_specific_suffix` |
|---:|---|
| 0 | `(safety_kind_rank, producer_namespace, producer_sequence, subject_kind, subject_id)` |
| 10 | `(fact_kind_rank, source_namespace, ingress_sequence)` |
| 20 | `(observation_kind_rank, source_namespace, source_sequence, watermark_namespace, watermark_sequence, observation_id)` |
| 30 | `(event_time, event_kind_rank, source_code, source_sequence, instrument_venue, instrument_symbol, interval_start, interval_end, adjustment, revision)` |
| 40 | `(timer_kind_rank, timer_namespace, timer_id, producer_sequence)` |
| 50 | `(end_kind_rank, producer_namespace, producer_sequence, run_id)` |

Rank 30 is exactly ADR 0004's accepted market admission key after its leading `available_at`;
source never compares before `event_time`. Frozen local ranks are:

- safety: `halt=0`, `failure_cutover=10`, `stop_cutover=20`;
- fact: `trade=0`, `rejection=10`, `acknowledgement=20`, `expiry=30`,
  `cancellation=40`, `submission_query=50`;
- reconciliation observation: `trade_detail=0`, `order_detail=10`,
  `position_snapshot=20`, `cash_snapshot=30`;
- timer: `safety_deadline=0`, `strategy_timer=10`, `maintenance=20`; and
- end of run: `bounded_source_exhausted=0`, `requested_end=10`.

Namespaces, IDs, subject kinds, and watermark namespaces are canonical non-empty ASCII identifiers;
sequences and local ranks are exact non-negative integers; digests are fixed lowercase SHA-256
text; instrument and market fields use ADR 0004 canonical values; times use its exact UTC
representation. Optional values have exact tagged encodings:

- ASCII text: absent `(0, "")`; present `(1, canonical_non_empty_ascii)`;
- non-negative integer: absent `(0, 0)`; present `(1, exact_non_negative_integer)`;
- subject: absent `(0, "", "")`; present `(1, subject_kind, subject_id)`;
- instrument: absent `(0, "", "")`; present `(1, venue, symbol)`; and
- watermark: absent `(0, "", 0)`; present `(1, namespace, sequence)`.

The absent payload is not a domain value and can occur only under presence rank zero. Missing and
present values therefore remain comparable without fabricating provenance. In addition to full
root-key uniqueness, each domain validates its identity before sorting: market identity follows
ADR 0004, fact-ingress identity is `(source_namespace, ingress_sequence)`, and other domains use
the closed identity fields in their suffix. A repeated ingress identity is a transport collision,
not a fact redelivery.

Adding a root or local kind requires a later Accepted decision assigning its rank and suffix.
Equal complete root keys or duplicate domain ingress identities fail; arrival order, content
digest, stable-sort fallback, mapping iteration, or producer scheduling is never a tie-breaker.
Runtime may assign a dispatch sequence after choosing a root to record applied order, but that
sequence cannot choose among concurrently ready roots.

Causal descendants finish serially inside their parent dispatch unit and are never reinserted
ahead of their cause. For one market root:

1. match only Orders eligible before this root;
2. normalize facts emitted by matching;
3. accept Fills, append balanced ledger entries, and publish the new portfolio/risk view;
4. run strategy, portfolio, and risk against that updated view;
5. obtain audit acknowledgement and submit newly approved Orders; and
6. make those Orders eligible only for a later market root.

Late facts are admitted when `available_at` makes them visible and never rewind earlier decisions.

### Phase 1 matching policy

The first matcher implementation is intentionally narrow:

- market Orders only;
- full fill on the first later eligible initial raw bar for the same instrument;
- an execution opportunity requires `adjustment == raw` and `revision == 0`;
- the Order records `eligible_after_available_at` from its causal root; the candidate root key MUST
  be later and its `event_time` MUST be strictly later than that timestamp;
- correction roots and late initial bars whose event time is not strictly later update admitted
  information but never provide a Phase 1 execution opportunity;
- price is the deterministic quantized next-bar close proxy;
- fee, modeled slippage, and modeled latency are zero;
- no partial fills, cancellation, amendment, volume participation, stop/limit logic, calendar, or
  venue-specific behavior; and
- no later eligible bar produces terminal `order.expired.no_eligible_market_data`, never a
  fabricated Fill.

The recorded close is a binary64 bit pattern under ADR 0004. The matcher obtains its exact rational
value from those bits, divides by the exact price quantum using integer/rational arithmetic, and
selects the nearest tick. An exact half-tick tie is adverse: upward for a buy and downward for a
sell. It does not call `Decimal(float)`.

Fee, slippage, latency, liquidity, cancellation, partial-fill, and venue models are later,
separately versioned policies. Their IDs, materialized parameters, instrument-spec set, and any
RNG stream labels enter ADR 0006 effective lineage. They consume only runtime-admitted
current/past context.

### Independent state machines

One overloaded status enum is forbidden. Risk, audit/submission, order projection, fact
processing, uncertainty, and reconciliation have separate transitions and outcomes.

#### Risk and pre-effect authorization

```text
intent_received
  -> allowed | resized | rejected | evaluation_failed

allowed | resized
  -> audit_pending
  -> audit_authorized | audit_failed | audit_ack_mismatch
```

Only `audit_authorized` can reach submission. Risk rejection is a normal terminal domain outcome.
Evaluation or audit failure enters the runtime failure/halt path with zero venue calls.

#### Submission

```text
created
  -> audit_authorized
  -> submission_pending
  -> submitted | definitely_not_submitted | submission_uncertain
```

The venue port returns only:

- `submission.submitted`;
- `submission.definitely_not_submitted`; or
- `submission.uncertain`.

Any timeout, disconnect, cancellation, crash window, lost response, or exception after an effect
may have occurred is uncertain unless the adapter proves that no effect occurred.

#### Order projection

After definite submission:

```text
submitted
  -> acknowledged | rejected | partially_filled | filled | expired | cancelled

acknowledged
  -> partially_filled | filled | expired | cancelled

partially_filled
  -> partially_filled | filled | expired | cancelled
```

Phase 1 never emits `partially_filled` or `cancelled`, but the canonical projection reserves them
so future real facts are not discarded. Filled, rejected, expired, cancelled, and
definitely-not-submitted projections are terminal. A late real trade after a terminal projection
still produces a fact-processing outcome and, when economically complete, a Fill and ledger
entry. The terminal projection does not reopen; reconciliation records a
`reconciliation.late_fact_after_terminal` anomaly and halts new submissions.

A Fill may prove definite submission before a venue acknowledgement.

### Duplicate, conflict, and restart behavior

Every handler is idempotent:

- exact `OrderIntent` replay returns the original RiskDecision and is not re-risked against newer
  state;
- exact approval replay returns the original Order and never consumes another sequence;
- after any venue-call attempt, exact submission replay returns the persisted original outcome
  with zero venue calls;
- when no definitive outcome was durably recorded, recovery marks the existing request uncertain
  and performs query/reconciliation only; the stable client key is correlation evidence, never
  permission to resubmit;
- after root ordering, Execution/OMS classifies every admitted Fact ingress by
  `(source_namespace, fact_deduplication_identity)`;
- same deduplication identity plus identical canonical `ExecutionFact` bytes returns
  `fact.duplicate` for that ingress and makes no economic or order-state mutation;
- same deduplication identity plus different canonical `ExecutionFact` bytes returns
  `fact.conflict`, halts new submissions, and makes no second mutation;
- exact duplicate Fill or ledger application returns its original no-op outcome;
- exact audit retry uses the same record ID and may return the same bound acknowledgement; and
- result/durability retries use stable record identities.

Ingress envelope fields can legitimately differ across redelivery and do not participate in the
fact duplicate/conflict comparison. Each ingress still receives its own explicit processing
outcome and audit attempt. Classifying an ingress only after runtime admits it in full root-key
order makes the accepted-first and duplicate-later result independent of input container order
without exposing a future root. Same identity or deduplication key with different canonical core
bytes returns a conflict, halts new submissions, performs no conflicting mutation, and fails the
run. An acknowledgement bound to a different digest, sequence, or run is fatal.

Durable recovery reconstructs ID allocation, deduplication, approvals, approval consumption,
order projection, accepted Fills, ledger application, halt state, and outstanding uncertainty
before any effect can resume.

### Uncertain submission and no blind retry

On `submission.uncertain` the system:

1. preserves the original Order and client submission key;
2. halts every new venue submission;
3. creates no replacement Order and infers nothing from a missing local acknowledgement;
4. queries and reconciles by stable client key and known venue identifiers; and
5. resolves only to:
   - `reconciliation.submission.confirmed_submitted`;
   - `reconciliation.submission.confirmed_not_submitted`;
   - `reconciliation.submission.confirmed_rejected`;
   - `reconciliation.submission.confirmed_filled`; or
   - `reconciliation.submission.still_unknown`.

Confirmed-not-submitted may permit a new, explicit attempt only after a fresh intent, risk
evaluation, Order, new client key, and pre-effect audit acknowledgement. The prior Order and key
are never resubmitted. It never triggers automatic retry.
Still-unknown leaves the run failed/incomplete with possible external exposure.

A discovered real Fill is processed during `failing` and even when audit persistence is
unavailable. The system reports missing durability; it does not erase the fact to make the run
appear clean. The in-process Phase 1 simulator normally returns only definite outcomes, but later
failure-injection tests MUST cover every uncertainty branch.

### Ledger authority and reconciliation

The portfolio append-only ledger is the sole canonical authority for account, cash, position, and
P&L state. Only:

- an accepted canonical Fill; or
- an explicit, immutable, separately authorized reconciliation adjustment

may append economic entries. Each entry is balanced, idempotent, and linked to its cause.

Evidence precedence is:

1. stable venue trade/fill detail;
2. venue order detail and summaries;
3. aggregate position/cash snapshots.

Trade detail can enter as an ExecutionFact, normalize to a Fill, pass the inbound audit or
documented failing-safety path, and update the ledger. Order, position, and cash snapshots are
observations. They never overwrite the ledger, fabricate a Fill, or create a silent balancing
entry.

Every reconciliation observation has source identity, source sequence, `available_at`, provenance,
and a comparable watermark. Outcomes include:

- `reconciliation.match`;
- `reconciliation.remote_ahead`;
- `reconciliation.local_ahead_stale`;
- `reconciliation.mismatch`;
- `reconciliation.unresolved_correlation`;
- `reconciliation.submission_unknown`;
- `reconciliation.late_fact_after_terminal`;
- `reconciliation.invalid`; and
- `reconciliation.quarantined`.

A stale or incomparable snapshot cannot clear a halt. Remote-ahead requests missing transaction
facts and then compares again. A same-watermark mismatch halts new submissions and fails closed.
An economically complete external Fill is applied even with unresolved Order correlation; it
remains flagged and halted. Incomplete evidence is retained but cannot mutate the ledger.
Automatic snapshot correction is forbidden. A future correction event requires explicit
authorization, provenance, canonical bytes, idempotency, and balanced ledger postings.

### Stable outcome registry

Logic branches on closed versioned codes, never exception text. V1 reserves:

| Family | Required codes |
|---|---|
| `validation.*` | `invalid_type`, `non_finite`, `out_of_range`, `not_quantized`, `price_domain`, `arithmetic_overflow`, `conflicting_id` |
| `preflight.*` | `unsupported_platform`, `provenance_unverified`, `manifest_unverified` |
| `durability.*` | `audit_append_failed`, `audit_ack_mismatch`, `result_write_failed` |
| `risk.*` | `allowed`, `resized`, `rejected`, `evaluation_failed`, `stale_approval` |
| `submission.*` | `submitted`, `definitely_not_submitted`, `uncertain`, `blocked_by_halt` |
| `fact.*` | `accepted`, `duplicate`, `invalid`, `invalid.missing_dedup_identity`, `conflict`, `unresolved` |
| `order.*` | `acknowledged`, `rejected`, `partially_filled`, `filled`, `expired`, `cancelled`, `expired.no_eligible_market_data` |
| `ledger.*` | `applied`, `duplicate`, `conflict`, `unbalanced`, `rounding_unrepresentable` |
| `reconciliation.*` | the outcomes defined above plus the five `submission.*` resolutions |

Every outcome carries its code, immutable subject identity, correlation/causation when known, and
closed structured details. Free-form human text is optional and non-normative. Persisted audit
acknowledgement and venue acknowledgement are distinct types and codes.

### Platform boundary

Execution v1 supports native CPython when:

```text
os.name == "posix" and sys.platform in {"darwin", "linux"}
```

Windows, Cygwin, and every other platform return `preflight.unsupported_platform` before UUID
generation, result-directory reservation, adapter construction, audit/feed start, or venue
effect. The platform check belongs at composition/preflight, not in portfolio, risk, execution
policy, or ledger code. A future Windows ADR must prove filesystem durability and boundary
equivalence. This restriction does not claim that existing manifest readers or non-execution
utilities are globally unsupported.

## Required implementation evidence

This ADR cannot be treated as implemented until separate Issues provide all evidence below.

### Golden traces

- allow -> audit acknowledgement -> submit -> fact -> Fill -> balanced ledger;
- resize with original/effective intent lineage;
- reject and evaluation failure with zero OMS/venue calls;
- next-bar-close Fill and end-of-run expiry;
- post-order correction and pre-order late initial bar with no execution opportunity;
- equal-availability multi-source market roots in exact ADR 0004 order;
- execution fact before a market decision at equal `available_at`;
- exact duplicate fact and Fill with no repeated mutation;
- two ordered ingress redeliveries of one stable fact, producing one accepted economic application
  and one explicit duplicate outcome;
- conflicting duplicate fail-closed;
- submission replay after a definitive outcome and after missing outcome durability, both with zero
  additional venue calls;
- uncertain submission resolved as submitted, not submitted, filled, and still unknown;
- complete external Fill with unresolved Order correlation; and
- reconciliation match, stale, remote-ahead, and same-watermark mismatch.

### Failure injection

- risk exception;
- pre-effect audit append and acknowledgement failure;
- definite no-submit;
- timeout before, during, and after a possible venue effect;
- lost submission response;
- inbound audit failure while a real Fill still drains;
- ledger apply failure;
- restart between every durable/effect boundary;
- stale or incomparable reconciliation snapshot; and
- result durability failure.

### State-machine and property tests

- only declared transitions are reachable and terminal projections do not reopen;
- cumulative Fill quantity never exceeds Order quantity;
- every accepted Fill changes the ledger exactly once and postings balance;
- independently ordered roots are permutation invariant;
- replay is idempotent and conflicting duplicates fail;
- fact redelivery results are invariant to input order and process reconstruction while each
  ingress keeps an explicit processing outcome;
- after any venue-call attempt, replay and recovery never call submission again;
- every price, quantity, and fee remains on its declared grid;
- maximum coefficient products, signed-price policies, signed zero, overflow, half-tick ties, and
  rounding residuals have stable outcomes independent of ambient Decimal context;
- risk reject/evaluation failure cannot reach submission;
- global halt prevents every new venue call;
- an Order caused by a market root cannot Fill from that root;
- correction roots and bars with `event_time <= eligible_after_available_at` cannot provide a
  Phase 1 execution opportunity; and
- market, matcher, and strategy cannot observe future or unadmitted data.

### Cross-process and platform evidence

Canonical trace bytes and economic outputs are identical across changed `PYTHONHASHSEED`, timezone,
locale, current directory, ambient Decimal context, input-container order, and scheduling
perturbation. The suite runs on macOS and Linux and proves explicit Windows fail-fast. Runtime and
platform fields already permitted by ADR 0006 may differ, but a fixed execution vector has exact
semantics.

## Consequences

- Phase 1 starts with a deliberately small and testable execution model rather than false realism.
- Backtest, paper, and future live modes must share OMS, risk, ledger, and outcome semantics.
- Uncertain submission is a first-class halted state, not an exception-retry policy.
- Broker snapshots cannot silently repair local accounting.
- Exact Decimal and instrument specifications become explicit lineage-bearing inputs.
- Runtime and execution ports remain consumer-owned and testable without concrete adapters.
- More realistic execution models require new versioned policies and evidence rather than hidden
  matcher conditionals.

## Rejected alternatives

### Import a mature engine directly

Rejected for this boundary because the reviewed engines import disproportionate runtime,
licensing, transitive-dependency, operational, and ownership closure. Their patterns remain useful
references.

### One mutable order-status enum

Rejected because risk, audit authorization, possible venue effect, venue observations, local
projection, ledger application, and reconciliation have different evidence and transition
authority.

### Floating-point execution accounting

Rejected because binary64 rounding, ambient conversion, and platform/library differences cannot
support exact order grids or balanced ledger invariants.

### Retry on timeout

Rejected because a timeout does not prove that no external effect occurred. A retry can duplicate
real exposure.

### Make broker snapshots authoritative

Rejected because a mutable observation can be stale, incomplete, or incomparable and cannot
replace transaction evidence or an append-only canonical ledger.

### Allow same-bar close fills

Rejected because a strategy that observes a closed bar cannot submit early enough to fill against
that same bar without look-ahead.

## Design-finding traceability

The Proposed text addresses:

- `ARCH-DESIGN-001` through `ARCH-DESIGN-006`;
- `DOMAIN-DESIGN-001` through `DOMAIN-DESIGN-009`.

Final Architecture and Backtest/Risk reviews must verify each finding against the exact candidate
SHA. Verification must independently check scope, links, terminology, repository gates,
wheel/clean-wheel behavior, and exact-head CI. Changing this ADR from Proposed to Accepted
requires explicit user authorization on the exact reviewed candidate.
