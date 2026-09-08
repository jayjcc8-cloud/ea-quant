from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from ea.core import ReplayWindow
from ea.data import decode_phase1_ohlcv_csv
from ea.web.app import create_app
from unit.test_web_api import ORIGIN, WRITE_HEADERS, _settings, _validate, _wait

pytestmark = pytest.mark.filterwarnings(
    "ignore:The anyio.abc.BlockingPortal alias is deprecated:DeprecationWarning"
)


def _target(root: Path, *, day: str = "03", start: str = "09:31", end: str = "09:34") -> None:
    document = yaml.safe_load((root / "bounded-long.yaml").read_text())
    payload = (root / "prices.csv").read_bytes().replace(b"2026-01-02", f"2026-01-{day}".encode())
    if start == "09:34":
        import re
        from datetime import timedelta

        payload = re.sub(
            rb"2026-01-\d{2}T09:\d{2}:\d{2}\.\d{6}Z",
            lambda match: (
                (datetime.fromisoformat(match.group().decode()) + timedelta(minutes=3))
                .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                .encode()
            ),
            payload,
        )
    (root / "later.csv").write_bytes(payload)
    start_utc = f"2026-01-{day}T{start}:00.000000Z"
    end_utc = f"2026-01-{day}T{end}:00.000000Z"
    selection = decode_phase1_ohlcv_csv(
        payload,
        replay_window=ReplayWindow(
            datetime.fromisoformat(start_utc), datetime.fromisoformat(end_utc)
        ),
    ).selection
    document["data"] = dict(
        path="later.csv",
        start_utc=start_utc,
        end_utc=end_utc,
        fingerprint=dict(
            sha256=selection.fingerprint.sha256.value,
            record_count=selection.fingerprint.record_count,
        ),
    )
    document["strategy"]["target_quantity"] = "1"
    (root / "later.yaml").write_text(yaml.safe_dump(document))


def _source(
    client: TestClient, *, scenario: str = "bounded-long.yaml", delay: int = 0
) -> dict[str, Any]:
    identity = _validate(client, scenario)["input_identity"]
    parameters = (
        None
        if scenario == "flat.yaml"
        else dict(
            initial_cash="10000",
            strategy_parameters=dict(target_quantity="2", entry_delay_bars=delay),
        )
    )
    response = client.post(
        "/api/backtests",
        headers=WRITE_HEADERS,
        json=dict(
            scenario_id=scenario,
            input_identity=identity,
            request_id="holdout-source",
            parameters=parameters,
        ),
    )
    assert response.status_code == 202, response.text
    job = _wait(client, response.json()["job_id"])
    assert job["status"] == "succeeded"
    return job


def _request(client: TestClient, source: dict[str, Any], **extra: object) -> Any:
    return client.post(
        "/api/holdouts",
        headers=WRITE_HEADERS,
        json=dict(source_job_id=source["job_id"], scenario_id="later.yaml", **extra),
    )


def test_holdout_real_reports_freeze_restart(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = settings.scenario_root
    document = yaml.safe_load((root / "bounded-long.yaml").read_text())
    document["execution"]["commission"] = dict(
        policy="deterministic-commission-v1", commission_bps="100"
    )
    (root / "bounded-long.yaml").write_text(yaml.safe_dump(document))
    _target(root)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        source = _source(client, delay=1)
        response = _request(client, source)
        assert response.status_code == 202, response.text
        relation = response.json()
        assert set(relation) == {
            "schema",
            "validation_id",
            "created_at",
            "source_job_id",
            "holdout_job_id",
        }
        assert relation["schema"] == "ea.chronological-holdout.v1"
        holdout = _wait(client, relation["holdout_job_id"])
        assert holdout["status"] == "succeeded"
        assert (
            holdout["input_snapshot"]["scenario"]["strategy"]
            == source["input_snapshot"]["scenario"]["strategy"]
        )
        assert holdout["attempt_id"] != source["attempt_id"]
        reports = [
            client.get(f"/api/backtests/{j['job_id']}/report").json() for j in [source, holdout]
        ]
        assert all(r["economics"]["fees"]["amount"] != "0" for r in reports)
        assert reports[0]["run_id"] != reports[1]["run_id"]
        assert len(client.get("/api/backtests").json()["jobs"]) == 2
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        assert client.get(f"/api/holdouts/{relation['validation_id']}").json() == relation
        assert client.get("/api/holdouts").json()["holdouts"] == [relation]
        assert client.get(f"/api/backtests/{holdout['job_id']}/report").json() == reports[1]


@pytest.mark.parametrize(
    "extra",
    [
        dict(target_quantity="4"),
        dict(entry_delay_bars=0),
        dict(parameters={}),
        dict(strategy_parameters={}),
    ],
)
def test_holdout_rejects_frontend_parameters(tmp_path: Path, extra: dict[str, object]) -> None:
    settings = _settings(tmp_path)
    _target(settings.scenario_root)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        source = _source(client)
        assert _request(client, source, **extra).status_code == 422
        assert len(client.get("/api/backtests").json()["jobs"]) == 1


@pytest.mark.parametrize("mode", ["flat", "unknown", "missing", "tampered"])
def test_holdout_rejects_source(tmp_path: Path, mode: str) -> None:
    settings = _settings(tmp_path)
    _target(settings.scenario_root)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        source = _source(client, scenario="flat.yaml" if mode == "flat" else "bounded-long.yaml")
        report = settings.workspace / "reports" / source["job_id"] / "report.json"
        if mode == "unknown":
            source["job_id"] = "unknown"
        elif mode == "missing":
            report.unlink()
        elif mode == "tampered":
            report.write_text("{}")
        assert _request(client, source).status_code in {404, 422}
        assert len(client.get("/api/backtests").json()["jobs"]) == 1


@pytest.mark.parametrize(
    ("day", "start", "end"),
    [
        ("02", "09:34", "09:37"),
        ("02", "09:31", "09:34"),
        ("02", "09:32", "09:33"),
        ("01", "09:31", "09:34"),
    ],
)
def test_holdout_rejects_nonlater_window(tmp_path: Path, day: str, start: str, end: str) -> None:
    settings = _settings(tmp_path)
    _target(settings.scenario_root, day=day, start=start, end=end)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        source = _source(client)
        assert _request(client, source).status_code == 422
        assert len(client.get("/api/backtests").json()["jobs"]) == 1


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("strategy", "id", "always-flat-v1"),
        ("instrument", "symbol", "MSFT"),
        ("instrument", "contract_multiplier", "2"),
        ("instrument", "settlement_currency", "EUR"),
        ("funding", "initial_cash", "9999"),
        ("risk", "max_order_quantity", "4"),
        ("execution", "policy", "other"),
        ("execution", "commission", dict(policy="other", commission_bps="1")),
        ("execution", "commission", dict(policy="deterministic-commission-v1", commission_bps="1")),
        ("", "randomness_profile", "other"),
    ],
)
def test_holdout_rejects_incompatible_configuration(
    tmp_path: Path, section: str, key: str, value: object
) -> None:
    settings = _settings(tmp_path)
    root = settings.scenario_root
    _target(root)
    document = yaml.safe_load((root / "later.yaml").read_text())
    (document[section] if section else document)[key] = value
    if section == "strategy":
        document["strategy"] = {"id": value}
    (root / "later.yaml").write_text(yaml.safe_dump(document))
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        source = _source(client)
        assert _request(client, source).status_code == 422
        assert len(client.get("/api/backtests").json()["jobs"]) == 1


def test_holdout_revalidates_target_dynamic_maximum(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _target(settings.scenario_root, end="09:33")
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        source = _source(client, delay=2)
        response = _request(client, source)
        assert response.status_code == 422
        assert len(client.get("/api/backtests").json()["jobs"]) == 1
        assert not list((settings.workspace / "jobs").glob("*/holdout.json"))


@pytest.mark.parametrize("state", ["accepted", "running", "failed", "interrupted"])
def test_source_must_have_successful_terminal_state(tmp_path: Path, state: str) -> None:
    from dataclasses import replace

    from ea.web.service import WebBoundaryError, WebService

    settings = _settings(tmp_path)
    _target(settings.scenario_root)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        source = _source(client)
    service = WebService(settings.scenario_root, settings.workspace)
    service.start()
    try:
        record = service.get_job(source["job_id"])
        service._store(replace(record, status=state))
        with pytest.raises(WebBoundaryError):
            service.create_holdout(source_job_id=record.job_id, scenario_id="later.yaml")
        assert len(service.list_jobs()) == 1
        assert service.list_holdouts() == []
    finally:
        service.stop()


def test_batch_member_source_does_not_mutate_batch(tmp_path: Path) -> None:
    from unit.test_web_api import _batch_request

    settings = _settings(tmp_path)
    _target(settings.scenario_root)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        batch = client.post(
            "/api/batches",
            headers=WRITE_HEADERS,
            json=_batch_request(
                _validate(client, "bounded-long.yaml")["input_identity"],
                [
                    dict(target_quantity="2", entry_delay_bars=0),
                    dict(target_quantity="3", entry_delay_bars=1),
                ],
            ),
        ).json()
        members = [_wait(client, job_id) for job_id in batch["member_job_ids"]]
        batch_before = client.get(f"/api/batches/{batch['batch_id']}").json()
        response = _request(client, members[1])
        assert response.status_code == 202, response.text
        holdout = _wait(client, response.json()["holdout_job_id"])
        assert holdout["status"] == "succeeded"
        assert (
            holdout["input_snapshot"]["scenario"]["strategy"]
            == members[1]["input_snapshot"]["scenario"]["strategy"]
        )
        assert client.get(f"/api/batches/{batch['batch_id']}").json() == batch_before


@pytest.mark.parametrize("stage", ["materialize", "publish", "submit"])
def test_atomic_failure_has_no_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    import os

    from ea.web.service import WebService

    settings = _settings(tmp_path)
    _target(settings.scenario_root)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        source = _source(client)
    service = WebService(settings.scenario_root, settings.workspace)
    service.start()

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("injected failure")

    try:
        with monkeypatch.context() as patch:
            if stage == "materialize":
                patch.setattr(service, "_materialize_scenario", fail)
            elif stage == "publish":
                patch.setattr(os, "replace", fail)
            else:
                patch.setattr(service._executor, "submit", fail)
            with pytest.raises(OSError, match="injected"):
                service.create_holdout(source_job_id=source["job_id"], scenario_id="later.yaml")
        assert len(service.list_jobs()) == 1
        assert service.list_holdouts() == []
        assert len(list(service.inputs_dir.glob("*.json"))) == 1
    finally:
        service.stop()
    restarted = WebService(settings.scenario_root, settings.workspace)
    restarted.start()
    assert len(restarted.list_jobs()) == 1
    assert restarted.list_holdouts() == []
    restarted.stop()


@pytest.mark.parametrize(
    ("start", "end", "accepted"),
    [
        ("2026-01-02T09:34:00.000000Z", "2026-01-02T09:35:00.000000Z", False),
        ("2026-01-02T09:33:00.000000Z", "2026-01-02T09:35:00.000000Z", False),
        ("2026-01-02T09:32:00.000000Z", "2026-01-02T09:33:00.000000Z", False),
        ("2026-01-01T09:31:00.000000Z", "2026-01-01T09:34:00.000000Z", False),
        ("2026-01-02T09:34:00.000001Z", "2026-01-02T09:35:00.000000Z", True),
        ("2026-02-02T09:31:00.000000Z", "2026-02-02T09:34:00.000000Z", True),
    ],
)
def test_exact_chronology_contract(start: str, end: str, accepted: bool) -> None:
    from ea.web.holdout import require_chronology

    source = dict(
        data=dict(start_utc="2026-01-02T09:31:00.000000Z", end_utc="2026-01-02T09:34:00.000000Z")
    )
    target = dict(data=dict(start_utc=start, end_utc=end))
    if accepted:
        require_chronology(source, target)
    else:
        with pytest.raises(ValueError, match="strictly later"):
            require_chronology(source, target)


def test_changed_snapshot_cannot_keep_old_scenario_identity(tmp_path: Path) -> None:
    import hashlib
    import json
    from dataclasses import replace

    from ea.web.service import _INPUT_DIGEST_DOMAIN, WebBoundaryError, WebService, _canonical_json

    settings = _settings(tmp_path)
    _target(settings.scenario_root)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        source = _source(client)
    service = WebService(settings.scenario_root, settings.workspace)
    service.start()
    try:
        record = service.get_job(source["job_id"])
        assert record.input_snapshot_bytes is not None
        snapshot = json.loads(record.input_snapshot_bytes)
        snapshot["scenario"]["strategy"]["target_quantity"] = "3"
        payload = _canonical_json(snapshot)
        service._store(
            replace(
                record,
                input_snapshot_bytes=payload,
                input_sha256=hashlib.sha256(_INPUT_DIGEST_DOMAIN + payload).hexdigest(),
            )
        )
        with pytest.raises(WebBoundaryError, match="identity"):
            service.create_holdout(source_job_id=record.job_id, scenario_id="later.yaml")
        assert len(service.list_jobs()) == 1
    finally:
        service.stop()


@pytest.mark.parametrize("published", [False, True])
def test_abrupt_publication_restarts_with_both_or_neither(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, published: bool
) -> None:
    import os

    from ea.web.service import WebService

    class AbruptExit(BaseException):
        pass

    settings = _settings(tmp_path)
    _target(settings.scenario_root)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        source = _source(client)
    service = WebService(settings.scenario_root, settings.workspace)
    service.start()
    original = os.replace

    def crash(before: Path, after: Path) -> None:
        if published:
            original(before, after)
        raise AbruptExit

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", crash)
            with pytest.raises(AbruptExit):
                service.create_holdout(source_job_id=source["job_id"], scenario_id="later.yaml")
    finally:
        service.stop()
    restarted = WebService(settings.scenario_root, settings.workspace)
    restarted.start()
    try:
        assert len(restarted.list_jobs()) == (2 if published else 1)
        relations = restarted.list_holdouts()
        assert len(relations) == (1 if published else 0)
        if published:
            relation = relations[0]
            assert restarted.get_job(relation["holdout_job_id"]).status == "interrupted"
            assert restarted.get_job(relation["source_job_id"]).status == "succeeded"
    finally:
        restarted.stop()


def test_busy_slot_rejects_before_holdout_persistence(tmp_path: Path) -> None:
    from ea.web.service import ServiceBusyError, WebService

    settings = _settings(tmp_path)
    _target(settings.scenario_root)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        source = _source(client)
    service = WebService(settings.scenario_root, settings.workspace)
    service.start()
    try:
        service._active = source["job_id"]
        with pytest.raises(ServiceBusyError):
            service.create_holdout(source_job_id=source["job_id"], scenario_id="later.yaml")
        assert len(service.list_jobs()) == 1
        assert service.list_holdouts() == []
    finally:
        service.stop()


def test_target_fingerprint_rechecked_after_candidate_listing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _target(settings.scenario_root)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        source = _source(client)
        candidates = client.get(f"/api/backtests/{source['job_id']}/holdout-scenarios")
        assert candidates.status_code == 200
        assert [item["scenario_id"] for item in candidates.json()["scenarios"]] == ["later.yaml"]
        target = settings.scenario_root / "later.csv"
        target.write_bytes(target.read_bytes().replace(b"110.0", b"109.0"))
        assert _request(client, source).status_code == 422
        assert len(client.get("/api/backtests").json()["jobs"]) == 1
