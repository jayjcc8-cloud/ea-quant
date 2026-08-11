"""Sealed joint construction for the Phase 1 historical lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, final

from ea.composition.run import AdmittedRecoveredRun
from ea.core.audit import AuditAppendAcknowledgement, AuditAppendPort
from ea.core.execution import InstrumentExecutionSpecSet
from ea.core.execution_identity import SourceNamespace
from ea.core.execution_messages import ExecutionPolicyRef, FactProvenanceId
from ea.core.lifecycle import (
    GlobalHaltFreshnessPort,
    InstrumentGateFreshnessPort,
    PortfolioFreshnessPort,
    RiskFreshnessPort,
)
from ea.core.run import RunBinding
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


class HistoricalLifecycleOrderVerifier(
    HistoricalOrderIssuanceVerifier,
    OrderResolutionVerifier,
    Protocol,
):
    """Composition-only intersection of matcher and fact Order lookup surfaces."""


@final
@dataclass(frozen=True, slots=True)
class Phase1HistoricalLifecycle:
    """Immutable public bundle published only after authorization activation."""

    _seal: object
    coordinator: Phase1HistoricalLifecycleCoordinator
    matcher: Phase1HistoricalMatcher
    fact_authority: Phase1ExecutionFactAuthority
    runtime: Phase1HistoricalMarketRuntime

    def __post_init__(self) -> None:
        if self._seal is not _LIFECYCLE_SEAL:
            raise TypeError("historical lifecycle bundles are created only by composition")


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
    portfolio: PortfolioFreshnessPort,
    risk: RiskFreshnessPort,
    global_halt: GlobalHaltFreshnessPort,
    instrument_gate: InstrumentGateFreshnessPort,
) -> Phase1HistoricalLifecycle:
    """Construct dormant authority, matcher, facts, coordinator, then activate once."""
    if type(prepared_acknowledgement) is not AuditAppendAcknowledgement:
        raise TypeError("fresh lifecycle construction requires one prepared acknowledgement")
    authorization, preparation_capability, activation_seal = (
        create_dormant_historical_submission_authorization_authority(
            binding=binding,
            audit=audit,
            runtime=runtime,
            spec_set=spec_set,
            execution_policy=execution_policy,
            portfolio=portfolio,
            risk=risk,
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
    )
    try:
        authorization.activate(coordinator, seal=activation_seal)
    except Exception:
        # No partially constructed object has escaped this stack frame.
        raise
    return Phase1HistoricalLifecycle(
        _seal=_LIFECYCLE_SEAL,
        coordinator=coordinator,
        matcher=matcher,
        fact_authority=fact_authority,
        runtime=runtime,
    )


def recover_phase1_historical_lifecycle(
    *,
    recovery: AdmittedRecoveredRun,
    runtime: Phase1HistoricalMarketRuntime,
    matcher_history: Phase1HistoricalMatcher,
    fact_history: Phase1ExecutionFactAuthority,
    order_issuance_verifier: HistoricalLifecycleOrderVerifier,
    portfolio: PortfolioFreshnessPort,
    risk: RiskFreshnessPort,
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
    binding, audit, records = recovery._consume()
    authorization, authorization_capability, activation_seal = (
        create_dormant_historical_submission_authorization_authority(
            binding=binding,
            audit=audit,
            runtime=runtime,
            spec_set=matcher_history.spec_set,
            execution_policy=matcher_history.execution_policy,
            portfolio=portfolio,
            risk=risk,
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
    coordinator = recover_phase1_lifecycle_coordinator(
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
    # Coordinator recovery exact-retries every retained record through this
    # same store-bound audit authority before an authorization attempt can be
    # restored or activated.
    authorization.recover_attempts(
        records,
        orders=order_issuance_verifier,
        submissions=matcher,
        seal=activation_seal,
    )
    authorization.activate(coordinator, seal=activation_seal)
    return Phase1HistoricalLifecycle(
        _seal=_LIFECYCLE_SEAL,
        coordinator=coordinator,
        matcher=matcher,
        fact_authority=fact_authority,
        runtime=runtime,
    )


def recover_phase1_historical_terminal_evidence(
    *,
    recovery: RecoveredTerminalRun,
    runtime: Phase1HistoricalMarketRuntime,
    matcher_history: Phase1HistoricalMatcher,
    fact_history: Phase1ExecutionFactAuthority,
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
    if (
        runtime.run_id != binding.reference.run_id
        or matcher_history.run_id != binding.reference.run_id
        or fact_history.run_id != binding.reference.run_id
        or runtime.spec_set.identifier != matcher_history.spec_set.identifier
        or fact_history.spec_set.identifier != matcher_history.spec_set.identifier
    ):
        raise TypeError("historical terminal recovery authority bindings conflict")
    return recover_phase1_terminal_evidence(
        binding=binding,
        runtime=runtime,
        matcher=matcher_history,
        fact_authority=fact_history,
        evidence_resolver=fact_history,
        records=records,
    )
