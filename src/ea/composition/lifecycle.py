"""Sealed joint construction for the Phase 1 historical lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, final

import ea.portfolio as portfolio
import ea.risk as risk
from ea.composition.frontier import create_acknowledged_lifecycle_frontier
from ea.composition.run import AdmittedRecoveredRun
from ea.core.audit import (
    AuditAppendAcknowledgement,
    AuditAppendPort,
    AuditRecord,
    AuditRecordKind,
    AuditRecoveryRecordSource,
    audit_acknowledgement_id,
    audit_append_acknowledgement_digest,
    create_audit_append_acknowledgement,
)
from ea.core.execution import InstrumentExecutionSpecSet
from ea.core.execution_identity import EconomicId, IngressIdentity, SourceNamespace
from ea.core.execution_messages import (
    ExecutionFact,
    ExecutionFactIngress,
    ExecutionPolicyRef,
    FactProvenanceId,
    Fill,
    Order,
    fill_digest,
)
from ea.core.execution_state import (
    ExecutionFactProcessingOutcome,
    OrderProjectionSnapshot,
    order_projection_snapshot_digest,
)
from ea.core.historical_matching import (
    HistoricalMatcherDispatchBatch,
    HistoricalMatcherState,
    HistoricalSubmissionReceipt,
)
from ea.core.lifecycle import (
    ActiveDispatchWindow,
    CoordinatorDispatchOutcome,
    CoordinatorRunState,
    CoordinatorTerminalOutcome,
    GlobalHaltFreshnessPort,
    InstrumentGateFreshnessPort,
    LifecycleError,
    PreTerminalCoordinatorState,
    TerminalCoordinatorState,
)
from ea.core.market_data import MarketDataEnvelope
from ea.core.outcomes import OutcomeCode
from ea.core.risk import Phase1RiskPolicy, phase1_risk_policy_digest
from ea.core.run import RunBinding, RunId, Sha256Digest
from ea.execution.fact_authority import (
    OrderResolutionVerifier,
    Phase1ExecutionFactAuthority,
    create_phase1_execution_fact_authority,
    recover_phase1_execution_fact_authority_history,
)
from ea.execution.matcher import (
    HistoricalOrderIssuanceVerifier,
    Phase1HistoricalMatcher,
    create_phase1_historical_matcher,
    recover_phase1_historical_matcher_history,
)
from ea.experiments.store import RecoveredTerminalRun, StoreError
from ea.runtime.authorization import (
    create_dormant_historical_submission_authorization_authority,
)
from ea.runtime.coordinator import (
    Phase1HistoricalLifecycleCoordinator,
    RecoveredTerminalCoordinatorEvidence,
    create_phase1_lifecycle_coordinator,
    recover_phase1_lifecycle_coordinator,
    recover_phase1_terminal_evidence,
)
from ea.runtime.historical import Phase1HistoricalMarketRuntime
from ea.runtime.matcher import (
    create_historical_matcher_descendant_fact_dispatch_verifier,
    create_historical_matcher_dispatch_verifier,
)

_LIFECYCLE_SEAL = object()
_READ_VIEW_SEAL = object()
_COORDINATOR_FACADE_SEAL = object()
_ECONOMIC_GATE_SEAL = object()


@final
class _Phase1EconomicGate:
    """Shared authority carrier for one phase-1 economic lineage."""

    __slots__ = (
        "__ledger",
        "__ledger_handoff_authority",
        "__risk_authority",
        "__risk_refresh_authority",
        "__frontier",
    )

    def __init__(
        self,
        *,
        ledger: Any,
        ledger_handoff_authority: Any,
        risk_authority: Any,
        risk_refresh_authority: Any,
        frontier: Any,
        seal: object,
    ) -> None:
        if seal is not _ECONOMIC_GATE_SEAL:
            raise TypeError("economic gates are created only by composition")
        self.__ledger = ledger
        self.__ledger_handoff_authority = ledger_handoff_authority
        self.__risk_authority = risk_authority
        self.__risk_refresh_authority = risk_refresh_authority
        self.__frontier = frontier

    @property
    def ledger(self) -> Any:
        return self.__ledger

    @property
    def ledger_handoff_authority(self) -> Any:
        return self.__ledger_handoff_authority

    @property
    def risk_authority(self) -> Any:
        return self.__risk_authority

    @property
    def risk_refresh_authority(self) -> Any:
        return self.__risk_refresh_authority

    @property
    def frontier(self) -> Any:
        return self.__frontier

    def __iter__(self) -> Any:
        return iter(
            (
                self.__ledger_handoff_authority,
                self.__risk_authority,
                self.__risk_refresh_authority,
                self.__frontier,
            )
        )

    def __getitem__(self, index: int) -> Any:
        values = (
            self.__ledger_handoff_authority,
            self.__risk_authority,
            self.__risk_refresh_authority,
            self.__frontier,
        )
        return values[index]


class HistoricalLifecycleOrderVerifier(
    HistoricalOrderIssuanceVerifier,
    OrderResolutionVerifier,
    Protocol,
):
    """Composition-only intersection of matcher and fact Order lookup surfaces."""


@final
class HistoricalMatcherHistoryView:
    """Read-only matcher evidence without submission or dispatch mutation."""

    __slots__ = ("__matcher",)

    def __init__(self, matcher: Phase1HistoricalMatcher, *, seal: object) -> None:
        if seal is not _READ_VIEW_SEAL or type(matcher) is not Phase1HistoricalMatcher:
            raise TypeError("matcher history views are created only by composition")
        self.__matcher = matcher

    @property
    def run_id(self) -> RunId:
        return self.__matcher.run_id

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet:
        return self.__matcher.spec_set

    @property
    def source_namespace(self) -> SourceNamespace:
        return self.__matcher.source_namespace

    @property
    def execution_policy(self) -> ExecutionPolicyRef:
        return self.__matcher.execution_policy

    @property
    def provenance_id(self) -> FactProvenanceId:
        return self.__matcher.provenance_id

    @property
    def state(self) -> HistoricalMatcherState:
        return self.__matcher.state

    def resolve_dispatch_batch(
        self,
        *,
        dispatch_sequence: int,
        trigger_root_sha256: Sha256Digest,
    ) -> HistoricalMatcherDispatchBatch | None:
        return self.__matcher.resolve_dispatch_batch(
            dispatch_sequence=dispatch_sequence,
            trigger_root_sha256=trigger_root_sha256,
        )

    def resolve_submission_receipt(
        self,
        *,
        order_id: EconomicId,
        execution_request_sha256: Sha256Digest,
    ) -> HistoricalSubmissionReceipt | None:
        return self.__matcher.resolve_submission_receipt(
            order_id=order_id,
            execution_request_sha256=execution_request_sha256,
        )


@final
class ExecutionFactHistoryView:
    """Read-only fact history and exact evidence resolvers."""

    __slots__ = ("__authority",)

    def __init__(self, authority: Phase1ExecutionFactAuthority, *, seal: object) -> None:
        if seal is not _READ_VIEW_SEAL or type(authority) is not Phase1ExecutionFactAuthority:
            raise TypeError("fact history views are created only by composition")
        self.__authority = authority

    @property
    def run_id(self) -> RunId:
        return self.__authority.run_id

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet:
        return self.__authority.spec_set

    @property
    def ingresses(self) -> tuple[ExecutionFactIngress, ...]:
        return self.__authority.ingresses

    @property
    def outcomes(self) -> tuple[ExecutionFactProcessingOutcome, ...]:
        return self.__authority.outcomes

    @property
    def first_facts(self) -> tuple[ExecutionFact, ...]:
        return self.__authority.first_facts

    @property
    def fills(self) -> tuple[Fill, ...]:
        return self.__authority.fills

    @property
    def projections(self) -> tuple[OrderProjectionSnapshot, ...]:
        return self.__authority.projections

    def resolve_processing_outcome(
        self,
        *,
        ingress_identity: IngressIdentity,
        ingress_sha256: Sha256Digest,
    ) -> ExecutionFactProcessingOutcome | None:
        return self.__authority.resolve_processing_outcome(
            ingress_identity=ingress_identity,
            ingress_sha256=ingress_sha256,
        )

    def resolve_fill(
        self,
        *,
        fill_id: EconomicId,
        fill_sha256: Sha256Digest,
    ) -> Fill | None:
        return self.__authority.resolve_fill(fill_id=fill_id, fill_sha256=fill_sha256)

    def resolve_projection_after(
        self,
        *,
        order_id: EconomicId,
        projection_sha256: Sha256Digest,
    ) -> OrderProjectionSnapshot | None:
        return self.__authority.resolve_projection_after(
            order_id=order_id,
            projection_sha256=projection_sha256,
        )


@final
class HistoricalRuntimeHistoryView:
    """Read-only runtime trace without pop or acknowledgement capability."""

    __slots__ = ("__runtime",)

    def __init__(self, runtime: Phase1HistoricalMarketRuntime, *, seal: object) -> None:
        if seal is not _READ_VIEW_SEAL or type(runtime) is not Phase1HistoricalMarketRuntime:
            raise TypeError("runtime history views are created only by composition")
        self.__runtime = runtime

    @property
    def run_id(self) -> RunId:
        return self.__runtime.run_id

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet:
        return self.__runtime.spec_set

    @property
    def committed_event_count(self) -> int:
        return self.__runtime.committed_event_count

    @property
    def next_dispatch_sequence(self) -> int | None:
        return self.__runtime.next_dispatch_sequence

    @property
    def terminal_acknowledged(self) -> bool:
        return self.__runtime.terminal_acknowledged

    @property
    def trace_records(self) -> tuple[bytes, ...]:
        return self.__runtime.trace_records

    @property
    def trace_digest(self) -> Sha256Digest:
        return self.__runtime.trace_digest


@final
class Phase1HistoricalLifecycleCoordinatorFacade:
    """The staged public mutation surface frozen by Accepted ADR 0021."""

    __slots__ = ("__coordinator",)

    def __init__(
        self,
        coordinator: Phase1HistoricalLifecycleCoordinator,
        *,
        seal: object,
    ) -> None:
        if (
            seal is not _COORDINATOR_FACADE_SEAL
            or type(coordinator) is not Phase1HistoricalLifecycleCoordinator
        ):
            raise TypeError("coordinator facades are created only by composition")
        self.__coordinator = coordinator

    @property
    def binding(self) -> RunBinding:
        return self.__coordinator.binding

    @property
    def state(self) -> CoordinatorRunState:
        return self.__coordinator.state

    @property
    def pre_terminal_state(self) -> PreTerminalCoordinatorState | None:
        return self.__coordinator.pre_terminal_state

    @property
    def terminal_state(self) -> TerminalCoordinatorState | None:
        return self.__coordinator.terminal_state

    @property
    def terminal_outcome(self) -> CoordinatorTerminalOutcome | None:
        return self.__coordinator.terminal_outcome

    def begin_next_dispatch(self) -> ActiveDispatchWindow:
        return self.__coordinator.begin_next_dispatch()

    def resume_active_dispatch(self) -> ActiveDispatchWindow:
        return self.__coordinator.resume_active_dispatch()

    def prepare_submission_authorization(
        self,
        window: ActiveDispatchWindow,
        order: Order,
        *,
        causal_market_root: MarketDataEnvelope,
        dispatch_sequence: int,
    ) -> AuditAppendAcknowledgement:
        return self.__coordinator.prepare_submission_authorization(
            window,
            order,
            causal_market_root=causal_market_root,
            dispatch_sequence=dispatch_sequence,
        )

    def submit_authorized_order(
        self,
        window: ActiveDispatchWindow,
        order: Order,
    ) -> HistoricalSubmissionReceipt:
        return self.__coordinator.submit_authorized_order(window, order)

    def complete_active_dispatch(
        self,
        window: ActiveDispatchWindow,
    ) -> CoordinatorDispatchOutcome:
        return self.__coordinator.complete_active_dispatch(window)

    def retry_active_dispatch_completion(self) -> CoordinatorDispatchOutcome:
        return self.__coordinator.retry_active_dispatch_completion()

    def retry_terminalization(self) -> CoordinatorTerminalOutcome:
        return self.__coordinator.retry_terminalization()


@final
@dataclass(frozen=True, slots=True)
class Phase1HistoricalLifecycle:
    """Immutable public bundle published only after authorization activation."""

    _seal: object
    coordinator: Phase1HistoricalLifecycleCoordinatorFacade
    matcher: HistoricalMatcherHistoryView
    fact_authority: ExecutionFactHistoryView
    runtime: HistoricalRuntimeHistoryView

    def __post_init__(self) -> None:
        if (
            self._seal is not _LIFECYCLE_SEAL
            or type(self.coordinator) is not Phase1HistoricalLifecycleCoordinatorFacade
            or type(self.matcher) is not HistoricalMatcherHistoryView
            or type(self.fact_authority) is not ExecutionFactHistoryView
            or type(self.runtime) is not HistoricalRuntimeHistoryView
        ):
            raise TypeError("historical lifecycle bundles are created only by composition")


def _require_recovery_history_frontier(
    *,
    records: AuditRecoveryRecordSource | tuple[AuditRecord, ...],
    runtime: Phase1HistoricalMarketRuntime,
    matcher: Phase1HistoricalMatcher,
    fact_authority: Phase1ExecutionFactAuthority,
    coordinator: Phase1HistoricalLifecycleCoordinator | None,
) -> None:
    """Reject canonical inner histories that extend beyond durable recovery evidence."""
    matcher_state = matcher.state
    completed_sequence = len(runtime.trace_records)
    expected_matcher_sequence = completed_sequence
    if coordinator is not None:
        active = coordinator._active
        if active is not None and active.batch is not None:
            expected_matcher_sequence = active.lease.dispatch_sequence
    actual_matcher_sequence = matcher_state.last_new_dispatch_sequence or 0
    if actual_matcher_sequence != expected_matcher_sequence:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "matcher recovery history extends beyond the durable dispatch frontier",
        )

    retained_authorizations = {
        (
            audit_acknowledgement_id(acknowledgement),
            audit_append_acknowledgement_digest(acknowledgement),
        )
        for record in records
        if record.record_kind is AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION
        for acknowledgement in (create_audit_append_acknowledgement(record),)
    }
    if any(
        (receipt.audit_acknowledgement_id, receipt.audit_acknowledgement_sha256)
        not in retained_authorizations
        for receipt in matcher_state._submission_receipts
    ):
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "matcher submission history extends beyond durable authorization evidence",
        )

    processed_ingresses = fact_authority.ingresses
    outcomes = fact_authority.outcomes
    issued_ingresses = matcher_state.issued_ingresses
    if (
        len(processed_ingresses) != len(outcomes)
        or len(processed_ingresses) > len(issued_ingresses)
        or processed_ingresses != issued_ingresses[: len(processed_ingresses)]
    ):
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "fact recovery history extends beyond the matcher dispatch frontier",
        )
    if tuple(fill_digest(fill) for fill in fact_authority.fills) != tuple(
        outcome.fill_sha256 for outcome in outcomes if outcome.fill_sha256 is not None
    ):
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "Fill recovery history extends beyond processing outcomes",
        )
    retained_projection_sha256s = {
        outcome.projection_after_sha256
        for outcome in outcomes
        if outcome.projection_after_sha256 is not None
    }
    if any(
        order_projection_snapshot_digest(projection) not in retained_projection_sha256s
        for projection in fact_authority.projections
    ):
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "projection recovery history extends beyond processing outcomes",
        )


def _create_economic_gate(
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    execution_policy: ExecutionPolicyRef,
    risk_policy: Phase1RiskPolicy,
) -> _Phase1EconomicGate:
    ledger = portfolio.create_portfolio_ledger(run_id, spec_set)
    ledger_authority = portfolio.create_phase1_ledger_handoff_authority(run_id, spec_set, ledger)
    risk_authority = risk.create_phase1_risk_authority(
        run_id=run_id, spec_set=spec_set, execution_policy=execution_policy, policy=risk_policy
    )
    refresh_authority = portfolio.create_phase1_portfolio_risk_refresh_authority(
        run_id=run_id,
        spec_set=spec_set,
        policy_id=risk_policy.policy_id,
        policy_sha256=phase1_risk_policy_digest(risk_policy),
    )
    frontier = create_acknowledged_lifecycle_frontier(
        initial_snapshot=ledger.snapshot, initial_risk_state=risk_authority.risk_state
    )
    return _Phase1EconomicGate(
        ledger=ledger,
        ledger_handoff_authority=ledger_authority,
        risk_authority=risk_authority,
        risk_refresh_authority=refresh_authority,
        frontier=frontier,
        seal=_ECONOMIC_GATE_SEAL,
    )


def create_phase1_historical_economic_gate(
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    execution_policy: ExecutionPolicyRef,
    risk_policy: Phase1RiskPolicy,
) -> _Phase1EconomicGate:
    """Create one shared gate for phase-1 historical execution composition."""
    return _create_economic_gate(
        run_id=run_id,
        spec_set=spec_set,
        execution_policy=execution_policy,
        risk_policy=risk_policy,
    )


def _require_economic_gate(
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    execution_policy: ExecutionPolicyRef,
    risk_policy: Phase1RiskPolicy,
    economic_gate: _Phase1EconomicGate | None,
) -> _Phase1EconomicGate:
    if economic_gate is None:
        return _create_economic_gate(
            run_id=run_id,
            spec_set=spec_set,
            execution_policy=execution_policy,
            risk_policy=risk_policy,
        )
    if type(economic_gate) is not _Phase1EconomicGate:
        raise TypeError("economic gate must be exact _Phase1EconomicGate")
    return economic_gate


def create_phase1_historical_lifecycle(
    *,
    binding: RunBinding,
    prepared_acknowledgement: AuditAppendAcknowledgement,
    audit: AuditAppendPort,
    runtime: Phase1HistoricalMarketRuntime,
    spec_set: InstrumentExecutionSpecSet,
    execution_policy: ExecutionPolicyRef,
    source_namespace: SourceNamespace,
    provenance_id: FactProvenanceId,
    order_issuance_verifier: HistoricalLifecycleOrderVerifier,
    risk_policy: Phase1RiskPolicy,
    global_halt: GlobalHaltFreshnessPort,
    instrument_gate: InstrumentGateFreshnessPort,
    economic_gate: _Phase1EconomicGate | None = None,
) -> Phase1HistoricalLifecycle:
    """Construct dormant authority, matcher, facts, coordinator, then activate once."""
    if type(prepared_acknowledgement) is not AuditAppendAcknowledgement:
        raise TypeError("fresh lifecycle construction requires one prepared acknowledgement")
    economic_gate = _require_economic_gate(
        run_id=binding.reference.run_id,
        spec_set=spec_set,
        execution_policy=execution_policy,
        risk_policy=risk_policy,
        economic_gate=economic_gate,
    )
    ledger_handoff_authority, risk_authority, risk_refresh_authority, frontier = economic_gate
    authorization, preparation_capability, activation_seal = (
        create_dormant_historical_submission_authorization_authority(
            binding=binding,
            audit=audit,
            runtime=runtime,
            spec_set=spec_set,
            execution_policy=execution_policy,
            portfolio=frontier,
            risk=frontier,
            global_halt=global_halt,
            instrument_gate=instrument_gate,
        )
    )
    dispatch_verifier = create_historical_matcher_dispatch_verifier(runtime)
    matcher = create_phase1_historical_matcher(
        run_id=binding.reference.run_id,
        spec_set=spec_set,
        execution_policy=execution_policy,
        source_namespace=source_namespace,
        provenance_id=provenance_id,
        order_issuance_verifier=order_issuance_verifier,
        submission_authorization_verifier=authorization,
        active_dispatch_verifier=dispatch_verifier,
    )
    descendant = create_historical_matcher_descendant_fact_dispatch_verifier(
        runtime=runtime,
        matcher=matcher,
    )
    fact_authority = create_phase1_execution_fact_authority(
        run_id=binding.reference.run_id,
        spec_set=spec_set,
        order_verifier=order_issuance_verifier,
        dispatch_verifier=descendant,
    )
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        prepared_acknowledgement=prepared_acknowledgement,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=fact_authority,
        evidence_resolver=fact_authority,
        authorization=authorization,
        authorization_capability=preparation_capability,
        ledger_handoff_authority=ledger_handoff_authority,
        risk_authority=risk_authority,
        risk_refresh_authority=risk_refresh_authority,
        frontier=frontier,
    )
    try:
        authorization.activate(coordinator, seal=activation_seal)
    except Exception:
        # No partially constructed object has escaped this stack frame.
        raise
    return Phase1HistoricalLifecycle(
        _seal=_LIFECYCLE_SEAL,
        coordinator=Phase1HistoricalLifecycleCoordinatorFacade(
            coordinator,
            seal=_COORDINATOR_FACADE_SEAL,
        ),
        matcher=HistoricalMatcherHistoryView(matcher, seal=_READ_VIEW_SEAL),
        fact_authority=ExecutionFactHistoryView(fact_authority, seal=_READ_VIEW_SEAL),
        runtime=HistoricalRuntimeHistoryView(runtime, seal=_READ_VIEW_SEAL),
    )


def recover_phase1_historical_lifecycle(
    *,
    recovery: AdmittedRecoveredRun,
    runtime: Phase1HistoricalMarketRuntime,
    matcher_history: Phase1HistoricalMatcher,
    fact_history: Phase1ExecutionFactAuthority,
    order_issuance_verifier: HistoricalLifecycleOrderVerifier,
    risk_policy: Phase1RiskPolicy,
    global_halt: GlobalHaltFreshnessPort,
    instrument_gate: InstrumentGateFreshnessPort,
) -> Phase1HistoricalLifecycle:
    """Rebind sealed canonical histories without exposing authorization capability."""
    if (
        type(recovery) is not AdmittedRecoveredRun
        or type(runtime) is not Phase1HistoricalMarketRuntime
        or type(matcher_history) is not Phase1HistoricalMatcher
        or type(fact_history) is not Phase1ExecutionFactAuthority
    ):
        raise TypeError("historical lifecycle recovery requires exact authoritative carriers")
    reservation, binding, audit, records = recovery._reserve_consumption()
    replay_started = False
    try:
        ledger_handoff_authority, risk_authority, risk_refresh_authority, frontier = (
            _create_economic_gate(
                binding.reference.run_id,
                matcher_history.spec_set,
                matcher_history.execution_policy,
                risk_policy,
            )
        )
        authorization, authorization_capability, activation_seal = (
            create_dormant_historical_submission_authorization_authority(
                binding=binding,
                audit=audit,
                runtime=runtime,
                spec_set=matcher_history.spec_set,
                execution_policy=matcher_history.execution_policy,
                portfolio=frontier,
                risk=frontier,
                global_halt=global_halt,
                instrument_gate=instrument_gate,
            )
        )
        dispatch = create_historical_matcher_dispatch_verifier(runtime)
        matcher = recover_phase1_historical_matcher_history(
            matcher_history,
            order_issuance_verifier=order_issuance_verifier,
            submission_authorization_verifier=authorization,
            active_dispatch_verifier=dispatch,
        )
        descendant = create_historical_matcher_descendant_fact_dispatch_verifier(
            runtime=runtime,
            matcher=matcher,
        )
        fact_authority = recover_phase1_execution_fact_authority_history(
            fact_history,
            order_verifier=order_issuance_verifier,
            dispatch_verifier=descendant,
        )
        authorization.recover_attempts(
            records,
            orders=order_issuance_verifier,
            submissions=matcher,
            seal=activation_seal,
        )
        replay_started = True
        coordinator = recover_phase1_lifecycle_coordinator(
            ledger_handoff_authority=ledger_handoff_authority,
            risk_authority=risk_authority,
            risk_refresh_authority=risk_refresh_authority,
            frontier=frontier,
            binding=binding,
            audit=audit,
            runtime=runtime,
            matcher=matcher,
            fact_authority=fact_authority,
            evidence_resolver=fact_authority,
            records=records,
            authorization=authorization,
            authorization_capability=authorization_capability,
        )
        _require_recovery_history_frontier(
            records=records,
            runtime=runtime,
            matcher=matcher,
            fact_authority=fact_authority,
            coordinator=coordinator,
        )
        authorization.activate(coordinator, seal=activation_seal)
        coordinator._reconcile_recovered_authorization()
        lifecycle = Phase1HistoricalLifecycle(
            _seal=_LIFECYCLE_SEAL,
            coordinator=Phase1HistoricalLifecycleCoordinatorFacade(
                coordinator,
                seal=_COORDINATOR_FACADE_SEAL,
            ),
            matcher=HistoricalMatcherHistoryView(matcher, seal=_READ_VIEW_SEAL),
            fact_authority=ExecutionFactHistoryView(fact_authority, seal=_READ_VIEW_SEAL),
            runtime=HistoricalRuntimeHistoryView(runtime, seal=_READ_VIEW_SEAL),
        )
        recovery._commit_consumption(reservation)
        return lifecycle
    except BaseException:
        finish = recovery._fail_consumption if replay_started else recovery._abort_consumption
        finish(reservation)
        raise


def recover_phase1_historical_terminal_evidence(
    *,
    recovery: RecoveredTerminalRun,
    runtime: Phase1HistoricalMarketRuntime,
    matcher_history: Phase1HistoricalMatcher,
    fact_history: Phase1ExecutionFactAuthority,
    risk_policy: Phase1RiskPolicy,
) -> RecoveredTerminalCoordinatorEvidence:
    """Consume store-issued terminal evidence and publish no mutation authority."""
    if (
        type(recovery) is not RecoveredTerminalRun
        or type(runtime) is not Phase1HistoricalMarketRuntime
        or type(matcher_history) is not Phase1HistoricalMatcher
        or type(fact_history) is not Phase1ExecutionFactAuthority
    ):
        raise TypeError("historical terminal recovery requires exact authoritative carriers")
    try:
        binding, records = recovery._consume()
    except StoreError as error:
        raise TypeError("historical terminal recovery evidence was already consumed") from error
    try:
        ledger_handoff_authority, risk_authority, risk_refresh_authority, frontier = (
            _create_economic_gate(
                binding.reference.run_id,
                matcher_history.spec_set,
                matcher_history.execution_policy,
                risk_policy,
            )
        )
        if (
            runtime.run_id != binding.reference.run_id
            or matcher_history.run_id != binding.reference.run_id
            or fact_history.run_id != binding.reference.run_id
            or runtime.spec_set.identifier != matcher_history.spec_set.identifier
            or fact_history.spec_set.identifier != matcher_history.spec_set.identifier
        ):
            raise TypeError("historical terminal recovery authority bindings conflict")
        terminal = recover_phase1_terminal_evidence(
            binding=binding,
            runtime=runtime,
            matcher=matcher_history,
            fact_authority=fact_history,
            evidence_resolver=fact_history,
            records=records,
            ledger_handoff_authority=ledger_handoff_authority,
            risk_authority=risk_authority,
            risk_refresh_authority=risk_refresh_authority,
            frontier=frontier,
        )
        _require_recovery_history_frontier(
            records=records,
            runtime=runtime,
            matcher=matcher_history,
            fact_authority=fact_history,
            coordinator=None,
        )
        return terminal
    finally:
        recovery._finish()
