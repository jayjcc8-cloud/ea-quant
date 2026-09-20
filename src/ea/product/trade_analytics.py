"""Read-time closed-trade statistics over verified immutable report bytes."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from hashlib import sha256
from typing import Any

from ea.product.round_trip_report import BacktestReportV2, BacktestReportV3


def _text(value: Decimal) -> str:
    if not value:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _ratio(numerator: Decimal, denominator: int | Decimal) -> str | None:
    if not denominator:
        return None
    return _text((numerator / denominator).quantize(Decimal("0.000000000000000001")))


def summarize_trades(pnls: list[str], durations: list[str]) -> dict[str, Any]:
    """Return after-fee statistics; missing denominators are explicitly null."""
    if len(pnls) != len(durations):
        raise ValueError("trade durations do not match closed trades")
    with localcontext() as context:
        context.prec = 80
        context.rounding = ROUND_HALF_EVEN
        amounts = [Decimal(value) for value in pnls]
        seconds = [Decimal(value) for value in durations]
        if any(not value.is_finite() for value in amounts + seconds) or any(
            value < 0 for value in seconds
        ):
            raise ValueError("invalid trade economics or duration")
        wins = [value for value in amounts if value > 0]
        losses = [value for value in amounts if value < 0]
        total_win = sum(wins, Decimal(0))
        total_loss = sum(losses, Decimal(0))
        return {
            "closed_trades": len(amounts),
            "wins": len(wins),
            "losses": len(losses),
            "breakeven": len(amounts) - len(wins) - len(losses),
            "win_rate": _ratio(Decimal(len(wins)), len(amounts)),
            "average_win": _ratio(total_win, len(wins)),
            "average_loss": _ratio(total_loss, len(losses)),
            "payoff_ratio": (
                _ratio(total_win * len(losses), -total_loss * len(wins))
                if wins and losses
                else None
            ),
            "average_holding_seconds": _ratio(sum(seconds, Decimal(0)), len(seconds)),
        }


def trade_analytics(payload: bytes) -> dict[str, Any]:
    """Verify a supported report and derive statistics without running strategy code."""
    schema = json.loads(payload).get("schema")
    if schema not in {"ea.backtest-report.v2", "ea.backtest-report.v3"}:
        raise ValueError("complete-trade evidence is unavailable for this report")
    report = (BacktestReportV3 if schema.endswith("v3") else BacktestReportV2)(payload, b"\n")
    document = report.document
    economic = document["economics"]
    fills = [leg["fill"] for leg in economic["execution_legs"] if leg["fill"] is not None]
    pnls = (
        [trade["realized_pnl"] for trade in economic["trades"]]
        if schema.endswith("v3")
        else [economic["realized_pnl"]["amount"]]
        if len(fills) == 2
        else []
    )
    if len(pnls) != len(fills) // 2:
        raise ValueError("closed-trade evidence conflicts")
    trades = []
    durations = []
    for index, pnl in enumerate(pnls):
        entry, exit_fill = fills[2 * index : 2 * index + 2]
        start = datetime.fromisoformat(entry["occurred_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(exit_fill["occurred_at"].replace("Z", "+00:00"))
        if start.utcoffset() is None or end.utcoffset() is None or end < start:
            raise ValueError("Fill timestamps do not define a holding duration")
        delta = end - start
        microseconds = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
        duration = _text(Decimal(microseconds) / 1000000)
        durations.append(duration)
        trades.append(
            {
                "entry_fill_id": entry["fill_id"],
                "exit_fill_id": exit_fill["fill_id"],
                "realized_pnl": pnl,
                "holding_seconds": duration,
            }
        )
    return {
        "schema": "ea.trade-analytics.v1",
        "run_id": document["run_id"],
        "report_sha256": sha256(payload).hexdigest(),
        "currency": economic["currency"],
        "has_open_position": bool(len(fills) % 2),
        "trades": trades,
        **summarize_trades(pnls, durations),
    }
