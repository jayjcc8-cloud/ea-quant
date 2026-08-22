from __future__ import annotations

from typing import Any

import pytest

from ea.core import (
    PortfolioSnapshot,
    RiskHaltReason,
    RiskPolicyId,
    RunId,
    Sha256Digest,
    canonical_portfolio_risk_refresh_bytes,
    portfolio_risk_refresh_digest,
)
from ea.core.risk import RiskStateSnapshot, _create_risk_state_snapshot
from ea.portfolio import (
    Phase1PortfolioRiskRefreshAuthority,
    create_phase1_portfolio_risk_refresh_authority,
    create_portfolio_ledger,
)
from ea.portfolio.risk_refresh_authority import PortfolioRiskRefreshAuthorityError
from unit.test_portfolio_ledger import RUN_ID, _fill, _spec_set

DIGESTS = tuple(Sha256Digest(f"{index:064x}") for index in range(1, 10))
POLICY_ID = RiskPolicyId("phase1.test-risk.v1")
POLICY_SHA256 = DIGESTS[0]


def _risk_state(
    *, halted: bool = False, run_id: RunId = RUN_ID, policy_id: RiskPolicyId = POLICY_ID
) -> RiskStateSnapshot:
    from datetime import UTC, datetime

    return _create_risk_state_snapshot(
        run_id=run_id,
        policy_id=policy_id,
        policy_sha256=POLICY_SHA256,
        risk_state_version=1 if halted else 0,
        halted=halted,
        halt_reason=RiskHaltReason.RECONCILIATION_REQUIRED if halted else None,
        halt_causal_root_available_at=(datetime(2026, 1, 2, 9, 31, tzinfo=UTC) if halted else None),
        halt_dispatch_sequence=1 if halted else None,
        conflict_existing_intent_sha256=None,
        conflict_submitted_intent_sha256=None,
    )


def _authority() -> Phase1PortfolioRiskRefreshAuthority:
    return create_phase1_portfolio_risk_refresh_authority(
        run_id=RUN_ID,
        spec_set=_spec_set(),
        policy_id=POLICY_ID,
        policy_sha256=POLICY_SHA256,
    )


def _snapshot_with_open_ref() -> PortfolioSnapshot:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    fill = _fill(spec_set, fill_sequence=1, dedup="refresh-open", resolved=False)
    from ea.core.ledger_integration import _create_ledger_application_command

    command = _create_ledger_application_command(
        run_id=RUN_ID,
        dispatch_sequence=3,
        audited_handoff_sha256=DIGESTS[2],
        processing_outcome_sha256=DIGESTS[3],
        fill_id=fill.fill_id,
        fill_sha256=__import__("ea.core", fromlist=["fill_digest"]).fill_digest(fill),
        requires_reconciliation=True,
    )
    ledger.apply_ledger_application_command(command, fill)
    return ledger.snapshot


def test_first_refresh_derives_permitted_flag_and_predecessor_chain() -> None:
    authority = _authority()
    spec_set = _spec_set()
    snapshot = create_portfolio_ledger(RUN_ID, spec_set).snapshot
    risk_state = _risk_state()

    refresh = authority.create_refresh(
        snapshot=snapshot,
        risk_state=risk_state,
        dispatch_sequence=1,
        ordered_ledger_ack_frontier_sha256=DIGESTS[1],
        coordinator_running=True,
        publication_window_clear=True,
        candidate_matches_internal=True,
    )

    assert refresh.refresh_sequence == 1
    assert refresh.dispatch_sequence == 1
    assert refresh.previous_refresh_sha256 is None
    assert refresh.submission_permitted is True
    assert authority.next_sequence == 2

    second = authority.create_refresh(
        snapshot=snapshot,
        risk_state=risk_state,
        dispatch_sequence=2,
        ordered_ledger_ack_frontier_sha256=DIGESTS[4],
        coordinator_running=True,
        publication_window_clear=True,
        candidate_matches_internal=True,
    )
    assert second.previous_refresh_sha256 == portfolio_risk_refresh_digest(refresh)


def test_halted_risk_state_derives_blocked_submission() -> None:
    authority = _authority()
    snapshot = create_portfolio_ledger(RUN_ID, _spec_set()).snapshot
    risk_state = _risk_state(halted=True)

    refresh = authority.create_refresh(
        snapshot=snapshot,
        risk_state=risk_state,
        dispatch_sequence=1,
        ordered_ledger_ack_frontier_sha256=DIGESTS[1],
        coordinator_running=True,
        publication_window_clear=True,
        candidate_matches_internal=True,
    )

    assert refresh.submission_permitted is False


def test_open_reconciliation_reference_derives_blocked_submission() -> None:
    authority = _authority()
    snapshot = _snapshot_with_open_ref()

    refresh = authority.create_refresh(
        snapshot=snapshot,
        risk_state=_risk_state(),
        dispatch_sequence=1,
        ordered_ledger_ack_frontier_sha256=DIGESTS[1],
        coordinator_running=True,
        publication_window_clear=True,
        candidate_matches_internal=True,
    )

    assert snapshot.open_reconciliation_refs != ()
    assert refresh.submission_permitted is False


def test_refresh_sequence_must_follow_contiguous_frontier() -> None:
    authority = _authority()
    snapshot = create_portfolio_ledger(RUN_ID, _spec_set()).snapshot

    with pytest.raises(PortfolioRiskRefreshAuthorityError, match="contiguous frontier"):
        authority.create_refresh(
            snapshot=snapshot,
            risk_state=_risk_state(),
            dispatch_sequence=3,
            ordered_ledger_ack_frontier_sha256=DIGESTS[1],
            coordinator_running=True,
            publication_window_clear=True,
            candidate_matches_internal=True,
        )


def test_exact_replay_returns_original_and_conflicting_bytes_are_rejected() -> None:
    authority = _authority()
    spec_set = _spec_set()
    snapshot = create_portfolio_ledger(RUN_ID, spec_set).snapshot
    risk_state = _risk_state()

    first = authority.create_refresh(
        snapshot=snapshot,
        risk_state=risk_state,
        dispatch_sequence=1,
        ordered_ledger_ack_frontier_sha256=DIGESTS[1],
        coordinator_running=True,
        publication_window_clear=True,
        candidate_matches_internal=True,
    )

    # Exact retry after a failed audit append: same key returns the original.
    replay = authority.create_refresh(
        snapshot=snapshot,
        risk_state=risk_state,
        dispatch_sequence=1,
        ordered_ledger_ack_frontier_sha256=DIGESTS[1],
        coordinator_running=False,
        publication_window_clear=False,
        candidate_matches_internal=False,
    )
    assert replay is first
    assert canonical_portfolio_risk_refresh_bytes(replay) == (
        canonical_portfolio_risk_refresh_bytes(first)
    )
    assert portfolio_risk_refresh_digest(replay) == portfolio_risk_refresh_digest(first)
    assert replay.refresh_sequence == first.refresh_sequence
    assert replay.previous_refresh_sha256 == first.previous_refresh_sha256
    assert replay.submission_permitted is True
    assert authority.next_sequence == 2

    # Same key with different evidence bytes is a conflict.
    drifted = create_portfolio_ledger(RUN_ID, spec_set)
    drifted.apply_fill(_fill(spec_set, fill_sequence=1, dedup="refresh-conflict"))
    with pytest.raises(PortfolioRiskRefreshAuthorityError, match="replay conflicts"):
        authority.create_refresh(
            snapshot=drifted.snapshot,
            risk_state=risk_state,
            dispatch_sequence=1,
            ordered_ledger_ack_frontier_sha256=DIGESTS[1],
            coordinator_running=False,
            publication_window_clear=False,
            candidate_matches_internal=False,
        )


def test_binding_conflicts_are_rejected_before_issue() -> None:
    authority = _authority()
    spec_set = _spec_set()
    snapshot = create_portfolio_ledger(RUN_ID, spec_set).snapshot

    with pytest.raises(PortfolioRiskRefreshAuthorityError, match="policy binding"):
        authority.create_refresh(
            snapshot=snapshot,
            risk_state=_risk_state(policy_id=RiskPolicyId("phase1.other-risk.v1")),
            dispatch_sequence=1,
            ordered_ledger_ack_frontier_sha256=DIGESTS[1],
            coordinator_running=True,
            publication_window_clear=True,
            candidate_matches_internal=True,
        )


@pytest.mark.parametrize(
    "false_fact",
    ("coordinator_running", "publication_window_clear", "candidate_matches_internal"),
)
def test_new_refresh_requires_every_derivation_fact(false_fact: str) -> None:
    authority = _authority()
    facts = {
        "coordinator_running": True,
        "publication_window_clear": True,
        "candidate_matches_internal": True,
    }
    facts[false_fact] = False
    refresh = authority.create_refresh(
        snapshot=create_portfolio_ledger(RUN_ID, _spec_set()).snapshot,
        risk_state=_risk_state(),
        dispatch_sequence=1,
        ordered_ledger_ack_frontier_sha256=DIGESTS[1],
        **facts,
    )
    assert refresh.submission_permitted is False


@pytest.mark.parametrize(
    "fact_name",
    ("coordinator_running", "publication_window_clear", "candidate_matches_internal"),
)
@pytest.mark.parametrize("invalid", (1, None, "true"))
def test_refresh_derivation_facts_require_exact_booleans(fact_name: str, invalid: object) -> None:
    authority = _authority()
    facts: dict[str, Any] = {
        "coordinator_running": True,
        "publication_window_clear": True,
        "candidate_matches_internal": True,
    }
    facts[fact_name] = invalid
    with pytest.raises(PortfolioRiskRefreshAuthorityError, match="exact booleans"):
        authority.create_refresh(
            snapshot=create_portfolio_ledger(RUN_ID, _spec_set()).snapshot,
            risk_state=_risk_state(),
            dispatch_sequence=1,
            ordered_ledger_ack_frontier_sha256=DIGESTS[1],
            **facts,
        )


def test_refresh_authority_module_imports_no_runtime_or_composition() -> None:
    import ast
    from pathlib import Path

    path = Path("src/ea/portfolio/risk_refresh_authority.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module)
    assert not any(
        name.startswith("ea.runtime") or name.startswith("ea.composition") for name in imports
    )
    assert not any(name.startswith("ea.experiments") for name in imports)
