"""Explicit CSV mapping at the controlled upload seam, not in the mapper.

``map_csv_capture`` is covered by its own unit tests. What is covered here is
what the store and the HTTP boundary do with its result: the exact bytes that
arrived are the retained raw source, the mapper's normalized capture is the
canonical identity, the whole declaration is sealed as evidence on the version,
a read re-derives the capture from the retained raw source and that same
declaration, the strict/legacy path is untouched, and one real engine run binds
a mapped version through its frozen mapping evidence.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import time
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from ea.plugin.builtins import builtin_catalog
from ea.plugin.selection import PluginSelectionV1, resolve_selection
from ea.web.app import WebSettings, create_app
from ea.web.service import InputChangedError, JobRecord, WebService
from ea.web.uploads import (
    AVAILABILITY_PROVIDED,
    MAP_EVIDENCE_SCHEMA,
    MAPPED_PREVIEW_SCHEMA,
    MAPPED_VERSION_SCHEMA,
    UPLOAD_VERSION_SCHEMA,
    ControlledUploadStoreV1,
    evidence_digest,
    seal_version_manifest,
)
from unit.test_web_research import HOST, WRITE_HEADERS, _scenario_root
from unit.test_web_research_declarative import RULE, _declarative_root
from unit.test_web_uploads import HEADER, _capture

pytestmark = pytest.mark.filterwarnings(
    "ignore:The anyio.abc.BlockingPortal alias is deprecated:DeprecationWarning"
)

# One vendor spelling of every strict field. Nothing here is inferred from the
# data: each destination is named, and each timestamp format is declared.
VENDOR_NAMES = {
    "schema_version": "v",
    "venue": "mic",
    "symbol": "ticker",
    "interval_start": "bar_start",
    "interval_end": "bar_end",
    "adjustment": "adj",
    "open": "o",
    "high": "h",
    "low": "lo",
    "close": "c",
    "volume": "vlm",
    "source": "feed",
    "source_sequence": "seq",
    "revision": "rev",
    "available_at": "seen",
}
MAPPING = {"columns": dict(VENDOR_NAMES), "constants": {}}
TIME = {
    "timezone": "UTC",
    "formats": {
        "interval_start": "utc-microseconds",
        "interval_end": "utc-microseconds",
        "available_at": "utc-microseconds",
    },
}
STRICT = _capture(4)


def _vendor(strict: bytes) -> bytes:
    """The same strict rows under vendor names, CRLF, and one unused non-ASCII column."""
    rows = list(csv.reader(io.StringIO(strict.decode("utf-8"), newline="")))
    assert rows[0] == HEADER.split(",")
    output = io.StringIO(newline="")
    writer = csv.writer(output, dialect="excel", lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([*[VENDOR_NAMES[field] for field in rows[0]], "note"])
    for index, row in enumerate(rows[1:]):
        writer.writerow([*row, f"row {index + 1} ünicode"])
    return output.getvalue().encode("utf-8")


def _store(tmp_path: Path) -> ControlledUploadStoreV1:
    return ControlledUploadStoreV1(tmp_path / "workspace")


def _mapped(store: ControlledUploadStoreV1, content: bytes, **overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "mapping": MAPPING,
        "time_declaration": TIME,
        "availability_policy": AVAILABILITY_PROVIDED,
        "file_name": "vendor-bars.csv",
        "source_reference": "vendor export 2026-01-05",
        "license_reference": "internal-research-only",
    }
    arguments.update(overrides)
    return store.publish(content, **arguments)


def _reseal(path: Path, mutate: Any) -> None:
    """Rewrite one published version the way a consistent hostile editor would."""
    document = json.loads(path.read_bytes())
    mutate(document)
    if "mapping_evidence" in document:
        document["mapping_evidence_sha256"] = evidence_digest(document["mapping_evidence"])
    path.write_bytes(
        json.dumps(
            seal_version_manifest(document),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    )


def _version_path(root: Path, dataset_id: str) -> Path:
    return root / "versions" / f"{dataset_id}.json"


def _await_job(service: WebService, job_id: str) -> JobRecord:
    for _ in range(600):
        record = service.get_job(job_id)
        if record.status not in {"accepted", "running"}:
            return record
        time.sleep(0.01)
    raise AssertionError("the offline engine did not settle in time")


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


# --- the raw source is not the capture ---------------------------------------


def test_the_arriving_bytes_are_the_raw_source_and_the_capture_is_the_mapped_form(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    vendor = _vendor(STRICT)
    manifest = _mapped(store, vendor)
    dataset_id = manifest["dataset_id"]
    assert vendor != STRICT
    assert b"\r\n" in vendor and b"\r\n" not in STRICT
    assert manifest["schema"] == MAPPED_VERSION_SCHEMA
    assert manifest["source_bytes_sha256"] == sha256(vendor).hexdigest()
    assert manifest["source_byte_count"] == len(vendor)
    assert manifest["capture_sha256"] == sha256(STRICT).hexdigest()
    assert manifest["capture_byte_count"] == len(STRICT)
    assert manifest["raw_retained"] is True
    assert manifest["source_bytes_sha256"] != manifest["capture_sha256"]
    # The retained raw source is what arrived, byte for byte: CRLF, the unused
    # non-ASCII column and its header, all still there. The capture is the
    # mapped strict form, which holds none of them.
    assert (store.root / "raw" / dataset_id).read_bytes() == vendor
    assert (store.root / "captures" / dataset_id).read_bytes() == STRICT
    assert "ünicode" in vendor.decode("utf-8") and "ünicode" not in STRICT.decode("utf-8")
    path, dataset = store.load(dataset_id)
    assert path == store.root / "captures" / dataset_id
    assert dataset.source_bytes_sha256.value == sha256(STRICT).hexdigest()
    assert store.inspect(dataset_id)["record_count"] == 4


def test_a_version_that_stops_reproducing_its_declaration_is_refused(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    vendor = _vendor(STRICT)
    manifest = _mapped(store, vendor)
    dataset_id = manifest["dataset_id"]
    evidence = store.version_evidence(dataset_id)
    digest = store.version_evidence_digest(dataset_id)
    assert evidence["mapping"]["evidence_sha256"] == manifest["mapping_evidence_sha256"]
    assert evidence["mapping"]["mapping"]["columns"]["close"] == "c"
    store.close()

    # A restart re-derives the same evidence from the retained raw source.
    reopened = _store(tmp_path)
    assert reopened.version_evidence_digest(dataset_id) == digest

    # A changed raw source is a changed source, not a re-read of the capture.
    raw = reopened.root / "raw" / dataset_id
    raw.write_bytes(vendor.replace(b"XNAS", b"XNAZ"))
    with pytest.raises(ValueError, match="raw source"):
        reopened.load(dataset_id)
    raw.write_bytes(vendor)
    assert reopened.version_evidence_digest(dataset_id) == digest

    # A re-sealed declaration that maps close from a column this source does not
    # have is internally consistent, so only re-deriving it can refuse it.
    _reseal(
        _version_path(reopened.root, dataset_id),
        lambda document: document["mapping_evidence"]["declaration"]["mapping"]["columns"].update(
            {"close": "not_a_column"}
        ),
    )
    assert reopened.manifest(dataset_id) is not None
    with pytest.raises(ValueError, match="mapping"):
        reopened.load(dataset_id)
    with pytest.raises(ValueError):
        reopened.version_evidence_digest(dataset_id)
    assert reopened.list()[0]["valid"] is False


def test_the_strict_version_is_unchanged_and_carries_no_mapping_evidence(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    manifest = store.publish(STRICT, availability_policy=AVAILABILITY_PROVIDED)
    dataset_id = manifest["dataset_id"]
    assert manifest["schema"] == UPLOAD_VERSION_SCHEMA
    assert "mapping_evidence" not in manifest
    assert manifest["source_bytes_sha256"] == manifest["capture_sha256"]
    evidence = store.version_evidence(dataset_id)
    assert evidence["schema"] == "ea.local-dataset-version-evidence.v1"
    assert "mapping" not in evidence
    assert "mapping" not in store.inspect(dataset_id)
    assert store.load(dataset_id)[1].source_bytes_sha256.value == manifest["capture_sha256"]


# --- preview: bounded, and never a publication -------------------------------


def test_a_mapped_preview_reports_the_mapper_evidence_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    vendor = _vendor(STRICT)
    preview = store.preview_mapped(
        vendor, mapping=MAPPING, time_declaration=TIME, availability_policy=AVAILABILITY_PROVIDED
    )
    assert preview["schema"] == MAPPED_PREVIEW_SCHEMA
    assert preview["mode"] == "explicit-mapping"
    assert preview["valid"] is True and preview["first_error"] is None
    assert preview["source"]["sha256"] == sha256(vendor).hexdigest()
    assert preview["source"]["stored_here"] is False
    assert preview["capture"]["sha256"] == sha256(STRICT).hexdigest()
    assert preview["mapping"]["columns"]["close"] == "c"
    assert preview["mapping"]["constants"] == {}
    assert preview["time"]["timezone"] == "UTC"
    assert preview["time"]["formats"]["interval_start"] == "utc-microseconds"
    assert preview["quality"]["bar_count"] == 4
    assert preview["preview"]["row_count_sampled"] == 4
    assert preview["preview"]["rows"][0]["close"] == "100.5"
    assert preview["mapping_evidence"]["schema"] == MAP_EVIDENCE_SCHEMA
    assert preview["mapping_evidence"]["declaration"] == {
        "mapping": MAPPING,
        "time_declaration": TIME,
    }
    assert preview["mapping_evidence_sha256"] == evidence_digest(preview["mapping_evidence"])

    # Every refusal is a bounded verdict with the mapper's own code, and the two
    # declarations are both required: no policy is assumed for a mapped input.
    missing_column = store.preview_mapped(
        vendor,
        mapping={"columns": {**MAPPING["columns"], "close": "nope"}, "constants": {}},
        time_declaration=TIME,
        availability_policy=AVAILABILITY_PROVIDED,
    )
    assert missing_column["valid"] is False
    assert missing_column["first_error"]["code"] == "mapping_column_missing"
    no_policy = store.preview_mapped(vendor, mapping=MAPPING, time_declaration=TIME)
    assert no_policy["valid"] is False and no_policy["first_error"]["code"] == "upload_policy"
    no_time = store.preview_mapped(
        vendor, mapping=MAPPING, availability_policy=AVAILABILITY_PROVIDED
    )
    assert no_time["valid"] is False and no_time["first_error"]["code"] == "mapping_declaration"
    # A preview published nothing at all.
    assert store.list() == []
    assert list(store.root.glob("raw/*")) == []
    assert list(store.root.glob("versions/*.json")) == []


# --- the HTTP boundary -------------------------------------------------------


def test_the_http_boundary_carries_file_bytes_and_refuses_ambiguity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RADIAN_SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings = _settings(tmp_path)
    app = create_app(settings)
    vendor = _vendor(STRICT)
    encoded = base64.b64encode(vendor).decode("ascii")
    declaration: dict[str, Any] = {
        "mode": "explicit-mapping",
        "mapping": MAPPING,
        "time_declaration": TIME,
        "availability_policy": AVAILABILITY_PROVIDED,
        "file_name": "vendor-bars.csv",
    }
    with TestClient(app) as client:
        preview = client.post(
            "/api/datasets/upload/preview",
            headers=WRITE_HEADERS,
            json={**declaration, "content_base64": encoded},
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["mode"] == "explicit-mapping"
        assert preview.json()["valid"] is True
        assert preview.json()["mapping_evidence_sha256"].startswith("sha256:")

        published = client.post(
            "/api/datasets/upload",
            headers=WRITE_HEADERS,
            json={**declaration, "content_base64": encoded},
        )
        assert published.status_code == 201, published.text
        manifest = published.json()
        dataset_id = manifest["dataset_id"]
        assert manifest["schema"] == MAPPED_VERSION_SCHEMA
        # The exact arriving bytes are the retained raw source: the base64 payload
        # is decoded once and never re-encoded through a text field.
        assert (settings.workspace / "uploads" / "raw" / dataset_id).read_bytes() == vendor
        assert manifest["capture_sha256"] == sha256(STRICT).hexdigest()

        for extra, code in (
            ({"content": "schema_version\n"}, "upload_invalid"),
            ({"content_base64": "not base64!"}, "upload_invalid"),
        ):
            refused = client.post(
                "/api/datasets/upload",
                headers=WRITE_HEADERS,
                json={**declaration, "content_base64": encoded, **extra},
            )
            assert refused.status_code == 422
            assert refused.json()["error"]["code"] == code

        incomplete = client.post(
            "/api/datasets/upload",
            headers=WRITE_HEADERS,
            json={**declaration, "time_declaration": None, "content_base64": encoded},
        )
        assert incomplete.status_code == 422
        assert incomplete.json()["error"]["code"] == "mapping_declaration"

        # The strict text path is what an existing client still sends.
        strict = client.post(
            "/api/datasets/upload",
            headers=WRITE_HEADERS,
            json={
                "content": _capture(2).decode("utf-8"),
                "availability_policy": AVAILABILITY_PROVIDED,
            },
        )
        assert strict.status_code == 201, strict.text
        assert strict.json()["schema"] == UPLOAD_VERSION_SCHEMA
        strict_preview = client.post(
            "/api/datasets/upload/preview",
            headers=WRITE_HEADERS,
            json={"content": _capture(8).decode("utf-8")},
        )
        assert strict_preview.status_code == 200
        assert strict_preview.json()["schema"] == "ea.local-upload-preview.v1"
        assert strict_preview.json()["valid"] is True

        listed = client.get("/api/datasets", headers=HOST).json()["datasets"]
        uploads = {entry["dataset_id"]: entry for entry in listed if entry["origin"] == "upload"}
        assert uploads[dataset_id]["mapping"]["mapping"]["columns"]["close"] == "c"


# --- one bound mapped version through the real engine ------------------------


def _bound_binding(service: WebService, dataset_id: str) -> Any:
    catalog = builtin_catalog(service, None)
    selection = PluginSelectionV1.decode(
        {
            "data": {
                "plugin_id": f"ea.builtin.data.{dataset_id}",
                "package_version": "1.0.0",
            },
            "strategy": {
                "plugin_id": "ea.builtin.strategy.price-rule-entry-v1",
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
    )
    return resolve_selection(catalog, selection)


def _parameters() -> dict[str, object]:
    return {
        "initial_cash": "10000",
        "strategy_parameters": {"rule": RULE, "target_quantity": "2"},
        "quantity": None,
        "strategy_source": None,
    }


def test_a_bound_mapped_version_runs_the_engine_under_its_mapping_evidence(
    tmp_path: Path,
) -> None:
    service = WebService(_declarative_root(tmp_path), tmp_path / "workspace")
    service.start()
    try:
        manifest = _mapped(service.uploads, _vendor(STRICT))
        dataset_id = manifest["dataset_id"]
        binding = _bound_binding(service, dataset_id)
        data = binding.component("data")
        assert data.identity["version_evidence_sha256"] == (
            service.uploads.version_evidence_digest(dataset_id)
        )
        assert data.identity["availability_policy_id"] == AVAILABILITY_PROVIDED

        validated = service.validate_scenario(
            "rule-entry.yaml", dataset_id=dataset_id, parameters=_parameters()
        )
        job, created = service.create_job(
            scenario_id="rule-entry.yaml",
            input_identity=cast("dict[str, object]", validated["input_identity"]),
            parameters=_parameters(),
            request_id="mapped-run",
            plugin_binding=binding.document(),
        )
        assert created is True
        record = _await_job(service, job.job_id)
        assert record.status == "succeeded", record.message
        # The published version travels with the job, mapping evidence included.
        version = cast("dict[str, Any]", record.dataset_version)
        evidence = cast("dict[str, Any]", version["mapping_evidence"])
        declaration = cast("dict[str, Any]", evidence["declaration"])
        assert version["mapping_evidence_sha256"] == manifest["mapping_evidence_sha256"]
        assert declaration["mapping"] == MAPPING
        report = json.loads(service.report(job.job_id))
        assert report["source"]["data"]["fingerprint"]["sha256"] == manifest["data_sha256"]
        assert record.input_identity["source_sha256"] == manifest["capture_sha256"]
        assert manifest["source_bytes_sha256"] != record.input_identity["source_sha256"]
    finally:
        service.stop()


def test_a_changed_mapping_declaration_is_refused_before_any_job(tmp_path: Path) -> None:
    service = WebService(_declarative_root(tmp_path), tmp_path / "workspace")
    service.start()
    try:
        manifest = _mapped(service.uploads, _vendor(STRICT))
        dataset_id = manifest["dataset_id"]
        binding = _bound_binding(service, dataset_id)
        validated = service.validate_scenario(
            "rule-entry.yaml", dataset_id=dataset_id, parameters=_parameters()
        )
        _reseal(
            _version_path(service.uploads.root, dataset_id),
            lambda document: document["mapping_evidence"]["declaration"]["mapping"][
                "columns"
            ].update({"close": "not_a_column"}),
        )
        # The confirmed binding names an evidence digest this version can no
        # longer derive, so the run is refused before any job exists.
        with pytest.raises(InputChangedError):
            service.create_job(
                scenario_id="rule-entry.yaml",
                input_identity=cast("dict[str, object]", validated["input_identity"]),
                parameters=_parameters(),
                request_id="mapped-run",
                plugin_binding=binding.document(),
            )
        assert service.list_jobs() == []
    finally:
        service.stop()
