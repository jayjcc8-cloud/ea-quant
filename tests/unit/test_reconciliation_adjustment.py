from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

import ea.core as core
from ea.core import (
    AuditContractError,
    AuditRecordKind,
    CanonicalDecimal,
    CurrencyCommodity,
    EconomicId,
    EconomicOwnerKind,
    InstrumentCommodity,
    LedgerAccountKind,
    LedgerPosting,
    OutcomeCode,
    ReconciliationAdjustmentConflictKind,
    ReconciliationAdjustmentFailureKind,
    ReconciliationAdjustmentOutcome,
    ReconciliationAdjustmentResult,
    ReconciliationAdjustmentTargetKind,
    ReconciliationAdjustmentVariant,
    ReconciliationContractError,
    ReconciliationTransaction,
    Sha256Digest,
    audit_subject_digest,
    canonical_reconciliation_adjustment_outcome_bytes,
    canonical_reconciliation_transaction_bytes,
    decode_reconciliation_adjustment_outcome,
    reconciliation_adjustment_outcome_digest,
    reconciliation_transaction_digest,
)
from ea.core.audit import require_canonical_audit_payload
from ea.core.reconciliation import (
    _create_reconciliation_adjustment_outcome,
    _create_reconciliation_transaction,
)
from unit.test_portfolio_ledger import INSTRUMENT, RUN_ID, USD, _spec_set

TIME = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
ADJUSTMENT_ID = EconomicId(RUN_ID, EconomicOwnerKind.RECONCILIATION_ADJUSTMENT, 1)
DIGESTS = tuple(Sha256Digest(f"{index:064x}") for index in range(1, 10))


def _canonical(document: object) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _position_postings(delta: str = "2") -> tuple[LedgerPosting, ...]:
    return (
        LedgerPosting(
            LedgerAccountKind.PORTFOLIO_POSITION,
            InstrumentCommodity(INSTRUMENT),
            CanonicalDecimal(delta),
        ),
        LedgerPosting(
            LedgerAccountKind.EXTERNAL_INVENTORY,
            InstrumentCommodity(INSTRUMENT),
            CanonicalDecimal(f"-{delta}"),
        ),
    )


def _transaction(**changes: object) -> ReconciliationTransaction:
    arguments: dict[str, object] = {
        "run_id": RUN_ID,
        "spec_set": _spec_set(),
        "entry_id": EconomicId(RUN_ID, EconomicOwnerKind.LEDGER_ENTRY, 1),
        "ledger_sequence": 1,
        "adjustment_id": ADJUSTMENT_ID,
        "authorization_sha256": DIGESTS[0],
        "observation_sha256": DIGESTS[1],
        "reconciliation_outcome_sha256": DIGESTS[2],
        "adjustment_command_sha256": DIGESTS[3],
        "variant": ReconciliationAdjustmentVariant.BALANCE_CORRECTION,
        "target_kind": ReconciliationAdjustmentTargetKind.INSTRUMENT_POSITION,
        "instrument": INSTRUMENT,
        "currency": None,
        "local_amount": CanonicalDecimal("10"),
        "observed_amount": CanonicalDecimal("12"),
        "delta": CanonicalDecimal("2"),
        "open_reconciliation_ref": None,
        "previous_transaction_sha256": None,
        "postings": _position_postings(),
        "occurred_at": TIME,
        "available_at": TIME,
    }
    arguments.update(changes)
    return _create_reconciliation_transaction(**arguments)  # type: ignore[arg-type]


def _outcome(**changes: object) -> ReconciliationAdjustmentOutcome:
    arguments: dict[str, object] = {
        "run_id": RUN_ID,
        "adjustment_id": ADJUSTMENT_ID,
        "authorization_sha256": DIGESTS[0],
        "observation_sha256": DIGESTS[1],
        "reconciliation_outcome_sha256": DIGESTS[2],
        "adjustment_command_sha256": DIGESTS[3],
        "result": ReconciliationAdjustmentResult.FAILED,
        "transaction": None,
        "failure_kind": ReconciliationAdjustmentFailureKind.STALE_FRONTIER,
        "conflict_kind": None,
        "before_snapshot_sha256": DIGESTS[4],
        "after_snapshot_sha256": DIGESTS[4],
    }
    arguments.update(changes)
    return _create_reconciliation_adjustment_outcome(**arguments)  # type: ignore[arg-type]


def test_balance_correction_transaction_derives_canonical_document_and_digest() -> None:
    transaction = _transaction()

    assert transaction.entry_id.owner_sequence == 1
    assert transaction.previous_transaction_sha256 is None
    assert transaction.postings == _position_postings()
    assert canonical_reconciliation_transaction_bytes(transaction).startswith(b"{")
    assert len(reconciliation_transaction_digest(transaction).value) == 64
    document = json.loads(canonical_reconciliation_transaction_bytes(transaction))
    assert document["schema"] == "ea.reconciliation-transaction.v1"
    assert document["variant"] == "balance_correction"
    assert document["target_kind"] == "instrument_position"
    assert document["delta"] == "2"
    assert document["postings"][0]["account"] == "portfolio.position"
    assert document["postings"][1]["account"] == "external.inventory"
    with pytest.raises(TypeError, match="ledger factory"):
        ReconciliationTransaction()


def test_cash_correction_derives_opposite_settlement_postings() -> None:
    transaction = _transaction(
        target_kind=ReconciliationAdjustmentTargetKind.SETTLEMENT_CASH,
        instrument=None,
        currency=USD,
        local_amount=CanonicalDecimal("125.5"),
        observed_amount=CanonicalDecimal("120"),
        delta=CanonicalDecimal("-5.5"),
        postings=(
            LedgerPosting(
                LedgerAccountKind.PORTFOLIO_CASH,
                CurrencyCommodity(USD),
                CanonicalDecimal("-5.5"),
            ),
            LedgerPosting(
                LedgerAccountKind.EXTERNAL_SETTLEMENT,
                CurrencyCommodity(USD),
                CanonicalDecimal("5.5"),
            ),
        ),
    )

    document = json.loads(canonical_reconciliation_transaction_bytes(transaction))
    assert document["currency"] == "USD"
    assert document["postings"][0]["account"] == "portfolio.cash"
    assert document["postings"][1]["account"] == "external.settlement"


@pytest.mark.parametrize(
    "changes",
    [
        {"delta": CanonicalDecimal("0")},
        {"delta": CanonicalDecimal("3")},
        {"local_amount": CanonicalDecimal("0.5")},
        {"observed_amount": CanonicalDecimal("12.5")},
        {"currency": USD},
        {"instrument": None},
        {"target_kind": ReconciliationAdjustmentTargetKind.OPEN_RECONCILIATION_REF},
        {"ledger_sequence": 2, "entry_id": EconomicId(RUN_ID, EconomicOwnerKind.LEDGER_ENTRY, 3)},
        {
            "ledger_sequence": 1,
            "previous_transaction_sha256": DIGESTS[5],
        },
        {"occurred_at": TIME, "available_at": datetime(2026, 1, 2, 9, 30, tzinfo=UTC)},
    ],
)
def test_balance_correction_rejects_conflicting_evidence(changes: dict[str, object]) -> None:
    with pytest.raises(ReconciliationContractError):
        _transaction(**changes)


def test_balance_correction_rejects_derived_posting_drift() -> None:
    with pytest.raises(ReconciliationContractError):
        _transaction(
            postings=(
                LedgerPosting(
                    LedgerAccountKind.PORTFOLIO_POSITION,
                    InstrumentCommodity(INSTRUMENT),
                    CanonicalDecimal("2"),
                ),
                LedgerPosting(
                    LedgerAccountKind.EXTERNAL_INVENTORY,
                    InstrumentCommodity(INSTRUMENT),
                    CanonicalDecimal("-3"),
                ),
            )
        )
    with pytest.raises(ReconciliationContractError):
        _transaction(
            postings=(
                LedgerPosting(
                    LedgerAccountKind.PORTFOLIO_POSITION,
                    InstrumentCommodity(INSTRUMENT),
                    CanonicalDecimal("2"),
                ),
                LedgerPosting(
                    LedgerAccountKind.PORTFOLIO_CASH,
                    CurrencyCommodity(USD),
                    CanonicalDecimal("-2"),
                ),
            )
        )


def test_ancestry_resolution_has_zero_postings_and_removes_only_named_reference() -> None:
    from ea.core import OpenReconciliationRef

    reference = OpenReconciliationRef(
        EconomicId(RUN_ID, EconomicOwnerKind.EXECUTION_FILL, 9),
        Sha256Digest("55" * 32),
        Sha256Digest("66" * 32),
    )
    transaction = _transaction(
        variant=ReconciliationAdjustmentVariant.ANCESTRY_RESOLUTION,
        target_kind=ReconciliationAdjustmentTargetKind.OPEN_RECONCILIATION_REF,
        instrument=None,
        local_amount=None,
        observed_amount=None,
        delta=None,
        open_reconciliation_ref=reference,
        postings=(),
    )

    document = json.loads(canonical_reconciliation_transaction_bytes(transaction))
    assert document["postings"] == []
    assert document["open_reconciliation_ref"]["fill_id"]["owner_sequence"] == 9
    assert document["delta"] is None

    with pytest.raises(ReconciliationContractError):
        _transaction(
            variant=ReconciliationAdjustmentVariant.ANCESTRY_RESOLUTION,
            target_kind=ReconciliationAdjustmentTargetKind.OPEN_RECONCILIATION_REF,
            instrument=None,
            local_amount=None,
            observed_amount=None,
            delta=None,
            open_reconciliation_ref=None,
            postings=(),
        )


def test_applied_outcome_binds_transaction_and_advancing_snapshots() -> None:
    transaction = _transaction()
    outcome = _create_reconciliation_adjustment_outcome(
        run_id=RUN_ID,
        adjustment_id=ADJUSTMENT_ID,
        authorization_sha256=DIGESTS[0],
        observation_sha256=DIGESTS[1],
        reconciliation_outcome_sha256=DIGESTS[2],
        adjustment_command_sha256=DIGESTS[3],
        result=ReconciliationAdjustmentResult.APPLIED,
        transaction=transaction,
        failure_kind=None,
        conflict_kind=None,
        before_snapshot_sha256=DIGESTS[4],
        after_snapshot_sha256=DIGESTS[5],
    )

    assert outcome.original_transaction_id == transaction.entry_id
    assert outcome.original_transaction_sha256 == reconciliation_transaction_digest(transaction)
    decoded = decode_reconciliation_adjustment_outcome(
        canonical_reconciliation_adjustment_outcome_bytes(outcome)
    )
    assert decoded == outcome
    assert len(reconciliation_adjustment_outcome_digest(outcome).value) == 64


@pytest.mark.parametrize(
    ("conflict_kind",),
    [
        (ReconciliationAdjustmentConflictKind.AUTHORIZATION_ID_COLLISION,),
        (ReconciliationAdjustmentConflictKind.ADJUSTMENT_ID_COLLISION,),
        (ReconciliationAdjustmentConflictKind.OBSERVATION_ALREADY_CONSUMED,),
        (ReconciliationAdjustmentConflictKind.INDEX_INCONSISTENT,),
    ],
)
def test_conflict_outcome_matrix_is_closed(
    conflict_kind: ReconciliationAdjustmentConflictKind,
) -> None:
    outcome = _outcome(
        result=ReconciliationAdjustmentResult.CONFLICT,
        failure_kind=None,
        conflict_kind=conflict_kind,
    )
    assert (
        decode_reconciliation_adjustment_outcome(
            canonical_reconciliation_adjustment_outcome_bytes(outcome)
        )
        == outcome
    )


@pytest.mark.parametrize(
    ("failure_kind",),
    [
        (ReconciliationAdjustmentFailureKind.INVALID_COMMAND,),
        (ReconciliationAdjustmentFailureKind.STALE_FRONTIER,),
        (ReconciliationAdjustmentFailureKind.ARITHMETIC_FAILURE,),
        (ReconciliationAdjustmentFailureKind.UNBALANCED,),
    ],
)
def test_failed_outcome_matrix_is_closed(
    failure_kind: ReconciliationAdjustmentFailureKind,
) -> None:
    outcome = _outcome(failure_kind=failure_kind)
    assert outcome.original_transaction_id is None
    assert (
        decode_reconciliation_adjustment_outcome(
            canonical_reconciliation_adjustment_outcome_bytes(outcome)
        )
        == outcome
    )


def test_duplicate_outcome_requires_original_evidence_and_equal_snapshots() -> None:
    transaction = _transaction()
    outcome = _create_reconciliation_adjustment_outcome(
        run_id=RUN_ID,
        adjustment_id=ADJUSTMENT_ID,
        authorization_sha256=DIGESTS[0],
        observation_sha256=DIGESTS[1],
        reconciliation_outcome_sha256=DIGESTS[2],
        adjustment_command_sha256=DIGESTS[3],
        result=ReconciliationAdjustmentResult.DUPLICATE,
        transaction=transaction,
        failure_kind=None,
        conflict_kind=None,
        before_snapshot_sha256=DIGESTS[4],
        after_snapshot_sha256=DIGESTS[4],
    )
    assert outcome.before_snapshot_sha256 == outcome.after_snapshot_sha256

    # Decoder path: explicit original evidence without the live transaction.
    decoded_form = _create_reconciliation_adjustment_outcome(
        run_id=RUN_ID,
        adjustment_id=ADJUSTMENT_ID,
        authorization_sha256=DIGESTS[0],
        observation_sha256=DIGESTS[1],
        reconciliation_outcome_sha256=DIGESTS[2],
        adjustment_command_sha256=DIGESTS[3],
        result=ReconciliationAdjustmentResult.DUPLICATE,
        transaction=None,
        failure_kind=None,
        conflict_kind=None,
        before_snapshot_sha256=DIGESTS[4],
        after_snapshot_sha256=DIGESTS[4],
        original_transaction_id=transaction.entry_id,
        original_transaction_sha256=reconciliation_transaction_digest(transaction),
    )
    assert decoded_form.original_transaction_id == transaction.entry_id
    assert (
        decode_reconciliation_adjustment_outcome(
            canonical_reconciliation_adjustment_outcome_bytes(decoded_form)
        )
        == decoded_form
    )

    with pytest.raises(ReconciliationContractError):
        _create_reconciliation_adjustment_outcome(
            run_id=RUN_ID,
            adjustment_id=ADJUSTMENT_ID,
            authorization_sha256=DIGESTS[0],
            observation_sha256=DIGESTS[1],
            reconciliation_outcome_sha256=DIGESTS[2],
            adjustment_command_sha256=DIGESTS[3],
            result=ReconciliationAdjustmentResult.DUPLICATE,
            transaction=transaction,
            failure_kind=None,
            conflict_kind=None,
            before_snapshot_sha256=DIGESTS[4],
            after_snapshot_sha256=DIGESTS[4],
            original_transaction_id=EconomicId(RUN_ID, EconomicOwnerKind.LEDGER_ENTRY, 2),
            original_transaction_sha256=reconciliation_transaction_digest(transaction),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"result": ReconciliationAdjustmentResult.APPLIED},
        {"failure_kind": None},
        {"conflict_kind": ReconciliationAdjustmentConflictKind.INDEX_INCONSISTENT},
        {"before_snapshot_sha256": DIGESTS[5]},
    ],
)
def test_outcome_rejects_unsupported_combinations(changes: dict[str, object]) -> None:
    with pytest.raises(ReconciliationContractError):
        _outcome(**changes)


def test_outcome_decoder_rejects_noncanonical_and_tampered_payloads() -> None:
    outcome = _outcome()
    payload = canonical_reconciliation_adjustment_outcome_bytes(outcome)
    document = json.loads(payload)
    invalid_payloads = (
        payload + b" ",
        _canonical({**document, "schema": "ea.reconciliation-adjustment-outcome.v0"}),
        _canonical({**document, "result": "applied"}),
        _canonical({**document, "failure_kind": "unknown"}),
        _canonical({**document, "extra": None}),
        _canonical(
            {
                **document,
                "original_transaction_id": {
                    "owner_kind": "reconciliation.adjustment",
                    "owner_sequence": 1,
                    "run_id": RUN_ID.value,
                },
            }
        ),
    )
    for invalid in invalid_payloads:
        with pytest.raises(ReconciliationContractError):
            decode_reconciliation_adjustment_outcome(invalid)

    oversized = b'{"padding":"' + (b"a" * 16_384) + b'"}'
    with pytest.raises(ReconciliationContractError) as error:
        decode_reconciliation_adjustment_outcome(oversized)
    assert error.value.code is OutcomeCode.OUT_OF_RANGE


def test_adjustment_outcome_audit_gate_reuses_strict_codec() -> None:
    outcome = _outcome()
    payload = canonical_reconciliation_adjustment_outcome_bytes(outcome)

    assert (
        require_canonical_audit_payload(
            AuditRecordKind.RECONCILIATION_ADJUSTMENT_OUTCOME,
            payload,
        )
        is payload
    )
    subject = audit_subject_digest(
        AuditRecordKind.RECONCILIATION_ADJUSTMENT_OUTCOME,
        payload,
    )
    assert len(subject.value) == 64

    document = json.loads(payload)
    for tampered in (
        _canonical({**document, "failure_kind": "unknown"}),
        _canonical({**document, "result": "applied"}),
    ):
        with pytest.raises(AuditContractError):
            require_canonical_audit_payload(
                AuditRecordKind.RECONCILIATION_ADJUSTMENT_OUTCOME,
                tampered,
            )


def test_adjustment_contracts_keep_private_factories_confined() -> None:
    assert "_create_reconciliation_transaction" not in core.__all__
    assert "_create_reconciliation_adjustment_outcome" not in core.__all__
    assert "ReconciliationTransaction" in core.__all__
    assert "ReconciliationAdjustmentOutcome" in core.__all__
    with pytest.raises(TypeError):
        ReconciliationAdjustmentOutcome()
