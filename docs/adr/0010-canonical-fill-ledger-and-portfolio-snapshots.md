# ADR 0010: Canonical Fill Ledger and Portfolio Snapshots

Date: 2026-07-26

## Status

Accepted

## Context

Accepted ADR 0008 makes the portfolio append-only ledger the sole authority for cash, positions,
and later P&L state. Issues #35, #37, and #39 implemented the exact economics, identities, outcomes,
execution messages, and `Fill` needed at the ledger boundary. Issue #41 implemented deterministic
root selection, but there is still no state owner that can apply an accepted Fill exactly once or
publish the immutable snapshot required by risk and runtime.

A general accounting framework does not fit this boundary. Beancount 3.2.3 and Django Ledger 0.8.4
bring GPL licenses, parser/ORM/reporting models, native or web-framework dependencies, and
translations that do not preserve the project's Fill, ingress, specification, and outcome
identities. EigenLedger 2.1.6 is a float/NumPy/analytics and market-data stack rather than a
transaction authority. The project already has the smaller primitives required to implement the
accepted contract without a new dependency.

This decision covers only canonical Fill application and immutable snapshots. Initial funding and
authorized reconciliation adjustments require their own exact messages and authorization
contract; they cannot be smuggled through an arbitrary posting API.

## Decision

### Ownership and dependency direction

`ea.core.portfolio` defines the dependency-neutral immutable values exchanged across portfolio,
risk, runtime, audit, reconciliation, and result boundaries:

- closed account and commodity values;
- postings and Fill-derived ledger transactions;
- immutable cash, position, rounding, and unresolved-Fill snapshot values;
- ledger application outcomes; and
- canonical bytes and domain-separated SHA-256 digests.

The portfolio domain remains the semantic owner of those values. They live in `core` only to avoid
peer-stage imports under ADR 0003.

`ea.portfolio.ledger` owns the single stateful append policy. It may import `ea.core` only. It does
not import runtime, risk, execution policy implementations, configuration, experiments,
composition, CLI, data/backtest/paper packages, persistence, audit implementations, result
adapters, SDKs, or network/filesystem code. No other component may mutate its state or append a
transaction.

The ledger is an inner in-memory authority, not a durability adapter. A later consumer-owned
persistence port may persist its immutable transaction and snapshot bytes without changing their
semantics.

### Exact run and specification binding

One ledger is factory-created for exactly:

- one exact `RunId`; and
- one exact `InstrumentExecutionSpecSet`, including its ID and canonical digest.

Construction is side-effect free and produces version zero:

- ledger sequence `0`;
- snapshot version `0`;
- no previous entry;
- no cash, position, rounding, or unresolved-Fill balance; and
- empty replay indexes.

The ledger never replaces its specification set. Every Fill is revalidated against the ledger's
exact specification ID, set ID, set digest, price/quantity grids, price domain, settlement
currency, and Phase 1 zero-fee contract through existing canonical execution helpers.

At factory time the ledger derives one currency-quantum registry from the complete specification
set. Every specification sharing one `SettlementCurrency` MUST declare the same exact
`currency_quantum`. A disagreement fails construction with
`PortfolioLedgerError(OutcomeCode.CONFLICTING_ID)` before version-zero state exists. This makes
cash validation currency-wide and independent of which instrument happens to update the balance
first.

### Closed commodities and accounts

There are two exact commodity variants:

- `InstrumentCommodity(instrument)` for position quantity; and
- `CurrencyCommodity(currency)` for settlement amounts.

There is one closed `LedgerAccountKind`:

- `portfolio.position`;
- `external.inventory`;
- `portfolio.cash`;
- `external.settlement`; and
- `portfolio.rounding`.

Position and external-inventory accounts accept only an `InstrumentCommodity`. Cash,
external-settlement, and rounding accounts accept only a `CurrencyCommodity`. Account,
commodity, and amount carriers require exact runtime types; bool, subclasses, mappings, float,
`Decimal`, and coercion fail.

`LedgerPosting` contains one account, one exact commodity, and one non-zero `CanonicalDecimal`
amount. Zero postings are forbidden. Portfolio cash postings must be exact multiples of the
instrument specification's currency quantum. Portfolio position postings must be exact multiples
of its quantity quantum. External settlement and rounding postings retain exact transaction
evidence and may be finer than the currency quantum.

### Fill-derived transaction

The ledger, not a caller, assigns the next entry identity:

`EconomicId(run_id, EconomicOwnerKind.LEDGER_ENTRY, next_ledger_sequence)`.

The first entry sequence is `1`, and every successful append advances ledger sequence and snapshot
version together by exactly one. Entry sequence never chooses economic order; runtime already
chooses the Fill's causal order.

One `LedgerTransaction` binds:

- run and ledger-entry IDs;
- canonical Fill ID and Fill digest;
- the Fill's source-scoped `FactDedupKey`;
- occurrence time and true provenance;
- optional known order/correlation/causation IDs without fabrication;
- exact instrument specification ID and instrument-spec set ID/digest;
- the previous transaction digest, absent only for the first entry;
- the canonical ordered postings; and
- an exact `requires_reconciliation` flag.

`requires_reconciliation` is exactly:

```text
fill.order_id is None
or fill.correlation_id is None
or fill.causation_id is None
```

`client_submission_key` and `venue_order_id` remain valuable correlation evidence but are not
required when all three local ancestry IDs are present. An applied unresolved Fill adds exactly
one `UnresolvedFillRef(fill_id, fill_digest)`. This Fill-only API never removes that reference;
only a future separately authorized reconciliation transition may do so. Duplicate and conflict
paths never add, remove, or alter unresolved references. The flag does not block an economically
complete external Fill from applying and does not invent missing ancestry.

Posting order is literal and closed:

1. `portfolio.position`;
2. `external.inventory`;
3. `portfolio.cash`, present only when settled amount `S != 0`;
4. `external.settlement`, present only when exact notional `E != 0`; and
5. `portfolio.rounding`, present only when residual `R != 0`.

Position and inventory postings are always present because a canonical Fill quantity is strictly
positive. Optional currency postings are filtered independently and retain the relative order
above. Therefore a zero-price Fill contains only the two balanced instrument postings, and a
sub-quantum Fill with `S == 0` still contains its balancing external-settlement and rounding
postings. No zero posting is ever constructed.

No caller-supplied posting, account, transaction ID, digest, ordering key, or balance is accepted.

### Exact side, settlement, and balance equations

`settle_execution` is the only Fill notional settlement boundary. Let:

- `q` be the positive canonical Fill quantity;
- `S` be its settled currency amount;
- `R` be its exact rounding residual; and
- `E = S + R` be the exact pre-settlement notional representable as `CanonicalDecimal`.

Let side sign be `+1` for buy and `-1` for sell. Generated postings are:

| Account | Commodity | Amount |
|---|---|---:|
| portfolio.position | instrument | `side_sign * q` |
| external.inventory | instrument | `-side_sign * q` |
| portfolio.cash | currency | `-side_sign * S`, omitted when `S == 0` |
| external.settlement | currency | `side_sign * E`, omitted when `E == 0` |
| portfolio.rounding | currency | `-side_sign * R`, omitted when `R == 0` |

These equations intentionally handle signed-price instruments without a special path. A negative
price reverses the currency flow through the signed settlement values while the quantity flow
continues to follow Order side.

Before append, postings are grouped by exact commodity and summed with integer-coefficient,
scale-aligned arithmetic independent of ambient Decimal context. Every commodity sum must be
exactly zero. Instrument and currency commodities never balance against each other.

If `E`, a next balance, or a transaction field cannot fit canonical bounds, application follows
the exact failure table below before mutation. No residual disappears and no partial append is
possible.

### Immutable snapshot

`PortfolioSnapshot` binds the run, instrument-spec set ID/digest, snapshot version, ledger sequence,
optional last entry ID/digest, and exact immutable tuples of:

- `CashBalance(currency, currency_quantum, amount)`;
- `PositionBalance(instrument, quantity_quantum, quantity)`;
- `RoundingBalance(currency, amount)`; and
- `UnresolvedFillRef(fill_id, fill_digest)`.

Snapshot balance tuples are sorted by canonical currency code or `(venue, symbol)`. Unresolved
Fill references are sorted by canonical Fill economic identity. Duplicate keys fail. Exact zero
cash, position, and rounding balances are omitted, so a round trip to zero removes the balance but
not its immutable transaction history.

Cash and position balances are revalidated against the ledger's currency registry and exact
specification set after every application. Their embedded quanta MUST equal that registry/spec
value. Rounding balances preserve exact residuals and are not silently quantized to a currency
unit. All public tuples and nested values are immutable and exact-type validated.

Snapshot invariants are literal:

- `snapshot_version == ledger_sequence`;
- version zero has both values `0`, both last-entry fields `None`, and every tuple empty;
- a non-zero version has both last-entry fields present;
- the last entry is an exact `EconomicId` for the same run and
  `EconomicOwnerKind.LEDGER_ENTRY`;
- `last_entry_id.owner_sequence == ledger_sequence`;
- `last_transaction_sha256` equals the digest of the last transaction in the ledger;
- cash, position, rounding, and unresolved keys are unique and in canonical order; and
- no cash, position, or rounding tuple contains an exact zero balance.

The ledger exposes only its current immutable snapshot and immutable transaction tuple. It never
returns internal dictionaries, sets, or mutable collections.

### Canonical evidence and digest chain

The three top-level canonical types use exactly:

| Type | `schema_version` | `canonicalization` | digest domain bytes |
|---|---:|---|---|
| `LedgerTransaction` | 1 | `ea-ledger-transaction-v1` | `b"ea.ledger-transaction.v1\0"` |
| `PortfolioSnapshot` | 1 | `ea-portfolio-snapshot-v1` | `b"ea.portfolio-snapshot.v1\0"` |
| `LedgerApplyOutcome` | 1 | `ea-ledger-apply-outcome-v1` | `b"ea.ledger-apply-outcome.v1\0"` |

Canonical JSON objects sort keys lexicographically and contain no whitespace. Encoding:

- uses exact ASCII keys and values;
- sorts object keys and all set-derived tuples by the closed domain order;
- encodes integers without invoking Python's decimal-string digit limit;
- encodes exact booleans as JSON `true` or `false`;
- encodes every absent optional field as JSON `null`;
- serializes `CanonicalDecimal.text` exactly; and
- never includes exception text, object representation, hash, memory address, mapping insertion
  order, local path, locale, timezone, wall time, or ambient Decimal state.

Embedded documents are literal:

- economic ID:
  `{"owner_kind": value, "owner_sequence": integer, "run_id": UUID text}`;
- instrument: `{"symbol": symbol, "venue": venue_code}`;
- currency commodity: `{"currency": currency_code, "kind": "currency"}`;
- instrument commodity:
  `{"instrument": instrument_document, "kind": "instrument"}`;
- posting:
  `{"account": account_value, "amount": canonical_decimal_text, "commodity": commodity_document}`;
- fact key:
  `{"dedup_identity": {"kind": "external_id", "value": text} | {"kind": "source_native_sequence", "value": integer}, "source_namespace": text}`;
- provenance:
  `{"provenance_id": text, "source_payload_sha256": lowercase_sha256}`;
- cash balance:
  `{"amount": text, "currency": code, "currency_quantum": text}`;
- position balance:
  `{"instrument": instrument_document, "quantity": text, "quantity_quantum": text}`;
- rounding balance: `{"amount": text, "currency": code}`;
- unresolved Fill:
  `{"fill_id": economic_id_document, "fill_sha256": lowercase_sha256}`; and
- existing ledger binding:
  `{"entry_id": economic_id_document, "fill_id": economic_id_document, "fill_sha256": lowercase_sha256, "transaction_sha256": lowercase_sha256}`.

`canonical_UTC_text` is exactly `%Y-%m-%dT%H:%M:%S.%fZ` after the existing exact UTC validator.
Every digest is `SHA-256(digest_domain_bytes + canonical_document_bytes)`.

The exact transaction document is:

```text
{
  "canonicalization": "ea-ledger-transaction-v1",
  "causation_id": economic_id_document | null,
  "client_submission_key": lowercase_sha256 | null,
  "correlation_id": economic_id_document | null,
  "entry_id": economic_id_document,
  "fact_key": fact_key_document,
  "fill_id": economic_id_document,
  "fill_sha256": lowercase_sha256,
  "instrument_specification_id": text,
  "instrument_spec_set_id": text,
  "instrument_spec_set_sha256": lowercase_sha256,
  "ledger_sequence": integer,
  "message_type": "ledger_transaction",
  "occurred_at": canonical_UTC_text,
  "order_id": economic_id_document | null,
  "postings": [posting_document, ...],
  "previous_transaction_sha256": lowercase_sha256 | null,
  "provenance": provenance_document,
  "requires_reconciliation": boolean,
  "run_id": UUID_text,
  "schema_version": 1,
  "venue_order_id": visible_ASCII_text | null
}
```

The exact snapshot document is:

```text
{
  "canonicalization": "ea-portfolio-snapshot-v1",
  "cash_balances": [cash_balance_document, ...],
  "instrument_spec_set_id": text,
  "instrument_spec_set_sha256": lowercase_sha256,
  "last_entry_id": economic_id_document | null,
  "last_transaction_sha256": lowercase_sha256 | null,
  "ledger_sequence": integer,
  "message_type": "portfolio_snapshot",
  "position_balances": [position_balance_document, ...],
  "rounding_balances": [rounding_balance_document, ...],
  "run_id": UUID_text,
  "schema_version": 1,
  "snapshot_version": integer,
  "unresolved_fills": [unresolved_fill_document, ...]
}
```

The exact outcome document is:

```text
{
  "after_snapshot_version": integer,
  "before_snapshot_version": integer,
  "canonicalization": "ea-ledger-apply-outcome-v1",
  "code": outcome_code,
  "conflict_kind": conflict_kind | null,
  "entry_index_binding": existing_ledger_binding | null,
  "fact_index_binding": existing_ledger_binding | null,
  "failure_stage": failure_stage | null,
  "fill_index_binding": existing_ledger_binding | null,
  "message_type": "ledger_apply_outcome",
  "run_id": UUID_text,
  "schema_version": 1,
  "snapshot_sha256": lowercase_sha256,
  "submitted_fill_id": economic_id_document,
  "submitted_fill_sha256": lowercase_sha256,
  "transaction_entry_id": economic_id_document | null,
  "transaction_sha256": lowercase_sha256 | null
}
```

The immutable Python `LedgerTransaction` and `PortfolioSnapshot` carriers expose exactly the
semantic fields listed in their respective canonical documents. `LedgerApplyOutcome` exposes the
semantic fields in its canonical document plus the exact current/post-operation
`PortfolioSnapshot` object and an optional exact `LedgerTransaction` object. The latter is the new
transaction for applied, the original transaction for duplicate, and `None` for conflict or
failure. Those nested objects are not recursively embedded in outcome JSON: their canonical
digest and, for a transaction, entry ID bind them through `snapshot_sha256`,
`transaction_sha256`, and `transaction_entry_id`. All carrier fields use exact runtime types and
immutable tuples; no mapping form is accepted as a carrier.

For `ledger.applied`, transaction fields are the new transaction and
`after_snapshot_version == before_snapshot_version + 1`. For `ledger.duplicate`, transaction
fields are the original transaction and both versions equal the current snapshot version. For
conflict or failure, both transaction fields are null. Every outcome's snapshot digest is the
digest of the exact current/post-operation snapshot object it carries.

Applied/duplicate outcomes have null conflict/binding/failure fields. Conflict outcomes have one
non-null conflict kind and the exact index bindings available under the table below, with null
failure stage. Failure outcomes have a non-null failure stage and null conflict/binding fields.

Every transaction includes the prior transaction digest. A snapshot includes the last entry ID and
digest, so the complete ordered transaction chain is bound without making mutable storage part of
the inner contract.

### Replay, conflict, and atomicity

The ledger retains non-evicting private indexes for:

- Fill ID -> canonical Fill digest and original transaction;
- source-scoped fact dedup key -> canonical Fill digest and original transaction; and
- ledger-entry ID -> canonical transaction digest.

The closed `LedgerConflictKind` values are:

- `fill_id_collision`;
- `fact_key_collision`;
- `fill_and_fact_collision`;
- `cross_index_collision`;
- `index_inconsistent`; and
- `entry_id_occupied`.

For replay lookup, `I` is the Fill-ID binding and `F` is the fact-key binding. Classification is
literal and does not depend on dictionary lookup order:

| `I` | `F` | Condition | Result |
|---|---|---|---|
| absent | absent | — | genuinely new identity; continue validation |
| present | absent | `I.fill_sha256 != submitted` | `ledger.conflict / fill_id_collision` |
| absent | present | `F.fill_sha256 != submitted` | `ledger.conflict / fact_key_collision` |
| present | absent | `I.fill_sha256 == submitted` | `ledger.conflict / index_inconsistent` |
| absent | present | `F.fill_sha256 == submitted` | `ledger.conflict / index_inconsistent` |
| present | present | bindings reference different entries | `ledger.conflict / cross_index_collision` |
| present | present | same entry but stored Fill/transaction binding fields disagree | `ledger.conflict / index_inconsistent` |
| present | present | same consistent entry and both digests equal submitted | `ledger.duplicate` |
| present | present | same consistent entry and stored Fill digest differs from submitted | `ledger.conflict / fill_and_fact_collision` |

Every conflict outcome includes `fill_index_binding` and/or `fact_index_binding` exactly as found.
No single unspecified "existing digest" is selected. An exact duplicate requires both indexes to
name the same original entry and digest.

After two absent replay lookups, the ledger derives the next entry ID. An already occupied next
entry ID returns `ledger.conflict / entry_id_occupied`, includes the occupied transaction as
`entry_index_binding`, and carries both identity-index bindings as null. Other conflict kinds have
`entry_index_binding == null`. This case is fail-closed internal-state evidence, not permission to
skip a sequence.

Apply precedence is exact:

1. require an exact `Fill` carrier;
2. require `fill.run_id == ledger.run_id`;
3. compute canonical Fill bytes and digest;
4. classify the Fill-ID and fact-key indexes by the table above;
5. for a genuinely new identity, validate exact ledger specification lineage, grids, price
   domain, fees, and economics;
6. require that the next ledger-entry sequence fits the unsigned 64-bit `EconomicId` range;
7. settle the Fill and construct exact `S`, `R`, `E`, filtered postings, and commodity sums;
8. construct complete next balances, transaction, replay indexes, unresolved references, snapshot,
   canonical bytes, and digests in local state;
9. commit the complete private state once; and
10. return `ledger.applied`.

Identity collision therefore wins over changed specification bytes for an existing Fill ID or fact
key. Specification/economic validation applies only to genuinely new identities.

Duplicate and conflict outcomes do not advance entry sequence or snapshot version and do not
change balances, transactions, indexes, or unresolved references. They report the ledger's current
snapshot and, for an exact duplicate, the original transaction. A conflict reports submitted and
existing digests but never substitutes the existing Fill payload.

`PortfolioLedgerError` accepts only:

- `validation.invalid_type`;
- `validation.out_of_range`;
- `validation.not_quantized`;
- `validation.price_domain`; and
- `validation.conflicting_id`.

It is used for factory/carrier/run/specification/grid/domain/fee violations and translates an
existing structured validation error by its exact code, never by text. Identity replay
classification already completed before a genuinely new Fill can raise a specification error.

The closed `LedgerFailureStage` and result mapping are:

| Condition | API result | `failure_stage` | Mutation |
|---|---|---|---|
| ledger sequence is already uint64 max | outcome `validation.out_of_range` | `ledger_sequence_exhausted` | none |
| `settle_execution` amount overflow | outcome `validation.arithmetic_overflow` | `settlement_arithmetic_overflow` | none |
| `settle_execution` residual unrepresentable | outcome `ledger.rounding_unrepresentable` | `settlement_rounding_unrepresentable` | none |
| exact `E = S + R` unrepresentable | outcome `validation.arithmetic_overflow` | `exact_notional_overflow` | none |
| next cash balance unrepresentable | outcome `validation.arithmetic_overflow` | `cash_balance_overflow` | none |
| next position balance unrepresentable | outcome `validation.arithmetic_overflow` | `position_balance_overflow` | none |
| next rounding balance unrepresentable | outcome `ledger.rounding_unrepresentable` | `rounding_balance_overflow` | none |
| any commodity sum is non-zero | outcome `ledger.unbalanced` | `commodity_unbalanced` | none |

The table order is normative evaluation precedence: conditions are evaluated from top to bottom,
and the first applicable row is the only returned failure. Candidate construction MUST therefore
check cash balance, then position balance, then rounding balance, and only then commodity balance,
even when more than one later condition would also be true. No implementation may select a
failure by mapping order, exception timing, or whichever candidate value it happens to construct
first.

The exact allowed outcome-code set is:

- `ledger.applied`;
- `ledger.duplicate`;
- `ledger.conflict`;
- `ledger.unbalanced`;
- `ledger.rounding_unrepresentable`;
- `validation.arithmetic_overflow`; and
- `validation.out_of_range`.

This decision does not invent a ledger arithmetic-overflow code. Structural exceptions and
returned outcomes are disjoint according to the precedence and table above.

The implementation builds every candidate value in local immutable structures. Only after all
validation and canonical encoding succeeds does it swap the complete private state. A failure at
any point leaves all observable state byte-for-byte unchanged.

### Public authority boundary

The public ledger API has:

- a factory for one empty run/specification-bound ledger;
- `snapshot`;
- `transactions`; and
- `apply_fill(fill)`.

It has no arbitrary append, posting factory, balance setter, initial-funding shortcut,
reconciliation adjustment, restore/import, mutable collection, persistence callback, audit sink,
clock, mode, runtime callback, risk decision, order creation, matcher, venue, adapter, SDK, I/O, or
thread/async API.

Negative cash is representable accounting state. It is not implicit permission to trade; later
risk policy decides whether an intent may proceed. Initial funding requires a separately
authorized reconciliation-adjustment decision and remains outside this ADR.

## Consequences

Positive:

- Every accepted Fill has one exact, balanced, replay-safe accounting application.
- Risk and later runtime code receive one immutable canonical snapshot instead of shadow positions.
- Rounding, signed-price behavior, unresolved correlation, and duplicate facts remain explicit.
- The inner authority adds no dependency or persistence coupling.

Negative:

- A useful funded backtest still requires the separately authorized initial-state/reconciliation
  slice.
- Non-evicting replay indexes retain all Fill identities for the run.
- Multi-commodity postings are more explicit than updating two dictionaries, but that explicitness
  is required to prove balance and causation.

## Alternatives rejected

### Use a general accounting or portfolio framework

Rejected because its parser/ORM/reporting/float model, dependencies, license, or I/O authority does
not preserve the accepted Fill and replay contract.

### Update cash and position dictionaries without postings

Rejected because it cannot prove multi-commodity balance, retain counterparty and rounding
evidence, or produce an append-only causal record.

### Allow callers to submit arbitrary balanced postings

Rejected because it bypasses Fill and future reconciliation authorization, creates a second
accounting policy surface, and permits silent initial funding or correction.

### Drop or quantize the rounding residual

Rejected by ADR 0008. The residual is explicit balanced evidence or the entire application fails.

### Reject an economically complete Fill with unresolved Order ancestry

Rejected because external execution evidence has higher authority than incomplete local
correlation. The Fill applies once and is flagged for reconciliation.

## Required evidence

- literal buy, sell, signed-price, exact-zero-residual, and non-zero-residual posting vectors;
- property tests proving exact zero sums per commodity and side symmetry;
- exact replay after zero or many later transactions with no second mutation;
- Fill-ID, fact-key, and transaction-identity conflict tests;
- mutation snapshots around every validation, overflow, balance, and encoding failure;
- maximum coefficient, grid, negative-value, signed-zero, and ambient Decimal tests;
- unresolved-correlation application without fabricated ancestry;
- canonical bytes/digests and transaction-chain golden vectors;
- cross-process invariance under hash seed, timezone, locale, CWD, Decimal context, and input order;
- public API and AST import-boundary tests proving the absence of arbitrary append and external
  effects; and
- full repository verification plus exact-head CI and SHA-bound Architecture, Portfolio/Ledger,
  and Verification reports.
