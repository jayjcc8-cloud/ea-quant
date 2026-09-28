"""Quiesced backup and isolated restore drills over real Paper state (PPV-05)."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from ea.cli.app import app
from ea.core import AuditRecord, RunReference, Sha256Digest
from ea.core.execution_messages import order_digest
from ea.core.ledger_integration import portfolio_risk_refresh_digest
from ea.experiments.audit import (
    PosixAuditJournal,
    create_posix_audit_journal,
    reopen_posix_audit_journal,
)
from ea.experiments.store import (
    CanonicalAttemptManifest,
    LocalResultStore,
    VerifiedIncompleteRecoveryBinding,
)
from ea.product import LoadedBacktestScenario, load_backtest_scenario
from ea.product.backtest import _funding_document, _write_once
from ea.product.paper import PaperTradingSession
from ea.product.paper_backup import (
    PaperBackupManifest,
    PaperBackupRefused,
    create_paper_backup,
    inspect_paper_backup,
    restore_paper_backup,
)
from ea.product.paper_recovery import (
    reconcile_paper_recovery,
    recover_paper_broker_observation,
    recover_paper_continuation,
    recover_paper_economic_state,
    recover_paper_orders,
    recover_paper_risk_refreshes,
    scan_paper_outbound_intents,
)
from ea.product.paper_run import resume_local_paper
from ea.product.paper_session import PaperSessionWriter, read_paper_binding
from ea.risk.kill_switch import (
    OperatorKillSwitchAuthority,
    canonical_kill_switch_bytes,
    create_operator_kill_switch_authority,
    restore_operator_kill_switch_authority,
)
from ea.risk.operational_safety import (
    OperationalSafetyLimits,
    canonical_operational_safety_limits_bytes,
    restore_operational_safety_limits,
)
from unit.test_bounded_round_trips_v1 import bounded_scenario
from unit.test_paper_runtime import drive
from unit.test_streaming import RUN_ID, SOURCE, ControlledClock

LINEAGE = Sha256Digest("1" * 64)
_AUTHORITATIVE_PATHS = (
    "manifest.json",
    "audit/audit-v1.journal",
    "funding.json",
    "kill-switch.json",
    "operational-safety.json",
    "strategy.eastrategy",
)
_CANDIDATE: dict[str, Any] = {
    "schema": "ea.accepted-candidate-binding.v1",
    "candidate_id": "candidate-ppv05",
    "fingerprint": "b" * 64,
    "strategy_id": "bounded-long-hold-roots-v1",
    "strategy_version": "1.0.0",
    "artifact_sha256": "c" * 64,
    "parameters_sha256": "d" * 64,
    "configuration_sha256": "e" * 64,
    "source_scenario_sha256": "f" * 64,
    "source_data_sha256": "0" * 64,
    "implementation": {"kind": "local"},
    "execution_scope": "offline-backtest-only",
}


def _canonical(document: object) -> bytes:
    return json.dumps(
        document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


@dataclass(slots=True)
class _Paper:
    """One real local Paper attempt plus the authority behind its durable side records."""

    run_dir: Path
    store: LocalResultStore
    journal: PosixAuditJournal
    engine: PaperTradingSession
    scenario: LoadedBacktestScenario
    kill_switch: OperatorKillSwitchAuthority

    def quiesce(self) -> None:
        """Release the writer lease the way a controlled stop does."""
        self.journal.close()
        self.store.close()


def _paper(tmp_path: Path, *, artifact: bool = True) -> _Paper:
    """Build one real attempt on a strict POSIX journal, exactly as the product does."""
    results = tmp_path / "results"
    results.mkdir(mode=0o700, parents=True)
    store = LocalResultStore(results.resolve())
    manifest_bytes = _canonical(
        {
            "schema": "ea.local-paper-attempt.v1",
            "run_id": RUN_ID.value,
            "lineage_sha256": LINEAGE.value,
            "started_at": "2026-01-02T09:30:00.000000Z",
            "operation": "paper.start",
            "account_id": "paper.local",
            "candidate": _CANDIDATE,
            "scenario": {"schema_version": 5},
            "prices": [100.0],
            "interval_seconds": 0.1,
            "event_limit": None,
        }
    )
    prepared = store.prepare_canonical_attempt(
        CanonicalAttemptManifest(RunReference(RUN_ID, LINEAGE), manifest_bytes)
    )
    journal = create_posix_audit_journal(prepared.audit)
    path = bounded_scenario(tmp_path)
    document = yaml.safe_load(path.read_text())
    document["funding"]["initial_cash"] = "1000"
    document["risk"]["max_notional"] = "1000"
    document["execution"]["slippage"] = {
        "policy": "deterministic-slippage-v1",
        "slippage_bps": "100",
    }
    path.write_text(yaml.safe_dump(document))
    scenario = load_backtest_scenario(path)
    kill_switch = create_operator_kill_switch_authority(RUN_ID)
    clock = ControlledClock()
    engine = PaperTradingSession(
        scenario,
        binding=prepared.audit.binding,
        audit=journal,
        clock=clock,
        monotonic=clock.monotonic,
        source_id=SOURCE,
        prices=(100.0,),
        stop_requested=lambda: False,
        kill_switch=kill_switch,
    )
    run_dir = results / RUN_ID.value
    # The durable side records the product publishes for a real attempt.
    _write_once(
        run_dir / "funding.json",
        _canonical(_funding_document(engine.funding, engine.gate.funding_outcome)),
    )
    _write_once(run_dir / "kill-switch.json", canonical_kill_switch_bytes(kill_switch))
    _write_once(
        run_dir / "operational-safety.json",
        canonical_operational_safety_limits_bytes(engine.operational_safety.limits),
    )
    if artifact:
        # Only an accepted Candidate that shipped a strategy package has one.
        _write_once(run_dir / "strategy.eastrategy", b"PK\x03\x04ppv-05 strategy artifact")
    # Derived and ephemeral state that a backup must never capture.
    PaperSessionWriter(run_dir, prepared.audit.binding).publish({"state": "running", "cash": "0"})
    (run_dir / "operational.jsonl").write_bytes(b'{"event":"paper.started"}\n')
    drive(engine, clock)
    return _Paper(run_dir, store, journal, engine, scenario, kill_switch)


def _records(run_dir: Path) -> tuple[AuditRecord, ...]:
    """Read one attempt's journal through the existing M1 recovery path."""
    store = LocalResultStore(run_dir.parent)
    try:
        binding, manifest_bytes = read_paper_binding(run_dir)
        verified = store.verify_recovery_attempt(
            CanonicalAttemptManifest(binding.reference, manifest_bytes)
        )
        assert type(verified) is VerifiedIncompleteRecoveryBinding
        recovered = store.recover_incomplete_attempt(verified)
        journal = reopen_posix_audit_journal(recovered.audit)
        try:
            return tuple(journal.recovery_records)
        finally:
            journal.close()
    finally:
        store.close()


def _witness(paper: _Paper, run_dir: Path) -> dict[str, Any]:
    """Reconstruct every asserted economic fact from one attempt's durable evidence."""
    records = _records(run_dir)
    spec_set = paper.scenario.spec_set
    continuation = recover_paper_continuation(
        records,
        spec_set=spec_set,
        funding=paper.engine.funding,
        instrument=paper.scenario.instrument,
    )
    economic = recover_paper_economic_state(
        records,
        run_id=RUN_ID,
        spec_set=spec_set,
        execution_policy=paper.scenario.execution_policy,
        risk_policy=paper.engine.gate.risk_authority.policy,
        funding=paper.engine.funding,
    )
    orders = recover_paper_orders(
        records, spec_set=spec_set, execution_policy=paper.scenario.execution_policy
    )
    refreshes = recover_paper_risk_refreshes(records)
    scan = scan_paper_outbound_intents(records)
    joint = reconcile_paper_recovery(scan, recover_paper_broker_observation(records))
    final = economic.final_refresh
    return {
        "cash": continuation.cash_text,
        "position": continuation.position_quantity_text,
        "fills": continuation.fills,
        "round_trips": continuation.round_trips,
        "open_orders": continuation.open_orders,
        "exactly_once": continuation.exactly_once,
        "ledger_sequence": economic.ledger.snapshot.ledger_sequence,
        "refresh_count": len(refreshes),
        "refresh_sequence": None if final is None else final.refresh_sequence,
        "refresh_digest": None if final is None else portfolio_risk_refresh_digest(final).value,
        "orders": [order_digest(order).value for order in orders],
        "intents": [
            (intent.order_owner_sequence, intent.classification.value, intent.terminal)
            for intent in scan.intents
        ],
        "joint_recovered": joint.recovered,
        "joint_exactly_once": joint.exactly_once,
        "joint_open_orders": list(joint.open_orders),
        "joint_recovered_fills": joint.recovered_fills,
    }


def test_restore_drill_preserves_economic_state_exactly(tmp_path: Path) -> None:
    """Quiesce, back up, restore into an empty root, then reopen through M1."""
    paper = _paper(tmp_path)
    live = paper.engine.status()
    paper.quiesce()
    source = _witness(paper, paper.run_dir)

    backup_root = tmp_path / "backups"
    result = create_paper_backup(paper.run_dir, backup_root, keep=3)
    manifest = result.manifest
    assert result.backup_dir == backup_root / result.backup_dir.name
    assert tuple(entry.path for entry in manifest.entries) == _AUTHORITATIVE_PATHS
    assert manifest.candidate == _CANDIDATE
    assert manifest.candidate_id == _CANDIDATE["candidate_id"]
    assert manifest.artifact_sha256 == _CANDIDATE["artifact_sha256"]
    assert manifest.configuration_sha256 == _CANDIDATE["configuration_sha256"]
    assert manifest.run_id == RUN_ID and manifest.lineage_sha256 == LINEAGE
    assert (
        manifest.attempt_manifest_sha256.value
        == sha256((paper.run_dir / "manifest.json").read_bytes()).hexdigest()
    )
    # Derived, ephemeral and lease-carrier paths are never captured.
    assert not (result.backup_dir / "files" / "outputs").exists()
    assert not (result.backup_dir / "files" / "operational.jsonl").exists()
    assert not (result.backup_dir / "files" / "audit" / "writer-v1.lock").exists()
    assert inspect_paper_backup(result.backup_dir) == manifest
    assert result.pruned == ()

    target = tmp_path / "isolated" / RUN_ID.value
    restored = restore_paper_backup(result.backup_dir, target)
    assert restored.run_dir == target and restored.entries == manifest.entries
    lock = target / "audit" / "writer-v1.lock"
    assert lock.stat().st_size == 0
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert list((target / "outputs").iterdir()) == []
    assert not (target / "operational.jsonl").exists()

    # The source backup is immutable across the restore: it still verifies.
    assert inspect_paper_backup(result.backup_dir) == manifest

    # Every asserted economic fact survives the round trip exactly.
    restored_state = _witness(paper, target)
    assert restored_state == source
    assert restored_state["cash"] == "986.8"
    assert restored_state["position"] == "0"
    assert restored_state["fills"] == 6
    assert restored_state["round_trips"] == 3
    assert restored_state["open_orders"] == ()
    assert restored_state["exactly_once"] is True
    assert restored_state["ledger_sequence"] == 7
    assert restored_state["refresh_count"] == 38
    assert restored_state["refresh_sequence"] == 38
    assert len(restored_state["orders"]) == 6
    assert restored_state["joint_recovered"] is True
    assert restored_state["joint_exactly_once"] is True
    assert restored_state["joint_recovered_fills"] == 6
    assert restored_state["joint_open_orders"] == []
    assert [intent[1] for intent in restored_state["intents"]] == ["sent_confirmed"] * 6
    assert [intent[2] for intent in restored_state["intents"]] == ["filled"] * 6
    # The live engine's acknowledged money agrees with the restored evidence.
    assert live["cash"] == restored_state["cash"]
    assert live["position"] == restored_state["position"]
    assert live["fills"] == restored_state["fills"]
    assert live["ledger_sequence"] == restored_state["ledger_sequence"]
    assert live["completed_round_trips"] == restored_state["round_trips"]

    # Candidate identity and the durable side records survive byte for byte.
    assert json.loads((target / "manifest.json").read_bytes()) == json.loads(
        (paper.run_dir / "manifest.json").read_bytes()
    )
    side_records = (
        "funding.json",
        "kill-switch.json",
        "operational-safety.json",
        "strategy.eastrategy",
    )
    for name in side_records:
        assert (target / name).read_bytes() == (paper.run_dir / name).read_bytes()
        assert stat.S_IMODE((target / name).stat().st_mode) == 0o600
    assert (target / "audit" / "audit-v1.journal").read_bytes() == (
        result.backup_dir / "files" / "audit" / "audit-v1.journal"
    ).read_bytes()
    restored_switch = restore_operator_kill_switch_authority(
        RUN_ID, (target / "kill-switch.json").read_bytes()
    )
    assert restored_switch._state.revision == paper.kill_switch._state.revision
    assert restored_switch._state.scopes == paper.kill_switch._state.scopes
    limits = restore_operational_safety_limits((target / "operational-safety.json").read_bytes())
    assert limits == paper.engine.operational_safety.limits
    assert type(limits) is OperationalSafetyLimits

    # Reopening goes through the existing M1 path and finds the same economics.
    admission = resume_local_paper(target)
    assert admission["state"] == "incomplete"
    assert admission["reconciliation_required"] is False
    assert admission["joint"]["recovered"] is True
    assert admission["joint"]["exactly_once"] is True
    assert admission["joint"]["recovered_fills"] == 6
    assert admission["joint"]["open_orders"] == []
    assert admission["joint"]["divergent_orders"] == []
    assert len(admission["incomplete_outbound_intents"]) == 0
    assert _witness(paper, target) == source


def test_corrupted_backup_is_refused_before_any_target_state_exists(tmp_path: Path) -> None:
    """A tampered payload or boundary digest fails closed with nothing created."""
    paper = _paper(tmp_path)
    paper.quiesce()
    result = create_paper_backup(paper.run_dir, tmp_path / "backups")
    journal = result.backup_dir / "files" / "audit" / "audit-v1.journal"
    original = journal.read_bytes()
    tampered = bytearray(original)
    tampered[len(tampered) // 2] ^= 0xFF
    journal.write_bytes(bytes(tampered))

    target = tmp_path / "isolated" / RUN_ID.value
    with pytest.raises(PaperBackupRefused, match="integrity"):
        restore_paper_backup(result.backup_dir, target)
    assert not target.exists()
    assert not (tmp_path / "isolated").exists()
    with pytest.raises(PaperBackupRefused, match="integrity"):
        inspect_paper_backup(result.backup_dir)

    # A truncated journal is refused as well, and still creates nothing at the target.
    journal.write_bytes(original[: len(original) - 4])
    with pytest.raises(PaperBackupRefused, match="integrity"):
        restore_paper_backup(result.backup_dir, target)
    assert not target.exists()

    # Tampering with the boundary digest itself is refused before any file is read.
    journal.write_bytes(original)
    document = json.loads((result.backup_dir / "backup.json").read_bytes())
    document["files_sha256"] = "a" * 64
    (result.backup_dir / "backup.json").write_bytes(_canonical(document))
    with pytest.raises(PaperBackupRefused, match="manifest identity"):
        restore_paper_backup(result.backup_dir, target)
    assert not target.exists()
    assert not (tmp_path / "isolated").exists()


def test_attempt_without_a_strategy_artifact_round_trips(tmp_path: Path) -> None:
    """An accepted Candidate with no strategy package has no artifact to capture."""
    paper = _paper(tmp_path, artifact=False)
    paper.quiesce()
    result = create_paper_backup(paper.run_dir, tmp_path / "backups")
    assert tuple(entry.path for entry in result.manifest.entries) == _AUTHORITATIVE_PATHS[:-1]
    assert result.manifest.artifact_sha256 == _CANDIDATE["artifact_sha256"]

    target = tmp_path / "isolated" / RUN_ID.value
    restored = restore_paper_backup(result.backup_dir, target)
    assert not (target / "strategy.eastrategy").exists()
    assert _witness(paper, target) == _witness(paper, paper.run_dir)
    admission = resume_local_paper(restored.run_dir)
    assert admission["reconciliation_required"] is False
    assert admission["joint"]["recovered_fills"] == 6


def test_active_attempt_is_refused_and_publishes_nothing(tmp_path: Path) -> None:
    """A held writer lease is not a safe snapshot boundary, so backup fails closed."""
    paper = _paper(tmp_path)  # the store still owns the writer lease here
    backup_root = tmp_path / "backups"
    with pytest.raises(PaperBackupRefused, match="quiesce"):
        create_paper_backup(paper.run_dir, backup_root)
    assert sorted(backup_root.iterdir()) == []
    paper.quiesce()
    result = create_paper_backup(paper.run_dir, backup_root)
    assert len(result.pruned) == 0


def test_backup_retention_keeps_only_the_newest(tmp_path: Path) -> None:
    """Retention is a deterministic keep-last-N over published backup names."""
    paper = _paper(tmp_path)
    paper.quiesce()
    root = tmp_path / "backups"
    names = [create_paper_backup(paper.run_dir, root, keep=2).backup_dir.name for _ in range(3)]
    remaining = sorted(entry.name for entry in root.iterdir())
    assert remaining == sorted(names)[-2:]
    assert names[0] not in remaining


def test_cli_backup_inspect_and_restore_round_trip(tmp_path: Path) -> None:
    """The additive Paper CLI surface creates, verifies and restores a backup."""
    paper = _paper(tmp_path)
    paper.quiesce()
    runner = CliRunner()
    created = runner.invoke(
        app,
        [
            "paper",
            "backup",
            "--run-dir",
            str(paper.run_dir),
            "--backup-root",
            str(tmp_path / "backups"),
        ],
    )
    assert created.exit_code == 0
    document = json.loads(created.stdout)
    assert document["schema"] == "ea.paper-backup.v1"
    assert document["run"]["run_id"] == RUN_ID.value

    inspected = runner.invoke(
        app, ["paper", "backup", "inspect", "--backup", document["backup_dir"]]
    )
    assert inspected.exit_code == 0
    assert json.loads(inspected.stdout)["verified"] is True

    target = tmp_path / "isolated" / RUN_ID.value
    restored = runner.invoke(
        app, ["paper", "restore", "--backup", document["backup_dir"], "--run-dir", str(target)]
    )
    assert restored.exit_code == 0
    assert json.loads(restored.stdout)["run_id"] == RUN_ID.value
    assert target.is_dir()

    # An existing target is refused with the fail-closed exit code, not an error code.
    again = runner.invoke(
        app, ["paper", "restore", "--backup", document["backup_dir"], "--run-dir", str(target)]
    )
    assert again.exit_code == 3
    # A missing backup path is an input error.
    missing = runner.invoke(
        app, ["paper", "backup", "inspect", "--backup", str(tmp_path / "absent")]
    )
    assert missing.exit_code == 2
    # A held writer lease is refused with the same fail-closed exit code.
    active = _paper(tmp_path / "active")
    held = runner.invoke(
        app,
        [
            "paper",
            "backup",
            "--run-dir",
            str(active.run_dir),
            "--backup-root",
            str(tmp_path / "active-backups"),
        ],
    )
    assert held.exit_code == 3
    assert isinstance(inspect_paper_backup(Path(document["backup_dir"])), PaperBackupManifest)
    active.quiesce()
