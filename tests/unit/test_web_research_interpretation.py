"""Batch A: one report-grounded conclusion per research task.

Self-contained on purpose: these tests import only ``ea.web.research`` and
``ea.web.service`` so the orchestration contract is proved without the HTTP
layer.

Every fixture here is a real engine run: the scenario, the job and the verified
report come from the installed product, never from a stub.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import replace
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml

from ea.web import report_claims
from ea.web.llm import ModelProviderError, ModelResponse, ToolCall
from ea.web.research import (
    BudgetExceededError,
    ResearchOrchestrator,
    ResearchSpec,
    ResearchStore,
    ResearchTask,
    TaskReservation,
    TaskStateError,
)
from ea.web.service import WebService
from ea.web.settings import RadianSettings

# --- fixtures ----------------------------------------------------------------


def _scenario_root(tmp_path: Any) -> Any:
    from unit.test_backtest_report import _priced_scenario

    root = tmp_path / "scenarios"
    source = _priced_scenario(root)
    source.rename(root / "bounded-long.yaml")
    document = yaml.safe_load((root / "bounded-long.yaml").read_text(encoding="utf-8"))
    flat = dict(document)
    flat["strategy"] = {"id": "always-flat-v1"}
    (root / "flat.yaml").write_text(yaml.safe_dump(flat, sort_keys=False), encoding="utf-8")
    # A research-visible strategy whose rule never fires on rising closes: the run
    # completes with no order and no fill, so the report itself is the evidence
    # that the criterion describes not trading rather than the idea.
    quiet = dict(document)
    quiet["schema_version"] = 2
    quiet["strategy"] = {
        "id": "price-rule-entry-v1",
        "version": 1,
        "parameters": {
            "rule": json.dumps(
                {
                    "version": 1,
                    "when": {
                        "op": "lt",
                        "left": {"kind": "price", "field": "close"},
                        "right": {"kind": "sma", "window": 2},
                    },
                    "action": "ENTER_LONG",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            "target_quantity": "2",
        },
    }
    (root / "quiet.yaml").write_text(yaml.safe_dump(quiet, sort_keys=False), encoding="utf-8")
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
    orchestrator = _orchestrator(tmp_path, monkeypatch)
    provider = FakeProvider(responses)
    cast(Any, orchestrator)._provider = provider  # test seam, same interface
    return orchestrator, cast(Any, orchestrator)._service, provider


def _task(**overrides: Any) -> ResearchTask:
    """A minimal persisted task; these tests only need one readable record."""
    now = "2026-10-05T00:00:00.000000Z"
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


def _propose_input(orchestrator: ResearchOrchestrator, **overrides: Any) -> dict[str, Any]:
    """One valid ``propose_spec`` argument set against the live catalog."""
    entry = next(item for item in orchestrator._catalog() if item["valid"])  # noqa: SLF001
    arguments: dict[str, Any] = {
        "strategy_id": entry["strategy_id"],
        "instrument": entry["instrument"] or {"venue": "XNAS", "symbol": "AAPL"},
        "initial_cash": "1000",
        "rationale": "deterministic fixture run",
        "assumptions": ["fixture data only"],
    }
    arguments.update(overrides)
    return arguments


def _spec(orchestrator: ResearchOrchestrator, **overrides: Any) -> ResearchSpec:
    """One validated spec built the way the orchestrator itself builds it."""
    spec = orchestrator._spec_from_input(_propose_input(orchestrator, **overrides))  # noqa: SLF001
    return orchestrator._with_defaults(spec)  # noqa: SLF001


def _recovered_orchestrator(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[ResearchOrchestrator, WebService, FakeProvider]:
    """A new process image: fresh store and service over the same workspace."""
    store = ResearchStore(tmp_path / "workspace")
    service = WebService(_scenario_root(tmp_path), tmp_path / "workspace")
    service.start()
    orchestrator = ResearchOrchestrator(store, service, _settings_store(tmp_path, monkeypatch))
    provider = FakeProvider([])
    cast(Any, orchestrator)._provider = provider
    return orchestrator, service, provider


def _wait_terminal(service: WebService, job_id: str, timeout: float = 30.0) -> Any:
    """Bounded wait for one real engine job to reach a terminal state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = service.get_job(job_id)
        if record.status in {"succeeded", "failed", "interrupted"}:
            return record
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach a terminal state within {timeout}s")


# --- budget: the final analysis slot is reserved -----------------------------
#
# A task that cannot pay for one conclusion call has no explanation slot, so the
# host must refuse before it dispatches anything rather than run a task whose
# own result can never be interpreted.


def test_model_reserve_holds_back_the_final_conclusion_slot(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator = _orchestrator(tmp_path, monkeypatch)
    task = replace(
        _task(budget={"max_model_calls": 2, "max_backtests": 5}),
        usage={"model_calls": 1, "input_tokens": 0, "output_tokens": 0, "backtests": 0},
        operation_kind="propose",
    )
    with pytest.raises(BudgetExceededError):
        orchestrator._reduce(  # noqa: SLF001
            task, "model_reserve", {"hold_back_last_call": True}
        )


def test_the_conclusion_call_itself_is_not_held_back(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator = _orchestrator(tmp_path, monkeypatch)
    task = replace(
        _task(budget={"max_model_calls": 2, "max_backtests": 5}),
        usage={"model_calls": 1, "input_tokens": 0, "output_tokens": 0, "backtests": 0},
        operation_kind="interpret",
    )
    reserved = orchestrator._reduce(task, "model_reserve", {})  # noqa: SLF001
    assert reserved.reservation is not None
    assert reserved.usage["model_calls"] == 2


def test_a_single_call_budget_fails_honestly_without_dispatching(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, _service_, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    provider.responses.append(
        _response(
            [
                {
                    "id": "call-1",
                    "name": "propose_spec",
                    "input": _propose_input(orchestrator),
                }
            ]
        )
    )
    monkeypatch.setattr(
        cast(Any, orchestrator), "_budget", lambda: {"max_model_calls": 1, "max_backtests": 5}
    )
    task = orchestrator.create("an idea that cannot pay for a conclusion")
    assert task.status == "failed"
    assert task.error is not None
    assert task.error["code"] == "budget_exceeded"
    assert provider.calls == []


def test_confirm_refuses_before_engine_execution_when_the_slot_is_missing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, _provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    store = cast(Any, orchestrator)._store
    # A one-call budget reaches confirmation only after the proposal spent it, so
    # the run could never be interpreted. The host must refuse before the engine.
    seeded = replace(
        _task(
            status="awaiting_confirmation",
            budget={"max_model_calls": 1, "max_backtests": 5},
            usage={"model_calls": 1, "input_tokens": 0, "output_tokens": 0, "backtests": 0},
        ),
        spec_draft=_spec(orchestrator),
    )
    store.save(seeded)
    submitted: list[Any] = []
    real_create = service.create_job

    def _spy(**kwargs: Any) -> Any:
        submitted.append(kwargs)
        return real_create(**kwargs)

    monkeypatch.setattr(service, "create_job", _spy)
    task = orchestrator.confirm(seeded.task_id)
    assert task.status == "failed"
    assert task.error is not None
    assert task.error["code"] == "budget_exceeded"
    assert submitted == []
    assert task.jobs == ()


# --- the real engine, a real report, one conclusion ---------------------------
#
# Everything below runs the installed engine and reads the verified formal
# report. The scripted provider only chooses codes; every value and every
# sentence in the published conclusion is read or written by the host.

CRITERION = {"field": "economics.net_pnl.amount", "expectation": "positive"}


class BlockingProvider(FakeProvider):
    """Blocks inside ``complete`` until released, reporting when it was reached."""

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


def _propose(orchestrator: ResearchOrchestrator, provider: FakeProvider, **overrides: Any) -> None:
    provider.responses.append(
        _response(
            [
                {
                    "id": "call-1",
                    "name": "propose_spec",
                    "input": _propose_input(orchestrator, **overrides),
                }
            ]
        )
    )


def _catalog_entry(orchestrator: ResearchOrchestrator, stem: str) -> dict[str, Any]:
    """The registered scenario whose name is ``stem``, as the host offers it."""
    entries = orchestrator._catalog()  # noqa: SLF001
    for entry in entries:
        if entry.get("name") == stem:
            return entry
    raise AssertionError(f"no scenario named {stem!r} among {[e.get('name') for e in entries]}")


def _succeeded_run(
    orchestrator: ResearchOrchestrator,
    service: WebService,
    provider: FakeProvider,
    *,
    criterion: dict[str, Any] | None = CRITERION,
    scenario: str | None = None,
) -> ResearchTask:
    """One full experiment: propose, confirm, run the real engine, reconcile."""
    overrides: dict[str, Any] = {} if criterion is None else {"criterion": criterion}
    if scenario is not None:
        entry = _catalog_entry(orchestrator, scenario)
        overrides["scenario_id"] = entry["scenario_id"]
        overrides["strategy_id"] = entry["strategy_id"]
        overrides["instrument"] = entry["instrument"]
    _propose(orchestrator, provider, **overrides)
    task = orchestrator.create("An idea with one frozen expectation")
    assert task.status == "awaiting_confirmation", task.error
    confirmed = orchestrator.confirm(task.task_id)
    assert confirmed.jobs, confirmed.error
    record = _wait_terminal(service, confirmed.jobs[0])
    assert record.status == "succeeded", record.message
    settled = orchestrator.refresh(task.task_id)
    assert settled.status == "succeeded", settled.error
    return settled


def _real_sign(value: str) -> str:
    """The sign of the report's own value, computed without the module under test."""
    parsed = Decimal(value)
    return "positive" if parsed > 0 else "negative" if parsed < 0 else "zero"


def _report_document(service: WebService, job_id: str) -> dict[str, Any]:
    """The verified report, read the way any other consumer of it would."""
    return cast(dict[str, Any], json.loads(service.report(job_id)))


def _inference_for(service: WebService, job_id: str) -> tuple[str, str]:
    """The reading of the real report, and the code that matches it."""
    value = _report_document(service, job_id)["economics"]["net_pnl"]["amount"]
    sign = _real_sign(value)
    code = (
        "criterion_direction_matched" if sign == "positive" else "criterion_direction_not_matched"
    )
    return value, code


def test_a_criterion_that_names_nothing_real_is_refused_and_can_be_repaired(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    _propose(orchestrator, provider, criterion={"field": "economics.profit", "expectation": "big"})
    _propose(orchestrator, provider, criterion=CRITERION)
    task = orchestrator.create("An idea whose first criterion names nothing real")
    assert task.status == "awaiting_confirmation", task.error
    assert task.spec_draft is not None
    assert task.spec_draft.criterion == {**CRITERION, "schema": report_claims.CRITERION_SCHEMA}
    # The refusal was fed back as a tool result, not silently dropped.
    follow_up = [message.text or "" for message in provider.calls[1]["messages"]]
    assert any(text.startswith("rejected:") for text in follow_up), follow_up
    service.stop()


def test_the_published_conclusion_reads_the_verified_report(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    task = _succeeded_run(orchestrator, service, provider)
    value, inference = _inference_for(service, task.jobs[0])
    sign = _real_sign(value)
    provider.responses.append(
        _response(
            [
                {
                    "id": "call-2",
                    "name": "conclude",
                    "input": {
                        "inference": inference,
                        "evidence_ref": {"field": CRITERION["field"]},
                    },
                }
            ]
        )
    )
    concluded = orchestrator.interpret(task.task_id)
    assert concluded.interpretation_error is None, concluded.interpretation_error
    interpretation = concluded.interpretation
    assert interpretation is not None
    assert interpretation["observed"]["value"] == value
    assert interpretation["observed"]["sign"] == sign
    assert interpretation["observed"]["unit"] == "currency"
    assert interpretation["outcome"] == (
        "supported_in_scope" if sign == "positive" else "not_supported"
    )
    # The host's own reading is published alongside the code the model selected.
    assert interpretation["criterion_met"] is (sign == "positive")
    assert interpretation["host_inference"] == inference
    assert interpretation["conclusion_scope"] == report_claims.FROZEN_SCOPE
    assert interpretation["observed"]["evidence_ref"]["field"] == CRITERION["field"]
    # Exactly two calls: one to propose, one to conclude. No re-dispatch.
    assert len(provider.calls) == 2
    assert concluded.usage["model_calls"] == 2


def test_a_model_claim_is_never_displayed_as_written(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    task = _succeeded_run(orchestrator, service, provider)
    _value, matching = _inference_for(service, task.jobs[0])
    # The opposite of the report's own reading, wrapped in confident prose.
    opposite = (
        "criterion_direction_not_matched"
        if matching == "criterion_direction_matched"
        else "criterion_direction_matched"
    )
    prose = "This proves the strategy is profitable and will make money."
    provider.responses.append(
        _response(
            [
                {
                    "id": "call-2",
                    "name": "conclude",
                    "input": {
                        "inference": opposite,
                        "evidence_ref": {"field": CRITERION["field"]},
                    },
                }
            ],
            text=prose,
        )
    )
    refused = orchestrator.interpret(task.task_id)
    assert refused.interpretation is None
    assert refused.interpretation_error is not None
    assert refused.interpretation_error["code"] == "claim_rejected"
    assert refused.status == "succeeded"
    body = json.dumps(refused.document())
    assert prose not in body
    assert "profitable" not in body
    assert "make money" not in body


def test_a_run_without_a_frozen_criterion_is_honestly_insufficient(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    task = _succeeded_run(orchestrator, service, provider, criterion=None)
    calls_before = len(provider.calls)
    concluded = orchestrator.interpret(task.task_id)
    interpretation = concluded.interpretation
    assert interpretation is not None
    assert interpretation["outcome"] == "insufficient_evidence"
    assert interpretation["inference"] == "criterion_not_stated"
    assert interpretation["criterion_met"] is None
    assert interpretation["observed"]["state"] == "not_stated"
    assert interpretation["conclusion_scope"] == report_claims.FROZEN_SCOPE
    # Nothing was compared against, so no model call is needed or spent.
    assert len(provider.calls) == calls_before
    assert concluded.usage["model_calls"] == calls_before


def test_a_model_cannot_widen_a_satisfied_criterion_into_a_wider_claim(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    task = _succeeded_run(orchestrator, service, provider)
    _value, matching = _inference_for(service, task.jobs[0])
    assert matching == "criterion_direction_matched"
    # The model asks for a claim wider than the frozen run. Nothing in the
    # accepted input declares a wider scope, so the call is refused: no
    # conclusion is published and the completed run is untouched.
    provider.responses.append(
        _response(
            [
                {
                    "id": "call-2",
                    "name": "conclude",
                    "input": {
                        "inference": "declared_scope_not_verified",
                        "evidence_ref": {"field": CRITERION["field"]},
                        "conclusion_scope": report_claims.BEYOND_SCOPE,
                    },
                }
            ]
        )
    )
    refused = orchestrator.interpret(task.task_id)
    assert refused.interpretation is None
    assert refused.interpretation_error is not None
    assert refused.interpretation_error["code"] == "claim_rejected"
    assert refused.status == "succeeded"
    assert refused.jobs == task.jobs


def test_a_model_cannot_move_the_conclusion_onto_another_report_field(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    task = _succeeded_run(orchestrator, service, provider)
    _value, matching = _inference_for(service, task.jobs[0])
    provider.responses.append(
        _response(
            [
                {
                    "id": "call-2",
                    "name": "conclude",
                    "input": {
                        "inference": matching,
                        "evidence_ref": {"field": "economics.fees.amount"},
                    },
                }
            ]
        )
    )
    refused = orchestrator.interpret(task.task_id)
    assert refused.interpretation is None
    assert refused.interpretation_error is not None
    assert refused.interpretation_error["code"] == "claim_rejected"


def test_a_provider_failure_charges_the_call_and_never_rewrites_the_run(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    task = _succeeded_run(orchestrator, service, provider)
    charged = task.usage["model_calls"]
    provider.responses.append(ModelProviderError("connection dropped"))
    unknown = orchestrator.interpret(task.task_id)
    assert unknown.status == "succeeded"
    assert unknown.error is None
    assert unknown.interpretation is None
    assert unknown.interpretation_error is not None
    assert unknown.interpretation_error["code"] == "model_outcome_unknown"
    assert unknown.usage["model_calls"] == charged + 1
    assert unknown.reservation is None
    # An explicit repeat never re-dispatches an unknown outcome.
    again = orchestrator.interpret(task.task_id)
    assert again.interpretation_error == unknown.interpretation_error
    assert len(provider.calls) == charged + 1


def test_an_observed_response_survives_a_restart_and_publishes_locally(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    task = _succeeded_run(orchestrator, service, provider)
    value, inference = _inference_for(service, task.jobs[0])
    provider.responses.append(
        _response([{"id": "call-2", "name": "conclude", "input": {"inference": inference}}])
    )
    # Persist the observed response, then abandon the process before publishing.
    generation = orchestrator._begin(task.task_id, "interpret")
    assert generation is not None
    live = orchestrator._store.get(task.task_id)
    response = provider.complete(system="", messages=[], tools=[])
    evidence = orchestrator._evidence_document(
        report_claims.identity_of(
            _report_document(service, task.jobs[0]), service.get_job(task.jobs[0])
        ),
        live.spec_confirmed[-1][2].criterion,
        response,
        orchestrator._interpretation_versions(live),
        orchestrator._model_route(),
    )
    orchestrator._transition(task.task_id, generation, "interpretation_evidence", evidence=evidence)
    service.stop()

    # A new process image: same workspace, fresh store, provider that must not be used.
    recovered, recovered_service, recovered_provider = _recovered_orchestrator(
        tmp_path, monkeypatch
    )
    published = recovered.refresh(task.task_id)
    assert published.status == "succeeded"
    assert published.interpretation is not None
    assert published.interpretation["observed"]["value"] == value
    assert recovered_provider.calls == []
    recovered_service.stop()


# --- the recorded call set is complete, and finite ---------------------------
#
# The audit record keeps the model's own call identity: the id it sent, the tool
# name, and the exact arguments. It is a bounded collection, so the number of
# calls and the UTF-8 size of the whole id/name/arguments set each have an
# explicit limit. Past either limit the response is refused whole and only a
# fixed-size record of the refusal is kept, because a shortened set can re-parse
# as a different, apparently valid conclusion.


def _canonical(document: Any) -> bytes:
    """The compact UTF-8 form the host persists, computed without the host."""
    encoded: bytes = json.dumps(
        document, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return encoded


def test_the_recorded_call_keeps_the_identity_it_was_sent_across_a_restart(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    task = _succeeded_run(orchestrator, service, provider)
    value, inference = _inference_for(service, task.jobs[0])
    arguments = {"inference": inference, "evidence_ref": {"field": CRITERION["field"]}}
    provider.responses.append(_response([{"id": "call-2", "name": "conclude", "input": arguments}]))
    concluded = orchestrator.interpret(task.task_id)
    assert concluded.interpretation is not None, concluded.interpretation_error

    evidence = concluded.interpretation_evidence
    assert evidence is not None
    assert len(evidence["calls"]) == 1
    call = evidence["calls"][0]
    # The id the model sent is part of the record, beside the tool name and the
    # exact arguments: the host stores what the call was, it never rewrites it.
    assert call["id"] == "call-2"
    assert call["tool"] == report_claims.CONCLUDE_TOOL_NAME
    assert json.loads(call["arguments"]) == arguments
    assert call["over_limit"] is False
    assert evidence["calls_rejection"] is None

    # A restart re-derives the same conclusion from these stored bytes alone.
    service.stop()
    recovered, recovered_service, recovered_provider = _recovered_orchestrator(
        tmp_path, monkeypatch
    )
    refreshed = recovered.refresh(task.task_id)
    assert refreshed.interpretation == concluded.interpretation
    assert refreshed.interpretation["observed"]["value"] == value
    assert recovered_provider.calls == []
    recovered_service.stop()


def test_a_response_past_the_call_count_bound_is_refused_whole(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    task = _succeeded_run(orchestrator, service, provider)
    charged = task.usage["model_calls"]
    # The contract is exactly one conclusion call, so a response carrying a
    # thousand unique calls is more call data than this product keeps, whatever
    # it says. Nothing about it is stored as if it were a conclusion.
    provider.responses.append(
        _response(
            [{"id": f"call-{index}", "name": "conclude", "input": {}} for index in range(1001)]
        )
    )
    refused = orchestrator.interpret(task.task_id)

    assert refused.status == "succeeded"
    assert refused.interpretation is None
    assert refused.interpretation_error is not None
    # A technical refusal on the conclusion axis, never a scientific finding
    # about the run: the computation and its verified report are untouched.
    assert refused.interpretation_error["step"] == "interpret"
    assert refused.interpretation_error["code"] == "claim_rejected"
    assert refused.usage["model_calls"] == charged + 1
    assert refused.reservation is None

    evidence = refused.interpretation_evidence
    assert evidence is not None
    # No prefix of the response is kept as a normal, parseable collection.
    assert evidence["calls"] is None
    refusal = evidence["calls_rejection"]
    assert set(refusal) == {
        "reason",
        "call_count",
        "calls_bytes",
        "calls_sha256",
        "limit_calls",
        "limit_bytes",
    }
    assert refusal["reason"] == "too_many_calls"
    assert refusal["call_count"] == 1001
    assert refusal["limit_calls"] < 1001
    # Fixed size: the refusal record cannot grow with the response it refuses.
    assert len(json.dumps(refusal, ensure_ascii=True, sort_keys=True)) < 512

    # The one call really was dispatched and observed, so it stays charged: no
    # refund, and no second call here or in a new process image.
    assert len(provider.calls) == charged + 1
    again = orchestrator.interpret(task.task_id)
    assert again.interpretation_evidence == evidence
    assert len(provider.calls) == charged + 1
    service.stop()

    recovered, recovered_service, recovered_provider = _recovered_orchestrator(
        tmp_path, monkeypatch
    )
    refreshed = recovered.refresh(task.task_id)
    assert refreshed.interpretation is None
    assert refreshed.interpretation_error is not None
    assert refreshed.interpretation_error["code"] == "claim_rejected"
    kept = refreshed.interpretation_evidence
    assert kept is not None
    assert kept["calls_rejection"] == refusal
    assert refreshed.usage["model_calls"] == charged + 1
    assert recovered_provider.calls == []
    recovered_service.stop()


def test_a_response_past_the_total_byte_bound_is_refused_with_its_real_size(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    task = _succeeded_run(orchestrator, service, provider)
    charged = task.usage["model_calls"]
    # Six calls, each comfortably inside the per-argument bound: what is over the
    # bound is the size of the whole set, not any single call in it.
    arguments = {
        "inference": "criterion_direction_matched",
        "evidence_ref": {"field": CRITERION["field"], "note": "x" * 3800},
    }
    assert len(_canonical(arguments)) <= 4000
    calls = [{"id": f"call-{index}", "name": "conclude", "input": arguments} for index in range(6)]
    provider.responses.append(_response(calls))

    refused = orchestrator.interpret(task.task_id)

    assert refused.interpretation is None
    assert refused.interpretation_error is not None
    assert refused.interpretation_error["code"] == "claim_rejected"
    assert refused.usage["model_calls"] == charged + 1
    evidence = refused.interpretation_evidence
    assert evidence is not None
    assert evidence["calls"] is None
    refusal = evidence["calls_rejection"]
    assert refusal["reason"] == "too_many_bytes"
    assert refusal["call_count"] == 6
    # The size and digest are the real ones, measured over the complete
    # id/name/arguments set exactly as the response carried it.
    whole = _canonical([{"id": c["id"], "name": c["name"], "arguments": c["input"]} for c in calls])
    assert refusal["calls_bytes"] == len(whole)
    assert refusal["calls_sha256"] == hashlib.sha256(whole).hexdigest()
    assert refusal["calls_bytes"] > refusal["limit_bytes"]

    # Nothing is re-derived or re-asked: the refusal is a persisted fact, and the
    # same bytes give the same refusal on a repeat and after a restart.
    again = orchestrator.interpret(task.task_id)
    assert again.interpretation_evidence == evidence
    assert len(provider.calls) == charged + 1
    service.stop()

    recovered, recovered_service, recovered_provider = _recovered_orchestrator(
        tmp_path, monkeypatch
    )
    refreshed = recovered.refresh(task.task_id)
    assert refreshed.interpretation_error is not None
    assert refreshed.interpretation_error["code"] == "claim_rejected"
    kept = refreshed.interpretation_evidence
    assert kept is not None
    assert kept["calls_rejection"] == refusal
    assert recovered_provider.calls == []
    recovered_service.stop()


def test_cancelling_a_live_interpretation_keeps_evidence_and_publishes_nothing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    task = _succeeded_run(orchestrator, service, provider)
    _value, inference = _inference_for(service, task.jobs[0])
    blocking = BlockingProvider(
        [_response([{"id": "call-2", "name": "conclude", "input": {"inference": inference}}])]
    )
    cast(Any, orchestrator)._provider = blocking
    outcome: list[Any] = []

    def run() -> None:
        outcome.append(orchestrator.interpret(task.task_id))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert blocking.invoked.wait(10), "the conclusion call never reached the provider"
    cancelled = orchestrator.cancel(task.task_id)
    # The run itself is already durable and succeeded; cancelling closes the
    # conclusion axis rather than rewriting computation that really happened.
    assert cancelled.status == "succeeded"
    assert cancelled.error is None
    assert cancelled.interpretation_error is not None
    assert cancelled.interpretation_error["code"] == "cancelled_by_user"
    blocking.release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()

    after = orchestrator._store.get(task.task_id)
    assert after.status == "succeeded"
    # The late response is kept as evidence only; nothing was published or revived.
    assert after.interpretation is None
    assert after.interpretation_evidence is not None
    assert len(blocking.calls) == 1
    refreshed = orchestrator.refresh(task.task_id)
    assert refreshed.status == "succeeded"
    assert refreshed.interpretation is None
    # A cancelled conclusion is never re-dispatched.
    again = orchestrator.interpret(task.task_id)
    assert again.interpretation is None
    assert len(blocking.calls) == 1
    service.stop()


# --- the observation and its publication are one durable step ---------------
#
# A charge with a recorded model outcome but no persisted response is a task
# whose conclusion slot is spent and whose evidence is gone: it can neither be
# published nor honestly re-dispatched. Every durable image must therefore carry
# both, or neither.


class RecordingStore(ResearchStore):
    """A store that keeps every durable image it was asked to write."""

    def __init__(self, root: Any) -> None:
        super().__init__(root)
        self.images: list[dict[str, Any]] = []

    def save(self, task: ResearchTask) -> None:
        self.images.append(task.document())
        super().save(task)


def test_no_durable_image_records_a_model_outcome_without_its_evidence(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    task = _succeeded_run(orchestrator, service, provider)
    _value, inference = _inference_for(service, task.jobs[0])
    recording = RecordingStore(tmp_path / "workspace")
    cast(Any, orchestrator)._store = recording
    provider.responses.append(
        _response([{"id": "call-2", "name": "conclude", "input": {"inference": inference}}])
    )
    concluded = orchestrator.interpret(task.task_id)
    assert concluded.interpretation is not None, concluded.interpretation_error

    # The run's earlier proposal call is an observation too, so the invariant is
    # measured from the moment the conclusion call was dispatched: from there on,
    # the only image in which the slot is no longer reserved is the one that also
    # carries the response. A charge with no evidence is a state that never exists.
    dispatched = next(
        index
        for index, image in enumerate(recording.images)
        if (image.get("reservation") or {}).get("status") == "dispatched"
    )
    settled = recording.images[dispatched:]
    assert settled
    assert any(image.get("interpretation_evidence") for image in settled)
    for image in settled:
        if image.get("reservation") is None:
            assert image.get("interpretation_evidence") is not None
    service.stop()


def test_a_recorded_outcome_is_enough_to_conclude_locally_after_a_restart(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    task = _succeeded_run(orchestrator, service, provider)
    value, inference = _inference_for(service, task.jobs[0])
    provider.responses.append(
        _response([{"id": "call-2", "name": "conclude", "input": {"inference": inference}}])
    )
    generation = orchestrator._begin(task.task_id, "interpret")
    assert generation is not None
    reserved = orchestrator._transition(task.task_id, generation, "model_reserve", step="interpret")
    assert reserved.reservation is not None
    seq = reserved.reservation.seq
    orchestrator._transition(task.task_id, generation, "model_dispatch")
    live = orchestrator._store.get(task.task_id)
    response = provider.complete(system="", messages=[], tools=[])
    evidence = orchestrator._evidence_document(
        report_claims.identity_of(
            _report_document(service, task.jobs[0]), service.get_job(task.jobs[0])
        ),
        live.spec_confirmed[-1][2].criterion,
        response,
        orchestrator._interpretation_versions(live),
        orchestrator._model_route(),
        seq=seq,
    )
    # One save: the charge, the observed outcome and the response land together.
    orchestrator._transition(
        task.task_id,
        generation,
        "model_concluded",
        seq=seq,
        response=response,
        evidence=evidence,
        model=orchestrator._observation(response),
    )
    service.stop()

    recovered, recovered_service, recovered_provider = _recovered_orchestrator(
        tmp_path, monkeypatch
    )
    published = recovered.refresh(task.task_id)
    assert published.interpretation is not None, published.interpretation_error
    assert published.interpretation["observed"]["value"] == value
    assert recovered_provider.calls == []
    recovered_service.stop()


# --- cancellation is atomic at every boundary --------------------------------


def test_cancelling_before_dispatch_releases_the_pending_charge(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, _provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    store = cast(Any, orchestrator)._store
    confirmed = _spec(orchestrator)
    task = replace(
        _task(
            status="succeeded",
            budget={"max_model_calls": 2, "max_backtests": 1},
            usage={"model_calls": 2, "input_tokens": 0, "output_tokens": 0, "backtests": 1},
            spec_confirmed=((1, "2026-10-05T00:00:00.000000Z", confirmed),),
            jobs=("job-1",),
            operation_kind="interpret",
        ),
        reservation=TaskReservation("model_call", 2, "pending", "2026-10-05T00:00:00.000000Z"),
    )
    store.save(task)
    cancelled = orchestrator.cancel(task.task_id)
    assert cancelled.status == "succeeded"
    assert cancelled.interpretation_error is not None
    # Nothing was dispatched, so the slot is not spent: it is released with the
    # cancellation in the same durable write.
    assert cancelled.reservation is None
    assert cancelled.usage["model_calls"] == 1
    service.stop()


def test_a_dispatched_conclusion_stays_charged_when_it_is_cancelled(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, _provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    store = cast(Any, orchestrator)._store
    confirmed = _spec(orchestrator)
    task = replace(
        _task(
            status="succeeded",
            budget={"max_model_calls": 2, "max_backtests": 1},
            usage={"model_calls": 2, "input_tokens": 0, "output_tokens": 0, "backtests": 1},
            spec_confirmed=((1, "2026-10-05T00:00:00.000000Z", confirmed),),
            jobs=("job-1",),
            operation_kind="interpret",
        ),
        reservation=TaskReservation("model_call", 2, "dispatched", "2026-10-05T00:00:00.000000Z"),
    )
    store.save(task)
    cancelled = orchestrator.cancel(task.task_id)
    assert cancelled.interpretation_error is not None
    # A dispatched call cannot be refunded, so it stays charged and unknown.
    assert cancelled.reservation is not None
    assert cancelled.reservation.status == "dispatched"
    assert cancelled.usage["model_calls"] == 2
    service.stop()


def test_the_reducer_refuses_to_reserve_a_conclusion_slot_after_cancellation(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator = _orchestrator(tmp_path, monkeypatch)
    task = replace(
        _task(status="succeeded", operation_kind="interpret"),
        interpretation_error={"step": "interpret", "code": "cancelled_by_user", "message": "x"},
    )
    with pytest.raises(TaskStateError):
        orchestrator._reduce(  # noqa: SLF001
            task, "model_reserve", {"step": "interpret"}
        )


def test_a_late_failure_never_overwrites_a_closed_conclusion(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator = _orchestrator(tmp_path, monkeypatch)
    closed = replace(
        _task(status="succeeded", operation_kind="interpret"),
        interpretation_error={"step": "interpret", "code": "cancelled_by_user", "message": "x"},
        interpretation_evidence={"schema": "x"},
    )
    after = orchestrator._reduce(  # noqa: SLF001
        closed,
        "interpretation_failure",
        {"error": {"step": "interpret", "code": "claim_rejected", "message": "late"}},
    )
    assert after.interpretation_error == closed.interpretation_error


def test_a_late_conclusion_never_revives_a_closed_axis(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator = _orchestrator(tmp_path, monkeypatch)
    evidence = {"schema": "x"}
    closed = replace(
        _task(status="succeeded", operation_kind="interpret"),
        interpretation_error={"step": "interpret", "code": "cancelled_by_user", "message": "x"},
        interpretation_evidence=evidence,
    )
    with pytest.raises(TaskStateError):
        orchestrator._reduce(  # noqa: SLF001
            closed, "interpretation", {"evidence": evidence, "interpretation": {"schema": "y"}}
        )


def _cancel_during_read(
    orchestrator: ResearchOrchestrator, task_id: str, *, at: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel the task during the ``at``-th verified-report read it makes.

    The wrapper is the smallest seam that reproduces a real cancellation landing
    at a specific point in the orchestration: it runs the real read first, then
    closes the axis exactly as the user's cancel would, and returns the report
    the host had already verified.
    """
    real = orchestrator._verified_report  # noqa: SLF001
    reads = {"n": 0}

    def read(inner_task_id: str) -> Any:
        found = real(inner_task_id)
        reads["n"] += 1
        if reads["n"] == at:
            orchestrator.cancel(task_id)
        return found

    monkeypatch.setattr(orchestrator, "_verified_report", read)


def test_a_cancellation_after_the_report_read_stops_before_the_reserve(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    task = _succeeded_run(orchestrator, service, provider)
    calls_before = len(provider.calls)
    _cancel_during_read(orchestrator, task.task_id, at=1, monkeypatch=monkeypatch)

    settled = orchestrator.interpret(task.task_id)

    # The axis closed after the host had read and verified the report but before
    # any slot was reserved: the reserve is refused, nothing is dispatched and
    # nothing is spent. The run and its report are untouched.
    assert settled.status == "succeeded"
    assert settled.interpretation is None
    assert settled.interpretation_evidence is None
    assert settled.reservation is None
    assert settled.interpretation_error is not None
    assert settled.interpretation_error["code"] == "cancelled_by_user"
    assert settled.usage["model_calls"] == 1
    assert len(provider.calls) == calls_before
    service.stop()


def test_a_cancellation_after_the_publication_snapshot_publishes_nothing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    task = _succeeded_run(orchestrator, service, provider)
    _value, inference = _inference_for(service, task.jobs[0])
    provider.responses.append(
        _response([{"id": "call-2", "name": "conclude", "input": {"inference": inference}}])
    )
    # The second read is the one ``_publish_interpretation`` makes from its own
    # snapshot, immediately before the locked publication write.
    _cancel_during_read(orchestrator, task.task_id, at=2, monkeypatch=monkeypatch)

    settled = orchestrator.interpret(task.task_id)

    # The response was observed and stored as evidence, and the axis then closed
    # before publication could take the lock. The evidence survives; the
    # conclusion is never published and never revives the closed axis.
    assert settled.interpretation is None
    assert settled.interpretation_evidence is not None
    assert settled.interpretation_error is not None
    assert settled.interpretation_error["code"] == "cancelled_by_user"
    assert "interpretation" not in settled.document()
    assert settled.usage["model_calls"] == 2
    assert len(provider.calls) == 2
    service.stop()


# --- a served conclusion is re-verified on every read ------------------------


def _published_run(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[ResearchOrchestrator, WebService, FakeProvider, ResearchTask]:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    task = _succeeded_run(orchestrator, service, provider)
    _value, inference = _inference_for(service, task.jobs[0])
    provider.responses.append(
        _response([{"id": "call-2", "name": "conclude", "input": {"inference": inference}}])
    )
    concluded = orchestrator.interpret(task.task_id)
    assert concluded.interpretation is not None, concluded.interpretation_error
    return orchestrator, service, provider, concluded


def test_tampering_with_the_report_withdraws_the_served_conclusion(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider, concluded = _published_run(tmp_path, monkeypatch)
    assert concluded.interpretation is not None
    value = concluded.interpretation["observed"]["value"]
    assert value in json.dumps(concluded.document())
    calls_before = len(provider.calls)
    report_path = service.reports_dir / concluded.jobs[0] / "report.json"
    document = json.loads(report_path.read_text(encoding="utf-8"))
    document["economics"]["net_pnl"]["amount"] = "999.00"
    report_path.write_text(json.dumps(document), encoding="utf-8")

    refreshed = orchestrator.refresh(concluded.task_id)
    assert refreshed.interpretation is None
    assert refreshed.interpretation_error is not None
    assert refreshed.interpretation_error["code"] in {
        "report_unavailable",
        "report_identity_changed",
    }
    # Nothing derived from the report is served any more; the tampered amount
    # never reaches the record the UI reads.
    assert "interpretation" not in refreshed.document()
    assert "999.00" not in json.dumps(refreshed.document())
    # Reading never dispatches and never spends.
    assert len(provider.calls) == calls_before
    assert refreshed.status == "succeeded"
    service.stop()


def test_a_deleted_report_withdraws_the_served_conclusion_across_a_restart(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, _provider, concluded = _published_run(tmp_path, monkeypatch)
    assert concluded.interpretation is not None
    rendered = concluded.interpretation["outcome_text"]
    assert rendered in json.dumps(concluded.document())
    (service.reports_dir / concluded.jobs[0] / "report.json").unlink()
    service.stop()
    # The computed outcome the task recorded is still there; the conclusion that
    # depended on the deleted report must not be served from it.
    stored = json.loads(
        (tmp_path / "workspace" / "research" / f"{concluded.task_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["interpretation"] is not None

    recovered, recovered_service, recovered_provider = _recovered_orchestrator(
        tmp_path, monkeypatch
    )
    refreshed = recovered.refresh(concluded.task_id)
    assert refreshed.interpretation is None
    assert refreshed.interpretation_error is not None
    assert refreshed.interpretation_error["code"] == "report_unavailable"
    # No rendered conclusion survives anywhere in the record the UI reads.
    assert "interpretation" not in refreshed.document()
    assert rendered not in json.dumps(refreshed.document())
    assert recovered_provider.calls == []
    recovered_service.stop()


def test_listing_tasks_re_verifies_the_conclusions_it_serves(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, _provider, concluded = _published_run(tmp_path, monkeypatch)
    assert concluded.interpretation is not None
    rendered = concluded.interpretation["outcome_text"]
    (service.reports_dir / concluded.jobs[0] / "report.json").unlink()
    listed = {task.task_id: task for task in orchestrator.reconcile_all()}
    served = listed[concluded.task_id]
    assert served.interpretation is None
    assert served.interpretation_error is not None
    assert "interpretation" not in served.document()
    assert rendered not in json.dumps(served.document())
    service.stop()


def test_an_unchanged_report_keeps_serving_the_same_conclusion(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider, concluded = _published_run(tmp_path, monkeypatch)
    calls_before = len(provider.calls)
    refreshed = orchestrator.refresh(concluded.task_id)
    assert refreshed.interpretation == concluded.interpretation
    assert len(provider.calls) == calls_before
    service.stop()


def test_a_conclusion_stored_under_older_semantics_is_a_technical_unavailability(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider, concluded = _published_run(tmp_path, monkeypatch)
    published = concluded.interpretation
    assert published is not None
    calls_before = len(provider.calls)
    store = cast(Any, orchestrator)._store  # noqa: SLF001
    legacy = {**published, "schema": "radian.research-interpretation.v1"}
    store.save(replace(concluded, interpretation=legacy))

    refreshed = orchestrator.refresh(concluded.task_id)

    # A stored conclusion this build cannot re-derive is reported as a technical
    # unavailability on the conclusion axis, not as a withdrawn or weaker claim,
    # and it is never re-rendered as if it were current.
    assert refreshed.interpretation is None
    assert refreshed.interpretation_error is not None
    assert refreshed.interpretation_error["code"] == "interpretation_schema_unsupported"
    assert "interpretation" not in refreshed.document()
    assert len(provider.calls) == calls_before
    service.stop()


# --- the packet says where its facts came from -------------------------------


def test_the_packet_carries_the_confirmed_spec_and_the_data_scope(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    task = _succeeded_run(orchestrator, service, provider)
    record = service.get_job(task.jobs[0])
    criterion = task.spec_confirmed[-1][2].criterion
    packet = report_claims.packet(
        question=task.raw_idea,
        criterion=criterion,
        report=_report_document(service, task.jobs[0]),
        report_identity=report_claims.identity_of(_report_document(service, task.jobs[0]), record),
        confirmed_spec=orchestrator._confirmed_projection(task),  # noqa: SLF001
        job=orchestrator._job_projection(record),  # noqa: SLF001
        data_scope=orchestrator._data_scope(record, _report_document(service, task.jobs[0])),  # noqa: SLF001
    )
    assert packet["confirmed_spec"]["strategy_id"] == task.spec_confirmed[-1][2].strategy_id
    assert packet["confirmed_spec"]["criterion"] == criterion
    assert packet["job"]["job_id"] == task.jobs[0]
    assert packet["data_scope"]["record_count"] == record.input_identity["record_count"]
    assert packet["data_scope"]["point_in_time"] is None
    assert packet["data_scope"]["quality_verified"] is False
    # The bounded time/input policy the run really used is projected; the two
    # facts this product does not hold stay named as unavailable.
    assert packet["data_scope"]["time_range"] == {
        "start_inclusive": "2026-01-02T09:31:00.000000Z",
        "end_exclusive": "2026-01-02T09:34:00.000000Z",
        "timezone": "UTC",
        "selection_field": "available_at",
    }
    assert packet["data_scope"]["unavailable"] == ["point_in_time", "quality_verified"]
    assert packet["host_added_limitations"] == [
        "no_out_of_sample_period",
        "no_benchmark_comparison",
        "deterministic_engine_no_uncertainty_estimate",
        "fees_are_modelled_not_observed",
        "backtest_only_no_live_execution",
        "coverage_unknown",
        "point_in_time_not_proven",
        "data_quality_not_independently_verified",
    ]
    assert "data_is_current_history_only" not in packet["host_added_limitations"]
    service.stop()


def test_a_conclusion_about_a_run_that_never_traded_is_insufficient_evidence(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    task = _succeeded_run(orchestrator, service, provider, scenario="quiet")
    report = _report_document(service, task.jobs[0])
    assert report["economics"]["counts"]["orders"] == 0
    assert report["economics"]["counts"]["fills"] == 0
    provider.responses.append(
        _response(
            [
                {
                    "id": "call-2",
                    "name": "conclude",
                    "input": {
                        "inference": "criterion_undefined_on_empty_activity",
                        "evidence_ref": {"field": CRITERION["field"]},
                    },
                }
            ]
        )
    )
    concluded = orchestrator.interpret(task.task_id)
    interpretation = concluded.interpretation
    assert interpretation is not None, concluded.interpretation_error
    assert interpretation["outcome"] == "insufficient_evidence"
    assert interpretation["inference"] == "criterion_undefined_on_empty_activity"
    assert interpretation["observed"]["state"] == "empty_activity"
    assert "value" not in interpretation["observed"]
    service.stop()
