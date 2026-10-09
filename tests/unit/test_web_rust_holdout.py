"""Chronological holdout over a run the Rust core owns.

The source is a real user-strategy experiment the core confirmed, ran and
registered; the holdout is a second, independent core-owned run of a strictly
later registered scenario. Everything here is the real product owner — the
compiled core, the real engine, the real Web service — and the assertions are
made over the HTTP contract and the owner's own retained bytes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from ea.web.app import WebSettings, create_app
from ea.web.quine import (
    QuineBridge,
    QuineClient,
    QuineHoldouts,
    QuineJobReader,
    QuineUnavailable,
)
from ea.web.research import ResearchOrchestrator, ResearchStore
from ea.web.service import WebService
from ea.web.user_strategies import UserStrategyService, UserStrategyStore
from unit.test_web_api import ORIGIN, WRITE_HEADERS
from unit.test_web_holdout import _target
from unit.test_web_research import _scenario_root
from unit.test_web_rust_experiments import (
    _STARTED,
    RUN_BUDGET,
    SCENARIO,
    SHORT_WORKSPACES,
    Core,
    _await_http_row,
    _create_via_http,
    _no_python_owner,
    _row,
    _settings,
    _wait,
    _web_settings,
)

pytestmark = pytest.mark.filterwarnings(
    "ignore:The anyio.abc.BlockingPortal alias is deprecated:DeprecationWarning"
)


@pytest.fixture(autouse=True)
def _stop_started() -> Iterator[None]:
    """Stop the owners this module starts and remove its short socket roots."""
    yield
    import shutil

    while _STARTED:
        _STARTED.pop().stop()
    while SHORT_WORKSPACES:
        shutil.rmtree(SHORT_WORKSPACES.pop(), ignore_errors=True)


def _commissioned(root: Path) -> None:
    """One scenario whose execution really charges a commission."""
    import yaml

    document = yaml.safe_load((root / SCENARIO).read_text())
    document["execution"]["commission"] = dict(
        policy="deterministic-commission-v1", commission_bps="100"
    )
    (root / SCENARIO).write_text(yaml.safe_dump(document))


def _source_job(client: TestClient, identity: str) -> str:
    """One confirmed user-strategy experiment, settled and named by its job."""
    preview = client.get(f"/api/user-strategies/{identity}/experiment/preview").json()
    launched = client.post(
        f"/api/user-strategies/{identity}/experiments",
        headers=WRITE_HEADERS,
        json={
            "launch_intent_id": "li-1",
            "base_revision": preview["draft_revision"],
            "preview_identity": preview["preview_identity"],
            "approved_budget": dict(RUN_BUDGET),
        },
    )
    assert launched.status_code == 200, launched.text
    row = _await_http_row(client, identity)
    assert row["status"] == "created", row["message"]
    return cast(str, row["job_id"])


def _await_job(client: TestClient, job_id: str) -> dict[str, Any]:
    """Wait for one core-owned run to leave the states it can still leave."""
    import time

    deadline = time.monotonic() + 180
    document: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/backtests/{job_id}")
        assert response.status_code == 200, response.text
        document = cast(dict[str, Any], response.json())
        if document["status"] not in {"accepted", "running", "stopping"}:
            return document
        time.sleep(0.2)
    raise AssertionError(f"run {job_id} never settled: {document}")


def _publish(settings: WebSettings, rows: int) -> tuple[str, bytes]:
    """Publish one synthetic capture into a Web workspace, before it is served."""
    from ea.web.uploads import AVAILABILITY_PROVIDED, ControlledUploadStoreV1
    from unit.test_web_uploads import _capture

    settings.workspace.mkdir(parents=True, exist_ok=True)
    content = _capture(rows)
    store = ControlledUploadStoreV1(settings.workspace, sweep=False)
    try:
        manifest = store.publish(content, availability_policy=AVAILABILITY_PROVIDED)
    finally:
        store.close()
    return str(manifest["dataset_id"]), content


def test_a_core_owned_success_offers_and_runs_a_later_registered_holdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real second core-owned run exists, and both formal reports are readable."""
    core = Core(tmp_path)
    _STARTED.append(core)
    settings = _web_settings(tmp_path, core)
    _commissioned(settings.scenario_root)
    _target(settings.scenario_root)
    counts = _no_python_owner(monkeypatch)
    app = create_app(settings)
    with TestClient(app, base_url=ORIGIN) as client:
        identity = _create_via_http(client)
        source = _source_job(client, identity)
        bridge = QuineBridge(core.socket)
        assert len(bridge.experiments()) == 1

        candidates = client.get(f"/api/backtests/{source}/holdout-scenarios")
        assert candidates.status_code == 200, candidates.text
        offered = [item["scenario_id"] for item in candidates.json()["scenarios"]]
        assert offered == ["later.yaml"]

        created = client.post(
            "/api/holdouts",
            headers=WRITE_HEADERS,
            json={"source_job_id": source, "scenario_id": "later.yaml"},
        )
        assert created.status_code == 202, created.text
        relation = cast(dict[str, Any], created.json())
        assert set(relation) == {
            "schema",
            "validation_id",
            "created_at",
            "source_job_id",
            "holdout_job_id",
        }
        assert relation["schema"] == "ea.chronological-holdout.v1"
        assert relation["source_job_id"] == source
        assert relation["holdout_job_id"] != source

        # The holdout really runs, as an independent run the owner registered.
        document = _await_job(client, relation["holdout_job_id"])
        assert document["status"] == "succeeded", document["message"]
        assert document["report_ready"] is True
        after = bridge.experiments()
        assert len(after) == 2
        child = next(item for item in after if item["job_id"] == relation["holdout_job_id"])
        assert child["status"] == "succeeded"
        assert bridge.registered_sha256(child, "report.json")
        assert counts == {"children": 0, "owners": 0}

        holdout = client.get(f"/api/backtests/{relation['holdout_job_id']}")
        assert holdout.status_code == 200, holdout.text
        assert holdout.json() == document

        source_job = client.get(f"/api/backtests/{source}").json()
        frozen_source = source_job["input_snapshot"]["scenario"]
        frozen_target = document["input_snapshot"]["scenario"]
        # Exact source parameters, funding, cost, risk, instrument and strategy
        # implementation are frozen from the owner's record; only the window, the
        # selected data and the consequent scenario identity differ.
        for block in ("strategy", "funding", "risk", "execution", "instrument"):
            assert frozen_target[block] == frozen_source[block], block
        assert frozen_target["data"]["start_utc"] > frozen_source["data"]["end_utc"]

        reports = [
            client.get(f"/api/backtests/{job}/report").json()
            for job in (source, relation["holdout_job_id"])
        ]
        assert all(item["economics"]["fees"]["amount"] != "0" for item in reports)
        assert reports[0]["run_id"] != reports[1]["run_id"]

        assert client.get("/api/holdouts").json()["holdouts"] == [relation]
        assert client.get(f"/api/holdouts/{relation['validation_id']}").json() == relation
        # Nothing of either run was copied into this workspace's own index.
        assert client.get("/api/backtests").json()["jobs"] == []


# --- the relationship outlives the process image that admitted it -------------


def test_the_relationship_and_both_reports_outlive_a_service_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = Core(tmp_path)
    _STARTED.append(core)
    settings = _web_settings(tmp_path, core)
    _commissioned(settings.scenario_root)
    _target(settings.scenario_root)
    _no_python_owner(monkeypatch)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        identity = _create_via_http(client)
        source = _source_job(client, identity)
        created = client.post(
            "/api/holdouts",
            headers=WRITE_HEADERS,
            json={"source_job_id": source, "scenario_id": "later.yaml"},
        )
        assert created.status_code == 202, created.text
        relation = cast(dict[str, Any], created.json())
        holdout = _await_job(client, relation["holdout_job_id"])

    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        assert client.get(f"/api/holdouts/{relation['validation_id']}").json() == relation
        assert client.get("/api/holdouts").json()["holdouts"] == [relation]
        reread = client.get(f"/api/backtests/{relation['holdout_job_id']}").json()
        assert reread == holdout
        assert reread["report_ready"] is True
        assert client.get(f"/api/backtests/{source}").json()["report_ready"] is True
        # The owner was never asked to run anything again: two runs, still.
        assert len(QuineBridge(core.socket).experiments()) == 2


def test_the_relationship_outlives_a_core_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = Core(tmp_path)
    _STARTED.append(core)
    settings = _web_settings(tmp_path, core)
    _commissioned(settings.scenario_root)
    _target(settings.scenario_root)
    _no_python_owner(monkeypatch)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        identity = _create_via_http(client)
        source = _source_job(client, identity)
        relation = cast(
            dict[str, Any],
            client.post(
                "/api/holdouts",
                headers=WRITE_HEADERS,
                json={"source_job_id": source, "scenario_id": "later.yaml"},
            ).json(),
        )
        _await_job(client, relation["holdout_job_id"])

    # The same owner comes back from the same workspace: the admission is its own
    # durable record, so the relationship is rebuilt from it and nothing reruns.
    core.stop()
    restarted = Core(tmp_path, workspace=core.workspace)
    _STARTED.append(restarted)
    with TestClient(create_app(_web_settings(tmp_path, restarted)), base_url=ORIGIN) as client:
        assert client.get("/api/holdouts").json()["holdouts"] == [relation]
        assert (
            client.get(f"/api/backtests/{relation['holdout_job_id']}").json()["report_ready"]
            is True
        )
        assert len(QuineBridge(restarted.socket).experiments()) == 2


# --- one explicitly selected later dataset -----------------------------------


def test_a_core_owned_success_holds_out_into_a_later_uploaded_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The uploaded target is a released version the confirmation binds and freezes."""
    from ea.web import rust_domain

    core = Core(tmp_path)
    _STARTED.append(core)
    settings = _web_settings(tmp_path, core)
    _commissioned(settings.scenario_root)
    _target(settings.scenario_root)
    counts = _no_python_owner(monkeypatch)
    dataset_id, content = _publish(settings, 8)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        identity = _create_via_http(client)
        source = _source_job(client, identity)

        offered = client.get(f"/api/backtests/{source}/holdout-scenarios").json()["scenarios"]
        named = [item for item in offered if item["input_identity"].get("dataset_id") == dataset_id]
        assert len(named) == 1
        created = client.post(
            "/api/holdouts",
            headers=WRITE_HEADERS,
            json={
                "source_job_id": source,
                "scenario_id": named[0]["scenario_id"],
                "dataset_id": dataset_id,
                "source_sha256": named[0]["input_identity"]["source_sha256"],
            },
        )
        assert created.status_code == 202, created.text
        relation = cast(dict[str, Any], created.json())
        document = _await_job(client, relation["holdout_job_id"])
        assert document["status"] == "succeeded", document["message"]
        assert document["input_snapshot"]["research_input"]["dataset_id"] == dataset_id
        source_job = client.get(f"/api/backtests/{source}").json()
        # The source selected no dataset, so nothing stale could be carried across;
        # everything but the data binding is the source's own frozen configuration.
        for block in ("strategy", "funding", "risk", "execution", "instrument"):
            assert (
                document["input_snapshot"]["scenario"][block]
                == source_job["input_snapshot"]["scenario"][block]
            ), block
        assert document["plugin_binding"]["components"][0]["layer"] == "data"
        assert counts == {"children": 0, "owners": 0}
        assert content

        # The target's own published version is what the confirmation froze, and a
        # version edited afterwards is refused rather than run from.
        bridge = QuineBridge(core.socket)
        experiment = bridge.job_experiment(relation["holdout_job_id"])
        assert experiment is not None
        envelope = bridge.frozen_document(experiment)
        assert envelope is not None
        assert envelope["dataset_version"]["dataset_id"] == dataset_id
        version = settings.workspace / "uploads" / "versions" / f"{dataset_id}.json"
        changed = json.loads(version.read_bytes())
        changed["source"]["reference"] = "changed after the confirmation"
        version.write_text(json.dumps(changed))
        frozen = rust_domain.run(
            "freeze", {"frozen": envelope, "capture_path": str(tmp_path / "drifted.data")}
        )
        assert frozen["status"] == "refused", frozen
        assert not (tmp_path / "drifted.data").exists()


def test_a_holdout_never_needs_the_source_run_s_own_data_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source selected from an upload is continued after that upload is gone."""
    import shutil

    core = Core(tmp_path)
    _STARTED.append(core)
    settings = _web_settings(tmp_path, core)
    _commissioned(settings.scenario_root)
    # The later scenario replays a window after the uploaded source's own window.
    _target(settings.scenario_root, day="06")
    _no_python_owner(monkeypatch)
    dataset_id, _content = _publish(settings, 8)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        identity = _create_via_http(client)
        selected = client.post(
            f"/api/user-strategies/{identity}/select-dataset",
            headers=WRITE_HEADERS,
            json={"operation_id": "select-1", "base_revision": 1, "dataset_id": dataset_id},
        )
        assert selected.status_code == 200, selected.text
        source = _source_job(client, identity)
        source_job = client.get(f"/api/backtests/{source}").json()
        assert source_job["input_snapshot"]["research_input"]["dataset_id"] == dataset_id

        # The source's own data is removed whole. Nothing in this holdout may need
        # it: the source's evidence travels inside the owner's retained envelope.
        shutil.move(str(settings.workspace / "uploads"), str(tmp_path / "uploads-gone"))

        created = client.post(
            "/api/holdouts",
            headers=WRITE_HEADERS,
            json={"source_job_id": source, "scenario_id": "later.yaml"},
        )
        assert created.status_code == 202, created.text
        relation = cast(dict[str, Any], created.json())
        document = _await_job(client, relation["holdout_job_id"])
        assert document["status"] == "succeeded", document["message"]
        # The target is the registered scenario's own data, so the source's dataset
        # binding is not carried across: no research evidence and no data capability.
        assert "research_input" not in document["input_snapshot"]
        assert "plugin_binding" not in document
        assert (
            document["input_snapshot"]["scenario"]["strategy"]
            == source_job["input_snapshot"]["scenario"]["strategy"]
        )
        assert client.get("/api/holdouts").json()["holdouts"] == [relation]


# --- every refusal happens before anything exists ----------------------------


@pytest.mark.parametrize(
    ("day", "start", "end"),
    [
        ("02", "09:31", "09:34"),
        ("02", "09:32", "09:34"),
        ("02", "09:30", "09:33"),
        ("01", "09:31", "09:34"),
    ],
)
def test_a_nonlater_or_overlapping_target_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, day: str, start: str, end: str
) -> None:
    core = Core(tmp_path)
    _STARTED.append(core)
    settings = _web_settings(tmp_path, core)
    _commissioned(settings.scenario_root)
    _target(settings.scenario_root, day=day, start=start, end=end)
    _no_python_owner(monkeypatch)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        identity = _create_via_http(client)
        source = _source_job(client, identity)
        bridge = QuineBridge(core.socket)
        before = bridge.experiments()
        refused = client.post(
            "/api/holdouts",
            headers=WRITE_HEADERS,
            json={"source_job_id": source, "scenario_id": "later.yaml"},
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "holdout_invalid"
        assert "strictly later" in refused.json()["error"]["message"]
        assert client.get("/api/holdouts").json()["holdouts"] == []
        # The owner was never asked to admit anything.
        assert bridge.experiments() == before


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("instrument", "symbol", "MSFT"),
        ("instrument", "settlement_currency", "EUR"),
        ("funding", "initial_cash", "9999"),
        ("risk", "max_order_quantity", "4"),
        ("execution", "commission", dict(policy="other")),
        ("", "randomness_profile", "other"),
    ],
)
def test_an_incompatible_target_configuration_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, section: str, key: str, value: object
) -> None:
    import yaml

    core = Core(tmp_path)
    _STARTED.append(core)
    settings = _web_settings(tmp_path, core)
    _commissioned(settings.scenario_root)
    _target(settings.scenario_root)
    document = yaml.safe_load((settings.scenario_root / "later.yaml").read_text())
    (document[section] if section else document)[key] = value
    (settings.scenario_root / "later.yaml").write_text(yaml.safe_dump(document))
    _no_python_owner(monkeypatch)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        identity = _create_via_http(client)
        source = _source_job(client, identity)
        bridge = QuineBridge(core.socket)
        before = bridge.experiments()
        refused = client.post(
            "/api/holdouts",
            headers=WRITE_HEADERS,
            json={"source_job_id": source, "scenario_id": "later.yaml"},
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "holdout_invalid"
        assert client.get("/api/holdouts").json()["holdouts"] == []
        assert bridge.experiments() == before


def test_a_target_that_cannot_meet_the_frozen_dynamic_bound_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frozen delay is revalidated against the later window, never clamped."""
    import yaml

    core = Core(tmp_path)
    _STARTED.append(core)
    settings = _web_settings(tmp_path, core)
    _commissioned(settings.scenario_root)
    document = yaml.safe_load((settings.scenario_root / SCENARIO).read_text())
    document["strategy"]["entry_delay_bars"] = 2
    (settings.scenario_root / SCENARIO).write_text(yaml.safe_dump(document))
    # Two bars of later data cannot support a frozen delay of two.
    _target(settings.scenario_root, end="09:33")
    _no_python_owner(monkeypatch)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        identity = _create_via_http(client)
        source = _source_job(client, identity)
        bridge = QuineBridge(core.socket)
        before = bridge.experiments()
        refused = client.post(
            "/api/holdouts",
            headers=WRITE_HEADERS,
            json={"source_job_id": source, "scenario_id": "later.yaml"},
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "holdout_invalid"
        assert bridge.experiments() == before


def test_a_source_without_a_verified_report_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tampered official report is not a source a holdout may be built on."""
    core = Core(tmp_path)
    _STARTED.append(core)
    settings = _web_settings(tmp_path, core)
    _commissioned(settings.scenario_root)
    _target(settings.scenario_root)
    _no_python_owner(monkeypatch)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        identity = _create_via_http(client)
        source = _source_job(client, identity)
        bridge = QuineBridge(core.socket)
        before = bridge.experiments()
        official = core.workspace / "official" / source / "report.json"
        official.write_text("{}")

        candidates = client.get(f"/api/backtests/{source}/holdout-scenarios")
        assert candidates.status_code == 422, candidates.text
        assert candidates.json()["error"]["code"] == "holdout_invalid"
        refused = client.post(
            "/api/holdouts",
            headers=WRITE_HEADERS,
            json={"source_job_id": source, "scenario_id": "later.yaml"},
        )
        assert refused.status_code == 422, refused.text
        assert client.get("/api/holdouts").json()["holdouts"] == []
        assert bridge.experiments() == before


# --- the owner that cannot answer --------------------------------------------


class LosingConfirmClient(QuineClient):
    """Sends one confirmation for real, then loses that one reply."""

    def __init__(self, *args: Any, drop_at: int = 1, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.confirms = 0
        self.drop_at = drop_at

    def _exchange(self, line: bytes, wait: float) -> bytes:
        import socket as socket_module
        import time

        if b'"confirm"' in line:
            self.confirms += 1
            if self.confirms == self.drop_at:
                stream = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
                try:
                    stream.settimeout(wait)
                    stream.connect(str(self.socket_path))
                    stream.sendall(line)
                finally:
                    # The core writes its reply into a socket nobody reads.
                    time.sleep(0.5)
                    stream.close()
                raise QuineUnavailable("the reply was lost")
        return super()._exchange(line, wait)


def _owner(
    tmp_path: Path, core: Core, *, client: QuineClient | None = None
) -> tuple[Any, Any, QuineBridge, Any]:
    """One real service and its owner, optionally with a broken IPC client."""
    workspace = tmp_path / "workspace"
    bridge = QuineBridge(core.socket, confirm_timeout=60.0, query_timeout=20.0, client=client)
    service = WebService(_scenario_root(tmp_path), workspace, external=QuineJobReader(bridge))
    service.start()
    _STARTED.append(service)
    research = ResearchOrchestrator(ResearchStore(workspace), service, _settings(tmp_path))
    strategies = UserStrategyService(
        UserStrategyStore(workspace),
        research,
        service,
        _settings(tmp_path),
        quine=bridge,
    )
    service.attach_holdout_owner(QuineHoldouts(bridge, service, research))
    return service, strategies, bridge, research


def _owner_source(strategies: Any, *, intent: str = "li-1") -> tuple[str, str]:
    """One settled core-owned source run: its strategy identity and its job."""
    strategy = strategies.create(
        name="Long AAPL",
        intent="One registered entry on the fixture data",
        scenario_id=SCENARIO,
        criterion={"field": "economics.net_pnl.amount", "expectation": "positive"},
    )
    identity = strategy.user_strategy_id
    preview = strategies.preview_experiment(identity)
    launched = strategies.launch_experiment(
        identity,
        launch_intent_id=intent,
        base_revision=preview["draft_revision"],
        preview_identity=preview["preview_identity"],
        approved_budget=dict(RUN_BUDGET),
    )
    assert _row(launched, intent)["status"] in {"confirmed", "running", "created"}
    row = _wait(strategies, identity)
    assert row["status"] == "created", row["message"]
    return identity, cast(str, row["job_id"])


def test_a_lost_holdout_reply_is_queried_and_never_confirmed_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = Core(tmp_path)
    _STARTED.append(core)
    client = LosingConfirmClient(core.socket, timeout=60.0, drop_at=2)
    service, strategies, bridge, _research = _owner(tmp_path, core, client=client)
    _commissioned(_scenario_root(tmp_path))
    _target(_scenario_root(tmp_path))
    counts = _no_python_owner(monkeypatch)
    _identity, source = _owner_source(strategies)
    assert client.confirms == 1

    # The holdout's own confirmation really happened and its reply was lost. What
    # comes back is the owner's record, found by querying that one intent — never a
    # second confirmation.
    relation = cast(
        dict[str, str], service.create_holdout(source_job_id=source, scenario_id="later.yaml")
    )
    assert client.confirms == 2
    assert len(bridge.experiments()) == 2
    assert service.get_holdout(relation["validation_id"]) == relation
    assert service.list_holdouts() == [relation]
    assert client.confirms == 2
    assert counts == {"children": 0, "owners": 0}
    # The admitted run really settles, as the owner's own success.
    experiment = bridge.job_experiment(relation["holdout_job_id"])
    assert experiment is not None
    assert experiment["status"] in {"eligible", "dispatched", "running", "succeeded"}


def test_a_lost_confirmation_and_query_remains_unknown_without_resending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class LosingBothClient(LosingConfirmClient):
        def _exchange(self, line: bytes, wait: float) -> bytes:
            if self.confirms >= self.drop_at and b'"query"' in line:
                raise QuineUnavailable("the recovery query was also lost")
            return super()._exchange(line, wait)

    core = Core(tmp_path)
    _STARTED.append(core)
    client = LosingBothClient(core.socket, timeout=60.0, drop_at=2)
    service, strategies, _bridge, _research = _owner(tmp_path, core, client=client)
    _commissioned(_scenario_root(tmp_path))
    _target(_scenario_root(tmp_path))
    counts = _no_python_owner(monkeypatch)
    _identity, source = _owner_source(strategies)
    with pytest.raises(QuineUnavailable, match="may already exist"):
        service.create_holdout(source_job_id=source, scenario_id="later.yaml")
    assert client.confirms == 2
    # The confirmation really reached Rust: its relation is recovered by a read,
    # even though neither the confirmation reply nor the recovery query arrived.
    assert len(QuineBridge(core.socket).experiments()) == 2
    relations = service.list_holdouts()
    assert len(relations) == 1
    assert relations[0]["source_job_id"] == source
    assert client.confirms == 2
    assert counts == {"children": 0, "owners": 0}


def test_an_unreachable_owner_fails_closed_and_caches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = Core(tmp_path)
    _STARTED.append(core)
    settings = _web_settings(tmp_path, core)
    _commissioned(settings.scenario_root)
    _target(settings.scenario_root)
    _no_python_owner(monkeypatch)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        identity = _create_via_http(client)
        source = _source_job(client, identity)
        created = client.post(
            "/api/holdouts",
            headers=WRITE_HEADERS,
            json={"source_job_id": source, "scenario_id": "later.yaml"},
        )
        assert created.status_code == 202, created.text
        relation = cast(dict[str, Any], created.json())
        _await_job(client, relation["holdout_job_id"])

        core.kill()
        listed = client.get("/api/holdouts")
        assert listed.status_code == 503, listed.text
        assert listed.json()["error"]["code"] == "quine_unavailable"
        read = client.get(f"/api/holdouts/{relation['validation_id']}")
        assert read.status_code == 503, read.text
        refused = client.post(
            "/api/holdouts",
            headers=WRITE_HEADERS,
            json={"source_job_id": source, "scenario_id": "later.yaml"},
        )
        assert refused.status_code == 503, refused.text
        assert client.get(f"/api/backtests/{source}/holdout-scenarios").status_code == 503, (
            "an unreadable owner is never answered from a cached claim"
        )


def test_a_busy_owner_slot_refuses_the_holdout_before_it_is_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's own single-slot refusal is surfaced; nothing is admitted here."""
    core = Core(tmp_path, args=("--test-hold-after-spawn",))
    _STARTED.append(core)
    service, strategies, bridge, _research = _owner(tmp_path, core)
    _commissioned(_scenario_root(tmp_path))
    _target(_scenario_root(tmp_path))
    _no_python_owner(monkeypatch)
    # The first launch is released, so it really succeeds and names a usable source.
    (core.workspace / "hold-release").write_text("go")
    identity, source = _owner_source(strategies)

    # The next launch is held open, so the owner's single slot is occupied.
    (core.workspace / "hold-release").unlink()
    preview = strategies.preview_experiment(identity)
    held = strategies.launch_experiment(
        identity,
        launch_intent_id="li-2",
        base_revision=preview["draft_revision"],
        preview_identity=preview["preview_identity"],
        approved_budget=dict(RUN_BUDGET),
    )
    assert _row(held, "li-2")["status"] in {"confirmed", "running"}

    from ea.web.quine import QuineRefused

    before = bridge.experiments()
    with pytest.raises(QuineRefused) as refused:
        service.create_holdout(source_job_id=source, scenario_id="later.yaml")
    assert refused.value.code == "eligibility_held"
    assert bridge.experiments() == before
    assert service.list_holdouts() == []
    (core.workspace / "hold-release").write_text("go")


def test_a_source_run_the_workspace_does_not_own_keeps_the_written_refusal(
    tmp_path: Path,
) -> None:
    """Only the configured owner is routed; every other owner is still refused."""
    from dataclasses import replace

    from ea.web.service import WebBoundaryError, WebService
    from unit.test_web_api import _settings as api_settings
    from unit.test_web_holdout import _source

    settings = api_settings(tmp_path)
    _commissioned(settings.scenario_root)
    _target(settings.scenario_root)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        source = _source(client)
    service = WebService(settings.scenario_root, settings.workspace)
    service.start()
    try:
        record = service.get_job(source["job_id"])
        service._store(replace(record, owner="another-owner"))
        with pytest.raises(WebBoundaryError, match="not supported"):
            service.create_holdout(source_job_id=record.job_id, scenario_id="later.yaml")
        with pytest.raises(WebBoundaryError, match="not supported"):
            service.holdout_candidates(record.job_id)
    finally:
        service.stop()


# --- a frozen local strategy artifact ----------------------------------------


def test_a_local_package_source_keeps_its_artifact_bytes_and_descriptor(
    tmp_path: Path,
) -> None:
    """A V3 artifact source is carried into the holdout, never re-read.

    The source envelope here is a real one: the artifact container is the same
    fixture the local-package integration tests use, the scenario names it by
    exact package identity, and the target is a strictly later scenario naming the
    same artifact. What the holdout carries must be the *source's* frozen bytes —
    not a second read of the strategy root, which could have changed — and the
    whole envelope must still re-verify as a frozen input in its own right.
    """
    import yaml

    from ea.strategy.package import inspect_package
    from ea.web import rust_domain
    from unit.test_web_local_package_integration import _package

    sentinel = tmp_path / "package-was-executed.txt"
    artifact = _package(sentinel)
    package = inspect_package(artifact)
    strategy_root = tmp_path / "strategies"
    strategy_root.mkdir()
    (strategy_root / "local-probe.eastrategy").write_bytes(artifact)

    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    source_csv = scenarios / "prices.csv"
    source_csv.write_bytes(_priced_fixture())
    later_csv = scenarios / "later.csv"
    later_csv.write_bytes(source_csv.read_bytes().replace(b"2026-01-02", b"2026-01-06"))

    def _write(name: str, csv: Path, start: str, end: str) -> Path:
        fingerprint = _csv_fingerprint(csv.read_bytes(), start, end)
        document = {
            "schema_version": 3,
            "data": {
                "path": csv.name,
                "start_utc": start,
                "end_utc": end,
                "fingerprint": fingerprint,
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
            "strategy": {
                "id": package.descriptor.strategy_id,
                "version": package.descriptor.strategy_version,
                "parameters": {"target_quantity": "2"},
                "source": {
                    "kind": "local-package",
                    "package_id": package.identity.package_id,
                    "artifact_sha256": package.identity.artifact_sha256,
                },
            },
            "funding": {"currency": "USD", "initial_cash": "10000"},
            "risk": {
                "max_order_quantity": "5",
                "max_position_quantity": "5",
                "max_notional": "1000",
            },
            "execution": {"policy": "phase1.next-bar-close.v1"},
            "randomness_profile": "none",
        }
        path = scenarios / name
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return path

    source = _write(
        "source.yaml", source_csv, "2026-01-02T09:31:00.000000Z", "2026-01-02T09:34:00.000000Z"
    )
    later = _write(
        "later.yaml", later_csv, "2026-01-06T09:31:00.000000Z", "2026-01-06T09:34:00.000000Z"
    )

    snapshot = rust_domain.run(
        "snapshot", {"scenario_path": str(source), "strategy_root": str(strategy_root)}
    )
    assert snapshot["status"] == "ok", snapshot
    frozen_source = snapshot["result"]["frozen"]
    assert frozen_source["package_file"] is not None

    rebound = rust_domain.run(
        "rebind",
        {
            "source": frozen_source,
            "scenario_id": "later.yaml",
            "scenario_path": str(later),
            "strategy_root": str(strategy_root),
        },
    )
    assert rebound["status"] == "ok", rebound
    target = rebound["result"]["frozen"]
    # The artifact bytes and digest are the source's own, and the descriptor that
    # names them travelled with the source scenario rather than being re-derived.
    assert target["package_file"] == frozen_source["package_file"]
    assert target["package_sha256"] == frozen_source["package_sha256"]
    assert (
        target["snapshot"]["scenario"]["strategy"]["source"]
        == frozen_source["snapshot"]["scenario"]["strategy"]["source"]
    )
    assert (
        target["snapshot"]["scenario"]["strategy"]["parameters"]
        == frozen_source["snapshot"]["scenario"]["strategy"]["parameters"]
    )
    assert (
        target["snapshot"]["scenario"]["data"]["start_utc"]
        > frozen_source["snapshot"]["scenario"]["data"]["end_utc"]
    )

    # The new envelope is itself a re-verifiable frozen input: materializing it
    # re-derives the scenario from those exact bytes and still finds this artifact.
    capture = tmp_path / "holdout.data"
    verified = rust_domain.run("freeze", {"frozen": target, "capture_path": str(capture)})
    assert verified["status"] == "ok", verified
    assert verified["result"]["package_sha256"] == frozen_source["package_sha256"]
    assert capture.is_file()


def _csv_fingerprint(payload: bytes, start: str, end: str) -> dict[str, Any]:
    """The real decoder's own fingerprint for one window of these exact bytes."""
    from datetime import datetime

    from ea.core import ReplayWindow
    from ea.data import decode_phase1_ohlcv_csv

    selection = decode_phase1_ohlcv_csv(
        payload,
        replay_window=ReplayWindow(datetime.fromisoformat(start), datetime.fromisoformat(end)),
    ).selection
    return {
        "sha256": selection.fingerprint.sha256.value,
        "record_count": selection.fingerprint.record_count,
    }


def _priced_fixture() -> bytes:
    """The repository's own demo capture, with the extra executable bar appended."""
    source = Path(__file__).resolve().parents[2] / "src" / "ea" / "product"
    payload = (source / "phase1_demo_ohlcv_v1.csv").read_bytes()
    return payload + (
        b"1,XNAS,AAPL,2026-01-02T09:32:00.000000Z,2026-01-02T09:33:00.000000Z,"
        b"raw,108.0,111.0,107.0,110.0,9.0,fixture.raw,3,0,2026-01-02T09:33:00.000000Z\n"
    )
