from __future__ import annotations

from dataclasses import replace
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from ea.web.app import WebSettings, create_app
from ea.web.llm import ModelResponse
from ea.web.research import (
    BudgetExceededError,
    InvalidSpecError,
    ResearchOrchestrator,
    ResearchStore,
    ResearchTask,
    TaskConflictError,
    TaskNotFoundError,
    TaskStateError,
)
from ea.web.service import WebService
from ea.web.settings import RadianSettings

pytestmark = pytest.mark.filterwarnings(
    "ignore:The anyio.abc.BlockingPortal alias is deprecated:DeprecationWarning"
)

ORIGIN = "http://127.0.0.1:8765"
HOST = {"host": "127.0.0.1:8765"}
WRITE_HEADERS = {
    **HOST,
    "origin": ORIGIN,
    "x-ea-web-request": "1",
    "content-type": "application/json",
}


def _scenario_root(tmp_path: Any) -> Any:
    from unit.test_backtest_report import _priced_scenario

    root = tmp_path / "scenarios"
    source = _priced_scenario(root)
    source.rename(root / "bounded-long.yaml")
    document = yaml.safe_load((root / "bounded-long.yaml").read_text(encoding="utf-8"))
    flat = dict(document)
    flat["strategy"] = {"id": "always-flat-v1"}
    (root / "flat.yaml").write_text(yaml.safe_dump(flat, sort_keys=False), encoding="utf-8")
    return root


def _service(tmp_path: Any) -> WebService:
    service = WebService(_scenario_root(tmp_path), tmp_path / "workspace")
    service.start()
    return service


def _settings_store(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> RadianSettings:
    path = tmp_path / "settings.json"
    monkeypatch.setenv("RADIAN_SETTINGS_FILE", str(path))
    return RadianSettings(path)


class FakeProvider:
    """Scripted provider: no network, explicit per-call responses."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(self, **kwargs: Any) -> ModelResponse:
        # Snapshot: the orchestrator keeps mutating its messages list between
        # iterations, and assertions read the state at each call time.
        snapshot = dict(kwargs)
        snapshot["messages"] = [dict(item) for item in kwargs.get("messages") or []]
        self.calls.append(snapshot)
        response: ModelResponse | BaseException = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _response(tool_calls: list[dict[str, Any]], **extra: Any) -> ModelResponse:
    defaults: dict[str, Any] = dict(
        text="",
        stop_reason="tool_use" if tool_calls else "end_turn",
        model="fake-model",
        input_tokens=11,
        output_tokens=7,
    )
    defaults.update(extra)
    return ModelResponse(tool_calls=tuple(tool_calls), **defaults)


def _orchestrator(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> ResearchOrchestrator:
    """Orchestrator with the real provider; no model is configured by default."""
    store = ResearchStore(tmp_path / "workspace")
    service = _service(tmp_path)
    return ResearchOrchestrator(store, service, _settings_store(tmp_path, monkeypatch))


def _fake_orchestrator(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, responses: list[Any]
) -> tuple[ResearchOrchestrator, WebService, FakeProvider]:
    """Orchestrator with a scripted provider that never touches the network."""
    store = ResearchStore(tmp_path / "workspace")
    service = _service(tmp_path)
    orchestrator = ResearchOrchestrator(store, service, _settings_store(tmp_path, monkeypatch))
    provider = FakeProvider(responses)
    cast(Any, orchestrator)._provider = provider  # test seam, same interface
    return orchestrator, service, provider


def _propose_call(
    catalog_strategy: str, catalog: list[dict[str, Any]], **overrides: Any
) -> dict[str, Any]:
    entry = next(
        item for item in catalog if item["strategy_id"] == catalog_strategy and item["valid"]
    )
    instrument = entry["instrument"] or {"venue": "XNAS", "symbol": "AAPL"}
    call = {
        "strategy_id": catalog_strategy,
        "instrument": instrument,
        "initial_cash": "1000",
        "rationale": "deterministic fixture run",
        "assumptions": ["fixture data only"],
    }
    call.update(overrides)
    return call


# --- store basics ------------------------------------------------------------


def test_store_roundtrip_and_dedup(tmp_path: Any) -> None:
    store = ResearchStore(tmp_path)
    now = "2026-09-29T00:00:00.000000Z"
    task = ResearchTask(
        task_id=str(uuid4()),
        created_at=now,
        updated_at=now,
        status="drafting",
        raw_idea="Test the bounded long strategy on AAPL",
        gap=None,
        budget={"max_model_calls": 10, "max_backtests": 5},
        usage={"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "backtests": 0},
        spec_draft=None,
        spec_confirmed=(),
        jobs=(),
        request_id=None,
        error=None,
        cancel_note=None,
        last_model=None,
    )
    store.save(task)
    loaded = store.get(task.task_id)
    assert loaded.raw_idea == task.raw_idea
    assert loaded.status == "drafting"
    assert [item.task_id for item in store.list()] == [task.task_id]
    assert store.find_active_by_idea("  Test   the bounded long strategy on AAPL ") is not None
    assert store.find_active_by_idea("something else") is None


# --- proposal flow -----------------------------------------------------------


def test_create_without_model_configuration_fails_honestly(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator = _orchestrator(tmp_path, monkeypatch)
    task = orchestrator.create("Trade AAPL momentum")
    assert task.status == "failed"
    assert task.error == {
        "step": "propose",
        "code": "model_not_configured",
        "message": "no model provider is configured",
    }
    assert task.raw_idea == "Trade AAPL momentum"
    assert task.usage["model_calls"] == 0


def test_proposal_succeeds_with_valid_tool_call(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, _service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    proposal = _response(
        [{"id": "call-1", "name": "propose_spec", "input": _propose_call(strategy, catalog)}]
    )
    provider.responses.append(proposal)
    task = orchestrator.create("Run the default long-only strategy on the fixture instrument")
    assert task.status == "awaiting_confirmation"
    assert task.spec_draft is not None
    assert task.spec_draft.strategy_id == strategy
    assert task.usage["model_calls"] == 1
    assert task.usage["input_tokens"] == 11
    assert task.usage["output_tokens"] == 7


def test_proposal_declares_unsupported_and_keeps_gap(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, _service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    provider.responses.append(
        _response(
            [
                {
                    "id": "call-1",
                    "name": "declare_unsupported",
                    "input": {"gap": "no BTC data registered"},
                }
            ]
        )
    )
    task = orchestrator.create("Backtest a BTC breakout")
    assert task.status == "unsupported"
    assert task.gap == "no BTC data registered"
    assert task.error is None
    assert task.raw_idea == "Backtest a BTC breakout"


def test_proposal_rejects_unknown_strategy_then_succeeds(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, _service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    instrument = catalog[0]["instrument"] or {"venue": "XNAS", "symbol": "AAPL"}
    provider.responses.append(
        _response(
            [
                {
                    "id": "call-1",
                    "name": "propose_spec",
                    "input": {
                        "strategy_id": "made-up-strategy",
                        "instrument": instrument,
                        "initial_cash": "1000",
                        "rationale": "x",
                    },
                }
            ]
        )
    )
    provider.responses.append(
        _response(
            [
                {
                    "id": "call-2",
                    "name": "propose_spec",
                    "input": _propose_call(strategy, catalog),
                }
            ]
        )
    )
    task = orchestrator.create("Eventually valid idea")
    assert task.status == "awaiting_confirmation"
    assert task.usage["model_calls"] == 2
    assert len(provider.calls) == 2
    # The second request carried the rejection feedback as tool results.
    feedback = provider.calls[1]["messages"][-1]["content"]
    assert "rejected" in str(feedback)


def test_proposal_without_tool_call_fails_without_inventing_a_spec(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, _service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    provider.responses.append(_response([], text="I cannot help with trading."))
    task = orchestrator.create("Do something with markets")
    assert task.status == "failed"
    assert task.error is not None and task.error["code"] == "no_tool_call"
    assert task.spec_draft is None


# --- confirmation and the real backtest path ---------------------------------


def test_confirm_runs_a_real_backtest_and_tracks_usage(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    proposal = _response(
        [{"id": "call-1", "name": "propose_spec", "input": _propose_call(strategy, catalog)}]
    )
    provider.responses.append(proposal)
    task = orchestrator.create("Confirm and run the fixture strategy")
    assert task.status == "awaiting_confirmation"

    confirmed = orchestrator.confirm(task.task_id)
    assert confirmed.status in {"running", "succeeded"}
    assert confirmed.usage["backtests"] == 1
    assert len(confirmed.jobs) == 1
    job = service.get_job(confirmed.jobs[0])
    assert job.scenario_id in {"bounded-long.yaml", "flat.yaml"}
    assert confirmed.request_id is not None


def test_confirm_without_run_keeps_confirmed_spec(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, _service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    proposal = _response(
        [{"id": "call-1", "name": "propose_spec", "input": _propose_call(strategy, catalog)}]
    )
    provider.responses.append(proposal)
    task = orchestrator.create("Spec only")
    confirmed = orchestrator.confirm(task.task_id, auto_run=False)
    assert confirmed.status == "confirmed"
    assert confirmed.jobs == ()
    assert len(confirmed.spec_confirmed) == 1
    assert confirmed.spec_confirmed[0][0] == 1
    assert confirmed.spec_draft is None


def test_edit_spec_is_validated_against_the_catalog(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, _service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    proposal = _response(
        [{"id": "call-1", "name": "propose_spec", "input": _propose_call(strategy, catalog)}]
    )
    provider.responses.append(proposal)
    task = orchestrator.create("Editable draft")
    with pytest.raises(InvalidSpecError):
        orchestrator.edit_spec(task.task_id, {"strategy_id": "not-registered"})
    edited = orchestrator.edit_spec(
        task.task_id, {"initial_cash": "5000", "rationale": "higher cash"}
    )
    assert edited.spec_draft is not None
    assert edited.spec_draft.initial_cash == "5000"
    assert edited.spec_draft.rationale == "higher cash"


# --- cancel, retry, budget ----------------------------------------------------


def test_cancel_blocks_retry_and_confirm(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    orchestrator, _service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    proposal = _response(
        [{"id": "call-1", "name": "propose_spec", "input": _propose_call(strategy, catalog)}]
    )
    provider.responses.append(proposal)
    task = orchestrator.create("Cancel me before running")
    cancelled = orchestrator.cancel(task.task_id)
    assert cancelled.status == "cancelled"
    assert orchestrator.cancel(task.task_id).status == "cancelled"  # idempotent
    with pytest.raises(TaskStateError):
        orchestrator.retry(task.task_id)
    with pytest.raises(TaskStateError):
        orchestrator.confirm(task.task_id)


def test_retry_only_for_failed_tasks_and_reuses_the_same_task(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator = _orchestrator(tmp_path, monkeypatch)
    task = orchestrator.create("Retry will still fail without a model")
    assert task.status == "failed"
    with pytest.raises(TaskNotFoundError):
        orchestrator.retry("missing-task-id")
    retried = orchestrator.retry(task.task_id)
    assert retried.task_id == task.task_id
    assert retried.status == "failed"
    assert retried.error is not None and retried.error["step"] == "propose"


def test_model_call_budget_is_enforced(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    orchestrator = _orchestrator(tmp_path, monkeypatch)
    task = orchestrator.create("budget probe")  # fails on missing model, but task exists
    exhausted = replace(
        task, usage={"model_calls": 10, "input_tokens": 0, "output_tokens": 0, "backtests": 0}
    )
    with pytest.raises(BudgetExceededError):
        orchestrator._charged_usage(exhausted, ModelResponse("", (), "end_turn", "m", 1, 1))  # noqa: SLF001


def test_duplicate_active_idea_is_rejected(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    orchestrator, _service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    proposal = _response(
        [{"id": "call-1", "name": "propose_spec", "input": _propose_call(strategy, catalog)}]
    )
    provider.responses.append(proposal)
    first = orchestrator.create("One active idea only")
    assert first.status == "awaiting_confirmation"
    with pytest.raises(TaskConflictError):
        orchestrator.create("One active idea only")
    with pytest.raises(TaskConflictError):
        orchestrator.create("  One   active idea only ")


# --- API surface -------------------------------------------------------------


def _app_settings(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> WebSettings:
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>RADIAN</title>", encoding="utf-8")
    return WebSettings(
        scenario_root=_scenario_root(tmp_path),
        workspace=tmp_path / "workspace",
        ui_dir=ui,
        port=8765,
    )


def test_research_api_without_model_reports_not_configured(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.json"
    monkeypatch.setenv("RADIAN_SETTINGS_FILE", str(path))
    app = create_app(_app_settings(tmp_path, monkeypatch))
    client = TestClient(app)
    with client:
        response = client.post(
            "/api/research/tasks", headers=WRITE_HEADERS, json={"idea": "API idea"}
        )
        assert response.status_code == 201
        task = response.json()
        assert task["status"] == "failed"
        assert task["error"]["code"] == "model_not_configured"
        listed = client.get("/api/research/tasks", headers=HOST)
        assert listed.status_code == 200
        assert [item["task_id"] for item in listed.json()["tasks"]] == [task["task_id"]]
        detail = client.get(f"/api/research/tasks/{task['task_id']}", headers=HOST)
        assert detail.status_code == 200
        assert detail.json()["raw_idea"] == "API idea"
        conflict = client.post(
            "/api/research/tasks", headers=WRITE_HEADERS, json={"idea": "API idea"}
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "research_task_exists"
        cancel = client.post(f"/api/research/tasks/{task['task_id']}/cancel", headers=WRITE_HEADERS)
        assert cancel.status_code == 200
        assert cancel.json()["status"] == "cancelled"
