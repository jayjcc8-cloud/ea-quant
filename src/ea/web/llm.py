"""Configurable model provider client for RADIAN research.

Keys live only in the backend-owned RadianSettings store (or the environment
variable its ``api_key_env`` names). This client is used exclusively by the
research orchestrator: it holds no account, funds, or order authority and
exposes no generic shell. A missing key is a typed configuration error, never
a fallback to another provider or an invented answer.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx2 as httpx

from ea.web.settings import RadianSettings, RadianSettingsError

_DEFAULT_BASE_URL = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"
_REQUEST_TIMEOUT_SECONDS = 60.0


class ModelNotConfigured(ValueError):
    """No usable model provider configuration exists."""


class ModelProviderError(ValueError):
    """A configured provider call failed; the message is safe to show."""


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """One bounded model completion with recorded usage."""

    text: str
    tool_calls: tuple[dict[str, Any], ...]
    stop_reason: str
    model: str
    input_tokens: int
    output_tokens: int


class AnthropicProvider:
    """Minimal Messages API client over the configured provider settings."""

    def __init__(self, settings: RadianSettings):
        self._settings = settings

    def _config(self) -> dict[str, Any]:
        try:
            document = self._settings.load()
        except RadianSettingsError as error:
            raise ModelNotConfigured("settings are unreadable") from error
        model = document.get("model") or {}
        if not isinstance(model, dict) or model.get("provider") != "anthropic":
            raise ModelNotConfigured("no model provider is configured")
        api_key = model.get("api_key")
        if not api_key:
            env_name = model.get("api_key_env")
            api_key = os.environ.get(env_name or "", "") if env_name else ""
        if not api_key or not model.get("model"):
            raise ModelNotConfigured("model provider is not configured")
        return {
            "api_key": api_key,
            "model": model["model"],
            "base_url": (model.get("base_url") or _DEFAULT_BASE_URL).rstrip("/"),
        }

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 2000,
    ) -> ModelResponse:
        """Run one Messages API completion and return text, tools and usage."""
        config = self._config()
        payload: dict[str, Any] = {
            "model": config["model"],
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
        try:
            response = httpx.post(
                f"{config['base_url']}/v1/messages",
                headers={
                    "x-api-key": config["api_key"],
                    "anthropic-version": _ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                json=payload,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as error:
            raise ModelProviderError(
                f"model provider is unreachable: {type(error).__name__}"
            ) from error
        if response.status_code != 200:
            raise ModelProviderError(
                f"model provider rejected the request ({response.status_code})"
            )
        try:
            document = response.json()
        except (json.JSONDecodeError, ValueError) as error:
            raise ModelProviderError("model provider returned an unreadable response") from error
        content = document.get("content")
        if not isinstance(content, list):
            raise ModelProviderError("model provider response is missing content")
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "input": block.get("input") if isinstance(block.get("input"), dict) else {},
                    }
                )
        usage = document.get("usage") or {}
        return ModelResponse(
            text="\n".join(text_parts),
            tool_calls=tuple(tool_calls),
            stop_reason=str(document.get("stop_reason") or ""),
            model=str(config["model"]),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )
