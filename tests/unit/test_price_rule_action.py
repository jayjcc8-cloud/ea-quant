from decimal import Decimal
from pathlib import Path

from ea.core.economics import CanonicalDecimal
from ea.product.scenario import load_backtest_scenario
from ea.strategy.package import pack_strategy
from ea.strategy.rules import DEFAULT_RULE, PriceRuleLogic
from ea.strategy.sdk_v1 import StrategyBarV1
from ea.strategy.sdk_v2 import PositionState, PositionViewV2


def test_action_mapping_retains_exact_entry_rule_and_no_exit(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    package = pack_strategy(
        root / "examples/local-strategies/price-rule-action-v2", tmp_path / "rule.eastrategy"
    )
    parameters = {"rule": DEFAULT_RULE, "target_quantity": "2"}
    event_logic = PriceRuleLogic(parameters)
    action_logic = package.module()["create_logic"](parameters)
    events = load_backtest_scenario(
        root / "examples/web-scenarios/price-rule-entry.yaml"
    ).dataset.selection.events
    flat = PositionViewV2(PositionState.FLAT_INITIAL, CanonicalDecimal("0"))
    entries = 0
    for event in events:
        bar = StrategyBarV1(
            event.event_time,
            event.available_at,
            *(
                Decimal(str(getattr(event.payload, field)))
                for field in ("open", "high", "low", "close", "volume")
            ),
        )
        quantity = event_logic.on_event(event)
        expected = (
            {"action": "HOLD"}
            if quantity is None
            else {"action": "ENTER_LONG", "quantity": quantity.text}
        )
        assert action_logic.on_bar(bar, flat) == expected
        entries += quantity is not None
    assert entries == 1
    assert action_logic.on_bar(bar, flat) == {"action": "HOLD"}
    for state in (PositionState.LONG_OPEN, PositionState.FLAT_CLOSED):
        assert action_logic.on_bar(bar, PositionViewV2(state, CanonicalDecimal("2"))) == {
            "action": "HOLD"
        }
    assert action_logic.requires_market_history_on_restore is True


def test_fresh_research_candidate_runs_bounded_paper_and_stops(tmp_path: Path) -> None:
    import json
    import shutil
    import threading
    import time

    import pytest
    import yaml
    from fastapi.testclient import TestClient

    from ea.core import RunId
    from ea.product.paper import restore_paper_trading_session
    from ea.product.paper_run import resume_local_paper, run_local_paper
    from ea.product.paper_session import read_paper_status, request_paper_stop
    from ea.web.app import WebSettings, create_app
    from unit.test_web_api import ORIGIN, WRITE_HEADERS, _wait

    root = Path(__file__).parents[2]
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    packages = tmp_path / "packages"
    packages.mkdir()
    package = pack_strategy(
        root / "examples/local-strategies/price-rule-action-v2", packages / "rule.eastrategy"
    )
    for name in ("moving-average-entry.csv", "moving-average-holdout.csv"):
        shutil.copyfile(root / "examples/web-scenarios" / name, scenarios / name)
    for suffix in ("entry", "holdout"):
        doc = yaml.safe_load(
            (root / f"examples/web-scenarios/price-rule-{suffix}.yaml").read_text()
        )
        # New scenario and strategy identity, never alter an old candidate/snapshot.
        doc["schema_version"] = 4
        doc["strategy"].update(
            id=package.descriptor.strategy_id,
            action_contract="V2",
            position_lifecycle="single-long-round-trip-v1",
            source={
                "kind": "local-package",
                "package_id": package.identity.package_id,
                "artifact_sha256": package.identity.artifact_sha256,
            },
        )
        (scenarios / f"action-{suffix}.yaml").write_text(yaml.safe_dump(doc))
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<title>Task-private fixture</title>")
    settings = WebSettings(
        scenario_root=scenarios,
        strategy_root=packages,
        workspace=tmp_path / "workspace",
        ui_dir=ui,
        port=8765,
    )
    runs = tmp_path / "runs"
    run_id = RunId("11111111-2222-4333-8444-555555555555")
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        identity = client.post(
            "/api/scenarios/action-entry.yaml/validate", headers=WRITE_HEADERS
        ).json()["input_identity"]
        source = client.post(
            "/api/backtests",
            headers=WRITE_HEADERS,
            json={
                "scenario_id": "action-entry.yaml",
                "input_identity": identity,
                "request_id": "task14-action-source",
            },
        ).json()
        assert _wait(client, source["job_id"])["status"] == "succeeded"
        relation = client.post(
            "/api/holdouts",
            headers=WRITE_HEADERS,
            json={
                "source_job_id": source["job_id"],
                "scenario_id": "action-holdout.yaml",
            },
        ).json()
        assert _wait(client, relation["holdout_job_id"])["status"] == "succeeded"
        candidate = client.post(
            "/api/candidates",
            headers=WRITE_HEADERS,
            json={"validation_id": relation["validation_id"]},
        ).json()
        candidate_id = candidate["candidate_id"]
        preparation_path = f"/api/candidates/{candidate_id}/simulation-preparation"
        evaluated = client.get(preparation_path).json()
        assert evaluated["blockers"] == ["explicit_research_acceptance_required"]
        with pytest.raises(ValueError, match="ACCEPTED|accepted"):
            run_local_paper(
                settings.workspace,
                candidate_id,
                scenarios / "action-entry.yaml",
                runs,
                run_id=run_id,
                event_limit=10,
            )
        assert not list(runs.iterdir())
        decision = client.post(
            f"/api/candidates/{candidate_id}/decision",
            headers=WRITE_HEADERS,
            json={"outcome": "ACCEPTED", "reason": "Task14 plumbing fixture only"},
        )
        assert decision.status_code == 200
        preparation = client.get(preparation_path).json()
        assert preparation["eligible"] and not preparation["execution_authorized"]
        assert not preparation["start_available"]
        frozen = {p: p.read_bytes() for p in settings.workspace.rglob("*") if p.is_file()}

    worker_errors = []
    workers = []

    def ready(attempt):
        def stop_after_fill():
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    status = read_paper_status(attempt)
                    if status.get("fills", 0) == 1:
                        request_paper_stop(attempt, timeout_seconds=5.0)
                        return
                    time.sleep(0.01)
                raise AssertionError("bounded simulation did not fill")
            except BaseException as error:
                worker_errors.append(error)

        worker = threading.Thread(target=stop_after_fill)
        workers.append(worker)
        worker.start()

    result = run_local_paper(
        settings.workspace,
        candidate_id,
        scenarios / "action-entry.yaml",
        runs,
        prices=(100.0, 101.0, 102.0),
        interval=0.05,
        event_limit=20,
        run_id=run_id,
        on_ready=ready,
    )
    for worker in workers:
        worker.join(timeout=6)
        assert not worker.is_alive()
    assert not worker_errors
    assert result["state"] == "stopped"
    assert result["fills"] == 1 and result["position"] == "2"
    # Third generated bar settles quantity 2 at 102 + 1% commission 2.04.
    assert result["cash"] == "9793.96"
    assert result["ledger_sequence"] == 2
    attempt = runs / run_id.value
    stopped = read_paper_status(attempt)
    assert not stopped["lease_held"]
    stable = {p: p.read_bytes() for p in attempt.rglob("*") if p.is_file()}
    request_paper_stop(attempt, timeout_seconds=1.0)
    assert resume_local_paper(attempt)["state"] == "terminal"
    with pytest.raises(ValueError, match="fresh attempt"):
        run_local_paper(
            settings.workspace,
            candidate_id,
            scenarios / "action-entry.yaml",
            runs,
            run_id=run_id,
            event_limit=10,
        )
    assert len(list(runs.iterdir())) == 1
    assert {p: p.read_bytes() for p in attempt.rglob("*") if p.is_file()} == stable
    assert {p: p.read_bytes() for p in settings.workspace.rglob("*") if p.is_file()} == frozen
    scenario = load_backtest_scenario(scenarios / "action-entry.yaml", strategy_root=packages)
    # Refuse before touching an audit/broker/ledger: no fake window reset.
    with pytest.raises(ValueError, match="market history cannot be restored"):
        restore_paper_trading_session(
            scenario,
            binding=None,
            audit=None,
            records=(),
            clock=None,
            monotonic=None,
            source_id=None,
            prices=(100.0,),
            stop_requested=lambda: False,
        )
    (tmp_path / "acceptance.json").write_text(
        json.dumps(
            {
                "source_job_id": source["job_id"],
                "holdout": relation,
                "candidate_id": candidate_id,
                "preparation": preparation,
                "result": result,
                "stopped_status": stopped,
                "resume": resume_local_paper(attempt),
                "no_candidate_mutations": True,
                "duplicate_start_rejected": True,
                "history_restore_rejected": True,
            },
            indent=2,
        )
    )
