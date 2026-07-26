from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SCRIPT = """
import json
import os
from decimal import Inexact, ROUND_CEILING, getcontext

from ea.core import (
    CanonicalDecimal,
    Instrument,
    InstrumentExecutionSpec,
    InstrumentSpecId,
    InstrumentSpecSetId,
    PriceDomain,
    SettlementCurrency,
    VenueId,
    build_instrument_spec_set,
    canonical_instrument_spec_set_bytes,
    instrument_spec_set_digest,
    settle_execution,
)

context = getcontext()
context.prec = int(os.environ["EA_TEST_DECIMAL_PRECISION"])
context.rounding = ROUND_CEILING
context.traps[Inexact] = True

def spec(symbol):
    instrument = Instrument(VenueId("XNAS"), symbol)
    return InstrumentExecutionSpec(
        instrument=instrument,
        specification_id=InstrumentSpecId(f"xnas.{symbol.lower()}.v1"),
        price_quantum=CanonicalDecimal("0.001"),
        quantity_quantum=CanonicalDecimal("1"),
        settlement_currency=SettlementCurrency("USD"),
        currency_quantum=CanonicalDecimal("0.01"),
        contract_multiplier=CanonicalDecimal("1"),
        price_domain=PriceDomain.POSITIVE,
    )

aapl = spec("AAPL")
msft = spec("MSFT")
items = (aapl, msft) if os.environ["EA_TEST_INPUT_ORDER"] == "forward" else (msft, aapl)
spec_set = build_instrument_spec_set(InstrumentSpecSetId("phase1.us-equities.v1"), items)
settlement = settle_execution(
    spec_set,
    aapl.instrument,
    CanonicalDecimal("1.015"),
    CanonicalDecimal("3"),
)
print(
    json.dumps(
        {
            "bytes": canonical_instrument_spec_set_bytes(spec_set).hex(),
            "digest": instrument_spec_set_digest(spec_set).value,
            "amount": settlement.amount.text,
            "residual": settlement.rounding_residual.text,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
)
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


def test_economic_vectors_ignore_process_environment_context_and_input_order(
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
