from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ea.product.trade_analytics import summarize_trades, trade_analytics


def test_closed_trade_statistics_exclude_open_position() -> None:
    result = summarize_trades(["10", "-5", "0", "20"], ["60", "120", "180", "240"])
    assert result == {
        "closed_trades": 4,
        "wins": 2,
        "losses": 1,
        "breakeven": 1,
        "win_rate": "0.5",
        "average_win": "15",
        "average_loss": "-5",
        "payoff_ratio": "3",
        "average_holding_seconds": "150",
    }


@pytest.mark.parametrize("pnls", [[], ["0"], ["10"], ["-5"]])
def test_missing_denominators_are_unavailable(pnls: list[str]) -> None:
    result = summarize_trades(pnls, ["1"] * len(pnls))
    assert result["payoff_ratio"] is None
    if not pnls:
        assert result["win_rate"] is None
        assert result["average_holding_seconds"] is None


def test_real_repeated_report_analytics(tmp_path: Path) -> None:
    from hashlib import sha256

    from ea.product import generate_backtest_report, load_backtest_scenario, run_backtest_scenario
    from unit.test_bounded_round_trips_v1 import bounded_scenario

    attempt = run_backtest_scenario(
        load_backtest_scenario(bounded_scenario(tmp_path / "input")), tmp_path / "runs"
    ).output_directory
    report = generate_backtest_report(attempt, tmp_path / "report").report
    result = trade_analytics(report.canonical_bytes)
    assert result["report_sha256"] == sha256(report.canonical_bytes).hexdigest()
    assert result["run_id"] == json.loads(report.canonical_bytes)["run_id"]
    assert result["currency"] == "USD"
    assert result["closed_trades"] == 3
    assert result["wins"] == 3
    assert result["average_holding_seconds"] == "120"
    assert result["payoff_ratio"] is None
    assert result["has_open_position"] is False
    assert len(result["trades"]) == 3


def test_web_analytics_reopens_without_csv_and_rejects_corruption(tmp_path: Path) -> None:
    from ea.web.service import ReportUnavailableError, WebService
    from unit.test_bounded_round_trips_v1 import bounded_scenario

    path = bounded_scenario(tmp_path / "scenarios")
    service = WebService(path.parent, tmp_path / "workspace")
    summary: Any = service.validate_scenario(path.name)
    service.start()
    job, _ = service.create_job(
        scenario_id=path.name, input_identity=summary["input_identity"], request_id="analytics"
    )
    service.stop()
    first = service.trade_analytics(job.job_id)
    (path.parent / "prices.csv").unlink()
    reopened = WebService(path.parent, tmp_path / "workspace")
    reopened.start()
    try:
        assert reopened.trade_analytics(job.job_id) == first
        report_path = reopened.reports_dir / job.job_id / "report.json"
        report_path.write_bytes(report_path.read_bytes() + b" ")
        with pytest.raises(ReportUnavailableError):
            reopened.trade_analytics(job.job_id)
    finally:
        reopened.stop()


@pytest.mark.parametrize("bars, closed, opened", [(1, 0, False), (10, 2, True)])
def test_actual_flat_and_open_reports(tmp_path: Path, bars: int, closed: int, opened: bool) -> None:
    from ea.product import generate_backtest_report, load_backtest_scenario, run_backtest_scenario
    from unit.test_bounded_round_trips_v1 import bounded_scenario

    attempt = run_backtest_scenario(
        load_backtest_scenario(bounded_scenario(tmp_path / "input", bars=bars)), tmp_path / "runs"
    ).output_directory
    report = generate_backtest_report(attempt, tmp_path / "report").report
    result = trade_analytics(report.canonical_bytes)
    assert result["closed_trades"] == closed
    assert result["has_open_position"] is opened
    assert len(result["trades"]) == closed
    if not closed:
        assert result["win_rate"] is None


def test_single_round_trip_report_is_supported(tmp_path: Path) -> None:
    from ea.product import generate_backtest_report, load_backtest_scenario, run_backtest_scenario
    from unit.test_single_round_trip_v1 import closed_scenario

    attempt = run_backtest_scenario(
        load_backtest_scenario(closed_scenario(tmp_path / "input")), tmp_path / "runs"
    ).output_directory
    report = generate_backtest_report(attempt, tmp_path / "report").report
    result = trade_analytics(report.canonical_bytes)
    assert result["closed_trades"] == result["wins"] == 1
    assert result["average_win"] == "36.56"


def test_average_rounding_is_independent_of_ambient_decimal_context() -> None:
    from decimal import ROUND_DOWN, localcontext

    expected = summarize_trades(["1", "0", "0"], ["0", "0", "1"])
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_DOWN
        assert summarize_trades(["1", "0", "0"], ["0", "0", "1"]) == expected
    assert expected["win_rate"] == "0.333333333333333333"


def test_invalid_durations_are_not_published() -> None:
    with pytest.raises(ValueError):
        summarize_trades(["1"], ["-1"])
    with pytest.raises(ValueError):
        summarize_trades(["1"], [])


def test_fees_can_turn_rising_prices_into_losing_trades(tmp_path: Path) -> None:
    import yaml

    from ea.product import generate_backtest_report, load_backtest_scenario, run_backtest_scenario
    from unit.test_bounded_round_trips_v1 import bounded_scenario

    path = bounded_scenario(tmp_path / "input")
    document = yaml.safe_load(path.read_text())
    document["execution"]["commission"]["commission_bps"] = "10000"
    path.write_text(yaml.safe_dump(document))
    attempt = run_backtest_scenario(
        load_backtest_scenario(path), tmp_path / "runs"
    ).output_directory
    report = generate_backtest_report(attempt, tmp_path / "report").report
    result = trade_analytics(report.canonical_bytes)
    assert result["closed_trades"] == result["losses"] == 3
    assert result["wins"] == 0
    assert result["win_rate"] == "0"
    assert result["average_win"] is None
