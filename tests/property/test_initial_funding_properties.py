"""Property contracts for immutable, quantized Phase 1 initial funding."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ea.core import CanonicalDecimal, InstrumentSpecSetId, SettlementCurrency, Sha256Digest
from ea.core.initial_funding import (
    InitialFundingError,
    InitialFundingSpec,
    canonical_initial_funding_spec_bytes,
    initial_funding_spec_digest,
)


def _spec(amount: CanonicalDecimal) -> InitialFundingSpec:
    return InitialFundingSpec(
        InstrumentSpecSetId("phase1.funding.property.v1"),
        Sha256Digest("7" * 64),
        SettlementCurrency("USD"),
        CanonicalDecimal("0.01"),
        amount,
    )


@given(cents=st.integers(min_value=1, max_value=10_000_000))
@settings(max_examples=80)
def test_quantized_positive_funding_has_stable_closed_identity(cents: int) -> None:
    amount = CanonicalDecimal(f"{cents // 100}.{cents % 100:02d}".rstrip("0").rstrip("."))
    first, second = _spec(amount), _spec(CanonicalDecimal(amount.text))

    assert canonical_initial_funding_spec_bytes(first) == canonical_initial_funding_spec_bytes(
        second
    )
    assert initial_funding_spec_digest(first) == initial_funding_spec_digest(second)


@given(mills=st.integers(min_value=1, max_value=10_000_000).filter(lambda value: value % 10))
@settings(max_examples=80)
def test_non_quantized_funding_fails_closed_for_every_non_cent_amount(mills: int) -> None:
    with pytest.raises(InitialFundingError, match="exact multiple"):
        _spec(CanonicalDecimal(f"{mills // 1000}.{mills % 1000:03d}"))
