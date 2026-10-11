"""Small desktop-only settings adapter; no model calls or workspace migration."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ea.web.settings import RadianSettings, atomic_private_json


def private_file(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("设置文件不能是符号链接。")
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("设置文件权限不安全；原文件未改变，请选择新的私人工作区。")


def validate_workspace(path: Path, runtime: Path) -> None:
    if (
        not path.is_absolute()
        or path.resolve() != path
        or not path.is_dir()
        or path == runtime
        or runtime in path.parents
        or path in runtime.parents
    ):
        raise ValueError("请选择已有的绝对目录，不能使用符号链接或应用程序目录。")
    # Never turn an arbitrary nonempty directory into an application workspace.
    if any(path.iterdir()) and not all(
        (path / name).is_dir() for name in ("core", "workspace", "scenarios", "logs")
    ):
        raise ValueError("请选择空目录或已有的 QUINE 工作区；不会移动或删除文件。")
    for name in ("core", "workspace", "scenarios", "logs", "desktop.lock", "settings.json"):
        if (path / name).is_symlink():
            raise ValueError("工作区包含符号链接，未切换。")
    for lock in (path / "desktop.lock", path / "core/quine.db.lock"):
        if lock.is_symlink():
            raise ValueError("工作区锁不能是符号链接。")
        if lock.exists():
            with lock.open("rb") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError("工作区正在使用中；请先退出使用它的 QUINE。") from None
    if not os.access(path, os.W_OK | os.X_OK):
        raise ValueError("工作区不可写，请选择可写的私人目录。")


@dataclass
class DesktopSettings:
    anchor: Path
    root: Path
    runtime: Path
    startup_error: str | None = None
    mutex: Any = field(default_factory=threading.RLock, repr=False)

    @property
    def path(self) -> Path:
        return self.anchor / "desktop-preferences.json"

    def preferences(self) -> dict[str, Any]:
        private_file(self.path)
        if not self.path.exists():
            return {}
        if self.path.stat().st_size > 8192:
            raise ValueError("桌面偏好不可读。")
        value = json.loads(self.path.read_text())
        if not isinstance(value, dict) or value.get("theme", "system") not in (
            "system",
            "light",
            "dark",
        ):
            raise ValueError("桌面偏好格式错误。")
        return value

    def status(self) -> dict[str, Any]:
        with self.mutex:
            prefs = self.preferences()
            store = RadianSettings(self.root / "settings.json")
            model_error = None
            try:
                private_file(store.path)
                store.load()
                model_status = store.status()
            except (ValueError, OSError):
                model_error = "模型设置不可读或权限不安全；未读取凭据。可重试或选择新的工作区。"
                model_status = {
                    "schema": "radian.settings-status.v1",
                    "settings_file": str(store.path),
                    "configured": False,
                    "configuration_present": None,
                    "credential_available": None,
                    "request_validation": "not_verified",
                    "provider": None,
                    "model": None,
                    "base_url": None,
                    "configuration_error": model_error,
                }
            return {
                "workspace": str(self.root),
                "next_workspace": prefs.get("next_workspace", str(self.root)),
                "theme": prefs.get("theme", "system"),
                "startup_error": self.startup_error,
                "model": model_status,
                "model_error": model_error,
            }

    def update(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.mutex:
            self.status()  # Verify both stores are readable before any change.
            # One explicit save per operation; no cross-file partial transaction.
            if len(body) != 1 or not set(body) <= {"model", "theme", "next_workspace"}:
                raise ValueError("请选择一个设置保存。")
            if "model" in body:
                self.save_model(body["model"])
            else:
                prefs = self.preferences()
                if "theme" in body:
                    if not isinstance(body["theme"], str) or body["theme"] not in (
                        "system",
                        "light",
                        "dark",
                    ):
                        raise ValueError("主题无效。")
                    prefs["theme"] = body["theme"]
                else:
                    raw = body["next_workspace"]
                    if not isinstance(raw, str) or len(raw) > 4096:
                        raise ValueError("工作区路径无效。")
                    target = Path(raw)
                    if target != self.root:
                        validate_workspace(target, self.runtime)
                        if target in self.root.parents or self.root in target.parents:
                            raise ValueError("工作区不能相互嵌套。")
                    prefs["next_workspace"] = str(target)
                atomic_private_json(self.path, prefs)
            return self.status()

    def save_model(self, value: object) -> None:
        if not isinstance(value, dict) or not set(value) <= {
            "provider",
            "model",
            "base_url",
            "api_key",
            "clear_key",
        }:
            raise ValueError("模型设置格式无效。")
        for key in ("provider", "model", "base_url", "api_key"):
            if key in value and (not isinstance(value[key], str) or len(value[key]) > 4096):
                raise ValueError("模型设置必须是长度受限的文本。")
        if value.get("provider") not in (
            "deepseek",
            "anthropic",
            "openai-chat-completions-compatible",
        ):
            raise ValueError("请选择支持的提供方。")
        if not value.get("model", "").strip():
            raise ValueError("请填写模型 ID。")
        endpoint = value.get("base_url", "").strip()
        url = urlsplit(endpoint)
        if endpoint and (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or any(ord(char) < 33 for char in endpoint)
            or (url.scheme == "http" and url.hostname not in ("127.0.0.1", "localhost", "::1"))
        ):
            raise ValueError("端点必须是 HTTP(S) 地址，不能含凭据、查询参数或片段。")
        if endpoint:
            _ = url.port  # Reject malformed/out-of-range ports locally.
        if "clear_key" in value and not isinstance(value["clear_key"], bool):
            raise ValueError("清除凭据选项无效。")
        store = RadianSettings(self.root / "settings.json")
        private_file(store.path)
        document = store.load() or {"schema": "radian.settings.v1"}
        old = document.get("model") or {}
        key = value.get("api_key", "")
        changed_route = (
            old.get("provider") != value.get("provider") or (old.get("base_url") or "") != endpoint
        )
        # Never silently forward an existing credential to a different provider/URL.
        if changed_route and old.get("api_key") and not key and not value.get("clear_key"):
            raise ValueError("更改提供方或端点时，请重新输入凭据或明确清除旧凭据。")
        document["model"] = {
            "provider": value.get("provider"),
            "model": value["model"].strip(),
            "base_url": endpoint or None,
            "api_key": None if value.get("clear_key") else (key or old.get("api_key")),
        }
        store.save(document)
