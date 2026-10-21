"""Neutral text/function-call port with two explicit wire adapters.

Each adapter serves one nonstreaming completion and one real streaming mode.
Product model/endpoint/credentials are independent of developer tooling. Custom
endpoints must implement the selected protocol; no protocol/provider fallback.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

import httpx2 as httpx

from ea.web.settings import RadianSettings, RadianSettingsError

_REQUEST_TIMEOUT_SECONDS = 60.0


class ModelNotConfigured(ValueError):
    """Configuration or requested capability is unavailable before dispatch."""


class ModelProviderError(ValueError):
    """Dispatch/response failure with a secret-free public message."""


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    tool_use: bool
    structured_output: bool
    usage: bool

    def document(self) -> dict[str, bool]:
        return {
            "tool_use": self.tool_use,
            "structured_output": self.structured_output,
            "usage": self.usage,
        }


@dataclass(frozen=True, slots=True)
class ModelRoute:
    provider: str
    protocol: str
    model: str
    endpoint: str
    capabilities: ModelCapabilities


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """Neutral text, assistant calls, or one tool result tied to its call ID."""

    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str
    tool_calls: tuple[ToolCall, ...]
    stop_reason: Literal["stop", "tool_calls"]
    model: str
    input_tokens: int | None
    output_tokens: int | None

    @property
    def usage_available(self) -> bool:
        return self.input_tokens is not None and self.output_tokens is not None


@dataclass(frozen=True, slots=True)
class ModelStreamEvent:
    """One real network event: a text delta, or the single final response."""

    kind: Literal["text_delta", "response"]
    text: str = ""
    response: ModelResponse | None = None


@runtime_checkable
class ModelPort(Protocol):
    def route(self) -> ModelRoute | None: ...

    def complete(
        self,
        *,
        system: str,
        messages: list[ModelMessage],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 2000,
        required_capabilities: tuple[str, ...] = (),
    ) -> ModelResponse: ...


@runtime_checkable
class StreamingModelPort(Protocol):
    """A ModelPort that also yields real incremental network events."""

    def route(self) -> ModelRoute | None: ...

    def complete(
        self,
        *,
        system: str,
        messages: list[ModelMessage],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 2000,
        required_capabilities: tuple[str, ...] = (),
    ) -> ModelResponse: ...

    def stream(
        self,
        *,
        system: str,
        messages: list[ModelMessage],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 2000,
        required_capabilities: tuple[str, ...] = (),
    ) -> Iterator[ModelStreamEvent]: ...


PROVIDER_PROTOCOLS: dict[str, tuple[str, ModelCapabilities]] = {
    "anthropic": ("anthropic-messages", ModelCapabilities(True, False, True)),
    "deepseek": ("deepseek-chat-completions", ModelCapabilities(True, False, True)),
    "openai-chat-completions-compatible": (
        "openai-chat-completions",
        ModelCapabilities(True, False, True),
    ),
}
REQUIRED_RESEARCH_CAPABILITIES: tuple[str, ...] = ("tool_use",)


def protocol_for(provider: str | None) -> str | None:
    entry = PROVIDER_PROTOCOLS.get(provider or "")
    return None if entry is None else entry[0]


def capabilities_for(provider: str | None) -> ModelCapabilities | None:
    entry = PROVIDER_PROTOCOLS.get(provider or "")
    return None if entry is None else entry[1]


def missing_capabilities(
    capabilities: ModelCapabilities | None,
    required: tuple[str, ...] = REQUIRED_RESEARCH_CAPABILITIES,
) -> tuple[str, ...]:
    return tuple(name for name in required if not getattr(capabilities, name, False))


def model_projection(
    provider: str | None, model: str | None, endpoint: str | None
) -> dict[str, Any]:
    capabilities = capabilities_for(provider)
    return {
        "protocol": protocol_for(provider),
        "model": model,
        "endpoint": endpoint,
        "capabilities": None if capabilities is None else capabilities.document(),
        "required_capabilities": list(REQUIRED_RESEARCH_CAPABILITIES),
        "missing_capabilities": list(missing_capabilities(capabilities)),
    }


def _call(identifier: object, name: object, arguments: object) -> ToolCall:
    if (
        not isinstance(identifier, str)
        or not identifier
        or not isinstance(name, str)
        or not name
        or not isinstance(arguments, dict)
    ):
        raise ModelProviderError("model provider returned an invalid tool call")
    try:
        json.dumps(arguments, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ModelProviderError("model provider returned invalid tool arguments") from error
    return ToolCall(identifier, name, arguments)


def _usage(
    document: dict[str, Any], input_key: str, output_key: str
) -> tuple[int | None, int | None]:
    usage = document.get("usage")
    if not isinstance(usage, dict):
        return None, None
    values = (usage.get(input_key), usage.get(output_key))
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in values):
        return None, None
    return values


def _response(
    text: str,
    calls: list[ToolCall],
    reason: object,
    model: str,
    usage: tuple[int | None, int | None],
    *,
    tool_reason: str,
    stop_reasons: tuple[str, ...],
) -> ModelResponse:
    if reason == tool_reason and calls:
        finish: Literal["stop", "tool_calls"] = "tool_calls"
    elif reason in stop_reasons and not calls:
        finish = "stop"
    else:
        # No partial tool may escape on length/refusal/unknown/inconsistent finish.
        failure = {
            "length": "length",
            "max_tokens": "length",
            "refusal": "refusal",
            "content_filter": "refusal",
        }.get(str(reason), "unknown")
        raise ModelProviderError(f"model provider returned an incomplete finish ({failure})")
    if not text and not calls:
        raise ModelProviderError("model provider returned empty content")
    if len({call.id for call in calls}) != len(calls):
        raise ModelProviderError("model provider returned duplicate tool call IDs")
    return ModelResponse(text, tuple(calls), finish, model, *usage)


@dataclass(slots=True)
class _ToolFragment:
    """One streamed tool call: identify once, then append argument fragments."""

    identifier: str | None
    name: str | None
    arguments: str = ""


@dataclass(slots=True)
class _StreamState:
    """Accumulated stream truth. Nothing here is trusted before completion."""

    text: list[str] = field(default_factory=list)
    fragments: dict[int, _ToolFragment] = field(default_factory=dict)
    open_block: tuple[int, str] | None = None
    stop_reason: object = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    terminal: bool = False

    def push_text(self, piece: str) -> None:
        """Extend the current text block, keeping block-level text semantics."""

        if not self.text:
            self.text.append("")
        self.text[-1] += piece


class _OpenStream:
    """A live response whose body is read lazily and closed exactly once."""

    def __init__(self, client: httpx.Client, response: httpx.Response) -> None:
        self.lines = response.iter_lines()
        self.status_code = response.status_code
        self._client = client
        self._response = response

    def close(self) -> None:
        self._response.close()
        self._client.close()


def _stream_start(url: str, headers: dict[str, str], payload: dict[str, Any]) -> _OpenStream:
    client = httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS)
    try:
        response = client.send(
            httpx.Request("POST", url, headers=headers, json=payload), stream=True
        )
    except httpx.HTTPError as error:
        client.close()
        raise ModelProviderError("model provider is unreachable") from error
    return _OpenStream(client, response)


def _block_index(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ModelProviderError("model provider returned an invalid content block")
    return value


def _arguments(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError as error:
        raise ModelProviderError("model provider returned invalid tool arguments") from error


def _usage_value(container: dict[str, Any], key: str) -> int | None:
    usage = container.get("usage")
    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


class _Provider:
    provider = ""
    default_base_url = ""

    def __init__(self, settings: RadianSettings):
        self._settings = settings

    def _config(self) -> dict[str, Any]:
        try:
            model = self._settings.profile()[1]
        except RadianSettingsError as error:
            raise ModelNotConfigured("settings are unreadable") from error
        if not isinstance(model, dict) or model.get("provider") != self.provider:
            raise ModelNotConfigured("no model provider is configured")
        api_key = model.get("api_key")
        if not api_key:
            env_name = model.get("api_key_env")
            api_key = os.environ.get(env_name or "", "") if env_name else ""
        if not api_key or not model.get("model"):
            raise ModelNotConfigured("model provider is not configured")
        missing = missing_capabilities(capabilities_for(self.provider))
        if missing:
            raise ModelNotConfigured(
                "the configured model protocol does not declare: " + ", ".join(missing)
            )
        return {
            "api_key": api_key,
            "model": model["model"],
            "base_url": (model.get("base_url") or self.default_base_url).rstrip("/"),
        }

    def route(self) -> ModelRoute | None:
        try:
            config = self._config()
        except ModelNotConfigured:
            return None
        capabilities = capabilities_for(self.provider)
        assert capabilities is not None
        return ModelRoute(
            self.provider,
            protocol_for(self.provider) or "",
            config["model"],
            config["base_url"],
            capabilities,
        )

    def _validate(
        self,
        messages: list[ModelMessage],
        tools: list[ToolDefinition] | None,
        max_tokens: int,
        required: tuple[str, ...],
    ) -> None:
        required = (*REQUIRED_RESEARCH_CAPABILITIES, *required)
        missing = missing_capabilities(capabilities_for(self.provider), required)
        if missing:
            raise ModelNotConfigured(
                "the configured model protocol does not declare: " + ", ".join(missing)
            )
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
            raise ModelNotConfigured("max_tokens must be a positive integer")
        pending: set[str] = set()
        seen: set[str] = set()
        for message in messages:
            if not isinstance(message, ModelMessage) or not isinstance(message.text, str):
                raise ModelNotConfigured("only neutral text/function messages are supported")
            if message.role == "tool":
                if message.call_id is None or message.call_id not in pending or message.tool_calls:
                    raise ModelNotConfigured("tool result must match one pending call")
                pending.remove(message.call_id)
            elif message.role in {"user", "assistant"}:
                if pending or message.call_id or (message.role == "user" and message.tool_calls):
                    raise ModelNotConfigured("invalid model message sequence")
                for call in message.tool_calls:
                    if (
                        not isinstance(call, ToolCall)
                        or not call.id
                        or not call.name
                        or not isinstance(call.arguments, dict)
                        or call.id in seen
                    ):
                        raise ModelNotConfigured("invalid assistant tool call")
                    pending.add(call.id)
                    seen.add(call.id)
            else:
                raise ModelNotConfigured("unsupported message role")
        if pending:
            raise ModelNotConfigured("tool calls require results before the next completion")
        try:
            for message in messages:
                for call in message.tool_calls:
                    json.dumps(call.arguments, allow_nan=False)
            for tool in tools or []:
                if isinstance(tool, ToolDefinition):
                    json.dumps(tool.parameters, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ModelNotConfigured("model tools require finite JSON values") from error
        for tool in tools or []:
            if (
                not isinstance(tool, ToolDefinition)
                or not tool.name
                or not isinstance(tool.parameters, dict)
                or tool.parameters.get("type") != "object"
            ):
                raise ModelNotConfigured("only JSON-object function tools are supported")

    def _post(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = httpx.post(
                url, headers=headers, json=payload, timeout=_REQUEST_TIMEOUT_SECONDS
            )
        except httpx.HTTPError as error:
            raise ModelProviderError("model provider is unreachable") from error
        if response.status_code != 200:
            raise ModelProviderError(
                f"model provider rejected the request ({response.status_code})"
            )
        try:
            document = response.json()
        except ValueError as error:
            raise ModelProviderError("model provider returned an unreadable response") from error
        if not isinstance(document, dict) or document.get("error"):
            raise ModelProviderError("model provider returned an invalid response")
        return document

    def _prepare(
        self,
        messages: list[ModelMessage],
        tools: list[ToolDefinition] | None,
        max_tokens: int,
        required: tuple[str, ...],
    ) -> dict[str, Any]:
        config = self._config()
        self._validate(messages, tools, max_tokens, required)
        return config

    def _sse_events(self, lines: Iterator[str]) -> Iterator[tuple[str | None, str]]:
        """Yield (event name, data) per server-sent event, comments excluded."""

        event: str | None = None
        data: list[str] = []
        for raw in lines:
            line = raw if isinstance(raw, str) else ""
            if not line:
                if data:
                    yield event, "\n".join(data)
                event, data = None, []
            elif line.startswith(":"):
                continue
            else:
                field, _, value = line.partition(":")
                if value.startswith(" "):
                    value = value[1:]
                if field == "data":
                    data.append(value)
                elif field == "event":
                    event = value
        if data:
            yield event, "\n".join(data)

    def _sse_decoded(
        self, events: Iterator[tuple[str | None, str]]
    ) -> Iterator[tuple[str | None, dict[str, Any]]]:
        for name, payload in events:
            if payload.strip() == "[DONE]":
                return
            try:
                event = json.loads(payload)
            except ValueError as error:
                raise ModelProviderError(
                    "model provider returned an unreadable response"
                ) from error
            if not isinstance(event, dict) or event.get("error"):
                raise ModelProviderError("model provider returned an invalid response")
            yield name, event

    def _validated_stream(
        self,
        request: Callable[[], _OpenStream],
        state: _StreamState,
        model: str,
        *,
        tool_reason: str,
        stop_reasons: tuple[str, ...],
    ) -> Iterator[ModelStreamEvent]:
        """Yield the real network deltas, then exactly one validated terminal response."""

        stream = request()
        if stream.status_code != 200:
            stream.close()
            raise ModelProviderError(f"model provider rejected the request ({stream.status_code})")
        try:
            for name, event in self._sse_decoded(self._sse_events(stream.lines)):
                yield from self._events(event, state, name)
        except httpx.HTTPError as error:
            raise ModelProviderError("model provider is unreachable") from error
        finally:
            stream.close()
        if not state.terminal:
            raise ModelProviderError("model provider returned a truncated stream")
        calls = [
            _call(fragment.identifier, fragment.name, _arguments(fragment.arguments))
            for fragment in state.fragments.values()
        ]
        yield ModelStreamEvent(
            "response",
            response=_response(
                "\n".join(state.text),
                calls,
                state.stop_reason,
                model,
                (state.input_tokens, state.output_tokens),
                tool_reason=tool_reason,
                stop_reasons=stop_reasons,
            ),
        )

    def _events(
        self, event: dict[str, Any], state: _StreamState, name: str | None = None
    ) -> Iterator[ModelStreamEvent]:
        raise NotImplementedError


class AnthropicProvider(_Provider):
    provider = "anthropic"
    default_base_url = "https://api.anthropic.com"

    def _request_body(
        self,
        system: str,
        messages: list[ModelMessage],
        tools: list[ToolDefinition] | None,
        max_tokens: int,
    ) -> dict[str, Any]:
        wire: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": message.call_id,
                    "content": message.text,
                }
                if wire and wire[-1]["role"] == "user" and isinstance(wire[-1]["content"], list):
                    wire[-1]["content"].append(block)
                else:
                    wire.append({"role": "user", "content": [block]})
            elif message.tool_calls:
                blocks: list[dict[str, Any]] = (
                    [{"type": "text", "text": message.text}] if message.text else []
                )
                blocks.extend(
                    {
                        "type": "tool_use",
                        "id": c.id,
                        "name": c.name,
                        "input": c.arguments,
                    }
                    for c in message.tool_calls
                )
                wire.append({"role": "assistant", "content": blocks})
            else:
                wire.append({"role": message.role, "content": message.text})
        payload: dict[str, Any] = {
            "system": system,
            "messages": wire,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in tools
            ]
        return payload

    def complete(
        self,
        *,
        system: str,
        messages: list[ModelMessage],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 2000,
        required_capabilities: tuple[str, ...] = (),
    ) -> ModelResponse:
        config = self._prepare(messages, tools, max_tokens, required_capabilities)
        payload = self._request_body(system, messages, tools, max_tokens)
        payload["model"] = config["model"]
        payload["stream"] = False
        document = self._post(
            f"{config['base_url']}/v1/messages",
            {
                "x-api-key": config["api_key"],
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            payload,
        )
        content = document.get("content")
        if not isinstance(content, list):
            raise ModelProviderError("model provider response is missing content")
        text: list[str] = []
        calls: list[ToolCall] = []
        for response_block in content:
            if not isinstance(response_block, dict):
                raise ModelProviderError("model provider returned an invalid content block")
            if response_block.get("type") == "text" and isinstance(response_block.get("text"), str):
                text.append(response_block["text"])
            elif response_block.get("type") == "tool_use":
                calls.append(
                    _call(
                        response_block.get("id"),
                        response_block.get("name"),
                        response_block.get("input"),
                    )
                )
            else:
                raise ModelProviderError("model provider returned unsupported content")
        return _response(
            "\n".join(text),
            calls,
            document.get("stop_reason"),
            config["model"],
            _usage(document, "input_tokens", "output_tokens"),
            tool_reason="tool_use",
            stop_reasons=("end_turn", "stop_sequence"),
        )

    def _events(
        self, event: dict[str, Any], state: _StreamState, name: str | None = None
    ) -> Iterator[ModelStreamEvent]:
        kind = event.get("type") or name
        if kind == "message_start":
            message = event.get("message")
            if isinstance(message, dict):
                state.input_tokens = _usage_value(message, "input_tokens")
                state.output_tokens = _usage_value(message, "output_tokens")
        elif kind == "content_block_start":
            index = _block_index(event.get("index"))
            block = event.get("content_block")
            if not isinstance(block, dict):
                raise ModelProviderError("model provider returned an invalid content block")
            block_type = block.get("type")
            if block_type == "text":
                initial = block.get("text") or ""
                if not isinstance(initial, str):
                    raise ModelProviderError("model provider returned unsupported content")
                state.text.append(initial)
                state.open_block = (index, "text")
                if initial:
                    yield ModelStreamEvent("text_delta", text=initial)
            elif block_type == "tool_use":
                state.fragments[index] = _ToolFragment(block.get("id"), block.get("name"))
                state.open_block = (index, "tool_use")
            else:
                raise ModelProviderError("model provider returned unsupported content")
        elif kind == "content_block_delta":
            index = _block_index(event.get("index"))
            delta = event.get("delta")
            if not isinstance(delta, dict):
                raise ModelProviderError("model provider returned unsupported content")
            if delta.get("type") == "text_delta":
                piece = delta.get("text")
                if not isinstance(piece, str) or state.open_block != (index, "text"):
                    raise ModelProviderError("model provider returned unsupported content")
                if piece:
                    state.push_text(piece)
                    yield ModelStreamEvent("text_delta", text=piece)
            elif delta.get("type") == "input_json_delta":
                fragment = state.fragments.get(index)
                if state.open_block != (index, "tool_use") or fragment is None:
                    raise ModelProviderError("model provider returned unsupported content")
                partial = delta.get("partial_json")
                if not isinstance(partial, str):
                    raise ModelProviderError("model provider returned invalid tool arguments")
                fragment.arguments += partial
            else:
                raise ModelProviderError("model provider returned unsupported content")
        elif kind == "content_block_stop":
            if state.open_block is not None and state.open_block[0] == event.get("index"):
                state.open_block = None
        elif kind == "message_delta":
            delta = event.get("delta")
            if isinstance(delta, dict) and delta.get("stop_reason") is not None:
                state.stop_reason = delta["stop_reason"]
            output = _usage_value(event, "output_tokens")
            if output is not None:
                state.output_tokens = output
        elif kind == "message_stop":
            state.terminal = True
        elif kind == "ping":
            return
        else:
            raise ModelProviderError(f"model provider returned an unknown stream event ({kind})")

    def stream(
        self,
        *,
        system: str,
        messages: list[ModelMessage],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 2000,
        required_capabilities: tuple[str, ...] = (),
    ) -> Iterator[ModelStreamEvent]:
        config = self._prepare(messages, tools, max_tokens, required_capabilities)
        payload = self._request_body(system, messages, tools, max_tokens)
        payload["model"] = config["model"]
        payload["stream"] = True
        url = f"{config['base_url']}/v1/messages"
        headers = {
            "x-api-key": config["api_key"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        return self._validated_stream(
            lambda: _stream_start(url, headers, payload),
            _StreamState(),
            config["model"],
            tool_reason="tool_use",
            stop_reasons=("end_turn", "stop_sequence"),
        )


class OpenAIChatCompletionsCompatibleProvider(_Provider):
    """Nonstreaming text/function subset; base_url is an API root including /v1.

    Uses max_completion_tokens. Endpoints/models must support this field, system
    text and function tools. Responses, legacy function_call, custom tools,
    reasoning history, multimedia and strict output are unsupported.
    """

    provider = "openai-chat-completions-compatible"
    default_base_url = "https://api.openai.com/v1"
    token_parameter = "max_completion_tokens"

    def _body(
        self,
        system: str,
        messages: list[ModelMessage],
        tools: list[ToolDefinition] | None,
        max_tokens: int,
    ) -> dict[str, Any]:
        wire: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for message in messages:
            item: dict[str, Any] = {"role": message.role, "content": message.text}
            if message.role == "tool":
                item["tool_call_id"] = message.call_id
            elif message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {
                            "name": c.name,
                            "arguments": json.dumps(c.arguments, allow_nan=False),
                        },
                    }
                    for c in message.tool_calls
                ]
            wire.append(item)
        payload: dict[str, Any] = {self.token_parameter: max_tokens, "messages": wire}
        if self.provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                        "strict": False,
                    },
                }
                for t in tools
            ]
        return payload

    def _events(
        self, event: dict[str, Any], state: _StreamState, name: str | None = None
    ) -> Iterator[ModelStreamEvent]:
        choices = event.get("choices")
        if choices is None:
            return
        if choices == []:
            # include_usage ends with a usage-only chunk after the choice finish.
            # It updates that response's accounting, never its termination state.
            prompt = _usage_value(event, "prompt_tokens")
            completion = _usage_value(event, "completion_tokens")
            if not state.terminal or prompt is None or completion is None:
                raise ModelProviderError("model provider returned an invalid usage trailer")
            state.input_tokens = prompt
            state.output_tokens = completion
            return
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ModelProviderError("model provider response must have one choice")
        choice = choices[0]
        delta = choice.get("delta")
        if isinstance(delta, dict):
            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise ModelProviderError("model provider returned unsupported content")
                if content:
                    state.push_text(content)
                    yield ModelStreamEvent("text_delta", text=content)
            raw_calls = delta.get("tool_calls") or []
            if not isinstance(raw_calls, list):
                raise ModelProviderError("model provider returned invalid tool calls")
            for raw in raw_calls:
                if not isinstance(raw, dict):
                    raise ModelProviderError("model provider returned invalid tool calls")
                fragment = state.fragments.setdefault(
                    _block_index(raw.get("index")), _ToolFragment(None, None)
                )
                identifier = raw.get("id")
                if identifier is not None and fragment.identifier is None:
                    fragment.identifier = identifier
                function = raw.get("function")
                if not isinstance(function, dict):
                    raise ModelProviderError("model provider returned invalid tool arguments")
                name = function.get("name")
                if name is not None and fragment.name is None:
                    fragment.name = name
                arguments = function.get("arguments")
                if arguments is not None:
                    if not isinstance(arguments, str):
                        raise ModelProviderError("model provider returned invalid tool arguments")
                    fragment.arguments += arguments
        finish = choice.get("finish_reason")
        if finish is not None:
            if not isinstance(finish, str):
                raise ModelProviderError("model provider returned an incomplete finish (unknown)")
            if state.stop_reason is not None and state.stop_reason != finish:
                raise ModelProviderError("model provider returned an inconsistent finish")
            state.stop_reason = finish
            state.terminal = True
        prompt = _usage_value(event, "prompt_tokens")
        if prompt is not None:
            state.input_tokens = prompt
        completion = _usage_value(event, "completion_tokens")
        if completion is not None:
            state.output_tokens = completion

    def complete(
        self,
        *,
        system: str,
        messages: list[ModelMessage],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 2000,
        required_capabilities: tuple[str, ...] = (),
    ) -> ModelResponse:
        config = self._prepare(messages, tools, max_tokens, required_capabilities)
        payload = self._body(system, messages, tools, max_tokens)
        payload["model"] = config["model"]
        payload["stream"] = False
        document = self._post(
            f"{config['base_url']}/chat/completions",
            {
                "Authorization": f"Bearer {config['api_key']}",
                "content-type": "application/json",
            },
            payload,
        )
        choices = document.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ModelProviderError("model provider response must have one choice")
        choice = choices[0]
        response_message = choice.get("message")
        if (
            not isinstance(response_message, dict)
            or response_message.get("role") != "assistant"
            or response_message.get("refusal")
            or response_message.get("function_call")
        ):
            raise ModelProviderError("model provider returned unsupported or refused content")
        text = response_message.get("content")
        if text is not None and not isinstance(text, str):
            raise ModelProviderError("model provider returned unsupported content")
        raw_calls = response_message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise ModelProviderError("model provider returned invalid tool calls")
        calls: list[ToolCall] = []
        for raw in raw_calls:
            if not isinstance(raw, dict) or raw.get("type") != "function":
                raise ModelProviderError("model provider returned an unsupported tool type")
            function = raw.get("function")
            if not isinstance(function, dict) or not isinstance(function.get("arguments"), str):
                raise ModelProviderError("model provider returned invalid tool arguments")
            try:
                arguments = json.loads(function["arguments"])
            except ValueError as error:
                raise ModelProviderError(
                    "model provider returned invalid tool arguments"
                ) from error
            calls.append(_call(raw.get("id"), function.get("name"), arguments))
        return _response(
            text or "",
            calls,
            choice.get("finish_reason"),
            config["model"],
            _usage(document, "prompt_tokens", "completion_tokens"),
            tool_reason="tool_calls",
            stop_reasons=("stop",),
        )

    def stream(
        self,
        *,
        system: str,
        messages: list[ModelMessage],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 2000,
        required_capabilities: tuple[str, ...] = (),
    ) -> Iterator[ModelStreamEvent]:
        config = self._prepare(messages, tools, max_tokens, required_capabilities)
        payload = self._body(system, messages, tools, max_tokens)
        payload["model"] = config["model"]
        payload["stream"] = True
        if self.provider != "deepseek":
            # DeepSeek always reports usage in the final chunk; the OpenAI-compatible
            # profile only does so when it is asked.
            payload["stream_options"] = {"include_usage": True}
        url = f"{config['base_url']}/chat/completions"
        headers = {
            "Authorization": f"Bearer {config['api_key']}",
            "content-type": "application/json",
        }
        return self._validated_stream(
            lambda: _stream_start(url, headers, payload),
            _StreamState(),
            config["model"],
            tool_reason="tool_calls",
            stop_reasons=("stop",),
        )


class DeepSeekProvider(OpenAIChatCompletionsCompatibleProvider):
    """Official DeepSeek non-thinking native function-call profile."""

    provider = "deepseek"
    default_base_url = "https://api.deepseek.com/v1"
    token_parameter = "max_tokens"

    def _config(self) -> dict[str, Any]:
        config = super()._config()
        if config["base_url"] != self.default_base_url:
            raise ModelNotConfigured("DeepSeek requires the official API endpoint")
        return config


def create_model_port(settings: RadianSettings) -> StreamingModelPort:
    try:
        settings = settings.snapshot()
        provider = settings.profile()[1].get("provider")
    except RadianSettingsError:
        settings = RadianSettings(settings.path, {"schema": "radian.settings.v1"})
        provider = None
    if provider == "deepseek":
        return DeepSeekProvider(settings)
    if provider == "openai-chat-completions-compatible":
        return OpenAIChatCompletionsCompatibleProvider(settings)
    # Absent/unsupported configuration fails at route/complete, never dispatches.
    return AnthropicProvider(settings)
