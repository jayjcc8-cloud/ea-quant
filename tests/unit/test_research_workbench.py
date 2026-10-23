"""Product-shaped collection reads; commands retain their existing authority."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ea.web.app import create_app
from ea.web.user_strategies import UserStrategyStore
from unit.test_web_api import ORIGIN, WRITE_HEADERS, _settings


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("RADIAN_SETTINGS_FILE", str(tmp_path / "settings.json"))
    with TestClient(create_app(_settings(tmp_path)), base_url=ORIGIN) as value:
        yield value


def idea(client: TestClient, name: str, exchange: str | None = None) -> Any:
    response = client.post(
        "/api/user-strategies",
        headers=WRITE_HEADERS,
        json={"name": name, "intent": "验证真实问题", "exchange_id": exchange},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_collection_is_compact_searchable_and_exchange_scoped(client: TestClient) -> None:
    first = idea(client, "均线研究", "binance")
    idea(client, "成本研究", "okx")
    idea(client, "历史想法")
    result = client.get("/api/research", params={"exchange_id": "binance", "q": "均线"})
    assert result.status_code == 200, result.text
    collection = result.json()
    assert collection["total"] == 3 and collection["matched"] == 1
    item = collection["items"][0]
    assert item["user_strategy_id"] == first["user_strategy_id"]
    assert item["exchange_id"] == "binance"
    assert item["draft"] == {"kind": "intent", "revision": 1, "pending_proposals": 0}
    assert item["latest_experiment"] is None and item["next_step"] == "draft"
    assert not {"messages", "calls", "history", "experiments", "parameters"}.intersection(item)
    assert client.get("/api/research", params={"exchange_id": "unknown"}).status_code == 422
    assert client.get("/api/research", params={"sort": "anything"}).status_code == 422


def test_exchange_assignment_is_metadata_and_rejects_stale_writes(client: TestClient) -> None:
    saved = idea(client, "旧研究")
    identity = saved["user_strategy_id"]
    request = {"exchange_id": "okx", "expected_updated_at": saved["updated_at"]}
    response = client.patch(
        f"/api/research/{identity}/exchange", headers=WRITE_HEADERS, json=request
    )
    assert response.status_code == 200, response.text
    changed = response.json()
    assert changed["exchange_id"] == "okx"
    assert changed["draft"] == saved["draft"]
    assert changed["experiments"] == [] and changed["calls"] == []
    stale = client.patch(
        f"/api/research/{identity}/exchange",
        headers=WRITE_HEADERS,
        json={**request, "exchange_id": "binance"},
    )
    assert stale.status_code == 409
    assert client.get("/api/research", params={"exchange_id": "okx"}).json()["matched"] == 1
    assert client.get(f"/api/user-strategies/{identity}").json()["exchange_id"] == "okx"


def test_unreadable_job_is_unknown_and_references_preserve_ambiguity(
    client: TestClient,
    tmp_path: Path,
) -> None:
    saved = idea(client, "实验状态")
    store = UserStrategyStore(tmp_path / "workspace")
    record = store.get(saved["user_strategy_id"])
    experiment = {
        "launch_intent_id": "a",
        "job_id": "missing",
        "status": "created",
        "payload": {"draft_revision": 1, "scenario_id": "private.yaml"},
    }
    store.save(replace(record, experiments=(experiment, {**experiment, "launch_intent_id": "b"})))
    summary = client.get("/api/research").json()["items"][0]
    assert summary["latest_experiment"]["job_status"] is None
    assert summary["latest_experiment"]["read_error"]
    assert summary["next_step"] == "inspect"
    references = client.get("/api/research/references", params={"job_id": "missing"}).json()[
        "items"
    ]
    assert len(references[0]["experiments"]) == 2
    assert "messages" not in references[0]
    assert client.get("/api/research/references", params={"job_id": "other"}).json()["items"] == []


def test_retired_read_routes_are_not_compatibility_aliases(client: TestClient) -> None:
    for path in ("/api/user-strategies", "/api/workspace/overview", "/api/search?q=aa"):
        assert client.get(path).status_code in {404, 405}


@pytest.mark.parametrize(
    ("owner_status", "job_status", "next_step"),
    [
        ("confirmed", "accepted", "inspect"),
        ("running", "running", "inspect"),
        ("stopping", "stopping", "inspect"),
        ("failed", "failed", "inspect"),
    ],
)
def test_core_observations_drive_the_collection_without_dispatch(
    client: TestClient,
    owner_status: str,
    job_status: str,
    next_step: str,
) -> None:
    from unittest.mock import Mock

    from ea.web.research_workbench import ResearchWorkbench
    from ea.web.user_strategies import UserStrategy

    record = UserStrategy.decode(idea(client, "所有者状态"))
    observed = replace(
        record,
        experiments=(
            {
                "owner": "quine",
                "status": owner_status,
                "job_id": "core-job",
                "payload": {"draft_revision": 1},
                "report_ready": False,
            },
        ),
    )
    strategies, jobs = Mock(), Mock()
    strategies.list.return_value = [observed]
    summary = ResearchWorkbench(strategies, jobs).collection()["items"][0]
    assert summary["latest_experiment"]["job_status"] == job_status
    assert summary["latest_experiment"]["report_ready"] is False
    assert summary["next_step"] == next_step
    strategies.list.assert_called_once_with()
    jobs.get_job.assert_not_called()


def test_registered_success_requires_readable_report_and_exact_job(client: TestClient) -> None:
    from unittest.mock import Mock

    from ea.web.research_workbench import ResearchWorkbench
    from ea.web.user_strategies import UserStrategy

    record = UserStrategy.decode(idea(client, "报告身份"))
    strategies, jobs = Mock(), Mock()
    jobs.get_job.return_value = Mock(job_id="core-job", status="succeeded")
    workbench = ResearchWorkbench(strategies, jobs)
    for ready in (False, True):
        observed = replace(
            record,
            experiments=(
                {
                    "owner": "quine",
                    "status": "created",
                    "job_id": "core-job",
                    "payload": {"draft_revision": 1},
                    "report_ready": ready,
                },
            ),
        )
        summary = workbench.summary(observed)
        assert summary["latest_experiment"]["job_status"] == "succeeded"
        assert summary["latest_experiment"]["report_ready"] is ready
        assert summary["next_step"] == ("evidence" if ready else "inspect")
    jobs.get_job.return_value = Mock(job_id="wrong-job", status="succeeded")
    summary = workbench.summary(observed)
    assert summary["latest_experiment"]["job_status"] is None
    assert summary["latest_experiment"]["read_error"]
    assert not summary["latest_experiment"]["report_ready"]


def test_old_records_remain_unassigned_and_sort_is_stable(client: TestClient) -> None:
    from ea.web.user_strategies import UserStrategy

    saved = idea(client, "Zebra")
    saved.pop("exchange_id")
    assert UserStrategy.decode(saved).exchange_id is None
    idea(client, "Alpha")
    assert [item["name"] for item in client.get("/api/research?sort=name").json()["items"]] == [
        "Alpha",
        "Zebra",
    ]
