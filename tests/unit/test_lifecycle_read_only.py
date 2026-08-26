from __future__ import annotations

from typing import Any

import pytest

from ea.core import (
    OutcomeCode,
    create_reconciliation_observation_root,
)
from ea.core.lifecycle import (
    LifecycleError,
    ReadOnlyReconciliationDispatchOutcome,
    ReadOnlyReconciliationDispatchWindow,
)
from ea.core.run import RunBinding, RunReference, Sha256Digest
from ea.reconciliation.authority import _create_observation_only_reconciliation_authority
from unit.test_historical_matcher import _system
from unit.test_lifecycle_composition import _staged_lifecycle
from unit.test_lifecycle_coordinator import (
    _Lease,
    _MemoryAudit,
    _NoFacts,
    _Runtime,
    create_phase1_lifecycle_coordinator,
)
from unit.test_lifecycle_ledger_gate import _FixedFillEvidence, _ledger_ports
from unit.test_reconciliation_authority import _observation


class _CountingFrontier:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.advance_calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def advance(self, **values: Any) -> None:
        self.advance_calls += 1
        self.inner.advance(**values)


class _CountingRuntime(_Runtime):
    acknowledgement_calls = 0
    fail_once = False

    def acknowledge(self, lease: Any) -> None:
        self.acknowledgement_calls += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("injected acknowledgement failure")
        super().acknowledge(lease)


def _read_only_coordinator(
    matcher: Any,
    root: Any,
    *,
    dispatch_sequence: int = 1,
    fail_once: bool = False,
) -> tuple[Any, _MemoryAudit, dict[str, Any], _CountingRuntime]:
    ports = _ledger_ports(
        matcher,
        first_sequence=dispatch_sequence,
        first_previous_refresh_sha256=(None if dispatch_sequence == 1 else Sha256Digest("33" * 32)),
    )
    ports["frontier"] = _CountingFrontier(ports["frontier"])
    reconciliation = _create_observation_only_reconciliation_authority(
        run_id=matcher.run_id,
        spec_set=matcher.spec_set,
        snapshot_view=ports["frontier"].current_snapshot,
    )
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)), Sha256Digest("22" * 32)
    )
    audit = _MemoryAudit(binding)
    runtime = _CountingRuntime(matcher, root, dispatch_sequence=dispatch_sequence)
    runtime.fail_once = fail_once
    coordinator = create_phase1_lifecycle_coordinator(
        binding=binding,
        audit=audit,
        runtime=runtime,
        matcher=matcher,
        fact_authority=_NoFacts(matcher),
        evidence_resolver=_FixedFillEvidence(None),
        reconciliation_authority=reconciliation,
        **ports,
    )
    return coordinator, audit, ports, runtime


def _root(matcher: Any, *, source_sequence: int = 7, observation_sequence: int = 1) -> Any:
    return create_reconciliation_observation_root(
        _observation(
            spec_set=matcher.spec_set,
            source_sequence=source_sequence,
            observation_sequence=observation_sequence,
        )
    )


def _effect_snapshot(
    coordinator: Any, audit: _MemoryAudit, frontier: _CountingFrontier
) -> tuple[Any, ...]:
    return len(audit.records), coordinator._state, coordinator._active, frontier.advance_calls


def test_rank_20_position_observation_settles_to_a_read_only_outcome() -> None:
    _fixture, matcher, _orders, _causal, _delayed, _end = _system()
    root = _root(matcher)
    coordinator, _audit, _ports, _runtime = _read_only_coordinator(matcher, root)

    outcome = coordinator.process_next_dispatch()

    assert type(outcome) is ReadOnlyReconciliationDispatchOutcome
    assert outcome.observation_sha256 == root.observation_sha256
    assert outcome.runtime_acknowledged is True


def test_retained_read_only_window_completes_and_publishes_only_once() -> None:
    _fixture, matcher, _orders, _causal, _delayed, _end = _system()
    coordinator, _audit, ports, runtime = _read_only_coordinator(matcher, _root(matcher))

    window = coordinator.begin_next_dispatch()

    with pytest.raises(LifecycleError, match="completion-only retry"):
        coordinator.retry_active_dispatch_completion()
    assert type(window) is ReadOnlyReconciliationDispatchWindow
    assert ports["frontier"].advance_calls == 1
    assert coordinator.resume_active_dispatch() is window
    outcome = coordinator.complete_active_dispatch(window)
    assert type(outcome) is ReadOnlyReconciliationDispatchOutcome
    assert ports["frontier"].advance_calls == 1
    assert runtime.acknowledgement_calls == 1


@pytest.mark.parametrize(
    ("source_sequence", "other_sequence"),
    [
        (7, 1),
        (7, 2),
        (8, 1),
    ],
)
def test_read_only_completion_rejects_nonretained_windows_before_effects(
    source_sequence: int, other_sequence: int
) -> None:
    _fixture, matcher, _orders, _causal, _delayed, _end = _system()
    coordinator, audit, ports, runtime = _read_only_coordinator(matcher, _root(matcher))
    coordinator.begin_next_dispatch()
    other, _other_audit, _other_ports, _other_runtime = _read_only_coordinator(
        matcher,
        _root(matcher, source_sequence=source_sequence, observation_sequence=other_sequence),
        dispatch_sequence=other_sequence,
    )
    candidate = other.begin_next_dispatch()
    before = _effect_snapshot(coordinator, audit, ports["frontier"])

    with pytest.raises(LifecycleError, match="active dispatch window conflicts") as captured:
        coordinator.complete_active_dispatch(candidate)

    assert captured.value.code is OutcomeCode.CONFLICTING_ID
    assert _effect_snapshot(coordinator, audit, ports["frontier"]) == before
    assert runtime.acknowledgement_calls == 0


def test_read_only_completion_rejects_arbitrary_and_stale_windows_before_effects() -> None:
    _fixture, matcher, _orders, _causal, _delayed, _end = _system()
    second = _root(matcher, source_sequence=8, observation_sequence=2)
    coordinator, audit, ports, runtime = _read_only_coordinator(matcher, _root(matcher))
    stale = coordinator.begin_next_dispatch()
    coordinator.complete_active_dispatch(stale)
    runtime._lease = _Lease(second, 2)
    current = coordinator.begin_next_dispatch()
    before = _effect_snapshot(coordinator, audit, ports["frontier"])

    for candidate in (object(), stale):
        with pytest.raises(LifecycleError, match="active dispatch window conflicts"):
            coordinator.complete_active_dispatch(candidate)
        assert _effect_snapshot(coordinator, audit, ports["frontier"]) == before
    assert runtime.acknowledgement_calls == 1
    assert current is coordinator._active.read_only.window


def test_read_only_completion_retry_does_not_republish_refresh() -> None:
    _fixture, matcher, _orders, _causal, _delayed, _end = _system()
    coordinator, _audit, ports, runtime = _read_only_coordinator(
        matcher, _root(matcher), fail_once=True
    )

    with pytest.raises(RuntimeError, match="injected acknowledgement failure"):
        coordinator.complete_active_dispatch(coordinator.begin_next_dispatch())
    coordinator.retry_active_dispatch_completion()

    assert ports["frontier"].advance_calls == 1
    assert runtime.acknowledgement_calls == 2


def test_composition_keeps_the_read_only_authority_private() -> None:
    lifecycle, *_ = _staged_lifecycle()

    coordinator = lifecycle.coordinator._Phase1HistoricalLifecycleCoordinatorFacade__coordinator

    assert coordinator._reconciliation_authority is not None
