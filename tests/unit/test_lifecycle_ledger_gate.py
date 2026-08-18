from __future__ import annotations

import json
from typing import Any

import pytest

from ea.core import (
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
    run_id = matcher.run_id
    spec_set = matcher.spec_set
    ledger = create_portfolio_ledger(run_id, spec_set)
    policy = _risk_policy(spec_set)
    return {
        "ledger_handoff_authority": create_phase1_ledger_handoff_authority(
            run_id, spec_set, ledger
        ),
        "risk_authority": create_phase1_risk_authority(
            run_id=run_id,
            spec_set=spec_set,
            execution_policy=EXECUTION_POLICY,
            policy=policy,
        ),
        "risk_refresh_authority": create_phase1_portfolio_risk_refresh_authority(
            run_id=run_id,
            spec_set=spec_set,
            policy_id=RISK_POLICY_ID,
            policy_sha256=phase1_risk_policy_digest(policy),
            first_sequence=8,
            first_previous_refresh_sha256=Sha256Digest("aa" * 32),
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
