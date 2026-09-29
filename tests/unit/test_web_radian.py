from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from fastapi.testclient import TestClient

from ea.web.app import WebSettings, create_app
from ea.web.radian import overview, paper_events, paper_overview, search
from ea.web.service import WebService
from ea.web.settings import RadianSettings, RadianSettingsError

pytestmark = pytest.mark.filterwarnings(
    "ignore:The anyio.abc.BlockingPortal alias is deprecated:DeprecationWarning"
)


def _scenario_root(tmp_path: Path) -> Path:
    from unit.test_backtest_report import _priced_scenario

    root = tmp_path / "scenarios"
    source = _priced_scenario(root)
    source.rename(root / "bounded-long.yaml")
    document = yaml.safe_load((root / "bounded-long.yaml").read_text(encoding="utf-8"))
    flat = dict(document)
    flat["strategy"] = {"id": "always-flat-v1"}
    (root / "flat.yaml").write_text(yaml.safe_dump(flat, sort_keys=False), encoding="utf-8")
    return root


def _service(tmp_path: Path) -> WebService:
    service = WebService(_scenario_root(tmp_path), tmp_path / "workspace")
    service.start()
    return service


def _settings(tmp_path: Path, **overrides: Any) -> WebSettings:
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>RADIAN</title>", encoding="utf-8")
    return WebSettings(
        scenario_root=_scenario_root(tmp_path),
        workspace=tmp_path / "workspace",
        ui_dir=ui,
        port=8765,
        **overrides,
    )


# --- settings store ---------------------------------------------------------


def test_settings_default_is_absent_and_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.json"
    monkeypatch.setenv("RADIAN_SETTINGS_FILE", str(path))
    store = RadianSettings(RadianSettings.default_path())
    assert store.load() == {}
    status = store.status()
    assert status["configured"] is False
    assert status["provider"] is None
    assert "api_key" not in status


def test_settings_save_load_and_secret_exclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.json"
    monkeypatch.setenv("RADIAN_SETTINGS_FILE", str(path))
    store = RadianSettings(RadianSettings.default_path())
    store.save(
        {
            "schema": "radian.settings.v1",
            "model": {"provider": "anthropic", "model": "claude-haiku-4-5-20251001", "api_key": "sk-secret"},
        }
    )
    status = store.status()
    assert status["configured"] is True
    assert status["provider"] == "anthropic"
    assert status["model"] == "claude-haiku-4-5-20251001"
    assert "sk-secret" not in json.dumps(status)
    assert os.stat(store.path).st_mode & 0o077 == 0


def test_settings_rejects_unknown_provider_and_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.json"
    monkeypatch.setenv("RADIAN_SETTINGS_FILE", str(path))
    store = RadianSettings(RadianSettings.default_path())
    with pytest.raises(RadianSettingsError):
        store.save({"model": {"provider": "nope", "model": "x", "api_key": "k"}})
    with pytest.raises(RadianSettingsError):
        store.save({"schema": "other.v1", "model": {}})
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RadianSettingsError):
        store.load()


# --- paper overview read ----------------------------------------------------


def test_paper_overview_without_runtime_root_is_explicitly_unavailable() -> None:
    result = paper_overview(None)
    assert result == {"available": False, "reason": "runtime_root_not_configured"}


def test_paper_overview_without_run_directories(tmp_path: Path) -> None:
    empty = tmp_path / "runtime"
    empty.mkdir()
    result = paper_overview(empty)
    assert result == {"available": False, "reason": "no_supervised_run_directories"}


def test_paper_overview_unreadable_run_directory_is_not_fake_data(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    not_a_run = runtime / "not-a-bound-run"
    not_a_run.mkdir(parents=True)
    result = paper_overview(runtime)
    assert result["available"] is False
    assert result["reason"] == "run_state_unreadable"


def test_paper_events_missing_log(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    result = paper_events(runtime)
    assert result == {"available": False, "reason": "no_supervised_run_directories"}
    not_a_run = runtime / "not-a-bound-run"
    not_a_run.mkdir()
    result = paper_events(runtime)
    assert result["available"] is False
    assert result["reason"] == "operational_log_missing"


def test_paper_events_reads_tail_and_skips_broken_lines(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    run_dir = runtime / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "operational.jsonl").write_text(
        '{"event": "first", "run_id": "run-1"}\nnot json\n{"event": "last"}\n',
        encoding="utf-8",
    )
    result = paper_events(runtime, limit=10)
    assert result["available"] is True
    assert result["run_id"] == "run-1"
    assert [item.get("event") for item in result["events"]] == ["first", "last"]
    assert result["count"] == 2


# --- overview and search ----------------------------------------------------


def test_overview_empty_workspace_is_honest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADIAN_SETTINGS_FILE", str(tmp_path / "settings.json"))
    service = _service(tmp_path)
    try:
        result = overview(service)
    finally:
        service.stop()
    assert result["recent_work"] == []
    assert result["attention"] == []
    assert result["paper"] == {"available": False, "reason": "runtime_root_not_configured"}
    assert result["model"]["configured"] is False


def test_search_matches_registered_scenarios(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        result = search(service, "always-flat")
        kinds = [item["kind"] for item in result["results"]]
        assert "scenario" in kinds
        assert any(item["id"] == "flat.yaml" for item in result["results"])
        empty = search(service, "no-such-object")
        assert empty["results"] == []
    finally:
        service.stop()


def test_search_minimum_query_length_is_enforced_by_api(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    client = TestClient(app)
    with client:
        # A wrong Host header is rejected by the loopback boundary first.
        response = client.get("/api/search", params={"q": "a"})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_host"
        # With the trusted loopback host, the query bound is enforced.
        response = client.get(
            "/api/search", params={"q": "a"}, headers={"host": "127.0.0.1:8765"}
        )
        assert response.status_code == 422


def test_workspace_overview_api_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADIAN_SETTINGS_FILE", str(tmp_path / "settings.json"))
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    with client:
        response = client.get(
            "/api/workspace/overview", headers={"host": "127.0.0.1:8765"}
        )
        assert response.status_code == 200, response.text
        document = response.json()
        assert document["schema"] == "radian.workspace-overview.v1"
        assert document["paper"]["available"] is False
        assert document["paper"]["reason"] == "runtime_root_not_configured"
        assert document["model"]["configured"] is False
        settings_response = client.get(
            "/api/settings/status", headers={"host": "127.0.0.1:8765"}
        )
        assert settings_response.status_code == 200
        assert settings_response.json()["configured"] is False


def test_web_settings_accept_runtime_root_and_alerts_stream(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    stream = tmp_path / "alerts.jsonl"
    settings = _settings(tmp_path, runtime_root=runtime, alerts_stream=stream)
    assert settings.runtime_root == runtime
    assert settings.alerts_stream == stream


def test_web_settings_reject_relative_runtime_root(tmp_path: Path) -> None:
    with pytest.raises(Exception):
        create_app(_settings(tmp_path, runtime_root=Path("relative/path")))
