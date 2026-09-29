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
from typing import Any, cast

from ea.product.paper_alerts import (
    AlertState,
    PaperAlertUnavailable,
    read_alert_stream,
)
from ea.product.paper_session import PaperSessionError, read_paper_status
from ea.web.service import WebService
from ea.web.settings import RadianSettings

_RECENT_WORK_LIMIT = 25
_ATTENTION_LIMIT = 12
_EVENT_LIMIT = 100
_EVENT_LIMIT_MAX = 500
_SEARCH_LIMIT = 20

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
        candidate = runtime_root / run_id
        return candidate if candidate.is_dir() else None
    try:
        children = [child for child in runtime_root.iterdir() if child.is_dir()]
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


def _alert_attention(alerts_stream: Path | None) -> list[dict[str, Any]]:
    if alerts_stream is None:
        return []
    try:
        stream = read_alert_stream(alerts_stream)
    except (PaperAlertUnavailable, OSError, ValueError):
        return []
    items: list[dict[str, Any]] = []
    for alert in stream.alerts:
        if alert.state is not AlertState.ACTIVE:
            continue
        items.append(
            {
                "id": alert.alert_id,
                "kind": "alert",
                "severity": alert.severity.value,
                "message": alert.reason,
                "observation": alert.observation,
                "observed_at": alert.last_seen.isoformat(),
                "state": alert.state.value,
                "operator_action": alert.operator_action,
            }
        )
    return items


def _paper_attention(paper: dict[str, Any]) -> list[dict[str, Any]]:
    if not paper.get("available"):
        return []
    items: list[dict[str, Any]] = []
    run_id = paper.get("run_id")
    if paper.get("state") == "interrupted":
        items.append(
            {
                "id": f"{run_id}:interrupted",
                "kind": "paper",
                "severity": "high",
                "message": (
                    f"Supervised Paper run {run_id} is interrupted: "
                    f"{paper.get('reason') or 'writer lease missing'}"
                ),
                "observed_at": paper.get("updated_at"),
                "run_id": run_id,
            }
        )
    health = paper.get("health")
    if isinstance(health, dict):
        reason_codes = list(health.get("reason_codes") or [])
        for code in reason_codes:
            items.append(
                {
                    "id": f"{run_id}:{code}",
                    "kind": "paper",
                    "severity": "high",
                    "message": f"Paper health reason: {code}",
                    "observed_at": health.get("observed_at"),
                    "run_id": run_id,
                }
            )
        if health.get("projection_stale") and "projection.stale" not in reason_codes:
            items.append(
                {
                    "id": f"{run_id}:stale",
                    "kind": "paper",
                    "severity": "high",
                    "message": "Paper health projection is stale",
                    "observed_at": health.get("observed_at"),
                    "run_id": run_id,
                }
            )
        if health.get("trade_blocking_guard"):
            items.append(
                {
                    "id": f"{run_id}:trade-blocking",
                    "kind": "paper",
                    "severity": "high",
                    "message": (
                        "Paper trade blocking guard engaged: "
                        f"{health['trade_blocking_guard']}"
                    ),
                    "observed_at": health.get("observed_at"),
                    "run_id": run_id,
                }
            )
        if health.get("operator_halt"):
            items.append(
                {
                    "id": f"{run_id}:operator-halt",
                    "kind": "paper",
                    "severity": "high",
                    "message": "Operator halt is engaged on the supervised Paper run",
                    "observed_at": health.get("observed_at"),
                    "run_id": run_id,
                }
            )
    return items


def _job_attention(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for job in jobs:
        if job.get("status") not in {"failed", "interrupted"}:
            continue
        items.append(
            {
                "id": str(job["job_id"]),
                "kind": "job",
                "severity": "high" if job.get("status") == "failed" else "medium",
                "message": job.get("message") or job.get("error_code") or "Run did not succeed",
                "observed_at": job.get("created_at"),
                "job_id": job["job_id"],
                "status": job["status"],
            }
        )
    return items[:5]


def _scenario_label(service: WebService, scenario_id: str) -> dict[str, Any] | None:
    for scenario in (cast(dict[str, Any], item) for item in service.registry.list()):
        if scenario.get("scenario_id") == scenario_id:
            summary = scenario.get("summary") or {}
            strategy = scenario.get("strategy_descriptor") or {}
            return {
                "scenario_id": scenario_id,
                "name": scenario.get("name"),
                "strategy_id": summary.get("strategy_id") or strategy.get("strategy_id"),
                "instrument": (
                    {"venue": summary["venue"], "symbol": summary["symbol"]}
                    if summary.get("venue")
                    else None
                ),
            }
    return None


def overview(
    service: WebService,
    *,
    runtime_root: Path | None = None,
    alerts_stream: Path | None = None,
    settings: RadianSettings | None = None,
) -> dict[str, Any]:
    """Aggregate one RADIAN workspace overview from existing read surfaces."""
    jobs = service.list_jobs()
    batches = service.list_batches()
    holdouts = service.list_holdouts()
    candidates = service.list_candidates()

    recent_work: list[dict[str, Any]] = []
    for job in jobs:
        scenario = _scenario_label(service, str(job.get("scenario_id", "")))
        recent_work.append(
            {
                "id": str(job["job_id"]),
                "kind": "backtest",
                "status": job.get("status"),
                "created_at": job.get("created_at"),
                "scenario_id": job.get("scenario_id"),
                "name": (scenario or {}).get("name"),
                "strategy_id": (scenario or {}).get("strategy_id"),
                "instrument": (scenario or {}).get("instrument"),
                "link": f"/backtests/{job['job_id']}",
            }
        )
    for batch in batches:
        scenario = _scenario_label(service, str(batch.get("scenario_id", "")))
        recent_work.append(
            {
                "id": str(batch["batch_id"]),
                "kind": "batch",
                "status": batch.get("status"),
                "created_at": batch.get("created_at"),
                "scenario_id": batch.get("scenario_id"),
                "name": (scenario or {}).get("name"),
                "strategy_id": (scenario or {}).get("strategy_id"),
                "instrument": (scenario or {}).get("instrument"),
                "member_count": batch.get("member_count"),
                "link": f"/batches/{batch['batch_id']}",
            }
        )
    for holdout in holdouts:
        recent_work.append(
            {
                "id": str(holdout["validation_id"]),
                "kind": "holdout",
                "status": "recorded",
                "created_at": holdout.get("created_at"),
                "source_job_id": holdout.get("source_job_id"),
                "holdout_job_id": holdout.get("holdout_job_id"),
                "link": f"/holdouts/{holdout['validation_id']}",
            }
        )
    for candidate in candidates:
        strategy = (candidate.get("projection") or {}).get("strategy") or {}
        recent_work.append(
            {
                "id": str(candidate["candidate_id"]),
                "kind": "candidate",
                "status": candidate.get("status"),
                "created_at": candidate.get("created_at"),
                "strategy_id": strategy.get("id"),
                "decision": (candidate.get("decision") or {}).get("outcome"),
                "link": f"/candidates/{candidate['candidate_id']}",
            }
        )
    recent_work.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    recent_work = recent_work[:_RECENT_WORK_LIMIT]

    paper = paper_overview(runtime_root)
    attention = _paper_attention(paper) + _job_attention(jobs) + _alert_attention(alerts_stream)
    attention = attention[:_ATTENTION_LIMIT]

    settings_status = (settings or RadianSettings(RadianSettings.default_path())).status()
    return {
        "schema": "radian.workspace-overview.v1",
        "recent_work": recent_work,
        "attention": attention,
        "paper": paper,
        "model": {
            "configured": bool(settings_status.get("configured")),
            "provider": settings_status.get("provider"),
            "model": settings_status.get("model"),
        },
    }


def search(
    service: WebService,
    query: str,
    *,
    limit: int = _SEARCH_LIMIT,
) -> dict[str, Any]:
    """Minimal search over existing queryable objects. No new index, no ranking model."""
    needle = query.strip().lower()
    results: list[dict[str, Any]] = []
    if needle:

        def matches(*values: object) -> bool:
            return any(needle in str(value).lower() for value in values)

        for scenario in (cast(dict[str, Any], item) for item in service.registry.list()):
            summary = scenario.get("summary") or {}
            if matches(
                scenario.get("scenario_id"),
                scenario.get("name"),
                summary.get("strategy_id"),
                summary.get("symbol"),
            ):
                results.append(
                    {
                        "kind": "scenario",
                        "id": scenario["scenario_id"],
                        "label": scenario.get("name") or scenario["scenario_id"],
                        "status": "valid" if scenario.get("valid") else "invalid",
                        "link": "/backtests",
                    }
                )
        for job in service.list_jobs():
            if matches(job.get("job_id"), job.get("scenario_id"), job.get("engine_run_id")):
                results.append(
                    {
                        "kind": "backtest",
                        "id": str(job["job_id"]),
                        "label": f"{job.get('scenario_id')} · {job['job_id']}",
                        "status": job.get("status"),
                        "link": f"/backtests/{job['job_id']}",
                    }
                )
        for batch in service.list_batches():
            if matches(batch.get("batch_id"), batch.get("scenario_id")):
                results.append(
                    {
                        "kind": "batch",
                        "id": str(batch["batch_id"]),
                        "label": f"batch {batch['batch_id']}",
                        "status": batch.get("status"),
                        "link": f"/batches/{batch['batch_id']}",
                    }
                )
        for holdout in service.list_holdouts():
            if matches(holdout.get("validation_id"), holdout.get("source_job_id")):
                results.append(
                    {
                        "kind": "holdout",
                        "id": str(holdout["validation_id"]),
                        "label": f"holdout {holdout['validation_id']}",
                        "status": "recorded",
                        "link": f"/holdouts/{holdout['validation_id']}",
                    }
                )
        for candidate in service.list_candidates():
            strategy = (candidate.get("projection") or {}).get("strategy") or {}
            if matches(candidate.get("candidate_id"), strategy.get("id")):
                results.append(
                    {
                        "kind": "candidate",
                        "id": str(candidate["candidate_id"]),
                        "label": f"{strategy.get('id')} · {candidate['candidate_id'][:12]}…",
                        "status": candidate.get("status"),
                        "link": f"/candidates/{candidate['candidate_id']}",
                    }
                )
    return {"schema": "radian.search.v1", "query": query, "results": results[:limit]}
