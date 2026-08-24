from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path

import pytest

from ea.core import (
    EMPTY_CHAIN_HEAD_SHA256,
    EMPTY_RECORD_SHA256,
    AuditRecordKind,
    AuditSubjectKind,
    CanonicalDecimal,
    CashBalance,
    CashReconciliationBalance,
    EconomicId,
    EconomicOwnerKind,
    ExistingLedgerBinding,
    FactProvenanceId,
    Instrument,
    InstrumentExecutionSpecSet,
    OpenReconciliationRef,
    OutcomeCode,
    PortfolioSnapshot,
    PositionBalance,
    PositionReconciliationBalance,
    PositionReconciliationDiscrepancy,
    ReconciliationAdjustmentAuthorization,
    ReconciliationAdjustmentCommand,
    ReconciliationAdjustmentTargetKind,
    ReconciliationAdjustmentVariant,
    ReconciliationAuthorizationDecision,
    ReconciliationAuthorizationPolicyId,
    ReconciliationObservation,
    ReconciliationObservationKind,
    ReconciliationOutcome,
    ReconciliationRequestedAction,
    ReconciliationScopeKind,
    ReconciliationWatermarkComparison,
    RunBinding,
    RunId,
    RunReference,
    RuntimeIdentifier,
    Sha256Digest,
    SourceNamespace,
    VenueId,
    audit_subject_digest,
    canonical_portfolio_snapshot_bytes,
    canonical_reconciliation_outcome_bytes,
    create_audit_append_acknowledgement,
    create_audit_record,
    create_reconciliation_observation,
    create_reconciliation_outcome,
    instrument_spec_set_digest,
    portfolio_snapshot_digest,
    reconciliation_adjustment_command_digest,
    reconciliation_observation_digest,
    reconciliation_outcome_digest,
)
from ea.reconciliation.authority import (
    Phase1ReconciliationAuthority,
    ReconciliationAuthorityError,
    create_phase1_reconciliation_authority,
)
from unit.test_portfolio_ledger import INSTRUMENT, RUN_ID, _spec, _spec_set
from unit.test_reconciliation_observation import TIME

PROJECT_ROOT = Path(__file__).resolve().parents[2]

OTHER_RUN_ID = RunId("87654321-4321-4321-8321-cba987654321")
BINDING = RunBinding(RunReference(RUN_ID, Sha256Digest("33" * 32)), Sha256Digest("44" * 32))
SOURCE = SourceNamespace("reconciliation.sim")
DIGEST = Sha256Digest("ab" * 32)
LEDGER_WATERMARK = SourceNamespace("ledger.portfolio")
POLICY_ID = ReconciliationAuthorizationPolicyId("policy.phase1.v1")
POLICY_SHA256 = Sha256Digest("99" * 32)


def _snapshot(
    *,
    ledger_sequence: int = 3,
    position_balances: tuple[PositionBalance, ...] | None = None,
    cash_balances: tuple[CashBalance, ...] | None = None,
    open_reconciliation_refs: tuple[OpenReconciliationRef, ...] = (),
    run_id: RunId = RUN_ID,
    spec_set: InstrumentExecutionSpecSet | None = None,
) -> PortfolioSnapshot:
    selected = _spec_set() if spec_set is None else spec_set
    positions = (
        (PositionBalance(INSTRUMENT, CanonicalDecimal("1"), CanonicalDecimal("10")),)
        if position_balances is None
        else position_balances
    )
    cash = () if cash_balances is None else cash_balances
    return PortfolioSnapshot(
        run_id=run_id,
        instrument_spec_set_id=selected.identifier,
        instrument_spec_set_sha256=instrument_spec_set_digest(selected),
        snapshot_version=ledger_sequence,
        ledger_sequence=ledger_sequence,
        last_entry_id=EconomicId(run_id, EconomicOwnerKind.LEDGER_ENTRY, ledger_sequence),
        last_transaction_sha256=Sha256Digest("88" * 32),
        cash_balances=cash,
        position_balances=positions,
        rounding_balances=(),
        unresolved_fills=(),
        open_reconciliation_bindings=tuple(
            ExistingLedgerBinding(
                EconomicId(run_id, EconomicOwnerKind.LEDGER_ENTRY, index),
                reference.fill_id,
                reference.fill_sha256,
                Sha256Digest(f"{index:064x}"),
            )
            for index, reference in enumerate(open_reconciliation_refs, start=1)
        ),
        open_reconciliation_refs=open_reconciliation_refs,
    )


def _observation(
    *,
    kind: ReconciliationObservationKind = ReconciliationObservationKind.POSITION_SNAPSHOT,
    scope: ReconciliationScopeKind = ReconciliationScopeKind.POSITION,
    balances: tuple[PositionReconciliationBalance | CashReconciliationBalance, ...] | None = None,
    watermark_sequence: int = 3,
    watermark_namespace: SourceNamespace = LEDGER_WATERMARK,
    source_sequence: int = 7,
    observation_sequence: int = 1,
    available_at: datetime = TIME,
    spec_set: InstrumentExecutionSpecSet | None = None,
) -> ReconciliationObservation:
    selected = (
        (PositionReconciliationBalance(INSTRUMENT, CanonicalDecimal("10")),)
        if balances is None
        else balances
    )
    return create_reconciliation_observation(
        run_id=RUN_ID,
        spec_set=_spec_set() if spec_set is None else spec_set,
        observation_id=EconomicId(
            RUN_ID,
            EconomicOwnerKind.RECONCILIATION_OBSERVATION,
            observation_sequence,
        ),
        kind=kind,
        source_namespace=SOURCE,
        source_sequence=source_sequence,
        occurred_at=TIME,
        available_at=available_at,
        watermark_namespace=watermark_namespace,
        watermark_sequence=watermark_sequence,
        declared_scope_kind=scope,
        declared_scope_id=RuntimeIdentifier("portfolio.default"),
        provenance_id=FactProvenanceId("reconciliation.fixture.v1"),
        provenance_payload_sha256=DIGEST,
        balances=selected,
    )


def _mismatch_observation(
    *,
    source_sequence: int = 7,
    observation_sequence: int = 1,
) -> ReconciliationObservation:
    return _observation(
        source_sequence=source_sequence,
        observation_sequence=observation_sequence,
        balances=(PositionReconciliationBalance(INSTRUMENT, CanonicalDecimal("12")),),
    )


def _authority(
    snapshot: PortfolioSnapshot,
    *,
    spec_set: InstrumentExecutionSpecSet | None = None,
) -> Phase1ReconciliationAuthority:
    selected = _spec_set() if spec_set is None else spec_set
    return create_phase1_reconciliation_authority(
        run_id=RUN_ID,
        spec_set=selected,
        snapshot_view=lambda: snapshot,
    )


def _outcome_acknowledgement(outcome: ReconciliationOutcome) -> object:
    payload = canonical_reconciliation_outcome_bytes(outcome)
    record = create_audit_record(
        binding=BINDING,
        owner_sequence=2,
        record_kind=AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME,
        subject_kind=AuditSubjectKind.RECONCILIATION_OUTCOME,
        subject_sha256=audit_subject_digest(
            AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME,
            payload,
        ),
        canonical_payload=payload,
        previous_record_sha256=EMPTY_RECORD_SHA256,
        previous_chain_head_sha256=EMPTY_CHAIN_HEAD_SHA256,
    )
    return create_audit_append_acknowledgement(record)


def _propose(
    authority: Phase1ReconciliationAuthority,
    *,
    snapshot: PortfolioSnapshot,
    observation: ReconciliationObservation,
    outcome: ReconciliationOutcome,
) -> ReconciliationAdjustmentCommand:
    return authority.propose_adjustment_command(
        binding=BINDING,
        outcome=outcome,
        outcome_acknowledgement=_outcome_acknowledgement(outcome),
        observation=observation,
        local_snapshot=snapshot,
    )


def _issue(
    authority: Phase1ReconciliationAuthority,
    *,
    outcome: ReconciliationOutcome,
    command: ReconciliationAdjustmentCommand,
    decision: ReconciliationAuthorizationDecision = ReconciliationAuthorizationDecision.ALLOWED,
) -> ReconciliationAdjustmentAuthorization:
    return authority.issue_authorization(
        binding=BINDING,
        outcome=outcome,
        outcome_acknowledgement=_outcome_acknowledgement(outcome),
        command=command,
        policy=POLICY_ID,
        policy_version=1,
        policy_sha256=POLICY_SHA256,
        decision=decision,
        available_at=TIME,
    )


def test_equal_watermark_and_balances_produce_match_without_halt() -> None:
    snapshot = _snapshot()
    authority = _authority(snapshot)
    observation = _observation()

    outcome = authority.admit_observation(observation, dispatch_sequence=7)

    assert type(outcome) is ReconciliationOutcome
    assert outcome.outcome_code is OutcomeCode.RECONCILIATION_MATCH
    assert outcome.requested_action is ReconciliationRequestedAction.NONE
    assert outcome.halt_requested is False
    assert outcome.watermark_comparison is ReconciliationWatermarkComparison.EQUAL
    assert outcome.discrepancies == ()
    assert outcome.observation_sha256 == reconciliation_observation_digest(observation)
    assert outcome.local_snapshot_version == snapshot.snapshot_version
    assert outcome.ledger_sequence == snapshot.ledger_sequence
    assert outcome.local_snapshot_sha256 == portfolio_snapshot_digest(snapshot)
    assert outcome.dispatch_sequence == 7


def test_lower_remote_watermark_is_local_ahead_stale_retain_and_halt() -> None:
    snapshot = _snapshot(ledger_sequence=3)
    authority = _authority(snapshot)
    observation = _observation(watermark_sequence=2)

    outcome = authority.admit_observation(observation, dispatch_sequence=7)

    assert outcome.outcome_code is OutcomeCode.RECONCILIATION_LOCAL_AHEAD_STALE
    assert outcome.requested_action is ReconciliationRequestedAction.RETAIN_AND_HALT
    assert outcome.watermark_comparison is ReconciliationWatermarkComparison.REMOTE_LOWER
    assert outcome.halt_requested is True
    assert outcome.discrepancies == ()


def test_higher_remote_watermark_is_remote_ahead_and_requests_facts() -> None:
    snapshot = _snapshot(ledger_sequence=3)
    authority = _authority(snapshot)
    observation = _observation(watermark_sequence=4)

    outcome = authority.admit_observation(observation, dispatch_sequence=7)

    assert outcome.outcome_code is OutcomeCode.RECONCILIATION_REMOTE_AHEAD
    assert outcome.requested_action is ReconciliationRequestedAction.REQUEST_MISSING_TRADE_FACTS
    assert outcome.watermark_comparison is ReconciliationWatermarkComparison.REMOTE_HIGHER
    assert outcome.halt_requested is True
    assert outcome.discrepancies == ()


def test_single_differing_target_proposes_one_adjustment_with_halt() -> None:
    snapshot = _snapshot()
    authority = _authority(snapshot)
    observation = _mismatch_observation()

    outcome = authority.admit_observation(observation, dispatch_sequence=7)

    assert outcome.outcome_code is OutcomeCode.RECONCILIATION_MISMATCH
    assert (
        outcome.requested_action is ReconciliationRequestedAction.PROPOSE_SINGLE_TARGET_ADJUSTMENT
    )
    assert outcome.halt_requested is True
    assert outcome.watermark_comparison is ReconciliationWatermarkComparison.EQUAL
    assert len(outcome.discrepancies) == 1
    discrepancy = outcome.discrepancies[0]
    assert type(discrepancy) is PositionReconciliationDiscrepancy
    assert discrepancy.instrument == INSTRUMENT
    assert discrepancy.local_amount == CanonicalDecimal("10")
    assert discrepancy.observed_amount == CanonicalDecimal("12")
    assert discrepancy.delta == CanonicalDecimal("2")


def test_two_differing_targets_quarantine_for_manual_decomposition() -> None:
    second = Instrument(VenueId("XNAS"), "TSLA")
    spec_set = _spec_set(
        _spec(),
        _spec(instrument=second, specification_id="xnas.tsla.v1"),
    )
    snapshot = _snapshot(spec_set=spec_set)
    authority = _authority(snapshot, spec_set=spec_set)
    observation = _observation(
        spec_set=spec_set,
        balances=(
            PositionReconciliationBalance(INSTRUMENT, CanonicalDecimal("12")),
            PositionReconciliationBalance(second, CanonicalDecimal("5")),
        ),
    )

    outcome = authority.admit_observation(observation, dispatch_sequence=7)

    assert outcome.outcome_code is OutcomeCode.RECONCILIATION_QUARANTINED
    assert outcome.requested_action is ReconciliationRequestedAction.MANUAL_EVIDENCE_DECOMPOSITION
    assert outcome.halt_requested is True
    assert outcome.watermark_comparison is ReconciliationWatermarkComparison.EQUAL
    assert len(outcome.discrepancies) == 2
    assert all(
        type(discrepancy) is PositionReconciliationDiscrepancy
        for discrepancy in outcome.discrepancies
    )


def test_observation_identity_replay_returns_same_outcome_and_conflict_on_different_bytes() -> None:
    snapshot = _snapshot()
    authority = _authority(snapshot)
    observation = _observation()

    first = authority.admit_observation(observation, dispatch_sequence=7)
    replay = authority.admit_observation(observation, dispatch_sequence=8)

    assert replay is first
    assert replay.dispatch_sequence == 7
    assert len(authority.observation_index) == 1
    assert len(authority.outcome_index) == 1

    conflicting = _observation(
        balances=(PositionReconciliationBalance(INSTRUMENT, CanonicalDecimal("12")),),
    )
    with pytest.raises(ReconciliationAuthorityError) as error:
        authority.admit_observation(conflicting, dispatch_sequence=9)
    assert error.value.code is OutcomeCode.CONFLICTING_ID
    assert len(authority.observation_index) == 1


def test_order_detail_observation_is_unresolved_correlation_retain_and_halt() -> None:
    reference = OpenReconciliationRef(
        EconomicId(RUN_ID, EconomicOwnerKind.EXECUTION_FILL, 9),
        Sha256Digest("55" * 32),
        Sha256Digest("66" * 32),
    )
    snapshot = _snapshot(open_reconciliation_refs=(reference,))
    authority = _authority(snapshot)
    observation = _observation(
        kind=ReconciliationObservationKind.ORDER_DETAIL,
        scope=ReconciliationScopeKind.ORDER,
        balances=(),
    )

    outcome = authority.admit_observation(observation, dispatch_sequence=7)

    assert outcome.outcome_code is OutcomeCode.RECONCILIATION_UNRESOLVED_CORRELATION
    assert outcome.requested_action is ReconciliationRequestedAction.RETAIN_AND_HALT
    assert outcome.watermark_comparison is ReconciliationWatermarkComparison.EQUAL
    assert outcome.halt_requested is True
    assert outcome.discrepancies == ()


def test_propose_adjustment_command_requires_eligible_row_and_derives_exact_command() -> None:
    snapshot = _snapshot()
    authority = _authority(snapshot)
    observation = _mismatch_observation()
    outcome = authority.admit_observation(observation, dispatch_sequence=7)

    command = _propose(authority, snapshot=snapshot, observation=observation, outcome=outcome)

    assert type(command) is ReconciliationAdjustmentCommand
    assert command.adjustment_id == EconomicId(
        RUN_ID,
        EconomicOwnerKind.RECONCILIATION_ADJUSTMENT,
        1,
    )
    assert command.observation_sha256 == reconciliation_observation_digest(observation)
    assert command.reconciliation_outcome_sha256 == reconciliation_outcome_digest(outcome)
    assert command.variant is ReconciliationAdjustmentVariant.BALANCE_CORRECTION
    assert command.target_kind is ReconciliationAdjustmentTargetKind.INSTRUMENT_POSITION
    assert command.instrument == INSTRUMENT
    assert command.local_amount == CanonicalDecimal("10")
    assert command.observed_amount == CanonicalDecimal("12")
    assert command.delta == CanonicalDecimal("2")
    assert command.dispatch_sequence == outcome.dispatch_sequence
    assert command.ledger_sequence == snapshot.ledger_sequence
    assert command.local_snapshot_sha256 == portfolio_snapshot_digest(snapshot)
    assert command.open_reconciliation_ref is None
    assert command.ancestry_order_id is None
    assert authority.next_adjustment_sequence == 2

    match_observation = _observation(source_sequence=8, observation_sequence=2)
    match_outcome = authority.admit_observation(match_observation, dispatch_sequence=8)
    with pytest.raises(ReconciliationAuthorityError) as error:
        _propose(authority, snapshot=snapshot, observation=match_observation, outcome=match_outcome)
    assert error.value.code is OutcomeCode.CONFLICTING_ID

    ancestry_outcome = create_reconciliation_outcome(
        run_id=RUN_ID,
        dispatch_sequence=9,
        observation_sha256=reconciliation_observation_digest(observation),
        local_snapshot_version=snapshot.snapshot_version,
        local_snapshot_sha256=portfolio_snapshot_digest(snapshot),
        ledger_sequence=snapshot.ledger_sequence,
        watermark_comparison=ReconciliationWatermarkComparison.EQUAL,
        discrepancies=(),
        outcome_code=OutcomeCode.RECONCILIATION_MATCH,
        requested_action=ReconciliationRequestedAction.PROPOSE_ANCESTRY_RESOLUTION,
        halt_requested=True,
    )
    with pytest.raises(ReconciliationAuthorityError) as error:
        _propose(authority, snapshot=snapshot, observation=observation, outcome=ancestry_outcome)
    assert error.value.code is OutcomeCode.CONFLICTING_ID


def test_authorization_issuance_is_sealed_one_use_and_records_denial() -> None:
    snapshot = _snapshot()
    authority = _authority(snapshot)

    first_observation = _mismatch_observation()
    first_outcome = authority.admit_observation(first_observation, dispatch_sequence=7)
    first_command = _propose(
        authority,
        snapshot=snapshot,
        observation=first_observation,
        outcome=first_outcome,
    )
    first = _issue(authority, outcome=first_outcome, command=first_command)

    assert type(first) is ReconciliationAdjustmentAuthorization
    assert first.authorization_id == EconomicId(
        RUN_ID,
        EconomicOwnerKind.RECONCILIATION_AUTHORIZATION,
        1,
    )
    assert first.adjustment_id == first_command.adjustment_id
    assert first.adjustment_id == EconomicId(
        RUN_ID,
        EconomicOwnerKind.RECONCILIATION_ADJUSTMENT,
        1,
    )
    assert first.command_sha256 == reconciliation_adjustment_command_digest(first_command)
    assert first.observation_sha256 == first_outcome.observation_sha256
    assert first.decision is ReconciliationAuthorizationDecision.ALLOWED
    assert authority.has_issued_authorization(first.authorization_id) is True
    assert authority.next_authorization_sequence == 2
    assert authority.next_adjustment_sequence == 2

    second_observation = _mismatch_observation(source_sequence=8, observation_sequence=2)
    second_outcome = authority.admit_observation(second_observation, dispatch_sequence=8)
    second_command = _propose(
        authority,
        snapshot=snapshot,
        observation=second_observation,
        outcome=second_outcome,
    )
    second = _issue(authority, outcome=second_outcome, command=second_command)
    assert second.authorization_id != first.authorization_id
    assert second.adjustment_id != first.adjustment_id
    assert second.authorization_id.owner_sequence == 2
    assert authority.has_issued_authorization(second.authorization_id) is True

    third_observation = _mismatch_observation(source_sequence=9, observation_sequence=3)
    third_outcome = authority.admit_observation(third_observation, dispatch_sequence=9)
    third_command = _propose(
        authority,
        snapshot=snapshot,
        observation=third_observation,
        outcome=third_outcome,
    )
    denied = _issue(
        authority,
        outcome=third_outcome,
        command=third_command,
        decision=ReconciliationAuthorizationDecision.DENIED,
    )
    assert denied.decision is ReconciliationAuthorizationDecision.DENIED
    assert authority.has_issued_authorization(denied.authorization_id) is True
    assert authority.next_authorization_sequence == 4


def test_factory_rejects_wrong_run_and_spec_set_binding() -> None:
    other_snapshot = _snapshot(run_id=OTHER_RUN_ID)
    with pytest.raises(ReconciliationAuthorityError) as error:
        create_phase1_reconciliation_authority(
            run_id=RUN_ID,
            spec_set=_spec_set(),
            snapshot_view=lambda: other_snapshot,
        )
    assert error.value.code is OutcomeCode.CONFLICTING_ID

    foreign = _spec_set(_spec(specification_id="xnas.foreign.v1"))
    with pytest.raises(ReconciliationAuthorityError) as error:
        create_phase1_reconciliation_authority(
            run_id=RUN_ID,
            spec_set=foreign,
            snapshot_view=lambda: _snapshot(),
        )
    assert error.value.code is OutcomeCode.CONFLICTING_ID

    with pytest.raises(ReconciliationAuthorityError) as error:
        create_phase1_reconciliation_authority(
            run_id=RUN_ID,
            spec_set=_spec_set(),
            snapshot_view=lambda: object(),  # type: ignore[arg-type]
        )
    assert error.value.code is OutcomeCode.INVALID_TYPE

    with pytest.raises(ReconciliationAuthorityError) as error:
        create_phase1_reconciliation_authority(
            run_id=RUN_ID,
            spec_set=_spec_set(),
            snapshot_view=123,  # type: ignore[arg-type]
        )
    assert error.value.code is OutcomeCode.INVALID_TYPE


def test_reconciliation_authority_module_keeps_the_import_boundary() -> None:
    path = PROJECT_ROOT / "src" / "ea" / "reconciliation" / "authority.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert all(
        name in {"__future__", "collections.abc", "dataclasses", "datetime", "types", "typing"}
        or name.startswith("ea.core.")
        for name in imports
    )
    assert not {"ea.runtime", "ea.composition", "ea.experiments"} & imports


def test_admit_observation_never_mutates_the_ledger_snapshot() -> None:
    holder: dict[str, PortfolioSnapshot] = {"snapshot": _snapshot()}
    authority = create_phase1_reconciliation_authority(
        run_id=RUN_ID,
        spec_set=_spec_set(),
        snapshot_view=lambda: holder["snapshot"],
    )
    before = canonical_portfolio_snapshot_bytes(holder["snapshot"])

    authority.admit_observation(_observation(), dispatch_sequence=7)
    assert canonical_portfolio_snapshot_bytes(holder["snapshot"]) == before

    authority.admit_observation(
        _mismatch_observation(source_sequence=8, observation_sequence=2),
        dispatch_sequence=8,
    )
    assert canonical_portfolio_snapshot_bytes(holder["snapshot"]) == before
    assert holder["snapshot"].ledger_sequence == 3
    assert holder["snapshot"].snapshot_version == 3


def test_trade_detail_observation_is_incomparable_invalid_retain_and_halt() -> None:
    snapshot = _snapshot()
    authority = _authority(snapshot)
    observation = _observation(
        kind=ReconciliationObservationKind.TRADE_DETAIL,
        scope=ReconciliationScopeKind.TRADE,
        balances=(),
    )

    outcome = authority.admit_observation(observation, dispatch_sequence=7)

    assert outcome.outcome_code is OutcomeCode.RECONCILIATION_INVALID
    assert outcome.requested_action is ReconciliationRequestedAction.RETAIN_AND_HALT
    assert outcome.watermark_comparison is ReconciliationWatermarkComparison.INCOMPARABLE
    assert outcome.halt_requested is True
    assert outcome.discrepancies == ()


def test_observation_only_authority_rejects_trade_detail_before_retaining_state() -> None:
    from ea.reconciliation.authority import _create_observation_only_reconciliation_authority

    authority = _create_observation_only_reconciliation_authority(
        run_id=RUN_ID,
        spec_set=_spec_set(),
        snapshot_view=lambda: _snapshot(),
    )
    trade_detail = _observation(
        kind=ReconciliationObservationKind.TRADE_DETAIL,
        scope=ReconciliationScopeKind.TRADE,
        balances=(),
    )

    with pytest.raises(ReconciliationAuthorityError, match="trade detail"):
        authority.admit_observation(trade_detail, dispatch_sequence=7)

    assert authority.observation_index == {}
    assert authority.outcome_index == {}
    assert not {
        "propose_adjustment_command",
        "issue_adjustment_authorization",
        "has_issued_authorization",
    } & set(dir(authority))


def test_non_ledger_watermark_namespace_is_incomparable_invalid_retain_and_halt() -> None:
    snapshot = _snapshot()
    authority = _authority(snapshot)
    observation = _observation(watermark_namespace=SourceNamespace("venue.snapshot"))

    outcome = authority.admit_observation(observation, dispatch_sequence=7)

    assert outcome.outcome_code is OutcomeCode.RECONCILIATION_INVALID
    assert outcome.requested_action is ReconciliationRequestedAction.RETAIN_AND_HALT
    assert outcome.watermark_comparison is ReconciliationWatermarkComparison.INCOMPARABLE
    assert outcome.halt_requested is True
    assert outcome.discrepancies == ()


def test_incomplete_observation_scope_is_invalid_not_implicit_zero() -> None:
    # ADR 0022 L148-149 / LEDGER-002: a locally-held balance omitted from the
    # declared observation scope is invalid evidence, never an implicit zero.
    second = Instrument(VenueId("XNAS"), "TSLA")
    spec_set = _spec_set(
        _spec(),
        _spec(instrument=second, specification_id="xnas.tsla.v1"),
    )
    snapshot = _snapshot(
        spec_set=spec_set,
        position_balances=(
            PositionBalance(INSTRUMENT, CanonicalDecimal("1"), CanonicalDecimal("10")),
            PositionBalance(second, CanonicalDecimal("1"), CanonicalDecimal("5")),
        ),
    )
    authority = _authority(snapshot, spec_set=spec_set)
    observation = _observation(
        spec_set=spec_set,
        balances=(PositionReconciliationBalance(INSTRUMENT, CanonicalDecimal("12")),),
    )

    outcome = authority.admit_observation(observation, dispatch_sequence=7)

    assert outcome.outcome_code is OutcomeCode.RECONCILIATION_INVALID
    assert outcome.requested_action is ReconciliationRequestedAction.RETAIN_AND_HALT
    assert outcome.halt_requested is True
    assert outcome.discrepancies == ()
