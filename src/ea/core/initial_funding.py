"""Canonical manifest-bound initial-funding evidence for the product kernel."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import final

from ea.core.economics import (
    CanonicalDecimal,
    EconomicValidationError,
    require_positive,
    require_quantized,
)
from ea.core.execution import InstrumentExecutionSpecSet, InstrumentSpecSetId, SettlementCurrency
from ea.core.run import RunContractError, Sha256Digest

INITIAL_FUNDING_SPEC_CANONICALIZATION = "ea-initial-funding-spec-v1"
INITIAL_FUNDING_SPEC_DOMAIN = b"ea.initial-funding-spec.v1\0"


class InitialFundingError(RunContractError):
    """A funding specification violates the closed product contract."""


def _fail(message: str) -> InitialFundingError:
    return InitialFundingError(message)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


@final
@dataclass(frozen=True, slots=True)
class InitialFundingSpec:
    """The single currency, positive, exact genesis amount for one spec set."""

    instrument_spec_set_id: InstrumentSpecSetId
    instrument_spec_set_sha256: Sha256Digest
    settlement_currency: SettlementCurrency
    currency_quantum: CanonicalDecimal
    amount: CanonicalDecimal

    def __post_init__(self) -> None:
        if type(self.instrument_spec_set_id) is not InstrumentSpecSetId:
            raise _fail("funding instrument specification-set identity must be exact")
        if type(self.instrument_spec_set_sha256) is not Sha256Digest:
            raise _fail("funding instrument specification-set digest must be exact")
        if type(self.settlement_currency) is not SettlementCurrency:
            raise _fail("funding settlement currency must be exact")
        if (
            type(self.currency_quantum) is not CanonicalDecimal
            or type(self.amount) is not CanonicalDecimal
        ):
            raise _fail("funding amount and quantum must be exact canonical decimals")
        try:
            require_positive(self.currency_quantum, field_name="funding currency_quantum")
            require_positive(self.amount, field_name="funding amount")
            require_quantized(self.amount, self.currency_quantum, field_name="funding amount")
        except EconomicValidationError as error:
            raise _fail(str(error)) from error


def canonical_initial_funding_spec_bytes(spec: InitialFundingSpec) -> bytes:
    """Return the literal closed-schema bytes which enter v2 lineage."""
    if type(spec) is not InitialFundingSpec:
        raise _fail("funding spec must be exact")
    return _canonical_json(
        {
            "amount": spec.amount.text,
            "canonicalization": INITIAL_FUNDING_SPEC_CANONICALIZATION,
            "currency_quantum": spec.currency_quantum.text,
            "instrument_spec_set_id": spec.instrument_spec_set_id.value,
            "instrument_spec_set_sha256": spec.instrument_spec_set_sha256.value,
            "schema_version": 1,
            "settlement_currency": spec.settlement_currency.code,
        }
    )


def initial_funding_spec_digest(spec: InitialFundingSpec) -> Sha256Digest:
    """Derive the domain-separated immutable funding identity."""
    return Sha256Digest(
        sha256(INITIAL_FUNDING_SPEC_DOMAIN + canonical_initial_funding_spec_bytes(spec)).hexdigest()
    )


def validate_initial_funding_spec(
    spec: InitialFundingSpec, spec_set: InstrumentExecutionSpecSet
) -> None:
    """Bind funding to exactly one settlement currency and quantum in the full set."""
    from ea.core.execution import instrument_spec_set_digest

    if type(spec) is not InitialFundingSpec or type(spec_set) is not InstrumentExecutionSpecSet:
        raise _fail("funding specification and specification set must be exact")
    currencies = {item.settlement_currency for item in spec_set.specifications}
    quanta = {item.currency_quantum for item in spec_set.specifications}
    if len(currencies) != 1 or len(quanta) != 1:
        raise _fail("funding specification set must have exactly one currency and quantum")
    if (
        spec.instrument_spec_set_id != spec_set.identifier
        or spec.instrument_spec_set_sha256 != instrument_spec_set_digest(spec_set)
        or spec.settlement_currency != next(iter(currencies))
        or spec.currency_quantum != next(iter(quanta))
    ):
        raise _fail("funding specification does not match the complete specification set")
