from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = r"""
import json
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.environ["EA_HISTORICAL_RUNTIME_SOURCE_ROOT"])

from ea.core import (
    CanonicalDecimal,
    Instrument,
    InstrumentExecutionSpec,
    InstrumentSpecId,
    InstrumentSpecSetId,
    PriceDomain,
    ReplayWindow,
    RunId,
    SettlementCurrency,
    VenueId,
    build_instrument_spec_set,
)
from ea.data import (
    create_phase1_historical_market_data_source,
    create_phase1_historical_market_source_bridge,
    decode_phase1_ohlcv_csv,
)
from ea.runtime import create_phase1_historical_market_runtime

header = (
    "schema_version,venue,symbol,interval_start,interval_end,adjustment,"
    "open,high,low,close,volume,source,source_sequence,revision,available_at"
)
row = (
    "1,XNAS,AAPL,2026-01-02T09:30:00.000000Z,2026-01-02T09:31:00.000000Z,"
    "raw,100.0,101.0,99.0,100.5,10.0,cross.raw,0,0,2026-01-02T09:31:00.000000Z"
)
window = ReplayWindow(
    datetime(2026, 1, 2, 9, 0, tzinfo=UTC),
    datetime(2026, 1, 2, 10, 0, tzinfo=UTC),
)
dataset = decode_phase1_ohlcv_csv(
    ("\n".join((header, row)) + "\n").encode(),
    replay_window=window,
)
source = create_phase1_historical_market_data_source(dataset)
bridge = create_phase1_historical_market_source_bridge(source)
spec_set = build_instrument_spec_set(
    InstrumentSpecSetId("historical-runtime-cross-v1"),
    (
        InstrumentExecutionSpec(
            instrument=Instrument(VenueId("XNAS"), "AAPL"),
            specification_id=InstrumentSpecId("xnas-aapl-cross-v1"),
            price_quantum=CanonicalDecimal("0.01"),
            quantity_quantum=CanonicalDecimal("1"),
            settlement_currency=SettlementCurrency("USD"),
            currency_quantum=CanonicalDecimal("0.01"),
            contract_multiplier=CanonicalDecimal("1"),
            price_domain=PriceDomain.POSITIVE,
        ),
    ),
)
runtime = create_phase1_historical_market_runtime(
    run_id=RunId("12345678-1234-4234-8234-123456789abc"),
    spec_set=spec_set,
    source=bridge,
)
while not runtime.terminal_acknowledged:
    lease = runtime.pop()
    runtime.acknowledge(lease)
print(json.dumps({
    "digest": runtime.trace_digest.value,
    "records": [record.decode("ascii") for record in runtime.trace_records],
}, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
"""

EXPECTED_MARKET_RECORD = (
    '{"clock_now":"2026-01-02T09:31:00.000000Z",'
    '"committed_cursor_as_of":"2026-01-02T09:31:00.000000Z",'
    '"committed_event_count":1,'
    '"data_sha256":"e8e3747d60108a74c6d842fd7fc0f0b5a3ed1d26838e72483f69b0822bcbac8c",'
    '"dispatch_sequence":1,'
    '"root":{"adjustment":"raw","available_at":"2026-01-02T09:31:00.000000Z",'
    '"close_bits":"4059200000000000","high_bits":"4059400000000000",'
    '"interval_end":"2026-01-02T09:31:00.000000Z",'
    '"interval_start":"2026-01-02T09:30:00.000000Z","kind":"bar",'
    '"low_bits":"4058c00000000000","open_bits":"4059000000000000","revision":0,'
    '"source":"cross.raw","source_sequence":0,"symbol":"AAPL","venue":"XNAS",'
    '"volume_bits":"4024000000000000"},'
    '"root_order_key":["2026-01-02T09:31:00.000000Z",30,'
    '"2026-01-02T09:31:00.000000Z",0,"cross.raw",0,"XNAS","AAPL",'
    '"2026-01-02T09:30:00.000000Z","2026-01-02T09:31:00.000000Z","raw",0],'
    '"run_id":"12345678-1234-4234-8234-123456789abc",'
    '"schema":"ea.phase1-historical-runtime-trace.v1",'
    '"terminal_acknowledged":false}'
)
EXPECTED_TERMINAL_RECORD = (
    '{"clock_now":"2026-01-02T10:00:00.000000Z","committed_cursor_as_of":null,'
    '"committed_event_count":1,'
    '"data_sha256":"e8e3747d60108a74c6d842fd7fc0f0b5a3ed1d26838e72483f69b0822bcbac8c",'
    '"dispatch_sequence":2,'
    '"root":{"available_at":"2026-01-02T10:00:00.000000Z",'
    '"kind":"bounded_source_exhausted",'
    '"producer_namespace":"runtime.phase1.historical","producer_sequence":0,'
    '"run_id":"12345678-1234-4234-8234-123456789abc","type":"end_of_run"},'
    '"root_order_key":["2026-01-02T10:00:00.000000Z",50,0,'
    '"runtime.phase1.historical",0,"12345678-1234-4234-8234-123456789abc"],'
    '"run_id":"12345678-1234-4234-8234-123456789abc",'
    '"schema":"ea.phase1-historical-runtime-trace.v1",'
    '"terminal_acknowledged":true}'
)


def _run(
    cwd: Path,
    *,
    hash_seed: str,
    timezone: str,
    locale: str,
    noise: str,
) -> bytes:
    cwd.mkdir()
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": hash_seed,
            "TZ": timezone,
            "LC_ALL": locale,
            "LANG": locale,
            "EA_HISTORICAL_RUNTIME_TEST_NOISE": noise,
            "EA_HISTORICAL_RUNTIME_SOURCE_ROOT": str(Path(__file__).resolve().parents[2] / "src"),
        }
    )
    return subprocess.check_output(
        [sys.executable, "-I", "-B", "-c", SCRIPT],
        cwd=cwd,
        env=environment,
    )


def test_historical_runtime_trace_and_digest_are_cross_process_golden(
    tmp_path: Path,
) -> None:
    expected = _run(
        tmp_path / "first",
        hash_seed="1",
        timezone="UTC",
        locale="C",
        noise="first",
    )
    actual = _run(
        tmp_path / "second",
        hash_seed="987654321",
        timezone="Asia/Shanghai",
        locale="POSIX",
        noise="second",
    )

    assert actual == expected
    assert expected.endswith(b"\n")
    assert json.loads(expected) == {
        "digest": "fffeb6e455d950606335c173028f246d505ebdb85c268dfc3a116b552791322e",
        "records": [EXPECTED_MARKET_RECORD, EXPECTED_TERMINAL_RECORD],
    }
