"""Dependency-neutral durable audit records from Accepted ADR 0020."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Protocol, final

from ea.core.execution_identity import EconomicId, EconomicOwnerKind
from ea.core.execution_state import EXECUTION_FACT_PROCESSING_OUTCOME_DIGEST_DOMAIN
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunBinding, RunContractError, Sha256Digest

AUDIT_RECORD_HEADER_SCHEMA = "ea.audit-record-header.v1"
AUDIT_ACKNOWLEDGEMENT_SCHEMA = "ea.audit-append-acknowledgement.v1"
AUDIT_CANONICALIZATION = "ea-canonical-json-v1"
AUDIT_RECORD_DIGEST_DOMAIN = b"ea.audit-record.v1\0"
AUDIT_CHAIN_DIGEST_DOMAIN = b"ea.audit-chain.v1\0"
AUDIT_ACKNOWLEDGEMENT_DIGEST_DOMAIN = b"ea.audit-append-acknowledgement.v1\0"
AUDIT_FRAME_DIGEST_DOMAIN = b"ea.audit-frame.v1\0"
EMPTY_RECORD_SHA256 = Sha256Digest(
    "4af7f9585d80e83ffefb82fd9993e42a0e5bcca05a957c2d66f7e6ec5af602cd"
)
EMPTY_CHAIN_HEAD_SHA256 = Sha256Digest(
    "daf430a5dce8e5d21acb79e2c4aa92b0da9f42108d96847e3da260cdbf271b75"
)
MAX_AUDIT_RECORDS = 600_006
MAX_AUDIT_HEADER_BYTES = 4_096
MAX_SMALL_AUDIT_PAYLOAD_BYTES = 4_096
MAX_LARGE_AUDIT_PAYLOAD_BYTES = 16_384

_MAX_UINT64 = (1 << 64) - 1
_RECORD_SEAL = object()
_ACK_SEAL = object()
_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.CONFLICTING_ID,
        OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
        OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
    }
)


class AuditContractError(ValueError):
    """Closed validation or durability failure at the audit boundary."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _ERROR_CODES:
            raise TypeError("audit errors require an exact supported OutcomeCode")
        self.code = code
        super().__init__(message)


def _fail(code: OutcomeCode, message: str) -> AuditContractError:
    return AuditContractError(code, message)


class AuditRecordKind(StrEnum):
    """Closed version-one journal record kinds in canonical rank order."""

    RUN_PREPARED = "run.prepared"
    MATCHER_DISPATCH_BATCH = "matcher.dispatch_batch"
    EXECUTION_FACT_PROCESSING_OUTCOME = "execution.fact_processing_outcome"
    PORTFOLIO_LEDGER_HANDOFF_OUTCOME = "portfolio.ledger_handoff_outcome"
    RISK_PORTFOLIO_REFRESH = "risk.portfolio_refresh"
    RECONCILIATION_OBSERVATION_OUTCOME = "reconciliation.observation_outcome"
    RECONCILIATION_ADJUSTMENT_AUTHORIZATION = "reconciliation.adjustment_authorization"
    SUBMISSION_PRE_EFFECT_AUTHORIZATION = "submission.pre_effect_authorization"
    RUNTIME_FAILING_SAFETY_TRANSITION = "runtime.failing_safety_transition"
    RUNTIME_DISPATCH_COMPLETED = "runtime.dispatch_completed"
    RUN_TERMINAL = "run.terminal"


class AuditSubjectKind(StrEnum):
    """Closed version-one subjects bound by audit records."""

    RUN_MANIFEST = "run_manifest"
    HISTORICAL_MATCHER_DISPATCH_BATCH = "historical_matcher_dispatch_batch"
    EXECUTION_FACT_PROCESSING_OUTCOME = "execution_fact_processing_outcome"
    PORTFOLIO_LEDGER_HANDOFF_OUTCOME = "portfolio_ledger_handoff_outcome"
    PORTFOLIO_RISK_REFRESH = "portfolio_risk_refresh"
    RECONCILIATION_OUTCOME = "reconciliation_outcome"
    RECONCILIATION_ADJUSTMENT_AUTHORIZATION = "reconciliation_adjustment_authorization"
    HISTORICAL_EXECUTION_REQUEST = "historical_execution_request"
    COORDINATOR_STATE = "coordinator_state"
    RUNTIME_DISPATCH = "runtime_dispatch"
    RUN_TERMINAL_STATE = "run_terminal_state"


AUDIT_SUBJECT_BY_RECORD_KIND: dict[AuditRecordKind, AuditSubjectKind] = {
    AuditRecordKind.RUN_PREPARED: AuditSubjectKind.RUN_MANIFEST,
    AuditRecordKind.MATCHER_DISPATCH_BATCH: AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
    AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME: (
        AuditSubjectKind.EXECUTION_FACT_PROCESSING_OUTCOME
    ),
    AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME: (
        AuditSubjectKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME
    ),
    AuditRecordKind.RISK_PORTFOLIO_REFRESH: AuditSubjectKind.PORTFOLIO_RISK_REFRESH,
    AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME: AuditSubjectKind.RECONCILIATION_OUTCOME,
    AuditRecordKind.RECONCILIATION_ADJUSTMENT_AUTHORIZATION: (
        AuditSubjectKind.RECONCILIATION_ADJUSTMENT_AUTHORIZATION
    ),
    AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION: (
        AuditSubjectKind.HISTORICAL_EXECUTION_REQUEST
    ),
    AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION: AuditSubjectKind.COORDINATOR_STATE,
    AuditRecordKind.RUNTIME_DISPATCH_COMPLETED: AuditSubjectKind.RUNTIME_DISPATCH,
    AuditRecordKind.RUN_TERMINAL: AuditSubjectKind.RUN_TERMINAL_STATE,
}

_LARGE_PAYLOAD_KINDS = frozenset(
    {
        AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME,
        AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
        AuditRecordKind.RISK_PORTFOLIO_REFRESH,
        AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME,
        AuditRecordKind.RECONCILIATION_ADJUSTMENT_AUTHORIZATION,
        AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION,
    }
)

_AUDIT_PAYLOAD_SCHEMA_BY_KIND: dict[AuditRecordKind, str] = {
    AuditRecordKind.RUN_PREPARED: "ea.audit-run-prepared.v1",
    AuditRecordKind.MATCHER_DISPATCH_BATCH: "ea.audit-matcher-dispatch-batch.v1",
    AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME: "ea.ledger-handoff-outcome.v1",
    AuditRecordKind.RISK_PORTFOLIO_REFRESH: "ea.portfolio-risk-refresh.v1",
    AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME: "ea.reconciliation-outcome.v2",
    AuditRecordKind.RECONCILIATION_ADJUSTMENT_AUTHORIZATION: (
        "ea.reconciliation-adjustment-authorization.v1"
    ),
    AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION: ("ea.audit-submission-authorization.v1"),
    AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION: "ea.audit-failing-safety.v1",
    AuditRecordKind.RUNTIME_DISPATCH_COMPLETED: "ea.audit-dispatch-completed.v2",
    AuditRecordKind.RUN_TERMINAL: "ea.audit-run-terminal.v1",
}
_AUDIT_PAYLOAD_FIELDS_BY_KIND: dict[AuditRecordKind, frozenset[str]] = {
    AuditRecordKind.RUN_PREPARED: frozenset(
        {"schema", "canonicalization", "run_id", "lineage_sha256", "manifest_sha256"}
    ),
    AuditRecordKind.MATCHER_DISPATCH_BATCH: frozenset(
        {
            "schema",
            "canonicalization",
            "run_id",
            "dispatch_kind",
            "dispatch_sequence",
            "trigger_root_key",
            "trigger_root_sha256",
            "batch_sha256",
            "ingress_count",
            "ordered_ingress_sha256s_sha256",
        }
    ),
    AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME: frozenset(
        {
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
        }
    ),
    AuditRecordKind.RISK_PORTFOLIO_REFRESH: frozenset(
        {
            "canonicalization",
            "dispatch_sequence",
            "exposure_sha256",
            "ordered_ledger_ack_frontier_sha256",
            "policy_id",
            "policy_sha256",
            "portfolio_snapshot_sha256",
            "portfolio_snapshot_version",
            "previous_refresh_sha256",
            "refresh_sequence",
            "risk_state_sha256",
            "risk_state_version",
            "run_id",
            "schema",
            "submission_permitted",
        }
    ),
    AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME: frozenset(
        {
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
    ),
    AuditRecordKind.RECONCILIATION_ADJUSTMENT_AUTHORIZATION: frozenset(
        {
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
    ),
    AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION: frozenset(
        {
            "schema",
            "canonicalization",
            "run_id",
            "order_id",
            "order_sha256",
            "execution_request_sha256",
            "causal_market_sha256",
            "causal_root_key",
            "dispatch_sequence",
            "portfolio_snapshot_version",
            "risk_state_version",
            "global_halt_epoch",
            "risk_halt_epoch",
            "held_for_order_id",
            "instrument_gate_id",
            "instrument_gate_version",
            "authorization_state_version",
            "instrument_spec_set_id",
            "instrument_spec_set_sha256",
            "execution_policy_id",
            "execution_policy_sha256",
        }
    ),
    AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION: frozenset(
        {
            "schema",
            "canonicalization",
            "run_id",
            "previous_state_sha256",
            "failing_state_sha256",
            "failure_code",
            "failed_record_kind",
            "failed_subject_kind",
            "failed_subject_sha256",
            "dispatch_sequence",
            "trigger_root_sha256",
        }
    ),
    AuditRecordKind.RUNTIME_DISPATCH_COMPLETED: frozenset(
        {
            "schema",
            "canonicalization",
            "run_id",
            "dispatch_kind",
            "dispatch_sequence",
            "trigger_root_key",
            "trigger_root_sha256",
            "batch_sha256",
            "outcome_count",
            "ordered_outcome_ack_sha256s_sha256",
            "pre_ack_state_sha256",
            "authorization_attempt_count",
            "authorization_attempt_outcome",
            "authorization_attempt_outcome_sha256",
            "submission_count",
            "ordered_submission_receipt_sha256s_sha256",
        }
    ),
    AuditRecordKind.RUN_TERMINAL: frozenset(
        {
            "schema",
            "canonicalization",
            "run_id",
            "terminal_kind",
            "last_dispatch_sequence",
            "last_trigger_root_sha256",
            "pre_terminal_state_sha256",
            "previous_chain_head_sha256",
        }
    ),
}
_SUBJECT_DOMAIN_BY_KIND: dict[AuditRecordKind, bytes] = {
    AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME: (
        b"ea.audit-subject.portfolio.ledger_handoff_outcome.v1\0"
    ),
    AuditRecordKind.RISK_PORTFOLIO_REFRESH: (b"ea.audit-subject.risk.portfolio_refresh.v1\0"),
    AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME: (
        b"ea.audit-subject.reconciliation.observation_outcome.v1\0"
    ),
    AuditRecordKind.RECONCILIATION_ADJUSTMENT_AUTHORIZATION: (
        b"ea.audit-subject.reconciliation.adjustment_authorization.v1\0"
    ),
    AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION: (
        b"ea.audit-subject.submission-authorization.v1\0"
    ),
    AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION: (b"ea.audit-subject.failing-safety.v1\0"),
    AuditRecordKind.RUNTIME_DISPATCH_COMPLETED: (b"ea.audit-subject.dispatch-completed.v1\0"),
    AuditRecordKind.RUN_TERMINAL: b"ea.audit-subject.run-terminal.v1\0",
}


@final
@dataclass(frozen=True, slots=True)
class AuditLogicalKey:
    """Stable retry key which intentionally excludes journal sequence and payload."""

    record_kind: AuditRecordKind
    subject_kind: AuditSubjectKind
    subject_sha256: Sha256Digest

    def __post_init__(self) -> None:
        _require_kind_subject(self.record_kind, self.subject_kind)
        if type(self.subject_sha256) is not Sha256Digest:
            raise _fail(OutcomeCode.INVALID_TYPE, "subject_sha256 must be an exact Sha256Digest")


@final
@dataclass(frozen=True, slots=True, init=False)
class AuditRecord:
    """Factory-only immutable record with raw canonical payload stored separately."""

    binding: RunBinding
    record_id: EconomicId
    record_kind: AuditRecordKind
    subject_kind: AuditSubjectKind
    subject_sha256: Sha256Digest
    canonical_payload: bytes
    payload_sha256: Sha256Digest
    previous_record_sha256: Sha256Digest
    previous_chain_head_sha256: Sha256Digest
    _seal: object

    def __init__(self) -> None:
        raise TypeError("AuditRecord values are created only by create_audit_record")

    @property
    def logical_key(self) -> AuditLogicalKey:
        return AuditLogicalKey(self.record_kind, self.subject_kind, self.subject_sha256)


@final
@dataclass(frozen=True, slots=True, init=False, weakref_slot=True)
class AuditAppendAcknowledgement:
    """Factory-only proof reconstructed from an independently read-back frame."""

    binding: RunBinding
    record_id: EconomicId
    record_kind: AuditRecordKind
    subject_kind: AuditSubjectKind
    subject_sha256: Sha256Digest
    payload_sha256: Sha256Digest
    record_sha256: Sha256Digest
    chain_head_sha256: Sha256Digest
    _seal: object

    def __init__(self) -> None:
        raise TypeError(
            "AuditAppendAcknowledgement values are created only from verified audit records"
        )


class AuditRecoveryRecordSource(Protocol):
    """Repeatable bounded-memory view over one verified journal prefix."""

    @property
    def binding(self) -> RunBinding: ...

    @property
    def record_count(self) -> int: ...

    def __iter__(self) -> Iterator[AuditRecord]: ...

    def prefix(self, record_count: int) -> AuditRecoveryRecordSource: ...

    def record_at(self, index: int) -> AuditRecord: ...

    def resolve_record(self, logical_key: AuditLogicalKey) -> AuditRecord | None: ...


class AuditAppendPort(Protocol):
    """Consumer-owned typed durability boundary."""

    @property
    def binding(self) -> RunBinding: ...

    def append(
        self,
        *,
        record_kind: AuditRecordKind,
        subject_kind: AuditSubjectKind,
        subject_sha256: Sha256Digest,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement: ...

    def settle_append(
        self,
        *,
        logical_key: AuditLogicalKey,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement | None:
        """Authoritatively settle one uncertain exact append as present or absent."""
        ...


def _canonical_json(document: object) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_run_prepared_audit_payload(binding: RunBinding) -> bytes:
    """Project the mandatory first audit payload from its exact durable binding."""
    if type(binding) is not RunBinding:
        raise _fail(OutcomeCode.INVALID_TYPE, "binding must be an exact RunBinding")
    return _canonical_json(
        {
            "canonicalization": AUDIT_CANONICALIZATION,
            "lineage_sha256": binding.reference.lineage_sha256.value,
            "manifest_sha256": binding.manifest_sha256.value,
            "run_id": binding.reference.run_id.value,
            "schema": "ea.audit-run-prepared.v1",
        }
    )


def _economic_id_document(identity: EconomicId) -> dict[str, object]:
    return {
        "owner_kind": identity.owner_kind.value,
        "owner_sequence": identity.owner_sequence,
        "run_id": identity.run_id.value,
    }


def _require_kind_subject(
    record_kind: object,
    subject_kind: object,
) -> tuple[AuditRecordKind, AuditSubjectKind]:
    if type(record_kind) is not AuditRecordKind or type(subject_kind) is not AuditSubjectKind:
        raise _fail(OutcomeCode.INVALID_TYPE, "audit kinds require exact enum carriers")
    if AUDIT_SUBJECT_BY_RECORD_KIND[record_kind] is not subject_kind:
        raise _fail(OutcomeCode.CONFLICTING_ID, "record kind and subject kind conflict")
    return record_kind, subject_kind


def _require_json_text(document: dict[str, object], field: str) -> str:
    value = document[field]
    if type(value) is not str:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} must be one JSON string")
    return value


def _require_json_digest(document: dict[str, object], field: str) -> None:
    try:
        Sha256Digest(_require_json_text(document, field))
    except RunContractError as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} is not one SHA-256 digest") from error


def _require_json_uint64(
    document: dict[str, object],
    field: str,
    *,
    positive: bool,
) -> int:
    value = document[field]
    lower = 1 if positive else 0
    if type(value) is not int or not lower <= value <= _MAX_UINT64:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} is outside its uint64 domain")
    return value


def _require_json_bool(document: dict[str, object], field: str) -> None:
    if type(document[field]) is not bool:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} must be one JSON boolean")


def _require_json_id(
    document: dict[str, object],
    field: str,
    *,
    expected_owner: EconomicOwnerKind | None = None,
    positive: bool = False,
) -> None:
    value = document[field]
    if type(value) is not dict or set(value) != {"owner_kind", "owner_sequence", "run_id"}:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} must be one canonical economic ID")
    if (
        type(value["owner_kind"]) is not str
        or value["owner_kind"] not in {owner.value for owner in EconomicOwnerKind}
        or type(value["run_id"]) is not str
        or value["run_id"] != document.get("run_id")
        or type(value["owner_sequence"]) is not int
        or not (1 if positive else 0) <= value["owner_sequence"] <= _MAX_UINT64
        or (expected_owner is not None and value["owner_kind"] != expected_owner.value)
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} economic ID carriers are invalid")


def _require_json_root_key(
    document: dict[str, object],
    field: str,
    *,
    expected_domain: str | None,
) -> None:
    from ea.core.historical_matching import (
        runtime_root_key_from_document,
        runtime_root_order_key_document,
    )

    value = document[field]
    if type(value) is not dict:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} must be one JSON object")
    try:
        key = runtime_root_key_from_document(value)
        if runtime_root_order_key_document(key) != value:
            raise ValueError("root key round-trip changed")
    except (TypeError, ValueError) as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} is not one canonical root key") from error
    if expected_domain is not None and value.get("root_domain") != expected_domain:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} domain conflicts with its record")


def _require_audit_owned_payload_values(
    record_kind: AuditRecordKind,
    document: dict[str, object],
) -> None:
    _require_json_text(document, "run_id")
    if record_kind is AuditRecordKind.RUN_PREPARED:
        _require_json_digest(document, "lineage_sha256")
        _require_json_digest(document, "manifest_sha256")
    elif record_kind is AuditRecordKind.MATCHER_DISPATCH_BATCH:
        if _require_json_text(document, "dispatch_kind") not in {"market", "end_of_run"}:
            raise _fail(OutcomeCode.CONFLICTING_ID, "dispatch_kind is outside its closed enum")
        _require_json_uint64(document, "dispatch_sequence", positive=True)
        expected_domain = "market_data" if document["dispatch_kind"] == "market" else "end_of_run"
        _require_json_root_key(
            document,
            "trigger_root_key",
            expected_domain=expected_domain,
        )
        for field in ("trigger_root_sha256", "batch_sha256", "ordered_ingress_sha256s_sha256"):
            _require_json_digest(document, field)
        _require_json_uint64(document, "ingress_count", positive=False)
    elif record_kind is AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME:
        from ea.core.reconciliation import (
            ReconciliationRequestedAction,
            ReconciliationWatermarkComparison,
        )

        for field in (
            "observation_sha256",
            "local_snapshot_sha256",
        ):
            _require_json_digest(document, field)
        _require_json_uint64(document, "dispatch_sequence", positive=True)
        _require_json_uint64(document, "local_snapshot_version", positive=False)
        _require_json_uint64(document, "ledger_sequence", positive=False)
        _require_json_bool(document, "halt_requested")
        try:
            OutcomeCode(_require_json_text(document, "outcome_code"))
            ReconciliationRequestedAction(_require_json_text(document, "requested_action"))
            ReconciliationWatermarkComparison(_require_json_text(document, "watermark_comparison"))
        except ValueError as error:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "reconciliation outcome enum conflicts",
            ) from error
        discrepancies = document["discrepancies"]
        if type(discrepancies) is not list or len(discrepancies) > 32:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "reconciliation discrepancies exceed their closed array contract",
            )
        discrepancy_keys = tuple(
            _require_reconciliation_discrepancy_document(discrepancy)
            for discrepancy in discrepancies
        )
        if discrepancy_keys != tuple(sorted(set(discrepancy_keys))):
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "reconciliation discrepancies are not canonical",
            )
        matrix = (
            document["watermark_comparison"],
            document["outcome_code"],
            document["requested_action"],
            document["halt_requested"],
            len(discrepancies),
        )
        allowed = {
            ("equal", "reconciliation.match", "none", False, 0),
            ("equal", "reconciliation.match", "propose_ancestry_resolution", True, 0),
            (
                "remote_lower",
                "reconciliation.local_ahead_stale",
                "retain_and_halt",
                True,
                0,
            ),
            (
                "remote_higher",
                "reconciliation.remote_ahead",
                "request_missing_trade_facts",
                True,
                0,
            ),
            (
                "equal",
                "reconciliation.mismatch",
                "propose_single_target_adjustment",
                True,
                1,
            ),
            ("incomparable", "reconciliation.invalid", "retain_and_halt", True, 0),
            (
                "equal",
                "reconciliation.unresolved_correlation",
                "retain_and_halt",
                True,
                0,
            ),
        }
        quarantined = (
            matrix[:4]
            == (
                "equal",
                "reconciliation.quarantined",
                "manual_evidence_decomposition",
                True,
            )
            and 2 <= len(discrepancies) <= 32
        )
        if matrix not in allowed and not quarantined:
            raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation outcome matrix conflicts")
    elif record_kind is AuditRecordKind.RECONCILIATION_ADJUSTMENT_AUTHORIZATION:
        from ea.core.reconciliation import (
            ReconciliationAuthorizationDecision,
            ReconciliationAuthorizationPolicyId,
        )

        _require_json_id(
            document,
            "authorization_id",
            expected_owner=EconomicOwnerKind.RECONCILIATION_AUTHORIZATION,
            positive=True,
        )
        _require_json_id(
            document,
            "adjustment_id",
            expected_owner=EconomicOwnerKind.RECONCILIATION_ADJUSTMENT,
            positive=True,
        )
        for field in (
            "command_sha256",
            "instrument_spec_set_sha256",
            "local_snapshot_sha256",
            "observation_sha256",
            "outcome_acknowledgement_sha256",
            "policy_sha256",
            "reconciliation_outcome_sha256",
        ):
            _require_json_digest(document, field)
        _require_json_uint64(document, "dispatch_sequence", positive=True)
        _require_json_uint64(document, "ledger_sequence", positive=False)
        _require_json_uint64(document, "policy_version", positive=True)
        _require_json_text(document, "instrument_spec_set_id")
        try:
            ReconciliationAuthorizationPolicyId(_require_json_text(document, "policy_id"))
            ReconciliationAuthorizationDecision(_require_json_text(document, "decision"))
            available_text = _require_json_text(document, "available_at")
            available_at = datetime.strptime(
                available_text,
                "%Y-%m-%dT%H:%M:%S.%fZ",
            ).replace(tzinfo=UTC)
        except (TypeError, ValueError) as error:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "reconciliation authorization values conflict",
            ) from error
        if available_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != available_text:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "reconciliation authorization time conflicts",
            )
    elif record_kind is AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION:
        for field in ("order_id", "held_for_order_id"):
            _require_json_id(
                document,
                field,
                expected_owner=EconomicOwnerKind.EXECUTION_ORDER,
                positive=True,
            )
        if document["order_id"] != document["held_for_order_id"]:
            raise _fail(OutcomeCode.CONFLICTING_ID, "authorization held order conflicts")
        for field in (
            "order_sha256",
            "execution_request_sha256",
            "causal_market_sha256",
            "instrument_gate_id",
            "instrument_spec_set_sha256",
            "execution_policy_sha256",
        ):
            _require_json_digest(document, field)
        _require_json_root_key(document, "causal_root_key", expected_domain="market_data")
        for field in (
            "dispatch_sequence",
            "instrument_gate_version",
            "authorization_state_version",
        ):
            _require_json_uint64(document, field, positive=True)
        for field in (
            "portfolio_snapshot_version",
            "risk_state_version",
            "global_halt_epoch",
            "risk_halt_epoch",
        ):
            _require_json_uint64(document, field, positive=False)
        _require_json_text(document, "instrument_spec_set_id")
        _require_json_text(document, "execution_policy_id")
    elif record_kind is AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION:
        for field in (
            "previous_state_sha256",
            "failing_state_sha256",
            "failed_subject_sha256",
            "trigger_root_sha256",
        ):
            _require_json_digest(document, field)
        try:
            OutcomeCode(_require_json_text(document, "failure_code"))
            AuditRecordKind(_require_json_text(document, "failed_record_kind"))
            AuditSubjectKind(_require_json_text(document, "failed_subject_kind"))
        except ValueError as error:
            raise _fail(OutcomeCode.CONFLICTING_ID, "failing payload enum is invalid") from error
        _require_json_uint64(document, "dispatch_sequence", positive=True)
    elif record_kind is AuditRecordKind.RUNTIME_DISPATCH_COMPLETED:
        from ea.core.lifecycle import (
            ORDERED_SUBMISSION_RECEIPT_DIGEST_DOMAIN,
            SubmissionAuthorizationAttemptStatus,
            decode_submission_authorization_attempt_outcome_document,
            submission_authorization_attempt_outcome_digest,
        )

        if _require_json_text(document, "dispatch_kind") not in {"market", "end_of_run"}:
            raise _fail(OutcomeCode.CONFLICTING_ID, "dispatch_kind is outside its closed enum")
        _require_json_uint64(document, "dispatch_sequence", positive=True)
        _require_json_uint64(document, "outcome_count", positive=False)
        attempt_count = _require_json_uint64(
            document,
            "authorization_attempt_count",
            positive=False,
        )
        submission_count = _require_json_uint64(
            document,
            "submission_count",
            positive=False,
        )
        if attempt_count not in {0, 1} or submission_count not in {0, 1}:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "completion frontier count exceeds one")
        expected_domain = "market_data" if document["dispatch_kind"] == "market" else "end_of_run"
        _require_json_root_key(
            document,
            "trigger_root_key",
            expected_domain=expected_domain,
        )
        for field in (
            "trigger_root_sha256",
            "batch_sha256",
            "ordered_outcome_ack_sha256s_sha256",
            "ordered_submission_receipt_sha256s_sha256",
            "pre_ack_state_sha256",
        ):
            _require_json_digest(document, field)
        attempt_document = document["authorization_attempt_outcome"]
        attempt_sha256 = document["authorization_attempt_outcome_sha256"]
        if attempt_count == 0:
            if attempt_document is not None or attempt_sha256 is not None:
                raise _fail(
                    OutcomeCode.CONFLICTING_ID,
                    "empty completion authorization frontier contains evidence",
                )
            authorized = False
        else:
            if type(attempt_sha256) is not str:
                raise _fail(
                    OutcomeCode.CONFLICTING_ID,
                    "completion authorization digest is missing",
                )
            _require_json_digest(document, "authorization_attempt_outcome_sha256")
            attempt = decode_submission_authorization_attempt_outcome_document(attempt_document)
            if submission_authorization_attempt_outcome_digest(attempt).value != attempt_sha256:
                raise _fail(
                    OutcomeCode.CONFLICTING_ID,
                    "completion authorization outcome digest conflicts",
                )
            if (
                attempt.binding.reference.run_id.value != document["run_id"]
                or attempt.dispatch_sequence != document["dispatch_sequence"]
                or attempt.trigger_root_sha256.value != document["trigger_root_sha256"]
            ):
                raise _fail(
                    OutcomeCode.CONFLICTING_ID,
                    "completion authorization identity conflicts",
                )
            authorized = attempt.status is SubmissionAuthorizationAttemptStatus.AUTHORIZED
        if authorized != (submission_count == 1):
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "completion authorization receipt bijection fails",
            )
        if document["dispatch_kind"] == "end_of_run" and (
            attempt_count != 0 or submission_count != 0
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "bounded-end completion contains submission")
        if submission_count == 0:
            empty_receipt_digest = ordered_digest_tuple(
                ORDERED_SUBMISSION_RECEIPT_DIGEST_DOMAIN,
                (),
            )
            if document["ordered_submission_receipt_sha256s_sha256"] != (
                empty_receipt_digest.value
            ):
                raise _fail(
                    OutcomeCode.CONFLICTING_ID,
                    "empty completion receipt aggregate conflicts",
                )
    elif record_kind is AuditRecordKind.RUN_TERMINAL:
        if _require_json_text(document, "terminal_kind") not in {"success", "failed"}:
            raise _fail(OutcomeCode.CONFLICTING_ID, "terminal_kind is outside its closed enum")
        _require_json_uint64(document, "last_dispatch_sequence", positive=True)
        for field in (
            "last_trigger_root_sha256",
            "pre_terminal_state_sha256",
            "previous_chain_head_sha256",
        ):
            _require_json_digest(document, field)


def _require_reconciliation_discrepancy_document(document: object) -> tuple[str, str, str]:
    from ea.core.economics import CanonicalDecimal, EconomicValidationError
    from ea.core.execution import SettlementCurrency
    from ea.core.identity import Instrument, VenueId

    if type(document) is not dict or document.get("kind") not in {
        "instrument_position",
        "settlement_cash",
    }:
        raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation discrepancy kind conflicts")
    expected = {
        "delta",
        "kind",
        "local_amount",
        "observed_amount",
        "instrument" if document["kind"] == "instrument_position" else "currency",
    }
    if set(document) != expected:
        raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation discrepancy fields conflict")
    if document["kind"] == "instrument_position":
        instrument = document["instrument"]
        if (
            type(instrument) is not dict
            or set(instrument) != {"symbol", "venue"}
            or type(instrument["symbol"]) is not str
            or type(instrument["venue"]) is not str
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation instrument conflicts")
        try:
            instrument_value = Instrument(
                VenueId(instrument["venue"]),
                instrument["symbol"],
            )
        except ValueError as error:
            raise _fail(
                OutcomeCode.CONFLICTING_ID, "reconciliation instrument conflicts"
            ) from error
        key = ("instrument_position", instrument_value.venue.code, instrument_value.symbol)
    else:
        try:
            currency = SettlementCurrency(document["currency"])
        except (TypeError, ValueError) as error:
            raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation currency conflicts") from error
        key = ("settlement_cash", currency.code, "")
    try:
        local = CanonicalDecimal(_require_json_text(document, "local_amount"))
        observed = CanonicalDecimal(_require_json_text(document, "observed_amount"))
        delta = CanonicalDecimal(_require_json_text(document, "delta"))
    except EconomicValidationError as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation amount conflicts") from error
    scale = max(local.scale, observed.scale)
    expected_coefficient = observed.coefficient * (10 ** (scale - observed.scale))
    expected_coefficient -= local.coefficient * (10 ** (scale - local.scale))
    actual_coefficient = delta.coefficient * (10 ** (scale - delta.scale))
    if delta.scale > scale:
        expected_coefficient *= 10 ** (delta.scale - scale)
        scale = delta.scale
        actual_coefficient = delta.coefficient
    if expected_coefficient != actual_coefficient or delta.coefficient == 0:
        raise _fail(OutcomeCode.CONFLICTING_ID, "reconciliation delta conflicts")
    return key


def _require_execution_outcome_payload_values(document: dict[str, object]) -> None:
    expected_fields = {
        "action",
        "anomalies",
        "canonicalization",
        "client_submission_key",
        "fact_key",
        "fact_sha256",
        "fill",
        "halt_requested",
        "ingress_identity",
        "ingress_sha256",
        "message_type",
        "order_resolutions",
        "outcome_code",
        "projection_after_sha256",
        "projection_before_sha256",
        "reported_order_id",
        "reported_venue_order",
        "requires_reconciliation",
        "resolved_order_id",
        "run_id",
        "runtime_dispatch_sequence",
        "schema_version",
    }
    if set(document) != expected_fields:
        raise _fail(OutcomeCode.CONFLICTING_ID, "execution outcome fields conflict")
    if (
        document["canonicalization"] != "ea-execution-fact-processing-outcome-v1"
        or document["message_type"] != "execution_fact_processing_outcome"
        or document["schema_version"] != 1
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "execution outcome schema is invalid")
    _require_json_text(document, "run_id")
    _require_json_text(document, "action")
    try:
        OutcomeCode(_require_json_text(document, "outcome_code"))
    except ValueError as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, "execution outcome code is invalid") from error
    _require_json_uint64(document, "runtime_dispatch_sequence", positive=True)
    for field in ("fact_sha256", "ingress_sha256"):
        _require_json_digest(document, field)
    for field in ("halt_requested", "requires_reconciliation"):
        _require_json_bool(document, field)
    for field in ("anomalies", "order_resolutions"):
        if type(document[field]) is not list:
            raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} must be one JSON array")
    for field in (
        "client_submission_key",
        "projection_after_sha256",
        "projection_before_sha256",
    ):
        if document[field] is not None:
            _require_json_digest(document, field)
    for field in ("reported_order_id", "resolved_order_id"):
        if document[field] is not None:
            _require_json_id(
                document,
                field,
                expected_owner=EconomicOwnerKind.EXECUTION_ORDER,
                positive=True,
            )


def require_canonical_audit_payload(
    record_kind: AuditRecordKind,
    canonical_payload: bytes,
) -> bytes:
    """Validate the exact bytes carrier, size and canonical-JSON representation."""
    if type(record_kind) is not AuditRecordKind or type(canonical_payload) is not bytes:
        raise _fail(OutcomeCode.INVALID_TYPE, "audit payload requires exact kind and bytes")
    limit = (
        MAX_LARGE_AUDIT_PAYLOAD_BYTES
        if record_kind in _LARGE_PAYLOAD_KINDS
        else MAX_SMALL_AUDIT_PAYLOAD_BYTES
    )
    if not 1 <= len(canonical_payload) <= limit:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "audit payload exceeds its kind-specific bound")
    try:
        text = canonical_payload.decode("utf-8")
        document = json.loads(
            text,
            object_pairs_hook=lambda pairs: _reject_duplicate_pairs(pairs),
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise _fail(
            OutcomeCode.CONFLICTING_ID, "audit payload is not strict canonical JSON"
        ) from error
    if type(document) is not dict or _canonical_json(document) != canonical_payload:
        raise _fail(OutcomeCode.CONFLICTING_ID, "audit payload is not canonical JSON")
    if record_kind in {
        AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
        AuditRecordKind.RISK_PORTFOLIO_REFRESH,
    }:
        from ea.core.ledger_integration import (
            canonical_ledger_handoff_outcome_bytes,
            canonical_portfolio_risk_refresh_bytes,
            decode_ledger_handoff_outcome,
            decode_portfolio_risk_refresh,
        )

        try:
            if record_kind is AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME:
                decoded_payload = canonical_ledger_handoff_outcome_bytes(
                    decode_ledger_handoff_outcome(canonical_payload)
                )
            else:
                decoded_payload = canonical_portfolio_risk_refresh_bytes(
                    decode_portfolio_risk_refresh(canonical_payload)
                )
        except ValueError as error:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "audit payload semantic codec rejected its value",
            ) from error
        if decoded_payload != canonical_payload:
            raise _fail(OutcomeCode.CONFLICTING_ID, "audit payload semantic round-trip conflicts")
    elif record_kind is not AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME:
        expected_fields = _AUDIT_PAYLOAD_FIELDS_BY_KIND[record_kind]
        if set(document) != expected_fields:
            raise _fail(OutcomeCode.CONFLICTING_ID, "audit payload fields conflict with its kind")
        if (
            document.get("schema") != _AUDIT_PAYLOAD_SCHEMA_BY_KIND[record_kind]
            or document.get("canonicalization") != AUDIT_CANONICALIZATION
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "audit payload schema is invalid")
        _require_audit_owned_payload_values(record_kind, document)
    else:
        _require_execution_outcome_payload_values(document)
    return canonical_payload


def audit_subject_digest(
    record_kind: AuditRecordKind,
    canonical_payload: bytes,
) -> Sha256Digest:
    """Reconstruct a record subject digest wherever the v1 payload owns that identity."""
    require_canonical_audit_payload(record_kind, canonical_payload)
    document = json.loads(canonical_payload)
    if record_kind is AuditRecordKind.RUN_PREPARED:
        return Sha256Digest(document["manifest_sha256"])
    if record_kind is AuditRecordKind.MATCHER_DISPATCH_BATCH:
        return Sha256Digest(document["batch_sha256"])
    if record_kind is AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME:
        return Sha256Digest(
            sha256(EXECUTION_FACT_PROCESSING_OUTCOME_DIGEST_DOMAIN + canonical_payload).hexdigest()
        )
    domain = _SUBJECT_DOMAIN_BY_KIND[record_kind]
    return Sha256Digest(
        sha256(domain + len(canonical_payload).to_bytes(8, "big") + canonical_payload).hexdigest()
    )


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def create_audit_record(
    *,
    binding: RunBinding,
    owner_sequence: int,
    record_kind: AuditRecordKind,
    subject_kind: AuditSubjectKind,
    subject_sha256: Sha256Digest,
    canonical_payload: bytes,
    previous_record_sha256: Sha256Digest,
    previous_chain_head_sha256: Sha256Digest,
) -> AuditRecord:
    """Create one record after validating every exact identity and byte bound."""
    if type(binding) is not RunBinding:
        raise _fail(OutcomeCode.INVALID_TYPE, "binding must be an exact RunBinding")
    _require_kind_subject(record_kind, subject_kind)
    if type(owner_sequence) is not int:
        raise _fail(OutcomeCode.INVALID_TYPE, "owner_sequence must have exact runtime type int")
    if not 1 <= owner_sequence <= min(_MAX_UINT64, MAX_AUDIT_RECORDS):
        raise _fail(OutcomeCode.OUT_OF_RANGE, "audit owner sequence is outside the v1 bound")
    for name, candidate_digest in (
        ("subject_sha256", subject_sha256),
        ("previous_record_sha256", previous_record_sha256),
        ("previous_chain_head_sha256", previous_chain_head_sha256),
    ):
        if type(candidate_digest) is not Sha256Digest:
            raise _fail(OutcomeCode.INVALID_TYPE, f"{name} must be an exact Sha256Digest")
    require_canonical_audit_payload(record_kind, canonical_payload)
    if audit_subject_digest(record_kind, canonical_payload) != subject_sha256:
        raise _fail(OutcomeCode.CONFLICTING_ID, "subject digest conflicts with canonical payload")
    try:
        payload_document = json.loads(canonical_payload)
    except json.JSONDecodeError as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, "audit payload is not JSON") from error
    if payload_document.get("run_id") != binding.reference.run_id.value:
        raise _fail(OutcomeCode.CONFLICTING_ID, "audit payload run binding conflicts")
    if owner_sequence == 1:
        if (
            record_kind is not AuditRecordKind.RUN_PREPARED
            or subject_sha256 != binding.manifest_sha256
            or previous_record_sha256 != EMPTY_RECORD_SHA256
            or previous_chain_head_sha256 != EMPTY_CHAIN_HEAD_SHA256
            or canonical_payload != canonical_run_prepared_audit_payload(binding)
        ):
            raise _fail(
                OutcomeCode.CONFLICTING_ID, "sequence one must be the exact preparation record"
            )
    elif record_kind is AuditRecordKind.RUN_PREPARED:
        raise _fail(OutcomeCode.CONFLICTING_ID, "preparation is admitted only at sequence one")
    value = object.__new__(AuditRecord)
    object.__setattr__(value, "binding", binding)
    object.__setattr__(
        value,
        "record_id",
        EconomicId(binding.reference.run_id, EconomicOwnerKind.AUDIT_RECORD, owner_sequence),
    )
    object.__setattr__(value, "record_kind", record_kind)
    object.__setattr__(value, "subject_kind", subject_kind)
    object.__setattr__(value, "subject_sha256", subject_sha256)
    object.__setattr__(value, "canonical_payload", canonical_payload)
    object.__setattr__(value, "payload_sha256", Sha256Digest(sha256(canonical_payload).hexdigest()))
    object.__setattr__(value, "previous_record_sha256", previous_record_sha256)
    object.__setattr__(value, "previous_chain_head_sha256", previous_chain_head_sha256)
    object.__setattr__(value, "_seal", _RECORD_SEAL)
    return value


def _require_record(record: object) -> AuditRecord:
    if type(record) is not AuditRecord or getattr(record, "_seal", None) is not _RECORD_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "record must be an exact factory-issued AuditRecord")
    return record


def audit_record_header_document(record: AuditRecord) -> dict[str, object]:
    """Project the exact v1 header object."""
    checked = _require_record(record)
    binding = checked.binding
    return {
        "canonicalization": AUDIT_CANONICALIZATION,
        "lineage_sha256": binding.reference.lineage_sha256.value,
        "manifest_sha256": binding.manifest_sha256.value,
        "payload_sha256": checked.payload_sha256.value,
        "previous_chain_head_sha256": checked.previous_chain_head_sha256.value,
        "previous_record_sha256": checked.previous_record_sha256.value,
        "record_id": _economic_id_document(checked.record_id),
        "record_kind": checked.record_kind.value,
        "run_id": binding.reference.run_id.value,
        "schema": AUDIT_RECORD_HEADER_SCHEMA,
        "subject_kind": checked.subject_kind.value,
        "subject_sha256": checked.subject_sha256.value,
    }


def canonical_audit_record_header_bytes(record: AuditRecord) -> bytes:
    payload = _canonical_json(audit_record_header_document(record))
    if not 1 <= len(payload) <= MAX_AUDIT_HEADER_BYTES:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "canonical audit header exceeds the v1 bound")
    return payload


def canonical_audit_record_bytes(record: AuditRecord) -> bytes:
    checked = _require_record(record)
    header = canonical_audit_record_header_bytes(checked)
    return (
        len(header).to_bytes(8, "big")
        + header
        + len(checked.canonical_payload).to_bytes(8, "big")
        + checked.canonical_payload
    )


def audit_record_digest(record: AuditRecord) -> Sha256Digest:
    return Sha256Digest(
        sha256(AUDIT_RECORD_DIGEST_DOMAIN + canonical_audit_record_bytes(record)).hexdigest()
    )


def audit_chain_head(record: AuditRecord) -> Sha256Digest:
    checked = _require_record(record)
    return Sha256Digest(
        sha256(
            AUDIT_CHAIN_DIGEST_DOMAIN
            + bytes.fromhex(checked.previous_chain_head_sha256.value)
            + bytes.fromhex(audit_record_digest(checked).value)
        ).hexdigest()
    )


def audit_frame_checksum(record: AuditRecord) -> Sha256Digest:
    return Sha256Digest(
        sha256(AUDIT_FRAME_DIGEST_DOMAIN + canonical_audit_record_bytes(record)).hexdigest()
    )


def create_audit_append_acknowledgement(record: AuditRecord) -> AuditAppendAcknowledgement:
    """Reconstruct an acknowledgement from one already verified record."""
    checked = _require_record(record)
    value = object.__new__(AuditAppendAcknowledgement)
    object.__setattr__(value, "binding", checked.binding)
    object.__setattr__(value, "record_id", checked.record_id)
    object.__setattr__(value, "record_kind", checked.record_kind)
    object.__setattr__(value, "subject_kind", checked.subject_kind)
    object.__setattr__(value, "subject_sha256", checked.subject_sha256)
    object.__setattr__(value, "payload_sha256", checked.payload_sha256)
    object.__setattr__(value, "record_sha256", audit_record_digest(checked))
    object.__setattr__(value, "chain_head_sha256", audit_chain_head(checked))
    object.__setattr__(value, "_seal", _ACK_SEAL)
    return value


def _require_acknowledgement(value: object) -> AuditAppendAcknowledgement:
    if (
        type(value) is not AuditAppendAcknowledgement
        or getattr(value, "_seal", None) is not _ACK_SEAL
    ):
        raise _fail(
            OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
            "acknowledgement is not exact factory-issued evidence",
        )
    return value


def audit_acknowledgement_document(
    acknowledgement: AuditAppendAcknowledgement,
) -> dict[str, object]:
    checked = _require_acknowledgement(acknowledgement)
    binding = checked.binding
    return {
        "canonicalization": AUDIT_CANONICALIZATION,
        "chain_head_sha256": checked.chain_head_sha256.value,
        "lineage_sha256": binding.reference.lineage_sha256.value,
        "manifest_sha256": binding.manifest_sha256.value,
        "payload_sha256": checked.payload_sha256.value,
        "record_id": _economic_id_document(checked.record_id),
        "record_kind": checked.record_kind.value,
        "record_sha256": checked.record_sha256.value,
        "run_id": binding.reference.run_id.value,
        "schema": AUDIT_ACKNOWLEDGEMENT_SCHEMA,
        "subject_kind": checked.subject_kind.value,
        "subject_sha256": checked.subject_sha256.value,
    }


def canonical_audit_append_acknowledgement_bytes(
    acknowledgement: AuditAppendAcknowledgement,
) -> bytes:
    return _canonical_json(audit_acknowledgement_document(acknowledgement))


def audit_append_acknowledgement_digest(
    acknowledgement: AuditAppendAcknowledgement,
) -> Sha256Digest:
    return Sha256Digest(
        sha256(
            AUDIT_ACKNOWLEDGEMENT_DIGEST_DOMAIN
            + canonical_audit_append_acknowledgement_bytes(acknowledgement)
        ).hexdigest()
    )


def audit_acknowledgement_id(acknowledgement: AuditAppendAcknowledgement) -> str:
    checked = _require_acknowledgement(acknowledgement)
    return f"audit.record/{checked.record_id.run_id.value}/{checked.record_id.owner_sequence}"


def require_audit_acknowledgement(
    acknowledgement: object,
    *,
    binding: RunBinding,
    logical_key: AuditLogicalKey,
    canonical_payload: bytes,
) -> AuditAppendAcknowledgement:
    """Require an acknowledgement to bind the complete requested logical record."""
    checked = _require_acknowledgement(acknowledgement)
    if type(binding) is not RunBinding or type(logical_key) is not AuditLogicalKey:
        raise _fail(OutcomeCode.INVALID_TYPE, "acknowledgement expectation carriers are invalid")
    if type(canonical_payload) is not bytes:
        raise _fail(OutcomeCode.INVALID_TYPE, "canonical_payload must be exact bytes")
    if (
        checked.binding != binding
        or checked.record_id.run_id != binding.reference.run_id
        or checked.record_id.owner_kind is not EconomicOwnerKind.AUDIT_RECORD
        or checked.record_id.owner_sequence < 1
        or checked.record_kind is not logical_key.record_kind
        or checked.subject_kind is not logical_key.subject_kind
        or checked.subject_sha256 != logical_key.subject_sha256
        or checked.payload_sha256.value != sha256(canonical_payload).hexdigest()
    ):
        raise _fail(
            OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
            "audit acknowledgement does not bind the requested record",
        )
    return checked


def ordered_digest_tuple(domain: bytes, digests: tuple[Sha256Digest, ...]) -> Sha256Digest:
    """Hash one authoritative digest tuple with the ADR 0020 aggregate framing."""
    if type(domain) is not bytes or type(digests) is not tuple:
        raise _fail(OutcomeCode.INVALID_TYPE, "ordered digest aggregate carriers are invalid")
    if any(type(value) is not Sha256Digest for value in digests):
        raise _fail(OutcomeCode.INVALID_TYPE, "ordered digest aggregate requires exact digests")
    return Sha256Digest(
        sha256(
            domain
            + len(digests).to_bytes(8, "big")
            + b"".join(bytes.fromhex(value.value) for value in digests)
        ).hexdigest()
    )


def decode_audit_record(
    *,
    binding: RunBinding,
    canonical_header: bytes,
    canonical_payload: bytes,
) -> AuditRecord:
    """Strictly decode one framed record against its externally fixed run binding."""
    if type(binding) is not RunBinding or type(canonical_header) is not bytes:
        raise _fail(OutcomeCode.INVALID_TYPE, "audit decode carriers are invalid")
    if not 1 <= len(canonical_header) <= MAX_AUDIT_HEADER_BYTES:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "audit header length is outside the v1 bound")
    try:
        document = json.loads(
            canonical_header.decode("utf-8"),
            object_pairs_hook=lambda pairs: _reject_duplicate_pairs(pairs),
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
        if type(document) is not dict or _canonical_json(document) != canonical_header:
            raise ValueError("noncanonical header")
        expected_keys = {
            "canonicalization",
            "lineage_sha256",
            "manifest_sha256",
            "payload_sha256",
            "previous_chain_head_sha256",
            "previous_record_sha256",
            "record_id",
            "record_kind",
            "run_id",
            "schema",
            "subject_kind",
            "subject_sha256",
        }
        if set(document) != expected_keys:
            raise ValueError("wrong header keys")
        record_id = document["record_id"]
        if type(record_id) is not dict or set(record_id) != {
            "owner_kind",
            "owner_sequence",
            "run_id",
        }:
            raise ValueError("wrong record id")
        if (
            document["schema"] != AUDIT_RECORD_HEADER_SCHEMA
            or document["canonicalization"] != AUDIT_CANONICALIZATION
            or document["run_id"] != binding.reference.run_id.value
            or document["lineage_sha256"] != binding.reference.lineage_sha256.value
            or document["manifest_sha256"] != binding.manifest_sha256.value
            or record_id["run_id"] != binding.reference.run_id.value
            or record_id["owner_kind"] != EconomicOwnerKind.AUDIT_RECORD.value
            or document["payload_sha256"] != sha256(canonical_payload).hexdigest()
        ):
            raise ValueError("binding conflict")
        value = create_audit_record(
            binding=binding,
            owner_sequence=record_id["owner_sequence"],
            record_kind=AuditRecordKind(document["record_kind"]),
            subject_kind=AuditSubjectKind(document["subject_kind"]),
            subject_sha256=Sha256Digest(document["subject_sha256"]),
            canonical_payload=canonical_payload,
            previous_record_sha256=Sha256Digest(document["previous_record_sha256"]),
            previous_chain_head_sha256=Sha256Digest(document["previous_chain_head_sha256"]),
        )
    except (KeyError, TypeError, ValueError, RunContractError) as error:
        if isinstance(error, AuditContractError):
            raise
        raise _fail(OutcomeCode.CONFLICTING_ID, "audit header conflicts with v1 schema") from error
    if canonical_audit_record_header_bytes(value) != canonical_header:
        raise _fail(OutcomeCode.CONFLICTING_ID, "audit header failed canonical reconstruction")
    return value
