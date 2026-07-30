"""Deterministic target-current portfolio planning from Accepted ADR 0017."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import NoReturn, final

from ea.core.economics import (
    CanonicalDecimal,
    EconomicValidationError,
    require_quantized,
)
from ea.core.execution import (
    InstrumentExecutionSpec,
    InstrumentExecutionSpecSet,
    instrument_spec_set_digest,
)
from ea.core.execution_identity import EconomicId, EconomicOwnerKind
from ea.core.execution_messages import (
    ExecutionMessageError,
    ExecutionPolicyRef,
    OrderIntent,
    OrderSide,
    TargetLineageRef,
    canonical_order_intent_bytes,
    create_order_intent,
    order_intent_digest,
)
from ea.core.identity import Instrument
from ea.core.outcomes import OutcomeCode
from ea.core.portfolio import (
    PortfolioLedgerError,
    PortfolioSnapshot,
    canonical_portfolio_snapshot_bytes,
    portfolio_snapshot_digest,
)
from ea.core.portfolio_planning import (
    Phase1PortfolioPolicy,
    Phase1PortfolioPolicyEntry,
    PlanningOutcomeKind,
    PortfolioPlanningAuthorityState,
    PortfolioPlanningError,
    PortfolioPlanningResult,
    PortfolioPolicyId,
    _create_planning_outcome,
    _create_portfolio_planning_authority_state,
    _create_portfolio_planning_result,
    _create_portfolio_target,
    _validate_planning_id,
    canonical_phase1_portfolio_policy_bytes,
    canonical_planning_outcome_bytes,
    canonical_portfolio_planning_authority_state_bytes,
    canonical_portfolio_planning_result_bytes,
    canonical_portfolio_target_bytes,
    create_phase1_portfolio_policy,
    phase1_portfolio_policy_digest,
    planning_outcome_digest,
    portfolio_planning_authority_state_digest,
    portfolio_planning_result_digest,
    portfolio_planning_submission_digest,
    portfolio_target_digest,
)
from ea.core.run import RunId, Sha256Digest
from ea.core.strategy import (
    AuthorityConflictKind,
    AuthorityKind,
    SignalDirection,
    StrategyContractError,
    StrategySignal,
    _create_authority_conflict_evidence,
    canonical_strategy_signal_bytes,
    strategy_signal_digest,
)
from ea.portfolio.ledger import PortfolioLedger

_MAX_UINT64 = (1 << 64) - 1


@dataclass(frozen=True, slots=True)
class _ReplayRecord:
    signal_bytes: bytes
    signal_sha256: Sha256Digest
    submitted_sha256: Sha256Digest
    snapshot_bytes: bytes
    snapshot_sha256: Sha256Digest
    result: PortfolioPlanningResult
    result_bytes: bytes
    result_sha256: Sha256Digest


@dataclass(frozen=True, slots=True)
class _AuthorityState:
    public: PortfolioPlanningAuthorityState
    replay_by_signal_id: MappingProxyType[EconomicId, _ReplayRecord]


def _fail(code: OutcomeCode, message: str) -> PortfolioPlanningError:
    return PortfolioPlanningError(code, message)


def _advance(value: int) -> int | None:
    return None if value == _MAX_UINT64 else value + 1


def _raise_structural(error: BaseException) -> NoReturn:
    code = getattr(error, "code", None)
    if code is OutcomeCode.INVALID_TYPE:
        translated = OutcomeCode.INVALID_TYPE
    elif code is OutcomeCode.NOT_QUANTIZED:
        translated = OutcomeCode.NOT_QUANTIZED
    elif code is OutcomeCode.CONFLICTING_ID:
        translated = OutcomeCode.CONFLICTING_ID
    else:
        translated = OutcomeCode.OUT_OF_RANGE
    raise _fail(translated, str(error)) from error


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


def _subtract(left: CanonicalDecimal, right: CanonicalDecimal) -> CanonicalDecimal:
    if type(left) is not CanonicalDecimal or type(right) is not CanonicalDecimal:
        raise _fail(OutcomeCode.INVALID_TYPE, "delta operands must be exact CanonicalDecimal")
    scale = max(left.scale, right.scale)
    coefficient = left.coefficient * (10 ** (scale - left.scale)) - right.coefficient * (
        10 ** (scale - right.scale)
    )
    try:
        return CanonicalDecimal(_scaled_text(coefficient, scale))
    except EconomicValidationError as error:
        _raise_structural(error)


def _absolute(value: CanonicalDecimal) -> CanonicalDecimal:
    if value.coefficient >= 0:
        return value
    try:
        return CanonicalDecimal(value.text.removeprefix("-"))
    except EconomicValidationError as error:
        _raise_structural(error)


def _signed_target(quantity: CanonicalDecimal, direction: SignalDirection) -> CanonicalDecimal:
    if direction is SignalDirection.FLAT:
        return CanonicalDecimal("0")
    if direction is SignalDirection.LONG:
        return quantity
    if direction is SignalDirection.SHORT:
        return CanonicalDecimal(f"-{quantity.text}")
    raise _fail(OutcomeCode.INVALID_TYPE, "signal direction is invalid")


@final
class PortfolioPlanningAuthority:
    """The sole mutable owner of Phase 1 targets and portfolio intents."""

    __slots__ = (
        "_entry_by_instrument",
        "_execution_policy",
        "_ledger",
        "_policy",
        "_policy_bytes",
        "_policy_sha256",
        "_run_id",
        "_spec_by_instrument",
        "_spec_set",
        "_spec_set_sha256",
        "_state",
    )

    _run_id: RunId
    _ledger: PortfolioLedger
    _spec_set: InstrumentExecutionSpecSet
    _spec_set_sha256: Sha256Digest
    _policy: Phase1PortfolioPolicy
    _policy_bytes: bytes
    _policy_sha256: Sha256Digest
    _execution_policy: ExecutionPolicyRef
    _entry_by_instrument: MappingProxyType[Instrument, Phase1PortfolioPolicyEntry]
    _spec_by_instrument: MappingProxyType[Instrument, InstrumentExecutionSpec]
    _state: _AuthorityState

    def __init__(self) -> None:
        raise TypeError(
            "PortfolioPlanningAuthority values are created only by "
            "create_portfolio_planning_authority"
        )

    @property
    def state(self) -> PortfolioPlanningAuthorityState:
        return self._state.public

    def lookup_by_signal_id(self, signal_id: EconomicId) -> PortfolioPlanningResult | None:
        _validate_planning_id(
            signal_id,
            field_name="lookup signal ID",
            expected_owner=EconomicOwnerKind.STRATEGY_SIGNAL,
            expected_run=self._run_id,
        )
        record = self._state.replay_by_signal_id.get(signal_id)
        return None if record is None else record.result

    def plan(self, signal: StrategySignal) -> PortfolioPlanningResult:
        """Plan one signal against exactly one retained latest ledger snapshot."""
        try:
            signal_bytes = canonical_strategy_signal_bytes(signal)
            signal_sha256 = strategy_signal_digest(signal)
            submitted_sha256 = portfolio_planning_submission_digest(signal)
        except StrategyContractError as error:
            _raise_structural(error)

        state = self._state
        existing = state.replay_by_signal_id.get(signal.signal_id)
        if existing is not None:
            if existing.signal_bytes == signal_bytes:
                return existing.result
            self._publish_conflict(
                kind=AuthorityConflictKind.IDENTITY_REUSE,
                signal=signal,
                occupied_owner_id=signal.signal_id,
                existing_sha256=existing.signal_sha256,
                submitted_sha256=submitted_sha256,
            )

        last_sequence = state.public.last_new_signal_dispatch_sequence
        if last_sequence is not None and signal.dispatch_sequence <= last_sequence:
            self._publish_conflict(
                kind=AuthorityConflictKind.NON_MONOTONE_DISPATCH,
                signal=signal,
                occupied_owner_id=None,
                existing_sha256=None,
                submitted_sha256=submitted_sha256,
            )
        if state.public.halted:
            raise _fail(OutcomeCode.CONFLICTING_ID, "portfolio planning authority is halted")
        if signal.run_id != self._run_id:
            raise _fail(OutcomeCode.CONFLICTING_ID, "signal run conflicts")
        entry = self._entry_by_instrument.get(signal.instrument)
        specification = self._spec_by_instrument.get(signal.instrument)
        if entry is None or specification is None:
            raise _fail(OutcomeCode.CONFLICTING_ID, "signal instrument is not in policy/spec set")
        try:
            current_policy_bytes = canonical_phase1_portfolio_policy_bytes(self._policy)
            current_policy_sha256 = phase1_portfolio_policy_digest(self._policy)
        except PortfolioPlanningError:
            raise
        except Exception as error:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "bound portfolio policy validation failed",
            ) from error
        if (
            current_policy_bytes != self._policy_bytes
            or current_policy_sha256 != self._policy_sha256
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "bound portfolio policy changed")
        target_next = state.public.target_next
        if target_next is None:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "portfolio target sequence is exhausted")

        snapshot, snapshot_bytes, snapshot_sha256 = self._read_snapshot()
        try:
            target_position = _signed_target(entry.target_quantity, signal.direction)
            current_position = CanonicalDecimal("0")
            for balance in snapshot.position_balances:
                if balance.instrument == signal.instrument:
                    current_position = balance.quantity
                    break
        except EconomicValidationError as error:
            _raise_structural(error)
        try:
            require_quantized(
                target_position,
                specification.quantity_quantum,
                field_name="target_position",
            )
            require_quantized(
                current_position,
                specification.quantity_quantum,
                field_name="current_position",
            )
            delta = _subtract(target_position, current_position)
            require_quantized(
                delta,
                specification.quantity_quantum,
                field_name="delta",
            )
        except EconomicValidationError as error:
            _raise_structural(error)

        try:
            target = _create_portfolio_target(
                run_id=self._run_id,
                target_id=EconomicId(
                    self._run_id,
                    EconomicOwnerKind.PORTFOLIO_TARGET,
                    target_next,
                ),
                signal=signal,
                signal_sha256=signal_sha256,
                instrument=signal.instrument,
                target_position=target_position,
                policy=self._policy,
                policy_sha256=self._policy_sha256,
                portfolio_snapshot_version=snapshot.snapshot_version,
                portfolio_snapshot_sha256=snapshot_sha256,
                instrument_specification_id=specification.specification_id,
            )
            canonical_portfolio_target_bytes(target)
            target_sha256 = portfolio_target_digest(target)
        except PortfolioPlanningError:
            raise
        except Exception as error:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "portfolio target publication preflight failed",
            ) from error
        target_after = _advance(target_next)
        intent_before = state.public.intent_next
        intent_after = intent_before
        intent: OrderIntent | None = None
        intent_sha256: Sha256Digest | None = None

        if snapshot.unresolved_fills:
            kind = PlanningOutcomeKind.BLOCKED_UNRESOLVED_FILLS
        elif delta.coefficient == 0:
            kind = PlanningOutcomeKind.ALREADY_AT_TARGET
        else:
            kind = PlanningOutcomeKind.INTENT_EMITTED
            if intent_before is None:
                raise _fail(OutcomeCode.OUT_OF_RANGE, "portfolio intent sequence is exhausted")
            side = OrderSide.BUY if delta.coefficient > 0 else OrderSide.SELL
            quantity = _absolute(delta)
            try:
                intent = create_order_intent(
                    run_id=self._run_id,
                    intent_id=EconomicId(
                        self._run_id,
                        EconomicOwnerKind.PORTFOLIO_INTENT,
                        intent_before,
                    ),
                    correlation_id=signal.signal_id,
                    target_lineage=TargetLineageRef(target.target_id, target_sha256),
                    instrument=signal.instrument,
                    side=side,
                    quantity=quantity,
                    portfolio_snapshot_version=snapshot.snapshot_version,
                    causal_root_available_at=signal.causal_root_available_at,
                    dispatch_sequence=signal.dispatch_sequence,
                    spec_set=self._spec_set,
                    execution_policy=self._execution_policy,
                )
                canonical_order_intent_bytes(intent)
                intent_sha256 = order_intent_digest(intent)
            except (ExecutionMessageError, EconomicValidationError) as error:
                _raise_structural(error)
            except Exception as error:
                raise _fail(
                    OutcomeCode.INVALID_TYPE,
                    "portfolio intent publication preflight failed",
                ) from error
            intent_after = _advance(intent_before)

        try:
            outcome = _create_planning_outcome(
                run_id=self._run_id,
                target=target,
                target_sha256=target_sha256,
                signal=signal,
                policy=self._policy,
                policy_sha256=self._policy_sha256,
                snapshot_sha256=snapshot_sha256,
                target_next_before=target_next,
                target_next_after=target_after,
                intent_next_before=intent_before,
                intent_next_after=intent_after,
                kind=kind,
                intent=intent,
                intent_sha256=intent_sha256,
                delta=delta,
            )
            canonical_planning_outcome_bytes(outcome)
            planning_outcome_digest(outcome)
            result = _create_portfolio_planning_result(
                signal=signal,
                target=target,
                portfolio_snapshot=snapshot,
                outcome=outcome,
                intent=intent,
            )
            result_bytes = canonical_portfolio_planning_result_bytes(result)
            result_sha256 = portfolio_planning_result_digest(result)
        except PortfolioPlanningError:
            raise
        except Exception as error:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "portfolio planning publication preflight failed",
            ) from error

        replay = dict(state.replay_by_signal_id)
        replay[signal.signal_id] = _ReplayRecord(
            signal_bytes=signal_bytes,
            signal_sha256=signal_sha256,
            submitted_sha256=submitted_sha256,
            snapshot_bytes=snapshot_bytes,
            snapshot_sha256=snapshot_sha256,
            result=result,
            result_bytes=result_bytes,
            result_sha256=result_sha256,
        )
        try:
            public = _create_portfolio_planning_authority_state(
                run_id=self._run_id,
                halted=False,
                target_next=target_after,
                intent_next=intent_after,
                last_new_signal_dispatch_sequence=signal.dispatch_sequence,
                result_count=state.public.result_count + 1,
                conflict=None,
            )
            canonical_portfolio_planning_authority_state_bytes(public)
            portfolio_planning_authority_state_digest(public)
        except PortfolioPlanningError:
            raise
        except Exception as error:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "portfolio planning state publication preflight failed",
            ) from error
        self._state = _AuthorityState(
            public=public,
            replay_by_signal_id=MappingProxyType(replay),
        )
        return result

    def _read_snapshot(self) -> tuple[PortfolioSnapshot, bytes, Sha256Digest]:
        try:
            snapshot = self._ledger.snapshot
        except Exception as error:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "portfolio ledger snapshot access failed",
            ) from error
        if type(snapshot) is not PortfolioSnapshot:
            raise _fail(OutcomeCode.INVALID_TYPE, "ledger snapshot must be exact")
        if (
            snapshot.run_id != self._run_id
            or snapshot.instrument_spec_set_id != self._spec_set.identifier
            or snapshot.instrument_spec_set_sha256 != self._spec_set_sha256
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "ledger snapshot lineage conflicts")
        try:
            snapshot_bytes = canonical_portfolio_snapshot_bytes(snapshot)
            snapshot_sha256 = portfolio_snapshot_digest(snapshot)
        except PortfolioLedgerError as error:
            _raise_structural(error)
        except Exception as error:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "portfolio snapshot canonicalization failed",
            ) from error
        return snapshot, snapshot_bytes, snapshot_sha256

    def _publish_conflict(
        self,
        *,
        kind: AuthorityConflictKind,
        signal: StrategySignal,
        occupied_owner_id: EconomicId | None,
        existing_sha256: Sha256Digest | None,
        submitted_sha256: Sha256Digest,
    ) -> NoReturn:
        state = self._state
        if state.public.conflict is None:
            try:
                conflict = _create_authority_conflict_evidence(
                    authority_kind=AuthorityKind.PORTFOLIO_PLANNING,
                    conflict_kind=kind,
                    run_id=self._run_id,
                    occupied_owner_id=occupied_owner_id,
                    dispatch_sequence=signal.dispatch_sequence,
                    last_new_dispatch_sequence=state.public.last_new_signal_dispatch_sequence,
                    existing_sha256=existing_sha256,
                    submitted_sha256=submitted_sha256,
                )
                public = _create_portfolio_planning_authority_state(
                    run_id=self._run_id,
                    halted=True,
                    target_next=state.public.target_next,
                    intent_next=state.public.intent_next,
                    last_new_signal_dispatch_sequence=state.public.last_new_signal_dispatch_sequence,
                    result_count=state.public.result_count,
                    conflict=conflict,
                )
                canonical_portfolio_planning_authority_state_bytes(public)
                portfolio_planning_authority_state_digest(public)
            except PortfolioPlanningError:
                raise
            except StrategyContractError as error:
                _raise_structural(error)
            except Exception as error:
                raise _fail(
                    OutcomeCode.INVALID_TYPE,
                    "portfolio conflict publication preflight failed",
                ) from error
            self._state = _AuthorityState(
                public=public,
                replay_by_signal_id=state.replay_by_signal_id,
            )
        raise _fail(OutcomeCode.CONFLICTING_ID, "portfolio planning replay conflicts")


def create_portfolio_planning_authority(
    *,
    run_id: RunId,
    ledger: PortfolioLedger,
    spec_set: InstrumentExecutionSpecSet,
    policy: Phase1PortfolioPolicy,
    execution_policy: ExecutionPolicyRef,
) -> PortfolioPlanningAuthority:
    """Create one planning authority bound to an exact ledger and policy."""
    if type(run_id) is not RunId:
        raise _fail(OutcomeCode.INVALID_TYPE, "run_id must be exact")
    if type(ledger) is not PortfolioLedger:
        raise _fail(OutcomeCode.INVALID_TYPE, "ledger must be an exact PortfolioLedger")
    if type(spec_set) is not InstrumentExecutionSpecSet:
        raise _fail(OutcomeCode.INVALID_TYPE, "spec_set must be exact")
    if type(policy) is not Phase1PortfolioPolicy:
        raise _fail(OutcomeCode.INVALID_TYPE, "policy must be exact")
    if type(execution_policy) is not ExecutionPolicyRef:
        raise _fail(OutcomeCode.INVALID_TYPE, "execution_policy must be exact")
    spec_sha256 = instrument_spec_set_digest(spec_set)
    if (
        policy.instrument_spec_set_id != spec_set.identifier
        or policy.instrument_spec_set_sha256 != spec_sha256
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "policy and specification set conflict")
    original_policy_bytes = canonical_phase1_portfolio_policy_bytes(policy)
    original_policy_sha256 = phase1_portfolio_policy_digest(policy)
    try:
        copied_entries = tuple(
            Phase1PortfolioPolicyEntry(
                instrument=spec_set.require(entry.instrument).instrument,
                target_quantity=CanonicalDecimal(entry.target_quantity.text),
            )
            for entry in policy.entries
        )
        retained_policy = create_phase1_portfolio_policy(
            policy_id=PortfolioPolicyId(policy.policy_id.value),
            entries=copied_entries,
            spec_set=spec_set,
        )
        policy_bytes = canonical_phase1_portfolio_policy_bytes(retained_policy)
        policy_sha256 = phase1_portfolio_policy_digest(retained_policy)
    except (PortfolioPlanningError, EconomicValidationError) as error:
        _raise_structural(error)
    except Exception as error:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "retained portfolio policy construction failed",
        ) from error
    if policy_bytes != original_policy_bytes or policy_sha256 != original_policy_sha256:
        raise _fail(OutcomeCode.CONFLICTING_ID, "retained portfolio policy copy conflicts")
    try:
        snapshot = ledger.snapshot
        canonical_portfolio_snapshot_bytes(snapshot)
        snapshot_sha256 = portfolio_snapshot_digest(snapshot)
    except (PortfolioLedgerError, EconomicValidationError) as error:
        _raise_structural(error)
    except Exception as error:
        raise _fail(OutcomeCode.INVALID_TYPE, "ledger binding validation failed") from error
    if (
        type(snapshot) is not PortfolioSnapshot
        or snapshot.run_id != run_id
        or snapshot.instrument_spec_set_id != spec_set.identifier
        or snapshot.instrument_spec_set_sha256 != spec_sha256
        or type(snapshot_sha256) is not Sha256Digest
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "ledger binding conflicts")
    entry_by_instrument = {entry.instrument: entry for entry in retained_policy.entries}
    spec_by_instrument: dict[Instrument, InstrumentExecutionSpec] = {
        specification.instrument: specification for specification in spec_set.specifications
    }
    public = _create_portfolio_planning_authority_state(
        run_id=run_id,
        halted=False,
        target_next=1,
        intent_next=1,
        last_new_signal_dispatch_sequence=None,
        result_count=0,
        conflict=None,
    )
    value = object.__new__(PortfolioPlanningAuthority)
    value._run_id = run_id
    value._ledger = ledger
    value._spec_set = spec_set
    value._spec_set_sha256 = spec_sha256
    value._policy = retained_policy
    value._policy_bytes = policy_bytes
    value._policy_sha256 = policy_sha256
    value._execution_policy = execution_policy
    value._entry_by_instrument = MappingProxyType(entry_by_instrument)
    value._spec_by_instrument = MappingProxyType(spec_by_instrument)
    value._state = _AuthorityState(
        public=public,
        replay_by_signal_id=MappingProxyType({}),
    )
    return value
