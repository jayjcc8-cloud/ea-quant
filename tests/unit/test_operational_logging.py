from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ea.core.execution_identity import EconomicId, EconomicOwnerKind, economic_id_digest
from ea.core.run import RunId, Sha256Digest
from ea.observability import (
    JsonlFileSink,
    OperationalContext,
    OperationalLogger,
    operational_identity,
)

RUN_A = "550e8400-e29b-41d4-a716-446655440000"
RUN_B = "550e8400-e29b-41d4-a716-446655440001"


def test_jsonl_preserves_explicit_context_and_existing_identities() -> None:
    lines: list[str] = []
    context = OperationalContext(RUN_A, "bounded-long-v1", "candidate-a", "simulated-a")
    logger = OperationalLogger(context, lines.append)
    signal = EconomicId(RunId(RUN_A), EconomicOwnerKind.STRATEGY_SIGNAL, 1)
    order = EconomicId(RunId(RUN_A), EconomicOwnerKind.EXECUTION_ORDER, 1)
    fill = EconomicId(RunId(RUN_A), EconomicOwnerKind.EXECUTION_FILL, 1)
    assert logger.emit(
        "execution.fill",
        correlation_id=signal,
        order_id=order,
        fill_id=fill,
        client_order_id=Sha256Digest("a" * 64),
        market_event_id="market-1",
        outcome="accepted",
        quantity="1.25",
        dispatch_sequence=3,
    )
    assert lines[0].endswith("\n") and lines[0].count("\n") == 1
    document = json.loads(lines[0])
    assert document["schema"] == "ea.operational-log.v1"
    assert document["schema_version"] == 1
    assert document["sequence"] == 1
    assert document["run_id"] == RUN_A
    assert document["strategy_id"] == "bounded-long-v1"
    assert document["candidate_id"] == "candidate-a"
    assert document["account_id"] == "simulated-a"
    assert document["operation"] == "run"
    assert document["correlation_id"] == economic_id_digest(signal).value
    assert document["order_id"] == economic_id_digest(order).value
    assert document["fill_id"] == economic_id_digest(fill).value
    assert document["client_order_id"] == "a" * 64
    assert document["quantity"] == "1.25"
    assert document["dispatch_sequence"] == 3
    assert datetime.fromisoformat(document["timestamp"]).tzinfo == UTC
    assert logger.failure_count == 0 and logger.last_failure is None


def test_explicit_contexts_stay_isolated_across_worker_threads() -> None:
    lines: list[str] = []

    def execute(run_id: str, strategy: str) -> None:
        logger = OperationalLogger(OperationalContext(run_id, strategy), lines.append)
        for _ in range(10):
            assert logger.emit("strategy.decision", outcome="hold")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(execute, RUN_A, "alpha"),
            executor.submit(execute, RUN_B, "beta"),
        ]
        for future in futures:
            future.result()
    records = [json.loads(line) for line in lines]
    for run_id, strategy in ((RUN_A, "alpha"), (RUN_B, "beta")):
        selected = [record for record in records if record["run_id"] == run_id]
        assert [record["sequence"] for record in selected] == list(range(1, 11))
        assert {record["strategy_id"] for record in selected} == {strategy}
        assert all(
            record["candidate_id"] is None and record["account_id"] is None for record in selected
        )
    context = OperationalContext(RUN_A)
    field = "run_id"
    with pytest.raises(FrozenInstanceError):
        setattr(context, field, RUN_B)


def test_sink_failure_is_observable_without_propagating_exception_details() -> None:
    lines: list[str] = []

    def sink(line: str) -> None:
        if not lines:
            lines.append("failed")
            raise OSError("SECRET must never be serialized")
        lines.append(line)

    logger = OperationalLogger(OperationalContext(RUN_A), sink)
    assert not logger.emit("run.started")
    assert logger.failure_count == 1 and logger.last_failure == "sink"
    assert logger.emit("run.completed", outcome="success")
    record = json.loads(lines[1])
    assert record["sequence"] == 2 and record["dropped_events"] == 1
    assert "SECRET" not in lines[1]


@pytest.mark.parametrize(
    "fields",
    [
        {"run_id": RUN_B},
        {"schema": "spoofed"},
        {"schema_version": 2},
        {"strategy_id": "spoofed"},
        {"sequence": 99},
        {"timestamp": "spoofed"},
        {"operation": "spoofed"},
        {"dropped_events": 0},
        {"payload": {"nested": "data"}},
        {"value": float("nan")},
        {"value": "x" * 513},
        {"value": "embedded\nline"},
        {"exception": "sensitive error text"},
        {"api_key": "sensitive key"},
        {"bad-name": "value"},
    ],
)
def test_unsafe_or_reserved_fields_are_dropped_without_mutation(fields: dict[str, Any]) -> None:
    lines: list[str] = []
    logger = OperationalLogger(OperationalContext(RUN_A), lines.append)
    assert not logger.emit("run.completed", **fields)
    assert lines == []
    assert logger.failure_count == 1 and logger.last_failure == "serialization"
    assert logger.emit("run.completed")
    assert json.loads(lines[0])["run_id"] == RUN_A


def test_unknown_objects_never_use_repr_and_identity_encoder_is_stable() -> None:
    class Sensitive:
        def __str__(self) -> str:
            raise AssertionError("do not stringify arbitrary objects")

        def __repr__(self) -> str:
            raise AssertionError("do not render arbitrary objects")

    logger = OperationalLogger(OperationalContext(RUN_A), lambda _: None)
    assert not logger.emit("run.failed", order_id=Sensitive())
    with pytest.raises(TypeError):
        operational_identity(Sensitive())
    assert operational_identity("known-id") == "known-id"
    assert operational_identity(None) is None


def test_event_and_field_limits_are_bounded_and_optional_ids_remain_absent() -> None:
    lines: list[str] = []
    logger = OperationalLogger(OperationalContext(RUN_A, operation="resume"), lines.append)
    assert not logger.emit("unsafe\nevent")
    excessive: dict[str, Any] = {f"field_{index}": index for index in range(33)}
    assert not logger.emit("run.started", **excessive)
    assert logger.emit("run.started", enabled=True, value=1.5, optional=None)
    record = json.loads(lines[0])
    assert record["operation"] == "resume"
    assert record["enabled"] is True
    assert record["value"] == 1.5 and record["optional"] is None
    assert "order_id" not in record
    assert record["dropped_events"] == 2


def test_file_sink_appends_parseable_records_and_does_not_create_missing_parents(
    tmp_path: Path,
) -> None:
    path = tmp_path / "operational.jsonl"
    logger = OperationalLogger(OperationalContext(RUN_A), JsonlFileSink(path))
    assert logger.emit("run.started")
    assert logger.emit("run.completed")
    assert [json.loads(line)["sequence"] for line in path.read_text().splitlines()] == [1, 2]
    unavailable = OperationalLogger(
        OperationalContext(RUN_B), JsonlFileSink(tmp_path / "absent" / "operational.jsonl")
    )
    assert not unavailable.emit("run.started")
    assert unavailable.last_failure == "sink"
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("link_type", ["symlink", "hardlink"])
def test_file_sink_refuses_shared_files_without_changing_target(
    tmp_path: Path, link_type: str
) -> None:
    target = tmp_path / "economic-evidence.json"
    target.write_text("unchanged\n")
    path = tmp_path / "operational.jsonl"
    if link_type == "symlink":
        path.symlink_to(target)
    else:
        path.hardlink_to(target)
    logger = OperationalLogger(OperationalContext(RUN_A), JsonlFileSink(path))
    assert not logger.emit("run.started")
    assert logger.failure_count == 1
    assert target.read_text() == "unchanged\n"
