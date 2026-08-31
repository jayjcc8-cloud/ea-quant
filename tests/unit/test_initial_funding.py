"""Focused contracts for canonical initial-funding evidence."""

from __future__ import annotations

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
