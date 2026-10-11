"""Bundled Mac lifecycle adapter. No experiment or report authority lives here.

The desktop owns stdin. EOF (including a desktop crash) closes this session.
Only a socket bound by this process is served; readiness is sent over stdout,
never discovered by connecting to a guessed port or another user's service.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from uvicorn import Server


def prepare_root(root: Path, runtime: Path) -> None:
    if not root.is_absolute() or root.is_symlink():
        raise ValueError("data_root_must_be_absolute_and_not_a_symlink")
    if (
        root.resolve() != root
        or root == runtime
        or runtime in root.parents
        or root in runtime.parents
    ):
        raise ValueError("data_root_must_be_separate_from_bundle")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("core", "workspace", "scenarios", "logs"):
        path = root / name
        if path.is_symlink():
            raise ValueError("data_directory_is_symlink")
        path.mkdir(mode=0o700, exist_ok=True)


def install_samples(root: Path, runtime: Path) -> None:
    # Only first use seeds examples. Never overwrite user scenes on an upgrade.
    marker = root / "samples-v1.installed"
    if marker.exists():
        return
    for name in ("synthetic-bounded-long.yaml", "prices.csv"):
        target = root / "scenarios" / name
        try:
            with target.open("xb") as output:
                output.write((runtime / "samples" / name).read_bytes())
        except FileExistsError:
            pass
    marker.touch(exist_ok=False)


def emit(document: dict[str, Any]) -> None:
    try:
        print(json.dumps(document), flush=True)
    except BrokenPipeError:
        # A crashed desktop cannot receive diagnostics; still reap our core.
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())


async def serve_web(
    server: Server, sockets: list[socket.socket], startup_finished: asyncio.Event
) -> None:
    """Embed the server's CLI exit contract inside the task, before asyncio sees it.

    Ordinary exceptions are session failures and normal return is shutdown. A
    nonzero process exit during server startup means startup failure, also an
    ordinary exception here. Normal exits, cancellation, interrupts and signals
    retain their control-flow semantics; never catch BaseException wholesale.
    """
    try:
        await server.serve(sockets=sockets)
    except SystemExit as error:
        if server.started or server.should_exit or error.code in (None, 0):
            startup_finished.set()
            raise
        raise RuntimeError("web_startup_failed") from None
    finally:
        # Close the readiness race even if the server exits before our poll runs.
        if server.started or server.should_exit:
            startup_finished.set()


async def session(root: Path, runtime: Path) -> None:
    from ea.desktop_settings import DesktopSettings, validate_workspace

    anchor = root
    prepare_root(anchor, runtime)
    desktop = DesktopSettings(anchor, anchor, runtime)
    # Serialize selection for one app profile as well as locking the selected workspace.
    with (anchor / "selection.lock").open("a+b") as selection_lock:
        try:
            fcntl.flock(selection_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("desktop_already_running") from None
        requested = desktop.preferences().get("next_workspace", str(anchor))
        if requested != str(anchor):
            try:
                chosen = Path(requested)
                validate_workspace(chosen, runtime)
                if chosen in anchor.parents or anchor in chosen.parents:
                    raise ValueError("工作区不能相互嵌套。")
                desktop.root = chosen
            except (ValueError, OSError):
                desktop.startup_error = (
                    "所选工作区不可用或被占用；已保留并打开默认工作区。请在设置中重新选择。"
                )
        startup_finished = asyncio.Event()
        try:
            await workspace_session(desktop.root, runtime, desktop, startup_finished)
        except Exception as error:
            # Only a selected workspace that never became usable may fall back.
            # workspace_session has already unwound its locks/children here. Once
            # ready, closing, or cleanup-uncertain, preserve the existing fatal path.
            if desktop.root == anchor or startup_finished.is_set():
                raise
            emit({"status": "workspace_startup_failed", "code": type(error).__name__})
            desktop.root = anchor
            desktop.startup_error = (
                "所选工作区启动失败；已打开默认工作区，原数据和目录权限保持不变。"
                "请在设置中取消待切换，或选择其他可用工作区。"
            )
            # One recovery attempt only; never retry or replay work in the failed target.
            await workspace_session(anchor, runtime, desktop, asyncio.Event())


async def workspace_session(
    root: Path, runtime: Path, desktop: Any, startup_finished: asyncio.Event
) -> None:
    import uvicorn

    from ea.web.app import WebSettings, create_app
    from ea.web.paper_desktop import DesktopPaper
    from ea.web.quine import QuineBridge, QuineClient

    os.umask(0o077)
    prepare_root(root, runtime)
    # Keep this file open for the entire session, including shutdown.
    with (root / "desktop.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("desktop_already_running") from None
        install_samples(root, runtime)
        os.environ["RADIAN_SETTINGS_FILE"] = str(root / "settings.json")
        # Use a short private IPC directory: macOS Unix socket paths max out at 104 bytes.
        with tempfile.TemporaryDirectory(prefix="quine-", dir="/tmp") as ipc_dir:
            ipc = Path(ipc_dir) / "core.sock"
            with (root / "logs" / "core.log").open("ab") as core_log:
                core = subprocess.Popen(
                    [
                        str(runtime / "quine-core"),
                        "serve",
                        "--workspace",
                        str(root / "core"),
                        "--socket",
                        str(ipc),
                        "--python",
                        sys.executable,
                        "--parent-stdin",
                    ],
                    cwd=root,
                    stdin=subprocess.PIPE,
                    stdout=core_log,
                    stderr=core_log,
                    env={
                        key: os.environ[key]
                        for key in ("HOME", "PATH", "LANG", "TMPDIR")
                        if key in os.environ
                    },
                )
                try:
                    for _ in range(200):
                        if core.poll() is not None:
                            raise RuntimeError("core_exited_before_ready")
                        if ipc.exists():
                            break
                        await asyncio.sleep(0.05)
                    else:
                        raise RuntimeError("core_readiness_timeout")
                    # The existing owner protocol confirms the socket is usable.
                    QuineClient(ipc).call("list", {})
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                        listener.bind(("127.0.0.1", 0))
                        listener.listen(128)
                        listener.setblocking(False)
                        port = listener.getsockname()[1]
                        paper = DesktopPaper(
                            root / "paper",
                            root / "workspace",
                            quine=QuineBridge(ipc, workspace=root / "core"),
                        )
                        paper.open()
                        application = create_app(
                            WebSettings(
                                scenario_root=root / "scenarios",
                                workspace=root / "workspace",
                                ui_dir=runtime / "ui",
                                port=port,
                                quine_socket=ipc,
                                quine_workspace=root / "core",
                                desktop_settings=desktop,
                                runtime_root=paper.runs,
                                desktop_paper=paper,
                            )
                        )
                        server = uvicorn.Server(
                            uvicorn.Config(
                                application,
                                host="127.0.0.1",
                                port=port,
                                proxy_headers=False,
                                server_header=False,
                                access_log=False,
                                timeout_graceful_shutdown=5,
                            )
                        )
                        loop = asyncio.get_running_loop()
                        parent_gone = asyncio.Event()

                        stopping_paper = False

                        async def request_exit(parent_lost: bool) -> None:
                            nonlocal stopping_paper
                            if stopping_paper:
                                return
                            stopping_paper = True
                            try:
                                confirmed = await asyncio.to_thread(paper.shutdown)
                                if confirmed:
                                    parent_gone.set()
                                    server.should_exit = True
                                else:
                                    emit(
                                        {
                                            "status": "close_blocked",
                                            "code": "paper_stop_unconfirmed",
                                        }
                                    )
                                    if parent_lost:
                                        # Keep ownership while an inherited pipe/lease could
                                        # still belong to the original attempt. No new app can
                                        # silently acquire this workspace and run it again.
                                        emit(
                                            {
                                                "status": "unknown",
                                                "code": "parent_lost_stop_unconfirmed",
                                            }
                                        )
                            finally:
                                stopping_paper = False

                        def parent_message() -> None:
                            startup_finished.set()
                            message = os.read(sys.stdin.fileno(), 4096)
                            if not message:
                                loop.remove_reader(sys.stdin.fileno())
                            asyncio.create_task(request_exit(not message))

                        loop.add_reader(sys.stdin.fileno(), parent_message)
                        task = asyncio.create_task(serve_web(server, [listener], startup_finished))
                        try:
                            while not server.started:
                                if task.done():
                                    await task
                                    startup_finished.set()
                                    return  # A normal server return is shutdown, not a failure.
                                if parent_gone.is_set():
                                    startup_finished.set()
                                    server.should_exit = True
                                    return
                                await asyncio.sleep(0.05)
                            startup_finished.set()
                            emit(
                                {
                                    "status": "ready",
                                    "url": f"http://127.0.0.1:{port}/strategies",
                                    "root": str(root),
                                    "core_pid": core.pid,
                                    "pid": os.getpid(),
                                }
                            )
                            while not task.done() and not parent_gone.is_set():
                                if core.poll() is not None:
                                    raise RuntimeError("core_exited")
                                await asyncio.sleep(0.1)
                        finally:
                            loop.remove_reader(sys.stdin.fileno())
                            confirmed = await asyncio.to_thread(paper.shutdown)
                            if not confirmed:
                                startup_finished.set()
                                raise RuntimeError("paper_stop_unconfirmed")
                            paper.close()
                            server.should_exit = True
                            try:
                                await asyncio.wait_for(task, timeout=12)
                            except TimeoutError:
                                startup_finished.set()  # Do not recover from uncertain cleanup.
                                raise RuntimeError("web_shutdown_timeout") from None
                finally:
                    # Closing core's parent pipe triggers its existing owner-loss path.
                    # We do not synthesize cancellation or claim worker completion.
                    try:
                        if core.stdin is not None:
                            core.stdin.close()
                        try:
                            core.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            core.kill()
                            core.wait(timeout=5)
                    except BaseException:
                        startup_finished.set()  # Failed reap must not start another session.
                        raise
                    emit(
                        {
                            "status": "closed",
                            "core_exit": core.returncode,
                            "research_state": "read_from_owner_on_reopen",
                        }
                    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(session(args.data_root, args.runtime.resolve(strict=True)))
    except Exception as error:
        # No config, credentials, request bodies, or exception detail on the pipe.
        emit(
            {
                "status": "error",
                "code": str(error)
                if isinstance(error, (ValueError, RuntimeError))
                else type(error).__name__,
            }
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
