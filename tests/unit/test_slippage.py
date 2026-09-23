from __future__ import annotations

import json
from decimal import localcontext
from pathlib import Path
from typing import Any

import pytest
import yaml

from ea.core import CanonicalDecimal, OrderSide
from ea.product import generate_backtest_report, load_backtest_scenario, run_backtest_scenario
from unit.test_commission import commission_scenario

pytestmark = pytest.mark.filterwarnings(
    "ignore:The anyio.abc.BlockingPortal alias is deprecated:DeprecationWarning"
)


def add_slippage(path: Path, bps: str = "100") -> Path:
    document = yaml.safe_load(path.read_text())
    document["execution"]["slippage"] = {
        "policy": "deterministic-slippage-v1",
        "slippage_bps": bps,
    }
    path.write_text(yaml.safe_dump(document))
    return path


@pytest.mark.parametrize("bps", ["-1", "10000", "NaN", "01", "1.0", "1e2"])
def test_invalid_slippage(tmp_path: Path, bps: str) -> None:
    with pytest.raises(ValueError):
        load_backtest_scenario(add_slippage(commission_scenario(tmp_path), bps))


@pytest.mark.parametrize(
    "close,bps,side,expected",
    [
        (100.0, "100", OrderSide.BUY, "101"),
        (100.0, "100", OrderSide.SELL, "99"),
        (100.0, "0.5", OrderSide.BUY, "100.01"),
        (100.0, "0.5", OrderSide.SELL, "99.99"),
        (100.0, "0", OrderSide.BUY, "100"),
        (-100.0, "100", OrderSide.BUY, "-99"),
        (-100.0, "100", OrderSide.SELL, "-101"),
    ],
)
def test_exact_adverse_price(close: float, bps: str, side: OrderSide, expected: str) -> None:
    from ea.core.historical_matching import _quantized_historical_close
    from unit.test_portfolio_ledger import _spec

    with localcontext() as context:
        context.prec = 2
        price = _quantized_historical_close(
            close, side=side, specification=_spec(), slippage_bps=CanonicalDecimal(bps)
        )
    assert price.text == expected


@pytest.mark.parametrize("commission", [None, "0", "100"])
def test_cost_identity_binds_both_parameters(commission: str | None) -> None:
    from ea.core.commission import (
        commission_bps_from_identity,
        execution_cost_policy_identity,
        slippage_bps_from_identity,
    )

    fee = None if commission is None else CanonicalDecimal(commission)
    identity = execution_cost_policy_identity(CanonicalDecimal("25"), fee)
    assert commission_bps_from_identity(*identity) == fee
    assert slippage_bps_from_identity(*identity) == CanonicalDecimal("25")
    with pytest.raises(ValueError):
        slippage_bps_from_identity(identity[0], "0" * 64)


def test_slipped_fill_drives_commission_and_report(tmp_path: Path) -> None:
    path = add_slippage(commission_scenario(tmp_path / "input"))
    attempt = run_backtest_scenario(
        load_backtest_scenario(path), tmp_path / "runs"
    ).output_directory
    result = json.loads((attempt / "result.json").read_bytes())
    # The exact 101.50 close becomes 102.515, adverse half-tick rounds to 102.52.
    assert result["fill_evidence"]["price"] == "102.52"
    report: Any = generate_backtest_report(attempt, tmp_path / "report").report.document
    assert report["economics"]["fees"]["amount"] == "2.05"
    assert report["economics"]["ending_cash"][0]["amount"] == "9792.91"
    assert report["economics"]["net_pnl"]["amount"] == "12.91"


def test_repeated_round_trips_apply_slippage_on_both_sides(tmp_path: Path) -> None:
    from unit.test_bounded_round_trips_v1 import bounded_scenario

    path = add_slippage(bounded_scenario(tmp_path / "input"))
    attempt = run_backtest_scenario(
        load_backtest_scenario(path), tmp_path / "runs"
    ).output_directory
    result = json.loads((attempt / "result.json").read_bytes())
    prices = [leg["fill"]["price"] for leg in result["execution_legs"]]
    assert prices == ["102.01", "101.97", "106.05", "105.93", "110.09", "109.89"]
    report: Any = generate_backtest_report(attempt, tmp_path / "report").report.document
    # Per-Fill fees: .20 + .20 + .21 + .21 + .22 + .22 = 1.26.
    assert report["economics"]["realized_pnl"]["amount"] == "-1.98"
    assert report["economics"]["fees"]["amount"] == "1.26"


@pytest.mark.parametrize("version", [1, 5])
@pytest.mark.parametrize("stage", ["funding_durable", "dispatch_durable", "reconciliation_durable"])
def test_fractional_slippage_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int, stage: str
) -> None:
    from uuid import uuid4

    from ea.core import RunId
    from ea.product import backtest as b
    from unit.test_backtest_resume import _AbruptInterruption, _interrupt_at
    from unit.test_bounded_round_trips_v1 import bounded_scenario

    path = (commission_scenario if version == 1 else bounded_scenario)(tmp_path / "input")
    add_slippage(path)
    document = yaml.safe_load(path.read_text())
    document["instrument"]["quantity_quantum"] = "0.001"
    parameters = document["strategy"] if version == 1 else document["strategy"]["parameters"]
    parameters["target_quantity"] = "0.333"
    path.write_text(yaml.safe_dump(document))
    scenario = load_backtest_scenario(path)
    run_id = RunId(str(uuid4()))
    baseline = run_backtest_scenario(
        scenario, tmp_path / "baseline", run_id=run_id
    ).output_directory
    _interrupt_at(monkeypatch, stage)
    with pytest.raises(_AbruptInterruption):
        run_backtest_scenario(scenario, tmp_path / "resumed", run_id=run_id)
    monkeypatch.setattr(b, "_TEST_INTERRUPT", None)
    attempt = tmp_path / "resumed" / run_id.value
    b.resume_backtest_attempt(attempt)
    assert (attempt / "result.json").read_bytes() == (baseline / "result.json").read_bytes()
    assert generate_backtest_report(attempt, tmp_path / "report1").report.canonical_bytes == (
        generate_backtest_report(baseline, tmp_path / "report2").report.canonical_bytes
    )


@pytest.mark.parametrize("version", [2, 3])
def test_local_action_packages_use_slippage(tmp_path: Path, version: int) -> None:
    from unit.test_local_action_v2_package import local_v2, set_prices
    from unit.test_local_bounded_round_trips_v1 import local_v3

    path, root, _ = (local_v2 if version == 2 else local_v3)(tmp_path / "input")
    if version == 2:
        set_prices(path, [10, 9, 11, 12, 13, 8, 7, 6])
    baseline = run_backtest_scenario(
        load_backtest_scenario(path, strategy_root=root), tmp_path / "baseline"
    ).output_directory
    add_slippage(path)
    slipped = run_backtest_scenario(
        load_backtest_scenario(path, strategy_root=root), tmp_path / "slipped"
    ).output_directory
    first = json.loads((baseline / "result.json").read_bytes())
    second = json.loads((slipped / "result.json").read_bytes())
    assert len(first["execution_legs"]) == len(second["execution_legs"]) >= 2
    for old, new in zip(first["execution_legs"], second["execution_legs"], strict=True):
        if old["fill"] is not None:
            before = CanonicalDecimal(old["fill"]["price"])
            after = CanonicalDecimal(new["fill"]["price"])
            assert after > before if old["fill"]["side"] == "buy" else after < before
    generate_backtest_report(slipped, tmp_path / "report")


def test_web_holdout_cost_policy_freezes_and_reopens(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from ea.web.app import create_app
    from unit.test_web_api import ORIGIN, _settings, _validate, _wait
    from unit.test_web_holdout import _request, _source, _target

    settings = _settings(tmp_path)
    path = add_slippage(settings.scenario_root / "bounded-long.yaml")
    _target(settings.scenario_root)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        assert _validate(client, path.name)["summary"]["slippage"]["slippage_bps"] == "100"
        source = _source(client)
        response = _request(client, source)
        assert response.status_code == 202, response.text
        holdout = _wait(client, response.json()["holdout_job_id"])
        assert holdout["status"] == "succeeded"
        assert (
            holdout["input_snapshot"]["scenario"]["execution"]
            == (source["input_snapshot"]["scenario"]["execution"])
        )
        reports = [
            client.get(f"/api/backtests/{j['job_id']}/report").json() for j in (source, holdout)
        ]
        # Each fill pays 1.02 extra per share relative to the source fixture's 101.50 close.
        assert reports[0]["economics"]["net_pnl"]["amount"] == "14.96"
    path.unlink()
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        for job, report in zip((source, holdout), reports, strict=True):
            assert client.get(f"/api/backtests/{job['job_id']}/report").json() == report


def test_slippage_price_domain_and_overflow_fail_closed() -> None:
    from ea.core import PriceDomain
    from ea.core.historical_matching import _quantized_historical_close
    from unit.test_portfolio_ledger import _spec

    with pytest.raises(ValueError):
        _quantized_historical_close(
            0.01,
            side=OrderSide.SELL,
            specification=_spec(price_domain=PriceDomain.POSITIVE),
            slippage_bps=CanonicalDecimal("9999"),
        )
    with pytest.raises(ValueError):
        _quantized_historical_close(
            1e20,
            side=OrderSide.BUY,
            specification=_spec(),
            slippage_bps=CanonicalDecimal("100"),
        )


def test_slippage_cannot_overdraw_cash_or_publish_success(tmp_path: Path) -> None:
    from ea.product import BacktestRunFailure

    path = commission_scenario(tmp_path / "input")
    document = yaml.safe_load(path.read_text())
    document["execution"].pop("commission")
    document["funding"]["initial_cash"] = "200"
    path.write_text(yaml.safe_dump(document))
    baseline = run_backtest_scenario(
        load_backtest_scenario(path), tmp_path / "baseline"
    ).output_directory
    assert (baseline / "result.json").is_file()
    add_slippage(path, "9900")
    with pytest.raises(BacktestRunFailure) as caught:
        run_backtest_scenario(load_backtest_scenario(path), tmp_path / "slipped")
    attempt = caught.value.output_directory
    assert not (attempt / "result.json").exists()
    assert json.loads((attempt / "failure.json").read_bytes())["terminal_state"] == "failed"
    with pytest.raises(ValueError):
        generate_backtest_report(attempt, tmp_path / "report")


def test_explicit_zero_has_same_economics_and_distinct_identity(tmp_path: Path) -> None:
    path = commission_scenario(tmp_path / "input")
    before = load_backtest_scenario(path)
    baseline = run_backtest_scenario(before, tmp_path / "baseline").output_directory
    first: Any = generate_backtest_report(baseline, tmp_path / "report1").report.document
    add_slippage(path, "0")
    after = load_backtest_scenario(path)
    assert after.scenario_sha256 != before.scenario_sha256
    attempt = run_backtest_scenario(after, tmp_path / "explicit").output_directory
    second: Any = generate_backtest_report(attempt, tmp_path / "report2").report.document
    assert first["economics"] == second["economics"]


def test_slipped_price_tampering_rejected_by_report(tmp_path: Path) -> None:
    path = add_slippage(commission_scenario(tmp_path / "input"))
    attempt = run_backtest_scenario(
        load_backtest_scenario(path), tmp_path / "runs"
    ).output_directory
    file = attempt / "result.json"
    document = json.loads(file.read_bytes())
    document["fill_evidence"]["price"] = "101.5"
    file.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    with pytest.raises(ValueError):
        generate_backtest_report(attempt, tmp_path / "report")
    assert not (tmp_path / "report" / "report.json").exists()
