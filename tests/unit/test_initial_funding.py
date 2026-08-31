"""Focused contracts for canonical initial-funding evidence."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

from ea.core.economics import CanonicalDecimal
from ea.core.execution import InstrumentSpecSetId, SettlementCurrency
from ea.core.execution_identity import EconomicId, EconomicOwnerKind
from ea.core.initial_funding import (
    InitialFundingConflictKind,
    InitialFundingError,
    InitialFundingOutcome,
    InitialFundingResult,
    InitialFundingSpec,
    InitialFundingTransaction,
    canonical_initial_funding_outcome_bytes,
    canonical_initial_funding_spec_bytes,
    canonical_initial_funding_transaction_bytes,
    initial_funding_spec_digest,
)
from ea.core.portfolio import CurrencyCommodity, LedgerAccountKind, LedgerPosting
from ea.core.run import RunId, Sha256Digest
from ea.portfolio import create_portfolio_ledger
from unit.test_portfolio_ledger import RUN_ID, _spec_set


def test_initial_funding_spec_has_stable_literal_canonical_bytes() -> None:
    spec = InitialFundingSpec(
        instrument_spec_set_id=InstrumentSpecSetId("phase1.test.v1"),
        instrument_spec_set_sha256=Sha256Digest("11" * 32),
        settlement_currency=SettlementCurrency("USD"),
        currency_quantum=CanonicalDecimal("0.01"),
        amount=CanonicalDecimal("1000"),
    )

    assert canonical_initial_funding_spec_bytes(spec) == (
        b'{"amount":"1000","canonicalization":"ea-initial-funding-spec-v1",'
        b'"currency_quantum":"0.01","instrument_spec_set_id":"phase1.test.v1",'
        b'"instrument_spec_set_sha256":"1111111111111111111111111111111111111111111111111111111111111111",'
        b'"schema_version":1,"settlement_currency":"USD"}'
    )
    assert initial_funding_spec_digest(spec).value == (
        "d68b5f72b813bedb6be9f7276554905c881bed6c645039fcefbce2b6dc227fa4"
    )


def test_initial_funding_requires_a_positive_quantized_amount() -> None:
    with pytest.raises(InitialFundingError, match="multiple"):
        InitialFundingSpec(
            InstrumentSpecSetId("phase1.test.v1"),
            Sha256Digest("11" * 32),
            SettlementCurrency("USD"),
            CanonicalDecimal("0.01"),
            CanonicalDecimal("0.001"),
        )


def test_initial_funding_transaction_is_closed_first_ledger_entry() -> None:
    run_id = RunId("123e4567-e89b-42d3-a456-426614174000")
    amount = CanonicalDecimal("1000")
    currency = SettlementCurrency("USD")
    transaction = InitialFundingTransaction(
        run_id=run_id,
        entry_id=EconomicId(run_id, EconomicOwnerKind.LEDGER_ENTRY, 1),
        ledger_sequence=1,
        manifest_sha256=Sha256Digest("11" * 32),
        lineage_sha256=Sha256Digest("22" * 32),
        funding_spec_sha256=initial_funding_spec_digest(
            InitialFundingSpec(
                InstrumentSpecSetId("phase1.test.v1"),
                Sha256Digest("33" * 32),
                currency,
                CanonicalDecimal("0.01"),
                amount,
            )
        ),
        instrument_spec_set_id=InstrumentSpecSetId("phase1.test.v1"),
        instrument_spec_set_sha256=Sha256Digest("33" * 32),
        settlement_currency=currency,
        currency_quantum=CanonicalDecimal("0.01"),
        amount=amount,
        prepared_audit_acknowledgement_sha256=Sha256Digest("44" * 32),
        previous_transaction_sha256=None,
        postings=(
            LedgerPosting(LedgerAccountKind.PORTFOLIO_CASH, CurrencyCommodity(currency), amount),
            LedgerPosting(
                LedgerAccountKind.EXTERNAL_SETTLEMENT,
                CurrencyCommodity(currency),
                CanonicalDecimal("-1000"),
            ),
        ),
    )

    assert InitialFundingResult.APPLIED.value == "applied"
    assert InitialFundingConflictKind.ENTRY_ID_OCCUPIED.value == "entry_id_occupied"
    assert b'"ledger_sequence":1' in canonical_initial_funding_transaction_bytes(transaction)
    with pytest.raises(InitialFundingError, match="first ledger entry"):
        replace(transaction, ledger_sequence=2)
    with pytest.raises(InitialFundingError, match="first ledger entry"):
        replace(
            transaction,
            previous_transaction_sha256=cast(None, Sha256Digest("55" * 32)),
        )
    with pytest.raises(InitialFundingError, match="two exact postings"):
        replace(transaction, postings=cast(tuple[LedgerPosting, LedgerPosting], ()))
    with pytest.raises(InitialFundingError, match="specification digest"):
        replace(transaction, funding_spec_sha256=Sha256Digest("66" * 32))
    with pytest.raises(InitialFundingError, match="postings conflict"):
        replace(transaction, postings=(transaction.postings[1], transaction.postings[0]))


def test_initial_funding_conflict_retains_the_empty_snapshot() -> None:
    run_id = RunId("123e4567-e89b-42d3-a456-426614174000")
    snapshot = create_portfolio_ledger(run_id, _spec_set()).snapshot
    outcome = InitialFundingOutcome(
        run_id=run_id,
        result=InitialFundingResult.CONFLICT,
        manifest_sha256=Sha256Digest("11" * 32),
        submitted_funding_spec_sha256=Sha256Digest("22" * 32),
        prepared_audit_acknowledgement_sha256=Sha256Digest("33" * 32),
        before_snapshot_version=0,
        after_snapshot_version=0,
        snapshot=snapshot,
        transaction=None,
        existing_transaction_sha256=Sha256Digest("44" * 32),
        conflict_kind=InitialFundingConflictKind.ENTRY_ID_OCCUPIED,
    )

    assert outcome.snapshot is snapshot
    assert b'"result":"conflict"' in canonical_initial_funding_outcome_bytes(outcome)


@pytest.mark.parametrize("case", ["snapshot", "digest", "result"])
def test_initial_funding_outcome_rejects_conflict_shape_or_snapshot_drift(
    case: str,
) -> None:
    run_id = RunId("123e4567-e89b-42d3-a456-426614174000")
    outcome = InitialFundingOutcome(
        run_id=run_id,
        result=InitialFundingResult.CONFLICT,
        manifest_sha256=Sha256Digest("11" * 32),
        submitted_funding_spec_sha256=Sha256Digest("22" * 32),
        prepared_audit_acknowledgement_sha256=Sha256Digest("33" * 32),
        before_snapshot_version=0,
        after_snapshot_version=0,
        snapshot=create_portfolio_ledger(run_id, _spec_set()).snapshot,
        transaction=None,
        existing_transaction_sha256=Sha256Digest("44" * 32),
        conflict_kind=InitialFundingConflictKind.ENTRY_ID_OCCUPIED,
    )

    with pytest.raises(InitialFundingError):
        if case == "snapshot":
            replace(outcome, before_snapshot_version=1)
        elif case == "digest":
            replace(outcome, existing_transaction_sha256=None)
        else:
            replace(outcome, result=InitialFundingResult.APPLIED)


def test_ledger_applies_genesis_once_and_retains_exact_replay() -> None:
    spec_set = _spec_set()
    funding = InitialFundingSpec(
        instrument_spec_set_id=spec_set.identifier,
        instrument_spec_set_sha256=__import__(
            "ea.core.execution", fromlist=["instrument_spec_set_digest"]
        ).instrument_spec_set_digest(spec_set),
        settlement_currency=SettlementCurrency("USD"),
        currency_quantum=CanonicalDecimal("0.01"),
        amount=CanonicalDecimal("1000"),
    )
    binding = __import__("ea.core.run", fromlist=["RunBinding", "RunReference"]).RunBinding(
        __import__("ea.core.run", fromlist=["RunReference"]).RunReference(
            RUN_ID, Sha256Digest("11" * 32)
        ),
        Sha256Digest("22" * 32),
    )
    ledger = create_portfolio_ledger(RUN_ID, spec_set)

    applied = ledger.apply_initial_funding(
        funding, binding=binding, prepared_acknowledgement=Sha256Digest("33" * 32)
    )
    replay = ledger.apply_initial_funding(
        funding, binding=binding, prepared_acknowledgement=Sha256Digest("33" * 32)
    )

    assert applied.result.value == "applied"
    assert applied.transaction is not None
    assert applied.transaction.ledger_sequence == 1
    assert applied.snapshot.cash_balances[0].amount == CanonicalDecimal("1000")
    assert replay is applied
    with pytest.raises(InitialFundingError, match="snapshot versions"):
        replace(applied, before_snapshot_version=1)
    with pytest.raises(InitialFundingError, match="applied funding outcome"):
        replace(applied, transaction=None)
    with pytest.raises(InitialFundingError, match="applied funding outcome"):
        replace(applied, conflict_kind=InitialFundingConflictKind.ENTRY_ID_OCCUPIED)


def test_applied_funding_outcome_rejects_transaction_binding_drift() -> None:
    spec_set = _spec_set()
    funding = InitialFundingSpec(
        instrument_spec_set_id=spec_set.identifier,
        instrument_spec_set_sha256=__import__(
            "ea.core.execution", fromlist=["instrument_spec_set_digest"]
        ).instrument_spec_set_digest(spec_set),
        settlement_currency=SettlementCurrency("USD"),
        currency_quantum=CanonicalDecimal("0.01"),
        amount=CanonicalDecimal("1000"),
    )
    binding = __import__("ea.core.run", fromlist=["RunBinding", "RunReference"]).RunBinding(
        __import__("ea.core.run", fromlist=["RunReference"]).RunReference(
            RUN_ID, Sha256Digest("11" * 32)
        ),
        Sha256Digest("22" * 32),
    )
    applied = create_portfolio_ledger(RUN_ID, spec_set).apply_initial_funding(
        funding, binding=binding, prepared_acknowledgement=Sha256Digest("33" * 32)
    )
    foreign_run_id = RunId("223e4567-e89b-42d3-a456-426614174000")
    foreign_binding = __import__("ea.core.run", fromlist=["RunBinding", "RunReference"]).RunBinding(
        __import__("ea.core.run", fromlist=["RunReference"]).RunReference(
            foreign_run_id, Sha256Digest("44" * 32)
        ),
        Sha256Digest("55" * 32),
    )
    foreign = create_portfolio_ledger(foreign_run_id, spec_set).apply_initial_funding(
        funding, binding=foreign_binding, prepared_acknowledgement=Sha256Digest("66" * 32)
    )

    assert foreign.transaction is not None
    with pytest.raises(InitialFundingError, match="applied funding outcome bindings"):
        replace(applied, transaction=foreign.transaction)
    with pytest.raises(InitialFundingError, match="applied funding outcome bindings"):
        replace(applied, manifest_sha256=Sha256Digest("77" * 32))
    with pytest.raises(InitialFundingError, match="applied funding outcome bindings"):
        replace(applied, submitted_funding_spec_sha256=Sha256Digest("88" * 32))
    with pytest.raises(InitialFundingError, match="applied funding outcome bindings"):
        replace(applied, prepared_audit_acknowledgement_sha256=Sha256Digest("99" * 32))


def test_applied_funding_outcome_rejects_every_non_genesis_snapshot_surface() -> None:
    from ea.core.initial_funding import initial_funding_transaction_digest
    from ea.core.portfolio import (
        CashBalance,
        ExistingLedgerBinding,
        OpenReconciliationRef,
        PositionBalance,
        RoundingBalance,
        UnresolvedFillRef,
    )
    from ea.core.run import RunBinding, RunReference

    spec_set = _spec_set()
    instrument = spec_set.specifications[0]
    binding = RunBinding(RunReference(RUN_ID, Sha256Digest("11" * 32)), Sha256Digest("22" * 32))

    def apply(amount: str) -> InitialFundingOutcome:
        funding = InitialFundingSpec(
            spec_set.identifier,
            __import__(
                "ea.core.execution", fromlist=["instrument_spec_set_digest"]
            ).instrument_spec_set_digest(spec_set),
            instrument.settlement_currency,
            instrument.currency_quantum,
            CanonicalDecimal(amount),
        )
        return create_portfolio_ledger(RUN_ID, spec_set).apply_initial_funding(
            funding, binding=binding, prepared_acknowledgement=Sha256Digest("33" * 32)
        )

    applied, foreign = apply("1000"), apply("2000")
    assert applied.transaction is not None
    fill_id = EconomicId(RUN_ID, EconomicOwnerKind.EXECUTION_FILL, 1)
    transaction_sha256 = initial_funding_transaction_digest(applied.transaction)
    ledger_binding = ExistingLedgerBinding(
        applied.transaction.entry_id, fill_id, Sha256Digest("44" * 32), transaction_sha256
    )
    reconciliation_ref = OpenReconciliationRef(
        fill_id, Sha256Digest("44" * 32), Sha256Digest("55" * 32)
    )

    def cash(
        currency: SettlementCurrency = instrument.settlement_currency,
        quantum: CanonicalDecimal = instrument.currency_quantum,
    ) -> tuple[CashBalance, ...]:
        return (CashBalance(currency, quantum, CanonicalDecimal("1000")),)

    changes: tuple[dict[str, Any], ...] = (
        {"instrument_spec_set_id": InstrumentSpecSetId("other.v1")},
        {"instrument_spec_set_sha256": Sha256Digest("66" * 32)},
        {"last_transaction_sha256": Sha256Digest("77" * 32)},
        {"cash_balances": ()},
        {"cash_balances": cash(SettlementCurrency("EUR"))},
        {"cash_balances": cash(quantum=CanonicalDecimal("0.1"))},
        {"cash_balances": (replace(cash()[0], amount=CanonicalDecimal("2000")),)},
        {"cash_balances": cash(SettlementCurrency("EUR")) + cash()},
        {
            "position_balances": (
                PositionBalance(
                    instrument.instrument, instrument.quantity_quantum, CanonicalDecimal("1")
                ),
            )
        },
        {
            "rounding_balances": (
                RoundingBalance(instrument.settlement_currency, CanonicalDecimal("0.01")),
            )
        },
        {"unresolved_fills": (UnresolvedFillRef(fill_id, Sha256Digest("44" * 32)),)},
        {
            "open_reconciliation_bindings": (ledger_binding,),
            "open_reconciliation_refs": (reconciliation_ref,),
        },
    )
    snapshots = (
        foreign.snapshot,
        *(replace(applied.snapshot, **change) for change in changes),
    )
    for snapshot in snapshots:
        with pytest.raises(InitialFundingError, match="genesis snapshot"):
            replace(applied, snapshot=snapshot)
