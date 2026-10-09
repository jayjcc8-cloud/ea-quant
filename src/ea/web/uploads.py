"""Controlled UTF-8 CSV upload staging and immutable dataset version publication.

This is the second data entrance; the registered read-only ``--data-root`` stays
the first. It adds no second decoder, engine or catalog: an uploaded file passes
the same strict Phase 1 OHLCV decoder, and a published upload becomes one more
entry in the same dataset catalog the registered root is listed through.

Two things are kept deliberately separate and both are recorded on the version:

* the raw bytes the user supplied, whose digest is provenance only, and
* the canonical capture the engine actually reads, whose semantic fingerprint is
  the economic identity.

The availability policy is part of the published version, never a hidden default.
``provided`` keeps the capture's own ``available_at`` column and records it as an
uploader assertion; ``simulated`` replaces that column with ``interval_end +
latency`` and says so. Neither is ever reported as verified point-in-time
availability.

A third case is an *explicitly mapped* upload: the arriving bytes are some
vendor's own CSV, and ``ea.web.csv_mapping`` normalizes them under a declaration
the uploader states field by field. The arriving bytes are still what is
retained as the raw source, the normalized capture is still what is published as
the canonical identity, and the whole declaration is sealed onto the version as
mapping evidence. Reading such a version runs the mapper again over the retained
raw source and the sealed declaration and requires it to reproduce the capture
exactly, so an edited declaration cannot be re-read as this version.

A fourth case is a capture a read-only historical Bar adapter normalized from
bytes a caller-supplied transport returned (``ea.plugin.historical_bars``). It is
one more version schema in this store, never a second store or a second catalog.
The retained raw source is that adapter's response envelope, the sealed provider
evidence names the exact query, provider, normalizer and policy, and a read runs
the normalizer again over the retained page bytes and requires the capture to be
reproduced exactly. No live provider request, permission, licence or
point-in-time verification is claimed for such a version.

A fifth case is an *offline paste*: the user supplies the response bytes
themselves and declares the instrument, the fixed UTC interval, the query range,
a simulated availability latency, and their own source, rights/licence and
intended-use assertions. It reuses the same adapter, the same store and the same
catalog; what differs is the provenance, which is the user's own assertion rather
than a caller's protocol, and the marking, which is either the user's paste or the
host's built-in synthetic example — and the example marking is accepted only for
the host's own example bytes and declaration. A publication binds the exact
preview identity (bytes plus declarations) that this store recomputed, so no
staged draft and no edited declaration can be published behind an old preview.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import stat
import threading
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from ea.data.historical import (
    _HEADER as STRICT_HEADER,
)
from ea.data.historical import (
    PHASE1_OHLCV_PROFILE,
    HistoricalMarketDataError,
    Phase1HistoricalDataset,
    read_full_capture_phase1_ohlcv_csv,
)
from ea.plugin import historical_bars
from ea.web.datasets import (
    DATASET_ID_PATTERN,
    LocalResearchDatasetRegistryV1,
    _coverage,
)

if TYPE_CHECKING:  # the mapper imports this module, so it is imported lazily here
    from ea.web.csv_mapping import MappedCsvCaptureV1

UPLOAD_PREVIEW_SCHEMA = "ea.local-upload-preview.v1"
UPLOAD_VERSION_SCHEMA = "ea.local-dataset-version.v1"
MAPPED_PREVIEW_SCHEMA = "ea.local-csv-mapping-preview.v1"
MAPPED_VERSION_SCHEMA = "ea.local-dataset-version.v2"
# A fourth entrance: a capture a replaceable read-only historical Bar adapter
# normalized from a caller-supplied transport. It is one more version schema in
# this store, not a second store, a second decoder or a second catalog.
PROVIDER_VERSION_SCHEMA = "ea.local-dataset-version.v3"
# A fifth entrance: one user's own pasted historical response read offline by the
# same read-only Bar adapter. It is one more schema in this same store and the
# same catalog — never a second store, decoder or catalog — and it records the
# user's own source, rights and intended-use assertions rather than a caller's
# protocol provenance.
OFFLINE_HISTORY_PREVIEW_SCHEMA = "ea.offline-history-preview.v1"
OFFLINE_HISTORY_VERSION_SCHEMA = "ea.local-dataset-version.v4"
OFFLINE_PREVIEW_IDENTITY_SCHEMA = "ea.offline-history-preview-identity.v1"
PROVIDER_EVIDENCE_SCHEMA = historical_bars.PROVIDER_EVIDENCE_SCHEMA
MAP_EVIDENCE_SCHEMA = "ea.local-csv-mapping-evidence.v1"
DATASET_CATALOG_SCHEMA = "ea.local-dataset-catalog.v1"
VERSION_EVIDENCE_SCHEMA = "ea.local-dataset-version-evidence.v1"

AVAILABILITY_PROVIDED = "ea.availability.provided-in-capture.v1"
AVAILABILITY_SIMULATED = "ea.availability.simulated-bar-end-plus-latency.v1"

# The strict decoder itself accepts up to PHASE1_OHLCV_MAX_BYTES (64 MiB) and
# 1_000_001 records. This store keeps its own, deliberately smaller ceiling: one
# HTTP request must not be able to materialize a million decoded records, and the
# limit is enforced before the decoder allocates anything.
MAX_UPLOAD_BYTES = 4 * 1024 * 1024
MAX_UPLOAD_RECORDS = 100_000
PREVIEW_SAMPLE_ROWS = 20
PREVIEW_SAMPLE_BYTES = 64 * 1024
PREVIEW_FULL_DECODE_BYTES = 4 * 1024 * 1024
MAX_PUBLISHED_VERSIONS = 32
MAX_STORED_BYTES = 384 * 1024 * 1024
MAX_NAME_BYTES = 128
MAX_REFERENCE_BYTES = 300
MAX_LATENCY_SECONDS = 366 * 24 * 3600
MAX_MANIFEST_BYTES = 64 * 1024
# A mapped version manifest also travels inside a job record, which this service
# reads back under its own 64 KiB bound, so it is kept comfortably below it.
MAX_MAPPED_MANIFEST_BYTES = 48 * 1024
MAX_MAPPING_DECLARATION_BYTES = 16 * 1024
# A provider version manifest also travels inside a job record, so it is bounded
# like a mapped one. Only digests and summaries are sealed there: the page bytes
# stay in the retained raw envelope and never enter the manifest.
MAX_PROVIDER_MANIFEST_BYTES = 48 * 1024
MAX_SWEEP_ENTRIES = 4096

_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_REFERENCE_PATTERN = re.compile(r"[\x20-\x7e]{0,300}\Z")
_HOST_FILE_PATTERN = re.compile(r"[0-9a-f]{32}\.csv\Z")
_DIRECTORY_NAMES = ("staging", "raw", "captures", "versions")
_TIME_SEMANTICS = {
    "timestamp_role": "interval",
    "timezone": "UTC",
    "bar_boundary": "explicit-interval-start-end",
}

# True of every version this store publishes, whichever entrance produced it.
_VERSION_LIMITATIONS = (
    "Availability is not verified point-in-time evidence; asserted and simulated values are "
    "never presented as PIT verified.",
    "The raw upload is provenance only; the canonical capture is the identity research binds.",
    "No row is dropped, filled, interpolated, resampled or corrected by the host.",
    "The declared source and licence are the uploader's assertion, not a legal verification.",
)

# True of the fixed strict profile's own decoder, and of it alone: it accepts one
# column layout, and its preview reads a bounded sample even though a publication
# then verifies the whole file.
_STRICT_LIMITATIONS = (
    "A successful preview of a bounded sample is not evidence that the whole file is valid.",
    "Only the fixed strict UTC 15-column OHLCV profile is accepted. The host has no general "
    "column mapper, and it reads the profile's own raw units rather than converting them.",
)

_POLICY_LIMITATIONS = (*_VERSION_LIMITATIONS, *_STRICT_LIMITATIONS)

# What is additionally true of a version an explicit mapping produced. The declaration
# replaces both strict-only statements: the mapper reads every row of the arriving file
# rather than a bounded sample, and it is the general column mapping the strict profile
# does not have.
_MAPPED_LIMITATIONS = (
    "The normalized capture is not the raw source: the arriving bytes are retained unchanged as "
    "the raw upload, and the declared mapping is the only thing that turned them into a capture.",
    "Only the declared columns, constants, timestamp formats and timezone are applied. Nothing is "
    "inferred from the data, and no undeclared source column is projected into the normalized "
    "capture; the original source is retained whole.",
    "The declaration is the uploader's own statement. It is sealed with the version and re-derived "
    "on every read, which proves it is intact rather than that it is correct.",
)

# What is additionally true of a version a provider adapter produced. The raw source
# is the adapter's response envelope rather than the provider's bytes alone, and the
# availability column was simulated rather than asserted by anyone.
_PROVIDER_LIMITATIONS = (
    "The retained raw source is one canonical JSON response envelope, not the provider's own "
    "bytes alone: it holds the exact page bytes base64-encoded beside the exact query, provider, "
    "normalizer, policy and digests. The '.csv' suffix of this dataset identifier is a catalog "
    "identity and not a statement about the retained bytes.",
    "The capture was produced by a replaceable read-only adapter from bytes a caller-supplied "
    "transport returned. No live provider request, no permission, no licence and no point-in-time "
    "verification is claimed; the provider's terms are unresolved and real use is blocked.",
    "available_at was simulated as interval_end plus the caller's declared latency. It is not an "
    "assertion by any uploader and not verified point-in-time availability.",
)

# What is additionally true of a version one user's own pasted response produced.
# This entrance states the user's own assertions, so it must not inherit the
# provider-transport statements above: no caller-supplied transport was involved
# and the user — not a provider protocol — asserted where the bytes came from.
_OFFLINE_USER_LIMITATIONS = (
    "Offline import only. The response bytes were pasted by the user and read in this process; "
    "this build contains no network transport and makes no request, and the host resolves no "
    "provider credential.",
    "The declared source, rights/licence and intended use are the user's own assertion, recorded "
    "verbatim. They are not a legal verification and establish no permission or grant.",
    "available_at was simulated as interval_end plus the user's declared latency. It is not "
    "verified point-in-time availability and is not an assertion by any provider.",
)

# What is additionally true of a version the host's built-in synthetic example
# produced. Nothing here is an uploader assertion.
_SYNTHETIC_VERSION_LIMITATIONS = (
    "Published from the host's built-in synthetic example, not from user-supplied data. It is a "
    "fixture for exercising preview, publication and catalog selection and supports no research "
    "claim.",
    "Availability is not verified point-in-time evidence; simulated values are never presented "
    "as PIT verified.",
    "The raw response is provenance only; the canonical capture is the identity research binds.",
    "The host made no network request and holds no provider permission or licence.",
)


class UploadError(ValueError):
    """One controlled-upload failure with a stable code and bounded location."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        record_number: int | None = None,
        field_name: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.record_number = record_number
        self.field_name = field_name

    def document(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "record_number": self.record_number,
            "field_name": self.field_name,
        }


def _strict_failure(error: HistoricalMarketDataError) -> UploadError:
    """Reuse the strict decoder's own vocabulary instead of inventing a second one."""
    return UploadError(
        error.code.value,
        "strict OHLCV validation failed",
        record_number=error.record_number,
        field_name=error.field_name,
    )


def _bounded_reference(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _REFERENCE_PATTERN.fullmatch(value) is None:
        raise UploadError(
            "upload_metadata",
            f"{field} must be at most {MAX_REFERENCE_BYTES} printable characters",
        )
    return value


def _bounded_file_name(value: object) -> str | None:
    """One client-supplied name kept as a display hint only; never used as a path."""
    if value is None:
        return None
    if (
        type(value) is not str
        or not 1 <= len(value.encode("utf-8")) <= MAX_NAME_BYTES
        or "/" in value
        or "\\" in value
        or value in {".", ".."}
        or any(ord(character) < 32 or ord(character) > 126 for character in value)
    ):
        raise UploadError("upload_metadata", "file_name must be one bounded plain file name")
    return value


@dataclass(frozen=True, slots=True)
class UploadPreviewV1:
    """One bounded sample plus whatever the host could honestly conclude from it."""

    byte_count: int
    header: tuple[str, ...]
    header_ok: bool
    rows: tuple[dict[str, Any], ...]
    truncated: bool
    full_validation: str
    valid: bool
    quality: dict[str, Any] | None
    first_error: dict[str, Any] | None

    def document(self) -> dict[str, Any]:
        return {
            "schema": UPLOAD_PREVIEW_SCHEMA,
            "profile": PHASE1_OHLCV_PROFILE,
            "byte_count": self.byte_count,
            "expected_header": list(STRICT_HEADER),
            "header": list(self.header),
            "header_ok": self.header_ok,
            "rows": list(self.rows),
            "row_count_sampled": len(self.rows),
            "truncated": self.truncated,
            "full_validation": self.full_validation,
            "valid": self.valid,
            "quality": self.quality,
            "first_error": self.first_error,
            "limitations": list(_POLICY_LIMITATIONS),
        }


def _sample_rows(
    content: bytes,
) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...], bool, dict[str, Any] | None]:
    """Bounded structural sample; never claims the whole file is valid."""
    sample = content[:PREVIEW_SAMPLE_BYTES]
    truncated = len(content) > PREVIEW_SAMPLE_BYTES
    try:
        text = sample.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return (
            (),
            (),
            truncated,
            UploadError("invalid_encoding", "the upload is not strict UTF-8").document(),
        )
    if "\x00" in text:
        return (
            (),
            (),
            truncated,
            UploadError("invalid_file_character", "the upload contains a NUL character").document(),
        )
    rows: list[list[str]] = []
    try:
        for row in csv.reader(io.StringIO(text, newline=""), dialect="excel", strict=True):
            rows.append(row)
            if len(rows) > PREVIEW_SAMPLE_ROWS + 1:
                break
    except csv.Error:
        return (
            (),
            (),
            truncated,
            UploadError(
                "malformed_csv",
                "the sampled rows are not well-formed CSV",
                record_number=len(rows) + 1,
            ).document(),
        )
    if not rows:
        return (), (), truncated, UploadError("empty_source", "the upload is empty").document()
    header = tuple(rows[0])
    data_rows = rows[1:]
    if truncated and data_rows:
        # The sample was cut mid-file, so its final row may itself be cut short;
        # reporting that as a data defect would be a fabricated error.
        data_rows = data_rows[:-1]
    sampled: list[dict[str, Any]] = []
    for number, row in enumerate(data_rows[:PREVIEW_SAMPLE_ROWS], start=2):
        if len(row) != len(STRICT_HEADER):
            return (
                header,
                tuple(sampled),
                truncated,
                UploadError(
                    "invalid_record_shape",
                    "the sampled row does not have the strict column count",
                    record_number=number,
                ).document(),
            )
        sampled.append(
            {
                "record_number": number,
                **{name: row[index] for index, name in enumerate(STRICT_HEADER)},
            }
        )
    return header, tuple(sampled), truncated, None


def _rewrite_available_at(content: bytes, latency_seconds: int) -> bytes:
    """Materialize the declared simulated policy as a strict-profile capture."""
    reader = csv.reader(
        io.StringIO(content.decode("utf-8"), newline=""), dialect="excel", strict=True
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output, dialect="excel", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    latency = timedelta(seconds=latency_seconds)
    for index, row in enumerate(reader):
        if index == 0:
            writer.writerow(row)
            continue
        interval_end = datetime.strptime(row[4], _DATETIME_FORMAT).replace(tzinfo=UTC)
        rewritten = list(row)
        rewritten[14] = (interval_end + latency).strftime(_DATETIME_FORMAT)
        writer.writerow(rewritten)
    return output.getvalue().encode("utf-8")


def _open_workspace(workspace: Path) -> int:
    """One nofollow descriptor for the workspace, before anything is written into it."""
    if workspace.is_symlink():
        raise UploadError("store_untrusted", "the upload workspace is not a real directory")
    try:
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        raise UploadError("store_untrusted", "the upload workspace could not be created") from None
    if workspace.is_symlink() or not workspace.is_dir():
        raise UploadError("store_untrusted", "the upload workspace is not a real directory")
    try:
        return os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise UploadError("store_untrusted", "the upload workspace could not be secured") from None


def _open_child_directory(parent: int, name: str) -> int:
    """Create and open one host-owned directory relative to an already-verified parent.

    The parent descriptor, not a path, is what the child is created and opened
    against, and the child itself is opened with ``O_NOFOLLOW`` after a stat that
    does not follow links. A symlinked or replaced child is refused here, before
    any file in it is written, read or removed.
    """
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent)
    except FileExistsError:
        pass
    except OSError:
        raise UploadError("store_untrusted", f"{name} could not be created") from None
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            raise UploadError(
                "store_untrusted",
                f"{name} must be a real directory inside the upload workspace",
            )
        return os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent
        )
    except UploadError:
        raise
    except OSError:
        raise UploadError("store_untrusted", f"{name} could not be secured") from None


def _write_new(directory: int, name: str, payload: bytes) -> None:
    """Create one immutable file inside a verified directory; an existing name is never
    overwritten, and a symlinked name is never followed."""
    descriptor = os.open(
        name,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with suppress(OSError):
            os.unlink(name, dir_fd=directory)
        raise


def _read_bounded(directory: int, name: str, limit: int) -> bytes | None:
    """Read one host-named regular file through a nofollow descriptor, or nothing."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        remaining = info.st_size
        chunks: list[bytes] = []
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 1 << 20))
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _entry_size(directory: int, name: str) -> int | None:
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except OSError:
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None


def _canonical_json(document: object) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def _validated_policy(value: object) -> dict[str, Any]:
    """Re-read one availability policy under the closed schema that writes it.

    The policy is metadata, so it is re-validated on every read: an edited manifest
    cannot smuggle in a third policy, a latency that does not match its own
    evidence string, or a claim of verified point-in-time availability.
    """
    keys = {"id", "version", "latency_seconds", "pit_verified", "evidence"}
    if type(value) is not dict or set(value) != keys:
        raise UploadError("upload_policy", "the availability policy is not the closed schema")
    policy = cast("dict[str, Any]", value)
    if policy["version"] != 1:
        raise UploadError("upload_policy", "the availability policy version is not supported")
    if policy["pit_verified"] is not False:
        raise UploadError(
            "upload_policy",
            "this host never records an upload as verified point-in-time availability",
        )
    if policy["id"] == AVAILABILITY_PROVIDED:
        if policy["latency_seconds"] is not None:
            raise UploadError(
                "upload_policy", "the provided availability policy does not take a latency"
            )
        if policy["evidence"] != "asserted-by-uploader":
            raise UploadError(
                "upload_policy", "the provided availability policy is an uploader assertion"
            )
        return policy
    if policy["id"] == AVAILABILITY_SIMULATED:
        latency = policy["latency_seconds"]
        if (
            type(latency) is not int
            or isinstance(latency, bool)
            or not 0 <= latency <= MAX_LATENCY_SECONDS
        ):
            raise UploadError(
                "upload_policy",
                "simulated availability requires an explicit latency in whole seconds",
            )
        if policy["evidence"] != f"host-simulated: interval_end + {latency}s":
            raise UploadError(
                "upload_policy", "the simulated availability evidence does not match its latency"
            )
        return policy
    raise UploadError("upload_policy", "availability_policy must be one of the supported policies")


def _policy_document(availability_policy: object, latency_seconds: object) -> dict[str, Any]:
    """Resolve the one explicitly chosen availability policy.

    There is no default policy and no default latency: a caller that does not say
    how the capture became available is refused rather than recorded as if the
    uploader had asserted it.
    """
    if availability_policy is None:
        raise UploadError(
            "upload_policy",
            "availability_policy must be chosen explicitly; the host never assumes how an "
            "uploaded capture became available",
        )
    if availability_policy == AVAILABILITY_PROVIDED:
        if latency_seconds is not None:
            # The provided-in-capture policy says the capture already carried its
            # availability; a latency alongside it is a contradiction, not a hint.
            raise UploadError(
                "upload_policy",
                "the provided-in-capture policy carries no latency; do not send one",
            )
        return _validated_policy(
            {
                "id": AVAILABILITY_PROVIDED,
                "version": 1,
                "latency_seconds": None,
                "pit_verified": False,
                "evidence": "asserted-by-uploader",
            }
        )
    if availability_policy == AVAILABILITY_SIMULATED:
        return _validated_policy(
            {
                "id": AVAILABILITY_SIMULATED,
                "version": 1,
                "latency_seconds": latency_seconds,
                "pit_verified": False,
                "evidence": f"host-simulated: interval_end + {latency_seconds}s",
            }
        )
    raise UploadError("upload_policy", "availability_policy must be one of the supported policies")


def evidence_digest(document: object) -> str:
    """One canonical digest over one bounded data-version evidence document."""
    return "sha256:" + sha256(_canonical_json(document)).hexdigest()


def _mapper_schema() -> str:
    """The mapping schema id this build ships, without importing the mapper eagerly."""
    from ea.web.csv_mapping import CSV_MAPPING_SCHEMA

    return CSV_MAPPING_SCHEMA


def _bounded_declaration(value: object, *, field: str) -> Any:
    """One caller-supplied mapping declaration, kept only as bounded canonical JSON.

    The declaration is both the mapper's input and part of the sealed evidence, so
    it is copied into plain JSON data and bounded here rather than being stored as
    whatever object a caller happened to hold.
    """
    try:
        payload = _canonical_json(value)
    except (TypeError, ValueError):
        raise UploadError(
            "mapping_declaration", f"{field} must be JSON data this host can record"
        ) from None
    if len(payload) > MAX_MAPPING_DECLARATION_BYTES:
        raise UploadError(
            "mapping_declaration",
            f"{field} is larger than the {MAX_MAPPING_DECLARATION_BYTES}-byte declaration limit",
        )
    return json.loads(payload)


def _mapping_evidence(result: MappedCsvCaptureV1, declaration: dict[str, Any]) -> dict[str, Any]:
    """The whole explicit declaration beside the mapper's own complete result.

    ``declaration`` is what the uploader stated; ``result`` is what the mapper
    derived from that declaration and from the arriving bytes, including the
    normalized capture's digests, the resolved mapping and time plan, the
    availability policy, the coverage evidence and the bounded preview. A read
    re-derives one from the other and requires the capture to be reproduced
    exactly, which is what ``_validated_mapping`` checks.
    """
    document = result.document()
    return {
        "schema": MAP_EVIDENCE_SCHEMA,
        "version": 1,
        "mapper": document["schema"],
        "profile": document["profile"],
        "declaration": declaration,
        "result": document,
    }


def _mapping_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    """The bounded part of one version's mapping evidence that evidence carries."""
    evidence = cast("dict[str, Any]", manifest["mapping_evidence"])
    result = cast("dict[str, Any]", evidence["result"])
    return {
        "schema": MAP_EVIDENCE_SCHEMA,
        "mapper": evidence["mapper"],
        "evidence_sha256": manifest["mapping_evidence_sha256"],
        "mapping": result["mapping"],
        "time": result["time"],
        "capture": result["capture"],
        "source": result["source"],
    }


def _mapping_declaration(manifest: dict[str, Any]) -> dict[str, Any]:
    """Re-read one version's sealed mapping evidence under its closed schema."""
    evidence = manifest.get("mapping_evidence")
    declaration = evidence.get("declaration") if type(evidence) is dict else None
    result = evidence.get("result") if type(evidence) is dict else None
    if (
        type(evidence) is not dict
        or evidence.get("schema") != MAP_EVIDENCE_SCHEMA
        or evidence.get("version") != 1
        or evidence.get("mapper") != _mapper_schema()
        or manifest.get("mapping_evidence_sha256") != evidence_digest(evidence)
        or type(declaration) is not dict
        or set(declaration) != {"mapping", "time_declaration"}
        or type(result) is not dict
        or result.get("schema") != _mapper_schema()
    ):
        raise ValueError("uploaded dataset version mapping evidence is not the closed schema")
    return cast("dict[str, Any]", declaration)


def _provider_declaration(manifest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Re-read one version's sealed provider evidence under its closed schema.

    It returns the sealed declaration (what the adapter stated it did) and the
    sealed result (what it derived), having first proved the evidence is still the
    evidence this version published.
    """
    evidence = manifest.get("provider_evidence")
    if (
        type(evidence) is not dict
        or evidence.get("schema") != PROVIDER_EVIDENCE_SCHEMA
        or evidence.get("version") != 1
        or manifest.get("provider_evidence_sha256") != evidence_digest(evidence)
    ):
        raise ValueError("uploaded dataset version provider evidence is not the closed schema")
    declaration = evidence.get("declaration")
    result = evidence.get("result")
    if type(declaration) is not dict or type(result) is not dict:
        raise ValueError("uploaded dataset version provider evidence is not the closed schema")
    return cast("dict[str, Any]", declaration), cast("dict[str, Any]", result)


def _provider_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    """The bounded part of one provider version's evidence that evidence carries."""
    declaration, result = _provider_declaration(manifest)
    evidence = cast("dict[str, Any]", manifest["provider_evidence"])
    return {
        "schema": PROVIDER_EVIDENCE_SCHEMA,
        "evidence_sha256": manifest["provider_evidence_sha256"],
        "provider": evidence["provider"],
        "normalizer": evidence["normalizer"],
        "query": declaration.get("query"),
        "policy": declaration.get("policy"),
        "transport": declaration.get("transport"),
        "observed_now_utc": declaration.get("observed_now_utc"),
        "ingested_at_utc": declaration.get("ingested_at_utc"),
        "pages": result.get("pages"),
        "records": result.get("records"),
        "capture": result.get("capture"),
    }


# The declared-by/asserted pair each offline provenance records. A user's own
# paste is an uploader assertion; the host's fixture asserts nothing.
OFFLINE_DECLARED_BY = {
    historical_bars.OFFLINE_RESPONSE_KIND_USER: ("user", True),
    historical_bars.OFFLINE_RESPONSE_KIND_SYNTHETIC: ("host-example", False),
}


def _offline_failure(error: historical_bars.HistoricalBarError) -> UploadError:
    """One adapter refusal, reported in this store's own vocabulary."""
    return UploadError(
        error.code.value,
        error.message,
        record_number=error.record_index,
        field_name=error.field_name,
    )


def _offline_query(
    *,
    venue: object,
    symbol: object,
    interval: object,
    start_ms: object,
    end_ms: object,
    latency_seconds: object,
) -> historical_bars.HistoricalBarQueryV1:
    """Re-read one declared offline query under the adapter's closed contract."""
    return historical_bars.HistoricalBarQueryV1(
        venue=cast("str", venue),
        symbol=cast("str", symbol),
        interval=cast("str", interval),
        start_ms=cast("int", start_ms),
        end_ms=cast("int", end_ms),
        latency_seconds=cast("int", latency_seconds),
    )


def _offline_declaration(
    *,
    synthetic: object,
    declared_source: object,
    declared_rights: object,
    intended_use: object,
) -> dict[str, Any]:
    """Re-read the user's own offline declarations under one closed contract.

    A user's own paste must state its source, its rights/licence and its intended
    use, because this host records no assertion nobody made and fabricates no
    licence. The built-in synthetic example is the opposite case: it is the host's
    fixture, so it takes no user assertion at all and one is refused rather than
    attached to it.
    """
    if type(synthetic) is not bool:
        raise UploadError("offline_declaration", "synthetic must be stated as true or false")
    source = _bounded_reference(declared_source, field="declared_source")
    rights = _bounded_reference(declared_rights, field="declared_rights")
    use = _bounded_reference(intended_use, field="intended_use")
    if synthetic:
        if source is not None or rights is not None or use is not None:
            raise UploadError(
                "offline_declaration",
                "the built-in synthetic example takes no source, rights or intended-use assertion",
            )
    elif not source or not rights or not use:
        raise UploadError(
            "offline_declaration",
            "declare the source, the rights/licence and the intended use of your own pasted "
            "response; the host records no assertion nobody made",
        )
    return {
        "synthetic": synthetic,
        "declared_source": source,
        "declared_rights": rights,
        "intended_use": use,
    }


def _offline_identity(
    payload: bytes,
    query: historical_bars.HistoricalBarQueryV1,
    *,
    synthetic: bool,
    declared_source: str | None,
    declared_rights: str | None,
    intended_use: str | None,
) -> str:
    """The exact identity a publication must bind: bytes plus declarations.

    Host-read instants are deliberately outside it, so a preview read a moment
    before a publication still names the same identity; the publication seals its
    own host read of the clock regardless.
    """
    return evidence_digest(
        {
            "schema": OFFLINE_PREVIEW_IDENTITY_SCHEMA,
            "adapter": historical_bars.adapter_identity_document(),
            "provenance": (
                historical_bars.OFFLINE_RESPONSE_KIND_SYNTHETIC
                if synthetic
                else historical_bars.OFFLINE_RESPONSE_KIND_USER
            ),
            "synthetic": synthetic,
            "response_byte_count": len(payload),
            "response_sha256": sha256(payload).hexdigest(),
            "query": query.document(),
            "declared_source": declared_source,
            "declared_rights": declared_rights,
            "intended_use": intended_use,
        }
    )


def _offline_limitations(synthetic: bool) -> list[str]:
    if synthetic:
        return [*_SYNTHETIC_VERSION_LIMITATIONS, *historical_bars.SYNTHETIC_OFFLINE_LIMITATIONS]
    return [*_VERSION_LIMITATIONS, *_OFFLINE_USER_LIMITATIONS]


def seal_version_manifest(document: dict[str, Any]) -> dict[str, Any]:
    """One published version's own body, with its integrity digest filled in.

    Like a plugin manifest, this proves the record is intact; it proves nothing
    about the bytes it names, which the capture and raw digests do separately. An
    out-of-band edit is detected rather than silently re-read as this version.
    """
    body = {key: value for key, value in document.items() if key != "manifest_sha256"}
    return {**body, "manifest_sha256": evidence_digest(body)}


def _manifest_is_intact(document: dict[str, Any]) -> bool:
    digest = document.get("manifest_sha256")
    return isinstance(digest, str) and digest == seal_version_manifest(document)["manifest_sha256"]


def _version_evidence(
    manifest: dict[str, Any], dataset_id: str, dataset: Phase1HistoricalDataset
) -> dict[str, Any]:
    """The bounded, canonical evidence one published upload version can prove."""
    instrument = dataset.selection.events[0].payload.instrument
    source = cast("dict[str, Any]", manifest.get("source"))
    provider_version = manifest.get("schema") == PROVIDER_VERSION_SCHEMA
    offline_version = manifest.get("schema") == OFFLINE_HISTORY_VERSION_SCHEMA
    evidence: dict[str, Any] = {
        "schema": VERSION_EVIDENCE_SCHEMA,
        "dataset_id": dataset_id,
        "origin": "upload",
        "kind": "ohlcv",
        "profile": dataset.profile,
        "version": manifest["version"],
        "published_at": manifest.get("published_at"),
        "manifest_sha256": manifest.get("manifest_sha256"),
        "capture_sha256": dataset.source_bytes_sha256.value,
        "capture_byte_count": manifest.get("capture_byte_count"),
        "data_sha256": dataset.selection.fingerprint.sha256.value,
        "record_count": dataset.selection.fingerprint.record_count,
        "source_bytes_sha256": manifest.get("source_bytes_sha256"),
        "source_byte_count": manifest.get("source_byte_count"),
        "source": {
            "declared_by": "caller" if provider_version else source.get("declared_by"),
            "asserted": False if provider_version else source.get("asserted"),
            "reference": source.get("reference"),
            "file_name": source.get("file_name"),
            "license_reference": source.get("license_reference"),
        },
        "instrument": {"venue": instrument.venue.code, "symbol": instrument.symbol},
        "availability_policy": _validated_policy(manifest.get("availability_policy")),
        "time_semantics": dict(_TIME_SEMANTICS),
    }
    if offline_version:
        # The user's own rights and intended-use assertions, and which provenance
        # the sealed response was read under, are part of what the evidence names.
        evidence["source"]["intended_use"] = source.get("intended_use")
        evidence["source"]["provenance"] = source.get("provenance")
    if manifest.get("schema") == MAPPED_VERSION_SCHEMA:
        # The declaration that produced this capture is part of what the evidence
        # names, so the digest a bound job freezes covers the mapping too.
        evidence["mapping"] = _mapping_summary(manifest)
    if provider_version or offline_version:
        # Likewise for a response capture: the query, provider, normalizer, policy
        # and page digests are part of what the bound version identity covers.
        evidence["provider"] = _provider_summary(manifest)
    return evidence


class ControlledUploadStoreV1:
    """One workspace-local store of immutable, explicitly published dataset versions.

    Every directory this store touches is opened once with ``O_NOFOLLOW`` relative
    to its already-verified parent, and every later write, read and clean-up goes
    through those descriptors rather than through a re-resolved path. A symlinked
    or replaced workspace, store root or child directory is therefore refused
    before anything is created, swept or written, and the store only ever removes
    files whose names it generated itself. This is path anchoring, not an
    operating-system sandbox: a hostile process with write access to the same
    directories is outside what this store can defend.
    """

    def __init__(self, workspace: Path, *, sweep: bool = True) -> None:
        self.root = workspace / "uploads"
        self._workspace_fd: int | None = None
        self._root_fd: int | None = None
        self._fds: dict[str, int] = {}
        try:
            self._workspace_fd = _open_workspace(workspace)
            self._root_fd = _open_child_directory(self._workspace_fd, "uploads")
            for name in _DIRECTORY_NAMES:
                self._fds[name] = _open_child_directory(self._root_fd, name)
        except BaseException:
            self.close()
            raise
        self._lock = threading.RLock()
        if sweep:
            # The owner's own store sweeps its unpublished leftovers at start-up.
            # A concurrent reader of the same store must not: another process may
            # be publishing into it right now, and its staging files are the
            # store's own in-flight work rather than an owner's leavings.
            self._sweep_orphans()

    def close(self) -> None:
        """Release the anchored directory descriptors; no file is touched."""
        descriptors = [*self._fds.values(), self._root_fd, self._workspace_fd]
        self._fds = {}
        self._root_fd = None
        self._workspace_fd = None
        for descriptor in descriptors:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)

    def __del__(self) -> None:
        with suppress(BaseException):
            self.close()

    @staticmethod
    def _names(directory: int) -> list[str]:
        try:
            return os.listdir(directory)
        except OSError:
            return []

    # -- staging helpers ----------------------------------------------------

    def _decode_staged(self, payload: bytes) -> Phase1HistoricalDataset:
        """Write one bounded staging file and run the strict decoder over it."""
        directory = self._fds["staging"]
        name = f"{uuid4().hex}.csv"
        path = self.root / "staging" / name
        try:
            _write_new(directory, name, payload)
            return read_full_capture_phase1_ohlcv_csv(path, directory_descriptor=directory)
        finally:
            with suppress(OSError):
                os.unlink(name, dir_fd=directory)

    def _sweep_orphans(self) -> None:
        """Remove only this store's own unpublished files, and nothing else.

        A file belongs to this store only when its name is one the store generates.
        Anything else in these directories was put there by somebody else and is
        left exactly where it is, as are every published version and its capture.
        The number of entries examined per sweep is bounded.
        """
        versions = set(self._versions())
        for name in ("staging", "raw", "captures"):
            directory = self._fds[name]
            for entry in sorted(self._names(directory))[:MAX_SWEEP_ENTRIES]:
                if _HOST_FILE_PATTERN.fullmatch(entry) is None:
                    continue
                if name != "staging" and entry in versions:
                    continue
                with suppress(OSError):
                    os.unlink(entry, dir_fd=directory)

    def _check_request(self, content: object) -> bytes:
        if type(content) is not bytes:
            raise UploadError("upload_invalid", "an upload must be raw bytes")
        if not content:
            raise UploadError("empty_source", "the upload is empty")
        if len(content) > MAX_UPLOAD_BYTES:
            raise UploadError(
                "source_too_large", f"the upload exceeds the {MAX_UPLOAD_BYTES}-byte limit"
            )
        return content

    def _single_instrument(self, dataset: Phase1HistoricalDataset) -> Any:
        instruments = {event.payload.instrument for event in dataset.selection.events}
        if len(instruments) != 1:
            raise UploadError(
                "invalid_identity", "an uploaded input requires exactly one instrument"
            )
        return next(iter(instruments))

    def _check_record_count(self, dataset: Phase1HistoricalDataset) -> None:
        count = dataset.selection.fingerprint.record_count
        if count > MAX_UPLOAD_RECORDS:
            raise UploadError(
                "too_many_records",
                f"the upload holds {count} records, above this store's {MAX_UPLOAD_RECORDS} limit",
            )

    # -- preview ------------------------------------------------------------

    def preview(self, content: object) -> dict[str, Any]:
        """A bounded sample and, for a small file, its verified quality summary."""
        try:
            payload = self._check_request(content)
        except UploadError as error:
            return UploadPreviewV1(
                byte_count=0,
                header=(),
                header_ok=False,
                rows=(),
                truncated=False,
                full_validation="not_attempted",
                valid=False,
                quality=None,
                first_error=error.document(),
            ).document()
        header, rows, truncated, first_error = _sample_rows(payload)
        header_ok = header == tuple(STRICT_HEADER)
        if not header_ok and first_error is None:
            first_error = UploadError(
                "invalid_header",
                "the header does not match the strict OHLCV profile",
                record_number=1,
            ).document()
        quality: dict[str, Any] | None = None
        valid = False
        full_validation = "not_attempted"
        if len(payload) <= PREVIEW_FULL_DECODE_BYTES:
            try:
                dataset = self._decode_staged(payload)
            except HistoricalMarketDataError as error:
                full_validation = "failed"
                if first_error is None:
                    first_error = _strict_failure(error).document()
            except OSError:
                full_validation = "not_attempted"
            else:
                full_validation = "passed"
                try:
                    self._check_record_count(dataset)
                except UploadError as error:
                    full_validation = "failed"
                    first_error = error.document()
                else:
                    valid = True
                    quality = _coverage(dataset)
        return UploadPreviewV1(
            byte_count=len(payload),
            header=header,
            header_ok=header_ok,
            rows=rows,
            truncated=truncated,
            full_validation=full_validation,
            valid=valid,
            quality=quality,
            first_error=first_error,
        ).document()

    # -- publication --------------------------------------------------------

    def preview_mapped(
        self,
        content: object,
        *,
        mapping: object = None,
        time_declaration: object = None,
        availability_policy: object = None,
        latency_seconds: object = None,
    ) -> dict[str, Any]:
        """A bounded preview of one explicitly mapped source; never a publication.

        The verdict is the mapper's own: it normalizes the whole source and the
        strict decoder re-reads the whole capture, so ``valid`` means the entire
        file maps. The bounded sample, the coverage evidence and the digest of the
        mapping evidence are handed out so a caller can confirm exactly what it
        would publish, and nothing is written: a preview creates no version.
        """
        try:
            payload = self._check_request(content)
            declaration = {
                "mapping": _bounded_declaration(mapping, field="mapping"),
                "time_declaration": _bounded_declaration(
                    time_declaration, field="time_declaration"
                ),
            }
            policy = _policy_document(availability_policy, latency_seconds)
            result = self._mapped_capture(
                payload, declaration["mapping"], declaration["time_declaration"], policy
            )
        except UploadError as error:
            return {
                "schema": MAPPED_PREVIEW_SCHEMA,
                "mode": "explicit-mapping",
                "valid": False,
                "first_error": error.document(),
                "byte_count": len(content) if type(content) is bytes else 0,
                "mapping_evidence": None,
                "mapping_evidence_sha256": None,
                "limitations": [*_VERSION_LIMITATIONS, *_MAPPED_LIMITATIONS],
            }
        document = result.document()
        evidence = _mapping_evidence(result, declaration)
        return {
            **document,
            "schema": MAPPED_PREVIEW_SCHEMA,
            "mode": "explicit-mapping",
            "valid": True,
            "first_error": None,
            "mapping_evidence": evidence,
            "mapping_evidence_sha256": evidence_digest(evidence),
            "limitations": [*document["limitations"], *_MAPPED_LIMITATIONS],
        }

    def _stored_bytes(self) -> int:
        total = 0
        for name in ("raw", "captures"):
            directory = self._fds[name]
            for entry in self._names(directory):
                size = _entry_size(directory, entry)
                if size is not None:
                    total += size
        return total

    def _mapped_capture(
        self,
        payload: bytes,
        mapping: Any,
        time_declaration: Any,
        policy: dict[str, Any],
    ) -> MappedCsvCaptureV1:
        """Run the one explicit mapping over the arriving bytes.

        ``ea.web.csv_mapping`` imports this module, so the mapper is imported here
        rather than at module scope and the dependency keeps one direction. The
        policy the store already validated is what the mapper is given, so the
        capture's own ``available_at`` is derived under the same closed policy the
        version records.
        """
        from ea.web.csv_mapping import map_csv_capture

        result = map_csv_capture(
            payload,
            mapping=mapping,
            time_declaration=time_declaration,
            availability_policy=policy["id"],
            latency_seconds=policy["latency_seconds"],
        )
        if result.record_count > MAX_UPLOAD_RECORDS:
            raise UploadError(
                "too_many_records",
                f"the mapped capture holds {result.record_count} records, above this store's "
                f"{MAX_UPLOAD_RECORDS} limit",
            )
        if len(result.strict_csv) > MAX_UPLOAD_BYTES:
            raise UploadError(
                "source_too_large", "the mapped capture is larger than this store accepts"
            )
        return result

    def publish(
        self,
        content: object,
        *,
        file_name: object = None,
        source_reference: object = None,
        license_reference: object = None,
        availability_policy: object = None,
        latency_seconds: object = None,
        mapping: object = None,
        time_declaration: object = None,
    ) -> dict[str, Any]:
        """Verify one upload end to end and publish it as one immutable version.

        ``availability_policy`` has no default: the caller states how the capture
        became available, or the upload is refused. ``mapping`` and
        ``time_declaration`` are both absent for a strict-profile upload and both
        present for an explicitly mapped one; one without the other is refused
        rather than guessed at.
        """
        with self._lock:
            payload = self._check_request(content)
            name = _bounded_file_name(file_name)
            source = _bounded_reference(source_reference, field="source_reference")
            licence = _bounded_reference(license_reference, field="license_reference")
            policy = _policy_document(availability_policy, latency_seconds)
            if (mapping is None) != (time_declaration is None):
                raise UploadError(
                    "mapping_declaration",
                    "an explicit mapping needs both 'mapping' and 'time_declaration'",
                )
            declaration = {
                "mapping": _bounded_declaration(mapping, field="mapping"),
                "time_declaration": _bounded_declaration(
                    time_declaration, field="time_declaration"
                ),
            }
            if len(self._versions()) >= MAX_PUBLISHED_VERSIONS:
                raise UploadError(
                    "upload_quota", f"the store holds the {MAX_PUBLISHED_VERSIONS}-version limit"
                )
            result: MappedCsvCaptureV1 | None = None
            if mapping is not None:
                result = self._mapped_capture(
                    payload, declaration["mapping"], declaration["time_declaration"], policy
                )
                capture = result.strict_csv
                capture_sha256 = result.capture_sha256
                capture_byte_count = result.capture_byte_count
                data_sha256 = result.data_sha256
                record_count = result.record_count
                venue, symbol = result.venue, result.symbol
                quality = result.quality
            else:
                try:
                    decoded = self._decode_staged(payload)
                except HistoricalMarketDataError as error:
                    raise _strict_failure(error) from None
                self._single_instrument(decoded)
                self._check_record_count(decoded)
                if policy["latency_seconds"] is None:
                    capture, redecoded = payload, decoded
                else:
                    capture = _rewrite_available_at(payload, policy["latency_seconds"])
                    if len(capture) > MAX_UPLOAD_BYTES:
                        raise UploadError(
                            "source_too_large",
                            "the simulated capture is larger than this store accepts",
                        )
                    try:
                        redecoded = self._decode_staged(capture)
                    except HistoricalMarketDataError as error:
                        raise _strict_failure(error) from None
                    self._check_record_count(redecoded)
                instrument = self._single_instrument(redecoded)
                venue, symbol = instrument.venue.code, instrument.symbol
                capture_sha256 = sha256(capture).hexdigest()
                capture_byte_count = len(capture)
                data_sha256 = redecoded.selection.fingerprint.sha256.value
                record_count = redecoded.selection.fingerprint.record_count
                quality = _coverage(redecoded)
            # The quota counts the converted capture, not only the raw bytes that
            # arrived: the simulated policy materializes a second, larger file, and
            # so does an explicit mapping.
            if self._stored_bytes() + len(payload) + len(capture) > MAX_STORED_BYTES:
                raise UploadError("upload_quota", "the upload store has reached its size limit")
            dataset_id = f"{uuid4().hex}.csv"
            raw_directory = self._fds["raw"]
            capture_directory = self._fds["captures"]
            versions_directory = self._fds["versions"]
            manifest: dict[str, Any] = {
                "schema": MAPPED_VERSION_SCHEMA if result is not None else UPLOAD_VERSION_SCHEMA,
                "dataset_id": dataset_id,
                "kind": "ohlcv",
                "profile": PHASE1_OHLCV_PROFILE,
                "version": 1,
                "published_at": datetime.now(UTC).strftime(_DATETIME_FORMAT),
                "source": {
                    "declared_by": "user",
                    "asserted": True,
                    "reference": source,
                    "file_name": name,
                    "license_reference": licence,
                },
                "availability_policy": policy,
                "source_bytes_sha256": sha256(payload).hexdigest(),
                "source_byte_count": len(payload),
                "raw_retained": True,
                "capture_sha256": capture_sha256,
                "capture_byte_count": capture_byte_count,
                "data_sha256": data_sha256,
                "record_count": record_count,
                "instrument": {"venue": venue, "symbol": symbol},
                "time_semantics": dict(_TIME_SEMANTICS),
                "quality": quality,
                "limitations": list(_POLICY_LIMITATIONS),
            }
            if result is not None:
                evidence = _mapping_evidence(result, declaration)
                manifest["mapping_evidence"] = evidence
                manifest["mapping_evidence_sha256"] = evidence_digest(evidence)
                manifest["limitations"] = [*_VERSION_LIMITATIONS, *_MAPPED_LIMITATIONS]
            manifest = seal_version_manifest(manifest)
            if len(_canonical_json(manifest)) > (
                MAX_MAPPED_MANIFEST_BYTES if result is not None else MAX_MANIFEST_BYTES
            ):
                # A version whose own manifest cannot be read back is not
                # publishable at all, so the bound is enforced before any write. A
                # mapped manifest is bounded more tightly because the same document
                # also travels inside a job record, which has its own read bound.
                raise UploadError(
                    "mapping_evidence",
                    "the mapping evidence is larger than one version manifest can carry",
                )
            try:
                _write_new(raw_directory, dataset_id, payload)
                if capture is payload:
                    os.link(
                        dataset_id,
                        dataset_id,
                        src_dir_fd=raw_directory,
                        dst_dir_fd=capture_directory,
                    )
                else:
                    _write_new(capture_directory, dataset_id, capture)
                pending = f".version-{uuid4().hex}"
                descriptor = os.open(
                    pending,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=versions_directory,
                )
                try:
                    with os.fdopen(descriptor, "wb", closefd=True) as handle:
                        handle.write(_canonical_json(manifest))
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(
                        pending,
                        f"{dataset_id}.json",
                        src_dir_fd=versions_directory,
                        dst_dir_fd=versions_directory,
                    )
                    os.fsync(versions_directory)
                finally:
                    with suppress(OSError):
                        os.unlink(pending, dir_fd=versions_directory)
            except OSError:
                for directory, entry in (
                    (versions_directory, f"{dataset_id}.json"),
                    (capture_directory, dataset_id),
                    (raw_directory, dataset_id),
                ):
                    with suppress(OSError):
                        os.unlink(entry, dir_fd=directory)
                raise UploadError(
                    "upload_unavailable", "the version could not be published"
                ) from None
            return manifest

    def publish_historical_bars(
        self,
        capture: object,
        *,
        file_name: object = None,
        source_reference: object = None,
    ) -> dict[str, Any]:
        """Publish one provider-normalized capture as one immutable version.

        The raw source retained for such a version is the adapter's own response
        envelope — the exact page bytes beside the exact query, provider,
        normalizer, policy and digests — and the capture is the strict CSV those
        bytes normalize to. Both are re-derived here before anything is written, so
        a capture that its own envelope does not reproduce is refused rather than
        published, and a read re-derives it again. No licence is recorded: this host
        holds no grant for the provider and fabricates none.
        """
        with self._lock:
            if type(capture) is not historical_bars.HistoricalBarCaptureV1:
                raise UploadError(
                    "provider_capture",
                    "a provider version is published from one HistoricalBarCaptureV1",
                )
            record = capture
            payload = record.envelope_json.encode("utf-8")
            capture_bytes = record.strict_csv
            if len(payload) > MAX_UPLOAD_BYTES:
                raise UploadError(
                    "source_too_large",
                    "the response envelope is larger than this store accepts",
                )
            if len(capture_bytes) > MAX_UPLOAD_BYTES:
                raise UploadError(
                    "source_too_large",
                    "the normalized capture is larger than this store accepts",
                )
            try:
                rederived = historical_bars.rederive_strict_capture(
                    payload, record.provider_evidence
                )
            except historical_bars.HistoricalBarError as error:
                raise UploadError(
                    "provider_capture",
                    f"the capture is not what its own response envelope reproduces "
                    f"({error.code.value})",
                ) from None
            if rederived != capture_bytes or sha256(rederived).hexdigest() != record.capture_sha256:
                raise UploadError(
                    "provider_capture",
                    "the capture is not what its own response envelope reproduces",
                )
            try:
                decoded = self._decode_staged(capture_bytes)
            except HistoricalMarketDataError as error:
                raise _strict_failure(error) from None
            self._single_instrument(decoded)
            self._check_record_count(decoded)
            latency = cast("dict[str, Any]", record.provider_evidence["declaration"])["query"][
                "latency_seconds"
            ]
            policy = _policy_document(AVAILABILITY_SIMULATED, latency)
            if len(self._versions()) >= MAX_PUBLISHED_VERSIONS:
                raise UploadError(
                    "upload_quota", f"the store holds the {MAX_PUBLISHED_VERSIONS}-version limit"
                )
            if self._stored_bytes() + len(payload) + len(capture_bytes) > MAX_STORED_BYTES:
                raise UploadError("upload_quota", "the upload store has reached its size limit")
            dataset_id = f"{uuid4().hex}.csv"
            raw_directory = self._fds["raw"]
            capture_directory = self._fds["captures"]
            versions_directory = self._fds["versions"]
            manifest: dict[str, Any] = {
                "schema": PROVIDER_VERSION_SCHEMA,
                "dataset_id": dataset_id,
                "kind": "ohlcv",
                "profile": PHASE1_OHLCV_PROFILE,
                "version": 1,
                "published_at": datetime.now(UTC).strftime(_DATETIME_FORMAT),
                "source": {
                    "declared_by": "caller",
                    "asserted": False,
                    "reference": _bounded_reference(source_reference, field="source_reference"),
                    "file_name": _bounded_file_name(file_name),
                    # Never fabricated: this host was granted nothing for the provider.
                    "license_reference": None,
                },
                "availability_policy": policy,
                "source_bytes_sha256": sha256(payload).hexdigest(),
                "source_byte_count": len(payload),
                "raw_retained": True,
                "capture_sha256": sha256(capture_bytes).hexdigest(),
                "capture_byte_count": len(capture_bytes),
                "data_sha256": decoded.selection.fingerprint.sha256.value,
                "record_count": decoded.selection.fingerprint.record_count,
                "instrument": {"venue": record.venue, "symbol": record.symbol},
                "time_semantics": dict(_TIME_SEMANTICS),
                "quality": _coverage(decoded),
                "limitations": [*_VERSION_LIMITATIONS, *_PROVIDER_LIMITATIONS],
            }
            evidence = record.provider_evidence
            manifest["provider_evidence"] = evidence
            manifest["provider_evidence_sha256"] = evidence_digest(evidence)
            manifest = seal_version_manifest(manifest)
            if len(_canonical_json(manifest)) > MAX_PROVIDER_MANIFEST_BYTES:
                raise UploadError(
                    "provider_evidence",
                    "the provider evidence is larger than one version manifest can carry",
                )
            try:
                _write_new(raw_directory, dataset_id, payload)
                _write_new(capture_directory, dataset_id, capture_bytes)
                pending = f".version-{uuid4().hex}"
                descriptor = os.open(
                    pending,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=versions_directory,
                )
                try:
                    with os.fdopen(descriptor, "wb", closefd=True) as handle:
                        handle.write(_canonical_json(manifest))
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(
                        pending,
                        f"{dataset_id}.json",
                        src_dir_fd=versions_directory,
                        dst_dir_fd=versions_directory,
                    )
                    os.fsync(versions_directory)
                finally:
                    with suppress(OSError):
                        os.unlink(pending, dir_fd=versions_directory)
            except OSError:
                for directory, entry in (
                    (versions_directory, f"{dataset_id}.json"),
                    (capture_directory, dataset_id),
                    (raw_directory, dataset_id),
                ):
                    with suppress(OSError):
                        os.unlink(entry, dir_fd=directory)
                raise UploadError(
                    "upload_unavailable", "the version could not be published"
                ) from None
            return manifest

    # -- offline pasted response --------------------------------------------

    def preview_offline_history(
        self,
        content: object,
        *,
        venue: object = None,
        symbol: object = None,
        interval: object = None,
        start_ms: object = None,
        end_ms: object = None,
        latency_seconds: object = None,
        synthetic: object = None,
        declared_source: object = None,
        declared_rights: object = None,
        intended_use: object = None,
    ) -> dict[str, Any]:
        """Read one pasted response offline and report what a publication would seal.

        The adapter's own normalizer is the only authority: the whole pasted
        response is normalized and the strict decoder re-reads the whole capture,
        so ``valid`` means the entire response is usable. Nothing is written, and
        the identity a publication must bind is handed out with the verdict.
        """
        try:
            payload = self._check_request(content)
            query = _offline_query(
                venue=venue,
                symbol=symbol,
                interval=interval,
                start_ms=start_ms,
                end_ms=end_ms,
                latency_seconds=latency_seconds,
            )
            declaration = _offline_declaration(
                synthetic=synthetic,
                declared_source=declared_source,
                declared_rights=declared_rights,
                intended_use=intended_use,
            )
            observed = datetime.now(UTC)
            capture = historical_bars.capture_offline_response(
                payload,
                query=query,
                observed_now=observed,
                ingested_at=datetime.now(UTC),
                synthetic=declaration["synthetic"],
            )
            decoded = self._decode_staged(capture.strict_csv)
            self._single_instrument(decoded)
            self._check_record_count(decoded)
            # The same policy a publication would seal, resolved here so that a
            # preview names exactly the availability the version would record.
            policy = _policy_document(AVAILABILITY_SIMULATED, query.latency_seconds)
        except UploadError as error:
            return self._offline_preview_error(error, content)
        except historical_bars.HistoricalBarError as error:
            return self._offline_preview_error(_offline_failure(error), content)
        declaration_document = cast("dict[str, Any]", capture.provider_evidence["declaration"])
        _, rows, truncated_bytes, sample_error = _sample_rows(capture.strict_csv)
        return {
            "schema": OFFLINE_HISTORY_PREVIEW_SCHEMA,
            "mode": "offline-history",
            "valid": True,
            "first_error": None,
            "synthetic": declaration["synthetic"],
            "provenance": cast("dict[str, Any]", declaration_document["transport"])["kind"],
            "provider": capture.provider_evidence["provider"],
            "normalizer": capture.provider_evidence["normalizer"],
            "byte_count": len(payload),
            "response_sha256": sha256(payload).hexdigest(),
            "envelope_sha256": sha256(capture.envelope_json.encode("utf-8")).hexdigest(),
            "capture": {
                "sha256": capture.capture_sha256,
                "byte_count": capture.capture_byte_count,
                "data_sha256": capture.data_sha256,
                "record_count": capture.record_count,
            },
            "instrument": {"venue": capture.venue, "symbol": capture.symbol},
            "query": declaration_document["query"],
            "observed_now_utc": declaration_document["observed_now_utc"],
            "ingested_at_utc": declaration_document["ingested_at_utc"],
            "availability_policy": policy,
            "quality": _coverage(decoded),
            "preview": {
                "rows": list(rows),
                "row_count_sampled": len(rows),
                "truncated": bool(truncated_bytes) or capture.record_count > len(rows),
                "valid": sample_error is None,
            },
            "declarations": {
                "source": declaration["declared_source"],
                "rights": declaration["declared_rights"],
                "intended_use": declaration["intended_use"],
            },
            "preview_sha256": _offline_identity(payload, query, **declaration),
            "limitations": _offline_limitations(declaration["synthetic"]),
        }

    def _offline_preview_error(self, error: UploadError, content: object) -> dict[str, Any]:
        """One refused offline preview: the refusal itself, and nothing else."""
        return {
            "schema": OFFLINE_HISTORY_PREVIEW_SCHEMA,
            "mode": "offline-history",
            "valid": False,
            "first_error": error.document(),
            "synthetic": None,
            "byte_count": len(content) if type(content) is bytes else 0,
            "preview_sha256": None,
            "limitations": _offline_limitations(False),
        }

    def publish_offline_history(
        self,
        content: object,
        *,
        venue: object = None,
        symbol: object = None,
        interval: object = None,
        start_ms: object = None,
        end_ms: object = None,
        latency_seconds: object = None,
        synthetic: object = None,
        declared_source: object = None,
        declared_rights: object = None,
        intended_use: object = None,
        preview_sha256: object = None,
    ) -> dict[str, Any]:
        """Publish one pasted response as one immutable, re-derivable version.

        The published request must name the exact preview identity this host
        already verified: the identity is recomputed here from the request's own
        bytes and declarations and a mismatch is refused, so no mutable staging
        record is trusted and no edited declaration can ride on an old preview.
        The retained raw source is the adapter's response envelope, which keeps the
        pasted bytes exactly, and a read normalizes them again under the sealed
        query, provenance and policy.
        """
        with self._lock:
            payload = self._check_request(content)
            try:
                query = _offline_query(
                    venue=venue,
                    symbol=symbol,
                    interval=interval,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    latency_seconds=latency_seconds,
                )
            except historical_bars.HistoricalBarError as error:
                raise _offline_failure(error) from None
            declaration = _offline_declaration(
                synthetic=synthetic,
                declared_source=declared_source,
                declared_rights=declared_rights,
                intended_use=intended_use,
            )
            identity = _offline_identity(payload, query, **declaration)
            if preview_sha256 != identity:
                raise UploadError(
                    "preview_mismatch",
                    "this is not the exact preview this host verified; preview these bytes and "
                    "declarations again and publish from that preview",
                )
            # One preview identity is one immutable version. A repeated
            # publication of the same identity — a retried request, a panel
            # reopened while an earlier publication was still settling — returns
            # the version already sealed for it instead of minting a second one.
            replay = self._published_offline_identity(identity)
            if replay is not None:
                return replay
            try:
                capture = historical_bars.capture_offline_response(
                    payload,
                    query=query,
                    observed_now=datetime.now(UTC),
                    ingested_at=datetime.now(UTC),
                    synthetic=declaration["synthetic"],
                )
            except historical_bars.HistoricalBarError as error:
                raise _offline_failure(error) from None
            raw = capture.envelope_json.encode("utf-8")
            try:
                rederived = historical_bars.rederive_strict_capture(raw, capture.provider_evidence)
            except historical_bars.HistoricalBarError as error:
                raise UploadError(
                    "provider_capture",
                    "the capture is not what its own retained response reproduces "
                    f"({error.code.value})",
                ) from None
            if rederived != capture.strict_csv:
                raise UploadError(
                    "provider_capture",
                    "the capture is not what its own retained response reproduces",
                )
            try:
                decoded = self._decode_staged(capture.strict_csv)
            except HistoricalMarketDataError as error:
                raise _strict_failure(error) from None
            self._single_instrument(decoded)
            self._check_record_count(decoded)
            policy = _policy_document(AVAILABILITY_SIMULATED, query.latency_seconds)
            if len(self._versions()) >= MAX_PUBLISHED_VERSIONS:
                raise UploadError(
                    "upload_quota", f"the store holds the {MAX_PUBLISHED_VERSIONS}-version limit"
                )
            if self._stored_bytes() + len(raw) + capture.capture_byte_count > MAX_STORED_BYTES:
                raise UploadError("upload_quota", "the upload store has reached its size limit")
            kind = cast(
                "dict[str, Any]",
                cast("dict[str, Any]", capture.provider_evidence["declaration"])["transport"],
            )["kind"]
            declared_by, asserted = OFFLINE_DECLARED_BY[cast("str", kind)]
            dataset_id = f"{uuid4().hex}.csv"
            raw_directory = self._fds["raw"]
            capture_directory = self._fds["captures"]
            versions_directory = self._fds["versions"]
            manifest: dict[str, Any] = {
                "schema": OFFLINE_HISTORY_VERSION_SCHEMA,
                "dataset_id": dataset_id,
                "kind": "ohlcv",
                "profile": PHASE1_OHLCV_PROFILE,
                "version": 1,
                "published_at": datetime.now(UTC).strftime(_DATETIME_FORMAT),
                "source": {
                    "declared_by": declared_by,
                    "asserted": asserted,
                    "reference": declaration["declared_source"],
                    "file_name": None,
                    "license_reference": declaration["declared_rights"],
                    "intended_use": declaration["intended_use"],
                    "provenance": kind,
                },
                "availability_policy": policy,
                "source_bytes_sha256": sha256(raw).hexdigest(),
                "source_byte_count": len(raw),
                "raw_retained": True,
                "capture_sha256": capture.capture_sha256,
                "capture_byte_count": capture.capture_byte_count,
                "data_sha256": decoded.selection.fingerprint.sha256.value,
                "record_count": decoded.selection.fingerprint.record_count,
                "instrument": {"venue": capture.venue, "symbol": capture.symbol},
                "time_semantics": dict(_TIME_SEMANTICS),
                "quality": _coverage(decoded),
                "limitations": _offline_limitations(declaration["synthetic"]),
            }
            # The exact preview identity this version seals, persisted with it so
            # that a repeat of the same publication is recognised as the same
            # version rather than as a new one.
            manifest["preview_sha256"] = identity
            manifest["provider_evidence"] = capture.provider_evidence
            manifest["provider_evidence_sha256"] = evidence_digest(capture.provider_evidence)
            manifest = seal_version_manifest(manifest)
            if len(_canonical_json(manifest)) > MAX_PROVIDER_MANIFEST_BYTES:
                raise UploadError(
                    "provider_evidence",
                    "the response evidence is larger than one version manifest can carry",
                )
            try:
                _write_new(raw_directory, dataset_id, raw)
                _write_new(capture_directory, dataset_id, capture.strict_csv)
                pending = f".version-{uuid4().hex}"
                descriptor = os.open(
                    pending,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=versions_directory,
                )
                try:
                    with os.fdopen(descriptor, "wb", closefd=True) as handle:
                        handle.write(_canonical_json(manifest))
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(
                        pending,
                        f"{dataset_id}.json",
                        src_dir_fd=versions_directory,
                        dst_dir_fd=versions_directory,
                    )
                    os.fsync(versions_directory)
                finally:
                    with suppress(OSError):
                        os.unlink(pending, dir_fd=versions_directory)
            except OSError:
                for directory, entry in (
                    (versions_directory, f"{dataset_id}.json"),
                    (capture_directory, dataset_id),
                    (raw_directory, dataset_id),
                ):
                    with suppress(OSError):
                        os.unlink(entry, dir_fd=directory)
                raise UploadError(
                    "upload_unavailable", "the version could not be published"
                ) from None
            return manifest

    # -- reading ------------------------------------------------------------

    def _published_offline_identity(self, identity: str) -> dict[str, Any] | None:
        """The version already sealed for one offline preview identity, if any.

        A version is only returned when it still loads: an entry that no longer
        reproduces its own retained response is not a version this identity can
        be answered with, and the caller then publishes afresh.
        """
        for dataset_id in self._versions():
            manifest = self.manifest(dataset_id)
            if (
                manifest is None
                or manifest.get("schema") != OFFLINE_HISTORY_VERSION_SCHEMA
                or manifest.get("preview_sha256") != identity
            ):
                continue
            try:
                self.load(dataset_id)
            except (HistoricalMarketDataError, OSError, ValueError):
                continue
            return manifest
        return None

    def _versions(self) -> list[str]:
        directory = self._fds["versions"]
        names: list[str] = []
        for entry in self._names(directory):
            if not entry.endswith(".json"):
                continue
            dataset_id = entry[: -len(".json")]
            if re.fullmatch(DATASET_ID_PATTERN, dataset_id) is None:
                continue
            if _entry_size(directory, entry) is not None:
                names.append(dataset_id)
        return sorted(names)

    def has(self, dataset_id: object) -> bool:
        if type(dataset_id) is not str or re.fullmatch(DATASET_ID_PATTERN, dataset_id) is None:
            return False
        return _entry_size(self._fds["versions"], f"{dataset_id}.json") is not None

    def manifest(self, dataset_id: object) -> dict[str, Any] | None:
        if type(dataset_id) is not str or re.fullmatch(DATASET_ID_PATTERN, dataset_id) is None:
            return None
        payload = _read_bounded(self._fds["versions"], f"{dataset_id}.json", MAX_MANIFEST_BYTES)
        if payload is None:
            return None
        try:
            document = json.loads(payload)
        except ValueError:
            return None
        if (
            type(document) is not dict
            or document.get("schema")
            not in {
                UPLOAD_VERSION_SCHEMA,
                MAPPED_VERSION_SCHEMA,
                PROVIDER_VERSION_SCHEMA,
                OFFLINE_HISTORY_VERSION_SCHEMA,
            }
            or document.get("dataset_id") != dataset_id
            or document.get("kind") != "ohlcv"
            or document.get("profile") != PHASE1_OHLCV_PROFILE
            or type(document.get("version")) is not int
            or document.get("raw_retained") is not True
            or not _manifest_is_intact(document)
        ):
            return None
        if document["schema"] == MAPPED_VERSION_SCHEMA and (
            type(document.get("mapping_evidence")) is not dict
            or type(document.get("mapping_evidence_sha256")) is not str
        ):
            return None
        if document["schema"] in {PROVIDER_VERSION_SCHEMA, OFFLINE_HISTORY_VERSION_SCHEMA} and (
            type(document.get("provider_evidence")) is not dict
            or type(document.get("provider_evidence_sha256")) is not str
        ):
            return None
        return cast("dict[str, Any]", document)

    def load(self, dataset_id: str) -> tuple[Path, Phase1HistoricalDataset]:
        """Re-read one published version and prove it still matches its own manifest.

        Every claim the manifest makes about the bytes is checked here — the capture
        digest, its size, the economic fingerprint, the record count, the retained
        raw source, the closed availability policy and the uploader-asserted
        provenance — and, for a mapped version, the mapper is run again over the
        retained raw source and the sealed declaration. A manifest edited after
        publication fails here, before any job could be dispatched against it.
        """
        manifest = self.manifest(dataset_id)
        if manifest is None:
            raise ValueError("uploaded dataset version is unavailable")
        directory = self._fds["captures"]
        path = self.root / "captures" / dataset_id
        dataset = read_full_capture_phase1_ohlcv_csv(path, directory_descriptor=directory)
        if dataset.profile != manifest["profile"]:
            raise ValueError("uploaded dataset capture no longer matches its published version")
        if dataset.source_bytes_sha256.value != manifest.get("capture_sha256"):
            raise ValueError("uploaded dataset capture no longer matches its published version")
        if _entry_size(directory, dataset_id) != manifest.get("capture_byte_count"):
            raise ValueError("uploaded dataset capture no longer matches its published version")
        fingerprint = dataset.selection.fingerprint
        if fingerprint.sha256.value != manifest.get("data_sha256"):
            raise ValueError("uploaded dataset capture no longer matches its published version")
        if fingerprint.record_count != manifest.get("record_count"):
            raise ValueError("uploaded dataset capture no longer matches its published version")
        raw = self._validated_raw(dataset_id, manifest)
        _validated_policy(manifest.get("availability_policy"))
        if manifest["schema"] == MAPPED_VERSION_SCHEMA:
            self._validated_mapping(raw, manifest)
        if manifest["schema"] in {PROVIDER_VERSION_SCHEMA, OFFLINE_HISTORY_VERSION_SCHEMA}:
            self._validated_provider(raw, manifest)
        if manifest.get("time_semantics") != _TIME_SEMANTICS:
            raise ValueError("uploaded dataset version declares unsupported time semantics")
        source = manifest.get("source")
        if manifest["schema"] == OFFLINE_HISTORY_VERSION_SCHEMA:
            # The provenance the version records must be the provenance its own
            # sealed response evidence declares, and the declared-by/asserted pair
            # must be the one that provenance implies. An out-of-band edit that
            # relabels a user's paste as the host's fixture (or the reverse) is a
            # different statement from the sealed one and is refused here.
            declaration, _ = _provider_declaration(manifest)
            kind = cast("dict[str, Any]", declaration.get("transport")).get("kind")
            if kind not in OFFLINE_DECLARED_BY:
                raise ValueError(
                    "uploaded dataset version does not declare a known offline provenance"
                )
            expected_provenance = OFFLINE_DECLARED_BY[cast("str", kind)]
            if (
                type(source) is not dict
                or source.get("provenance") != kind
                or (source.get("declared_by"), source.get("asserted")) != expected_provenance
            ):
                raise ValueError(
                    "uploaded dataset version is not recorded under the provenance its response "
                    "evidence declares"
                )
        else:
            expected_provenance = (
                ("caller", False)
                if manifest["schema"] == PROVIDER_VERSION_SCHEMA
                else ("user", True)
            )
            if (
                type(source) is not dict
                or (source.get("declared_by"), source.get("asserted")) != expected_provenance
            ):
                raise ValueError(
                    "uploaded dataset version is not recorded as uploader-asserted provenance"
                )
        if len({event.payload.instrument for event in dataset.selection.events}) != 1:
            raise ValueError("uploaded input requires exactly one instrument")
        return path, dataset

    def _validated_raw(self, dataset_id: str, manifest: dict[str, Any]) -> bytes:
        """The retained raw source must still be the bytes that were published."""
        raw = _read_bounded(self._fds["raw"], dataset_id, MAX_UPLOAD_BYTES)
        if raw is None or len(raw) != manifest.get("source_byte_count"):
            raise ValueError("uploaded dataset version no longer matches its retained raw source")
        if sha256(raw).hexdigest() != manifest.get("source_bytes_sha256"):
            raise ValueError("uploaded dataset version no longer matches its retained raw source")
        return raw

    def _validated_mapping(self, raw: bytes, manifest: dict[str, Any]) -> None:
        """Re-derive the capture from the retained raw source and the sealed declaration.

        The mapper is the only authority on what a declaration means, so a read runs
        it again instead of trusting the capture that is on disk. An edited
        declaration, an edited raw source or a capture that is not what the
        declaration produces cannot agree with the published digests, and the
        version is refused rather than re-read as if it were intact.
        """
        from ea.web.csv_mapping import map_csv_capture

        declaration = _mapping_declaration(manifest)
        policy = _validated_policy(manifest.get("availability_policy"))
        try:
            rederived = map_csv_capture(
                raw,
                mapping=declaration["mapping"],
                time_declaration=declaration["time_declaration"],
                availability_policy=policy["id"],
                latency_seconds=policy["latency_seconds"],
            )
        except UploadError as error:
            raise ValueError(
                f"uploaded dataset version no longer reproduces its declared mapping ({error.code})"
            ) from None
        recorded = cast(
            "dict[str, Any]", cast("dict[str, Any]", manifest["mapping_evidence"])["result"]
        )
        document = rederived.document()
        for key in ("source", "capture", "mapping", "time", "availability_policy"):
            if document[key] != recorded.get(key):
                raise ValueError(
                    "uploaded dataset version mapping evidence no longer reproduces its capture"
                )
        if (
            rederived.capture_sha256 != manifest.get("capture_sha256")
            or rederived.capture_byte_count != manifest.get("capture_byte_count")
            or rederived.data_sha256 != manifest.get("data_sha256")
            or rederived.record_count != manifest.get("record_count")
            or rederived.source_bytes_sha256 != manifest.get("source_bytes_sha256")
        ):
            raise ValueError("uploaded dataset version no longer reproduces its declared mapping")

    def _validated_provider(self, raw: bytes, manifest: dict[str, Any]) -> None:
        """Re-derive the capture from the retained envelope and the sealed evidence.

        The adapter's normalizer is the only authority on what a response envelope
        means, so a read runs it again over the retained page bytes under the sealed
        query, provider, normalizer and policy. An edited envelope, an edited
        declaration or a capture that is not what those page bytes normalize to
        cannot agree with the published digests, and the version is refused rather
        than re-read as if it were intact.
        """
        declaration, result = _provider_declaration(manifest)
        try:
            rederived = historical_bars.rederive_strict_capture(
                raw, cast("dict[str, Any]", manifest["provider_evidence"])
            )
        except historical_bars.HistoricalBarError as error:
            raise ValueError(
                "uploaded dataset version no longer reproduces its retained provider response "
                f"({error.code.value})"
            ) from None
        if (
            sha256(rederived).hexdigest() != manifest.get("capture_sha256")
            or len(rederived) != manifest.get("capture_byte_count")
            or result.get("capture", {}).get("data_sha256") != manifest.get("data_sha256")
            or result.get("capture", {}).get("record_count") != manifest.get("record_count")
            or result.get("instrument") != manifest.get("instrument")
            or declaration.get("query", {}).get("latency_seconds")
            != _validated_policy(manifest.get("availability_policy"))["latency_seconds"]
        ):
            raise ValueError(
                "uploaded dataset version no longer reproduces its retained provider response"
            )

    def version_evidence(self, dataset_id: str) -> dict[str, Any]:
        """One bounded, re-validated evidence document for a published upload.

        It is derived from the manifest *and* from the capture re-read through this
        store's own descriptors, so it is only ever produced for a version that
        still matches every claim its manifest makes. It carries no point-in-time
        claim: ``pit_verified`` stays false in the policy it records.
        """
        manifest = self.manifest(dataset_id)
        if manifest is None:
            raise ValueError("uploaded dataset version is unavailable")
        _, dataset = self.load(dataset_id)
        return _version_evidence(manifest, dataset_id, dataset)

    def version_evidence_digest(self, dataset_id: str) -> str:
        """The one host-validated digest an accepted job freezes for this version."""
        return evidence_digest(self.version_evidence(dataset_id))

    def inspect(self, dataset_id: str) -> dict[str, Any]:
        manifest = self.manifest(dataset_id)
        if manifest is None:
            raise ValueError("uploaded dataset version is unavailable")
        _, dataset = self.load(dataset_id)
        instrument = dataset.selection.events[0].payload.instrument
        entry: dict[str, Any] = {
            "dataset_id": dataset_id,
            "valid": True,
            "origin": "upload",
            "profile": dataset.profile,
            "source_sha256": dataset.source_bytes_sha256.value,
            "capture_sha256": manifest.get("capture_sha256"),
            "data_sha256": dataset.selection.fingerprint.sha256.value,
            "record_count": dataset.selection.fingerprint.record_count,
            "venue": instrument.venue.code,
            "symbol": instrument.symbol,
            "replay_start_utc": dataset.replay_window.start_inclusive.strftime(_DATETIME_FORMAT),
            "replay_end_utc": dataset.replay_window.end_exclusive.strftime(_DATETIME_FORMAT),
            "availability_policy": _validated_policy(manifest.get("availability_policy")),
            "version_evidence_digest": evidence_digest(
                _version_evidence(manifest, dataset_id, dataset)
            ),
            "version_manifest": manifest,
            **_coverage(dataset),
        }
        if manifest["schema"] == MAPPED_VERSION_SCHEMA:
            entry["mapping"] = _mapping_summary(manifest)
        if manifest["schema"] in {PROVIDER_VERSION_SCHEMA, OFFLINE_HISTORY_VERSION_SCHEMA}:
            entry["provider"] = _provider_summary(manifest)
        return entry

    def list(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for dataset_id in self._versions():
            try:
                entries.append(self.inspect(dataset_id))
            except (HistoricalMarketDataError, OSError, ValueError):
                entries.append(
                    {
                        "dataset_id": dataset_id,
                        "valid": False,
                        "origin": "upload",
                        "message": (
                            "published upload is unreadable or no longer matches its version"
                        ),
                        "error_code": "invalid_or_unavailable_input",
                    }
                )
        return entries


class LocalResearchDatasetCatalogV1:
    """One catalog over the trusted registered root and the controlled upload store."""

    def __init__(
        self,
        registered: LocalResearchDatasetRegistryV1 | None,
        uploads: ControlledUploadStoreV1,
    ) -> None:
        self._registered = registered
        self._uploads = uploads

    @property
    def uploads(self) -> ControlledUploadStoreV1:
        return self._uploads

    def has(self, dataset_id: object) -> bool:
        return self._uploads.has(dataset_id)

    def load(self, dataset_id: str) -> tuple[Path, Phase1HistoricalDataset]:
        if self._registered is not None:
            try:
                return self._registered.load(dataset_id)
            except ValueError:
                # An identifier unknown to the registered root may still be a
                # published upload. A strict validation failure of a *registered*
                # file is not retried, and no uploaded raw name can collide with
                # a registered identifier because uploads are host-named.
                if not self._uploads.has(dataset_id):
                    raise
        return self._uploads.load(dataset_id)

    def inspect(self, dataset_id: str) -> dict[str, Any]:
        if self._registered is not None:
            try:
                entry = self._registered.inspect(dataset_id)
            except (HistoricalMarketDataError, OSError, ValueError):
                if not self._uploads.has(dataset_id):
                    raise
            else:
                return {"origin": "registered", **entry}
        return self._uploads.inspect(dataset_id)

    def version_evidence(self, dataset_id: object) -> dict[str, Any] | None:
        """One version's re-validated evidence, or None for the read-only registered root."""
        if type(dataset_id) is not str or not self._uploads.has(dataset_id):
            return None
        try:
            return self._uploads.version_evidence(dataset_id)
        except (HistoricalMarketDataError, OSError, ValueError):
            return None

    def version_evidence_digest(self, dataset_id: object) -> str | None:
        """The digest an accepted job freezes; None when this version has no such evidence."""
        evidence = self.version_evidence(dataset_id)
        return None if evidence is None else evidence_digest(evidence)

    def list(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        if self._registered is not None:
            entries.extend({"origin": "registered", **entry} for entry in self._registered.list())
        entries.extend(self._uploads.list())
        return entries

    def document(self) -> dict[str, Any]:
        return {"schema": DATASET_CATALOG_SCHEMA, "datasets": self.list()}
