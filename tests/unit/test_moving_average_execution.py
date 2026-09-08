from __future__ import annotations

import csv
import io
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from ea.core import ReplayWindow
from ea.data import decode_phase1_ohlcv_csv
from ea.product import generate_backtest_report, load_backtest_scenario, run_backtest_scenario


def ma_scenario(tmp_path: Path, prices: list[int], *, quantity: str = "3") -> Path:
    tmp_path.mkdir(parents=True)
    root = Path("examples/web-scenarios")
    path = tmp_path / "moving-average-entry.yaml"
    shutil.copy(root / path.name, path)
    rows = list(csv.DictReader((root / "moving-average-entry.csv").read_text().splitlines()))
    for row, price in zip(rows, prices, strict=True):
        row.update(open=str(price), high=str(price + 1), low=str(price - 1), close=str(price))
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    payload = text.getvalue().encode()
    (tmp_path / "moving-average-entry.csv").write_bytes(payload)
    document = yaml.safe_load(path.read_text())
    dataset = decode_phase1_ohlcv_csv(
        payload,
        replay_window=ReplayWindow(
            datetime.fromisoformat(document["data"]["start_utc"]),
            datetime.fromisoformat(document["data"]["end_utc"]),
        ),
    )
    document["data"]["fingerprint"] = {
        "sha256": dataset.selection.fingerprint.sha256.value,
        "record_count": dataset.selection.fingerprint.record_count,
    }
    document["strategy"]["parameters"] = {
        "fast_window": 2,
        "slow_window": 3,
        "target_quantity": quantity,
    }
    path.write_text(yaml.safe_dump(document))
    return path


def execute(path: Path, root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    run = run_backtest_scenario(load_backtest_scenario(path), output_root=root / "runs")
    generate_backtest_report(run.output_directory, root / "report")
    return json.loads((run.output_directory / "result.json").read_bytes()), json.loads(
        (root / "report/report.json").read_bytes()
    )


@pytest.mark.parametrize("prices", [[105, 104, 103, 102, 101, 100], [105, 104, 103, 102, 101, 200]])
def test_nontrigger_or_last_bar_only_trigger_has_no_order(
    tmp_path: Path, prices: list[int]
) -> None:
    path = ma_scenario(tmp_path / "input", prices)
    result, report = execute(path, tmp_path / "output")
    assert result["order"] is None and result["fill"] is None
    assert report["economics"]["fees"]["amount"] == "0"
    assert report["economics"]["equity"]["amount"] == "10000"


def test_ma_is_deterministic_no_lookahead_and_uses_fee_ledger(tmp_path: Path) -> None:
    path = ma_scenario(tmp_path / "input", [100, 101, 102, 103, 104, 105])
    first, report = execute(path, tmp_path / "one")
    second, second_report = execute(path, tmp_path / "two")
    assert first["fill"]["price"] == "103"
    assert first["fill"]["quantity"] == "3"
    assert report["economics"]["fees"]["amount"] == "3.09"
    assert report["economics"] == second_report["economics"]
    assert first["run_id"] != second["run_id"]
    changed = ma_scenario(tmp_path / "future", [100, 101, 102, 103, 200, 300])
    third, _ = execute(changed, tmp_path / "three")
    assert {k: first["fill"][k] for k in ("quantity", "price", "side")} == {
        k: third["fill"][k] for k in ("quantity", "price", "side")
    }


def test_ma_target_uses_existing_risk_resize(tmp_path: Path) -> None:
    path = ma_scenario(tmp_path / "input", [100, 101, 102, 103, 104, 105], quantity="100")
    result, report = execute(path, tmp_path / "output")
    assert result["risk"]["decision"] == "resize"
    assert result["fill"]["quantity"] == "5"
    assert report["economics"]["fees"]["amount"] == "5.15"
