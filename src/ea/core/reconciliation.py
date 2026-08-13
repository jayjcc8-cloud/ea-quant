"""Dependency-neutral reconciliation evidence from Accepted ADR 0022."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, final

from ea.core.economics import CanonicalDecimal, EconomicValidationError, require_quantized
from ea.core.execution import (
    InstrumentExecutionSpecSet,
    InstrumentSpecSetId,
    SettlementCurrency,
    instrument_spec_set_digest,
)
from ea.core.execution_identity import EconomicId, EconomicOwnerKind, SourceNamespace
from ea.core.execution_messages import FactProvenanceId
from ea.core.identity import Instrument, VenueId
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunId, Sha256Digest
from ea.core.runtime import ReconciliationObservationKind, RuntimeIdentifier
from ea.core.time import TimeValidationError, require_utc

RECONCILIATION_OBSERVATION_SCHEMA = "ea.reconciliation-observation.v1"
RECONCILIATION_OBSERVATION_DIGEST_DOMAIN = b"ea.reconciliation-observation.v1\0"
RECONCILIATION_CANONICALIZATION = "ea-canonical-json-v1"
MAX_RECONCILIATION_PAYLOAD_BYTES = 16_384
MAX_RECONCILIATION_BALANCES = 32

_MAX_UINT64 = (1 << 64) - 1
_VALUE_SEAL = object()
_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.NOT_QUANTIZED,
        OutcomeCode.CONFLICTING_ID,
    }
)


class ReconciliationContractError(ValueError):
    """Closed validation failure for reconciliation evidence."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _ERROR_CODES:
            raise TypeError("reconciliation errors require an exact supported OutcomeCode")
        self.code = code
        super().__init__(message)


def _fail(code: OutcomeCode, message: str) -> ReconciliationContractError:
    return ReconciliationContractError(code, message)


class ReconciliationScopeKind(StrEnum):
    TRADE = "trade"
    ORDER = "order"
    POSITION = "position"
    CASH = "cash"


class ReconciliationBalanceKind(StrEnum):
    INSTRUMENT_POSITION = "instrument_position"
    SETTLEMENT_CASH = "settlement_cash"


@final
@dataclass(frozen=True, slots=True)
class PositionReconciliationBalance:
    instrument: Instrument
    quantity: CanonicalDecimal

    def __post_init__(self) -> None:
        if type(self.instrument) is not Instrument:
            raise _fail(OutcomeCode.INVALID_TYPE, "position balance instrument must be exact")
        if type(self.quantity) is not CanonicalDecimal:
            raise _fail(OutcomeCode.INVALID_TYPE, "position balance quantity must be exact")


@final
@dataclass(frozen=True, slots=True)
class CashReconciliationBalance:
    currency: SettlementCurrency
    amount: CanonicalDecimal

    def __post_init__(self) -> None:
        if type(self.currency) is not SettlementCurrency:
            raise _fail(OutcomeCode.INVALID_TYPE, "cash balance currency must be exact")
        if type(self.amount) is not CanonicalDecimal:
            raise _fail(OutcomeCode.INVALID_TYPE, "cash balance amount must be exact")


ReconciliationBalance = PositionReconciliationBalance | CashReconciliationBalance


@final
@dataclass(frozen=True, slots=True, init=False)
class ReconciliationObservation:
    run_id: RunId
    instrument_spec_set_id: InstrumentSpecSetId
    instrument_spec_set_sha256: Sha256Digest
    observation_id: EconomicId
    kind: ReconciliationObservationKind
    source_namespace: SourceNamespace
    source_sequence: int
    occurred_at: datetime
    available_at: datetime
    watermark_namespace: SourceNamespace
    watermark_sequence: int
    declared_scope_kind: ReconciliationScopeKind
    declared_scope_id: RuntimeIdentifier
    provenance_id: FactProvenanceId
    provenance_payload_sha256: Sha256Digest
    balances: tuple[ReconciliationBalance, ...]
    _seal: object

    def __init__(self) -> None:
        raise TypeError("reconciliation observations are created only by their factory")


def create_reconciliation_observation(
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    observation_id: EconomicId,
    kind: ReconciliationObservationKind,
    source_namespace: SourceNamespace,
    source_sequence: int,
    occurred_at: datetime,
    available_at: datetime,
    watermark_namespace: SourceNamespace,
    watermark_sequence: int,
    declared_scope_kind: ReconciliationScopeKind,
    declared_scope_id: RuntimeIdentifier,
    provenance_id: FactProvenanceId,
    provenance_payload_sha256: Sha256Digest,
    balances: tuple[ReconciliationBalance, ...],
) -> ReconciliationObservation:
    """Validate one complete, bounded, kind-specific reconciliation root."""
    _require_exact_types(
        run_id=run_id,
        spec_set=spec_set,
        observation_id=observation_id,
        kind=kind,
        source_namespace=source_namespace,
        watermark_namespace=watermark_namespace,
        declared_scope_kind=declared_scope_kind,
        declared_scope_id=declared_scope_id,
        provenance_id=provenance_id,
        provenance_payload_sha256=provenance_payload_sha256,
        balances=balances,
    )
    _require_uint64(source_sequence, "source_sequence")
    _require_uint64(watermark_sequence, "watermark_sequence")
    canonical_occurred_at = _require_time(occurred_at, "occurred_at")
    canonical_available_at = _require_time(available_at, "available_at")
    if canonical_available_at < canonical_occurred_at:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "available_at cannot precede occurred_at")
    if observation_id.run_id != run_id:
        raise _fail(OutcomeCode.CONFLICTING_ID, "observation and run identities conflict")
    if observation_id.owner_kind is not EconomicOwnerKind.RECONCILIATION_OBSERVATION:
        raise _fail(OutcomeCode.CONFLICTING_ID, "observation identity owner conflicts")
    _require_kind_scope_and_balances(kind, declared_scope_kind, balances, spec_set)

    value = object.__new__(ReconciliationObservation)
    object.__setattr__(value, "run_id", run_id)
    object.__setattr__(value, "instrument_spec_set_id", spec_set.identifier)
    object.__setattr__(value, "instrument_spec_set_sha256", instrument_spec_set_digest(spec_set))
    object.__setattr__(value, "observation_id", observation_id)
    object.__setattr__(value, "kind", kind)
    object.__setattr__(value, "source_namespace", source_namespace)
    object.__setattr__(value, "source_sequence", source_sequence)
    object.__setattr__(value, "occurred_at", canonical_occurred_at)
    object.__setattr__(value, "available_at", canonical_available_at)
    object.__setattr__(value, "watermark_namespace", watermark_namespace)
    object.__setattr__(value, "watermark_sequence", watermark_sequence)
    object.__setattr__(value, "declared_scope_kind", declared_scope_kind)
    object.__setattr__(value, "declared_scope_id", declared_scope_id)
    object.__setattr__(value, "provenance_id", provenance_id)
    object.__setattr__(value, "provenance_payload_sha256", provenance_payload_sha256)
    object.__setattr__(value, "balances", balances)
    object.__setattr__(value, "_seal", _VALUE_SEAL)
    canonical_reconciliation_observation_bytes(value)
    return value


def canonical_reconciliation_observation_bytes(observation: ReconciliationObservation) -> bytes:
    if type(observation) is not ReconciliationObservation or observation._seal is not _VALUE_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "reconciliation observation must be factory-issued")
    payload = _canonical_json(_observation_document(observation))
    if len(payload) > MAX_RECONCILIATION_PAYLOAD_BYTES:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "reconciliation observation exceeds byte bound")
    return payload


def reconciliation_observation_digest(observation: ReconciliationObservation) -> Sha256Digest:
    payload = canonical_reconciliation_observation_bytes(observation)
    return _framed_digest(RECONCILIATION_OBSERVATION_DIGEST_DOMAIN, payload)


def decode_reconciliation_observation(
    canonical_payload: bytes,
    spec_set: InstrumentExecutionSpecSet,
) -> ReconciliationObservation:
    document = _decode_canonical_json(canonical_payload, "reconciliation observation")
    if type(document) is not dict or set(document) != {
        "available_at",
        "balances",
        "canonicalization",
        "declared_scope_id",
        "declared_scope_kind",
        "instrument_spec_set_id",
        "instrument_spec_set_sha256",
        "kind",
        "observation_id",
        "occurred_at",
        "provenance_id",
        "provenance_payload_sha256",
        "run_id",
        "schema",
        "source_namespace",
        "source_sequence",
        "watermark_namespace",
        "watermark_sequence",
    }:
        raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation observation fields conflict")
    if (
        _require_text(document, "schema") != RECONCILIATION_OBSERVATION_SCHEMA
        or _require_text(document, "canonicalization") != RECONCILIATION_CANONICALIZATION
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation observation schema conflicts")
    if type(spec_set) is not InstrumentExecutionSpecSet:
        raise _fail(OutcomeCode.INVALID_TYPE, "spec_set must be exact")
    if _require_text(
        document, "instrument_spec_set_id"
    ) != spec_set.identifier.value or _decode_digest(
        document, "instrument_spec_set_sha256"
    ) != instrument_spec_set_digest(spec_set):
        raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation specification binding conflicts")
    balances_document = document["balances"]
    if type(balances_document) is not list:
        raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation balances must be an array")
    balances = tuple(_decode_balance(item) for item in balances_document)
    try:
        run_id = RunId(_require_text(document, "run_id"))
        observation = create_reconciliation_observation(
            run_id=run_id,
            spec_set=spec_set,
            observation_id=_decode_economic_id(document["observation_id"], run_id),
            kind=ReconciliationObservationKind(_require_text(document, "kind")),
            source_namespace=SourceNamespace(_require_text(document, "source_namespace")),
            source_sequence=_require_json_int(document, "source_sequence"),
            occurred_at=_decode_time(_require_text(document, "occurred_at"), "occurred_at"),
            available_at=_decode_time(_require_text(document, "available_at"), "available_at"),
            watermark_namespace=SourceNamespace(_require_text(document, "watermark_namespace")),
            watermark_sequence=_require_json_int(document, "watermark_sequence"),
            declared_scope_kind=ReconciliationScopeKind(
                _require_text(document, "declared_scope_kind")
            ),
            declared_scope_id=RuntimeIdentifier(_require_text(document, "declared_scope_id")),
            provenance_id=FactProvenanceId(_require_text(document, "provenance_id")),
            provenance_payload_sha256=_decode_digest(
                document,
                "provenance_payload_sha256",
            ),
            balances=balances,
        )
    except (ValueError, TypeError) as error:
        if type(error) is ReconciliationContractError:
            raise
        raise _fail(
            OutcomeCode.CONFLICTING_ID, "reconciliation observation value conflicts"
        ) from error
    if canonical_reconciliation_observation_bytes(observation) != canonical_payload:
        raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation observation round-trip conflicts")
    return observation


def _require_exact_types(**values: object) -> None:
    expected = {
        "run_id": RunId,
        "spec_set": InstrumentExecutionSpecSet,
        "observation_id": EconomicId,
        "kind": ReconciliationObservationKind,
        "source_namespace": SourceNamespace,
        "watermark_namespace": SourceNamespace,
        "declared_scope_kind": ReconciliationScopeKind,
        "declared_scope_id": RuntimeIdentifier,
        "provenance_id": FactProvenanceId,
        "provenance_payload_sha256": Sha256Digest,
        "balances": tuple,
    }
    for field, expected_type in expected.items():
        if type(values[field]) is not expected_type:
            raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be exact")
    balances = values["balances"]
    assert type(balances) is tuple
    if any(
        type(balance) not in (PositionReconciliationBalance, CashReconciliationBalance)
        for balance in balances
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "balances contain an unsupported value")


def _require_kind_scope_and_balances(
    kind: ReconciliationObservationKind,
    scope_kind: ReconciliationScopeKind,
    balances: tuple[ReconciliationBalance, ...],
    spec_set: InstrumentExecutionSpecSet,
) -> None:
    expected_scope = {
        ReconciliationObservationKind.TRADE_DETAIL: ReconciliationScopeKind.TRADE,
        ReconciliationObservationKind.ORDER_DETAIL: ReconciliationScopeKind.ORDER,
        ReconciliationObservationKind.POSITION_SNAPSHOT: ReconciliationScopeKind.POSITION,
        ReconciliationObservationKind.CASH_SNAPSHOT: ReconciliationScopeKind.CASH,
    }[kind]
    if scope_kind is not expected_scope:
        raise _fail(OutcomeCode.CONFLICTING_ID, "observation kind and declared scope conflict")
    if kind in {
        ReconciliationObservationKind.TRADE_DETAIL,
        ReconciliationObservationKind.ORDER_DETAIL,
    }:
        if balances:
            raise _fail(OutcomeCode.CONFLICTING_ID, "detail observations cannot contain balances")
        return
    if not 1 <= len(balances) <= MAX_RECONCILIATION_BALANCES:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "snapshot observations require 1..32 balances")
    if kind is ReconciliationObservationKind.POSITION_SNAPSHOT:
        if any(type(balance) is not PositionReconciliationBalance for balance in balances):
            raise _fail(OutcomeCode.CONFLICTING_ID, "position snapshot balance kind conflicts")
        keys = tuple(
            balance.instrument.key
            for balance in balances
            if type(balance) is PositionReconciliationBalance
        )
        if keys != tuple(sorted(set(keys))):
            raise _fail(
                OutcomeCode.CONFLICTING_ID, "position balances are not canonical and unique"
            )
        for balance in balances:
            assert type(balance) is PositionReconciliationBalance
            try:
                require_quantized(
                    balance.quantity,
                    spec_set.require(balance.instrument).quantity_quantum,
                    field_name="position_reconciliation_quantity",
                )
            except EconomicValidationError as error:
                raise _fail(error.code, "position reconciliation quantity is invalid") from error
        return
    if any(type(balance) is not CashReconciliationBalance for balance in balances):
        raise _fail(OutcomeCode.CONFLICTING_ID, "cash snapshot balance kind conflicts")
    cash_keys = tuple(
        balance.currency.code for balance in balances if type(balance) is CashReconciliationBalance
    )
    if cash_keys != tuple(sorted(set(cash_keys))):
        raise _fail(OutcomeCode.CONFLICTING_ID, "cash balances are not canonical and unique")
    quantums: dict[str, CanonicalDecimal] = {}
    for specification in spec_set.specifications:
        code = specification.settlement_currency.code
        retained = quantums.setdefault(code, specification.currency_quantum)
        if retained != specification.currency_quantum:
            raise _fail(OutcomeCode.CONFLICTING_ID, "currency quantum conflicts within spec set")
    for balance in balances:
        assert type(balance) is CashReconciliationBalance
        try:
            quantum = quantums[balance.currency.code]
        except KeyError as error:
            raise _fail(
                OutcomeCode.CONFLICTING_ID, "cash currency is absent from spec set"
            ) from error
        try:
            require_quantized(
                balance.amount,
                quantum,
                field_name="cash_reconciliation_amount",
            )
        except EconomicValidationError as error:
            raise _fail(error.code, "cash reconciliation amount is invalid") from error


def _observation_document(observation: ReconciliationObservation) -> dict[str, object]:
    return {
        "available_at": _time_text(observation.available_at),
        "balances": [_balance_document(balance) for balance in observation.balances],
        "canonicalization": RECONCILIATION_CANONICALIZATION,
        "declared_scope_id": observation.declared_scope_id.value,
        "declared_scope_kind": observation.declared_scope_kind.value,
        "instrument_spec_set_id": observation.instrument_spec_set_id.value,
        "instrument_spec_set_sha256": observation.instrument_spec_set_sha256.value,
        "kind": observation.kind.value,
        "observation_id": _economic_id_document(observation.observation_id),
        "occurred_at": _time_text(observation.occurred_at),
        "provenance_id": observation.provenance_id.value,
        "provenance_payload_sha256": observation.provenance_payload_sha256.value,
        "run_id": observation.run_id.value,
        "schema": RECONCILIATION_OBSERVATION_SCHEMA,
        "source_namespace": observation.source_namespace.value,
        "source_sequence": observation.source_sequence,
        "watermark_namespace": observation.watermark_namespace.value,
        "watermark_sequence": observation.watermark_sequence,
    }


def _balance_document(balance: ReconciliationBalance) -> dict[str, object]:
    if type(balance) is PositionReconciliationBalance:
        return {
            "instrument": {
                "symbol": balance.instrument.symbol,
                "venue": balance.instrument.venue.code,
            },
            "kind": ReconciliationBalanceKind.INSTRUMENT_POSITION.value,
            "quantity": balance.quantity.text,
        }
    if type(balance) is CashReconciliationBalance:
        return {
            "amount": balance.amount.text,
            "currency": balance.currency.code,
            "kind": ReconciliationBalanceKind.SETTLEMENT_CASH.value,
        }
    raise _fail(OutcomeCode.INVALID_TYPE, "reconciliation balance must be exact")


def _decode_balance(document: object) -> ReconciliationBalance:
    if type(document) is not dict or type(document.get("kind")) is not str:
        raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation balance document conflicts")
    try:
        kind = ReconciliationBalanceKind(document["kind"])
        if kind is ReconciliationBalanceKind.INSTRUMENT_POSITION:
            if set(document) != {"instrument", "kind", "quantity"}:
                raise _fail(OutcomeCode.CONFLICTING_ID, "position balance fields conflict")
            instrument = document["instrument"]
            if type(instrument) is not dict or set(instrument) != {"symbol", "venue"}:
                raise _fail(OutcomeCode.CONFLICTING_ID, "position instrument fields conflict")
            return PositionReconciliationBalance(
                Instrument(
                    VenueId(_require_text(instrument, "venue")),
                    _require_text(instrument, "symbol"),
                ),
                CanonicalDecimal(_require_text(document, "quantity")),
            )
        if set(document) != {"amount", "currency", "kind"}:
            raise _fail(OutcomeCode.CONFLICTING_ID, "cash balance fields conflict")
        return CashReconciliationBalance(
            SettlementCurrency(_require_text(document, "currency")),
            CanonicalDecimal(_require_text(document, "amount")),
        )
    except (ValueError, TypeError) as error:
        if type(error) is ReconciliationContractError:
            raise
        raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation balance value conflicts") from error


def _economic_id_document(identity: EconomicId) -> dict[str, object]:
    return {
        "owner_kind": identity.owner_kind.value,
        "owner_sequence": identity.owner_sequence,
        "run_id": identity.run_id.value,
    }


def _decode_economic_id(document: object, run_id: RunId) -> EconomicId:
    if type(document) is not dict or set(document) != {"owner_kind", "owner_sequence", "run_id"}:
        raise _fail(OutcomeCode.CONFLICTING_ID, "observation identity document conflicts")
    if _require_text(document, "run_id") != run_id.value:
        raise _fail(OutcomeCode.CONFLICTING_ID, "observation identity run conflicts")
    try:
        return EconomicId(
            run_id,
            EconomicOwnerKind(_require_text(document, "owner_kind")),
            _require_json_int(document, "owner_sequence"),
        )
    except (ValueError, TypeError) as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, "observation identity value conflicts") from error


def _require_uint64(value: object, field: str) -> int:
    if type(value) is not int:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be exact int")
    if not 0 <= value <= _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field} must be uint64")
    return value


def _require_time(value: object, field: str) -> datetime:
    if type(value) is not datetime:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be exact datetime")
    try:
        return require_utc(value, field=field)
    except TimeValidationError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field} must be canonical UTC") from error


def _time_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _decode_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} is not canonical UTC text") from error
    if _time_text(parsed) != value:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} is not canonical UTC text")
    return parsed


def _require_text(document: dict[str, Any], field: str) -> str:
    value = document[field]
    if type(value) is not str:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} must be exact JSON text")
    return value


def _decode_digest(document: dict[str, Any], field: str) -> Sha256Digest:
    try:
        return Sha256Digest(_require_text(document, field))
    except (ValueError, TypeError) as error:
        if type(error) is ReconciliationContractError:
            raise
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} must be a canonical digest") from error


def _require_json_int(document: dict[str, Any], field: str) -> int:
    value = document[field]
    if type(value) is not int:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} must be exact JSON integer")
    return value


def _decode_canonical_json(payload: bytes, field: str) -> object:
    if type(payload) is not bytes:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} payload must be exact bytes")
    if not 1 <= len(payload) <= MAX_RECONCILIATION_PAYLOAD_BYTES:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field} payload exceeds byte bound")
    try:
        document = json.loads(payload, object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} payload is invalid JSON") from error
    _require_json_value(document)
    if _canonical_json(document) != payload:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} payload is not canonical JSON")
    return document


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _require_json_value(value: object) -> None:
    if value is None or type(value) in (bool, str, int):
        return
    if type(value) is list:
        for item in value:
            _require_json_value(item)
        return
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise _fail(OutcomeCode.CONFLICTING_ID, "JSON object keys must be exact text")
        for item in value.values():
            _require_json_value(item)
        return
    raise _fail(OutcomeCode.CONFLICTING_ID, "JSON value type is outside canonical contract")


def _canonical_json(document: object) -> bytes:
    try:
        return json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation document is invalid") from error


def _framed_digest(domain: bytes, payload: bytes) -> Sha256Digest:
    return Sha256Digest(sha256(domain + len(payload).to_bytes(8, "big") + payload).hexdigest())
