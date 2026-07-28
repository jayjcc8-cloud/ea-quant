from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SCRIPT = """
import json
import os
from datetime import UTC, datetime
from decimal import Inexact, ROUND_CEILING, getcontext

from ea.core import (
    CanonicalDecimal,
    EconomicId,
    EconomicOwnerKind,
    ExecutionPolicyId,
    ExecutionPolicyRef,
    Instrument,
    InstrumentExecutionSpec,
    InstrumentRiskLimit,
    InstrumentSpecId,
    InstrumentSpecSetId,
    OrderSide,
    PortfolioSnapshot,
    PriceDomain,
    RiskHaltReason,
    RiskPolicyId,
    RunId,
    SettlementCurrency,
    Sha256Digest,
    TargetLineageRef,
    VenueId,
    build_instrument_spec_set,
    canonical_phase1_risk_policy_bytes,
    canonical_risk_evaluation_evidence_bytes,
    canonical_risk_state_snapshot_bytes,
    create_order_intent,
    create_phase1_risk_policy,
    phase1_risk_policy_digest,
    risk_evaluation_evidence_digest,
    risk_state_snapshot_digest,
)
from ea.risk import create_phase1_risk_authority

context = getcontext()
context.prec = int(os.environ["EA_TEST_DECIMAL_PRECISION"])
context.rounding = ROUND_CEILING
context.traps[Inexact] = True

run_id = RunId("12345678-1234-4234-8234-123456789abc")
timestamp = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
currency = SettlementCurrency("USD")

def specification(venue, symbol):
    return InstrumentExecutionSpec(
        instrument=Instrument(VenueId(venue), symbol),
        specification_id=InstrumentSpecId(f"{venue.lower()}.{symbol.lower()}.v1"),
        price_quantum=CanonicalDecimal("0.01"),
        quantity_quantum=CanonicalDecimal("1"),
        settlement_currency=currency,
        currency_quantum=CanonicalDecimal("0.01"),
        contract_multiplier=CanonicalDecimal("1"),
        price_domain=PriceDomain.POSITIVE,
    )

aapl = specification("XNAS", "AAPL")
ibm = specification("XNYS", "IBM")
items = (aapl, ibm) if os.environ["EA_TEST_INPUT_ORDER"] == "forward" else (ibm, aapl)
spec_set = build_instrument_spec_set(InstrumentSpecSetId("phase1.cross-risk.v1"), items)
execution_policy = ExecutionPolicyRef(
    ExecutionPolicyId("phase1.execution.v1"),
    Sha256Digest("1" * 64),
)
limits = [
    InstrumentRiskLimit(aapl.instrument, CanonicalDecimal("5"), CanonicalDecimal("10")),
    InstrumentRiskLimit(ibm.instrument, CanonicalDecimal("3"), CanonicalDecimal("7")),
]
if os.environ["EA_TEST_INPUT_ORDER"] == "reverse":
    limits.reverse()
policy = create_phase1_risk_policy(
    policy_id=RiskPolicyId("phase1.risk.v1"),
    spec_set=spec_set,
    execution_policy=execution_policy,
    instrument_limits=limits,
)
authority = create_phase1_risk_authority(
    run_id=run_id,
    spec_set=spec_set,
    execution_policy=execution_policy,
    policy=policy,
)
snapshot = PortfolioSnapshot(
    run_id=run_id,
    instrument_spec_set_id=spec_set.identifier,
    instrument_spec_set_sha256=policy.instrument_spec_set_sha256,
    snapshot_version=0,
    ledger_sequence=0,
    last_entry_id=None,
    last_transaction_sha256=None,
    cash_balances=(),
    position_balances=(),
    rounding_balances=(),
    unresolved_fills=(),
)

def identity(owner, sequence):
    return EconomicId(run_id, owner, sequence)

def intent(instrument, sequence, quantity, dispatch_sequence=None):
    return create_order_intent(
        run_id=run_id,
        intent_id=identity(EconomicOwnerKind.PORTFOLIO_INTENT, sequence),
        correlation_id=identity(EconomicOwnerKind.STRATEGY_SIGNAL, 1),
        target_lineage=TargetLineageRef(
            identity(EconomicOwnerKind.PORTFOLIO_TARGET, 1),
            Sha256Digest("2" * 64),
        ),
        instrument=instrument,
        side=OrderSide.BUY,
        quantity=CanonicalDecimal(quantity),
        portfolio_snapshot_version=0,
        causal_root_available_at=timestamp,
        dispatch_sequence=sequence if dispatch_sequence is None else dispatch_sequence,
        spec_set=spec_set,
        execution_policy=execution_policy,
    )

uint64_boundary = 1 << 64
very_large = (1 << 4096) + 73
allowed_intent = intent(aapl.instrument, 1, "2", uint64_boundary)
allowed = authority.evaluate(allowed_intent, snapshot)
resized = authority.evaluate(intent(ibm.instrument, 2, "5", very_large), snapshot)
replay = authority.evaluate(allowed_intent, snapshot)
authority.engage_halt(RiskHaltReason.KILL_SWITCH, timestamp, very_large)

conflict_authority = create_phase1_risk_authority(
    run_id=run_id,
    spec_set=spec_set,
    execution_policy=execution_policy,
    policy=policy,
)
conflict_authority.evaluate(intent(aapl.instrument, 1, "2", 1), snapshot)
try:
    conflict_authority.evaluate(
        intent(aapl.instrument, 1, "3", uint64_boundary),
        snapshot,
    )
except ValueError:
    pass
else:
    raise AssertionError("identity conflict must fail")

vectors = {
    "evidence": [
        [
            canonical_risk_evaluation_evidence_bytes(item).hex(),
            risk_evaluation_evidence_digest(item).value,
        ]
        for item in (allowed.evidence, resized.evidence)
    ],
    "policy": [
        canonical_phase1_risk_policy_bytes(policy).hex(),
        phase1_risk_policy_digest(policy).value,
    ],
    "replay_identity": replay is allowed,
    "public_halt_state": [
        canonical_risk_state_snapshot_bytes(authority.risk_state).hex(),
        risk_state_snapshot_digest(authority.risk_state).value,
    ],
    "conflict_halt_state": [
        canonical_risk_state_snapshot_bytes(conflict_authority.risk_state).hex(),
        risk_state_snapshot_digest(conflict_authority.risk_state).value,
    ],
}
print(json.dumps(vectors, sort_keys=True, separators=(",", ":")))
"""


def _run(
    cwd: Path,
    *,
    hash_seed: str,
    timezone: str,
    locale: str,
    precision: str,
    input_order: str,
) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": hash_seed,
            "TZ": timezone,
            "LC_ALL": locale,
            "LANG": locale,
            "EA_TEST_DECIMAL_PRECISION": precision,
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


def test_risk_vectors_ignore_process_environment_and_input_order(tmp_path: Path) -> None:
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()
    first = _run(
        first_cwd,
        hash_seed="1",
        timezone="UTC",
        locale="C",
        precision="1",
        input_order="forward",
    )
    second = _run(
        second_cwd,
        hash_seed="987654321",
        timezone="Asia/Shanghai",
        locale="POSIX",
        precision="37",
        input_order="reverse",
    )
    assert first == second
