"""WU-5 boundary tests: real snapshot inputs, a lock that cannot be repaired.

WU-2 froze the evidence contract; these tests cover only the thin operational
layer WU-5 added over it, and the four boundaries the acceptance criteria name:

1. the snapshot is collected from the real local Paper environment, not from
   caller-supplied numbers;
2. collecting it mutates nothing;
3. an input that cannot be observed stays unobservable rather than becoming
   healthy evidence;
4. the gate lock is create-only, and identity drift becomes ``INVALID`` instead
   of being silently relocked.

The attempts these tests read are built by the product's own path (``_paper``
from the PPV-05 suite), so the snapshot is taken over the same manifest, journal
and durable side records a real run publishes.
"""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ea.cli.app import app
from ea.core.run import RunId, RunReference, Sha256Digest
from ea.experiments.store import CanonicalAttemptManifest, LocalResultStore
from ea.product.paper_alerts import (
    ALERT_SIGNALS,
    AlertSignalReading,
    SignalCondition,
    project_alert_stream,
    write_alert_stream,
)
from ea.product.paper_backup import create_paper_backup
from ea.product.paper_evidence import (
    AlertSummary,
    BackupSummary,
    BrokerMode,
    GateType,
    GateVerdict,
    PaperGateIdentity,
    PaperSnapshot,
    SupervisorState,
    build_paper_snapshot,
    canonical_gate_identity_bytes,
    decode_paper_snapshot,
    evaluate_gate,
)
from ea.product.paper_gate_prep import (
    LAUNCHD_LABELS,
    PaperGatePrepError,
    current_gate_identity,
    lock_gate_identity,
    observe_git_head,
    observe_paper_snapshot,
    read_locked_gate_identity,
)
from ea.product.paper_health import (
    BrokerState,
    KillSwitchProjectionState,
    MarketState,
    PaperHealthObservation,
    PaperSafetyObservation,
    ReconciliationState,
    RecoveryState,
    RuntimeState,
    StorageState,
    StrategyHeartbeatState,
    build_paper_health,
    decode_paper_health,
)
from ea.product.paper_session import (
    PaperSessionError,
    PaperSessionWriter,
    read_paper_binding,
)
from ea.risk.operational_safety import OperationalSafetyAuthority, OperationalSafetyLimits
from unit.test_paper_backup import _paper
from unit.test_streaming import RUN_ID

REPO = Path(__file__).resolve().parents[2]
OBSERVED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
GATE_ID = "m4-rc-72h"


def _health_document(run_id: RunId, observed_at: datetime) -> dict[str, Any]:
    """The product's own healthy projection, for the run under test."""
    return build_paper_health(
        PaperHealthObservation(
            process_alive=True,
            runtime_state=RuntimeState.RUNNING,
            recovery_state=RecoveryState.NOT_REQUIRED,
            reconciliation_state=ReconciliationState.CLEAN,
            market_state=MarketState.FRESH,
            broker_state=BrokerState.AVAILABLE,
            strategy_heartbeat_state=StrategyHeartbeatState.LIVE,
            storage_state=StorageState.AVAILABLE,
            kill_switch_state=KillSwitchProjectionState.ACTIVE,
            safety=PaperSafetyObservation(
                kill_switch_halted=False,
                reconciliation_healthy=True,
                market_age_seconds=0.5,
                broker_available=True,
                strategy_live=True,
                daily_loss=Decimal("0"),
                current_exposure=Decimal("0"),
                outstanding_order_exposure=Decimal("0"),
                open_order_count=0,
                reference_price=Decimal("100"),
                now_monotonic=1_000.0,
            ),
            observed_at=observed_at,
            run_id=run_id,
            candidate_id="candidate-wu5",
        ),
        authority=OperationalSafetyAuthority(
            run_id=run_id,
            limits=OperationalSafetyLimits(
                max_market_age_seconds=5.0,
                max_daily_loss=Decimal("1000"),
                max_total_exposure=Decimal("1000"),
                max_open_orders=6,
                max_order_rate=6,
                order_rate_window_seconds=60.0,
                max_price_deviation_bps=250,
            ),
        ),
    ).document()


class Live:
    """One real attempt that is running now: lease held, healthy projection.

    ``at`` is the instant the projection is published for. The reader's freshness
    bound is real time, so a test that drives the CLI -- which reads the clock
    itself -- must publish for now, while a test that observes at a fixed instant
    publishes for that instant.
    """

    def __init__(self, tmp_path: Path, *, at: datetime = OBSERVED_AT) -> None:
        self.paper = _paper(tmp_path / "live")
        self.run_dir = self.paper.run_dir
        self.binding = read_paper_binding(self.run_dir)[0]
        self.publish_health(at=at)

    def publish_health(self, *, at: datetime = OBSERVED_AT) -> None:
        PaperSessionWriter(self.run_dir, self.binding).publish(
            {"state": "running", "health": _health_document(RUN_ID, at)}
        )


def _fresh_backup_on_the_host(tmp_path: Path, backups: Path) -> Path:
    """A second, settled attempt captured through the real backup path.

    It is what makes ``--backup-root`` observable as FRESH for a *live* attempt:
    the host's newest verified backup is a host-level fact, not this attempt's.
    """
    settled = _paper(tmp_path / "settled")
    settled.quiesce()
    return create_paper_backup(settled.run_dir, backups).backup_dir


def _clean_alert_stream(tmp_path: Path, *, at: datetime = OBSERVED_AT) -> Path:
    path = tmp_path / "alerts" / "paper-alerts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = project_alert_stream(
        host_id="gate-prep-test-host",
        readings=tuple(
            AlertSignalReading(
                type=spec.type,
                condition=SignalCondition.INACTIVE,
                observation="no condition observed",
            )
            for spec in ALERT_SIGNALS
        ),
        observed_at=at,
        run_id=None,
        account_id=None,
    )
    write_alert_stream(path, stream)
    return path


def _operator_surface(tmp_path: Path) -> tuple[Path, Path]:
    """One operator config and the two rendered LaunchAgents a gate pins.

    Idempotent on purpose: a drift test edits one of these in place, and asking
    for the surface again must not quietly restore the bytes it changed.
    """
    config = tmp_path / "run-config.env"
    if not config.exists():
        config.write_text("EA_BIN=/usr/local/bin/ea\nBACKUP_KEEP=5\n", encoding="utf-8")
    launchd = tmp_path / "LaunchAgents"
    launchd.mkdir(exist_ok=True)
    for label in LAUNCHD_LABELS:
        rendered = launchd / f"{label}.plist"
        if not rendered.exists():
            rendered.write_text(
                f"<plist><dict><key>Label</key><string>{label}</string></dict></plist>",
                encoding="utf-8",
            )
    return config, launchd


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    """(relative path) -> (mode, size, sha256) for every file and directory."""

    def record(path: Path) -> tuple[int, int, str]:
        info = path.stat()
        if path.is_file():
            return (
                stat.S_IMODE(info.st_mode),
                info.st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        return (stat.S_IMODE(info.st_mode), 0, "")

    return {str(path.relative_to(root)): record(path) for path in sorted(root.rglob("*"))}


def _healthy_snapshot(*, repo_sha: str | None = None) -> PaperSnapshot:
    """A snapshot used only as the substrate for an identity-drift assertion."""
    return build_paper_snapshot(
        captured_at=OBSERVED_AT,
        repo_sha=observe_git_head(REPO) if repo_sha is None else repo_sha,
        run_id=RUN_ID,
        supervisor_state=SupervisorState.RUNNING,
        writer_lease_held=True,
        broker_mode=BrokerMode.LOCAL_SIMULATED_PAPER,
        live_enabled=False,
        health=decode_paper_health(_health_document(RUN_ID, OBSERVED_AT)),
        alert_state=AlertSummary.CLEAN,
        backup_state=BackupSummary.FRESH,
    )


# ---------------------------------------------------------------------------
# acceptance A: the snapshot is the real environment's own read models
# ---------------------------------------------------------------------------


def test_snapshot_is_collected_from_the_real_local_inputs(tmp_path: Path) -> None:
    live = Live(tmp_path)
    alerts = _clean_alert_stream(tmp_path)
    backups = tmp_path / "backups"
    _fresh_backup_on_the_host(tmp_path, backups)

    snapshot = observe_paper_snapshot(
        run_dir=live.run_dir,
        repository=REPO,
        supervisor_state=SupervisorState.RUNNING,
        now=OBSERVED_AT + timedelta(seconds=1),
        backup_root=backups,
        alert_stream_path=alerts,
    )

    assert isinstance(snapshot, PaperSnapshot)
    assert snapshot.run_id == RUN_ID
    # The repository identity is this checkout's own HEAD, read from git.
    assert snapshot.repo_sha == observe_git_head(REPO)
    assert (
        snapshot.repo_sha
        == subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    assert snapshot.supervisor_state is SupervisorState.RUNNING
    # Liveness is the live lease observation, not the value the writer recorded.
    assert snapshot.writer_lease_held is True
    assert snapshot.broker_mode is BrokerMode.LOCAL_SIMULATED_PAPER
    # The attempt's own recorded profile is the only observable Paper/Live fact.
    assert snapshot.live_enabled is False
    assert snapshot.runtime_state is RuntimeState.RUNNING
    assert snapshot.reconciliation_state is ReconciliationState.CLEAN
    assert snapshot.readiness is True
    assert snapshot.alert_state.value == "clean"
    assert snapshot.backup_state.value == "fresh"
    assert snapshot.schema == "ea.paper-snapshot.v1"
    # What leaves the process is the contract's own document, and it decodes.
    assert decode_paper_snapshot(json.loads(json.dumps(snapshot.document()))) == snapshot


def test_snapshot_mutates_nothing(tmp_path: Path) -> None:
    live = Live(tmp_path)
    alerts = _clean_alert_stream(tmp_path)
    backups = tmp_path / "backups"
    _fresh_backup_on_the_host(tmp_path, backups)

    def surface() -> dict[str, tuple[int, int, str]]:
        return {
            **{f"attempt/{key}": value for key, value in _tree_snapshot(live.run_dir).items()},
            **{f"alerts/{key}": value for key, value in _tree_snapshot(alerts.parent).items()},
            **{f"backups/{key}": value for key, value in _tree_snapshot(backups).items()},
        }

    before = surface()
    observe_paper_snapshot(
        run_dir=live.run_dir,
        repository=REPO,
        supervisor_state=SupervisorState.RUNNING,
        now=OBSERVED_AT + timedelta(seconds=1),
        backup_root=backups,
        alert_stream_path=alerts,
    )
    assert surface() == before


def test_snapshot_keeps_unobserved_inputs_unobserved(tmp_path: Path) -> None:
    """A blind observer must not produce healthy evidence.

    Every input is either observed or explicitly unavailable, and none of the
    unobserved ones may resolve to the healthy value: an absent health
    projection is not ready, and an absent alert stream is not clean.
    """
    live = Live(tmp_path)
    config, launchd = _operator_surface(tmp_path)
    # Republish the same attempt with no health slot at all.
    PaperSessionWriter(live.run_dir, live.binding).publish({"state": "running"})
    missing_backups = tmp_path / "never-created-backups"

    snapshot = observe_paper_snapshot(
        run_dir=live.run_dir,
        repository=REPO,
        supervisor_state=SupervisorState.UNKNOWN,
        now=OBSERVED_AT,
        backup_root=missing_backups,
        alert_stream_path=tmp_path / "alerts" / "absent.json",
    )

    assert snapshot.health is None
    assert snapshot.readiness is False
    assert snapshot.runtime_state.value == "unknown"
    assert snapshot.risk_gate_state.value == "unknown"
    assert snapshot.reconciliation_state.value == "unknown"
    # A missing alert stream is unavailable, never clean; an unreadable backup
    # root is unavailable, never fresh.
    assert snapshot.alert_state.value == "unavailable"
    assert snapshot.backup_state.value == "unavailable"

    identity = current_gate_identity(
        gate_id=GATE_ID,
        gate_type=GateType.T72H,
        repository=REPO,
        config_path=config,
        launchd_dir=launchd,
        started_at=OBSERVED_AT,
    )
    verdict = evaluate_gate(snapshot, identity, identity, evaluated_at=OBSERVED_AT)
    assert verdict.verdict is GateVerdict.FAIL
    assert set(verdict.reasons) == {
        "reconciliation_not_clean",
        "risk_gate_not_healthy",
        "runtime_not_running",
        "runtime_not_ready",
        "backup_not_fresh",
        "critical_alert_active",
    }


def test_an_empty_backup_root_is_absent_not_fresh(tmp_path: Path) -> None:
    live = Live(tmp_path)
    empty = tmp_path / "empty-backups"
    empty.mkdir()

    snapshot = observe_paper_snapshot(
        run_dir=live.run_dir,
        repository=REPO,
        supervisor_state=SupervisorState.RUNNING,
        now=OBSERVED_AT,
        backup_root=empty,
        alert_stream_path=_clean_alert_stream(tmp_path),
    )
    assert snapshot.backup_state.value == "absent"
    assert snapshot.alert_state.value == "clean"


def test_snapshot_refuses_when_the_attempt_cannot_be_read(tmp_path: Path) -> None:
    with pytest.raises(PaperSessionError):
        observe_paper_snapshot(
            run_dir=tmp_path / "no-such-attempt",
            repository=REPO,
            supervisor_state=SupervisorState.RUNNING,
            now=OBSERVED_AT,
        )


def test_snapshot_reports_anything_but_the_local_profile_as_live_capable(
    tmp_path: Path,
) -> None:
    """The Paper/Live boundary is read from the attempt, and fails closed.

    There is no Live flag to read, so the boundary is the profile the attempt
    recorded for itself: anything else -- including a manifest this module
    cannot understand -- is reported as live-capable, which makes the gate
    INVALID rather than quietly Live-denied.
    """
    root = tmp_path / "results"
    root.mkdir()
    store = LocalResultStore(root.resolve())
    run_id = RunId("99999999-9999-4999-8999-999999999999")
    payload = json.dumps(
        {
            "schema": "ea.local-paper-attempt.v1",
            "run_id": run_id.value,
            "lineage_sha256": Sha256Digest("9" * 64).value,
            "operation": "paper.start",
            "account_id": "live.broker",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    prepared = store.prepare_canonical_attempt(
        CanonicalAttemptManifest(RunReference(run_id, Sha256Digest("9" * 64)), payload)
    )
    try:
        run_dir = root / run_id.value
        PaperSessionWriter(run_dir, prepared.audit.binding).publish({"state": "running"})
        snapshot = observe_paper_snapshot(
            run_dir=run_dir,
            repository=REPO,
            supervisor_state=SupervisorState.RUNNING,
            now=OBSERVED_AT,
        )
    finally:
        store.close()
    assert snapshot.live_enabled is True
    assert evaluate_gate(
        snapshot, _identity(tmp_path), _identity(tmp_path), evaluated_at=OBSERVED_AT
    ).reasons == ("unexpected_live_capability",)


def _identity(tmp_path: Path) -> PaperGateIdentity:
    config, launchd = _operator_surface(tmp_path)
    return current_gate_identity(
        gate_id=GATE_ID,
        gate_type=GateType.T72H,
        repository=REPO,
        config_path=config,
        launchd_dir=launchd,
        started_at=OBSERVED_AT,
    )


def test_git_head_must_be_one_real_commit(tmp_path: Path) -> None:
    with pytest.raises(PaperGatePrepError):
        observe_git_head(tmp_path)
    with pytest.raises(PaperGatePrepError):
        observe_git_head(Path("relative"))


# ---------------------------------------------------------------------------
# acceptance B: the lock is create-only, and drift is INVALID
# ---------------------------------------------------------------------------


def test_gate_identity_pins_the_existing_wu2_fields(tmp_path: Path) -> None:
    config, launchd = _operator_surface(tmp_path)
    identity = current_gate_identity(
        gate_id=GATE_ID,
        gate_type=GateType.T72H,
        repository=REPO,
        config_path=config,
        launchd_dir=launchd,
        started_at=OBSERVED_AT,
    )
    document = identity.document()
    assert set(document) == {
        "schema",
        "gate_id",
        "gate_type",
        "repo_sha",
        "runtime_profile",
        "config_identity",
        "launchd_identity",
        "evidence_schema_version",
        "started_at",
    }
    assert document["schema"] == "ea.paper-gate-identity.v1"
    assert document["gate_type"] == "72h"
    assert document["runtime_profile"] == "local-simulated-paper-v1"
    assert document["evidence_schema_version"] == "1"
    assert document["repo_sha"] == observe_git_head(REPO)


def test_lock_persists_once_and_never_relocks(tmp_path: Path) -> None:
    path = tmp_path / "gate-identity.json"
    locked = _identity(tmp_path)
    lock_gate_identity(path, locked)

    assert path.read_bytes() == canonical_gate_identity_bytes(locked)
    assert read_locked_gate_identity(path) == locked
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    before = path.read_bytes()
    with pytest.raises(PaperGatePrepError, match="already locked"):
        lock_gate_identity(path, _identity(tmp_path))
    assert path.read_bytes() == before


def test_operator_config_drift_is_invalid_not_a_relock(tmp_path: Path) -> None:
    config, launchd = _operator_surface(tmp_path)
    path = tmp_path / "gate-identity.json"
    lock_gate_identity(path, _identity(tmp_path))

    # The operator edits the pinned config after the gate locked. The identity is
    # re-read as it is now; the lock does not move with it.
    config.write_text("EA_BIN=/usr/local/bin/ea\nBACKUP_KEEP=9\n", encoding="utf-8")
    current = _identity(tmp_path)
    verdict = evaluate_gate(
        _healthy_snapshot(),
        read_locked_gate_identity(path),
        current,
        evaluated_at=OBSERVED_AT,
    )
    assert verdict.verdict is GateVerdict.INVALID
    assert verdict.reasons == ("config_drift",)


def test_launchd_drift_is_invalid(tmp_path: Path) -> None:
    config, launchd = _operator_surface(tmp_path)
    path = tmp_path / "gate-identity.json"
    locked = _identity(tmp_path)
    lock_gate_identity(path, locked)

    (launchd / f"{LAUNCHD_LABELS[0]}.plist").write_text("<plist/>", encoding="utf-8")
    verdict = evaluate_gate(
        _healthy_snapshot(),
        read_locked_gate_identity(path),
        _identity(tmp_path),
        evaluated_at=OBSERVED_AT,
    )
    assert verdict.verdict is GateVerdict.INVALID
    assert verdict.reasons == ("launchd_drift",)


def test_repository_drift_is_invalid(tmp_path: Path) -> None:
    """A moved checkout invalidates the gate even when the runtime is healthy."""
    path = tmp_path / "gate-identity.json"
    lock_gate_identity(path, _identity(tmp_path))
    verdict = evaluate_gate(
        _healthy_snapshot(repo_sha="b" * 40),
        read_locked_gate_identity(path),
        _identity(tmp_path),
        evaluated_at=OBSERVED_AT,
    )
    assert verdict.verdict is GateVerdict.INVALID
    assert verdict.reasons == ("repo_sha_drift",)


def test_a_missing_rendered_agent_is_not_a_lockable_identity(tmp_path: Path) -> None:
    config, launchd = _operator_surface(tmp_path)
    (launchd / f"{LAUNCHD_LABELS[1]}.plist").unlink()
    # A supervisor that is only half rendered is not a lockable identity: the
    # identity is rebuilt from what is actually there, and it is not there.
    with pytest.raises(PaperGatePrepError):
        current_gate_identity(
            gate_id=GATE_ID,
            gate_type=GateType.T72H,
            repository=REPO,
            config_path=config,
            launchd_dir=launchd,
            started_at=OBSERVED_AT,
        )


def test_a_lock_that_is_not_its_own_canonical_bytes_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "gate-identity.json"
    lock_gate_identity(path, _identity(tmp_path))
    path.write_text(json.dumps(json.loads(path.read_bytes()), indent=2), encoding="utf-8")
    with pytest.raises(PaperGatePrepError, match="canonical"):
        read_locked_gate_identity(path)


# ---------------------------------------------------------------------------
# CLI wiring: one document, honest exit codes, no gate started
# ---------------------------------------------------------------------------


def test_cli_snapshot_prints_one_contract_document(tmp_path: Path) -> None:
    live = Live(tmp_path, at=datetime.now(UTC))
    result = CliRunner().invoke(
        app,
        [
            "paper",
            "snapshot",
            "--run-dir",
            str(live.run_dir),
            "--repo",
            str(REPO),
            "--supervisor-state",
            "running",
            "--alert-stream",
            str(_clean_alert_stream(tmp_path)),
        ],
    )
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["schema"] == "ea.paper-snapshot.v1"
    assert document["run_id"] == RUN_ID.value
    assert document["writer_lease_held"] is True
    assert document["live_enabled"] is False


def test_cli_snapshot_fails_closed_on_unreadable_evidence(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "paper",
            "snapshot",
            "--run-dir",
            str(tmp_path / "no-such-attempt"),
            "--repo",
            str(REPO),
        ],
    )
    assert result.exit_code == 3
    assert result.stdout == ""


def test_cli_snapshot_rejects_an_unknown_supervisor_state(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "paper",
            "snapshot",
            "--run-dir",
            str(tmp_path / "whatever"),
            "--repo",
            str(REPO),
            "--supervisor-state",
            "probably-fine",
        ],
    )
    assert result.exit_code == 2


def test_cli_gate_lock_reports_an_invalid_gate_id_as_input(tmp_path: Path) -> None:
    """A label the frozen contract rejects is input, not a crash.

    ``PaperGateIdentity`` validates the operator's gate id, so a bad label used to
    escape this command as a traceback instead of the exit-2 input surface the
    other lock arguments already use. Nothing is written either way.
    """
    config, launchd = _operator_surface(tmp_path)
    identity = tmp_path / "gate-identity.json"

    result = CliRunner().invoke(
        app,
        [
            "paper",
            "gate",
            "lock",
            "--identity",
            str(identity),
            "--gate-id",
            "BAD ID",
            "--gate-type",
            "72h",
            "--repo",
            str(REPO),
            "--config",
            str(config),
            "--launchd-dir",
            str(launchd),
        ],
    )

    assert result.exit_code == 2
    assert "Paper gate lock input error:" in result.stderr
    assert "gate_id" in result.stderr
    # Only the deliberate exit terminated the command. The frozen contract's
    # PaperEvidenceError is caught here, so it never reaches the runner as itself
    # and never renders as a traceback.
    assert isinstance(result.exception, SystemExit)
    assert result.stdout == ""
    assert not identity.exists()


def test_cli_gate_lock_then_evaluate_is_the_only_path_to_a_verdict(tmp_path: Path) -> None:
    """Locking starts nothing; evaluating is a reading of the lock.

    The same real inputs are judged twice: unchanged, the gate is PASS, so the
    command is usable as the pre-freeze readiness check; after the operator
    config moves it is INVALID, and the lock has not moved with it.
    """
    live = Live(tmp_path, at=datetime.now(UTC))
    alerts = _clean_alert_stream(tmp_path, at=datetime.now(UTC))
    backups = tmp_path / "backups"
    _fresh_backup_on_the_host(tmp_path, backups)
    config, launchd = _operator_surface(tmp_path)
    identity = tmp_path / "gate-identity.json"
    runner = CliRunner()

    lock = runner.invoke(
        app,
        [
            "paper",
            "gate",
            "lock",
            "--identity",
            str(identity),
            "--gate-id",
            GATE_ID,
            "--gate-type",
            "72h",
            "--repo",
            str(REPO),
            "--config",
            str(config),
            "--launchd-dir",
            str(launchd),
        ],
    )
    assert lock.exit_code == 0, lock.output
    locked_bytes = identity.read_bytes()

    evaluate = [
        "paper",
        "gate",
        "evaluate",
        "--identity",
        str(identity),
        "--run-dir",
        str(live.run_dir),
        "--repo",
        str(REPO),
        "--config",
        str(config),
        "--launchd-dir",
        str(launchd),
        "--supervisor-state",
        "running",
        "--backup-root",
        str(backups),
        "--alert-stream",
        str(alerts),
    ]
    passing = runner.invoke(app, evaluate)
    assert passing.exit_code == 0, passing.output
    document = json.loads(passing.stdout)
    # The verdict is exactly WU-2's; this surface only supplies the inputs.
    assert document["verdict"]["schema"] == "ea.paper-gate-verdict.v1"
    assert document["verdict"]["verdict"] == "pass"
    assert document["verdict"]["reasons"] == []
    assert document["locked_identity"] == json.loads(locked_bytes)
    assert document["snapshot"]["writer_lease_held"] is True

    config.write_text("EA_BIN=/usr/local/bin/ea\nBACKUP_KEEP=9\n", encoding="utf-8")
    drifted = runner.invoke(app, evaluate)
    assert drifted.exit_code == 3
    drift_document = json.loads(drifted.stdout)
    assert drift_document["verdict"]["verdict"] == "invalid"
    assert drift_document["verdict"]["reasons"] == ["config_drift"]
    assert identity.read_bytes() == locked_bytes
