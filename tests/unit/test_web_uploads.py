"""Controlled upload entrance: bounded preview before any durable publication."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from ea.data.historical import HistoricalMarketDataError
from ea.web import uploads as uploads_module
from ea.web.datasets import LocalResearchDatasetRegistryV1
from ea.web.uploads import (
    AVAILABILITY_PROVIDED,
    AVAILABILITY_SIMULATED,
    MAX_UPLOAD_BYTES,
    PREVIEW_SAMPLE_BYTES,
    PREVIEW_SAMPLE_ROWS,
    ControlledUploadStoreV1,
    LocalResearchDatasetCatalogV1,
    UploadError,
    seal_version_manifest,
)

HEADER = (
    "schema_version,venue,symbol,interval_start,interval_end,adjustment,"
    "open,high,low,close,volume,source,source_sequence,revision,available_at"
)
START = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
STAMP = "%Y-%m-%dT%H:%M:%S.%fZ"


def _stamp(value: datetime) -> str:
    return value.strftime(STAMP)


def _line(
    index: int,
    *,
    venue: str = "XNAS",
    symbol: str = "AAPL",
    latency_seconds: int = 0,
) -> str:
    """One strict-profile row: a closed one-hour bar, sequence equal to its index."""
    start = START + timedelta(hours=index)
    end = start + timedelta(hours=1)
    return ",".join(
        [
            "1",
            venue,
            symbol,
            _stamp(start),
            _stamp(end),
            "raw",
            "100.0",
            "101.0",
            "99.0",
            "100.5",
            "10.0",
            "vendor.a",
            str(index),
            "0",
            _stamp(end + timedelta(seconds=latency_seconds)),
        ]
    )


def _capture(rows: int, *, header: str = HEADER, **kwargs: Any) -> bytes:
    body = "\n".join(_line(index, **kwargs) for index in range(rows))
    return f"{header}\n{body}\n".encode()


def _store(tmp_path: Any) -> ControlledUploadStoreV1:
    return ControlledUploadStoreV1(tmp_path / "workspace")


def _published(tmp_path: Any, content: bytes, **kwargs: Any) -> tuple[Any, dict[str, Any]]:
    """Publish one capture the way a caller must: with an explicit policy."""
    store = _store(tmp_path)
    kwargs.setdefault("availability_policy", AVAILABILITY_PROVIDED)
    return store, store.publish(content, **kwargs)


# -- preview ----------------------------------------------------------------


def test_preview_of_a_valid_capture_reports_verified_quality(tmp_path: Any) -> None:
    preview = _store(tmp_path).preview(_capture(8))
    assert preview["schema"] == "ea.local-upload-preview.v1"
    assert preview["header_ok"] is True
    assert preview["full_validation"] == "passed"
    assert preview["valid"] is True
    assert preview["first_error"] is None
    assert preview["quality"]["bar_count"] == 8
    assert preview["rows"][0]["record_number"] == 2
    assert preview["rows"][0]["interval_start"] == _stamp(START)
    assert preview["rows"][0]["available_at"] == _stamp(START + timedelta(hours=1))
    assert preview["limitations"]


def test_preview_reports_the_header_defect_at_record_one(tmp_path: Any) -> None:
    preview = _store(tmp_path).preview(_capture(4, header=HEADER.replace("volume", "vol")))
    assert preview["header_ok"] is False
    assert preview["valid"] is False
    assert preview["first_error"]["code"] == "invalid_header"
    assert preview["first_error"]["record_number"] == 1


def test_preview_keeps_the_strict_failure_location(tmp_path: Any) -> None:
    # The second row's interval_end is moved before its own interval_start, which
    # is a real strict-profile defect rather than a formatting difference.
    broken = _capture(4).replace(
        b"2026-01-05T15:30:00.000000Z,raw", b"2026-01-05T14:00:00.000000Z,raw"
    )
    preview = _store(tmp_path).preview(broken)
    assert preview["valid"] is False
    assert preview["first_error"]["code"] == "invalid_bar"
    assert preview["first_error"]["record_number"] == 2
    assert preview["first_error"]["field_name"] == "interval_end"


def test_preview_of_a_large_capture_stays_bounded_and_makes_no_whole_file_claim(
    tmp_path: Any, monkeypatch: Any
) -> None:
    oversized = _capture(900)
    assert len(oversized) > PREVIEW_SAMPLE_BYTES
    preview = _store(tmp_path).preview(oversized)
    assert preview["truncated"] is True
    assert len(preview["rows"]) == PREVIEW_SAMPLE_ROWS
    # The sample was cut mid-file, so its final partial row is not a data defect.
    assert preview["first_error"] is None
    assert preview["valid"] is True
    # Above the full-decode budget the sample stands alone: validity is unclaimed.
    monkeypatch.setattr(uploads_module, "PREVIEW_FULL_DECODE_BYTES", PREVIEW_SAMPLE_BYTES)
    bounded = _store(tmp_path).preview(oversized)
    assert bounded["full_validation"] == "not_attempted"
    assert bounded["valid"] is False
    assert bounded["quality"] is None
    assert bounded["first_error"] is None
    assert bounded["truncated"] is True


def test_preview_rejects_non_bytes_and_empty_input(tmp_path: Any) -> None:
    store = _store(tmp_path)
    assert store.preview("not bytes")["first_error"]["code"] == "upload_invalid"
    assert store.preview(b"")["first_error"]["code"] == "empty_source"
    assert store.preview(b"\xff\xfe")["first_error"]["code"] == "invalid_encoding"
    assert store.list() == []


# -- policy -----------------------------------------------------------------


def test_provided_policy_keeps_the_capture_bytes_and_says_it_is_asserted(tmp_path: Any) -> None:
    content = _capture(3, latency_seconds=45)
    store, manifest = _published(
        tmp_path, content, source_reference="vendor-daily-export", license_reference="internal"
    )
    policy = manifest["availability_policy"]
    assert policy["id"] == AVAILABILITY_PROVIDED
    assert policy["latency_seconds"] is None
    assert policy["pit_verified"] is False
    assert manifest["capture_sha256"] == manifest["source_bytes_sha256"]
    assert manifest["source"]["asserted"] is True
    assert manifest["source"]["reference"] == "vendor-daily-export"
    # The capture is the retained raw bytes themselves, not a copy of them.
    raw = store.root / "raw" / manifest["dataset_id"]
    capture = store.root / "captures" / manifest["dataset_id"]
    assert raw.stat().st_ino == capture.stat().st_ino
    _, dataset = store.load(manifest["dataset_id"])
    assert dataset.selection.events[0].available_at == START + timedelta(hours=1, seconds=45)


def test_simulated_policy_is_materialized_and_recorded(tmp_path: Any) -> None:
    content = _capture(3)
    store, manifest = _published(
        tmp_path,
        content,
        availability_policy=AVAILABILITY_SIMULATED,
        latency_seconds=3600,
    )
    policy = manifest["availability_policy"]
    assert policy["id"] == AVAILABILITY_SIMULATED
    assert policy["latency_seconds"] == 3600
    assert policy["pit_verified"] is False
    assert manifest["capture_sha256"] != manifest["source_bytes_sha256"]
    assert manifest["raw_retained"] is True
    assert (store.root / "raw" / manifest["dataset_id"]).read_bytes() == content
    _, dataset = store.load(manifest["dataset_id"])
    assert dataset.selection.events[0].available_at == START + timedelta(hours=2)


def test_policy_must_be_explicit_and_complete(tmp_path: Any) -> None:
    store = _store(tmp_path)
    with pytest.raises(UploadError) as missing_policy:
        # There is no default: an omitted policy is never recorded as if the
        # uploader had asserted how the capture became available.
        store.publish(_capture(2))
    assert missing_policy.value.code == "upload_policy"
    with pytest.raises(UploadError) as missing_latency:
        store.publish(_capture(2), availability_policy=AVAILABILITY_SIMULATED)
    assert missing_latency.value.code == "upload_policy"
    with pytest.raises(UploadError) as unexpected_latency:
        store.publish(_capture(2), availability_policy=AVAILABILITY_PROVIDED, latency_seconds=1)
    assert unexpected_latency.value.code == "upload_policy"
    with pytest.raises(UploadError) as negative:
        store.publish(_capture(2), availability_policy=AVAILABILITY_SIMULATED, latency_seconds=-1)
    assert negative.value.code == "upload_policy"
    with pytest.raises(UploadError) as unknown:
        store.publish(_capture(2), availability_policy="whatever")
    assert unknown.value.code == "upload_policy"
    assert store.list() == []


def test_metadata_is_bounded_and_never_a_path(tmp_path: Any) -> None:
    store = _store(tmp_path)
    with pytest.raises(UploadError) as traversal:
        store.publish(
            _capture(2), file_name="../../etc/passwd", availability_policy=AVAILABILITY_PROVIDED
        )
    assert traversal.value.code == "upload_metadata"
    with pytest.raises(UploadError) as control:
        store.publish(
            _capture(2), source_reference="line\nbreak", availability_policy=AVAILABILITY_PROVIDED
        )
    assert control.value.code == "upload_metadata"
    _, manifest = _published(tmp_path, _capture(2), file_name="my export.csv")
    assert manifest["source"]["file_name"] == "my export.csv"
    assert manifest["dataset_id"].endswith(".csv")
    assert "/" not in manifest["dataset_id"]


# -- publication bounds -----------------------------------------------------


def test_upload_size_and_identity_are_bounded(tmp_path: Any, monkeypatch: Any) -> None:
    store = _store(tmp_path)
    # The store's own ceiling is deliberately smaller than the decoder's, so one
    # request can never materialize the decoder's million-record worst case.
    assert MAX_UPLOAD_BYTES == 4 * 1024 * 1024
    monkeypatch.setattr(uploads_module, "MAX_UPLOAD_BYTES", 64)
    with pytest.raises(UploadError) as oversize:
        store.publish(_capture(4), availability_policy=AVAILABILITY_PROVIDED)
    assert oversize.value.code == "source_too_large"
    monkeypatch.undo()
    mixed = f"{HEADER}\n{_line(0)}\n{_line(1)}\n{_line(2, symbol='MSFT')}\n".encode()
    with pytest.raises(UploadError) as identity:
        store.publish(mixed, availability_policy=AVAILABILITY_PROVIDED)
    assert identity.value.code == "invalid_identity"
    assert store.list() == []


def test_record_count_is_bounded(tmp_path: Any, monkeypatch: Any) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(uploads_module, "MAX_UPLOAD_RECORDS", 2)
    with pytest.raises(UploadError) as too_many:
        store.publish(_capture(4), availability_policy=AVAILABILITY_PROVIDED)
    assert too_many.value.code == "too_many_records"
    preview = store.preview(_capture(4))
    assert preview["valid"] is False
    assert preview["first_error"]["code"] == "too_many_records"
    assert store.list() == []


def test_version_count_is_bounded(tmp_path: Any, monkeypatch: Any) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(uploads_module, "MAX_PUBLISHED_VERSIONS", 1)
    store.publish(_capture(2), availability_policy=AVAILABILITY_PROVIDED)
    with pytest.raises(UploadError) as quota:
        store.publish(_capture(2), availability_policy=AVAILABILITY_PROVIDED)
    assert quota.value.code == "upload_quota"
    assert len(store.list()) == 1


# -- atomicity --------------------------------------------------------------


def test_a_failed_publication_leaves_no_version_at_all(tmp_path: Any, monkeypatch: Any) -> None:
    store = _store(tmp_path)
    original = uploads_module._write_new  # noqa: SLF001 — the host's own file writer
    staging = store._fds["staging"]  # noqa: SLF001 — the directory a store owns

    def flaky(directory: int, name: str, payload: bytes) -> None:
        if directory != staging:
            # Staging still works; the publication write itself is what fails.
            raise OSError("disk full")
        original(directory, name, payload)

    monkeypatch.setattr(uploads_module, "_write_new", flaky)
    with pytest.raises(UploadError) as failure:
        store.publish(_capture(3), availability_policy=AVAILABILITY_SIMULATED, latency_seconds=60)
    assert failure.value.code == "upload_unavailable"
    assert store.list() == []
    assert list((store.root / "versions").iterdir()) == []
    assert list((store.root / "raw").iterdir()) == []
    assert list((store.root / "captures").iterdir()) == []
    monkeypatch.undo()
    # The store is left usable: the next publication is clean.
    published = store.publish(_capture(3), availability_policy=AVAILABILITY_PROVIDED)
    assert published["dataset_id"]


def test_unpublished_staging_leftovers_are_never_listed(tmp_path: Any) -> None:
    store = _store(tmp_path)
    _published(tmp_path, _capture(2))
    stray = store.root / "staging" / f"{uuid4().hex}.csv"
    stray.write_bytes(_capture(1))
    unrelated = store.root / "staging" / "someone-elses-notes.txt"
    unrelated.write_text("keep me", encoding="utf-8")
    assert [entry["valid"] for entry in store.list()] == [True]
    fresh = ControlledUploadStoreV1(tmp_path / "workspace")
    assert stray.exists() is False
    # Only names this store generates are ever swept; anything else stays.
    assert unrelated.read_text(encoding="utf-8") == "keep me"
    assert [entry["valid"] for entry in fresh.list()] == [True]
    assert len(list((fresh.root / "captures").iterdir())) == 1


def test_a_tampered_capture_is_not_served_as_the_published_version(tmp_path: Any) -> None:
    store, manifest = _published(tmp_path, _capture(3))
    capture = store.root / "captures" / manifest["dataset_id"]
    capture.write_bytes(f"{capture.read_text(encoding='utf-8')}{_line(50)}\n".encode())
    with pytest.raises(ValueError, match="no longer matches"):
        store.load(manifest["dataset_id"])
    entry = store.list()[0]
    assert entry["valid"] is False
    assert entry["error_code"] == "invalid_or_unavailable_input"


def test_manifest_identity_is_host_generated_and_stable(tmp_path: Any) -> None:
    store, manifest = _published(tmp_path, _capture(5))
    assert manifest["schema"] == "ea.local-dataset-version.v1"
    assert manifest["profile"] == "ea-phase1-ohlcv-csv-v1"
    assert manifest["instrument"] == {"venue": "XNAS", "symbol": "AAPL"}
    assert manifest["time_semantics"]["bar_boundary"] == "explicit-interval-start-end"
    assert len(manifest["dataset_id"]) == 36
    assert store.manifest(manifest["dataset_id"]) == manifest
    assert store.has(manifest["dataset_id"]) is True
    assert store.has("research.csv") is False
    assert store.manifest("../../etc/passwd") is None


# -- path trust -------------------------------------------------------------


def test_a_symlinked_staging_directory_is_refused_before_anything_is_removed(
    tmp_path: Any,
) -> None:
    """The exact reported case: staging points outside, and keep.csv survives.

    The store must not write, sweep or delete through a directory it does not own,
    and it must refuse the store before it touches anything at all.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.csv"
    keep.write_bytes(_capture(2))
    workspace = tmp_path / "workspace"
    (workspace / "uploads").mkdir(parents=True)
    (workspace / "uploads" / "staging").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UploadError) as untrusted:
        ControlledUploadStoreV1(workspace)
    assert untrusted.value.code == "store_untrusted"
    assert keep.exists() is True
    assert keep.read_bytes() == _capture(2)
    assert sorted(entry.name for entry in outside.iterdir()) == ["keep.csv"]


def test_a_child_directory_replaced_after_initialization_is_not_followed(tmp_path: Any) -> None:
    """A replaced child path fails closed instead of being written through.

    The strict decoder is handed both the anchored descriptor and the pathname, and
    it refuses when the two no longer name the same file. That refusal is the safe
    product behaviour: reads and writes stay on the descriptors opened at
    construction, and a replaced root is rejected rather than trusted.
    """
    store = _store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    staging = store.root / "staging"
    staging.rename(store.root / "staging-original")
    staging.symlink_to(outside, target_is_directory=True)
    with pytest.raises(UploadError) as replaced:
        store.publish(_capture(2), availability_policy=AVAILABILITY_PROVIDED)
    assert replaced.value.code == "source_changed_during_read"
    # No version, no capture and no raw bytes exist, and nothing at all was
    # created through the replacement.
    assert store.list() == []
    assert list(outside.iterdir()) == []
    assert list((store.root / "versions").iterdir()) == []
    assert list((store.root / "raw").iterdir()) == []
    assert list((store.root / "captures").iterdir()) == []


# -- provenance -------------------------------------------------------------


def test_a_published_version_carries_one_re_validated_evidence_digest(tmp_path: Any) -> None:
    store, manifest = _published(tmp_path, _capture(3), source_reference="vendor-a")
    dataset_id = manifest["dataset_id"]
    evidence = store.version_evidence(dataset_id)
    digest = store.version_evidence_digest(dataset_id)
    assert evidence["schema"] == "ea.local-dataset-version-evidence.v1"
    assert evidence["dataset_id"] == dataset_id
    assert evidence["source"]["reference"] == "vendor-a"
    assert evidence["source"]["asserted"] is True
    assert evidence["availability_policy"]["pit_verified"] is False
    assert evidence["record_count"] == 3
    assert evidence["manifest_sha256"] == manifest["manifest_sha256"]
    assert digest.startswith("sha256:")
    # Both data entrances answer through one catalog, and the digest does not
    # depend on which path read it.
    catalog = LocalResearchDatasetCatalogV1(None, store)
    assert catalog.version_evidence_digest(dataset_id) == digest
    assert catalog.inspect(dataset_id)["version_evidence_digest"] == digest
    assert store.load(dataset_id)[1].selection.fingerprint.record_count == 3


def test_an_edited_version_manifest_is_refused_and_keeps_the_frozen_digest(
    tmp_path: Any,
) -> None:
    store, manifest = _published(tmp_path, _capture(3))
    dataset_id = manifest["dataset_id"]
    frozen = store.version_evidence_digest(dataset_id)
    path = store.root / "versions" / f"{dataset_id}.json"
    edited = json.loads(path.read_bytes())
    edited["source"]["reference"] = "someone-else"
    path.write_bytes(json.dumps(edited, sort_keys=True).encode())
    # The edited record is no longer this version, so it is not served at all,
    # while the digest an accepted job froze stays nameable and unchanged.
    assert store.manifest(dataset_id) is None
    with pytest.raises(ValueError):
        store.load(dataset_id)
    assert store.list()[0]["valid"] is False
    assert frozen.startswith("sha256:")


def test_a_recomputed_manifest_digest_still_changes_the_evidence(tmp_path: Any) -> None:
    """A hostile rewrite that re-seals the record cannot keep the same evidence."""
    store, manifest = _published(tmp_path, _capture(3))
    dataset_id = manifest["dataset_id"]
    frozen = store.version_evidence_digest(dataset_id)
    path = store.root / "versions" / f"{dataset_id}.json"
    edited = json.loads(path.read_bytes())
    edited["source"]["reference"] = "someone-else"
    path.write_bytes(json.dumps(seal_version_manifest(edited), sort_keys=True).encode())
    assert store.manifest(dataset_id) is not None
    assert store.version_evidence_digest(dataset_id) != frozen
    assert store.load(dataset_id)[0].name == dataset_id


# -- catalog ----------------------------------------------------------------


def test_catalog_lists_both_entrances_and_prefers_the_registered_root(tmp_path: Any) -> None:
    registered_root = tmp_path / "registered"
    registered_root.mkdir()
    (registered_root / "research.csv").write_bytes(_capture(6))
    (registered_root / "broken.csv").write_bytes(b"schema_version,venue\n1,XNAS\n")
    registry = LocalResearchDatasetRegistryV1(registered_root)
    uploads = _store(tmp_path)
    published = uploads.publish(_capture(4), availability_policy=AVAILABILITY_PROVIDED)
    catalog = LocalResearchDatasetCatalogV1(registry, uploads)
    entries = {entry["dataset_id"]: entry for entry in catalog.list()}
    assert set(entries) == {"research.csv", "broken.csv", published["dataset_id"]}
    assert entries["research.csv"]["origin"] == "registered"
    assert entries[published["dataset_id"]]["origin"] == "upload"
    assert entries[published["dataset_id"]]["availability_policy"]["pit_verified"] is False
    assert entries[published["dataset_id"]]["version_evidence_digest"].startswith("sha256:")
    assert entries["broken.csv"]["valid"] is False
    loaded, _ = catalog.load("research.csv")
    assert loaded == registry.root / "research.csv"
    assert catalog.load(published["dataset_id"])[1].profile == "ea-phase1-ohlcv-csv-v1"
    # A registered input has no upload version evidence to name, and the catalog
    # says so rather than inventing one.
    assert catalog.version_evidence("research.csv") is None
    assert catalog.version_evidence_digest("research.csv") is None
    with pytest.raises(HistoricalMarketDataError):
        # A registered input that fails strict validation is reported as failing;
        # the upload store never silently serves it instead.
        catalog.load("broken.csv")
    with pytest.raises(ValueError):
        catalog.load("does-not-exist.csv")


def test_catalog_without_a_registered_root_still_serves_uploads(tmp_path: Any) -> None:
    uploads = _store(tmp_path)
    published = uploads.publish(_capture(2), availability_policy=AVAILABILITY_PROVIDED)
    catalog = LocalResearchDatasetCatalogV1(None, uploads)
    assert [entry["dataset_id"] for entry in catalog.list()] == [published["dataset_id"]]
    assert catalog.document()["schema"] == "ea.local-dataset-catalog.v1"
    assert catalog.has(published["dataset_id"]) is True
    with pytest.raises(ValueError):
        catalog.load("research.csv")


def test_chinese_source_is_frozen_with_original_byte_and_control_bounds(tmp_path: Path) -> None:
    store, manifest = _published(
        tmp_path,
        _capture(3),
        source_reference="QUINE 合成教学示例；非真实市场数据",
        license_reference="作者允许复制、修改和使用",
    )
    frozen = store.version_evidence(manifest["dataset_id"])
    assert frozen["source"]["reference"] == "QUINE 合成教学示例；非真实市场数据"
    assert frozen["source"]["asserted"] is True
    for value in ("中" * 101, "hidden\u200btext", "line\nbreak", "\ud800"):
        with pytest.raises(UploadError) as refused:
            store.publish(
                _capture(3), source_reference=value, availability_policy=AVAILABILITY_PROVIDED
            )
        assert refused.value.code == "upload_metadata"
