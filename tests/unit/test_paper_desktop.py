"""Bounded desktop simulation admission over real Rust-owned research evidence."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ea.web.app import create_app
from unit.test_web_api import ORIGIN, WRITE_HEADERS
from unit.test_web_holdout import _target
from unit.test_web_rust_experiments import Core, _create_via_http, _web_settings
from unit.test_web_rust_holdout import _await_job, _offered, _post_holdout, _source_job


@pytest.mark.parametrize("finite", [False, True])
def test_rust_candidate_reads_owner_evidence_without_legacy_job_copies(
    tmp_path: Path, finite: bool
) -> None:
    import shutil
    import time
    from dataclasses import replace
    from uuid import uuid4

    import yaml

    from ea.web.paper_desktop import DesktopPaper
    from ea.web.quine import QuineBridge
    from unit.test_bounded_round_trips_v1 import bounded_scenario

    owner_root = Path(__file__).parents[2] / ".local" / "paper-desktop" / ("q" + uuid4().hex[:6])
    core = Core(tmp_path, workspace=owner_root)
    manager = None
    try:
        settings = _web_settings(tmp_path, core)
        if finite:
            bounded_scenario(settings.scenario_root).replace(
                settings.scenario_root / "bounded-long.yaml"
            )
            manager = DesktopPaper(
                tmp_path / "paper",
                settings.workspace,
                quine=QuineBridge(core.socket, workspace=core.workspace),
            )
            manager.open()
            settings = replace(settings, desktop_paper=manager)
        _target(settings.scenario_root)
        if finite:
            target = settings.scenario_root / "later.yaml"
            document = yaml.safe_load(target.read_text())
            document["strategy"].pop("target_quantity")
            target.write_text(yaml.safe_dump(document))
        with TestClient(create_app(settings), base_url=ORIGIN) as client:
            source = _source_job(client, _create_via_http(client))
            relation = _post_holdout(
                client, source, identity=_offered(client, source, "later.yaml")
            ).json()
            assert _await_job(client, relation["holdout_job_id"])["status"] == "succeeded"
            response = client.post(
                "/api/candidates",
                headers=WRITE_HEADERS,
                json={"validation_id": relation["validation_id"]},
            )
            assert response.status_code == 201, response.text
            candidate = response.json()
            path = f"/api/candidates/{candidate['candidate_id']}"
            assert (
                client.post(
                    path + "/decision",
                    headers=WRITE_HEADERS,
                    json={"outcome": "ACCEPTED", "reason": "Synthetic workflow check only"},
                ).status_code
                == 200
            )
            assert client.get(path).json()["status"] == "ACCEPTED"
            preparation = client.get(path + "/simulation-preparation").json()
            if finite:
                assert preparation["eligible"], preparation
                request_id = str(uuid4())
                launched = client.post(
                    "/api/paper/instances",
                    headers=WRITE_HEADERS,
                    json={
                        "request_id": request_id,
                        "candidate_id": candidate["candidate_id"],
                        "fingerprint": preparation["fingerprint"],
                        "name": "Finite owner run",
                        "prices": [100],
                        "interval_seconds": 0.01,
                        "duration_seconds": 2,
                    },
                )
                assert launched.status_code == 202, launched.text
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    ended = client.get(f"/api/paper/instances/{request_id}").json()
                    if ended["terminal_confirmed"]:
                        break
                    time.sleep(0.05)
                assert ended["terminal_confirmed"], ended
                assert ended["overview"]["stop_trigger"] == "duration_budget"
                assert ended["overview"]["cash"] == "9998.8"
                assert ended["process_exit"] == 0
            else:
                assert preparation["blockers"] == [
                    "unsupported_paper_profile_requires_action_v2_v4_v5"
                ]
            assert not list((settings.workspace / "jobs").glob("*.json"))
            # An owner artifact drift is unavailable, never a replacement report.
            report = core.workspace / "official" / source / "report.json"
            report.write_bytes(report.read_bytes() + b" ")
            assert client.get(path).status_code == 409
    finally:
        if manager is not None:
            manager.close()
        core.stop()
        shutil.rmtree(owner_root)


def test_launch_identity_survives_lost_response_and_stop_reopens(tmp_path: Path) -> None:
    import json
    import time
    from uuid import uuid4

    from ea.web.paper_desktop import DesktopPaper
    from unit.test_bounded_round_trips_v1 import bounded_scenario
    from unit.test_candidate_runtime import _create_candidate_evidence

    path = bounded_scenario(tmp_path / "scenarios")
    _create_candidate_evidence(tmp_path, path, None)
    selection = json.loads((tmp_path / "selection.json").read_text())
    root = tmp_path / "desktop-paper"
    manager = DesktopPaper(root, tmp_path / "workspace")
    manager.open()
    try:
        prepared = manager.prepare(selection["candidate_id"])
        request = dict(
            request_id=str(uuid4()),
            candidate_id=selection["candidate_id"],
            fingerprint=prepared["fingerprint"],
            name="Local simulation",
            prices=[100.0],
            interval_seconds=0.1,
        )
        first = manager.start(request)
        assert manager.start(request)["run_id"] == first["run_id"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            observed = manager.get(request["request_id"])
            if observed.get("overview", {}).get("fills") == 6:
                break
            time.sleep(0.1)
        else:
            raise AssertionError(observed)
        stopped = manager.stop(request["request_id"])
        assert stopped["terminal_confirmed"]
        assert stopped["overview"]["cash"] == "9998.8"
        assert stopped["overview"]["ledger_sequence"] == 7
        assert len(list((root / "runs").iterdir())) == 1
    finally:
        manager.close()
    reopened = DesktopPaper(root, tmp_path / "workspace")
    reopened.open()
    try:
        assert reopened.get(request["request_id"])["terminal_confirmed"]
        assert reopened.start(request)["run_id"] == request["request_id"]
        assert len(list((root / "runs").iterdir())) == 1
    finally:
        reopened.close()


def test_failed_or_uncertain_stop_never_releases_the_start_gate(tmp_path: Path) -> None:
    import json
    import time
    from uuid import uuid4

    import pytest

    from ea.web.paper_desktop import DesktopPaper
    from unit.test_bounded_round_trips_v1 import bounded_scenario
    from unit.test_candidate_runtime import _create_candidate_evidence

    path = bounded_scenario(tmp_path / "scenarios")
    _create_candidate_evidence(tmp_path, path, None)
    candidate = json.loads((tmp_path / "selection.json").read_text())["candidate_id"]
    manager = DesktopPaper(tmp_path / "paper", tmp_path / "workspace")
    manager.open()
    request = dict(
        request_id=str(uuid4()),
        candidate_id=candidate,
        fingerprint=manager.prepare(candidate)["fingerprint"],
        name="Fault probe",
        prices=[100.0],
        interval_seconds=1.0,
    )
    try:
        manager.start(request)
        deadline = time.monotonic() + 10
        while not manager.get(request["request_id"])["overview"]["available"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        # Reproduce the failure class on this test's own captured child only.
        child = manager._children[request["request_id"]]
        child.kill()
        child.wait(timeout=5)
        assert manager.get(request["request_id"])["state"] == "unknown"
        assert not manager.stop(request["request_id"], timeout=0.0)["terminal_confirmed"]
        with pytest.raises(ValueError, match="unconfirmed"):
            manager.start({**request, "request_id": str(uuid4())})
        assert not manager.shutdown(timeout=0.0)
        assert len(list(manager.runs.iterdir())) == 1
        # Unknown history survives another reader; it is not automatically resumed.
        other = DesktopPaper(manager.root, manager.workspace)
        assert other.get(request["request_id"])["state"] == "unknown"
    finally:
        # The test has proved and reaped this exact dead child; preserve all evidence.
        cleanup_child = manager._children.get(request["request_id"])
        if cleanup_child is not None:
            if cleanup_child.poll() is None:
                cleanup_child.kill()
                cleanup_child.wait(timeout=5)
            if cleanup_child.stdin is not None and not cleanup_child.stdin.closed:
                cleanup_child.stdin.close()
        manager._lock.close()


def test_candidate_fingerprint_drift_refuses_before_attempt_or_strategy_execution(
    tmp_path: Path,
) -> None:
    import json

    import pytest

    from ea.product.paper_run import run_local_paper
    from unit.test_bounded_round_trips_v1 import bounded_scenario
    from unit.test_candidate_runtime import _create_candidate_evidence

    path = bounded_scenario(tmp_path / "scenarios")
    _create_candidate_evidence(tmp_path, path, None)
    candidate = json.loads((tmp_path / "selection.json").read_text())["candidate_id"]
    with pytest.raises(ValueError, match="confirmed startup identity"):
        run_local_paper(
            tmp_path / "workspace",
            candidate,
            path,
            tmp_path / "runs",
            expected_fingerprint="0" * 64,
        )
    assert list((tmp_path / "runs").iterdir()) == []


def test_runtime_reads_cannot_escape_the_configured_root(tmp_path: Path) -> None:
    import pytest

    from ea.web.radian import paper_events, paper_overview

    root = tmp_path / "runtime"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (other / "operational.jsonl").write_text('{"event":"unrelated"}\n')
    (root / "alias").symlink_to(other, target_is_directory=True)
    for name in ("../other", str(other), "alias"):
        with pytest.raises(ValueError):
            paper_events(root, run_id=name)
        with pytest.raises(ValueError):
            paper_overview(root, run_id=name)


def test_workspace_selection_requires_verified_stop(tmp_path: Path) -> None:
    from unittest.mock import Mock

    from unit.test_desktop_settings import context
    from unit.test_web_radian import _settings

    desktop = context(tmp_path)
    target = tmp_path / "next"
    target.mkdir()
    paper = Mock()
    paper.shutdown.return_value = False
    settings = _settings(tmp_path, desktop_settings=desktop, desktop_paper=paper)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        before = desktop.status()["next_workspace"]
        response = client.post(
            "/api/desktop/settings",
            headers=WRITE_HEADERS,
            json={"next_workspace": str(target)},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "paper_stop_unconfirmed"
        assert desktop.status()["next_workspace"] == before
        paper.shutdown.assert_called_once_with()
        paper.shutdown.return_value = True
        response = client.post(
            "/api/desktop/settings",
            headers=WRITE_HEADERS,
            json={"next_workspace": str(target)},
        )
        assert response.status_code == 200
        assert desktop.status()["next_workspace"] == str(target)
        assert list(target.iterdir()) == []


def test_replaced_request_directory_cannot_become_a_new_owner(tmp_path: Path) -> None:
    import pytest

    from ea.web.paper_desktop import DesktopPaper

    manager = DesktopPaper(tmp_path / "paper", tmp_path / "workspace")
    manager.open()
    original = manager.requests.with_name("retained-requests")
    manager.requests.rename(original)
    manager.requests.mkdir(mode=0o700)
    try:
        with pytest.raises(ValueError, match="directory identity changed"):
            manager.list()
        with pytest.raises(ValueError, match="directory identity changed"):
            manager.shutdown()
    finally:
        manager.requests.rmdir()
        original.rename(manager.requests)
        manager.close()


def test_invalid_workspace_save_does_not_stop_paper(tmp_path: Path) -> None:
    from unittest.mock import Mock

    from unit.test_desktop_settings import context
    from unit.test_web_radian import _settings

    desktop = context(tmp_path)
    paper = Mock()
    target = tmp_path / "next"
    target.mkdir()
    settings = _settings(tmp_path, desktop_settings=desktop, desktop_paper=paper)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        before = desktop.status()["next_workspace"]
        bodies: tuple[dict[str, object], ...] = (
            {"next_workspace": "relative-test-path"},
            {"next_workspace": str(target), "theme": "dark"},
            {"next_workspace": []},
        )
        for body in bodies:
            response = client.post("/api/desktop/settings", headers=WRITE_HEADERS, json=body)
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "settings_not_saved"
            assert desktop.status()["next_workspace"] == before
            paper.shutdown.assert_not_called()
