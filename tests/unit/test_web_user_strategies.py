"""User-strategy slice: a durable strategy, approved AI calls, typed diffs, frozen experiments.

Everything here is the real product owner unless it is explicitly a double: the
``WebService``, its engine, the ``ResearchOrchestrator`` and the durable stores are
the real ones, and only the model provider is a scripted ``ModelPort`` that never
touches the network. Every assertion is about observed behaviour — a durable
record, a refused action, a job that does or does not exist — never about a name
appearing in a module.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from ea.core import CanonicalDecimal, ReplayWindow
from ea.core.commission import commission_policy_identity
from ea.data import decode_phase1_ohlcv_csv
from ea.strategy.rules import DEFAULT_RULE
from ea.web.app import create_app
from ea.web.llm import (
    ModelCapabilities,
    ModelProviderError,
    ModelResponse,
    ModelRoute,
)
from ea.web.report_claims import host_reading
from ea.web.research import (
    InvalidSpecError,
    ResearchOrchestrator,
    ResearchStore,
    ResearchTaskError,
    TaskStateError,
)
from ea.web.service import (
    ExpectedInput,
    InputChangedError,
    RequestConflictError,
    WebBoundaryError,
    WebService,
)
from ea.web.user_strategies import (
    StrategyDraft,
    UserStrategyBudgetError,
    UserStrategyConflictError,
    UserStrategyError,
    UserStrategyService,
    UserStrategyStaleError,
    UserStrategyStateError,
    UserStrategyStore,
)
from unit.test_web_api import ORIGIN, WRITE_HEADERS, _settings
from unit.test_web_research import (
    BlockingProvider,
    FakeProvider,
    _response,
    _scenario_root,
    _settings_store,
)

pytestmark = pytest.mark.filterwarnings(
    "ignore:The anyio.abc.BlockingPortal alias is deprecated:DeprecationWarning"
)

#: One explicit approval for exactly one model call and no backtest.
CALL_BUDGET = {"max_model_calls": 1, "max_backtests": 0}
#: One explicit approval for exactly one backtest.
RUN_BUDGET = {"max_model_calls": 0, "max_backtests": 1}
#: The one registered executable scenario in the shared fixture root.
SCENARIO = "bounded-long.yaml"
#: The user's own acceptance criterion, in the report's own vocabulary.
CRITERION = {"field": "economics.net_pnl.amount", "expectation": "positive"}
#: The same criterion as the host stores it: the schema is the host's to add.
FROZEN_CRITERION = {"schema": "radian.report-criterion.v1", **CRITERION}
#: The route a scripted provider stands in for: facts, never a credential.
FIXTURE_ROUTE = ModelRoute(
    provider="fixture",
    protocol="fixture-messages",
    model="fixture-model",
    endpoint="https://fixture.invalid/v1/messages",
    capabilities=ModelCapabilities(tool_use=True, structured_output=False, usage=True),
)


class Crash(BaseException):
    """A process death, not a provider answer: it must leave the record as it was."""


class RoutedProvider(FakeProvider):
    """A scripted provider that can also describe the route it stands in for."""

    def route(self) -> ModelRoute:
        return FIXTURE_ROUTE


class RoutedBlockingProvider(BlockingProvider):
    """The blocking double, with the same truthful route."""

    def route(self) -> ModelRoute:
        return FIXTURE_ROUTE


#: Every real service this module started, stopped after the test that started it.
_STARTED: list[WebService] = []


@pytest.fixture(autouse=True)
def _stop_started_services() -> Iterator[None]:
    yield
    while _STARTED:
        _STARTED.pop().stop()


def _stack(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    responses: list[Any] | None = None,
    *,
    scripted: bool = True,
) -> tuple[UserStrategyService, ResearchOrchestrator, WebService, FakeProvider]:
    """One process image: real service, real research orchestrator, scripted provider."""
    service = WebService(_scenario_root(tmp_path), tmp_path / "workspace")
    service.start()
    _STARTED.append(service)
    research = ResearchOrchestrator(
        ResearchStore(tmp_path / "workspace"), service, _settings_store(tmp_path, monkeypatch)
    )
    strategies = UserStrategyService(
        UserStrategyStore(tmp_path / "workspace"),
        research,
        service,
        _settings_store(tmp_path, monkeypatch),
    )
    provider = RoutedProvider(responses or [])
    if scripted:
        cast(Any, strategies)._provider = provider  # test seam, same ModelPort interface
    return strategies, research, service, provider


def _terminal_job(service: WebService, job_id: str) -> None:
    """One bounded wait on the real job index: this service runs one job at a time."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if service.get_job(job_id).status not in {"accepted", "running"}:
            return
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never reached a terminal state")


def _durable(tmp_path: Any, strategy_id: str) -> Any:
    """The document as another process would read it back from the workspace."""
    return UserStrategyStore(tmp_path / "workspace").get(strategy_id)


def _children(tmp_path: Any) -> list[Any]:
    return list(ResearchStore(tmp_path / "workspace").list())


def _forbid_engine_runs(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count real engine runs: a refused submission must reach the engine zero times.

    An admitted run now reaches the engine through one worker process, so the
    boundary that proves "no engine run happened" is the controller's own spawn
    step, patched on the service class itself — the same boundary every service in
    this process uses, including a second workspace opened later in the same test.
    A worker is spawned only to run the engine, so a count of zero is the same
    fact the old module-global patch asserted.
    """
    runs = {"count": 0}
    real = WebService._spawn_worker

    def counted(self: WebService, control: Any) -> Any:
        runs["count"] += 1
        return real(self, control)

    monkeypatch.setattr(WebService, "_spawn_worker", counted)
    return runs


def _create(strategies: UserStrategyService, name: str = "Long AAPL") -> Any:
    return strategies.create(
        name=name,
        intent="Test one registered entry on the fixture data",
        scenario_id=SCENARIO,
        criterion=CRITERION,
    )


def _change(items: list[dict[str, Any]], call_id: str = "call-1", **extra: Any) -> ModelResponse:
    """One scripted typed-diff answer from the model."""
    return _response(
        [{"id": call_id, "name": "propose_change", "input": {"items": items}}], **extra
    )


def _propose(
    strategies: UserStrategyService,
    provider: FakeProvider,
    strategy: Any,
    items: list[dict[str, Any]],
    *,
    call_id: str = "call-1",
    base_revision: int = 1,
) -> tuple[Any, dict[str, Any]]:
    provider.responses.append(_change(items, call_id))
    updated = strategies.send_message(
        strategy.user_strategy_id,
        call_id=call_id,
        base_revision=base_revision,
        text="Propose a change to the draft",
        approved_budget=dict(CALL_BUDGET),
    )
    return updated, updated.proposals[-1]


#: A confirmation with no approval at all is the one request the helper passes through.
_ABSENT = object()


def _launch(
    strategies: UserStrategyService,
    strategy_id: str,
    *,
    launch_intent_id: str = "li-1",
    base_revision: int | None = None,
    approved_budget: Any = _ABSENT,
    preview_identity: str | None = None,
) -> Any:
    """One confirmation of exactly one previewed input.

    Every confirmation carries the identity of the preview it was made from, because
    a draft revision alone does not name what would run. A caller that replays an
    earlier confirmation passes that confirmation's own identity back in.
    """
    preview = strategies.preview_experiment(strategy_id)
    return strategies.launch_experiment(
        strategy_id,
        launch_intent_id=launch_intent_id,
        base_revision=preview["draft_revision"] if base_revision is None else base_revision,
        preview_identity=(
            preview["preview_identity"] if preview_identity is None else preview_identity
        ),
        approved_budget=dict(RUN_BUDGET) if approved_budget is _ABSENT else approved_budget,
    )


TWO_FIELDS: list[dict[str, Any]] = [
    {
        "path": "parameters.target_quantity",
        "value": "3",
        "reason": "A wider sample size.",
        "evidence": ["idea: more size"],
    },
    {
        "path": "parameters.entry_delay_bars",
        "value": 1,
        "reason": "Skip the first bar.",
        "evidence": ["idea: wait one bar"],
    },
]


# --- creation and isolation ---------------------------------------------------


def test_create_saves_one_draft_from_the_registered_scenario_without_dispatch(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    created = _create(strategies)

    assert created.name == "Long AAPL"
    assert created.user_strategy_id
    # A user strategy is never the engine strategy identity.
    assert created.user_strategy_id != created.draft.strategy_id
    document = cast(dict[str, Any], service.validate_scenario(SCENARIO))
    summary = cast(dict[str, Any], document["summary"])
    contracts = {
        item["name"]: item for item in cast(list[dict[str, Any]], document["strategy_parameters"])
    }
    assert created.draft.strategy_id == summary["strategy_id"] == "bounded-long-v1"
    assert created.draft.venue == summary["venue"] and created.draft.symbol == summary["symbol"]
    assert created.draft.scenario_id == SCENARIO
    assert created.draft.initial_cash == summary["initial_cash"] == "10000"
    # The source binding is the workspace's own raw identity, and this local scenario
    # has none: the selection fingerprint stays a distinct field of the data identity
    # instead of being stored as if it were where the bytes came from.
    identity = cast(dict[str, Any], document["input_identity"])
    assert "dataset_id" not in identity and "source_sha256" not in identity
    assert created.draft.dataset_id is None and created.draft.source_sha256 is None
    assert identity["data_sha256"] and identity["record_count"]
    # The draft starts from every registered executable parameter, valued by the scenario.
    assert set(created.draft.parameters) == set(contracts)
    assert created.draft.parameters == {
        name: item["current_value"] if item["current_value"] is not None else item["default"]
        for name, item in contracts.items()
    }
    assert created.draft.parameters["target_quantity"] == "2"
    # The host owns the criterion vocabulary: the user's field and sign are kept
    # and the report's own schema is what is durably stored.
    assert created.draft.criterion == FROZEN_CRITERION
    assert created.draft.revision == 1
    assert created.history == ()
    # Creating a strategy is a save: no model call, no reservation, no job.
    assert created.calls == ()
    assert created.messages == ()
    assert provider.calls == []
    assert service.list_jobs() == []
    assert _children(tmp_path) == []
    assert [item.user_strategy_id for item in strategies.list()] == [created.user_strategy_id]

    # Only a currently registered, valid scenario can be a starting point.
    with pytest.raises(UserStrategyStateError):
        strategies.create(name="Unknown", intent="Not a scenario", scenario_id="missing.yaml")
    # The criterion vocabulary is the report's, so an unregistered field is
    # refused before anything is saved.
    with pytest.raises(UserStrategyStateError):
        strategies.create(
            name="Unknown criterion",
            intent="A criterion the report does not define",
            scenario_id=SCENARIO,
            criterion={"field": "made.up", "expectation": "positive"},
        )
    assert len(strategies.list()) == 1


def test_two_strategies_over_one_scenario_stay_independent(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    first = _create(strategies, "Fast idea")
    second = _create(strategies, "Slow idea")
    assert first.user_strategy_id != second.user_strategy_id
    assert first.draft.scenario_id == second.draft.scenario_id

    provider.responses.append(_response([], text="Only the first strategy hears this."))
    answered = strategies.send_message(
        first.user_strategy_id,
        call_id="call-1",
        base_revision=1,
        text="A question about the first idea",
        approved_budget=dict(CALL_BUDGET),
    )
    assert answered.messages[-1]["text"] == "Only the first strategy hears this."
    untouched = strategies.get(second.user_strategy_id)
    assert untouched.messages == () and untouched.calls == () and untouched.proposals == ()
    assert len(strategies.list()) == 2


def test_the_store_never_shows_an_unpublished_or_mismatched_record(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashed save leaves a temporary file behind, and that is not a strategy."""
    strategies, _research, _service, _provider = _stack(tmp_path, monkeypatch)
    created = _create(strategies)
    directory = tmp_path / "workspace" / "user-strategies"
    published = (directory / f"{created.user_strategy_id}.json").read_bytes()

    (directory / ".tmp-crash.json").write_bytes(published)
    (directory / ".tmp-9f1c.json").write_bytes(b'{"schema": "ea.user-strategy.v1"}')
    # One record whose own identity disagrees with its file name is not shown either.
    (directory / "another-identity.json").write_bytes(published)

    listed = strategies.list()
    assert [item.user_strategy_id for item in listed] == [created.user_strategy_id]
    # Nothing is hidden for good: a leftover is still on disk and is not a readable id.
    assert (directory / ".tmp-crash.json").exists()
    with pytest.raises(UserStrategyStateError):
        strategies.get(".tmp-crash")


# --- approved model calls -----------------------------------------------------


def test_a_call_with_zero_or_excess_budget_never_reaches_the_provider(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)

    with pytest.raises(UserStrategyBudgetError):
        strategies.send_message(
            strategy.user_strategy_id,
            call_id="call-1",
            base_revision=1,
            text="Ask with zero approved calls",
            approved_budget={"max_model_calls": 0, "max_backtests": 0},
        )
    # An approval larger than the workspace's own budget is not an approval either.
    with pytest.raises(UserStrategyBudgetError):
        strategies.send_message(
            strategy.user_strategy_id,
            call_id="call-1",
            base_revision=1,
            text="Ask with an impossible budget",
            approved_budget={"max_model_calls": 99, "max_backtests": 0},
        )
    refused = strategies.get(strategy.user_strategy_id)
    assert refused.calls == () and refused.messages == () and refused.draft.revision == 1
    assert provider.calls == []


def test_the_reservation_is_durable_before_the_provider_is_reached(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategies, _research, _service, _provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    provider = RoutedBlockingProvider([_response([], text="A plain answer.")])
    cast(Any, strategies)._provider = provider

    failures: list[BaseException] = []

    def run() -> None:
        try:
            strategies.send_message(
                strategy_id,
                call_id="call-1",
                base_revision=1,
                text="Explain the draft",
                approved_budget=dict(CALL_BUDGET),
            )
        except BaseException as error:  # noqa: BLE001 — recorded for the assertion below
            failures.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert provider.invoked.wait(10)

    # While the provider is still blocked, the request is already durable on disk.
    durable = _durable(tmp_path, strategy_id)
    assert durable.messages[-1]["role"] == "user"
    assert durable.messages[-1]["call_id"] == "call-1"
    call = durable.calls[-1]
    assert call["call_id"] == "call-1"
    assert call["status"] == "dispatched"
    assert call["base_revision"] == 1
    assert call["approved_budget"] == CALL_BUDGET
    assert set(call["route"]) == {"provider", "model", "endpoint"}
    # The route is described, never resolved: no credential and no key reference.
    assert call["route"]["provider"] == FIXTURE_ROUTE.provider
    assert call["route"]["model"] == FIXTURE_ROUTE.model
    assert call["route"]["endpoint"] == FIXTURE_ROUTE.endpoint

    provider.release.set()
    thread.join(10)
    assert failures == []
    assert _durable(tmp_path, strategy_id).calls[-1]["status"] == "observed"


def test_a_repeated_call_id_replays_its_outcome_and_a_changed_payload_conflicts(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    provider.responses.append(_response([], text="One answer."))
    first = strategies.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="Same question",
        approved_budget=dict(CALL_BUDGET),
    )
    assert len(provider.calls) == 1

    replayed = strategies.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="Same question",
        approved_budget=dict(CALL_BUDGET),
    )
    assert len(provider.calls) == 1
    assert replayed.calls == first.calls
    assert replayed.messages == first.messages
    # The call's public revision is the request's own, on the record and on the
    # message that request produced, and a fresh read of the store returns both
    # exactly as they were written.
    assert replayed.calls[-1]["base_revision"] == 1
    assert replayed.calls[-1]["status"] == "observed"
    assert replayed.messages[-1]["base_revision"] == 1
    durable = _durable(tmp_path, strategy_id)
    assert durable.calls == first.calls
    assert durable.messages == first.messages

    # A second service over the same workspace replays the same stored record
    # instead of reaching the provider again.
    reopened = UserStrategyService(
        UserStrategyStore(tmp_path / "workspace"),
        _research,
        _service,
        _settings_store(tmp_path, monkeypatch),
    )
    cast(Any, reopened)._provider = provider
    provider.responses.append(_response([], text="An answer that must not be used."))
    replayed_again = reopened.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="Same question",
        approved_budget=dict(CALL_BUDGET),
    )
    assert len(provider.calls) == 1
    assert replayed_again.calls == first.calls
    assert replayed_again.messages == first.messages
    assert [item["base_revision"] for item in replayed_again.calls] == [1]
    assert "An answer that must not be used." not in [
        item["text"] for item in replayed_again.messages
    ]

    with pytest.raises(UserStrategyConflictError):
        strategies.send_message(
            strategy_id,
            call_id="call-1",
            base_revision=1,
            text="A different question under the same id",
            approved_budget=dict(CALL_BUDGET),
        )
    # The approval is part of that request too: another one under the same id conflicts.
    with pytest.raises(UserStrategyConflictError):
        strategies.send_message(
            strategy_id,
            call_id="call-1",
            base_revision=1,
            text="Same question",
            approved_budget={"max_model_calls": 2, "max_backtests": 1},
        )
    with pytest.raises(UserStrategyStaleError):
        strategies.send_message(
            strategy_id,
            call_id="call-2",
            base_revision=7,
            text="A question about a revision that is not the head",
            approved_budget=dict(CALL_BUDGET),
        )
    assert len(provider.calls) == 1


def test_missing_configuration_is_recorded_honestly_and_charges_nothing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategies, _research, _service, _provider = _stack(tmp_path, monkeypatch, scripted=False)
    strategy = _create(strategies)
    assert not (tmp_path / "settings.json").exists()

    answered = strategies.send_message(
        strategy.user_strategy_id,
        call_id="call-1",
        base_revision=1,
        text="Explain the draft with no provider configured",
        approved_budget=dict(CALL_BUDGET),
    )
    call = answered.calls[-1]
    assert call["status"] == "unconfigured"
    assert call["message"]
    # Nothing was measured, so nothing is reported as a measurement: the tokens
    # are absent, not zero, and the cost is unavailable without a price source.
    assert call["input_tokens"] is None and call["output_tokens"] is None
    assert call["usage_available"] is False
    assert call["cost"]["available"] is False and call["cost"]["amount"] is None
    assert answered.draft.revision == 1
    assert all(message["kind"] != "proposal" for message in answered.messages)


def test_an_unknown_provider_outcome_is_retained_and_never_resent(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategies, _research, service, provider = _stack(
        tmp_path, monkeypatch, [ModelProviderError("model provider is unreachable: ConnectError")]
    )
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    answered = strategies.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="A call whose outcome nobody observed",
        approved_budget=dict(CALL_BUDGET),
    )
    call = answered.calls[-1]
    assert call["status"] == "unknown"
    assert call["message"]
    # An unobserved outcome is not a measured one.
    assert call["input_tokens"] is None and call["output_tokens"] is None
    assert call["usage_available"] is False
    assert len(provider.calls) == 1

    service.stop()
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    reconciled = strategies.reconcile(strategy_id)
    assert reconciled.calls[-1]["status"] == "unknown"
    assert provider.calls == []

    # The same id replays the recorded outcome instead of re-dispatching it.
    replayed = strategies.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="A call whose outcome nobody observed",
        approved_budget=dict(CALL_BUDGET),
    )
    assert replayed.calls[-1]["status"] == "unknown"
    assert provider.calls == []

    # Only an explicit new call id and budget is another attempt.
    provider.responses.append(_response([], text="A second, explicit attempt."))
    retried = strategies.send_message(
        strategy_id,
        call_id="call-2",
        base_revision=1,
        text="A call whose outcome nobody observed",
        approved_budget=dict(CALL_BUDGET),
    )
    assert retried.calls[-1]["status"] == "observed"
    assert len(provider.calls) == 1


def test_a_call_interrupted_mid_dispatch_reconciles_unknown_without_resend_or_refund(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch, [Crash()])
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id

    with pytest.raises(Crash):
        strategies.send_message(
            strategy_id,
            call_id="call-1",
            base_revision=1,
            text="The process dies while the model is being asked",
            approved_budget=dict(CALL_BUDGET),
        )
    # The charge was reserved and handed over; nothing was ever observed.
    assert _durable(tmp_path, strategy_id).calls[-1]["status"] == "dispatched"
    assert len(provider.calls) == 1

    service.stop()
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    reconciled = strategies.reconcile(strategy_id)
    assert reconciled.calls[-1]["status"] == "unknown"
    # The reservation was never observed, so no usage is invented for it.
    assert reconciled.calls[-1]["input_tokens"] is None
    assert reconciled.calls[-1]["usage_available"] is False
    assert provider.calls == []
    # A second restart neither re-sends nor invents a second record.
    reconciled = strategies.reconcile(strategy_id)
    assert len(reconciled.calls) == 1
    assert reconciled.calls[-1]["status"] == "unknown"


def test_a_call_interrupted_before_dispatch_is_not_unknown_and_was_never_sent(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process died after the reservation was durable and before any dispatch."""
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id

    def crash(*args: Any, **kwargs: Any) -> Any:
        raise Crash()

    monkeypatch.setattr(strategies, "_mark_dispatched", crash)
    with pytest.raises(Crash):
        strategies.send_message(
            strategy_id,
            call_id="call-1",
            base_revision=1,
            text="The process dies before the provider is reached",
            approved_budget=dict(CALL_BUDGET),
        )
    # The reservation is durable and still pending, so nothing was handed over.
    assert _durable(tmp_path, strategy_id).calls[-1]["status"] == "pending"
    assert provider.calls == []

    service.stop()
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    reconciled = strategies.reconcile(strategy_id)
    assert reconciled.calls[-1]["status"] == "interrupted_before_dispatch"
    assert reconciled.calls[-1]["message"]
    assert reconciled.calls[-1]["input_tokens"] is None
    assert provider.calls == []
    # A second restart neither re-sends it nor rewrites the classification.
    again = strategies.reconcile(strategy_id)
    assert again.calls[-1]["status"] == "interrupted_before_dispatch"
    assert provider.calls == []


def test_a_response_this_workspace_cannot_process_is_refused_with_the_usage_it_observed(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider answered, so the outcome was observed: it is refused, not unknown."""
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    provider.responses.append(_response([], text="x" * 9000))

    answered = strategies.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="Ask a question whose answer cannot be stored as it arrived",
        approved_budget=dict(CALL_BUDGET),
    )
    call = answered.calls[-1]
    assert call["status"] == "refused"
    assert call["message"]
    # This answer was observed, so its usage is a measurement rather than a guess.
    assert call["model"] == "fake-model"
    assert call["input_tokens"] == 11 and call["output_tokens"] == 7
    assert call["usage_available"] is True
    assert answered.draft.revision == 1
    # Nothing is left unresolved, so recovery has nothing to classify here.
    assert strategies.reconcile(strategy_id).calls[-1]["status"] == "refused"


def test_a_conversation_sends_the_newest_question_once_with_this_strategys_own_history(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One request to the model: this strategy's stated intent and its own turns."""
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id

    provider.responses.append(_response([], text="First answer."))
    strategies.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="First question",
        approved_budget=dict(CALL_BUDGET),
    )
    provider.responses.append(_response([], text="Second answer."))
    strategies.send_message(
        strategy_id,
        call_id="call-2",
        base_revision=1,
        text="Second question",
        approved_budget=dict(CALL_BUDGET),
    )

    messages = cast(list[Any], provider.calls[-1]["messages"])
    texts = [message.text for message in messages]
    # The question being asked is sent exactly once, and it is the last turn.
    assert texts[-1] == "Second question"
    assert texts.count("Second question") == 1
    assert texts.count("First question") == 1
    assert [message.role for message in messages] == ["user", "user", "assistant", "user"]
    # The opening turn carries this strategy's own intent, draft and criterion.
    opening = cast(str, texts[0])
    assert "Test one registered entry on the fixture data" in opening
    assert "bounded-long-v1" in opening
    assert "Draft revision 1" in opening
    assert "economics.net_pnl.amount is positive" in opening
    # Nothing is claimed about a run: this strategy has confirmed none.
    assert "Host-verified readings" not in opening
    assert "verified report is not available" not in opening


def test_a_conversation_states_only_this_strategys_own_host_verified_run_facts(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run of another strategy is not a fact about this one, and no reading is invented."""
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    mine = _create(strategies, "Fast idea")
    other = _create(strategies, "Slow idea")
    launched = _launch(strategies, mine.user_strategy_id)
    job_id = cast(str, launched.experiments[-1]["job_id"])
    _terminal_job(service, job_id)
    try:
        # Whether this workspace can verify the run at all is an observed fact here.
        verified = bool(service.report(job_id))
    except Exception:  # noqa: BLE001 — an unverifiable run has no reading to state
        verified = False

    provider.responses.append(_response([], text="An answer."))
    answered = strategies.send_message(
        mine.user_strategy_id,
        call_id="call-1",
        base_revision=1,
        text="What did the run find?",
        approved_budget=dict(CALL_BUDGET),
    )
    assert answered.messages[-1]["text"] == "An answer."
    opening = cast(str, cast(list[Any], provider.calls[-1]["messages"])[0].text)
    assert job_id in opening
    assert other.name not in opening and other.user_strategy_id not in opening
    readings = {
        line.split(" = ")[0].strip()
        for line in opening.split("\n")
        if " = " in line and line.strip().startswith("economics.")
    }
    if verified:
        assert "Host-verified readings" in opening
        assert readings
    else:
        assert "verified report is not available here" in opening
        assert readings == set()
    # Only the report's own allowlisted fields are ever stated as host facts.
    assert readings <= {
        "economics.net_pnl.amount",
        "economics.fees.amount",
        "economics.total_return.value",
        "economics.counts.fills",
        "economics.counts.orders",
    }


# --- typed diffs --------------------------------------------------------------


def test_an_answer_may_be_plain_text_and_a_diff_must_name_typed_executable_paths(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id

    provider.responses.append(_response([], text="Your draft uses one entry of two units."))
    explained = strategies.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="What does my draft do?",
        approved_budget=dict(CALL_BUDGET),
    )
    assert explained.proposals == ()
    assert explained.draft.revision == 1
    assert explained.messages[-1]["kind"] == "message"
    assert explained.messages[-1]["text"] == "Your draft uses one entry of two units."

    # A diff that also names an identity field is refused as a whole.
    provider.responses.append(
        _change(
            [
                {
                    "path": "parameters.target_quantity",
                    "value": "3",
                    "reason": "More size.",
                    "evidence": [],
                },
                {"path": "strategy_id", "value": "moving-average-entry-v1", "reason": "Swap."},
            ],
            call_id="call-2",
        )
    )
    refused = strategies.send_message(
        strategy_id,
        call_id="call-2",
        base_revision=1,
        text="Change the strategy and the size",
        approved_budget=dict(CALL_BUDGET),
    )
    assert refused.proposals == ()
    assert refused.draft.revision == 1
    assert refused.calls[-1]["status"] == "refused"
    assert refused.calls[-1]["message"]
    assert refused.messages[-1]["text"]

    # An unknown parameter path is refused the same way.
    provider.responses.append(
        _change(
            [{"path": "parameters.made_up", "value": 1, "reason": "Unknown."}],
            call_id="call-3",
        )
    )
    unknown = strategies.send_message(
        strategy_id,
        call_id="call-3",
        base_revision=1,
        text="Change something that does not exist",
        approved_budget=dict(CALL_BUDGET),
    )
    assert unknown.proposals == ()
    assert unknown.calls[-1]["status"] == "refused"

    # A registered path carrying a value of the wrong type is not a typed diff either.
    provider.responses.append(
        _change(
            [{"path": "parameters.entry_delay_bars", "value": "1", "reason": "A string."}],
            call_id="call-4",
        )
    )
    mistyped = strategies.send_message(
        strategy_id,
        call_id="call-4",
        base_revision=1,
        text="Delay the entry by one bar",
        approved_budget=dict(CALL_BUDGET),
    )
    assert mistyped.proposals == ()
    assert isinstance(mistyped.draft, StrategyDraft)
    assert mistyped.draft.parameters["entry_delay_bars"] == 0
    assert mistyped.calls[-1]["status"] == "refused"
    assert len(provider.calls) == 4


def test_applying_a_selected_subset_validates_the_whole_draft_and_stales_the_rest(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    with_proposal, proposal = _propose(strategies, provider, strategy, TWO_FIELDS)

    assert proposal["status"] == "open"
    assert proposal["base_revision"] == 1
    assert [item["path"] for item in proposal["items"]] == [
        "parameters.target_quantity",
        "parameters.entry_delay_bars",
    ]
    assert proposal["items"][0]["before"] == "2" and proposal["items"][0]["after"] == "3"
    assert proposal["items"][0]["reason"] == "A wider sample size."
    assert proposal["items"][0]["evidence"] == ["idea: more size"]
    assert proposal["items"][0]["proposer"]
    assert with_proposal.draft.revision == 1

    applied = strategies.apply_proposal(
        strategy_id,
        proposal_id=proposal["proposal_id"],
        operation_id="op-1",
        paths=["parameters.target_quantity"],
    )
    assert applied.draft.revision == 2
    assert isinstance(applied.draft, StrategyDraft)
    assert applied.draft.parameters["target_quantity"] == "3"
    assert applied.draft.parameters["entry_delay_bars"] == 0
    assert [item.revision for item in applied.history] == [1]
    change = applied.changes[-1]
    assert change["operation_id"] == "op-1" and change["kind"] == "apply"
    assert change["proposal_id"] == proposal["proposal_id"]
    assert change["from_revision"] == 1 and change["to_revision"] == 2
    assert change["items"] == [
        {
            "path": "parameters.target_quantity",
            "before": "2",
            "after": "3",
            "reason": "A wider sample size.",
            # The provenance of each field travels with the change that wrote it.
            "evidence": ["idea: more size"],
            "proposer": "model",
        }
    ]
    assert change["proposer"] == "model"
    assert change["evidence"] == ["idea: more size"]
    settled = applied.proposals[-1]
    assert settled["status"] == "applied"
    assert settled["applied_paths"] == ["parameters.target_quantity"]
    assert settled["stale_paths"] == ["parameters.entry_delay_bars"]

    # The leftover belongs to the old base and never applies to the new head.
    with pytest.raises(UserStrategyStaleError):
        strategies.apply_proposal(
            strategy_id,
            proposal_id=proposal["proposal_id"],
            operation_id="op-2",
            paths=["parameters.entry_delay_bars"],
        )
    # The same operation is idempotent; the same id with another selection conflicts.
    replayed = strategies.apply_proposal(
        strategy_id,
        proposal_id=proposal["proposal_id"],
        operation_id="op-1",
        paths=["parameters.target_quantity"],
    )
    assert replayed.draft.revision == 2 and len(replayed.changes) == 1
    with pytest.raises(UserStrategyConflictError):
        strategies.apply_proposal(
            strategy_id,
            proposal_id=proposal["proposal_id"],
            operation_id="op-1",
            paths=["parameters.entry_delay_bars"],
        )
    assert strategies.get(strategy_id).draft.revision == 2


def test_a_repeated_apply_resolves_its_selection_against_the_proposal_it_came_from(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The selection is compared as a set, and the original proposal is its authority."""
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    _strategy, proposal = _propose(strategies, provider, strategy, TWO_FIELDS)

    both = ["parameters.target_quantity", "parameters.entry_delay_bars"]
    applied = strategies.apply_proposal(
        strategy_id,
        proposal_id=proposal["proposal_id"],
        operation_id="op-1",
        paths=list(reversed(both)),
    )
    assert applied.draft.revision == 2
    # The applied selection is the set that was asked for; the recorded order is
    # the proposal's own and is not part of what the caller requested.
    assert set(applied.changes[-1]["applied_paths"]) == set(both)
    assert applied.proposals[-1]["status"] == "applied"

    # The same selection stated in another order is the same request, not a second one.
    replayed = strategies.apply_proposal(
        strategy_id,
        proposal_id=proposal["proposal_id"],
        operation_id="op-1",
        paths=both,
    )
    assert replayed.draft.revision == 2 and len(replayed.changes) == 1
    # A different selection under that id is a conflict, resolved against the proposal
    # the operation came from rather than against its status today.
    with pytest.raises(UserStrategyConflictError):
        strategies.apply_proposal(
            strategy_id,
            proposal_id=proposal["proposal_id"],
            operation_id="op-1",
            paths=["parameters.entry_delay_bars"],
        )
    assert strategies.get(strategy_id).draft.revision == 2
    assert len(strategies.get(strategy_id).changes) == 1


def test_a_whole_spec_rejection_writes_no_revision_even_for_the_valid_part_of_the_subset(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``target_quantity`` is bounded by the real instrument's quantity quantum.

    ``"2.5"`` is inside the field's own registered bounds, so the model's diff is a
    valid typed proposal; it is the complete resulting draft that the engine refuses.
    The other selected field is valid on its own and must not be written either: the
    selected set fails as one unit and the proposal stays open and untouched.
    """
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    document = cast(dict[str, Any], service.validate_scenario(SCENARIO))
    contracts = {
        item["name"]: item for item in cast(list[dict[str, Any]], document["strategy_parameters"])
    }
    quantum = contracts["target_quantity"]["minimum"]
    # The item bound this proposal passes, and the real quantum it violates.
    assert Decimal(cast(str, quantum)) <= Decimal("2.5")
    assert Decimal("2.5") % Decimal(cast(str, quantum)) != 0
    _strategy, proposal = _propose(
        strategies,
        provider,
        strategy,
        [
            {
                "path": "parameters.target_quantity",
                "value": "2.5",
                "reason": "Half a unit more.",
                "evidence": [],
            },
            {
                "path": "parameters.entry_delay_bars",
                "value": 1,
                "reason": "Skip the first bar.",
                "evidence": [],
            },
        ],
    )
    # Both items are typed and inside their registered bounds, so the diff is stored.
    assert proposal["status"] == "open"
    assert [item["path"] for item in proposal["items"]] == [
        "parameters.target_quantity",
        "parameters.entry_delay_bars",
    ]

    with pytest.raises(UserStrategyStateError):
        strategies.apply_proposal(
            strategy_id,
            proposal_id=proposal["proposal_id"],
            operation_id="op-1",
            paths=None,
        )
    refused = strategies.get(strategy_id)
    assert refused.draft.revision == 1
    assert isinstance(refused.draft, StrategyDraft)
    assert refused.draft.parameters["target_quantity"] == "2"
    assert refused.draft.parameters["entry_delay_bars"] == 0
    assert refused.changes == ()
    assert refused.history == ()
    open_proposal = refused.proposals[-1]
    assert open_proposal["status"] == "open"
    assert open_proposal["applied_paths"] == []


def test_reject_revise_and_undo_keep_the_draft_and_history_honest(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id

    _strategy, first = _propose(strategies, provider, strategy, TWO_FIELDS, call_id="call-1")
    rejected = strategies.reject_proposal(strategy_id, proposal_id=first["proposal_id"])
    assert rejected.proposals[-1]["status"] == "rejected"
    assert rejected.draft.revision == 1 and rejected.changes == () and rejected.history == ()

    # Revise asks the model again on the current base and keeps every proposal.
    _strategy, second = _propose(strategies, provider, strategy, TWO_FIELDS, call_id="call-2")
    provider.responses.append(
        _change(
            [
                {
                    "path": "parameters.target_quantity",
                    "value": "4",
                    "reason": "An even wider sample size.",
                    "evidence": [],
                }
            ],
            call_id="call-3",
        )
    )
    revised = strategies.revise_proposal(
        strategy_id,
        proposal_id=second["proposal_id"],
        call_id="call-3",
        text="Try a wider size instead",
        approved_budget=dict(CALL_BUDGET),
    )
    assert [item["proposal_id"] for item in revised.proposals] == [
        first["proposal_id"],
        second["proposal_id"],
        revised.proposals[-1]["proposal_id"],
    ]
    assert revised.proposals[0]["status"] == "rejected"
    assert revised.proposals[1]["status"] == "superseded"
    third = revised.proposals[-1]
    assert third["status"] == "open"
    assert third["base_revision"] == 1 and third["call_id"] == "call-3"
    assert revised.draft.revision == 1

    applied = strategies.apply_proposal(
        strategy_id, proposal_id=third["proposal_id"], operation_id="op-apply"
    )
    assert applied.draft.revision == 2
    assert isinstance(applied.draft, StrategyDraft)
    assert applied.draft.parameters["target_quantity"] == "4"

    undone = strategies.undo(
        strategy_id, operation_id="op-undo", head_revision=2, target_operation_id="op-apply"
    )
    assert undone.draft.revision == 3
    assert isinstance(undone.draft, StrategyDraft)
    assert undone.draft.parameters["target_quantity"] == "2"
    assert [item.revision for item in undone.history] == [1, 2]
    undo_change = undone.changes[-1]
    assert undo_change["kind"] == "undo"
    assert undo_change["proposal_id"] == third["proposal_id"]
    assert undo_change["target_operation_id"] == "op-apply"
    assert undo_change["from_revision"] == 2 and undo_change["to_revision"] == 3
    assert undo_change["applied_paths"] == ["parameters.target_quantity"]
    assert undo_change["stale_paths"] == []
    # A user-driven reversal says so, and records the values it actually wrote back
    # with the provenance of each field it reversed.
    assert undo_change["proposer"] == "user"
    assert undo_change["items"] == [
        {
            "path": "parameters.target_quantity",
            "before": "4",
            "after": "2",
            "reason": "An even wider sample size.",
            "evidence": [],
            "proposer": "model",
        }
    ]
    # The same undo request is idempotent: it is compared with the full request.
    replayed_undo = strategies.undo(
        strategy_id, operation_id="op-undo", head_revision=2, target_operation_id="op-apply"
    )
    assert replayed_undo.draft.revision == 3 and len(replayed_undo.changes) == 2
    # The same undo id with another revision or target is a conflict, not a second undo.
    with pytest.raises(UserStrategyConflictError):
        strategies.undo(
            strategy_id, operation_id="op-undo", head_revision=3, target_operation_id="op-apply"
        )
    # An older application is not the head of the revision the caller is looking at.
    with pytest.raises(UserStrategyStaleError):
        strategies.undo(
            strategy_id,
            operation_id="op-undo-2",
            head_revision=3,
            target_operation_id="op-apply",
        )
    assert strategies.get(strategy_id).draft.revision == 3
    assert len(strategies.get(strategy_id).changes) == 2


@pytest.mark.parametrize(
    "case",
    [
        "provider_error",
        "provider_timeout",
        "text_only",
        "invalid_replacement",
        "empty_replacement",
    ],
)
def test_a_revise_without_a_replacement_never_supersedes_the_candidate(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """The candidate is superseded only by the replacement that actually arrives.

    A refinement reserves its call and reaches the provider, and the provider then
    fails, times out, answers in plain text, or answers with a change this workspace
    refuses whole. None of those is a replacement, so the original proposal has to be
    exactly as it was: open, on its own revision, and applicable. That is only true
    while the supersede is written in the same durable record as a valid new
    proposal, which is why none of these outcomes may consume the candidate first.
    """
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    _strategy, first = _propose(strategies, provider, strategy, TWO_FIELDS, call_id="call-1")

    if case == "provider_error":
        provider.responses.append(ModelProviderError("the provider refused this request"))
    elif case == "provider_timeout":
        provider.responses.append(TimeoutError("the provider did not answer in time"))
    elif case == "text_only":
        provider.responses.append(_response([], text="I would rather explain this instead."))
    elif case == "invalid_replacement":
        provider.responses.append(
            _change(
                [{"path": "not.a.registered.path", "value": "1", "reason": "no"}], call_id="call-2"
            )
        )
    else:
        provider.responses.append(_change([], call_id="call-2"))

    revised = strategies.revise_proposal(
        strategy_id,
        proposal_id=first["proposal_id"],
        call_id="call-2",
        text="Refine the proposed change",
        approved_budget=dict(CALL_BUDGET),
    )

    assert revised.draft.revision == 1
    assert [item["proposal_id"] for item in revised.proposals] == [first["proposal_id"]]
    held = _durable(tmp_path, strategy_id)
    assert held.proposals[-1]["status"] == "open"
    assert held.proposals[-1]["base_revision"] == 1
    # The candidate is still a candidate: it applies to the draft it was made on.
    applied = strategies.apply_proposal(
        strategy_id, proposal_id=first["proposal_id"], operation_id="op-apply"
    )
    assert applied.draft.revision == 2
    assert isinstance(applied.draft, StrategyDraft)
    assert applied.draft.parameters["target_quantity"] == "3"


def test_an_ordinary_turn_supersedes_nothing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a refinement replaces the candidate it named; an ordinary turn never does.

    A plain explanation and a brand-new proposal are both ordinary messages, and
    neither carries a revise action. The candidate already open therefore stays open
    and applicable even though the conversation moved on.
    """
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    _strategy, first = _propose(strategies, provider, strategy, TWO_FIELDS, call_id="call-1")

    provider.responses.append(_response([], text="This draft holds a fixed target size."))
    strategies.send_message(
        strategy_id,
        call_id="call-2",
        base_revision=1,
        text="What does this draft do?",
        approved_budget=dict(CALL_BUDGET),
    )
    _strategy, second = _propose(strategies, provider, strategy, TWO_FIELDS, call_id="call-3")

    held = strategies.get(strategy_id)
    assert [item["proposal_id"] for item in held.proposals] == [
        first["proposal_id"],
        second["proposal_id"],
    ]
    assert [item["status"] for item in held.proposals] == ["open", "open"]
    applied = strategies.apply_proposal(
        strategy_id, proposal_id=first["proposal_id"], operation_id="op-apply"
    )
    assert applied.draft.revision == 2


def test_a_valid_refinement_supersedes_only_the_proposal_it_named(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One refinement replaces exactly its own candidate, in its own durable write.

    With two candidates open, refining one leaves the other open and applicable: the
    supersede names the proposal the originating call's own request named, and the
    replacement is written together with it.
    """
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    _strategy, first = _propose(strategies, provider, strategy, TWO_FIELDS, call_id="call-1")
    _strategy, second = _propose(strategies, provider, strategy, TWO_FIELDS, call_id="call-2")

    provider.responses.append(_change(TWO_FIELDS, call_id="call-3"))
    revised = strategies.revise_proposal(
        strategy_id,
        proposal_id=second["proposal_id"],
        call_id="call-3",
        text="Refine the proposed change",
        approved_budget=dict(CALL_BUDGET),
    )

    statuses = {item["proposal_id"]: item["status"] for item in revised.proposals}
    assert statuses[first["proposal_id"]] == "open"
    assert statuses[second["proposal_id"]] == "superseded"
    replacement = revised.proposals[-1]
    assert replacement["status"] == "open"
    assert replacement["base_revision"] == 1
    # The replacement is the one that applies, and the untouched candidate still can.
    applied = strategies.apply_proposal(
        strategy_id, proposal_id=replacement["proposal_id"], operation_id="op-apply"
    )
    assert applied.draft.revision == 2


def test_a_refinement_whose_settlement_save_fails_keeps_the_candidate_open(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supersede is durable only with the replacement it belongs to.

    The store publishes by writing a temporary file and replacing the record, so a
    save that fails before the replacement leaves the previous record exactly as it
    was. When that failing save is the one that would have settled the refinement,
    the original candidate is still open on disk and the call is still dispatched,
    which is precisely what recovery classifies: it is never silently re-sent.
    """
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    _strategy, first = _propose(strategies, provider, strategy, TWO_FIELDS, call_id="call-1")
    provider.responses.append(_change(TWO_FIELDS, call_id="call-2"))

    real_save = UserStrategyStore.save

    def failing_save(self: UserStrategyStore, item: Any) -> None:
        settled = any(
            entry.get("call_id") == "call-2"
            and entry.get("status") not in {"pending", "dispatched"}
            for entry in item.calls
        )
        if settled:
            raise OSError("the durable write did not complete")
        real_save(self, item)

    monkeypatch.setattr(UserStrategyStore, "save", failing_save)
    with pytest.raises(OSError):
        strategies.revise_proposal(
            strategy_id,
            proposal_id=first["proposal_id"],
            call_id="call-2",
            text="Refine the proposed change",
            approved_budget=dict(CALL_BUDGET),
        )

    held = _durable(tmp_path, strategy_id)
    assert held.proposals[-1]["status"] == "open"
    assert held.calls[-1]["status"] == "dispatched"
    assert held.draft.revision == 1
    # The interrupted refinement is never re-sent on its own: the next turn is refused
    # until recovery has classified the call that stayed dispatched.
    with pytest.raises(UserStrategyStateError):
        strategies.send_message(
            strategy_id,
            call_id="call-3",
            base_revision=1,
            text="Are you there?",
            approved_budget=dict(CALL_BUDGET),
        )
    assert len(provider.calls) == 2
    monkeypatch.undo()


@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize("operation", ["apply", "reject", "undo"])
def test_a_refinement_serializes_queued_operations_without_overwriting_their_changes(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, replacement: bool, operation: str
) -> None:
    """A late success or failure cannot write over a queued user decision."""
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    strategy, first = _propose(strategies, provider, strategy, TWO_FIELDS, call_id="call-1")
    if operation == "undo":
        strategy = strategies.apply_proposal(
            strategy_id, proposal_id=first["proposal_id"], operation_id="prior-apply"
        )
        strategy, first = _propose(
            strategies, provider, strategy, TWO_FIELDS, call_id="second-proposal", base_revision=2
        )
    revision = strategy.draft.revision
    reply = _change(TWO_FIELDS, call_id="call-2") if replacement else TimeoutError("fixture")
    blocked = RoutedBlockingProvider([reply])
    cast(Any, strategies)._provider = blocked
    failures: list[BaseException] = []
    outcomes: list[BaseException] = []
    completed: list[Any] = []
    started = threading.Event()

    def refine() -> None:
        try:
            strategies.revise_proposal(
                strategy_id,
                proposal_id=first["proposal_id"],
                call_id="call-2",
                text="Refine the proposed change",
                approved_budget=dict(CALL_BUDGET),
            )
        except BaseException as error:  # noqa: BLE001 — asserted in the parent thread
            failures.append(error)

    def decide() -> None:
        started.set()
        try:
            if operation == "apply":
                result = strategies.apply_proposal(
                    strategy_id, proposal_id=first["proposal_id"], operation_id="op-apply"
                )
            elif operation == "reject":
                result = strategies.reject_proposal(strategy_id, proposal_id=first["proposal_id"])
            else:
                result = strategies.undo(
                    strategy_id,
                    operation_id="op-undo",
                    head_revision=revision,
                    target_operation_id="prior-apply",
                )
            completed.append(result)
        except BaseException as error:  # noqa: BLE001 — asserted in the parent thread
            outcomes.append(error)

    refiner = threading.Thread(target=refine)
    decider = threading.Thread(target=decide)
    refiner.start()
    try:
        assert blocked.invoked.wait(5)
        decider.start()
        assert started.wait(5)
        decider.join(0.1)
        assert decider.is_alive() and completed == [] and outcomes == []
        # During dispatch the original is still available, never prematurely retired.
        assert strategies.get(strategy_id).proposals[-1]["status"] == "open"
    finally:
        blocked.release.set()
        refiner.join(5)
        if decider.ident is not None:
            decider.join(5)
    assert not refiner.is_alive() and not decider.is_alive()
    assert failures == [] and len(blocked.calls) == 1
    held = _durable(tmp_path, strategy_id)
    statuses = {item["proposal_id"]: item["status"] for item in held.proposals}
    if operation == "apply" and replacement:
        assert completed == []
        assert len(outcomes) == 1 and isinstance(outcomes[0], UserStrategyStaleError)
        assert held.draft.revision == revision
        assert statuses[first["proposal_id"]] == "superseded"
    else:
        assert outcomes == [] and len(completed) == 1
        assert held == completed[0]
        if operation == "apply":
            assert held.draft.revision == revision + 1
            assert statuses[first["proposal_id"]] == "applied"
        elif operation == "reject":
            assert held.draft.revision == revision
            assert statuses[first["proposal_id"]] == "rejected"
        else:
            assert held.draft.revision == revision + 1
            assert held.changes[-1]["kind"] == "undo"
            candidate = held.proposals[-1]
            assert candidate["base_revision"] == revision
            with pytest.raises(UserStrategyStaleError):
                strategies.apply_proposal(
                    strategy_id, proposal_id=candidate["proposal_id"], operation_id="late-apply"
                )


# --- the registered contract the model is given --------------------------------


def _contracts(service: WebService) -> dict[str, dict[str, Any]]:
    """The fixture scenario's own registered parameter contracts, as it states them."""
    document = cast(dict[str, Any], service.validate_scenario(SCENARIO))
    return {
        str(item["name"]): item
        for item in cast(list[dict[str, Any]], document["strategy_parameters"])
    }


def _branches(call: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The proposal-tool branches the provider actually received, keyed by path."""
    tools = cast(list[Any], call["tools"])
    assert [tool.name for tool in tools] == [
        "discover_resources",
        "inspect_research_input",
        "read_experiments",
        "describe_data",
        "experiment_status",
        "propose_change",
    ]
    proposal = next(tool for tool in tools if tool.name == "propose_change")
    schema = cast(dict[str, Any], proposal.parameters)
    properties = cast(dict[str, Any], schema["properties"])
    array = cast(dict[str, Any], properties["items"])
    # The tool's one property is the array of proposed fields, and the alternatives
    # are the schema of each item in it.
    assert array["type"] == "array"
    item = cast(dict[str, Any], array["items"])
    by_path: dict[str, dict[str, Any]] = {}
    for branch in cast(list[dict[str, Any]], item["oneOf"]):
        declared = cast(dict[str, Any], cast(dict[str, Any], branch["properties"])["path"])
        paths = declared.get("enum") or [declared.get("const")]
        assert len(paths) == 1 and paths[0] not in by_path
        by_path[str(paths[0])] = branch
    return by_path


def _assert_bounds(sentence: str, contract: dict[str, Any]) -> None:
    """The registered bounds are stated in the value's own JSON type, or as absent."""
    minimum = contract["minimum"]
    maximum = contract["maximum"]
    assert (
        (f"registered minimum {json.dumps(minimum)}" in sentence)
        if minimum is not None
        else ("no registered minimum" in sentence)
    )
    assert (
        (f"registered maximum {json.dumps(maximum)}" in sentence)
        if maximum is not None
        else ("no registered maximum" in sentence)
    )


def test_the_proposal_tool_is_typed_per_registered_path_with_the_actual_bounds(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the provider receives is the host's own contract, not a lossy summary.

    Each changeable path is one branch that names its JSON type: a decimal is a
    string, an integer is an integer, and only what this workspace can really edit
    is offered at all.
    """
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)

    provider.responses.append(_response([], text="An answer."))
    strategies.send_message(
        strategy.user_strategy_id,
        call_id="call-1",
        base_revision=1,
        text="What does my draft do?",
        approved_budget=dict(CALL_BUDGET),
    )
    call = provider.calls[-1]
    branches = _branches(call)
    contracts = _contracts(service)

    # The scenario the user picked registers only kinds this workspace can edit, so
    # every registered parameter is a changeable path and nothing else is.
    editable = {
        name: item for name, item in contracts.items() if item["type"] in {"integer", "decimal"}
    }
    assert editable == contracts
    assert set(branches) == {"initial_cash"} | {f"parameters.{name}" for name in editable}

    for name, contract in contracts.items():
        branch = branches[f"parameters.{name}"]
        properties = cast(dict[str, Any], branch["properties"])
        value_schema = cast(dict[str, Any], properties["value"])
        sentence = cast(str, branch["description"])
        current = strategy.draft.parameters[name]
        assert set(cast(list[str], branch["required"])) == {"path", "value", "reason"}
        if contract["type"] == "integer":
            assert value_schema["type"] == "integer"
            assert type(current) is int
            assert f"current value {json.dumps(current)}" in sentence
            # An integer is a JSON integer here: not a string and not a boolean.
            assert f'current value "{current}"' not in sentence
        else:
            assert value_schema["type"] == "string"
            assert isinstance(current, str)
            assert f'current value "{current}"' in sentence
        _assert_bounds(sentence, contract)

    # The initial cash is the one field that is not a registered parameter, and it
    # is a positive decimal string exactly as the host requires it.
    cash = branches["initial_cash"]
    cash_properties = cast(dict[str, Any], cash["properties"])
    cash_schema = cast(dict[str, Any], cash_properties["value"])
    cash_sentence = cast(str, cash["description"])
    assert cash_schema["type"] == "string"
    assert f'current value "{strategy.draft.initial_cash}"' in cash_sentence
    assert "positive" in cash_sentence
    assert "no registered minimum" in cash_sentence
    assert "no registered maximum" in cash_sentence

    # The instruction the model is given: propose only when the user asks for it.
    system = cast(str, call["system"])
    assert "propose_change" in system
    assert "explicit" in system.lower()


def test_the_conversation_states_each_contract_the_way_the_tool_schema_states_it(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One contract description, the same in the conversation and in the tool."""
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)

    provider.responses.append(_response([], text="An answer."))
    strategies.send_message(
        strategy.user_strategy_id,
        call_id="call-1",
        base_revision=1,
        text="Explain the draft",
        approved_budget=dict(CALL_BUDGET),
    )
    call = provider.calls[-1]
    opening = cast(str, cast(list[Any], call["messages"])[0].text)
    branches = _branches(call)

    for name, contract in _contracts(service).items():
        branch = branches[f"parameters.{name}"]
        current = strategy.draft.parameters[name]
        # The very sentence the tool branch carries is the one the model reads here.
        assert cast(str, branch["description"]) in opening
        assert f"current value {json.dumps(current)}" in opening
        if contract["type"] == "decimal":
            # A decimal is shown as a JSON string, never as a bare number.
            assert f'current value "{current}"' in opening
        else:
            assert f'current value "{current}"' not in opening
    assert cast(str, branches["initial_cash"]["description"]) in opening
    assert f'current value "{strategy.draft.initial_cash}"' in opening


def test_a_json_number_for_a_decimal_field_is_refused_and_its_string_is_applied(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The change the provider must not get away with for a decimal field: 3.

    The real call returned no tool call at all, so this is the shape a model that
    reads the old, untyped schema would send. The host refuses it and proposes
    nothing, and the same change stated as the canonical decimal string the
    contract asks for is a normal proposal that applies as revision 2 — with no
    model call and no experiment behind it.
    """
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    runs = _forbid_engine_runs(monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id

    provider.responses.append(
        _change([{"path": "parameters.target_quantity", "value": 3, "reason": "More size."}])
    )
    refused = strategies.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="Make the size three",
        approved_budget=dict(CALL_BUDGET),
    )
    assert refused.calls[-1]["status"] == "refused"
    assert refused.calls[-1]["message"]
    assert refused.proposals == ()
    assert refused.draft.revision == 1
    assert isinstance(refused.draft, StrategyDraft)
    assert refused.draft.parameters["target_quantity"] == "2"

    _updated, proposal = _propose(
        strategies,
        provider,
        strategy,
        [{"path": "parameters.target_quantity", "value": "3", "reason": "More size."}],
        call_id="call-2",
    )
    assert proposal["status"] == "open"
    assert proposal["items"][0]["before"] == "2" and proposal["items"][0]["after"] == "3"
    applied = strategies.apply_proposal(
        strategy_id, proposal_id=proposal["proposal_id"], operation_id="op-1"
    )
    assert applied.draft.revision == 2
    assert isinstance(applied.draft, StrategyDraft)
    assert applied.draft.parameters["target_quantity"] == "3"
    # Applying a typed diff executes nothing: no further model call, no child, no run.
    assert len(provider.calls) == 2
    assert service.list_jobs() == [] and _children(tmp_path) == [] and runs["count"] == 0


def test_a_local_contract_failure_reaches_no_provider_and_leaves_the_pending_call(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading the contract is local: it happens before the dispatch boundary."""
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    real = strategies._scenario_document

    def unavailable(scenario_id: str) -> dict[str, Any]:
        raise UserStrategyStateError("the registered scenario is not readable here")

    monkeypatch.setattr(strategies, "_scenario_document", unavailable)
    with pytest.raises(UserStrategyStateError):
        strategies.send_message(
            strategy_id,
            call_id="call-1",
            base_revision=1,
            text="Explain the draft",
            approved_budget=dict(CALL_BUDGET),
        )
    # No provider was reached and the reservation never became a dispatch.
    assert provider.calls == []
    durable = _durable(tmp_path, strategy_id)
    assert durable.calls[-1]["status"] == "pending"
    assert "dispatched_at" not in durable.calls[-1]
    assert durable.messages[-1]["role"] == "user"
    assert durable.messages[-1]["call_id"] == "call-1"
    assert all(item["kind"] != "proposal" for item in durable.messages)

    # Existing recovery classifies it exactly as any interruption before dispatch.
    monkeypatch.setattr(strategies, "_scenario_document", real)
    reconciled = strategies.reconcile(strategy_id)
    assert reconciled.calls[-1]["status"] == "interrupted_before_dispatch"
    assert provider.calls == []


def test_a_registered_parameter_this_workspace_cannot_edit_is_read_only(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A price rule is a registered parameter, and it is not a changeable path.

    The fixture scenario registers only editable kinds, so one read-only parameter
    is added to the document this owner reads — the same contract shape the
    workspace itself produces, and the same kind the host already refuses to edit.
    """
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    document = cast(dict[str, Any], service.validate_scenario(SCENARIO))
    document["strategy_parameters"] = [
        *cast(list[dict[str, Any]], document["strategy_parameters"]),
        {
            "name": "rule",
            "type": "price-rule-v1",
            "required": True,
            "default": DEFAULT_RULE,
            "static_minimum": None,
            "static_maximum": None,
            "minimum": None,
            "maximum": None,
            "current_value": DEFAULT_RULE,
        },
    ]
    monkeypatch.setattr(strategies, "_scenario_document", lambda scenario_id: document)

    provider.responses.append(_response([], text="An answer."))
    strategies.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="Explain the draft",
        approved_budget=dict(CALL_BUDGET),
    )
    call = provider.calls[-1]
    branches = _branches(call)
    assert set(branches) == {
        "initial_cash",
        "parameters.target_quantity",
        "parameters.entry_delay_bars",
    }
    opening = cast(str, cast(list[Any], call["messages"])[0].text)
    line = next(item for item in opening.split("\n") if "parameters.rule" in item)
    assert "price-rule-v1" in line and "read-only" in line

    # Naming it anyway is refused by the host, and the draft does not move.
    provider.responses.append(
        _change([{"path": "parameters.rule", "value": DEFAULT_RULE, "reason": "Use this rule."}])
    )
    refused = strategies.send_message(
        strategy_id,
        call_id="call-2",
        base_revision=1,
        text="Use this rule",
        approved_budget=dict(CALL_BUDGET),
    )
    assert refused.calls[-1]["status"] == "refused"
    assert refused.proposals == ()
    assert refused.draft.revision == 1
    assert isinstance(refused.draft, StrategyDraft)
    assert "rule" not in refused.draft.parameters


# --- frozen experiments -------------------------------------------------------


def test_launch_is_idempotent_per_intent_and_a_new_intent_is_a_new_experiment(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id

    document = cast(dict[str, Any], service.validate_scenario(SCENARIO))
    summary = cast(dict[str, Any], document["summary"])
    # The same validation the workspace admits for this draft: the identity a
    # confirmation is compared against is the parameterized input's own identity.
    admitted = cast(
        dict[str, Any],
        service.validate_scenario(
            SCENARIO,
            parameters={
                "initial_cash": strategy.draft.initial_cash,
                "strategy_parameters": dict(strategy.draft.parameters),
                "quantity": None,
                "strategy_source": None,
            },
        ),
    )
    preview = strategies.preview_experiment(strategy_id)
    assert preview["draft_revision"] == 1
    assert preview["strategy_id"] == summary["strategy_id"] == "bounded-long-v1"
    assert preview["scenario_id"] == strategy.draft.scenario_id == SCENARIO
    assert preview["venue"] == summary["venue"] and preview["symbol"] == summary["symbol"]
    assert preview["initial_cash"] == summary["initial_cash"] == "10000"
    # The experiment would run the current draft, not the scenario's original values.
    assert preview["parameters"] == strategy.draft.parameters
    assert preview["parameters"]["target_quantity"] == summary["parameters"]["target_quantity"]
    # The real data identity, the real cost facts and the real budget, not a copy.
    assert preview["record_count"] == summary["record_count"]
    assert preview["data"] == summary["data"]
    assert preview["commission"] == summary["commission"]
    assert preview["slippage"] == summary["slippage"]
    assert preview["latency"] == summary["latency"]
    assert preview["criterion"] == strategy.draft.criterion == FROZEN_CRITERION
    assert preview["budget"] == {"max_model_calls": 10, "max_backtests": 5}
    # The preview carries the admitted snapshot's own identity: the source binding,
    # the input digest and the normalized strategy, execution, risk and data blocks
    # a confirmation would freeze.
    confirmation = cast(str, preview["preview_identity"])
    assert confirmation.startswith("sha256:") and len(confirmation) == len("sha256:") + 64
    assert preview["input_identity"] == admitted["normalized_input_identity"]
    assert preview["input_sha256"]
    normalized = cast(dict[str, Any], preview["normalized"])
    assert normalized["strategy"]["target_quantity"] == "2"
    execution = cast(dict[str, Any], normalized["execution"])
    # The normalized execution is the admitted execution itself. This fixture
    # scenario declares no commission, so there is no value to read here: an absent
    # cost assumption stays absent, and the preview reports exactly that.
    assert preview["commission"] is None
    assert execution.get("commission") == preview["commission"] == summary["commission"]
    assert execution.get("slippage") == preview["slippage"] == summary["slippage"]
    assert execution.get("latency") == preview["latency"] == summary["latency"]
    assert normalized["data"] == preview["data"]
    assert set(normalized) == {"strategy", "execution", "risk", "funding", "instrument", "data"}
    # A preview reads; it starts nothing and stores nothing.
    assert service.list_jobs() == [] and _children(tmp_path) == []
    assert strategies.get(strategy_id).experiments == ()

    with pytest.raises(UserStrategyBudgetError):
        strategies.launch_experiment(
            strategy_id,
            launch_intent_id="li-1",
            base_revision=1,
            preview_identity=confirmation,
            approved_budget=None,
        )
    assert service.list_jobs() == [] and _children(tmp_path) == []
    assert strategies.get(strategy_id).experiments == ()

    launched = _launch(strategies, strategy_id, preview_identity=confirmation)
    first = launched.experiments[-1]
    assert first["launch_intent_id"] == "li-1" and first["status"] == "created"
    assert first["task_id"] and first["job_id"]
    assert {
        "launch_intent_id",
        "request_id",
        "payload_sha256",
        "payload",
        "task_id",
        "job_id",
        "status",
        "message",
        "created_at",
    } <= set(first)
    # The record keeps the immutable payload itself, not only its hash, so the same
    # intent is always compared to what it froze and never to today's draft.
    frozen_payload = cast(dict[str, Any], first["payload"])
    assert frozen_payload["draft_revision"] == 1
    assert frozen_payload["scenario_id"] == SCENARIO
    assert frozen_payload["strategy_id"] == strategy.draft.strategy_id
    assert frozen_payload["parameters"] == strategy.draft.parameters
    assert frozen_payload["initial_cash"] == strategy.draft.initial_cash
    assert frozen_payload["criterion"] == FROZEN_CRITERION
    assert frozen_payload["approved_budget"] == RUN_BUDGET
    assert frozen_payload["record_count"] == preview["record_count"]
    assert frozen_payload["preview_identity"] == confirmation
    assert frozen_payload["input_identity"] == admitted["normalized_input_identity"]
    assert frozen_payload["input_sha256"] == preview["input_sha256"]
    assert frozen_payload["normalized"] == preview["normalized"]
    assert frozen_payload["data"] == summary["data"]
    # This local scenario has no raw upload binding: the selection fingerprint is a
    # data fact and is never stored as if it were the source identity.
    assert frozen_payload["dataset_id"] is None and frozen_payload["source_sha256"] is None
    assert first["payload_sha256"]
    job = service.get_job(cast(str, first["job_id"]))
    assert job.scenario_id == SCENARIO
    assert len(service.list_jobs()) == 1
    children = _children(tmp_path)
    assert len(children) == 1
    child = next(item for item in children if item.task_id == first["task_id"])
    frozen = child.spec_confirmed[0][2]
    assert frozen.strategy_id == strategy.draft.strategy_id
    assert frozen.parameters == strategy.draft.parameters
    # The frozen child carries exactly the approved budget: no model call, one run.
    assert child.budget == RUN_BUDGET
    assert child.usage["model_calls"] == 0 and child.usage["backtests"] == 1

    again = _launch(strategies, strategy_id, preview_identity=confirmation)
    assert again.experiments[-1]["job_id"] == first["job_id"]
    assert len(service.list_jobs()) == 1 and len(_children(tmp_path)) == 1

    # The draft moves on; the same confirmation still resolves to its original
    # experiment, because the request it names is the one that was persisted.
    _strategy, proposal = _propose(strategies, provider, strategy, TWO_FIELDS, call_id="call-1")
    moved = strategies.apply_proposal(
        strategy_id, proposal_id=proposal["proposal_id"], operation_id="op-1"
    )
    assert moved.draft.revision == 2
    still_first = _launch(strategies, strategy_id, base_revision=1, preview_identity=confirmation)
    assert still_first.experiments[-1]["job_id"] == first["job_id"]
    assert len(service.list_jobs()) == 1
    # A changed revision under that same intent is a conflict, never a second run.
    with pytest.raises(UserStrategyConflictError):
        strategies.launch_experiment(
            strategy_id,
            launch_intent_id="li-1",
            base_revision=2,
            preview_identity=confirmation,
            approved_budget=dict(RUN_BUDGET),
        )
    # An approval other than the exact one this intent froze is a conflict too.
    with pytest.raises(UserStrategyConflictError):
        strategies.launch_experiment(
            strategy_id,
            launch_intent_id="li-1",
            base_revision=1,
            preview_identity=confirmation,
            approved_budget={"max_model_calls": 1, "max_backtests": 1},
        )
    assert len(service.list_jobs()) == 1
    # This service runs at most one job at a time, so the first experiment has to
    # be terminal before a second one can be dispatched at all.
    _terminal_job(service, cast(str, first["job_id"]))
    # Only an explicit new intent creates another experiment.
    second = _launch(strategies, strategy_id, launch_intent_id="li-2", base_revision=2).experiments[
        -1
    ]
    assert second["job_id"] != first["job_id"]
    assert len(service.list_jobs()) == 2
    assert len(_children(tmp_path)) == 2
    kept = next(
        item
        for item in strategies.get(strategy_id).experiments
        if item["launch_intent_id"] == "li-1"
    )
    assert kept == first


def test_a_confirmation_of_a_changed_input_is_refused_and_starts_nothing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One draft revision can admit different inputs: the revision is not the identity."""
    strategies, _research, service, _provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    preview = strategies.preview_experiment(strategy_id)
    confirmation = cast(str, preview["preview_identity"])

    # The registered scenario really changes — one of its risk assumptions and its
    # declared cost assumption — while the draft stays exactly where it was, so the
    # same revision now admits a different input: the real engine would freeze
    # different bytes.
    declared_commission = {"policy": "deterministic-commission-v1", "commission_bps": "100"}
    scenario_file = tmp_path / "scenarios" / SCENARIO
    scenario = yaml.safe_load(scenario_file.read_text(encoding="utf-8"))
    scenario["risk"]["max_order_quantity"] = "4"
    scenario["execution"]["commission"] = dict(declared_commission)
    scenario_file.write_text(yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8")

    changed = strategies.preview_experiment(strategy_id)
    assert changed["draft_revision"] == preview["draft_revision"] == 1
    assert changed["normalized"]["risk"] != preview["normalized"]["risk"]
    assert changed["input_sha256"] != preview["input_sha256"]
    assert changed["preview_identity"] != confirmation
    # The declared cost assumption is the scenario's own, and the normalized
    # execution the preview shows is the admitted one: the exact declared value,
    # not a price this owner invented and not the absent value it reported before.
    assert preview["commission"] is None
    assert changed["commission"] == declared_commission
    assert changed["normalized"]["execution"]["commission"] == declared_commission

    with pytest.raises(UserStrategyStaleError):
        strategies.launch_experiment(
            strategy_id,
            launch_intent_id="li-1",
            base_revision=1,
            preview_identity=confirmation,
            approved_budget=dict(RUN_BUDGET),
        )
    # The refusal is free: nothing was written, reserved or dispatched.
    assert strategies.get(strategy_id).experiments == ()
    assert service.list_jobs() == [] and _children(tmp_path) == []

    # Confirming the input that is actually there starts exactly one run, and the
    # frozen payload carries the costs it displayed with the input it confirmed.
    launched = _launch(strategies, strategy_id)
    experiment = launched.experiments[-1]
    assert experiment["status"] == "created" and experiment["job_id"]
    assert experiment["payload"]["preview_identity"] == changed["preview_identity"]
    assert experiment["payload"]["normalized"]["execution"]["commission"] == declared_commission
    assert len(service.list_jobs()) == 1 and len(_children(tmp_path)) == 1


def test_a_crash_before_the_child_never_creates_one_automatically(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategies, research, service, _provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id

    def crash(**kwargs: Any) -> Any:
        raise RuntimeError("simulated crash before any child existed")

    monkeypatch.setattr(research, "create_owned_experiment", crash)
    with pytest.raises(RuntimeError):
        _launch(strategies, strategy_id)
    assert _children(tmp_path) == [] and service.list_jobs() == []
    intent = strategies.get(strategy_id).experiments[-1]
    assert intent["launch_intent_id"] == "li-1"
    assert intent["status"] == "pending"
    assert intent["task_id"] is None and intent["job_id"] is None
    assert intent["payload_sha256"] and intent["request_id"]

    service.stop()
    strategies, _research, service, _provider = _stack(tmp_path, monkeypatch)
    reconciled = strategies.reconcile(strategy_id)
    assert reconciled.experiments[-1]["status"] == "interrupted_before_dispatch"
    assert _children(tmp_path) == [] and service.list_jobs() == []
    # A restart never creates the missing child on its own, not even twice.
    assert (
        strategies.reconcile(strategy_id).experiments[-1]["status"] == "interrupted_before_dispatch"
    )
    assert _children(tmp_path) == [] and service.list_jobs() == []

    # The user's explicit decision is a new intent, and that is the one that runs.
    attempted = _launch(strategies, strategy_id, launch_intent_id="li-2")
    assert attempted.experiments[-1]["status"] == "created"
    assert len(service.list_jobs()) == 1


def test_a_crash_after_job_publication_rebinds_the_same_job(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategies, research, service, _provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    real = research.create_owned_experiment

    def crash_after_publication(**kwargs: Any) -> Any:
        real(**kwargs)
        raise RuntimeError("simulated crash after the child and its job were durable")

    monkeypatch.setattr(research, "create_owned_experiment", crash_after_publication)
    with pytest.raises(RuntimeError):
        _launch(strategies, strategy_id)
    monkeypatch.setattr(research, "create_owned_experiment", real)

    children = _children(tmp_path)
    jobs = service.list_jobs()
    assert len(children) == 1 and len(jobs) == 1
    unpublished = strategies.get(strategy_id).experiments[-1]
    assert unpublished["task_id"] is None
    retained = cast(str, unpublished["payload"]["preview_identity"])

    service.stop()
    strategies, _research, service, _provider = _stack(tmp_path, monkeypatch)
    reconciled = strategies.reconcile(strategy_id)
    intent = reconciled.experiments[-1]
    assert intent["status"] == "created"
    assert intent["task_id"] == children[0].task_id
    assert intent["job_id"] == jobs[0]["job_id"]
    # The published experiment is rebound, never duplicated.
    assert len(service.list_jobs()) == 1 and len(_children(tmp_path)) == 1
    replayed = _launch(strategies, strategy_id, base_revision=1, preview_identity=retained)
    assert replayed.experiments[-1]["job_id"] == jobs[0]["job_id"]
    assert len(service.list_jobs()) == 1


def test_a_confirmed_snapshot_outlives_the_source_it_was_read_from(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frozen payload keeps what the user confirmed, not a pointer to the data."""
    strategies, _research, service, _provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    preview = strategies.preview_experiment(strategy_id)
    retained = cast(str, preview["preview_identity"])
    launched = strategies.launch_experiment(
        strategy_id,
        launch_intent_id="li-1",
        base_revision=1,
        preview_identity=retained,
        approved_budget=dict(RUN_BUDGET),
    )
    experiment = launched.experiments[-1]
    assert experiment["job_id"] and experiment["task_id"]

    # The real source this preview was read from disappears.
    (tmp_path / "scenarios" / "prices.csv").unlink()
    with pytest.raises(UserStrategyStateError):
        strategies.preview_experiment(strategy_id)
    # A new confirmation is impossible while the input is gone: nothing is invented.
    with pytest.raises(UserStrategyStateError):
        _launch(strategies, strategy_id, launch_intent_id="li-2")

    # The confirmed experiment still resolves to exactly what it froze, and the
    # admitted snapshot it recorded is still readable.
    kept = strategies.launch_experiment(
        strategy_id,
        launch_intent_id="li-1",
        base_revision=1,
        preview_identity=retained,
        approved_budget=dict(RUN_BUDGET),
    ).experiments[-1]
    assert kept == experiment
    payload = cast(dict[str, Any], kept["payload"])
    assert payload["input_identity"] == preview["input_identity"]
    assert payload["input_sha256"] == preview["input_sha256"]
    assert payload["normalized"] == preview["normalized"]
    assert payload["record_count"] == preview["record_count"]
    assert payload["data"] == preview["data"]
    assert service.get_job(cast(str, kept["job_id"])).scenario_id == SCENARIO
    assert len(_children(tmp_path)) == 1 and len(service.list_jobs()) == 1


# --- the confirmed input across the final freeze boundary ---------------------

#: The real cost assumption the changed fixture scenario declares; the confirmed
#: scenario declares none, so the changed one is a different admitted cost.
DECLARED_COMMISSION = {"policy": "deterministic-commission-v1", "commission_bps": "100"}


def _admitted(service: WebService, draft: Any) -> dict[str, Any]:
    """The workspace's own admitted input for one draft, normalized by the engine.

    The source identity of the registered document and the normalized identity of
    the parameterized input are two different facts: this returns both, so a test
    never has to guess which one a stored field carries.
    """
    return cast(
        dict[str, Any],
        service.validate_scenario(
            SCENARIO,
            parameters={
                "initial_cash": draft.initial_cash,
                "strategy_parameters": dict(draft.parameters),
                "quantity": None,
                "strategy_source": None,
            },
        ),
    )


def _change_registered_scenario(tmp_path: Any) -> None:
    """Rewrite the registered fixture scenario from the confirmed A to a valid B.

    B declares a real commission and carries different price bytes, so both the
    cost assumption and the data identity a run would freeze have moved, while the
    scenario stays exactly the admitted shape this workspace runs: the declared
    fingerprint is recomputed from the changed bytes through the loader's own
    decoder. Nothing else is written, and no fixture is recopied — not here and
    not by a later restart of this workspace.
    """
    root = tmp_path / "scenarios"
    scenario_file = root / SCENARIO
    document = yaml.safe_load(scenario_file.read_text(encoding="utf-8"))
    data = document["data"]
    document["execution"]["commission"] = dict(DECLARED_COMMISSION)
    data_file = root / data["path"]
    payload = data_file.read_bytes()
    changed = payload.replace(b",108.0,111.0,107.0,110.0,9.0,", b",112.0,113.0,111.0,112.5,9.0,")
    assert changed != payload and len(changed) == len(payload)
    data_file.write_bytes(changed)
    window = ReplayWindow(
        datetime.fromisoformat(data["start_utc"].replace("Z", "+00:00")),
        datetime.fromisoformat(data["end_utc"].replace("Z", "+00:00")),
    )
    dataset = decode_phase1_ohlcv_csv(changed, replay_window=window)
    data["fingerprint"] = {
        "record_count": dataset.selection.fingerprint.record_count,
        "sha256": dataset.selection.fingerprint.sha256.value,
    }
    scenario_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def test_owned_freeze_refuses_a_scenario_changed_before_the_start_rereads_it(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child is frozen from A; a scenario that changed after that never runs."""
    strategies, research, service, _provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    admitted = _admitted(service, strategy.draft)
    # The source binding of this ordinary registered scenario is genuinely absent:
    # the selection fingerprint of the data is a different fact and is never kept
    # as if it were where the bytes came from.
    source_a = cast(dict[str, Any], admitted["input_identity"])
    normalized_a = cast(dict[str, Any], admitted["normalized_input_identity"])
    assert "dataset_id" not in source_a and "source_sha256" not in source_a
    preview = strategies.preview_experiment(strategy_id)
    confirmation = cast(str, preview["preview_identity"])
    # The confirmation names the normalized admitted input, its own digest and the
    # record count of the data — and A's cost assumption is the absent one.
    assert preview["input_identity"] == normalized_a
    assert preview["input_sha256"] and preview["record_count"] == source_a["record_count"]
    assert preview["commission"] is None
    assert cast(dict[str, Any], preview["normalized"])["execution"].get("commission") is None

    observed: dict[str, Any] = {}
    real_start = research._start_backtest

    def start_after_the_scenario_changed(task_id: str, generation: int | None = None) -> Any:
        """The child is durable here; the scenario changes before the real read."""
        if not observed:
            observed["children"] = len(_children(tmp_path))
            _change_registered_scenario(tmp_path)
            changed = _admitted(service, strategy.draft)
            observed["source"] = changed["input_identity"]
            observed["normalized"] = changed["normalized_input_identity"]
            observed["summary"] = changed["summary"]
        return real_start(task_id, generation)

    monkeypatch.setattr(research, "_start_backtest", start_after_the_scenario_changed)
    experiment = _launch(strategies, strategy_id, preview_identity=confirmation).experiments[-1]

    # The change landed after the child was durable, the changed scenario is still
    # a valid admitted input, and it differs in cost, in data and in identity.
    assert observed["children"] == 1
    source_b = cast(dict[str, Any], observed["source"])
    assert source_b["scenario_sha256"] != source_a["scenario_sha256"]
    assert source_b["data_sha256"] != source_a["data_sha256"]
    assert cast(dict[str, Any], observed["normalized"]) != normalized_a
    assert cast(dict[str, Any], observed["summary"])["commission"] == DECLARED_COMMISSION

    # The run is refused: no job was published, no engine submission exists, and
    # the refusal is recorded on the child that carried the confirmation.
    assert experiment["status"] == "failed"
    assert experiment["message"]
    assert experiment["job_id"] is None
    child = _children(tmp_path)[0]
    assert experiment["task_id"] == child.task_id
    assert child.jobs == ()
    assert child.backtest_reservation is None
    assert child.usage["backtests"] == 0
    assert service.list_jobs() == []

    # The confirmation the user made is untouched, durably: it still names A, its
    # normalized cost facts, its data and its absent raw upload binding.
    kept = _durable(tmp_path, strategy_id).experiments[-1]
    assert kept["payload"]["preview_identity"] == confirmation
    assert kept["payload"]["input_identity"] == normalized_a
    assert kept["payload"]["input_sha256"] == preview["input_sha256"]
    assert kept["payload"]["normalized"] == preview["normalized"]
    assert kept["payload"]["data"] == preview["data"]
    assert kept["payload"]["record_count"] == preview["record_count"]
    assert kept["payload"]["dataset_id"] is None and kept["payload"]["source_sha256"] is None

    # A later process over the same workspace. The fixture is not recopied, so the
    # registered scenario is still the changed B and the child is still there.
    service.stop()
    service2 = WebService(tmp_path / "scenarios", tmp_path / "workspace")
    service2.start()
    _STARTED.append(service2)
    research2 = ResearchOrchestrator(
        ResearchStore(tmp_path / "workspace"), service2, _settings_store(tmp_path, monkeypatch)
    )
    strategies2 = UserStrategyService(
        UserStrategyStore(tmp_path / "workspace"),
        research2,
        service2,
        _settings_store(tmp_path, monkeypatch),
    )
    still_b = cast(dict[str, Any], service2.validate_scenario(SCENARIO))
    assert still_b["input_identity"] == source_b

    # The same intent and the same confirmation still cannot reach the changed
    # input, and the owned child cannot be re-driven from the legacy surface.
    replayed = _launch(strategies2, strategy_id, preview_identity=confirmation).experiments[-1]
    assert replayed["job_id"] is None and replayed["status"] == "failed"
    with pytest.raises(ResearchTaskError):
        research2.retry(child.task_id)
    assert service2.list_jobs() == [] and len(_children(tmp_path)) == 1

    # An owned record whose retained confirmation is gone is fail-closed: whatever
    # this process answers, it never falls back to starting the changed input.
    store = UserStrategyStore(tmp_path / "workspace")
    record = store.get(strategy_id)
    stripped = tuple(
        {key: value for key, value in item.items() if key not in {"payload", "payload_sha256"}}
        for item in record.experiments
    )
    store.save(replace(record, experiments=stripped))
    # Whatever this process answers, it never starts the changed input for a
    # record whose retained confirmation cannot be read back.
    with contextlib.suppress(UserStrategyError):
        _launch(strategies2, strategy_id, preview_identity=confirmation)
    assert service2.list_jobs() == [] and len(_children(tmp_path)) == 1


def test_owned_freeze_refuses_a_scenario_changed_at_the_final_submission_boundary(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A -> B between validation and the last input read is refused, not executed."""
    strategies, _research, service, _provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    admitted = _admitted(service, strategy.draft)
    source_a = cast(dict[str, Any], admitted["input_identity"])
    normalized_a = cast(dict[str, Any], admitted["normalized_input_identity"])
    preview = strategies.preview_experiment(strategy_id)
    confirmation = cast(str, preview["preview_identity"])
    assert preview["input_identity"] == normalized_a
    assert preview["commission"] is None

    entered = {"count": 0}
    observed: dict[str, Any] = {}
    real_create_job = service.create_job

    def create_job_after_the_scenario_changed(**kwargs: Any) -> Any:
        """The final submission boundary: the input changes before it is read."""
        entered["count"] += 1
        if not observed:
            observed["before"] = cast(dict[str, Any], service.validate_scenario(SCENARIO))[
                "input_identity"
            ]
            _change_registered_scenario(tmp_path)
            changed = _admitted(service, strategy.draft)
            observed["source"] = changed["input_identity"]
            observed["normalized"] = changed["normalized_input_identity"]
            observed["summary"] = changed["summary"]
        return real_create_job(**kwargs)

    monkeypatch.setattr(service, "create_job", create_job_after_the_scenario_changed)
    experiment = _launch(strategies, strategy_id, preview_identity=confirmation).experiments[-1]

    # The engine still admitted A when this boundary was entered, the change to a
    # valid B landed before it read anything, and the boundary was entered once.
    assert entered["count"] == 1
    assert observed["before"] == source_a
    source_b = cast(dict[str, Any], observed["source"])
    assert source_b["scenario_sha256"] != source_a["scenario_sha256"]
    assert source_b["data_sha256"] != source_a["data_sha256"]
    assert cast(dict[str, Any], observed["normalized"]) != normalized_a
    assert cast(dict[str, Any], observed["summary"])["commission"] == DECLARED_COMMISSION

    # The refusal is definite: no job, no engine submission, and the pending
    # reservation released rather than carried as a possible dispatch.
    assert experiment["status"] == "failed"
    assert experiment["message"]
    assert experiment["job_id"] is None
    child = _children(tmp_path)[0]
    assert experiment["task_id"] == child.task_id
    assert child.jobs == ()
    assert child.backtest_reservation is None
    assert child.usage["backtests"] == 0
    # A refusal that provably happened before any publication is not an unobserved
    # outcome, and no report can exist for a run that never started.
    assert (child.error or {}).get("code") != "job_outcome_unknown"
    assert service.list_jobs() == []

    # What is retained is still A: the confirmed identity, its digest, the cost and
    # data facts the preview showed, and no invented source binding.
    kept = _durable(tmp_path, strategy_id).experiments[-1]
    assert kept["payload"]["preview_identity"] == confirmation
    assert kept["payload"]["input_identity"] == normalized_a
    assert kept["payload"]["input_sha256"] == preview["input_sha256"]
    assert kept["payload"]["normalized"] == preview["normalized"]
    assert kept["payload"]["data"] == preview["data"]
    assert kept["payload"]["record_count"] == preview["record_count"]
    assert kept["payload"]["dataset_id"] is None and kept["payload"]["source_sha256"] is None


@pytest.mark.parametrize(
    ("obstruction", "expected"),
    [
        (
            "gate_failed",
            {"status": "failed", "child": "failed", "code": "submission_refused", "held": False},
        ),
        (
            "cancelled",
            {
                "status": "interrupted_before_dispatch",
                "child": "cancelled",
                "code": None,
                "held": False,
            },
        ),
        (
            "stale_generation",
            {
                "status": "interrupted_before_dispatch",
                "child": "running",
                "code": None,
                "held": True,
            },
        ),
    ],
)
def test_owned_freeze_refuses_publication_when_the_dispatch_gate_cannot_affirm(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, obstruction: str, expected: dict[str, Any]
) -> None:
    """A gate that cannot affirm publishes nothing, submits nothing, refunds nothing twice.

    The three ways a gate can refuse are not one fact. A gate that raised and a user
    who cancelled both leave a child that provably never dispatched: its pending slot
    is released once and no run exists. A stale operation asking about a task another
    owner now holds is different again — it must not touch that owner's own slot, so
    the charge stays exactly where the new owner left it and nothing is written.
    """
    strategies, research, service, _provider = _stack(tmp_path, monkeypatch)
    runs = _forbid_engine_runs(monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    real_gate = research._dispatch_reserved_backtest
    handoff: dict[str, Any] = {}

    def obstructed(task_id: str, generation: int) -> None:
        if obstruction == "gate_failed":
            raise WebBoundaryError(
                "the reserved backtest is no longer this operation's to dispatch"
            )
        if obstruction == "cancelled":
            # The user cancels the child between its reservation and publication.
            research.cancel(task_id)
            real_gate(task_id, generation)
            return
        # A real ownership handoff, the state a restarted process is left in: this
        # operation completes and a new one begins and takes the task over. The old
        # generation asking to dispatch now is asking about somebody else's slot.
        research._transition(task_id, generation, "noop", finish=True)
        handoff["generation"] = research._begin(task_id, "reconcile")
        assert handoff["generation"] is not None
        real_gate(task_id, generation)

    monkeypatch.setattr(research, "_dispatch_reserved_backtest", obstructed)
    experiment = _launch(strategies, strategy_id).experiments[-1]

    assert experiment["status"] == expected["status"]
    assert experiment["job_id"] is None
    child = _children(tmp_path)[0]
    assert experiment["task_id"] == child.task_id
    assert child.status == expected["child"]
    assert child.jobs == ()
    assert (child.error or {}).get("code") == expected["code"]
    assert service.list_jobs() == [] and runs["count"] == 0
    if expected["held"]:
        # The stale operation left the new owner's pending slot exactly as it found
        # it: not released here, not charged twice, and never read as a dispatch.
        assert research._active.get(child.task_id) == handoff["generation"]
        assert cast(Any, child.backtest_reservation).status == "pending"
        assert child.usage["backtests"] == 1
        # The new owner settles that leftover charge once and dispatches nothing.
        research._transition(child.task_id, handoff["generation"], "noop", finish=True)
        settled = research.reconcile(child.task_id)
        assert settled.status == "failed" and settled.jobs == ()
        assert settled.backtest_reservation is None and settled.usage["backtests"] == 0
        assert service.list_jobs() == [] and runs["count"] == 0
    else:
        assert child.backtest_reservation is None and child.usage["backtests"] == 0

        # A later process over the same workspace reads the same answer, and a replay
        # of the same intent resolves to that record: nothing is re-dispatched and
        # nothing is refunded a second time.
        kept = _durable(tmp_path, strategy_id).experiments[-1]
        payload = cast(dict[str, Any], kept["payload"])
        service.stop()
        strategies2, _research2, service2, _provider2 = _stack(tmp_path, monkeypatch)
        replayed = _launch(
            strategies2,
            strategy_id,
            base_revision=payload["draft_revision"],
            preview_identity=payload["preview_identity"],
        ).experiments[-1]
        assert replayed["status"] == expected["status"] and replayed["job_id"] is None
        assert len(_children(tmp_path)) == 1 and _children(tmp_path)[0].usage["backtests"] == 0
        assert service2.list_jobs() == [] and runs["count"] == 0


@pytest.mark.parametrize("landed", [False, True])
def test_owned_freeze_never_refunds_a_dispatch_write_that_did_not_confirm(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, landed: bool
) -> None:
    """An unconfirmed dispatch write keeps its charge: nothing is released or re-sent.

    The gate's own write can fail either before or after its replacement became
    visible, and neither failure proves the dispatch did not land. In both windows
    no job is published and the engine is never reached; the charge is left exactly
    as the durable record has it, and a later process settles it from that record
    alone instead of from what a reread happens to show.
    """
    strategies, research, service, _provider = _stack(tmp_path, monkeypatch)
    runs = _forbid_engine_runs(monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    real_gate = research._dispatch_reserved_backtest
    real_store = research._store
    failed = {"count": 0}

    class UnconfirmedWrite:
        """The real store, whose next save raises around its own replacement."""

        def __getattr__(self, name: str) -> Any:
            return getattr(real_store, name)

        def save(self, task: Any) -> None:
            if failed["count"]:
                real_store.save(task)
                return
            failed["count"] += 1
            if landed:
                # The replacement became visible before the barrier failed.
                real_store.save(task)
            raise OSError("the durable write did not confirm")

    def failing_gate(task_id: str, generation: int) -> None:
        # The very next write this operation makes is its dispatch.
        research._store = cast(Any, UnconfirmedWrite())
        real_gate(task_id, generation)

    monkeypatch.setattr(research, "_dispatch_reserved_backtest", failing_gate)
    experiment = _launch(strategies, strategy_id).experiments[-1]

    assert failed["count"] == 1
    assert experiment["job_id"] is None
    child = _children(tmp_path)[0]
    assert child.jobs == ()
    assert service.list_jobs() == [] and runs["count"] == 0
    # The unconfirmed write keeps ownership and the charge: nothing is released
    # here, and the durable marker alone says whether a dispatch may have landed.
    assert child.usage["backtests"] == 1
    assert cast(Any, child.backtest_reservation).status == ("dispatched" if landed else "pending")
    assert experiment["status"] == ("unknown" if landed else "interrupted_before_dispatch")

    service.stop()
    _strategies2, research2, service2, _provider2 = _stack(tmp_path, monkeypatch)
    settled = research2.reconcile(child.task_id)
    assert settled.jobs == () and service2.list_jobs() == [] and runs["count"] == 0
    if landed:
        # A dispatch that may have happened is never refunded and never re-sent.
        assert settled.backtest_reservation is not None
        assert settled.usage["backtests"] == 1
    else:
        # The write never landed, so the slot is returned once, exactly as any
        # interruption before a durable dispatch marker is.
        assert settled.backtest_reservation is None
        assert settled.usage["backtests"] == 0


def test_owned_freeze_never_resolves_a_request_identity_onto_another_input(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The publication boundary refuses to resolve a request onto a run of other input."""
    strategies, _research, service, _provider = _stack(tmp_path, monkeypatch)
    runs = _forbid_engine_runs(monkeypatch)
    strategy = _create(strategies)
    parameters: dict[str, Any] = {
        "initial_cash": strategy.draft.initial_cash,
        "strategy_parameters": dict(strategy.draft.parameters),
        "quantity": None,
        "strategy_source": None,
    }
    confirmed = service.prepare_input_snapshot(SCENARIO, parameters=parameters)
    expected = ExpectedInput(
        snapshot=cast(dict[str, object], confirmed["snapshot"]),
        input_sha256=cast(str, confirmed["input_sha256"]),
    )

    # The registered input changes to a valid B, and a run of B is published under
    # the very request identity this confirmation owns.
    _change_registered_scenario(tmp_path)
    source_b = cast(dict[str, Any], service.validate_scenario(SCENARIO)["input_identity"])
    first, created = service.create_job(
        scenario_id=SCENARIO,
        input_identity=cast(dict[str, object], source_b),
        parameters=parameters,
        request_id="req-owned",
    )
    assert created and first.input_sha256 != expected.input_sha256
    _terminal_job(service, first.job_id)

    # Resolving that request now would serve B as the confirmed A, so it is refused
    # before anything is created, re-run, relabelled or refunded.
    with pytest.raises(InputChangedError):
        service.create_job(
            scenario_id=SCENARIO,
            input_identity=cast(dict[str, object], source_b),
            parameters=parameters,
            request_id="req-owned",
            expected_input=expected,
        )
    assert runs["count"] == 1
    kept = service.get_job(first.job_id)
    assert kept.status == "succeeded" and kept.input_sha256 == first.input_sha256
    assert len(service.list_jobs()) == 1


@pytest.mark.parametrize("damage", ["snapshot", "partial_owner"])
def test_owned_freeze_never_serves_a_bound_run_under_a_damaged_confirmation(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    """A published run is read back and held to a readable, unchanged confirmation."""
    strategies, _research, service, _provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    experiment = _launch(strategies, strategy_id).experiments[-1]
    job_id = cast(str, experiment["job_id"])
    _terminal_job(service, job_id)
    child = _children(tmp_path)[0]
    assert child.jobs == (job_id,)

    store = ResearchStore(tmp_path / "workspace")
    if damage == "snapshot":
        envelope = cast(dict[str, Any], child.expected_input)
        snapshot = cast(dict[str, Any], envelope["snapshot"])
        scenario = cast(dict[str, Any], snapshot["scenario"])
        # The scenario block is edited while the digest it was admitted under is
        # kept, so the document no longer reproduces its own claimed digest.
        store.save(
            replace(
                child,
                expected_input={
                    **envelope,
                    "snapshot": {
                        **snapshot,
                        "scenario": {
                            **scenario,
                            "funding": {"currency": "USD", "initial_cash": "1"},
                        },
                    },
                },
            )
        )
    else:
        # Half of the owner pair the host wrote: not a legacy record, and not a
        # readable owned one either.
        store.save(replace(child, owner_launch_intent_id=None))

    service.stop()
    _strategies2, research2, service2, _provider2 = _stack(tmp_path, monkeypatch)
    settled = research2.reconcile(child.task_id)

    assert settled.status == "failed"
    assert (settled.error or {}).get("code") == "owned_input_unreadable"
    # The refusal never relabels, refunds or re-sends the run it refused to serve.
    assert settled.jobs == (job_id,)
    assert settled.backtest_reservation is None and settled.usage["backtests"] == 1
    kept = service2.get_job(job_id)
    assert kept.status == "succeeded"
    assert kept.input_sha256 == experiment["payload"]["input_sha256"]
    assert len(service2.list_jobs()) == 1 and len(_children(tmp_path)) == 1


def test_owned_freeze_never_serves_a_bound_run_under_a_damaged_launch_record(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch that never settled is not served a run its retained input does not name."""
    strategies, _research, service, _provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    experiment = _launch(strategies, strategy_id).experiments[-1]
    job_id = cast(str, experiment["job_id"])
    _terminal_job(service, job_id)

    # The interruption window: the child and its run are durable, but the launch
    # record still reads as unsettled — and the input it retained was edited while
    # the digest it was confirmed under was kept.
    store = UserStrategyStore(tmp_path / "workspace")
    saved = store.get(strategy_id)
    payload = cast(dict[str, Any], experiment["payload"])
    snapshot = cast(dict[str, Any], payload["snapshot"])
    scenario = cast(dict[str, Any], snapshot["scenario"])
    pending = {
        **experiment,
        "payload": {
            **payload,
            "snapshot": {
                **snapshot,
                "scenario": {**scenario, "funding": {"currency": "USD", "initial_cash": "1"}},
            },
        },
        "status": "pending",
        "job_id": None,
        "message": None,
    }
    store.save(replace(saved, experiments=(*saved.experiments[:-1], pending)))

    service.stop()
    strategies2, _research2, service2, _provider2 = _stack(tmp_path, monkeypatch)
    reconciled = strategies2.reconcile(strategy_id).experiments[-1]

    assert reconciled["status"] == "failed" and reconciled["job_id"] is None
    assert "not the input that was confirmed" in cast(str, reconciled["message"])
    # The real run is left exactly as it is: still published, still itself.
    kept = service2.get_job(job_id)
    assert kept.status == "succeeded" and kept.input_sha256 == payload["input_sha256"]
    assert len(service2.list_jobs()) == 1 and len(_children(tmp_path)) == 1


@pytest.mark.parametrize("corruption", ["missing", "malformed"])
def test_owned_freeze_never_reads_a_child_whose_confirmed_input_is_unreadable_as_legacy(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    """Ownership comes from the record, not from whether an expectation is present."""
    strategies, research, service, _provider = _stack(tmp_path, monkeypatch)
    runs = _forbid_engine_runs(monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    store = ResearchStore(tmp_path / "workspace")
    real_start = research._start_backtest
    observed: dict[str, Any] = {}

    def start_over_an_unreadable_child(task_id: str, generation: int | None = None) -> Any:
        if not observed:
            child = store.get(task_id)
            observed["owned"] = child.owner_user_strategy_id
            observed["expected"] = child.expected_input is not None
            store.save(
                replace(
                    child,
                    expected_input=(
                        None
                        if corruption == "missing"
                        else {"schema": "radian.owned-expected-input.v0", "request_id": "other"}
                    ),
                )
            )
        return real_start(task_id, generation)

    monkeypatch.setattr(research, "_start_backtest", start_over_an_unreadable_child)
    experiment = _launch(strategies, strategy_id).experiments[-1]

    # The child was durable, owned and carrying a confirmation before it was made
    # unreadable, and the refusal below is about that owned child.
    assert observed == {"owned": strategy_id, "expected": True}
    assert experiment["status"] == "failed"
    assert experiment["message"]
    assert experiment["job_id"] is None
    child = _children(tmp_path)[0]
    assert child.owner_user_strategy_id == strategy_id
    assert experiment["task_id"] == child.task_id
    assert child.jobs == ()
    assert child.backtest_reservation is None and child.usage["backtests"] == 0
    assert (child.error or {}).get("code") == "owned_input_unreadable"
    assert service.list_jobs() == [] and runs["count"] == 0


def test_owned_freeze_keeps_the_confirmed_snapshot_and_the_run_that_froze_it(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A confirmed revision runs, and the run's own report names that exact input."""
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    # The registered scenario the user confirms declares a real commission and
    # carries different price bytes, so the cost the report shows is the confirmed
    # scenario's own declared assumption rather than one this test invented.
    _change_registered_scenario(tmp_path)
    # A second draft revision: what is confirmed is a parameterized scenario, so the
    # normalized identity it runs under is genuinely not the registered document's
    # own source identity, and the two must never be mixed up.
    _updated, proposal = _propose(
        strategies,
        provider,
        strategy,
        [
            {
                "path": "parameters.target_quantity",
                "value": "3",
                "reason": "A wider sample size.",
                "evidence": ["idea: more size"],
            }
        ],
    )
    strategy = strategies.apply_proposal(
        strategy_id, proposal_id=proposal["proposal_id"], operation_id="op-1"
    )
    assert strategy.draft.revision == 2
    preview = strategies.preview_experiment(strategy_id)
    assert preview["commission"] == DECLARED_COMMISSION
    experiment = _launch(strategies, strategy_id).experiments[-1]
    job_id = cast(str, experiment["job_id"])
    _terminal_job(service, job_id)

    child = _children(tmp_path)[0]
    envelope = cast(dict[str, Any], child.expected_input)
    snapshot = cast(dict[str, Any], envelope["snapshot"])
    identity = cast(dict[str, Any], snapshot["identity"])
    source_identity = cast(dict[str, Any], snapshot["source_identity"])
    assert envelope["schema"] == "radian.owned-expected-input.v1"
    assert envelope["request_id"] == child.request_id == experiment["request_id"]
    assert envelope["owner_user_strategy_id"] == child.owner_user_strategy_id == strategy_id
    assert envelope["owner_launch_intent_id"] == child.owner_launch_intent_id == "li-1"
    assert envelope["source_revision"] == experiment["payload"]["draft_revision"] == 2
    assert envelope["input_sha256"] == experiment["payload"]["input_sha256"]
    assert envelope["input_sha256"] == preview["input_sha256"]
    # The retained confirmation is the whole admitted input, not a pointer to it,
    # and the identity it runs under is not the registered source's own.
    assert envelope["snapshot"] == experiment["payload"]["snapshot"]
    assert snapshot["scenario_id"] == SCENARIO
    assert identity == preview["input_identity"]
    assert identity["scenario_sha256"] != source_identity["scenario_sha256"]
    assert identity["data_sha256"] == source_identity["data_sha256"]
    assert snapshot["scenario"]["execution"]["commission"] == DECLARED_COMMISSION
    assert snapshot["scenario"]["execution"]["policy"] == "phase1.next-bar-close.v1"

    # The job that ran froze exactly that document, and its own digest agrees.
    record = service.get_job(job_id)
    assert record.status == "succeeded"
    assert record.input_sha256 == envelope["input_sha256"]
    assert record.input_identity == source_identity
    frozen = record.input_snapshot_bytes
    assert frozen is not None
    assert json.loads(frozen) == snapshot

    # The verified report ties the run it describes to the confirmed data and to the
    # cost the confirmed scenario actually declares.
    report = json.loads(service.report(job_id))
    assert report["run_id"] == record.engine_run_id
    assert report["completion"]["terminal_outcome"] == "success"
    assert report["source"]["scenario_sha256"] == identity["scenario_sha256"]
    assert report["source"]["data"]["fingerprint"] == {
        "record_count": identity["record_count"],
        "sha256": identity["data_sha256"],
    }
    # The base snapshot keeps its own phase 1 execution policy, and the report
    # names the commission wrapper that policy's declared bps actually applies.
    committed_policy_id, committed_policy_sha256 = commission_policy_identity(
        CanonicalDecimal(DECLARED_COMMISSION["commission_bps"])
    )
    assert report["source"]["execution"]["policy_id"] == committed_policy_id
    assert report["source"]["execution"]["policy_sha256"] == committed_policy_sha256
    fees = cast(dict[str, Any], report["economics"]["fees"])
    assert fees["rule"] == DECLARED_COMMISSION["policy"]
    assert fees["currency"] == "USD"
    assert fees["count"] == report["economics"]["counts"]["fills"] >= 1
    charged = Decimal(cast(str, fees["amount"]))
    # The filled quantity at the fixture's own price under 100 bps: exact, non-zero.
    assert charged == Decimal("3.04") and charged > 0


def test_owned_replay_resolves_its_own_request_from_the_durable_record_alone(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed request replays onto its own run, never onto today's source.

    The confirmation, the request identity and the run it already published are
    the evidence about what ran. Once that run is terminal, a replay of the very
    same request resolves to that job from its durable record — in a later
    process, with the registered source changed or gone, and without loading that
    source again, re-entering the publication gate or submitting anything twice.
    """
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    runs = _forbid_engine_runs(monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    # A second draft revision, so the input this confirmation admits is not the
    # registered document's own source identity.
    _updated, proposal = _propose(
        strategies,
        provider,
        strategy,
        [
            {
                "path": "parameters.target_quantity",
                "value": "3",
                "reason": "A wider sample size.",
                "evidence": ["idea: more size"],
            }
        ],
    )
    strategy = strategies.apply_proposal(
        strategy_id, proposal_id=proposal["proposal_id"], operation_id="op-1"
    )
    experiment = _launch(strategies, strategy_id).experiments[-1]
    job_id = cast(str, experiment["job_id"])
    request_id = cast(str, experiment["request_id"])
    _terminal_job(service, job_id)

    # The whole confirmation as the durable record carries it: request identity,
    # source identity, snapshot, digest and the parameters confirmed with them.
    child = _children(tmp_path)[0]
    envelope = cast(dict[str, Any], child.expected_input)
    snapshot = cast(dict[str, Any], envelope["snapshot"])
    source_identity = cast(dict[str, object], snapshot["source_identity"])
    expected = ExpectedInput(
        snapshot=cast(dict[str, object], snapshot),
        input_sha256=cast(str, envelope["input_sha256"]),
    )
    payload = cast(dict[str, Any], experiment["payload"])
    assert isinstance(strategy.draft, StrategyDraft)
    parameters: dict[str, Any] = {
        "initial_cash": strategy.draft.initial_cash,
        "strategy_parameters": dict(strategy.draft.parameters),
        "quantity": None,
        "strategy_source": None,
    }
    assert parameters["initial_cash"] == payload["initial_cash"]
    assert parameters["strategy_parameters"] == payload["parameters"]
    retained_report = service.report(job_id)
    assert service.get_job(job_id).input_sha256 == expected.input_sha256

    # A later process reads the same workspace, and the registered scenario the
    # confirmation was admitted from moves on to a valid B.
    service.stop()
    strategies, _research, service, _provider = _stack(tmp_path, monkeypatch)
    _change_registered_scenario(tmp_path)
    changed = cast(dict[str, Any], service.validate_scenario(SCENARIO))
    changed_expected = cast(
        dict[str, Any], service.prepare_input_snapshot(SCENARIO, parameters=parameters)
    )

    # What is already published is still served as itself, and the same
    # confirmation still resolves to the experiment it confirmed: neither the
    # reread nor the replay previews, reserves, creates or dispatches anything.
    assert service.get_job(job_id).input_sha256 == expected.input_sha256
    assert service.report(job_id) == retained_report
    replayed = strategies.launch_experiment(
        strategy_id,
        launch_intent_id="li-1",
        base_revision=2,
        preview_identity=cast(str, payload["preview_identity"]),
        approved_budget=dict(RUN_BUDGET),
    ).experiments[-1]
    assert replayed["job_id"] == job_id and replayed["status"] == "created"
    recovered = strategies.reconcile(strategy_id).experiments[-1]
    assert recovered["job_id"] == job_id and recovered["status"] == "created"
    # The launch record is "created" because that is what a confirmed launch is;
    # the run it names is the completed one, read back as itself.
    assert service.get_job(job_id).status == "succeeded"

    # A replay carrying other admitted source bytes, other parameters, or other
    # confirmed evidence is not this run, and is refused rather than resolved.
    with pytest.raises(RequestConflictError):
        service.create_job(
            scenario_id=SCENARIO,
            input_identity=cast(dict[str, object], changed["input_identity"]),
            parameters=parameters,
            request_id=request_id,
        )
    conflicting = dict(parameters)
    conflicting["strategy_parameters"] = {
        **cast(dict[str, Any], parameters["strategy_parameters"]),
        "target_quantity": "4",
    }
    with pytest.raises(WebBoundaryError):
        service.create_job(
            scenario_id=SCENARIO,
            input_identity=source_identity,
            parameters=conflicting,
            request_id=request_id,
            expected_input=expected,
        )
    with pytest.raises(InputChangedError):
        service.create_job(
            scenario_id=SCENARIO,
            input_identity=source_identity,
            parameters=parameters,
            request_id=request_id,
            expected_input=ExpectedInput(
                snapshot=cast(dict[str, object], changed_expected["snapshot"]),
                input_sha256=cast(str, changed_expected["input_sha256"]),
            ),
        )

    # Now the registered source itself disappears: the durable record is the only
    # evidence left about this request, and reading it has to be enough.
    (tmp_path / "scenarios" / SCENARIO).unlink()
    assert service.report(job_id) == retained_report

    def never_published() -> None:
        raise AssertionError("a resolved request re-entered the publication boundary")

    record, created = service.create_job(
        scenario_id=SCENARIO,
        input_identity=source_identity,
        parameters=parameters,
        request_id=request_id,
        expected_input=expected,
        before_publish=never_published,
    )
    assert created is False and record.job_id == job_id
    assert record.status == "succeeded"
    assert record.input_sha256 == expected.input_sha256
    assert json.loads(cast(bytes, record.input_snapshot_bytes)) == snapshot
    # One job and one engine run: the replay resolved to what already ran.
    assert len(service.list_jobs()) == 1 and runs["count"] == 1


def test_owned_freeze_never_rebinds_a_run_of_another_input_under_its_own_request(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job found by request identity that ran other input is not this confirmation's."""
    strategies, research, service, _provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    real = research.create_owned_experiment

    def crash(**kwargs: Any) -> Any:
        raise RuntimeError("simulated crash before any child existed")

    monkeypatch.setattr(research, "create_owned_experiment", crash)
    with pytest.raises(RuntimeError):
        _launch(strategies, strategy_id)
    monkeypatch.setattr(research, "create_owned_experiment", real)
    pending = strategies.get(strategy_id).experiments[-1]
    assert pending["status"] == "pending" and pending["task_id"] is None

    # Another input — the same registered scenario, a different size — is published
    # under this launch's own request identity. It is a real, durable run.
    admitted = _admitted(service, strategy.draft)
    other, created = service.create_job(
        scenario_id=SCENARIO,
        input_identity=cast(dict[str, object], admitted["input_identity"]),
        parameters={
            "initial_cash": strategy.draft.initial_cash,
            "strategy_parameters": {**strategy.draft.parameters, "target_quantity": "4"},
            "quantity": None,
            "strategy_source": None,
        },
        request_id=cast(str, pending["request_id"]),
    )
    assert created and other.input_sha256 != pending["payload"]["input_sha256"]
    _terminal_job(service, other.job_id)

    service.stop()
    strategies, _research, service, _provider = _stack(tmp_path, monkeypatch)
    reconciled = strategies.reconcile(strategy_id).experiments[-1]
    # Recovery refuses to present that run as this confirmation's own, and the run
    # itself is left exactly as it is.
    assert reconciled["status"] == "failed"
    assert reconciled["job_id"] is None
    assert "not the input that was confirmed" in cast(str, reconciled["message"])
    assert service.get_job(other.job_id).input_sha256 == other.input_sha256


def test_a_run_that_may_have_been_dispatched_is_unknown_and_never_resent_or_refunded(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine boundary never confirmed publication, so absence is not proven."""
    strategies, _research, service, _provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    submissions = {"count": 0}

    def unconfirmed(**kwargs: Any) -> Any:
        """The boundary is entered, the dispatch is affirmed, then nothing is confirmed."""
        submissions["count"] += 1
        before_publish = kwargs.get("before_publish")
        assert before_publish is not None
        before_publish()
        raise RuntimeError("the engine boundary never confirmed publication")

    monkeypatch.setattr(service, "create_job", unconfirmed)
    launched = _launch(strategies, strategy_id)
    experiment = launched.experiments[-1]
    # The outcome was not observed: this is unknown, never a failed or absent run.
    assert experiment["status"] == "unknown"
    assert experiment["task_id"] and experiment["job_id"] is None
    assert experiment["message"]
    assert submissions["count"] == 1
    child = _children(tmp_path)[0]
    assert child.task_id == experiment["task_id"]
    assert child.backtest_reservation is not None
    # The durable reservation proves a dispatch may have happened.
    assert child.backtest_reservation.status == "dispatched"
    assert child.usage["backtests"] == 1
    assert child.jobs == () and service.list_jobs() == []

    # The same confirmation replays the unknown record; it is never re-sent.
    retained = cast(str, experiment["payload"]["preview_identity"])
    again = _launch(strategies, strategy_id, preview_identity=retained)
    assert again.experiments[-1] == experiment
    assert submissions["count"] == 1
    # A restart classifies nothing new: an unresolved dispatch stays unresolved.
    service.stop()
    strategies, _research, service, _provider = _stack(tmp_path, monkeypatch)
    reconciled = strategies.reconcile(strategy_id)
    assert reconciled.experiments[-1]["status"] == "unknown"
    assert reconciled.experiments[-1]["job_id"] is None
    assert len(_children(tmp_path)) == 1 and service.list_jobs() == []
    assert reconciled.experiments[-1]["task_id"] == experiment["task_id"]


def test_a_refused_child_is_recorded_as_a_failure_with_its_own_message(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal before any run exists is a real failure, not an unobserved outcome."""
    strategies, research, service, _provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id

    def refuse(**kwargs: Any) -> Any:
        raise InvalidSpecError("the confirmed draft is not a registered research strategy")

    monkeypatch.setattr(research, "create_owned_experiment", refuse)
    experiment = _launch(strategies, strategy_id).experiments[-1]
    assert experiment["status"] == "failed"
    assert experiment["message"] == "the confirmed draft is not a registered research strategy"
    assert experiment["task_id"] is None and experiment["job_id"] is None
    # A refusal that created nothing is a failure, not an unknown outcome, and
    # recovery has nothing to reclassify.
    assert _children(tmp_path) == [] and service.list_jobs() == []
    assert strategies.reconcile(strategy_id).experiments[-1]["status"] == "failed"


def test_a_strategy_owned_child_is_frozen_against_the_legacy_edit_and_confirm_paths(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategies, research, service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    launched = _launch(strategies, strategy_id)
    task_id = cast(str, launched.experiments[-1]["task_id"])

    child = _children(tmp_path)[0]
    assert child.task_id == task_id
    assert child.owner_user_strategy_id == strategy_id
    assert child.owner_launch_intent_id == "li-1"
    assert child.spec_confirmed and child.jobs
    frozen = child.spec_confirmed[0][2].document()

    # The child is a frozen experiment, never a second editable draft. Only the
    # ownership guard can answer like this: its spec was confirmed long ago.
    with pytest.raises(TaskStateError, match="user strategy"):
        research.edit_spec(task_id, {"initial_cash": "999999"})
    with pytest.raises(TaskStateError, match="user strategy"):
        research.confirm(task_id)

    _strategy, proposal = _propose(strategies, provider, strategy, TWO_FIELDS, call_id="call-1")
    monkeypatch.setattr(service, "create_job", _never_called)
    strategies.apply_proposal(strategy_id, proposal_id=proposal["proposal_id"], operation_id="op-1")

    unchanged = _children(tmp_path)[0]
    assert unchanged.spec_confirmed[0][2].document() == frozen
    assert unchanged.jobs == child.jobs
    assert strategies.get(strategy_id).draft.revision == 2
    assert len(service.list_jobs()) == 1


def _never_called(**kwargs: Any) -> Any:
    raise AssertionError("no action through the strategy may start another job")


# --- the HTTP surface ---------------------------------------------------------


def test_http_select_dataset_route_is_one_bounded_draft_mutation(tmp_path: Any) -> None:
    """The route publishes a catalog identity, and only the identity."""
    from ea.web.uploads import AVAILABILITY_PROVIDED
    from unit.test_web_uploads import _capture

    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        published = client.post(
            "/api/datasets/upload",
            headers=WRITE_HEADERS,
            json={
                "content": _capture(8).decode("utf-8"),
                "file_name": "vendor-bars.csv",
                "availability_policy": AVAILABILITY_PROVIDED,
            },
        )
        assert published.status_code == 201, published.text
        dataset_id = str(published.json()["dataset_id"])

        created = client.post(
            "/api/user-strategies",
            headers=WRITE_HEADERS,
            json={
                "name": "Upload run",
                "intent": "Select one uploaded dataset",
                "scenario_id": SCENARIO,
            },
        )
        assert created.status_code == 201, created.text
        identity = str(created.json()["user_strategy_id"])
        route = f"/api/user-strategies/{identity}/select-dataset"

        chosen = client.post(
            route,
            headers=WRITE_HEADERS,
            json={"operation_id": "select-1", "base_revision": 1, "dataset_id": dataset_id},
        )
        assert chosen.status_code == 200, chosen.text
        body = cast(dict[str, Any], chosen.json())
        assert body["draft"]["revision"] == 2
        assert body["draft"]["dataset_id"] == dataset_id
        assert body["draft"]["source_sha256"]
        assert body["changes"][-1]["kind"] == "select-dataset"
        assert body["dataset"]["operation_id"] == "select-1"

        # The same operation replays as itself; a selection from the revision the
        # caller read before is stale; a dataset the host cannot bind is refused.
        replay = client.post(
            route,
            headers=WRITE_HEADERS,
            json={"operation_id": "select-1", "base_revision": 1, "dataset_id": dataset_id},
        )
        assert replay.status_code == 200 and replay.json()["draft"]["revision"] == 2
        stale = client.post(
            route,
            headers=WRITE_HEADERS,
            json={"operation_id": "select-2", "base_revision": 1, "dataset_id": dataset_id},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "stale_base_revision"
        unknown = client.post(
            route,
            headers=WRITE_HEADERS,
            json={"operation_id": "select-3", "base_revision": 2, "dataset_id": "absent.csv"},
        )
        assert unknown.status_code == 422
        assert unknown.json()["error"]["code"] == "user_strategy_invalid"
        # A body that names a path instead of an identity is not this contract.
        assert (
            client.post(
                route,
                headers=WRITE_HEADERS,
                json={
                    "operation_id": "select-4",
                    "base_revision": 2,
                    "dataset_id": "/etc/passwd",
                },
            ).status_code
            == 422
        )


def test_http_routes_bound_identities_and_keep_the_frozen_child_read_only(
    tmp_path: Any,
) -> None:
    """The new boundary in one pass: strict bodies, scoped ids, one frozen-child guard."""
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        first_response = client.post(
            "/api/user-strategies",
            headers=WRITE_HEADERS,
            json={
                "name": "HTTP strategy",
                "intent": "Check the routes, not the model",
                "scenario_id": SCENARIO,
                "criterion": CRITERION,
            },
        )
        assert first_response.status_code == 201, first_response.text
        first = cast(dict[str, Any], first_response.json())
        second_response = client.post(
            "/api/user-strategies",
            headers=WRITE_HEADERS,
            json={
                "name": "HTTP strategy two",
                "intent": "The same scenario, with no criterion stated",
                "scenario_id": SCENARIO,
            },
        )
        assert second_response.status_code == 201, second_response.text
        second = cast(dict[str, Any], second_response.json())
        assert first["draft"]["criterion"] == FROZEN_CRITERION
        assert second["draft"]["criterion"] is None
        listed = cast(list[dict[str, Any]], client.get("/api/user-strategies").json()["strategies"])
        assert {item["user_strategy_id"] for item in listed} == {
            first["user_strategy_id"],
            second["user_strategy_id"],
        }

        # A proposal identity belongs to its own strategy: an unknown one is refused
        # and the draft it was aimed at is untouched.
        refused = client.post(
            f"/api/user-strategies/{second['user_strategy_id']}/proposals/"
            f"{first['user_strategy_id']}/apply",
            headers=WRITE_HEADERS,
            json={"operation_id": "op-1"},
        )
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "user_strategy_invalid"
        untouched = client.get(f"/api/user-strategies/{second['user_strategy_id']}")
        assert untouched.json()["draft"]["revision"] == 1
        assert untouched.json()["changes"] == []

        # An identity that is not a bounded id never names a file, an absent id is a
        # plain 404, and an unbounded body fails the strict schema before any service
        # call — no path, URL or key is ever accepted from the client.
        assert client.get("/api/user-strategies/settings.json").status_code == 422
        assert client.get(f"/api/user-strategies/{'x' * 65}").status_code == 422
        assert client.get(f"/api/user-strategies/{uuid4()}").status_code == 404
        assert (
            client.post(
                "/api/user-strategies",
                headers=WRITE_HEADERS,
                json={"name": "", "intent": "no name", "scenario_id": SCENARIO},
            ).status_code
            == 422
        )

        # The undo route names the revision the caller is looking at and the
        # application it reverses; the older two-field body is refused by the schema.
        assert (
            client.post(
                f"/api/user-strategies/{first['user_strategy_id']}/undo",
                headers=WRITE_HEADERS,
                json={"operation_id": "op-1"},
            ).status_code
            == 422
        )

        # One explicit confirmation starts the real experiment path: a preview, then a
        # child task and one engine job, with the confirmed payload retained on the record.
        preview = client.get(f"/api/user-strategies/{first['user_strategy_id']}/experiment/preview")
        assert preview.status_code == 200, preview.text
        previewed = cast(dict[str, Any], preview.json())
        assert previewed["preview_identity"] and previewed["input_sha256"]
        assert previewed["normalized"]["strategy"]["target_quantity"] == "2"
        # A confirmation that does not carry the previewed identity never reaches the
        # service: the strict body refuses it first.
        assert (
            client.post(
                f"/api/user-strategies/{first['user_strategy_id']}/experiments",
                headers=WRITE_HEADERS,
                json={
                    "launch_intent_id": "li-1",
                    "base_revision": 1,
                    "approved_budget": dict(RUN_BUDGET),
                },
            ).status_code
            == 422
        )
        launch = client.post(
            f"/api/user-strategies/{first['user_strategy_id']}/experiments",
            headers=WRITE_HEADERS,
            json={
                "launch_intent_id": "li-1",
                "base_revision": 1,
                "preview_identity": previewed["preview_identity"],
                "approved_budget": dict(RUN_BUDGET),
            },
        )
        assert launch.status_code == 200, launch.text
        experiment = cast(dict[str, Any], launch.json()["experiments"][0])
        assert experiment["status"] == "created" and experiment["job_id"]
        assert experiment["task_id"]
        assert experiment["payload"]["draft_revision"] == 1
        assert experiment["payload"]["criterion"] == FROZEN_CRITERION
        assert experiment["payload"]["preview_identity"] == previewed["preview_identity"]
        assert experiment["payload"]["approved_budget"] == RUN_BUDGET

        # The legacy research surface can neither edit nor re-confirm a frozen child.
        edit = client.patch(
            f"/api/research/tasks/{experiment['task_id']}/spec",
            headers=WRITE_HEADERS,
            json={"initial_cash": "999999"},
        )
        assert edit.status_code == 409
        assert edit.json()["error"]["code"] == "spec_edit_rejected"
        confirm = client.post(
            f"/api/research/tasks/{experiment['task_id']}/confirm",
            headers=WRITE_HEADERS,
            json={"auto_run": True},
        )
        assert confirm.status_code == 409
        assert "user strategy" in confirm.json()["error"]["message"]

        # Neither the draft nor the confirmed experiment moved.
        after = cast(
            dict[str, Any],
            client.get(f"/api/user-strategies/{first['user_strategy_id']}").json(),
        )
        assert after["draft"]["revision"] == 1
        kept = cast(list[dict[str, Any]], after["experiments"])
        assert len(kept) == 1
        assert kept[0]["job_id"] == experiment["job_id"]
        assert kept[0]["status"] == "created"


# --- intent first: one unbound intent and one explicit binding ----------------
#
# The same durable user strategy may exist before it has a scenario: an intent
# with no executable draft, its own conversation and its own approved calls. A
# scenario is bound later, explicitly, as one recorded revision of that same
# record. Nothing here is a second chat object, a second store or a second
# budget: the intent's identity, messages, calls, proposal history and
# experiments are the ones the bound strategy keeps.

#: The unbound draft as it is encoded and read back: an intent and nothing else.
INTENT_DRAFT = {"kind": "intent", "revision": 1, "scenario_id": None}
#: One user's research intent, stated before any scenario exists.
INTENT = "Test whether buying a fixed-size dip has any edge on the fixture data"


def _empty_stack(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    responses: list[Any] | None = None,
) -> tuple[UserStrategyService, ResearchOrchestrator, WebService, FakeProvider]:
    """One process image whose scenario catalog is genuinely empty."""
    root = tmp_path / "empty-scenarios"
    root.mkdir(exist_ok=True)
    service = WebService(root, tmp_path / "workspace")
    service.start()
    _STARTED.append(service)
    research = ResearchOrchestrator(
        ResearchStore(tmp_path / "workspace"), service, _settings_store(tmp_path, monkeypatch)
    )
    strategies = UserStrategyService(
        UserStrategyStore(tmp_path / "workspace"),
        research,
        service,
        _settings_store(tmp_path, monkeypatch),
    )
    provider = RoutedProvider(responses or [])
    cast(Any, strategies)._provider = provider  # test seam, same ModelPort interface
    return strategies, research, service, provider


def _new_intent(
    strategies: UserStrategyService, intent: str = INTENT, name: str = "New idea"
) -> Any:
    """One unbound strategy: a saved intent, no scenario and no executable draft."""
    return strategies.create(name=name, intent=intent, scenario_id=None)


def test_an_intent_is_saved_and_reopened_with_no_scenario_catalog(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first slice in one place: a save that needs no scenario to exist."""
    strategies, _research, service, provider = _empty_stack(tmp_path, monkeypatch)
    created = _new_intent(strategies)

    # The draft is an intent and nothing else: no strategy, venue, symbol, funding,
    # parameter or scenario is invented to make it look executable.
    assert created.draft.document() == INTENT_DRAFT
    assert created.user_strategy_id
    assert created.name == "New idea" and created.intent == INTENT
    assert created.history == () and created.messages == () and created.calls == ()
    assert created.proposals == () and created.changes == () and created.experiments == ()
    # Saving an intent is a save: no provider, no reservation, no job, no child.
    assert provider.calls == [] and service.list_jobs() == [] and _children(tmp_path) == []
    # This catalog really has nothing to bind, so no executable draft can exist here.
    with pytest.raises(UserStrategyStateError):
        strategies.create(name="Bound", intent=INTENT, scenario_id=SCENARIO)
    assert len(strategies.list()) == 1

    # A later process over the same workspace reopens the same identity, revision
    # and conversation, and still cannot invent a scenario for it.
    service.stop()
    strategies, _research, service, provider = _empty_stack(tmp_path, monkeypatch)
    reopened = strategies.get(created.user_strategy_id)
    assert reopened == created
    assert [item.user_strategy_id for item in strategies.list()] == [created.user_strategy_id]
    with pytest.raises(UserStrategyStateError):
        strategies.bind_scenario(
            created.user_strategy_id, operation_id="bind-1", base_revision=1, scenario_id=SCENARIO
        )
    assert strategies.get(created.user_strategy_id).draft.document() == INTENT_DRAFT
    assert strategies.get(created.user_strategy_id).changes == ()


def test_an_unbound_clarification_has_only_read_tools_and_its_own_history(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly one approved call, told only this intent's own facts and turns."""
    # The catalog is genuinely empty here: an approved clarification of a saved
    # intent is proven without a scenario existing anywhere in this workspace.
    strategies, _research, service, provider = _empty_stack(tmp_path, monkeypatch)
    runs = _forbid_engine_runs(monkeypatch)
    strategy = _new_intent(strategies)
    strategy_id = strategy.user_strategy_id
    other = _new_intent(strategies, intent="A second, unrelated idea", name="Other idea")

    provider.responses.append(_response([], text="Tell me which market and data you have."))
    answered = strategies.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="What do you need from me?",
        approved_budget=dict(CALL_BUDGET),
    )
    assert answered.calls[-1]["status"] == "observed"
    assert answered.messages[-1]["text"] == "Tell me which market and data you have."
    assert answered.draft.document() == INTENT_DRAFT
    assert answered.proposals == ()

    call = provider.calls[-1]
    # An unbound intent may inspect actual resources, but cannot propose, bind or run.
    assert [tool.name for tool in call["tools"]] == [
        "discover_resources",
        "inspect_research_input",
    ]
    assert call["required_capabilities"] == ("tool_use",)
    messages = cast(list[Any], call["messages"])
    texts = [message.text for message in messages]
    joined = "\n".join(texts)
    opening = cast(str, texts[0])
    assert INTENT in opening
    assert other.intent not in opening and other.user_strategy_id not in opening
    assert texts[-1] == "What do you need from me?"
    assert texts.count("What do you need from me?") == 1
    # The intent is stated once in this request, however the host words it.
    assert joined.count(INTENT) == 1
    # No scenario, strategy, instrument, funding or run is stated as a fact about
    # this strategy, and the model is told plainly what it is: an intent with
    # nothing bound yet, so it can only clarify.
    assert SCENARIO not in opening and "bounded-long" not in opening
    assert "Host-verified" not in opening
    assert "no scenario" in opening.lower() or "unbound" in opening.lower()

    # The next question carries the recorded conversation, once each, and this
    # strategy's history stays its own.
    provider.responses.append(_response([], text="One more answer."))
    strategies.send_message(
        strategy_id,
        call_id="call-2",
        base_revision=1,
        text="Second question",
        approved_budget=dict(CALL_BUDGET),
    )
    messages = cast(list[Any], provider.calls[-1]["messages"])
    texts = [message.text for message in messages]
    assert [message.role for message in messages] == ["user", "user", "assistant", "user"]
    assert texts[-1] == "Second question" and texts.count("Second question") == 1
    assert texts.count("What do you need from me?") == 1
    assert texts.count("Tell me which market and data you have.") == 1
    assert strategies.get(other.user_strategy_id).messages == ()
    assert len(provider.calls) == 2
    assert service.list_jobs() == [] and _children(tmp_path) == [] and runs["count"] == 0


def test_an_unbound_clarification_with_no_configured_provider_is_still_saved_honestly(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No provider is a fact about the call, never a reason to lose the intent."""
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch, scripted=False)
    strategy = _new_intent(strategies)
    strategy_id = strategy.user_strategy_id
    assert not (tmp_path / "settings.json").exists()

    answered = strategies.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="Explain what this idea still needs",
        approved_budget=dict(CALL_BUDGET),
    )
    call = answered.calls[-1]
    assert call["status"] == "unconfigured"
    assert call["message"]
    # Nothing was measured, so nothing is reported as a measurement.
    assert call["input_tokens"] is None and call["output_tokens"] is None
    assert call["usage_available"] is False
    assert call["cost"]["available"] is False and call["cost"]["amount"] is None
    assert call["base_revision"] == 1
    assert provider.calls == []
    # The user's own turn and the honest call record are durable, and the intent is
    # still the same intent it was before the call.
    assert answered.messages[-1]["role"] == "user"
    assert answered.messages[-1]["text"] == "Explain what this idea still needs"
    assert answered.draft.document() == INTENT_DRAFT
    durable = _durable(tmp_path, strategy_id)
    assert durable.calls[-1]["status"] == "unconfigured"
    assert durable.messages == answered.messages

    service.stop()
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch, scripted=False)
    reopened = strategies.get(strategy_id)
    assert reopened.calls == durable.calls and reopened.messages == durable.messages
    assert reopened.draft.document() == INTENT_DRAFT
    assert service.list_jobs() == [] and _children(tmp_path) == []


def test_an_unbound_clarification_replays_and_its_approval_is_per_request(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The intent's revision is revision 1, and it reuses the bound call ledger."""
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _new_intent(strategies)
    strategy_id = strategy.user_strategy_id

    # An explicit zero budget is refused, exactly as on a bound draft.
    with pytest.raises(UserStrategyBudgetError):
        strategies.send_message(
            strategy_id,
            call_id="call-1",
            base_revision=1,
            text="No approval",
            approved_budget={"max_model_calls": 0, "max_backtests": 0},
        )
    with pytest.raises(UserStrategyBudgetError):
        strategies.send_message(
            strategy_id,
            call_id="call-1",
            base_revision=1,
            text="No approval",
            approved_budget={"max_model_calls": 99, "max_backtests": 0},
        )
    assert provider.calls == [] and strategies.get(strategy_id).calls == ()

    provider.responses.append(_response([], text="One answer."))
    first = strategies.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="Same question",
        approved_budget=dict(CALL_BUDGET),
    )
    assert len(provider.calls) == 1
    replayed = strategies.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="Same question",
        approved_budget=dict(CALL_BUDGET),
    )
    assert len(provider.calls) == 1
    assert replayed.calls == first.calls and replayed.messages == first.messages
    assert _durable(tmp_path, strategy_id).calls == first.calls

    with pytest.raises(UserStrategyConflictError):
        strategies.send_message(
            strategy_id,
            call_id="call-1",
            base_revision=1,
            text="A different question",
            approved_budget=dict(CALL_BUDGET),
        )
    with pytest.raises(UserStrategyConflictError):
        strategies.send_message(
            strategy_id,
            call_id="call-1",
            base_revision=1,
            text="Same question",
            approved_budget={"max_model_calls": 1, "max_backtests": 1},
        )
    # The intent's own revision is the base revision: no revision 0 is pretended.
    with pytest.raises(UserStrategyStaleError):
        strategies.send_message(
            strategy_id,
            call_id="call-2",
            base_revision=2,
            text="A revision this intent never had",
            approved_budget=dict(CALL_BUDGET),
        )
    assert len(provider.calls) == 1


def test_an_unbound_answer_that_calls_a_tool_is_refused_with_its_measured_usage(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No proposal can exist before a scenario is bound, and this answer was observed."""
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    runs = _forbid_engine_runs(monkeypatch)
    strategy = _new_intent(strategies)
    strategy_id = strategy.user_strategy_id
    provider.responses.append(_change(TWO_FIELDS, call_id="call-1"))

    refused = strategies.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="Change my draft, please",
        approved_budget=dict(CALL_BUDGET),
    )
    call = refused.calls[-1]
    assert call["status"] == "refused" and call["message"]
    # The provider answered, so what it used is a measurement, not a guess.
    assert call["model"] == "fake-model"
    assert call["input_tokens"] == 11 and call["output_tokens"] == 7
    assert call["usage_available"] is True
    assert refused.proposals == ()
    assert all(item["kind"] != "proposal" for item in refused.messages)
    assert refused.draft.document() == INTENT_DRAFT
    assert refused.changes == () and refused.history == () and refused.experiments == ()
    assert service.list_jobs() == [] and _children(tmp_path) == [] and runs["count"] == 0
    # A refusal is a settled outcome, so recovery has nothing to classify here.
    assert strategies.reconcile(strategy_id).calls[-1]["status"] == "refused"


def test_an_unbound_tool_answer_never_becomes_a_proposal_after_binding(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An answer to an unbound turn can never be replayed as a proposal on the draft."""
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _new_intent(strategies)
    strategy_id = strategy.user_strategy_id
    provider.responses.append(_change(TWO_FIELDS, call_id="call-1"))
    strategies.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="Change my draft, please",
        approved_budget=dict(CALL_BUDGET),
    )

    bound = strategies.bind_scenario(
        strategy_id, operation_id="bind-1", base_revision=1, scenario_id=SCENARIO
    )
    assert bound.draft.revision == 2
    assert bound.proposals == ()
    # Nothing this turn observed becomes a proposal or a proposal message: both
    # ledgers are read here as their own explicit type.
    proposals: list[dict[str, Any]] = list(bound.proposals)
    messages: list[dict[str, Any]] = list(bound.messages)
    assert all(item["status"] != "open" for item in proposals)
    assert all(item["kind"] != "proposal" for item in messages)
    assert len(provider.calls) == 1


def test_unbound_executable_actions_are_refused_before_any_effect(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hiding a button is not a guard: every executable action refuses on its own."""
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    runs = _forbid_engine_runs(monkeypatch)
    strategy = _new_intent(strategies)
    strategy_id = strategy.user_strategy_id

    with pytest.raises(UserStrategyStateError):
        strategies.apply_proposal(strategy_id, proposal_id="proposal-1", operation_id="op-1")
    with pytest.raises(UserStrategyStateError):
        strategies.reject_proposal(strategy_id, proposal_id="proposal-1")
    with pytest.raises(UserStrategyStateError):
        strategies.revise_proposal(
            strategy_id,
            proposal_id="proposal-1",
            call_id="call-1",
            text="Try again",
            approved_budget=dict(CALL_BUDGET),
        )
    with pytest.raises(UserStrategyStateError):
        strategies.undo(
            strategy_id, operation_id="op-1", head_revision=1, target_operation_id="op-0"
        )
    with pytest.raises(UserStrategyStateError):
        strategies.preview_experiment(strategy_id)
    with pytest.raises(UserStrategyStateError):
        strategies.launch_experiment(
            strategy_id,
            launch_intent_id="li-1",
            base_revision=1,
            preview_identity="sha256:" + "0" * 64,
            approved_budget=dict(RUN_BUDGET),
        )

    # Every refusal happened before a reservation, a call, a child or a job existed.
    refused = strategies.get(strategy_id)
    assert refused.draft.document() == INTENT_DRAFT
    assert refused.messages == () and refused.calls == () and refused.proposals == ()
    assert refused.changes == () and refused.history == () and refused.experiments == ()
    assert provider.calls == []
    assert service.list_jobs() == [] and _children(tmp_path) == [] and runs["count"] == 0
    assert len(strategies.list()) == 1


def test_binding_forms_the_executable_draft_of_the_chosen_scenario_once(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One explicit choice of one real scenario completes this same record."""
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    runs = _forbid_engine_runs(monkeypatch)
    strategy = _new_intent(strategies)
    strategy_id = strategy.user_strategy_id
    provider.responses.append(_response([], text="A clarification before binding."))
    clarified = strategies.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="What would a first experiment need?",
        approved_budget=dict(CALL_BUDGET),
    )

    bound = strategies.bind_scenario(
        strategy_id,
        operation_id="bind-1",
        base_revision=1,
        scenario_id=SCENARIO,
        criterion=CRITERION,
    )

    # One identity, one conversation, one call ledger: binding forked nothing.
    assert bound.user_strategy_id == strategy_id
    assert bound.name == clarified.name and bound.intent == clarified.intent
    assert bound.created_at == clarified.created_at
    assert bound.messages == clarified.messages and bound.calls == clarified.calls
    assert len(provider.calls) == 1
    # The intent was revision 1; the executable draft is revision 2 of that same
    # record, built exactly the way the legacy bound create builds it.
    assert [item.document() for item in bound.history] == [INTENT_DRAFT]
    assert bound.draft.revision == 2 and bound.draft.scenario_id == SCENARIO
    legacy = _create(strategies, "Bound the legacy way")
    assert bound.draft == replace(legacy.draft, revision=2)
    assert bound.draft.criterion == FROZEN_CRITERION
    # The binding is one durable operation with one recorded revision advance, and
    # the request it was made from is kept to resolve a replay.
    assert [item["operation_id"] for item in bound.changes] == ["bind-1"]
    change = bound.changes[-1]
    assert change["kind"] == "bind"
    assert change["from_revision"] == 1 and change["to_revision"] == 2
    assert change["scenario_id"] == SCENARIO
    # Binding called no model, previewed nothing, created nothing and ran nothing.
    assert service.list_jobs() == [] and _children(tmp_path) == [] and runs["count"] == 0
    assert bound.experiments == ()
    assert len(strategies.list()) == 2


def test_a_repeated_binding_replays_and_a_changed_or_stale_one_is_refused(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One operation is one binding; an invalid, stale or second one binds nothing."""
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _new_intent(strategies)
    strategy_id = strategy.user_strategy_id

    with pytest.raises(UserStrategyStateError):
        strategies.bind_scenario(
            strategy_id, operation_id="bind-0", base_revision=1, scenario_id="missing.yaml"
        )
    with pytest.raises(UserStrategyStaleError):
        strategies.bind_scenario(
            strategy_id, operation_id="bind-0", base_revision=2, scenario_id=SCENARIO
        )
    with pytest.raises(UserStrategyStateError):
        strategies.bind_scenario(
            strategy_id,
            operation_id="bind-0",
            base_revision=1,
            scenario_id=SCENARIO,
            criterion={"field": "made.up", "expectation": "positive"},
        )
    assert strategies.get(strategy_id).draft.document() == INTENT_DRAFT
    assert strategies.get(strategy_id).changes == ()
    assert provider.calls == []

    bound = strategies.bind_scenario(
        strategy_id, operation_id="bind-1", base_revision=1, scenario_id=SCENARIO
    )
    assert bound.draft.revision == 2
    assert isinstance(bound.draft, StrategyDraft)
    assert bound.draft.criterion is None

    # The same operation with the same request is the same binding, not a second one.
    replayed = strategies.bind_scenario(
        strategy_id, operation_id="bind-1", base_revision=1, scenario_id=SCENARIO
    )
    assert replayed.draft == bound.draft and replayed.changes == bound.changes
    # The whole request is its identity: another scenario, criterion or revision
    # under that operation id is a conflict, never a second binding.
    for scenario_id, criterion in (("flat.yaml", None), (SCENARIO, CRITERION)):
        with pytest.raises(UserStrategyConflictError):
            strategies.bind_scenario(
                strategy_id,
                operation_id="bind-1",
                base_revision=1,
                scenario_id=scenario_id,
                criterion=criterion,
            )
    # A new operation cannot rebind an already executable draft.
    with pytest.raises(UserStrategyStateError):
        strategies.bind_scenario(
            strategy_id, operation_id="bind-2", base_revision=2, scenario_id=SCENARIO
        )
    assert strategies.get(strategy_id).draft.revision == 2
    assert provider.calls == []


def test_an_approval_given_before_binding_is_never_spent_after_it(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binding is a revision: an approval made on the intent is not a bound one."""
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _new_intent(strategies)
    strategy_id = strategy.user_strategy_id
    provider.responses.append(_response([], text="A clarification."))
    unbound = strategies.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="What is missing?",
        approved_budget=dict(CALL_BUDGET),
    )
    bound = strategies.bind_scenario(
        strategy_id, operation_id="bind-1", base_revision=1, scenario_id=SCENARIO
    )
    assert bound.draft.revision == 2 and len(provider.calls) == 1

    # A new request against the old intent revision is stale, and the old call id
    # replays what it actually recorded instead of being spent a second time.
    with pytest.raises(UserStrategyStaleError):
        strategies.send_message(
            strategy_id,
            call_id="call-2",
            base_revision=1,
            text="A new question on the old revision",
            approved_budget=dict(CALL_BUDGET),
        )
    replayed = strategies.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="What is missing?",
        approved_budget=dict(CALL_BUDGET),
    )
    assert replayed.calls == unbound.calls and replayed.messages == unbound.messages
    assert replayed.draft.revision == 2
    assert len(provider.calls) == 1


def test_a_binding_replays_across_restart_and_after_the_draft_moves_on(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored request, not today's draft, is what a repeated binding resolves."""
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    strategy = _new_intent(strategies)
    strategy_id = strategy.user_strategy_id
    bound = strategies.bind_scenario(
        strategy_id,
        operation_id="bind-1",
        base_revision=1,
        scenario_id=SCENARIO,
        criterion=CRITERION,
    )
    assert bound.draft.revision == 2

    service.stop()
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    replayed = strategies.bind_scenario(
        strategy_id,
        operation_id="bind-1",
        base_revision=1,
        scenario_id=SCENARIO,
        criterion=CRITERION,
    )
    assert replayed.draft.revision == 2 and len(replayed.changes) == 1

    # The draft moves on; the original binding request is still the one resolved.
    provider.responses.append(_change(TWO_FIELDS, call_id="call-2"))
    proposed = strategies.send_message(
        strategy_id,
        call_id="call-2",
        base_revision=2,
        text="Propose a change to the draft",
        approved_budget=dict(CALL_BUDGET),
    )
    applied = strategies.apply_proposal(
        strategy_id, proposal_id=proposed.proposals[-1]["proposal_id"], operation_id="op-1"
    )
    assert applied.draft.revision == 3
    still = strategies.bind_scenario(
        strategy_id,
        operation_id="bind-1",
        base_revision=1,
        scenario_id=SCENARIO,
        criterion=CRITERION,
    )
    assert still.draft.revision == 3 and len(still.changes) == 2
    # A new operation still cannot rebind, and none of this called the model.
    with pytest.raises(UserStrategyStateError):
        strategies.bind_scenario(
            strategy_id, operation_id="bind-2", base_revision=3, scenario_id=SCENARIO
        )
    assert len(provider.calls) == 1
    assert service.list_jobs() == [] and _children(tmp_path) == []


@pytest.mark.parametrize("interruption", ["pending", "dispatched"])
def test_binding_refuses_an_unresolved_call_until_recovery_classifies_it(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    """An unresolved call is settled first; the binding never overwrites it."""
    responses: list[Any] = [Crash()] if interruption == "dispatched" else []
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch, responses)
    strategy = _new_intent(strategies, "An idea whose first call was interrupted")
    strategy_id = strategy.user_strategy_id

    def crash(*args: Any, **kwargs: Any) -> Any:
        raise Crash()

    if interruption == "pending":
        # The process died after the reservation was durable and before dispatch.
        monkeypatch.setattr(strategies, "_mark_dispatched", crash)
    with pytest.raises(Crash):
        strategies.send_message(
            strategy_id,
            call_id="call-1",
            base_revision=1,
            text="Did this question reach the model?",
            approved_budget=dict(CALL_BUDGET),
        )
    assert _durable(tmp_path, strategy_id).calls[-1]["status"] == interruption
    assert len(provider.calls) == (1 if interruption == "dispatched" else 0)

    with pytest.raises(UserStrategyStateError):
        strategies.bind_scenario(
            strategy_id, operation_id="bind-1", base_revision=1, scenario_id=SCENARIO
        )
    assert _durable(tmp_path, strategy_id).draft.document() == INTENT_DRAFT
    assert _durable(tmp_path, strategy_id).changes == ()

    service.stop()
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    reconciled = strategies.reconcile(strategy_id)
    assert reconciled.calls[-1]["status"] == (
        "unknown" if interruption == "dispatched" else "interrupted_before_dispatch"
    )
    assert provider.calls == []

    # Only after recovery has classified it can the user bind the scenario.
    bound = strategies.bind_scenario(
        strategy_id, operation_id="bind-1", base_revision=1, scenario_id=SCENARIO
    )
    assert bound.draft.revision == 2 and bound.draft.scenario_id == SCENARIO
    assert bound.calls == reconciled.calls
    assert provider.calls == [] and service.list_jobs() == [] and _children(tmp_path) == []


def test_a_binding_never_waits_for_an_in_flight_call_and_is_refused_while_it_is_held(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-strategy lock is shared, and a busy strategy refuses instead of waiting."""
    strategies, _research, _service, _provider = _stack(tmp_path, monkeypatch)
    strategy = _new_intent(strategies, "An idea being clarified right now")
    strategy_id = strategy.user_strategy_id
    provider = RoutedBlockingProvider([_response([], text="A clarification.")])
    cast(Any, strategies)._provider = provider

    failures: list[BaseException] = []

    def clarify() -> None:
        try:
            strategies.send_message(
                strategy_id,
                call_id="call-1",
                base_revision=1,
                text="What data would this need?",
                approved_budget=dict(CALL_BUDGET),
            )
        except BaseException as error:  # noqa: BLE001 — recorded for the assertion below
            failures.append(error)

    sender = threading.Thread(target=clarify)
    sender.start()
    assert provider.invoked.wait(10)

    outcomes: list[BaseException] = []

    def bind() -> None:
        try:
            strategies.bind_scenario(
                strategy_id, operation_id="bind-1", base_revision=1, scenario_id=SCENARIO
            )
        except BaseException as error:  # noqa: BLE001 — recorded for the assertion below
            outcomes.append(error)

    binder = threading.Thread(target=bind)
    binder.start()
    try:
        binder.join(10)
        # A binding that waits for the in-flight model call is not a refusal, and
        # binding after it silently would be worse: it must say it is busy.
        assert not binder.is_alive()
    finally:
        provider.release.set()
        sender.join(10)
        binder.join(10)

    assert failures == []
    assert len(outcomes) == 1 and isinstance(outcomes[0], UserStrategyStateError)
    # The refused binding consumed nothing and overwrote nothing: the intent is
    # still the intent, and the approved call settled as its own record.
    held = strategies.get(strategy_id)
    assert held.draft.document() == INTENT_DRAFT
    assert held.changes == () and held.calls[-1]["status"] == "observed"
    assert len(provider.calls) == 1

    # Once nothing is in flight, the same operation binds normally.
    bound = strategies.bind_scenario(
        strategy_id, operation_id="bind-1", base_revision=1, scenario_id=SCENARIO
    )
    assert bound.draft.revision == 2 and bound.draft.scenario_id == SCENARIO
    assert bound.calls == held.calls
    assert len(provider.calls) == 1


def test_a_v1_record_without_an_intent_discriminator_still_reads_and_runs(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No bulk migration: the executable encoding written before this slice still works."""
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies, "A strategy written before the union existed")
    strategy_id = strategy.user_strategy_id
    path = tmp_path / "workspace" / "user-strategies" / f"{strategy_id}.json"
    document = cast(dict[str, Any], json.loads(path.read_bytes()))

    # The v1 executable record: the same fields, written before any discriminator
    # existed. This is what an older workspace still holds on disk.
    v1 = dict(document)
    v1["schema"] = "ea.user-strategy.v1"
    v1["draft"] = {key: value for key, value in document["draft"].items() if key != "kind"}
    v1["history"] = [
        {key: value for key, value in item.items() if key != "kind"}
        for item in cast(list[dict[str, Any]], document.get("history") or [])
    ]
    v1.pop("binding", None)
    assert v1["schema"] == "ea.user-strategy.v1"
    assert "kind" not in v1["draft"]
    path.write_bytes(json.dumps(v1, sort_keys=True, indent=2).encode())

    reopened = strategies.get(strategy_id)
    assert reopened.draft == strategy.draft
    assert reopened.draft.revision == 1 and reopened.draft.scenario_id == SCENARIO
    preview = strategies.preview_experiment(strategy_id)
    assert preview["draft_revision"] == 1 and preview["scenario_id"] == SCENARIO

    # The bound path over that record is unchanged: calls, proposals and revisions.
    provider.responses.append(_response([], text="A bound answer."))
    answered = strategies.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="Explain the draft",
        approved_budget=dict(CALL_BUDGET),
    )
    assert answered.calls[-1]["status"] == "observed"
    _updated, proposal = _propose(strategies, provider, answered, TWO_FIELDS, call_id="call-2")
    applied = strategies.apply_proposal(
        strategy_id, proposal_id=proposal["proposal_id"], operation_id="op-1"
    )
    assert applied.draft.revision == 2
    assert isinstance(applied.draft, StrategyDraft)
    assert applied.draft.parameters["target_quantity"] == "3"


def test_one_intent_binds_proposes_applies_and_runs_the_existing_experiment_path(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One scripted provider through the whole slice: clarify, bind, propose, run."""
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    strategy = _new_intent(strategies)
    strategy_id = strategy.user_strategy_id
    assert [item.user_strategy_id for item in strategies.list()] == [strategy_id]

    # 1. Clarification while unbound: one approved call with only read tools.
    provider.responses.append(_response([], text="Which registered scenario should this use?"))
    clarified = strategies.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="What is missing before this can run?",
        approved_budget=dict(CALL_BUDGET),
    )
    assert clarified.draft.document() == INTENT_DRAFT
    assert [tool.name for tool in provider.calls[-1]["tools"]] == [
        "discover_resources",
        "inspect_research_input",
    ]

    # 2. The user picks one exact scenario; the host validates it and advances once.
    bound = strategies.bind_scenario(
        strategy_id,
        operation_id="bind-1",
        base_revision=1,
        scenario_id=SCENARIO,
        criterion=CRITERION,
    )
    assert bound.draft.revision == 2 and bound.draft.scenario_id == SCENARIO
    assert len(provider.calls) == 1

    # 3. The bound path is the existing one: one typed diff, applied, running nothing.
    _updated, proposal = _propose(
        strategies, provider, bound, TWO_FIELDS, call_id="call-2", base_revision=2
    )
    applied = strategies.apply_proposal(
        strategy_id, proposal_id=proposal["proposal_id"], operation_id="op-1"
    )
    assert applied.draft.revision == 3
    assert isinstance(applied.draft, StrategyDraft)
    assert applied.draft.parameters["target_quantity"] == "3"
    assert service.list_jobs() == [] and _children(tmp_path) == []

    # 4. The existing confirmation path runs the real engine and publishes its report.
    experiment = _launch(strategies, strategy_id).experiments[-1]
    job_id = cast(str, experiment["job_id"])
    _terminal_job(service, job_id)
    assert service.get_job(job_id).status == "succeeded"
    report = json.loads(service.report(job_id))
    assert report["completion"]["terminal_outcome"] == "success"
    assert experiment["payload"]["criterion"] == FROZEN_CRITERION

    # One identity, one conversation and one call ledger from start to finish.
    kept = strategies.get(strategy_id)
    assert kept.user_strategy_id == strategy_id
    assert kept.intent == INTENT
    assert kept.messages == applied.messages
    assert [item["call_id"] for item in kept.calls] == ["call-1", "call-2"]
    assert kept.draft.revision == 3
    assert [item.user_strategy_id for item in strategies.list()] == [strategy_id]
    assert len(_children(tmp_path)) == 1 and len(service.list_jobs()) == 1


def test_http_saves_an_intent_binds_it_and_refuses_unbound_execution(tmp_path: Any) -> None:
    """The same contract at the HTTP boundary: strict bodies, real ids, real refusals."""
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        created = client.post(
            "/api/user-strategies",
            headers=WRITE_HEADERS,
            json={"name": "Idea first", "intent": INTENT},
        )
        assert created.status_code == 201, created.text
        unbound = cast(dict[str, Any], created.json())
        strategy_id = unbound["user_strategy_id"]
        assert unbound["draft"] == INTENT_DRAFT
        assert unbound["intent"] == INTENT
        assert unbound["messages"] == [] and unbound["calls"] == []
        assert unbound["experiments"] == [] and unbound["changes"] == []

        # The same identity reopens with the same intent revision.
        reloaded = cast(dict[str, Any], client.get(f"/api/user-strategies/{strategy_id}").json())
        assert reloaded["draft"] == INTENT_DRAFT and reloaded["intent"] == INTENT

        # Nothing executable exists yet, and every executable route says so itself.
        preview = client.get(f"/api/user-strategies/{strategy_id}/experiment/preview")
        assert preview.status_code == 422
        assert preview.json()["error"]["code"] == "user_strategy_invalid"
        apply = client.post(
            f"/api/user-strategies/{strategy_id}/proposals/proposal-1/apply",
            headers=WRITE_HEADERS,
            json={"operation_id": "op-1"},
        )
        assert apply.status_code == 422
        assert apply.json()["error"]["code"] == "user_strategy_invalid"
        launch = client.post(
            f"/api/user-strategies/{strategy_id}/experiments",
            headers=WRITE_HEADERS,
            json={
                "launch_intent_id": "li-1",
                "base_revision": 1,
                "preview_identity": "sha256:" + "0" * 64,
                "approved_budget": dict(RUN_BUDGET),
            },
        )
        assert launch.status_code == 422
        assert launch.json()["error"]["code"] == "user_strategy_invalid"

        # The binding body is strict, its scenario must be real, and its revision
        # must be the head the caller actually read.
        incomplete = client.post(
            f"/api/user-strategies/{strategy_id}/bind-scenario",
            headers=WRITE_HEADERS,
            json={"operation_id": "bind-1", "base_revision": 1},
        )
        assert incomplete.status_code == 422
        unknown = client.post(
            f"/api/user-strategies/{strategy_id}/bind-scenario",
            headers=WRITE_HEADERS,
            json={"operation_id": "bind-1", "base_revision": 1, "scenario_id": "missing.yaml"},
        )
        assert unknown.status_code == 422
        assert unknown.json()["error"]["code"] == "user_strategy_invalid"
        stale = client.post(
            f"/api/user-strategies/{strategy_id}/bind-scenario",
            headers=WRITE_HEADERS,
            json={"operation_id": "bind-1", "base_revision": 9, "scenario_id": SCENARIO},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "stale_base_revision"
        assert client.get(f"/api/user-strategies/{strategy_id}").json()["draft"] == INTENT_DRAFT

        body = {
            "operation_id": "bind-1",
            "base_revision": 1,
            "scenario_id": SCENARIO,
            "criterion": CRITERION,
        }
        bound = client.post(
            f"/api/user-strategies/{strategy_id}/bind-scenario",
            headers=WRITE_HEADERS,
            json=dict(body),
        )
        assert bound.status_code == 200, bound.text
        executable = cast(dict[str, Any], bound.json())
        assert executable["draft"]["revision"] == 2
        assert executable["draft"]["scenario_id"] == SCENARIO
        assert executable["draft"]["strategy_id"] == "bounded-long-v1"
        assert executable["draft"]["criterion"] == FROZEN_CRITERION
        assert executable["draft"]["parameters"]["target_quantity"] == "2"
        assert executable["user_strategy_id"] == strategy_id

        # The same operation replays as itself; a different request conflicts and a
        # second operation cannot rebind.
        replayed = client.post(
            f"/api/user-strategies/{strategy_id}/bind-scenario",
            headers=WRITE_HEADERS,
            json=dict(body),
        )
        assert replayed.status_code == 200
        assert replayed.json()["draft"] == executable["draft"]
        assert replayed.json()["changes"] == executable["changes"]
        conflict = client.post(
            f"/api/user-strategies/{strategy_id}/bind-scenario",
            headers=WRITE_HEADERS,
            json={**body, "scenario_id": "flat.yaml"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "operation_conflict"
        rebind = client.post(
            f"/api/user-strategies/{strategy_id}/bind-scenario",
            headers=WRITE_HEADERS,
            json={"operation_id": "bind-2", "base_revision": 2, "scenario_id": SCENARIO},
        )
        assert rebind.status_code == 422
        assert rebind.json()["error"]["code"] == "user_strategy_invalid"

        # The legacy bound create is unchanged and still revision 1.
        legacy = client.post(
            "/api/user-strategies",
            headers=WRITE_HEADERS,
            json={
                "name": "HTTP strategy",
                "intent": "The same scenario, bound at creation",
                "scenario_id": SCENARIO,
                "criterion": CRITERION,
            },
        )
        assert legacy.status_code == 201, legacy.text
        assert legacy.json()["draft"]["revision"] == 1
        assert legacy.json()["draft"]["scenario_id"] == SCENARIO
        assert legacy.json()["draft"]["criterion"] == FROZEN_CRITERION


# --- selected frozen evidence: the runs one turn may explain -------------------
#
# A conversation may be grounded in the runs the user selected, and in nothing
# else. A selection names one or two of this strategy's own created, readable,
# identity-matched frozen runs; the host reads and verifies each of them and
# retains that verified context on the call before the provider can be reached. A
# selection that is not one of them is refused before anything is written or
# sent, and the latest run is never silently substituted for the selected one.


def _two_runs(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[UserStrategyService, WebService, FakeProvider, str, dict[str, Any], dict[str, Any]]:
    """One strategy with two frozen runs: revision 1, then revision 2 of its draft."""
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    first = _launch(strategies, strategy_id).experiments[-1]
    _terminal_job(service, cast(str, first["job_id"]))
    _updated, proposal = _propose(strategies, provider, strategy, TWO_FIELDS, call_id="call-1")
    strategies.apply_proposal(strategy_id, proposal_id=proposal["proposal_id"], operation_id="op-1")
    second = _launch(strategies, strategy_id, launch_intent_id="li-2").experiments[-1]
    _terminal_job(service, cast(str, second["job_id"]))
    assert first["status"] == second["status"] == "created"
    assert first["payload"]["draft_revision"] == 1
    assert second["payload"]["draft_revision"] == 2
    return strategies, service, provider, strategy_id, first, second


def _opening(provider: FakeProvider, index: int = -1) -> str:
    """The one fact turn this owner opens a request with."""
    return cast(str, cast(list[Any], provider.calls[index]["messages"])[0].text)


#: One more typed change, proposed over selected evidence and applied by the user.
NEXT_FIELDS: list[dict[str, Any]] = [
    {
        "path": "parameters.target_quantity",
        "value": "4",
        "reason": "Test a larger size next.",
        "evidence": ["run: the selected frozen run"],
    },
]


def test_one_selected_frozen_run_is_the_evidence_the_turn_is_grounded_in(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run the user selected is read, verified and stated — and no other run is."""
    strategies, service, provider, strategy_id, first, second = _two_runs(tmp_path, monkeypatch)
    job_id = cast(str, first["job_id"])
    payload = cast(dict[str, Any], first["payload"])
    job = service.get_job(job_id)
    report = cast(dict[str, Any], json.loads(service.report(job_id)))

    provider.responses.append(_response([], text="A reading of the run you selected."))
    answered = strategies.send_message(
        strategy_id,
        call_id="call-2",
        base_revision=2,
        text="解释所选证据",
        approved_budget=dict(CALL_BUDGET),
        evidence_job_ids=[job_id],
    )
    assert answered.calls[-1]["status"] == "observed"
    assert answered.messages[-1]["text"] == "A reading of the run you selected."

    opening = _opening(provider)
    # The selected run is stated with its own frozen facts, and the run that was
    # never selected is not described at all — not even as a comparison.
    assert f"job {job_id}" in opening
    assert cast(str, second["job_id"]) not in opening
    assert "frozen draft revision 1" in opening
    assert payload["input_sha256"] in opening
    assert "Selected run A:" in opening and "Selected run B:" not in opening
    # A run's own frozen criterion is read as that run's, and the limits of one
    # window are stated with it so the turn is told what a reading is not.
    assert "its frozen criterion economics.net_pnl.amount is positive" in opening
    assert "one window" in opening and "not realized profit" in opening
    # Nothing here is the older reading of whichever run happened to be latest,
    # and the draft a proposal would still target is today's head revision.
    assert "Host-verified readings of this strategy's own frozen run" not in opening
    assert "Draft revision 2" in opening

    call = _durable(tmp_path, strategy_id).calls[-1]
    assert call["request"]["evidence_job_ids"] == [job_id]
    assert call["payload_sha256"]
    (record,) = cast(list[dict[str, Any]], call["evidence"])
    assert record["job_id"] == job_id
    assert record["launch_intent_id"] == "li-1"
    assert record["draft_revision"] == payload["draft_revision"] == 1
    assert record["run_id"] == job.engine_run_id
    assert record["report_sha256"] == job.report_sha256
    assert record["input_sha256"] == payload["input_sha256"]
    assert record["record_count"] == payload["record_count"]
    assert record["parameters"] == payload["parameters"]
    assert set(cast(dict[str, Any], record["assumptions"])) == {
        "strategy",
        "execution",
        "risk",
        "funding",
        "instrument",
        "data",
    }
    assert record["criterion"] == FROZEN_CRITERION
    reading = host_reading(FROZEN_CRITERION, report)
    assert record["reading"] == {"outcome": reading["outcome"], "inference": reading["inference"]}
    assert "backtest_only_no_live_execution" in cast(list[str], record["limitations"])
    # Every stated fact is the report's own value with the report's own currency.
    assert {
        "path": "economics.net_pnl.amount",
        "value": report["economics"]["net_pnl"]["amount"],
        "currency": report["economics"]["currency"],
    } in cast(list[dict[str, Any]], record["facts"])


def test_two_selected_runs_keep_each_runs_own_frozen_criterion(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two runs are one bounded pair, ordered A then B, each read as the run it was."""
    strategies, _service, provider, strategy_id, first, second = _two_runs(tmp_path, monkeypatch)
    jobs = [cast(str, first["job_id"]), cast(str, second["job_id"])]

    # The head draft moves on and now states a criterion the frozen runs were not
    # confirmed under. The two selected runs keep their own, whatever today's head
    # says: what was frozen is not rewritten by a later revision.
    store = UserStrategyStore(tmp_path / "workspace")
    saved = store.get(strategy_id)
    draft = cast(StrategyDraft, saved.draft)
    store.save(
        replace(
            saved,
            draft=replace(
                draft,
                revision=draft.revision + 1,
                criterion={"field": "economics.total_return.value", "expectation": "negative"},
            ),
        )
    )

    provider.responses.append(_response([], text="A reading of both runs."))
    answered = strategies.send_message(
        strategy_id,
        call_id="call-2",
        base_revision=3,
        text="解释所选证据",
        approved_budget=dict(CALL_BUDGET),
        evidence_job_ids=jobs,
    )
    assert answered.calls[-1]["status"] == "observed"

    opening = _opening(provider)
    assert opening.count("Selected run A:") == 1 and opening.count("Selected run B:") == 1
    assert opening.index(jobs[0]) < opening.index(jobs[1])
    assert "frozen draft revision 1" in opening and "frozen draft revision 2" in opening
    assert "its frozen criterion economics.net_pnl.amount is positive" in opening
    assert "its frozen criterion economics.total_return.value" not in opening
    # The head draft's own criterion is still stated as the draft's, so the turn is
    # in no doubt about which draft a proposal would be made against.
    assert "The user's stated criterion: economics.total_return.value is negative" in opening
    assert "Draft revision 3" in opening

    records = cast(list[dict[str, Any]], _durable(tmp_path, strategy_id).calls[-1]["evidence"])
    assert [item["job_id"] for item in records] == jobs
    assert [item["draft_revision"] for item in records] == [1, 2]
    assert records[0]["input_sha256"] == first["payload"]["input_sha256"]
    assert records[1]["input_sha256"] == second["payload"]["input_sha256"]
    assert records[0]["report_sha256"] != records[1]["report_sha256"]
    assert records[0]["criterion"] == records[1]["criterion"] == FROZEN_CRITERION


def test_continuity_a_selected_pair_is_the_only_evidence_the_turn_is_grounded_in(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pair the user named is the whole of what this turn is told about runs.

    Each selected run is stated as itself, in the user's own order, with its own
    frozen parameters, revision and limits. A difference between two runs is a
    comparability question before it is an explanation, and a report that records
    no benchmark comparison says something about that run alone: it is not by
    itself a finding that the selected runs cannot be compared. An ordinary answer
    that calls no tool is handled as any other turn is and adds no proposal, so the
    turn is one new call and the user's own message and nothing else.
    """
    strategies, _service, provider, strategy_id, first, second = _two_runs(tmp_path, monkeypatch)
    # The user names the later run as A and the earlier one as B: what the request
    # states is what the model is told, which is not the order they ran in.
    jobs = [cast(str, second["job_id"]), cast(str, first["job_id"])]
    asked = len(provider.calls)
    before = _durable(tmp_path, strategy_id)
    assert before.proposals

    provider.responses.append(_response([], text="一个普通的回答。"))
    answered = strategies.send_message(
        strategy_id,
        call_id="call-2",
        base_revision=2,
        text="解释这两次运行",
        approved_budget=dict(CALL_BUDGET),
        evidence_job_ids=jobs,
    )
    assert len(provider.calls) == asked + 1
    # An ordinary answer with no tool call is handled as any other turn is: the call
    # is observed, its text is the answer, and nothing is proposed or applied.
    assert answered.calls[-1]["status"] == "observed"
    assert answered.messages[-1]["text"] == "一个普通的回答。"
    assert answered.proposals == before.proposals
    # What is added is the tail of the history — the user's own turn and its reply —
    # and nothing else. The messages this strategy already had, this fixture's
    # earlier proposal among them, are exactly the messages it still has.
    assert answered.messages[: len(before.messages)] == before.messages
    appended = answered.messages[len(before.messages) :]
    assert all(item["kind"] != "proposal" for item in appended)
    assert answered.draft.revision == 2
    # The turn is one new call and the user's own turn: nothing else was added, and
    # the history this strategy already had is still exactly that history.
    assert len(answered.calls) == len(before.calls) + 1
    assert len(answered.messages) == len(before.messages) + 2
    assert len(appended) == 2
    assert [item["job_id"] for item in answered.calls[-1]["evidence"]] == jobs

    opening = _opening(provider)
    lines = opening.split("\n")
    a_line = next(line for line in lines if line.startswith("Selected run A:"))
    b_line = next(line for line in lines if line.startswith("Selected run B:"))
    assert f"job {jobs[0]}" in a_line
    assert f"frozen draft revision {second['payload']['draft_revision']}, launch li-2" in a_line
    assert f"frozen draft revision {first['payload']['draft_revision']}, launch li-1" in b_line
    assert lines.index(a_line) < lines.index(b_line)
    # Each run is stated as itself: neither line carries the other run's job, and
    # each run's own frozen parameters are stated with it, in the selected order.
    assert jobs[1] not in a_line and jobs[0] not in b_line
    parameters = [line for line in lines if line.strip().startswith("the parameters it froze:")]
    assert len(parameters) == 2
    # Each line carries the run it belongs to, read back out of the document that run
    # was frozen from: the two runs really did freeze different values, so neither
    # line can be the other's.
    first_frozen = cast(dict[str, Any], first["payload"]["parameters"])
    second_frozen = cast(dict[str, Any], second["payload"]["parameters"])
    assert second_frozen["target_quantity"] != first_frozen["target_quantity"]
    assert f"target_quantity = {second_frozen['target_quantity']}" in parameters[0]
    assert f"target_quantity = {first_frozen['target_quantity']}" in parameters[1]
    # A verified reading of each run is stated, with that run's own input digest.
    assert opening.count("economics.net_pnl.amount = ") == 2
    assert cast(str, second["payload"]["input_sha256"]) in opening
    assert cast(str, first["payload"]["input_sha256"]) in opening
    assert "no other run of this strategy is described" in opening
    assert "a difference between two runs is a comparability question" in opening
    assert "no_benchmark_comparison" in opening
    assert "not by itself" in opening
    assert "One run over one window supports no causal claim" in opening
    assert "Net P&L includes the valuation of any position still open" in opening
    assert "are not running anything" in opening
    # The draft a proposal would still target is today's head, not what was frozen.
    assert "Draft revision 2" in opening

    # A selection this strategy did not freeze is refused before anything is asked,
    # and leaves no call record and no second request behind it.
    after = _durable(tmp_path, strategy_id)
    with pytest.raises(UserStrategyStateError):
        strategies.send_message(
            strategy_id,
            call_id="call-3",
            base_revision=2,
            text="解释一次不属于这条策略的运行",
            approved_budget=dict(CALL_BUDGET),
            evidence_job_ids=["job-elsewhere"],
        )
    assert len(provider.calls) == asked + 1
    assert _durable(tmp_path, strategy_id).calls == after.calls


@pytest.mark.parametrize(
    "case",
    ["foreign", "duplicate", "too_many", "empty", "not_an_id", "unknown", "not_created"],
)
def test_evidence_that_is_not_a_created_run_of_this_strategy_is_refused_before_dispatch(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """A selection that is not this strategy's own readable frozen run asks nothing."""
    strategies, service, provider, strategy_id, first, second = _two_runs(tmp_path, monkeypatch)
    baseline = len(provider.calls)
    first_id, second_id = cast(str, first["job_id"]), cast(str, second["job_id"])
    other = _create(strategies, "Another strategy entirely")
    foreign = cast(
        str,
        _launch(strategies, other.user_strategy_id, launch_intent_id="li-1").experiments[-1][
            "job_id"
        ],
    )
    _terminal_job(service, foreign)
    assert len(service.list_jobs()) == 3

    if case == "not_created":
        # The second experiment is no longer a created run, so the id it named is not
        # evidence this strategy holds — whatever the job index still carries.
        store = UserStrategyStore(tmp_path / "workspace")
        saved = store.get(strategy_id)
        store.save(
            replace(
                saved,
                experiments=tuple(
                    {**item, "status": "pending", "job_id": None}
                    if item["launch_intent_id"] == "li-2"
                    else item
                    for item in saved.experiments
                ),
            )
        )

    selection: dict[str, list[str]] = {
        "foreign": [foreign],
        "duplicate": [first_id, first_id],
        "too_many": [first_id, second_id, foreign],
        "empty": [],
        "not_an_id": ["not a valid id"],
        "unknown": ["job-that-never-existed"],
        "not_created": [second_id],
    }
    provider.responses.append(_response([], text="An answer that must never be asked for."))
    with pytest.raises(UserStrategyStateError):
        strategies.send_message(
            strategy_id,
            call_id="call-2",
            base_revision=2,
            text="解释所选证据",
            approved_budget=dict(CALL_BUDGET),
            evidence_job_ids=selection[case],
        )
    # The refusal is complete and free: no call, no message, no proposal, no ask —
    # the record this strategy already had is exactly the record it still has.
    before = _durable(tmp_path, strategy_id)
    provider.responses.append(_response([], text="An answer that must never be asked for."))
    with pytest.raises(UserStrategyStateError):
        strategies.send_message(
            strategy_id,
            call_id="call-2",
            base_revision=2,
            text="解释所选证据",
            approved_budget=dict(CALL_BUDGET),
            evidence_job_ids=selection[case],
        )
    durable = _durable(tmp_path, strategy_id)
    assert durable.calls == before.calls and before.calls
    assert durable.messages == before.messages
    assert durable.proposals == before.proposals
    assert durable.experiments == before.experiments
    assert len(provider.calls) == baseline
    assert len(service.list_jobs()) == 3


def test_a_selected_run_whose_report_cannot_be_verified_is_refused_rather_than_guessed(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable, altered or other-run report is a refusal, never a reading."""
    strategies, service, provider, strategy_id, first, _second = _two_runs(tmp_path, monkeypatch)
    job_id = cast(str, first["job_id"])
    baseline = len(provider.calls)
    real_report = service.report
    real_job = service.get_job(job_id)
    mode = {"kind": "unreadable"}
    before = _durable(tmp_path, strategy_id)

    def report(identifier: str) -> bytes:
        if mode["kind"] == "unreadable":
            raise OSError("the report store cannot be read")
        return real_report(identifier) + b" "

    class AnotherRun:
        """The same durable job, claiming a run this report does not belong to."""

        engine_run_id = "another-run-entirely"

        def __getattr__(self, name: str) -> Any:
            return getattr(real_job, name)

    def attempt(call_id: str) -> None:
        with pytest.raises(UserStrategyStateError):
            strategies.send_message(
                strategy_id,
                call_id=call_id,
                base_revision=2,
                text="解释所选证据",
                approved_budget=dict(CALL_BUDGET),
                evidence_job_ids=[job_id],
            )

    monkeypatch.setattr(service, "report", report)
    attempt("call-unreadable")
    mode["kind"] = "changed_bytes"
    attempt("call-changed-report")
    monkeypatch.setattr(service, "get_job", lambda identifier: AnotherRun())
    attempt("call-another-run")

    durable = _durable(tmp_path, strategy_id)
    assert durable.calls == before.calls and before.calls
    assert durable.messages == before.messages
    assert durable.proposals == before.proposals
    assert len(provider.calls) == baseline


def test_a_selected_run_that_has_not_succeeded_has_no_evidence_to_explain(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run without a verified report is not evidence, so the turn is not told it is."""
    strategies, service, provider, strategy_id, first, _second = _two_runs(tmp_path, monkeypatch)
    job_id = cast(str, first["job_id"])
    baseline = len(provider.calls)

    class Running:
        status = "running"
        report_ready = False

    before = _durable(tmp_path, strategy_id)
    monkeypatch.setattr(service, "get_job", lambda identifier: Running())
    with pytest.raises(UserStrategyStateError):
        strategies.send_message(
            strategy_id,
            call_id="call-2",
            base_revision=2,
            text="解释所选证据",
            approved_budget=dict(CALL_BUDGET),
            evidence_job_ids=[job_id],
        )
    durable = _durable(tmp_path, strategy_id)
    assert durable.calls == before.calls and before.calls
    assert durable.messages == before.messages
    assert len(provider.calls) == baseline


def test_the_verified_evidence_is_durable_before_the_provider_is_reached(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frozen context is on the record before dispatch, so a crash cannot lose it."""
    strategies, service, _provider, strategy_id, first, _second = _two_runs(tmp_path, monkeypatch)
    job_id = cast(str, first["job_id"])
    provider = RoutedBlockingProvider([_response([], text="A reading of the selected run.")])
    cast(Any, strategies)._provider = provider

    failures: list[BaseException] = []

    def run() -> None:
        try:
            strategies.send_message(
                strategy_id,
                call_id="call-2",
                base_revision=2,
                text="解释所选证据",
                approved_budget=dict(CALL_BUDGET),
                evidence_job_ids=[job_id],
            )
        except BaseException as error:  # noqa: BLE001 — recorded for the assertion below
            failures.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert provider.invoked.wait(10)

    # While the provider is still blocked, the verified context is already durable.
    call = _durable(tmp_path, strategy_id).calls[-1]
    assert call["status"] == "dispatched"
    assert call["request"]["evidence_job_ids"] == [job_id]
    (record,) = cast(list[dict[str, Any]], call["evidence"])
    assert record["job_id"] == job_id
    assert record["report_sha256"] == service.get_job(job_id).report_sha256
    assert record["input_sha256"] == first["payload"]["input_sha256"]

    provider.release.set()
    thread.join(10)
    assert failures == []
    settled = _durable(tmp_path, strategy_id).calls[-1]
    assert settled["status"] == "observed"
    assert settled["evidence"] == call["evidence"]


def test_a_replayed_evidence_call_answers_from_its_record_and_a_changed_selection_conflicts(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The selection is part of the call identity, and a replay rereads nothing."""
    strategies, service, provider, strategy_id, first, second = _two_runs(tmp_path, monkeypatch)
    jobs = [cast(str, first["job_id"]), cast(str, second["job_id"])]
    provider.responses.append(_response([], text="One reading of A."))
    answered = strategies.send_message(
        strategy_id,
        call_id="call-2",
        base_revision=2,
        text="解释所选证据",
        approved_budget=dict(CALL_BUDGET),
        evidence_job_ids=[jobs[0]],
    )
    baseline = len(provider.calls)
    assert answered.calls[-1]["status"] == "observed"

    def send(selection: list[str] | None) -> Any:
        return strategies.send_message(
            strategy_id,
            call_id="call-2",
            base_revision=2,
            text="解释所选证据",
            approved_budget=dict(CALL_BUDGET),
            evidence_job_ids=selection,
        )

    # The same request under the same id is that call, answered from its record.
    replayed = send([jobs[0]])
    assert replayed.calls == answered.calls and replayed.messages == answered.messages
    assert len(provider.calls) == baseline
    # Another selection, or none at all, is a different request under that id.
    for selection in ([jobs[0], jobs[1]], [jobs[1]], None):
        with pytest.raises(UserStrategyConflictError):
            send(selection)
    assert len(provider.calls) == baseline

    # A replay reads its own recorded answer: evidence that is unreadable now is
    # never reread, and the persisted result is what it was.
    def never_reread(identifier: str) -> bytes:
        raise AssertionError("a replay reread mutable evidence")

    monkeypatch.setattr(service, "report", never_reread)
    again = send([jobs[0]])
    assert again.calls == answered.calls and again.messages == answered.messages
    assert len(provider.calls) == baseline


def test_applying_a_proposal_made_over_selected_evidence_leaves_the_runs_untouched(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next experiment is proposed against today's draft; the frozen runs stay frozen."""
    strategies, service, provider, strategy_id, first, _second = _two_runs(tmp_path, monkeypatch)
    job_id = cast(str, first["job_id"])
    provider.responses.append(_change(NEXT_FIELDS, call_id="call-2"))
    answered = strategies.send_message(
        strategy_id,
        call_id="call-2",
        base_revision=2,
        text="提出下一实验",
        approved_budget=dict(CALL_BUDGET),
        evidence_job_ids=[job_id],
    )
    proposal = answered.proposals[-1]
    assert proposal["call_id"] == "call-2"
    assert proposal["items"][0]["path"] == "parameters.target_quantity"

    before = _durable(tmp_path, strategy_id)
    assert cast(list[dict[str, Any]], before.calls[-1]["evidence"])[0]["job_id"] == job_id
    applied = strategies.apply_proposal(
        strategy_id, proposal_id=proposal["proposal_id"], operation_id="op-2"
    )
    assert applied.draft.revision == 3
    assert isinstance(applied.draft, StrategyDraft)
    assert applied.draft.parameters["target_quantity"] == "4"
    # Applying writes one revision of the draft and nothing else: the frozen runs,
    # the retained evidence and the run index are exactly what they were.
    after = _durable(tmp_path, strategy_id)
    assert after.experiments == before.experiments
    assert after.calls == before.calls
    assert after.calls[-1]["evidence"] == before.calls[-1]["evidence"]
    assert applied.experiments == before.experiments
    assert len(service.list_jobs()) == 2


def test_an_intent_has_no_evidence_and_refuses_a_selection_before_any_dispatch(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An intent has no frozen run, so it has nothing to explain and asks nothing."""
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    strategy = _new_intent(strategies)
    with pytest.raises(UserStrategyStateError):
        strategies.send_message(
            strategy.user_strategy_id,
            call_id="call-1",
            base_revision=1,
            text="解释所选证据",
            approved_budget=dict(CALL_BUDGET),
            evidence_job_ids=["job-that-never-existed"],
        )
    refused = strategies.get(strategy.user_strategy_id)
    assert refused.calls == () and refused.messages == ()
    assert provider.calls == [] and service.list_jobs() == [] and _children(tmp_path) == []


# --- revising a proposal that was made over selected evidence -----------------
#
# A proposal made over two runs the user selected is a question about those runs.
# Asking to clarify or refine it is that same question about the same evidence, so
# the new call is grounded in exactly the pair the original proposal was made over
# — in the order the user named them, A then B — read back from this strategy's own
# durable record and re-verified against the frozen runs before the provider can be
# reached. It is never re-grounded in whichever run happens to be latest by then.
# A retained record that no longer holds that pair together is refused whole: the
# open proposal, the draft, the calls and the messages stay exactly as they were,
# and no provider is contacted.


def _observed_turn(
    strategies: UserStrategyService,
    provider: FakeProvider,
    strategy_id: str,
    *,
    call_id: str,
    base_revision: int,
) -> None:
    """One ordinary observed turn: a plain answer, no tool call and no proposal."""
    provider.responses.append(_response([], text="An ordinary reading."))
    strategies.send_message(
        strategy_id,
        call_id=call_id,
        base_revision=base_revision,
        text="What does this draft do?",
        approved_budget=dict(CALL_BUDGET),
    )


def _grounded_proposal(
    strategies: UserStrategyService,
    provider: FakeProvider,
    strategy_id: str,
    selected: list[str],
    *,
    call_id: str = "call-2",
    base_revision: int = 2,
) -> dict[str, Any]:
    """One open proposal made over the runs the user selected, in the order given."""
    provider.responses.append(_change(NEXT_FIELDS, call_id=call_id))
    updated = strategies.send_message(
        strategy_id,
        call_id=call_id,
        base_revision=base_revision,
        text="Propose the next experiment over the selected runs",
        approved_budget=dict(CALL_BUDGET),
        evidence_job_ids=list(selected),
    )
    return updated.proposals[-1]


def _scripted_revise(
    strategies: UserStrategyService,
    provider: FakeProvider,
    strategy_id: str,
    proposal_id: str,
    *,
    call_id: str = "call-3",
    text: str = "Refine the proposed change",
) -> Any:
    """One scripted refinement: the model answers with one more typed diff."""
    provider.responses.append(_change(NEXT_FIELDS, call_id=call_id))
    return strategies.revise_proposal(
        strategy_id,
        proposal_id=proposal_id,
        call_id=call_id,
        text=text,
        approved_budget=dict(CALL_BUDGET),
    )


def _damage_originating_call(
    tmp_path: Any,
    strategy_id: str,
    proposal_id: str,
    case: str,
    *,
    call_id: str = "call-2",
    impersonator: str = "call-9",
) -> None:
    """One retained record edited the way a damaged store would read back.

    Every case edits only this strategy's own durable document — the proposal, the
    call its ``call_id`` names, that call's request/evidence pair, or the recorded
    model message — so each case is a record this strategy would really read, never a
    test-only second owner.
    """
    store = UserStrategyStore(tmp_path / "workspace")
    saved = store.get(strategy_id)
    calls = [dict(item) for item in saved.calls]
    proposals = [dict(item) for item in saved.proposals]
    messages = [dict(item) for item in saved.messages]
    index = next(i for i, item in enumerate(calls) if item.get("call_id") == call_id)
    if case == "missing_call":
        calls = [item for item in calls if item.get("call_id") != call_id]
    elif case == "ambiguous_call":
        calls.insert(index, dict(calls[index]))
    elif case == "mislabelled_call":
        proposals = [
            {**item, "call_id": "call-1"} if item.get("proposal_id") == proposal_id else item
            for item in proposals
        ]
    elif case == "impersonating_call":
        # A genuine observed call at the same revision that did not produce this
        # proposal: only the recorded proposal/call linkage can tell them apart.
        proposals = [
            {**item, "call_id": impersonator} if item.get("proposal_id") == proposal_id else item
            for item in proposals
        ]
    elif case == "stripped_selection":
        # Both members of the originally grounded pair removed together, with the
        # call's recorded request digest left in place: the digest no longer hashes
        # to the request that is stored, so this is not a legacy ungrounded proposal.
        request = cast(dict[str, Any], calls[index]["request"])
        stripped = {key: value for key, value in calls[index].items() if key != "evidence"}
        calls[index] = {
            **stripped,
            "request": {key: value for key, value in request.items() if key != "evidence_job_ids"},
        }
    elif case == "corrupted_request_digest":
        calls[index] = {**calls[index], "payload_sha256": "0" * 64}
    elif case == "missing_request_digest":
        calls[index] = {
            key: value for key, value in calls[index].items() if key != "payload_sha256"
        }
    elif case == "unobserved_source":
        # The request and its evidence are intact, but the call that kept them never
        # records an observed answer, so it records no proposal either.
        calls[index] = {**calls[index], "status": "unknown"}
    elif case == "foreign_role_message":
        # The recorded model message this proposal is read back from is not one the
        # observed settle path could have written.
        messages = [
            {**item, "role": "user"}
            if item.get("kind") == "proposal" and item.get("proposal_id") == proposal_id
            else item
            for item in messages
        ]
    elif case == "request_without_evidence":
        request = cast(dict[str, Any], calls[index]["request"])
        calls[index] = {
            **calls[index],
            "request": {key: value for key, value in request.items() if key != "evidence_job_ids"},
        }
    elif case == "evidence_without_request":
        calls[index] = {key: value for key, value in calls[index].items() if key != "evidence"}
    elif case == "reordered_evidence":
        calls[index] = {
            **calls[index],
            "evidence": list(reversed(cast(list[Any], calls[index]["evidence"]))),
        }
    elif case == "altered_evidence_job":
        records = [dict(item) for item in cast(list[Any], calls[index]["evidence"])]
        records[0]["job_id"] = "job-elsewhere"
        calls[index] = {**calls[index], "evidence": records}
    elif case == "conflicting_revision":
        proposals = [
            {**item, "base_revision": 1} if item.get("proposal_id") == proposal_id else item
            for item in proposals
        ]
    else:  # pragma: no cover - a case this helper does not own
        raise AssertionError(f"unknown damage case {case}")
    store.save(
        replace(saved, calls=tuple(calls), proposals=tuple(proposals), messages=tuple(messages))
    )


def _unreadable_report(identifier: str) -> bytes:
    """A report store that cannot be read at all."""
    raise OSError("the report store cannot be read")


def _damage_frozen_run(tmp_path: Any, strategy_id: str, case: str) -> None:
    """One frozen run this strategy kept, edited so it is no longer that run."""
    store = UserStrategyStore(tmp_path / "workspace")
    saved = store.get(strategy_id)
    experiments = [dict(item) for item in saved.experiments]
    index = next(i for i, item in enumerate(experiments) if item.get("launch_intent_id") == "li-2")
    if case == "not_created":
        experiments[index] = {**experiments[index], "status": "pending", "job_id": None}
    elif case == "stale_payload":
        payload = cast(dict[str, Any], experiments[index]["payload"])
        experiments[index] = {
            **experiments[index],
            "payload": {**payload, "record_count": int(payload["record_count"]) + 1},
        }
    else:  # pragma: no cover - a case this helper does not own
        raise AssertionError(f"unknown frozen-run damage {case}")
    store.save(replace(saved, experiments=tuple(experiments)))


def test_revise_evidence_inherits_the_ordered_pair_the_proposal_was_made_over(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refinement of a grounded proposal keeps that proposal's own pair, in order.

    The proposal was made over two runs the user selected as A and B in deliberately
    reverse chronological order, which is not the order the runs themselves happened
    in. Refining it is the same question about the same evidence: the new call's
    request and its host-verified evidence are that ordered pair, read from this
    strategy's own record and re-verified against the frozen runs, and the new
    proposal targets today's draft revision. The original proposal is superseded as
    any revise supersedes it, and nothing is applied or run.
    """
    strategies, service, provider, strategy_id, first, second = _two_runs(tmp_path, monkeypatch)
    pair = [cast(str, second["job_id"]), cast(str, first["job_id"])]
    proposal = _grounded_proposal(strategies, provider, strategy_id, pair)
    assert proposal["call_id"] == "call-2" and proposal["base_revision"] == 2
    jobs_before = len(service.list_jobs())
    changes_before = len(_durable(tmp_path, strategy_id).changes)
    calls_before = len(provider.calls)

    revised = _scripted_revise(strategies, provider, strategy_id, proposal["proposal_id"])

    # One approval is exactly one provider call, and a revise runs nothing.
    assert len(provider.calls) == calls_before + 1
    # Normal revise semantics: one new open proposal against today's revision, the
    # original superseded, and the draft itself untouched.
    assert revised.draft.revision == 2
    superseded = next(
        item for item in revised.proposals if item["proposal_id"] == proposal["proposal_id"]
    )
    assert superseded["status"] == "superseded"
    new_proposal = revised.proposals[-1]
    assert new_proposal["status"] == "open"
    assert new_proposal["call_id"] == "call-3" and new_proposal["base_revision"] == 2

    call = revised.calls[-1]
    assert call["call_id"] == "call-3"
    assert call["request"]["action"] == "revise"
    assert call["request"]["proposal_id"] == proposal["proposal_id"]
    assert call["request"]["evidence_job_ids"] == pair
    assert [item["job_id"] for item in call["evidence"]] == pair
    # Each inherited record is that run's own frozen facts, not a later run's.
    for record, run in zip(call["evidence"], (second, first), strict=True):
        payload = cast(dict[str, Any], run["payload"])
        assert record["draft_revision"] == payload["draft_revision"]
        assert record["input_sha256"] == payload["input_sha256"]
        assert record["parameters"] == payload["parameters"]
        assert record["criterion"] == FROZEN_CRITERION
        assert record["report_sha256"] == service.get_job(cast(str, run["job_id"])).report_sha256

    # The one request the model was sent states both runs, in the pair's own order,
    # and states no other run in their place.
    opening = _opening(provider)
    assert opening.count("Selected run A:") == 1 and opening.count("Selected run B:") == 1
    assert opening.index(pair[0]) < opening.index(pair[1])
    assert "no other run of this strategy is described" in opening
    assert "Host-verified readings of this strategy's own frozen run" not in opening
    assert "Draft revision 2" in opening

    # The persisted state a later process reads back holds the same pair.
    durable = _durable(tmp_path, strategy_id)
    assert durable.calls[-1]["request"]["evidence_job_ids"] == pair
    assert [item["job_id"] for item in durable.calls[-1]["evidence"]] == pair
    # A revise is a model call and no more: nothing was applied and nothing ran.
    assert len(durable.changes) == changes_before
    assert len(durable.experiments) == 2
    assert len(service.list_jobs()) == jobs_before


def test_revise_evidence_keeps_a_single_selected_run_as_the_whole_of_the_evidence(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One selected run stays one run: a refinement never invents a second report."""
    strategies, _service, provider, strategy_id, first, _second = _two_runs(tmp_path, monkeypatch)
    single = [cast(str, first["job_id"])]
    proposal = _grounded_proposal(strategies, provider, strategy_id, single)

    revised = _scripted_revise(strategies, provider, strategy_id, proposal["proposal_id"])

    call = revised.calls[-1]
    assert call["request"]["evidence_job_ids"] == single
    assert [item["job_id"] for item in call["evidence"]] == single
    opening = _opening(provider)
    assert "Selected run A:" in opening and "Selected run B:" not in opening
    assert single[0] in opening


def test_revise_evidence_is_not_invented_for_an_ordinary_proposal(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proposal made with no selection keeps revising with none.

    An ordinary proposal has no evidence selection and no frozen context, which is a
    different thing from a grounded proposal whose provenance is damaged: revising
    it asks for no run at all, and a run is never forced on it.
    """
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy_id = strategy.user_strategy_id
    _strategy, proposal = _propose(strategies, provider, strategy, TWO_FIELDS, call_id="call-1")
    assert "evidence_job_ids" not in _durable(tmp_path, strategy_id).calls[-1]["request"]

    revised = _scripted_revise(strategies, provider, strategy_id, proposal["proposal_id"])

    new_call = revised.calls[-1]
    assert "evidence_job_ids" not in new_call["request"]
    assert "evidence" not in new_call
    assert revised.draft.revision == 1
    new_proposal = revised.proposals[-1]
    assert new_proposal["status"] == "open" and new_proposal["base_revision"] == 1
    superseded = next(
        item for item in revised.proposals if item["proposal_id"] == proposal["proposal_id"]
    )
    assert superseded["status"] == "superseded"
    assert "Host-verified readings" not in _opening(provider)


@pytest.mark.parametrize(
    "case",
    [
        "missing_call",
        "ambiguous_call",
        "mislabelled_call",
        "impersonating_call",
        "stripped_selection",
        "corrupted_request_digest",
        "missing_request_digest",
        "unobserved_source",
        "foreign_role_message",
        "request_without_evidence",
        "evidence_without_request",
        "reordered_evidence",
        "altered_evidence_job",
        "conflicting_revision",
    ],
)
def test_revise_evidence_refuses_a_record_that_no_longer_holds_the_pair_together(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """A refinement is grounded through the proposal's own originating call, or not at all.

    The pair is inherited through the proposal -> its unique originating call of this
    same strategy -> that call's own recorded request and verified evidence, and the
    two sides of that record have to agree with the proposal. An originating call
    that is gone or is present twice, a proposal whose ``call_id`` no longer names
    the call that produced it — whether that is an older call or a genuine observed
    call at the same revision that produced no proposal — a request and evidence
    pair with one side missing, an evidence order or identity that disagrees with
    the request, and a proposal whose recorded revision disagrees with its call are
    each refused whole rather than silently re-grounded in some other run or in no
    run at all.

    The originating call is a source only while it is still the request it was
    recorded under and an answer it actually observed. Both members of the pair
    removed together leave the request no longer hashing to the digest the call kept
    for it; a digest that is corrupted or gone leaves nothing to hash against; a
    call whose recorded outcome is not an observed answer, and a recorded model
    message the observed settle path could not have written, are sources that never
    produced this proposal. Each of those is a damaged record rather than an
    ordinary ungrounded proposal, so none may quietly become a refinement grounded
    in this strategy's latest run: nothing is written and no provider is contacted.
    """
    strategies, service, provider, strategy_id, first, second = _two_runs(tmp_path, monkeypatch)
    pair = [cast(str, second["job_id"]), cast(str, first["job_id"])]
    proposal = _grounded_proposal(strategies, provider, strategy_id, pair)
    # One more genuine, observed turn at the same revision that produced no proposal.
    _observed_turn(strategies, provider, strategy_id, call_id="call-9", base_revision=2)
    _damage_originating_call(tmp_path, strategy_id, proposal["proposal_id"], case)
    baseline = len(provider.calls)
    jobs_before = len(service.list_jobs())
    before = _durable(tmp_path, strategy_id)

    with pytest.raises(UserStrategyStateError):
        _scripted_revise(strategies, provider, strategy_id, proposal["proposal_id"])

    after = _durable(tmp_path, strategy_id)
    assert after.calls == before.calls and before.calls
    assert after.proposals == before.proposals
    assert after.messages == before.messages
    assert after.draft == before.draft
    assert after.history == before.history
    assert after.experiments == before.experiments
    # The original proposal stands rather than being superseded by a refine that was
    # refused before it could be admitted.
    still_open = next(
        item for item in after.proposals if item["proposal_id"] == proposal["proposal_id"]
    )
    assert still_open["status"] == "open"
    assert len(provider.calls) == baseline
    assert len(service.list_jobs()) == jobs_before


@pytest.mark.parametrize(
    "case",
    [
        "unreadable_report",
        "changed_report",
        "another_run",
        "not_succeeded",
        "not_created",
        "stale_payload",
        "republished_report",
    ],
)
def test_revise_evidence_reverifies_the_frozen_runs_before_any_new_dispatch(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """A refinement re-proves its own pair on the frozen runs, and never guesses.

    The pair is read again from this strategy's own record, and before a new call
    exists each run is held to its frozen payload, its job and that job's report. A
    report that cannot be read, a report that is no longer the run it was read for,
    a run that is no longer the run it names or no longer succeeded, a frozen
    payload that no longer hashes to what was recorded, a run that is no longer one
    this strategy created, and a report that has since been re-published under a
    different digest are all refused whole. The open proposal, the draft, the calls
    and the messages stay exactly as they were, nothing is silently re-grounded in a
    single latest run or in a newer reading of the same job, and no provider is
    contacted.
    """
    strategies, service, provider, strategy_id, first, second = _two_runs(tmp_path, monkeypatch)
    pair = [cast(str, second["job_id"]), cast(str, first["job_id"])]
    proposal = _grounded_proposal(strategies, provider, strategy_id, pair)
    baseline = len(provider.calls)
    real_job = service.get_job(pair[0])
    real_report = service.report
    real_jobs = {job_id: service.get_job(job_id) for job_id in pair}

    class AnotherRun:
        """The same durable job, claiming a run this report does not belong to."""

        engine_run_id = "another-run-entirely"

        def __getattr__(self, name: str) -> Any:
            return getattr(real_job, name)

    class Running:
        status = "running"
        report_ready = False

    class Republished:
        """The same durable job, publishing the digest of a re-published report."""

        def __init__(self, digest: str) -> None:
            self.report_sha256 = digest

        def __getattr__(self, name: str) -> Any:
            return getattr(real_job, name)

    if case == "unreadable_report":
        monkeypatch.setattr(service, "report", _unreadable_report)
    elif case == "changed_report":
        monkeypatch.setattr(service, "report", lambda identifier: real_report(identifier) + b" ")
    elif case == "another_run":
        monkeypatch.setattr(service, "get_job", lambda identifier: AnotherRun())
    elif case == "not_succeeded":
        monkeypatch.setattr(service, "get_job", lambda identifier: Running())
    elif case == "republished_report":
        # Today's report for that job is internally valid — it hashes to the digest
        # the job now publishes — but it is not the report the original call recorded,
        # so it may not silently replace the evidence that call was grounded in.
        today = real_report(pair[0]) + b"\n"
        monkeypatch.setattr(
            service,
            "report",
            lambda identifier: today if identifier == pair[0] else real_report(identifier),
        )
        monkeypatch.setattr(
            service,
            "get_job",
            lambda identifier: (
                Republished(hashlib.sha256(today).hexdigest())
                if identifier == pair[0]
                else real_jobs[identifier]
            ),
        )
    else:
        _damage_frozen_run(tmp_path, strategy_id, case)

    before = _durable(tmp_path, strategy_id)
    with pytest.raises(UserStrategyStateError):
        _scripted_revise(strategies, provider, strategy_id, proposal["proposal_id"])

    after = _durable(tmp_path, strategy_id)
    assert after.calls == before.calls and before.calls
    assert after.proposals == before.proposals
    assert after.messages == before.messages
    assert after.draft == before.draft
    still_open = next(
        item for item in after.proposals if item["proposal_id"] == proposal["proposal_id"]
    )
    assert still_open["status"] == "open"
    assert len(provider.calls) == baseline


def test_revise_evidence_replays_an_identical_request_without_a_second_call(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One request under one call id is one call, however many times it is sent."""
    strategies, service, provider, strategy_id, first, second = _two_runs(tmp_path, monkeypatch)
    pair = [cast(str, second["job_id"]), cast(str, first["job_id"])]
    proposal = _grounded_proposal(strategies, provider, strategy_id, pair)
    revised = _scripted_revise(strategies, provider, strategy_id, proposal["proposal_id"])
    baseline = len(provider.calls)
    assert revised.calls[-1]["request"]["evidence_job_ids"] == pair

    replayed = _scripted_revise(strategies, provider, strategy_id, proposal["proposal_id"])
    assert replayed.calls == revised.calls
    assert replayed.messages == revised.messages
    assert replayed.proposals == revised.proposals
    assert len(provider.calls) == baseline

    # The recorded answer is the answer to this identity, so the runs are not re-read
    # to replay it: evidence that has become unreadable since is never consulted.
    def never_reread(identifier: str) -> bytes:
        raise AssertionError("a replay reread mutable evidence")

    monkeypatch.setattr(service, "report", never_reread)
    again = _scripted_revise(strategies, provider, strategy_id, proposal["proposal_id"])
    assert again.calls == revised.calls and again.messages == revised.messages
    assert len(provider.calls) == baseline

    # The same id carrying a different request is a conflict, and asks nothing.
    with pytest.raises(UserStrategyConflictError):
        _scripted_revise(
            strategies,
            provider,
            strategy_id,
            proposal["proposal_id"],
            text="A different refinement",
        )
    assert len(provider.calls) == baseline
    assert _durable(tmp_path, strategy_id).calls == revised.calls


def test_revise_evidence_needs_its_own_explicit_approval_before_any_dispatch(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A grounded refinement is one approved model call, and nothing happens without it."""
    strategies, _service, provider, strategy_id, first, second = _two_runs(tmp_path, monkeypatch)
    pair = [cast(str, second["job_id"]), cast(str, first["job_id"])]
    proposal = _grounded_proposal(strategies, provider, strategy_id, pair)
    baseline = len(provider.calls)
    before = _durable(tmp_path, strategy_id)

    with pytest.raises(UserStrategyBudgetError):
        strategies.revise_proposal(
            strategy_id,
            proposal_id=proposal["proposal_id"],
            call_id="call-3",
            text="Refine the proposed change",
        )

    after = _durable(tmp_path, strategy_id)
    assert after.calls == before.calls and before.calls
    assert after.proposals == before.proposals
    assert after.messages == before.messages
    assert len(provider.calls) == baseline


def test_revise_evidence_keeps_its_pair_after_the_draft_has_moved_on(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proposal's own revision ages with the draft, and that is not damage.

    The runs the proposal was made over, and the readings they were frozen with, are
    what they were; the refinement is still made against today's draft, and the pair
    it inherits is unchanged by the draft having advanced.
    """
    strategies, _service, provider, strategy_id, first, second = _two_runs(tmp_path, monkeypatch)
    pair = [cast(str, second["job_id"]), cast(str, first["job_id"])]
    proposal = _grounded_proposal(strategies, provider, strategy_id, pair)
    store = UserStrategyStore(tmp_path / "workspace")
    saved = store.get(strategy_id)
    draft = cast(StrategyDraft, saved.draft)
    store.save(replace(saved, draft=replace(draft, revision=draft.revision + 1)))

    revised = _scripted_revise(strategies, provider, strategy_id, proposal["proposal_id"])

    assert revised.draft.revision == 3
    new_proposal = revised.proposals[-1]
    assert new_proposal["status"] == "open" and new_proposal["base_revision"] == 3
    assert revised.calls[-1]["request"]["evidence_job_ids"] == pair
    assert [item["job_id"] for item in revised.calls[-1]["evidence"]] == pair
    assert "Draft revision 3" in _opening(provider)


def test_http_keeps_the_old_message_body_and_refuses_a_selection_it_cannot_verify(
    tmp_path: Any,
) -> None:
    """The optional field is additive: an old body replays, and a bad selection is refused."""
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        created = client.post(
            "/api/user-strategies",
            headers=WRITE_HEADERS,
            json={
                "name": "HTTP evidence",
                "intent": "Check the message body, not the model",
                "scenario_id": SCENARIO,
                "criterion": CRITERION,
            },
        )
        assert created.status_code == 201, created.text
        strategy_id = cast(dict[str, Any], created.json())["user_strategy_id"]
        legacy = {
            "call_id": "call-1",
            "base_revision": 1,
            "text": "Explain the draft",
            "approved_budget": dict(CALL_BUDGET),
        }

        # The old request shape is the old request shape: the same idempotent body
        # replays as one recorded call, with no evidence key anywhere on it.
        first = client.post(
            f"/api/user-strategies/{strategy_id}/messages", headers=WRITE_HEADERS, json=dict(legacy)
        )
        assert first.status_code == 200, first.text
        call = cast(list[dict[str, Any]], first.json()["calls"])[0]
        assert set(cast(dict[str, Any], call["request"])) == {
            "action",
            "text",
            "base_revision",
            "proposal_id",
            "approved_budget",
        }
        assert "evidence" not in call
        replayed = client.post(
            f"/api/user-strategies/{strategy_id}/messages", headers=WRITE_HEADERS, json=dict(legacy)
        )
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["calls"] == first.json()["calls"]

        # A selection the host cannot verify is refused by the service, not by the
        # body shape, and it is refused before anything is written or asked.
        unknown = client.post(
            f"/api/user-strategies/{strategy_id}/messages",
            headers=WRITE_HEADERS,
            json={**legacy, "call_id": "call-2", "evidence_job_ids": ["job-that-never-existed"]},
        )
        assert unknown.status_code == 422
        assert unknown.json()["error"]["code"] == "user_strategy_invalid"
        # An empty selection, and more than two runs, are not a shape this host reads.
        empty = client.post(
            f"/api/user-strategies/{strategy_id}/messages",
            headers=WRITE_HEADERS,
            json={**legacy, "call_id": "call-3", "evidence_job_ids": []},
        )
        assert empty.status_code == 422
        assert empty.json()["error"]["code"] == "user_strategy_invalid"
        too_many = client.post(
            f"/api/user-strategies/{strategy_id}/messages",
            headers=WRITE_HEADERS,
            json={**legacy, "call_id": "call-4", "evidence_job_ids": ["a", "b", "c"]},
        )
        assert too_many.status_code == 422
        assert too_many.json()["error"]["code"] == "invalid_request"

        # Neither refusal touched the one call this strategy really recorded.
        kept = cast(dict[str, Any], client.get(f"/api/user-strategies/{strategy_id}").json())
        assert [item["call_id"] for item in cast(list[dict[str, Any]], kept["calls"])] == ["call-1"]


def test_a_selected_run_frozen_without_a_criterion_is_still_explained_as_itself(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real run may be frozen before any criterion is stated; that is a reading."""
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    strategy = strategies.create(
        name="No criterion yet",
        intent="Run once before the user has stated what would count",
        scenario_id=SCENARIO,
    )
    strategy_id = strategy.user_strategy_id
    run = _launch(strategies, strategy_id).experiments[-1]
    job_id = cast(str, run["job_id"])
    _terminal_job(service, job_id)
    assert run["payload"]["criterion"] is None
    job = service.get_job(job_id)
    assert job.status == "succeeded" and job.report_sha256

    provider.responses.append(_response([], text="A reading of a run with no criterion."))
    answered = strategies.send_message(
        strategy_id,
        call_id="call-1",
        base_revision=1,
        text="解释所选证据",
        approved_budget=dict(CALL_BUDGET),
        evidence_job_ids=[job_id],
    )
    assert answered.calls[-1]["status"] == "observed"
    (record,) = cast(list[dict[str, Any]], _durable(tmp_path, strategy_id).calls[-1]["evidence"])
    # The run's own absence of a criterion is what is recorded and stated: the host's
    # reading of it is its own outcome, never a refusal and never a guessed criterion.
    assert record["criterion"] is None
    assert record["reading"] == {
        "outcome": "insufficient_evidence",
        "inference": "criterion_not_stated",
    }
    opening = _opening(provider)
    assert f"job {job_id}" in opening
    assert "no criterion was frozen before this run" in opening
    assert "its frozen criterion" not in opening


@pytest.mark.parametrize("edited", ["parameters", "normalized", "criterion", "draft_revision"])
def test_a_frozen_payload_edited_under_its_own_digest_is_refused_before_dispatch(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, edited: str
) -> None:
    """The retained digest is what makes the retained payload evidence, and it is read.

    A record whose frozen blocks were edited while the digest it was frozen under was
    kept is not this strategy's evidence: the payload is refused whole, before any
    contextual field is read, before any provider is reached and before any call or
    reservation is written.
    """
    strategies, service, provider, strategy_id, first, _second = _two_runs(tmp_path, monkeypatch)
    job_id = cast(str, first["job_id"])
    baseline = len(provider.calls)
    payload = cast(dict[str, Any], first["payload"])
    replacements: dict[str, Any] = {
        "parameters": {**cast(dict[str, Any], payload["parameters"]), "target_quantity": "999"},
        "normalized": {"strategy": {"target_quantity": "999"}},
        "criterion": {"field": "economics.net_pnl.amount", "expectation": "negative"},
        "draft_revision": 99,
    }
    store = UserStrategyStore(tmp_path / "workspace")
    saved = store.get(strategy_id)
    store.save(
        replace(
            saved,
            experiments=tuple(
                {**item, "payload": {**payload, edited: replacements[edited]}}
                if item["launch_intent_id"] == "li-1"
                else item
                for item in saved.experiments
            ),
        )
    )
    # The edit is exactly the one under test: the frozen blocks moved, the digest the
    # record was frozen under did not.
    corrupted = _durable(tmp_path, strategy_id).experiments[0]
    assert corrupted["payload_sha256"] == first["payload_sha256"]
    assert corrupted["payload"][edited] == replacements[edited]

    before = _durable(tmp_path, strategy_id)
    provider.responses.append(_response([], text="An answer that must never be asked for."))
    with pytest.raises(UserStrategyStateError, match="frozen payload"):
        strategies.send_message(
            strategy_id,
            call_id="call-2",
            base_revision=2,
            text="解释所选证据",
            approved_budget=dict(CALL_BUDGET),
            evidence_job_ids=[job_id],
        )
    durable = _durable(tmp_path, strategy_id)
    assert durable.calls == before.calls and before.calls
    assert durable.messages == before.messages
    assert durable.experiments == before.experiments
    assert len(provider.calls) == baseline
    assert len(service.list_jobs()) == 2


def test_a_selected_report_that_states_no_matching_source_is_refused_rather_than_trusted(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A report is this run's only if it states the source the run was confirmed from.

    The report is served under a digest that matches it, so only the source check can
    refuse it: a report that states no source, and one that names another scenario, are
    both unverified rather than agreeing with the frozen input.
    """
    strategies, service, provider, strategy_id, first, _second = _two_runs(tmp_path, monkeypatch)
    job_id = cast(str, first["job_id"])
    real_job = service.get_job(job_id)
    real_report = service.report(job_id)
    baseline = len(provider.calls)
    before = _durable(tmp_path, strategy_id)

    def altered(kind: str) -> tuple[bytes, str]:
        document = cast(dict[str, Any], json.loads(real_report))
        if kind == "no_source":
            document.pop("source", None)
        else:
            source = cast(dict[str, Any], document["source"])
            document["source"] = {**source, "scenario_sha256": "f" * 64}
        raw = json.dumps(document).encode()
        return raw, hashlib.sha256(raw).hexdigest()

    class Published:
        """The same durable job, publishing the digest of the report as it is served."""

        def __init__(self, job: Any, digest: str) -> None:
            self._job = job
            self.report_sha256 = digest

        def __getattr__(self, name: str) -> Any:
            return getattr(self._job, name)

    def attempt(kind: str, call_id: str) -> None:
        raw, digest = altered(kind)
        monkeypatch.setattr(service, "report", lambda identifier: raw)
        monkeypatch.setattr(service, "get_job", lambda identifier: Published(real_job, digest))
        with pytest.raises(UserStrategyStateError, match="does not state the source"):
            strategies.send_message(
                strategy_id,
                call_id=call_id,
                base_revision=2,
                text="解释所选证据",
                approved_budget=dict(CALL_BUDGET),
                evidence_job_ids=[job_id],
            )

    attempt("no_source", "call-no-source")
    attempt("other_scenario", "call-other-source")

    durable = _durable(tmp_path, strategy_id)
    assert durable.calls == before.calls and before.calls
    assert durable.messages == before.messages
    assert durable.experiments == before.experiments
    assert len(provider.calls) == baseline
    assert len(service.list_jobs()) == 2


# --- one explicitly selected dataset ----------------------------------------


def _upload(service: WebService, rows: int) -> str:
    """Publish one synthetic strict-profile capture through the real upload store."""
    from ea.web.uploads import AVAILABILITY_PROVIDED
    from unit.test_web_uploads import _capture

    manifest = service.uploads.publish(_capture(rows), availability_policy=AVAILABILITY_PROVIDED)
    return str(manifest["dataset_id"])


def _contract(strategies: UserStrategyService, strategy: Any, path: str) -> dict[str, Any]:
    """One registered field contract, read from the draft's own data selection."""
    draft = cast(StrategyDraft, strategy.draft)
    for entry in strategies._registered_paths(draft):  # noqa: SLF001
        if entry.path == path:
            return {"kind": entry.kind, "maximum": entry.maximum, "current": entry.current}
    raise AssertionError(f"{path} is not a registered path of this draft")


def test_selecting_an_uploaded_dataset_is_one_revision_and_replays_as_itself(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One explicit selection: one revision, one durable request, one replay."""
    strategies, _research, service, _provider = _stack(tmp_path, monkeypatch)
    created = _create(strategies)
    identity = created.user_strategy_id
    dataset = _upload(service, 8)

    selected = strategies.select_dataset(
        identity, operation_id="select-1", base_revision=1, dataset_id=dataset
    )
    draft = cast(StrategyDraft, selected.draft)
    assert draft.revision == 2
    assert draft.dataset_id == dataset
    assert draft.source_sha256
    assert selected.dataset is not None and selected.dataset["operation_id"] == "select-1"
    assert [item["kind"] for item in selected.changes] == ["select-dataset"]

    # The same operation is answered by the record it already produced, across a
    # fresh read of the durable document: it is never a second revision.
    durable = _durable(tmp_path, identity)
    replayed = strategies.select_dataset(
        identity, operation_id="select-1", base_revision=1, dataset_id=dataset
    )
    assert replayed.draft == durable.draft
    assert len(replayed.changes) == 1

    # The same operation bound to a different request is a conflict, and a second
    # selection from the revision the caller read before is stale.
    with pytest.raises(UserStrategyConflictError):
        strategies.select_dataset(
            identity, operation_id="select-1", base_revision=1, dataset_id="other.csv"
        )
    with pytest.raises(UserStrategyStaleError):
        strategies.select_dataset(
            identity, operation_id="select-2", base_revision=1, dataset_id=dataset
        )
    # A dataset this workspace cannot bind a run to is refused before any write.
    with pytest.raises(UserStrategyStateError):
        strategies.select_dataset(
            identity, operation_id="select-3", base_revision=2, dataset_id="absent.csv"
        )
    assert _durable(tmp_path, identity).changes == durable.changes


def test_the_selected_dataset_bounds_what_a_candidate_may_change(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model's own field contract follows the data the draft really runs on."""
    from ea.web.user_strategies import _PROPOSE_TOOL_NAME

    strategies, _research, service, _provider = _stack(tmp_path, monkeypatch)
    created = _create(strategies)
    identity = created.user_strategy_id
    path = "parameters.entry_delay_bars"
    before = _contract(strategies, _durable(tmp_path, identity), path)

    # A longer capture admits a later entry bar than the registered scenario's own
    # four bars do, so the same field is bounded differently once it is selected.
    dataset = _upload(service, 8)
    selected = strategies.select_dataset(
        identity, operation_id="select-1", base_revision=1, dataset_id=dataset
    )
    after = _contract(strategies, selected, path)
    assert isinstance(after["maximum"], int) and isinstance(before["maximum"], int)
    assert after["maximum"] > before["maximum"]

    def propose(value: int) -> tuple[list[dict[str, Any]], str | None]:
        response = _response(
            [
                {
                    "id": "call-1",
                    "name": _PROPOSE_TOOL_NAME,
                    "input": {"items": [{"path": path, "value": value, "reason": "next bar"}]},
                }
            ]
        )
        return strategies._diff(_durable(tmp_path, identity), response)  # noqa: SLF001

    # A value inside the selected data's bound is a readable typed diff; a value
    # beyond it is refused whole, against the data rather than against the default.
    within, reason = propose(after["maximum"])
    assert reason is None and within and within[0]["after"] == after["maximum"]
    beyond, refused = propose(after["maximum"] + 1)
    assert beyond == [] and refused


def test_a_conversation_states_the_selected_version_the_host_verified(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outgoing model context names the selected data version, and its rows don't."""
    strategies, _research, service, provider = _stack(tmp_path, monkeypatch)
    created = _create(strategies)
    identity = created.user_strategy_id
    dataset = _upload(service, 8)
    selected = strategies.select_dataset(
        identity, operation_id="select-1", base_revision=1, dataset_id=dataset
    )

    provider.responses.append(_response([], text="An observation."))
    strategies.send_message(
        identity,
        call_id="call-1",
        base_revision=2,
        text="Describe the data this draft runs on",
        approved_budget=dict(CALL_BUDGET),
    )
    assert len(provider.calls) == 1
    opening = cast(str, cast(list[Any], provider.calls[-1]["messages"])[0].text)
    # What the model is told is the host's own reading of the selected version: the
    # dataset it was admitted as, the source digest it was admitted under, and the
    # evidence digest this workspace re-derives for that published version now.
    assert dataset in opening
    assert isinstance(selected.draft, StrategyDraft)
    assert isinstance(selected.draft.source_sha256, str)
    assert selected.draft.source_sha256 in opening
    assert cast(str, service.dataset_catalog.version_evidence_digest(dataset)) in opening
    assert "entry_delay_bars" in opening
    # The capture's rows, its path and this workspace's roots are not part of any
    # model context: the block is identity and shape only.
    assert str(service.uploads.root) not in opening
    assert "100.5" not in opening


def test_manual_save_contract_revision_and_replay(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    identity = strategy.user_strategy_id
    contract = strategies.draft_contract(identity)
    assert contract["draft_revision"] == 1
    fields = {field["path"]: field for field in contract["fields"]}
    assert fields["parameters.entry_delay_bars"]["json_type"] == "integer"
    assert fields["parameters.target_quantity"]["json_type"] == "string"
    assert fields["initial_cash"]["editable"] is True
    before = strategies.preview_experiment(identity)
    request = dict(
        operation_id="manual-1",
        expected_revision=1,
        values={"parameters.entry_delay_bars": 1, "initial_cash": "12000"},
    )
    saved = strategies.save_draft(identity, **request)
    assert saved.draft.revision == 2
    assert saved.history[-1] == strategy.draft
    assert saved.changes[-1]["kind"] == "manual"
    assert saved.experiments == strategy.experiments
    assert strategies.save_draft(identity, **request) == saved
    with pytest.raises(UserStrategyConflictError):
        strategies.save_draft(identity, **{**request, "values": {"initial_cash": "13000"}})
    with pytest.raises(UserStrategyStaleError):
        strategies.save_draft(identity, **{**request, "operation_id": "manual-2"})
    with pytest.raises(UserStrategyStaleError):
        strategies.launch_experiment(
            identity,
            launch_intent_id="old-preview",
            base_revision=1,
            preview_identity=before["preview_identity"],
            approved_budget=RUN_BUDGET,
        )
    assert strategies.preview_experiment(identity)["preview_identity"] != before["preview_identity"]
    assert provider.calls == []
    assert _children(tmp_path) == []


@pytest.mark.parametrize(
    "values",
    [
        {"parameters.target_quantity": "2.5", "parameters.entry_delay_bars": 1},
        {"parameters.entry_delay_bars": True},
        {"parameters.target_quantity": 3},
        {"initial_cash": "-1"},
        {"symbol": "NEW"},
        {"parameters.unregistered": 1},
        {},
    ],
)
def test_manual_save_validates_whole_draft_without_partial_write(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, values: dict[str, Any]
) -> None:
    strategies, _research, _service, _provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    with pytest.raises(UserStrategyStateError):
        strategies.save_draft(
            strategy.user_strategy_id,
            operation_id="manual-invalid",
            expected_revision=1,
            values=values,
        )
    assert _durable(tmp_path, strategy.user_strategy_id) == strategy


def test_manual_save_compares_revision_inside_lock(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategies, _research, _service, _provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    identity = strategy.user_strategy_id
    # An in-flight model operation owns this exact lock. Manual writes fail promptly.
    with strategies._lock(identity), pytest.raises(UserStrategyStateError, match="busy"):
        strategies.save_draft(
            identity, operation_id="held", expected_revision=1, values={"initial_cash": "11000"}
        )
    strategies.save_draft(
        identity, operation_id="first", expected_revision=1, values={"initial_cash": "11000"}
    )
    with pytest.raises(UserStrategyStaleError):
        strategies.save_draft(
            identity, operation_id="late", expected_revision=1, values={"initial_cash": "12000"}
        )
    assert _durable(tmp_path, identity).draft.initial_cash == "11000"


def test_http_manual_save_strict_contract(tmp_path: Any) -> None:
    with TestClient(create_app(_settings(tmp_path)), base_url=ORIGIN) as client:
        created = client.post(
            "/api/user-strategies",
            headers=WRITE_HEADERS,
            json={"name": "Manual", "intent": "Edit locally", "scenario_id": SCENARIO},
        )
        identity = created.json()["user_strategy_id"]
        route = f"/api/user-strategies/{identity}/draft"
        assert client.get(route + "/contract").status_code == 200
        request = {
            "operation_id": "manual-http",
            "expected_revision": 1,
            "values": {"initial_cash": "15000"},
        }
        assert (
            client.post(
                route, headers=WRITE_HEADERS, json={**request, "expected_revision": True}
            ).status_code
            == 422
        )
        saved = client.post(route, headers=WRITE_HEADERS, json=request)
        assert saved.status_code == 200, saved.text
        assert saved.json()["draft"]["revision"] == 2
        assert client.post(route, headers=WRITE_HEADERS, json=request).status_code == 200
        stale = client.post(route, headers=WRITE_HEADERS, json={**request, "operation_id": "late"})
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "stale_base_revision"


def test_manual_save_keeps_frozen_history_and_stales_ai_apply_and_undo(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)
    strategy, proposal = _propose(strategies, provider, strategy, TWO_FIELDS)
    identity = strategy.user_strategy_id
    applied = strategies.apply_proposal(
        identity, proposal_id=proposal["proposal_id"], operation_id="apply-before-manual"
    )
    # An existing frozen experiment is a retained document, never rebuilt by editing.
    frozen = {
        "launch_intent_id": "frozen-history",
        "payload": {"draft_revision": 1, "parameters": dict(strategy.draft.parameters)},
    }
    strategies._store.save(replace(applied, experiments=(frozen,)))
    edited = strategies.save_draft(
        identity,
        operation_id="manual-after-ai",
        expected_revision=2,
        values={"initial_cash": "15000"},
    )
    assert edited.experiments == (frozen,)
    assert edited.history[-1] == applied.draft
    with pytest.raises(UserStrategyStaleError):
        strategies.undo(
            identity,
            operation_id="old-undo",
            head_revision=2,
            target_operation_id="apply-before-manual",
        )
    with pytest.raises(UserStrategyStaleError):
        strategies.apply_proposal(
            identity, proposal_id=proposal["proposal_id"], operation_id="old-apply"
        )
