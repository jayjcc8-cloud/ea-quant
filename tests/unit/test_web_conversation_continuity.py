"""Long-lived interaction retains its ledger while each explicit turn stays bounded."""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from ea.web.service import WebService
from ea.web.user_strategies import (
    _MAX_HISTORY_CHARS,
    UserStrategyBudgetError,
)
from unit.test_web_conversation import Streaming, _create, _read_tool, _stack, _store
from unit.test_web_conversation_store import _call, _message
from unit.test_web_research import _response


@pytest.fixture
def started() -> Iterator[list[WebService]]:
    services: list[WebService] = []
    yield services
    for service in reversed(services):
        service.stop()


def test_more_than_200_calls_continue_without_losing_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list[WebService]
) -> None:
    provider = Streaming([_response([], text="continued")])
    service = _stack(tmp_path, monkeypatch, started, provider)
    strategy = _create(service)
    store = _store(tmp_path)
    store.migrate_conversation(strategy.user_strategy_id)
    calls = tuple(_call(f"old-{i}") for i in range(220))
    messages = tuple(
        _message(i * 2 + offset, role=role, text=f"{role}-{i}", call_id=call["call_id"])
        for i, call in enumerate(calls)
        for offset, role in enumerate(("user", "model"))
    )
    historical = replace(strategy, calls=calls, messages=messages)
    store.save(historical)
    with store._write() as connection:
        store._seed_reservations(connection, historical)
    request = dict(
        call_id="new-turn",
        base_revision=1,
        text="continue",
        approved_budget={"max_model_calls": 4, "max_backtests": 0},
    )
    list(service.conversation_turn(strategy.user_strategy_id, **request))
    result = store.get(strategy.user_strategy_id)
    assert result.calls[:220] == calls
    assert result.messages[:440] == messages
    assert len(result.calls) == 221
    assert len(result.messages) == 442
    assert store.model_call_usage()["reserved_model_calls"] == 221
    wire = [item.text for item in provider.calls[0]["messages"]]
    assert "user-211" not in wire
    assert wire[-17:] == [entry["text"] for entry in messages[-16:]] + ["continue"]
    assert "omitted" in wire[0]
    assert "revision" in wire[0].lower()
    list(service.conversation_turn(strategy.user_strategy_id, **request))
    assert len(provider.calls) == 1
    assert _store(tmp_path).get(strategy.user_strategy_id) == result


@pytest.mark.parametrize("approved", [1, 4])
def test_each_turn_stops_at_its_own_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list[WebService], approved: int
) -> None:
    provider = Streaming([_read_tool("describe_data", f"read-{i}") for i in range(5)])
    service = _stack(tmp_path, monkeypatch, started, provider)
    strategy = _create(service)
    list(
        service.conversation_turn(
            strategy.user_strategy_id,
            call_id="bounded",
            base_revision=1,
            text="keep reading",
            approved_budget={"max_model_calls": approved, "max_backtests": 0},
        )
    )
    assert len(provider.calls) == approved
    assert _store(tmp_path).model_call_usage()["reserved_model_calls"] == approved
    assert service._turn_remaining(service.get(strategy.user_strategy_id), "bounded") == 0
    # Later settings cannot enlarge the saved authorization.
    service._budget = {"max_model_calls": 100, "max_backtests": 5}
    assert service._turn_remaining(service.get(strategy.user_strategy_id), "bounded") == 0


def test_new_approval_above_four_is_rejected_before_any_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list[WebService]
) -> None:
    provider = Streaming([])
    service = _stack(tmp_path, monkeypatch, started, provider)
    strategy = _create(service)
    for approved in (5, 100):
        with pytest.raises(UserStrategyBudgetError):
            list(
                service.conversation_turn(
                    strategy.user_strategy_id,
                    call_id=f"too-wide-{approved}",
                    base_revision=1,
                    text="continue",
                    approved_budget={"max_model_calls": approved, "max_backtests": 0},
                )
            )
    assert service.get(strategy.user_strategy_id).calls == ()
    assert service.get(strategy.user_strategy_id).messages == ()
    assert _store(tmp_path).model_call_usage()["reserved_model_calls"] == 0
    assert provider.calls == []


@pytest.mark.parametrize("intent_only", [False, True])
def test_history_keeps_complete_root_child_and_legacy_turns_and_drops_oversized_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list[WebService], intent_only: bool
) -> None:
    service = _stack(tmp_path, monkeypatch, started, Streaming([]))
    strategy = (
        service.create(name="Intent", intent="research an idea", scenario_id=None)
        if intent_only
        else _create(service)
    )
    root, child, legacy, current = (_call(name) for name in ("root", "child", "legacy", "current"))
    child["request"]["turn_id"] = "root"
    messages = tuple(
        _message(i, role=role, text=text, call_id=call)
        for i, (role, text, call) in enumerate(
            [
                ("user", "old question", "root"),
                ("model", "old read", "root"),
                ("model", "child answer", "child"),
                ("user", "legacy question", "legacy"),
                ("model", "legacy answer", "legacy"),
                ("user", "current question", "current"),
            ]
        )
    )
    strategy = replace(strategy, calls=(root, child, legacy, current), messages=messages)
    history, omitted = service._recorded_turns(strategy, "current question")
    assert not omitted
    assert [item.text for item in history] == [item["text"] for item in messages]
    oversized = dict(messages[1], text="x" * _MAX_HISTORY_CHARS)
    strategy = replace(strategy, messages=(messages[0], oversized, *messages[2:]))
    history, omitted = service._recorded_turns(strategy, "current question")
    assert omitted
    assert [item.text for item in history] == [
        "legacy question",
        "legacy answer",
        "current question",
    ]
    if intent_only:
        wire = service._intent_conversation(strategy, "current question")
    else:
        wire = service._conversation(strategy, "current question", (), None)
    assert [item.text for item in wire[1:]] == [item.text for item in history]
    assert "omitted" in wire[0].text
    assert strategy.messages[1]["text"] == oversized["text"]


def test_a_saved_old_turn_keeps_its_original_approval_on_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list[WebService]
) -> None:
    service = _stack(tmp_path, monkeypatch, started, Streaming([]))
    strategy = _create(service)
    call = _call("old-turn")
    old_budget = {"max_model_calls": 6, "max_backtests": 0}
    call["request"]["approved_budget"] = call["approved_budget"] = old_budget
    store = _store(tmp_path)
    store.migrate_conversation(strategy.user_strategy_id)
    strategy = replace(strategy, calls=(call,))
    store.save(strategy)
    service._budget = {"max_model_calls": 1, "max_backtests": 0}
    replayed = service.send_message(
        strategy.user_strategy_id,
        call_id="old-turn",
        base_revision=1,
        text=call["request"]["text"],
        approved_budget=old_budget,
    )
    assert replayed == strategy
    assert service._turn_remaining(replayed, "old-turn") == 5
