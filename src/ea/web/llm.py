"""Neutral text/function-call port with two explicit, nonstreaming wire adapters.

Product model/endpoint/credentials are independent of developer tooling. Custom
endpoints must implement the selected protocol; no protocol/provider fallback.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
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


PROVIDER_PROTOCOLS: dict[str, tuple[str, ModelCapabilities]] = {
    "anthropic": ("anthropic-messages", ModelCapabilities(True, False, True)),
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


class _Provider:
    provider = ""
    default_base_url = ""

    def __init__(self, settings: RadianSettings):
        self._settings = settings

    def _config(self) -> dict[str, Any]:
        try:
            model = self._settings.load().get("model") or {}
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


class AnthropicProvider(_Provider):
    provider = "anthropic"
    default_base_url = "https://api.anthropic.com"

    def complete(
        self,
        *,
        system: str,
        messages: list[ModelMessage],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 2000,
        required_capabilities: tuple[str, ...] = (),
    ) -> ModelResponse:
        config = self._config()
        self._validate(messages, tools, max_tokens, required_capabilities)
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
            "model": config["model"],
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


class OpenAIChatCompletionsCompatibleProvider(_Provider):
    """Nonstreaming text/function subset; base_url is an API root including /v1.

    Uses max_completion_tokens. Endpoints/models must support this field, system
    text and function tools. Responses, legacy function_call, custom tools,
    reasoning history, multimedia, streaming and strict output are unsupported.
    """

    provider = "openai-chat-completions-compatible"
    default_base_url = "https://api.openai.com/v1"

    def complete(
        self,
        *,
        system: str,
        messages: list[ModelMessage],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 2000,
        required_capabilities: tuple[str, ...] = (),
    ) -> ModelResponse:
        config = self._config()
        self._validate(messages, tools, max_tokens, required_capabilities)
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
        payload: dict[str, Any] = {
            "model": config["model"],
            "messages": wire,
            "max_completion_tokens": max_tokens,
            "stream": False,
        }
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


def create_model_port(settings: RadianSettings) -> ModelPort:
    try:
        provider = (settings.load().get("model") or {}).get("provider")
    except RadianSettingsError:
        provider = None
    if provider == "openai-chat-completions-compatible":
        return OpenAIChatCompletionsCompatibleProvider(settings)
    # Absent/unsupported configuration fails at route/complete, never dispatches.
    return AnthropicProvider(settings)
