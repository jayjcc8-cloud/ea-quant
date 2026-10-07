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
from ea.product.backtest import run_backtest_scenario as engine_run_backtest_scenario
from ea.web import service as service_module
from ea.web.app import create_app
from ea.web.llm import (
    ModelCapabilities,
    ModelProviderError,
    ModelResponse,
    ModelRoute,
)
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

    The engine boundary is the module global the service actually calls, patched
    on the service module itself — the same boundary every service in this process
    uses, so the count covers a second workspace opened later in the same test as
    well. The real function is taken from the module that defines it rather than
    from a re-export.
    """
    runs = {"count": 0}
    real = engine_run_backtest_scenario

    def counted(*args: Any, **kwargs: Any) -> Any:
        runs["count"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(service_module, "run_backtest_scenario", counted)
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


def test_a_call_without_explicit_approval_never_reaches_the_provider(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategies, _research, _service, provider = _stack(tmp_path, monkeypatch)
    strategy = _create(strategies)

    with pytest.raises(UserStrategyBudgetError):
        strategies.send_message(
            strategy.user_strategy_id,
            call_id="call-1",
            base_revision=1,
            text="Ask without approving anything",
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
    assert applied.draft.revision == 2 and applied.draft.parameters["target_quantity"] == "4"

    undone = strategies.undo(
        strategy_id, operation_id="op-undo", head_revision=2, target_operation_id="op-apply"
    )
    assert undone.draft.revision == 3
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
