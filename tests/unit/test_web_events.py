from pathlib import Path

from fastapi.testclient import TestClient

from ea.product.prediction import read_document
from ea.web.app import create_app
from unit.test_web_api import ORIGIN, WRITE_HEADERS, _settings


def test_event_view_reuses_lifecycle_and_rejects_regression(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        catalog = client.get("/api/events").json()["samples"]
        assert len(catalog) == 2
        sample = catalog[0]
        path = f"/api/events/{sample['sample_id']}"
        assert client.get(path).json()["result"] is None
        early = {"as_of": "2026-10-03T11:10:07Z"}
        pending = client.post(path + "/simulate", headers=WRITE_HEADERS, json=early)
        assert pending.json()["result"]["action"] == "not_visible"
        assert pending.json()["result"]["cash"] == "0"
        final = {"as_of": sample["observation"]["available_at"]}
        result = client.post(path + "/simulate", headers=WRITE_HEADERS, json=final).json()
        assert result["result"]["cash"] == "10"
        assert result["result"]["settlement_count"] == 1
        duplicate = client.post(path + "/simulate", headers=WRITE_HEADERS, json=final).json()
        assert duplicate["result"]["action"] == "duplicate_noop"
        assert client.get(path).json()["result"]["cash"] == "10"
        failure = client.post(path + "/simulate", headers=WRITE_HEADERS, json=early)
        assert failure.status_code == 409
        assert failure.json()["error"]["code"] == "event_simulation_rejected"
        assert client.get(path).json()["result"]["settlement_count"] == 1
        assert client.post(path + "/simulate", json=final).status_code == 403
        assert (
            client.post(
                "/api/events/unknown/simulate", headers=WRITE_HEADERS, json=final
            ).status_code
            == 404
        )
        assert (
            client.post(
                path + "/simulate", headers=WRITE_HEADERS, json={"as_of": "bad"}
            ).status_code
            == 422
        )
        void_path = f"/api/events/{catalog[1]['sample_id']}"
        void = client.post(void_path + "/simulate", headers=WRITE_HEADERS, json=final).json()[
            "result"
        ]
        assert (void["status"], void["cash"], void["success"]) == ("void", "4", False)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        assert client.get(path).json()["result"]["settlement_count"] == 1


def test_packaged_samples_are_exact_c2_fixtures() -> None:
    from ea.web.events import sample_document

    root = Path(__file__).resolve().parents[2] / "examples" / "prediction"
    for name in ("historical-resolved", "synthetic-void"):
        assert sample_document(name) == read_document(root / f"{name}.json")
