"""Single deterministic pre-trade risk authority from Accepted ADR 0011."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import NoReturn, final

from ea.core.economics import (
    CanonicalDecimal,
    EconomicValidationError,
    require_positive,
    require_quantized,
)
from ea.core.execution import (
    InstrumentExecutionSpec,
    InstrumentExecutionSpecSet,
    SettlementCurrency,
    instrument_spec_set_digest,
)
from ea.core.execution_identity import EconomicId, EconomicOwnerKind
from ea.core.execution_messages import (
    ExecutionMessageError,
    ExecutionPolicyRef,
    OrderIntent,
    OrderSide,
    RiskDecision,
    RiskDecisionKind,
    allow_order_intent,
    canonical_execution_approval_bytes,
    canonical_order_intent_bytes,
    canonical_risk_decision_bytes,
    execution_approval_digest,
    fail_order_intent_evaluation,
    order_intent_digest,
    reject_order_intent,
    resize_order_intent,
    risk_decision_digest,
)
from ea.core.identity import Instrument
from ea.core.outcomes import OutcomeCode
from ea.core.portfolio import (
    PortfolioLedgerError,
    PortfolioSnapshot,
    canonical_portfolio_snapshot_bytes,
    portfolio_snapshot_digest,
)
from ea.core.risk import (
    InstrumentRiskLimit,
    Phase1RiskPolicy,
    RiskContractError,
    RiskEvaluationEvidence,
    RiskEvaluationResult,
    RiskHaltReason,
    RiskReasonCode,
    RiskStateSnapshot,
    _create_risk_evaluation_evidence,
    _create_risk_evaluation_result,
    _create_risk_state_snapshot,
    canonical_phase1_risk_policy_bytes,
    canonical_risk_evaluation_evidence_bytes,
    canonical_risk_state_snapshot_bytes,
    phase1_risk_policy_digest,
    risk_evaluation_evidence_digest,
    risk_state_snapshot_digest,
)
from ea.core.run import RunId, Sha256Digest
from ea.core.time import TimeValidationError, require_utc

_MAX_UINT64 = (1 << 64) - 1
_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.NOT_QUANTIZED,
        OutcomeCode.CONFLICTING_ID,
    }
)


class RiskAuthorityError(ValueError):
    """Structured fail-closed public risk-authority error."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _ERROR_CODES:
            raise TypeError("risk authority errors require an exact permitted OutcomeCode")
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _ReplayRecord:
    intent_sha256: Sha256Digest
    intent_bytes: bytes
    portfolio_snapshot_sha256: Sha256Digest
    decision: RiskDecision
    evidence: RiskEvaluationEvidence
    result: RiskEvaluationResult


@dataclass(frozen=True, slots=True)
class _AuthorityState:
    risk_state: RiskStateSnapshot
    decision_next: int | None
    approval_next: int | None
    replay_index: Mapping[EconomicId, _ReplayRecord]
    decisions: tuple[RiskDecision, ...]
    evidence: tuple[RiskEvaluationEvidence, ...]
    results: tuple[RiskEvaluationResult, ...]


@final
class Phase1RiskAuthority:
    """The sole mutable owner of Phase 1 risk evaluation and replay state."""

    _run_id: RunId
    _spec_set: InstrumentExecutionSpecSet
    _spec_set_sha256: Sha256Digest
    _execution_policy: ExecutionPolicyRef
    _policy: Phase1RiskPolicy
    _limit_by_instrument: Mapping[Instrument, InstrumentRiskLimit]
    _spec_by_instrument: Mapping[Instrument, InstrumentExecutionSpec]
    _currency_quanta: Mapping[SettlementCurrency, CanonicalDecimal]
    _state: _AuthorityState

    __slots__ = (
        "_currency_quanta",
        "_execution_policy",
        "_limit_by_instrument",
        "_policy",
        "_run_id",
        "_spec_by_instrument",
        "_spec_set",
        "_spec_set_sha256",
        "_state",
    )

    def __init__(self) -> None:
        raise TypeError(
            "Phase1RiskAuthority values are created only by create_phase1_risk_authority"
        )

    @property
    def policy(self) -> Phase1RiskPolicy:
        return self._policy

    @property
    def risk_state(self) -> RiskStateSnapshot:
        return self._state.risk_state

    @property
    def decisions(self) -> tuple[RiskDecision, ...]:
        return self._state.decisions

    @property
    def evidence(self) -> tuple[RiskEvaluationEvidence, ...]:
        return self._state.evidence

    @property
    def results(self) -> tuple[RiskEvaluationResult, ...]:
        return self._state.results

    def engage_halt(
        self,
        reason: RiskHaltReason,
        causal_root_available_at: datetime,
        dispatch_sequence: int,
    ) -> RiskStateSnapshot:
        """Engage the first canonical public halt and never rewrite it."""
        if type(reason) is not RiskHaltReason:
            raise RiskAuthorityError(OutcomeCode.INVALID_TYPE, "halt reason must be exact")
        if reason is RiskHaltReason.INTENT_IDENTITY_CONFLICT:
            raise RiskAuthorityError(
                OutcomeCode.OUT_OF_RANGE,
                "intent identity conflict is an internal-only halt reason",
            )
        if type(causal_root_available_at) is not datetime:
            raise RiskAuthorityError(OutcomeCode.INVALID_TYPE, "halt causal time must be exact")
        try:
            canonical_time = require_utc(
                causal_root_available_at,
                field="causal_root_available_at",
            )
        except TimeValidationError as error:
            raise RiskAuthorityError(OutcomeCode.OUT_OF_RANGE, str(error)) from error
        _require_non_negative(dispatch_sequence, field_name="dispatch_sequence")
        if self._state.risk_state.halted:
            return self._state.risk_state

        next_risk_state = _create_risk_state_snapshot(
            run_id=self._run_id,
            policy_id=self._policy.policy_id,
            policy_sha256=phase1_risk_policy_digest(self._policy),
            risk_state_version=1,
            halted=True,
            halt_reason=reason,
            halt_causal_root_available_at=canonical_time,
            halt_dispatch_sequence=dispatch_sequence,
            conflict_existing_intent_sha256=None,
            conflict_submitted_intent_sha256=None,
        )
        next_state = _freeze_state(
            risk_state=next_risk_state,
            decision_next=self._state.decision_next,
            approval_next=self._state.approval_next,
            replay_index=self._state.replay_index,
            decisions=self._state.decisions,
            evidence=self._state.evidence,
            results=self._state.results,
        )
        canonical_risk_state_snapshot_bytes(next_risk_state)
        risk_state_snapshot_digest(next_risk_state)
        self._state = next_state
        return next_risk_state

    def evaluate(
        self,
        intent: OrderIntent,
        portfolio_snapshot: PortfolioSnapshot,
    ) -> RiskEvaluationResult:
        """Evaluate one new canonical intent or return its exact recorded replay."""
        if type(intent) is not OrderIntent:
            raise RiskAuthorityError(OutcomeCode.INVALID_TYPE, "intent must be exact")
        if type(portfolio_snapshot) is not PortfolioSnapshot:
            raise RiskAuthorityError(OutcomeCode.INVALID_TYPE, "portfolio snapshot must be exact")
        if type(intent.intent_id) is not EconomicId:
            raise RiskAuthorityError(OutcomeCode.INVALID_TYPE, "intent ID must be exact")
        _require_non_negative(
            intent.dispatch_sequence,
            field_name="intent dispatch_sequence",
        )
        try:
            intent_bytes = canonical_order_intent_bytes(intent)
            intent_sha256 = order_intent_digest(intent)
        except (ExecutionMessageError, RiskContractError, EconomicValidationError) as error:
            _raise_structural(error)
        except (AttributeError, TypeError) as error:
            raise RiskAuthorityError(
                OutcomeCode.INVALID_TYPE,
                "intent cannot be canonically encoded",
            ) from error

        existing = self._state.replay_index.get(intent.intent_id)
        if existing is not None:
            if existing.intent_bytes == intent_bytes:
                return existing.result
            self._engage_identity_conflict(
                intent=intent,
                existing_sha256=existing.intent_sha256,
                submitted_sha256=intent_sha256,
            )

        self._require_structural_intent(intent)
        if self._state.decision_next is None:
            raise RiskAuthorityError(
                OutcomeCode.OUT_OF_RANGE,
                "risk decision sequence is exhausted",
            )

        failure_reason = self._classify_lineage_failure(intent, portfolio_snapshot)
        try:
            canonical_portfolio_snapshot_bytes(portfolio_snapshot)
            snapshot_sha256 = portfolio_snapshot_digest(portfolio_snapshot)
        except PortfolioLedgerError as error:
            _raise_structural(error)
        except (AttributeError, TypeError) as error:
            raise RiskAuthorityError(
                OutcomeCode.INVALID_TYPE,
                "portfolio snapshot cannot be canonically encoded",
            ) from error

        if failure_reason is not None:
            return self._register(
                intent=intent,
                intent_bytes=intent_bytes,
                intent_sha256=intent_sha256,
                portfolio_snapshot_version=portfolio_snapshot.snapshot_version,
                portfolio_snapshot_sha256=snapshot_sha256,
                kind=RiskDecisionKind.EVALUATION_FAILED,
                reason=failure_reason,
                approved_quantity=None,
            )
        if self._state.risk_state.halted:
            return self._register(
                intent=intent,
                intent_bytes=intent_bytes,
                intent_sha256=intent_sha256,
                portfolio_snapshot_version=portfolio_snapshot.snapshot_version,
                portfolio_snapshot_sha256=snapshot_sha256,
                kind=RiskDecisionKind.REJECT,
                reason=RiskReasonCode.HALTED,
                approved_quantity=None,
            )
        limit = self._limit_by_instrument.get(intent.instrument)
        if limit is None:
            return self._register(
                intent=intent,
                intent_bytes=intent_bytes,
                intent_sha256=intent_sha256,
                portfolio_snapshot_version=portfolio_snapshot.snapshot_version,
                portfolio_snapshot_sha256=snapshot_sha256,
                kind=RiskDecisionKind.REJECT,
                reason=RiskReasonCode.INSTRUMENT_NOT_CONFIGURED,
                approved_quantity=None,
            )

        current_position = _position_for(portfolio_snapshot, intent.instrument)
        try:
            side_capacity = _side_capacity(
                maximum=limit.maximum_absolute_position,
                current=current_position,
                side=intent.side,
            )
        except EconomicValidationError:
            return self._register(
                intent=intent,
                intent_bytes=intent_bytes,
                intent_sha256=intent_sha256,
                portfolio_snapshot_version=portfolio_snapshot.snapshot_version,
                portfolio_snapshot_sha256=snapshot_sha256,
                kind=RiskDecisionKind.EVALUATION_FAILED,
                reason=RiskReasonCode.ARITHMETIC_FAILURE,
                approved_quantity=None,
            )
        approved_quantity = min(
            intent.quantity,
            limit.maximum_order_quantity,
            side_capacity,
        )
        if approved_quantity.coefficient == 0:
            return self._register(
                intent=intent,
                intent_bytes=intent_bytes,
                intent_sha256=intent_sha256,
                portfolio_snapshot_version=portfolio_snapshot.snapshot_version,
                portfolio_snapshot_sha256=snapshot_sha256,
                kind=RiskDecisionKind.REJECT,
                reason=RiskReasonCode.NO_POSITION_CAPACITY,
                approved_quantity=None,
            )
        if approved_quantity == intent.quantity:
            kind = RiskDecisionKind.ALLOW
            reason = RiskReasonCode.WITHIN_LIMITS
        else:
            kind = RiskDecisionKind.RESIZE
            order_binds = (
                limit.maximum_order_quantity < intent.quantity
                and limit.maximum_order_quantity == approved_quantity
            )
            position_binds = side_capacity < intent.quantity and side_capacity == approved_quantity
            if order_binds and position_binds:
                reason = RiskReasonCode.RESIZED_ORDER_AND_POSITION_LIMITS
            elif order_binds:
                reason = RiskReasonCode.RESIZED_ORDER_LIMIT
            elif position_binds:
                reason = RiskReasonCode.RESIZED_POSITION_LIMIT
            else:
                raise AssertionError("positive partial capacity has no binding limit")
        return self._register(
            intent=intent,
            intent_bytes=intent_bytes,
            intent_sha256=intent_sha256,
            portfolio_snapshot_version=portfolio_snapshot.snapshot_version,
            portfolio_snapshot_sha256=snapshot_sha256,
            kind=kind,
            reason=reason,
            approved_quantity=approved_quantity,
        )

    def _require_structural_intent(
        self,
        intent: OrderIntent,
    ) -> InstrumentExecutionSpec:
        if intent.run_id != self._run_id:
            raise RiskAuthorityError(
                OutcomeCode.CONFLICTING_ID,
                "intent run conflicts with risk authority",
            )
        if (
            intent.instrument_spec_set_id != self._spec_set.identifier
            or intent.instrument_spec_set_sha256 != self._spec_set_sha256
        ):
            raise RiskAuthorityError(
                OutcomeCode.CONFLICTING_ID,
                "intent specification-set lineage conflicts",
            )
        specification = self._spec_by_instrument.get(intent.instrument)
        if (
            specification is None
            or intent.instrument_specification_id != specification.specification_id
        ):
            raise RiskAuthorityError(
                OutcomeCode.CONFLICTING_ID,
                "intent specification lineage conflicts",
            )
        try:
            require_positive(intent.quantity, field_name="intent_quantity")
            require_quantized(
                intent.quantity,
                specification.quantity_quantum,
                field_name="intent_quantity",
            )
        except EconomicValidationError as error:
            _raise_structural(error)
        return specification

    def _classify_lineage_failure(
        self,
        intent: OrderIntent,
        snapshot: PortfolioSnapshot,
    ) -> RiskReasonCode | None:
        if intent.execution_policy != self._execution_policy:
            return RiskReasonCode.LINEAGE_MISMATCH
        if snapshot.run_id != self._run_id or snapshot.run_id != intent.run_id:
            return RiskReasonCode.LINEAGE_MISMATCH
        if (
            snapshot.instrument_spec_set_id != self._spec_set.identifier
            or snapshot.instrument_spec_set_sha256 != self._spec_set_sha256
        ):
            return RiskReasonCode.LINEAGE_MISMATCH
        if snapshot.snapshot_version < intent.portfolio_snapshot_version:
            return RiskReasonCode.STALE_PORTFOLIO_SNAPSHOT
        if snapshot.snapshot_version > intent.portfolio_snapshot_version:
            return RiskReasonCode.LINEAGE_MISMATCH
        if not self._snapshot_balances_match(snapshot):
            return RiskReasonCode.LINEAGE_MISMATCH
        return None

    def _snapshot_balances_match(self, snapshot: PortfolioSnapshot) -> bool:
        for position_balance in snapshot.position_balances:
            specification = self._spec_by_instrument.get(position_balance.instrument)
            if (
                specification is None
                or position_balance.quantity_quantum != specification.quantity_quantum
            ):
                return False
            try:
                require_quantized(
                    position_balance.quantity,
                    specification.quantity_quantum,
                    field_name="position_quantity",
                )
            except EconomicValidationError:
                return False
        for cash_balance in snapshot.cash_balances:
            quantum = self._currency_quanta.get(cash_balance.currency)
            if quantum is None or cash_balance.currency_quantum != quantum:
                return False
            try:
                require_quantized(cash_balance.amount, quantum, field_name="cash_amount")
            except EconomicValidationError:
                return False
        return all(
            balance.currency in self._currency_quanta for balance in snapshot.rounding_balances
        )

    def _register(
        self,
        *,
        intent: OrderIntent,
        intent_bytes: bytes,
        intent_sha256: Sha256Digest,
        portfolio_snapshot_version: int,
        portfolio_snapshot_sha256: Sha256Digest,
        kind: RiskDecisionKind,
        reason: RiskReasonCode,
        approved_quantity: CanonicalDecimal | None,
    ) -> RiskEvaluationResult:
        decision_before = self._state.decision_next
        if decision_before is None:
            raise RiskAuthorityError(
                OutcomeCode.OUT_OF_RANGE,
                "risk decision sequence is exhausted",
            )
        decision_after = _advance(decision_before)
        decision_id = EconomicId(
            self._run_id,
            EconomicOwnerKind.RISK_DECISION,
            decision_before,
        )
        approval_before = self._state.approval_next
        approval_after = approval_before
        approval_id: EconomicId | None = None
        if kind in (RiskDecisionKind.ALLOW, RiskDecisionKind.RESIZE):
            if approval_before is None:
                kind = RiskDecisionKind.EVALUATION_FAILED
                reason = RiskReasonCode.APPROVAL_SEQUENCE_EXHAUSTED
                approved_quantity = None
            else:
                approval_after = _advance(approval_before)
                approval_id = EconomicId(
                    self._run_id,
                    EconomicOwnerKind.RISK_APPROVAL,
                    approval_before,
                )
        try:
            if kind is RiskDecisionKind.ALLOW:
                if approval_id is None:
                    raise AssertionError("allow requires allocated approval")
                decision = allow_order_intent(
                    decision_id=decision_id,
                    approval_id=approval_id,
                    intent=intent,
                    spec_set=self._spec_set,
                    risk_state_version=self._state.risk_state.risk_state_version,
                )
            elif kind is RiskDecisionKind.RESIZE:
                if approval_id is None or approved_quantity is None:
                    raise AssertionError("resize requires allocated approval and quantity")
                decision = resize_order_intent(
                    decision_id=decision_id,
                    approval_id=approval_id,
                    intent=intent,
                    approved_quantity=approved_quantity,
                    spec_set=self._spec_set,
                    risk_state_version=self._state.risk_state.risk_state_version,
                )
            elif kind is RiskDecisionKind.REJECT:
                decision = reject_order_intent(
                    decision_id=decision_id,
                    intent=intent,
                    spec_set=self._spec_set,
                    risk_state_version=self._state.risk_state.risk_state_version,
                )
            elif kind is RiskDecisionKind.EVALUATION_FAILED:
                decision = fail_order_intent_evaluation(
                    decision_id=decision_id,
                    intent=intent,
                    spec_set=self._spec_set,
                    risk_state_version=self._state.risk_state.risk_state_version,
                )
            else:
                raise AssertionError("unknown risk decision kind")
            evidence = _create_risk_evaluation_evidence(
                decision=decision,
                intent_sha256=intent_sha256,
                portfolio_snapshot_version=portfolio_snapshot_version,
                portfolio_snapshot_sha256=portfolio_snapshot_sha256,
                policy=self._policy,
                risk_state_version=self._state.risk_state.risk_state_version,
                reason_code=reason,
                decision_next_before=decision_before,
                decision_next_after=decision_after,
                approval_next_before=approval_before,
                approval_next_after=approval_after,
            )
            result = _create_risk_evaluation_result(decision, evidence)
        except (ExecutionMessageError, RiskContractError, EconomicValidationError) as error:
            _raise_structural(error)

        record = _ReplayRecord(
            intent_sha256=intent_sha256,
            intent_bytes=intent_bytes,
            portfolio_snapshot_sha256=portfolio_snapshot_sha256,
            decision=decision,
            evidence=evidence,
            result=result,
        )
        next_replay = _copy_replay_index(self._state.replay_index)
        next_replay[intent.intent_id] = record
        next_state = _freeze_state(
            risk_state=self._state.risk_state,
            decision_next=decision_after,
            approval_next=approval_after,
            replay_index=next_replay,
            decisions=(*self._state.decisions, decision),
            evidence=(*self._state.evidence, evidence),
            results=(*self._state.results, result),
        )
        _preflight_public_evidence(
            policy=self._policy,
            risk_state=self._state.risk_state,
            decision=decision,
            evidence=evidence,
        )
        self._state = next_state
        return result

    def _engage_identity_conflict(
        self,
        *,
        intent: OrderIntent,
        existing_sha256: Sha256Digest,
        submitted_sha256: Sha256Digest,
    ) -> NoReturn:
        if not self._state.risk_state.halted:
            next_risk_state = _create_risk_state_snapshot(
                run_id=self._run_id,
                policy_id=self._policy.policy_id,
                policy_sha256=phase1_risk_policy_digest(self._policy),
                risk_state_version=1,
                halted=True,
                halt_reason=RiskHaltReason.INTENT_IDENTITY_CONFLICT,
                halt_causal_root_available_at=intent.causal_root_available_at,
                halt_dispatch_sequence=intent.dispatch_sequence,
                conflict_existing_intent_sha256=existing_sha256,
                conflict_submitted_intent_sha256=submitted_sha256,
            )
            next_state = _freeze_state(
                risk_state=next_risk_state,
                decision_next=self._state.decision_next,
                approval_next=self._state.approval_next,
                replay_index=self._state.replay_index,
                decisions=self._state.decisions,
                evidence=self._state.evidence,
                results=self._state.results,
            )
            canonical_risk_state_snapshot_bytes(next_risk_state)
            risk_state_snapshot_digest(next_risk_state)
            self._state = next_state
        raise RiskAuthorityError(
            OutcomeCode.CONFLICTING_ID,
            "intent identity is occupied by different canonical bytes",
        )


def create_phase1_risk_authority(
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    execution_policy: ExecutionPolicyRef,
    policy: Phase1RiskPolicy,
) -> Phase1RiskAuthority:
    """Create one run- and contract-bound version-zero authority."""
    if type(run_id) is not RunId:
        raise RiskAuthorityError(OutcomeCode.INVALID_TYPE, "run_id must be exact")
    if type(spec_set) is not InstrumentExecutionSpecSet:
        raise RiskAuthorityError(OutcomeCode.INVALID_TYPE, "spec_set must be exact")
    if type(execution_policy) is not ExecutionPolicyRef:
        raise RiskAuthorityError(OutcomeCode.INVALID_TYPE, "execution_policy must be exact")
    if type(policy) is not Phase1RiskPolicy:
        raise RiskAuthorityError(OutcomeCode.INVALID_TYPE, "policy must be exact")
    spec_set_sha256 = instrument_spec_set_digest(spec_set)
    if (
        policy.instrument_spec_set_id != spec_set.identifier
        or policy.instrument_spec_set_sha256 != spec_set_sha256
        or policy.execution_policy != execution_policy
    ):
        raise RiskAuthorityError(
            OutcomeCode.CONFLICTING_ID,
            "risk policy construction bindings conflict",
        )
    spec_by_instrument = {
        specification.instrument: specification for specification in spec_set.specifications
    }
    currency_quanta: dict[SettlementCurrency, CanonicalDecimal] = {}
    for specification in spec_set.specifications:
        existing = currency_quanta.get(specification.settlement_currency)
        if existing is not None and existing != specification.currency_quantum:
            raise RiskAuthorityError(
                OutcomeCode.CONFLICTING_ID,
                "one settlement currency has conflicting quanta",
            )
        currency_quanta[specification.settlement_currency] = specification.currency_quantum
    policy_sha256 = phase1_risk_policy_digest(policy)
    initial = _create_risk_state_snapshot(
        run_id=run_id,
        policy_id=policy.policy_id,
        policy_sha256=policy_sha256,
        risk_state_version=0,
        halted=False,
        halt_reason=None,
        halt_causal_root_available_at=None,
        halt_dispatch_sequence=None,
        conflict_existing_intent_sha256=None,
        conflict_submitted_intent_sha256=None,
    )
    authority = object.__new__(Phase1RiskAuthority)
    authority._run_id = run_id
    authority._spec_set = spec_set
    authority._spec_set_sha256 = spec_set_sha256
    authority._execution_policy = execution_policy
    authority._policy = policy
    authority._limit_by_instrument = MappingProxyType(
        {limit.instrument: limit for limit in policy.instrument_limits}
    )
    authority._spec_by_instrument = MappingProxyType(dict(spec_by_instrument))
    authority._currency_quanta = MappingProxyType(dict(currency_quanta))
    authority._state = _freeze_state(
        risk_state=initial,
        decision_next=1,
        approval_next=1,
        replay_index={},
        decisions=(),
        evidence=(),
        results=(),
    )
    canonical_phase1_risk_policy_bytes(policy)
    phase1_risk_policy_digest(policy)
    canonical_risk_state_snapshot_bytes(initial)
    risk_state_snapshot_digest(initial)
    return authority


def _copy_replay_index(
    replay_index: Mapping[EconomicId, _ReplayRecord],
) -> dict[EconomicId, _ReplayRecord]:
    return dict(replay_index)


def _freeze_state(
    *,
    risk_state: RiskStateSnapshot,
    decision_next: int | None,
    approval_next: int | None,
    replay_index: Mapping[EconomicId, _ReplayRecord],
    decisions: tuple[RiskDecision, ...],
    evidence: tuple[RiskEvaluationEvidence, ...],
    results: tuple[RiskEvaluationResult, ...],
) -> _AuthorityState:
    return _AuthorityState(
        risk_state=risk_state,
        decision_next=decision_next,
        approval_next=approval_next,
        replay_index=MappingProxyType(dict(replay_index)),
        decisions=decisions,
        evidence=evidence,
        results=results,
    )


def _preflight_public_evidence(
    *,
    policy: Phase1RiskPolicy,
    risk_state: RiskStateSnapshot,
    decision: RiskDecision,
    evidence: RiskEvaluationEvidence,
) -> None:
    canonical_phase1_risk_policy_bytes(policy)
    phase1_risk_policy_digest(policy)
    canonical_risk_state_snapshot_bytes(risk_state)
    risk_state_snapshot_digest(risk_state)
    canonical_risk_decision_bytes(decision)
    risk_decision_digest(decision)
    if decision.approval is not None:
        canonical_execution_approval_bytes(decision.approval)
        execution_approval_digest(decision.approval)
    canonical_risk_evaluation_evidence_bytes(evidence)
    risk_evaluation_evidence_digest(evidence)


def _position_for(snapshot: PortfolioSnapshot, instrument: Instrument) -> CanonicalDecimal:
    for balance in snapshot.position_balances:
        if balance.instrument == instrument:
            return balance.quantity
    return CanonicalDecimal("0")


def _side_capacity(
    *,
    maximum: CanonicalDecimal,
    current: CanonicalDecimal,
    side: OrderSide,
) -> CanonicalDecimal:
    if side is OrderSide.BUY:
        value = _add_decimal(maximum, current, right_sign=-1)
    elif side is OrderSide.SELL:
        value = _add_decimal(maximum, current, right_sign=1)
    else:
        raise AssertionError("unknown order side")
    return max(CanonicalDecimal("0"), value)


def _add_decimal(
    left: CanonicalDecimal,
    right: CanonicalDecimal,
    *,
    right_sign: int,
) -> CanonicalDecimal:
    if right_sign not in (-1, 1):
        raise AssertionError("right_sign must be -1 or 1")
    scale = max(left.scale, right.scale)
    coefficient = left.coefficient * (10 ** (scale - left.scale))
    coefficient += right_sign * right.coefficient * (10 ** (scale - right.scale))
    return CanonicalDecimal(_scaled_text(coefficient, scale))


def _scaled_text(coefficient: int, scale: int) -> str:
    if coefficient == 0:
        return "0"
    while scale > 0 and coefficient % 10 == 0:
        coefficient //= 10
        scale -= 1
    sign = "-" if coefficient < 0 else ""
    digits = str(abs(coefficient))
    if scale == 0:
        return sign + digits
    if len(digits) <= scale:
        digits = ("0" * (scale + 1 - len(digits))) + digits
    split = len(digits) - scale
    return f"{sign}{digits[:split]}.{digits[split:]}"


def _advance(value: int) -> int | None:
    if type(value) is not int or value < 1 or value > _MAX_UINT64:
        raise AssertionError("risk allocation state is invalid")
    return None if value == _MAX_UINT64 else value + 1


def _require_non_negative(value: object, *, field_name: str) -> int:
    if type(value) is not int:
        raise RiskAuthorityError(
            OutcomeCode.INVALID_TYPE,
            f"{field_name} must be an exact int",
        )
    if value < 0:
        raise RiskAuthorityError(
            OutcomeCode.OUT_OF_RANGE,
            f"{field_name} must be non-negative",
        )
    return value


def _raise_structural(error: object) -> NoReturn:
    code = getattr(error, "code", None)
    if code is OutcomeCode.INVALID_TYPE:
        translated = OutcomeCode.INVALID_TYPE
    elif code is OutcomeCode.NOT_QUANTIZED:
        translated = OutcomeCode.NOT_QUANTIZED
    elif code is OutcomeCode.CONFLICTING_ID:
        translated = OutcomeCode.CONFLICTING_ID
    elif code in {
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.NON_FINITE,
        OutcomeCode.PRICE_DOMAIN,
        OutcomeCode.ARITHMETIC_OVERFLOW,
    }:
        translated = OutcomeCode.OUT_OF_RANGE
    else:
        raise AssertionError("unexpected validation outcome at risk authority boundary")
    raise RiskAuthorityError(translated, str(error)) from (
        error
        if isinstance(
            error,
            BaseException,
        )
        else None
    )
