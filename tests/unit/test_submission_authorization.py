from __future__ import annotations

import json
from copy import copy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from ea.core.audit import (
    AuditContractError,
    AuditRecordKind,
    AuditSubjectKind,
    audit_acknowledgement_id,
    audit_append_acknowledgement_digest,
    audit_subject_digest,
    canonical_run_prepared_audit_payload,
)
from ea.core.execution_identity import SourceNamespace
from ea.core.execution_messages import (
    Order,
    canonical_execution_request_bytes,
    canonical_order_bytes,
    execution_request_digest,
    order_digest,
)
from ea.core.historical_matching import (
    HistoricalPreEffectAuthorizationError,
    HistoricalSubmissionReceipt,
    _create_historical_submission_receipt,
    historical_market_root_digest,
)
from ea.core.lifecycle import (
    CoordinatorPhase,
    GlobalHaltSnapshot,
    InstrumentGateSnapshot,
    RuntimeLifecyclePort,
    SubmissionAuthorizationAttemptStatus,
)
from ea.core.market_data import Adjustment, Bar, MarketDataEnvelope, SourceId
from ea.core.market_data_codec import canonical_market_data_record_bytes
from ea.core.outcomes import OutcomeCode
from ea.core.risk import _create_risk_state_snapshot, phase1_risk_policy_digest
from ea.core.run import RunBinding, RunId, RunReference, Sha256Digest
from ea.core.runtime import runtime_root_order_key
from ea.experiments.audit import _OsAuditOps, create_posix_audit_journal
from ea.experiments.store import LocalResultStore
from ea.runtime.authorization import (
    _authorization_payload_from_receipt,
    create_dormant_historical_submission_authorization_authority,
)
from unit.test_execution_fact_authority import (
    EXECUTION_POLICY,
    TIME,
    _orders,
    _policy,
    _snapshot,
)
from unit.test_lifecycle_coordinator import _MemoryAudit
from unit.test_store import _root, _spec


class _RepeatableRecordSource:
    def __init__(self, records: tuple[Any, ...]) -> None:
        self._records = records
        self.record_count = len(records)
        self.binding = records[0].binding

    def __iter__(self) -> Any:
        return iter(self._records)


class _Port:
    def __init__(self, value: Any) -> None:
        self.value = value

    def current_snapshot(self) -> Any:
        return self.value

    def current_state(self) -> Any:
        return self.value

    def current_for(self, instrument: Any) -> Any:
        return self.value


class _ChangeAfterFirstGateRead(_Port):
    def __init__(self, value: Any, next_value: Any) -> None:
        super().__init__(value)
        self.next_value = next_value

    def current_for(self, instrument: Any) -> Any:
        del instrument
        value = self.value
        if self.next_value is not None:
            self.value = self.next_value
            self.next_value = None
        return value


class _CommitThenRaiseAudit(_MemoryAudit):
    raised = False

    def append(self, **values: Any) -> Any:
        acknowledgement = super().append(**values)
        if (
            values["record_kind"] is AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION
            and not self.raised
        ):
            self.raised = True
            raise AuditContractError(
                OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                "injected committed authorization return failure",
            )
        return acknowledgement


class _FailBeforeAuthorizationAudit(_MemoryAudit):
    def append(self, **values: Any) -> Any:
        if values["record_kind"] is AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION:
            raise AuditContractError(
                OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                "injected definite authorization append failure",
            )
        return super().append(**values)


class _UnresolvableAuthorizationAudit:
    def __init__(self, binding: RunBinding) -> None:
        self.binding = binding
        self._inner = _MemoryAudit(binding)

    def append(self, **values: Any) -> Any:
        if values["record_kind"] is AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION:
            raise AuditContractError(
                OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                "injected uncertain authorization append failure",
            )
        return self._inner.append(**values)

    def settle_append(self, **values: Any) -> Any:
        del values
        raise AuditContractError(
            OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
            "injected uncertain authorization settlement failure",
        )


class _TwoPostFsyncFailuresAuditOps(_OsAuditOps):
    remaining_failures = 0

    def fsync(self, descriptor: int) -> None:
        super().fsync(descriptor)
        if self.remaining_failures:
            self.remaining_failures -= 1
            raise OSError("injected post-fsync authorization failure")


class _Runtime:
    def __init__(self, run_id: Any, spec_set: Any, root: Any) -> None:
        self.run_id = run_id
        self.spec_set = spec_set
        self.active_lease = SimpleNamespace(root=root, dispatch_sequence=1)


class _NoSubmissions:
    def resolve_submission_receipt(self, **values: Any) -> None:
        del values
        return None


class _NoOrders:
    def resolve_issued_order_by_id(self, order_id: Any) -> None:
        del order_id
        return None


class _Submissions:
    def __init__(self, receipt: HistoricalSubmissionReceipt) -> None:
        self.receipt = receipt

    def resolve_submission_receipt(self, **values: Any) -> HistoricalSubmissionReceipt:
        del values
        return self.receipt


def _receipt(
    order: Any,
    root: MarketDataEnvelope,
    acknowledgement: Any,
    document: dict[str, Any],
) -> HistoricalSubmissionReceipt:
    return _create_historical_submission_receipt(
        causal_market_root=root,
        run_id=order.run_id,
        source_namespace=SourceNamespace("phase1.historical-matcher.v1"),
        submission_sequence=1,
        order_id=order.order_id,
        order_sha256=order_digest(order),
        execution_request_sha256=execution_request_digest(order),
        client_submission_key=order.client_submission_key,
        instrument=order.instrument,
        side=order.side,
        quantity_text=order.quantity.text,
        causal_market_sha256=historical_market_root_digest(root),
        causal_root_key=runtime_root_order_key(root),
        dispatch_sequence=order.dispatch_sequence,
        eligible_after_available_at=order.eligible_after_available_at,
        audit_acknowledgement_id=audit_acknowledgement_id(acknowledgement),
        audit_acknowledgement_sha256=audit_append_acknowledgement_digest(acknowledgement),
        global_halt_epoch=document["global_halt_epoch"],
        risk_halt_epoch=document["risk_halt_epoch"],
        instrument_gate_id=document["instrument_gate_id"],
        instrument_gate_version=document["instrument_gate_version"],
        authorization_state_version=document["authorization_state_version"],
        instrument_spec_set_id=order.instrument_spec_set_id,
        instrument_spec_set_sha256=order.instrument_spec_set_sha256,
        execution_policy=order.execution_policy,
    )


def _market() -> MarketDataEnvelope:
    spec_set, _authority, orders = _orders()
    instrument = orders[0].instrument
    return MarketDataEnvelope(
        payload=Bar(
            instrument=instrument,
            interval_start=TIME - timedelta(minutes=1),
            interval_end=TIME,
            adjustment=Adjustment.RAW,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=10.0,
        ),
        source=SourceId("primary.raw"),
        available_at=TIME,
        source_sequence=0,
        revision=0,
    )


def _attempt_system(audit_type: Any) -> tuple[Any, object, Any, Any, Order, MarketDataEnvelope]:
    spec_set, _order_authority, orders = _orders()
    order = orders[0]
    root = _market()
    binding = RunBinding(
        RunReference(order.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = audit_type(binding)
    audit.append(
        record_kind=AuditRecordKind.RUN_PREPARED,
        subject_kind=AuditSubjectKind.RUN_MANIFEST,
        subject_sha256=binding.manifest_sha256,
        canonical_payload=canonical_run_prepared_audit_payload(binding),
    )
    policy = _policy(spec_set)
    risk = _create_risk_state_snapshot(
        run_id=order.run_id,
        policy_id=policy.policy_id,
        policy_sha256=phase1_risk_policy_digest(policy),
        risk_state_version=0,
        halted=False,
        halt_reason=None,
        halt_causal_root_available_at=None,
        halt_dispatch_sequence=None,
        conflict_existing_intent_sha256=None,
        conflict_submitted_intent_sha256=None,
    )
    gate = _Port(
        InstrumentGateSnapshot(
            order.run_id,
            order.instrument,
            order.order_id,
            Sha256Digest("33" * 32),
            1,
            False,
        )
    )
    runtime = _Runtime(order.run_id, spec_set, root)
    authority, capability, activation_seal = (
        create_dormant_historical_submission_authorization_authority(
            binding=binding,
            audit=audit,
            runtime=cast(RuntimeLifecyclePort, runtime),
            spec_set=spec_set,
            execution_policy=EXECUTION_POLICY,
            portfolio=_Port(_snapshot(spec_set)),
            risk=_Port(risk),
            global_halt=_Port(GlobalHaltSnapshot(order.run_id, False, 0)),
            instrument_gate=gate,
        )
    )
    authority.activate(
        SimpleNamespace(
            binding=binding,
            state=SimpleNamespace(state_version=1, phase=CoordinatorPhase.RUNNING),
        ),
        seal=activation_seal,
    )
    return authority, capability, audit, gate, order, root


@pytest.mark.parametrize(
    ("audit_type", "expected_status"),
    [
        (_CommitThenRaiseAudit, SubmissionAuthorizationAttemptStatus.AUTHORIZED),
        (_FailBeforeAuthorizationAudit, SubmissionAuthorizationAttemptStatus.FAILED),
        (_UnresolvableAuthorizationAudit, SubmissionAuthorizationAttemptStatus.UNRESOLVED),
    ],
)
def test_prepare_attempt_closes_append_failure_state(
    audit_type: Any,
    expected_status: SubmissionAuthorizationAttemptStatus,
) -> None:
    authority, capability, audit, _gate, order, root = _attempt_system(audit_type)

    outcome = authority.prepare_attempt(
        order,
        causal_market_root=root,
        dispatch_sequence=1,
        capability=capability,
    )

    assert outcome.status is expected_status
    if expected_status is SubmissionAuthorizationAttemptStatus.AUTHORIZED:
        assert outcome.acknowledgement_sha256 is not None
        assert (
            authority.resolve_attempt(
                order_id=order.order_id,
                execution_request_sha256=execution_request_digest(order),
            )
            is not None
        )
        assert [record.record_kind for record in audit.records].count(
            AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION
        ) == 1
    else:
        assert outcome.acknowledgement_sha256 is None


def test_prepare_attempt_settles_the_exact_captured_freshness_payload() -> None:
    authority, capability, audit, gate, order, root = _attempt_system(_CommitThenRaiseAudit)
    initial = gate.value
    changing_gate = _ChangeAfterFirstGateRead(
        initial,
        InstrumentGateSnapshot(
            initial.run_id,
            initial.instrument,
            initial.held_for_order_id,
            initial.instrument_gate_id,
            initial.instrument_gate_version + 1,
            initial.halted,
        ),
    )
    authority._instrument_gate = changing_gate

    outcome = authority.prepare_attempt(
        order,
        causal_market_root=root,
        dispatch_sequence=1,
        capability=capability,
    )

    assert outcome.status is SubmissionAuthorizationAttemptStatus.BURNED
    authorization_records = [
        record
        for record in audit.records
        if record.record_kind is AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION
    ]
    assert len(authorization_records) == 1
    assert (
        json.loads(authorization_records[0].canonical_payload)["instrument_gate_version"]
        == initial.instrument_gate_version
    )


def test_posix_uncertain_attempt_preserves_original_key_before_freshness_burn(
    tmp_path: Path,
) -> None:
    spec_set, _order_authority, orders = _orders()
    order = orders[0]
    root = _market()
    prepared = LocalResultStore(_root(tmp_path)).prepare(
        _spec(),
        lambda: UUID(order.run_id.value),
    )
    binding = prepared.audit.binding
    ops = _TwoPostFsyncFailuresAuditOps()
    audit = create_posix_audit_journal(prepared.audit, _ops=ops)
    policy = _policy(spec_set)
    risk = _create_risk_state_snapshot(
        run_id=order.run_id,
        policy_id=policy.policy_id,
        policy_sha256=phase1_risk_policy_digest(policy),
        risk_state_version=0,
        halted=False,
        halt_reason=None,
        halt_causal_root_available_at=None,
        halt_dispatch_sequence=None,
        conflict_existing_intent_sha256=None,
        conflict_submitted_intent_sha256=None,
    )
    gate = _Port(
        InstrumentGateSnapshot(
            order.run_id,
            order.instrument,
            order.order_id,
            Sha256Digest("33" * 32),
            1,
            False,
        )
    )
    runtime = _Runtime(order.run_id, spec_set, root)
    authority, capability, activation_seal = (
        create_dormant_historical_submission_authorization_authority(
            binding=binding,
            audit=audit,
            runtime=cast(RuntimeLifecyclePort, runtime),
            spec_set=spec_set,
            execution_policy=EXECUTION_POLICY,
            portfolio=_Port(_snapshot(spec_set)),
            risk=_Port(risk),
            global_halt=_Port(GlobalHaltSnapshot(order.run_id, False, 0)),
            instrument_gate=gate,
        )
    )
    authority.activate(
        SimpleNamespace(
            binding=binding,
            state=SimpleNamespace(state_version=1, phase=CoordinatorPhase.RUNNING),
        ),
        seal=activation_seal,
    )
    ops.remaining_failures = 2

    unresolved = authority.prepare_attempt(
        order,
        causal_market_root=root,
        dispatch_sequence=1,
        capability=capability,
    )
    assert unresolved.status is SubmissionAuthorizationAttemptStatus.UNRESOLVED
    assert unresolved.logical_key is not None
    gate.value = InstrumentGateSnapshot(
        order.run_id,
        order.instrument,
        order.order_id,
        Sha256Digest("33" * 32),
        1,
        True,
    )

    burned = authority.prepare_attempt(
        order,
        causal_market_root=root,
        dispatch_sequence=1,
        capability=capability,
    )

    assert burned.status is SubmissionAuthorizationAttemptStatus.BURNED
    assert burned.logical_key == unresolved.logical_key
    assert burned.acknowledgement_sha256 is not None
    assert [record.record_kind for record in audit.records].count(
        AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION
    ) == 1


def test_prepare_attempt_returns_denied_without_append() -> None:
    authority, capability, audit, gate, order, root = _attempt_system(_MemoryAudit)
    gate.value = InstrumentGateSnapshot(
        order.run_id,
        order.instrument,
        order.order_id,
        Sha256Digest("33" * 32),
        1,
        True,
    )

    outcome = authority.prepare_attempt(
        order,
        causal_market_root=root,
        dispatch_sequence=1,
        capability=capability,
    )

    assert outcome.status is SubmissionAuthorizationAttemptStatus.DENIED
    assert outcome.error_code is OutcomeCode.SUBMISSION_BLOCKED_BY_HALT
    assert [record.record_kind for record in audit.records].count(
        AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION
    ) == 0


def test_dormant_authority_activates_once_and_issues_one_durable_proof() -> None:
    spec_set, _order_authority, orders = _orders()
    order = orders[0]
    root = _market()
    binding = RunBinding(
        RunReference(order.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _MemoryAudit(binding)
    audit.append(
        record_kind=AuditRecordKind.RUN_PREPARED,
        subject_kind=AuditSubjectKind.RUN_MANIFEST,
        subject_sha256=binding.manifest_sha256,
        canonical_payload=canonical_run_prepared_audit_payload(binding),
    )
    policy = _policy(spec_set)
    portfolio = _snapshot(spec_set)
    risk = _create_risk_state_snapshot(
        run_id=order.run_id,
        policy_id=policy.policy_id,
        policy_sha256=phase1_risk_policy_digest(policy),
        risk_state_version=0,
        halted=False,
        halt_reason=None,
        halt_causal_root_available_at=None,
        halt_dispatch_sequence=None,
        conflict_existing_intent_sha256=None,
        conflict_submitted_intent_sha256=None,
    )
    global_halt = GlobalHaltSnapshot(order.run_id, False, 0)
    gate = InstrumentGateSnapshot(
        order.run_id,
        order.instrument,
        order.order_id,
        Sha256Digest("33" * 32),
        1,
        False,
    )
    runtime = _Runtime(order.run_id, spec_set, root)
    authority, preparation_capability, activation_seal = (
        create_dormant_historical_submission_authorization_authority(
            binding=binding,
            audit=audit,
            runtime=cast(RuntimeLifecyclePort, runtime),
            spec_set=spec_set,
            execution_policy=EXECUTION_POLICY,
            portfolio=_Port(portfolio),
            risk=_Port(risk),
            global_halt=_Port(global_halt),
            instrument_gate=_Port(gate),
        )
    )

    with pytest.raises(HistoricalPreEffectAuthorizationError, match="dormant"):
        authority.prepare(
            order,
            causal_market_root=root,
            dispatch_sequence=1,
            capability=preparation_capability,
        )

    coordinator: Any = SimpleNamespace(
        binding=binding,
        state=SimpleNamespace(state_version=1, phase=CoordinatorPhase.RUNNING),
    )
    authority.activate(coordinator, seal=activation_seal)
    acknowledgement = authority.prepare(
        order,
        causal_market_root=root,
        dispatch_sequence=1,
        capability=preparation_capability,
    )
    replay = authority.prepare(
        order,
        causal_market_root=root,
        dispatch_sequence=1,
        capability=preparation_capability,
    )
    proof = authority.verify_authorized_historical_submission(
        order_id=order.order_id,
        canonical_order_bytes=canonical_order_bytes(order),
        canonical_execution_request_bytes=canonical_execution_request_bytes(order),
        canonical_causal_market_bytes=canonical_market_data_record_bytes(root),
        causal_market_sha256=historical_market_root_digest(root),
        causal_root_key=runtime_root_order_key(root),
        dispatch_sequence=1,
    )

    assert replay is acknowledgement
    assert proof.audit_acknowledgement_sha256.value
    assert [record.record_kind.value for record in audit.records] == [
        "run.prepared",
        "submission.pre_effect_authorization",
    ]
    with pytest.raises(HistoricalPreEffectAuthorizationError, match="already activated"):
        authority.activate(coordinator, seal=activation_seal)

    failed_authority, _failed_capability, failed_seal = (
        create_dormant_historical_submission_authorization_authority(
            binding=binding,
            audit=audit,
            runtime=cast(RuntimeLifecyclePort, runtime),
            spec_set=spec_set,
            execution_policy=EXECUTION_POLICY,
            portfolio=_Port(portfolio),
            risk=_Port(risk),
            global_halt=_Port(global_halt),
            instrument_gate=_Port(gate),
        )
    )
    foreign_coordinator: Any = SimpleNamespace(
        binding=RunBinding(
            binding.reference,
            Sha256Digest("44" * 32),
        ),
        state=coordinator.state,
    )
    with pytest.raises(HistoricalPreEffectAuthorizationError, match="binding conflicts"):
        failed_authority.activate(foreign_coordinator, seal=failed_seal)
    with pytest.raises(HistoricalPreEffectAuthorizationError, match="seal conflicts"):
        failed_authority.activate(coordinator, seal=failed_seal)


def test_recovery_rebuilds_current_attempt_without_second_append() -> None:
    spec_set, order_authority, orders = _orders()
    order = orders[0]
    root = _market()
    binding = RunBinding(
        RunReference(order.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _MemoryAudit(binding)
    audit.append(
        record_kind=AuditRecordKind.RUN_PREPARED,
        subject_kind=AuditSubjectKind.RUN_MANIFEST,
        subject_sha256=binding.manifest_sha256,
        canonical_payload=canonical_run_prepared_audit_payload(binding),
    )
    policy = _policy(spec_set)
    portfolio = _snapshot(spec_set)
    risk = _create_risk_state_snapshot(
        run_id=order.run_id,
        policy_id=policy.policy_id,
        policy_sha256=phase1_risk_policy_digest(policy),
        risk_state_version=0,
        halted=False,
        halt_reason=None,
        halt_causal_root_available_at=None,
        halt_dispatch_sequence=None,
        conflict_existing_intent_sha256=None,
        conflict_submitted_intent_sha256=None,
    )
    global_halt = _Port(GlobalHaltSnapshot(order.run_id, False, 0))
    gate = _Port(
        InstrumentGateSnapshot(
            order.run_id,
            order.instrument,
            order.order_id,
            Sha256Digest("33" * 32),
            1,
            False,
        )
    )
    runtime = _Runtime(order.run_id, spec_set, root)
    portfolio_port = _Port(portfolio)
    risk_port = _Port(risk)

    def create_authority(
        authority_audit: _MemoryAudit = audit,
    ) -> tuple[Any, object, object]:
        return create_dormant_historical_submission_authorization_authority(
            binding=binding,
            audit=authority_audit,
            runtime=cast(RuntimeLifecyclePort, runtime),
            spec_set=spec_set,
            execution_policy=EXECUTION_POLICY,
            portfolio=portfolio_port,
            risk=risk_port,
            global_halt=global_halt,
            instrument_gate=gate,
        )

    first, capability, seal = create_authority()
    coordinator: Any = SimpleNamespace(
        binding=binding,
        state=SimpleNamespace(state_version=1, phase=CoordinatorPhase.RUNNING),
    )
    first.activate(coordinator, seal=seal)
    original_ack = first.prepare(
        order,
        causal_market_root=root,
        dispatch_sequence=1,
        capability=capability,
    )
    authorization_document = json.loads(audit.records[-1].canonical_payload)
    original_receipt = _receipt(order, root, original_ack, authorization_document)
    assert _authorization_payload_from_receipt(object(), original_ack, order) is None  # type: ignore[arg-type]
    incomplete_receipt = object.__new__(HistoricalSubmissionReceipt)
    assert _authorization_payload_from_receipt(incomplete_receipt, original_ack, order) is None
    foreign_order = copy(order)
    object.__setattr__(foreign_order, "run_id", RunId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
    assert (
        _authorization_payload_from_receipt(original_receipt, original_ack, foreign_order) is None
    )

    recovered, recovered_capability, recovered_seal = create_authority()
    recovered.recover_attempts(
        _RepeatableRecordSource(tuple(audit.records)),
        orders=order_authority,
        submissions=_NoSubmissions(),
        seal=recovered_seal,
    )
    recovered.activate(coordinator, seal=recovered_seal)
    recovered_ack = recovered.prepare(
        order,
        causal_market_root=root,
        dispatch_sequence=1,
        capability=recovered_capability,
    )
    proof = recovered.verify_authorized_historical_submission(
        order_id=order.order_id,
        canonical_order_bytes=canonical_order_bytes(order),
        canonical_execution_request_bytes=canonical_execution_request_bytes(order),
        canonical_causal_market_bytes=canonical_market_data_record_bytes(root),
        causal_market_sha256=historical_market_root_digest(root),
        causal_root_key=runtime_root_order_key(root),
        dispatch_sequence=1,
    )

    assert recovered_ack == original_ack
    assert proof.audit_acknowledgement_sha256.value
    assert len(audit.records) == 2

    committed, _committed_capability, committed_seal = create_authority()
    committed.recover_attempts(
        tuple(audit.records),
        orders=order_authority,
        submissions=_Submissions(original_receipt),
        seal=committed_seal,
    )
    invalid_records, _invalid_capability, invalid_records_seal = create_authority()
    with pytest.raises(HistoricalPreEffectAuthorizationError, match="records are invalid"):
        invalid_records.recover_attempts(
            list(audit.records),
            orders=order_authority,
            submissions=_NoSubmissions(),
            seal=invalid_records_seal,
        )
    invalid_seal, _invalid_seal_capability, _expected_seal = create_authority()
    with pytest.raises(HistoricalPreEffectAuthorizationError, match="seal conflicts"):
        invalid_seal.recover_attempts(
            tuple(audit.records),
            orders=order_authority,
            submissions=_NoSubmissions(),
            seal=object(),
        )
    duplicate, _duplicate_capability, duplicate_seal = create_authority()
    with pytest.raises(HistoricalPreEffectAuthorizationError, match="key is duplicated"):
        duplicate.recover_attempts(
            (*audit.records, audit.records[-1]),
            orders=order_authority,
            submissions=_NoSubmissions(),
            seal=duplicate_seal,
        )
    missing_order, _missing_capability, missing_order_seal = create_authority()
    with pytest.raises(HistoricalPreEffectAuthorizationError, match="Order evidence conflicts"):
        missing_order.recover_attempts(
            tuple(audit.records),
            orders=_NoOrders(),
            submissions=_NoSubmissions(),
            seal=missing_order_seal,
        )

    tampered_document = dict(authorization_document)
    tampered_document["portfolio_snapshot_version"] += 1
    tampered_payload = json.dumps(
        tampered_document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    tampered_audit = _MemoryAudit(binding)
    tampered_audit.append(
        record_kind=AuditRecordKind.RUN_PREPARED,
        subject_kind=AuditSubjectKind.RUN_MANIFEST,
        subject_sha256=binding.manifest_sha256,
        canonical_payload=canonical_run_prepared_audit_payload(binding),
    )
    tampered_ack = tampered_audit.append(
        record_kind=AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION,
        subject_kind=AuditSubjectKind.HISTORICAL_EXECUTION_REQUEST,
        subject_sha256=audit_subject_digest(
            AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION,
            tampered_payload,
        ),
        canonical_payload=tampered_payload,
    )
    tampered_receipt = _receipt(order, root, tampered_ack, tampered_document)
    conflicting, _conflicting_capability, conflicting_seal = create_authority(tampered_audit)
    with pytest.raises(HistoricalPreEffectAuthorizationError, match="receipt evidence conflicts"):
        conflicting.recover_attempts(
            tuple(tampered_audit.records),
            orders=order_authority,
            submissions=_Submissions(tampered_receipt),
            seal=conflicting_seal,
        )

    global_halt.value = GlobalHaltSnapshot(order.run_id, False, 1)
    with pytest.raises(HistoricalPreEffectAuthorizationError, match="state changed"):
        recovered.verify_authorized_historical_submission(
            order_id=order.order_id,
            canonical_order_bytes=canonical_order_bytes(order),
            canonical_execution_request_bytes=canonical_execution_request_bytes(order),
            canonical_causal_market_bytes=canonical_market_data_record_bytes(root),
            causal_market_sha256=historical_market_root_digest(root),
            causal_root_key=runtime_root_order_key(root),
            dispatch_sequence=1,
        )
