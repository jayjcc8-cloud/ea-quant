# ADR 0004: Canonical Market Data, Time, and Visibility

Date: 2026-07-16

## Status

Accepted

## Context

ADR 0003 requires one mode-neutral runtime to admit and sequence market events before any feature,
strategy, matcher, or other inner component can consume them. It deliberately leaves the concrete
Instrument, market-data, time, revision, visibility, and equal-timestamp ordering contracts to
Issue #12.

The Phase 0 placeholder models do not provide those contracts. Instrument identity is an
ambiguous string concatenation, a Bar has one unspecified timestamp, non-finite values can enter
the model, revisions and source provenance do not exist, and input iteration order can silently
decide equal-timestamp behavior. Those gaps would make point-in-time backtests vulnerable to
look-ahead and make results impossible to reproduce across data sources or processes.

Mature systems such as NautilusTrader and LEAN inform the separation of venue-local instrument
identity, event time, knowledge time, and closed-bar boundaries. This ADR adopts only the minimum
semantics needed by EA. It does not copy either framework's type system, event engine, or vendor
model; the project's deterministic tests and actual run evidence remain authoritative.

## Decision

### Structured Instrument identity

The canonical Instrument identity is the structured pair:

```text
(venue.code, symbol)
```

`VenueId.code` is an explicitly supplied uppercase ASCII token matching
`[A-Z0-9][A-Z0-9._-]{0,31}`. `Instrument.symbol` is an explicitly supplied venue-local ASCII token
matching `[A-Za-z0-9][A-Za-z0-9._/-]{0,63}`. Symbol case is preserved and is significant. Core
constructors never trim, uppercase, case-fold, Unicode-normalize, or resolve aliases. A data
adapter must map each vendor identifier to this canonical pair before entering `core`.

The derived `VENUE:SYMBOL` value is a human-readable identifier only. Both components prohibit
`:` so this display value is unambiguous, but canonical persistence and API payloads must preserve
the two structured fields. Quote currency, asset class, contract multiplier, tick size, lot size,
and vendor identifiers are not part of Instrument identity and are deferred metadata contracts.

Identity values are immutable and hashable. The same symbol on two venues is two Instruments, and
two symbols that differ only in case are distinct when a venue's canonical mapping says so.

### UTC and closed Bar intervals

Every canonical datetime is timezone-aware and normalized to the standard-library `UTC` object.
Naive values and non-zero UTC offsets fail in `core`; adapters must convert them explicitly before
constructing canonical values. Python `datetime` microsecond resolution is the Phase 1 precision.
No core component reads wall time.

A Bar represents one completed half-open interval:

```text
[interval_start, interval_end)
```

`interval_start < interval_end` is mandatory. `event_time` is derived as `interval_end`; a second
stored timestamp is forbidden because it could disagree. A Bar is closed at exactly
`interval_end`. Forming/provisional bars require a future distinct type and cannot masquerade as
canonical Bars. Session calendars, holidays, DST alignment, and calendar-duration inference stay
at an adapter or later domain boundary.

### Adjustment and OHLCV

Every Bar declares an `Adjustment`. This ADR accepts only `RAW`, meaning source/venue prices and
instrument-native traded quantity without a corporate-action transformation. There is no
`UNKNOWN` or ambiguous `ADJUSTED` default. Raw and any future adjusted series are distinct logical
records, not revisions of one another. A future ADR may add an adjustment mode only after defining
its factor, basis date, OHLC transformation, volume transformation, and reproducibility lineage.

Open, high, low, close, and volume are canonical Python `float` (IEEE-754 binary64) values. Core
accepts the exact runtime type `float` and rejects implicit strings, integers, booleans, dataframe
scalars, NaN, and positive or negative infinity. Adapters perform any explicit conversion. Values
must satisfy:

```text
low <= open <= high
low <= close <= high
volume >= 0
```

Equal prices, fractional or zero volume, and finite negative prices are valid. A global negative
price ban would reject real commodity, spread, and rate observations; instrument-specific price
rules belong to a later metadata or risk contract. Volume is instrument-native quantity, not an
implicitly calculated quote notional. Missing values are errors rather than NaN or zero sentinels.

Market observations and feature calculations remain finite `float`. Order quantity, limit/stop
price, Fill, cash, fees, positions, risk limits, and ledger entries must use a future explicit
`Decimal` or scaled-integer contract and instrument tick/lot quantization. `Decimal(float_value)`
is forbidden. That exact execution/accounting boundary belongs to Issue #15 and portfolio work;
this ADR does not introduce Money, Order, Fill, or ledger types.

### Source, availability, emission, and revision

Each immutable `MarketDataEnvelope` wraps one Bar and requires:

- `source`: a stable lowercase ASCII source/dataset namespace matching
  `[a-z0-9][a-z0-9._-]{0,63}`; it is distinct from the trading venue;
- `source_sequence`: a non-negative integer, unique within the canonical source stream and stable
  across replay;
- `revision`: a non-negative integer; zero is the initial convention, gaps are allowed;
- `available_at`: the earliest UTC instant when this revision was knowable to this source/system.

There are no defaults for these fields. Integers must have exact runtime type `int`, so `bool` is
invalid. Historical file import time is not automatically `available_at`; an adapter must obtain
or derive honest point-in-time lineage. A dataset containing only final values with no historical
availability cannot be described as point-in-time safe.

For a closed Bar:

```text
payload.event_time <= available_at
```

The identities are:

```text
logical record = (
    BAR,
    venue,
    symbol,
    interval_start,
    interval_end,
    adjustment,
    source,
)

record version = (logical record, revision)
source emission = (source, source_sequence)
```

`available_at` and `source_sequence` are not logical record identity. `source` and `adjustment`
are identity so one source cannot revise another source and raw data cannot revise an adjusted
series.

Within one logical record, higher revisions must have non-decreasing `available_at` and strictly
increasing `source_sequence`. Revisions may skip numbers. Every record version and source emission
must be unique. An exact duplicate is an error rather than a silent deduplication; a repeated
record version with a different payload or availability is a conflict; a repeated source emission
for another record is a source-sequence collision. Batch validation fails before admission for all
of these conditions.

All revisions are retained. A loader must not collapse a history to its final revision before a
replay because doing so would reveal future corrections.

### As-of snapshot and visibility

`as_of` is a UTC query cutoff or the injected clock value; it is not stored in an event. An
envelope is visible at cutoff `T` exactly when:

```text
payload.event_time <= T and available_at <= T
```

The boundary is inclusive. Because `available_at >= event_time`, availability normally provides
the effective gate, but both conditions remain explicit in the contract.

`latest_as_of(events, T)` validates the full batch, removes non-visible versions, groups the
remainder by logical record, selects the highest visible revision in each group, and returns the
selected envelopes in canonical admission order. Sources are never merged. A correction affects
snapshots only at and after its own `available_at`; querying an earlier cutoff produces the same
result whether or not that future correction is present in storage.

The replay/admission view is deliberately different from the snapshot projection: it emits each
visible revision at its own availability time. Runtime policy may later decide how a correction
affects strategy state, but it may not erase the correction's time of knowledge.

### Deterministic admission order

Market envelopes use this exact ascending total-order key:

```text
(
    available_at,
    event_time,
    event_kind_rank,       # BAR is 0
    source.code,
    source_sequence,
    instrument.venue.code,
    instrument.symbol,
    interval_start,
    interval_end,
    adjustment.value,
    revision,
)
```

Availability is first so an older event published late cannot be processed before information
that was actually knowable earlier. Source lexical order is only a deterministic merge
tie-breaker, not a source-authority policy. Float payload values never participate in identity or
ordering. Python hash values, object addresses, random UUIDs, set/dict traversal, original input
position, and stable-sort fallback are forbidden tie-breakers.

Batch validation checks ordering-key uniqueness before sorting. A duplicate or conflict fails
explicitly instead of allowing input order to decide. BAR rank 0 is frozen; a future event kind
must receive a stable rank through a new decision. Cross-domain ordering among market data,
timers, decisions, orders, and fills remains Issue #15/runtime work.

### Injected-clock admission boundary

`Clock.now()` is an injected capability and must return canonical UTC. Within a run its values are
monotonic. A time-only watermark is insufficient because several emissions may legitimately share
one `available_at`. The pure runtime-facing admission helper therefore reads the clock exactly
once and accepts an immutable cursor containing the previous clock cutoff and the last admitted
envelope. It rejects a clock value earlier than the cursor cutoff and returns visible envelopes
whose full admission key is greater than the cursor's last key. An optional positive `limit` lets
runtime drain a complete history in bounded serialized batches without losing equal-availability
events. The returned cursor carries the exact cutoff and last emitted key into the next dispatch
unit and must be committed only after the returned batch has been processed successfully. With no
cursor it begins at the first visible history record.

Every call supplies the complete cumulative canonical history, including the cursor's last event;
an independent delta page is invalid input. Batch validation therefore continues to enforce
source-wide emission uniqueness and revision history across old and new records. A bounded
historical source validates its complete immutable batch before replay. A future growing adapter
must additionally retain global validation state and append only in canonical admission order: if
a record becomes known only after a cursor is committed, its honest `available_at` is that later
knowledge time and its key must be greater than the committed cursor. Inserting a newly disclosed
event at or before a committed key is invalid lineage, not a reason to rewind strategy state
implicitly. Growing-source state is not implemented by this Issue.

The helper does not advance the clock and exposes no iterator or cursor into future source data,
future payload, or store. A future historical-feed adapter may hold future records privately, but
its public runtime port must apply this gate. Features, strategies, matchers, and paper simulators
receive only the immutable admitted envelopes supplied by runtime; they cannot call the feed,
inspect storage, or advance time. A private scheduler may eventually receive only a
next-availability timestamp, not the future payload, under a runtime-owned port.

This Issue implements immutable core values, validation, ordering, as-of projection, and the pure
admission helper. It does not implement a historical feed adapter, virtual clock, data store, or
runtime coordinator.

## Consequences

- Instrument and Bar constructors replace the Phase 0 placeholders. `exchange`, `quote_currency`,
  and ambiguous `timestamp` constructor fields are intentionally not retained.
- The compatibility module may re-export the new canonical classes, but only one implementation
  and one schema exist.
- Invalid source data fails at the boundary instead of being silently normalized or reordered.
- Backtests can reproduce multi-source equal-time order and point-in-time revision visibility.
- Adapters carry more responsibility: explicit identity mapping, UTC conversion, float conversion,
  source sequence generation, and honest availability lineage.
- Retaining revisions costs storage, but is necessary for auditable point-in-time replay.
- Changing the ordering tuple or identity rules after this ADR is Accepted can change backtest
  results and requires a superseding ADR.

## Alternatives considered

### Keep `exchange:symbol` as the canonical identity

Rejected. It conflates structure with display serialization, cannot safely evolve metadata, and
the existing object equality already disagrees with the derived ID.

### Normalize identifiers silently in core

Rejected. Silent trimming or case conversion can merge distinct vendor values and hides adapter
mapping decisions that must be reproducible.

### Store one generic timestamp

Rejected. Bar interval end and time of knowledge have different semantics; conflating them causes
look-ahead.

### Use Decimal for all market data

Rejected for the analytical boundary. It would add conversion cost and complexity without making
feature calculations or source precision exact. Exact execution and accounting values still
require a separate quantized contract.

### Keep only the final revision

Rejected. A final correction would become visible before its historical publication time.

### Use input order or payload values for ties

Rejected. Input order is adapter/process dependent, while floats are unsuitable identity and can
change after correction. Stable source sequences provide explicit replay lineage.

## Non-goals

- Vendor adapters, ingestion, storage, dataframe schemas, or data-version manifests.
- Feature engineering, strategies, reports, or performance metrics.
- Historical feed, virtual clock, runtime coordinator, matcher, paper simulator, or event bus.
- Corporate-action engines, adjustment factors, total-return data, or source precedence.
- Exchange calendars, sessions, holidays, DST policy, or nanosecond tick/quote/order-book data.
- Instrument currency, tick size, lot size, contract multiplier, or security-master registry.
- Order, Fill, Money, portfolio, ledger, risk, configuration, or run-manifest schemas.
- Cross-domain event priority or correction-triggered strategy policy.
