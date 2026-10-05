"""Local fake-transport contracts; these are not real-provider E2E tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest

from ea.web import llm, research
from ea.web.settings import RadianSettings

PROVIDERS = ("anthropic", "openai-chat-completions-compatible")
KEY = "fixture-not-a-credential"


def store(tmp_path: Path, provider: str, *, max_model_calls: int = 2) -> RadianSettings:
    settings = RadianSettings(tmp_path / "settings.json")
    settings.save(
        {
            "schema": "radian.settings.v1",
            "model": {
                "provider": provider,
                "model": "fixture-model",
                "api_key": KEY,
                "base_url": "http://fixture.invalid/api",
            },
            "research": {"max_model_calls": max_model_calls, "max_backtests": 1},
        }
    )
    return settings


def document(
    provider: str,
    calls: list[llm.ToolCall] | None = None,
    *,
    usage: object = None,
    finish: str | None = None,
) -> dict[str, Any]:
    calls = calls or []
    if provider == "anthropic":
        result: dict[str, Any] = {
            "content": [
                {"type": "text", "text": "fixture text"},
                *(
                    {
                        "type": "tool_use",
                        "id": c.id,
                        "name": c.name,
                        "input": c.arguments,
                    }
                    for c in calls
                ),
            ],
            "stop_reason": finish or ("tool_use" if calls else "end_turn"),
        }
    else:
        result = {
            "choices": [
                {
                    "finish_reason": finish or ("tool_calls" if calls else "stop"),
                    "message": {
                        "role": "assistant",
                        "content": "fixture text",
                        "tool_calls": [
                            {
                                "type": "function",
                                "id": c.id,
                                "function": {
                                    "name": c.name,
                                    "arguments": json.dumps(c.arguments),
                                },
                            }
                            for c in calls
                        ],
                    },
                }
            ]
        }
    if usage is not None:
        result["usage"] = usage
    return result


def transport(
    monkeypatch: pytest.MonkeyPatch, documents: list[object], status: int = 200
) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> httpx.Response:
        recorded.append({"url": url, **kwargs})
        return httpx.Response(status, json=documents.pop(0))

    monkeypatch.setattr(httpx, "post", post)
    return recorded


@pytest.mark.parametrize("provider", PROVIDERS)
def test_neutral_round_trip_and_explicit_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    settings = store(tmp_path, provider)
    port = llm.create_model_port(settings)
    calls = [
        llm.ToolCall("one", "lookup", {"symbol": "AAPL"}),
        llm.ToolCall("two", "lookup", {"symbol": "MSFT"}),
    ]
    usage = (
        {"input_tokens": 12, "output_tokens": 5}
        if provider == "anthropic"
        else {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
    )
    captured = transport(
        monkeypatch,
        [document(provider, calls, usage=usage), document(provider, usage=usage)],
    )
    tools = [llm.ToolDefinition("lookup", "Lookup", {"type": "object"})]
    messages = [llm.ModelMessage("user", "idea")]
    response = port.complete(system="policy", messages=messages, tools=tools, max_tokens=321)
    assert response.tool_calls == tuple(calls)
    assert response.text == "fixture text" and response.stop_reason == "tool_calls"
    assert response.usage_available and (
        response.input_tokens,
        response.output_tokens,
    ) == (12, 5)
    messages += [
        llm.ModelMessage("assistant", response.text, response.tool_calls),
        llm.ModelMessage("tool", "first result", call_id="one"),
        llm.ModelMessage("tool", "second result", call_id="two"),
    ]
    assert port.complete(system="policy", messages=messages, tools=tools).stop_reason == "stop"
    assert len(captured) == 2
    first, second = captured[0]["json"], captured[1]["json"]
    assert first["model"] == "fixture-model"
    if provider == "anthropic":
        assert captured[0]["url"] == "http://fixture.invalid/api/v1/messages"
        assert first["system"] == "policy" and first["max_tokens"] == 321
        assert first["tools"][0]["input_schema"] == tools[0].parameters
        assert second["messages"][-1]["role"] == "user"
        assert [b["tool_use_id"] for b in second["messages"][-1]["content"]] == [
            "one",
            "two",
        ]
        assert second["messages"][-2]["content"][0]["text"] == response.text
    else:
        assert captured[0]["url"] == "http://fixture.invalid/api/chat/completions"
        assert first["messages"][0] == {"role": "system", "content": "policy"}
        assert first["max_completion_tokens"] == 321 and first["stream"] is False
        assert first["tools"][0]["function"]["parameters"] == tools[0].parameters
        assert [m["role"] for m in second["messages"]] == [
            "system",
            "user",
            "assistant",
            "tool",
            "tool",
        ]
        assert (
            json.loads(second["messages"][2]["tool_calls"][0]["function"]["arguments"])
            == calls[0].arguments
        )
        assert second["messages"][-1]["tool_call_id"] == "two"
    status = settings.status()
    assert status["configured"] and status["provider"] == provider
    assert status["research_budget"] == {"max_model_calls": 2, "max_backtests": 1}
    assert status["request_validation"] == "not_verified" and KEY not in json.dumps(status)
    assert port.route() is not None and KEY not in repr(port.route())


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "usage",
    [None, {}, {"input_tokens": -1}, {"prompt_tokens": True, "completion_tokens": 2}],
)
def test_missing_or_invalid_usage_remains_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, usage: object
) -> None:
    transport(monkeypatch, [document(provider, usage=usage)])
    result = llm.create_model_port(store(tmp_path, provider)).complete(
        system="", messages=[llm.ModelMessage("user", "idea")]
    )
    assert result.input_tokens is None and result.output_tokens is None
    assert not result.usage_available


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("finish", ["length", "max_tokens", "refusal", "content_filter", "unknown"])
def test_incomplete_or_unsupported_finish_never_returns_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, finish: str
) -> None:
    transport(monkeypatch, [document(provider, [llm.ToolCall("c", "x", {})], finish=finish)])
    with pytest.raises(llm.ModelProviderError):
        llm.create_model_port(store(tmp_path, provider)).complete(
            system="", messages=[llm.ModelMessage("user", "idea")]
        )


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("capability", ["structured_output", "streaming", "images", "responses"])
def test_unsupported_capability_fails_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, capability: str
) -> None:
    captured = transport(monkeypatch, [])
    with pytest.raises(llm.ModelNotConfigured, match=capability):
        llm.create_model_port(store(tmp_path, provider)).complete(
            system="",
            messages=[llm.ModelMessage("user", "idea")],
            required_capabilities=(capability,),
        )
    assert captured == []


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("status", [401, 429, 500])
def test_http_errors_hide_provider_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, status: int
) -> None:
    transport(monkeypatch, [{"error": KEY}], status)
    with pytest.raises(llm.ModelProviderError) as error:
        llm.create_model_port(store(tmp_path, provider)).complete(
            system="", messages=[llm.ModelMessage("user", "idea")]
        )
    assert str(status) in str(error.value) and KEY not in str(error.value)


@pytest.mark.parametrize("arguments", ["not-json", "[]", '"text"', '{"x": NaN}'])
def test_openai_rejects_non_object_or_malformed_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: str
) -> None:
    payload = document(PROVIDERS[1], [llm.ToolCall("c", "x", {})])
    payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = arguments
    transport(monkeypatch, [payload])
    with pytest.raises(llm.ModelProviderError):
        llm.create_model_port(store(tmp_path, PROVIDERS[1])).complete(
            system="", messages=[llm.ModelMessage("user", "idea")]
        )


@pytest.mark.parametrize("provider", PROVIDERS)
def test_same_strategy_draft_repair_flow_and_restart_with_missing_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    from unit.test_web_research import _propose_call, _service

    service = _service(tmp_path)
    # The two calls below are the proposal and its repair, so this fixture runs on
    # a budget that also holds the final conclusion slot the single-experiment
    # contract reserves up front; a two-call budget could never reach a conclusion.
    settings = store(tmp_path, provider, max_model_calls=3)
    path = tmp_path / "research"
    orchestrator = research.ResearchOrchestrator(research.ResearchStore(path), service, settings)
    catalog = orchestrator._catalog()  # noqa: SLF001
    arguments = _propose_call(catalog[0]["strategy_id"], catalog)
    invalid = {**arguments, "strategy_id": "unregistered"}
    captured = transport(
        monkeypatch,
        [
            document(provider, [llm.ToolCall("bad", "propose_spec", invalid)]),
            document(provider, [llm.ToolCall("good", "propose_spec", arguments)]),
        ],
    )
    try:
        task = orchestrator.create("same fixture strategy idea")
        assert task.status == "awaiting_confirmation"
        assert (
            task.spec_draft is not None and task.spec_draft.strategy_id == catalog[0]["strategy_id"]
        )
        assert task.usage["model_calls"] == 2 and task.usage["unavailable_model_calls"] == 2
        assert task.last_model is not None and task.last_model["usage_available"] is False
        reopened = research.ResearchStore(path).get(task.task_id)
        assert reopened.usage == task.usage and reopened.spec_draft == task.spec_draft
        assert len(captured) == 2 and "rejected" in json.dumps(captured[1]["json"])
        # Only a draft is generated. No backtest or confirmation is performed here.
        assert task.jobs == ()
    finally:
        service.stop()


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "case", ["invalid-call", "duplicate-id", "no-finish", "empty", "error-envelope"]
)
def test_malformed_responses_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, case: str
) -> None:
    payload = document(provider, [llm.ToolCall("c", "x", {})])
    if case == "invalid-call":
        if provider == "anthropic":
            payload["content"][-1]["input"] = []
        else:
            payload["choices"][0]["message"]["tool_calls"][0]["id"] = None
    elif case == "duplicate-id":
        payload = document(provider, [llm.ToolCall("c", "x", {}), llm.ToolCall("c", "x", {})])
    elif case == "no-finish":
        if provider == "anthropic":
            payload.pop("stop_reason")
        else:
            payload["choices"][0].pop("finish_reason")
    elif case == "empty":
        payload = (
            {"content": [], "stop_reason": "end_turn"}
            if provider == "anthropic"
            else {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": None},
                        "finish_reason": "stop",
                    }
                ]
            }
        )
    else:
        payload = {"error": {"message": KEY}}
    transport(monkeypatch, [payload])
    with pytest.raises(llm.ModelProviderError) as error:
        llm.create_model_port(store(tmp_path, provider)).complete(
            system="", messages=[llm.ModelMessage("user", "idea")]
        )
    assert KEY not in str(error.value)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("case", ["unmatched-result", "pending-call", "vendor-shape", "array-tool"])
def test_invalid_requests_never_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, case: str
) -> None:
    messages: Any = [llm.ModelMessage("user", "idea")]
    tools = None
    if case == "unmatched-result":
        messages += [llm.ModelMessage("tool", "result", call_id="unknown")]
    elif case == "pending-call":
        messages += [llm.ModelMessage("assistant", tool_calls=(llm.ToolCall("c", "x", {}),))]
    elif case == "vendor-shape":
        messages = [{"role": "user", "content": "idea"}]
    else:
        tools = [llm.ToolDefinition("x", "", {"type": "array"})]
    captured = transport(monkeypatch, [])
    with pytest.raises(llm.ModelNotConfigured):
        llm.create_model_port(store(tmp_path, provider)).complete(
            system="", messages=messages, tools=tools
        )
    assert captured == []


@pytest.mark.parametrize("provider", PROVIDERS)
def test_provider_failure_stays_charged_and_is_restart_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    from unit.test_web_research import _service

    service = _service(tmp_path)
    settings = store(tmp_path, provider)
    path = tmp_path / "research"
    orchestrator = research.ResearchOrchestrator(research.ResearchStore(path), service, settings)
    captured = transport(
        monkeypatch,
        [document(provider, [llm.ToolCall("c", "propose_spec", {})], finish="length")],
    )
    try:
        task = orchestrator.create("truncated fixture")
        assert task.status == "failed" and task.error is not None
        assert task.error["code"] == "provider_error"
        assert task.usage["model_calls"] == 1 and task.spent_calls == (1,)
        assert task.spec_draft is None and len(captured) == 1
        reopened = research.ResearchStore(path).get(task.task_id)
        assert reopened.error == task.error and reopened.spent_calls == task.spent_calls
    finally:
        service.stop()


def test_deepseek_native_tools_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = store(tmp_path, "deepseek")
    config = settings.load()
    config["model"]["base_url"] = "https://api.deepseek.com/v1"
    config["model"]["model"] = "deepseek-flash"
    settings.save(config)
    call = llm.ToolCall("call-one", "lookup", {"symbol": "AAPL"})
    recorded = transport(
        monkeypatch,
        [
            document("deepseek", [call], usage={"prompt_tokens": 7, "completion_tokens": 3}),
            document("deepseek"),
        ],
    )
    port = llm.create_model_port(settings)
    tools = [llm.ToolDefinition("lookup", "Lookup", {"type": "object"})]
    messages = [llm.ModelMessage("user", "idea")]
    response = port.complete(system="policy", messages=messages, tools=tools, max_tokens=321)
    assert response.tool_calls == (call,)
    assert response.usage_available and response.input_tokens == 7
    messages += [
        llm.ModelMessage("assistant", response.text, response.tool_calls),
        llm.ModelMessage("tool", "result", call_id=call.id),
    ]
    port.complete(system="policy", messages=messages, tools=tools)
    wire = recorded[0]["json"]
    assert recorded[0]["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert wire["model"] == "deepseek-flash" and wire["max_tokens"] == 321
    assert "max_completion_tokens" not in wire
    assert wire["thinking"] == {"type": "disabled"} and wire["stream"] is False
    assert wire["tools"][0]["function"]["parameters"] == tools[0].parameters
    assert recorded[1]["json"]["messages"][-1] == {
        "role": "tool",
        "content": "result",
        "tool_call_id": "call-one",
    }


def test_deepseek_rejects_nonofficial_endpoint_and_preserves_openai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = transport(monkeypatch, [document("openai-chat-completions-compatible")])
    port = llm.create_model_port(store(tmp_path, "deepseek"))
    with pytest.raises(llm.ModelNotConfigured, match="official"):
        port.complete(system="policy", messages=[llm.ModelMessage("user", "idea")])
    assert recorded == []
    port = llm.create_model_port(store(tmp_path, "openai-chat-completions-compatible"))
    port.complete(system="policy", messages=[llm.ModelMessage("user", "idea")], max_tokens=123)
    assert recorded[0]["json"]["max_completion_tokens"] == 123
    assert "thinking" not in recorded[0]["json"] and "max_tokens" not in recorded[0]["json"]
