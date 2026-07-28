from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, MutableMapping
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from types import MappingProxyType
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
    RiskDecision,
    RiskEvaluationEvidence,
    RiskEvaluationResult,
    RiskHaltReason,
    RiskPolicyId,
    RiskReasonCode,
    RunId,
    SettlementCurrency,
    Sha256Digest,
    TargetLineageRef,
    VenueId,
    allow_order_intent,
    build_instrument_spec_set,
    canonical_execution_request_bytes,
    canonical_order_bytes,
    canonical_order_intent_bytes,
    canonical_risk_decision_bytes,
    canonical_risk_evaluation_evidence_bytes,
    create_order_intent,
    create_phase1_risk_policy,
    execution_approval_digest,
    execution_request_digest,
    order_client_submission_key,
    order_digest,
    order_intent_digest,
    phase1_risk_policy_digest,
    portfolio_snapshot_digest,
    risk_decision_digest,
)
from ea.core.execution import InstrumentExecutionSpecSet
from ea.execution import (
    ExecutionAuthorityError,
    Phase1OrderAuthority,
    create_phase1_order_authority,
)
from ea.execution import authority as authority_module
from ea.risk import Phase1RiskAuthority, create_phase1_risk_authority

RUN_ID = RunId("12345678-1234-4234-8234-123456789abc")
OTHER_RUN_ID = RunId("87654321-4321-4321-8321-cba987654321")
INSTRUMENT = Instrument(VenueId("XNAS"), "AAPL")
TIME = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
EXECUTION_POLICY = ExecutionPolicyRef(
    ExecutionPolicyId("phase1.execution.v1"),
    Sha256Digest("1" * 64),
)
MAX_UINT64 = (1 << 64) - 1


def _id(
    owner: EconomicOwnerKind,
    sequence: int,
    *,
    run_id: RunId = RUN_ID,
) -> EconomicId:
    return EconomicId(run_id, owner, sequence)


def _spec_set(*, quantity_quantum: str = "1") -> InstrumentExecutionSpecSet:
    specification = InstrumentExecutionSpec(
        instrument=INSTRUMENT,
        specification_id=InstrumentSpecId("xnas.aapl.v1"),
        price_quantum=CanonicalDecimal("0.01"),
        quantity_quantum=CanonicalDecimal(quantity_quantum),
        settlement_currency=SettlementCurrency("USD"),
        currency_quantum=CanonicalDecimal("0.01"),
        contract_multiplier=CanonicalDecimal("1"),
        price_domain=PriceDomain.POSITIVE,
    )
    return build_instrument_spec_set(
        InstrumentSpecSetId("phase1.test.v1"),
        (specification,),
    )


def _policy(
    spec_set: InstrumentExecutionSpecSet,
    *,
    order_limit: str = "5",
    position_limit: str = "10",
    execution_policy: ExecutionPolicyRef = EXECUTION_POLICY,
) -> Phase1RiskPolicy:
    return create_phase1_risk_policy(
        policy_id=RiskPolicyId("phase1.risk.v1"),
        spec_set=spec_set,
        execution_policy=execution_policy,
        instrument_limits=(
            InstrumentRiskLimit(
                instrument=INSTRUMENT,
                maximum_order_quantity=CanonicalDecimal(order_limit),
                maximum_absolute_position=CanonicalDecimal(position_limit),
            ),
        ),
    )


def _intent(
    spec_set: InstrumentExecutionSpecSet,
    *,
    sequence: int = 1,
    quantity: str = "2",
    run_id: RunId = RUN_ID,
    execution_policy: ExecutionPolicyRef = EXECUTION_POLICY,
    portfolio_snapshot_version: int = 0,
) -> OrderIntent:
    return create_order_intent(
        run_id=run_id,
        intent_id=_id(EconomicOwnerKind.PORTFOLIO_INTENT, sequence, run_id=run_id),
        correlation_id=_id(EconomicOwnerKind.STRATEGY_SIGNAL, 1, run_id=run_id),
        target_lineage=TargetLineageRef(
            _id(EconomicOwnerKind.PORTFOLIO_TARGET, 1, run_id=run_id),
            Sha256Digest("2" * 64),
        ),
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        quantity=CanonicalDecimal(quantity),
        portfolio_snapshot_version=portfolio_snapshot_version,
        causal_root_available_at=TIME,
        dispatch_sequence=sequence,
        spec_set=spec_set,
        execution_policy=execution_policy,
    )


def _snapshot(
    spec_set: InstrumentExecutionSpecSet,
    *,
    position: str | None = None,
) -> PortfolioSnapshot:
    from ea.core import instrument_spec_set_digest

    version = 0 if position is None else 1
    return PortfolioSnapshot(
        run_id=RUN_ID,
        instrument_spec_set_id=spec_set.identifier,
        instrument_spec_set_sha256=instrument_spec_set_digest(spec_set),
        snapshot_version=version,
        ledger_sequence=version,
        last_entry_id=(None if version == 0 else _id(EconomicOwnerKind.LEDGER_ENTRY, version)),
        last_transaction_sha256=None if version == 0 else Sha256Digest("3" * 64),
        cash_balances=(),
        position_balances=(
            ()
            if position is None
            else (
                PositionBalance(
                    instrument=INSTRUMENT,
                    quantity_quantum=spec_set.specifications[0].quantity_quantum,
                    quantity=CanonicalDecimal(position),
                ),
            )
        ),
        rounding_balances=(),
        unresolved_fills=(),
    )


def _risk_authority(
    spec_set: InstrumentExecutionSpecSet,
    policy: Phase1RiskPolicy,
) -> Phase1RiskAuthority:
    return create_phase1_risk_authority(
        run_id=RUN_ID,
        spec_set=spec_set,
        execution_policy=EXECUTION_POLICY,
        policy=policy,
    )


def _order_authority(
    spec_set: InstrumentExecutionSpecSet,
    policy: Phase1RiskPolicy,
    risk_result_verifier: object | None = None,
) -> Phase1OrderAuthority:
    verifier = (
        _risk_authority(spec_set, policy) if risk_result_verifier is None else risk_result_verifier
    )
    return create_phase1_order_authority(
        run_id=RUN_ID,
        spec_set=spec_set,
        execution_policy=EXECUTION_POLICY,
        risk_policy=policy,
        risk_result_verifier=cast(Any, verifier),
    )


class _VerifierStub:
    def __init__(
        self,
        spec_set: InstrumentExecutionSpecSet,
        policy: Phase1RiskPolicy,
        *,
        run_id: object = RUN_ID,
        execution_policy: object = EXECUTION_POLICY,
        response: object = True,
        error: Exception | None = None,
    ) -> None:
        self.run_id = run_id
        self.spec_set = spec_set
        self.execution_policy = execution_policy
        self.policy = policy
        self.response = response
        self.error = error
        self.calls = 0

    def has_issued_result(
        self,
        *,
        intent_id: EconomicId,
        canonical_intent_bytes: bytes,
        canonical_decision_bytes: bytes,
        canonical_evidence_bytes: bytes,
    ) -> bool:
        assert type(intent_id) is EconomicId
        assert type(canonical_intent_bytes) is bytes
        assert type(canonical_decision_bytes) is bytes
        assert type(canonical_evidence_bytes) is bytes
        self.calls += 1
        if self.error is not None:
            raise self.error
        return cast(bool, self.response)


def _coherent_allow_result(
    *,
    intent: OrderIntent,
    spec_set: InstrumentExecutionSpecSet,
    template: RiskEvaluationResult,
) -> RiskEvaluationResult:
    assert template.decision.approval is not None
    decision = allow_order_intent(
        decision_id=template.decision.decision_id,
        approval_id=template.decision.approval.approval_id,
        intent=intent,
        spec_set=spec_set,
        risk_state_version=template.decision.risk_state_version,
    )
    assert decision.approval is not None
    evidence = _replace_slots(
        template.evidence,
        decision_sha256=risk_decision_digest(decision),
        approval_sha256=execution_approval_digest(decision.approval),
        reason_code=RiskReasonCode.WITHIN_LIMITS,
    )
    return _result(decision, evidence)


def _replace_slots(value: object, **changes: object) -> Any:
    replacement = object.__new__(type(value))
    slots = cast(tuple[str, ...], cast(Any, type(value)).__slots__)
    for name in slots:
        object.__setattr__(
            replacement,
            name,
            changes.get(name, getattr(value, name)),
        )
    return replacement


def _result(
    decision: RiskDecision,
    evidence: RiskEvaluationEvidence,
) -> RiskEvaluationResult:
    value = object.__new__(RiskEvaluationResult)
    object.__setattr__(value, "decision", decision)
    object.__setattr__(value, "evidence", evidence)
    return value


def _assert_error(
    error: pytest.ExceptionInfo[ExecutionAuthorityError],
    code: OutcomeCode,
) -> None:
    assert error.value.code is code


def _assert_execution_state_unchanged(
    authority: Phase1OrderAuthority,
    original_state: object,
) -> None:
    state = cast(Any, original_state)
    assert authority._state is state
    assert authority.next_order_sequence == state.order_next
    assert authority._state.approval_index is state.approval_index
    assert authority._state.intent_index is state.intent_index
    assert authority._state.decision_index is state.decision_index
    assert authority.orders is state.orders


def _raise_injected(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("injected pre-publication failure")


def test_factory_freezes_contract_bindings_and_initial_state() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    authority = _order_authority(spec_set, policy)

    assert authority.run_id is RUN_ID
    assert authority.spec_set is spec_set
    assert authority.execution_policy is EXECUTION_POLICY
    assert authority.risk_policy is policy
    assert authority.next_order_sequence == 1
    assert authority.orders == ()
    assert authority._state.approval_index == {}
    assert authority._state.intent_index == {}
    assert authority._state.decision_index == {}
    with pytest.raises(TypeError, match="created only"):
        Phase1OrderAuthority()
    with pytest.raises(TypeError):
        authority._state.approval_index[_id(EconomicOwnerKind.RISK_APPROVAL, 1)] = cast(  # type: ignore[index]
            Any,
            None,
        )
    with pytest.raises(FrozenInstanceError):
        authority._state.order_next = 2  # type: ignore[misc]


def test_factory_rejects_wrong_types_and_conflicting_policy_lineage() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    risk = _risk_authority(spec_set, policy)
    invalid_calls: tuple[Callable[[], Phase1OrderAuthority], ...] = (
        lambda: create_phase1_order_authority(
            run_id=cast(RunId, "run"),
            spec_set=spec_set,
            execution_policy=EXECUTION_POLICY,
            risk_policy=policy,
            risk_result_verifier=risk,
        ),
        lambda: create_phase1_order_authority(
            run_id=RUN_ID,
            spec_set=cast(InstrumentExecutionSpecSet, None),
            execution_policy=EXECUTION_POLICY,
            risk_policy=policy,
            risk_result_verifier=risk,
        ),
        lambda: create_phase1_order_authority(
            run_id=RUN_ID,
            spec_set=spec_set,
            execution_policy=cast(ExecutionPolicyRef, None),
            risk_policy=policy,
            risk_result_verifier=risk,
        ),
        lambda: create_phase1_order_authority(
            run_id=RUN_ID,
            spec_set=spec_set,
            execution_policy=EXECUTION_POLICY,
            risk_policy=cast(Phase1RiskPolicy, None),
            risk_result_verifier=risk,
        ),
    )
    for invalid_call in invalid_calls:
        with pytest.raises(ExecutionAuthorityError) as error:
            invalid_call()
        _assert_error(error, OutcomeCode.INVALID_TYPE)

    foreign_policy = _policy(
        spec_set,
        execution_policy=ExecutionPolicyRef(
            ExecutionPolicyId("foreign.execution.v1"),
            Sha256Digest("9" * 64),
        ),
    )
    with pytest.raises(ExecutionAuthorityError) as conflict:
        create_phase1_order_authority(
            run_id=RUN_ID,
            spec_set=spec_set,
            execution_policy=EXECUTION_POLICY,
            risk_policy=foreign_policy,
            risk_result_verifier=risk,
        )
    _assert_error(conflict, OutcomeCode.CONFLICTING_ID)


def test_factory_closes_verifier_shape_type_and_binding_error_matrix() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)

    class NonCallableVerifier:
        run_id = RUN_ID
        execution_policy = EXECUTION_POLICY
        has_issued_result: object = None

        def __init__(self) -> None:
            self.spec_set = spec_set
            self.policy = policy

    wrong_policy = _policy(spec_set, order_limit="4")
    malformed: tuple[object, ...] = (
        object(),
        NonCallableVerifier(),
        _VerifierStub(spec_set, policy, run_id="not-a-run"),
    )
    conflicting: tuple[object, ...] = (
        _VerifierStub(spec_set, policy, run_id=OTHER_RUN_ID),
        _VerifierStub(_spec_set(quantity_quantum="0.5"), policy),
        _VerifierStub(
            spec_set,
            policy,
            execution_policy=ExecutionPolicyRef(
                ExecutionPolicyId("foreign.execution.v1"),
                Sha256Digest("9" * 64),
            ),
        ),
        _VerifierStub(spec_set, wrong_policy),
    )

    for verifier in malformed:
        with pytest.raises(ExecutionAuthorityError) as error:
            create_phase1_order_authority(
                run_id=RUN_ID,
                spec_set=spec_set,
                execution_policy=EXECUTION_POLICY,
                risk_policy=policy,
                risk_result_verifier=cast(Any, verifier),
            )
        _assert_error(error, OutcomeCode.INVALID_TYPE)

    for verifier in conflicting:
        with pytest.raises(ExecutionAuthorityError) as error:
            create_phase1_order_authority(
                run_id=RUN_ID,
                spec_set=spec_set,
                execution_policy=EXECUTION_POLICY,
                risk_policy=policy,
                risk_result_verifier=cast(Any, verifier),
            )
        _assert_error(error, OutcomeCode.CONFLICTING_ID)


@pytest.mark.parametrize(
    ("binding_error", "expected_code"),
    [
        (AttributeError("missing binding"), OutcomeCode.INVALID_TYPE),
        (TypeError("invalid binding"), OutcomeCode.INVALID_TYPE),
        (RuntimeError("unexpected binding failure"), None),
    ],
)
def test_factory_binding_access_error_contract(
    binding_error: Exception,
    expected_code: OutcomeCode | None,
) -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)

    class FailingVerifier:
        @property
        def run_id(self) -> RunId:
            raise binding_error

    if expected_code is None:
        with pytest.raises(RuntimeError, match="unexpected binding failure"):
            _order_authority(spec_set, policy, FailingVerifier())
    else:
        with pytest.raises(ExecutionAuthorityError) as error:
            _order_authority(spec_set, policy, FailingVerifier())
        _assert_error(error, expected_code)


def test_allow_creates_one_canonical_order_and_exact_replay_is_identity_stable() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    risk = _risk_authority(spec_set, policy)
    intent = _intent(spec_set)
    result = risk.evaluate(intent, _snapshot(spec_set))
    authority = _order_authority(spec_set, policy, risk)

    order = authority.create_order(intent, result)
    state = authority._state

    assert order.order_id == _id(EconomicOwnerKind.EXECUTION_ORDER, 1)
    assert order.intent_id == intent.intent_id
    assert result.decision.approval is not None
    assert order.approval_id == result.decision.approval.approval_id
    assert order.quantity == intent.quantity
    assert authority.next_order_sequence == 2
    assert authority.orders == (order,)
    assert canonical_order_bytes(order)
    assert order_digest(order)
    assert canonical_execution_request_bytes(order)
    assert execution_request_digest(order)
    assert order.client_submission_key == order_client_submission_key(order)

    replayed = authority.create_order(intent, result)
    assert replayed is order
    assert authority._state is state
    assert authority.orders is state.orders


def test_resize_uses_only_the_approved_quantity() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set, order_limit="1")
    intent = _intent(spec_set, quantity="2")
    risk = _risk_authority(spec_set, policy)
    result = risk.evaluate(intent, _snapshot(spec_set))
    authority = _order_authority(spec_set, policy, risk)

    order = authority.create_order(intent, result)

    assert order.quantity == CanonicalDecimal("1")
    assert order.original_intent_sha256 != order.effective_intent_sha256
    assert authority.orders == (order,)


def test_risk_registry_membership_uses_canonical_bytes_not_object_identity() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    risk = _risk_authority(spec_set, policy)
    intent = _intent(spec_set)
    result = risk.evaluate(intent, _snapshot(spec_set))
    intent_bytes = bytes(bytearray(canonical_order_intent_bytes(intent)))
    decision_bytes = bytes(bytearray(canonical_risk_decision_bytes(result.decision)))
    evidence_bytes = bytes(bytearray(canonical_risk_evaluation_evidence_bytes(result.evidence)))

    assert risk.has_issued_result(
        intent_id=intent.intent_id,
        canonical_intent_bytes=intent_bytes,
        canonical_decision_bytes=decision_bytes,
        canonical_evidence_bytes=evidence_bytes,
    )
    assert not risk.has_issued_result(
        intent_id=intent.intent_id,
        canonical_intent_bytes=intent_bytes,
        canonical_decision_bytes=decision_bytes,
        canonical_evidence_bytes=evidence_bytes + b" ",
    )


@pytest.mark.parametrize("changed_dimension", ["intent", "decision", "evidence"])
def test_risk_registry_rejects_each_independent_canonical_byte_mismatch(
    changed_dimension: str,
) -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    risk = _risk_authority(spec_set, policy)
    intent = _intent(spec_set)
    result = risk.evaluate(intent, _snapshot(spec_set))
    assert result.decision.approval is not None
    intent_bytes = canonical_order_intent_bytes(intent)
    decision_bytes = canonical_risk_decision_bytes(result.decision)
    evidence_bytes = canonical_risk_evaluation_evidence_bytes(result.evidence)

    alternate_intent = _intent(spec_set, quantity="3")
    alternate_decision = allow_order_intent(
        decision_id=result.decision.decision_id,
        approval_id=result.decision.approval.approval_id,
        intent=intent,
        spec_set=spec_set,
        risk_state_version=1,
    )
    alternate_evidence = _replace_slots(
        result.evidence,
        portfolio_snapshot_sha256=Sha256Digest("9" * 64),
    )
    candidates = {
        "intent": canonical_order_intent_bytes(alternate_intent),
        "decision": canonical_risk_decision_bytes(alternate_decision),
        "evidence": canonical_risk_evaluation_evidence_bytes(alternate_evidence),
    }
    supplied = {
        "intent": intent_bytes,
        "decision": decision_bytes,
        "evidence": evidence_bytes,
    }
    supplied[changed_dimension] = candidates[changed_dimension]

    assert (
        supplied[changed_dimension]
        != {
            "intent": intent_bytes,
            "decision": decision_bytes,
            "evidence": evidence_bytes,
        }[changed_dimension]
    )
    assert not risk.has_issued_result(
        intent_id=intent.intent_id,
        canonical_intent_bytes=supplied["intent"],
        canonical_decision_bytes=supplied["decision"],
        canonical_evidence_bytes=supplied["evidence"],
    )


def test_valid_alternate_snapshot_digest_is_rejected_only_by_exact_membership() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    risk = _risk_authority(spec_set, policy)
    intent = _intent(spec_set)
    result = risk.evaluate(intent, _snapshot(spec_set))
    substituted_digest = Sha256Digest("9" * 64)
    assert substituted_digest != result.evidence.portfolio_snapshot_sha256
    forged_evidence = _replace_slots(
        result.evidence,
        portfolio_snapshot_sha256=substituted_digest,
    )
    forged = _result(result.decision, forged_evidence)
    authority = _order_authority(spec_set, policy, risk)
    original_state = authority._state

    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(intent, forged)

    _assert_error(error, OutcomeCode.CONFLICTING_ID)
    assert not risk.has_issued_result(
        intent_id=intent.intent_id,
        canonical_intent_bytes=canonical_order_intent_bytes(intent),
        canonical_decision_bytes=canonical_risk_decision_bytes(result.decision),
        canonical_evidence_bytes=canonical_risk_evaluation_evidence_bytes(forged_evidence),
    )
    _assert_execution_state_unchanged(authority, original_state)


def test_coherent_alternate_snapshot_and_risk_state_result_is_not_issued() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    risk = _risk_authority(spec_set, policy)
    recorded_intent = _intent(spec_set)
    recorded = risk.evaluate(recorded_intent, _snapshot(spec_set))
    assert recorded.decision.approval is not None
    alternate_snapshot = _snapshot(spec_set, position="1")
    alternate_intent = _intent(
        spec_set,
        portfolio_snapshot_version=alternate_snapshot.snapshot_version,
    )
    alternate_decision = allow_order_intent(
        decision_id=recorded.decision.decision_id,
        approval_id=recorded.decision.approval.approval_id,
        intent=alternate_intent,
        spec_set=spec_set,
        risk_state_version=1,
    )
    assert alternate_decision.approval is not None
    alternate_evidence = _replace_slots(
        recorded.evidence,
        intent_sha256=order_intent_digest(alternate_intent),
        portfolio_snapshot_version=alternate_snapshot.snapshot_version,
        portfolio_snapshot_sha256=portfolio_snapshot_digest(alternate_snapshot),
        risk_state_version=1,
        decision_sha256=risk_decision_digest(alternate_decision),
        approval_sha256=execution_approval_digest(alternate_decision.approval),
    )
    alternate_result = _result(alternate_decision, alternate_evidence)
    authority = _order_authority(spec_set, policy, risk)
    original_state = authority._state

    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(alternate_intent, alternate_result)

    _assert_error(error, OutcomeCode.CONFLICTING_ID)
    assert not risk.has_issued_result(
        intent_id=alternate_intent.intent_id,
        canonical_intent_bytes=canonical_order_intent_bytes(alternate_intent),
        canonical_decision_bytes=canonical_risk_decision_bytes(alternate_decision),
        canonical_evidence_bytes=canonical_risk_evaluation_evidence_bytes(alternate_evidence),
    )
    _assert_execution_state_unchanged(authority, original_state)


def test_bound_registry_rejects_an_unissued_but_coherent_result() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    bound_risk = _risk_authority(spec_set, policy)
    other_risk = _risk_authority(spec_set, policy)
    intent = _intent(spec_set)
    result = other_risk.evaluate(intent, _snapshot(spec_set))
    authority = _order_authority(spec_set, policy, bound_risk)
    original_state = authority._state

    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(intent, result)

    _assert_error(error, OutcomeCode.CONFLICTING_ID)
    assert authority._state is original_state


def test_equivalent_result_object_is_accepted_when_bound_registry_has_exact_bytes() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    bound_risk = _risk_authority(spec_set, policy)
    other_risk = _risk_authority(spec_set, policy)
    intent = _intent(spec_set)
    bound_result = bound_risk.evaluate(intent, _snapshot(spec_set))
    equivalent_result = other_risk.evaluate(intent, _snapshot(spec_set))
    assert equivalent_result is not bound_result
    assert canonical_risk_decision_bytes(equivalent_result.decision) == (
        canonical_risk_decision_bytes(bound_result.decision)
    )
    authority = _order_authority(spec_set, policy, bound_risk)

    order = authority.create_order(intent, equivalent_result)

    assert order.intent_id == intent.intent_id


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [(False, OutcomeCode.CONFLICTING_ID), (1, OutcomeCode.INVALID_TYPE)],
)
def test_verifier_false_or_non_boolean_is_fail_closed(
    response: object,
    expected_code: OutcomeCode,
) -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    risk = _risk_authority(spec_set, policy)
    intent = _intent(spec_set)
    result = risk.evaluate(intent, _snapshot(spec_set))
    verifier = _VerifierStub(spec_set, policy, response=response)
    authority = _order_authority(spec_set, policy, verifier)
    original_state = authority._state

    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(intent, result)

    _assert_error(error, expected_code)
    assert verifier.calls == 1
    assert authority._state is original_state


@pytest.mark.parametrize(
    "verifier_error",
    [RuntimeError("provenance failed"), TypeError("provenance type failed")],
)
def test_unexpected_verifier_exception_propagates_without_publication(
    verifier_error: Exception,
) -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    risk = _risk_authority(spec_set, policy)
    intent = _intent(spec_set)
    result = risk.evaluate(intent, _snapshot(spec_set))
    verifier = _VerifierStub(spec_set, policy, error=verifier_error)
    authority = _order_authority(spec_set, policy, verifier)
    original_state = authority._state

    with pytest.raises(type(verifier_error), match=str(verifier_error)) as error:
        authority.create_order(intent, result)

    assert error.value is verifier_error
    assert authority._state is original_state


def test_exact_order_replay_precedes_a_later_verifier_failure() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    risk = _risk_authority(spec_set, policy)
    intent = _intent(spec_set)
    result = risk.evaluate(intent, _snapshot(spec_set))
    verifier = _VerifierStub(spec_set, policy)
    authority = _order_authority(spec_set, policy, verifier)
    order = authority.create_order(intent, result)
    state = authority._state
    verifier.error = RuntimeError("must not be consulted on replay")

    replay = authority.create_order(intent, result)

    assert replay is order
    assert verifier.calls == 1
    assert authority._state is state


def test_historical_issuance_remains_valid_after_risk_halt() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    risk = _risk_authority(spec_set, policy)
    intent = _intent(spec_set)
    result = risk.evaluate(intent, _snapshot(spec_set))
    risk.engage_halt(RiskHaltReason.KILL_SWITCH, TIME, 99)
    authority = _order_authority(spec_set, policy, risk)

    order = authority.create_order(intent, result)

    assert order.intent_id == intent.intent_id
    assert risk.risk_state.halted


def test_provenance_rejection_precedes_order_sequence_exhaustion() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    risk = _risk_authority(spec_set, policy)
    intent = _intent(spec_set)
    result = risk.evaluate(intent, _snapshot(spec_set))
    verifier = _VerifierStub(spec_set, policy, response=False)
    authority = _order_authority(spec_set, policy, verifier)
    authority._state = replace(authority._state, order_next=None)
    original_state = authority._state

    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(intent, result)

    _assert_error(error, OutcomeCode.CONFLICTING_ID)
    assert authority._state is original_state


@pytest.mark.parametrize(
    ("order_limit", "position", "expected_reason"),
    [
        ("5", "9", RiskReasonCode.RESIZED_POSITION_LIMIT),
        ("1", "9", RiskReasonCode.RESIZED_ORDER_AND_POSITION_LIMITS),
    ],
)
def test_registered_position_resize_truth_table_is_accepted(
    order_limit: str,
    position: str,
    expected_reason: RiskReasonCode,
) -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set, order_limit=order_limit)
    risk = _risk_authority(spec_set, policy)
    intent = _intent(spec_set, quantity="2", portfolio_snapshot_version=1)
    result = risk.evaluate(intent, _snapshot(spec_set, position=position))
    authority = _order_authority(spec_set, policy, risk)

    order = authority.create_order(intent, result)

    assert result.evidence.reason_code is expected_reason
    assert order.quantity == CanonicalDecimal("1")


def test_risk_reducing_crossing_order_may_exceed_absolute_position_limit() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set, order_limit="30", position_limit="10")
    risk = _risk_authority(spec_set, policy)
    intent = _intent(spec_set, quantity="20", portfolio_snapshot_version=1)
    result = risk.evaluate(intent, _snapshot(spec_set, position="-15"))
    authority = _order_authority(spec_set, policy, risk)

    order = authority.create_order(intent, result)

    assert result.evidence.reason_code is RiskReasonCode.WITHIN_LIMITS
    assert order.quantity == CanonicalDecimal("20")


def test_static_policy_rejects_coherent_oversized_allow_before_provenance() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set, order_limit="5")
    risk = _risk_authority(spec_set, policy)
    intent = _intent(spec_set, quantity="6")
    resized = risk.evaluate(intent, _snapshot(spec_set))
    forged = _coherent_allow_result(intent=intent, spec_set=spec_set, template=resized)
    verifier = _VerifierStub(spec_set, policy)
    authority = _order_authority(spec_set, policy, verifier)
    original_state = authority._state

    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(intent, forged)

    _assert_error(error, OutcomeCode.CONFLICTING_ID)
    assert verifier.calls == 0
    assert authority._state is original_state


def test_membership_rejects_static_valid_allow_when_position_required_resize() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set, order_limit="5")
    risk = _risk_authority(spec_set, policy)
    intent = _intent(spec_set, quantity="2", portfolio_snapshot_version=1)
    resized = risk.evaluate(intent, _snapshot(spec_set, position="9"))
    forged = _coherent_allow_result(intent=intent, spec_set=spec_set, template=resized)
    authority = _order_authority(spec_set, policy, risk)
    original_state = authority._state

    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(intent, forged)

    _assert_error(error, OutcomeCode.CONFLICTING_ID)
    assert authority._state is original_state


@pytest.mark.parametrize(
    "forged_reason",
    [
        RiskReasonCode.RESIZED_POSITION_LIMIT,
        RiskReasonCode.RESIZED_ORDER_AND_POSITION_LIMITS,
    ],
)
def test_order_limit_resize_rejects_forged_position_reason(
    forged_reason: RiskReasonCode,
) -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set, order_limit="1")
    risk = _risk_authority(spec_set, policy)
    intent = _intent(spec_set, quantity="2")
    result = risk.evaluate(intent, _snapshot(spec_set))
    forged = _result(
        result.decision,
        _replace_slots(result.evidence, reason_code=forged_reason),
    )
    authority = _order_authority(spec_set, policy, risk)
    original_state = authority._state

    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(intent, forged)

    _assert_error(error, OutcomeCode.CONFLICTING_ID)
    assert authority._state is original_state


@pytest.mark.parametrize(
    "forged_reason",
    [
        RiskReasonCode.RESIZED_ORDER_LIMIT,
        RiskReasonCode.RESIZED_ORDER_AND_POSITION_LIMITS,
    ],
)
def test_position_limit_resize_rejects_forged_order_reason(
    forged_reason: RiskReasonCode,
) -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set, order_limit="5")
    risk = _risk_authority(spec_set, policy)
    intent = _intent(spec_set, quantity="2", portfolio_snapshot_version=1)
    result = risk.evaluate(intent, _snapshot(spec_set, position="9"))
    forged = _result(
        result.decision,
        _replace_slots(result.evidence, reason_code=forged_reason),
    )
    authority = _order_authority(spec_set, policy, risk)
    original_state = authority._state

    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(intent, forged)

    _assert_error(error, OutcomeCode.CONFLICTING_ID)
    assert authority._state is original_state


def test_unconfigured_instrument_cannot_carry_a_coherent_executable_result() -> None:
    spec_set = _spec_set()
    configured_policy = _policy(spec_set)
    intent = _intent(spec_set)
    configured_risk = _risk_authority(spec_set, configured_policy)
    valid = configured_risk.evaluate(intent, _snapshot(spec_set))
    empty_policy = create_phase1_risk_policy(
        policy_id=RiskPolicyId("phase1.risk.v1"),
        spec_set=spec_set,
        execution_policy=EXECUTION_POLICY,
        instrument_limits=(),
    )
    evidence = _replace_slots(
        valid.evidence,
        policy_sha256=phase1_risk_policy_digest(empty_policy),
    )
    forged = _result(valid.decision, evidence)
    verifier = _VerifierStub(spec_set, empty_policy)
    authority = _order_authority(spec_set, empty_policy, verifier)
    original_state = authority._state

    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(intent, forged)

    _assert_error(error, OutcomeCode.CONFLICTING_ID)
    assert verifier.calls == 0
    assert authority._state is original_state


def test_non_executable_risk_result_never_consumes_an_order_id() -> None:
    spec_set = _spec_set()
    policy = create_phase1_risk_policy(
        policy_id=RiskPolicyId("phase1.risk.v1"),
        spec_set=spec_set,
        execution_policy=EXECUTION_POLICY,
        instrument_limits=(),
    )
    intent = _intent(spec_set)
    risk = _risk_authority(spec_set, policy)
    result = risk.evaluate(intent, _snapshot(spec_set))
    authority = _order_authority(spec_set, policy, risk)
    original_state = authority._state

    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(intent, result)

    _assert_error(error, OutcomeCode.CONFLICTING_ID)
    assert authority._state is original_state


def test_occupied_identity_with_different_canonical_bytes_is_conflict() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    risk = _risk_authority(spec_set, policy)
    original_intent = _intent(spec_set, quantity="2")
    original_result = risk.evaluate(
        original_intent,
        _snapshot(spec_set),
    )
    authority = _order_authority(spec_set, policy, risk)
    authority.create_order(original_intent, original_result)
    original_state = authority._state

    conflicting_intent = _intent(spec_set, quantity="3")
    conflicting_result = _risk_authority(spec_set, policy).evaluate(
        conflicting_intent,
        _snapshot(spec_set),
    )
    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(conflicting_intent, conflicting_result)

    _assert_error(error, OutcomeCode.CONFLICTING_ID)
    assert authority._state is original_state


def test_order_sequence_maximum_transitions_to_none_and_replay_still_wins() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    risk = _risk_authority(spec_set, policy)
    first_intent = _intent(spec_set, sequence=1)
    first_result = risk.evaluate(first_intent, _snapshot(spec_set))
    authority = _order_authority(spec_set, policy, risk)
    authority._state = replace(authority._state, order_next=MAX_UINT64)

    order = authority.create_order(first_intent, first_result)
    assert order.order_id.owner_sequence == MAX_UINT64
    assert authority.next_order_sequence is None
    exhausted_state = authority._state
    assert authority.create_order(first_intent, first_result) is order
    assert authority._state is exhausted_state

    second_intent = _intent(spec_set, sequence=2)
    second_result = risk.evaluate(second_intent, _snapshot(spec_set))
    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(second_intent, second_result)
    _assert_error(error, OutcomeCode.OUT_OF_RANGE)
    assert authority._state is exhausted_state


@pytest.mark.parametrize(
    ("field_name", "replacement", "code"),
    [
        ("portfolio_snapshot_version", -1, OutcomeCode.OUT_OF_RANGE),
        ("portfolio_snapshot_version", True, OutcomeCode.INVALID_TYPE),
        ("risk_state_version", -1, OutcomeCode.OUT_OF_RANGE),
        ("reason_code", RiskReasonCode.RESIZED_ORDER_LIMIT, OutcomeCode.CONFLICTING_ID),
        ("policy_sha256", Sha256Digest("9" * 64), OutcomeCode.CONFLICTING_ID),
        ("decision_sha256", Sha256Digest("8" * 64), OutcomeCode.CONFLICTING_ID),
        ("approval_sha256", Sha256Digest("7" * 64), OutcomeCode.CONFLICTING_ID),
        ("decision_next_before", 2, OutcomeCode.CONFLICTING_ID),
        ("decision_next_before", -1, OutcomeCode.OUT_OF_RANGE),
        ("decision_next_before", MAX_UINT64 + 1, OutcomeCode.OUT_OF_RANGE),
        ("decision_next_before", 1.0, OutcomeCode.INVALID_TYPE),
        ("decision_next_after", 0, OutcomeCode.OUT_OF_RANGE),
        ("decision_next_after", None, OutcomeCode.OUT_OF_RANGE),
        ("approval_next_before", None, OutcomeCode.OUT_OF_RANGE),
        ("approval_next_before", -1, OutcomeCode.OUT_OF_RANGE),
        ("approval_next_after", 3, OutcomeCode.OUT_OF_RANGE),
        ("approval_next_after", True, OutcomeCode.INVALID_TYPE),
    ],
)
def test_forged_evidence_is_rejected_without_publication(
    field_name: str,
    replacement: object,
    code: OutcomeCode,
) -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    intent = _intent(spec_set)
    risk = _risk_authority(spec_set, policy)
    valid = risk.evaluate(intent, _snapshot(spec_set))
    evidence = _replace_slots(valid.evidence, **{field_name: replacement})
    forged = _result(valid.decision, evidence)
    authority = _order_authority(spec_set, policy, risk)
    original_state = authority._state

    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(intent, forged)

    _assert_error(error, code)
    assert authority._state is original_state


@pytest.mark.parametrize(
    ("decision_sequence", "approval_sequence"),
    [(0, 1), (1, 0)],
)
def test_low_level_sequence_zero_cannot_bypass_risk_allocation_authority(
    decision_sequence: int,
    approval_sequence: int,
) -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    intent = _intent(spec_set)
    valid = _risk_authority(spec_set, policy).evaluate(intent, _snapshot(spec_set))
    decision = allow_order_intent(
        decision_id=_id(EconomicOwnerKind.RISK_DECISION, decision_sequence),
        approval_id=_id(EconomicOwnerKind.RISK_APPROVAL, approval_sequence),
        intent=intent,
        spec_set=spec_set,
        risk_state_version=0,
    )
    assert decision.approval is not None
    evidence = _replace_slots(
        valid.evidence,
        decision_id=decision.decision_id,
        decision_sha256=risk_decision_digest(decision),
        approval_sha256=execution_approval_digest(decision.approval),
        decision_next_before=decision_sequence,
        decision_next_after=decision_sequence + 1,
        approval_next_before=approval_sequence,
        approval_next_after=approval_sequence + 1,
    )
    forged = _result(decision, evidence)
    authority = _order_authority(spec_set, policy)
    original_state = authority._state

    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(intent, forged)

    _assert_error(error, OutcomeCode.OUT_OF_RANGE)
    assert authority._state is original_state


def test_risk_allocation_maximum_to_none_is_accepted() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    intent = _intent(spec_set)
    risk = _risk_authority(spec_set, policy)
    risk._state = replace(
        risk._state,
        decision_next=MAX_UINT64,
        approval_next=MAX_UINT64,
    )
    result = risk.evaluate(intent, _snapshot(spec_set))
    authority = _order_authority(spec_set, policy, risk)

    order = authority.create_order(intent, result)

    assert order.decision_id.owner_sequence == MAX_UINT64
    assert order.approval_id.owner_sequence == MAX_UINT64


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity"])
def test_forged_non_finite_decimal_preserves_exact_outcome(
    text: str,
) -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    intent = _intent(spec_set)
    result = _risk_authority(spec_set, policy).evaluate(intent, _snapshot(spec_set))
    forged_intent = _replace_slots(intent, quantity=_forged_decimal(text))
    authority = _order_authority(spec_set, policy)
    original_state = authority._state

    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(forged_intent, result)

    _assert_error(error, OutcomeCode.NON_FINITE)
    assert authority._state is original_state


@pytest.mark.parametrize("target", ["decision", "approval"])
@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity"])
def test_forged_decision_and_approval_non_finite_decimal_preserves_exact_outcome(
    target: str,
    text: str,
) -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    intent = _intent(spec_set)
    valid = _risk_authority(spec_set, policy).evaluate(intent, _snapshot(spec_set))
    if target == "decision":
        decision = _replace_slots(
            valid.decision,
            approved_quantity=_forged_decimal(text),
        )
    else:
        assert valid.decision.approval is not None
        approval = _replace_slots(
            valid.decision.approval,
            approved_quantity=_forged_decimal(text),
        )
        decision = _replace_slots(valid.decision, approval=approval)
    authority = _order_authority(spec_set, policy)
    original_state = authority._state

    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(intent, _result(decision, valid.evidence))

    _assert_error(error, OutcomeCode.NON_FINITE)
    assert authority._state is original_state
    assert authority._state.approval_index is original_state.approval_index
    assert authority._state.intent_index is original_state.intent_index
    assert authority._state.decision_index is original_state.decision_index
    assert authority.orders is original_state.orders


def _forged_decimal(text: str) -> CanonicalDecimal:
    value = object.__new__(CanonicalDecimal)
    object.__setattr__(value, "text", text)
    object.__setattr__(value, "_coefficient", 1)
    object.__setattr__(value, "_scale", 0)
    return value


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("not-a-digest", OutcomeCode.OUT_OF_RANGE),
        (1, OutcomeCode.INVALID_TYPE),
    ],
)
def test_forged_opaque_snapshot_digest_is_reconstructed(
    value: object,
    code: OutcomeCode,
) -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    intent = _intent(spec_set)
    valid = _risk_authority(spec_set, policy).evaluate(intent, _snapshot(spec_set))
    digest = object.__new__(Sha256Digest)
    object.__setattr__(digest, "value", value)
    evidence = _replace_slots(
        valid.evidence,
        portfolio_snapshot_sha256=digest,
    )
    authority = _order_authority(spec_set, policy)
    original_state = authority._state

    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(intent, _result(valid.decision, evidence))

    _assert_error(error, code)
    assert authority._state is original_state


class _ValueCarrier:
    def __init__(self, value: str) -> None:
        self.value = value


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"run_id": OTHER_RUN_ID}, OutcomeCode.CONFLICTING_ID),
        (
            {"intent_id": _id(EconomicOwnerKind.RISK_DECISION, 1)},
            OutcomeCode.CONFLICTING_ID,
        ),
        (
            {"correlation_id": _id(EconomicOwnerKind.PORTFOLIO_TARGET, 1)},
            OutcomeCode.CONFLICTING_ID,
        ),
        (
            {"causation_id": _id(EconomicOwnerKind.PORTFOLIO_TARGET, 2)},
            OutcomeCode.CONFLICTING_ID,
        ),
        ({"quantity": CanonicalDecimal("0")}, OutcomeCode.OUT_OF_RANGE),
        ({"quantity": CanonicalDecimal("1.5")}, OutcomeCode.NOT_QUANTIZED),
        ({"order_kind": _ValueCarrier("limit")}, OutcomeCode.INVALID_TYPE),
        ({"time_in_force": _ValueCarrier("good_till_cancelled")}, OutcomeCode.INVALID_TYPE),
        ({"price_constraint": CanonicalDecimal("1")}, OutcomeCode.OUT_OF_RANGE),
        ({"causal_root_available_at": datetime(2026, 1, 2, 9, 31)}, OutcomeCode.OUT_OF_RANGE),
        ({"dispatch_sequence": -1}, OutcomeCode.OUT_OF_RANGE),
        ({"dispatch_sequence": True}, OutcomeCode.INVALID_TYPE),
        ({"target_lineage": None}, OutcomeCode.INVALID_TYPE),
        ({"execution_policy": None}, OutcomeCode.INVALID_TYPE),
    ],
)
def test_forged_intent_fields_are_reproved_before_order_allocation(
    changes: dict[str, object],
    code: OutcomeCode,
) -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    intent = _intent(spec_set)
    result = _risk_authority(spec_set, policy).evaluate(intent, _snapshot(spec_set))
    forged = _replace_slots(intent, **changes)
    authority = _order_authority(spec_set, policy)
    original_state = authority._state

    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(forged, result)

    _assert_error(error, code)
    assert authority._state is original_state


@pytest.mark.parametrize(
    ("target", "changes", "code"),
    [
        ("decision", {"run_id": OTHER_RUN_ID}, OutcomeCode.CONFLICTING_ID),
        (
            "decision",
            {"causation_id": _id(EconomicOwnerKind.PORTFOLIO_INTENT, 2)},
            OutcomeCode.CONFLICTING_ID,
        ),
        (
            "decision",
            {"intent_sha256": Sha256Digest("9" * 64)},
            OutcomeCode.CONFLICTING_ID,
        ),
        (
            "decision",
            {"portfolio_snapshot_version": 1},
            OutcomeCode.CONFLICTING_ID,
        ),
        (
            "decision",
            {"causal_root_available_at": datetime(2026, 1, 2, 9, 31)},
            OutcomeCode.OUT_OF_RANGE,
        ),
        (
            "approval",
            {"run_id": OTHER_RUN_ID},
            OutcomeCode.CONFLICTING_ID,
        ),
        (
            "approval",
            {"decision_id": _id(EconomicOwnerKind.RISK_DECISION, 2)},
            OutcomeCode.CONFLICTING_ID,
        ),
        (
            "approval",
            {"approved_quantity": CanonicalDecimal("1")},
            OutcomeCode.CONFLICTING_ID,
        ),
        (
            "approval",
            {"risk_state_version": 1},
            OutcomeCode.CONFLICTING_ID,
        ),
        (
            "approval",
            {"causal_root_available_at": datetime(2026, 1, 2, 9, 31)},
            OutcomeCode.OUT_OF_RANGE,
        ),
        (
            "approval",
            {"dispatch_sequence": -1},
            OutcomeCode.OUT_OF_RANGE,
        ),
    ],
)
def test_forged_decision_and_approval_fields_are_reproved(
    target: str,
    changes: dict[str, object],
    code: OutcomeCode,
) -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    intent = _intent(spec_set)
    valid = _risk_authority(spec_set, policy).evaluate(intent, _snapshot(spec_set))
    if target == "decision":
        decision = _replace_slots(valid.decision, **changes)
    else:
        assert valid.decision.approval is not None
        approval = _replace_slots(valid.decision.approval, **changes)
        decision = _replace_slots(valid.decision, approval=approval)
    forged = _result(decision, valid.evidence)
    authority = _order_authority(spec_set, policy)
    original_state = authority._state

    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(intent, forged)

    _assert_error(error, code)
    assert authority._state is original_state


def test_incomplete_exact_carriers_return_invalid_type_without_publication() -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    intent = _intent(spec_set)
    incomplete = object.__new__(RiskEvaluationResult)
    authority = _order_authority(spec_set, policy)
    original_state = authority._state

    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(intent, incomplete)

    _assert_error(error, OutcomeCode.INVALID_TYPE)
    assert authority._state is original_state


@pytest.mark.parametrize(
    "case",
    [
        "approval_only",
        "intent_only",
        "decision_only",
        "approval_intent",
        "approval_decision",
        "intent_decision",
        "different_records",
    ],
)
def test_every_inconsistent_triple_index_lookup_is_a_conflict(case: str) -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    risk = _risk_authority(spec_set, policy)
    first_intent = _intent(spec_set, sequence=1)
    first_result = risk.evaluate(first_intent, _snapshot(spec_set))
    second_intent = _intent(spec_set, sequence=2)
    second_result = risk.evaluate(second_intent, _snapshot(spec_set))
    authority = _order_authority(spec_set, policy, risk)
    authority.create_order(first_intent, first_result)
    authority.create_order(second_intent, second_result)
    assert first_result.decision.approval is not None
    assert second_result.decision.approval is not None
    first_approval_id = first_result.decision.approval.approval_id
    second_approval_id = second_result.decision.approval.approval_id
    first_record = authority._state.approval_index[first_approval_id]
    second_record = authority._state.approval_index[second_approval_id]
    approval_index: dict[EconomicId, Any] = {}
    intent_index: dict[EconomicId, Any] = {}
    decision_index: dict[EconomicId, Any] = {}
    if case in {"approval_only", "approval_intent", "approval_decision"}:
        approval_index[first_approval_id] = first_record
    if case in {"intent_only", "approval_intent", "intent_decision"}:
        intent_index[first_intent.intent_id] = first_record
    if case in {"decision_only", "approval_decision", "intent_decision"}:
        decision_index[first_result.decision.decision_id] = first_record
    if case == "different_records":
        approval_index[first_approval_id] = first_record
        intent_index[first_intent.intent_id] = second_record
        decision_index[first_result.decision.decision_id] = first_record
    authority._state = replace(
        authority._state,
        approval_index=MappingProxyType(approval_index),
        intent_index=MappingProxyType(intent_index),
        decision_index=MappingProxyType(decision_index),
    )
    original_state = authority._state

    with pytest.raises(ExecutionAuthorityError) as error:
        authority.create_order(first_intent, first_result)

    _assert_error(error, OutcomeCode.CONFLICTING_ID)
    assert authority._state is original_state


@pytest.mark.parametrize(
    "target",
    [
        "_make_record",
        "_copy_approval_index",
        "_copy_intent_index",
        "_copy_decision_index",
        "_insert_approval_record",
        "_insert_intent_record",
        "_insert_decision_record",
        "_freeze_approval_index",
        "_freeze_intent_index",
        "_freeze_decision_index",
        "_preflight_public_record",
        "_preflight_candidate_state",
    ],
)
def test_every_candidate_publication_stage_is_atomic(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    intent = _intent(spec_set)
    risk = _risk_authority(spec_set, policy)
    result = risk.evaluate(intent, _snapshot(spec_set))
    authority = _order_authority(spec_set, policy, risk)
    original_state = authority._state

    with monkeypatch.context() as patch:
        patch.setattr(authority_module, target, _raise_injected)
        with pytest.raises(RuntimeError, match="injected pre-publication"):
            authority.create_order(intent, result)

    assert authority._state is original_state
    assert authority._state.approval_index is original_state.approval_index
    assert authority._state.intent_index is original_state.intent_index
    assert authority._state.decision_index is original_state.decision_index
    assert authority.orders is original_state.orders


@pytest.mark.parametrize(
    "target",
    [
        "create_order",
        "canonical_order_bytes",
        "order_digest",
        "canonical_execution_request_bytes",
        "execution_request_digest",
        "order_client_submission_key",
    ],
)
def test_each_real_order_evidence_construction_boundary_is_atomic(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    intent = _intent(spec_set)
    risk = _risk_authority(spec_set, policy)
    result = risk.evaluate(intent, _snapshot(spec_set))
    authority = _order_authority(spec_set, policy, risk)
    original_state = authority._state

    with monkeypatch.context() as patch:
        patch.setattr(authority_module, target, _raise_injected)
        with pytest.raises(RuntimeError, match="injected pre-publication"):
            authority.create_order(intent, result)

    assert authority._state is original_state
    assert authority._state.approval_index is original_state.approval_index
    assert authority._state.intent_index is original_state.intent_index
    assert authority._state.decision_index is original_state.decision_index
    assert authority.orders is original_state.orders


@pytest.mark.parametrize(
    "target",
    [
        "canonical_order_bytes",
        "order_digest",
        "canonical_execution_request_bytes",
        "execution_request_digest",
        "order_client_submission_key",
    ],
)
def test_each_real_order_evidence_final_repreflight_boundary_is_atomic(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    intent = _intent(spec_set)
    risk = _risk_authority(spec_set, policy)
    result = risk.evaluate(intent, _snapshot(spec_set))
    authority = _order_authority(spec_set, policy, risk)
    original_state = authority._state
    original = cast(Callable[..., Any], getattr(authority_module, target))
    calls = 0

    def fail_second_call(*args: object, **kwargs: object) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            _raise_injected()
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(authority_module, target, fail_second_call)
        with pytest.raises(RuntimeError, match="injected pre-publication"):
            authority.create_order(intent, result)

    assert calls == 2
    assert authority._state is original_state
    assert authority._state.approval_index is original_state.approval_index
    assert authority._state.intent_index is original_state.intent_index
    assert authority._state.decision_index is original_state.decision_index
    assert authority.orders is original_state.orders


class _FailsDuringCopy(Mapping[EconomicId, Any]):
    def __getitem__(self, key: EconomicId) -> Any:
        raise KeyError(key)

    def __iter__(self) -> Iterator[EconomicId]:
        raise RuntimeError("injected real mapping copy failure")

    def __len__(self) -> int:
        return 0


class _FailsDuringFreeze(MutableMapping[EconomicId, Any]):
    def __init__(self) -> None:
        self._values: dict[EconomicId, Any] = {}

    def __getitem__(self, key: EconomicId) -> Any:
        return self._values[key]

    def __setitem__(self, key: EconomicId, value: Any) -> None:
        self._values[key] = value

    def __delitem__(self, key: EconomicId) -> None:
        del self._values[key]

    def __iter__(self) -> Iterator[EconomicId]:
        raise RuntimeError("injected real defensive-freeze copy failure")

    def __len__(self) -> int:
        return len(self._values)


@pytest.mark.parametrize(
    ("state_field", "copy_target"),
    [
        ("approval_index", "_copy_approval_index"),
        ("intent_index", "_copy_intent_index"),
        ("decision_index", "_copy_decision_index"),
    ],
)
def test_actual_dict_copy_operations_are_fail_closed(
    state_field: str,
    copy_target: str,
) -> None:
    failing = _FailsDuringCopy()
    copy = cast(
        Callable[[Mapping[EconomicId, Any]], dict[EconomicId, Any]],
        getattr(authority_module, copy_target),
    )
    with pytest.raises(RuntimeError, match="injected real mapping copy failure"):
        copy(failing)

    spec_set = _spec_set()
    policy = _policy(spec_set)
    intent = _intent(spec_set)
    risk = _risk_authority(spec_set, policy)
    result = risk.evaluate(intent, _snapshot(spec_set))
    authority = _order_authority(spec_set, policy, risk)
    if state_field == "approval_index":
        authority._state = replace(authority._state, approval_index=failing)
    elif state_field == "intent_index":
        authority._state = replace(authority._state, intent_index=failing)
    else:
        authority._state = replace(authority._state, decision_index=failing)
    original_state = authority._state

    with pytest.raises(RuntimeError, match="injected real mapping copy failure"):
        authority.create_order(intent, result)

    assert authority._state is original_state


@pytest.mark.parametrize(
    "freeze_target",
    [
        "_freeze_approval_index",
        "_freeze_intent_index",
        "_freeze_decision_index",
    ],
)
def test_actual_defensive_freeze_operations_copy_the_mapping(
    freeze_target: str,
) -> None:
    freeze = cast(
        Callable[[Mapping[EconomicId, Any]], Mapping[EconomicId, Any]],
        getattr(authority_module, freeze_target),
    )
    with pytest.raises(RuntimeError, match="injected real mapping copy failure"):
        freeze(_FailsDuringCopy())


@pytest.mark.parametrize(
    "copy_target",
    [
        "_copy_approval_index",
        "_copy_intent_index",
        "_copy_decision_index",
    ],
)
def test_each_real_defensive_freeze_failure_is_atomic_inside_create_order(
    monkeypatch: pytest.MonkeyPatch,
    copy_target: str,
) -> None:
    spec_set = _spec_set()
    policy = _policy(spec_set)
    intent = _intent(spec_set)
    risk = _risk_authority(spec_set, policy)
    result = risk.evaluate(intent, _snapshot(spec_set))
    authority = _order_authority(spec_set, policy, risk)
    original_state = authority._state

    def return_two_stage_mapping(_: object) -> Any:
        return _FailsDuringFreeze()

    with monkeypatch.context() as patch:
        patch.setattr(authority_module, copy_target, return_two_stage_mapping)
        with pytest.raises(
            RuntimeError,
            match="injected real defensive-freeze copy failure",
        ):
            authority.create_order(intent, result)

    assert authority._state is original_state
    assert authority._state.approval_index is original_state.approval_index
    assert authority._state.intent_index is original_state.intent_index
    assert authority._state.decision_index is original_state.decision_index
    assert authority.orders is original_state.orders


def test_error_class_rejects_non_validation_outcome() -> None:
    with pytest.raises(TypeError, match="permitted OutcomeCode"):
        ExecutionAuthorityError(OutcomeCode.RISK_ALLOWED, "wrong family")
