"""Focused contracts for canonical initial-funding evidence."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ea.core.economics import CanonicalDecimal
from ea.core.execution import (
    InstrumentSpecSetId,
    SettlementCurrency,
    instrument_spec_set_digest,
)
from ea.core.initial_funding import (
    InitialFundingError,
    InitialFundingSpec,
    canonical_initial_funding_spec_bytes,
    initial_funding_spec_digest,
)
from ea.core.run import RunBinding, RunReference, Sha256Digest
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


def test_ledger_applies_genesis_once_and_retains_exact_replay() -> None:
    spec_set = _spec_set()
    funding = InitialFundingSpec(
        instrument_spec_set_id=spec_set.identifier,
        instrument_spec_set_sha256=instrument_spec_set_digest(spec_set),
        settlement_currency=SettlementCurrency("USD"),
        currency_quantum=CanonicalDecimal("0.01"),
        amount=CanonicalDecimal("1000"),
    )
    binding = RunBinding(RunReference(RUN_ID, Sha256Digest("11" * 32)), Sha256Digest("22" * 32))
    acknowledgement = Sha256Digest("33" * 32)
    ledger = create_portfolio_ledger(RUN_ID, spec_set)

    applied = ledger.apply_initial_funding(
        funding, binding=binding, prepared_acknowledgement=acknowledgement
    )
    replay = ledger.apply_initial_funding(
        funding, binding=binding, prepared_acknowledgement=acknowledgement
    )

    assert applied.result.value == "applied"
    assert applied.transaction is not None
    assert applied.transaction.ledger_sequence == 1
    assert applied.snapshot.cash_balances[0].amount == CanonicalDecimal("1000")
    assert replay is applied
    assert replace(applied, before_snapshot_version=0, after_snapshot_version=1) == applied
    for value in (False, True):
        with pytest.raises(InitialFundingError, match="snapshot versions conflict"):
            replace(applied, before_snapshot_version=value)
        with pytest.raises(InitialFundingError, match="snapshot versions conflict"):
            replace(applied, after_snapshot_version=value)


def test_transaction_cannot_splice_a_different_spec_set_under_the_same_digest() -> None:
    spec_set = _spec_set()
    funding = InitialFundingSpec(
        spec_set.identifier,
        instrument_spec_set_digest(spec_set),
        SettlementCurrency("USD"),
        CanonicalDecimal("0.01"),
        CanonicalDecimal("1000"),
    )
    binding = RunBinding(RunReference(RUN_ID, Sha256Digest("11" * 32)), Sha256Digest("22" * 32))
    outcome = create_portfolio_ledger(RUN_ID, spec_set).apply_initial_funding(
        funding, binding=binding, prepared_acknowledgement=Sha256Digest("33" * 32)
    )
    assert outcome.transaction is not None

    with pytest.raises(InitialFundingError, match="digest conflicts"):
        replace(
            outcome.transaction,
            instrument_spec_set_id=InstrumentSpecSetId("phase1.splice.v1"),
        )
