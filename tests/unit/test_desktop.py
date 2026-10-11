"""Desktop data placement must remain stable and separate from the bundle."""

from pathlib import Path

import pytest

from ea.desktop import install_samples, prepare_root


def test_desktop_upgrade_preserves_user_scenes_and_settings(tmp_path: Path) -> None:
    runtime = tmp_path / "bundle"
    samples = runtime / "samples"
    samples.mkdir(parents=True)
    (samples / "synthetic-bounded-long.yaml").write_text("original")
    (samples / "prices.csv").write_text("synthetic")
    root = tmp_path / "data"
    prepare_root(root, runtime)
    install_samples(root, runtime)
    scene = root / "scenarios/synthetic-bounded-long.yaml"
    scene.write_text("user revised")
    settings = root / "settings.json"
    settings.write_text('{"schema":"radian.settings.v1"}')
    (samples / "synthetic-bounded-long.yaml").write_text("new release")
    prepare_root(root, runtime)
    install_samples(root, runtime)
    assert scene.read_text() == "user revised"
    assert settings.read_text() == '{"schema":"radian.settings.v1"}'
    assert root.stat().st_mode & 0o777 == 0o700


def test_desktop_refuses_bundle_and_symlink_data_paths(tmp_path: Path) -> None:
    runtime = tmp_path / "bundle"
    runtime.mkdir()
    with pytest.raises(ValueError, match="separate"):
        prepare_root(runtime / "data", runtime)
    root = tmp_path / "data"
    root.symlink_to(runtime, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        prepare_root(root, runtime)
    root2 = tmp_path / "data2"
    root2.mkdir()
    (root2 / "workspace").symlink_to(runtime, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        prepare_root(root2, runtime)


def test_selected_workspace_log_failure_recovers_without_changing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    import ea.desktop as desktop
    from ea.desktop_settings import DesktopSettings

    runtime = tmp_path / "bundle"
    (runtime / "samples").mkdir(parents=True)
    for name in ("synthetic-bounded-long.yaml", "prices.csv"):
        (runtime / "samples" / name).write_text("fixture")
    anchor, target = tmp_path / "a", tmp_path / "b"
    for root in (anchor, target):
        prepare_root(root, runtime)
        install_samples(root, runtime)
    log = target / "logs/core.log"
    log.write_text("preserved target diagnostics")
    log.chmod(0o400)
    sentinel = anchor / "workspace/preserved"
    sentinel.write_text("original research")
    store = DesktopSettings(anchor, anchor, runtime)
    store.update({"next_workspace": str(target)})
    preference_before = store.path.read_bytes()
    original = desktop.workspace_session
    calls = []

    async def attempt(root, runtime, settings, startup_finished):
        calls.append(root)
        if root == target:
            await original(root, runtime, settings, startup_finished)
        else:
            assert settings.root == anchor
            assert "启动失败" in settings.startup_error
            assert settings.status()["next_workspace"] == str(target)
            settings.update({"next_workspace": str(anchor)})  # The existing UI cancel is usable.
            startup_finished.set()

    monkeypatch.setattr(desktop, "workspace_session", attempt)
    try:
        asyncio.run(desktop.session(anchor, runtime))
        assert calls == [target, anchor]
        assert sentinel.read_text() == "original research"
        assert log.read_text() == "preserved target diagnostics"
        assert log.stat().st_mode & 0o777 == 0o400
        assert store.path.read_bytes() != preference_before  # Only the explicit cancellation.
        assert store.status()["next_workspace"] == str(anchor)
    finally:
        log.chmod(0o600)  # Test fixture only, for pytest cleanup.


@pytest.mark.parametrize("ready,closing", [(True, False), (False, True)])
def test_started_or_closing_workspace_failure_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ready: bool, closing: bool
) -> None:
    import asyncio

    import ea.desktop as desktop
    from ea.desktop_settings import DesktopSettings

    anchor, target, runtime = tmp_path / "a", tmp_path / "b", tmp_path / "bundle"
    prepare_root(anchor, runtime)
    target.mkdir()
    DesktopSettings(anchor, anchor, runtime).update({"next_workspace": str(target)})
    calls = []

    async def fail(root, runtime, settings, startup_finished):
        calls.append(root)
        if ready or closing:
            startup_finished.set()
        raise RuntimeError("active_or_closing_session_failure")

    monkeypatch.setattr(desktop, "workspace_session", fail)
    with pytest.raises(RuntimeError, match="active_or_closing"):
        asyncio.run(desktop.session(anchor, runtime))
    assert calls == [target]


def test_default_workspace_failure_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    import ea.desktop as desktop

    calls = []

    async def fail(root, runtime, settings, startup_finished):
        calls.append(root)
        raise PermissionError("default_unavailable")

    monkeypatch.setattr(desktop, "workspace_session", fail)
    with pytest.raises(PermissionError, match="default_unavailable"):
        asyncio.run(desktop.session(tmp_path / "a", tmp_path / "bundle"))
    assert calls == [tmp_path / "a"]


def test_web_owner_conflict_is_an_embedded_startup_failure(
    tmp_path: Path,
) -> None:
    import asyncio
    import fcntl

    import uvicorn

    from ea.desktop import serve_web
    from ea.web.app import create_app
    from unit.test_web_radian import _settings

    settings = _settings(tmp_path)
    settings.workspace.mkdir()
    lock = settings.workspace / "service.lock"
    lock.touch(mode=0o600)
    server = uvicorn.Server(uvicorn.Config(create_app(settings), lifespan="on"))

    async def run() -> None:
        finished = asyncio.Event()
        # Crucial: the exception must be translated INSIDE the detached task,
        # before SystemExit can interrupt asyncio.run and bypass its caller.
        task = asyncio.create_task(serve_web(server, [], finished))
        with pytest.raises(RuntimeError, match="web_startup_failed"):
            await task
        assert not finished.is_set()
        assert not server.started

    with lock.open("rb") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(run())


def test_selected_and_default_log_failures_stop_after_one_recovery(
    tmp_path: Path,
) -> None:
    import asyncio

    import ea.desktop as desktop
    from ea.desktop_settings import DesktopSettings

    runtime = tmp_path / "bundle"
    (runtime / "samples").mkdir(parents=True)
    for name in ("synthetic-bounded-long.yaml", "prices.csv"):
        (runtime / "samples" / name).write_text("fixture")
    anchor, target = tmp_path / "a", tmp_path / "b"
    for root in (anchor, target):
        prepare_root(root, runtime)
        install_samples(root, runtime)
        (root / "logs/core.log").write_text("unchanged")
        (root / "logs/core.log").chmod(0o400)
    store = DesktopSettings(anchor, anchor, runtime)
    store.update({"next_workspace": str(target)})
    before = store.path.read_bytes()
    try:
        with pytest.raises(PermissionError):
            asyncio.run(desktop.session(anchor, runtime))
        assert store.path.read_bytes() == before
        for root in (anchor, target):
            assert (root / "logs/core.log").read_text() == "unchanged"
            assert (root / "logs/core.log").stat().st_mode & 0o777 == 0o400
    finally:
        for root in (anchor, target):
            (root / "logs/core.log").chmod(0o600)
