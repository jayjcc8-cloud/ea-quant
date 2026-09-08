"""ChronologicalHoldoutV1 relationship and pure configuration constraints."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from ea.core import ReplayWindow


@dataclass(frozen=True, slots=True)
class HoldoutRecord:
    validation_id: str
    created_at: str
    source_job_id: str
    holdout_job_id: str
    schema: str = "ea.chronological-holdout.v1"

    def document(self) -> dict[str, str]:
        return asdict(self)


def decode_holdout(payload: bytes) -> HoldoutRecord:
    document = json.loads(payload)
    record = HoldoutRecord(**document)
    if record.schema != "ea.chronological-holdout.v1":
        raise ValueError("invalid holdout schema")
    for identity in (record.validation_id, record.source_job_id, record.holdout_job_id):
        if str(UUID(identity)) != identity:
            raise ValueError("invalid holdout identity")
    datetime.strptime(record.created_at, "%Y-%m-%dT%H:%M:%S.%fZ")
    if record.source_job_id == record.holdout_job_id:
        raise ValueError("holdout requires independent jobs")
    return record


def compatible(source: dict[str, Any], target: dict[str, Any]) -> bool:
    def projection(document: dict[str, Any]) -> dict[str, Any]:
        return {
            **{key: value for key, value in document.items() if key not in {"data", "strategy"}},
            "strategy": {"id": document["strategy"]["id"]},
        }

    return projection(source) == projection(target)


def require_chronology(source: dict[str, Any], target: dict[str, Any]) -> None:
    def window(document: dict[str, Any]) -> ReplayWindow:
        return ReplayWindow(
            datetime.fromisoformat(document["data"]["start_utc"]),
            datetime.fromisoformat(document["data"]["end_utc"]),
        )

    if window(target).start_inclusive <= window(source).end_exclusive:
        raise ValueError("holdout start must be strictly later than source end")
