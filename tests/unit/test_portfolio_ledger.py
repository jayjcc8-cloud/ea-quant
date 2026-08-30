from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import cast

import pytest

from ea.core import (
    CanonicalDecimal,
    EconomicId,
    EconomicOwnerKind,
    ExistingLedgerBinding,
    ExternalFactId,
    FactProvenance,
    FactProvenanceId,
    FeeCode,
    FeeEntry,
    Instrument,
    InstrumentExecutionSpec,
    InstrumentSpecId,
    InstrumentSpecSetId,
    LedgerAccountKind,
    LedgerConflictKind,
    LedgerFailureStage,
    LedgerTransaction,
    OpenReconciliationRef,
    OrderSide,
    OutcomeCode,
    PortfolioLedgerError,
    PortfolioSnapshot,
    PriceDomain,
    RunId,
    SettlementCurrency,
    Sha256Digest,
    SourceNamespace,
    VenueId,
    VenueOrderId,
    build_instrument_spec_set,
    canonical_ledger_apply_outcome_bytes,
    canonical_ledger_transaction_bytes,
    canonical_portfolio_snapshot_bytes,
    create_fill,
    create_trade_execution_fact,
    fill_digest,
    instrument_spec_set_digest,
    ledger_apply_outcome_digest,
    ledger_transaction_digest,
    portfolio_snapshot_digest,
)
from ea.core.economics import EconomicValidationError
from ea.core.execution import InstrumentExecutionSpecSet
from ea.core.execution_messages import Fill
from ea.core.initial_funding import (
    InitialFundingSpec,
    InitialFundingTransaction,
    canonical_initial_funding_transaction_bytes,
)
from ea.core.reconciliation import (
    ReconciliationTransaction,
    canonical_reconciliation_transaction_bytes,
)
from ea.portfolio import create_portfolio_ledger
from ea.portfolio.ledger import PortfolioLedger

RUN_ID = RunId("12345678-1234-4234-8234-123456789abc")
OTHER_RUN_ID = RunId("87654321-4321-4321-8321-cba987654321")
INSTRUMENT = Instrument(VenueId("XNAS"), "AAPL")
USD = SettlementCurrency("USD")
SOURCE = SourceNamespace("sim.primary")
TIME = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
PROVENANCE = FactProvenance(
    FactProvenanceId("phase1.simulator.v1"),
    Sha256Digest("3" * 64),
)


def _spec(
    *,
    instrument: Instrument = INSTRUMENT,
    specification_id: str = "xnas.aapl.v1",
    price_quantum: str = "0.01",
    quantity_quantum: str = "1",
    currency_quantum: str = "0.01",
    multiplier: str = "1",
    price_domain: PriceDomain = PriceDomain.SIGNED,
) -> InstrumentExecutionSpec:
    return InstrumentExecutionSpec(
        instrument=instrument,
        specification_id=InstrumentSpecId(specification_id),
        price_quantum=CanonicalDecimal(price_quantum),
        quantity_quantum=CanonicalDecimal(quantity_quantum),
        settlement_currency=USD,
        currency_quantum=CanonicalDecimal(currency_quantum),
        contract_multiplier=CanonicalDecimal(multiplier),
        price_domain=price_domain,
    )


def _spec_set(*specifications: InstrumentExecutionSpec) -> InstrumentExecutionSpecSet:
    selected = specifications or (_spec(),)
    return build_instrument_spec_set(
        InstrumentSpecSetId("phase1.test.v1"),
        selected,
    )


def _id(kind: EconomicOwnerKind, sequence: int, *, run_id: RunId = RUN_ID) -> EconomicId:
    return EconomicId(run_id, kind, sequence)


def _fill(
    spec_set: InstrumentExecutionSpecSet,
    *,
    fill_sequence: int,
    dedup: str,
    side: OrderSide = OrderSide.BUY,
    quantity: str = "1",
    price: str = "10",
    resolved: bool = True,
    occurred_at: datetime = TIME,
    run_id: RunId = RUN_ID,
) -> Fill:
    order_id = _id(EconomicOwnerKind.EXECUTION_ORDER, 6, run_id=run_id)
    fact = create_trade_execution_fact(
        source_namespace=SOURCE,
        dedup_identity=ExternalFactId(dedup),
        occurred_at=occurred_at,
        provenance=PROVENANCE,
        spec_set=spec_set,
        instrument=spec_set.specifications[0].instrument,
        side=side,
        quantity=CanonicalDecimal(quantity),
        price=CanonicalDecimal(price),
        client_submission_key=Sha256Digest("4" * 64) if resolved else None,
        venue_order_id=VenueOrderId("venue-order-7") if resolved else None,
        order_id=order_id if resolved else None,
        correlation_id=(
            _id(EconomicOwnerKind.STRATEGY_SIGNAL, 1, run_id=run_id) if resolved else None
        ),
        causation_id=order_id if resolved else None,
    )
    return create_fill(
        fill_id=_id(EconomicOwnerKind.EXECUTION_FILL, fill_sequence, run_id=run_id),
        fact=fact,
        spec_set=spec_set,
    )


def _state_bytes(ledger: PortfolioLedger) -> tuple[bytes, tuple[bytes, ...]]:
    def transaction_bytes(
        item: InitialFundingTransaction | LedgerTransaction | ReconciliationTransaction,
    ) -> bytes:
        if type(item) is InitialFundingTransaction:
            return canonical_initial_funding_transaction_bytes(item)
        if type(item) is LedgerTransaction:
            return canonical_ledger_transaction_bytes(item)
        assert type(item) is ReconciliationTransaction
        return canonical_reconciliation_transaction_bytes(item)

    return (
        canonical_portfolio_snapshot_bytes(ledger.snapshot),
        tuple(transaction_bytes(item) for item in ledger.transactions),
    )


def test_adjustment_state_freeze_preserves_initial_funding_outcome() -> None:
    from unit.test_ledger_adjustment import BINDING, _audited, _correction_bundle

    ledger, _spec_set_value, _observation, _outcome, _acknowledgement, command, authorization = (
        _correction_bundle()
    )
    original = ledger.apply_initial_funding(
        InitialFundingSpec(
            ledger.snapshot.instrument_spec_set_id,
            ledger.snapshot.instrument_spec_set_sha256,
            USD,
            CanonicalDecimal("0.01"),
            CanonicalDecimal("1000"),
        ),
        binding=BINDING,
        prepared_acknowledgement=Sha256Digest("3" * 64),
    )
    ledger._state = replace(ledger._state, initial_funding_outcome=original)

    ledger.apply_reconciliation_adjustment(_audited(authorization), command)

    assert ledger._state.initial_funding_outcome is original


def _replace_fill(fill: Fill, **changes: object) -> Fill:
    replacement = object.__new__(Fill)
    for name in Fill.__slots__:
        object.__setattr__(
            replacement,
            name,
            changes.get(name, getattr(fill, name)),
        )
    return replacement


def test_factory_creates_exact_immutable_version_zero_snapshot() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)

    assert ledger.transactions == ()
    assert ledger.snapshot.snapshot_version == 0
    assert ledger.snapshot.ledger_sequence == 0
    assert ledger.snapshot.last_entry_id is None
    assert ledger.snapshot.cash_balances == ()
    assert ledger.snapshot.position_balances == ()
    assert ledger.snapshot.rounding_balances == ()
    assert ledger.snapshot.unresolved_fills == ()
    assert ledger.snapshot.open_reconciliation_bindings == ()
    assert ledger.snapshot.open_reconciliation_refs == ()
    assert b'"open_reconciliation_bindings":[]' in canonical_portfolio_snapshot_bytes(
        ledger.snapshot
    )
    assert b'"snapshot_version":0' in canonical_portfolio_snapshot_bytes(ledger.snapshot)
    assert len(portfolio_snapshot_digest(ledger.snapshot).value) == 64
    with pytest.raises(FrozenInstanceError):
        ledger.snapshot.snapshot_version = 1  # type: ignore[misc]
    with pytest.raises(TypeError, match="created only"):
        PortfolioLedger()


def test_factory_rejects_conflicting_currency_quanta_before_state() -> None:
    second = Instrument(VenueId("XNYS"), "IBM")
    spec_set = _spec_set(
        _spec(),
        _spec(
            instrument=second,
            specification_id="xnys.ibm.v1",
            currency_quantum="0.001",
        ),
    )
    with pytest.raises(PortfolioLedgerError) as error:
        create_portfolio_ledger(RUN_ID, spec_set)
    assert error.value.code is OutcomeCode.CONFLICTING_ID


def test_buy_and_sell_post_balanced_entries_and_round_trip_balances() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    buy = _fill(spec_set, fill_sequence=10, dedup="buy-1", quantity="3", price="10.01")
    sell = _fill(
        spec_set,
        fill_sequence=11,
        dedup="sell-1",
        side=OrderSide.SELL,
        quantity="3",
        price="10.01",
        occurred_at=TIME + timedelta(seconds=1),
    )

    applied = ledger.apply_fill(buy)
    assert applied.code is OutcomeCode.LEDGER_APPLIED
    assert applied.before_snapshot_version == 0
    assert applied.after_snapshot_version == 1
    assert applied.transaction is ledger.transactions[0]
    assert [posting.account for posting in ledger.transactions[0].postings] == [
        LedgerAccountKind.PORTFOLIO_POSITION,
        LedgerAccountKind.EXTERNAL_INVENTORY,
        LedgerAccountKind.PORTFOLIO_CASH,
        LedgerAccountKind.EXTERNAL_SETTLEMENT,
    ]
    assert ledger.snapshot.position_balances[0].quantity == CanonicalDecimal("3")
    assert ledger.snapshot.cash_balances[0].amount == CanonicalDecimal("-30.03")

    second = ledger.apply_fill(sell)
    assert second.code is OutcomeCode.LEDGER_APPLIED
    assert ledger.snapshot.snapshot_version == 2
    assert ledger.snapshot.cash_balances == ()
    assert ledger.snapshot.position_balances == ()
    assert ledger.snapshot.rounding_balances == ()
    assert ledger.transactions[1].previous_transaction_sha256 == ledger_transaction_digest(
        ledger.transactions[0]
    )
    last_transaction = ledger.transactions[1]
    assert type(last_transaction) is LedgerTransaction
    assert ledger.snapshot.last_transaction_sha256 == ledger_transaction_digest(last_transaction)


def test_zero_price_and_subquantum_vectors_never_construct_zero_postings() -> None:
    zero_set = _spec_set(_spec(price_quantum="0.001", currency_quantum="0.01"))
    zero_ledger = create_portfolio_ledger(RUN_ID, zero_set)
    zero = zero_ledger.apply_fill(_fill(zero_set, fill_sequence=10, dedup="zero", price="0"))
    assert zero.code is OutcomeCode.LEDGER_APPLIED
    assert [item.account for item in zero_ledger.transactions[0].postings] == [
        LedgerAccountKind.PORTFOLIO_POSITION,
        LedgerAccountKind.EXTERNAL_INVENTORY,
    ]
    assert zero_ledger.snapshot.cash_balances == ()
    assert zero_ledger.snapshot.rounding_balances == ()

    sub_ledger = create_portfolio_ledger(RUN_ID, zero_set)
    sub = sub_ledger.apply_fill(_fill(zero_set, fill_sequence=11, dedup="sub", price="0.005"))
    assert sub.code is OutcomeCode.LEDGER_APPLIED
    assert [item.account for item in sub_ledger.transactions[0].postings] == [
        LedgerAccountKind.PORTFOLIO_POSITION,
        LedgerAccountKind.EXTERNAL_INVENTORY,
        LedgerAccountKind.EXTERNAL_SETTLEMENT,
        LedgerAccountKind.PORTFOLIO_ROUNDING,
    ]
    assert all(item.amount.coefficient != 0 for item in sub_ledger.transactions[0].postings)
    assert sub_ledger.snapshot.cash_balances == ()
    assert sub_ledger.snapshot.rounding_balances[0].amount == CanonicalDecimal("-0.005")


def test_signed_negative_price_reverses_currency_without_reversing_quantity() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    outcome = ledger.apply_fill(
        _fill(spec_set, fill_sequence=10, dedup="negative", quantity="2", price="-3")
    )
    assert outcome.code is OutcomeCode.LEDGER_APPLIED
    assert ledger.snapshot.position_balances[0].quantity == CanonicalDecimal("2")
    assert ledger.snapshot.cash_balances[0].amount == CanonicalDecimal("6")


def test_unresolved_fill_applies_once_and_reference_survives_later_replay() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    unresolved = _fill(
        spec_set,
        fill_sequence=10,
        dedup="unresolved",
        resolved=False,
    )
    applied = ledger.apply_fill(unresolved)
    assert applied.code is OutcomeCode.LEDGER_APPLIED
    first_transaction = ledger.transactions[0]
    assert type(first_transaction) is LedgerTransaction
    assert first_transaction.requires_reconciliation is True
    assert ledger.snapshot.unresolved_fills[0].fill_id == unresolved.fill_id

    later = _fill(
        spec_set,
        fill_sequence=11,
        dedup="later",
        occurred_at=TIME + timedelta(seconds=1),
    )
    ledger.apply_fill(later)
    state = _state_bytes(ledger)
    duplicate = ledger.apply_fill(unresolved)
    assert duplicate.code is OutcomeCode.LEDGER_DUPLICATE
    assert duplicate.transaction is ledger.transactions[0]
    assert _state_bytes(ledger) == state
    assert len(ledger.snapshot.unresolved_fills) == 1


def test_fill_and_fact_identity_collisions_return_exact_evidence_without_mutation() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    original = _fill(spec_set, fill_sequence=10, dedup="original", price="10")
    assert ledger.apply_fill(original).code is OutcomeCode.LEDGER_APPLIED
    baseline = _state_bytes(ledger)

    fill_collision = _fill(spec_set, fill_sequence=10, dedup="other", price="11")
    first = ledger.apply_fill(fill_collision)
    assert first.code is OutcomeCode.LEDGER_CONFLICT
    assert first.conflict_kind is LedgerConflictKind.FILL_ID_COLLISION
    assert first.fill_index_binding is not None
    assert first.fact_index_binding is None
    assert _state_bytes(ledger) == baseline

    fact_collision = _fill(spec_set, fill_sequence=11, dedup="original", price="11")
    second = ledger.apply_fill(fact_collision)
    assert second.conflict_kind is LedgerConflictKind.FACT_KEY_COLLISION
    assert second.fill_index_binding is None
    assert second.fact_index_binding is not None
    assert _state_bytes(ledger) == baseline

    both_collision = _fill(spec_set, fill_sequence=10, dedup="original", price="11")
    third = ledger.apply_fill(both_collision)
    assert third.conflict_kind is LedgerConflictKind.FILL_AND_FACT_COLLISION
    assert third.fill_index_binding == third.fact_index_binding
    assert _state_bytes(ledger) == baseline


def test_corrupt_replay_indexes_fail_closed_for_every_matrix_shape() -> None:
    spec_set = _spec_set()
    original = _fill(spec_set, fill_sequence=10, dedup="matrix")

    missing_fact = create_portfolio_ledger(RUN_ID, spec_set)
    missing_fact.apply_fill(original)
    missing_fact._state = replace(
        missing_fact._state,
        fact_index=MappingProxyType({}),
    )
    first = missing_fact.apply_fill(original)
    assert first.conflict_kind is LedgerConflictKind.INDEX_INCONSISTENT
    assert first.fill_index_binding is not None
    assert first.fact_index_binding is None

    cross = create_portfolio_ledger(RUN_ID, spec_set)
    cross.apply_fill(original)
    binding = cross._state.fill_index[original.fill_id]
    cross_fact_index = dict(cross._state.fact_index)
    cross_fact_index[original.fact_key] = ExistingLedgerBinding(
        entry_id=_id(EconomicOwnerKind.LEDGER_ENTRY, 2),
        fill_id=binding.fill_id,
        fill_sha256=binding.fill_sha256,
        transaction_sha256=binding.transaction_sha256,
    )
    cross._state = replace(
        cross._state,
        fact_index=MappingProxyType(cross_fact_index),
    )
    second = cross.apply_fill(original)
    assert second.conflict_kind is LedgerConflictKind.CROSS_INDEX_COLLISION
    assert second.fill_index_binding is not None
    assert second.fact_index_binding is not None

    inconsistent = create_portfolio_ledger(RUN_ID, spec_set)
    inconsistent.apply_fill(original)
    binding = inconsistent._state.fill_index[original.fill_id]
    inconsistent_fact_index = dict(inconsistent._state.fact_index)
    inconsistent_fact_index[original.fact_key] = ExistingLedgerBinding(
        entry_id=binding.entry_id,
        fill_id=binding.fill_id,
        fill_sha256=binding.fill_sha256,
        transaction_sha256=Sha256Digest("8" * 64),
    )
    inconsistent._state = replace(
        inconsistent._state,
        fact_index=MappingProxyType(inconsistent_fact_index),
    )
    third = inconsistent.apply_fill(original)
    assert third.conflict_kind is LedgerConflictKind.INDEX_INCONSISTENT
    assert third.fill_index_binding != third.fact_index_binding


def test_replay_identity_classification_precedes_changed_specification_validation() -> None:
    original_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, original_set)
    original = _fill(original_set, fill_sequence=10, dedup="same", price="10")
    ledger.apply_fill(original)
    baseline = _state_bytes(ledger)

    changed_set = _spec_set(_spec(multiplier="2"))
    changed = _fill(changed_set, fill_sequence=10, dedup="same", price="10")
    outcome = ledger.apply_fill(changed)
    assert outcome.code is OutcomeCode.LEDGER_CONFLICT
    assert outcome.conflict_kind is LedgerConflictKind.FILL_AND_FACT_COLLISION
    assert _state_bytes(ledger) == baseline


def test_entry_identity_occupation_is_fail_closed_conflict() -> None:
    spec_set = _spec_set()
    source = create_portfolio_ledger(RUN_ID, spec_set)
    source.apply_fill(_fill(spec_set, fill_sequence=10, dedup="source"))
    occupied = source.transactions[0]

    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    entry_index = dict(ledger._state.entry_index)
    entry_index[occupied.entry_id] = occupied
    ledger._state = replace(
        ledger._state,
        entry_index=MappingProxyType(entry_index),
    )
    baseline = _state_bytes(ledger)
    outcome = ledger.apply_fill(_fill(spec_set, fill_sequence=11, dedup="new"))
    assert outcome.code is OutcomeCode.LEDGER_CONFLICT
    assert outcome.conflict_kind is LedgerConflictKind.ENTRY_ID_OCCUPIED
    assert outcome.entry_index_binding is not None
    assert outcome.fill_index_binding is None
    assert outcome.fact_index_binding is None
    assert _state_bytes(ledger) == baseline


def test_invalid_specification_wins_before_entry_identity_occupation() -> None:
    spec_set = _spec_set()
    source = create_portfolio_ledger(RUN_ID, spec_set)
    source.apply_fill(_fill(spec_set, fill_sequence=10, dedup="occupied-source"))
    occupied = source.transactions[0]

    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    entry_index = dict(ledger._state.entry_index)
    entry_index[occupied.entry_id] = occupied
    ledger._state = replace(
        ledger._state,
        entry_index=MappingProxyType(entry_index),
    )
    fill = _fill(spec_set, fill_sequence=11, dedup="invalid-before-occupied")
    invalid = _replace_fill(
        fill,
        instrument_specification_id=InstrumentSpecId("xnas.aapl.other"),
    )
    baseline = _state_bytes(ledger)
    with pytest.raises(PortfolioLedgerError) as error:
        ledger.apply_fill(invalid)
    assert error.value.code is OutcomeCode.CONFLICTING_ID
    assert _state_bytes(ledger) == baseline


def test_position_cash_and_rounding_overflow_precedence_is_atomic() -> None:
    position_set = _spec_set(_spec(price_quantum="1", currency_quantum="1"))
    position_ledger = create_portfolio_ledger(RUN_ID, position_set)
    maximum = "99999999999999999999"
    position_ledger.apply_fill(
        _fill(
            position_set,
            fill_sequence=10,
            dedup="position-max",
            quantity=maximum,
            price="0",
        )
    )
    baseline = _state_bytes(position_ledger)
    position_failure = position_ledger.apply_fill(
        _fill(position_set, fill_sequence=11, dedup="position-over", price="0")
    )
    assert position_failure.code is OutcomeCode.ARITHMETIC_OVERFLOW
    assert position_failure.failure_stage is LedgerFailureStage.POSITION_BALANCE_OVERFLOW
    assert _state_bytes(position_ledger) == baseline

    cash_ledger = create_portfolio_ledger(RUN_ID, position_set)
    cash_ledger.apply_fill(
        _fill(
            position_set,
            fill_sequence=20,
            dedup="cash-max",
            side=OrderSide.SELL,
            price=maximum,
        )
    )
    baseline = _state_bytes(cash_ledger)
    cash_failure = cash_ledger.apply_fill(
        _fill(
            position_set,
            fill_sequence=21,
            dedup="cash-over",
            side=OrderSide.SELL,
            price="1",
        )
    )
    assert cash_failure.failure_stage is LedgerFailureStage.CASH_BALANCE_OVERFLOW
    assert _state_bytes(cash_ledger) == baseline

    rounding_set = _spec_set(
        _spec(
            price_quantum="0.5",
            currency_quantum=maximum,
        )
    )
    rounding_ledger = create_portfolio_ledger(RUN_ID, rounding_set)
    half = "49999999999999999999.5"
    rounding_ledger.apply_fill(
        _fill(rounding_set, fill_sequence=30, dedup="rounding-max", price=half)
    )
    rounding_ledger.apply_fill(
        _fill(rounding_set, fill_sequence=31, dedup="rounding-limit", price=half)
    )
    baseline = _state_bytes(rounding_ledger)
    rounding_failure = rounding_ledger.apply_fill(
        _fill(rounding_set, fill_sequence=32, dedup="rounding-over", price=half)
    )
    assert rounding_failure.code is OutcomeCode.ROUNDING_UNREPRESENTABLE
    assert rounding_failure.failure_stage is LedgerFailureStage.ROUNDING_BALANCE_OVERFLOW
    assert _state_bytes(rounding_ledger) == baseline


def test_cash_overflow_wins_when_cash_and_position_would_both_overflow() -> None:
    spec_set = _spec_set(_spec(price_quantum="1", currency_quantum="1"))
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    maximum = "99999999999999999999"
    ledger.apply_fill(
        _fill(
            spec_set,
            fill_sequence=10,
            dedup="both-max",
            quantity=maximum,
            price="1",
        )
    )
    baseline = _state_bytes(ledger)
    failure = ledger.apply_fill(_fill(spec_set, fill_sequence=11, dedup="both-over", price="1"))
    assert failure.code is OutcomeCode.ARITHMETIC_OVERFLOW
    assert failure.failure_stage is LedgerFailureStage.CASH_BALANCE_OVERFLOW
    assert _state_bytes(ledger) == baseline


def test_uint64_sequence_exhaustion_is_outcome_and_does_not_mutate() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    maximum = (1 << 64) - 1
    exhausted = PortfolioSnapshot(
        run_id=RUN_ID,
        instrument_spec_set_id=spec_set.identifier,
        instrument_spec_set_sha256=ledger.snapshot.instrument_spec_set_sha256,
        snapshot_version=maximum,
        ledger_sequence=maximum,
        last_entry_id=_id(EconomicOwnerKind.LEDGER_ENTRY, maximum),
        last_transaction_sha256=Sha256Digest("9" * 64),
        cash_balances=(),
        position_balances=(),
        rounding_balances=(),
        unresolved_fills=(),
    )
    ledger._state = replace(ledger._state, snapshot=exhausted)
    baseline = _state_bytes(ledger)
    failure = ledger.apply_fill(_fill(spec_set, fill_sequence=10, dedup="exhausted"))
    assert failure.code is OutcomeCode.OUT_OF_RANGE
    assert failure.failure_stage is LedgerFailureStage.LEDGER_SEQUENCE_EXHAUSTED
    assert _state_bytes(ledger) == baseline


@pytest.mark.parametrize(
    "specification,invalid_price,expected_code",
    [
        (_spec(), "1.001", OutcomeCode.NOT_QUANTIZED),
        (
            _spec(price_domain=PriceDomain.POSITIVE),
            "-1",
            OutcomeCode.PRICE_DOMAIN,
        ),
    ],
)
def test_invalid_grid_or_domain_wins_before_sequence_exhaustion(
    specification: InstrumentExecutionSpec,
    invalid_price: str,
    expected_code: OutcomeCode,
) -> None:
    spec_set = _spec_set(specification)
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    maximum = (1 << 64) - 1
    exhausted = PortfolioSnapshot(
        run_id=RUN_ID,
        instrument_spec_set_id=spec_set.identifier,
        instrument_spec_set_sha256=ledger.snapshot.instrument_spec_set_sha256,
        snapshot_version=maximum,
        ledger_sequence=maximum,
        last_entry_id=_id(EconomicOwnerKind.LEDGER_ENTRY, maximum),
        last_transaction_sha256=Sha256Digest("9" * 64),
        cash_balances=(),
        position_balances=(),
        rounding_balances=(),
        unresolved_fills=(),
    )
    ledger._state = replace(ledger._state, snapshot=exhausted)
    valid = _fill(
        spec_set,
        fill_sequence=10,
        dedup=f"invalid-{expected_code.name.lower()}",
        price="1",
    )
    invalid = _replace_fill(valid, price=CanonicalDecimal(invalid_price))
    baseline = _state_bytes(ledger)
    with pytest.raises(PortfolioLedgerError) as error:
        ledger.apply_fill(invalid)
    assert error.value.code is expected_code
    assert _state_bytes(ledger) == baseline


def test_settlement_exact_notional_unbalanced_and_encoding_failures_are_atomic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overflow_set = _spec_set(
        _spec(
            price_quantum="1",
            quantity_quantum="1",
            currency_quantum="1",
            multiplier="99999999999999999999",
        )
    )
    overflow_ledger = create_portfolio_ledger(RUN_ID, overflow_set)
    overflow = overflow_ledger.apply_fill(
        _fill(
            overflow_set,
            fill_sequence=10,
            dedup="settlement-over",
            price="99999999999999999999",
            quantity="99999999999999999999",
        )
    )
    assert overflow.failure_stage is LedgerFailureStage.SETTLEMENT_ARITHMETIC_OVERFLOW
    assert _state_bytes(overflow_ledger)[1] == ()

    residual_set = _spec_set(
        _spec(
            price_quantum="0.000000000000000001",
            quantity_quantum="0.000000000000000001",
            currency_quantum="1",
        )
    )
    residual_ledger = create_portfolio_ledger(RUN_ID, residual_set)
    residual = residual_ledger.apply_fill(
        _fill(
            residual_set,
            fill_sequence=11,
            dedup="settlement-residual",
            price="0.000000000000000001",
            quantity="0.000000000000000001",
        )
    )
    assert residual.code is OutcomeCode.ROUNDING_UNREPRESENTABLE
    assert residual.failure_stage is LedgerFailureStage.SETTLEMENT_ROUNDING_UNREPRESENTABLE
    assert _state_bytes(residual_ledger)[1] == ()

    spec_set = _spec_set()
    exact_ledger = create_portfolio_ledger(RUN_ID, spec_set)
    exact_fill = _fill(spec_set, fill_sequence=20, dedup="exact-over")

    def fail_add(_: CanonicalDecimal, __: CanonicalDecimal) -> CanonicalDecimal:
        raise EconomicValidationError(OutcomeCode.OUT_OF_RANGE, "forced exact overflow")

    monkeypatch.setattr("ea.portfolio.ledger._add_decimal", fail_add)
    exact = exact_ledger.apply_fill(exact_fill)
    assert exact.failure_stage is LedgerFailureStage.EXACT_NOTIONAL_OVERFLOW
    assert _state_bytes(exact_ledger)[1] == ()
    monkeypatch.undo()

    unbalanced_ledger = create_portfolio_ledger(RUN_ID, spec_set)
    unbalanced_fill = _fill(spec_set, fill_sequence=21, dedup="unbalanced")
    monkeypatch.setattr("ea.portfolio.ledger._postings_balance", lambda _: False)
    unbalanced = unbalanced_ledger.apply_fill(unbalanced_fill)
    assert unbalanced.code is OutcomeCode.LEDGER_UNBALANCED
    assert unbalanced.failure_stage is LedgerFailureStage.COMMODITY_UNBALANCED
    assert _state_bytes(unbalanced_ledger)[1] == ()
    monkeypatch.undo()

    encoding_ledger = create_portfolio_ledger(RUN_ID, spec_set)
    encoding_fill = _fill(spec_set, fill_sequence=22, dedup="encoding")
    baseline = _state_bytes(encoding_ledger)

    def fail_digest(_: object) -> Sha256Digest:
        raise RuntimeError("forced canonical encoding failure")

    monkeypatch.setattr("ea.portfolio.ledger.ledger_transaction_digest", fail_digest)
    with pytest.raises(RuntimeError, match="forced canonical encoding failure"):
        encoding_ledger.apply_fill(encoding_fill)
    assert _state_bytes(encoding_ledger) == baseline


@pytest.mark.parametrize(
    "target",
    [
        "ea.portfolio.ledger.canonical_portfolio_snapshot_bytes",
        "ea.portfolio.ledger.canonical_ledger_apply_outcome_bytes",
    ],
)
def test_snapshot_and_outcome_encoding_failures_do_not_swap_state(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    fill = _fill(spec_set, fill_sequence=10, dedup=target.rsplit(".", 1)[-1])
    original_state = ledger._state
    baseline = _state_bytes(ledger)

    def fail_encoding(_: object) -> bytes:
        raise RuntimeError("forced canonical encoding failure")

    monkeypatch.setattr(target, fail_encoding)
    with pytest.raises(RuntimeError, match="forced canonical encoding failure"):
        ledger.apply_fill(fill)
    assert ledger._state is original_state
    assert _state_bytes(ledger) == baseline


def test_success_replaces_one_frozen_private_state_aggregate() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    original_state = ledger._state
    outcome = ledger.apply_fill(_fill(spec_set, fill_sequence=10, dedup="state-swap"))
    assert outcome.code is OutcomeCode.LEDGER_APPLIED
    assert ledger._state is not original_state
    assert original_state.snapshot.snapshot_version == 0
    assert original_state.transactions == ()
    assert original_state.cash == {}
    with pytest.raises(TypeError):
        cast(dict[SettlementCurrency, CanonicalDecimal], original_state.cash)[USD] = (
            CanonicalDecimal("1")
        )


def test_wrong_carrier_and_run_are_structural_errors_without_mutation() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    baseline = _state_bytes(ledger)
    with pytest.raises(PortfolioLedgerError) as wrong_type:
        ledger.apply_fill(cast(Fill, object()))
    assert wrong_type.value.code is OutcomeCode.INVALID_TYPE

    other = _fill(
        spec_set,
        fill_sequence=10,
        dedup="other-run",
        run_id=OTHER_RUN_ID,
    )
    with pytest.raises(PortfolioLedgerError) as wrong_run:
        ledger.apply_fill(other)
    assert wrong_run.value.code is OutcomeCode.CONFLICTING_ID
    assert _state_bytes(ledger) == baseline


def test_new_fill_fee_contract_is_revalidated_against_ledger_specification() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    fill = _fill(spec_set, fill_sequence=10, dedup="forged-fee")
    forged = _replace_fill(
        fill,
        fees=(
            FeeEntry(
                fee_code=FeeCode.COMMISSION,
                currency=SettlementCurrency("EUR"),
                amount=CanonicalDecimal("0"),
            ),
        ),
    )
    baseline = _state_bytes(ledger)
    with pytest.raises(PortfolioLedgerError) as error:
        ledger.apply_fill(forged)
    assert error.value.code is OutcomeCode.CONFLICTING_ID
    assert _state_bytes(ledger) == baseline


def test_canonical_documents_and_digests_are_exact_and_domain_separated() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    fill = _fill(spec_set, fill_sequence=10, dedup="golden", price="1.25")
    outcome = ledger.apply_fill(fill)
    transaction = ledger.transactions[0]
    assert type(transaction) is LedgerTransaction

    transaction_bytes = canonical_ledger_transaction_bytes(transaction)
    snapshot_bytes = canonical_portfolio_snapshot_bytes(ledger.snapshot)
    outcome_bytes = canonical_ledger_apply_outcome_bytes(outcome)
    assert b'"canonicalization":"ea-ledger-transaction-v1"' in transaction_bytes
    assert b'"canonicalization":"ea-portfolio-snapshot-v2"' in snapshot_bytes
    assert b'"canonicalization":"ea-ledger-apply-outcome-v1"' in outcome_bytes
    assert b'"requires_reconciliation":false' in transaction_bytes
    assert b'"conflict_kind":null' in outcome_bytes
    assert (
        ledger_transaction_digest(transaction).value
        == hashlib.sha256(b"ea.ledger-transaction.v1\0" + transaction_bytes).hexdigest()
    )
    assert (
        portfolio_snapshot_digest(ledger.snapshot).value
        == hashlib.sha256(b"ea.portfolio-snapshot.v2\0" + snapshot_bytes).hexdigest()
    )
    assert (
        ledger_apply_outcome_digest(outcome).value
        == hashlib.sha256(b"ea.ledger-apply-outcome.v1\0" + outcome_bytes).hexdigest()
    )
    assert (
        ledger_transaction_digest(transaction).value
        == "c0f15d17bb2bc494b81ce599485e990a3566cced08f6c554bb30a6ddba947418"
    )
    assert (
        portfolio_snapshot_digest(ledger.snapshot).value
        == "1fc7923f8ddf520a041b3a5deebc83e73b66f91e3e33c24490357e1326623437"
    )
    assert (
        ledger_apply_outcome_digest(outcome).value
        == "a4d7e485efd4218ea61b53e3ade4d92facab90840e2aa9b5030ece124e427b6f"
    )
    assert outcome.snapshot_sha256 == portfolio_snapshot_digest(outcome.snapshot)
    assert outcome.transaction_sha256 == ledger_transaction_digest(transaction)
    assert fill_digest(fill) == transaction.fill_sha256


@pytest.mark.parametrize(
    "factory,code",
    [
        (
            lambda: create_portfolio_ledger(cast(RunId, "run"), _spec_set()),
            OutcomeCode.INVALID_TYPE,
        ),
        (
            lambda: PortfolioSnapshot(
                run_id=RUN_ID,
                instrument_spec_set_id=InstrumentSpecSetId("phase1.test.v1"),
                instrument_spec_set_sha256=Sha256Digest("1" * 64),
                snapshot_version=1,
                ledger_sequence=0,
                last_entry_id=None,
                last_transaction_sha256=None,
                cash_balances=(),
                position_balances=(),
                rounding_balances=(),
                unresolved_fills=(),
            ),
            OutcomeCode.CONFLICTING_ID,
        ),
    ],
)
def test_public_validation_uses_closed_structural_codes(
    factory: Callable[[], object],
    code: OutcomeCode,
) -> None:
    with pytest.raises(PortfolioLedgerError) as error:
        factory()
    assert error.value.code is code


def test_snapshot_rejects_cross_run_open_reconciliation_binding() -> None:
    # VERIFY-005: an open reconciliation binding whose Fill belongs to another
    # run must be rejected at the snapshot factory, not only at the binding.
    reference = OpenReconciliationRef(
        _id(EconomicOwnerKind.EXECUTION_FILL, 9, run_id=OTHER_RUN_ID),
        Sha256Digest("5" * 64),
        Sha256Digest("6" * 64),
    )
    spec_set = _spec_set()

    with pytest.raises(PortfolioLedgerError, match="run conflicts"):
        PortfolioSnapshot(
            run_id=RUN_ID,
            instrument_spec_set_id=spec_set.identifier,
            instrument_spec_set_sha256=instrument_spec_set_digest(spec_set),
            snapshot_version=3,
            ledger_sequence=3,
            last_entry_id=_id(EconomicOwnerKind.LEDGER_ENTRY, 3),
            last_transaction_sha256=Sha256Digest("8" * 64),
            cash_balances=(),
            position_balances=(),
            rounding_balances=(),
            unresolved_fills=(),
            open_reconciliation_bindings=(
                ExistingLedgerBinding(
                    _id(EconomicOwnerKind.LEDGER_ENTRY, 2, run_id=OTHER_RUN_ID),
                    reference.fill_id,
                    reference.fill_sha256,
                    Sha256Digest("9" * 64),
                ),
            ),
            open_reconciliation_refs=(reference,),
        )
