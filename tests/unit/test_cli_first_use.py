from __future__ import annotations

from importlib import metadata
from pathlib import Path

import pytest
from rich.text import Text
from typer.testing import CliRunner

import ea.cli.app as cli_module
from ea.cli.app import app

# The README first-use contract tests were retired with the README rewrite in #244 ("docs: make
# Mac-local Paper startup the README quick start"). That rewrite removed the "Installed Wheel:
# First Strict Report" section, its `first-use-prices.csv` / `first-use-scenario.yaml` fenced
# blocks and its installed-wheel install commands, and README.md is capped at 120 lines by
# `test_project_control.py::test_entry_documents_are_small_and_delivery_first`. The strict
# Scenario, fingerprint, resume and report behavior those tests exercised through the README stays
# covered by `test_backtest_scenario.py` and the scenario/resume/report/demo suites.


def _plain(output: str) -> str:
    return " ".join(Text.from_ansi(output).plain.replace("│", " ").split())


def test_root_version_uses_distribution_metadata_without_business_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def unexpected_call(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("version lookup entered a business or configuration path")

    for name in (
        "load_configuration",
        "run_offline_demo",
        "load_backtest_scenario",
        "run_backtest_scenario",
        "resume_backtest_attempt",
        "generate_backtest_report",
    ):
        monkeypatch.setattr(cli_module, name, unexpected_call)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        app,
        ["--config", str(tmp_path / "missing.yaml"), "--version"],
    )

    assert result.exit_code == 0
    assert result.stdout == f"{metadata.version('ea-quant')}\n"
    assert list(tmp_path.iterdir()) == []


def test_help_distinguishes_strict_attempts_from_the_run_only_reset_demo() -> None:
    runner = CliRunner()
    root_help = _plain(runner.invoke(app, ["--help"]).output)
    backtest_help = _plain(runner.invoke(app, ["backtest", "--help"]).output)
    run_help = _plain(runner.invoke(app, ["backtest", "run", "--help"]).output)
    resume_help = _plain(runner.invoke(app, ["backtest", "resume", "--help"]).output)
    report_help = _plain(runner.invoke(app, ["backtest", "report", "--help"]).output)

    assert "--version" in root_help
    assert "Show the installed ea-quant version and exit." in root_help
    assert "Strict scenarios support validate, run, resume, and report" in backtest_help
    assert "RESET demo is run-only" in backtest_help
    assert "strict runs create UUID attempt directories" in run_help
    assert "RESET demo writes phase1-demo-v1" in run_help
    assert "run-only RESET demo" in run_help
    assert "Existing strict Backtest attempt directory" in resume_help
    assert "Completed strict Backtest attempt directory" in report_help


def test_reset_demo_remains_run_only_and_unsupported_followups_fail_closed(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    output_root = (tmp_path / "demo-runs").resolve()
    demo = runner.invoke(app, ["backtest", "run", "--output-root", str(output_root)])
    demo_attempt = output_root / "phase1-demo-v1"

    resumed = runner.invoke(app, ["backtest", "resume", "--run-dir", str(demo_attempt)])
    reported = runner.invoke(
        app,
        [
            "backtest",
            "report",
            "--run-dir",
            str(demo_attempt),
            "--output-dir",
            str((tmp_path / "demo-report").resolve()),
        ],
    )

    assert demo.exit_code == 0
    assert (demo_attempt / "result.json").is_file()
    assert resumed.exit_code == 3
    assert "backtest resume failed closed" in resumed.output
    assert reported.exit_code == 3
    assert "backtest report failed closed" in reported.output
    assert not (tmp_path / "demo-report").exists()
