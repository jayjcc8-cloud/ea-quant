"""Explicit task request grants through the existing graph and durable call owner."""

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, NoReturn

import pytest

from ea.web.llm import ModelProviderError, ModelStreamEvent
from ea.web.user_strategies import (
    _EXPLAIN_INSTRUCTION,
    UserStrategy,
    UserStrategyBudgetError,
    UserStrategyConflictError,
    UserStrategyService,
)
from unit.test_web_domain_agent import Stack, tool
from unit.test_web_domain_agent import stack as stack
from unit.test_web_research import _response


def start(
    owner: UserStrategyService, strategy: UserStrategy, limit: int | None, identity: str = "task-1"
) -> list[dict[str, Any]]:
    return list(
        owner.conversation_turn(
            strategy.user_strategy_id,
            call_id=identity,
            base_revision=strategy.draft.revision,
            text="Inspect available research inputs, then explain them.",
            **(
                {
                    "approved_budget": {"max_model_calls": limit, "max_backtests": 0},
                    "task_budget": limit,
                }
                if limit is not None
                else {}
            ),
        )
    )


def topup(
    owner: UserStrategyService, strategy: UserStrategy, limit: int, grant: str = "grant-1"
) -> list[dict[str, Any]]:
    return list(
        owner.conversation_budget(
            strategy.user_strategy_id, turn_id="task-1", grant_id=grant, total_model_calls=limit
        )
    )


def test_task_can_continue_past_four_and_finish_without_repeat_approval(stack: Stack) -> None:
    owner, service, provider = stack
    strategy = owner.create(name="Task", intent="Inspect inputs")
    provider.responses.extend([tool("discover_resources", {}, f"read-{i}") for i in range(5)])
    provider.responses.append(_response([], text="Available inputs explained."))
    events = start(owner, strategy, 6)
    assert len(provider.calls) == 6
    assert events[-1]["kind"] == "done"
    task = owner.conversation_state(strategy.user_strategy_id)["task"]
    assert (task["limit"], task["used"], task["remaining"], task["stop_reason"]) == (
        6,
        6,
        0,
        "completed",
    )
    assert not service.list_jobs()


def test_exhaustion_topup_is_durable_idempotent_and_resumes_same_task(stack: Stack) -> None:
    owner, _, provider = stack
    strategy = owner.create(name="Task", intent="Inspect inputs")
    provider.responses.extend([tool("discover_resources", {}), _response([], text="Done")])
    events = start(owner, strategy, 1)
    assert events[-1]["kind"] == "interrupt"
    assert events[-1]["waiting"]["kind"] == "budget"
    assert len(provider.calls) == 1
    # Reopening/reconciling only reads; no provider auto-retry, and usage is stable.
    owner.reconcile_all()
    for _ in range(2):
        state = owner.conversation_state(strategy.user_strategy_id)
        assert state["task"]["used"] == 1 and state["task"]["stop_reason"] == "budget_exhausted"
    assert len(provider.calls) == 1
    topup(owner, strategy, 2)
    topup(owner, strategy, 2)
    assert len(provider.calls) == 2
    task = owner.conversation_state(strategy.user_strategy_id)["task"]
    assert task["used"] == 2 and task["stop_reason"] == "completed"
    with pytest.raises(UserStrategyConflictError):
        topup(owner, strategy, 3)
    assert len(provider.calls) == 2


@pytest.mark.parametrize("limit", [3, None])
def test_unknown_keeps_occupancy_and_never_replays_on_restart(
    stack: Stack, limit: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, _, provider = stack
    strategy = owner.create(name="Task", intent="Inspect inputs")

    def unknown(**kwargs: Any) -> NoReturn:
        raise ModelProviderError("transport outcome not observed")

    monkeypatch.setattr(provider, "stream", unknown)
    start(owner, strategy, limit)
    owner.reconcile_all()
    state = owner.conversation_state(strategy.user_strategy_id)
    assert state["task"]["used"] == 1
    assert state["task"]["stop_reason"] == "unknown"
    assert owner.get(strategy.user_strategy_id).calls[0]["status"] == "unknown"
    with pytest.raises(UserStrategyBudgetError):
        topup(owner, strategy, 5)


@pytest.mark.parametrize("limit", [8, None])
def test_cancel_during_dispatch_stops_followups_and_preserves_late_receipt(
    stack: Stack, limit: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, _, provider = stack
    strategy = owner.create(name="Task", intent="Inspect inputs")
    entered, release = threading.Event(), threading.Event()
    original = provider.stream
    provider.responses.append(tool("discover_resources", {}))

    def slow(**kwargs: Any) -> Iterator[ModelStreamEvent]:
        entered.set()
        assert release.wait(5)
        yield from original(**kwargs)

    monkeypatch.setattr(provider, "stream", slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(start, owner, strategy, limit)
        assert entered.wait(5)
        try:
            owner.cancel_conversation_task(strategy.user_strategy_id, turn_id="task-1")
            owner.cancel_conversation_task(strategy.user_strategy_id, turn_id="task-1")
        finally:
            release.set()
        running.result(timeout=5)
    assert len(provider.calls) == 1
    task = owner.conversation_state(strategy.user_strategy_id)["task"]
    assert task["used"] == 1 and task["stop_reason"] == "cancelled"
    assert not owner.get(strategy.user_strategy_id).calls[0].get("tool_results")
    assert owner.conversation_state(strategy.user_strategy_id)["pending"] is None


def test_consecutive_tool_failures_stop_with_reason_inside_budget(stack: Stack) -> None:
    owner, _, provider = stack
    strategy = owner.create(name="Task", intent="Inspect inputs")
    provider.responses.extend(
        [tool("inspect_research_input", {"base_revision": 99}, f"bad-{i}") for i in range(2)]
    )
    start(owner, strategy, 8)
    task = owner.conversation_state(strategy.user_strategy_id)["task"]
    assert len(provider.calls) == 2 and task["stop_reason"] == "consecutive_failures"


def test_legacy_approval_is_not_silently_upgraded_and_task_requires_integer(stack: Stack) -> None:
    owner, _, provider = stack
    strategy = owner.create(name="Task", intent="Inspect inputs")
    with pytest.raises(UserStrategyBudgetError):
        list(
            owner.conversation_turn(
                strategy.user_strategy_id,
                call_id="legacy",
                base_revision=1,
                text="Old approval",
                approved_budget={"max_model_calls": 5, "max_backtests": 0},
            )
        )
    with pytest.raises(UserStrategyBudgetError):
        start(owner, strategy, True)
    assert not provider.calls


def test_budget_resume_after_checkpoint_loss_uses_saved_read_receipt(stack: Stack) -> None:
    import sqlite3

    owner, _, provider = stack
    strategy = owner.create(name="Task", intent="Inspect inputs")
    provider.responses.extend([tool("discover_resources", {}), _response([], text="Done")])
    start(owner, strategy, 1)
    with sqlite3.connect(owner._store.conversation_db_path) as connection:
        connection.execute("DELETE FROM checkpoints")
        connection.execute("DELETE FROM writes")
    topup(owner, strategy, 3)
    state = owner.conversation_state(strategy.user_strategy_id)
    assert state["pending"] is None and state["task"]["stop_reason"] == "completed"
    assert len(provider.calls) == 2
    assert any(m.role == "tool" and '"ok": true' in m.text for m in provider.calls[-1]["messages"])


def test_duplicate_concurrent_topups_reserve_once(stack: Stack) -> None:
    owner, _, provider = stack
    strategy = owner.create(name="Task", intent="Inspect inputs")
    provider.responses.extend([tool("discover_resources", {}), _response([], text="Done")])
    start(owner, strategy, 1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(topup, owner, strategy, 2)
        b = pool.submit(topup, owner, strategy, 2)
        a.result(timeout=5)
        b.result(timeout=5)
    assert len(provider.calls) == 2
    state = owner.conversation_state(strategy.user_strategy_id)
    assert state["task"]["used"] == 2 and len(state["task"]["grants"]) == 1
    assert state["pending"] is None


def test_task_experiment_explanation_shares_budget_and_requires_confirmation(stack: Stack) -> None:
    from unit import test_web_conversation as c

    owner, service, provider = stack
    strategy = c._create(owner)
    provider.responses.extend(
        [
            c._change("parameters.target_quantity", "3"),
            _response([], text="Verified report explained"),
        ]
    )
    events = start(owner, strategy, 1)
    assert events[-1]["waiting"]["kind"] == "proposal"
    proposal = owner.get(strategy.user_strategy_id).proposals[0]["proposal_id"]
    owner.apply_proposal(strategy.user_strategy_id, proposal_id=proposal, operation_id="apply-task")
    list(
        owner.conversation_resume(
            strategy.user_strategy_id, turn_id="task-1", action="applied", proposal_id=proposal
        )
    )
    assert not service.list_jobs()
    pending = owner.conversation_state(strategy.user_strategy_id)["pending"]
    events = list(
        owner.conversation_resume(
            strategy.user_strategy_id,
            turn_id="task-1",
            action="confirm",
            base_revision=pending["base_revision"],
            launch_intent_id=pending["launch_intent_id"],
            preview_identity=pending["preview_identity"],
        )
    )
    for _ in range(100):
        pending = owner.conversation_state(strategy.user_strategy_id)["pending"]
        if pending["phase"] != "awaiting_observation":
            break
        import time

        time.sleep(0.01)
        events = list(
            owner.conversation_resume(strategy.user_strategy_id, turn_id="task-1", action="observe")
        )
    assert pending["phase"] == "awaiting_budget"
    assert len(provider.calls) == 1 and len(service.list_jobs()) == 1
    topup(owner, strategy, 2)
    assert len(provider.calls) == 2 and len(service.list_jobs()) == 1
    state = owner.conversation_state(strategy.user_strategy_id)
    assert state["task"]["used"] == 2 and state["pending"] is None
    assert any(m.text == _EXPLAIN_INSTRUCTION for m in provider.calls[-1]["messages"])


def test_http_budget_and_cancel_controls_restore_after_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from ea.web.app import create_app
    from ea.web.user_strategies import UserStrategyService
    from unit import test_web_conversation as c
    from unit.test_web_api import ORIGIN, WRITE_HEADERS, _settings

    provider = c.Streaming([tool("discover_resources", {}), _response([], text="Done")])
    original = UserStrategyService.__init__

    def inject(self: UserStrategyService, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        self._provider = provider

    monkeypatch.setattr(UserStrategyService, "__init__", inject)
    with TestClient(create_app(_settings(tmp_path)), base_url=ORIGIN) as client:
        strategy = client.post(
            "/api/user-strategies",
            headers=WRITE_HEADERS,
            json={"name": "task", "intent": "Inspect inputs"},
        ).json()
        url = f"/api/user-strategies/{strategy['user_strategy_id']}"
        response = client.post(
            url + "/messages/stream",
            headers=WRITE_HEADERS,
            json={
                "call_id": "task-1",
                "base_revision": 1,
                "text": "Inspect inputs",
                "approved_budget": {"max_model_calls": 1, "max_backtests": 0},
                "task_budget": 1,
            },
        )
        assert response.status_code == 200
        state = client.get(url + "/conversation").json()
        assert state["pending"]["phase"] == "awaiting_budget"
        assert state["task"]["used"] == 1
        response = client.post(
            url + "/conversation/resume",
            headers=WRITE_HEADERS,
            json={
                "turn_id": "task-1",
                "action": "budget",
                "grant_id": "grant-1",
                "total_model_calls": 2,
            },
        )
        assert response.status_code == 200
        state = client.get(url + "/conversation").json()
        assert state["pending"] is None and state["task"]["stop_reason"] == "completed"
        assert len(provider.calls) == 2
        provider.responses.append(_response([], text="New interactive request"))
        response = client.post(
            url + "/messages/stream",
            headers=WRITE_HEADERS,
            json={"call_id": "normal-new", "base_revision": 1, "text": "Continue research"},
        )
        assert response.status_code == 200
        assert client.get(url + "/conversation").json()["task"]["mode"] == "interactive"
        assert len(provider.calls) == 3


def test_restart_classifies_dispatched_task_without_refund_or_resend(stack: Stack) -> None:
    from dataclasses import replace

    owner, _, provider = stack
    strategy = owner.create(name="Task", intent="Inspect inputs")
    provider.responses.append(_response([], text="Done"))
    start(owner, strategy, 3)
    stored = owner._store.get(strategy.user_strategy_id)
    root = {**stored.calls[0], "status": "dispatched"}
    root.pop("response", None)
    owner._store.save(replace(stored, calls=(root,)))
    owner._store.update_task(strategy.user_strategy_id, "task-1", status="running", reason=None)
    owner.reconcile_all()
    owner.reconcile_all()
    state = owner.conversation_state(strategy.user_strategy_id)
    assert state["task"]["stop_reason"] == "unknown" and state["task"]["used"] == 1
    assert len(provider.calls) == 1


def test_unconfigured_task_releases_proven_absence_once(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ea.web.llm import ModelNotConfigured

    owner, _, provider = stack
    strategy = owner.create(name="Task", intent="Inspect inputs")

    def missing(*args: Any) -> NoReturn:
        raise ModelNotConfigured("synthetic unavailable provider")

    monkeypatch.setattr(owner, "_port_for_call", missing)
    start(owner, strategy, 3)
    for _ in range(2):
        owner.reconcile_all()
        state = owner.conversation_state(strategy.user_strategy_id)
        assert state["task"]["stop_reason"] == "unconfigured"
        assert state["task"]["used"] == 0 and state["task"]["remaining"] == 3
    assert not provider.calls


def test_replayed_spent_grant_keeps_waiting_and_allows_a_new_grant(stack: Stack) -> None:
    owner, _, provider = stack
    strategy = owner.create(name="Task", intent="Inspect inputs")
    provider.responses.extend(
        [
            tool("discover_resources", {}, "read-1"),
            tool("discover_resources", {}, "read-2"),
            _response([], text="Done"),
        ]
    )
    start(owner, strategy, 1)
    topup(owner, strategy, 2)
    repeated = topup(owner, strategy, 2)
    assert repeated[-1]["kind"] == "interrupt"
    state = owner.conversation_state(strategy.user_strategy_id)
    assert state["task"]["status"] == "awaiting_budget" and state["task"]["used"] == 2
    topup(owner, strategy, 3, "grant-2")
    assert len(provider.calls) == 3
    assert owner.conversation_state(strategy.user_strategy_id)["task"]["stop_reason"] == "completed"


def test_failed_read_count_survives_lost_budget_checkpoint(stack: Stack) -> None:
    import sqlite3

    owner, _, provider = stack
    strategy = owner.create(name="Task", intent="Inspect inputs")
    provider.responses.extend(
        [
            tool("inspect_research_input", {"base_revision": 99}, "bad-1"),
            tool("inspect_research_input", {"base_revision": 99}, "bad-2"),
            _response([], text="Must not dispatch"),
        ]
    )
    start(owner, strategy, 1)
    with sqlite3.connect(owner._store.conversation_db_path) as connection:
        connection.execute("DELETE FROM checkpoints")
        connection.execute("DELETE FROM writes")
    topup(owner, strategy, 3)
    assert len(provider.calls) == 2
    assert (
        owner.conversation_state(strategy.user_strategy_id)["task"]["stop_reason"]
        == "consecutive_failures"
    )


def test_successful_legacy_read_resets_failure_count(stack: Stack) -> None:
    from unit import test_web_conversation as c

    owner, _, provider = stack
    strategy = c._create(owner)
    provider.responses.extend(
        [
            tool("inspect_research_input", {"base_revision": 99}, "bad-1"),
            tool("describe_data", {}, "good"),
            tool("inspect_research_input", {"base_revision": 99}, "bad-2"),
            _response([], text="Done"),
        ]
    )
    start(owner, strategy, 4)
    assert len(provider.calls) == 4
    assert owner.conversation_state(strategy.user_strategy_id)["task"]["stop_reason"] == "completed"


def test_interactive_message_needs_no_numeric_grant_and_keeps_researching(stack: Stack) -> None:
    owner, service, provider = stack
    strategy = owner.create(name="Continuous", intent="Inspect research inputs")
    provider.responses.extend(
        [tool("discover_resources", {"symbol": str(i)}, f"r-{i}") for i in range(6)]
    )
    provider.responses.append(_response([], text="Inputs inspected."))
    events = list(
        owner.conversation_turn(
            strategy.user_strategy_id, call_id="normal", base_revision=1, text="Inspect inputs"
        )
    )
    assert len(provider.calls) == 7
    assert events[-1]["kind"] == "done"
    state = owner.conversation_state(strategy.user_strategy_id)
    assert state["task"]["mode"] == "interactive"
    assert state["task"]["stop_reason"] == "completed"
    assert state["budget"]["task_budget_required"] is False
    assert not service.list_jobs()


def test_old_exhausted_task_can_continue_without_choosing_a_number(stack: Stack) -> None:
    owner, _, provider = stack
    strategy = owner.create(name="Legacy", intent="Inspect inputs")
    provider.responses.extend([tool("discover_resources", {}), _response([], text="Continued")])
    start(owner, strategy, 1)
    original = owner.get(strategy.user_strategy_id).calls[0]["request"]
    for _ in range(2):
        list(
            owner.conversation_resume(
                strategy.user_strategy_id,
                turn_id="task-1",
                action="continue",
                grant_id="continue-1",
            )
        )
    assert len(provider.calls) == 2
    saved = owner.get(strategy.user_strategy_id)
    assert saved.calls[0]["request"] == original
    assert owner.conversation_state(strategy.user_strategy_id)["pending"] is None


def test_interactive_safety_pause_is_not_success_and_survives_checkpoint_loss(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ea.web import user_strategies as module

    owner, _, provider = stack
    monkeypatch.setattr(module, "_INTERACTIVE_MAX_DISPATCHES", 2)
    strategy = owner.create(name="Guard", intent="Inspect inputs")
    provider.responses.extend(
        [tool("discover_resources", {"symbol": str(i)}, f"r-{i}") for i in range(2)]
    )
    provider.responses.append(_response([], text="Finished"))
    list(
        owner.conversation_turn(
            strategy.user_strategy_id, call_id="normal", base_revision=1, text="Inspect"
        )
    )
    state = owner.conversation_state(strategy.user_strategy_id)
    assert state["task"]["stop_reason"] == "execution_limit"
    assert state["pending"]["phase"] == "awaiting_budget"
    with owner._store._write() as db:
        for table in ("checkpoints", "writes"):
            db.execute(f"DELETE FROM {table}")
    list(
        owner.conversation_resume(
            strategy.user_strategy_id, turn_id="normal", action="continue", grant_id="next"
        )
    )
    assert len(provider.calls) == 3
    assert owner.conversation_state(strategy.user_strategy_id)["task"]["stop_reason"] == "completed"


def test_stop_cuts_partial_generation_without_claiming_completion_then_new_message_can_run(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, _, provider = stack
    strategy = owner.create(name="Stop", intent="Inspect inputs")
    original = provider.stream

    def partial(**kwargs: Any) -> Iterator[ModelStreamEvent]:
        yield ModelStreamEvent("text_delta", text="first")
        owner.cancel_conversation_task(strategy.user_strategy_id, turn_id="partial")
        yield ModelStreamEvent("text_delta", text="must not be emitted")
        raise AssertionError("cancelled stream should have closed")

    monkeypatch.setattr(provider, "stream", partial)
    events = list(
        owner.conversation_turn(
            strategy.user_strategy_id, call_id="partial", base_revision=1, text="Inspect"
        )
    )
    assert not any(e.get("text") == "must not be emitted" for e in events)
    state = owner.conversation_state(strategy.user_strategy_id)
    assert state["task"]["stop_reason"] == "cancelled"
    assert owner.get(strategy.user_strategy_id).calls[0]["status"] == "unknown"
    owner.reconcile_all()
    monkeypatch.setattr(provider, "stream", original)
    provider.responses.append(_response([], text="New direction"))
    list(
        owner.conversation_turn(
            strategy.user_strategy_id, call_id="next", base_revision=1, text="Use another approach"
        )
    )
    assert len(provider.calls) == 1
    assert owner.conversation_state(strategy.user_strategy_id)["task"]["stop_reason"] == "completed"
