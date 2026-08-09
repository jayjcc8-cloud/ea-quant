"""Typed audit/output binders that fail before mismatched persistence."""

from __future__ import annotations

from typing import Protocol

from ea.core.audit import (
    AuditAppendAcknowledgement,
    AuditLogicalKey,
    AuditRecordKind,
    AuditSubjectKind,
    require_audit_acknowledgement,
)
from ea.core.run import (
    RunBinding,
    RunBindingMismatchError,
    RunReference,
    Sha256Digest,
    require_run_binding,
)
from ea.experiments.store import (
    AuditRunBinding,
    OutputCapability,
    OutputRunBinding,
)


class BoundaryBindingError(RuntimeError):
    """Raised when a bound audit/output acknowledgement is invalid."""


class RawAuditPort(Protocol):
    @property
    def binding(self) -> RunBinding: ...

    def append(
        self,
        *,
        record_kind: AuditRecordKind,
        subject_kind: AuditSubjectKind,
        subject_sha256: Sha256Digest,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement:
        """Persist and acknowledge one typed record under the adapter's binding."""
        ...


class RawOutputPort(Protocol):
    def write(
        self,
        binding: RunBinding,
        capability: OutputCapability,
        payload: bytes,
    ) -> RunBinding:
        """Persist and acknowledge one output under the supplied binding."""
        ...


def _require_payload(payload: bytes) -> None:
    if type(payload) is not bytes:
        raise BoundaryBindingError("bound payload must be exact bytes")


class BoundAuditPort:
    """Audit port pre-bound to one durable attempt and its audit capability."""

    __slots__ = ("_binding", "_raw")

    def __init__(self, prepared: AuditRunBinding, raw: RawAuditPort) -> None:
        if type(prepared) is not AuditRunBinding:
            raise BoundaryBindingError("audit port requires AuditRunBinding")
        if not callable(getattr(raw, "append", None)):
            raise BoundaryBindingError("raw audit port must provide append")
        try:
            raw_binding = raw.binding
        except AttributeError as exc:
            raise BoundaryBindingError("raw audit port must expose its binding") from exc
        if type(raw_binding) is not RunBinding or raw_binding != prepared.binding:
            raise BoundaryBindingError("raw audit port binding does not match the attempt")
        self._binding = prepared.binding
        self._raw = raw

    @property
    def reference(self) -> RunReference:
        return self._binding.reference

    @property
    def binding(self) -> RunBinding:
        return self._binding

    def append(
        self,
        *,
        record_kind: AuditRecordKind,
        subject_kind: AuditSubjectKind,
        subject_sha256: Sha256Digest,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement:
        """Precheck typed identity, persist, then validate complete acknowledgement evidence."""
        _require_payload(canonical_payload)
        logical_key = AuditLogicalKey(record_kind, subject_kind, subject_sha256)
        acknowledgement = self._raw.append(
            record_kind=record_kind,
            subject_kind=subject_kind,
            subject_sha256=subject_sha256,
            canonical_payload=canonical_payload,
        )
        try:
            return require_audit_acknowledgement(
                acknowledgement,
                binding=self._binding,
                logical_key=logical_key,
                canonical_payload=canonical_payload,
            )
        except ValueError as exc:
            raise BoundaryBindingError("audit acknowledgement binding does not match") from exc


class BoundOutputPort:
    """Result port pre-bound to one durable attempt and its output capability."""

    __slots__ = ("_binding", "_capability", "_raw")

    def __init__(self, prepared: OutputRunBinding, raw: RawOutputPort) -> None:
        if type(prepared) is not OutputRunBinding:
            raise BoundaryBindingError("output port requires OutputRunBinding")
        if not callable(getattr(raw, "write", None)):
            raise BoundaryBindingError("raw output port must provide write")
        self._binding = prepared.binding
        self._capability = prepared.capability
        self._raw = raw

    @property
    def reference(self) -> RunReference:
        return self._binding.reference

    def write(self, reference: RunReference, payload: bytes) -> None:
        """Precheck identity, persist, then validate the raw acknowledgement."""
        _require_payload(payload)
        if type(reference) is not RunReference or reference != self._binding.reference:
            raise RunBindingMismatchError("output run reference does not match the bound attempt")
        acknowledgement = self._raw.write(self._binding, self._capability, payload)
        try:
            require_run_binding(self._binding, acknowledgement)
        except ValueError as exc:
            raise BoundaryBindingError("output acknowledgement binding does not match") from exc
