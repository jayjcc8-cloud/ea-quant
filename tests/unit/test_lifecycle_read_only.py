from __future__ import annotations

from ea.core import (
    AuditRecordKind,
    AuditSubjectKind,
    ReconciliationObservationKind,
    create_reconciliation_observation_root,
)
from ea.core.audit import canonical_run_prepared_audit_payload
from ea.core.lifecycle import ReadOnlyReconciliationDispatchOutcome
from ea.core.run import RunBinding, RunReference, Sha256Digest
from ea.reconciliation.authority import _create_observation_only_reconciliation_authority
from ea.runtime.coordinator import create_phase1_lifecycle_coordinator
from unit.test_historical_matcher import _system
from unit.test_lifecycle_composition import _staged_lifecycle
from unit.test_lifecycle_coordinator import _MemoryAudit, _NoFacts, _Runtime
from unit.test_lifecycle_ledger_gate import _FixedFillEvidence, _ledger_ports
from unit.test_reconciliation_authority import _observation


def test_rank_20_position_observation_settles_to_a_read_only_outcome() -> None:
    _fixture, matcher, _orders, _causal, _delayed, _end = _system()
    root = create_reconciliation_observation_root(
        _observation(
            kind=ReconciliationObservationKind.POSITION_SNAPSHOT,
            spec_set=matcher.spec_set,
        )
    )
    ports = _ledger_ports(
        matcher,
        first_sequence=1,
        first_previous_refresh_sha256=None,
    )
    reconciliation = _create_observation_only_reconciliation_authority(
        run_id=matcher.run_id,
        spec_set=matcher.spec_set,
        snapshot_view=ports["frontier"].current_snapshot,
    )
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)), Sha256Digest("22" * 32)
    )
    audit = _MemoryAudit(binding)
    prepared = audit.append(
        record_kind=AuditRecordKind.RUN_PREPARED,
        subject_kind=AuditSubjectKind.RUN_MANIFEST,
        subject_sha256=binding.manifest_sha256,
        canonical_payload=canonical_run_prepared_audit_payload(binding),
    )
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        prepared_acknowledgement=prepared,
        audit=audit,
        runtime=_Runtime(matcher, root),
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_FixedFillEvidence(None),
        reconciliation_authority=reconciliation,
        **ports,
    )

    outcome = coordinator.process_next_dispatch()

    assert type(outcome) is ReadOnlyReconciliationDispatchOutcome
    assert outcome.observation_sha256 == root.observation_sha256
    assert outcome.runtime_acknowledged is True


def test_composition_keeps_the_read_only_authority_private() -> None:
    lifecycle, *_ = _staged_lifecycle()

    coordinator = lifecycle.coordinator._Phase1HistoricalLifecycleCoordinatorFacade__coordinator

    assert coordinator._reconciliation_authority is not None
