"""One workspace-local strategy package: explicit trust, then the real engine.

The sentinel fixture is self-owned and harmless: the package's Python writes one
file when it is executed, which is exactly what lets these tests observe — rather
than claim — that inspection, registration, the static catalog, a restart read and
a dry activation never run it. The container is assembled statically from a
canonical manifest and the source itself, so the marker cannot appear while the
fixture is being built. Nothing here reaches a network, a provider or a real
credential; the model side is the scripted, offline ``FakeProvider``.
"""

from __future__ import annotations

import base64
import json
import os
import time
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from fastapi.testclient import TestClient

from ea.plugin.local_strategy import (
    LOCAL_STRATEGY_ENTRYPOINT,
    local_strategy_plugin_id,
)
from ea.plugin.manifest import (
    MANIFEST_SCHEMA_VERSION,
    RUNTIME_PROFILE,
    InterfaceRangeV1,
    ManifestError,
    PluginManifestV1,
    compute_artifact_digest,
    seal_manifest,
)
from ea.strategy.package import _container
from ea.web.app import WebSettings, create_app
from ea.web.local_packages import LOCAL_SCENARIO_ID, SLOT_DIRECTORY
from ea.web.research import ResearchOrchestrator, ResearchStore
from ea.web.service import JobRecord, WebBoundaryError, WebService
from unit.test_web_research import (
    WRITE_HEADERS,
    FakeProvider,
    _propose_call,
    _response,
    _scenario_root,
    _settings_store,
)
from unit.test_web_uploads import HEADER, _capture

BASE_SCENARIO_ID = "bounded-long.yaml"
FORBIDDEN_LOG = "package-was-executed.txt"
STRATEGY_ID = "local-probe-entry-v1"
PACKAGE_ID = "example.local-probe"
PACKAGE_VERSION = "1.0.0"
SOURCES = {
    "schema_version": 1,
    "package_id": PACKAGE_ID,
    "strategy": {
        "id": STRATEGY_ID,
        "version": 1,
        "display_name": "Local probe",
        "parameters": [
            {
                "name": "target_quantity",
                "type": "decimal",
                "required": True,
                "default": "2",
                "static_minimum": "0",
                "static_maximum": None,
            }
        ],
        "outcome_mode": "optional_single_long_entry",
    },
}


def _package(sentinel: Path) -> bytes:
    """One captured ``.eastrategy``, assembled without executing anything.

    The container is built from the canonical manifest document and the module
    source exactly as ``strategy.py`` should contain it, so no part of this fixture
    imports, compiles or runs the module: the marker below can only appear once the
    engine itself runs those bytes.
    """
    source = (
        "from pathlib import Path\n"
        "from ea.strategy.sdk_v1 import StrategyDecisionV1\n"
        f"Path({str(sentinel)!r}).write_text('executed')\n"
        "def validate_parameters(parameters, context): pass\n"
        "class Logic:\n"
        " def __init__(self, parameters): self.p = parameters\n"
        " def on_bar(self, bar):\n"
        '  return StrategyDecisionV1(self.p["target_quantity"])\n'
        "def create_logic(parameters): return Logic(parameters)\n"
    )
    canonical = json.dumps(
        SOURCES, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return _container(canonical, source.encode("utf-8"))


def _declaration(**overrides: Any) -> PluginManifestV1:
    """One complete declaration: real interface, real profile, blank digest."""
    values: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "plugin_id": local_strategy_plugin_id(PACKAGE_ID),
        "kind": "strategy",
        "package_version": PACKAGE_VERSION,
        "artifact_digest": "",
        "interface": InterfaceRangeV1("ea.strategy-contract", 1, 0),
        "entrypoint": LOCAL_STRATEGY_ENTRYPOINT,
        "provides": ("strategy.research",),
        "requires": (),
        "dependencies": (),
        "effects": ("local_compute",),
        "runtime_profile": RUNTIME_PROFILE,
        "config_schema": {},
        "author": "the package author",
        "license": "the licence it is distributed under",
    }
    values.update(overrides)
    return PluginManifestV1(**values)


def _body(**overrides: Any) -> dict[str, Any]:
    """The same complete declaration as JSON data, digest still blank."""
    return _declaration(**overrides).document()


def _manifest() -> dict[str, Any]:
    """The same complete declaration, with the digest this host would compute."""
    return seal_manifest(_declaration()).document()


def _grant_request(payload: bytes, **overrides: Any) -> dict[str, Any]:
    manifest = _manifest()
    values: dict[str, Any] = {
        "manifest": manifest,
        "payload": payload,
        "trusted": True,
        "effects": ("local_compute",),
        "base_scenario_id": BASE_SCENARIO_ID,
        "expected_plugin_id": manifest["plugin_id"],
        "expected_package_version": manifest["package_version"],
        "expected_manifest_digest": manifest["artifact_digest"],
        "expected_implementation_digest": "sha256:" + sha256(payload).hexdigest(),
    }
    values.update(overrides)
    return values


def _service(tmp_path: Path, workspace: Path | None = None) -> WebService:
    service = WebService(_scenario_root(tmp_path), workspace or tmp_path / "workspace")
    service.start()
    return service


def _settings(tmp_path: Path) -> WebSettings:
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>RADIAN</title>", encoding="utf-8")
    return WebSettings(
        scenario_root=_scenario_root(tmp_path),
        workspace=tmp_path / "workspace",
        ui_dir=ui,
        port=8765,
    )


def _boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, responses: list[Any] | None = None
) -> tuple[ResearchOrchestrator, WebService, FakeProvider]:
    service = _service(tmp_path)
    orchestrator = ResearchOrchestrator(
        ResearchStore(tmp_path / "workspace"), service, _settings_store(tmp_path, monkeypatch)
    )
    provider = FakeProvider(list(responses or []))
    cast(Any, orchestrator)._provider = provider  # test seam, same interface as the real one
    return orchestrator, service, provider


def _await_job(service: WebService, job_id: str) -> JobRecord:
    for _ in range(600):
        record = service.get_job(job_id)
        if record.status not in {"accepted", "running"}:
            return record
        time.sleep(0.01)
    raise AssertionError("the offline engine did not settle in time")


def _mapped_identity_publish(service: WebService) -> dict[str, Any]:
    """Publish the strict capture through an all-identity explicit mapping."""
    return service.uploads.publish(
        _capture(4),
        mapping={
            "columns": {field: field for field in HEADER.split(",")},
            "constants": {},
        },
        time_declaration={
            "timezone": "UTC",
            "formats": {
                "interval_start": "utc-microseconds",
                "interval_end": "utc-microseconds",
                "available_at": "utc-microseconds",
            },
        },
        availability_policy="ea.availability.provided-in-capture.v1",
        file_name="vendor-bars.csv",
    )


def test_inspection_and_registration_execute_nothing(tmp_path: Path) -> None:
    sentinel = tmp_path / FORBIDDEN_LOG
    payload = _package(sentinel)
    service = _service(tmp_path)
    try:
        inspected = service.inspect_local_strategy(manifest=_manifest(), payload=payload)
        assert inspected["static_only"] is True
        assert inspected["trust_established"] is False
        assert inspected["plugin_id"] == local_strategy_plugin_id(PACKAGE_ID)
        assert not sentinel.exists()

        registered = service.register_local_strategy(**_grant_request(payload))
        assert registered["trusted"] is True
        # Registration captures and seals the facts; it never reads the Python.
        assert not sentinel.exists()

        summary = service.validate_scenario(LOCAL_SCENARIO_ID)
        assert summary["valid"] is True
        assert summary["scenario_id"] == LOCAL_SCENARIO_ID
        # Unlike static inspect and register above, this validates the retained
        # scenario against a human-trusted grant, so running the package's
        # parameter validator is the expected, legitimate effect. The marker below
        # is the observation of that trusted validation, not a static path.
        assert sentinel.exists()
        assert service.list_jobs() == []
    finally:
        service.stop()


def test_an_untrusted_or_drifted_registration_is_refused_before_any_job(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / FORBIDDEN_LOG
    payload = _package(sentinel)
    service = _service(tmp_path)
    slot = tmp_path / "workspace" / SLOT_DIRECTORY
    try:
        refusals: list[dict[str, Any]] = [
            {"trusted": False},
            {"effects": ()},
            {"expected_implementation_digest": "sha256:" + "0" * 64},
            {"expected_package_version": "9.9.9"},
            {"expected_manifest_digest": "sha256:" + "1" * 64},
            {"base_scenario_id": "not-registered.yaml"},
        ]
        for overrides in refusals:
            with pytest.raises((ManifestError, WebBoundaryError, ValueError)):
                service.register_local_strategy(**_grant_request(payload, **overrides))
        # Nothing was captured, nothing became selectable, no work was created, and
        # no partial slot was left behind either.
        assert service.local_strategy_status()["registered"] is False
        assert service.local_strategy_facts() is None
        assert service.list_jobs() == []
        assert list(slot.iterdir()) == []
        assert not sentinel.exists()
    finally:
        service.stop()


def test_a_local_sourced_base_scenario_is_refused_before_any_loader(tmp_path: Path) -> None:
    """A base naming another local package is refused by a static read, not by a loader.

    The point is the order: this host reads the registered YAML itself, so the base is
    refused before ``registry.load`` could have had to import that other package's
    Python to find out what it was.
    """
    sentinel = tmp_path / FORBIDDEN_LOG
    payload = _package(sentinel)
    root = _scenario_root(tmp_path)
    base = cast(dict[str, Any], yaml.safe_load((root / BASE_SCENARIO_ID).read_text()))
    base["strategy"] = {
        "id": "other-local-entry-v1",
        "version": 1,
        "source": {
            "kind": "local-package",
            "package_id": "example.other",
            "artifact_sha256": "0" * 64,
        },
        "parameters": {"target_quantity": "2"},
    }
    (root / "local-base.yaml").write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")
    service = _service(tmp_path)
    slot = tmp_path / "workspace" / SLOT_DIRECTORY
    try:
        with pytest.raises(ManifestError) as refused:
            service.register_local_strategy(
                **_grant_request(payload, base_scenario_id="local-base.yaml")
            )
        assert refused.value.code == "base_scenario_local"
        assert not slot.exists() or list(slot.iterdir()) == []
        assert service.list_jobs() == []
        assert not sentinel.exists()
    finally:
        service.stop()


def test_a_symlinked_slot_is_refused_and_writes_nothing_outside(tmp_path: Path) -> None:
    sentinel = tmp_path / FORBIDDEN_LOG
    payload = _package(sentinel)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / SLOT_DIRECTORY).symlink_to(outside, target_is_directory=True)
    service = _service(tmp_path, workspace)
    try:
        with pytest.raises((ManifestError, WebBoundaryError, ValueError)):
            service.register_local_strategy(**_grant_request(payload))
        # The link was refused rather than followed: nothing landed outside.
        assert list(outside.iterdir()) == []
        assert service.local_strategy_status()["trusted"] is False
        assert service.local_strategy_status()["registered"] is False
        assert service.local_strategy_facts() is None
        assert not sentinel.exists()
    finally:
        service.stop()


def test_a_replaced_slot_directory_is_refused_before_any_write(tmp_path: Path) -> None:
    sentinel = tmp_path / FORBIDDEN_LOG
    payload = _package(sentinel)
    service = _service(tmp_path)
    slot = tmp_path / "workspace" / SLOT_DIRECTORY
    try:
        assert service.register_local_strategy(**_grant_request(payload))["trusted"] is True
        # Somebody swaps the initialized slot directory for a real, empty one: the
        # write must be refused, not redirected into the directory now at that name.
        os.rename(slot, tmp_path / "replaced-slot")
        os.mkdir(slot)
        with pytest.raises((ManifestError, WebBoundaryError, ValueError)):
            service.register_local_strategy(**_grant_request(payload))
        assert list(slot.iterdir()) == []
        assert service.local_strategy_status()["trusted"] is False
        assert service.local_strategy_facts() is None
        assert not sentinel.exists()
    finally:
        service.stop()


def test_the_virtual_scenario_and_its_selection_live_only_while_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = tmp_path / FORBIDDEN_LOG
    payload = _package(sentinel)
    orchestrator, service, _provider = _boot(tmp_path, monkeypatch)
    try:
        assert service.local_strategy_facts() is None
        # The baseline: only the one scenario research can actually run on this
        # fixture. The local slot adds nothing until it is trusted.
        assert [entry["scenario_id"] for entry in orchestrator._catalog()] == [BASE_SCENARIO_ID]
        with pytest.raises((WebBoundaryError, ValueError)):
            service.register_local_strategy(**_grant_request(payload, trusted=False))

        service.register_local_strategy(**_grant_request(payload))
        facts = service.local_strategy_facts()
        assert facts is not None
        assert facts["scenario_id"] == LOCAL_SCENARIO_ID
        assert facts["strategy_id"] == STRATEGY_ID
        base = cast(
            dict[str, Any], json.loads(service.registry.load(BASE_SCENARIO_ID).canonical_bytes)
        )
        assert facts["instrument"] == {
            "venue": base["instrument"]["venue"],
            "symbol": base["instrument"]["symbol"],
        }
        assert facts["initial_cash"] == base["funding"]["initial_cash"]
        parameters = cast(list[dict[str, Any]], facts["parameters"])
        assert [item["name"] for item in parameters] == ["target_quantity"]
        assert [item["default"] for item in parameters] == ["2"]
        assert LOCAL_SCENARIO_ID in [entry["scenario_id"] for entry in orchestrator._catalog()]
        plugin_id = local_strategy_plugin_id(PACKAGE_ID)
        offered = {entry["plugin_id"]: entry for entry in orchestrator.plugin_catalog()["plugins"]}
        assert offered[plugin_id]["usable"] is True
        assert offered[plugin_id]["entrypoint"] == LOCAL_STRATEGY_ENTRYPOINT

        binding = service.local_strategy_binding()
        assert binding is not None
        components = cast(list[dict[str, Any]], binding["components"])
        assert [item["plugin_id"] for item in components] == [plugin_id]
        validated = service.validate_scenario(LOCAL_SCENARIO_ID)
        with pytest.raises((WebBoundaryError, ValueError)):
            # The slot is never reachable through a direct call that named no binding.
            service.create_job(
                scenario_id=LOCAL_SCENARIO_ID,
                input_identity=cast(dict[str, object], validated["input_identity"]),
                request_id="unbound",
            )
        assert service.list_jobs() == []

        assert service.revoke_local_strategy()["trusted"] is False
        assert service.local_strategy_facts() is None
        assert service.local_strategy_binding() is None
        assert local_strategy_plugin_id(PACKAGE_ID) not in [
            entry["plugin_id"] for entry in orchestrator.plugin_catalog()["plugins"]
        ]
    finally:
        service.stop()


def test_a_full_research_binding_activates_the_slot_without_running_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binding research actually freezes carries data, validator and strategy.

    Activation is asked for exactly that complete binding and must verify only the
    strategy component against this slot, then hand back the engine catalog without
    executing the package — the marker is the observation, not a claim.
    """
    sentinel = tmp_path / FORBIDDEN_LOG
    payload = _package(sentinel)
    orchestrator, service, provider = _boot(tmp_path, monkeypatch)
    try:
        dataset_id = cast(str, _mapped_identity_publish(service)["dataset_id"])
        service.register_local_strategy(**_grant_request(payload))
        plugin_id = local_strategy_plugin_id(PACKAGE_ID)
        selection = {
            "data": {"plugin_id": f"ea.builtin.data.{dataset_id}", "package_version": "1.0.0"},
            "strategy": {"plugin_id": plugin_id, "package_version": PACKAGE_VERSION},
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
                        "input": _propose_call(
                            STRATEGY_ID,
                            orchestrator._catalog(),
                            dataset_id=dataset_id,
                            parameters={"target_quantity": "2"},
                        ),
                    }
                ]
            )
        )
        task = orchestrator.create("Dry activation of the trusted local package", selection)
        assert task.status == "awaiting_confirmation", task.error
        confirmed = orchestrator.confirm(task.task_id, auto_run=False)
        assert confirmed.jobs == ()
        frozen = confirmed.pinned_binding
        assert frozen is not None
        layers = {item["layer"]: item for item in cast(list[dict[str, Any]], frozen["components"])}
        assert sorted(layers) == ["backtest", "data", "strategy", "validator"]
        assert layers["strategy"]["plugin_id"] == plugin_id

        # The exact call research makes before dispatch, over the whole binding.
        catalog = service._local_catalog(cast(dict[str, object], frozen))  # noqa: SLF001
        # ``catalog.get`` is the distribution-owned builtin API, so the local package is
        # addressed by its exact document instead. ``resolve`` matches the declared
        # source against the captured artifact and returns its entry; it imports
        # nothing, which the absent marker still shows.
        capability = service.local_strategy_capability()
        assert capability is not None
        entry = catalog.resolve(
            {
                "id": STRATEGY_ID,
                "version": 1,
                "source": capability.source(),
                "parameters": {"target_quantity": "2"},
            }
        )
        assert entry.descriptor.strategy_id == STRATEGY_ID
        assert not sentinel.exists()
        assert service.list_jobs() == []
    finally:
        service.stop()


def test_http_inspection_seals_a_complete_manifest_and_refuses_a_wrong_digest(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / FORBIDDEN_LOG
    payload = _package(sentinel)
    body = _body()
    with TestClient(create_app(_settings(tmp_path))) as client:
        # A complete declarative body may leave artifact_digest blank; the host fills
        # exactly that one field and returns the sealed manifest it inspected.
        response = client.post(
            "/api/plugins/local-strategy/inspect",
            headers=WRITE_HEADERS,
            json={
                "content_base64": base64.b64encode(payload).decode("ascii"),
                "manifest": body,
            },
        )
        assert response.status_code == 200, response.text
        inspected = response.json()
        assert inspected["static_only"] is True
        assert inspected["trust_established"] is False
        assert inspected["manifest_digest"] == compute_artifact_digest(body)
        assert inspected["manifest"]["artifact_digest"] == compute_artifact_digest(body)
        assert not sentinel.exists()

        # A digest the host did not compute is refused, never repaired.
        wrong = client.post(
            "/api/plugins/local-strategy/inspect",
            headers=WRITE_HEADERS,
            json={
                "content_base64": base64.b64encode(payload).decode("ascii"),
                "manifest": {**body, "artifact_digest": "sha256:" + "0" * 64},
            },
        )
        assert wrong.status_code == 422, wrong.text
        assert wrong.json()["error"]["code"] == "digest_mismatch"

        # Registration without the explicit decision is refused, and the slot stays
        # empty: no trust was recorded and nothing was captured.
        refused = client.post(
            "/api/plugins/local-strategy/register",
            headers=WRITE_HEADERS,
            json={
                "content_base64": base64.b64encode(payload).decode("ascii"),
                "manifest": body,
                "effects": ["local_compute"],
                "base_scenario_id": BASE_SCENARIO_ID,
                "expected_plugin_id": body["plugin_id"],
                "expected_package_version": PACKAGE_VERSION,
                "expected_manifest_digest": compute_artifact_digest(body),
                "expected_implementation_digest": "sha256:" + sha256(payload).hexdigest(),
            },
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "trust_required"
        slot = client.get("/api/plugins/local-strategy", headers=WRITE_HEADERS)
        assert slot.status_code == 200, slot.text
        assert slot.json()["registered"] is False
        assert slot.json()["trusted"] is False
        assert not sentinel.exists()


def test_a_mapped_version_and_a_trusted_local_strategy_run_the_real_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = tmp_path / FORBIDDEN_LOG
    payload = _package(sentinel)
    orchestrator, service, provider = _boot(tmp_path, monkeypatch)
    try:
        dataset_id = cast(str, _mapped_identity_publish(service)["dataset_id"])
        service.register_local_strategy(**_grant_request(payload))
        plugin_id = local_strategy_plugin_id(PACKAGE_ID)

        selection = {
            "data": {"plugin_id": f"ea.builtin.data.{dataset_id}", "package_version": "1.0.0"},
            "strategy": {"plugin_id": plugin_id, "package_version": PACKAGE_VERSION},
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
                        "input": _propose_call(
                            STRATEGY_ID,
                            orchestrator._catalog(),
                            dataset_id=dataset_id,
                            parameters={"target_quantity": "2"},
                        ),
                    }
                ]
            )
        )
        task = orchestrator.create("Run the trusted local package", selection)
        assert task.status == "awaiting_confirmation", task.error
        assert task.spec_draft is not None
        assert task.spec_draft.strategy_id == STRATEGY_ID

        confirmed = orchestrator.confirm(task.task_id)
        assert confirmed.jobs, confirmed.error
        frozen = confirmed.pinned_binding
        assert frozen is not None
        layers = {item["layer"]: item for item in cast(list[dict[str, Any]], frozen["components"])}
        assert layers["strategy"]["plugin_id"] == plugin_id
        assert layers["data"]["identity"]["origin"] == "upload"

        job_id = confirmed.jobs[0]
        record = _await_job(service, job_id)
        assert record.status == "succeeded", record.message
        assert record.plugin_binding == frozen
        assert record.scenario_id == LOCAL_SCENARIO_ID
        assert sentinel.exists()
        report = service.report(job_id).decode("utf-8")
        assert PACKAGE_ID in report
        assert dataset_id in report
    finally:
        service.stop()
