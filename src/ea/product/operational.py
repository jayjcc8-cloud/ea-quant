"""Read-only operational observations around the existing product authorities."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

from ea.core import MarketDataEnvelope, causal_market_digest
from ea.observability import JsonlFileSink, OperationalContext, OperationalLogger


def product_logger(
    attempt: Path,
    scenario: Any,
    run_id: Any,
    *,
    operation: str,
    enabled: bool = True,
    sink: Callable[[str], None] | None = None,
) -> OperationalLogger | None:
    """Construct observational context without changing attempt admission or economics."""
    if not enabled:
        return None
    try:
        context = OperationalContext(
            run_id=run_id.value, strategy_id=scenario.strategy_id.value, operation=operation
        )
        selected_sink = JsonlFileSink(attempt / "operational.jsonl") if sink is None else sink
        return OperationalLogger(context, selected_sink)
    except Exception:
        return None


def _best_effort[**P](method: Callable[P, None]) -> Callable[P, None]:
    @wraps(method)
    def observe(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            method(*args, **kwargs)
        except Exception:
            # Reading/formatting supplementary observations must not affect economics.
            return

    return observe


class ProductObservation:
    """Emit supplemental evidence; never participate in execution decisions."""

    def __init__(self, logger: OperationalLogger | None) -> None:
        self.logger = logger
        self._outcomes = 0
        self._fills = 0
        self._orders: dict[Any, Any] = {}
        self._observed_fills: dict[Any, Any] = {}

    @_best_effort
    def emit(self, event: str, **fields: Any) -> None:
        if self.logger is not None:
            self.logger.emit(event, **fields)

    @_best_effort
    def window(self, runtime: Any, lifecycle: Any, economic_gate: Any) -> None:
        if self.logger is None:
            return
        lease = runtime.active_lease
        if lease is not None and type(lease.root) is MarketDataEnvelope:
            self.emit(
                "market.event",
                market_event_id=causal_market_digest(lease.root),
                dispatch_sequence=lease.dispatch_sequence,
                symbol=lease.root.payload.instrument.symbol,
            )
        history = lifecycle.fact_authority
        for outcome in history.outcomes[self._outcomes :]:
            order = self._orders.get(outcome.resolved_order_id)
            self.emit(
                "execution.fact",
                correlation_id=None if order is None else order.correlation_id,
                order_id=outcome.resolved_order_id,
                client_order_id=outcome.client_submission_key,
                fill_id=outcome.fill_id,
                outcome=outcome.outcome_code.value,
                dispatch_sequence=outcome.runtime_dispatch_sequence,
                fact_id=outcome.fact_sha256.value,
            )
        self._outcomes = len(history.outcomes)
        snapshot = economic_gate.ledger.snapshot
        for fill in history.fills[self._fills :]:
            self._observed_fills[fill.fill_id] = fill
            fields = {
                "correlation_id": fill.correlation_id,
                "order_id": fill.order_id,
                "client_order_id": fill.client_submission_key,
                "fill_id": fill.fill_id,
            }
            self.emit(
                "execution.fill",
                **fields,
                quantity=fill.quantity.text,
                price=fill.price.text,
                side=fill.side.value,
            )
            position = next(
                (
                    b.quantity.text
                    for b in snapshot.position_balances
                    if b.instrument == fill.instrument
                ),
                "0",
            )
            cash = snapshot.cash_balances[0] if snapshot.cash_balances else None
            self.emit(
                "portfolio.updated",
                **fields,
                ledger_sequence=snapshot.ledger_sequence,
                position_quantity=position,
                cash_amount=None if cash is None else cash.amount.text,
                currency=None if cash is None else cash.currency.code,
            )
        self._fills = len(history.fills)

    @_best_effort
    def strategy(self, signal: Any) -> None:
        self.emit(
            "strategy.decision",
            correlation_id=signal.signal_id,
            market_event_id=signal.causal_market_sha256,
            outcome=signal.direction.value,
            dispatch_sequence=signal.dispatch_sequence,
        )

    @_best_effort
    def hold(self, market_root: Any, dispatch_sequence: int) -> None:
        self.emit(
            "strategy.decision",
            market_event_id=causal_market_digest(market_root),
            outcome="hold",
            dispatch_sequence=dispatch_sequence,
        )

    @_best_effort
    def risk(self, signal: Any, result: Any) -> None:
        self.emit(
            "risk.decision",
            correlation_id=result.decision.correlation_id,
            market_event_id=signal.causal_market_sha256,
            outcome=result.decision.kind.value,
            reason_code=result.evidence.reason_code.value,
        )

    @_best_effort
    def order(self, order: Any, signal: Any) -> None:
        self._orders[order.order_id] = order
        self.emit(
            "order.created",
            correlation_id=order.correlation_id,
            market_event_id=signal.causal_market_sha256,
            order_id=order.order_id,
            client_order_id=order.client_submission_key,
            quantity=order.quantity.text,
            side=order.side.value,
        )

    @_best_effort
    def submitted(self, order: Any, receipt: Any) -> None:
        self.emit(
            "broker.submitted",
            correlation_id=order.correlation_id,
            market_event_id=receipt.causal_market_sha256,
            order_id=receipt.order_id,
            client_order_id=receipt.client_submission_key,
            outcome=receipt.outcome_code.value,
            broker="historical_simulator",
            dispatch_sequence=receipt.dispatch_sequence,
        )

    @_best_effort
    def reconciliation(self, outcome: Any, *, position: bool) -> None:
        for fill in list(self._observed_fills.values()) or [None]:
            self.emit(
                "reconciliation.result",
                outcome=outcome.outcome_code.value,
                scope="position" if position else "cash",
                correlation_id=None if fill is None else fill.correlation_id,
                order_id=None if fill is None else fill.order_id,
                client_order_id=None if fill is None else fill.client_submission_key,
                fill_id=None if fill is None else fill.fill_id,
            )
