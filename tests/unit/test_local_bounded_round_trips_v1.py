from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from ea.product import generate_backtest_report, load_backtest_scenario, run_backtest_scenario
from ea.product.equity_path import generate_equity_path_analysis
from ea.strategy.package import pack_strategy
from unit.test_bounded_round_trips_v1 import bounded_scenario
from unit.test_local_action_v2_package import set_prices


def local_v3(tmp_path: Path, *, varying: bool = False) -> tuple[Path, Path, Any]:
    path = bounded_scenario(tmp_path / "scenarios")
    source = tmp_path / "source"
    source.mkdir()
    example = (
        Path(__file__).resolve().parents[2]
        / "examples/local-strategies/moving-average-crossover-v3"
    )
    for name in ("manifest.json", "strategy.py"):
        (source / name).write_bytes((example / name).read_bytes())
    if varying:
        (source / "strategy.py").write_text("""
def validate_parameters(p, context): pass
class Logic:
    def __init__(self): self.entries = 0
    def on_bar(self, bar, position):
        if position.quantity.coefficient == 0:
            self.entries += 1
            return {"action": "ENTER_LONG", "quantity": str(self.entries)}
        return {"action": "EXIT_LONG"}
def create_logic(p): return Logic()
""")
    root = tmp_path / "packages"
    root.mkdir()
    package = pack_strategy(source, root / "ma.eastrategy")
    doc = yaml.safe_load(path.read_text())
    doc["strategy"].update(
        id=package.descriptor.strategy_id,
        parameters={"fast_window": 1, "slow_window": 2, "quantity": "2"},
        source={
            "kind": "local-package",
            "package_id": package.identity.package_id,
            "artifact_sha256": package.identity.artifact_sha256,
        },
    )
    path.write_text(yaml.safe_dump(doc))
    if not varying:
        set_prices(path, [10, 9, 11, 12, 13, 8, 7, 6] * 3)
    return path, root, package


def test_package_v3_ma_repeats_and_path(tmp_path: Path) -> None:
    path, root, package = local_v3(tmp_path)
    assert type(package).__name__ == "StrategyPackageV3"
    assert package.document()["schema"] == "ea-strategy-package-v3"
    loaded = load_backtest_scenario(path, strategy_root=root)
    attempt = run_backtest_scenario(loaded, tmp_path / "runs").output_directory
    report = generate_backtest_report(attempt, tmp_path / "report").report
    doc: Any = report.document
    assert doc["economics"]["completed_round_trips"] == 3
    path_doc = json.loads(generate_equity_path_analysis(attempt, report).canonical_bytes)
    assert path_doc["schema"] == "ea.backtest-equity-path.v3"
    assert path_doc["display_points"][-1]["equity"] == doc["economics"]["equity"]["amount"]
    assert (attempt / "strategy.eastrategy").read_bytes() == package.artifact_bytes


def test_local_v3_varying_quantities_keep_one_planning_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ea.product import round_trip as rt

    path, root, _ = local_v3(tmp_path, varying=True)
    from ea.portfolio import create_portfolio_planning_authority as original

    owners = []

    def factory(**kwargs: Any) -> Any:
        owner = original(**kwargs)
        owners.append(owner)
        return owner

    monkeypatch.setattr(rt, "create_portfolio_planning_authority", factory)
    attempt = run_backtest_scenario(
        load_backtest_scenario(path, strategy_root=root), tmp_path / "runs"
    ).output_directory
    result = json.loads((attempt / "result.json").read_bytes())
    assert len(owners) == 1
    assert [leg["fill"]["quantity"] for leg in result["execution_legs"]] == [
        "1",
        "1",
        "2",
        "2",
        "3",
        "3",
    ]
    assert [
        leg["order_evidence"]["intent_id"]["owner_sequence"] for leg in result["execution_legs"]
    ] == list(range(1, 7))
    generate_backtest_report(attempt, tmp_path / "report")


def test_package_v3_cannot_use_legacy_v4_route(tmp_path: Path) -> None:
    path, root, _ = local_v3(tmp_path)
    doc = yaml.safe_load(path.read_text())
    doc["schema_version"] = 4
    doc["strategy"].pop("max_round_trips")
    doc["strategy"]["position_lifecycle"] = "single-long-round-trip-v1"
    path.write_text(yaml.safe_dump(doc))
    with pytest.raises(ValueError):
        load_backtest_scenario(path, strategy_root=root)


def test_holdout_compatibility_binds_round_trip_limit(tmp_path: Path) -> None:
    from ea.web.holdout import compatible

    path, root, _ = local_v3(tmp_path)
    source = json.loads(load_backtest_scenario(path, strategy_root=root).canonical_bytes)
    target = json.loads(json.dumps(source))
    target["strategy"]["max_round_trips"] = 1
    assert not compatible(source, target)


def test_local_ma_resume_reconstructs_strategy_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil
    from uuid import uuid4

    from ea.core import RunId
    from ea.product import backtest as b
    from unit.test_backtest_resume import _AbruptInterruption

    path, root, _ = local_v3(tmp_path)
    loaded = load_backtest_scenario(path, strategy_root=root)
    run_id = RunId(str(uuid4()))
    baseline = run_backtest_scenario(loaded, tmp_path / "baseline", run_id=run_id).output_directory
    seen = 0

    def interrupt(stage: str) -> None:
        nonlocal seen
        if stage == "dispatch_durable":
            seen += 1
            if seen == 4:
                raise _AbruptInterruption()

    monkeypatch.setattr(b, "_TEST_INTERRUPT", interrupt)
    with pytest.raises(_AbruptInterruption):
        run_backtest_scenario(loaded, tmp_path / "interrupted", run_id=run_id)
    monkeypatch.setattr(b, "_TEST_INTERRUPT", None)
    shutil.rmtree(tmp_path / "source")
    shutil.rmtree(root)
    attempt = tmp_path / "interrupted" / run_id.value
    b.resume_backtest_attempt(attempt)
    for name in ("result.json", "funding.json", "audit.jsonl"):
        assert (attempt / name).read_bytes() == (baseline / name).read_bytes()


def test_builtin_v5_web_uses_v5_job_and_v3_evidence(tmp_path: Path) -> None:
    from ea.web.service import WebService

    path = bounded_scenario(tmp_path / "scenarios")
    service = WebService(path.parent, tmp_path / "workspace")
    service.start()
    summary: Any = service.validate_scenario(path.name)
    job, _ = service.create_job(
        scenario_id=path.name, input_identity=summary["input_identity"], request_id="v5"
    )
    service.stop()
    completed = service.get_job(job.job_id)
    assert completed.status == "succeeded"
    assert completed.schema == "ea.local-web-job.v5"
    report = service.report(job.job_id)
    assert json.loads(report)["schema"] == "ea.backtest-report.v3"
    path_bytes = service.artifact(job.job_id, "equity-path.json")
    assert json.loads(path_bytes)["schema"] == "ea.backtest-equity-path.v3"
    (path.parent / "prices.csv").unlink()
    reopened = WebService(path.parent, tmp_path / "workspace")
    reopened.start()
    try:
        assert reopened.report(job.job_id) == report
        assert reopened.artifact(job.job_id, "equity-path.json") == path_bytes
    finally:
        reopened.stop()
