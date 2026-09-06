from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from ea.core import ReplayWindow
from ea.data import decode_phase1_ohlcv_csv
from ea.product import (
    BacktestScenarioError,
    load_backtest_scenario,
    parameterize_backtest_scenario,
)


def _write_valid_scenario(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    data_path = tmp_path / "prices.csv"
    data_path.write_bytes(Path("src/ea/product/phase1_demo_ohlcv_v1.csv").resolve().read_bytes())
    window = ReplayWindow(
        datetime(2026, 1, 2, 9, 31, tzinfo=UTC),
        datetime(2026, 1, 2, 9, 33, tzinfo=UTC),
    )
    dataset = decode_phase1_ohlcv_csv(data_path.read_bytes(), replay_window=window)
    document = {
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
    scenario_path = tmp_path / "scenario.yaml"
    scenario_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return scenario_path


def test_loads_strict_funded_bounded_long_scenario(tmp_path: Path) -> None:
    scenario_path = _write_valid_scenario(tmp_path)

    loaded = load_backtest_scenario(scenario_path)

    assert loaded.scenario_path == scenario_path.resolve()
    assert loaded.data_path == (tmp_path / "prices.csv").resolve()
    assert loaded.strategy_id.value == "bounded-long-v1"
    assert loaded.target_quantity is not None
    assert loaded.target_quantity.text == "2"
    assert loaded.entry_delay_bars == 0
    assert loaded.entry_delay_bars_maximum == 1
    assert json.loads(loaded.canonical_bytes)["strategy"]["entry_delay_bars"] == 0
    assert loaded.initial_cash.text == "10000"
    assert loaded.randomness.document() == {
        "profile": "none",
        "master_seed": "not_applicable",
    }
    assert loaded.dataset.selection.fingerprint.record_count == 3


def test_entry_delay_maximum_uses_next_bar_eligibility_not_record_count(
    tmp_path: Path,
) -> None:
    scenario_path = _write_valid_scenario(tmp_path)
    data_path = tmp_path / "prices.csv"
    data_path.write_text(
        "schema_version,venue,symbol,interval_start,interval_end,adjustment,open,high,low,close,volume,source,source_sequence,revision,available_at\n"
        "1,XNAS,AAPL,2026-01-02T09:30:00.000000Z,2026-01-02T09:31:00.000000Z,raw,100,101,99,100.5,10,fixture.raw,0,0,2026-01-02T09:31:00.000000Z\n"
        "1,XNAS,AAPL,2026-01-02T09:31:00.000000Z,2026-01-02T09:32:00.000000Z,raw,101,102,100,101.5,11,fixture.raw,1,0,2026-01-02T09:32:00.000000Z\n"
        "1,XNAS,AAPL,2026-01-02T09:31:00.000000Z,2026-01-02T09:32:00.000000Z,raw,101,103,100,102,12,fixture.raw,2,1,2026-01-02T09:32:30.000000Z\n",
        encoding="utf-8",
    )
    window = ReplayWindow(
        datetime(2026, 1, 2, 9, 31, tzinfo=UTC),
        datetime(2026, 1, 2, 9, 33, tzinfo=UTC),
    )
    dataset = decode_phase1_ohlcv_csv(data_path.read_bytes(), replay_window=window)
    document = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    document["data"]["fingerprint"] = {
        "sha256": dataset.selection.fingerprint.sha256.value,
        "record_count": dataset.selection.fingerprint.record_count,
    }
    scenario_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    loaded = load_backtest_scenario(scenario_path)

    assert loaded.dataset.selection.fingerprint.record_count == 3
    assert loaded.entry_delay_bars_maximum == 0


def test_bounded_long_rejects_when_only_later_root_is_not_matcher_eligible(
    tmp_path: Path,
) -> None:
    scenario_path = _write_valid_scenario(tmp_path)
    data_path = tmp_path / "prices.csv"
    data_path.write_text(
        "schema_version,venue,symbol,interval_start,interval_end,adjustment,open,high,low,close,volume,source,source_sequence,revision,available_at\n"
        "1,XNAS,AAPL,2026-01-02T09:30:00.000000Z,2026-01-02T09:31:00.000000Z,raw,100,101,99,100.5,10,fixture.raw,0,0,2026-01-02T09:31:00.000000Z\n"
        "1,XNAS,AAPL,2026-01-02T09:31:00.000000Z,2026-01-02T09:32:00.000000Z,raw,101,103,100,102,12,fixture.raw,1,1,2026-01-02T09:32:30.000000Z\n",
        encoding="utf-8",
    )
    window = ReplayWindow(
        datetime(2026, 1, 2, 9, 31, tzinfo=UTC),
        datetime(2026, 1, 2, 9, 33, tzinfo=UTC),
    )
    dataset = decode_phase1_ohlcv_csv(data_path.read_bytes(), replay_window=window)
    document = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    document["data"]["fingerprint"] = {
        "sha256": dataset.selection.fingerprint.sha256.value,
        "record_count": dataset.selection.fingerprint.record_count,
    }
    scenario_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(
        BacktestScenarioError,
        match="bounded-long-v1 requires a market bar with an executable next bar",
    ):
        load_backtest_scenario(scenario_path)


def test_parameterizes_cash_and_strategy_parameters_with_new_canonical_identity(
    tmp_path: Path,
) -> None:
    source = load_backtest_scenario(_write_valid_scenario(tmp_path))

    parameterized = parameterize_backtest_scenario(
        source,
        initial_cash="12000",
        quantity="4",
        entry_delay_bars=1,
    )

    document = json.loads(parameterized.canonical_bytes)
    assert parameterized.initial_cash.text == "12000"
    assert parameterized.target_quantity is not None
    assert parameterized.target_quantity.text == "4"
    assert parameterized.entry_delay_bars == 1
    assert document["funding"]["initial_cash"] == "12000"
    assert document["strategy"]["target_quantity"] == "4"
    assert document["strategy"]["entry_delay_bars"] == 1
    assert parameterized.instrument == source.instrument
    assert parameterized.spec_set == source.spec_set
    assert parameterized.dataset is source.dataset
    assert parameterized.data_path == source.data_path
    assert document["instrument"]["symbol"] == "AAPL"
    assert document["instrument"]["venue"] == "XNAS"
    assert (
        parameterized.scenario_sha256.value
        == hashlib.sha256(b"ea.backtest-scenario.v1\0" + parameterized.canonical_bytes).hexdigest()
    )
    assert parameterized.scenario_sha256 != source.scenario_sha256
    assert source.initial_cash.text == "10000"
    assert source.target_quantity is not None
    assert source.target_quantity.text == "2"
    assert source.entry_delay_bars == 0


@pytest.mark.parametrize("entry_delay_bars", [-1, 2])
def test_parameter_contract_rejects_entry_delay_outside_dynamic_bounds(
    tmp_path: Path,
    entry_delay_bars: int,
) -> None:
    source = load_backtest_scenario(_write_valid_scenario(tmp_path))

    with pytest.raises(BacktestScenarioError, match="entry_delay_bars"):
        parameterize_backtest_scenario(
            source,
            initial_cash="10000",
            quantity="2",
            entry_delay_bars=entry_delay_bars,
        )


@pytest.mark.parametrize(
    ("initial_cash", "quantity", "message"),
    [
        ("0", "2", "initial_cash must be strictly positive"),
        ("10000.00", "2", "initial_cash must be an ea-decimal-v1 string"),
        ("10000", "1.5", "quantity is not quantized"),
        ("10000", None, "bounded-long-v1 requires quantity"),
    ],
)
def test_parameter_contract_rejects_invalid_editable_values(
    tmp_path: Path,
    initial_cash: str,
    quantity: str | None,
    message: str,
) -> None:
    source = load_backtest_scenario(_write_valid_scenario(tmp_path))

    with pytest.raises(BacktestScenarioError, match=message):
        parameterize_backtest_scenario(
            source,
            initial_cash=initial_cash,
            quantity=quantity,
        )


def test_parameter_contract_forbids_quantity_for_registered_flat_strategy(tmp_path: Path) -> None:
    scenario_path = _change(
        _change(_write_valid_scenario(tmp_path), "strategy.id", "always-flat-v1"),
        "strategy.target_quantity",
        None,
    )
    source = load_backtest_scenario(scenario_path)

    with pytest.raises(BacktestScenarioError, match="always-flat-v1 forbids quantity"):
        parameterize_backtest_scenario(source, initial_cash="10000", quantity="1")


def _change(path: Path, dotted: str, value: object) -> Path:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    owner = document
    parts = dotted.split(".")
    for part in parts[:-1]:
        nested = owner[part]
        assert isinstance(nested, dict)
        owner = nested
    owner[parts[-1]] = value
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 2, "unsupported_schema_version"),
        ("funding.initial_cash", 10000, "string_type"),
        ("funding.initial_cash", "0", "strictly positive"),
        ("strategy.target_quantity", "1.5", "not quantized"),
        ("risk.max_order_quantity", "0", "strictly positive"),
        ("risk.max_position_quantity", "1.5", "not quantized"),
        ("risk.max_notional", "0", "strictly positive"),
        ("funding.currency", "EUR", "funding currency conflicts"),
        ("instrument.symbol", "MSFT", "instrument conflicts"),
        ("data.start_utc", "2026-01-02T09:31:00+00:00", "canonical UTC"),
        ("data.end_utc", "2026-01-02T09:30:00.000000Z", "earlier"),
        ("data.path", "missing.csv", "cannot be resolved"),
        ("data.fingerprint.sha256", "0" * 64, "fingerprint conflicts"),
        ("strategy.id", "future-strategy-v1", "enum"),
        ("execution.policy", "same-bar-close-v1", "literal_error"),
        ("randomness_profile", "fixed", "literal_error"),
    ],
)
def test_scenario_rejects_invalid_or_inconsistent_inputs(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    scenario_path = _change(_write_valid_scenario(tmp_path), field, value)

    with pytest.raises(BacktestScenarioError, match=message):
        load_backtest_scenario(scenario_path)


def test_scenario_rejects_unknown_and_missing_fields(tmp_path: Path) -> None:
    unknown_path = _change(_write_valid_scenario(tmp_path), "risk.typo_limit", "1")
    with pytest.raises(BacktestScenarioError, match="unknown field 'risk.typo_limit'"):
        load_backtest_scenario(unknown_path)

    missing_path = _write_valid_scenario(tmp_path / "missing")
    document = yaml.safe_load(missing_path.read_text(encoding="utf-8"))
    del document["funding"]["initial_cash"]
    missing_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    with pytest.raises(BacktestScenarioError, match="funding.initial_cash"):
        load_backtest_scenario(missing_path)


def test_always_flat_forbids_target_quantity(tmp_path: Path) -> None:
    scenario_path = _change(
        _write_valid_scenario(tmp_path),
        "strategy.id",
        "always-flat-v1",
    )

    with pytest.raises(BacktestScenarioError, match="incompatible_strategy_parameters"):
        load_backtest_scenario(scenario_path)


def test_scenario_rejects_market_prices_outside_instrument_domain(tmp_path: Path) -> None:
    scenario_path = _write_valid_scenario(tmp_path)
    data_path = tmp_path / "prices.csv"
    lines = data_path.read_text(encoding="utf-8").splitlines()
    negative_rows = [lines[0]]
    for line in lines[1:]:
        fields = line.split(",")
        fields[6:10] = ["-100", "-99", "-102", "-101"]
        negative_rows.append(",".join(fields))
    data_path.write_text("\n".join(negative_rows) + "\n", encoding="utf-8")
    window = ReplayWindow(
        datetime(2026, 1, 2, 9, 31, tzinfo=UTC),
        datetime(2026, 1, 2, 9, 33, tzinfo=UTC),
    )
    dataset = decode_phase1_ohlcv_csv(data_path.read_bytes(), replay_window=window)
    document = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    document["data"]["fingerprint"] = {
        "sha256": dataset.selection.fingerprint.sha256.value,
        "record_count": dataset.selection.fingerprint.record_count,
    }
    scenario_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(BacktestScenarioError, match="price domain"):
        load_backtest_scenario(scenario_path)


def test_duplicate_yaml_key_fails_closed(tmp_path: Path) -> None:
    scenario_path = _write_valid_scenario(tmp_path)
    content = scenario_path.read_text(encoding="utf-8")
    scenario_path.write_text(content + "schema_version: 1\n", encoding="utf-8")

    with pytest.raises(BacktestScenarioError, match="invalid YAML"):
        load_backtest_scenario(scenario_path)
