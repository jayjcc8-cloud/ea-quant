from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SCRIPT = """
import json
import os
from decimal import ROUND_CEILING, ROUND_FLOOR, getcontext
from datetime import UTC, datetime
from ea.core import (
    EMPTY_CHAIN_HEAD_SHA256, EMPTY_RECORD_SHA256, AuditRecordKind,
    AuditSubjectKind, CanonicalDecimal, EconomicId, EconomicOwnerKind,
    FactProvenanceId, IngressIdentity, Instrument, InstrumentExecutionSpec, InstrumentSpecId,
    InstrumentSpecSetId, LedgerHandoffAction, PortfolioSnapshot, PriceDomain,
    ReconciliationAuthorizationDecision, ReconciliationAuthorizationPolicyId,
    ReconciliationObservationKind, ReconciliationRequestedAction,
    ReconciliationScopeKind, ReconciliationWatermarkComparison, RiskPolicyId,
    RunBinding, RunId, RunReference, RuntimeIdentifier, SettlementCurrency,
    Sha256Digest, SourceNamespace,
    VenueId, PositionReconciliationBalance, build_instrument_spec_set,
    audit_subject_digest,
    audited_reconciliation_adjustment_authorization_digest,
    canonical_audited_reconciliation_adjustment_authorization_bytes,
    canonical_ledger_application_command_bytes,
    canonical_ledger_handoff_outcome_bytes,
    canonical_portfolio_risk_refresh_bytes,
    canonical_reconciliation_adjustment_authorization_bytes,
    canonical_reconciliation_adjustment_command_bytes,
    canonical_reconciliation_observation_bytes,
    canonical_reconciliation_outcome_bytes,
    create_audit_append_acknowledgement, create_audit_record,
    create_audited_reconciliation_adjustment_authorization,
    create_position_reconciliation_discrepancy,
    create_reconciliation_observation,
    create_reconciliation_outcome,
    create_ledger_handoff_outcome,
    ledger_application_command_digest,
    ledger_handoff_outcome_digest,
    OutcomeCode,
    portfolio_risk_refresh_digest,
    reconciliation_adjustment_authorization_digest,
    reconciliation_adjustment_command_digest,
    reconciliation_observation_digest,
    reconciliation_outcome_digest,
)
from ea.core.ledger_integration import (
    _create_ledger_application_command, _create_portfolio_risk_refresh,
)
from ea.core.reconciliation import (
    _create_reconciliation_adjustment_authorization,
    _create_reconciliation_adjustment_command,
)
from ea.core.risk import _create_risk_state_snapshot

decimal_context = getcontext()
decimal_context.prec = int(os.environ["EA_TEST_DECIMAL_PRECISION"])
decimal_context.rounding = (
    ROUND_CEILING
    if os.environ["EA_TEST_DECIMAL_ROUNDING"] == "ceiling"
    else ROUND_FLOOR
)
run_id = RunId("12345678-1234-4234-8234-123456789abc")
fill_id = EconomicId(run_id, EconomicOwnerKind.EXECUTION_FILL, 7)
command = _create_ledger_application_command(
    run_id=run_id,
    dispatch_sequence=2,
    audited_handoff_sha256=Sha256Digest("11" * 32),
    processing_outcome_sha256=Sha256Digest("22" * 32),
    fill_id=fill_id,
    fill_sha256=Sha256Digest("33" * 32),
    requires_reconciliation=False,
)
outcome = create_ledger_handoff_outcome(
    run_id=run_id,
    dispatch_sequence=2,
    ingress_identity=IngressIdentity(SourceNamespace("sim.execution"), 3),
    audited_handoff_sha256=Sha256Digest("11" * 32),
    processing_outcome_sha256=Sha256Digest("22" * 32),
    processing_outcome_ack_sha256=Sha256Digest("44" * 32),
    fill_id=None,
    fill_sha256=None,
    action=LedgerHandoffAction.NOT_APPLICABLE,
    original_ledger_apply_outcome=None,
    original_ledger_apply_outcome_sha256=None,
    before_snapshot_version=4,
    before_snapshot_sha256=Sha256Digest("55" * 32),
    after_snapshot_version=4,
    after_snapshot_sha256=Sha256Digest("55" * 32),
    requires_reconciliation=False,
    halt_requested=False,
    failure=None,
)
snapshot = PortfolioSnapshot(
    run_id, InstrumentSpecSetId("phase1.test.v1"), Sha256Digest("66" * 32),
    0, 0, None, None, (), (), (), (),
)
risk_state = _create_risk_state_snapshot(
    run_id=run_id,
    policy_id=RiskPolicyId("phase1.test-risk.v1"),
    policy_sha256=Sha256Digest("77" * 32),
    risk_state_version=0,
    halted=False,
    halt_reason=None,
    halt_causal_root_available_at=None,
    halt_dispatch_sequence=None,
    conflict_existing_intent_sha256=None,
    conflict_submitted_intent_sha256=None,
)
refresh = _create_portfolio_risk_refresh(
    portfolio_snapshot=snapshot,
    risk_state=risk_state,
    dispatch_sequence=1,
    refresh_sequence=1,
    ordered_ledger_ack_frontier_sha256=Sha256Digest("88" * 32),
    submission_permitted=True,
    previous_refresh_sha256=None,
)
instrument = Instrument(VenueId("XNAS"), "AAPL")
other_instrument = Instrument(VenueId("XNAS"), "MSFT")
specifications = (
    InstrumentExecutionSpec(
        instrument=instrument,
        specification_id=InstrumentSpecId("xnas.aapl.v1"),
        price_quantum=CanonicalDecimal("0.01"),
        quantity_quantum=CanonicalDecimal("1"),
        settlement_currency=SettlementCurrency("USD"),
        currency_quantum=CanonicalDecimal("0.01"),
        contract_multiplier=CanonicalDecimal("1"),
        price_domain=PriceDomain.SIGNED,
    ),
    InstrumentExecutionSpec(
        instrument=other_instrument,
        specification_id=InstrumentSpecId("xnas.msft.v1"),
        price_quantum=CanonicalDecimal("0.01"),
        quantity_quantum=CanonicalDecimal("1"),
        settlement_currency=SettlementCurrency("USD"),
        currency_quantum=CanonicalDecimal("0.01"),
        contract_multiplier=CanonicalDecimal("1"),
        price_domain=PriceDomain.SIGNED,
    ),
)
if os.environ["EA_TEST_INPUT_ORDER"] == "reverse":
    specifications = tuple(reversed(specifications))
spec_set = build_instrument_spec_set(
    InstrumentSpecSetId("phase1.test.v1"),
    specifications,
)
observation = create_reconciliation_observation(
    run_id=run_id,
    spec_set=spec_set,
    observation_id=EconomicId(run_id, EconomicOwnerKind.RECONCILIATION_OBSERVATION, 1),
    kind=ReconciliationObservationKind.POSITION_SNAPSHOT,
    source_namespace=SourceNamespace("reconciliation.sim"),
    source_sequence=7,
    occurred_at=datetime(2026, 1, 2, 9, 31, tzinfo=UTC),
    available_at=datetime(2026, 1, 2, 9, 31, tzinfo=UTC),
    watermark_namespace=SourceNamespace("ledger.portfolio"),
    watermark_sequence=3,
    declared_scope_kind=ReconciliationScopeKind.POSITION,
    declared_scope_id=RuntimeIdentifier("portfolio.default"),
    provenance_id=FactProvenanceId("reconciliation.fixture.v1"),
    provenance_payload_sha256=Sha256Digest("99" * 32),
    balances=(PositionReconciliationBalance(instrument, CanonicalDecimal("10")),),
)
binding = RunBinding(
    RunReference(run_id, Sha256Digest("aa" * 32)),
    Sha256Digest("bb" * 32),
)
discrepancy = create_position_reconciliation_discrepancy(
    spec_set=spec_set,
    instrument=instrument,
    local_amount=CanonicalDecimal("8"),
    observed_amount=CanonicalDecimal("10"),
)
reconciliation_outcome = create_reconciliation_outcome(
    run_id=run_id,
    dispatch_sequence=9,
    observation_sha256=reconciliation_observation_digest(observation),
    local_snapshot_version=3,
    local_snapshot_sha256=Sha256Digest("cc" * 32),
    ledger_sequence=3,
    watermark_comparison=ReconciliationWatermarkComparison.EQUAL,
    discrepancies=(discrepancy,),
    outcome_code=OutcomeCode.RECONCILIATION_MISMATCH,
    requested_action=ReconciliationRequestedAction.PROPOSE_SINGLE_TARGET_ADJUSTMENT,
    halt_requested=True,
)
reconciliation_outcome_payload = canonical_reconciliation_outcome_bytes(
    reconciliation_outcome
)
outcome_record = create_audit_record(
    binding=binding,
    owner_sequence=2,
    record_kind=AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME,
    subject_kind=AuditSubjectKind.RECONCILIATION_OUTCOME,
    subject_sha256=audit_subject_digest(
        AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME,
        reconciliation_outcome_payload,
    ),
    canonical_payload=reconciliation_outcome_payload,
    previous_record_sha256=EMPTY_RECORD_SHA256,
    previous_chain_head_sha256=EMPTY_CHAIN_HEAD_SHA256,
)
outcome_acknowledgement = create_audit_append_acknowledgement(outcome_record)
adjustment_command = _create_reconciliation_adjustment_command(
    binding=binding,
    spec_set=spec_set,
    observation=observation,
    outcome=reconciliation_outcome,
    outcome_acknowledgement=outcome_acknowledgement,
    adjustment_id=EconomicId(
        run_id,
        EconomicOwnerKind.RECONCILIATION_ADJUSTMENT,
        1,
    ),
)
authorization = _create_reconciliation_adjustment_authorization(
    binding=binding,
    spec_set=spec_set,
    outcome=reconciliation_outcome,
    outcome_acknowledgement=outcome_acknowledgement,
    command=adjustment_command,
    authorization_id=EconomicId(
        run_id,
        EconomicOwnerKind.RECONCILIATION_AUTHORIZATION,
        1,
    ),
    policy_id=ReconciliationAuthorizationPolicyId("reconciliation.fixture-policy.v1"),
    policy_version=1,
    policy_sha256=Sha256Digest("dd" * 32),
    decision=ReconciliationAuthorizationDecision.ALLOWED,
    available_at=datetime(2026, 1, 2, 9, 32, tzinfo=UTC),
)
authorization_payload = canonical_reconciliation_adjustment_authorization_bytes(authorization)
authorization_record = create_audit_record(
    binding=binding,
    owner_sequence=3,
    record_kind=AuditRecordKind.RECONCILIATION_ADJUSTMENT_AUTHORIZATION,
    subject_kind=AuditSubjectKind.RECONCILIATION_ADJUSTMENT_AUTHORIZATION,
    subject_sha256=audit_subject_digest(
        AuditRecordKind.RECONCILIATION_ADJUSTMENT_AUTHORIZATION,
        authorization_payload,
    ),
    canonical_payload=authorization_payload,
    previous_record_sha256=EMPTY_RECORD_SHA256,
    previous_chain_head_sha256=EMPTY_CHAIN_HEAD_SHA256,
)
audited_authorization = create_audited_reconciliation_adjustment_authorization(
    authorization,
    create_audit_append_acknowledgement(authorization_record),
)
print(json.dumps({
    "command": canonical_ledger_application_command_bytes(command).hex(),
    "command_sha256": ledger_application_command_digest(command).value,
    "outcome": canonical_ledger_handoff_outcome_bytes(outcome).hex(),
    "outcome_sha256": ledger_handoff_outcome_digest(outcome).value,
    "refresh": canonical_portfolio_risk_refresh_bytes(refresh).hex(),
    "refresh_sha256": portfolio_risk_refresh_digest(refresh).value,
    "observation": canonical_reconciliation_observation_bytes(observation).hex(),
    "observation_sha256": reconciliation_observation_digest(observation).value,
    "reconciliation_outcome": reconciliation_outcome_payload.hex(),
    "reconciliation_outcome_sha256": reconciliation_outcome_digest(
        reconciliation_outcome
    ).value,
    "adjustment_command": canonical_reconciliation_adjustment_command_bytes(
        adjustment_command
    ).hex(),
    "adjustment_command_sha256": reconciliation_adjustment_command_digest(
        adjustment_command
    ).value,
    "authorization": authorization_payload.hex(),
    "authorization_sha256": reconciliation_adjustment_authorization_digest(
        authorization
    ).value,
    "audited_authorization": canonical_audited_reconciliation_adjustment_authorization_bytes(
        audited_authorization
    ).hex(),
    "audited_authorization_sha256": audited_reconciliation_adjustment_authorization_digest(
        audited_authorization
    ).value,
}, sort_keys=True, separators=(",", ":")))
"""


def _run(
    cwd: Path,
    *,
    hash_seed: str,
    timezone: str,
    locale: str,
    decimal_precision: str,
    decimal_rounding: str,
    input_order: str,
) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": hash_seed,
            "TZ": timezone,
            "LC_ALL": locale,
            "LANG": locale,
            "EA_TEST_DECIMAL_PRECISION": decimal_precision,
            "EA_TEST_DECIMAL_ROUNDING": decimal_rounding,
            "EA_TEST_INPUT_ORDER": input_order,
        }
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _SCRIPT],
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_ledger_integration_vectors_ignore_process_environment_and_input_order(
    tmp_path: Path,
) -> None:
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()

    first = _run(
        first_cwd,
        hash_seed="1",
        timezone="UTC",
        locale="C",
        decimal_precision="6",
        decimal_rounding="ceiling",
        input_order="forward",
    )
    second = _run(
        second_cwd,
        hash_seed="987654321",
        timezone="Asia/Shanghai",
        locale="POSIX",
        decimal_precision="50",
        decimal_rounding="floor",
        input_order="reverse",
    )

    assert first == second
