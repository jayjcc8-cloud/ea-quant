#!/usr/bin/env python3
"""Host acceptance for the WU-1 macOS launchd local Paper runtime operator.

This is the macOS counterpart of ``scripts/accept_production_runtime.py``. It
runs the real host drills against the real per-user LaunchAgents, the real
installed ``ea`` entrypoint and the real ``~/EA`` runtime boundary, and writes
machine-readable evidence under ``~/EA/evidence/``.

It performs four drills, in order:

A. No Claude: the supervised ``ea paper start`` runs under launchd with no
   Claude process anywhere in its ancestry, and reports the expected readiness
   and reconciliation projection.
B. Unexpected restart: SIGKILL the supervised Paper process; launchd replaces
   it, the replacement is a fresh attempt identity, and the killed attempt is
   still recoverable only through the existing M1 ``ea paper resume`` path.
C. Scheduled backup: the launchd backup job fires on its own interval and
   produces a backup that ``ea paper backup inspect`` verifies.
D. Stop/start: ``ea-runtime stop`` leaves the run stopped across an observation
   interval longer than the restart throttle, and a later ``ea-runtime start``
   brings a supervised run back.

Nothing here enables Live, submits an order, or hardens this machine: the
acceptance reports host continuity facts rather than changing power management.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, cast

REPO = Path(__file__).resolve().parents[1]
EA = REPO / "venv" / "bin" / "ea"
PAPER_LABEL = "com.ea.paper"
BACKUP_LABEL = "com.ea.paper-backup"
EXAMPLE_INPUTS = ("source.yaml", "prices.csv", "later.yaml", "later.csv")
DEFAULT_BACKUP_INTERVAL_SECONDS = 300
DEFAULT_THROTTLE_SECONDS = 10
STOP_OBSERVATION_SECONDS = 120


class AcceptanceError(RuntimeError):
    """The host acceptance could not complete as required."""


# ---------------------------------------------------------------------------
# process helpers
# ---------------------------------------------------------------------------


def run(*args: str, check: bool = True, timeout: float = 120.0) -> str:
    completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if check and completed.returncode != 0:
        raise AcceptanceError(
            f"{' '.join(args)} failed with {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed.stdout


def run_json(*args: str, check: bool = True) -> tuple[int, Any]:
    completed = subprocess.run(args, capture_output=True, text=True, check=False)
    document: Any = None
    text = completed.stdout.strip()
    if text:
        try:
            document = json.loads(text)
        except ValueError:
            document = None
    if check and completed.returncode != 0:
        raise AcceptanceError(
            f"{' '.join(args)} failed with {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed.returncode, document


def launchd_fields(label: str) -> dict[str, str]:
    """Read the top-level fields of one loaded job from `launchctl print`."""
    completed = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )
    fields: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        match = re.match(r"^\t([a-z][a-z ]*?) = (.*)$", line)
        if match is not None:
            fields.setdefault(match.group(1), match.group(2).strip())
    return fields


def launchd_loaded(label: str) -> bool:
    completed = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


def process_table() -> dict[int, tuple[int, str]]:
    table: dict[int, tuple[int, str]] = {}
    for line in run("ps", "-axo", "pid=,ppid=,command=").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        try:
            table[int(parts[0])] = (int(parts[1]), parts[2])
        except ValueError:
            continue
    return table


def ancestor_chain(pid: int, table: dict[int, tuple[int, str]]) -> list[int]:
    chain = [pid]
    seen = {pid}
    current = pid
    for _ in range(64):
        parent = table.get(current, (1, ""))[0]
        if parent in seen or parent <= 1:
            chain.append(parent)
            return chain
        chain.append(parent)
        seen.add(parent)
        current = parent
    return chain


def paper_processes(runtime_root: Path) -> list[int]:
    """PIDs of live `ea paper start` processes writing into this runtime root."""
    pids: list[int] = []
    for pid, (_, command) in process_table().items():
        if " paper start " in f" {command} " and str(runtime_root) in command:
            pids.append(pid)
    return sorted(pids)


def wait_for(predicate: Any, *, timeout: float, interval: float = 0.5) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


# ---------------------------------------------------------------------------
# operator surface
# ---------------------------------------------------------------------------


def operator(home_ea: Path, *args: str, check: bool = True) -> tuple[int, str]:
    completed = subprocess.run(
        ["/bin/sh", str(home_ea / "supervisor" / "ea-runtime"), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "HOME": str(Path.home()), "EA_RUNTIME_HOME": str(home_ea)},
        timeout=300,
    )
    if check and completed.returncode != 0:
        raise AcceptanceError(
            f"ea-runtime {' '.join(args)} failed with {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    return completed.returncode, completed.stdout


def current_run_dir(home_ea: Path) -> Path | None:
    marker = home_ea / "supervisor" / "state" / "current-run"
    if not marker.is_file():
        return None
    value = marker.read_text(encoding="utf-8").strip()
    return Path(value) if value else None


def launch_history(home_ea: Path) -> list[dict[str, Any]]:
    path = home_ea / "supervisor" / "state" / "launch-history.jsonl"
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def paper_status(run_dir: Path) -> dict[str, Any] | None:
    _, document = run_json(str(EA), "paper", "status", "--run-dir", str(run_dir), check=False)
    return cast("dict[str, Any] | None", document)


def backup_directories(backup_root: Path) -> list[Path]:
    if not backup_root.is_dir():
        return []
    return sorted(
        (
            path
            for path in backup_root.iterdir()
            if path.is_dir() and path.name.startswith("backup-")
        ),
        key=lambda path: path.name,
    )


# ---------------------------------------------------------------------------
# candidate preparation
# ---------------------------------------------------------------------------


class LoopbackWebService:
    """The existing `ea web serve` on loopback, used exactly as the CLI test does."""

    def __init__(self, scenario_root: Path, workspace: Path, ui_dir: Path) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.scenario_root = scenario_root
        self.workspace = workspace
        self.ui_dir = ui_dir
        self.log: IO[bytes] | None = None
        self.process: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> LoopbackWebService:
        self.log = (self.workspace.parent / "logs" / "prepare-web.log").open("ab")
        self.process = subprocess.Popen(
            [
                str(EA),
                "web",
                "serve",
                "--scenario-root",
                str(self.scenario_root),
                "--workspace",
                str(self.workspace),
                "--ui-dir",
                str(self.ui_dir),
                "--port",
                str(self.port),
            ],
            stdout=self.log,
            stderr=self.log,
        )
        if wait_for(self._healthy, timeout=60) is None:
            self.__exit__(None, None, None)
            raise AcceptanceError("the loopback research service did not become healthy")
        return self

    def __exit__(self, *_: object) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=30)
        if self.log is not None:
            self.log.close()

    def _healthy(self) -> bool:
        try:
            self.request("/api/health", None, "GET")
            return True
        except (OSError, urllib.error.URLError, AcceptanceError):
            return False

    def request(self, path: str, body: object = None, method: str = "POST") -> Any:
        headers = {
            "Origin": self.origin,
            "Content-Type": "application/json",
            "X-EA-Web-Request": "1",
        }
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.origin + path, data=data, headers=headers, method=method
        )
        with urllib.request.urlopen(request, timeout=10) as reply:
            payload = reply.read()
        return json.loads(payload) if payload else None

    def wait_for_job(self, job_id: str, timeout: float = 120.0) -> dict[str, Any]:
        def finished() -> dict[str, Any] | None:
            document = self.request(f"/api/backtests/{job_id}", method="GET")
            if document["status"] in {"accepted", "running"}:
                return None
            return cast("dict[str, Any]", document)

        job = wait_for(finished, timeout=timeout, interval=0.2)
        if job is None:
            raise AcceptanceError(f"job {job_id} did not finish")
        if job["status"] != "succeeded":
            raise AcceptanceError(f"job {job_id} finished as {job['status']}")
        return cast("dict[str, Any]", job)


def accepted_candidate_id(workspace: Path, candidate_id: str) -> bool:
    completed = subprocess.run(
        [
            str(EA),
            "candidate",
            "inspect",
            "--workspace",
            str(workspace),
            "--candidate-id",
            candidate_id,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


def prepare_workspace(home_ea: Path, candidate_id: str | None) -> str:
    """Reuse a recorded accepted Candidate, or create one through the real API."""
    workspace = home_ea / "workspace"
    if candidate_id and accepted_candidate_id(workspace, candidate_id):
        return candidate_id
    if workspace.exists() and any(workspace.iterdir()):
        raise AcceptanceError(
            f"{workspace} already holds research data but no recorded accepted Candidate "
            "verifies for this installed EA; refusing to overwrite it. Move that workspace "
            "aside if a fresh Candidate is wanted."
        )
    inputs = home_ea / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    for name in EXAMPLE_INPUTS:
        shutil.copyfile(REPO / "examples" / "paper" / name, inputs / name)
    ui = home_ea / "ui"
    ui.mkdir(parents=True, exist_ok=True)
    (ui / "index.html").write_text("<!doctype html><title>EA Web</title>\n", encoding="utf-8")
    (home_ea / "logs").mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)

    with LoopbackWebService(inputs, workspace, ui) as service:
        identity = service.request("/api/scenarios/source.yaml/validate")["input_identity"]
        source = service.request(
            "/api/backtests",
            {
                "scenario_id": "source.yaml",
                "input_identity": identity,
                "request_id": "wu1-local-runtime-source",
            },
        )
        service.wait_for_job(source["job_id"])
        relation = service.request(
            "/api/holdouts", {"source_job_id": source["job_id"], "scenario_id": "later.yaml"}
        )
        service.wait_for_job(relation["holdout_job_id"])
        created = service.request("/api/candidates", {"validation_id": relation["validation_id"]})
        candidate = created["candidate_id"]
        service.request(
            f"/api/candidates/{candidate}/decision",
            {"outcome": "ACCEPTED", "reason": "WU-1 local runtime operator host drills"},
        )
    if not accepted_candidate_id(workspace, candidate):
        raise AcceptanceError("the accepted Candidate did not verify after creation")
    return cast(str, candidate)


def write_run_config(home_ea: Path, candidate_id: str) -> Path:
    supervisor = home_ea / "supervisor"
    supervisor.mkdir(parents=True, exist_ok=True)
    config = supervisor / "run-config.env"
    if config.is_file():
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        shutil.copyfile(config, supervisor / f"run-config.env.bak-{stamp}")
    document = {
        "EA_BIN": str(EA),
        "WORKSPACE": str(home_ea / "workspace"),
        "CANDIDATE_ID": candidate_id,
        "SCENARIO": str(home_ea / "inputs" / "source.yaml"),
        "RUNTIME_ROOT": str(home_ea / "runtime"),
        "PRICES": "100",
        "INTERVAL": "0.1",
        "EVENT_LIMIT": "",
        "BACKUP_ROOT": str(home_ea / "backups"),
        "BACKUP_KEEP": "5",
        "BACKUP_INTERVAL_SECONDS": str(DEFAULT_BACKUP_INTERVAL_SECONDS),
        "THROTTLE_INTERVAL_SECONDS": str(DEFAULT_THROTTLE_SECONDS),
    }
    config.write_text(
        "".join(f"{key}={value}\n" for key, value in document.items()), encoding="utf-8"
    )
    config.chmod(0o600)
    return config


# ---------------------------------------------------------------------------
# host continuity
# ---------------------------------------------------------------------------


def host_continuity() -> dict[str, Any]:
    def optional(*args: str) -> str:
        return run(*args, check=False).strip()

    filesystem = os.statvfs(Path.home())
    return {
        "uname": optional("uname", "-a"),
        "sw_vers": optional("sw_vers"),
        "uptime": optional("uptime"),
        "battery": optional("pmset", "-g", "batt"),
        "power_settings": optional("pmset", "-g", "custom"),
        "disk_free_bytes": filesystem.f_bavail * filesystem.f_frsize,
        "disk_human": optional("df", "-h", str(Path.home())),
        "note": (
            "Observed only. No power-management setting was changed by this acceptance; "
            "reported so a reviewer can judge whether the host can stay awake for a soak."
        ),
    }


# ---------------------------------------------------------------------------
# drills
# ---------------------------------------------------------------------------


def drill_a(home_ea: Path) -> dict[str, Any]:
    def live() -> dict[str, Any] | None:
        run_dir = current_run_dir(home_ea)
        if run_dir is None or not run_dir.is_dir():
            return None
        status = paper_status(run_dir)
        if status is None or status.get("lease_held") is not True:
            return None
        return status

    status = wait_for(live, timeout=120)
    if status is None:
        raise AcceptanceError("drill A: no live supervised attempt appeared")
    pid = int(launchd_fields(PAPER_LABEL).get("pid", "0"))
    table = process_table()
    chain = ancestor_chain(pid, table)
    chain_commands = [table.get(item, (0, "launchd/kernel"))[1] for item in chain]
    claude_pids = sorted(
        item for item, (_, command) in table.items() if "claude" in command.lower()
    )
    claude_in_ancestry = any("claude" in command.lower() for command in chain_commands)
    run_dir = current_run_dir(home_ea)
    assert run_dir is not None
    health = status["health"]
    result = {
        "CLAUDE_PROCESS": "OFF" if not claude_in_ancestry else "ON",
        "EA_PROCESS": "RUNNING",
        "paper_pid": pid,
        "ancestor_chain": chain,
        "ancestor_commands": chain_commands,
        "claude_processes_on_host": claude_pids,
        "claude_in_paper_ancestry": claude_in_ancestry,
        "launchd": launchd_fields(PAPER_LABEL),
        "run_dir": str(run_dir),
        "state": status["state"],
        "lease_held": status["lease_held"],
        "process_alive": health["process_alive"],
        "runtime_ready": health["runtime_ready"],
        "reconciliation_state": health["reconciliation_state"],
        "storage_state": health["storage_state"],
        "reason_codes": health["reason_codes"],
    }
    failures = []
    if claude_in_ancestry:
        failures.append("a Claude process is an ancestor of the supervised Paper process")
    if status["state"] != "running":
        failures.append(f"expected a running attempt, saw {status['state']}")
    if health["runtime_ready"] is not True:
        failures.append("the health projection is not runtime_ready")
    if health["reconciliation_state"] == "unavailable":
        failures.append("reconciliation could not be observed")
    result["failures"] = failures
    result["result"] = "PASS" if not failures else "FAIL"
    return result


def drill_b(home_ea: Path, runtime_root: Path) -> dict[str, Any]:
    fields = launchd_fields(PAPER_LABEL)
    old_pid = int(fields["pid"])
    old_run = current_run_dir(home_ea)
    if old_run is None:
        raise AcceptanceError("drill B: no recorded run to kill")
    before = len(launch_history(home_ea))
    os.kill(old_pid, signal.SIGKILL)

    def replaced() -> dict[str, Any] | None:
        events = launch_history(home_ea)
        if len(events) <= before:
            return None
        events_tail = events[before:]
        newest = events_tail[-1]
        if int(newest["pid"]) == old_pid:
            return None
        return newest

    newest = wait_for(replaced, timeout=120, interval=0.5)
    if newest is None:
        raise AcceptanceError("drill B: launchd did not replace the killed process")
    new_run = Path(newest["run_dir"])
    if (
        wait_for(lambda: (paper_status(new_run) or {}).get("lease_held") is True, timeout=120)
        is None
    ):
        raise AcceptanceError("drill B: the replacement attempt never became live")

    new_status = paper_status(new_run)
    assert new_status is not None
    resume_code, resume_document = run_json(
        str(EA), "paper", "resume", "--run-dir", str(old_run), check=False
    )
    old_status = paper_status(old_run) or {}
    live_pids = paper_processes(runtime_root)
    new_pid = int(launchd_fields(PAPER_LABEL)["pid"])

    result = {
        "killed_pid": old_pid,
        "killed_run_dir": str(old_run),
        "replacement_pid": new_pid,
        "replacement_run_dir": str(new_run),
        "replacement_run_id": new_run.name,
        "fresh_run_id": new_run.name != old_run.name,
        "replacement_state": new_status["state"],
        "replacement_lease_held": new_status["lease_held"],
        "replacement_process_alive": new_status["health"]["process_alive"],
        "replacement_reconciliation": new_status.get("reconciliation"),
        "killed_attempt_status": {
            "state": old_status.get("state"),
            "lease_held": old_status.get("lease_held"),
            "reason": old_status.get("reason"),
        },
        "m1_resume_exit_code": resume_code,
        "m1_resume_document": resume_document,
        "live_paper_processes": live_pids,
        "launchd": launchd_fields(PAPER_LABEL),
    }
    failures = []
    if new_run.name == old_run.name:
        failures.append("the replacement reused the killed attempt identity")
    if new_pid == old_pid:
        failures.append("the supervised PID did not change")
    if len(live_pids) != 1:
        failures.append(f"expected exactly one live Paper process, saw {live_pids}")
    if old_status.get("lease_held") is not False:
        failures.append("the killed attempt still reports a held writer lease")
    if resume_code not in (0, 3) or resume_document is None:
        failures.append("the M1 resume path did not classify the killed attempt")
    result["failures"] = failures
    result["result"] = "PASS" if not failures else "FAIL"
    return result


def drill_c(home_ea: Path, backup_root: Path, timeout: float) -> dict[str, Any]:
    supervisor = home_ea / "supervisor"
    before_runs = int(launchd_fields(BACKUP_LABEL).get("runs", "0") or "0")
    observed: dict[str, Any] = {}

    def fired() -> dict[str, Any] | None:
        fields = launchd_fields(BACKUP_LABEL)
        runs = int(fields.get("runs", "0") or "0")
        observed["launchd"] = fields
        observed["runs"] = runs
        observed["backups"] = [str(path) for path in backup_directories(backup_root)]
        if runs > before_runs and observed["backups"]:
            return fields
        return None

    if wait_for(fired, timeout=timeout, interval=2.0) is None:
        raise AcceptanceError(
            f"drill C: the scheduled backup job did not fire within {timeout:.0f}s"
        )
    backups = backup_directories(backup_root)
    newest = backups[-1]
    inspect_code, inspect_document = run_json(
        str(EA), "paper", "backup", "inspect", "--backup", str(newest), check=False
    )
    log = supervisor.parent / "logs"
    result = {
        "backup_interval_seconds": DEFAULT_BACKUP_INTERVAL_SECONDS,
        "backup_job_runs_before": before_runs,
        "backup_job_runs_after": observed["runs"],
        "backup_root": str(backup_root),
        "backups": [str(path) for path in backups],
        "backup": str(newest),
        "inspect_exit_code": inspect_code,
        "inspect_document": inspect_document,
        "launchd": observed["launchd"],
        "backup_stdout": (log / "backup.out.log").read_text(encoding="utf-8")[-2000:]
        if (log / "backup.out.log").is_file()
        else "",
        "backup_stderr": (log / "backup.err.log").read_text(encoding="utf-8")[-2000:]
        if (log / "backup.err.log").is_file()
        else "",
    }
    failures = []
    if observed["runs"] <= before_runs:
        failures.append("the backup LaunchAgent did not record a new scheduled run")
    if inspect_code != 0 or not (inspect_document or {}).get("verified"):
        failures.append("ea paper backup inspect did not verify the captured backup")
    result["failures"] = failures
    result["result"] = "PASS" if not failures else "FAIL"
    return result


def drill_d(home_ea: Path, runtime_root: Path) -> dict[str, Any]:
    before_history = launch_history(home_ea)
    stop_code, stop_output = operator(home_ea, "stop", check=False)
    stopped_run = current_run_dir(home_ea)
    assert stopped_run is not None
    stopped_status = paper_status(stopped_run) or {}
    fields_after_stop = launchd_fields(PAPER_LABEL)

    # Observe for longer than the restart throttle: an unconditional restart or
    # a crash-restart loop would show up as a new launch record or a new PID.
    observations: list[dict[str, Any]] = []
    deadline = time.monotonic() + STOP_OBSERVATION_SECONDS
    while time.monotonic() < deadline:
        fields = launchd_fields(PAPER_LABEL)
        observations.append(
            {
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "state": fields.get("state"),
                "pid": fields.get("pid", "0"),
                "runs": fields.get("runs", "0"),
                "launch_records": len(launch_history(home_ea)),
                "paper_processes": paper_processes(runtime_root),
            }
        )
        time.sleep(10)

    stable = all(
        item["launch_records"] == len(before_history)
        and item["paper_processes"] == []
        and item["state"] == "not running"
        for item in observations
    )

    start_code, start_output = operator(home_ea, "start", check=False)
    restarted_run = current_run_dir(home_ea)
    assert restarted_run is not None
    restarted = wait_for(
        lambda: (paper_status(restarted_run) or {}).get("lease_held") is True, timeout=120
    )
    restarted_status = paper_status(restarted_run) or {}

    result = {
        "stop_exit_code": stop_code,
        "stop_output_tail": stop_output[-1000:],
        "stopped_run_dir": str(stopped_run),
        "stopped_state": stopped_status.get("state"),
        "stopped_lease_held": stopped_status.get("lease_held"),
        "stopped_reason": stopped_status.get("reason"),
        "launchd_after_stop": fields_after_stop,
        "observation_seconds": STOP_OBSERVATION_SECONDS,
        "observation_samples": len(observations),
        "observations": observations,
        "stable_while_stopped": stable,
        "start_exit_code": start_code,
        "start_output_tail": start_output[-1000:],
        "restarted_run_dir": str(restarted_run),
        "restarted_run_id": restarted_run.name,
        "restarted_state": restarted_status.get("state"),
        "restarted_lease_held": restarted_status.get("lease_held"),
        "restarted_pid": launchd_fields(PAPER_LABEL).get("pid"),
    }
    failures = []
    if stop_code != 0:
        failures.append(f"ea-runtime stop exited {stop_code}")
    if stopped_status.get("state") != "stopped" or stopped_status.get("lease_held") is not False:
        failures.append("the stopped attempt did not reach a released terminal state")
    if not stable:
        failures.append(
            "launchd restarted or relaunched the job while it was intentionally stopped"
        )
    if start_code != 0 or restarted is None:
        failures.append("a later ea-runtime start did not bring a supervised run back")
    if restarted_run.name == stopped_run.name:
        failures.append("the restarted run reused the stopped attempt identity")
    result["failures"] = failures
    result["result"] = "PASS" if not failures else "FAIL"
    return result


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


def install(home_ea: Path, config: Path) -> dict[str, Any]:
    code, output = operator(home_ea, "install", "--config", str(config), check=False)
    if code != 0:
        raise AcceptanceError(f"ea-runtime install failed with {code}: {output}")
    agents = Path.home() / "Library" / "LaunchAgents"
    documents: dict[str, Any] = {}
    for label in (PAPER_LABEL, BACKUP_LABEL):
        path = agents / f"{label}.plist"
        documents[label] = plistlib.loads(path.read_bytes())
    if not launchd_loaded(PAPER_LABEL) or not launchd_loaded(BACKUP_LABEL):
        raise AcceptanceError("both LaunchAgents must be loaded after install")
    return {
        "plists": {label: path_document for label, path_document in documents.items()},
        "keep_alive": documents[PAPER_LABEL].get("KeepAlive"),
        "launchd_mode": "LaunchAgent (per-user ~/Library/LaunchAgents)",
        "install_output": output[-2000:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home-ea", type=Path, default=Path.home() / "EA")
    parser.add_argument("--evidence-dir", type=Path, default=None)
    parser.add_argument(
        "--backup-timeout",
        type=float,
        default=DEFAULT_BACKUP_INTERVAL_SECONDS + 180,
        help="Seconds to wait for the scheduled backup job to fire.",
    )
    parser.add_argument(
        "--keep-installed",
        action="store_true",
        help="Leave the LaunchAgents loaded after the drills.",
    )
    args = parser.parse_args()

    home_ea: Path = args.home_ea.expanduser().resolve()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    evidence_dir = (args.evidence_dir or (home_ea / "evidence")) / f"wu1-{stamp}"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    runtime_root = home_ea / "runtime"
    backup_root = home_ea / "backups"
    for name in ("runtime", "workspace", "logs", "backups", "supervisor", "inputs", "ui"):
        (home_ea / name).mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "schema": "ea.wu1-local-runtime-acceptance.v1",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo": str(REPO),
        "home_ea": str(home_ea),
        "head_sha": run("git", "-C", str(REPO), "rev-parse", "HEAD").strip(),
        "base_sha": "719852ee65b80a4c227661981d47866b988ffa87",
        "live": "DENIED",
        "evidence_dir": str(evidence_dir),
        "host_continuity": host_continuity(),
    }
    installed = False
    try:
        previous_candidate: str | None = None
        config_path = home_ea / "supervisor" / "run-config.env"
        if config_path.is_file():
            match = re.search(
                r"^CANDIDATE_ID=(.+)$", config_path.read_text(encoding="utf-8"), re.MULTILINE
            )
            if match is not None:
                previous_candidate = match.group(1).strip()
        candidate = prepare_workspace(home_ea, previous_candidate)
        report["candidate_id"] = candidate
        config = write_run_config(home_ea, candidate)
        report["run_config"] = config.read_text(encoding="utf-8")
        report["install"] = install(home_ea, config)
        installed = True
        shutil.copyfile(
            Path.home() / "Library" / "LaunchAgents" / f"{PAPER_LABEL}.plist",
            evidence_dir / f"{PAPER_LABEL}.plist",
        )
        shutil.copyfile(
            Path.home() / "Library" / "LaunchAgents" / f"{BACKUP_LABEL}.plist",
            evidence_dir / f"{BACKUP_LABEL}.plist",
        )

        drills: tuple[tuple[str, Callable[[], dict[str, Any]]], ...] = (
            ("drill_a_no_claude", lambda: drill_a(home_ea)),
            ("drill_b_unexpected_restart", lambda: drill_b(home_ea, runtime_root)),
            (
                "drill_c_scheduled_backup",
                lambda: drill_c(home_ea, backup_root, args.backup_timeout),
            ),
            ("drill_d_stop_start", lambda: drill_d(home_ea, runtime_root)),
        )
        for name, function in drills:
            print(f"running {name} ...", flush=True)
            outcome = function()
            report[name] = outcome
            (evidence_dir / f"{name}.json").write_text(
                json.dumps(outcome, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(f"{name}: {outcome['result']}", flush=True)
    except AcceptanceError as error:
        report["error"] = str(error)
        print(f"acceptance error: {error}", file=sys.stderr, flush=True)
    finally:
        try:
            if installed:
                operator(home_ea, "stop", check=False)
        finally:
            if installed and not args.keep_installed:
                operator(home_ea, "uninstall", check=False)
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        report["launch_agents_removed"] = not launchd_loaded(PAPER_LABEL) and not launchd_loaded(
            BACKUP_LABEL
        )
        drill_results = {
            key: value["result"]
            for key, value in report.items()
            if isinstance(value, dict) and "result" in value
        }
        report["drills"] = drill_results
        report["result"] = (
            "PASS" if drill_results and all(v == "PASS" for v in drill_results.values()) else "FAIL"
        )
        report_path = evidence_dir / "acceptance.json"
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps({"result": report["result"], "drills": drill_results}), flush=True)
        print(f"evidence: {report_path}", flush=True)
    return 0 if report.get("result") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
