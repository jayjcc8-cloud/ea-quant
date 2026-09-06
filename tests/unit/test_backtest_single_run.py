from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import yaml

import ea.product.backtest as backtest_module
from ea.core import (
    CanonicalDecimal,
    CashReconciliationBalance,
    ReplayWindow,
    RunId,
    SignalDirection,
    StrategyContractError,
)
from ea.data import (
    create_phase1_historical_market_data_source,
    create_phase1_historical_market_source_bridge,
    decode_phase1_ohlcv_csv,
)
from ea.product import (
    BacktestRunError,
    BacktestRunFailure,
    load_backtest_scenario,
    run_backtest_scenario,
)
from ea.runtime import (
    create_active_market_dispatch_verifier,
    create_phase1_historical_market_runtime,
)
from ea.strategy import create_strategy_signal_authority


def _scenario(tmp_path: Path, **changes: object) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    data_path = tmp_path / "prices.csv"
    data_path.write_bytes(Path("src/ea/product/phase1_demo_ohlcv_v1.csv").resolve().read_bytes())
    window = ReplayWindow(
        datetime(2026, 1, 2, 9, 31, tzinfo=UTC),
        datetime(2026, 1, 2, 9, 33, tzinfo=UTC),
    )
    dataset = decode_phase1_ohlcv_csv(data_path.read_bytes(), replay_window=window)
    document: dict[str, object] = {
        "schema_version": 1,
        "data": {
            "path": "prices.csv",
            "start_utc": "2026-01-02T09:31:00.000000Z",
            "end_utc": "2026-01-02T09:33:00.000000Z",
            "fingerprint": {
                "sha256": dataset.selection.fingerprint.sha256.value,
                "record_count": dataset.selection.fingerprint.record_count,
            },
        },
        "instrument": {
            "venue": "XNAS",
            "symbol": "AAPL",
            "specification_id": "xnas.aapl.v1",
            "specification_set_id": "scenario.xnas.aapl.v1",
            "settlement_currency": "USD",
            "price_quantum": "0.01",
            "quantity_quantum": "1",
            "currency_quantum": "0.01",
            "contract_multiplier": "1",
        },
        "strategy": {"id": "bounded-long-v1", "target_quantity": "2"},
        "funding": {"currency": "USD", "initial_cash": "10000"},
        "risk": {
            "max_order_quantity": "5",
            "max_position_quantity": "5",
            "max_notional": "1000",
        },
        "execution": {"policy": "phase1.next-bar-close.v1"},
        "randomness_profile": "none",
    }
    for dotted, value in changes.items():
        owner = document
        parts = dotted.split("__")
        for part in parts[:-1]:
            nested = owner[part]
            assert isinstance(nested, dict)
            owner = nested
        if value is None:
            owner.pop(parts[-1], None)
        else:
            owner[parts[-1]] = value
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _report(result_directory: Path) -> dict[str, object]:
    value = json.loads((result_directory / "result.json").read_bytes())
    assert isinstance(value, dict)
    return value


def test_flat_scenario_preserves_funded_cash_without_trade(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(
        _scenario(
            tmp_path / "input",
            strategy__id="always-flat-v1",
            strategy__target_quantity=None,
        )
    )

    completed = run_backtest_scenario(scenario, (tmp_path / "runs").resolve())
    report = _report(completed.output_directory)

    assert report["status"] == "success"
    assert report["terminal_state"] == "completed"
    assert report["strategy"] == {"id": "always-flat-v1", "signal": "flat"}
    assert report["initial_funding"] == {
        "amount": "10000",
        "currency": "USD",
        "ledger_sequence": 1,
        "status": "applied",
    }
    assert report["order"] is None
    assert report["fill"] is None
    assert report["ending_cash"] == [{"amount": "10000", "currency": "USD"}]
    assert report["ending_positions"] == []
    assert report["reconciliation"] == {"cash": "match", "position": "not_required_empty"}


def test_affordable_bounded_long_runs_through_fill_and_funded_ledger(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))

    completed = run_backtest_scenario(scenario, (tmp_path / "runs").resolve())
    report = _report(completed.output_directory)

    assert report["risk"]["decision"] == "allow"  # type: ignore[index]
    assert report["order"]["quantity"] == "2"  # type: ignore[index]
    assert report["fill"]["quantity"] == "2"  # type: ignore[index]
    assert report["fill"]["price"] == "101.5"  # type: ignore[index]
    assert report["ending_cash"] == [{"amount": "9797", "currency": "USD"}]
    assert report["ending_positions"] == [{"quantity": "2", "symbol": "AAPL", "venue": "XNAS"}]
    assert report["ledger_sequence"] == 2
    assert report["reconciliation"] == {"cash": "match", "position": "match"}


def test_entry_delay_uses_later_canonical_market_root_and_execution_bar(
    tmp_path: Path,
) -> None:
    source = _scenario(tmp_path / "source", strategy__entry_delay_bars=0)
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    data_path = source.parent / "prices.csv"
    data_path.write_text(
        data_path.read_text(encoding="utf-8")
        + "1,XNAS,AAPL,2026-01-02T09:32:00.000000Z,2026-01-02T09:33:00.000000Z,"
        "raw,108.0,111.0,107.0,110.0,9.0,fixture.raw,3,0,"
        "2026-01-02T09:33:00.000000Z\n",
        encoding="utf-8",
    )
    window = ReplayWindow(
        datetime(2026, 1, 2, 9, 31, tzinfo=UTC),
        datetime(2026, 1, 2, 9, 34, tzinfo=UTC),
    )
    dataset = decode_phase1_ohlcv_csv(data_path.read_bytes(), replay_window=window)
    document["data"]["end_utc"] = "2026-01-02T09:34:00.000000Z"
    document["data"]["fingerprint"] = {
        "sha256": dataset.selection.fingerprint.sha256.value,
        "record_count": dataset.selection.fingerprint.record_count,
    }
    source.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    delayed_document = dict(document)
    delayed_document["strategy"] = dict(document["strategy"])
    delayed_document["strategy"]["entry_delay_bars"] = 2
    delayed = tmp_path / "source" / "delayed.yaml"
    delayed.write_text(yaml.safe_dump(delayed_document, sort_keys=False), encoding="utf-8")

    immediate_result = run_backtest_scenario(
        load_backtest_scenario(source),
        (tmp_path / "immediate-runs").resolve(),
    )
    delayed_result = run_backtest_scenario(
        load_backtest_scenario(delayed),
        (tmp_path / "delayed-runs").resolve(),
    )
    immediate_report = _report(immediate_result.output_directory)
    delayed_report = _report(delayed_result.output_directory)

    assert immediate_report["fill"]["price"] == "101.5"  # type: ignore[index]
    assert delayed_report["fill"]["price"] == "110"  # type: ignore[index]
    assert immediate_report["fill_evidence"]["occurred_at"] == (  # type: ignore[index]
        "2026-01-02T09:32:00.000000Z"
    )
    assert delayed_report["fill_evidence"]["occurred_at"] == (  # type: ignore[index]
        "2026-01-02T09:33:00.000000Z"
    )
    assert immediate_report["lineage_sha256"] != delayed_report["lineage_sha256"]


def test_order_limit_resizes_through_existing_risk_authority(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(
        _scenario(tmp_path / "input", strategy__target_quantity="10", risk__max_order_quantity="2")
    )

    completed = run_backtest_scenario(scenario, (tmp_path / "runs").resolve())
    report = _report(completed.output_directory)

    assert report["risk"]["decision"] == "resize"  # type: ignore[index]
    assert report["risk"]["effective_max_order_quantity"] == "2"  # type: ignore[index]
    assert report["fill"]["quantity"] == "2"  # type: ignore[index]


def test_insufficient_cash_rejects_without_success_or_trade(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input", funding__initial_cash="50"))

    with pytest.raises(BacktestRunFailure) as captured:
        run_backtest_scenario(scenario, (tmp_path / "runs").resolve())

    directory = captured.value.output_directory
    assert not (directory / "result.json").exists()
    failure = json.loads((directory / "failure.json").read_bytes())
    assert failure["code"] == "risk.rejected"
    assert failure["terminal_state"] == "failed"
    assert failure["order"] is None
    assert failure["fill"] is None


def test_max_notional_resizes_before_execution(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(
        _scenario(
            tmp_path / "input",
            strategy__target_quantity="10",
            risk__max_order_quantity="10",
            risk__max_position_quantity="10",
            risk__max_notional="150",
        )
    )

    completed = run_backtest_scenario(scenario, (tmp_path / "runs").resolve())
    report = _report(completed.output_directory)

    assert report["risk"]["decision"] == "resize"  # type: ignore[index]
    assert report["risk"]["notional_capacity"] == "1"  # type: ignore[index]
    assert report["fill"]["quantity"] == "1"  # type: ignore[index]


def test_max_position_resizes_before_execution(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(
        _scenario(
            tmp_path / "input",
            strategy__target_quantity="10",
            risk__max_order_quantity="10",
            risk__max_position_quantity="1",
            risk__max_notional="10000",
        )
    )

    completed = run_backtest_scenario(scenario, (tmp_path / "runs").resolve())
    report = _report(completed.output_directory)

    assert report["risk"]["decision"] == "resize"  # type: ignore[index]
    assert report["fill"]["quantity"] == "1"  # type: ignore[index]


def test_public_strategy_seam_rejects_future_market_payload(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    run_id = RunId("00000000-0000-4000-8000-000000000081")
    source = create_phase1_historical_market_data_source(scenario.dataset)
    runtime = create_phase1_historical_market_runtime(
        run_id=run_id,
        spec_set=scenario.spec_set,
        source=create_phase1_historical_market_source_bridge(source),
    )
    current = runtime.pop()
    future = scenario.dataset.selection.events[1]
    assert future.available_at > current.root.available_at
    authority = create_strategy_signal_authority(
        run_id=run_id,
        verifier=create_active_market_dispatch_verifier(runtime),
    )

    with pytest.raises(StrategyContractError):
        authority.issue(
            future,
            dispatch_sequence=current.dispatch_sequence,
            direction=SignalDirection.LONG,
        )

    assert authority.state.issuance_count == 0


def test_equivalent_fresh_attempts_have_distinct_run_ids_and_same_semantics(
    tmp_path: Path,
) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    root = (tmp_path / "runs").resolve()

    first = _report(run_backtest_scenario(scenario, root).output_directory)
    second = _report(run_backtest_scenario(scenario, root).output_directory)

    assert first["run_id"] != second["run_id"]
    assert first["lineage_sha256"] == second["lineage_sha256"]
    assert first["semantic_outcome_sha256"] == second["semantic_outcome_sha256"]


def test_run_uses_caller_reserved_attempt_identity(tmp_path: Path) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    reserved = RunId("00000000-0000-4000-8000-000000000170")

    completed = run_backtest_scenario(
        scenario,
        (tmp_path / "runs").resolve(),
        run_id=reserved,
    )
    report = _report(completed.output_directory)

    assert completed.output_directory.name == reserved.value
    assert report["run_id"] == reserved.value


def test_reconciliation_mismatch_retains_failure_without_success_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    original = backtest_module._observation

    def mismatching_observation(**kwargs: object) -> object:
        observation = original(**kwargs)  # type: ignore[arg-type]
        if kwargs["position"] is False:
            object.__setattr__(
                observation,
                "balances",
                (
                    CashReconciliationBalance(
                        scenario.funding_currency,
                        CanonicalDecimal("1"),
                    ),
                ),
            )
        return observation

    monkeypatch.setattr(backtest_module, "_observation", mismatching_observation)

    with pytest.raises(BacktestRunFailure) as captured:
        run_backtest_scenario(scenario, (tmp_path / "runs").resolve())

    directory = captured.value.output_directory
    failure = json.loads((directory / "failure.json").read_bytes())
    assert failure["code"] == "reconciliation.mismatch"
    assert failure["terminal_state"] == "failed"
    assert not (directory / "result.json").exists()


def test_existing_attempt_directory_fails_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = load_backtest_scenario(_scenario(tmp_path / "input"))
    output_root = (tmp_path / "runs").resolve()
    fixed = UUID("00000000-0000-4000-8000-000000000081")
    attempt = output_root / str(fixed)
    attempt.mkdir(parents=True)
    marker = attempt / "retained.txt"
    marker.write_text("original", encoding="utf-8")
    monkeypatch.setattr(backtest_module, "uuid4", lambda: fixed)

    with pytest.raises(BacktestRunError, match="fresh attempt"):
        run_backtest_scenario(scenario, output_root)

    assert marker.read_text(encoding="utf-8") == "original"
    assert not (attempt / "result.json").exists()
