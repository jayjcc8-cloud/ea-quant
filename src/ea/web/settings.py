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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ALLOWED_PROVIDERS = frozenset({"anthropic"})
_SCHEMA = "radian.settings.v1"
_MAX_BYTES = 64 * 1024


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
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RadianSettingsError("settings file is not valid JSON") from error
        _validate(document)
        return document

    def save(self, document: dict[str, Any]) -> None:
        """Atomically replace the stored document after validation."""
        _validate(document)
        payload = (
            json.dumps(
                document,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor: int | None = None
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise RadianSettingsError("settings write made no complete progress")
            os.fsync(descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def status(self) -> dict[str, Any]:
        """Project the model configuration without exposing any secret."""
        try:
            model = self.load().get("model") or {}
        except RadianSettingsError:
            model = {}
        configured = (
            isinstance(model, dict)
            and model.get("provider") in _ALLOWED_PROVIDERS
            and bool(model.get("model"))
            and (bool(model.get("api_key")) or bool(model.get("api_key_env")))
        )
        return {
            "schema": "radian.settings-status.v1",
            "settings_file": str(self.path),
            "configured": configured,
            "provider": model.get("provider") if isinstance(model, dict) else None,
            "model": model.get("model") if isinstance(model, dict) else None,
            "base_url": model.get("base_url") if isinstance(model, dict) else None,
            "api_key_env": model.get("api_key_env") if isinstance(model, dict) else None,
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
