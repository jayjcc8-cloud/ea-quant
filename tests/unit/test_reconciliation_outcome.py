from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

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
    EconomicId,
    EconomicOwnerKind,
    OpenReconciliationRef,
    OutcomeCode,
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
    audit_subject_digest,
    audited_reconciliation_adjustment_authorization_digest,
    canonical_audited_reconciliation_adjustment_authorization_bytes,
    canonical_reconciliation_adjustment_authorization_bytes,
    canonical_reconciliation_adjustment_command_bytes,
    canonical_reconciliation_outcome_bytes,
    create_audit_append_acknowledgement,
    create_audit_record,
    create_audited_reconciliation_adjustment_authorization,
    create_cash_reconciliation_discrepancy,
    create_position_reconciliation_discrepancy,
    create_reconciliation_observation,
    create_reconciliation_outcome,
    decode_reconciliation_outcome,
    reconciliation_adjustment_authorization_digest,
    reconciliation_adjustment_command_digest,
    reconciliation_observation_digest,
    reconciliation_outcome_digest,
)
from ea.core.audit import require_canonical_audit_payload
from ea.core.reconciliation import (
    _create_reconciliation_adjustment_authorization,
    _create_reconciliation_adjustment_command,
    _decode_reconciliation_adjustment_authorization,
    _decode_reconciliation_adjustment_command,
)
from unit.test_execution_messages import SPEC_SET, _order
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
    ReconciliationObservation,
    ReconciliationOutcome,
    AuditAppendAcknowledgement,
    ReconciliationAdjustmentCommand,
]:
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
        adjustment_id=EconomicId(
            RUN_ID,
            EconomicOwnerKind.RECONCILIATION_ADJUSTMENT,
            1,
        ),
    )
    return observation, outcome, acknowledgement, command


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
        adjustment_id=adjustment_id,
    )
    encoded = canonical_reconciliation_adjustment_command_bytes(command)

    assert _decode_reconciliation_adjustment_command(encoded, _spec_set(), outcome) == command
    assert len(reconciliation_adjustment_command_digest(command).value) == 64
    assert (
        canonical_reconciliation_adjustment_command_bytes(
            _create_reconciliation_adjustment_command(
                binding=BINDING,
                spec_set=_spec_set(),
                observation=observation,
                outcome=outcome,
                outcome_acknowledgement=acknowledgement,
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
            adjustment_id=adjustment_id,
        )
    document = json.loads(encoded)
    with pytest.raises(ReconciliationContractError):
        _decode_reconciliation_adjustment_command(
            _canonical({**document, "local_snapshot_sha256": "ff" * 32}),
            _spec_set(),
            outcome,
        )


def test_ancestry_command_binds_exact_open_reference_and_order() -> None:
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
        watermark_sequence=base_observation.watermark_sequence,
        declared_scope_kind=base_observation.declared_scope_kind,
        declared_scope_id=base_observation.declared_scope_id,
        provenance_id=base_observation.provenance_id,
        provenance_payload_sha256=base_observation.provenance_payload_sha256,
        balances=(),
    )
    outcome = _outcome(
        observation_sha256=reconciliation_observation_digest(observation),
        requested_action=ReconciliationRequestedAction.PROPOSE_ANCESTRY_RESOLUTION,
        halt_requested=True,
    )
    reference = OpenReconciliationRef(
        EconomicId(RUN_ID, EconomicOwnerKind.EXECUTION_FILL, 9),
        Sha256Digest("55" * 32),
        Sha256Digest("66" * 32),
    )

    command = _create_reconciliation_adjustment_command(
        binding=BINDING,
        spec_set=SPEC_SET,
        observation=observation,
        outcome=outcome,
        outcome_acknowledgement=_outcome_acknowledgement(outcome),
        adjustment_id=EconomicId(
            RUN_ID,
            EconomicOwnerKind.RECONCILIATION_ADJUSTMENT,
            2,
        ),
        open_reconciliation_ref=reference,
        ancestry_order=_order(),
    )

    document = json.loads(canonical_reconciliation_adjustment_command_bytes(command))
    assert document["variant"] == "ancestry_resolution"
    assert document["target"]["kind"] == "open_reconciliation_ref"
    assert (
        _decode_reconciliation_adjustment_command(
            canonical_reconciliation_adjustment_command_bytes(command),
            SPEC_SET,
            outcome,
        )
        == command
    )


@pytest.mark.parametrize("decision", tuple(ReconciliationAuthorizationDecision))
def test_authorization_binds_command_outcome_ack_and_audit(
    decision: ReconciliationAuthorizationDecision,
) -> None:
    _, outcome, outcome_acknowledgement, command = _balance_command_bundle()
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
    _, outcome, outcome_acknowledgement, command = _balance_command_bundle()
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
    with pytest.raises(ReconciliationContractError, match="acknowledgement"):
        create_audited_reconciliation_adjustment_authorization(authorization, None)
    document = json.loads(canonical_reconciliation_adjustment_authorization_bytes(authorization))
    with pytest.raises(AuditContractError):
        require_canonical_audit_payload(
            AuditRecordKind.RECONCILIATION_ADJUSTMENT_AUTHORIZATION,
            _canonical({**document, "decision": "override"}),
        )
    assert "_create_reconciliation_adjustment_authorization" not in core.__all__
    assert "_decode_reconciliation_adjustment_authorization" not in core.__all__
