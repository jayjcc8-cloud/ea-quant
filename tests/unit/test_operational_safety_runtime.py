"""Operational safety runtime integration tests (PPV-15)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import cast

from ea.core import AuditRecordKind
from ea.core.audit import AuditRecord
from ea.product.offline_demo import _DemoAudit
from ea.product.paper import PaperTradingSession
from ea.risk.operational_safety import OperationalSafetyLimits
from unit.test_paper_runtime import drive, session


def _limits(
    *,
    max_market_age_seconds: float = 5.0,
    max_daily_loss: Decimal = Decimal("1000"),
    max_total_exposure: Decimal = Decimal("1000"),
    max_open_orders: int = 2,
    max_order_rate: int = 4,
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


def _safety_records(engine: PaperTradingSession) -> list[AuditRecord]:
    return [
        record
        for record in cast(_DemoAudit, engine.audit).records
        if record.record_kind is AuditRecordKind.PAPER_OPERATIONAL_SAFETY
    ]


def test_healthy_session_authorizes_every_order(tmp_path: Path) -> None:
    engine, clock = session(tmp_path)
    drive(engine, clock)
    assert engine.status()["fills"] == 6
    assert _safety_records(engine) == []


def test_daily_loss_halt_stops_further_broker_effects(tmp_path: Path) -> None:
    engine, clock = session(tmp_path, operational_limits=_limits(max_daily_loss=Decimal("0.01")))
    drive(engine, clock)
    records = _safety_records(engine)
    assert engine.status()["fills"] < 6
    assert engine.gate.risk_authority.risk_state.halted is True
    assert len(records) >= 1
    payload = json.loads(records[0].canonical_payload)
    assert payload["verdict"] == "halt"
    assert payload["guard"] == "daily_loss"
    assert payload["identity"]


def test_exposure_deny_blocks_broker_effect_without_halting(tmp_path: Path) -> None:
    engine, clock = session(tmp_path, operational_limits=_limits(max_total_exposure=Decimal("1")))
    drive(engine, clock)
    records = _safety_records(engine)
    assert engine.status()["fills"] == 0
    assert engine.gate.risk_authority.risk_state.halted is False
    assert len(records) >= 1
    payload = json.loads(records[0].canonical_payload)
    assert payload["verdict"] == "deny"
    assert payload["guard"] == "exposure"
