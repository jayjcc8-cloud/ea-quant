"""RADIAN local product settings: one small backend-owned store.

Only the backend process reads and writes this file. Keys never travel to the
UI, logs, or research artifacts; the UI receives a status projection that says
whether a model provider is configured, never the key itself.

The store defaults to absent. Absent means "not configured", which the product
surface must present honestly, never as a working provider.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

_ALLOWED_PROVIDERS = frozenset({"anthropic", "openai-chat-completions-compatible", "deepseek"})
_SCHEMA = "radian.settings.v1"
_MAX_BYTES = 64 * 1024
_DEFAULT_MAX_MODEL_CALLS = 10
_DEFAULT_MAX_BACKTESTS = 5
_MAX_RESEARCH_BOUND = 100


class RadianSettingsError(ValueError):
    """A settings document does not match the supported shape."""


@dataclass(frozen=True, slots=True)
class RadianSettings:
    """One settings file location, owned by the backend only."""

    path: Path

    @staticmethod
    def default_path() -> Path:
        override = os.environ.get("RADIAN_SETTINGS_FILE")
        if override:
            return Path(override).expanduser().absolute()
        return Path.home() / ".config" / "radian" / "settings.json"

    def load(self) -> dict[str, Any]:
        """Return the stored document, or the empty default when absent."""
        try:
            payload = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise RadianSettingsError("settings file is unreadable") from error
        if len(payload) > _MAX_BYTES:
            raise RadianSettingsError("settings file exceeds the supported size")
        try:
            document = cast(dict[str, Any], json.loads(payload.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RadianSettingsError("settings file is not valid JSON") from error
        _validate(document)
        return document

    def save(self, document: dict[str, Any]) -> None:
        """Atomically replace the stored document after validation."""
        _validate(document)
        atomic_private_json(self.path, document)

    def status(self) -> dict[str, Any]:
        """Project the model configuration without exposing any secret.

        The projection names the provider, the protocol that provider value
        actually selects, the configured endpoint (never a default one), the
        capabilities the protocol declares, the capabilities research requires
        and the frozen research budget. A provider that is not configured, or
        whose protocol does not declare a required capability, is reported
        unconfigured with a reason — never as usable.
        """
        try:
            document = self.load()
        except RadianSettingsError:
            document = {}
        raw_model = document.get("model")
        model: dict[str, Any] = raw_model if isinstance(raw_model, dict) else {}
        provider = model.get("provider")
        provider = provider if isinstance(provider, str) else None
        endpoint = model.get("base_url")
        endpoint = endpoint if isinstance(endpoint, str) and endpoint else None

        # Local import keeps this module independent of the adapter layer while
        # the protocol and capability facts stay owned by it.
        from ea.web.llm import model_projection

        projection = model_projection(provider, model.get("model"), endpoint)
        research = document.get("research") or {}
        configuration_present = bool(model)
        env_name = model.get("api_key_env")
        credential_available = bool(
            model.get("api_key")
            or (os.environ.get(env_name, "") if isinstance(env_name, str) and env_name else "")
        )
        usable = (
            provider in _ALLOWED_PROVIDERS and bool(model.get("model")) and credential_available
        )
        missing = projection["missing_capabilities"]
        configured = bool(usable and not missing)
        if configured:
            configuration_error = None
        elif not usable:
            configuration_error = "no usable model provider is configured"
        else:
            configuration_error = "the configured model protocol does not declare: " + ", ".join(
                missing
            )
        return {
            "schema": "radian.settings-status.v1",
            "settings_file": str(self.path),
            # Local readiness is not proof of a valid key or a real request.
            "configured": configured,
            "configuration_present": configuration_present,
            "credential_available": credential_available,
            "request_validation": "not_verified",
            "provider": provider,
            "model": model.get("model") if isinstance(model, dict) else None,
            "base_url": endpoint,
            "endpoint": endpoint,
            "protocol": projection["protocol"],
            "capabilities": projection["capabilities"],
            "required_capabilities": projection["required_capabilities"],
            "missing_capabilities": missing,
            "configuration_error": configuration_error,
            "api_key_env": model.get("api_key_env") if isinstance(model, dict) else None,
            "research_budget": {
                "max_model_calls": _budget(research, "max_model_calls", _DEFAULT_MAX_MODEL_CALLS),
                "max_backtests": _budget(research, "max_backtests", _DEFAULT_MAX_BACKTESTS),
            },
        }


def _validate(document: object) -> None:
    if document is None:
        return
    if not isinstance(document, dict) or document.get("schema") != _SCHEMA:
        raise RadianSettingsError("settings schema is not supported")
    model = document.get("model")
    if model is None:
        return
    if not isinstance(model, dict):
        raise RadianSettingsError("model settings must be one object")
    for key in ("provider", "model", "base_url", "api_key", "api_key_env"):
        value = model.get(key)
        if value is not None and not isinstance(value, str):
            raise RadianSettingsError(f"model setting {key} must be text")
    if "provider" in model and model["provider"] not in _ALLOWED_PROVIDERS:
        raise RadianSettingsError(
            f"model provider must be one of: {', '.join(sorted(_ALLOWED_PROVIDERS))}"
        )
    research = document.get("research")
    if research is None:
        return
    if not isinstance(research, dict):
        raise RadianSettingsError("research settings must be one object")
    for key in ("max_model_calls", "max_backtests"):
        value = research.get(key)
        if value is not None and (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 1 <= value <= _MAX_RESEARCH_BOUND
        ):
            raise RadianSettingsError(f"research budget {key} must be an integer from 1 to 100")


def _budget(document: object, key: str, default: int) -> int:
    value = document.get(key) if isinstance(document, dict) else None
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= _MAX_RESEARCH_BOUND:
        return value
    return default


def atomic_private_json(path: Path, document: dict[str, Any]) -> None:
    """Complete private write then atomic replacement; failed writes preserve the old file."""
    payload = (
        json.dumps(document, ensure_ascii=True, allow_nan=False, sort_keys=True).encode() + b"\n"
    )
    if len(payload) > _MAX_BYTES or path.is_symlink():
        raise RadianSettingsError("settings destination or size is invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
