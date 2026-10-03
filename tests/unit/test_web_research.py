from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from ea.web.app import WebSettings, create_app
from ea.web.llm import ModelNotConfigured, ModelProviderError, ModelResponse, ToolCall
from ea.web.research import (
    BudgetExceededError,
    InvalidSpecError,
    ResearchOrchestrator,
    ResearchStore,
    ResearchTask,
    ResearchTaskError,
    TaskConflictError,
    TaskNotFoundError,
    TaskReservation,
    TaskStateError,
)
from ea.web.service import JobNotFoundError, ServiceBusyError, WebService
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
        snapshot["messages"] = list(kwargs.get("messages") or [])
        self.calls.append(snapshot)
        response: ModelResponse | BaseException = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _response(tool_calls: list[dict[str, Any]], **extra: Any) -> ModelResponse:
    defaults: dict[str, Any] = dict(
        text="",
        stop_reason="tool_calls" if tool_calls else "stop",
        model="fake-model",
        input_tokens=11,
        output_tokens=7,
    )
    defaults.update(extra)
    return ModelResponse(
        tool_calls=tuple(ToolCall(c["id"], c["name"], c["input"]) for c in tool_calls),
        **defaults,
    )


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


def _task(**overrides: Any) -> ResearchTask:
    """A minimal persisted task; the store tests only need one readable record."""
    now = "2026-09-29T00:00:00.000000Z"
    values: dict[str, Any] = dict(
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
    values.update(overrides)
    return ResearchTask(**values)


def _rebuild_service(tmp_path: Any) -> WebService:
    """Rebuild the real service over the same workspace, as a restart does."""
    service = WebService(_scenario_root(tmp_path), tmp_path / "workspace")
    service.start()
    return service


def _recovered_orchestrator(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, responses: list[Any] | None = None
) -> tuple[ResearchOrchestrator, WebService, FakeProvider]:
    """New process image: fresh store, rebound real service, scripted provider."""
    store = ResearchStore(tmp_path / "workspace")
    service = _rebuild_service(tmp_path)
    orchestrator = ResearchOrchestrator(store, service, _settings_store(tmp_path, monkeypatch))
    provider = FakeProvider(responses or [])
    cast(Any, orchestrator)._provider = provider  # test seam, same interface
    return orchestrator, service, provider


class BlockingProvider(FakeProvider):
    """Scripted provider that blocks inside ``complete`` until released.

    It reports the instant its invocation begins so a test can prove what was
    already durable on disk *before* the provider was reached.
    """

    def __init__(self, script: list[Any]) -> None:
        super().__init__([])
        self.script = list(script)
        self.invoked = threading.Event()
        self.release = threading.Event()

    def complete(self, **kwargs: Any) -> ModelResponse:
        snapshot = dict(kwargs)
        snapshot["messages"] = list(kwargs.get("messages") or [])
        self.calls.append(snapshot)
        self.invoked.set()
        assert self.release.wait(10), "test did not release the blocked provider"
        response: ModelResponse | BaseException = self.script.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


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
    task = _task(request_id=str(uuid4()))
    store.save(task)
    loaded = store.get(task.task_id)
    assert loaded.raw_idea == task.raw_idea
    assert loaded.status == "drafting"
    assert loaded.request_id == task.request_id
    assert [item.task_id for item in store.list()] == [task.task_id]
    assert store.find_active_by_idea("  Test   the bounded long strategy on AAPL ") is not None
    assert store.find_active_by_idea("something else") is None


def test_interrupted_write_keeps_the_previous_readable_record(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ResearchStore(tmp_path)
    task = _task()
    store.save(task)
    replacement = replace(task, status="awaiting_confirmation")

    def fail_publish(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated interruption before publication")

    monkeypatch.setattr(os, "replace", fail_publish)
    with pytest.raises(OSError):
        store.save(replacement)
    monkeypatch.undo()

    # The complete previous record is still the published one.
    assert store.get(task.task_id).status == "drafting"
    record = json.loads((tmp_path / "research" / f"{task.task_id}.json").read_text())
    assert record["status"] == "drafting"
    assert list((tmp_path / "research").glob(".*")) == []
    store.save(replacement)
    assert store.get(task.task_id).status == "awaiting_confirmation"


def test_publish_failure_leaves_no_record_when_none_existed(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ResearchStore(tmp_path)
    task = _task()

    def fail_publish(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated interruption before publication")

    monkeypatch.setattr(os, "replace", fail_publish)
    with pytest.raises(OSError):
        store.save(task)
    monkeypatch.undo()

    with pytest.raises(TaskNotFoundError):
        store.get(task.task_id)
    assert list((tmp_path / "research").glob(".*")) == []


def test_abandoned_publish_temp_files_are_ignored_and_cleaned(tmp_path: Any) -> None:
    store = ResearchStore(tmp_path)
    task = _task()
    store.save(task)
    abandoned = tmp_path / "research" / ".task-abandoned"
    abandoned.write_text("{not json", encoding="utf-8")

    assert [item.task_id for item in store.list()] == [task.task_id]
    assert store.get(task.task_id).status == "drafting"
    ResearchStore(tmp_path)  # a restart cleans its own unpublished temp files
    assert not abandoned.exists()
    assert store.get(task.task_id).status == "drafting"


def test_records_without_the_new_reservation_fields_stay_readable(tmp_path: Any) -> None:
    """Old task records have no reservation metadata and must still load."""
    from ea.web.research import _SCHEMA

    store = ResearchStore(tmp_path)
    task = _task()
    document = task.document()
    assert document["schema"] == _SCHEMA
    for absent in ("reservation", "spent_calls"):
        document.pop(absent, None)
    (tmp_path / "research").mkdir(parents=True, exist_ok=True)
    (tmp_path / "research" / f"{task.task_id}.json").write_text(
        json.dumps(document), encoding="utf-8"
    )

    loaded = store.get(task.task_id)
    assert loaded.usage["model_calls"] == 0
    assert loaded.spent_calls == ()
    assert loaded.reservation is None
    assert loaded.backtest_reservation is None
    assert [item.task_id for item in store.list()] == [task.task_id]


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
    # The observed source model and timestamp survive the success save, so a
    # reader can tell a normal outcome from an unobserved one.
    assert task.last_model is not None
    assert task.last_model["model"] == "fake-model"
    assert task.last_model["outcome"] == "observed"
    assert task.last_model["at"].endswith("Z")
    assert orchestrator._store.get(task.task_id).last_model == task.last_model  # noqa: SLF001


def test_persisted_model_evidence_marks_an_unknown_outcome_honestly(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport failure leaves an explicit unknown classification, not silence."""
    orchestrator, _service, provider = _fake_orchestrator(
        tmp_path,
        monkeypatch,
        [
            ModelProviderError("model provider is unreachable: ConnectError"),
            ModelProviderError("model provider returned an unreadable response"),
        ],
    )
    first = orchestrator.create("a call the provider never observed")
    second = orchestrator.create("another unobserved call")
    for task in (first, second):
        assert task.status == "failed"
        assert task.error is not None and task.error["code"] == "provider_error"
        assert task.last_model is not None
        assert task.last_model["outcome"] == "unknown_after_dispatch"
        assert task.last_model["model"] == "unknown"
        assert task.last_model["at"].endswith("Z")
        # Honest charge, no invented tokens, no invented model identity.
        assert task.usage["model_calls"] == 1
        assert task.usage["input_tokens"] == 0
        assert task.usage["output_tokens"] == 0
        assert orchestrator._store.get(task.task_id).last_model == task.last_model  # noqa: SLF001
    assert len(provider.calls) == 2


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
    feedback = provider.calls[1]["messages"][-1].text
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
        orchestrator._reduce(exhausted, "model_reserve", {})  # noqa: SLF001


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


# --- budget reservation, dispatch and recovery --------------------------------
#
# These use a scripted local provider. They are not a real model/network test and
# make no claim about provider behaviour; they assert durable local bookkeeping.


def test_model_call_is_reserved_durably_before_the_provider_sees_it(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, _provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    store = orchestrator._store  # noqa: SLF001
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    provider = BlockingProvider(
        [
            _response(
                [
                    {
                        "id": "call-1",
                        "name": "propose_spec",
                        "input": _propose_call(strategy, catalog),
                    }
                ]
            )
        ]
    )
    cast(Any, orchestrator)._provider = provider
    failures: list[BaseException] = []
    thread = threading.Thread(
        target=lambda: _capture(failures, lambda: orchestrator.create("reserved before dispatch")),
        daemon=True,
    )
    thread.start()
    try:
        assert provider.invoked.wait(10), "the provider was never invoked"
        # The provider is blocked inside its call; the charge is already durable.
        pending = store.list()[0]
        assert pending.usage["model_calls"] == 1
        assert pending.reservation is not None
        assert pending.reservation.kind == "model_call"
        assert pending.reservation.status == "dispatched"
    finally:
        provider.release.set()
        thread.join(timeout=10)
        # Recorded before cleanup so a hang is reported without masking an
        # earlier assertion failure.
        finished = not thread.is_alive()
        service.stop()
    assert finished, "the blocked model call did not return after it was released"
    assert failures == []
    stored = store.list()[0]
    assert stored.status == "awaiting_confirmation"
    assert stored.reservation is None
    assert stored.spent_calls == (1,)
    assert stored.usage["input_tokens"] == 11
    assert stored.usage["output_tokens"] == 7


def test_exhausted_budget_never_invokes_the_provider(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, _service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    provider.responses.append(
        _response(
            [{"id": "call-1", "name": "propose_spec", "input": _propose_call(strategy, catalog)}]
        )
    )
    task = orchestrator.create("budgeted idea")
    assert task.status == "awaiting_confirmation"
    assert len(provider.calls) == 1
    provider.calls.clear()

    # A retry with the budget spent must be refused before any dispatch, even
    # though the scripted provider still has a response queued.
    store = orchestrator._store  # noqa: SLF001
    store.save(
        replace(
            task,
            status="failed",
            error={"step": "propose", "code": "no_tool_call", "message": "seed"},
            usage={**task.usage, "model_calls": 10},
        )
    )

    failed = orchestrator.retry(task.task_id)
    assert failed.status == "failed"
    assert failed.error is not None and failed.error["code"] == "budget_exceeded"
    assert failed.reservation is None
    assert provider.calls == []


def test_missing_configuration_releases_the_reservation_without_a_dispatch(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator = _orchestrator(tmp_path, monkeypatch)  # real provider, nothing configured
    task = orchestrator.create("no model configured")
    assert task.status == "failed"
    assert task.error is not None and task.error["code"] == "model_not_configured"
    assert task.usage["model_calls"] == 0
    assert task.spent_calls == ()
    assert task.reservation is None


def test_provider_failure_consumes_the_call_once_without_auto_redispatch(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(
        tmp_path, monkeypatch, [ModelProviderError("model provider is unreachable: ConnectError")]
    )
    task = orchestrator.create("provider outage idea")
    assert task.status == "failed"
    assert task.error is not None and task.error["code"] == "provider_error"
    # Reserved before dispatch and never released: the outcome is unknown, not free.
    assert task.usage["model_calls"] == 1
    assert task.spent_calls == (1,)
    assert task.reservation is None
    assert len(provider.calls) == 1
    # The unknown outcome survives the failure classification, without inventing
    # tokens or a model identity that was never observed.
    assert task.last_model is not None
    assert task.last_model["outcome"] == "unknown_after_dispatch"
    assert task.usage["input_tokens"] == 0 and task.usage["output_tokens"] == 0

    service.stop()
    orchestrator, _service, provider = _recovered_orchestrator(tmp_path, monkeypatch)
    claimed = orchestrator.reconcile(task.task_id)
    assert claimed.usage["model_calls"] == 1
    assert claimed.spent_calls == (1,)
    assert claimed.reservation is None
    assert provider.calls == []
    # A second restart does not charge or dispatch again.
    again = orchestrator.reconcile(task.task_id)
    assert again.usage["model_calls"] == 1
    assert again.spent_calls == (1,)


def test_restart_releases_a_model_call_reserved_before_dispatch(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``pending`` proves no provider transport: released once, then retryable."""
    orchestrator, service, _provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    store = orchestrator._store  # noqa: SLF001
    seeded = _task(
        request_id=str(uuid4()),
        usage={"model_calls": 1, "input_tokens": 0, "output_tokens": 0, "backtests": 0},
        reservation=TaskReservation(kind="model_call", seq=1, status="pending", at="x"),
    )
    store.save(seeded)
    service.stop()

    orchestrator, _service, provider = _recovered_orchestrator(tmp_path, monkeypatch)
    recovered = orchestrator.reconcile_all()
    assert len(recovered) == 1
    released = recovered[0]
    # Pending means the call was charged but never handed to the provider, so
    # the charge is given back once and the task is retryable again.
    assert released.usage["model_calls"] == 0
    assert released.spent_calls == ()
    assert released.reservation is None
    assert released.status == "failed"
    assert released.error is not None
    assert released.error["code"] == "interrupted_before_dispatch"
    assert provider.calls == []
    again = orchestrator.reconcile(seeded.task_id)
    assert again.usage["model_calls"] == 0
    assert again.status == "failed"

    # The honest failure is retryable in place, with a fresh scripted attempt.
    provider.responses.append(_model_evidence_response(orchestrator, "call-1"))
    retried = orchestrator.retry(seeded.task_id)
    assert retried.status == "awaiting_confirmation"
    assert retried.usage["model_calls"] == 1
    assert retried.spent_calls == (1,)
    assert len(provider.calls) == 1
    assert "new task" not in " ".join(str(item) for item in (retried.error or {}).values())


def test_restart_consumes_a_dispatched_model_call_and_pauses_it(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``dispatched`` may have been billed: consumed once, never auto-retried."""
    orchestrator, service, _provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    store = orchestrator._store  # noqa: SLF001
    seeded = _task(
        request_id=str(uuid4()),
        usage={"model_calls": 1, "input_tokens": 0, "output_tokens": 0, "backtests": 0},
        reservation=TaskReservation(kind="model_call", seq=1, status="dispatched", at="x"),
    )
    store.save(seeded)
    service.stop()

    orchestrator, _service, provider = _recovered_orchestrator(tmp_path, monkeypatch)
    claimed = orchestrator.reconcile(seeded.task_id)
    assert claimed.usage["model_calls"] == 1
    assert claimed.spent_calls == (1,)
    assert claimed.reservation is None
    assert claimed.status == "failed"
    assert claimed.error is not None and claimed.error["code"] == "model_outcome_unknown"
    assert claimed.last_model is not None
    assert claimed.last_model["outcome"] == "unknown_after_dispatch"
    assert claimed.usage["input_tokens"] == 0 and claimed.usage["output_tokens"] == 0
    assert provider.calls == []
    # A restart never re-dispatches the unknown attempt: the user must cancel
    # and start a new task explicitly.
    with pytest.raises(TaskStateError) as refusal:
        orchestrator.retry(seeded.task_id)
    assert "cancel" in str(refusal.value)
    assert provider.calls == []
    assert orchestrator.reconcile(seeded.task_id).usage["model_calls"] == 1


def test_legacy_drafting_task_without_any_call_becomes_a_retryable_failure(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, _provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    store = orchestrator._store  # noqa: SLF001
    seeded = _task(request_id=str(uuid4()))
    store.save(seeded)
    service.stop()

    orchestrator, _service, provider = _recovered_orchestrator(tmp_path, monkeypatch)
    classified = orchestrator.reconcile(seeded.task_id)
    assert classified.status == "failed"
    assert classified.error is not None
    assert classified.error["code"] == "interrupted_before_dispatch"
    assert classified.usage["model_calls"] == 0
    assert classified.reservation is None
    assert provider.calls == []

    provider.responses.append(_model_evidence_response(orchestrator, "call-1"))
    retried = orchestrator.retry(seeded.task_id)
    assert retried.status == "awaiting_confirmation"
    assert retried.usage["model_calls"] == 1


def test_missing_configuration_releases_from_the_current_durable_record(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A known no-dispatch release never rolls back observed token evidence."""
    orchestrator, _service, _provider = _fake_orchestrator(
        tmp_path, monkeypatch, [_response([], text="no tools")]
    )
    observed = orchestrator.create("prior observed usage")
    assert observed.status == "failed"
    assert observed.usage["model_calls"] == 1
    assert observed.usage["input_tokens"] == 11
    assert observed.usage["output_tokens"] == 7
    store = orchestrator._store  # noqa: SLF001

    # The provider stops being configured while a fresh reservation is durable
    # and already marked dispatched: ``ModelNotConfigured`` is raised before any
    # transport, so it is the one dispatched failure that provably never left.
    cast(Any, orchestrator)._provider = _NoConfigProvider()
    released = orchestrator.retry(observed.task_id)
    assert released.status == "failed"
    assert released.error is not None and released.error["code"] == "model_not_configured"
    assert released.usage["model_calls"] == 1
    assert released.spent_calls == (1,)
    assert released.reservation is None
    stored = store.get(observed.task_id)
    assert stored.usage["input_tokens"] == 11
    assert stored.usage["output_tokens"] == 7
    assert stored.last_model is not None
    assert stored.last_model["outcome"] == "observed"


def _model_evidence_response(orchestrator: ResearchOrchestrator, call_id: str) -> ModelResponse:
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    return _response(
        [{"id": call_id, "name": "propose_spec", "input": _propose_call(strategy, catalog)}]
    )


class _NoConfigProvider:
    """Raises the typed no-configuration error before any transport."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, **kwargs: Any) -> ModelResponse:
        self.calls += 1
        raise ModelNotConfigured("no model provider is configured")


def _capture(failures: list[BaseException], action: Any) -> None:
    try:
        action()
    except BaseException as error:  # noqa: BLE001 — recorded for the test thread
        failures.append(error)


# --- same-process serialization (A-R2) ---------------------------------------


def _blocked_create(
    orchestrator: ResearchOrchestrator,
    provider: BlockingProvider,
    idea: str,
) -> tuple[threading.Thread, list[BaseException], list[ResearchTask]]:
    failures: list[BaseException] = []
    results: list[ResearchTask] = []

    def run() -> None:
        _capture(failures, lambda: results.append(orchestrator.create(idea)))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    # The call is durable and the provider is blocked inside it: the payload is
    # proved before the background thread is allowed to finish.
    assert provider.invoked.wait(10), "the provider was never invoked"
    live = orchestrator._store.list()[0]  # noqa: SLF001
    assert live.reservation is not None and live.reservation.status == "dispatched"
    return thread, failures, results


def test_refresh_during_an_active_call_leaves_the_live_round_alone(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read racing an in-process dispatch must not claim it crashed."""
    orchestrator, service, _provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    store = orchestrator._store  # noqa: SLF001
    provider = BlockingProvider([_model_evidence_response(orchestrator, "call-1")])
    cast(Any, orchestrator)._provider = provider
    thread, create_failures, _results = _blocked_create(
        orchestrator, provider, "active dispatch is not a restart"
    )
    task_id = store.list()[0].task_id
    observed: list[ResearchTask] = []
    failures: list[BaseException] = []
    refresh_thread = threading.Thread(
        target=lambda: _capture(failures, lambda: observed.append(orchestrator.refresh(task_id))),
        daemon=True,
    )
    refresh_thread.start()
    provider.release.set()
    thread.join(timeout=10)
    refresh_thread.join(timeout=10)
    assert not thread.is_alive() and not refresh_thread.is_alive()
    assert create_failures == []
    assert failures == []
    assert observed != []
    # The racing read never manufactured a restart classification.
    assert observed[0].status != "failed"
    stored = store.get(task_id)
    # The live round finished normally and was never re-classified as unknown.
    assert stored.status == "awaiting_confirmation"
    assert stored.reservation is None
    assert stored.spent_calls == (1,)
    assert stored.usage["input_tokens"] == 11
    assert stored.last_model is not None
    assert stored.last_model["outcome"] == "observed"
    service.stop()


def test_cancel_acknowledgement_survives_a_late_model_result(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A late tool result cannot resurrect a cancelled task or run another round."""
    orchestrator, service, _provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    store = orchestrator._store  # noqa: SLF001
    provider = BlockingProvider([_model_evidence_response(orchestrator, "call-1")])
    cast(Any, orchestrator)._provider = provider
    worker, create_failures, _results = _blocked_create(
        orchestrator, provider, "cancel during dispatch"
    )
    task_id = store.list()[0].task_id
    live = store.get(task_id)
    # The acknowledged state at the moment of cancel, before the call returns.
    acknowledged = orchestrator.cancel(task_id)
    assert acknowledged.status == "cancelled"
    assert acknowledged.error is None
    # The dispatching round was still blocked inside its provider call, and the
    # live reservation was left exactly as it was.
    assert worker.is_alive()
    still_live = store.get(task_id)
    assert still_live.reservation == live.reservation
    provider.release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert create_failures == []
    stored = store.get(task_id)
    # The late model result was discarded: the acknowledgement stands.
    assert stored.status == "cancelled"
    assert stored.error is None
    # The observed round is still accounted once; no extra round was started.
    assert stored.usage["model_calls"] == 1
    assert stored.spent_calls == (1,)
    assert len(provider.calls) == 1
    assert orchestrator.refresh(task_id).status == "cancelled"
    service.stop()


def test_concurrent_retries_cannot_double_spend_the_last_model_call_slot(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, _provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    store = orchestrator._store  # noqa: SLF001
    provider = BlockingProvider([_model_evidence_response(orchestrator, "call-1")])
    cast(Any, orchestrator)._provider = provider
    seeded = _task(
        request_id=str(uuid4()),
        status="failed",
        error={"step": "propose", "code": "no_tool_call", "message": "seed"},
        budget={"max_model_calls": 1, "max_backtests": 5},
    )
    store.save(seeded)
    outcomes: list[BaseException | ResearchTask] = []

    def attempt() -> None:
        try:
            outcomes.append(orchestrator.retry(seeded.task_id))
        except BaseException as error:  # noqa: BLE001 — recorded for the test thread
            outcomes.append(error)

    first = threading.Thread(target=attempt, daemon=True)
    first.start()
    # The first retry reaches the provider while the second one starts: the
    # round is owned by this process, so the second can neither add a charge nor
    # reach the provider, and it may not classify the live round either.
    assert provider.invoked.wait(10), "no retry reached the provider"
    second = threading.Thread(target=attempt, daemon=True)
    second.start()
    second.join(timeout=10)
    assert not second.is_alive()
    provider.release.set()
    first.join(timeout=10)
    assert not first.is_alive()
    stored = store.get(seeded.task_id)
    assert stored.usage["model_calls"] == 1
    assert stored.spent_calls == (1,)
    assert stored.status == "awaiting_confirmation"
    assert len(provider.calls) == 1
    # Exactly one retry dispatches and spends the last slot. The loser reports
    # that live in-flight round instead of spending it a second time.
    results = [item for item in outcomes if isinstance(item, ResearchTask)]
    assert [item.status for item in results] == ["failed", "awaiting_confirmation"]
    loser = results[0]
    assert loser.error is not None and loser.error["code"] == "no_tool_call"
    assert loser.reservation is not None
    assert loser.reservation.status == "dispatched"
    assert loser.usage["model_calls"] == 1
    assert [item for item in outcomes if isinstance(item, BaseException)] == []
    service.stop()


# --- job identity, quota binding and crash recovery ---------------------------


def _confirmed_task(
    orchestrator: ResearchOrchestrator, provider: FakeProvider, idea: str
) -> ResearchTask:
    """Create one task with a scripted proposal and confirm it without running."""
    provider.responses.append(_model_evidence_response(orchestrator, "call-1"))
    task = orchestrator.create(idea)
    assert task.status == "awaiting_confirmation"
    confirmed = orchestrator.confirm(task.task_id, auto_run=False)
    assert confirmed.status == "confirmed"
    return confirmed


def _paused_after_a_published_submission(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[ResearchOrchestrator, WebService, ResearchTask, Any, Any]:
    """One confirmed task paused at an unprovable submission boundary.

    The real job *is* published under the task's request identity, but the
    lookup that would have resolved the submission went unreadable, so the task
    paused with a single retained charge and no binding. Returns the
    orchestrator, its service, the task, the published ``JobRecord`` and the
    real lookup for callers that want to answer with it.
    """
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    task = _confirmed_task(orchestrator, provider, "Published but never learned")
    real_create = service.create_job
    real_lookup = service.lookup_job_by_request_id
    created: list[Any] = []
    lookups = {"n": 0}

    def publish_then_fail(**kwargs: Any) -> Any:
        record, _is_new = real_create(**kwargs)
        created.append(record)
        raise RuntimeError("simulated failure after the job record was durable")

    def unreadable_after_the_publication(request_id: str) -> Any:
        lookups["n"] += 1
        if lookups["n"] > 2:
            # The pre-flight and pre-create lookups answered; the lookup that
            # resolves the failed submission cannot.
            raise OSError("job index is unavailable")
        return real_lookup(request_id)

    monkeypatch.setattr(service, "create_job", publish_then_fail)
    monkeypatch.setattr(service, "lookup_job_by_request_id", unreadable_after_the_publication)
    paused = orchestrator._start_backtest(task.task_id)  # noqa: SLF001
    assert paused.status == "failed"
    assert paused.error is not None and paused.error["code"] == "job_outcome_unknown"
    assert paused.jobs == ()
    assert paused.usage["backtests"] == 1
    assert paused.backtest_reservation is not None
    assert len(created) == 1
    published = created[0]
    assert published.attempt_id is not None
    assert {str(item["job_id"]) for item in service.list_jobs()} == {published.job_id}
    return orchestrator, service, task, published, real_lookup


def test_unexpected_create_failure_after_publication_keeps_one_charge(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A create_job exception can happen after the job is already durable.

    The task must rebind the durable job and keep its single charge instead of
    refunding and submitting a second economic attempt.
    """
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    confirmed = _confirmed_task(orchestrator, provider, "Fails after job publication")
    real_create = service.create_job
    created: list[Any] = []
    inner = {"raised": False}

    def create_then_fail(**kwargs: Any) -> Any:
        record, _is_new = real_create(**kwargs)
        created.append(record)
        if not inner["raised"]:
            inner["raised"] = True
            raise RuntimeError("simulated failure after the job record was durable")
        return record

    monkeypatch.setattr(service, "create_job", create_then_fail)
    task_id = confirmed.task_id
    rebound = orchestrator._start_backtest(task_id)  # noqa: SLF001
    durable = created[0]
    # The exception happened after publication, so the job is real: it is
    # re-bound with the same identity, and the single charge is retained.
    assert rebound.status in {"running", "succeeded"}
    assert rebound.jobs == (durable.job_id,)
    assert rebound.usage["backtests"] == 1
    assert rebound.backtest_reservation is None
    assert rebound.error is None
    assert len(created) == 1
    assert len(service.list_jobs()) == 1
    assert service.get_job(durable.job_id).request_id == confirmed.request_id
    assert service.get_job(durable.job_id).attempt_id == durable.attempt_id
    # Re-entering the same task re-derives from the bound job: still one job.
    again = orchestrator._start_backtest(task_id)  # noqa: SLF001
    assert again.jobs == rebound.jobs
    assert again.usage["backtests"] == 1
    assert len(created) == 1
    assert len(service.list_jobs()) == 1
    service.stop()
    service.stop()


def test_ambiguous_lookup_failure_pauses_without_submitting_or_refunding(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable index is not a dispatch: no job, and no invented spend.

    This task never reserved a backtest slot, so an unavailable lookup can prove
    neither that a job exists nor that anything was ever funded. Nothing is
    created, the charge counter stays at zero, and no reservation is invented to
    justify a refund.
    """
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    confirmed = _confirmed_task(orchestrator, provider, "Lookup becomes unavailable")
    real_lookup = service.lookup_job_by_request_id
    blocking = {"on": False}
    creates: list[dict[str, Any]] = []
    real_create = service.create_job

    def flaky_lookup(request_id: str) -> Any:
        if blocking["on"]:
            raise OSError("job index is unavailable")
        return real_lookup(request_id)

    def counted_create(**kwargs: Any) -> Any:
        creates.append(kwargs)
        return real_create(**kwargs)

    monkeypatch.setattr(service, "lookup_job_by_request_id", flaky_lookup)
    monkeypatch.setattr(service, "create_job", counted_create)
    blocking["on"] = True
    paused = orchestrator._start_backtest(confirmed.task_id)  # noqa: SLF001
    assert paused.status == "failed"
    assert paused.error is not None and paused.error["code"] == "job_outcome_unknown"
    # The disclosed exit is a reachable one: cancel this task, create another.
    assert "cancel" in paused.error["message"].lower()
    assert paused.usage["backtests"] == 0
    assert paused.backtest_reservation is None
    assert paused.jobs == ()
    assert creates == []
    # The ambiguous state is not silently retried into a first submission.
    with pytest.raises(TaskStateError) as refused:
        orchestrator.retry(confirmed.task_id)
    assert "cancel" in str(refused.value).lower()
    assert creates == []
    assert orchestrator.refresh(confirmed.task_id).usage["backtests"] == 0
    blocking["on"] = False
    service.stop()

    # A restart invents nothing either: no charge, no job, still paused.
    orchestrator, recovered_service, _provider = _recovered_orchestrator(tmp_path, monkeypatch)
    resolved = orchestrator.refresh(confirmed.task_id)
    assert resolved.status == "failed"
    assert resolved.error is not None and resolved.error["code"] == "job_outcome_unknown"
    assert resolved.usage["backtests"] == 0
    assert resolved.backtest_reservation is None
    assert resolved.jobs == ()
    assert recovered_service.list_jobs() == []


def test_dispatched_unknown_submission_keeps_its_charge_and_is_never_resubmitted(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatched submission whose outcome is unprovable stays consumed.

    The slot and its ``dispatched`` marker were durable before the job call, so
    the charge is real. An ordinary index lookup that cannot answer is not proof
    that no job was published, so nothing is refunded and nothing is
    re-submitted — not even across a restart whose absence proof is not
    authoritative.
    """
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    confirmed = _confirmed_task(orchestrator, provider, "Dispatched then unprovable")
    real_lookup = service.lookup_job_by_request_id
    lookups = {"n": 0}
    creates: list[dict[str, Any]] = []

    def unreadable_once_dispatched(request_id: str) -> Any:
        lookups["n"] += 1
        if lookups["n"] > 2:
            # The pre-flight and pre-create lookups answered; the lookup that
            # would resolve the failed submission cannot.
            raise OSError("job index is unavailable")
        return real_lookup(request_id)

    def fail_before_publishing(**kwargs: Any) -> Any:
        creates.append(kwargs)
        raise RuntimeError("submission outcome could not be reported")

    monkeypatch.setattr(service, "lookup_job_by_request_id", unreadable_once_dispatched)
    monkeypatch.setattr(service, "create_job", fail_before_publishing)
    paused = orchestrator._start_backtest(confirmed.task_id)  # noqa: SLF001
    assert paused.status == "failed"
    assert paused.error is not None and paused.error["code"] == "job_outcome_unknown"
    # A real durable charge, kept with its unproven boundary marker.
    assert paused.usage["backtests"] == 1
    assert paused.backtest_reservation is not None
    assert paused.backtest_reservation.status == "dispatched"
    assert paused.jobs == ()
    assert len(creates) == 1
    assert service.list_jobs() == []
    with pytest.raises(TaskStateError):
        orchestrator.retry(confirmed.task_id)
    assert len(creates) == 1
    assert orchestrator.refresh(confirmed.task_id).usage["backtests"] == 1
    monkeypatch.setattr(service, "lookup_job_by_request_id", real_lookup)
    service.stop()

    # A restart cannot prove that no late publication is still possible, so the
    # consumed slot stays consumed and no second job appears.
    orchestrator, recovered_service, _provider = _recovered_orchestrator(tmp_path, monkeypatch)
    after = orchestrator.reconcile(confirmed.task_id)
    assert after.status == "failed"
    assert after.error is not None and after.error["code"] == "job_outcome_unknown"
    assert after.usage["backtests"] == 1
    assert after.jobs == ()
    assert after.backtest_reservation is not None
    assert after.backtest_reservation.status == "dispatched"
    assert recovered_service.list_jobs() == []

    # The disclosed exit: a deliberate cancel keeps the charge, discloses the
    # unproven boundary and never restores orchestration.
    cancelled = orchestrator.cancel(confirmed.task_id)
    assert cancelled.status == "cancelled"
    assert cancelled.cancel_note is not None
    assert "not a rollback" in cancelled.cancel_note
    assert cancelled.usage["backtests"] == 1
    assert cancelled.backtest_reservation is not None
    assert recovered_service.list_jobs() == []
    assert orchestrator.refresh(confirmed.task_id).status == "cancelled"


def test_pre_create_busy_failure_keeps_the_dispatched_charge_and_is_never_resubmitted(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejection after the slot was dispatched is conservative, not a refund.

    ``ServiceBusyError`` says the local service refused this submission, but by
    then the slot was already dispatched and this product cannot prove that no
    job was published under the identity, so the charge and its reservation stay
    exactly as they are. Nothing is re-submitted automatically: the exit is a
    deliberate cancel and a new task.
    """
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    confirmed = _confirmed_task(orchestrator, provider, "Busy before any effect")
    real_create = service.create_job
    attempts: list[dict[str, Any]] = []
    rejections = {"left": 1}

    def busy_once(**kwargs: Any) -> Any:
        attempts.append(kwargs)
        if rejections["left"]:
            rejections["left"] -= 1
            raise ServiceBusyError("one local backtest is already active")
        return real_create(**kwargs)

    monkeypatch.setattr(service, "create_job", busy_once)
    rejected = orchestrator._start_backtest(confirmed.task_id)  # noqa: SLF001
    assert rejected.status == "failed"
    assert rejected.error is not None and rejected.error["code"] == "job_outcome_unknown"
    assert "cancel" in rejected.error["message"].lower()
    assert rejected.usage["backtests"] == 1
    assert rejected.backtest_reservation is not None
    assert rejected.backtest_reservation.status == "dispatched"
    assert rejected.jobs == ()
    assert service.list_jobs() == []
    assert len(attempts) == 1

    # No automatic retry, no second submission, no refund on refresh.
    with pytest.raises(TaskStateError) as refused:
        orchestrator.retry(confirmed.task_id)
    assert "cancel" in str(refused.value).lower()
    assert len(attempts) == 1
    refreshed = orchestrator.refresh(confirmed.task_id)
    assert refreshed.usage["backtests"] == 1
    assert refreshed.backtest_reservation is not None
    assert len(attempts) == 1
    service.stop()

    # A restart keeps the consumed charge as well: the index holds no job, but
    # that absence does not prove no late publication was possible.
    orchestrator, recovered_service, _provider = _recovered_orchestrator(tmp_path, monkeypatch)
    resolved = orchestrator.reconcile(confirmed.task_id)
    assert resolved.status == "failed"
    assert resolved.error is not None and resolved.error["code"] == "job_outcome_unknown"
    assert resolved.usage["backtests"] == 1
    assert resolved.backtest_reservation is not None
    assert resolved.jobs == ()
    assert recovered_service.list_jobs() == []
    recovered_service.stop()


def test_pending_backtest_reservation_before_dispatch_is_released_once(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pending window is the one backtest charge that may be returned.

    A hard stop between the durable ``pending`` marker and the ``dispatched``
    marker leaves a slot that was provably never handed to the job service: no
    job can exist for it. Reconciliation releases it exactly once and the task
    becomes retryable, which re-charges one fresh slot.
    """
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    confirmed = _confirmed_task(orchestrator, provider, "Stopped between pending and dispatched")
    real_transition = orchestrator._transition  # noqa: SLF001

    def stop_before_dispatch(
        task_id: str, generation: int | None, event: str, **data: Any
    ) -> ResearchTask:
        if event == "backtest_dispatch":
            raise SystemExit("simulated process stop between pending and dispatched")
        return real_transition(task_id, generation, event, **data)

    monkeypatch.setattr(orchestrator, "_transition", stop_before_dispatch)
    with pytest.raises(SystemExit):
        orchestrator._start_backtest(confirmed.task_id)  # noqa: SLF001
    durable = orchestrator._store.get(confirmed.task_id)  # noqa: SLF001
    assert durable.backtest_reservation is not None
    assert durable.backtest_reservation.status == "pending"
    assert durable.usage["backtests"] == 1
    assert service.list_jobs() == []
    service.stop()

    # A restart proves the absence: the marker was never dispatched, so the
    # slot is released once and the failure is retryable.
    orchestrator, recovered_service, _provider = _recovered_orchestrator(tmp_path, monkeypatch)
    released = orchestrator.reconcile(confirmed.task_id)
    assert released.status == "failed"
    assert released.error is not None
    assert released.error["code"] == "interrupted_before_dispatch"
    assert released.usage["backtests"] == 0
    assert released.backtest_reservation is None
    assert recovered_service.list_jobs() == []
    assert orchestrator.reconcile(confirmed.task_id).usage["backtests"] == 0

    retried = orchestrator.retry(confirmed.task_id)
    assert retried.usage["backtests"] == 1
    assert len(retried.jobs) == 1
    assert recovered_service.get_job(retried.jobs[0]).request_id == confirmed.request_id
    assert len(recovered_service.list_jobs()) == 1
    assert orchestrator.refresh(confirmed.task_id).usage["backtests"] == 1
    recovered_service.stop()


def test_restart_rebinds_the_same_job_and_attempt_after_an_ambiguous_submission(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart resolves an unproven boundary without creating a second job.

    A real job was published under the task's request identity, but the task
    never learned about it because the index went unreadable at that moment.
    Restart reconciliation must re-bind that exact job and attempt, with the
    same single charge and no second submission.
    """
    orchestrator, service, task, published, _real_lookup = _paused_after_a_published_submission(
        tmp_path, monkeypatch
    )
    service.stop()

    # Restart: the durable index is readable again and proves the job exists.
    orchestrator, recovered_service, provider = _recovered_orchestrator(tmp_path, monkeypatch)
    rebound = orchestrator.reconcile(task.task_id)
    assert rebound.jobs == (published.job_id,)
    assert rebound.backtest_reservation is None
    assert rebound.usage["backtests"] == 1
    record = recovered_service.get_job(published.job_id)
    assert record.job_id == published.job_id
    assert record.attempt_id == published.attempt_id
    assert record.request_id == task.request_id
    # Re-binding is not a re-submission: one job, one attempt, one charge.
    assert [str(item["job_id"]) for item in recovered_service.list_jobs()] == [published.job_id]
    assert provider.calls == []


def test_late_publication_of_the_same_identity_is_rebound_not_resubmitted(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job that shows up after the pause is re-bound, never re-submitted."""
    orchestrator, service, task, published, real_lookup = _paused_after_a_published_submission(
        tmp_path, monkeypatch
    )
    answers = {"n": 0}

    def absent_then_found(request_id: str) -> Any:
        answers["n"] += 1
        if answers["n"] > 1:
            # The job was published late: the boundary lookup now finds it.
            return real_lookup(request_id)
        raise JobNotFoundError("no job is bound to this request_id")

    monkeypatch.setattr(service, "lookup_job_by_request_id", absent_then_found)
    rebound = orchestrator._start_backtest(task.task_id)  # noqa: SLF001
    assert rebound.jobs == (published.job_id,)
    assert rebound.backtest_reservation is None
    assert rebound.usage["backtests"] == 1
    record = service.get_job(published.job_id)
    assert record.attempt_id == published.attempt_id
    assert record.request_id == task.request_id
    assert [str(item["job_id"]) for item in service.list_jobs()] == [published.job_id]


def test_request_id_is_durable_before_create_job_and_quota_binds_once(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    provider.responses.append(
        _response(
            [{"id": "call-1", "name": "propose_spec", "input": _propose_call(strategy, catalog)}]
        )
    )
    task = orchestrator.create("Bound exactly once")
    store = orchestrator._store  # noqa: SLF001
    seen: list[tuple[str | None, int]] = []
    real_create = service.create_job

    def spy(**kwargs: Any) -> Any:
        persisted = store.get(task.task_id)
        seen.append((kwargs["request_id"], persisted.usage["backtests"]))
        return real_create(**kwargs)

    monkeypatch.setattr(service, "create_job", spy)
    scheduled = orchestrator.confirm(task.task_id, auto_run=False)
    assert scheduled.status == "confirmed"
    bound = orchestrator._start_backtest(task.task_id)  # noqa: SLF001
    assert bound.status in {"running", "succeeded"}
    assert bound.usage["backtests"] == 1
    assert len(bound.jobs) == 1
    assert [request for request, _used in seen] == [bound.request_id]
    # The identity was durable before the submission, and the slot was charged
    # in the same durable record.
    assert seen[0][0] is not None
    assert seen[0][1] == 1

    # Re-entering the same task re-derives from the bound job: one identity, one
    # job, one charge.
    again = orchestrator._start_backtest(bound.task_id)  # noqa: SLF001
    assert again.jobs == bound.jobs
    assert again.usage["backtests"] == 1
    assert len(service.list_jobs()) == 1


def test_crash_after_job_creation_reconciles_the_same_job_without_refunding(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    provider.responses.append(
        _response(
            [{"id": "call-1", "name": "propose_spec", "input": _propose_call(strategy, catalog)}]
        )
    )
    task = orchestrator.create("Crash between job creation and binding")
    store = orchestrator._store  # noqa: SLF001
    real_save = store.save
    real_create = service.create_job
    guard = {"crash": False}

    def crash_before_binding(**kwargs: Any) -> Any:
        created = real_create(**kwargs)
        # The real job and its attempt are durable; the task binding is not.
        guard["crash"] = True
        return created

    def crash_after_job(task_to_save: ResearchTask) -> None:
        if guard["crash"]:
            raise ResearchTaskError("simulated crash before the task binding")
        real_save(task_to_save)

    monkeypatch.setattr(service, "create_job", crash_before_binding)
    monkeypatch.setattr(store, "save", crash_after_job)
    with pytest.raises(ResearchTaskError):
        orchestrator.confirm(task.task_id)
    monkeypatch.setattr(store, "save", real_save)

    interrupted = store.get(task.task_id)
    assert interrupted.request_id == task.request_id
    assert interrupted.backtest_reservation is not None
    assert interrupted.usage["backtests"] == 1
    # The durable reservation exists, but no job is bound to the task yet.
    assert interrupted.jobs == ()
    assert len(service.list_jobs()) == 1
    original = service.list_jobs()[0]
    original_job_id = str(original["job_id"])
    original_attempt_id = str(original["attempt_id"])
    service.stop()

    # New process image over the same workspace, as a restart does.
    orchestrator, recovered_service, recovered_provider = _recovered_orchestrator(
        tmp_path, monkeypatch
    )
    bound = orchestrator.refresh(task.task_id)
    assert bound.status == "succeeded"
    assert bound.usage["backtests"] == 1
    assert bound.backtest_reservation is None
    assert len(bound.jobs) == 1
    assert len(recovered_service.list_jobs()) == 1
    job = recovered_service.get_job(bound.jobs[0])
    assert job.request_id == task.request_id
    # The original job and its attempt id are re-bound, never re-created.
    assert job.job_id == original_job_id
    assert job.attempt_id == original_attempt_id
    # No automatic provider dispatch or new economic work on restart.
    assert recovered_provider.calls == []


def test_failed_job_is_a_safe_view_and_retry_boundary(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real job that failed is reported as failed and is never silently rerun."""
    import ea.web.service as web_service

    def fail_engine(*args: Any, **kwargs: Any) -> Any:
        raise OSError("simulated engine failure inside the real backtest attempt")

    monkeypatch.setattr(web_service, "run_backtest_scenario", fail_engine)
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    provider.responses.append(
        _response(
            [{"id": "call-1", "name": "propose_spec", "input": _propose_call(strategy, catalog)}]
        )
    )
    task = orchestrator.create("A run the engine will fail")
    assert task.status == "awaiting_confirmation"

    bound = orchestrator.confirm(task.task_id)
    assert bound.status in {"running", "succeeded", "failed"}
    job_id = bound.jobs[0]
    if bound.status == "running":
        bound = orchestrator.refresh(task.task_id)
    record = service.get_job(job_id)
    assert record.status == "failed"
    assert bound.status == "failed"
    assert bound.error is not None
    assert bound.error["step"] == "backtest"
    assert bound.error["code"] == str(record.error_code)

    calls_before = list(provider.calls)
    jobs_before = service.list_jobs()
    assert service.get_job(bound.jobs[0]).request_id == task.request_id
    # An already-created, already-failed economic attempt is never re-funded or
    # silently re-run: retry re-derives the same job identity and outcome.
    retried = orchestrator.retry(task.task_id)
    assert retried.status == "failed"
    assert retried.jobs == bound.jobs
    assert retried.usage["backtests"] == 1
    assert service.list_jobs() == jobs_before
    assert provider.calls == calls_before


def test_interrupted_job_is_classified_and_never_rerun(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job the service interrupted on restart is reported, not re-funded."""
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    provider.responses.append(
        _response(
            [{"id": "call-1", "name": "propose_spec", "input": _propose_call(strategy, catalog)}]
        )
    )
    task = orchestrator.create("Interrupted run")
    bound = orchestrator.confirm(task.task_id)
    assert bound.jobs
    job_id = bound.jobs[0]
    service.stop()

    job_path = tmp_path / "workspace" / "jobs" / f"{job_id}.json"
    raw = job_path.read_bytes()
    assert raw.count(b'"status":"succeeded"') == 1
    # An accepted-but-unfinished job, exactly as a hard restart leaves it on
    # disk: a same-length byte edit keeps the canonical payload intact.
    job_path.write_bytes(raw.replace(b'"status":"succeeded"', b'"status":"running"'))

    orchestrator, recovered_service, provider = _recovered_orchestrator(tmp_path, monkeypatch)
    # The restarted service keeps the attempt and marks it interrupted.
    assert recovered_service.get_job(job_id).status == "interrupted"
    classified = orchestrator.refresh(task.task_id)
    assert classified.status == "failed"
    assert classified.error is not None and classified.error["code"] == "interrupted"
    assert classified.usage["backtests"] == 1
    assert provider.calls == []

    calls_before = list(provider.calls)
    jobs_before = recovered_service.list_jobs()
    # Retrying an interrupted economic attempt never re-funds or re-runs it: the
    # retry either re-derives the same interrupted job or is refused.
    try:
        retried = orchestrator.retry(task.task_id)
    except TaskStateError:
        retried = None
    if retried is not None:
        assert retried.status == "failed"
        assert retried.error is not None and retried.error["code"] == "interrupted"
    final = orchestrator.refresh(task.task_id)
    assert final.status == "failed"
    assert final.error is not None and final.error["code"] == "interrupted"
    assert final.usage["backtests"] == 1
    assert final.jobs == (job_id,)
    assert recovered_service.list_jobs() == jobs_before
    assert provider.calls == calls_before


def test_cancel_stops_orchestration_without_rolling_back_a_started_job(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    provider.responses.append(
        _response(
            [{"id": "call-1", "name": "propose_spec", "input": _propose_call(strategy, catalog)}]
        )
    )
    task = orchestrator.create("Cancel after running")
    bound = orchestrator.confirm(task.task_id)
    assert bound.jobs

    cancelled = orchestrator.cancel(task.task_id)
    assert cancelled.status == "cancelled"
    assert cancelled.cancel_note is not None
    # The started backtest keeps its own state; cancel is not a rollback.
    job = service.get_job(bound.jobs[0])
    assert job.status in {"accepted", "running", "succeeded", "failed"}
    after_cancel = orchestrator.refresh(task.task_id)
    assert after_cancel.status == "cancelled"
    assert after_cancel.jobs == bound.jobs
    calls_after = len(provider.calls)
    with pytest.raises(TaskStateError):
        orchestrator.retry(task.task_id)
    assert len(provider.calls) == calls_after


# --- admission and finalisation windows (R2/R3 barriers) ----------------------


def _false_interruption(task: ResearchTask) -> bool:
    """True when a round that is still running here was read as a restart.

    ``job_outcome_unknown``/``model_outcome_unknown`` are the two codes that
    mean "this attempt may already have been dispatched and is not re-run". A
    concurrent reader inside a live round must never produce either one.
    """
    return (
        task.status == "failed"
        and task.error is not None
        and task.error.get("code") in {"job_outcome_unknown", "model_outcome_unknown"}
    )


def test_admission_window_keeps_one_owner_one_charge_and_no_false_interruption(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The admission transition is indivisible for both dispatch kinds.

    The barrier opens the exact window the old code left between a durable
    admission fact and the in-process ownership claim. A competing admission
    and a concurrent reader both run inside that window: the round must still
    gain one owner, one charge and one job, and must never be read as an
    interrupted restart.
    """
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    store = orchestrator._store  # noqa: SLF001

    # --- a competing admission during external validation ------------------
    confirmed = _confirmed_task(orchestrator, provider, "Admission window backtest")
    real_validate = service.validate_scenario
    validating = threading.Event()
    proceed = threading.Event()
    gate = {"open": True}
    real_create = service.create_job
    creates: list[Any] = []

    def barriered_validate(*args: Any, **kwargs: Any) -> Any:
        if gate["open"]:
            gate["open"] = False
            validating.set()
            assert proceed.wait(10), "the validation barrier was never released"
        return real_validate(*args, **kwargs)

    def counted_create(**kwargs: Any) -> Any:
        creates.append(kwargs)
        return real_create(**kwargs)

    monkeypatch.setattr(service, "validate_scenario", barriered_validate)
    monkeypatch.setattr(service, "create_job", counted_create)
    submitting: list[ResearchTask] = []
    submitter_failures: list[BaseException] = []
    submitter = threading.Thread(
        target=lambda: _capture(
            submitter_failures,
            lambda: submitting.append(orchestrator._start_backtest(confirmed.task_id)),  # noqa: SLF001
        ),
        daemon=True,
    )
    submitter.start()
    try:
        assert validating.wait(10), "the backtest never reached external validation"
        # Nothing durable yet: the validation runs outside the task lock.
        assert store.get(confirmed.task_id).backtest_reservation is None
        # A competing admission for the same task runs in that window.
        competitor = orchestrator._start_backtest(confirmed.task_id)  # noqa: SLF001
    finally:
        proceed.set()
        submitter.join(timeout=20)
        submitted = not submitter.is_alive()
    assert submitted, "the barriered admission never returned"
    assert submitter_failures == []
    settled = store.get(confirmed.task_id)
    assert settled.usage["backtests"] == 1
    assert settled.backtest_reservation is None
    assert len(settled.jobs) == 1
    assert len(creates) == 1
    assert len(service.list_jobs()) == 1
    assert not _false_interruption(competitor)
    assert submitting and not _false_interruption(submitting[0])
    assert submitting[0].jobs == settled.jobs

    # --- a concurrent reader during the model admission --------------------
    provider.responses.append(_model_evidence_response(orchestrator, "call-1"))
    claiming = threading.Event()
    release = threading.Event()
    arm = {"open": True}
    real_transition = orchestrator._transition  # noqa: SLF001

    def barriered_claim(
        task_id: str, generation: int | None, event: str, **kwargs: Any
    ) -> ResearchTask:
        record = real_transition(task_id, generation, event, **kwargs)
        if event == "model_dispatch" and arm["open"]:
            arm["open"] = False
            claiming.set()
            assert release.wait(10), "the admission barrier was never released"
        return record

    monkeypatch.setattr(orchestrator, "_transition", barriered_claim)
    calls_before = len(provider.calls)
    created: list[ResearchTask] = []
    proposal_failures: list[BaseException] = []
    reader: list[ResearchTask] = []
    proposer = threading.Thread(
        target=lambda: _capture(
            proposal_failures, lambda: created.append(orchestrator.create("Admission window model"))
        ),
        daemon=True,
    )
    proposer.start()
    probe: threading.Thread | None = None
    try:
        assert claiming.wait(10), "the model admission never reached the barrier"
        proposing = store.find_active_by_idea("Admission window model")
        assert proposing is not None
        model_task_id = proposing.task_id
        durable = store.get(model_task_id)
        assert durable.reservation is not None
        assert durable.reservation.status == "dispatched"
        assert durable.usage["model_calls"] == 1
        probe = threading.Thread(
            target=lambda: reader.append(orchestrator.refresh(model_task_id)), daemon=True
        )
        probe.start()
    finally:
        release.set()
        proposer.join(timeout=20)
        finished = not proposer.is_alive()
        if probe is not None:
            probe.join(timeout=20)
            finished = finished and not probe.is_alive()
        service.stop()
    assert finished, "a model admission participant never returned"
    assert proposal_failures == []
    assert reader and not _false_interruption(reader[0])
    assert created and created[0].status == "awaiting_confirmation"
    model_settled = store.get(model_task_id)
    assert model_settled.usage["model_calls"] == 1
    assert model_settled.spent_calls == (1,)
    assert len(provider.calls) == calls_before + 1


def test_finalisation_window_keeps_ownership_until_the_outcome_is_durable(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ownership outlives the external call, and survives a failed save.

    A result that has returned but is not yet durably recorded still belongs to
    a live round: a reader must not read it as a restart, a cancel must stay
    terminal, and a final persistence failure must fail closed by keeping the
    in-process ownership instead of releasing the round for a re-dispatch.
    """
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    store = orchestrator._store  # noqa: SLF001
    blocking = BlockingProvider([_model_evidence_response(orchestrator, "call-1")])
    cast(Any, orchestrator)._provider = blocking
    entered = threading.Event()
    release = threading.Event()
    real_transition = orchestrator._transition  # noqa: SLF001

    def barriered_finalise(
        task_id: str, generation: int | None, event: str, **kwargs: Any
    ) -> ResearchTask:
        if event == "model_observed":
            entered.set()
            assert release.wait(10), "the finalisation barrier was never released"
        return real_transition(task_id, generation, event, **kwargs)

    monkeypatch.setattr(orchestrator, "_transition", barriered_finalise)
    outcomes: list[ResearchTask] = []
    failures: list[BaseException] = []
    worker = threading.Thread(
        target=lambda: _capture(
            failures, lambda: outcomes.append(orchestrator.create("Finalisation window"))
        ),
        daemon=True,
    )
    worker.start()
    try:
        assert blocking.invoked.wait(10), "the provider was never reached"
        blocking.release.set()
        assert entered.wait(10), "the returned result never reached the finalisation barrier"
        task_id = store.list()[-1].task_id
        # The result came back but is not durable yet: this is still live work.
        live = orchestrator.refresh(task_id)
        assert not _false_interruption(live), live.error
        cancelled = orchestrator.cancel(task_id)
        assert cancelled.status == "cancelled"
    finally:
        release.set()
        worker.join(timeout=20)
        finished = not worker.is_alive()
        service.stop()
    assert finished, "the finalisation never returned"
    assert failures == []
    assert len(outcomes) == 1
    stored = store.get(task_id)
    # The cancel acknowledgement owns the terminal state; the late result only
    # records its own charge.
    assert stored.status == "cancelled"
    assert stored.spent_calls == (1,)
    assert stored.usage["model_calls"] == 1
    assert len(blocking.calls) == 1

    # --- an injected failure of the final outcome save ---------------------
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    store = orchestrator._store  # noqa: SLF001
    provider.responses.append(_model_evidence_response(orchestrator, "call-1"))
    real_save = store.save

    def failing_final_save(record: ResearchTask) -> None:
        if record.reservation is None and record.spent_calls:
            raise OSError("simulated final persistence failure")
        real_save(record)

    monkeypatch.setattr(store, "save", failing_final_save)
    lost: list[ResearchTask] = []
    save_failures: list[BaseException] = []
    loser = threading.Thread(
        target=lambda: _capture(
            save_failures, lambda: lost.append(orchestrator.create("Final save failure"))
        ),
        daemon=True,
    )
    loser.start()
    loser.join(timeout=20)
    assert not loser.is_alive(), "the failed finalisation never returned"
    assert len(save_failures) == 1 and isinstance(save_failures[0], OSError)
    assert lost == []
    failing = store.find_active_by_idea("Final save failure")
    assert failing is not None
    failing_task_id = failing.task_id
    # The round is not durable, so ownership must be retained (fail closed):
    # nothing is re-dispatched and no reader is told the round was interrupted.
    assert orchestrator._is_active(failing_task_id) is True  # noqa: SLF001
    after = orchestrator.refresh(failing_task_id)
    assert not _false_interruption(after), after.error
    durable = store.get(failing_task_id)
    assert durable.reservation is not None
    assert durable.reservation.status == "dispatched"
    assert durable.usage["model_calls"] == 1
    assert len(provider.calls) == 1
    service.stop()

    # Only a process restart recovers the durable state, once, without refund.
    recovered, recovered_service, _recovered_provider = _recovered_orchestrator(
        tmp_path, monkeypatch
    )
    resolved = recovered.reconcile(failing_task_id)
    assert resolved.status == "failed"
    assert resolved.error is not None and resolved.error["code"] == "model_outcome_unknown"
    assert resolved.usage["model_calls"] == 1
    assert resolved.spent_calls == (1,)
    recovered_service.stop()


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


@pytest.mark.parametrize(
    "outcome", ["success", "provider_error", "validation_error", "unknown", "found"]
)
def test_cancel_dominates_every_late_terminal_write(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    """Every former writer takes the same token gate, including error/bind."""
    from types import SimpleNamespace

    orchestrator, service, _provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    store = orchestrator._store  # noqa: SLF001
    seeded = _task(
        request_id="stable",
        usage={"model_calls": 1, "input_tokens": 0, "output_tokens": 0, "backtests": 0},
        reservation=TaskReservation("model_call", 1, "dispatched", "x"),
    )
    store.save(seeded)
    generation = orchestrator._begin(seeded.task_id, "propose")  # noqa: SLF001
    assert generation is not None
    entered, release = threading.Event(), threading.Event()
    outcomes: list[ResearchTask] = []
    failures: list[BaseException] = []

    def late() -> None:
        entered.set()
        assert release.wait(10)
        data: dict[str, Any]
        if outcome == "success":
            event, data = "proposal", {"status": "awaiting_confirmation", "spec": None}
        elif outcome == "provider_error":
            event, data = (
                "model_unknown",
                {
                    "seq": 1,
                    "model": {"model": "unknown", "outcome": "unknown_after_dispatch"},
                    "error": {"step": "propose", "code": "provider_error", "message": "late"},
                },
            )
        elif outcome == "found":
            event, data = (
                "job_found",
                {"record": SimpleNamespace(request_id="stable", job_id="original-job")},
            )
        else:
            event, data = (
                "failure",
                {
                    "error": {
                        "step": "start_backtest",
                        "code": "scenario_invalid"
                        if outcome == "validation_error"
                        else "job_outcome_unknown",
                        "message": "late",
                    }
                },
            )
        outcomes.append(
            orchestrator._transition(seeded.task_id, generation, event, finish=True, **data)
        )  # noqa: SLF001

    worker = threading.Thread(target=lambda: _capture(failures, late), daemon=True)
    worker.start()
    try:
        assert entered.wait(10)
        assert orchestrator.cancel(seeded.task_id).status == "cancelled"
        assert orchestrator._is_active(seeded.task_id)  # noqa: SLF001
    finally:
        release.set()
        worker.join(10)
        service.stop()
    assert not worker.is_alive() and failures == []
    settled = store.get(seeded.task_id)
    assert settled.status == "cancelled" and settled.error is None
    assert outcomes[0].status == "cancelled"
    assert not orchestrator._is_active(seeded.task_id)  # noqa: SLF001
    if outcome == "provider_error":
        assert settled.spent_calls == (1,)
        assert settled.last_model is not None
    if outcome == "found":
        assert settled.jobs == ("original-job",)


def test_old_token_cannot_write_or_release_a_new_attempt(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, service, _provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    store = orchestrator._store  # noqa: SLF001
    task = _task()
    store.save(task)
    first = orchestrator._begin(task.task_id, "propose")  # noqa: SLF001
    assert first is not None
    finished = orchestrator._transition(task.task_id, first, "noop", finish=True)  # noqa: SLF001
    # Closed duplicate is a pure no-op, not another save/reduce.
    assert (
        orchestrator._transition(task.task_id, first, "failure", finish=True, error={"code": "old"})
        == finished
    )  # noqa: SLF001
    second = orchestrator._begin(task.task_id, "propose")  # noqa: SLF001
    assert second is not None and second > first
    before = store.get(task.task_id)
    assert (
        orchestrator._transition(task.task_id, first, "failure", finish=True, error={"code": "old"})
        == before
    )  # noqa: SLF001
    assert orchestrator._propose(task.task_id, first) == before  # noqa: SLF001
    assert orchestrator._start_backtest(task.task_id, first) == before  # noqa: SLF001
    assert orchestrator._active[task.task_id] == second  # noqa: SLF001
    orchestrator._transition(task.task_id, second, "noop", finish=True)  # noqa: SLF001
    service.stop()


@pytest.mark.parametrize("after_replace", [False, True])
def test_save_failure_keeps_owner_and_replayed_finish_does_not_charge_twice(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    after_replace: bool,
) -> None:
    orchestrator, service, _provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    store = orchestrator._store  # noqa: SLF001
    task = _task(
        usage={"model_calls": 1, "input_tokens": 0, "output_tokens": 0, "backtests": 0},
        reservation=TaskReservation("model_call", 1, "dispatched", "x"),
    )
    store.save(task)
    generation = orchestrator._begin(task.task_id, "propose")  # noqa: SLF001
    assert generation is not None
    real_save = store.save
    armed = {"on": True}

    def interrupted(record: ResearchTask) -> None:
        if armed["on"] and record.operation_completion_generation == generation:
            armed["on"] = False
            if after_replace:
                real_save(record)
            raise OSError("durability barrier failed")
        real_save(record)

    monkeypatch.setattr(store, "save", interrupted)
    data = {"seq": 1, "response": _response([]), "model": {"model": "fake", "outcome": "observed"}}
    with pytest.raises(OSError):
        orchestrator._transition(task.task_id, generation, "model_observed", finish=True, **data)  # noqa: SLF001
    assert orchestrator._active[task.task_id] == generation  # noqa: SLF001
    diagnostic = orchestrator.refresh(task.task_id)
    assert diagnostic.runtime_diagnostic is not None
    assert "task_persistence_failed" in diagnostic.runtime_diagnostic
    assert store.get(task.task_id).usage["model_calls"] == 1
    settled = orchestrator._transition(
        task.task_id, generation, "model_observed", finish=True, **data
    )  # noqa: SLF001
    assert settled.spent_calls == (1,)
    assert settled.usage["input_tokens"] == 11 and settled.usage["output_tokens"] == 7
    assert settled.operation_completion_generation == generation
    assert not orchestrator._is_active(task.task_id)  # noqa: SLF001
    assert (
        orchestrator._transition(task.task_id, generation, "model_observed", finish=True, **data)
        == settled
    )  # noqa: SLF001
    service.stop()


def test_legacy_generation_defaults_do_not_imply_a_successful_operation(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, service, _provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    store = orchestrator._store  # noqa: SLF001
    task = _task()
    store.save(task)
    path = tmp_path / "workspace" / "research" / f"{task.task_id}.json"
    document = json.loads(path.read_text())
    for key in ("operation_generation", "operation_completion_generation", "operation_kind"):
        document.pop(key)
    path.write_text(json.dumps(document))
    legacy = store.get(task.task_id)
    assert legacy.operation_generation == 0 and legacy.operation_completion_generation == 0
    resolved = orchestrator.reconcile(task.task_id)
    assert resolved.status == "failed"
    assert resolved.error is not None and resolved.error["code"] == "interrupted_before_dispatch"
    assert resolved.operation_generation > 0
    service.stop()
