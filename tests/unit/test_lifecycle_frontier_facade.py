from __future__ import annotations

import pytest

from ea.composition.frontier import (
    FrontierError,
    create_acknowledged_lifecycle_frontier,
)
from ea.core import (
    PortfolioSnapshot,
    RiskHaltReason,
    RiskPolicyId,
    Sha256Digest,
    canonical_portfolio_snapshot_bytes,
)
from ea.core.ledger_integration import (
    PortfolioRiskRefresh,
    _create_portfolio_risk_refresh,
    portfolio_risk_refresh_digest,
)
from ea.core.risk import RiskStateSnapshot, _create_risk_state_snapshot
from ea.portfolio import create_portfolio_ledger
from unit.test_portfolio_ledger import RUN_ID, _fill, _spec_set

POLICY_ID = RiskPolicyId("phase1.test-risk.v1")
POLICY_SHA256 = Sha256Digest("1" * 64)


def _risk_state(*, halted: bool = False, version: int = 0) -> RiskStateSnapshot:
    return _create_risk_state_snapshot(
        run_id=RUN_ID,
        policy_id=POLICY_ID,
        policy_sha256=POLICY_SHA256,
        risk_state_version=version,
        halted=halted,
        halt_reason=RiskHaltReason.RECONCILIATION_REQUIRED if halted else None,
        halt_causal_root_available_at=None,
        halt_dispatch_sequence=None,
        conflict_existing_intent_sha256=None,
        conflict_submitted_intent_sha256=None,
    )


def _refresh(
    snapshot: PortfolioSnapshot,
    risk_state: RiskStateSnapshot,
    *,
    sequence: int,
    previous: Sha256Digest | None,
) -> PortfolioRiskRefresh:
    return _create_portfolio_risk_refresh(
        portfolio_snapshot=snapshot,
        risk_state=risk_state,
        dispatch_sequence=sequence,
        refresh_sequence=sequence,
        ordered_ledger_ack_frontier_sha256=Sha256Digest("2" * 64),
        submission_permitted=True,
        previous_refresh_sha256=previous,
    )


def test_frontier_publishes_only_exact_acknowledged_refresh() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    initial_snapshot = ledger.snapshot
    initial_risk = _risk_state()
    frontier = create_acknowledged_lifecycle_frontier(
        initial_snapshot=initial_snapshot,
        initial_risk_state=initial_risk,
    )

    assert frontier.published_snapshot == initial_snapshot
    assert frontier.published_refresh is None
    assert frontier.current_snapshot() == initial_snapshot
    assert frontier.current_state() == initial_risk

    ledger.apply_fill(_fill(spec_set, fill_sequence=1, dedup="frontier-a"))
    refresh = _refresh(ledger.snapshot, initial_risk, sequence=1, previous=None)
    frontier.advance(
        snapshot=ledger.snapshot,
        risk_state=initial_risk,
        refresh=refresh,
    )

    assert frontier.published_snapshot == ledger.snapshot
    assert frontier.published_refresh == refresh
    assert frontier.internal_snapshot == ledger.snapshot


def test_frontier_rejects_refresh_that_does_not_bind_candidate_values() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    other = create_portfolio_ledger(RUN_ID, spec_set)
    other.apply_fill(_fill(spec_set, fill_sequence=1, dedup="frontier-b"))
    frontier = create_acknowledged_lifecycle_frontier(
        initial_snapshot=ledger.snapshot,
        initial_risk_state=_risk_state(),
    )
    refresh = _refresh(ledger.snapshot, _risk_state(), sequence=1, previous=None)

    with pytest.raises(FrontierError, match="joint publication"):
        frontier.advance(
            snapshot=other.snapshot,
            risk_state=_risk_state(),
            refresh=refresh,
        )


def test_frontier_enforces_the_refresh_predecessor_chain() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    risk_state = _risk_state()
    frontier = create_acknowledged_lifecycle_frontier(
        initial_snapshot=ledger.snapshot,
        initial_risk_state=risk_state,
    )

    ledger.apply_fill(_fill(spec_set, fill_sequence=1, dedup="frontier-c"))
    first = _refresh(ledger.snapshot, risk_state, sequence=1, previous=None)
    frontier.advance(snapshot=ledger.snapshot, risk_state=risk_state, refresh=first)

    ledger.apply_fill(_fill(spec_set, fill_sequence=2, dedup="frontier-d"))
    broken = _refresh(
        ledger.snapshot,
        risk_state,
        sequence=2,
        previous=Sha256Digest("9" * 64),
    )
    with pytest.raises(FrontierError, match="predecessor"):
        frontier.advance(snapshot=ledger.snapshot, risk_state=risk_state, refresh=broken)

    linked = _refresh(
        ledger.snapshot,
        risk_state,
        sequence=2,
        previous=portfolio_risk_refresh_digest(first),
    )
    frontier.advance(snapshot=ledger.snapshot, risk_state=risk_state, refresh=linked)
    assert frontier.published_refresh == linked


def test_frontier_keeps_published_state_stable_until_advance() -> None:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    initial = ledger.snapshot
    frontier = create_acknowledged_lifecycle_frontier(
        initial_snapshot=initial,
        initial_risk_state=_risk_state(),
    )
    ledger.apply_fill(_fill(spec_set, fill_sequence=1, dedup="frontier-e"))

    assert canonical_portfolio_snapshot_bytes(frontier.published_snapshot) == (
        canonical_portfolio_snapshot_bytes(initial)
    )
    assert frontier.published_snapshot != ledger.snapshot
    assert frontier.current_snapshot() != ledger.snapshot
