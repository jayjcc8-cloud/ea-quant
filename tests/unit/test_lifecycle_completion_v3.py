from __future__ import annotations

import json

import pytest

from ea.core import (
    EMPTY_CHAIN_HEAD_SHA256,
    EMPTY_RECORD_SHA256,
    AuditAppendAcknowledgement,
    AuditRecordKind,
    AuditSubjectKind,
    RunBinding,
    RunReference,
    Sha256Digest,
    audit_subject_digest,
    canonical_dispatch_completed_audit_payload,
    canonical_dispatch_completed_v3_audit_payload,
    create_audit_append_acknowledgement,
    create_audit_record,
)
from unit.test_historical_matcher import _system

DIGESTS = tuple(Sha256Digest(f"{index:064x}") for index in range(1, 8))


def _v3_document(*, ledger_acks: tuple[str, ...] = ()) -> tuple[object, dict[str, object]]:
    _fixture, matcher, _orders, causal, delayed, _end = _system()
    batch = matcher.match_active_market_root(delayed, dispatch_sequence=1)
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    payload = canonical_dispatch_completed_v3_audit_payload(
        binding=binding,
        batch=batch,
        outcome_acknowledgements=(),
        pre_ack_state_sha256=DIGESTS[0],
        ledger_outcome_acknowledgements=tuple(
            _acknowledgement(binding, value, 2 + index) for index, value in enumerate(ledger_acks)
        ),
        final_portfolio_snapshot_sha256=DIGESTS[1],
        final_risk_state_sha256=DIGESTS[2],
    )
    return binding, json.loads(payload)


def _acknowledgement(
    binding: RunBinding, subject: str, sequence: int
) -> AuditAppendAcknowledgement:
    import json as _json

    from unit.test_audit_journal import _batch_payload

    document = _json.loads(_batch_payload())
    document["run_id"] = binding.reference.run_id.value
    document["batch_sha256"] = subject
    payload = _json.dumps(
        document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()
    subject_sha256 = audit_subject_digest(AuditRecordKind.MATCHER_DISPATCH_BATCH, payload)
    record = create_audit_record(
        binding=binding,
        owner_sequence=sequence,
        record_kind=AuditRecordKind.MATCHER_DISPATCH_BATCH,
        subject_kind=AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
        subject_sha256=subject_sha256,
        canonical_payload=payload,
        previous_record_sha256=EMPTY_RECORD_SHA256,
        previous_chain_head_sha256=EMPTY_CHAIN_HEAD_SHA256,
    )
    return create_audit_append_acknowledgement(record)


def test_completion_v3_binds_ledger_frontier_and_final_digests() -> None:
    _, document = _v3_document()

    assert document["schema"] == "ea.audit-dispatch-completed.v3"
    assert document["ledger_outcome_count"] == 0
    assert document["final_portfolio_snapshot_sha256"] == DIGESTS[1].value
    assert document["final_risk_state_sha256"] == DIGESTS[2].value
    assert document["outcome_count"] == 0
    assert set(document) == {
        "authorization_attempt_count",
        "authorization_attempt_outcome",
        "authorization_attempt_outcome_sha256",
        "batch_sha256",
        "canonicalization",
        "dispatch_kind",
        "dispatch_sequence",
        "final_portfolio_snapshot_sha256",
        "final_risk_state_sha256",
        "ledger_outcome_count",
        "ordered_ledger_ack_sha256s_sha256",
        "ordered_outcome_ack_sha256s_sha256",
        "ordered_submission_receipt_sha256s_sha256",
        "outcome_count",
        "pre_ack_state_sha256",
        "run_id",
        "schema",
        "submission_count",
        "trigger_root_key",
        "trigger_root_sha256",
    }


def test_completion_v3_ledger_ack_aggregate_is_ordered_and_deterministic() -> None:
    _, first = _v3_document(ledger_acks=("aa" * 32, "bb" * 32, "cc" * 32))
    _, second = _v3_document(ledger_acks=("aa" * 32, "bb" * 32, "cc" * 32))
    _, reordered = _v3_document(ledger_acks=("cc" * 32, "bb" * 32, "aa" * 32))

    assert first["ledger_outcome_count"] == 3
    assert first["ordered_ledger_ack_sha256s_sha256"] == second["ordered_ledger_ack_sha256s_sha256"]
    assert (
        first["ordered_ledger_ack_sha256s_sha256"] != reordered["ordered_ledger_ack_sha256s_sha256"]
    )


def test_completion_v3_revalidates_all_shared_v2_evidence() -> None:
    _fixture, matcher, _orders, _causal, delayed, _end = _system()
    batch = matcher.match_active_market_root(delayed, dispatch_sequence=1)
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )

    with pytest.raises(Exception, match="must be exact"):
        canonical_dispatch_completed_v3_audit_payload(
            binding=binding,
            batch=batch,
            outcome_acknowledgements=(),
            pre_ack_state_sha256=DIGESTS[0],
            ledger_outcome_acknowledgements=(),
            final_portfolio_snapshot_sha256=Sha256Digest("11" * 32),
            final_risk_state_sha256="not-a-digest",  # type: ignore[arg-type]
        )

    with pytest.raises(Exception, match="invalid"):
        canonical_dispatch_completed_v3_audit_payload(
            binding=binding,
            batch=batch,
            outcome_acknowledgements=(),
            pre_ack_state_sha256=DIGESTS[0],
            ledger_outcome_acknowledgements=(object(),),  # type: ignore[arg-type]
            final_portfolio_snapshot_sha256=DIGESTS[1],
            final_risk_state_sha256=DIGESTS[2],
        )


def test_completion_v2_and_v3_disagree_so_old_records_never_prove_frontiers() -> None:
    _fixture, matcher, _orders, _causal, delayed, _end = _system()
    batch = matcher.match_active_market_root(delayed, dispatch_sequence=1)
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    v2 = json.loads(
        canonical_dispatch_completed_audit_payload(
            binding=binding,
            batch=batch,
            outcome_acknowledgements=(),
            pre_ack_state_sha256=DIGESTS[0],
        )
    )
    v3 = json.loads(
        canonical_dispatch_completed_v3_audit_payload(
            binding=binding,
            batch=batch,
            outcome_acknowledgements=(),
            pre_ack_state_sha256=DIGESTS[0],
            ledger_outcome_acknowledgements=(),
            final_portfolio_snapshot_sha256=DIGESTS[1],
            final_risk_state_sha256=DIGESTS[2],
        )
    )

    assert v2["schema"] == "ea.audit-dispatch-completed.v2"
    assert v3["schema"] == "ea.audit-dispatch-completed.v3"
    assert "ledger_outcome_count" not in v2
    assert v3["ledger_outcome_count"] == 0
