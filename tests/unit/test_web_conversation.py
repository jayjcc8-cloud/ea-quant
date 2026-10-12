"""The bounded conversation graph: one turn, its tools, and the decisions it waits for.

Everything here is the real product owner — the store, its canonical records, the
service, the real WebService and its engine — except the model provider, which is a
scripted double that never touches the network. The journey test therefore runs a
real backtest through the real engine and reads its real formal report; it does not
exercise the separately installed Rust core owner, which is reported as a limitation
rather than claimed.

The provider double can stream or answer whole, and the tests use both: a streaming
port produces real deltas, a complete-only port produces none, and neither is ever
sliced into a reply it did not send.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any, cast

import pytest

from ea.web.llm import ModelCapabilities, ModelResponse, ModelRoute, ModelStreamEvent
from ea.web.research import ResearchOrchestrator, ResearchStore
from ea.web.service import WebService
from ea.web.user_strategies import (
    _EXPLAIN_INSTRUCTION,
    StrategyDraft,
    UserStrategy,
    UserStrategyService,
    UserStrategyStateError,
    UserStrategyStore,
)
from unit.test_web_research import FakeProvider, _response, _scenario_root, _settings_store

SCENARIO = "bounded-long.yaml"
CRITERION = {"field": "economics.net_pnl.amount", "expectation": "positive"}
ROUTE = ModelRoute(
    provider="fixture",
    protocol="fixture-messages",
    model="fixture-model",
    endpoint="https://fixture.invalid/v1/messages",
    capabilities=ModelCapabilities(tool_use=True, structured_output=False, usage=True),
)
#: Enough approved model calls for one loop that reads, proposes and explains.
WIDE_BUDGET = {"max_model_calls": 6, "max_backtests": 2}
#: A workspace grant that also covers the calls a migration imports as reserved.
LONG_BUDGET = {"max_model_calls": 20, "max_backtests": 2}
RUN_BUDGET = {"max_model_calls": 0, "max_backtests": 1}


class Routed(FakeProvider):
    """A complete-only port: it answers whole and therefore sends no delta at all."""

    def route(self) -> ModelRoute:
        return ROUTE


class Streaming(Routed):
    """A port that really streams: every delta below is one the provider sent."""

    def stream(self, **kwargs: Any) -> Iterator[ModelStreamEvent]:
        snapshot = dict(kwargs)
        snapshot["messages"] = list(kwargs.get("messages") or [])
        self.calls.append(snapshot)
        answer: Any = self.responses.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        for index in range(0, len(answer.text), 4):
            yield ModelStreamEvent("text_delta", text=answer.text[index : index + 4])
        yield ModelStreamEvent("response", response=answer)


@pytest.fixture
def _started() -> Iterator[list[WebService]]:
    started: list[WebService] = []
    yield started
    while started:
        started.pop().stop()


def _stack(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    started: list[WebService],
    provider: Any,
    *,
    budget: dict[str, int] | None = None,
) -> UserStrategyService:
    service = WebService(_scenario_root(tmp_path), tmp_path / "workspace")
    service.start()
    started.append(service)
    strategies = UserStrategyService(
        UserStrategyStore(tmp_path / "workspace"),
        ResearchOrchestrator(
            ResearchStore(tmp_path / "workspace"), service, _settings_store(tmp_path, monkeypatch)
        ),
        service,
        _settings_store(tmp_path, monkeypatch),
        budget=budget,
    )
    cast(Any, strategies)._provider = provider
    return strategies


def _store(tmp_path: Any) -> UserStrategyStore:
    return UserStrategyStore(tmp_path / "workspace")


def _create(strategies: UserStrategyService, name: str = "Long AAPL") -> UserStrategy:
    return strategies.create(
        name=name,
        intent="Test one registered entry on the fixture data",
        scenario_id=SCENARIO,
        criterion=CRITERION,
    )


def _change(path: str, value: Any, tool_id: str = "tool-1") -> ModelResponse:
    return _response(
        [
            {
                "id": tool_id,
                "name": "propose_change",
                "input": {
                    "items": [{"path": path, "value": value, "reason": "the user asked for it"}]
                },
            }
        ],
        text="I would change one registered field.",
    )


def _read_tool(name: str, tool_id: str) -> ModelResponse:
    return _response([{"id": tool_id, "name": name, "input": {}}])


def _events(source: Iterator[dict[str, Any]]) -> list[dict[str, Any]]:
    return list(source)


def _kinds(events: list[dict[str, Any]]) -> list[str]:
    return [str(item["kind"]) for item in events]


# -- the retained conversation --------------------------------------------


def test_a_retained_conversation_keeps_its_records_across_migration_and_a_turn(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, _started: list[WebService]
) -> None:
    provider = Routed([_response([], text=f"answer {index}") for index in range(1, 8)])
    strategies = _stack(tmp_path, monkeypatch, _started, provider, budget=LONG_BUDGET)
    created = _create(strategies)
    for index in range(1, 8):
        strategies._ask(
            strategies.get(created.user_strategy_id),
            call_id=f"turn-{index}",
            base_revision=1,
            text=f"question {index}",
            approved_budget={"max_model_calls": 1, "max_backtests": 0},
        )
    before = strategies.get(created.user_strategy_id)
    # Opening a retained conversation accounts for historical calls before the UI
    # offers a new authorization; the first send cannot reveal a smaller allowance.
    opened = strategies.conversation_state(created.user_strategy_id)
    assert opened["budget"]["reserved_model_calls"] == 7
    assert opened["budget"]["remaining_model_calls"] == 13
    assert len(before.messages) == 14
    identifiers = [item["message_id"] for item in before.messages]

    store = _store(tmp_path)
    store.migrate_conversation(created.user_strategy_id)
    migrated = store.migrate_conversation(created.user_strategy_id)
    reopened = _store(tmp_path).get(created.user_strategy_id)

    # Every message, call and proposal survives the cutover, in its own order and
    # with its own identity, and the archived JSON stops being the authority.
    assert [item["message_id"] for item in migrated.messages] == identifiers
    assert [item["message_id"] for item in reopened.messages] == identifiers
    assert [item["call_id"] for item in reopened.calls] == [
        item["call_id"] for item in before.calls
    ]
    assert [item["role"] for item in reopened.messages] == [
        item["role"] for item in before.messages
    ]

    # A later turn still answers with the earlier turns in front of it.
    provider.responses.append(_response([], text="the eighth answer"))
    answered = strategies.send_message(
        created.user_strategy_id,
        call_id="turn-8",
        base_revision=1,
        text="question 8",
        approved_budget={"max_model_calls": 1, "max_backtests": 0},
    )
    wire = [message.text for message in provider.calls[-1]["messages"]]
    assert "question 7" in wire and "the eighth answer" not in wire
    assert "question 1" in wire
    assert answered.messages[-1]["text"] == "the eighth answer"
    assert answered.messages[-1]["message_id"] not in identifiers


# -- one controlled journey ------------------------------------------------


def test_the_whole_turn_runs_as_a_graph_with_real_streamed_deltas(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, _started: list[WebService]
) -> None:
    provider = Streaming(
        [
            _read_tool("describe_data", "read-1"),
            _change("parameters.target_quantity", "3", tool_id="propose-1"),
            _response([], text="the change is applied and nothing has run yet"),
        ]
    )
    strategies = _stack(tmp_path, monkeypatch, _started, provider, budget=WIDE_BUDGET)
    created = _create(strategies)

    events = _events(
        strategies.conversation_turn(
            created.user_strategy_id,
            call_id="turn-1",
            base_revision=1,
            text="please raise the quantity",
            approved_budget=dict(WIDE_BUDGET),
        )
    )
    kinds = _kinds(events)

    # The model's own deltas arrived as they were sent, and they are exactly the
    # text it sent — nothing was sliced out of a finished answer.
    deltas = [item["text"] for item in events if item["kind"] == "text_delta"]
    assert "".join(deltas) == "I would change one registered field."
    assert len(deltas) == -(-len("I would change one registered field.") // 4)
    assert "tool_start" in kinds and "tool_end" in kinds
    started = [item for item in events if item["kind"] == "tool_start"]
    assert [item["tool"] for item in started] == ["describe_data", "propose_change"]
    assert kinds[-1] == "interrupt"
    assert events[-1]["waiting"]["kind"] == "proposal"

    # The proposal is a real record of this strategy, not text in a message.
    strategy = strategies.get(created.user_strategy_id)
    assert [item["status"] for item in strategy.proposals] == ["open"]
    proposal_id = events[-1]["waiting"]["proposal_id"]
    assert strategy.proposals[0]["proposal_id"] == proposal_id
    assert strategy.proposals[0]["call_id"] == strategy.calls[1]["call_id"]
    assert strategy.proposals[0]["base_revision"] == 1
    assert strategy.conversation_confirmation is not None
    assert strategy.conversation_confirmation["phase"] == "awaiting_proposal"
    # Two model calls were really made: the read loop and the proposal.
    assert len(provider.calls) == 2

    state = strategies.conversation_state(created.user_strategy_id)
    assert state["pending"]["phase"] == "awaiting_proposal"
    assert state["pending"]["proposal_id"] == proposal_id
    assert state["open_proposals"] == [proposal_id]


def test_a_decision_that_names_another_proposal_is_refused_before_anything_runs(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, _started: list[WebService]
) -> None:
    provider = Streaming([_change("parameters.target_quantity", "3")])
    strategies = _stack(tmp_path, monkeypatch, _started, provider, budget=WIDE_BUDGET)
    created = _create(strategies)
    _events(
        strategies.conversation_turn(
            created.user_strategy_id,
            call_id="turn-1",
            base_revision=1,
            text="raise it",
            approved_budget=dict(WIDE_BUDGET),
        )
    )

    with pytest.raises(UserStrategyStateError):
        _events(
            strategies.conversation_resume(
                created.user_strategy_id,
                turn_id="turn-1",
                action="applied",
                proposal_id="prop-something-else",
            )
        )
    assert strategies.get(created.user_strategy_id).draft.revision == 1


def test_an_apply_alone_runs_nothing_and_confirming_needs_the_exact_preview(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, _started: list[WebService]
) -> None:
    provider = Streaming([_change("parameters.target_quantity", "3")])
    strategies = _stack(tmp_path, monkeypatch, _started, provider, budget=WIDE_BUDGET)
    created = _create(strategies)
    events = _events(
        strategies.conversation_turn(
            created.user_strategy_id,
            call_id="turn-1",
            base_revision=1,
            text="raise it",
            approved_budget=dict(WIDE_BUDGET),
        )
    )
    proposal_id = events[-1]["waiting"]["proposal_id"]

    # Resuming "applied" before the proposal was applied is refused, with its reason
    # on the stream, and the turn keeps waiting: it never applies anything itself and
    # never reads an apply out of the model's own text.
    refused = _events(
        strategies.conversation_resume(
            created.user_strategy_id, turn_id="turn-1", action="applied", proposal_id=proposal_id
        )
    )
    assert "error" in _kinds(refused)
    assert refused[-1]["kind"] == "interrupt"
    assert refused[-1]["waiting"]["kind"] == "proposal"
    assert strategies.get(created.user_strategy_id).draft.revision == 1
    assert strategies.conversation_state(created.user_strategy_id)["pending"]["proposal_id"] == (
        proposal_id
    )

    # The existing apply semantics remain the only way a draft changes.
    applied = strategies.apply_proposal(
        created.user_strategy_id, proposal_id=proposal_id, operation_id="op-1"
    )
    assert applied.draft.revision == 2

    resumed = _events(
        strategies.conversation_resume(
            created.user_strategy_id, turn_id="turn-1", action="applied", proposal_id=proposal_id
        )
    )
    assert _kinds(resumed)[-1] == "interrupt"
    waiting = resumed[-1]["waiting"]
    assert waiting["kind"] == "confirmation"
    assert waiting["base_revision"] == 2
    assert waiting["preview_identity"].startswith("sha256:")
    # Applying a change never runs anything by itself.
    strategy = strategies.get(created.user_strategy_id)
    assert strategy.experiments == ()

    with pytest.raises(UserStrategyStateError):
        _events(
            strategies.conversation_resume(
                created.user_strategy_id,
                turn_id="turn-1",
                action="confirm",
                launch_intent_id=waiting["launch_intent_id"],
                base_revision=waiting["base_revision"],
                preview_identity="sha256:" + "0" * 64,
            )
        )
    assert strategies.get(created.user_strategy_id).experiments == ()


def test_the_confirmed_journey_observes_the_real_run_and_explains_its_report(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, _started: list[WebService]
) -> None:
    provider = Streaming(
        [
            _read_tool("describe_data", "read-1"),
            _read_tool("experiment_status", "read-2"),
            _change("parameters.target_quantity", "3", tool_id="propose-1"),
            _response([], text="the run reports a positive net P&L, as its verified report states"),
        ]
    )
    strategies = _stack(tmp_path, monkeypatch, _started, provider, budget=WIDE_BUDGET)
    created = _create(strategies)
    events = _events(
        strategies.conversation_turn(
            created.user_strategy_id,
            call_id="turn-1",
            base_revision=1,
            text="raise the quantity and run it",
            approved_budget=dict(WIDE_BUDGET),
        )
    )
    proposal_id = events[-1]["waiting"]["proposal_id"]
    strategies.apply_proposal(
        created.user_strategy_id, proposal_id=proposal_id, operation_id="op-1"
    )
    preview = _events(
        strategies.conversation_resume(
            created.user_strategy_id, turn_id="turn-1", action="applied", proposal_id=proposal_id
        )
    )[-1]["waiting"]

    confirmed = _events(
        strategies.conversation_resume(
            created.user_strategy_id,
            turn_id="turn-1",
            action="confirm",
            launch_intent_id=preview["launch_intent_id"],
            base_revision=preview["base_revision"],
            preview_identity=preview["preview_identity"],
        )
    )

    strategy = strategies.get(created.user_strategy_id)
    assert len(strategy.experiments) == 1
    experiment = strategy.experiments[0]
    assert experiment["launch_intent_id"] == preview["launch_intent_id"]
    assert experiment["payload"]["draft_revision"] == 2
    assert experiment["payload"]["approved_budget"] == RUN_BUDGET
    # The run is real: a job of this workspace really produced a verified report.
    job_id = str(experiment["job_id"])
    assert confirmed[-1]["kind"] in {"interrupt", "done"}
    if confirmed[-1]["kind"] == "done":
        assert strategy.messages[-1]["text"].startswith("the run reports")
    else:
        # The run was still going: observing it again reads the same run, and the
        # resume never launches a second one.
        assert confirmed[-1]["waiting"]["kind"] == "observation"
        while True:
            again = _events(
                strategies.conversation_resume(
                    created.user_strategy_id, turn_id="turn-1", action="observe"
                )
            )
            if again[-1]["kind"] == "done":
                break
            assert again[-1]["waiting"]["kind"] == "observation"
        assert len(strategies.get(created.user_strategy_id).experiments) == 1
        assert len(_started[0].list_jobs()) == 1
    assert (
        strategies.get(created.user_strategy_id).messages[-1]["text"].startswith("the run reports")
    )
    report = json.loads(_started[0].report(job_id))
    assert report["run_id"]
    assert provider.calls[-1]["messages"]
    assert strategies.get(created.user_strategy_id).calls[-1]["evidence"][0]["job_id"] == job_id


def test_a_lost_confirmation_reply_resolves_to_the_same_run(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, _started: list[WebService]
) -> None:
    provider = Streaming([_change("parameters.target_quantity", "3")])
    strategies = _stack(tmp_path, monkeypatch, _started, provider, budget=WIDE_BUDGET)
    created = _create(strategies)
    events = _events(
        strategies.conversation_turn(
            created.user_strategy_id,
            call_id="turn-1",
            base_revision=1,
            text="raise it",
            approved_budget=dict(WIDE_BUDGET),
        )
    )
    proposal_id = events[-1]["waiting"]["proposal_id"]
    strategies.apply_proposal(
        created.user_strategy_id, proposal_id=proposal_id, operation_id="op-1"
    )
    preview = _events(
        strategies.conversation_resume(
            created.user_strategy_id, turn_id="turn-1", action="applied", proposal_id=proposal_id
        )
    )[-1]["waiting"]
    confirm = {
        "turn_id": "turn-1",
        "action": "confirm",
        "launch_intent_id": preview["launch_intent_id"],
        "base_revision": preview["base_revision"],
        "preview_identity": preview["preview_identity"],
    }
    _events(strategies.conversation_resume(created.user_strategy_id, **confirm))
    once = strategies.get(created.user_strategy_id)

    # The reply is lost, so the client confirms the very same intent again. The
    # existing owner resolves it to the run it already confirmed.
    before = len(_started[0].list_jobs())
    _events(
        strategies.conversation_resume(
            created.user_strategy_id,
            turn_id="turn-1",
            action="confirm",
            **{
                key: value for key, value in confirm.items() if key != "turn_id" and key != "action"
            },
        )
    )
    twice = strategies.get(created.user_strategy_id)
    assert len(twice.experiments) == 1
    assert len(_started[0].list_jobs()) == before
    assert twice.experiments[0]["job_id"] == once.experiments[0]["job_id"]


def test_a_changed_input_makes_the_prior_preview_unconfirmable(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, _started: list[WebService]
) -> None:
    provider = Streaming([_change("parameters.target_quantity", "3")])
    strategies = _stack(tmp_path, monkeypatch, _started, provider, budget=WIDE_BUDGET)
    created = _create(strategies)
    events = _events(
        strategies.conversation_turn(
            created.user_strategy_id,
            call_id="turn-1",
            base_revision=1,
            text="raise it",
            approved_budget=dict(WIDE_BUDGET),
        )
    )
    proposal_id = events[-1]["waiting"]["proposal_id"]
    strategies.apply_proposal(
        created.user_strategy_id, proposal_id=proposal_id, operation_id="op-1"
    )
    preview = _events(
        strategies.conversation_resume(
            created.user_strategy_id, turn_id="turn-1", action="applied", proposal_id=proposal_id
        )
    )[-1]["waiting"]

    # The registered scenario changes after the preview but before the confirmation.
    scenario = _scenario_root(tmp_path) / SCENARIO
    scenario.write_text(
        scenario.read_text(encoding="utf-8").replace(
            "max_order_quantity: '5'", "max_order_quantity: '4'"
        ),
        encoding="utf-8",
    )

    refused = _events(
        strategies.conversation_resume(
            created.user_strategy_id,
            turn_id="turn-1",
            action="confirm",
            launch_intent_id=preview["launch_intent_id"],
            base_revision=preview["base_revision"],
            preview_identity=preview["preview_identity"],
        )
    )
    assert "error" in _kinds(refused)
    assert strategies.get(created.user_strategy_id).experiments == ()
    assert _started[0].list_jobs() == []


def test_pending_state_and_tool_failures_survive_a_reload(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, _started: list[WebService]
) -> None:
    provider = Streaming([_change("parameters.target_quantity", "3")])
    strategies = _stack(tmp_path, monkeypatch, _started, provider, budget=WIDE_BUDGET)
    created = _create(strategies)
    events = _events(
        strategies.conversation_turn(
            created.user_strategy_id,
            call_id="turn-1",
            base_revision=1,
            text="raise it",
            approved_budget=dict(WIDE_BUDGET),
        )
    )
    proposal_id = events[-1]["waiting"]["proposal_id"]

    # A second, reopened service reads the same waiting decision from the store.
    _started.pop().stop()
    reopened = _stack(tmp_path, monkeypatch, _started, Streaming([]), budget=WIDE_BUDGET)
    state = reopened.conversation_state(created.user_strategy_id)
    assert state["pending"]["proposal_id"] == proposal_id
    assert state["pending"]["turn_id"] == "turn-1"
    assert state["budget"]["reserved_model_calls"] >= 1
    assert state["budget"]["remaining_model_calls"] >= 0

    # A tool this conversation does not offer is refused honestly, with no change.
    provider.responses.append(_read_tool("delete_everything", "bad-1"))
    reopened.reject_proposal(created.user_strategy_id, proposal_id=proposal_id)
    _events(
        reopened.conversation_resume(
            created.user_strategy_id, turn_id="turn-1", action="rejected", proposal_id=proposal_id
        )
    )
    rejected = reopened.get(created.user_strategy_id)
    assert [item["status"] for item in rejected.proposals] == ["rejected"]
    assert rejected.draft.revision == 1
    assert rejected.conversation_confirmation is None


def test_a_provider_failure_is_unknown_and_never_fabricates_an_answer(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, _started: list[WebService]
) -> None:
    from ea.web.llm import ModelProviderError

    provider = Streaming([ModelProviderError("model provider is unreachable")])
    strategies = _stack(tmp_path, monkeypatch, _started, provider, budget=WIDE_BUDGET)
    created = _create(strategies)

    events = _events(
        strategies.conversation_turn(
            created.user_strategy_id,
            call_id="turn-1",
            base_revision=1,
            text="a question",
            approved_budget=dict(WIDE_BUDGET),
        )
    )
    strategy = strategies.get(created.user_strategy_id)
    assert strategy.calls[-1]["status"] == "unknown"
    assert [item["role"] for item in strategy.messages] == ["user"]
    assert "text_delta" not in _kinds(events)
    assert events[-1]["kind"] == "done"


def test_a_turn_replays_without_a_second_provider_call_or_reservation(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, _started: list[WebService]
) -> None:
    provider = Streaming([_response([], text="the one answer")])
    strategies = _stack(tmp_path, monkeypatch, _started, provider, budget=WIDE_BUDGET)
    created = _create(strategies)
    _events(
        strategies.conversation_turn(
            created.user_strategy_id,
            call_id="turn-1",
            base_revision=1,
            text="a question",
            approved_budget=dict(WIDE_BUDGET),
        )
    )
    once = strategies.get(created.user_strategy_id)
    assert len(provider.calls) == 1

    # The same identity again — the reply was lost, not the work — is the same turn.
    again = _events(
        strategies.conversation_turn(
            created.user_strategy_id,
            call_id="turn-1",
            base_revision=1,
            text="a question",
            approved_budget=dict(WIDE_BUDGET),
        )
    )
    assert len(provider.calls) == 1
    assert _kinds(again) == ["state", "done"]
    assert strategies.get(created.user_strategy_id) == once


def test_losing_the_checkpoint_never_sends_the_recorded_call_again(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, _started: list[WebService]
) -> None:
    provider = Streaming([_read_tool("describe_data", "read-1"), _response([], text="done")])
    strategies = _stack(tmp_path, monkeypatch, _started, provider, budget=WIDE_BUDGET)
    created = _create(strategies)
    _events(
        strategies.conversation_turn(
            created.user_strategy_id,
            call_id="turn-1",
            base_revision=1,
            text="what data is this?",
            approved_budget=dict(WIDE_BUDGET),
        )
    )
    assert len(provider.calls) == 2
    store = _store(tmp_path)
    store.migrate_conversation(created.user_strategy_id)
    calls = len(provider.calls)

    # Only the graph checkpoints are lost. The canonical records are not, and
    # the second call of this turn is already recorded, so it is never re-sent.
    import sqlite3

    connection = sqlite3.connect(store.conversation_db_path)
    try:
        with connection:
            connection.execute("DELETE FROM checkpoints")
            connection.execute("DELETE FROM writes")
    finally:
        connection.close()

    strategy = strategies.get(created.user_strategy_id)
    assert strategy.calls[-1]["response"]["text"] == "done"
    assert len(strategy.calls) == 2
    assert len(provider.calls) == calls


def test_lost_confirmation_checkpoint_recovers_the_frozen_preview_without_a_new_model_call(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, _started: list[WebService]
) -> None:
    import sqlite3

    provider = Streaming([_change("parameters.target_quantity", "3")])
    strategies = _stack(tmp_path, monkeypatch, _started, provider, budget=WIDE_BUDGET)
    created = _create(strategies)
    first = list(
        strategies.conversation_turn(
            created.user_strategy_id,
            call_id="turn-1",
            base_revision=1,
            text="raise it",
            approved_budget={"max_model_calls": 1, "max_backtests": 0},
        )
    )
    proposal = first[-1]["waiting"]["proposal_id"]
    strategies.apply_proposal(created.user_strategy_id, proposal_id=proposal, operation_id="op-1")
    preview = list(
        strategies.conversation_resume(
            created.user_strategy_id, turn_id="turn-1", action="applied", proposal_id=proposal
        )
    )[-1]["waiting"]
    store = _store(tmp_path)
    with sqlite3.connect(store.conversation_db_path) as connection:
        connection.execute("DELETE FROM checkpoints")
        connection.execute("DELETE FROM writes")
    events = list(
        strategies.conversation_resume(
            created.user_strategy_id,
            turn_id="turn-1",
            action="confirm",
            launch_intent_id=preview["launch_intent_id"],
            base_revision=preview["base_revision"],
            preview_identity=preview["preview_identity"],
        )
    )
    assert not [e for e in events if e.get("code")]
    experiment = strategies.get(created.user_strategy_id).experiments[0]
    assert experiment["launch_intent_id"] == preview["launch_intent_id"]
    assert experiment["payload"]["preview_identity"] == preview["preview_identity"]
    assert len(provider.calls) == store.model_call_usage()["reserved_model_calls"] == 1
    # The same receipt query cannot turn into another dispatch after checkpoints disappear.
    list(
        strategies.conversation_resume(
            created.user_strategy_id,
            turn_id="turn-1",
            action="confirm",
            launch_intent_id=preview["launch_intent_id"],
            base_revision=preview["base_revision"],
            preview_identity=preview["preview_identity"],
        )
    )
    assert len(_started[0].list_jobs()) == 1


def test_cumulative_budget_cannot_be_refreshed_by_a_new_turn_or_strategy(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, _started: list[WebService]
) -> None:
    from ea.web.user_strategies import UserStrategyBudgetError

    provider = Streaming([_read_tool("describe_data", "read-1"), _response([], text="clarify")])
    strategies = _stack(
        tmp_path, monkeypatch, _started, provider, budget={"max_model_calls": 2, "max_backtests": 1}
    )
    first, other = _create(strategies), _create(strategies, "other")
    list(
        strategies.conversation_turn(
            first.user_strategy_id,
            call_id="turn-1",
            base_revision=1,
            text="describe",
            approved_budget={"max_model_calls": 2, "max_backtests": 0},
        )
    )
    for identity in (first.user_strategy_id, other.user_strategy_id):
        with pytest.raises(UserStrategyBudgetError):
            list(
                strategies.conversation_turn(
                    identity,
                    call_id="turn-2",
                    base_revision=1,
                    text="again",
                    approved_budget={"max_model_calls": 1, "max_backtests": 0},
                )
            )
    assert len(provider.calls) == _store(tmp_path).model_call_usage()["reserved_model_calls"] == 2
    calls = strategies.get(first.user_strategy_id).calls
    assert all(call["usage_available"] and call["input_tokens"] == 11 for call in calls)


def test_http_stream_and_reload_preserve_the_separate_confirmation(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from ea.web.app import create_app
    from unit.test_web_api import ORIGIN, WRITE_HEADERS, _settings

    provider = Streaming([_change("parameters.target_quantity", "3")])
    monkeypatch.setattr(UserStrategyService, "_model_port", lambda self: provider)
    with TestClient(create_app(_settings(tmp_path)), base_url=ORIGIN) as client:
        created = client.post(
            "/api/user-strategies",
            headers=WRITE_HEADERS,
            json={"name": "stream", "intent": "Inspect one scenario", "scenario_id": SCENARIO},
        ).json()
        identity = created["user_strategy_id"]
        body = {
            "call_id": "turn-http",
            "base_revision": 1,
            "text": "raise quantity",
            "approved_budget": {"max_model_calls": 2, "max_backtests": 0},
        }
        with client.stream(
            "POST",
            f"/api/user-strategies/{identity}/messages/stream",
            headers=WRITE_HEADERS,
            json=body,
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            events = [
                json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")
            ]
        assert "text_delta" in _kinds(events)
        proposal = events[-1]["waiting"]["proposal_id"]
        applied = client.post(
            f"/api/user-strategies/{identity}/proposals/{proposal}/apply",
            headers=WRITE_HEADERS,
            json={"operation_id": "apply-http"},
        )
        assert applied.status_code == 200
        assert applied.json()["experiments"] == []
        resumed = client.post(
            f"/api/user-strategies/{identity}/conversation/resume",
            headers=WRITE_HEADERS,
            json={"turn_id": "turn-http", "action": "applied", "proposal_id": proposal},
        )
        assert resumed.status_code == 200
        state = client.get(f"/api/user-strategies/{identity}/conversation").json()
        assert state["pending"]["phase"] == "awaiting_confirmation"
        assert state["preview"]["draft_revision"] == 2
        assert client.get(f"/api/user-strategies/{identity}").json()["experiments"] == []


def test_usage_trailer_settles_and_replays_without_a_new_budget_reservation(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, _started: list[WebService]
) -> None:
    from ea.web import llm
    from unit.test_web_model_stream import openai_body, store, transport

    network = transport(
        monkeypatch,
        openai_body(
            text="one settled answer",
            usage={"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
        ),
    )
    provider = llm.create_model_port(
        store(tmp_path / "provider", "openai-chat-completions-compatible")
    )
    strategies = _stack(tmp_path, monkeypatch, _started, provider, budget=WIDE_BUDGET)
    created = _create(strategies)
    request = dict(
        call_id="usage-turn", base_revision=1, text="a question", approved_budget=dict(WIDE_BUDGET)
    )
    events = _events(strategies.conversation_turn(created.user_strategy_id, **request))
    assert events[-1]["kind"] == "done"
    settled = strategies.get(created.user_strategy_id)
    call = settled.calls[-1]
    assert call["status"] == "observed" and call["usage_available"]
    assert (call["input_tokens"], call["output_tokens"]) == (9, 2)
    assert settled.messages[-1]["text"] == "one settled answer"
    assert _store(tmp_path).model_call_usage()["reserved_model_calls"] == 1
    again = _events(strategies.conversation_turn(created.user_strategy_id, **request))
    assert _kinds(again) == ["state", "done"]
    assert strategies.get(created.user_strategy_id) == settled
    assert len(network.requests) == 1
    assert _store(tmp_path).model_call_usage()["reserved_model_calls"] == 1


# -- the explanation's own request context ---------------------------------


#: The identities the applied workflow joins: the launch that confirmed the change,
#: and the run whose verified report an explanation states.
APPLIED_INTENT = "int-applied"
APPLIED_PREVIEW = "sha256:" + "1" * 64
APPLIED_JOB = "job-applied"
APPLIED_RUN = "run-applied"
APPLIED_INPUT = "2" * 64
APPLIED_REPORT = "3" * 64
APPLIED_QUESTION = "raise the quantity and run it"


def _applied_workflow(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, started: list[WebService], provider: Any
) -> tuple[UserStrategyService, UserStrategy, dict[str, Any], list[dict[str, Any]]]:
    """One strategy whose proposal this workspace really applied, with its run provided.

    The turn, its proposal receipt, the apply the user made and the apply change are
    the product's own records. The completed experiment that confirmation created is
    provided here as the already-verified evidence this projection is given, so the
    request-building seam is tested without a second backtest, without the core owner
    and without a real provider.
    """
    strategies = _stack(tmp_path, monkeypatch, started, provider, budget=WIDE_BUDGET)
    created = _create(strategies)
    events = _events(
        strategies.conversation_turn(
            created.user_strategy_id,
            call_id="turn-1",
            base_revision=1,
            text=APPLIED_QUESTION,
            approved_budget={"max_model_calls": 1, "max_backtests": 0},
        )
    )
    proposal_id = events[-1]["waiting"]["proposal_id"]
    strategies.apply_proposal(
        created.user_strategy_id, proposal_id=proposal_id, operation_id="op-1"
    )
    applied = strategies.get(created.user_strategy_id)
    strategies._store.save(
        replace(
            applied,
            experiments=(
                {
                    "launch_intent_id": APPLIED_INTENT,
                    "request_id": "request-applied",
                    "payload_sha256": "0" * 64,
                    "payload": {
                        "draft_revision": applied.draft.revision,
                        "preview_identity": APPLIED_PREVIEW,
                        "input_sha256": APPLIED_INPUT,
                    },
                    "task_id": "task-applied",
                    "job_id": APPLIED_JOB,
                    "status": "created",
                    "message": None,
                    "created_at": "2026-10-12T00:00:00Z",
                },
            ),
        )
    )
    applied = strategies.get(created.user_strategy_id)
    receipt = next(
        item["text"]
        for item in events
        if item["kind"] == "tool_end" and item["tool"] == "propose_change"
    )
    state: dict[str, Any] = {
        "turn_id": "turn-1",
        "message": APPLIED_QUESTION,
        "explain": True,
        "exchange": [
            {
                "role": "assistant",
                "text": "I would change one registered field.",
                "calls": [
                    {
                        "id": "propose-1",
                        "name": "propose_change",
                        "arguments": {
                            "items": [
                                {
                                    "path": "parameters.target_quantity",
                                    "value": "3",
                                    "reason": "the user asked for it",
                                }
                            ]
                        },
                    }
                ],
            },
            {"role": "tool", "call_id": "propose-1", "text": receipt},
        ],
        "proposal_id": proposal_id,
        "launch_intent_id": APPLIED_INTENT,
        "preview_identity": APPLIED_PREVIEW,
        "job_id": APPLIED_JOB,
    }
    evidence: list[dict[str, Any]] = [
        {
            "job_id": APPLIED_JOB,
            "launch_intent_id": APPLIED_INTENT,
            "draft_revision": applied.draft.revision,
            "run_id": APPLIED_RUN,
            "report_sha256": APPLIED_REPORT,
            "input_sha256": APPLIED_INPUT,
        }
    ]
    return strategies, applied, state, evidence


def _explanation(
    strategies: UserStrategyService,
    strategy: UserStrategy,
    state: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> list[str]:
    """The one request an explanation of this workflow would send, as its own texts."""
    return [
        message.text
        for message in strategies._answer_messages(
            strategy,
            state,
            strategies._registered_paths(cast(StrategyDraft, strategy.draft)),
            evidence,
        )
    ]


def test_the_explanation_states_the_applied_workflow_after_the_retained_receipt(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, _started: list[WebService]
) -> None:
    strategies, applied, state, evidence = _applied_workflow(
        tmp_path,
        monkeypatch,
        _started,
        Routed([_change("parameters.target_quantity", "3", tool_id="propose-1")]),
    )
    proposal = applied.proposals[0]
    base = proposal["base_revision"]
    assert (proposal["status"], applied.draft.revision) == ("applied", base + 1)

    texts = _explanation(strategies, applied, state, evidence)
    receipt, update = texts[-3], texts[-2]

    # The receipt this turn recorded is retained exactly as it was written, and it
    # still says what was true when the model first proposed: nothing was applied.
    assert receipt == state["exchange"][1]["text"]
    assert "not applied" in receipt
    assert "no experiment may be run" in receipt
    # The host's own update comes after that retained history and immediately before
    # the instruction to answer, and it names the exact identities of the workflow.
    assert texts[-4] == "I would change one registered field."
    assert texts[-1] == _EXPLAIN_INSTRUCTION
    assert f"Proposal {proposal['proposal_id']} was applied" in update
    assert f"draft revision {base} to the applied revision {base + 1}" in update
    assert f"launch intent {APPLIED_INTENT}" in update
    assert f"preview identity {APPLIED_PREVIEW}" in update
    assert f"job {APPLIED_JOB}" in update
    assert f"engine run {APPLIED_RUN}" in update
    assert f"confirmed input digest {APPLIED_INPUT}" in update
    assert f"report with digest {APPLIED_REPORT}" in update
    assert "completed successfully" in update
    assert "belongs to this applied change" in update
    assert "supersedes it" in update
    assert f"current draft revision is {base + 1}." in update

    # The user then advances the draft, so today's draft is no longer the applied
    # revision. The update still states the revision the report belongs to, and the
    # current draft revision separately, instead of re-reading today's draft.
    strategies.undo(
        applied.user_strategy_id,
        operation_id="op-2",
        head_revision=base + 1,
        target_operation_id="op-1",
    )
    later = strategies.get(applied.user_strategy_id)
    assert later.draft.revision == base + 2
    moved = _explanation(strategies, later, state, evidence)[-2]
    assert f"applied revision {base + 1}" in moved
    assert f"current draft revision is {base + 2}." in moved
    assert f"later draft than the frozen revision {base + 1}" in moved


@pytest.mark.parametrize(
    "mutation",
    ["launch intent", "job", "preview", "report identity", "applied revision"],
)
def test_a_workflow_that_does_not_join_is_refused_instead_of_explained(
    mutation: str,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    _started: list[WebService],
) -> None:
    strategies, applied, state, evidence = _applied_workflow(
        tmp_path,
        monkeypatch,
        _started,
        Routed([_change("parameters.target_quantity", "3", tool_id="propose-1")]),
    )
    if mutation == "launch intent":
        state["launch_intent_id"] = "int-somewhere-else"
    elif mutation == "job":
        state["job_id"] = "job-somewhere-else"
    elif mutation == "preview":
        state["preview_identity"] = "sha256:" + "0" * 64
    elif mutation == "report identity":
        evidence[0]["report_sha256"] = None
    else:
        evidence[0]["draft_revision"] = applied.draft.revision - 1

    # No explanation is built from a workflow that does not join, so this turn never
    # reaches a provider, and nothing in the strategy's own record is added or
    # rewritten to claim a completion the records do not support.
    before = strategies.get(applied.user_strategy_id)
    with pytest.raises(UserStrategyStateError):
        _explanation(strategies, before, state, evidence)
    after = strategies.get(applied.user_strategy_id)
    assert after == before
    assert len(after.calls) == len(applied.calls)
