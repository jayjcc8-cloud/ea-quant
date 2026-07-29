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

Before calling `csv.reader`, the adapter performs a bounded lexical preflight over the decoded
text. It tracks logical record, field position, and quote state; rejects a bare CR, embedded
newline in a quoted field, invalid quote transition, decoded field longer than 1,024 code points,
and more than 1,000,001 logical records. Consequently the standard reader never receives an
oversized or multiline field and does not depend on process-global `csv.field_size_limit`.

The reader is constructed over `io.StringIO(text, newline="")` with exactly:

```python
csv.reader(
    stream,
    dialect="excel",
    delimiter=",",
    quotechar='"',
    doublequote=True,
    escapechar=None,
    skipinitialspace=False,
    strict=True,
    quoting=csv.QUOTE_MINIMAL,
)
```

No other dialect option or locale state participates. A decoded empty record and a one-field
record beginning with `#` are ordinary invalid record shapes; comments are never recognized.

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
@final
@dataclass(frozen=True, slots=True, init=False)
class Phase1HistoricalDataset:
    profile: Literal["ea-phase1-ohlcv-csv-v1"]
    replay_window: ReplayWindow
    selection: MarketDataSelection
    source_bytes_sha256: Sha256Digest
    source_byte_count: int

    def __init__(self) -> None:
        raise TypeError("datasets are issued only by the Phase 1 OHLCV decoder")
```

`source_bytes_sha256` is ordinary SHA-256 over the exact captured bytes and is diagnostic lineage.
It does not replace or alter `selection.fingerprint`, and it is not accepted in place of the ADR
0006 manifest data fingerprint. `source_byte_count` is in `1..67_108_864`.

The dataset is factory-only and is issued only after the pure decoder has completed byte hashing,
all parsing, canonical validation, replay-window selection, and semantic fingerprinting. Direct
construction raises `TypeError`. The source factory requires the exact dataset type and
revalidates the profile, window/selection equality, selection fingerprint, digest/count types,
byte-count bound, and every derivable selection invariant before copying history.

The value does not make future events safe to pass inward. It is a composition/data-boundary value
used to build the manifest and the historical source. Its raw byte fields remain diagnostic
decoder evidence and are never manifest identity, cursor identity, or source-authority evidence.

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

`path` must be a standard `pathlib.Path` value for the current platform. A string, bytes, or
arbitrary `PathLike` is invalid. Phase 1 file reading requires POSIX `os.O_NOFOLLOW` and
`os.O_NONBLOCK`; when either capability is unavailable the helper fails closed as
`source_unreadable`.

The helper uses `lstat`, rejects a missing path, directory, symlink, socket, device, FIFO, or other
non-regular path, then opens with `os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK`. `O_NONBLOCK`
prevents a regular-to-FIFO/device replacement between `lstat` and `open` from blocking before
descriptor validation. The helper immediately verifies the descriptor with `fstat` and rejects a
non-regular descriptor before any read. It then reads at most 67,108,865 bytes in binary mode,
performs a second `fstat` and path `lstat`, and rejects the capture if:

- the descriptor or path is no longer the same regular `(device, inode)`;
- pre-read, post-read, or path size differs from the captured byte count;
- nanosecond modification time changed; or
- the path disappeared or changed identity before the final check.

It never follows a symlink and never rewrites, renames, locks, deletes, or creates the source.
Platform or permission failures map to the closed I/O matrix below without exposing
locale-dependent exception text as canonical evidence.

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
history fails closed. Here “missing last event” means a non-null last event that cannot be
resolved exactly in the source; a factory-issued initial cursor with `last_event=None` is valid.
Callers cannot supply a raw `AdmissionCursor`. Read-only `as_of` and `last_event` properties
delegate to the inner admission cursor for runtime evidence.

For a non-null last event, the source resolves its unique full ADR 0004 admission key in a private
source-owned map, compares ADR 0006 canonical record bytes byte-for-byte, and reconstructs the
inner cursor with the source-owned event before any scheduling or admission calculation.
Dataclass equality is insufficient because binary64 `+0.0` and `-0.0` compare equal but have
different canonical record bytes. A missing key or byte mismatch is `invalid_cursor`.

For `last_event=None`, the cursor is valid only when no event in the bound selection was visible
at `cursor.as_of`. This is the exact candidate produced by a successful admission before the first
availability. If any event was already visible at that cutoff, the cursor could skip evidence and
is `invalid_cursor`.

`next_available_at` validates that a non-null cursor is exact, carries this source's semantic
binding, and carries either the valid initial no-event state or one exact visible source-owned
last event. It returns:

- the first event's `available_at` when `cursor` is null;
- `max(cursor.as_of, first_event.available_at)` for a valid initial cursor;
- `max(cursor.as_of, next_event.available_at)` for the first canonical event whose full admission
  key is greater than the cursor's last event;
- `None` after the final event.

Several later events may share the returned timestamp. The timestamp is scheduling metadata, not
payload visibility or admission. A returned value equal to `cursor.as_of` requires the runtime to
drain again without advancing its clock; this occurs after a limited batch leaves already-visible
events. A returned value is never earlier than the committed cutoff. Calling `next_available_at`
does not mutate the source or cursor.

`admit` reads `clock.now()` exactly once through the existing injected-clock boundary and applies
ADR 0004 visibility and full admission-key rules to the private selected history using the
cursor's inner `AdmissionCursor`. It returns a newly issued, semantically bound candidate cursor
plus currently visible events after the supplied cursor, bounded by a positive exact-int `limit`
when present. It never advances the clock, commits a cursor, or mutates state.

The runtime owns cursor commit. It can obtain a cursor only from this source API and commits the
returned cursor only after the admitted batch and all consequences have been processed
successfully. Repeating `admit` with the prior cursor is an exact replay of source selection.
Repeating it with the candidate cursor continues after the last admitted key.

An admission before the first event returns an empty tuple and a bound cursor with
`as_of=clock.now()` and `last_event=None`. Replaying the prior cursor returns the same candidate;
committing the candidate and calling `next_available_at` returns at least its cutoff and normally
the first event's availability; admitting at that availability starts from the first event. A
clock earlier than `cursor.as_of`, a cursor from another semantic selection, or an unresolved
non-null last event fails before returning any event.

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
invalid_adjustment
invalid_identity
invalid_timestamp
invalid_integer
invalid_float
invalid_bar
invalid_envelope
canonical_batch_conflict
empty_replay_selection
invalid_dataset
invalid_cursor
clock_regressed
invalid_limit
```

The following matrices are normative. `null` means the evidence field is `None`. When a field
name is shown, it is the literal header name. The first matching row in each operation is the only
returned failure.

#### Byte decoder matrix

| Condition | Code | `record_number` | `field_name` |
|---|---|---:|---|
| `content` or `replay_window` has the wrong exact type | `invalid_type` | null | null |
| zero bytes | `empty_source` | null | null |
| more than 67,108,864 bytes | `source_too_large` | null | null |
| strict UTF-8 decode fails, or decoded text starts with BOM | `invalid_encoding` | null | null |
| NUL or bare CR outside a CRLF pair | `invalid_file_character` | null | null |
| lexical quote transition is invalid, or `csv.reader` raises after lexical preflight | `malformed_csv` | next logical record | null |
| quoted field contains CR/LF | `invalid_field_character` | current logical record | current field, or null in header |
| decoded field exceeds 1,024 code points | `invalid_record_shape` | current logical record | current field, or null in header |
| logical record count would exceed 1,000,001 | `invalid_record_shape` | 1,000,002 | null |
| header differs by value, width, order, quoting result, or case | `invalid_header` | 1 | null |
| header is the only record | `invalid_record_shape` | 2 | null |
| data record is blank, comment-shaped, or not exactly 15 fields | `invalid_record_shape` | current logical record | null |
| decoded data field contains non-ASCII/control character | `invalid_field_character` | current logical record | first offending field in header order |
| schema token is not exact `1` | `invalid_schema_version` | current logical record | `schema_version` |
| venue constructor rejects | `invalid_identity` | current logical record | `venue` |
| symbol/Instrument constructor rejects | `invalid_identity` | current logical record | `symbol` |
| interval timestamp token/UTC/reformat rejects | `invalid_timestamp` | current logical record | `interval_start`, then `interval_end` |
| adjustment is not exact `raw` | `invalid_adjustment` | current logical record | `adjustment` |
| OHLCV grammar/conversion/finite check rejects | `invalid_float` | current logical record | `open`, `high`, `low`, `close`, then `volume` |
| interval start is not earlier than end | `invalid_bar` | current logical record | `interval_end` |
| `low > high` | `invalid_bar` | current logical record | `high` |
| open is outside `[low, high]` | `invalid_bar` | current logical record | `open` |
| close is outside `[low, high]` | `invalid_bar` | current logical record | `close` |
| volume is negative | `invalid_bar` | current logical record | `volume` |
| source constructor rejects | `invalid_identity` | current logical record | `source` |
| source-sequence grammar/range rejects | `invalid_integer` | current logical record | `source_sequence` |
| revision grammar/range rejects | `invalid_integer` | current logical record | `revision` |
| availability timestamp rejects | `invalid_timestamp` | current logical record | `available_at` |
| availability is earlier than event time | `invalid_envelope` | current logical record | `available_at` |
| exact `Bar`/envelope construction rejects after all explicit checks | `invalid_bar` / `invalid_envelope` | current logical record | field of the violated explicit invariant |
| complete history conflict from the matrix below | `canonical_batch_conflict` | as below | as below |
| no event falls inside the replay window | `empty_replay_selection` | null | null |

Data records are evaluated in file order and fields in the construction order already frozen.
Header parsing finishes before a field name can appear in evidence. A lexer failure in the header
therefore uses a null field. “Next logical record” is one plus the count of completely decoded
records, which is stable across LF and CRLF.

The adapter explicitly checks every `Bar`, envelope, and batch invariant before invoking the
existing constructor/helper. A structured core failure at the corresponding call is translated
only by the operation and already-known field. A core failure contradicting completed explicit
checks is an implementation defect and propagates; text matching is forbidden.

#### Complete-history conflict matrix

During the file-order pass, each row is indexed by record-version key, source-emission key, and
full admission key in that order. After that pass, logical histories are visited by ascending
logical key and revision.

| Condition | Record evidence | Field evidence |
|---|---:|---|
| repeated record-version key, whether exact duplicate or conflicting payload | later file record | null |
| repeated source-emission key | later file record | `source_sequence` |
| repeated full admission key | later file record | null |
| higher revision has earlier `available_at` | offending higher-revision record | `available_at` |
| higher revision has non-increasing `source_sequence` | offending higher-revision record | `source_sequence` |

All rows use `canonical_batch_conflict`. File order identifies the later duplicate only; it never
orders admitted output or changes the semantic fingerprint.

#### Local-file reader matrix

| Condition | Code | Evidence |
|---|---|---|
| wrong `path` or replay-window type | `invalid_type` | null/null |
| `O_NOFOLLOW` or `O_NONBLOCK` unavailable; initial `lstat` fails; path missing; symlink; initial non-regular path | `source_unreadable` | null/null |
| after an initial regular `lstat`, no-follow/nonblocking open fails or descriptor `fstat` is non-regular or identity-mismatched | `source_changed_during_read` | null/null |
| verified regular-descriptor read fails | `source_unreadable` | null/null |
| pre-read size exceeds the byte bound, or the bounded read obtains a 67,108,865th byte | `source_too_large` | null/null |
| descriptor/path identity, regular kind, size, byte count, or nanosecond mtime changes across capture | `source_changed_during_read` | null/null |
| stable capture succeeds | invoke the byte decoder matrix | unchanged |

An `OSError` during initial inspection is `source_unreadable`. Once initial `lstat` has proved a
regular path, an open failure is classified as `source_changed_during_read` because the path or
its accessibility changed before descriptor capture. An `OSError` while reading an already
verified regular descriptor is `source_unreadable`. An `OSError` or identity mismatch during
post-read descriptor/path stability checks is `source_changed_during_read`. The helper closes its
descriptor in `finally`; a close failure after successful capture propagates as an unexpected
implementation/platform error rather than changing the captured-data classification.

#### Dataset and source construction matrix

Direct `Phase1HistoricalDataset`, `Phase1HistoricalMarketDataSource`, and
`Phase1HistoricalSourceCursor` construction raises `TypeError`; it never returns
`HistoricalMarketDataError`.

| Operation/condition | Code | Evidence |
|---|---|---|
| source factory receives a non-exact dataset | `invalid_type` | null/null |
| exact dataset fails profile, type, byte bound, replay-window/selection equality, semantic fingerprint, ordering, or other derivable invariant | `invalid_dataset` | null/null |
| all invariants hold | construct source and private admission-key/record-byte indexes | no failure |

The source does not expose or use diagnostic raw-byte SHA/count as cursor or source authority.

#### `next_available_at` matrix

Validation order is exact cursor type, semantic binding, inner cursor shape/UTC, then source-owned
membership.

| Condition | Code | Evidence |
|---|---|---|
| cursor is neither null nor exact `Phase1HistoricalSourceCursor` | `invalid_cursor` | null/null |
| replay window, data SHA, or record count differs | `invalid_cursor` | null/null |
| inner cursor is malformed or non-UTC | `invalid_cursor` | null/null |
| `last_event=None` but an event was visible at `as_of` | `invalid_cursor` | null/null |
| non-null last admission key is absent or canonical record bytes differ | `invalid_cursor` | null/null |
| valid cursor or null | return the monotone timestamp/`None` contract above | no failure |

#### `admit` matrix

Static validation completes before `clock.now()` is called: exact clock port shape, cursor using
the preceding matrix, then limit. The clock is then read exactly once.

| Condition | Code | Evidence |
|---|---|---|
| clock lacks a callable `now` attribute | `invalid_type` | null/null |
| cursor fails the exact matrix above | `invalid_cursor` | null/null |
| limit is present but is not an exact positive `int` | `invalid_limit` | null/null |
| `clock.now()` returns a non-exact datetime, naive value, or non-zero UTC offset | `invalid_type` | null/null |
| clock cutoff is earlier than cursor cutoff | `clock_regressed` | null/null |
| valid call | return issued bound cursor and visible canonical tuple | no failure |

Attribute access failure or a non-callable `now` is the declared `invalid_type`. Once a callable
has been obtained, any exception raised by executing `now()` propagates unchanged; it is not
reclassified. An exact datetime with zero offset is normalized to the standard `UTC` object under
ADR 0004. The source call publishes nothing before the returned UTC value and complete candidate
have been validated.

Overall operation precedence is:

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
11. method-specific cursor, clock-port, limit, single clock read, and monotonicity validation.

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
- early-clock/empty-batch candidate, commit, replay, and first-availability traces;
- next-availability tests including equal timestamps, already-visible limited chunks, arbitrary
  positive limits, monotone wake-up, and same-cutoff draining;
- cursor replay, foreign binding, missing key, changed payload with same key, binary64 signed-zero,
  underflow, clock regression, exhaustion, and no-clock-advance tests;
- golden tests binding every normative failure-matrix row to exact code/record/field evidence;
- API tests proving no public future-event/history/path/iterator surface;
- filesystem symlink, non-regular, oversize, short-read/change-during-read, and read-failure tests
  on supported platforms, including regular-to-FIFO and regular-to-device swaps proving the
  nonblocking descriptor check;
- cross-process tests varying hash seed, locale, timezone, and environment while producing
  byte-identical canonical evidence; and
- failure-injection tests proving no partial publication.

At final verification, the exact candidate must pass repository `full`, current line/branch
coverage floors, byte-identical wheels, clean-wheel smoke, and exact-head CI.

### Design-review finding disposition

The first exact-SHA design review at `91b5ae61c2a946e140527dd042d0a50d0141dfde`
returned HOLD. This revision addresses:

- `ARCH51-001` / `DATA51-002`: dataset construction is factory-only and decoder-issued; the
  source no longer exposes or treats raw-byte diagnostics as source/cursor authority.
- `ARCH51-002` / `BACKTEST51-001`: a source-issued bound initial cursor with `last_event=None` has
  complete validity, commit, replay, scheduling, and first-admission semantics.
- `BACKTEST51-002`: next wake-up is monotone and same-cutoff draining is mandatory after a limited
  visible batch.
- `DATA51-001`: cursor membership resolves by admission key, compares canonical record bytes, and
  reconstructs with the source-owned event, including signed-zero distinction.
- `ARCH51-003` / `DATA51-003`: byte decoder, canonical conflict, filesystem, construction,
  scheduling, cursor, clock, limit, evidence, and unexpected-exception behavior are frozen in
  normative matrices.
- `DATA51-004`: capture requires both `O_NOFOLLOW` and `O_NONBLOCK`; a raced FIFO/device cannot
  block before `fstat`, and the exact raced non-regular classification and failure-injection
  evidence are frozen.

Closure requires new exact-SHA Architecture and Data/Backtest verdicts. This section does not
accept the ADR or authorize implementation.

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
