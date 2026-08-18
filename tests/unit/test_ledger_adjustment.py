from __future__ import annotations

import pytest

from ea.core import (
    CanonicalDecimal,
    CashReconciliationBalance,
    EconomicId,
    EconomicOwnerKind,
    OutcomeCode,
    ReconciliationAdjustmentFailureKind,
    ReconciliationAdjustmentResult,
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
    RunReference,
    Sha256Digest,
    audited_reconciliation_adjustment_authorization_digest,
    canonical_portfolio_snapshot_bytes,
    create_audited_reconciliation_adjustment_authorization,
    create_cash_reconciliation_discrepancy,
    create_reconciliation_observation,
    create_reconciliation_outcome,
    reconciliation_adjustment_command_digest,
)
from ea.core.execution import InstrumentExecutionSpecSet
from ea.core.portfolio import PortfolioLedgerError
from ea.core.reconciliation import (
    AuditedReconciliationAdjustmentAuthorization,
    ReconciliationAdjustmentAuthorization,
    ReconciliationAdjustmentCommand,
    _authorization_binding,
    _create_reconciliation_adjustment_authorization,
    _create_reconciliation_adjustment_command,
)
from ea.portfolio import create_portfolio_ledger
from ea.portfolio.ledger import PortfolioLedger
from unit.test_portfolio_ledger import RUN_ID, USD, _fill, _spec_set
from unit.test_reconciliation_observation import TIME
from unit.test_reconciliation_outcome import _outcome_acknowledgement

BINDING = RunBinding(RunReference(RUN_ID, Sha256Digest("33" * 32)), Sha256Digest("44" * 32))


def _correction_bundle(
    *,
    decision: ReconciliationAuthorizationDecision = ReconciliationAuthorizationDecision.ALLOWED,
) -> tuple[
    PortfolioLedger,
    InstrumentExecutionSpecSet,
    ReconciliationObservation,
    ReconciliationOutcome,
    object,
    ReconciliationAdjustmentCommand,
    ReconciliationAdjustmentAuthorization,
]:
    spec_set = _spec_set()
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    fill = _fill(spec_set, fill_sequence=1, dedup="adjustment-cash")
    ledger.apply_fill(fill)
    local_cash = ledger.snapshot.cash_balances[0].amount
    observed_cash = CanonicalDecimal("-9.99")
    observation = create_reconciliation_observation(
        run_id=RUN_ID,
        spec_set=spec_set,
        observation_id=EconomicId(
            RUN_ID,
            EconomicOwnerKind.RECONCILIATION_OBSERVATION,
            1,
        ),
        kind=ReconciliationObservationKind.CASH_SNAPSHOT,
        source_namespace=__import__("ea.core", fromlist=["SourceNamespace"]).SourceNamespace(
            "reconciliation.sim"
        ),
        source_sequence=7,
        occurred_at=TIME,
        available_at=TIME,
        watermark_namespace=__import__("ea.core", fromlist=["SourceNamespace"]).SourceNamespace(
            "ledger.portfolio"
        ),
        watermark_sequence=ledger.snapshot.ledger_sequence,
        declared_scope_kind=ReconciliationScopeKind.CASH,
        declared_scope_id=__import__("ea.core", fromlist=["RuntimeIdentifier"]).RuntimeIdentifier(
            "portfolio.default"
        ),
        provenance_id=__import__("ea.core", fromlist=["FactProvenanceId"]).FactProvenanceId(
            "reconciliation.fixture.v1"
        ),
        provenance_payload_sha256=Sha256Digest("ab" * 32),
        balances=(CashReconciliationBalance(USD, observed_cash),),
    )
    discrepancy = create_cash_reconciliation_discrepancy(
        spec_set=spec_set,
        currency=USD,
        local_amount=local_cash,
        observed_amount=observed_cash,
    )
    outcome = create_reconciliation_outcome(
        run_id=RUN_ID,
        dispatch_sequence=8,
        observation_sha256=__import__(
            "ea.core", fromlist=["reconciliation_observation_digest"]
        ).reconciliation_observation_digest(observation),
        local_snapshot_version=ledger.snapshot.snapshot_version,
        local_snapshot_sha256=__import__(
            "ea.core", fromlist=["portfolio_snapshot_digest"]
        ).portfolio_snapshot_digest(ledger.snapshot),
        ledger_sequence=ledger.snapshot.ledger_sequence,
        watermark_comparison=ReconciliationWatermarkComparison.EQUAL,
        discrepancies=(discrepancy,),
        outcome_code=OutcomeCode.RECONCILIATION_MISMATCH,
        requested_action=ReconciliationRequestedAction.PROPOSE_SINGLE_TARGET_ADJUSTMENT,
        halt_requested=True,
    )
    acknowledgement = _outcome_acknowledgement(outcome)
    command = _create_reconciliation_adjustment_command(
        binding=BINDING,
        spec_set=spec_set,
        observation=observation,
        outcome=outcome,
        outcome_acknowledgement=acknowledgement,
        local_snapshot=ledger.snapshot,
        adjustment_id=EconomicId(
            RUN_ID,
            EconomicOwnerKind.RECONCILIATION_ADJUSTMENT,
            1,
        ),
    )
    authorization = _create_reconciliation_adjustment_authorization(
        binding=BINDING,
        spec_set=spec_set,
        outcome=outcome,
        outcome_acknowledgement=acknowledgement,
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
    return ledger, spec_set, observation, outcome, acknowledgement, command, authorization


def _audited(
    authorization: ReconciliationAdjustmentAuthorization,
) -> AuditedReconciliationAdjustmentAuthorization:
    from ea.core import audit_subject_digest  # noqa: F401
    from ea.core.audit import create_audit_record  # noqa: F401
    from unit.test_reconciliation_outcome import _authorization_acknowledgement

    return create_audited_reconciliation_adjustment_authorization(
        authorization,
        _authorization_acknowledgement(authorization),
    )


def test_balance_correction_applies_exactly_once_with_shared_entry_chain() -> None:
    (
        ledger,
        _spec_set_value,
        _observation,
        _outcome,
        _acknowledgement,
        command,
        authorization,
    ) = _correction_bundle()
    audited = _audited(authorization)

    assert command.variant is ReconciliationAdjustmentVariant.BALANCE_CORRECTION
    before = canonical_portfolio_snapshot_bytes(ledger.snapshot)
    outcome = ledger.apply_reconciliation_adjustment(audited, command)

    assert outcome.result is ReconciliationAdjustmentResult.APPLIED
    assert ledger.snapshot.snapshot_version == 2
    assert ledger.snapshot.cash_balances[0].amount == CanonicalDecimal("-9.99")
    assert canonical_portfolio_snapshot_bytes(ledger.snapshot) != before
    assert len(ledger.transactions) == 2
    correction = ledger.transactions[1]
    assert type(correction).__name__ == "ReconciliationTransaction"
    assert correction.entry_id.owner_sequence == 2
    assert correction.previous_transaction_sha256 is not None
    assert len(audited_reconciliation_adjustment_authorization_digest(audited).value) == 64

    replay = ledger.apply_reconciliation_adjustment(audited, command)
    assert replay.result is ReconciliationAdjustmentResult.APPLIED
    assert replay == outcome
    assert ledger.snapshot.snapshot_version == 2


def test_stale_frontier_fails_closed_before_mutation() -> None:
    (
        ledger,
        spec_set,
        _observation,
        _outcome,
        _acknowledgement,
        command,
        authorization,
    ) = _correction_bundle()
    audited = _audited(authorization)
    second = _fill(spec_set, fill_sequence=2, dedup="adjustment-second")
    ledger.apply_fill(second)
    frontier = canonical_portfolio_snapshot_bytes(ledger.snapshot)

    outcome = ledger.apply_reconciliation_adjustment(audited, command)

    assert outcome.result is ReconciliationAdjustmentResult.FAILED
    assert outcome.failure_kind is ReconciliationAdjustmentFailureKind.STALE_FRONTIER
    assert canonical_portfolio_snapshot_bytes(ledger.snapshot) == frontier


def test_denied_authorization_fails_closed_without_mutation() -> None:
    (
        ledger,
        _spec_set_value,
        _observation,
        _outcome,
        _acknowledgement,
        command,
        authorization,
    ) = _correction_bundle(
        decision=ReconciliationAuthorizationDecision.DENIED,
    )
    audited = _audited(authorization)
    frontier = canonical_portfolio_snapshot_bytes(ledger.snapshot)

    outcome = ledger.apply_reconciliation_adjustment(audited, command)

    assert outcome.result is ReconciliationAdjustmentResult.FAILED
    assert outcome.failure_kind is ReconciliationAdjustmentFailureKind.INVALID_COMMAND
    assert canonical_portfolio_snapshot_bytes(ledger.snapshot) == frontier


def test_command_digest_drift_conflicts_before_mutation() -> None:
    (
        ledger,
        _spec_set_value,
        _observation,
        _outcome,
        _acknowledgement,
        command,
        authorization,
    ) = _correction_bundle()
    audited = _audited(authorization)
    forged = object.__new__(type(command))
    for field in command.__slots__:
        if field == "_seal":
            object.__setattr__(forged, "_seal", command._seal)
        else:
            object.__setattr__(forged, field, getattr(command, field))
    object.__setattr__(forged, "delta", CanonicalDecimal("0.02"))
    with pytest.raises(PortfolioLedgerError, match="authorization conflicts"):
        ledger.apply_reconciliation_adjustment(audited, forged)


def test_adjustment_authority_evidence_is_sealed_and_bound() -> None:
    (
        ledger,
        _spec_set_value,
        _observation,
        _outcome,
        _acknowledgement,
        command,
        authorization,
    ) = _correction_bundle()
    audited = _audited(authorization)

    with pytest.raises(PortfolioLedgerError, match="exact factory-issued"):
        ledger.apply_reconciliation_adjustment(None, command)  # type: ignore[arg-type]
    with pytest.raises(PortfolioLedgerError, match="exact factory-issued"):
        ledger.apply_reconciliation_adjustment(audited, None)  # type: ignore[arg-type]
    assert _authorization_binding(authorization) == BINDING
    assert len(reconciliation_adjustment_command_digest(command).value) == 64


def test_ancestry_resolution_removes_both_references_and_they_stay_removed() -> None:
    # ARCH-006 regression: the internal unresolved mapping must drop the
    # resolved fill together with the open tuples, and the removal must
    # survive later mutations.
    from ea.core import (
        OpenReconciliationRef,
        ReconciliationObservationKind,
        ReconciliationRequestedAction,
        ReconciliationScopeKind,
        create_fill,
        fill_digest,
        portfolio_snapshot_digest,
        reconciliation_observation_digest,
    )
    from ea.core.ledger_integration import _create_ledger_application_command
    from ea.core.reconciliation import (
        _ancestry_evidence_digest,
        _ancestry_order_scope_id,
    )
    from unit.test_execution_messages import SPEC_SET, _order, _trade_fact

    spec_set = SPEC_SET
    order = _order()
    fill = create_fill(
        fill_id=EconomicId(RUN_ID, EconomicOwnerKind.EXECUTION_FILL, 9),
        fact=_trade_fact(resolved=False),
        spec_set=spec_set,
    )
    reference = OpenReconciliationRef(
        fill.fill_id,
        fill_digest(fill),
        Sha256Digest("bb" * 32),
    )
    ledger = create_portfolio_ledger(RUN_ID, spec_set)
    command = _create_ledger_application_command(
        run_id=RUN_ID,
        dispatch_sequence=3,
        audited_handoff_sha256=Sha256Digest("aa" * 32),
        processing_outcome_sha256=Sha256Digest("bb" * 32),
        fill_id=fill.fill_id,
        fill_sha256=fill_digest(fill),
        requires_reconciliation=True,
    )
    ledger.apply_ledger_application_command(command, fill)
    assert ledger.snapshot.unresolved_fills != ()
    assert ledger.snapshot.open_reconciliation_refs == (reference,)

    observation = create_reconciliation_observation(
        run_id=RUN_ID,
        spec_set=spec_set,
        observation_id=EconomicId(
            RUN_ID,
            EconomicOwnerKind.RECONCILIATION_OBSERVATION,
            1,
        ),
        kind=ReconciliationObservationKind.ORDER_DETAIL,
        source_namespace=__import__("ea.core", fromlist=["SourceNamespace"]).SourceNamespace(
            "reconciliation.sim"
        ),
        source_sequence=7,
        occurred_at=TIME,
        available_at=TIME,
        watermark_namespace=__import__("ea.core", fromlist=["SourceNamespace"]).SourceNamespace(
            "ledger.portfolio"
        ),
        watermark_sequence=ledger.snapshot.ledger_sequence,
        declared_scope_kind=ReconciliationScopeKind.ORDER,
        declared_scope_id=_ancestry_order_scope_id(order),
        provenance_id=__import__("ea.core", fromlist=["FactProvenanceId"]).FactProvenanceId(
            "reconciliation.fixture.v1"
        ),
        provenance_payload_sha256=_ancestry_evidence_digest(fill, reference, order),
        balances=(),
    )
    outcome = create_reconciliation_outcome(
        run_id=RUN_ID,
        dispatch_sequence=8,
        observation_sha256=reconciliation_observation_digest(observation),
        local_snapshot_version=ledger.snapshot.snapshot_version,
        local_snapshot_sha256=portfolio_snapshot_digest(ledger.snapshot),
        ledger_sequence=ledger.snapshot.ledger_sequence,
        watermark_comparison=ReconciliationWatermarkComparison.EQUAL,
        discrepancies=(),
        outcome_code=OutcomeCode.RECONCILIATION_MATCH,
        requested_action=ReconciliationRequestedAction.PROPOSE_ANCESTRY_RESOLUTION,
        halt_requested=True,
    )
    acknowledgement = _outcome_acknowledgement(outcome)
    adjustment_command = _create_reconciliation_adjustment_command(
        binding=BINDING,
        spec_set=spec_set,
        observation=observation,
        outcome=outcome,
        outcome_acknowledgement=acknowledgement,
        local_snapshot=ledger.snapshot,
        adjustment_id=EconomicId(
            RUN_ID,
            EconomicOwnerKind.RECONCILIATION_ADJUSTMENT,
            2,
        ),
        open_reconciliation_ref=reference,
        ancestry_fill=fill,
        ancestry_order=order,
    )
    authorization = _create_reconciliation_adjustment_authorization(
        binding=BINDING,
        spec_set=spec_set,
        outcome=outcome,
        outcome_acknowledgement=acknowledgement,
        command=adjustment_command,
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
    audited = create_audited_reconciliation_adjustment_authorization(
        authorization,
        __import__(
            "unit.test_reconciliation_outcome", fromlist=["_authorization_acknowledgement"]
        )._authorization_acknowledgement(authorization),
    )

    result = ledger.apply_reconciliation_adjustment(audited, adjustment_command)
    assert result.result is ReconciliationAdjustmentResult.APPLIED
    assert len(ledger.snapshot.unresolved_fills) == 0
    assert len(ledger.snapshot.open_reconciliation_refs) == 0

    second = _fill(spec_set, fill_sequence=2, dedup="adjustment-after-ancestry")
    ledger.apply_fill(second)
    assert len(ledger.snapshot.unresolved_fills) == 0
    assert len(ledger.snapshot.open_reconciliation_refs) == 0
