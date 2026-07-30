"""Closed canonical execution messages from Accepted ADR 0008."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import NoReturn, cast, final

from ea.core.economics import (
    CanonicalDecimal,
    EconomicValidationError,
    require_positive,
    require_quantized,
)
from ea.core.execution import (
    InstrumentExecutionSpec,
    InstrumentExecutionSpecSet,
    InstrumentSpecId,
    InstrumentSpecSetId,
    SettlementCurrency,
    instrument_spec_set_digest,
)
from ea.core.execution_identity import (
    EconomicId,
    EconomicOwnerKind,
    ExecutionIdentityError,
    ExternalFactId,
    FactDedupIdentity,
    FactDedupKey,
    IngressIdentity,
    SourceNamespace,
    SourceNativeSequence,
)
from ea.core.identity import IdentityValidationError, Instrument, VenueId
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunContractError, RunId, Sha256Digest
from ea.core.time import TimeValidationError, require_utc

MESSAGE_SCHEMA_VERSION = 1

ORDER_INTENT_CANONICALIZATION = "ea-order-intent-v1"
ORDER_INTENT_DIGEST_DOMAIN = b"ea.order-intent.v1\0"
EFFECTIVE_ORDER_INTENT_CANONICALIZATION = "ea-effective-order-intent-v1"
EFFECTIVE_ORDER_INTENT_DIGEST_DOMAIN = b"ea.effective-order-intent.v1\0"
RISK_DECISION_CANONICALIZATION = "ea-risk-decision-v1"
RISK_DECISION_DIGEST_DOMAIN = b"ea.risk-decision.v1\0"
EXECUTION_APPROVAL_CANONICALIZATION = "ea-execution-approval-v1"
EXECUTION_APPROVAL_DIGEST_DOMAIN = b"ea.execution-approval.v1\0"
ORDER_CANONICALIZATION = "ea-order-v1"
ORDER_DIGEST_DOMAIN = b"ea.order.v1\0"
CLIENT_SUBMISSION_KEY_DOMAIN = b"ea.client-submission-key.v1\0"
EXECUTION_REQUEST_CANONICALIZATION = "ea-execution-request-v1"
EXECUTION_REQUEST_DIGEST_DOMAIN = b"ea.execution-request.v1\0"
EXECUTION_FACT_CANONICALIZATION = "ea-execution-fact-v1"
EXECUTION_FACT_DIGEST_DOMAIN = b"ea.execution-fact.v1\0"
EXECUTION_FACT_INGRESS_CANONICALIZATION = "ea-execution-fact-ingress-v1"
EXECUTION_FACT_INGRESS_DIGEST_DOMAIN = b"ea.execution-fact-ingress.v1\0"
FILL_CANONICALIZATION = "ea-fill-v1"
FILL_DIGEST_DOMAIN = b"ea.fill.v1\0"

_TOKEN_PATTERN = re.compile(r"[a-z][a-z0-9._-]{0,127}\Z", flags=re.ASCII)
_MESSAGE_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.CONFLICTING_ID,
        OutcomeCode.FACT_INVALID,
        OutcomeCode.FACT_INVALID_MISSING_DEDUP_IDENTITY,
    }
)
_LIFECYCLE_CODES = frozenset(
    {
        OutcomeCode.ORDER_ACKNOWLEDGED,
        OutcomeCode.ORDER_REJECTED,
        OutcomeCode.ORDER_EXPIRED,
        OutcomeCode.ORDER_EXPIRED_NO_ELIGIBLE_MARKET_DATA,
        OutcomeCode.ORDER_CANCELLED,
    }
)
_SUBMISSION_QUERY_CODES = frozenset(
    {
        OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_SUBMITTED,
        OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_NOT_SUBMITTED,
        OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_REJECTED,
        OutcomeCode.RECONCILIATION_SUBMISSION_CONFIRMED_FILLED,
        OutcomeCode.RECONCILIATION_SUBMISSION_STILL_UNKNOWN,
    }
)
_LIFECYCLE_CODE_BY_KIND: dict[ExecutionFactKind, OutcomeCode]


class ExecutionMessageError(ValueError):
    """Structured fail-closed execution-message error."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _MESSAGE_ERROR_CODES:
            raise TypeError("execution message errors require an exact permitted OutcomeCode")
        self.code = code
        super().__init__(message)


def _fail(code: OutcomeCode, message: str) -> ExecutionMessageError:
    return ExecutionMessageError(code, message)


class OrderSide(StrEnum):
    """Closed side vocabulary."""

    BUY = "buy"
    SELL = "sell"


class OrderKind(StrEnum):
    """Closed Phase 1 order-kind vocabulary."""

    MARKET = "market"


class TimeInForce(StrEnum):
    """Closed Phase 1 time-in-force vocabulary."""

    GOOD_FOR_NEXT_ELIGIBLE_MARKET_EVENT = "good_for_next_eligible_market_event"


class RiskDecisionKind(StrEnum):
    """Closed risk-decision variants."""

    ALLOW = "allow"
    RESIZE = "resize"
    REJECT = "reject"
    EVALUATION_FAILED = "evaluation_failed"


class ExecutionFactKind(StrEnum):
    """Closed execution-fact kinds in ADR 0008 rank order."""

    TRADE = "trade"
    REJECTION = "rejection"
    ACKNOWLEDGEMENT = "acknowledgement"
    EXPIRY = "expiry"
    CANCELLATION = "cancellation"
    SUBMISSION_QUERY = "submission_query"


_LIFECYCLE_CODE_BY_KIND = {
    ExecutionFactKind.ACKNOWLEDGEMENT: OutcomeCode.ORDER_ACKNOWLEDGED,
    ExecutionFactKind.REJECTION: OutcomeCode.ORDER_REJECTED,
    ExecutionFactKind.EXPIRY: OutcomeCode.ORDER_EXPIRED,
    ExecutionFactKind.CANCELLATION: OutcomeCode.ORDER_CANCELLED,
}


class FeeCode(StrEnum):
    """Closed Phase 1 fee code."""

    COMMISSION = "commission"


@final
@dataclass(frozen=True, slots=True)
class ExecutionPolicyId:
    value: str

    def __post_init__(self) -> None:
        _require_token(self.value, field_name="execution_policy_id")


@final
@dataclass(frozen=True, slots=True)
class FactProvenanceId:
    value: str

    def __post_init__(self) -> None:
        _require_token(self.value, field_name="provenance_id")


@final
@dataclass(frozen=True, slots=True)
class VenueOrderId:
    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "venue_order_id must have exact runtime type str",
            )
        if not 1 <= len(self.value) <= 128 or any(
            ord(character) < 0x21 or ord(character) > 0x7E for character in self.value
        ):
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                "venue_order_id must contain 1..128 visible ASCII characters",
            )


@final
@dataclass(frozen=True, slots=True)
class TargetLineageRef:
    """Opaque prevalidated target lineage owned by portfolio."""

    target_id: EconomicId
    target_sha256: Sha256Digest

    def __post_init__(self) -> None:
        _require_id_owner(
            self.target_id,
            EconomicOwnerKind.PORTFOLIO_TARGET,
            field_name="target_id",
        )
        if type(self.target_sha256) is not Sha256Digest:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "target_sha256 must be an exact Sha256Digest",
            )


@final
@dataclass(frozen=True, slots=True)
class ExecutionPolicyRef:
    """Opaque prevalidated execution-policy lineage."""

    identifier: ExecutionPolicyId
    sha256: Sha256Digest

    def __post_init__(self) -> None:
        if type(self.identifier) is not ExecutionPolicyId:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "execution policy identifier must be an exact ExecutionPolicyId",
            )
        if type(self.sha256) is not Sha256Digest:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "execution policy sha256 must be an exact Sha256Digest",
            )


@final
@dataclass(frozen=True, slots=True)
class FactProvenance:
    identifier: FactProvenanceId
    source_payload_sha256: Sha256Digest

    def __post_init__(self) -> None:
        if type(self.identifier) is not FactProvenanceId:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "provenance identifier must be an exact FactProvenanceId",
            )
        if type(self.source_payload_sha256) is not Sha256Digest:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "source_payload_sha256 must be an exact Sha256Digest",
            )


@final
@dataclass(frozen=True, slots=True)
class FeeEntry:
    fee_code: FeeCode
    currency: SettlementCurrency
    amount: CanonicalDecimal

    def __post_init__(self) -> None:
        if type(self.fee_code) is not FeeCode:
            raise _fail(OutcomeCode.INVALID_TYPE, "fee_code must be an exact FeeCode")
        if type(self.currency) is not SettlementCurrency:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "fee currency must be an exact SettlementCurrency",
            )
        if type(self.amount) is not CanonicalDecimal:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "fee amount must be an exact CanonicalDecimal",
            )
        if self.amount.text != "0":
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                "Phase 1 commission amount must be exactly zero",
            )


def _require_token(value: object, *, field_name: str) -> str:
    if type(value) is not str:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            f"{field_name} must have exact runtime type str",
        )
    if _TOKEN_PATTERN.fullmatch(value) is None:
        raise _fail(
            OutcomeCode.OUT_OF_RANGE,
            f"{field_name} must match [a-z][a-z0-9._-]{{0,127}}",
        )
    return value


def _require_non_negative(value: object, *, field_name: str) -> int:
    if type(value) is not int:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            f"{field_name} must have exact runtime type int",
        )
    if value < 0:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} must be non-negative")
    return value


def _require_id_owner(
    identity: object,
    owner: EconomicOwnerKind,
    *,
    field_name: str,
) -> EconomicId:
    if type(identity) is not EconomicId:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            f"{field_name} must be an exact EconomicId",
        )
    if identity.owner_kind is not owner:
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            f"{field_name} has a conflicting owner kind",
        )
    return identity


def _require_same_run(run_id: RunId, *identities: EconomicId | None) -> None:
    if type(run_id) is not RunId:
        raise _fail(OutcomeCode.INVALID_TYPE, "run_id must be an exact RunId")
    for identity in identities:
        if identity is None:
            continue
        if type(identity) is not EconomicId:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "economic ancestry values must be exact EconomicId or None",
            )
        if identity.run_id != run_id:
            raise _fail(OutcomeCode.CONFLICTING_ID, "economic ancestry crosses run scope")


def _require_spec_set(
    spec_set: object,
    *,
    instrument: Instrument,
    specification_id: InstrumentSpecId | None = None,
    set_id: InstrumentSpecSetId | None = None,
    set_sha256: Sha256Digest | None = None,
) -> tuple[InstrumentExecutionSpec, Sha256Digest]:
    if type(spec_set) is not InstrumentExecutionSpecSet:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "spec_set must be an exact InstrumentExecutionSpecSet",
        )
    if type(instrument) is not Instrument:
        raise _fail(OutcomeCode.INVALID_TYPE, "instrument must be an exact Instrument")
    specification = spec_set.require(instrument)
    digest = instrument_spec_set_digest(spec_set)
    if specification_id is not None and (
        type(specification_id) is not InstrumentSpecId
        or specification.specification_id != specification_id
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "instrument specification lineage conflicts")
    if set_id is not None and (
        type(set_id) is not InstrumentSpecSetId or spec_set.identifier != set_id
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "instrument spec-set identity conflicts")
    if set_sha256 is not None and (type(set_sha256) is not Sha256Digest or digest != set_sha256):
        raise _fail(OutcomeCode.CONFLICTING_ID, "instrument spec-set digest conflicts")
    return specification, digest


def _require_quantity(
    quantity: object,
    specification: InstrumentExecutionSpec,
) -> CanonicalDecimal:
    if type(quantity) is not CanonicalDecimal:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "quantity must be an exact CanonicalDecimal",
        )
    require_positive(quantity, field_name="quantity")
    require_quantized(
        quantity,
        specification.quantity_quantum,
        field_name="quantity",
    )
    return quantity


def _require_price(
    price: object,
    specification: InstrumentExecutionSpec,
) -> CanonicalDecimal:
    if type(price) is not CanonicalDecimal:
        raise _fail(OutcomeCode.INVALID_TYPE, "price must be an exact CanonicalDecimal")
    if specification.price_domain.value == "positive" and price.coefficient <= 0:
        raise EconomicValidationError(
            OutcomeCode.PRICE_DOMAIN,
            "price must be strictly positive",
        )
    if specification.price_domain.value == "non_negative" and price.coefficient < 0:
        raise EconomicValidationError(
            OutcomeCode.PRICE_DOMAIN,
            "price must be non-negative",
        )
    require_quantized(price, specification.price_quantum, field_name="price")
    return price


def _require_time(value: object, *, field_name: str) -> datetime:
    if type(value) is not datetime:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            f"{field_name} must have exact runtime type datetime",
        )
    try:
        return require_utc(value, field=field_name)
    except TimeValidationError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error


def _utc_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _decimal_integer_bytes(value: int) -> bytes:
    if type(value) is not int:
        raise TypeError("canonical integer encoder requires exact int")
    if value == 0:
        return b"0"
    sign = b""
    remaining = value
    if remaining < 0:
        sign = b"-"
        remaining = -remaining
    chunks: list[int] = []
    while remaining:
        remaining, chunk = divmod(remaining, 1_000_000_000)
        chunks.append(chunk)
    head = str(chunks.pop()).encode("ascii")
    tail = b"".join(f"{chunk:09d}".encode("ascii") for chunk in reversed(chunks))
    return sign + head + tail


def _encode_json(value: object) -> bytes:
    if value is None:
        return b"null"
    if type(value) is str:
        return json.dumps(value, ensure_ascii=True, allow_nan=False).encode("ascii")
    if type(value) is int:
        return _decimal_integer_bytes(value)
    if type(value) is list:
        return b"[" + b",".join(_encode_json(item) for item in value) + b"]"
    if type(value) is dict:
        mapping = value
        if any(type(key) is not str for key in mapping):
            raise TypeError("canonical JSON object keys must be exact str")
        return (
            b"{"
            + b",".join(
                _encode_json(key) + b":" + _encode_json(mapping[key]) for key in sorted(mapping)
            )
            + b"}"
        )
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _digest(domain: bytes, payload: bytes) -> Sha256Digest:
    return Sha256Digest(sha256(domain + payload).hexdigest())


def _economic_id_document(identity: EconomicId) -> dict[str, object]:
    if type(identity) is not EconomicId:
        raise _fail(OutcomeCode.INVALID_TYPE, "economic ID must be exact")
    return {
        "owner_kind": identity.owner_kind.value,
        "owner_sequence": identity.owner_sequence,
        "run_id": identity.run_id.value,
    }


def _instrument_document(instrument: Instrument) -> dict[str, object]:
    if type(instrument) is not Instrument:
        raise _fail(OutcomeCode.INVALID_TYPE, "instrument must be exact")
    return {
        "symbol": instrument.symbol,
        "venue": instrument.venue.code,
    }


def _target_document(lineage: TargetLineageRef) -> dict[str, object]:
    return {
        "target_id": _economic_id_document(lineage.target_id),
        "target_sha256": lineage.target_sha256.value,
    }


def _policy_document(policy: ExecutionPolicyRef) -> dict[str, object]:
    return {
        "execution_policy_id": policy.identifier.value,
        "execution_policy_sha256": policy.sha256.value,
    }


def _provenance_document(provenance: FactProvenance) -> dict[str, object]:
    return {
        "provenance_id": provenance.identifier.value,
        "source_payload_sha256": provenance.source_payload_sha256.value,
    }


def _dedup_document(identity: FactDedupIdentity) -> dict[str, object]:
    if type(identity) is ExternalFactId:
        return {"kind": "external_id", "value": identity.value}
    if type(identity) is SourceNativeSequence:
        return {"kind": "source_native_sequence", "value": identity.value}
    raise _fail(
        OutcomeCode.INVALID_TYPE,
        "dedup identity must be an exact ExternalFactId or SourceNativeSequence",
    )


def _optional_id_document(identity: EconomicId | None) -> dict[str, object] | None:
    return None if identity is None else _economic_id_document(identity)


@final
@dataclass(frozen=True, slots=True, init=False)
class OrderIntent:
    run_id: RunId
    intent_id: EconomicId
    correlation_id: EconomicId
    causation_id: EconomicId
    target_lineage: TargetLineageRef
    instrument: Instrument
    side: OrderSide
    quantity: CanonicalDecimal
    order_kind: OrderKind
    time_in_force: TimeInForce
    price_constraint: None
    portfolio_snapshot_version: int
    causal_root_available_at: datetime
    dispatch_sequence: int
    instrument_specification_id: InstrumentSpecId
    instrument_spec_set_id: InstrumentSpecSetId
    instrument_spec_set_sha256: Sha256Digest
    execution_policy: ExecutionPolicyRef

    def __init__(self) -> None:
        raise TypeError("OrderIntent values are created only by create_order_intent")


def create_order_intent(
    *,
    run_id: RunId,
    intent_id: EconomicId,
    correlation_id: EconomicId,
    target_lineage: TargetLineageRef,
    instrument: Instrument,
    side: OrderSide,
    quantity: CanonicalDecimal,
    portfolio_snapshot_version: int,
    causal_root_available_at: datetime,
    dispatch_sequence: int,
    spec_set: InstrumentExecutionSpecSet,
    execution_policy: ExecutionPolicyRef,
) -> OrderIntent:
    """Create one valid Phase 1 portfolio intent."""
    if type(run_id) is not RunId:
        raise _fail(OutcomeCode.INVALID_TYPE, "run_id must be an exact RunId")
    _require_id_owner(intent_id, EconomicOwnerKind.PORTFOLIO_INTENT, field_name="intent_id")
    _require_id_owner(
        correlation_id,
        EconomicOwnerKind.STRATEGY_SIGNAL,
        field_name="correlation_id",
    )
    if type(target_lineage) is not TargetLineageRef:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "target_lineage must be an exact TargetLineageRef",
        )
    if type(side) is not OrderSide:
        raise _fail(OutcomeCode.INVALID_TYPE, "side must be an exact OrderSide")
    if type(execution_policy) is not ExecutionPolicyRef:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "execution_policy must be an exact ExecutionPolicyRef",
        )
    _require_same_run(run_id, intent_id, correlation_id, target_lineage.target_id)
    specification, set_digest = _require_spec_set(spec_set, instrument=instrument)
    _require_quantity(quantity, specification)
    snapshot_version = _require_non_negative(
        portfolio_snapshot_version,
        field_name="portfolio_snapshot_version",
    )
    causal_time = _require_time(
        causal_root_available_at,
        field_name="causal_root_available_at",
    )
    sequence = _require_non_negative(dispatch_sequence, field_name="dispatch_sequence")
    value = object.__new__(OrderIntent)
    object.__setattr__(value, "run_id", run_id)
    object.__setattr__(value, "intent_id", intent_id)
    object.__setattr__(value, "correlation_id", correlation_id)
    object.__setattr__(value, "causation_id", target_lineage.target_id)
    object.__setattr__(value, "target_lineage", target_lineage)
    object.__setattr__(value, "instrument", instrument)
    object.__setattr__(value, "side", side)
    object.__setattr__(value, "quantity", quantity)
    object.__setattr__(value, "order_kind", OrderKind.MARKET)
    object.__setattr__(
        value,
        "time_in_force",
        TimeInForce.GOOD_FOR_NEXT_ELIGIBLE_MARKET_EVENT,
    )
    object.__setattr__(value, "price_constraint", None)
    object.__setattr__(value, "portfolio_snapshot_version", snapshot_version)
    object.__setattr__(value, "causal_root_available_at", causal_time)
    object.__setattr__(value, "dispatch_sequence", sequence)
    object.__setattr__(
        value,
        "instrument_specification_id",
        specification.specification_id,
    )
    object.__setattr__(value, "instrument_spec_set_id", spec_set.identifier)
    object.__setattr__(value, "instrument_spec_set_sha256", set_digest)
    object.__setattr__(value, "execution_policy", execution_policy)
    return value


def _order_intent_document(intent: OrderIntent) -> dict[str, object]:
    if type(intent) is not OrderIntent:
        raise _fail(OutcomeCode.INVALID_TYPE, "intent must be an exact OrderIntent")
    return {
        "canonicalization": ORDER_INTENT_CANONICALIZATION,
        "causal_root_available_at": _utc_text(intent.causal_root_available_at),
        "causation_id": _economic_id_document(intent.causation_id),
        "correlation_id": _economic_id_document(intent.correlation_id),
        "dispatch_sequence": intent.dispatch_sequence,
        "execution_policy": _policy_document(intent.execution_policy),
        "instrument": _instrument_document(intent.instrument),
        "instrument_spec_set_id": intent.instrument_spec_set_id.value,
        "instrument_spec_set_sha256": intent.instrument_spec_set_sha256.value,
        "instrument_specification_id": intent.instrument_specification_id.value,
        "intent_id": _economic_id_document(intent.intent_id),
        "message_type": "order_intent",
        "order_kind": intent.order_kind.value,
        "portfolio_snapshot_version": intent.portfolio_snapshot_version,
        "price_constraint": None,
        "quantity": intent.quantity.text,
        "run_id": intent.run_id.value,
        "schema_version": MESSAGE_SCHEMA_VERSION,
        "side": intent.side.value,
        "target_lineage": _target_document(intent.target_lineage),
        "time_in_force": intent.time_in_force.value,
    }


def canonical_order_intent_bytes(intent: OrderIntent) -> bytes:
    return _encode_json(_order_intent_document(intent))


def order_intent_digest(intent: OrderIntent) -> Sha256Digest:
    return _digest(ORDER_INTENT_DIGEST_DOMAIN, canonical_order_intent_bytes(intent))


def _effective_intent_document(
    intent: OrderIntent,
    approved_quantity: CanonicalDecimal,
) -> dict[str, object]:
    return {
        "approved_quantity": approved_quantity.text,
        "canonicalization": EFFECTIVE_ORDER_INTENT_CANONICALIZATION,
        "intent_id": _economic_id_document(intent.intent_id),
        "original_intent_sha256": order_intent_digest(intent).value,
        "projection_type": "effective_order_intent",
        "run_id": intent.run_id.value,
        "schema_version": MESSAGE_SCHEMA_VERSION,
    }


def canonical_effective_order_intent_bytes(
    intent: OrderIntent,
    approved_quantity: CanonicalDecimal,
) -> bytes:
    if type(intent) is not OrderIntent or type(approved_quantity) is not CanonicalDecimal:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "effective intent requires exact intent and approved quantity",
        )
    return _encode_json(_effective_intent_document(intent, approved_quantity))


def effective_order_intent_digest(
    intent: OrderIntent,
    approved_quantity: CanonicalDecimal,
) -> Sha256Digest:
    return _digest(
        EFFECTIVE_ORDER_INTENT_DIGEST_DOMAIN,
        canonical_effective_order_intent_bytes(intent, approved_quantity),
    )


@final
@dataclass(frozen=True, slots=True, init=False)
class ExecutionApproval:
    run_id: RunId
    approval_id: EconomicId
    decision_id: EconomicId
    intent_id: EconomicId
    correlation_id: EconomicId
    causation_id: EconomicId
    original_intent_sha256: Sha256Digest
    effective_intent_sha256: Sha256Digest
    approved_quantity: CanonicalDecimal
    portfolio_snapshot_version: int
    risk_state_version: int
    causal_root_available_at: datetime
    dispatch_sequence: int

    def __init__(self) -> None:
        raise TypeError("ExecutionApproval values are created only by risk-decision factories")


@final
@dataclass(frozen=True, slots=True, init=False)
class RiskDecision:
    run_id: RunId
    decision_id: EconomicId
    intent_id: EconomicId
    correlation_id: EconomicId
    causation_id: EconomicId
    intent_sha256: Sha256Digest
    kind: RiskDecisionKind
    outcome_code: OutcomeCode
    portfolio_snapshot_version: int
    risk_state_version: int
    causal_root_available_at: datetime
    dispatch_sequence: int
    approved_quantity: CanonicalDecimal | None
    effective_intent_sha256: Sha256Digest | None
    approval: ExecutionApproval | None

    def __init__(self) -> None:
        raise TypeError("RiskDecision values are created only by risk-decision factories")


def _make_approval(
    *,
    approval_id: EconomicId,
    decision_id: EconomicId,
    intent: OrderIntent,
    approved_quantity: CanonicalDecimal,
    effective_digest: Sha256Digest,
    risk_state_version: int,
) -> ExecutionApproval:
    _require_id_owner(
        approval_id,
        EconomicOwnerKind.RISK_APPROVAL,
        field_name="approval_id",
    )
    _require_same_run(intent.run_id, approval_id, decision_id)
    value = object.__new__(ExecutionApproval)
    object.__setattr__(value, "run_id", intent.run_id)
    object.__setattr__(value, "approval_id", approval_id)
    object.__setattr__(value, "decision_id", decision_id)
    object.__setattr__(value, "intent_id", intent.intent_id)
    object.__setattr__(value, "correlation_id", intent.correlation_id)
    object.__setattr__(value, "causation_id", decision_id)
    object.__setattr__(value, "original_intent_sha256", order_intent_digest(intent))
    object.__setattr__(value, "effective_intent_sha256", effective_digest)
    object.__setattr__(value, "approved_quantity", approved_quantity)
    object.__setattr__(
        value,
        "portfolio_snapshot_version",
        intent.portfolio_snapshot_version,
    )
    object.__setattr__(value, "risk_state_version", risk_state_version)
    object.__setattr__(
        value,
        "causal_root_available_at",
        intent.causal_root_available_at,
    )
    object.__setattr__(value, "dispatch_sequence", intent.dispatch_sequence)
    return value


def _create_risk_decision(
    *,
    kind: RiskDecisionKind,
    decision_id: EconomicId,
    intent: OrderIntent,
    spec_set: InstrumentExecutionSpecSet,
    risk_state_version: int,
    approval_id: EconomicId | None,
    approved_quantity: CanonicalDecimal | None,
) -> RiskDecision:
    if type(intent) is not OrderIntent:
        raise _fail(OutcomeCode.INVALID_TYPE, "intent must be an exact OrderIntent")
    _require_id_owner(
        decision_id,
        EconomicOwnerKind.RISK_DECISION,
        field_name="decision_id",
    )
    _require_same_run(intent.run_id, decision_id)
    specification, _ = _require_spec_set(
        spec_set,
        instrument=intent.instrument,
        specification_id=intent.instrument_specification_id,
        set_id=intent.instrument_spec_set_id,
        set_sha256=intent.instrument_spec_set_sha256,
    )
    risk_version = _require_non_negative(risk_state_version, field_name="risk_state_version")
    approval: ExecutionApproval | None = None
    effective_digest: Sha256Digest | None = None
    outcome_by_kind = {
        RiskDecisionKind.ALLOW: OutcomeCode.RISK_ALLOWED,
        RiskDecisionKind.RESIZE: OutcomeCode.RISK_RESIZED,
        RiskDecisionKind.REJECT: OutcomeCode.RISK_REJECTED,
        RiskDecisionKind.EVALUATION_FAILED: OutcomeCode.RISK_EVALUATION_FAILED,
    }
    if kind in (RiskDecisionKind.ALLOW, RiskDecisionKind.RESIZE):
        if type(approval_id) is not EconomicId or type(approved_quantity) is not CanonicalDecimal:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "executable risk decisions require exact approval ID and quantity",
            )
        _require_quantity(approved_quantity, specification)
        if approved_quantity > intent.quantity:
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                "approved quantity cannot exceed original intent quantity",
            )
        if kind is RiskDecisionKind.ALLOW and approved_quantity != intent.quantity:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "allow must preserve the original intent quantity",
            )
        if kind is RiskDecisionKind.RESIZE and approved_quantity == intent.quantity:
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                "resize must reduce the original intent quantity",
            )
        effective_digest = effective_order_intent_digest(intent, approved_quantity)
        approval = _make_approval(
            approval_id=approval_id,
            decision_id=decision_id,
            intent=intent,
            approved_quantity=approved_quantity,
            effective_digest=effective_digest,
            risk_state_version=risk_version,
        )
    elif approval_id is not None or approved_quantity is not None:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "non-executable risk decisions cannot carry approval fields",
        )

    value = object.__new__(RiskDecision)
    object.__setattr__(value, "run_id", intent.run_id)
    object.__setattr__(value, "decision_id", decision_id)
    object.__setattr__(value, "intent_id", intent.intent_id)
    object.__setattr__(value, "correlation_id", intent.correlation_id)
    object.__setattr__(value, "causation_id", intent.intent_id)
    object.__setattr__(value, "intent_sha256", order_intent_digest(intent))
    object.__setattr__(value, "kind", kind)
    object.__setattr__(value, "outcome_code", outcome_by_kind[kind])
    object.__setattr__(
        value,
        "portfolio_snapshot_version",
        intent.portfolio_snapshot_version,
    )
    object.__setattr__(value, "risk_state_version", risk_version)
    object.__setattr__(
        value,
        "causal_root_available_at",
        intent.causal_root_available_at,
    )
    object.__setattr__(value, "dispatch_sequence", intent.dispatch_sequence)
    object.__setattr__(value, "approved_quantity", approved_quantity)
    object.__setattr__(value, "effective_intent_sha256", effective_digest)
    object.__setattr__(value, "approval", approval)
    return value


def allow_order_intent(
    *,
    decision_id: EconomicId,
    approval_id: EconomicId,
    intent: OrderIntent,
    spec_set: InstrumentExecutionSpecSet,
    risk_state_version: int,
) -> RiskDecision:
    return _create_risk_decision(
        kind=RiskDecisionKind.ALLOW,
        decision_id=decision_id,
        intent=intent,
        spec_set=spec_set,
        risk_state_version=risk_state_version,
        approval_id=approval_id,
        approved_quantity=intent.quantity,
    )


def resize_order_intent(
    *,
    decision_id: EconomicId,
    approval_id: EconomicId,
    intent: OrderIntent,
    approved_quantity: CanonicalDecimal,
    spec_set: InstrumentExecutionSpecSet,
    risk_state_version: int,
) -> RiskDecision:
    return _create_risk_decision(
        kind=RiskDecisionKind.RESIZE,
        decision_id=decision_id,
        intent=intent,
        spec_set=spec_set,
        risk_state_version=risk_state_version,
        approval_id=approval_id,
        approved_quantity=approved_quantity,
    )


def reject_order_intent(
    *,
    decision_id: EconomicId,
    intent: OrderIntent,
    spec_set: InstrumentExecutionSpecSet,
    risk_state_version: int,
) -> RiskDecision:
    return _create_risk_decision(
        kind=RiskDecisionKind.REJECT,
        decision_id=decision_id,
        intent=intent,
        spec_set=spec_set,
        risk_state_version=risk_state_version,
        approval_id=None,
        approved_quantity=None,
    )


def fail_order_intent_evaluation(
    *,
    decision_id: EconomicId,
    intent: OrderIntent,
    spec_set: InstrumentExecutionSpecSet,
    risk_state_version: int,
) -> RiskDecision:
    return _create_risk_decision(
        kind=RiskDecisionKind.EVALUATION_FAILED,
        decision_id=decision_id,
        intent=intent,
        spec_set=spec_set,
        risk_state_version=risk_state_version,
        approval_id=None,
        approved_quantity=None,
    )


def _approval_document(approval: ExecutionApproval) -> dict[str, object]:
    if type(approval) is not ExecutionApproval:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "approval must be an exact ExecutionApproval",
        )
    return {
        "approval_id": _economic_id_document(approval.approval_id),
        "approved_quantity": approval.approved_quantity.text,
        "canonicalization": EXECUTION_APPROVAL_CANONICALIZATION,
        "causal_root_available_at": _utc_text(approval.causal_root_available_at),
        "causation_id": _economic_id_document(approval.causation_id),
        "correlation_id": _economic_id_document(approval.correlation_id),
        "decision_id": _economic_id_document(approval.decision_id),
        "dispatch_sequence": approval.dispatch_sequence,
        "effective_intent_sha256": approval.effective_intent_sha256.value,
        "intent_id": _economic_id_document(approval.intent_id),
        "message_type": "execution_approval",
        "original_intent_sha256": approval.original_intent_sha256.value,
        "portfolio_snapshot_version": approval.portfolio_snapshot_version,
        "risk_state_version": approval.risk_state_version,
        "run_id": approval.run_id.value,
        "schema_version": MESSAGE_SCHEMA_VERSION,
    }


def canonical_execution_approval_bytes(approval: ExecutionApproval) -> bytes:
    return _encode_json(_approval_document(approval))


def execution_approval_digest(approval: ExecutionApproval) -> Sha256Digest:
    return _digest(
        EXECUTION_APPROVAL_DIGEST_DOMAIN,
        canonical_execution_approval_bytes(approval),
    )


def _risk_decision_document(decision: RiskDecision) -> dict[str, object]:
    if type(decision) is not RiskDecision:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "decision must be an exact RiskDecision",
        )
    return {
        "approval": None if decision.approval is None else _approval_document(decision.approval),
        "approved_quantity": (
            None if decision.approved_quantity is None else decision.approved_quantity.text
        ),
        "canonicalization": RISK_DECISION_CANONICALIZATION,
        "causal_root_available_at": _utc_text(decision.causal_root_available_at),
        "causation_id": _economic_id_document(decision.causation_id),
        "correlation_id": _economic_id_document(decision.correlation_id),
        "decision_id": _economic_id_document(decision.decision_id),
        "dispatch_sequence": decision.dispatch_sequence,
        "effective_intent_sha256": (
            None
            if decision.effective_intent_sha256 is None
            else decision.effective_intent_sha256.value
        ),
        "intent_id": _economic_id_document(decision.intent_id),
        "intent_sha256": decision.intent_sha256.value,
        "kind": decision.kind.value,
        "message_type": "risk_decision",
        "outcome_code": decision.outcome_code.value,
        "portfolio_snapshot_version": decision.portfolio_snapshot_version,
        "risk_state_version": decision.risk_state_version,
        "run_id": decision.run_id.value,
        "schema_version": MESSAGE_SCHEMA_VERSION,
    }


def canonical_risk_decision_bytes(decision: RiskDecision) -> bytes:
    return _encode_json(_risk_decision_document(decision))


def risk_decision_digest(decision: RiskDecision) -> Sha256Digest:
    return _digest(RISK_DECISION_DIGEST_DOMAIN, canonical_risk_decision_bytes(decision))


@final
@dataclass(frozen=True, slots=True, init=False)
class Order:
    run_id: RunId
    order_id: EconomicId
    intent_id: EconomicId
    correlation_id: EconomicId
    causation_id: EconomicId
    decision_id: EconomicId
    approval_id: EconomicId
    original_intent_sha256: Sha256Digest
    effective_intent_sha256: Sha256Digest
    risk_decision_sha256: Sha256Digest
    approval_sha256: Sha256Digest
    portfolio_snapshot_version: int
    risk_state_version: int
    dispatch_sequence: int
    instrument: Instrument
    side: OrderSide
    quantity: CanonicalDecimal
    order_kind: OrderKind
    time_in_force: TimeInForce
    price_constraint: None
    instrument_specification_id: InstrumentSpecId
    instrument_spec_set_id: InstrumentSpecSetId
    instrument_spec_set_sha256: Sha256Digest
    execution_policy: ExecutionPolicyRef
    eligible_after_available_at: datetime

    def __init__(self) -> None:
        raise TypeError("Order values are created only by create_order")

    @property
    def client_submission_key(self) -> Sha256Digest:
        return order_client_submission_key(self)


def create_order(
    *,
    order_id: EconomicId,
    intent: OrderIntent,
    decision: RiskDecision,
    spec_set: InstrumentExecutionSpecSet,
) -> Order:
    """Materialize one approved Order without consuming mutable approval state."""
    if type(intent) is not OrderIntent or type(decision) is not RiskDecision:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "Order construction requires exact intent and decision",
        )
    _require_id_owner(order_id, EconomicOwnerKind.EXECUTION_ORDER, field_name="order_id")
    _require_same_run(intent.run_id, order_id, decision.decision_id)
    if (
        decision.kind not in (RiskDecisionKind.ALLOW, RiskDecisionKind.RESIZE)
        or decision.approval is None
        or decision.approved_quantity is None
        or decision.effective_intent_sha256 is None
    ):
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "only an executable allow/resize decision can create an Order",
        )
    if (
        decision.intent_id != intent.intent_id
        or decision.intent_sha256 != order_intent_digest(intent)
        or decision.correlation_id != intent.correlation_id
        or decision.causation_id != intent.intent_id
        or decision.portfolio_snapshot_version != intent.portfolio_snapshot_version
        or decision.causal_root_available_at != intent.causal_root_available_at
        or decision.dispatch_sequence != intent.dispatch_sequence
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "risk decision does not bind the intent")
    approval = decision.approval
    if (
        approval.decision_id != decision.decision_id
        or approval.intent_id != intent.intent_id
        or approval.correlation_id != intent.correlation_id
        or approval.causation_id != decision.decision_id
        or approval.original_intent_sha256 != order_intent_digest(intent)
        or approval.effective_intent_sha256 != decision.effective_intent_sha256
        or approval.approved_quantity != decision.approved_quantity
        or approval.portfolio_snapshot_version != intent.portfolio_snapshot_version
        or approval.risk_state_version != decision.risk_state_version
        or approval.causal_root_available_at != intent.causal_root_available_at
        or approval.dispatch_sequence != intent.dispatch_sequence
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "approval does not bind the risk decision")
    specification, _ = _require_spec_set(
        spec_set,
        instrument=intent.instrument,
        specification_id=intent.instrument_specification_id,
        set_id=intent.instrument_spec_set_id,
        set_sha256=intent.instrument_spec_set_sha256,
    )
    _require_quantity(decision.approved_quantity, specification)
    value = object.__new__(Order)
    object.__setattr__(value, "run_id", intent.run_id)
    object.__setattr__(value, "order_id", order_id)
    object.__setattr__(value, "intent_id", intent.intent_id)
    object.__setattr__(value, "correlation_id", intent.correlation_id)
    object.__setattr__(value, "causation_id", approval.approval_id)
    object.__setattr__(value, "decision_id", decision.decision_id)
    object.__setattr__(value, "approval_id", approval.approval_id)
    object.__setattr__(value, "original_intent_sha256", order_intent_digest(intent))
    object.__setattr__(
        value,
        "effective_intent_sha256",
        decision.effective_intent_sha256,
    )
    object.__setattr__(value, "risk_decision_sha256", risk_decision_digest(decision))
    object.__setattr__(value, "approval_sha256", execution_approval_digest(approval))
    object.__setattr__(
        value,
        "portfolio_snapshot_version",
        intent.portfolio_snapshot_version,
    )
    object.__setattr__(value, "risk_state_version", decision.risk_state_version)
    object.__setattr__(value, "dispatch_sequence", intent.dispatch_sequence)
    object.__setattr__(value, "instrument", intent.instrument)
    object.__setattr__(value, "side", intent.side)
    object.__setattr__(value, "quantity", decision.approved_quantity)
    object.__setattr__(value, "order_kind", intent.order_kind)
    object.__setattr__(value, "time_in_force", intent.time_in_force)
    object.__setattr__(value, "price_constraint", None)
    object.__setattr__(
        value,
        "instrument_specification_id",
        intent.instrument_specification_id,
    )
    object.__setattr__(value, "instrument_spec_set_id", intent.instrument_spec_set_id)
    object.__setattr__(
        value,
        "instrument_spec_set_sha256",
        intent.instrument_spec_set_sha256,
    )
    object.__setattr__(value, "execution_policy", intent.execution_policy)
    object.__setattr__(
        value,
        "eligible_after_available_at",
        intent.causal_root_available_at,
    )
    return value


def _order_document(order: Order) -> dict[str, object]:
    if type(order) is not Order:
        raise _fail(OutcomeCode.INVALID_TYPE, "order must be an exact Order")
    return {
        "approval_id": _economic_id_document(order.approval_id),
        "approval_sha256": order.approval_sha256.value,
        "canonicalization": ORDER_CANONICALIZATION,
        "causation_id": _economic_id_document(order.causation_id),
        "correlation_id": _economic_id_document(order.correlation_id),
        "decision_id": _economic_id_document(order.decision_id),
        "dispatch_sequence": order.dispatch_sequence,
        "effective_intent_sha256": order.effective_intent_sha256.value,
        "eligible_after_available_at": _utc_text(order.eligible_after_available_at),
        "execution_policy": _policy_document(order.execution_policy),
        "instrument": _instrument_document(order.instrument),
        "instrument_spec_set_id": order.instrument_spec_set_id.value,
        "instrument_spec_set_sha256": order.instrument_spec_set_sha256.value,
        "instrument_specification_id": order.instrument_specification_id.value,
        "intent_id": _economic_id_document(order.intent_id),
        "message_type": "order",
        "order_id": _economic_id_document(order.order_id),
        "order_kind": order.order_kind.value,
        "original_intent_sha256": order.original_intent_sha256.value,
        "portfolio_snapshot_version": order.portfolio_snapshot_version,
        "price_constraint": None,
        "quantity": order.quantity.text,
        "risk_decision_sha256": order.risk_decision_sha256.value,
        "risk_state_version": order.risk_state_version,
        "run_id": order.run_id.value,
        "schema_version": MESSAGE_SCHEMA_VERSION,
        "side": order.side.value,
        "time_in_force": order.time_in_force.value,
    }


def canonical_order_bytes(order: Order) -> bytes:
    return _encode_json(_order_document(order))


def order_digest(order: Order) -> Sha256Digest:
    return _digest(ORDER_DIGEST_DOMAIN, canonical_order_bytes(order))


def order_client_submission_key(order: Order) -> Sha256Digest:
    return _digest(CLIENT_SUBMISSION_KEY_DOMAIN, canonical_order_bytes(order))


def _execution_request_document(order: Order) -> dict[str, object]:
    return {
        "canonicalization": EXECUTION_REQUEST_CANONICALIZATION,
        "client_submission_key": order.client_submission_key.value,
        "eligible_after_available_at": _utc_text(order.eligible_after_available_at),
        "execution_policy": _policy_document(order.execution_policy),
        "instrument": _instrument_document(order.instrument),
        "instrument_spec_set_id": order.instrument_spec_set_id.value,
        "instrument_spec_set_sha256": order.instrument_spec_set_sha256.value,
        "instrument_specification_id": order.instrument_specification_id.value,
        "order_id": _economic_id_document(order.order_id),
        "order_kind": order.order_kind.value,
        "order_sha256": order_digest(order).value,
        "price_constraint": None,
        "projection_type": "execution_request",
        "quantity": order.quantity.text,
        "run_id": order.run_id.value,
        "schema_version": MESSAGE_SCHEMA_VERSION,
        "side": order.side.value,
        "time_in_force": order.time_in_force.value,
    }


def canonical_execution_request_bytes(order: Order) -> bytes:
    if type(order) is not Order:
        raise _fail(OutcomeCode.INVALID_TYPE, "order must be an exact Order")
    return _encode_json(_execution_request_document(order))


def execution_request_digest(order: Order) -> Sha256Digest:
    return _digest(
        EXECUTION_REQUEST_DIGEST_DOMAIN,
        canonical_execution_request_bytes(order),
    )


@final
@dataclass(frozen=True, slots=True)
class LifecycleFactPayload:
    outcome_code: OutcomeCode

    def __post_init__(self) -> None:
        if type(self.outcome_code) is not OutcomeCode:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "lifecycle outcome must be an exact OutcomeCode",
            )
        if self.outcome_code not in _LIFECYCLE_CODES:
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                "lifecycle outcome is not a declared fact value",
            )


@final
@dataclass(frozen=True, slots=True, init=False)
class TradeFactPayload:
    side: OrderSide
    quantity: CanonicalDecimal
    price: CanonicalDecimal
    fees: tuple[FeeEntry, ...]
    instrument_specification_id: InstrumentSpecId
    instrument_spec_set_id: InstrumentSpecSetId
    instrument_spec_set_sha256: Sha256Digest

    def __init__(self) -> None:
        raise TypeError("TradeFactPayload values are created only by trade fact construction")


@final
@dataclass(frozen=True, slots=True)
class SubmissionQueryFactPayload:
    outcome_code: OutcomeCode

    def __post_init__(self) -> None:
        if type(self.outcome_code) is not OutcomeCode:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "submission query outcome must be an exact OutcomeCode",
            )
        if self.outcome_code not in _SUBMISSION_QUERY_CODES:
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                "submission query outcome is not a declared reconciliation value",
            )


ExecutionFactPayload = LifecycleFactPayload | TradeFactPayload | SubmissionQueryFactPayload


@final
@dataclass(frozen=True, slots=True, init=False)
class ExecutionFact:
    source_namespace: SourceNamespace
    dedup_identity: FactDedupIdentity
    kind: ExecutionFactKind
    occurred_at: datetime
    instrument: Instrument | None
    client_submission_key: Sha256Digest | None
    venue_order_id: VenueOrderId | None
    order_id: EconomicId | None
    correlation_id: EconomicId | None
    causation_id: EconomicId | None
    payload: ExecutionFactPayload
    provenance: FactProvenance

    def __init__(self) -> None:
        raise TypeError("ExecutionFact values are created only by fact factories")

    @property
    def dedup_key(self) -> FactDedupKey:
        return FactDedupKey(self.source_namespace, self.dedup_identity)

    @property
    def fact_sha256(self) -> Sha256Digest:
        return execution_fact_digest(self)


def _require_fact_common(
    *,
    source_namespace: object,
    dedup_identity: object,
    occurred_at: object,
    provenance: object,
    instrument: object,
    client_submission_key: object,
    venue_order_id: object,
    order_id: object,
    correlation_id: object,
    causation_id: object,
    require_order_cause: bool,
) -> tuple[
    SourceNamespace,
    FactDedupIdentity,
    datetime,
    FactProvenance,
    Instrument | None,
    Sha256Digest | None,
    VenueOrderId | None,
    EconomicId | None,
    EconomicId | None,
    EconomicId | None,
]:
    if type(source_namespace) is not SourceNamespace:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "source_namespace must be an exact SourceNamespace",
        )
    if type(dedup_identity) is ExternalFactId:
        identity: FactDedupIdentity = dedup_identity
    elif type(dedup_identity) is SourceNativeSequence:
        identity = dedup_identity
    else:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "dedup identity must be an exact declared variant",
        )
    time = _require_time(occurred_at, field_name="occurred_at")
    if type(provenance) is not FactProvenance:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "provenance must be an exact FactProvenance",
        )
    if instrument is not None and type(instrument) is not Instrument:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "instrument must be an exact Instrument or None",
        )
    if client_submission_key is not None and type(client_submission_key) is not Sha256Digest:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "client_submission_key must be an exact Sha256Digest or None",
        )
    if venue_order_id is not None and type(venue_order_id) is not VenueOrderId:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "venue_order_id must be an exact VenueOrderId or None",
        )
    known_order: EconomicId | None
    if order_id is None:
        known_order = None
    elif type(order_id) is EconomicId:
        _require_id_owner(
            order_id,
            EconomicOwnerKind.EXECUTION_ORDER,
            field_name="order_id",
        )
        known_order = order_id
    else:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "order_id must be an exact EconomicId or None",
        )
    correlation: EconomicId | None
    if correlation_id is None:
        correlation = None
    elif type(correlation_id) is EconomicId:
        _require_id_owner(
            correlation_id,
            EconomicOwnerKind.STRATEGY_SIGNAL,
            field_name="correlation_id",
        )
        correlation = correlation_id
    else:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "correlation_id must be an exact EconomicId or None",
        )
    cause: EconomicId | None
    if causation_id is None:
        cause = None
    elif type(causation_id) is EconomicId:
        if causation_id.owner_kind not in (
            EconomicOwnerKind.EXECUTION_ORDER,
            EconomicOwnerKind.RECONCILIATION_OBSERVATION,
        ):
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "fact causation owner kind is not permitted",
            )
        cause = causation_id
    else:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "causation_id must be an exact EconomicId or None",
        )
    present = tuple(
        identity for identity in (known_order, correlation, cause) if identity is not None
    )
    if present:
        _require_same_run(present[0].run_id, *present)
    if require_order_cause and known_order is not None and cause != known_order:
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "known lifecycle/trade order requires equal causation_id",
        )
    return (
        source_namespace,
        identity,
        time,
        provenance,
        instrument,
        client_submission_key,
        venue_order_id,
        known_order,
        correlation,
        cause,
    )


def _new_fact(
    *,
    source_namespace: SourceNamespace,
    dedup_identity: FactDedupIdentity,
    kind: ExecutionFactKind,
    occurred_at: datetime,
    instrument: Instrument | None,
    client_submission_key: Sha256Digest | None,
    venue_order_id: VenueOrderId | None,
    order_id: EconomicId | None,
    correlation_id: EconomicId | None,
    causation_id: EconomicId | None,
    payload: ExecutionFactPayload,
    provenance: FactProvenance,
) -> ExecutionFact:
    value = object.__new__(ExecutionFact)
    object.__setattr__(value, "source_namespace", source_namespace)
    object.__setattr__(value, "dedup_identity", dedup_identity)
    object.__setattr__(value, "kind", kind)
    object.__setattr__(value, "occurred_at", occurred_at)
    object.__setattr__(value, "instrument", instrument)
    object.__setattr__(value, "client_submission_key", client_submission_key)
    object.__setattr__(value, "venue_order_id", venue_order_id)
    object.__setattr__(value, "order_id", order_id)
    object.__setattr__(value, "correlation_id", correlation_id)
    object.__setattr__(value, "causation_id", causation_id)
    object.__setattr__(value, "payload", payload)
    object.__setattr__(value, "provenance", provenance)
    return value


def create_lifecycle_execution_fact(
    *,
    kind: ExecutionFactKind,
    source_namespace: SourceNamespace,
    dedup_identity: FactDedupIdentity,
    occurred_at: datetime,
    provenance: FactProvenance,
    instrument: Instrument | None = None,
    client_submission_key: Sha256Digest | None = None,
    venue_order_id: VenueOrderId | None = None,
    order_id: EconomicId | None = None,
    correlation_id: EconomicId | None = None,
    causation_id: EconomicId | None = None,
    outcome_code: OutcomeCode | None = None,
) -> ExecutionFact:
    if type(kind) is not ExecutionFactKind:
        raise _fail(OutcomeCode.INVALID_TYPE, "kind must be an exact ExecutionFactKind")
    if kind not in _LIFECYCLE_CODE_BY_KIND:
        raise _fail(
            OutcomeCode.OUT_OF_RANGE,
            "lifecycle fact kind must be acknowledgement/rejection/expiry/cancellation",
        )
    if outcome_code is not None and type(outcome_code) is not OutcomeCode:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "outcome_code must be an exact OutcomeCode or None",
        )
    if outcome_code is None:
        selected_outcome = _LIFECYCLE_CODE_BY_KIND[kind]
    elif (
        kind is ExecutionFactKind.EXPIRY
        and outcome_code is OutcomeCode.ORDER_EXPIRED_NO_ELIGIBLE_MARKET_DATA
    ):
        selected_outcome = outcome_code
    else:
        raise _fail(
            OutcomeCode.OUT_OF_RANGE,
            "explicit lifecycle outcome is not allowed for this fact kind",
        )
    (
        source,
        identity,
        time,
        source_provenance,
        known_instrument,
        client_key,
        venue_id,
        known_order,
        correlation,
        cause,
    ) = _require_fact_common(
        source_namespace=source_namespace,
        dedup_identity=dedup_identity,
        occurred_at=occurred_at,
        provenance=provenance,
        instrument=instrument,
        client_submission_key=client_submission_key,
        venue_order_id=venue_order_id,
        order_id=order_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
        require_order_cause=True,
    )
    return _new_fact(
        source_namespace=source,
        dedup_identity=identity,
        kind=kind,
        occurred_at=time,
        instrument=known_instrument,
        client_submission_key=client_key,
        venue_order_id=venue_id,
        order_id=known_order,
        correlation_id=correlation,
        causation_id=cause,
        payload=LifecycleFactPayload(selected_outcome),
        provenance=source_provenance,
    )


def _phase1_trade_payload(
    *,
    spec_set: InstrumentExecutionSpecSet,
    instrument: Instrument,
    side: OrderSide,
    quantity: CanonicalDecimal,
    price: CanonicalDecimal,
) -> TradeFactPayload:
    if type(side) is not OrderSide:
        raise _fail(OutcomeCode.INVALID_TYPE, "side must be an exact OrderSide")
    specification, set_digest = _require_spec_set(spec_set, instrument=instrument)
    _require_quantity(quantity, specification)
    _require_price(price, specification)
    zero = CanonicalDecimal("0")
    require_quantized(zero, specification.currency_quantum, field_name="fee")
    fee = FeeEntry(FeeCode.COMMISSION, specification.settlement_currency, zero)
    payload = object.__new__(TradeFactPayload)
    object.__setattr__(payload, "side", side)
    object.__setattr__(payload, "quantity", quantity)
    object.__setattr__(payload, "price", price)
    object.__setattr__(payload, "fees", (fee,))
    object.__setattr__(
        payload,
        "instrument_specification_id",
        specification.specification_id,
    )
    object.__setattr__(payload, "instrument_spec_set_id", spec_set.identifier)
    object.__setattr__(payload, "instrument_spec_set_sha256", set_digest)
    return payload


def create_trade_execution_fact(
    *,
    source_namespace: SourceNamespace,
    dedup_identity: FactDedupIdentity,
    occurred_at: datetime,
    provenance: FactProvenance,
    spec_set: InstrumentExecutionSpecSet,
    instrument: Instrument,
    side: OrderSide,
    quantity: CanonicalDecimal,
    price: CanonicalDecimal,
    client_submission_key: Sha256Digest | None = None,
    venue_order_id: VenueOrderId | None = None,
    order_id: EconomicId | None = None,
    correlation_id: EconomicId | None = None,
    causation_id: EconomicId | None = None,
) -> ExecutionFact:
    (
        source,
        identity,
        time,
        source_provenance,
        known_instrument,
        client_key,
        venue_id,
        known_order,
        correlation,
        cause,
    ) = _require_fact_common(
        source_namespace=source_namespace,
        dedup_identity=dedup_identity,
        occurred_at=occurred_at,
        provenance=provenance,
        instrument=instrument,
        client_submission_key=client_submission_key,
        venue_order_id=venue_order_id,
        order_id=order_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
        require_order_cause=True,
    )
    if known_instrument is None:
        raise _fail(OutcomeCode.INVALID_TYPE, "trade fact requires an instrument")
    payload = _phase1_trade_payload(
        spec_set=spec_set,
        instrument=known_instrument,
        side=side,
        quantity=quantity,
        price=price,
    )
    return _new_fact(
        source_namespace=source,
        dedup_identity=identity,
        kind=ExecutionFactKind.TRADE,
        occurred_at=time,
        instrument=known_instrument,
        client_submission_key=client_key,
        venue_order_id=venue_id,
        order_id=known_order,
        correlation_id=correlation,
        causation_id=cause,
        payload=payload,
        provenance=source_provenance,
    )


def create_submission_query_execution_fact(
    *,
    source_namespace: SourceNamespace,
    dedup_identity: FactDedupIdentity,
    occurred_at: datetime,
    provenance: FactProvenance,
    outcome_code: OutcomeCode,
    subject_order: Order,
    venue_order_id: VenueOrderId | None = None,
) -> ExecutionFact:
    if type(subject_order) is not Order:
        raise _fail(OutcomeCode.INVALID_TYPE, "subject_order must be an exact Order")
    payload = SubmissionQueryFactPayload(outcome_code)
    (
        source,
        identity,
        time,
        source_provenance,
        instrument,
        client_key,
        venue_id,
        known_order,
        correlation,
        cause,
    ) = _require_fact_common(
        source_namespace=source_namespace,
        dedup_identity=dedup_identity,
        occurred_at=occurred_at,
        provenance=provenance,
        instrument=subject_order.instrument,
        client_submission_key=subject_order.client_submission_key,
        venue_order_id=venue_order_id,
        order_id=subject_order.order_id,
        correlation_id=subject_order.correlation_id,
        causation_id=subject_order.order_id,
        require_order_cause=True,
    )
    return _new_fact(
        source_namespace=source,
        dedup_identity=identity,
        kind=ExecutionFactKind.SUBMISSION_QUERY,
        occurred_at=time,
        instrument=instrument,
        client_submission_key=client_key,
        venue_order_id=venue_id,
        order_id=known_order,
        correlation_id=correlation,
        causation_id=cause,
        payload=payload,
        provenance=source_provenance,
    )


def _fee_document(fee: FeeEntry) -> dict[str, object]:
    return {
        "amount": fee.amount.text,
        "currency": fee.currency.code,
        "fee_code": fee.fee_code.value,
    }


def _fact_payload_document(payload: ExecutionFactPayload) -> dict[str, object]:
    if type(payload) is LifecycleFactPayload:
        return {
            "outcome_code": payload.outcome_code.value,
            "payload_type": "lifecycle",
        }
    if type(payload) is TradeFactPayload:
        return {
            "fees": [_fee_document(fee) for fee in payload.fees],
            "instrument_spec_set_id": payload.instrument_spec_set_id.value,
            "instrument_spec_set_sha256": payload.instrument_spec_set_sha256.value,
            "instrument_specification_id": payload.instrument_specification_id.value,
            "payload_type": "trade",
            "price": payload.price.text,
            "quantity": payload.quantity.text,
            "side": payload.side.value,
        }
    if type(payload) is SubmissionQueryFactPayload:
        return {
            "outcome_code": payload.outcome_code.value,
            "payload_type": "submission_query",
        }
    raise _fail(OutcomeCode.INVALID_TYPE, "fact payload has an unsupported concrete type")


def _fact_document(
    fact: ExecutionFact,
    *,
    include_digest: bool,
) -> dict[str, object]:
    if type(fact) is not ExecutionFact:
        raise _fail(OutcomeCode.INVALID_TYPE, "fact must be an exact ExecutionFact")
    document: dict[str, object] = {
        "canonicalization": EXECUTION_FACT_CANONICALIZATION,
        "causation_id": _optional_id_document(fact.causation_id),
        "client_submission_key": (
            None if fact.client_submission_key is None else fact.client_submission_key.value
        ),
        "correlation_id": _optional_id_document(fact.correlation_id),
        "dedup_identity": _dedup_document(fact.dedup_identity),
        "instrument": (None if fact.instrument is None else _instrument_document(fact.instrument)),
        "kind": fact.kind.value,
        "message_type": "execution_fact",
        "occurred_at": _utc_text(fact.occurred_at),
        "order_id": _optional_id_document(fact.order_id),
        "payload": _fact_payload_document(fact.payload),
        "provenance": _provenance_document(fact.provenance),
        "schema_version": MESSAGE_SCHEMA_VERSION,
        "source_namespace": fact.source_namespace.value,
        "venue_order_id": (None if fact.venue_order_id is None else fact.venue_order_id.value),
    }
    if include_digest:
        document["fact_sha256"] = execution_fact_digest(fact).value
    return document


def canonical_execution_fact_preimage_bytes(fact: ExecutionFact) -> bytes:
    return _encode_json(_fact_document(fact, include_digest=False))


def execution_fact_digest(fact: ExecutionFact) -> Sha256Digest:
    return _digest(
        EXECUTION_FACT_DIGEST_DOMAIN,
        canonical_execution_fact_preimage_bytes(fact),
    )


def canonical_execution_fact_bytes(fact: ExecutionFact) -> bytes:
    return _encode_json(_fact_document(fact, include_digest=True))


@final
@dataclass(frozen=True, slots=True, init=False)
class ExecutionFactIngress:
    available_at: datetime
    source_namespace: SourceNamespace
    ingress_sequence: int
    fact: ExecutionFact

    def __init__(self) -> None:
        raise TypeError(
            "ExecutionFactIngress values are created only by create_execution_fact_ingress"
        )

    @property
    def identity(self) -> IngressIdentity:
        return IngressIdentity(self.source_namespace, self.ingress_sequence)


def create_execution_fact_ingress(
    *,
    available_at: datetime,
    source_namespace: SourceNamespace,
    ingress_sequence: int,
    fact: ExecutionFact,
) -> ExecutionFactIngress:
    if type(source_namespace) is not SourceNamespace:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "source_namespace must be an exact SourceNamespace",
        )
    if type(fact) is not ExecutionFact:
        raise _fail(OutcomeCode.INVALID_TYPE, "fact must be an exact ExecutionFact")
    sequence = _require_non_negative(ingress_sequence, field_name="ingress_sequence")
    available = _require_time(available_at, field_name="available_at")
    if source_namespace != fact.source_namespace:
        raise _fail(
            OutcomeCode.FACT_INVALID,
            "ingress and fact source namespaces conflict",
        )
    if available < fact.occurred_at:
        raise _fail(
            OutcomeCode.OUT_OF_RANGE,
            "available_at cannot precede occurred_at",
        )
    value = object.__new__(ExecutionFactIngress)
    object.__setattr__(value, "available_at", available)
    object.__setattr__(value, "source_namespace", source_namespace)
    object.__setattr__(value, "ingress_sequence", sequence)
    object.__setattr__(value, "fact", fact)
    return value


def _ingress_document(ingress: ExecutionFactIngress) -> dict[str, object]:
    if type(ingress) is not ExecutionFactIngress:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "ingress must be an exact ExecutionFactIngress",
        )
    return {
        "available_at": _utc_text(ingress.available_at),
        "canonicalization": EXECUTION_FACT_INGRESS_CANONICALIZATION,
        "fact": _fact_document(ingress.fact, include_digest=True),
        "ingress_sequence": ingress.ingress_sequence,
        "message_type": "execution_fact_ingress",
        "schema_version": MESSAGE_SCHEMA_VERSION,
        "source_namespace": ingress.source_namespace.value,
    }


def canonical_execution_fact_ingress_bytes(ingress: ExecutionFactIngress) -> bytes:
    return _encode_json(_ingress_document(ingress))


def execution_fact_ingress_digest(ingress: ExecutionFactIngress) -> Sha256Digest:
    return _digest(
        EXECUTION_FACT_INGRESS_DIGEST_DOMAIN,
        canonical_execution_fact_ingress_bytes(ingress),
    )


@final
@dataclass(frozen=True, slots=True, init=False)
class Fill:
    run_id: RunId
    fill_id: EconomicId
    source_namespace: SourceNamespace
    dedup_identity: FactDedupIdentity
    fact_sha256: Sha256Digest
    provenance: FactProvenance
    occurred_at: datetime
    instrument: Instrument
    side: OrderSide
    quantity: CanonicalDecimal
    price: CanonicalDecimal
    fees: tuple[FeeEntry, ...]
    instrument_specification_id: InstrumentSpecId
    instrument_spec_set_id: InstrumentSpecSetId
    instrument_spec_set_sha256: Sha256Digest
    client_submission_key: Sha256Digest | None
    venue_order_id: VenueOrderId | None
    order_id: EconomicId | None
    correlation_id: EconomicId | None
    causation_id: EconomicId | None

    def __init__(self) -> None:
        raise TypeError("Fill values are created only by create_fill")

    @property
    def fact_key(self) -> FactDedupKey:
        return FactDedupKey(self.source_namespace, self.dedup_identity)


def create_fill(
    *,
    fill_id: EconomicId,
    fact: ExecutionFact,
    spec_set: InstrumentExecutionSpecSet,
) -> Fill:
    if type(fact) is not ExecutionFact:
        raise _fail(OutcomeCode.INVALID_TYPE, "fact must be an exact ExecutionFact")
    _require_id_owner(fill_id, EconomicOwnerKind.EXECUTION_FILL, field_name="fill_id")
    if (
        fact.kind is not ExecutionFactKind.TRADE
        or type(fact.payload) is not TradeFactPayload
        or fact.instrument is None
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "Fill requires one complete trade fact")
    payload = fact.payload
    specification, _ = _require_spec_set(
        spec_set,
        instrument=fact.instrument,
        specification_id=payload.instrument_specification_id,
        set_id=payload.instrument_spec_set_id,
        set_sha256=payload.instrument_spec_set_sha256,
    )
    _require_quantity(payload.quantity, specification)
    _require_price(payload.price, specification)
    _require_phase1_fees(payload.fees, specification)
    _require_same_run(
        fill_id.run_id,
        fact.order_id,
        fact.correlation_id,
        fact.causation_id,
    )
    value = object.__new__(Fill)
    object.__setattr__(value, "run_id", fill_id.run_id)
    object.__setattr__(value, "fill_id", fill_id)
    object.__setattr__(value, "source_namespace", fact.source_namespace)
    object.__setattr__(value, "dedup_identity", fact.dedup_identity)
    object.__setattr__(value, "fact_sha256", fact.fact_sha256)
    object.__setattr__(value, "provenance", fact.provenance)
    object.__setattr__(value, "occurred_at", fact.occurred_at)
    object.__setattr__(value, "instrument", fact.instrument)
    object.__setattr__(value, "side", payload.side)
    object.__setattr__(value, "quantity", payload.quantity)
    object.__setattr__(value, "price", payload.price)
    object.__setattr__(value, "fees", payload.fees)
    object.__setattr__(
        value,
        "instrument_specification_id",
        payload.instrument_specification_id,
    )
    object.__setattr__(value, "instrument_spec_set_id", payload.instrument_spec_set_id)
    object.__setattr__(
        value,
        "instrument_spec_set_sha256",
        payload.instrument_spec_set_sha256,
    )
    object.__setattr__(value, "client_submission_key", fact.client_submission_key)
    object.__setattr__(value, "venue_order_id", fact.venue_order_id)
    object.__setattr__(value, "order_id", fact.order_id)
    object.__setattr__(value, "correlation_id", fact.correlation_id)
    object.__setattr__(value, "causation_id", fact.causation_id)
    return value


def _require_phase1_fees(
    fees: object,
    specification: InstrumentExecutionSpec,
) -> tuple[FeeEntry, ...]:
    if type(fees) is not tuple or len(fees) != 1 or type(fees[0]) is not FeeEntry:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "Phase 1 trade requires one exact FeeEntry tuple",
        )
    fee = fees[0]
    if (
        fee.fee_code is not FeeCode.COMMISSION
        or fee.currency != specification.settlement_currency
        or fee.amount.text != "0"
    ):
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "Phase 1 fee does not bind the instrument settlement specification",
        )
    require_quantized(fee.amount, specification.currency_quantum, field_name="fee")
    return fees


def _fill_document(fill: Fill) -> dict[str, object]:
    if type(fill) is not Fill:
        raise _fail(OutcomeCode.INVALID_TYPE, "fill must be an exact Fill")
    return {
        "canonicalization": FILL_CANONICALIZATION,
        "causation_id": _optional_id_document(fill.causation_id),
        "client_submission_key": (
            None if fill.client_submission_key is None else fill.client_submission_key.value
        ),
        "correlation_id": _optional_id_document(fill.correlation_id),
        "dedup_identity": _dedup_document(fill.dedup_identity),
        "fact_sha256": fill.fact_sha256.value,
        "fees": [_fee_document(fee) for fee in fill.fees],
        "fill_id": _economic_id_document(fill.fill_id),
        "instrument": _instrument_document(fill.instrument),
        "instrument_spec_set_id": fill.instrument_spec_set_id.value,
        "instrument_spec_set_sha256": fill.instrument_spec_set_sha256.value,
        "instrument_specification_id": fill.instrument_specification_id.value,
        "message_type": "fill",
        "occurred_at": _utc_text(fill.occurred_at),
        "order_id": _optional_id_document(fill.order_id),
        "price": fill.price.text,
        "provenance": _provenance_document(fill.provenance),
        "quantity": fill.quantity.text,
        "run_id": fill.run_id.value,
        "schema_version": MESSAGE_SCHEMA_VERSION,
        "side": fill.side.value,
        "source_namespace": fill.source_namespace.value,
        "venue_order_id": (None if fill.venue_order_id is None else fill.venue_order_id.value),
    }


def canonical_fill_bytes(fill: Fill) -> bytes:
    return _encode_json(_fill_document(fill))


def fill_digest(fill: Fill) -> Sha256Digest:
    return _digest(FILL_DIGEST_DOMAIN, canonical_fill_bytes(fill))


def _parse_decimal_integer(text: str) -> int:
    if type(text) is not str or not text:
        raise ValueError("invalid JSON integer")
    negative = text.startswith("-")
    digits = text[1:] if negative else text
    if not digits or (len(digits) > 1 and digits.startswith("0")) or not digits.isascii():
        raise ValueError("invalid JSON integer")
    value = 0
    for index in range(0, len(digits), 9):
        chunk = digits[index : index + 9]
        if not chunk.isdigit():
            raise ValueError("invalid JSON integer")
        value = value * (10 ** len(chunk)) + int(chunk)
    return -value if negative else value


def _reject_json_number(_: str) -> NoReturn:
    raise ValueError("floating-point JSON values are forbidden")


def _reject_json_constant(_: str) -> NoReturn:
    raise ValueError("non-finite JSON values are forbidden")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("duplicate or non-string JSON object key")
        result[key] = value
    return result


def _decode_json(payload: bytes) -> dict[str, object]:
    if type(payload) is not bytes:
        raise _fail(OutcomeCode.INVALID_TYPE, "message payload must be exact bytes")
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            parse_int=_parse_decimal_integer,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise _fail(
            OutcomeCode.OUT_OF_RANGE,
            "message payload is not closed canonical JSON",
        ) from error
    if type(decoded) is not dict:
        raise _fail(OutcomeCode.INVALID_TYPE, "message document must be a JSON object")
    return decoded


def _require_envelope(
    document: dict[str, object],
    expected: frozenset[str],
    *,
    message_type: str,
    canonicalization: str,
) -> None:
    unknown = set(document).difference(expected)
    if unknown:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"unknown message fields: {sorted(unknown)!r}")
    if (
        not {"message_type", "canonicalization", "schema_version"}.issubset(document)
        or type(document.get("message_type")) is not str
        or type(document.get("canonicalization")) is not str
        or type(document.get("schema_version")) is not int
        or document.get("message_type") != message_type
        or document.get("canonicalization") != canonicalization
        or document.get("schema_version") != MESSAGE_SCHEMA_VERSION
    ):
        raise _fail(OutcomeCode.OUT_OF_RANGE, "message envelope is not the declared version")


def _require_keys(
    document: dict[str, object],
    expected: frozenset[str],
    *,
    message_type: str,
    canonicalization: str,
) -> None:
    _require_envelope(
        document,
        expected,
        message_type=message_type,
        canonicalization=canonicalization,
    )
    missing = expected.difference(document)
    if missing:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"missing message fields: {sorted(missing)!r}")


def _require_round_trip(
    payload: bytes,
    document: dict[str, object],
    expected: dict[str, object],
    canonical: bytes,
    *,
    fact: bool = False,
) -> None:
    if document != expected:
        code = OutcomeCode.FACT_INVALID if fact else OutcomeCode.CONFLICTING_ID
        raise _fail(code, "message fields conflict with their canonical context")
    if payload != canonical:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "message bytes are not canonical")


def _parse_run_id(value: object) -> RunId:
    if type(value) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, "run_id must be exact str on wire")
    try:
        return RunId(value)
    except RunContractError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error


def _parse_digest(value: object, *, field_name: str) -> Sha256Digest:
    if type(value) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must be exact str on wire")
    try:
        return Sha256Digest(value)
    except RunContractError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error


def _parse_economic_id(value: object, *, field_name: str) -> EconomicId:
    if type(value) is not dict:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must be an object")
    if set(value) != {"owner_kind", "owner_sequence", "run_id"}:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} has invalid keys")
    owner_raw = value["owner_kind"]
    if type(owner_raw) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name}.owner_kind must be str")
    try:
        owner = EconomicOwnerKind(owner_raw)
    except ValueError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name}.owner_kind is unknown") from error
    sequence = value["owner_sequence"]
    if type(sequence) is not int:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name}.owner_sequence must be int")
    try:
        return EconomicId(_parse_run_id(value["run_id"]), owner, sequence)
    except ExecutionIdentityError as error:
        raise _fail(error.code, str(error)) from error


def _parse_instrument(value: object) -> Instrument:
    if type(value) is not dict or set(value) != {"symbol", "venue"}:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "instrument must have exact venue/symbol keys")
    venue_raw = value["venue"]
    symbol = value["symbol"]
    if type(venue_raw) is not str or type(symbol) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, "instrument venue/symbol must be exact str")
    try:
        return Instrument(VenueId(venue_raw), symbol)
    except IdentityValidationError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error


def _parse_decimal(value: object, *, field_name: str) -> CanonicalDecimal:
    if type(value) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must be exact str on wire")
    return CanonicalDecimal(value)


def _parse_time(value: object, *, field_name: str) -> datetime:
    if type(value) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field_name} must be exact str on wire")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} is not canonical UTC") from error
    if _utc_text(parsed) != value:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} is not canonical UTC")
    return parsed


def _parse_dedup_identity(value: object) -> FactDedupIdentity:
    if value is None:
        raise _fail(
            OutcomeCode.FACT_INVALID_MISSING_DEDUP_IDENTITY,
            "fact dedup identity is missing",
        )
    if type(value) is not dict or set(value) != {"kind", "value"}:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "dedup identity has invalid keys")
    kind = value["kind"]
    identity_value = value["value"]
    try:
        if kind == "external_id":
            if type(identity_value) is not str:
                raise _fail(
                    OutcomeCode.INVALID_TYPE,
                    "external dedup value must be exact str",
                )
            return ExternalFactId(identity_value)
        if kind == "source_native_sequence":
            if type(identity_value) is not int:
                raise _fail(
                    OutcomeCode.INVALID_TYPE,
                    "native dedup value must be exact int",
                )
            return SourceNativeSequence(identity_value)
    except ExecutionIdentityError as error:
        raise _fail(error.code, str(error)) from error
    raise _fail(OutcomeCode.OUT_OF_RANGE, "dedup identity tag is unknown")


def _parse_source_namespace(value: object) -> SourceNamespace:
    if type(value) is not str:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "source_namespace must be exact str on wire",
        )
    try:
        return SourceNamespace(value)
    except ExecutionIdentityError as error:
        raise _fail(error.code, str(error)) from error


def _parse_optional_economic_id(
    value: object,
    *,
    field_name: str,
) -> EconomicId | None:
    return None if value is None else _parse_economic_id(value, field_name=field_name)


def _parse_optional_digest(value: object, *, field_name: str) -> Sha256Digest | None:
    return None if value is None else _parse_digest(value, field_name=field_name)


def _parse_optional_venue_order_id(value: object) -> VenueOrderId | None:
    if value is None:
        return None
    if type(value) is not str:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "venue_order_id must be exact str or null on wire",
        )
    return VenueOrderId(value)


def _parse_target_lineage(value: object) -> TargetLineageRef:
    if type(value) is not dict or set(value) != {"target_id", "target_sha256"}:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "target_lineage has invalid keys")
    return TargetLineageRef(
        _parse_economic_id(value["target_id"], field_name="target_id"),
        _parse_digest(value["target_sha256"], field_name="target_sha256"),
    )


def _parse_policy(value: object) -> ExecutionPolicyRef:
    if type(value) is not dict or set(value) != {
        "execution_policy_id",
        "execution_policy_sha256",
    }:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "execution_policy has invalid keys")
    identifier = value["execution_policy_id"]
    if type(identifier) is not str:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "execution_policy_id must be exact str on wire",
        )
    return ExecutionPolicyRef(
        ExecutionPolicyId(identifier),
        _parse_digest(
            value["execution_policy_sha256"],
            field_name="execution_policy_sha256",
        ),
    )


def _parse_provenance(value: object) -> FactProvenance:
    if type(value) is not dict or set(value) != {
        "provenance_id",
        "source_payload_sha256",
    }:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "provenance has invalid keys")
    identifier = value["provenance_id"]
    if type(identifier) is not str:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "provenance_id must be exact str on wire",
        )
    return FactProvenance(
        FactProvenanceId(identifier),
        _parse_digest(
            value["source_payload_sha256"],
            field_name="source_payload_sha256",
        ),
    )


def _parse_enum[T: StrEnum](
    enum_type: type[T],
    value: object,
    *,
    field_name: str,
) -> T:
    if type(value) is not str:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            f"{field_name} must be exact str on wire",
        )
    try:
        return enum_type(value)
    except ValueError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field_name} is unknown") from error


_ORDER_INTENT_KEYS = frozenset(
    {
        "canonicalization",
        "message_type",
        "schema_version",
        "run_id",
        "intent_id",
        "correlation_id",
        "causation_id",
        "target_lineage",
        "instrument",
        "side",
        "quantity",
        "order_kind",
        "time_in_force",
        "price_constraint",
        "portfolio_snapshot_version",
        "causal_root_available_at",
        "dispatch_sequence",
        "instrument_specification_id",
        "instrument_spec_set_id",
        "instrument_spec_set_sha256",
        "execution_policy",
    }
)
_RISK_DECISION_KEYS = frozenset(
    {
        "canonicalization",
        "message_type",
        "schema_version",
        "run_id",
        "decision_id",
        "intent_id",
        "correlation_id",
        "causation_id",
        "intent_sha256",
        "kind",
        "outcome_code",
        "portfolio_snapshot_version",
        "risk_state_version",
        "causal_root_available_at",
        "dispatch_sequence",
        "approved_quantity",
        "effective_intent_sha256",
        "approval",
    }
)
_APPROVAL_KEYS = frozenset(
    {
        "canonicalization",
        "message_type",
        "schema_version",
        "run_id",
        "approval_id",
        "decision_id",
        "intent_id",
        "correlation_id",
        "causation_id",
        "original_intent_sha256",
        "effective_intent_sha256",
        "approved_quantity",
        "portfolio_snapshot_version",
        "risk_state_version",
        "causal_root_available_at",
        "dispatch_sequence",
    }
)
_ORDER_KEYS = frozenset(
    {
        "canonicalization",
        "message_type",
        "schema_version",
        "run_id",
        "order_id",
        "intent_id",
        "correlation_id",
        "causation_id",
        "decision_id",
        "approval_id",
        "original_intent_sha256",
        "effective_intent_sha256",
        "risk_decision_sha256",
        "approval_sha256",
        "portfolio_snapshot_version",
        "risk_state_version",
        "dispatch_sequence",
        "instrument",
        "side",
        "quantity",
        "order_kind",
        "time_in_force",
        "price_constraint",
        "instrument_specification_id",
        "instrument_spec_set_id",
        "instrument_spec_set_sha256",
        "execution_policy",
        "eligible_after_available_at",
    }
)
_FACT_KEYS = frozenset(
    {
        "canonicalization",
        "message_type",
        "schema_version",
        "source_namespace",
        "dedup_identity",
        "kind",
        "occurred_at",
        "instrument",
        "client_submission_key",
        "venue_order_id",
        "order_id",
        "correlation_id",
        "causation_id",
        "payload",
        "provenance",
        "fact_sha256",
    }
)
_INGRESS_KEYS = frozenset(
    {
        "canonicalization",
        "message_type",
        "schema_version",
        "available_at",
        "source_namespace",
        "ingress_sequence",
        "fact",
    }
)
_FILL_KEYS = frozenset(
    {
        "canonicalization",
        "message_type",
        "schema_version",
        "run_id",
        "fill_id",
        "source_namespace",
        "dedup_identity",
        "fact_sha256",
        "provenance",
        "occurred_at",
        "instrument",
        "side",
        "quantity",
        "price",
        "fees",
        "instrument_specification_id",
        "instrument_spec_set_id",
        "instrument_spec_set_sha256",
        "client_submission_key",
        "venue_order_id",
        "order_id",
        "correlation_id",
        "causation_id",
    }
)

_ECONOMIC_ID_WIRE_KEYS = frozenset({"owner_kind", "owner_sequence", "run_id"})
_INSTRUMENT_WIRE_KEYS = frozenset({"symbol", "venue"})
_TARGET_LINEAGE_WIRE_KEYS = frozenset({"target_id", "target_sha256"})
_POLICY_WIRE_KEYS = frozenset({"execution_policy_id", "execution_policy_sha256"})
_PROVENANCE_WIRE_KEYS = frozenset({"provenance_id", "source_payload_sha256"})
_DEDUP_WIRE_KEYS = frozenset({"kind", "value"})
_FEE_WIRE_KEYS = frozenset({"amount", "currency", "fee_code"})
_LIFECYCLE_PAYLOAD_WIRE_KEYS = frozenset({"outcome_code", "payload_type"})
_TRADE_PAYLOAD_WIRE_KEYS = frozenset(
    {
        "fees",
        "instrument_spec_set_id",
        "instrument_spec_set_sha256",
        "instrument_specification_id",
        "payload_type",
        "price",
        "quantity",
        "side",
    }
)
_SUBMISSION_QUERY_PAYLOAD_WIRE_KEYS = frozenset({"outcome_code", "payload_type"})


def _require_wire_type(
    value: object,
    expected: type[object],
    *,
    field_name: str,
) -> None:
    if type(value) is not expected:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            f"{field_name} must have exact runtime type {expected.__name__} on wire",
        )


def _require_optional_wire_type(
    value: object,
    expected: type[object],
    *,
    field_name: str,
) -> None:
    if value is not None:
        _require_wire_type(value, expected, field_name=field_name)


def _require_wire_object(
    value: object,
    expected_keys: frozenset[str],
    *,
    field_name: str,
) -> dict[str, object]:
    _require_wire_type(value, dict, field_name=field_name)
    document = cast(dict[str, object], value)
    if set(document) != expected_keys:
        raise _fail(
            OutcomeCode.OUT_OF_RANGE,
            f"{field_name} has invalid keys",
        )
    return document


def _preflight_economic_id_types(value: object, *, field_name: str) -> None:
    document = _require_wire_object(
        value,
        _ECONOMIC_ID_WIRE_KEYS,
        field_name=field_name,
    )
    _require_wire_type(
        document["owner_kind"],
        str,
        field_name=f"{field_name}.owner_kind",
    )
    _require_wire_type(
        document["owner_sequence"],
        int,
        field_name=f"{field_name}.owner_sequence",
    )
    _require_wire_type(
        document["run_id"],
        str,
        field_name=f"{field_name}.run_id",
    )


def _preflight_instrument_types(value: object, *, field_name: str) -> None:
    document = _require_wire_object(
        value,
        _INSTRUMENT_WIRE_KEYS,
        field_name=field_name,
    )
    _require_wire_type(document["symbol"], str, field_name=f"{field_name}.symbol")
    _require_wire_type(document["venue"], str, field_name=f"{field_name}.venue")


def _preflight_target_lineage_types(value: object) -> None:
    document = _require_wire_object(
        value,
        _TARGET_LINEAGE_WIRE_KEYS,
        field_name="target_lineage",
    )
    _preflight_economic_id_types(document["target_id"], field_name="target_id")
    _require_wire_type(
        document["target_sha256"],
        str,
        field_name="target_sha256",
    )


def _preflight_policy_types(value: object) -> None:
    document = _require_wire_object(
        value,
        _POLICY_WIRE_KEYS,
        field_name="execution_policy",
    )
    _require_wire_type(
        document["execution_policy_id"],
        str,
        field_name="execution_policy_id",
    )
    _require_wire_type(
        document["execution_policy_sha256"],
        str,
        field_name="execution_policy_sha256",
    )


def _preflight_provenance_types(value: object) -> None:
    document = _require_wire_object(
        value,
        _PROVENANCE_WIRE_KEYS,
        field_name="provenance",
    )
    _require_wire_type(
        document["provenance_id"],
        str,
        field_name="provenance_id",
    )
    _require_wire_type(
        document["source_payload_sha256"],
        str,
        field_name="source_payload_sha256",
    )


def _preflight_dedup_types(value: object) -> None:
    document = _require_wire_object(
        value,
        _DEDUP_WIRE_KEYS,
        field_name="dedup_identity",
    )
    _require_wire_type(document["kind"], str, field_name="dedup_identity.kind")
    kind = document["kind"]
    if kind == "external_id":
        _require_wire_type(
            document["value"],
            str,
            field_name="dedup_identity.value",
        )
    elif kind == "source_native_sequence":
        _require_wire_type(
            document["value"],
            int,
            field_name="dedup_identity.value",
        )
    elif type(document["value"]) not in (str, int):
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "dedup_identity.value must be exact str or int on wire",
        )


def _preflight_fee_types(value: object, *, index: int) -> None:
    document = _require_wire_object(
        value,
        _FEE_WIRE_KEYS,
        field_name=f"fees[{index}]",
    )
    for field in ("amount", "currency", "fee_code"):
        _require_wire_type(
            document[field],
            str,
            field_name=f"fees[{index}].{field}",
        )


def _preflight_fees_types(value: object) -> None:
    _require_wire_type(value, list, field_name="fees")
    for index, fee in enumerate(cast(list[object], value)):
        _preflight_fee_types(fee, index=index)


def _preflight_approval_types(document: dict[str, object]) -> None:
    _require_keys(
        document,
        _APPROVAL_KEYS,
        message_type="execution_approval",
        canonicalization=EXECUTION_APPROVAL_CANONICALIZATION,
    )
    for field in (
        "run_id",
        "original_intent_sha256",
        "effective_intent_sha256",
        "approved_quantity",
        "causal_root_available_at",
    ):
        _require_wire_type(document[field], str, field_name=field)
    for field in (
        "portfolio_snapshot_version",
        "risk_state_version",
        "dispatch_sequence",
    ):
        _require_wire_type(document[field], int, field_name=field)
    for field in (
        "approval_id",
        "decision_id",
        "intent_id",
        "correlation_id",
        "causation_id",
    ):
        _preflight_economic_id_types(document[field], field_name=field)


def _preflight_order_intent_types(document: dict[str, object]) -> None:
    for field in (
        "run_id",
        "side",
        "quantity",
        "order_kind",
        "time_in_force",
        "causal_root_available_at",
        "instrument_specification_id",
        "instrument_spec_set_id",
        "instrument_spec_set_sha256",
    ):
        _require_wire_type(document[field], str, field_name=field)
    for field in ("portfolio_snapshot_version", "dispatch_sequence"):
        _require_wire_type(document[field], int, field_name=field)
    for field in ("intent_id", "correlation_id", "causation_id"):
        _preflight_economic_id_types(document[field], field_name=field)
    _preflight_target_lineage_types(document["target_lineage"])
    _preflight_instrument_types(document["instrument"], field_name="instrument")
    _preflight_policy_types(document["execution_policy"])


def _preflight_risk_decision_types(document: dict[str, object]) -> None:
    for field in (
        "run_id",
        "intent_sha256",
        "kind",
        "outcome_code",
        "causal_root_available_at",
    ):
        _require_wire_type(document[field], str, field_name=field)
    for field in (
        "portfolio_snapshot_version",
        "risk_state_version",
        "dispatch_sequence",
    ):
        _require_wire_type(document[field], int, field_name=field)
    for field in ("decision_id", "intent_id", "correlation_id", "causation_id"):
        _preflight_economic_id_types(document[field], field_name=field)
    _require_optional_wire_type(
        document["approved_quantity"],
        str,
        field_name="approved_quantity",
    )
    _require_optional_wire_type(
        document["effective_intent_sha256"],
        str,
        field_name="effective_intent_sha256",
    )
    approval = document["approval"]
    _require_optional_wire_type(approval, dict, field_name="approval")
    if approval is not None:
        _preflight_approval_types(cast(dict[str, object], approval))


def _preflight_order_types(document: dict[str, object]) -> None:
    for field in (
        "run_id",
        "original_intent_sha256",
        "effective_intent_sha256",
        "risk_decision_sha256",
        "approval_sha256",
        "side",
        "quantity",
        "order_kind",
        "time_in_force",
        "instrument_specification_id",
        "instrument_spec_set_id",
        "instrument_spec_set_sha256",
        "eligible_after_available_at",
    ):
        _require_wire_type(document[field], str, field_name=field)
    for field in (
        "portfolio_snapshot_version",
        "risk_state_version",
        "dispatch_sequence",
    ):
        _require_wire_type(document[field], int, field_name=field)
    for field in (
        "order_id",
        "intent_id",
        "correlation_id",
        "causation_id",
        "decision_id",
        "approval_id",
    ):
        _preflight_economic_id_types(document[field], field_name=field)
    _preflight_instrument_types(document["instrument"], field_name="instrument")
    _preflight_policy_types(document["execution_policy"])


def _preflight_fact_payload_types(value: object) -> None:
    _require_wire_type(value, dict, field_name="payload")
    payload = cast(dict[str, object], value)
    if "payload_type" not in payload:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "payload_type is missing")
    _require_wire_type(payload["payload_type"], str, field_name="payload_type")
    payload_keys = frozenset(payload)
    if payload_keys == _TRADE_PAYLOAD_WIRE_KEYS:
        for field in (
            "instrument_spec_set_id",
            "instrument_spec_set_sha256",
            "instrument_specification_id",
            "payload_type",
            "price",
            "quantity",
            "side",
        ):
            _require_wire_type(payload[field], str, field_name=field)
        _preflight_fees_types(payload["fees"])
    elif payload_keys in (
        _LIFECYCLE_PAYLOAD_WIRE_KEYS,
        _SUBMISSION_QUERY_PAYLOAD_WIRE_KEYS,
    ):
        for field in ("outcome_code", "payload_type"):
            _require_wire_type(payload[field], str, field_name=field)
    else:
        raise _fail(
            OutcomeCode.OUT_OF_RANGE,
            "fact payload has no declared key shape",
        )


def _preflight_fact_types(document: dict[str, object]) -> None:
    for field in (
        "source_namespace",
        "kind",
        "occurred_at",
        "fact_sha256",
    ):
        _require_wire_type(document[field], str, field_name=field)
    _preflight_dedup_types(document["dedup_identity"])
    _require_optional_wire_type(
        document["client_submission_key"],
        str,
        field_name="client_submission_key",
    )
    _require_optional_wire_type(
        document["venue_order_id"],
        str,
        field_name="venue_order_id",
    )
    instrument = document["instrument"]
    _require_optional_wire_type(instrument, dict, field_name="instrument")
    if instrument is not None:
        _preflight_instrument_types(instrument, field_name="instrument")
    for field in ("order_id", "correlation_id", "causation_id"):
        identity = document[field]
        _require_optional_wire_type(identity, dict, field_name=field)
        if identity is not None:
            _preflight_economic_id_types(identity, field_name=field)
    _preflight_provenance_types(document["provenance"])
    _preflight_fact_payload_types(document["payload"])


def _preflight_ingress_types(
    document: dict[str, object],
    fact_document: dict[str, object],
) -> None:
    _require_wire_type(document["available_at"], str, field_name="available_at")
    _require_wire_type(
        document["source_namespace"],
        str,
        field_name="source_namespace",
    )
    _require_wire_type(
        document["ingress_sequence"],
        int,
        field_name="ingress_sequence",
    )
    _preflight_fact_types(fact_document)


def _preflight_fill_types(document: dict[str, object]) -> None:
    for field in (
        "run_id",
        "source_namespace",
        "fact_sha256",
        "occurred_at",
        "side",
        "quantity",
        "price",
        "instrument_specification_id",
        "instrument_spec_set_id",
        "instrument_spec_set_sha256",
    ):
        _require_wire_type(document[field], str, field_name=field)
    _preflight_economic_id_types(document["fill_id"], field_name="fill_id")
    _preflight_dedup_types(document["dedup_identity"])
    _preflight_provenance_types(document["provenance"])
    _preflight_instrument_types(document["instrument"], field_name="instrument")
    _preflight_fees_types(document["fees"])
    _require_optional_wire_type(
        document["client_submission_key"],
        str,
        field_name="client_submission_key",
    )
    _require_optional_wire_type(
        document["venue_order_id"],
        str,
        field_name="venue_order_id",
    )
    for field in ("order_id", "correlation_id", "causation_id"):
        identity = document[field]
        _require_optional_wire_type(identity, dict, field_name=field)
        if identity is not None:
            _preflight_economic_id_types(identity, field_name=field)


def decode_order_intent(
    payload: bytes,
    *,
    spec_set: InstrumentExecutionSpecSet,
    target_lineage: TargetLineageRef,
    execution_policy: ExecutionPolicyRef,
) -> OrderIntent:
    """Decode and fully re-prove one canonical OrderIntent."""
    if (
        type(spec_set) is not InstrumentExecutionSpecSet
        or type(target_lineage) is not TargetLineageRef
        or type(execution_policy) is not ExecutionPolicyRef
    ):
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "OrderIntent decode context must have exact canonical types",
        )
    document = _decode_json(payload)
    _require_keys(
        document,
        _ORDER_INTENT_KEYS,
        message_type="order_intent",
        canonicalization=ORDER_INTENT_CANONICALIZATION,
    )
    _preflight_order_intent_types(document)
    run_id = _parse_run_id(document["run_id"])
    intent_id = _parse_economic_id(document["intent_id"], field_name="intent_id")
    correlation_id = _parse_economic_id(
        document["correlation_id"],
        field_name="correlation_id",
    )
    _parse_economic_id(document["causation_id"], field_name="causation_id")
    parsed_target = _parse_target_lineage(document["target_lineage"])
    parsed_policy = _parse_policy(document["execution_policy"])
    if parsed_target != target_lineage or parsed_policy != execution_policy:
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "opaque target or policy lineage conflicts with decode context",
        )
    instrument = _parse_instrument(document["instrument"])
    side = _parse_enum(OrderSide, document["side"], field_name="side")
    quantity = _parse_decimal(document["quantity"], field_name="quantity")
    if (
        _parse_enum(OrderKind, document["order_kind"], field_name="order_kind")
        is not OrderKind.MARKET
        or _parse_enum(
            TimeInForce,
            document["time_in_force"],
            field_name="time_in_force",
        )
        is not TimeInForce.GOOD_FOR_NEXT_ELIGIBLE_MARKET_EVENT
        or document["price_constraint"] is not None
    ):
        raise _fail(OutcomeCode.OUT_OF_RANGE, "OrderIntent is not the Phase 1 profile")
    snapshot = _require_non_negative(
        document["portfolio_snapshot_version"],
        field_name="portfolio_snapshot_version",
    )
    causal_time = _parse_time(
        document["causal_root_available_at"],
        field_name="causal_root_available_at",
    )
    dispatch = _require_non_negative(
        document["dispatch_sequence"],
        field_name="dispatch_sequence",
    )
    specification_id_raw = document["instrument_specification_id"]
    set_id_raw = document["instrument_spec_set_id"]
    if type(specification_id_raw) is not str or type(set_id_raw) is not str:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "instrument specification IDs must be exact str on wire",
        )
    InstrumentSpecId(specification_id_raw)
    InstrumentSpecSetId(set_id_raw)
    _parse_digest(
        document["instrument_spec_set_sha256"],
        field_name="instrument_spec_set_sha256",
    )
    intent = create_order_intent(
        run_id=run_id,
        intent_id=intent_id,
        correlation_id=correlation_id,
        target_lineage=target_lineage,
        instrument=instrument,
        side=side,
        quantity=quantity,
        portfolio_snapshot_version=snapshot,
        causal_root_available_at=causal_time,
        dispatch_sequence=dispatch,
        spec_set=spec_set,
        execution_policy=execution_policy,
    )
    expected = _order_intent_document(intent)
    _require_round_trip(
        payload,
        document,
        expected,
        canonical_order_intent_bytes(intent),
    )
    return intent


def _validate_approval_document_types(document: dict[str, object]) -> None:
    _require_keys(
        document,
        _APPROVAL_KEYS,
        message_type="execution_approval",
        canonicalization=EXECUTION_APPROVAL_CANONICALIZATION,
    )
    _parse_run_id(document["run_id"])
    for field in (
        "approval_id",
        "decision_id",
        "intent_id",
        "correlation_id",
        "causation_id",
    ):
        _parse_economic_id(document[field], field_name=field)
    for field in (
        "original_intent_sha256",
        "effective_intent_sha256",
    ):
        _parse_digest(document[field], field_name=field)
    _parse_decimal(document["approved_quantity"], field_name="approved_quantity")
    for field in (
        "portfolio_snapshot_version",
        "risk_state_version",
        "dispatch_sequence",
    ):
        _require_non_negative(document[field], field_name=field)
    _parse_time(
        document["causal_root_available_at"],
        field_name="causal_root_available_at",
    )


def decode_risk_decision(
    payload: bytes,
    *,
    intent: OrderIntent,
    spec_set: InstrumentExecutionSpecSet,
) -> RiskDecision:
    """Decode and re-prove one closed risk-decision variant."""
    if type(intent) is not OrderIntent or type(spec_set) is not InstrumentExecutionSpecSet:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "RiskDecision decode context must contain exact intent/spec set",
        )
    document = _decode_json(payload)
    _require_keys(
        document,
        _RISK_DECISION_KEYS,
        message_type="risk_decision",
        canonicalization=RISK_DECISION_CANONICALIZATION,
    )
    _preflight_risk_decision_types(document)
    _parse_run_id(document["run_id"])
    decision_id = _parse_economic_id(document["decision_id"], field_name="decision_id")
    for field in ("intent_id", "correlation_id", "causation_id"):
        _parse_economic_id(document[field], field_name=field)
    _parse_digest(document["intent_sha256"], field_name="intent_sha256")
    kind = _parse_enum(
        RiskDecisionKind,
        document["kind"],
        field_name="kind",
    )
    outcome = _parse_enum(
        OutcomeCode,
        document["outcome_code"],
        field_name="outcome_code",
    )
    risk_version = _require_non_negative(
        document["risk_state_version"],
        field_name="risk_state_version",
    )
    _require_non_negative(
        document["portfolio_snapshot_version"],
        field_name="portfolio_snapshot_version",
    )
    _require_non_negative(
        document["dispatch_sequence"],
        field_name="dispatch_sequence",
    )
    _parse_time(
        document["causal_root_available_at"],
        field_name="causal_root_available_at",
    )
    approval_raw = document["approval"]
    if kind in (RiskDecisionKind.ALLOW, RiskDecisionKind.RESIZE):
        if type(approval_raw) is not dict:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "executable decision approval must be an object",
            )
        _validate_approval_document_types(approval_raw)
        approval_id = _parse_economic_id(
            approval_raw["approval_id"],
            field_name="approval_id",
        )
        approved_quantity = _parse_decimal(
            document["approved_quantity"],
            field_name="approved_quantity",
        )
        _parse_digest(
            document["effective_intent_sha256"],
            field_name="effective_intent_sha256",
        )
        if kind is RiskDecisionKind.ALLOW:
            decision = allow_order_intent(
                decision_id=decision_id,
                approval_id=approval_id,
                intent=intent,
                spec_set=spec_set,
                risk_state_version=risk_version,
            )
        else:
            decision = resize_order_intent(
                decision_id=decision_id,
                approval_id=approval_id,
                intent=intent,
                approved_quantity=approved_quantity,
                spec_set=spec_set,
                risk_state_version=risk_version,
            )
    else:
        if (
            approval_raw is not None
            or document["approved_quantity"] is not None
            or document["effective_intent_sha256"] is not None
        ):
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "non-executable decision carries executable fields",
            )
        factory = (
            reject_order_intent if kind is RiskDecisionKind.REJECT else fail_order_intent_evaluation
        )
        decision = factory(
            decision_id=decision_id,
            intent=intent,
            spec_set=spec_set,
            risk_state_version=risk_version,
        )
    if outcome is not decision.outcome_code:
        raise _fail(OutcomeCode.CONFLICTING_ID, "risk outcome code conflicts with variant")
    expected = _risk_decision_document(decision)
    _require_round_trip(
        payload,
        document,
        expected,
        canonical_risk_decision_bytes(decision),
    )
    return decision


def decode_execution_approval(
    payload: bytes,
    *,
    intent: OrderIntent,
    decision: RiskDecision,
) -> ExecutionApproval:
    """Decode the standalone bytes of the approval carried by a decision."""
    if type(intent) is not OrderIntent or type(decision) is not RiskDecision:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "approval decode context requires exact intent and decision",
        )
    if decision.approval is None:
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "non-executable decision has no approval",
        )
    document = _decode_json(payload)
    _preflight_approval_types(document)
    _validate_approval_document_types(document)
    approval = decision.approval
    if decision.intent_id != intent.intent_id or decision.intent_sha256 != order_intent_digest(
        intent
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "decision does not bind intent")
    expected = _approval_document(approval)
    _require_round_trip(
        payload,
        document,
        expected,
        canonical_execution_approval_bytes(approval),
    )
    return approval


def decode_order(
    payload: bytes,
    *,
    intent: OrderIntent,
    decision: RiskDecision,
    spec_set: InstrumentExecutionSpecSet,
) -> Order:
    """Decode and re-prove one approved Order."""
    if (
        type(intent) is not OrderIntent
        or type(decision) is not RiskDecision
        or type(spec_set) is not InstrumentExecutionSpecSet
    ):
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "Order decode context must have exact intent/decision/spec set",
        )
    document = _decode_json(payload)
    _require_keys(
        document,
        _ORDER_KEYS,
        message_type="order",
        canonicalization=ORDER_CANONICALIZATION,
    )
    _preflight_order_types(document)
    _parse_run_id(document["run_id"])
    order_id = _parse_economic_id(document["order_id"], field_name="order_id")
    for field in (
        "intent_id",
        "correlation_id",
        "causation_id",
        "decision_id",
        "approval_id",
    ):
        _parse_economic_id(document[field], field_name=field)
    for field in (
        "original_intent_sha256",
        "effective_intent_sha256",
        "risk_decision_sha256",
        "approval_sha256",
        "instrument_spec_set_sha256",
    ):
        _parse_digest(document[field], field_name=field)
    for field in (
        "portfolio_snapshot_version",
        "risk_state_version",
        "dispatch_sequence",
    ):
        _require_non_negative(document[field], field_name=field)
    _parse_instrument(document["instrument"])
    _parse_enum(OrderSide, document["side"], field_name="side")
    _parse_decimal(document["quantity"], field_name="quantity")
    _parse_enum(OrderKind, document["order_kind"], field_name="order_kind")
    _parse_enum(
        TimeInForce,
        document["time_in_force"],
        field_name="time_in_force",
    )
    if document["price_constraint"] is not None:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "Phase 1 Order price constraint must be null")
    for field, value_type in (
        ("instrument_specification_id", InstrumentSpecId),
        ("instrument_spec_set_id", InstrumentSpecSetId),
    ):
        raw = document[field]
        if type(raw) is not str:
            raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be exact str")
        value_type(raw)
    _parse_policy(document["execution_policy"])
    _parse_time(
        document["eligible_after_available_at"],
        field_name="eligible_after_available_at",
    )
    order = create_order(
        order_id=order_id,
        intent=intent,
        decision=decision,
        spec_set=spec_set,
    )
    expected = _order_document(order)
    _require_round_trip(payload, document, expected, canonical_order_bytes(order))
    return order


@final
@dataclass(frozen=True, slots=True)
class IndependentFactDecodeContext:
    spec_set: InstrumentExecutionSpecSet

    def __post_init__(self) -> None:
        if type(self.spec_set) is not InstrumentExecutionSpecSet:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "independent fact context requires an exact spec set",
            )


@final
@dataclass(frozen=True, slots=True)
class SubmissionQueryFactDecodeContext:
    spec_set: InstrumentExecutionSpecSet
    subject_order: Order

    def __post_init__(self) -> None:
        if (
            type(self.spec_set) is not InstrumentExecutionSpecSet
            or type(self.subject_order) is not Order
        ):
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "submission query context requires exact spec set and Order",
            )


FactDecodeContext = IndependentFactDecodeContext | SubmissionQueryFactDecodeContext


def _require_fact_keys(document: dict[str, object]) -> None:
    _require_envelope(
        document,
        _FACT_KEYS,
        message_type="execution_fact",
        canonicalization=EXECUTION_FACT_CANONICALIZATION,
    )
    if "dedup_identity" not in document or document["dedup_identity"] is None:
        raise _fail(
            OutcomeCode.FACT_INVALID_MISSING_DEDUP_IDENTITY,
            "fact dedup identity is missing",
        )
    missing = _FACT_KEYS.difference(document)
    if missing:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"missing fact fields: {sorted(missing)!r}")


def _parse_lifecycle_payload(
    value: object,
    *,
    kind: ExecutionFactKind,
) -> OutcomeCode:
    if type(value) is not dict or set(value) != {"outcome_code", "payload_type"}:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "lifecycle payload has invalid keys")
    if value["payload_type"] != "lifecycle":
        raise _fail(OutcomeCode.OUT_OF_RANGE, "lifecycle payload type is invalid")
    code = _parse_enum(
        OutcomeCode,
        value["outcome_code"],
        field_name="outcome_code",
    )
    expected = _LIFECYCLE_CODE_BY_KIND[kind]
    if code is expected:
        return code
    if (
        kind is ExecutionFactKind.EXPIRY
        and code is OutcomeCode.ORDER_EXPIRED_NO_ELIGIBLE_MARKET_DATA
    ):
        return code
    if code is not expected:
        raise _fail(
            OutcomeCode.OUT_OF_RANGE,
            "lifecycle outcome conflicts with fact kind",
        )
    raise AssertionError("unreachable lifecycle code")


def _parse_trade_payload(
    value: object,
    *,
    instrument: Instrument,
    spec_set: InstrumentExecutionSpecSet,
) -> tuple[OrderSide, CanonicalDecimal, CanonicalDecimal]:
    expected_keys = {
        "fees",
        "instrument_spec_set_id",
        "instrument_spec_set_sha256",
        "instrument_specification_id",
        "payload_type",
        "price",
        "quantity",
        "side",
    }
    if type(value) is not dict or set(value) != expected_keys:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "trade payload has invalid keys")
    if value["payload_type"] != "trade":
        raise _fail(OutcomeCode.OUT_OF_RANGE, "trade payload type is invalid")
    side = _parse_enum(OrderSide, value["side"], field_name="side")
    quantity = _parse_decimal(value["quantity"], field_name="quantity")
    price = _parse_decimal(value["price"], field_name="price")
    specification_id_raw = value["instrument_specification_id"]
    set_id_raw = value["instrument_spec_set_id"]
    if type(specification_id_raw) is not str or type(set_id_raw) is not str:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "trade specification IDs must be exact str",
        )
    specification_id = InstrumentSpecId(specification_id_raw)
    set_id = InstrumentSpecSetId(set_id_raw)
    set_digest = _parse_digest(
        value["instrument_spec_set_sha256"],
        field_name="instrument_spec_set_sha256",
    )
    specification, _ = _require_spec_set(
        spec_set,
        instrument=instrument,
        specification_id=specification_id,
        set_id=set_id,
        set_sha256=set_digest,
    )
    fees = _parse_fees(value["fees"])
    _require_phase1_fees(fees, specification)
    return side, quantity, price


def _parse_fees(value: object) -> tuple[FeeEntry, ...]:
    if type(value) is not list or len(value) != 1 or type(value[0]) is not dict:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "Phase 1 fees must be a one-element JSON array",
        )
    fee_document = value[0]
    if set(fee_document) != {"amount", "currency", "fee_code"}:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "fee document has invalid keys")
    if fee_document["fee_code"] != FeeCode.COMMISSION.value:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "fee code is not Phase 1 commission")
    currency = fee_document["currency"]
    if type(currency) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, "fee currency must be exact str")
    return (
        FeeEntry(
            FeeCode.COMMISSION,
            SettlementCurrency(currency),
            _parse_decimal(fee_document["amount"], field_name="fee amount"),
        ),
    )


def _parse_submission_query_payload(value: object) -> OutcomeCode:
    if type(value) is not dict or set(value) != {"outcome_code", "payload_type"}:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "submission query payload has invalid keys")
    if value["payload_type"] != "submission_query":
        raise _fail(OutcomeCode.OUT_OF_RANGE, "submission query payload type is invalid")
    code = _parse_enum(
        OutcomeCode,
        value["outcome_code"],
        field_name="outcome_code",
    )
    SubmissionQueryFactPayload(code)
    return code


def decode_execution_fact(
    payload: bytes,
    *,
    context: FactDecodeContext,
) -> ExecutionFact:
    """Decode one canonical fact under its exact kind-dependent context."""
    if type(context) not in (
        IndependentFactDecodeContext,
        SubmissionQueryFactDecodeContext,
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "fact decode context has unsupported type")
    document = _decode_json(payload)
    _require_fact_keys(document)
    _preflight_fact_types(document)
    source = _parse_source_namespace(document["source_namespace"])
    identity = _parse_dedup_identity(document["dedup_identity"])
    kind = _parse_enum(
        ExecutionFactKind,
        document["kind"],
        field_name="kind",
    )
    occurred_at = _parse_time(document["occurred_at"], field_name="occurred_at")
    instrument_value = document["instrument"]
    instrument = None if instrument_value is None else _parse_instrument(instrument_value)
    client_key = _parse_optional_digest(
        document["client_submission_key"],
        field_name="client_submission_key",
    )
    venue_order_id = _parse_optional_venue_order_id(document["venue_order_id"])
    order_id = _parse_optional_economic_id(document["order_id"], field_name="order_id")
    correlation_id = _parse_optional_economic_id(
        document["correlation_id"],
        field_name="correlation_id",
    )
    causation_id = _parse_optional_economic_id(
        document["causation_id"],
        field_name="causation_id",
    )
    provenance = _parse_provenance(document["provenance"])
    wire_fact_digest = _parse_digest(document["fact_sha256"], field_name="fact_sha256")

    if kind is ExecutionFactKind.SUBMISSION_QUERY:
        if type(context) is not SubmissionQueryFactDecodeContext:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "submission query fact requires its exact subject-Order context",
            )
        outcome = _parse_submission_query_payload(document["payload"])
        fact = create_submission_query_execution_fact(
            source_namespace=source,
            dedup_identity=identity,
            occurred_at=occurred_at,
            provenance=provenance,
            outcome_code=outcome,
            subject_order=context.subject_order,
            venue_order_id=venue_order_id,
        )
    elif type(context) is not IndependentFactDecodeContext:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "non-query fact requires independent fact decode context",
        )
    elif kind is ExecutionFactKind.TRADE:
        if instrument is None:
            raise _fail(OutcomeCode.INVALID_TYPE, "trade fact requires instrument")
        side, quantity, price = _parse_trade_payload(
            document["payload"],
            instrument=instrument,
            spec_set=context.spec_set,
        )
        fact = create_trade_execution_fact(
            source_namespace=source,
            dedup_identity=identity,
            occurred_at=occurred_at,
            provenance=provenance,
            spec_set=context.spec_set,
            instrument=instrument,
            side=side,
            quantity=quantity,
            price=price,
            client_submission_key=client_key,
            venue_order_id=venue_order_id,
            order_id=order_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
    else:
        if kind not in _LIFECYCLE_CODE_BY_KIND:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "fact kind has no declared payload")
        lifecycle_code = _parse_lifecycle_payload(document["payload"], kind=kind)
        fact = create_lifecycle_execution_fact(
            kind=kind,
            source_namespace=source,
            dedup_identity=identity,
            occurred_at=occurred_at,
            provenance=provenance,
            instrument=instrument,
            client_submission_key=client_key,
            venue_order_id=venue_order_id,
            order_id=order_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            outcome_code=(
                None if lifecycle_code is _LIFECYCLE_CODE_BY_KIND[kind] else lifecycle_code
            ),
        )
    if wire_fact_digest != fact.fact_sha256:
        raise _fail(OutcomeCode.FACT_INVALID, "fact_sha256 does not match fact preimage")
    expected = _fact_document(fact, include_digest=True)
    _require_round_trip(
        payload,
        document,
        expected,
        canonical_execution_fact_bytes(fact),
    )
    return fact


def decode_execution_fact_ingress(
    payload: bytes,
    *,
    context: FactDecodeContext,
) -> ExecutionFactIngress:
    """Decode one ingress envelope and its complete contextual fact."""
    if type(context) not in (
        IndependentFactDecodeContext,
        SubmissionQueryFactDecodeContext,
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "ingress decode context has unsupported type")
    document = _decode_json(payload)
    _require_keys(
        document,
        _INGRESS_KEYS,
        message_type="execution_fact_ingress",
        canonicalization=EXECUTION_FACT_INGRESS_CANONICALIZATION,
    )
    fact_document = document["fact"]
    if type(fact_document) is not dict:
        raise _fail(OutcomeCode.INVALID_TYPE, "ingress fact must be an object")
    _require_fact_keys(fact_document)
    _preflight_ingress_types(document, fact_document)
    fact = decode_execution_fact(_encode_json(fact_document), context=context)
    source = _parse_source_namespace(document["source_namespace"])
    sequence = _require_non_negative(
        document["ingress_sequence"],
        field_name="ingress_sequence",
    )
    available_at = _parse_time(document["available_at"], field_name="available_at")
    ingress = create_execution_fact_ingress(
        available_at=available_at,
        source_namespace=source,
        ingress_sequence=sequence,
        fact=fact,
    )
    expected = _ingress_document(ingress)
    _require_round_trip(
        payload,
        document,
        expected,
        canonical_execution_fact_ingress_bytes(ingress),
    )
    return ingress


def decode_fill(
    payload: bytes,
    *,
    fact: ExecutionFact,
    spec_set: InstrumentExecutionSpecSet,
) -> Fill:
    """Decode one Fill and re-prove that it is copied from the supplied trade fact."""
    if type(fact) is not ExecutionFact or type(spec_set) is not InstrumentExecutionSpecSet:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "Fill decode context requires exact fact and spec set",
        )
    document = _decode_json(payload)
    _require_keys(
        document,
        _FILL_KEYS,
        message_type="fill",
        canonicalization=FILL_CANONICALIZATION,
    )
    _preflight_fill_types(document)
    _parse_run_id(document["run_id"])
    fill_id = _parse_economic_id(document["fill_id"], field_name="fill_id")
    _parse_source_namespace(document["source_namespace"])
    _parse_dedup_identity(document["dedup_identity"])
    for field in (
        "fact_sha256",
        "instrument_spec_set_sha256",
    ):
        _parse_digest(document[field], field_name=field)
    _parse_provenance(document["provenance"])
    _parse_time(document["occurred_at"], field_name="occurred_at")
    _parse_instrument(document["instrument"])
    _parse_enum(OrderSide, document["side"], field_name="side")
    _parse_decimal(document["quantity"], field_name="quantity")
    _parse_decimal(document["price"], field_name="price")
    _parse_fees(document["fees"])
    for field, value_type in (
        ("instrument_specification_id", InstrumentSpecId),
        ("instrument_spec_set_id", InstrumentSpecSetId),
    ):
        raw = document[field]
        if type(raw) is not str:
            raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be exact str")
        value_type(raw)
    _parse_optional_digest(
        document["client_submission_key"],
        field_name="client_submission_key",
    )
    _parse_optional_venue_order_id(document["venue_order_id"])
    for field in ("order_id", "correlation_id", "causation_id"):
        _parse_optional_economic_id(document[field], field_name=field)
    fill = create_fill(fill_id=fill_id, fact=fact, spec_set=spec_set)
    expected = _fill_document(fill)
    _require_round_trip(payload, document, expected, canonical_fill_bytes(fill))
    return fill
