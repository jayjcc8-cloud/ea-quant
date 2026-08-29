"""Focused contracts for manifest-bound ledger genesis funding."""

from __future__ import annotations

from ea.core.economics import CanonicalDecimal
from ea.core.execution import InstrumentSpecSetId, SettlementCurrency
from ea.core.initial_funding import (
    InitialFundingSpec,
    canonical_initial_funding_spec_bytes,
    initial_funding_spec_digest,
)
from ea.core.run import Sha256Digest


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
