"""Composition-owned acknowledged publication frontier from Accepted ADR 0022."""

from __future__ import annotations

from dataclasses import dataclass
from typing import final

from ea.core.ledger_integration import PortfolioRiskRefresh
from ea.core.outcomes import OutcomeCode
from ea.core.portfolio import PortfolioSnapshot, portfolio_snapshot_digest
from ea.core.risk import RiskStateSnapshot, risk_state_snapshot_digest

_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.CONFLICTING_ID,
    }
)


class FrontierError(ValueError):
    """Closed failure at the acknowledged publication frontier."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _ERROR_CODES:
            raise TypeError("frontier errors require an exact supported OutcomeCode")
        self.code = code
        super().__init__(message)


def _fail(code: OutcomeCode, message: str) -> FrontierError:
    return FrontierError(code, message)


@dataclass(frozen=True, slots=True)
class _FrontierState:
    published_snapshot: PortfolioSnapshot
    published_risk_state: RiskStateSnapshot
    published_refresh: PortfolioRiskRefresh | None
    internal_snapshot: PortfolioSnapshot
    internal_risk_state: RiskStateSnapshot


@final
class AcknowledgedLifecycleFrontier:
    """Separate internal and published portfolio/risk states (ADR 0022 L359-369).

    The mutable ledger may advance its internal snapshot and the risk authority
    its internal halt, but every public freshness consumer reads only the
    jointly published state. Publication advances in one pointer swap and only
    for an exact acknowledged refresh that binds the candidate values.
    """

    _state: _FrontierState

    __slots__ = ("_state",)

    def __init__(self) -> None:
        raise TypeError("acknowledged frontiers are created only by their factory")

    @property
    def published_snapshot(self) -> PortfolioSnapshot:
        return self._state.published_snapshot

    @property
    def published_risk_state(self) -> RiskStateSnapshot:
        return self._state.published_risk_state

    @property
    def published_refresh(self) -> PortfolioRiskRefresh | None:
        return self._state.published_refresh

    @property
    def internal_snapshot(self) -> PortfolioSnapshot:
        return self._state.internal_snapshot

    @property
    def internal_risk_state(self) -> RiskStateSnapshot:
        return self._state.internal_risk_state

    def current_snapshot(self) -> PortfolioSnapshot:
        """PortfolioFreshnessPort view of the published frontier."""
        return self._state.published_snapshot

    def current_state(self) -> RiskStateSnapshot:
        """RiskFreshnessPort view of the published frontier."""
        return self._state.published_risk_state

    def advance(
        self,
        *,
        snapshot: PortfolioSnapshot,
        risk_state: RiskStateSnapshot,
        refresh: PortfolioRiskRefresh,
    ) -> None:
        """Swap the joint published frontier to one exact acknowledged refresh."""
        state = self._state
        _require_candidate(snapshot, risk_state, refresh)
        expected_previous = (
            None if state.published_refresh is None else _refresh_digest(state.published_refresh)
        )
        if refresh.previous_refresh_sha256 != expected_previous:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "refresh predecessor does not continue the published chain",
            )
        if refresh.portfolio_snapshot_sha256 != portfolio_snapshot_digest(
            snapshot
        ) or refresh.risk_state_sha256 != risk_state_snapshot_digest(risk_state):
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "refresh does not bind the proposed joint publication values",
            )
        self._state = _FrontierState(
            published_snapshot=snapshot,
            published_risk_state=risk_state,
            published_refresh=refresh,
            internal_snapshot=snapshot,
            internal_risk_state=risk_state,
        )


def _require_candidate(
    snapshot: PortfolioSnapshot,
    risk_state: RiskStateSnapshot,
    refresh: PortfolioRiskRefresh,
) -> None:
    if type(snapshot) is not PortfolioSnapshot or type(risk_state) is not RiskStateSnapshot:
        raise _fail(OutcomeCode.INVALID_TYPE, "frontier candidate values must be exact")
    if type(refresh) is not PortfolioRiskRefresh:
        raise _fail(OutcomeCode.INVALID_TYPE, "frontier refresh must be exact")
    if snapshot.run_id != risk_state.run_id or refresh.run_id != snapshot.run_id:
        raise _fail(OutcomeCode.CONFLICTING_ID, "frontier candidate runs conflict")


def _refresh_digest(refresh: PortfolioRiskRefresh) -> object:
    from ea.core.ledger_integration import portfolio_risk_refresh_digest

    return portfolio_risk_refresh_digest(refresh)


def create_acknowledged_lifecycle_frontier(
    *,
    initial_snapshot: PortfolioSnapshot,
    initial_risk_state: RiskStateSnapshot,
) -> AcknowledgedLifecycleFrontier:
    """Create one frontier at the initial acknowledged portfolio/risk states."""
    if type(initial_snapshot) is not PortfolioSnapshot or (
        type(initial_risk_state) is not RiskStateSnapshot
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "initial frontier values must be exact")
    if initial_snapshot.run_id != initial_risk_state.run_id:
        raise _fail(OutcomeCode.CONFLICTING_ID, "initial frontier runs conflict")
    value = object.__new__(AcknowledgedLifecycleFrontier)
    object.__setattr__(
        value,
        "_state",
        _FrontierState(
            published_snapshot=initial_snapshot,
            published_risk_state=initial_risk_state,
            published_refresh=None,
            internal_snapshot=initial_snapshot,
            internal_risk_state=initial_risk_state,
        ),
    )
    return value
