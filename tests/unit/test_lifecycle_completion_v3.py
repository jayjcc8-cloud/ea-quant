from __future__ import annotations

import json

import pytest

from ea.core import (
    EMPTY_CHAIN_HEAD_SHA256,
    EMPTY_RECORD_SHA256,
    AuditAppendAcknowledgement,
    AuditRecordKind,
    AuditSubjectKind,
    OpenReconciliationRef,
    PortfolioSnapshot,
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


def test_terminal_v2_binds_the_final_publication_frontier() -> None:
    from ea.core import CoordinatorTerminalKind, RunBinding, RunId, RunReference
    from ea.core.lifecycle import (
        canonical_run_terminal_v2_audit_payload,
        create_pre_terminal_coordinator_state,
    )

    state = create_pre_terminal_coordinator_state(
        binding=RunBinding(
            RunReference(RunId("12345678-1234-4234-8234-123456789abc"), Sha256Digest("11" * 32)),
            Sha256Digest("22" * 32),
        ),
        state_version=4,
        terminal_kind=CoordinatorTerminalKind.SUCCESS,
        last_dispatch_sequence=3,
        last_trigger_root_sha256=Sha256Digest("33" * 32),
        dispatch_completion_ack_sha256=Sha256Digest("44" * 32),
        previous_chain_head_sha256=Sha256Digest("44" * 32),
        failure_code=None,
    )
    payload = canonical_run_terminal_v2_audit_payload(
        state,
        final_published_snapshot_sha256=Sha256Digest("77" * 32),
        final_risk_refresh_sha256=Sha256Digest("88" * 32),
        open_reconciliation_ref_aggregate_sha256=Sha256Digest("99" * 32),
        ordered_reconciliation_frontier_sha256s_sha256=Sha256Digest("aa" * 32),
    )
    from ea.core.audit import require_canonical_audit_payload as gate

    assert gate(AuditRecordKind.RUN_TERMINAL, payload) is payload
    document = json.loads(payload)
    assert document["schema"] == "ea.audit-run-terminal.v2"
    assert document["final_risk_refresh_sha256"] == "88" * 32


def test_open_reconciliation_aggregate_is_ordered_and_deterministic() -> None:
    from ea.core import (
        EconomicId,
        EconomicOwnerKind,
        OpenReconciliationRef,
        RunId,
        open_reconciliation_aggregate_digest,
    )

    run_id = RunId("12345678-1234-4234-8234-123456789abc")
    first = OpenReconciliationRef(
        EconomicId(run_id, EconomicOwnerKind.EXECUTION_FILL, 1),
        Sha256Digest("11" * 32),
        Sha256Digest("22" * 32),
    )
    second = OpenReconciliationRef(
        EconomicId(run_id, EconomicOwnerKind.EXECUTION_FILL, 2),
        Sha256Digest("33" * 32),
        Sha256Digest("44" * 32),
    )
    snapshot_one = _snapshot_with_refs((first, second))
    snapshot_same = _snapshot_with_refs((first, second))
    snapshot_single = _snapshot_with_refs((first,))

    assert len(open_reconciliation_aggregate_digest(snapshot_one).value) == 64
    assert open_reconciliation_aggregate_digest(snapshot_one) == (
        open_reconciliation_aggregate_digest(snapshot_same)
    )
    assert open_reconciliation_aggregate_digest(snapshot_one) != (
        open_reconciliation_aggregate_digest(snapshot_single)
    )
    empty = _snapshot_with_refs(())
    assert len(open_reconciliation_aggregate_digest(empty).value) == 64


def _snapshot_with_refs(
    refs: tuple[OpenReconciliationRef, ...],
) -> PortfolioSnapshot:
    from ea.core import (
        EconomicId,
        EconomicOwnerKind,
        ExistingLedgerBinding,
        PortfolioSnapshot,
    )
    from unit.test_portfolio_ledger import RUN_ID as FIXTURE_RUN
    from unit.test_portfolio_ledger import _spec_set

    spec_set = _spec_set()
    bindings = tuple(
        ExistingLedgerBinding(
            EconomicId(FIXTURE_RUN, EconomicOwnerKind.LEDGER_ENTRY, index + 1),
            reference.fill_id,
            reference.fill_sha256,
            Sha256Digest(f"{index + 1:064x}"),
        )
        for index, reference in enumerate(refs)
    )
    return PortfolioSnapshot(
        run_id=FIXTURE_RUN,
        instrument_spec_set_id=spec_set.identifier,
        instrument_spec_set_sha256=__import__(
            "ea.core", fromlist=["instrument_spec_set_digest"]
        ).instrument_spec_set_digest(spec_set),
        snapshot_version=len(refs),
        ledger_sequence=len(refs),
        last_entry_id=(
            None
            if not bindings
            else EconomicId(FIXTURE_RUN, EconomicOwnerKind.LEDGER_ENTRY, len(bindings))
        ),
        last_transaction_sha256=(None if not bindings else bindings[-1].transaction_sha256),
        cash_balances=(),
        position_balances=(),
        rounding_balances=(),
        unresolved_fills=(),
        open_reconciliation_bindings=bindings,
        open_reconciliation_refs=refs,
    )
