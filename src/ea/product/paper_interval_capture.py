"""Collect local Paper observations; seal before stopping, then bind settled backups.

This is an evidence file, not runtime authority. The runtime refuses attempt reuse,
so two live observations of the same binding plus its verified settled journal can
bound that attempt. Directory inventory and the operator's append-only launch log
retain replacements, including launches that never produced a readable attempt.
Launch timestamps alone never grant coverage. Local evidence is not authenticated
against someone who can rewrite both the runtime and these files.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from ea.core.run import RunId
from ea.product.market_stream import RealUtcClock
from ea.product.paper_backup import inspect_paper_backup
from ea.product.paper_evidence import (
    GateType,
    PaperGateIdentity,
    SupervisorState,
    decode_gate_identity,
    decode_paper_snapshot,
    evaluate_gate,
)
from ea.product.paper_gate_history import (
    PaperAttemptAssociation,
    PaperAttemptEvidence,
    PaperIntervalHistory,
    PaperIntervalObservation,
    _binding_document,
    _binding_from_document,
    canonical_paper_interval_history_bytes,
    decode_paper_interval_history,
)
from ea.product.paper_gate_prep import (
    PaperGatePrepError,
    current_gate_identity,
    observe_paper_snapshot,
)
from ea.product.paper_session import PaperSessionError, read_paper_binding, read_paper_status


class PaperIntervalError(ValueError):
    """The saved interval cannot be collected or read back."""


def _canonical(document: object) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _write_once(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _supervisor_state() -> SupervisorState:
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/com.ea.paper"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return SupervisorState.UNKNOWN
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.strip() == "state = running":
                return SupervisorState.RUNNING
            if line.strip() == "state = not running":
                return SupervisorState.NOT_RUNNING
    return SupervisorState.UNKNOWN


def _run_dir(paths: dict[str, str]) -> Path:
    if paths.get("current_run"):
        run = Path(Path(paths["current_run"]).read_text().strip())
    else:
        run = Path(paths["run_dir"])
    if run.parent.resolve() != Path(paths["runtime_root"]).resolve():
        raise PaperIntervalError("current attempt is outside the recorded runtime root")
    return run


def _observe(
    paths: dict[str, str],
    locked: PaperGateIdentity | None,
    *,
    gate_id: str,
    gate_type: GateType,
    kind: str,
) -> dict[str, Any]:
    now = RealUtcClock().now()
    errors: list[str] = []
    current = None
    snapshot = None
    try:
        current = current_gate_identity(
            gate_id=gate_id,
            gate_type=gate_type,
            repository=Path(paths["repository"]),
            config_path=Path(paths["config"]),
            launchd_dir=Path(paths["launchd_dir"]),
            started_at=now if locked is None else locked.started_at,
        )
        snapshot = observe_paper_snapshot(
            run_dir=_run_dir(paths),
            repository=Path(paths["repository"]),
            now=now,
            supervisor_state=_supervisor_state()
            if paths.get("launch_history")
            else SupervisorState.UNKNOWN,
            backup_root=Path(paths["backup_root"]) if paths.get("backup_root") else None,
            alert_stream_path=Path(paths["alert_stream"]) if paths.get("alert_stream") else None,
        )
    except (OSError, ValueError, PaperSessionError, PaperGatePrepError) as error:
        errors.append(f"boundary observation unavailable: {error}")
    inventory: dict[str, Any] = {}
    try:
        entries = sorted(Path(paths["runtime_root"]).iterdir())
    except OSError as error:
        entries = []
        errors.append(f"attempt inventory unavailable: {error}")
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            RunId(entry.name)
            binding, manifest = read_paper_binding(entry)
            status = read_paper_status(entry, now=now)
            inventory[entry.name] = {
                "binding": _binding_document(binding),
                "started_at": json.loads(manifest).get("started_at"),
                "lease_held": status["lease_held"],
                "state": status["state"],
                "ended_at": status.get("ended_at"),
            }
        except (ValueError, OSError, PaperSessionError) as error:
            inventory[entry.name] = None
            errors.append(f"unreadable attempt {entry.name}: {error}")
    launch_history: str | None = None
    if paths.get("launch_history"):
        try:
            launch_history = Path(paths["launch_history"]).read_text()
            for line in launch_history.splitlines():
                event = json.loads(line)
                if event["event"] != "launch" or event["run_dir"] != str(
                    Path(paths["runtime_root"]) / event["run_id"]
                ):
                    raise ValueError("launch identity conflicts")
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append(f"launch inventory unavailable: {error}")
    return {
        "kind": "observe" if snapshot is None and kind == "end" else kind,
        "captured_at": now.isoformat(),
        "snapshot": None if snapshot is None else snapshot.document(),
        "current_identity": None if current is None else current.document(),
        "inventory": inventory,
        "errors": errors,
        "launch_history": launch_history,
    }


def begin_interval(
    interval_dir: Path,
    *,
    run_dir: Path,
    repository: Path,
    config: Path,
    launchd_dir: Path,
    gate_id: str,
    gate_type: GateType,
    backup_root: Path | None = None,
    alert_stream: Path | None = None,
    current_run: Path | None = None,
    launch_history: Path | None = None,
) -> dict[str, Any]:
    paths = {
        name: str(path.expanduser().resolve())
        for name, path in {
            "run_dir": run_dir,
            "runtime_root": run_dir.parent,
            "repository": repository,
            "config": config,
            "launchd_dir": launchd_dir,
            "backup_root": backup_root,
            "alert_stream": alert_stream,
            "current_run": current_run,
            "launch_history": launch_history,
        }.items()
        if path is not None
    }
    first = _observe(paths, None, gate_id=gate_id, gate_type=gate_type, kind="begin")
    if first["snapshot"] is None or first["current_identity"] is None:
        raise PaperIntervalError("; ".join(first["errors"]))
    snapshot = decode_paper_snapshot(first["snapshot"])
    if not snapshot.writer_lease_held or snapshot.runtime_state.value != "running":
        raise PaperIntervalError("begin requires an observed running attempt with its writer lease")
    interval_dir.mkdir(parents=True, exist_ok=False)
    first["paths"] = paths
    _write_once(interval_dir / "observations.jsonl", _canonical(first) + b"\n")
    return first


def _read_observations(payload: bytes) -> list[dict[str, Any]]:
    if not payload.endswith(b"\n"):
        raise PaperIntervalError("observation write is incomplete")
    rows = [json.loads(line) for line in payload.splitlines()]
    if not rows or rows[0]["kind"] != "begin":
        raise PaperIntervalError("interval has no begin observation")
    previous = None
    for index, row in enumerate(rows):
        stamp = datetime.fromisoformat(row["captured_at"])
        if (
            row["snapshot"] is not None
            and decode_paper_snapshot(row["snapshot"]).captured_at != stamp
        ):
            raise PaperIntervalError("observation timestamp conflicts")
        if row["current_identity"] is not None:
            decode_gate_identity(row["current_identity"])
        if previous is not None and stamp <= previous:
            raise PaperIntervalError("observation times must strictly increase")
        if index and row["kind"] not in ("observe", "end"):
            raise PaperIntervalError("unexpected interval observation")
        if row["kind"] == "end" and index != len(rows) - 1:
            raise PaperIntervalError("observations follow the sealed end")
        previous = stamp
    return rows


def observe_interval(interval_dir: Path, *, end: bool = False) -> dict[str, Any]:
    with (interval_dir / "observations.jsonl").open("r+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rows = _read_observations(stream.read())
        if rows[-1]["kind"] == "end":
            raise PaperIntervalError("interval is already sealed")
        locked = decode_gate_identity(rows[0]["current_identity"])
        row = _observe(
            rows[0]["paths"],
            locked,
            gate_id=locked.gate_id,
            gate_type=locked.gate_type,
            kind="end" if end else "observe",
        )
        if datetime.fromisoformat(row["captured_at"]) <= datetime.fromisoformat(
            rows[-1]["captured_at"]
        ):
            raise PaperIntervalError("clock did not advance")
        stream.write(_canonical(row) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    if end and row["kind"] != "end":
        raise PaperIntervalError(
            "end observation unavailable; failure retained, interval remains open"
        )
    return row


def _sealed(interval_dir: Path) -> list[dict[str, Any]]:
    rows = _read_observations((interval_dir / "observations.jsonl").read_bytes())
    if rows[-1]["kind"] != "end":
        raise PaperIntervalError("seal the running boundary before settling or evaluating")
    return rows


def _history(
    rows: list[dict[str, Any]], backups: dict[str, Path], interval_dir: Path
) -> tuple[PaperIntervalHistory, list[str]]:
    locked = decode_gate_identity(rows[0]["current_identity"])
    start = locked.started_at
    end = decode_paper_snapshot(rows[-1]["snapshot"]).captured_at
    baseline = rows[0]["inventory"]
    selected = {name for name, item in baseline.items() if item is None or item["lease_held"]}
    for row in rows[1:]:
        selected.update(set(row["inventory"]) - set(baseline))
    errors = [error for row in rows for error in row["errors"]]
    previous_launch = rows[0]["launch_history"]
    previous_inventory = set(baseline) & selected
    for row in rows[1:]:
        if not previous_inventory <= set(row["inventory"]):
            errors.append("attempt inventory disappeared during the interval")
        previous_inventory.update(set(row["inventory"]) & selected)
        launch = row["launch_history"]
        if previous_launch is not None and (
            launch is None or not launch.startswith(previous_launch)
        ):
            errors.append("launch inventory was truncated or replaced")
        if launch is not None:
            if previous_launch is not None and launch.startswith(previous_launch):
                for line in launch[len(previous_launch) :].splitlines():
                    try:
                        name = json.loads(line)["run_id"]
                        selected.add(name)
                        if name not in row["inventory"]:
                            errors.append(f"launch inventory missing: {name}")
                    except (ValueError, KeyError, TypeError):
                        errors.append("unreadable appended launch inventory")
            previous_launch = launch
    attempts: list[PaperAttemptEvidence] = []
    associations: list[PaperAttemptAssociation] = []
    for name in sorted(selected):
        entries = [row["inventory"].get(name) for row in rows]
        available = [item for item in entries if item is not None]
        if not available:
            errors.append(f"no readable binding for {name}")
            continue
        binding = _binding_from_document(available[0]["binding"], field="observed binding")
        if any(item["binding"] != available[0]["binding"] for item in available):
            errors.append(f"attempt binding changed: {name}")
        attempts.append(
            PaperAttemptEvidence(binding, backups.get(name, interval_dir / "missing-backup" / name))
        )
        live_rows = [
            row
            for row in rows
            if row["snapshot"] is not None
            and row["snapshot"]["run_id"] == name
            and row["snapshot"]["writer_lease_held"]
        ]
        if not live_rows:
            # The manifest/exec-before event alone cannot resolve provenance or coverage.
            continue
        begin = decode_paper_snapshot(live_rows[0]["snapshot"]).captured_at
        finish = decode_paper_snapshot(live_rows[-1]["snapshot"]).captured_at
        for row, item in zip(rows, entries, strict=True):
            if (
                item is not None
                and not item["lease_held"]
                and datetime.fromisoformat(row["captured_at"]) >= begin
            ):
                # Use the runtime's own ended_at, never backup creation or
                # exec-before launch time. A crash has no such terminal time;
                # its incomplete settled journal supplies the certain incident.
                observed = datetime.fromisoformat(row["captured_at"])
                stamp = item["ended_at"]
                stopped_at = datetime.fromisoformat(stamp) if stamp else None
                if stopped_at is not None and begin < stopped_at <= observed:
                    finish = stopped_at
                else:
                    finish = observed
                    errors.append(f"no execution end timestamp for {name}")
                break
        if begin < finish:
            associations.append(
                PaperAttemptAssociation(
                    binding,
                    begin,
                    finish,
                    decode_gate_identity(live_rows[0]["current_identity"]),
                )
            )
    return PaperIntervalHistory(
        locked,
        end if errors else start,
        end,
        tuple(attempts),
        tuple(associations),
        observations=tuple(
            PaperIntervalObservation(
                None if row["snapshot"] is None else decode_paper_snapshot(row["snapshot"]),
                decode_gate_identity(row["current_identity"]),
                datetime.fromisoformat(row["captured_at"]) if row["snapshot"] is None else None,
            )
            for row in rows
            if row["current_identity"] is not None
        ),
    ), errors


def settle_interval(interval_dir: Path, backup_dirs: list[Path]) -> dict[str, Any]:
    rows = _sealed(interval_dir)
    backups: dict[str, Path] = {}
    observed = {name: item for row in rows for name, item in row["inventory"].items() if item}
    for path in backup_dirs:
        manifest = inspect_paper_backup(path)
        name = manifest.run_id.value
        binding = {
            "run_id": name,
            "lineage_sha256": manifest.lineage_sha256.value,
            "manifest_sha256": manifest.attempt_manifest_sha256.value,
        }
        if name not in observed or binding != observed[name]["binding"]:
            raise PaperIntervalError("backup is not bound to an observed attempt")
        if name in backups:
            raise PaperIntervalError("provide one explicit settled backup per attempt")
        backups[name] = path.resolve()
    history, errors = _history(rows, backups, interval_dir)
    _write_once(interval_dir / "history.json", canonical_paper_interval_history_bytes(history))
    return {"history": history.document(), "inventory_errors": errors}


def evaluate_interval(interval_dir: Path) -> dict[str, Any]:
    rows = _sealed(interval_dir)
    history = decode_paper_interval_history(
        json.loads((interval_dir / "history.json").read_bytes())
    )
    # Reconstruct from the retained observations, so an accidentally shortened
    # inventory/history file cannot silently certify a different interval.
    rebuilt, errors = _history(
        rows,
        {item.binding.reference.run_id.value: item.backup_dir for item in history.attempts},
        interval_dir,
    )
    if rebuilt != history:
        raise PaperIntervalError("saved history conflicts with the collected observations")
    boundary = decode_paper_snapshot(rows[-1]["snapshot"])
    current = decode_gate_identity(rows[-1]["current_identity"])
    verdict = evaluate_gate(
        boundary, history.identity, current, history, evaluated_at=boundary.captured_at
    )
    try:
        current_status = read_paper_status(_run_dir(rows[0]["paths"]))
    except (OSError, ValueError, PaperSessionError) as error:
        current_status = {"unavailable": str(error)}
    return {
        "ended_at": boundary.captured_at.isoformat(),
        "boundary_snapshot": boundary.document(),
        "boundary_identity": current.document(),
        "history": history.document(),
        "historical_verdict": verdict.document(),
        "inventory_errors": errors,
        "current_status": current_status,
        "backup_freshness_source": (
            "Saved snapshot: newest verified copy in the configured backup root; "
            "not the active run's recovery point. Settled backups do not refresh it."
        ),
    }
