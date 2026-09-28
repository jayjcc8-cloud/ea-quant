"""Contract tests for the macOS launchd local Paper operator layer.

These run without launchd: they render the real templates through the real
install path, drive the real runner against a stub `ea` entrypoint, and assert
the lifecycle contract the host acceptance then proves on the machine. The
subject is the operator layer only; no trading behaviour is re-tested here.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

REPO = Path(__file__).resolve().parents[2]
OPS = REPO / "ops" / "launchd"
RUNTIME = OPS / "ea-runtime"
PAPER_LABEL = "com.ea.paper"
BACKUP_LABEL = "com.ea.paper-backup"

# A run id shaped exactly like the ones the runner mints, so attempt-directory
# selection sees realistic names.
ATTEMPT_OLD = "aaaaaaaa-1111-4111-8111-111111111111"
ATTEMPT_MID = "bbbbbbbb-2222-4222-8222-222222222222"
ATTEMPT_NEW = "cccccccc-3333-4333-8333-333333333333"
CANDIDATE = "11111111-2222-4333-8444-555555555555"


def _stub_ea(directory: Path) -> Path:
    """A stand-in for `ea` that records argv and fakes the two read commands."""
    script = directory / "ea-stub"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "ARGV %s\\n" "$*" >> "{directory / "argv.log"}"\n'
        'if [ "$1" = "paper" ] && [ "$2" = "start" ]; then\n'
        '  printf \'{"paper_started":"stub"}\\n\'\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "paper" ] && [ "$2" = "status" ]; then\n'
        "  shift 2\n"
        '  dir=""; while [ $# -gt 0 ]; do case "$1" in --run-dir) dir=$2; shift 2;; '
        "*) shift;; esac; done\n"
        '  held=true; [ -f "$dir/.settled" ] && held=false\n'
        '  printf \'{"lease_held":%s}\\n\' "$held"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "paper" ] && [ "$2" = "backup" ]; then\n'
        "  shift 2\n"
        '  dir=""; root=""; keep=5\n'
        '  while [ $# -gt 0 ]; do case "$1" in --run-dir) dir=$2; shift 2;; '
        "--backup-root) root=$2; shift 2;; --keep) keep=$2; shift 2;; *) shift;; esac; done\n"
        '  rid=${dir##*/}; mkdir -p "$root/backup-20260101T000000000000Z-$rid"\n'
        '  printf \'{"backup":"%s/backup-20260101T000000000000Z-%s","keep":%s}\\n\' '
        '"$root" "$rid" "$keep"\n'
        "  exit 0\n"
        "fi\n"
        "exit 9\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script


class Harness:
    """One throwaway runtime home plus the operator environment that points at it."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.home_ea = root / "EA"
        self.agents = root / "LaunchAgents"
        self.supervisor = self.home_ea / "supervisor"
        self.runtime = self.home_ea / "runtime"
        self.backups = self.home_ea / "backups"
        self.workspace = root / "workspace"
        self.inputs = root / "inputs"
        for directory in (
            self.supervisor,
            self.runtime,
            self.backups,
            self.workspace,
            self.inputs,
            self.home_ea / "logs",
            self.agents,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        (self.inputs / "source.yaml").write_text("schema_version: 5\n", encoding="utf-8")
        self.ea = _stub_ea(root)
        monkeypatch.setenv("EA_RUNTIME_HOME", str(self.home_ea))
        monkeypatch.setenv("EA_LAUNCH_AGENTS_DIR", str(self.agents))
        monkeypatch.setenv("EA_RUNTIME_NO_BOOTSTRAP", "1")

    def config(self, **overrides: str) -> Path:
        document = {
            "EA_BIN": str(self.ea),
            "WORKSPACE": str(self.workspace),
            "CANDIDATE_ID": CANDIDATE,
            "SCENARIO": str(self.inputs / "source.yaml"),
            "RUNTIME_ROOT": str(self.runtime),
            "PRICES": "100",
            "INTERVAL": "0.1",
            "EVENT_LIMIT": "",
            "BACKUP_ROOT": str(self.backups),
            "BACKUP_KEEP": "5",
            "BACKUP_INTERVAL_SECONDS": "300",
            "THROTTLE_INTERVAL_SECONDS": "10",
            **overrides,
        }
        path = self.supervisor / "run-config.env"
        path.write_text(
            "".join(f"{key}={value}\n" for key, value in document.items()), encoding="utf-8"
        )
        return path

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["/bin/sh", str(RUNTIME), *args],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ},
        )
        if check:
            assert completed.returncode == 0, completed.stderr
        return completed

    def install(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        return self.run("install", "--config", str(self.config(**overrides)))

    def plist(self, label: str) -> dict[str, Any]:
        return cast("dict[str, Any]", plistlib.loads((self.agents / f"{label}.plist").read_bytes()))

    def argv(self) -> list[str]:
        log = self.root / "argv.log"
        if not log.is_file():
            return []
        return [line.removeprefix("ARGV ") for line in log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


def test_runner_is_posix_sh_parseable_and_never_daemonizes() -> None:
    assert subprocess.run(["sh", "-n", str(RUNTIME)], check=False).returncode == 0
    source = RUNTIME.read_text(encoding="utf-8")
    assert 'exec "$@"' in source
    for detached in ("nohup", "setsid", "disown", "daemon(", "& \n", " &$"):
        assert detached not in source


def test_paper_agent_is_a_per_user_launch_agent_that_only_restarts_unsuccessful_exits(
    harness: Harness,
) -> None:
    harness.install()
    document = harness.plist(PAPER_LABEL)
    assert document["Label"] == PAPER_LABEL
    assert document["ProgramArguments"] == [
        "/bin/sh",
        str(harness.supervisor / "ea-runtime"),
        "run",
    ]
    # The crash-versus-intentional-stop distinction is launchd's own
    # SuccessfulExit rule, never an unconditional KeepAlive.
    assert document["KeepAlive"] == {"SuccessfulExit": False}
    assert document["KeepAlive"] is not True
    assert document["RunAtLoad"] is True
    assert document["ThrottleInterval"] == 10
    assert document["StandardOutPath"] == str(harness.home_ea / "logs" / "paper.out.log")
    assert "EnvironmentVariables" not in document


def test_backup_agent_schedules_the_existing_backup_command(harness: Harness) -> None:
    harness.install(BACKUP_INTERVAL_SECONDS="120")
    document = harness.plist(BACKUP_LABEL)
    assert document["ProgramArguments"] == [
        "/bin/sh",
        str(harness.supervisor / "ea-runtime"),
        "backup",
    ]
    assert document["StartInterval"] == 120
    assert "KeepAlive" not in document
    assert "RunAtLoad" not in document


def test_every_supervised_launch_mints_a_fresh_attempt_identity(harness: Harness) -> None:
    harness.install()
    assert harness.run("run", check=False).returncode == 0
    assert harness.run("run", check=False).returncode == 0
    run_ids = [
        argv.split("--run-id ")[1].split()[0] for argv in harness.argv() if "--run-id " in argv
    ]
    assert len(run_ids) == 2
    assert len(set(run_ids)) == 2
    # A crash replacement must never collide with the attempt it replaces, so
    # the recorded current run always names the most recent identity.
    current = (harness.supervisor / "state" / "current-run").read_text(encoding="utf-8").strip()
    assert current == str(harness.runtime / run_ids[-1])
    history = [
        json.loads(line)
        for line in (harness.supervisor / "state" / "launch-history.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event["run_id"] for event in history] == run_ids


def test_launch_pins_every_input_from_the_config_file(harness: Harness) -> None:
    harness.install(PRICES="100,101", INTERVAL="0.25", EVENT_LIMIT="40")
    harness.run("run", check=False)
    argv = harness.argv()[0]
    assert argv.split() == [
        "paper",
        "start",
        "--workspace",
        str(harness.workspace),
        "--candidate-id",
        CANDIDATE,
        "--scenario",
        str(harness.inputs / "source.yaml"),
        "--output-root",
        str(harness.runtime),
        "--run-id",
        argv.split("--run-id ")[1].split()[0],
        "--prices",
        "100,101",
        "--interval",
        "0.25",
        "--event-limit",
        "40",
    ]


def test_launch_omits_the_event_limit_when_the_config_leaves_it_empty(harness: Harness) -> None:
    harness.install()
    harness.run("run", check=False)
    assert "--event-limit" not in harness.argv()[0]


@pytest.mark.parametrize(
    "overrides",
    [
        {"RUNTIME_ROOT": "relative/runtime"},
        {"CANDIDATE_ID": "not-a-uuid"},
        {"BACKUP_KEEP": "0"},
        {"BACKUP_INTERVAL_SECONDS": "soon"},
        {"THROTTLE_INTERVAL_SECONDS": "0"},
        {"WORKSPACE": "/tmp/has space"},
        {"EVENT_LIMIT": "-1"},
    ],
)
def test_the_runner_refuses_a_config_it_cannot_pin_exactly(
    harness: Harness, overrides: dict[str, str]
) -> None:
    harness.config(**overrides)
    completed = harness.run("run", check=False)
    assert completed.returncode != 0
    assert "ea-runtime:" in completed.stderr


def _attempt(harness: Harness, run_id: str, *, settled: bool, order: int) -> Path:
    directory = harness.runtime / run_id
    directory.mkdir(parents=True, exist_ok=True)
    if settled:
        (directory / ".settled").write_text("", encoding="utf-8")
    stamp = 1_767_225_600 + order * 3600
    os.utime(directory, (stamp, stamp))
    return directory


def test_scheduled_backup_captures_only_the_newest_settled_uncaptured_attempt(
    harness: Harness,
) -> None:
    harness.install()
    oldest = _attempt(harness, ATTEMPT_OLD, settled=True, order=1)
    captured = _attempt(harness, ATTEMPT_MID, settled=True, order=2)
    live = _attempt(harness, ATTEMPT_NEW, settled=False, order=3)
    (harness.backups / f"backup-20260101T000000000000Z-{ATTEMPT_MID}").mkdir()

    first = harness.run("backup")
    assert f"capturing {oldest}" in first.stdout
    captures = [line for line in harness.argv() if line.startswith("paper backup ")]
    assert len(captures) == 1
    assert captures[0].startswith(f"paper backup --run-dir {oldest} ")
    # The live attempt is never captured: a held writer lease is not quiesced,
    # and the already-captured attempt is never captured twice. Both are still
    # observed, and neither observation is allowed to mutate anything.
    assert str(live) not in " ".join(captures)
    assert str(captured) not in " ".join(captures)

    harness.run("backup")
    assert "no settled attempt needs capture" in harness.run("backup").stdout


def test_scheduled_backup_publishes_nothing_when_every_attempt_is_live(harness: Harness) -> None:
    harness.install()
    _attempt(harness, ATTEMPT_NEW, settled=False, order=1)
    completed = harness.run("backup")
    assert "no settled attempt needs capture" in completed.stdout
    assert harness.argv() == [
        f"paper status --run-dir {harness.runtime / ATTEMPT_NEW}",
    ]
    assert harness.backups.is_dir()
