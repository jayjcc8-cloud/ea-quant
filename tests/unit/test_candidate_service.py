from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ea.web.app import create_app
from unit.test_web_api import ORIGIN, _settings, _wait
from unit.test_web_holdout import _request, _source, _target


def test_candidate_reopens_after_source_deletion_and_rejects_changed_evidence(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _target(settings.scenario_root)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        source = _source(client)
        relation = _request(client, source).json()
        assert _wait(client, relation["holdout_job_id"])["status"] == "succeeded"
    from ea.web.service import WebService

    service = WebService(settings.scenario_root, settings.workspace)
    service.start()
    try:
        record = service.create_candidate(relation["validation_id"])
        accepted = service.decide_candidate(
            record["candidate_id"], "ACCEPTED", "Retain for further research"
        )
        assert accepted["status"] == "ACCEPTED"
        assert (
            service.decide_candidate(
                record["candidate_id"], "ACCEPTED", "Retain for further research"
            )
            == accepted
        )
    finally:
        service.stop()
    for path in settings.scenario_root.glob("*.csv"):
        path.unlink()
    service = WebService(settings.scenario_root, settings.workspace)
    service.start()
    try:
        assert service.get_candidate(record["candidate_id"]) == accepted
        report = settings.workspace / "reports" / source["job_id"] / "report.json"
        report.write_bytes(report.read_bytes() + b" ")
        with pytest.raises(ValueError):
            service.get_candidate(record["candidate_id"])
        with pytest.raises(ValueError):
            service.decide_candidate(
                record["candidate_id"], "ACCEPTED", "Retain for further research"
            )
    finally:
        service.stop()


def test_candidate_api_requires_explicit_decision_and_reports_unavailable(tmp_path: Path) -> None:
    from unit.test_web_api import WRITE_HEADERS

    settings = _settings(tmp_path)
    _target(settings.scenario_root)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        source = _source(client)
        relation = _request(client, source).json()
        _wait(client, relation["holdout_job_id"])
        created = client.post(
            "/api/candidates",
            json={"validation_id": relation["validation_id"]},
            headers=WRITE_HEADERS,
        )
        assert created.status_code == 201, created.text
        record = created.json()
        url = "/api/candidates/" + record["candidate_id"]
        assert client.get(url).json() == record
        assert (
            client.post(
                url + "/decision",
                json={"outcome": "ACCEPTED", "reason": " "},
                headers=WRITE_HEADERS,
            ).status_code
            == 409
        )
        response = client.post(
            url + "/decision",
            json={"outcome": "REJECTED", "reason": "Insufficient evidence quality"},
            headers=WRITE_HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "REJECTED"
        assert client.get("/api/candidates").json()["candidates"][0]["status"] == "REJECTED"
        path = settings.workspace / "candidates" / (record["candidate_id"] + ".json")
        path.write_bytes(b"{}\n")
        assert client.get(url).status_code == 409
        assert client.get("/api/candidates").json()["candidates"][0]["status"] == "UNAVAILABLE"
        assert client.get("/api/backtests/" + source["job_id"] + "/report").status_code == 200


@pytest.mark.parametrize(
    "field",
    ["scenario_sha256", "data_sha256", "report_sha256", "run_id", "semantic_outcome_sha256"],
)
def test_candidate_rejects_cross_evidence_conflict_even_with_updated_file_hash(
    tmp_path: Path, field: str
) -> None:
    import json
    from hashlib import sha256

    from ea.web.candidates import canonical
    from ea.web.service import WebService

    settings = _settings(tmp_path)
    _target(settings.scenario_root)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        source = _source(client)
        relation = _request(client, source).json()
        _wait(client, relation["holdout_job_id"])
    path = settings.workspace / "reports" / source["job_id"] / "equity-path.json"
    document = json.loads(path.read_bytes())
    document[field] = "0" * 64
    path.write_bytes(canonical(document))
    job_path = settings.workspace / "jobs" / (source["job_id"] + ".json")
    job = json.loads(job_path.read_bytes())
    job["equity_path_sha256"] = sha256(path.read_bytes()).hexdigest()
    job_path.write_bytes(canonical(job))
    service = WebService(settings.scenario_root, settings.workspace)
    service.start()
    try:
        with pytest.raises(ValueError):
            service.create_candidate(relation["validation_id"])
        assert not list((settings.workspace / "candidates").glob("*.json"))
    finally:
        service.stop()


def test_failed_candidate_decision_publish_preserves_evaluated_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ea.web.service as module

    settings = _settings(tmp_path)
    _target(settings.scenario_root)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        source = _source(client)
        relation = _request(client, source).json()
        _wait(client, relation["holdout_job_id"])
    service = module.WebService(settings.scenario_root, settings.workspace)
    service.start()
    try:
        record = service.create_candidate(relation["validation_id"])
        original_replace = module.os.replace

        def fail_candidate(source: object, destination: object) -> None:
            if "/candidates/" in str(destination):
                raise OSError("injected publication failure")
            original_replace(source, destination)

        monkeypatch.setattr(module.os, "replace", fail_candidate)
        with pytest.raises(OSError):
            service.decide_candidate(record["candidate_id"], "ACCEPTED", "Retain")
        assert service.get_candidate(record["candidate_id"]) == record
        assert len(list(service.candidates_dir.iterdir())) == 1
    finally:
        service.stop()
