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
    SourceNativeSequence,
    VenueId,
    build_instrument_spec_set,
    canonical_ledger_apply_outcome_bytes,
    canonical_ledger_transaction_bytes,
    canonical_portfolio_snapshot_bytes,
    create_fill,
    create_trade_execution_fact,
    ledger_apply_outcome_digest,
    ledger_transaction_digest,
    portfolio_snapshot_digest,
)
from ea.portfolio import create_portfolio_ledger

context = getcontext()
context.prec = int(os.environ["EA_TEST_DECIMAL_PRECISION"])
context.rounding = ROUND_CEILING
context.traps[Inexact] = True

run_id = RunId("12345678-1234-4234-8234-123456789abc")
occurred_at = datetime(2026, 1, 2, 9, 31, tzinfo=UTC)
currency = SettlementCurrency("USD")

def specification(venue, symbol):
    instrument = Instrument(VenueId(venue), symbol)
    return InstrumentExecutionSpec(
        instrument=instrument,
        specification_id=InstrumentSpecId(f"{venue.lower()}.{symbol.lower()}.v1"),
        price_quantum=CanonicalDecimal("0.001"),
        quantity_quantum=CanonicalDecimal("1"),
        settlement_currency=currency,
        currency_quantum=CanonicalDecimal("0.01"),
        contract_multiplier=CanonicalDecimal("1"),
        price_domain=PriceDomain.SIGNED,
    )

aapl = specification("XNAS", "AAPL")
ibm = specification("XNYS", "IBM")
items = (aapl, ibm) if os.environ["EA_TEST_INPUT_ORDER"] == "forward" else (ibm, aapl)
spec_set = build_instrument_spec_set(InstrumentSpecSetId("phase1.cross.v1"), items)
ledger = create_portfolio_ledger(run_id, spec_set)

def fill(specification, sequence, price):
    fact = create_trade_execution_fact(
        source_namespace=SourceNamespace(sorted({"sim.secondary", "sim.primary"})[0]),
        dedup_identity=SourceNativeSequence(10**5000 + sequence),
        occurred_at=occurred_at,
        provenance=FactProvenance(
            FactProvenanceId("phase1.simulator.v1"),
            Sha256Digest("3" * 64),
        ),
        spec_set=spec_set,
        instrument=specification.instrument,
        side=OrderSide.BUY if sequence % 2 else OrderSide.SELL,
        quantity=CanonicalDecimal(str(sequence)),
        price=CanonicalDecimal(price),
    )
    return create_fill(
        fill_id=EconomicId(run_id, EconomicOwnerKind.EXECUTION_FILL, sequence),
        fact=fact,
        spec_set=spec_set,
    )

first = ledger.apply_fill(fill(aapl, 1, "10.005"))
second = ledger.apply_fill(fill(ibm, 2, "-2.335"))
duplicate = ledger.apply_fill(fill(aapl, 1, "10.005"))

vectors = {
    "outcomes": [
        [
            canonical_ledger_apply_outcome_bytes(item).hex(),
            ledger_apply_outcome_digest(item).value,
        ]
        for item in (first, second, duplicate)
    ],
    "snapshot": [
        canonical_portfolio_snapshot_bytes(ledger.snapshot).hex(),
        portfolio_snapshot_digest(ledger.snapshot).value,
    ],
    "transactions": [
        [
            canonical_ledger_transaction_bytes(item).hex(),
            ledger_transaction_digest(item).value,
        ]
        for item in ledger.transactions
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


def test_portfolio_vectors_ignore_process_environment_and_input_order(
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
