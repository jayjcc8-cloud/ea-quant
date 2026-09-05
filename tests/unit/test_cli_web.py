from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from ea.cli.app import app
from ea.web.app import WebSettings


def test_web_help_exposes_only_explicit_local_roots_and_port() -> None:
    result = CliRunner().invoke(app, ["web", "serve", "--help"])

    assert result.exit_code == 0
    assert "--scenario-root" in result.stdout
    assert "--workspace" in result.stdout
    assert "--ui-dir" in result.stdout
    assert "--port" in result.stdout
    assert "127.0.0.1" in result.stdout
    assert "--host" not in result.stdout


def test_web_serve_passes_explicit_boundaries_to_local_server(
    tmp_path: Path, monkeypatch: object
) -> None:
    from pytest import MonkeyPatch

    typed_monkeypatch = monkeypatch
    assert isinstance(typed_monkeypatch, MonkeyPatch)
    scenario_root = tmp_path / "scenarios"
    ui_dir = tmp_path / "ui"
    scenario_root.mkdir()
    ui_dir.mkdir()
    (ui_dir / "index.html").write_text("<!doctype html>", encoding="utf-8")
    workspace = tmp_path / "workspace"
    captured: list[WebSettings] = []

    def fake_serve(settings: WebSettings) -> None:
        captured.append(settings)

    import ea.web.server as server

    typed_monkeypatch.setattr(server, "serve_local_web", fake_serve)
    result = CliRunner().invoke(
        app,
        [
            "web",
            "serve",
            "--scenario-root",
            str(scenario_root),
            "--workspace",
            str(workspace),
            "--ui-dir",
            str(ui_dir),
            "--port",
            "9123",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert len(captured) == 1
    settings = captured[0]
    assert settings.scenario_root == scenario_root.resolve()
    assert settings.workspace == workspace.resolve()
    assert settings.ui_dir == ui_dir.resolve()
    assert settings.port == 9123
