from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from ea.core import (
    CanonicalDecimal,
    EconomicId,
    EconomicOwnerKind,
    ExternalFactId,
    FactProvenance,
    FactProvenanceId,
    Instrument,
    InstrumentExecutionSpec,
    InstrumentSpecId,
    InstrumentSpecSetId,
    LedgerTransaction,
    OrderSide,
    OutcomeCode,
    PriceDomain,
    RunId,
    SettlementCurrency,
    Sha256Digest,
    SourceNamespace,
    VenueId,
    build_instrument_spec_set,
    canonical_ledger_transaction_bytes,
    canonical_portfolio_snapshot_bytes,
    create_fill,
    create_trade_execution_fact,
)
from ea.core.execution_messages import Fill
from ea.core.reconciliation import ReconciliationTransaction
from ea.portfolio import create_portfolio_ledger

RUN_ID = RunId("12345678-1234-4234-8234-123456789abc")
INSTRUMENT = Instrument(VenueId("XNAS"), "AAPL")
SOURCE = SourceNamespace("property.sim")
PROVENANCE = FactProvenance(
    FactProvenanceId("phase1.property.v1"),
    Sha256Digest("7" * 64),
)
TIME = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
SPEC = InstrumentExecutionSpec(
    instrument=INSTRUMENT,
    specification_id=InstrumentSpecId("xnas.aapl.property.v1"),
    price_quantum=CanonicalDecimal("0.001"),
    quantity_quantum=CanonicalDecimal("1"),
    settlement_currency=SettlementCurrency("USD"),
    currency_quantum=CanonicalDecimal("0.01"),
    contract_multiplier=CanonicalDecimal("1"),
    price_domain=PriceDomain.SIGNED,
)
SPEC_SET = build_instrument_spec_set(
    InstrumentSpecSetId("phase1.property.v1"),
    [SPEC],
)


def _scaled_text(coefficient: int, scale: int) -> str:
    if coefficient == 0:
        return "0"
    while scale and coefficient % 10 == 0:
        coefficient //= 10
        scale -= 1
    sign = "-" if coefficient < 0 else ""
    digits = str(abs(coefficient))
    if scale == 0:
        return sign + digits
    if len(digits) <= scale:
        digits = "0" * (scale + 1 - len(digits)) + digits
    split = len(digits) - scale
    return f"{sign}{digits[:split]}.{digits[split:]}"


def _fill(
    *,
    sequence: int,
    side: OrderSide,
    price_mills: int,
    quantity: int,
) -> Fill:
    fact = create_trade_execution_fact(
        source_namespace=SOURCE,
        dedup_identity=ExternalFactId(f"trade-{sequence}"),
        occurred_at=TIME + timedelta(microseconds=sequence),
        provenance=PROVENANCE,
        spec_set=SPEC_SET,
        instrument=INSTRUMENT,
        side=side,
        quantity=CanonicalDecimal(str(quantity)),
        price=CanonicalDecimal(_scaled_text(price_mills, 3)),
    )
    return create_fill(
        fill_id=EconomicId(RUN_ID, EconomicOwnerKind.EXECUTION_FILL, sequence),
        fact=fact,
        spec_set=SPEC_SET,
    )


def _commodity_sums(fill: Fill) -> dict[object, int]:
    ledger = create_portfolio_ledger(RUN_ID, SPEC_SET)
    outcome = ledger.apply_fill(fill)
    assert outcome.code is OutcomeCode.LEDGER_APPLIED
    transaction = ledger.transactions[0]
    scales: dict[object, int] = {}
    for posting in transaction.postings:
        scales[posting.commodity] = max(scales.get(posting.commodity, 0), posting.amount.scale)
    totals = {commodity: 0 for commodity in scales}
    for posting in transaction.postings:
        totals[posting.commodity] += posting.amount.coefficient * (
            10 ** (scales[posting.commodity] - posting.amount.scale)
        )
    return totals


@given(
    side=st.sampled_from((OrderSide.BUY, OrderSide.SELL)),
    price_mills=st.integers(min_value=-100_000, max_value=100_000),
    quantity=st.integers(min_value=1, max_value=1_000),
)
@settings(max_examples=80)
def test_every_fill_is_exactly_balanced_per_commodity(
    side: OrderSide,
    price_mills: int,
    quantity: int,
) -> None:
    totals = _commodity_sums(
        _fill(
            sequence=1,
            side=side,
            price_mills=price_mills,
            quantity=quantity,
        )
    )
    assert totals
    assert set(totals.values()) == {0}


@given(
    price_mills=st.integers(min_value=-100_000, max_value=100_000),
    quantity=st.integers(min_value=1, max_value=1_000),
)
@settings(max_examples=80)
def test_buy_sell_side_symmetry_round_trips_every_balance(
    price_mills: int,
    quantity: int,
) -> None:
    ledger = create_portfolio_ledger(RUN_ID, SPEC_SET)
    buy = _fill(
        sequence=1,
        side=OrderSide.BUY,
        price_mills=price_mills,
        quantity=quantity,
    )
    sell = _fill(
        sequence=2,
        side=OrderSide.SELL,
        price_mills=price_mills,
        quantity=quantity,
    )
    assert ledger.apply_fill(buy).code is OutcomeCode.LEDGER_APPLIED
    assert ledger.apply_fill(sell).code is OutcomeCode.LEDGER_APPLIED
    assert ledger.snapshot.cash_balances == ()
    assert ledger.snapshot.position_balances == ()
    assert ledger.snapshot.rounding_balances == ()


@given(later_count=st.integers(min_value=0, max_value=20))
@settings(max_examples=21)
def test_replay_after_any_bounded_later_history_never_mutates(
    later_count: int,
) -> None:
    ledger = create_portfolio_ledger(RUN_ID, SPEC_SET)
    original = _fill(
        sequence=1,
        side=OrderSide.BUY,
        price_mills=10_005,
        quantity=3,
    )
    ledger.apply_fill(original)
    for offset in range(later_count):
        ledger.apply_fill(
            _fill(
                sequence=offset + 2,
                side=OrderSide.SELL if offset % 2 else OrderSide.BUY,
                price_mills=1_000 + offset,
                quantity=offset + 1,
            )
        )
    before = canonical_portfolio_snapshot_bytes(ledger.snapshot)
    transactions = tuple(_ledger_transaction_bytes(item) for item in ledger.transactions)
    duplicate = ledger.apply_fill(original)
    assert duplicate.code is OutcomeCode.LEDGER_DUPLICATE
    assert canonical_portfolio_snapshot_bytes(ledger.snapshot) == before
    assert tuple(_ledger_transaction_bytes(item) for item in ledger.transactions) == transactions


def _ledger_transaction_bytes(
    item: LedgerTransaction | ReconciliationTransaction,
) -> bytes:
    assert type(item) is LedgerTransaction
    return canonical_ledger_transaction_bytes(item)
