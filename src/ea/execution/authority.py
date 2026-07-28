"""Single deterministic Order-creation authority from Accepted ADR 0008/0011."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, NoReturn, Protocol, cast, final

from ea.core.economics import CanonicalDecimal, EconomicValidationError
from ea.core.execution import InstrumentExecutionSpecSet, instrument_spec_set_digest
from ea.core.execution_identity import (
    EconomicId,
    EconomicOwnerKind,
    ExecutionIdentityError,
)
from ea.core.execution_messages import (
    ExecutionApproval,
    ExecutionMessageError,
    ExecutionPolicyRef,
    Order,
    OrderIntent,
    OrderKind,
    RiskDecision,
    RiskDecisionKind,
    TargetLineageRef,
    TimeInForce,
    canonical_execution_approval_bytes,
    canonical_execution_request_bytes,
    canonical_order_bytes,
    canonical_order_intent_bytes,
    canonical_risk_decision_bytes,
    create_order,
    decode_execution_approval,
    decode_order_intent,
    decode_risk_decision,
    execution_approval_digest,
    execution_request_digest,
    order_client_submission_key,
    order_digest,
    order_intent_digest,
    risk_decision_digest,
)
from ea.core.outcomes import OutcomeCode
from ea.core.risk import (
    InstrumentRiskLimit,
    Phase1RiskPolicy,
    RiskContractError,
    RiskEvaluationEvidence,
    RiskEvaluationResult,
    RiskPolicyId,
    RiskReasonCode,
    canonical_risk_evaluation_evidence_bytes,
    phase1_risk_policy_digest,
)
from ea.core.run import RunContractError, RunId, Sha256Digest
from ea.core.time import TimeValidationError, require_utc

_MAX_UINT64 = (1 << 64) - 1
_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.NON_FINITE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.NOT_QUANTIZED,
        OutcomeCode.PRICE_DOMAIN,
        OutcomeCode.ARITHMETIC_OVERFLOW,
        OutcomeCode.CONFLICTING_ID,
    }
)
_EXECUTABLE_REASONS = {
    RiskDecisionKind.ALLOW: frozenset({RiskReasonCode.WITHIN_LIMITS}),
    RiskDecisionKind.RESIZE: frozenset(
        {
            RiskReasonCode.RESIZED_ORDER_LIMIT,
            RiskReasonCode.RESIZED_POSITION_LIMIT,
            RiskReasonCode.RESIZED_ORDER_AND_POSITION_LIMITS,
        }
    ),
}


class ExecutionAuthorityError(ValueError):
    """Structured fail-closed public Order-authority error."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _ERROR_CODES:
            raise TypeError("execution authority errors require an exact permitted OutcomeCode")
        self.code = code
        super().__init__(message)


class RiskResultIssuanceVerifier(Protocol):
    """Consumer-owned read-only proof that Risk issued one canonical result."""

    @property
    def run_id(self) -> RunId: ...

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet: ...

    @property
    def execution_policy(self) -> ExecutionPolicyRef: ...

    @property
    def policy(self) -> Phase1RiskPolicy: ...

    def has_issued_result(
        self,
        *,
        intent_id: EconomicId,
        canonical_intent_bytes: bytes,
        canonical_decision_bytes: bytes,
        canonical_evidence_bytes: bytes,
    ) -> bool: ...


class _IssuanceVerifierFailure(Exception):
    """Preserve an unexpected verifier exception across public error translation."""

    error: Exception

    def __init__(self, error: Exception) -> None:
        self.error = error
        super().__init__(str(error))


@dataclass(frozen=True, slots=True)
class _SubmittedInput:
    intent: OrderIntent
    result: RiskEvaluationResult
    decision: RiskDecision
    evidence: RiskEvaluationEvidence
    approval: ExecutionApproval | None
    intent_bytes: bytes
    decision_bytes: bytes
    evidence_bytes: bytes
    approval_bytes: bytes | None


@dataclass(frozen=True, slots=True)
class _OrderRecord:
    intent_bytes: bytes
    decision_bytes: bytes
    evidence_bytes: bytes
    order: Order
    order_bytes: bytes
    order_sha256: Sha256Digest
    execution_request_bytes: bytes
    execution_request_sha256: Sha256Digest
    client_submission_key: Sha256Digest


@dataclass(frozen=True, slots=True)
class _OrderAuthorityState:
    order_next: int | None
    approval_index: Mapping[EconomicId, _OrderRecord]
    intent_index: Mapping[EconomicId, _OrderRecord]
    decision_index: Mapping[EconomicId, _OrderRecord]
    order_id_index: Mapping[EconomicId, _OrderRecord]
    client_submission_key_index: Mapping[Sha256Digest, _OrderRecord]
    orders: tuple[Order, ...]


@final
class Phase1OrderAuthority:
    """The sole mutable owner of Phase 1 approval consumption and Order IDs."""

    _run_id: RunId
    _spec_set: InstrumentExecutionSpecSet
    _spec_set_sha256: Sha256Digest
    _execution_policy: ExecutionPolicyRef
    _risk_policy: Phase1RiskPolicy
    _risk_policy_sha256: Sha256Digest
    _risk_result_verifier: RiskResultIssuanceVerifier
    _state: _OrderAuthorityState

    __slots__ = (
        "_execution_policy",
        "_risk_policy",
        "_risk_policy_sha256",
        "_risk_result_verifier",
        "_run_id",
        "_spec_set",
        "_spec_set_sha256",
        "_state",
    )

    def __init__(self) -> None:
        raise TypeError(
            "Phase1OrderAuthority values are created only by create_phase1_order_authority"
        )

    @property
    def run_id(self) -> RunId:
        return self._run_id

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet:
        return self._spec_set

    @property
    def execution_policy(self) -> ExecutionPolicyRef:
        return self._execution_policy

    @property
    def risk_policy(self) -> Phase1RiskPolicy:
        return self._risk_policy

    @property
    def next_order_sequence(self) -> int | None:
        return self._state.order_next

    @property
    def orders(self) -> tuple[Order, ...]:
        return self._state.orders

    def resolve_issued_order_by_id(self, order_id: EconomicId) -> Order | None:
        """Return one exact issued Order by canonical Order ID without mutation."""
        if type(order_id) is not EconomicId:
            raise ExecutionAuthorityError(
                OutcomeCode.INVALID_TYPE,
                "order_id must be an exact EconomicId",
            )
        record = self._state.order_id_index.get(order_id)
        return None if record is None else record.order

    def resolve_issued_order_by_client_submission_key(
        self,
        client_submission_key: Sha256Digest,
    ) -> Order | None:
        """Return one exact issued Order by stable client key without mutation."""
        if type(client_submission_key) is not Sha256Digest:
            raise ExecutionAuthorityError(
                OutcomeCode.INVALID_TYPE,
                "client_submission_key must be an exact Sha256Digest",
            )
        record = self._state.client_submission_key_index.get(client_submission_key)
        return None if record is None else record.order

    def create_order(
        self,
        intent: OrderIntent,
        result: RiskEvaluationResult,
    ) -> Order:
        """Consume one coherent approval exactly once and publish one canonical Order."""
        try:
            submitted = _materialize_submitted_input(intent, result)
            replay = self._classify_replay(submitted)
            if replay is not None:
                return replay

            reconstructed_intent = decode_order_intent(
                submitted.intent_bytes,
                spec_set=self._spec_set,
                target_lineage=submitted.intent.target_lineage,
                execution_policy=submitted.intent.execution_policy,
            )
            if reconstructed_intent != submitted.intent:
                raise ExecutionAuthorityError(
                    OutcomeCode.CONFLICTING_ID,
                    "submitted intent fields differ from their canonical reconstruction",
                )
            reconstructed_decision = decode_risk_decision(
                submitted.decision_bytes,
                intent=reconstructed_intent,
                spec_set=self._spec_set,
            )
            if (
                reconstructed_decision.kind not in (RiskDecisionKind.ALLOW, RiskDecisionKind.RESIZE)
                or reconstructed_decision.approval is None
                or submitted.approval is None
                or submitted.approval_bytes is None
            ):
                raise ExecutionAuthorityError(
                    OutcomeCode.CONFLICTING_ID,
                    "only a complete executable risk result can create an Order",
                )
            reconstructed_approval = decode_execution_approval(
                submitted.approval_bytes,
                intent=reconstructed_intent,
                decision=reconstructed_decision,
            )
            if (
                reconstructed_decision != submitted.decision
                or reconstructed_approval != submitted.approval
            ):
                raise ExecutionAuthorityError(
                    OutcomeCode.CONFLICTING_ID,
                    "submitted risk fields differ from their canonical reconstruction",
                )
            _require_evidence(
                submitted.evidence,
                intent=reconstructed_intent,
                decision=reconstructed_decision,
                approval=reconstructed_approval,
                risk_policy=self._risk_policy,
                risk_policy_sha256=self._risk_policy_sha256,
            )
            self._require_frozen_lineage(
                reconstructed_intent,
                submitted.evidence,
            )
            _require_static_policy_proof(
                intent=reconstructed_intent,
                decision=reconstructed_decision,
                evidence=submitted.evidence,
                approval=reconstructed_approval,
                risk_policy=self._risk_policy,
            )
            _require_issued_result(
                self._risk_result_verifier,
                submitted=submitted,
            )

            order_before = self._state.order_next
            if order_before is None:
                raise ExecutionAuthorityError(
                    OutcomeCode.OUT_OF_RANGE,
                    "execution Order sequence is exhausted",
                )
            order_after = _advance(order_before)
            order_id = EconomicId(
                self._run_id,
                EconomicOwnerKind.EXECUTION_ORDER,
                order_before,
            )
            order = create_order(
                order_id=order_id,
                intent=reconstructed_intent,
                decision=reconstructed_decision,
                spec_set=self._spec_set,
            )
            record = _make_record(
                submitted=submitted,
                order=order,
            )

            approval_index = _copy_approval_index(self._state.approval_index)
            intent_index = _copy_intent_index(self._state.intent_index)
            decision_index = _copy_decision_index(self._state.decision_index)
            order_id_index = _copy_order_id_index(self._state.order_id_index)
            client_key_index = _copy_client_submission_key_index(
                self._state.client_submission_key_index
            )
            _insert_approval_record(
                approval_index,
                reconstructed_approval.approval_id,
                record,
            )
            _insert_intent_record(
                intent_index,
                reconstructed_intent.intent_id,
                record,
            )
            _insert_decision_record(
                decision_index,
                reconstructed_decision.decision_id,
                record,
            )
            _insert_order_id_record(order_id_index, order.order_id, record)
            _insert_client_submission_key_record(
                client_key_index,
                record.client_submission_key,
                record,
            )
            next_state = _OrderAuthorityState(
                order_next=order_after,
                approval_index=_freeze_approval_index(approval_index),
                intent_index=_freeze_intent_index(intent_index),
                decision_index=_freeze_decision_index(decision_index),
                order_id_index=_freeze_order_id_index(order_id_index),
                client_submission_key_index=_freeze_client_submission_key_index(client_key_index),
                orders=(*self._state.orders, order),
            )
            _preflight_public_record(record)
            _preflight_candidate_state(
                next_state,
                approval_id=reconstructed_approval.approval_id,
                intent_id=reconstructed_intent.intent_id,
                decision_id=reconstructed_decision.decision_id,
                record=record,
            )
            self._state = next_state
            return order
        except ExecutionAuthorityError:
            raise
        except _IssuanceVerifierFailure as failure:
            raise failure.error from None
        except (
            EconomicValidationError,
            ExecutionIdentityError,
            ExecutionMessageError,
            RiskContractError,
            RunContractError,
            TimeValidationError,
        ) as error:
            _raise_public(error)
        except (AttributeError, TypeError) as error:
            raise ExecutionAuthorityError(
                OutcomeCode.INVALID_TYPE,
                "submitted execution carrier is structurally incomplete",
            ) from error

    def _classify_replay(self, submitted: _SubmittedInput) -> Order | None:
        intent_key = (
            submitted.intent.intent_id if type(submitted.intent.intent_id) is EconomicId else None
        )
        decision_key = (
            submitted.decision.decision_id
            if type(submitted.decision.decision_id) is EconomicId
            else None
        )
        approval_key = (
            submitted.approval.approval_id
            if submitted.approval is not None and type(submitted.approval.approval_id) is EconomicId
            else None
        )
        approval_record = (
            None if approval_key is None else self._state.approval_index.get(approval_key)
        )
        intent_record = None if intent_key is None else self._state.intent_index.get(intent_key)
        decision_record = (
            None if decision_key is None else self._state.decision_index.get(decision_key)
        )
        if approval_record is intent_record is decision_record is None:
            return None
        if (
            approval_key is not None
            and intent_key is not None
            and decision_key is not None
            and approval_record is not None
            and approval_record is intent_record
            and approval_record is decision_record
            and approval_record.intent_bytes == submitted.intent_bytes
            and approval_record.decision_bytes == submitted.decision_bytes
            and approval_record.evidence_bytes == submitted.evidence_bytes
        ):
            return approval_record.order
        raise ExecutionAuthorityError(
            OutcomeCode.CONFLICTING_ID,
            "an execution consumption identity is occupied by conflicting canonical evidence",
        )

    def _require_frozen_lineage(
        self,
        intent: OrderIntent,
        evidence: RiskEvaluationEvidence,
    ) -> None:
        current_spec_sha256 = instrument_spec_set_digest(self._spec_set)
        current_risk_sha256 = phase1_risk_policy_digest(self._risk_policy)
        if (
            intent.run_id != self._run_id
            or intent.instrument_spec_set_id != self._spec_set.identifier
            or intent.instrument_spec_set_sha256 != self._spec_set_sha256
            or current_spec_sha256 != self._spec_set_sha256
            or intent.execution_policy != self._execution_policy
            or evidence.policy_id != self._risk_policy.policy_id
            or evidence.policy_sha256 != self._risk_policy_sha256
            or current_risk_sha256 != self._risk_policy_sha256
            or self._risk_policy.instrument_spec_set_id != self._spec_set.identifier
            or self._risk_policy.instrument_spec_set_sha256 != self._spec_set_sha256
            or self._risk_policy.execution_policy != self._execution_policy
        ):
            raise ExecutionAuthorityError(
                OutcomeCode.CONFLICTING_ID,
                "execution authority lineage conflicts with the supplied result",
            )


def create_phase1_order_authority(
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    execution_policy: ExecutionPolicyRef,
    risk_policy: Phase1RiskPolicy,
    risk_result_verifier: RiskResultIssuanceVerifier,
) -> Phase1OrderAuthority:
    """Create one run- and contract-bound Order authority at sequence one."""
    try:
        if type(run_id) is not RunId:
            raise ExecutionAuthorityError(OutcomeCode.INVALID_TYPE, "run_id must be exact")
        if type(spec_set) is not InstrumentExecutionSpecSet:
            raise ExecutionAuthorityError(OutcomeCode.INVALID_TYPE, "spec_set must be exact")
        if type(execution_policy) is not ExecutionPolicyRef:
            raise ExecutionAuthorityError(
                OutcomeCode.INVALID_TYPE,
                "execution_policy must be exact",
            )
        if type(risk_policy) is not Phase1RiskPolicy:
            raise ExecutionAuthorityError(
                OutcomeCode.INVALID_TYPE,
                "risk_policy must be exact",
            )
        spec_set_sha256 = instrument_spec_set_digest(spec_set)
        risk_policy_sha256 = phase1_risk_policy_digest(risk_policy)
        if (
            risk_policy.instrument_spec_set_id != spec_set.identifier
            or risk_policy.instrument_spec_set_sha256 != spec_set_sha256
            or risk_policy.execution_policy != execution_policy
        ):
            raise ExecutionAuthorityError(
                OutcomeCode.CONFLICTING_ID,
                "Order-authority construction bindings conflict",
            )
        verifier = _require_verifier_binding(
            risk_result_verifier,
            run_id=run_id,
            spec_set=spec_set,
            spec_set_sha256=spec_set_sha256,
            execution_policy=execution_policy,
            risk_policy=risk_policy,
            risk_policy_sha256=risk_policy_sha256,
        )
        authority = object.__new__(Phase1OrderAuthority)
        authority._run_id = run_id
        authority._spec_set = spec_set
        authority._spec_set_sha256 = spec_set_sha256
        authority._execution_policy = execution_policy
        authority._risk_policy = risk_policy
        authority._risk_policy_sha256 = risk_policy_sha256
        authority._risk_result_verifier = verifier
        authority._state = _OrderAuthorityState(
            order_next=1,
            approval_index=_freeze_approval_index({}),
            intent_index=_freeze_intent_index({}),
            decision_index=_freeze_decision_index({}),
            order_id_index=_freeze_order_id_index({}),
            client_submission_key_index=_freeze_client_submission_key_index({}),
            orders=(),
        )
        return authority
    except ExecutionAuthorityError:
        raise
    except (
        EconomicValidationError,
        ExecutionIdentityError,
        ExecutionMessageError,
        RiskContractError,
        RunContractError,
        TimeValidationError,
    ) as error:
        _raise_public(error)
    except (AttributeError, TypeError) as error:
        raise ExecutionAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "Order-authority construction carrier is structurally incomplete",
        ) from error


def _require_verifier_binding(
    verifier: object,
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    spec_set_sha256: Sha256Digest,
    execution_policy: ExecutionPolicyRef,
    risk_policy: Phase1RiskPolicy,
    risk_policy_sha256: Sha256Digest,
) -> RiskResultIssuanceVerifier:
    candidate = cast(Any, verifier)
    try:
        verifier_run_id = candidate.run_id
        verifier_spec_set = candidate.spec_set
        verifier_execution_policy = candidate.execution_policy
        verifier_policy = candidate.policy
        membership = candidate.has_issued_result
    except (AttributeError, TypeError) as error:
        raise ExecutionAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "risk-result verifier has an incomplete construction contract",
        ) from error
    if (
        type(verifier_run_id) is not RunId
        or type(verifier_spec_set) is not InstrumentExecutionSpecSet
        or type(verifier_execution_policy) is not ExecutionPolicyRef
        or type(verifier_policy) is not Phase1RiskPolicy
        or not callable(membership)
    ):
        raise ExecutionAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "risk-result verifier bindings must have exact canonical runtime types",
        )
    if (
        verifier_run_id != run_id
        or verifier_spec_set.identifier != spec_set.identifier
        or instrument_spec_set_digest(verifier_spec_set) != spec_set_sha256
        or verifier_execution_policy != execution_policy
        or verifier_policy.policy_id != risk_policy.policy_id
        or phase1_risk_policy_digest(verifier_policy) != risk_policy_sha256
    ):
        raise ExecutionAuthorityError(
            OutcomeCode.CONFLICTING_ID,
            "risk-result verifier construction bindings conflict",
        )
    return cast(RiskResultIssuanceVerifier, verifier)


def _require_static_policy_proof(
    *,
    intent: OrderIntent,
    decision: RiskDecision,
    evidence: RiskEvaluationEvidence,
    approval: ExecutionApproval,
    risk_policy: Phase1RiskPolicy,
) -> None:
    limit = _instrument_limit(risk_policy, intent)
    if limit is None:
        raise ExecutionAuthorityError(
            OutcomeCode.CONFLICTING_ID,
            "an unconfigured instrument cannot carry an executable risk result",
        )
    approved = approval.approved_quantity
    requested = intent.quantity
    maximum = limit.maximum_order_quantity
    if type(approved) is not CanonicalDecimal:
        raise ExecutionAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "approved quantity must have exact canonical runtime type",
        )
    if decision.kind is RiskDecisionKind.ALLOW:
        valid = (
            evidence.reason_code is RiskReasonCode.WITHIN_LIMITS
            and approved == requested
            and requested <= maximum
        )
    elif decision.kind is RiskDecisionKind.RESIZE:
        reduced = CanonicalDecimal("0") < approved < requested
        if evidence.reason_code is RiskReasonCode.RESIZED_ORDER_LIMIT:
            valid = reduced and requested > maximum and approved == maximum
        elif evidence.reason_code is RiskReasonCode.RESIZED_POSITION_LIMIT:
            valid = reduced and approved < maximum
        elif evidence.reason_code is RiskReasonCode.RESIZED_ORDER_AND_POSITION_LIMITS:
            valid = reduced and requested > maximum and approved == maximum
        else:
            valid = False
    else:
        valid = False
    if not valid:
        raise ExecutionAuthorityError(
            OutcomeCode.CONFLICTING_ID,
            "executable risk result conflicts with the frozen policy truth table",
        )


def _instrument_limit(
    risk_policy: Phase1RiskPolicy,
    intent: OrderIntent,
) -> InstrumentRiskLimit | None:
    for limit in risk_policy.instrument_limits:
        if limit.instrument == intent.instrument:
            return limit
    return None


def _require_issued_result(
    verifier: RiskResultIssuanceVerifier,
    *,
    submitted: _SubmittedInput,
) -> None:
    try:
        issued = verifier.has_issued_result(
            intent_id=submitted.intent.intent_id,
            canonical_intent_bytes=submitted.intent_bytes,
            canonical_decision_bytes=submitted.decision_bytes,
            canonical_evidence_bytes=submitted.evidence_bytes,
        )
    except Exception as error:
        raise _IssuanceVerifierFailure(error) from error
    if type(issued) is not bool:
        raise ExecutionAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "risk-result verifier must return an exact bool",
        )
    if not issued:
        raise ExecutionAuthorityError(
            OutcomeCode.CONFLICTING_ID,
            "risk result was not issued by the bound risk authority",
        )


def _materialize_submitted_input(
    intent: OrderIntent,
    result: RiskEvaluationResult,
) -> _SubmittedInput:
    if type(intent) is not OrderIntent or type(result) is not RiskEvaluationResult:
        raise ExecutionAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "Order creation requires exact OrderIntent and RiskEvaluationResult",
        )
    decision = result.decision
    evidence = result.evidence
    if type(decision) is not RiskDecision or type(evidence) is not RiskEvaluationEvidence:
        raise ExecutionAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "risk result requires exact decision and evidence values",
        )
    _require_submitted_intent_profile(intent)
    _require_submitted_decision_profile(decision)
    approval = decision.approval if type(decision.approval) is ExecutionApproval else None
    return _SubmittedInput(
        intent=intent,
        result=result,
        decision=decision,
        evidence=evidence,
        approval=approval,
        intent_bytes=canonical_order_intent_bytes(intent),
        decision_bytes=canonical_risk_decision_bytes(decision),
        evidence_bytes=canonical_risk_evaluation_evidence_bytes(evidence),
        approval_bytes=(None if approval is None else canonical_execution_approval_bytes(approval)),
    )


def _require_submitted_intent_profile(intent: OrderIntent) -> None:
    if (
        type(intent.target_lineage) is not TargetLineageRef
        or type(intent.execution_policy) is not ExecutionPolicyRef
        or type(intent.order_kind) is not OrderKind
        or type(intent.time_in_force) is not TimeInForce
        or type(intent.causal_root_available_at) is not datetime
    ):
        raise ExecutionAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "intent profile fields must have exact canonical runtime types",
        )
    if (
        intent.order_kind is not OrderKind.MARKET
        or intent.time_in_force is not TimeInForce.GOOD_FOR_NEXT_ELIGIBLE_MARKET_EVENT
        or intent.price_constraint is not None
    ):
        raise ExecutionAuthorityError(
            OutcomeCode.OUT_OF_RANGE,
            "intent is outside the closed Phase 1 execution profile",
        )
    try:
        require_utc(
            intent.causal_root_available_at,
            field="causal_root_available_at",
        )
    except TimeValidationError as error:
        raise ExecutionAuthorityError(OutcomeCode.OUT_OF_RANGE, str(error)) from error


def _require_submitted_decision_profile(decision: RiskDecision) -> None:
    if type(decision.causal_root_available_at) is not datetime:
        raise ExecutionAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "decision causal time must have exact runtime type datetime",
        )
    try:
        require_utc(
            decision.causal_root_available_at,
            field="causal_root_available_at",
        )
    except TimeValidationError as error:
        raise ExecutionAuthorityError(OutcomeCode.OUT_OF_RANGE, str(error)) from error
    if decision.approval is not None:
        if type(decision.approval) is not ExecutionApproval:
            raise ExecutionAuthorityError(
                OutcomeCode.INVALID_TYPE,
                "decision approval must be an exact ExecutionApproval or None",
            )
        if type(decision.approval.causal_root_available_at) is not datetime:
            raise ExecutionAuthorityError(
                OutcomeCode.INVALID_TYPE,
                "approval causal time must have exact runtime type datetime",
            )
        try:
            require_utc(
                decision.approval.causal_root_available_at,
                field="causal_root_available_at",
            )
        except TimeValidationError as error:
            raise ExecutionAuthorityError(OutcomeCode.OUT_OF_RANGE, str(error)) from error


def _require_evidence(
    evidence: RiskEvaluationEvidence,
    *,
    intent: OrderIntent,
    decision: RiskDecision,
    approval: ExecutionApproval,
    risk_policy: Phase1RiskPolicy,
    risk_policy_sha256: Sha256Digest,
) -> None:
    _require_evidence_types(evidence)
    _require_allocated_transition(
        before=evidence.decision_next_before,
        after=evidence.decision_next_after,
        allocated_id=decision.decision_id,
        field_name="decision_next",
    )
    _require_allocated_transition(
        before=evidence.approval_next_before,
        after=evidence.approval_next_after,
        allocated_id=approval.approval_id,
        field_name="approval_next",
    )
    expected_reason_codes = _EXECUTABLE_REASONS.get(decision.kind)
    if expected_reason_codes is None or evidence.reason_code not in expected_reason_codes:
        raise ExecutionAuthorityError(
            OutcomeCode.CONFLICTING_ID,
            "risk reason code conflicts with the executable decision",
        )
    if (
        evidence.run_id != intent.run_id
        or evidence.intent_id != intent.intent_id
        or evidence.intent_sha256 != order_intent_digest(intent)
        or evidence.portfolio_snapshot_version != intent.portfolio_snapshot_version
        or evidence.portfolio_snapshot_version != decision.portfolio_snapshot_version
        or evidence.policy_id != risk_policy.policy_id
        or evidence.policy_sha256 != risk_policy_sha256
        or evidence.risk_state_version != decision.risk_state_version
        or evidence.decision_id != decision.decision_id
        or evidence.decision_sha256 != risk_decision_digest(decision)
        or evidence.approval_sha256 != execution_approval_digest(approval)
    ):
        raise ExecutionAuthorityError(
            OutcomeCode.CONFLICTING_ID,
            "risk evaluation evidence conflicts with the canonical approval lineage",
        )


def _require_evidence_types(evidence: RiskEvaluationEvidence) -> None:
    exact_types: tuple[tuple[object, type[object], str], ...] = (
        (evidence.run_id, RunId, "run_id"),
        (evidence.intent_id, EconomicId, "intent_id"),
        (evidence.intent_sha256, Sha256Digest, "intent_sha256"),
        (
            evidence.portfolio_snapshot_sha256,
            Sha256Digest,
            "portfolio_snapshot_sha256",
        ),
        (evidence.policy_id, RiskPolicyId, "policy_id"),
        (evidence.policy_sha256, Sha256Digest, "policy_sha256"),
        (evidence.decision_id, EconomicId, "decision_id"),
        (evidence.decision_sha256, Sha256Digest, "decision_sha256"),
        (evidence.reason_code, RiskReasonCode, "reason_code"),
    )
    for value, expected, field_name in exact_types:
        if type(value) is not expected:
            raise ExecutionAuthorityError(
                OutcomeCode.INVALID_TYPE,
                f"{field_name} must have its exact canonical runtime type",
            )
    approval_sha256 = evidence.approval_sha256
    if type(approval_sha256) is not Sha256Digest:
        raise ExecutionAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "approval_sha256 must have its exact canonical runtime type",
        )
    nested_strings = (
        evidence.run_id.value,
        evidence.intent_sha256.value,
        evidence.portfolio_snapshot_sha256.value,
        evidence.policy_id.value,
        evidence.policy_sha256.value,
        evidence.decision_sha256.value,
        approval_sha256.value,
    )
    if any(type(value) is not str for value in nested_strings):
        raise ExecutionAuthorityError(
            OutcomeCode.INVALID_TYPE,
            "evidence nested text must have exact runtime type str",
        )
    for identity in (evidence.intent_id, evidence.decision_id):
        if (
            type(identity.run_id) is not RunId
            or type(identity.owner_kind) is not EconomicOwnerKind
            or type(identity.owner_sequence) is not int
        ):
            raise ExecutionAuthorityError(
                OutcomeCode.INVALID_TYPE,
                "evidence economic IDs contain a non-canonical runtime type",
            )
    RunId(evidence.run_id.value)
    EconomicId(
        evidence.intent_id.run_id,
        evidence.intent_id.owner_kind,
        evidence.intent_id.owner_sequence,
    )
    Sha256Digest(evidence.intent_sha256.value)
    Sha256Digest(evidence.portfolio_snapshot_sha256.value)
    RiskPolicyId(evidence.policy_id.value)
    Sha256Digest(evidence.policy_sha256.value)
    EconomicId(
        evidence.decision_id.run_id,
        evidence.decision_id.owner_kind,
        evidence.decision_id.owner_sequence,
    )
    Sha256Digest(evidence.decision_sha256.value)
    Sha256Digest(approval_sha256.value)
    for field_name, value in (
        ("portfolio_snapshot_version", evidence.portfolio_snapshot_version),
        ("risk_state_version", evidence.risk_state_version),
    ):
        if type(value) is not int:
            raise ExecutionAuthorityError(
                OutcomeCode.INVALID_TYPE,
                f"{field_name} must have exact runtime type int",
            )
        if value < 0:
            raise ExecutionAuthorityError(
                OutcomeCode.OUT_OF_RANGE,
                f"{field_name} must be non-negative",
            )


def _require_allocated_transition(
    *,
    before: object,
    after: object,
    allocated_id: EconomicId,
    field_name: str,
) -> None:
    if before is None:
        raise ExecutionAuthorityError(
            OutcomeCode.OUT_OF_RANGE,
            f"{field_name}_before cannot be exhausted for an allocated ID",
        )
    if type(before) is not int:
        raise ExecutionAuthorityError(
            OutcomeCode.INVALID_TYPE,
            f"{field_name}_before must have exact runtime type int",
        )
    if after is not None and type(after) is not int:
        raise ExecutionAuthorityError(
            OutcomeCode.INVALID_TYPE,
            f"{field_name}_after must have exact runtime type int or None",
        )
    if type(allocated_id) is not EconomicId:
        raise ExecutionAuthorityError(
            OutcomeCode.INVALID_TYPE,
            f"{field_name} allocated ID must be exact",
        )
    owner_sequence = allocated_id.owner_sequence
    if (
        type(owner_sequence) is not int
        or before < 1
        or before > _MAX_UINT64
        or owner_sequence < 1
        or owner_sequence > _MAX_UINT64
    ):
        raise ExecutionAuthorityError(
            OutcomeCode.OUT_OF_RANGE,
            f"{field_name} allocation must be in 1..2^64-1",
        )
    if before != owner_sequence:
        raise ExecutionAuthorityError(
            OutcomeCode.CONFLICTING_ID,
            f"{field_name} before state conflicts with allocated ID",
        )
    expected_after = None if before == _MAX_UINT64 else before + 1
    if after != expected_after:
        raise ExecutionAuthorityError(
            OutcomeCode.OUT_OF_RANGE,
            f"{field_name} after state is not the exact allocation successor",
        )


def _make_record(
    *,
    submitted: _SubmittedInput,
    order: Order,
) -> _OrderRecord:
    return _OrderRecord(
        intent_bytes=submitted.intent_bytes,
        decision_bytes=submitted.decision_bytes,
        evidence_bytes=submitted.evidence_bytes,
        order=order,
        order_bytes=canonical_order_bytes(order),
        order_sha256=order_digest(order),
        execution_request_bytes=canonical_execution_request_bytes(order),
        execution_request_sha256=execution_request_digest(order),
        client_submission_key=order_client_submission_key(order),
    )


def _preflight_public_record(record: _OrderRecord) -> None:
    if (
        canonical_order_bytes(record.order) != record.order_bytes
        or order_digest(record.order) != record.order_sha256
        or canonical_execution_request_bytes(record.order) != record.execution_request_bytes
        or execution_request_digest(record.order) != record.execution_request_sha256
        or order_client_submission_key(record.order) != record.client_submission_key
        or record.order.client_submission_key != record.client_submission_key
    ):
        raise AssertionError("canonical Order preflight changed before publication")


def _preflight_candidate_state(
    state: _OrderAuthorityState,
    *,
    approval_id: EconomicId,
    intent_id: EconomicId,
    decision_id: EconomicId,
    record: _OrderRecord,
) -> None:
    if (
        state.approval_index.get(approval_id) is not record
        or state.intent_index.get(intent_id) is not record
        or state.decision_index.get(decision_id) is not record
        or state.order_id_index.get(record.order.order_id) is not record
        or state.client_submission_key_index.get(record.client_submission_key) is not record
        or not state.orders
        or state.orders[-1] is not record.order
    ):
        raise AssertionError("candidate Order state does not publish one coherent record")


def _copy_approval_index(
    index: Mapping[EconomicId, _OrderRecord],
) -> dict[EconomicId, _OrderRecord]:
    return dict(index)


def _copy_intent_index(
    index: Mapping[EconomicId, _OrderRecord],
) -> dict[EconomicId, _OrderRecord]:
    return dict(index)


def _copy_decision_index(
    index: Mapping[EconomicId, _OrderRecord],
) -> dict[EconomicId, _OrderRecord]:
    return dict(index)


def _copy_order_id_index(
    index: Mapping[EconomicId, _OrderRecord],
) -> dict[EconomicId, _OrderRecord]:
    return dict(index)


def _copy_client_submission_key_index(
    index: Mapping[Sha256Digest, _OrderRecord],
) -> dict[Sha256Digest, _OrderRecord]:
    return dict(index)


def _insert_approval_record(
    index: dict[EconomicId, _OrderRecord],
    identity: EconomicId,
    record: _OrderRecord,
) -> None:
    index[identity] = record


def _insert_intent_record(
    index: dict[EconomicId, _OrderRecord],
    identity: EconomicId,
    record: _OrderRecord,
) -> None:
    index[identity] = record


def _insert_decision_record(
    index: dict[EconomicId, _OrderRecord],
    identity: EconomicId,
    record: _OrderRecord,
) -> None:
    index[identity] = record


def _insert_order_id_record(
    index: dict[EconomicId, _OrderRecord],
    identity: EconomicId,
    record: _OrderRecord,
) -> None:
    index[identity] = record


def _insert_client_submission_key_record(
    index: dict[Sha256Digest, _OrderRecord],
    identity: Sha256Digest,
    record: _OrderRecord,
) -> None:
    index[identity] = record


def _freeze_approval_index(
    index: Mapping[EconomicId, _OrderRecord],
) -> Mapping[EconomicId, _OrderRecord]:
    return MappingProxyType(dict(index))


def _freeze_intent_index(
    index: Mapping[EconomicId, _OrderRecord],
) -> Mapping[EconomicId, _OrderRecord]:
    return MappingProxyType(dict(index))


def _freeze_decision_index(
    index: Mapping[EconomicId, _OrderRecord],
) -> Mapping[EconomicId, _OrderRecord]:
    return MappingProxyType(dict(index))


def _freeze_order_id_index(
    index: Mapping[EconomicId, _OrderRecord],
) -> Mapping[EconomicId, _OrderRecord]:
    return MappingProxyType(dict(index))


def _freeze_client_submission_key_index(
    index: Mapping[Sha256Digest, _OrderRecord],
) -> Mapping[Sha256Digest, _OrderRecord]:
    return MappingProxyType(dict(index))


def _advance(value: int) -> int | None:
    if type(value) is not int or value < 1 or value > _MAX_UINT64:
        raise AssertionError("Order allocation state is invalid")
    return None if value == _MAX_UINT64 else value + 1


def _raise_public(error: object) -> NoReturn:
    code = getattr(error, "code", None)
    if type(code) is OutcomeCode and code in _ERROR_CODES:
        raise ExecutionAuthorityError(code, str(error)) from (
            error if isinstance(error, BaseException) else None
        )
    if isinstance(error, TimeValidationError):
        raise ExecutionAuthorityError(OutcomeCode.OUT_OF_RANGE, str(error)) from error
    if isinstance(error, RunContractError):
        raise ExecutionAuthorityError(OutcomeCode.OUT_OF_RANGE, str(error)) from error
    raise AssertionError("unexpected validation outcome at Order-authority boundary") from (
        error if isinstance(error, BaseException) else None
    )
