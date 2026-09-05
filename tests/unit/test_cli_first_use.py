from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

import pytest
from rich.text import Text
from typer.testing import CliRunner

import ea.cli.app as cli_module
from ea.cli.app import app
from ea.product import BacktestStrategyId, load_backtest_scenario

PROJECT_ROOT = Path(__file__).resolve().parents[2]
README_PATH = PROJECT_ROOT / "README.md"


def _plain(output: str) -> str:
    return " ".join(Text.from_ansi(output).plain.replace("│", " ").split())


def _readme_block(name: str, language: str) -> str:
    readme = README_PATH.read_text(encoding="utf-8")
    match = re.search(
        rf"<!-- {re.escape(name)}:start -->\n```{language}\n(.*?)```\n"
        rf"<!-- {re.escape(name)}:end -->",
        readme,
        flags=re.DOTALL,
    )
    assert match is not None, f"README block {name!r} is missing"
    return match.group(1)


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


def test_readme_first_use_input_satisfies_the_strict_scenario_contract(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "prices.csv").write_text(
        _readme_block("first-use-prices.csv", "csv"),
        encoding="utf-8",
    )
    scenario_path = input_dir / "scenario.yaml"
    scenario_path.write_text(
        _readme_block("first-use-scenario.yaml", "yaml"),
        encoding="utf-8",
    )

    loaded = load_backtest_scenario(scenario_path)

    assert loaded.strategy_id is BacktestStrategyId.ALWAYS_FLAT
    assert loaded.dataset.selection.fingerprint.record_count == 4
    assert (
        loaded.dataset.selection.fingerprint.sha256.value
        == "c95c6182ba68d8c03726336172b5ce089c464fdd87ba28cb4444460fcdbae2fb"
    )
    assert loaded.data_path == (input_dir / "prices.csv").resolve()


def test_readme_first_use_commands_are_checkout_independent_and_release_accurate() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    installed = readme.split("## Installed Wheel: First Strict Report", 1)[1].split(
        "## Contributor Setup", 1
    )[0]

    for command in (
        "--version",
        "backtest validate",
        "backtest run",
        "backtest resume",
        "backtest report",
        "ATTEMPT_DIR",
    ):
        assert command in installed
    assert "scripts/bootstrap_local.py" not in installed
    assert "tests/" not in installed
    assert "uv venv --python 3.12 user-env" in installed
    assert 'uv pip install --python user-env/bin/python "$WHEEL"' in installed
    assert "python3.12 -m venv" not in installed
    assert "published v0.2.0 wheel does not provide `ea --version`" in readme
    assert "RESET demo is run-only and does not support `resume` or `report`" in readme


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
