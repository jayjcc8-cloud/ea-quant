from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ea.core.audit import (
    AuditRecordKind,
    AuditSubjectKind,
    canonical_run_prepared_audit_payload,
)
from ea.core.execution_messages import (
    canonical_execution_request_bytes,
    canonical_order_bytes,
)
from ea.core.historical_matching import HistoricalPreEffectAuthorizationError
from ea.core.lifecycle import (
    CoordinatorPhase,
    GlobalHaltSnapshot,
    InstrumentGateSnapshot,
    RuntimeLifecyclePort,
)
from ea.core.market_data import Adjustment, Bar, MarketDataEnvelope, SourceId
from ea.core.market_data_codec import canonical_market_data_record_bytes
from ea.core.risk import _create_risk_state_snapshot, phase1_risk_policy_digest
from ea.core.run import RunBinding, RunReference, Sha256Digest
from ea.core.runtime import runtime_root_order_key
from ea.core.strategy import causal_market_digest
from ea.runtime.authorization import (
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


class _Port:
    def __init__(self, value: Any) -> None:
        self.value = value

    def current_snapshot(self) -> Any:
        return self.value

    def current_state(self) -> Any:
        return self.value

    def current_for(self, instrument: Any) -> Any:
        return self.value


class _Runtime:
    def __init__(self, run_id: Any, spec_set: Any, root: Any) -> None:
        self.run_id = run_id
        self.spec_set = spec_set
        self.active_lease = SimpleNamespace(root=root, dispatch_sequence=1)


class _NoSubmissions:
    def resolve_submission_receipt(self, **values: Any) -> None:
        del values
        return None


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
        causal_market_sha256=causal_market_digest(root),
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

    def create_authority() -> tuple[Any, object, object]:
        return create_dormant_historical_submission_authorization_authority(
            binding=binding,
            audit=audit,
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

    recovered, recovered_capability, recovered_seal = create_authority()
    recovered.recover_attempts(
        tuple(audit.records),
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
        causal_market_sha256=causal_market_digest(root),
        causal_root_key=runtime_root_order_key(root),
        dispatch_sequence=1,
    )

    assert recovered_ack == original_ack
    assert proof.audit_acknowledgement_sha256.value
    assert len(audit.records) == 2

    global_halt.value = GlobalHaltSnapshot(order.run_id, False, 1)
    with pytest.raises(HistoricalPreEffectAuthorizationError, match="state changed"):
        recovered.verify_authorized_historical_submission(
            order_id=order.order_id,
            canonical_order_bytes=canonical_order_bytes(order),
            canonical_execution_request_bytes=canonical_execution_request_bytes(order),
            canonical_causal_market_bytes=canonical_market_data_record_bytes(root),
            causal_market_sha256=causal_market_digest(root),
            causal_root_key=runtime_root_order_key(root),
            dispatch_sequence=1,
        )
