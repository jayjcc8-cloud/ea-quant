"""Cancelling a research task stops the run the controller owns.

The task record and the job record answer different questions: the task is
cancelled and closed to further dispatch, while the job's own state is the real
outcome of the compute. These tests drive a real dispatch through the scripted
provider and a real worker process, and check both answers against each other.
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from ea.web.app import create_app
from ea.web.research import ResearchOrchestrator, ResearchTask, TaskStateError
from ea.web.service import WebService
from unit.test_web_api import (
    ORIGIN,
    WRITE_HEADERS,
    _hook_launcher,
    _settings,
    _validate,
    _wait,
)
from unit.test_web_research import FakeProvider, _confirmed_task, _fake_orchestrator

_BLOCKING_HOOK = """
import time


def hook(*, scenario, control, request, worker):
    (control / "computing.marker").write_text("computing", encoding="ascii")
    while True:
        time.sleep(0.05)
"""

TERMINAL = frozenset({"succeeded", "failed", "stopped", "interrupted"})


def _hook(tmp_path: Path) -> str:
    path = tmp_path / "blocking-hook.py"
    path.write_text(_BLOCKING_HOOK, encoding="utf-8")
    return f"{path}:hook"


def _stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ResearchOrchestrator, FakeProvider, WebService, str]:
    """One orchestrator over a real service whose runs block inside compute."""
    hook = _hook(tmp_path)
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    service._test_hook = hook
    return orchestrator, provider, service, hook


def _dispatch(
    orchestrator: ResearchOrchestrator, provider: FakeProvider, idea: str
) -> ResearchTask:
    task = _confirmed_task(orchestrator, provider, idea)
    dispatched = orchestrator._start_backtest(task.task_id)  # noqa: SLF001
    assert dispatched.jobs, dispatched.error
    return dispatched


def _wait_marker(service: WebService, job_id: str, bound: float = 30.0) -> None:
    marker = service.worker_dir / job_id / "computing.marker"
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        if marker.is_file():
            return
        time.sleep(0.02)
    raise AssertionError("the run never started computing")


def _wait_job(service: WebService, job_id: str, bound: float = 60.0) -> Any:
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        job = service.get_job(job_id)
        if job.status in TERMINAL:
            return job
        time.sleep(0.02)
    raise AssertionError("the job never settled")


def test_cancelling_a_task_stops_the_run_it_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cancellation reaches the controller and the compute really stops."""
    orchestrator, provider, service, _hook_reference = _stack(tmp_path, monkeypatch)
    try:
        dispatched = _dispatch(orchestrator, provider, "Cancel the run in flight")
        job_id = dispatched.jobs[0]
        _wait_marker(service, job_id)

        cancelled = orchestrator.cancel(dispatched.task_id)
        assert cancelled.status == "cancelled"
        assert cancelled.cancel_note is not None
        assert "not a rollback" in cancelled.cancel_note
        # The accepted request is visible at once, before the stop is confirmed.
        assert service.get_job(job_id).status in {"stopping", "stopped"}

        final = _wait_job(service, job_id)
        assert final.status == "stopped"
        assert final.report_sha256 is None
        assert final.engine_run_id is None
        # Nothing derived from a stopped run is served, and no owned compute is left.
        assert list(service.runs_dir.iterdir()) == []
        assert not (service.reports_dir / job_id).exists()
        with service._lock:
            assert service._runs == {}
        # The task keeps its own terminal answer and refuses further work.
        assert orchestrator.refresh(dispatched.task_id).status == "cancelled"
        with pytest.raises(TaskStateError):
            orchestrator.retry(dispatched.task_id)
    finally:
        service.stop()


def test_cancelling_after_the_run_committed_keeps_the_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A committed run is not rewritten by a cancellation that arrives later."""
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    try:
        dispatched = _dispatch(orchestrator, provider, "A run that finishes first")
        job_id = dispatched.jobs[0]
        committed = _wait_job(service, job_id)
        assert committed.status == "succeeded"

        cancelled = orchestrator.cancel(dispatched.task_id)
        assert cancelled.status == "cancelled"
        after = service.get_job(job_id)
        assert after == committed
        assert service.report(job_id)
        assert (service.reports_dir / job_id / "report.json").is_file()
    finally:
        service.stop()


def test_cancelling_reconciles_a_run_the_projection_no_longer_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run admitted inside the dispatch window is stopped through its request id."""
    orchestrator, provider, service, _hook_reference = _stack(tmp_path, monkeypatch)
    store = cast(Any, orchestrator)._store
    try:
        dispatched = _dispatch(orchestrator, provider, "Bound by request identity only")
        job_id = dispatched.jobs[0]
        _wait_marker(service, job_id)

        # The task projection is stale: it no longer lists the run it started, but
        # its own request identity still resolves to that same job.
        stale = store.get(dispatched.task_id)
        store.save(replace(stale, jobs=()))
        assert store.get(dispatched.task_id).jobs == ()

        cancelled = orchestrator.cancel(dispatched.task_id)
        assert cancelled.status == "cancelled"
        final = _wait_job(service, job_id)
        assert final.status == "stopped"
        assert list(service.runs_dir.iterdir()) == []
    finally:
        service.stop()


def test_a_cancelled_task_dispatches_nothing_further(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation closes the task against new work, and reaches no engine."""
    orchestrator, provider, service, _hook_reference = _stack(tmp_path, monkeypatch)
    launches: list[Path] = []
    real_spawn = service._spawn_worker

    def counted(control: Path) -> Any:
        launches.append(control)
        return real_spawn(control)

    monkeypatch.setattr(service, "_spawn_worker", counted)
    try:
        task = _confirmed_task(orchestrator, provider, "Never dispatched")
        assert orchestrator.cancel(task.task_id).status == "cancelled"
        with pytest.raises(TaskStateError):
            orchestrator.confirm(task.task_id)
        with pytest.raises(TaskStateError):
            orchestrator.retry(task.task_id)
        assert launches == []
    finally:
        service.stop()


def test_a_cancellation_accepted_after_the_dispatch_mark_publishes_no_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy submission cancelled inside its own dispatch window starts nothing.

    A legacy task records ``backtest_dispatch`` before the job boundary, so the
    window between that durable mark and the first job record is real. A
    cancellation accepted there must not publish a run and must not start
    compute; the mark itself is kept as the charged slot it always was.
    """
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    confirmed = _confirmed_task(orchestrator, provider, "Cancelled inside the dispatch window")
    admitted, release = Event(), Event()
    real_create = service.create_job

    def gated_create(**kwargs: Any) -> Any:
        admitted.set()
        assert release.wait(30), "the test never released the job boundary"
        return real_create(**kwargs)

    monkeypatch.setattr(service, "create_job", gated_create)
    outcomes: list[Any] = []

    def submit() -> None:
        try:
            outcomes.append(orchestrator._start_backtest(confirmed.task_id))  # noqa: SLF001
        except BaseException as error:  # noqa: BLE001 — the test reports what escaped
            outcomes.append(error)

    worker = Thread(target=submit, daemon=True)
    worker.start()
    try:
        assert admitted.wait(30), "the submission never reached the job boundary"
        # The dispatch mark is durable, and no job exists yet.
        durable = orchestrator._store.get(confirmed.task_id)  # noqa: SLF001
        assert durable.backtest_reservation is not None
        assert durable.backtest_reservation.status == "dispatched"
        assert service.list_jobs() == []

        assert orchestrator.cancel(confirmed.task_id).status == "cancelled"
        release.set()
        worker.join(30)
        assert not worker.is_alive(), "the submission never returned"
        assert outcomes and not isinstance(outcomes[0], BaseException), outcomes
        assert cast(ResearchTask, outcomes[0]).status == "cancelled"
        # Nothing was published and no compute was ever spawned for it.
        assert service.list_jobs() == []
        assert list(service.worker_dir.iterdir()) == []
        settled = orchestrator._store.get(confirmed.task_id)  # noqa: SLF001
        assert settled.status == "cancelled"
        assert settled.jobs == ()
    finally:
        release.set()
        service.stop()


# --- the HTTP stop route ----------------------------------------------------


def _http_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """App settings whose RADIAN settings file is a missing file in this workspace.

    The app reads the operator's settings path; pointing it at a file that does
    not exist inside this test's own workspace keeps the run hermetic without
    reading or writing anything outside it.
    """
    path = tmp_path / "workspace" / "radian-settings.json"
    assert not path.exists()
    monkeypatch.setenv("RADIAN_SETTINGS_FILE", str(path))
    return _settings(tmp_path)


def test_the_stop_route_accepts_a_live_run_then_reports_the_confirmed_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_hook: Any
) -> None:
    """The route answers with the accepted stop, and the run ends stopped."""
    hook = worker_hook("blocking", _BLOCKING_HOOK)
    monkeypatch.setattr(WebService, "_spawn_worker", _hook_launcher(hook))
    settings = _http_settings(tmp_path, monkeypatch)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        validated = _validate(client, "bounded-long.yaml")
        accepted = client.post(
            "/api/backtests",
            headers=WRITE_HEADERS,
            json={
                "scenario_id": "bounded-long.yaml",
                "input_identity": validated["input_identity"],
                "request_id": "stop-route-live-0001",
            },
        )
        assert accepted.status_code == 202, accepted.text
        job_id = accepted.json()["job_id"]
        marker = settings.workspace / ".worker" / job_id / "computing.marker"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not marker.is_file():
            time.sleep(0.02)
        assert marker.is_file(), "the run never started computing"

        # The same write guard every mutating route carries.
        assert client.post(f"/api/backtests/{job_id}/stop").status_code == 403
        accepted_stop = client.post(f"/api/backtests/{job_id}/stop", headers=WRITE_HEADERS)
        assert accepted_stop.json()["status"] == "stopping"

        stopped = _wait(client, job_id)
        assert stopped["status"] == "stopped"
        assert stopped["report_ready"] is False
        assert stopped["report_sha256"] is None
        assert client.get(f"/api/backtests/{job_id}/report").status_code == 409
        # Asking again for a terminal run answers with that same record.
        again = client.post(f"/api/backtests/{job_id}/stop", headers=WRITE_HEADERS)
        assert again.status_code == 200
        assert again.json() == stopped


def test_the_stop_route_never_rewrites_a_committed_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that already committed keeps its record and its official report."""
    settings = _http_settings(tmp_path, monkeypatch)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        validated = _validate(client, "bounded-long.yaml")
        accepted = client.post(
            "/api/backtests",
            headers=WRITE_HEADERS,
            json={
                "scenario_id": "bounded-long.yaml",
                "input_identity": validated["input_identity"],
                "request_id": "stop-route-committed-0001",
            },
        )
        assert accepted.status_code == 202, accepted.text
        job_id = accepted.json()["job_id"]
        committed = _wait(client, job_id)
        assert committed["status"] == "succeeded"
        before = client.get(f"/api/backtests/{job_id}/report")
        assert before.status_code == 200

        response = client.post(f"/api/backtests/{job_id}/stop", headers=WRITE_HEADERS)
        assert response.status_code == 200
        assert response.json() == committed
        assert client.get(f"/api/backtests/{job_id}/report").content == before.content
