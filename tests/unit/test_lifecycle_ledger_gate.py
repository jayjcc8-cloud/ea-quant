from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from ea.core import (
    AuditRecord,
    AuditRecordKind,
    EconomicId,
    EconomicOwnerKind,
    ExecutionFactAction,
    ExecutionPolicyId,
    ExecutionPolicyRef,
    InstrumentExecutionSpecSet,
    InstrumentRiskLimit,
    OrderResolutionKeyKind,
    Phase1RiskPolicy,
    RiskPolicyId,
    RunBinding,
    RunReference,
    Sha256Digest,
    create_execution_fact_processing_outcome,
    create_fill,
    create_order_resolution_binding,
    create_phase1_risk_policy,
    phase1_risk_policy_digest,
)
from ea.core.execution_messages import Fill
from ea.core.execution_state import ExecutionFactProcessingOutcome
from ea.core.market_data import MarketDataEnvelope
from ea.execution.matcher import Phase1HistoricalMatcher
from ea.portfolio import (
    create_phase1_ledger_handoff_authority,
    create_phase1_portfolio_risk_refresh_authority,
    create_portfolio_ledger,
)
from ea.risk import create_phase1_risk_authority
from unit.test_historical_matcher import _system
from unit.test_lifecycle_coordinator import (
    _MemoryAudit,
    _RetainedOutcomeFacts,
    _Runtime,
)
from unit.test_lifecycle_coordinator import (
    create_phase1_lifecycle_coordinator as _build_coordinator,
)

EXECUTION_POLICY = ExecutionPolicyRef(
    ExecutionPolicyId("phase1.execution.v1"),
    Sha256Digest("1" * 64),
)
RISK_POLICY_ID = RiskPolicyId("phase1.risk.v1")


class _FixedFillEvidence:
    def __init__(self, fill: object) -> None:
        self._fill = fill

    def resolve_fill(self, *, fill_id: Any, fill_sha256: Any) -> object:
        return self._fill

    def resolve_projection_after(self, **_: Any) -> None:
        return None


def _risk_policy(spec_set: InstrumentExecutionSpecSet) -> Phase1RiskPolicy:
    from ea.core import CanonicalDecimal

    instrument = spec_set.specifications[0].instrument
    return create_phase1_risk_policy(
        policy_id=RISK_POLICY_ID,
        spec_set=spec_set,
        execution_policy=EXECUTION_POLICY,
        instrument_limits=(
            InstrumentRiskLimit(
                instrument=instrument,
                maximum_order_quantity=CanonicalDecimal("1000"),
                maximum_absolute_position=CanonicalDecimal("1000"),
            ),
        ),
    )


def _ledger_ports(matcher: Phase1HistoricalMatcher) -> dict[str, Any]:
    from ea.composition import create_acknowledged_lifecycle_frontier

    run_id = matcher.run_id
    spec_set = matcher.spec_set
    ledger = create_portfolio_ledger(run_id, spec_set)
    policy = _risk_policy(spec_set)
    risk_authority = create_phase1_risk_authority(
        run_id=run_id,
        spec_set=spec_set,
        execution_policy=EXECUTION_POLICY,
        policy=policy,
    )
    return {
        "ledger_handoff_authority": create_phase1_ledger_handoff_authority(
            run_id, spec_set, ledger
        ),
        "risk_authority": risk_authority,
        "risk_refresh_authority": create_phase1_portfolio_risk_refresh_authority(
            run_id=run_id,
            spec_set=spec_set,
            policy_id=RISK_POLICY_ID,
            policy_sha256=phase1_risk_policy_digest(policy),
            first_sequence=8,
            first_previous_refresh_sha256=Sha256Digest("aa" * 32),
        ),
        "frontier": create_acknowledged_lifecycle_frontier(
            initial_snapshot=ledger.snapshot,
            initial_risk_state=risk_authority.risk_state,
        ),
    }


def _outcome_bundle(
    matcher: Phase1HistoricalMatcher, delayed: MarketDataEnvelope
) -> tuple[Fill, ExecutionFactProcessingOutcome]:
    batch = matcher.match_active_market_root(delayed, dispatch_sequence=8)
    ingress = batch.ingresses[0]
    fact = ingress.fact
    key_kinds = tuple(
        kind
        for present, kind in (
            (fact.order_id is not None, OrderResolutionKeyKind.ORDER_ID),
            (fact.client_submission_key is not None, OrderResolutionKeyKind.CLIENT_SUBMISSION_KEY),
            (fact.venue_order_id is not None, OrderResolutionKeyKind.VENUE_ORDER_ID),
        )
        if present
    )
    fill = create_fill(
        fill_id=EconomicId(matcher.run_id, EconomicOwnerKind.EXECUTION_FILL, 1),
        fact=fact,
        spec_set=matcher.spec_set,
    )
    outcome = create_execution_fact_processing_outcome(
        run_id=matcher.run_id,
        runtime_dispatch_sequence=8,
        ingress=ingress,
        action=ExecutionFactAction.ACCEPTED,
        anomalies=(),
        order_resolutions=tuple(
            create_order_resolution_binding(key_kind=kind, resolved_order=None)
            for kind in key_kinds
        ),
        resolved_order=None,
        fill=fill,
        projection_before=None,
        projection_after=None,
    )
    return fill, outcome


def _coordinator_with_gate(
    matcher: Phase1HistoricalMatcher,
    delayed: MarketDataEnvelope,
    *,
    fill: Fill | None,
    outcome: ExecutionFactProcessingOutcome,
) -> tuple[Any, _MemoryAudit]:
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _MemoryAudit(binding)
    coordinator = _build_coordinator(
        binding=binding,
        audit=audit,
        runtime=_Runtime(matcher, delayed, dispatch_sequence=8),
        matcher=matcher,
        fact_authority=_RetainedOutcomeFacts(matcher, outcome),
        evidence_resolver=_FixedFillEvidence(fill),
        **_ledger_ports(matcher),
    )
    return coordinator, audit


def test_ledger_gate_commits_outcomes_refresh_and_completion_v3() -> None:
    _fixture, matcher, orders, causal, delayed, _end = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    fill, outcome = _outcome_bundle(matcher, delayed)
    coordinator, audit = _coordinator_with_gate(matcher, delayed, fill=fill, outcome=outcome)

    window = coordinator.begin_next_dispatch()
    assert window is not None

    result = coordinator.complete_active_dispatch(window)
    assert result.runtime_acknowledged is True

    kinds = [record.record_kind for record in audit.records]
    assert kinds.count(AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME) == 1
    assert kinds.count(AuditRecordKind.RISK_PORTFOLIO_REFRESH) == 1
    completion_record = next(
        record
        for record in audit.records
        if record.record_kind is AuditRecordKind.RUNTIME_DISPATCH_COMPLETED
    )
    document = json.loads(completion_record.canonical_payload)
    assert document["schema"] == "ea.audit-dispatch-completed.v3"
    assert document["ledger_outcome_count"] == 1
    assert document["final_portfolio_snapshot_sha256"] != document["final_risk_state_sha256"]


def test_ledger_gate_halts_on_reconciliation_outcome() -> None:
    from ea.core import ExecutionFactAnomaly

    _fixture, matcher, orders, causal, delayed, _end = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    batch = matcher.match_active_market_root(delayed, dispatch_sequence=8)
    ingress = batch.ingresses[0]
    fact = ingress.fact
    key_kinds = tuple(
        kind
        for present, kind in (
            (fact.order_id is not None, OrderResolutionKeyKind.ORDER_ID),
            (fact.client_submission_key is not None, OrderResolutionKeyKind.CLIENT_SUBMISSION_KEY),
            (fact.venue_order_id is not None, OrderResolutionKeyKind.VENUE_ORDER_ID),
        )
        if present
    )
    fill = create_fill(
        fill_id=EconomicId(matcher.run_id, EconomicOwnerKind.EXECUTION_FILL, 1),
        fact=fact,
        spec_set=matcher.spec_set,
    )
    outcome = create_execution_fact_processing_outcome(
        run_id=matcher.run_id,
        runtime_dispatch_sequence=8,
        ingress=ingress,
        action=ExecutionFactAction.UNRESOLVED,
        anomalies=(ExecutionFactAnomaly.UNKNOWN_ORDER,),
        order_resolutions=tuple(
            create_order_resolution_binding(key_kind=kind, resolved_order=None)
            for kind in key_kinds
        ),
        resolved_order=None,
        fill=fill,
        projection_before=None,
        projection_after=None,
    )
    coordinator, audit = _coordinator_with_gate(matcher, delayed, fill=fill, outcome=outcome)

    window = coordinator.begin_next_dispatch()
    assert window is not None
    result = coordinator.complete_active_dispatch(window)
    assert result.runtime_acknowledged is True

    refresh_record = next(
        record
        for record in audit.records
        if record.record_kind is AuditRecordKind.RISK_PORTFOLIO_REFRESH
    )
    refresh_document = json.loads(refresh_record.canonical_payload)
    assert refresh_document["submission_permitted"] is False


def test_gate_factory_rejects_mismatched_authority_bindings() -> None:
    _fixture, matcher, _orders, _causal, delayed, _end = _system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _MemoryAudit(binding)
    ports = _ledger_ports(matcher)
    ports["risk_refresh_authority"] = create_phase1_portfolio_risk_refresh_authority(
        run_id=matcher.run_id,
        spec_set=matcher.spec_set,
        policy_id=RISK_POLICY_ID,
        policy_sha256=Sha256Digest("9" * 64),
    )
    from ea.core.lifecycle import LifecycleError

    with pytest.raises(LifecycleError, match="policy binding"):
        _build_coordinator(
            binding=binding,
            audit=audit,
            runtime=_Runtime(matcher, delayed),
            matcher=matcher,
            fact_authority=_RetainedOutcomeFacts(matcher, None),  # type: ignore[arg-type]
            evidence_resolver=_FixedFillEvidence(None),
            **ports,
        )


class _FailOnceRefreshAudit(_MemoryAudit):
    def __init__(self, binding: RunBinding) -> None:
        super().__init__(binding)
        self.refresh_failed = False

    def append(
        self, *, record_kind: Any, subject_kind: Any, subject_sha256: Any, canonical_payload: Any
    ) -> Any:
        if record_kind is AuditRecordKind.RISK_PORTFOLIO_REFRESH and not self.refresh_failed:
            self.refresh_failed = True
            raise __import__("ea.core.audit", fromlist=["AuditContractError"]).AuditContractError(
                __import__(
                    "ea.core", fromlist=["OutcomeCode"]
                ).OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                "injected refresh append failure",
            )
        return super().append(
            record_kind=record_kind,
            subject_kind=subject_kind,
            subject_sha256=subject_sha256,
            canonical_payload=canonical_payload,
        )


def test_gate_retry_after_refresh_append_failure_resolves_the_same_frontier() -> None:
    _fixture, matcher, orders, causal, delayed, _end = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    fill, outcome = _outcome_bundle(matcher, delayed)
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _FailOnceRefreshAudit(binding)
    coordinator = _build_coordinator(
        binding=binding,
        audit=audit,
        runtime=_Runtime(matcher, delayed, dispatch_sequence=8),
        matcher=matcher,
        fact_authority=_RetainedOutcomeFacts(matcher, outcome),
        evidence_resolver=_FixedFillEvidence(fill),
        **_ledger_ports(matcher),
    )
    with pytest.raises(Exception, match="injected refresh"):
        coordinator.process_next_dispatch()

    from ea.core.lifecycle import CoordinatorPhase

    assert coordinator.state.phase is CoordinatorPhase.FAILING
    retry = coordinator.retry_active_dispatch()
    assert retry.runtime_acknowledged is True
    kinds = [record.record_kind for record in audit.records]
    assert kinds.count(AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME) == 1
    assert kinds.count(AuditRecordKind.RISK_PORTFOLIO_REFRESH) == 1
    completion_record = next(
        record
        for record in audit.records
        if record.record_kind is AuditRecordKind.RUNTIME_DISPATCH_COMPLETED
    )
    document = json.loads(completion_record.canonical_payload)
    assert document["schema"] == "ea.audit-dispatch-completed.v3"


class _FailOnceLedgerOutcomeAudit(_MemoryAudit):
    def __init__(self, binding: RunBinding) -> None:
        super().__init__(binding)
        self.ledger_failed = False

    def append(
        self, *, record_kind: Any, subject_kind: Any, subject_sha256: Any, canonical_payload: Any
    ) -> Any:
        if (
            record_kind is AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME
            and not self.ledger_failed
        ):
            self.ledger_failed = True
            raise __import__("ea.core.audit", fromlist=["AuditContractError"]).AuditContractError(
                __import__(
                    "ea.core", fromlist=["OutcomeCode"]
                ).OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                "injected ledger outcome append failure",
            )
        return super().append(
            record_kind=record_kind,
            subject_kind=subject_kind,
            subject_sha256=subject_sha256,
            canonical_payload=canonical_payload,
        )


def test_ledger_failure_after_mutation_retries_only_the_same_record() -> None:
    _fixture, matcher, orders, causal, delayed, _end = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    fill, outcome = _outcome_bundle(matcher, delayed)
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _FailOnceLedgerOutcomeAudit(binding)
    ports = _ledger_ports(matcher)
    ledger = ports["ledger_handoff_authority"]._state.ledger
    coordinator = _build_coordinator(
        binding=binding,
        audit=audit,
        runtime=_Runtime(matcher, delayed, dispatch_sequence=8),
        matcher=matcher,
        fact_authority=_RetainedOutcomeFacts(matcher, outcome),
        evidence_resolver=_FixedFillEvidence(fill),
        **ports,
    )
    with pytest.raises(Exception, match="injected ledger"):
        coordinator.process_next_dispatch()

    from ea.core.lifecycle import CoordinatorPhase

    assert coordinator.state.phase is CoordinatorPhase.FAILING
    assert ledger.snapshot.snapshot_version == 1
    retry = coordinator.retry_active_dispatch()
    assert retry.runtime_acknowledged is True
    # The economic mutation was never duplicated: exactly one transaction.
    assert len(ledger.transactions) == 1
    kinds = [record.record_kind for record in audit.records]
    assert kinds.count(AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME) == 1


def _record_entry(
    records: Sequence[AuditRecord], kind: AuditRecordKind
) -> tuple[int, AuditRecord, Any]:
    for position, record in enumerate(records, start=2):
        if record.record_kind is kind:
            return (
                position,
                record,
                __import__(
                    "ea.core", fromlist=["create_audit_append_acknowledgement"]
                ).create_audit_append_acknowledgement(record),
            )
    raise AssertionError("record kind not found")


def test_recover_ledger_frontier_replays_with_byte_equality() -> None:
    from ea.runtime.coordinator import _recover_ledger_frontier, _RecoveredDispatch

    _fixture, matcher, orders, causal, delayed, _end = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    fill, outcome = _outcome_bundle(matcher, delayed)
    coordinator, audit = _coordinator_with_gate(matcher, delayed, fill=fill, outcome=outcome)
    window = coordinator.begin_next_dispatch()
    assert window is not None
    active = coordinator._active
    assert active is not None
    assert all(value is not None for value in active.ledger_acks)

    ledger_entries: list[Any] = [
        _record_entry(audit.records, AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME)
    ]
    refresh_entry = _record_entry(audit.records, AuditRecordKind.RISK_PORTFOLIO_REFRESH)
    recovered = _RecoveredDispatch(
        sequence=8,
        trigger_sha256=active.trigger_sha256,
        ledger_records=ledger_entries,
        refresh_record=refresh_entry,
    )

    fresh_coordinator, _fresh_audit = _coordinator_with_gate(
        matcher, delayed, fill=fill, outcome=outcome
    )
    _recover_ledger_frontier(fresh_coordinator, active, recovered)
    assert all(value is not None for value in active.ledger_acks)
    assert active.refresh_ack is not None
    assert active.final_portfolio_snapshot_sha256 is not None
    assert active.final_risk_state_sha256 is not None


def test_recover_ledger_frontier_rejects_corrupted_payload() -> None:
    from ea.core.lifecycle import LifecycleError
    from ea.runtime.coordinator import _recover_ledger_frontier, _RecoveredDispatch

    _fixture, matcher, orders, causal, delayed, _end = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    fill, outcome = _outcome_bundle(matcher, delayed)
    coordinator, audit = _coordinator_with_gate(matcher, delayed, fill=fill, outcome=outcome)
    window = coordinator.begin_next_dispatch()
    assert window is not None
    active = coordinator._active
    assert active is not None

    position, record, acknowledgement = _record_entry(
        audit.records, AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME
    )
    corrupted = object.__new__(type(record))
    for field in (
        "binding",
        "record_id",
        "record_kind",
        "subject_kind",
        "subject_sha256",
        "canonical_payload",
        "payload_sha256",
        "previous_record_sha256",
        "previous_chain_head_sha256",
        "_seal",
    ):
        object.__setattr__(corrupted, field, getattr(record, field))
    object.__setattr__(corrupted, "canonical_payload", record.canonical_payload + b" ")
    recovered = _RecoveredDispatch(
        sequence=8,
        trigger_sha256=active.trigger_sha256,
        ledger_records=[(position, corrupted, acknowledgement)],
        refresh_record=None,
    )
    fresh_coordinator, _fresh_audit = _coordinator_with_gate(
        matcher, delayed, fill=fill, outcome=outcome
    )
    with pytest.raises(LifecycleError, match="ledger outcome payload conflicts"):
        _recover_ledger_frontier(fresh_coordinator, active, recovered)


def test_terminal_payload_emits_v2_when_gate_bound() -> None:
    from ea.core import CoordinatorTerminalKind
    from ea.core.lifecycle import create_pre_terminal_coordinator_state
    from ea.runtime.coordinator import _terminal_payload

    _fixture, matcher, orders, causal, delayed, _end = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    fill, outcome = _outcome_bundle(matcher, delayed)
    coordinator, _audit = _coordinator_with_gate(matcher, delayed, fill=fill, outcome=outcome)
    coordinator._final_refresh_value_sha256 = Sha256Digest("ee" * 32)
    state = create_pre_terminal_coordinator_state(
        binding=coordinator._binding,
        state_version=4,
        terminal_kind=CoordinatorTerminalKind.SUCCESS,
        last_dispatch_sequence=8,
        last_trigger_root_sha256=Sha256Digest("33" * 32),
        dispatch_completion_ack_sha256=Sha256Digest("44" * 32),
        previous_chain_head_sha256=Sha256Digest("44" * 32),
        failure_code=None,
    )
    payload = _terminal_payload(coordinator, state)
    document = json.loads(payload)
    assert document["schema"] == "ea.audit-run-terminal.v2"
    assert document["final_risk_refresh_sha256"] == "ee" * 32
    assert document["open_reconciliation_ref_aggregate_sha256"]


def test_terminal_payload_requires_final_refresh_when_gate_bound() -> None:
    from ea.core import CoordinatorTerminalKind
    from ea.core.lifecycle import LifecycleError, create_pre_terminal_coordinator_state
    from ea.runtime.coordinator import _terminal_payload

    _fixture, matcher, orders, causal, delayed, _end = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    fill, outcome = _outcome_bundle(matcher, delayed)
    coordinator, _audit = _coordinator_with_gate(matcher, delayed, fill=fill, outcome=outcome)
    state = create_pre_terminal_coordinator_state(
        binding=coordinator._binding,
        state_version=4,
        terminal_kind=CoordinatorTerminalKind.SUCCESS,
        last_dispatch_sequence=8,
        last_trigger_root_sha256=Sha256Digest("33" * 32),
        dispatch_completion_ack_sha256=Sha256Digest("44" * 32),
        previous_chain_head_sha256=Sha256Digest("44" * 32),
        failure_code=None,
    )
    with pytest.raises(LifecycleError, match="final risk refresh"):
        _terminal_payload(coordinator, state)


def test_gate_binding_rejects_run_and_specification_conflicts() -> None:
    from ea.core.lifecycle import LifecycleError

    _fixture, matcher, _orders, _causal, delayed, _end = _system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _MemoryAudit(binding)
    ports = _ledger_ports(matcher)
    from ea.core import RunId

    other_run = RunId("87654321-4321-4321-8321-cba987654321")
    foreign_ledger = create_portfolio_ledger(other_run, matcher.spec_set)
    ports["ledger_handoff_authority"] = create_phase1_ledger_handoff_authority(
        other_run, matcher.spec_set, foreign_ledger
    )
    with pytest.raises(LifecycleError, match="runs conflict"):
        _build_coordinator(
            binding=binding,
            audit=audit,
            runtime=_Runtime(matcher, delayed),
            matcher=matcher,
            fact_authority=_RetainedOutcomeFacts(matcher, None),  # type: ignore[arg-type]
            evidence_resolver=_FixedFillEvidence(None),
            **ports,
        )


def test_no_fill_frontier_does_not_halt_and_permits_submission() -> None:
    # RUNTIME-002 regression: a not_applicable handoff is not a failure.
    from ea.core import ExecutionFactAction

    _fixture, matcher, orders, causal, delayed, _end = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    batch = matcher.match_active_market_root(delayed, dispatch_sequence=8)
    ingress = batch.ingresses[0]
    fact = ingress.fact
    key_kinds = tuple(
        kind
        for present, kind in (
            (fact.order_id is not None, OrderResolutionKeyKind.ORDER_ID),
            (fact.client_submission_key is not None, OrderResolutionKeyKind.CLIENT_SUBMISSION_KEY),
            (fact.venue_order_id is not None, OrderResolutionKeyKind.VENUE_ORDER_ID),
        )
        if present
    )
    outcome = create_execution_fact_processing_outcome(
        run_id=matcher.run_id,
        runtime_dispatch_sequence=8,
        ingress=ingress,
        action=ExecutionFactAction.DUPLICATE,
        anomalies=(),
        order_resolutions=(),
        resolved_order=None,
        fill=None,
        projection_before=None,
        projection_after=None,
    )
    coordinator, audit = _coordinator_with_gate(matcher, delayed, fill=None, outcome=outcome)

    window = coordinator.begin_next_dispatch()
    assert window is not None
    result = coordinator.complete_active_dispatch(window)
    assert result.runtime_acknowledged is True

    refresh_record = next(
        record
        for record in audit.records
        if record.record_kind is AuditRecordKind.RISK_PORTFOLIO_REFRESH
    )
    refresh_document = json.loads(refresh_record.canonical_payload)
    assert refresh_document["submission_permitted"] is True
    ledger_record = next(
        record
        for record in audit.records
        if record.record_kind is AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME
    )
    ledger_document = json.loads(ledger_record.canonical_payload)
    assert ledger_document["action"] == "not_applicable"


def test_refresh_seeding_rejects_invalid_predecessor_combinations() -> None:
    from ea.portfolio.risk_refresh_authority import PortfolioRiskRefreshAuthorityError

    _fixture, matcher, _orders, _causal, _delayed, _end = _system()
    spec_set = matcher.spec_set
    with pytest.raises(PortfolioRiskRefreshAuthorityError, match="predecessor"):
        create_phase1_portfolio_risk_refresh_authority(
            run_id=matcher.run_id,
            spec_set=spec_set,
            policy_id=RISK_POLICY_ID,
            policy_sha256=Sha256Digest("1" * 64),
            first_sequence=1,
            first_previous_refresh_sha256=Sha256Digest("aa" * 32),
        )
    with pytest.raises(PortfolioRiskRefreshAuthorityError, match="predecessor"):
        create_phase1_portfolio_risk_refresh_authority(
            run_id=matcher.run_id,
            spec_set=spec_set,
            policy_id=RISK_POLICY_ID,
            policy_sha256=Sha256Digest("1" * 64),
            first_sequence=8,
        )
