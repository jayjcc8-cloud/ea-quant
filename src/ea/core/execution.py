"""Instrument-bound execution specifications and exact settlement identity."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import final

from ea.core.economics import (
    CanonicalDecimal,
    EconomicErrorCode,
    EconomicValidationError,
    ScalarSettlement,
    require_positive,
    require_quantized,
    settle_product,
)
from ea.core.identity import Instrument
from ea.core.run import Sha256Digest

INSTRUMENT_SPEC_SET_SCHEMA_VERSION = 1
INSTRUMENT_SPEC_SET_CANONICALIZATION = "ea-instrument-spec-set-v1"
INSTRUMENT_SPEC_SET_DIGEST_DOMAIN = b"ea.instrument-spec-set.v1\0"

_CURRENCY_PATTERN = re.compile(r"[A-Z][A-Z0-9]{2,11}\Z", flags=re.ASCII)
_SPEC_ID_PATTERN = re.compile(r"[a-z][a-z0-9._-]{0,127}\Z", flags=re.ASCII)


def _fail(code: EconomicErrorCode, message: str) -> EconomicValidationError:
    return EconomicValidationError(code, message)


class PriceDomain(StrEnum):
    """Closed price-domain policy from ADR 0008."""

    POSITIVE = "positive"
    NON_NEGATIVE = "non_negative"
    SIGNED = "signed"


@final
@dataclass(frozen=True, slots=True)
class SettlementCurrency:
    """Canonical settlement-currency identity."""

    code: str

    def __post_init__(self) -> None:
        if type(self.code) is not str:
            raise _fail(
                EconomicErrorCode.INVALID_TYPE,
                "settlement currency must have exact runtime type str",
            )
        if _CURRENCY_PATTERN.fullmatch(self.code) is None:
            raise _fail(
                EconomicErrorCode.OUT_OF_RANGE,
                "settlement currency must match [A-Z][A-Z0-9]{2,11}",
            )


@final
@dataclass(frozen=True, slots=True)
class InstrumentSpecId:
    """Immutable version identity for one instrument specification."""

    value: str

    def __post_init__(self) -> None:
        _validate_spec_id(self.value, field_name="specification_id")


@final
@dataclass(frozen=True, slots=True)
class InstrumentSpecSetId:
    """Immutable identity for one code-defined specification set."""

    value: str

    def __post_init__(self) -> None:
        _validate_spec_id(self.value, field_name="instrument_spec_set_id")


def _validate_spec_id(value: object, *, field_name: str) -> None:
    if type(value) is not str:
        raise _fail(
            EconomicErrorCode.INVALID_TYPE,
            f"{field_name} must have exact runtime type str",
        )
    if _SPEC_ID_PATTERN.fullmatch(value) is None:
        raise _fail(
            EconomicErrorCode.OUT_OF_RANGE,
            f"{field_name} must match [a-z][a-z0-9._-]{{0,127}}",
        )


@final
@dataclass(frozen=True, slots=True)
class InstrumentExecutionSpec:
    """All exact execution economics for one structured instrument."""

    instrument: Instrument
    specification_id: InstrumentSpecId
    price_quantum: CanonicalDecimal
    quantity_quantum: CanonicalDecimal
    settlement_currency: SettlementCurrency
    currency_quantum: CanonicalDecimal
    contract_multiplier: CanonicalDecimal
    price_domain: PriceDomain

    def __post_init__(self) -> None:
        if type(self.instrument) is not Instrument:
            raise _fail(EconomicErrorCode.INVALID_TYPE, "instrument must be an exact Instrument")
        if type(self.specification_id) is not InstrumentSpecId:
            raise _fail(
                EconomicErrorCode.INVALID_TYPE,
                "specification_id must be an exact InstrumentSpecId",
            )
        if type(self.settlement_currency) is not SettlementCurrency:
            raise _fail(
                EconomicErrorCode.INVALID_TYPE,
                "settlement_currency must be an exact SettlementCurrency",
            )
        if type(self.price_domain) is not PriceDomain:
            raise _fail(EconomicErrorCode.INVALID_TYPE, "price_domain must be an exact PriceDomain")
        require_positive(self.price_quantum, field_name="price_quantum")
        require_positive(self.quantity_quantum, field_name="quantity_quantum")
        require_positive(self.currency_quantum, field_name="currency_quantum")
        require_positive(self.contract_multiplier, field_name="contract_multiplier")


@final
@dataclass(frozen=True, slots=True)
class InstrumentExecutionSpecSet:
    """One canonically ordered, duplicate-free specification set."""

    identifier: InstrumentSpecSetId
    specifications: tuple[InstrumentExecutionSpec, ...]

    def __post_init__(self) -> None:
        if type(self.identifier) is not InstrumentSpecSetId:
            raise _fail(
                EconomicErrorCode.INVALID_TYPE,
                "identifier must be an exact InstrumentSpecSetId",
            )
        if type(self.specifications) is not tuple:
            raise _fail(
                EconomicErrorCode.INVALID_TYPE,
                "specifications must be an exact tuple",
            )
        if any(type(spec) is not InstrumentExecutionSpec for spec in self.specifications):
            raise _fail(
                EconomicErrorCode.INVALID_TYPE,
                "specifications must contain exact InstrumentExecutionSpec values",
            )
        keys = tuple(spec.instrument.key for spec in self.specifications)
        if len(keys) != len(set(keys)):
            raise _fail(
                EconomicErrorCode.CONFLICTING_ID,
                "instrument execution specifications contain a duplicate instrument",
            )
        if keys != tuple(sorted(keys)):
            raise _fail(
                EconomicErrorCode.OUT_OF_RANGE,
                "instrument execution specifications are not in canonical order",
            )

    def require(self, instrument: Instrument) -> InstrumentExecutionSpec:
        if type(instrument) is not Instrument:
            raise _fail(EconomicErrorCode.INVALID_TYPE, "instrument must be an exact Instrument")
        for specification in self.specifications:
            if specification.instrument == instrument:
                return specification
        raise _fail(
            EconomicErrorCode.OUT_OF_RANGE,
            "instrument has no execution specification in the selected set",
        )


def build_instrument_spec_set(
    identifier: InstrumentSpecSetId,
    specifications: Iterable[InstrumentExecutionSpec],
) -> InstrumentExecutionSpecSet:
    """Materialize and canonically order one finite specification iterable."""
    if type(identifier) is not InstrumentSpecSetId:
        raise _fail(
            EconomicErrorCode.INVALID_TYPE,
            "identifier must be an exact InstrumentSpecSetId",
        )
    try:
        materialized = tuple(specifications)
    except TypeError as error:
        raise _fail(
            EconomicErrorCode.INVALID_TYPE,
            "specifications must be a finite iterable",
        ) from error
    if any(type(spec) is not InstrumentExecutionSpec for spec in materialized):
        raise _fail(
            EconomicErrorCode.INVALID_TYPE,
            "specifications must contain exact InstrumentExecutionSpec values",
        )
    ordered = tuple(sorted(materialized, key=lambda spec: spec.instrument.key))
    return InstrumentExecutionSpecSet(identifier=identifier, specifications=ordered)


def _specification_mapping(specification: InstrumentExecutionSpec) -> dict[str, object]:
    return {
        "contract_multiplier": specification.contract_multiplier.text,
        "currency_quantum": specification.currency_quantum.text,
        "instrument": {
            "symbol": specification.instrument.symbol,
            "venue": specification.instrument.venue.code,
        },
        "price_domain": specification.price_domain.value,
        "price_quantum": specification.price_quantum.text,
        "quantity_quantum": specification.quantity_quantum.text,
        "settlement_currency": specification.settlement_currency.code,
        "specification_id": specification.specification_id.value,
    }


def canonical_instrument_spec_set_bytes(spec_set: InstrumentExecutionSpecSet) -> bytes:
    """Return the closed canonical JSON projection for one specification set."""
    if type(spec_set) is not InstrumentExecutionSpecSet:
        raise _fail(
            EconomicErrorCode.INVALID_TYPE,
            "spec_set must be an exact InstrumentExecutionSpecSet",
        )
    document = {
        "canonicalization": INSTRUMENT_SPEC_SET_CANONICALIZATION,
        "instrument_spec_set_id": spec_set.identifier.value,
        "record_count": len(spec_set.specifications),
        "schema_version": INSTRUMENT_SPEC_SET_SCHEMA_VERSION,
        "specs": [_specification_mapping(spec) for spec in spec_set.specifications],
    }
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def instrument_spec_set_digest(spec_set: InstrumentExecutionSpecSet) -> Sha256Digest:
    """Derive the domain-separated digest from internally projected bytes."""
    payload = canonical_instrument_spec_set_bytes(spec_set)
    return Sha256Digest(sha256(INSTRUMENT_SPEC_SET_DIGEST_DOMAIN + payload).hexdigest())


@final
@dataclass(frozen=True, slots=True, init=False)
class ExecutionSettlement:
    """Factory-only settlement with every identity needed by later risk and ledger owners."""

    instrument: Instrument
    specification_id: InstrumentSpecId
    instrument_spec_set_id: InstrumentSpecSetId
    instrument_spec_set_sha256: Sha256Digest
    settlement_currency: SettlementCurrency
    amount: CanonicalDecimal
    rounding_residual: CanonicalDecimal

    def __init__(self) -> None:
        raise TypeError("ExecutionSettlement values are created only by settle_execution")


def _execution_settlement(
    *,
    instrument: Instrument,
    specification_id: InstrumentSpecId,
    instrument_spec_set_id: InstrumentSpecSetId,
    instrument_spec_set_sha256: Sha256Digest,
    settlement_currency: SettlementCurrency,
    amount: CanonicalDecimal,
    rounding_residual: CanonicalDecimal,
) -> ExecutionSettlement:
    values = (
        (instrument, Instrument),
        (specification_id, InstrumentSpecId),
        (instrument_spec_set_id, InstrumentSpecSetId),
        (instrument_spec_set_sha256, Sha256Digest),
        (settlement_currency, SettlementCurrency),
        (amount, CanonicalDecimal),
        (rounding_residual, CanonicalDecimal),
    )
    if any(type(value) is not expected for value, expected in values):
        raise AssertionError("internal execution settlement inputs must be canonical")
    result = object.__new__(ExecutionSettlement)
    object.__setattr__(result, "instrument", instrument)
    object.__setattr__(result, "specification_id", specification_id)
    object.__setattr__(result, "instrument_spec_set_id", instrument_spec_set_id)
    object.__setattr__(result, "instrument_spec_set_sha256", instrument_spec_set_sha256)
    object.__setattr__(result, "settlement_currency", settlement_currency)
    object.__setattr__(result, "amount", amount)
    object.__setattr__(result, "rounding_residual", rounding_residual)
    return result


def _require_price_domain(
    price: CanonicalDecimal,
    domain: PriceDomain,
) -> CanonicalDecimal:
    if type(price) is not CanonicalDecimal or type(domain) is not PriceDomain:
        raise _fail(
            EconomicErrorCode.INVALID_TYPE,
            "price and price domain must be exact canonical values",
        )
    if domain is PriceDomain.POSITIVE and price.coefficient <= 0:
        raise _fail(EconomicErrorCode.PRICE_DOMAIN, "price must be strictly positive")
    if domain is PriceDomain.NON_NEGATIVE and price.coefficient < 0:
        raise _fail(EconomicErrorCode.PRICE_DOMAIN, "price must be non-negative")
    return price


def settle_execution(
    spec_set: InstrumentExecutionSpecSet,
    instrument: Instrument,
    price: CanonicalDecimal,
    quantity: CanonicalDecimal,
) -> ExecutionSettlement:
    """Validate one instrument-bound economic event and settle it atomically."""
    specification = validate_execution_inputs(
        spec_set,
        instrument,
        price,
        quantity,
    )
    scalar: ScalarSettlement = settle_product(
        price,
        quantity,
        specification.contract_multiplier,
        specification.currency_quantum,
    )
    return _execution_settlement(
        instrument=specification.instrument,
        specification_id=specification.specification_id,
        instrument_spec_set_id=spec_set.identifier,
        instrument_spec_set_sha256=instrument_spec_set_digest(spec_set),
        settlement_currency=specification.settlement_currency,
        amount=scalar.amount,
        rounding_residual=scalar.rounding_residual,
    )


def validate_execution_inputs(
    spec_set: InstrumentExecutionSpecSet,
    instrument: Instrument,
    price: CanonicalDecimal,
    quantity: CanonicalDecimal,
) -> InstrumentExecutionSpec:
    """Validate execution lineage, grids, domain, and quantity without settlement."""
    if type(spec_set) is not InstrumentExecutionSpecSet:
        raise _fail(
            EconomicErrorCode.INVALID_TYPE,
            "spec_set must be an exact InstrumentExecutionSpecSet",
        )
    if type(instrument) is not Instrument:
        raise _fail(EconomicErrorCode.INVALID_TYPE, "instrument must be an exact Instrument")
    if type(price) is not CanonicalDecimal or type(quantity) is not CanonicalDecimal:
        raise _fail(
            EconomicErrorCode.INVALID_TYPE,
            "price and quantity must be exact CanonicalDecimal values",
        )
    specification = spec_set.require(instrument)
    _require_price_domain(price, specification.price_domain)
    require_positive(quantity, field_name="quantity")
    require_quantized(price, specification.price_quantum, field_name="price")
    require_quantized(quantity, specification.quantity_quantum, field_name="quantity")
    return specification
