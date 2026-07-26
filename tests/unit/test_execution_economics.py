from __future__ import annotations

from collections.abc import Callable
from typing import cast

import pytest

from ea.core import (
    CanonicalDecimal,
    EconomicErrorCode,
    EconomicValidationError,
    Instrument,
    InstrumentExecutionSpec,
    InstrumentExecutionSpecSet,
    InstrumentSpecId,
    InstrumentSpecSetId,
    PriceDomain,
    SettlementCurrency,
    VenueId,
    build_instrument_spec_set,
    canonical_instrument_spec_set_bytes,
    instrument_spec_set_digest,
    settle_execution,
)

AAPL = Instrument(VenueId("XNAS"), "AAPL")
MSFT = Instrument(VenueId("XNAS"), "MSFT")
SET_ID = InstrumentSpecSetId("phase1.us-equities.v1")


def _spec(
    instrument: Instrument = AAPL,
    *,
    specification_id: str | None = None,
    price_quantum: str = "0.01",
    quantity_quantum: str = "1",
    currency: str = "USD",
    currency_quantum: str = "0.01",
    multiplier: str = "1",
    price_domain: PriceDomain = PriceDomain.POSITIVE,
) -> InstrumentExecutionSpec:
    label = specification_id or f"{instrument.venue.code.lower()}.{instrument.symbol.lower()}.v1"
    return InstrumentExecutionSpec(
        instrument=instrument,
        specification_id=InstrumentSpecId(label),
        price_quantum=CanonicalDecimal(price_quantum),
        quantity_quantum=CanonicalDecimal(quantity_quantum),
        settlement_currency=SettlementCurrency(currency),
        currency_quantum=CanonicalDecimal(currency_quantum),
        contract_multiplier=CanonicalDecimal(multiplier),
        price_domain=price_domain,
    )


def _set(*specifications: InstrumentExecutionSpec) -> InstrumentExecutionSpecSet:
    return build_instrument_spec_set(SET_ID, specifications or (_spec(),))


def _assert_code(
    error: pytest.ExceptionInfo[EconomicValidationError],
    code: EconomicErrorCode,
) -> None:
    assert error.value.code is code


@pytest.mark.parametrize(
    ("constructor", "value", "code"),
    [
        (SettlementCurrency, "usd", EconomicErrorCode.OUT_OF_RANGE),
        (SettlementCurrency, "US", EconomicErrorCode.OUT_OF_RANGE),
        (SettlementCurrency, "US-D", EconomicErrorCode.OUT_OF_RANGE),
        (InstrumentSpecId, "XNAS.aapl.v1", EconomicErrorCode.OUT_OF_RANGE),
        (InstrumentSpecSetId, "phase 1", EconomicErrorCode.OUT_OF_RANGE),
    ],
)
def test_execution_identity_grammars_are_closed(
    constructor: type[SettlementCurrency] | type[InstrumentSpecId] | type[InstrumentSpecSetId],
    value: str,
    code: EconomicErrorCode,
) -> None:
    with pytest.raises(EconomicValidationError) as error:
        constructor(value)

    _assert_code(error, code)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: _spec(price_quantum="0"),
        lambda: _spec(quantity_quantum="-1"),
        lambda: _spec(currency_quantum="0"),
        lambda: _spec(multiplier="-0.1"),
    ],
)
def test_instrument_spec_requires_strictly_positive_economic_parameters(
    factory: Callable[[], InstrumentExecutionSpec],
) -> None:
    with pytest.raises(EconomicValidationError) as error:
        factory()

    _assert_code(error, EconomicErrorCode.OUT_OF_RANGE)


def test_builder_canonicalizes_input_order_and_digest() -> None:
    aapl = _spec(AAPL)
    msft = _spec(MSFT)

    forward = _set(aapl, msft)
    reverse = _set(msft, aapl)

    assert tuple(spec.instrument for spec in forward.specifications) == (AAPL, MSFT)
    assert reverse == forward
    assert canonical_instrument_spec_set_bytes(reverse) == canonical_instrument_spec_set_bytes(
        forward
    )
    assert instrument_spec_set_digest(reverse) == instrument_spec_set_digest(forward)


def test_direct_spec_set_constructor_rejects_noncanonical_order() -> None:
    with pytest.raises(EconomicValidationError) as error:
        InstrumentExecutionSpecSet(
            identifier=SET_ID,
            specifications=(_spec(MSFT), _spec(AAPL)),
        )

    _assert_code(error, EconomicErrorCode.OUT_OF_RANGE)


def test_spec_set_rejects_duplicate_instrument_even_when_payload_differs() -> None:
    with pytest.raises(EconomicValidationError) as error:
        _set(_spec(AAPL), _spec(AAPL, specification_id="xnas.aapl.v2"))

    _assert_code(error, EconomicErrorCode.CONFLICTING_ID)


def test_canonical_spec_set_bytes_and_digest_are_frozen() -> None:
    spec_set = _set(_spec())
    expected = (
        b'{"canonicalization":"ea-instrument-spec-set-v1",'
        b'"instrument_spec_set_id":"phase1.us-equities.v1","record_count":1,'
        b'"schema_version":1,"specs":[{"contract_multiplier":"1",'
        b'"currency_quantum":"0.01","instrument":{"symbol":"AAPL","venue":"XNAS"},'
        b'"price_domain":"positive","price_quantum":"0.01","quantity_quantum":"1",'
        b'"settlement_currency":"USD","specification_id":"xnas.aapl.v1"}]}'
    )

    assert canonical_instrument_spec_set_bytes(spec_set) == expected
    assert (
        instrument_spec_set_digest(spec_set).value
        == "5a630d40e1f1a49451f4b2ba9e22b2453ee3982c3752bfc4e7ea0e71982ae17a"
    )


@pytest.mark.parametrize(
    "changed_spec",
    [
        _spec(specification_id="xnas.aapl.v2"),
        _spec(price_quantum="0.05"),
        _spec(quantity_quantum="0.1"),
        _spec(currency="EUR"),
        _spec(currency_quantum="0.001"),
        _spec(multiplier="2"),
        _spec(price_domain=PriceDomain.SIGNED),
    ],
)
def test_every_execution_relevant_spec_field_changes_digest(
    changed_spec: InstrumentExecutionSpec,
) -> None:
    baseline_spec = _spec()
    baseline = _set(baseline_spec)
    changed = _set(changed_spec)

    assert instrument_spec_set_digest(changed) != instrument_spec_set_digest(baseline)


def test_set_identity_and_instrument_identity_change_digest() -> None:
    baseline = _set(_spec(AAPL))
    changed_set_id = build_instrument_spec_set(
        InstrumentSpecSetId("phase1.us-equities.v2"),
        baseline.specifications,
    )
    changed_instrument = _set(_spec(MSFT))

    assert instrument_spec_set_digest(changed_set_id) != instrument_spec_set_digest(baseline)
    assert instrument_spec_set_digest(changed_instrument) != instrument_spec_set_digest(baseline)


@pytest.mark.parametrize(
    ("domain", "price", "accepted"),
    [
        (PriceDomain.POSITIVE, "1", True),
        (PriceDomain.POSITIVE, "0", False),
        (PriceDomain.POSITIVE, "-1", False),
        (PriceDomain.NON_NEGATIVE, "1", True),
        (PriceDomain.NON_NEGATIVE, "0", True),
        (PriceDomain.NON_NEGATIVE, "-1", False),
        (PriceDomain.SIGNED, "1", True),
        (PriceDomain.SIGNED, "0", True),
        (PriceDomain.SIGNED, "-1", True),
    ],
)
def test_price_domain_matrix_is_independent_from_grid(
    domain: PriceDomain,
    price: str,
    accepted: bool,
) -> None:
    spec_set = _set(_spec(price_domain=domain))

    if accepted:
        result = settle_execution(spec_set, AAPL, CanonicalDecimal(price), CanonicalDecimal("1"))
        assert result.amount == CanonicalDecimal(price)
    else:
        with pytest.raises(EconomicValidationError) as error:
            settle_execution(spec_set, AAPL, CanonicalDecimal(price), CanonicalDecimal("1"))
        _assert_code(error, EconomicErrorCode.PRICE_DOMAIN)


def test_execution_settlement_validates_grids_and_positive_quantity() -> None:
    spec_set = _set(_spec(quantity_quantum="0.5"))

    with pytest.raises(EconomicValidationError) as price_error:
        settle_execution(spec_set, AAPL, CanonicalDecimal("1.001"), CanonicalDecimal("1"))
    _assert_code(price_error, EconomicErrorCode.NOT_QUANTIZED)

    with pytest.raises(EconomicValidationError) as quantity_error:
        settle_execution(spec_set, AAPL, CanonicalDecimal("1"), CanonicalDecimal("0.1"))
    _assert_code(quantity_error, EconomicErrorCode.NOT_QUANTIZED)

    with pytest.raises(EconomicValidationError) as zero_error:
        settle_execution(spec_set, AAPL, CanonicalDecimal("1"), CanonicalDecimal("0"))
    _assert_code(zero_error, EconomicErrorCode.OUT_OF_RANGE)


def test_execution_settlement_carries_currency_and_specification_identity() -> None:
    spec_set = _set(_spec(multiplier="2"))

    result = settle_execution(
        spec_set,
        AAPL,
        CanonicalDecimal("10.01"),
        CanonicalDecimal("3"),
    )

    assert result.instrument == AAPL
    assert result.specification_id == InstrumentSpecId("xnas.aapl.v1")
    assert result.instrument_spec_set_id == SET_ID
    assert result.instrument_spec_set_sha256 == instrument_spec_set_digest(spec_set)
    assert result.settlement_currency == SettlementCurrency("USD")
    assert result.amount == CanonicalDecimal("60.06")
    assert result.rounding_residual == CanonicalDecimal("0")


def test_execution_settlement_rejects_missing_instrument_and_float_inputs() -> None:
    spec_set = _set(_spec())

    with pytest.raises(EconomicValidationError) as missing_error:
        settle_execution(spec_set, MSFT, CanonicalDecimal("1"), CanonicalDecimal("1"))
    _assert_code(missing_error, EconomicErrorCode.OUT_OF_RANGE)

    with pytest.raises(EconomicValidationError) as float_error:
        settle_execution(
            spec_set,
            AAPL,
            cast(CanonicalDecimal, 1.0),
            CanonicalDecimal("1"),
        )
    _assert_code(float_error, EconomicErrorCode.INVALID_TYPE)
