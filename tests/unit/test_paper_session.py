from __future__ import annotations

import json
import os
import stat
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from ea.core.run import RunBinding, RunId, RunReference, Sha256Digest
from ea.experiments.store import CanonicalAttemptManifest, LocalResultStore
from ea.product.paper_session import (
    PaperSessionError,
    PaperSessionTimeout,
    PaperSessionWriter,
    read_paper_status,
    request_paper_stop,
)

RUN_ID = RunId("123e4567-e89b-42d3-a456-426614174000")
LINEAGE = Sha256Digest("1" * 64)


@pytest.fixture
def attempt(tmp_path: Path) -> Iterator[tuple[LocalResultStore, Path, RunBinding]]:
    root = tmp_path / "results"
    root.mkdir()
    store = LocalResultStore(root.resolve())
    reference = RunReference(RUN_ID, LINEAGE)
    manifest = {
        "schema": "ea.local-paper-attempt.v1",
        "run_id": RUN_ID.value,
        "lineage_sha256": LINEAGE.value,
    }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    prepared = store.prepare_canonical_attempt(CanonicalAttemptManifest(reference, payload))
    try:
        yield store, root / RUN_ID.value, prepared.audit.binding
    finally:
        store.close()


def test_status_publication_is_atomic_bound_and_observational(
    attempt: tuple[LocalResultStore, Path, RunBinding],
) -> None:
    _, run_dir, binding = attempt
    writer = PaperSessionWriter(run_dir, binding)
    writer.publish({"state": "starting", "cash": "1000.00", "order_count": 0})
    path = run_dir / "outputs" / "paper-status.json"
    first = path.read_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(first)["manifest_sha256"] == binding.manifest_sha256.value
    assert read_paper_status(run_dir) == {
        "schema": "ea.local-paper-status.v1",
        "run_id": RUN_ID.value,
        "manifest_sha256": binding.manifest_sha256.value,
        "state": "starting",
        "cash": "1000.00",
        "order_count": 0,
        "lease_held": True,
    }
    writer.publish({"state": "running", "cash": "999.50", "order_count": 1})
    assert path.read_bytes() != first
    assert read_paper_status(run_dir)["cash"] == "999.50"
    assert sorted(p.name for p in path.parent.iterdir()) == ["paper-status.json"]


def test_binding_and_supplied_identity_conflicts_never_replace_status(
    attempt: tuple[LocalResultStore, Path, RunBinding],
) -> None:
    _, run_dir, binding = attempt
    wrong = RunBinding(binding.reference, Sha256Digest("f" * 64))
    with pytest.raises(PaperSessionError, match="manifest"):
        PaperSessionWriter(run_dir, wrong)
    writer = PaperSessionWriter(run_dir, binding)
    writer.publish({"state": "running"})
    path = run_dir / "outputs" / "paper-status.json"
    before = path.read_bytes()
    with pytest.raises(PaperSessionError, match="identity"):
        writer.publish({"state": "stopped", "run_id": "other"})
    with pytest.raises(PaperSessionError, match="bound"):
        writer.publish({"state": "running", "oversize": "x" * 70_000})
    assert path.read_bytes() == before


def test_missing_lease_interrupts_running_but_terminal_remains_visible(
    attempt: tuple[LocalResultStore, Path, RunBinding],
) -> None:
    store, run_dir, binding = attempt
    writer = PaperSessionWriter(run_dir, binding)
    writer.publish({"state": "running", "cash": "1000"})
    store.close()
    status = read_paper_status(run_dir)
    assert status["state"] == "interrupted"
    assert status["reason"] == "writer_lease_missing"
    assert status["lease_held"] is False
    assert status["cash"] == "1000"


def test_terminal_status_with_lease_is_still_exiting(
    attempt: tuple[LocalResultStore, Path, RunBinding],
) -> None:
    store, run_dir, binding = attempt
    writer = PaperSessionWriter(run_dir, binding)
    writer.publish({"state": "stopped", "reason": "user_stop"})
    assert read_paper_status(run_dir)["lease_held"] is True
    with pytest.raises(PaperSessionTimeout):
        request_paper_stop(run_dir, timeout_seconds=0.0)
    store.close()
    status = read_paper_status(run_dir)
    assert status["state"] == "stopped"
    assert status["lease_held"] is False


def test_stop_request_is_idempotent_and_timeout_retains_request(
    attempt: tuple[LocalResultStore, Path, RunBinding],
) -> None:
    store, run_dir, binding = attempt
    writer = PaperSessionWriter(run_dir, binding)
    writer.publish({"state": "running"})
    with pytest.raises(PaperSessionTimeout):
        request_paper_stop(run_dir, timeout_seconds=0.0)
    stop = run_dir / "outputs" / "paper-stop.json"
    first = stop.read_bytes()
    assert stat.S_IMODE(stop.stat().st_mode) == 0o600
    assert writer.stop_requested()
    with pytest.raises(PaperSessionTimeout):
        request_paper_stop(run_dir, timeout_seconds=0.0)
    assert stop.read_bytes() == first
    writer.publish({"state": "stopped", "reason": "user_stop"})
    store.close()
    assert request_paper_stop(run_dir, timeout_seconds=0.0)["state"] == "stopped"
    assert stop.read_bytes() == first


def test_stop_waits_for_terminal_and_released_writer_lease(
    attempt: tuple[LocalResultStore, Path, RunBinding],
) -> None:
    store, run_dir, binding = attempt
    writer = PaperSessionWriter(run_dir, binding)
    writer.publish({"state": "running"})

    def finish() -> None:
        deadline = time.monotonic() + 1.0
        while not writer.stop_requested() and time.monotonic() < deadline:
            time.sleep(0.005)
        writer.publish({"state": "stopped", "reason": "user_stop"})
        store.close()

    thread = threading.Thread(target=finish)
    thread.start()
    try:
        status = request_paper_stop(run_dir, timeout_seconds=1.0)
    finally:
        thread.join(timeout=1.0)
    assert status["state"] == "stopped"
    assert status["lease_held"] is False
    assert not thread.is_alive()


def test_malformed_stop_and_symlinked_internal_entries_fail_closed(
    attempt: tuple[LocalResultStore, Path, RunBinding],
    tmp_path: Path,
) -> None:
    _, run_dir, binding = attempt
    writer = PaperSessionWriter(run_dir, binding)
    writer.publish({"state": "running"})
    stop = run_dir / "outputs" / "paper-stop.json"
    stop.write_bytes(b'{"schema":"wrong"}')
    os.chmod(stop, 0o600)
    with pytest.raises(PaperSessionError):
        writer.stop_requested()
    with pytest.raises(PaperSessionError):
        request_paper_stop(run_dir, timeout_seconds=0.0)
    stop.unlink()
    target = tmp_path / "target"
    target.write_text("outside")
    stop.symlink_to(target)
    with pytest.raises(PaperSessionError):
        writer.stop_requested()
    stop.unlink()
    status = run_dir / "outputs" / "paper-status.json"
    status.unlink()
    status.symlink_to(target)
    with pytest.raises(PaperSessionError):
        read_paper_status(run_dir)
    with pytest.raises(PaperSessionError):
        writer.publish({"state": "stopped"})
    assert target.read_text() == "outside"


def test_unknown_attempt_and_final_symlink_rejected_but_parent_alias_allowed(
    attempt: tuple[LocalResultStore, Path, RunBinding],
    tmp_path: Path,
) -> None:
    _, run_dir, binding = attempt
    with pytest.raises(PaperSessionError):
        read_paper_status(tmp_path / "missing")
    alias = tmp_path / "alias"
    alias.symlink_to(run_dir.parent, target_is_directory=True)
    writer = PaperSessionWriter(alias / run_dir.name, binding)
    writer.publish({"state": "running"})
    assert read_paper_status(alias / run_dir.name)["lease_held"] is True
    direct_alias = tmp_path / "attempt-alias"
    direct_alias.symlink_to(run_dir, target_is_directory=True)
    with pytest.raises(PaperSessionError):
        read_paper_status(direct_alias)


def test_failed_atomic_replace_keeps_last_complete_status(
    attempt: tuple[LocalResultStore, Path, RunBinding],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, run_dir, binding = attempt
    writer = PaperSessionWriter(run_dir, binding)
    writer.publish({"state": "running", "cash": "1000"})
    path = run_dir / "outputs" / "paper-status.json"
    original = path.read_bytes()

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr("ea.product.paper_session.os.replace", fail_replace)
    with pytest.raises(PaperSessionError, match="atomically"):
        writer.publish({"state": "stopping", "cash": "1000"})
    assert path.read_bytes() == original
    assert sorted(p.name for p in path.parent.iterdir()) == ["paper-status.json"]


@pytest.mark.parametrize("entry", ["manifest", "lease", "outputs"])
def test_internal_symlinks_are_rejected(
    attempt: tuple[LocalResultStore, Path, RunBinding],
    tmp_path: Path,
    entry: str,
) -> None:
    _, run_dir, binding = attempt
    PaperSessionWriter(run_dir, binding).publish({"state": "running"})
    if entry == "manifest":
        original = run_dir / "manifest.json"
        relocated = tmp_path / "manifest-copy"
        relocated.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(relocated)
    elif entry == "lease":
        original = run_dir / "audit" / "writer-v1.lock"
        relocated = tmp_path / "lease-copy"
        relocated.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(relocated)
    else:
        original = run_dir / "outputs"
        relocated = tmp_path / "outputs-copy"
        original.rename(relocated)
        original.symlink_to(relocated, target_is_directory=True)
    with pytest.raises(PaperSessionError):
        read_paper_status(run_dir)


def test_malformed_or_rebound_status_fails_closed(
    attempt: tuple[LocalResultStore, Path, RunBinding],
) -> None:
    _, run_dir, binding = attempt
    writer = PaperSessionWriter(run_dir, binding)
    writer.publish({"state": "running"})
    path = run_dir / "outputs" / "paper-status.json"
    original = json.loads(path.read_bytes())
    rebound = {**original, "manifest_sha256": "f" * 64}
    path.write_bytes(json.dumps(rebound, sort_keys=True, separators=(",", ":")).encode())
    with pytest.raises(PaperSessionError, match="identity"):
        read_paper_status(run_dir)
    with pytest.raises(PaperSessionError):
        writer.publish({"state": "stopped"})
    path.write_bytes(b'{"state":"running","state":"stopped"}')
    with pytest.raises(PaperSessionError, match="malformed"):
        read_paper_status(run_dir)
