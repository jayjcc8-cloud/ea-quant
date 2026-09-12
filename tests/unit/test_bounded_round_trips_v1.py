from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from ea.core import ReplayWindow
from ea.data import decode_phase1_ohlcv_csv
from ea.product import generate_backtest_report, load_backtest_scenario, run_backtest_scenario
from unit.test_single_round_trip_v1 import round_trip_scenario


def bounded_scenario(tmp_path: Path, *, bars: int = 12, limit: int = 3) -> Path:
    path = round_trip_scenario(tmp_path)
    csv = tmp_path / "prices.csv"
    header = csv.read_text().splitlines()[0]
    start = datetime(2026, 1, 2, 9, 30, tzinfo=UTC)

    def stamp(t: datetime) -> str:
        return t.strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    rows = [header]
    for i in range(bars):
        price = 100 + i
        rows.append(
            f"1,XNAS,AAPL,{stamp(start + timedelta(minutes=i))},"
            f"{stamp(start + timedelta(minutes=i + 1))},raw,"
            f"{price},{price},{price},{price},10,fixture.raw,{i + 1},0,"
            f"{stamp(start + timedelta(minutes=i + 1))}"
        )
    csv.write_text("\n".join(rows) + "\n")
    window = ReplayWindow(start + timedelta(minutes=1), start + timedelta(minutes=bars + 1))
    data = decode_phase1_ohlcv_csv(csv.read_bytes(), replay_window=window)
    doc = yaml.safe_load(path.read_text())
    doc["schema_version"] = 5
    doc["strategy"].update(
        id="bounded-long-hold-roots-v1",
        position_lifecycle="bounded-long-round-trips-v1",
        max_round_trips=limit,
    )
    doc["data"]["end_utc"] = stamp(start + timedelta(minutes=bars + 1))
    doc["data"]["fingerprint"] = {
        "sha256": data.selection.fingerprint.sha256.value,
        "record_count": data.selection.fingerprint.record_count,
    }
    path.write_text(yaml.safe_dump(doc))
    return path


def test_v5_binds_explicit_limit(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(bounded_scenario(tmp_path))
    assert scenario.schema_version == 5
    assert json.loads(scenario.canonical_bytes)["strategy"]["max_round_trips"] == 3


def test_three_round_trips_formal_economics(tmp_path: Path) -> None:
    attempt = run_backtest_scenario(
        load_backtest_scenario(bounded_scenario(tmp_path / "input")), tmp_path / "runs"
    ).output_directory
    result = json.loads((attempt / "result.json").read_bytes())
    assert result["schema"] == "ea.backtest-single-run-result.v4"
    assert result["completed_round_trips"] == 3
    assert result["ledger_sequence"] == 7
    report: Any = generate_backtest_report(attempt, tmp_path / "report").report.document
    assert report["schema"] == "ea.backtest-report.v3"
    economics = report["economics"]
    assert len(economics["trades"]) == 3
    assert economics["realized_pnl"]["amount"] == "10.73"
    assert economics["fees"]["amount"] == "1.27"
    assert economics["equity"]["amount"] == "10010.73"
    assert economics["open_position"] is None


@pytest.mark.parametrize(
    "bars,trades,outcome,fills",
    [
        (1, 0, "FLAT_NO_TRADE", 0),
        (4, 1, "FLAT_AFTER_TRADES", 2),
        (10, 2, "OPEN_AT_END", 5),
    ],
)
def test_bounded_terminal_states(
    tmp_path: Path, bars: int, trades: int, outcome: str, fills: int
) -> None:
    attempt = run_backtest_scenario(
        load_backtest_scenario(bounded_scenario(tmp_path / "in", bars=bars)), tmp_path / "runs"
    ).output_directory
    result = json.loads((attempt / "result.json").read_bytes())
    assert result["completed_round_trips"] == trades
    assert result["position_outcome"] == outcome
    assert result["ledger_sequence"] == 1 + fills
    report: Any = generate_backtest_report(attempt, tmp_path / "report").report.document
    assert len(report["economics"]["trades"]) == trades
    assert (report["economics"]["open_position"] is not None) == (fills % 2 == 1)


@pytest.mark.parametrize("limit", [0, 257, True, "3", 1.5])
def test_invalid_bound_rejected(tmp_path: Path, limit: object) -> None:
    from ea.product.scenario import BacktestScenarioError

    path = bounded_scenario(tmp_path)
    doc = yaml.safe_load(path.read_text())
    doc["strategy"]["max_round_trips"] = limit
    path.write_text(yaml.safe_dump(doc))
    with pytest.raises(BacktestScenarioError):
        load_backtest_scenario(path)


def test_reentry_over_limit_fails_closed(tmp_path: Path) -> None:
    from ea.product import BacktestRunFailure

    with pytest.raises(BacktestRunFailure):
        run_backtest_scenario(
            load_backtest_scenario(bounded_scenario(tmp_path / "in", limit=1)), tmp_path / "runs"
        )
    attempt = next((tmp_path / "runs").iterdir())
    assert not (attempt / "result.json").exists()


@pytest.mark.parametrize(
    "stage,occurrence",
    [
        ("funding_durable", 1),
        ("dispatch_durable", 1),
        ("dispatch_durable", 2),
        ("dispatch_durable", 3),
        ("dispatch_durable", 4),
        ("reconciliation_durable", 1),
        ("terminal_before_publication", 1),
    ],
)
def test_multi_dispatch_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, occurrence: int
) -> None:
    from uuid import uuid4

    from ea.core import RunId
    from ea.product import backtest as b
    from unit.test_backtest_resume import _AbruptInterruption

    loaded = load_backtest_scenario(bounded_scenario(tmp_path / "input"))
    run_id = RunId(str(uuid4()))
    baseline = run_backtest_scenario(loaded, tmp_path / "baseline", run_id=run_id).output_directory
    seen = 0

    def interrupt(current: str) -> None:
        nonlocal seen
        if current == stage:
            seen += 1
            if seen == occurrence:
                raise _AbruptInterruption()

    publish = b._publish_success
    monkeypatch.setattr(b, "_TEST_INTERRUPT", interrupt)
    if stage == "terminal_before_publication":

        def stop(*args: Any) -> None:
            raise _AbruptInterruption()

        monkeypatch.setattr(b, "_publish_success", stop)
    with pytest.raises(_AbruptInterruption):
        run_backtest_scenario(loaded, tmp_path / "interrupted", run_id=run_id)
    monkeypatch.setattr(b, "_TEST_INTERRUPT", None)
    monkeypatch.setattr(b, "_publish_success", publish)
    attempt = tmp_path / "interrupted" / run_id.value
    resumed = b.resume_backtest_attempt(attempt)
    for name in ("result.json", "audit.jsonl", "funding.json"):
        assert (attempt / name).read_bytes() == (baseline / name).read_bytes()
    assert b.resume_backtest_attempt(attempt) == resumed
    assert generate_backtest_report(attempt, tmp_path / "rr").report.canonical_bytes == (
        generate_backtest_report(baseline, tmp_path / "br").report.canonical_bytes
    )


@pytest.mark.parametrize("field", ["trades", "open_position", "completed_round_trips"])
def test_v3_reader_rejects_fabricated_trade_evidence(tmp_path: Path, field: str) -> None:
    from ea.product import BacktestReportError, BacktestReportV3

    attempt = run_backtest_scenario(
        load_backtest_scenario(bounded_scenario(tmp_path / "in")), tmp_path / "runs"
    ).output_directory
    report: Any = generate_backtest_report(attempt, tmp_path / "report").report
    doc: Any = report.document
    doc["economics"][field] = [] if field == "trades" else {} if field == "open_position" else 0
    with pytest.raises(BacktestReportError):
        BacktestReportV3(
            json.dumps(doc, sort_keys=True, separators=(",", ":")).encode() + b"\n",
            report.summary_bytes,
        )


@pytest.mark.parametrize("decision", ["reject", "resize"])
def test_bounded_exit_rejects_partial_or_rejected_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
) -> None:
    from unit import test_single_round_trip_v1 as legacy

    monkeypatch.setattr(legacy, "closed_scenario", bounded_scenario)
    legacy.test_exit_risk_failure_prevents_second_order(tmp_path, monkeypatch, decision)


@pytest.mark.parametrize("action", ["ENTER_LONG", "EXIT_LONG"])
def test_bounded_illegal_position_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    from unit import test_single_round_trip_v1 as legacy

    monkeypatch.setattr(legacy, "closed_scenario", bounded_scenario)
    legacy.test_invalid_position_action_fails_closed(tmp_path, monkeypatch, action)


def test_repeated_entry_resize_and_identity(tmp_path: Path) -> None:
    path = bounded_scenario(tmp_path / "in")
    doc = yaml.safe_load(path.read_text())
    doc["risk"]["max_order_quantity"] = "1"
    path.write_text(yaml.safe_dump(doc))
    attempt = run_backtest_scenario(
        load_backtest_scenario(path), tmp_path / "runs"
    ).output_directory
    result = json.loads((attempt / "result.json").read_bytes())
    legs = result["execution_legs"]
    assert [leg["order"]["order_id"]["owner_sequence"] for leg in legs] == list(range(1, 7))
    assert [leg["fill"]["fill_id"]["owner_sequence"] for leg in legs] == list(range(1, 7))
    assert [leg["fill"]["quantity"] for leg in legs] == ["1"] * 6
    assert [leg["risk"]["decision"] for leg in legs] == ["resize", "allow"] * 3
    report: Any = generate_backtest_report(attempt, tmp_path / "report").report.document
    assert report["economics"]["completed_round_trips"] == 3


def test_bounded_entry_rejection_has_no_fill(tmp_path: Path) -> None:
    from ea.product import BacktestRunFailure

    path = bounded_scenario(tmp_path / "in")
    doc = yaml.safe_load(path.read_text())
    doc["risk"]["max_notional"] = "1"
    path.write_text(yaml.safe_dump(doc))
    with pytest.raises(BacktestRunFailure):
        run_backtest_scenario(load_backtest_scenario(path), tmp_path / "runs")
    attempt = next((tmp_path / "runs").iterdir())
    assert not (attempt / "result.json").exists()
    assert (
        json.loads((attempt / "failure.json").read_bytes())["last_durable_frontier"]
        == "funding_durable"
    )


@pytest.mark.parametrize("field", ["net_pnl", "equity", "fees", "gross_unrealized_pnl"])
def test_v3_rejects_fabricated_aggregates(tmp_path: Path, field: str) -> None:
    from ea.product import BacktestReportError, BacktestReportV3

    attempt = run_backtest_scenario(
        load_backtest_scenario(bounded_scenario(tmp_path / "in")), tmp_path / "runs"
    ).output_directory
    report = generate_backtest_report(attempt, tmp_path / "report").report
    doc: Any = report.document
    doc["economics"][field]["amount"] = "999999"
    with pytest.raises(BacktestReportError):
        BacktestReportV3(
            json.dumps(doc, sort_keys=True, separators=(",", ":")).encode() + b"\n",
            report.summary_bytes,
        )
