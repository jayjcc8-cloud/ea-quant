"""Local Action V2 artifacts exercise the existing complete offline research route."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from ea.product import generate_backtest_report, load_backtest_scenario, run_backtest_scenario
from ea.strategy.package import _container, canonical_json, pack_strategy, validate_package
from unit.test_single_round_trip_v1 import closed_scenario


def local_v2(tmp_path: Path) -> tuple[Path, Path, Any]:
    path = closed_scenario(tmp_path / "scenarios")
    source = tmp_path / "source"
    source.mkdir()
    example = (
        Path(__file__).resolve().parents[2]
        / "examples/local-strategies/moving-average-crossover-v2"
    )
    for name in ("manifest.json", "strategy.py"):
        (source / name).write_bytes((example / name).read_bytes())
    manifest = json.loads((source / "manifest.json").read_bytes())
    root = tmp_path / "packages"
    root.mkdir()
    package = pack_strategy(source, root / "ma.eastrategy")
    doc = yaml.safe_load(path.read_text())
    doc["strategy"] = {
        "id": manifest["strategy"]["id"],
        "version": 1,
        "parameters": {"fast_window": 1, "slow_window": 2, "quantity": "2"},
        "action_contract": "V2",
        "position_lifecycle": "single-long-round-trip-v1",
        "source": {
            "kind": "local-package",
            "package_id": package.identity.package_id,
            "artifact_sha256": package.identity.artifact_sha256,
        },
    }
    path.write_text(yaml.safe_dump(doc))
    return path, root, package


def test_v2_pack_dispatch_determinism_and_identity(tmp_path: Path) -> None:
    _, _, package = local_v2(tmp_path)
    assert type(package).__name__ == "StrategyPackageV2"
    assert package.document()["schema"] == "ea-strategy-package-v2"
    assert "outcome_mode" not in package.document()
    same = pack_strategy(tmp_path / "source", tmp_path / "same.eastrategy")
    assert same.artifact_bytes == package.artifact_bytes
    assert same.identity == package.identity
    code = tmp_path / "source" / "strategy.py"
    code.write_bytes(code.read_bytes() + b"\n")
    assert pack_strategy(code.parent, tmp_path / "changed.eastrategy").identity != package.identity


@pytest.mark.parametrize(
    "field,value",
    [("action_contract", "V3"), ("position_lifecycle", "multi"), ("outcome_mode", "no_entry")],
)
def test_v2_rejects_wrong_contract(tmp_path: Path, field: str, value: str) -> None:
    _, _, package = local_v2(tmp_path)
    with zipfile.ZipFile(io.BytesIO(package.artifact_bytes)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    manifest["strategy"][field] = value
    with pytest.raises(ValueError):
        validate_package(_container(canonical_json(manifest), package.source_bytes))


def test_v2_load_and_never_enter_without_reserved_quantity_parameter(tmp_path: Path) -> None:
    path, root, package = local_v2(tmp_path)
    scenario = load_backtest_scenario(path, strategy_root=root)
    assert scenario.strategy_entry.descriptor.strategy_id == package.descriptor.strategy_id
    run = run_backtest_scenario(scenario, tmp_path / "runs")
    report: Any = generate_backtest_report(run.output_directory, tmp_path / "report").report
    assert report.document["economics"]["position_outcome"] == "FLAT_INITIAL"
    assert (run.output_directory / "strategy.eastrategy").read_bytes() == package.artifact_bytes


def set_prices(path: Path, prices: list[int], *, day: int = 2) -> None:
    from datetime import UTC, datetime, timedelta

    from ea.core import ReplayWindow
    from ea.data import decode_phase1_ohlcv_csv

    csv = path.parent / ("prices.csv" if day == 2 else "later.csv")
    header = (path.parent / "prices.csv").read_text().splitlines()[0]
    start = datetime(2026, 1, day, 9, 31, tzinfo=UTC)

    def stamp(t: datetime) -> str:
        return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    rows = [header]
    for i, price in enumerate(prices):
        t = start + timedelta(minutes=i)
        rows.append(
            f"1,XNAS,AAPL,{stamp(t - timedelta(minutes=1))},{stamp(t)},raw,"
            f"{price},{price},{price},{price},10,fixture.raw,{i},0,{stamp(t)}"
        )
    csv.write_text("\n".join(rows) + "\n")
    end = start + timedelta(minutes=len(prices))
    dataset = decode_phase1_ohlcv_csv(csv.read_bytes(), replay_window=ReplayWindow(start, end))
    doc = yaml.safe_load(path.read_text())
    doc["data"] = {
        "path": csv.name,
        "start_utc": stamp(start),
        "end_utc": stamp(end),
        "fingerprint": {
            "sha256": dataset.selection.fingerprint.sha256.value,
            "record_count": len(prices),
        },
    }
    path.write_text(yaml.safe_dump(doc))


@pytest.mark.parametrize(
    "prices,outcome,fills",
    [
        ([10, 9, 8, 7, 6, 5, 4, 3], "FLAT_INITIAL", 0),
        ([10, 9, 11, 12, 13, 14, 15, 16], "OPEN_AT_END", 1),
        ([10, 9, 11, 12, 13, 8, 7, 6], "CLOSED", 2),
    ],
)
def test_stateful_local_v2_real_economics(
    tmp_path: Path, prices: list[int], outcome: str, fills: int
) -> None:
    from ea.product.equity_path import generate_equity_path_analysis
    from ea.product.scenario import parameterize_strategy_scenario

    path, root, package = local_v2(tmp_path)
    set_prices(path, prices)
    loaded = load_backtest_scenario(path, strategy_root=root)
    changed = parameterize_strategy_scenario(
        loaded, initial_cash="10000", parameters={**loaded.strategy_parameters, "quantity": "3"}
    )
    assert changed.scenario_sha256 != loaded.scenario_sha256
    assert (
        load_backtest_scenario(path, strategy_root=root).scenario_sha256 == loaded.scenario_sha256
    )
    (root / "ma.eastrategy").unlink()
    run = run_backtest_scenario(loaded, tmp_path / "runs")
    report: Any = generate_backtest_report(run.output_directory, tmp_path / "report").report
    economic = report.document["economics"]
    assert economic["position_outcome"] == outcome
    assert economic["counts"]["fills"] == fills
    curve = json.loads(generate_equity_path_analysis(run.output_directory, report).canonical_bytes)
    assert curve["schema"] == "ea.backtest-equity-path.v2"
    assert curve["display_points"][-1]["equity"] == economic["equity"]["amount"]
    if outcome == "CLOSED":
        assert (
            economic["equity"]["amount"] == "9989.97"
        )  # 10000 - 24 - 0.02 + 14 - 0.01 (per-leg rounding)
        assert economic["completed_round_trips"] == 1
    assert (run.output_directory / "strategy.eastrategy").read_bytes() == package.artifact_bytes


def test_local_v2_web_batch_holdout_frozen_reopen(tmp_path: Path) -> None:
    from ea.web.service import WebService

    path, root, package = local_v2(tmp_path)
    set_prices(path, [10, 9, 11, 12, 13, 8, 7, 6])
    target = path.parent / "later.yaml"
    target.write_bytes(path.read_bytes())
    set_prices(target, [20, 19, 21, 22, 23, 18, 17, 16], day=3)
    doc = yaml.safe_load(target.read_text())
    doc["strategy"]["parameters"]["quantity"] = "9"
    target.write_text(yaml.safe_dump(doc))
    service = WebService(path.parent, tmp_path / "workspace", strategy_root=root)
    service.start()
    summary: Any = service.validate_scenario(path.name)
    assert summary["strategy_descriptor"]["display_name"] == "Local MA crossover"
    job, _ = service.create_job(
        scenario_id=path.name, input_identity=summary["input_identity"], request_id="v2"
    )
    service.stop()
    assert service.get_job(job.job_id).status == "succeeded"
    assert (
        cast(dict[str, Any], service.get_job(job.job_id).document(presentation=True)["parameters"])[
            "quantity"
        ]
        == "2"
    )
    service = WebService(path.parent, tmp_path / "workspace", strategy_root=root)
    service.start()
    batch = service.create_batch(
        scenario_id=path.name,
        input_identity=summary["input_identity"],
        initial_cash="10000",
        runs=[{"fast_window": 1, "slow_window": 2, "quantity": q} for q in ("2", "3")],
    )
    service.stop()
    assert all(
        service.get_job(j).status == "succeeded" for j in cast(list[str], batch["member_job_ids"])
    )
    (root / "ma.eastrategy").unlink()
    service = WebService(path.parent, tmp_path / "workspace", strategy_root=root)
    service.start()
    relation = service.create_holdout(source_job_id=job.job_id, scenario_id=target.name)
    service.stop()
    holdout = service.get_job(relation["holdout_job_id"])
    assert holdout.status == "succeeded"
    frozen = json.loads(holdout.input_snapshot_bytes or b"{}")["scenario"]["strategy"]
    assert (
        frozen
        == json.loads(service.get_job(job.job_id).input_snapshot_bytes or b"{}")["scenario"][
            "strategy"
        ]
    )
    assert (
        service.inputs_dir / f"{holdout.job_id}.eastrategy"
    ).read_bytes() == package.artifact_bytes
    report = service.report(holdout.job_id)
    for csv in path.parent.glob("*.csv"):
        csv.unlink()
    reopened = WebService(path.parent, tmp_path / "workspace", strategy_root=root)
    reopened.start()
    try:
        assert (
            cast(
                dict[str, Any],
                reopened.get_job(job.job_id).document(presentation=True)["strategy_descriptor"],
            )["display_name"]
            == "Local MA crossover"
        )
        assert reopened.report(holdout.job_id) == report
    finally:
        reopened.stop()


@pytest.mark.parametrize(
    "mutation", ["tamper", "extra", "duplicate", "wrong-version", "v1-route", "shadow"]
)
def test_local_v2_invalid_container_and_route(tmp_path: Path, mutation: str) -> None:
    from ea.strategy.catalog import ResearchStrategyCatalogV1

    path, root, package = local_v2(tmp_path)
    if mutation == "v1-route":
        doc = yaml.safe_load(path.read_text())
        doc["schema_version"] = 3
        doc["strategy"].pop("action_contract")
        doc["strategy"].pop("position_lifecycle")
        path.write_text(yaml.safe_dump(doc))
        with pytest.raises(ValueError):
            load_backtest_scenario(path, strategy_root=root)
        return
    if mutation == "tamper":
        with pytest.raises(ValueError):
            validate_package(package.artifact_bytes + b"x")
        with pytest.raises(ValueError):
            validate_package(package.artifact_bytes, expected_sha256="0" * 64)
        return
    manifest = json.loads((tmp_path / "source" / "manifest.json").read_bytes())
    if mutation in ("wrong-version", "shadow"):
        if mutation == "wrong-version":
            manifest["schema_version"] = True
        else:
            manifest["strategy"]["id"] = "single-long-hold-roots-v1"
        with pytest.raises(ValueError):
            changed = validate_package(_container(canonical_json(manifest), package.source_bytes))
            ResearchStrategyCatalogV1((changed,))
    else:
        content = io.BytesIO(package.artifact_bytes)
        with zipfile.ZipFile(content, "a") as archive:
            archive.writestr("strategy.py" if mutation == "duplicate" else "extra.py", b"")
        with pytest.raises(ValueError):
            validate_package(content.getvalue())


@pytest.mark.parametrize(
    "decision",
    [
        "{'action':'SHORT'}",
        "{'action':'EXIT_LONG','quantity':'1'}",
        "{'action':'ENTER_LONG','quantity':'0.5'}",
        "{'action':'ENTER_LONG','quantity':'0'}",
        "{'action':'ENTER_LONG','quantity':'2','price':'1'}",
    ],
)
def test_local_v2_bad_action_fails_before_order(tmp_path: Path, decision: str) -> None:
    from ea.product import BacktestRunFailure

    path, root, _ = local_v2(tmp_path)
    code = tmp_path / "source" / "strategy.py"
    code.write_text(
        "def validate_parameters(p,c): pass\nclass Logic:\n def on_bar(self,b,p): return "
        + decision
        + "\ndef create_logic(p): return Logic()\n"
    )
    (root / "ma.eastrategy").unlink()
    package = pack_strategy(code.parent, root / "ma.eastrategy")
    doc = yaml.safe_load(path.read_text())
    doc["strategy"]["source"]["artifact_sha256"] = package.identity.artifact_sha256
    path.write_text(yaml.safe_dump(doc))
    with pytest.raises(BacktestRunFailure) as error:
        run_backtest_scenario(load_backtest_scenario(path, strategy_root=root), tmp_path / "runs")
    assert not (error.value.output_directory / "result.json").exists()
    from ea.core import AuditRecordKind

    journal = (error.value.output_directory / "audit" / "audit-v1.journal").read_bytes()
    assert len(journal) > 0
    # The journal frames contain canonical audit JSON; failed attempts need no terminal export.
    assert AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION.value.encode() not in journal
    assert (error.value.output_directory / "failure.json").is_file()


def test_local_risk_projection_is_pinned_by_order_hash(tmp_path: Path) -> None:
    from ea.product.round_trip_report import _verify_local_risk

    path, root, _ = local_v2(tmp_path)
    set_prices(path, [10, 9, 11, 12, 13, 8, 7, 6])
    doc = yaml.safe_load(path.read_text())
    doc["risk"]["max_order_quantity"] = "1"
    path.write_text(yaml.safe_dump(doc))
    run = run_backtest_scenario(load_backtest_scenario(path, strategy_root=root), tmp_path / "runs")
    report = generate_backtest_report(run.output_directory, tmp_path / "report")
    assert report.status == "success"
    result = json.loads((run.output_directory / "result.json").read_bytes())
    leg = result["execution_legs"][0]
    assert leg["risk"]["decision"] == "resize"
    with pytest.raises(ValueError, match="risk decision conflicts"):
        _verify_local_risk(leg["order_evidence"], {"decision": "allow"}, exit_leg=False)
