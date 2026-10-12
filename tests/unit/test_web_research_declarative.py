"""The batch2 vertical slice: two data entrances, declarations, two real engine runs.

Everything the model side does here is a scripted fixture: it is never route
evidence and no request leaves this process. Everything after the proposal is the
existing engine, its own report, the existing job/input evidence, the controlled
upload store and the closed declaration store.

Two complete real engine runs are covered, one per data entrance:

* an uploaded, explicitly-policied dataset version bound to a declarative
  strategy and a declarative validator, and
* the registered read-only CSV bound to the built-in strategy, validator and
  backtest capabilities.

Every other test here is a fail-before-effect boundary: an illegal primitive, a
removed declaration (never a latest fallback), a provider timeout and the HTTP
request bounds. None of them dispatches an engine run.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from fastapi.testclient import TestClient

from ea.plugin.declarative import (
    DECLARATION_SCHEMA,
    STRATEGY_TARGET_PLUGIN_ID,
    VALIDATOR_TARGET_PLUGIN_ID,
    decode_declaration,
    rule_digest,
    seal_declaration,
)
from ea.product.scenario import load_backtest_scenario
from ea.strategy.rules import decode_rule
from ea.web.app import (
    MAX_DECLARATION_REQUEST_BYTES,
    MAX_UPLOAD_REQUEST_BYTES,
    WebSettings,
    create_app,
)
from ea.web.llm import ModelProviderError
from ea.web.research import ResearchOrchestrator, ResearchStore
from ea.web.service import InputChangedError, JobRecord, WebService
from ea.web.uploads import AVAILABILITY_PROVIDED, AVAILABILITY_SIMULATED, seal_version_manifest
from unit.test_web_research import (
    HOST,
    WRITE_HEADERS,
    FakeProvider,
    _propose_call,
    _response,
    _scenario_root,
    _settings_store,
)
from unit.test_web_uploads import HEADER, START, _capture, _line, _stamp

pytestmark = pytest.mark.filterwarnings(
    "ignore:The anyio.abc.BlockingPortal alias is deprecated:DeprecationWarning"
)

# The price-only rule every declaration and scenario below fixes. ``decode_rule``
# returns the closed engine's own canonical text, and the declaration stores
# exactly that, so no formatting freedom exists between them.
RULE = decode_rule(
    '{"version":1,"when":{"op":"gt","left":{"kind":"price","field":"close"},'
    '"right":{"kind":"price","field":"open"}},"action":"ENTER_LONG"}'
).canonical
ILLEGAL_RULE = "__import__('os').system('id')"
SIMULATED_LATENCY_SECONDS = 60
SOURCE_REFERENCE = "vendor export 2026-01-05"
LICENSE_REFERENCE = "internal-research-only"
# The strict refusal codes an upload row can raise; a row this host cannot read
# exactly as written is refused, never dropped, filled or reshaped.
STRICT_REFUSALS = frozenset(
    {"invalid_record_shape", "invalid_bar", "invalid_timestamp", "malformed_csv"}
)
# One host-generated staging name: the only shape the store's sweep may remove.
STRAY_NAME = "0123456789abcdef0123456789abcdef.csv"


# --- fixtures ----------------------------------------------------------------


def _declarative_root(tmp_path: Any) -> Path:
    """The registered scenario root plus one V2 scenario for the rule strategy."""
    root: Path = _scenario_root(tmp_path)
    document = yaml.safe_load((root / "bounded-long.yaml").read_text(encoding="utf-8"))
    document["schema_version"] = 2
    document["strategy"] = {
        "id": "price-rule-entry-v1",
        "version": 1,
        "parameters": {"rule": RULE, "target_quantity": "2"},
    }
    (root / "rule-entry.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    # The scenario must be loadable on its own before any upload is bound to it.
    loaded = load_backtest_scenario(root / "rule-entry.yaml")
    assert loaded.schema_version == 2
    assert loaded.strategy_parameters["rule"] == RULE
    return root


def _data_root(tmp_path: Any, root: Path) -> Path:
    """One registered strict CSV beside the controlled upload entrance."""
    directory: Path = tmp_path / "data"
    directory.mkdir()
    (directory / "research.csv").write_bytes(
        load_backtest_scenario(root / "bounded-long.yaml").data_path.read_bytes()
    )
    return directory


def _boot(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    root: Path,
    data_root: Path | None = None,
    responses: list[Any] | None = None,
) -> tuple[ResearchOrchestrator, WebService, FakeProvider]:
    settings = _settings_store(tmp_path, monkeypatch)
    service = WebService(root, tmp_path / "workspace", data_root=data_root)
    service.start()
    orchestrator = ResearchOrchestrator(ResearchStore(tmp_path / "workspace"), service, settings)
    provider = FakeProvider(list(responses or []))
    cast(Any, orchestrator)._provider = provider  # noqa: SLF001 — test seam
    return orchestrator, service, provider


def _strategy_declaration(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": DECLARATION_SCHEMA,
        "declaration_id": "user.rule.entry",
        "kind": "strategy",
        "package_version": "1.0.0",
        "title": "Close above open entry",
        "declared_by": "user",
        "rule": RULE,
        "rule_history": 1,
        "constraints": None,
    }
    body.update(overrides)
    return seal_declaration(body)


def _validator_declaration(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": DECLARATION_SCHEMA,
        "declaration_id": "user.validator.declared-availability",
        "kind": "validator",
        "package_version": "1.0.0",
        "title": "Require a declared availability policy",
        "declared_by": "user",
        "rule": None,
        "rule_history": None,
        "constraints": {
            "min_record_count": 3,
            "max_observed_gap_count": 0,
            "require_declared_availability": True,
        },
    }
    body.update(overrides)
    return seal_declaration(body)


def _payload(document: dict[str, Any]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _register(orchestrator: ResearchOrchestrator, document: dict[str, Any]) -> dict[str, Any]:
    outcome = orchestrator.register_declaration(_payload(document))
    assert outcome["usable"] is True, outcome
    assert outcome["registered"] is True
    return outcome


def _publish(service: WebService, **overrides: Any) -> dict[str, Any]:
    """Publish one bounded capture through the controlled upload store."""
    arguments: dict[str, Any] = {
        "file_name": "vendor-bars.csv",
        "source_reference": SOURCE_REFERENCE,
        "license_reference": LICENSE_REFERENCE,
        "availability_policy": AVAILABILITY_SIMULATED,
        "latency_seconds": SIMULATED_LATENCY_SECONDS,
    }
    arguments.update(overrides)
    return service.uploads.publish(_capture(4), **arguments)


def _await_job(service: WebService, job_id: str) -> JobRecord:
    for _ in range(600):
        record = service.get_job(job_id)
        if record.status not in {"accepted", "running"}:
            return record
        time.sleep(0.01)
    raise AssertionError("the offline engine did not settle in time")


def _declarative_selection(dataset_id: str) -> dict[str, Any]:
    return {
        "data": {"plugin_id": f"ea.builtin.data.{dataset_id}", "package_version": "1.0.0"},
        "strategy": {"plugin_id": "user.rule.entry", "package_version": "1.0.0"},
        "validator": {
            "plugin_id": "user.validator.declared-availability",
            "package_version": "1.0.0",
        },
        "backtest": {"plugin_id": "ea.builtin.backtest.event-replay", "package_version": "1.0.0"},
    }


def _builtin_selection(dataset_id: str) -> dict[str, Any]:
    """The same four layers, resolved to the host's own built-in capabilities."""
    return {
        "data": {"plugin_id": f"ea.builtin.data.{dataset_id}", "package_version": "1.0.0"},
        "strategy": {
            "plugin_id": "ea.builtin.strategy.price-rule-entry-v1",
            "package_version": "1.0.0",
        },
        "validator": {
            "plugin_id": "ea.builtin.validator.local-scenario-input",
            "package_version": "1.0.0",
        },
        "backtest": {"plugin_id": "ea.builtin.backtest.event-replay", "package_version": "1.0.0"},
    }


# --- the two entrances share one catalog -------------------------------------


def test_both_data_entrances_are_listed_through_one_catalog_with_their_origin(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _declarative_root(tmp_path)
    data_root = _data_root(tmp_path, root)
    _orchestrator, service, _provider = _boot(tmp_path, monkeypatch, root=root, data_root=data_root)
    try:
        published = _publish(service)
        entries = service.dataset_catalog.list()
        by_id = {entry["dataset_id"]: entry for entry in entries}
        assert by_id["research.csv"]["origin"] == "registered"
        assert by_id[published["dataset_id"]]["origin"] == "upload"

        registered = service.dataset_catalog.inspect("research.csv")
        assert registered["origin"] == "registered"
        # A registered input carries no availability policy at all, which is
        # materially different from an asserted one.
        assert "availability_policy" not in registered

        uploaded = service.dataset_catalog.inspect(published["dataset_id"])
        assert uploaded["origin"] == "upload"
        assert uploaded["availability_policy"]["id"] == AVAILABILITY_SIMULATED
        assert uploaded["availability_policy"]["pit_verified"] is False
        assert uploaded["version_manifest"]["schema"] == "ea.local-dataset-version.v1"
        assert service.dataset_catalog.document()["schema"] == "ea.local-dataset-catalog.v1"

        # Both entrances describe the same strict decoder: one record per row.
        assert uploaded["record_count"] == 4
    finally:
        service.stop()


def test_a_simulated_policy_changes_only_availability_and_keeps_source_evidence(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raw bytes are provenance; the host corrects no value but ``available_at``."""
    root = _declarative_root(tmp_path)
    _orchestrator, service, _provider = _boot(tmp_path, monkeypatch, root=root)
    try:
        published = _publish(service)
        dataset_id = published["dataset_id"]
        raw = (service.uploads.root / "raw" / dataset_id).read_text(encoding="utf-8").splitlines()
        capture = (
            (service.uploads.root / "captures" / dataset_id)
            .read_text(encoding="utf-8")
            .splitlines()
        )
        assert len(raw) == len(capture) == 5
        assert raw[0] == capture[0] == HEADER
        for raw_line, capture_line in zip(raw[1:], capture[1:], strict=True):
            # Every economic field survives byte for byte; only availability moved.
            assert raw_line.split(",")[:14] == capture_line.split(",")[:14]
        first_end = datetime.strptime(_stamp(START + timedelta(hours=1)), "%Y-%m-%dT%H:%M:%S.%fZ")
        assert capture[1].split(",")[14] == _stamp(
            first_end.replace(tzinfo=UTC) + timedelta(seconds=SIMULATED_LATENCY_SECONDS)
        )
        # The two digests are deliberately different and both are recorded.
        assert published["raw_retained"] is True
        assert published["source_bytes_sha256"] != published["capture_sha256"]
        assert published["source"]["asserted"] is True
        assert published["source"]["declared_by"] == "user"
        assert published["source"]["reference"] == SOURCE_REFERENCE
        assert published["source"]["file_name"] == "vendor-bars.csv"
        assert published["source"]["license_reference"] == LICENSE_REFERENCE
        assert published["record_count"] == 4
        assert any("point-in-time" in limitation for limitation in published["limitations"])
    finally:
        service.stop()


# --- one whole declared run over an uploaded version -------------------------


def test_an_uploaded_version_runs_the_real_engine_under_declarations(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uploaded version + declarative strategy + declarative validator, real engine.

    The model side is the scripted offline ``FakeProvider`` fixture: it is only
    how a spec is authored here, never evidence that a real provider route works.
    """
    root = _declarative_root(tmp_path)
    data_root = _data_root(tmp_path, root)
    orchestrator, service, provider = _boot(tmp_path, monkeypatch, root=root, data_root=data_root)
    try:
        published = _publish(service)
        dataset_id = published["dataset_id"]
        _register(orchestrator, _strategy_declaration())
        _register(orchestrator, _validator_declaration())

        catalog = orchestrator._catalog()  # noqa: SLF001
        selection = _declarative_selection(dataset_id)
        provider.responses.append(
            _response(
                [
                    {
                        "id": "call-1",
                        "name": "propose_spec",
                        "input": _propose_call(
                            "price-rule-entry-v1",
                            catalog,
                            dataset_id=dataset_id,
                            parameters={"rule": RULE, "target_quantity": "2"},
                        ),
                    }
                ]
            )
        )
        task = orchestrator.create("Declared entry over an uploaded version", selection)
        assert task.status == "awaiting_confirmation", task.error
        assert task.spec_draft is not None
        assert task.spec_draft.dataset_id == dataset_id
        assert task.spec_draft.parameters["rule"] == RULE

        confirmed = orchestrator.confirm(task.task_id)
        assert confirmed.jobs, confirmed.error
        frozen = confirmed.pinned_binding
        assert frozen is not None
        layers = {item["layer"]: item for item in frozen["components"]}
        assert set(layers) == {"data", "strategy", "validator", "backtest"}
        assert layers["strategy"]["plugin_id"] == "user.rule.entry"
        assert layers["strategy"]["identity"]["wraps"] == STRATEGY_TARGET_PLUGIN_ID
        assert layers["strategy"]["identity"]["declared_rule_sha256"] == rule_digest(RULE)
        assert layers["validator"]["identity"]["wraps"] == VALIDATOR_TARGET_PLUGIN_ID
        assert layers["validator"]["identity"]["validator_constraints"] == {
            "min_record_count": 3,
            "max_observed_gap_count": 0,
            "require_declared_availability": True,
        }
        assert layers["data"]["identity"]["origin"] == "upload"
        assert layers["data"]["identity"]["availability_policy_id"] == AVAILABILITY_SIMULATED

        job_id = confirmed.jobs[0]
        record = _await_job(service, job_id)
        assert record.status == "succeeded", record.message
        assert record.plugin_binding == frozen
        assert record.engine_run_id is not None

        # The published version travels with the job, and it never claims PIT.
        frozen_version = record.dataset_version
        assert frozen_version is not None
        # One Any-valued copy: the assertions below read nested policy and source
        # facts, and the record itself is deliberately opaque here.
        version: dict[str, Any] = {key: value for key, value in frozen_version.items()}
        assert version["dataset_id"] == dataset_id
        policy = version["availability_policy"]
        assert isinstance(policy, dict)
        assert policy["id"] == AVAILABILITY_SIMULATED
        assert policy["pit_verified"] is False
        assert policy["latency_seconds"] == SIMULATED_LATENCY_SECONDS
        assert int(str(policy["evidence"]).rsplit(" ", 1)[1].rstrip("s")) == (
            SIMULATED_LATENCY_SECONDS
        )
        source = version["source"]
        assert isinstance(source, dict)
        assert source["reference"] == SOURCE_REFERENCE
        assert source["asserted"] is True
        assert record.input_identity["dataset_id"] == dataset_id
        # The bound research input is the canonical capture, not the raw upload.
        assert record.input_identity["source_sha256"] == version["capture_sha256"]
        assert version["source_bytes_sha256"] != version["capture_sha256"]
        assert record.input_identity["record_count"] == 4
        assert "dataset_version" in record.document()

        final = orchestrator.refresh(task.task_id)
        assert final.status == "succeeded"
        assert final.pinned_binding == frozen

        payload = service.report(job_id)
        report = json.loads(payload)
        assert report["run_id"] == record.engine_run_id
        assert report["schema"] == "ea.backtest-report.v1"
        assert report["source"]["strategy"]["id"] == "price-rule-entry-v1"
        # No layer of this run reports asserted or simulated availability as
        # verified point-in-time availability.
        assert b"pit_verified" not in payload
        assert record.input_snapshot_bytes is not None
        assert dataset_id.encode("ascii") in record.input_snapshot_bytes

        # A restart over the same workspace retains source, policy and binding.
        service.stop()
        reopened = WebService(root, tmp_path / "workspace", data_root=data_root)
        reopened.start()
        reopened_orchestrator = ResearchOrchestrator(
            ResearchStore(tmp_path / "workspace"), reopened, orchestrator._settings
        )  # noqa: SLF001
        recovered_provider = FakeProvider([])
        cast(Any, reopened_orchestrator)._provider = recovered_provider  # noqa: SLF001
        try:
            recovered = reopened_orchestrator.refresh(task.task_id)
            assert recovered.status == "succeeded"
            assert recovered.pinned_binding == frozen
            assert recovered.jobs == confirmed.jobs
            reopened_record = reopened.get_job(job_id)
            assert reopened_record.plugin_binding == frozen
            assert reopened_record.dataset_version == version
            assert reopened.get_job(job_id).document() == record.document()
            assert reopened.report(job_id) == payload
            assert recovered_provider.calls == []
        finally:
            reopened.stop()
    finally:
        service.stop()


# --- the same run over the registered entrance --------------------------------


def test_a_registered_input_runs_the_real_engine_with_builtin_capabilities(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registered CSV end to end: built-ins only, real engine, its own report.

    This is the second and last complete engine run of this batch. It shows the
    slice is not specific to an uploaded input: the same orchestrator, the same
    catalog, the same engine and the same report layer, over the read-only
    registered data entrance with no declaration involved.
    """
    root = _declarative_root(tmp_path)
    data_root = _data_root(tmp_path, root)
    orchestrator, service, provider = _boot(tmp_path, monkeypatch, root=root, data_root=data_root)
    try:
        catalog = orchestrator._catalog()  # noqa: SLF001
        provider.responses.append(
            _response(
                [
                    {
                        "id": "call-1",
                        "name": "propose_spec",
                        "input": _propose_call(
                            "price-rule-entry-v1",
                            catalog,
                            dataset_id="research.csv",
                            parameters={"rule": RULE, "target_quantity": "2"},
                        ),
                    }
                ]
            )
        )
        task = orchestrator.create(
            "Registered input, built-in capabilities", _builtin_selection("research.csv")
        )
        assert task.status == "awaiting_confirmation", task.error
        assert task.spec_draft is not None
        assert task.spec_draft.dataset_id == "research.csv"

        confirmed = orchestrator.confirm(task.task_id)
        assert confirmed.jobs, confirmed.error
        frozen = confirmed.pinned_binding
        assert frozen is not None
        layers = {item["layer"]: item for item in frozen["components"]}
        assert set(layers) == {"data", "strategy", "validator", "backtest"}
        assert layers["data"]["plugin_id"] == "ea.builtin.data.research.csv"
        assert layers["data"]["identity"]["origin"] == "registered"
        # A registered input carries no availability policy at all: nobody asserted
        # anything about how its bars became available, and nothing invented one.
        assert layers["data"]["identity"]["availability_policy_id"] is None
        assert layers["strategy"]["plugin_id"] == "ea.builtin.strategy.price-rule-entry-v1"
        assert layers["validator"]["plugin_id"] == "ea.builtin.validator.local-scenario-input"
        assert layers["backtest"]["plugin_id"] == "ea.builtin.backtest.event-replay"

        job_id = confirmed.jobs[0]
        record = _await_job(service, job_id)
        assert record.status == "succeeded", record.message
        assert record.plugin_binding == frozen
        assert record.engine_run_id is not None
        assert record.input_identity["dataset_id"] == "research.csv"
        assert record.input_snapshot_bytes is not None

        report = json.loads(service.report(job_id))
        assert report["schema"] == "ea.backtest-report.v1"
        assert report["run_id"] == record.engine_run_id
        assert report["source"]["strategy"]["id"] == "price-rule-entry-v1"

        final = orchestrator.refresh(task.task_id)
        assert final.status == "succeeded"
        assert final.jobs == confirmed.jobs
        assert final.pinned_binding == frozen
    finally:
        service.stop()


def test_a_registered_input_is_never_treated_as_declared_availability(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declarative validator refuses an input with no declared policy."""
    root = _declarative_root(tmp_path)
    data_root = _data_root(tmp_path, root)
    orchestrator, service, provider = _boot(tmp_path, monkeypatch, root=root, data_root=data_root)
    try:
        _register(orchestrator, _strategy_declaration())
        _register(orchestrator, _validator_declaration())
        catalog = orchestrator._catalog()  # noqa: SLF001
        selection = _declarative_selection("research.csv")
        for round_number in range(5):
            provider.responses.append(
                _response(
                    [
                        {
                            "id": f"call-{round_number}",
                            "name": "propose_spec",
                            "input": _propose_call(
                                "price-rule-entry-v1",
                                catalog,
                                dataset_id="research.csv",
                                parameters={"rule": RULE, "target_quantity": "2"},
                            ),
                        }
                    ]
                )
            )
        task = orchestrator.create("Declared validation over a registered input", selection)
        assert task.status == "failed"
        assert task.error is not None and task.error["code"] == "too_many_rounds"
        results = [
            message.text for message in provider.calls[1]["messages"] if message.role == "tool"
        ]
        assert any("availability policy" in text for text in results)
        assert task.usage["backtests"] == 0
        assert service.list_jobs() == []
    finally:
        service.stop()


# --- fail before any new effect ----------------------------------------------


def test_an_illegal_declaration_is_refused_and_registers_nothing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _declarative_root(tmp_path)
    orchestrator, service, _provider = _boot(tmp_path, monkeypatch, root=root)
    try:
        for document, code in (
            (_strategy_declaration(rule=ILLEGAL_RULE), "declaration_rule"),
            (
                _strategy_declaration(
                    rule='{"version":1,"when":{"op":"exec"},"action":"ENTER_LONG"}'
                ),
                "declaration_rule",
            ),
            (
                _strategy_declaration(declaration_id="ea.builtin.strategy.shadow"),
                "declaration_reserved",
            ),
            (_validator_declaration(constraints={"allow_everything": True}), "declaration_field"),
        ):
            outcome = orchestrator.register_declaration(_payload(document))
            assert outcome["usable"] is False, document
            assert outcome["code"] == code
            assert outcome["registered"] is False
        # Nothing was stored, so nothing new can be selected and no run exists.
        assert service.declarations.load() == ()
        assert list(service.declarations.root.glob("*.json")) == []
        assert orchestrator.declaration_catalog()["declarations"] == []
        assert service.list_jobs() == []

        # Registration is add-only: the same identity is never replaced.
        _register(orchestrator, _strategy_declaration())
        stored = service.declarations.path("user.rule.entry").read_bytes()
        duplicate = orchestrator.register_declaration(
            _payload(_strategy_declaration(title="A different body"))
        )
        assert duplicate["usable"] is False and duplicate["code"] == "declaration_exists"
        assert service.declarations.path("user.rule.entry").read_bytes() == stored
    finally:
        service.stop()


def test_a_proposal_with_an_illegal_primitive_reaches_no_engine(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _declarative_root(tmp_path)
    orchestrator, service, provider = _boot(tmp_path, monkeypatch, root=root)
    try:
        catalog = orchestrator._catalog()  # noqa: SLF001
        for round_number in range(5):
            provider.responses.append(
                _response(
                    [
                        {
                            "id": f"call-{round_number}",
                            "name": "propose_spec",
                            "input": _propose_call(
                                "price-rule-entry-v1",
                                catalog,
                                parameters={"rule": ILLEGAL_RULE, "target_quantity": "1"},
                            ),
                        }
                    ]
                )
            )
        task = orchestrator.create("A rule the closed engine does not have")
        assert task.status == "failed"
        assert task.error is not None and task.error["code"] == "too_many_rounds"
        results = [
            message.text for message in provider.calls[1]["messages"] if message.role == "tool"
        ]
        assert results and all(text.startswith("rejected:") for text in results)
        assert task.usage["backtests"] == 0
        assert task.jobs == ()
        assert service.list_jobs() == []
        assert list((tmp_path / "workspace" / "runs").iterdir()) == []
    finally:
        service.stop()


def test_a_declaration_removed_after_confirmation_stops_before_any_effect(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exact-version binding only: an absent declared version is never substituted."""
    root = _declarative_root(tmp_path)
    orchestrator, service, provider = _boot(tmp_path, monkeypatch, root=root)
    try:
        published = _publish(service)
        dataset_id = published["dataset_id"]
        _register(orchestrator, _strategy_declaration())
        _register(orchestrator, _validator_declaration())
        catalog = orchestrator._catalog()  # noqa: SLF001
        provider.responses.append(
            _response(
                [
                    {
                        "id": "call-1",
                        "name": "propose_spec",
                        "input": _propose_call(
                            "price-rule-entry-v1",
                            catalog,
                            dataset_id=dataset_id,
                            parameters={"rule": RULE, "target_quantity": "2"},
                        ),
                    }
                ]
            )
        )
        task = orchestrator.create(
            "Remove the declaration before running", _declarative_selection(dataset_id)
        )
        confirmed = orchestrator.confirm(task.task_id, auto_run=False)
        frozen = confirmed.pinned_binding
        assert frozen is not None and confirmed.jobs == ()

        service.declarations.discard("user.rule.entry")
        failed = orchestrator._start_backtest(confirmed.task_id)  # noqa: SLF001 — the product path
        assert failed.status == "failed"
        assert failed.error is not None and failed.error["code"] == "plugin_unavailable"
        assert failed.pinned_binding == frozen
        assert failed.usage["backtests"] == 0
        assert failed.jobs == ()
        assert service.list_jobs() == []
    finally:
        service.stop()


def test_an_uploaded_version_is_usable_only_through_its_selected_data_capability(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three refusals that keep an uploaded version's provenance a user decision.

    No engine run happens in any of them. An unselected draft that names an
    uploaded version is refused while it is being drafted, the direct backtest and
    batch entrances are refused before any input is materialized because they
    carry no capability binding at all, and a version whose sealed source changes
    under an already confirmed task fails its binding re-verification before
    dispatch — the case where the evidence digest is the only thing that could
    have caught it.
    """
    root = _declarative_root(tmp_path)
    orchestrator, service, provider = _boot(tmp_path, monkeypatch, root=root)
    try:
        published = _publish(service)
        dataset_id = published["dataset_id"]
        catalog = orchestrator._catalog()  # noqa: SLF001
        parameters: dict[str, object] = {"rule": RULE, "target_quantity": "2"}

        # (1) The user selected nothing, so nothing freezes this version's evidence.
        provider.responses.append(
            _response(
                [
                    {
                        "id": "call-1",
                        "name": "propose_spec",
                        "input": _propose_call(
                            "price-rule-entry-v1",
                            catalog,
                            dataset_id=dataset_id,
                            parameters=parameters,
                        ),
                    }
                ]
            )
        )
        unselected = orchestrator.create("An uploaded version nobody selected")
        assert unselected.status == "failed"
        assert unselected.error is not None
        assert unselected.error["code"] == "upload_data_capability_required"
        assert unselected.usage["backtests"] == 0
        assert unselected.jobs == ()

        # (2) The direct entrances carry no binding, so an upload is refused there
        # even though the input identity itself is the host's own, current one.
        validated = service.validate_scenario(
            "rule-entry.yaml",
            dataset_id=dataset_id,
            parameters={
                "initial_cash": "10000",
                "strategy_parameters": parameters,
                "quantity": None,
                "strategy_source": None,
            },
        )
        identity = validated["input_identity"]
        assert isinstance(identity, dict)
        assert identity["dataset_id"] == dataset_id
        with pytest.raises(InputChangedError):
            service.create_job(
                scenario_id="rule-entry.yaml",
                input_identity=identity,
                parameters={
                    "initial_cash": "10000",
                    "strategy_parameters": parameters,
                    "quantity": None,
                    "strategy_source": None,
                },
                request_id="unbound-upload-request",
            )
        with pytest.raises(InputChangedError):
            service.create_batch(
                scenario_id="rule-entry.yaml",
                input_identity=identity,
                initial_cash="10000",
                runs=[parameters, {**parameters, "target_quantity": "3"}],
            )
        assert service.list_jobs() == []

        # (3) The confirmed task's data capability is pinned to a digest of the
        # published version evidence; editing the sealed source changes it.
        _register(orchestrator, _strategy_declaration())
        _register(orchestrator, _validator_declaration())
        provider.responses.append(
            _response(
                [
                    {
                        "id": "call-2",
                        "name": "propose_spec",
                        "input": _propose_call(
                            "price-rule-entry-v1",
                            catalog,
                            dataset_id=dataset_id,
                            parameters=parameters,
                        ),
                    }
                ]
            )
        )
        task = orchestrator.create(
            "A sealed source that changes after confirmation",
            _declarative_selection(dataset_id),
        )
        assert task.status == "awaiting_confirmation", task.error
        confirmed = orchestrator.confirm(task.task_id, auto_run=False)
        frozen = confirmed.pinned_binding
        assert frozen is not None and confirmed.jobs == ()
        before = service.uploads.version_evidence_digest(dataset_id)

        manifest_path = service.uploads.root / "versions" / f"{dataset_id}.json"
        document = json.loads(manifest_path.read_bytes())
        document["source"]["reference"] = "a later, different export"
        manifest_path.write_bytes(
            json.dumps(
                seal_version_manifest(document),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        )
        assert service.uploads.version_evidence_digest(dataset_id) != before

        failed = orchestrator._start_backtest(confirmed.task_id)  # noqa: SLF001 — the product path
        assert failed.status == "failed"
        assert failed.error is not None and failed.error["code"] == "binding_drift"
        assert failed.pinned_binding == frozen
        assert failed.usage["backtests"] == 0
        assert failed.jobs == ()
        assert service.list_jobs() == []
    finally:
        service.stop()


def test_a_scripted_provider_timeout_fails_before_any_effect(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out provider leaves an unknown-outcome charge and no engine run.

    The selection is resolved, and both declared layers are registered, before the
    provider is reached — so the only thing this failure can be is the timeout.
    """
    root = _declarative_root(tmp_path)
    orchestrator, service, _provider = _boot(
        tmp_path,
        monkeypatch,
        root=root,
        responses=[ModelProviderError("model provider timed out after 30s")],
    )
    try:
        published = _publish(service)
        _register(orchestrator, _strategy_declaration())
        _register(orchestrator, _validator_declaration())
        task = orchestrator.create(
            "A provider that never answers", _declarative_selection(published["dataset_id"])
        )
        assert task.status == "failed"
        assert task.error is not None and task.error["code"] == "provider_error"
        assert task.usage["model_calls"] == 1
        assert task.usage["backtests"] == 0
        # The reservation settled as a spent call with an unobserved outcome: it is
        # never refunded, and the message states that nothing is re-dispatched.
        assert task.reservation is None
        assert task.spent_calls == (1,)
        assert task.last_model is not None
        assert task.last_model["outcome"] == "unknown_after_dispatch"
        assert "not re-dispatched automatically" in str(task.error["message"])
        assert task.jobs == ()
        assert service.list_jobs() == []
        # The registered declarations and the published version are untouched.
        assert {item.declaration_id for item in service.declarations.load()} == {
            "user.rule.entry",
            "user.validator.declared-availability",
        }
        assert service.uploads.has(published["dataset_id"]) is True
        assert service.uploads.version_evidence(published["dataset_id"])["source"]["reference"] == (
            SOURCE_REFERENCE
        )
    finally:
        service.stop()


# --- API surface -------------------------------------------------------------


def _api_settings(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> WebSettings:
    monkeypatch.setenv("RADIAN_SETTINGS_FILE", str(tmp_path / "settings.json"))
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>RADIAN</title>", encoding="utf-8")
    root = _declarative_root(tmp_path)
    return WebSettings(
        scenario_root=root,
        workspace=tmp_path / "workspace",
        ui_dir=ui,
        port=8765,
        data_root=_data_root(tmp_path, root),
    )


def test_upload_and_declaration_api_routes(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(_api_settings(tmp_path, monkeypatch))
    with TestClient(app) as client:
        listed = client.get("/api/datasets", headers=HOST)
        assert listed.status_code == 200
        assert {entry["origin"] for entry in listed.json()["datasets"]} == {"registered"}

        preview = client.post(
            "/api/datasets/upload/preview",
            headers=WRITE_HEADERS,
            json={"content": _capture(8).decode("utf-8"), "file_name": "bars.csv"},
        )
        assert preview.status_code == 200
        assert preview.json()["valid"] is True
        assert preview.json()["header_ok"] is True
        assert preview.json()["quality"]["bar_count"] == 8
        assert preview.json()["limitations"]

        published = client.post(
            "/api/datasets/upload",
            headers=WRITE_HEADERS,
            json={
                "content": _capture(4).decode("utf-8"),
                "file_name": "vendor-bars.csv",
                "source_reference": SOURCE_REFERENCE,
                "license_reference": LICENSE_REFERENCE,
                "availability_policy": AVAILABILITY_SIMULATED,
                "latency_seconds": SIMULATED_LATENCY_SECONDS,
            },
        )
        assert published.status_code == 201, published.text
        manifest = published.json()
        dataset_id = manifest["dataset_id"]
        assert manifest["availability_policy"]["pit_verified"] is False
        assert {entry["origin"] for entry in manifest["datasets"]} == {"registered", "upload"}

        detail = client.get(f"/api/datasets/{dataset_id}", headers=HOST)
        assert detail.status_code == 200
        assert detail.json()["origin"] == "upload"
        assert detail.json()["version_manifest"]["schema"] == "ea.local-dataset-version.v1"
        assert detail.json()["availability_policy"]["id"] == AVAILABILITY_SIMULATED

        missing_policy_latency = client.post(
            "/api/datasets/upload",
            headers=WRITE_HEADERS,
            json={
                "content": _capture(2).decode("utf-8"),
                "availability_policy": AVAILABILITY_SIMULATED,
            },
        )
        assert missing_policy_latency.status_code == 422
        assert missing_policy_latency.json()["error"]["code"] == "upload_policy"

        broken = _capture(4).replace(
            b"2026-01-05T15:30:00.000000Z,raw", b"2026-01-05T14:00:00.000000Z,raw"
        )
        rejected = client.post(
            "/api/datasets/upload",
            headers=WRITE_HEADERS,
            json={
                "content": broken.decode("utf-8"),
                "availability_policy": AVAILABILITY_PROVIDED,
            },
        )
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "invalid_bar"
        # A refused publication leaves nothing half-published behind it.
        after = client.get("/api/datasets", headers=HOST).json()["datasets"]
        assert [entry for entry in after if entry["origin"] == "upload"] == [
            entry for entry in manifest["datasets"] if entry["origin"] == "upload"
        ]

        # Raw bytes: passing json= would let the client add the very content type
        # this boundary is about, so the header is genuinely absent here.
        no_json = client.post(
            "/api/datasets/upload/preview",
            headers={k: v for k, v in WRITE_HEADERS.items() if k != "content-type"},
            content=b'{"content": "schema_version"}',
        )
        assert no_json.status_code == 415
        assert no_json.json()["error"]["code"] == "json_required"

        declarations = client.get("/api/plugins/declarations", headers=HOST)
        assert declarations.status_code == 200
        assert declarations.json()["declarations"] == []
        assert declarations.json()["supported_kinds"] == ["strategy", "validator"]
        assert "require_declared_availability" in declarations.json()["validator_constraints"]

        # A user-supplied body without a digest is sealed by the host; a wrong
        # digest is never repaired.
        body = _strategy_declaration()
        body.pop("artifact_digest")
        validated = client.post(
            "/api/plugins/declarations/validate",
            headers=WRITE_HEADERS,
            json={"declaration": body},
        )
        assert validated.status_code == 200
        assert validated.json()["usable"] is True
        assert validated.json()["registered"] is False
        assert validated.json()["declaration"]["artifact_digest"].startswith("sha256:")

        illegal = client.post(
            "/api/plugins/declarations/validate",
            headers=WRITE_HEADERS,
            json={"declaration": _strategy_declaration(rule=ILLEGAL_RULE)},
        )
        assert illegal.status_code == 200
        assert illegal.json()["usable"] is False
        assert illegal.json()["code"] == "declaration_rule"
        assert illegal.json()["registered"] is False

        registered = client.post(
            "/api/plugins/declarations",
            headers=WRITE_HEADERS,
            json={"declaration": _validator_declaration()},
        )
        assert registered.status_code == 201, registered.text
        assert registered.json()["registered"] is True

        duplicate = client.post(
            "/api/plugins/declarations",
            headers=WRITE_HEADERS,
            json={"declaration": _validator_declaration()},
        )
        assert duplicate.status_code == 422
        assert duplicate.json()["code"] == "declaration_exists"

        stored = client.get("/api/plugins/declarations", headers=HOST).json()
        assert [item["declaration_id"] for item in stored["declarations"]] == [
            "user.validator.declared-availability"
        ]
        assert stored["declarations"][0]["usable"] is True

        catalog = client.get("/api/plugins/catalog", headers=HOST).json()
        ids = {entry["plugin_id"] for entry in catalog["plugins"]}
        assert "user.validator.declared-availability" in ids
        assert f"ea.builtin.data.{dataset_id}" in ids

        recipe = client.post(
            "/api/plugins/recipe/resolve",
            headers=WRITE_HEADERS,
            json={
                "selection": {
                    "data": {
                        "plugin_id": f"ea.builtin.data.{dataset_id}",
                        "package_version": "1.0.0",
                    },
                    "validator": {
                        "plugin_id": "user.validator.declared-availability",
                        "package_version": "1.0.0",
                    },
                }
            },
        )
        assert recipe.status_code == 200
        assert recipe.json()["usable"] is True
        components = {item["layer"]: item for item in recipe.json()["binding"]["components"]}
        assert (
            components["validator"]["identity"]["validator_constraints"][
                "require_declared_availability"
            ]
            is True
        )


def test_write_bodies_are_bounded_before_any_json_parse(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound is on the bytes that arrive, not on a declared length."""
    app = create_app(_api_settings(tmp_path, monkeypatch))
    with TestClient(app) as client:
        # Not JSON at all: a 413 rather than a 422 proves the body was refused
        # while it was still arriving, before any parser could see it.
        oversized = client.post(
            "/api/datasets/upload",
            headers=WRITE_HEADERS,
            content=b"x" * (MAX_UPLOAD_REQUEST_BYTES + 1),
        )
        assert oversized.status_code == 413
        assert oversized.json()["error"]["code"] == "request_too_large"

        declarations = client.post(
            "/api/plugins/declarations",
            headers=WRITE_HEADERS,
            content=b"x" * (MAX_DECLARATION_REQUEST_BYTES + 1),
        )
        assert declarations.status_code == 413
        assert declarations.json()["error"]["code"] == "request_too_large"

        # Neither refused body left anything behind it.
        listed = client.get("/api/datasets", headers=HOST).json()["datasets"]
        assert [entry for entry in listed if entry["origin"] == "upload"] == []
        stored = client.get("/api/plugins/declarations", headers=HOST).json()
        assert stored["declarations"] == []


def test_the_declaration_store_survives_a_restart_through_the_api(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _api_settings(tmp_path, monkeypatch)
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post(
            "/api/plugins/declarations",
            headers=WRITE_HEADERS,
            json={"declaration": _strategy_declaration()},
        )
        assert created.status_code == 201
    app = create_app(settings)
    with TestClient(app) as client:
        stored = client.get("/api/plugins/declarations", headers=HOST).json()
        assert [item["declaration_id"] for item in stored["declarations"]] == ["user.rule.entry"]
        assert stored["declarations"][0]["rule"] == RULE
        # The stored bytes are the declaration's own canonical document.
        on_disk = json.loads(
            (settings.workspace / "plugin-declarations" / "user.rule.entry.json").read_bytes()
        )
        assert on_disk == decode_declaration(_payload(_strategy_declaration())).document()


def test_the_published_version_is_read_back_from_disk_not_from_memory(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _api_settings(tmp_path, monkeypatch)
    app = create_app(settings)
    with TestClient(app) as client:
        manifest = client.post(
            "/api/datasets/upload",
            headers=WRITE_HEADERS,
            json={
                "content": _capture(4).decode("utf-8"),
                "file_name": "vendor-bars.csv",
                "availability_policy": AVAILABILITY_PROVIDED,
            },
        ).json()
    app = create_app(settings)
    with TestClient(app) as client:
        detail = client.get(f"/api/datasets/{manifest['dataset_id']}", headers=HOST)
        assert detail.status_code == 200
        assert detail.json()["origin"] == "upload"
        assert detail.json()["record_count"] == 4
        assert detail.json()["availability_policy"]["id"] == (
            "ea.availability.provided-in-capture.v1"
        )
        assert detail.json()["availability_policy"]["pit_verified"] is False
        assert detail.json()["availability_policy"]["evidence"] == "asserted-by-uploader"


def test_an_unpublished_upload_identifier_is_not_a_dataset(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(_api_settings(tmp_path, monkeypatch))
    with TestClient(app) as client:
        unknown = client.get("/api/datasets/0123456789abcdef0123456789abcdef.csv", headers=HOST)
        assert unknown.status_code == 422
        assert unknown.json()["error"]["code"] == "dataset_invalid"
        traversal = client.get("/api/datasets/..%2Fprices.csv", headers=HOST)
        assert traversal.status_code in {404, 422}


def test_a_staged_file_never_becomes_visible_without_a_published_version(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a version manifest makes an upload addressable; staging is swept."""
    root = _declarative_root(tmp_path)
    _orchestrator, service, _provider = _boot(tmp_path, monkeypatch, root=root)
    try:
        published = _publish(service)
        dataset_id = published["dataset_id"]
        staging = service.uploads.root / "staging"
        # One file this store would have generated and abandoned, and one that is
        # somebody else's: only the first of the two may ever be removed.
        (staging / STRAY_NAME).write_bytes(_capture(2))
        (staging / "someone-elses-notes.txt").write_text("not mine", encoding="utf-8")
        assert service.uploads.has(STRAY_NAME) is False
        service.stop()
        reopened = WebService(root, tmp_path / "workspace")
        reopened.start()
        try:
            assert list(reopened.uploads.root.glob("staging/*.csv")) == []
            assert (staging / "someone-elses-notes.txt").read_text(encoding="utf-8") == "not mine"
            assert reopened.uploads.has(dataset_id) is True
            assert [entry["dataset_id"] for entry in reopened.dataset_catalog.list()] == [
                dataset_id
            ]
        finally:
            reopened.stop()
    finally:
        service.stop()


def test_an_upload_is_rejected_when_its_identifier_is_unknown(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable upload id is a refusal, never a fallback to another input."""
    root = _declarative_root(tmp_path)
    _orchestrator, service, _provider = _boot(tmp_path, monkeypatch, root=root)
    try:
        with pytest.raises(ValueError):
            service.uploads.load("missing.csv")
        with pytest.raises(ValueError):
            service.uploads.inspect("missing.csv")
        # An empty body is a preview verdict, not an exception, and a preview is
        # never a publication.
        empty = service.uploads.preview(b"")
        assert empty["valid"] is False
        assert empty["first_error"]["code"] == "empty_source"
        assert service.dataset_catalog.list() == []
    finally:
        service.stop()


def test_a_capture_that_no_longer_matches_its_version_is_refused(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _declarative_root(tmp_path)
    _orchestrator, service, _provider = _boot(tmp_path, monkeypatch, root=root)
    try:
        published = _publish(service)
        dataset_id = published["dataset_id"]
        capture = service.uploads.root / "captures" / dataset_id
        capture.write_bytes(_capture(5))
        with pytest.raises(ValueError, match="no longer matches"):
            service.uploads.load(dataset_id)
        entries = {entry["dataset_id"]: entry for entry in service.dataset_catalog.list()}
        assert entries[dataset_id]["valid"] is False
        assert entries[dataset_id]["error_code"] == "invalid_or_unavailable_input"
    finally:
        service.stop()


def test_an_upload_row_that_would_be_silently_corrected_is_refused(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host drops, fills and reshapes nothing; a defect is a refusal."""
    root = _declarative_root(tmp_path)
    _orchestrator, service, _provider = _boot(tmp_path, monkeypatch, root=root)
    try:
        # A trailing value the host could have filled in, and an interval_end the
        # host could have bumped up to its own available_at.
        short_row = f"{HEADER}\n{_line(0).rsplit(',', 1)[0]}\n".encode()
        moved_end = _capture(2).replace(
            b"2026-01-05T15:30:00.000000Z", b"2026-01-05T14:00:00.000000Z"
        )
        for broken in (short_row, moved_end):
            with pytest.raises(ValueError) as refused:
                service.uploads.publish(broken, availability_policy=AVAILABILITY_PROVIDED)
            assert getattr(refused.value, "code", "") in STRICT_REFUSALS
        # A refused upload is never partially published.
        assert service.uploads.list() == []
        assert list(service.uploads.root.glob("raw/*")) == []
        assert list(service.uploads.root.glob("captures/*")) == []
        assert list(service.uploads.root.glob("versions/*.json")) == []
    finally:
        service.stop()
