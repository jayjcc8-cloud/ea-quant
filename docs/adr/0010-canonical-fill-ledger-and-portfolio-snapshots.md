# ADR 0010: Canonical Fill Ledger and Portfolio Snapshots

Date: 2026-07-26

## Status

Proposed

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
- exact instrument-spec set ID/digest;
- the previous transaction digest, absent only for the first entry;
- the canonical ordered postings; and
- an exact `requires_reconciliation` flag.

`requires_reconciliation` is true when any Order ancestry required for the local chain is absent.
The flag does not block an economically complete external Fill from applying and does not invent
missing ancestry.

Posting order is literal and closed:

1. `portfolio.position`;
2. `external.inventory`;
3. `portfolio.cash`;
4. `external.settlement`; and
5. `portfolio.rounding`, present only for a non-zero residual.

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
| portfolio.cash | currency | `-side_sign * S` |
| external.settlement | currency | `side_sign * E` |
| portfolio.rounding | currency | `-side_sign * R`, omitted when `R == 0` |

These equations intentionally handle signed-price instruments without a special path. A negative
price reverses the currency flow through the signed settlement values while the quantity flow
continues to follow Order side.

Before append, postings are grouped by exact commodity and summed with integer-coefficient,
scale-aligned arithmetic independent of ambient Decimal context. Every commodity sum must be
exactly zero. Instrument and currency commodities never balance against each other.

If `E`, a next balance, or a transaction field cannot fit canonical bounds, application fails
before mutation with the applicable closed code. Failure to represent or accumulate the rounding
posting uses `ledger.rounding_unrepresentable`. No residual disappears and no partial append is
possible.

### Immutable snapshot

`PortfolioSnapshot` binds the run, instrument-spec set ID/digest, snapshot version, ledger sequence,
optional last entry ID/digest, and exact immutable tuples of:

- `CashBalance(currency, amount)`;
- `PositionBalance(instrument, quantity)`;
- `RoundingBalance(currency, amount)`; and
- `UnresolvedFillRef(fill_id, fill_digest)`.

Snapshot balance tuples are sorted by canonical currency code or `(venue, symbol)`. Unresolved
Fill references are sorted by canonical Fill economic identity. Duplicate keys fail. Exact zero
cash, position, and rounding balances are omitted, so a round trip to zero removes the balance but
not its immutable transaction history.

Cash and position balances are revalidated against the ledger's exact specification set after
every application. Rounding balances preserve exact residuals and are not silently quantized to a
currency unit. All public tuples and nested values are immutable and exact-type validated.

The ledger exposes only its current immutable snapshot and immutable transaction tuple. It never
returns internal dictionaries, sets, or mutable collections.

### Canonical evidence and digest chain

Transactions, snapshots, and application outcomes each have one versioned canonical JSON byte
encoding and domain-separated SHA-256 digest. Encoding:

- uses exact ASCII keys and values;
- sorts object keys and all set-derived tuples by the closed domain order;
- encodes integers without invoking Python's decimal-string digit limit;
- serializes `CanonicalDecimal.text` exactly; and
- never includes exception text, object representation, hash, memory address, mapping insertion
  order, local path, locale, timezone, wall time, or ambient Decimal state.

Every transaction includes the prior transaction digest. A snapshot includes the last entry ID and
digest, so the complete ordered transaction chain is bound without making mutable storage part of
the inner contract.

### Replay, conflict, and atomicity

The ledger retains non-evicting private indexes for:

- Fill ID -> canonical Fill digest and original transaction;
- source-scoped fact dedup key -> canonical Fill digest and original transaction; and
- ledger-entry ID -> canonical transaction digest.

For one exact Fill:

1. wrong runtime type, run, or specification lineage fails validation before replay lookup;
2. an existing Fill ID or fact key bound to different canonical Fill bytes returns
   `ledger.conflict`;
3. an exact existing Fill ID and fact key bound to the same digest returns `ledger.duplicate`;
4. otherwise the ledger constructs and validates the complete transaction, complete next replay
   indexes, and complete next snapshot before committing them together; and
5. successful commit returns `ledger.applied`.

Duplicate and conflict outcomes do not advance entry sequence or snapshot version and do not
change balances, transactions, indexes, or unresolved references. They report the ledger's current
snapshot and, for an exact duplicate, the original transaction. A conflict reports submitted and
existing digests but never substitutes the existing Fill payload.

`LedgerApplyOutcome` has exact variants for applied, duplicate, conflict, unbalanced,
rounding-unrepresentable, and arithmetic-overflow results. Structural type/range/lineage errors use
one `PortfolioLedgerError` carrying only the permitted closed `OutcomeCode`; no logic branches on
exception text.

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
