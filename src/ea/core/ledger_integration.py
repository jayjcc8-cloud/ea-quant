"""Dependency-neutral audited ledger-integration values from Accepted ADR 0022."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any, final

from ea.core.execution_identity import (
    EconomicId,
    EconomicOwnerKind,
    IngressIdentity,
    SourceNamespace,
)
from ea.core.outcomes import OutcomeCode
from ea.core.portfolio import LEDGER_APPLY_OUTCOME_DIGEST_DOMAIN
from ea.core.run import RunId, Sha256Digest

LEDGER_APPLICATION_COMMAND_SCHEMA = "ea.ledger-application-command.v1"
LEDGER_APPLICATION_COMMAND_DIGEST_DOMAIN = b"ea.ledger-application-command.v1\0"
LEDGER_HANDOFF_OUTCOME_SCHEMA = "ea.ledger-handoff-outcome.v1"
LEDGER_HANDOFF_OUTCOME_DIGEST_DOMAIN = b"ea.ledger-handoff-outcome.v1\0"
LEDGER_INTEGRATION_CANONICALIZATION = "ea-canonical-json-v1"
MAX_LEDGER_INTEGRATION_PAYLOAD_BYTES = 16_384

_MAX_UINT64 = (1 << 64) - 1
_VALUE_SEAL = object()
_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.CONFLICTING_ID,
    }
)


class LedgerIntegrationError(ValueError):
    """Closed structural failure for ledger-integration evidence."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _ERROR_CODES:
            raise TypeError("ledger integration errors require an exact supported OutcomeCode")
        self.code = code
        super().__init__(message)


def _fail(code: OutcomeCode, message: str) -> LedgerIntegrationError:
    return LedgerIntegrationError(code, message)


class LedgerHandoffAction(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    EFFECT_COMMITTED = "effect_committed"
    FAILED = "failed"


class LedgerHandoffFailure(StrEnum):
    UNBOUND_EXISTING_FILL = "unbound_existing_fill"
    LEDGER_CONFLICT = "ledger_conflict"
    ARITHMETIC_FAILURE = "arithmetic_failure"
    UNBALANCED = "unbalanced"
    STRUCTURAL_ERROR = "structural_error"
    EVIDENCE_MISMATCH = "evidence_mismatch"
    INDEX_INCONSISTENT = "index_inconsistent"


@final
@dataclass(frozen=True, slots=True, init=False)
class LedgerApplicationCommand:
    run_id: RunId
    dispatch_sequence: int
    audited_handoff_sha256: Sha256Digest
    processing_outcome_sha256: Sha256Digest
    fill_id: EconomicId
    fill_sha256: Sha256Digest
    requires_reconciliation: bool
    _seal: object

    def __init__(self) -> None:
        raise TypeError("ledger application commands are issued only by the integration factory")


@final
@dataclass(frozen=True, slots=True, init=False)
class LedgerHandoffOutcome:
    run_id: RunId
    dispatch_sequence: int
    ingress_identity: IngressIdentity
    audited_handoff_sha256: Sha256Digest
    processing_outcome_sha256: Sha256Digest
    processing_outcome_ack_sha256: Sha256Digest
    fill_id: EconomicId | None
    fill_sha256: Sha256Digest | None
    action: LedgerHandoffAction
    original_ledger_apply_outcome: bytes | None
    original_ledger_apply_outcome_sha256: Sha256Digest | None
    before_snapshot_version: int
    before_snapshot_sha256: Sha256Digest
    after_snapshot_version: int
    after_snapshot_sha256: Sha256Digest
    requires_reconciliation: bool
    halt_requested: bool
    failure: LedgerHandoffFailure | None
    _seal: object

    def __init__(self) -> None:
        raise TypeError("ledger handoff outcomes are created only by their factory")


def _create_ledger_application_command(
    *,
    run_id: RunId,
    dispatch_sequence: int,
    audited_handoff_sha256: Sha256Digest,
    processing_outcome_sha256: Sha256Digest,
    fill_id: EconomicId,
    fill_sha256: Sha256Digest,
    requires_reconciliation: bool,
) -> LedgerApplicationCommand:
    _require_run_id(run_id)
    _require_positive_uint64(dispatch_sequence, "dispatch_sequence")
    _require_digest(audited_handoff_sha256, "audited_handoff_sha256")
    _require_digest(processing_outcome_sha256, "processing_outcome_sha256")
    _require_fill_id(fill_id, run_id)
    _require_digest(fill_sha256, "fill_sha256")
    if type(requires_reconciliation) is not bool:
        raise _fail(OutcomeCode.INVALID_TYPE, "requires_reconciliation must be exact bool")
    value = object.__new__(LedgerApplicationCommand)
    for name, field in (
        ("run_id", run_id),
        ("dispatch_sequence", dispatch_sequence),
        ("audited_handoff_sha256", audited_handoff_sha256),
        ("processing_outcome_sha256", processing_outcome_sha256),
        ("fill_id", fill_id),
        ("fill_sha256", fill_sha256),
        ("requires_reconciliation", requires_reconciliation),
    ):
        object.__setattr__(value, name, field)
    object.__setattr__(value, "_seal", _VALUE_SEAL)
    return value


def create_ledger_handoff_outcome(
    *,
    run_id: RunId,
    dispatch_sequence: int,
    ingress_identity: IngressIdentity,
    audited_handoff_sha256: Sha256Digest,
    processing_outcome_sha256: Sha256Digest,
    processing_outcome_ack_sha256: Sha256Digest,
    fill_id: EconomicId | None,
    fill_sha256: Sha256Digest | None,
    action: LedgerHandoffAction,
    original_ledger_apply_outcome: bytes | None,
    original_ledger_apply_outcome_sha256: Sha256Digest | None,
    before_snapshot_version: int,
    before_snapshot_sha256: Sha256Digest,
    after_snapshot_version: int,
    after_snapshot_sha256: Sha256Digest,
    requires_reconciliation: bool,
    halt_requested: bool,
    failure: LedgerHandoffFailure | None,
) -> LedgerHandoffOutcome:
    """Create one immutable result after validating its complete action matrix."""
    _require_run_id(run_id)
    _require_positive_uint64(dispatch_sequence, "dispatch_sequence")
    if type(ingress_identity) is not IngressIdentity:
        raise _fail(OutcomeCode.INVALID_TYPE, "ingress_identity must be exact")
    for name, digest in (
        ("audited_handoff_sha256", audited_handoff_sha256),
        ("processing_outcome_sha256", processing_outcome_sha256),
        ("processing_outcome_ack_sha256", processing_outcome_ack_sha256),
        ("before_snapshot_sha256", before_snapshot_sha256),
        ("after_snapshot_sha256", after_snapshot_sha256),
    ):
        _require_digest(digest, name)
    _require_uint64(before_snapshot_version, "before_snapshot_version")
    _require_uint64(after_snapshot_version, "after_snapshot_version")
    if type(action) is not LedgerHandoffAction:
        raise _fail(OutcomeCode.INVALID_TYPE, "ledger handoff action must be exact")
    if failure is not None and type(failure) is not LedgerHandoffFailure:
        raise _fail(OutcomeCode.INVALID_TYPE, "ledger handoff failure must be exact or None")
    if type(requires_reconciliation) is not bool or type(halt_requested) is not bool:
        raise _fail(OutcomeCode.INVALID_TYPE, "ledger handoff flags must be exact bool")
    if requires_reconciliation and not halt_requested:
        raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation handoff must request a halt")
    _require_optional_fill_pair(fill_id, fill_sha256, run_id)
    apply_bytes, apply_document = _require_optional_apply_outcome(
        original_ledger_apply_outcome,
        original_ledger_apply_outcome_sha256,
    )
    unchanged = (
        before_snapshot_version == after_snapshot_version
        and before_snapshot_sha256 == after_snapshot_sha256
    )
    if action is LedgerHandoffAction.NOT_APPLICABLE:
        if fill_id is not None or apply_bytes is not None or not unchanged or failure is not None:
            raise _fail(OutcomeCode.CONFLICTING_ID, "not-applicable handoff fields conflict")
    elif action is LedgerHandoffAction.EFFECT_COMMITTED:
        if (
            fill_id is None
            or fill_sha256 is None
            or apply_bytes is None
            or apply_document is None
            or after_snapshot_version != before_snapshot_version + 1
            or failure is not None
            or apply_document["code"] != OutcomeCode.LEDGER_APPLIED.value
            or apply_document["before_snapshot_version"] != before_snapshot_version
            or apply_document["after_snapshot_version"] != after_snapshot_version
            or apply_document["submitted_fill_id"] != _economic_id_document(fill_id)
            or apply_document["submitted_fill_sha256"] != fill_sha256.value
            or apply_document["run_id"] != run_id.value
            or apply_document["snapshot_sha256"] != after_snapshot_sha256.value
            or apply_document["transaction_entry_id"] is None
            or apply_document["transaction_sha256"] is None
            or apply_document["conflict_kind"] is not None
            or apply_document["entry_index_binding"] is not None
            or apply_document["fact_index_binding"] is not None
            or apply_document["fill_index_binding"] is not None
            or apply_document["failure_stage"] is not None
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "committed handoff fields conflict")
    elif apply_bytes is not None or not unchanged or failure is None or not halt_requested:
        raise _fail(OutcomeCode.CONFLICTING_ID, "failed handoff fields conflict")

    value = object.__new__(LedgerHandoffOutcome)
    fields = locals().copy()
    fields["original_ledger_apply_outcome"] = apply_bytes
    fields.pop("value", None)
    fields.pop("apply_bytes", None)
    fields.pop("apply_document", None)
    fields.pop("unchanged", None)
    for name in LedgerHandoffOutcome.__slots__:
        if name != "_seal":
            object.__setattr__(value, name, fields[name])
    object.__setattr__(value, "_seal", _VALUE_SEAL)
    canonical_ledger_handoff_outcome_bytes(value)
    return value


def canonical_ledger_application_command_bytes(command: LedgerApplicationCommand) -> bytes:
    if type(command) is not LedgerApplicationCommand or command._seal is not _VALUE_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "ledger application command must be factory-issued")
    payload = _canonical_json(
        {
            "audited_handoff_sha256": command.audited_handoff_sha256.value,
            "canonicalization": LEDGER_INTEGRATION_CANONICALIZATION,
            "dispatch_sequence": command.dispatch_sequence,
            "fill_id": _economic_id_document(command.fill_id),
            "fill_sha256": command.fill_sha256.value,
            "processing_outcome_sha256": command.processing_outcome_sha256.value,
            "requires_reconciliation": command.requires_reconciliation,
            "run_id": command.run_id.value,
            "schema": LEDGER_APPLICATION_COMMAND_SCHEMA,
        }
    )
    if len(payload) > MAX_LEDGER_INTEGRATION_PAYLOAD_BYTES:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "ledger application command exceeds its byte bound")
    return payload


def ledger_application_command_digest(command: LedgerApplicationCommand) -> Sha256Digest:
    return _framed_digest(
        LEDGER_APPLICATION_COMMAND_DIGEST_DOMAIN,
        canonical_ledger_application_command_bytes(command),
    )


def canonical_ledger_handoff_outcome_bytes(outcome: LedgerHandoffOutcome) -> bytes:
    if type(outcome) is not LedgerHandoffOutcome or outcome._seal is not _VALUE_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "ledger handoff outcome must be factory-issued")
    original = (
        None
        if outcome.original_ledger_apply_outcome is None
        else _decode_canonical_json(
            outcome.original_ledger_apply_outcome,
            "original ledger outcome",
        )
    )
    payload = _canonical_json(
        {
            "action": outcome.action.value,
            "after_snapshot_sha256": outcome.after_snapshot_sha256.value,
            "after_snapshot_version": outcome.after_snapshot_version,
            "audited_handoff_sha256": outcome.audited_handoff_sha256.value,
            "before_snapshot_sha256": outcome.before_snapshot_sha256.value,
            "before_snapshot_version": outcome.before_snapshot_version,
            "canonicalization": LEDGER_INTEGRATION_CANONICALIZATION,
            "dispatch_sequence": outcome.dispatch_sequence,
            "failure": None if outcome.failure is None else outcome.failure.value,
            "fill_id": _economic_id_document(outcome.fill_id),
            "fill_sha256": None if outcome.fill_sha256 is None else outcome.fill_sha256.value,
            "halt_requested": outcome.halt_requested,
            "ingress_identity": _ingress_identity_document(outcome.ingress_identity),
            "original_ledger_apply_outcome": original,
            "original_ledger_apply_outcome_sha256": (
                None
                if outcome.original_ledger_apply_outcome_sha256 is None
                else outcome.original_ledger_apply_outcome_sha256.value
            ),
            "processing_outcome_ack_sha256": outcome.processing_outcome_ack_sha256.value,
            "processing_outcome_sha256": outcome.processing_outcome_sha256.value,
            "requires_reconciliation": outcome.requires_reconciliation,
            "run_id": outcome.run_id.value,
            "schema": LEDGER_HANDOFF_OUTCOME_SCHEMA,
        }
    )
    if len(payload) > MAX_LEDGER_INTEGRATION_PAYLOAD_BYTES:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "ledger handoff outcome exceeds its byte bound")
    return payload


def ledger_handoff_outcome_digest(outcome: LedgerHandoffOutcome) -> Sha256Digest:
    return _framed_digest(
        LEDGER_HANDOFF_OUTCOME_DIGEST_DOMAIN,
        canonical_ledger_handoff_outcome_bytes(outcome),
    )


def decode_ledger_handoff_outcome(canonical_payload: bytes) -> LedgerHandoffOutcome:
    """Strictly decode and re-encode one canonical handoff outcome."""
    document = _decode_canonical_json(canonical_payload, "ledger handoff outcome")
    if type(document) is not dict or set(document) != {
        "action",
        "after_snapshot_sha256",
        "after_snapshot_version",
        "audited_handoff_sha256",
        "before_snapshot_sha256",
        "before_snapshot_version",
        "canonicalization",
        "dispatch_sequence",
        "failure",
        "fill_id",
        "fill_sha256",
        "halt_requested",
        "ingress_identity",
        "original_ledger_apply_outcome",
        "original_ledger_apply_outcome_sha256",
        "processing_outcome_ack_sha256",
        "processing_outcome_sha256",
        "requires_reconciliation",
        "run_id",
        "schema",
    }:
        raise _fail(OutcomeCode.CONFLICTING_ID, "ledger handoff outcome fields conflict")
    if (
        document["schema"] != LEDGER_HANDOFF_OUTCOME_SCHEMA
        or document["canonicalization"] != LEDGER_INTEGRATION_CANONICALIZATION
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "ledger handoff outcome schema conflicts")
    original_document = document["original_ledger_apply_outcome"]
    original_bytes = None if original_document is None else _canonical_json(original_document)
    try:
        outcome = create_ledger_handoff_outcome(
            run_id=RunId(_require_text(document, "run_id")),
            dispatch_sequence=_require_json_int(document, "dispatch_sequence"),
            ingress_identity=_decode_ingress_identity(document["ingress_identity"]),
            audited_handoff_sha256=Sha256Digest(_require_text(document, "audited_handoff_sha256")),
            processing_outcome_sha256=Sha256Digest(
                _require_text(document, "processing_outcome_sha256")
            ),
            processing_outcome_ack_sha256=Sha256Digest(
                _require_text(document, "processing_outcome_ack_sha256")
            ),
            fill_id=_decode_optional_economic_id(document["fill_id"]),
            fill_sha256=_decode_optional_digest(document["fill_sha256"]),
            action=LedgerHandoffAction(_require_text(document, "action")),
            original_ledger_apply_outcome=original_bytes,
            original_ledger_apply_outcome_sha256=_decode_optional_digest(
                document["original_ledger_apply_outcome_sha256"]
            ),
            before_snapshot_version=_require_json_int(document, "before_snapshot_version"),
            before_snapshot_sha256=Sha256Digest(_require_text(document, "before_snapshot_sha256")),
            after_snapshot_version=_require_json_int(document, "after_snapshot_version"),
            after_snapshot_sha256=Sha256Digest(_require_text(document, "after_snapshot_sha256")),
            requires_reconciliation=_require_json_bool(document, "requires_reconciliation"),
            halt_requested=_require_json_bool(document, "halt_requested"),
            failure=(
                None
                if document["failure"] is None
                else LedgerHandoffFailure(_require_text(document, "failure"))
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, LedgerIntegrationError):
            raise
        raise _fail(OutcomeCode.CONFLICTING_ID, "ledger handoff outcome values conflict") from error
    if canonical_ledger_handoff_outcome_bytes(outcome) != canonical_payload:
        raise _fail(OutcomeCode.CONFLICTING_ID, "ledger handoff outcome round-trip conflicts")
    return outcome


def _decode_ledger_application_command(canonical_payload: bytes) -> LedgerApplicationCommand:
    document = _decode_canonical_json(canonical_payload, "ledger application command")
    if type(document) is not dict or set(document) != {
        "audited_handoff_sha256",
        "canonicalization",
        "dispatch_sequence",
        "fill_id",
        "fill_sha256",
        "processing_outcome_sha256",
        "requires_reconciliation",
        "run_id",
        "schema",
    }:
        raise _fail(OutcomeCode.CONFLICTING_ID, "ledger application command fields conflict")
    if (
        document["schema"] != LEDGER_APPLICATION_COMMAND_SCHEMA
        or document["canonicalization"] != LEDGER_INTEGRATION_CANONICALIZATION
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "ledger application command schema conflicts")
    try:
        fill_id = _decode_optional_economic_id(document["fill_id"])
        if fill_id is None:
            raise _fail(OutcomeCode.CONFLICTING_ID, "ledger application command fill is missing")
        command = _create_ledger_application_command(
            run_id=RunId(_require_text(document, "run_id")),
            dispatch_sequence=_require_json_int(document, "dispatch_sequence"),
            audited_handoff_sha256=Sha256Digest(_require_text(document, "audited_handoff_sha256")),
            processing_outcome_sha256=Sha256Digest(
                _require_text(document, "processing_outcome_sha256")
            ),
            fill_id=fill_id,
            fill_sha256=Sha256Digest(_require_text(document, "fill_sha256")),
            requires_reconciliation=_require_json_bool(document, "requires_reconciliation"),
        )
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, LedgerIntegrationError):
            raise
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "ledger application command values conflict",
        ) from error
    if canonical_ledger_application_command_bytes(command) != canonical_payload:
        raise _fail(OutcomeCode.CONFLICTING_ID, "ledger application command round-trip conflicts")
    return command


def _require_optional_apply_outcome(
    payload: bytes | None,
    digest: Sha256Digest | None,
) -> tuple[bytes | None, dict[str, object] | None]:
    if (payload is None) != (digest is None):
        raise _fail(OutcomeCode.CONFLICTING_ID, "original ledger outcome pair is incomplete")
    if payload is None:
        return None, None
    if type(payload) is not bytes or type(digest) is not Sha256Digest:
        raise _fail(OutcomeCode.INVALID_TYPE, "original ledger outcome pair must be exact")
    document = _decode_canonical_json(payload, "original ledger outcome")
    if type(document) is not dict or set(document) != {
        "after_snapshot_version",
        "before_snapshot_version",
        "canonicalization",
        "code",
        "conflict_kind",
        "entry_index_binding",
        "fact_index_binding",
        "failure_stage",
        "fill_index_binding",
        "message_type",
        "run_id",
        "schema_version",
        "snapshot_sha256",
        "submitted_fill_id",
        "submitted_fill_sha256",
        "transaction_entry_id",
        "transaction_sha256",
    }:
        raise _fail(OutcomeCode.CONFLICTING_ID, "original ledger outcome fields conflict")
    if (
        document["canonicalization"] != "ea-ledger-apply-outcome-v1"
        or document["message_type"] != "ledger_apply_outcome"
        or document["schema_version"] != 1
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "original ledger outcome schema conflicts")
    for field in ("before_snapshot_version", "after_snapshot_version"):
        _require_json_int(document, field)
    for field in ("run_id", "code", "snapshot_sha256", "submitted_fill_sha256"):
        _require_text(document, field)
    try:
        RunId(document["run_id"])
        OutcomeCode(document["code"])
        Sha256Digest(document["snapshot_sha256"])
        Sha256Digest(document["submitted_fill_sha256"])
        submitted_fill_id = _decode_optional_economic_id(document["submitted_fill_id"])
        transaction_entry_id = _decode_optional_economic_id(document["transaction_entry_id"])
        if submitted_fill_id is None:
            raise ValueError("submitted Fill identity is missing")
        if transaction_entry_id is None:
            raise ValueError("transaction entry identity is missing")
        if (
            transaction_entry_id.owner_kind is not EconomicOwnerKind.LEDGER_ENTRY
            or transaction_entry_id.run_id != submitted_fill_id.run_id
        ):
            raise ValueError("transaction entry binding conflicts")
        if _decode_optional_digest(document["transaction_sha256"]) is None:
            raise ValueError("transaction digest is missing")
    except (TypeError, ValueError) as error:
        if isinstance(error, LedgerIntegrationError):
            raise
        raise _fail(
            OutcomeCode.CONFLICTING_ID, "original ledger outcome values conflict"
        ) from error
    expected = Sha256Digest(sha256(LEDGER_APPLY_OUTCOME_DIGEST_DOMAIN + payload).hexdigest())
    if expected != digest:
        raise _fail(OutcomeCode.CONFLICTING_ID, "original ledger outcome digest conflicts")
    return payload, document


def _require_optional_fill_pair(
    fill_id: EconomicId | None,
    fill_sha256: Sha256Digest | None,
    run_id: RunId,
) -> None:
    if (fill_id is None) != (fill_sha256 is None):
        raise _fail(OutcomeCode.CONFLICTING_ID, "fill identity and digest must be paired")
    if fill_id is not None:
        _require_fill_id(fill_id, run_id)
        _require_digest(fill_sha256, "fill_sha256")


def _require_fill_id(value: object, run_id: RunId) -> EconomicId:
    if type(value) is not EconomicId:
        raise _fail(OutcomeCode.INVALID_TYPE, "fill_id must be an exact EconomicId")
    if value.owner_kind is not EconomicOwnerKind.EXECUTION_FILL or value.run_id != run_id:
        raise _fail(OutcomeCode.CONFLICTING_ID, "fill identity binding conflicts")
    return value


def _require_run_id(value: object) -> RunId:
    if type(value) is not RunId:
        raise _fail(OutcomeCode.INVALID_TYPE, "run_id must be exact")
    return value


def _require_digest(value: object, field: str) -> Sha256Digest:
    if type(value) is not Sha256Digest:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be an exact Sha256Digest")
    return value


def _require_uint64(value: object, field: str) -> int:
    if type(value) is not int:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be exact int")
    if not 0 <= value <= _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field} is outside uint64")
    return value


def _require_positive_uint64(value: object, field: str) -> int:
    result = _require_uint64(value, field)
    if result == 0:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field} must be positive")
    return result


def _economic_id_document(value: EconomicId | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "owner_kind": value.owner_kind.value,
        "owner_sequence": value.owner_sequence,
        "run_id": value.run_id.value,
    }


def _ingress_identity_document(value: IngressIdentity) -> dict[str, object]:
    return {
        "ingress_sequence": value.ingress_sequence,
        "source_namespace": value.source_namespace.value,
    }


def _decode_optional_economic_id(document: object) -> EconomicId | None:
    if document is None:
        return None
    if type(document) is not dict or set(document) != {
        "owner_kind",
        "owner_sequence",
        "run_id",
    }:
        raise _fail(OutcomeCode.CONFLICTING_ID, "economic identity document conflicts")
    return EconomicId(
        RunId(_require_text(document, "run_id")),
        EconomicOwnerKind(_require_text(document, "owner_kind")),
        _require_json_int(document, "owner_sequence"),
    )


def _decode_ingress_identity(document: object) -> IngressIdentity:
    if type(document) is not dict or set(document) != {
        "ingress_sequence",
        "source_namespace",
    }:
        raise _fail(OutcomeCode.CONFLICTING_ID, "ingress identity document conflicts")
    return IngressIdentity(
        SourceNamespace(_require_text(document, "source_namespace")),
        _require_json_int(document, "ingress_sequence"),
    )


def _decode_optional_digest(value: object) -> Sha256Digest | None:
    if value is None:
        return None
    if type(value) is not str:
        raise _fail(OutcomeCode.CONFLICTING_ID, "optional digest must be text or null")
    return Sha256Digest(value)


def _require_text(document: dict[str, Any], field: str) -> str:
    value = document[field]
    if type(value) is not str:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} must be exact JSON text")
    return value


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


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _decode_canonical_json(payload: bytes, field: str) -> object:
    if type(payload) is not bytes:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} payload must be exact bytes")
    if not 1 <= len(payload) <= MAX_LEDGER_INTEGRATION_PAYLOAD_BYTES:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field} payload exceeds its byte bound")
    try:
        document = json.loads(payload, object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} payload is invalid JSON") from error
    _require_json_value(document)
    if _canonical_json(document) != payload:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} payload is not canonical JSON")
    return document


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
    raise _fail(OutcomeCode.CONFLICTING_ID, "JSON value type is outside the canonical contract")


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
        raise _fail(OutcomeCode.CONFLICTING_ID, "ledger integration document is invalid") from error


def _framed_digest(domain: bytes, payload: bytes) -> Sha256Digest:
    return Sha256Digest(sha256(domain + len(payload).to_bytes(8, "big") + payload).hexdigest())
