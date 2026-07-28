from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SCRIPT = r"""
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Inexact, ROUND_CEILING, ROUND_FLOOR, getcontext

from ea.core import (
    CanonicalDecimal,
    EconomicId,
    EconomicOwnerKind,
    ExecutionFactKind,
    ExecutionPolicyId,
    ExecutionPolicyRef,
    ExternalFactId,
    FactProvenance,
    FactProvenanceId,
    Instrument,
    InstrumentExecutionSpec,
    InstrumentRiskLimit,
    InstrumentSpecId,
    InstrumentSpecSetId,
    OrderSide,
    PortfolioSnapshot,
    PriceDomain,
    RiskPolicyId,
    RunId,
    SettlementCurrency,
    Sha256Digest,
    SourceNamespace,
    TargetLineageRef,
    VenueId,
    VenueOrderId,
    build_instrument_spec_set,
    canonical_execution_fact_processing_outcome_bytes,
    canonical_fill_bytes,
    canonical_order_projection_snapshot_bytes,
    create_execution_fact_ingress,
    create_lifecycle_execution_fact,
    create_order_intent,
    create_phase1_risk_policy,
    create_trade_execution_fact,
    execution_fact_processing_outcome_digest,
    fill_digest,
    instrument_spec_set_digest,
    order_projection_snapshot_digest,
    prepare_bounded_runtime_roots,
)
from ea.execution import (
    create_phase1_execution_fact_authority,
    create_phase1_order_authority,
)
from ea.risk import create_phase1_risk_authority
from ea.runtime import (
    create_deterministic_root_queue,
    create_phase1_execution_fact_ingress_authority,
)

context = getcontext()
context.prec = int(os.environ["EA_TEST_DECIMAL_PRECISION"])
context.rounding = (
    ROUND_CEILING if os.environ["EA_TEST_DECIMAL_ROUNDING"] == "ceiling" else ROUND_FLOOR
)
context.traps[Inexact] = True

run_id = RunId("12345678-1234-4234-8234-123456789abc")
time = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
instrument = Instrument(VenueId("XNAS"), "AAPL")
source_namespace = SourceNamespace("sim.primary")
provenance = FactProvenance(
    FactProvenanceId("phase1.simulator.v1"),
    Sha256Digest("2" * 64),
)
spec_set = build_instrument_spec_set(
    InstrumentSpecSetId("phase1.cross-fact.v1"),
    (
        InstrumentExecutionSpec(
            instrument=instrument,
            specification_id=InstrumentSpecId("xnas.aapl.v1"),
            price_quantum=CanonicalDecimal("0.01"),
            quantity_quantum=CanonicalDecimal("1"),
            settlement_currency=SettlementCurrency("USD"),
            currency_quantum=CanonicalDecimal("0.01"),
            contract_multiplier=CanonicalDecimal("1"),
            price_domain=PriceDomain.POSITIVE,
        ),
    ),
)
execution_policy = ExecutionPolicyRef(
    ExecutionPolicyId("phase1.execution.v1"),
    Sha256Digest("1" * 64),
)
policy = create_phase1_risk_policy(
    policy_id=RiskPolicyId("phase1.risk.v1"),
    spec_set=spec_set,
    execution_policy=execution_policy,
    instrument_limits=(
        InstrumentRiskLimit(
            instrument,
            CanonicalDecimal("10"),
            CanonicalDecimal("100"),
        ),
    ),
)
risk = create_phase1_risk_authority(
    run_id=run_id,
    spec_set=spec_set,
    execution_policy=execution_policy,
    policy=policy,
)
orders = create_phase1_order_authority(
    run_id=run_id,
    spec_set=spec_set,
    execution_policy=execution_policy,
    risk_policy=policy,
    risk_result_verifier=risk,
)
snapshot = PortfolioSnapshot(
    run_id=run_id,
    instrument_spec_set_id=spec_set.identifier,
    instrument_spec_set_sha256=instrument_spec_set_digest(spec_set),
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

def make_intent(sequence):
    return create_order_intent(
        run_id=run_id,
        intent_id=identity(EconomicOwnerKind.PORTFOLIO_INTENT, sequence),
        correlation_id=identity(EconomicOwnerKind.STRATEGY_SIGNAL, sequence),
        target_lineage=TargetLineageRef(
            identity(EconomicOwnerKind.PORTFOLIO_TARGET, sequence),
            Sha256Digest(f"{sequence:x}".rjust(64, "0")),
        ),
        instrument=instrument,
        side=OrderSide.BUY,
        quantity=CanonicalDecimal("2"),
        portfolio_snapshot_version=0,
        causal_root_available_at=time,
        dispatch_sequence=sequence,
        spec_set=spec_set,
        execution_policy=execution_policy,
    )

created_orders = []
for sequence in (1, 2):
    intent = make_intent(sequence)
    created_orders.append(orders.create_order(intent, risk.evaluate(intent, snapshot)))
first_order, second_order = created_orders

trade_fact = create_trade_execution_fact(
    source_namespace=source_namespace,
    dedup_identity=ExternalFactId("overfill"),
    occurred_at=time,
    provenance=provenance,
    spec_set=spec_set,
    instrument=instrument,
    side=OrderSide.BUY,
    quantity=CanonicalDecimal("3"),
    price=CanonicalDecimal("100"),
    client_submission_key=first_order.client_submission_key,
    venue_order_id=VenueOrderId("venue-first"),
    order_id=first_order.order_id,
    correlation_id=first_order.correlation_id,
    causation_id=first_order.order_id,
)
conflict_fact = create_lifecycle_execution_fact(
    kind=ExecutionFactKind.ACKNOWLEDGEMENT,
    source_namespace=source_namespace,
    dedup_identity=ExternalFactId("binding-conflict"),
    occurred_at=time + timedelta(seconds=1),
    provenance=provenance,
    instrument=instrument,
    client_submission_key=second_order.client_submission_key,
    venue_order_id=VenueOrderId("venue-conflict"),
    order_id=first_order.order_id,
    correlation_id=first_order.correlation_id,
    causation_id=first_order.order_id,
)
trade_ingress = create_execution_fact_ingress(
    available_at=time,
    source_namespace=source_namespace,
    ingress_sequence=1,
    fact=trade_fact,
)
conflict_ingress = create_execution_fact_ingress(
    available_at=time + timedelta(seconds=1),
    source_namespace=source_namespace,
    ingress_sequence=2,
    fact=conflict_fact,
)
input_order = (
    (trade_ingress, conflict_ingress)
    if os.environ["EA_TEST_INPUT_ORDER"] == "forward"
    else (conflict_ingress, trade_ingress)
)
source = create_phase1_execution_fact_ingress_authority(
    run_id=run_id,
    spec_set=spec_set,
    source_namespace=source_namespace,
)
for ingress in input_order:
    source.register_ingress(ingress)
queue = create_deterministic_root_queue(
    run_id=run_id,
    spec_set=spec_set,
    plan=prepare_bounded_runtime_roots(input_order),
    fact_issuance_verifiers=(source,),
)
facts = create_phase1_execution_fact_authority(
    run_id=run_id,
    spec_set=spec_set,
    order_verifier=orders,
    dispatch_verifier=queue,
)
outcomes = []
dispatch_sequences = []
for _ in range(2):
    lease = queue.pop()
    dispatch_sequences.append(lease.dispatch_sequence)
    outcomes.append(facts.process_ingress(lease.root))
    queue.acknowledge(lease)

projection = facts.projection_for_order(first_order.order_id)
assert projection is not None
vectors = {
    "dispatch_sequences": dispatch_sequences,
    "fills": [
        [canonical_fill_bytes(fill).hex(), fill_digest(fill).value]
        for fill in facts.fills
    ],
    "outcomes": [
        [
            canonical_execution_fact_processing_outcome_bytes(outcome).hex(),
            execution_fact_processing_outcome_digest(outcome).value,
            [anomaly.value for anomaly in outcome.anomalies],
        ]
        for outcome in outcomes
    ],
    "projection": [
        canonical_order_projection_snapshot_bytes(projection).hex(),
        order_projection_snapshot_digest(projection).value,
    ],
    "observed_quantity": facts.observed_quantity_for_order(first_order.order_id).text,
    "halt_requested": facts.halt_requested,
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
    rounding: str,
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
            "EA_TEST_DECIMAL_ROUNDING": rounding,
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


def test_fact_authority_vectors_ignore_process_environment_and_input_order(
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
        precision="2",
        rounding="ceiling",
        input_order="forward",
    )
    second = _run(
        second_cwd,
        hash_seed="987654321",
        timezone="Asia/Shanghai",
        locale="POSIX",
        precision="37",
        rounding="floor",
        input_order="reverse",
    )
    assert first == second
