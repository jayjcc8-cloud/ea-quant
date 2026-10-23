"""Screen-shaped research reads over the existing strategy and execution owners.

No independent lifecycle or persisted summary: a draft is not an experiment and
an experiment's confirmation is not proof that its job or report succeeded.
"""

from __future__ import annotations

from typing import Any

from ea.web.quine import QuineError
from ea.web.service import JobNotFoundError, WebService
from ea.web.user_strategies import IntentDraft, UserStrategy, UserStrategyService


class ResearchWorkbench:
    def __init__(self, strategies: UserStrategyService, jobs: WebService) -> None:
        self.strategies = strategies
        self.jobs = jobs

    def summary(self, research: UserStrategy) -> dict[str, Any]:
        draft = research.draft
        intent = isinstance(draft, IntentDraft)
        pending = sum(item.get("status") == "open" for item in research.proposals)
        latest = None
        if research.experiments:
            record = research.experiments[-1]
            latest = {
                "launch_intent_id": record.get("launch_intent_id"),
                "job_id": record.get("job_id"),
                "draft_revision": (record.get("payload") or {}).get("draft_revision"),
                "launch_status": record.get("status"),
                "job_status": None,
                "report_ready": False,
                "read_error": None,
            }
            if (
                record.get("owner") == "quine"
                and record.get("job_id")
                and record.get("status")
                in {
                    "confirmed",
                    "running",
                    "stopping",
                    "failed",
                }
            ):
                # list() has already reconciled these against their Rust owner.
                # Unknown/unconfirmed/cancelled keep their own vocabulary below.
                latest["job_status"] = (
                    "accepted" if record["status"] == "confirmed" else record["status"]
                )
            elif record.get("status") == "created" and record.get("job_id"):
                try:
                    job = self.jobs.get_job(str(record["job_id"]))
                    if job.job_id != record["job_id"]:
                        raise ValueError("读取到的任务与这条运行不一致。")
                    latest["job_status"] = job.status
                    # Rust's reconciled projection additionally verifies the file.
                    latest["report_ready"] = (
                        bool(record.get("report_ready"))
                        if record.get("owner") == "quine"
                        else bool(job.document(presentation=True).get("report_ready"))
                    )
                except (JobNotFoundError, OSError, ValueError, QuineError) as error:
                    latest["read_error"] = str(error)
            elif record.get("status") in {"unknown", "unconfirmed"}:
                latest["read_error"] = (
                    record.get("message") or "Experiment outcome is not confirmed."
                )
        next_step = "draft" if intent else "review" if pending else "experiment"
        if latest:
            if (
                latest["read_error"]
                or latest["launch_status"] in {"pending", "dispatched"}
                or latest["job_status"] in {"accepted", "running", "stopping", "failed"}
                or (latest["job_status"] == "succeeded" and not latest["report_ready"])
            ):
                next_step = "inspect"
            elif latest["report_ready"]:
                next_step = "evidence"
        return {
            "user_strategy_id": research.user_strategy_id,
            "name": research.name,
            "intent": research.intent,
            "exchange_id": research.exchange_id,
            "updated_at": research.updated_at,
            "instrument": None
            if isinstance(draft, IntentDraft)
            else {"venue": draft.venue, "symbol": draft.symbol},
            "draft": {
                "kind": "intent" if intent else "configured",
                "revision": draft.revision,
                "pending_proposals": pending,
            },
            "experiment_count": len(research.experiments),
            "latest_experiment": latest,
            "next_step": next_step,
        }

    def collection(
        self, *, q: str = "", exchange_id: str | None = None, sort: str = "updated"
    ) -> dict[str, Any]:
        records = self.strategies.list()
        query = q.strip().casefold()
        matching = []
        for item in records:
            symbol = (
                ""
                if isinstance(item.draft, IntentDraft)
                else f"{item.draft.venue}:{item.draft.symbol}"
            )
            if exchange_id is not None and item.exchange_id != exchange_id:
                continue
            if query not in f"{item.name} {item.intent} {symbol}".casefold():
                continue
            matching.append(item)
        # Stable identity tie-breaks remain stable even when timestamps are equal.
        matching.sort(key=lambda item: item.user_strategy_id)
        matching.sort(
            key=lambda item: item.name.casefold() if sort == "name" else item.updated_at,
            reverse=sort != "name",
        )
        return {
            "items": [self.summary(item) for item in matching],
            "total": len(records),
            "matched": len(matching),
        }

    def references(self, job_id: str | None = None) -> dict[str, Any]:
        """Only identities needed by report associations and resource provenance.

        Preserve duplicate frozen references: ambiguity must never be resolved
        by picking the first matching research or by deduplicating its records.
        """
        result = []
        for item in self.strategies.list():
            experiments = []
            for record in item.experiments:
                if job_id is not None and record.get("job_id") != job_id:
                    continue
                payload = record.get("payload") or {}
                identity = payload.get("input_identity") or {}
                experiments.append(
                    {
                        "job_id": record.get("job_id"),
                        "launch_intent_id": record.get("launch_intent_id"),
                        "payload": {
                            "draft_revision": payload.get("draft_revision"),
                            "scenario_id": payload.get("scenario_id"),
                            "input_identity": {
                                key: identity.get(key) for key in ("dataset_id", "source_sha256")
                            },
                        },
                    }
                )
            if job_id is not None and not experiments:
                continue
            draft = item.draft.document()
            result.append(
                {
                    "user_strategy_id": item.user_strategy_id,
                    "name": item.name,
                    "draft": {
                        key: draft[key]
                        for key in (
                            "kind",
                            "revision",
                            "scenario_id",
                            "dataset_id",
                            "source_sha256",
                        )
                        if key in draft
                    },
                    "experiments": experiments,
                }
            )
        return {"items": result}
