"""Conversation storage: one migrated SQLite authority and one call reservation ledger.

The subject is the existing ``UserStrategyStore``. A legacy workspace holds one
JSON record per strategy; a strategy becomes SQLite-authoritative only by an
explicit, idempotent ``migrate_conversation``, and only a committed row selects
that authority. Every reservation is written before the provider can be reached,
counted cumulatively for the whole workspace, and never refunded.

Only the model provider is a double here. Everything else — the store, its
transactions, the service and the workspace settings — is the real owner.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from ea.web.llm import ModelCapabilities, ModelRoute
from ea.web.research import ResearchOrchestrator, ResearchStore
from ea.web.service import WebService
from ea.web.user_strategies import (
    StrategyDraft,
    UserStrategy,
    UserStrategyBudgetError,
    UserStrategyConflictError,
    UserStrategyError,
    UserStrategyNotFoundError,
    UserStrategyService,
    UserStrategyStore,
    _digest,
)
from unit.test_web_research import FakeProvider, _response, _scenario_root, _settings_store

_AT = "2026-10-01T00:00:00Z"
SCENARIO = "bounded-long.yaml"
CRITERION = {"field": "economics.net_pnl.amount", "expectation": "positive"}
CALL_BUDGET = {"max_model_calls": 1, "max_backtests": 0}
ROUTE = ModelRoute(
    provider="fixture",
    protocol="fixture-messages",
    model="fixture-model",
    endpoint="https://fixture.invalid/v1/messages",
    capabilities=ModelCapabilities(tool_use=True, structured_output=False, usage=True),
)


class _Routed(FakeProvider):
    """The scripted provider, with the route a migrated conversation reserves under."""

    def route(self) -> ModelRoute:
        return ROUTE


def _call(call_id: str, *, status: str = "observed", revision: int = 1) -> dict[str, Any]:
    """One durable call record, exactly as the service writes it."""
    request = {
        "action": "message",
        "text": f"question {call_id}",
        "base_revision": revision,
        "proposal_id": None,
        "approved_budget": {"max_model_calls": 1, "max_backtests": 0},
    }
    return {
        "call_id": call_id,
        "status": status,
        "route": {"provider": "fixture", "model": "fixture-model", "endpoint": ROUTE.endpoint},
        "base_revision": revision,
        "approved_budget": {"max_model_calls": 1, "max_backtests": 0},
        "request": request,
        "payload_sha256": _digest(request),
        "reserved_at": _AT,
        "message": None,
        "model": "fixture-model",
        "input_tokens": 3,
        "output_tokens": 5,
        "usage_available": True,
        "cost": {"amount": None, "available": False, "reason": "no price source"},
    }


def _message(
    index: int,
    *,
    role: str,
    text: str,
    call_id: str,
    kind: str = "message",
    proposal_id: str | None = None,
) -> dict[str, Any]:
    return {
        "message_id": f"message-{index:02d}",
        "role": role,
        "kind": kind,
        "text": text,
        "base_revision": 1,
        "at": _AT,
        "call_id": call_id,
        "proposal_id": proposal_id,
    }


def _draft() -> StrategyDraft:
    return StrategyDraft(
        revision=1,
        strategy_id="bounded-long-v1",
        venue="fixture",
        symbol="AAPL",
        scenario_id=SCENARIO,
        dataset_id=None,
        source_sha256=None,
        initial_cash="10000",
        parameters={"target_quantity": "2"},
    )


def _conversation(identity: str, name: str = "Legacy idea") -> UserStrategy:
    """A legacy conversation: eight messages, four calls, one linked proposal."""
    calls = tuple(_call(f"call-{index}") for index in range(1, 5))
    messages = (
        _message(1, role="user", text="first question", call_id="call-1"),
        _message(2, role="model", text="first answer", call_id="call-1"),
        _message(3, role="user", text="second question", call_id="call-2"),
        _message(4, role="model", text="second answer", call_id="call-2"),
        _message(5, role="user", text="third question", call_id="call-3"),
        _message(6, role="model", text="third answer", call_id="call-3"),
        _message(
            7,
            role="model",
            text="I would change the quantity",
            call_id="call-4",
            kind="proposal",
            proposal_id="proposal-1",
        ),
        _message(8, role="user", text="fourth question", call_id="call-4"),
    )
    proposals = (
        {
            "proposal_id": "proposal-1",
            "call_id": "call-3",
            "base_revision": 1,
            "status": "open",
            "created_at": _AT,
            "items": [{"path": "parameters.target_quantity", "value": "3"}],
            "applied_paths": [],
            "stale_paths": [],
        },
    )
    return UserStrategy(
        user_strategy_id=identity,
        created_at=_AT,
        updated_at=_AT,
        name=name,
        intent="One legacy idea, asked about four times",
        draft=_draft(),
        messages=messages,
        calls=calls,
        proposals=proposals,
    )


def _legacy(tmp_path: Path, strategy: UserStrategy) -> tuple[UserStrategyStore, bytes]:
    store = UserStrategyStore(tmp_path / "workspace")
    store.save(strategy)
    published = tmp_path / "workspace" / "user-strategies" / f"{strategy.user_strategy_id}.json"
    return store, published.read_bytes()


def _published(tmp_path: Path, identity: str) -> bytes:
    return (tmp_path / "workspace" / "user-strategies" / f"{identity}.json").read_bytes()


def _pending(store: UserStrategyStore, identity: str, call_id: str) -> UserStrategy:
    """This strategy's document with one more durable, pre-dispatch call record."""
    current = store.get(identity)
    return replace(current, calls=(*current.calls, _call(call_id, status="pending")))


def _row(store: UserStrategyStore, identity: str) -> dict[str, Any]:
    connection = sqlite3.connect(store.conversation_db_path)
    try:
        found = connection.execute(
            "SELECT document FROM strategy_documents WHERE strategy_id = ?", (identity,)
        ).fetchone()
    finally:
        connection.close()
    assert found is not None
    return cast(dict[str, Any], json.loads(found[0]))


# -- migration -------------------------------------------------------------


def test_a_long_conversation_migrates_with_every_relation_intact(tmp_path: Path) -> None:
    legacy = _conversation("legacy-one")
    store, _ = _legacy(tmp_path, legacy)
    assert not store.conversation_db_path.exists(), "the database is created lazily"

    migrated = store.migrate_conversation("legacy-one")

    assert store.conversation_db_path == (
        tmp_path / "workspace" / "user-strategies" / "conversations.sqlite3"
    )
    assert store.is_conversation_migrated("legacy-one") is True
    assert store.is_conversation_migrated("legacy-two") is False
    assert migrated == legacy
    assert [item["message_id"] for item in migrated.messages] == [
        f"message-{index:02d}" for index in range(1, 9)
    ]
    assert [item["role"] for item in migrated.messages] == [
        "user",
        "model",
        "user",
        "model",
        "user",
        "model",
        "model",
        "user",
    ]
    assert [item["call_id"] for item in migrated.messages] == [
        "call-1",
        "call-1",
        "call-2",
        "call-2",
        "call-3",
        "call-3",
        "call-4",
        "call-4",
    ]
    assert [item["call_id"] for item in migrated.calls] == [
        "call-1",
        "call-2",
        "call-3",
        "call-4",
    ]
    assert migrated.proposals[0]["call_id"] == "call-3"
    assert migrated.proposals[0]["proposal_id"] == "proposal-1"
    assert migrated.messages[6]["proposal_id"] == "proposal-1"
    assert migrated.messages[6]["kind"] == "proposal"
    # The committed row carries the same conversation, and it is now the authority
    # even when the pre-migration JSON source is gone.
    assert _row(store, "legacy-one")["messages"] == list(legacy.messages)
    (tmp_path / "workspace" / "user-strategies" / "legacy-one.json").unlink()
    assert store.get("legacy-one") == legacy


def test_repeating_a_migration_is_idempotent_and_reopening_keeps_the_ids(tmp_path: Path) -> None:
    legacy = _conversation("legacy-one")
    store, before = _legacy(tmp_path, legacy)
    first = store.migrate_conversation("legacy-one")
    reserved = store.model_call_usage()

    second = store.migrate_conversation("legacy-one")

    reopened = UserStrategyStore(tmp_path / "workspace")
    assert second == first == reopened.get("legacy-one") == legacy
    assert reopened.model_call_usage() == reserved
    assert [item["message_id"] for item in second.messages] == [
        item["message_id"] for item in legacy.messages
    ]
    assert _published(tmp_path, "legacy-one") == before
    with pytest.raises(UserStrategyNotFoundError):
        store.migrate_conversation("absent-one")


def test_a_repeated_migration_never_reads_the_archived_source_again(tmp_path: Path) -> None:
    legacy = _conversation("legacy-one")
    store, _ = _legacy(tmp_path, legacy)
    first = store.migrate_conversation("legacy-one")
    source = tmp_path / "workspace" / "user-strategies" / "legacy-one.json"
    source.unlink()

    assert store.migrate_conversation("legacy-one") == first

    source.write_bytes(b"{ not the record it was migrated from")
    assert store.migrate_conversation("legacy-one") == first
    assert store.get("legacy-one") == legacy


def test_a_source_whose_identity_is_not_the_requested_one_is_never_published(
    tmp_path: Path,
) -> None:
    store, _ = _legacy(tmp_path, _conversation("legacy-two"))
    renamed = tmp_path / "workspace" / "user-strategies" / "legacy-one.json"
    renamed.write_bytes(_published(tmp_path, "legacy-two"))

    with pytest.raises(UserStrategyError):
        store.migrate_conversation("legacy-one")

    assert store.is_conversation_migrated("legacy-one") is False
    assert store.model_call_usage() == {"reserved_model_calls": 0, "strategies": {}}
    assert store.get("legacy-two").messages == _conversation("legacy-two").messages


def test_a_save_after_migration_updates_sqlite_and_leaves_the_legacy_json_untouched(
    tmp_path: Path,
) -> None:
    legacy = _conversation("legacy-one")
    store, before = _legacy(tmp_path, legacy)
    migrated = store.migrate_conversation("legacy-one")
    updated = replace(
        migrated,
        name="Renamed after migration",
        updated_at="2026-10-02T00:00:00Z",
        messages=(
            *migrated.messages,
            _message(9, role="user", text="fifth question", call_id="call-4"),
        ),
    )

    store.save(updated)

    assert _published(tmp_path, "legacy-one") == before
    assert _row(store, "legacy-one")["name"] == "Renamed after migration"
    assert UserStrategyStore(tmp_path / "workspace").get("legacy-one") == updated


def test_list_shows_migrated_and_unmigrated_strategies_once_each(tmp_path: Path) -> None:
    store, _ = _legacy(tmp_path, _conversation("legacy-one"))
    store.save(_conversation("legacy-two", name="Second idea"))
    store.migrate_conversation("legacy-one")
    directory = tmp_path / "workspace" / "user-strategies"
    (directory / ".tmp-crash.json").write_bytes(_published(tmp_path, "legacy-two"))
    (directory / "another-identity.json").write_bytes(_published(tmp_path, "legacy-two"))

    listed = store.list()

    assert [item.user_strategy_id for item in listed] == ["legacy-one", "legacy-two"]
    assert [item.name for item in listed] == ["Legacy idea", "Second idea"]
    assert UserStrategyStore(tmp_path / "workspace").list() == listed


def test_a_corrupt_migrated_document_fails_closed_instead_of_falling_back(
    tmp_path: Path,
) -> None:
    legacy = _conversation("legacy-one")
    store, _ = _legacy(tmp_path, legacy)
    store.migrate_conversation("legacy-one")
    connection = sqlite3.connect(store.conversation_db_path)
    try:
        with connection:
            connection.execute(
                "UPDATE strategy_documents SET document = ? WHERE strategy_id = ?",
                ('{"schema": "ea.user-strategy.v1", "messages": "not a list"}', "legacy-one"),
            )
    finally:
        connection.close()

    with pytest.raises(UserStrategyError):
        store.get("legacy-one")

    # The JSON copy is a pre-migration source, never a silent substitute.
    assert store.list() == []
    assert (tmp_path / "workspace" / "user-strategies" / "legacy-one.json").exists()


# -- reservations ----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "counted"),
    [
        ("pending", True),
        ("dispatched", True),
        ("observed", True),
        ("refused", True),
        ("unknown", True),
        ("unconfigured", False),
        ("interrupted_before_dispatch", False),
    ],
)
def test_migration_counts_only_calls_that_may_have_reached_the_provider(
    tmp_path: Path, status: str, counted: bool
) -> None:
    strategy = replace(_conversation("legacy-one"), calls=(_call("call-1", status=status),))
    store, _ = _legacy(tmp_path, strategy)

    store.migrate_conversation("legacy-one")

    usage = store.model_call_usage()
    assert usage == {
        "reserved_model_calls": 1 if counted else 0,
        "strategies": {"legacy-one": 1} if counted else {},
    }


def test_two_conversations_share_one_ceiling_and_a_replay_never_spends_or_resets(
    tmp_path: Path,
) -> None:
    store, _ = _legacy(tmp_path, _conversation("legacy-one"))
    store.save(_conversation("legacy-two", name="Second idea"))
    store.migrate_conversation("legacy-one")
    store.migrate_conversation("legacy-two")
    assert store.model_call_usage() == {
        "reserved_model_calls": 8,
        "strategies": {"legacy-one": 4, "legacy-two": 4},
    }

    asked = _pending(store, "legacy-one", "call-nine")
    reserved = store.reserve_model_call(asked, "call-nine", 9)

    assert reserved["replayed"] is False and reserved["call_id"] == "call-nine"
    assert reserved["reserved_at"] == _AT
    assert store.model_call_usage()["reserved_model_calls"] == 9
    # The strategy document was published in the same transaction as the reservation.
    assert store.get("legacy-one") == asked

    replay = store.reserve_model_call(asked, "call-nine", 9)

    assert replay == {**reserved, "replayed": True}
    assert store.model_call_usage()["reserved_model_calls"] == 9, "a replay never spends"
    assert store.get("legacy-one") == asked

    # The same workspace ceiling covers the other conversation.
    with pytest.raises(UserStrategyBudgetError):
        store.reserve_model_call(_pending(store, "legacy-two", "call-ten"), "call-ten", 9)
    assert store.model_call_usage()["reserved_model_calls"] == 9


def test_a_changed_request_under_a_reserved_call_id_conflicts(tmp_path: Path) -> None:
    store, _ = _legacy(tmp_path, _conversation("legacy-one"))
    store.migrate_conversation("legacy-one")
    asked = _pending(store, "legacy-one", "call-nine")
    store.reserve_model_call(asked, "call-nine", 20)
    changed = _call("call-nine", status="pending")
    changed["request"] = {**changed["request"], "text": "a different question"}
    conflicting = replace(asked, calls=(*asked.calls[:-1], changed))

    with pytest.raises(UserStrategyConflictError):
        store.reserve_model_call(conflicting, "call-nine", 20)

    assert store.model_call_usage()["reserved_model_calls"] == 5
    assert store.get("legacy-one") == asked


def test_an_exhausted_ceiling_publishes_nothing(tmp_path: Path) -> None:
    store, _ = _legacy(tmp_path, _conversation("legacy-one"))
    store.migrate_conversation("legacy-one")
    before = store.get("legacy-one")

    with pytest.raises(UserStrategyBudgetError):
        store.reserve_model_call(_pending(store, "legacy-one", "call-nine"), "call-nine", 4)

    assert store.get("legacy-one") == before, "no call and no message was published"
    assert store.model_call_usage()["reserved_model_calls"] == 4


def test_reserving_requires_a_migrated_conversation(tmp_path: Path) -> None:
    store, _ = _legacy(tmp_path, _conversation("legacy-one"))

    with pytest.raises(UserStrategyError):
        store.reserve_model_call(store.get("legacy-one"), "call-1", 4)

    assert store.model_call_usage() == {"reserved_model_calls": 0, "strategies": {}}


def test_a_reopened_workspace_still_counts_an_unobserved_call(tmp_path: Path) -> None:
    strategy = replace(_conversation("legacy-one"), calls=(_call("call-1", status="unknown"),))
    store, _ = _legacy(tmp_path, strategy)
    store.migrate_conversation("legacy-one")

    reopened = UserStrategyStore(tmp_path / "workspace")

    assert reopened.model_call_usage() == {
        "reserved_model_calls": 1,
        "strategies": {"legacy-one": 1},
    }
    with pytest.raises(UserStrategyBudgetError):
        reopened.reserve_model_call(_pending(reopened, "legacy-one", "call-2"), "call-2", 1)


# -- the service uses the same reservation ---------------------------------


@pytest.fixture
def _started() -> Iterator[list[WebService]]:
    started: list[WebService] = []
    yield started
    while started:
        started.pop().stop()


def _service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    started: list[WebService],
    *,
    budget: dict[str, int] | None = None,
) -> tuple[UserStrategyService, _Routed]:
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
    provider = _Routed([])
    cast(Any, strategies)._provider = provider
    return strategies, provider


def _create(strategies: UserStrategyService, name: str) -> UserStrategy:
    return strategies.create(
        name=name, intent="One legacy idea", scenario_id=SCENARIO, criterion=CRITERION
    )


def test_each_conversation_migrates_before_reserving_its_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _started: list[WebService]
) -> None:
    strategies, provider = _service(tmp_path, monkeypatch, _started)
    migrated = _create(strategies, "Migrated idea")
    legacy = _create(strategies, "Legacy idea")
    store = UserStrategyStore(tmp_path / "workspace")
    before = _published(tmp_path, migrated.user_strategy_id)
    store.migrate_conversation(migrated.user_strategy_id)
    provider.responses.append(_response([], text="An answer for the migrated idea"))
    provider.responses.append(_response([], text="An answer for the legacy idea"))

    answered = strategies.send_message(
        migrated.user_strategy_id,
        call_id="call-1",
        base_revision=1,
        text="A question",
        approved_budget=dict(CALL_BUDGET),
    )
    untouched = strategies.send_message(
        legacy.user_strategy_id,
        call_id="call-1",
        base_revision=1,
        text="A question",
        approved_budget=dict(CALL_BUDGET),
    )

    assert answered.calls[-1]["status"] == "observed"
    assert answered.messages[-1]["text"] == "An answer for the migrated idea"
    assert untouched.messages[-1]["text"] == "An answer for the legacy idea"
    # Both entry points migrate before dispatch; a new conversation cannot refresh
    # the shared allowance. Neither retained JSON is written again.
    assert store.model_call_usage() == {
        "reserved_model_calls": 2,
        "strategies": {migrated.user_strategy_id: 1, legacy.user_strategy_id: 1},
    }
    assert _published(tmp_path, migrated.user_strategy_id) == before
    assert UserStrategyStore(tmp_path / "workspace").get(migrated.user_strategy_id) == answered
    assert (tmp_path / "workspace" / "user-strategies" / f"{legacy.user_strategy_id}.json").exists()


def test_the_workspace_ceiling_refuses_the_next_turn_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _started: list[WebService]
) -> None:
    strategies, provider = _service(
        tmp_path, monkeypatch, _started, budget={"max_model_calls": 1, "max_backtests": 0}
    )
    created = _create(strategies, "One call only")
    store = UserStrategyStore(tmp_path / "workspace")
    store.migrate_conversation(created.user_strategy_id)
    provider.responses.append(_response([], text="The one answer"))
    answered = strategies.send_message(
        created.user_strategy_id,
        call_id="call-1",
        base_revision=1,
        text="The one question",
        approved_budget=dict(CALL_BUDGET),
    )

    assert answered.calls[-1]["status"] == "observed"
    # A second turn has no reservation left, so it is refused before it is recorded.
    with pytest.raises(UserStrategyBudgetError):
        strategies.send_message(
            created.user_strategy_id,
            call_id="call-2",
            base_revision=1,
            text="A second question",
            approved_budget=dict(CALL_BUDGET),
        )

    reopened = UserStrategyStore(tmp_path / "workspace")
    assert reopened.model_call_usage()["reserved_model_calls"] == 1
    assert reopened.get(created.user_strategy_id) == answered
    assert len(provider.calls) == 1
