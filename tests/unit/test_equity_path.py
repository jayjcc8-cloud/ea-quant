from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from ea.product import (
    equity_path,
    generate_backtest_report,
    load_backtest_scenario,
    run_backtest_scenario,
)
from unit.test_backtest_report import _priced_scenario
from unit.test_backtest_single_run import _scenario


def test_exact_drawdown_ties_and_bounded_display() -> None:
    points = [
        equity_path.EquityPoint(i, str(i), value)
        for i, value in enumerate(["100", "120", "90", "120", "90", "110"])
    ]
    result = equity_path.analyze_points(lambda: iter(points))
    assert result["max_drawdown"] == dict(
        amount="30",
        ratio="0.25",
        peak_index=1,
        peak_time="1",
        peak_equity="120",
        trough_index=2,
        trough_time="2",
        trough_equity="90",
    )
    assert result["point_count"] == 6
    assert result["display_points"] == [p.document() for p in points]


def test_large_path_preserves_unsampled_extrema() -> None:
    points = [equity_path.EquityPoint(i, str(i), "100") for i in range(10001)]
    points[123] = equity_path.EquityPoint(123, "123", "200")
    points[4567] = equity_path.EquityPoint(4567, "4567", "10")
    result = equity_path.analyze_points(lambda: iter(points))
    assert result["point_count"] == 10001
    assert result["max_drawdown"]["ratio"] == "0.95"
    indices = [p["index"] for p in result["display_points"]]
    assert len(indices) <= 2048
    assert {0, 123, 4567, 10000} <= set(indices)
    assert indices == sorted(set(indices))
    assert result == equity_path.analyze_points(lambda: iter(points))


@pytest.mark.parametrize("flat", [True, False])
def test_real_attempt_path_is_deterministic_and_matches_report(tmp_path: Path, flat: bool) -> None:
    scenario = load_backtest_scenario(_priced_scenario(tmp_path / "input", flat=flat))
    attempt = run_backtest_scenario(scenario, tmp_path / "runs").output_directory
    report = generate_backtest_report(attempt, tmp_path / "report").report
    first = equity_path.generate_equity_path_analysis(attempt, report)
    second = equity_path.generate_equity_path_analysis(attempt, report)
    assert first.canonical_bytes == second.canonical_bytes
    d = json.loads(first.canonical_bytes)
    assert d["report_sha256"] == sha256(report.canonical_bytes).hexdigest()
    assert (
        d["display_points"][-1]["equity"]
        == json.loads(report.canonical_bytes)["economics"]["equity"]["amount"]
    )
    assert d["display_points"][0]["equity"] == "10000"
    assert d["point_count"] == 5  # anchor, initial bar, revision, fill bar, later bar
    assert d["max_drawdown"]["ratio"] == "0"


def test_commission_commits_at_fill_root(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(
        _scenario(
            tmp_path / "input",
            execution__commission={
                "policy": "deterministic-commission-v1",
                "commission_bps": "100",
            },
        )
    )
    attempt = run_backtest_scenario(scenario, tmp_path / "runs").output_directory
    report = generate_backtest_report(attempt, tmp_path / "report").report
    d = json.loads(equity_path.generate_equity_path_analysis(attempt, report).canonical_bytes)
    assert [p["equity"] for p in d["display_points"]] == ["10000", "10000", "10000", "9997.97"]
    assert d["max_drawdown"]["amount"] == "2.03"
    assert d["max_drawdown"]["ratio"] == "0.000203"
    assert d["max_drawdown"]["peak_index"] == 0
    assert d["max_drawdown"]["trough_index"] == 3


def test_web_v3_path_integrity_restart_and_legacy(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from ea.web.app import create_app
    from unit.test_web_api import WRITE_HEADERS, _settings, _validate, _wait

    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8765") as client:
        v = _validate(client, "bounded-long.yaml")
        response = client.post(
            "/api/backtests",
            headers=WRITE_HEADERS,
            json={
                "scenario_id": "bounded-long.yaml",
                "input_identity": v["input_identity"],
                "request_id": "path-analysis-001",
            },
        )
        job_id = response.json()["job_id"]
        job = _wait(client, job_id)
        assert job["schema"] == "ea.local-web-job.v3"
        path = client.get(f"/api/backtests/{job_id}/artifacts/equity-path.json")
        assert path.status_code == 200
        assert sha256(path.content).hexdigest() == job["equity_path_sha256"]
        assert set(p.name for p in (settings.workspace / "reports" / job_id).iterdir()) == {
            "report.json",
            "summary.txt",
            "equity-path.json",
        }
    (settings.scenario_root / "prices.csv").unlink()
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8765") as client:
        assert (
            client.get(f"/api/backtests/{job_id}/artifacts/equity-path.json").content
            == path.content
        )
        artifact = settings.workspace / "reports" / job_id / "equity-path.json"
        artifact.write_bytes(path.content + b" ")
        assert client.get(f"/api/backtests/{job_id}/artifacts/equity-path.json").status_code == 409
    from ea.web.service import _canonical_json, _decode_job

    for schema in ("ea.local-web-job.v1", "ea.local-web-job.v2"):
        old = dict(job, schema=schema)
        old.pop("equity_path_sha256")
        old.pop("parameters", None)
        old.pop("strategy_descriptor", None)
        if schema.endswith("v1"):
            for field in ("created_at", "input_snapshot", "input_sha256", "attempt_id"):
                old.pop(field)
        assert _decode_job(_canonical_json(old)).document()["schema"] == schema


@pytest.mark.parametrize(
    "prices,expected,amount,ratio,peak,trough",
    [
        (
            [100, 100, 120, 90, 120, 90, 110],
            ["10000", "10000", "10000", "10040", "9980", "10040", "9980", "10020"],
            "60",
            "0.00597609561752988",
            3,
            4,
        ),
        ([100, 100, 100], ["10000"] * 4, "0", "0", 0, 0),
        ([100, 100, 110, 120], ["10000", "10000", "10000", "10020", "10040"], "0", "0", 0, 0),
    ],
)
def test_known_ohlcv_path(
    tmp_path: Path,
    prices: list[int],
    expected: list[str],
    amount: str,
    ratio: str,
    peak: int,
    trough: int,
) -> None:
    import csv
    from datetime import UTC, datetime, timedelta

    import yaml

    from ea.core import ReplayWindow
    from ea.data import decode_phase1_ohlcv_csv

    source = _scenario(tmp_path / "input")
    data = source.parent / "prices.csv"
    headers = data.read_text().splitlines()[0].split(",")
    start = datetime(2026, 1, 2, 9, 30, tzinfo=UTC)

    def time(i: int) -> str:
        return (start + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    with data.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for index, price in enumerate(prices):
            writer.writerow(
                [
                    1,
                    "XNAS",
                    "AAPL",
                    time(index),
                    time(index + 1),
                    "raw",
                    price,
                    price,
                    price,
                    price,
                    10,
                    "fixture.raw",
                    index,
                    0,
                    time(index + 1),
                ]
            )
    window = ReplayWindow(start + timedelta(minutes=1), start + timedelta(minutes=len(prices) + 1))
    fingerprint = decode_phase1_ohlcv_csv(
        data.read_bytes(), replay_window=window
    ).selection.fingerprint
    scenario = yaml.safe_load(source.read_text())
    scenario["data"].update(
        start_utc=time(1),
        end_utc=time(len(prices) + 1),
        fingerprint={"record_count": fingerprint.record_count, "sha256": fingerprint.sha256.value},
    )
    source.write_text(yaml.safe_dump(scenario))
    attempt = run_backtest_scenario(
        load_backtest_scenario(source), tmp_path / "runs"
    ).output_directory
    report = generate_backtest_report(attempt, tmp_path / "report").report
    d = json.loads(equity_path.generate_equity_path_analysis(attempt, report).canonical_bytes)
    assert [p["equity"] for p in d["display_points"]] == expected
    dd = d["max_drawdown"]
    assert (dd["amount"], dd["ratio"], dd["peak_index"], dd["trough_index"]) == (
        amount,
        ratio,
        peak,
        trough,
    )
    assert dd["peak_time"] == time(max(1, peak))
    assert dd["trough_time"] == time(max(1, trough))


def test_path_failure_does_not_publish_partial_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    import ea.web.service as service
    from ea.web.app import create_app
    from unit.test_web_api import WRITE_HEADERS, _settings, _validate, _wait

    def fail(*args: object) -> None:
        raise ValueError("path invariant")

    monkeypatch.setattr(service, "generate_equity_path_analysis", fail)
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8765") as client:
        v = _validate(client, "bounded-long.yaml")
        response = client.post(
            "/api/backtests",
            headers=WRITE_HEADERS,
            json={
                "scenario_id": "bounded-long.yaml",
                "input_identity": v["input_identity"],
                "request_id": "path-failure-001",
            },
        )
        job = _wait(client, response.json()["job_id"])
        assert job["status"] == "failed"
        assert job["report_ready"] is False
        assert job["equity_path_sha256"] is None
        assert not (settings.workspace / "reports" / job["job_id"]).exists()
        assert (
            client.get(f"/api/backtests/{job['job_id']}/artifacts/equity-path.json").status_code
            == 409
        )


def test_mismatched_report_and_changed_source_fail_closed(tmp_path: Path) -> None:
    from ea.product.reporting import BacktestReportError, BacktestReportV1, _canonical_json

    scenario = load_backtest_scenario(_priced_scenario(tmp_path / "input"))
    attempt = run_backtest_scenario(scenario, tmp_path / "runs").output_directory
    report = generate_backtest_report(attempt, tmp_path / "report").report
    document: Any = report.document
    document["economics"]["equity"]["amount"] = "1"
    changed = BacktestReportV1(_canonical_json(document) + b"\n", report.summary_bytes)
    with pytest.raises(BacktestReportError):
        equity_path.generate_equity_path_analysis(attempt, changed)
    scenario.data_path.write_bytes(scenario.data_path.read_bytes() + b"\n")
    with pytest.raises(BacktestReportError):
        equity_path.generate_equity_path_analysis(attempt, report)


def test_display_boundary_and_half_even_ratio() -> None:
    def points(count: int) -> Callable[[], Iterator[equity_path.EquityPoint]]:
        return lambda: (equity_path.EquityPoint(i, str(i), "100") for i in range(count))

    assert len(equity_path.analyze_points(points(2048))["display_points"]) == 2048
    displayed = equity_path.analyze_points(points(2049))["display_points"]
    assert {p["index"] for p in displayed} == set(range(2049)) - {2046}
    sequence = [equity_path.EquityPoint(0, "0", "6"), equity_path.EquityPoint(1, "1", "5")]
    assert (
        equity_path.analyze_points(lambda: iter(sequence))["max_drawdown"]["ratio"]
        == "0.166666666666666667"
    )


@pytest.mark.parametrize("schema", ["ea.local-web-job.v1", "ea.local-web-job.v2"])
def test_legacy_completed_job_reopens_without_rewriting_or_regeneration(
    tmp_path: Path, schema: str
) -> None:
    from fastapi.testclient import TestClient

    from ea.web.app import create_app
    from ea.web.service import _canonical_json
    from unit.test_web_api import WRITE_HEADERS, _settings, _validate, _wait

    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8765") as client:
        validated = _validate(client, "bounded-long.yaml")
        created = client.post(
            "/api/backtests",
            headers=WRITE_HEADERS,
            json={
                "scenario_id": "bounded-long.yaml",
                "input_identity": validated["input_identity"],
                "request_id": "legacy-path-0001",
            },
        )
        job = _wait(client, created.json()["job_id"])
        job_id = job["job_id"]
        report = client.get(f"/api/backtests/{job_id}/report").content
    index = settings.workspace / "jobs" / f"{job_id}.json"
    persisted = json.loads(index.read_bytes())
    persisted["schema"] = schema
    persisted.pop("equity_path_sha256")
    if schema.endswith("v1"):
        for key in ("created_at", "input_snapshot", "input_sha256", "attempt_id"):
            persisted.pop(key)
    index.write_bytes(_canonical_json(persisted))
    original_index = index.read_bytes()
    (settings.workspace / "reports" / job_id / "equity-path.json").unlink()
    (settings.scenario_root / "prices.csv").unlink()
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8765") as client:
        assert client.get(f"/api/backtests/{job_id}/report").content == report
        assert client.get(f"/api/backtests/{job_id}").json()["report_ready"] is True
        assert client.get("/api/backtests").json()["jobs"][0]["schema"] == schema
        response = client.get(f"/api/backtests/{job_id}/artifacts/equity-path.json")
        assert response.status_code == 409
        assert "legacy run" in response.text
    assert index.read_bytes() == original_index
