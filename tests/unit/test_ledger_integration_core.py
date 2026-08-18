from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest

import ea.core as core
from ea.core import (
    EMPTY_CHAIN_HEAD_SHA256,
    EMPTY_RECORD_SHA256,
    AuditContractError,
    AuditRecordKind,
    AuditSubjectKind,
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
    RunBinding,
    RunId,
    RunReference,
    Sha256Digest,
    SourceNamespace,
    audit_subject_digest,
    audited_ledger_handoff_digest,
    canonical_audited_ledger_handoff_bytes,
    canonical_ledger_application_command_bytes,
    canonical_ledger_apply_outcome_bytes,
    canonical_ledger_handoff_outcome_bytes,
    canonical_portfolio_risk_refresh_bytes,
    canonical_portfolio_snapshot_bytes,
    create_audit_append_acknowledgement,
    create_audit_record,
    create_audited_ledger_handoff,
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
from ea.core.audit import require_canonical_audit_payload
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


def _risk_refresh(
    *,
    halted: bool = False,
    permitted: bool = True,
    dispatch: int = 1,
    refresh: int = 1,
    halt_dispatch_sequence: int | None = None,
) -> PortfolioRiskRefresh:
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
        halt_dispatch_sequence=(
            halt_dispatch_sequence
            if halt_dispatch_sequence is not None
            else (1 if halted else None)
        ),
        conflict_existing_intent_sha256=None,
        conflict_submitted_intent_sha256=None,
    )
    return _create_portfolio_risk_refresh(
        portfolio_snapshot=snapshot,
        risk_state=risk_state,
        dispatch_sequence=dispatch,
        refresh_sequence=refresh,
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


def test_new_audit_kinds_delegate_strict_semantic_codecs_and_subject_domains() -> None:
    outcome = _not_applicable()
    outcome_payload = canonical_ledger_handoff_outcome_bytes(outcome)
    refresh_payload = canonical_portfolio_risk_refresh_bytes(_risk_refresh())

    assert (
        require_canonical_audit_payload(
            AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
            outcome_payload,
        )
        is outcome_payload
    )
    assert (
        require_canonical_audit_payload(
            AuditRecordKind.RISK_PORTFOLIO_REFRESH,
            refresh_payload,
        )
        is refresh_payload
    )
    assert audit_subject_digest(
        AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
        outcome_payload,
    ) != ledger_handoff_outcome_digest(outcome)
    with pytest.raises(AuditContractError):
        require_canonical_audit_payload(
            AuditRecordKind.RISK_PORTFOLIO_REFRESH,
            outcome_payload,
        )


def test_audited_ledger_handoff_requires_exact_outcome_acknowledgement() -> None:
    outcome = _not_applicable()
    payload = canonical_ledger_handoff_outcome_bytes(outcome)
    binding = RunBinding(RunReference(RUN_ID, DIGESTS[8]), DIGESTS[9])
    record = create_audit_record(
        binding=binding,
        owner_sequence=2,
        record_kind=AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
        subject_kind=AuditSubjectKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
        subject_sha256=audit_subject_digest(
            AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
            payload,
        ),
        canonical_payload=payload,
        previous_record_sha256=EMPTY_RECORD_SHA256,
        previous_chain_head_sha256=EMPTY_CHAIN_HEAD_SHA256,
    )
    acknowledgement = create_audit_append_acknowledgement(record)

    handoff = create_audited_ledger_handoff(outcome, acknowledgement)

    assert handoff.outcome_sha256 == ledger_handoff_outcome_digest(outcome)
    assert len(canonical_audited_ledger_handoff_bytes(handoff)) < 4_096
    assert len(audited_ledger_handoff_digest(handoff).value) == 64
    with pytest.raises(FrozenInstanceError):
        handoff.outcome_sha256 = DIGESTS[10]  # type: ignore[misc]

    refresh_payload = canonical_portfolio_risk_refresh_bytes(_risk_refresh())
    wrong_record = create_audit_record(
        binding=binding,
        owner_sequence=2,
        record_kind=AuditRecordKind.RISK_PORTFOLIO_REFRESH,
        subject_kind=AuditSubjectKind.PORTFOLIO_RISK_REFRESH,
        subject_sha256=audit_subject_digest(
            AuditRecordKind.RISK_PORTFOLIO_REFRESH,
            refresh_payload,
        ),
        canonical_payload=refresh_payload,
        previous_record_sha256=EMPTY_RECORD_SHA256,
        previous_chain_head_sha256=EMPTY_CHAIN_HEAD_SHA256,
    )
    with pytest.raises(AuditContractError):
        create_audited_ledger_handoff(
            outcome,
            create_audit_append_acknowledgement(wrong_record),
        )


# --- SHA-bound review fixes for 68f59627 (Issue #70) -----------------------


def test_portfolio_risk_exposure_digest_matches_hand_computed_golden() -> None:
    # VERIFY-003: pin the exposure digest formula with an independent
    # hashlib computation: domain + u64be(payload length) + payload.
    spec_set = _spec_set()
    snapshot = create_portfolio_ledger(RUN_ID, spec_set).snapshot
    snapshot_bytes = canonical_portfolio_snapshot_bytes(snapshot)
    refresh = _risk_refresh()

    expected = Sha256Digest(
        hashlib.sha256(
            b"ea.portfolio-risk-exposure.v1\0"
            + len(snapshot_bytes).to_bytes(8, "big")
            + snapshot_bytes
        ).hexdigest()
    )

    assert refresh.exposure_sha256 == expected
    assert expected.value == "e85214f024127d6bf0d394b1f1c13467914994a8cc0b78e5ec501cb34f0ad530"


def test_handoff_failure_schema_is_closed_and_round_trips() -> None:
    # VERIFY-004: the ledger-handoff failure enum had no positive or negative
    # schema test in the strict decoder.
    failed = create_ledger_handoff_outcome(
        run_id=RUN_ID,
        dispatch_sequence=2,
        ingress_identity=INGRESS,
        audited_handoff_sha256=DIGESTS[0],
        processing_outcome_sha256=DIGESTS[1],
        processing_outcome_ack_sha256=DIGESTS[2],
        fill_id=None,
        fill_sha256=None,
        action=LedgerHandoffAction.FAILED,
        original_ledger_apply_outcome=None,
        original_ledger_apply_outcome_sha256=None,
        before_snapshot_version=4,
        before_snapshot_sha256=DIGESTS[3],
        after_snapshot_version=4,
        after_snapshot_sha256=DIGESTS[3],
        requires_reconciliation=False,
        halt_requested=True,
        failure=LedgerHandoffFailure.EVIDENCE_MISMATCH,
    )
    payload = canonical_ledger_handoff_outcome_bytes(failed)

    assert decode_ledger_handoff_outcome(payload) == failed
    document = json.loads(payload)
    for tampered in ("unknown_failure", "evidence mismatch", 1, True):
        with pytest.raises(LedgerIntegrationError):
            decode_ledger_handoff_outcome(_canonical({**document, "failure": tampered}))


def test_risk_refresh_rejects_dispatch_and_refresh_sequence_mismatch() -> None:
    # VERIFY-005: refresh==dispatch equality was enforced but never tested.
    with pytest.raises(LedgerIntegrationError, match="refresh and dispatch sequences"):
        _risk_refresh(dispatch=2, refresh=1)


def test_risk_refresh_rejects_refresh_before_bound_risk_halt() -> None:
    # VERIFY-005: a refresh emitted before the bound risk halt dispatch was
    # rejected by the factory but had no test.
    with pytest.raises(LedgerIntegrationError, match="precedes its bound risk halt"):
        _risk_refresh(halted=True, dispatch=1, halt_dispatch_sequence=2)
