"""Strict immutable research evidence identity and one-way human decisions."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid4

DOMAIN = b"ea.research-candidate.fingerprint.v1\x00"


def canonical(document: object) -> bytes:
    return (
        json.dumps(
            document, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
        )
        + "\n"
    ).encode()


def _fields(value: Any, names: str) -> None:
    if type(value) is not dict or set(value) != set(names.split()):
        raise ValueError("candidate fields are invalid")


def _text(value: Any) -> None:
    if type(value) is not str or not value.strip() or len(value) > 4096:
        raise ValueError("candidate text is invalid")


def _digest(value: Any) -> None:
    if type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("candidate digest is invalid")


def _uuid(value: Any) -> None:
    if type(value) is not str or str(UUID(value)) != value or UUID(value).version != 4:
        raise ValueError("candidate UUID is invalid")


def _time(value: Any) -> datetime:
    if (
        type(value) is not str
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", value) is None
    ):
        raise ValueError("candidate timestamp is invalid")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")


def _distribution(value: Any) -> None:
    _fields(value, "name version")
    _text(value["name"])
    _text(value["version"])


def validate_projection(value: Any) -> None:
    _fields(value, "schema schema_version strategy source holdout relationship")
    if (
        value["schema"] != "ea.research-candidate.v1"
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError("candidate projection schema is invalid")
    strategy = value["strategy"]
    _fields(strategy, "id version parameters implementation")
    _text(strategy["id"])
    if type(strategy["version"]) is not int or strategy["version"] < 1:
        raise ValueError("candidate strategy version is invalid")
    if type(strategy["parameters"]) is not dict:
        raise ValueError("candidate parameters are invalid")
    for key, parameter in strategy["parameters"].items():
        _text(key)
        if type(parameter) not in (str, int):
            raise ValueError("candidate parameter type is invalid")
        if type(parameter) is str:
            _text(parameter)
    implementation = strategy["implementation"]
    if type(implementation) is not dict:
        raise ValueError("candidate implementation is invalid")
    kind = implementation.get("kind")
    if kind not in ("builtin", "local"):
        raise ValueError("candidate implementation kind is invalid")
    _fields(
        implementation,
        "kind code_sha256 distribution"
        + (" package_id artifact_sha256" if kind == "local" else ""),
    )
    _digest(implementation["code_sha256"])
    _distribution(implementation["distribution"])
    if kind == "local":
        _text(implementation["package_id"])
        _digest(implementation["artifact_sha256"])
    for role in ("source", "holdout"):
        evidence = value[role]
        _fields(
            evidence,
            "job_id run_id input_sha256 scenario_sha256 data_sha256 record_count "
            "report_sha256 equity_path_sha256 code_sha256 distribution",
        )
        for field in ("job_id", "run_id"):
            _uuid(evidence[field])
        for field in (
            "input_sha256",
            "scenario_sha256",
            "data_sha256",
            "report_sha256",
            "equity_path_sha256",
            "code_sha256",
        ):
            _digest(evidence[field])
        if type(evidence["record_count"]) is not int or evidence["record_count"] < 1:
            raise ValueError("candidate record count is invalid")
        _distribution(evidence["distribution"])
        if (
            evidence["code_sha256"] != implementation["code_sha256"]
            or evidence["distribution"] != implementation["distribution"]
        ):
            raise ValueError("candidate implementation identities conflict")
    if (
        value["source"]["job_id"] == value["holdout"]["job_id"]
        or value["source"]["run_id"] == value["holdout"]["run_id"]
    ):
        raise ValueError("candidate requires independent evidence")
    _fields(value["relationship"], "validation_id sha256")
    _uuid(value["relationship"]["validation_id"])
    _digest(value["relationship"]["sha256"])


def create_record(projection: dict[str, Any], now: str) -> dict[str, Any]:
    validate_projection(projection)
    _time(now)
    return {
        "schema": "ea.research-candidate-record.v1",
        "candidate_id": str(uuid4()),
        "fingerprint": sha256(DOMAIN + canonical(projection)).hexdigest(),
        "projection": deepcopy(projection),
        "status": "EVALUATED",
        "created_at": now,
        "evaluated_at": now,
        "decision": None,
    }


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate candidate field")
        result[key] = value
    return result


def decode_record(payload: bytes) -> dict[str, Any]:
    if len(payload) > 65536:
        raise ValueError("candidate record is too large")
    value = json.loads(payload, object_pairs_hook=_pairs)
    _fields(
        value, "schema candidate_id fingerprint projection status created_at evaluated_at decision"
    )
    if value["schema"] != "ea.research-candidate-record.v1" or canonical(value) != payload:
        raise ValueError("candidate record is not canonical")
    _uuid(value["candidate_id"])
    _digest(value["fingerprint"])
    validate_projection(value["projection"])
    if sha256(DOMAIN + canonical(value["projection"])).hexdigest() != value["fingerprint"]:
        raise ValueError("candidate fingerprint conflicts")
    evaluated = _time(value["evaluated_at"])
    if value["created_at"] != value["evaluated_at"]:
        raise ValueError("candidate creation time conflicts")
    if value["status"] == "EVALUATED":
        if value["decision"] is not None:
            raise ValueError("evaluated candidate has terminal decision")
    elif value["status"] in ("ACCEPTED", "REJECTED"):
        decision = value["decision"]
        _fields(decision, "outcome reason decided_at")
        _text(decision["reason"])
        if decision["outcome"] != value["status"] or _time(decision["decided_at"]) < evaluated:
            raise ValueError("candidate decision conflicts")
    else:
        raise ValueError("candidate status is invalid")
    return cast(dict[str, Any], value)


def decide_record(record: dict[str, Any], outcome: str, reason: str, now: str) -> dict[str, Any]:
    checked = decode_record(canonical(record))
    _text(reason)
    if outcome not in ("ACCEPTED", "REJECTED"):
        raise ValueError("candidate outcome is invalid")
    if checked["status"] != "EVALUATED":
        if checked["decision"]["outcome"] == outcome and checked["decision"]["reason"] == reason:
            return checked
        raise ValueError("candidate decision is immutable")
    if _time(now) < _time(checked["evaluated_at"]):
        raise ValueError("candidate decision predates evaluation")
    checked["status"] = outcome
    checked["decision"] = {"outcome": outcome, "reason": reason, "decided_at": now}
    return decode_record(canonical(checked))
