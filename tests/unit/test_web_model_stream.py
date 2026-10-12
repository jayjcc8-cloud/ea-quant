"""Real-delta streaming adapter contract over a fake network transport.

Only the socket is faked: the request is built by the real httpx2 client and the
body is a real httpx2 Response whose bytes are produced one server-sent-event
frame at a time, recorded as they are pulled. A test can therefore prove that a
text delta reaches the caller while the terminal network event is still unread,
and that no delta was sliced out of a complete reply.
"""

from __future__ import annotations

import json
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any, cast

import httpx2 as httpx
import pytest

from ea.web import llm
from ea.web.settings import RadianSettings

PROVIDERS = ("anthropic", "openai-chat-completions-compatible")
KEY = "fixture-not-a-credential"


def store(tmp_path: Path, provider: str) -> RadianSettings:
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
        }
    )
    return settings


class Network:
    """A fake socket for one stream: records pulled frames and closes."""

    def __init__(self, chunks: list[str], status: int) -> None:
        self.chunks = chunks
        self.status = status
        self.pulled: list[int] = []
        self.closed: list[str] = []
        self.requests: list[httpx.Request] = []

    def body(self) -> httpx.SyncByteStream:
        network = self

        class Body(httpx.SyncByteStream):
            def __iter__(self) -> Iterator[bytes]:
                for index, chunk in enumerate(network.chunks):
                    network.pulled.append(index)
                    yield chunk.encode()

            def close(self) -> None:
                network.closed.append("body")

        return Body()

    def client(self) -> Any:
        network = self

        class Client:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def send(self, request: httpx.Request, *, stream: bool = False) -> httpx.Response:
                network.requests.append(request)
                return httpx.Response(network.status, stream=network.body())

            def close(self) -> None:
                network.closed.append("client")

        return Client

    def sent(self, index: int = 0) -> dict[str, Any]:
        request = self.requests[index]
        body = json.loads(request.content)
        assert isinstance(body, dict)
        return body


def transport(monkeypatch: pytest.MonkeyPatch, chunks: list[str], *, status: int = 200) -> Network:
    network = Network(chunks, status)
    monkeypatch.setattr(httpx, "Client", network.client())
    return network


def frames(*events: tuple[str, dict[str, Any]]) -> list[str]:
    """One network chunk per event, so pulled frames are observable."""

    return [
        f"event: {name}\ndata: {json.dumps(data)}\n\n" if name else f"data: {json.dumps(data)}\n\n"
        for name, data in events
    ]


def anthropic_body(
    *blocks: tuple[str, ...],
    calls: list[tuple[str, str, list[str]]] | None = None,
    stop_reason: str | None = "end_turn",
    usage: dict[str, Any] | None = None,
) -> list[str]:
    """One chunk per event. Each argument is one text block of real deltas."""

    events: list[tuple[str, dict[str, Any]]] = [
        ("message_start", {"type": "message_start", "message": {"usage": usage or {}}})
    ]
    index = 0
    for block in blocks:
        events.append(
            ("content_block_start", {"index": index, "content_block": {"type": "text", "text": ""}})
        )
        for part in block:
            events.append(
                (
                    "content_block_delta",
                    {"index": index, "delta": {"type": "text_delta", "text": part}},
                )
            )
        events.append(("content_block_stop", {"index": index}))
        index += 1
    for identifier, name, fragments in calls or []:
        events.append(
            (
                "content_block_start",
                {
                    "index": index,
                    "content_block": {"type": "tool_use", "id": identifier, "name": name},
                },
            )
        )
        for fragment in fragments:
            events.append(
                (
                    "content_block_delta",
                    {
                        "index": index,
                        "delta": {"type": "input_json_delta", "partial_json": fragment},
                    },
                )
            )
        events.append(("content_block_stop", {"index": index}))
        index += 1
    events.append(
        ("message_delta", {"delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": 7}})
    )
    events.append(("message_stop", {}))
    return frames(*events)


def openai_body(
    *,
    text: str | None = None,
    calls: list[tuple[str, str, list[str]]] | None = None,
    finish_reason: str | None = "stop",
    usage: dict[str, Any] | None = None,
    done: bool = True,
) -> list[str]:
    events: list[tuple[str, dict[str, Any]]] = []
    if text is not None:
        events.append(("", {"choices": [{"index": 0, "delta": {"content": text}}]}))
    for index, (identifier, name, fragments) in enumerate(calls or []):
        for position, fragment in enumerate(fragments):
            function: dict[str, Any] = {"arguments": fragment}
            if position == 0:
                function["name"] = name
            events.append(
                (
                    "",
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": index,
                                            "id": identifier if position == 0 else "",
                                            "function": function,
                                        }
                                    ]
                                },
                            }
                        ]
                    },
                )
            )
    final: dict[str, Any] = {"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}
    final["usage"] = None
    events.append(("", final))
    if usage is not None:
        events.append(("", {"choices": [], "usage": usage}))
    chunks = frames(*events)
    if done:
        chunks.append("data: [DONE]\n\n")
    return chunks


def events_of(stream: Iterator[llm.ModelStreamEvent]) -> list[llm.ModelStreamEvent]:
    return list(stream)


# 1. Each real text delta reaches the caller while the network event is open.
def test_anthropic_text_deltas_arrive_before_the_terminal_network_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    network = transport(monkeypatch, anthropic_body(("Al", "pha"), usage={"input_tokens": 4}))
    port = llm.create_model_port(store(tmp_path, "anthropic"))
    assert isinstance(port, llm.StreamingModelPort)
    terminal = next(index for index, chunk in enumerate(network.chunks) if "message_stop" in chunk)

    stream = port.stream(
        system="policy",
        messages=[llm.ModelMessage("user", "idea")],
        tools=[llm.ToolDefinition("lookup", "Lookup", {"type": "object"})],
    )
    assert network.pulled == [], "opening the stream must not read the body"

    first = next(stream)
    assert (first.kind, first.text, first.response) == ("text_delta", "Al", None)
    assert terminal not in network.pulled, "the first delta precedes the terminal event"

    second = next(stream)
    assert (second.kind, second.text, second.response) == ("text_delta", "pha", None)

    terminal_events = events_of(stream)
    assert [event.kind for event in terminal_events] == ["response"]
    response = terminal_events[0].response
    assert response is not None
    assert response.text == "Alpha" and response.stop_reason == "stop"
    assert response.model == "fixture-model"
    assert response.usage_available and (response.input_tokens, response.output_tokens) == (4, 7)
    assert network.closed == ["body", "client"]
    assert len(network.requests) == 1
    request = network.requests[0]
    assert str(request.url) == "http://fixture.invalid/api/v1/messages"
    assert request.headers["x-api-key"] == KEY
    assert network.sent()["stream"] is True and network.sent()["max_tokens"] == 2000


def test_stream_is_a_lazy_iterator_that_closes_the_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    network = transport(monkeypatch, anthropic_body(("Al", "pha")))
    port = llm.create_model_port(store(tmp_path, "anthropic"))
    stream = cast(
        "Generator[llm.ModelStreamEvent, None, None]",
        port.stream(system="", messages=[llm.ModelMessage("user", "idea")]),
    )
    assert network.requests == []
    assert next(stream).text == "Al"
    assert network.closed == []
    stream.close()
    assert network.closed == ["body", "client"], "an abandoned stream is closed, not leaked"


# 2. Text and tools coexist; arguments aggregate and validate only at the end.
def test_anthropic_split_tool_arguments_keep_text_and_matching_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport(
        monkeypatch,
        anthropic_body(
            ("Checking ",),
            ("the tape.",),
            calls=[
                ("call-one", "lookup", ['{"symbol"', ': "AA', 'PL"}']),
                ("call-two", "lookup", ['{"symbol": "MSFT"}']),
            ],
            stop_reason="tool_use",
        ),
    )
    port = llm.create_model_port(store(tmp_path, "anthropic"))
    events = events_of(port.stream(system="", messages=[llm.ModelMessage("user", "idea")]))
    assert [event.text for event in events if event.kind == "text_delta"] == [
        "Checking ",
        "the tape.",
    ]
    finals = [event.response for event in events if event.kind == "response"]
    assert len(finals) == 1
    response = finals[0]
    assert response is not None and response.stop_reason == "tool_calls"
    assert response.tool_calls == (
        llm.ToolCall("call-one", "lookup", {"symbol": "AAPL"}),
        llm.ToolCall("call-two", "lookup", {"symbol": "MSFT"}),
    )
    assert response.text == "Checking \nthe tape."


def test_anthropic_tool_only_response_emits_no_text_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport(
        monkeypatch,
        anthropic_body(
            calls=[("call-one", "lookup", ['{"sym', 'bol": "AAPL"}'])], stop_reason="tool_use"
        ),
    )
    port = llm.create_model_port(store(tmp_path, "anthropic"))
    events = events_of(port.stream(system="", messages=[llm.ModelMessage("user", "idea")]))
    assert [event.kind for event in events] == ["response"]
    response = events[0].response
    assert response is not None and response.text == ""
    assert response.tool_calls == (llm.ToolCall("call-one", "lookup", {"symbol": "AAPL"}),)


# 3. OpenAI-chat-compatible and DeepSeek SSE.
def test_openai_text_deltas_and_usage_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    network = transport(
        monkeypatch,
        openai_body(
            text="Hel", usage={"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11}
        ),
    )
    port = llm.create_model_port(store(tmp_path, "openai-chat-completions-compatible"))
    events = events_of(port.stream(system="", messages=[llm.ModelMessage("user", "idea")]))
    assert [event.text for event in events if event.kind == "text_delta"] == ["Hel"]
    response = events[-1].response
    assert response is not None
    assert (response.text, response.stop_reason) == ("Hel", "stop")
    assert (response.input_tokens, response.output_tokens) == (9, 2)
    assert str(network.requests[0].url) == "http://fixture.invalid/api/chat/completions"
    assert network.sent()["stream"] is True
    assert network.sent()["stream_options"] == {"include_usage": True}


@pytest.mark.parametrize("with_tool", [False, True])
def test_openai_usage_trailer_settles_once_after_the_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_tool: bool
) -> None:
    network = transport(
        monkeypatch,
        openai_body(
            text="ok",
            calls=[("call-one", "lookup", ['{"symbol":"AAPL"}'])] if with_tool else None,
            finish_reason="tool_calls" if with_tool else "stop",
            usage={"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
        ),
    )
    port = llm.create_model_port(store(tmp_path, "openai-chat-completions-compatible"))
    stream = port.stream(system="", messages=[llm.ModelMessage("user", "idea")])
    assert next(stream).text == "ok"
    assert len(network.pulled) == 1
    final = list(stream)
    assert [event.kind for event in final] == ["response"]
    response = final[0].response
    assert response is not None and response.text == "ok" and response.usage_available
    assert (response.input_tokens, response.output_tokens) == (9, 2)
    assert response.stop_reason == ("tool_calls" if with_tool else "stop")
    assert response.tool_calls == (
        (llm.ToolCall("call-one", "lookup", {"symbol": "AAPL"}),) if with_tool else ()
    )
    assert len(network.requests) == 1
    assert network.closed == ["body", "client"]


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"prompt_tokens": 9},
        {"prompt_tokens": -1, "completion_tokens": 2},
        {"prompt_tokens": True, "completion_tokens": 2},
        {"prompt_tokens": 9, "completion_tokens": "2"},
    ],
)
def test_openai_malformed_usage_trailer_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, usage: Any
) -> None:
    chunks = openai_body(text="ok", done=False)
    chunks += frames(("", {"choices": [], "usage": usage}))
    chunks.append("data: [DONE]\n\n")
    transport(monkeypatch, chunks)
    port = llm.create_model_port(store(tmp_path, "openai-chat-completions-compatible"))
    stream = port.stream(system="", messages=[llm.ModelMessage("user", "idea")])
    assert next(stream).text == "ok"
    with pytest.raises(llm.ModelProviderError):
        list(stream)


@pytest.mark.parametrize("finish", [None, "length"])
def test_openai_usage_trailer_never_substitutes_for_a_successful_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finish: str | None
) -> None:
    transport(
        monkeypatch,
        openai_body(
            text="partial",
            finish_reason=finish,
            usage={"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
        ),
    )
    port = llm.create_model_port(store(tmp_path, "openai-chat-completions-compatible"))
    stream = port.stream(system="", messages=[llm.ModelMessage("user", "idea")])
    assert next(stream).text == "partial"
    with pytest.raises(llm.ModelProviderError):
        list(stream)


def test_openai_split_tool_arguments_aggregate_by_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport(
        monkeypatch,
        openai_body(
            calls=[("call-one", "lookup", ['{"symbol"', ': "AA', 'PL", "n": 1}'])],
            finish_reason="tool_calls",
        ),
    )
    port = llm.create_model_port(store(tmp_path, "openai-chat-completions-compatible"))
    events = events_of(port.stream(system="", messages=[llm.ModelMessage("user", "idea")]))
    assert [event.kind for event in events] == ["response"]
    response = events[0].response
    assert response is not None and response.stop_reason == "tool_calls"
    assert response.tool_calls == (llm.ToolCall("call-one", "lookup", {"symbol": "AAPL", "n": 1}),)


def test_openai_two_tool_calls_aggregate_by_index_not_arrival_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def call(index: int, identifier: str, name: str, arguments: str) -> tuple[str, dict[str, Any]]:
        function: dict[str, Any] = {"arguments": arguments}
        if name:
            function["name"] = name
        return (
            "",
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [{"index": index, "id": identifier, "function": function}]
                        },
                    }
                ]
            },
        )

    chunks = frames(
        call(0, "a", "one", '{"x"'),
        call(1, "b", "two", '{"y"'),
        call(0, "", "", ": 1}"),
        call(1, "", "", ": 2}"),
        ("", {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
    )
    chunks.append("data: [DONE]\n\n")
    transport(monkeypatch, chunks)
    port = llm.create_model_port(store(tmp_path, "openai-chat-completions-compatible"))
    response = events_of(port.stream(system="", messages=[llm.ModelMessage("user", "idea")]))[
        -1
    ].response
    assert response is not None
    assert response.tool_calls == (
        llm.ToolCall("a", "one", {"x": 1}),
        llm.ToolCall("b", "two", {"y": 2}),
    )


def test_deepseek_stream_keeps_its_native_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = store(tmp_path, "deepseek")
    config = settings.load()
    config["model"]["base_url"] = "https://api.deepseek.com/v1"
    settings.save(config)
    network = transport(
        monkeypatch,
        openai_body(text="ok", usage={"prompt_tokens": 3, "completion_tokens": 1}),
    )
    port = llm.create_model_port(settings)
    response = events_of(port.stream(system="", messages=[llm.ModelMessage("user", "idea")]))[
        -1
    ].response
    assert response is not None and response.text == "ok"
    assert (response.input_tokens, response.output_tokens) == (3, 1)
    wire = network.sent()
    assert str(network.requests[0].url) == "https://api.deepseek.com/v1/chat/completions"
    assert wire["max_tokens"] == 2000 and "max_completion_tokens" not in wire
    assert wire["thinking"] == {"type": "disabled"} and wire["stream"] is True
    assert "stream_options" not in wire
    assert network.requests[0].headers["authorization"] == f"Bearer {KEY}"


def test_openai_ignores_keepalive_frames_without_choices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chunks = frames(("", {"choices": [{"index": 0, "delta": {"content": "ok"}}]}))
    chunks += frames(("", {"keepalive": True}))
    chunks += openai_body(usage={"prompt_tokens": 1, "completion_tokens": 1})
    transport(monkeypatch, chunks)
    port = llm.create_model_port(store(tmp_path, "openai-chat-completions-compatible"))
    events = events_of(port.stream(system="", messages=[llm.ModelMessage("user", "idea")]))
    assert [event.text for event in events if event.kind == "text_delta"] == ["ok"]
    response = events[-1].response
    assert response is not None and (response.input_tokens, response.output_tokens) == (1, 1)


# 4. Truncated and malformed streams fail closed with secret-free errors.
def test_truncated_tool_arguments_fail_closed_after_real_text_deltas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport(
        monkeypatch,
        openai_body(
            text="thinking", calls=[("call-one", "lookup", ['{"symbol": "AA'])], done=False
        ),
    )
    port = llm.create_model_port(store(tmp_path, "openai-chat-completions-compatible"))
    stream = port.stream(system="", messages=[llm.ModelMessage("user", "idea")])
    assert next(stream).text == "thinking"
    with pytest.raises(llm.ModelProviderError) as error:
        events_of(stream)
    assert "arguments" in str(error.value) and KEY not in str(error.value)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "case",
    [
        "invalid-json",
        "no-terminal",
        "duplicate-tool-id",
        "length-finish",
        "empty-stream",
        "error-envelope",
        "non-choice-frame",
        "stray-tool-delta",
    ],
)
def test_malformed_streams_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, case: str
) -> None:
    chunks: list[str]
    if provider == "anthropic":
        if case == "invalid-json":
            chunks = ["data: {not json\n\n"]
        elif case == "empty-stream":
            chunks = []
        elif case == "error-envelope":
            chunks = frames(("error", {"error": {"message": KEY}}))
        elif case == "non-choice-frame":
            chunks = frames(("mystery", {}))
        elif case == "stray-tool-delta":
            chunks = frames(
                (
                    "content_block_start",
                    {"index": 0, "content_block": {"type": "text", "text": ""}},
                ),
                (
                    "content_block_delta",
                    {"index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
                ),
            )
        elif case == "duplicate-tool-id":
            chunks = anthropic_body(
                calls=[("dup", "x", ["{}"]), ("dup", "x", ["{}"])], stop_reason="tool_use"
            )
        elif case == "no-terminal":
            chunks = anthropic_body(("partial",))[:-1]
        else:
            chunks = anthropic_body(("partial",), stop_reason="length")
    elif case == "invalid-json":
        chunks = ["data: {not json\n\n"]
    elif case == "empty-stream":
        chunks = []
    elif case == "error-envelope":
        chunks = frames(("", {"error": {"message": KEY}}))
    elif case == "non-choice-frame":
        chunks = frames(("", {"choices": [{}, {}]}))
    elif case == "stray-tool-delta":
        chunks = frames(
            ("", {"choices": [{"index": 0, "delta": {"tool_calls": [{"function": {}}]}}]})
        )
    elif case == "duplicate-tool-id":
        chunks = openai_body(
            calls=[("dup", "x", ["{}"]), ("dup", "x", ["{}"])], finish_reason="tool_calls"
        )
    elif case == "no-terminal":
        chunks = openai_body(text="partial", finish_reason=None)
    else:
        chunks = openai_body(text="partial", finish_reason="length")

    transport(monkeypatch, chunks)
    port = llm.create_model_port(store(tmp_path, provider))
    with pytest.raises(llm.ModelProviderError) as error:
        events_of(port.stream(system="", messages=[llm.ModelMessage("user", "idea")]))
    assert KEY not in str(error.value)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("status", [401, 429, 500])
def test_streaming_http_errors_hide_the_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, status: int
) -> None:
    network = transport(monkeypatch, frames(("", {"error": {"message": KEY}})), status=status)
    port = llm.create_model_port(store(tmp_path, provider))
    with pytest.raises(llm.ModelProviderError) as error:
        events_of(port.stream(system="", messages=[llm.ModelMessage("user", "idea")]))
    assert str(status) in str(error.value) and KEY not in str(error.value)
    assert network.pulled == [] and network.closed == ["body", "client"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_stream_transport_errors_are_secret_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    closed: list[str] = []

    class Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def send(self, request: httpx.Request, *, stream: bool = False) -> httpx.Response:
            raise httpx.ReadError(f"connection reset while using {KEY}")

        def close(self) -> None:
            closed.append("client")

    monkeypatch.setattr(httpx, "Client", Client)
    port = llm.create_model_port(store(tmp_path, provider))
    with pytest.raises(llm.ModelProviderError) as error:
        events_of(port.stream(system="", messages=[llm.ModelMessage("user", "idea")]))
    assert KEY not in str(error.value) and closed == ["client"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_invalid_requests_and_missing_capability_never_open_a_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    network = transport(monkeypatch, [])
    port = llm.create_model_port(store(tmp_path, provider))
    vendor_shaped: Any = [{"role": "user", "content": "idea"}]
    with pytest.raises(llm.ModelNotConfigured):
        events_of(port.stream(system="", messages=vendor_shaped))
    with pytest.raises(llm.ModelNotConfigured, match="streaming"):
        events_of(
            port.stream(
                system="",
                messages=[llm.ModelMessage("user", "idea")],
                required_capabilities=("streaming",),
            )
        )
    assert network.requests == [] and network.pulled == []


def test_complete_remains_nonstreaming_on_the_same_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> httpx.Response:
        recorded.append({"url": url, **kwargs})
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "reply"}], "stop_reason": "end_turn"}
        )

    monkeypatch.setattr(httpx, "post", post)
    port = llm.create_model_port(store(tmp_path, "anthropic"))
    response = port.complete(system="", messages=[llm.ModelMessage("user", "idea")])
    assert response.text == "reply"
    assert recorded[0]["json"]["stream"] is False
    assert isinstance(port, llm.ModelPort) and port.route() is not None
