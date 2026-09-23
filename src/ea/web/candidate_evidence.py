"""Read-only Candidate evidence verification shared by research and guarded loading.

No service startup, strategy catalog scan, or executable package validation occurs here.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from ea.product.reporting import BacktestReportV1
from ea.product.round_trip_report import BacktestReportV2, BacktestReportV3
from ea.product.scenario import scenario_digest_domain
from ea.strategy.package import read_regular
from ea.strategy.registry import project_parameters
from ea.web import candidates
from ea.web.holdout import compatible, decode_holdout, require_chronology


class CandidateEvidenceReader:
    def __init__(self, workspace: Path) -> None:
        if (
            not workspace.is_absolute()
            or not workspace.is_dir()
            or workspace.resolve(strict=True) != workspace
        ):
            raise ValueError("candidate workspace must be an existing resolved directory")
        self.workspace = workspace
        self.jobs_dir = workspace / "jobs"
        self.inputs_dir = workspace / "inputs"
        self.reports_dir = workspace / "reports"

    def _job_path(self, job_id: str) -> Path:
        bundle = self.jobs_dir / job_id
        return bundle / "job.json" if bundle.is_dir() else self.jobs_dir / f"{job_id}.json"

    def read(self, path: Path) -> bytes:
        if not path.is_relative_to(self.workspace):
            raise ValueError("candidate evidence is outside workspace")
        for parent in (path, *path.parents):
            if parent.is_symlink():
                raise ValueError("candidate evidence path is not regular")
            if parent == self.workspace:
                break
        return read_regular(path, path.parent)

    def job(self, job_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        candidates._uuid(job_id)
        from ea.web.service import _decode_job

        job = _decode_job(self.read(self._job_path(job_id)))
        if (
            job.job_id != job_id
            or job.status != "succeeded"
            or job.input_snapshot_bytes is None
            or job.schema
            not in {"ea.local-web-job.v3", "ea.local-web-job.v4", "ea.local-web-job.v5"}
            or job.engine_run_id != job.attempt_id
        ):
            raise ValueError("candidate requires completed snapshot and path evidence")
        snapshot = json.loads(job.input_snapshot_bytes)
        scenario = snapshot["scenario"]
        identity = snapshot["identity"]
        scenario_sha = sha256(
            scenario_digest_domain(scenario["schema_version"])
            + candidates.canonical(scenario).rstrip(b"\n")
        ).hexdigest()
        if scenario_sha != identity["scenario_sha256"]:
            raise ValueError("candidate scenario identity conflicts")
        report_bytes = self.read(self.reports_dir / job_id / "report.json")
        version = {"ea.local-web-job.v3": 1, "ea.local-web-job.v4": 2, "ea.local-web-job.v5": 3}[
            job.schema
        ]
        {1: BacktestReportV1, 2: BacktestReportV2, 3: BacktestReportV3}[version](
            report_bytes, b"\n"
        )
        report = json.loads(report_bytes)
        path_bytes = self.read(self.reports_dir / job_id / "equity-path.json")
        path = json.loads(path_bytes)
        candidates.validate_path(path, report, version)
        source = report["source"]
        fingerprint = scenario["data"]["fingerprint"]
        if (
            sha256(report_bytes).hexdigest() != job.report_sha256
            or sha256(path_bytes).hexdigest() != job.equity_path_sha256
            or candidates.canonical(path) != path_bytes
            or path["schema"] != f"ea.backtest-equity-path.v{version}"
            or path["schema_version"] != version
            or report["run_id"] != job.engine_run_id
            or path["run_id"] != job.engine_run_id
            or path["report_sha256"] != job.report_sha256
            or path["semantic_outcome_sha256"] != report["completion"]["semantic_outcome_sha256"]
            or source["scenario_sha256"] != scenario_sha
            or path["scenario_sha256"] != scenario_sha
            or source["data"]["fingerprint"] != fingerprint
            or identity["data_sha256"] != fingerprint["sha256"]
            or identity["record_count"] != fingerprint["record_count"]
            or path["data_sha256"] != fingerprint["sha256"]
            or path["record_count"] != fingerprint["record_count"]
            or source["instrument"] != scenario["instrument"]
            or source["strategy"] != scenario["strategy"]
            or (
                version == 1
                and (
                    source["replay_window"]["start_inclusive"] != scenario["data"]["start_utc"]
                    or source["replay_window"]["end_exclusive"] != scenario["data"]["end_utc"]
                )
            )
        ):
            raise ValueError("candidate evidence identities conflict")
        strategy = scenario["strategy"]
        implementation = {
            "kind": "builtin",
            "code_sha256": source["code_sha256"],
            "distribution": source["distribution"],
        }
        if "source" in strategy:
            package = self.read(self.inputs_dir / f"{job_id}.eastrategy")
            if sha256(package).hexdigest() != strategy["source"]["artifact_sha256"]:
                raise ValueError("candidate frozen package identity conflicts")
            implementation.update(
                kind="local",
                package_id=strategy["source"]["package_id"],
                artifact_sha256=strategy["source"]["artifact_sha256"],
            )
        parameters = strategy.get("parameters")
        if parameters is None:
            parameters = project_parameters(strategy)
        strategy_identity = {
            "id": strategy["id"],
            "version": strategy.get("version", 1),
            "parameters": parameters,
            "implementation": implementation,
        }
        evidence = {
            "job_id": job_id,
            "run_id": job.engine_run_id,
            "input_sha256": job.input_sha256,
            "scenario_sha256": scenario_sha,
            "data_sha256": fingerprint["sha256"],
            "record_count": fingerprint["record_count"],
            "report_sha256": job.report_sha256,
            "equity_path_sha256": job.equity_path_sha256,
            "code_sha256": source["code_sha256"],
            "distribution": source["distribution"],
        }
        return evidence, strategy_identity, scenario

    def projection(self, validation_id: str, holdout_job_id: str) -> dict[str, Any]:
        candidates._uuid(validation_id)
        candidates._uuid(holdout_job_id)
        payload = self.read(self.jobs_dir / holdout_job_id / "holdout.json")
        current = decode_holdout(payload)
        if (
            current.validation_id != validation_id
            or current.holdout_job_id != holdout_job_id
            or candidates.canonical(current.document()) != payload
        ):
            raise ValueError("candidate holdout relation conflicts")
        source, strategy, source_scenario = self.job(current.source_job_id)
        holdout, holdout_strategy, holdout_scenario = self.job(current.holdout_job_id)
        if strategy != holdout_strategy or not compatible(source_scenario, holdout_scenario):
            raise ValueError("candidate source and holdout configurations conflict")
        require_chronology(source_scenario, holdout_scenario)
        projection = {
            "schema": "ea.research-candidate.v1",
            "schema_version": 1,
            "strategy": strategy,
            "source": source,
            "holdout": holdout,
            "relationship": {"validation_id": validation_id, "sha256": sha256(payload).hexdigest()},
        }
        candidates.validate_projection(projection)
        return projection

    def record(self, candidate_id: str) -> dict[str, Any]:
        candidates._uuid(candidate_id)
        record = candidates.decode_record(
            self.read(self.workspace / "candidates" / f"{candidate_id}.json")
        )
        projection = record["projection"]
        if record["candidate_id"] != candidate_id or projection != self.projection(
            projection["relationship"]["validation_id"], projection["holdout"]["job_id"]
        ):
            raise ValueError("candidate pinned evidence is unavailable")
        return record
