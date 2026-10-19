"""Domain tools through the real graph; scripted models never call a provider."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ea.web.llm import ModelResponse, ModelStreamEvent
from ea.web.service import WebService
from ea.web.uploads import AVAILABILITY_PROVIDED
from ea.web.user_strategies import UserStrategy, UserStrategyService
from unit import test_web_conversation as conversation
from unit.test_web_research import _response
from unit.test_web_uploads import _capture

Stack = tuple[UserStrategyService, WebService, conversation.Streaming]


@pytest.fixture
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Stack]:
    started: list[WebService] = []
    provider = conversation.Streaming([])
    strategies = conversation._stack(
        tmp_path, monkeypatch, started, provider, budget=conversation.LONG_BUDGET
    )
    yield strategies, started[0], provider
    while started:
        started.pop().stop()


def tool(name: str, arguments: dict[str, Any], identity: str = "read-1") -> ModelResponse:
    return _response([{"id": identity, "name": name, "input": arguments}])


def turn(
    strategies: UserStrategyService, strategy: UserStrategy, call: str = "turn-1"
) -> list[dict[str, Any]]:
    return list(
        strategies.conversation_turn(
            strategy.user_strategy_id,
            call_id=call,
            base_revision=strategy.draft.revision,
            text="Discover and inspect suitable inputs, then propose the requested change.",
            approved_budget={"max_model_calls": 4, "max_backtests": 1},
        )
    )


def result(events: list[dict[str, Any]], name: str) -> dict[str, Any]:
    value = json.loads(
        next(e["text"] for e in events if e["kind"] == "tool_end" and e["tool"] == name)
    )
    assert isinstance(value, dict)
    return value


def test_intent_discovers_and_checks_real_template_input_without_binding(stack: Stack) -> None:
    strategies, service, provider = stack
    published = service.uploads.publish(_capture(8), availability_policy=AVAILABILITY_PROVIDED)
    strategy = strategies.create(name="AAPL", intent="Test the supported long entry")
    provider.responses.extend(
        [
            tool("discover_resources", {"symbol": "AAPL"}),
            tool(
                "inspect_research_input",
                {
                    "base_revision": 1,
                    "scenario_id": "template:bounded-long.yaml",
                    "dataset_id": published["dataset_id"],
                },
                "inspect-1",
            ),
            _response(
                [], text="The selected input is valid; choose this binding before a proposal."
            ),
        ]
    )
    events = turn(strategies, strategy)
    catalog = result(events, "discover_resources")
    assert any(item["scenario_id"] == "template:bounded-long.yaml" for item in catalog["templates"])
    assert [item["dataset_id"] for item in catalog["datasets"]] == [published["dataset_id"]]
    checked = result(events, "inspect_research_input")
    assert checked["dataset_id"] == published["dataset_id"]
    assert checked["input"]["summary"]["record_count"] == 8
    assert checked["binding_required"] is True
    offered = {t.name for t in provider.calls[0]["tools"]}
    assert offered == {"discover_resources", "inspect_research_input"}
    assert len(provider.calls) == 3
    assert strategies.get(strategy.user_strategy_id).draft == strategy.draft
    assert not service.list_jobs()


def test_no_matching_data_is_empty_not_a_synthetic_substitute(stack: Stack) -> None:
    strategies, service, provider = stack
    service.uploads.publish(_capture(8), availability_policy=AVAILABILITY_PROVIDED)
    strategy = strategies.create(name="Missing", intent="Find OTHER data")
    provider.responses.extend(
        [
            tool("discover_resources", {"symbol": "OTHER"}),
            _response([], text="No matching input is available."),
        ]
    )
    events = turn(strategies, strategy)
    catalog = result(events, "discover_resources")
    assert catalog["datasets"] == [] and catalog["templates"] == []
    assert not service.list_jobs()


def test_invalid_read_can_recover_inside_the_approved_turn(stack: Stack) -> None:
    strategies, service, provider = stack
    strategy = strategies.create(name="Recover", intent="Inspect AAPL")
    provider.responses.extend(
        [
            tool(
                "inspect_research_input",
                {
                    "base_revision": 1,
                    "scenario_id": "template:bounded-long.yaml",
                    "dataset_id": "missing.csv",
                },
            ),
            tool("discover_resources", {"symbol": "AAPL"}, "discover-2"),
            _response([], text="The input is missing; the catalog lists no published dataset."),
        ]
    )
    events = turn(strategies, strategy)
    assert any(e["kind"] == "tool_error" for e in events)
    assert result(events, "discover_resources")["datasets"] == []
    assert len(provider.calls) == 3
    assert any(m.role == "tool" and '"ok": false' in m.text for m in provider.calls[1]["messages"])
    assert not service.list_jobs()


def test_stale_input_inspection_is_not_a_preview(stack: Stack) -> None:
    strategies, service, provider = stack
    strategy = conversation._create(strategies)
    provider.responses.extend(
        [
            tool("inspect_research_input", {"base_revision": 0}),
            _response([], text="Reload the current revision before preparing an experiment."),
        ]
    )
    events = turn(strategies, strategy)
    assert any(e["kind"] == "tool_error" and "revision" in e["error"] for e in events)
    assert not any(e["kind"] == "tool_end" for e in events)
    assert not strategies.get(strategy.user_strategy_id).proposals
    assert not service.list_jobs()


def test_template_tool_path_runs_real_engine_once_and_reads_verified_report(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unit.test_web_user_strategies import _terminal_job

    strategies, service, provider = stack
    published = service.uploads.publish(_capture(8), availability_policy=AVAILABILITY_PROVIDED)
    idea = strategies.create(name="Tool journey", intent="Test a supported long entry")
    bound = strategies.bind_scenario(
        idea.user_strategy_id,
        operation_id="bind-tool-case",
        base_revision=1,
        scenario_id="template:bounded-long.yaml",
        dataset_id=published["dataset_id"],
    )
    provider.responses.extend(
        [
            tool("inspect_research_input", {"base_revision": 2}),
            conversation._change("parameters.target_quantity", "3"),
        ]
    )
    events = turn(strategies, bound)
    inspection = result(events, "inspect_research_input")
    assert inspection["preview"]["record_count"] == 8
    assert inspection["run_authorized"] is False
    proposal = events[-1]["waiting"]["proposal_id"]
    applied = strategies.apply_proposal(
        bound.user_strategy_id, proposal_id=proposal, operation_id="apply-1"
    )
    assert applied.draft.revision == 3 and not service.list_jobs()
    waiting = list(
        strategies.conversation_resume(
            bound.user_strategy_id, turn_id="turn-1", action="applied", proposal_id=proposal
        )
    )[-1]["waiting"]
    assert waiting["base_revision"] == 3

    # The next scripted response resolves the actual job only after confirmation.
    class Explain:
        def __call__(self) -> ModelResponse:
            job_id = strategies.get(bound.user_strategy_id).experiments[0]["job_id"]
            return tool("read_experiments", {"job_ids": [job_id]}, "report-1")

    original_stream = provider.stream

    def stream(**kwargs: Any) -> Iterator[ModelStreamEvent]:
        if not provider.responses:
            provider.responses.extend(
                [
                    Explain()(),
                    _response(
                        [], text="Read the official report; one window cannot establish robustness."
                    ),
                ]
            )
        yield from original_stream(**kwargs)

    monkeypatch.setattr(provider, "stream", stream)
    confirmation = dict(
        turn_id="turn-1",
        action="confirm",
        launch_intent_id=waiting["launch_intent_id"],
        base_revision=waiting["base_revision"],
        preview_identity=waiting["preview_identity"],
    )
    events = list(strategies.conversation_resume(bound.user_strategy_id, **confirmation))
    experiment = strategies.get(bound.user_strategy_id).experiments[0]
    _terminal_job(service, experiment["job_id"])
    if events[-1]["kind"] == "interrupt":
        events += list(
            strategies.conversation_resume(
                bound.user_strategy_id, turn_id="turn-1", action="observe"
            )
        )
    reports = result(events, "read_experiments")["reports"]
    assert reports[0]["job_id"] == experiment["job_id"]
    assert reports[0]["report_sha256"] == service.get_job(experiment["job_id"]).report_sha256
    assert reports[0]["draft_revision"] == 3
    # Canonical call records survive re-reading and contain the actual tool result.
    stored = strategies.get(bound.user_strategy_id)
    assert stored.calls[0]["tool_results"][0]["status"] == "done"
    assert len(provider.calls) == 4 and len(service.list_jobs()) == 1
    before = len(provider.calls)
    list(strategies.conversation_resume(bound.user_strategy_id, **confirmation))
    assert len(provider.calls) == before and len(service.list_jobs()) == 1


def test_selected_report_pair_is_verified_and_foreign_selection_is_refused(stack: Stack) -> None:
    from unit.test_web_user_strategies import _launch, _terminal_job

    strategies, service, provider = stack
    strategy = conversation._create(strategies)
    for identity in ("a", "b"):
        strategy = _launch(
            strategies,
            strategy.user_strategy_id,
            launch_intent_id=identity,
            approved_budget={"max_model_calls": 0, "max_backtests": 1},
        )
        _terminal_job(service, strategy.experiments[-1]["job_id"])
    selected = [item["job_id"] for item in strategy.experiments]
    provider.responses.extend(
        [
            tool("read_experiments", {"job_ids": selected}),
            conversation._change("parameters.target_quantity", "3"),
        ]
    )
    events = list(
        strategies.conversation_turn(
            strategy.user_strategy_id,
            call_id="pair",
            base_revision=1,
            text="Read the selected reports and propose quantity 3.",
            evidence_job_ids=selected,
            approved_budget={"max_model_calls": 4, "max_backtests": 0},
        )
    )
    facts = result(events, "read_experiments")
    assert [item["job_id"] for item in facts["reports"]] == selected
    assert facts["comparison_url"] == f"/backtests/compare/{selected[0]}/{selected[1]}"
    assert facts["comparison_computed"] is False
    stored = strategies.get(strategy.user_strategy_id)
    assert stored.calls[-1]["request"]["evidence_job_ids"] == selected
    assert stored.calls[-1]["evidence"] == facts["reports"]
    proposal = events[-1]["waiting"]["proposal_id"]
    strategies.reject_proposal(strategy.user_strategy_id, proposal_id=proposal)
    list(
        strategies.conversation_resume(
            strategy.user_strategy_id, turn_id="pair", action="rejected", proposal_id=proposal
        )
    )
    assert strategies.get(strategy.user_strategy_id).draft.revision == 1
    provider.responses.extend(
        [
            tool("read_experiments", {"job_ids": ["foreign-job"]}),
            _response([], text="Select a verified own report first."),
        ]
    )
    events = turn(strategies, strategy, "foreign")
    assert any(e["kind"] == "tool_error" for e in events)
    assert len(service.list_jobs()) == 2


def test_late_model_proposal_cannot_move_to_a_new_revision(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    strategies, service, provider = stack
    strategy = conversation._create(strategies)
    provider.responses.append(conversation._change("parameters.target_quantity", "3"))
    original = provider.stream

    def delayed(**kwargs: Any) -> Iterator[ModelStreamEvent]:
        current = strategies.get(strategy.user_strategy_id)
        strategies._store.save(replace(current, draft=replace(current.draft, revision=2)))
        yield from original(**kwargs)

    monkeypatch.setattr(provider, "stream", delayed)
    events = turn(strategies, strategy)
    assert any(e["kind"] == "tool_error" and "revision" in e["error"] for e in events)
    assert not strategies.get(strategy.user_strategy_id).proposals
    assert not service.list_jobs()
