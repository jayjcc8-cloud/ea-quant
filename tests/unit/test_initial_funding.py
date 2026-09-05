from __future__ import annotations

import json

from ea.core import (
    CanonicalDecimal,
    FundingTransaction,
    InitialFunding,
    Instrument,
    InstrumentExecutionSpec,
    InstrumentExecutionSpecSet,
    InstrumentSpecId,
    InstrumentSpecSetId,
    LedgerAccountKind,
    OutcomeCode,
    PriceDomain,
    RunId,
    SettlementCurrency,
    VenueId,
    build_instrument_spec_set,
    canonical_funding_apply_outcome_bytes,
    funding_transaction_digest,
    portfolio_snapshot_digest,
)
from ea.portfolio import create_portfolio_ledger


def _run_id() -> RunId:
    return RunId("123e4567-e89b-42d3-a456-426614174000")


def _spec_set() -> InstrumentExecutionSpecSet:
    return build_instrument_spec_set(
        InstrumentSpecSetId("scenario.xnas.aapl.v1"),
        (
            InstrumentExecutionSpec(
                instrument=Instrument(VenueId("XNAS"), "AAPL"),
                specification_id=InstrumentSpecId("xnas.aapl.v1"),
                price_quantum=CanonicalDecimal("0.01"),
                quantity_quantum=CanonicalDecimal("1"),
                settlement_currency=SettlementCurrency("USD"),
                currency_quantum=CanonicalDecimal("0.01"),
                contract_multiplier=CanonicalDecimal("1"),
                price_domain=PriceDomain.POSITIVE,
            ),
        ),
    )


def test_initial_funding_is_the_first_balanced_ledger_transaction() -> None:
    ledger = create_portfolio_ledger(_run_id(), _spec_set())
    funding = InitialFunding(_run_id(), SettlementCurrency("USD"), CanonicalDecimal("10000"))

    outcome = ledger.apply_initial_funding(funding)

    assert outcome.code is OutcomeCode.LEDGER_APPLIED
    assert outcome.before_snapshot_version == 0
    assert outcome.after_snapshot_version == 1
    assert outcome.snapshot.cash_balances[0].amount == CanonicalDecimal("10000")
    assert type(outcome.transaction) is FundingTransaction
    assert outcome.snapshot_sha256 == portfolio_snapshot_digest(outcome.snapshot)
    assert outcome.transaction_sha256 == funding_transaction_digest(outcome.transaction)
    document = json.loads(canonical_funding_apply_outcome_bytes(outcome))
    assert document["snapshot_sha256"] == outcome.snapshot_sha256.value
    assert document["transaction_sha256"] == outcome.transaction_sha256.value
    assert ledger.transactions == (outcome.transaction,)
    assert [posting.account for posting in outcome.transaction.postings] == [
        LedgerAccountKind.PORTFOLIO_CASH,
        LedgerAccountKind.EXTERNAL_SETTLEMENT,
    ]
    assert sum(posting.amount.coefficient for posting in outcome.transaction.postings) == 0


def test_equivalent_funding_replay_is_exactly_once() -> None:
    ledger = create_portfolio_ledger(_run_id(), _spec_set())
    funding = InitialFunding(_run_id(), SettlementCurrency("USD"), CanonicalDecimal("10000"))

    first = ledger.apply_initial_funding(funding)
    replay = ledger.apply_initial_funding(funding)

    assert replay is first
    assert len(ledger.transactions) == 1
    assert ledger.snapshot.cash_balances[0].amount == CanonicalDecimal("10000")


def test_conflicting_funding_fails_closed_without_mutation() -> None:
    ledger = create_portfolio_ledger(_run_id(), _spec_set())
    accepted = InitialFunding(_run_id(), SettlementCurrency("USD"), CanonicalDecimal("10000"))
    conflict = InitialFunding(_run_id(), SettlementCurrency("USD"), CanonicalDecimal("20000"))
    ledger.apply_initial_funding(accepted)
    before = ledger.snapshot

    outcome = ledger.apply_initial_funding(conflict)

    assert outcome.code is OutcomeCode.LEDGER_CONFLICT
    assert outcome.before_snapshot_version == outcome.after_snapshot_version == 1
    assert outcome.transaction is None
    assert ledger.snapshot is before
    assert len(ledger.transactions) == 1
