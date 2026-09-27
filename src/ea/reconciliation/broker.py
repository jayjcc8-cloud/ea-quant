"""Broker order observation reconciliation for the local Paper runtime (PPV-13).

The existing ``Phase1ReconciliationAuthority`` already compares acknowledged cash
and position balances against the local ledger frontier. This module fills the
order/fill gap: it compares one external broker observation of an order against
the local fact-owned projection and returns one closed result.

The module never repairs balances, never synthesizes Fills and never deletes
Orders. A non-MATCH result is evidence that the runtime must retain and act on
(halt or reconcile), never auto-correct.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import final

from ea.core.execution_state import OrderProjectionState
from ea.core.run import Sha256Digest


class BrokerReconciliationResult(StrEnum):
    """Closed broker-vs-local comparison outcome."""

    MATCH = "match"
    LOCAL_BEHIND = "local_behind"
    BROKER_BEHIND = "broker_behind"
    OBSERVATION_STALE = "observation_stale"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class BrokerObservedOrderState(StrEnum):
    """Normalized external broker order state (closed vocabulary)."""

    UNKNOWN = "unknown"
    SUBMITTED = "submitted"
    FILLED = "filled"
    CANCELLED = "cancelled"


_OPEN_PROJECTIONS = frozenset(
    {
        OrderProjectionState.SUBMITTED,
        OrderProjectionState.ACKNOWLEDGED,
        OrderProjectionState.PARTIALLY_FILLED,
    }
)
_NON_FILL_TERMINAL = frozenset(
    {
        OrderProjectionState.CANCELLED,
        OrderProjectionState.EXPIRED,
        OrderProjectionState.REJECTED,
    }
)


@final
@dataclass(frozen=True, slots=True)
class BrokerOrderObservation:
    """One external broker observation of one client submission."""

    client_submission_key: Sha256Digest
    observed_state: BrokerObservedOrderState
    venue_order_id: str | None


@final
@dataclass(frozen=True, slots=True)
class BrokerOrderReconciliation:
    """The closed comparison result plus retained evidence identity."""

    client_submission_key: Sha256Digest
    result: BrokerReconciliationResult
    local_state: OrderProjectionState | None
    observed_state: BrokerObservedOrderState

    @property
    def halt_required(self) -> bool:
        """True when the comparison must halt rather than continue trading."""
        return self.result is not BrokerReconciliationResult.MATCH


def _local_kind(state: OrderProjectionState | None) -> str:
    if state is None:
        return "unknown"
    if state is OrderProjectionState.DEFINITELY_NOT_SUBMITTED:
        return "not_sent"
    if state is OrderProjectionState.FILLED:
        return "filled"
    if state in _NON_FILL_TERMINAL:
        return "cancelled"
    if state in _OPEN_PROJECTIONS:
        return "open"
    return "unknown"


def reconcile_broker_order(
    *,
    local_state: OrderProjectionState | None,
    observation: BrokerOrderObservation,
) -> BrokerOrderReconciliation:
    """Compare one broker observation against the local projection state.

    ``local_state`` is the fact authority's current projection state (or None
    when the order is unknown locally); this function never mutates it.
    ``OBSERVATION_STALE`` is reserved for when the observation timestamp
    predates the local projection frontier; the caller supplies a non-stale
    observation here.
    """
    if type(observation) is not BrokerOrderObservation:
        raise ValueError("broker observation must be exact")
    if local_state is not None and type(local_state) is not OrderProjectionState:
        raise ValueError("local state must be an exact OrderProjectionState or None")

    local_kind = _local_kind(local_state)
    observed = observation.observed_state

    if observed is BrokerObservedOrderState.UNKNOWN:
        if local_kind in ("unknown", "not_sent"):
            result = BrokerReconciliationResult.MATCH
        elif local_kind == "open":
            result = BrokerReconciliationResult.UNKNOWN
        else:
            result = BrokerReconciliationResult.BROKER_BEHIND
    elif observed is BrokerObservedOrderState.SUBMITTED:
        if local_kind == "open":
            result = BrokerReconciliationResult.MATCH
        elif local_kind == "unknown":
            result = BrokerReconciliationResult.LOCAL_BEHIND
        elif local_kind == "not_sent":
            result = BrokerReconciliationResult.CONFLICT
        else:
            result = BrokerReconciliationResult.BROKER_BEHIND
    elif observed is BrokerObservedOrderState.FILLED:
        if local_kind == "filled":
            result = BrokerReconciliationResult.MATCH
        elif local_kind == "cancelled":
            result = BrokerReconciliationResult.CONFLICT
        else:
            result = BrokerReconciliationResult.LOCAL_BEHIND
    elif observed is BrokerObservedOrderState.CANCELLED:
        if local_kind == "cancelled":
            result = BrokerReconciliationResult.MATCH
        elif local_kind == "filled" or local_kind == "not_sent":
            result = BrokerReconciliationResult.CONFLICT
        else:
            result = BrokerReconciliationResult.LOCAL_BEHIND
    else:
        raise ValueError("broker observed state is invalid")

    return BrokerOrderReconciliation(
        client_submission_key=observation.client_submission_key,
        result=result,
        local_state=local_state,
        observed_state=observed,
    )
