"""RADIAN local product workspace: read-only aggregation over existing authorities.

Every document produced here is derived by reading the existing Web service,
the supervised Paper runtime and the durable alert stream. This module computes
no economics, holds no account authority and mutates nothing: it is the thin
read surface the RADIAN workspace pages bind to.

Absence is represented explicitly (``available: false`` plus a reason), never
as zero, empty success, or sample data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ea.product.paper_session import PaperSessionError, read_paper_status

_EVENT_LIMIT = 100
_EVENT_LIMIT_MAX = 500

# The bounded set of keys forwarded from one observed paper status document.
# Anything else is excluded rather than passed through unexamined.
_PAPER_STATUS_KEYS = (
    "state",
    "reason",
    "cash",
    "equity",
    "position",
    "currency",
    "symbol",
    "valuation_price",
    "valuation_time",
    "orders",
    "fills",
    "observed_fills",
    "duplicate_facts",
    "completed_round_trips",
    "ledger_sequence",
    "internal_ledger_sequence",
    "market_events",
    "fact_events",
    "dispatch_sequence",
    "acknowledged_dispatch_sequence",
    "pending_facts",
    "pending_orders",
    "heartbeat_count",
    "incomplete",
    "risk_halted",
    "kill_switch_halted",
    "reconciliation",
    "operational_log_failures",
    "terminal_durable",
    "account_id",
    "candidate_id",
    "run_id",
    "started_at",
    "ended_at",
    "updated_at",
)


def _newest_run_dir(runtime_root: Path, run_id: str | None = None) -> Path | None:
    if run_id is not None:
        if Path(run_id).name != run_id or run_id in {".", ".."}:
            raise ValueError("run identity must be one directory name")
        candidate = runtime_root / run_id
        if candidate.is_symlink() or candidate.resolve().parent != runtime_root.resolve():
            raise ValueError("run directory is outside configured runtime")
        return candidate if candidate.is_dir() else None
    try:
        children = [
            child for child in runtime_root.iterdir() if child.is_dir() and not child.is_symlink()
        ]
    except OSError:
        return None
    if not children:
        return None
    return max(children, key=lambda child: child.stat().st_mtime)


def paper_overview(runtime_root: Path | None, run_id: str | None = None) -> dict[str, Any]:
    """Project one observed run from the supervised Paper runtime, read-only.

    The returned document reuses the existing ``read_paper_status`` observation
    (bound manifest identity, live writer lease, re-observed health). A missing
    root, missing run directory, or unreadable state is an explicit
    ``available: false`` with a reason.
    """
    if runtime_root is None:
        return {"available": False, "reason": "runtime_root_not_configured"}
    run_dir = _newest_run_dir(runtime_root, run_id=run_id)
    if run_dir is None:
        return {"available": False, "reason": "no_supervised_run_directories"}
    try:
        status = read_paper_status(run_dir)
    except (PaperSessionError, OSError, ValueError) as error:
        return {
            "available": False,
            "reason": "run_state_unreadable",
            "run_id": run_dir.name if run_id is None else run_id,
            "detail": type(error).__name__,
        }
    projected = {key: status[key] for key in _PAPER_STATUS_KEYS if key in status}
    projected["lease_held"] = bool(status.get("lease_held"))
    projected["health"] = status.get("health")
    return {"available": True, **projected}


def paper_events(
    runtime_root: Path | None,
    run_id: str | None = None,
    limit: int = _EVENT_LIMIT,
) -> dict[str, Any]:
    """Return the tail of one run's correlated operational log, read-only."""
    if runtime_root is None:
        return {"available": False, "reason": "runtime_root_not_configured"}
    run_dir = _newest_run_dir(runtime_root, run_id=run_id)
    if run_dir is None:
        return {"available": False, "reason": "no_supervised_run_directories"}
    path = run_dir / "operational.jsonl"
    if not path.is_file():
        return {"available": False, "run_id": run_dir.name, "reason": "operational_log_missing"}
    bounded = max(1, min(limit, _EVENT_LIMIT_MAX))
    try:
        payload = path.read_bytes()
    except OSError:
        return {"available": False, "run_id": run_dir.name, "reason": "operational_log_unreadable"}
    lines = payload.decode("utf-8", errors="replace").splitlines()
    events: list[dict[str, Any]] = []
    for line in lines[-bounded:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {
        "available": True,
        "run_id": run_dir.name,
        "events": events,
        "count": len(events),
        "total_lines": len(lines),
        "truncated": len(lines) > bounded,
    }
