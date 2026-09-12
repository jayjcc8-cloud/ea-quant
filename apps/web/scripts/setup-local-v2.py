"""Build external local V2 acceptance inputs using the installed product."""

import hashlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from ea.core import ReplayWindow
from ea.data import decode_phase1_ohlcv_csv

scenarios, data, artifact = map(Path, sys.argv[1:])
header = (scenarios / "prices.csv").read_text().splitlines()[0]
for day, name, prices in [
    (2, "local-v2.csv", [10, 9, 11, 12, 13, 8, 7, 6]),
    (3, "local-v2-later.csv", [20, 19, 21, 22, 23, 18, 17, 16]),
]:
    start = datetime(2026, 1, day, 9, 31, tzinfo=UTC)

    def stamp(t: datetime) -> str:
        return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    rows = [header]
    for i, price in enumerate(prices):
        t = start + timedelta(minutes=i)
        rows.append(
            f"1,XNAS,AAPL,{stamp(t - timedelta(minutes=1))},{stamp(t)},raw,"
            f"{price},{price},{price},{price},10,fixture.raw,{i},0,{stamp(t)}"
        )
    payload = ("\n".join(rows) + "\n").encode()
    (data / name).write_bytes(payload)
    if day == 2:
        (scenarios / name).write_bytes(payload)
        end = start + timedelta(minutes=len(prices))
        dataset = decode_phase1_ohlcv_csv(payload, replay_window=ReplayWindow(start, end))
        doc = yaml.safe_load((scenarios / "roundtrip.yaml").read_text())
        doc["data"] = {
            "path": name,
            "start_utc": stamp(start),
            "end_utc": stamp(end),
            "fingerprint": {
                "sha256": dataset.selection.fingerprint.sha256.value,
                "record_count": len(prices),
            },
        }
        doc["strategy"] = {
            "id": "moving-average-crossover-round-trip-v1",
            "version": 1,
            "action_contract": "V2",
            "position_lifecycle": "single-long-round-trip-v1",
            "parameters": {"fast_window": 1, "slow_window": 2, "quantity": "2"},
            "source": {
                "kind": "local-package",
                "package_id": "example.ma",
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            },
        }
        (scenarios / "local-v2.yaml").write_text(yaml.safe_dump(doc))
