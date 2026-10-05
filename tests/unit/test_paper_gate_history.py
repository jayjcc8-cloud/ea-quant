"""Whole-interval Paper gate aggregation over explicit durable interval evidence.

One focused acceptance group for the interval contract: an earlier failure or an
interrupted attempt can never be erased by a later healthy snapshot; a fully
covered, clean, identity-matched interval passes; missing, corrupt, ambiguous,
foreign or unresolved provenance is a gap or an exclusion, never a pass; the
boundary observation must be of the instant the window closes and of a run this
window's own coverage rests on; and the terminal reasons the runtime itself
records as failed decide FAIL rather than pending work alone.

Every backup below is a real captured attempt built through the existing store,
audit-journal and backup owners, so the interval reader is exercised against the
real on-disk evidence shape rather than a stub.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ea.core import RunBinding, RunId, RunReference, Sha256Digest
from ea.core.audit import AuditRecordKind, AuditSubjectKind, audit_subject_digest
from ea.core.outcomes import OutcomeCode
from ea.core.paper import canonical_paper_terminal_payload
from ea.experiments.audit import create_posix_audit_journal
from ea.experiments.store import CanonicalAttemptManifest, LocalResultStore
from ea.product.paper_backup import create_paper_backup
from ea.product.paper_evidence import (
    AlertSummary,
    BackupSummary,
    BrokerMode,
    GateType,
    GateVerdict,
    PaperEvidenceError,
    PaperGateIdentity,
    PaperGateVerdict,
    PaperSnapshot,
    SupervisorState,
    build_gate_identity,
    build_paper_snapshot,
    evaluate_snapshot_health,
)
from ea.product.paper_evidence import evaluate_gate as evaluate_gate_entry
from ea.product.paper_gate_history import (
    FAIL_ATTEMPT_FAILED,
    FAIL_ATTEMPT_INTERRUPTED,
    GAP_AMBIGUOUS_ASSOCIATION,
    GAP_CORRUPT_EVIDENCE,
    GAP_COVERAGE,
    GAP_EVIDENCE,
    GAP_MISSING_EVIDENCE,
    GAP_PROVENANCE,
    GAP_SNAPSHOT_RUN_NOT_REPRESENTED,
    GAP_UNASSOCIATED_EVIDENCE,
    REQUIRED_DURATIONS,
    AttemptEvidenceState,
    PaperAttemptAssociation,
    PaperAttemptEvidence,
    PaperIntervalHistory,
    canonical_paper_interval_history_bytes,
    decode_paper_interval_history,
    evaluate_gate,
    read_attempt_evidence,
    required_duration,
)
from ea.product.paper_health import (
    BrokerState,
    KillSwitchProjectionState,
    MarketState,
    PaperHealthSnapshot,
    ReconciliationState,
    RecoveryState,
    RuntimeState,
    StorageState,
    StrategyHeartbeatState,
    TradePermissionBasis,
)
from ea.product.paper_session import PaperSessionWriter

INTERRUPTED_RUN = RunId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
FAILED_RUN = RunId("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
OTHER_RUN = RunId("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
LINEAGE = Sha256Digest("1" * 64)
UNCAPTURED_MANIFEST = Sha256Digest("9" * 64)
REPO_SHA = "a" * 40
OTHER_SHA = "b" * 40
CONFIG_BYTES = b"EA_BIN=/usr/local/bin/ea\nWORKSPACE=/workspace\n"
OTHER_CONFIG_BYTES = CONFIG_BYTES + b"PRICES=99\n"
LAUNCHD_BYTES = b"<plist><key>Label</key><string>com.ea.paper</string></plist>\n"
STARTED_AT = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)
MIDPOINT = STARTED_AT + timedelta(hours=24)
WINDOW_END = STARTED_AT + timedelta(hours=72)

# The runtime's own stop vocabulary: only a deliberate operator stop closes an
# interval cleanly; every other reason it can record is its own failed ending.
CLEAN_STOP_REASON = "stop_requested"
CAPACITY_REASON = "audit_capacity_exhausted"
FAILED_RUNTIME_REASONS = (
    "source_exhausted",
    "source_stalled",
    "stale_market",
    "callback_error",
    "source_error",
)
UNKNOWN_REASON = "future_unrecognized_reason"

# terminal shapes a real attempt's journal can durably hold
PAPER_STOP = "paper_stop"
PAPER_REASON = "paper_reason"
PAPER_CAPACITY = "paper_capacity"
COORDINATOR_SUCCESS = "coordinator_success"
COORDINATOR_FAILED = "coordinator_failed"
NO_TERMINAL = "none"

# Every field of the per-association identity, drifted one at a time.
FOREIGN_IDENTITIES: tuple[dict[str, Any], ...] = (
    {"gate_id": "M4-72H-002"},
    {"gate_type": GateType.T7D},
    {"repo_sha": OTHER_SHA},
    {"started_at": STARTED_AT - timedelta(hours=1)},
    {"runtime_profile": "local-simulated-paper-v2"},
    {"evidence_schema_version": "2"},
    {"config_bytes": OTHER_CONFIG_BYTES},
    {"launchd_bytes": LAUNCHD_BYTES + b"<!-- drift -->\n"},
)


def _canonical(document: object) -> bytes:
    return json.dumps(
        document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


# The small durable side records a real attempt keeps beside its manifest. The
# capture holds them only to bounded, non-shared-writable regular files, so the
# fixture publishes them 0600 exactly as the product does.
_SIDE_RECORDS = ("funding.json", "kill-switch.json", "operational-safety.json")


def _write_side_records(run_dir: Path) -> None:
    for name in _SIDE_RECORDS:
        path = run_dir / name
        path.write_bytes(_canonical({"record": name, "state": "recorded"}))
        path.chmod(0o600)


def _terminal_payload(
    run_id: RunId, *, reason: str, pending_facts: int = 0, pending_orders: int = 0
) -> bytes:
    """One real v1 Paper terminal payload: exactly the shape the runtime appends."""
    return canonical_paper_terminal_payload(
        run_id,
        dispatch_sequence=1,
        reason=reason,
        portfolio_snapshot_sha256=Sha256Digest("c" * 64),
        risk_state_sha256=Sha256Digest("d" * 64),
        pending_facts=pending_facts,
        pending_orders=pending_orders,
        fill_count=6,
    )


def _coordinator_terminal(run_id: RunId, *, kind: str) -> bytes:
    return _canonical(
        {
            "canonicalization": "ea-canonical-json-v1",
            "last_dispatch_sequence": 1,
            "last_trigger_root_sha256": "e" * 64,
            "pre_terminal_state_sha256": "f" * 64,
            "previous_chain_head_sha256": "0" * 64,
            "run_id": run_id.value,
            "schema": "ea.audit-run-terminal.v1",
            "terminal_kind": kind,
        }
    )


def _terminal_for(run_id: RunId, shape: str, reason: str | None) -> bytes | None:
    if shape == PAPER_STOP:
        return _terminal_payload(run_id, reason=CLEAN_STOP_REASON)
    if shape == PAPER_REASON:
        assert reason is not None
        return _terminal_payload(run_id, reason=reason)
    if shape == PAPER_CAPACITY:
        return _terminal_payload(run_id, reason=CAPACITY_REASON, pending_facts=1, pending_orders=1)
    if shape == COORDINATOR_SUCCESS:
        return _coordinator_terminal(run_id, kind="success")
    if shape == COORDINATOR_FAILED:
        return _coordinator_terminal(run_id, kind="failed")
    assert shape == NO_TERMINAL
    return None


def _build_attempt(
    root: Path,
    run_id: RunId,
    *,
    shape: str,
    started_at: datetime,
    reason: str | None = None,
) -> Any:
    """Prepare one real attempt, write its terminal shape, then release the lease."""
    results = root / "results"
    results.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = LocalResultStore(results.resolve())
    run_dir = results.resolve() / run_id.value
    try:
        prepared = store.prepare_canonical_attempt(
            CanonicalAttemptManifest(
                RunReference(run_id, LINEAGE),
                _canonical(
                    {
                        "schema": "ea.local-paper-attempt.v1",
                        "run_id": run_id.value,
                        "lineage_sha256": LINEAGE.value,
                        "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                        "operation": "paper.start",
                    }
                ),
            )
        )
        journal = create_posix_audit_journal(prepared.audit)
        try:
            payload = _terminal_for(run_id, shape, reason)
            if payload is not None:
                journal.append(
                    record_kind=AuditRecordKind.RUN_TERMINAL,
                    subject_kind=AuditSubjectKind.RUN_TERMINAL_STATE,
                    subject_sha256=audit_subject_digest(AuditRecordKind.RUN_TERMINAL, payload),
                    canonical_payload=payload,
                )
        finally:
            journal.close()
        PaperSessionWriter(run_dir, prepared.audit.binding).publish({"state": "stopped"})
        _write_side_records(run_dir)
        binding = prepared.audit.binding
    finally:
        store.close()
    return run_dir, binding


def _attempt(
    tmp_path: Path,
    run_id: RunId,
    *,
    shape: str,
    name: str,
    started_at: datetime = STARTED_AT,
    reason: str | None = None,
) -> PaperAttemptEvidence:
    """One real captured backup whose provenance the product's own reader verifies."""
    run_dir, binding = _build_attempt(
        tmp_path / name, run_id, shape=shape, started_at=started_at, reason=reason
    )
    result = create_paper_backup(run_dir, tmp_path / name / "backups")
    assert result.manifest.run_id == run_id
    assert result.manifest.attempt_manifest_sha256 == binding.manifest_sha256
    return PaperAttemptEvidence(binding=binding, backup_dir=result.backup_dir)


def _missing(tmp_path: Path, binding: RunBinding) -> PaperAttemptEvidence:
    return PaperAttemptEvidence(binding=binding, backup_dir=tmp_path / "never-captured")


def _uncaptured(run_id: RunId, tmp_path: Path) -> PaperAttemptEvidence:
    """Evidence for an attempt this fixture never builds and never captures."""
    return PaperAttemptEvidence(
        binding=RunBinding(RunReference(run_id, LINEAGE), UNCAPTURED_MANIFEST),
        backup_dir=tmp_path / "uncaptured-attempt",
    )


def _association(
    evidence: PaperAttemptEvidence,
    begin: datetime,
    end: datetime,
    identity: PaperGateIdentity | None,
) -> PaperAttemptAssociation:
    return PaperAttemptAssociation(
        binding=evidence.binding, begin=begin, end=end, identity=identity
    )


def _history(
    identity: PaperGateIdentity,
    *,
    attempts: tuple[PaperAttemptEvidence, ...],
    associations: tuple[PaperAttemptAssociation, ...],
    coverage_start: datetime = STARTED_AT,
    coverage_end: datetime = WINDOW_END,
) -> PaperIntervalHistory:
    return PaperIntervalHistory(
        identity=identity,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        attempts=attempts,
        associations=associations,
    )


def _locked(**overrides: Any) -> PaperGateIdentity:
    values: dict[str, Any] = {
        "gate_id": "M4-72H-001",
        "gate_type": GateType.T72H,
        "repo_sha": REPO_SHA,
        "config_bytes": CONFIG_BYTES,
        "launchd_bytes": LAUNCHD_BYTES,
        "started_at": STARTED_AT,
    }
    values.update(overrides)
    return build_gate_identity(**values)


def _current(locked: PaperGateIdentity, **overrides: Any) -> PaperGateIdentity:
    values: dict[str, Any] = {
        "gate_id": locked.gate_id,
        "gate_type": locked.gate_type,
        "repo_sha": locked.repo_sha,
        "config_bytes": CONFIG_BYTES,
        "launchd_bytes": LAUNCHD_BYTES,
        "started_at": STARTED_AT,
    }
    values.update(overrides)
    return build_gate_identity(**values)


def _healthy_snapshot(run_id: RunId = INTERRUPTED_RUN, **overrides: Any) -> PaperSnapshot:
    captured_at = overrides.pop("captured_at", WINDOW_END)
    health = PaperHealthSnapshot(
        process_alive=True,
        runtime_ready=True,
        trade_permitted=True,
        runtime_state=RuntimeState.RUNNING,
        recovery_state=RecoveryState.NOT_REQUIRED,
        reconciliation_state=ReconciliationState.CLEAN,
        market_state=MarketState.FRESH,
        broker_state=BrokerState.AVAILABLE,
        strategy_heartbeat_state=StrategyHeartbeatState.LIVE,
        storage_state=StorageState.AVAILABLE,
        kill_switch_state=KillSwitchProjectionState.ACTIVE,
        operator_halt=False,
        reason_codes=(),
        trade_blocking_guard=None,
        trade_reason=None,
        trade_permission_basis=TradePermissionBasis.ZERO_EFFECT_PROBE,
        trade_permission_unexercised_guards=("price_deviation",),
        observed_at=captured_at,
        run_id=run_id,
        candidate_id="candidate-1",
    )
    values: dict[str, Any] = {
        "captured_at": captured_at,
        "repo_sha": REPO_SHA,
        "run_id": run_id,
        "supervisor_state": SupervisorState.RUNNING,
        "writer_lease_held": True,
        "broker_mode": BrokerMode.LOCAL_SIMULATED_PAPER,
        "live_enabled": False,
        "alert_state": AlertSummary.CLEAN,
        "backup_state": BackupSummary.FRESH,
        "health": health,
    }
    values.update(overrides)
    return build_paper_snapshot(**values)


def _evaluate(
    history: object,
    locked: PaperGateIdentity,
    *,
    snapshot: PaperSnapshot | None = None,
    evaluated_at: datetime = WINDOW_END,
    current: PaperGateIdentity | None = None,
) -> PaperGateVerdict:
    return evaluate_gate(
        _healthy_snapshot() if snapshot is None else snapshot,
        locked,
        _current(locked) if current is None else current,
        history,  # type: ignore[arg-type]
        evaluated_at=evaluated_at,
    )


# ---------------------------------------------------------------------------
# certain incidents survive a later healthy snapshot
# ---------------------------------------------------------------------------


def test_interrupted_interval_is_a_certain_failure(tmp_path: Path) -> None:
    """A captured association with no durable terminal is an interruption, not a gap."""
    locked = _locked()
    interrupted = _attempt(tmp_path, INTERRUPTED_RUN, shape=NO_TERMINAL, name="interrupted")
    record = read_attempt_evidence(interrupted.backup_dir, interrupted.binding)
    assert record.state is AttemptEvidenceState.INCOMPLETE
    assert record.terminal_reason is None
    verdict = _evaluate(
        _history(
            locked,
            attempts=(interrupted,),
            associations=(_association(interrupted, STARTED_AT, WINDOW_END, locked),),
        ),
        locked,
    )
    assert verdict.verdict is GateVerdict.FAIL
    assert verdict.reasons == (FAIL_ATTEMPT_INTERRUPTED,)


def test_durable_capacity_stop_with_pending_work_is_a_certain_failure(tmp_path: Path) -> None:
    """A durable terminal retaining pending work is the recorded incident shape."""
    locked = _locked()
    stopped = _attempt(tmp_path, INTERRUPTED_RUN, shape=PAPER_CAPACITY, name="capacity")
    verdict = _evaluate(
        _history(
            locked,
            attempts=(stopped,),
            associations=(_association(stopped, STARTED_AT, WINDOW_END, locked),),
        ),
        locked,
    )
    assert verdict.verdict is GateVerdict.FAIL
    assert verdict.reasons == (FAIL_ATTEMPT_FAILED,)


def test_durable_capacity_stop_without_pending_work_is_still_a_certain_failure(
    tmp_path: Path,
) -> None:
    """An exhausted audit envelope is the runtime's own failed end, pending or not."""
    locked = _locked()
    stopped = _attempt(
        tmp_path,
        INTERRUPTED_RUN,
        shape=PAPER_REASON,
        name="capacity-empty",
        reason=CAPACITY_REASON,
    )
    record = read_attempt_evidence(stopped.backup_dir, stopped.binding)
    assert record.state is AttemptEvidenceState.FAILED
    verdict = _evaluate(
        _history(
            locked,
            attempts=(stopped,),
            associations=(_association(stopped, STARTED_AT, WINDOW_END, locked),),
        ),
        locked,
    )
    assert verdict.verdict is GateVerdict.FAIL
    assert verdict.reasons == (FAIL_ATTEMPT_FAILED,)


@pytest.mark.parametrize("reason", FAILED_RUNTIME_REASONS)
def test_recorded_runtime_failure_reasons_are_certain_failures(tmp_path: Path, reason: str) -> None:
    """Every reason the runtime records as failed stays an incident with nothing pending."""
    locked = _locked()
    stopped = _attempt(
        tmp_path, INTERRUPTED_RUN, shape=PAPER_REASON, name=f"stopped-{reason}", reason=reason
    )
    record = read_attempt_evidence(stopped.backup_dir, stopped.binding)
    assert record.state is AttemptEvidenceState.FAILED
    assert record.terminal_reason == reason
    verdict = _evaluate(
        _history(
            locked,
            attempts=(stopped,),
            associations=(_association(stopped, STARTED_AT, WINDOW_END, locked),),
        ),
        locked,
    )
    assert verdict.verdict is GateVerdict.FAIL
    assert verdict.reasons == (FAIL_ATTEMPT_FAILED,)


def test_unknown_terminal_reason_is_a_gap_not_a_clean_stop(tmp_path: Path) -> None:
    """A reason outside the runtime's own vocabulary is never read as success."""
    locked = _locked()
    unknown = _attempt(
        tmp_path, INTERRUPTED_RUN, shape=PAPER_REASON, name="unknown", reason=UNKNOWN_REASON
    )
    verdict = _evaluate(
        _history(
            locked,
            attempts=(unknown,),
            associations=(_association(unknown, STARTED_AT, WINDOW_END, locked),),
        ),
        locked,
    )
    assert verdict.verdict is GateVerdict.INVALID
    assert verdict.reasons == (GAP_CORRUPT_EVIDENCE,)


def test_failed_terminal_is_a_certain_failure(tmp_path: Path) -> None:
    """A durable terminal that says the run failed can never be relabelled."""
    locked = _locked()
    failed = _attempt(tmp_path, FAILED_RUN, shape=COORDINATOR_FAILED, name="failed")
    verdict = _evaluate(
        _history(
            locked,
            attempts=(failed,),
            associations=(_association(failed, STARTED_AT, WINDOW_END, locked),),
        ),
        locked,
        snapshot=_healthy_snapshot(FAILED_RUN),
    )
    assert verdict.verdict is GateVerdict.FAIL
    assert verdict.reasons == (FAIL_ATTEMPT_FAILED,)


def test_a_deliberate_stop_inside_the_window_is_an_interruption(tmp_path: Path) -> None:
    """A clean stop that ends before the window closes ends the gate interval there."""
    locked = _locked()
    stopped = _attempt(tmp_path, INTERRUPTED_RUN, shape=PAPER_STOP, name="stopped")
    later = _attempt(tmp_path, OTHER_RUN, shape=COORDINATOR_SUCCESS, name="later")
    assert read_attempt_evidence(stopped.backup_dir, stopped.binding).state is (
        AttemptEvidenceState.CLEAN
    )
    verdict = _evaluate(
        _history(
            locked,
            attempts=(stopped, later),
            associations=(
                _association(stopped, STARTED_AT, MIDPOINT, locked),
                _association(later, MIDPOINT, WINDOW_END, locked),
            ),
        ),
        locked,
        snapshot=_healthy_snapshot(OTHER_RUN),
    )
    assert verdict.verdict is GateVerdict.FAIL
    assert verdict.reasons == (FAIL_ATTEMPT_INTERRUPTED,)


def test_old_failure_is_not_erased_by_a_new_healthy_snapshot(tmp_path: Path) -> None:
    """The whole point: a clean later attempt and healthy snapshot keep the incident."""
    locked = _locked()
    old = _attempt(tmp_path, INTERRUPTED_RUN, shape=NO_TERMINAL, name="old")
    new = _attempt(tmp_path, OTHER_RUN, shape=PAPER_STOP, name="new")
    history = _history(
        locked,
        attempts=(old, new),
        associations=(
            _association(old, STARTED_AT, MIDPOINT, locked),
            _association(new, MIDPOINT, WINDOW_END, locked),
        ),
    )
    assert read_attempt_evidence(new.backup_dir, new.binding).state is AttemptEvidenceState.CLEAN
    verdict = _evaluate(history, locked, snapshot=_healthy_snapshot(OTHER_RUN))
    assert verdict.verdict is GateVerdict.FAIL
    assert FAIL_ATTEMPT_INTERRUPTED in verdict.reasons


def test_zero_pending_failed_attempt_is_not_erased_by_a_later_healthy_run(
    tmp_path: Path,
) -> None:
    """A failed attempt with nothing pending is still an incident, later run or not."""
    locked = _locked()
    old = _attempt(
        tmp_path,
        INTERRUPTED_RUN,
        shape=PAPER_REASON,
        name="old-failed",
        reason=FAILED_RUNTIME_REASONS[0],
    )
    new = _attempt(tmp_path, OTHER_RUN, shape=COORDINATOR_SUCCESS, name="new-healthy")
    history = _history(
        locked,
        attempts=(old, new),
        associations=(
            _association(old, STARTED_AT, MIDPOINT, locked),
            _association(new, MIDPOINT, WINDOW_END, locked),
        ),
    )
    verdict = _evaluate(history, locked, snapshot=_healthy_snapshot(OTHER_RUN))
    assert verdict.verdict is GateVerdict.FAIL
    assert verdict.reasons == (FAIL_ATTEMPT_FAILED,)


def test_gap_reasons_survive_alongside_a_certain_failure(tmp_path: Path) -> None:
    """A missing second attempt is a gap; the known interruption is still certain."""
    locked = _locked()
    interrupted = _attempt(tmp_path, INTERRUPTED_RUN, shape=NO_TERMINAL, name="interrupted")
    clean = _attempt(tmp_path, OTHER_RUN, shape=PAPER_STOP, name="clean")
    history = _history(
        locked,
        attempts=(interrupted, _missing(tmp_path, clean.binding)),
        associations=(
            _association(interrupted, STARTED_AT, MIDPOINT, locked),
            _association(clean, MIDPOINT, WINDOW_END, locked),
        ),
    )
    verdict = _evaluate(history, locked)
    assert verdict.verdict is GateVerdict.FAIL
    assert FAIL_ATTEMPT_INTERRUPTED in verdict.reasons
    assert GAP_MISSING_EVIDENCE in verdict.reasons


# ---------------------------------------------------------------------------
# the passing shapes
# ---------------------------------------------------------------------------


def test_fully_covered_clean_interval_passes(tmp_path: Path) -> None:
    """Complete explicit coverage over one clean attempt is the only pass."""
    locked = _locked()
    clean = _attempt(tmp_path, INTERRUPTED_RUN, shape=PAPER_STOP, name="clean")
    assert read_attempt_evidence(clean.backup_dir, clean.binding).state is (
        AttemptEvidenceState.CLEAN
    )
    history = _history(
        locked,
        attempts=(clean,),
        associations=(_association(clean, STARTED_AT, WINDOW_END, locked),),
    )
    verdict = _evaluate(history, locked)
    assert verdict.verdict is GateVerdict.PASS
    assert verdict.reasons == ()
    assert verdict.gate_id == locked.gate_id and verdict.gate_type is GateType.T72H


def test_adjacent_complete_attempts_pass(tmp_path: Path) -> None:
    """Two completed coordinator runs, each with its own clean terminal, span the window."""
    locked = _locked()
    first = _attempt(tmp_path, INTERRUPTED_RUN, shape=COORDINATOR_SUCCESS, name="first")
    second = _attempt(tmp_path, OTHER_RUN, shape=COORDINATOR_SUCCESS, name="second")
    history = _history(
        locked,
        attempts=(first, second),
        associations=(
            _association(first, STARTED_AT, MIDPOINT, locked),
            _association(second, MIDPOINT, WINDOW_END, locked),
        ),
    )
    verdict = _evaluate(history, locked, snapshot=_healthy_snapshot(OTHER_RUN))
    assert verdict.verdict is GateVerdict.PASS
    assert verdict.reasons == ()


def test_a_healthier_later_attempt_cannot_pass_a_failed_window(tmp_path: Path) -> None:
    """A clean attempt covering only part of the window leaves an uncovered gap."""
    locked = _locked()
    clean = _attempt(tmp_path, INTERRUPTED_RUN, shape=PAPER_STOP, name="clean")
    history = _history(
        locked,
        attempts=(clean,),
        associations=(_association(clean, MIDPOINT, WINDOW_END, locked),),
    )
    verdict = _evaluate(history, locked)
    assert verdict.verdict is GateVerdict.INVALID
    assert GAP_COVERAGE in verdict.reasons


def test_a_coverage_hole_between_two_clean_attempts_cannot_pass(tmp_path: Path) -> None:
    """Claimed coverage that spans the window is not enough; the union must too."""
    locked = _locked()
    first = _attempt(tmp_path, INTERRUPTED_RUN, shape=COORDINATOR_SUCCESS, name="first")
    second = _attempt(tmp_path, OTHER_RUN, shape=COORDINATOR_SUCCESS, name="second")
    hole = timedelta(hours=1)
    history = _history(
        locked,
        attempts=(first, second),
        associations=(
            _association(first, STARTED_AT, MIDPOINT - hole, locked),
            _association(second, MIDPOINT + hole, WINDOW_END, locked),
        ),
    )
    assert history.coverage_start == STARTED_AT and history.coverage_end == WINDOW_END
    verdict = _evaluate(history, locked, snapshot=_healthy_snapshot(OTHER_RUN))
    assert verdict.verdict is GateVerdict.INVALID
    assert GAP_COVERAGE in verdict.reasons


# ---------------------------------------------------------------------------
# gaps: unavailable, corrupt, ambiguous, unassociated, absent history
# ---------------------------------------------------------------------------


def test_missing_evidence_is_a_gap(tmp_path: Path) -> None:
    locked = _locked()
    clean = _attempt(tmp_path, INTERRUPTED_RUN, shape=PAPER_STOP, name="clean")
    absent = _missing(tmp_path, clean.binding)
    verdict = _evaluate(
        _history(
            locked,
            attempts=(absent,),
            associations=(_association(absent, STARTED_AT, WINDOW_END, locked),),
        ),
        locked,
    )
    assert verdict.verdict is GateVerdict.INVALID
    assert verdict.reasons == (GAP_MISSING_EVIDENCE,)


def test_corrupt_evidence_is_a_gap(tmp_path: Path) -> None:
    """A tampered journal proves nothing; it is a gap, never a pass or an incident."""
    locked = _locked()
    clean = _attempt(tmp_path, INTERRUPTED_RUN, shape=PAPER_STOP, name="clean")
    journal = clean.backup_dir / "files" / "audit" / "audit-v1.journal"
    tampered = bytearray(journal.read_bytes())
    tampered[-1] ^= 0xFF
    journal.write_bytes(bytes(tampered))
    verdict = _evaluate(
        _history(
            locked,
            attempts=(clean,),
            associations=(_association(clean, STARTED_AT, WINDOW_END, locked),),
        ),
        locked,
    )
    assert verdict.verdict is GateVerdict.INVALID
    assert verdict.reasons == (GAP_CORRUPT_EVIDENCE,)


def test_ambiguous_association_is_a_gap(tmp_path: Path) -> None:
    locked = _locked()
    clean = _attempt(tmp_path, INTERRUPTED_RUN, shape=PAPER_STOP, name="clean")
    history = _history(
        locked,
        attempts=(clean,),
        associations=(
            _association(clean, STARTED_AT, MIDPOINT, locked),
            _association(clean, MIDPOINT, WINDOW_END, locked),
        ),
    )
    verdict = _evaluate(history, locked)
    assert verdict.verdict is GateVerdict.INVALID
    assert GAP_AMBIGUOUS_ASSOCIATION in verdict.reasons


def test_unassociated_inventory_is_a_gap(tmp_path: Path) -> None:
    locked = _locked()
    clean = _attempt(tmp_path, INTERRUPTED_RUN, shape=PAPER_STOP, name="clean")
    verdict = _evaluate(_history(locked, attempts=(clean,), associations=()), locked)
    assert verdict.verdict is GateVerdict.INVALID
    assert GAP_UNASSOCIATED_EVIDENCE in verdict.reasons


def test_absent_or_invalid_history_is_a_fail_closed_gap() -> None:
    """Without explicit interval history there is no interval evidence at all."""
    locked = _locked()
    for history in (None, "not-history", object()):
        verdict = _evaluate(history, locked)
        assert verdict.verdict is GateVerdict.INVALID
        assert verdict.reasons == (GAP_EVIDENCE,)


def test_omitted_interval_history_is_a_fail_closed_gap() -> None:
    """Both Gate entry points default the interval input to the explicit gap."""
    locked = _locked()
    for entry in (evaluate_gate, evaluate_gate_entry):
        verdict = entry(_healthy_snapshot(), locked, _current(locked), evaluated_at=WINDOW_END)
        assert verdict.verdict is GateVerdict.INVALID
        assert verdict.reasons == (GAP_EVIDENCE,)


# ---------------------------------------------------------------------------
# per-association identity provenance
# ---------------------------------------------------------------------------


def test_foreign_identity_evidence_is_excluded_without_being_read(tmp_path: Path) -> None:
    """An overlapping attempt under another frozen identity cannot touch this window."""
    locked = _locked()
    clean = _attempt(tmp_path, INTERRUPTED_RUN, shape=PAPER_STOP, name="clean")
    foreign_failed = _attempt(tmp_path, FAILED_RUN, shape=COORDINATOR_FAILED, name="foreign")
    foreign_identity = _locked(config_bytes=OTHER_CONFIG_BYTES)
    uncaptured = _uncaptured(OTHER_RUN, tmp_path)
    verdict = _evaluate(
        _history(
            locked,
            attempts=(clean, foreign_failed, uncaptured),
            associations=(
                _association(clean, STARTED_AT, WINDOW_END, locked),
                # A real failed record of another identity, overlapping the window.
                _association(foreign_failed, STARTED_AT, MIDPOINT, foreign_identity),
                # And a foreign association whose evidence was never even captured:
                # nothing under it is read, so it cannot become a missing-evidence gap.
                _association(uncaptured, MIDPOINT, WINDOW_END, foreign_identity),
            ),
        ),
        locked,
    )
    assert not uncaptured.backup_dir.exists()
    assert verdict.verdict is GateVerdict.PASS
    assert verdict.reasons == ()


@pytest.mark.parametrize("overrides", FOREIGN_IDENTITIES)
def test_foreign_identity_association_cannot_cover_the_window(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    """Every field of the per-association identity is compared, exactly."""
    locked = _locked()
    foreign = _uncaptured(FAILED_RUN, tmp_path)
    verdict = _evaluate(
        _history(
            locked,
            attempts=(foreign,),
            associations=(_association(foreign, STARTED_AT, WINDOW_END, _locked(**overrides)),),
        ),
        locked,
    )
    assert not foreign.backup_dir.exists()
    assert verdict.verdict is GateVerdict.INVALID
    assert verdict.reasons == (GAP_COVERAGE,)


def test_unknown_association_identity_is_a_provenance_gap(tmp_path: Path) -> None:
    """An unresolved association is never inferred from the run id or the backup."""
    locked = _locked()
    clean = _attempt(tmp_path, INTERRUPTED_RUN, shape=PAPER_STOP, name="clean")
    unresolved = _missing(tmp_path, clean.binding)
    verdict = _evaluate(
        _history(
            locked,
            attempts=(unresolved,),
            associations=(_association(unresolved, STARTED_AT, WINDOW_END, None),),
        ),
        locked,
    )
    assert verdict.verdict is GateVerdict.INVALID
    assert GAP_PROVENANCE in verdict.reasons
    assert GAP_MISSING_EVIDENCE not in verdict.reasons


def test_resolved_identity_is_required_for_a_clean_attempt_to_pass(tmp_path: Path) -> None:
    """The same clean attempt passes when its identity is stated and not when it is not."""
    locked = _locked()
    clean = _attempt(tmp_path, INTERRUPTED_RUN, shape=PAPER_STOP, name="clean")
    assert read_attempt_evidence(clean.backup_dir, clean.binding).state is (
        AttemptEvidenceState.CLEAN
    )
    resolved = _evaluate(
        _history(
            locked,
            attempts=(clean,),
            associations=(_association(clean, STARTED_AT, WINDOW_END, locked),),
        ),
        locked,
    )
    assert resolved.verdict is GateVerdict.PASS

    unresolved = _evaluate(
        _history(
            locked,
            attempts=(clean,),
            associations=(_association(clean, STARTED_AT, WINDOW_END, None),),
        ),
        locked,
    )
    assert unresolved.verdict is GateVerdict.INVALID
    assert GAP_PROVENANCE in unresolved.reasons


def test_unknown_identity_outside_the_window_does_not_pollute(tmp_path: Path) -> None:
    """Unresolved evidence wholly outside the window is excluded, not a gap."""
    locked = _locked()
    clean = _attempt(tmp_path, INTERRUPTED_RUN, shape=PAPER_STOP, name="clean")
    earlier = _attempt(
        tmp_path,
        OTHER_RUN,
        shape=NO_TERMINAL,
        name="earlier",
        started_at=STARTED_AT - timedelta(days=2),
    )
    verdict = _evaluate(
        _history(
            locked,
            attempts=(clean, earlier),
            associations=(
                _association(clean, STARTED_AT, WINDOW_END, locked),
                _association(earlier, STARTED_AT - timedelta(days=2), STARTED_AT, None),
            ),
        ),
        locked,
    )
    assert verdict.verdict is GateVerdict.PASS
    assert verdict.reasons == ()


# ---------------------------------------------------------------------------
# the boundary observation
# ---------------------------------------------------------------------------


def test_stale_snapshot_observation_cannot_certify_the_window(tmp_path: Path) -> None:
    """The boundary observation must be of the instant the window closes."""
    locked = _locked()
    clean = _attempt(tmp_path, INTERRUPTED_RUN, shape=PAPER_STOP, name="clean")
    history = _history(
        locked,
        attempts=(clean,),
        associations=(_association(clean, STARTED_AT, WINDOW_END, locked),),
    )
    fresh = _evaluate(history, locked)
    assert fresh.verdict is GateVerdict.PASS

    stale = _evaluate(history, locked, snapshot=_healthy_snapshot(captured_at=STARTED_AT))
    assert stale.verdict is GateVerdict.INVALID
    assert stale.reasons == (GAP_COVERAGE,)


def test_snapshot_run_must_cover_the_boundary_from_the_left(tmp_path: Path) -> None:
    """A run whose association stops before the window's end cannot stand for it."""
    locked = _locked()
    first = _attempt(tmp_path, INTERRUPTED_RUN, shape=COORDINATOR_SUCCESS, name="first")
    second = _attempt(tmp_path, OTHER_RUN, shape=COORDINATOR_SUCCESS, name="second")
    history = _history(
        locked,
        attempts=(first, second),
        associations=(
            _association(first, STARTED_AT, MIDPOINT, locked),
            _association(second, MIDPOINT, WINDOW_END, locked),
        ),
    )
    boundary = _evaluate(history, locked, snapshot=_healthy_snapshot(OTHER_RUN))
    assert boundary.verdict is GateVerdict.PASS

    outside = _evaluate(history, locked, snapshot=_healthy_snapshot(INTERRUPTED_RUN))
    assert outside.verdict is GateVerdict.INVALID
    assert outside.reasons == (GAP_SNAPSHOT_RUN_NOT_REPRESENTED,)


def test_snapshot_of_an_unrepresented_run_is_a_gap(tmp_path: Path) -> None:
    """A healthy snapshot whose run is not in the history cannot certify it."""
    locked = _locked()
    clean = _attempt(tmp_path, INTERRUPTED_RUN, shape=PAPER_STOP, name="clean")
    verdict = _evaluate(
        _history(
            locked,
            attempts=(clean,),
            associations=(_association(clean, STARTED_AT, WINDOW_END, locked),),
        ),
        locked,
        snapshot=_healthy_snapshot(FAILED_RUN),
    )
    assert verdict.verdict is GateVerdict.INVALID
    assert verdict.reasons == (GAP_SNAPSHOT_RUN_NOT_REPRESENTED,)


# ---------------------------------------------------------------------------
# exact time and identity exclusions
# ---------------------------------------------------------------------------


def test_earlier_incident_outside_the_window_is_excluded(tmp_path: Path) -> None:
    """An incident wholly before the window belongs to another gate, not this one."""
    locked = _locked()
    clean = _attempt(tmp_path, INTERRUPTED_RUN, shape=PAPER_STOP, name="clean")
    earlier = _attempt(
        tmp_path,
        OTHER_RUN,
        shape=NO_TERMINAL,
        name="earlier",
        started_at=STARTED_AT - timedelta(days=2),
    )
    history = _history(
        locked,
        attempts=(clean, earlier),
        associations=(
            _association(clean, STARTED_AT, WINDOW_END, locked),
            _association(earlier, STARTED_AT - timedelta(days=2), STARTED_AT, locked),
        ),
    )
    verdict = _evaluate(history, locked)
    assert verdict.verdict is GateVerdict.PASS
    assert verdict.reasons == ()
    assert earlier.binding != clean.binding


def test_earlier_incident_alone_cannot_cover_this_window(tmp_path: Path) -> None:
    locked = _locked()
    earlier = _attempt(
        tmp_path,
        OTHER_RUN,
        shape=NO_TERMINAL,
        name="earlier",
        started_at=STARTED_AT - timedelta(days=2),
    )
    verdict = _evaluate(
        _history(
            locked,
            attempts=(earlier,),
            associations=(
                _association(earlier, STARTED_AT - timedelta(days=2), STARTED_AT, locked),
            ),
        ),
        locked,
    )
    assert verdict.verdict is GateVerdict.INVALID
    assert verdict.reasons == (GAP_COVERAGE,)


def test_frozen_identity_drift_invalidates_the_interval(tmp_path: Path) -> None:
    """A moved frozen identity invalidates whatever the interval evidence shows."""
    locked = _locked()
    clean = _attempt(tmp_path, INTERRUPTED_RUN, shape=PAPER_STOP, name="clean")
    verdict = _evaluate(
        _history(
            locked,
            attempts=(clean,),
            associations=(_association(clean, STARTED_AT, WINDOW_END, locked),),
        ),
        locked,
        current=_current(locked, config_bytes=OTHER_CONFIG_BYTES),
    )
    assert verdict.verdict is GateVerdict.INVALID
    assert verdict.reasons == ("config_drift",)


def test_repo_sha_drift_still_precedes_interval_evaluation(tmp_path: Path) -> None:
    locked = _locked()
    clean = _attempt(tmp_path, INTERRUPTED_RUN, shape=PAPER_STOP, name="clean")
    verdict = _evaluate(
        _history(
            locked,
            attempts=(clean,),
            associations=(_association(clean, STARTED_AT, WINDOW_END, locked),),
        ),
        locked,
        snapshot=_healthy_snapshot(repo_sha=OTHER_SHA),
    )
    assert verdict.verdict is GateVerdict.INVALID
    assert verdict.reasons == ("repo_sha_drift",)


# ---------------------------------------------------------------------------
# declared duration, bounded reading and the explicit input contract
# ---------------------------------------------------------------------------


def test_full_requested_duration_must_have_elapsed(tmp_path: Path) -> None:
    locked = _locked()
    clean = _attempt(tmp_path, INTERRUPTED_RUN, shape=PAPER_STOP, name="clean")
    history = _history(
        locked,
        attempts=(clean,),
        associations=(_association(clean, STARTED_AT, WINDOW_END, locked),),
    )
    short = _evaluate(
        history,
        locked,
        snapshot=_healthy_snapshot(captured_at=MIDPOINT),
        evaluated_at=MIDPOINT,
    )
    assert short.verdict is GateVerdict.INVALID
    assert GAP_COVERAGE in short.reasons

    seven_day = _locked(gate_type=GateType.T7D)
    assert required_duration(GateType.T72H) == timedelta(hours=72)
    assert REQUIRED_DURATIONS[GateType.T7D] == timedelta(days=7)
    unelapsed = _evaluate(history, seven_day)
    assert unelapsed.verdict is GateVerdict.INVALID
    assert GAP_COVERAGE in unelapsed.reasons


def test_attempt_evidence_is_a_bounded_summary(tmp_path: Path) -> None:
    """The reader keeps a count and the terminal's own label, not the frames."""
    clean = _attempt(tmp_path, INTERRUPTED_RUN, shape=PAPER_STOP, name="clean")
    record = read_attempt_evidence(clean.backup_dir, clean.binding)
    assert record.state is AttemptEvidenceState.CLEAN
    assert record.terminal_reason == CLEAN_STOP_REASON
    # preparation plus the terminal, retained as one bounded count
    assert type(record.records) is int and record.records == 2
    assert record.binding == clean.binding


def test_terminal_reason_vocabulary_matches_the_runtime() -> None:
    """The closed stop vocabulary is the runtime's own, never an invented one."""
    # Imported here so one heavy composition import cannot fail the whole module.
    from ea.product.paper import PAPER_CAPACITY_EXHAUSTED_REASON
    from ea.product.paper_gate_history import (
        _CLEAN_TERMINAL_REASONS,
        _FAILED_TERMINAL_REASONS,
    )
    from ea.runtime.streaming import StreamStopReason

    recorded = {reason.value for reason in StreamStopReason}
    assert {StreamStopReason.STOP_REQUESTED.value} == _CLEAN_TERMINAL_REASONS
    assert (recorded - _CLEAN_TERMINAL_REASONS) | {
        PAPER_CAPACITY_EXHAUSTED_REASON
    } == _FAILED_TERMINAL_REASONS


def test_history_validation_is_exact(tmp_path: Path) -> None:
    """The explicit association input is validated where it is constructed."""
    locked = _locked()
    clean = _attempt(tmp_path, INTERRUPTED_RUN, shape=PAPER_STOP, name="clean")
    association = _association(clean, STARTED_AT, WINDOW_END, locked)

    # An association naming an attempt the inventory does not declare is refused,
    # never silently attributed to a declared attempt.
    foreign = RunBinding(RunReference(FAILED_RUN, LINEAGE), clean.binding.manifest_sha256)
    with pytest.raises(PaperEvidenceError) as unassociated:
        PaperIntervalHistory(
            identity=locked,
            coverage_start=STARTED_AT,
            coverage_end=WINDOW_END,
            attempts=(clean,),
            associations=(
                PaperAttemptAssociation(
                    binding=foreign, begin=STARTED_AT, end=WINDOW_END, identity=locked
                ),
            ),
        )
    assert unassociated.value.code is OutcomeCode.CONFLICTING_ID

    # A reversed interval, either unbounded end and any non-identity are refused
    # at the association itself, so no interval can ever be built from them.
    with pytest.raises(PaperEvidenceError) as reversed_order:
        PaperAttemptAssociation(binding=clean.binding, begin=WINDOW_END, end=STARTED_AT)
    assert reversed_order.value.code is OutcomeCode.OUT_OF_RANGE

    with pytest.raises(PaperEvidenceError) as unbounded_begin:
        PaperAttemptAssociation(
            binding=clean.binding,
            begin=None,
            end=WINDOW_END,  # type: ignore[arg-type]
        )
    assert unbounded_begin.value.code is OutcomeCode.OUT_OF_RANGE

    with pytest.raises(PaperEvidenceError) as unbounded_end:
        PaperAttemptAssociation(
            binding=clean.binding,
            begin=STARTED_AT,
            end=None,  # type: ignore[arg-type]
        )
    assert unbounded_end.value.code is OutcomeCode.OUT_OF_RANGE

    with pytest.raises(PaperEvidenceError) as not_an_identity:
        PaperAttemptAssociation(
            binding=clean.binding,
            begin=STARTED_AT,
            end=WINDOW_END,
            identity="not-an-identity",  # type: ignore[arg-type]
        )
    assert not_an_identity.value.code is OutcomeCode.INVALID_TYPE

    with pytest.raises(PaperEvidenceError) as duplicated:
        PaperIntervalHistory(
            identity=locked,
            coverage_start=STARTED_AT,
            coverage_end=WINDOW_END,
            attempts=(clean, clean),
            associations=(association,),
        )
    assert duplicated.value.code is OutcomeCode.CONFLICTING_ID


def test_history_round_trips_as_canonical_evidence(tmp_path: Path) -> None:
    locked = _locked()
    clean = _attempt(tmp_path, INTERRUPTED_RUN, shape=PAPER_STOP, name="clean")
    history = _history(
        locked,
        attempts=(clean,),
        associations=(
            _association(clean, STARTED_AT, MIDPOINT, locked),
            _association(clean, MIDPOINT, WINDOW_END, None),
        ),
    )
    document = history.document()
    assert document["schema"] == "ea.paper-interval-history.v1"
    assert document["associations"][0]["identity"] == locked.document()
    assert document["associations"][1]["identity"] is None

    decoded = decode_paper_interval_history(
        json.loads(canonical_paper_interval_history_bytes(history))
    )
    assert decoded == history
    assert decoded.associations[0].identity == locked
    assert decoded.associations[1].identity is None

    document["coverage_start"] = "not-a-time"
    with pytest.raises(PaperEvidenceError) as error:
        decode_paper_interval_history(document)
    assert error.value.code is OutcomeCode.OUT_OF_RANGE

    document["coverage_start"] = WINDOW_END.isoformat()
    document["associations"][0]["identity"] = {"schema": "not-a-gate-identity"}
    with pytest.raises(PaperEvidenceError) as identity_error:
        decode_paper_interval_history(document)
    assert identity_error.value.code is OutcomeCode.CONFLICTING_ID


def test_interval_evaluator_rejects_non_evidence_types() -> None:
    locked = _locked()
    with pytest.raises(PaperEvidenceError) as error:
        evaluate_gate(
            "not-a-snapshot",  # type: ignore[arg-type]
            locked,
            _current(locked),
            None,
            evaluated_at=WINDOW_END,
        )
    assert error.value.code is OutcomeCode.INVALID_TYPE


# ---------------------------------------------------------------------------
# the old single-snapshot evaluator keeps its old semantics
# ---------------------------------------------------------------------------


def test_instant_health_evaluator_is_unchanged() -> None:
    locked = _locked()
    healthy = evaluate_snapshot_health(
        _healthy_snapshot(), locked, _current(locked), evaluated_at=WINDOW_END
    )
    assert healthy.verdict is GateVerdict.PASS
    assert healthy.reasons == ()

    sick = evaluate_snapshot_health(
        _healthy_snapshot(writer_lease_held=False),
        locked,
        _current(locked),
        evaluated_at=WINDOW_END,
    )
    assert sick.verdict is GateVerdict.FAIL
    assert sick.reasons == ("writer_not_exclusive",)

    drifted = evaluate_snapshot_health(
        _healthy_snapshot(), locked, _current(locked, repo_sha=OTHER_SHA), evaluated_at=WINDOW_END
    )
    assert drifted.verdict is GateVerdict.INVALID
    assert drifted.reasons == ("repo_sha_drift",)
