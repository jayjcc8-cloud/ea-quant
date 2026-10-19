"""A supported template plus a published CSV reaches the existing real engine."""

import json
from pathlib import Path
from typing import Any

import pytest

from ea.web.uploads import AVAILABILITY_PROVIDED
from ea.web.user_strategies import StrategyDraft, UserStrategyStateError
from unit.test_web_uploads import _capture
from unit.test_web_user_strategies import _launch, _stack, _terminal_job


def test_template_and_csv_form_one_revision_then_one_real_experiment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategies, _, service, provider = _stack(tmp_path, monkeypatch)
    try:
        templates = service.registry.templates()
        assert len(templates) == 2
        assert all(item["valid"] for item in templates)
        before = {p.name: p.read_bytes() for p in service.registry.root.iterdir()}
        dataset = service.uploads.publish(_capture(6), availability_policy=AVAILABILITY_PROVIDED)
        strategy = strategies.create(name="CSV research", intent="Test a supported entry")
        request: dict[str, Any] = {
            "operation_id": "prepare-one",
            "base_revision": 1,
            "scenario_id": "template:bounded-long.yaml",
            "dataset_id": dataset["dataset_id"],
        }
        bound = strategies.bind_scenario(strategy.user_strategy_id, **request)
        assert isinstance(bound.draft, StrategyDraft)
        assert bound.draft.revision == 2
        assert bound.draft.dataset_id == dataset["dataset_id"]
        assert bound.draft.venue == "XNAS" and bound.draft.symbol == "AAPL"
        assert not bound.experiments and not provider.calls
        assert (
            strategies.bind_scenario(strategy.user_strategy_id, **request).document()
            == bound.document()
        )
        revised = strategies.save_draft(
            strategy.user_strategy_id,
            operation_id="cash",
            expected_revision=2,
            values={"initial_cash": "12000", "parameters.target_quantity": "3"},
        )
        assert revised.draft.revision == 3
        experiment = _launch(strategies, strategy.user_strategy_id).experiments[-1]
        assert experiment["status"] == "created", experiment
        _terminal_job(service, experiment["job_id"])
        report = service.report(experiment["job_id"])
        assert json.loads(report)["completion"]["terminal_outcome"] == "success"
        version = service.get_job(experiment["job_id"]).dataset_version
        assert version is not None
        assert version["dataset_id"] == dataset["dataset_id"]
        service.uploads.publish(_capture(7), availability_policy=AVAILABILITY_PROVIDED)
        assert service.report(experiment["job_id"]) == report
        assert len(service.list_jobs()) == 1
        assert {p.name: p.read_bytes() for p in service.registry.root.iterdir()} == before
    finally:
        service.stop()


def test_incompatible_csv_or_missing_template_data_never_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategies, _, service, provider = _stack(tmp_path, monkeypatch)
    try:
        dataset = service.uploads.publish(
            _capture(4, symbol="OTHER"), availability_policy=AVAILABILITY_PROVIDED
        )
        strategy = strategies.create(name="Refuse", intent="Keep my idea")
        for selected in (None, dataset["dataset_id"]):
            with pytest.raises(UserStrategyStateError):
                strategies.bind_scenario(
                    strategy.user_strategy_id,
                    operation_id="bind",
                    base_revision=1,
                    scenario_id="template:bounded-long.yaml",
                    dataset_id=selected,
                )
        assert strategies._store.get(strategy.user_strategy_id).document() == strategy.document()
        assert not service.list_jobs() and not provider.calls
    finally:
        service.stop()


def test_uploaded_input_capture_is_private_and_refuses_source_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ea.web.service import InputChangedError

    _, _, service, _ = _stack(tmp_path, monkeypatch)
    try:
        published = service.uploads.publish(_capture(6), availability_policy=AVAILABILITY_PROVIDED)
        scenario = service._bind_dataset(
            service.registry.load("template:bounded-long.yaml"), published["dataset_id"]
        )
        original = scenario.data_path.read_bytes()
        frozen = service._materialize_scenario("capture-once", scenario)
        assert frozen.data_path.parent == service.inputs_dir
        assert frozen.data_path.stat().st_nlink == 1
        assert frozen.data_path.stat().st_ino != scenario.data_path.stat().st_ino
        assert frozen.data_path.read_bytes() == original
        assert json.loads(frozen.scenario_path.read_bytes())["data"]["path"] == str(
            frozen.data_path
        )
        with pytest.raises(FileExistsError):
            service._materialize_scenario("capture-once", scenario)
        scenario.data_path.chmod(0o600)
        scenario.data_path.write_bytes(original.replace(b"100", b"101"))
        assert frozen.data_path.read_bytes() == original
        with pytest.raises(InputChangedError):
            service._materialize_scenario("drift-refused", scenario)
        assert not (service.inputs_dir / "drift-refused.csv").exists()
    finally:
        service.stop()


def test_desktop_template_freezes_the_same_identity_as_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ea.web.quine import QuineBridge

    strategies, _, service, _ = _stack(tmp_path, monkeypatch)
    try:
        published = service.uploads.publish(_capture(6), availability_policy=AVAILABILITY_PROVIDED)
        strategy = strategies.create(name="Desktop template", intent="Explicit CSV")
        bound = strategies.bind_scenario(
            strategy.user_strategy_id,
            operation_id="bind",
            base_revision=1,
            scenario_id="template:bounded-long.yaml",
            dataset_id=published["dataset_id"],
        )
        strategies._quine = QuineBridge(tmp_path / "unused.sock")
        assert isinstance(bound.draft, StrategyDraft)
        admitted = strategies._admitted_request(bound.draft)
        frozen = strategies._freeze_for_core(bound.draft, admitted)
        assert frozen.envelope["scenario_id"] == "template:bounded-long.yaml"
        assert frozen.snapshot_sha256 == admitted["input_sha256"]
    finally:
        service.stop()
