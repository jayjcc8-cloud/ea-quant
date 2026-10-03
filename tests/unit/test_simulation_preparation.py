"""Synthetic short windows validate the bridge, never economic approval."""

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ea.product.paper_run import run_local_paper
from ea.web.app import WebSettings, create_app
from unit.test_web_api import ORIGIN, WRITE_HEADERS, _wait


def test_price_candidate_preparation_rejects_start_even_after_explicit_acceptance(
    tmp_path: Path,
) -> None:
    root = tmp_path / "scenarios"
    root.mkdir()
    examples = Path(__file__).parents[2] / "examples" / "web-scenarios"
    for name in (
        "moving-average-entry.csv",
        "moving-average-holdout.csv",
        "price-rule-entry.yaml",
        "price-rule-holdout.yaml",
    ):
        shutil.copyfile(examples / name, root / name)
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<title>Fixture</title>")
    settings = WebSettings(
        scenario_root=root, workspace=tmp_path / "workspace", ui_dir=ui, port=8765
    )
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        identity = client.post(
            "/api/scenarios/price-rule-entry.yaml/validate", headers=WRITE_HEADERS
        ).json()["input_identity"]
        source = client.post(
            "/api/backtests",
            headers=WRITE_HEADERS,
            json={
                "scenario_id": "price-rule-entry.yaml",
                "input_identity": identity,
                "request_id": "simulation-fixture",
            },
        ).json()
        assert _wait(client, source["job_id"])["status"] == "succeeded"
        relation = client.post(
            "/api/holdouts",
            headers=WRITE_HEADERS,
            json={
                "source_job_id": source["job_id"],
                "scenario_id": "price-rule-holdout.yaml",
            },
        ).json()
        assert _wait(client, relation["holdout_job_id"])["status"] == "succeeded"
        candidate = client.post(
            "/api/candidates",
            headers=WRITE_HEADERS,
            json={
                "validation_id": relation["validation_id"],
            },
        ).json()
        path = f"/api/candidates/{candidate['candidate_id']}/simulation-preparation"
        before = {p: p.read_bytes() for p in settings.workspace.rglob("*") if p.is_file()}
        preparation = client.get(path).json()
        assert preparation["candidate_status"] == "EVALUATED"
        assert preparation["scenario_version"] == 2
        assert preparation["blockers"] == [
            "explicit_research_acceptance_required",
            "unsupported_paper_profile_requires_action_v2_v4_v5",
        ]
        assert preparation["binding"] is None
        assert not preparation["eligible"]
        assert not preparation["execution_authorized"]
        assert preparation["small_sample_warning"]
        assert client.get(path).json() == preparation
        assert {p: p.read_bytes() for p in settings.workspace.rglob("*") if p.is_file()} == before
        accepted = client.post(
            f"/api/candidates/{candidate['candidate_id']}/decision",
            headers=WRITE_HEADERS,
            json={"outcome": "ACCEPTED", "reason": "Task-private fixture plumbing only"},
        )
        assert accepted.status_code == 200
        prepared = client.get(path).json()
        assert prepared["blockers"] == ["unsupported_paper_profile_requires_action_v2_v4_v5"]
        assert prepared["binding"]["candidate_id"] == candidate["candidate_id"]
        assert not prepared["eligible"] and not prepared["start_available"]
        assert prepared["runtime_state"] == "not_started"
        # The real shared runtime rejects this exact test-only accepted profile.
        with pytest.raises(ValueError, match="Action V2 long-only V4/V5"):
            run_local_paper(
                settings.workspace,
                candidate["candidate_id"],
                root / "price-rule-entry.yaml",
                tmp_path / "simulation-runs",
                event_limit=2,
            )
        assert list((tmp_path / "simulation-runs").iterdir()) == []
        assert client.post(path, headers=WRITE_HEADERS).status_code == 405
        report = settings.workspace / "reports" / source["job_id"] / "report.json"
        report.write_bytes(report.read_bytes() + b" ")
        assert client.get(path).status_code == 409
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        assert client.get(path).status_code == 409
