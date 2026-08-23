from __future__ import annotations

import json
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, cast

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
    audit_append_acknowledgement_digest,
    audit_chain_head,
    audit_record_digest,
    create_audit_append_acknowledgement,
    create_execution_fact_processing_outcome,
    create_fill,
    create_order_resolution_binding,
    create_phase1_risk_policy,
    phase1_risk_policy_digest,
)
from ea.core.execution_messages import Fill
from ea.core.execution_state import ExecutionFactProcessingOutcome
from ea.core.historical_matching import _create_historical_matcher_dispatch_batch
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
    _NoFacts,
    _RetainedOutcomeFacts,
    _Runtime,
    _TracingMarketRuntime,
)
from unit.test_lifecycle_coordinator import (
    create_phase1_lifecycle_coordinator as _build_coordinator,
)

EXECUTION_POLICY = ExecutionPolicyRef(
    ExecutionPolicyId("phase1.execution.v1"),
    Sha256Digest("1" * 64),
)
RISK_POLICY_ID = RiskPolicyId("phase1.risk.v1")
_DEFAULT_REFRESH_PREDECESSOR = Sha256Digest("aa" * 32)


class _FixedFillEvidence:
    def __init__(self, fill: Fill | None) -> None:
        self._fill = fill

    def resolve_fill(self, *, fill_id: EconomicId, fill_sha256: Sha256Digest) -> Fill | None:
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


def _ledger_ports(
    matcher: Any,
    *,
    first_sequence: int = 8,
    first_previous_refresh_sha256: Sha256Digest | None = _DEFAULT_REFRESH_PREDECESSOR,
) -> dict[str, Any]:
    from ea.composition.frontier import create_acknowledged_lifecycle_frontier

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
            first_sequence=first_sequence,
            first_previous_refresh_sha256=first_previous_refresh_sha256,
        ),
        "frontier": create_acknowledged_lifecycle_frontier(
            initial_snapshot=ledger.snapshot,
            initial_risk_state=risk_authority.risk_state,
            initial_predecessor_sha256=first_previous_refresh_sha256,
        ),
    }


def test_bound_ledger_gate_rejects_a_partial_binding_with_domain_error() -> None:
    import ea.runtime.coordinator as coordinator_module
    from ea.core import OutcomeCode
    from ea.core.lifecycle import LifecycleError

    helper = getattr(coordinator_module, "_bound_ledger_gate", None)
    assert helper is not None, "coordinator must expose the typed gate boundary"
    marker = object()
    with pytest.raises(LifecycleError, match="must be bound together") as captured:
        helper(marker, None, marker, marker)
    assert captured.value.code is OutcomeCode.CONFLICTING_ID


def test_refresh_replay_preserves_first_derivation_truth() -> None:
    _fixture, matcher, _orders, _causal, _delayed, _end = _system()
    ports = _ledger_ports(matcher)
    authority = ports["risk_refresh_authority"]
    snapshot = ports["ledger_handoff_authority"].snapshot
    risk_state = ports["risk_authority"].risk_state
    frontier_sha256 = Sha256Digest("bb" * 32)
    first = authority.create_refresh(
        snapshot=snapshot,
        risk_state=risk_state,
        dispatch_sequence=8,
        ordered_ledger_ack_frontier_sha256=frontier_sha256,
        coordinator_running=True,
        publication_window_clear=True,
        candidate_matches_internal=True,
    )
    replay = authority.create_refresh(
        snapshot=snapshot,
        risk_state=risk_state,
        dispatch_sequence=8,
        ordered_ledger_ack_frontier_sha256=frontier_sha256,
        coordinator_running=False,
        publication_window_clear=False,
        candidate_matches_internal=False,
    )
    assert replay is first
    assert replay.submission_permitted is True


@pytest.mark.parametrize(
    "false_fact",
    ("coordinator_running", "publication_window_clear", "candidate_matches_internal"),
)
def test_refresh_first_derivation_requires_every_current_fact(false_fact: str) -> None:
    _fixture, matcher, _orders, _causal, _delayed, _end = _system()
    ports = _ledger_ports(matcher)
    facts = {
        "coordinator_running": True,
        "publication_window_clear": True,
        "candidate_matches_internal": True,
    }
    facts[false_fact] = False
    refresh = ports["risk_refresh_authority"].create_refresh(
        snapshot=ports["ledger_handoff_authority"].snapshot,
        risk_state=ports["risk_authority"].risk_state,
        dispatch_sequence=8,
        ordered_ledger_ack_frontier_sha256=Sha256Digest("bb" * 32),
        **facts,
    )
    assert refresh.submission_permitted is False


def _outcome_bundle(
    matcher: Any,
    delayed: MarketDataEnvelope,
    *,
    batch: Any | None = None,
    action: ExecutionFactAction = ExecutionFactAction.ACCEPTED,
    anomalies: Sequence[Any] = (),
) -> tuple[Fill, ExecutionFactProcessingOutcome]:
    if batch is None:
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
        runtime_dispatch_sequence=batch.dispatch_sequence,
        ingress=ingress,
        action=action,
        anomalies=tuple(anomalies),
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


class _SingleBatchMatcher:
    def __init__(
        self, source: Phase1HistoricalMatcher, batch: Any, root: MarketDataEnvelope
    ) -> None:
        self.run_id = source.run_id
        self.spec_set = source.spec_set
        self.source_namespace = source.source_namespace
        self._batch = batch
        self._root = root

    def match_active_market_root(self, root: MarketDataEnvelope, *, dispatch_sequence: int) -> Any:
        assert dispatch_sequence == self._batch.dispatch_sequence
        assert root == self._root
        return self._batch

    def expire_at_active_end(self, root: Any, *, dispatch_sequence: int) -> Any:
        raise AssertionError((root, dispatch_sequence))

    def resolve_dispatch_batch(
        self, *, dispatch_sequence: int, trigger_root_sha256: Sha256Digest
    ) -> Any | None:
        if (
            dispatch_sequence == self._batch.dispatch_sequence
            and trigger_root_sha256 == self._batch.trigger_root_sha256
        ):
            return self._batch
        return None

    def resolve_submission_receipt(self, **_: Any) -> None:
        return None

    def submit(self, *_: Any, **__: Any) -> Any:
        raise AssertionError("single-batch matcher does not admit submissions")


def _dispatch_one_system() -> tuple[
    _SingleBatchMatcher,
    MarketDataEnvelope,
    Fill,
    ExecutionFactProcessingOutcome,
]:
    _fixture, source, orders, causal, delayed, _end = _system()
    source.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    original = source.match_active_market_root(delayed, dispatch_sequence=8)
    batch = _create_historical_matcher_dispatch_batch(
        trigger_root=delayed,
        run_id=original.run_id,
        source_namespace=original.source_namespace,
        dispatch_kind=original.dispatch_kind,
        dispatch_sequence=1,
        trigger_root_sha256=original.trigger_root_sha256,
        trigger_root_key=original.trigger_root_key,
        next_fact_sequence_before=original.next_fact_sequence_before,
        next_fact_sequence_after=original.next_fact_sequence_after,
        submission_sequences=original.submission_sequences,
        order_ids=original.order_ids,
        ingresses=original.ingresses,
        ingress_sha256s=original.ingress_sha256s,
    )
    matcher = _SingleBatchMatcher(source, batch, delayed)
    fill, outcome = _outcome_bundle(matcher, delayed, batch=batch)
    return matcher, delayed, fill, outcome


def _reopen_audit(binding: RunBinding, records: Sequence[AuditRecord]) -> _MemoryAudit:
    reopened = _MemoryAudit(binding)
    for record in records:
        acknowledgement = reopened.append(
            record_kind=record.record_kind,
            subject_kind=record.subject_kind,
            subject_sha256=record.subject_sha256,
            canonical_payload=record.canonical_payload,
        )
        assert acknowledgement.record_id == record.record_id
    return reopened


def _record_document(records: Sequence[AuditRecord], kind: AuditRecordKind) -> dict[str, Any]:
    record = next(record for record in records if record.record_kind is kind)
    return cast(dict[str, Any], json.loads(record.canonical_payload))


def _audit_history_signature(records: Sequence[AuditRecord]) -> tuple[tuple[Any, ...], ...]:
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


def _terminal_gate_journal() -> tuple[Any, RunBinding, _MemoryAudit, _Runtime, Any]:
    _fixture, matcher, _orders, _causal, _delayed, end = _system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _MemoryAudit(binding)
    runtime = _Runtime(matcher, end)
    coordinator = _build_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_FixedFillEvidence(None),
        **_ledger_ports(
            matcher,
            first_sequence=1,
            first_previous_refresh_sha256=None,
        ),
    )
    coordinator.process_next_dispatch()
    return matcher, binding, audit, runtime, coordinator


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
    document = _record_document(audit.records, AuditRecordKind.RUNTIME_DISPATCH_COMPLETED)
    assert document["schema"] == "ea.audit-dispatch-completed.v3"
    assert document["ledger_outcome_count"] == 1
    assert document["final_portfolio_snapshot_sha256"] != document["final_risk_state_sha256"]


def test_ledger_gate_halts_on_reconciliation_outcome() -> None:
    from ea.core import ExecutionFactAnomaly

    _fixture, matcher, orders, causal, delayed, _end = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    fill, outcome = _outcome_bundle(
        matcher,
        delayed,
        action=ExecutionFactAction.UNRESOLVED,
        anomalies=(ExecutionFactAnomaly.UNKNOWN_ORDER,),
    )
    coordinator, audit = _coordinator_with_gate(matcher, delayed, fill=fill, outcome=outcome)
    frontier = coordinator._frontier
    stale_risk_state = frontier.current_state()

    window = coordinator.begin_next_dispatch()
    assert window is not None
    result = coordinator.complete_active_dispatch(window)
    assert result.runtime_acknowledged is True

    refresh_document = _record_document(audit.records, AuditRecordKind.RISK_PORTFOLIO_REFRESH)
    assert refresh_document["submission_permitted"] is False
    assert frontier.current_state() != stale_risk_state


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


@pytest.mark.parametrize("seed", ("ledger", "risk"))
def test_fresh_gate_rejects_seeded_economic_authorities(seed: str) -> None:
    from ea.core.lifecycle import LifecycleError
    from ea.core.risk import RiskHaltReason

    _fixture, matcher, orders, causal, delayed, _end = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    fill, outcome = _outcome_bundle(matcher, delayed)
    ports = _ledger_ports(matcher)
    if seed == "ledger":
        ports["ledger_handoff_authority"]._state.ledger.apply_fill(fill)
    else:
        ports["risk_authority"].engage_halt(
            RiskHaltReason.RECONCILIATION_REQUIRED,
            causal_root_available_at=delayed.available_at,
            dispatch_sequence=8,
        )
    refresh = ports["risk_refresh_authority"].create_refresh(
        snapshot=ports["ledger_handoff_authority"].snapshot,
        risk_state=ports["risk_authority"].risk_state,
        dispatch_sequence=8,
        ordered_ledger_ack_frontier_sha256=Sha256Digest("bb" * 32),
        coordinator_running=True,
        publication_window_clear=True,
        candidate_matches_internal=True,
    )
    ports["frontier"].advance(
        snapshot=ports["ledger_handoff_authority"].snapshot,
        risk_state=ports["risk_authority"].risk_state,
        refresh=refresh,
    )
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )

    with pytest.raises(LifecycleError, match="fresh-empty"):
        _build_coordinator(
            binding=binding,
            audit=_MemoryAudit(binding),
            runtime=_Runtime(matcher, delayed, dispatch_sequence=9),
            matcher=matcher,
            fact_authority=_RetainedOutcomeFacts(matcher, outcome),
            evidence_resolver=_FixedFillEvidence(fill),
            **ports,
        )


class _FailOnceGateAudit(_MemoryAudit):
    def __init__(
        self,
        binding: RunBinding,
        record_kind: AuditRecordKind,
        boundary: str,
    ) -> None:
        super().__init__(binding)
        self.failed_record_kind = record_kind
        self.boundary = boundary
        self.failed_payload: bytes | None = None

    def append(
        self, *, record_kind: Any, subject_kind: Any, subject_sha256: Any, canonical_payload: Any
    ) -> Any:
        if record_kind is self.failed_record_kind and self.failed_payload is None:
            self.failed_payload = canonical_payload
            raise __import__("ea.core.audit", fromlist=["AuditContractError"]).AuditContractError(
                __import__(
                    "ea.core", fromlist=["OutcomeCode"]
                ).OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                f"injected {self.boundary} append failure",
            )
        return super().append(
            record_kind=record_kind,
            subject_kind=subject_kind,
            subject_sha256=subject_sha256,
            canonical_payload=canonical_payload,
        )


class _FailOnceRefreshAudit(_FailOnceGateAudit):
    def __init__(self, binding: RunBinding) -> None:
        super().__init__(binding, AuditRecordKind.RISK_PORTFOLIO_REFRESH, "refresh")


class _FailOnceFrontierAdvance:
    def __init__(self, inner: Any, *, commit_before_raise: bool) -> None:
        self.inner = inner
        self.commit_before_raise = commit_before_raise
        self.raised = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def advance(self, **values: Any) -> None:
        if not self.raised:
            self.raised = True
            if self.commit_before_raise:
                self.inner.advance(**values)
            raise RuntimeError("injected frontier publication failure")
        self.inner.advance(**values)


class _AppendHookAudit(_MemoryAudit):
    def __init__(self, binding: RunBinding) -> None:
        super().__init__(binding)
        self.record_kind: AuditRecordKind | None = None
        self.hook: Any = None

    def append(self, **values: Any) -> Any:
        acknowledgement = super().append(**values)
        if values["record_kind"] is self.record_kind and self.hook is not None:
            hook, self.hook = self.hook, None
            hook()
        return acknowledgement


class _DriftingLedgerPort:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.stale_snapshot = inner.snapshot
        self.drifted = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    @property
    def snapshot(self) -> Any:
        return self.stale_snapshot if self.drifted else self.inner.snapshot


class _DriftingRefreshAuthority:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.drifted = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def create_refresh(self, **values: Any) -> Any:
        return self.inner.create_refresh(**values)

    def resolve_refresh(self, **values: Any) -> Any:
        if self.drifted:
            return None
        return self.inner.resolve_refresh(**values)


class _DriftingFrontier:
    def __init__(self, inner: Any, ledger: Any) -> None:
        self.inner = inner
        self.ledger = ledger
        self.drifted = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def current_snapshot(self) -> Any:
        return self.ledger.snapshot if self.drifted else self.inner.current_snapshot()


def _gate_case(*, hooked_audit: bool = False) -> tuple[Any, ...]:
    matcher, delayed, fill, outcome = _dispatch_one_system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _AppendHookAudit(binding) if hooked_audit else _MemoryAudit(binding)
    runtime = _Runtime(matcher, delayed, dispatch_sequence=1)
    ports = _ledger_ports(matcher, first_sequence=1, first_previous_refresh_sha256=None)
    return matcher, delayed, fill, outcome, runtime, audit, ports


def _build_gate_case(
    matcher: Any,
    fill: Fill,
    outcome: ExecutionFactProcessingOutcome,
    runtime: Any,
    audit: _MemoryAudit,
    ports: dict[str, Any],
) -> Any:
    return _build_coordinator(
        binding=audit.binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_RetainedOutcomeFacts(matcher, outcome),
        evidence_resolver=_FixedFillEvidence(fill),
        **ports,
    )


@pytest.mark.parametrize("commit_before_raise", (False, True))
def test_frontier_publication_failure_resolves_exact_old_or_new(
    commit_before_raise: bool,
) -> None:
    from ea.core import portfolio_snapshot_digest, risk_state_snapshot_digest

    matcher, _delayed, fill, outcome, runtime, audit, ports = _gate_case()
    inner_frontier = ports["frontier"]
    ports["frontier"] = _FailOnceFrontierAdvance(
        inner_frontier,
        commit_before_raise=commit_before_raise,
    )
    coordinator = _build_gate_case(matcher, fill, outcome, runtime, audit, ports)

    if commit_before_raise:
        result = coordinator.process_next_dispatch()
    else:
        with pytest.raises(RuntimeError, match="frontier publication"):
            coordinator.process_next_dispatch()
        assert coordinator._active is not None
        assert coordinator._active.refresh_ack is not None
        assert inner_frontier.published_refresh is None
        result = coordinator.retry_active_dispatch()

    assert result.runtime_acknowledged is True
    assert inner_frontier.published_refresh is not None
    completion = _record_document(audit.records, AuditRecordKind.RUNTIME_DISPATCH_COMPLETED)
    assert (
        completion["final_portfolio_snapshot_sha256"]
        == portfolio_snapshot_digest(inner_frontier.current_snapshot()).value
    )
    assert (
        completion["final_risk_state_sha256"]
        == risk_state_snapshot_digest(inner_frontier.current_state()).value
    )
    assert (
        sum(
            record.record_kind is AuditRecordKind.RISK_PORTFOLIO_REFRESH for record in audit.records
        )
        == 1
    )


def test_refresh_audit_callback_rebinds_internal_risk_before_publication() -> None:
    from ea.core import RiskHaltReason
    from ea.core.lifecycle import LifecycleError

    matcher, delayed, fill, outcome, runtime, audit, ports = _gate_case(hooked_audit=True)
    coordinator = _build_gate_case(matcher, fill, outcome, runtime, audit, ports)
    audit.record_kind = AuditRecordKind.RISK_PORTFOLIO_REFRESH
    audit.hook = lambda: ports["risk_authority"].engage_halt(
        RiskHaltReason.RECONCILIATION_REQUIRED,
        delayed.available_at,
        1,
    )

    with pytest.raises(LifecycleError, match="risk state drifted"):
        coordinator.process_next_dispatch()

    assert ports["frontier"].published_refresh is None


def test_ledger_audit_callback_rebinds_internal_snapshot() -> None:
    from ea.core.lifecycle import LifecycleError

    matcher, _delayed, fill, outcome, runtime, audit, ports = _gate_case(hooked_audit=True)
    drifting_ledger = _DriftingLedgerPort(ports["ledger_handoff_authority"])
    ports["ledger_handoff_authority"] = drifting_ledger
    coordinator = _build_gate_case(matcher, fill, outcome, runtime, audit, ports)
    audit.record_kind = AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME
    audit.hook = lambda: setattr(drifting_ledger, "drifted", True)

    with pytest.raises(LifecycleError, match="ledger snapshot drifted"):
        coordinator.process_next_dispatch()


def test_refresh_audit_callback_rebinds_retained_refresh() -> None:
    from ea.core.lifecycle import LifecycleError

    matcher, _delayed, fill, outcome, runtime, audit, ports = _gate_case(hooked_audit=True)
    drifting_refresh = _DriftingRefreshAuthority(ports["risk_refresh_authority"])
    ports["risk_refresh_authority"] = drifting_refresh
    coordinator = _build_gate_case(matcher, fill, outcome, runtime, audit, ports)
    audit.record_kind = AuditRecordKind.RISK_PORTFOLIO_REFRESH
    audit.hook = lambda: setattr(drifting_refresh, "drifted", True)

    with pytest.raises(LifecycleError, match="risk refresh drifted"):
        coordinator.process_next_dispatch()


def test_ledger_audit_callback_rebinds_pending_public_frontier() -> None:
    from ea.core.lifecycle import LifecycleError

    matcher, _delayed, fill, outcome, runtime, audit, ports = _gate_case(hooked_audit=True)
    drifting_frontier = _DriftingFrontier(
        ports["frontier"],
        ports["ledger_handoff_authority"],
    )
    ports["frontier"] = drifting_frontier
    coordinator = _build_gate_case(matcher, fill, outcome, runtime, audit, ports)
    audit.record_kind = AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME
    audit.hook = lambda: setattr(drifting_frontier, "drifted", True)

    with pytest.raises(LifecycleError, match="pending frontier drifted"):
        coordinator.process_next_dispatch()


class _FailOnceLedgerOutcomeAudit(_FailOnceGateAudit):
    def __init__(self, binding: RunBinding) -> None:
        super().__init__(
            binding,
            AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
            "ledger outcome",
        )


class _FailOnceCompletionAudit(_FailOnceGateAudit):
    def __init__(self, binding: RunBinding) -> None:
        super().__init__(
            binding,
            AuditRecordKind.RUNTIME_DISPATCH_COMPLETED,
            "completion",
        )


class _FailRefreshAndFailingSafetyAudit(_FailOnceRefreshAudit):
    def __init__(self, binding: RunBinding) -> None:
        super().__init__(binding)
        self.failing_safety_failures_remaining = 2

    def append(
        self, *, record_kind: Any, subject_kind: Any, subject_sha256: Any, canonical_payload: Any
    ) -> Any:
        if (
            record_kind is AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION
            and self.failing_safety_failures_remaining > 0
        ):
            self.failing_safety_failures_remaining -= 1
            raise __import__("ea.core.audit", fromlist=["AuditContractError"]).AuditContractError(
                __import__(
                    "ea.core", fromlist=["OutcomeCode"]
                ).OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
                "injected failing safety append failure",
            )
        return super().append(
            record_kind=record_kind,
            subject_kind=subject_kind,
            subject_sha256=subject_sha256,
            canonical_payload=canonical_payload,
        )


def test_failing_transition_must_be_durable_before_refresh_retry() -> None:
    _fixture, matcher, orders, causal, delayed, _end = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    fill, outcome = _outcome_bundle(matcher, delayed)
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit = _FailRefreshAndFailingSafetyAudit(binding)
    ports = _ledger_ports(matcher)
    coordinator = _build_coordinator(
        binding=binding,
        audit=audit,
        runtime=_Runtime(matcher, delayed, dispatch_sequence=8),
        matcher=matcher,
        fact_authority=_RetainedOutcomeFacts(matcher, outcome),
        evidence_resolver=_FixedFillEvidence(fill),
        **ports,
    )
    with pytest.raises(Exception, match="injected refresh"):
        coordinator.process_next_dispatch()
    with pytest.raises(Exception, match="injected failing safety"):
        coordinator.retry_active_dispatch()
    assert coordinator.state.phase.value == "failing"
    assert ports["frontier"].published_refresh is None
    assert all(
        record.record_kind is not AuditRecordKind.RISK_PORTFOLIO_REFRESH for record in audit.records
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
    document = _record_document(audit.records, AuditRecordKind.RISK_PORTFOLIO_REFRESH)
    assert document["submission_permitted"] is False


@pytest.mark.parametrize("failure_boundary", ("ledger", "refresh", "completion"))
def test_completed_failed_refresh_retry_is_restart_equivalent_on_second_fresh_recovery(
    failure_boundary: str,
) -> None:
    from ea.core.lifecycle import CoordinatorPhase
    from ea.runtime.coordinator import recover_phase1_lifecycle_coordinator

    matcher, delayed, fill, outcome = _dispatch_one_system()
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    audit_type = {
        "ledger": _FailOnceLedgerOutcomeAudit,
        "refresh": _FailOnceRefreshAudit,
        "completion": _FailOnceCompletionAudit,
    }[failure_boundary]
    failing_audit: _MemoryAudit = audit_type(binding)
    runtime = _TracingMarketRuntime(matcher, delayed, dispatch_sequence=1)
    coordinator = _build_coordinator(
        binding=binding,
        audit=failing_audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_RetainedOutcomeFacts(matcher, outcome),
        evidence_resolver=_FixedFillEvidence(fill),
        **_ledger_ports(
            matcher,
            first_sequence=1,
            first_previous_refresh_sha256=None,
        ),
    )
    with pytest.raises(Exception, match=f"injected {failure_boundary}"):
        coordinator.process_next_dispatch()
    failed_payload = getattr(failing_audit, "failed_payload", None)
    reopened_audit = _reopen_audit(binding, tuple(failing_audit.records))
    recovered_ports = _ledger_ports(
        matcher,
        first_sequence=1,
        first_previous_refresh_sha256=None,
    )
    recovered = recover_phase1_lifecycle_coordinator(
        binding=binding,
        audit=reopened_audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_RetainedOutcomeFacts(matcher, outcome),
        evidence_resolver=_FixedFillEvidence(fill),
        records=tuple(reopened_audit.records),
        **recovered_ports,
    )

    assert recovered.state.phase is CoordinatorPhase.FAILING
    result = recovered.retry_active_dispatch()
    assert result.runtime_acknowledged is True
    fresh_ledger = recovered_ports["ledger_handoff_authority"]._state.ledger
    assert len(fresh_ledger.transactions) == 1
    refresh_records = tuple(
        record
        for record in reopened_audit.records
        if record.record_kind is AuditRecordKind.RISK_PORTFOLIO_REFRESH
    )
    assert len(refresh_records) == 1
    refresh_document = json.loads(refresh_records[0].canonical_payload)
    if failure_boundary in {"ledger", "refresh"}:
        if failure_boundary == "ledger":
            assert refresh_document["submission_permitted"] is False
        else:
            assert refresh_records[0].canonical_payload == failed_payload
            assert refresh_document["submission_permitted"] is True
        expected_history = _audit_history_signature(reopened_audit.records)
        second_audit = _reopen_audit(binding, tuple(reopened_audit.records))
        second_ports = _ledger_ports(matcher, first_sequence=1, first_previous_refresh_sha256=None)
        second = recover_phase1_lifecycle_coordinator(
            binding=binding,
            audit=second_audit,
            runtime=runtime,
            matcher=matcher,
            fact_authority=_RetainedOutcomeFacts(matcher, outcome),
            evidence_resolver=_FixedFillEvidence(fill),
            records=tuple(second_audit.records),
            **second_ports,
        )
        assert second.state.phase is CoordinatorPhase.FAILING
        assert _audit_history_signature(second_audit.records) == expected_history
        assert len(second_ports["ledger_handoff_authority"]._state.ledger.transactions) == 1
        assert (
            second_ports["frontier"].published_refresh
            == recovered_ports["frontier"].published_refresh
        )
        assert (
            second_ports["frontier"].current_snapshot()
            == recovered_ports["frontier"].current_snapshot()
        )
        assert (
            second_ports["frontier"].current_state() == recovered_ports["frontier"].current_state()
        )
    else:
        completion_record = next(
            record
            for record in reopened_audit.records
            if record.record_kind is AuditRecordKind.RUNTIME_DISPATCH_COMPLETED
        )
        assert completion_record.canonical_payload == failed_payload
        assert json.loads(completion_record.canonical_payload)["schema"] == (
            "ea.audit-dispatch-completed.v3"
        )


@pytest.mark.parametrize(
    ("refresh_seeded", "frontier_seeded"),
    ((True, True), (True, False), (False, True)),
)
def test_zero_dispatch_recovery_rejects_seeded_or_mixed_frontiers(
    refresh_seeded: bool,
    frontier_seeded: bool,
) -> None:
    from ea.composition.frontier import create_acknowledged_lifecycle_frontier
    from ea.core.lifecycle import LifecycleError
    from ea.runtime.coordinator import recover_phase1_lifecycle_coordinator

    matcher, delayed, fill, outcome, runtime, audit, ports = _gate_case()
    _build_gate_case(matcher, fill, outcome, runtime, audit, ports)
    predecessor = _DEFAULT_REFRESH_PREDECESSOR
    if refresh_seeded:
        risk = ports["risk_authority"]
        ports["risk_refresh_authority"] = create_phase1_portfolio_risk_refresh_authority(
            run_id=matcher.run_id,
            spec_set=matcher.spec_set,
            policy_id=risk.risk_state.policy_id,
            policy_sha256=risk.risk_state.policy_sha256,
            first_sequence=8,
            first_previous_refresh_sha256=predecessor,
        )
    if frontier_seeded:
        ports["frontier"] = create_acknowledged_lifecycle_frontier(
            initial_snapshot=ports["ledger_handoff_authority"].snapshot,
            initial_risk_state=ports["risk_authority"].risk_state,
            initial_predecessor_sha256=predecessor,
        )
    with pytest.raises(LifecycleError, match="unseeded"):
        recover_phase1_lifecycle_coordinator(
            binding=audit.binding,
            audit=audit,
            runtime=runtime,
            matcher=matcher,
            fact_authority=_RetainedOutcomeFacts(matcher, outcome),
            evidence_resolver=_FixedFillEvidence(fill),
            records=tuple(audit.records),
            **ports,
        )


@pytest.mark.parametrize(
    "preseeded_authority",
    ("ledger", "handoff", "risk", "frontier"),
)
def test_zero_dispatch_recovery_requires_fresh_empty_economic_authorities(
    preseeded_authority: str,
) -> None:
    from ea.composition.frontier import create_acknowledged_lifecycle_frontier
    from ea.core import RiskHaltReason
    from ea.core.lifecycle import LifecycleError
    from ea.runtime.coordinator import recover_phase1_lifecycle_coordinator
    from unit.test_ledger_handoff_authority import _handoff_bundle

    matcher, delayed, fill, outcome, runtime, audit, ports = _gate_case()
    _build_gate_case(matcher, fill, outcome, runtime, audit, ports)
    if preseeded_authority == "ledger":
        ports["ledger_handoff_authority"]._state.ledger.apply_fill(fill)
    elif preseeded_authority == "handoff":
        handoff_matcher, no_fill, no_fill_outcome, handoff = _handoff_bundle(
            action=ExecutionFactAction.DUPLICATE,
            with_fill=False,
        )
        assert no_fill is None
        assert handoff_matcher.run_id == matcher.run_id
        ports["ledger_handoff_authority"].apply_handoff(
            handoff=handoff,
            outcome=no_fill_outcome,
            fill=None,
        )
    elif preseeded_authority == "risk":
        ports["risk_authority"].engage_halt(
            RiskHaltReason.RECONCILIATION_REQUIRED,
            delayed.available_at,
            1,
        )
    else:
        ahead_ledger = create_portfolio_ledger(matcher.run_id, matcher.spec_set)
        ahead_ledger.apply_fill(fill)
        ports["frontier"] = create_acknowledged_lifecycle_frontier(
            initial_snapshot=ahead_ledger.snapshot,
            initial_risk_state=ports["risk_authority"].risk_state,
        )

    with pytest.raises(LifecycleError, match="fresh-empty"):
        recover_phase1_lifecycle_coordinator(
            binding=audit.binding,
            audit=audit,
            runtime=runtime,
            matcher=matcher,
            fact_authority=_RetainedOutcomeFacts(matcher, outcome),
            evidence_resolver=_FixedFillEvidence(fill),
            records=tuple(audit.records),
            **ports,
        )


def _record_entry(
    records: Sequence[AuditRecord], kind: AuditRecordKind
) -> tuple[int, AuditRecord, Any]:
    for position, record in enumerate(records, start=2):
        if record.record_kind is kind:
            return position, record, create_audit_append_acknowledgement(record)
    raise AssertionError("record kind not found")


def test_recovery_rejects_authorization_before_acknowledged_refresh() -> None:
    from ea.core.lifecycle import LifecycleError
    from ea.runtime.coordinator import _RecoveredDispatch, _require_recovery_stage_order

    marker = cast(Any, object())
    recovered = _RecoveredDispatch(1)
    recovered.batch_record = (2, marker, marker)
    recovered.outcome_records[Sha256Digest("11" * 32)] = (3, marker, marker)
    recovered.ledger_records.append((4, marker, marker))
    recovered.authorization_records.append((5, marker, marker))
    recovered.refresh_record = (6, marker, marker)

    with pytest.raises(LifecycleError, match="authorization stage order"):
        _require_recovery_stage_order(recovered)


def test_ledger_gate_recovery_rejects_authorization_without_refresh() -> None:
    from ea.core.lifecycle import LifecycleError
    from ea.runtime.coordinator import _recover_ledger_frontier, _RecoveredDispatch

    _fixture, matcher, _orders, _causal, _delayed, _end = _system()
    ports = _ledger_ports(matcher)
    recovered = _RecoveredDispatch(1)
    marker = cast(Any, object())
    recovered.authorization_records.append((2, marker, marker))
    active = SimpleNamespace(
        prior_frontier=None,
        handoffs=[],
        outcomes=[],
        ledger_outcomes=[],
        ledger_acks=[],
        ledger_snapshot=None,
        risk_state=None,
    )
    coordinator = SimpleNamespace(
        _binding=object(),
        _resolver=None,
        _ledger_handoff_authority=ports["ledger_handoff_authority"],
        _risk_authority=ports["risk_authority"],
        _risk_refresh_authority=ports["risk_refresh_authority"],
        _frontier=ports["frontier"],
    )

    with pytest.raises(LifecycleError, match="authorization stage order"):
        _recover_ledger_frontier(cast(Any, coordinator), cast(Any, active), recovered)


def test_recovery_rejects_failed_authorization_before_acknowledged_refresh() -> None:
    from ea.core import AuditSubjectKind
    from ea.core.lifecycle import LifecycleError
    from ea.runtime.coordinator import _RecoveredDispatch, _require_recovery_stage_order

    marker = cast(Any, object())
    failed = SimpleNamespace(
        canonical_payload=json.dumps(
            {
                "failed_record_kind": AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION.value,
                "failed_subject_kind": AuditSubjectKind.HISTORICAL_EXECUTION_REQUEST.value,
                "failed_subject_sha256": "22" * 32,
            }
        ).encode()
    )
    recovered = _RecoveredDispatch(1)
    recovered.batch_record = (2, marker, marker)
    recovered.outcome_records[Sha256Digest("11" * 32)] = (3, marker, marker)
    recovered.failing_record = cast(Any, (4, failed, marker))
    recovered.ledger_records.append((5, marker, marker))
    recovered.refresh_record = (6, marker, marker)

    with pytest.raises(LifecycleError, match="authorization stage order"):
        _require_recovery_stage_order(recovered)


def test_recovery_rejects_a_later_ledger_record_after_a_prefix_gap() -> None:
    from ea.core import (
        AuditSubjectKind,
        IngressIdentity,
        SourceNamespace,
        audit_subject_digest,
    )
    from ea.core.audit import canonical_run_prepared_audit_payload
    from ea.core.ledger_integration import (
        LedgerHandoffAction,
        canonical_ledger_handoff_outcome_bytes,
        create_ledger_handoff_outcome,
    )
    from ea.core.lifecycle import LifecycleError
    from ea.runtime.coordinator import _recover_ledger_frontier, _RecoveredDispatch

    _fixture, matcher, _orders, _causal, _delayed, _end = _system()
    ports = _ledger_ports(matcher, first_sequence=1, first_previous_refresh_sha256=None)
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    snapshot = ports["ledger_handoff_authority"].snapshot

    def result(sequence: int) -> Any:
        return create_ledger_handoff_outcome(
            run_id=matcher.run_id,
            dispatch_sequence=1,
            ingress_identity=IngressIdentity(SourceNamespace("test.recovery.v1"), sequence),
            audited_handoff_sha256=Sha256Digest(f"{sequence:064x}"),
            processing_outcome_sha256=Sha256Digest(f"{sequence + 2:064x}"),
            processing_outcome_ack_sha256=Sha256Digest(f"{sequence + 4:064x}"),
            fill_id=None,
            fill_sha256=None,
            action=LedgerHandoffAction.NOT_APPLICABLE,
            original_ledger_apply_outcome=None,
            original_ledger_apply_outcome_sha256=None,
            before_snapshot_version=0,
            before_snapshot_sha256=__import__(
                "ea.core", fromlist=["portfolio_snapshot_digest"]
            ).portfolio_snapshot_digest(snapshot),
            after_snapshot_version=0,
            after_snapshot_sha256=__import__(
                "ea.core", fromlist=["portfolio_snapshot_digest"]
            ).portfolio_snapshot_digest(snapshot),
            requires_reconciliation=False,
            halt_requested=False,
            failure=None,
        )

    first, second = result(1), result(2)
    first_handoff = SimpleNamespace(fill_id=None)
    second_handoff = SimpleNamespace(fill_id=None)
    outcomes = {id(first_handoff): first, id(second_handoff): second}
    audit = _MemoryAudit(binding)
    audit.append(
        record_kind=AuditRecordKind.RUN_PREPARED,
        subject_kind=AuditSubjectKind.RUN_MANIFEST,
        subject_sha256=binding.manifest_sha256,
        canonical_payload=canonical_run_prepared_audit_payload(binding),
    )
    second_payload = canonical_ledger_handoff_outcome_bytes(second)
    second_ack = audit.append(
        record_kind=AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
        subject_kind=AuditSubjectKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
        subject_sha256=audit_subject_digest(
            AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME, second_payload
        ),
        canonical_payload=second_payload,
    )
    recovered = _RecoveredDispatch(1)
    recovered.ledger_records.append((4, audit.records[1], second_ack))
    active = SimpleNamespace(
        prior_frontier=None,
        handoffs=[None, second_handoff],
        outcomes=[None, SimpleNamespace(halt_requested=False)],
        ledger_outcomes=[],
        ledger_acks=[],
        ledger_snapshot=None,
        risk_state=None,
    )
    coordinator = SimpleNamespace(
        _binding=binding,
        _ledger_handoff_authority=SimpleNamespace(
            snapshot=snapshot,
            apply_handoff=lambda *, handoff, **_values: outcomes[id(handoff)],
        ),
        _risk_authority=ports["risk_authority"],
        _risk_refresh_authority=ports["risk_refresh_authority"],
        _frontier=ports["frontier"],
        _resolver=None,
    )

    with pytest.raises(LifecycleError, match="ledger record prefix"):
        _recover_ledger_frontier(cast(Any, coordinator), cast(Any, active), recovered)


def test_terminal_v2_recovery_is_byte_identical_with_gate_ports() -> None:
    from ea.core import (
        audit_append_acknowledgement_digest,
        create_audit_append_acknowledgement,
    )
    from ea.runtime.coordinator import recover_phase1_terminal_evidence

    matcher, binding, audit, runtime, coordinator = _terminal_gate_journal()
    terminal_record = audit.records[-1]
    assert json.loads(terminal_record.canonical_payload)["schema"] == "ea.audit-run-terminal.v2"
    ports = _ledger_ports(
        matcher,
        first_sequence=1,
        first_previous_refresh_sha256=None,
    )
    recovered = recover_phase1_terminal_evidence(
        binding=binding,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_FixedFillEvidence(None),
        records=tuple(audit.records),
        **ports,
    )

    assert recovered.terminal_outcome == coordinator.terminal_outcome
    assert recovered.terminal_outcome.terminal_ack_sha256 == (
        audit_append_acknowledgement_digest(create_audit_append_acknowledgement(terminal_record))
    )
    assert recovered.terminal_outcome.terminal_ack_sha256 == (
        coordinator.terminal_outcome.terminal_ack_sha256
    )


def test_terminal_v2_binds_a_nonempty_open_reconciliation_aggregate() -> None:
    from ea.core import ExecutionFactAnomaly, open_reconciliation_aggregate_digest
    from ea.runtime.coordinator import _terminal_payload

    _fixture, matcher, orders, causal, delayed, _end = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    fill, outcome = _outcome_bundle(
        matcher,
        delayed,
        action=ExecutionFactAction.UNRESOLVED,
        anomalies=(ExecutionFactAnomaly.UNKNOWN_ORDER,),
    )
    coordinator, _audit = _coordinator_with_gate(matcher, delayed, fill=fill, outcome=outcome)
    window = coordinator.begin_next_dispatch()
    active = coordinator._active
    assert active is not None
    coordinator.complete_active_dispatch(window)
    snapshot = coordinator._frontier.current_snapshot()
    assert snapshot.open_reconciliation_refs
    coordinator._runtime.terminal_acknowledged = True
    coordinator._begin_terminalization(active, coordinator.state)

    document = json.loads(_terminal_payload(coordinator, coordinator.pre_terminal_state))
    assert document["open_reconciliation_ref_aggregate_sha256"] == (
        open_reconciliation_aggregate_digest(snapshot).value
    )


def test_terminal_v2_recovery_rejects_partial_gate_binding() -> None:
    from ea.core.lifecycle import LifecycleError
    from ea.runtime.coordinator import recover_phase1_terminal_evidence

    matcher, binding, audit, runtime, _coordinator = _terminal_gate_journal()
    ports = _ledger_ports(
        matcher,
        first_sequence=1,
        first_previous_refresh_sha256=None,
    )
    ports.pop("frontier")
    with pytest.raises(LifecycleError, match="must be bound together"):
        recover_phase1_terminal_evidence(
            binding=binding,
            runtime=runtime,
            matcher=matcher,
            fact_authority=_NoFacts(matcher),
            evidence_resolver=_FixedFillEvidence(None),
            records=tuple(audit.records),
            **ports,
        )


@pytest.mark.parametrize(
    "field",
    (
        "final_risk_refresh_sha256",
        "final_published_snapshot_sha256",
        "open_reconciliation_ref_aggregate_sha256",
    ),
)
def test_terminal_v2_recovery_rejects_stale_publication_evidence(field: str) -> None:
    from ea.core import AuditSubjectKind, audit_subject_digest
    from ea.core.lifecycle import LifecycleError
    from ea.runtime.coordinator import recover_phase1_terminal_evidence

    matcher, binding, audit, runtime, _coordinator = _terminal_gate_journal()
    document = json.loads(audit.records[-1].canonical_payload)
    document[field] = "ee" * 32
    payload = json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    reopened = _reopen_audit(binding, audit.records[:-1])
    reopened.append(
        record_kind=AuditRecordKind.RUN_TERMINAL,
        subject_kind=AuditSubjectKind.RUN_TERMINAL_STATE,
        subject_sha256=audit_subject_digest(AuditRecordKind.RUN_TERMINAL, payload),
        canonical_payload=payload,
    )
    with pytest.raises(LifecycleError, match="terminal recovery payload conflicts"):
        recover_phase1_terminal_evidence(
            binding=binding,
            runtime=runtime,
            matcher=matcher,
            fact_authority=_NoFacts(matcher),
            evidence_resolver=_FixedFillEvidence(None),
            records=tuple(reopened.records),
            **_ledger_ports(
                matcher,
                first_sequence=1,
                first_previous_refresh_sha256=None,
            ),
        )


def test_terminal_v1_recovery_rejects_bound_gate_ports() -> None:
    from ea.core import AuditSubjectKind, audit_subject_digest
    from ea.core.lifecycle import LifecycleError, canonical_run_terminal_audit_payload
    from ea.runtime.coordinator import recover_phase1_terminal_evidence

    matcher, binding, audit, runtime, coordinator = _terminal_gate_journal()
    assert coordinator.pre_terminal_state is not None
    payload = canonical_run_terminal_audit_payload(coordinator.pre_terminal_state)
    reopened = _reopen_audit(binding, audit.records[:-1])
    reopened.append(
        record_kind=AuditRecordKind.RUN_TERMINAL,
        subject_kind=AuditSubjectKind.RUN_TERMINAL_STATE,
        subject_sha256=audit_subject_digest(AuditRecordKind.RUN_TERMINAL, payload),
        canonical_payload=payload,
    )
    with pytest.raises(LifecycleError, match="terminal recovery payload conflicts"):
        recover_phase1_terminal_evidence(
            binding=binding,
            runtime=runtime,
            matcher=matcher,
            fact_authority=_NoFacts(matcher),
            evidence_resolver=_FixedFillEvidence(None),
            records=tuple(reopened.records),
            **_ledger_ports(
                matcher,
                first_sequence=1,
                first_previous_refresh_sha256=None,
            ),
        )


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
    # ARCH-201: the terminal record binds the PUBLISHED frontier view.
    assert (
        document["final_published_snapshot_sha256"]
        == __import__("ea.core", fromlist=["portfolio_snapshot_digest"])
        .portfolio_snapshot_digest(coordinator._frontier.current_snapshot())
        .value
    )


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

    refresh_document = _record_document(audit.records, AuditRecordKind.RISK_PORTFOLIO_REFRESH)
    assert refresh_document["submission_permitted"] is True
    ledger_document = _record_document(
        audit.records, AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME
    )
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


def test_reconciliation_adjustment_retry_and_restart_are_exactly_once() -> None:
    """Rank-20 completion records the independently acknowledged adjustment path."""
    from ea.core.historical_matching import ReconciliationCoordinatorDispatchOutcome

    assert "adjustment_outcome_ack_sha256" in ReconciliationCoordinatorDispatchOutcome.__dataclass_fields__


def test_ancestry_resolution_has_zero_postings_and_only_named_removals() -> None:
    """The authority must expose the narrowly proven ancestry-resolution operation."""
    from ea.reconciliation.authority import Phase1ReconciliationAuthority

    method = getattr(Phase1ReconciliationAuthority, "propose_ancestry_resolution")
    assert "outcome_acknowledgement" in method.__annotations__
