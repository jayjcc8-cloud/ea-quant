"""Dependency-neutral Phase 1 lifecycle ports and evidence from ADR 0020."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any, Protocol, final

from ea.core.audit import (
    AuditAppendAcknowledgement,
    AuditLogicalKey,
    AuditRecordKind,
    AuditSubjectKind,
    _canonical_completion_v4_reconciliation_root_key_document,
    audit_append_acknowledgement_digest,
    audit_subject_digest,
    ordered_digest_tuple,
    require_audit_acknowledgement,
)
from ea.core.execution import InstrumentExecutionSpecSet
from ea.core.execution_identity import (
    EconomicId,
    EconomicOwnerKind,
    IngressIdentity,
    SourceNamespace,
)
from ea.core.execution_messages import ExecutionFactIngress, Fill, Order
from ea.core.execution_state import (
    ExecutionFactProcessingOutcome,
    OrderProjectionSnapshot,
    canonical_execution_fact_processing_outcome_bytes,
    execution_fact_processing_outcome_digest,
)
from ea.core.historical_matching import (
    HistoricalDispatchKind,
    HistoricalMatcherDispatchBatch,
    HistoricalSubmissionReceipt,
    historical_matcher_dispatch_batch_digest,
    historical_submission_receipt_digest,
    runtime_root_order_key_document,
)
from ea.core.identity import Instrument
from ea.core.market_data import MarketDataEnvelope
from ea.core.outcomes import OutcomeCode
from ea.core.portfolio import PortfolioSnapshot
from ea.core.risk import RiskStateSnapshot
from ea.core.run import RunBinding, RunId, RunReference, Sha256Digest
from ea.core.runtime import EndOfRunRoot, RuntimeRoot, RuntimeRootOrderKey

ORDERED_INGRESS_DIGEST_DOMAIN = b"ea.audit-ordered-ingress-digests.v1\0"
ORDERED_OUTCOME_ACK_DIGEST_DOMAIN = b"ea.audit-ordered-outcome-ack-digests.v1\0"
ORDERED_HANDOFF_DIGEST_DOMAIN = b"ea.coordinator-ordered-handoff-digests.v1\0"
ORDERED_SUBMISSION_RECEIPT_DIGEST_DOMAIN = b"ea.coordinator-ordered-submission-receipt-digests.v1\0"
ORDERED_LEDGER_ACK_DIGEST_DOMAIN = b"ea.audit-ordered-ledger-ack-digests.v1\0"
ORDERED_RECONCILIATION_FRONTIER_DIGEST_DOMAIN = (
    b"ea.audit-ordered-reconciliation-frontier-digests.v1\0"
)

_STATE_DOMAIN = b"ea.coordinator-state.v1\0"
_PRE_TERMINAL_STATE_DOMAIN = b"ea.coordinator-pre-terminal-state.v1\0"
_TERMINAL_STATE_DOMAIN = b"ea.coordinator-terminal-state.v1\0"
_HANDOFF_DOMAIN = b"ea.coordinator-audited-fact-handoff.v1\0"
_DISPATCH_OUTCOME_DOMAIN = b"ea.coordinator-dispatch-outcome.v1\0"
_TERMINAL_OUTCOME_DOMAIN = b"ea.coordinator-terminal-outcome.v1\0"
_ACTIVE_DISPATCH_WINDOW_DOMAIN = b"ea.coordinator-active-dispatch-window.v1\0"
_READ_ONLY_RECONCILIATION_WINDOW_DOMAIN = b"ea.coordinator-read-only-reconciliation-window.v1\0"
_READ_ONLY_RECONCILIATION_OUTCOME_DOMAIN = b"ea.coordinator-read-only-reconciliation-outcome.v1\0"
_AUTHORIZATION_ATTEMPT_OUTCOME_DOMAIN = b"ea.submission-authorization-attempt-outcome.v1\0"
_MAX_UINT64 = (1 << 64) - 1
_VALUE_SEAL = object()


class LifecycleError(ValueError):
    """Closed lifecycle validation failure."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode:
            raise TypeError("lifecycle errors require an exact OutcomeCode")
        self.code = code
        super().__init__(message)


def _fail(code: OutcomeCode, message: str) -> LifecycleError:
    return LifecycleError(code, message)


def _canonical_json(document: object) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _framed_digest(domain: bytes, payload: bytes) -> Sha256Digest:
    return Sha256Digest(sha256(domain + len(payload).to_bytes(8, "big") + payload).hexdigest())


def _economic_id_document(value: EconomicId | None) -> dict[str, object] | None:
    if value is None:
        return None
    if type(value) is not EconomicId:
        raise _fail(OutcomeCode.INVALID_TYPE, "economic identity must be exact")
    return {
        "owner_kind": value.owner_kind.value,
        "owner_sequence": value.owner_sequence,
        "run_id": value.run_id.value,
    }


def _ingress_identity_document(value: IngressIdentity) -> dict[str, object]:
    if type(value) is not IngressIdentity:
        raise _fail(OutcomeCode.INVALID_TYPE, "ingress identity must be exact")
    return {
        "ingress_sequence": value.ingress_sequence,
        "source_namespace": value.source_namespace.value,
    }


class CoordinatorPhase(StrEnum):
    ADMITTED = "admitted"
    RUNNING = "running"
    DRAINING = "draining"
    FAILING = "failing"


class CoordinatorTerminalKind(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


class ActiveDispatchWindowStage(StrEnum):
    OPEN = "open"
    COMPLETION_FROZEN = "completion_frozen"
    COMPLETION_ACKNOWLEDGED = "completion_acknowledged"
    COMPLETED = "completed"


class SubmissionAuthorizationAttemptStatus(StrEnum):
    DENIED = "denied"
    UNRESOLVED = "unresolved"
    AUTHORIZED = "authorized"
    BURNED = "burned"
    FAILED = "failed"


class RuntimeDispatchLeaseView(Protocol):
    @property
    def root(self) -> RuntimeRoot: ...

    @property
    def dispatch_sequence(self) -> int: ...


class RuntimeLifecyclePort(Protocol):
    @property
    def run_id(self) -> RunId: ...

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet: ...

    @property
    def active_lease(self) -> RuntimeDispatchLeaseView | None: ...

    @property
    def terminal_acknowledged(self) -> bool: ...

    @property
    def trace_records(self) -> tuple[bytes, ...]: ...

    @property
    def trace_digest(self) -> Sha256Digest: ...

    def pop(self) -> RuntimeDispatchLeaseView: ...

    def acknowledge(self, lease: Any) -> None: ...


class HistoricalMatcherPort(Protocol):
    @property
    def run_id(self) -> RunId: ...

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet: ...

    @property
    def source_namespace(self) -> SourceNamespace: ...

    def match_active_market_root(
        self,
        root: MarketDataEnvelope,
        *,
        dispatch_sequence: int,
    ) -> HistoricalMatcherDispatchBatch: ...

    def expire_at_active_end(
        self,
        root: EndOfRunRoot,
        *,
        dispatch_sequence: int,
    ) -> HistoricalMatcherDispatchBatch: ...

    def resolve_dispatch_batch(
        self,
        *,
        dispatch_sequence: int,
        trigger_root_sha256: Sha256Digest,
    ) -> HistoricalMatcherDispatchBatch | None: ...

    def resolve_submission_receipt(
        self,
        *,
        order_id: EconomicId,
        execution_request_sha256: Sha256Digest,
    ) -> HistoricalSubmissionReceipt | None: ...

    def submit(
        self,
        order: Order,
        *,
        causal_market_root: MarketDataEnvelope,
        dispatch_sequence: int,
    ) -> HistoricalSubmissionReceipt: ...


class ExecutionFactAuthorityPort(Protocol):
    @property
    def run_id(self) -> RunId: ...

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet: ...

    def process_ingress(
        self,
        ingress: ExecutionFactIngress,
    ) -> ExecutionFactProcessingOutcome: ...

    def resolve_processing_outcome(
        self,
        *,
        ingress_identity: IngressIdentity,
        ingress_sha256: Sha256Digest,
    ) -> ExecutionFactProcessingOutcome | None: ...


class ExecutionEvidenceResolverPort(Protocol):
    def resolve_fill(
        self,
        *,
        fill_id: EconomicId,
        fill_sha256: Sha256Digest,
    ) -> Fill | None: ...

    def resolve_projection_after(
        self,
        *,
        order_id: EconomicId,
        projection_sha256: Sha256Digest,
    ) -> OrderProjectionSnapshot | None: ...


class PortfolioFreshnessPort(Protocol):
    def current_snapshot(self) -> PortfolioSnapshot: ...


class RiskFreshnessPort(Protocol):
    def current_state(self) -> RiskStateSnapshot: ...


@final
@dataclass(frozen=True, slots=True)
class GlobalHaltSnapshot:
    run_id: RunId
    halted: bool
    global_halt_epoch: int

    def __post_init__(self) -> None:
        if (
            type(self.run_id) is not RunId
            or type(self.halted) is not bool
            or type(self.global_halt_epoch) is not int
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "global halt snapshot carriers are invalid")
        if not 0 <= self.global_halt_epoch <= _MAX_UINT64:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "global halt epoch is outside uint64")


@final
@dataclass(frozen=True, slots=True)
class InstrumentGateSnapshot:
    run_id: RunId
    instrument: Instrument
    held_for_order_id: EconomicId | None
    instrument_gate_id: Sha256Digest
    instrument_gate_version: int
    halted: bool

    def __post_init__(self) -> None:
        if (
            type(self.run_id) is not RunId
            or type(self.instrument) is not Instrument
            or (
                self.held_for_order_id is not None
                and type(self.held_for_order_id) is not EconomicId
            )
            or type(self.instrument_gate_id) is not Sha256Digest
            or type(self.instrument_gate_version) is not int
            or type(self.halted) is not bool
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "instrument gate snapshot carriers are invalid")
        if not 1 <= self.instrument_gate_version <= _MAX_UINT64:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "instrument gate version must be positive uint64")


class GlobalHaltFreshnessPort(Protocol):
    def current_state(self) -> GlobalHaltSnapshot: ...


class InstrumentGateFreshnessPort(Protocol):
    def current_for(self, instrument: Instrument) -> InstrumentGateSnapshot: ...


class SubmissionAuthorizationPreparationPort(Protocol):
    def prepare(
        self,
        order: Order,
        *,
        causal_market_root: MarketDataEnvelope,
        dispatch_sequence: int,
        capability: object,
    ) -> AuditAppendAcknowledgement: ...

    def prepare_attempt(
        self,
        order: Order,
        *,
        causal_market_root: MarketDataEnvelope,
        dispatch_sequence: int,
        capability: object,
    ) -> SubmissionAuthorizationAttemptOutcome: ...

    def resolve_attempt(
        self,
        *,
        order_id: EconomicId,
        execution_request_sha256: Sha256Digest,
    ) -> SubmissionAuthorizationAttemptOutcome | None: ...

    def resolve_attempt_acknowledgement(
        self,
        *,
        order_id: EconomicId,
        execution_request_sha256: Sha256Digest,
    ) -> AuditAppendAcknowledgement | None: ...

    def resolve_attempt_order(
        self,
        *,
        order_id: EconomicId,
        execution_request_sha256: Sha256Digest,
    ) -> Order | None: ...


@final
@dataclass(frozen=True, slots=True, init=False)
class ActiveDispatchWindow:
    binding: RunBinding
    coordinator_state_version: int
    dispatch_kind: HistoricalDispatchKind
    dispatch_sequence: int
    trigger_root_key: RuntimeRootOrderKey
    trigger_root_sha256: Sha256Digest
    batch_sha256: Sha256Digest
    batch_ack_sha256: Sha256Digest
    handoff_count: int
    ordered_handoff_sha256s_sha256: Sha256Digest
    audited_handoff_chain_head_sha256: Sha256Digest
    authorization_allowed: bool
    _seal: object

    def __init__(self) -> None:
        raise TypeError("active dispatch windows are created only by the coordinator")

    def __copy__(self) -> ActiveDispatchWindow:
        raise TypeError("active dispatch windows cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> ActiveDispatchWindow:
        del memo
        raise TypeError("active dispatch windows cannot be copied")

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("active dispatch windows cannot be serialized")


def _create_active_dispatch_window(
    *,
    binding: RunBinding,
    coordinator_state_version: int,
    dispatch_kind: HistoricalDispatchKind,
    dispatch_sequence: int,
    trigger_root_key: RuntimeRootOrderKey,
    trigger_root_sha256: Sha256Digest,
    batch_sha256: Sha256Digest,
    batch_ack_sha256: Sha256Digest,
    handoff_sha256s: tuple[Sha256Digest, ...],
    audited_handoff_chain_head_sha256: Sha256Digest,
    authorization_allowed: bool,
) -> ActiveDispatchWindow:
    if (
        type(binding) is not RunBinding
        or type(coordinator_state_version) is not int
        or type(dispatch_kind) is not HistoricalDispatchKind
        or type(dispatch_sequence) is not int
        or type(trigger_root_key) is not RuntimeRootOrderKey
        or type(trigger_root_sha256) is not Sha256Digest
        or type(batch_sha256) is not Sha256Digest
        or type(batch_ack_sha256) is not Sha256Digest
        or type(handoff_sha256s) is not tuple
        or any(type(value) is not Sha256Digest for value in handoff_sha256s)
        or type(audited_handoff_chain_head_sha256) is not Sha256Digest
        or type(authorization_allowed) is not bool
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "active dispatch window carriers are invalid")
    if not 1 <= coordinator_state_version <= _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "window state version is outside uint64")
    if not 1 <= dispatch_sequence <= _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "window dispatch sequence is outside uint64")
    if authorization_allowed and dispatch_kind is not HistoricalDispatchKind.MARKET:
        raise _fail(OutcomeCode.CONFLICTING_ID, "bounded-end window cannot authorize")
    value = object.__new__(ActiveDispatchWindow)
    for name, field in {
        "binding": binding,
        "coordinator_state_version": coordinator_state_version,
        "dispatch_kind": dispatch_kind,
        "dispatch_sequence": dispatch_sequence,
        "trigger_root_key": trigger_root_key,
        "trigger_root_sha256": trigger_root_sha256,
        "batch_sha256": batch_sha256,
        "batch_ack_sha256": batch_ack_sha256,
        "handoff_count": len(handoff_sha256s),
        "ordered_handoff_sha256s_sha256": ordered_digest_tuple(
            ORDERED_HANDOFF_DIGEST_DOMAIN,
            handoff_sha256s,
        ),
        "audited_handoff_chain_head_sha256": audited_handoff_chain_head_sha256,
        "authorization_allowed": authorization_allowed,
    }.items():
        object.__setattr__(value, name, field)
    object.__setattr__(value, "_seal", _VALUE_SEAL)
    return value


def canonical_active_dispatch_window_bytes(window: ActiveDispatchWindow) -> bytes:
    if type(window) is not ActiveDispatchWindow or window._seal is not _VALUE_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "active dispatch window must be coordinator-issued")
    binding = window.binding
    return _canonical_json(
        {
            "audited_handoff_chain_head_sha256": (window.audited_handoff_chain_head_sha256.value),
            "authorization_allowed": window.authorization_allowed,
            "batch_ack_sha256": window.batch_ack_sha256.value,
            "batch_sha256": window.batch_sha256.value,
            "canonicalization": "ea-canonical-json-v1",
            "coordinator_state_version": window.coordinator_state_version,
            "dispatch_kind": window.dispatch_kind.value,
            "dispatch_sequence": window.dispatch_sequence,
            "handoff_count": window.handoff_count,
            "lineage_sha256": binding.reference.lineage_sha256.value,
            "manifest_sha256": binding.manifest_sha256.value,
            "ordered_handoff_sha256s_sha256": (window.ordered_handoff_sha256s_sha256.value),
            "run_id": binding.reference.run_id.value,
            "schema": "ea.coordinator-active-dispatch-window.v1",
            "trigger_root_key": runtime_root_order_key_document(window.trigger_root_key),
            "trigger_root_sha256": window.trigger_root_sha256.value,
        }
    )


def active_dispatch_window_digest(window: ActiveDispatchWindow) -> Sha256Digest:
    return _framed_digest(
        _ACTIVE_DISPATCH_WINDOW_DOMAIN,
        canonical_active_dispatch_window_bytes(window),
    )


@final
@dataclass(frozen=True, slots=True, init=False)
class ReadOnlyReconciliationDispatchWindow:
    """Sealed rank-20 staged carrier with no matcher or effect evidence."""

    binding: RunBinding
    coordinator_state_version: int
    dispatch_sequence: int
    trigger_root_key: RuntimeRootOrderKey
    trigger_root_sha256: Sha256Digest
    observation_sha256: Sha256Digest
    outcome_ack_sha256: Sha256Digest
    refresh_ack_sha256: Sha256Digest
    refresh_value_sha256: Sha256Digest
    final_portfolio_snapshot_sha256: Sha256Digest
    final_risk_state_sha256: Sha256Digest
    pre_ack_state_sha256: Sha256Digest
    authorization_allowed: bool
    _seal: object

    def __init__(self) -> None:
        raise TypeError("read-only reconciliation windows are created only by the coordinator")

    def __copy__(self) -> ReadOnlyReconciliationDispatchWindow:
        raise TypeError("read-only reconciliation windows cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> ReadOnlyReconciliationDispatchWindow:
        del memo
        raise TypeError("read-only reconciliation windows cannot be copied")

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("read-only reconciliation windows cannot be serialized")


def _create_read_only_reconciliation_dispatch_window(
    *,
    binding: RunBinding,
    coordinator_state_version: int,
    dispatch_sequence: int,
    trigger_root_key: RuntimeRootOrderKey,
    trigger_root_sha256: Sha256Digest,
    observation_sha256: Sha256Digest,
    outcome_ack_sha256: Sha256Digest,
    refresh_ack_sha256: Sha256Digest,
    refresh_value_sha256: Sha256Digest,
    final_portfolio_snapshot_sha256: Sha256Digest,
    final_risk_state_sha256: Sha256Digest,
    pre_ack_state_sha256: Sha256Digest,
) -> ReadOnlyReconciliationDispatchWindow:
    values = (
        trigger_root_sha256,
        observation_sha256,
        outcome_ack_sha256,
        refresh_ack_sha256,
        refresh_value_sha256,
        final_portfolio_snapshot_sha256,
        final_risk_state_sha256,
        pre_ack_state_sha256,
    )
    if (
        type(binding) is not RunBinding
        or type(coordinator_state_version) is not int
        or type(dispatch_sequence) is not int
        or type(trigger_root_key) is not RuntimeRootOrderKey
        or any(type(value) is not Sha256Digest for value in values)
    ):
        raise _fail(
            OutcomeCode.INVALID_TYPE, "read-only reconciliation window carriers are invalid"
        )
    if (
        not 1 <= coordinator_state_version <= _MAX_UINT64
        or not 1 <= dispatch_sequence <= _MAX_UINT64
    ):
        raise _fail(
            OutcomeCode.OUT_OF_RANGE, "read-only reconciliation window sequence is outside uint64"
        )
    if trigger_root_key.domain_rank != 20 or trigger_root_sha256 != observation_sha256:
        raise _fail(OutcomeCode.CONFLICTING_ID, "read-only reconciliation window root conflicts")
    value = object.__new__(ReadOnlyReconciliationDispatchWindow)
    for name, field in {
        "binding": binding,
        "coordinator_state_version": coordinator_state_version,
        "dispatch_sequence": dispatch_sequence,
        "trigger_root_key": trigger_root_key,
        "trigger_root_sha256": trigger_root_sha256,
        "observation_sha256": observation_sha256,
        "outcome_ack_sha256": outcome_ack_sha256,
        "refresh_ack_sha256": refresh_ack_sha256,
        "refresh_value_sha256": refresh_value_sha256,
        "final_portfolio_snapshot_sha256": final_portfolio_snapshot_sha256,
        "final_risk_state_sha256": final_risk_state_sha256,
        "pre_ack_state_sha256": pre_ack_state_sha256,
        "authorization_allowed": False,
    }.items():
        object.__setattr__(value, name, field)
    object.__setattr__(value, "_seal", _VALUE_SEAL)
    return value


def canonical_read_only_reconciliation_dispatch_window_bytes(
    window: ReadOnlyReconciliationDispatchWindow,
) -> bytes:
    if type(window) is not ReadOnlyReconciliationDispatchWindow or window._seal is not _VALUE_SEAL:
        raise _fail(
            OutcomeCode.INVALID_TYPE, "read-only reconciliation window must be coordinator-issued"
        )
    binding = window.binding
    return _canonical_json(
        {
            "authorization_allowed": False,
            "canonicalization": "ea-canonical-json-v1",
            "coordinator_state_version": window.coordinator_state_version,
            "dispatch_kind": "reconciliation_observation",
            "dispatch_sequence": window.dispatch_sequence,
            "final_portfolio_snapshot_sha256": window.final_portfolio_snapshot_sha256.value,
            "final_risk_state_sha256": window.final_risk_state_sha256.value,
            "lineage_sha256": binding.reference.lineage_sha256.value,
            "manifest_sha256": binding.manifest_sha256.value,
            "observation_sha256": window.observation_sha256.value,
            "outcome_ack_sha256": window.outcome_ack_sha256.value,
            "pre_ack_state_sha256": window.pre_ack_state_sha256.value,
            "refresh_ack_sha256": window.refresh_ack_sha256.value,
            "refresh_value_sha256": window.refresh_value_sha256.value,
            "run_id": binding.reference.run_id.value,
            "schema": "ea.coordinator-read-only-reconciliation-window.v1",
            "trigger_root_key": _canonical_completion_v4_reconciliation_root_key_document(
                window.trigger_root_key, expected_run_id=binding.reference.run_id.value
            ),
            "trigger_root_sha256": window.trigger_root_sha256.value,
        }
    )


def read_only_reconciliation_dispatch_window_digest(
    window: ReadOnlyReconciliationDispatchWindow,
) -> Sha256Digest:
    return _framed_digest(
        _READ_ONLY_RECONCILIATION_WINDOW_DOMAIN,
        canonical_read_only_reconciliation_dispatch_window_bytes(window),
    )


@final
@dataclass(frozen=True, slots=True, init=False)
class ReadOnlyReconciliationDispatchOutcome:
    """Sealed completed result for one read-only reconciliation window."""

    binding: RunBinding
    dispatch_sequence: int
    trigger_root_key: RuntimeRootOrderKey
    trigger_root_sha256: Sha256Digest
    observation_sha256: Sha256Digest
    outcome_ack_sha256: Sha256Digest
    refresh_ack_sha256: Sha256Digest
    refresh_value_sha256: Sha256Digest
    final_portfolio_snapshot_sha256: Sha256Digest
    final_risk_state_sha256: Sha256Digest
    pre_ack_state_sha256: Sha256Digest
    dispatch_completion_ack_sha256: Sha256Digest
    runtime_acknowledged: bool
    resulting_state_sha256: Sha256Digest
    _seal: object

    def __init__(self) -> None:
        raise TypeError("read-only reconciliation outcomes are created only by the coordinator")

    def __copy__(self) -> ReadOnlyReconciliationDispatchOutcome:
        raise TypeError("read-only reconciliation outcomes cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> ReadOnlyReconciliationDispatchOutcome:
        del memo
        raise TypeError("read-only reconciliation outcomes cannot be copied")

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("read-only reconciliation outcomes cannot be serialized")


def _create_read_only_reconciliation_dispatch_outcome(
    *,
    window: ReadOnlyReconciliationDispatchWindow,
    dispatch_completion_ack_sha256: Sha256Digest,
    resulting_state: CoordinatorRunState,
) -> ReadOnlyReconciliationDispatchOutcome:
    if (
        type(window) is not ReadOnlyReconciliationDispatchWindow
        or window._seal is not _VALUE_SEAL
        or type(dispatch_completion_ack_sha256) is not Sha256Digest
        or type(resulting_state) is not CoordinatorRunState
    ):
        raise _fail(
            OutcomeCode.INVALID_TYPE, "read-only reconciliation outcome carriers are invalid"
        )
    value = object.__new__(ReadOnlyReconciliationDispatchOutcome)
    for name in (
        "binding",
        "dispatch_sequence",
        "trigger_root_key",
        "trigger_root_sha256",
        "observation_sha256",
        "outcome_ack_sha256",
        "refresh_ack_sha256",
        "refresh_value_sha256",
        "final_portfolio_snapshot_sha256",
        "final_risk_state_sha256",
        "pre_ack_state_sha256",
    ):
        object.__setattr__(value, name, getattr(window, name))
    object.__setattr__(value, "dispatch_completion_ack_sha256", dispatch_completion_ack_sha256)
    object.__setattr__(value, "runtime_acknowledged", True)
    object.__setattr__(
        value, "resulting_state_sha256", coordinator_run_state_digest(resulting_state)
    )
    object.__setattr__(value, "_seal", _VALUE_SEAL)
    return value


def canonical_read_only_reconciliation_dispatch_outcome_bytes(
    outcome: ReadOnlyReconciliationDispatchOutcome,
) -> bytes:
    if (
        type(outcome) is not ReadOnlyReconciliationDispatchOutcome
        or outcome._seal is not _VALUE_SEAL
    ):
        raise _fail(
            OutcomeCode.INVALID_TYPE, "read-only reconciliation outcome must be coordinator-issued"
        )
    binding = outcome.binding
    return _canonical_json(
        {
            "canonicalization": "ea-canonical-json-v1",
            "dispatch_completion_ack_sha256": outcome.dispatch_completion_ack_sha256.value,
            "dispatch_kind": "reconciliation_observation",
            "dispatch_sequence": outcome.dispatch_sequence,
            "final_portfolio_snapshot_sha256": outcome.final_portfolio_snapshot_sha256.value,
            "final_risk_state_sha256": outcome.final_risk_state_sha256.value,
            "lineage_sha256": binding.reference.lineage_sha256.value,
            "manifest_sha256": binding.manifest_sha256.value,
            "observation_sha256": outcome.observation_sha256.value,
            "outcome_ack_sha256": outcome.outcome_ack_sha256.value,
            "pre_ack_state_sha256": outcome.pre_ack_state_sha256.value,
            "refresh_ack_sha256": outcome.refresh_ack_sha256.value,
            "refresh_value_sha256": outcome.refresh_value_sha256.value,
            "resulting_state_sha256": outcome.resulting_state_sha256.value,
            "run_id": binding.reference.run_id.value,
            "runtime_acknowledged": True,
            "schema": "ea.coordinator-read-only-reconciliation-outcome.v1",
            "trigger_root_key": _canonical_completion_v4_reconciliation_root_key_document(
                outcome.trigger_root_key, expected_run_id=binding.reference.run_id.value
            ),
            "trigger_root_sha256": outcome.trigger_root_sha256.value,
        }
    )


def read_only_reconciliation_dispatch_outcome_digest(
    outcome: ReadOnlyReconciliationDispatchOutcome,
) -> Sha256Digest:
    return _framed_digest(
        _READ_ONLY_RECONCILIATION_OUTCOME_DOMAIN,
        canonical_read_only_reconciliation_dispatch_outcome_bytes(outcome),
    )


@final
@dataclass(frozen=True, slots=True, init=False)
class SubmissionAuthorizationAttemptOutcome:
    binding: RunBinding
    dispatch_sequence: int
    trigger_root_sha256: Sha256Digest
    order_id: EconomicId
    execution_request_sha256: Sha256Digest
    authorization_payload_sha256: Sha256Digest
    status: SubmissionAuthorizationAttemptStatus
    logical_key: AuditLogicalKey | None
    acknowledgement_sha256: Sha256Digest | None
    error_code: OutcomeCode | None
    _seal: object

    def __init__(self) -> None:
        raise TypeError("authorization attempt outcomes are created only by their factory")


def create_submission_authorization_attempt_outcome(
    *,
    binding: RunBinding,
    dispatch_sequence: int,
    trigger_root_sha256: Sha256Digest,
    order_id: EconomicId,
    execution_request_sha256: Sha256Digest,
    authorization_payload_sha256: Sha256Digest,
    status: SubmissionAuthorizationAttemptStatus,
    logical_key: AuditLogicalKey | None,
    acknowledgement_sha256: Sha256Digest | None,
    error_code: OutcomeCode | None,
) -> SubmissionAuthorizationAttemptOutcome:
    if (
        type(binding) is not RunBinding
        or type(dispatch_sequence) is not int
        or type(trigger_root_sha256) is not Sha256Digest
        or type(order_id) is not EconomicId
        or type(execution_request_sha256) is not Sha256Digest
        or type(authorization_payload_sha256) is not Sha256Digest
        or type(status) is not SubmissionAuthorizationAttemptStatus
        or (logical_key is not None and type(logical_key) is not AuditLogicalKey)
        or (acknowledgement_sha256 is not None and type(acknowledgement_sha256) is not Sha256Digest)
        or (error_code is not None and type(error_code) is not OutcomeCode)
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "authorization attempt carriers are invalid")
    if not 1 <= dispatch_sequence <= _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "authorization dispatch sequence is outside uint64")
    if (
        order_id.run_id != binding.reference.run_id
        or order_id.owner_kind is not EconomicOwnerKind.EXECUTION_ORDER
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "authorization Order identity conflicts")
    if logical_key is not None and (
        logical_key.record_kind is not AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION
        or logical_key.subject_kind is not AuditSubjectKind.HISTORICAL_EXECUTION_REQUEST
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "authorization logical key conflicts")
    expected_presence = {
        SubmissionAuthorizationAttemptStatus.DENIED: (False, False, True),
        SubmissionAuthorizationAttemptStatus.UNRESOLVED: (True, False, True),
        SubmissionAuthorizationAttemptStatus.AUTHORIZED: (True, True, False),
        SubmissionAuthorizationAttemptStatus.BURNED: (True, True, True),
        SubmissionAuthorizationAttemptStatus.FAILED: (True, False, True),
    }[status]
    actual_presence = (
        logical_key is not None,
        acknowledgement_sha256 is not None,
        error_code is not None,
    )
    if actual_presence != expected_presence:
        raise _fail(OutcomeCode.CONFLICTING_ID, "authorization outcome status evidence conflicts")
    value = object.__new__(SubmissionAuthorizationAttemptOutcome)
    for name, field in {
        "binding": binding,
        "dispatch_sequence": dispatch_sequence,
        "trigger_root_sha256": trigger_root_sha256,
        "order_id": order_id,
        "execution_request_sha256": execution_request_sha256,
        "authorization_payload_sha256": authorization_payload_sha256,
        "status": status,
        "logical_key": logical_key,
        "acknowledgement_sha256": acknowledgement_sha256,
        "error_code": error_code,
    }.items():
        object.__setattr__(value, name, field)
    object.__setattr__(value, "_seal", _VALUE_SEAL)
    return value


def canonical_submission_authorization_attempt_outcome_bytes(
    outcome: SubmissionAuthorizationAttemptOutcome,
) -> bytes:
    if (
        type(outcome) is not SubmissionAuthorizationAttemptOutcome
        or outcome._seal is not _VALUE_SEAL
    ):
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "authorization attempt outcome must be factory-issued",
        )
    binding = outcome.binding
    return _canonical_json(
        {
            "acknowledgement_sha256": (
                None
                if outcome.acknowledgement_sha256 is None
                else outcome.acknowledgement_sha256.value
            ),
            "authorization_payload_sha256": outcome.authorization_payload_sha256.value,
            "canonicalization": "ea-canonical-json-v1",
            "dispatch_sequence": outcome.dispatch_sequence,
            "error_code": None if outcome.error_code is None else outcome.error_code.value,
            "execution_request_sha256": outcome.execution_request_sha256.value,
            "lineage_sha256": binding.reference.lineage_sha256.value,
            "logical_key": (
                None if outcome.logical_key is None else _logical_key_document(outcome.logical_key)
            ),
            "manifest_sha256": binding.manifest_sha256.value,
            "order_id": _economic_id_document(outcome.order_id),
            "run_id": binding.reference.run_id.value,
            "schema": "ea.submission-authorization-attempt-outcome.v1",
            "status": outcome.status.value,
            "trigger_root_sha256": outcome.trigger_root_sha256.value,
        }
    )


def submission_authorization_attempt_outcome_digest(
    outcome: SubmissionAuthorizationAttemptOutcome,
) -> Sha256Digest:
    return _framed_digest(
        _AUTHORIZATION_ATTEMPT_OUTCOME_DOMAIN,
        canonical_submission_authorization_attempt_outcome_bytes(outcome),
    )


def decode_submission_authorization_attempt_outcome_document(
    document: object,
) -> SubmissionAuthorizationAttemptOutcome:
    expected_fields = {
        "acknowledgement_sha256",
        "authorization_payload_sha256",
        "canonicalization",
        "dispatch_sequence",
        "error_code",
        "execution_request_sha256",
        "lineage_sha256",
        "logical_key",
        "manifest_sha256",
        "order_id",
        "run_id",
        "schema",
        "status",
        "trigger_root_sha256",
    }
    if type(document) is not dict or set(document) != expected_fields:
        raise _fail(OutcomeCode.CONFLICTING_ID, "authorization outcome document fields conflict")
    if (
        document["schema"] != "ea.submission-authorization-attempt-outcome.v1"
        or document["canonicalization"] != "ea-canonical-json-v1"
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "authorization outcome schema conflicts")
    order_document = document["order_id"]
    logical_document = document["logical_key"]
    if type(order_document) is not dict or set(order_document) != {
        "owner_kind",
        "owner_sequence",
        "run_id",
    }:
        raise _fail(OutcomeCode.CONFLICTING_ID, "authorization Order document conflicts")
    if logical_document is not None and (
        type(logical_document) is not dict
        or set(logical_document) != {"record_kind", "subject_kind", "subject_sha256"}
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "authorization logical key document conflicts")
    try:
        run_id = RunId(document["run_id"])
        binding = RunBinding(
            RunReference(run_id, Sha256Digest(document["lineage_sha256"])),
            Sha256Digest(document["manifest_sha256"]),
        )
        order_id = EconomicId(
            RunId(order_document["run_id"]),
            EconomicOwnerKind(order_document["owner_kind"]),
            order_document["owner_sequence"],
        )
        logical_key = (
            None
            if logical_document is None
            else AuditLogicalKey(
                AuditRecordKind(logical_document["record_kind"]),
                AuditSubjectKind(logical_document["subject_kind"]),
                Sha256Digest(logical_document["subject_sha256"]),
            )
        )
        acknowledgement_sha256 = (
            None
            if document["acknowledgement_sha256"] is None
            else Sha256Digest(document["acknowledgement_sha256"])
        )
        error_code = None if document["error_code"] is None else OutcomeCode(document["error_code"])
        value = create_submission_authorization_attempt_outcome(
            binding=binding,
            dispatch_sequence=document["dispatch_sequence"],
            trigger_root_sha256=Sha256Digest(document["trigger_root_sha256"]),
            order_id=order_id,
            execution_request_sha256=Sha256Digest(document["execution_request_sha256"]),
            authorization_payload_sha256=Sha256Digest(document["authorization_payload_sha256"]),
            status=SubmissionAuthorizationAttemptStatus(document["status"]),
            logical_key=logical_key,
            acknowledgement_sha256=acknowledgement_sha256,
            error_code=error_code,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "authorization outcome document carriers conflict",
        ) from error
    if json.loads(canonical_submission_authorization_attempt_outcome_bytes(value)) != document:
        raise _fail(OutcomeCode.CONFLICTING_ID, "authorization outcome is not canonical")
    return value


@final
@dataclass(frozen=True, slots=True, init=False)
class CoordinatorRunState:
    binding: RunBinding
    state_version: int
    phase: CoordinatorPhase
    active_dispatch_sequence: int | None
    active_trigger_root_sha256: Sha256Digest | None
    matcher_batch_sha256: Sha256Digest | None
    ordered_ingress_sha256s_sha256: Sha256Digest
    ordered_outcome_ack_sha256s_sha256: Sha256Digest
    missing_audit_logical_keys: tuple[AuditLogicalKey, ...]
    failure_code: OutcomeCode | None
    last_completed_dispatch_sequence: int | None
    last_audit_chain_head_sha256: Sha256Digest
    _seal: object

    def __init__(self) -> None:
        raise TypeError("CoordinatorRunState values are created only by their factory")


def create_coordinator_run_state(
    *,
    binding: RunBinding,
    state_version: int,
    phase: CoordinatorPhase,
    active_dispatch_sequence: int | None,
    active_trigger_root_sha256: Sha256Digest | None,
    matcher_batch_sha256: Sha256Digest | None,
    ordered_ingress_sha256s_sha256: Sha256Digest,
    ordered_outcome_ack_sha256s_sha256: Sha256Digest,
    missing_audit_logical_keys: tuple[AuditLogicalKey, ...],
    failure_code: OutcomeCode | None,
    last_completed_dispatch_sequence: int | None,
    last_audit_chain_head_sha256: Sha256Digest,
) -> CoordinatorRunState:
    if (
        type(binding) is not RunBinding
        or type(state_version) is not int
        or type(phase) is not CoordinatorPhase
        or type(missing_audit_logical_keys) is not tuple
        or any(type(value) is not AuditLogicalKey for value in missing_audit_logical_keys)
        or (failure_code is not None and type(failure_code) is not OutcomeCode)
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "coordinator state carriers are invalid")
    if not 1 <= state_version <= _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "state version must be positive uint64")
    for sequence_value in (active_dispatch_sequence, last_completed_dispatch_sequence):
        if sequence_value is not None and (
            type(sequence_value) is not int or not 1 <= sequence_value <= _MAX_UINT64
        ):
            raise _fail(OutcomeCode.OUT_OF_RANGE, "dispatch sequence is outside uint64")
    for optional_digest in (active_trigger_root_sha256, matcher_batch_sha256):
        if optional_digest is not None and type(optional_digest) is not Sha256Digest:
            raise _fail(OutcomeCode.INVALID_TYPE, "optional coordinator digest is invalid")
    if (active_dispatch_sequence is None) != (active_trigger_root_sha256 is None):
        raise _fail(OutcomeCode.CONFLICTING_ID, "active dispatch identity is incomplete")
    if phase is CoordinatorPhase.FAILING and failure_code is None:
        raise _fail(OutcomeCode.CONFLICTING_ID, "failing state requires a failure code")
    if phase is CoordinatorPhase.ADMITTED and (
        active_dispatch_sequence is not None
        or failure_code is not None
        or last_completed_dispatch_sequence is not None
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "admitted state contains runtime evidence")
    for required_digest in (
        ordered_ingress_sha256s_sha256,
        ordered_outcome_ack_sha256s_sha256,
        last_audit_chain_head_sha256,
    ):
        if type(required_digest) is not Sha256Digest:
            raise _fail(OutcomeCode.INVALID_TYPE, "coordinator state digests must be exact")
    state_value = object.__new__(CoordinatorRunState)
    fields = {
        "binding": binding,
        "state_version": state_version,
        "phase": phase,
        "active_dispatch_sequence": active_dispatch_sequence,
        "active_trigger_root_sha256": active_trigger_root_sha256,
        "matcher_batch_sha256": matcher_batch_sha256,
        "ordered_ingress_sha256s_sha256": ordered_ingress_sha256s_sha256,
        "ordered_outcome_ack_sha256s_sha256": ordered_outcome_ack_sha256s_sha256,
        "missing_audit_logical_keys": missing_audit_logical_keys,
        "failure_code": failure_code,
        "last_completed_dispatch_sequence": last_completed_dispatch_sequence,
        "last_audit_chain_head_sha256": last_audit_chain_head_sha256,
    }
    for name, field in fields.items():
        object.__setattr__(state_value, name, field)
    object.__setattr__(state_value, "_seal", _VALUE_SEAL)
    return state_value


def _logical_key_document(value: AuditLogicalKey) -> dict[str, str]:
    return {
        "record_kind": value.record_kind.value,
        "subject_kind": value.subject_kind.value,
        "subject_sha256": value.subject_sha256.value,
    }


def canonical_coordinator_run_state_bytes(state: CoordinatorRunState) -> bytes:
    if type(state) is not CoordinatorRunState or state._seal is not _VALUE_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "coordinator state must be factory-issued")
    binding = state.binding
    return _canonical_json(
        {
            "active_dispatch_sequence": state.active_dispatch_sequence,
            "active_trigger_root_sha256": (
                None
                if state.active_trigger_root_sha256 is None
                else state.active_trigger_root_sha256.value
            ),
            "canonicalization": "ea-canonical-json-v1",
            "failure_code": None if state.failure_code is None else state.failure_code.value,
            "last_audit_chain_head_sha256": state.last_audit_chain_head_sha256.value,
            "last_completed_dispatch_sequence": state.last_completed_dispatch_sequence,
            "lineage_sha256": binding.reference.lineage_sha256.value,
            "manifest_sha256": binding.manifest_sha256.value,
            "matcher_batch_sha256": (
                None if state.matcher_batch_sha256 is None else state.matcher_batch_sha256.value
            ),
            "missing_audit_logical_keys": [
                _logical_key_document(value) for value in state.missing_audit_logical_keys
            ],
            "ordered_ingress_sha256s_sha256": state.ordered_ingress_sha256s_sha256.value,
            "ordered_outcome_ack_sha256s_sha256": (state.ordered_outcome_ack_sha256s_sha256.value),
            "phase": state.phase.value,
            "run_id": binding.reference.run_id.value,
            "schema": "ea.coordinator-state.v1",
            "state_version": state.state_version,
        }
    )


def coordinator_run_state_digest(state: CoordinatorRunState) -> Sha256Digest:
    return _framed_digest(_STATE_DOMAIN, canonical_coordinator_run_state_bytes(state))


@final
@dataclass(frozen=True, slots=True, init=False)
class PreTerminalCoordinatorState:
    binding: RunBinding
    state_version: int
    terminal_kind: CoordinatorTerminalKind
    last_dispatch_sequence: int
    last_trigger_root_sha256: Sha256Digest
    dispatch_completion_ack_sha256: Sha256Digest
    previous_chain_head_sha256: Sha256Digest
    failure_code: OutcomeCode | None
    _seal: object

    def __init__(self) -> None:
        raise TypeError("pre-terminal states are created only by their factory")


def create_pre_terminal_coordinator_state(
    *,
    binding: RunBinding,
    state_version: int,
    terminal_kind: CoordinatorTerminalKind,
    last_dispatch_sequence: int,
    last_trigger_root_sha256: Sha256Digest,
    dispatch_completion_ack_sha256: Sha256Digest,
    previous_chain_head_sha256: Sha256Digest,
    failure_code: OutcomeCode | None,
) -> PreTerminalCoordinatorState:
    if (
        type(binding) is not RunBinding
        or type(state_version) is not int
        or type(terminal_kind) is not CoordinatorTerminalKind
        or type(last_dispatch_sequence) is not int
        or type(last_trigger_root_sha256) is not Sha256Digest
        or type(dispatch_completion_ack_sha256) is not Sha256Digest
        or type(previous_chain_head_sha256) is not Sha256Digest
        or (failure_code is not None and type(failure_code) is not OutcomeCode)
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "pre-terminal state carriers are invalid")
    if not 1 <= state_version <= _MAX_UINT64 or not 1 <= last_dispatch_sequence <= _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "pre-terminal counters are outside uint64")
    if (terminal_kind is CoordinatorTerminalKind.SUCCESS) != (failure_code is None):
        raise _fail(OutcomeCode.CONFLICTING_ID, "terminal kind conflicts with failure code")
    value = object.__new__(PreTerminalCoordinatorState)
    for name, field in {
        "binding": binding,
        "state_version": state_version,
        "terminal_kind": terminal_kind,
        "last_dispatch_sequence": last_dispatch_sequence,
        "last_trigger_root_sha256": last_trigger_root_sha256,
        "dispatch_completion_ack_sha256": dispatch_completion_ack_sha256,
        "previous_chain_head_sha256": previous_chain_head_sha256,
        "failure_code": failure_code,
    }.items():
        object.__setattr__(value, name, field)
    object.__setattr__(value, "_seal", _VALUE_SEAL)
    return value


def canonical_pre_terminal_coordinator_state_bytes(
    state: PreTerminalCoordinatorState,
) -> bytes:
    if type(state) is not PreTerminalCoordinatorState or state._seal is not _VALUE_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "pre-terminal state must be factory-issued")
    binding = state.binding
    return _canonical_json(
        {
            "canonicalization": "ea-canonical-json-v1",
            "dispatch_completion_ack_sha256": state.dispatch_completion_ack_sha256.value,
            "failure_code": None if state.failure_code is None else state.failure_code.value,
            "last_dispatch_sequence": state.last_dispatch_sequence,
            "last_trigger_root_sha256": state.last_trigger_root_sha256.value,
            "lineage_sha256": binding.reference.lineage_sha256.value,
            "manifest_sha256": binding.manifest_sha256.value,
            "previous_chain_head_sha256": state.previous_chain_head_sha256.value,
            "run_id": binding.reference.run_id.value,
            "schema": "ea.coordinator-pre-terminal-state.v1",
            "state_version": state.state_version,
            "terminal_kind": state.terminal_kind.value,
        }
    )


def pre_terminal_coordinator_state_digest(
    state: PreTerminalCoordinatorState,
) -> Sha256Digest:
    return _framed_digest(
        _PRE_TERMINAL_STATE_DOMAIN,
        canonical_pre_terminal_coordinator_state_bytes(state),
    )


@final
@dataclass(frozen=True, slots=True, init=False)
class TerminalCoordinatorState:
    binding: RunBinding
    state_version: int
    terminal_kind: CoordinatorTerminalKind
    pre_terminal_state_sha256: Sha256Digest
    terminal_record_id: EconomicId
    terminal_ack_sha256: Sha256Digest
    final_chain_head_sha256: Sha256Digest
    failure_code: OutcomeCode | None
    _seal: object

    def __init__(self) -> None:
        raise TypeError("terminal states are created only by their factory")


def create_terminal_coordinator_state(
    *,
    pre_terminal_state: PreTerminalCoordinatorState,
    terminal_acknowledgement: AuditAppendAcknowledgement,
    terminal_payload: bytes | None = None,
) -> TerminalCoordinatorState:
    if type(pre_terminal_state) is not PreTerminalCoordinatorState:
        raise _fail(OutcomeCode.INVALID_TYPE, "pre-terminal state must be exact")
    if terminal_acknowledgement.record_kind is not AuditRecordKind.RUN_TERMINAL:
        raise _fail(OutcomeCode.CONFLICTING_ID, "terminal acknowledgement kind conflicts")
    if terminal_acknowledgement.binding != pre_terminal_state.binding:
        raise _fail(OutcomeCode.CONFLICTING_ID, "terminal acknowledgement binding conflicts")
    payload = (
        canonical_run_terminal_audit_payload(pre_terminal_state)
        if terminal_payload is None
        else terminal_payload
    )
    if type(payload) is not bytes:
        raise _fail(OutcomeCode.INVALID_TYPE, "terminal payload must be exact bytes")
    require_audit_acknowledgement(
        terminal_acknowledgement,
        binding=pre_terminal_state.binding,
        logical_key=AuditLogicalKey(
            AuditRecordKind.RUN_TERMINAL,
            AuditSubjectKind.RUN_TERMINAL_STATE,
            audit_subject_digest(AuditRecordKind.RUN_TERMINAL, payload),
        ),
        canonical_payload=payload,
    )
    if pre_terminal_state.state_version == _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "terminal state version would overflow uint64")
    value = object.__new__(TerminalCoordinatorState)
    object.__setattr__(value, "binding", pre_terminal_state.binding)
    object.__setattr__(value, "state_version", pre_terminal_state.state_version + 1)
    object.__setattr__(value, "terminal_kind", pre_terminal_state.terminal_kind)
    object.__setattr__(
        value,
        "pre_terminal_state_sha256",
        pre_terminal_coordinator_state_digest(pre_terminal_state),
    )
    object.__setattr__(value, "terminal_record_id", terminal_acknowledgement.record_id)
    object.__setattr__(
        value,
        "terminal_ack_sha256",
        audit_append_acknowledgement_digest(terminal_acknowledgement),
    )
    object.__setattr__(value, "final_chain_head_sha256", terminal_acknowledgement.chain_head_sha256)
    object.__setattr__(value, "failure_code", pre_terminal_state.failure_code)
    object.__setattr__(value, "_seal", _VALUE_SEAL)
    return value


def canonical_terminal_coordinator_state_bytes(state: TerminalCoordinatorState) -> bytes:
    if type(state) is not TerminalCoordinatorState or state._seal is not _VALUE_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "terminal state must be factory-issued")
    binding = state.binding
    return _canonical_json(
        {
            "canonicalization": "ea-canonical-json-v1",
            "failure_code": None if state.failure_code is None else state.failure_code.value,
            "final_chain_head_sha256": state.final_chain_head_sha256.value,
            "lineage_sha256": binding.reference.lineage_sha256.value,
            "manifest_sha256": binding.manifest_sha256.value,
            "pre_terminal_state_sha256": state.pre_terminal_state_sha256.value,
            "run_id": binding.reference.run_id.value,
            "schema": "ea.coordinator-terminal-state.v1",
            "state_version": state.state_version,
            "terminal_ack_sha256": state.terminal_ack_sha256.value,
            "terminal_kind": state.terminal_kind.value,
            "terminal_record_id": _economic_id_document(state.terminal_record_id),
        }
    )


def terminal_coordinator_state_digest(state: TerminalCoordinatorState) -> Sha256Digest:
    return _framed_digest(_TERMINAL_STATE_DOMAIN, canonical_terminal_coordinator_state_bytes(state))


@final
@dataclass(frozen=True, slots=True, init=False)
class CoordinatorTerminalOutcome:
    pre_terminal_state_sha256: Sha256Digest
    terminal_record_id: EconomicId
    terminal_ack_sha256: Sha256Digest
    terminal_state_sha256: Sha256Digest
    _seal: object

    def __init__(self) -> None:
        raise TypeError("terminal outcomes are created only by their factory")


def create_coordinator_terminal_outcome(
    *,
    pre_terminal_state: PreTerminalCoordinatorState,
    terminal_acknowledgement: AuditAppendAcknowledgement,
    terminal_state: TerminalCoordinatorState,
    terminal_payload: bytes | None = None,
) -> CoordinatorTerminalOutcome:
    expected = create_terminal_coordinator_state(
        pre_terminal_state=pre_terminal_state,
        terminal_acknowledgement=terminal_acknowledgement,
        terminal_payload=terminal_payload,
    )
    expected_bytes = canonical_terminal_coordinator_state_bytes(expected)
    if expected_bytes != canonical_terminal_coordinator_state_bytes(terminal_state):
        raise _fail(OutcomeCode.CONFLICTING_ID, "terminal state evidence conflicts")
    value = object.__new__(CoordinatorTerminalOutcome)
    object.__setattr__(
        value,
        "pre_terminal_state_sha256",
        pre_terminal_coordinator_state_digest(pre_terminal_state),
    )
    object.__setattr__(value, "terminal_record_id", terminal_acknowledgement.record_id)
    object.__setattr__(
        value,
        "terminal_ack_sha256",
        audit_append_acknowledgement_digest(terminal_acknowledgement),
    )
    object.__setattr__(
        value,
        "terminal_state_sha256",
        terminal_coordinator_state_digest(terminal_state),
    )
    object.__setattr__(value, "_seal", _VALUE_SEAL)
    return value


def canonical_coordinator_terminal_outcome_bytes(outcome: CoordinatorTerminalOutcome) -> bytes:
    if type(outcome) is not CoordinatorTerminalOutcome or outcome._seal is not _VALUE_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "terminal outcome must be factory-issued")
    return _canonical_json(
        {
            "canonicalization": "ea-canonical-json-v1",
            "pre_terminal_state_sha256": outcome.pre_terminal_state_sha256.value,
            "schema": "ea.coordinator-terminal-outcome.v1",
            "terminal_ack_sha256": outcome.terminal_ack_sha256.value,
            "terminal_record_id": _economic_id_document(outcome.terminal_record_id),
            "terminal_state_sha256": outcome.terminal_state_sha256.value,
        }
    )


def coordinator_terminal_outcome_digest(outcome: CoordinatorTerminalOutcome) -> Sha256Digest:
    return _framed_digest(
        _TERMINAL_OUTCOME_DOMAIN,
        canonical_coordinator_terminal_outcome_bytes(outcome),
    )


@final
@dataclass(frozen=True, slots=True, init=False)
class AuditedExecutionFactHandoff:
    dispatch_sequence: int
    ingress_identity: IngressIdentity
    outcome_sha256: Sha256Digest
    batch_ack_sha256: Sha256Digest
    outcome_record_id: EconomicId
    outcome_ack_sha256: Sha256Digest
    fill_id: EconomicId | None
    fill_sha256: Sha256Digest | None
    projection_after_sha256: Sha256Digest | None
    _seal: object

    def __init__(self) -> None:
        raise TypeError("audited handoffs are created only by their factory")


def create_audited_execution_fact_handoff(
    *,
    outcome: ExecutionFactProcessingOutcome,
    batch_acknowledgement: AuditAppendAcknowledgement,
    outcome_acknowledgement: AuditAppendAcknowledgement,
) -> AuditedExecutionFactHandoff:
    if type(outcome) is not ExecutionFactProcessingOutcome:
        raise _fail(OutcomeCode.INVALID_TYPE, "handoff requires an exact processing outcome")
    outcome_payload = canonical_execution_fact_processing_outcome_bytes(outcome)
    outcome_digest = execution_fact_processing_outcome_digest(outcome)
    require_audit_acknowledgement(
        outcome_acknowledgement,
        binding=outcome_acknowledgement.binding,
        logical_key=AuditLogicalKey(
            AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME,
            AuditSubjectKind.EXECUTION_FACT_PROCESSING_OUTCOME,
            outcome_digest,
        ),
        canonical_payload=outcome_payload,
    )
    if (
        batch_acknowledgement.binding != outcome_acknowledgement.binding
        or batch_acknowledgement.record_kind is not AuditRecordKind.MATCHER_DISPATCH_BATCH
        or outcome.runtime_dispatch_sequence < 1
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "handoff audit bindings conflict")
    value = object.__new__(AuditedExecutionFactHandoff)
    object.__setattr__(value, "dispatch_sequence", outcome.runtime_dispatch_sequence)
    object.__setattr__(value, "ingress_identity", outcome.ingress_identity)
    object.__setattr__(value, "outcome_sha256", outcome_digest)
    object.__setattr__(
        value,
        "batch_ack_sha256",
        audit_append_acknowledgement_digest(batch_acknowledgement),
    )
    object.__setattr__(value, "outcome_record_id", outcome_acknowledgement.record_id)
    object.__setattr__(
        value,
        "outcome_ack_sha256",
        audit_append_acknowledgement_digest(outcome_acknowledgement),
    )
    object.__setattr__(value, "fill_id", outcome.fill_id)
    object.__setattr__(value, "fill_sha256", outcome.fill_sha256)
    object.__setattr__(value, "projection_after_sha256", outcome.projection_after_sha256)
    object.__setattr__(value, "_seal", _VALUE_SEAL)
    return value


def canonical_audited_execution_fact_handoff_bytes(
    handoff: AuditedExecutionFactHandoff,
) -> bytes:
    if type(handoff) is not AuditedExecutionFactHandoff or handoff._seal is not _VALUE_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "handoff must be factory-issued")
    return _canonical_json(
        {
            "batch_ack_sha256": handoff.batch_ack_sha256.value,
            "canonicalization": "ea-canonical-json-v1",
            "dispatch_sequence": handoff.dispatch_sequence,
            "fill_id": _economic_id_document(handoff.fill_id),
            "fill_sha256": None if handoff.fill_sha256 is None else handoff.fill_sha256.value,
            "ingress_identity": _ingress_identity_document(handoff.ingress_identity),
            "outcome_ack_sha256": handoff.outcome_ack_sha256.value,
            "outcome_record_id": _economic_id_document(handoff.outcome_record_id),
            "outcome_sha256": handoff.outcome_sha256.value,
            "projection_after_sha256": (
                None
                if handoff.projection_after_sha256 is None
                else handoff.projection_after_sha256.value
            ),
            "schema": "ea.coordinator-audited-fact-handoff.v1",
        }
    )


def audited_execution_fact_handoff_digest(
    handoff: AuditedExecutionFactHandoff,
) -> Sha256Digest:
    return _framed_digest(_HANDOFF_DOMAIN, canonical_audited_execution_fact_handoff_bytes(handoff))


@final
@dataclass(frozen=True, slots=True, init=False)
class CoordinatorDispatchOutcome:
    dispatch_kind: HistoricalDispatchKind
    dispatch_sequence: int
    trigger_root_key: RuntimeRootOrderKey
    trigger_root_sha256: Sha256Digest
    batch_sha256: Sha256Digest
    batch_ack_sha256: Sha256Digest
    handoffs: tuple[AuditedExecutionFactHandoff, ...]
    dispatch_completion_ack_sha256: Sha256Digest
    runtime_acknowledged: bool
    resulting_state_sha256: Sha256Digest
    _seal: object

    def __init__(self) -> None:
        raise TypeError("dispatch outcomes are created only by their factory")


def create_coordinator_dispatch_outcome(
    *,
    batch: HistoricalMatcherDispatchBatch,
    batch_acknowledgement: AuditAppendAcknowledgement,
    handoffs: tuple[AuditedExecutionFactHandoff, ...],
    dispatch_completion_acknowledgement: AuditAppendAcknowledgement,
    runtime_acknowledged: bool,
    resulting_state: CoordinatorRunState,
) -> CoordinatorDispatchOutcome:
    if (
        type(batch) is not HistoricalMatcherDispatchBatch
        or type(handoffs) is not tuple
        or any(type(value) is not AuditedExecutionFactHandoff for value in handoffs)
        or type(runtime_acknowledged) is not bool
        or type(resulting_state) is not CoordinatorRunState
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "dispatch outcome carriers are invalid")
    batch_payload = canonical_matcher_batch_audit_payload(batch_acknowledgement.binding, batch)
    require_audit_acknowledgement(
        batch_acknowledgement,
        binding=batch_acknowledgement.binding,
        logical_key=AuditLogicalKey(
            AuditRecordKind.MATCHER_DISPATCH_BATCH,
            AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
            historical_matcher_dispatch_batch_digest(batch),
        ),
        canonical_payload=batch_payload,
    )
    if (
        dispatch_completion_acknowledgement.record_kind
        is not AuditRecordKind.RUNTIME_DISPATCH_COMPLETED
        or dispatch_completion_acknowledgement.binding != batch_acknowledgement.binding
        or any(value.dispatch_sequence != batch.dispatch_sequence for value in handoffs)
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "dispatch outcome evidence conflicts")
    value = object.__new__(CoordinatorDispatchOutcome)
    object.__setattr__(value, "dispatch_kind", batch.dispatch_kind)
    object.__setattr__(value, "dispatch_sequence", batch.dispatch_sequence)
    object.__setattr__(value, "trigger_root_key", batch.trigger_root_key)
    object.__setattr__(value, "trigger_root_sha256", batch.trigger_root_sha256)
    object.__setattr__(value, "batch_sha256", historical_matcher_dispatch_batch_digest(batch))
    object.__setattr__(
        value,
        "batch_ack_sha256",
        audit_append_acknowledgement_digest(batch_acknowledgement),
    )
    object.__setattr__(value, "handoffs", handoffs)
    object.__setattr__(
        value,
        "dispatch_completion_ack_sha256",
        audit_append_acknowledgement_digest(dispatch_completion_acknowledgement),
    )
    object.__setattr__(value, "runtime_acknowledged", runtime_acknowledged)
    object.__setattr__(
        value, "resulting_state_sha256", coordinator_run_state_digest(resulting_state)
    )
    object.__setattr__(value, "_seal", _VALUE_SEAL)
    return value


def canonical_coordinator_dispatch_outcome_bytes(
    outcome: CoordinatorDispatchOutcome,
) -> bytes:
    if type(outcome) is not CoordinatorDispatchOutcome or outcome._seal is not _VALUE_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "dispatch outcome must be factory-issued")
    handoff_digests = tuple(
        audited_execution_fact_handoff_digest(value) for value in outcome.handoffs
    )
    return _canonical_json(
        {
            "batch_ack_sha256": outcome.batch_ack_sha256.value,
            "batch_sha256": outcome.batch_sha256.value,
            "canonicalization": "ea-canonical-json-v1",
            "dispatch_completion_ack_sha256": (outcome.dispatch_completion_ack_sha256.value),
            "dispatch_kind": outcome.dispatch_kind.value,
            "dispatch_sequence": outcome.dispatch_sequence,
            "handoff_count": len(handoff_digests),
            "ordered_handoff_sha256s_sha256": ordered_digest_tuple(
                ORDERED_HANDOFF_DIGEST_DOMAIN,
                handoff_digests,
            ).value,
            "resulting_state_sha256": outcome.resulting_state_sha256.value,
            "runtime_acknowledged": outcome.runtime_acknowledged,
            "schema": "ea.coordinator-dispatch-outcome.v1",
            "trigger_root_key": runtime_root_order_key_document(outcome.trigger_root_key),
            "trigger_root_sha256": outcome.trigger_root_sha256.value,
        }
    )


def coordinator_dispatch_outcome_digest(
    outcome: CoordinatorDispatchOutcome,
) -> Sha256Digest:
    return _framed_digest(
        _DISPATCH_OUTCOME_DOMAIN,
        canonical_coordinator_dispatch_outcome_bytes(outcome),
    )


type LifecycleDispatchWindow = ActiveDispatchWindow | ReadOnlyReconciliationDispatchWindow
type LifecycleDispatchOutcome = CoordinatorDispatchOutcome | ReadOnlyReconciliationDispatchOutcome


def canonical_matcher_batch_audit_payload(
    binding: RunBinding,
    batch: HistoricalMatcherDispatchBatch,
) -> bytes:
    if type(binding) is not RunBinding or type(batch) is not HistoricalMatcherDispatchBatch:
        raise _fail(OutcomeCode.INVALID_TYPE, "batch audit payload carriers are invalid")
    if batch.run_id != binding.reference.run_id:
        raise _fail(OutcomeCode.CONFLICTING_ID, "batch run binding conflicts")
    return _canonical_json(
        {
            "batch_sha256": historical_matcher_dispatch_batch_digest(batch).value,
            "canonicalization": "ea-canonical-json-v1",
            "dispatch_kind": batch.dispatch_kind.value,
            "dispatch_sequence": batch.dispatch_sequence,
            "ingress_count": len(batch.ingress_sha256s),
            "ordered_ingress_sha256s_sha256": ordered_digest_tuple(
                ORDERED_INGRESS_DIGEST_DOMAIN,
                batch.ingress_sha256s,
            ).value,
            "run_id": binding.reference.run_id.value,
            "schema": "ea.audit-matcher-dispatch-batch.v1",
            "trigger_root_key": runtime_root_order_key_document(batch.trigger_root_key),
            "trigger_root_sha256": batch.trigger_root_sha256.value,
        }
    )


def canonical_dispatch_completed_audit_payload(
    *,
    binding: RunBinding,
    batch: HistoricalMatcherDispatchBatch,
    outcome_acknowledgements: tuple[AuditAppendAcknowledgement, ...],
    pre_ack_state_sha256: Sha256Digest,
    authorization_attempt_outcome: SubmissionAuthorizationAttemptOutcome | None = None,
    submission_receipts: tuple[HistoricalSubmissionReceipt, ...] = (),
) -> bytes:
    if (
        type(binding) is not RunBinding
        or type(batch) is not HistoricalMatcherDispatchBatch
        or type(outcome_acknowledgements) is not tuple
        or type(pre_ack_state_sha256) is not Sha256Digest
        or (
            authorization_attempt_outcome is not None
            and type(authorization_attempt_outcome) is not SubmissionAuthorizationAttemptOutcome
        )
        or type(submission_receipts) is not tuple
        or any(type(value) is not HistoricalSubmissionReceipt for value in submission_receipts)
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "completion payload carriers are invalid")
    if len(submission_receipts) > 1:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "completion admits at most one submission receipt")
    if authorization_attempt_outcome is not None and (
        authorization_attempt_outcome.binding != binding
        or authorization_attempt_outcome.dispatch_sequence != batch.dispatch_sequence
        or authorization_attempt_outcome.trigger_root_sha256 != batch.trigger_root_sha256
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "completion authorization frontier conflicts")
    authorized = (
        authorization_attempt_outcome is not None
        and authorization_attempt_outcome.status is SubmissionAuthorizationAttemptStatus.AUTHORIZED
    )
    if authorized != (len(submission_receipts) == 1):
        raise _fail(OutcomeCode.CONFLICTING_ID, "completion authorization receipt bijection fails")
    if submission_receipts:
        receipt = submission_receipts[0]
        assert authorization_attempt_outcome is not None
        if (
            receipt.run_id != binding.reference.run_id
            or receipt.dispatch_sequence != batch.dispatch_sequence
            or receipt.causal_market_sha256 != batch.trigger_root_sha256
            or receipt.order_id != authorization_attempt_outcome.order_id
            or receipt.execution_request_sha256
            != authorization_attempt_outcome.execution_request_sha256
            or receipt.audit_acknowledgement_sha256
            != authorization_attempt_outcome.acknowledgement_sha256
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "completion submission receipt conflicts")
    if batch.dispatch_kind is HistoricalDispatchKind.END_OF_RUN and (
        authorization_attempt_outcome is not None or submission_receipts
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "bounded-end completion cannot submit")
    ack_digests = tuple(
        audit_append_acknowledgement_digest(value) for value in outcome_acknowledgements
    )
    attempt_document = (
        None
        if authorization_attempt_outcome is None
        else json.loads(
            canonical_submission_authorization_attempt_outcome_bytes(authorization_attempt_outcome)
        )
    )
    receipt_digests = tuple(
        historical_submission_receipt_digest(value) for value in submission_receipts
    )
    return _canonical_json(
        {
            "authorization_attempt_count": (0 if authorization_attempt_outcome is None else 1),
            "authorization_attempt_outcome": attempt_document,
            "authorization_attempt_outcome_sha256": (
                None
                if authorization_attempt_outcome is None
                else submission_authorization_attempt_outcome_digest(
                    authorization_attempt_outcome
                ).value
            ),
            "batch_sha256": historical_matcher_dispatch_batch_digest(batch).value,
            "canonicalization": "ea-canonical-json-v1",
            "dispatch_kind": batch.dispatch_kind.value,
            "dispatch_sequence": batch.dispatch_sequence,
            "outcome_count": len(ack_digests),
            "ordered_outcome_ack_sha256s_sha256": ordered_digest_tuple(
                ORDERED_OUTCOME_ACK_DIGEST_DOMAIN,
                ack_digests,
            ).value,
            "pre_ack_state_sha256": pre_ack_state_sha256.value,
            "run_id": binding.reference.run_id.value,
            "schema": "ea.audit-dispatch-completed.v2",
            "submission_count": len(receipt_digests),
            "ordered_submission_receipt_sha256s_sha256": ordered_digest_tuple(
                ORDERED_SUBMISSION_RECEIPT_DIGEST_DOMAIN,
                receipt_digests,
            ).value,
            "trigger_root_key": runtime_root_order_key_document(batch.trigger_root_key),
            "trigger_root_sha256": batch.trigger_root_sha256.value,
        }
    )


def canonical_dispatch_completed_v3_audit_payload(
    *,
    binding: RunBinding,
    batch: HistoricalMatcherDispatchBatch,
    outcome_acknowledgements: tuple[AuditAppendAcknowledgement, ...],
    pre_ack_state_sha256: Sha256Digest,
    ledger_outcome_acknowledgements: tuple[AuditAppendAcknowledgement, ...],
    final_portfolio_snapshot_sha256: Sha256Digest,
    final_risk_state_sha256: Sha256Digest,
    authorization_attempt_outcome: SubmissionAuthorizationAttemptOutcome | None = None,
    submission_receipts: tuple[HistoricalSubmissionReceipt, ...] = (),
) -> bytes:
    """Build completion-v3 with the ledger and final publication frontiers."""
    if type(ledger_outcome_acknowledgements) is not tuple or any(
        type(value) is not AuditAppendAcknowledgement for value in ledger_outcome_acknowledgements
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "completion ledger acknowledgements are invalid")
    if any(
        value.binding != binding
        or value.record_kind is not AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME
        or value.subject_kind is not AuditSubjectKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME
        for value in ledger_outcome_acknowledgements
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "completion ledger acknowledgements conflict")
    if (
        type(final_portfolio_snapshot_sha256) is not Sha256Digest
        or type(final_risk_state_sha256) is not Sha256Digest
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "completion final frontier digests must be exact")
    base_document = json.loads(
        canonical_dispatch_completed_audit_payload(
            binding=binding,
            batch=batch,
            outcome_acknowledgements=outcome_acknowledgements,
            pre_ack_state_sha256=pre_ack_state_sha256,
            authorization_attempt_outcome=authorization_attempt_outcome,
            submission_receipts=submission_receipts,
        )
    )
    ledger_digests = tuple(
        audit_append_acknowledgement_digest(value) for value in ledger_outcome_acknowledgements
    )
    document = {
        **base_document,
        "final_portfolio_snapshot_sha256": final_portfolio_snapshot_sha256.value,
        "final_risk_state_sha256": final_risk_state_sha256.value,
        "ledger_outcome_count": len(ledger_digests),
        "ordered_ledger_ack_sha256s_sha256": ordered_digest_tuple(
            ORDERED_LEDGER_ACK_DIGEST_DOMAIN,
            ledger_digests,
        ).value,
        "schema": "ea.audit-dispatch-completed.v3",
    }
    return _canonical_json(document)


def canonical_dispatch_completed_v4_audit_payload(
    *,
    binding: RunBinding,
    dispatch_sequence: int,
    trigger_root_key: RuntimeRootOrderKey,
    trigger_root_sha256: Sha256Digest,
    observation_sha256: Sha256Digest,
    outcome_acknowledgement: AuditAppendAcknowledgement,
    refresh_acknowledgement: AuditAppendAcknowledgement,
    refresh_value_sha256: Sha256Digest,
    final_portfolio_snapshot_sha256: Sha256Digest,
    final_risk_state_sha256: Sha256Digest,
    pre_ack_state_sha256: Sha256Digest,
) -> bytes:
    """Build the read-only reconciliation completion-v4 frontier."""
    if (
        type(binding) is not RunBinding
        or type(dispatch_sequence) is not int
        or type(trigger_root_key) is not RuntimeRootOrderKey
        or any(
            type(value) is not Sha256Digest
            for value in (
                trigger_root_sha256,
                observation_sha256,
                refresh_value_sha256,
                final_portfolio_snapshot_sha256,
                final_risk_state_sha256,
                pre_ack_state_sha256,
            )
        )
        or type(outcome_acknowledgement) is not AuditAppendAcknowledgement
        or type(refresh_acknowledgement) is not AuditAppendAcknowledgement
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "completion-v4 carriers are invalid")
    if not 1 <= dispatch_sequence <= _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "completion-v4 dispatch sequence is outside uint64")
    root_key_document = _canonical_completion_v4_reconciliation_root_key_document(
        trigger_root_key, expected_run_id=binding.reference.run_id.value
    )
    if trigger_root_sha256 != observation_sha256:
        raise _fail(OutcomeCode.CONFLICTING_ID, "completion-v4 root digest conflicts")
    if (
        outcome_acknowledgement.binding != binding
        or outcome_acknowledgement.record_kind
        is not AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME
        or outcome_acknowledgement.subject_kind is not AuditSubjectKind.RECONCILIATION_OUTCOME
        or refresh_acknowledgement.binding != binding
        or refresh_acknowledgement.record_kind is not AuditRecordKind.RISK_PORTFOLIO_REFRESH
        or refresh_acknowledgement.subject_kind is not AuditSubjectKind.PORTFOLIO_RISK_REFRESH
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "completion-v4 acknowledgement frontier conflicts")
    outcome_ack_sha256 = audit_append_acknowledgement_digest(outcome_acknowledgement)
    refresh_ack_sha256 = audit_append_acknowledgement_digest(refresh_acknowledgement)
    empty_ledger = ordered_digest_tuple(ORDERED_LEDGER_ACK_DIGEST_DOMAIN, ())
    empty_submissions = ordered_digest_tuple(ORDERED_SUBMISSION_RECEIPT_DIGEST_DOMAIN, ())
    return _canonical_json(
        {
            "authorization_allowed": False,
            "authorization_attempt_count": 0,
            "authorization_attempt_outcome": None,
            "authorization_attempt_outcome_sha256": None,
            "batch_ack_sha256": None,
            "batch_sha256": None,
            "canonicalization": "ea-canonical-json-v1",
            "dispatch_kind": "reconciliation_observation",
            "dispatch_sequence": dispatch_sequence,
            "final_portfolio_snapshot_sha256": final_portfolio_snapshot_sha256.value,
            "final_risk_state_sha256": final_risk_state_sha256.value,
            "ledger_outcome_count": 0,
            "observation_sha256": observation_sha256.value,
            "ordered_ledger_ack_sha256s_sha256": empty_ledger.value,
            "ordered_outcome_ack_sha256s_sha256": outcome_ack_sha256.value,
            "ordered_submission_receipt_sha256s_sha256": empty_submissions.value,
            "outcome_acknowledgement_sha256": outcome_ack_sha256.value,
            "outcome_count": 1,
            "pre_ack_state_sha256": pre_ack_state_sha256.value,
            "refresh_acknowledgement_sha256": refresh_ack_sha256.value,
            "refresh_value_sha256": refresh_value_sha256.value,
            "run_id": binding.reference.run_id.value,
            "schema": "ea.audit-dispatch-completed.v4",
            "submission_count": 0,
            "trigger_root_key": root_key_document,
            "trigger_root_sha256": trigger_root_sha256.value,
        }
    )


def canonical_run_terminal_v2_audit_payload(
    state: PreTerminalCoordinatorState,
    *,
    final_published_snapshot_sha256: Sha256Digest,
    final_risk_refresh_sha256: Sha256Digest,
    open_reconciliation_ref_aggregate_sha256: Sha256Digest,
    ordered_reconciliation_frontier_sha256s_sha256: Sha256Digest,
) -> bytes:
    """Build the ADR 0022 terminal-v2 record binding the final publication frontier."""
    for digest in (
        final_published_snapshot_sha256,
        final_risk_refresh_sha256,
        open_reconciliation_ref_aggregate_sha256,
        ordered_reconciliation_frontier_sha256s_sha256,
    ):
        if type(digest) is not Sha256Digest:
            raise _fail(OutcomeCode.INVALID_TYPE, "terminal frontier digests must be exact")
    base_document = json.loads(canonical_run_terminal_audit_payload(state))
    document = {
        **base_document,
        "final_published_snapshot_sha256": final_published_snapshot_sha256.value,
        "final_risk_refresh_sha256": final_risk_refresh_sha256.value,
        "open_reconciliation_ref_aggregate_sha256": (
            open_reconciliation_ref_aggregate_sha256.value
        ),
        "ordered_reconciliation_frontier_sha256s_sha256": (
            ordered_reconciliation_frontier_sha256s_sha256.value
        ),
        "schema": "ea.audit-run-terminal.v2",
    }
    return _canonical_json(document)


def dispatch_completed_subject_digest(canonical_payload: bytes) -> Sha256Digest:
    return audit_subject_digest(AuditRecordKind.RUNTIME_DISPATCH_COMPLETED, canonical_payload)


def canonical_failing_safety_audit_payload(
    *,
    binding: RunBinding,
    previous_state_sha256: Sha256Digest,
    failing_state_sha256: Sha256Digest,
    failure_code: OutcomeCode,
    failed_logical_key: AuditLogicalKey,
    dispatch_sequence: int,
    trigger_root_sha256: Sha256Digest,
) -> bytes:
    if (
        type(binding) is not RunBinding
        or type(previous_state_sha256) is not Sha256Digest
        or type(failing_state_sha256) is not Sha256Digest
        or type(failure_code) is not OutcomeCode
        or type(failed_logical_key) is not AuditLogicalKey
        or type(dispatch_sequence) is not int
        or type(trigger_root_sha256) is not Sha256Digest
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "failing-safety payload carriers are invalid")
    if not 1 <= dispatch_sequence <= _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "dispatch sequence is outside uint64")
    return _canonical_json(
        {
            "canonicalization": "ea-canonical-json-v1",
            "dispatch_sequence": dispatch_sequence,
            "failed_record_kind": failed_logical_key.record_kind.value,
            "failed_subject_kind": failed_logical_key.subject_kind.value,
            "failed_subject_sha256": failed_logical_key.subject_sha256.value,
            "failing_state_sha256": failing_state_sha256.value,
            "failure_code": failure_code.value,
            "previous_state_sha256": previous_state_sha256.value,
            "run_id": binding.reference.run_id.value,
            "schema": "ea.audit-failing-safety.v1",
            "trigger_root_sha256": trigger_root_sha256.value,
        }
    )


def canonical_run_terminal_audit_payload(state: PreTerminalCoordinatorState) -> bytes:
    if type(state) is not PreTerminalCoordinatorState or state._seal is not _VALUE_SEAL:
        raise _fail(OutcomeCode.INVALID_TYPE, "terminal payload requires pre-terminal state")
    return _canonical_json(
        {
            "canonicalization": "ea-canonical-json-v1",
            "last_dispatch_sequence": state.last_dispatch_sequence,
            "last_trigger_root_sha256": state.last_trigger_root_sha256.value,
            "pre_terminal_state_sha256": pre_terminal_coordinator_state_digest(state).value,
            "previous_chain_head_sha256": state.previous_chain_head_sha256.value,
            "run_id": state.binding.reference.run_id.value,
            "schema": "ea.audit-run-terminal.v1",
            "terminal_kind": state.terminal_kind.value,
        }
    )
