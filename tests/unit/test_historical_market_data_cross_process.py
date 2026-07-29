from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = r"""
import json
from datetime import UTC, datetime

from ea.core import ReplayWindow
from ea.data import create_phase1_historical_market_data_source, decode_phase1_ohlcv_csv
from ea.data.fingerprint import canonical_market_data_record_bytes

header = (
    "schema_version,venue,symbol,interval_start,interval_end,adjustment,"
    "open,high,low,close,volume,source,source_sequence,revision,available_at"
)
rows = (
    "1,XNAS,AAPL,2026-01-02T09:31:00.000000Z,2026-01-02T09:32:00.000000Z,"
    "raw,100.5,102.0,100.0,101.5,12.0,cross.raw,2,0,2026-01-02T09:32:00.000000Z",
    "1,XNAS,AAPL,2026-01-02T09:30:00.000000Z,2026-01-02T09:31:00.000000Z,"
    "raw,100.0,101.0,99.0,100.5,10.0,cross.raw,0,0,2026-01-02T09:31:00.000000Z",
    "1,XNAS,MSFT,2026-01-02T09:30:00.000000Z,2026-01-02T09:31:00.000000Z,"
    "raw,200.0,202.0,199.0,201.0,20.0,cross.raw,1,0,2026-01-02T09:31:00.000000Z",
)
content = ("\n".join((header, *rows)) + "\n").encode()
window = ReplayWindow(
    datetime(2026, 1, 2, 9, 0, tzinfo=UTC),
    datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
)
dataset = decode_phase1_ohlcv_csv(content, replay_window=window)
source = create_phase1_historical_market_data_source(dataset)

class Clock:
    def now(self):
        return datetime(2026, 1, 2, 10, 0, tzinfo=UTC)

cursor, admitted = source.admit(clock=Clock(), cursor=None)
print(json.dumps({
    "raw": dataset.source_bytes_sha256.value,
    "semantic": dataset.selection.fingerprint.sha256.value,
    "records": [
        canonical_market_data_record_bytes(event).hex()
        for event in dataset.selection.events
    ],
    "admitted": [
        canonical_market_data_record_bytes(event).hex()
        for event in admitted
    ],
    "cursor_as_of": cursor.as_of.isoformat(),
    "next": source.next_available_at(cursor),
}, sort_keys=True, separators=(",", ":"), default=str))
"""


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
            "EA_HISTORICAL_TEST_NOISE": noise,
        }
    )
    return subprocess.check_output(
        [sys.executable, "-I", "-B", "-c", SCRIPT],
        cwd=cwd,
        env=environment,
    )


def test_historical_evidence_is_cross_process_invariant(tmp_path: Path) -> None:
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
