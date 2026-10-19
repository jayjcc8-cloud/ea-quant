"""Workspace profiles and real adapter resolution; no external requests."""

from pathlib import Path
from typing import Any

import pytest

from ea.desktop_settings import DesktopSettings
from ea.web.llm import create_model_port
from ea.web.settings import RadianSettings
from ea.web.user_strategies import _AdmittedTurn


def test_profiles_save_without_changing_default_or_moving_legacy_credentials(
    tmp_path: Path,
) -> None:
    desktop = DesktopSettings(tmp_path, tmp_path, tmp_path / "bundle")
    desktop.update({"model": {"provider": "deepseek", "model": "old", "api_key": "fixture-only"}})
    store = RadianSettings(tmp_path / "settings.json")
    original = store.load()["model"].copy()
    result = desktop.update(
        {
            "model_profile": {
                "id": "local",
                "name": "Local fixture",
                "provider": "openai-chat-completions-compatible",
                "model": "new",
                "base_url": "http://127.0.0.1:9999/v1",
                "api_key": "second-fixture",
            }
        }
    )
    assert store.load()["model"] == original
    assert result["model"]["provider"] == "deepseek"
    assert result["model"]["default_profile_id"] == "legacy"
    assert "second-fixture" not in str(result)
    assert len(result["model"]["profiles"]) == 2
    desktop.update({"default_model_profile": "local"})
    assert store.status()["provider"] == "openai-chat-completions-compatible"
    assert store.status("legacy")["model"] == "old"
    assert store.load()["model"] == original
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        desktop.update({"default_model_profile": "missing"})
    assert store.path.read_bytes() == before


def test_adapter_snapshot_and_next_resolution_use_their_own_protocol(tmp_path: Path) -> None:
    store = RadianSettings(tmp_path / "settings.json")
    store.save(
        {
            "schema": "radian.settings.v1",
            "model": {"provider": "deepseek", "model": "old", "api_key": "fixture-only"},
        }
    )
    old = create_model_port(store.snapshot())
    store.save(
        {
            "schema": "radian.settings.v1",
            "model": {
                "provider": "openai-chat-completions-compatible",
                "model": "new",
                "base_url": "http://127.0.0.1:9999/v1",
                "api_key": "fixture-only",
            },
        }
    )
    current = create_model_port(store.snapshot())
    old_route, current_route = old.route(), current.route()
    assert old_route is not None and current_route is not None
    assert old_route.provider == "deepseek"
    assert old_route.model == "old"
    assert current_route.provider == "openai-chat-completions-compatible"
    assert current_route.model == "new"


def test_profile_route_change_never_reuses_an_old_credential(tmp_path: Path) -> None:
    desktop = DesktopSettings(tmp_path, tmp_path, tmp_path / "bundle")
    body = {
        "id": "local",
        "name": "Local",
        "provider": "openai-chat-completions-compatible",
        "model": "fixture",
        "base_url": "http://127.0.0.1:9999",
        "api_key": "fixture-only",
    }
    desktop.update({"model_profile": body})
    store = RadianSettings(tmp_path / "settings.json")
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        desktop.update(
            {"model_profile": {**body, "base_url": "http://localhost:9988", "api_key": ""}}
        )
    assert store.path.read_bytes() == before


def test_admitted_turn_keeps_its_connection_and_rejects_edited_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ea.web.llm import ModelNotConfigured
    from unit.test_web_protocol_adapters import document, transport
    from unit.test_web_user_strategies import _stack

    strategies, _, service, _ = _stack(tmp_path, monkeypatch, scripted=False)
    try:
        store = strategies._settings
        store.save(
            {
                "schema": "radian.settings.v1",
                "model": {"provider": "deepseek", "model": "old", "api_key": "fixture-only"},
                "model_profiles": {
                    "second": {
                        "name": "Second",
                        "revision": 1,
                        "provider": "openai-chat-completions-compatible",
                        "model": "second-model",
                        "base_url": "http://127.0.0.1:9999/v1",
                        "api_key": "second-fixture",
                    }
                },
            }
        )
        strategy = strategies.create(name="Research", intent="Explain this idea")
        admitted = strategies._admit(
            strategy,
            call_id="first",
            base_revision=1,
            text="Explain",
            approved_budget={"max_model_calls": 2, "max_backtests": 0},
            action="message",
            proposal_id=None,
            evidence_job_ids=None,
            expected_evidence=None,
        )
        config = store.load()
        config["default_profile_id"] = "second"
        store.save(config)
        assert isinstance(admitted, _AdmittedTurn)
        port = strategies._port_for_call(admitted.strategy, "first")
        route = port.route()
        assert route is not None and route.provider == "deepseek"
        captured = transport(monkeypatch, [document("deepseek")])
        result = strategies._observe(admitted.strategy, "first", "Explain", 1)
        assert captured[0]["url"] == "https://api.deepseek.com/v1/chat/completions"
        assert captured[0]["json"]["model"] == "old"
        assert result.calls[0]["model_profile"]["id"] == "legacy"
        next_route = strategies._model_port().route()
        assert next_route is not None
        assert next_route.provider == "openai-chat-completions-compatible"
        selected = strategies.select_model_profile(strategy.user_strategy_id, "second")
        assert selected.draft == strategy.draft
        assert selected.calls == result.calls
        next_turn = strategies._admit(
            selected,
            call_id="next",
            base_revision=1,
            text="Next",
            approved_budget={"max_model_calls": 1, "max_backtests": 0},
            action="message",
            proposal_id=None,
            evidence_job_ids=None,
            expected_evidence=None,
        )
        assert isinstance(next_turn, _AdmittedTurn)
        config["model_profiles"]["second"]["revision"] = 2
        store.save(config)
        with pytest.raises(ModelNotConfigured, match="changed"):
            strategies._port_for_call(next_turn.strategy, "next")
        assert len(captured) == 1
        assert "fixture-only" not in str(result.document())
        assert "second-fixture" not in str(result.document())
    finally:
        service.stop()


def test_legacy_tool_rounds_keep_the_admitted_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx2

    from ea.web.llm import ToolCall
    from unit.test_web_protocol_adapters import document
    from unit.test_web_research import _orchestrator

    orchestrator = _orchestrator(tmp_path, monkeypatch)
    settings = orchestrator._settings
    settings.save(
        {
            "schema": "radian.settings.v1",
            "model": {"provider": "deepseek", "model": "first", "api_key": "fixture-only"},
            "model_profiles": {
                "second": {
                    "name": "Second",
                    "revision": 1,
                    "provider": "openai-chat-completions-compatible",
                    "model": "second",
                    "api_key": "second-fixture",
                    "base_url": "https://second.invalid/v1",
                }
            },
            "research": {"max_model_calls": 4, "max_backtests": 1},
        }
    )
    observed = []

    def post(url: str, **kwargs: Any) -> httpx2.Response:
        observed.append((url, kwargs["json"]["model"]))
        config = settings.load()
        config["default_profile_id"] = "second"
        settings.save(config)
        calls = (
            [ToolCall("lookup", "list_available_strategies", {})]
            if len(observed) == 1
            else [ToolCall("done", "declare_unsupported", {"gap": "Fixture scope"})]
        )
        return httpx2.Response(200, json=document("deepseek", calls))

    monkeypatch.setattr(httpx2, "post", post)
    try:
        orchestrator.create("One operation over two rounds")
        assert observed == [("https://api.deepseek.com/v1/chat/completions", "first")] * 2
        orchestrator.create("A distinct next operation")
        assert observed[-1] == ("https://second.invalid/v1/chat/completions", "second")
    finally:
        orchestrator._service.stop()
