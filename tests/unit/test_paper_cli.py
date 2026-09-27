from __future__ import annotations

import pytest
from typer.testing import CliRunner

from ea.cli.app import app

_START = [
    "paper",
    "start",
    "--workspace",
    "/missing/workspace",
    "--candidate-id",
    "missing",
    "--scenario",
    "/missing/scenario",
    "--output-root",
    "/missing/runs",
]


@pytest.mark.parametrize(
    "args,environment",
    [
        (["--run-mode", "live", *_START], {}),
        (_START, {"EA_RUN_MODE": "live"}),
        (["--config", "/must-not-read-credentials", *_START], {}),
        (_START, {"EA_CONFIG_PATH": "/must-not-read-credentials"}),
    ],
)
def test_paper_rejects_live_and_global_config_before_candidate_loading(
    args: list[str],
    environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("configuration loading must not happen")

    monkeypatch.setattr("ea.cli.app.load_configuration", forbidden)
    result = CliRunner().invoke(app, args, env=environment)
    assert result.exit_code == 2
    assert "Paper rejects" in result.output


def test_paper_help_exposes_the_actual_start_status_stop_entry() -> None:
    result = CliRunner().invoke(app, ["paper", "--help"])
    assert result.exit_code == 0
    for command in ("start", "status", "stop", "resume"):
        assert command in result.output


def test_paper_resume_rejects_a_missing_run_directory() -> None:
    result = CliRunner().invoke(app, ["paper", "resume", "--run-dir", "/missing/run-dir"])
    assert result.exit_code == 3
    assert "Paper resume rejected" in result.output
