"""Whole-interval Paper gate aggregation over explicit durable interval evidence.

The instant-health rule set lives in :mod:`ea.product.paper_evidence`; it judges
one observation. This module answers the gate question instead: *was the whole
requested interval clean?* It does that only from evidence a caller names
explicitly.

The contract is deliberately small and honest about what it can prove:

* ``PaperAttemptEvidence`` -- one attempt's exact identity bound to one captured
  backup directory. Only an explicit path is ever read; nothing is discovered.
* ``PaperAttemptAssociation`` -- the half-open ``[begin, end)`` interval of
  wallclock during which one attempt is claimed to be the runtime's evidence,
  together with the frozen Gate identity that attempt ran under (``None`` when
  the caller did not resolve it). Both bounds are required: an unbounded
  association proves no coverage.
* ``PaperIntervalHistory`` -- the frozen Gate identity, the bounded coverage
  window, the complete attempt inventory and their associations. The inventory
  is retained in full; it is never reduced to a newest entry.

What this cannot prove, and therefore never assumes:

* **Full Gate identity is not recoverable from durable attempt or backup
  evidence.** The identity, the inventory and the associations are caller
  claims. They are validated for internal consistency -- exact types, UTC,
  identity equality, exact bindings, frame checksums, chain links -- and a
  well-formed but incomplete or dishonest inventory cannot be detected here.
  An association whose identity the caller leaves unresolved can never be
  inferred from the run id, the backup or the clock: it is a gap when it falls
  inside the window, and it is excluded when it falls outside it.
* **Wallclock coverage is caller-asserted.** A backup's ``created_at`` is a
  creation instant, never an execution interval, and no time is ever inferred
  from it. Without explicit coverage the interval cannot be certified: it is a
  gap.
* **Integrity is not authenticity.** ``inspect_paper_backup`` proves a backup is
  internally consistent and that its journal binds to its own attempt; it is not
  a signature and is anchored nowhere outside the backup. Someone who can
  already write to both the backup root and the attempt root is explicitly
  outside this threat model.
* **Absence of a durable terminal is an interruption, not a success.** A
  captured, verified, associated attempt whose journal holds no ``RUN_TERMINAL``
  did not complete and fails the interval. Evidence that cannot be read at all
  is a gap instead, because nothing about that attempt can be established. The
  terminal's own reason decides the rest: only the deliberate stop the runtime
  records as ``stopped`` ends an interval cleanly, and only where the association
  reaches the window's close -- a deliberate stop that ends before it stops the
  gate interval inside the window. Every other recorded reason is the runtime's
  own failed ending, with or without pending work, and an unrecognized reason is
  a gap rather than a clean stop.
* **Complete coverage is taken from the caller.** An attempt whose association
  does not intersect the gate window is excluded without contaminating it, so an
  incident the caller omits from an otherwise covered window is invisible here.
  Coverage is a claim this module checks for internal consistency, never a fact
  it can discover.

Classifications are read from the durable payload schemas the runtime actually
writes (``ea.audit-paper-terminal.v1`` and ``ea.audit-run-terminal.v1``/``v2``)
and from the closed stop vocabulary the runtime can record; no success field is
invented. Nothing here grants restore, recovery or trading authority: the only
output is a deterministic verdict.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any

from ea.core.audit import (
    AUDIT_FRAME_DIGEST_DOMAIN,
    EMPTY_CHAIN_HEAD_SHA256,
    EMPTY_RECORD_SHA256,
    MAX_AUDIT_HEADER_BYTES,
    MAX_AUDIT_RECORDS,
    MAX_LARGE_AUDIT_PAYLOAD_BYTES,
    AuditContractError,
    AuditRecordKind,
    AuditSubjectKind,
    audit_chain_head,
    audit_record_digest,
    decode_audit_record,
)
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunBinding, RunContractError, RunId, RunReference, Sha256Digest
from ea.core.time import TimeValidationError, require_utc
from ea.experiments.audit import AUDIT_JOURNAL_NAME, AUDIT_JOURNAL_PREAMBLE
from ea.product.paper_backup import PaperBackupEntry, PaperBackupError, inspect_paper_backup
from ea.product.paper_evidence import (
    GateType,
    GateVerdict,
    PaperEvidenceError,
    PaperGateIdentity,
    PaperGateVerdict,
    PaperSnapshot,
    _dedup,
    _hard_rule_reasons,
    _identity_drift_reasons,
    decode_gate_identity,
    decode_paper_snapshot,
)

INTERVAL_HISTORY_SCHEMA = "ea.paper-interval-history.v1"

# The two nominal long-soak gates. A gate can only report PASS once this much
# time has actually elapsed inside its own window.
REQUIRED_DURATIONS: dict[GateType, timedelta] = {
    GateType.T72H: timedelta(hours=72),
    GateType.T7D: timedelta(days=7),
}

# Gap reason codes: evidence is absent, unusable or not demonstrably complete.
# ``GAP_EVIDENCE`` is the fail-closed answer when no interval history is given.
GAP_EVIDENCE = "evidence_gap"
GAP_COVERAGE = "coverage_gap"
GAP_MISSING_EVIDENCE = "missing_evidence"
GAP_CORRUPT_EVIDENCE = "corrupt_evidence"
GAP_AMBIGUOUS_ASSOCIATION = "ambiguous_association"
GAP_UNASSOCIATED_EVIDENCE = "unassociated_evidence"
GAP_PROVENANCE = "provenance_gap"
GAP_SNAPSHOT_RUN_NOT_REPRESENTED = "snapshot_run_not_represented"

# Certain-incident reason codes. These are never softened into a gap, and they
# survive next to whatever gaps the same interval also carries.
FAIL_ATTEMPT_FAILED = "attempt_failed"
FAIL_ATTEMPT_INTERRUPTED = "attempt_interrupted"

# The published backup layout, read-only: ``<backup>/files/<relative path>``.
_FILES_DIRECTORY_NAME = "files"
_JOURNAL_ENTRY_PATH = f"audit/{AUDIT_JOURNAL_NAME}"
_PAPER_TERMINAL_SCHEMA = "ea.audit-paper-terminal.v1"
_RUN_TERMINAL_SCHEMAS = frozenset({"ea.audit-run-terminal.v1", "ea.audit-run-terminal.v2"})
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

# The closed stop vocabulary the local Paper runtime can durably record, taken
# from ``ea.runtime.streaming.StreamStopReason`` and the capacity stop in
# ``ea.product.paper`` (``PAPER_CAPACITY_EXHAUSTED_REASON``): the runtime marks
# its own state ``stopped`` only for a deliberate operator stop and ``failed``
# for every other reason, including an exhausted audit envelope. Only the
# deliberate stop can end a gate interval cleanly; everything else the runtime
# can record stays a certain incident even when no work is left pending, and a
# reason outside this vocabulary is never read as success either.
_CLEAN_TERMINAL_REASONS = frozenset({"stop_requested"})
_FAILED_TERMINAL_REASONS = frozenset(
    {
        "source_exhausted",
        "source_stalled",
        "stale_market",
        "callback_error",
        "source_error",
        "audit_capacity_exhausted",
    }
)


def _fail(code: OutcomeCode, message: str) -> PaperEvidenceError:
    return PaperEvidenceError(code, message)


def required_duration(gate_type: GateType) -> timedelta:
    """The nominal duration a gate of this type declares."""
    if type(gate_type) is not GateType:
        raise _fail(OutcomeCode.INVALID_TYPE, "gate_type must be an exact GateType")
    return REQUIRED_DURATIONS[gate_type]


def _canonical(document: object) -> bytes:
    return json.dumps(
        document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _require_utc(value: object, *, field: str) -> datetime:
    if type(value) is not datetime:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be a datetime")
    try:
        return require_utc(value, field=field)
    except TimeValidationError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error


def _require_stamp(value: object, *, field: str) -> datetime:
    if type(value) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be a str")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, f"{field} is not a timestamp") from error
    return _require_utc(parsed, field=field)


def _require_binding(value: object, *, field: str) -> RunBinding:
    if type(value) is not RunBinding:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be an exact RunBinding")
    return value


def _require_identity(value: object, *, field: str) -> PaperGateIdentity | None:
    if value is not None and type(value) is not PaperGateIdentity:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be an exact gate identity or None")
    return value


def _require_path(value: object, *, field: str) -> Path:
    if not isinstance(value, Path):
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be one Path")
    return value


def _digest(value: object, *, field: str) -> Sha256Digest:
    if type(value) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be a str")
    try:
        return Sha256Digest(value)
    except (RunContractError, ValueError) as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} is not a SHA-256 digest") from error


def _run_id(value: object, *, field: str) -> RunId:
    if type(value) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be a str")
    try:
        return RunId(value)
    except (RunContractError, ValueError) as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} is not a canonical run id") from error


def _binding_document(binding: RunBinding) -> dict[str, str]:
    return {
        "run_id": binding.reference.run_id.value,
        "lineage_sha256": binding.reference.lineage_sha256.value,
        "manifest_sha256": binding.manifest_sha256.value,
    }


def _binding_from_document(document: object, *, field: str) -> RunBinding:
    if type(document) is not dict:
        raise _fail(OutcomeCode.INVALID_TYPE, f"{field} must be one object")
    try:
        run_id = document["run_id"]
        lineage = document["lineage_sha256"]
        manifest = document["manifest_sha256"]
    except KeyError as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, f"{field} is missing an identity field") from error
    return RunBinding(
        RunReference(
            _run_id(run_id, field=f"{field}.run_id"),
            _digest(lineage, field=f"{field}.lineage_sha256"),
        ),
        _digest(manifest, field=f"{field}.manifest_sha256"),
    )


# ---------------------------------------------------------------------------
# the explicit interval input
# ---------------------------------------------------------------------------


class AttemptEvidenceState(StrEnum):
    """How one attempt's durable evidence ends. Only ``CLEAN`` may pass."""

    CLEAN = "clean"
    FAILED = "failed"
    INCOMPLETE = "incomplete"

    @property
    def passing(self) -> bool:
        return self is AttemptEvidenceState.CLEAN


@dataclass(frozen=True, slots=True)
class PaperAttemptEvidence:
    """One attempt's exact identity bound to one explicitly named backup directory."""

    binding: RunBinding
    backup_dir: Path

    def __post_init__(self) -> None:
        _require_binding(self.binding, field="binding")
        _require_path(self.backup_dir, field="backup_dir")

    def document(self) -> dict[str, Any]:
        return {"binding": _binding_document(self.binding), "backup_dir": str(self.backup_dir)}


@dataclass(frozen=True, slots=True)
class PaperAttemptAssociation:
    """The half-open interval ``[begin, end)`` one attempt is claimed to cover.

    ``identity`` is the frozen Gate identity that attempt ran under, stated by
    the caller for this association alone. It is deliberately explicit and never
    inferred from the history's own identity, the run id, the backup or the
    clock: a full identity that differs from the gate's is definitively foreign
    evidence, and an unresolved ``None`` is unknown provenance. Both are
    excluded from the window's coverage, and an unknown association that falls
    inside the window is an explicit provenance gap.
    """

    binding: RunBinding
    begin: datetime
    end: datetime
    identity: PaperGateIdentity | None = None

    def __post_init__(self) -> None:
        _require_binding(self.binding, field="binding")
        _require_identity(self.identity, field="identity")
        if self.begin is None or self.end is None:
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                "an association must bound both ends explicitly; coverage is never assumed",
            )
        begin = _require_utc(self.begin, field="begin")
        end = _require_utc(self.end, field="end")
        if begin >= end:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "association must be a non-empty interval")

    def intersect(self, start: datetime, end: datetime) -> bool:
        """Whether this half-open association overlaps ``[start, end)`` at all."""
        return self.begin < end and self.end > start

    def covers_left(self, end: datetime) -> bool:
        """Whether this association reaches ``end`` from the left, given it is inside."""
        return self.begin < end <= self.end

    def document(self) -> dict[str, Any]:
        return {
            **_binding_document(self.binding),
            "begin": self.begin.isoformat(),
            "end": self.end.isoformat(),
            "identity": None if self.identity is None else self.identity.document(),
        }


@dataclass(frozen=True, slots=True)
class PaperIntervalObservation:
    """An actual saved observation, including identity before any later restoration."""

    snapshot: PaperSnapshot
    current_identity: PaperGateIdentity

    def __post_init__(self) -> None:
        if type(self.snapshot) is not PaperSnapshot:
            raise _fail(OutcomeCode.INVALID_TYPE, "observation requires an exact snapshot")
        if type(self.current_identity) is not PaperGateIdentity:
            raise _fail(OutcomeCode.INVALID_TYPE, "observation requires an exact identity")

    def document(self) -> dict[str, Any]:
        return {
            "snapshot": self.snapshot.document(),
            "current_identity": self.current_identity.document(),
        }


@dataclass(frozen=True, slots=True)
class PaperIntervalHistory:
    """The explicit, bounded interval evidence one gate evaluation judges.

    ``attempts`` is the complete inventory of attempts the caller asserts ran
    around ``[coverage_start, coverage_end)``; it is never filtered down to a
    newest entry. Every inventory attempt must be named by at least one
    association; an attempt with none is a gap, and an attempt with several is
    an ambiguous association and also a gap -- neither is ever resolved by
    picking one. An association may extend outside the window: an incident that
    began earlier is excluded by the window, not by rejection.
    """

    identity: PaperGateIdentity
    coverage_start: datetime
    coverage_end: datetime
    attempts: tuple[PaperAttemptEvidence, ...]
    associations: tuple[PaperAttemptAssociation, ...]
    schema: str = INTERVAL_HISTORY_SCHEMA
    observations: tuple[PaperIntervalObservation, ...] = ()

    def __post_init__(self) -> None:
        if self.schema != INTERVAL_HISTORY_SCHEMA:
            raise _fail(OutcomeCode.CONFLICTING_ID, "interval history schema conflicts")
        if type(self.identity) is not PaperGateIdentity:
            raise _fail(OutcomeCode.INVALID_TYPE, "identity must be an exact gate identity")
        coverage_start = _require_utc(self.coverage_start, field="coverage_start")
        coverage_end = _require_utc(self.coverage_end, field="coverage_end")
        if coverage_start > coverage_end:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "coverage must be an ordered interval")
        if type(self.observations) is not tuple or any(
            type(item) is not PaperIntervalObservation for item in self.observations
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "observations must be exact observations")
        if any(
            not self.identity.started_at <= item.snapshot.captured_at <= coverage_end
            for item in self.observations
        ):
            raise _fail(OutcomeCode.OUT_OF_RANGE, "observation is outside the interval")
        if type(self.attempts) is not tuple:
            raise _fail(OutcomeCode.INVALID_TYPE, "attempts must be one tuple")
        if not all(type(attempt) is PaperAttemptEvidence for attempt in self.attempts):
            raise _fail(OutcomeCode.INVALID_TYPE, "attempts must be exact attempt evidence")
        if type(self.associations) is not tuple:
            raise _fail(OutcomeCode.INVALID_TYPE, "associations must be one tuple")
        if not all(type(item) is PaperAttemptAssociation for item in self.associations):
            raise _fail(OutcomeCode.INVALID_TYPE, "associations must be exact associations")
        bindings = [attempt.binding for attempt in self.attempts]
        if len(set(bindings)) != len(bindings):
            raise _fail(OutcomeCode.CONFLICTING_ID, "attempt inventory must not repeat")
        for association in self.associations:
            if association.binding not in bindings:
                raise _fail(
                    OutcomeCode.CONFLICTING_ID,
                    "association names an attempt outside the declared inventory",
                )

    def document(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "identity": self.identity.document(),
            "coverage_start": self.coverage_start.isoformat(),
            "coverage_end": self.coverage_end.isoformat(),
            "attempts": [attempt.document() for attempt in self.attempts],
            "associations": [association.document() for association in self.associations],
            "observations": [item.document() for item in self.observations],
        }


def canonical_paper_interval_history_bytes(history: PaperIntervalHistory) -> bytes:
    """Return the canonical durable document for one explicit interval input."""
    if type(history) is not PaperIntervalHistory:
        raise _fail(OutcomeCode.INVALID_TYPE, "history must be an exact PaperIntervalHistory")
    return _canonical(history.document())


def decode_paper_interval_history(document: object) -> PaperIntervalHistory:
    """Rebuild one interval input from its durable document; malformed input fails closed."""
    if type(document) is not dict:
        raise _fail(OutcomeCode.INVALID_TYPE, "interval history must be one object")
    if document.get("schema") != INTERVAL_HISTORY_SCHEMA:
        raise _fail(OutcomeCode.CONFLICTING_ID, "interval history schema conflicts")
    raw_attempts = document.get("attempts")
    raw_associations = document.get("associations")
    raw_observations = document.get("observations", [])
    if type(raw_observations) is not list or any(
        type(item) is not dict for item in raw_observations
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "observations must be an array of objects")
    if type(raw_attempts) is not list or type(raw_associations) is not list:
        raise _fail(OutcomeCode.INVALID_TYPE, "interval history inventory must be two arrays")
    attempts: list[PaperAttemptEvidence] = []
    for item in raw_attempts:
        if type(item) is not dict or type(item.get("backup_dir")) is not str:
            raise _fail(OutcomeCode.INVALID_TYPE, "attempt evidence must name one backup path")
        attempts.append(
            PaperAttemptEvidence(
                binding=_binding_from_document(item.get("binding"), field="attempt binding"),
                backup_dir=Path(item["backup_dir"]),
            )
        )
    associations: list[PaperAttemptAssociation] = []
    for item in raw_associations:
        if type(item) is not dict:
            raise _fail(OutcomeCode.INVALID_TYPE, "association must be one object")
        raw_identity = item.get("identity")
        associations.append(
            PaperAttemptAssociation(
                binding=_binding_from_document(item, field="association binding"),
                begin=_require_stamp(item.get("begin"), field="begin"),
                end=_require_stamp(item.get("end"), field="end"),
                identity=None if raw_identity is None else decode_gate_identity(raw_identity),
            )
        )
    return PaperIntervalHistory(
        identity=decode_gate_identity(document.get("identity")),
        coverage_start=_require_stamp(document.get("coverage_start"), field="coverage_start"),
        coverage_end=_require_stamp(document.get("coverage_end"), field="coverage_end"),
        attempts=tuple(attempts),
        associations=tuple(associations),
        observations=tuple(
            PaperIntervalObservation(
                decode_paper_snapshot(item.get("snapshot")),
                decode_gate_identity(item.get("current_identity")),
            )
            for item in raw_observations
        ),
    )


# ---------------------------------------------------------------------------
# read-only evidence reading
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PaperAttemptRecord:
    """One attempt's durable evidence, read from one backup without mutation.

    ``state`` is the only classification the interval uses. ``terminal_reason``
    is the durable terminal's own label -- the Paper terminal reason or the
    coordinator terminal kind -- retained for explanation and never synthesized
    here; it is ``None`` when the attempt left no durable terminal at all.
    ``records`` is a bounded count, never the decoded frames themselves.
    """

    binding: RunBinding
    backup_dir: Path
    state: AttemptEvidenceState
    terminal_reason: str | None
    records: int

    def __post_init__(self) -> None:
        _require_binding(self.binding, field="binding")
        _require_path(self.backup_dir, field="backup_dir")
        if type(self.state) is not AttemptEvidenceState:
            raise _fail(OutcomeCode.INVALID_TYPE, "state must be an exact AttemptEvidenceState")
        if self.terminal_reason is not None and type(self.terminal_reason) is not str:
            raise _fail(OutcomeCode.INVALID_TYPE, "terminal_reason must be text or None")
        if type(self.records) is not int or self.records < 1:
            raise _fail(OutcomeCode.OUT_OF_RANGE, "records must be a positive exact int")

    @property
    def passing(self) -> bool:
        return self.state.passing


@dataclass(frozen=True, slots=True)
class _JournalSummary:
    """The bounded classification of one captured journal; no frame is retained."""

    records: int
    state: AttemptEvidenceState
    terminal_reason: str | None


def _read_exact(descriptor: int, size: int, offset: int) -> bytes:
    """Read one exact byte range, or refuse: captured evidence has no torn tail."""
    chunk = os.pread(descriptor, size, offset)
    if len(chunk) != size:
        raise _fail(OutcomeCode.CONFLICTING_ID, "captured journal is truncated")
    return chunk


def _summarise_journal(
    path: Path,
    binding: RunBinding,
    entry: PaperBackupEntry,
) -> _JournalSummary:
    """Verify one captured journal completely, in bounded memory, read-only.

    ``reopen_posix_audit_journal`` is a writer path over the attempt: it repairs
    a torn tail and re-admits the preparation record, so it can never be pointed
    at captured bytes. This reader instead walks the frames with ``pread`` and
    re-verifies, exactly as a linear rescan does, the preamble, every frame
    checksum and length bound, every sequence number, both chain links, the
    exact binding and the first record's preparation -- but it *classifies*
    rather than retains. One captured attempt may hold ``MAX_AUDIT_RECORDS``
    frames, so no decoded record, header or payload survives the frame that
    carried it: only the count, the terminal's own outcome, its reason and one
    aggregate boolean do, and judging a full legacy journal costs bounded memory
    instead of its size. The bytes are hashed as they are read and required to
    match the digest the backup recorded, so the classification is exactly of
    the bytes that were verified, and a short or torn frame can never be
    reported as partial success.
    """
    descriptor: int | None = None
    try:
        descriptor = os.open(path, _READ_FLAGS)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size != entry.size_bytes:
            raise _fail(OutcomeCode.CONFLICTING_ID, "captured journal size conflicts")
        size = info.st_size
        hasher = sha256()
        preamble = _read_exact(descriptor, len(AUDIT_JOURNAL_PREAMBLE), 0)
        if preamble != AUDIT_JOURNAL_PREAMBLE:
            raise _fail(OutcomeCode.CONFLICTING_ID, "captured journal preamble is invalid")
        hasher.update(preamble)
        offset = len(AUDIT_JOURNAL_PREAMBLE)
        count = 0
        previous_record = EMPTY_RECORD_SHA256
        previous_chain = EMPTY_CHAIN_HEAD_SHA256
        terminal_seen = False
        terminal_state: AttemptEvidenceState | None = None
        terminal_reason: str | None = None
        failing_transition = False
        while offset < size:
            raw_header_length = _read_exact(descriptor, 8, offset)
            header_length = int.from_bytes(raw_header_length, "big")
            if not 1 <= header_length <= MAX_AUDIT_HEADER_BYTES:
                raise _fail(OutcomeCode.CONFLICTING_ID, "captured journal header bound conflicts")
            offset += 8
            header = _read_exact(descriptor, header_length, offset)
            offset += header_length
            raw_payload_length = _read_exact(descriptor, 8, offset)
            payload_length = int.from_bytes(raw_payload_length, "big")
            if not 1 <= payload_length <= MAX_LARGE_AUDIT_PAYLOAD_BYTES:
                raise _fail(OutcomeCode.CONFLICTING_ID, "captured journal payload bound conflicts")
            offset += 8
            payload = _read_exact(descriptor, payload_length, offset)
            offset += payload_length
            checksum = _read_exact(descriptor, 32, offset)
            offset += 32
            framed = raw_header_length + header + raw_payload_length + payload
            if checksum != sha256(AUDIT_FRAME_DIGEST_DOMAIN + framed).digest():
                raise _fail(OutcomeCode.CONFLICTING_ID, "captured journal checksum conflicts")
            try:
                record = decode_audit_record(
                    binding=binding, canonical_header=header, canonical_payload=payload
                )
            except AuditContractError as error:
                raise _fail(OutcomeCode.CONFLICTING_ID, "captured frame conflicts") from error
            count += 1
            if (
                record.record_id.owner_sequence != count
                or count > MAX_AUDIT_RECORDS
                or record.previous_record_sha256 != previous_record
                or record.previous_chain_head_sha256 != previous_chain
                or terminal_seen
            ):
                raise _fail(OutcomeCode.CONFLICTING_ID, "captured journal chain conflicts")
            if count == 1 and (
                record.record_kind is not AuditRecordKind.RUN_PREPARED
                or record.subject_kind is not AuditSubjectKind.RUN_MANIFEST
                or record.subject_sha256 != binding.manifest_sha256
            ):
                raise _fail(
                    OutcomeCode.CONFLICTING_ID, "captured journal does not begin with preparation"
                )
            previous_record = audit_record_digest(record)
            previous_chain = audit_chain_head(record)
            terminal_seen = record.record_kind is AuditRecordKind.RUN_TERMINAL
            if terminal_seen:
                # The terminal is the last frame a journal can carry, so at most
                # one outcome is ever classified and nothing is accumulated.
                terminal_state, terminal_reason = _terminal_outcome(record.canonical_payload)
            elif record.record_kind is AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION:
                failing_transition = True
            hasher.update(framed)
            hasher.update(checksum)
        if offset != size or count == 0:
            raise _fail(OutcomeCode.CONFLICTING_ID, "captured journal is not a complete journal")
        if Sha256Digest(hasher.hexdigest()) != entry.sha256:
            raise _fail(OutcomeCode.CONFLICTING_ID, "captured journal does not match its digest")
    except OSError as error:
        raise PaperBackupError("captured journal could not be read") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not terminal_seen or terminal_state is None:
        # No durable terminal: the attempt did not complete, whatever it left.
        return _JournalSummary(count, AttemptEvidenceState.INCOMPLETE, None)
    if terminal_state is AttemptEvidenceState.CLEAN and failing_transition:
        # A recorded failing safety transition is never erased by a later terminal.
        return _JournalSummary(count, AttemptEvidenceState.FAILED, terminal_reason)
    return _JournalSummary(count, terminal_state, terminal_reason)


def _terminal_outcome(canonical_payload: bytes) -> tuple[AttemptEvidenceState, str | None]:
    """Classify one durable terminal payload by the schema and reason it declares.

    The Paper runtime writes ``ea.audit-paper-terminal.v1`` on its own close path
    and records there why the stream ended. A deliberate ``stop_requested`` is
    the runtime's ``stopped`` end; every other reason the runtime can record is
    its own ``failed`` end -- an exhausted or stalled source, stale market, a
    callback or source error, an exhausted audit envelope -- and stays a certain
    incident even when the terminal retains no pending work. A reason outside
    that closed vocabulary is not read as success either: an unrecognized reason
    is a gap, never a clean stop. The coordinator terminal states its own kind.
    Any other schema is refused instead of being read as success.
    """
    try:
        document = json.loads(canonical_payload)
    except (RecursionError, UnicodeError, ValueError) as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, "terminal payload is malformed JSON") from error
    if type(document) is not dict:
        raise _fail(OutcomeCode.CONFLICTING_ID, "terminal payload must be one object")
    schema = document.get("schema")
    if schema == _PAPER_TERMINAL_SCHEMA:
        reason = document.get("reason")
        pending_facts = document.get("pending_facts")
        pending_orders = document.get("pending_orders")
        if (
            type(reason) is not str
            or not reason
            or type(pending_facts) is not int
            or pending_facts < 0
            or type(pending_orders) is not int
            or pending_orders < 0
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "paper terminal payload conflicts")
        if pending_facts or pending_orders:
            # Work that never resolved is an operator attention item, whatever
            # the reason says.
            return AttemptEvidenceState.FAILED, reason
        if reason in _CLEAN_TERMINAL_REASONS:
            return AttemptEvidenceState.CLEAN, reason
        if reason in _FAILED_TERMINAL_REASONS:
            return AttemptEvidenceState.FAILED, reason
        raise _fail(OutcomeCode.CONFLICTING_ID, "paper terminal reason is not a known outcome")
    if schema in _RUN_TERMINAL_SCHEMAS:
        kind = document.get("terminal_kind")
        if kind == "failed":
            return AttemptEvidenceState.FAILED, kind
        if kind == "success":
            return AttemptEvidenceState.CLEAN, kind
        raise _fail(OutcomeCode.CONFLICTING_ID, "run terminal kind is not a known outcome")
    raise _fail(OutcomeCode.CONFLICTING_ID, "terminal payload schema is not a known outcome")


def read_attempt_evidence(backup_dir: Path, expected: RunBinding) -> PaperAttemptRecord:
    """Read one attempt's durable evidence from one explicitly named backup path.

    The backup is verified with the product's own ``inspect_paper_backup``, then
    the captured journal is verified and classified bounded and read-only, and
    the bytes read are bound to the digest the backup recorded. The captured
    attempt must be exactly the one ``expected`` names: a backup of some other
    attempt is ambiguous provenance, never silently re-labelled.

    A missing or unverifiable backup raises
    :class:`ea.product.paper_backup.PaperBackupError`; a verified backup whose
    journal contradicts its own identity raises
    :class:`ea.product.paper_evidence.PaperEvidenceError`.
    """
    _require_binding(expected, field="expected")
    _require_path(backup_dir, field="backup_dir")
    manifest = inspect_paper_backup(backup_dir)
    captured = RunBinding(
        RunReference(manifest.run_id, manifest.lineage_sha256),
        manifest.attempt_manifest_sha256,
    )
    if captured != expected:
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "captured attempt identity conflicts with the expected binding",
        )
    entry = next(
        (candidate for candidate in manifest.entries if candidate.path == _JOURNAL_ENTRY_PATH),
        None,
    )
    if entry is None:
        raise _fail(OutcomeCode.CONFLICTING_ID, "captured backup holds no audit journal")
    summary = _summarise_journal(
        backup_dir / _FILES_DIRECTORY_NAME / _JOURNAL_ENTRY_PATH, captured, entry
    )
    return PaperAttemptRecord(
        binding=captured,
        backup_dir=backup_dir,
        state=summary.state,
        terminal_reason=summary.terminal_reason,
        records=summary.records,
    )


# ---------------------------------------------------------------------------
# whole-interval aggregation
# ---------------------------------------------------------------------------


def _covers_window(
    associations: tuple[PaperAttemptAssociation, ...],
    start: datetime,
    end: datetime,
) -> bool:
    """Whether the half-open association union covers ``[start, end)`` entirely.

    Every association is clipped to the window by construction: only its overlap
    with ``[start, end)`` can contribute, so an association reaching far outside
    the window certifies nothing beyond the part that lies inside it.
    """
    cursor = start
    for association in sorted(associations, key=lambda item: (item.begin, item.end)):
        if association.end <= cursor:
            continue
        if association.begin > cursor:
            return False
        cursor = association.end
        if cursor >= end:
            return True
    return cursor >= end


def _interval_reasons(
    history: PaperIntervalHistory | None,
    snapshot: PaperSnapshot,
    locked: PaperGateIdentity,
    evaluated_at: datetime,
) -> tuple[list[str], list[str]]:
    """Return ``(gap_reasons, certain_incident_reasons)`` for the whole interval."""
    if type(history) is not PaperIntervalHistory:
        # No explicit interval evidence: fail closed, never a snapshot-only pass.
        return [GAP_EVIDENCE], []
    if history.identity != locked:
        # Evidence locked to another identity cannot speak for this gate window.
        return [GAP_COVERAGE], []
    gaps: list[str] = []
    failures: list[str] = []
    window_start = locked.started_at
    window_end = evaluated_at
    elapsed = window_end - window_start
    if elapsed < required_duration(locked.gate_type):
        # A window that has not run for its requested duration cannot pass, and a
        # non-positive window is not coverage at all.
        gaps.append(GAP_COVERAGE)
    if history.coverage_start > window_start or history.coverage_end < window_end:
        gaps.append(GAP_COVERAGE)
    if snapshot.captured_at != window_end:
        # The boundary observation has to be the observation of the instant the
        # window closes. Any other instant -- earlier or later -- describes a
        # different interval, so it can never certify this one.
        gaps.append(GAP_COVERAGE)

    by_binding = {attempt.binding: attempt for attempt in history.attempts}
    per_binding: dict[RunBinding, list[PaperAttemptAssociation]] = {}
    for association in history.associations:
        per_binding.setdefault(association.binding, []).append(association)

    if not history.attempts:
        gaps.append(GAP_EVIDENCE)
    elif any(binding not in per_binding for binding in by_binding):
        gaps.append(GAP_UNASSOCIATED_EVIDENCE)
    if any(len(items) > 1 for items in per_binding.values()):
        # An ambiguous association is never resolved by picking one of them.
        gaps.append(GAP_AMBIGUOUS_ASSOCIATION)

    covering: list[PaperAttemptAssociation] = []
    boundary_runs: set[RunId] = set()
    for binding, evidence in by_binding.items():
        items = per_binding.get(binding)
        if not items or len(items) > 1:
            continue
        association = items[0]
        if association.identity is None:
            if association.intersect(window_start, window_end):
                # Nobody resolved this association to the frozen identity: it is
                # unknown provenance, never inferred from the run id or the
                # backup, and it can neither cover nor contaminate the window.
                gaps.append(GAP_PROVENANCE)
            continue
        if association.identity != locked:
            # A definitively foreign identity is excluded outright -- from
            # coverage and from incidents alike -- and its evidence is never
            # read at all.
            continue
        covering.append(association)
        if not association.intersect(window_start, window_end):
            # Wholly outside this window: excluded without contaminating it.
            continue
        if association.covers_left(window_end):
            # Only an association established for this identity, unambiguously,
            # and reaching the window's end from its left may stand for the
            # boundary observation.
            boundary_runs.add(binding.reference.run_id)
        if not evidence.backup_dir.is_dir():
            gaps.append(GAP_MISSING_EVIDENCE)
            continue
        try:
            record = read_attempt_evidence(evidence.backup_dir, binding)
        except (PaperBackupError, ValueError, OSError):
            # Unreadable or contradictory evidence proves nothing in either
            # direction: it is a gap, never a pass and never an incident.
            # ``ValueError`` covers this module's own refusals and every
            # validator the product's readers raise underneath them.
            gaps.append(GAP_CORRUPT_EVIDENCE)
            continue
        if record.state is AttemptEvidenceState.FAILED:
            failures.append(FAIL_ATTEMPT_FAILED)
        elif record.state is AttemptEvidenceState.INCOMPLETE:
            failures.append(FAIL_ATTEMPT_INTERRUPTED)
        elif record.terminal_reason in _CLEAN_TERMINAL_REASONS and association.end < window_end:
            # A deliberate stop that ends before the window closes stops the
            # gate interval inside it, whatever the next attempt does.
            failures.append(FAIL_ATTEMPT_INTERRUPTED)

    if not _covers_window(tuple(covering), window_start, window_end):
        gaps.append(GAP_COVERAGE)

    # The boundary observation is checked last, and only when nothing else has
    # already decided the interval: a snapshot of a run this window's coverage
    # does not rest on can never certify an otherwise clean window, but it adds
    # no reason to a window that already carries a certain incident or a gap.
    if not gaps and not failures and snapshot.run_id not in boundary_runs:
        gaps.append(GAP_SNAPSHOT_RUN_NOT_REPRESENTED)
    return gaps, failures


def evaluate_gate(
    snapshot: PaperSnapshot,
    locked_identity: PaperGateIdentity,
    current_identity: PaperGateIdentity,
    history: PaperIntervalHistory | None = None,
    *,
    evaluated_at: datetime,
) -> PaperGateVerdict:
    """Judge the whole gate interval from explicit durable interval evidence.

    Identity drift is evaluated first and produces ``INVALID``: once the gate's
    evidentiary basis has moved, no interval can rescue it. Then the explicit
    interval history is aggregated over ``[locked.started_at, evaluated_at)``:

    * a certain incident -- a failed or interrupted attempt whose evidence is
      durable, verified and unambiguously associated with this window, or a
      deliberate stop that ends before the window closes -- produces ``FAIL``;
    * any gap (absent, uncertifiable, missing, corrupt, ambiguous or unresolved
      provenance, an incomplete inventory, a window the matching associations do
      not fully cover, a gate whose requested duration has not elapsed, an
      observation taken at an instant other than the window's close, or a
      boundary snapshot of a run this coverage does not rest on) produces
      ``INVALID``, and is retained alongside a certain incident rather than
      replacing it;
    * ``PASS`` requires all of: the requested duration elapsed, explicit coverage
      spanning the window, a complete and unambiguous inventory, a boundary
      snapshot captured exactly at the window's close whose run an established
      matching association covers the window with, and every associated attempt
      clean.

    ``history`` defaults to ``None``. Omitting it -- or passing anything that is
    not exact interval evidence -- is the explicit "no interval evidence was
    supplied" case and is a gap. One healthy instant can never stand for an
    interval, no backup is ever discovered automatically, and no newest-backup
    index is consulted.
    """
    if type(snapshot) is not PaperSnapshot:
        raise _fail(OutcomeCode.INVALID_TYPE, "snapshot must be an exact PaperSnapshot")
    if type(locked_identity) is not PaperGateIdentity:
        raise _fail(OutcomeCode.INVALID_TYPE, "locked_identity must be an exact identity")
    if type(current_identity) is not PaperGateIdentity:
        raise _fail(OutcomeCode.INVALID_TYPE, "current_identity must be an exact identity")
    evaluated_at = _require_utc(evaluated_at, field="evaluated_at")

    observations = (
        tuple(
            item
            for item in history.observations
            if locked_identity.started_at <= item.snapshot.captured_at <= evaluated_at
        )
        if type(history) is PaperIntervalHistory and history.identity == locked_identity
        else ()
    )
    drift_reasons = _identity_drift_reasons(snapshot, locked_identity, current_identity)
    for observation in observations:
        drift_reasons.extend(
            _identity_drift_reasons(
                observation.snapshot, locked_identity, observation.current_identity
            )
        )
    drift = _dedup(drift_reasons)
    if drift:
        return PaperGateVerdict(
            verdict=GateVerdict.INVALID,
            gate_id=locked_identity.gate_id,
            gate_type=locked_identity.gate_type,
            evaluated_at=evaluated_at,
            reasons=drift,
        )

    gaps, failures = _interval_reasons(history, snapshot, locked_identity, evaluated_at)
    failures.extend(_hard_rule_reasons(snapshot))
    for observation in observations:
        failures.extend(_hard_rule_reasons(observation.snapshot))
    if failures:
        return PaperGateVerdict(
            verdict=GateVerdict.FAIL,
            gate_id=locked_identity.gate_id,
            gate_type=locked_identity.gate_type,
            evaluated_at=evaluated_at,
            reasons=_dedup(failures + gaps),
        )
    if gaps:
        return PaperGateVerdict(
            verdict=GateVerdict.INVALID,
            gate_id=locked_identity.gate_id,
            gate_type=locked_identity.gate_type,
            evaluated_at=evaluated_at,
            reasons=_dedup(gaps),
        )
    return PaperGateVerdict(
        verdict=GateVerdict.PASS,
        gate_id=locked_identity.gate_id,
        gate_type=locked_identity.gate_type,
        evaluated_at=evaluated_at,
        reasons=(),
    )


__all__ = [
    "FAIL_ATTEMPT_FAILED",
    "FAIL_ATTEMPT_INTERRUPTED",
    "GAP_AMBIGUOUS_ASSOCIATION",
    "GAP_CORRUPT_EVIDENCE",
    "GAP_COVERAGE",
    "GAP_EVIDENCE",
    "GAP_MISSING_EVIDENCE",
    "GAP_PROVENANCE",
    "GAP_SNAPSHOT_RUN_NOT_REPRESENTED",
    "GAP_UNASSOCIATED_EVIDENCE",
    "INTERVAL_HISTORY_SCHEMA",
    "REQUIRED_DURATIONS",
    "AttemptEvidenceState",
    "PaperAttemptAssociation",
    "PaperAttemptEvidence",
    "PaperAttemptRecord",
    "PaperIntervalHistory",
    "PaperIntervalObservation",
    "canonical_paper_interval_history_bytes",
    "decode_paper_interval_history",
    "evaluate_gate",
    "read_attempt_evidence",
    "required_duration",
]
