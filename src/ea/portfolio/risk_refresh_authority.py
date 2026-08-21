"""Sealed portfolio-risk refresh authority from Accepted ADR 0022."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import final

from ea.core.execution import InstrumentExecutionSpecSet, instrument_spec_set_digest
from ea.core.ledger_integration import (
    PortfolioRiskRefresh,
    _create_portfolio_risk_refresh,
    portfolio_risk_refresh_digest,
)
from ea.core.outcomes import OutcomeCode
from ea.core.portfolio import PortfolioSnapshot, portfolio_snapshot_digest
from ea.core.risk import (
    RiskPolicyId,
    RiskStateSnapshot,
    risk_state_snapshot_digest,
)
from ea.core.run import RunId, Sha256Digest

_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.CONFLICTING_ID,
    }
)

_REFRESH_KEY = tuple[RunId, int, Sha256Digest]


class PortfolioRiskRefreshAuthorityError(ValueError):
    """Closed failure at the risk-refresh publication boundary."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _ERROR_CODES:
            raise TypeError("risk refresh errors require an exact supported OutcomeCode")
        self.code = code
        super().__init__(message)


def _fail(code: OutcomeCode, message: str) -> PortfolioRiskRefreshAuthorityError:
    return PortfolioRiskRefreshAuthorityError(code, message)


@dataclass(frozen=True, slots=True)
class _AuthorityState:
    run_id: RunId
    spec_set: InstrumentExecutionSpecSet
    policy_id: RiskPolicyId
    policy_sha256: Sha256Digest
    next_sequence: int
    previous_refresh_sha256: Sha256Digest | None
    replay_index: MappingProxyType[_REFRESH_KEY, PortfolioRiskRefresh]


@final
class Phase1PortfolioRiskRefreshAuthority:
    """The sole issuer of immutable portfolio/risk refresh publication evidence."""

    _state: _AuthorityState

    __slots__ = ("_state",)

    def __init__(self) -> None:
        raise TypeError("risk refresh authorities are created only by their factory")

    @property
    def run_id(self) -> RunId:
        return self._state.run_id

    @property
    def next_sequence(self) -> int:
        return self._state.next_sequence

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet:
        return self._state.spec_set

    @property
    def policy_id(self) -> RiskPolicyId:
        return self._state.policy_id

    @property
    def policy_sha256(self) -> Sha256Digest:
        return self._state.policy_sha256

    def resolve_refresh(
        self,
        *,
        dispatch_sequence: int,
        ordered_ledger_ack_frontier_sha256: Sha256Digest,
    ) -> PortfolioRiskRefresh | None:
        if type(dispatch_sequence) is not int or dispatch_sequence < 1:
            raise _fail(OutcomeCode.INVALID_TYPE, "refresh dispatch sequence must be positive")
        if type(ordered_ledger_ack_frontier_sha256) is not Sha256Digest:
            raise _fail(OutcomeCode.INVALID_TYPE, "refresh frontier digest must be exact")
        return self._state.replay_index.get(
            (self._state.run_id, dispatch_sequence, ordered_ledger_ack_frontier_sha256)
        )

    def create_refresh(
        self,
        *,
        snapshot: PortfolioSnapshot,
        risk_state: RiskStateSnapshot,
        dispatch_sequence: int,
        ordered_ledger_ack_frontier_sha256: Sha256Digest,
        coordinator_running: bool,
        publication_window_clear: bool,
        candidate_matches_internal: bool,
    ) -> PortfolioRiskRefresh:
        """Issue one immutable refresh per completed dispatch frontier."""
        state = self._state
        if type(snapshot) is not PortfolioSnapshot or snapshot.run_id != state.run_id:
            raise _fail(OutcomeCode.CONFLICTING_ID, "refresh snapshot binding conflicts")
        if type(risk_state) is not RiskStateSnapshot or risk_state.run_id != state.run_id:
            raise _fail(OutcomeCode.CONFLICTING_ID, "refresh risk binding conflicts")
        if (
            risk_state.policy_id != state.policy_id
            or risk_state.policy_sha256 != state.policy_sha256
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "refresh policy binding conflicts")
        if (
            snapshot.instrument_spec_set_id != state.spec_set.identifier
            or snapshot.instrument_spec_set_sha256 != instrument_spec_set_digest(state.spec_set)
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "refresh specification binding conflicts")
        if type(ordered_ledger_ack_frontier_sha256) is not Sha256Digest:
            raise _fail(OutcomeCode.INVALID_TYPE, "refresh frontier digest must be exact")
        if type(dispatch_sequence) is not int or dispatch_sequence < 1:
            raise _fail(OutcomeCode.INVALID_TYPE, "refresh dispatch sequence must be positive")
        key = (state.run_id, dispatch_sequence, ordered_ledger_ack_frontier_sha256)
        existing: PortfolioRiskRefresh | None = state.replay_index.get(key)
        if existing is not None:
            if (
                portfolio_snapshot_digest(snapshot) == existing.portfolio_snapshot_sha256
                and risk_state_snapshot_digest(risk_state) == existing.risk_state_sha256
            ):
                return existing
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "refresh replay conflicts with the retained frontier",
            )
        facts = (
            coordinator_running,
            publication_window_clear,
            candidate_matches_internal,
        )
        if any(type(value) is not bool for value in facts):
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "refresh derivation facts must be exact booleans",
            )
        submission_permitted = (
            not risk_state.halted
            and not snapshot.open_reconciliation_refs
            and snapshot.open_reconciliation_bindings == ()
            and coordinator_running
            and publication_window_clear
            and candidate_matches_internal
        )
        # RISK-001 continuity: the Phase 1 resource model keeps dispatch
        # sequences contiguous (D = M + R + 1), so refresh_sequence ==
        # dispatch_sequence and each new frontier advances by exactly one.
        if dispatch_sequence != state.next_sequence:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "refresh dispatch sequence is outside the contiguous frontier",
            )
        refresh = _create_portfolio_risk_refresh(
            portfolio_snapshot=snapshot,
            risk_state=risk_state,
            dispatch_sequence=dispatch_sequence,
            refresh_sequence=dispatch_sequence,
            ordered_ledger_ack_frontier_sha256=ordered_ledger_ack_frontier_sha256,
            submission_permitted=submission_permitted,
            previous_refresh_sha256=state.previous_refresh_sha256,
        )
        next_index = dict(state.replay_index)
        next_index[key] = refresh
        self._state = _AuthorityState(
            run_id=state.run_id,
            spec_set=state.spec_set,
            policy_id=state.policy_id,
            policy_sha256=state.policy_sha256,
            next_sequence=state.next_sequence + 1,
            previous_refresh_sha256=portfolio_risk_refresh_digest(refresh),
            replay_index=MappingProxyType(next_index),
        )
        return refresh


def create_phase1_portfolio_risk_refresh_authority(
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    policy_id: RiskPolicyId,
    policy_sha256: Sha256Digest,
    first_sequence: int = 1,
    first_previous_refresh_sha256: Sha256Digest | None = None,
) -> Phase1PortfolioRiskRefreshAuthority:
    """Create one run/specification/policy-bound refresh authority.

    ``first_sequence`` binds the contiguous frontier start and
    ``first_previous_refresh_sha256`` the retained predecessor; composition
    supplies both for recovered runs whose journal already advanced the
    refresh frontier.
    """
    if type(run_id) is not RunId or type(spec_set) is not InstrumentExecutionSpecSet:
        raise _fail(OutcomeCode.INVALID_TYPE, "refresh authority binding must be exact")
    if type(policy_id) is not RiskPolicyId or type(policy_sha256) is not Sha256Digest:
        raise _fail(OutcomeCode.INVALID_TYPE, "refresh policy binding must be exact")
    if type(first_sequence) is not int or not 1 <= first_sequence <= (1 << 64) - 1:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "refresh first sequence must be positive")
    if first_previous_refresh_sha256 is not None and (
        type(first_previous_refresh_sha256) is not Sha256Digest or first_sequence == 1
    ):
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "refresh predecessor requires a later first sequence",
        )
    if first_sequence > 1 and first_previous_refresh_sha256 is None:
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "recovered refresh frontier requires its predecessor",
        )
    value = object.__new__(Phase1PortfolioRiskRefreshAuthority)
    object.__setattr__(
        value,
        "_state",
        _AuthorityState(
            run_id=run_id,
            spec_set=spec_set,
            policy_id=policy_id,
            policy_sha256=policy_sha256,
            next_sequence=first_sequence,
            previous_refresh_sha256=first_previous_refresh_sha256,
            replay_index=MappingProxyType({}),
        ),
    )
    return value
