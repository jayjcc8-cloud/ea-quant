from __future__ import annotations

import pytest

from ea.core import (
    EMPTY_CHAIN_HEAD_SHA256,
    EMPTY_RECORD_SHA256,
    AuditRecordKind,
    AuditSubjectKind,
    EconomicId,
    EconomicOwnerKind,
    ExecutionFactAction,
    ExecutionFactAnomaly,
    ExecutionFactProcessingOutcome,
    LedgerHandoffAction,
    LedgerHandoffFailure,
    OrderResolutionKeyKind,
    RunBinding,
    RunReference,
    Sha256Digest,
    audit_subject_digest,
    canonical_execution_fact_processing_outcome_bytes,
    create_audit_append_acknowledgement,
    create_audit_record,
    create_audited_execution_fact_handoff,
    create_execution_fact_processing_outcome,
    create_fill,
    create_order_resolution_binding,
    ledger_handoff_outcome_digest,
)
from ea.core.execution_messages import Fill
from ea.core.lifecycle import AuditedExecutionFactHandoff
from ea.core.portfolio import portfolio_snapshot_digest
from ea.execution.matcher import Phase1HistoricalMatcher
from ea.portfolio import (
    create_phase1_ledger_handoff_authority,
    create_portfolio_ledger,
)
from ea.portfolio.ledger_authority import LedgerHandoffAuthorityError
from unit.test_audit_journal import _batch_payload
from unit.test_historical_matcher import _system


def _handoff_bundle(
    *,
    action: ExecutionFactAction = ExecutionFactAction.ACCEPTED,
    with_fill: bool = True,
    anomalies: tuple[ExecutionFactAnomaly, ...] = (),
) -> tuple[
    Phase1HistoricalMatcher,
    Fill | None,
    ExecutionFactProcessingOutcome,
    AuditedExecutionFactHandoff,
]:
    _fixture, matcher, orders, causal, delayed, _end = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    batch = matcher.match_active_market_root(delayed, dispatch_sequence=8)
    ingress = batch.ingresses[0]
    fact = ingress.fact
    key_kinds = tuple(
        kind
        for present, kind in (
            (fact.order_id is not None, OrderResolutionKeyKind.ORDER_ID),
            (fact.client_submission_key is not None, OrderResolutionKeyKind.CLIENT_SUBMISSION_KEY),
            (fact.venue_order_id is not None, OrderResolutionKeyKind.VENUE_ORDER_ID),
        )
        if present
    )
    fill = None
    if with_fill:
        fill = create_fill(
            fill_id=EconomicId(matcher.run_id, EconomicOwnerKind.EXECUTION_FILL, 1),
            fact=fact,
            spec_set=matcher.spec_set,
        )
    outcome = create_execution_fact_processing_outcome(
        run_id=matcher.run_id,
        runtime_dispatch_sequence=8,
        ingress=ingress,
        action=action,
        anomalies=anomalies,
        order_resolutions=(
            ()
            if action in (ExecutionFactAction.DUPLICATE, ExecutionFactAction.CONFLICT)
            else tuple(
                create_order_resolution_binding(key_kind=kind, resolved_order=None)
                for kind in key_kinds
            )
        ),
        resolved_order=None,
        fill=fill,
        projection_before=None,
        projection_after=None,
    )
    payload = canonical_execution_fact_processing_outcome_bytes(outcome)
    binding = RunBinding(
        RunReference(matcher.run_id, Sha256Digest("11" * 32)),
        Sha256Digest("22" * 32),
    )
    outcome_record = create_audit_record(
        binding=binding,
        owner_sequence=2,
        record_kind=AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME,
        subject_kind=AuditSubjectKind.EXECUTION_FACT_PROCESSING_OUTCOME,
        subject_sha256=audit_subject_digest(
            AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME,
            payload,
        ),
        canonical_payload=payload,
        previous_record_sha256=EMPTY_RECORD_SHA256,
        previous_chain_head_sha256=EMPTY_CHAIN_HEAD_SHA256,
    )
    outcome_ack = create_audit_append_acknowledgement(outcome_record)
    import json as _json

    batch_document = _json.loads(_batch_payload())
    batch_document["run_id"] = binding.reference.run_id.value
    batch_payload = _json.dumps(
        batch_document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    batch_record = create_audit_record(
        binding=binding,
        owner_sequence=2,
        record_kind=AuditRecordKind.MATCHER_DISPATCH_BATCH,
        subject_kind=AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
        subject_sha256=Sha256Digest("33" * 32),
        canonical_payload=batch_payload,
        previous_record_sha256=EMPTY_RECORD_SHA256,
        previous_chain_head_sha256=EMPTY_CHAIN_HEAD_SHA256,
    )
    batch_ack = create_audit_append_acknowledgement(batch_record)
    handoff = create_audited_execution_fact_handoff(
        outcome=outcome,
        batch_acknowledgement=batch_ack,
        outcome_acknowledgement=outcome_ack,
    )
    return matcher, fill, outcome, handoff


def test_no_fill_handoff_binds_unchanged_snapshot_without_mutation() -> None:
    matcher, fill, outcome, handoff = _handoff_bundle(
        action=ExecutionFactAction.DUPLICATE,
        with_fill=False,
    )
    assert fill is None
    ledger = create_portfolio_ledger(matcher.run_id, matcher.spec_set)
    authority = create_phase1_ledger_handoff_authority(
        matcher.run_id,
        matcher.spec_set,
        ledger,
    )

    before = portfolio_snapshot_digest(ledger.snapshot)
    result = authority.apply_handoff(handoff=handoff, outcome=outcome, fill=None)

    assert result.action is LedgerHandoffAction.NOT_APPLICABLE
    assert result.before_snapshot_sha256 == before
    assert result.after_snapshot_sha256 == before
    assert portfolio_snapshot_digest(ledger.snapshot) == before
    assert result.failure is None


def test_applied_fill_handoff_commits_once_and_retains_original_result() -> None:
    matcher, fill, outcome, handoff = _handoff_bundle()
    ledger = create_portfolio_ledger(matcher.run_id, matcher.spec_set)
    authority = create_phase1_ledger_handoff_authority(
        matcher.run_id,
        matcher.spec_set,
        ledger,
    )

    first = authority.apply_handoff(handoff=handoff, outcome=outcome, fill=fill)
    assert first.action is LedgerHandoffAction.EFFECT_COMMITTED
    assert first.original_ledger_apply_outcome is not None
    assert ledger.snapshot.snapshot_version == 1
    snapshot_after = portfolio_snapshot_digest(ledger.snapshot)

    replay = authority.apply_handoff(handoff=handoff, outcome=outcome, fill=fill)
    assert replay.action is LedgerHandoffAction.EFFECT_COMMITTED
    assert replay.original_ledger_apply_outcome == first.original_ledger_apply_outcome
    assert replay.after_snapshot_sha256 == snapshot_after
    assert len(ledger.transactions) == 1
    assert len(ledger_handoff_outcome_digest(replay).value) == 64


def test_unresolved_fill_requires_reconciliation_and_halt() -> None:
    matcher, fill, outcome, handoff = _handoff_bundle(
        action=ExecutionFactAction.UNRESOLVED,
        anomalies=(ExecutionFactAnomaly.UNKNOWN_ORDER,),
    )
    ledger = create_portfolio_ledger(matcher.run_id, matcher.spec_set)
    authority = create_phase1_ledger_handoff_authority(
        matcher.run_id,
        matcher.spec_set,
        ledger,
    )

    result = authority.apply_handoff(handoff=handoff, outcome=outcome, fill=fill)

    assert result.action is LedgerHandoffAction.EFFECT_COMMITTED
    assert result.requires_reconciliation is True
    assert result.halt_requested is True
    assert ledger.snapshot.open_reconciliation_refs != ()


def test_mismatched_fill_evidence_fails_closed_at_the_authority() -> None:
    matcher, fill, outcome, handoff = _handoff_bundle()
    ledger = create_portfolio_ledger(matcher.run_id, matcher.spec_set)
    authority = create_phase1_ledger_handoff_authority(
        matcher.run_id,
        matcher.spec_set,
        ledger,
    )

    with pytest.raises(LedgerHandoffAuthorityError, match="fill evidence"):
        authority.apply_handoff(handoff=handoff, outcome=outcome, fill=None)
    assert ledger.snapshot.snapshot_version == 0


def test_legacy_applied_fill_integration_fails_closed_as_unbound() -> None:
    matcher, fill, outcome, handoff = _handoff_bundle()
    ledger = create_portfolio_ledger(matcher.run_id, matcher.spec_set)
    assert fill is not None
    ledger.apply_fill(fill)
    authority = create_phase1_ledger_handoff_authority(
        matcher.run_id,
        matcher.spec_set,
        ledger,
    )

    result = authority.apply_handoff(handoff=handoff, outcome=outcome, fill=fill)

    assert result.action is LedgerHandoffAction.FAILED
    assert result.failure is LedgerHandoffFailure.UNBOUND_EXISTING_FILL
    assert result.halt_requested is True
    assert ledger.snapshot.snapshot_version == 1
