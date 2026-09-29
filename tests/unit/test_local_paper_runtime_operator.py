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


def _stub_launchctl(directory: Path) -> Path:
    """A stand-in for `launchctl` that keeps the loaded/state facts in files.

    The runner asks launchd two questions — is this label loaded, and what is
    its job state — and until this stub existed those questions reached the
    host's real `launchctl`, which is neither deterministic nor available on the
    Linux CI runner.
    """
    state = directory / "launchctl-state"
    state.mkdir(parents=True, exist_ok=True)
    script = directory / "launchctl-stub"
    script.write_text(
        "#!/bin/sh\n"
        f'state="{state}"\n'
        'case "$1" in\n'
        "  print)\n"
        "    target=$2; label=${target##*/}\n"
        "    # A dotless target is the gui/<uid> domain itself, not a job: launchd\n"
        "    # answers for a domain it owns even when no job is loaded, and the\n"
        "    # runner uses exactly that to tell 'not loaded' from 'cannot ask'.\n"
        "    case $label in\n"
        "      *.*) ;;\n"
        '      *) printf "%s = {\\n}\\n" "$target"; exit 0 ;;\n'
        "    esac\n"
        '    [ -f "$state/$label.loaded" ] || exit 1\n'
        '    printf "%s = {\\n" "$target"\n'
        '    printf "\\tstate = %s\\n" "$(cat "$state/$label.state")"\n'
        '    printf "\\tpid = %s\\n" "$(cat "$state/$label.pid")"\n'
        '    printf "\\truns = %s\\n" "$(cat "$state/$label.runs")"\n'
        '    printf "\\tlast exit code = %s\\n" "$(cat "$state/$label.exit")"\n'
        '    printf "}\\n"\n'
        "    ;;\n"
        "  bootstrap)\n"
        '    label=$(basename "$3" .plist)\n'
        '    : > "$state/$label.loaded"\n'
        '    printf "not running\\n" > "$state/$label.state"\n'
        '    printf "0\\n" > "$state/$label.pid"\n'
        '    printf "0\\n" > "$state/$label.runs"\n'
        '    printf "0\\n" > "$state/$label.exit"\n'
        "    ;;\n"
        "  bootout)\n"
        "    label=${2##*/}\n"
        '    rm -f "$state/$label.loaded" "$state/$label.state" "$state/$label.pid" \\\n'
        '          "$state/$label.runs" "$state/$label.exit"\n'
        "    ;;\n"
        "  kickstart)\n"
        "    label=${2##*/}\n"
        '    printf "running\\n" > "$state/$label.state"\n'
        "    ;;\n"
        "esac\n"
        "exit 0\n",
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
        self.launchd_state = root / "launchctl-state"
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
        monkeypatch.setenv("EA_LAUNCHCTL", str(_stub_launchctl(root)))
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

    def run(
        self, *args: str, check: bool = True, armed: bool = False
    ) -> subprocess.CompletedProcess[str]:
        """Drive the operator.

        `armed` drops EA_RUNTIME_NO_BOOTSTRAP for this call, so the runner really
        calls the stub launchd. It is how a test puts the machine into the state
        the refusal guards are about.
        """
        env = {**os.environ}
        if armed:
            env.pop("EA_RUNTIME_NO_BOOTSTRAP", None)
        completed = subprocess.run(
            ["/bin/sh", str(RUNTIME), *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if check:
            assert completed.returncode == 0, completed.stderr
        return completed

    def mark_running(self, label: str = PAPER_LABEL) -> None:
        (self.launchd_state / f"{label}.state").write_text("running\n", encoding="utf-8")

    def pin_bounded_config(self, value: str = "40") -> None:
        """Pin a bounded profile the way an older install or a hand-edit would.

        The worker commands read the *pinned* config, so this is the only way to
        put a host into the state those guards are about.
        """
        path = self.supervisor / "run-config.env"
        path.write_text(
            path.read_text(encoding="utf-8").replace("EVENT_LIMIT=\n", f"EVENT_LIMIT={value}\n"),
            encoding="utf-8",
        )

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
        # Only the rendered plist claims the supervised entrypoint; that flag is
        # what makes this job the single writer (see the manual-run refusal test).
        "--supervised",
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
    harness.install(PRICES="100,101", INTERVAL="0.25")
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
    ]


def test_a_supervised_launch_never_carries_an_event_bound(harness: Harness) -> None:
    harness.install()
    harness.run("run", check=False)
    assert "--event-limit" not in harness.argv()[0]


def test_a_bounded_profile_is_refused_before_it_can_become_a_restart_loop(
    harness: Harness,
) -> None:
    """#243 item 1, the P0 before any long Gate.

    `ea paper start` ends a bounded run with a non-zero source-exhaustion exit
    and the paper LaunchAgent restarts an unsuccessful exit by design, so a
    non-empty EVENT_LIMIT under supervision would mint a fresh attempt
    directory, writer lease and audit journal on every restart, forever. The
    profile is refused instead of being given a cleverer restart policy.
    """
    # A good install first, so the refusal has something it could have damaged.
    harness.install()
    pinned = harness.supervisor / "run-config.env"
    pinned_before = pinned.read_bytes()
    plist_before = (harness.agents / f"{PAPER_LABEL}.plist").read_bytes()
    log = harness.home_ea / "logs" / "paper.err.log"
    log.write_text("previous run\n", encoding="utf-8")

    bounded = harness.root / "bounded-config.env"
    bounded.write_text(
        pinned_before.decode("utf-8").replace("EVENT_LIMIT=\n", "EVENT_LIMIT=40\n"),
        encoding="utf-8",
    )
    completed = harness.run("install", "--config", str(bounded), check=False)
    assert completed.returncode != 0
    assert "EVENT_LIMIT must be empty" in completed.stderr
    # Refused before ANY mutation: install copies the config over the pinned one
    # and truncates the logs after this point, so a refusal any later would
    # leave an already-installed host pinned to a config that can never launch.
    assert (harness.agents / f"{PAPER_LABEL}.plist").read_bytes() == plist_before
    assert pinned.read_bytes() == pinned_before
    assert log.read_text(encoding="utf-8") == "previous run\n"
    assert harness.argv() == []


@pytest.mark.parametrize("command", [("install",), ("reload",), ("start",), ("run",)])
def test_every_arming_path_refuses_a_pinned_bounded_profile(
    harness: Harness, command: tuple[str, ...]
) -> None:
    """A host can already be pinned to a bounded profile: an older install wrote
    one, or an operator hand-edited the file. Every path that would render, arm
    or launch it must refuse, not just the one that reads a --config argument."""
    harness.install()
    harness.pin_bounded_config()
    refused = harness.run(*command, check=False)
    assert refused.returncode != 0
    assert "EVENT_LIMIT must be empty" in refused.stderr
    assert harness.argv() == []


@pytest.mark.parametrize("command", [("log",), ("uninstall",), ("status",), ("stop",)])
def test_a_pinned_bounded_profile_never_blocks_recovery(
    harness: Harness, command: tuple[str, ...]
) -> None:
    """The other half of item 1: refusing to ARM a bounded profile must not
    strand a machine already pinned to one. `log` and `uninstall` work outright;
    `status` and `stop` are never refused for the bound, though on a host with
    no attempt yet they may still fail for their own reasons."""
    harness.install()
    harness.pin_bounded_config()
    completed = harness.run(*command, check=False)
    assert "EVENT_LIMIT must be empty" not in completed.stderr
    if command in (("log",), ("uninstall",)):
        assert completed.returncode == 0, completed.stderr


def test_a_manual_run_is_refused_while_the_launch_agent_is_loaded(harness: Harness) -> None:
    """#243 item 2, the other P0.

    A hand-run `ea-runtime run` would be a second, unsupervised Paper process
    against the same Candidate. Distinct attempt directories keep the files
    apart, but `ea-runtime stop` aims at the attempt the agent recorded, so the
    manual process would keep trading unobserved with its own writer lease.
    """
    harness.run("install", "--config", str(harness.config()), armed=True)
    refused = harness.run("run", check=False)
    assert refused.returncode != 0
    assert "refusing an unsupervised run" in refused.stderr
    assert harness.argv() == []

    # Only the flag the rendered plist passes claims the supervised entrypoint,
    # and it is never forwarded to the product CLI.
    assert harness.run("run", "--supervised", check=False).returncode == 0
    assert harness.argv()[0].startswith("paper start ")
    assert "--supervised" not in harness.argv()[0]

    # Unloading the agent frees the manual path again.
    harness.run("uninstall", armed=True)
    assert harness.run("run", check=False).returncode == 0


def test_a_manual_run_fails_closed_when_launchd_cannot_be_reached(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A negative answer is only worth something if launchd is answering.

    Over a non-GUI session (ssh, or a broken EA_LAUNCHCTL) every `launchctl
    print` fails, including the one for a label that IS loaded. Reading that as
    "not loaded" would let a hand-run become the second writer the guard exists
    to prevent, so "cannot tell" must refuse rather than allow.
    """
    harness.install()
    monkeypatch.setenv("EA_LAUNCHCTL", "/nonexistent-launchctl")
    refused = harness.run("run", check=False)
    assert refused.returncode != 0
    assert "launchd is not answering" in refused.stderr
    assert harness.argv() == []

    # The supervised path is unaffected: launchd runs the job in its own GUI
    # domain, and the flag is what tells the runner it came from the plist.
    assert harness.run("run", "--supervised", check=False).returncode == 0
    assert harness.argv()[0].startswith("paper start ")


def test_reload_refuses_to_restart_a_live_run(harness: Harness) -> None:
    """#243 item 3.

    `reload` re-renders and then boots the label out and back in, and RunAtLoad
    starts a fresh attempt. Because `start` kickstarts the already-loaded
    definition, a later stop/start would not even pick the re-render up, so the
    restart is refused rather than left as a silent side effect.
    """
    harness.run("install", "--config", str(harness.config()), armed=True)
    harness.mark_running()
    refused = harness.run("reload", check=False, armed=True)
    assert refused.returncode != 0
    assert "reload would restart it" in refused.stderr
    # Refused before it wrote anything, so the live run's definition is intact.
    assert harness.plist(PAPER_LABEL)["ThrottleInterval"] == 10

    # A stopped profile may accept a re-render, and the new render really lands.
    (harness.launchd_state / f"{PAPER_LABEL}.state").write_text("not running\n", encoding="utf-8")
    harness.config(THROTTLE_INTERVAL_SECONDS="30")
    assert harness.run("reload", check=False, armed=True).returncode == 0
    assert harness.plist(PAPER_LABEL)["ThrottleInterval"] == 30
    assert (harness.launchd_state / f"{PAPER_LABEL}.loaded").exists()


def test_install_refuses_to_restart_a_live_run(harness: Harness) -> None:
    """`install` is the same door as `reload`: it re-renders and then boots the
    label out and back in, so RunAtLoad would silently start a fresh attempt on
    a live run. The refusal covers it for the same reason."""
    harness.run("install", "--config", str(harness.config()), armed=True)
    harness.mark_running()
    refused = harness.run(
        "install",
        "--config",
        str(harness.config(THROTTLE_INTERVAL_SECONDS="30")),
        check=False,
        armed=True,
    )
    assert refused.returncode != 0
    assert "install would restart it" in refused.stderr
    # Refused before it re-rendered: the live definition still has the old value.
    assert harness.plist(PAPER_LABEL)["ThrottleInterval"] == 10


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
        {"EVENT_LIMIT": "40"},
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
