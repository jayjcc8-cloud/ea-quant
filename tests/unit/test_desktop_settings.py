import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from ea.desktop_settings import DesktopSettings, validate_workspace
from ea.web.settings import RadianSettings


def context(tmp_path: Path) -> DesktopSettings:
    root = tmp_path / "home"
    root.mkdir(mode=0o700)
    return DesktopSettings(root, root, tmp_path / "bundle")


def test_model_secret_is_write_only_and_save_does_not_authenticate(tmp_path: Path) -> None:
    desktop = context(tmp_path)
    result = desktop.update(
        {
            "model": {
                "provider": "deepseek",
                "model": "fake",
                "base_url": "http://127.0.0.1:9999/v1",
                "api_key": "fixture-only",
            }
        }
    )
    assert result["model"]["configured"] is True
    assert result["model"]["request_validation"] == "not_verified"
    assert "fixture-only" not in json.dumps(result)
    desktop.update(
        {
            "model": {
                "provider": "deepseek",
                "model": "fake-2",
                "base_url": "http://127.0.0.1:9999/v1",
                "api_key": "",
            }
        }
    )
    store = RadianSettings(desktop.root / "settings.json")
    assert store.load()["model"]["api_key"] == "fixture-only"
    assert store.path.stat().st_mode & 0o777 == 0o600
    desktop.update(
        {"model": {"provider": "deepseek", "model": "fake-2", "base_url": "", "clear_key": True}}
    )
    assert desktop.status()["model"]["configured"] is False


def test_atomic_failure_preserves_old_document(tmp_path: Path) -> None:
    store = RadianSettings(tmp_path / "settings.json")
    store.save({"schema": "radian.settings.v1"})
    old = store.path.read_bytes()
    with patch("os.replace", side_effect=OSError("fixture failure")), pytest.raises(OSError):
        store.save({"schema": "radian.settings.v1", "model": {"provider": "deepseek"}})
    assert store.path.read_bytes() == old
    assert list(tmp_path.iterdir()) == [store.path]


def test_workspace_is_deferred_and_preferences_survive_new_context(tmp_path: Path) -> None:
    desktop = context(tmp_path)
    target = tmp_path / "chosen"
    target.mkdir()
    desktop.update({"next_workspace": str(target)})
    assert list(target.iterdir()) == []
    assert desktop.status()["workspace"] == str(desktop.root)
    desktop.update({"theme": "dark"})
    reopened = DesktopSettings(desktop.anchor, desktop.root, desktop.runtime)
    assert reopened.status()["theme"] == "dark"
    assert reopened.status()["next_workspace"] == str(target)
    desktop.update({"next_workspace": str(desktop.root)})
    assert desktop.status()["next_workspace"] == str(desktop.root)


def test_workspace_and_endpoint_rejections_preserve_settings(tmp_path: Path) -> None:
    desktop = context(tmp_path)
    occupied = tmp_path / "unrelated"
    occupied.mkdir()
    (occupied / "precious").write_text("keep")
    with pytest.raises(ValueError):
        desktop.update({"next_workspace": str(occupied)})
    alias = tmp_path / "alias"
    alias.symlink_to(occupied)
    with pytest.raises(ValueError):
        validate_workspace(alias, desktop.runtime)
    for url in ["https://key@example.com", "https://example.com?key=fixture", "file:///tmp/x"]:
        with pytest.raises(ValueError):
            desktop.update({"model": {"provider": "deepseek", "model": "fake", "base_url": url}})
    assert not (desktop.root / "settings.json").exists()
    assert (occupied / "precious").read_text() == "keep"


def test_secret_symlink_and_permissive_file_are_not_overwritten(tmp_path: Path) -> None:
    desktop = context(tmp_path)
    target = tmp_path / "target"
    target.write_text("keep")
    path = desktop.root / "settings.json"
    path.symlink_to(target)
    with pytest.raises(ValueError):
        desktop.update({"model": {"provider": "deepseek", "model": "fake"}})
    assert target.read_text() == "keep"
    assert desktop.status()["model_error"]
    desktop.update({"theme": "dark"})  # Recovery controls remain usable.
    path.unlink()
    path.write_text('{"schema":"radian.settings.v1"}')
    os.chmod(path, 0o644)
    with pytest.raises(ValueError):
        desktop.update({"model": {"provider": "deepseek", "model": "fake"}})
    assert path.stat().st_mode & 0o777 == 0o644


def test_busy_workspace_and_changed_route_are_rejected(tmp_path: Path) -> None:
    import fcntl

    desktop = context(tmp_path)
    target = tmp_path / "other"
    for name in ("core", "workspace", "scenarios", "logs"):
        (target / name).mkdir(parents=True)
    with (target / "desktop.lock").open("wb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="正在使用"):
            desktop.update({"next_workspace": str(target)})
    desktop.update({"model": {"provider": "deepseek", "model": "fake", "api_key": "only-fake"}})
    before = (desktop.root / "settings.json").read_bytes()
    with pytest.raises(ValueError):
        desktop.update(
            {
                "model": {
                    "provider": "deepseek",
                    "model": "fake",
                    "base_url": "https://example.invalid",
                }
            }
        )
    assert (desktop.root / "settings.json").read_bytes() == before


def test_desktop_http_boundary_and_secret_redaction(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from ea.web.app import create_app
    from unit.test_web_radian import _settings

    desktop = context(tmp_path)
    settings = _settings(tmp_path, desktop_settings=desktop)
    headers = {"Origin": "http://127.0.0.1:8765", "X-EA-Web-Request": "1"}
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8765") as client:
        body = {"model": {"provider": "deepseek", "model": "fake", "api_key": "never-echo-fixture"}}
        assert client.post("/api/desktop/settings", json=body).status_code == 403
        assert (
            client.post(
                "/api/desktop/settings",
                json=body,
                headers={**headers, "Origin": "https://evil.invalid"},
            ).status_code
            == 403
        )
        saved = client.post("/api/desktop/settings", json=body, headers=headers)
        assert saved.status_code == 200
        assert "never-echo-fixture" not in saved.text
        assert "never-echo-fixture" not in client.get("/api/desktop/settings").text
        bad = client.post(
            "/api/desktop/settings",
            content="never-echo-fixture",
            headers={**headers, "Content-Type": "application/json"},
        )
        assert bad.status_code == 409 and "never-echo-fixture" not in bad.text
        assert (
            client.post("/api/desktop/settings", json={"theme": []}, headers=headers).status_code
            == 409
        )


def test_application_identity_is_recorded_or_explicitly_unknown(tmp_path: Path) -> None:
    from ea import __version__

    desktop = context(tmp_path)
    identity = desktop.status()["application"]
    assert identity["engine_version"] == __version__
    assert identity["build_id"] is None
    assert identity["source_revision"] is None
    desktop.runtime.mkdir()
    info = desktop.runtime / "build-info.json"
    info.write_text(
        json.dumps(
            {
                "desktop_version": "0.1.0",
                "build_id": "local-test",
                "source_revision": "abc123",
                "source_dirty": True,
                "api_key": "must-not-copy",
            }
        )
    )
    identity = desktop.status()["application"]
    assert identity["build_id"] == "local-test"
    assert identity["source_dirty"] is True
    assert "must-not-copy" not in json.dumps(identity)
    info.write_text("broken")
    assert desktop.status()["application"]["build_id"] is None
