from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from ea.core import (
    AuditContractError,
    AuditRecordKind,
    CanonicalDecimal,
    OutcomeCode,
    ReconciliationContractError,
    ReconciliationOutcome,
    ReconciliationRequestedAction,
    ReconciliationWatermarkComparison,
    Sha256Digest,
    audit_subject_digest,
    canonical_reconciliation_outcome_bytes,
    create_cash_reconciliation_discrepancy,
    create_position_reconciliation_discrepancy,
    create_reconciliation_outcome,
    decode_reconciliation_outcome,
    reconciliation_outcome_digest,
)
from ea.core.audit import require_canonical_audit_payload
from unit.test_portfolio_ledger import INSTRUMENT, RUN_ID, USD, _spec_set

OBSERVATION_SHA256 = Sha256Digest("11" * 32)
SNAPSHOT_SHA256 = Sha256Digest("22" * 32)


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
