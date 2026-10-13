"""Read-only Paper explanation conversations over one frozen evidence snapshot.

Every fixture here is a hand-built desktop adapter and model port: no process,
broker, clock or network is involved. The tests pin the properties the AI path
must never lose — an explicit call, no tool, no business write, a budget refused
before anything durable, and a recorded turn that is queried rather than re-sent.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ea.web.llm import ModelCapabilities, ModelResponse, ModelRoute, ModelStreamEvent
from ea.web.paper_explanation import (
    PaperExplanationBudgetError,
    PaperExplanationConflictError,
    PaperExplanationNotFoundError,
    PaperExplanationService,
)
from unit.test_web_api import ORIGIN, WRITE_HEADERS

RUN_A = "11111111-1111-4111-8111-111111111111"
RUN_B = "22222222-2222-4222-8222-222222222222"
ROUTE = ModelRoute(
    provider="fixture",
    protocol="fixture-messages",
    model="fixture-model",
    endpoint="https://fixture.invalid/v1/messages",
    capabilities=ModelCapabilities(tool_use=True, structured_output=False, usage=True),
)
BUDGET = {"max_model_calls": 1, "max_backtests": 0}


def snapshot(run_id: str, *, equity: str = "9998.8") -> dict[str, Any]:
    return {
        "schema": "ea.paper-explanation-evidence.v1",
        "run_id": run_id,
        "instance_name": "本地模拟",
        "candidate_id": "candidate-1",
        "source_job_id": "job-1",
        "strategy": {"id": "bounded-long", "version": 1},
        "configuration_sha256": "a" * 64,
        "market": {"kind": "local_synthetic_feed", "interval_seconds": 1.0, "bounded_prices": 3},
        "observed_at": "2026-10-13T00:00:00+00:00",
        "state": "running",
        "state_scope": "live",
        "historical": False,
        "terminal_durable": False,
        "economics": {
            "available": True,
            "reason": None,
            "equity": equity,
            "cash": "9998.8",
            "fills": 2,
        },
        "health": {
            "available": True,
            "reason": None,
            "runtime_ready": True,
            "trade_permitted": True,
            "permission_scope": "live",
        },
        "limits": ["行情来自本机生成的合成价格序列。"],
    }


class FakeDesktop:
    """A desktop adapter that records every effect it is asked to perform."""

    def __init__(self) -> None:
        self.snapshots: dict[str, dict[str, Any]] = {RUN_A: snapshot(RUN_A), RUN_B: snapshot(RUN_B)}
        self.reads: list[str] = []
        self.effects: list[str] = []

    def explanation_context(self, run_id: str) -> dict[str, Any]:
        self.reads.append(run_id)
        if run_id not in self.snapshots:
            raise FileNotFoundError("no receipt")
        return dict(self.snapshots[run_id])

    def start(self, *_args: Any) -> None:
        self.effects.append("start")

    def stop(self, *_args: Any) -> None:
        self.effects.append("stop")

    def resume(self, *_args: Any) -> None:
        self.effects.append("resume")

    def publish(self, *_args: Any) -> None:
        self.effects.append("publish")


class Port:
    """A streaming port whose answers are supplied whole per call."""

    def __init__(self, answers: list[Any] | None = None) -> None:
        self.answers = list(answers or [])
        self.calls: list[dict[str, Any]] = []

    def route(self) -> ModelRoute:
        return ROUTE

    def _next(self, kwargs: dict[str, Any]) -> Any:
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def complete(self, **kwargs: Any) -> ModelResponse:
        return self._next(kwargs)

    def stream(self, **kwargs: Any) -> Iterator[ModelStreamEvent]:
        answer = self._next(kwargs)
        for index in range(0, len(answer.text), 4):
            yield ModelStreamEvent("text_delta", text=answer.text[index : index + 4])
        yield ModelStreamEvent("response", response=answer)


def response(text: str, *, tool_calls: tuple[Any, ...] = ()) -> ModelResponse:
    return ModelResponse(
        text=text,
        tool_calls=tool_calls,
        stop_reason="tool_calls" if tool_calls else "stop",
        model="fixture-model",
        input_tokens=10,
        output_tokens=20,
    )


def service(tmp_path: Path, desktop: FakeDesktop, port: Port) -> PaperExplanationService:
    return PaperExplanationService(desktop, tmp_path, provider=port)


def run(
    service_: PaperExplanationService, run_id: str, call_id: str, text: str, budget: Any = BUDGET
) -> list[dict[str, Any]]:
    return list(service_.turn(run_id, call_id=call_id, text=text, approved_budget=budget))


def kind(events: list[dict[str, Any]]) -> list[str]:
    return [event["kind"] for event in events]


def test_one_explicit_call_streams_deltas_and_writes_no_business_state(tmp_path: Path) -> None:
    desktop, port = FakeDesktop(), Port([response("这是基于证据的解释。")])
    explanation = service(tmp_path, desktop, port)
    events = run(explanation, RUN_A, "call-1", "当前状态是什么？")
    assert kind(events) == ["state", "text_delta", "text_delta", "text_delta", "state", "done"]
    assert (
        "".join(event["text"] for event in events if event["kind"] == "text_delta")
        == "这是基于证据的解释。"
    )
    assert len(port.calls) == 1
    # No tool is ever offered, and no business effect is performed by an explanation.
    assert port.calls[0]["tools"] == []
    assert desktop.effects == []
    state = explanation.state(RUN_A)
    assert [message["role"] for message in state["messages"]] == ["user", "assistant"]
    assert state["messages"][1]["evidence_observed_at"] == "2026-10-13T00:00:00+00:00"
    assert state["messages"][1]["evidence_scope"] == "live"
    assert state["calls"][0]["status"] == "observed"


def test_an_unsolicited_tool_call_is_refused_and_never_executed(tmp_path: Path) -> None:
    from ea.web.llm import ToolCall

    tool = ToolCall(id="t-1", name="start_paper", arguments={})
    desktop, port = FakeDesktop(), Port([response("", tool_calls=(tool,))])
    explanation = service(tmp_path, desktop, port)
    events = run(explanation, RUN_A, "call-1", "帮我启动一个新的运行")
    assert events[-2]["kind"] == "state"
    assert any(
        event["kind"] == "error" and event["kind_reason"] == "tool_refused" for event in events
    )
    assert explanation.state(RUN_A)["calls"][0]["status"] == "refused"
    assert desktop.effects == []


@pytest.mark.parametrize(
    "budget",
    [
        {"max_model_calls": 0, "max_backtests": 0},
        {"max_model_calls": 5, "max_backtests": 0},
        {"max_model_calls": 1, "max_backtests": 1},
        {"max_model_calls": 1},
        {"max_model_calls": "1", "max_backtests": 0},
    ],
)
def test_an_invalid_budget_is_refused_before_any_reservation(tmp_path: Path, budget: Any) -> None:
    desktop, port = FakeDesktop(), Port([response("不该发生")])
    explanation = service(tmp_path, desktop, port)
    with pytest.raises(PaperExplanationBudgetError):
        run(explanation, RUN_A, "call-1", "问题", budget)
    assert port.calls == []
    assert desktop.reads == []
    assert explanation.state(RUN_A)["calls"] == []


def test_same_identity_replays_against_frozen_evidence_even_after_the_snapshot_changes(
    tmp_path: Path,
) -> None:
    desktop, port = FakeDesktop(), Port([response("第一次解释。")])
    explanation = service(tmp_path, desktop, port)
    run(explanation, RUN_A, "call-1", "状态如何？")
    desktop.snapshots[RUN_A] = snapshot(RUN_A, equity="1.0")
    replay = run(explanation, RUN_A, "call-1", "状态如何？")
    assert kind(replay) == ["state", "done"]
    assert len(port.calls) == 1
    assert len(desktop.reads) == 1  # a recorded turn never re-captures evidence
    state = explanation.state(RUN_A)
    assert state["messages"][1]["text"] == "第一次解释。"
    assert state["calls"][0]["evidence"]["economics"]["equity"] == "9998.8"


def test_a_changed_question_on_the_same_identity_conflicts(tmp_path: Path) -> None:
    desktop, port = FakeDesktop(), Port([response("第一次解释。")])
    explanation = service(tmp_path, desktop, port)
    run(explanation, RUN_A, "call-1", "状态如何？")
    with pytest.raises(PaperExplanationConflictError):
        run(explanation, RUN_A, "call-1", "换个问题")
    assert len(port.calls) == 1


def test_a_failed_call_is_recorded_unknown_and_never_re_sent(tmp_path: Path) -> None:
    from ea.web.llm import ModelProviderError

    desktop, port = FakeDesktop(), Port([ModelProviderError("timeout")])
    explanation = service(tmp_path, desktop, port)
    events = run(explanation, RUN_A, "call-1", "会不会重发？")
    assert any(event["kind"] == "error" for event in events)
    assert explanation.state(RUN_A)["calls"][0]["status"] == "unknown"
    replay = run(explanation, RUN_A, "call-1", "会不会重发？")
    assert kind(replay) == ["state", "done"]
    assert len(port.calls) == 1
    # A fresh explicit turn is still allowed after a visible failure.
    port.answers.append(response("新的回答。"))
    assert kind(run(explanation, RUN_A, "call-2", "再来一次")) == [
        "state",
        "text_delta",
        "text_delta",
        "state",
        "done",
    ]
    assert len(port.calls) == 2


def test_two_instances_keep_separate_conversations(tmp_path: Path) -> None:
    desktop = FakeDesktop()
    port = Port([response("A 的解释。"), response("B 的解释。")])
    explanation = service(tmp_path, desktop, port)
    run(explanation, RUN_A, "call-1", "A 的问题")
    run(explanation, RUN_B, "call-1", "B 的问题")
    assert explanation.state(RUN_A)["messages"][1]["text"] == "A 的解释。"
    assert explanation.state(RUN_B)["messages"][1]["text"] == "B 的解释。"
    assert desktop.reads == [RUN_A, RUN_B]


def test_an_unknown_instance_is_not_found_and_spends_nothing(tmp_path: Path) -> None:
    desktop, port = FakeDesktop(), Port([response("不该发生")])
    explanation = service(tmp_path, desktop, port)
    with pytest.raises(PaperExplanationNotFoundError):
        run(explanation, "33333333-3333-4333-8333-333333333333", "call-1", "问题")
    assert port.calls == []


def test_the_request_carries_only_a_bounded_suffix_of_complete_turns(tmp_path: Path) -> None:
    desktop = FakeDesktop()
    port = Port([response(f"回答 {index}") for index in range(11)])
    explanation = service(tmp_path, desktop, port)
    for index in range(11):
        run(explanation, RUN_A, f"call-{index}", f"问题 {index}")
    sent = port.calls[-1]["messages"]
    # 8 complete prior turns, each question and answer, then this turn's request.
    assert len(sent) == 8 * 2 + 1
    assert sent[0].text == "问题 2"
    assert "问题 0" not in "".join(message.text for message in sent)


def test_http_rejects_browser_supplied_evidence_and_unavailable_paper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ea.web.app import WebSettings, create_app

    desktop, port = FakeDesktop(), Port([response("HTTP 解释")])
    monkeypatch.setattr("ea.web.paper_explanation.create_model_port", lambda _settings: port)
    settings = WebSettings(
        scenario_root=tmp_path / "scenarios",
        workspace=tmp_path / "workspace",
        ui_dir=tmp_path / "ui",
        port=8765,
        desktop_paper=desktop,
    )
    settings.workspace.mkdir(parents=True, exist_ok=True)
    settings.scenario_root.mkdir()
    settings.ui_dir.mkdir()
    (settings.ui_dir / "index.html").write_text("fixture")
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        body = {"call_id": "call-1", "text": "问题", "approved_budget": BUDGET}
        # A browser may not supply evidence: the field is not part of the contract.
        rejected = client.post(
            f"/api/paper/instances/{RUN_A}/messages/stream",
            headers=WRITE_HEADERS,
            json={**body, "evidence": {"state": "running"}},
        )
        assert rejected.status_code == 422
        assert port.calls == []
        # GET is a read: it returns the frozen evidence and dispatch nothing.
        read = client.get(f"/api/paper/instances/{RUN_A}/conversation")
        assert read.status_code == 200
        assert read.json()["context"]["run_id"] == RUN_A
        assert port.calls == []
        sent = client.post(
            f"/api/paper/instances/{RUN_A}/messages/stream", headers=WRITE_HEADERS, json=body
        )
        assert sent.status_code == 200
        assert '"kind": "text_delta"' in sent.text
        assert len(port.calls) == 1
        desktop.snapshots.clear()
        restored = client.get(f"/api/paper/instances/{RUN_A}/conversation").json()
        assert restored["context"] is None
        assert restored["messages"][-1]["text"] == "HTTP 解释"
        assert len(port.calls) == 1
    unavailable = WebSettings(
        scenario_root=tmp_path / "s2", workspace=tmp_path / "w2", ui_dir=tmp_path / "ui2", port=81
    )
    unavailable.workspace.mkdir(parents=True, exist_ok=True)
    unavailable.scenario_root.mkdir()
    unavailable.ui_dir.mkdir()
    (unavailable.ui_dir / "index.html").write_text("fixture")
    with TestClient(create_app(unavailable), base_url="http://127.0.0.1:81") as client:
        assert client.get(f"/api/paper/instances/{RUN_A}/conversation").status_code == 409


def test_existing_research_database_and_paper_first_database_coexist(tmp_path: Path) -> None:
    from ea.web.user_strategies import UserStrategyStore

    research = UserStrategyStore(tmp_path)
    with research._write():
        pass
    desktop, port = FakeDesktop(), Port([response("共存")])
    explanation = service(tmp_path, desktop, port)
    assert explanation.state(RUN_A)["calls"] == []
    run(explanation, RUN_A, "call-1", "共存")
    assert research.model_call_usage()["reserved_model_calls"] == 1
    other = tmp_path / "paper-first"
    run(service(other, FakeDesktop(), Port([response("先运行")])), RUN_A, "call-1", "问题")
    assert not UserStrategyStore(other).is_conversation_migrated(RUN_A)


def test_interrupted_stream_recovers_unknown_without_repeating_dispatch(tmp_path: Path) -> None:
    desktop, port = FakeDesktop(), Port([response("一段尚未完成的解释")])
    explanation = service(tmp_path, desktop, port)
    stream = explanation.turn(RUN_A, call_id="lost", text="问题", approved_budget=BUDGET)
    assert next(stream)["kind"] == "state"
    assert next(stream)["kind"] == "text_delta"
    assert explanation.state(RUN_A)["calls"][0]["status"] == "dispatched"
    stream.close()
    reopened = service(tmp_path, desktop, port)
    assert reopened.state(RUN_A)["calls"][0]["status"] == "unknown"
    run(reopened, RUN_A, "lost", "问题")
    assert len(port.calls) == 1


def test_unconfigured_and_provider_error_details_are_safe(tmp_path: Path) -> None:
    class Unconfigured(Port):
        def route(self) -> None:
            return None

    no_model = service(tmp_path, FakeDesktop(), Unconfigured())
    run(no_model, RUN_A, "no-model", "问题")
    assert no_model.state(RUN_A)["calls"][0]["status"] == "unconfigured"
    broken = service(tmp_path, FakeDesktop(), Port([RuntimeError("SENSITIVE-ERROR")]))
    events = run(broken, RUN_B, "bad", "问题")
    assert "SENSITIVE-ERROR" not in str(events)


def test_a_missing_host_context_never_erases_recorded_answers(tmp_path: Path) -> None:
    desktop, port = FakeDesktop(), Port([response("原来的解释")])
    explanation = service(tmp_path, desktop, port)
    run(explanation, RUN_A, "saved", "问题")
    desktop.snapshots.clear()
    assert explanation.state(RUN_A)["messages"][-1]["text"] == "原来的解释"
    run(explanation, RUN_A, "saved", "问题")
    assert len(port.calls) == 1
