"""Property-level invariants for manifest-bound initial funding."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from unit.test_portfolio_ledger import RUN_ID, _spec_set

from ea.core.economics import CanonicalDecimal
from ea.core.execution import SettlementCurrency, instrument_spec_set_digest
from ea.core.initial_funding import (
    InitialFundingConflictKind,
    InitialFundingError,
    InitialFundingSpec,
)
from ea.core.run import RunBinding, RunReference, Sha256Digest
from ea.portfolio import create_portfolio_ledger


@given(st.integers(min_value=1, max_value=1_000_000))
def test_funding_replay_is_exact_and_manifest_mutation_conflicts(cents: int) -> None:
    spec_set = _spec_set()
    amount = CanonicalDecimal(str(Decimal(cents) / Decimal(100)))
    funding = InitialFundingSpec(
        spec_set.identifier,
        instrument_spec_set_digest(spec_set),
        SettlementCurrency("USD"),
        CanonicalDecimal("0.01"),
        amount,
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
    conflict = ledger.apply_initial_funding(
        funding,
        binding=RunBinding(RunReference(RUN_ID, Sha256Digest("44" * 32)), binding.manifest_sha256),
        prepared_acknowledgement=acknowledgement,
    )

    assert replay is applied
    assert conflict.conflict_kind is InitialFundingConflictKind.MANIFEST_BINDING_CONFLICT
    assert conflict.snapshot == applied.snapshot
    assert ledger.transactions == (applied.transaction,)


@given(st.integers(min_value=0, max_value=1_000_000))
def test_funding_rejects_every_positive_amount_off_the_currency_grid(cents: int) -> None:
    spec_set = _spec_set()
    amount = CanonicalDecimal(str(Decimal(cents) / Decimal(100) + Decimal("0.001")))

    with pytest.raises(InitialFundingError, match="multiple"):
        InitialFundingSpec(
            spec_set.identifier,
            instrument_spec_set_digest(spec_set),
            SettlementCurrency("USD"),
            CanonicalDecimal("0.01"),
            amount,
        )
