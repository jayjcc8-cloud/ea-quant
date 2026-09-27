"""Broker order observation reconciliation tests (PPV-13 core)."""

from __future__ import annotations

from ea.core.execution_state import OrderProjectionState
from ea.core.run import Sha256Digest
from ea.reconciliation.broker import (
    BrokerObservedOrderState,
    BrokerOrderObservation,
    BrokerOrderReconciliation,
    BrokerReconciliationResult,
    reconcile_broker_order,
)

CLIENT_KEY = Sha256Digest("1" * 64)


def _observation(state: BrokerObservedOrderState) -> BrokerOrderObservation:
    return BrokerOrderObservation(CLIENT_KEY, state, None)


def _reconcile(
    local: OrderProjectionState | None, observed: BrokerObservedOrderState
) -> BrokerOrderReconciliation:
    return reconcile_broker_order(local_state=local, observation=_observation(observed))


def test_exact_match_open() -> None:
    result = _reconcile(OrderProjectionState.SUBMITTED, BrokerObservedOrderState.SUBMITTED)
    assert result.result is BrokerReconciliationResult.MATCH
    assert result.halt_required is False


def test_exact_match_filled() -> None:
    result = _reconcile(OrderProjectionState.FILLED, BrokerObservedOrderState.FILLED)
    assert result.result is BrokerReconciliationResult.MATCH


def test_exact_match_cancelled() -> None:
    result = _reconcile(OrderProjectionState.CANCELLED, BrokerObservedOrderState.CANCELLED)
    assert result.result is BrokerReconciliationResult.MATCH


def test_both_unknown_is_match() -> None:
    result = _reconcile(None, BrokerObservedOrderState.UNKNOWN)
    assert result.result is BrokerReconciliationResult.MATCH


def test_missing_local_fill_is_local_behind() -> None:
    result = _reconcile(OrderProjectionState.ACKNOWLEDGED, BrokerObservedOrderState.FILLED)
    assert result.result is BrokerReconciliationResult.LOCAL_BEHIND
    assert result.halt_required is True


def test_unknown_submitted_order_is_unknown() -> None:
    result = _reconcile(OrderProjectionState.ACKNOWLEDGED, BrokerObservedOrderState.UNKNOWN)
    assert result.result is BrokerReconciliationResult.UNKNOWN
    assert result.halt_required is True


def test_broker_order_without_local_knowledge_is_local_behind() -> None:
    result = _reconcile(None, BrokerObservedOrderState.SUBMITTED)
    assert result.result is BrokerReconciliationResult.LOCAL_BEHIND


def test_local_not_sent_broker_submitted_is_conflict() -> None:
    result = _reconcile(
        OrderProjectionState.DEFINITELY_NOT_SUBMITTED, BrokerObservedOrderState.SUBMITTED
    )
    assert result.result is BrokerReconciliationResult.CONFLICT


def test_filled_versus_cancelled_is_conflict() -> None:
    result = _reconcile(OrderProjectionState.FILLED, BrokerObservedOrderState.CANCELLED)
    assert result.result is BrokerReconciliationResult.CONFLICT


def test_local_filled_broker_open_is_broker_behind() -> None:
    result = _reconcile(OrderProjectionState.FILLED, BrokerObservedOrderState.SUBMITTED)
    assert result.result is BrokerReconciliationResult.BROKER_BEHIND
