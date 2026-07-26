from __future__ import annotations

import os
import random
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    ExternalFactId,
    FactProvenance,
    FactProvenanceId,
    Instrument,
    InstrumentExecutionSpec,
    InstrumentSpecId,
    InstrumentSpecSetId,
    OrderSide,
    PriceDomain,
    RunId,
    SettlementCurrency,
    Sha256Digest,
    SourceNamespace,
    TargetLineageRef,
    VenueId,
    VenueOrderId,
    allow_order_intent,
    build_instrument_spec_set,
    canonical_effective_order_intent_bytes,
    canonical_execution_approval_bytes,
    canonical_execution_fact_bytes,
    canonical_execution_fact_ingress_bytes,
    canonical_execution_request_bytes,
    canonical_fill_bytes,
    canonical_order_bytes,
    canonical_order_intent_bytes,
    canonical_risk_decision_bytes,
    create_execution_fact_ingress,
    create_fill,
    create_order,
    create_order_intent,
    create_trade_execution_fact,
    effective_order_intent_digest,
    execution_approval_digest,
    execution_fact_digest,
    execution_fact_ingress_digest,
    execution_request_digest,
    fill_digest,
    order_digest,
    order_intent_digest,
    risk_decision_digest,
)

context = getcontext()
context.prec = int(os.environ["EA_TEST_DECIMAL_PRECISION"])
context.rounding = ROUND_CEILING
context.traps[Inexact] = True

run_id = RunId("12345678-1234-4234-8234-123456789abc")
causal_time = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)

def spec(symbol):
    instrument = Instrument(VenueId("XNAS"), symbol)
    return InstrumentExecutionSpec(
        instrument=instrument,
        specification_id=InstrumentSpecId(f"xnas.{symbol.lower()}.v1"),
        price_quantum=CanonicalDecimal("0.01"),
        quantity_quantum=CanonicalDecimal("1"),
        settlement_currency=SettlementCurrency("USD"),
        currency_quantum=CanonicalDecimal("0.01"),
        contract_multiplier=CanonicalDecimal("1"),
        price_domain=PriceDomain.POSITIVE,
    )

aapl = spec("AAPL")
msft = spec("MSFT")
spec_items = (aapl, msft) if os.environ["EA_TEST_INPUT_ORDER"] == "forward" else (msft, aapl)
spec_set = build_instrument_spec_set(
    InstrumentSpecSetId("phase1.us-equities.v1"),
    spec_items,
)
target = TargetLineageRef(
    EconomicId(run_id, EconomicOwnerKind.PORTFOLIO_TARGET, 2),
    Sha256Digest("1" * 64),
)
policy = ExecutionPolicyRef(
    ExecutionPolicyId("phase1.next-bar-close.v1"),
    Sha256Digest("2" * 64),
)
intent = create_order_intent(
    run_id=run_id,
    intent_id=EconomicId(run_id, EconomicOwnerKind.PORTFOLIO_INTENT, 3),
    correlation_id=EconomicId(run_id, EconomicOwnerKind.STRATEGY_SIGNAL, 1),
    target_lineage=target,
    instrument=aapl.instrument,
    side=OrderSide.BUY,
    quantity=CanonicalDecimal("10"),
    portfolio_snapshot_version=5,
    causal_root_available_at=causal_time,
    dispatch_sequence=7,
    spec_set=spec_set,
    execution_policy=policy,
)
decision = allow_order_intent(
    decision_id=EconomicId(run_id, EconomicOwnerKind.RISK_DECISION, 4),
    approval_id=EconomicId(run_id, EconomicOwnerKind.RISK_APPROVAL, 5),
    intent=intent,
    spec_set=spec_set,
    risk_state_version=11,
)
approval = decision.approval
assert approval is not None
order = create_order(
    order_id=EconomicId(run_id, EconomicOwnerKind.EXECUTION_ORDER, 6),
    intent=intent,
    decision=decision,
    spec_set=spec_set,
)
source = SourceNamespace(sorted({"sim.secondary", "sim.primary"})[0])
fact = create_trade_execution_fact(
    source_namespace=source,
    dedup_identity=ExternalFactId('trade"\\\\42'),
    occurred_at=causal_time,
    provenance=FactProvenance(
        FactProvenanceId("phase1.simulator.v1"),
        Sha256Digest("3" * 64),
    ),
    spec_set=spec_set,
    instrument=aapl.instrument,
    side=OrderSide.BUY,
    quantity=CanonicalDecimal("10"),
    price=CanonicalDecimal("101.25"),
    client_submission_key=order.client_submission_key,
    venue_order_id=VenueOrderId("venue-order-7"),
    order_id=order.order_id,
    correlation_id=order.correlation_id,
    causation_id=order.order_id,
)
ingress = create_execution_fact_ingress(
    available_at=causal_time,
    source_namespace=source,
    ingress_sequence=10**5000,
    fact=fact,
)
fill = create_fill(
    fill_id=EconomicId(run_id, EconomicOwnerKind.EXECUTION_FILL, 12),
    fact=fact,
    spec_set=spec_set,
)

vectors = {
    "approval": [
        canonical_execution_approval_bytes(approval).hex(),
        execution_approval_digest(approval).value,
    ],
    "decision": [
        canonical_risk_decision_bytes(decision).hex(),
        risk_decision_digest(decision).value,
    ],
    "effective": [
        canonical_effective_order_intent_bytes(intent, intent.quantity).hex(),
        effective_order_intent_digest(intent, intent.quantity).value,
    ],
    "fact": [
        canonical_execution_fact_bytes(fact).hex(),
        execution_fact_digest(fact).value,
    ],
    "fill": [canonical_fill_bytes(fill).hex(), fill_digest(fill).value],
    "ingress": [
        canonical_execution_fact_ingress_bytes(ingress).hex(),
        execution_fact_ingress_digest(ingress).value,
    ],
    "intent": [
        canonical_order_intent_bytes(intent).hex(),
        order_intent_digest(intent).value,
    ],
    "order": [canonical_order_bytes(order).hex(), order_digest(order).value],
    "request": [
        canonical_execution_request_bytes(order).hex(),
        execution_request_digest(order).value,
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


def test_execution_message_vectors_ignore_process_environment_and_input_order(
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


def test_complete_vectors_ignore_bounded_shuffled_concurrent_schedules(
    tmp_path: Path,
) -> None:
    jobs: list[tuple[Path, str, str, str, str, str]] = [
        (
            tmp_path / f"schedule-{index}",
            str(1000 + index),
            "UTC" if index % 2 == 0 else "Asia/Shanghai",
            "C" if index % 3 else "POSIX",
            str(1 + index * 7),
            "forward" if index % 2 == 0 else "reverse",
        )
        for index in range(8)
    ]
    for cwd, *_ in jobs:
        cwd.mkdir()
    random.Random(390047).shuffle(jobs)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(
                _run,
                cwd,
                hash_seed=hash_seed,
                timezone=timezone,
                locale=locale,
                precision=precision,
                input_order=input_order,
            )
            for cwd, hash_seed, timezone, locale, precision, input_order in jobs
        ]
        results = [future.result() for future in as_completed(futures)]

    assert len(set(results)) == 1
