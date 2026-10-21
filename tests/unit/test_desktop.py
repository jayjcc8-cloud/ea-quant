"""Desktop data placement must remain stable and separate from the bundle."""

from pathlib import Path

import pytest

from ea.desktop import install_samples, prepare_root
from ea.desktop_settings import DesktopSettings


@pytest.mark.parametrize("parent_lost", [False, True])
def test_native_owner_pipe_distinguishes_requested_exit_from_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parent_lost: bool
) -> None:
    import asyncio
    import os
    from types import SimpleNamespace

    import uvicorn

    import ea.desktop as desktop
    import ea.web.app
    import ea.web.paper_desktop
    import ea.web.quine

    runtime = tmp_path / "bundle"
    (runtime / "samples").mkdir(parents=True)
    for name in ("synthetic-bounded-long.yaml", "prices.csv"):
        (runtime / "samples" / name).write_text("fixture")
    root = tmp_path / "data"
    methods: list[str] = []
    receipts: list[dict] = []

    class Core:
        pid = 123
        returncode = None

        def __init__(self, args: list[str], **kwargs: object) -> None:
            Path(args[args.index("--socket") + 1]).touch()
            self.stdin = SimpleNamespace(close=lambda: None)

        def poll(self) -> None:
            return None

        def wait(self, *, timeout: float) -> None:
            # Match the separately integrated core EOF contract, without
            # launching another service from this lifecycle boundary test.
            self.returncode = 0 if "quiesce" in methods else 70

    class Client:
        def __init__(self, socket: Path, **kwargs: object) -> None:
            pass

        def call(self, method: str, params: dict, **kwargs: object) -> dict:
            methods.append(method)
            return {"drained": True, "pending": 0} if method == "quiesce" else {}

    async def serve(server: SimpleNamespace, sockets: list, finished: asyncio.Event) -> None:
        server.started = True
        while not server.should_exit:
            await asyncio.sleep(0.01)

    monkeypatch.setattr(desktop.subprocess, "Popen", Core)
    monkeypatch.setattr(desktop, "emit", receipts.append)
    monkeypatch.setattr(desktop, "serve_web", serve)
    monkeypatch.setattr(ea.web.quine, "QuineClient", Client)
    monkeypatch.setattr(ea.web.app, "create_app", lambda settings: object())
    monkeypatch.setattr(
        ea.web.paper_desktop,
        "DesktopPaper",
        lambda *args, **kwargs: SimpleNamespace(
            open=lambda: None, close=lambda: None, shutdown=lambda: True, runs=root / "paper"
        ),
    )
    monkeypatch.setattr(
        uvicorn, "Server", lambda config: SimpleNamespace(started=False, should_exit=False)
    )
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "r") as owner:
        monkeypatch.setattr(desktop.sys, "stdin", owner)
        try:
            if not parent_lost:
                os.write(write_fd, b"shutdown\n")
            else:
                os.close(write_fd)
            asyncio.run(
                asyncio.wait_for(
                    desktop.workspace_session(root, runtime, None, asyncio.Event()), timeout=3
                )
            )
        finally:
            if not parent_lost:
                os.close(write_fd)
    assert ("quiesce" in methods) is not parent_lost
    assert receipts[-1] == {
        "status": "interrupted" if parent_lost else "closed",
        "core_exit": 70 if parent_lost else 0,
        "research_state": "read_from_owner_on_reopen",
    }


@pytest.mark.parametrize("first", ["lost_receipt", "pending"])
def test_research_exit_retries_only_the_idempotent_drain(first: str) -> None:
    import asyncio

    from ea.desktop import drain_research
    from ea.web.quine import QuineUnavailable

    class Client:
        calls: list[str] = []

        def call(self, method: str, params: dict, *, timeout: float) -> dict:
            self.calls.append(method)
            assert params == {}
            assert 0 < timeout <= 1
            if len(self.calls) == 1:
                if first == "lost_receipt":
                    raise QuineUnavailable("receipt lost")
                return {"drained": False, "pending": 1}
            return {"drained": True, "pending": 0}

    client = Client()
    assert asyncio.run(drain_research(client, timeout=1))
    assert client.calls == ["quiesce", "quiesce"]


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"drained": True, "pending": 1},
        {"drained": 1, "pending": 0},
        {"drained": True, "pending": False},
    ],
)
def test_research_exit_requires_an_explicit_complete_core_receipt(reply: dict) -> None:
    import asyncio

    from ea.desktop import drain_research

    class Client:
        def call(self, method: str, params: dict, *, timeout: float) -> dict:
            return reply

    assert not asyncio.run(drain_research(Client(), timeout=0.01))


def test_research_exit_unavailable_does_not_become_success() -> None:
    import asyncio

    from ea.desktop import drain_research
    from ea.web.quine import QuineUnavailable

    class Client:
        def call(self, method: str, params: dict, *, timeout: float) -> dict:
            raise QuineUnavailable("unreachable")

    assert not asyncio.run(drain_research(Client(), timeout=0.01))


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

    async def attempt(
        root: Path, runtime: Path, settings: DesktopSettings, startup_finished: asyncio.Event
    ) -> None:
        calls.append(root)
        if root == target:
            await original(root, runtime, settings, startup_finished)
        else:
            assert settings.root == anchor
            assert settings.startup_error is not None
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

    anchor, target, runtime = tmp_path / "a", tmp_path / "b", tmp_path / "bundle"
    prepare_root(anchor, runtime)
    target.mkdir()
    DesktopSettings(anchor, anchor, runtime).update({"next_workspace": str(target)})
    calls = []

    async def fail(
        root: Path, runtime: Path, settings: DesktopSettings, startup_finished: asyncio.Event
    ) -> None:
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

    async def fail(
        root: Path, runtime: Path, settings: DesktopSettings, startup_finished: asyncio.Event
    ) -> None:
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
