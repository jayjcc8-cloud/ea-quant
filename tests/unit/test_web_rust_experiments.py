"""The isolated candidate workspace: the Rust core owns the run, Python projects it.

Everything here is the real product owner unless it is explicitly a double. The
quine core is the real compiled binary taking its real workspace lock, spawning
the real Python worker and running the real engine, and the Web service, its
stores and the strategy owner are the real ones. Only the model provider and the
deliberately broken IPC clients are doubles.

The behaviours this module holds the integration to:

* a confirmation under the strategy lock freezes the admitted input through the
  domain adapter, sends exactly one stable launch intent, and the core alone
  dispatches, cancels and commits;
* an experiment owned by the core creates no Python research child, reservation,
  job, attempt or publication in this workspace;
* a lost IPC reply is answered by *querying that same intent*, never by a second
  confirmation and never by a local success or failure claim;
* a cancellation that arrives before the confirmation reaches the core's own
  barrier, and the confirmation is refused by the core rather than locally;
* a read rebuilds the projection from the core, so a restart — or a wiped
  projection — reconstructs the state, and a core that cannot be reached reads as
  unknown rather than from a cached terminal claim;
* the official report is served only for a success the core registered for exactly
  the frozen input that was confirmed, byte for byte.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket as socket_module
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from ea.web.app import WebSettings, create_app
from ea.web.quine import QuineBridge, QuineClient, QuineHoldouts, QuineJobReader, QuineUnavailable
from ea.web.research import ResearchOrchestrator, ResearchStore
from ea.web.service import ReportUnavailableError, WebService
from ea.web.settings import RadianSettings
from ea.web.user_strategies import (
    StrategyDraft,
    UserStrategyConflictError,
    UserStrategyService,
    UserStrategyStore,
)
from unit.test_web_api import ORIGIN, WRITE_HEADERS
from unit.test_web_research import _scenario_root

pytestmark = pytest.mark.filterwarnings(
    "ignore:The anyio.abc.BlockingPortal alias is deprecated:DeprecationWarning"
)

ROOT = Path(__file__).resolve().parents[2]
#: The prebuilt debug binary of the core and the toolchain that builds it. Both
#: live under ``.local`` so nothing here installs or writes outside the checkout.
CORE_BINARY = ROOT / ".local" / "rust-target" / "debug" / "quine-core"
CARGO = ROOT / ".local" / "rust" / "cargo" / "bin" / "cargo"
CRATE = ROOT / "crates" / "quine-core"
#: One private bound local socket needs a short path (``SUN_LEN`` is 104 bytes on
#: macOS), so each core gets a short directory of its own under ``.local`` instead
#: of the deeper ``--basetemp`` tree. It is removed when the test that made it ends.
CORE_ROOT = ROOT / ".local" / "quine-integration"
SHORT_WORKSPACES: list[Path] = []
SCENARIO = "bounded-long.yaml"
RUN_BUDGET = {"max_model_calls": 0, "max_backtests": 1}
FACT_PATHS = (
    "economics.net_pnl.amount",
    "economics.fees.amount",
    "economics.total_return.value",
    "economics.counts.fills",
    "economics.counts.orders",
)


def _short_workspace() -> Path:
    CORE_ROOT.mkdir(parents=True, exist_ok=True)
    workspace = CORE_ROOT / uuid4().hex[:8]
    workspace.mkdir()
    SHORT_WORKSPACES.append(workspace)
    return workspace


def _cargo() -> tuple[str, dict[str, str]] | None:
    """The cargo to build with, and the environment that keeps it checkout-local."""
    environment = dict(os.environ)
    environment["CARGO_TARGET_DIR"] = str(ROOT / ".local" / "rust-target")
    if (ROOT / ".local" / "rust" / "cargo").is_dir():
        environment.setdefault("CARGO_HOME", str(ROOT / ".local" / "rust" / "cargo"))
    if (ROOT / ".local" / "rust" / "rustup").is_dir():
        environment.setdefault("RUSTUP_HOME", str(ROOT / ".local" / "rust" / "rustup"))
    if CARGO.is_file():
        return str(CARGO), environment
    found = shutil.which("cargo")
    return (found, environment) if found is not None else None


def _build_core() -> Path:
    """The real core binary, built from *this* checkout's sources before any test runs.

    The binary is rebuilt every session rather than reused because a stale one would
    prove the sources of whoever built it last instead of the sources under test —
    exactly the way a real change can pass here while being absent from the binary.

    Every way of not having that binary is a failure of this precondition, never a
    skip: these are the integration tests for the confirmed-experiment contract, and
    a run in which they silently did not happen is not a run of them. A machine with
    no Rust toolchain, a toolchain that cannot be executed, and a toolchain that
    cannot build this crate are three different messages for one outcome — the tests
    did not run, so the suite fails and says why.
    """
    toolchain = _cargo()
    if toolchain is None:
        raise AssertionError(
            "no Rust toolchain is available here, so quine-core cannot be built from this "
            f"checkout; these integration tests are never skipped. Expected cargo at {CARGO} "
            "or on PATH."
        )
    command, environment = toolchain
    try:
        built = subprocess.run(
            [command, "build", "--manifest-path", str(CRATE / "Cargo.toml")],
            check=False,
            capture_output=True,
            env=environment,
            timeout=900,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AssertionError(f"the Rust toolchain {command!r} could not be run: {error}") from None
    if built.returncode != 0 or not CORE_BINARY.is_file():
        detail = built.stderr.decode("utf-8", errors="replace")[-4000:]
        raise AssertionError(f"quine-core could not be built from this checkout:\n{detail}")
    return CORE_BINARY


_BUILT_CORE: Path | None = None


def _core_binary() -> Path:
    """The real core binary, built from this checkout once per test session."""
    global _BUILT_CORE
    if _BUILT_CORE is None:
        _BUILT_CORE = _build_core()
    return _BUILT_CORE


# --- the build precondition ---------------------------------------------------


def test_a_missing_toolchain_fails_these_integration_tests_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not having a toolchain is a failed precondition, never a quiet absence."""
    monkeypatch.setattr(sys.modules[__name__], "_cargo", lambda: None)
    with pytest.raises(AssertionError, match="no Rust toolchain"):
        _build_core()


def test_a_toolchain_that_cannot_build_this_crate_fails_with_its_own_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A toolchain that is present and fails is reported as that build failure."""
    monkeypatch.setattr(sys.modules[__name__], "_cargo", lambda: (sys.executable, dict(os.environ)))
    with pytest.raises(AssertionError, match="could not be built from this checkout"):
        _build_core()


class Core:
    """One real core process serving one workspace of its own."""

    def __init__(
        self, root: Path, *, args: tuple[str, ...] = (), workspace: Path | None = None
    ) -> None:
        # A restart takes the same workspace again: it is the same owner coming
        # back, not a second one with an empty lock and an empty store.
        self.workspace = _short_workspace() if workspace is None else workspace
        self.socket = self.workspace / "core.sock"
        self.log = root / "core.log"
        self._log = self.log.open("ab")
        self.process = subprocess.Popen(
            [
                str(_core_binary()),
                "serve",
                "--workspace",
                str(self.workspace),
                "--python",
                sys.executable,
                *args,
            ],
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        self._await_socket()

    def _await_socket(self, seconds: float = 20.0) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.socket.exists():
                probe = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
                try:
                    probe.settimeout(1.0)
                    probe.connect(str(self.socket))
                    return
                except OSError:
                    pass
                finally:
                    probe.close()
            if self.process.poll() is not None:
                raise AssertionError(f"the core exited: {self._tail()}")
            time.sleep(0.02)
        raise AssertionError(f"the core never answered on {self.socket}: {self._tail()}")

    def _tail(self) -> str:
        try:
            return self.log.read_text(encoding="utf-8", errors="replace")[-2000:]
        except OSError:
            return ""

    def kill(self) -> None:
        """Stop the core without giving it a chance to settle anything."""
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=20)

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=20)
        self._log.close()


@dataclass
class Stack:
    """One process image whose experiment owner is the core."""

    strategies: UserStrategyService
    service: WebService
    bridge: QuineBridge
    workspace: Path


_STARTED: list[Any] = []


@pytest.fixture(autouse=True)
def _stop_started() -> Iterator[None]:
    yield
    while _STARTED:
        _STARTED.pop().stop()
    while SHORT_WORKSPACES:
        shutil.rmtree(SHORT_WORKSPACES.pop(), ignore_errors=True)


def _settings(tmp_path: Path) -> RadianSettings:
    return RadianSettings(tmp_path / "settings.json")


def _stack(tmp_path: Path, socket: Path) -> Stack:
    workspace = tmp_path / "workspace"
    bridge = QuineBridge(socket, confirm_timeout=60.0, query_timeout=20.0)
    service = WebService(
        _scenario_root(tmp_path),
        workspace,
        external=QuineJobReader(bridge),
    )
    service.start()
    _STARTED.append(service)
    research = ResearchOrchestrator(ResearchStore(workspace), service, _settings(tmp_path))
    strategies = UserStrategyService(
        UserStrategyStore(workspace),
        research,
        service,
        _settings(tmp_path),
        quine=bridge,
    )
    # The owner admits the chronological holdouts of the runs it holds, exactly as
    # the real application wires it.
    service.attach_holdout_owner(QuineHoldouts(bridge, service, research))
    return Stack(strategies, service, bridge, workspace)


def _rebind(stack: Stack, socket: Path) -> None:
    """A restarted owner: the same service now speaks to a new core process.

    The owner is swapped through the same test seam the rest of the suite uses for
    an injected double, because the service is built with one owner for its life.
    """
    stack.bridge = QuineBridge(socket, confirm_timeout=60.0, query_timeout=20.0)
    cast(Any, stack.strategies)._quine = stack.bridge


def _create(strategies: UserStrategyService) -> Any:
    return strategies.create(
        name="Long AAPL",
        intent="Test one registered entry on the fixture data",
        scenario_id=SCENARIO,
        criterion={"field": "economics.net_pnl.amount", "expectation": "positive"},
    )


def _launch(strategies: UserStrategyService, strategy_id: str, *, intent: str = "li-1") -> Any:
    preview = strategies.preview_experiment(strategy_id)
    return strategies.launch_experiment(
        strategy_id,
        launch_intent_id=intent,
        base_revision=preview["draft_revision"],
        preview_identity=preview["preview_identity"],
        approved_budget=dict(RUN_BUDGET),
    )


def _row(strategy: Any, intent: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        next(item for item in strategy.experiments if item["launch_intent_id"] == intent),
    )


def _wait(
    strategies: UserStrategyService,
    strategy_id: str,
    intent: str = "li-1",
    *,
    terminal: tuple[str, ...] = ("created", "failed", "cancelled", "unknown"),
    seconds: float = 180.0,
) -> dict[str, Any]:
    """Wait for one core-owned experiment to leave the states it can still leave."""
    deadline = time.monotonic() + seconds
    record: dict[str, Any] = {}
    while time.monotonic() < deadline:
        record = _row(strategies.reconcile(strategy_id), intent)
        if record["status"] in terminal:
            return record
        time.sleep(0.2)
    raise AssertionError(f"experiment {intent} never settled: {record}")


def _no_python_owner(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Prove the migrated path reaches no Python owner: no child, no reservation."""
    counts = {"children": 0, "owners": 0}
    real_spawn = WebService._spawn_worker

    def counted_spawn(self: WebService, control: Any) -> Any:
        counts["children"] += 1
        return real_spawn(self, control)

    def refused_owner(*_args: Any, **_kwargs: Any) -> Any:
        counts["owners"] += 1
        raise AssertionError("the in-process research owner must not be reached")

    monkeypatch.setattr(WebService, "_spawn_worker", counted_spawn)
    monkeypatch.setattr(ResearchOrchestrator, "create_owned_experiment", refused_owner)
    return counts


# --- the real run -----------------------------------------------------------


def test_confirmed_experiment_runs_through_the_core_and_publishes_its_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = Core(tmp_path)
    _STARTED.append(core)
    stack = _stack(tmp_path, core.socket)
    counts = _no_python_owner(monkeypatch)
    strategy = _create(stack.strategies)
    identity = strategy.user_strategy_id

    launched = _launch(stack.strategies, identity)
    record = _row(launched, "li-1")
    assert record["owner"] == "quine"
    assert record["status"] in {"confirmed", "running", "created"}

    settled = _wait(stack.strategies, identity)
    assert settled["status"] == "created", settled["message"]
    assert settled["report_ready"] is True
    assert settled["frozen_sha256"] and settled["attempt_id"]
    facts = {
        item["path"]: item["value"] for item in cast(list[dict[str, object]], settled["facts"])
    }
    assert set(FACT_PATHS) <= set(facts)
    # The migrated path reached no Python research owner and spawned no worker of
    # this service: the run, its supervision and its result are the core's own.
    assert counts == {"children": 0, "owners": 0}

    # The run is readable through the workspace's own job vocabulary, out of the
    # core's publication and against the digest the core registered for it.
    job_id = cast(str, settled["job_id"])
    projected = stack.service.get_job(job_id)
    assert projected.document()["report_ready"] is True
    assert json.loads(stack.service.report(job_id))["run_id"] == settled["attempt_id"]
    assert stack.service.artifact(job_id, "summary.txt")

    # Nothing of this run was copied into this workspace's own index.
    assert stack.service.list_jobs() == []
    assert list((stack.workspace / "jobs").iterdir()) == []
    assert list((stack.workspace / "reports").iterdir()) == []


def test_repeated_confirmation_replays_and_a_different_input_conflicts(tmp_path: Path) -> None:
    core = Core(tmp_path)
    _STARTED.append(core)
    stack = _stack(tmp_path, core.socket)
    strategy = _create(stack.strategies)
    identity = strategy.user_strategy_id
    _launch(stack.strategies, identity)
    settled = _wait(stack.strategies, identity)
    assert settled["status"] == "created"

    # The same intent, preview and budget resolve to the run already confirmed:
    # nothing is dispatched again and no second backtest is charged.
    replayed = _launch(stack.strategies, identity)
    again = _row(replayed, "li-1")
    assert (again["job_id"], again["attempt_id"]) == (settled["job_id"], settled["attempt_id"])
    assert len(replayed.experiments) == 1

    preview = stack.strategies.preview_experiment(identity)
    with pytest.raises(UserStrategyConflictError):
        stack.strategies.launch_experiment(
            identity,
            launch_intent_id="li-1",
            base_revision=preview["draft_revision"],
            preview_identity="sha256:" + "0" * 64,
            approved_budget=dict(RUN_BUDGET),
        )


def test_cancel_before_confirm_reaches_the_core_barrier(tmp_path: Path) -> None:
    core = Core(tmp_path)
    _STARTED.append(core)
    stack = _stack(tmp_path, core.socket)
    strategy = _create(stack.strategies)
    identity = strategy.user_strategy_id
    preview = stack.strategies.preview_experiment(identity)

    # The stop is accepted for an intent the core has never seen, and the barrier
    # is the core's own: it holds the cancellation before any confirmation exists.
    cancellation = stack.bridge.cancel(identity, "li-1")
    assert cancellation["cancel_receipt"]
    assert cancellation["experiment"] is None
    assert stack.bridge.query(identity, "li-1")[1] is not None

    # The confirmation that follows is refused by the core, and what this owner
    # records is that refusal: cancelled, with no run and no result of any kind.
    launched = stack.strategies.launch_experiment(
        identity,
        launch_intent_id="li-1",
        base_revision=preview["draft_revision"],
        preview_identity=preview["preview_identity"],
        approved_budget=dict(RUN_BUDGET),
    )
    record = _row(launched, "li-1")
    assert record["status"] == "cancelled"
    assert "cancelled before it was confirmed" in cast(str, record["message"])
    assert record["job_id"] is None
    assert record["report_ready"] is False
    assert stack.bridge.query(identity, "li-1")[0] is None
    assert stack.service.list_jobs() == []


# --- a lost reply and a broken owner ---------------------------------------


class LosingClient(QuineClient):
    """Sends the confirmation for real, then loses its reply exactly once."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.confirms = 0
        self.lost = 0

    def _exchange(self, line: bytes, wait: float) -> bytes:
        if b'"confirm"' in line:
            self.confirms += 1
            if self.lost == 0:
                self.lost += 1
                stream = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
                try:
                    stream.settimeout(wait)
                    stream.connect(str(self.socket_path))
                    stream.sendall(line)
                finally:
                    # The core writes its reply into a socket nobody reads: the
                    # confirmation happened and this process never saw the answer.
                    time.sleep(0.5)
                    stream.close()
                raise QuineUnavailable("the reply was lost")
        return super()._exchange(line, wait)


def test_lost_reply_is_query_only_and_never_a_second_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = Core(tmp_path)
    _STARTED.append(core)
    client = LosingClient(core.socket, timeout=60.0)
    workspace = tmp_path / "workspace"
    bridge = QuineBridge(core.socket, confirm_timeout=60.0, query_timeout=20.0, client=client)
    service = WebService(_scenario_root(tmp_path), workspace, external=QuineJobReader(bridge))
    service.start()
    _STARTED.append(service)
    counts = _no_python_owner(monkeypatch)
    strategies = UserStrategyService(
        UserStrategyStore(workspace),
        ResearchOrchestrator(ResearchStore(workspace), service, _settings(tmp_path)),
        service,
        _settings(tmp_path),
        quine=bridge,
    )
    strategy = _create(strategies)
    identity = strategy.user_strategy_id

    # The core really confirmed it; the answer never arrived. What is recorded is
    # the core's own state — never a local failure and never a local success.
    launched = _launch(strategies, identity)
    record = _row(launched, "li-1")
    assert client.confirms == 1
    assert record["status"] in {"confirmed", "running", "created"}

    # Asking about that same confirmation again queries the core: no second
    # confirmation is sent and the run is the one already created.
    again = _launch(strategies, identity)
    second = _row(again, "li-1")
    assert client.confirms == 1
    assert second["attempt_id"] == record["attempt_id"]
    assert len(again.experiments) == 1

    settled = _wait(strategies, identity)
    assert settled["status"] == "created"
    assert settled["attempt_id"] == record["attempt_id"]
    assert counts == {"children": 0, "owners": 0}


def test_unreachable_core_reports_unconfirmed_and_never_a_fallback_run(tmp_path: Path) -> None:
    absent = tmp_path / "absent-core"
    absent.mkdir()
    stack = _stack(tmp_path, absent / "core.sock")
    strategy = _create(stack.strategies)

    launched = _launch(stack.strategies, strategy.user_strategy_id)
    record = _row(launched, "li-1")
    assert record["status"] == "unconfirmed"
    assert "never answered" in cast(str, record["message"])
    # No fallback owner exists: nothing was created on behalf of a core that
    # never answered, and no local job or report stands in for it.
    assert stack.service.list_jobs() == []
    assert list((stack.workspace / "reports").iterdir()) == []

    # A read with the core still unreachable is unknown, never the cached record.
    row = _row(stack.strategies.reconcile(strategy.user_strategy_id), "li-1")
    assert row["status"] == "unknown"
    assert "could not be reached" in cast(str, row["message"])
    assert row["report_ready"] is False


def test_core_loss_mid_run_reads_as_unknown_and_restart_stays_unknown(tmp_path: Path) -> None:
    # The core's own test seam holds the spawn-to-running-state window open, so the
    # run cannot commit while the process is killed: an interruption no restart can
    # prove ended is exactly the case this reads.
    core = Core(tmp_path, args=("--test-hold-after-spawn",))
    _STARTED.append(core)
    stack = _stack(tmp_path, core.socket)
    strategy = _create(stack.strategies)
    identity = strategy.user_strategy_id
    launched = _launch(stack.strategies, identity)
    assert _row(launched, "li-1")["status"] in {"confirmed", "running"}

    core.kill()
    # With the owner gone the state is unknown — never the last state this process
    # happened to write, and never a report claim.
    row = _row(stack.strategies.reconcile(identity), "li-1")
    assert row["status"] == "unknown"
    assert row["report_ready"] is False
    assert row["facts"] is None
    assert "could not be reached" in cast(str, row["message"])

    restarted = Core(tmp_path, workspace=core.workspace)
    _STARTED.append(restarted)
    _rebind(stack, restarted.socket)
    row = _row(stack.strategies.reconcile(identity), "li-1")
    assert row["status"] == "unknown"
    assert row["report_ready"] is False
    # A later confirmation of that same intent is answered from the core too: the
    # interrupted run is not re-dispatched and no second run is created.
    replay = stack.strategies.launch_experiment(
        identity,
        launch_intent_id="li-1",
        base_revision=row["payload"]["draft_revision"],
        preview_identity=row["payload"]["preview_identity"],
        approved_budget=dict(RUN_BUDGET),
    )
    assert len(replay.experiments) == 1
    assert stack.service.list_jobs() == []


def test_restart_rebuilds_a_wiped_projection_from_the_core(tmp_path: Path) -> None:
    core = Core(tmp_path)
    _STARTED.append(core)
    stack = _stack(tmp_path, core.socket)
    strategy = _create(stack.strategies)
    identity = strategy.user_strategy_id
    _launch(stack.strategies, identity)
    settled = _wait(stack.strategies, identity)
    assert settled["status"] == "created"

    # A restart whose local projection is gone: the record survives, but every
    # projected field is wiped, exactly as an older or lost projection would be.
    projected = {
        "status",
        "message",
        "job_id",
        "attempt_id",
        "frozen_sha256",
        "phase",
        "report_ready",
        "report_sha256",
        "facts",
        "report_schema",
        "path_schema",
    }
    store = UserStrategyStore(stack.workspace)
    durable = store.get(identity)
    store.save(
        replace(
            durable,
            experiments=tuple(
                {
                    **{key: value for key, value in item.items() if key not in projected},
                    "status": "pending",
                    "job_id": None,
                    "message": None,
                }
                for item in durable.experiments
            ),
        )
    )

    row = _row(stack.strategies.reconcile(identity), "li-1")
    assert row["status"] == "created"
    assert row["job_id"] == settled["job_id"]
    assert row["attempt_id"] == settled["attempt_id"]
    assert row["report_ready"] is True
    assert row["facts"] == settled["facts"]


# --- report identity and download protection --------------------------------


def test_a_wiped_experiment_projection_is_rebuilt_from_the_owner(tmp_path: Path) -> None:
    """A workspace that lost every experiment record recovers them from the core.

    Nothing of the confirmation is remembered here: the records are reconstructed
    from the frozen bytes the owner retained, with the same intent, job, attempt,
    frozen input and report — and the run is never dispatched again.
    """
    core = Core(tmp_path)
    _STARTED.append(core)
    stack = _stack(tmp_path, core.socket)
    strategy = _create(stack.strategies)
    identity = strategy.user_strategy_id
    _launch(stack.strategies, identity)
    settled = _wait(stack.strategies, identity)
    assert settled["status"] == "created"

    # Every experiment record this workspace held is gone, and so is every field
    # of it: only the strategy's own draft and conversation remain.
    store = UserStrategyStore(stack.workspace)
    durable = store.get(identity)
    store.save(replace(durable, experiments=()))
    assert UserStrategyStore(stack.workspace).get(identity).experiments == ()

    # A restarted service over the same workspace discovers them again.
    restarted = UserStrategyService(
        UserStrategyStore(stack.workspace),
        ResearchOrchestrator(ResearchStore(stack.workspace), stack.service, _settings(tmp_path)),
        stack.service,
        _settings(tmp_path),
        quine=QuineBridge(core.socket, confirm_timeout=60.0, query_timeout=20.0),
    )
    rebuilt = restarted.reconcile(identity)
    assert len(rebuilt.experiments) == 1
    restored = rebuilt.experiments[0]
    assert restored["launch_intent_id"] == settled["launch_intent_id"]
    assert restored["request_id"] == settled["request_id"]
    assert restored["owner"] == "quine"
    assert restored["payload"] == settled["payload"]
    assert restored["payload_sha256"] == settled["payload_sha256"]
    assert restored["status"] == "created"
    assert restored["job_id"] == settled["job_id"]
    assert restored["attempt_id"] == settled["attempt_id"]
    assert restored["frozen_sha256"] == settled["frozen_sha256"]
    assert restored["report_ready"] is True
    assert restored["report_sha256"] == settled["report_sha256"]
    # The replay of the same old intent resolves to the run it already confirmed:
    # no second run is dispatched and today's draft is not bound to it.
    replay = restarted.launch_experiment(
        identity,
        launch_intent_id=cast(str, restored["launch_intent_id"]),
        base_revision=1,
        preview_identity=cast(str, restored["payload"]["preview_identity"]),
        approved_budget=dict(RUN_BUDGET),
    )
    assert len(replay.experiments) == 1
    assert replay.experiments[0]["job_id"] == settled["job_id"]
    # The confirmation the owner holds is still exactly one, with its own frozen
    # input: nothing re-froze today's draft under the old intent.
    assert [item["intent_id"] for item in QuineBridge(core.socket).experiments()] == ["li-1"]
    assert stack.service.list_jobs() == []


def test_a_wiped_projection_never_binds_todays_altered_draft(tmp_path: Path) -> None:
    """A lost record cannot make an old intent run a draft that has moved on."""
    core = Core(tmp_path)
    _STARTED.append(core)
    stack = _stack(tmp_path, core.socket)
    strategy = _create(stack.strategies)
    identity = strategy.user_strategy_id
    _launch(stack.strategies, identity)
    settled = _wait(stack.strategies, identity)
    assert settled["status"] == "created"
    assert settled["payload"]["parameters"] != {"target_quantity": "3", "entry_delay_bars": 0}

    store = UserStrategyStore(stack.workspace)
    durable = store.get(identity)
    # The record is gone and the draft moves on under the same revision history.
    assert isinstance(durable.draft, StrategyDraft)
    altered = replace(
        durable,
        experiments=(),
        draft=replace(
            durable.draft,
            revision=2,
            parameters={"target_quantity": "3", "entry_delay_bars": 0},
        ),
    )
    store.save(replace(altered, experiments=()))

    # The same intent with the same confirmation it was made of resolves to the run
    # it already confirmed: nothing is dispatched again and nothing is re-frozen.
    replay = stack.strategies.launch_experiment(
        identity,
        launch_intent_id="li-1",
        base_revision=1,
        preview_identity=cast(str, settled["payload"]["preview_identity"]),
        approved_budget=dict(RUN_BUDGET),
    )
    assert replay.experiments[0]["job_id"] == settled["job_id"]
    assert replay.experiments[0]["payload"]["parameters"] == settled["payload"]["parameters"]

    # The same intent carrying today's altered draft is refused as a conflict: an
    # intent that is already bound never becomes a second run of another input.
    today = stack.strategies.preview_experiment(identity)
    assert today["draft_revision"] == 2
    with pytest.raises(UserStrategyConflictError):
        stack.strategies.launch_experiment(
            identity,
            launch_intent_id="li-1",
            base_revision=2,
            preview_identity=today["preview_identity"],
            approved_budget=dict(RUN_BUDGET),
        )

    # The owner still holds exactly the one confirmation it always held, with the
    # frozen input it froze then.
    held = QuineBridge(core.socket).experiments()
    assert [item["intent_id"] for item in held] == ["li-1"]
    assert held[0]["frozen_sha256"] == settled["frozen_sha256"]
    assert stack.service.list_jobs() == []


def test_report_is_refused_until_the_core_registers_a_success(tmp_path: Path) -> None:
    # Held before the running state: the run exists in the core and nothing is
    # registered, so the existing report read has no verified report to serve.
    core = Core(tmp_path, args=("--test-hold-after-spawn",))
    _STARTED.append(core)
    stack = _stack(tmp_path, core.socket)
    strategy = _create(stack.strategies)
    identity = strategy.user_strategy_id
    launched = _launch(stack.strategies, identity)
    job_id = cast(str, _row(launched, "li-1")["job_id"])
    assert stack.service.get_job(job_id).status in {"running", "accepted"}

    with pytest.raises(ReportUnavailableError):
        stack.service.report(job_id)
    with pytest.raises(ReportUnavailableError):
        stack.service.artifact(job_id, "equity-path.json")
    # The run's own document says plainly that no result exists yet.
    assert stack.service.get_job(job_id).document()["report_ready"] is False


def test_report_requires_the_exact_frozen_input_that_was_confirmed(tmp_path: Path) -> None:
    core = Core(tmp_path)
    _STARTED.append(core)
    stack = _stack(tmp_path, core.socket)
    strategy = _create(stack.strategies)
    identity = strategy.user_strategy_id
    _launch(stack.strategies, identity)
    settled = _wait(stack.strategies, identity)
    assert settled["status"] == "created"

    # A projection whose retained confirmation is not the frozen input the owner
    # holds is not presented as that run's result: it reads as unknown here, while
    # the run itself stays readable through its owner's own record.
    store = UserStrategyStore(stack.workspace)
    durable = store.get(identity)
    store.save(
        replace(
            durable,
            experiments=tuple(
                {**item, "payload": {**item["payload"], "input_sha256": "0" * 64}}
                for item in durable.experiments
            ),
        )
    )
    assert _row(stack.strategies.reconcile(identity), "li-1")["status"] == "unknown"
    assert "does not reproduce the frozen input" in cast(
        str, _row(stack.strategies.reconcile(identity), "li-1")["message"]
    )


def test_altered_official_bytes_are_never_served(tmp_path: Path) -> None:
    core = Core(tmp_path)
    _STARTED.append(core)
    stack = _stack(tmp_path, core.socket)
    strategy = _create(stack.strategies)
    identity = strategy.user_strategy_id
    _launch(stack.strategies, identity)
    settled = _wait(stack.strategies, identity)
    assert settled["status"] == "created"
    job_id = cast(str, settled["job_id"])
    assert stack.service.report(job_id)

    # The published bytes are held to the digest the owner registered for them, so
    # an edited artifact is refused rather than served as that run's report.
    official = core.workspace / "official" / job_id / "report.json"
    payload = bytearray(official.read_bytes())
    payload[-2] ^= 0x01
    official.write_bytes(bytes(payload))
    with pytest.raises(ReportUnavailableError):
        stack.service.report(job_id)
    with pytest.raises(ReportUnavailableError):
        stack.service.artifact(job_id, "summary.txt")


# --- the HTTP surface --------------------------------------------------------


def _web_settings(tmp_path: Path, core: Core) -> WebSettings:
    ui = tmp_path / "ui"
    ui.mkdir(exist_ok=True)
    (ui / "index.html").write_text("<!doctype html><title>EA Web</title>", encoding="utf-8")
    return WebSettings(
        scenario_root=_scenario_root(tmp_path),
        workspace=tmp_path / "web-workspace",
        ui_dir=ui,
        port=8765,
        quine_socket=core.socket,
    )


def _create_via_http(client: TestClient) -> str:
    created = client.post(
        "/api/user-strategies",
        headers=WRITE_HEADERS,
        json={
            "name": "Long AAPL",
            "intent": "Test one registered entry on the fixture data",
            "scenario_id": SCENARIO,
            "criterion": {"field": "economics.net_pnl.amount", "expectation": "positive"},
        },
    )
    assert created.status_code == 201, created.text
    return cast(str, created.json()["user_strategy_id"])


def test_http_stop_and_official_artifact_reads(tmp_path: Path) -> None:
    core = Core(tmp_path)
    _STARTED.append(core)
    app = create_app(_web_settings(tmp_path, core))
    with TestClient(app, base_url=ORIGIN) as client:
        identity = _create_via_http(client)
        preview = client.get(f"/api/user-strategies/{identity}/experiment/preview")
        assert preview.status_code == 200, preview.text
        facts = preview.json()
        # The preview says which owner a confirmation would reach.
        assert facts["owner"] == "quine"

        launched = client.post(
            f"/api/user-strategies/{identity}/experiments",
            headers=WRITE_HEADERS,
            json={
                "launch_intent_id": "li-1",
                "base_revision": facts["draft_revision"],
                "preview_identity": facts["preview_identity"],
                "approved_budget": dict(RUN_BUDGET),
            },
        )
        assert launched.status_code == 200, launched.text

        row = _await_http_row(client, identity)

        # A stop the core cannot honour because the run already committed is the
        # core's own refusal, carried through with the core's own code.
        late = client.post(
            f"/api/user-strategies/{identity}/experiments/li-1/cancel", headers=WRITE_HEADERS
        )
        assert late.status_code == 422, late.text
        assert late.json()["error"]["code"] == "late_cancel_refused"
        assert row["status"] == "created"


def _await_http_row(client: TestClient, identity: str) -> dict[str, Any]:
    """Wait for one strategy's latest experiment to leave its live states."""
    deadline = time.monotonic() + 180
    row: dict[str, Any] = {}
    while time.monotonic() < deadline:
        read = client.get(f"/api/user-strategies/{identity}")
        assert read.status_code == 200, read.text
        row = read.json()["experiments"][-1]
        if row["status"] in {"created", "failed", "cancelled", "unknown"}:
            return row
        time.sleep(0.2)
    raise AssertionError(f"experiment never settled: {row}")


def test_existing_backtest_views_read_a_core_registered_success(tmp_path: Path) -> None:
    """The mature run, report, path and analytics reads serve the owner's own run.

    Nothing here is a second report implementation: the same endpoints the
    workspace has always served answer for a run the core registered, and they are
    held to the identities and digests the core registered for it.
    """
    core = Core(tmp_path)
    _STARTED.append(core)
    settings = _web_settings(tmp_path, core)
    app = create_app(settings)
    with TestClient(app, base_url=ORIGIN) as client:
        identity = _create_via_http(client)
        preview = client.get(f"/api/user-strategies/{identity}/experiment/preview").json()
        launched = client.post(
            f"/api/user-strategies/{identity}/experiments",
            headers=WRITE_HEADERS,
            json={
                "launch_intent_id": "li-1",
                "base_revision": preview["draft_revision"],
                "preview_identity": preview["preview_identity"],
                "approved_budget": dict(RUN_BUDGET),
            },
        )
        assert launched.status_code == 200, launched.text
        row = _await_http_row(client, identity)
        assert row["status"] == "created"
        job_id = cast(str, row["job_id"])

        job = client.get(f"/api/backtests/{job_id}")
        assert job.status_code == 200, job.text
        document = job.json()
        assert document["job_id"] == job_id
        assert document["status"] == "succeeded"
        assert document["report_ready"] is True
        # The admitted input the confirmation froze is what these views read, and
        # the engine run id is the owner's own attempt.
        assert document["engine_run_id"] == row["attempt_id"]
        assert document["input_sha256"] == row["payload"]["input_sha256"]
        assert document["input_snapshot"]["scenario_id"] == SCENARIO

        report = client.get(f"/api/backtests/{job_id}/report")
        assert report.status_code == 200, report.text
        assert sha256(report.content).hexdigest() == document["report_sha256"]
        assert report.json()["run_id"] == row["attempt_id"]

        artifact = client.get(f"/api/backtests/{job_id}/artifacts/equity-path.json")
        assert artifact.status_code == 200, artifact.text
        assert sha256(artifact.content).hexdigest() == document["equity_path_sha256"]
        summary = client.get(f"/api/backtests/{job_id}/artifacts/summary.txt")
        assert summary.status_code == 200, summary.text
        assert sha256(summary.content).hexdigest() == document["summary_sha256"]

        analytics = client.get(f"/api/backtests/{job_id}/trade-analytics")
        # One analytics authority, not a second one for owner-registered runs: the
        # same scenario run by this workspace answers the same way, whether its
        # report version carries complete trades or not.
        local_id = _run_local(client)
        local = _wait_http_job(client, local_id)
        assert local["status"] == "succeeded" and local["report_ready"] is True
        local_analytics = client.get(f"/api/backtests/{local_id}/trade-analytics")
        assert analytics.status_code == local_analytics.status_code
        assert analytics.status_code in {200, 409}, analytics.text

        # A run the workspace never stored is not in its own index: the owner keeps
        # the run, and only the run this workspace executed here is published.
        listed = [item["job_id"] for item in client.get("/api/backtests").json()["jobs"]]
        assert listed == [local_id]
        assert {path.stem for path in (settings.workspace / "jobs").iterdir()} == {local_id}
        assert job_id not in listed


def _run_local(client: TestClient) -> str:
    """One run of the same scenario this workspace executes itself."""
    validated = client.post(f"/api/scenarios/{SCENARIO}/validate", headers=WRITE_HEADERS)
    assert validated.status_code == 200, validated.text
    accepted = client.post(
        "/api/backtests",
        headers=WRITE_HEADERS,
        json={
            "scenario_id": SCENARIO,
            "input_identity": validated.json()["input_identity"],
            "request_id": "request-local-0001",
        },
    )
    assert accepted.status_code == 202, accepted.text
    return cast(str, accepted.json()["job_id"])


def _wait_http_job(client: TestClient, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    document: dict[str, Any] = {}
    while time.monotonic() < deadline:
        read = client.get(f"/api/backtests/{job_id}")
        assert read.status_code == 200, read.text
        document = read.json()
        if document["status"] not in {"accepted", "running", "stopping"}:
            return document
        time.sleep(0.1)
    raise AssertionError(f"local job never settled: {document}")


def test_two_owner_registered_runs_compare_through_the_existing_views(tmp_path: Path) -> None:
    """Two core-owned successes are comparable by the existing comparison reads."""
    core = Core(tmp_path)
    _STARTED.append(core)
    app = create_app(_web_settings(tmp_path, core))
    with TestClient(app, base_url=ORIGIN) as client:
        identity = _create_via_http(client)
        jobs: list[str] = []
        for intent in ("li-1", "li-2"):
            preview = client.get(f"/api/user-strategies/{identity}/experiment/preview").json()
            launched = client.post(
                f"/api/user-strategies/{identity}/experiments",
                headers=WRITE_HEADERS,
                json={
                    "launch_intent_id": intent,
                    "base_revision": preview["draft_revision"],
                    "preview_identity": preview["preview_identity"],
                    "approved_budget": dict(RUN_BUDGET),
                },
            )
            assert launched.status_code == 200, launched.text
            row = _await_http_row(client, identity)
            assert row["status"] == "created"
            jobs.append(cast(str, row["job_id"]))

        assert jobs[0] != jobs[1]
        statuses: list[int] = []
        for job_id in jobs:
            assert client.get(f"/api/backtests/{job_id}").json()["report_ready"] is True
            assert client.get(f"/api/backtests/{job_id}/report").status_code == 200
            assert (
                client.get(f"/api/backtests/{job_id}/artifacts/equity-path.json").status_code == 200
            )
            statuses.append(client.get(f"/api/backtests/{job_id}/trade-analytics").status_code)
        # Both runs are read by the same authority, so they answer alike.
        assert statuses[0] == statuses[1]
        assert statuses[0] in {200, 409}


def _row_in(document: dict[str, Any], intent: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        next(item for item in document["experiments"] if item["launch_intent_id"] == intent),
    )


def test_http_cancel_before_confirm_reaches_the_core_barrier(tmp_path: Path) -> None:
    """A stop for a stable intent arrives before its confirmation, through the API."""
    core = Core(tmp_path)
    _STARTED.append(core)
    app = create_app(_web_settings(tmp_path, core))
    with TestClient(app, base_url=ORIGIN) as client:
        identity = _create_via_http(client)
        preview = client.get(f"/api/user-strategies/{identity}/experiment/preview").json()

        # No experiment record exists here yet: the stop is still forwarded, because
        # the barrier that refuses a late confirmation is the core's own.
        stopped = client.post(
            f"/api/user-strategies/{identity}/experiments/li-1/cancel", headers=WRITE_HEADERS
        )
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["experiments"] == []

        launched = client.post(
            f"/api/user-strategies/{identity}/experiments",
            headers=WRITE_HEADERS,
            json={
                "launch_intent_id": "li-1",
                "base_revision": preview["draft_revision"],
                "preview_identity": preview["preview_identity"],
                "approved_budget": dict(RUN_BUDGET),
            },
        )
        assert launched.status_code == 200, launched.text
        record = _row_in(launched.json(), "li-1")
        # The core refused the confirmation it had already been told to cancel: no
        # run exists, and what this workspace records is that refusal.
        assert record["status"] == "cancelled"
        assert record["job_id"] is None
        assert "cancelled before it was confirmed" in cast(str, record["message"])
        assert client.get("/api/backtests").json()["jobs"] == []


def test_http_never_answers_from_a_cache_when_the_core_is_gone(tmp_path: Path) -> None:
    core = Core(tmp_path)
    _STARTED.append(core)
    settings = _web_settings(tmp_path, core)
    app = create_app(settings)
    with TestClient(app, base_url=ORIGIN) as client:
        # A confirmation whose core never answered at all: no local run, no local
        # success and no local failure is invented for it.
        identity = _create_via_http(client)
        preview = client.get(f"/api/user-strategies/{identity}/experiment/preview")
        assert preview.status_code == 200, preview.text
        facts = preview.json()
        core.kill()
        launched = client.post(
            f"/api/user-strategies/{identity}/experiments",
            headers=WRITE_HEADERS,
            json={
                "launch_intent_id": "li-9",
                "base_revision": facts["draft_revision"],
                "preview_identity": facts["preview_identity"],
                "approved_budget": dict(RUN_BUDGET),
            },
        )
        assert launched.status_code == 200, launched.text
        assert launched.json()["experiments"][-1]["status"] == "unconfirmed"
        assert not any((settings.workspace / "jobs").iterdir())


def _registered_proposal(tools: list[Any]) -> tuple[str, str, object] | None:
    """One registered parameter the host offered, and one step past its value.

    The tool schema carries the host's own bound path — one enum per changeable
    field — and its description states the current value in the host's own type, so
    one step past that value is a typed change the host's own validation accepts.
    """
    for tool in tools:
        try:
            branches = tool.parameters["properties"]["items"]["items"]["oneOf"]
        except (KeyError, TypeError):
            continue
        for branch in branches:
            try:
                path = str(branch["properties"]["path"]["enum"][0])
                kind = branch["properties"]["value"].get("type")
            except (KeyError, IndexError, TypeError):
                continue
            if not path.startswith("parameters."):
                continue
            match = re.search(
                r'current value ("?)(\d+(?:\.\d+)?)\1', str(branch.get("description", ""))
            )
            if match is None:
                continue
            text = match.group(2)
            if "." in text:
                whole, fraction = text.split(".", 1)
                value: object = f"{int(whole) + 1}.{fraction}"
            else:
                value = int(text) + 1 if kind == "integer" else str(int(text) + 1)
            return str(tool.name), path, value
    return None


def _scripted_propose_port() -> Any:
    """One offline, deterministic model port that answers a typed proposal.

    It reads the schema the host offered it and proposes one change to a registered
    parameter. It reaches no network, invents no run and cannot produce a report or
    a result: those come from the engine through the core.
    """
    from ea.web.llm import ModelCapabilities, ModelResponse, ModelRoute, ToolCall

    route = ModelRoute(
        provider="scripted",
        protocol="scripted-messages",
        model="scripted-model",
        endpoint="https://scripted.invalid/v1/messages",
        capabilities=ModelCapabilities(tool_use=True, structured_output=False, usage=True),
    )

    class ScriptedProvider:
        def route(self) -> ModelRoute:
            return route

        def complete(
            self,
            *,
            system: str,
            messages: list[Any],
            tools: list[Any] | None = None,
            max_tokens: int = 2000,
            required_capabilities: tuple[str, ...] = (),
        ) -> ModelResponse:
            del system, messages, max_tokens, required_capabilities
            proposal = _registered_proposal(list(tools or []))
            if proposal is None:
                return ModelResponse(
                    text="scripted provider: no registered parameter was offered this turn.",
                    tool_calls=(),
                    stop_reason="stop",
                    model="scripted-model",
                    input_tokens=11,
                    output_tokens=7,
                )
            name, path, value = proposal
            return ModelResponse(
                text=f"scripted provider: one typed change against {path}.",
                tool_calls=(
                    ToolCall(
                        id="scripted-call-1",
                        name=name,
                        arguments={
                            "items": [
                                {
                                    "path": path,
                                    "value": value,
                                    "reason": (
                                        "scripted provider: raise this registered parameter by "
                                        "one step."
                                    ),
                                }
                            ]
                        },
                    ),
                ),
                stop_reason="tool_calls",
                model="scripted-model",
                input_tokens=11,
                output_tokens=7,
            )

    return ScriptedProvider()


def test_scripted_provider_drives_apply_confirm_and_the_real_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One offline scripted model port, driven through the app to a real report.

    The provider above is the only double: it answers one typed ``propose_change``
    for a registered parameter. The user applies it, confirms the new draft, and
    the real core runs the real engine — twice, so both runs are readable by the
    existing report and comparison views. It is driven through the HTTP contract
    in-process: no server, no browser and no network.
    """
    monkeypatch.setattr(
        "ea.web.user_strategies.create_model_port",
        lambda _settings: _scripted_propose_port(),
    )
    core = Core(tmp_path)
    _STARTED.append(core)
    app = create_app(_web_settings(tmp_path, core))
    with TestClient(app, base_url=ORIGIN) as client:
        identity = _create_via_http(client)
        asked = client.post(
            f"/api/user-strategies/{identity}/messages",
            headers=WRITE_HEADERS,
            json={
                "call_id": "scripted-call-1",
                "base_revision": 1,
                "text": "Propose the next change to this draft.",
                "approved_budget": {"max_model_calls": 1, "max_backtests": 0},
            },
        )
        assert asked.status_code == 200, asked.text
        proposal = asked.json()["proposals"][-1]
        assert proposal["status"] == "open"
        assert len(proposal["items"]) == 1
        # The scripted provider states one typed change against a registered
        # parameter, derived from the schema the host offered it.
        item = proposal["items"][0]
        assert str(item["path"]).startswith("parameters.")

        applied = client.post(
            f"/api/user-strategies/{identity}/proposals/{proposal['proposal_id']}/apply",
            headers=WRITE_HEADERS,
            json={"operation_id": "scripted-apply-1", "paths": None},
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["draft"]["revision"] == 2
        name = str(item["path"]).split(".", 1)[1]
        assert applied.json()["draft"]["parameters"][name] == item["after"]

        jobs: list[str] = []
        for intent in ("li-1", "li-2"):
            preview = client.get(f"/api/user-strategies/{identity}/experiment/preview").json()
            launched = client.post(
                f"/api/user-strategies/{identity}/experiments",
                headers=WRITE_HEADERS,
                json={
                    "launch_intent_id": intent,
                    "base_revision": preview["draft_revision"],
                    "preview_identity": preview["preview_identity"],
                    "approved_budget": dict(RUN_BUDGET),
                },
            )
            assert launched.status_code == 200, launched.text
            row = _await_http_row(client, identity)
            assert row["status"] == "created", row["message"]
            jobs.append(cast(str, row["job_id"]))

        # Both runs are real engine attempts of this draft, and both are read by
        # the existing report and comparison views.
        assert jobs[0] != jobs[1]
        for job_id in jobs:
            document = client.get(f"/api/backtests/{job_id}").json()
            assert document["status"] == "succeeded" and document["report_ready"] is True
            report = client.get(f"/api/backtests/{job_id}/report")
            assert report.status_code == 200, report.text
            assert report.json()["run_id"] == document["engine_run_id"]


def test_http_never_serves_a_cached_result_when_the_owner_is_killed(tmp_path: Path) -> None:
    core = Core(tmp_path)
    _STARTED.append(core)
    app = create_app(_web_settings(tmp_path, core))
    with TestClient(app, base_url=ORIGIN) as client:
        identity = _create_via_http(client)
        preview = client.get(f"/api/user-strategies/{identity}/experiment/preview").json()
        launched = client.post(
            f"/api/user-strategies/{identity}/experiments",
            headers=WRITE_HEADERS,
            json={
                "launch_intent_id": "li-1",
                "base_revision": preview["draft_revision"],
                "preview_identity": preview["preview_identity"],
                "approved_budget": dict(RUN_BUDGET),
            },
        )
        assert launched.status_code == 200, launched.text
        row = _await_http_row(client, identity)
        assert row["status"] == "created"
        job_id = cast(str, row["job_id"])
        assert client.get(f"/api/backtests/{job_id}").status_code == 200

        # The owner is gone. Everything that was readable a moment ago is now
        # unanswered rather than served from this process's last reading: no cached
        # terminal, no cached report, and no fallback to another owner.
        core.kill()
        assert client.get(f"/api/backtests/{job_id}").status_code == 503
        report = client.get(f"/api/backtests/{job_id}/report")
        assert report.status_code == 503, report.text
        assert report.json()["error"]["code"] == "quine_unavailable"
        assert client.get(f"/api/backtests/{job_id}/artifacts/summary.txt").status_code == 503
        read = client.get(f"/api/user-strategies/{identity}")
        assert read.status_code == 200, read.text
        settled = read.json()["experiments"][-1]
        assert settled["status"] == "unknown"
        assert settled["report_ready"] is False
        assert settled["facts"] is None
        assert "could not be reached" in cast(str, settled["message"])


# A chronological holdout over a core-owned run is no longer refused: it is
# admitted by the owner itself, and its integration tests live in
# ``unit.test_web_rust_holdout``.


# --- one explicitly selected, uploaded dataset -------------------------------


def _publish_upload(service: WebService, rows: int) -> tuple[str, bytes]:
    """Publish one synthetic strict-profile capture, and name what was published."""
    from ea.web.uploads import AVAILABILITY_PROVIDED
    from unit.test_web_uploads import _capture

    content = _capture(rows)
    manifest = service.uploads.publish(content, availability_policy=AVAILABILITY_PROVIDED)
    return str(manifest["dataset_id"]), content


def _catalog_binding(service: WebService, tmp_path: Path, dataset_id: str) -> dict[str, Any]:
    """The exact capability binding this host's own catalog resolves for one dataset."""
    research = ResearchOrchestrator(
        ResearchStore(tmp_path / "workspace"), service, _settings(tmp_path)
    )
    binding = research.catalog_binding(dataset_id=dataset_id)
    assert binding is not None
    return binding.document()


def _upload_snapshot(
    service: WebService, tmp_path: Path, dataset_id: str, *, binding: dict[str, Any] | None = None
) -> dict[str, Any]:
    from ea.web import rust_domain

    attached = _catalog_binding(service, tmp_path, dataset_id) if binding is None else binding
    return rust_domain.run(
        "snapshot",
        {
            "scenario_path": str(service.registered_scenario_file(SCENARIO)),
            "dataset_id": dataset_id,
            "workspace_root": str(service.uploads.root.parent),
            "binding": attached,
        },
    )


def test_an_uploaded_dataset_is_bound_to_its_published_version_and_captured_privately(
    tmp_path: Path,
) -> None:
    """The confirmation names a released version, and the run freezes its own copy."""
    from ea.web import rust_domain
    from unit.test_web_uploads import _capture

    service = WebService(_scenario_root(tmp_path), tmp_path / "workspace")
    service.start()
    _STARTED.append(service)
    dataset_id, content = _publish_upload(service, 8)

    snapshot = _upload_snapshot(service, tmp_path, dataset_id)
    assert snapshot["status"] == "ok", snapshot
    envelope = snapshot["result"]["frozen"]
    # The published version is bound into the frozen input, and it is the version
    # whose bytes the confirmation admitted rather than a claim carried alongside.
    version = envelope["dataset_version"]
    assert version["dataset_id"] == dataset_id
    assert version["capture_sha256"] == envelope["research_input"]["source_sha256"]
    assert envelope["plugin_binding"]["components"][0]["plugin_id"] == (
        f"ea.builtin.data.{dataset_id}"
    )

    target = tmp_path / "capture.data"
    frozen = rust_domain.run("freeze", {"frozen": envelope, "capture_path": str(target)})
    assert frozen["status"] == "ok", frozen
    capture = frozen["result"]["data_capture"]
    assert Path(capture["path"]) == target
    assert target.read_bytes() == content
    # What is materialized reads from the private snapshot, not from the store.
    materialized = json.loads(bytes.fromhex(frozen["result"]["scenario_file"]))
    assert materialized["data"]["path"] == str(target)
    assert (
        frozen["result"]["scenario_file_sha256"]
        == sha256(bytes.fromhex(frozen["result"]["scenario_file"])).hexdigest()
    )

    # A binding that names another dataset's capability is refused whole, and a
    # selection that carries no released version is refused rather than guessed.
    other_id, _content = _publish_upload(service, 4)
    mismatched = _upload_snapshot(
        service, tmp_path, dataset_id, binding=_catalog_binding(service, tmp_path, other_id)
    )
    assert mismatched["status"] == "refused" and mismatched["code"] == "binding_invalid"
    unbound = rust_domain.run(
        "snapshot",
        {
            "scenario_path": str(service.registered_scenario_file(SCENARIO)),
            "dataset_id": dataset_id,
            "workspace_root": str(service.uploads.root.parent),
        },
    )
    assert unbound["status"] == "refused" and unbound["code"] == "binding_invalid"

    # A retained version whose digest no longer reproduces is refused rather than
    # read as the run's input: the binding is verified, not carried on trust.
    tampered = dict(envelope)
    tampered["dataset_version_sha256"] = "0" * 64
    corrupt = rust_domain.run(
        "freeze", {"frozen": tampered, "capture_path": str(tmp_path / "fourth.data")}
    )
    assert corrupt["status"] == "refused" and corrupt["code"] == "digest_mismatch"
    assert not (tmp_path / "fourth.data").exists()

    # A source that is not the confirmed one is refused before anything is
    # materialized. Changed bytes inside the admitted window are refused outright.
    source_path, _dataset = service.uploads.load(dataset_id)
    source_path.write_bytes(content.replace(b"100.5", b"101.5"))
    drift = rust_domain.run(
        "freeze", {"frozen": envelope, "capture_path": str(tmp_path / "second.data")}
    )
    assert drift["status"] == "refused"
    assert not (tmp_path / "second.data").exists()
    # And so is a source whose *selected fingerprint* is unchanged: one extra bar
    # outside the replay window leaves the selected bars identical while the bytes
    # are no longer the confirmed ones, and the admitted evidence names the bytes.
    source_path.write_bytes(_capture(9))
    later = rust_domain.run(
        "freeze", {"frozen": envelope, "capture_path": str(tmp_path / "third.data")}
    )
    assert later["status"] == "refused"
    assert later["code"] in {"identity_mismatch", "input_changed"}
    assert not (tmp_path / "third.data").exists()


def test_a_registered_input_needs_no_version_and_still_resolves(tmp_path: Path) -> None:
    """A plain registered scenario, and a registered dataset, are unchanged."""
    from ea.web import rust_domain
    from unit.test_web_uploads import _capture

    service = WebService(_scenario_root(tmp_path), tmp_path / "workspace")
    service.start()
    _STARTED.append(service)

    # The scenario's own data, with nothing selected: no version, no capability
    # binding, no research input — exactly the input this path always admitted.
    plain = rust_domain.run(
        "snapshot", {"scenario_path": str(service.registered_scenario_file(SCENARIO))}
    )
    assert plain["status"] == "ok", plain
    envelope = plain["result"]["frozen"]
    assert envelope["dataset_version"] is None
    assert envelope["dataset_version_sha256"] is None
    assert envelope["plugin_binding"] is None
    assert envelope["research_input"] is None

    # One explicitly selected registered dataset resolves through a data root
    # alone — this workspace has no upload store in that request — and it carries
    # evidence without claiming a released version it never published.
    data_root = tmp_path / "registered"
    data_root.mkdir()
    (data_root / "full.csv").write_bytes(_capture(6))
    selected = rust_domain.run(
        "snapshot",
        {
            "scenario_path": str(service.registered_scenario_file(SCENARIO)),
            "dataset_id": "full.csv",
            "data_root": str(data_root),
        },
    )
    assert selected["status"] == "ok", selected
    registered = selected["result"]["frozen"]
    assert registered["dataset_version"] is None
    assert registered["plugin_binding"] is None
    assert registered["research_input"]["dataset_id"] == "full.csv"


def test_freeze_refuses_a_published_version_that_changed_after_confirmation(
    tmp_path: Path,
) -> None:
    """A version edit between admission and materialization never becomes a run."""
    from ea.web import rust_domain

    service = WebService(_scenario_root(tmp_path), tmp_path / "workspace")
    service.start()
    _STARTED.append(service)
    dataset_id, _content = _publish_upload(service, 8)
    snapshot = _upload_snapshot(service, tmp_path, dataset_id)
    assert snapshot["status"] == "ok", snapshot

    # The published version's own declaration is edited: the store itself refuses to
    # re-derive that version's evidence any more.
    version = service.uploads.root / "versions" / f"{dataset_id}.json"
    changed = json.loads(version.read_bytes())
    changed["source"]["reference"] = "changed between admission and materialization"
    version.write_text(json.dumps(changed))
    with pytest.raises(ValueError):
        service.uploads.version_evidence(dataset_id)

    target = tmp_path / "after-drift.data"
    frozen = rust_domain.run(
        "freeze",
        {"frozen": snapshot["result"]["frozen"], "capture_path": str(target)},
    )
    assert frozen["status"] == "refused", frozen
    assert not target.exists()


def test_a_confirmed_experiment_runs_from_an_uploaded_dataset_and_outlives_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One uploaded selection, confirmed through the core, outliving its own upload."""
    core = Core(tmp_path)
    _STARTED.append(core)
    stack = _stack(tmp_path, core.socket)
    counts = _no_python_owner(monkeypatch)
    service = stack.service
    dataset_id, content = _publish_upload(service, 8)

    strategy = _create(stack.strategies)
    identity = strategy.user_strategy_id
    selected = stack.strategies.select_dataset(
        identity, operation_id="select-1", base_revision=1, dataset_id=dataset_id
    )
    assert selected.draft.revision == 2

    launched = _launch(stack.strategies, identity)
    assert _row(launched, "li-1")["status"] in {"confirmed", "running", "created"}
    settled = _wait(stack.strategies, identity)
    assert settled["status"] == "created", settled["message"]
    assert settled["report_ready"] is True
    assert counts == {"children": 0, "owners": 0}

    job_id = cast(str, settled["job_id"])
    projected = service.get_job(job_id)
    assert projected.status == "succeeded" and projected.report_sha256
    # The core ran from its own private snapshot of the confirmed bytes, not from
    # the uploaded store, and that snapshot is exactly what was published.
    capture = core.workspace / "inputs" / f"{job_id}.data"
    assert capture.is_file() and capture.read_bytes() == content

    # The published upload is removed whole. The frozen experiment, its official
    # report and its admitted identities are unchanged: nothing about this run was
    # ever read from the upload again.
    before = service.artifact(job_id, "report.json")
    for name in ("raw", "captures", "versions"):
        for path in (service.uploads.root / name).iterdir():
            path.unlink()
    assert service.get_job(job_id).status == "succeeded"
    assert service.artifact(job_id, "report.json") == before
    assert _row(stack.strategies.reconcile(identity), "li-1")["status"] == "created"


def test_trusted_local_package_runs_from_capture_and_reopens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ea.web.local_packages import LOCAL_SCENARIO_ID
    from unit.test_web_local_package_integration import _grant_request, _package

    core = Core(tmp_path)
    _STARTED.append(core)
    stack = _stack(tmp_path, core.socket)
    counts = _no_python_owner(monkeypatch)
    sentinel = tmp_path / "strategy-executed"
    payload = _package(sentinel)
    stack.service.register_local_strategy(**_grant_request(payload))
    assert not sentinel.exists()
    strategy = stack.strategies.create(
        name="Trusted capture",
        intent="Run the exact trusted package",
        scenario_id=LOCAL_SCENARIO_ID,
    )
    _launch(stack.strategies, strategy.user_strategy_id)
    settled = _wait(stack.strategies, strategy.user_strategy_id)
    assert settled["status"] == "created", settled.get("message")
    assert sentinel.exists()
    assert counts == {"children": 0, "owners": 0}
    job_id = settled["job_id"]
    captured = core.workspace / "inputs" / f"{job_id}.eastrategy"
    assert captured.read_bytes() == payload
    assert captured.stat().st_mode & 0o222 == 0
    report = stack.service.report(job_id)
    assert sha256(payload).hexdigest().encode() in report
    stack.service.stop()
    sentinel.unlink()
    reopened = _stack(tmp_path, core.socket)
    assert reopened.service.report(job_id) == report
    assert not sentinel.exists()  # Reopening history inspects bytes without importing Python.


@pytest.mark.parametrize("change", ["revoke", "replace", "reregister"])
def test_local_package_trust_drift_refuses_confirmation_without_execution(
    tmp_path: Path, change: str
) -> None:
    from ea.web.local_packages import LOCAL_SCENARIO_ID
    from unit.test_web_local_package_integration import _grant_request, _package

    core = Core(tmp_path)
    _STARTED.append(core)
    stack = _stack(tmp_path, core.socket)
    sentinel = tmp_path / "strategy-executed"
    stack.service.register_local_strategy(**_grant_request(_package(sentinel)))
    strategy = stack.strategies.create(
        name="Trust drift", intent="Refuse changed trust", scenario_id=LOCAL_SCENARIO_ID
    )
    preview = stack.strategies.preview_experiment(strategy.user_strategy_id)
    sentinel.unlink(missing_ok=True)
    if change == "revoke":
        stack.service.revoke_local_strategy()
    elif change == "replace":
        stack.service.local_strategy.root.joinpath("package.eastrategy").write_bytes(
            _package(tmp_path / "replacement-executed")
        )
    else:
        stack.service.register_local_strategy(
            **_grant_request(_package(tmp_path / "replacement-executed"))
        )
    with pytest.raises(ValueError):
        stack.strategies.launch_experiment(
            strategy.user_strategy_id,
            launch_intent_id="drift",
            base_revision=preview["draft_revision"],
            preview_identity=preview["preview_identity"],
            approved_budget=dict(RUN_BUDGET),
        )
    assert not sentinel.exists()
    assert not (tmp_path / "replacement-executed").exists()
    assert list((core.workspace / "inputs").glob("*.eastrategy")) == []


@pytest.mark.parametrize("boundary", ["freeze", "worker"])
@pytest.mark.parametrize("change", ["revoke", "replace"])
def test_frozen_local_package_rechecks_trust_before_loading_code(
    tmp_path: Path, boundary: str, change: str
) -> None:
    from ea.plugin.manifest import ManifestError
    from ea.web import rust_domain, worker
    from ea.web.local_packages import LOCAL_SCENARIO_ID
    from unit.test_web_local_package_integration import _grant_request, _package

    # No core is needed for this precisely controlled pre-load interleaving.
    stack = _stack(tmp_path, tmp_path / "absent.sock")
    sentinel = tmp_path / "strategy-executed"
    package = _package(sentinel)
    stack.service.register_local_strategy(**_grant_request(package))
    reply = rust_domain.run(
        "snapshot",
        {
            "scenario_path": str(stack.service.registered_scenario_file(LOCAL_SCENARIO_ID)),
            "local_strategy_workspace": str(stack.workspace),
            "binding": stack.service.local_strategy_binding(),
        },
    )
    assert reply["status"] == "ok", reply
    frozen = reply["result"]["frozen"]
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_bytes(bytes.fromhex(frozen["scenario_file"]))
    package_path = tmp_path / "frozen.eastrategy"
    package_path.write_bytes(package)
    sentinel.unlink(missing_ok=True)
    if change == "revoke":
        stack.service.revoke_local_strategy()
    else:
        # Even another explicitly trusted package cannot upgrade a frozen binding.
        stack.service.register_local_strategy(
            **_grant_request(_package(tmp_path / "replacement-executed"))
        )
    if boundary == "freeze":
        refused = rust_domain.run("freeze", {"frozen": frozen})
        assert refused["status"] == "refused", refused
        assert refused["code"] in {"grant_required", "binding_drift"}
    else:
        with pytest.raises(ManifestError):
            worker._load_frozen_scenario(
                {
                    "scenario": {
                        "path": str(scenario_path),
                        "sha256": frozen["scenario_file_sha256"],
                        "identity_sha256": frozen["scenario_identity_sha256"],
                    },
                    "package": {"path": str(package_path), "sha256": frozen["package_sha256"]},
                    "local_strategy_workspace": str(stack.workspace),
                    "plugin_binding": frozen["plugin_binding"],
                }
            )
    assert not sentinel.exists()
    assert not (tmp_path / "replacement-executed").exists()


def test_conversation_closes_the_loop_through_the_real_rust_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unit.test_web_conversation import Streaming, _change, _read_tool
    from unit.test_web_research import _response

    monkeypatch.setattr(sys.modules[__name__], "CORE_ROOT", ROOT / ".local/langgraph/core")
    core = Core(tmp_path)
    _STARTED.append(core)
    stack = _stack(tmp_path, core.socket)
    provider = Streaming(
        [
            _response([], text="Earlier conversation retained."),
            _read_tool("describe_data", "data-1"),
            _response([], text="Which quantity should the next experiment use?"),
            _change("parameters.target_quantity", "3"),
            _response([], text="The verified report is one scenario, not a profitability claim."),
            _response([], text="We can investigate the report limitations next."),
        ]
    )
    cast(Any, stack.strategies)._provider = provider
    created = _create(stack.strategies)
    identity = created.user_strategy_id
    stack.strategies._ask(
        created,
        call_id="legacy-1",
        base_revision=1,
        text="remember this",
        approved_budget={"max_model_calls": 1, "max_backtests": 0},
    )
    before = stack.strategies.get(identity).messages
    list(
        stack.strategies.conversation_turn(
            identity,
            call_id="read-1",
            base_revision=1,
            text="Describe the authorized data and clarify the next change.",
            approved_budget={"max_model_calls": 2, "max_backtests": 0},
        )
    )
    proposed = list(
        stack.strategies.conversation_turn(
            identity,
            call_id="proposal-1",
            base_revision=1,
            text="Use quantity 3",
            approved_budget={"max_model_calls": 2, "max_backtests": 0},
        )
    )
    assert proposed[-1]["waiting"]["kind"] == "proposal"
    proposal = proposed[-1]["waiting"]["proposal_id"]
    stack.strategies.apply_proposal(identity, proposal_id=proposal, operation_id="apply-1")
    assert stack.strategies.get(identity).experiments == ()
    preview = list(
        stack.strategies.conversation_resume(
            identity, turn_id="proposal-1", action="applied", proposal_id=proposal
        )
    )[-1]["waiting"]
    assert preview["kind"] == "confirmation"
    confirm = dict(
        turn_id="proposal-1",
        action="confirm",
        launch_intent_id=preview["launch_intent_id"],
        base_revision=preview["base_revision"],
        preview_identity=preview["preview_identity"],
    )
    events = list(stack.strategies.conversation_resume(identity, **confirm))
    deadline = time.monotonic() + 30
    while events[-1]["kind"] == "interrupt" and time.monotonic() < deadline:
        time.sleep(0.03)
        events = list(
            stack.strategies.conversation_resume(identity, turn_id="proposal-1", action="observe")
        )
    assert events[-1]["kind"] == "done", events
    final = stack.strategies.get(identity)
    assert list(final.messages[: len(before)]) == list(before)
    assert len(final.experiments) == 1
    experiment = final.experiments[0]
    assert experiment["owner"] == "quine"
    assert experiment["status"] == "created"
    assert final.calls[-1]["evidence"][0]["job_id"] == experiment["job_id"]
    assert json.loads(stack.service.report(experiment["job_id"]))["run_id"]
    assert ResearchStore(stack.workspace).list() == []
    # Lost client receipt is resolved by the original intent, even after completion.
    list(stack.strategies.conversation_resume(identity, **confirm))
    assert len(stack.strategies.get(identity).experiments) == 1
    list(
        stack.strategies.conversation_turn(
            identity,
            call_id="follow-1",
            base_revision=2,
            text="What is still uncertain?",
            approved_budget={"max_model_calls": 1, "max_backtests": 0},
        )
    )
    assert stack.strategies.get(identity).messages[-1]["text"].endswith("limitations next.")
    assert len(provider.calls) == 6
