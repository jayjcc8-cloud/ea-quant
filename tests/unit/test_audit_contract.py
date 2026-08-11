from __future__ import annotations

import json

import pytest

from ea.core.audit import (
    EMPTY_CHAIN_HEAD_SHA256,
    EMPTY_RECORD_SHA256,
    AuditContractError,
    AuditRecordKind,
    AuditSubjectKind,
    audit_append_acknowledgement_digest,
    audit_chain_head,
    audit_frame_checksum,
    audit_record_digest,
    canonical_audit_append_acknowledgement_bytes,
    canonical_audit_record_header_bytes,
    create_audit_append_acknowledgement,
    create_audit_record,
    ordered_digest_tuple,
    require_canonical_audit_payload,
)
from ea.core.run import RunBinding, RunId, RunReference, Sha256Digest


def _binding() -> RunBinding:
    return RunBinding(
        reference=RunReference(
            RunId("123e4567-e89b-42d3-a456-426614174000"),
            Sha256Digest("11" * 32),
        ),
        manifest_sha256=Sha256Digest("22" * 32),
    )


def _prepared_payload() -> bytes:
    return (
        b'{"canonicalization":"ea-canonical-json-v1","lineage_sha256":"'
        + b"11" * 32
        + b'","manifest_sha256":"'
        + b"22" * 32
        + b'","run_id":"123e4567-e89b-42d3-a456-426614174000",'
        b'"schema":"ea.audit-run-prepared.v1"}'
    )


def test_sequence_one_matches_accepted_adr_0020_golden_vector() -> None:
    record = create_audit_record(
        binding=_binding(),
        owner_sequence=1,
        record_kind=AuditRecordKind.RUN_PREPARED,
        subject_kind=AuditSubjectKind.RUN_MANIFEST,
        subject_sha256=Sha256Digest("22" * 32),
        canonical_payload=_prepared_payload(),
        previous_record_sha256=EMPTY_RECORD_SHA256,
        previous_chain_head_sha256=EMPTY_CHAIN_HEAD_SHA256,
    )
    acknowledgement = create_audit_append_acknowledgement(record)

    assert len(_prepared_payload()) == 296
    assert record.payload_sha256 == Sha256Digest(
        "6186ae20684529abcb8b9008e94548235cf9979f76ec72f1aab50455c2db44a1"
    )
    assert len(canonical_audit_record_header_bytes(record)) == 821
    assert audit_record_digest(record) == Sha256Digest(
        "d8a3dce4093e148449b1c07b66b99c640732adb49446135be12645b64875b1b8"
    )
    assert audit_chain_head(record) == Sha256Digest(
        "3fca3a84a1cf211e48b674197476396fc4be8184551c9a5f6fbf6f4c4200aa76"
    )
    assert audit_frame_checksum(record) == Sha256Digest(
        "578e6e89482f060c57521d2b1c22379004c7d8125d3d38480bc7fac6de09fe7b"
    )
    assert audit_append_acknowledgement_digest(acknowledgement) == Sha256Digest(
        "d9c5b61de36ae0a60c57d8975239470af0c99d211d9fcd736b1decbbd35dbc4d"
    )
    assert canonical_audit_append_acknowledgement_bytes(acknowledgement).startswith(
        b'{"canonicalization":"ea-canonical-json-v1"'
    )


def test_empty_ordered_digest_vectors_are_stable() -> None:
    assert ordered_digest_tuple(b"ea.audit-ordered-ingress-digests.v1\0", ()) == Sha256Digest(
        "2438409ca5926710ca25fea083cf1a53c582ed679a2bdb84fdfa3ce62dab8027"
    )
    assert ordered_digest_tuple(b"ea.audit-ordered-outcome-ack-digests.v1\0", ()) == Sha256Digest(
        "6a2fb6988624cc65253db3cf79f653c16a6f2ad11aa7a1a1ca785f82bc8995a1"
    )


def _canonical(document: dict[str, object]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


@pytest.mark.parametrize(
    ("record_kind", "document", "field", "invalid"),
    [
        (
            AuditRecordKind.RUN_PREPARED,
            {
                "canonicalization": "ea-canonical-json-v1",
                "lineage_sha256": "11" * 32,
                "manifest_sha256": "22" * 32,
                "run_id": "123e4567-e89b-42d3-a456-426614174000",
                "schema": "ea.audit-run-prepared.v1",
            },
            "lineage_sha256",
            "not-a-digest",
        ),
        (
            AuditRecordKind.MATCHER_DISPATCH_BATCH,
            {
                "batch_sha256": "33" * 32,
                "canonicalization": "ea-canonical-json-v1",
                "dispatch_kind": "market",
                "dispatch_sequence": 1,
                "ingress_count": 0,
                "ordered_ingress_sha256s_sha256": "44" * 32,
                "run_id": "123e4567-e89b-42d3-a456-426614174000",
                "schema": "ea.audit-matcher-dispatch-batch.v1",
                "trigger_root_key": {},
                "trigger_root_sha256": "55" * 32,
            },
            "dispatch_sequence",
            True,
        ),
        (
            AuditRecordKind.RUN_TERMINAL,
            {
                "canonicalization": "ea-canonical-json-v1",
                "last_dispatch_sequence": 2,
                "last_trigger_root_sha256": "33" * 32,
                "pre_terminal_state_sha256": "44" * 32,
                "previous_chain_head_sha256": "55" * 32,
                "run_id": "123e4567-e89b-42d3-a456-426614174000",
                "schema": "ea.audit-run-terminal.v1",
                "terminal_kind": "success",
            },
            "terminal_kind",
            "unknown",
        ),
    ],
)
def test_canonical_payload_rejects_wrong_typed_or_unknown_committed_values(
    record_kind: AuditRecordKind,
    document: dict[str, object],
    field: str,
    invalid: object,
) -> None:
    invalid_document = {**document, field: invalid}

    with pytest.raises(AuditContractError):
        require_canonical_audit_payload(record_kind, _canonical(invalid_document))
