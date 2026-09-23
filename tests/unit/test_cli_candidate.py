from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ea.cli.app import app
from ea.web.app import create_app
from unit.test_web_api import ORIGIN, WRITE_HEADERS, _settings, _wait
from unit.test_web_holdout import _request, _source, _target


def test_candidate_cli_requires_acceptance_and_binds_real_offline_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _target(settings.scenario_root)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        source = _source(client)
        relation = _request(client, source).json()
        assert _wait(client, relation["holdout_job_id"])["status"] == "succeeded"
        response = client.post(
            "/api/candidates",
            json={"validation_id": relation["validation_id"]},
            headers=WRITE_HEADERS,
        )
        assert response.status_code == 201
        candidate_id = response.json()["candidate_id"]
        runner = CliRunner()
        selection = ["--workspace", str(settings.workspace), "--candidate-id", candidate_id]
        rejected = runner.invoke(app, ["candidate", "inspect", *selection])
        assert rejected.exit_code == 3
        accepted = client.post(
            f"/api/candidates/{candidate_id}/decision",
            json={"outcome": "ACCEPTED", "reason": "Explicit offline acceptance scenario"},
            headers=WRITE_HEADERS,
        )
        assert accepted.status_code == 200

    before = {
        str(path.relative_to(settings.workspace)): path.read_bytes()
        for path in settings.workspace.rglob("*")
        if path.is_file()
    }
    inspected = runner.invoke(app, ["candidate", "inspect", *selection])
    assert inspected.exit_code == 0, inspected.output
    assert json.loads(inspected.stdout)["candidate_id"] == candidate_id
    assert {
        str(path.relative_to(settings.workspace)): path.read_bytes()
        for path in settings.workspace.rglob("*")
        if path.is_file()
    } == before

    outputs = tmp_path / "candidate-runs"
    executed = runner.invoke(
        app,
        [
            "candidate",
            "run",
            *selection,
            "--scenario",
            str(settings.scenario_root / "bounded-long.yaml"),
            "--output-root",
            str(outputs),
        ],
    )
    assert executed.exit_code == 0, executed.output
    attempt = next(path for path in outputs.iterdir() if path.is_dir())
    binding = json.loads((attempt / "candidate-binding.json").read_bytes())
    assert binding["candidate_id"] == candidate_id
    events = [
        json.loads(line) for line in (attempt / "operational.jsonl").read_bytes().splitlines()
    ]
    assert events and {event["candidate_id"] for event in events} == {candidate_id}
    result = json.loads((attempt / "result.json").read_bytes())
    assert result["status"] == "success"
    assert result["ledger_sequence"] == 2
