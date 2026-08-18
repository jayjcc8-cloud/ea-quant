from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SCRIPT = """
import os
from decimal import ROUND_CEILING, ROUND_FLOOR, getcontext
from ea.core import (
    CanonicalDecimal, EconomicId, EconomicOwnerKind, PortfolioSnapshot,
    ReconciliationAdjustmentFailureKind, ReconciliationAdjustmentResult,
    ReconciliationAdjustmentVariant, ReconciliationAdjustmentTargetKind,
    RunId, Sha256Digest, SettlementCurrency,
    canonical_portfolio_snapshot_bytes,
)
from ea.core.reconciliation import (
    _create_reconciliation_adjustment_outcome,
    _create_reconciliation_transaction,
    canonical_reconciliation_adjustment_outcome_bytes,
    canonical_reconciliation_transaction_bytes,
    reconciliation_adjustment_outcome_digest,
    reconciliation_transaction_digest,
)
from ea.portfolio import (
    create_phase1_portfolio_risk_refresh_authority,
    create_portfolio_ledger,
)
from ea.core.execution import (
    InstrumentExecutionSpec,
    InstrumentSpecId,
    InstrumentSpecSetId,
    build_instrument_spec_set,
)
from ea.core.identity import Instrument, VenueId
from ea.core.portfolio import LedgerAccountKind, LedgerPosting, InstrumentCommodity
from ea.core.risk import RiskPolicyId
from unit.test_portfolio_ledger import _spec_set

decimal_context = getcontext()
decimal_context.prec = int(os.environ["EA_TEST_DECIMAL_PRECISION"])
decimal_context.rounding = (
    ROUND_CEILING
    if os.environ["EA_TEST_DECIMAL_ROUNDING"] == "ceiling"
    else ROUND_FLOOR
)

run_id = RunId("12345678-1234-4234-8234-123456789abc")
spec_set = _spec_set()
instrument = spec_set.specifications[0].instrument
postings = (
    LedgerPosting(
        LedgerAccountKind.PORTFOLIO_POSITION,
        InstrumentCommodity(instrument),
        CanonicalDecimal("2"),
    ),
    LedgerPosting(
        LedgerAccountKind.EXTERNAL_INVENTORY,
        InstrumentCommodity(instrument),
        CanonicalDecimal("-2"),
    ),
)
# occurred/available must be exact datetimes; build the canonical vectors below
from datetime import UTC, datetime

TIME = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
transaction = _create_reconciliation_transaction(
    run_id=run_id,
    spec_set=spec_set,
    entry_id=EconomicId(run_id, EconomicOwnerKind.LEDGER_ENTRY, 1),
    ledger_sequence=1,
    adjustment_id=EconomicId(run_id, EconomicOwnerKind.RECONCILIATION_ADJUSTMENT, 1),
    authorization_sha256=Sha256Digest("11" * 32),
    observation_sha256=Sha256Digest("22" * 32),
    reconciliation_outcome_sha256=Sha256Digest("33" * 32),
    adjustment_command_sha256=Sha256Digest("44" * 32),
    variant=ReconciliationAdjustmentVariant.BALANCE_CORRECTION,
    target_kind=ReconciliationAdjustmentTargetKind.INSTRUMENT_POSITION,
    instrument=instrument,
    currency=None,
    local_amount=CanonicalDecimal("10"),
    observed_amount=CanonicalDecimal("12"),
    delta=CanonicalDecimal("2"),
    open_reconciliation_ref=None,
    previous_transaction_sha256=None,
    postings=postings,
    occurred_at=TIME,
    available_at=TIME,
)
outcome = _create_reconciliation_adjustment_outcome(
    run_id=run_id,
    adjustment_id=EconomicId(run_id, EconomicOwnerKind.RECONCILIATION_ADJUSTMENT, 1),
    authorization_sha256=Sha256Digest("11" * 32),
    observation_sha256=Sha256Digest("22" * 32),
    reconciliation_outcome_sha256=Sha256Digest("33" * 32),
    adjustment_command_sha256=Sha256Digest("44" * 32),
    result=ReconciliationAdjustmentResult.DUPLICATE,
    transaction=transaction,
    failure_kind=None,
    conflict_kind=None,
    before_snapshot_sha256=Sha256Digest("55" * 32),
    after_snapshot_sha256=Sha256Digest("55" * 32),
)
ledger = create_portfolio_ledger(run_id, spec_set)
refresh_authority = create_phase1_portfolio_risk_refresh_authority(
    run_id=run_id,
    spec_set=spec_set,
    policy_id=RiskPolicyId("phase1.test-risk.v1"),
    policy_sha256=Sha256Digest("66" * 32),
)
from ea.core.risk import _create_risk_state_snapshot
risk_state = _create_risk_state_snapshot(
    run_id=run_id,
    policy_id=RiskPolicyId("phase1.test-risk.v1"),
    policy_sha256=Sha256Digest("66" * 32),
    risk_state_version=0,
    halted=False,
    halt_reason=None,
    halt_causal_root_available_at=None,
    halt_dispatch_sequence=None,
    conflict_existing_intent_sha256=None,
    conflict_submitted_intent_sha256=None,
)
refresh = refresh_authority.create_refresh(
    snapshot=ledger.snapshot,
    risk_state=risk_state,
    dispatch_sequence=1,
    ordered_ledger_ack_frontier_sha256=Sha256Digest("77" * 32),
)

vectors = {
    "transaction_bytes": canonical_reconciliation_transaction_bytes(transaction).hex(),
    "transaction_digest": reconciliation_transaction_digest(transaction).value,
    "outcome_bytes": canonical_reconciliation_adjustment_outcome_bytes(outcome).hex(),
    "outcome_digest": reconciliation_adjustment_outcome_digest(outcome).value,
    "refresh_submission_permitted": refresh.submission_permitted,
    "refresh_exposure_length": len(refresh.exposure_sha256.value),
}
import json
print(json.dumps(vectors, sort_keys=True))
"""


def _run_child(env: dict[str, str]) -> dict[str, object]:
    import json

    completed = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        cwd=Path(__file__).resolve().parents[2],
        timeout=120,
    )
    return dict(json.loads(completed.stdout.strip().splitlines()[-1]))


def test_adjustment_and_refresh_vectors_ignore_process_environment() -> None:
    base_env = {**os.environ, "PYTHONPATH": "src:tests"}
    vectors = []
    for rounding, precision in (("ceiling", 24), ("floor", 40)):
        environment = {**base_env}
        environment["EA_TEST_DECIMAL_ROUNDING"] = rounding
        environment["EA_TEST_DECIMAL_PRECISION"] = str(precision)
        environment["PYTHONHASHSEED"] = "7"
        vectors.append(_run_child(environment))

    assert vectors[0] == vectors[1]
    transaction_digest = vectors[0]["transaction_digest"]
    assert isinstance(transaction_digest, str)
    assert len(transaction_digest) == 64
    assert vectors[0]["outcome_digest"] != vectors[0]["transaction_digest"]
    assert vectors[0]["refresh_submission_permitted"] is True
