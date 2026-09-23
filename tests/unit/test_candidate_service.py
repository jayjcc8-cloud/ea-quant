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
    [
        "scenario_sha256",
        "data_sha256",
        "report_sha256",
        "run_id",
        "semantic_outcome_sha256",
        "currency",
        "incomplete",
    ],
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
    if field == "incomplete":
        document.pop("display_points")
    else:
        document[field] = "EUR" if field == "currency" else "0" * 64
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
    import os

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
        original_replace = os.replace

        def fail_candidate(source: Path, destination: Path) -> None:
            if "/candidates/" in str(destination):
                raise OSError("injected publication failure")
            original_replace(source, destination)

        monkeypatch.setattr(os, "replace", fail_candidate)
        with pytest.raises(OSError):
            service.decide_candidate(record["candidate_id"], "ACCEPTED", "Retain")
        assert service.get_candidate(record["candidate_id"]) == record
        assert len(list(service.candidates_dir.iterdir())) == 1
    finally:
        service.stop()


@pytest.mark.parametrize("version", [2, 3])
def test_round_trip_candidate_binds_nominal_report_and_path_versions(
    tmp_path: Path, version: int
) -> None:
    import json
    from datetime import datetime
    from hashlib import sha256

    import yaml

    from ea.core import ReplayWindow
    from ea.data import decode_phase1_ohlcv_csv
    from ea.web.candidates import canonical
    from ea.web.service import WebService
    from unit.test_bounded_round_trips_v1 import bounded_scenario
    from unit.test_single_round_trip_v1 import closed_scenario

    source_path = (
        closed_scenario(tmp_path / "scenarios")
        if version == 2
        else bounded_scenario(tmp_path / "scenarios")
    )
    root = source_path.parent
    source_document = yaml.safe_load(source_path.read_text())
    target_document = json.loads(json.dumps(source_document).replace("2026-01-02", "2026-01-03"))
    payload = (root / "prices.csv").read_bytes().replace(b"2026-01-02", b"2026-01-03")
    (root / "later.csv").write_bytes(payload)
    target_document["data"]["path"] = "later.csv"
    dataset = decode_phase1_ohlcv_csv(
        payload,
        replay_window=ReplayWindow(
            datetime.fromisoformat(target_document["data"]["start_utc"]),
            datetime.fromisoformat(target_document["data"]["end_utc"]),
        ),
    )
    target_document["data"]["fingerprint"] = {
        "sha256": dataset.selection.fingerprint.sha256.value,
        "record_count": dataset.selection.fingerprint.record_count,
    }
    (root / "later.yaml").write_text(yaml.safe_dump(target_document))
    service = WebService(root, tmp_path / "workspace")
    service.start()
    try:
        summary = service.validate_scenario(source_path.name)
        identity = summary["input_identity"]
        assert isinstance(identity, dict)
        source, _ = service.create_job(
            scenario_id=source_path.name,
            input_identity=identity,
            request_id="candidate-version-source",
        )
    finally:
        service.stop()
    assert service.get_job(source.job_id).status == "succeeded"
    service = WebService(root, tmp_path / "workspace")
    service.start()
    try:
        relation = service.create_holdout(source_job_id=source.job_id, scenario_id="later.yaml")
    finally:
        service.stop()
    assert service.get_job(relation["holdout_job_id"]).status == "succeeded"
    service = WebService(root, tmp_path / "workspace")
    service.start()
    try:
        record = service.create_candidate(relation["validation_id"])
        duplicate = service.create_candidate(relation["validation_id"])
        assert record["status"] == "EVALUATED"
        assert record["candidate_id"] != duplicate["candidate_id"]
        assert record["fingerprint"] == duplicate["fingerprint"]
        for role in ("source", "holdout"):
            evidence = record["projection"][role]
            job = service.get_job(evidence["job_id"])
            assert job.schema == f"ea.local-web-job.v{version + 2}"
            report = json.loads(service.report(job.job_id))
            path = json.loads(service.artifact(job.job_id, "equity-path.json"))
            assert report["schema"] == f"ea.backtest-report.v{version}"
            assert path["schema"] == f"ea.backtest-equity-path.v{version}"
            assert report["run_id"] == path["run_id"] == evidence["run_id"]
    finally:
        service.stop()
    for csv in root.glob("*.csv"):
        csv.unlink()
    service = WebService(root, tmp_path / "workspace")
    service.start()
    try:
        assert service.get_candidate(record["candidate_id"]) == record
        path_file = service.reports_dir / source.job_id / "equity-path.json"
        wrong_path = json.loads(path_file.read_bytes())
        wrong_version = 2 if version == 3 else 3
        wrong_path["schema"] = f"ea.backtest-equity-path.v{wrong_version}"
        wrong_path["schema_version"] = wrong_version
        path_file.write_bytes(canonical(wrong_path))
        job_file = service.jobs_dir / f"{source.job_id}.json"
        wrong_job = json.loads(job_file.read_bytes())
        wrong_job["equity_path_sha256"] = sha256(path_file.read_bytes()).hexdigest()
        job_file.write_bytes(canonical(wrong_job))
        with pytest.raises(ValueError):
            service.create_candidate(relation["validation_id"])
        with pytest.raises(ValueError):
            service.decide_candidate(record["candidate_id"], "ACCEPTED", "Retain")
        assert all(item["status"] == "UNAVAILABLE" for item in service.list_candidates())
        assert service.report(source.job_id)
    finally:
        service.stop()
