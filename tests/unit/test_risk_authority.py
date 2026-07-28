from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from ea.core import (
    CanonicalDecimal,
    EconomicId,
    EconomicOwnerKind,
    ExecutionPolicyId,
    ExecutionPolicyRef,
    Instrument,
    InstrumentExecutionSpec,
    InstrumentRiskLimit,
    InstrumentSpecId,
    InstrumentSpecSetId,
    OrderIntent,
    OrderSide,
    OutcomeCode,
    Phase1RiskPolicy,
    PortfolioSnapshot,
    PositionBalance,
    PriceDomain,
    RiskContractError,
    RiskDecisionKind,
    RiskEvaluationEvidence,
    RiskEvaluationResult,
    RiskHaltReason,
    RiskPolicyId,
    RiskReasonCode,
    RiskStateSnapshot,
    RunId,
    SettlementCurrency,
    Sha256Digest,
    TargetLineageRef,
    VenueId,
    build_instrument_spec_set,
    canonical_phase1_risk_policy_bytes,
    canonical_risk_evaluation_evidence_bytes,
    canonical_risk_state_snapshot_bytes,
    create_order_intent,
    create_phase1_risk_policy,
    phase1_risk_policy_digest,
    portfolio_snapshot_digest,
    risk_evaluation_evidence_digest,
    risk_state_snapshot_digest,
)
from ea.core import execution_messages as execution_messages_module
from ea.core.execution import InstrumentExecutionSpecSet
from ea.risk import Phase1RiskAuthority, RiskAuthorityError, create_phase1_risk_authority
from ea.risk import authority as authority_module

RUN_ID = RunId("12345678-1234-4234-8234-123456789abc")
OTHER_RUN_ID = RunId("87654321-4321-4321-8321-cba987654321")
INSTRUMENT = Instrument(VenueId("XNAS"), "AAPL")
SECOND_INSTRUMENT = Instrument(VenueId("XNYS"), "IBM")
USD = SettlementCurrency("USD")
TIME = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
EXECUTION_POLICY = ExecutionPolicyRef(
    ExecutionPolicyId("phase1.execution.v1"),
    Sha256Digest("1" * 64),
)


def _spec(
    *,
    instrument: Instrument = INSTRUMENT,
    specification_id: str = "xnas.aapl.v1",
    quantity_quantum: str = "1",
    currency_quantum: str = "0.01",
) -> InstrumentExecutionSpec:
    return InstrumentExecutionSpec(
        instrument=instrument,
        specification_id=InstrumentSpecId(specification_id),
        price_quantum=CanonicalDecimal("0.01"),
        quantity_quantum=CanonicalDecimal(quantity_quantum),
        settlement_currency=USD,
        currency_quantum=CanonicalDecimal(currency_quantum),
        contract_multiplier=CanonicalDecimal("1"),
        price_domain=PriceDomain.POSITIVE,
    )


def _spec_set(*specifications: InstrumentExecutionSpec) -> InstrumentExecutionSpecSet:
    return build_instrument_spec_set(
        InstrumentSpecSetId("phase1.test.v1"),
        specifications or (_spec(),),
    )


def _limit(
    *,
    instrument: Instrument = INSTRUMENT,
    order: str = "5",
    position: str = "10",
) -> InstrumentRiskLimit:
    return InstrumentRiskLimit(
        instrument=instrument,
        maximum_order_quantity=CanonicalDecimal(order),
        maximum_absolute_position=CanonicalDecimal(position),
    )


def _policy(
    spec_set: InstrumentExecutionSpecSet,
    *limits: InstrumentRiskLimit,
    execution_policy: ExecutionPolicyRef = EXECUTION_POLICY,
) -> Phase1RiskPolicy:
    return create_phase1_risk_policy(
        policy_id=RiskPolicyId("phase1.risk.v1"),
        spec_set=spec_set,
        execution_policy=execution_policy,
        instrument_limits=limits,
    )


def _authority(
    spec_set: InstrumentExecutionSpecSet,
    policy: Phase1RiskPolicy,
    *,
    execution_policy: ExecutionPolicyRef = EXECUTION_POLICY,
) -> Phase1RiskAuthority:
    return create_phase1_risk_authority(
        run_id=RUN_ID,
        spec_set=spec_set,
        execution_policy=execution_policy,
        policy=policy,
    )


def _id(
    owner: EconomicOwnerKind,
    sequence: int,
    *,
    run_id: RunId = RUN_ID,
) -> EconomicId:
    return EconomicId(run_id, owner, sequence)


def _intent(
    spec_set: InstrumentExecutionSpecSet,
    *,
    sequence: int = 1,
    dispatch_sequence: int | None = None,
    side: OrderSide = OrderSide.BUY,
    quantity: str = "2",
    snapshot_version: int = 0,
    instrument: Instrument = INSTRUMENT,
    run_id: RunId = RUN_ID,
    execution_policy: ExecutionPolicyRef = EXECUTION_POLICY,
) -> OrderIntent:
    return create_order_intent(
        run_id=run_id,
        intent_id=_id(EconomicOwnerKind.PORTFOLIO_INTENT, sequence, run_id=run_id),
        correlation_id=_id(EconomicOwnerKind.STRATEGY_SIGNAL, 1, run_id=run_id),
        target_lineage=TargetLineageRef(
            _id(EconomicOwnerKind.PORTFOLIO_TARGET, 1, run_id=run_id),
            Sha256Digest("2" * 64),
        ),
        instrument=instrument,
        side=side,
        quantity=CanonicalDecimal(quantity),
        portfolio_snapshot_version=snapshot_version,
        causal_root_available_at=TIME,
        dispatch_sequence=sequence if dispatch_sequence is None else dispatch_sequence,
        spec_set=spec_set,
        execution_policy=execution_policy,
    )


def _snapshot(
    spec_set: InstrumentExecutionSpecSet,
    *,
    position: str | None = None,
    position_quantum: str = "1",
    run_id: RunId = RUN_ID,
    set_id: InstrumentSpecSetId | None = None,
    set_sha256: Sha256Digest | None = None,
) -> PortfolioSnapshot:
    version = 0 if position is None else 1
    return PortfolioSnapshot(
        run_id=run_id,
        instrument_spec_set_id=set_id or spec_set.identifier,
        instrument_spec_set_sha256=set_sha256 or _spec_set_sha256(spec_set),
        snapshot_version=version,
        ledger_sequence=version,
        last_entry_id=(
            None if version == 0 else _id(EconomicOwnerKind.LEDGER_ENTRY, version, run_id=run_id)
        ),
        last_transaction_sha256=None if version == 0 else Sha256Digest("3" * 64),
        cash_balances=(),
        position_balances=(
            ()
            if position is None
            else (
                PositionBalance(
                    instrument=INSTRUMENT,
                    quantity_quantum=CanonicalDecimal(position_quantum),
                    quantity=CanonicalDecimal(position),
                ),
            )
        ),
        rounding_balances=(),
        unresolved_fills=(),
    )


def _spec_set_sha256(spec_set: InstrumentExecutionSpecSet) -> Sha256Digest:
    from ea.core import instrument_spec_set_digest

    return instrument_spec_set_digest(spec_set)


def _replace_intent(intent: OrderIntent, **changes: object) -> OrderIntent:
    replacement = object.__new__(OrderIntent)
    for name in OrderIntent.__slots__:
        object.__setattr__(
            replacement,
            name,
            changes.get(name, getattr(intent, name)),
        )
    return replacement


def _assert_error(error: pytest.ExceptionInfo[RiskAuthorityError], code: OutcomeCode) -> None:
    assert error.value.code is code


def _raise_injected(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("injected pre-publication failure")


def _assert_authority_unchanged(
    authority: Phase1RiskAuthority,
    original_state: Any,
    original_risk_state_bytes: bytes,
    original_risk_state_digest: Sha256Digest,
    *,
    compare_replay_contents: bool = True,
) -> None:
    current = authority._state
    assert current is original_state
    assert current.risk_state is original_state.risk_state
    assert current.decision_next == original_state.decision_next
    assert current.approval_next == original_state.approval_next
    assert current.replay_index is original_state.replay_index
    if compare_replay_contents:
        assert dict(current.replay_index) == dict(original_state.replay_index)
    assert current.decisions is original_state.decisions
    assert current.evidence is original_state.evidence
    assert current.results is original_state.results
    assert canonical_risk_state_snapshot_bytes(current.risk_state) == original_risk_state_bytes
    assert risk_state_snapshot_digest(current.risk_state) == original_risk_state_digest


class _FailsDuringReplayCopy(Mapping[EconomicId, Any]):
    def __getitem__(self, key: EconomicId) -> Any:
        raise KeyError(key)

    def __iter__(self) -> Iterator[EconomicId]:
        raise RuntimeError("injected replay-index copy failure")

    def __len__(self) -> int:
        return 0


class _FailsDuringReplayFreeze(MutableMapping[EconomicId, Any]):
    def __init__(self) -> None:
        self._values: dict[EconomicId, Any] = {}

    def __getitem__(self, key: EconomicId) -> Any:
        return self._values[key]

    def __setitem__(self, key: EconomicId, value: Any) -> None:
        self._values[key] = value

    def __delitem__(self, key: EconomicId) -> None:
        del self._values[key]

    def __iter__(self) -> Iterator[EconomicId]:
        raise RuntimeError("injected replay-index freeze failure")

    def __len__(self) -> int:
        return len(self._values)


def test_policy_factory_sorts_limits_and_emits_literal_canonical_document() -> None:
    spec_set = _spec_set(
        _spec(),
        _spec(
            instrument=SECOND_INSTRUMENT,
            specification_id="xnys.ibm.v1",
        ),
    )
    policy = _policy(
        spec_set,
        _limit(instrument=SECOND_INSTRUMENT, order="3", position="7"),
        _limit(),
    )

    assert tuple(item.instrument for item in policy.instrument_limits) == (
        INSTRUMENT,
        SECOND_INSTRUMENT,
    )
    document = json.loads(canonical_phase1_risk_policy_bytes(policy))
    assert document["canonicalization"] == "ea-risk-policy-v1"
    assert document["default_action"] == "deny"
    assert document["message_type"] == "phase1_risk_policy"
    assert document["schema_version"] == 1
    assert document["policy_id"] == "phase1.risk.v1"
    assert document["instrument_limits"][0]["instrument"] == {
        "symbol": "AAPL",
        "venue": "XNAS",
    }
    assert len(phase1_risk_policy_digest(policy).value) == 64
    with pytest.raises(FrozenInstanceError):
        policy.instrument_limits = ()  # type: ignore[misc]
    with pytest.raises(TypeError, match="created only"):
        Phase1RiskPolicy()


@pytest.mark.parametrize(
    ("policy_id", "code"),
    [
        (cast(str, 1), OutcomeCode.INVALID_TYPE),
        ("Uppercase", OutcomeCode.OUT_OF_RANGE),
        ("", OutcomeCode.OUT_OF_RANGE),
    ],
)
def test_policy_id_has_exact_token_contract(policy_id: str, code: OutcomeCode) -> None:
    with pytest.raises(RiskContractError) as error:
        RiskPolicyId(policy_id)
    assert error.value.code is code


def test_policy_rejects_duplicate_absent_and_non_quantized_limits() -> None:
    spec_set = _spec_set(_spec(quantity_quantum="0.5"))
    duplicate = _limit(order="1", position="2")
    with pytest.raises(RiskContractError) as duplicate_error:
        _policy(spec_set, duplicate, duplicate)
    assert duplicate_error.value.code is OutcomeCode.CONFLICTING_ID

    with pytest.raises(RiskContractError) as absent_error:
        _policy(spec_set, _limit(instrument=SECOND_INSTRUMENT))
    assert absent_error.value.code is OutcomeCode.OUT_OF_RANGE

    with pytest.raises(RiskContractError) as grid_error:
        _policy(spec_set, _limit(order="1.1", position="2"))
    assert grid_error.value.code is OutcomeCode.NOT_QUANTIZED


def test_risk_value_factories_reject_wrong_exact_boundary_types() -> None:
    spec_set = _spec_set()
    valid_limit = _limit()
    with pytest.raises(TypeError, match="permitted OutcomeCode"):
        RiskContractError(OutcomeCode.RISK_ALLOWED, "wrong family")
    with pytest.raises(RiskContractError) as instrument_error:
        InstrumentRiskLimit(
            instrument=cast(Instrument, "XNAS:AAPL"),
            maximum_order_quantity=CanonicalDecimal("1"),
            maximum_absolute_position=CanonicalDecimal("1"),
        )
    assert instrument_error.value.code is OutcomeCode.INVALID_TYPE

    invalid_calls: tuple[Callable[[], Phase1RiskPolicy], ...] = (
        lambda: create_phase1_risk_policy(
            policy_id=cast(RiskPolicyId, "phase1.risk.v1"),
            spec_set=spec_set,
            execution_policy=EXECUTION_POLICY,
            instrument_limits=(valid_limit,),
        ),
        lambda: create_phase1_risk_policy(
            policy_id=RiskPolicyId("phase1.risk.v1"),
            spec_set=cast(InstrumentExecutionSpecSet, None),
            execution_policy=EXECUTION_POLICY,
            instrument_limits=(valid_limit,),
        ),
        lambda: create_phase1_risk_policy(
            policy_id=RiskPolicyId("phase1.risk.v1"),
            spec_set=spec_set,
            execution_policy=cast(ExecutionPolicyRef, None),
            instrument_limits=(valid_limit,),
        ),
        lambda: create_phase1_risk_policy(
            policy_id=RiskPolicyId("phase1.risk.v1"),
            spec_set=spec_set,
            execution_policy=EXECUTION_POLICY,
            instrument_limits=cast(tuple[InstrumentRiskLimit, ...], None),
        ),
        lambda: create_phase1_risk_policy(
            policy_id=RiskPolicyId("phase1.risk.v1"),
            spec_set=spec_set,
            execution_policy=EXECUTION_POLICY,
            instrument_limits=cast(
                tuple[InstrumentRiskLimit, ...],
                ("not-a-limit",),
            ),
        ),
    )
    for invalid_call in invalid_calls:
        with pytest.raises(RiskContractError) as error:
            invalid_call()
        assert error.value.code is OutcomeCode.INVALID_TYPE


def test_factory_creates_exact_version_zero_state_and_protected_surface() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set, _limit())
    authority = _authority(spec_set, policy)

    assert authority.policy is policy
    assert authority.risk_state.risk_state_version == 0
    assert authority.risk_state.halted is False
    assert authority.decisions == ()
    assert authority.evidence == ()
    assert authority.results == ()
    document = json.loads(canonical_risk_state_snapshot_bytes(authority.risk_state))
    assert document == {
        "canonicalization": "ea-risk-state-v1",
        "conflict_existing_intent_sha256": None,
        "conflict_submitted_intent_sha256": None,
        "halt_causal_root_available_at": None,
        "halt_dispatch_sequence": None,
        "halt_reason": None,
        "halted": False,
        "message_type": "risk_state_snapshot",
        "policy_id": "phase1.risk.v1",
        "policy_sha256": phase1_risk_policy_digest(policy).value,
        "risk_state_version": 0,
        "run_id": RUN_ID.value,
        "schema_version": 1,
    }
    assert len(risk_state_snapshot_digest(authority.risk_state).value) == 64
    with pytest.raises(TypeError, match="created only"):
        Phase1RiskAuthority()
    with pytest.raises(TypeError, match="created only"):
        RiskStateSnapshot()
    with pytest.raises(TypeError, match="created only"):
        RiskEvaluationEvidence()
    with pytest.raises(TypeError, match="created only"):
        RiskEvaluationResult()


def test_allow_registers_exact_decision_approval_and_evidence() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set, _limit())
    authority = _authority(spec_set, policy)
    intent = _intent(spec_set)

    result = authority.evaluate(intent, _snapshot(spec_set))

    assert result.decision.kind is RiskDecisionKind.ALLOW
    assert result.decision.outcome_code is OutcomeCode.RISK_ALLOWED
    assert result.decision.approved_quantity == CanonicalDecimal("2")
    assert result.decision.approval is not None
    assert result.decision.decision_id == _id(EconomicOwnerKind.RISK_DECISION, 1)
    assert result.decision.approval.approval_id == _id(EconomicOwnerKind.RISK_APPROVAL, 1)
    assert result.evidence.reason_code is RiskReasonCode.WITHIN_LIMITS
    assert result.evidence.decision_next_before == 1
    assert result.evidence.decision_next_after == 2
    assert result.evidence.approval_next_before == 1
    assert result.evidence.approval_next_after == 2
    assert result.evidence.approval_sha256 is not None
    assert authority.decisions == (result.decision,)
    assert authority.evidence == (result.evidence,)
    assert authority.results == (result,)
    evidence_document = json.loads(canonical_risk_evaluation_evidence_bytes(result.evidence))
    assert evidence_document["reason_code"] == "within_limits"
    assert evidence_document["decision_id"]["owner_kind"] == "risk.decision"
    assert len(risk_evaluation_evidence_digest(result.evidence).value) == 64


@pytest.mark.parametrize(
    ("order_limit", "position_limit", "position", "expected", "reason"),
    [
        ("3", "10", None, "3", RiskReasonCode.RESIZED_ORDER_LIMIT),
        ("10", "10", "8", "2", RiskReasonCode.RESIZED_POSITION_LIMIT),
        ("2", "10", "8", "2", RiskReasonCode.RESIZED_ORDER_AND_POSITION_LIMITS),
    ],
)
def test_resize_uses_greatest_capacity_and_exact_binding_reason(
    order_limit: str,
    position_limit: str,
    position: str | None,
    expected: str,
    reason: RiskReasonCode,
) -> None:
    spec_set = _spec_set()
    authority = _authority(
        spec_set,
        _policy(spec_set, _limit(order=order_limit, position=position_limit)),
    )
    version = 0 if position is None else 1

    result = authority.evaluate(
        _intent(spec_set, quantity="5", snapshot_version=version),
        _snapshot(spec_set, position=position),
    )

    assert result.decision.kind is RiskDecisionKind.RESIZE
    assert result.decision.approved_quantity == CanonicalDecimal(expected)
    assert result.evidence.reason_code is reason


@pytest.mark.parametrize(
    ("side", "position", "quantity", "kind", "approved"),
    [
        (OrderSide.BUY, "12", "1", RiskDecisionKind.REJECT, None),
        (OrderSide.SELL, "12", "5", RiskDecisionKind.ALLOW, "5"),
        (OrderSide.SELL, "12", "25", RiskDecisionKind.RESIZE, "22"),
        (OrderSide.SELL, "-12", "1", RiskDecisionKind.REJECT, None),
        (OrderSide.BUY, "-12", "5", RiskDecisionKind.ALLOW, "5"),
        (OrderSide.BUY, "-12", "25", RiskDecisionKind.RESIZE, "22"),
    ],
)
def test_out_of_limit_positions_allow_only_safe_or_reducing_capacity(
    side: OrderSide,
    position: str,
    quantity: str,
    kind: RiskDecisionKind,
    approved: str | None,
) -> None:
    spec_set = _spec_set()
    authority = _authority(
        spec_set,
        _policy(spec_set, _limit(order="30", position="10")),
    )

    result = authority.evaluate(
        _intent(
            spec_set,
            side=side,
            quantity=quantity,
            snapshot_version=1,
        ),
        _snapshot(spec_set, position=position),
    )

    assert result.decision.kind is kind
    assert result.decision.approved_quantity == (
        None if approved is None else CanonicalDecimal(approved)
    )


def test_deny_by_default_and_halt_are_replay_stable_rejections() -> None:
    spec_set = _spec_set(
        _spec(),
        _spec(
            instrument=SECOND_INSTRUMENT,
            specification_id="xnys.ibm.v1",
        ),
    )
    policy = _policy(spec_set, _limit())
    authority = _authority(spec_set, policy)
    denied = authority.evaluate(
        _intent(spec_set, instrument=SECOND_INSTRUMENT),
        _snapshot(spec_set),
    )
    assert denied.decision.kind is RiskDecisionKind.REJECT
    assert denied.evidence.reason_code is RiskReasonCode.INSTRUMENT_NOT_CONFIGURED

    halted = _authority(spec_set, policy)
    state = halted.engage_halt(RiskHaltReason.KILL_SWITCH, TIME, 7)
    rejected = halted.evaluate(_intent(spec_set), _snapshot(spec_set))
    assert state.risk_state_version == 1
    assert rejected.decision.kind is RiskDecisionKind.REJECT
    assert rejected.evidence.reason_code is RiskReasonCode.HALTED
    assert rejected.decision.risk_state_version == 1


@pytest.mark.parametrize(
    ("intent_policy", "snapshot", "reason"),
    [
        (
            ExecutionPolicyRef(ExecutionPolicyId("foreign.v1"), Sha256Digest("4" * 64)),
            None,
            RiskReasonCode.LINEAGE_MISMATCH,
        ),
        (EXECUTION_POLICY, "stale", RiskReasonCode.STALE_PORTFOLIO_SNAPSHOT),
        (EXECUTION_POLICY, "newer", RiskReasonCode.LINEAGE_MISMATCH),
        (EXECUTION_POLICY, "foreign_run", RiskReasonCode.LINEAGE_MISMATCH),
        (EXECUTION_POLICY, "foreign_set", RiskReasonCode.LINEAGE_MISMATCH),
        (EXECUTION_POLICY, "wrong_grid", RiskReasonCode.LINEAGE_MISMATCH),
    ],
)
def test_lineage_failures_register_one_evaluation_failed_decision(
    intent_policy: ExecutionPolicyRef,
    snapshot: str | None,
    reason: RiskReasonCode,
) -> None:
    spec_set = _spec_set()
    authority = _authority(spec_set, _policy(spec_set, _limit()))
    intent_version = 1 if snapshot in {"stale", "wrong_grid"} else 0
    intent = _intent(
        spec_set,
        execution_policy=intent_policy,
        snapshot_version=intent_version,
    )
    if snapshot is None or snapshot == "stale":
        supplied = _snapshot(spec_set)
    elif snapshot == "newer":
        supplied = _snapshot(spec_set, position="1")
    elif snapshot == "foreign_run":
        supplied = _snapshot(spec_set, run_id=OTHER_RUN_ID)
    elif snapshot == "foreign_set":
        supplied = _snapshot(
            spec_set,
            set_id=InstrumentSpecSetId("foreign.v1"),
            set_sha256=Sha256Digest("5" * 64),
        )
    elif snapshot == "wrong_grid":
        supplied = _snapshot(
            spec_set,
            position="1",
            position_quantum="0.5",
        )
    else:
        raise AssertionError("unknown test case")

    result = authority.evaluate(intent, supplied)

    assert result.decision.kind is RiskDecisionKind.EVALUATION_FAILED
    assert result.evidence.reason_code is reason
    assert result.decision.approval is None
    assert result.evidence.approval_next_before == 1
    assert result.evidence.approval_next_after == 1
    assert result.evidence.portfolio_snapshot_version == supplied.snapshot_version
    assert result.evidence.portfolio_snapshot_sha256 == portfolio_snapshot_digest(supplied)


def test_exact_replay_returns_original_object_after_newer_state_and_halt() -> None:
    spec_set = _spec_set()
    authority = _authority(spec_set, _policy(spec_set, _limit()))
    intent = _intent(spec_set)
    original_snapshot = _snapshot(spec_set)
    original = authority.evaluate(intent, original_snapshot)
    authority.engage_halt(RiskHaltReason.EXTERNAL_SAFETY_HALT, TIME + timedelta(seconds=1), 9)

    replay = authority.evaluate(intent, _snapshot(spec_set, position="7"))

    assert replay is original
    assert authority.results == (original,)
    assert authority.risk_state.halted is True


def test_unbounded_dispatch_evaluates_and_replays_after_newer_state() -> None:
    spec_set = _spec_set()
    authority = _authority(spec_set, _policy(spec_set, _limit()))
    dispatch = 1 << 64
    intent = _intent(spec_set, dispatch_sequence=dispatch)
    original = authority.evaluate(intent, _snapshot(spec_set))
    authority.engage_halt(
        RiskHaltReason.EXTERNAL_SAFETY_HALT,
        TIME + timedelta(seconds=1),
        dispatch + 1,
    )
    state_before = authority._state

    replay = authority.evaluate(intent, _snapshot(spec_set, position="7"))

    assert replay is original
    assert authority._state is state_before
    assert authority.results == (original,)
    assert original.decision.dispatch_sequence == dispatch
    assert original.decision.approval is not None
    assert original.decision.approval.dispatch_sequence == dispatch


def test_same_id_different_bytes_engages_one_atomic_conflict_halt() -> None:
    spec_set = _spec_set()
    authority = _authority(spec_set, _policy(spec_set, _limit()))
    original = _intent(spec_set)
    authority.evaluate(original, _snapshot(spec_set))
    conflicting = _replace_intent(original, quantity=CanonicalDecimal("3"))
    before_decisions = authority.decisions

    with pytest.raises(RiskAuthorityError) as error:
        authority.evaluate(conflicting, _snapshot(spec_set))

    _assert_error(error, OutcomeCode.CONFLICTING_ID)
    first_halt = authority.risk_state
    assert first_halt.halt_reason is RiskHaltReason.INTENT_IDENTITY_CONFLICT
    assert first_halt.conflict_existing_intent_sha256 is not None
    assert first_halt.conflict_submitted_intent_sha256 is not None
    assert authority.decisions is before_decisions

    with pytest.raises(RiskAuthorityError):
        authority.evaluate(conflicting, _snapshot(spec_set))
    assert authority.risk_state is first_halt
    assert len(authority.decisions) == 1


def test_large_submitted_dispatch_engages_exact_conflict_halt() -> None:
    spec_set = _spec_set()
    authority = _authority(spec_set, _policy(spec_set, _limit()))
    original = _intent(spec_set)
    authority.evaluate(original, _snapshot(spec_set))
    submitted_dispatch = 1 << 64
    conflicting = _replace_intent(
        original,
        dispatch_sequence=submitted_dispatch,
    )

    with pytest.raises(RiskAuthorityError) as error:
        authority.evaluate(conflicting, _snapshot(spec_set))

    _assert_error(error, OutcomeCode.CONFLICTING_ID)
    state = authority.risk_state
    assert state.halt_reason is RiskHaltReason.INTENT_IDENTITY_CONFLICT
    assert state.halt_dispatch_sequence == submitted_dispatch
    assert (
        json.loads(canonical_risk_state_snapshot_bytes(state))["halt_dispatch_sequence"]
        == submitted_dispatch
    )


def test_large_conflict_after_existing_halt_never_rewrites_first_state() -> None:
    spec_set = _spec_set()
    authority = _authority(spec_set, _policy(spec_set, _limit()))
    original = _intent(spec_set)
    authority.evaluate(original, _snapshot(spec_set))
    first = authority.engage_halt(RiskHaltReason.KILL_SWITCH, TIME, 7)
    first_bytes = canonical_risk_state_snapshot_bytes(first)
    first_digest = risk_state_snapshot_digest(first)
    state_before = authority._state

    with pytest.raises(RiskAuthorityError) as error:
        authority.evaluate(
            _replace_intent(original, dispatch_sequence=1 << 512),
            _snapshot(spec_set),
        )

    _assert_error(error, OutcomeCode.CONFLICTING_ID)
    assert authority._state is state_before
    assert authority.risk_state is first
    assert canonical_risk_state_snapshot_bytes(authority.risk_state) == first_bytes
    assert risk_state_snapshot_digest(authority.risk_state) == first_digest


def test_digest_collision_does_not_downgrade_different_bytes_to_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_set = _spec_set()
    authority = _authority(spec_set, _policy(spec_set, _limit()))
    original = _intent(spec_set)
    collision = Sha256Digest("a" * 64)
    monkeypatch.setattr(authority_module, "order_intent_digest", lambda _: collision)
    monkeypatch.setattr(execution_messages_module, "order_intent_digest", lambda _: collision)
    authority.evaluate(original, _snapshot(spec_set))

    with pytest.raises(RiskAuthorityError) as error:
        authority.evaluate(
            _replace_intent(original, quantity=CanonicalDecimal("3")),
            _snapshot(spec_set),
        )

    _assert_error(error, OutcomeCode.CONFLICTING_ID)
    assert authority.risk_state.conflict_existing_intent_sha256 == collision
    assert authority.risk_state.conflict_submitted_intent_sha256 == collision


def test_public_halt_validates_command_and_never_rewrites_first_cause() -> None:
    spec_set = _spec_set()
    authority = _authority(spec_set, _policy(spec_set, _limit()))
    with pytest.raises(RiskAuthorityError) as internal:
        authority.engage_halt(RiskHaltReason.INTENT_IDENTITY_CONFLICT, TIME, 1)
    _assert_error(internal, OutcomeCode.OUT_OF_RANGE)
    with pytest.raises(RiskAuthorityError) as wrong_type:
        authority.engage_halt(cast(RiskHaltReason, "kill_switch"), TIME, 1)
    _assert_error(wrong_type, OutcomeCode.INVALID_TYPE)
    with pytest.raises(RiskAuthorityError) as wrong_time_type:
        authority.engage_halt(
            RiskHaltReason.KILL_SWITCH,
            cast(datetime, "2026-01-02T09:31:00Z"),
            1,
        )
    _assert_error(wrong_time_type, OutcomeCode.INVALID_TYPE)
    with pytest.raises(RiskAuthorityError) as non_utc:
        authority.engage_halt(RiskHaltReason.KILL_SWITCH, datetime(2026, 1, 1), 1)
    _assert_error(non_utc, OutcomeCode.OUT_OF_RANGE)
    first = authority.engage_halt(RiskHaltReason.KILL_SWITCH, TIME, 1 << 64)
    assert first.halt_dispatch_sequence == 1 << 64
    expected_bytes = (
        b'{"canonicalization":"ea-risk-state-v1",'
        b'"conflict_existing_intent_sha256":null,'
        b'"conflict_submitted_intent_sha256":null,'
        b'"halt_causal_root_available_at":"2026-01-02T09:31:00.000000Z",'
        b'"halt_dispatch_sequence":18446744073709551616,'
        b'"halt_reason":"kill_switch","halted":true,'
        b'"message_type":"risk_state_snapshot","policy_id":"phase1.risk.v1",'
        b'"policy_sha256":'
        b'"65d973538dc972f7dc566afb676a89548bcc88875e0b10464080bd52c3667be6",'
        b'"risk_state_version":1,'
        b'"run_id":"12345678-1234-4234-8234-123456789abc","schema_version":1}'
    )
    assert canonical_risk_state_snapshot_bytes(first) == expected_bytes
    assert (
        risk_state_snapshot_digest(first).value
        == "7bd69a50937391d0e900849e08840c2b0ce88ef320d0a56bec48f8a6f54d7a0e"
    )
    repeated = authority.engage_halt(
        RiskHaltReason.EXTERNAL_SAFETY_HALT,
        TIME + timedelta(days=1),
        1 << 512,
    )
    assert repeated is first
    assert repeated.halt_reason is RiskHaltReason.KILL_SWITCH


class _IntSubclass(int):
    pass


@pytest.mark.parametrize(
    ("dispatch", "code"),
    [
        (-1, OutcomeCode.OUT_OF_RANGE),
        (True, OutcomeCode.INVALID_TYPE),
        (_IntSubclass(1), OutcomeCode.INVALID_TYPE),
        (1.0, OutcomeCode.INVALID_TYPE),
        ("1", OutcomeCode.INVALID_TYPE),
    ],
)
def test_public_halt_rejects_invalid_dispatch_without_coercion_or_mutation(
    dispatch: object,
    code: OutcomeCode,
) -> None:
    spec_set = _spec_set()
    authority = _authority(spec_set, _policy(spec_set, _limit()))
    original_state = authority._state

    with pytest.raises(RiskAuthorityError) as error:
        authority.engage_halt(
            RiskHaltReason.KILL_SWITCH,
            TIME,
            cast(int, dispatch),
        )

    _assert_error(error, code)
    assert authority._state is original_state


def test_public_halt_preserves_very_large_exact_dispatch() -> None:
    spec_set = _spec_set()
    authority = _authority(spec_set, _policy(spec_set, _limit()))
    dispatch = (1 << 4096) + 73

    state = authority.engage_halt(RiskHaltReason.EXTERNAL_SAFETY_HALT, TIME, dispatch)

    assert state.halt_dispatch_sequence == dispatch
    assert (
        json.loads(canonical_risk_state_snapshot_bytes(state))["halt_dispatch_sequence"] == dispatch
    )


def test_decision_and_approval_exhaustion_follow_literal_transitions() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set, _limit())
    decision_exhausted = _authority(spec_set, policy)
    decision_exhausted._state = replace(decision_exhausted._state, decision_next=None)
    with pytest.raises(RiskAuthorityError) as decision_error:
        decision_exhausted.evaluate(_intent(spec_set), _snapshot(spec_set))
    _assert_error(decision_error, OutcomeCode.OUT_OF_RANGE)
    assert decision_exhausted.results == ()

    approval_exhausted = _authority(spec_set, policy)
    approval_exhausted._state = replace(approval_exhausted._state, approval_next=None)
    failed = approval_exhausted.evaluate(_intent(spec_set), _snapshot(spec_set))
    assert failed.decision.kind is RiskDecisionKind.EVALUATION_FAILED
    assert failed.evidence.reason_code is RiskReasonCode.APPROVAL_SEQUENCE_EXHAUSTED
    assert failed.evidence.approval_next_before is None
    assert failed.evidence.approval_next_after is None

    boundary = _authority(spec_set, policy)
    maximum = (1 << 64) - 1
    boundary._state = replace(
        boundary._state,
        decision_next=maximum,
        approval_next=maximum,
    )
    result = boundary.evaluate(_intent(spec_set), _snapshot(spec_set))
    assert result.decision.decision_id.owner_sequence == maximum
    assert result.decision.approval is not None
    assert result.decision.approval.approval_id.owner_sequence == maximum
    assert result.evidence.decision_next_after is None
    assert result.evidence.approval_next_after is None


def test_unrepresentable_side_capacity_registers_arithmetic_failure_without_approval() -> None:
    spec_set = _spec_set()
    maximum = "99999999999999999999"
    authority = _authority(
        spec_set,
        _policy(spec_set, _limit(order="1", position=maximum)),
    )

    result = authority.evaluate(
        _intent(
            spec_set,
            quantity="1",
            snapshot_version=1,
        ),
        _snapshot(spec_set, position=f"-{maximum}"),
    )

    assert result.decision.kind is RiskDecisionKind.EVALUATION_FAILED
    assert result.evidence.reason_code is RiskReasonCode.ARITHMETIC_FAILURE
    assert result.decision.approval is None
    assert result.evidence.approval_next_before == 1
    assert result.evidence.approval_next_after == 1


def test_structural_intent_errors_consume_no_ids_or_state() -> None:
    spec_set = _spec_set(_spec(quantity_quantum="1"))
    authority = _authority(spec_set, _policy(spec_set, _limit()))
    original_state = authority._state
    malformed = _replace_intent(_intent(spec_set), quantity=CanonicalDecimal("1.5"))

    with pytest.raises(RiskAuthorityError) as error:
        authority.evaluate(malformed, _snapshot(spec_set))

    _assert_error(error, OutcomeCode.NOT_QUANTIZED)
    assert authority._state is original_state
    assert authority.results == ()


@pytest.mark.parametrize(
    ("dispatch", "code"),
    [
        (-1, OutcomeCode.OUT_OF_RANGE),
        (True, OutcomeCode.INVALID_TYPE),
        (_IntSubclass(1), OutcomeCode.INVALID_TYPE),
        (1.0, OutcomeCode.INVALID_TYPE),
        ("1", OutcomeCode.INVALID_TYPE),
    ],
)
@pytest.mark.parametrize("occupied", [False, True])
def test_forged_dispatch_fails_before_lookup_without_mutation(
    dispatch: object,
    code: OutcomeCode,
    occupied: bool,
) -> None:
    spec_set = _spec_set()
    authority = _authority(spec_set, _policy(spec_set, _limit()))
    intent = _intent(spec_set, sequence=1 if occupied else 2)
    if occupied:
        authority.evaluate(intent, _snapshot(spec_set))
    original_state = authority._state
    malformed = _replace_intent(intent, dispatch_sequence=dispatch)

    with pytest.raises(RiskAuthorityError) as error:
        authority.evaluate(malformed, _snapshot(spec_set))

    _assert_error(error, code)
    assert authority._state is original_state
    assert authority.risk_state.halted is False


_EXECUTABLE_REGISTRATION_FAILURE_TARGETS = (
    ("authority", "canonical_order_intent_bytes"),
    ("authority", "order_intent_digest"),
    ("authority", "canonical_portfolio_snapshot_bytes"),
    ("authority", "portfolio_snapshot_digest"),
    ("authority", "allow_order_intent"),
    ("execution_messages", "_make_approval"),
    ("authority", "_create_risk_evaluation_evidence"),
    ("authority", "_create_risk_evaluation_result"),
    ("authority", "_ReplayRecord"),
    ("authority", "_freeze_state"),
    ("authority", "canonical_phase1_risk_policy_bytes"),
    ("authority", "phase1_risk_policy_digest"),
    ("authority", "canonical_risk_state_snapshot_bytes"),
    ("authority", "risk_state_snapshot_digest"),
    ("authority", "canonical_risk_decision_bytes"),
    ("authority", "risk_decision_digest"),
    ("authority", "canonical_execution_approval_bytes"),
    ("authority", "execution_approval_digest"),
    ("authority", "canonical_risk_evaluation_evidence_bytes"),
    ("authority", "risk_evaluation_evidence_digest"),
)


@pytest.mark.parametrize(
    ("module_name", "target"),
    _EXECUTABLE_REGISTRATION_FAILURE_TARGETS,
)
def test_executable_registration_failure_matrix_is_atomic(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    target: str,
) -> None:
    spec_set = _spec_set()
    authority = _authority(spec_set, _policy(spec_set, _limit()))
    original_state = authority._state
    original_bytes = canonical_risk_state_snapshot_bytes(authority.risk_state)
    original_digest = risk_state_snapshot_digest(authority.risk_state)
    module = authority_module if module_name == "authority" else execution_messages_module

    with monkeypatch.context() as patch:
        patch.setattr(module, target, _raise_injected)
        with pytest.raises(RuntimeError, match="injected pre-publication"):
            authority.evaluate(_intent(spec_set), _snapshot(spec_set))

    _assert_authority_unchanged(
        authority,
        original_state,
        original_bytes,
        original_digest,
    )


_NON_EXECUTABLE_REGISTRATION_FAILURE_TARGETS = (
    "canonical_order_intent_bytes",
    "order_intent_digest",
    "canonical_portfolio_snapshot_bytes",
    "portfolio_snapshot_digest",
    "reject_order_intent",
    "_create_risk_evaluation_evidence",
    "_create_risk_evaluation_result",
    "_ReplayRecord",
    "_freeze_state",
    "canonical_phase1_risk_policy_bytes",
    "phase1_risk_policy_digest",
    "canonical_risk_state_snapshot_bytes",
    "risk_state_snapshot_digest",
    "canonical_risk_decision_bytes",
    "risk_decision_digest",
    "canonical_risk_evaluation_evidence_bytes",
    "risk_evaluation_evidence_digest",
)


@pytest.mark.parametrize("target", _NON_EXECUTABLE_REGISTRATION_FAILURE_TARGETS)
def test_non_executable_registration_failure_matrix_is_atomic(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    spec_set = _spec_set()
    authority = _authority(spec_set, _policy(spec_set))
    original_state = authority._state
    original_bytes = canonical_risk_state_snapshot_bytes(authority.risk_state)
    original_digest = risk_state_snapshot_digest(authority.risk_state)

    with monkeypatch.context() as patch:
        patch.setattr(authority_module, target, _raise_injected)
        with pytest.raises(RuntimeError, match="injected pre-publication"):
            authority.evaluate(_intent(spec_set), _snapshot(spec_set))

    _assert_authority_unchanged(
        authority,
        original_state,
        original_bytes,
        original_digest,
    )


def test_actual_replay_index_copy_failure_is_atomic() -> None:
    spec_set = _spec_set()
    authority = _authority(spec_set, _policy(spec_set, _limit()))
    authority._state = replace(
        authority._state,
        replay_index=cast(Any, _FailsDuringReplayCopy()),
    )
    original_state = authority._state
    original_bytes = canonical_risk_state_snapshot_bytes(authority.risk_state)
    original_digest = risk_state_snapshot_digest(authority.risk_state)

    with pytest.raises(RuntimeError, match="injected replay-index copy failure"):
        authority.evaluate(_intent(spec_set), _snapshot(spec_set))

    _assert_authority_unchanged(
        authority,
        original_state,
        original_bytes,
        original_digest,
        compare_replay_contents=False,
    )


def test_real_freeze_state_replay_mapping_copy_failure_is_atomic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_set = _spec_set()
    authority = _authority(spec_set, _policy(spec_set, _limit()))
    original_state = authority._state
    original_bytes = canonical_risk_state_snapshot_bytes(authority.risk_state)
    original_digest = risk_state_snapshot_digest(authority.risk_state)

    def return_freeze_failure_mapping(_: object) -> Any:
        return _FailsDuringReplayFreeze()

    with monkeypatch.context() as patch:
        patch.setattr(
            authority_module,
            "_copy_replay_index",
            return_freeze_failure_mapping,
        )
        with pytest.raises(RuntimeError, match="injected replay-index freeze failure"):
            authority.evaluate(_intent(spec_set), _snapshot(spec_set))

    _assert_authority_unchanged(
        authority,
        original_state,
        original_bytes,
        original_digest,
    )


@pytest.mark.parametrize(
    ("case", "module_name", "target"),
    [
        ("allow", "authority", "allow_order_intent"),
        ("resize", "authority", "resize_order_intent"),
        ("reject", "authority", "reject_order_intent"),
        ("evaluation_failed", "authority", "fail_order_intent_evaluation"),
        ("approval", "execution_messages", "_make_approval"),
    ],
)
def test_every_decision_and_approval_construction_path_is_atomic(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    module_name: str,
    target: str,
) -> None:
    spec_set = _spec_set()
    if case == "resize":
        authority = _authority(
            spec_set,
            _policy(spec_set, _limit(order="1", position="10")),
        )
        intent = _intent(spec_set, quantity="2")
    elif case == "reject":
        authority = _authority(spec_set, _policy(spec_set))
        intent = _intent(spec_set)
    elif case == "evaluation_failed":
        authority = _authority(spec_set, _policy(spec_set, _limit()))
        intent = _intent(
            spec_set,
            execution_policy=ExecutionPolicyRef(
                ExecutionPolicyId("foreign.execution.v1"),
                Sha256Digest("9" * 64),
            ),
        )
    else:
        authority = _authority(spec_set, _policy(spec_set, _limit()))
        intent = _intent(spec_set)
    original_state = authority._state
    original_bytes = canonical_risk_state_snapshot_bytes(authority.risk_state)
    original_digest = risk_state_snapshot_digest(authority.risk_state)
    module = authority_module if module_name == "authority" else execution_messages_module

    with monkeypatch.context() as patch:
        patch.setattr(module, target, _raise_injected)
        with pytest.raises(RuntimeError, match="injected pre-publication"):
            authority.evaluate(intent, _snapshot(spec_set))

    _assert_authority_unchanged(
        authority,
        original_state,
        original_bytes,
        original_digest,
    )


@pytest.mark.parametrize(
    "target",
    [
        "phase1_risk_policy_digest",
        "_create_risk_state_snapshot",
        "_freeze_state",
        "canonical_risk_state_snapshot_bytes",
        "risk_state_snapshot_digest",
    ],
)
def test_public_halt_failure_matrix_is_atomic(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    spec_set = _spec_set()
    authority = _authority(spec_set, _policy(spec_set, _limit()))
    original_state = authority._state
    original_bytes = canonical_risk_state_snapshot_bytes(authority.risk_state)
    original_digest = risk_state_snapshot_digest(authority.risk_state)

    with monkeypatch.context() as patch:
        patch.setattr(authority_module, target, _raise_injected)
        with pytest.raises(RuntimeError, match="injected pre-publication"):
            authority.engage_halt(RiskHaltReason.KILL_SWITCH, TIME, 1 << 64)

    _assert_authority_unchanged(
        authority,
        original_state,
        original_bytes,
        original_digest,
    )


@pytest.mark.parametrize(
    "target",
    [
        "phase1_risk_policy_digest",
        "_create_risk_state_snapshot",
        "_freeze_state",
        "canonical_risk_state_snapshot_bytes",
        "risk_state_snapshot_digest",
    ],
)
def test_identity_conflict_halt_failure_matrix_is_atomic(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    spec_set = _spec_set()
    authority = _authority(spec_set, _policy(spec_set, _limit()))
    original = _intent(spec_set)
    authority.evaluate(original, _snapshot(spec_set))
    original_state = authority._state
    original_bytes = canonical_risk_state_snapshot_bytes(authority.risk_state)
    original_digest = risk_state_snapshot_digest(authority.risk_state)

    with monkeypatch.context() as patch:
        patch.setattr(authority_module, target, _raise_injected)
        with pytest.raises(RuntimeError, match="injected pre-publication"):
            authority.evaluate(
                _replace_intent(original, dispatch_sequence=1 << 64),
                _snapshot(spec_set),
            )

    _assert_authority_unchanged(
        authority,
        original_state,
        original_bytes,
        original_digest,
    )


def test_authority_factory_rejects_conflicting_bindings() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set, _limit())
    foreign_policy = ExecutionPolicyRef(
        ExecutionPolicyId("foreign.execution.v1"),
        Sha256Digest("9" * 64),
    )
    with pytest.raises(RiskAuthorityError) as error:
        _authority(spec_set, policy, execution_policy=foreign_policy)
    _assert_error(error, OutcomeCode.CONFLICTING_ID)

    other = Instrument(VenueId("XASE"), "MSFT")
    conflicting_quanta = _spec_set(
        _spec(),
        _spec(
            instrument=other,
            specification_id="xase.msft.v1",
            currency_quantum="1",
        ),
    )
    conflicting_policy = _policy(conflicting_quanta, _limit())
    with pytest.raises(RiskAuthorityError) as currency_error:
        _authority(conflicting_quanta, conflicting_policy)
    _assert_error(currency_error, OutcomeCode.CONFLICTING_ID)


def test_authority_public_boundary_rejects_wrong_exact_types() -> None:
    spec_set = _spec_set()
    authority = _authority(spec_set, _policy(spec_set, _limit()))
    snapshot = _snapshot(spec_set)
    with pytest.raises(TypeError, match="permitted OutcomeCode"):
        RiskAuthorityError(OutcomeCode.RISK_ALLOWED, "wrong family")
    with pytest.raises(RiskAuthorityError) as intent_type:
        authority.evaluate(cast(OrderIntent, None), snapshot)
    _assert_error(intent_type, OutcomeCode.INVALID_TYPE)
    with pytest.raises(RiskAuthorityError) as snapshot_type:
        authority.evaluate(_intent(spec_set), cast(PortfolioSnapshot, None))
    _assert_error(snapshot_type, OutcomeCode.INVALID_TYPE)
    forged = _replace_intent(_intent(spec_set), intent_id="portfolio.intent:1")
    with pytest.raises(RiskAuthorityError) as intent_id_type:
        authority.evaluate(forged, snapshot)
    _assert_error(intent_id_type, OutcomeCode.INVALID_TYPE)
