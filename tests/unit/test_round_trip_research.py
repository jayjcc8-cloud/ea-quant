from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ea.product import generate_backtest_report, load_backtest_scenario, run_backtest_scenario
from ea.product.equity_path import generate_equity_path_analysis
from ea.web.service import WebService
from unit.test_single_round_trip_v1 import closed_scenario


def test_v2_path_uses_each_acknowledged_cash_balance(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(closed_scenario(tmp_path / "input"))
    attempt = run_backtest_scenario(scenario, tmp_path / "runs").output_directory
    report: Any = generate_backtest_report(attempt, tmp_path / "report").report
    path = json.loads(generate_equity_path_analysis(attempt, report).canonical_bytes)
    assert path["schema"] == "ea.backtest-equity-path.v2"
    assert [point["equity"] for point in path["display_points"]] == [
        "10000",
        "10000",
        "10000",
        "9999.8",
        "10016.8",
        "10036.56",
        "10036.56",
    ]
    assert path["max_drawdown"]["ratio"] == "0.00002"
    assert path["display_points"][-1]["equity"] == report.document["economics"]["equity"]["amount"]


def test_v4_web_runs_and_reopens_persisted_evidence(tmp_path: Path) -> None:
    path = closed_scenario(tmp_path / "scenarios")
    service = WebService(path.parent, tmp_path / "workspace")
    summary: Any = service.validate_scenario(path.name)
    assert summary["strategy_descriptor"]["action_contract"] == "V2"
    service.start()
    job, _ = service.create_job(
        scenario_id=path.name, input_identity=summary["input_identity"], request_id="round-trip"
    )
    service.stop()
    record = service.get_job(job.job_id)
    assert record.status == "succeeded"
    assert record.schema == "ea.local-web-job.v4"
    report = service.report(job.job_id)
    analysis = service.artifact(job.job_id, "equity-path.json")
    assert json.loads(report)["schema"] == "ea.backtest-report.v2"
    assert json.loads(analysis)["schema"] == "ea.backtest-equity-path.v2"
    (path.parent / "prices.csv").unlink()
    reopened = WebService(path.parent, tmp_path / "workspace")
    reopened.start()
    assert reopened.report(job.job_id) == report
    assert reopened.artifact(job.job_id, "equity-path.json") == analysis
    reopened.stop()


def test_v2_open_and_never_entered_paths(tmp_path: Path) -> None:
    import yaml

    for name, parameters, outcome, fills in (
        ("open", {"hold_root_count": 100}, "OPEN_AT_END", 1),
        ("flat", {"entry_delay": 100}, "FLAT_INITIAL", 0),
    ):
        path = closed_scenario(tmp_path / name)
        document = yaml.safe_load(path.read_text())
        document["strategy"]["parameters"].update(parameters)
        path.write_text(yaml.safe_dump(document))
        attempt = run_backtest_scenario(
            load_backtest_scenario(path), tmp_path / "runs"
        ).output_directory
        report: Any = generate_backtest_report(attempt, tmp_path / f"{name}-report").report
        analysis = json.loads(generate_equity_path_analysis(attempt, report).canonical_bytes)
        assert report.document["economics"]["position_outcome"] == outcome
        assert report.document["economics"]["counts"]["fills"] == fills
        assert (
            analysis["display_points"][-1]["equity"]
            == report.document["economics"]["equity"]["amount"]
        )
        if fills == 0:
            assert all(p["equity"] == "10000" for p in analysis["display_points"])
            assert analysis["max_drawdown"]["ratio"] == "0"


def test_v4_job_cannot_be_relabelled_as_legacy(tmp_path: Path) -> None:
    import pytest

    from ea.web.service import WebBoundaryError, _canonical_json, _decode_job

    path = closed_scenario(tmp_path / "scenarios")
    service = WebService(path.parent, tmp_path / "workspace")
    service.start()
    try:
        summary: Any = service.validate_scenario(path.name)
        job, _ = service.create_job(
            scenario_id=path.name, input_identity=summary["input_identity"], request_id="round-trip"
        )
    finally:
        service.stop()
    document = service.get_job(job.job_id).document()
    document["schema"] = "ea.local-web-job.v3"
    with pytest.raises(WebBoundaryError):
        _decode_job(_canonical_json(document))
