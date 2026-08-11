from __future__ import annotations

import os
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

import ea.composition.run as run_composition
from ea.composition.lifecycle import (
    Phase1HistoricalLifecycle,
    _require_recovery_history_frontier,
    create_phase1_historical_lifecycle,
    recover_phase1_historical_lifecycle,
    recover_phase1_historical_terminal_evidence,
)
from ea.composition.run import RunCompositionError, admit_recovered_run
from ea.core.lifecycle import CoordinatorPhase, LifecycleError
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
from ea.runtime.historical import create_phase1_historical_market_runtime
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

    lifecycle = recover_phase1_historical_lifecycle(
        recovery=admitted,
        runtime=runtime,
        matcher_history=matcher,
        fact_history=fact_history,
        order_issuance_verifier=order_verifier,
        portfolio=freshness,
        risk=freshness,
        global_halt=freshness,
        instrument_gate=freshness,
    )

    assert lifecycle.coordinator.state.phase is CoordinatorPhase.ADMITTED
    assert lifecycle.matcher is not matcher
    assert lifecycle.fact_authority is not fact_history
    assert lifecycle.matcher.state == matcher.state
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
        )

    with pytest.raises(TypeError, match="created only by composition"):
        Phase1HistoricalLifecycle(
            _seal=object(),
            coordinator=lifecycle.coordinator,
            matcher=lifecycle.matcher,
            fact_authority=lifecycle.fact_authority,
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
            matcher_history=matcher,
            fact_history=fact_history,
            order_issuance_verifier=order_verifier,
            portfolio=freshness,
            risk=freshness,
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
    with pytest.raises(RunCompositionError, match="already consumed"):
        admitted._consume()
    admitted_journals[0].close()
    record = recovered_store._record_for(recovered._authority)
    os.close(record.writer_lock_fd)
