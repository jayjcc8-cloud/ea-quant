"""Offline historical import: pasted response bytes, no transport, sealed provenance.

Two entrances are covered here. The adapter reads one documented 12-field klines
response the caller already holds and keeps those exact bytes in a re-derivable
capture; the controlled store previews and publishes that capture as one
immutable version whose source, rights and intended use are the user's own
assertion. Neither path opens a socket, and neither records a paste as synthetic
or as supplier-verified.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from ea.plugin import historical_bars
from ea.web.app import WebSettings, create_app
from ea.web.uploads import (
    AVAILABILITY_SIMULATED,
    OFFLINE_HISTORY_VERSION_SCHEMA,
    ControlledUploadStoreV1,
    UploadError,
)

ORIGIN = "http://127.0.0.1:8765"
pytestmark = pytest.mark.filterwarnings(
    "ignore:The anyio.abc.BlockingPortal alias is deprecated:DeprecationWarning"
)
WRITE_HEADERS = {
    "origin": ORIGIN,
    "x-ea-web-request": "1",
    "content-type": "application/json",
}
PAGE_LIMIT = historical_bars.PAGE_LIMIT
ONE_HOUR_MS = 60 * 60 * 1000
START_MS = 1_704_067_200_000  # 2024-01-01T00:00:00Z
OBSERVED = datetime(2024, 1, 3, tzinfo=UTC)


def _record(open_ms: int, close_ms: int) -> list[Any]:
    """One documented record: numeric strings where the strict profile reads them."""
    return [
        open_ms,
        "100.0",
        "101.0",
        "99.0",
        "100.5",
        "10.0",
        close_ms,
        "1000.0",
        7,
        "4.0",
        "400.0",
        "0",
    ]


def _body(bars: int, *, start_ms: int = START_MS) -> bytes:
    records = [
        _record(start_ms + index * ONE_HOUR_MS, start_ms + (index + 1) * ONE_HOUR_MS - 1)
        for index in range(bars)
    ]
    return json.dumps(records).encode("utf-8")


def _query(*, start_ms: int = START_MS, end_ms: int = START_MS + 3 * ONE_HOUR_MS, latency: int = 0):
    return historical_bars.HistoricalBarQueryV1(
        venue="BINANCE",
        symbol="BTCUSDT",
        interval="1h",
        start_ms=start_ms,
        end_ms=end_ms,
        latency_seconds=latency,
    )


def _capture(body: bytes, *, synthetic: bool = False, **query: Any):
    return historical_bars.capture_offline_response(
        body,
        query=_query(**query),
        observed_now=OBSERVED,
        ingested_at=OBSERVED,
        synthetic=synthetic,
    )


def _store(tmp_path: Path) -> ControlledUploadStoreV1:
    return ControlledUploadStoreV1(tmp_path / "workspace")


def _user(**overrides: Any) -> dict[str, Any]:
    declaration: dict[str, Any] = {
        "venue": "BINANCE",
        "symbol": "BTCUSDT",
        "interval": "1h",
        "start_ms": START_MS,
        "end_ms": START_MS + 3 * ONE_HOUR_MS,
        "latency_seconds": 0,
        "synthetic": False,
        "declared_source": "my own export of a public historical endpoint",
        "declared_rights": "public data, my own copy",
        "intended_use": "local research",
    }
    declaration.update(overrides)
    return declaration


# -- the adapter's offline read -------------------------------------------------


def test_offline_capture_keeps_the_exact_pasted_bytes_and_rederives_them() -> None:
    body = _body(3)
    capture = _capture(body)

    envelope = json.loads(capture.envelope_json)
    # The pasted bytes are retained whole, as one page that never had a transport.
    assert envelope["pages"][0]["body_base64"] == base64.b64encode(body).decode("ascii")
    assert envelope["pages"][0]["status"] == 200
    assert envelope["pages"][0]["request_timeout_seconds"] == 0.0
    assert envelope["transport"]["kind"] == historical_bars.OFFLINE_RESPONSE_KIND_USER
    assert envelope["transport"]["real_network"] is False
    assert envelope["transport"]["provider_terms"] == "unresolved"

    # A read re-normalizes those exact bytes under the sealed query and reproduces
    # the strict capture byte for byte; nothing else is trusted.
    assert (
        historical_bars.rederive_strict_capture(
            capture.envelope_json.encode(), capture.provider_evidence
        )
        == capture.strict_csv
    )

    # The decoded rows carry the declared instrument and the user's own latency.
    text = capture.strict_csv.decode("utf-8")
    assert text.count("\n") == 4
    assert "BINANCE,BTCUSDT" in text
    assert capture.record_count == 3


def test_a_paste_is_never_marked_synthetic_on_request() -> None:
    with pytest.raises(historical_bars.HistoricalBarError) as refused:
        _capture(_body(3), synthetic=True)
    assert refused.value.code is historical_bars.HistoricalBarFailureCode.OFFLINE_RESPONSE_INVALID

    # The built-in example is only the example when both its bytes and its
    # declaration are exactly the host's own; its bytes alone are not enough.
    with pytest.raises(historical_bars.HistoricalBarError):
        _capture(
            historical_bars.SYNTHETIC_EXAMPLE_BODY,
            synthetic=True,
            start_ms=historical_bars.SYNTHETIC_EXAMPLE_START_MS - ONE_HOUR_MS,
            end_ms=historical_bars.SYNTHETIC_EXAMPLE_END_MS - ONE_HOUR_MS,
        )

    example = historical_bars.capture_offline_response(
        historical_bars.SYNTHETIC_EXAMPLE_BODY,
        query=historical_bars.synthetic_example_query(),
        observed_now=datetime(2024, 2, 1, tzinfo=UTC),
        ingested_at=datetime(2024, 2, 1, tzinfo=UTC),
        synthetic=True,
    )
    assert (
        json.loads(example.envelope_json)["transport"]["kind"]
        == historical_bars.OFFLINE_RESPONSE_KIND_SYNTHETIC
    )


def test_one_full_page_short_of_the_declared_range_is_refused() -> None:
    # Exactly the page limit of bars, with the declared range end still ahead and
    # the host's clock well past the last bar's close: a single pasted response
    # cannot be one page of a larger answer.
    end_ms = START_MS + (PAGE_LIMIT + 1) * ONE_HOUR_MS
    with pytest.raises(historical_bars.HistoricalBarError) as refused:
        historical_bars.capture_offline_response(
            _body(PAGE_LIMIT),
            query=_query(end_ms=end_ms),
            observed_now=datetime(2024, 3, 1, tzinfo=UTC),
            ingested_at=datetime(2024, 3, 1, tzinfo=UTC),
        )
    assert (
        refused.value.code is historical_bars.HistoricalBarFailureCode.OFFLINE_RESPONSE_INCOMPLETE
    )


# -- the store's preview and publication ----------------------------------------


def test_preview_reports_quality_and_identity_and_writes_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    preview = store.preview_offline_history(_body(3), **_user())

    assert preview["valid"] is True
    assert preview["synthetic"] is False
    assert preview["provenance"] == historical_bars.OFFLINE_RESPONSE_KIND_USER
    assert preview["capture"]["record_count"] == 3
    assert preview["quality"]["bar_count"] == 3
    assert preview["declarations"] == {
        "source": "my own export of a public historical endpoint",
        "rights": "public data, my own copy",
        "intended_use": "local research",
    }
    assert preview["availability_policy"]["id"] == AVAILABILITY_SIMULATED
    assert preview["availability_policy"]["pit_verified"] is False
    assert preview["preview_sha256"].startswith("sha256:")
    assert store.list() == []
    assert not any((tmp_path / "workspace").rglob("*.json"))


def test_a_user_paste_must_declare_its_source_rights_and_use(tmp_path: Path) -> None:
    preview = _store(tmp_path).preview_offline_history(_body(3), **_user(declared_rights=None))
    assert preview["valid"] is False
    assert preview["first_error"]["code"] == "offline_declaration"
    assert preview["preview_sha256"] is None


def test_the_example_is_synthetic_and_takes_no_user_assertion(tmp_path: Path) -> None:
    example = _user(
        synthetic=True,
        venue=historical_bars.SYNTHETIC_EXAMPLE_VENUE,
        symbol=historical_bars.SYNTHETIC_EXAMPLE_SYMBOL,
        interval=historical_bars.SYNTHETIC_EXAMPLE_INTERVAL,
        start_ms=historical_bars.SYNTHETIC_EXAMPLE_START_MS,
        end_ms=historical_bars.SYNTHETIC_EXAMPLE_END_MS,
        latency_seconds=historical_bars.SYNTHETIC_EXAMPLE_LATENCY_SECONDS,
        declared_source=None,
        declared_rights=None,
        intended_use=None,
    )
    preview = _store(tmp_path).preview_offline_history(
        historical_bars.SYNTHETIC_EXAMPLE_BODY, **example
    )
    assert preview["valid"] is True
    assert preview["synthetic"] is True
    assert preview["declarations"] == {"source": None, "rights": None, "intended_use": None}
    assert any("fixture" in item for item in preview["limitations"])

    # The example's own bytes never carry a user's assertion, and a paste never
    # borrows the example's marking.
    with_assertion = dict(example, declared_source="someone else's data")
    refused = _store(tmp_path).preview_offline_history(
        historical_bars.SYNTHETIC_EXAMPLE_BODY, **with_assertion
    )
    assert refused["first_error"]["code"] == "offline_declaration"
    borrowed = _store(tmp_path).preview_offline_history(
        _body(3),
        **_user(synthetic=True, declared_source=None, declared_rights=None, intended_use=None),
    )
    assert borrowed["first_error"]["code"] == "offline_response_invalid"


def test_publish_binds_the_exact_preview_and_survives_a_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    preview = store.preview_offline_history(_body(3), **_user())
    manifest = store.publish_offline_history(
        _body(3), preview_sha256=preview["preview_sha256"], **_user()
    )

    assert manifest["schema"] == OFFLINE_HISTORY_VERSION_SCHEMA
    assert manifest["source"]["declared_by"] == "user"
    assert manifest["source"]["asserted"] is True
    assert manifest["source"]["intended_use"] == "local research"
    assert manifest["source"]["provenance"] == historical_bars.OFFLINE_RESPONSE_KIND_USER
    assert manifest["availability_policy"]["id"] == AVAILABILITY_SIMULATED
    assert (
        manifest["provider_evidence"]["normalizer"]["version"] == historical_bars.NORMALIZER_VERSION
    )

    dataset_id = cast(str, manifest["dataset_id"])
    assert [entry["dataset_id"] for entry in store.list()] == [dataset_id]
    inspected = store.inspect(dataset_id)
    assert inspected["provider"]["evidence_sha256"] == manifest["provider_evidence_sha256"]
    digest = store.version_evidence_digest(dataset_id)

    # A restarted store re-derives the version from the retained envelope alone.
    restarted = _store(tmp_path)
    assert restarted.version_evidence_digest(dataset_id) == digest
    _, dataset = restarted.load(dataset_id)
    assert dataset.selection.fingerprint.record_count == 3

    # The retained bytes are the response envelope, not the normalized capture.
    with pytest.raises(UploadError) as refused:
        restarted.publish_offline_history(_body(3), preview_sha256="sha256:" + "0" * 64, **_user())
    assert refused.value.code == "preview_mismatch"


def test_a_changed_declaration_is_not_the_previewed_one(tmp_path: Path) -> None:
    store = _store(tmp_path)
    preview = store.preview_offline_history(_body(3), **_user())
    with pytest.raises(UploadError) as refused:
        store.publish_offline_history(
            _body(3),
            preview_sha256=preview["preview_sha256"],
            **_user(intended_use="something else"),
        )
    assert refused.value.code == "preview_mismatch"
    assert store.list() == []


# -- the HTTP boundary ----------------------------------------------------------


def _settings(tmp_path: Path) -> WebSettings:
    scenario_root = tmp_path / "scenarios"
    scenario_root.mkdir()
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>EA Web</title>", encoding="utf-8")
    return WebSettings(
        scenario_root=scenario_root, workspace=tmp_path / "workspace", ui_dir=ui, port=8765
    )


def test_http_example_preview_and_publish_round_trip(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        example = client.get("/api/datasets/offline-history/example")
        assert example.status_code == 200
        document = example.json()
        assert document["synthetic"] is True
        assert document["response_sha256"] == historical_bars.SYNTHETIC_EXAMPLE_BODY_SHA256

        declaration = {
            "venue": document["venue"],
            "symbol": document["symbol"],
            "interval": document["interval"],
            "start_ms": document["start_ms"],
            "end_ms": document["end_ms"],
            "latency_seconds": document["latency_seconds"],
            "synthetic": True,
        }
        preview_response = client.post(
            "/api/datasets/offline-history/preview",
            json={"content": document["response_body"], **declaration},
            headers=WRITE_HEADERS,
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        assert preview["valid"] is True
        assert preview["synthetic"] is True

        published = client.post(
            "/api/datasets/offline-history",
            json={
                "content": document["response_body"],
                **declaration,
                "preview_sha256": preview["preview_sha256"],
            },
            headers=WRITE_HEADERS,
        )
        assert published.status_code == 201, published.text
        version = published.json()
        assert version["schema"] == OFFLINE_HISTORY_VERSION_SCHEMA
        assert version["source"]["declared_by"] == "host-example"
        assert version["source"]["asserted"] is False
        assert [entry["dataset_id"] for entry in version["datasets"]] == [version["dataset_id"]]

        # Only the exact previewed identity publishes; a stale digest is refused.
        stale = client.post(
            "/api/datasets/offline-history",
            json={
                "content": document["response_body"],
                **declaration,
                "preview_sha256": "sha256:" + "0" * 64,
            },
            headers=WRITE_HEADERS,
        )
        assert stale.status_code == 422
        assert stale.json()["error"]["code"] == "preview_mismatch"


def test_http_boundary_refuses_a_missing_response_and_a_refused_paste(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        missing = client.post(
            "/api/datasets/offline-history/preview", json=dict(_user()), headers=WRITE_HEADERS
        )
        assert missing.status_code == 422
        assert missing.json()["error"]["code"] == "offline_response_invalid"

        # A user paste that states no rights is refused at the store, and the
        # refusal is reported as a preview verdict rather than a publication.
        refused = client.post(
            "/api/datasets/offline-history/preview",
            json={"content": _body(3).decode("utf-8"), **_user(declared_rights=None)},
            headers=WRITE_HEADERS,
        )
        assert refused.status_code == 200
        verdict = refused.json()
        assert verdict["valid"] is False
        assert verdict["first_error"]["code"] == "offline_declaration"


def test_a_repeated_publication_is_the_same_version(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    content = _body(3).decode("utf-8")
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        preview = client.post(
            "/api/datasets/offline-history/preview",
            json={"content": content, **_user()},
            headers=WRITE_HEADERS,
        ).json()
        body = {"content": content, **_user(), "preview_sha256": preview["preview_sha256"]}
        first = client.post("/api/datasets/offline-history", json=body, headers=WRITE_HEADERS)
        # A retry of the exact preview identity — the same request the panel
        # would resend after a close — is answered with the version already
        # sealed for it, not with a second immutable version.
        second = client.post("/api/datasets/offline-history", json=body, headers=WRITE_HEADERS)

        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        assert second.json()["dataset_id"] == first.json()["dataset_id"]
        assert second.json()["manifest_sha256"] == first.json()["manifest_sha256"]
        assert second.json()["preview_sha256"] == preview["preview_sha256"]
        assert len(second.json()["datasets"]) == 1
        listed = client.get("/api/datasets").json()["datasets"]
        assert [entry["dataset_id"] for entry in listed] == [first.json()["dataset_id"]]


def test_a_multibyte_paste_is_refused_at_the_byte_bound(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    # Under the character ceiling this client shows and over the adapter's
    # 512 KiB response bound once the paste is encoded: the bound is on bytes.
    content = "é" * (historical_bars.MAX_PAGE_BYTES // 2 + 1024)
    assert len(content) < historical_bars.MAX_PAGE_BYTES
    assert len(content.encode("utf-8")) > historical_bars.MAX_PAGE_BYTES

    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        refused = client.post(
            "/api/datasets/offline-history/preview",
            json={"content": content, **_user()},
            headers=WRITE_HEADERS,
        )
        assert refused.status_code == 200, refused.text
        verdict = refused.json()
        assert verdict["valid"] is False
        assert verdict["first_error"]["code"] == "response_too_large"
        # Nothing was read past the bound, so nothing was recorded either.
        assert client.get("/api/datasets").json()["datasets"] == []


def test_a_job_record_carrying_an_offline_version_decodes(tmp_path: Path) -> None:
    """A persisted job that names an offline version is re-readable.

    The job index stores the published version whole, so the record decoder must
    accept the offline schema exactly as it accepts the earlier ones; otherwise a
    restart cannot read back a job that ran over an offline import.
    """
    from ea.web import service as service_module

    store = _store(tmp_path)
    preview = store.preview_offline_history(_body(3), **_user())
    manifest = store.publish_offline_history(
        _body(3), preview_sha256=preview["preview_sha256"], **_user()
    )
    document = {
        "schema": "ea.local-web-job.v1",
        "job_id": "00000000-0000-4000-8000-0000000000ff",
        "request_id": "request-offline-version-0001",
        "scenario_id": "bounded-long.yaml",
        "status": "failed",
        "input_identity": {"data_sha256": "a" * 64, "record_count": 3, "scenario_sha256": "b" * 64},
        "report_ready": False,
        "dataset_version": manifest,
    }
    record = service_module._decode_job(service_module._canonical_json(document))
    assert record.dataset_version is not None
    assert record.dataset_version["schema"] == OFFLINE_HISTORY_VERSION_SCHEMA
    assert record.dataset_version["dataset_id"] == manifest["dataset_id"]
    assert record.dataset_version["preview_sha256"] == manifest["preview_sha256"]
