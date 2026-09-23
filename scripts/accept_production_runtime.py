#!/usr/bin/env python3
"""Isolated installed PPV-02 acceptance; never use a production workspace or unit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, cast


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def wait_for(predicate: Any, timeout: float = 40) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError("acceptance condition timed out")


def install(bundle: Path, root: Path) -> tuple[Path, dict[str, Any]]:
    with zipfile.ZipFile(bundle) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        release = root / "releases" / manifest["commit"]
        assert not release.exists(), "distinct pinned commits required"
        for name in archive.namelist():
            assert not Path(name).is_absolute() and ".." not in Path(name).parts
        archive.extractall(release)
    run("uv", "venv", "--python", "3.12", str(release / ".venv"))
    python = str(release / ".venv/bin/python")
    run(
        "uv",
        "pip",
        "install",
        "--python",
        python,
        "--require-hashes",
        "-r",
        str(release / "requirements.txt"),
    )
    wheel = next(release.glob("*.whl"))
    run("uv", "pip", "install", "--python", python, "--no-deps", str(wheel))
    return release, manifest


def package_bytes(release: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(next(release.glob("*.whl"))) as wheel:
        return {name: wheel.read(name) for name in wheel.namelist() if name.startswith("ea/")}


def snapshot(workspace: Path) -> dict[str, str]:
    return {
        str(p.relative_to(workspace)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in workspace.rglob("*")
        if p.is_file() and p.name != "service.lock"
    }


def accept(old_bundle: Path, new_bundle: Path, root: Path, systemd: bool) -> None:
    assert root.is_absolute() and not root.exists(), "acceptance root must be new and absolute"
    root.mkdir(parents=True)
    old, old_manifest = install(old_bundle, root)
    new, new_manifest = install(new_bundle, root)
    # This specific rollback is safe because PPV-02 does not change any installed EA state owner.
    assert package_bytes(old) == package_bytes(new), "rollback requires unchanged EA state code"
    shutil.copyfile(new / "runtime.py", root / "runtime.py")
    shutil.copytree(new / "scenarios", root / "inputs/scenarios")
    (root / "workspace").mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"

    def request(path: str, body: object = None) -> bytes:
        headers = {"Origin": origin, "Content-Type": "application/json", "X-EA-Web-Request": "1"}
        data = None if body is None else json.dumps(body).encode()
        with urllib.request.urlopen(
            urllib.request.Request(origin + path, data=data, headers=headers), timeout=2
        ) as reply:
            return cast(bytes, reply.read())

    def available() -> bool:
        try:
            return bool(request("/api/health"))
        except (OSError, urllib.error.URLError):
            return False

    config = root / "runtime.toml"

    def select(release: Path, manifest: dict[str, Any]) -> None:
        document = {
            "schema": "ea.production-runtime.v1",
            "bundle_root": str(release),
            "commit": manifest["commit"],
            "manifest_sha256": hashlib.sha256((release / "manifest.json").read_bytes()).hexdigest(),
            "scenario_root": str(root / "inputs/scenarios"),
            "workspace": str(root / "workspace"),
            "port": port,
        }
        pending = root / "runtime.next.toml"
        pending.write_text("".join(f"{k} = {json.dumps(v)}\n" for k, v in document.items()))
        identity = json.loads(
            run(
                str(release / ".venv/bin/python"),
                "-I",
                str(root / "runtime.py"),
                "check",
                "--config",
                str(pending),
            )
        )
        assert identity["commit"] == manifest["commit"] and identity["live_available"] is False
        os.replace(pending, config)
        pending_link = root / "current.next"
        pending_link.symlink_to(release, target_is_directory=True)
        os.replace(pending_link, root / "current")

    select(old, old_manifest)
    unit = f"ea-ppv02-accept-{os.getpid()}.service"
    unit_path = Path("/etc/systemd/system") / unit
    process: subprocess.Popen[bytes] | None = None
    log = (root / "service.log").open("ab")
    unit_installed = False

    def ctl(*args: str) -> str:
        return run("sudo", "-n", "systemctl", *args, unit)

    def start() -> None:
        nonlocal process
        if systemd:
            ctl("reset-failed")
            ctl("start")
        else:
            process = subprocess.Popen(
                [
                    str(root / "current/.venv/bin/python"),
                    "-I",
                    str(root / "runtime.py"),
                    "serve",
                    "--config",
                    str(config),
                ],
                cwd=root,
                stdout=log,
                stderr=log,
            )
        wait_for(available)

    def stop() -> None:
        if systemd:
            ctl("stop")
            assert ctl("show", "--property=MainPID", "--value") == "0"
        elif process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=65)
        wait_for(lambda: not available())

    def pid() -> int:
        if systemd:
            return int(ctl("show", "--property=MainPID", "--value"))
        assert process is not None
        return process.pid

    def report_equal(job: str, expected: bytes) -> None:
        assert request(f"/api/backtests/{job}/report") == expected
        assert request(f"/api/backtests/{job}/artifacts/report.json") == expected
        assert b"<html" in request(f"/backtests/{job}").lower()

    evidence: dict[str, Any] = {
        "old_commit": old_manifest["commit"],
        "new_commit": new_manifest["commit"],
        "systemd": systemd,
    }
    try:
        if systemd:
            text = (new / "ea.service").read_text().replace("/srv/ea", str(root))
            account = pwd.getpwuid(os.getuid()).pw_name
            import grp

            group = grp.getgrgid(os.getgid()).gr_name
            text = text.replace("User=ea\n", f"User={account}\n").replace(
                "Group=ea\n", f"Group={group}\n"
            )
            prepared = root / unit
            prepared.write_text(text)
            run("systemd-analyze", "verify", str(prepared))
            assert not unit_path.exists()
            run("sudo", "-n", "install", "-m", "644", str(prepared), str(unit_path))
            unit_installed = True
            run("sudo", "-n", "systemctl", "daemon-reload")
            ctl("enable")
            assert ctl("is-enabled") == "enabled"
            evidence["boot_eligibility"] = "VERIFIED; actual reboot not executed"
        start()
        initial_pid = pid()
        health = json.loads(request("/api/health"))
        assert health["offline_only"] is True
        validated = json.loads(request("/api/scenarios/bounded-long.yaml/validate", {}))
        job = json.loads(
            request(
                "/api/backtests",
                {
                    "scenario_id": "bounded-long.yaml",
                    "input_identity": validated["input_identity"],
                    "request_id": "ppv02-installed-acceptance",
                },
            )
        )["job_id"]
        wait_for(lambda: json.loads(request(f"/api/backtests/{job}"))["status"] == "succeeded")
        report = request(f"/api/backtests/{job}/report")
        report_equal(job, report)
        stop()
        state = snapshot(root / "workspace")
        start()
        assert pid() != initial_pid
        report_equal(job, report)
        stop()
        assert snapshot(root / "workspace") == state
        select(new, new_manifest)
        start()
        report_equal(job, report)
        stop()
        assert snapshot(root / "workspace") == state
        select(old, old_manifest)
        start()
        report_equal(job, report)
        evidence["installed_restart_activation_rollback"] = "VERIFIED"
        evidence["job_id"] = job
        evidence["report_sha256"] = hashlib.sha256(report).hexdigest()
        if systemd:
            stop()
            ctl("reset-failed")
            start()
            before = pid()
            ctl("kill", "--kill-whom=main", "--signal=SIGKILL")
            wait_for(lambda: pid() not in (0, before) and available())
            assert int(ctl("show", "--property=NRestarts", "--value")) >= 1
            report_equal(job, report)
            # Repeated real process failures exhaust the canonical 3 starts / 60 seconds.
            for _ in range(2):
                previous = pid()
                ctl("kill", "--kill-whom=main", "--signal=SIGKILL")
                wait_for(
                    lambda previous=previous: (
                        ctl("show", "--property=Result", "--value") == "start-limit-hit"
                        or (pid() not in (0, previous) and available())
                    )
                )
                if ctl("show", "--property=Result", "--value") == "start-limit-hit":
                    break
            wait_for(
                lambda previous=previous: (
                    ctl("show", "--property=Result", "--value") == "start-limit-hit"
                )
            )
            evidence["bounded_failure_restart"] = "VERIFIED"
            ctl("reset-failed")
            start()
            stop()
            time.sleep(6)
            assert pid() == 0 and not available()
            evidence["clean_stop"] = "VERIFIED"
        else:
            # Fault injection here proves lock release and reopen only, not a supervisor policy.
            assert process is not None
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
            start()
            report_equal(job, report)
            stop()
            evidence["systemd_supervision"] = "NOT_YET_HOST_VERIFIED (direct-process mode)"
        assert snapshot(root / "workspace") == state
        evidence["persistent_bytes_unchanged"] = True
        (root / "acceptance.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps(evidence, indent=2), flush=True)
    finally:
        try:
            stop()
        finally:
            if unit_installed:
                journal = run("sudo", "-n", "journalctl", "-u", unit, "--no-pager")
                (root / "journal.txt").write_text(journal)
                ctl("disable")
                unit_path_command = str(unit_path)
                run("sudo", "-n", "rm", "--", unit_path_command)
                run("sudo", "-n", "systemctl", "daemon-reload")
                subprocess.run(["sudo", "-n", "systemctl", "reset-failed", unit], check=False)
            log.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-bundle", type=Path, required=True)
    parser.add_argument("--new-bundle", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--systemd", action="store_true")
    args = parser.parse_args()
    accept(args.old_bundle.resolve(), args.new_bundle.resolve(), args.root, args.systemd)


if __name__ == "__main__":
    main()
