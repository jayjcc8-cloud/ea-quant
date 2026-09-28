"""PPV-03 health projection: liveness, readiness and trade permission separated."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ea.core import AuditRecord, AuditRecordKind
from ea.core.risk import RiskHaltReason
from ea.experiments.audit import reopen_posix_audit_journal
from ea.experiments.store import (
    CanonicalAttemptManifest,
    LocalResultStore,
    VerifiedIncompleteRecoveryBinding,
)
from ea.product.paper import restore_paper_trading_session
from ea.product.paper_health import (
    LIVENESS_NOT_ALIVE,
    PAPER_HEALTH_SCHEMA,
    PAPER_HEALTH_UNAVAILABLE_SCHEMA,
    PROJECTION_STALE,
    READINESS_BROKER_HEALTH,
    READINESS_MARKET_FRESHNESS,
    READINESS_RECONCILIATION,
    READINESS_RECOVERY,
    READINESS_STORAGE,
    READINESS_STRATEGY_HEARTBEAT,
    RUNTIME_STOPPED,
    TRADE_NO_MARKET_REFERENCE,
    BrokerState,
    KillSwitchProjectionState,
    MarketState,
    PaperHealthError,
    PaperHealthObservation,
    PaperHealthSnapshot,
    PaperSafetyObservation,
    ReconciliationState,
    RecoveryState,
    RuntimeState,
    StorageState,
    StrategyHeartbeatState,
    build_paper_health,
    canonical_paper_health_bytes,
    decode_paper_health,
    is_paper_health_unavailable,
    observed_paper_health,
    paper_health_unavailable_document,
)
from ea.product.paper_run import resume_local_paper
from ea.product.paper_session import read_paper_binding
from ea.risk.kill_switch import (
    KillSwitchScope,
    create_operator_kill_switch_authority,
)
from ea.risk.operational_safety import (
    OperationalSafetyAuthority,
    OperationalSafetyLimits,
    OperationalSafetyVerdict,
)
from unit.test_paper_runtime import drive, session
from unit.test_streaming import RUN_ID, SOURCE

UNDERWAY = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _limits(*, max_order_rate: int = 6) -> OperationalSafetyLimits:
    return OperationalSafetyLimits(
        max_market_age_seconds=5.0,
        max_daily_loss=Decimal("1000"),
        max_total_exposure=Decimal("1000"),
        max_open_orders=6,
        max_order_rate=max_order_rate,
        order_rate_window_seconds=60.0,
        max_price_deviation_bps=250,
    )


def _authority(*, max_order_rate: int = 6) -> OperationalSafetyAuthority:
    return OperationalSafetyAuthority(run_id=RUN_ID, limits=_limits(max_order_rate=max_order_rate))


def _safety(**overrides: Any) -> PaperSafetyObservation:
    values: dict[str, Any] = {
        "kill_switch_halted": False,
        "reconciliation_healthy": True,
        "market_age_seconds": 0.5,
        "broker_available": True,
        "strategy_live": True,
        "daily_loss": Decimal("0"),
        "current_exposure": Decimal("0"),
        "outstanding_order_exposure": Decimal("0"),
        "open_order_count": 0,
        "reference_price": Decimal("100"),
        "now_monotonic": 1_000.0,
    }
    values.update(overrides)
    return PaperSafetyObservation(**values)


def _observation(**overrides: Any) -> PaperHealthObservation:
    values: dict[str, Any] = {
        "process_alive": True,
        "runtime_state": RuntimeState.RUNNING,
        "recovery_state": RecoveryState.NOT_REQUIRED,
        "reconciliation_state": ReconciliationState.CLEAN,
        "market_state": MarketState.FRESH,
        "broker_state": BrokerState.AVAILABLE,
        "strategy_heartbeat_state": StrategyHeartbeatState.LIVE,
        "storage_state": StorageState.AVAILABLE,
        "kill_switch_state": KillSwitchProjectionState.ACTIVE,
        "safety": _safety(),
        "observed_at": UNDERWAY,
        "run_id": RUN_ID,
        "candidate_id": "candidate-1",
    }
    values.update(overrides)
    return PaperHealthObservation(**values)


def _health(**overrides: Any) -> PaperHealthSnapshot:
    """Project one snapshot whose PPV-15 observation matches the named states."""
    values = dict(overrides)
    if "safety" not in values:
        values["safety"] = _safety(
            kill_switch_halted=(
                values.get("kill_switch_state") is KillSwitchProjectionState.HALTED
            ),
            reconciliation_healthy=(
                values.get("reconciliation_state", ReconciliationState.CLEAN)
                is ReconciliationState.CLEAN
            ),
            broker_available=(
                values.get("broker_state", BrokerState.AVAILABLE) is BrokerState.AVAILABLE
            ),
            strategy_live=(
                values.get("strategy_heartbeat_state", StrategyHeartbeatState.LIVE)
                is StrategyHeartbeatState.LIVE
            ),
            market_age_seconds=(90.0 if values.get("market_state") is MarketState.STALE else 0.5),
        )
    return build_paper_health(_observation(**values), authority=_authority())


# --- A: a healthy runtime is alive, ready and permitted -----------------------


def test_healthy_projection_is_alive_ready_and_permitted() -> None:
    snapshot = _health()
    assert snapshot.process_alive is True
    assert snapshot.runtime_ready is True
    assert snapshot.trade_permitted is True
    assert snapshot.operator_halt is False
    assert snapshot.reason_codes == ()
    assert snapshot.trade_blocking_guard is None
    assert (snapshot.run_id, snapshot.candidate_id) == (RUN_ID, "candidate-1")
    assert snapshot.schema == PAPER_HEALTH_SCHEMA


def test_healthy_running_session_projects_alive_ready_and_permitted(tmp_path: Path) -> None:
    engine, clock = session(tmp_path)
    observed: list[PaperHealthSnapshot] = []
    original = engine.on_market

    def observing(root: Any, sequence: int) -> None:
        original(root, sequence)
        if not observed:
            observed.append(
                engine.health(
                    candidate_id="candidate-live",
                    recovery_state=RecoveryState.NOT_REQUIRED,
                    storage_state=StorageState.AVAILABLE,
                )
            )

    engine.on_market = observing  # type: ignore[method-assign]
    drive(engine, clock)

    (snapshot,) = observed
    assert snapshot.process_alive is True
    assert snapshot.runtime_ready is True
    assert snapshot.trade_permitted is True
    assert snapshot.reason_codes == ()
    assert snapshot.runtime_state is RuntimeState.RUNNING
    assert snapshot.market_state is MarketState.FRESH
    assert snapshot.strategy_heartbeat_state is StrategyHeartbeatState.LIVE
    assert snapshot.reconciliation_state is ReconciliationState.CLEAN
    assert snapshot.kill_switch_state is KillSwitchProjectionState.NOT_CONFIGURED


# --- B: an operational failure is alive but not ready -------------------------


def test_reconciliation_conflict_blocks_readiness_but_not_liveness() -> None:
    snapshot = _health(reconciliation_state=ReconciliationState.UNRESOLVED)
    assert snapshot.process_alive is True
    assert snapshot.runtime_ready is False
    assert snapshot.trade_permitted is False
    assert snapshot.reason_codes == (
        READINESS_RECONCILIATION,
        "trade_permission.reconciliation",
    )
    # The authority's own first blocking guard, verbatim.
    assert snapshot.trade_blocking_guard == "reconciliation"
    assert snapshot.trade_reason == "unresolved reconciliation state"


def test_operational_failure_after_a_run_is_alive_but_not_ready(tmp_path: Path) -> None:
    engine, clock = session(tmp_path)
    drive(engine, clock)
    # An unresolved reconciliation state the risk authority already halted on.
    engine.gate.risk_authority.engage_halt(
        RiskHaltReason.RECONCILIATION_REQUIRED,
        clock.now(),
        engine.completed_sequence + 1,
    )
    snapshot = engine.health(
        recovery_state=RecoveryState.NOT_REQUIRED,
        storage_state=StorageState.AVAILABLE,
    )
    assert snapshot.process_alive is True
    assert snapshot.runtime_ready is False
    assert snapshot.reconciliation_state is ReconciliationState.UNRESOLVED
    assert READINESS_RECONCILIATION in snapshot.reason_codes
    assert snapshot.trade_permitted is False
    assert snapshot.trade_blocking_guard == "reconciliation"


# --- C: operator halt is not breakage ----------------------------------------


def test_operator_halt_keeps_readiness_and_blocks_only_permission() -> None:
    snapshot = _health(kill_switch_state=KillSwitchProjectionState.HALTED)
    assert snapshot.process_alive is True
    assert snapshot.runtime_ready is True
    assert snapshot.trade_permitted is False
    assert snapshot.operator_halt is True
    assert snapshot.reason_codes == ("trade_permission.kill_switch",)
    assert snapshot.trade_blocking_guard == "kill_switch"
    assert snapshot.trade_reason == "operator kill switch is halted"


def test_operator_halt_is_alive_and_ready_but_never_permitted(tmp_path: Path) -> None:
    kill_switch = create_operator_kill_switch_authority(RUN_ID)
    engine, clock = session(tmp_path, kill_switch=kill_switch)
    observed: list[PaperHealthSnapshot] = []
    original = engine.on_market

    def observing(root: Any, sequence: int) -> None:
        original(root, sequence)
        if not observed:
            # The operator halts a healthy runtime: nothing is broken, so only
            # trade permission may change.
            kill_switch.halt(
                KillSwitchScope.STRATEGY, reason="operator halt", changed_at=clock.now()
            )
            observed.append(
                engine.health(
                    recovery_state=RecoveryState.NOT_REQUIRED,
                    storage_state=StorageState.AVAILABLE,
                )
            )

    engine.on_market = observing  # type: ignore[method-assign]
    drive(engine, clock)

    (snapshot,) = observed
    assert snapshot.process_alive is True
    assert snapshot.runtime_ready is True
    assert snapshot.trade_permitted is False
    assert snapshot.operator_halt is True
    assert snapshot.kill_switch_state is KillSwitchProjectionState.HALTED
    assert snapshot.reason_codes == ("trade_permission.kill_switch",)
    assert snapshot.trade_blocking_guard == "kill_switch"


# --- the probe is decision-neutral -------------------------------------------


def test_zero_effect_probe_does_not_change_a_later_real_decision() -> None:
    # Three submissions inside the sixty-second window and one long expired: the
    # real decision is made by the rate guard, so the count is load bearing.
    ages = (0.5, 1.0, 2.0, 900.0)
    probed = _authority(max_order_rate=3)
    unprobed = _authority(max_order_rate=3)
    for authority in (probed, unprobed):
        authority.seed_submissions(ages, now_monotonic=1_000.0)
    real = _safety(now_monotonic=1_000.0).proposed_input(
        proposed_order_exposure=Decimal("10"),
        proposed_effective_price=Decimal("100"),
        order_identity="order-under-test",
    )
    before = unprobed.authorize(real)

    # The projection asks first; its probe prunes only already-expired entries.
    assert probed.authorize(_safety(now_monotonic=1_000.0).probe_input()) is not None
    after = probed.authorize(real)

    assert before.verdict is OperationalSafetyVerdict.DENY
    assert before.guard == "order_rate"
    assert (after.verdict, after.guard, after.observed) == (
        before.verdict,
        before.guard,
        before.observed,
    )


def test_reason_codes_never_claim_to_enumerate_every_guard() -> None:
    # Stale market and a broker outage both block, but the authority
    # short-circuits: only the first guard may ever be named.
    snapshot = _health(
        market_state=MarketState.STALE,
        broker_state=BrokerState.UNAVAILABLE,
        safety=_safety(market_age_seconds=90.0, broker_available=False),
    )
    assert snapshot.trade_permitted is False
    assert snapshot.trade_blocking_guard == "market_freshness"
    assert snapshot.reason_codes == (
        READINESS_BROKER_HEALTH,
        READINESS_MARKET_FRESHNESS,
        "trade_permission.market_freshness",
    )


def test_projection_without_a_market_reference_never_trades() -> None:
    snapshot = _health(safety=None, market_state=MarketState.UNKNOWN)
    assert snapshot.process_alive is True
    assert snapshot.trade_permitted is False
    assert snapshot.trade_blocking_guard is None
    assert snapshot.reason_codes == (
        READINESS_MARKET_FRESHNESS,
        TRADE_NO_MARKET_REFERENCE,
    )


def test_readiness_reports_every_unhealthy_dependency() -> None:
    snapshot = _health(
        recovery_state=RecoveryState.RECONCILIATION_REQUIRED,
        reconciliation_state=ReconciliationState.UNRESOLVED,
        market_state=MarketState.STALE,
        broker_state=BrokerState.UNAVAILABLE,
        strategy_heartbeat_state=StrategyHeartbeatState.NOT_LIVE,
        storage_state=StorageState.UNAVAILABLE,
        runtime_state=RuntimeState.STOPPED,
    )
    assert snapshot.runtime_ready is False
    assert set(snapshot.reason_codes) == {
        READINESS_RECOVERY,
        READINESS_RECONCILIATION,
        READINESS_MARKET_FRESHNESS,
        READINESS_BROKER_HEALTH,
        READINESS_STRATEGY_HEARTBEAT,
        READINESS_STORAGE,
        RUNTIME_STOPPED,
        "trade_permission.reconciliation",
    }
    assert snapshot.reason_codes == tuple(sorted(snapshot.reason_codes))


def test_not_alive_is_not_ready_and_never_an_error() -> None:
    snapshot = _health(process_alive=False)
    assert snapshot.process_alive is False
    assert snapshot.runtime_ready is False
    assert LIVENESS_NOT_ALIVE in snapshot.reason_codes


# --- the durable document round-trips ----------------------------------------


def test_canonical_document_round_trips() -> None:
    snapshot = _health()
    payload = canonical_paper_health_bytes(snapshot)
    assert b'"schema":"ea.paper-health.v1"' in payload
    assert decode_paper_health(snapshot.document()) == snapshot
    assert canonical_paper_health_bytes(decode_paper_health(snapshot.document())) == payload


def test_malformed_durable_document_fails_closed() -> None:
    document = _health().document()
    document["runtime_ready"] = "yes"
    with pytest.raises(PaperHealthError, match="runtime_ready"):
        decode_paper_health(document)
    with pytest.raises(PaperHealthError, match="schema"):
        decode_paper_health({**document, "schema": "ea.paper-health.v2"})


def test_unavailable_marker_is_never_read_as_a_projection() -> None:
    marker = paper_health_unavailable_document(detail="RuntimeError: no projection")
    assert marker["schema"] == PAPER_HEALTH_UNAVAILABLE_SCHEMA
    assert is_paper_health_unavailable(marker) is True
    assert is_paper_health_unavailable(_health().document()) is False
    with pytest.raises(PaperHealthError, match="schema"):
        decode_paper_health(marker)


# --- the offline reader re-observes liveness and currency --------------------


def test_offline_reader_overrides_liveness_from_the_writer_lease() -> None:
    snapshot = _health()
    fresh = observed_paper_health(
        snapshot.document(), process_alive=True, now=UNDERWAY, max_projection_age_seconds=5.0
    )
    assert fresh["process_alive"] is True
    assert fresh["projection_stale"] is False
    assert fresh["runtime_ready"] is True

    # The lease is gone, so the process is not alive: truthful non-liveness, not
    # an operational fault, and never a claim of a current readiness verdict.
    dead = observed_paper_health(
        snapshot.document(),
        process_alive=False,
        now=UNDERWAY + timedelta(seconds=1),
        max_projection_age_seconds=5.0,
    )
    assert dead["process_alive"] is False
    assert dead["projection_stale"] is True
    assert dead["runtime_ready"] is False
    assert LIVENESS_NOT_ALIVE in dead["reason_codes"]
    assert PROJECTION_STALE in dead["reason_codes"]
    assert dead["trade_permitted"] is True  # PPV-15's verdict is not re-derived


def test_offline_reader_marks_an_old_projection_stale() -> None:
    snapshot = _health()
    aged = observed_paper_health(
        snapshot.document(),
        process_alive=True,
        now=UNDERWAY + timedelta(seconds=30),
        max_projection_age_seconds=5.0,
    )
    assert aged["age_seconds"] == 30.0
    assert aged["projection_stale"] is True
    assert aged["runtime_ready"] is False
    assert PROJECTION_STALE in aged["reason_codes"]
    # The writer is alive, so this is not reported as process death.
    assert aged["process_alive"] is True
    assert LIVENESS_NOT_ALIVE not in aged["reason_codes"]


# --- D: crash, resume, reconcile, then project health ------------------------


def test_crashed_attempt_resumes_reconciles_and_projects_health(tmp_path: Path) -> None:
    # Reuse the M1 attempt fixture: crash after the broker accepted the Order,
    # leaving one durable "submitted" intent with no terminal fact.
    from unit.test_paper_resume import _build_attempt

    store, journal, engine, clock, run_dir = _build_attempt(
        tmp_path, crash_after=AuditRecordKind.PAPER_SUBMISSION_RESULT
    )
    with pytest.raises(RuntimeError, match="crash injection"):
        drive(engine, clock)
    records: tuple[AuditRecord, ...] = tuple(journal.recovery_records)
    # Release exactly as a dead process would, then take the existing restart
    # admission over the same journal.
    journal.close()
    store.close()
    admission = resume_local_paper(run_dir)
    assert admission["state"] == "incomplete"
    assert admission["reconciliation_required"] is False
    assert admission["joint"]["recovered"] is True
    assert admission["joint"]["open_orders"] == [1]

    # Continue the admitted attempt and project health while it runs.
    binding, manifest_bytes = read_paper_binding(run_dir)
    reopened_store = LocalResultStore(run_dir.parent.resolve())
    try:
        verified = reopened_store.verify_recovery_attempt(
            CanonicalAttemptManifest(binding.reference, manifest_bytes)
        )
        assert type(verified) is VerifiedIncompleteRecoveryBinding
        recovered = reopened_store.recover_incomplete_attempt(verified)
        reopened = reopen_posix_audit_journal(recovered.audit)
    except BaseException:
        reopened_store.close()
        raise
    try:
        restored = restore_paper_trading_session(
            engine.scenario,
            binding=binding,
            audit=reopened,
            records=records,
            clock=clock,
            monotonic=clock.monotonic,
            source_id=SOURCE,
            prices=(100.0,),
            stop_requested=lambda: False,
        )
        observed: list[PaperHealthSnapshot] = []
        original = restored.on_market

        def observing(root: Any, sequence: int) -> None:
            original(root, sequence)
            if not observed:
                observed.append(
                    restored.health(
                        candidate_id="candidate-recovered",
                        recovery_state=RecoveryState.ADMITTED,
                        storage_state=StorageState.AVAILABLE,
                    )
                )

        restored.on_market = observing  # type: ignore[method-assign]
        drive(restored, clock)

        (snapshot,) = observed
        assert snapshot.process_alive is True
        assert snapshot.runtime_ready is True
        assert snapshot.trade_permitted is True
        assert snapshot.recovery_state is RecoveryState.ADMITTED
        assert snapshot.reconciliation_state is ReconciliationState.CLEAN
        assert snapshot.runtime_state is RuntimeState.RUNNING
        assert snapshot.candidate_id == "candidate-recovered"
    finally:
        reopened.close()
        reopened_store.close()
