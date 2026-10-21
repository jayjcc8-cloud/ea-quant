"""RADIAN local product settings: one small backend-owned store.

Only the backend process reads and writes this file. Keys never travel to the
UI, logs, or research artifacts; the UI receives a status projection that says
whether a model provider is configured, never the key itself.

The store defaults to absent. Absent means "not configured", which the product
surface must present honestly, never as a working provider.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
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
    _snapshot: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    @staticmethod
    def default_path() -> Path:
        override = os.environ.get("RADIAN_SETTINGS_FILE")
        if override:
            return Path(override).expanduser().absolute()
        return Path.home() / ".config" / "radian" / "settings.json"

    def load(self) -> dict[str, Any]:
        """Return the stored document, or the empty default when absent."""
        if self._snapshot is not None:
            return copy.deepcopy(self._snapshot)
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

    def profile(self, profile_id: str | None = None) -> tuple[str, dict[str, Any]]:
        document = self.load()
        identity = profile_id or document.get("default_profile_id") or "legacy"
        model = (
            (document.get("model") or {})
            if identity == "legacy"
            else (document.get("model_profiles", {}).get(identity))
        )
        if not isinstance(model, dict):
            raise RadianSettingsError("the selected model connection does not exist")
        return identity, copy.deepcopy(model)

    def profile_ref(self, profile_id: str | None = None) -> dict[str, Any]:
        identity, model = self.profile(profile_id)
        # No key or key hash enters the research record. Explicit saves increment
        # revision; the route digest also detects manual non-secret route edits.
        safe = {key: model.get(key) for key in ("provider", "model", "base_url", "revision")}
        version = hashlib.sha256(json.dumps(safe, sort_keys=True).encode()).hexdigest()
        return {
            "id": identity,
            "name": model.get("name") or "原有连接",
            "revision": model.get("revision", 1),
            "route_version": version,
        }

    def snapshot(self, profile_id: str | None = None) -> RadianSettings:
        # One read fixes both protocol selection and the adapter's request. The
        # credential stays only in this private in-memory object, never history.
        document = self.load()
        source = RadianSettings(self.path, document)
        _, model = source.profile(profile_id)
        if not model.get("api_key") and model.get("api_key_env"):
            model["api_key"] = os.environ.get(model["api_key_env"], "")
        return RadianSettings(
            self.path,
            {"schema": _SCHEMA, "model": model, "research": document.get("research") or {}},
        )

    def status(self, profile_id: str | None = None, *, _catalog: bool = True) -> dict[str, Any]:
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
        source = RadianSettings(self.path, document)
        try:
            identity, model = source.profile(profile_id)
        except RadianSettingsError:
            identity, model = profile_id or "legacy", {}
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
            "profile_id": identity,
            "default_profile_id": document.get("default_profile_id") or "legacy",
            **(
                {
                    "profiles": [
                        {**source.status(item, _catalog=False), **source.profile_ref(item)}
                        for item in (["legacy"] if document.get("model") else [])
                        + list(document.get("model_profiles") or {})
                    ]
                }
                if _catalog
                else {}
            ),
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
    profiles = document.get("model_profiles", {})
    if not isinstance(profiles, dict) or len(profiles) > 32:
        raise RadianSettingsError("model profiles must be a bounded object")
    for identity, entry in profiles.items():
        if (
            not isinstance(identity, str)
            or identity == "legacy"
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identity)
        ):
            raise RadianSettingsError("model profile identity is invalid")
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("name"), str)
            or not 1 <= len(entry["name"]) <= 100
        ):
            raise RadianSettingsError("model profile name is invalid")
        if type(entry.get("revision")) is not int or entry["revision"] < 1:
            raise RadianSettingsError("model profile revision is invalid")
        _validate({"schema": _SCHEMA, "model": entry})
    selected = document.get("default_profile_id")
    if selected is not None and (
        not isinstance(selected, str) or (selected != "legacy" and selected not in profiles)
    ):
        raise RadianSettingsError("default model profile does not exist")
    model = document.get("model") or {}
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
