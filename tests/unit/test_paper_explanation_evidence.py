"""Real bound files, synthetic values; AI reads must not mutate runtime evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ea.core.run import RunId, RunReference, Sha256Digest
from ea.experiments.store import CanonicalAttemptManifest, LocalResultStore
from ea.product.paper_session import PaperSessionWriter
from ea.web.paper_desktop import DesktopPaper, PaperEvidenceConflictError
from unit.test_paper_session import _health_document

RUN = "123e4567-e89b-42d3-a456-426614174000"
CANDIDATE = "22222222-2222-4222-8222-222222222222"
SOURCE = "33333333-3333-4333-8333-333333333333"


def fixture(root: Path) -> tuple[DesktopPaper, LocalResultStore, PaperSessionWriter]:
    desktop = DesktopPaper(root / "paper", root / "workspace")
    desktop.open()
    now = datetime.now(UTC).isoformat()
    request = {
        "request_id": RUN,
        "candidate_id": CANDIDATE,
        "fingerprint": "a" * 64,
        "name": "验收模拟 A",
        "prices": [100.0],
        "interval_seconds": 1.0,
    }
    record = {
        "schema": "ea.desktop-paper-request.v1",
        "workspace": str(desktop.workspace),
        "request": request,
        "created_at": now,
        "source_job_id": SOURCE,
        "strategy": {"id": "bounded-long-round-trips-v1", "version": 1},
        "configuration_sha256": "b" * 64,
        "stop_requested": False,
    }
    desktop._publish(desktop.requests / f"{RUN}.json", record)
    reference = RunReference(RunId(RUN), Sha256Digest("1" * 64))
    document = {
        "schema": "ea.local-paper-attempt.v1",
        "run_id": RUN,
        "lineage_sha256": "1" * 64,
        "operation": "paper.start",
        "candidate": {
            "candidate_id": CANDIDATE,
            "fingerprint": "a" * 64,
            "configuration_sha256": "b" * 64,
            "strategy_id": "bounded-long-round-trips-v1",
            "strategy_version": 1,
        },
        "prices": [100.0],
        "interval_seconds": 1.0,
    }
    store = LocalResultStore(desktop.runs)
    manifest = CanonicalAttemptManifest(
        reference, json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    )
    prepared = store.prepare_canonical_attempt(manifest)
    return desktop, store, PaperSessionWriter(desktop.runs / RUN, prepared.audit.binding)


def status(*, age: int = 0, state: str = "running", health: bool = True) -> dict[str, Any]:
    at = datetime.now(UTC) - timedelta(seconds=age)
    data: dict[str, Any] = {
        "state": state,
        "updated_at": at.isoformat(),
        "cash": "10000",
        "equity": "10000",
        "currency": "USD",
        "position": "0",
        "fills": 0,
        "valuation_time": at.isoformat(),
        "valuation_price": "100",
        "private_path": "/private/not-model-evidence",
    }
    if health:
        data["health"] = {**_health_document(at), "candidate_id": CANDIDATE}
    return data


def hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_read_only_evidence_is_bound_scoped_and_has_zero_business_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop, store, writer = fixture(tmp_path)
    try:
        writer.publish(status())
        before = hashes(desktop.root)

        def forbidden(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("business write or recovery called")

        for name in ("get", "list", "start", "stop", "_publish"):
            monkeypatch.setattr(desktop, name, forbidden)
        monkeypatch.setattr("ea.web.paper_desktop.resume_local_paper", forbidden)
        evidence = desktop.explanation_context(RUN)
        assert hashes(desktop.root) == before
        assert evidence["run_id"] == RUN
        assert evidence["candidate_id"] == CANDIDATE
        assert evidence["source_job_id"] == SOURCE
        assert evidence["candidate_fingerprint"] == "a" * 64
        assert evidence["state_scope"] == "current"
        assert evidence["health"]["permission_scope"] == "observed"
        assert "trade_permission_basis" in evidence["health"]
        assert "/private/" not in json.dumps(evidence)
        assert "workspace" not in evidence
    finally:
        store.close()
        desktop._lock.close()


@pytest.mark.parametrize(
    "mode", ["stale", "missing-health", "no-lease", "historical", "missing-status"]
)
def test_evidence_never_claims_current_permission_without_current_health(
    tmp_path: Path, mode: str
) -> None:
    desktop, store, writer = fixture(tmp_path)
    try:
        writer.publish(
            status(
                age=60 if mode == "stale" else 0,
                health=mode != "missing-health",
                state="stopped" if mode == "historical" else "running",
            )
        )
        if mode in {"no-lease", "historical"}:
            store.close()
        if mode == "missing-status":
            (desktop.runs / RUN / "outputs" / "paper-status.json").unlink()
        evidence = desktop.explanation_context(RUN)
        assert evidence["state_scope"] != "current"
        assert evidence["health"].get("trade_permitted") is None
        assert evidence["health"].get("runtime_ready") is None
        if mode == "stale":
            assert evidence["state_scope"] == "stale"
        if mode == "historical":
            assert evidence["state_scope"] == "historical"
    finally:
        store.close()
        desktop._lock.close()


def test_binding_drift_is_refused_before_provider_dispatch(tmp_path: Path) -> None:
    desktop, store, writer = fixture(tmp_path)
    try:
        writer.publish(status())
        path = desktop.requests / f"{RUN}.json"
        record = json.loads(path.read_text())
        record["request"]["fingerprint"] = "c" * 64
        desktop._publish(path, record)
        with pytest.raises(PaperEvidenceConflictError):
            desktop.explanation_context(RUN)
    finally:
        store.close()
        desktop._lock.close()
