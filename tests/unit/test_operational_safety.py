"""Operational safety authority guard tests (PPV-15 core)."""

from __future__ import annotations

from decimal import Decimal

from ea.core.run import RunId
from ea.risk.operational_safety import (
    OperationalSafetyAuthority,
    OperationalSafetyInput,
    OperationalSafetyLimits,
    OperationalSafetyVerdict,
    canonical_operational_safety_limits_bytes,
    restore_operational_safety_limits,
)

RUN_ID = RunId("12345678-1234-4234-8234-123456789abc")


def _limits(
    *,
    max_market_age_seconds: float = 5.0,
    max_daily_loss: Decimal = Decimal("100"),
    max_total_exposure: Decimal = Decimal("1000"),
    max_open_orders: int = 2,
    max_order_rate: int = 3,
    order_rate_window_seconds: float = 60.0,
    max_price_deviation_bps: int = 250,
) -> OperationalSafetyLimits:
    return OperationalSafetyLimits(
        max_market_age_seconds=max_market_age_seconds,
        max_daily_loss=max_daily_loss,
        max_total_exposure=max_total_exposure,
        max_open_orders=max_open_orders,
        max_order_rate=max_order_rate,
        order_rate_window_seconds=order_rate_window_seconds,
        max_price_deviation_bps=max_price_deviation_bps,
    )


def _input(
    *,
    kill_switch_halted: bool = False,
    reconciliation_healthy: bool = True,
    market_age_seconds: float = 0.5,
    broker_available: bool = True,
    strategy_live: bool = True,
    daily_loss: Decimal = Decimal("0"),
    current_exposure: Decimal = Decimal("0"),
    outstanding_order_exposure: Decimal = Decimal("0"),
    proposed_order_exposure: Decimal = Decimal("100"),
    open_order_count: int = 0,
    proposed_effective_price: Decimal = Decimal("101"),
    reference_price: Decimal = Decimal("100"),
    order_identity: str = "order-1",
    now_monotonic: float = 0.0,
) -> OperationalSafetyInput:
    return OperationalSafetyInput(
        kill_switch_halted=kill_switch_halted,
        reconciliation_healthy=reconciliation_healthy,
        market_age_seconds=market_age_seconds,
        broker_available=broker_available,
        strategy_live=strategy_live,
        daily_loss=daily_loss,
        current_exposure=current_exposure,
        outstanding_order_exposure=outstanding_order_exposure,
        proposed_order_exposure=proposed_order_exposure,
        open_order_count=open_order_count,
        proposed_effective_price=proposed_effective_price,
        reference_price=reference_price,
        order_identity=order_identity,
        now_monotonic=now_monotonic,
    )


def _authority(
    *,
    max_market_age_seconds: float = 5.0,
    max_daily_loss: Decimal = Decimal("100"),
    max_total_exposure: Decimal = Decimal("1000"),
    max_open_orders: int = 2,
    max_order_rate: int = 3,
    order_rate_window_seconds: float = 60.0,
    max_price_deviation_bps: int = 250,
) -> OperationalSafetyAuthority:
    return OperationalSafetyAuthority(
        run_id=RUN_ID,
        limits=_limits(
            max_market_age_seconds=max_market_age_seconds,
            max_daily_loss=max_daily_loss,
            max_total_exposure=max_total_exposure,
            max_open_orders=max_open_orders,
            max_order_rate=max_order_rate,
            order_rate_window_seconds=order_rate_window_seconds,
            max_price_deviation_bps=max_price_deviation_bps,
        ),
    )


def test_healthy_input_allows() -> None:
    decision = _authority().authorize(_input())
    assert decision.verdict is OperationalSafetyVerdict.ALLOW
    assert decision.guard == "none"


def test_kill_switch_halt() -> None:
    decision = _authority().authorize(_input(kill_switch_halted=True))
    assert decision.verdict is OperationalSafetyVerdict.HALT
    assert decision.guard == "kill_switch"
    assert decision.identity == "order-1"


def test_reconciliation_conflict_halt() -> None:
    decision = _authority().authorize(_input(reconciliation_healthy=False))
    assert decision.verdict is OperationalSafetyVerdict.HALT
    assert decision.guard == "reconciliation"


def test_market_freshness_boundary_allows_then_stale_halts() -> None:
    assert _authority().authorize(_input(market_age_seconds=5.0)).verdict is (
        OperationalSafetyVerdict.ALLOW
    )
    decision = _authority().authorize(_input(market_age_seconds=5.1))
    assert decision.verdict is OperationalSafetyVerdict.HALT
    assert decision.guard == "market_freshness"


def test_broker_health_unavailable_halt() -> None:
    decision = _authority().authorize(_input(broker_available=False))
    assert decision.verdict is OperationalSafetyVerdict.HALT
    assert decision.guard == "broker_health"


def test_strategy_heartbeat_stalled_halt() -> None:
    decision = _authority().authorize(_input(strategy_live=False))
    assert decision.verdict is OperationalSafetyVerdict.HALT
    assert decision.guard == "strategy_heartbeat"


def test_daily_loss_boundary_allows_then_exceeded_halts() -> None:
    assert (
        _authority(max_daily_loss=Decimal("100"))
        .authorize(_input(daily_loss=Decimal("100")))
        .verdict
        is OperationalSafetyVerdict.ALLOW
    )
    decision = _authority(max_daily_loss=Decimal("100")).authorize(
        _input(daily_loss=Decimal("100.01"))
    )
    assert decision.verdict is OperationalSafetyVerdict.HALT
    assert decision.guard == "daily_loss"
    assert decision.limit == "100"
    assert decision.observed == "100.01"


def test_exposure_exceeded_deny() -> None:
    decision = _authority(max_total_exposure=Decimal("1000")).authorize(
        _input(
            current_exposure=Decimal("400"),
            outstanding_order_exposure=Decimal("300"),
            proposed_order_exposure=Decimal("400"),
        )
    )
    assert decision.verdict is OperationalSafetyVerdict.DENY
    assert decision.guard == "exposure"


def test_exposure_boundary_allows() -> None:
    decision = _authority(max_total_exposure=Decimal("1000")).authorize(
        _input(
            current_exposure=Decimal("400"),
            outstanding_order_exposure=Decimal("300"),
            proposed_order_exposure=Decimal("300"),
        )
    )
    assert decision.verdict is OperationalSafetyVerdict.ALLOW


def test_open_orders_reached_deny() -> None:
    decision = _authority(max_open_orders=2).authorize(_input(open_order_count=2))
    assert decision.verdict is OperationalSafetyVerdict.DENY
    assert decision.guard == "open_orders"


def test_open_orders_boundary_allows() -> None:
    decision = _authority(max_open_orders=2).authorize(_input(open_order_count=1))
    assert decision.verdict is OperationalSafetyVerdict.ALLOW


def test_order_rate_exceeded_deny() -> None:
    authority = _authority(max_order_rate=3, order_rate_window_seconds=10.0)
    authority.record_submission(1.0)
    authority.record_submission(2.0)
    authority.record_submission(3.0)
    decision = authority.authorize(_input(now_monotonic=4.0))
    assert decision.verdict is OperationalSafetyVerdict.DENY
    assert decision.guard == "order_rate"


def test_order_rate_window_expiry_allows() -> None:
    authority = _authority(max_order_rate=3, order_rate_window_seconds=10.0)
    authority.record_submission(1.0)
    authority.record_submission(2.0)
    authority.record_submission(3.0)
    decision = authority.authorize(_input(now_monotonic=20.0))
    assert decision.verdict is OperationalSafetyVerdict.ALLOW


def test_price_deviation_exceeded_deny() -> None:
    decision = _authority(max_price_deviation_bps=250).authorize(
        _input(proposed_effective_price=Decimal("103"), reference_price=Decimal("100"))
    )
    assert decision.verdict is OperationalSafetyVerdict.DENY
    assert decision.guard == "price_deviation"


def test_price_deviation_boundary_allows() -> None:
    decision = _authority(max_price_deviation_bps=250).authorize(
        _input(proposed_effective_price=Decimal("102.5"), reference_price=Decimal("100"))
    )
    assert decision.verdict is OperationalSafetyVerdict.ALLOW


def test_seed_submissions_rebuilds_rate_window() -> None:
    authority = _authority(max_order_rate=3, order_rate_window_seconds=10.0)
    authority.seed_submissions((1.0, 2.0, 3.0), now_monotonic=4.0)
    decision = authority.authorize(_input(now_monotonic=4.0))
    assert decision.verdict is OperationalSafetyVerdict.DENY
    assert decision.guard == "order_rate"


def test_limits_round_trip() -> None:
    limits = _limits(max_daily_loss=Decimal("50"), max_open_orders=5)
    restored = restore_operational_safety_limits(canonical_operational_safety_limits_bytes(limits))
    assert restored == limits
