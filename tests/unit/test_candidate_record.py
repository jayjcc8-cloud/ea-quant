from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest

from ea.web.candidates import canonical, create_record, decide_record, decode_record


def projection() -> dict[str, Any]:
    def evidence() -> dict[str, Any]:
        return {
            "job_id": str(uuid4()),
            "run_id": str(uuid4()),
            "input_sha256": "a" * 64,
            "scenario_sha256": "b" * 64,
            "data_sha256": "c" * 64,
            "record_count": 3,
            "report_sha256": "d" * 64,
            "equity_path_sha256": "e" * 64,
            "code_sha256": "f" * 64,
            "distribution": {"name": "ea-quant", "version": "0.2.0"},
        }

    return {
        "schema": "ea.research-candidate.v1",
        "schema_version": 1,
        "strategy": {
            "id": "bounded-long-v1",
            "version": 1,
            "parameters": {"target_quantity": "2"},
            "implementation": {
                "kind": "builtin",
                "code_sha256": "f" * 64,
                "distribution": {"name": "ea-quant", "version": "0.2.0"},
            },
        },
        "source": evidence(),
        "holdout": evidence(),
        "relationship": {"validation_id": str(uuid4()), "sha256": "1" * 64},
    }


def test_candidate_fingerprint_excludes_record_and_decision_identity() -> None:
    identity = projection()
    a = create_record(identity, "2026-09-20T08:00:00.000000Z")
    b = create_record(identity, "2026-09-20T08:01:00.000000Z")
    assert a["candidate_id"] != b["candidate_id"]
    assert a["fingerprint"] == b["fingerprint"]
    changed = deepcopy(identity)
    changed["source"]["report_sha256"] = "2" * 64
    assert create_record(changed, a["created_at"])["fingerprint"] != a["fingerprint"]
    accepted = decide_record(a, "ACCEPTED", "Retain this evidence", b["created_at"])
    assert accepted["fingerprint"] == a["fingerprint"]
    assert decode_record(canonical(accepted)) == accepted
    assert (
        decide_record(accepted, "ACCEPTED", "Retain this evidence", "2026-09-21T08:00:00.000000Z")
        == accepted
    )
    with pytest.raises(ValueError):
        decide_record(accepted, "REJECTED", "Different decision", b["created_at"])


def test_candidate_rejects_incomplete_or_ambiguous_records() -> None:
    record = create_record(projection(), "2026-09-20T08:00:00.000000Z")
    with pytest.raises(ValueError):
        decide_record(record, "ACCEPTED", " ", record["created_at"])
    with pytest.raises(ValueError):
        decode_record(canonical({**record, "unknown": True}))
    with pytest.raises(ValueError):
        decode_record(
            canonical(record).replace(
                b'"status":"EVALUATED"', b'"status":"EVALUATED","status":"ACCEPTED"'
            )
        )
    record["projection"]["source"]["report_sha256"] = "3" * 64
    with pytest.raises(ValueError):
        decode_record(canonical(record))


@pytest.mark.parametrize("outcome", ["ACCEPTED", "REJECTED"])
def test_candidate_terminal_decision_cannot_be_revised(outcome: str) -> None:
    record = create_record(projection(), "2026-09-20T08:00:00.000000Z")
    decided = decide_record(record, outcome, "Explicit human judgment", record["created_at"])
    assert (
        decide_record(decided, outcome, "Explicit human judgment", "2026-09-22T08:00:00.000000Z")
        == decided
    )
    for target, reason in [(outcome, "New reason"), ("EVALUATED", "Undo"), ("DRAFT", "Reset")]:
        with pytest.raises(ValueError):
            decide_record(decided, target, reason, "2026-09-22T08:00:00.000000Z")
    assert record["status"] == "EVALUATED"
    assert record["decision"] is None


def test_candidate_fingerprint_binds_strategy_configuration_and_runtime_identity() -> None:
    original = projection()
    baseline = create_record(original, "2026-09-20T08:00:00.000000Z")
    changed = deepcopy(original)
    changed["strategy"]["parameters"]["target_quantity"] = "3"
    assert create_record(changed, baseline["created_at"])["fingerprint"] != baseline["fingerprint"]
    changed = deepcopy(original)
    changed["source"]["run_id"] = str(uuid4())
    assert create_record(changed, baseline["created_at"])["fingerprint"] != baseline["fingerprint"]


@pytest.mark.parametrize("identity", ["unknown", "00000000-0000-1000-8000-000000000000"])
def test_candidate_record_requires_canonical_uuid4(identity: str) -> None:
    record = create_record(projection(), "2026-09-20T08:00:00.000000Z")
    record["candidate_id"] = identity
    with pytest.raises(ValueError):
        decode_record(canonical(record))
