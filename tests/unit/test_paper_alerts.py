"""PPV-06 monitoring and alerts V1: the stream, its dedup and the nine signals.

The tests below are written against real Paper state wherever a projection is
involved, because the value of this unit is that it reads what other owners
already decided. They also pin the boundary: no trading-decision module may
import the alert surface, and the alerting thresholds must never become a second
safety engine.
"""

from __future__ import annotations

import ast
import json
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ea.cli.app import app
from ea.core.run import RunBinding, RunId, RunReference, Sha256Digest
from ea.experiments.audit import PosixAuditJournal, create_posix_audit_journal
from ea.experiments.store import CanonicalAttemptManifest, LocalResultStore
from ea.product.paper_alert_evaluation import (
    evaluate_paper_alerts,
    observe_backup_staleness,
    observe_disk_capacity,
)
from ea.product.paper_alerts import (
    ALERT_SIGNALS,
    DEFAULT_ALERT_THRESHOLDS,
    AlertScope,
    AlertSeverity,
    AlertSignalReading,
    AlertState,
    AlertThresholds,
    AlertType,
    PaperAlertError,
    PaperAlertStreamAbsent,
    PaperAlertUnavailable,
    SignalCondition,
    alert_id_for,
    canonical_alert_stream_bytes,
    decode_alert_stream,
    project_alert_stream,
    read_alert_stream,
    require_host_id,
    write_alert_stream,
)
from ea.product.paper_backup import create_paper_backup, latest_verified_paper_backup
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
)
from ea.product.paper_session import PaperSessionWriter
from ea.risk.kill_switch import (
    canonical_kill_switch_bytes,
    create_operator_kill_switch_authority,
)
from ea.risk.operational_safety import (
    OperationalSafetyAuthority,
    OperationalSafetyLimits,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HOST = "test-host"
RUN_ID = RunId("123e4567-e89b-42d3-a456-426614174000")
OTHER_RUN_ID = RunId("223e4567-e89b-42d3-a456-426614174000")
LINEAGE = Sha256Digest("1" * 64)
INSTANT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@dataclass(slots=True)
class _Clock:
    """A clock the test moves by hand; the evaluator never calls the wall clock."""

    instant: datetime = INSTANT

    def now(self) -> datetime:
        return self.instant


def _limits() -> OperationalSafetyLimits:
    return OperationalSafetyLimits(
        max_market_age_seconds=5.0,
        max_daily_loss=Decimal("1000"),
        max_total_exposure=Decimal("1000"),
        max_open_orders=6,
        max_order_rate=6,
        order_rate_window_seconds=60.0,
        max_price_deviation_bps=250,
    )


def _health_document(
    *,
    observed_at: datetime = INSTANT,
    kill_switch_state: KillSwitchProjectionState = KillSwitchProjectionState.ACTIVE,
    reconciliation_state: ReconciliationState = ReconciliationState.CLEAN,
    strategy_heartbeat_state: StrategyHeartbeatState = StrategyHeartbeatState.LIVE,
    run_id: RunId = RUN_ID,
) -> dict[str, Any]:
    """One real PPV-03 projection, built through PPV-03's own builder."""
    return build_paper_health(
        PaperHealthObservation(
            process_alive=True,
            runtime_state=RuntimeState.RUNNING,
            recovery_state=RecoveryState.NOT_REQUIRED,
            reconciliation_state=reconciliation_state,
            market_state=MarketState.FRESH,
            broker_state=BrokerState.AVAILABLE,
            strategy_heartbeat_state=strategy_heartbeat_state,
            storage_state=StorageState.AVAILABLE,
            kill_switch_state=kill_switch_state,
            safety=PaperSafetyObservation(
                kill_switch_halted=kill_switch_state is KillSwitchProjectionState.HALTED,
                reconciliation_healthy=reconciliation_state is ReconciliationState.CLEAN,
                market_age_seconds=0.5,
                broker_available=True,
                strategy_live=strategy_heartbeat_state is StrategyHeartbeatState.LIVE,
                daily_loss=Decimal("0"),
                current_exposure=Decimal("0"),
                outstanding_order_exposure=Decimal("0"),
                open_order_count=0,
                reference_price=Decimal("100"),
                now_monotonic=1_000.0,
            ),
            observed_at=observed_at,
            run_id=run_id,
            candidate_id="candidate-ppv06",
        ),
        authority=OperationalSafetyAuthority(run_id=run_id, limits=_limits()),
    ).document()


@dataclass(slots=True)
class _Attempt:
    """One real bound attempt whose status the product itself publishes."""

    store: LocalResultStore
    journal: PosixAuditJournal
    run_dir: Path
    binding: RunBinding
    writer: PaperSessionWriter

    def publish(self, document: dict[str, Any]) -> None:
        self.writer.publish(document)

    def quiesce(self) -> None:
        """Release the writer lease the way a controlled stop does."""
        self.journal.close()
        self.store.close()


def _attempt(tmp_path: Path, *, run_id: RunId = RUN_ID) -> _Attempt:
    root = tmp_path / "results"
    root.mkdir()
    store = LocalResultStore(root.resolve())
    reference = RunReference(run_id, LINEAGE)
    payload = json.dumps(
        {
            "schema": "ea.local-paper-attempt.v1",
            "run_id": run_id.value,
            "lineage_sha256": LINEAGE.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    prepared = store.prepare_canonical_attempt(CanonicalAttemptManifest(reference, payload))
    run_dir = root / run_id.value
    return _Attempt(
        store=store,
        journal=create_posix_audit_journal(prepared.audit),
        run_dir=run_dir,
        binding=prepared.audit.binding,
        writer=PaperSessionWriter(run_dir, prepared.audit.binding),
    )


@pytest.fixture
def attempt(tmp_path: Path) -> Iterator[_Attempt]:
    built = _attempt(tmp_path)
    try:
        yield built
    finally:
        built.journal.close()
        built.store.close()


def _canonical(document: object) -> bytes:
    return json.dumps(
        document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _publish_side_records(run_dir: Path) -> None:
    """The durable files a real attempt carries; only their bytes are captured."""
    (run_dir / "funding.json").write_bytes(_canonical({"schema": "ea.funding.v1"}))
    (run_dir / "kill-switch.json").write_bytes(
        canonical_kill_switch_bytes(create_operator_kill_switch_authority(RUN_ID))
    )
    (run_dir / "operational-safety.json").write_bytes(_canonical({"schema": "ea.limits.v1"}))


def _reading(alert_type: AlertType, condition: SignalCondition) -> AlertSignalReading:
    return AlertSignalReading(alert_type, condition, f"observed {alert_type.value}")


def _readings(**active: bool) -> tuple[AlertSignalReading, ...]:
    """One full reading set; a named signal is active, everything else inactive."""
    return tuple(
        _reading(
            spec.type,
            SignalCondition.ACTIVE if active.get(spec.type.value) else SignalCondition.INACTIVE,
        )
        for spec in ALERT_SIGNALS
    )


def _stream(
    readings: tuple[AlertSignalReading, ...],
    *,
    at: datetime = INSTANT,
    run_id: RunId | None = RUN_ID,
    previous: Any = None,
    thresholds: AlertThresholds = DEFAULT_ALERT_THRESHOLDS,
    host_id: str = HOST,
) -> Any:
    return project_alert_stream(
        host_id=host_id,
        readings=readings,
        observed_at=at,
        run_id=run_id,
        account_id="paper.local" if run_id is not None else None,
        previous=previous,
        thresholds=thresholds,
    )


# --------------------------------------------------------------------------- #
# The stream contract
# --------------------------------------------------------------------------- #


def test_stream_round_trips_through_canonical_bytes() -> None:
    stream = _stream(_readings(runtime_down=True))
    payload = canonical_alert_stream_bytes(stream)
    assert canonical_alert_stream_bytes(decode_alert_stream(json.loads(payload))) == payload
    assert stream.document()["schema"] == "ea.paper-alert-stream.v1"


def test_every_evaluation_publishes_every_signal_in_canonical_order() -> None:
    stream = _stream(_readings())
    assert tuple(signal.type for signal in stream.signals) == tuple(
        spec.type for spec in ALERT_SIGNALS
    )
    assert len(ALERT_SIGNALS) == 9


def test_alert_identity_is_derived_from_identity_not_from_time_or_order() -> None:
    spec = ALERT_SIGNALS[0]
    assert spec.scope is AlertScope.RUN
    first = alert_id_for(AlertType.RUNTIME_DOWN, host_id=HOST, run_id=RUN_ID)
    again = alert_id_for(AlertType.RUNTIME_DOWN, host_id=HOST, run_id=RUN_ID)
    assert first == again
    assert first != alert_id_for(AlertType.RUNTIME_DOWN, host_id=HOST, run_id=OTHER_RUN_ID)
    assert first != alert_id_for(AlertType.RUNTIME_DOWN, host_id="other-host", run_id=RUN_ID)
    assert first != alert_id_for(AlertType.UNEXPECTED_RESTART, host_id=HOST, run_id=RUN_ID)
    # A host-scoped signal is one per host, so it carries no run identity at all.
    assert alert_id_for(AlertType.BACKUP_STALE, host_id=HOST, run_id=None) == (
        f"{HOST}|backup_stale"
    )
    with pytest.raises(PaperAlertError, match="host-scoped"):
        alert_id_for(AlertType.BACKUP_STALE, host_id=HOST, run_id=RUN_ID)


def test_first_occurrence_opens_active_and_a_repeat_only_moves_last_seen() -> None:
    opened = _stream(_readings(runtime_down=True))
    (alert,) = opened.alerts
    assert alert.state is AlertState.ACTIVE
    assert alert.first_seen == alert.last_seen == INSTANT

    later = INSTANT + timedelta(minutes=5)
    continued = _stream(_readings(runtime_down=True), at=later, previous=opened)
    (still,) = continued.alerts
    assert still.alert_id == alert.alert_id
    assert still.state is AlertState.ACTIVE
    assert still.first_seen == INSTANT
    assert still.last_seen == later


def test_recovery_resolves_and_reoccurrence_opens_a_new_alert() -> None:
    opened = _stream(_readings(runtime_down=True))
    recovered_at = INSTANT + timedelta(minutes=1)
    recovered = _stream(_readings(), at=recovered_at, previous=opened)
    (resolved,) = recovered.alerts
    assert resolved.state is AlertState.RESOLVED
    assert resolved.first_seen == INSTANT
    assert resolved.last_seen == recovered_at

    again_at = recovered_at + timedelta(minutes=1)
    again = _stream(_readings(runtime_down=True), at=again_at, previous=recovered)
    (reopened,) = again.alerts
    assert reopened.alert_id == resolved.alert_id
    assert reopened.state is AlertState.ACTIVE
    # A new incident, not a silent flip of the resolved record.
    assert reopened.first_seen == again_at


def test_an_unavailable_reading_never_resolves_an_open_alert() -> None:
    opened = _stream(_readings(runtime_down=True))
    blind = tuple(
        AlertSignalReading(spec.type, SignalCondition.UNAVAILABLE, "cannot see")
        for spec in ALERT_SIGNALS
    )
    later = INSTANT + timedelta(minutes=1)
    stream = _stream(blind, at=later, previous=opened)
    (held,) = stream.alerts
    assert held.state is AlertState.ACTIVE
    assert held.last_seen == INSTANT
    assert all(signal.condition is SignalCondition.UNAVAILABLE for signal in stream.signals)


def test_alerts_for_another_run_are_carried_forward_untouched() -> None:
    other = _stream(_readings(runtime_down=True), run_id=OTHER_RUN_ID)
    (alert,) = other.alerts
    later = INSTANT + timedelta(minutes=1)
    # Observing a different run must not drop the other run's open alert.
    stream = _stream(_readings(), at=later, previous=other)
    assert alert in stream.alerts
    assert stream.active_alerts() == (alert,)


def test_only_resolved_alerts_are_pruned_never_an_open_one() -> None:
    # A run that opens and recovers repeatedly leaves a long resolved history.
    stream = _stream(_readings(runtime_down=True))
    for step in range(1, 40):
        stream = _stream(
            _readings(),
            at=INSTANT + timedelta(minutes=2 * step - 1),
            previous=stream,
        )
        stream = _stream(
            _readings(runtime_down=True),
            at=INSTANT + timedelta(minutes=2 * step),
            previous=stream,
        )
    resolved = [alert for alert in stream.alerts if alert.state is AlertState.RESOLVED]
    assert len(resolved) <= 64
    assert stream.active_alerts()


def test_thresholded_signal_does_not_alert_before_its_own_threshold() -> None:
    thresholds = AlertThresholds(not_ready_seconds=300.0)
    first = _stream(_readings(not_ready_beyond_threshold=True), at=INSTANT, thresholds=thresholds)
    assert first.alerts == ()
    assert first.signals[1].condition is SignalCondition.ACTIVE
    assert first.signals[1].active_since == INSTANT

    before = _stream(
        _readings(not_ready_beyond_threshold=True),
        at=INSTANT + timedelta(seconds=299),
        previous=first,
        thresholds=thresholds,
    )
    assert before.alerts == ()

    after = _stream(
        _readings(not_ready_beyond_threshold=True),
        at=INSTANT + timedelta(seconds=300),
        previous=before,
        thresholds=thresholds,
    )
    (alert,) = after.alerts
    assert alert.type is AlertType.NOT_READY_BEYOND_THRESHOLD
    assert alert.first_seen == INSTANT + timedelta(seconds=300)


def _referenced_names(relative: str) -> set[str]:
    """Every name the module actually references, with prose deliberately excluded.

    A docstring saying "this is NOT OperationalSafetyLimits" is not a dependency;
    an import or a reference is. Parsing is what tells the two apart.
    """
    tree = ast.parse((REPO_ROOT / relative).read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.update((alias.name, alias.name.rpartition(".")[2]))
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(alias.name for alias in node.names)
    return names


def test_alerting_thresholds_are_not_a_safety_engine() -> None:
    forbidden = {
        "operational_safety",
        "OperationalSafetyLimits",
        "OperationalSafetyAuthority",
        "OperationalSafetyInput",
        "OperationalSafetyVerdict",
    }
    for relative in (
        "src/ea/product/paper_alerts.py",
        "src/ea/product/paper_alert_evaluation.py",
    ):
        assert _referenced_names(relative) & forbidden == set(), relative
    assert not hasattr(DEFAULT_ALERT_THRESHOLDS, "max_order_rate")
    assert not hasattr(DEFAULT_ALERT_THRESHOLDS, "max_daily_loss")


def test_no_trading_decision_path_reads_the_alert_stream() -> None:
    for relative in (
        "src/ea/risk/operational_safety.py",
        "src/ea/risk/kill_switch.py",
        "src/ea/product/paper.py",
        "src/ea/product/paper_run.py",
        "src/ea/product/paper_health.py",
        "src/ea/product/paper_session.py",
        "src/ea/product/market_stream.py",
    ):
        named = _referenced_names(relative)
        assert not any("paper_alert" in name for name in named), relative


# --------------------------------------------------------------------------- #
# Durable publication and read-only inspection
# --------------------------------------------------------------------------- #


def test_published_stream_is_private_atomic_and_replaceable(tmp_path: Path) -> None:
    target = tmp_path / "alerts" / "paper-alerts.json"
    first = _stream(_readings(runtime_down=True))
    write_alert_stream(target, first)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert sorted(path.name for path in target.parent.iterdir()) == ["paper-alerts.json"]
    assert read_alert_stream(target) == first

    second = _stream(_readings(), at=INSTANT + timedelta(minutes=1), previous=first)
    write_alert_stream(target, second)
    assert read_alert_stream(target) == second
    assert sorted(path.name for path in target.parent.iterdir()) == ["paper-alerts.json"]


def test_a_malformed_stream_is_unavailable_and_never_an_empty_alert_set(tmp_path: Path) -> None:
    target = tmp_path / "paper-alerts.json"
    target.write_bytes(b'{"schema":"ea.paper-alert-stream.v1"}')
    with pytest.raises(PaperAlertUnavailable):
        read_alert_stream(target)


def test_a_non_canonical_stream_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "paper-alerts.json"
    write_alert_stream(target, _stream(_readings()))
    payload = json.loads(target.read_bytes())
    target.write_bytes(json.dumps(payload, indent=2).encode())
    with pytest.raises(PaperAlertUnavailable):
        read_alert_stream(target)


def test_an_absent_stream_is_absent_rather_than_empty(tmp_path: Path) -> None:
    with pytest.raises(PaperAlertStreamAbsent):
        read_alert_stream(tmp_path / "missing.json")


def test_unknown_fields_in_a_durable_stream_are_a_conflict(tmp_path: Path) -> None:
    stream = _stream(_readings())
    document = stream.document()
    document["unexpected"] = True
    with pytest.raises(PaperAlertError, match="unknown"):
        decode_alert_stream(document)
    document.pop("unexpected")
    document["alerts"] = [{"alert_id": "x"}]
    with pytest.raises(PaperAlertError, match="unknown or missing"):
        decode_alert_stream(document)


def test_alert_identity_must_bind_to_the_stream_host() -> None:
    stream = _stream(_readings(runtime_down=True))
    document = stream.document()
    document["alerts"][0]["alert_id"] = "someone-else|runtime_down"
    with pytest.raises(PaperAlertError, match="bind"):
        decode_alert_stream(document)


def test_signal_states_require_a_start_time_exactly_when_active() -> None:
    from ea.product.paper_alerts import AlertSignalState

    with pytest.raises(PaperAlertError, match="carries when it began"):
        AlertSignalState(
            type=AlertType.RUNTIME_DOWN,
            scope=AlertScope.RUN,
            severity=AlertSeverity.CRITICAL,
            condition=SignalCondition.ACTIVE,
            active_since=None,
            observation="active with no start",
        )
    with pytest.raises(PaperAlertError, match="unavailable is not active"):
        AlertSignalState(
            type=AlertType.RUNTIME_DOWN,
            scope=AlertScope.RUN,
            severity=AlertSeverity.CRITICAL,
            condition=SignalCondition.UNAVAILABLE,
            active_since=INSTANT,
            observation="unavailable with a start",
        )


def test_threshold_policy_rejects_a_non_positive_or_non_finite_value() -> None:
    with pytest.raises(PaperAlertError):
        AlertThresholds(not_ready_seconds=0.0)
    with pytest.raises(PaperAlertError):
        AlertThresholds(backup_stale_seconds=float("inf"))
    with pytest.raises(PaperAlertError):
        AlertThresholds(disk_free_floor_bytes=-1)


def test_host_scope_labels_are_bounded() -> None:
    assert require_host_id("host-1.example") == "host-1.example"
    for bad in ("", "-leading", "has space", "a" * 65):
        with pytest.raises(PaperAlertError):
            require_host_id(bad)


def test_an_evaluation_cannot_travel_backwards_in_time() -> None:
    stream = _stream(_readings(), at=INSTANT)
    with pytest.raises(PaperAlertError, match="backwards"):
        _stream(_readings(), at=INSTANT - timedelta(seconds=1), previous=stream)


def test_a_stream_belonging_to_another_host_is_refused() -> None:
    stream = _stream(_readings())
    with pytest.raises(PaperAlertError, match="another host"):
        _stream(_readings(), previous=stream, host_id="other-host")


# --------------------------------------------------------------------------- #
# The nine signals, read from the projections that already exist
# --------------------------------------------------------------------------- #


def test_a_healthy_live_run_opens_no_alert_but_still_names_every_signal(
    attempt: _Attempt,
) -> None:
    attempt.publish({"state": "running", "account_id": "paper.local", "health": _health_document()})
    evaluation = evaluate_paper_alerts(run_dir=attempt.run_dir, clock=_Clock())
    stream = evaluation.project(host_id=HOST)

    assert stream.alerts == ()
    conditions = {signal.type: signal.condition for signal in stream.signals}
    for spec in ALERT_SIGNALS:
        if spec.type in (
            AlertType.BACKUP_FAILED,
            AlertType.BROKER_UNAVAILABLE,
            AlertType.BACKUP_STALE,
            # While the attempt runs no terminal journal record is expected yet,
            # so this signal is not assessable rather than false.
            AlertType.UNEXPECTED_RESTART,
        ):
            assert conditions[spec.type] is SignalCondition.UNAVAILABLE
        else:
            assert conditions[spec.type] is SignalCondition.INACTIVE


def test_operator_halt_is_read_from_the_live_projection_not_the_durable_file(
    attempt: _Attempt, tmp_path: Path
) -> None:
    # The trap: outputs/kill-switch.json says the switch is ACTIVE and is never
    # re-persisted after a halt, so reading it would report "not halted" during a
    # real halt. Only the live health projection is allowed to answer.
    durable = attempt.run_dir / "kill-switch.json"
    durable.write_bytes(canonical_kill_switch_bytes(create_operator_kill_switch_authority(RUN_ID)))
    attempt.publish(
        {
            "state": "running",
            "account_id": "paper.local",
            "health": _health_document(kill_switch_state=KillSwitchProjectionState.HALTED),
        }
    )
    stream = evaluate_paper_alerts(run_dir=attempt.run_dir, clock=_Clock()).project(host_id=HOST)
    (alert,) = stream.active_alerts()
    assert alert.type is AlertType.OPERATOR_HALT_ACTIVE
    assert alert.run_id == RUN_ID
    assert "halt" in alert.observation


def test_a_released_lease_over_a_live_state_alerts_runtime_down(attempt: _Attempt) -> None:
    attempt.publish({"state": "running", "account_id": "paper.local", "health": _health_document()})
    attempt.quiesce()
    evaluation = evaluate_paper_alerts(run_dir=attempt.run_dir, clock=_Clock())
    stream = evaluation.project(host_id=HOST)
    types = {alert.type for alert in stream.active_alerts()}
    assert AlertType.RUNTIME_DOWN in types
    # The process is gone, so the last journal record cannot be run.terminal
    # either: the run never recorded a durable terminal state.
    assert AlertType.UNEXPECTED_RESTART in types
    assert stream.active_alerts()[0].run_id == RUN_ID


def test_an_attempt_that_ended_as_designed_alerts_nothing(attempt: _Attempt) -> None:
    attempt.publish(
        {
            "state": "stopped",
            "account_id": "paper.local",
            "terminal_durable": True,
            "health": _health_document(),
        }
    )
    attempt.quiesce()
    stream = evaluate_paper_alerts(run_dir=attempt.run_dir, clock=_Clock()).project(host_id=HOST)
    assert stream.active_alerts() == ()


def test_an_attempt_that_failed_without_recording_a_terminal_is_an_unexpected_restart(
    attempt: _Attempt,
) -> None:
    attempt.publish(
        {
            "state": "failed",
            "account_id": "paper.local",
            "terminal_durable": False,
            "health": _health_document(),
        }
    )
    attempt.quiesce()
    stream = evaluate_paper_alerts(run_dir=attempt.run_dir, clock=_Clock()).project(host_id=HOST)
    types = {alert.type for alert in stream.active_alerts()}
    assert types == {AlertType.UNEXPECTED_RESTART}


def test_unresolved_reconciliation_alerts_while_the_runtime_stays_alive(
    attempt: _Attempt,
) -> None:
    attempt.publish(
        {
            "state": "running",
            "account_id": "paper.local",
            "health": _health_document(reconciliation_state=ReconciliationState.UNRESOLVED),
        }
    )
    stream = evaluate_paper_alerts(run_dir=attempt.run_dir, clock=_Clock()).project(host_id=HOST)
    (alert,) = stream.active_alerts()
    assert alert.type is AlertType.RECONCILIATION_CONFLICT


def test_an_unknown_reconciliation_state_is_unavailable_not_healthy(
    attempt: _Attempt,
) -> None:
    attempt.publish(
        {
            "state": "running",
            "account_id": "paper.local",
            "health": _health_document(reconciliation_state=ReconciliationState.UNKNOWN),
        }
    )
    stream = evaluate_paper_alerts(run_dir=attempt.run_dir, clock=_Clock()).project(host_id=HOST)
    signal = next(
        state for state in stream.signals if state.type is AlertType.RECONCILIATION_CONFLICT
    )
    assert signal.condition is SignalCondition.UNAVAILABLE
    assert stream.alerts == ()


def test_not_ready_beyond_the_threshold_alerts_only_after_the_elapsed_policy(
    attempt: _Attempt,
) -> None:
    attempt.publish(
        {
            "state": "running",
            "account_id": "paper.local",
            "health": _health_document(strategy_heartbeat_state=StrategyHeartbeatState.NOT_LIVE),
        }
    )
    thresholds = AlertThresholds(not_ready_seconds=300.0)
    first = evaluate_paper_alerts(
        run_dir=attempt.run_dir, clock=_Clock(), thresholds=thresholds
    ).project(host_id=HOST, thresholds=thresholds)
    assert first.alerts == ()

    later = _Clock(INSTANT + timedelta(seconds=301))
    second = evaluate_paper_alerts(
        run_dir=attempt.run_dir, clock=later, thresholds=thresholds
    ).project(host_id=HOST, previous=first, thresholds=thresholds)
    (alert,) = second.active_alerts()
    assert alert.type is AlertType.NOT_READY_BEYOND_THRESHOLD
    assert alert.severity is AlertSeverity.WARNING


def test_backup_stale_ages_the_newest_verified_backup(attempt: _Attempt, tmp_path: Path) -> None:
    attempt.publish({"state": "running", "account_id": "paper.local"})
    _publish_side_records(attempt.run_dir)
    attempt.quiesce()
    backup_root = tmp_path / "backups"
    result = create_paper_backup(attempt.run_dir, backup_root)

    recorded = latest_verified_paper_backup(backup_root)
    assert recorded is not None
    created_at = recorded.created_at
    fresh = observe_backup_staleness(
        backup_root, now=created_at + timedelta(seconds=1), thresholds=DEFAULT_ALERT_THRESHOLDS
    )
    assert fresh.condition is SignalCondition.INACTIVE

    # Age the recorded creation instant without touching the verified bytes: the
    # boundary digest covers the captured files, not the manifest timestamp, so a
    # restamped backup is still a backup that verifies.
    document = json.loads((result.backup_dir / "backup.json").read_bytes())
    document["created_at"] = (created_at - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    (result.backup_dir / "backup.json").write_bytes(_canonical(document))
    stale = observe_backup_staleness(
        backup_root, now=created_at, thresholds=DEFAULT_ALERT_THRESHOLDS
    )
    assert stale.condition is SignalCondition.ACTIVE
    assert "verified backup" in stale.observation


def test_backup_stale_skips_a_backup_that_no_longer_verifies(
    attempt: _Attempt, tmp_path: Path
) -> None:
    attempt.publish({"state": "running", "account_id": "paper.local"})
    _publish_side_records(attempt.run_dir)
    attempt.quiesce()
    backup_root = tmp_path / "backups"
    create_paper_backup(attempt.run_dir, backup_root)
    # Corrupt the captured journal: the newest directory is no longer a usable
    # backup, and the age question must be answered by what still verifies.
    journal = next(backup_root.glob("backup-*")) / "files" / "audit" / "audit-v1.journal"
    journal.write_bytes(journal.read_bytes() + b"tampered")
    assert latest_verified_paper_backup(backup_root) is None
    unverified = observe_backup_staleness(
        backup_root, now=datetime.now(UTC), thresholds=DEFAULT_ALERT_THRESHOLDS
    )
    assert unverified.condition is SignalCondition.UNAVAILABLE
    assert "no verified backup" in unverified.observation


def test_backup_stale_is_unavailable_without_a_backup_root() -> None:
    reading = observe_backup_staleness(None, now=INSTANT, thresholds=DEFAULT_ALERT_THRESHOLDS)
    assert reading.condition is SignalCondition.UNAVAILABLE


def test_latest_verified_backup_can_be_narrowed_to_one_run(
    attempt: _Attempt, tmp_path: Path
) -> None:
    attempt.publish({"state": "running", "account_id": "paper.local"})
    _publish_side_records(attempt.run_dir)
    attempt.quiesce()
    backup_root = tmp_path / "backups"
    created = create_paper_backup(attempt.run_dir, backup_root)
    assert latest_verified_paper_backup(backup_root, run_id=RUN_ID) is not None
    assert latest_verified_paper_backup(backup_root, run_id=OTHER_RUN_ID) is None
    newest = latest_verified_paper_backup(backup_root)
    assert newest is not None
    assert newest.manifest == created.manifest
    with pytest.raises(Exception, match="absolute"):
        latest_verified_paper_backup(Path("relative"))


def test_disk_capacity_low_follows_the_configured_floor(tmp_path: Path) -> None:
    low = observe_disk_capacity(tmp_path, thresholds=AlertThresholds(disk_free_floor_bytes=1 << 62))
    assert low.condition is SignalCondition.ACTIVE
    high = observe_disk_capacity(tmp_path, thresholds=AlertThresholds(disk_free_floor_bytes=0))
    assert high.condition is SignalCondition.INACTIVE
    blind = observe_disk_capacity(tmp_path / "missing", thresholds=DEFAULT_ALERT_THRESHOLDS)
    assert blind.condition is SignalCondition.UNAVAILABLE
    assert observe_disk_capacity(None, thresholds=DEFAULT_ALERT_THRESHOLDS).condition is (
        SignalCondition.UNAVAILABLE
    )


def test_backup_failed_and_broker_unavailable_are_always_unavailable(
    attempt: _Attempt,
) -> None:
    attempt.publish({"state": "running", "account_id": "paper.local", "health": _health_document()})
    stream = evaluate_paper_alerts(run_dir=attempt.run_dir, clock=_Clock()).project(host_id=HOST)
    by_type = {signal.type: signal for signal in stream.signals}
    assert by_type[AlertType.BACKUP_FAILED].condition is SignalCondition.UNAVAILABLE
    assert "no durable per-attempt backup outcome record" in (
        by_type[AlertType.BACKUP_FAILED].observation
    )
    assert by_type[AlertType.BROKER_UNAVAILABLE].condition is SignalCondition.UNAVAILABLE
    assert "PPV-17" in by_type[AlertType.BROKER_UNAVAILABLE].observation


def test_an_unreadable_attempt_is_recorded_never_raised(tmp_path: Path) -> None:
    evaluation = evaluate_paper_alerts(run_dir=tmp_path / "not-an-attempt", clock=_Clock())
    assert evaluation.run_id is None
    run_scoped = {
        AlertType.RUNTIME_DOWN,
        AlertType.NOT_READY_BEYOND_THRESHOLD,
        AlertType.RECONCILIATION_CONFLICT,
        AlertType.OPERATOR_HALT_ACTIVE,
        AlertType.UNEXPECTED_RESTART,
    }
    for reading in evaluation.readings:
        if reading.type in run_scoped:
            assert reading.condition is SignalCondition.UNAVAILABLE
    assert evaluation.project(host_id=HOST).alerts == ()
    with pytest.raises(PaperAlertError, match="absolute"):
        evaluate_paper_alerts(run_dir=Path("relative"), clock=_Clock())


def test_a_run_that_publishes_an_unavailable_health_marker_is_not_treated_as_healthy(
    attempt: _Attempt,
) -> None:
    attempt.publish(
        {
            "state": "running",
            "account_id": "paper.local",
            "health": {
                "schema": "ea.paper-health-unavailable.v1",
                "available": False,
                "detail": "x",
            },
        }
    )
    stream = evaluate_paper_alerts(run_dir=attempt.run_dir, clock=_Clock()).project(host_id=HOST)
    by_type = {signal.type: signal for signal in stream.signals}
    for alert_type in (
        AlertType.NOT_READY_BEYOND_THRESHOLD,
        AlertType.RECONCILIATION_CONFLICT,
        AlertType.OPERATOR_HALT_ACTIVE,
    ):
        assert by_type[alert_type].condition is SignalCondition.UNAVAILABLE
    assert stream.alerts == ()


# --------------------------------------------------------------------------- #
# Delivery: the CLI surface
# --------------------------------------------------------------------------- #


def test_cli_reports_an_absent_stream_as_unavailable_with_exit_three(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["paper", "alerts", "show", "--stream", str(tmp_path / "missing.json")]
    )
    assert result.exit_code == 3
    assert "unavailable" in result.output


def test_cli_evaluates_publishes_and_reads_back_one_line_of_canonical_json(
    attempt: _Attempt, tmp_path: Path
) -> None:
    attempt.publish({"state": "running", "account_id": "paper.local", "health": _health_document()})
    target = tmp_path / "alerts" / "paper-alerts.json"
    runner = CliRunner()
    published = runner.invoke(
        app,
        [
            "paper",
            "alerts",
            "evaluate",
            "--run-dir",
            str(attempt.run_dir),
            "--stream",
            str(target),
            "--host-id",
            HOST,
        ],
    )
    assert published.exit_code == 0, published.output
    payload = target.read_bytes()
    assert payload == canonical_alert_stream_bytes(read_alert_stream(target))
    assert published.output.strip() == payload.decode("ascii")
    assert json.loads(payload)["host_id"] == HOST

    shown = runner.invoke(app, ["paper", "alerts", "show", "--stream", str(target)])
    assert shown.exit_code == 0
    assert shown.output.strip() == payload.decode("ascii")


def test_cli_refuses_to_overwrite_a_stream_it_cannot_read(
    attempt: _Attempt, tmp_path: Path
) -> None:
    attempt.publish({"state": "running", "account_id": "paper.local"})
    target = tmp_path / "paper-alerts.json"
    target.write_bytes(b"not json at all")
    result = CliRunner().invoke(
        app,
        [
            "paper",
            "alerts",
            "evaluate",
            "--run-dir",
            str(attempt.run_dir),
            "--stream",
            str(target),
            "--host-id",
            HOST,
        ],
    )
    assert result.exit_code == 3
    assert target.read_bytes() == b"not json at all"


def test_cli_paper_help_lists_the_alerts_entry() -> None:
    result = CliRunner().invoke(app, ["paper", "--help"])
    assert result.exit_code == 0
    assert "alerts" in result.output
    nested = CliRunner().invoke(app, ["paper", "alerts", "--help"])
    assert nested.exit_code == 0
    assert "evaluate" in nested.output
    assert "show" in nested.output


def test_cli_alerts_requires_a_subcommand(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["paper", "alerts"])
    assert result.exit_code == 2
    assert "evaluate or show" in result.output
