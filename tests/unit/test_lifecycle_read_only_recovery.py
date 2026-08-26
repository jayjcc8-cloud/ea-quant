from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from ea.core import (
    AuditRecordKind,
    AuditSubjectKind,
    ReconciliationObservationKind,
    ReconciliationObservationRoot,
    RunBinding,
    RunReference,
    Sha256Digest,
    create_reconciliation_observation_root,
)
from ea.core.lifecycle import LifecycleError
from ea.core.runtime import RuntimeOrderingError, runtime_root_order_key
from ea.experiments.audit import create_posix_audit_journal
from ea.experiments.store import LocalResultStore
from ea.runtime._coordinator_read_only import drive_read_only
from ea.runtime._coordinator_read_only_recovery import _require_read_only_recovery_order
from ea.runtime._coordinator_recovery import _require_runtime_trace
from ea.runtime.coordinator import (
    create_phase1_lifecycle_coordinator,
    recover_phase1_lifecycle_coordinator,
)
from ea.runtime.historical import historical_runtime_trace_digest
from unit.test_historical_matcher import _system
from unit.test_lifecycle_coordinator import _NoFacts, _Runtime
from unit.test_lifecycle_ledger_gate import _FixedFillEvidence, _ledger_ports
from unit.test_reconciliation_authority import _observation
from unit.test_store import _root, _spec


def _trace_order_key(root: ReconciliationObservationRoot) -> list[str | int]:
    return [
        value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if type(value) is datetime
        else cast(str | int, value)
        for value in runtime_root_order_key(root).as_tuple()
    ]


class _ReadOnlyRuntime(_Runtime):
    def __init__(
        self,
        *args: Any,
        fail_first_acknowledgement: bool = False,
        write_trace: bool = False,
    ) -> None:
        super().__init__(*args)
        self.acknowledgement_calls = 0
        self.fail_first_acknowledgement = fail_first_acknowledgement
        self.write_trace = write_trace

    def acknowledge(self, lease: Any) -> None:
        self.acknowledgement_calls += 1
        if self.fail_first_acknowledgement:
            self.fail_first_acknowledgement = False
            raise RuntimeError("injected pre-trace acknowledgement failure")
        assert lease is self._lease
        self._popped = False
        if self.write_trace:
            root = lease.root
            document = {
                "clock_now": root.available_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "committed_cursor_as_of": None,
                "committed_event_count": 0,
                "data_sha256": "00" * 32,
                "dispatch_sequence": lease.dispatch_sequence,
                "root": json.loads(root.canonical_observation_bytes),
                "root_order_key": _trace_order_key(root),
                "run_id": self.run_id.value,
                "schema": "ea.phase1-historical-runtime-trace.v2",
                "terminal_acknowledged": False,
            }
            record = json.dumps(
                document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            self.trace_records = (record,)
            self.trace_digest = historical_runtime_trace_digest(self.trace_records)


def _journal_bound_read_only_dispatch(
    tmp_path: Path, *, fail_first_acknowledgement: bool = False, write_trace: bool = False
) -> tuple[Any, Any, Any, dict[str, Any], _ReadOnlyRuntime, Any, Any]:
    _fixture, matcher, _orders, _causal, _delayed, _end = _system()
    root = create_reconciliation_observation_root(
        _observation(
            kind=ReconciliationObservationKind.POSITION_SNAPSHOT,
            spec_set=matcher.spec_set,
        )
    )
    prepared = LocalResultStore(_root(tmp_path)).prepare(
        _spec(), lambda: UUID(matcher.run_id.value)
    )
    journal = create_posix_audit_journal(prepared.audit)
    ports = _ledger_ports(matcher, first_sequence=1, first_previous_refresh_sha256=None)
    from ea.reconciliation.authority import _create_observation_only_reconciliation_authority

    reconciliation = _create_observation_only_reconciliation_authority(
        run_id=matcher.run_id,
        spec_set=matcher.spec_set,
        snapshot_view=ports["frontier"].current_snapshot,
    )
    runtime = _ReadOnlyRuntime(
        matcher,
        root,
        fail_first_acknowledgement=fail_first_acknowledgement,
        write_trace=write_trace,
    )
    prepared_acknowledgement = journal.append(
        record_kind=AuditRecordKind.RUN_PREPARED,
        subject_kind=AuditSubjectKind.RUN_MANIFEST,
        subject_sha256=prepared.audit.binding.manifest_sha256,
        canonical_payload=journal.records[0].canonical_payload,
    )
    coordinator = create_phase1_lifecycle_coordinator(
        binding=prepared.audit.binding,
        prepared_acknowledgement=prepared_acknowledgement,
        audit=journal,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_FixedFillEvidence(None),
        reconciliation_authority=reconciliation,
        **ports,
    )
    return coordinator, matcher, reconciliation, ports, runtime, journal, root


def _recover_read_only_dispatch(
    *,
    matcher: Any,
    reconciliation: Any,
    runtime: _ReadOnlyRuntime,
    journal: Any,
) -> tuple[Any, dict[str, Any]]:
    ports = _ledger_ports(matcher, first_sequence=1, first_previous_refresh_sha256=None)
    coordinator = recover_phase1_lifecycle_coordinator(
        binding=journal.binding,
        audit=journal,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_FixedFillEvidence(None),
        records=journal.recovery_records,
        reconciliation_authority=reconciliation,
        **ports,
    )
    return coordinator, ports


def test_recovery_accepts_one_canonical_v2_read_only_trace_root() -> None:
    _fixture, matcher, _orders, _causal, _delayed, _end = _system()
    root = create_reconciliation_observation_root(
        _observation(
            kind=ReconciliationObservationKind.POSITION_SNAPSHOT,
            spec_set=matcher.spec_set,
        )
    )
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)), Sha256Digest("22" * 32)
    )
    document = {
        "clock_now": root.available_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "committed_cursor_as_of": None,
        "committed_event_count": 0,
        "data_sha256": "00" * 32,
        "dispatch_sequence": 1,
        "root": json.loads(root.canonical_observation_bytes),
        "root_order_key": _trace_order_key(root),
        "run_id": matcher.run_id.value,
        "schema": "ea.phase1-historical-runtime-trace.v2",
        "terminal_acknowledged": False,
    }
    record = json.dumps(
        document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    runtime = SimpleNamespace(
        spec_set=matcher.spec_set,
        trace_records=(record,),
        trace_digest=historical_runtime_trace_digest((record,)),
    )

    trace = _require_runtime_trace(runtime, binding)

    assert trace[1][1] == root.observation_sha256


def test_read_only_recovery_rejects_completion_without_mandatory_refresh() -> None:
    outcome = SimpleNamespace(record_kind=AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME)
    completion = SimpleNamespace(record_kind=AuditRecordKind.RUNTIME_DISPATCH_COMPLETED)
    group = SimpleNamespace(
        read_only_outcome_record=(2, outcome, None),
        batch_record=None,
        outcome_records={},
        authorization_records=[],
        ledger_records=[],
        failing_record=None,
        refresh_record=None,
        completion_record=(3, completion, None),
    )

    with pytest.raises(LifecycleError, match="prefix"):
        _require_read_only_recovery_order(group)


def test_incomplete_journal_recovery_retains_only_the_active_lease(tmp_path: Path) -> None:
    coordinator, matcher, reconciliation, _ports, runtime, journal, _root_value = (
        _journal_bound_read_only_dispatch(tmp_path)
    )
    active = coordinator._capture_lease(runtime.pop())
    coordinator._active = active
    drive_read_only(coordinator, active, complete=False)
    retained_count = len(journal.records)
    acknowledgement_calls = runtime.acknowledgement_calls

    recovered, ports = _recover_read_only_dispatch(
        matcher=matcher,
        reconciliation=reconciliation,
        runtime=runtime,
        journal=journal,
    )

    assert len(journal.records) == retained_count
    assert runtime.acknowledgement_calls == acknowledgement_calls
    assert ports["frontier"].previous_refresh_sha256 is None
    assert recovered.state.last_completed_dispatch_sequence is None
    assert recovered._active is not None
    assert recovered._active.read_only is None


def test_completed_journal_without_trace_fails_before_recovery_side_effects(tmp_path: Path) -> None:
    coordinator, matcher, reconciliation, _ports, runtime, journal, _root_value = (
        _journal_bound_read_only_dispatch(tmp_path, fail_first_acknowledgement=True)
    )
    active = coordinator._capture_lease(runtime.pop())
    coordinator._active = active
    with pytest.raises(RuntimeError, match="pre-trace"):
        drive_read_only(coordinator, active, complete=True)
    retained_count = len(journal.records)
    acknowledgement_calls = runtime.acknowledgement_calls
    recovery_ports = _ledger_ports(matcher, first_sequence=1, first_previous_refresh_sha256=None)

    with pytest.raises(LifecycleError, match="completion acknowledgement is missing"):
        recover_phase1_lifecycle_coordinator(
            binding=journal.binding,
            audit=journal,
            runtime=runtime,
            matcher=matcher,
            fact_authority=_NoFacts(matcher),
            evidence_resolver=_FixedFillEvidence(None),
            records=journal.recovery_records,
            reconciliation_authority=reconciliation,
            **recovery_ports,
        )

    assert len(journal.records) == retained_count
    assert runtime.acknowledgement_calls == acknowledgement_calls
    assert recovery_ports["frontier"].previous_refresh_sha256 is None


def test_completed_journal_recovery_matches_the_uninterrupted_frontier_and_trace(
    tmp_path: Path,
) -> None:
    coordinator, matcher, reconciliation, source_ports, runtime, journal, _root_value = (
        _journal_bound_read_only_dispatch(tmp_path, write_trace=True)
    )
    active = coordinator._capture_lease(runtime.pop())
    coordinator._active = active
    drive_read_only(coordinator, active, complete=True)
    retained_records = tuple(journal.records)
    retained_trace = runtime.trace_records

    recovered, recovered_ports = _recover_read_only_dispatch(
        matcher=matcher,
        reconciliation=reconciliation,
        runtime=runtime,
        journal=journal,
    )

    assert tuple(journal.records) == retained_records
    assert runtime.trace_records == retained_trace
    assert recovered.state == coordinator.state
    assert (
        recovered_ports["frontier"].previous_refresh_sha256
        == source_ports["frontier"].previous_refresh_sha256
    )


def test_completed_journal_recovery_admits_the_root_to_a_fresh_authority(
    tmp_path: Path,
) -> None:
    coordinator, matcher, _reconciliation, source_ports, runtime, journal, _root_value = (
        _journal_bound_read_only_dispatch(tmp_path, write_trace=True)
    )
    active = coordinator._capture_lease(runtime.pop())
    coordinator._active = active
    uninterrupted = drive_read_only(coordinator, active, complete=True)
    from ea.reconciliation.authority import _create_observation_only_reconciliation_authority

    fresh_reconciliation = _create_observation_only_reconciliation_authority(
        run_id=matcher.run_id,
        spec_set=matcher.spec_set,
        snapshot_view=source_ports["frontier"].current_snapshot,
    )

    recovered, recovered_ports = _recover_read_only_dispatch(
        matcher=matcher,
        reconciliation=fresh_reconciliation,
        runtime=runtime,
        journal=journal,
    )

    assert recovered.state == coordinator.state
    assert recovered_ports["frontier"].previous_refresh_sha256 == (
        source_ports["frontier"].previous_refresh_sha256
    )
    assert recovered._active is None
    assert uninterrupted.observation_sha256 == active.lease.root.observation_sha256


def test_completed_journal_with_wrong_trace_fails_before_recovery_side_effects(
    tmp_path: Path,
) -> None:
    coordinator, matcher, reconciliation, _ports, runtime, journal, _root_value = (
        _journal_bound_read_only_dispatch(tmp_path, write_trace=True)
    )
    active = coordinator._capture_lease(runtime.pop())
    coordinator._active = active
    drive_read_only(coordinator, active, complete=True)
    document = json.loads(runtime.trace_records[0])
    document["root_order_key"] = []
    record = json.dumps(
        document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    runtime.trace_records = (record,)
    retained_count = len(journal.records)
    acknowledgement_calls = runtime.acknowledgement_calls
    recovery_ports = _ledger_ports(matcher, first_sequence=1, first_previous_refresh_sha256=None)

    with pytest.raises(RuntimeOrderingError, match="values conflict"):
        recover_phase1_lifecycle_coordinator(
            binding=journal.binding,
            audit=journal,
            runtime=runtime,
            matcher=matcher,
            fact_authority=_NoFacts(matcher),
            evidence_resolver=_FixedFillEvidence(None),
            records=journal.recovery_records,
            reconciliation_authority=reconciliation,
            **recovery_ports,
        )

    assert len(journal.records) == retained_count
    assert runtime.acknowledgement_calls == acknowledgement_calls
    assert recovery_ports["frontier"].previous_refresh_sha256 is None


def test_completed_journal_with_substituted_outcome_fails_before_recovery_side_effects(
    tmp_path: Path,
) -> None:
    coordinator, matcher, reconciliation, _ports, runtime, journal, _root_value = (
        _journal_bound_read_only_dispatch(tmp_path, write_trace=True)
    )
    active = coordinator._capture_lease(runtime.pop())
    coordinator._active = active
    drive_read_only(coordinator, active, complete=True)
    assert active.read_only is not None
    assert active.read_only.outcome is not None
    object.__setattr__(
        active.read_only.outcome,
        "local_snapshot_version",
        active.read_only.outcome.local_snapshot_version + 1,
    )
    retained_count = len(journal.records)
    acknowledgement_calls = runtime.acknowledgement_calls
    recovery_ports = _ledger_ports(matcher, first_sequence=1, first_previous_refresh_sha256=None)

    with pytest.raises(LifecycleError, match="payload conflicts"):
        recover_phase1_lifecycle_coordinator(
            binding=journal.binding,
            audit=journal,
            runtime=runtime,
            matcher=matcher,
            fact_authority=_NoFacts(matcher),
            evidence_resolver=_FixedFillEvidence(None),
            records=journal.recovery_records,
            reconciliation_authority=reconciliation,
            **recovery_ports,
        )

    assert len(journal.records) == retained_count
    assert runtime.acknowledgement_calls == acknowledgement_calls
    assert recovery_ports["frontier"].previous_refresh_sha256 is None
