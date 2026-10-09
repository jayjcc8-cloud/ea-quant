"""Cancellation against the worker commit gate.

The controller owns one admission write and one finalization write per attempt.
These tests hold each of those two windows open and prove what may and may not
happen inside it: an accepted cancellation is never overwritten by a later
``running`` write, a verified-but-uncommitted result is never published after a
stop is accepted, a committed success is never rewritten by a later stop, and a
failing attempt settles through the same gate using the current job state.
"""

from __future__ import annotations

from pathlib import Path
from threading import Event
from time import monotonic, sleep
from typing import Any, cast

import pytest

from ea.web.service import JobRecord, ReportUnavailableError, WebService
from unit.test_web_api import _settings

REQUEST_ID = "request-worker-commit-0001"


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
    """An exception is arbitrated against current state, never an old record."""
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
        final = _terminal(service, accepted.job_id)
        assert final.status == "stopped", "an accepted stop outranks a late failure"
        assert final.error_code is None
        assert launches == []
    finally:
        release.set()
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
