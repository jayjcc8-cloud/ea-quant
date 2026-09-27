"""Restart admission integration tests over a real reopened POSIX journal."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ea.core import AuditRecordKind, RunReference, Sha256Digest
from ea.core.paper import canonical_paper_terminal_payload
from ea.core.portfolio import portfolio_snapshot_digest
from ea.core.risk import risk_state_snapshot_digest
from ea.experiments.audit import PosixAuditJournal, create_posix_audit_journal
from ea.experiments.store import CanonicalAttemptManifest, LocalResultStore
from ea.product import load_backtest_scenario
from ea.product.paper import PaperTradingSession
from ea.product.paper_run import resume_local_paper
from unit.test_bounded_round_trips_v1 import bounded_scenario
from unit.test_paper_runtime import drive
from unit.test_streaming import RUN_ID, SOURCE, ControlledClock

LINEAGE = Sha256Digest("1" * 64)


def _build_attempt(
    tmp_path: Path,
    *,
    crash_after: AuditRecordKind | None,
) -> tuple[LocalResultStore, PosixAuditJournal, PaperTradingSession, ControlledClock, Path]:
    root = tmp_path / "results"
    root.mkdir()
    store = LocalResultStore(root.resolve())
    reference = RunReference(RUN_ID, LINEAGE)
    manifest_document = {
        "schema": "ea.local-paper-attempt.v1",
        "run_id": RUN_ID.value,
        "lineage_sha256": LINEAGE.value,
    }
    manifest_bytes = json.dumps(manifest_document, sort_keys=True, separators=(",", ":")).encode()
    prepared = store.prepare_canonical_attempt(CanonicalAttemptManifest(reference, manifest_bytes))
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
        crash_after=crash_after,
    )
    return store, journal, engine, clock, root / RUN_ID.value


def _drive_and_release(
    engine: PaperTradingSession,
    clock: ControlledClock,
    store: LocalResultStore,
    journal: PosixAuditJournal,
    *,
    crash_after: AuditRecordKind | None,
) -> None:
    if crash_after is None:
        drive(engine, clock)
    else:
        with pytest.raises(RuntimeError, match="crash injection"):
            drive(engine, clock)
    journal.close()
    store.close()


def test_crash_before_effect_restart_is_unknown_and_requires_reconciliation(
    tmp_path: Path,
) -> None:
    store, journal, engine, clock, run_dir = _build_attempt(
        tmp_path, crash_after=AuditRecordKind.PAPER_SUBMISSION_AUTHORIZATION
    )
    _drive_and_release(
        engine, clock, store, journal, crash_after=AuditRecordKind.PAPER_SUBMISSION_AUTHORIZATION
    )
    admission = resume_local_paper(run_dir)
    # The scan still flags the intent as unknown (never blindly resent), but the
    # broker query resolves it: no broker record means the order was never sent.
    assert admission["state"] == "incomplete"
    assert admission["incomplete_outbound_intents"][0]["classification"] == "unknown"
    assert admission["reconciliation_required"] is False
    assert admission["joint"]["recovered"] is True
    assert admission["joint"]["not_sent_orders"] == [1]
    assert admission["joint"]["divergent_orders"] == []


def test_crash_after_broker_accept_is_sent_confirmed_and_never_resent(tmp_path: Path) -> None:
    store, journal, engine, clock, run_dir = _build_attempt(
        tmp_path, crash_after=AuditRecordKind.PAPER_SUBMISSION_RESULT
    )
    _drive_and_release(
        engine, clock, store, journal, crash_after=AuditRecordKind.PAPER_SUBMISSION_RESULT
    )
    admission = resume_local_paper(run_dir)
    # The broker confirms the order is open; it is kept for continuation and
    # never resent, so no duplicate economic effect can occur.
    assert admission["reconciliation_required"] is False
    assert admission["joint"]["recovered"] is True
    assert admission["joint"]["open_orders"] == [1]
    assert admission["joint"]["recovered_fills"] == 0
    intent = admission["incomplete_outbound_intents"][0]
    assert intent["classification"] == "sent_confirmed"
    assert intent["terminal"] is None


def test_crash_after_fill_retains_exactly_one_economic_effect_per_order(tmp_path: Path) -> None:
    store, journal, engine, clock, run_dir = _build_attempt(tmp_path, crash_after=None)
    _drive_and_release(engine, clock, store, journal, crash_after=None)
    admission = resume_local_paper(run_dir)
    # No terminal record was written (crash before publication), yet every
    # outbound intent is durably filled: exactly one economic effect each.
    assert admission["state"] == "incomplete"
    assert admission["reconciliation_required"] is False
    assert len(admission["outbound_intents"]) == 6
    assert all(
        intent["classification"] == "sent_confirmed" and intent["terminal"] == "filled"
        for intent in admission["outbound_intents"]
    )
    joint = admission["joint"]
    assert joint["recovered"] is True
    assert joint["exactly_once"] is True
    assert joint["recovered_fills"] == 6
    assert joint["divergent_orders"] == []


def test_terminal_attempt_resumes_without_incomplete_work(tmp_path: Path) -> None:
    store, journal, engine, clock, run_dir = _build_attempt(tmp_path, crash_after=None)
    drive(engine, clock)
    status = engine.status()
    engine._append(
        AuditRecordKind.RUN_TERMINAL,
        canonical_paper_terminal_payload(
            engine.run_id,
            dispatch_sequence=status["dispatch_sequence"],
            reason="stop_requested",
            portfolio_snapshot_sha256=portfolio_snapshot_digest(
                engine.gate.frontier.current_snapshot()
            ),
            risk_state_sha256=risk_state_snapshot_digest(engine.gate.frontier.current_state()),
            pending_facts=status["pending_facts"],
            pending_orders=status["pending_orders"],
            fill_count=len(engine.committed),
        ),
    )
    journal.close()
    store.close()
    admission = resume_local_paper(run_dir)
    assert admission["state"] == "terminal"
    assert admission["reconciliation_required"] is False
    assert admission["outbound_intents"] == []
