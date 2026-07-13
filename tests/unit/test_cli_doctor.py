import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from typer.testing import CliRunner

import ea
from ea.cli.app import app


def test_doctor_reports_project_health() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "EA system doctor" in result.stdout
    assert "python:" in result.stdout
    assert "config: development" in result.stdout
    assert "paper trading: enabled" in result.stdout
    assert "live trading: disabled" in result.stdout


def _environment_without_pythonpath() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return env


def test_source_and_distribution_versions_match() -> None:
    assert metadata.version("ea-quant") == ea.__version__ == "0.1.1"


def test_doctor_runs_as_installed_python_module(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-m", "ea.cli.app", "doctor"],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=_environment_without_pythonpath(),
        text=True,
    )

    assert result.returncode == 0
    assert "EA system doctor" in result.stdout
    assert "config: development" in result.stdout


def test_doctor_runs_as_installed_console_script(tmp_path: Path) -> None:
    script_name = "ea.exe" if os.name == "nt" else "ea"
    entrypoint = Path(sys.executable).with_name(script_name)

    result = subprocess.run(
        [entrypoint, "doctor"],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=_environment_without_pythonpath(),
        text=True,
    )

    assert result.returncode == 0
    assert "EA system doctor" in result.stdout
    assert "config: development" in result.stdout
