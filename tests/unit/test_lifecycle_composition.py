from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import pytest

from ea.composition.lifecycle import recover_phase1_historical_lifecycle
from ea.composition.run import RunCompositionError, admit_recovered_run
from ea.core.lifecycle import CoordinatorPhase
from ea.data import create_phase1_historical_market_source_bridge
from ea.execution.fact_authority import create_phase1_execution_fact_authority
from ea.experiments.audit import create_posix_audit_journal, reopen_posix_audit_journal
from ea.experiments.store import LocalResultStore
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
        self._orders = tuple(orders)

    def resolve_issued_order_by_id(self, order_id: Any) -> Any:
        return next((order for order in self._orders if order.order_id == order_id), None)

    def resolve_issued_order_by_client_submission_key(self, key: Any) -> Any:
        return next(
            (order for order in self._orders if order.client_submission_key == key),
            None,
        )


def test_recovery_composition_consumes_store_prefix_and_injected_histories(tmp_path: Any) -> None:
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
    recovered = recovered_store.recover_incomplete_attempt(verified)
    admitted_journals = []

    def reopen(binding: Any) -> Any:
        value = reopen_posix_audit_journal(binding)
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
    with pytest.raises(RunCompositionError, match="already consumed"):
        admitted._consume()
    admitted_journals[0].close()
    record = recovered_store._record_for(recovered._authority)
    os.close(record.writer_lock_fd)
