from __future__ import annotations

import json
import os
from inspect import signature
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

import ea.composition.lifecycle as lifecycle_composition
import ea.composition.run as run_composition
from ea.composition.lifecycle import (
    ExecutionFactHistoryView,
    HistoricalMatcherHistoryView,
    HistoricalRuntimeHistoryView,
    Phase1HistoricalLifecycle,
    Phase1HistoricalLifecycleCoordinatorFacade,
    _require_recovery_history_frontier,
    create_phase1_historical_lifecycle,
    recover_phase1_historical_lifecycle,
    recover_phase1_historical_terminal_evidence,
)
from ea.composition.run import RunCompositionError, admit_recovered_run
from ea.core.audit import (
    AuditRecordKind,
    AuditSubjectKind,
    audit_append_acknowledgement_digest,
    audit_chain_head,
    audit_record_digest,
    canonical_run_prepared_audit_payload,
    create_audit_append_acknowledgement,
)
from ea.core.execution_identity import SourceNamespace
from ea.core.execution_messages import FactProvenanceId
from ea.core.lifecycle import (
    ActiveDispatchWindowStage,
    CoordinatorPhase,
    GlobalHaltSnapshot,
    InstrumentGateSnapshot,
    LifecycleError,
    active_dispatch_window_digest,
    canonical_active_dispatch_window_bytes,
)
from ea.core.market_data import MarketDataEnvelope
from ea.core.outcomes import OutcomeCode
from ea.core.risk import _create_risk_state_snapshot, phase1_risk_policy_digest
from ea.core.run import RunBinding, RunReference, Sha256Digest
from ea.data import create_phase1_historical_market_source_bridge
from ea.execution.fact_authority import (
    create_phase1_execution_fact_authority,
    recover_phase1_execution_fact_authority_history,
)
from ea.execution.matcher import recover_phase1_historical_matcher_history
from ea.experiments.audit import (
    PosixAuditJournal,
    create_posix_audit_journal,
    reopen_posix_audit_journal,
)
from ea.experiments.binding import BoundAuditPort
from ea.experiments.store import (
    AuditRunBinding,
    LocalResultStore,
    VerifiedIncompleteRecoveryBinding,
)
from ea.runtime.authorization import (
    create_dormant_historical_submission_authorization_authority,
)
from ea.runtime.coordinator import recover_phase1_lifecycle_coordinator
from ea.runtime.historical import create_phase1_historical_market_runtime
from ea.runtime.matcher import (
    create_historical_matcher_descendant_fact_dispatch_verifier,
    create_historical_matcher_dispatch_verifier,
)
from unit.test_audit_journal import _release_simulated_process_writer
from unit.test_execution_fact_authority import (
    EXECUTION_POLICY,
    _orders,
    _policy,
    _snapshot,
)
from unit.test_historical_matcher import _system
from unit.test_historical_runtime import _row, _source
from unit.test_lifecycle_coordinator import _MemoryAudit
from unit.test_store import _root, _spec


def _audit_history_signature(records: tuple[Any, ...]) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            record.canonical_payload,
            record.subject_sha256,
            audit_record_digest(record),
            audit_append_acknowledgement_digest(create_audit_append_acknowledgement(record)),
            audit_chain_head(record),
        )
        for record in records
    )


def _policy_for(matcher: Any) -> Any:
    from ea.core import create_phase1_risk_policy

    policy = _policy(matcher.spec_set)
    return create_phase1_risk_policy(
        policy_id=policy.policy_id,
        spec_set=matcher.spec_set,
        execution_policy=matcher.execution_policy,
        instrument_limits=policy.instrument_limits,
    )


class _UncertainOnceAuthorizationAudit:
    def __init__(self, binding: RunBinding) -> None:
        self.binding = binding
        self._inner = _MemoryAudit(binding)
        self.failed = False
        self.settlement_failed = False

    @property
    def records(self) -> list[Any]:
        return self._inner.records

    def append(self, **values: Any) -> Any:
        if (
            values["record_kind"] is AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION
            and not self.failed
        ):
            self.failed = True
            self._inner.append(**values)
            raise RuntimeError("injected uncertain authorization append failure")
        return self._inner.append(**values)

    def settle_append(self, **values: Any) -> Any:
        if not self.settlement_failed:
            self.settlement_failed = True
            raise RuntimeError("injected uncertain authorization settlement failure")
        return self._inner.settle_append(**values)


@pytest.mark.parametrize(
    "failure_stage",
    ("coordinator", "history-frontier", "activation", "reconciliation"),
)
def test_gated_recovery_failure_cannot_reopen_consumption(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    from ea.execution.fact_authority import Phase1ExecutionFactAuthority
    from ea.runtime.historical import Phase1HistoricalMarketRuntime

    recovery = object.__new__(run_composition.AdmittedRecoveredRun)
    object.__setattr__(recovery, "_consumption_lock", Lock())
    object.__setattr__(recovery, "_consumption_state", "available")
    object.__setattr__(recovery, "_consumption_token", None)
    object.__setattr__(recovery, "binding", SimpleNamespace())
    object.__setattr__(recovery, "audit", SimpleNamespace())
    object.__setattr__(recovery, "records", SimpleNamespace())

    def fail_at(stage: str) -> None:
        if failure_stage == stage:
            raise RuntimeError(f"injected {stage} failure after economic replay began")

    authorization = SimpleNamespace(
        recover_attempts=lambda *args, **kwargs: None,
        activate=lambda *args, **kwargs: fail_at("activation"),
    )
    coordinator = SimpleNamespace(
        _reconcile_recovered_authorization=lambda: fail_at("reconciliation")
    )
    for name, replacement in (
        (
            "create_dormant_historical_submission_authorization_authority",
            lambda **kwargs: (authorization, object(), object()),
        ),
        ("create_historical_matcher_dispatch_verifier", lambda runtime: object()),
        ("recover_phase1_historical_matcher_history", lambda *args, **kwargs: object()),
        (
            "create_historical_matcher_descendant_fact_dispatch_verifier",
            lambda **kwargs: object(),
        ),
        (
            "recover_phase1_execution_fact_authority_history",
            lambda *args, **kwargs: object(),
        ),
    ):
        monkeypatch.setattr(lifecycle_composition, name, replacement)

    def recover_coordinator(**kwargs: Any) -> Any:
        fail_at("coordinator")
        return coordinator

    monkeypatch.setattr(
        lifecycle_composition,
        "recover_phase1_lifecycle_coordinator",
        recover_coordinator,
    )
    monkeypatch.setattr(
        lifecycle_composition,
        "_require_recovery_history_frontier",
        lambda **kwargs: fail_at("history-frontier"),
    )
    _fixture, matcher, _orders_value, _causal, _delayed, _end = _system()
    object.__setattr__(
        recovery,
        "binding",
        RunBinding(RunReference(matcher.run_id, Sha256Digest("11" * 32)), Sha256Digest("22" * 32)),
    )
    frontier = object()
    monkeypatch.setattr(
        lifecycle_composition,
        "_create_economic_gate",
        lambda *args: (frontier, frontier, frontier, frontier),
    )
    with pytest.raises(RuntimeError, match=failure_stage):
        recover_phase1_historical_lifecycle(
            recovery=recovery,
            runtime=object.__new__(Phase1HistoricalMarketRuntime),
            matcher_history=matcher,
            fact_history=object.__new__(Phase1ExecutionFactAuthority),
            order_issuance_verifier=object(),  # type: ignore[arg-type]
            risk_policy=_policy_for(matcher),
            global_halt=object(),  # type: ignore[arg-type]
            instrument_gate=object(),  # type: ignore[arg-type]
        )

    with pytest.raises(RunCompositionError, match="already consumed"):
        recovery._reserve_consumption()


class _FailOnceAuthorizationAudit(_MemoryAudit):
    failed = False

    def append(self, **values: Any) -> Any:
        if (
            values["record_kind"] is AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION
            and not self.failed
        ):
            self.failed = True
            raise RuntimeError("injected definite authorization append failure")
        return super().append(**values)


class _AuthorizationAppendHookAudit(_MemoryAudit):
    hook: Any = None

    def append(self, **values: Any) -> Any:
        acknowledgement = super().append(**values)
        if (
            values["record_kind"] is AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION
            and self.hook is not None
        ):
            hook, self.hook = self.hook, None
            hook()
        return acknowledgement


class _CommitThenRaiseCompletionAudit(_MemoryAudit):
    raised = False

    def append(self, **values: Any) -> Any:
        acknowledgement = super().append(**values)
        if values["record_kind"] is AuditRecordKind.RUNTIME_DISPATCH_COMPLETED and not self.raised:
            self.raised = True
            raise RuntimeError("injected committed completion return failure")
        return acknowledgement


class _RaiseAfterCommittedAuthorization:
    def __init__(self, authority: Any) -> None:
        self.authority = authority

    def prepare_attempt(self, *args: Any, **values: Any) -> Any:
        self.authority.prepare_attempt(*args, **values)
        raise RuntimeError("injected committed attempt resolver conflict")

    def resolve_attempt_order(self, **values: Any) -> None:
        del values
        return None

    def resolve_attempt(self, **values: Any) -> None:
        del values
        return None

    def resolve_attempt_acknowledgement(self, **values: Any) -> None:
        del values
        return None


class _UnusedFreshness:
    def current_snapshot(self) -> Any:
        return None

    def current_state(self) -> Any:
        return None

    def current_for(self, instrument: Any) -> Any:
        del instrument
        return None


class _Freshness:
    def __init__(self, value: Any) -> None:
        self.value = value

    def current_snapshot(self) -> Any:
        return self.value

    def current_state(self) -> Any:
        return self.value

    def current_for(self, instrument: Any) -> Any:
        del instrument
        return self.value


class _RecoveryOrderVerifier:
    def __init__(self, matcher: Any, orders: list[Any]) -> None:
        self.run_id = matcher.run_id
        self.spec_set = matcher.spec_set
        self.execution_policy = matcher.execution_policy
        self._orders = tuple(orders)

    def resolve_issued_order_by_id(self, order_id: Any) -> Any:
        return next((order for order in self._orders if order.order_id == order_id), None)

    def resolve_issued_order_by_client_submission_key(self, key: Any) -> Any:
        return next(
            (order for order in self._orders if order.client_submission_key == key),
            None,
        )


class _UnusedDispatch:
    def __init__(self, matcher: Any) -> None:
        self.run_id = matcher.run_id
        self.spec_set = matcher.spec_set

    def resolve_active_issued_fact_dispatch(self, **values: Any) -> None:
        del values
        return None


def _empty_runtime_for(matcher: Any) -> Any:
    source = create_phase1_historical_market_source_bridge(
        _source(
            _row(
                start="2026-01-02T09:30:00.000000Z",
                end="2026-01-02T09:31:00.000000Z",
                available="2026-01-02T09:31:00.000000Z",
                sequence=0,
            )
        )
    )
    return create_phase1_historical_market_runtime(
        run_id=matcher.run_id,
        spec_set=matcher.spec_set,
        source=source,
    )


def test_public_lifecycle_composition_owns_economic_capabilities() -> None:
    forbidden = {
        "portfolio",
        "risk",
        "ledger_handoff_authority",
        "risk_authority",
        "risk_refresh_authority",
        "frontier",
    }
    for factory in (
        create_phase1_historical_lifecycle,
        recover_phase1_historical_lifecycle,
        recover_phase1_historical_terminal_evidence,
    ):
        parameters = signature(factory).parameters
        assert "risk_policy" in parameters
        assert forbidden.isdisjoint(parameters)


def _staged_lifecycle(
    audit_factory: Any = _MemoryAudit,
) -> tuple[Any, Any, tuple[Any, ...], Any, Any, tuple[Any, ...]]:
    spec_set, order_authority, orders = _orders(count=2)
    order = orders[0]
    binding = RunBinding(
        RunReference(order.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = audit_factory(binding)
    prepared_acknowledgement = audit.append(
        record_kind=AuditRecordKind.RUN_PREPARED,
        subject_kind=AuditSubjectKind.RUN_MANIFEST,
        subject_sha256=binding.manifest_sha256,
        canonical_payload=canonical_run_prepared_audit_payload(binding),
    )
    source = create_phase1_historical_market_source_bridge(
        _source(
            _row(
                start="2026-01-02T09:30:00.000000Z",
                end="2026-01-02T09:31:00.000000Z",
                available="2026-01-02T09:31:00.000000Z",
                sequence=0,
            )
        )
    )
    runtime = create_phase1_historical_market_runtime(
        run_id=order.run_id,
        spec_set=spec_set,
        source=source,
    )
    risk_policy = _policy(spec_set)
    risk = _create_risk_state_snapshot(
        run_id=order.run_id,
        policy_id=risk_policy.policy_id,
        policy_sha256=phase1_risk_policy_digest(risk_policy),
        risk_state_version=0,
        halted=False,
        halt_reason=None,
        halt_causal_root_available_at=None,
        halt_dispatch_sequence=None,
        conflict_existing_intent_sha256=None,
        conflict_submitted_intent_sha256=None,
    )
    freshness = (
        _Freshness(_snapshot(spec_set)),
        _Freshness(risk),
        _Freshness(GlobalHaltSnapshot(order.run_id, False, 0)),
        _Freshness(
            InstrumentGateSnapshot(
                order.run_id,
                order.instrument,
                order.order_id,
                Sha256Digest("33" * 32),
                1,
                False,
            )
        ),
    )
    lifecycle = create_phase1_historical_lifecycle(
        binding=binding,
        prepared_acknowledgement=prepared_acknowledgement,
        audit=audit,
        runtime=runtime,
        spec_set=spec_set,
        execution_policy=EXECUTION_POLICY,
        source_namespace=SourceNamespace("phase1.historical-matcher.v1"),
        provenance_id=FactProvenanceId("phase1.simulator.v1"),
        order_issuance_verifier=order_authority,
        risk_policy=risk_policy,
        global_halt=freshness[2],
        instrument_gate=freshness[3],
    )
    return lifecycle, order_authority, orders, runtime, audit, freshness


def _recover_staged_lifecycle(
    lifecycle: Phase1HistoricalLifecycle,
    *,
    order_authority: Any,
    runtime: Any,
    audit: _MemoryAudit,
    freshness: tuple[Any, ...],
) -> tuple[Any, Any]:
    matcher_history = cast(Any, lifecycle.matcher)._HistoricalMatcherHistoryView__matcher
    fact_history = cast(Any, lifecycle.fact_authority)._ExecutionFactHistoryView__authority
    gate = lifecycle_composition._create_economic_gate(
        audit.binding.reference.run_id,
        matcher_history.spec_set,
        matcher_history.execution_policy,
        _policy_for(matcher_history),
    )
    authorization, capability, activation_seal = (
        create_dormant_historical_submission_authorization_authority(
            binding=audit.binding,
            audit=audit,
            runtime=runtime,
            spec_set=matcher_history.spec_set,
            execution_policy=matcher_history.execution_policy,
            portfolio=gate[3],
            risk=gate[3],
            global_halt=freshness[2],
            instrument_gate=freshness[3],
        )
    )
    matcher = recover_phase1_historical_matcher_history(
        matcher_history,
        order_issuance_verifier=order_authority,
        submission_authorization_verifier=authorization,
        active_dispatch_verifier=create_historical_matcher_dispatch_verifier(runtime),
    )
    fact_authority = recover_phase1_execution_fact_authority_history(
        fact_history,
        order_verifier=order_authority,
        dispatch_verifier=create_historical_matcher_descendant_fact_dispatch_verifier(
            runtime=runtime,
            matcher=matcher,
        ),
    )
    records = tuple(audit.records)
    authorization.recover_attempts(
        records,
        orders=order_authority,
        submissions=matcher,
        seal=activation_seal,
    )
    coordinator = recover_phase1_lifecycle_coordinator(
        binding=audit.binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=fact_authority,
        evidence_resolver=fact_authority,
        records=records,
        authorization=authorization,
        authorization_capability=capability,
        ledger_handoff_authority=gate[0],
        risk_authority=gate[1],
        risk_refresh_authority=gate[2],
        frontier=gate[3],
    )
    authorization.activate(coordinator, seal=activation_seal)
    coordinator._reconcile_recovered_authorization()
    return coordinator, matcher


def test_composed_staged_window_authorizes_and_submits_before_completion() -> None:
    spec_set, order_authority, orders = _orders(count=2)
    order = orders[0]
    binding = RunBinding(
        RunReference(order.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _MemoryAudit(binding)
    prepared_acknowledgement = audit.append(
        record_kind=AuditRecordKind.RUN_PREPARED,
        subject_kind=AuditSubjectKind.RUN_MANIFEST,
        subject_sha256=binding.manifest_sha256,
        canonical_payload=canonical_run_prepared_audit_payload(binding),
    )
    source = create_phase1_historical_market_source_bridge(
        _source(
            _row(
                start="2026-01-02T09:30:00.000000Z",
                end="2026-01-02T09:31:00.000000Z",
                available="2026-01-02T09:31:00.000000Z",
                sequence=0,
            )
        )
    )
    runtime = create_phase1_historical_market_runtime(
        run_id=order.run_id,
        spec_set=spec_set,
        source=source,
    )
    risk_policy = _policy(spec_set)
    lifecycle = create_phase1_historical_lifecycle(
        binding=binding,
        prepared_acknowledgement=prepared_acknowledgement,
        audit=audit,
        runtime=runtime,
        spec_set=spec_set,
        execution_policy=EXECUTION_POLICY,
        source_namespace=SourceNamespace("phase1.historical-matcher.v1"),
        provenance_id=FactProvenanceId("phase1.simulator.v1"),
        order_issuance_verifier=order_authority,
        risk_policy=risk_policy,
        global_halt=_Freshness(GlobalHaltSnapshot(order.run_id, False, 0)),
        instrument_gate=_Freshness(
            InstrumentGateSnapshot(
                order.run_id,
                order.instrument,
                order.order_id,
                Sha256Digest("33" * 32),
                1,
                False,
            )
        ),
    )

    assert type(lifecycle.coordinator) is Phase1HistoricalLifecycleCoordinatorFacade
    assert not hasattr(lifecycle.coordinator, "process_next_dispatch")
    assert not hasattr(lifecycle.coordinator, "retry_active_dispatch")
    assert not hasattr(lifecycle.coordinator, "_authorization")
    assert not hasattr(lifecycle.coordinator, "_matcher")
    assert not hasattr(lifecycle.coordinator, "_runtime")

    window = lifecycle.coordinator.begin_next_dispatch()
    lease = runtime.active_lease
    assert lease is not None
    root = lease.root
    assert type(root) is MarketDataEnvelope
    acknowledgement = lifecycle.coordinator.prepare_submission_authorization(
        window,
        order,
        causal_market_root=root,
        dispatch_sequence=lease.dispatch_sequence,
    )
    with pytest.raises(LifecycleError, match="exact matcher receipt"):
        lifecycle.coordinator.complete_active_dispatch(window)
    with pytest.raises(LifecycleError) as occupied:
        lifecycle.coordinator.prepare_submission_authorization(
            window,
            orders[1],
            causal_market_root=root,
            dispatch_sequence=lease.dispatch_sequence,
        )
    assert occupied.value.code is OutcomeCode.RISK_STALE_APPROVAL
    assert [record.record_kind for record in audit.records].count(
        AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION
    ) == 1
    receipt = lifecycle.coordinator.submit_authorized_order(window, order)

    assert (
        lifecycle.coordinator.prepare_submission_authorization(
            window,
            order,
            causal_market_root=root,
            dispatch_sequence=lease.dispatch_sequence,
        )
        is acknowledgement
    )
    assert lifecycle.coordinator.submit_authorized_order(window, order) is receipt
    assert runtime.active_lease is lease

    outcome = lifecycle.coordinator.complete_active_dispatch(window)

    assert outcome.runtime_acknowledged is True
    assert runtime.active_lease is None
    assert [record.record_kind for record in audit.records] == [
        AuditRecordKind.RUN_PREPARED,
        AuditRecordKind.MATCHER_DISPATCH_BATCH,
        AuditRecordKind.RISK_PORTFOLIO_REFRESH,
        AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION,
        AuditRecordKind.RUNTIME_DISPATCH_COMPLETED,
    ]
    completion_document = json.loads(audit.records[-1].canonical_payload)
    assert completion_document["authorization_attempt_outcome"]["status"] == "authorized"
    assert completion_document["submission_count"] == 1


def test_unresolved_authorization_retry_preserves_key_and_burns_on_freshness_change() -> None:
    lifecycle, _authority, orders, runtime, audit, freshness = _staged_lifecycle(
        _UncertainOnceAuthorizationAudit
    )
    order = orders[0]
    window = lifecycle.coordinator.begin_next_dispatch()
    lease = runtime.active_lease
    assert lease is not None
    assert type(lease.root) is MarketDataEnvelope

    with pytest.raises(LifecycleError) as unresolved:
        lifecycle.coordinator.prepare_submission_authorization(
            window,
            order,
            causal_market_root=lease.root,
            dispatch_sequence=lease.dispatch_sequence,
        )
    assert unresolved.value.code is OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED
    with pytest.raises(LifecycleError, match="unresolved authorization"):
        lifecycle.coordinator.complete_active_dispatch(window)

    freshness[3].value = InstrumentGateSnapshot(
        order.run_id,
        order.instrument,
        order.order_id,
        Sha256Digest("33" * 32),
        1,
        True,
    )
    with pytest.raises(LifecycleError) as burned:
        lifecycle.coordinator.prepare_submission_authorization(
            window,
            order,
            causal_market_root=lease.root,
            dispatch_sequence=lease.dispatch_sequence,
        )
    assert burned.value.code is OutcomeCode.RISK_STALE_APPROVAL
    outcome = lifecycle.coordinator.complete_active_dispatch(window)
    completion = json.loads(audit.records[-1].canonical_payload)

    assert outcome.runtime_acknowledged is True
    assert completion["authorization_attempt_outcome"]["status"] == "burned"
    assert completion["submission_count"] == 0
    assert [record.record_kind for record in audit.records].count(
        AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION
    ) == 1


def test_denied_and_burned_attempts_complete_without_matcher_receipt() -> None:
    for audit_factory, burn_after_append in (
        (_MemoryAudit, False),
        (_AuthorizationAppendHookAudit, True),
    ):
        lifecycle, _authority, orders, runtime, audit, freshness = _staged_lifecycle(audit_factory)
        order = orders[0]
        gate = freshness[3]
        halted = InstrumentGateSnapshot(
            order.run_id,
            order.instrument,
            order.order_id,
            Sha256Digest("33" * 32),
            1,
            True,
        )
        if burn_after_append:
            audit.hook = lambda gate=gate, halted=halted: setattr(gate, "value", halted)
        else:
            gate.value = halted
        window = lifecycle.coordinator.begin_next_dispatch()
        lease = runtime.active_lease
        assert lease is not None
        assert type(lease.root) is MarketDataEnvelope

        with pytest.raises(LifecycleError) as rejected:
            lifecycle.coordinator.prepare_submission_authorization(
                window,
                order,
                causal_market_root=lease.root,
                dispatch_sequence=lease.dispatch_sequence,
            )
        assert (
            rejected.value.code is OutcomeCode.SUBMISSION_BLOCKED_BY_HALT
            if not burn_after_append
            else OutcomeCode.RISK_STALE_APPROVAL
        )

        outcome = lifecycle.coordinator.complete_active_dispatch(window)
        completion = json.loads(audit.records[-1].canonical_payload)
        assert outcome.runtime_acknowledged is True
        assert completion["authorization_attempt_outcome"]["status"] == (
            "burned" if burn_after_append else "denied"
        )
        assert completion["submission_count"] == 0


def test_definite_authorization_failure_closes_window_and_drains_failing_dispatch() -> None:
    lifecycle, order_authority, orders, runtime, audit, freshness = _staged_lifecycle(
        _FailOnceAuthorizationAudit
    )
    order = orders[0]
    window = lifecycle.coordinator.begin_next_dispatch()
    lease = runtime.active_lease
    assert lease is not None
    assert type(lease.root) is MarketDataEnvelope

    with pytest.raises(LifecycleError) as failed:
        lifecycle.coordinator.prepare_submission_authorization(
            window,
            order,
            causal_market_root=lease.root,
            dispatch_sequence=lease.dispatch_sequence,
        )
    assert failed.value.code is OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED
    assert lifecycle.coordinator.state.phase is CoordinatorPhase.FAILING
    with pytest.raises(LifecycleError, match="window conflicts"):
        lifecycle.coordinator.complete_active_dispatch(window)

    recovered, _matcher = _recover_staged_lifecycle(
        lifecycle,
        order_authority=order_authority,
        runtime=runtime,
        audit=audit,
        freshness=freshness,
    )
    assert recovered.state.phase is CoordinatorPhase.FAILING
    failing_window = recovered.resume_active_dispatch()
    assert failing_window.authorization_allowed is False
    outcome = recovered.complete_active_dispatch(failing_window)
    completion = json.loads(audit.records[-1].canonical_payload)

    assert outcome.runtime_acknowledged is True
    assert completion["authorization_attempt_outcome"] is None
    assert completion["submission_count"] == 0
    record_count = len(audit.records)

    restarted, restarted_matcher = _recover_staged_lifecycle(
        lifecycle,
        order_authority=order_authority,
        runtime=runtime,
        audit=audit,
        freshness=freshness,
    )
    assert restarted.state.phase is CoordinatorPhase.FAILING
    assert restarted.state.last_completed_dispatch_sequence == 1
    assert len(audit.records) == record_count
    assert len(restarted_matcher.state.receipt_sha256s) == 0
    assert [record.record_kind for record in audit.records].count(
        AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION
    ) == 0
    assert [record.record_kind for record in audit.records].count(
        AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION
    ) == 1
    assert [record.record_kind for record in audit.records].count(
        AuditRecordKind.RUNTIME_DISPATCH_COMPLETED
    ) == 1


def test_failed_authorization_completion_recovery_validates_exact_missing_key() -> None:
    lifecycle, order_authority, orders, runtime, audit, freshness = _staged_lifecycle(
        _FailOnceAuthorizationAudit
    )
    order = orders[0]
    window = lifecycle.coordinator.begin_next_dispatch()
    lease = runtime.active_lease
    assert lease is not None
    assert type(lease.root) is MarketDataEnvelope
    with pytest.raises(LifecycleError):
        lifecycle.coordinator.prepare_submission_authorization(
            window,
            order,
            causal_market_root=lease.root,
            dispatch_sequence=lease.dispatch_sequence,
        )

    failing_window = lifecycle.coordinator.resume_active_dispatch()
    lifecycle.coordinator.complete_active_dispatch(failing_window)
    completion = json.loads(audit.records[-1].canonical_payload)
    assert completion["authorization_attempt_outcome"] is None
    record_count = len(audit.records)

    recovered, matcher = _recover_staged_lifecycle(
        lifecycle,
        order_authority=order_authority,
        runtime=runtime,
        audit=audit,
        freshness=freshness,
    )

    assert recovered.state.phase is CoordinatorPhase.FAILING
    assert recovered.state.last_completed_dispatch_sequence == 1
    assert len(audit.records) == record_count
    assert len(matcher.state.receipt_sha256s) == 0


def _definite_authorization_failure_completion(*, restart_before_completion: bool) -> Any:
    lifecycle, order_authority, orders, runtime, audit, freshness = _staged_lifecycle(
        _FailOnceAuthorizationAudit
    )
    order = orders[0]
    window = lifecycle.coordinator.begin_next_dispatch()
    lease = runtime.active_lease
    assert lease is not None
    assert type(lease.root) is MarketDataEnvelope
    with pytest.raises(LifecycleError):
        lifecycle.coordinator.prepare_submission_authorization(
            window,
            order,
            causal_market_root=lease.root,
            dispatch_sequence=lease.dispatch_sequence,
        )
    coordinator: Any = lifecycle.coordinator
    if restart_before_completion:
        coordinator, _matcher = _recover_staged_lifecycle(
            lifecycle,
            order_authority=order_authority,
            runtime=runtime,
            audit=audit,
            freshness=freshness,
        )
    failing_window = coordinator.resume_active_dispatch()
    coordinator.complete_active_dispatch(failing_window)
    return audit.records[-1]


def test_definite_authorization_failure_completion_is_restart_invariant() -> None:
    uninterrupted = _definite_authorization_failure_completion(restart_before_completion=False)
    restarted = _definite_authorization_failure_completion(restart_before_completion=True)

    assert uninterrupted.record_kind is AuditRecordKind.RUNTIME_DISPATCH_COMPLETED
    assert restarted.record_kind is AuditRecordKind.RUNTIME_DISPATCH_COMPLETED
    assert uninterrupted.canonical_payload == restarted.canonical_payload
    assert uninterrupted.subject_sha256 == restarted.subject_sha256
    completion = json.loads(uninterrupted.canonical_payload)
    assert completion["authorization_attempt_count"] == 0
    assert completion["authorization_attempt_outcome"] is None


def test_committed_completion_return_failure_is_resolved_without_failing_transition() -> None:
    lifecycle, _authority, _orders, runtime, audit, _freshness = _staged_lifecycle(
        _CommitThenRaiseCompletionAudit
    )
    window = lifecycle.coordinator.begin_next_dispatch()

    outcome = lifecycle.coordinator.complete_active_dispatch(window)

    assert outcome.runtime_acknowledged is True
    assert lifecycle.coordinator.state.phase is CoordinatorPhase.RUNNING
    assert runtime.active_lease is None
    assert [record.record_kind for record in audit.records].count(
        AuditRecordKind.RUNTIME_DISPATCH_COMPLETED
    ) == 1
    assert AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION not in {
        record.record_kind for record in audit.records
    }


def test_committed_authorization_resolver_conflict_fails_closed_and_recovers() -> None:
    lifecycle, order_authority, orders, runtime, audit, freshness = _staged_lifecycle()
    order = orders[0]
    coordinator = cast(
        Any,
        lifecycle.coordinator,
    )._Phase1HistoricalLifecycleCoordinatorFacade__coordinator
    authority = coordinator._authorization
    coordinator._authorization = _RaiseAfterCommittedAuthorization(authority)
    window = lifecycle.coordinator.begin_next_dispatch()
    lease = runtime.active_lease
    assert lease is not None
    assert type(lease.root) is MarketDataEnvelope

    with pytest.raises(LifecycleError, match="could not be settled"):
        lifecycle.coordinator.prepare_submission_authorization(
            window,
            order,
            causal_market_root=lease.root,
            dispatch_sequence=lease.dispatch_sequence,
        )

    assert lifecycle.coordinator.state.phase is CoordinatorPhase.FAILING
    assert runtime.active_lease is lease
    with pytest.raises(LifecycleError, match="window conflicts"):
        lifecycle.coordinator.complete_active_dispatch(window)
    assert [record.record_kind for record in audit.records].count(
        AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION
    ) == 1
    assert [record.record_kind for record in audit.records].count(
        AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION
    ) == 1
    assert [record.record_kind for record in audit.records].count(
        AuditRecordKind.RUNTIME_DISPATCH_COMPLETED
    ) == 0

    recovered, _matcher = _recover_staged_lifecycle(
        lifecycle,
        order_authority=order_authority,
        runtime=runtime,
        audit=audit,
        freshness=freshness,
    )
    failing_window = recovered.resume_active_dispatch()
    outcome = recovered.complete_active_dispatch(failing_window)
    completion = json.loads(audit.records[-1].canonical_payload)

    assert outcome.runtime_acknowledged is True
    assert completion["authorization_attempt_count"] == 1
    assert completion["authorization_attempt_outcome"]["status"] == "burned"
    assert [record.record_kind for record in audit.records].count(
        AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION
    ) == 1
    assert [record.record_kind for record in audit.records].count(
        AuditRecordKind.RUNTIME_DISPATCH_COMPLETED
    ) == 1


def test_recovery_discards_provisional_freeze_and_reissues_canonical_window() -> None:
    lifecycle, order_authority, _orders, runtime, audit, freshness = _staged_lifecycle()
    window = lifecycle.coordinator.begin_next_dispatch()
    raw = cast(Any, lifecycle.coordinator)._Phase1HistoricalLifecycleCoordinatorFacade__coordinator
    assert raw._active is not None
    raw._active.window_stage = ActiveDispatchWindowStage.COMPLETION_FROZEN

    recovered, _matcher = _recover_staged_lifecycle(
        lifecycle,
        order_authority=order_authority,
        runtime=runtime,
        audit=audit,
        freshness=freshness,
    )
    recovered_window = recovered.resume_active_dispatch()

    assert recovered_window is not window
    assert canonical_active_dispatch_window_bytes(recovered_window) == (
        canonical_active_dispatch_window_bytes(window)
    )
    assert active_dispatch_window_digest(recovered_window) == active_dispatch_window_digest(window)
    outcome = recovered.complete_active_dispatch(recovered_window)
    assert outcome.runtime_acknowledged is True


def test_public_facade_forwards_only_staged_state_and_retry_operations() -> None:
    lifecycle, _order_authority, _orders, _runtime, audit, _freshness = _staged_lifecycle()
    coordinator = lifecycle.coordinator

    assert coordinator.binding == audit.binding
    assert coordinator.state.phase is CoordinatorPhase.ADMITTED
    assert coordinator.pre_terminal_state is None
    assert coordinator.terminal_state is None
    assert coordinator.terminal_outcome is None
    with pytest.raises(LifecycleError, match="completion-only retry"):
        coordinator.retry_active_dispatch_completion()
    with pytest.raises(LifecycleError, match="terminalization"):
        coordinator.retry_terminalization()

    coordinator.begin_next_dispatch()
    with pytest.raises(LifecycleError, match="completion-only retry"):
        coordinator.retry_active_dispatch_completion()


def test_public_bundle_and_facade_reject_each_foreign_carrier() -> None:
    lifecycle, _order_authority, _orders, _runtime, _audit, _freshness = _staged_lifecycle()
    seal = lifecycle_composition._LIFECYCLE_SEAL
    with pytest.raises(TypeError, match="created only by composition"):
        Phase1HistoricalLifecycle(
            _seal=seal,
            coordinator=object(),  # type: ignore[arg-type]
            matcher=lifecycle.matcher,
            fact_authority=lifecycle.fact_authority,
            runtime=lifecycle.runtime,
        )
    with pytest.raises(TypeError, match="created only by composition"):
        Phase1HistoricalLifecycle(
            _seal=seal,
            coordinator=lifecycle.coordinator,
            matcher=object(),  # type: ignore[arg-type]
            fact_authority=lifecycle.fact_authority,
            runtime=lifecycle.runtime,
        )
    with pytest.raises(TypeError, match="created only by composition"):
        Phase1HistoricalLifecycle(
            _seal=seal,
            coordinator=lifecycle.coordinator,
            matcher=lifecycle.matcher,
            fact_authority=object(),  # type: ignore[arg-type]
            runtime=lifecycle.runtime,
        )
    with pytest.raises(TypeError, match="created only by composition"):
        Phase1HistoricalLifecycle(
            _seal=seal,
            coordinator=lifecycle.coordinator,
            matcher=lifecycle.matcher,
            fact_authority=lifecycle.fact_authority,
            runtime=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="created only by composition"):
        Phase1HistoricalLifecycleCoordinatorFacade(
            object(),  # type: ignore[arg-type]
            seal=lifecycle_composition._COORDINATOR_FACADE_SEAL,
        )
    read_view_seal = lifecycle_composition._READ_VIEW_SEAL
    with pytest.raises(TypeError, match="created only by composition"):
        HistoricalMatcherHistoryView(object(), seal=read_view_seal)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="created only by composition"):
        ExecutionFactHistoryView(object(), seal=read_view_seal)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="created only by composition"):
        HistoricalRuntimeHistoryView(object(), seal=read_view_seal)  # type: ignore[arg-type]


def test_recovered_durable_authorization_reopens_the_same_submission_window() -> None:
    lifecycle, order_authority, orders, runtime, audit, freshness = _staged_lifecycle()
    order = orders[0]
    window = lifecycle.coordinator.begin_next_dispatch()
    lease = runtime.active_lease
    assert lease is not None
    root = lease.root
    acknowledgement = lifecycle.coordinator.prepare_submission_authorization(
        window,
        order,
        causal_market_root=root,
        dispatch_sequence=lease.dispatch_sequence,
    )
    record_count = len(audit.records)

    recovered, _matcher = _recover_staged_lifecycle(
        lifecycle,
        order_authority=order_authority,
        runtime=runtime,
        audit=audit,
        freshness=freshness,
    )
    raw = cast(Any, recovered)
    authorization = raw._authorization
    raw._authorization = None
    with pytest.raises(LifecycleError, match="authority is unavailable"):
        recovered._reconcile_recovered_authorization()
    raw._authorization = SimpleNamespace(
        resolve_attempt=lambda **_values: None,
        resolve_attempt_acknowledgement=lambda **_values: None,
    )
    with pytest.raises(LifecycleError, match="activation conflicts"):
        recovered._reconcile_recovered_authorization()
    raw._authorization = authorization
    assert raw._mutation_lock.acquire(blocking=False)
    try:
        with pytest.raises(LifecycleError, match="reentrant"):
            recovered._reconcile_recovered_authorization()
    finally:
        raw._mutation_lock.release()
    recovered._reconcile_recovered_authorization()
    recovered_window = recovered.resume_active_dispatch()

    assert (
        recovered.prepare_submission_authorization(
            recovered_window,
            order,
            causal_market_root=root,
            dispatch_sequence=lease.dispatch_sequence,
        )
        == acknowledgement
    )
    recovered.submit_authorized_order(recovered_window, order)
    outcome = recovered.complete_active_dispatch(recovered_window)

    assert outcome.runtime_acknowledged is True
    assert len(audit.records) == record_count + 1
    assert [record.record_kind for record in audit.records].count(
        AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION
    ) == 1


def test_recovered_matcher_receipt_is_replayed_without_second_submission() -> None:
    lifecycle, order_authority, orders, runtime, audit, freshness = _staged_lifecycle()
    order = orders[0]
    window = lifecycle.coordinator.begin_next_dispatch()
    lease = runtime.active_lease
    assert lease is not None
    root = lease.root
    lifecycle.coordinator.prepare_submission_authorization(
        window,
        order,
        causal_market_root=root,
        dispatch_sequence=lease.dispatch_sequence,
    )
    original_receipt = lifecycle.coordinator.submit_authorized_order(window, order)
    record_count = len(audit.records)

    recovered, matcher = _recover_staged_lifecycle(
        lifecycle,
        order_authority=order_authority,
        runtime=runtime,
        audit=audit,
        freshness=freshness,
    )
    active = cast(Any, recovered)._active
    assert active is not None
    retained_attempt = active.authorization_attempt
    active.authorization_attempt = None
    with pytest.raises(LifecycleError, match="frontier is not authorized"):
        recovered._reconcile_recovered_authorization()
    active.authorization_attempt = retained_attempt
    recovered._reconcile_recovered_authorization()
    recovered_window = recovered.resume_active_dispatch()
    replayed_receipt = recovered.submit_authorized_order(recovered_window, order)
    outcome = recovered.complete_active_dispatch(recovered_window)

    assert replayed_receipt == original_receipt
    assert outcome.runtime_acknowledged is True
    assert len(matcher.state.receipt_sha256s) == 1
    assert len(audit.records) == record_count + 1


def test_recovery_frontier_rejects_future_matcher_dispatch() -> None:
    _fixture, matcher, orders, _causal, delayed, _end = _system()
    matcher.match_active_market_root(delayed, dispatch_sequence=1)
    fact_history = create_phase1_execution_fact_authority(
        run_id=matcher.run_id,
        spec_set=matcher.spec_set,
        order_verifier=_RecoveryOrderVerifier(matcher, orders),
        dispatch_verifier=_UnusedDispatch(matcher),
    )

    with pytest.raises(LifecycleError, match="dispatch frontier"):
        _require_recovery_history_frontier(
            records=(),
            runtime=_empty_runtime_for(matcher),
            matcher=matcher,
            fact_authority=fact_history,
            coordinator=None,
        )


def test_recovery_frontier_rejects_submission_without_durable_authorization() -> None:
    _fixture, matcher, orders, causal, _delayed, _end = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    fact_history = create_phase1_execution_fact_authority(
        run_id=matcher.run_id,
        spec_set=matcher.spec_set,
        order_verifier=_RecoveryOrderVerifier(matcher, orders),
        dispatch_verifier=_UnusedDispatch(matcher),
    )

    with pytest.raises(LifecycleError, match="authorization evidence"):
        _require_recovery_history_frontier(
            records=(),
            runtime=_empty_runtime_for(matcher),
            matcher=matcher,
            fact_authority=fact_history,
            coordinator=None,
        )


def test_recovery_frontier_rejects_fact_history_without_matcher_ingress() -> None:
    _fixture, matcher, _orders, _causal, _delayed, _end = _system()
    future_fact_history = SimpleNamespace(
        ingresses=(object(),),
        outcomes=(object(),),
        fills=(),
        projections=(),
    )

    with pytest.raises(LifecycleError, match="fact recovery history"):
        _require_recovery_history_frontier(
            records=(),
            runtime=_empty_runtime_for(matcher),
            matcher=matcher,
            fact_authority=future_fact_history,  # type: ignore[arg-type]
            coordinator=None,
        )


def test_recovery_composition_consumes_store_prefix_and_injected_histories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixture, matcher, orders, _causal, _delayed, _end = _system()
    root = _root(tmp_path)
    original_store = LocalResultStore(root)
    prepared = original_store.prepare(_spec(), lambda: UUID(matcher.run_id.value))
    manifest = original_store.verify_manifest(prepared.manifest_verification)
    journal = create_posix_audit_journal(prepared.audit)
    journal.close()
    _release_simulated_process_writer(original_store, prepared)

    recovered_store = LocalResultStore(root)
    verified = recovered_store.verify_recovery_attempt(manifest)
    assert isinstance(verified, VerifiedIncompleteRecoveryBinding)
    recovered = recovered_store.recover_incomplete_attempt(verified)
    admitted_journals: list[PosixAuditJournal] = []

    def reopen(prepared: AuditRunBinding) -> PosixAuditJournal:
        value = reopen_posix_audit_journal(prepared)
        admitted_journals.append(value)
        return value

    admitted = admit_recovered_run(recovered, audit_factory=reopen)
    source = create_phase1_historical_market_source_bridge(
        _source(
            _row(
                start="2026-01-02T09:30:00.000000Z",
                end="2026-01-02T09:31:00.000000Z",
                available="2026-01-02T09:31:00.000000Z",
                sequence=0,
            )
        )
    )
    runtime = create_phase1_historical_market_runtime(
        run_id=matcher.run_id,
        spec_set=matcher.spec_set,
        source=source,
    )
    freshness = _UnusedFreshness()
    order_verifier = _RecoveryOrderVerifier(matcher, orders)
    fact_history = create_phase1_execution_fact_authority(
        run_id=matcher.run_id,
        spec_set=matcher.spec_set,
        order_verifier=order_verifier,
        dispatch_verifier=_UnusedDispatch(matcher),
    )

    original_factory = create_dormant_historical_submission_authorization_authority
    original_coordinator_factory = recover_phase1_lifecycle_coordinator
    factory_calls = 0
    coordinator_calls = 0

    def fail_first_factory(**kwargs: Any) -> Any:
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            raise OSError("declared transient reconstruction failure")
        return original_factory(**kwargs)

    def fail_first_coordinator(**kwargs: Any) -> Any:
        nonlocal coordinator_calls
        coordinator_calls += 1
        return original_coordinator_factory(**kwargs)

    monkeypatch.setattr(
        lifecycle_composition,
        "create_dormant_historical_submission_authorization_authority",
        fail_first_factory,
    )
    monkeypatch.setattr(
        lifecycle_composition,
        "recover_phase1_lifecycle_coordinator",
        fail_first_coordinator,
    )
    with pytest.raises(OSError, match="declared transient reconstruction failure"):
        recover_phase1_historical_lifecycle(
            recovery=admitted,
            runtime=runtime,
            matcher_history=matcher,
            fact_history=fact_history,
            order_issuance_verifier=order_verifier,
            risk_policy=_policy_for(matcher),
            global_halt=freshness,
            instrument_gate=freshness,
        )

    journal_path = root / str(UUID(matcher.run_id.value)) / "audit" / "audit-v1.journal"
    journal_bytes = journal_path.read_bytes()
    journal_history = _audit_history_signature(tuple(admitted_journals[0].records))
    matcher_state = matcher.state
    fact_outcomes = fact_history.outcomes
    runtime_trace = runtime.trace_records
    lifecycle = recover_phase1_historical_lifecycle(
        recovery=admitted,
        runtime=runtime,
        matcher_history=matcher,
        fact_history=fact_history,
        order_issuance_verifier=order_verifier,
        risk_policy=_policy_for(matcher),
        global_halt=freshness,
        instrument_gate=freshness,
    )

    assert journal_path.read_bytes() == journal_bytes
    assert _audit_history_signature(tuple(admitted_journals[0].records)) == journal_history
    assert matcher.state == matcher_state
    assert fact_history.outcomes == fact_outcomes
    assert runtime.trace_records == runtime_trace

    assert factory_calls == 2
    assert coordinator_calls == 1

    assert lifecycle.coordinator.state.phase is CoordinatorPhase.ADMITTED
    assert type(lifecycle.matcher) is HistoricalMatcherHistoryView
    assert type(lifecycle.fact_authority) is ExecutionFactHistoryView
    assert lifecycle.matcher.state == matcher.state
    assert not hasattr(lifecycle.matcher, "submit")
    assert not hasattr(lifecycle.matcher, "match_active_market_root")
    assert not hasattr(lifecycle.fact_authority, "process_ingress")
    assert not hasattr(lifecycle.runtime, "pop")
    assert not hasattr(lifecycle.runtime, "acknowledge")
    assert admitted_journals[0].records == tuple(admitted.records)
    recovery_parameters = signature(recover_phase1_historical_lifecycle).parameters
    assert "authorization" not in recovery_parameters
    assert "authorization_capability" not in recovery_parameters
    assert "activation_seal" not in recovery_parameters
    with pytest.raises(TypeError, match="terminal recovery requires exact"):
        recover_phase1_historical_terminal_evidence(
            recovery=object(),  # type: ignore[arg-type]
            runtime=runtime,
            matcher_history=matcher,
            fact_history=fact_history,
            risk_policy=_policy_for(matcher),
        )

    with pytest.raises(TypeError, match="created only by composition"):
        Phase1HistoricalLifecycle(
            _seal=object(),
            coordinator=lifecycle.coordinator,
            matcher=lifecycle.matcher,
            fact_authority=lifecycle.fact_authority,
            runtime=lifecycle.runtime,
        )
    with pytest.raises(TypeError, match="created only by composition"):
        Phase1HistoricalLifecycleCoordinatorFacade(
            object(),  # type: ignore[arg-type]
            seal=object(),
        )
    with pytest.raises(TypeError, match="prepared acknowledgement"):
        create_phase1_historical_lifecycle(
            binding=admitted.binding,
            prepared_acknowledgement=object(),  # type: ignore[arg-type]
            audit=admitted.audit,
            runtime=runtime,
            spec_set=matcher.spec_set,
            execution_policy=matcher.execution_policy,
            source_namespace=matcher.source_namespace,
            provenance_id=matcher.provenance_id,
            order_issuance_verifier=order_verifier,
            risk_policy=_policy_for(matcher),
            global_halt=freshness,
            instrument_gate=freshness,
        )
    with pytest.raises(TypeError, match="exact authoritative carriers"):
        recover_phase1_historical_lifecycle(
            recovery=object(),  # type: ignore[arg-type]
            runtime=runtime,
            matcher_history=matcher,
            fact_history=fact_history,
            order_issuance_verifier=order_verifier,
            risk_policy=_policy_for(matcher),
            global_halt=freshness,
            instrument_gate=freshness,
        )

    seal = run_composition._RECOVERED_ADMISSION_SEAL
    with pytest.raises(RunCompositionError, match="store-issued evidence"):
        run_composition.AdmittedRecoveredRun(
            object(),
            recovered=recovered,
            audit=admitted.audit,
            records=admitted.records,
        )
    with pytest.raises(RunCompositionError, match="one admitted audit port"):
        run_composition.AdmittedRecoveredRun(
            seal,
            recovered=recovered,
            audit=object(),  # type: ignore[arg-type]
            records=admitted.records,
        )
    foreign_binding = RunBinding(
        admitted.binding.reference,
        Sha256Digest("ff" * 32),
    )
    foreign_audit = object.__new__(BoundAuditPort)
    object.__setattr__(foreign_audit, "_binding", foreign_binding)
    object.__setattr__(foreign_audit, "_raw", admitted_journals[0])
    with pytest.raises(RunCompositionError, match="one admitted audit port"):
        run_composition.AdmittedRecoveredRun(
            seal,
            recovered=recovered,
            audit=foreign_audit,
            records=admitted.records,
        )
    object.__setattr__(recovered, "record_count", admitted.records.record_count + 1)
    with pytest.raises(RunCompositionError, match="one admitted audit port"):
        run_composition.AdmittedRecoveredRun(
            seal,
            recovered=recovered,
            audit=admitted.audit,
            records=admitted.records,
        )
    object.__setattr__(recovered, "record_count", admitted.records.record_count)
    foreign_records = SimpleNamespace(
        binding=foreign_binding,
        record_count=admitted.records.record_count,
    )
    with pytest.raises(RunCompositionError, match="one admitted audit port"):
        run_composition.AdmittedRecoveredRun(
            seal,
            recovered=recovered,
            audit=admitted.audit,
            records=foreign_records,
        )
    probe = run_composition.AdmittedRecoveredRun(
        seal,
        recovered=recovered,
        audit=admitted.audit,
        records=admitted.records,
    )
    with pytest.raises(AttributeError, match="immutable"):
        probe.records = ()  # type: ignore[assignment]
    probe._reserve_consumption()
    with pytest.raises(RunCompositionError, match="reservation changed"):
        probe._commit_consumption(object())
    abort_probe = run_composition.AdmittedRecoveredRun(
        seal,
        recovered=recovered,
        audit=admitted.audit,
        records=admitted.records,
    )
    abort_probe._reserve_consumption()
    with pytest.raises(RunCompositionError, match="reservation changed"):
        abort_probe._abort_consumption(object())
    with pytest.raises(RunCompositionError, match="already consumed"):
        admitted._consume()
    admitted_journals[0].close()
    record = recovered_store._record_for(recovered._authority)
    os.close(record.writer_lock_fd)
