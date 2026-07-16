# ADR 0003: Shared Runtime with Ports and Adapters

Date: 2026-07-15

## Status

Accepted

## Context

ADR 0001 selected a Python-first modular monorepo and required stable seams for data, strategy,
portfolio, risk, execution, brokers, and reporting. The current architecture diagram still shows
a separate path from the canonical data store to a backtest engine and then to audit. If Phase 1
were implemented against that diagram, backtest could acquire its own order, fill, position, and
P&L semantics while paper and live trading followed another path.

The system instead needs one enforceable runtime contract before any mode-specific engine is
built. The contract must preserve deterministic backtests, independent risk control, auditable
decisions, and later substitution of paper or live adapters without allowing strategy code to
know which mode is running.

Once accepted, this ADR freezes responsibilities and dependency direction. It intentionally does not freeze
Python class signatures or decide the market-data, configuration, run-manifest, or matching
schemas tracked by Issues #12 through #15.

## Decision

### One order-producing runtime path

Every workflow that claims orders, fills, positions, P&L, or trading results MUST use one
mode-neutral runtime coordinator and the same logical chain:

`market event -> strategy -> portfolio -> risk -> shared execution/OMS -> venue adapter`

A venue adapter translates each vendor report at its boundary into a canonical, secret-free raw
execution fact and passes it to the shared execution/OMS. Execution/OMS validates, normalizes, and
deduplicates the fact, preserves its true source provenance and known identifiers, and emits an
explicit immutable processing outcome, including accepted, duplicate, invalid/rejected, or
unresolved results. It does not write an audit sink. In the normal path, the runtime waits for
mandatory audit persisted acknowledgement of that outcome before dispatching it to portfolio,
risk, or reconciliation owners. If audit fails, the runtime enters the explicit `failing` safety
path and still does not discard the real external fact.

Domain owners update their own state and emit immutable semantic outcomes; they never write an
audit sink. The runtime wraps and routes boundary, lifecycle, failure, and pre-effect audit records,
while the audit adapter alone owns append persistence and acknowledgement. The coordinator
dispatches outcomes to owners but does not apply accounting or risk policy.

The runtime coordinator owns sequencing, queueing, lifecycle, audit-record routing, the outbound
pre-effect acknowledgement gate, and the normal-path inbound-owner acknowledgement gate. It MUST
NOT own strategy, portfolio, risk, matching, order-state, accounting policy, or audit persistence.
An optional feature stage may run before strategy, but it receives only data visible at the
injected clock and cannot bypass the rest of the chain.

Portfolio planning owns both desired `PortfolioTarget` values and the `OrderIntent` values derived
from the target and current canonical state. Every target produces an auditable planning outcome,
including a no-op, and may produce zero or more intents. Every emitted intent that is successfully
evaluated produces an auditable risk decision with allow, resize, or reject semantics. A
risk-evaluation failure is fatal and produces no approved output. Rejected intents never reach
execution. Resized intents retain lineage to both the original intent and the approved value. This
decision vocabulary does not freeze a Python type or field schema.

Backtest and paper are adapter profiles around this shared runtime, not alternative trading
engines. Research may remain a read-only exploratory workflow, but any research output claiming
trades or P&L MUST enter the backtest profile and full chain.

Historical matchers and paper simulators receive only immutable current-or-past market context
already admitted and sequenced by the runtime. They MUST NOT pull or advance a feed, read a market
data store, inspect a future iterator, or advance the clock. Issues #12 and #15 define the context
schema and deterministic ordering without weakening this admission boundary.

### Dependency direction and port ownership

Dependencies point inward:

1. `core` is the shared kernel for stable values, identities, event envelopes, errors, and the
   immutable cross-stage message definitions required to avoid peer-stage imports. It imports no
   other `ea` package. The stage named as semantic owner controls a message's meaning and
   evolution even when its dependency-neutral value definition lives in `core`.
2. `features`, `strategy`, `portfolio`, `risk`, and `execution` own their policy and public
   contracts. They may depend on `core` and their own narrow contracts, but MUST NOT import
   concrete adapters, configuration sources, CLI code, or mode packages. They MUST NOT import
   one another merely to call the next stage; the runtime coordinator performs dispatch.
3. The mode-neutral runtime kernel may depend on `core` and public stage contracts. It MUST NOT
   depend on `config`, `cli`, vendor SDKs, storage implementations, or concrete adapters.
4. Concrete `data`, `backtest`, `paper`, `brokers`, and `monitoring` adapters depend inward and
   implement ports owned by the consuming inner use case. Adapters MUST NOT define the
   core-facing contract, import peer adapters, or mutate another component's state.
5. `config` and `experiments` are outer-support packages. They may expose composition-facing
   contracts and use their own private libraries, but policy stages and the runtime kernel MUST
   NOT import them or read their state ambiently.
6. The outer composition root is the only production location allowed to import both inner
   components and concrete adapters. `cli` is a delivery entry point and delegates to the
   composition root rather than invoking stages directly.

Ports stay with their consumer instead of accumulating in a generic `ports` package. This port
ownership is distinct from the dependency-neutral immutable messages in `core`. The runtime owns
clock/event-source coordination, the mandatory audit outbound pre-effect and normal-path
inbound-owner acknowledgement gates, and audit/result ports;
execution owns the venue gateway port; each policy stage owns its callable contract. Vendor or
adapter-private representations and SDK models are translated at adapter boundaries and MUST NOT
enter the inner pipeline.

The logical runtime area contains a mode-neutral kernel and an outer composition function or
module. The exact Python layout remains an implementation choice, but import-boundary tests MUST
be able to distinguish the inner kernel from the outer composition root.

### Composition root and mode profiles

The composition root selects exactly one validated mode profile, constructs one implementation
for every required port, wires the graph, and owns all component lifetimes. Components MUST NOT
locate or construct peers, read ambient configuration, or use service locators and global
singletons. Tests may explicitly compose fakes.

All profiles require the same stage contracts and mode-agnostic strategy, portfolio, risk, runtime,
and execution/OMS policies. A run may select a policy implementation independently of mode, but a
mode profile may change only adapters and validated operational configuration; it cannot replace
or skip a stage:

- **Backtest:** bounded historical feed, deterministic virtual clock, deterministic simulated
  venue/matcher, mandatory local audit and deterministic result adapters, and no external
  broker-order writes.
- **Paper:** live or explicit replay feed, injected feed/real clock, simulated venue, durable
  mandatory audit/result adapters, and no external broker-order writes.
- **Live:** live feed, injected real clock, real venue adapter, and durable mandatory audit/result
  adapters. The profile is a future contract only; live construction and connectivity remain
  unavailable.

Mandatory audit or result failure fails the run and blocks new submissions in every profile. Only
optional metrics, alerts, and telemetry are best effort and cannot affect a trading decision.

Inner components receive capabilities such as time, data, randomness, configuration snapshots,
and venue access through contracts. They MUST NOT inspect a mode name or read wall time, future
data, environment variables, files, network clients, or unseeded randomness.

### State and message ownership

Ownership is singular:

- the runtime owns graph lifetime, event queue, ordering, and dispatch;
- the runtime envelopes/routes boundary, lifecycle, failure, and pre-effect audit records and owns
  the outbound pre-effect and normal-path inbound-owner acknowledgement gates, but not audit
  persistence;
- the data/clock adapters own event and time production, while the runtime alone admits visible
  events to inner stages;
- strategy owns only strategy-local state plus Signal semantics and production;
- portfolio owns target/intention semantics, planning, canonical `Account` / `Position`, cash, and
  the sole canonical ledger; after accepted fills or reconciliation it returns an updated immutable
  snapshot through the runtime;
- risk owns risk-decision semantics, limits, halt state, and decision/derived state; it evaluates an
  `OrderIntent` together with the latest immutable canonical portfolio snapshot and is not a second
  ledger;
- shared execution/OMS owns Order/Fill semantics, alone creates orders and canonical execution
  requests, and owns their state machine and correlation; it accepts an approval-bearing risk
  outcome referencing the effective intent, never a bare `OrderIntent`;
- a venue adapter translates vendor reports into canonical, secret-free raw execution facts;
  execution/OMS preserves their provenance, normalizes/deduplicates them, and publishes an explicit
  immutable processing outcome for every fact, including canonical order events and fills;
- a historical matcher or paper simulator consumes only runtime-admitted current/past market
  context and cannot independently operate a feed, store, iterator, or clock;
- monitoring/audit owns append persistence and persisted acknowledgement but never drives or
  transforms trading policy.

Portfolio cash and positions change only from accepted canonical fills or explicit reconciliation
events. The resulting immutable snapshot returns through the runtime to risk and other consumers.
Execution/reconciliation outcomes may update risk-derived state, but broker snapshots are only
reconciliation observations and risk never owns a second silent ledger. No mutable state is shared
across stages, and no adapter may maintain a writable shadow portfolio or order book that competes
with the canonical owner.

Every strategy-originated chain preserves end-to-end correlation from Signal through
PortfolioTarget, OrderIntent, risk decision, Order, and Fill so that audit and reconciliation can
reconstruct the decision path. Externally originated execution or reconciliation facts record only
their true origin and known venue/order identifiers. Missing ancestry MUST NOT be fabricated, and
a real fill MUST NOT be discarded merely because correlation is unresolved; Issue #15 owns that
unresolved-correlation behavior.

### Lifecycle and failure boundary

Each runtime graph is single-use and follows:

`constructed -> starting -> running -> stopping -> stopped`

Fatal transitions follow `starting | running | stopping -> failing -> failed`. `stopped` and
`failed` are terminal, and `failed` is reached only after safety drain and cleanup. Restart creates
a new graph.

The runtime observes stop/failure requests only between serialized dispatch units and does not
interrupt an executing domain callback. The current unit completes before a deterministic cutover.
Any venue request not both authorized and issued before that cutover MUST NOT be submitted.

Construction is side-effect free. Start validates a complete graph, starts downstream consumers
and mandatory audit before upstream producers, and admits no event until all required components
are ready. The coordinator serializes decision dispatch even when adapters perform concurrent I/O;
Phase 1 backtests use a deterministic queue and virtual clock.

At the stop cutover, the runtime closes ingress, accepts no new market/timer input, and starts no
new decision chain. Queued-but-unstarted market/timer inputs receive explicit audited abandonment
outcomes in deterministic order. The runtime permits no new outbound venue submission, but drains
already-originated execution reports and reconciliation facts, normalizes them, and dispatches
canonical outcomes to portfolio/risk owners. Those owners may update the canonical ledger,
immutable snapshots, derived risk state, and reconciliation state. The runtime then records final
audit/result state, closes execution and venue resources, and closes mandatory audit last. Stop is
idempotent. A partial start or fatal failure unwinds only started resources in reverse order.

Risk rejection is a normal domain outcome. Unexpected policy-stage failures and terminal
event-source, execution, venue, mandatory-audit, or result failures enter `failing` at the next
dispatch boundary. The failure cutover applies the same input abandonment and submission ban as
stop, while safety-critical execution-report drain, reconciliation, canonical state updates,
mandatory audit attempts, and cleanup remain allowed. Adapter retries are bounded and surface
terminal failure.

If mandatory audit is unavailable, the runtime MUST remain fail-closed for new submissions but
MUST NOT discard an actual execution/reconciliation fact to pretend the run is clean. It continues
safety-critical drain, surfaces a terminal failure through every still-available result/error
channel, and never claims unavailable evidence was durably persisted. Optional metrics, alerts,
and telemetry remain best effort and cannot affect decisions.

Every simulated or real venue effect uses the same pre-effect handshake:

1. Execution/OMS prepares a canonical `Order` and a canonical, redacted execution request.
2. The runtime envelopes the intent, risk decision, Order, and request as mandatory audit data.
3. The audit adapter performs append persistence and returns persisted acknowledgement or failure.
4. Only acknowledgement authorizes Execution/OMS to submit that same request through its
   execution-owned venue port; failure enters `failing` without calling the venue.
5. The venue adapter only encodes the canonical request as a vendor wire command and adds
   authentication inside the adapter boundary. It MUST NOT silently alter instrument, side,
   quantity, price constraints, or other economic order semantics. A required semantic adjustment
   returns through Execution/OMS, repeats risk evaluation when approval could change, and requires
   a new audit acknowledgement before submission.

Credentials, signatures, tokens, and vendor wire objects MUST NOT enter the runtime, domain
messages, or audit adapter. The system does not claim atomicity between audit storage and a broker.
An uncertain broker submission MUST NOT be blindly retried; the runtime halts new submissions and
requires reconciliation. Externally originated facts cannot be audited before they happen. The
venue adapter first translates the vendor report to a canonical raw fact, Execution/OMS preserves
provenance and produces an explicit processing outcome, and the runtime normally obtains audit
acknowledgement before downstream owner dispatch. Audit failure instead enters the documented
`failing` safety path, where the real fact is processed without claiming durable evidence. This
ordering neither sends vendor objects to runtime nor permits Execution/OMS to write the audit sink.

### Forbidden bypasses

The following are architecture violations:

- strategy calling a broker, exchange SDK, venue, OMS, or risk implementation, or creating an
  `Order` or `Fill`;
- portfolio or risk submitting to a venue, and execution accepting a bare intent instead of an
  approval-bearing risk outcome referencing the effective intent;
- a backtest- or paper-specific strategy, portfolio, risk, OMS, ledger, fill, or P&L pipeline;
- a data feed calling strategy directly or a venue adapter mutating portfolio/risk directly;
- Execution/OMS silently dropping an inbound raw fact, erasing true provenance, or failing to emit
  an explicit processing outcome;
- normal-path runtime dispatch of an inbound outcome to a state owner before audit acknowledgement;
  audit failure MUST instead use the explicit `failing` safety path;
- a domain stage writing an audit sink, or the runtime authorizing any simulated/real venue request
  before persisted audit acknowledgement;
- a matcher/simulator pulling or advancing a feed, reading a market-data store, inspecting a
  future iterator, or advancing a clock;
- concrete adapter or SDK imports in inner modules, vendor/adapter-private objects crossing
  inward-facing ports, or adapters importing peer adapters;
- credentials, signatures, tokens, or vendor wire objects leaving the outer adapter boundary;
- an adapter silently changing audited economic order semantics instead of returning the request
  through OMS/risk and a new audit acknowledgement;
- mode checks in strategy, portfolio, risk, or execution;
- adapter construction outside the composition root, direct CLI-to-stage calls, service
  locators, global singletons, or ambient configuration/time/random/network access;
- shared mutable state; fabricated external lineage; discarded real fills due to unresolved
  correlation; swallowed mandatory-audit or fatal failures; blind retries after an uncertain
  submission; or new trading decisions/submissions during `stopping`, `failing`, or after terminal
  state;
- wiring a paper profile to a real order-writing venue adapter.

### Deferred contracts

This ADR does not choose behavior owned by the following Issues:

- [Issue #12](https://github.com/jayjcc8-cloud/ea-quant/issues/12): canonical market-data,
  instrument, time, as-of visibility, and deterministic event-ordering semantics;
- [Issue #13](https://github.com/jayjcc8-cloud/ea-quant/issues/13): strict typed configuration,
  source precedence, secret references, and fail-closed mode authorization;
- [Issue #14](https://github.com/jayjcc8-cloud/ea-quant/issues/14): reproducible run manifest and
  code/configuration/data/seed lineage;
- [Issue #15](https://github.com/jayjcc8-cloud/ea-quant/issues/15): deterministic matching,
  execution outcome, fill, and reconciliation semantics.

## Consequences

Positive:

- Backtest, paper, and future live modes cannot silently diverge in trading policy or accounting.
- Risk, audit, and execution are mandatory boundaries rather than optional helper calls.
- Deterministic adapters can be tested without vendor SDKs or live connectivity.
- Domain policy remains replaceable and independent of orchestration and infrastructure.

Negative:

- Phase 1 needs a small runtime coordinator and explicit contracts before producing a useful
  backtest.
- Import boundaries and mode wiring require automated architecture tests once packages exist.
- Audit-before-effect and uncertain-submission handling add operational complexity.
- Some semantics remain intentionally unresolved until Issues #12 through #15 are completed.

## Validation

Later implementation must prove this decision with:

- import-boundary tests for inward dependencies and the sole composition root;
- mode-wiring tests proving backtest and paper cannot construct a real order-writing venue;
- ordered-spy contract tests for strategy -> portfolio -> risk -> execution;
- tests proving risk rejection never reaches execution or a venue;
- tests proving every emitted intent reaches risk with the latest immutable canonical portfolio
  snapshot and that execution rejects a bare intent without approval proof;
- ordered inbound-report tests proving adapter translation -> OMS processing outcome -> runtime
  audit acknowledgement -> owner dispatch, including duplicate/invalid/unresolved outcomes and the
  audit-failure safety path;
- tests proving execution outcomes return through execution/OMS and the runtime queue before
  portfolio state changes, and that the updated snapshot returns to runtime/risk;
- tests proving simulated venues consume only runtime-admitted current/past market context and
  cannot independently access or advance a feed, store, future iterator, or clock;
- deterministic golden backtests plus lifecycle/failure-injection tests covering cutover,
  audited abandonment, safety drain, and terminal cleanup;
- mandatory audit-acknowledgement-before-effect tests for backtest, paper, and future live venue
  ports, including request semantic invariance across adapter encoding, plus uncertain-submission
  and unresolved-external-correlation reconciliation tests.
