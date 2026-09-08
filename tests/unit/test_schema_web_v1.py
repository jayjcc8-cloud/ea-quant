from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from ea.core import ReplayWindow
from ea.data import decode_phase1_ohlcv_csv
from ea.web.app import create_app
from unit.test_web_api import ORIGIN, WRITE_HEADERS, _settings, _validate, _wait


def test_ma_batch_holdout_full_map_defaults_and_restart(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    for path in Path("examples/web-scenarios").glob("moving-average-*"):
        shutil.copy(path, settings.scenario_root / path.name)
    later = settings.scenario_root / "moving-average-holdout.yaml"
    document = yaml.safe_load(later.read_text())
    document["strategy"]["parameters"] = {
        "fast_window": 1,
        "slow_window": 2,
        "target_quantity": "1",
    }
    later.write_text(yaml.safe_dump(document))
    parameters = {"fast_window": 2, "slow_window": 3, "target_quantity": "3"}
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        registered = _validate(client, "moving-average-entry.yaml")
        request: dict[str, Any] = dict(
            scenario_id="moving-average-entry.yaml",
            input_identity=registered["input_identity"],
            initial_cash="10000",
            runs=[
                {"strategy_parameters": parameters},
                {"strategy_parameters": {**parameters, "target_quantity": "4"}},
            ],
        )
        duplicate = {
            **request,
            "runs": [
                {"strategy_parameters": parameters},
                {"strategy_parameters": dict(reversed(list(parameters.items())))},
            ],
        }
        assert client.post("/api/batches", json=duplicate, headers=WRITE_HEADERS).status_code == 422
        response = client.post("/api/batches", json=request, headers=WRITE_HEADERS)
        assert response.status_code == 202, response.text
        batch_id = response.json()["batch_id"]
        members = [_wait(client, identity) for identity in response.json()["member_job_ids"]]
        assert all(m["status"] == "succeeded" for m in members)
        assert members[0]["parameters"] == parameters
        frozen = members[0]["input_snapshot"]
        source = members[0]["job_id"]
        response = client.post(
            "/api/holdouts",
            headers=WRITE_HEADERS,
            json={"source_job_id": source, "scenario_id": later.name},
        )
        assert response.status_code == 202, response.text
        relation = response.json()
        holdout = _wait(client, relation["holdout_job_id"])
        assert holdout["status"] == "succeeded"
        assert holdout["parameters"] == parameters
        assert holdout["attempt_id"] != members[0]["attempt_id"]
        report = client.get(f"/api/backtests/{holdout['job_id']}/report").json()
        assert report["economics"]["fees"]["amount"] != "0"
        before = client.get(f"/api/batches/{batch_id}").json()
        # Target defaults validate on the smaller window; frozen slow=3 cannot.
        document["data"]["end_utc"] = "2026-01-03T09:34:00.000000Z"
        selection = decode_phase1_ohlcv_csv(
            (settings.scenario_root / document["data"]["path"]).read_bytes(),
            replay_window=ReplayWindow(
                datetime.fromisoformat(document["data"]["start_utc"]),
                datetime.fromisoformat(document["data"]["end_utc"]),
            ),
        ).selection
        document["data"]["fingerprint"] = {
            "sha256": selection.fingerprint.sha256.value,
            "record_count": selection.fingerprint.record_count,
        }
        later.write_text(yaml.safe_dump(document))
        assert _validate(client, later.name)["valid"]
        response = client.post(
            "/api/holdouts",
            headers=WRITE_HEADERS,
            json={"source_job_id": source, "scenario_id": later.name},
        )
        assert response.status_code == 422
        assert len(client.get("/api/backtests").json()["jobs"]) == 3

    persisted = {str(p): p.read_bytes() for p in settings.workspace.rglob("holdout.json")}
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        assert client.get(f"/api/holdouts/{relation['validation_id']}").json() == relation
        assert client.get(f"/api/backtests/{holdout['job_id']}/report").json() == report
        assert client.get(f"/api/batches/{batch_id}").json() == before
        assert client.get(f"/api/backtests/{source}").json()["input_snapshot"] == frozen
    assert {str(p): p.read_bytes() for p in settings.workspace.rglob("holdout.json")} == persisted
