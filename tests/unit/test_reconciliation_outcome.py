from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

import ea.core as core
from ea.core import (
    EMPTY_CHAIN_HEAD_SHA256,
    EMPTY_RECORD_SHA256,
    AuditAppendAcknowledgement,
    AuditContractError,
    AuditedReconciliationAdjustmentAuthorization,
    AuditRecordKind,
    AuditSubjectKind,
    CanonicalDecimal,
    CashBalance,
    CashReconciliationBalance,
    EconomicId,
    EconomicOwnerKind,
    ExistingLedgerBinding,
    InstrumentExecutionSpecSet,
    OpenReconciliationRef,
    OutcomeCode,
    PortfolioLedgerError,
    PortfolioSnapshot,
    PositionBalance,
    PositionReconciliationBalance,
    ReconciliationAdjustmentAuthorization,
    ReconciliationAdjustmentCommand,
    ReconciliationAuthorizationDecision,
    ReconciliationAuthorizationPolicyId,
    ReconciliationContractError,
    ReconciliationObservation,
    ReconciliationObservationKind,
    ReconciliationOutcome,
    ReconciliationRequestedAction,
    ReconciliationScopeKind,
    ReconciliationWatermarkComparison,
    RunBinding,
    RunReference,
    Sha256Digest,
    SourceNamespace,
    UnresolvedFillRef,
    audit_subject_digest,
    audited_reconciliation_adjustment_authorization_digest,
    canonical_audited_reconciliation_adjustment_authorization_bytes,
    canonical_portfolio_snapshot_bytes,
    canonical_reconciliation_adjustment_authorization_bytes,
    canonical_reconciliation_adjustment_command_bytes,
    canonical_reconciliation_outcome_bytes,
    create_audit_append_acknowledgement,
    create_audit_record,
    create_audited_reconciliation_adjustment_authorization,
    create_cash_reconciliation_discrepancy,
    create_fill,
    create_order,
    create_position_reconciliation_discrepancy,
    create_reconciliation_observation,
    create_reconciliation_outcome,
    decode_reconciliation_outcome,
    fill_digest,
    instrument_spec_set_digest,
    portfolio_snapshot_digest,
    reconciliation_adjustment_authorization_digest,
    reconciliation_adjustment_command_digest,
    reconciliation_observation_digest,
    reconciliation_outcome_digest,
)
from ea.core.audit import require_canonical_audit_payload
from ea.core.reconciliation import (
    _ancestry_evidence_digest,
    _ancestry_order_scope_id,
    _authorization_binding,
    _canonical_ancestry_evidence_bytes,
    _create_reconciliation_adjustment_authorization,
    _create_reconciliation_adjustment_command,
    _decode_reconciliation_adjustment_authorization,
    _decode_reconciliation_adjustment_command,
)
from unit.test_execution_messages import SPEC_SET, _allow, _intent, _order, _trade_fact
from unit.test_portfolio_ledger import INSTRUMENT, RUN_ID, USD, _spec_set
from unit.test_reconciliation_observation import TIME, _observation

OBSERVATION_SHA256 = Sha256Digest("11" * 32)
SNAPSHOT_SHA256 = Sha256Digest("22" * 32)
BINDING = RunBinding(RunReference(RUN_ID, Sha256Digest("33" * 32)), Sha256Digest("44" * 32))


def _canonical(document: object) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _snapshot(
    *,
    spec_set: InstrumentExecutionSpecSet | None = None,
    position_amount: str | None = "10",
    cash_amount: str | None = None,
    open_reconciliation_refs: tuple[OpenReconciliationRef, ...] = (),
) -> PortfolioSnapshot:
    selected = _spec_set() if spec_set is None else spec_set
    return PortfolioSnapshot(
        run_id=RUN_ID,
        instrument_spec_set_id=selected.identifier,
        instrument_spec_set_sha256=instrument_spec_set_digest(selected),
        snapshot_version=3,
        ledger_sequence=3,
        last_entry_id=EconomicId(RUN_ID, EconomicOwnerKind.LEDGER_ENTRY, 3),
        last_transaction_sha256=Sha256Digest("88" * 32),
        cash_balances=(
            ()
            if cash_amount is None
            else (CashBalance(USD, CanonicalDecimal("0.01"), CanonicalDecimal(cash_amount)),)
        ),
        position_balances=(
            ()
            if position_amount is None
            else (
                PositionBalance(
                    INSTRUMENT,
                    CanonicalDecimal("1"),
                    CanonicalDecimal(position_amount),
                ),
            )
        ),
        rounding_balances=(),
        unresolved_fills=(),
        open_reconciliation_bindings=tuple(
            ExistingLedgerBinding(
                EconomicId(RUN_ID, EconomicOwnerKind.LEDGER_ENTRY, index),
                reference.fill_id,
                reference.fill_sha256,
                Sha256Digest(f"{index:064x}"),
            )
            for index, reference in enumerate(open_reconciliation_refs, start=1)
        ),
        open_reconciliation_refs=open_reconciliation_refs,
    )


def _outcome(**changes: object) -> ReconciliationOutcome:
    arguments: dict[str, object] = {
        "run_id": RUN_ID,
        "dispatch_sequence": 7,
        "observation_sha256": OBSERVATION_SHA256,
        "local_snapshot_version": 4,
        "local_snapshot_sha256": SNAPSHOT_SHA256,
        "ledger_sequence": 4,
        "watermark_comparison": ReconciliationWatermarkComparison.EQUAL,
        "discrepancies": (),
        "outcome_code": OutcomeCode.RECONCILIATION_MATCH,
        "requested_action": ReconciliationRequestedAction.NONE,
        "halt_requested": False,
    }
    arguments.update(changes)
    return create_reconciliation_outcome(**arguments)  # type: ignore[arg-type]


def test_portfolio_snapshot_digest_binds_complete_ancestry_open_reference() -> None:
    reference = OpenReconciliationRef(
        EconomicId(RUN_ID, EconomicOwnerKind.EXECUTION_FILL, 9),
        Sha256Digest("55" * 32),
        Sha256Digest("66" * 32),
    )
    snapshot = _snapshot(open_reconciliation_refs=(reference,))
    document = json.loads(canonical_portfolio_snapshot_bytes(snapshot))

    assert snapshot.unresolved_fills == ()
    assert document["open_reconciliation_bindings"] == [
        {
            "entry_id": {
                "owner_kind": "ledger.entry",
                "owner_sequence": 1,
                "run_id": RUN_ID.value,
            },
            "fill_id": {
                "owner_kind": "execution.fill",
                "owner_sequence": 9,
                "run_id": RUN_ID.value,
            },
            "fill_sha256": "55" * 32,
            "transaction_sha256": f"{1:064x}",
        }
    ]
    assert document["open_reconciliation_refs"] == [
        {
            "fill_id": {
                "owner_kind": "execution.fill",
                "owner_sequence": 9,
                "run_id": RUN_ID.value,
            },
            "fill_sha256": "55" * 32,
            "processing_outcome_sha256": "66" * 32,
        }
    ]
    with pytest.raises(PortfolioLedgerError, match="exact ledger binding"):
        PortfolioSnapshot(
            run_id=snapshot.run_id,
            instrument_spec_set_id=snapshot.instrument_spec_set_id,
            instrument_spec_set_sha256=snapshot.instrument_spec_set_sha256,
            snapshot_version=snapshot.snapshot_version,
            ledger_sequence=snapshot.ledger_sequence,
            last_entry_id=snapshot.last_entry_id,
            last_transaction_sha256=snapshot.last_transaction_sha256,
            cash_balances=snapshot.cash_balances,
            position_balances=snapshot.position_balances,
            rounding_balances=snapshot.rounding_balances,
            unresolved_fills=(),
            open_reconciliation_bindings=(),
            open_reconciliation_refs=(reference,),
        )


def test_portfolio_snapshot_rejects_open_and_unresolved_fill_digest_conflict() -> None:
    reference = OpenReconciliationRef(
        EconomicId(RUN_ID, EconomicOwnerKind.EXECUTION_FILL, 9),
        Sha256Digest("55" * 32),
        Sha256Digest("66" * 32),
    )
    snapshot = _snapshot(open_reconciliation_refs=(reference,))

    with pytest.raises(PortfolioLedgerError, match="open and unresolved Fill digests"):
        PortfolioSnapshot(
            run_id=snapshot.run_id,
            instrument_spec_set_id=snapshot.instrument_spec_set_id,
            instrument_spec_set_sha256=snapshot.instrument_spec_set_sha256,
            snapshot_version=snapshot.snapshot_version,
            ledger_sequence=snapshot.ledger_sequence,
            last_entry_id=snapshot.last_entry_id,
            last_transaction_sha256=snapshot.last_transaction_sha256,
            cash_balances=snapshot.cash_balances,
            position_balances=snapshot.position_balances,
            rounding_balances=snapshot.rounding_balances,
            unresolved_fills=(UnresolvedFillRef(reference.fill_id, Sha256Digest("77" * 32)),),
            open_reconciliation_bindings=snapshot.open_reconciliation_bindings,
            open_reconciliation_refs=(reference,),
        )

    matching = replace(
        snapshot,
        unresolved_fills=(UnresolvedFillRef(reference.fill_id, reference.fill_sha256),),
    )
    assert matching.unresolved_fills[0].fill_sha256 == reference.fill_sha256


def test_portfolio_snapshot_rejects_inconsistent_open_frontier_bindings() -> None:
    first = OpenReconciliationRef(
        EconomicId(RUN_ID, EconomicOwnerKind.EXECUTION_FILL, 9),
        Sha256Digest("55" * 32),
        Sha256Digest("66" * 32),
    )
    second = OpenReconciliationRef(
        EconomicId(RUN_ID, EconomicOwnerKind.EXECUTION_FILL, 10),
        Sha256Digest("77" * 32),
        Sha256Digest("88" * 32),
    )
    snapshot = _snapshot(open_reconciliation_refs=(first, second))
    first_binding, second_binding = snapshot.open_reconciliation_bindings

    with pytest.raises(PortfolioLedgerError, match="lacks its exact reference"):
        replace(snapshot, open_reconciliation_refs=(first,))
    with pytest.raises(PortfolioLedgerError, match="keys are not canonical"):
        replace(
            snapshot,
            open_reconciliation_bindings=(second_binding, first_binding),
        )
    with pytest.raises(PortfolioLedgerError, match="reuse a ledger entry"):
        replace(
            snapshot,
            open_reconciliation_bindings=(
                first_binding,
                ExistingLedgerBinding(
                    first_binding.entry_id,
                    second.fill_id,
                    second.fill_sha256,
                    second_binding.transaction_sha256,
                ),
            ),
        )
    with pytest.raises(PortfolioLedgerError, match="exact ledger binding"):
        replace(
            snapshot,
            open_reconciliation_bindings=(
                ExistingLedgerBinding(
                    first_binding.entry_id,
                    first.fill_id,
                    Sha256Digest("99" * 32),
                    first_binding.transaction_sha256,
                ),
                second_binding,
            ),
        )
    with pytest.raises(PortfolioLedgerError, match="ahead of the snapshot frontier"):
        replace(
            snapshot,
            open_reconciliation_bindings=(
                ExistingLedgerBinding(
                    EconomicId(RUN_ID, EconomicOwnerKind.LEDGER_ENTRY, 4),
                    first.fill_id,
                    first.fill_sha256,
                    first_binding.transaction_sha256,
                ),
                second_binding,
            ),
        )
    with pytest.raises(PortfolioLedgerError, match="applied transaction"):
        replace(
            snapshot,
            open_reconciliation_bindings=(
                ExistingLedgerBinding(
                    EconomicId(RUN_ID, EconomicOwnerKind.LEDGER_ENTRY, 0),
                    first.fill_id,
                    first.fill_sha256,
                    first_binding.transaction_sha256,
                ),
                second_binding,
            ),
        )
    assert snapshot.last_transaction_sha256 is not None
    last_transaction_binding = ExistingLedgerBinding(
        EconomicId(RUN_ID, EconomicOwnerKind.LEDGER_ENTRY, 3),
        first.fill_id,
        first.fill_sha256,
        snapshot.last_transaction_sha256,
    )
    assert (
        replace(
            snapshot,
            open_reconciliation_bindings=(last_transaction_binding, second_binding),
        ).open_reconciliation_bindings[0]
        == last_transaction_binding
    )
    with pytest.raises(PortfolioLedgerError, match="last transaction"):
        replace(
            snapshot,
            open_reconciliation_bindings=(
                ExistingLedgerBinding(
                    EconomicId(RUN_ID, EconomicOwnerKind.LEDGER_ENTRY, 3),
                    first.fill_id,
                    first.fill_sha256,
                    first_binding.transaction_sha256,
                ),
                second_binding,
            ),
        )


def _outcome_acknowledgement(
    outcome: ReconciliationOutcome,
) -> AuditAppendAcknowledgement:
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


def _balance_command_bundle() -> tuple[
    PortfolioSnapshot,
    ReconciliationObservation,
    ReconciliationOutcome,
    AuditAppendAcknowledgement,
    ReconciliationAdjustmentCommand,
]:
    snapshot = _snapshot()
    observation = _observation(
        balances=(PositionReconciliationBalance(INSTRUMENT, CanonicalDecimal("12")),),
    )
    discrepancy = create_position_reconciliation_discrepancy(
        spec_set=_spec_set(),
        instrument=INSTRUMENT,
        local_amount=CanonicalDecimal("10"),
        observed_amount=CanonicalDecimal("12"),
    )
    outcome = _outcome(
        observation_sha256=reconciliation_observation_digest(observation),
        local_snapshot_version=snapshot.snapshot_version,
        local_snapshot_sha256=portfolio_snapshot_digest(snapshot),
        ledger_sequence=snapshot.ledger_sequence,
        discrepancies=(discrepancy,),
        outcome_code=OutcomeCode.RECONCILIATION_MISMATCH,
        requested_action=ReconciliationRequestedAction.PROPOSE_SINGLE_TARGET_ADJUSTMENT,
        halt_requested=True,
    )
    acknowledgement = _outcome_acknowledgement(outcome)
    command = _create_reconciliation_adjustment_command(
        binding=BINDING,
        spec_set=_spec_set(),
        observation=observation,
        outcome=outcome,
        outcome_acknowledgement=acknowledgement,
        local_snapshot=snapshot,
        adjustment_id=EconomicId(
            RUN_ID,
            EconomicOwnerKind.RECONCILIATION_ADJUSTMENT,
            1,
        ),
    )
    return snapshot, observation, outcome, acknowledgement, command


def _authorization_acknowledgement(
    authorization: ReconciliationAdjustmentAuthorization,
) -> AuditAppendAcknowledgement:
    payload = canonical_reconciliation_adjustment_authorization_bytes(authorization)
    record = create_audit_record(
        binding=BINDING,
        owner_sequence=3,
        record_kind=AuditRecordKind.RECONCILIATION_ADJUSTMENT_AUTHORIZATION,
        subject_kind=AuditSubjectKind.RECONCILIATION_ADJUSTMENT_AUTHORIZATION,
        subject_sha256=audit_subject_digest(
            AuditRecordKind.RECONCILIATION_ADJUSTMENT_AUTHORIZATION,
            payload,
        ),
        canonical_payload=payload,
        previous_record_sha256=EMPTY_RECORD_SHA256,
        previous_chain_head_sha256=EMPTY_CHAIN_HEAD_SHA256,
    )
    return create_audit_append_acknowledgement(record)


def test_outcome_v2_round_trips_without_command_digest_field() -> None:
    outcome = _outcome()
    payload = canonical_reconciliation_outcome_bytes(outcome)
    document = json.loads(payload)

    assert document["schema"] == "ea.reconciliation-outcome.v2"
    assert "proposed_adjustment_command_sha256" not in document
    assert decode_reconciliation_outcome(payload, _spec_set()) == outcome
    assert reconciliation_outcome_digest(outcome).value == (
        "dbf01f9065a911a5addc1468eb0d749bb76f1067e377bd55af253d3c592affa8"
    )
    with pytest.raises(FrozenInstanceError):
        outcome.halt_requested = True  # type: ignore[misc]


def test_single_position_discrepancy_is_exact_proposal_basis() -> None:
    discrepancy = create_position_reconciliation_discrepancy(
        spec_set=_spec_set(),
        instrument=INSTRUMENT,
        local_amount=CanonicalDecimal("10"),
        observed_amount=CanonicalDecimal("12"),
    )
    outcome = _outcome(
        discrepancies=(discrepancy,),
        outcome_code=OutcomeCode.RECONCILIATION_MISMATCH,
        requested_action=ReconciliationRequestedAction.PROPOSE_SINGLE_TARGET_ADJUSTMENT,
        halt_requested=True,
    )

    decoded = decode_reconciliation_outcome(
        canonical_reconciliation_outcome_bytes(outcome),
        _spec_set(),
    )

    assert decoded == outcome
    assert decoded.discrepancies[0].delta == CanonicalDecimal("2")


def test_single_cash_discrepancy_derives_negative_delta() -> None:
    discrepancy = create_cash_reconciliation_discrepancy(
        spec_set=_spec_set(),
        currency=USD,
        local_amount=CanonicalDecimal("125.5"),
        observed_amount=CanonicalDecimal("120"),
    )
    outcome = _outcome(
        discrepancies=(discrepancy,),
        outcome_code=OutcomeCode.RECONCILIATION_MISMATCH,
        requested_action=ReconciliationRequestedAction.PROPOSE_SINGLE_TARGET_ADJUSTMENT,
        halt_requested=True,
    )

    assert outcome.discrepancies[0].delta == CanonicalDecimal("-5.5")


@pytest.mark.parametrize(
    "changes",
    [
        {"dispatch_sequence": 0},
        {"dispatch_sequence": True},
        {"outcome_code": OutcomeCode.RECONCILIATION_MISMATCH},
        {"halt_requested": True},
        {
            "watermark_comparison": ReconciliationWatermarkComparison.REMOTE_HIGHER,
            "outcome_code": OutcomeCode.RECONCILIATION_REMOTE_AHEAD,
        },
    ],
)
def test_outcome_action_matrix_rejects_conflicting_evidence(changes: dict[str, object]) -> None:
    with pytest.raises(ReconciliationContractError):
        _outcome(**changes)


def test_outcome_decoder_rejects_v1_command_digest_unknown_and_noncanonical() -> None:
    payload = canonical_reconciliation_outcome_bytes(_outcome())
    document = json.loads(payload)
    invalid_payloads = (
        _canonical({**document, "schema": "ea.reconciliation-outcome.v1"}),
        _canonical({**document, "proposed_adjustment_command_sha256": None}),
        _canonical({**document, "requested_action": "unknown"}),
        _canonical({**document, "local_snapshot_version": True}),
        payload + b" ",
        payload.replace(b'"run_id":', b'"run_id":"duplicate","run_id":', 1),
    )

    for invalid in invalid_payloads:
        with pytest.raises(ReconciliationContractError):
            decode_reconciliation_outcome(invalid, _spec_set())


def test_reconciliation_outcome_audit_vocabulary_accepts_only_v2_contract() -> None:
    payload = canonical_reconciliation_outcome_bytes(_outcome())

    assert (
        require_canonical_audit_payload(
            AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME,
            payload,
        )
        is payload
    )
    assert (
        len(
            audit_subject_digest(
                AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME,
                payload,
            ).value
        )
        == 64
    )
    document = json.loads(payload)
    with pytest.raises(AuditContractError):
        require_canonical_audit_payload(
            AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME,
            _canonical({**document, "schema": "ea.reconciliation-outcome.v1"}),
        )
    with pytest.raises(AuditContractError):
        require_canonical_audit_payload(
            AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME,
            _canonical({**document, "halt_requested": True}),
        )


def test_outcome_audit_rejects_tampered_discrepancy_delta() -> None:
    discrepancy = create_position_reconciliation_discrepancy(
        spec_set=_spec_set(),
        instrument=INSTRUMENT,
        local_amount=CanonicalDecimal("10"),
        observed_amount=CanonicalDecimal("12"),
    )
    outcome = _outcome(
        discrepancies=(discrepancy,),
        outcome_code=OutcomeCode.RECONCILIATION_MISMATCH,
        requested_action=ReconciliationRequestedAction.PROPOSE_SINGLE_TARGET_ADJUSTMENT,
        halt_requested=True,
    )
    document = json.loads(canonical_reconciliation_outcome_bytes(outcome))
    document["discrepancies"][0]["delta"] = "3"

    with pytest.raises(AuditContractError):
        require_canonical_audit_payload(
            AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME,
            _canonical(document),
        )


def test_balance_command_requires_exact_outcome_ack_and_is_rederived() -> None:
    snapshot = _snapshot()
    observation = _observation(
        balances=(PositionReconciliationBalance(INSTRUMENT, CanonicalDecimal("12")),),
    )
    discrepancy = create_position_reconciliation_discrepancy(
        spec_set=_spec_set(),
        instrument=INSTRUMENT,
        local_amount=CanonicalDecimal("10"),
        observed_amount=CanonicalDecimal("12"),
    )
    outcome = _outcome(
        observation_sha256=reconciliation_observation_digest(observation),
        local_snapshot_version=snapshot.snapshot_version,
        local_snapshot_sha256=portfolio_snapshot_digest(snapshot),
        ledger_sequence=snapshot.ledger_sequence,
        discrepancies=(discrepancy,),
        outcome_code=OutcomeCode.RECONCILIATION_MISMATCH,
        requested_action=ReconciliationRequestedAction.PROPOSE_SINGLE_TARGET_ADJUSTMENT,
        halt_requested=True,
    )
    acknowledgement = _outcome_acknowledgement(outcome)
    adjustment_id = EconomicId(
        RUN_ID,
        EconomicOwnerKind.RECONCILIATION_ADJUSTMENT,
        1,
    )

    command = _create_reconciliation_adjustment_command(
        binding=BINDING,
        spec_set=_spec_set(),
        observation=observation,
        outcome=outcome,
        outcome_acknowledgement=acknowledgement,
        local_snapshot=snapshot,
        adjustment_id=adjustment_id,
    )
    encoded = canonical_reconciliation_adjustment_command_bytes(command)

    assert (
        _decode_reconciliation_adjustment_command(
            encoded,
            binding=BINDING,
            spec_set=_spec_set(),
            observation=observation,
            outcome=outcome,
            outcome_acknowledgement=acknowledgement,
            local_snapshot=snapshot,
        )
        == command
    )
    assert len(reconciliation_adjustment_command_digest(command).value) == 64
    assert (
        canonical_reconciliation_adjustment_command_bytes(
            _create_reconciliation_adjustment_command(
                binding=BINDING,
                spec_set=_spec_set(),
                observation=observation,
                outcome=outcome,
                outcome_acknowledgement=acknowledgement,
                local_snapshot=snapshot,
                adjustment_id=adjustment_id,
            )
        )
        == encoded
    )
    with pytest.raises(TypeError, match="factory"):
        ReconciliationAdjustmentCommand()
    assert "_create_reconciliation_adjustment_command" not in core.__all__
    assert "_decode_reconciliation_adjustment_command" not in core.__all__
    with pytest.raises(ReconciliationContractError, match="acknowledgement"):
        _create_reconciliation_adjustment_command(
            binding=BINDING,
            spec_set=_spec_set(),
            observation=observation,
            outcome=outcome,
            outcome_acknowledgement=None,
            local_snapshot=snapshot,
            adjustment_id=adjustment_id,
        )
    document = json.loads(encoded)
    with pytest.raises(ReconciliationContractError):
        _decode_reconciliation_adjustment_command(
            _canonical({**document, "local_snapshot_sha256": "ff" * 32}),
            binding=BINDING,
            spec_set=_spec_set(),
            observation=observation,
            outcome=outcome,
            outcome_acknowledgement=acknowledgement,
            local_snapshot=snapshot,
        )
    with pytest.raises(ReconciliationContractError, match="evidence"):
        _create_reconciliation_adjustment_command(
            binding=BINDING,
            spec_set=_spec_set(),
            observation=observation,
            outcome=outcome,
            outcome_acknowledgement=acknowledgement,
            local_snapshot=_snapshot(position_amount="9"),
            adjustment_id=adjustment_id,
        )
    drifted_snapshot = _snapshot(position_amount="9")
    drifted_outcome = _outcome(
        observation_sha256=reconciliation_observation_digest(observation),
        local_snapshot_version=drifted_snapshot.snapshot_version,
        local_snapshot_sha256=portfolio_snapshot_digest(drifted_snapshot),
        ledger_sequence=drifted_snapshot.ledger_sequence,
        discrepancies=(discrepancy,),
        outcome_code=OutcomeCode.RECONCILIATION_MISMATCH,
        requested_action=ReconciliationRequestedAction.PROPOSE_SINGLE_TARGET_ADJUSTMENT,
        halt_requested=True,
    )
    with pytest.raises(ReconciliationContractError, match="proposal evidence"):
        _create_reconciliation_adjustment_command(
            binding=BINDING,
            spec_set=_spec_set(),
            observation=observation,
            outcome=drifted_outcome,
            outcome_acknowledgement=_outcome_acknowledgement(drifted_outcome),
            local_snapshot=drifted_snapshot,
            adjustment_id=adjustment_id,
        )

    foreign_watermark_observation = create_reconciliation_observation(
        run_id=observation.run_id,
        spec_set=_spec_set(),
        observation_id=observation.observation_id,
        kind=observation.kind,
        source_namespace=observation.source_namespace,
        source_sequence=observation.source_sequence,
        occurred_at=observation.occurred_at,
        available_at=observation.available_at,
        watermark_namespace=SourceNamespace("ledger.foreign"),
        watermark_sequence=observation.watermark_sequence,
        declared_scope_kind=observation.declared_scope_kind,
        declared_scope_id=observation.declared_scope_id,
        provenance_id=observation.provenance_id,
        provenance_payload_sha256=observation.provenance_payload_sha256,
        balances=observation.balances,
    )
    foreign_watermark_outcome = _outcome(
        observation_sha256=reconciliation_observation_digest(foreign_watermark_observation),
        local_snapshot_version=snapshot.snapshot_version,
        local_snapshot_sha256=portfolio_snapshot_digest(snapshot),
        ledger_sequence=snapshot.ledger_sequence,
        discrepancies=(discrepancy,),
        outcome_code=OutcomeCode.RECONCILIATION_MISMATCH,
        requested_action=ReconciliationRequestedAction.PROPOSE_SINGLE_TARGET_ADJUSTMENT,
        halt_requested=True,
    )
    with pytest.raises(ReconciliationContractError, match="evidence bindings"):
        _create_reconciliation_adjustment_command(
            binding=BINDING,
            spec_set=_spec_set(),
            observation=foreign_watermark_observation,
            outcome=foreign_watermark_outcome,
            outcome_acknowledgement=_outcome_acknowledgement(foreign_watermark_outcome),
            local_snapshot=snapshot,
            adjustment_id=adjustment_id,
        )


def test_ancestry_evidence_has_exact_adr_0024_payload_and_digest() -> None:
    order = _order()
    fill = create_fill(
        fill_id=EconomicId(RUN_ID, EconomicOwnerKind.EXECUTION_FILL, 9),
        fact=_trade_fact(resolved=False),
        spec_set=SPEC_SET,
    )
    reference = OpenReconciliationRef(
        fill.fill_id,
        fill_digest(fill),
        Sha256Digest("66" * 32),
    )
    assert _canonical_ancestry_evidence_bytes(fill, reference, order) == (
        b'{"canonicalization":"ea-reconciliation-v1","fill_id":{"owner_kind":'
        b'"execution.fill","owner_sequence":9,"run_id":"12345678-1234-4234-8234-'
        b'123456789abc"},"fill_sha256":"a61886047d302da13edb78a9b174640be95e9706651be'
        b'5699e54671299835a30","order_id":{"owner_kind":"execution.order","owner_sequence":'
        b'6,"run_id":"12345678-1234-4234-8234-123456789abc"},"order_sha256":"d041fee3'
        b'fea81d69ead4db20bdbea7c52db1ed59b05c50e265536aee7ea08d70","processing_outcome_'
        b'sha256":"6666666666666666666666666666666666666666666666666666666666666666",'
        b'"run_id":"12345678-1234-4234-8234-123456789abc","schema":"ea.reconciliation-'
        b'ancestry-evidence.v1"}'
    )
    assert _ancestry_evidence_digest(fill, reference, order).value == (
        "0eb4cb5c6a9c3e1e694777335b78773799664a540a57f734af6f0adaf3484b4c"
    )


def test_ancestry_command_binds_exact_open_reference_and_order() -> None:
    order = _order()
    fill = create_fill(
        fill_id=EconomicId(RUN_ID, EconomicOwnerKind.EXECUTION_FILL, 9),
        fact=_trade_fact(resolved=False),
        spec_set=SPEC_SET,
    )
    reference = OpenReconciliationRef(
        fill.fill_id,
        fill_digest(fill),
        Sha256Digest("66" * 32),
    )
    snapshot = _snapshot(
        spec_set=SPEC_SET,
        position_amount=None,
        open_reconciliation_refs=(reference,),
    )
    base_observation = _observation(
        kind=ReconciliationObservationKind.ORDER_DETAIL,
        scope=ReconciliationScopeKind.ORDER,
        balances=(),
    )
    observation = create_reconciliation_observation(
        run_id=RUN_ID,
        spec_set=SPEC_SET,
        observation_id=base_observation.observation_id,
        kind=base_observation.kind,
        source_namespace=base_observation.source_namespace,
        source_sequence=base_observation.source_sequence,
        occurred_at=base_observation.occurred_at,
        available_at=base_observation.available_at,
        watermark_namespace=base_observation.watermark_namespace,
        watermark_sequence=snapshot.ledger_sequence,
        declared_scope_kind=base_observation.declared_scope_kind,
        declared_scope_id=_ancestry_order_scope_id(order),
        provenance_id=base_observation.provenance_id,
        provenance_payload_sha256=_ancestry_evidence_digest(fill, reference, order),
        balances=(),
    )
    outcome = _outcome(
        observation_sha256=reconciliation_observation_digest(observation),
        local_snapshot_version=snapshot.snapshot_version,
        local_snapshot_sha256=portfolio_snapshot_digest(snapshot),
        ledger_sequence=snapshot.ledger_sequence,
        requested_action=ReconciliationRequestedAction.PROPOSE_ANCESTRY_RESOLUTION,
        halt_requested=True,
    )
    acknowledgement = _outcome_acknowledgement(outcome)

    command = _create_reconciliation_adjustment_command(
        binding=BINDING,
        spec_set=SPEC_SET,
        observation=observation,
        outcome=outcome,
        outcome_acknowledgement=acknowledgement,
        local_snapshot=snapshot,
        adjustment_id=EconomicId(
            RUN_ID,
            EconomicOwnerKind.RECONCILIATION_ADJUSTMENT,
            2,
        ),
        open_reconciliation_ref=reference,
        ancestry_fill=fill,
        ancestry_order=order,
    )

    document = json.loads(canonical_reconciliation_adjustment_command_bytes(command))
    assert document["variant"] == "ancestry_resolution"
    assert document["target"]["kind"] == "open_reconciliation_ref"
    assert (
        _decode_reconciliation_adjustment_command(
            canonical_reconciliation_adjustment_command_bytes(command),
            binding=BINDING,
            spec_set=SPEC_SET,
            observation=observation,
            outcome=outcome,
            outcome_acknowledgement=acknowledgement,
            local_snapshot=snapshot,
            ancestry_fill=fill,
            ancestry_order=order,
        )
        == command
    )
    foreign_fill = create_fill(
        fill_id=EconomicId(RUN_ID, EconomicOwnerKind.EXECUTION_FILL, 10),
        fact=_trade_fact(resolved=False),
        spec_set=SPEC_SET,
    )
    with pytest.raises(ReconciliationContractError, match="evidence"):
        _create_reconciliation_adjustment_command(
            binding=BINDING,
            spec_set=SPEC_SET,
            observation=observation,
            outcome=outcome,
            outcome_acknowledgement=acknowledgement,
            local_snapshot=snapshot,
            adjustment_id=command.adjustment_id,
            open_reconciliation_ref=reference,
            ancestry_fill=foreign_fill,
            ancestry_order=order,
        )
    foreign_intent = _intent()
    foreign_order = create_order(
        order_id=EconomicId(RUN_ID, EconomicOwnerKind.EXECUTION_ORDER, 7),
        intent=foreign_intent,
        decision=_allow(foreign_intent),
        spec_set=SPEC_SET,
    )
    with pytest.raises(ReconciliationContractError, match="evidence"):
        _create_reconciliation_adjustment_command(
            binding=BINDING,
            spec_set=SPEC_SET,
            observation=observation,
            outcome=outcome,
            outcome_acknowledgement=acknowledgement,
            local_snapshot=snapshot,
            adjustment_id=command.adjustment_id,
            open_reconciliation_ref=reference,
            ancestry_fill=fill,
            ancestry_order=foreign_order,
        )
    with pytest.raises(ReconciliationContractError, match="acknowledgement"):
        _decode_reconciliation_adjustment_command(
            canonical_reconciliation_adjustment_command_bytes(command),
            binding=BINDING,
            spec_set=SPEC_SET,
            observation=observation,
            outcome=outcome,
            outcome_acknowledgement=None,
            local_snapshot=snapshot,
            ancestry_fill=fill,
            ancestry_order=order,
        )
    with pytest.raises(ReconciliationContractError, match="evidence"):
        _create_reconciliation_adjustment_command(
            binding=BINDING,
            spec_set=SPEC_SET,
            observation=observation,
            outcome=outcome,
            outcome_acknowledgement=acknowledgement,
            local_snapshot=_snapshot(spec_set=SPEC_SET, position_amount=None),
            adjustment_id=command.adjustment_id,
            open_reconciliation_ref=reference,
            ancestry_fill=fill,
            ancestry_order=order,
        )


def test_cash_command_uses_exact_observed_balance_and_round_trips() -> None:
    snapshot = _snapshot(position_amount=None, cash_amount="125.5")
    observation = _observation(
        kind=ReconciliationObservationKind.CASH_SNAPSHOT,
        scope=ReconciliationScopeKind.CASH,
        balances=(CashReconciliationBalance(USD, CanonicalDecimal("120")),),
    )
    discrepancy = create_cash_reconciliation_discrepancy(
        spec_set=_spec_set(),
        currency=USD,
        local_amount=CanonicalDecimal("125.5"),
        observed_amount=CanonicalDecimal("120"),
    )
    outcome = _outcome(
        observation_sha256=reconciliation_observation_digest(observation),
        local_snapshot_version=snapshot.snapshot_version,
        local_snapshot_sha256=portfolio_snapshot_digest(snapshot),
        ledger_sequence=snapshot.ledger_sequence,
        discrepancies=(discrepancy,),
        outcome_code=OutcomeCode.RECONCILIATION_MISMATCH,
        requested_action=ReconciliationRequestedAction.PROPOSE_SINGLE_TARGET_ADJUSTMENT,
        halt_requested=True,
    )
    command = _create_reconciliation_adjustment_command(
        binding=BINDING,
        spec_set=_spec_set(),
        observation=observation,
        outcome=outcome,
        outcome_acknowledgement=_outcome_acknowledgement(outcome),
        local_snapshot=snapshot,
        adjustment_id=EconomicId(
            RUN_ID,
            EconomicOwnerKind.RECONCILIATION_ADJUSTMENT,
            3,
        ),
    )
    payload = canonical_reconciliation_adjustment_command_bytes(command)

    assert json.loads(payload)["target"]["kind"] == "settlement_cash"
    acknowledgement = _outcome_acknowledgement(outcome)
    assert (
        _decode_reconciliation_adjustment_command(
            payload,
            binding=BINDING,
            spec_set=_spec_set(),
            observation=observation,
            outcome=outcome,
            outcome_acknowledgement=acknowledgement,
            local_snapshot=snapshot,
        )
        == command
    )
    document = json.loads(payload)
    invalid_payloads = (
        _canonical({key: value for key, value in document.items() if key != "schema"}),
        _canonical({**document, "schema": "ea.reconciliation-adjustment-command.v0"}),
        _canonical({**document, "canonicalization": "unknown"}),
        _canonical({**document, "target": None}),
        _canonical({**document, "unexpected": None}),
        _canonical({**document, "delta": "0"}),
        _canonical({**document, "target": {**document["target"], "unexpected": None}}),
        _canonical(
            {
                **document,
                "target": {
                    **document["target"],
                    "kind": "open_reconciliation_ref",
                },
            }
        ),
        _canonical({**document, "observed_amount": "121", "delta": "-4.5"}),
        _canonical({**document, "target": {**document["target"], "kind": "unknown"}}),
    )
    for invalid in invalid_payloads:
        with pytest.raises(ReconciliationContractError):
            _decode_reconciliation_adjustment_command(
                invalid,
                binding=BINDING,
                spec_set=_spec_set(),
                observation=observation,
                outcome=outcome,
                outcome_acknowledgement=acknowledgement,
                local_snapshot=snapshot,
            )


@pytest.mark.parametrize("decision", tuple(ReconciliationAuthorizationDecision))
def test_authorization_binds_command_outcome_ack_and_audit(
    decision: ReconciliationAuthorizationDecision,
) -> None:
    _, _, outcome, outcome_acknowledgement, command = _balance_command_bundle()
    authorization = _create_reconciliation_adjustment_authorization(
        binding=BINDING,
        spec_set=_spec_set(),
        outcome=outcome,
        outcome_acknowledgement=outcome_acknowledgement,
        command=command,
        authorization_id=EconomicId(
            RUN_ID,
            EconomicOwnerKind.RECONCILIATION_AUTHORIZATION,
            1,
        ),
        policy_id=ReconciliationAuthorizationPolicyId("reconciliation.test-policy.v1"),
        policy_version=1,
        policy_sha256=Sha256Digest("77" * 32),
        decision=decision,
        available_at=TIME,
    )
    payload = canonical_reconciliation_adjustment_authorization_bytes(authorization)

    assert (
        _decode_reconciliation_adjustment_authorization(
            payload,
            binding=BINDING,
            spec_set=_spec_set(),
            outcome=outcome,
            outcome_acknowledgement=outcome_acknowledgement,
            command=command,
        )
        == authorization
    )
    assert len(reconciliation_adjustment_authorization_digest(authorization).value) == 64
    assert (
        require_canonical_audit_payload(
            AuditRecordKind.RECONCILIATION_ADJUSTMENT_AUTHORIZATION,
            payload,
        )
        is payload
    )
    acknowledgement = _authorization_acknowledgement(authorization)
    audited = create_audited_reconciliation_adjustment_authorization(
        authorization,
        acknowledgement,
    )
    assert type(audited) is AuditedReconciliationAdjustmentAuthorization
    assert len(canonical_audited_reconciliation_adjustment_authorization_bytes(audited)) < 4_096
    assert len(audited_reconciliation_adjustment_authorization_digest(audited).value) == 64


def test_authorization_rejects_wrong_identity_ack_and_payload_drift() -> None:
    _, _, outcome, outcome_acknowledgement, command = _balance_command_bundle()
    authorization = _create_reconciliation_adjustment_authorization(
        binding=BINDING,
        spec_set=_spec_set(),
        outcome=outcome,
        outcome_acknowledgement=outcome_acknowledgement,
        command=command,
        authorization_id=EconomicId(
            RUN_ID,
            EconomicOwnerKind.RECONCILIATION_AUTHORIZATION,
            1,
        ),
        policy_id=ReconciliationAuthorizationPolicyId("reconciliation.test-policy.v1"),
        policy_version=1,
        policy_sha256=Sha256Digest("77" * 32),
        decision=ReconciliationAuthorizationDecision.ALLOWED,
        available_at=TIME,
    )
    with pytest.raises(TypeError, match="authority"):
        ReconciliationAdjustmentAuthorization()
    with pytest.raises(TypeError, match="audit"):
        AuditedReconciliationAdjustmentAuthorization()
    with pytest.raises(ReconciliationContractError):
        _create_reconciliation_adjustment_authorization(
            binding=BINDING,
            spec_set=_spec_set(),
            outcome=outcome,
            outcome_acknowledgement=outcome_acknowledgement,
            command=command,
            authorization_id=command.adjustment_id,
            policy_id=ReconciliationAuthorizationPolicyId("reconciliation.test-policy.v1"),
            policy_version=1,
            policy_sha256=Sha256Digest("77" * 32),
            decision=ReconciliationAuthorizationDecision.ALLOWED,
            available_at=TIME,
        )
    with pytest.raises(ReconciliationContractError, match="identity sequence must be positive"):
        _create_reconciliation_adjustment_authorization(
            binding=BINDING,
            spec_set=_spec_set(),
            outcome=outcome,
            outcome_acknowledgement=outcome_acknowledgement,
            command=command,
            authorization_id=EconomicId(
                RUN_ID,
                EconomicOwnerKind.RECONCILIATION_AUTHORIZATION,
                0,
            ),
            policy_id=ReconciliationAuthorizationPolicyId("reconciliation.test-policy.v1"),
            policy_version=1,
            policy_sha256=Sha256Digest("77" * 32),
            decision=ReconciliationAuthorizationDecision.ALLOWED,
            available_at=TIME,
        )
    with pytest.raises(ReconciliationContractError, match="acknowledgement"):
        create_audited_reconciliation_adjustment_authorization(authorization, None)
    with pytest.raises(ReconciliationContractError):
        canonical_reconciliation_adjustment_authorization_bytes(None)  # type: ignore[arg-type]
    with pytest.raises(ReconciliationContractError):
        canonical_audited_reconciliation_adjustment_authorization_bytes(
            None  # type: ignore[arg-type]
        )
    document = json.loads(canonical_reconciliation_adjustment_authorization_bytes(authorization))
    invalid_audit_payloads = (
        _canonical({**document, "decision": "override"}),
        _canonical({**document, "available_at": "2026-1-02T09:31:00.000000Z"}),
    )
    for invalid in invalid_audit_payloads:
        with pytest.raises(AuditContractError):
            require_canonical_audit_payload(
                AuditRecordKind.RECONCILIATION_ADJUSTMENT_AUTHORIZATION,
                invalid,
            )
    assert "_create_reconciliation_adjustment_authorization" not in core.__all__
    assert "_decode_reconciliation_adjustment_authorization" not in core.__all__
    with pytest.raises(ReconciliationContractError):
        _authorization_binding(None)  # type: ignore[arg-type]
    forged = object.__new__(ReconciliationAdjustmentAuthorization)
    object.__setattr__(forged, "_seal", authorization._seal)
    object.__setattr__(forged, "_binding", None)
    with pytest.raises(ReconciliationContractError):
        _authorization_binding(forged)


def test_authorization_decoder_rejects_every_binding_drift() -> None:
    _, _, outcome, outcome_acknowledgement, command = _balance_command_bundle()
    authorization = _create_reconciliation_adjustment_authorization(
        binding=BINDING,
        spec_set=_spec_set(),
        outcome=outcome,
        outcome_acknowledgement=outcome_acknowledgement,
        command=command,
        authorization_id=EconomicId(
            RUN_ID,
            EconomicOwnerKind.RECONCILIATION_AUTHORIZATION,
            1,
        ),
        policy_id=ReconciliationAuthorizationPolicyId("reconciliation.test-policy.v1"),
        policy_version=1,
        policy_sha256=Sha256Digest("77" * 32),
        decision=ReconciliationAuthorizationDecision.ALLOWED,
        available_at=TIME,
    )
    payload = canonical_reconciliation_adjustment_authorization_bytes(authorization)
    document = json.loads(payload)
    invalid_payloads = (
        _canonical({**document, "unexpected": None}),
        _canonical({**document, "schema": "ea.reconciliation-adjustment-authorization.v0"}),
        _canonical({**document, "canonicalization": "unknown"}),
        _canonical(
            {
                **document,
                "authorization_id": {
                    "owner_kind": EconomicOwnerKind.RECONCILIATION_ADJUSTMENT.value,
                    "owner_sequence": 1,
                    "run_id": RUN_ID.value,
                },
            }
        ),
        _canonical(
            {
                **document,
                "adjustment_id": {
                    "owner_kind": EconomicOwnerKind.RECONCILIATION_ADJUSTMENT.value,
                    "owner_sequence": 99,
                    "run_id": RUN_ID.value,
                },
            }
        ),
        _canonical({**document, "instrument_spec_set_id": "different.v1"}),
        _canonical({**document, "instrument_spec_set_sha256": "10" * 32}),
        _canonical({**document, "dispatch_sequence": 8}),
        _canonical({**document, "ledger_sequence": 99}),
        _canonical({**document, "local_snapshot_sha256": "20" * 32}),
        _canonical({**document, "observation_sha256": "30" * 32}),
        _canonical({**document, "reconciliation_outcome_sha256": "40" * 32}),
        _canonical({**document, "command_sha256": "50" * 32}),
        _canonical({**document, "outcome_acknowledgement_sha256": "60" * 32}),
        _canonical({**document, "policy_id": "INVALID POLICY"}),
        _canonical({**document, "policy_version": 0}),
        _canonical({**document, "policy_sha256": "invalid"}),
        _canonical({**document, "decision": "override"}),
        _canonical({**document, "available_at": "2026-01-02T09:31:00Z"}),
        _canonical({**document, "run_id": "87654321-4321-4321-8321-cba987654321"}),
        payload + b" ",
    )

    for invalid in invalid_payloads:
        with pytest.raises(ReconciliationContractError):
            _decode_reconciliation_adjustment_authorization(
                invalid,
                binding=BINDING,
                spec_set=_spec_set(),
                outcome=outcome,
                outcome_acknowledgement=outcome_acknowledgement,
                command=command,
            )

    with pytest.raises(ReconciliationContractError):
        _create_reconciliation_adjustment_authorization(
            binding=BINDING,
            spec_set=_spec_set(),
            outcome=outcome,
            outcome_acknowledgement=outcome_acknowledgement,
            command=command,
            authorization_id=authorization.authorization_id,
            policy_id="reconciliation.test-policy.v1",  # type: ignore[arg-type]
            policy_version=1,
            policy_sha256=Sha256Digest("77" * 32),
            decision=ReconciliationAuthorizationDecision.ALLOWED,
            available_at=TIME,
        )
