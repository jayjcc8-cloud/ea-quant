"""Prepare real captured Coinbase daily OHLCV using the installed product decoder."""

import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from ea.core import ReplayWindow
from ea.data import decode_phase1_ohlcv_csv

scenarios, data, artifact, capture = map(Path, sys.argv[1:])
header = (scenarios / "prices.csv").read_text().splitlines()[0]
raw = sorted(json.loads(capture.read_bytes()))


def stamp(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


for name, candles in [("local-v3.csv", raw[:50]), ("local-v3-later.csv", raw[60:110])]:
    rows = [header]
    for i, (timestamp, low, high, opening, close, volume) in enumerate(candles):
        start = datetime.fromtimestamp(timestamp, UTC)
        end = start + timedelta(days=1)
        rows.append(
            f"1,COINBASE,BTC-USD,{stamp(start)},{stamp(end)},raw,"
            f"{opening},{high},{low},{close},{volume},coinbase.daily,{i},0,{stamp(end)}"
        )
    payload = ("\n".join(rows) + "\n").encode()
    (data / name).write_bytes(payload)
    if name == "local-v3.csv":
        (scenarios / name).write_bytes(payload)
        first = datetime.fromtimestamp(candles[0][0], UTC) + timedelta(days=1)
        end = datetime.fromtimestamp(candles[-1][0], UTC) + timedelta(days=1, microseconds=1)
        dataset = decode_phase1_ohlcv_csv(payload, replay_window=ReplayWindow(first, end))
        doc = yaml.safe_load((scenarios / "roundtrip.yaml").read_text())
        doc["schema_version"] = 5
        doc["data"] = {
            "path": name,
            "start_utc": stamp(first),
            "end_utc": stamp(end),
            "fingerprint": {
                "sha256": dataset.selection.fingerprint.sha256.value,
                "record_count": len(candles),
            },
        }
        doc["instrument"].update(
            venue="COINBASE",
            symbol="BTC-USD",
            specification_id="coinbase.btcusd.v1",
            specification_set_id="scenario.coinbase.btcusd.v1",
            quantity_quantum="0.00000001",
        )
        doc["funding"]["initial_cash"] = "1000000"
        doc["risk"] = {
            "max_order_quantity": "2",
            "max_position_quantity": "2",
            "max_notional": "1000000",
        }
        doc["execution"]["commission"]["commission_bps"] = "10"
        doc["strategy"] = {
            "id": "moving-average-crossover-bounded-v1",
            "version": 1,
            "action_contract": "V2",
            "position_lifecycle": "bounded-long-round-trips-v1",
            "max_round_trips": 256,
            "parameters": {"fast_window": 1, "slow_window": 2, "quantity": "1"},
            "source": {
                "kind": "local-package",
                "package_id": "example.ma.bounded",
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            },
        }
        (scenarios / "local-v3.yaml").write_text(yaml.safe_dump(doc))
