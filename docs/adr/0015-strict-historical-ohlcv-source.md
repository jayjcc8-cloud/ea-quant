# ADR 0015: Strict Historical OHLCV Source Boundary

Date: 2026-07-28

## Status

Proposed

## Context

Accepted ADR 0004 defines canonical `Bar`, `MarketDataEnvelope`, revision, availability,
deterministic admission order, as-of visibility, and an injected-clock admission helper. Accepted
ADR 0006 defines the replay window and semantic `DataFingerprint`. The repository implements those
dependency-neutral values and pure functions, but it does not yet have:

- a concrete local Phase 1 OHLCV format;
- a strict adapter that turns file bytes into canonical envelopes without permissive inference;
- a bounded source capability that holds future payloads privately; or
- a runtime-facing way to learn the next availability time without obtaining the future event.

Without that boundary, later backtest code could parse timestamps differently across libraries,
collapse revisions, derive `available_at` from file order, silently coerce missing or non-finite
values, expose the entire future dataset to strategy or matching code, or fingerprint different
semantic inputs as though they were one run.

Issue #51 owns only this data slice. It does not implement a virtual clock, runtime coordinator,
features, strategy, portfolio planning, matching, execution, ledger integration, or reports.

## Reuse assessment

The required capability is deterministic parsing of one small closed tabular schema, followed by
existing EA canonical validation.

The provisional selection is Python 3.12.13 standard-library `csv`, wrapped by a narrow EA adapter.
It introduces no package, native binary, transitive dependency, lockfile, deployment burden, or
additional supply-chain surface. The adapter, not `csv`, owns the exact encoding, header, token,
timestamp, row, error, and canonical-construction policy.

Current alternatives inspected on 2026-07-28:

- pandas v3.0.5, BSD-3-Clause, released 2026-07-22 and actively maintained;
- Polars py-1.43.1, MIT, released 2026-07-27 and actively maintained.

Both are capable parsers. They are rejected for this boundary because dataframe inference,
missing-value/coercion semantics, native/transitive footprint, and broad engine ownership add
more semantic and operational surface than the closed Phase 1 format needs. Neither is prohibited
for later research or larger analytical adapters; neither may define the canonical ingestion
contract implicitly.

## Decision

### Ownership and package direction

The concrete adapter lives under `ea.data`. It may depend inward on:

- `ea.core.market_data`;
- `ea.core.run`;
- `ea.core.time`; and
- the existing `ea.data.fingerprint` selection/fingerprint boundary.

`core`, `runtime`, `features`, `strategy`, `portfolio`, `risk`, and `execution` do not import the
adapter. The future runtime composition root may receive the source through a consumer-owned
structural port. Strategies, matchers, and portfolio policies never receive a data source, file
path, byte buffer, future iterator, or `MarketDataSelection`.

The adapter uses only the Python standard library and existing project code. This ADR authorizes
no dependency or lockfile change.

### Closed file profile

The Phase 1 profile name is:

```text
ea-phase1-ohlcv-csv-v1
```

Input is one bounded byte string. A filesystem helper may read a local regular file into that
byte string once and then invoke the same decoder. Parsing and fingerprinting always use those
captured bytes; the path is never reopened during one decode.

The byte contract is:

- minimum length: 1 byte;
- maximum length: 67,108,864 bytes (64 MiB);
- encoding: strict UTF-8;
- UTF-8 BOM: forbidden;
- NUL bytes: forbidden;
- newline: LF or CRLF; the decoder uses universal-newline interpretation only for CSV record
  boundaries;
- maximum logical CSV records including the header: 1,000,001;
- maximum decoded field length: 1,024 Unicode code points;
- blank records and comment records: forbidden;
- delimiter: literal comma;
- quote character: literal double quote;
- doubled quote escapes are accepted according to the standard CSV reader;
- backslash escaping, delimiter detection, dialect detection, and whitespace skipping are
  forbidden.

Quoted and unquoted fields are semantically equivalent after CSV decoding. Embedded CR, LF, NUL,
and non-ASCII code points are forbidden in every decoded field. This keeps row evidence bound to
one logical CSV record and prevents invisible identifier or numeric normalization.

The first logical record is the exact ordered header:

```text
schema_version,venue,symbol,interval_start,interval_end,adjustment,open,high,low,close,volume,source,source_sequence,revision,available_at
```

It has no aliases, optional columns, additional columns, alternate order, case variants, or
surrounding whitespace. Every later logical record has exactly 15 fields. At least one data record
must exist before replay-window selection.

`record_number` in error evidence is one-based over logical CSV records: the header is record 1
and the first data row is record 2. `field_name` is either one exact header name or `null` for a
record/header/file-level failure.

### Exact tokens

Every field is used exactly as decoded. No token is trimmed, case-folded, Unicode-normalized,
locale-parsed, imputed, or defaulted.

`schema_version` is exactly:

```text
1
```

`venue`, `symbol`, and `source` are already canonical identifiers. The adapter constructs exact
`VenueId`, `Instrument`, and `SourceId` values. There is no vendor alias map or implicit mapping in
this profile.

`adjustment` is exactly:

```text
raw
```

Each datetime token uses exactly 27 ASCII characters:

```text
YYYY-MM-DDTHH:MM:SS.ffffffZ
```

It must parse to an exact standard-library `datetime`, reformat byte-for-byte to the same token,
and use the canonical `UTC` object. Leap seconds, offsets, missing or extra fractional digits,
lowercase `z`, whitespace, and timezone inference are rejected.

`source_sequence` and `revision` use the canonical unsigned decimal grammar:

```text
0 | [1-9][0-9]*
```

They must fit `0..2^64-1`. A sign, leading zero, decimal point, exponent, whitespace, or non-ASCII
digit is invalid.

`open`, `high`, `low`, `close`, and `volume` use the canonical decimal-to-binary64 grammar:

```text
-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?(0|[1-9][0-9]*))?
```

Leading plus, leading/trailing whitespace, leading zeros, bare decimal points, hexadecimal forms,
underscores, locale separators, `NaN`, and infinities are rejected before conversion. Conversion
uses the standard-library exact `float` constructor. The resulting runtime value must be finite;
overflow is rejected. Underflow and normal binary64 rounding are the explicit Phase 1 analytical
contract. Existing `Bar` validation then enforces OHLC and non-negative volume invariants.

Different accepted decimal tokens that convert to identical binary64 values represent the same
canonical market observation and therefore the same semantic fingerprint. The raw file bytes are
an adapter diagnostic, not a second run identity.

### Canonical construction and selection

For every data record, construction order is fixed:

1. validate decoded-field shape and ASCII policy;
2. validate `schema_version`;
3. construct `VenueId` and `Instrument`;
4. parse `interval_start` and `interval_end`;
5. validate `adjustment`;
6. parse OHLCV binary64 values;
7. construct `Bar`;
8. construct `SourceId`;
9. parse `source_sequence` and `revision`;
10. parse `available_at`;
11. construct `MarketDataEnvelope`.

The decoder accumulates no public result while parsing. After every record is constructed, it
calls the existing canonical batch validation/order boundary over the complete file history.
Duplicate record versions, source-emission collisions, admission-key collisions, revision
availability regression, and revision source-sequence regression retain ADR 0004 semantics.
Original file order is never an admission tie-breaker.

The decoder then calls the existing `select_and_fingerprint_market_data` with the exact supplied
`ReplayWindow`. Selection remains:

```text
window.start_inclusive <= envelope.available_at < window.end_exclusive
```

The returned events are in canonical admission order and the `DataFingerprint` is computed from
the existing ADR 0006 canonical record bytes, not from CSV spelling, row order, file metadata, or
path. An empty selected window is an explicit failure.

The public decoded value is immutable:

```python
@dataclass(frozen=True, slots=True)
class Phase1HistoricalDataset:
    profile: Literal["ea-phase1-ohlcv-csv-v1"]
    replay_window: ReplayWindow
    selection: MarketDataSelection
    source_bytes_sha256: Sha256Digest
    source_byte_count: int
```

`source_bytes_sha256` is ordinary SHA-256 over the exact captured bytes and is diagnostic lineage.
It does not replace or alter `selection.fingerprint`, and it is not accepted in place of the ADR
0006 manifest data fingerprint. `source_byte_count` is in `1..67_108_864`.

The value does not make future events safe to pass inward. It is a composition/data-boundary value
used to build the manifest and the historical source.

### Public decoder and local-file helper

The public pure decoder is:

```python
decode_phase1_ohlcv_csv(
    content: bytes,
    *,
    replay_window: ReplayWindow,
) -> Phase1HistoricalDataset
```

`content` must have exact runtime type `bytes`; subclasses and bytearray-like values are rejected.
`replay_window` must be exact `ReplayWindow`.

The optional filesystem adapter is:

```python
read_phase1_ohlcv_csv(
    path: Path,
    *,
    replay_window: ReplayWindow,
) -> Phase1HistoricalDataset
```

`path` must be a standard `pathlib.Path` value for the current platform. The helper rejects a
string, bytes, arbitrary `PathLike`, missing path, directory, symlink,
socket, device, FIFO, or non-regular file. It obtains a pre-read stat, reads at most
67,108,865 bytes in binary mode, obtains a post-read stat, and rejects the read if device, inode,
size, or nanosecond modification time changed. It never follows a symlink and never rewrites,
renames, locks, deletes, or creates the source. Platform or permission failures map to the closed
I/O failure below without exposing locale-dependent exception text as canonical evidence.

### Historical source capability

The source is constructed only by:

```python
create_phase1_historical_market_data_source(
    dataset: Phase1HistoricalDataset,
) -> Phase1HistoricalMarketDataSource
```

Direct class construction raises `TypeError`. Construction validates the dataset and privately
copies the exact immutable selected event tuple. The public surface is only:

```python
source.replay_window -> ReplayWindow
source.fingerprint -> DataFingerprint
source.source_bytes_sha256 -> Sha256Digest

source.next_available_at(
    cursor: Phase1HistoricalSourceCursor | None,
) -> datetime | None

source.admit(
    *,
    clock: Clock,
    cursor: Phase1HistoricalSourceCursor | None,
    limit: int | None = None,
) -> tuple[Phase1HistoricalSourceCursor, tuple[MarketDataEnvelope, ...]]
```

There is no `events`, `history`, iterator, indexing, slicing, length, peek-payload, seek, rewind,
reset, append, refresh, load, path, file, dataframe, store, or clock-advance API.

`Phase1HistoricalSourceCursor` is a frozen, slots-based, factory-only wrapper issued by
`source.admit`. Direct construction raises `TypeError`. It contains:

```text
replay_window
data_sha256
record_count
admission_cursor
```

The first three fields exactly bind the existing ADR 0004 `AdmissionCursor` to this source's
semantic selection. A cursor from another byte spelling is compatible only when replay window,
semantic SHA-256, and record count are all identical; raw file SHA is deliberately not cursor
identity. A cursor with a different binding, a missing last event, or an event outside the bound
history fails closed. Callers cannot supply a raw `AdmissionCursor`. Read-only `as_of` and
`last_event` properties delegate to the inner admission cursor for runtime evidence.

`next_available_at` validates that a non-null cursor is exact, carries this source's semantic
binding, and carries a visible last event. It returns:

- the first event's `available_at` when `cursor` is null;
- the `available_at` of the first canonical event whose full admission key is greater than the
  cursor's last event;
- `None` after the final event.

Several later events may share the returned timestamp. The timestamp is scheduling metadata, not
payload visibility or admission. Calling `next_available_at` does not mutate the source or cursor.

`admit` reads `clock.now()` exactly once through the existing injected-clock boundary and applies
ADR 0004 visibility and full admission-key rules to the private selected history using the
cursor's inner `AdmissionCursor`. It returns a newly issued, semantically bound candidate cursor
plus currently visible events after the supplied cursor, bounded by a positive exact-int `limit`
when present. It never advances the clock, commits a cursor, or mutates state.

The runtime owns cursor commit. It can obtain a cursor only from this source API and commits the
returned cursor only after the admitted batch and
all consequences have been processed successfully. Repeating `admit` with the prior cursor is an
exact replay of source selection. Repeating it with the candidate cursor continues after the last
admitted key. A clock earlier than `cursor.as_of`, a cursor from another source/history, or a
cursor whose last event is missing fails before returning any event.

The source is immutable and bounded; growing-source late disclosure remains outside Phase 1. A
future source cannot reuse this contract silently because append authority and committed-key
lineage require a separate stateful decision.

### Closed failures and precedence

`HistoricalMarketDataError` contains only:

```text
code: HistoricalMarketDataFailureCode
record_number: int | None
field_name: str | None
```

It has a stable non-canonical human message, but exception text and OS text are not test or result
identity. The closed codes are:

```text
invalid_type
source_unreadable
source_changed_during_read
empty_source
source_too_large
invalid_encoding
invalid_file_character
malformed_csv
invalid_header
invalid_record_shape
invalid_field_character
invalid_schema_version
invalid_identity
invalid_timestamp
invalid_integer
invalid_float
invalid_bar
invalid_envelope
canonical_batch_conflict
empty_replay_selection
invalid_cursor
clock_regressed
invalid_limit
```

Precedence is:

1. exact argument types;
2. filesystem kind/read/stability for the path helper;
3. byte length;
4. UTF-8/BOM/NUL;
5. CSV syntax and record/field bounds;
6. exact header;
7. per-record construction in the fixed field order above;
8. complete canonical batch validation;
9. replay-window selection and semantic fingerprint;
10. dataset/source invariant validation;
11. cursor, clock, and limit validation for source calls.

The first applicable failure is the only failure. A decoder, reader, source factory, or source
call publishes no partial dataset, event tuple, cursor, or mutable state on failure. Existing
structured core errors are translated by explicit type and condition, never by message matching.
Unexpected implementation errors are not recast as data-quality outcomes.

### Determinism and evidence

The implementation must provide:

- golden source bytes, exact diagnostic byte SHA, canonical events, and semantic fingerprint;
- LF/CRLF and quoted/unquoted semantic-equivalence tests;
- strict BOM, encoding, NUL, header, width, blank/comment, multiline, oversized field/file, and
  record-count failures;
- exact timestamp, integer, float, OHLCV, identity, availability, revision, source-emission,
  duplicate/conflict, and equal-admission-key tests;
- input-row permutation tests proving canonical order and semantic fingerprint independence;
- replay-window boundary and empty-selection tests;
- next-availability tests including several events at one availability timestamp;
- cursor replay, foreign cursor, missing last event, clock regression, limit, exhaustion, and
  no-clock-advance tests;
- API tests proving no public future-event/history/path/iterator surface;
- filesystem symlink, non-regular, oversize, short-read/change-during-read, and read-failure tests
  on supported platforms;
- cross-process tests varying hash seed, locale, timezone, and environment while producing
  byte-identical canonical evidence; and
- failure-injection tests proving no partial publication.

At final verification, the exact candidate must pass repository `full`, current line/branch
coverage floors, byte-identical wheels, clean-wheel smoke, and exact-head CI.

## Consequences

Positive:

- Local OHLCV input has one reviewable schema and deterministic error surface.
- File spelling and path are separated from canonical semantic run identity.
- Runtime composition can schedule by knowledge time without exposing future payloads.
- Revisions, equal timestamps, cursor replay, and no-look-ahead remain governed by existing
  canonical values rather than a dataframe engine.
- The slice adds no dependency or second data model.

Negative:

- The format is intentionally strict and requires explicit upstream conversion.
- Binary64 conversion retains analytical rounding and underflow behavior.
- The 64 MiB / 1,000,000-row profile is not a general data lake format.
- Capturing file bytes once uses memory proportional to the bounded file.
- A runtime coordinator and virtual clock are still required before a backtest can run.

## Alternatives rejected

### Use pandas or Polars as the canonical parser

Rejected because inference and dataframe semantics would become part of the trust boundary while
adding unnecessary dependency and operational surface. A later adapter may use either library
only if it produces this exact canonical contract and passes the same evidence.

### Permit configurable headers, delimiters, time formats, or identifier maps

Rejected for Phase 1. Generic ingestion hides normalization decisions inside runtime configuration
and expands the reproducibility surface. Upstream conversion creates the one closed profile.

### Derive `available_at` from row order or file modification time

Rejected because neither proves historical knowledge time and both enable look-ahead. Every record
must carry explicit canonical `available_at`.

### Expose the selected event tuple directly to runtime consumers

Rejected because strategy or matching code could inspect future payloads. The dataset remains a
composition value; runtime-facing consumers receive only the source capability and admitted
events.

### Make the source statefully advance itself

Rejected because source mutation before downstream processing succeeds can lose events or make
replay depend on exception timing. Runtime owns atomic cursor commit.

## Non-goals

- Remote download, HTTP, database, object store, dataframe, notebook, or vendor SDK.
- Generic schemas, auto-detection, missing-value imputation, timezone inference, or identifier
  aliasing.
- Adjusted data, corporate actions, calendars, sessions, ticks, quotes, or order books.
- Source precedence, correction-triggered strategy policy, or growing-source append authority.
- Virtual clock, scheduler, runtime coordinator, feature state, strategy, portfolio planning,
  risk, OMS, matcher, ledger application, reconciliation, audit, or result output.
- Configuration changes, dependencies, lockfile changes, release, deployment, or credentials.
