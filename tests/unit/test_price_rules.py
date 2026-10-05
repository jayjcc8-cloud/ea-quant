from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, cast

import pytest

from ea.core.economics import CanonicalDecimal
from ea.strategy.registry import BUILTIN_STRATEGIES, resolve_parameters
from ea.strategy.rules import DEFAULT_RULE, PriceRuleLogic, decode_rule
from ea.web.research import ResearchOrchestrator, ResearchStore
from ea.web.service import WebService
from ea.web.settings import RadianSettings
from unit.test_web_research import FakeProvider, _response

PRICE = {"kind": "price", "field": "close"}
SMA = {"kind": "sma", "window": 2}
GT = {"op": "gt", "left": PRICE, "right": SMA}
LT = {"op": "lt", "left": PRICE, "right": SMA}


def rule(when: Any) -> str:
    return json.dumps({"version": 1, "when": when, "action": "ENTER_LONG"})


@pytest.mark.parametrize(
    "bad",
    [
        rule({"op": "eval", "code": "1"}),
        rule({"op": "gt", "left": {**PRICE, "offset": -1}, "right": SMA}),
        rule({"op": "gt", "left": PRICE, "right": {"kind": "sma", "window": True}}),
        rule({"op": "gt", "left": PRICE, "right": {"kind": "sma", "window": 65}}),
        rule({"op": "gt", "left": PRICE, "right": {"kind": "sma", "window": 0}}),
        rule({"op": "all", "args": []}),
        rule(
            {
                "op": "gt",
                "left": {"kind": "price", "field": "future_close"},
                "right": SMA,
            }
        ),
        DEFAULT_RULE.replace('"ENTER_LONG"', '"EXIT_LONG"'),
        DEFAULT_RULE.replace('"version":1', '"version":true'),
        DEFAULT_RULE.replace('"version":1', '"version":1,"version":1'),
        rule(
            {
                "op": "not",
                "arg": {
                    "op": "not",
                    "arg": {
                        "op": "not",
                        "arg": {"op": "not", "arg": {"op": "not", "arg": GT}},
                    },
                },
            }
        ),
        rule({"op": "all", "args": [{"op": "all", "args": [GT] * 8}] * 8}),
        "x" * 4097,
    ],
)
def test_reject_closed_contract(bad: str) -> None:
    with pytest.raises(ValueError):
        decode_rule(bad)


def test_canonical_and_dataset_boundary() -> None:
    parsed = decode_rule(DEFAULT_RULE)
    assert decode_rule(parsed.canonical).canonical == parsed.canonical
    entry = BUILTIN_STRATEGIES.get("price-rule-entry-v1", 1)
    assert (
        entry.normalize({"rule": DEFAULT_RULE, "target_quantity": "2"})["rule"] == parsed.canonical
    )
    with pytest.raises(ValueError, match="history"):
        resolve_parameters(
            "price-rule-entry-v1",
            1,
            {"rule": DEFAULT_RULE, "target_quantity": "2"},
            quantity_quantum=CanonicalDecimal("1"),
            last_entry_index=0,
        )


def test_warmup_not_and_single_entry() -> None:
    from ea.product.scenario import load_backtest_scenario

    root = Path(__file__).parents[2] / "examples" / "web-scenarios"
    scenario = load_backtest_scenario(root / "moving-average-entry.yaml")
    events = scenario.dataset.selection.events
    logic = PriceRuleLogic({"rule": rule({"op": "not", "arg": LT}), "target_quantity": "2"})
    assert logic.on_event(events[0]) is None
    decisions = [logic.on_event(event) for event in events[1:]]
    assert decisions.count(CanonicalDecimal("2")) == 1


def wait(service: WebService, job_id: str) -> Any:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = service.get_job(job_id)
        if job.status in {"succeeded", "failed"}:
            assert job.status == "succeeded", job.document()
            return job
        time.sleep(0.02)
    raise AssertionError("backtest did not finish")


@pytest.mark.parametrize(
    "condition",
    [
        GT,
        {
            "op": "all",
            "args": [
                LT,
                {"op": "not", "arg": {"op": "eq", "left": PRICE, "right": SMA}},
            ],
        },
    ],
)
def test_two_rules_real_research_holdout_candidate(tmp_path: Path, condition: Any) -> None:
    root = tmp_path / "scenarios"
    root.mkdir()
    examples = Path(__file__).parents[2] / "examples" / "web-scenarios"
    for name in (
        "moving-average-entry.csv",
        "moving-average-holdout.csv",
        "price-rule-entry.yaml",
        "price-rule-holdout.yaml",
    ):
        shutil.copyfile(examples / name, root / name)
    service = WebService(root, tmp_path / "workspace")
    service.start()
    try:
        orchestrator = ResearchOrchestrator(
            ResearchStore(tmp_path / "workspace"),
            service,
            RadianSettings(tmp_path / "settings.json"),
        )
        expression = rule(condition)
        provider = FakeProvider(
            [
                _response(
                    [
                        {
                            "id": "r1",
                            "name": "propose_spec",
                            "input": {
                                "strategy_id": "price-rule-entry-v1",
                                "instrument": {"venue": "XNAS", "symbol": "AAPL"},
                                "scenario_id": "price-rule-entry.yaml",
                                "initial_cash": "10000",
                                "parameters": {
                                    "rule": expression,
                                    "target_quantity": "2",
                                },
                                "rationale": "Fixture contract, no real model claim",
                            },
                        }
                    ]
                )
            ]
        )
        cast(Any, orchestrator)._provider = provider
        task = orchestrator.create("Validate this bounded price rule")
        assert task.status == "awaiting_confirmation", task.error
        confirmed = orchestrator.confirm(task.task_id)
        source = wait(service, confirmed.jobs[0])
        assert orchestrator.refresh(task.task_id).status == "succeeded"
        relation = service.create_holdout(
            source_job_id=source.job_id, scenario_id="price-rule-holdout.yaml"
        )
        wait(service, relation["holdout_job_id"])
        candidate = service.create_candidate(relation["validation_id"])
        assert candidate["status"] == "EVALUATED"
        # Frozen source and holdout preserve the same canonical rule and implementation ID.
        for job_id in (source.job_id, relation["holdout_job_id"]):
            snapshot_bytes = service.get_job(job_id).input_snapshot_bytes
            assert snapshot_bytes is not None
            snapshot = json.loads(snapshot_bytes)
            assert (
                decode_rule(snapshot["scenario"]["strategy"]["parameters"]["rule"]).canonical
                == decode_rule(expression).canonical
            )
        assert service.get_candidate(candidate["candidate_id"]) == candidate
        (tmp_path / "c1-evidence.json").write_text(
            json.dumps(
                {
                    "condition": condition,
                    "source_job": source.job_id,
                    "holdout": relation,
                    "candidate": candidate,
                },
                default=str,
            )
        )
    finally:
        service.stop()


def test_rule_history_counts_only_eligible_raw_bars() -> None:
    with pytest.raises(ValueError, match="history"):
        resolve_parameters(
            "price-rule-entry-v1",
            1,
            {"rule": DEFAULT_RULE, "target_quantity": "2"},
            quantity_quantum=CanonicalDecimal("1"),
            last_entry_index=1,
            history_bars=1,
        )


@pytest.mark.parametrize("overrides", [{}, {"rule": DEFAULT_RULE}])
def test_rule_proposal_fills_registered_defaults(tmp_path: Path, overrides: Any) -> None:
    root = tmp_path / "scenarios"
    root.mkdir()
    examples = Path(__file__).parents[2] / "examples" / "web-scenarios"
    for name in ("moving-average-entry.csv", "price-rule-entry.yaml"):
        shutil.copyfile(examples / name, root / name)
    service = WebService(root, tmp_path / "workspace")
    service.start()
    try:
        orchestrator = ResearchOrchestrator(
            ResearchStore(tmp_path / "workspace"),
            service,
            RadianSettings(tmp_path / "settings.json"),
        )
        provider = FakeProvider(
            [
                _response(
                    [
                        {
                            "id": "defaults",
                            "name": "propose_spec",
                            "input": {
                                "strategy_id": "price-rule-entry-v1",
                                "instrument": {"venue": "XNAS", "symbol": "AAPL"},
                                "scenario_id": "price-rule-entry.yaml",
                                "initial_cash": "10000",
                                "parameters": overrides,
                                "rationale": "Use registered defaults",
                            },
                        }
                    ]
                )
            ]
        )
        cast(Any, orchestrator)._provider = provider
        task = orchestrator.create("Use default quantity")
        assert task.status == "awaiting_confirmation", task.error
        assert task.spec_draft is not None
        assert task.spec_draft.parameters["target_quantity"] == "2"
        assert (
            decode_rule(task.spec_draft.parameters["rule"]).canonical
            == decode_rule(DEFAULT_RULE).canonical
        )
        assert task.jobs == ()
    finally:
        service.stop()
