from __future__ import annotations

import ast
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest

import ea.core as core
from ea.core import (
    EconomicId,
    EconomicOwnerKind,
    IngressIdentity,
    LedgerApplicationCommand,
    LedgerHandoffAction,
    LedgerHandoffFailure,
    LedgerHandoffOutcome,
    LedgerIntegrationError,
    OutcomeCode,
    PortfolioRiskRefresh,
    RiskHaltReason,
    RiskPolicyId,
    RunId,
    Sha256Digest,
    SourceNamespace,
    canonical_ledger_application_command_bytes,
    canonical_ledger_apply_outcome_bytes,
    canonical_ledger_handoff_outcome_bytes,
    canonical_portfolio_risk_refresh_bytes,
    create_ledger_handoff_outcome,
    decode_ledger_handoff_outcome,
    decode_portfolio_risk_refresh,
    fill_digest,
    ledger_application_command_digest,
    ledger_apply_outcome_digest,
    ledger_handoff_outcome_digest,
    portfolio_risk_refresh_digest,
    portfolio_snapshot_digest,
)
from ea.core.ledger_integration import (
    _create_ledger_application_command,
    _create_portfolio_risk_refresh,
    _decode_ledger_application_command,
)
from ea.core.risk import _create_risk_state_snapshot
from ea.portfolio import create_portfolio_ledger
from unit.test_portfolio_ledger import RUN_ID, _fill, _spec_set

INGRESS = IngressIdentity(SourceNamespace("sim.execution"), 3)
DIGESTS = tuple(Sha256Digest(f"{value:064x}") for value in range(1, 12))
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _canonical(document: object) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _applied_ledger_evidence() -> tuple[
    EconomicId,
    Sha256Digest,
    bytes,
    Sha256Digest,
    Sha256Digest,
    Sha256Digest,
]:
    spec_set = _spec_set()
    fill = _fill(spec_set, fill_sequence=10, dedup="ledger-integration")
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    before_sha256 = portfolio_snapshot_digest(ledger.snapshot)
    applied = ledger.apply_fill(fill)
    return (
        fill.fill_id,
        fill_digest(fill),
        canonical_ledger_apply_outcome_bytes(applied),
        ledger_apply_outcome_digest(applied),
        before_sha256,
        applied.snapshot_sha256,
    )


def _not_applicable() -> LedgerHandoffOutcome:
    return create_ledger_handoff_outcome(
        run_id=RUN_ID,
        dispatch_sequence=2,
        ingress_identity=INGRESS,
        audited_handoff_sha256=DIGESTS[0],
        processing_outcome_sha256=DIGESTS[1],
        processing_outcome_ack_sha256=DIGESTS[2],
        fill_id=None,
        fill_sha256=None,
        action=LedgerHandoffAction.NOT_APPLICABLE,
        original_ledger_apply_outcome=None,
        original_ledger_apply_outcome_sha256=None,
        before_snapshot_version=4,
        before_snapshot_sha256=DIGESTS[3],
        after_snapshot_version=4,
        after_snapshot_sha256=DIGESTS[3],
        requires_reconciliation=False,
        halt_requested=False,
        failure=None,
    )


def test_application_command_is_private_factory_issued_and_deterministic() -> None:
    fill_id, fill_sha256, _, _, _, _ = _applied_ledger_evidence()
    command = _create_ledger_application_command(
        run_id=RUN_ID,
        dispatch_sequence=2,
        audited_handoff_sha256=DIGESTS[0],
        processing_outcome_sha256=DIGESTS[1],
        fill_id=fill_id,
        fill_sha256=fill_sha256,
        requires_reconciliation=True,
    )
    encoded = canonical_ledger_application_command_bytes(command)

    assert _decode_ledger_application_command(encoded) == command
    assert ledger_application_command_digest(command).value == (
        "56af0490bc51ec2aff9759e71675009a5bcfeb34a9e69d27ae33cbeca484f3cb"
    )
    assert "_create_ledger_application_command" not in core.__all__
    assert "_decode_ledger_application_command" not in core.__all__
    with pytest.raises(TypeError, match="integration factory"):
        LedgerApplicationCommand()


def test_handoff_action_matrix_round_trips_exact_original_ledger_result() -> None:
    not_applicable = _not_applicable()
    (
        fill_id,
        fill_sha256,
        apply_bytes,
        apply_sha256,
        before_snapshot_sha256,
        after_snapshot_sha256,
    ) = _applied_ledger_evidence()
    committed = create_ledger_handoff_outcome(
        run_id=RUN_ID,
        dispatch_sequence=2,
        ingress_identity=INGRESS,
        audited_handoff_sha256=DIGESTS[0],
        processing_outcome_sha256=DIGESTS[1],
        processing_outcome_ack_sha256=DIGESTS[2],
        fill_id=fill_id,
        fill_sha256=fill_sha256,
        action=LedgerHandoffAction.EFFECT_COMMITTED,
        original_ledger_apply_outcome=apply_bytes,
        original_ledger_apply_outcome_sha256=apply_sha256,
        before_snapshot_version=0,
        before_snapshot_sha256=before_snapshot_sha256,
        after_snapshot_version=1,
        after_snapshot_sha256=after_snapshot_sha256,
        requires_reconciliation=False,
        halt_requested=False,
        failure=None,
    )
    failed = create_ledger_handoff_outcome(
        run_id=RUN_ID,
        dispatch_sequence=2,
        ingress_identity=INGRESS,
        audited_handoff_sha256=DIGESTS[0],
        processing_outcome_sha256=DIGESTS[1],
        processing_outcome_ack_sha256=DIGESTS[2],
        fill_id=fill_id,
        fill_sha256=fill_sha256,
        action=LedgerHandoffAction.FAILED,
        original_ledger_apply_outcome=None,
        original_ledger_apply_outcome_sha256=None,
        before_snapshot_version=4,
        before_snapshot_sha256=DIGESTS[3],
        after_snapshot_version=4,
        after_snapshot_sha256=DIGESTS[3],
        requires_reconciliation=True,
        halt_requested=True,
        failure=LedgerHandoffFailure.ARITHMETIC_FAILURE,
    )

    for outcome in (not_applicable, committed, failed):
        encoded = canonical_ledger_handoff_outcome_bytes(outcome)
        assert decode_ledger_handoff_outcome(encoded) == outcome
        assert len(ledger_handoff_outcome_digest(outcome).value) == 64
    assert committed.original_ledger_apply_outcome == apply_bytes
    with pytest.raises(FrozenInstanceError):
        committed.action = LedgerHandoffAction.FAILED  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    [
        {"fill_id": EconomicId(RUN_ID, EconomicOwnerKind.EXECUTION_ORDER, 10)},
        {"fill_sha256": None},
        {"after_snapshot_version": 2},
        {"original_ledger_apply_outcome_sha256": Sha256Digest("ff" * 32)},
        {"failure": LedgerHandoffFailure.LEDGER_CONFLICT},
        {"requires_reconciliation": True, "halt_requested": False},
    ],
)
def test_committed_handoff_rejects_conflicting_evidence(changes: dict[str, object]) -> None:
    (
        fill_id,
        fill_sha256,
        apply_bytes,
        apply_sha256,
        before_snapshot_sha256,
        after_snapshot_sha256,
    ) = _applied_ledger_evidence()
    arguments: dict[str, object] = {
        "run_id": RUN_ID,
        "dispatch_sequence": 2,
        "ingress_identity": INGRESS,
        "audited_handoff_sha256": DIGESTS[0],
        "processing_outcome_sha256": DIGESTS[1],
        "processing_outcome_ack_sha256": DIGESTS[2],
        "fill_id": fill_id,
        "fill_sha256": fill_sha256,
        "action": LedgerHandoffAction.EFFECT_COMMITTED,
        "original_ledger_apply_outcome": apply_bytes,
        "original_ledger_apply_outcome_sha256": apply_sha256,
        "before_snapshot_version": 0,
        "before_snapshot_sha256": before_snapshot_sha256,
        "after_snapshot_version": 1,
        "after_snapshot_sha256": after_snapshot_sha256,
        "requires_reconciliation": False,
        "halt_requested": False,
        "failure": None,
    }
    arguments.update(changes)

    with pytest.raises(LedgerIntegrationError):
        create_ledger_handoff_outcome(**arguments)  # type: ignore[arg-type]


def test_strict_decoder_rejects_noncanonical_duplicate_unknown_and_bool_int() -> None:
    document = json.loads(canonical_ledger_handoff_outcome_bytes(_not_applicable()))
    invalid_payloads = (
        json.dumps(document).encode(),
        canonical_ledger_handoff_outcome_bytes(_not_applicable()).replace(
            b'"action":', b'"action":"not_applicable","action":', 1
        ),
        _canonical({**document, "extra": 1}),
        _canonical({**document, "action": "unknown"}),
        _canonical({**document, "dispatch_sequence": True}),
        b"x" * 16_385,
    )

    for payload in invalid_payloads:
        with pytest.raises(LedgerIntegrationError):
            decode_ledger_handoff_outcome(payload)


def test_decoder_rejects_subclass_and_command_binding_conflicts() -> None:
    class BytesSubclass(bytes):
        pass

    with pytest.raises(LedgerIntegrationError) as captured:
        decode_ledger_handoff_outcome(
            BytesSubclass(canonical_ledger_handoff_outcome_bytes(_not_applicable()))
        )
    assert captured.value.code is OutcomeCode.INVALID_TYPE

    fill_id, fill_sha256, _, _, _, _ = _applied_ledger_evidence()
    command = _create_ledger_application_command(
        run_id=RUN_ID,
        dispatch_sequence=2,
        audited_handoff_sha256=DIGESTS[0],
        processing_outcome_sha256=DIGESTS[1],
        fill_id=fill_id,
        fill_sha256=fill_sha256,
        requires_reconciliation=False,
    )
    document = json.loads(canonical_ledger_application_command_bytes(command))
    with pytest.raises(LedgerIntegrationError):
        _decode_ledger_application_command(_canonical({**document, "dispatch_sequence": False}))


def test_factory_rejects_run_and_exact_type_drift() -> None:
    outcome = _not_applicable()
    with pytest.raises(LedgerIntegrationError):
        create_ledger_handoff_outcome(
            run_id=outcome.run_id,
            dispatch_sequence=outcome.dispatch_sequence,
            ingress_identity=outcome.ingress_identity,
            audited_handoff_sha256=outcome.audited_handoff_sha256,
            processing_outcome_sha256=outcome.processing_outcome_sha256,
            processing_outcome_ack_sha256=outcome.processing_outcome_ack_sha256,
            fill_id=outcome.fill_id,
            fill_sha256=outcome.fill_sha256,
            action=outcome.action,
            original_ledger_apply_outcome=outcome.original_ledger_apply_outcome,
            original_ledger_apply_outcome_sha256=(outcome.original_ledger_apply_outcome_sha256),
            before_snapshot_version=True,
            before_snapshot_sha256=outcome.before_snapshot_sha256,
            after_snapshot_version=outcome.after_snapshot_version,
            after_snapshot_sha256=outcome.after_snapshot_sha256,
            requires_reconciliation=outcome.requires_reconciliation,
            halt_requested=outcome.halt_requested,
            failure=outcome.failure,
        )
    with pytest.raises(LedgerIntegrationError):
        _create_ledger_application_command(
            run_id=RunId("87654321-4321-4321-8321-cba987654321"),
            dispatch_sequence=2,
            audited_handoff_sha256=DIGESTS[0],
            processing_outcome_sha256=DIGESTS[1],
            fill_id=EconomicId(RUN_ID, EconomicOwnerKind.EXECUTION_FILL, 1),
            fill_sha256=DIGESTS[2],
            requires_reconciliation=False,
        )


def test_ledger_integration_module_is_dependency_neutral_and_capability_confined() -> None:
    path = PROJECT_ROOT / "src" / "ea" / "core" / "ledger_integration.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert all(
        name in {"__future__", "dataclasses", "enum", "hashlib", "typing"}
        or name.startswith("ea.core.")
        for name in imports
    )
    assert (
        not {
            "ea.runtime",
            "ea.portfolio",
            "ea.risk",
            "ea.composition",
            "ea.experiments",
        }
        & imports
    )
    assert {
        "LedgerApplicationCommand",
        "LedgerHandoffOutcome",
        "PortfolioRiskRefresh",
        "create_ledger_handoff_outcome",
        "decode_ledger_handoff_outcome",
    } <= set(core.__all__)
    assert not {
        "_create_ledger_application_command",
        "_decode_ledger_application_command",
        "_create_portfolio_risk_refresh",
    } & set(core.__all__)


def _risk_refresh(*, halted: bool = False, permitted: bool = True) -> PortfolioRiskRefresh:
    spec_set = _spec_set()
    snapshot = create_portfolio_ledger(RUN_ID, spec_set).snapshot
    risk_state = _create_risk_state_snapshot(
        run_id=RUN_ID,
        policy_id=RiskPolicyId("phase1.test-risk.v1"),
        policy_sha256=DIGESTS[6],
        risk_state_version=1 if halted else 0,
        halted=halted,
        halt_reason=RiskHaltReason.RECONCILIATION_REQUIRED if halted else None,
        halt_causal_root_available_at=(datetime(2026, 1, 2, 9, 31, tzinfo=UTC) if halted else None),
        halt_dispatch_sequence=1 if halted else None,
        conflict_existing_intent_sha256=None,
        conflict_submitted_intent_sha256=None,
    )
    return _create_portfolio_risk_refresh(
        portfolio_snapshot=snapshot,
        risk_state=risk_state,
        dispatch_sequence=1,
        refresh_sequence=1,
        ordered_ledger_ack_frontier_sha256=DIGESTS[7],
        submission_permitted=permitted,
        previous_refresh_sha256=None,
    )


def test_portfolio_risk_refresh_derives_and_round_trips_candidate_frontier() -> None:
    refresh = _risk_refresh()
    encoded = canonical_portfolio_risk_refresh_bytes(refresh)

    assert decode_portfolio_risk_refresh(encoded) == refresh
    assert len(portfolio_risk_refresh_digest(refresh).value) == 64
    assert refresh.portfolio_snapshot_version == 0
    assert refresh.risk_state_version == 0
    assert refresh.submission_permitted is True
    assert "_create_portfolio_risk_refresh" not in core.__all__
    with pytest.raises(TypeError, match="lifecycle factory"):
        PortfolioRiskRefresh()


def test_portfolio_risk_refresh_rejects_halted_permission_and_decoder_drift() -> None:
    with pytest.raises(LedgerIntegrationError, match="cannot permit"):
        _risk_refresh(halted=True, permitted=True)

    document = json.loads(canonical_portfolio_risk_refresh_bytes(_risk_refresh()))
    for payload in (
        _canonical({**document, "submission_permitted": 1}),
        _canonical({**document, "refresh_sequence": 0}),
        _canonical({**document, "previous_refresh_sha256": "11" * 32}),
        _canonical({**document, "extra": None}),
    ):
        with pytest.raises(LedgerIntegrationError):
            decode_portfolio_risk_refresh(payload)
