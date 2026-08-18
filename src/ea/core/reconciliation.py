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
from ea.core.execution_messages import FactProvenanceId, Fill, Order, fill_digest, order_digest
from ea.core.identity import Instrument, VenueId
from ea.core.outcomes import OutcomeCode
from ea.core.portfolio import (
    OpenReconciliationRef,
    PortfolioSnapshot,
    portfolio_snapshot_digest,
)
from ea.core.run import RunBinding, RunId, Sha256Digest
from ea.core.runtime import ReconciliationObservationKind, RuntimeIdentifier
from ea.core.time import TimeValidationError, require_utc

RECONCILIATION_OBSERVATION_SCHEMA = "ea.reconciliation-observation.v1"
RECONCILIATION_OBSERVATION_DIGEST_DOMAIN = b"ea.reconciliation-observation.v1\0"
RECONCILIATION_OUTCOME_SCHEMA = "ea.reconciliation-outcome.v2"
RECONCILIATION_OUTCOME_DIGEST_DOMAIN = b"ea.reconciliation-outcome.v2\0"
RECONCILIATION_ADJUSTMENT_COMMAND_SCHEMA = "ea.reconciliation-adjustment-command.v1"
RECONCILIATION_ADJUSTMENT_COMMAND_DIGEST_DOMAIN = b"ea.reconciliation-adjustment-command.v1\0"
RECONCILIATION_ADJUSTMENT_AUTHORIZATION_SCHEMA = "ea.reconciliation-adjustment-authorization.v1"
RECONCILIATION_ADJUSTMENT_AUTHORIZATION_DIGEST_DOMAIN = (
    b"ea.reconciliation-adjustment-authorization.v1\0"
)
AUDITED_RECONCILIATION_ADJUSTMENT_AUTHORIZATION_SCHEMA = (
    "ea.audited-reconciliation-adjustment-authorization.v1"
)
AUDITED_RECONCILIATION_ADJUSTMENT_AUTHORIZATION_DIGEST_DOMAIN = (
    b"ea.audited-reconciliation-adjustment-authorization.v1\0"
)
RECONCILIATION_CANONICALIZATION = "ea-canonical-json-v1"
MAX_RECONCILIATION_PAYLOAD_BYTES = 16_384
MAX_RECONCILIATION_BALANCES = 32

_ANCESTRY_EVIDENCE_DIGEST_DOMAIN = b"ea.reconciliation-ancestry-evidence.v1\0"
_ANCESTRY_EVIDENCE_CANONICALIZATION = "ea-reconciliation-v1"
_PORTFOLIO_LEDGER_WATERMARK_NAMESPACE = SourceNamespace("ledger.portfolio")

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


class ReconciliationWatermarkComparison(StrEnum):
    EQUAL = "equal"
    REMOTE_LOWER = "remote_lower"
    REMOTE_HIGHER = "remote_higher"
    INCOMPARABLE = "incomparable"


class ReconciliationRequestedAction(StrEnum):
    NONE = "none"
    REQUEST_MISSING_TRADE_FACTS = "request_missing_trade_facts"
    RETAIN_AND_HALT = "retain_and_halt"
    MANUAL_EVIDENCE_DECOMPOSITION = "manual_evidence_decomposition"
    PROPOSE_SINGLE_TARGET_ADJUSTMENT = "propose_single_target_adjustment"
    PROPOSE_ANCESTRY_RESOLUTION = "propose_ancestry_resolution"


class ReconciliationDiscrepancyKind(StrEnum):
    INSTRUMENT_POSITION = "instrument_position"
    SETTLEMENT_CASH = "settlement_cash"


class ReconciliationAdjustmentVariant(StrEnum):
    BALANCE_CORRECTION = "balance_correction"
    ANCESTRY_RESOLUTION = "ancestry_resolution"


class ReconciliationAdjustmentTargetKind(StrEnum):
    INSTRUMENT_POSITION = "instrument_position"
    SETTLEMENT_CASH = "settlement_cash"
    OPEN_RECONCILIATION_REF = "open_reconciliation_ref"


class ReconciliationAuthorizationDecision(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"


@final
@dataclass(frozen=True, slots=True)
class ReconciliationAuthorizationPolicyId:
    value: str

    def __post_init__(self) -> None:
        try:
            RuntimeIdentifier(self.value)
        except (TypeError, ValueError) as error:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "reconciliation authorization policy ID is invalid",
            ) from error


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
class PositionReconciliationDiscrepancy:
    instrument: Instrument
    local_amount: CanonicalDecimal
    observed_amount: CanonicalDecimal
    delta: CanonicalDecimal
    _seal: object

    def __init__(self) -> None:
        raise TypeError("position discrepancies are created only by their factory")


@final
@dataclass(frozen=True, slots=True, init=False)
class CashReconciliationDiscrepancy:
    currency: SettlementCurrency
    local_amount: CanonicalDecimal
    observed_amount: CanonicalDecimal
    delta: CanonicalDecimal
    _seal: object

    def __init__(self) -> None:
        raise TypeError("cash discrepancies are created only by their factory")


ReconciliationDiscrepancy = PositionReconciliationDiscrepancy | CashReconciliationDiscrepancy


@final
@dataclass(frozen=True, slots=True, init=False)
class ReconciliationOutcome:
    run_id: RunId
    dispatch_sequence: int
    observation_sha256: Sha256Digest
    local_snapshot_version: int
    local_snapshot_sha256: Sha256Digest
    ledger_sequence: int
    watermark_comparison: ReconciliationWatermarkComparison
    discrepancies: tuple[ReconciliationDiscrepancy, ...]
    outcome_code: OutcomeCode
    requested_action: ReconciliationRequestedAction
    halt_requested: bool
    _seal: object

    def __init__(self) -> None:
        raise TypeError("reconciliation outcomes are created only by their factory")


@final
@dataclass(frozen=True, slots=True, init=False)
class ReconciliationAdjustmentCommand:
    run_id: RunId
    instrument_spec_set_id: InstrumentSpecSetId
    instrument_spec_set_sha256: Sha256Digest
    adjustment_id: EconomicId
    observation_sha256: Sha256Digest
    reconciliation_outcome_sha256: Sha256Digest
    ledger_sequence: int
    local_snapshot_sha256: Sha256Digest
    variant: ReconciliationAdjustmentVariant
    target_kind: ReconciliationAdjustmentTargetKind
    instrument: Instrument | None
    currency: SettlementCurrency | None
    local_amount: CanonicalDecimal | None
    observed_amount: CanonicalDecimal | None
    delta: CanonicalDecimal | None
    open_reconciliation_ref: OpenReconciliationRef | None
    ancestry_order_id: EconomicId | None
    ancestry_order_sha256: Sha256Digest | None
    dispatch_sequence: int
    _seal: object

    def __init__(self) -> None:
        raise TypeError("reconciliation adjustment commands are created only by their factory")


@final
@dataclass(frozen=True, slots=True, init=False)
class ReconciliationAdjustmentAuthorization:
    _binding: RunBinding
    run_id: RunId
    instrument_spec_set_id: InstrumentSpecSetId
    instrument_spec_set_sha256: Sha256Digest
    authorization_id: EconomicId
    adjustment_id: EconomicId
    observation_sha256: Sha256Digest
    reconciliation_outcome_sha256: Sha256Digest
    outcome_acknowledgement_sha256: Sha256Digest
    ledger_sequence: int
    local_snapshot_sha256: Sha256Digest
    command_sha256: Sha256Digest
    policy_id: ReconciliationAuthorizationPolicyId
    policy_version: int
    policy_sha256: Sha256Digest
    decision: ReconciliationAuthorizationDecision
    available_at: datetime
    dispatch_sequence: int
    _seal: object

    def __init__(self) -> None:
        raise TypeError("reconciliation authorizations are created only by their authority")


@final
@dataclass(frozen=True, slots=True, init=False)
class AuditedReconciliationAdjustmentAuthorization:
    authorization: ReconciliationAdjustmentAuthorization
    authorization_sha256: Sha256Digest
    authorization_record_id: EconomicId
    acknowledgement_sha256: Sha256Digest
    chain_head_sha256: Sha256Digest
    _seal: object

    def __init__(self) -> None:
        raise TypeError("audited reconciliation authorizations require exact audit evidence")


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


def create_position_reconciliation_discrepancy(
    *,
    spec_set: InstrumentExecutionSpecSet,
    instrument: Instrument,
    local_amount: CanonicalDecimal,
    observed_amount: CanonicalDecimal,
) -> PositionReconciliationDiscrepancy:
    if type(spec_set) is not InstrumentExecutionSpecSet or type(instrument) is not Instrument:
        raise _fail(OutcomeCode.INVALID_TYPE, "position discrepancy identity must be exact")
    for field, amount in (("local_amount", local_amount), ("observed_amount", observed_amount)):
        if type(amount) is not CanonicalDecimal:
            raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be exact")
        try:
            require_quantized(
                amount,
                spec_set.require(instrument).quantity_quantum,
                field_name=f"position_discrepancy_{field}",
            )
        except EconomicValidationError as error:
            raise _fail(error.code, f"position discrepancy {field} is invalid") from error
    delta = _subtract_decimal(observed_amount, local_amount)
    if delta == CanonicalDecimal("0"):
        raise _fail(OutcomeCode.CONFLICTING_ID, "position discrepancy delta cannot be zero")
    value = object.__new__(PositionReconciliationDiscrepancy)
    object.__setattr__(value, "instrument", instrument)
    object.__setattr__(value, "local_amount", local_amount)
    object.__setattr__(value, "observed_amount", observed_amount)
    object.__setattr__(value, "delta", delta)
    object.__setattr__(value, "_seal", _VALUE_SEAL)
    return value


def create_cash_reconciliation_discrepancy(
    *,
    spec_set: InstrumentExecutionSpecSet,
    currency: SettlementCurrency,
    local_amount: CanonicalDecimal,
    observed_amount: CanonicalDecimal,
) -> CashReconciliationDiscrepancy:
    if type(spec_set) is not InstrumentExecutionSpecSet or type(currency) is not SettlementCurrency:
        raise _fail(OutcomeCode.INVALID_TYPE, "cash discrepancy identity must be exact")
    quantums = {
        specification.currency_quantum
        for specification in spec_set.specifications
        if specification.settlement_currency == currency
    }
    if len(quantums) != 1:
        raise _fail(OutcomeCode.CONFLICTING_ID, "cash discrepancy currency binding conflicts")
    quantum = next(iter(quantums))
    for field, amount in (("local_amount", local_amount), ("observed_amount", observed_amount)):
        if type(amount) is not CanonicalDecimal:
            raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be exact")
        try:
            require_quantized(
                amount,
                quantum,
                field_name=f"cash_discrepancy_{field}",
            )
        except EconomicValidationError as error:
            raise _fail(error.code, f"cash discrepancy {field} is invalid") from error
    delta = _subtract_decimal(observed_amount, local_amount)
    if delta == CanonicalDecimal("0"):
        raise _fail(OutcomeCode.CONFLICTING_ID, "cash discrepancy delta cannot be zero")
    value = object.__new__(CashReconciliationDiscrepancy)
    object.__setattr__(value, "currency", currency)
    object.__setattr__(value, "local_amount", local_amount)
    object.__setattr__(value, "observed_amount", observed_amount)
    object.__setattr__(value, "delta", delta)
    object.__setattr__(value, "_seal", _VALUE_SEAL)
    return value


def create_reconciliation_outcome(
    *,
    run_id: RunId,
    dispatch_sequence: int,
    observation_sha256: Sha256Digest,
    local_snapshot_version: int,
    local_snapshot_sha256: Sha256Digest,
    ledger_sequence: int,
    watermark_comparison: ReconciliationWatermarkComparison,
    discrepancies: tuple[ReconciliationDiscrepancy, ...],
    outcome_code: OutcomeCode,
    requested_action: ReconciliationRequestedAction,
    halt_requested: bool,
) -> ReconciliationOutcome:
    if type(run_id) is not RunId:
        raise _fail(OutcomeCode.INVALID_TYPE, "run_id must be exact")
    _require_uint64(dispatch_sequence, "dispatch_sequence")
    _require_uint64(local_snapshot_version, "local_snapshot_version")
    _require_uint64(ledger_sequence, "ledger_sequence")
    if dispatch_sequence == 0:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "dispatch_sequence must be positive")
    for field, digest in (
        ("observation_sha256", observation_sha256),
        ("local_snapshot_sha256", local_snapshot_sha256),
    ):
        if type(digest) is not Sha256Digest:
            raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be exact")
    if (
        type(watermark_comparison) is not ReconciliationWatermarkComparison
        or type(discrepancies) is not tuple
        or any(
            type(item) not in (PositionReconciliationDiscrepancy, CashReconciliationDiscrepancy)
            or item._seal is not _VALUE_SEAL
            for item in discrepancies
        )
        or type(outcome_code) is not OutcomeCode
        or type(requested_action) is not ReconciliationRequestedAction
        or type(halt_requested) is not bool
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "reconciliation outcome values must be exact")
    if len(discrepancies) > MAX_RECONCILIATION_BALANCES:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "reconciliation discrepancy tuple exceeds 32")
    if tuple(_discrepancy_sort_key(item) for item in discrepancies) != tuple(
        sorted({_discrepancy_sort_key(item) for item in discrepancies})
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation discrepancies are not canonical")
    _require_outcome_matrix(
        watermark_comparison,
        discrepancies,
        outcome_code,
        requested_action,
        halt_requested,
    )
    value = object.__new__(ReconciliationOutcome)
    for field, candidate in (
        ("run_id", run_id),
        ("dispatch_sequence", dispatch_sequence),
        ("observation_sha256", observation_sha256),
        ("local_snapshot_version", local_snapshot_version),
        ("local_snapshot_sha256", local_snapshot_sha256),
        ("ledger_sequence", ledger_sequence),
        ("watermark_comparison", watermark_comparison),
        ("discrepancies", discrepancies),
        ("outcome_code", outcome_code),
        ("requested_action", requested_action),
        ("halt_requested", halt_requested),
    ):
        object.__setattr__(value, field, candidate)
    object.__setattr__(value, "_seal", _VALUE_SEAL)
    canonical_reconciliation_outcome_bytes(value)
    return value


def canonical_reconciliation_outcome_bytes(outcome: ReconciliationOutcome) -> bytes:
    if type(outcome) is not ReconciliationOutcome or outcome._seal is not _VALUE_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "reconciliation outcome must be factory-issued")
    payload = _canonical_json(_outcome_document(outcome))
    if len(payload) > MAX_RECONCILIATION_PAYLOAD_BYTES:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "reconciliation outcome exceeds byte bound")
    return payload


def reconciliation_outcome_digest(outcome: ReconciliationOutcome) -> Sha256Digest:
    return _framed_digest(
        RECONCILIATION_OUTCOME_DIGEST_DOMAIN,
        canonical_reconciliation_outcome_bytes(outcome),
    )


def decode_reconciliation_outcome(
    canonical_payload: bytes,
    spec_set: InstrumentExecutionSpecSet,
) -> ReconciliationOutcome:
    document = _decode_canonical_json(canonical_payload, "reconciliation outcome")
    expected_fields = {
        "canonicalization",
        "discrepancies",
        "dispatch_sequence",
        "halt_requested",
        "ledger_sequence",
        "local_snapshot_sha256",
        "local_snapshot_version",
        "observation_sha256",
        "outcome_code",
        "requested_action",
        "run_id",
        "schema",
        "watermark_comparison",
    }
    if type(document) is not dict or set(document) != expected_fields:
        raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation outcome fields conflict")
    if (
        _require_text(document, "schema") != RECONCILIATION_OUTCOME_SCHEMA
        or _require_text(document, "canonicalization") != RECONCILIATION_CANONICALIZATION
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation outcome schema conflicts")
    if type(spec_set) is not InstrumentExecutionSpecSet:
        raise _fail(OutcomeCode.INVALID_TYPE, "spec_set must be exact")
    discrepancy_documents = document["discrepancies"]
    if type(discrepancy_documents) is not list:
        raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation discrepancies must be an array")
    try:
        outcome = create_reconciliation_outcome(
            run_id=RunId(_require_text(document, "run_id")),
            dispatch_sequence=_require_json_int(document, "dispatch_sequence"),
            observation_sha256=_decode_digest(document, "observation_sha256"),
            local_snapshot_version=_require_json_int(document, "local_snapshot_version"),
            local_snapshot_sha256=_decode_digest(document, "local_snapshot_sha256"),
            ledger_sequence=_require_json_int(document, "ledger_sequence"),
            watermark_comparison=ReconciliationWatermarkComparison(
                _require_text(document, "watermark_comparison")
            ),
            discrepancies=tuple(
                _decode_discrepancy(item, spec_set) for item in discrepancy_documents
            ),
            outcome_code=OutcomeCode(_require_text(document, "outcome_code")),
            requested_action=ReconciliationRequestedAction(
                _require_text(document, "requested_action")
            ),
            halt_requested=_require_json_bool(document, "halt_requested"),
        )
    except (ValueError, TypeError) as error:
        if type(error) is ReconciliationContractError:
            raise
        raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation outcome values conflict") from error
    if canonical_reconciliation_outcome_bytes(outcome) != canonical_payload:
        raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation outcome round-trip conflicts")
    return outcome


def _create_reconciliation_adjustment_command(
    *,
    binding: RunBinding,
    spec_set: InstrumentExecutionSpecSet,
    observation: ReconciliationObservation,
    outcome: ReconciliationOutcome,
    outcome_acknowledgement: object,
    local_snapshot: PortfolioSnapshot,
    adjustment_id: EconomicId,
    open_reconciliation_ref: OpenReconciliationRef | None = None,
    ancestry_fill: Fill | None = None,
    ancestry_order: Order | None = None,
) -> ReconciliationAdjustmentCommand:
    """Derive one command only after the exact v2 outcome is durably acknowledged."""
    from ea.core.audit import (
        AuditLogicalKey,
        AuditRecordKind,
        AuditSubjectKind,
        audit_subject_digest,
        require_audit_acknowledgement,
    )

    if (
        type(binding) is not RunBinding
        or type(spec_set) is not InstrumentExecutionSpecSet
        or type(observation) is not ReconciliationObservation
        or observation._seal is not _VALUE_SEAL
        or type(outcome) is not ReconciliationOutcome
        or outcome._seal is not _VALUE_SEAL
        or type(local_snapshot) is not PortfolioSnapshot
        or type(adjustment_id) is not EconomicId
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "adjustment command evidence must be exact")
    run_id = binding.reference.run_id
    if (
        observation.run_id != run_id
        or outcome.run_id != run_id
        or adjustment_id.run_id != run_id
        or adjustment_id.owner_kind is not EconomicOwnerKind.RECONCILIATION_ADJUSTMENT
        or observation.instrument_spec_set_id != spec_set.identifier
        or observation.instrument_spec_set_sha256 != instrument_spec_set_digest(spec_set)
        or outcome.observation_sha256 != reconciliation_observation_digest(observation)
        or local_snapshot.run_id != run_id
        or local_snapshot.instrument_spec_set_id != spec_set.identifier
        or local_snapshot.instrument_spec_set_sha256 != instrument_spec_set_digest(spec_set)
        or local_snapshot.snapshot_version != outcome.local_snapshot_version
        or local_snapshot.ledger_sequence != outcome.ledger_sequence
        or portfolio_snapshot_digest(local_snapshot) != outcome.local_snapshot_sha256
        or observation.watermark_namespace != _PORTFOLIO_LEDGER_WATERMARK_NAMESPACE
        or observation.watermark_sequence != local_snapshot.ledger_sequence
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "adjustment command evidence bindings conflict")
    outcome_payload = canonical_reconciliation_outcome_bytes(outcome)
    subject_sha256 = audit_subject_digest(
        AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME,
        outcome_payload,
    )
    try:
        require_audit_acknowledgement(
            outcome_acknowledgement,
            binding=binding,
            logical_key=AuditLogicalKey(
                AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME,
                AuditSubjectKind.RECONCILIATION_OUTCOME,
                subject_sha256,
            ),
            canonical_payload=outcome_payload,
        )
    except ValueError as error:
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "adjustment command requires the exact outcome acknowledgement",
        ) from error
    if outcome.requested_action is ReconciliationRequestedAction.PROPOSE_SINGLE_TARGET_ADJUSTMENT:
        if (
            len(outcome.discrepancies) != 1
            or open_reconciliation_ref is not None
            or ancestry_fill is not None
            or ancestry_order is not None
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "balance command evidence conflicts")
        discrepancy = outcome.discrepancies[0]
        if type(discrepancy) is PositionReconciliationDiscrepancy:
            if (
                observation.kind is not ReconciliationObservationKind.POSITION_SNAPSHOT
                or PositionReconciliationBalance(
                    discrepancy.instrument,
                    discrepancy.observed_amount,
                )
                not in observation.balances
                or discrepancy.local_amount
                != _snapshot_position_amount(local_snapshot, discrepancy.instrument)
            ):
                raise _fail(OutcomeCode.CONFLICTING_ID, "position proposal evidence conflicts")
            return _build_adjustment_command(
                spec_set=spec_set,
                outcome=outcome,
                adjustment_id=adjustment_id,
                variant=ReconciliationAdjustmentVariant.BALANCE_CORRECTION,
                target_kind=ReconciliationAdjustmentTargetKind.INSTRUMENT_POSITION,
                instrument=discrepancy.instrument,
                currency=None,
                local_amount=discrepancy.local_amount,
                observed_amount=discrepancy.observed_amount,
                delta=discrepancy.delta,
                open_reconciliation_ref=None,
                ancestry_order_id=None,
                ancestry_order_sha256=None,
            )
        assert type(discrepancy) is CashReconciliationDiscrepancy
        if (
            observation.kind is not ReconciliationObservationKind.CASH_SNAPSHOT
            or CashReconciliationBalance(discrepancy.currency, discrepancy.observed_amount)
            not in observation.balances
            or discrepancy.local_amount
            != _snapshot_cash_amount(local_snapshot, discrepancy.currency)
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "cash proposal evidence conflicts")
        return _build_adjustment_command(
            spec_set=spec_set,
            outcome=outcome,
            adjustment_id=adjustment_id,
            variant=ReconciliationAdjustmentVariant.BALANCE_CORRECTION,
            target_kind=ReconciliationAdjustmentTargetKind.SETTLEMENT_CASH,
            instrument=None,
            currency=discrepancy.currency,
            local_amount=discrepancy.local_amount,
            observed_amount=discrepancy.observed_amount,
            delta=discrepancy.delta,
            open_reconciliation_ref=None,
            ancestry_order_id=None,
            ancestry_order_sha256=None,
        )
    if outcome.requested_action is not ReconciliationRequestedAction.PROPOSE_ANCESTRY_RESOLUTION:
        raise _fail(OutcomeCode.CONFLICTING_ID, "outcome does not authorize command derivation")
    if (
        type(open_reconciliation_ref) is not OpenReconciliationRef
        or type(ancestry_fill) is not Fill
        or type(ancestry_order) is not Order
        or open_reconciliation_ref.fill_id.run_id != run_id
        or open_reconciliation_ref not in local_snapshot.open_reconciliation_refs
        or not any(
            binding.fill_id == open_reconciliation_ref.fill_id
            and binding.fill_sha256 == open_reconciliation_ref.fill_sha256
            for binding in local_snapshot.open_reconciliation_bindings
        )
        or ancestry_fill.fill_id != open_reconciliation_ref.fill_id
        or fill_digest(ancestry_fill) != open_reconciliation_ref.fill_sha256
        or ancestry_fill.run_id != run_id
        or ancestry_fill.instrument_spec_set_id != spec_set.identifier
        or ancestry_fill.instrument_spec_set_sha256 != instrument_spec_set_digest(spec_set)
        or ancestry_order.run_id != run_id
        or ancestry_order.instrument_spec_set_id != spec_set.identifier
        or ancestry_order.instrument_spec_set_sha256 != instrument_spec_set_digest(spec_set)
        or observation.kind is not ReconciliationObservationKind.ORDER_DETAIL
        or observation.declared_scope_id != _ancestry_order_scope_id(ancestry_order)
        or observation.provenance_payload_sha256
        != _ancestry_evidence_digest(
            ancestry_fill,
            open_reconciliation_ref,
            ancestry_order,
        )
        or not _fill_ancestry_is_compatible(ancestry_fill, ancestry_order)
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "ancestry command evidence conflicts")
    return _build_adjustment_command(
        spec_set=spec_set,
        outcome=outcome,
        adjustment_id=adjustment_id,
        variant=ReconciliationAdjustmentVariant.ANCESTRY_RESOLUTION,
        target_kind=ReconciliationAdjustmentTargetKind.OPEN_RECONCILIATION_REF,
        instrument=None,
        currency=None,
        local_amount=None,
        observed_amount=None,
        delta=None,
        open_reconciliation_ref=open_reconciliation_ref,
        ancestry_order_id=ancestry_order.order_id,
        ancestry_order_sha256=order_digest(ancestry_order),
    )


def _snapshot_position_amount(
    snapshot: PortfolioSnapshot,
    instrument: Instrument,
) -> CanonicalDecimal:
    for balance in snapshot.position_balances:
        if balance.instrument == instrument:
            return balance.quantity
    return CanonicalDecimal("0")


def _snapshot_cash_amount(
    snapshot: PortfolioSnapshot,
    currency: SettlementCurrency,
) -> CanonicalDecimal:
    for balance in snapshot.cash_balances:
        if balance.currency == currency:
            return balance.amount
    return CanonicalDecimal("0")


def _ancestry_order_scope_id(order: Order) -> RuntimeIdentifier:
    return RuntimeIdentifier(
        f"execution_order:{order.run_id.value}:{order.order_id.owner_sequence}"
    )


def _ancestry_evidence_digest(
    fill: Fill,
    reference: OpenReconciliationRef,
    order: Order,
) -> Sha256Digest:
    return _framed_digest(
        _ANCESTRY_EVIDENCE_DIGEST_DOMAIN,
        _canonical_ancestry_evidence_bytes(fill, reference, order),
    )


def _canonical_ancestry_evidence_bytes(
    fill: Fill,
    reference: OpenReconciliationRef,
    order: Order,
) -> bytes:
    return _canonical_json(
        {
            "canonicalization": _ANCESTRY_EVIDENCE_CANONICALIZATION,
            "fill_id": _economic_id_document(fill.fill_id),
            "fill_sha256": fill_digest(fill).value,
            "order_id": _economic_id_document(order.order_id),
            "order_sha256": order_digest(order).value,
            "processing_outcome_sha256": reference.processing_outcome_sha256.value,
            "run_id": fill.run_id.value,
            "schema": "ea.reconciliation-ancestry-evidence.v1",
        }
    )


def _fill_ancestry_is_compatible(fill: Fill, order: Order) -> bool:
    missing_ancestry = (
        fill.order_id is None or fill.correlation_id is None or fill.causation_id is None
    )
    return (
        missing_ancestry
        and fill.instrument == order.instrument
        and fill.side is order.side
        and fill.instrument_specification_id == order.instrument_specification_id
        and (
            fill.client_submission_key is None
            or fill.client_submission_key == order.client_submission_key
        )
        and (fill.order_id is None or fill.order_id == order.order_id)
        and (fill.correlation_id is None or fill.correlation_id == order.correlation_id)
        and (fill.causation_id is None or fill.causation_id == order.order_id)
    )


def _build_adjustment_command(
    *,
    spec_set: InstrumentExecutionSpecSet,
    outcome: ReconciliationOutcome,
    adjustment_id: EconomicId,
    variant: ReconciliationAdjustmentVariant,
    target_kind: ReconciliationAdjustmentTargetKind,
    instrument: Instrument | None,
    currency: SettlementCurrency | None,
    local_amount: CanonicalDecimal | None,
    observed_amount: CanonicalDecimal | None,
    delta: CanonicalDecimal | None,
    open_reconciliation_ref: OpenReconciliationRef | None,
    ancestry_order_id: EconomicId | None,
    ancestry_order_sha256: Sha256Digest | None,
) -> ReconciliationAdjustmentCommand:
    value = object.__new__(ReconciliationAdjustmentCommand)
    for field, candidate in (
        ("run_id", outcome.run_id),
        ("instrument_spec_set_id", spec_set.identifier),
        ("instrument_spec_set_sha256", instrument_spec_set_digest(spec_set)),
        ("adjustment_id", adjustment_id),
        ("observation_sha256", outcome.observation_sha256),
        ("reconciliation_outcome_sha256", reconciliation_outcome_digest(outcome)),
        ("ledger_sequence", outcome.ledger_sequence),
        ("local_snapshot_sha256", outcome.local_snapshot_sha256),
        ("variant", variant),
        ("target_kind", target_kind),
        ("instrument", instrument),
        ("currency", currency),
        ("local_amount", local_amount),
        ("observed_amount", observed_amount),
        ("delta", delta),
        ("open_reconciliation_ref", open_reconciliation_ref),
        ("ancestry_order_id", ancestry_order_id),
        ("ancestry_order_sha256", ancestry_order_sha256),
        ("dispatch_sequence", outcome.dispatch_sequence),
    ):
        object.__setattr__(value, field, candidate)
    object.__setattr__(value, "_seal", _VALUE_SEAL)
    canonical_reconciliation_adjustment_command_bytes(value)
    return value


def canonical_reconciliation_adjustment_command_bytes(
    command: ReconciliationAdjustmentCommand,
) -> bytes:
    if type(command) is not ReconciliationAdjustmentCommand or command._seal is not _VALUE_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "adjustment command must be factory-issued")
    payload = _canonical_json(_adjustment_command_document(command))
    if len(payload) > MAX_RECONCILIATION_PAYLOAD_BYTES:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "adjustment command exceeds byte bound")
    return payload


def reconciliation_adjustment_command_digest(
    command: ReconciliationAdjustmentCommand,
) -> Sha256Digest:
    return _framed_digest(
        RECONCILIATION_ADJUSTMENT_COMMAND_DIGEST_DOMAIN,
        canonical_reconciliation_adjustment_command_bytes(command),
    )


def _create_reconciliation_adjustment_authorization(
    *,
    binding: RunBinding,
    spec_set: InstrumentExecutionSpecSet,
    outcome: ReconciliationOutcome,
    outcome_acknowledgement: object,
    command: ReconciliationAdjustmentCommand,
    authorization_id: EconomicId,
    policy_id: ReconciliationAuthorizationPolicyId,
    policy_version: int,
    policy_sha256: Sha256Digest,
    decision: ReconciliationAuthorizationDecision,
    available_at: datetime,
) -> ReconciliationAdjustmentAuthorization:
    from ea.core.audit import (
        AuditLogicalKey,
        AuditRecordKind,
        AuditSubjectKind,
        audit_append_acknowledgement_digest,
        audit_subject_digest,
        require_audit_acknowledgement,
    )

    if (
        type(binding) is not RunBinding
        or type(spec_set) is not InstrumentExecutionSpecSet
        or type(outcome) is not ReconciliationOutcome
        or outcome._seal is not _VALUE_SEAL
        or type(command) is not ReconciliationAdjustmentCommand
        or command._seal is not _VALUE_SEAL
        or type(authorization_id) is not EconomicId
        or type(policy_id) is not ReconciliationAuthorizationPolicyId
        or type(policy_sha256) is not Sha256Digest
        or type(decision) is not ReconciliationAuthorizationDecision
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "authorization evidence must be exact")
    _require_uint64(policy_version, "policy_version")
    if policy_version == 0:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "policy_version must be positive")
    if authorization_id.owner_sequence == 0:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "authorization identity sequence must be positive")
    checked_available_at = _require_time(available_at, "available_at")
    run_id = binding.reference.run_id
    if (
        command.run_id != run_id
        or outcome.run_id != run_id
        or authorization_id.run_id != run_id
        or authorization_id.owner_kind is not EconomicOwnerKind.RECONCILIATION_AUTHORIZATION
        or command.instrument_spec_set_id != spec_set.identifier
        or command.instrument_spec_set_sha256 != instrument_spec_set_digest(spec_set)
        or command.reconciliation_outcome_sha256 != reconciliation_outcome_digest(outcome)
        or command.observation_sha256 != outcome.observation_sha256
        or command.ledger_sequence != outcome.ledger_sequence
        or command.local_snapshot_sha256 != outcome.local_snapshot_sha256
        or command.dispatch_sequence != outcome.dispatch_sequence
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "authorization evidence bindings conflict")
    outcome_payload = canonical_reconciliation_outcome_bytes(outcome)
    outcome_subject_sha256 = audit_subject_digest(
        AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME,
        outcome_payload,
    )
    try:
        checked_acknowledgement = require_audit_acknowledgement(
            outcome_acknowledgement,
            binding=binding,
            logical_key=AuditLogicalKey(
                AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME,
                AuditSubjectKind.RECONCILIATION_OUTCOME,
                outcome_subject_sha256,
            ),
            canonical_payload=outcome_payload,
        )
    except ValueError as error:
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "authorization requires the exact outcome acknowledgement",
        ) from error
    value = object.__new__(ReconciliationAdjustmentAuthorization)
    for field, candidate in (
        ("_binding", binding),
        ("run_id", run_id),
        ("instrument_spec_set_id", spec_set.identifier),
        ("instrument_spec_set_sha256", instrument_spec_set_digest(spec_set)),
        ("authorization_id", authorization_id),
        ("adjustment_id", command.adjustment_id),
        ("observation_sha256", command.observation_sha256),
        ("reconciliation_outcome_sha256", command.reconciliation_outcome_sha256),
        (
            "outcome_acknowledgement_sha256",
            audit_append_acknowledgement_digest(checked_acknowledgement),
        ),
        ("ledger_sequence", command.ledger_sequence),
        ("local_snapshot_sha256", command.local_snapshot_sha256),
        ("command_sha256", reconciliation_adjustment_command_digest(command)),
        ("policy_id", policy_id),
        ("policy_version", policy_version),
        ("policy_sha256", policy_sha256),
        ("decision", decision),
        ("available_at", checked_available_at),
        ("dispatch_sequence", command.dispatch_sequence),
    ):
        object.__setattr__(value, field, candidate)
    object.__setattr__(value, "_seal", _VALUE_SEAL)
    canonical_reconciliation_adjustment_authorization_bytes(value)
    return value


def canonical_reconciliation_adjustment_authorization_bytes(
    authorization: ReconciliationAdjustmentAuthorization,
) -> bytes:
    if (
        type(authorization) is not ReconciliationAdjustmentAuthorization
        or authorization._seal is not _VALUE_SEAL
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "authorization must be authority-issued")
    payload = _canonical_json(_adjustment_authorization_document(authorization))
    if len(payload) > MAX_RECONCILIATION_PAYLOAD_BYTES:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "authorization exceeds byte bound")
    return payload


def reconciliation_adjustment_authorization_digest(
    authorization: ReconciliationAdjustmentAuthorization,
) -> Sha256Digest:
    return _framed_digest(
        RECONCILIATION_ADJUSTMENT_AUTHORIZATION_DIGEST_DOMAIN,
        canonical_reconciliation_adjustment_authorization_bytes(authorization),
    )


def _decode_reconciliation_adjustment_authorization(
    canonical_payload: bytes,
    *,
    binding: RunBinding,
    spec_set: InstrumentExecutionSpecSet,
    outcome: ReconciliationOutcome,
    outcome_acknowledgement: object,
    command: ReconciliationAdjustmentCommand,
) -> ReconciliationAdjustmentAuthorization:
    document = _decode_canonical_json(canonical_payload, "reconciliation authorization")
    expected_fields = {
        "adjustment_id",
        "authorization_id",
        "available_at",
        "canonicalization",
        "command_sha256",
        "decision",
        "dispatch_sequence",
        "instrument_spec_set_id",
        "instrument_spec_set_sha256",
        "ledger_sequence",
        "local_snapshot_sha256",
        "observation_sha256",
        "outcome_acknowledgement_sha256",
        "policy_id",
        "policy_sha256",
        "policy_version",
        "reconciliation_outcome_sha256",
        "run_id",
        "schema",
    }
    if type(document) is not dict or set(document) != expected_fields:
        raise _fail(OutcomeCode.CONFLICTING_ID, "authorization fields conflict")
    if (
        _require_text(document, "schema") != RECONCILIATION_ADJUSTMENT_AUTHORIZATION_SCHEMA
        or _require_text(document, "canonicalization") != RECONCILIATION_CANONICALIZATION
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "authorization schema conflicts")
    try:
        run_id = RunId(_require_text(document, "run_id"))
        authorization = _create_reconciliation_adjustment_authorization(
            binding=binding,
            spec_set=spec_set,
            outcome=outcome,
            outcome_acknowledgement=outcome_acknowledgement,
            command=command,
            authorization_id=_decode_economic_id(document["authorization_id"], run_id),
            policy_id=ReconciliationAuthorizationPolicyId(_require_text(document, "policy_id")),
            policy_version=_require_json_int(document, "policy_version"),
            policy_sha256=_decode_digest(document, "policy_sha256"),
            decision=ReconciliationAuthorizationDecision(_require_text(document, "decision")),
            available_at=_decode_time(_require_text(document, "available_at"), "available_at"),
        )
        if (
            run_id != binding.reference.run_id
            or _decode_economic_id(document["adjustment_id"], run_id) != command.adjustment_id
            or _require_text(document, "instrument_spec_set_id") != spec_set.identifier.value
            or _decode_digest(document, "instrument_spec_set_sha256")
            != instrument_spec_set_digest(spec_set)
            or _require_json_int(document, "dispatch_sequence") != command.dispatch_sequence
            or _require_json_int(document, "ledger_sequence") != command.ledger_sequence
            or _decode_digest(document, "local_snapshot_sha256") != command.local_snapshot_sha256
            or _decode_digest(document, "observation_sha256") != command.observation_sha256
            or _decode_digest(document, "reconciliation_outcome_sha256")
            != command.reconciliation_outcome_sha256
            or _decode_digest(document, "command_sha256")
            != reconciliation_adjustment_command_digest(command)
            or _decode_digest(document, "outcome_acknowledgement_sha256")
            != authorization.outcome_acknowledgement_sha256
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "authorization bindings conflict")
    except (ValueError, TypeError) as error:
        if type(error) is ReconciliationContractError:
            raise
        raise _fail(OutcomeCode.CONFLICTING_ID, "authorization values conflict") from error
    if canonical_reconciliation_adjustment_authorization_bytes(authorization) != canonical_payload:
        raise _fail(OutcomeCode.CONFLICTING_ID, "authorization round-trip conflicts")
    return authorization


def create_audited_reconciliation_adjustment_authorization(
    authorization: ReconciliationAdjustmentAuthorization,
    acknowledgement: object,
) -> AuditedReconciliationAdjustmentAuthorization:
    from ea.core.audit import (
        AuditLogicalKey,
        AuditRecordKind,
        AuditSubjectKind,
        audit_append_acknowledgement_digest,
        audit_subject_digest,
        require_audit_acknowledgement,
    )

    payload = canonical_reconciliation_adjustment_authorization_bytes(authorization)
    subject_sha256 = audit_subject_digest(
        AuditRecordKind.RECONCILIATION_ADJUSTMENT_AUTHORIZATION,
        payload,
    )
    try:
        checked = require_audit_acknowledgement(
            acknowledgement,
            binding=_authorization_binding(authorization),
            logical_key=AuditLogicalKey(
                AuditRecordKind.RECONCILIATION_ADJUSTMENT_AUTHORIZATION,
                AuditSubjectKind.RECONCILIATION_ADJUSTMENT_AUTHORIZATION,
                subject_sha256,
            ),
            canonical_payload=payload,
        )
    except ValueError as error:
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "audited authorization requires the exact acknowledgement",
        ) from error
    value = object.__new__(AuditedReconciliationAdjustmentAuthorization)
    object.__setattr__(value, "authorization", authorization)
    object.__setattr__(
        value,
        "authorization_sha256",
        reconciliation_adjustment_authorization_digest(authorization),
    )
    object.__setattr__(value, "authorization_record_id", checked.record_id)
    object.__setattr__(
        value,
        "acknowledgement_sha256",
        audit_append_acknowledgement_digest(checked),
    )
    object.__setattr__(value, "chain_head_sha256", checked.chain_head_sha256)
    object.__setattr__(value, "_seal", _VALUE_SEAL)
    return value


def canonical_audited_reconciliation_adjustment_authorization_bytes(
    audited: AuditedReconciliationAdjustmentAuthorization,
) -> bytes:
    if (
        type(audited) is not AuditedReconciliationAdjustmentAuthorization
        or audited._seal is not _VALUE_SEAL
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "audited authorization must be factory-issued")
    return _canonical_json(
        {
            "acknowledgement_sha256": audited.acknowledgement_sha256.value,
            "authorization_record_id": _economic_id_document(audited.authorization_record_id),
            "authorization_sha256": audited.authorization_sha256.value,
            "canonicalization": RECONCILIATION_CANONICALIZATION,
            "chain_head_sha256": audited.chain_head_sha256.value,
            "run_id": audited.authorization.run_id.value,
            "schema": AUDITED_RECONCILIATION_ADJUSTMENT_AUTHORIZATION_SCHEMA,
        }
    )


def audited_reconciliation_adjustment_authorization_digest(
    audited: AuditedReconciliationAdjustmentAuthorization,
) -> Sha256Digest:
    return _framed_digest(
        AUDITED_RECONCILIATION_ADJUSTMENT_AUTHORIZATION_DIGEST_DOMAIN,
        canonical_audited_reconciliation_adjustment_authorization_bytes(audited),
    )


def _authorization_binding(authorization: ReconciliationAdjustmentAuthorization) -> RunBinding:
    """Return the audit binding only for an exact issued authorization."""
    if (
        type(authorization) is not ReconciliationAdjustmentAuthorization
        or authorization._seal is not _VALUE_SEAL
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "authorization must be authority-issued")
    binding = authorization._binding
    if type(binding) is not RunBinding:
        raise _fail(OutcomeCode.CONFLICTING_ID, "authorization audit binding is unavailable")
    return binding


def _decode_reconciliation_adjustment_command(
    canonical_payload: bytes,
    *,
    binding: RunBinding,
    spec_set: InstrumentExecutionSpecSet,
    observation: ReconciliationObservation,
    outcome: ReconciliationOutcome,
    outcome_acknowledgement: object,
    local_snapshot: PortfolioSnapshot,
    ancestry_fill: Fill | None = None,
    ancestry_order: Order | None = None,
) -> ReconciliationAdjustmentCommand:
    document = _decode_canonical_json(canonical_payload, "reconciliation adjustment command")
    common_fields = {
        "adjustment_id",
        "canonicalization",
        "dispatch_sequence",
        "instrument_spec_set_id",
        "instrument_spec_set_sha256",
        "ledger_sequence",
        "local_snapshot_sha256",
        "observation_sha256",
        "reconciliation_outcome_sha256",
        "run_id",
        "schema",
        "variant",
    }
    if type(document) is not dict or not common_fields <= set(document):
        raise _fail(OutcomeCode.CONFLICTING_ID, "adjustment command fields conflict")
    if (
        _require_text(document, "schema") != RECONCILIATION_ADJUSTMENT_COMMAND_SCHEMA
        or _require_text(document, "canonicalization") != RECONCILIATION_CANONICALIZATION
        or type(spec_set) is not InstrumentExecutionSpecSet
        or type(outcome) is not ReconciliationOutcome
        or outcome._seal is not _VALUE_SEAL
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "adjustment command schema conflicts")
    try:
        run_id = RunId(_require_text(document, "run_id"))
        variant = ReconciliationAdjustmentVariant(_require_text(document, "variant"))
        adjustment_id = _decode_economic_id(document["adjustment_id"], run_id)
        common_checks = (
            run_id == outcome.run_id,
            adjustment_id.owner_kind is EconomicOwnerKind.RECONCILIATION_ADJUSTMENT,
            _require_text(document, "instrument_spec_set_id") == spec_set.identifier.value,
            _decode_digest(document, "instrument_spec_set_sha256")
            == instrument_spec_set_digest(spec_set),
            _require_json_int(document, "dispatch_sequence") == outcome.dispatch_sequence,
            _require_json_int(document, "ledger_sequence") == outcome.ledger_sequence,
            _decode_digest(document, "local_snapshot_sha256") == outcome.local_snapshot_sha256,
            _decode_digest(document, "observation_sha256") == outcome.observation_sha256,
            _decode_digest(document, "reconciliation_outcome_sha256")
            == reconciliation_outcome_digest(outcome),
        )
        if not all(common_checks):
            raise _fail(OutcomeCode.CONFLICTING_ID, "adjustment command binding conflicts")
        target = document["target"]
        if type(target) is not dict or type(target.get("kind")) is not str:
            raise _fail(OutcomeCode.CONFLICTING_ID, "adjustment command target conflicts")
        target_kind = ReconciliationAdjustmentTargetKind(target["kind"])
        if variant is ReconciliationAdjustmentVariant.BALANCE_CORRECTION:
            if outcome.requested_action is not (
                ReconciliationRequestedAction.PROPOSE_SINGLE_TARGET_ADJUSTMENT
            ):
                raise _fail(OutcomeCode.CONFLICTING_ID, "balance command proposal conflicts")
            if set(document) != common_fields | {
                "delta",
                "local_amount",
                "observed_amount",
                "target",
            }:
                raise _fail(OutcomeCode.CONFLICTING_ID, "balance command fields conflict")
            local_amount = CanonicalDecimal(_require_text(document, "local_amount"))
            observed_amount = CanonicalDecimal(_require_text(document, "observed_amount"))
            delta = _subtract_decimal(observed_amount, local_amount)
            if delta.text != _require_text(document, "delta"):
                raise _fail(OutcomeCode.CONFLICTING_ID, "balance command delta conflicts")
            if target_kind is ReconciliationAdjustmentTargetKind.INSTRUMENT_POSITION:
                if set(target) != {"instrument", "kind"}:
                    raise _fail(OutcomeCode.CONFLICTING_ID, "position target fields conflict")
                instrument_document = target["instrument"]
                if type(instrument_document) is not dict or set(instrument_document) != {
                    "symbol",
                    "venue",
                }:
                    raise _fail(OutcomeCode.CONFLICTING_ID, "position target conflicts")
                discrepancy: ReconciliationDiscrepancy = create_position_reconciliation_discrepancy(
                    spec_set=spec_set,
                    instrument=Instrument(
                        VenueId(_require_text(instrument_document, "venue")),
                        _require_text(instrument_document, "symbol"),
                    ),
                    local_amount=local_amount,
                    observed_amount=observed_amount,
                )
            elif target_kind is ReconciliationAdjustmentTargetKind.SETTLEMENT_CASH:
                if set(target) != {"currency", "kind"}:
                    raise _fail(OutcomeCode.CONFLICTING_ID, "cash target fields conflict")
                discrepancy = create_cash_reconciliation_discrepancy(
                    spec_set=spec_set,
                    currency=SettlementCurrency(_require_text(target, "currency")),
                    local_amount=local_amount,
                    observed_amount=observed_amount,
                )
            else:
                raise _fail(OutcomeCode.CONFLICTING_ID, "balance command target conflicts")
            if outcome.discrepancies != (discrepancy,):
                raise _fail(OutcomeCode.CONFLICTING_ID, "balance command proposal conflicts")
            command = _create_reconciliation_adjustment_command(
                binding=binding,
                spec_set=spec_set,
                observation=observation,
                outcome=outcome,
                outcome_acknowledgement=outcome_acknowledgement,
                local_snapshot=local_snapshot,
                adjustment_id=adjustment_id,
            )
        else:
            if outcome.requested_action is not (
                ReconciliationRequestedAction.PROPOSE_ANCESTRY_RESOLUTION
            ):
                raise _fail(OutcomeCode.CONFLICTING_ID, "ancestry command proposal conflicts")
            if (
                set(document)
                != common_fields
                | {
                    "ancestry_order_id",
                    "ancestry_order_sha256",
                    "target",
                }
                or target_kind is not ReconciliationAdjustmentTargetKind.OPEN_RECONCILIATION_REF
            ):
                raise _fail(OutcomeCode.CONFLICTING_ID, "ancestry command fields conflict")
            if set(target) != {
                "fill_id",
                "fill_sha256",
                "kind",
                "processing_outcome_sha256",
            }:
                raise _fail(OutcomeCode.CONFLICTING_ID, "ancestry target fields conflict")
            open_reference = OpenReconciliationRef(
                _decode_economic_id(target["fill_id"], run_id),
                Sha256Digest(_require_text(target, "fill_sha256")),
                Sha256Digest(_require_text(target, "processing_outcome_sha256")),
            )
            ancestry_order_id = _decode_economic_id(document["ancestry_order_id"], run_id)
            if ancestry_order_id.owner_kind is not EconomicOwnerKind.EXECUTION_ORDER:
                raise _fail(OutcomeCode.CONFLICTING_ID, "ancestry Order identity conflicts")
            command = _create_reconciliation_adjustment_command(
                binding=binding,
                spec_set=spec_set,
                observation=observation,
                outcome=outcome,
                outcome_acknowledgement=outcome_acknowledgement,
                local_snapshot=local_snapshot,
                adjustment_id=adjustment_id,
                open_reconciliation_ref=open_reference,
                ancestry_fill=ancestry_fill,
                ancestry_order=ancestry_order,
            )
    except (ValueError, TypeError) as error:
        if type(error) is ReconciliationContractError:
            raise
        raise _fail(OutcomeCode.CONFLICTING_ID, "adjustment command values conflict") from error
    if canonical_reconciliation_adjustment_command_bytes(command) != canonical_payload:
        raise _fail(OutcomeCode.CONFLICTING_ID, "adjustment command round-trip conflicts")
    return command


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


def _outcome_document(outcome: ReconciliationOutcome) -> dict[str, object]:
    return {
        "canonicalization": RECONCILIATION_CANONICALIZATION,
        "discrepancies": [_discrepancy_document(item) for item in outcome.discrepancies],
        "dispatch_sequence": outcome.dispatch_sequence,
        "halt_requested": outcome.halt_requested,
        "ledger_sequence": outcome.ledger_sequence,
        "local_snapshot_sha256": outcome.local_snapshot_sha256.value,
        "local_snapshot_version": outcome.local_snapshot_version,
        "observation_sha256": outcome.observation_sha256.value,
        "outcome_code": outcome.outcome_code.value,
        "requested_action": outcome.requested_action.value,
        "run_id": outcome.run_id.value,
        "schema": RECONCILIATION_OUTCOME_SCHEMA,
        "watermark_comparison": outcome.watermark_comparison.value,
    }


def _adjustment_command_document(
    command: ReconciliationAdjustmentCommand,
) -> dict[str, object]:
    document: dict[str, object] = {
        "adjustment_id": _economic_id_document(command.adjustment_id),
        "canonicalization": RECONCILIATION_CANONICALIZATION,
        "dispatch_sequence": command.dispatch_sequence,
        "instrument_spec_set_id": command.instrument_spec_set_id.value,
        "instrument_spec_set_sha256": command.instrument_spec_set_sha256.value,
        "ledger_sequence": command.ledger_sequence,
        "local_snapshot_sha256": command.local_snapshot_sha256.value,
        "observation_sha256": command.observation_sha256.value,
        "reconciliation_outcome_sha256": command.reconciliation_outcome_sha256.value,
        "run_id": command.run_id.value,
        "schema": RECONCILIATION_ADJUSTMENT_COMMAND_SCHEMA,
        "variant": command.variant.value,
    }
    if command.variant is ReconciliationAdjustmentVariant.BALANCE_CORRECTION:
        assert (
            command.local_amount is not None
            and command.observed_amount is not None
            and command.delta is not None
        )
        if command.target_kind is ReconciliationAdjustmentTargetKind.INSTRUMENT_POSITION:
            assert command.instrument is not None
            target: dict[str, object] = {
                "instrument": {
                    "symbol": command.instrument.symbol,
                    "venue": command.instrument.venue.code,
                },
                "kind": command.target_kind.value,
            }
        else:
            assert command.currency is not None
            target = {"currency": command.currency.code, "kind": command.target_kind.value}
        document.update(
            {
                "delta": command.delta.text,
                "local_amount": command.local_amount.text,
                "observed_amount": command.observed_amount.text,
                "target": target,
            }
        )
        return document
    assert (
        command.open_reconciliation_ref is not None
        and command.ancestry_order_id is not None
        and command.ancestry_order_sha256 is not None
    )
    document.update(
        {
            "ancestry_order_id": _economic_id_document(command.ancestry_order_id),
            "ancestry_order_sha256": command.ancestry_order_sha256.value,
            "target": {
                "fill_id": _economic_id_document(command.open_reconciliation_ref.fill_id),
                "fill_sha256": command.open_reconciliation_ref.fill_sha256.value,
                "kind": command.target_kind.value,
                "processing_outcome_sha256": (
                    command.open_reconciliation_ref.processing_outcome_sha256.value
                ),
            },
        }
    )
    return document


def _adjustment_authorization_document(
    authorization: ReconciliationAdjustmentAuthorization,
) -> dict[str, object]:
    return {
        "adjustment_id": _economic_id_document(authorization.adjustment_id),
        "authorization_id": _economic_id_document(authorization.authorization_id),
        "available_at": _time_text(authorization.available_at),
        "canonicalization": RECONCILIATION_CANONICALIZATION,
        "command_sha256": authorization.command_sha256.value,
        "decision": authorization.decision.value,
        "dispatch_sequence": authorization.dispatch_sequence,
        "instrument_spec_set_id": authorization.instrument_spec_set_id.value,
        "instrument_spec_set_sha256": authorization.instrument_spec_set_sha256.value,
        "ledger_sequence": authorization.ledger_sequence,
        "local_snapshot_sha256": authorization.local_snapshot_sha256.value,
        "observation_sha256": authorization.observation_sha256.value,
        "outcome_acknowledgement_sha256": authorization.outcome_acknowledgement_sha256.value,
        "policy_id": authorization.policy_id.value,
        "policy_sha256": authorization.policy_sha256.value,
        "policy_version": authorization.policy_version,
        "reconciliation_outcome_sha256": authorization.reconciliation_outcome_sha256.value,
        "run_id": authorization.run_id.value,
        "schema": RECONCILIATION_ADJUSTMENT_AUTHORIZATION_SCHEMA,
    }


def _discrepancy_document(discrepancy: ReconciliationDiscrepancy) -> dict[str, object]:
    common = {
        "delta": discrepancy.delta.text,
        "local_amount": discrepancy.local_amount.text,
        "observed_amount": discrepancy.observed_amount.text,
    }
    if type(discrepancy) is PositionReconciliationDiscrepancy:
        return {
            **common,
            "instrument": {
                "symbol": discrepancy.instrument.symbol,
                "venue": discrepancy.instrument.venue.code,
            },
            "kind": ReconciliationDiscrepancyKind.INSTRUMENT_POSITION.value,
        }
    if type(discrepancy) is CashReconciliationDiscrepancy:
        return {
            **common,
            "currency": discrepancy.currency.code,
            "kind": ReconciliationDiscrepancyKind.SETTLEMENT_CASH.value,
        }
    raise _fail(OutcomeCode.INVALID_TYPE, "reconciliation discrepancy must be exact")


def _decode_discrepancy(
    document: object,
    spec_set: InstrumentExecutionSpecSet,
) -> ReconciliationDiscrepancy:
    if type(document) is not dict or type(document.get("kind")) is not str:
        raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation discrepancy document conflicts")
    try:
        kind = ReconciliationDiscrepancyKind(document["kind"])
        if kind is ReconciliationDiscrepancyKind.INSTRUMENT_POSITION:
            if set(document) != {
                "delta",
                "instrument",
                "kind",
                "local_amount",
                "observed_amount",
            }:
                raise _fail(OutcomeCode.CONFLICTING_ID, "position discrepancy fields conflict")
            instrument_document = document["instrument"]
            if type(instrument_document) is not dict or set(instrument_document) != {
                "symbol",
                "venue",
            }:
                raise _fail(OutcomeCode.CONFLICTING_ID, "position discrepancy target conflicts")
            discrepancy: ReconciliationDiscrepancy = create_position_reconciliation_discrepancy(
                spec_set=spec_set,
                instrument=Instrument(
                    VenueId(_require_text(instrument_document, "venue")),
                    _require_text(instrument_document, "symbol"),
                ),
                local_amount=CanonicalDecimal(_require_text(document, "local_amount")),
                observed_amount=CanonicalDecimal(_require_text(document, "observed_amount")),
            )
        else:
            if set(document) != {
                "currency",
                "delta",
                "kind",
                "local_amount",
                "observed_amount",
            }:
                raise _fail(OutcomeCode.CONFLICTING_ID, "cash discrepancy fields conflict")
            discrepancy = create_cash_reconciliation_discrepancy(
                spec_set=spec_set,
                currency=SettlementCurrency(_require_text(document, "currency")),
                local_amount=CanonicalDecimal(_require_text(document, "local_amount")),
                observed_amount=CanonicalDecimal(_require_text(document, "observed_amount")),
            )
        if discrepancy.delta.text != _require_text(document, "delta"):
            raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation discrepancy delta conflicts")
        return discrepancy
    except (ValueError, TypeError) as error:
        if type(error) is ReconciliationContractError:
            raise
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "reconciliation discrepancy values conflict",
        ) from error


def _discrepancy_sort_key(discrepancy: ReconciliationDiscrepancy) -> tuple[str, str, str]:
    if type(discrepancy) is PositionReconciliationDiscrepancy:
        return (
            ReconciliationDiscrepancyKind.INSTRUMENT_POSITION.value,
            discrepancy.instrument.venue.code,
            discrepancy.instrument.symbol,
        )
    if type(discrepancy) is CashReconciliationDiscrepancy:
        return (ReconciliationDiscrepancyKind.SETTLEMENT_CASH.value, discrepancy.currency.code, "")
    raise _fail(OutcomeCode.INVALID_TYPE, "reconciliation discrepancy must be exact")


def _require_outcome_matrix(
    comparison: ReconciliationWatermarkComparison,
    discrepancies: tuple[ReconciliationDiscrepancy, ...],
    outcome_code: OutcomeCode,
    action: ReconciliationRequestedAction,
    halt_requested: bool,
) -> None:
    exact = (
        comparison,
        outcome_code,
        action,
        halt_requested,
        len(discrepancies),
    )
    allowed_exact = {
        (
            ReconciliationWatermarkComparison.EQUAL,
            OutcomeCode.RECONCILIATION_MATCH,
            ReconciliationRequestedAction.NONE,
            False,
            0,
        ),
        (
            ReconciliationWatermarkComparison.EQUAL,
            OutcomeCode.RECONCILIATION_MATCH,
            ReconciliationRequestedAction.PROPOSE_ANCESTRY_RESOLUTION,
            True,
            0,
        ),
        (
            ReconciliationWatermarkComparison.REMOTE_LOWER,
            OutcomeCode.RECONCILIATION_LOCAL_AHEAD_STALE,
            ReconciliationRequestedAction.RETAIN_AND_HALT,
            True,
            0,
        ),
        (
            ReconciliationWatermarkComparison.REMOTE_HIGHER,
            OutcomeCode.RECONCILIATION_REMOTE_AHEAD,
            ReconciliationRequestedAction.REQUEST_MISSING_TRADE_FACTS,
            True,
            0,
        ),
        (
            ReconciliationWatermarkComparison.EQUAL,
            OutcomeCode.RECONCILIATION_MISMATCH,
            ReconciliationRequestedAction.PROPOSE_SINGLE_TARGET_ADJUSTMENT,
            True,
            1,
        ),
        (
            ReconciliationWatermarkComparison.INCOMPARABLE,
            OutcomeCode.RECONCILIATION_INVALID,
            ReconciliationRequestedAction.RETAIN_AND_HALT,
            True,
            0,
        ),
        (
            ReconciliationWatermarkComparison.EQUAL,
            OutcomeCode.RECONCILIATION_UNRESOLVED_CORRELATION,
            ReconciliationRequestedAction.RETAIN_AND_HALT,
            True,
            0,
        ),
    }
    if exact in allowed_exact:
        return
    if (
        comparison is ReconciliationWatermarkComparison.EQUAL
        and outcome_code is OutcomeCode.RECONCILIATION_QUARANTINED
        and action is ReconciliationRequestedAction.MANUAL_EVIDENCE_DECOMPOSITION
        and halt_requested
        and 2 <= len(discrepancies) <= MAX_RECONCILIATION_BALANCES
    ):
        return
    raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation outcome action matrix conflicts")


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


def _require_json_bool(document: dict[str, Any], field: str) -> bool:
    value = document[field]
    if type(value) is not bool:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} must be exact JSON boolean")
    return value


def _subtract_decimal(left: CanonicalDecimal, right: CanonicalDecimal) -> CanonicalDecimal:
    scale = max(left.scale, right.scale)
    coefficient = left.coefficient * (10 ** (scale - left.scale))
    coefficient -= right.coefficient * (10 ** (scale - right.scale))
    try:
        return CanonicalDecimal(_scaled_decimal_text(coefficient, scale))
    except EconomicValidationError as error:
        # LEDGER-001: the delta can exceed the canonical 38/20/18 digit bounds
        # even when both operands are individually valid. Fail with this
        # module's closed error instead of leaking the economics-domain
        # exception type out of the discrepancy factories; the decode path
        # keeps its own fail-closed re-wrapping.
        raise _fail(
            OutcomeCode.OUT_OF_RANGE,
            "reconciliation discrepancy delta exceeds canonical decimal bounds",
        ) from error


def _scaled_decimal_text(coefficient: int, scale: int) -> str:
    if coefficient == 0:
        return "0"
    negative = coefficient < 0
    digits = str(abs(coefficient)).rjust(scale + 1, "0")
    if scale:
        digits = f"{digits[:-scale]}.{digits[-scale:]}".rstrip("0").rstrip(".")
    if digits == "0":
        return "0"
    return ("-" if negative else "") + digits


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
