from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from ea.cli.app import app


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
    assert runner.invoke(
        app,
        ["backtest", "run", "--output-root", str(output_root)],
    ).exit_code == 0

    repeated = runner.invoke(
        app,
        ["backtest", "run", "--output-root", str(output_root)],
    )

    assert repeated.exit_code == 2
    assert "demo input error: output attempt already exists" in repeated.output
    assert "Traceback" not in repeated.output
