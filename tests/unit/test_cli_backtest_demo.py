from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import ea.product.backtest as backtest_module
from ea.cli.app import app
from ea.product import load_backtest_scenario, run_backtest_scenario
from unit.test_backtest_single_run import _scenario


class _AbruptInterruption(BaseException):
    pass


def test_backtest_run_uses_accepted_path(tmp_path: Path) -> None:
    output_root = (tmp_path / "results").resolve()
    result = CliRunner().invoke(
        app,
        ["backtest", "run", "--output-root", str(output_root)],
    )

    assert result.exit_code == 0
    assert "offline demo:" in result.stdout
    output_directory = output_root / "phase1-demo-v1"
    assert f"result: {output_directory / 'result.json'}" in result.stdout
    report = json.loads((output_directory / "result.json").read_bytes())
    assert report["run_status"] == "completed"
    assert report["trade_outcome"] == "filled"
    assert report["run_outcome"] == "filled"


def test_backtest_run_rejects_hidden_mode_override(tmp_path: Path) -> None:
    output_root = (tmp_path / "results").resolve()
    result = CliRunner().invoke(
        app,
        [
            "backtest",
            "run",
            "--output-root",
            str(output_root),
            "--mode",
            "risk_reject",
        ],
    )

    assert result.exit_code != 0
    assert "no such option" in result.output.lower()


def test_backtest_run_refuses_an_existing_attempt_without_traceback(tmp_path: Path) -> None:
    output_root = (tmp_path / "results").resolve()
    runner = CliRunner()
    assert (
        runner.invoke(
            app,
            ["backtest", "run", "--output-root", str(output_root)],
        ).exit_code
        == 0
    )

    repeated = runner.invoke(
        app,
        ["backtest", "run", "--output-root", str(output_root)],
    )

    assert repeated.exit_code == 2
    assert "demo input error: output attempt already exists" in repeated.output
    assert "Traceback" not in repeated.output


def test_backtest_validate_accepts_strict_scenario_without_creating_run(
    tmp_path: Path,
) -> None:
    scenario = _scenario(tmp_path / "input")
    output_root = tmp_path / "runs"

    result = CliRunner().invoke(
        app,
        ["backtest", "validate", "--scenario", str(scenario)],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "scenario valid: BacktestScenario v1"
    assert not output_root.exists()


def test_backtest_scenario_run_emits_result_without_traceback(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path / "input")
    output_root = (tmp_path / "runs").resolve()

    result = CliRunner().invoke(
        app,
        [
            "backtest",
            "run",
            "--scenario",
            str(scenario),
            "--output-root",
            str(output_root),
        ],
    )

    assert result.exit_code == 0
    assert "backtest: success" in result.stdout
    assert "Traceback" not in result.output
    reports = tuple(output_root.glob("*/result.json"))
    assert len(reports) == 1
    assert json.loads(reports[0].read_bytes())["status"] == "success"


def test_backtest_cli_is_reproducible_across_fresh_processes(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path / "input")
    output_root = (tmp_path / "runs").resolve()
    command = [
        sys.executable,
        "-c",
        "from ea.cli.app import app; app()",
        "backtest",
        "run",
        "--scenario",
        str(scenario),
        "--output-root",
        str(output_root),
    ]

    first = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=False)
    second = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=False)

    assert (first.returncode, second.returncode) == (0, 0)
    reports = [json.loads(path.read_bytes()) for path in output_root.glob("*/result.json")]
    assert len(reports) == 2
    assert reports[0]["run_id"] != reports[1]["run_id"]
    assert reports[0]["lineage_sha256"] == reports[1]["lineage_sha256"]
    assert reports[0]["semantic_outcome_sha256"] == reports[1]["semantic_outcome_sha256"]


def test_backtest_resume_cli_continues_same_attempt_without_identity_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    output_root = (tmp_path / "runs").resolve()

    def interrupt(stage: str) -> None:
        if stage == "funding_durable":
            raise _AbruptInterruption

    monkeypatch.setattr(backtest_module, "_TEST_INTERRUPT", interrupt)
    with pytest.raises(_AbruptInterruption):
        run_backtest_scenario(scenario, output_root)
    monkeypatch.setattr(backtest_module, "_TEST_INTERRUPT", None)
    attempt = next(output_root.iterdir())

    result = CliRunner().invoke(
        app,
        ["backtest", "resume", "--run-dir", str(attempt)],
    )

    assert result.exit_code == 0
    assert "backtest resume: success" in result.stdout
    assert f"result: {attempt / 'result.json'}" in result.stdout
    assert json.loads((attempt / "result.json").read_bytes())["run_id"] == attempt.name
    assert "Traceback" not in result.output


def test_backtest_cli_sanitizes_handled_internal_failure_and_points_to_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(tmp_path / "input")

    def fail(**_kwargs: object) -> object:
        raise RuntimeError("sensitive internal detail")

    monkeypatch.setattr(backtest_module, "_execute", fail)
    result = CliRunner().invoke(
        app,
        [
            "backtest",
            "run",
            "--scenario",
            str(scenario),
            "--output-root",
            str((tmp_path / "runs").resolve()),
        ],
    )

    assert result.exit_code == 3
    assert "backtest failed closed" in result.output
    assert "evidence:" in result.output
    assert "sensitive internal detail" not in result.output
    assert "Traceback" not in result.output
