from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from ea.product import BacktestScenarioError, load_backtest_scenario
from unit.test_backtest_report import _priced_scenario


def write_v2(tmp_path: Path) -> Path:
    path = _priced_scenario(tmp_path)
    document = yaml.safe_load(path.read_text())
    document["schema_version"] = 2
    document["strategy"] = {
        "id": "moving-average-entry-v1",
        "version": 1,
        "parameters": {"fast_window": 1, "slow_window": 2, "target_quantity": "2"},
    }
    path.write_text(yaml.safe_dump(document))
    return path


def test_v2_loads_and_binds_all_parameters(tmp_path: Path) -> None:
    path = write_v2(tmp_path)
    loaded = load_backtest_scenario(path)
    assert json.loads(loaded.canonical_bytes)["canonicalization"] == "ea-backtest-scenario-v2"
    assert loaded.strategy_parameters == {
        "fast_window": 1,
        "slow_window": 2,
        "target_quantity": "2",
    }
    document = yaml.safe_load(path.read_text())
    document["strategy"]["parameters"]["target_quantity"] = "3"
    path.write_text(yaml.safe_dump(document))
    assert load_backtest_scenario(path).scenario_sha256 != loaded.scenario_sha256


def test_v2_requires_enough_history_and_next_execution_bar(tmp_path: Path) -> None:
    path = write_v2(tmp_path)
    document = yaml.safe_load(path.read_text())
    document["strategy"]["parameters"]["slow_window"] = 3
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(BacktestScenarioError):
        load_backtest_scenario(path)


def test_moving_average_runs_existing_economic_and_report_path(tmp_path: Path) -> None:
    from ea.product import generate_backtest_report, run_backtest_scenario

    path = write_v2(tmp_path / "source")
    result = run_backtest_scenario(load_backtest_scenario(path), output_root=tmp_path / "runs")
    report = generate_backtest_report(
        run_dir=result.output_directory, output_dir=tmp_path / "report"
    )
    assert report is not None


def test_web_v2_generic_summary_and_parameter_validation(tmp_path: Path) -> None:
    from ea.web.service import WebService

    path = write_v2(tmp_path / "scenarios")
    service = WebService(path.parent, tmp_path / "workspace")
    summary = cast(
        dict[str, Any],
        service.validate_scenario(
            path.name,
            parameters={
                "initial_cash": "10000",
                "strategy_parameters": {"fast_window": 1, "slow_window": 2, "target_quantity": "3"},
            },
        ),
    )
    assert [p["name"] for p in summary["strategy_parameters"]] == [
        "fast_window",
        "slow_window",
        "target_quantity",
    ]
    assert summary["summary"]["parameters"]["target_quantity"] == "3"


def test_all_frozen_v1_example_identities_are_unchanged() -> None:
    fixtures = json.loads(Path("tests/fixtures/scenario-v1-identities.json").read_text())
    assert fixtures
    for name, expected in fixtures.items():
        loaded = load_backtest_scenario(Path(name))
        assert loaded.canonical_bytes.decode() == expected["canonical"]
        assert loaded.scenario_sha256.value == expected["sha256"]


@pytest.mark.parametrize(
    "parameters",
    [
        {"fast_window": 1, "slow_window": 2, "target_quantity": "2.0"},
        {"fast_window": 1, "slow_window": 2, "target_quantity": "2", "unexpected": 1},
        {"fast_window": 1, "slow_window": 2},
        {"fast_window": True, "slow_window": 2, "target_quantity": "2"},
    ],
)
def test_v2_rejects_noncanonical_and_unclosed_inputs(
    tmp_path: Path, parameters: dict[str, object]
) -> None:
    path = write_v2(tmp_path)
    document = yaml.safe_load(path.read_text())
    document["strategy"]["parameters"] = parameters
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(BacktestScenarioError):
        load_backtest_scenario(path)


def test_v2_rejects_duplicate_yaml_and_unknown_fields(tmp_path: Path) -> None:
    path = write_v2(tmp_path)
    original = path.read_text()
    for suffix in ("\nschema_version: 2\n", "\nunknown: 1\n"):
        path.write_text(original + suffix)
        with pytest.raises(BacktestScenarioError):
            load_backtest_scenario(path)


def test_v2_canonical_order_and_strategy_version_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from ea.strategy.registry import BUILTIN_STRATEGIES

    path = write_v2(tmp_path)
    first = load_backtest_scenario(path)
    document = yaml.safe_load(path.read_text())
    document["strategy"]["parameters"] = dict(
        reversed(list(document["strategy"]["parameters"].items()))
    )
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    assert load_backtest_scenario(path).canonical_bytes == first.canonical_bytes
    entry = BUILTIN_STRATEGIES.get("moving-average-entry-v1", 1)
    monkeypatch.setitem(
        BUILTIN_STRATEGIES._entries,
        "moving-average-entry-v1",
        replace(entry, descriptor=replace(entry.descriptor, strategy_version=2)),
    )
    document["strategy"]["version"] = 2
    path.write_text(yaml.safe_dump(document))
    assert load_backtest_scenario(path).scenario_sha256 != first.scenario_sha256


def test_third_builtin_uses_generic_backend_without_orchestration_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ea.strategy.registry import (
        BUILTIN_STRATEGIES,
        EntryLogic,
        ParameterV1,
        StrategyDescriptorV1,
        StrategyEntryV1,
    )
    from ea.web.service import WebService

    entry = StrategyEntryV1(
        StrategyDescriptorV1(
            1,
            "third-reference",
            1,
            "Third reference",
            True,
            (ParameterV1("unfamiliar_window", "integer", True, 2, 1, 5),),
        ),
        lambda parameters: None,
        lambda parameters: EntryLogic({}, mode="flat"),
        lambda orders, fills: None,
    )
    monkeypatch.setitem(BUILTIN_STRATEGIES._entries, "third-reference", entry)
    path = write_v2(tmp_path / "source")
    document = yaml.safe_load(path.read_text())
    document["strategy"] = {
        "id": "third-reference",
        "version": 1,
        "parameters": {"unfamiliar_window": 3},
    }
    path.write_text(yaml.safe_dump(document))
    service = WebService(path.parent, tmp_path / "workspace")
    summary = cast(
        dict[str, Any],
        service.validate_scenario(
            path.name,
            parameters={"initial_cash": "20000", "strategy_parameters": {"unfamiliar_window": 4}},
        ),
    )
    assert summary["summary"]["parameters"] == {"unfamiliar_window": 4}
    assert summary["strategy_parameters"][0]["name"] == "unfamiliar_window"
    from ea.product import generate_backtest_report, run_backtest_scenario

    result = run_backtest_scenario(load_backtest_scenario(path), output_root=tmp_path / "runs")
    generate_backtest_report(result.output_directory, tmp_path / "report")
