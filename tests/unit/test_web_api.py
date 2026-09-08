from __future__ import annotations

import hashlib
import json
import shutil
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest
import yaml
from fastapi.testclient import TestClient

import ea.web.service as web_service
from ea.core import RunId
from ea.product import LoadedBacktestScenario
from ea.product import run_backtest_scenario as run_product_backtest
from ea.web.app import WebSettings, create_app
from ea.web.service import WebBoundaryError, WebService
from unit.test_backtest_report import _priced_scenario

ORIGIN = "http://127.0.0.1:8765"
WRITE_HEADERS = {
    "origin": ORIGIN,
    "x-ea-web-request": "1",
    "content-type": "application/json",
}
pytestmark = pytest.mark.filterwarnings(
    "ignore:The anyio.abc.BlockingPortal alias is deprecated:DeprecationWarning"
)


def _scenario_root(tmp_path: Path) -> Path:
    root = tmp_path / "scenarios"
    source = _priced_scenario(root)
    source.rename(root / "bounded-long.yaml")
    document = yaml.safe_load((root / "bounded-long.yaml").read_text(encoding="utf-8"))

    flat = dict(document)
    flat["strategy"] = {"id": "always-flat-v1"}
    (root / "flat.yaml").write_text(yaml.safe_dump(flat, sort_keys=False), encoding="utf-8")

    one = dict(document)
    one["strategy"] = {"id": "bounded-long-v1", "target_quantity": "1"}
    (root / "one.yaml").write_text(yaml.safe_dump(one, sort_keys=False), encoding="utf-8")

    low_cash = dict(document)
    low_cash["funding"] = {"currency": "USD", "initial_cash": "50"}
    (root / "low-cash.yaml").write_text(yaml.safe_dump(low_cash, sort_keys=False), encoding="utf-8")

    invalid = dict(document)
    invalid["data"] = dict(cast(dict[str, object], invalid["data"]))
    invalid["data"]["fingerprint"] = {"sha256": "0" * 64, "record_count": 4}
    (root / "invalid.yaml").write_text(yaml.safe_dump(invalid, sort_keys=False), encoding="utf-8")
    return root


def _settings(tmp_path: Path) -> WebSettings:
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>EA Web</title>", encoding="utf-8")
    (ui / "app.js").write_text("console.log('ea')", encoding="utf-8")
    return WebSettings(
        scenario_root=_scenario_root(tmp_path),
        workspace=tmp_path / "workspace",
        ui_dir=ui,
        port=8765,
    )


def _validate(client: TestClient, scenario_id: str) -> dict[str, Any]:
    response = client.post(f"/api/scenarios/{scenario_id}/validate", headers=WRITE_HEADERS)
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


def _wait(client: TestClient, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"/api/backtests/{job_id}")
        assert response.status_code == 200, response.text
        job = cast(dict[str, Any], response.json())
        if job["status"] not in {"accepted", "running"}:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish")


def _batch_request(
    identity: dict[str, object],
    runs: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "scenario_id": "bounded-long.yaml",
        "input_identity": identity,
        "initial_cash": "10000",
        "runs": [{"strategy_parameters": run} for run in runs],
    }


def test_batch_api_creates_two_real_jobs_and_recovers_them_after_restart(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        validated = _validate(client, "bounded-long.yaml")
        response = client.post(
            "/api/batches",
            json=_batch_request(
                validated["input_identity"],
                [
                    {"target_quantity": "2", "entry_delay_bars": 0},
                    {"target_quantity": "2", "entry_delay_bars": 2},
                ],
            ),
            headers=WRITE_HEADERS,
        )
        assert response.status_code == 202, response.text
        accepted = response.json()
        assert accepted["schema"] == "ea.local-web-batch.v1"
        assert accepted["scenario_id"] == "bounded-long.yaml"
        assert accepted["member_count"] == 2
        assert accepted["status"] == "running"
        assert len(accepted["members"]) == 2
        assert [member["presentation_status"] for member in accepted["members"]] == [
            "queued",
            "queued",
        ]

        completed = [_wait(client, member["job_id"]) for member in accepted["members"]]
        assert [member["status"] for member in completed] == ["succeeded", "succeeded"]
        reports = [
            client.get(f"/api/backtests/{member['job_id']}/report").json() for member in completed
        ]
        assert [report["economics"]["execution"]["fill"]["price"] for report in reports] == [
            "101.5",
            "110",
        ]
        assert [report["economics"]["net_pnl"]["amount"] for report in reports] == ["17", "0"]

        batch_id = accepted["batch_id"]
        current = client.get(f"/api/batches/{batch_id}")
        assert current.status_code == 200
        assert current.json()["status"] == "complete"

    with TestClient(create_app(settings), base_url=ORIGIN) as restarted:
        recovered = restarted.get(f"/api/batches/{batch_id}")
        assert recovered.status_code == 200
        assert recovered.json() == current.json()
        assert [
            member["input_snapshot"]["scenario"]["strategy"]
            for member in recovered.json()["members"]
        ] == [
            {"id": "bounded-long-v1", "target_quantity": "2", "entry_delay_bars": 0},
            {"id": "bounded-long-v1", "target_quantity": "2", "entry_delay_bars": 2},
        ]


@pytest.mark.parametrize("run_count", [1, 11])
def test_batch_api_rejects_out_of_bounds_run_counts_without_creating_jobs(
    tmp_path: Path,
    run_count: int,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        validated = _validate(client, "bounded-long.yaml")
        runs = [
            {"target_quantity": str(index + 1), "entry_delay_bars": index % 3}
            for index in range(run_count)
        ]
        response = client.post(
            "/api/batches",
            json=_batch_request(validated["input_identity"], runs),
            headers=WRITE_HEADERS,
        )

        assert response.status_code == 422
        assert client.get("/api/backtests").json() == {"jobs": []}


def test_batch_api_accepts_ten_distinct_parameter_combinations(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        validated = _validate(client, "bounded-long.yaml")
        runs = [
            {"target_quantity": str(quantity), "entry_delay_bars": delay}
            for quantity in range(1, 5)
            for delay in range(3)
        ][:10]
        response = client.post(
            "/api/batches",
            json=_batch_request(validated["input_identity"], runs),
            headers=WRITE_HEADERS,
        )

        assert response.status_code == 202, response.text
        assert response.json()["member_count"] == 10
        terminal = [_wait(client, member["job_id"]) for member in response.json()["members"]]
        assert all(member["status"] not in {"accepted", "running"} for member in terminal)


def test_batch_api_isolates_success_and_failed_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_delayed_member(
        scenario: LoadedBacktestScenario,
        output_root: Path,
        *,
        run_id: RunId,
    ) -> Any:
        if scenario.entry_delay_bars == 2:
            raise RuntimeError("injected member failure")
        return run_product_backtest(scenario, output_root, run_id=run_id)

    monkeypatch.setattr(web_service, "run_backtest_scenario", fail_delayed_member)
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        validated = _validate(client, "bounded-long.yaml")
        request = _batch_request(
            validated["input_identity"],
            [
                {"target_quantity": "1", "entry_delay_bars": 0},
                {"target_quantity": "1", "entry_delay_bars": 2},
            ],
        )
        response = client.post("/api/batches", json=request, headers=WRITE_HEADERS)
        assert response.status_code == 202, response.text
        accepted = response.json()
        for member in accepted["members"]:
            _wait(client, member["job_id"])

        batch = client.get(f"/api/batches/{accepted['batch_id']}").json()
        assert batch["status"] == "complete"
        assert [member["presentation_status"] for member in batch["members"]] == [
            "succeeded",
            "failed",
        ]
        assert [member["report_ready"] for member in batch["members"]] == [True, False]


@pytest.mark.parametrize(
    "invalid_run",
    [
        {"target_quantity": "2", "entry_delay_bars": 3},
        {"target_quantity": "2", "entry_delay_bars": 0, "unknown": 1},
    ],
)
def test_batch_api_validates_every_member_before_creating_any_job(
    tmp_path: Path,
    invalid_run: dict[str, object],
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        validated = _validate(client, "bounded-long.yaml")
        response = client.post(
            "/api/batches",
            json=_batch_request(
                validated["input_identity"],
                [
                    {"target_quantity": "2", "entry_delay_bars": 0},
                    invalid_run,
                ],
            ),
            headers=WRITE_HEADERS,
        )

        assert response.status_code == 422
        assert client.get("/api/backtests").json() == {"jobs": []}


def test_batch_api_rejects_duplicate_normalized_parameter_combinations(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        validated = _validate(client, "bounded-long.yaml")
        response = client.post(
            "/api/batches",
            json=_batch_request(
                validated["input_identity"],
                [
                    {"target_quantity": "2", "entry_delay_bars": 0},
                    {"target_quantity": "2", "entry_delay_bars": 0},
                ],
            ),
            headers=WRITE_HEADERS,
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "duplicate_batch_run"
        assert client.get("/api/backtests").json() == {"jobs": []}


def test_history_is_newest_first_by_persisted_creation_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated_ids = iter(
        [
            "ffffffff-ffff-4fff-8fff-ffffffffffff",
            "00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000000",
            "00000000-0000-4000-8000-000000000002",
        ]
    )
    created_at = iter(["2026-09-05T15:00:00.000000Z", "2026-09-05T15:10:00.000000Z"])
    monkeypatch.setattr(web_service, "uuid4", lambda: next(generated_ids))
    monkeypatch.setattr(web_service, "_created_at", lambda: next(created_at))
    settings = _settings(tmp_path)

    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        validated = _validate(client, "bounded-long.yaml")
        accepted_ids: list[str] = []
        for request_id in ("request-history-0001", "request-history-0002"):
            response = client.post(
                "/api/backtests",
                json={
                    "scenario_id": "bounded-long.yaml",
                    "input_identity": validated["input_identity"],
                    "request_id": request_id,
                },
                headers=WRITE_HEADERS,
            )
            assert response.status_code == 202, response.text
            accepted = response.json()
            accepted_ids.append(accepted["job_id"])
            assert _wait(client, accepted["job_id"])["status"] == "succeeded"

        history = client.get("/api/backtests").json()["jobs"]
        assert [item["job_id"] for item in history] == list(reversed(accepted_ids))
        assert [item["created_at"] for item in history] == [
            "2026-09-05T15:10:00.000000Z",
            "2026-09-05T15:00:00.000000Z",
        ]


def test_validation_normalizes_editable_parameters_without_changing_registered_symbol(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        response = client.post(
            "/api/scenarios/bounded-long.yaml/validate",
            json={"parameters": {"initial_cash": "20000", "quantity": "4"}},
            headers=WRITE_HEADERS,
        )

    assert response.status_code == 200, response.text
    validated = response.json()
    assert validated["summary"]["initial_cash"] == "20000"
    assert validated["summary"]["parameters"]["target_quantity"] == "4"
    assert validated["summary"]["symbol"] == "AAPL"
    assert validated["summary"]["venue"] == "XNAS"
    assert (
        validated["normalized_input_identity"]["scenario_sha256"]
        != validated["input_identity"]["scenario_sha256"]
    )
    assert (
        validated["normalized_input_identity"]["data_sha256"]
        == validated["input_identity"]["data_sha256"]
    )


def test_backend_publishes_and_validates_the_two_strategy_parameters(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        registered = _validate(client, "bounded-long.yaml")
        contracts = {item["name"]: item for item in registered["strategy_parameters"]}
        assert set(contracts) == {"target_quantity", "entry_delay_bars"}
        assert contracts["entry_delay_bars"] == {
            "name": "entry_delay_bars",
            "required": True,
            "static_minimum": 0,
            "static_maximum": None,
            "type": "integer",
            "default": 0,
            "current_value": 0,
            "minimum": 0,
            "maximum": 2,
        }

        response = client.post(
            "/api/scenarios/bounded-long.yaml/validate",
            json={
                "parameters": {
                    "initial_cash": "20000",
                    "strategy_parameters": {
                        "target_quantity": "4",
                        "entry_delay_bars": 2,
                    },
                }
            },
            headers=WRITE_HEADERS,
        )

    assert response.status_code == 200, response.text
    validated = response.json()
    assert validated["summary"]["parameters"]["target_quantity"] == "4"
    assert validated["summary"]["parameters"]["entry_delay_bars"] == 2
    normalized_contracts = {item["name"]: item for item in validated["strategy_parameters"]}
    assert normalized_contracts["entry_delay_bars"]["current_value"] == 2


@pytest.mark.parametrize(
    "strategy_parameters",
    [
        {"target_quantity": "2", "entry_delay_bars": 3},
        {"target_quantity": "2", "entry_delay_bars": "1"},
        {"target_quantity": "2", "entry_delay_bars": 0, "unknown": 1},
    ],
)
def test_api_rejects_invalid_or_unknown_strategy_parameters(
    tmp_path: Path,
    strategy_parameters: dict[str, object],
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        response = client.post(
            "/api/scenarios/bounded-long.yaml/validate",
            json={
                "parameters": {
                    "initial_cash": "10000",
                    "strategy_parameters": strategy_parameters,
                }
            },
            headers=WRITE_HEADERS,
        )

    assert response.status_code == 422


def _tree_digest(path: Path) -> dict[str, str]:
    return {
        str(item.relative_to(path)): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def _input_sha256(snapshot: dict[str, object]) -> str:
    payload = (
        json.dumps(
            snapshot,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )
    return hashlib.sha256(b"ea.local-web-input.v1\0" + payload).hexdigest()


def test_new_job_persists_normalized_input_snapshot_and_digest(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        validated = _validate(client, "bounded-long.yaml")
        response = client.post(
            "/api/backtests",
            json={
                "scenario_id": "bounded-long.yaml",
                "input_identity": validated["input_identity"],
                "parameters": {"initial_cash": "20000", "quantity": "4"},
                "request_id": "request-snapshot-0001",
            },
            headers=WRITE_HEADERS,
        )
        assert response.status_code == 202, response.text
        accepted = response.json()

        snapshot = accepted["input_snapshot"]
        assert accepted["schema"] == "ea.local-web-job.v2"
        assert snapshot["schema"] == "ea.local-web-input.v1"
        assert snapshot["scenario_id"] == "bounded-long.yaml"
        assert snapshot["source_identity"] == validated["input_identity"]
        assert snapshot["identity"]["data_sha256"] == validated["input_identity"]["data_sha256"]
        assert snapshot["identity"]["record_count"] == validated["input_identity"]["record_count"]
        assert (
            snapshot["identity"]["scenario_sha256"]
            != validated["input_identity"]["scenario_sha256"]
        )
        assert snapshot["scenario"]["funding"] == {
            "currency": "USD",
            "initial_cash": "20000",
        }
        assert snapshot["scenario"]["strategy"] == {
            "id": "bounded-long-v1",
            "target_quantity": "4",
            "entry_delay_bars": 0,
        }
        assert snapshot["scenario"]["instrument"]["symbol"] == "AAPL"
        assert snapshot["scenario"]["instrument"]["venue"] == "XNAS"
        assert snapshot["scenario"]["data"]["fingerprint"] == {
            "record_count": 4,
            "sha256": validated["input_identity"]["data_sha256"],
        }
        assert accepted["input_sha256"] == _input_sha256(snapshot)
        assert datetime.strptime(accepted["created_at"], "%Y-%m-%dT%H:%M:%S.%fZ")

        persisted = json.loads(
            (settings.workspace / "jobs" / f"{accepted['job_id']}.json").read_text(encoding="ascii")
        )
        assert persisted["input_snapshot"] == snapshot
        assert persisted["input_sha256"] == accepted["input_sha256"]
        assert persisted["created_at"] == accepted["created_at"]
        materialized = settings.workspace / "inputs" / f"{accepted['job_id']}.json"
        assert materialized.is_file()
        assert materialized.resolve().is_relative_to((settings.workspace / "inputs").resolve())

        completed = _wait(client, accepted["job_id"])
        report = client.get(f"/api/backtests/{completed['job_id']}/report").json()
        assert report["economics"]["initial_funding"] == {
            "amount": "20000",
            "currency": "USD",
        }
        assert report["economics"]["ending_positions"] == [
            {"quantity": "4", "symbol": "AAPL", "venue": "XNAS"}
        ]
        assert report["economics"]["equity"]["amount"] == "20034"
        assert report["economics"]["net_pnl"]["amount"] == "34"
        assert report["economics"]["total_return"]["value"] == "0.0017"

        duplicate = client.post(
            "/api/backtests",
            json={
                "scenario_id": "bounded-long.yaml",
                "input_identity": validated["input_identity"],
                "parameters": {"initial_cash": "20000", "quantity": "4"},
                "request_id": "request-snapshot-0001",
            },
            headers=WRITE_HEADERS,
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["job_id"] == accepted["job_id"]

        conflict = client.post(
            "/api/backtests",
            json={
                "scenario_id": "bounded-long.yaml",
                "input_identity": validated["input_identity"],
                "parameters": {"initial_cash": "20000", "quantity": "3"},
                "request_id": "request-snapshot-0001",
            },
            headers=WRITE_HEADERS,
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "input_conflict"


def test_nested_strategy_parameters_persist_reload_and_reach_the_real_engine(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        registered = _validate(client, "bounded-long.yaml")
        response = client.post(
            "/api/backtests",
            json={
                "scenario_id": "bounded-long.yaml",
                "input_identity": registered["input_identity"],
                "parameters": {
                    "initial_cash": "10000",
                    "strategy_parameters": {
                        "target_quantity": "2",
                        "entry_delay_bars": 2,
                    },
                },
                "request_id": "request-delayed-entry-0001",
            },
            headers=WRITE_HEADERS,
        )
        assert response.status_code == 202, response.text
        accepted = response.json()
        assert accepted["input_snapshot"]["scenario"]["strategy"] == {
            "id": "bounded-long-v1",
            "target_quantity": "2",
            "entry_delay_bars": 2,
        }
        completed = _wait(client, accepted["job_id"])
        assert completed["status"] == "succeeded"
        report = client.get(f"/api/backtests/{accepted['job_id']}/report").json()
        assert report["source"]["strategy"] == {
            "id": "bounded-long-v1",
            "target_quantity": "2",
            "entry_delay_bars": 2,
        }
        assert report["economics"]["execution"]["fill"] == {
            "price": "110",
            "quantity": "2",
            "side": "buy",
        }
        assert report["economics"]["ending_cash"] == [{"amount": "9780", "currency": "USD"}]
        assert report["economics"]["net_pnl"]["amount"] == "0"

    with TestClient(create_app(settings), base_url=ORIGIN) as restarted:
        recovered = restarted.get(f"/api/backtests/{accepted['job_id']}")
        assert recovered.status_code == 200
        assert recovered.json()["input_snapshot"]["scenario"]["strategy"] == {
            "id": "bounded-long-v1",
            "target_quantity": "2",
            "entry_delay_bars": 2,
        }
        assert restarted.get(f"/api/backtests/{accepted['job_id']}/report").json() == report


def test_idempotency_compares_effective_normalized_input_when_parameters_are_omitted(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        validated = _validate(client, "bounded-long.yaml")
        explicit = client.post(
            "/api/backtests",
            json={
                "scenario_id": "bounded-long.yaml",
                "input_identity": validated["input_identity"],
                "parameters": {"initial_cash": "20000", "quantity": "4"},
                "request_id": "request-explicit-then-omitted-0001",
            },
            headers=WRITE_HEADERS,
        )
        assert explicit.status_code == 202, explicit.text
        assert _wait(client, explicit.json()["job_id"])["status"] == "succeeded"

        omitted_retry = client.post(
            "/api/backtests",
            json={
                "scenario_id": "bounded-long.yaml",
                "input_identity": validated["input_identity"],
                "request_id": "request-explicit-then-omitted-0001",
            },
            headers=WRITE_HEADERS,
        )
        assert omitted_retry.status_code == 409
        assert omitted_retry.json()["error"]["code"] == "input_conflict"

        omitted = client.post(
            "/api/backtests",
            json={
                "scenario_id": "bounded-long.yaml",
                "input_identity": validated["input_identity"],
                "request_id": "request-omitted-then-explicit-0002",
            },
            headers=WRITE_HEADERS,
        )
        assert omitted.status_code == 202, omitted.text
        assert _wait(client, omitted.json()["job_id"])["status"] == "succeeded"

        explicit_retry = client.post(
            "/api/backtests",
            json={
                "scenario_id": "bounded-long.yaml",
                "input_identity": validated["input_identity"],
                "parameters": {"initial_cash": "20000", "quantity": "4"},
                "request_id": "request-omitted-then-explicit-0002",
            },
            headers=WRITE_HEADERS,
        )
        assert explicit_retry.status_code == 409
        assert explicit_retry.json()["error"]["code"] == "input_conflict"


def test_worker_consumes_input_frozen_at_job_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    service = WebService(settings.scenario_root, settings.workspace)
    service.start()
    try:
        validated = service.registry.validate("bounded-long.yaml")
        original_load = service.registry.load
        load_count = 0

        def load_only_for_acceptance(scenario_id: str) -> object:
            nonlocal load_count
            load_count += 1
            if load_count > 1:
                raise AssertionError("worker reloaded mutable registry input")
            return original_load(scenario_id)

        monkeypatch.setattr(service.registry, "load", load_only_for_acceptance)
        accepted, created = service.create_job(
            scenario_id="bounded-long.yaml",
            input_identity=cast(dict[str, object], validated["input_identity"]),
            request_id="request-frozen-input-0001",
        )
        assert created is True

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            completed = service.get_job(accepted.job_id)
            if completed.status not in {"accepted", "running"}:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("frozen-input job did not finish")

        assert completed.status == "succeeded"
        assert completed.engine_run_id
        assert completed.document()["attempt_id"] == completed.engine_run_id
        assert load_count == 1
    finally:
        service.stop()


def test_restart_rejects_v2_job_when_snapshot_conflicts_with_outer_identity(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        validated = _validate(client, "flat.yaml")
        response = client.post(
            "/api/backtests",
            json={
                "scenario_id": "flat.yaml",
                "input_identity": validated["input_identity"],
                "request_id": "request-snapshot-conflict-0001",
            },
            headers=WRITE_HEADERS,
        )
        job = _wait(client, response.json()["job_id"])

    path = settings.workspace / "jobs" / f"{job['job_id']}.json"
    document = json.loads(path.read_text(encoding="ascii"))
    document["scenario_id"] = "different.yaml"
    path.write_text(
        json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="ascii",
    )

    with (
        pytest.raises(WebBoundaryError, match="workspace job index is invalid"),
        TestClient(create_app(settings), base_url=ORIGIN),
    ):
        pass


def test_real_api_runs_reports_is_idempotent_and_survives_restart(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app, base_url=ORIGIN) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json() == {
            "service": "ea-local-web",
            "version": "0.2.0",
            "offline_only": True,
            "single_active_job": True,
        }

        scenarios = client.get("/api/scenarios")
        assert scenarios.status_code == 200
        by_id = {item["scenario_id"]: item for item in scenarios.json()["scenarios"]}
        assert set(by_id) == {
            "bounded-long.yaml",
            "flat.yaml",
            "invalid.yaml",
            "low-cash.yaml",
            "one.yaml",
        }
        assert by_id["bounded-long.yaml"]["valid"] is True
        assert by_id["invalid.yaml"] == {
            "scenario_id": "invalid.yaml",
            "name": "invalid",
            "valid": False,
            "error_code": "scenario_invalid",
            "message": "data fingerprint conflicts with selected market data",
        }

        validated = _validate(client, "bounded-long.yaml")
        request = {
            "scenario_id": "bounded-long.yaml",
            "input_identity": validated["input_identity"],
            "request_id": "request-long-0001",
        }
        accepted = client.post("/api/backtests", json=request, headers=WRITE_HEADERS)
        assert accepted.status_code == 202, accepted.text
        job_id = accepted.json()["job_id"]

        duplicate = client.post("/api/backtests", json=request, headers=WRITE_HEADERS)
        assert duplicate.status_code == 200
        assert duplicate.json()["job_id"] == job_id

        job = _wait(client, job_id)
        assert job["status"] == "succeeded"
        assert job["report_ready"] is True
        assert job["engine_run_id"]

        report_response = client.get(f"/api/backtests/{job_id}/report")
        assert report_response.status_code == 200
        report = report_response.json()
        assert report["run_id"] == job["engine_run_id"]
        assert report["economics"]["ending_cash"] == [{"amount": "9797", "currency": "USD"}]
        assert report["economics"]["ending_positions"] == [
            {"quantity": "2", "symbol": "AAPL", "venue": "XNAS"}
        ]
        assert report["economics"]["equity"]["amount"] == "10017"
        assert report["economics"]["net_pnl"]["amount"] == "17"
        assert report["economics"]["total_return"]["value"] == "0.0017"
        assert report["economics"]["counts"] == {"fills": 1, "orders": 1}
        assert report["economics"]["execution"] == {
            "fill": {"price": "101.5", "quantity": "2", "side": "buy"},
            "order": {"quantity": "2", "side": "buy"},
        }
        independently_valued_equity = (
            Decimal("10000") - Decimal("2") * Decimal("101.5") + Decimal("2") * Decimal("110")
        )
        assert independently_valued_equity == Decimal("10017")
        assert independently_valued_equity - Decimal("10000") == Decimal("17")
        assert (independently_valued_equity - Decimal("10000")) / Decimal("10000") == Decimal(
            "0.0017"
        )

        attempt = settings.workspace / "runs" / job["engine_run_id"]
        report_dir = settings.workspace / "reports" / job_id
        before = (_tree_digest(attempt), _tree_digest(report_dir))
        assert client.get(f"/api/backtests/{job_id}").status_code == 200
        download = client.get(f"/api/backtests/{job_id}/artifacts/report.json")
        assert download.status_code == 200
        assert json.loads(download.content) == report
        assert client.get(f"/api/backtests/{job_id}/artifacts/secret.txt").status_code == 404
        assert (_tree_digest(attempt), _tree_digest(report_dir)) == before

        one = _validate(client, "one.yaml")
        conflict = dict(request)
        conflict["input_identity"] = one["input_identity"]
        assert (
            client.post("/api/backtests", json=conflict, headers=WRITE_HEADERS).status_code == 409
        )

        next_request = {
            "scenario_id": "one.yaml",
            "input_identity": one["input_identity"],
            "request_id": "request-one-0002",
        }
        next_job_id = client.post(
            "/api/backtests", json=next_request, headers=WRITE_HEADERS
        ).json()["job_id"]
        next_job = _wait(client, next_job_id)
        assert next_job["engine_run_id"] != job["engine_run_id"]
        next_report = client.get(f"/api/backtests/{next_job_id}/report").json()
        assert next_report["economics"]["ending_cash"] == [{"amount": "9898.5", "currency": "USD"}]
        assert next_report["economics"]["equity"]["amount"] == "10008.5"
        assert next_report["economics"]["net_pnl"]["amount"] == "8.5"
        assert next_report["economics"]["total_return"]["value"] == "0.00085"
        independently_valued_changed = Decimal("10000") - Decimal("101.5") + Decimal("110")
        assert independently_valued_changed == Decimal("10008.5")
        assert (independently_valued_changed - Decimal("10000")) / Decimal("10000") == Decimal(
            "0.00085"
        )

    with TestClient(create_app(settings), base_url=ORIGIN) as restarted:
        recovered = restarted.get(f"/api/backtests/{job_id}")
        assert recovered.status_code == 200
        assert recovered.json()["status"] == "succeeded"
        assert restarted.get(f"/api/backtests/{job_id}/report").json() == report


def test_api_fails_closed_for_input_business_and_browser_boundaries(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        assert (
            client.post("/api/scenarios/invalid.yaml/validate", headers=WRITE_HEADERS).status_code
            == 422
        )
        assert client.post(
            "/api/scenarios/../prices.csv/validate", headers=WRITE_HEADERS
        ).status_code in {
            404,
            405,
            422,
        }
        assert client.post("/api/scenarios/bounded-long.yaml/validate").status_code == 403
        assert (
            client.post(
                "/api/scenarios/bounded-long.yaml/validate",
                headers={"origin": "http://evil.example", "x-ea-web-request": "1"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/backtests",
                content="scenario_id=bounded-long.yaml",
                headers={**WRITE_HEADERS, "content-type": "application/x-www-form-urlencoded"},
            ).status_code
            == 415
        )

        bounded = _validate(client, "bounded-long.yaml")
        free_text_symbol = client.post(
            "/api/backtests",
            json={
                "scenario_id": "bounded-long.yaml",
                "input_identity": bounded["input_identity"],
                "parameters": {
                    "initial_cash": "10000",
                    "quantity": "2",
                    "symbol": "MSFT",
                },
                "request_id": "request-symbol-0001",
            },
            headers=WRITE_HEADERS,
        )
        assert free_text_symbol.status_code == 422
        assert free_text_symbol.json()["error"]["code"] == "invalid_request"

        invalid_quantity = client.post(
            "/api/backtests",
            json={
                "scenario_id": "bounded-long.yaml",
                "input_identity": bounded["input_identity"],
                "parameters": {"initial_cash": "10000", "quantity": "1.5"},
                "request_id": "request-quantity-0001",
            },
            headers=WRITE_HEADERS,
        )
        assert invalid_quantity.status_code == 422
        assert invalid_quantity.json()["error"]["code"] == "scenario_invalid"

        low = _validate(client, "low-cash.yaml")
        response = client.post(
            "/api/backtests",
            json={
                "scenario_id": "low-cash.yaml",
                "input_identity": low["input_identity"],
                "request_id": "request-lowcash-0003",
            },
            headers=WRITE_HEADERS,
        )
        failed = _wait(client, response.json()["job_id"])
        assert failed["status"] == "failed"
        assert failed["error_code"]
        assert failed["report_ready"] is False
        assert client.get(f"/api/backtests/{failed['job_id']}/report").status_code == 409

        assert client.get("/backtests/example-job").text == "<!doctype html><title>EA Web</title>"
        unknown = client.get("/api/unknown")
        assert unknown.status_code == 404
        assert unknown.headers["content-type"].startswith("application/json")

    with TestClient(create_app(settings), base_url="http://localhost:8765") as wrong_host:
        assert wrong_host.get("/api/health").status_code == 400


def test_scenario_registry_rejects_symlink_escapes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_scenario = outside / "linked.yaml"
    outside_scenario.write_bytes((settings.scenario_root / "bounded-long.yaml").read_bytes())
    (settings.scenario_root / "linked.yaml").symlink_to(outside_scenario)

    outside_data = outside / "prices.csv"
    outside_data.write_bytes((settings.scenario_root / "prices.csv").read_bytes())
    (settings.scenario_root / "escape.csv").symlink_to(outside_data)
    escaped_document = yaml.safe_load(
        (settings.scenario_root / "bounded-long.yaml").read_text(encoding="utf-8")
    )
    escaped_document["data"]["path"] = "escape.csv"
    (settings.scenario_root / "data-escape.yaml").write_text(
        yaml.safe_dump(escaped_document, sort_keys=False), encoding="utf-8"
    )

    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        assert (
            client.post("/api/scenarios/linked.yaml/validate", headers=WRITE_HEADERS).status_code
            == 404
        )
        escaped = client.post("/api/scenarios/data-escape.yaml/validate", headers=WRITE_HEADERS)
        assert escaped.status_code == 422
        assert escaped.json()["error"]["code"] == "scenario_invalid"


def test_scenario_registry_rejects_oversized_yaml(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    (settings.scenario_root / "oversized.yaml").write_bytes(b"#" * (64 * 1024 + 1))

    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        response = client.post("/api/scenarios/oversized.yaml/validate", headers=WRITE_HEADERS)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "scenario_not_found"


def test_app_rejects_ui_and_workspace_symlink_escapes(tmp_path: Path) -> None:
    ui_case = tmp_path / "ui-case"
    ui_case.mkdir()
    ui_settings = _settings(ui_case)
    outside_index = tmp_path / "outside-index.html"
    outside_index.write_text("outside", encoding="utf-8")
    (ui_settings.ui_dir / "index.html").unlink()
    (ui_settings.ui_dir / "index.html").symlink_to(outside_index)

    with pytest.raises(WebBoundaryError, match="ui directory must contain index.html"):
        create_app(ui_settings)

    workspace_case = tmp_path / "workspace-case"
    workspace_case.mkdir()
    workspace_settings = _settings(workspace_case)
    workspace_settings.workspace.mkdir()
    outside_jobs = tmp_path / "outside-jobs"
    outside_jobs.mkdir()
    (workspace_settings.workspace / "jobs").symlink_to(outside_jobs)

    with pytest.raises(WebBoundaryError, match="job index resolves outside workspace"):
        create_app(workspace_settings)


@pytest.mark.parametrize(
    ("scenario_relative", "workspace_relative", "ui_relative"),
    [
        ("shared/scenarios", "shared", "ui"),
        ("scenarios", "shared/workspace", "shared"),
        ("shared", "workspace", "shared/ui"),
    ],
)
def test_app_rejects_overlapping_authorized_roots(
    tmp_path: Path,
    scenario_relative: str,
    workspace_relative: str,
    ui_relative: str,
) -> None:
    scenario_root = tmp_path / scenario_relative
    ui_dir = tmp_path / ui_relative
    scenario_root.mkdir(parents=True)
    ui_dir.mkdir(parents=True, exist_ok=True)
    (ui_dir / "index.html").write_text("<!doctype html>", encoding="utf-8")

    with pytest.raises(WebBoundaryError, match="must not overlap"):
        create_app(
            WebSettings(
                scenario_root=scenario_root,
                workspace=tmp_path / workspace_relative,
                ui_dir=ui_dir,
                port=8765,
            )
        )


def test_service_rejects_overlapping_input_and_workspace_roots(tmp_path: Path) -> None:
    scenario_root = tmp_path / "shared" / "scenarios"
    scenario_root.mkdir(parents=True)

    with pytest.raises(WebBoundaryError, match="must not overlap"):
        WebService(scenario_root, tmp_path / "shared")


def test_reporter_failure_retains_successful_engine_run_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)

    def fail_report(_run_dir: Path, _output_dir: Path) -> object:
        raise RuntimeError("reporter diagnostic must not escape")

    monkeypatch.setattr("ea.web.service.generate_backtest_report", fail_report)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        validated = _validate(client, "flat.yaml")
        response = client.post(
            "/api/backtests",
            json={
                "scenario_id": "flat.yaml",
                "input_identity": validated["input_identity"],
                "request_id": "request-report-failure-0001",
            },
            headers=WRITE_HEADERS,
        )
        failed = _wait(client, response.json()["job_id"])

    assert failed["status"] == "failed"
    assert failed["error_code"] == "report_failed"
    assert failed["message"] == "the engine succeeded but the formal report was unavailable"
    assert failed["engine_run_id"]
    assert failed["report_ready"] is False
    run_dir = settings.workspace / "runs" / failed["engine_run_id"]
    assert (run_dir / "result.json").is_file()
    assert not (settings.workspace / "reports" / failed["job_id"] / "report.json").exists()


def test_summary_download_rejects_bytes_changed_after_report_generation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        validated = _validate(client, "flat.yaml")
        response = client.post(
            "/api/backtests",
            json={
                "scenario_id": "flat.yaml",
                "input_identity": validated["input_identity"],
                "request_id": "request-summary-tamper-0001",
            },
            headers=WRITE_HEADERS,
        )
        job = _wait(client, response.json()["job_id"])
        summary_path = settings.workspace / "reports" / job["job_id"] / "summary.txt"
        summary_path.write_text("tampered summary\n", encoding="utf-8")

        report_response = client.get(f"/api/backtests/{job['job_id']}/artifacts/report.json")
        summary_response = client.get(f"/api/backtests/{job['job_id']}/artifacts/summary.txt")

    assert report_response.status_code == 200
    assert summary_response.status_code == 409
    assert summary_response.json()["error"]["code"] == "report_unavailable"


def test_workspace_lease_blocks_a_second_service_instance(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    with (
        TestClient(create_app(settings), base_url=ORIGIN),
        pytest.raises(
            WebBoundaryError,
            match="workspace is already owned by another local Web service",
        ),
        TestClient(create_app(settings), base_url=ORIGIN),
    ):
        pass


def test_restart_marks_inflight_job_interrupted_without_rerun(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    jobs = settings.workspace / "jobs"
    jobs.mkdir(parents=True)
    job_id = "00000000-0000-4000-8000-000000000167"
    document = {
        "engine_run_id": None,
        "error_code": None,
        "input_identity": {
            "data_sha256": "1" * 64,
            "record_count": 4,
            "scenario_sha256": "2" * 64,
        },
        "job_id": job_id,
        "message": None,
        "report_ready": False,
        "report_sha256": None,
        "summary_sha256": None,
        "request_id": "request-interrupted-0001",
        "scenario_id": "bounded-long.yaml",
        "schema": "ea.local-web-job.v1",
        "status": "running",
    }
    payload = json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    (jobs / f"{job_id}.json").write_text(payload, encoding="ascii")

    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        recovered = client.get(f"/api/backtests/{job_id}")
        assert recovered.status_code == 200
        assert recovered.json() == {
            **document,
            "status": "interrupted",
            "error_code": "service_restarted",
            "message": "local service stopped before completion; the job was not rerun",
        }
        assert list((settings.workspace / "runs").iterdir()) == []
        assert list((settings.workspace / "reports").iterdir()) == []


def test_restart_retains_reserved_attempt_identity_after_real_engine_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    engine_finished = Event()
    release_worker = Event()

    def pause_after_engine(*args: object, **kwargs: object) -> object:
        result = run_product_backtest(*args, **kwargs)  # type: ignore[arg-type]
        engine_finished.set()
        assert release_worker.wait(10)
        return result

    monkeypatch.setattr(web_service, "run_backtest_scenario", pause_after_engine)
    service = WebService(settings.scenario_root, settings.workspace)
    service.start()
    recovered: WebService | None = None
    try:
        validated = service.registry.validate("bounded-long.yaml")
        accepted, created = service.create_job(
            scenario_id="bounded-long.yaml",
            input_identity=cast(dict[str, object], validated["input_identity"]),
            request_id="request-interrupted-attempt-0002",
        )
        assert created is True
        assert engine_finished.wait(10)

        persisted = service.get_job(accepted.job_id)
        assert persisted.status == "running"
        assert persisted.attempt_id is not None
        assert persisted.engine_run_id is None
        attempt = settings.workspace / "runs" / persisted.attempt_id
        assert (attempt / "result.json").is_file()

        recovered_workspace = tmp_path / "recovered-workspace"
        shutil.copytree(settings.workspace, recovered_workspace)
        recovered = WebService(settings.scenario_root, recovered_workspace)
        recovered.start()

        interrupted = recovered.get_job(accepted.job_id)
        assert interrupted.status == "interrupted"
        assert interrupted.attempt_id == persisted.attempt_id
        assert interrupted.engine_run_id is None
        assert interrupted.report_sha256 is None
        assert interrupted.summary_sha256 is None
        assert len(list((recovered_workspace / "runs").iterdir())) == 1
        assert not (recovered_workspace / "reports" / accepted.job_id).exists()
    finally:
        if recovered is not None:
            recovered.stop()
        release_worker.set()
        service.stop()
