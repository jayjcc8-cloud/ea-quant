"""Neutral model port and settings projection contract.

These tests pin the replaceable-provider contract B introduces: the projection
names the protocol a provider value actually selects, the capabilities that
protocol declares, and the capabilities research requires. A provider whose
protocol does not declare a required capability is unconfigured and never
dispatches. No secret material may appear in the projection.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx2 as httpx
import pytest

from ea.web import llm
from ea.web.settings import RadianSettings, RadianSettingsError

_FIXTURE_KEY = "local-fixture-key-not-a-credential"
_ENV_KEY_VALUE = "local-env-fixture-value-not-a-credential"


def _store(tmp_path: Path, model: dict[str, object]) -> RadianSettings:
    store = RadianSettings(tmp_path / "settings.json")
    store.save({"schema": "radian.settings.v1", "model": model})
    return store


def _messages_model(**extra: object) -> dict[str, object]:
    return {
        "provider": "anthropic",
        "model": "fixture-model-v1",
        "api_key": _FIXTURE_KEY,
        **extra,
    }


def test_status_projects_protocol_endpoint_capabilities_and_budget(tmp_path: Path) -> None:
    store = RadianSettings(tmp_path / "settings.json")
    store.save(
        {
            "schema": "radian.settings.v1",
            "model": _messages_model(base_url="http://127.0.0.1:9999"),
            "research": {"max_model_calls": 4, "max_backtests": 2},
        }
    )

    status = store.status()

    assert status["configured"] is True
    assert status["provider"] == "anthropic"
    assert status["model"] == "fixture-model-v1"
    assert status["endpoint"] == "http://127.0.0.1:9999"
    assert status["protocol"] == "anthropic-messages"
    assert status["capabilities"] == {
        "tool_use": True,
        "structured_output": False,
        "usage": True,
    }
    assert status["required_capabilities"] == ["tool_use"]
    assert status["missing_capabilities"] == []
    assert status["configuration_error"] is None
    assert status["research_budget"] == {"max_model_calls": 4, "max_backtests": 2}
    assert _FIXTURE_KEY not in json.dumps(status)


def test_absent_settings_report_unconfigured_with_the_missing_capability(tmp_path: Path) -> None:
    status = RadianSettings(tmp_path / "absent.json").status()

    assert status["configured"] is False
    assert status["provider"] is None
    assert status["protocol"] is None
    assert status["capabilities"] is None
    assert status["endpoint"] is None
    assert status["required_capabilities"] == ["tool_use"]
    assert status["missing_capabilities"] == ["tool_use"]
    assert status["configuration_error"] == "no usable model provider is configured"


def test_unconfigured_key_is_reported_without_a_protocol_claim(tmp_path: Path) -> None:
    store = _store(tmp_path, {"provider": "anthropic", "model": "fixture-model-v1"})

    status = store.status()

    assert status["configured"] is False
    assert status["protocol"] == "anthropic-messages"
    assert status["missing_capabilities"] == []
    assert status["configuration_error"] == "no usable model provider is configured"


def test_protocol_without_tool_use_is_unconfigured_and_never_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, _messages_model())
    monkeypatch.setitem(
        llm.PROVIDER_PROTOCOLS,
        "anthropic",
        (
            "anthropic-messages",
            llm.ModelCapabilities(tool_use=False, structured_output=True, usage=True),
        ),
    )

    def _forbidden_dispatch(*args: object, **kwargs: object) -> None:
        raise AssertionError("a protocol without tool use must not dispatch")

    monkeypatch.setattr(httpx, "post", _forbidden_dispatch)

    status = store.status()
    assert status["configured"] is False
    assert status["missing_capabilities"] == ["tool_use"]
    assert "tool_use" in str(status["configuration_error"])

    provider = llm.AnthropicProvider(store)
    assert provider.route() is None
    with pytest.raises(llm.ModelNotConfigured, match="tool_use"):
        provider.complete(
            system="system",
            messages=[{"role": "user", "content": "idea"}],  # type: ignore[list-item]
        )


def test_messages_adapter_keeps_its_own_protocol_for_any_endpoint(tmp_path: Path) -> None:
    store = _store(tmp_path, _messages_model(base_url="http://127.0.0.1:9999"))
    provider = llm.AnthropicProvider(store)

    route = provider.route()

    assert isinstance(provider, llm.ModelPort)
    assert route is not None
    assert route.provider == "anthropic"
    assert route.protocol == "anthropic-messages"
    assert route.endpoint == "http://127.0.0.1:9999"
    assert route.capabilities.tool_use is True
    assert route.capabilities.usage is True
    assert _FIXTURE_KEY not in repr(route)
    # A provider value without a real adapter has no protocol and no adapter
    # may be relabelled as generically compatible.
    assert llm.protocol_for("anthropic") == "anthropic-messages"
    assert llm.protocol_for("openai") is None
    assert llm.capabilities_for("openai") is None
    assert llm.REQUIRED_RESEARCH_CAPABILITIES == ("tool_use",)
    assert llm.missing_capabilities(None) == ("tool_use",)


def test_env_named_key_resolves_without_entering_the_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RADIAN_TEST_MODEL_KEY", _ENV_KEY_VALUE)
    store = _store(
        tmp_path,
        {
            "provider": "anthropic",
            "model": "fixture-model-v1",
            "api_key_env": "RADIAN_TEST_MODEL_KEY",
        },
    )

    status = store.status()

    assert status["configured"] is True
    assert status["api_key_env"] == "RADIAN_TEST_MODEL_KEY"
    assert _ENV_KEY_VALUE not in json.dumps(status)
    route = llm.AnthropicProvider(store).route()
    assert route is not None
    assert _ENV_KEY_VALUE not in repr(route)


def test_unsupported_provider_value_is_still_rejected(tmp_path: Path) -> None:
    store = RadianSettings(tmp_path / "settings.json")

    with pytest.raises(RadianSettingsError):
        store.save(
            {
                "schema": "radian.settings.v1",
                "model": {"provider": "openai-compatible", "model": "x"},
            }
        )


@pytest.mark.parametrize("value", [None, "", "local-env-fixture-not-a-credential"])
def test_readiness_distinguishes_env_name_from_available_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    name = "RADIAN_MISSING_TEST_KEY"
    monkeypatch.delenv(name, raising=False)
    if value is not None:
        monkeypatch.setenv(name, value)
    store = _store(
        tmp_path,
        {
            "provider": "anthropic",
            "model": "fixture-model",
            "api_key_env": name,
        },
    )
    status = store.status()
    assert status["configuration_present"] is True
    assert status["credential_available"] is bool(value)
    assert status["configured"] is bool(value)
    assert status["request_validation"] == "not_verified"
    assert (llm.create_model_port(store).route() is not None) is bool(value)
    if value:
        assert value not in json.dumps(status)


def test_research_consumes_factory_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ea.web import research
    from unit.test_web_research import _response, _service

    class StubPort:
        calls = 0

        def route(self) -> llm.ModelRoute | None:
            return None

        def complete(self, **kwargs: object) -> llm.ModelResponse:
            self.calls += 1
            return _response(
                [
                    {
                        "id": "stub-declaration",
                        "name": "declare_unsupported",
                        "input": {"gap": "Scripted unsupported fixture"},
                    }
                ]
            )

    port = StubPort()
    monkeypatch.setattr(research, "create_model_port", lambda settings: port)
    service = _service(tmp_path)
    try:
        orchestrator = research.ResearchOrchestrator(
            research.ResearchStore(tmp_path / "research"),
            service,
            RadianSettings(tmp_path / "absent.json"),
        )
        task = orchestrator.create("Unsupported fixture idea")
        assert task.status == "unsupported"
        assert task.gap == "Scripted unsupported fixture"
        assert port.calls == 1
    finally:
        service.stop()
