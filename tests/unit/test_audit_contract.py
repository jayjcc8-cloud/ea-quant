from __future__ import annotations

import json

import pytest

from ea.core.audit import (
    EMPTY_CHAIN_HEAD_SHA256,
    EMPTY_RECORD_SHA256,
    MAX_AUDIT_RECORDS,
    AuditContractError,
    AuditLogicalKey,
    AuditRecordKind,
    AuditSubjectKind,
    audit_append_acknowledgement_digest,
    audit_chain_head,
    audit_frame_checksum,
    audit_record_digest,
    audit_subject_digest,
    canonical_audit_append_acknowledgement_bytes,
    canonical_audit_record_header_bytes,
    canonical_run_prepared_audit_payload,
    create_audit_append_acknowledgement,
    create_audit_record,
    decode_audit_record,
    ordered_digest_tuple,
    require_audit_acknowledgement,
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


def test_accepted_adr_0022_record_sequence_bound_is_exact() -> None:
    payload = _canonical(
        {
            "canonicalization": "ea-canonical-json-v1",
            "dispatch_sequence": 1,
            "failed_record_kind": "matcher.dispatch_batch",
            "failed_subject_kind": "historical_matcher_dispatch_batch",
            "failed_subject_sha256": "33" * 32,
            "failing_state_sha256": "44" * 32,
            "failure_code": "validation.conflicting_id",
            "previous_state_sha256": "55" * 32,
            "run_id": "123e4567-e89b-42d3-a456-426614174000",
            "schema": "ea.audit-failing-safety.v1",
            "trigger_root_sha256": "66" * 32,
        }
    )
    kind = AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION
    arguments = {
        "binding": _binding(),
        "record_kind": kind,
        "subject_kind": AuditSubjectKind.COORDINATOR_STATE,
        "subject_sha256": audit_subject_digest(kind, payload),
        "canonical_payload": payload,
        "previous_record_sha256": Sha256Digest("77" * 32),
        "previous_chain_head_sha256": Sha256Digest("88" * 32),
    }

    record = create_audit_record(owner_sequence=MAX_AUDIT_RECORDS, **arguments)

    assert record.record_id.owner_sequence == 600_006
    with pytest.raises(AuditContractError, match="outside the v1 bound"):
        create_audit_record(owner_sequence=MAX_AUDIT_RECORDS + 1, **arguments)


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


def test_audit_recovery_contract_rejects_malformed_boundary_carriers() -> None:
    binding = _binding()
    prepared = _prepared_payload()

    with pytest.raises(AuditContractError, match="exact RunBinding"):
        canonical_run_prepared_audit_payload(object())  # type: ignore[arg-type]
    with pytest.raises(AuditContractError, match="exact enum"):
        AuditLogicalKey("run.prepared", AuditSubjectKind.RUN_MANIFEST, Sha256Digest("22" * 32))  # type: ignore[arg-type]
    with pytest.raises(AuditContractError, match="conflict"):
        AuditLogicalKey(
            AuditRecordKind.RUN_PREPARED,
            AuditSubjectKind.RUNTIME_DISPATCH,
            Sha256Digest("22" * 32),
        )
    with pytest.raises(AuditContractError, match="exact Sha256Digest"):
        AuditLogicalKey(
            AuditRecordKind.RUN_PREPARED,
            AuditSubjectKind.RUN_MANIFEST,
            "22" * 32,  # type: ignore[arg-type]
        )

    malformed_payloads = (
        b"",
        b"\xff",
        b"NaN",
        b'{"schema":"duplicate","schema":"duplicate"}',
        b"[]",
        b'{ "canonicalization":"ea-canonical-json-v1" }',
        _canonical({"canonicalization": "ea-canonical-json-v1"}),
        _canonical(
            {
                "canonicalization": "wrong",
                "lineage_sha256": "11" * 32,
                "manifest_sha256": "22" * 32,
                "run_id": "123e4567-e89b-42d3-a456-426614174000",
                "schema": "ea.audit-run-prepared.v1",
            }
        ),
        _canonical(
            {
                "canonicalization": "ea-canonical-json-v1",
                "lineage_sha256": "not-a-digest",
                "manifest_sha256": "22" * 32,
                "run_id": True,
                "schema": "ea.audit-run-prepared.v1",
            }
        ),
    )
    for payload in malformed_payloads:
        with pytest.raises(AuditContractError):
            require_canonical_audit_payload(AuditRecordKind.RUN_PREPARED, payload)
    with pytest.raises(AuditContractError, match="bound"):
        require_canonical_audit_payload(
            AuditRecordKind.RUN_PREPARED,
            b"x" * 4_097,
        )

    invalid_batch = {
        "batch_sha256": "33" * 32,
        "canonicalization": "ea-canonical-json-v1",
        "dispatch_kind": "unknown",
        "dispatch_sequence": 1,
        "ingress_count": 0,
        "ordered_ingress_sha256s_sha256": "44" * 32,
        "run_id": "123e4567-e89b-42d3-a456-426614174000",
        "schema": "ea.audit-matcher-dispatch-batch.v1",
        "trigger_root_key": {},
        "trigger_root_sha256": "55" * 32,
    }
    for update in (
        {},
        {"dispatch_kind": "market", "dispatch_sequence": 0},
        {"dispatch_kind": "market", "dispatch_sequence": 1, "trigger_root_key": []},
        {"dispatch_kind": "market", "dispatch_sequence": 1, "trigger_root_key": {}},
    ):
        with pytest.raises(AuditContractError):
            require_canonical_audit_payload(
                AuditRecordKind.MATCHER_DISPATCH_BATCH,
                _canonical({**invalid_batch, **update}),
            )

    record = create_audit_record(
        binding=binding,
        owner_sequence=1,
        record_kind=AuditRecordKind.RUN_PREPARED,
        subject_kind=AuditSubjectKind.RUN_MANIFEST,
        subject_sha256=Sha256Digest("22" * 32),
        canonical_payload=prepared,
        previous_record_sha256=EMPTY_RECORD_SHA256,
        previous_chain_head_sha256=EMPTY_CHAIN_HEAD_SHA256,
    )
    acknowledgement = create_audit_append_acknowledgement(record)
    logical_key = record.logical_key
    assert (
        require_audit_acknowledgement(
            acknowledgement,
            binding=binding,
            logical_key=logical_key,
            canonical_payload=prepared,
        )
        is acknowledgement
    )
    for invalid_binding, invalid_key, invalid_payload in (
        (object(), logical_key, prepared),
        (binding, object(), prepared),
        (binding, logical_key, "not-bytes"),
        (binding, logical_key, b"{}"),
    ):
        with pytest.raises(AuditContractError):
            require_audit_acknowledgement(
                acknowledgement,
                binding=invalid_binding,  # type: ignore[arg-type]
                logical_key=invalid_key,  # type: ignore[arg-type]
                canonical_payload=invalid_payload,  # type: ignore[arg-type]
            )

    for domain, digests in (
        ("not-bytes", ()),
        (b"domain", []),
        (b"domain", (object(),)),
    ):
        with pytest.raises(AuditContractError):
            ordered_digest_tuple(domain, digests)  # type: ignore[arg-type]


def test_audit_record_recovery_rejects_forged_records_and_headers() -> None:
    binding = _binding()
    prepared = _prepared_payload()
    valid_arguments = {
        "binding": binding,
        "owner_sequence": 1,
        "record_kind": AuditRecordKind.RUN_PREPARED,
        "subject_kind": AuditSubjectKind.RUN_MANIFEST,
        "subject_sha256": Sha256Digest("22" * 32),
        "canonical_payload": prepared,
        "previous_record_sha256": EMPTY_RECORD_SHA256,
        "previous_chain_head_sha256": EMPTY_CHAIN_HEAD_SHA256,
    }
    invalid_argument_updates: tuple[dict[str, object], ...] = (
        {"binding": object()},
        {"owner_sequence": True},
        {"owner_sequence": 0},
        {"subject_sha256": "22" * 32},
        {"subject_sha256": Sha256Digest("33" * 32)},
        {"previous_record_sha256": "44" * 32},
        {"previous_record_sha256": Sha256Digest("44" * 32)},
        {"owner_sequence": 2},
    )
    for update in invalid_argument_updates:
        with pytest.raises(AuditContractError):
            create_audit_record(**{**valid_arguments, **update})  # type: ignore[arg-type]

    foreign_payload = _canonical(
        {
            "canonicalization": "ea-canonical-json-v1",
            "lineage_sha256": "11" * 32,
            "manifest_sha256": "22" * 32,
            "run_id": "223e4567-e89b-42d3-a456-426614174000",
            "schema": "ea.audit-run-prepared.v1",
        }
    )
    with pytest.raises(AuditContractError, match="run binding"):
        create_audit_record(
            **{**valid_arguments, "canonical_payload": foreign_payload}  # type: ignore[arg-type]
        )

    record = create_audit_record(**valid_arguments)  # type: ignore[arg-type]
    header = canonical_audit_record_header_bytes(record)
    header_document = json.loads(header)
    invalid_headers = (
        b"[]",
        b'{ "schema":"noncanonical" }',
        _canonical({"schema": "wrong-keys"}),
        _canonical({**header_document, "record_id": {}}),
        _canonical({**header_document, "schema": "wrong-schema"}),
        _canonical(
            {
                **header_document,
                "record_id": {**header_document["record_id"], "owner_sequence": "one"},
            }
        ),
    )
    with pytest.raises(AuditContractError, match="carriers"):
        decode_audit_record(
            binding=object(),  # type: ignore[arg-type]
            canonical_header=header,
            canonical_payload=prepared,
        )
    with pytest.raises(AuditContractError, match="length"):
        decode_audit_record(binding=binding, canonical_header=b"", canonical_payload=prepared)
    for invalid_header in invalid_headers:
        with pytest.raises(AuditContractError):
            decode_audit_record(
                binding=binding,
                canonical_header=invalid_header,
                canonical_payload=prepared,
            )
    with pytest.raises(AuditContractError, match="factory-issued"):
        create_audit_append_acknowledgement(object())  # type: ignore[arg-type]


def test_execution_outcome_audit_recovery_rejects_malformed_payloads() -> None:
    outcome: dict[str, object] = {
        "action": "ignored",
        "anomalies": [],
        "canonicalization": "ea-execution-fact-processing-outcome-v1",
        "client_submission_key": None,
        "fact_key": {},
        "fact_sha256": "11" * 32,
        "fill": None,
        "halt_requested": False,
        "ingress_identity": {},
        "ingress_sha256": "22" * 32,
        "message_type": "execution_fact_processing_outcome",
        "order_resolutions": [],
        "outcome_code": "fact.accepted",
        "projection_after_sha256": None,
        "projection_before_sha256": None,
        "reported_order_id": None,
        "reported_venue_order": None,
        "requires_reconciliation": False,
        "resolved_order_id": None,
        "run_id": "123e4567-e89b-42d3-a456-426614174000",
        "runtime_dispatch_sequence": 1,
        "schema_version": 1,
    }
    malformed: tuple[dict[str, object], ...] = (
        {},
        {**outcome, "canonicalization": "wrong"},
        {**outcome, "run_id": True},
        {**outcome, "outcome_code": "unknown"},
        {**outcome, "runtime_dispatch_sequence": 0},
        {**outcome, "anomalies": {}},
        {**outcome, "halt_requested": 0},
    )
    for document in malformed:
        with pytest.raises(AuditContractError):
            require_canonical_audit_payload(
                AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME,
                _canonical(document),
            )
