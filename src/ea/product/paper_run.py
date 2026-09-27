"""Fresh attempt admission and real process lifetime for installed local Paper."""

from __future__ import annotations

import json
import signal
import time
from collections.abc import Callable
from hashlib import sha256
from math import isfinite
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

from ea.core import AuditRecordKind, RunId, RunReference, Sha256Digest, SourceId
from ea.core.paper import canonical_paper_terminal_payload
from ea.core.portfolio import portfolio_snapshot_digest
from ea.core.risk import risk_state_snapshot_digest
from ea.experiments.audit import (
    PosixAuditJournal,
    create_posix_audit_journal,
    reopen_posix_audit_journal,
)
from ea.experiments.store import (
    CanonicalAttemptManifest,
    LocalResultStore,
    VerifiedIncompleteRecoveryBinding,
    VerifiedTerminalRecoveryBinding,
)
from ea.observability import JsonlFileSink, OperationalContext, OperationalLogger
from ea.product.backtest import _funding_document, _safe_output_root, _write_once
from ea.product.candidate import (
    _configuration,
    _load_binding,
    _preflight,
    inspect_candidate_binding,
)
from ea.product.market_stream import (
    LocalSimulatedMarketSource,
    RealUtcClock,
    run_local_market_stream,
)
from ea.product.paper import PaperTradingSession
from ea.product.paper_recovery import (
    PaperOutboundIntent,
    reconcile_paper_recovery,
    recover_paper_broker_observation,
    scan_paper_outbound_intents,
)
from ea.product.paper_session import PaperSessionWriter, read_paper_binding
from ea.product.scenario import _load_scenario_document
from ea.risk.kill_switch import (
    canonical_kill_switch_bytes,
    create_operator_kill_switch_authority,
)
from ea.risk.operational_safety import canonical_operational_safety_limits_bytes
from ea.strategy.catalog import ResearchStrategyCatalogV1


def _canonical(document: object) -> bytes:
    return json.dumps(
        document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def run_local_paper(
    workspace: Path,
    candidate_id: str,
    scenario_path: Path,
    output_root: Path,
    *,
    prices: tuple[float, ...] = (100.0,),
    interval: float = 0.1,
    event_limit: int | None = None,
    run_id: RunId | None = None,
    on_ready: Callable[[Path], None] | None = None,
    crash_after: AuditRecordKind | None = None,
) -> dict[str, Any]:
    """Explicitly start one fresh local simulation; no recovery or external transport."""
    if (
        type(prices) is not tuple
        or not 1 <= len(prices) <= 64
        or any(type(p) is not float or not isfinite(p) or p <= 0 for p in prices)
    ):
        raise ValueError("Paper prices require 1..64 positive finite values")
    if type(interval) is not float or not isfinite(interval) or not 0.01 <= interval <= 10.0:
        raise ValueError("Paper interval must be in 0.01..10 seconds")
    if event_limit is not None and (type(event_limit) is not int or event_limit < 1):
        raise ValueError("Paper event limit must be positive")
    run_id = RunId(str(uuid4())) if run_id is None else run_id
    output_root = _safe_output_root(output_root.expanduser().absolute())
    attempt = output_root / run_id.value
    if attempt.exists() or attempt.is_symlink():
        raise ValueError("Paper requires a fresh attempt; existing state cannot resume")
    # One trust-boundary read retains immutable Candidate bytes all the way to execution.
    accepted = inspect_candidate_binding(workspace.expanduser().resolve(strict=True), candidate_id)
    path = scenario_path.expanduser().resolve(strict=True)
    document = _preflight(path, accepted)
    if document["schema_version"] not in (4, 5):
        raise ValueError("Paper requires accepted Action V2 long-only V4/V5 configuration")
    if _configuration(document) != accepted._configuration_bytes:
        raise ValueError("Paper scenario differs from accepted normalized configuration")
    accepted = _load_binding(accepted)
    catalog = ResearchStrategyCatalogV1(
        () if accepted.strategy_package is None else (accepted.strategy_package,)
    )
    scenario = _load_scenario_document(path, document, catalog=catalog)
    if _configuration(json.loads(scenario.canonical_bytes)) != accepted._configuration_bytes:
        raise ValueError("Paper loaded configuration conflicts with acceptance")
    artifact = (
        None if scenario.strategy_package is None else scenario.strategy_package.artifact_bytes
    )
    if artifact != accepted._artifact_bytes:
        raise ValueError("Paper loaded artifact conflicts with acceptance")
    operation = {
        "operation": "paper.start",
        "account_id": "paper.local",
        "candidate": accepted.document(),
        "scenario": json.loads(scenario.canonical_bytes),
        "prices": list(prices),
        "interval_seconds": interval,
        "event_limit": event_limit,
    }
    lineage = Sha256Digest(
        sha256(b"ea.local-paper-lineage.v1\0" + _canonical(operation)).hexdigest()
    )
    clock = RealUtcClock()
    started = clock.now().isoformat()
    manifest_bytes = _canonical(
        {
            "schema": "ea.local-paper-attempt.v1",
            "run_id": run_id.value,
            "lineage_sha256": lineage.value,
            "started_at": started,
            **operation,
        }
    )
    manifest = CanonicalAttemptManifest(RunReference(run_id, lineage), manifest_bytes)
    store = LocalResultStore(output_root)
    journal: PosixAuditJournal | None = None
    source: LocalSimulatedMarketSource | None = None
    engine: PaperTradingSession | None = None
    writer: PaperSessionWriter | None = None
    stopping = Event()
    previous_handlers: dict[signal.Signals, Any] = {}
    result: dict[str, Any] = {
        "state": "starting",
        "started_at": started,
        "candidate_id": candidate_id,
        "account_id": "paper.local",
        "run_dir": str(attempt),
        "run_id": run_id.value,
    }
    logger = OperationalLogger(
        OperationalContext(
            run_id.value,
            strategy_id=scenario.strategy_id.value,
            candidate_id=candidate_id,
            account_id="paper.local",
            operation="paper.start",
        ),
        JsonlFileSink(attempt / "operational.jsonl"),
    )

    def request_stop(_signum: int, _frame: object) -> None:
        stopping.set()

    def should_stop() -> bool:
        assert writer is not None
        return stopping.is_set() or writer.stop_requested()

    def publish() -> None:
        assert writer is not None and engine is not None
        result.update(engine.status())
        result["updated_at"] = clock.now().isoformat()
        result["operational_log_failures"] = logger.failure_count
        writer.publish(result)

    def sleep(seconds: float) -> None:
        # Bound cooperative-file stop latency even for a slow configured feed.
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if should_stop():
                return
            stopping.wait(min(0.1, max(0.0, deadline - time.monotonic())))

    try:
        prepared = store.prepare_canonical_attempt(manifest)
        writer = PaperSessionWriter(attempt, manifest.binding)
        writer.publish(result)
        journal = create_posix_audit_journal(prepared.audit)
        source_id = SourceId("paper.local.simulated")
        source = LocalSimulatedMarketSource(
            instrument=scenario.instrument,
            source_id=source_id,
            prices=prices,
            repeats=None,
            bar_seconds=interval,
            event_limit=event_limit,
        )
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, request_stop)
        kill_switch = create_operator_kill_switch_authority(run_id)
        engine = PaperTradingSession(
            scenario,
            binding=manifest.binding,
            audit=journal,
            clock=clock,
            monotonic=time.monotonic,
            source_id=source_id,
            prices=prices,
            stop_requested=should_stop,
            operational_logger=logger,
            max_market_age_seconds=max(5.0, interval * 4),
            stall_timeout_seconds=max(5.0, interval * 4),
            kill_switch=kill_switch,
            crash_after=crash_after,
        )
        _write_once(
            attempt / "funding.json",
            _canonical(_funding_document(engine.funding, engine.gate.funding_outcome)),
        )
        _write_once(
            attempt / "kill-switch.json",
            canonical_kill_switch_bytes(kill_switch),
        )
        _write_once(
            attempt / "operational-safety.json",
            canonical_operational_safety_limits_bytes(engine.operational_safety.limits),
        )
        if artifact is not None:
            _write_once(attempt / "strategy.eastrategy", artifact)
        logger.emit("paper.started")
        publish()
        if on_ready is not None:
            on_ready(attempt)
        stream = run_local_market_stream(
            source=source,
            runtime=engine.runtime,
            clock=clock,
            sleep=sleep,
            poll_interval_seconds=interval,
            on_market=engine.on_market,
            on_fact=engine.on_fact,
            on_heartbeat=lambda _: publish(),
            stop_requested=should_stop,
        )
        result.update(engine.status())
        result["reconciliation"] = (
            engine.reconcile() if engine.completed_sequence else "not_required_no_dispatch"
        )
        result["state"] = (
            "stopped"
            if stream.reason is not None and stream.reason.value == "stop_requested"
            else "failed"
        )
        engine._append(
            AuditRecordKind.RUN_TERMINAL,
            canonical_paper_terminal_payload(
                run_id,
                dispatch_sequence=stream.dispatch_sequence,
                reason=str(result["reason"]),
                portfolio_snapshot_sha256=portfolio_snapshot_digest(
                    engine.gate.frontier.current_snapshot()
                ),
                risk_state_sha256=risk_state_snapshot_digest(engine.gate.frontier.current_state()),
                pending_facts=stream.pending_facts,
                pending_orders=int(engine.pending is not None),
                fill_count=len(engine.committed),
            ),
        )
        result["terminal_durable"] = True
    except BaseException as error:
        if writer is None:
            raise
        if engine is not None:
            result.update(engine.status())
        result.update(
            state="failed",
            failure_type=type(error).__name__,
            failure_detail=str(error)[:512],
            incomplete=True,
            terminal_durable=False,
        )
        if result.get("reason") is None:
            result["reason"] = "startup_or_publication_failure"
        logger.emit(
            "paper.failed", outcome=str(result["reason"]), failure_type=type(error).__name__
        )
        # Do not retry failed dispatches, invent a durable terminal or resume this attempt.
    finally:
        try:
            if writer is not None:
                result["ended_at"] = clock.now().isoformat()
                result["operational_log_failures"] = logger.failure_count
                writer.publish(result)
                logger.emit(
                    "paper.stopped" if result["state"] == "stopped" else "paper.failed",
                    outcome=str(result.get("reason")),
                )
        finally:
            try:
                if source is not None:
                    source.close()
            finally:
                try:
                    if journal is not None:
                        journal.close()
                finally:
                    try:
                        store.close()
                    finally:
                        for signum, previous in previous_handlers.items():
                            signal.signal(signum, previous)
    return result


def _intent_document(intent: PaperOutboundIntent) -> dict[str, Any]:
    return {
        "order_owner_sequence": intent.order_owner_sequence,
        "classification": intent.classification.value,
        "terminal": None if intent.terminal is None else intent.terminal.value,
        "client_submission_key": (
            None if intent.client_submission_key is None else intent.client_submission_key.value
        ),
        "execution_request_sha256": (
            None
            if intent.execution_request_sha256 is None
            else intent.execution_request_sha256.value
        ),
        "venue_order_id": intent.venue_order_id,
        "dispatch_sequence": intent.dispatch_sequence,
    }


def resume_local_paper(run_dir: Path) -> dict[str, Any]:
    """Reopen one existing Paper attempt and admit or reject continued trading.

    Restart admission (PPV-12): reopens the verified manifest, recovers the
    audit journal, and scans durable outbound intents. A terminal attempt has no
    incomplete work; an incomplete attempt that retains any ambiguous or
    submitted-but-unresolved outbound intent reports ``reconciliation_required``
    and must not be resent. This never repairs, rewrites or deletes state.
    """
    run_dir = run_dir.expanduser().absolute()
    binding, manifest_bytes = read_paper_binding(run_dir)
    manifest = CanonicalAttemptManifest(binding.reference, manifest_bytes)
    store = LocalResultStore(_safe_output_root(run_dir.parent))
    try:
        verified = store.verify_recovery_attempt(manifest)
        if type(verified) is VerifiedTerminalRecoveryBinding:
            store.recover_terminal_attempt(verified)
            return {
                "state": "terminal",
                "run_id": binding.reference.run_id.value,
                "reconciliation_required": False,
                "outbound_intents": [],
                "incomplete_outbound_intents": [],
            }
        if type(verified) is VerifiedIncompleteRecoveryBinding:
            recovered = store.recover_incomplete_attempt(verified)
            journal = reopen_posix_audit_journal(recovered.audit)
            try:
                records = journal.recovery_records
                scan = scan_paper_outbound_intents(records)
                broker_observations = recover_paper_broker_observation(records)
                joint = reconcile_paper_recovery(scan, broker_observations)
            finally:
                journal.close()
            intents = [_intent_document(intent) for intent in scan.intents]
            incomplete = [_intent_document(intent) for intent in scan.incomplete]
            return {
                "state": "incomplete",
                "run_id": binding.reference.run_id.value,
                "reconciliation_required": not joint.recovered,
                "outbound_intents": intents,
                "incomplete_outbound_intents": incomplete,
                "joint": {
                    "recovered": joint.recovered,
                    "exactly_once": joint.exactly_once,
                    "recovered_orders": joint.recovered_orders,
                    "recovered_fills": joint.recovered_fills,
                    "open_orders": list(joint.open_orders),
                    "not_sent_orders": list(joint.not_sent_orders),
                    "divergent_orders": list(joint.divergent_orders),
                    "reason": joint.reason,
                },
            }
        raise ValueError("unsupported paper recovery classification")
    finally:
        store.close()
