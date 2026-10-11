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
from typing import Any


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
    os.chmod(root, 0o700)
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


async def session(root: Path, runtime: Path) -> None:
    import uvicorn

    from ea.web.app import WebSettings, create_app
    from ea.web.quine import QuineClient

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
                        application = create_app(
                            WebSettings(
                                scenario_root=root / "scenarios",
                                workspace=root / "workspace",
                                ui_dir=runtime / "ui",
                                port=port,
                                quine_socket=ipc,
                                quine_workspace=root / "core",
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
                        task = asyncio.create_task(server.serve(sockets=[listener]))
                        loop = asyncio.get_running_loop()
                        parent_gone = asyncio.Event()
                        loop.add_reader(sys.stdin.fileno(), parent_gone.set)
                        try:
                            while not server.started:
                                if task.done():
                                    await task
                                    raise RuntimeError("web_exited_before_ready")
                                if parent_gone.is_set():
                                    server.should_exit = True
                                    return
                                await asyncio.sleep(0.05)
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
                            server.should_exit = True
                            try:
                                await asyncio.wait_for(task, timeout=12)
                            except TimeoutError:
                                raise RuntimeError("web_shutdown_timeout") from None
                finally:
                    # Closing core's parent pipe triggers its existing owner-loss path.
                    # We do not synthesize cancellation or claim worker completion.
                    if core.stdin is not None:
                        core.stdin.close()
                    try:
                        core.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        core.kill()
                        core.wait(timeout=5)
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
