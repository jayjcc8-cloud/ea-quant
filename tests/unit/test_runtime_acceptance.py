from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts import accept_production_runtime as acceptance  # noqa: E402


def replies() -> dict[str, bytes]:
    documents: dict[str, bytes] = {}
    for job, report in REPORTS.items():
        documents[f"/api/backtests/{job}"] = json.dumps(
            {
                "job_id": job,
                "status": "succeeded",
                "report_ready": True,
                "report_sha256": hashlib.sha256(report).hexdigest(),
            }
        ).encode()
        documents[f"/api/backtests/{job}/report"] = report
        documents[f"/api/backtests/{job}/artifacts/report.json"] = report
        documents[f"/backtests/{job}"] = b"<html>installed UI</html>"
    return documents


REPORTS = {"old-writer": b'{"result":"old"}', "new-writer": b'{"result":"new"}'}


def test_both_writers_reports_remain_readable() -> None:
    documents = replies()
    acceptance.assert_reports(documents.__getitem__, REPORTS)


@pytest.mark.parametrize("job", REPORTS)
@pytest.mark.parametrize("suffix", ["/report", "/artifacts/report.json"])
def test_incompatible_reader_cannot_pass(job: str, suffix: str) -> None:
    documents = replies()
    documents[f"/api/backtests/{job}{suffix}"] = b'{"result":"changed"}'
    with pytest.raises(AssertionError):
        acceptance.assert_reports(documents.__getitem__, REPORTS)


@pytest.mark.parametrize(
    "field,value", [("status", "interrupted"), ("report_ready", False), ("report_sha256", "0" * 64)]
)
def test_reclassified_or_unbound_job_cannot_pass(field: str, value: Any) -> None:
    documents = replies()
    path = "/api/backtests/new-writer"
    job = json.loads(documents[path])
    job[field] = value
    documents[path] = json.dumps(job).encode()
    with pytest.raises(AssertionError):
        acceptance.assert_reports(documents.__getitem__, REPORTS)
