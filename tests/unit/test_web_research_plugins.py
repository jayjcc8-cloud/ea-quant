"""Capability selection consumed by the existing research task and job evidence."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from ea.plugin.catalog import AdapterV1, PluginCatalogV1
from ea.plugin.manifest import (
    KIND_INTERFACES,
    MANIFEST_SCHEMA_VERSION,
    RUNTIME_PROFILE,
    InterfaceRangeV1,
    PluginManifestV1,
    seal_manifest,
)
from ea.product.scenario import load_backtest_scenario
from ea.web import llm
from ea.web.app import create_app
from ea.web.llm import ModelResponse
from ea.web.research import (
    ResearchOrchestrator,
    ResearchStore,
    SelectionRejectedError,
)
from ea.web.service import JobRecord, WebService
from ea.web.settings import RadianSettings
from unit.test_web_research import (
    HOST,
    WRITE_HEADERS,
    FakeProvider,
    _app_settings,
    _fake_orchestrator,
    _propose_call,
    _response,
    _scenario_root,
    _service,
    _settings_store,
    _task,
)

pytestmark = pytest.mark.filterwarnings(
    "ignore:The anyio.abc.BlockingPortal alias is deprecated:DeprecationWarning"
)

SELECTED = {
    "strategy": {
        "plugin_id": "ea.builtin.strategy.bounded-long-v1",
        "package_version": "1.0.0",
    },
    "validator": {
        "plugin_id": "ea.builtin.validator.local-scenario-input",
        "package_version": "1.0.0",
    },
    "backtest": {"plugin_id": "ea.builtin.backtest.event-replay", "package_version": "1.0.0"},
}


def _orchestrator_with_settings(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    responses: list[Any],
    document: dict[str, Any] | None,
) -> tuple[ResearchOrchestrator, Any, Any]:
    """Orchestrator over a real service with one explicit settings document."""
    settings = _settings_store(tmp_path, monkeypatch)
    if document is not None:
        settings.save(document)
    store = ResearchStore(tmp_path / "workspace")
    service = _service(tmp_path)
    orchestrator = ResearchOrchestrator(store, service, settings)
    provider = FakeProvider(responses)
    cast(Any, orchestrator)._provider = provider  # noqa: SLF001 — test seam
    return orchestrator, service, provider


def _strategy_manifest(**overrides: Any) -> PluginManifestV1:
    adapter = AdapterV1(
        entrypoint="test.host.adapter.strategy.alpha",
        kind="strategy",
        provides=("strategy.research",),
        effects=("local_compute",),
        identity={"strategy_id": "alpha", "strategy_version": 1},
    )
    values: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "plugin_id": "test.capability.strategy",
        "kind": "strategy",
        "package_version": "1.0.0",
        "artifact_digest": "",
        "interface": InterfaceRangeV1(KIND_INTERFACES["strategy"], 1, 0),
        "entrypoint": adapter.entrypoint,
        "provides": adapter.provides,
        "effects": adapter.effects,
        "runtime_profile": RUNTIME_PROFILE,
        "config_schema": {},
    }
    values.update(overrides)
    return seal_manifest(PluginManifestV1(**values))


TEST_ADAPTER = AdapterV1(
    entrypoint="test.host.adapter.strategy.alpha",
    kind="strategy",
    provides=("strategy.research",),
    effects=("local_compute",),
    identity={
        "strategy_id": "alpha",
        "strategy_version": 1,
        # A host adapter record is this host's own fact; the stand-in stands in
        # for the real implementation fingerprint the built-ins carry.
        "implementation_sha256": "sha256:" + "0" * 64,
    },
)
TEST_SELECTION = {"strategy": {"plugin_id": "test.capability.strategy", "package_version": "1.0.0"}}


# --- selection is consumed ---------------------------------------------------


def test_selection_is_recorded_told_to_the_model_and_consumed_by_the_run(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    provider.responses.append(
        _response(
            [{"id": "call-1", "name": "propose_spec", "input": _propose_call(strategy, catalog)}]
        )
    )
    task = orchestrator.create("Selected capability run", SELECTED)
    assert task.selection == SELECTED
    assert "Selected capabilities" in provider.calls[0]["system"]
    assert "ea.builtin.strategy.bounded-long-v1" in provider.calls[0]["system"]
    assert task.spec_draft is not None
    assert task.spec_draft.selection == SELECTED

    confirmed = orchestrator.confirm(task.task_id)
    assert confirmed.pinned_binding is not None
    assert confirmed.pinned_binding["schema"] == "ea.plugin-binding.v1"
    components = {item["layer"]: item for item in confirmed.pinned_binding["components"]}
    assert set(components) == {"strategy", "validator", "backtest"}
    identity = components["strategy"]["identity"]
    assert identity["strategy_id"] == "bounded-long-v1"
    assert identity["strategy_version"] == 1
    # The semantic identity stays id and version; the fingerprint of the wrapped
    # implementation is additive plugin evidence, not part of that identity.
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", identity["implementation_sha256"])

    job = service.get_job(confirmed.jobs[0])
    assert job.plugin_binding is not None
    assert job.plugin_binding["schema"] == "ea.plugin-binding.v1"
    assert job.plugin_binding["binding_sha256"] == confirmed.pinned_binding["binding_sha256"]
    # The existing input evidence is untouched: the binding is additive evidence.
    assert job.input_snapshot_bytes is not None
    assert b"plugin_binding" not in job.input_snapshot_bytes


def test_unselected_task_stays_unbound_and_readable(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    provider.responses.append(
        _response(
            [{"id": "call-1", "name": "propose_spec", "input": _propose_call(strategy, catalog)}]
        )
    )
    task = orchestrator.create("No explicit selection")
    confirmed = orchestrator.confirm(task.task_id)
    assert confirmed.selection is None and confirmed.pinned_binding is None
    job = service.get_job(confirmed.jobs[0])
    assert job.plugin_binding is None
    assert "plugin_binding" not in job.document()

    # A record written before capability binding existed stays readable.
    legacy = _task(request_id="legacy-request")
    orchestrator._store.save(legacy)  # noqa: SLF001
    assert legacy.task_id in {item.task_id for item in orchestrator._store.list()}  # noqa: SLF001
    assert "selection" not in legacy.document()
    assert "pinned_binding" not in legacy.document()


def test_model_cannot_override_the_user_selection(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, _service_instance, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    payload = _propose_call(strategy, catalog)
    payload["selection"] = {
        "strategy": {"plugin_id": "ea.builtin.strategy.always-flat-v1", "package_version": "1.0.0"}
    }
    provider.responses.append(
        _response([{"id": "call-1", "name": "propose_spec", "input": payload}])
    )
    task = orchestrator.create("Injection attempt", SELECTED)
    assert task.spec_draft is not None
    assert task.spec_draft.selection == SELECTED


def test_selected_strategy_must_be_the_one_the_spec_uses(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, _service_instance, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    selection = {
        "strategy": {
            "plugin_id": "ea.builtin.strategy.price-rule-entry-v1",
            "package_version": "1.0.0",
        }
    }
    for round_number in range(5):
        provider.responses.append(
            _response(
                [
                    {
                        "id": f"call-{round_number}",
                        "name": "propose_spec",
                        "input": _propose_call(strategy, catalog),
                    }
                ]
            )
        )
    task = orchestrator.create("Wrong strategy", selection)
    # The model cannot satisfy the selection by ignoring it: each round is told
    # that the spec must use the selected strategy, and the task fails honestly.
    assert task.status == "failed"
    assert task.error is not None and task.error["code"] == "too_many_rounds"
    results = [message for message in provider.calls[1]["messages"] if message.role == "tool"]
    assert any("the selected strategy capability" in message.text for message in results)


# --- rejection before any provider or engine call ----------------------------


def test_unconfigured_model_route_is_rejected_before_the_provider_runs(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    with pytest.raises(SelectionRejectedError) as caught:
        orchestrator.create(
            "Anthropic route",
            {"model": {"plugin_id": "ea.builtin.model.anthropic", "package_version": "1.0.0"}},
        )
    assert caught.value.code == "model_route_not_configured"
    assert provider.calls == []
    assert orchestrator._store.list() == []  # noqa: SLF001
    assert service.list_jobs() == []


def test_missing_tool_use_capability_is_rejected_before_the_provider_runs(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        llm.PROVIDER_PROTOCOLS,
        "deepseek",
        ("deepseek-chat-completions", llm.ModelCapabilities(False, False, True)),
    )
    orchestrator, _service_instance, provider = _orchestrator_with_settings(
        tmp_path,
        monkeypatch,
        [],
        {
            "schema": "radian.settings.v1",
            "model": {"provider": "deepseek", "model": "deepseek-chat"},
        },
    )
    with pytest.raises(SelectionRejectedError) as caught:
        orchestrator.create(
            "No tool use",
            {"model": {"plugin_id": "ea.builtin.model.deepseek", "package_version": "1.0.0"}},
        )
    assert caught.value.code == "model_capability_missing"
    assert provider.calls == []


def test_unknown_interface_major_and_missing_version_are_rejected_in_a_recipe(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, _service_instance, _provider = _fake_orchestrator(tmp_path, monkeypatch, [])

    def newer_major(_service: Any, _model_route: Any = None) -> PluginCatalogV1:
        manifest = _strategy_manifest(interface=InterfaceRangeV1(KIND_INTERFACES["strategy"], 2, 0))
        return PluginCatalogV1([manifest], [TEST_ADAPTER])

    monkeypatch.setattr("ea.web.research.builtin_catalog", newer_major)
    rejected = orchestrator.resolve_recipe(TEST_SELECTION)
    assert rejected["usable"] is False
    assert rejected["code"] == "unknown_interface_major"

    def only_this_version(_service: Any, _model_route: Any = None) -> PluginCatalogV1:
        return PluginCatalogV1([_strategy_manifest()], [TEST_ADAPTER])

    monkeypatch.setattr("ea.web.research.builtin_catalog", only_this_version)
    missing = orchestrator.resolve_recipe(
        {"strategy": {"plugin_id": "test.capability.strategy", "package_version": "9.9.9"}}
    )
    assert missing["usable"] is False
    assert missing["code"] == "plugin_unavailable"
    usable = orchestrator.resolve_recipe(TEST_SELECTION)
    assert usable["usable"] is True
    assert usable["binding"]["components"][0]["identity"]["strategy_id"] == "alpha"


# --- the model route is part of the frozen binding ---------------------------

ROUTE: dict[str, Any] = {
    "schema": "radian.settings.v1",
    "model": {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "base_url": "https://example.invalid/v1",
        # A fixture placeholder. Nothing here reads a real credential or secret.
        "api_key": "fixture-key-not-a-credential",
    },
}
MODEL_SELECTION = {"model": {"plugin_id": "ea.builtin.model.deepseek", "package_version": "1.0.0"}}


def _route_orchestrator(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, provider: FakeProvider
) -> tuple[ResearchOrchestrator, RadianSettings]:
    """An orchestrator over one explicit, non-secret model route."""
    settings = _settings_store(tmp_path, monkeypatch)
    settings.save(ROUTE)
    service = _service(tmp_path)
    orchestrator = ResearchOrchestrator(ResearchStore(tmp_path / "workspace"), service, settings)
    cast(Any, orchestrator)._provider = provider  # noqa: SLF001 — test seam
    return orchestrator, settings


class _ReroutingProvider(FakeProvider):
    """A scripted provider that reconfigures the host route after each call.

    The model side stays a fixture and never route evidence: no request leaves
    this process. It only makes a settings change land between two rounds, which
    is what an operator editing the configuration mid-run does.
    """

    def __init__(self, responses: list[Any], reroute: Callable[[], None]) -> None:
        super().__init__(responses)
        self._reroute = reroute

    def complete(self, **kwargs: Any) -> ModelResponse:
        response = super().complete(**kwargs)
        self._reroute()
        return response


def test_the_frozen_model_identity_names_the_route_it_was_accepted_on(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider([])
    orchestrator, settings = _route_orchestrator(tmp_path, monkeypatch, provider)
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    provider.responses.append(
        _response(
            [{"id": "call-1", "name": "propose_spec", "input": _propose_call(strategy, catalog)}]
        )
    )
    task = orchestrator.create("A run on one route", MODEL_SELECTION)
    confirmed = orchestrator.confirm(task.task_id, auto_run=False)
    assert confirmed.pinned_binding is not None
    components = {item["layer"]: item for item in confirmed.pinned_binding["components"]}
    identity = components["model"]["identity"]
    assert identity["provider"] == "deepseek"
    assert identity["model"] == "deepseek-chat"
    assert identity["endpoint"] == "https://example.invalid/v1"
    assert str(identity["implementation_sha256"]).startswith("sha256:")
    # Non-secret facts only: no credential and no key reference is ever recorded.
    assert "api_key" not in identity
    assert "api_key_env" not in identity

    # The same provider on another model is no longer the accepted recipe, and
    # the refusal happens before the provider is asked again.
    calls_before = list(provider.calls)
    settings.save({**ROUTE, "model": {**ROUTE["model"], "model": "deepseek-reasoner"}})
    reloaded = orchestrator._store.get(task.task_id)  # noqa: SLF001
    with pytest.raises(SelectionRejectedError) as caught:
        orchestrator._recheck_selection(reloaded)  # noqa: SLF001 — the dispatch guard
    assert caught.value.code == "binding_drift"
    assert provider.calls == calls_before


def test_a_route_that_disappears_between_rounds_stops_the_next_dispatch(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings_store(tmp_path, monkeypatch)
    settings.save(ROUTE)
    orchestrator = ResearchOrchestrator(
        ResearchStore(tmp_path / "workspace"), _service(tmp_path), settings
    )
    catalog = orchestrator._catalog()  # noqa: SLF001
    payload = _propose_call(catalog[0]["strategy_id"], catalog)
    # A proposal the spec validator refuses keeps the loop going to a second round.
    payload["strategy_id"] = "not-a-registered-strategy"

    def unconfigure() -> None:
        settings.save({"schema": "radian.settings.v1"})

    provider = _ReroutingProvider(
        [_response([{"id": "call-1", "name": "propose_spec", "input": payload}])], unconfigure
    )
    cast(Any, orchestrator)._provider = provider  # noqa: SLF001 — test seam

    task = orchestrator.create("A run whose route goes away", MODEL_SELECTION)
    assert task.status == "failed"
    assert task.error is not None and task.error["code"] == "model_route_not_configured"
    # One round reached the provider; the second was refused before it could.
    assert len(provider.calls) == 1
    assert task.usage["model_calls"] == 1
    assert task.reservation is None


# --- the frozen binding ------------------------------------------------------


def _confirmed_with_selection(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[ResearchOrchestrator, Any, Any]:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    provider.responses.append(
        _response(
            [{"id": "call-1", "name": "propose_spec", "input": _propose_call(strategy, catalog)}]
        )
    )
    task = orchestrator.create("Frozen binding", SELECTED)
    return orchestrator, service, orchestrator.confirm(task.task_id, auto_run=False)


def test_a_changed_catalog_cannot_alter_the_accepted_binding(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, confirmed = _confirmed_with_selection(tmp_path, monkeypatch)
    frozen = confirmed.pinned_binding
    monkeypatch.setattr(
        "ea.web.research.builtin_catalog",
        lambda _service, _model_route=None: PluginCatalogV1([], []),
    )
    failed = orchestrator._start_backtest(confirmed.task_id)  # noqa: SLF001 — the product path
    assert failed.status == "failed"
    assert failed.error is not None and failed.error["code"] == "plugin_unavailable"
    assert failed.pinned_binding == frozen
    assert failed.usage["backtests"] == 0
    assert service.list_jobs() == []


def test_a_redeclared_version_under_the_same_number_is_detected(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, confirmed = _confirmed_with_selection(tmp_path, monkeypatch)

    def redeclared(_service: Any, _model_route: Any = None) -> PluginCatalogV1:
        manifest = _strategy_manifest(
            plugin_id="ea.builtin.strategy.bounded-long-v1",
            config_schema={"strategy_id": "bounded-long-v1", "revision": 2},
        )
        return PluginCatalogV1([manifest], [TEST_ADAPTER])

    monkeypatch.setattr("ea.web.research.builtin_catalog", redeclared)
    failed = orchestrator._start_backtest(confirmed.task_id)  # noqa: SLF001
    assert failed.status == "failed"
    assert failed.error is not None and failed.error["code"] == "binding_drift"
    assert service.list_jobs() == []


def test_a_changed_implementation_artifact_cannot_verify_the_same_binding(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binding follows the wrapped implementation, not only its manifest."""
    orchestrator, service, confirmed = _confirmed_with_selection(tmp_path, monkeypatch)
    frozen = confirmed.pinned_binding
    # The same descriptor and version, different bytes behind the capability.
    monkeypatch.setattr(
        "ea.plugin.builtins.implementation_identity",
        lambda _kind: "sha256:" + "1" * 64,
    )
    failed = orchestrator._start_backtest(confirmed.task_id)  # noqa: SLF001 — the product path
    assert failed.status == "failed"
    assert failed.error is not None and failed.error["code"] == "binding_drift"
    assert failed.pinned_binding == frozen
    assert failed.usage["backtests"] == 0
    assert service.list_jobs() == []


def test_confirmation_refuses_a_selection_that_no_longer_resolves(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, service, provider = _fake_orchestrator(tmp_path, monkeypatch, [])
    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    provider.responses.append(
        _response(
            [{"id": "call-1", "name": "propose_spec", "input": _propose_call(strategy, catalog)}]
        )
    )
    task = orchestrator.create("Confirm under a changed catalog", SELECTED)
    monkeypatch.setattr(
        "ea.web.research.builtin_catalog",
        lambda _service, _model_route=None: PluginCatalogV1([], []),
    )
    with pytest.raises(SelectionRejectedError):
        orchestrator.confirm(task.task_id)
    reloaded = orchestrator._store.get(task.task_id)  # noqa: SLF001
    assert reloaded.pinned_binding is None
    assert reloaded.jobs == ()
    assert service.list_jobs() == []


# --- API surface -------------------------------------------------------------


def test_plugin_api_lists_catalog_and_resolves_a_recipe(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RADIAN_SETTINGS_FILE", str(tmp_path / "settings.json"))
    app = create_app(_app_settings(tmp_path, monkeypatch))
    with TestClient(app) as client:
        catalog = client.get("/api/plugins/catalog", headers=HOST)
        assert catalog.status_code == 200
        document = catalog.json()
        assert document["schema"] == "ea.plugin-catalog.v1"
        ids = {entry["plugin_id"] for entry in document["plugins"]}
        assert "ea.builtin.strategy.bounded-long-v1" in ids
        assert "ea.builtin.backtest.event-replay" in ids
        assert "ea.builtin.validator.local-scenario-input" in ids

        resolved = client.post(
            "/api/plugins/recipe/resolve", headers=WRITE_HEADERS, json={"selection": SELECTED}
        )
        assert resolved.status_code == 200
        recipe = resolved.json()
        assert recipe["usable"] is True
        assert recipe["binding"]["schema"] == "ea.plugin-binding.v1"

        unknown = client.post(
            "/api/plugins/recipe/resolve",
            headers=WRITE_HEADERS,
            json={
                "selection": {
                    "strategy": {
                        "plugin_id": "ea.builtin.strategy.absent",
                        "package_version": "1.0.0",
                    }
                }
            },
        )
        assert unknown.json()["usable"] is False
        assert unknown.json()["code"] == "plugin_unavailable"

        rejected = client.post(
            "/api/research/tasks",
            headers=WRITE_HEADERS,
            json={
                "idea": "Anthropic route",
                "selection": {
                    "model": {
                        "plugin_id": "ea.builtin.model.anthropic",
                        "package_version": "1.0.0",
                    }
                },
            },
        )
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "selection_rejected"


# --- one whole selected offline run ------------------------------------------


def _await_job(service: WebService, job_id: str) -> JobRecord:
    """Wait on the existing job record until the real offline engine settles."""
    for _ in range(600):
        record = service.get_job(job_id)
        if record.status not in {"accepted", "running"}:
            return record
        time.sleep(0.01)
    raise AssertionError("the offline engine did not settle in time")


def test_a_selected_recipe_runs_the_real_engine_over_registered_strict_data(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strict local CSV data, a selected recipe, the engine's own report, restart.

    The model side is a scripted fixture: it is never route evidence and no
    request leaves this process. Everything after the proposal is the existing
    engine, its own report and the existing job and input evidence.
    """
    root = _scenario_root(tmp_path)
    data_root = tmp_path / "data"
    data_root.mkdir()
    dataset = data_root / "research.csv"
    dataset.write_bytes(load_backtest_scenario(root / "bounded-long.yaml").data_path.read_bytes())

    settings = _settings_store(tmp_path, monkeypatch)
    store = ResearchStore(tmp_path / "workspace")
    service = WebService(root, tmp_path / "workspace", data_root=data_root)
    service.start()
    orchestrator = ResearchOrchestrator(store, service, settings)
    provider = FakeProvider([])
    cast(Any, orchestrator)._provider = provider  # noqa: SLF001 — test seam

    catalog = orchestrator._catalog()  # noqa: SLF001
    strategy = catalog[0]["strategy_id"]
    selection = {
        "data": {"plugin_id": "ea.builtin.data.research.csv", "package_version": "1.0.0"},
        "strategy": {
            "plugin_id": f"ea.builtin.strategy.{strategy}",
            "package_version": "1.0.0",
        },
        "validator": {
            "plugin_id": "ea.builtin.validator.local-scenario-input",
            "package_version": "1.0.0",
        },
        "backtest": {
            "plugin_id": "ea.builtin.backtest.event-replay",
            "package_version": "1.0.0",
        },
    }
    provider.responses.append(
        _response(
            [
                {
                    "id": "call-1",
                    "name": "propose_spec",
                    "input": _propose_call(strategy, catalog, dataset_id="research.csv"),
                }
            ]
        )
    )
    task = orchestrator.create("A whole selected offline run", selection)
    assert task.status == "awaiting_confirmation", task.error
    assert task.spec_draft is not None and task.spec_draft.dataset_id == "research.csv"

    confirmed = orchestrator.confirm(task.task_id)
    assert confirmed.jobs, confirmed.error
    frozen = confirmed.pinned_binding
    assert frozen is not None
    layers = {item["layer"] for item in frozen["components"]}
    assert layers == {"data", "strategy", "validator", "backtest"}
    job_id = confirmed.jobs[0]

    record = _await_job(service, job_id)
    assert record.status == "succeeded", record.message
    assert record.plugin_binding == frozen
    final = orchestrator.refresh(task.task_id)
    assert final.status == "succeeded"
    assert final.pinned_binding == frozen

    # The engine's own formal report, over the strategy and data this run bound.
    report = service.report(job_id)
    document = json.loads(report)
    assert document["run_id"] == record.engine_run_id
    assert str(document["schema"]).startswith("ea.backtest-report.")
    assert document["source"]["strategy"]["id"] == strategy
    assert record.input_snapshot_bytes is not None
    assert b"research.csv" in record.input_snapshot_bytes

    # A restart over the same workspace retains the same recipe, report and job.
    service.stop()
    reopened_service = WebService(root, tmp_path / "workspace", data_root=data_root)
    reopened_service.start()
    reopened = ResearchOrchestrator(
        ResearchStore(tmp_path / "workspace"), reopened_service, settings
    )
    recovered_provider = FakeProvider([])
    cast(Any, reopened)._provider = recovered_provider  # noqa: SLF001 — test seam
    try:
        recovered = reopened.refresh(task.task_id)
        assert recovered.status == "succeeded"
        assert recovered.pinned_binding == frozen
        assert recovered.jobs == confirmed.jobs
        assert reopened_service.get_job(job_id).plugin_binding == frozen
        assert reopened_service.report(job_id) == report
        assert recovered_provider.calls == []
    finally:
        reopened_service.stop()
