import os
import subprocess
import sys

from typer.testing import CliRunner

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


def test_doctor_runs_as_python_module() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"

    result = subprocess.run(
        [sys.executable, "-m", "ea.cli.app", "doctor"],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert result.returncode == 0
    assert "EA system doctor" in result.stdout
    assert "config: development" in result.stdout
