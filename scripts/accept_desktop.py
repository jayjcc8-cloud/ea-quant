"""Exercise installed runtime lifecycle with empty HOME and a tool-free PATH.

Usage: python scripts/accept_desktop.py --runtime APP/Contents/Resources/runtime --root ABS
Only synthetic data is created, under the explicitly chosen fresh acceptance root.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def line(process: subprocess.Popen[str], timeout: float = 40) -> dict[str, Any]:
    assert process.stdout is not None
    assert select.select([process.stdout], [], [], timeout)[0], "runtime response timeout"
    raw = process.stdout.readline()
    assert raw, f"runtime pipe closed: {process.poll()}"
    return json.loads(raw)  # type: ignore[no-any-return]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    runtime, root = args.runtime.resolve(), args.root.resolve()
    root.mkdir(mode=0o700)  # Fresh root required; don't touch earlier acceptance/user data.
    home = root / "empty-home"
    home.mkdir()
    data = root / "user-data"
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}
    processes: list[subprocess.Popen[str]] = []
    evidence: dict[str, Any] = {"runtime": str(runtime), "data": str(data), "checks": []}

    def start() -> subprocess.Popen[str]:
        with (root / "runtime.log").open("a") as log:
            child = subprocess.Popen(
                [
                    str(runtime / "python/bin/python3.12"),
                    "-I",
                    "-B",
                    "-m",
                    "ea.desktop",
                    "--runtime",
                    str(runtime),
                    "--data-root",
                    str(data),
                ],
                cwd=home,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
            )
        processes.append(child)
        return child

    def request(
        origin: str,
        path: str,
        body: dict[str, Any] | None = None,
        override_origin: str | None = None,
    ) -> Any:
        req = urllib.request.Request(
            origin + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Origin": override_origin or origin,
                "X-EA-Web-Request": "1",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.load(response)

    def stop(child: subprocess.Popen[str]) -> None:
        assert child.stdin is not None
        child.stdin.close()
        assert child.wait(timeout=25) == 0

    try:
        child = start()
        ready = line(child)
        assert ready["status"] == "ready", ready
        origin = ready["url"].removesuffix("/strategies")
        request(origin, "/api/health")
        duplicate = start()
        assert line(duplicate) == {"status": "error", "code": "desktop_already_running"}
        assert duplicate.wait(timeout=10) == 1
        request(origin, "/api/health")
        evidence["checks"].append("duplicate launch refused without disturbing original")
        try:
            request(origin, "/api/user-strategies", {}, "https://untrusted.invalid")
            raise AssertionError("remote origin admitted")
        except urllib.error.HTTPError as error:
            assert error.code == 403
        evidence["checks"].append("foreign origin write refused")
        created = request(
            origin,
            "/api/user-strategies",
            {
                "name": "桌面安装验收 · 合成样例",
                "intent": "运行合成样例验证安装包，无模型调用",
                "scenario_id": "synthetic-bounded-long.yaml",
                "criterion": {"field": "economics.net_pnl.amount", "expectation": "positive"},
            },
        )
        identity = created["user_strategy_id"]
        path = f"/api/user-strategies/{identity}"
        preview = request(origin, path + "/experiment/preview")
        assert preview["owner"] == "quine"
        request(
            origin,
            path + "/experiments",
            {
                "launch_intent_id": "desktop-acceptance-v1",
                "base_revision": preview["draft_revision"],
                "preview_identity": preview["preview_identity"],
                "approved_budget": {"max_model_calls": 0, "max_backtests": 1},
            },
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            row = request(origin, path)["experiments"][-1]
            if row["status"] in ("created", "unknown", "failed", "cancelled"):
                break
            time.sleep(0.15)
        assert row["status"] == "created", row
        job_id = row["job_id"]
        report = request(origin, f"/api/backtests/{job_id}/report")
        assert report["run_id"] == row["attempt_id"]
        evidence.update(strategy_id=identity, job_id=job_id, report=report)
        evidence["checks"].append("installed Python → Rust owner → real engine → official report")
        stop(child)
        try:
            os.kill(ready["core_pid"], 0)
            raise AssertionError("core remained live")
        except ProcessLookupError:
            pass
        evidence["checks"].append("stdin EOF shut down web and reaped owned core")
        child = start()
        ready = line(child)
        assert ready["status"] == "ready"
        origin = ready["url"].removesuffix("/strategies")
        assert request(origin, path)["experiments"][-1]["job_id"] == job_id
        assert request(origin, f"/api/backtests/{job_id}/report") == report
        evidence["checks"].append("reopen preserved strategy and byte-equivalent report document")
        os.kill(ready["core_pid"], signal.SIGTERM)
        assert child.wait(timeout=25) == 1
        evidence["checks"].append("unexpected owned core death terminates service with error")
        child = start()
        assert line(child)["status"] == "ready"
        stop(child)
        evidence["checks"].append("restart after core failure recovered")
        (root / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        print(json.dumps({"result": "PASS", "checks": evidence["checks"]}, ensure_ascii=False))
    finally:
        for process in processes:
            if process.poll() is None:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                try:
                    process.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
