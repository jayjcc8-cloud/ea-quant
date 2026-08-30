"""Generated invariants for the one-time initial-funding genesis transaction."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st
from unit.test_portfolio_ledger import RUN_ID, _spec_set

from ea.core.economics import CanonicalDecimal
from ea.core.execution import instrument_spec_set_digest
from ea.core.initial_funding import InitialFundingSpec
from ea.core.run import RunBinding, RunReference, Sha256Digest
from ea.portfolio import create_portfolio_ledger


@given(cents=st.integers(min_value=1, max_value=10_000_000))
def test_quantized_positive_funding_replay_never_mutates(cents: int) -> None:
    spec_set = _spec_set()
    whole, remainder = divmod(cents, 100)
    amount = CanonicalDecimal(
        str(whole)
        if remainder == 0
        else f"{whole}.{remainder // 10}"
        if remainder % 10 == 0
        else f"{whole}.{remainder:02d}"
    )
    funding = InitialFundingSpec(
        instrument_spec_set_id=spec_set.identifier,
        instrument_spec_set_sha256=instrument_spec_set_digest(spec_set),
        settlement_currency=spec_set.specifications[0].settlement_currency,
        currency_quantum=CanonicalDecimal("0.01"),
        amount=amount,
    )
    binding = RunBinding(RunReference(RUN_ID, Sha256Digest("11" * 32)), Sha256Digest("22" * 32))
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    first = ledger.apply_initial_funding(
        funding, binding=binding, prepared_acknowledgement=Sha256Digest("33" * 32)
    )
    snapshot = ledger.snapshot
    replay = ledger.apply_initial_funding(
        funding, binding=binding, prepared_acknowledgement=Sha256Digest("33" * 32)
    )

    assert replay is first
    assert ledger.snapshot == snapshot
    assert len(ledger.transactions) == 1
