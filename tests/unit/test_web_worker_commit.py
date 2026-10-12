"""Cancellation against the worker commit gate.

The controller owns one admission write and one finalization write per attempt.
These tests hold each of those two windows open and prove what may and may not
happen inside it: an accepted cancellation is never overwritten by a later
``running`` write, a verified-but-uncommitted result is never published after a
stop is accepted, a committed success is never rewritten by a later stop, and a
failing attempt settles through the same gate using the current job state.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any, cast

import pytest

from ea.web import worker
from ea.web.service import JobRecord, ReportUnavailableError, WebService
from unit.test_web_api import _settings

REQUEST_ID = "request-worker-commit-0001"


def _wait_committed(service: WebService, bound: float = 60.0) -> None:
    """Wait until the supervising thread has released the active-job slot.

    That slot is released after the commit gate has run, so it is the in-process
    signal ordered after the final write. The live worker handle is dropped
    before the gate is reached, so it cannot stand in for this barrier.
    """
    deadline = monotonic() + bound
    while monotonic() < deadline:
        with service._lock:
            if service._active is None:
                return
        sleep(0.02)
    raise AssertionError("the supervisor never released its active slot")


def _unverifiable_group(service: WebService) -> list[Any]:
    """Spawn for real, then hold the handle with no captured group identity."""
    spawned: list[Any] = []
    real_spawn = service._spawn_worker

    def without_identity(control: Path) -> Any:
        process = replace(real_spawn(control), process_group=None)
        spawned.append(process)
        return process

    service._spawn_worker = without_identity  # type: ignore[method-assign]
    return spawned


#: A trusted local test worker that computes until something stops it.
_BLOCKING_HOOK = """
import time


def hook(*, scenario, control, request, worker):
    (control / "computing.marker").write_text("computing", encoding="ascii")
    while True:
        time.sleep(0.05)
"""


def _blocking_hook(tmp_path: Path) -> str:
    path = tmp_path / "commit-blocking-hook.py"
    path.write_text(_BLOCKING_HOOK, encoding="utf-8")
    return f"{path}:hook"


def _service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[WebService, list[Path]]:
    """One started service that records every worker it actually spawns."""
    settings = _settings(tmp_path)
    service = WebService(settings.scenario_root, settings.workspace)
    launches: list[Path] = []
    real_spawn = service._spawn_worker

    def counted(control: Path) -> Any:
        launches.append(control)
        return real_spawn(control)

    monkeypatch.setattr(service, "_spawn_worker", counted)
    service.start()
    return service, launches


def _submit(service: WebService, request_id: str = REQUEST_ID) -> JobRecord:
    validated = service.validate_scenario("bounded-long.yaml")
    accepted, _created = service.create_job(
        scenario_id="bounded-long.yaml",
        input_identity=cast(dict[str, object], validated["input_identity"]),
        request_id=request_id,
    )
    return accepted


def _terminal(service: WebService, job_id: str) -> JobRecord:
    deadline = monotonic() + 30
    while monotonic() < deadline:
        job = service.get_job(job_id)
        if job.status not in {"accepted", "running", "stopping"}:
            return job
        sleep(0.01)
    raise AssertionError("the job did not settle within thirty seconds")


def _pause_after_settlement(
    service: WebService, monkeypatch: pytest.MonkeyPatch
) -> tuple[Event, Event]:
    """Hold the window between a verified result and its commit open."""
    original = service._settle
    ready, release = Event(), Event()

    def gated(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        ready.set()
        assert release.wait(30), "the test never released the commit window"
        return result

    monkeypatch.setattr(service, "_settle", gated)
    return ready, release


def test_cancellation_before_the_supervisor_runs_never_spawns_compute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop accepted before ``_run`` starts is not overwritten by ``running``."""
    service, launches = _service(tmp_path, monkeypatch)
    original = service._run
    ready, release = Event(), Event()

    def gated(*args: Any, **kwargs: Any) -> None:
        ready.set()
        assert release.wait(30), "the test never released the supervisor"
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_run", gated)
    try:
        accepted = _submit(service)
        assert ready.wait(30)
        assert service.stop_job(accepted.job_id).status == "stopping"
        release.set()
        final = _terminal(service, accepted.job_id)
        assert final.status == "stopped"
        assert final.error_code is None
        assert final.engine_run_id is None
        assert final.report_sha256 is None
        assert launches == [], "a cancelled job must never spawn compute"
        assert list(service.runs_dir.iterdir()) == []
        assert not (service.reports_dir / accepted.job_id).exists()
        with pytest.raises(ReportUnavailableError):
            service.report(accepted.job_id)
    finally:
        release.set()
        service.stop()


def test_cancellation_after_verification_blocks_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified result is not published once a stop has been accepted."""
    service, launches = _service(tmp_path, monkeypatch)
    ready, release = _pause_after_settlement(service, monkeypatch)
    try:
        accepted = _submit(service)
        assert ready.wait(30), "the worker never produced a verificable result"
        assert len(launches) == 1
        cancellation = service.stop_job(accepted.job_id)
        assert cancellation.status == "stopping"
        release.set()
        final = _terminal(service, accepted.job_id)
        assert final.status == "stopped"
        assert final.report_sha256 is None
        assert final.summary_sha256 is None
        assert final.equity_path_sha256 is None
        assert final.engine_run_id is None
        assert list(service.runs_dir.iterdir()) == []
        assert not (service.reports_dir / accepted.job_id).exists()
        # The refused attempt keeps its staging as diagnostics; nothing reads it.
        assert (service.worker_dir / accepted.job_id).is_dir()
        with pytest.raises(ReportUnavailableError):
            service.report(accepted.job_id)
    finally:
        release.set()
        service.stop()


def test_the_same_window_commits_a_success_when_nothing_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The held window is a real commit path, not a path that never publishes."""
    service, launches = _service(tmp_path, monkeypatch)
    ready, release = _pause_after_settlement(service, monkeypatch)
    try:
        accepted = _submit(service)
        assert ready.wait(30)
        release.set()
        final = _terminal(service, accepted.job_id)
        assert final.status == "succeeded"
        assert len(launches) == 1
        assert final.report_sha256 == service.get_job(accepted.job_id).report_sha256
        assert (service.runs_dir / cast(str, final.attempt_id)).is_dir()
        assert (service.reports_dir / accepted.job_id / "report.json").is_file()
    finally:
        release.set()
        service.stop()


def test_stop_after_commit_never_rewrites_a_succeeded_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed, committed result is not overwritten by a later cancellation."""
    service, _launches = _service(tmp_path, monkeypatch)
    try:
        accepted = _submit(service)
        committed = _terminal(service, accepted.job_id)
        assert committed.status == "succeeded"
        after = service.stop_job(accepted.job_id)
        assert after == committed
        assert service.get_job(accepted.job_id) == committed
        assert service.report(accepted.job_id)
    finally:
        service.stop()


def test_failed_attempt_settles_through_the_same_gate_as_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception is arbitrated against current state, never an old record.

    The failure is raised where the controller cannot place it relative to the
    dispatch boundary, so it proves nothing about compute having ended: the
    accepted stop stays the job's state, unconfirmed, and the late failure never
    becomes the outcome the job is reported with.
    """
    service, launches = _service(tmp_path, monkeypatch)
    entered, release = Event(), Event()

    def failing_execute(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(30), "the test never released the failing attempt"
        raise RuntimeError("injected supervisor failure")

    monkeypatch.setattr(service, "_execute", failing_execute)
    try:
        accepted = _submit(service)
        assert entered.wait(30)
        assert service.stop_job(accepted.job_id).status == "stopping"
        release.set()
        _wait_committed(service)
        final = service.get_job(accepted.job_id)
        assert final.status == "stopping", final.status
        assert final.report_sha256 is None and final.engine_run_id is None
        assert launches == []
    finally:
        release.set()
        service.stop()


def test_a_dispatch_that_starts_compute_then_fails_is_never_a_confirmed_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spawn that raises after starting a child is not proof that none started.

    The spawn call is the one thing that starts compute, so a failure raised by
    that call leaves it unknown whether a child exists — and no execution fact
    shows any group has gone. A cancellation accepted while the controller holds
    no handle for the attempt must therefore stay unconfirmed rather than being
    recorded as a confirmed stop.
    """
    settings = _settings(tmp_path)
    service = WebService(settings.scenario_root, settings.workspace)
    service._test_hook = _blocking_hook(tmp_path)
    service.start()
    real_spawn = service._spawn_worker
    live: list[Any] = []
    entered, release = Event(), Event()

    def spawn_then_fail(control: Path) -> Any:
        process = real_spawn(control)
        live.append(process)
        entered.set()
        assert release.wait(60), "the test never released the spawning dispatch"
        raise OSError("the dispatch started a child and then failed")

    monkeypatch.setattr(service, "_spawn_worker", spawn_then_fail)
    try:
        accepted = _submit(service, "request-spawn-then-fail-0001")
        assert entered.wait(60), "the spawn never started"
        # The child is running from this controller's point of view, yet no
        # handle has been registered for it.
        with service._lock:
            assert service._runs == {}
        assert live and live[0].stopped() is False

        assert service.stop_job(accepted.job_id).status == "stopping"
        release.set()
        _wait_committed(service)

        final = service.get_job(accepted.job_id)
        assert final.status not in {"stopped", "succeeded"}, final.status
        assert final.report_sha256 is None and final.engine_run_id is None
        assert not (service.reports_dir / accepted.job_id).exists()
        # Nothing proved this child's group is gone, so nothing may say it stopped.
        assert live and live[0].stopped() is False
    finally:
        release.set()
        # End the captured handle through its own verified group, then reap it.
        for process in live:
            process.kill()
            assert process.wait(15), "the test worker outlived its cleanup"
            process.close()
        service.stop()


def test_repeated_dispatch_and_repeated_stop_keep_one_attempt_and_one_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idempotent admission and idempotent cancellation never add an attempt."""
    service, launches = _service(tmp_path, monkeypatch)
    ready, release = _pause_after_settlement(service, monkeypatch)
    try:
        accepted = _submit(service)
        duplicate, created = service.create_job(
            scenario_id="bounded-long.yaml",
            input_identity=service.get_job(accepted.job_id).input_identity,
            request_id=REQUEST_ID,
        )
        assert created is False
        assert duplicate.job_id == accepted.job_id
        assert ready.wait(30)
        assert service.stop_job(accepted.job_id).status == "stopping"
        assert service.stop_job(accepted.job_id).status == "stopping"
        release.set()
        final = _terminal(service, accepted.job_id)
        assert final.status == "stopped"
        assert len(launches) == 1
        assert len(service.list_jobs()) == 1
    finally:
        release.set()
        service.stop()


def test_a_late_cancellation_never_upgrades_an_unconfirmed_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a confirmed group disappearance, or never-spawned compute, permits stopped.

    The supervisor could not show that the owned group was gone, so the attempt
    settled as unconfirmed. A cancellation accepted while that verdict is on its
    way to the commit gate must not turn it into a confirmed stop: the group was
    never shown to be gone, and reporting ``stopped`` would claim compute had
    ended when nothing proved it had.
    """
    monkeypatch.setattr(worker, "KILL_GRACE_SECONDS", 0.05)
    service, _launches = _service(tmp_path, monkeypatch)
    spawned = _unverifiable_group(service)
    real_settle = service._settle
    ready, release = Event(), Event()

    def held_settle(*args: Any, **kwargs: Any) -> Any:
        intent = real_settle(*args, **kwargs)
        ready.set()
        assert release.wait(60), "the test never released the commit gate"
        return intent

    monkeypatch.setattr(service, "_settle", held_settle)
    try:
        accepted = _submit(service, "request-late-unknown-stop-0001")
        assert ready.wait(60), "the supervisor never reached its verdict"
        assert service.stop_job(accepted.job_id).status == "stopping"
        release.set()
        _wait_committed(service)
        final = service.get_job(accepted.job_id)
        assert spawned and spawned[0].stopped() is False
        assert final.status == "stopping", final.status
        assert final.error_code == "worker_group_unconfirmed"
        assert final.report_sha256 is None and final.engine_run_id is None
        assert list(service.runs_dir.iterdir()) == []
        assert not (service.reports_dir / accepted.job_id).exists()
    finally:
        release.set()
        service.stop()


def test_shutdown_never_upgrades_an_unconfirmed_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown is a stop verdict too, and it is held to the same proof."""
    monkeypatch.setattr(worker, "KILL_GRACE_SECONDS", 0.05)
    service, _launches = _service(tmp_path, monkeypatch)
    spawned = _unverifiable_group(service)
    real_settle = service._settle
    ready, release = Event(), Event()

    def held_settle(*args: Any, **kwargs: Any) -> Any:
        intent = real_settle(*args, **kwargs)
        ready.set()
        assert release.wait(60), "the test never released the commit gate"
        return intent

    monkeypatch.setattr(service, "_settle", held_settle)
    try:
        accepted = _submit(service, "request-shutdown-unknown-stop-0001")
        assert ready.wait(60), "the supervisor never reached its verdict"
        stopper = Thread(target=service.stop, daemon=True)
        stopper.start()
        deadline = monotonic() + 30
        while not service._closing and monotonic() < deadline:
            sleep(0.01)
        assert service._closing, "shutdown never began"
        release.set()
        stopper.join(60)
        assert not stopper.is_alive()
        final = service.get_job(accepted.job_id)
        assert spawned and spawned[0].stopped() is False
        assert final.status == "stopping", final.status
        assert final.error_code == "worker_group_unconfirmed"
        assert final.report_sha256 is None
    finally:
        release.set()
