"""Gate preparation for the local Paper runtime (WU-5).

WU-2 froze the evidence contract -- ``PaperSnapshot``, ``PaperGateIdentity``,
``PaperGateVerdict`` and the pure ``evaluate_gate`` -- and deliberately stopped
short of the real host surface. This module is that thin operational layer, and
nothing more:

* :func:`observe_paper_snapshot` reads the real local Paper environment into the
  **existing** ``PaperSnapshot``. Every input is an existing owner's read: the
  ``ea paper status`` health projection (with liveness re-observed from the live
  writer lease), the newest verified backup, the durable alert stream, the
  attempt's own recorded profile and this checkout's git HEAD. It recomputes no
  economic, risk, reconciliation or readiness truth, and it writes nothing.
* :func:`current_gate_identity` materialises the **existing**
  ``PaperGateIdentity`` from the real operator config and the rendered launchd
  definitions, and :func:`lock_gate_identity` persists one, once.
* ``evaluate_gate`` remains the only verdict authority. Nothing here judges a
  gate, and nothing here starts, times or tracks one.

Absent evidence stays absent. A status that cannot be read fails the observation
rather than producing a snapshot, a missing alert stream is ``unavailable`` and
never ``clean``, and an unreadable backup root is never ``fresh`` -- so a gate
prepared against a blind observer cannot come out ``PASS``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from ea.core import RunId
from ea.product.paper_alerts import PaperAlertUnavailable, read_alert_stream
from ea.product.paper_backup import PaperBackupError, latest_verified_paper_backup
from ea.product.paper_evidence import (
    DEFAULT_BACKUP_FRESHNESS_SECONDS,
    AlertSummary,
    BackupSummary,
    BrokerMode,
    GateType,
    PaperGateIdentity,
    PaperSnapshot,
    SupervisorState,
    build_gate_identity,
    build_paper_snapshot,
    canonical_gate_identity_bytes,
    decode_gate_identity,
    summarize_alert_state,
    summarize_backup_state,
)
from ea.product.paper_health import decode_paper_health, is_paper_health_unavailable
from ea.product.paper_session import read_paper_binding, read_paper_status
from ea.strategy.package import read_regular

# The two rendered LaunchAgents whose drift would invalidate a gate. Sorted, so
# the identity digest does not depend on directory iteration order.
LAUNCHD_LABELS = ("com.ea.paper", "com.ea.paper-backup")

# The account and operation the local simulated Paper profile records in its own
# attempt manifest (``ea.product.paper_run``). They are the only observable trace
# of which profile an attempt ran under.
_LOCAL_PAPER_ACCOUNT = "paper.local"
_LOCAL_PAPER_OPERATION = "paper.start"

_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z", flags=re.ASCII)
_MAX_IDENTITY_BYTES = 65_536


class PaperGatePrepError(RuntimeError):
    """The gate preparation surface could not observe or persist what it must."""


def observe_git_head(repository: Path) -> str:
    """Read the exact commit this checkout is on, or fail closed.

    This is the repository identity a gate locks. It is deliberately only an
    observation of HEAD: whether the worktree is clean, or whether the installed
    wheel matches, is not this module's question, and inventing a policy for it
    here would be a new gate rule rather than a new reading of an old fact.
    """
    if not isinstance(repository, Path) or not repository.is_absolute():
        raise PaperGatePrepError("repository must be an absolute Path")
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--verify", "HEAD^{commit}"],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PaperGatePrepError("git HEAD could not be observed") from error
    if completed.returncode != 0:
        raise PaperGatePrepError("that directory is not a readable git checkout")
    try:
        head = completed.stdout.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise PaperGatePrepError("git HEAD is not ASCII") from error
    if _GIT_SHA.fullmatch(head) is None:
        raise PaperGatePrepError("git HEAD is not one full lowercase commit")
    return head


def observe_config_identity_bytes(config_path: Path) -> bytes:
    """Read the operator config whose bytes are the configurable identity."""
    return _observe_regular_bytes(config_path, field="operator config")


def observe_launchd_identity_bytes(launchd_dir: Path) -> bytes:
    """Read the rendered launchd definitions the running supervisor is pinned to.

    These are the *rendered* LaunchAgents, not the repository templates: the
    template is a build input, while the rendered plist carries the pinned paths,
    intervals and throttle the job actually runs with. Both labels are required,
    because a supervisor that is only half rendered is not a lockable identity.
    Each definition is framed with its own label, so two definitions can never
    concatenate into the same bytes as a different pair.
    """
    if not isinstance(launchd_dir, Path) or not launchd_dir.is_absolute():
        raise PaperGatePrepError("launchd directory must be an absolute Path")
    framed = bytearray()
    for label in LAUNCHD_LABELS:
        rendered = _observe_regular_bytes(
            launchd_dir / f"{label}.plist", field=f"rendered {label} definition"
        )
        framed += label.encode("ascii") + b"\0" + rendered + b"\0"
    return bytes(framed)


def _observe_regular_bytes(path: Path, *, field: str) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute():
        raise PaperGatePrepError(f"{field} must be an absolute Path")
    try:
        return read_regular(path, path.parent, limit=_MAX_IDENTITY_BYTES)
    except ValueError as error:
        raise PaperGatePrepError(f"{field} is missing or is not a plain readable file") from error


def _observe_live_enabled(manifest_bytes: bytes) -> bool:
    """Whether the attempt carries a capability other than local simulated Paper.

    There is no Live flag to read, because the product has no Live capability to
    record one. What *is* observable is the profile the attempt recorded for
    itself, so the boundary is read from that: an attempt that is not the local
    simulated Paper operation on the local Paper account is reported as
    live-capable. That fails closed -- an unreadable or unexpected profile makes
    the gate ``INVALID`` instead of quietly reading as Live-denied.
    """
    try:
        document: Any = json.loads(manifest_bytes)
    except (ValueError, TypeError):
        return True
    if type(document) is not dict:
        return True
    return not (
        document.get("operation") == _LOCAL_PAPER_OPERATION
        and document.get("account_id") == _LOCAL_PAPER_ACCOUNT
    )


def observe_paper_snapshot(
    *,
    run_dir: Path,
    repository: Path,
    supervisor_state: SupervisorState,
    now: datetime,
    backup_root: Path | None = None,
    alert_stream_path: Path | None = None,
) -> PaperSnapshot:
    """Read the real local Paper environment into one WU-2 ``PaperSnapshot``.

    Read-only and side-effect free: it opens the attempt (a shared, non-blocking
    read of the writer lease, which the product's own ``ea paper status`` already
    takes), lists the backup root, reads the alert stream and runs one read-only
    git command. Nothing here repairs, forces, captures, writes or resends.

    Every input that cannot be observed keeps the contract's own unavailable
    value -- an unavailable health projection, an unreadable alert stream and a
    missing backup root all reduce to a state that cannot pass a gate. A status
    that cannot be read at all raises instead, so there is no snapshot to
    misread as healthy.
    """
    if type(supervisor_state) is not SupervisorState:
        raise PaperGatePrepError("supervisor_state must be an exact SupervisorState")
    repo_sha = observe_git_head(repository)
    status = read_paper_status(run_dir, now=now)
    manifest_bytes = read_paper_binding(run_dir)[1]

    health_slot = status.get("health")
    if health_slot is None or is_paper_health_unavailable(health_slot):
        # An absent or explicitly unavailable projection is not a healthy one.
        health = None
    else:
        health = decode_paper_health(health_slot)

    if alert_stream_path is None:
        alert_state = AlertSummary.UNAVAILABLE
    else:
        try:
            stream = read_alert_stream(alert_stream_path)
        except PaperAlertUnavailable:
            # Absent and unreadable are both "cannot observe", never "clean".
            alert_state = AlertSummary.UNAVAILABLE
        else:
            alert_state = summarize_alert_state(stream)

    if backup_root is None:
        backup_state = BackupSummary.UNAVAILABLE
    else:
        try:
            verified = latest_verified_paper_backup(backup_root)
        except PaperBackupError:
            backup_state = BackupSummary.UNAVAILABLE
        else:
            backup_state = summarize_backup_state(
                None if verified is None else verified.created_at,
                captured_at=now,
                freshness_seconds=DEFAULT_BACKUP_FRESHNESS_SECONDS,
            )

    return build_paper_snapshot(
        captured_at=now,
        repo_sha=repo_sha,
        run_id=RunId(status["run_id"]),
        supervisor_state=supervisor_state,
        writer_lease_held=status["lease_held"],
        broker_mode=BrokerMode.LOCAL_SIMULATED_PAPER,
        live_enabled=_observe_live_enabled(manifest_bytes),
        health=health,
        alert_state=alert_state,
        backup_state=backup_state,
    )


def current_gate_identity(
    *,
    gate_id: str,
    gate_type: GateType,
    repository: Path,
    config_path: Path,
    launchd_dir: Path,
    started_at: datetime,
) -> PaperGateIdentity:
    """Materialise the identity a gate locked now would certify against.

    Rebuilt from the same real inputs every time it is asked, which is what makes
    drift observable: this is the "current" half of
    ``evaluate_gate(snapshot, locked, current)`` and it is never repaired towards
    a previously locked identity.
    """
    return build_gate_identity(
        gate_id=gate_id,
        gate_type=gate_type,
        repo_sha=observe_git_head(repository),
        config_bytes=observe_config_identity_bytes(config_path),
        launchd_bytes=observe_launchd_identity_bytes(launchd_dir),
        started_at=started_at,
    )


def lock_gate_identity(identity_path: Path, identity: PaperGateIdentity) -> None:
    """Persist one gate identity, once, and never replace or repair an existing one.

    Creation is exclusive: an identity that is already locked stays locked. Drift
    after that becomes ``INVALID`` through ``evaluate_gate``, which is the whole
    point of locking -- silently relocking would turn a broken gate into a fresh
    one. The write is a single ``O_EXCL`` create plus an ``fsync``, so a reader
    never sees a partial identity.
    """
    if type(identity) is not PaperGateIdentity:
        raise PaperGatePrepError("identity must be an exact PaperGateIdentity")
    if not isinstance(identity_path, Path) or not identity_path.is_absolute():
        raise PaperGatePrepError("identity path must be an absolute Path")
    payload = canonical_gate_identity_bytes(identity)
    try:
        descriptor = os.open(
            identity_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except FileExistsError as error:
        raise PaperGatePrepError(
            "a gate identity is already locked here; drift is INVALID, never a relock"
        ) from error
    except OSError as error:
        raise PaperGatePrepError("the gate identity could not be created") from error
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(identity_path.parent)
    except OSError as error:
        # The create was exclusive and this process owns the file, so a half
        # written lock is removed rather than left behind as an unreadable
        # identity that would block the operator from retrying.
        identity_path.unlink(missing_ok=True)
        raise PaperGatePrepError("the gate identity could not be written") from error


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_locked_gate_identity(identity_path: Path) -> PaperGateIdentity:
    """Read one locked gate identity back, refusing anything but its exact bytes."""
    payload = _observe_regular_bytes(identity_path, field="locked gate identity")
    try:
        document: Any = json.loads(payload)
    except (ValueError, TypeError) as error:
        raise PaperGatePrepError("the locked gate identity is not one JSON object") from error
    identity = decode_gate_identity(document)
    if payload != canonical_gate_identity_bytes(identity):
        # A lock file that is not its own canonical form is not the document that
        # was locked, whatever it happens to decode to.
        raise PaperGatePrepError("the locked gate identity is not in canonical form")
    return identity


__all__ = [
    "LAUNCHD_LABELS",
    "PaperGatePrepError",
    "current_gate_identity",
    "lock_gate_identity",
    "observe_config_identity_bytes",
    "observe_git_head",
    "observe_launchd_identity_bytes",
    "observe_paper_snapshot",
    "read_locked_gate_identity",
]
