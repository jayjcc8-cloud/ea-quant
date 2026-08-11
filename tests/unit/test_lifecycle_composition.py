from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

import ea.composition.run as run_composition
from ea.composition.lifecycle import (
    Phase1HistoricalLifecycle,
    create_phase1_historical_lifecycle,
    recover_phase1_historical_lifecycle,
)
from ea.composition.run import RunCompositionError, admit_recovered_run
from ea.core.lifecycle import CoordinatorPhase
from ea.core.run import RunBinding, Sha256Digest
from ea.data import create_phase1_historical_market_source_bridge
from ea.execution.fact_authority import create_phase1_execution_fact_authority
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
from ea.runtime.historical import create_phase1_historical_market_runtime
from ea.runtime.matcher import (
    create_historical_matcher_descendant_fact_dispatch_verifier,
    create_historical_matcher_dispatch_verifier,
)
from unit.test_audit_journal import _release_simulated_process_writer
from unit.test_historical_matcher import _system
from unit.test_historical_runtime import _row, _source
from unit.test_store import _root, _spec


class _UnusedFreshness:
    def current_snapshot(self) -> Any:
        return None

    def current_state(self) -> Any:
        return None

    def current_for(self, instrument: Any) -> Any:
        del instrument
        return None


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


def test_recovery_composition_consumes_store_prefix_and_injected_histories(
    tmp_path: Path,
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
    authorization, capability, activation_seal = (
        create_dormant_historical_submission_authorization_authority(
            binding=admitted.binding,
            audit=admitted.audit,
            runtime=runtime,
            spec_set=matcher.spec_set,
            execution_policy=matcher.execution_policy,
            portfolio=freshness,
            risk=freshness,
            global_halt=freshness,
            instrument_gate=freshness,
        )
    )
    dispatch = create_historical_matcher_dispatch_verifier(runtime)
    object.__setattr__(matcher, "_submission_authorization_verifier", authorization)
    object.__setattr__(matcher, "_active_dispatch_verifier", dispatch)
    object.__setattr__(matcher, "_active_dispatch_runtime_identity", dispatch.runtime_identity)
    order_verifier = _RecoveryOrderVerifier(matcher, orders)
    object.__setattr__(matcher, "_order_issuance_verifier", order_verifier)
    descendant = create_historical_matcher_descendant_fact_dispatch_verifier(
        runtime=runtime,
        matcher=matcher,
    )
    facts = create_phase1_execution_fact_authority(
        run_id=matcher.run_id,
        spec_set=matcher.spec_set,
        order_verifier=order_verifier,
        dispatch_verifier=descendant,
    )

    lifecycle = recover_phase1_historical_lifecycle(
        recovery=admitted,
        runtime=runtime,
        matcher=matcher,
        fact_authority=facts,
        authorization=authorization,
        authorization_capability=capability,
        activation_seal=activation_seal,
        order_issuance_verifier=order_verifier,
    )

    assert lifecycle.coordinator.state.phase is CoordinatorPhase.ADMITTED
    assert lifecycle.matcher is matcher
    assert admitted_journals[0].records == recovered.records

    with pytest.raises(TypeError, match="created only by composition"):
        Phase1HistoricalLifecycle(
            _seal=object(),
            coordinator=lifecycle.coordinator,
            matcher=matcher,
            fact_authority=facts,
            runtime=runtime,
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
            portfolio=freshness,
            risk=freshness,
            global_halt=freshness,
            instrument_gate=freshness,
        )
    with pytest.raises(TypeError, match="exact authoritative carriers"):
        recover_phase1_historical_lifecycle(
            recovery=object(),  # type: ignore[arg-type]
            runtime=runtime,
            matcher=matcher,
            fact_authority=facts,
            authorization=authorization,
            authorization_capability=capability,
            activation_seal=activation_seal,
            order_issuance_verifier=order_verifier,
        )
    original_order_verifier = matcher._order_issuance_verifier
    object.__setattr__(matcher, "_order_issuance_verifier", object())
    with pytest.raises(TypeError, match="authority identities conflict"):
        recover_phase1_historical_lifecycle(
            recovery=admitted,
            runtime=runtime,
            matcher=matcher,
            fact_authority=facts,
            authorization=authorization,
            authorization_capability=capability,
            activation_seal=activation_seal,
            order_issuance_verifier=order_verifier,
        )
    object.__setattr__(matcher, "_order_issuance_verifier", original_order_verifier)
    original_fact_dispatch = facts._dispatch_verifier
    object.__setattr__(facts, "_dispatch_verifier", SimpleNamespace())
    with pytest.raises(TypeError, match="bindings are incomplete"):
        recover_phase1_historical_lifecycle(
            recovery=admitted,
            runtime=runtime,
            matcher=matcher,
            fact_authority=facts,
            authorization=authorization,
            authorization_capability=capability,
            activation_seal=activation_seal,
            order_issuance_verifier=order_verifier,
        )
    object.__setattr__(facts, "_dispatch_verifier", original_fact_dispatch)

    seal = run_composition._RECOVERED_ADMISSION_SEAL
    with pytest.raises(RunCompositionError, match="store-issued evidence"):
        run_composition.AdmittedRecoveredRun(
            object(),
            recovered=recovered,
            audit=admitted.audit,
        )
    with pytest.raises(RunCompositionError, match="one admitted audit port"):
        run_composition.AdmittedRecoveredRun(
            seal,
            recovered=recovered,
            audit=object(),  # type: ignore[arg-type]
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
        )
    original_records = recovered.records
    invalid_record_sets: tuple[object, ...] = ([], (), (object(),))
    for invalid_records in invalid_record_sets:
        object.__setattr__(recovered, "records", invalid_records)
        with pytest.raises(RunCompositionError, match="one admitted audit port"):
            run_composition.AdmittedRecoveredRun(
                seal,
                recovered=recovered,
                audit=admitted.audit,
            )
    object.__setattr__(recovered, "records", original_records)
    object.__setattr__(recovered, "record_count", len(original_records) + 1)
    with pytest.raises(RunCompositionError, match="one admitted audit port"):
        run_composition.AdmittedRecoveredRun(
            seal,
            recovered=recovered,
            audit=admitted.audit,
        )
    object.__setattr__(recovered, "record_count", len(original_records))
    original_record_binding = original_records[0].binding
    object.__setattr__(original_records[0], "binding", foreign_binding)
    with pytest.raises(RunCompositionError, match="one admitted audit port"):
        run_composition.AdmittedRecoveredRun(
            seal,
            recovered=recovered,
            audit=admitted.audit,
        )
    object.__setattr__(original_records[0], "binding", original_record_binding)
    probe = run_composition.AdmittedRecoveredRun(
        seal,
        recovered=recovered,
        audit=admitted.audit,
    )
    with pytest.raises(AttributeError, match="immutable"):
        probe.records = ()
    with pytest.raises(RunCompositionError, match="already consumed"):
        admitted._consume()
    admitted_journals[0].close()
    record = recovered_store._record_for(recovered._authority)
    os.close(record.writer_lock_fd)
