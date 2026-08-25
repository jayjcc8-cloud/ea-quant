from __future__ import annotations

import json
from types import SimpleNamespace

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
    canonical_ledger_handoff_outcome_bytes,
    create_audit_append_acknowledgement,
    create_audit_record,
)
from unit.test_historical_matcher import _system

DIGESTS = tuple(Sha256Digest(f"{index:064x}") for index in range(1, 8))


def _v3_document(*, ledger_acks: tuple[str, ...] = ()) -> tuple[RunBinding, dict[str, object]]:
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
    from unit.test_ledger_integration_core import _not_applicable

    document = json.loads(canonical_ledger_handoff_outcome_bytes(_not_applicable()))
    document["run_id"] = binding.reference.run_id.value
    document["audited_handoff_sha256"] = subject
    payload = json.dumps(
        document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()
    subject_sha256 = audit_subject_digest(AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME, payload)
    record = create_audit_record(
        binding=binding,
        owner_sequence=sequence,
        record_kind=AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
        subject_kind=AuditSubjectKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
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


def test_completion_v4_binds_read_only_reconciliation_frontier() -> None:
    from ea.core import (
        canonical_reconciliation_outcome_bytes,
        create_reconciliation_observation_root,
        reconciliation_observation_digest,
        runtime_root_order_key,
    )
    from ea.core.lifecycle import canonical_dispatch_completed_v4_audit_payload
    from unit.test_lifecycle_ledger_gate import (
        _coordinator_with_gate,
        _outcome_bundle,
    )
    from unit.test_reconciliation_authority import _authority, _mismatch_observation, _snapshot

    fixture, matcher, orders, causal, delayed, _end = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    fill, execution_outcome = _outcome_bundle(matcher, delayed)
    coordinator, audit = _coordinator_with_gate(
        matcher, delayed, fill=fill, outcome=execution_outcome
    )
    coordinator.complete_active_dispatch(coordinator.begin_next_dispatch())
    refresh_record = next(
        record
        for record in audit.records
        if record.record_kind is AuditRecordKind.RISK_PORTFOLIO_REFRESH
    )
    binding = refresh_record.binding
    refresh_ack = create_audit_append_acknowledgement(refresh_record)
    observation = _mismatch_observation()
    reconciliation_outcome = _authority(_snapshot()).admit_observation(
        observation, dispatch_sequence=1
    )
    outcome_payload = canonical_reconciliation_outcome_bytes(reconciliation_outcome)
    outcome_record = create_audit_record(
        binding=binding,
        owner_sequence=2,
        record_kind=AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME,
        subject_kind=AuditSubjectKind.RECONCILIATION_OUTCOME,
        subject_sha256=audit_subject_digest(
            AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME, outcome_payload
        ),
        canonical_payload=outcome_payload,
        previous_record_sha256=EMPTY_RECORD_SHA256,
        previous_chain_head_sha256=EMPTY_CHAIN_HEAD_SHA256,
    )
    outcome_ack = create_audit_append_acknowledgement(outcome_record)
    root = create_reconciliation_observation_root(observation)

    payload = canonical_dispatch_completed_v4_audit_payload(
        binding=binding,
        dispatch_sequence=1,
        trigger_root_key=runtime_root_order_key(root),
        trigger_root_sha256=root.observation_sha256,
        observation_sha256=reconciliation_observation_digest(observation),
        outcome_acknowledgement=outcome_ack,
        refresh_acknowledgement=refresh_ack,
        refresh_value_sha256=DIGESTS[2],
        final_portfolio_snapshot_sha256=DIGESTS[3],
        final_risk_state_sha256=DIGESTS[4],
        pre_ack_state_sha256=DIGESTS[5],
    )

    document = json.loads(payload)
    assert document["schema"] == "ea.audit-dispatch-completed.v4"
    assert document["dispatch_kind"] == "reconciliation_observation"
    assert document["batch_sha256"] is None
    assert document["authorization_allowed"] is False
    assert document["outcome_count"] == 1
    assert document["ledger_outcome_count"] == 0
    assert document["submission_count"] == 0
    assert set(document) == {
        "authorization_allowed",
        "authorization_attempt_count",
        "authorization_attempt_outcome",
        "authorization_attempt_outcome_sha256",
        "batch_ack_sha256",
        "batch_sha256",
        "canonicalization",
        "dispatch_kind",
        "dispatch_sequence",
        "final_portfolio_snapshot_sha256",
        "final_risk_state_sha256",
        "ledger_outcome_count",
        "observation_sha256",
        "ordered_ledger_ack_sha256s_sha256",
        "ordered_outcome_ack_sha256s_sha256",
        "ordered_submission_receipt_sha256s_sha256",
        "outcome_acknowledgement_sha256",
        "outcome_count",
        "pre_ack_state_sha256",
        "refresh_acknowledgement_sha256",
        "refresh_value_sha256",
        "run_id",
        "schema",
        "submission_count",
        "trigger_root_key",
        "trigger_root_sha256",
    }
    from ea.core.audit import (
        _canonical_completion_v4_reconciliation_root_key_document,
        require_canonical_audit_payload,
    )

    assert (
        require_canonical_audit_payload(AuditRecordKind.RUNTIME_DISPATCH_COMPLETED, payload)
        == payload
    )

    with pytest.raises(Exception, match="completion-v4 root digest conflicts"):
        canonical_dispatch_completed_v4_audit_payload(
            binding=binding,
            dispatch_sequence=1,
            trigger_root_key=runtime_root_order_key(root),
            trigger_root_sha256=DIGESTS[0],
            observation_sha256=reconciliation_observation_digest(observation),
            outcome_acknowledgement=outcome_ack,
            refresh_acknowledgement=refresh_ack,
            refresh_value_sha256=DIGESTS[2],
            final_portfolio_snapshot_sha256=DIGESTS[3],
            final_risk_state_sha256=DIGESTS[4],
            pre_ack_state_sha256=DIGESTS[5],
        )
    invalid_builder_key = runtime_root_order_key(root)
    object.__setattr__(invalid_builder_key, "available_at", None)
    with pytest.raises(Exception, match="completion-v4 root key conflicts"):
        _canonical_completion_v4_reconciliation_root_key_document(
            invalid_builder_key, expected_run_id=binding.reference.run_id.value
        )
    for field, invalid in (
        ("available_at", "2026-01-02T09:31:00Z"),
        ("producer_namespace", ""),
        ("watermark_namespace", ""),
        ("producer_sequence", -1),
        ("watermark_sequence", -1),
        ("observation_owner_sequence", -1),
        ("observation_owner_kind", "execution.order"),
        ("trigger_root_sha256", DIGESTS[0].value),
    ):
        candidate = json.loads(payload)
        root_key = dict(candidate["trigger_root_key"])
        target = candidate if field == "trigger_root_sha256" else root_key
        target[field] = invalid
        candidate["trigger_root_key"] = root_key
        with pytest.raises(Exception, match="completion-v4 root key conflicts"):
            require_canonical_audit_payload(
                AuditRecordKind.RUNTIME_DISPATCH_COMPLETED,
                json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode(),
            )


def test_completion_v4_rejects_non_null_effect_frontier() -> None:
    from ea.core.audit import require_canonical_audit_payload

    binding, document = _v3_document()
    document["schema"] = "ea.audit-dispatch-completed.v4"
    document["dispatch_kind"] = "reconciliation_observation"
    document["batch_sha256"] = None
    document["batch_ack_sha256"] = None
    document["authorization_allowed"] = False
    document["observation_sha256"] = DIGESTS[0].value
    document["outcome_acknowledgement_sha256"] = DIGESTS[1].value
    document["refresh_acknowledgement_sha256"] = DIGESTS[2].value
    document["refresh_value_sha256"] = DIGESTS[3].value
    document["ledger_outcome_count"] = 0
    document["ordered_ledger_ack_sha256s_sha256"] = DIGESTS[4].value
    document["final_portfolio_snapshot_sha256"] = DIGESTS[5].value
    document["final_risk_state_sha256"] = DIGESTS[6].value
    document["trigger_root_key"] = {
        "available_at": "2026-01-02T09:31:00.000000Z",
        "domain_rank": 20,
        "kind_rank": 20,
        "observation_owner_kind": "reconciliation.observation",
        "observation_owner_sequence": 1,
        "producer_namespace": "reconciliation.sim",
        "producer_sequence": 1,
        "root_domain": "reconciliation_observation",
        "run_id": binding.reference.run_id.value,
        "watermark_namespace": "ledger.portfolio",
        "watermark_sequence": 3,
    }
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()

    with pytest.raises(Exception, match="completion-v4 requires one outcome acknowledgement"):
        require_canonical_audit_payload(AuditRecordKind.RUNTIME_DISPATCH_COMPLETED, payload)


def test_read_only_reconciliation_carriers_are_sealed_without_matcher_evidence() -> None:
    from ea.core.lifecycle import (
        ReadOnlyReconciliationDispatchOutcome,
        ReadOnlyReconciliationDispatchWindow,
    )

    with pytest.raises(TypeError, match="created only by the coordinator"):
        ReadOnlyReconciliationDispatchWindow()
    with pytest.raises(TypeError, match="created only by the coordinator"):
        ReadOnlyReconciliationDispatchOutcome()


def test_completion_v3_ledger_ack_aggregate_is_ordered_and_deterministic() -> None:
    _, first = _v3_document(ledger_acks=("aa" * 32, "bb" * 32, "cc" * 32))
    _, second = _v3_document(ledger_acks=("aa" * 32, "bb" * 32, "cc" * 32))
    _, reordered = _v3_document(ledger_acks=("cc" * 32, "bb" * 32, "aa" * 32))

    assert first["ledger_outcome_count"] == 3
    assert first["ordered_ledger_ack_sha256s_sha256"] == second["ordered_ledger_ack_sha256s_sha256"]
    assert (
        first["ordered_ledger_ack_sha256s_sha256"] != reordered["ordered_ledger_ack_sha256s_sha256"]
    )


def test_pre_ack_chain_head_uses_latest_physical_acknowledgement() -> None:
    from ea.runtime.coordinator import _pre_ack_chain_head

    binding, _ = _v3_document()
    acknowledgements = tuple(_acknowledgement(binding, f"{n:064x}", n) for n in (2, 3, 4))
    active = SimpleNamespace(
        ledger_acks=[acknowledgements[0]],
        refresh_ack=acknowledgements[1],
        authorization_ack=acknowledgements[2],
        batch_ack=None,
    )
    assert _pre_ack_chain_head(active, ()) == acknowledgements[2].chain_head_sha256  # type: ignore[arg-type]


def test_completion_v3_revalidates_all_shared_v2_evidence() -> None:
    _fixture, matcher, _orders, _causal, delayed, _end = _system()
    batch = matcher.match_active_market_root(delayed, dispatch_sequence=1)
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )

    foreign_binding = RunBinding(binding.reference, Sha256Digest("44" * 32))
    with pytest.raises(Exception, match="ledger acknowledgements conflict"):
        canonical_dispatch_completed_v3_audit_payload(
            binding=binding,
            batch=batch,
            outcome_acknowledgements=(),
            pre_ack_state_sha256=DIGESTS[0],
            ledger_outcome_acknowledgements=(_acknowledgement(foreign_binding, "aa" * 32, 2),),
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
