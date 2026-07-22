import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest
from rich.text import Text
from typer.testing import CliRunner

import ea
from ea.cli.app import app


def test_doctor_reports_project_health(isolated_ea_environment: None) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "EA system doctor" in result.stdout
    assert "python:" in result.stdout
    assert "config file: <defaults>" in result.stdout
    assert "schema version: 1" in result.stdout
    assert "environment: development" in result.stdout
    assert "run mode: backtest" in result.stdout
    assert "live profile: unavailable" in result.stdout


def test_doctor_uses_global_cli_overrides(
    isolated_ea_environment: None,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        "schema_version: 1\nenvironment: development\nrun:\n  mode: backtest\n",
        encoding="utf-8",
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "--environment",
            "staging",
            "--run-mode",
            "paper",
            "doctor",
        ],
    )

    assert result.exit_code == 0
    assert f"config file: {config_path.resolve()}" in result.stdout
    assert "environment: staging" in result.stdout
    assert "run mode: paper" in result.stdout


def test_doctor_reports_safe_configuration_error(
    isolated_ea_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_value = "must-not-be-echoed"
    monkeypatch.setenv("EA_FUTURE_SETTING", secret_value)
    runner = CliRunner()

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 2
    assert "configuration error:" in result.output
    assert "EA_FUTURE_SETTING" in result.output
    assert secret_value not in result.output
    assert "Traceback" not in result.output


def test_doctor_reports_invalid_utf8_as_safe_configuration_error(
    isolated_ea_environment: None,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "invalid.yaml"
    config_path.write_bytes(b"\xffmust-not-be-echoed")
    runner = CliRunner()

    result = runner.invoke(app, ["--config", str(config_path), "doctor"])

    assert result.exit_code == 2
    assert "configuration error: cannot read selected config file:" in result.output
    assert "must-not-be-echoed" not in result.output
    assert "Traceback" not in result.output


def test_doctor_reports_path_resolution_failure_safely(
    isolated_ea_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    internal_error = "must-not-be-echoed"

    def fail_to_expand_path(_path: Path) -> Path:
        raise RuntimeError(internal_error)

    monkeypatch.setattr(Path, "expanduser", fail_to_expand_path)
    runner = CliRunner()

    result = runner.invoke(app, ["--config", "settings.yaml", "doctor"])

    assert result.exit_code == 2
    assert "configuration error: cannot resolve selected config path" in result.output
    assert internal_error not in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("invalid_path", ["a\0b.yaml", "\ud800.yaml"])
def test_doctor_reports_invalid_path_safely(
    isolated_ea_environment: None,
    invalid_path: str,
) -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["--config", invalid_path, "doctor"])

    assert result.exit_code == 2
    assert "configuration error: cannot resolve selected config path" in result.output
    assert invalid_path not in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    ("option_name", "first_value"),
    [
        ("--config", "settings.yaml"),
        ("--environment", "development"),
        ("--run-mode", "backtest"),
    ],
)
def test_doctor_rejects_duplicate_cli_options_without_value_leak(
    isolated_ea_environment: None,
    option_name: str,
    first_value: str,
) -> None:
    secret_value = "must-not-be-echoed"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [option_name, first_value, option_name, secret_value, "doctor"],
    )

    assert result.exit_code == 2
    assert f"CLI option '{option_name}' may be specified only once" in result.output
    assert secret_value not in result.output
    assert "Traceback" not in result.output


def test_doctor_rejects_unknown_cli_option_without_value_leak(
    isolated_ea_environment: None,
) -> None:
    secret_value = "must-not-be-echoed"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--future-setting", secret_value, "doctor"],
        color=True,
    )
    plain_output = Text.from_ansi(result.output).plain

    assert result.exit_code == 2
    assert "No such option: --future-setting" in plain_output
    assert secret_value not in plain_output
    assert "Traceback" not in plain_output


def test_doctor_escapes_control_characters_in_unknown_option() -> None:
    unsafe_option = "--future-\x1b[2J"
    secret_value = "must-not-be-echoed"
    runner = CliRunner()

    result = runner.invoke(app, [unsafe_option, secret_value, "doctor"])
    plain_output = Text.from_ansi(result.output).plain

    assert result.exit_code == 2
    assert "\x1b[2J" not in result.output
    assert r"--future-\x1b[2J" in plain_output
    assert secret_value not in plain_output


def test_doctor_rejects_unexpected_positional_value_without_value_leak(
    isolated_ea_environment: None,
) -> None:
    secret_value = "must-not-be-echoed"
    runner = CliRunner()

    result = runner.invoke(app, ["doctor", secret_value])

    assert result.exit_code == 2
    assert "configuration error: unexpected positional arguments" in result.output
    assert secret_value not in result.output
    assert "Traceback" not in result.output


def test_doctor_escapes_control_characters_in_selected_path(
    isolated_ea_environment: None,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "settings_\x1b[2J.yaml"
    config_path.write_text(
        "schema_version: 1\nenvironment: development\nrun:\n  mode: backtest\n",
        encoding="utf-8",
    )
    runner = CliRunner()

    result = runner.invoke(app, ["--config", str(config_path), "doctor"])

    assert result.exit_code == 0
    assert "\x1b[2J" not in result.output
    assert r"settings_\x1b[2J.yaml" in result.output


def test_doctor_rejects_unavailable_live_mode(
    isolated_ea_environment: None,
) -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["--run-mode", "live", "doctor"])

    assert result.exit_code == 2
    assert "configuration error:" in result.output
    assert "Traceback" not in result.output


def _isolated_subprocess_environment() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if name.upper() != "PYTHONPATH" and not name.upper().startswith("EA_")
    }


def test_subprocess_environment_removes_ambient_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_ENV", "production")
    monkeypatch.setenv("EA_FUTURE_SETTING", "must-not-leak")
    monkeypatch.setenv("ea_future_lowercase", "must-not-leak")
    monkeypatch.setenv("PYTHONPATH", "/tmp/must-not-leak")
    monkeypatch.setenv("pythonpath", "/tmp/must-not-leak-either")

    env = _isolated_subprocess_environment()

    assert "PYTHONPATH" not in {name.upper() for name in env}
    assert not any(name.upper().startswith("EA_") for name in env)


def test_source_and_distribution_versions_match() -> None:
    assert metadata.version("ea-quant") == ea.__version__ == "0.1.1"


def test_doctor_runs_as_installed_python_module(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-m", "ea.cli.app", "doctor"],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=_isolated_subprocess_environment(),
        text=True,
    )

    assert result.returncode == 0
    assert "EA system doctor" in result.stdout
    assert "environment: development" in result.stdout
    assert "run mode: backtest" in result.stdout


def test_doctor_runs_as_installed_console_script(tmp_path: Path) -> None:
    script_name = "ea.exe" if os.name == "nt" else "ea"
    entrypoint = Path(sys.executable).with_name(script_name)

    result = subprocess.run(
        [entrypoint, "doctor"],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=_isolated_subprocess_environment(),
        text=True,
    )

    assert result.returncode == 0
    assert "EA system doctor" in result.stdout
    assert "environment: development" in result.stdout
    assert "run mode: backtest" in result.stdout
