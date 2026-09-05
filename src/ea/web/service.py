"""Bounded scenario registry and persistent single-job Web service state."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from ea.product import (
    BacktestRunFailure,
    BacktestScenarioError,
    LoadedBacktestScenario,
    generate_backtest_report,
    load_backtest_scenario,
    run_backtest_scenario,
)

_SCENARIO_ID = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_JOB_STATES = frozenset({"accepted", "running", "succeeded", "failed", "interrupted"})
_ARTIFACTS = frozenset({"report.json", "summary.txt"})


class WebBoundaryError(ValueError):
    """A request crosses a configured local Web boundary."""


class ScenarioNotFoundError(WebBoundaryError):
    """A registered scenario does not exist."""


class InputChangedError(WebBoundaryError):
    """Validated input identity no longer matches the current files."""


class RequestConflictError(WebBoundaryError):
    """An idempotency key was reused with different input."""


class ServiceBusyError(WebBoundaryError):
    """The single active-job slot is occupied."""


class JobNotFoundError(WebBoundaryError):
    """A workspace job does not exist."""


class ReportUnavailableError(WebBoundaryError):
    """A job has no verified immutable report."""


def _canonical_json(document: object) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )


def _directory(path: Path, *, label: str, create: bool) -> Path:
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved = path.resolve(strict=True)
        value = resolved.stat()
    except (OSError, RuntimeError, ValueError):
        raise WebBoundaryError(f"{label} is unavailable") from None
    if not stat.S_ISDIR(value.st_mode):
        raise WebBoundaryError(f"{label} must be a directory")
    return resolved


def _contained(path: Path, root: Path) -> bool:
    try:
        return path.is_relative_to(root)
    except (AttributeError, ValueError):
        return False


def _scenario_identity(scenario: LoadedBacktestScenario) -> dict[str, object]:
    fingerprint = scenario.dataset.selection.fingerprint
    return {
        "scenario_sha256": scenario.scenario_sha256.value,
        "data_sha256": fingerprint.sha256.value,
        "record_count": fingerprint.record_count,
    }


def _scenario_summary(scenario_id: str, scenario: LoadedBacktestScenario) -> dict[str, object]:
    target = scenario.target_quantity
    return {
        "scenario_id": scenario_id,
        "name": Path(scenario_id).stem,
        "valid": True,
        "input_identity": _scenario_identity(scenario),
        "summary": {
            "strategy_id": scenario.strategy_id.value,
            "venue": scenario.instrument.venue.code,
            "symbol": scenario.instrument.symbol,
            "initial_cash": scenario.initial_cash.text,
            "target_quantity": None if target is None else target.text,
            "record_count": scenario.dataset.selection.fingerprint.record_count,
        },
    }


class ScenarioRegistry:
    """Resolve only immediate YAML files registered under one authorized root."""

    def __init__(self, root: Path) -> None:
        self.root = _directory(root, label="scenario root", create=False)

    def _path(self, scenario_id: str) -> Path:
        if (
            not 1 <= len(scenario_id) <= 128
            or any(character not in _SCENARIO_ID for character in scenario_id)
            or Path(scenario_id).suffix not in {".yaml", ".yml"}
        ):
            raise ScenarioNotFoundError("scenario is not registered")
        candidate = self.root / scenario_id
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise ScenarioNotFoundError("scenario is not registered") from None
        if not _contained(resolved, self.root) or not resolved.is_file():
            raise ScenarioNotFoundError("scenario is not registered")
        return resolved

    def load(self, scenario_id: str) -> LoadedBacktestScenario:
        scenario = load_backtest_scenario(self._path(scenario_id))
        if not _contained(scenario.scenario_path, self.root) or not _contained(
            scenario.data_path, self.root
        ):
            raise BacktestScenarioError("scenario input resolves outside the authorized root")
        return scenario

    def validate(self, scenario_id: str) -> dict[str, object]:
        return _scenario_summary(scenario_id, self.load(scenario_id))

    def list(self) -> list[dict[str, object]]:
        try:
            candidates = sorted(
                path.name
                for path in self.root.iterdir()
                if path.suffix in {".yaml", ".yml"} and not path.name.startswith(".")
            )
        except OSError:
            raise WebBoundaryError("scenario root cannot be listed") from None
        if len(candidates) > 100:
            raise WebBoundaryError("scenario root exceeds the 100-file limit")
        results: list[dict[str, object]] = []
        for scenario_id in candidates:
            try:
                results.append(self.validate(scenario_id))
            except (BacktestScenarioError, ScenarioNotFoundError) as error:
                message = str(error)
                if message == "data fingerprint conflicts with captured replay selection":
                    message = "data fingerprint conflicts with selected market data"
                results.append(
                    {
                        "scenario_id": scenario_id,
                        "name": Path(scenario_id).stem,
                        "valid": False,
                        "error_code": "scenario_invalid",
                        "message": message,
                    }
                )
        return results


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    request_id: str
    scenario_id: str
    input_identity: dict[str, object]
    status: str
    engine_run_id: str | None = None
    report_sha256: str | None = None
    error_code: str | None = None
    message: str | None = None

    def document(self) -> dict[str, object]:
        return {
            "schema": "ea.local-web-job.v1",
            "job_id": self.job_id,
            "request_id": self.request_id,
            "scenario_id": self.scenario_id,
            "input_identity": self.input_identity,
            "status": self.status,
            "engine_run_id": self.engine_run_id,
            "report_sha256": self.report_sha256,
            "error_code": self.error_code,
            "message": self.message,
            "report_ready": self.status == "succeeded" and self.report_sha256 is not None,
        }


def _decode_job(payload: bytes) -> JobRecord:
    try:
        document = json.loads(payload)
        if type(document) is not dict or _canonical_json(document) != payload:
            raise ValueError
        if document.get("schema") != "ea.local-web-job.v1":
            raise ValueError
        status_value = document.get("status")
        identity = document.get("input_identity")
        if type(status_value) is not str or status_value not in _JOB_STATES:
            raise ValueError
        if type(identity) is not dict:
            raise ValueError
        fields = ("job_id", "request_id", "scenario_id")
        if any(type(document.get(field)) is not str for field in fields):
            raise ValueError
        optional = ("engine_run_id", "report_sha256", "error_code", "message")
        if any(
            document.get(field) is not None and type(document.get(field)) is not str
            for field in optional
        ):
            raise ValueError
        if type(document.get("report_ready")) is not bool:
            raise ValueError
        return JobRecord(
            job_id=document["job_id"],
            request_id=document["request_id"],
            scenario_id=document["scenario_id"],
            input_identity=identity,
            status=status_value,
            engine_run_id=document.get("engine_run_id"),
            report_sha256=document.get("report_sha256"),
            error_code=document.get("error_code"),
            message=document.get("message"),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise WebBoundaryError("workspace job index is invalid") from None


class WorkspaceLease:
    """Cross-process exclusive writer lease for one Web workspace."""

    def __init__(self, workspace: Path) -> None:
        self._path = workspace / "service.lock"
        self._descriptor: int | None = None

    def acquire(self) -> None:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self._path,
                os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            value = os.fstat(descriptor)
            if not stat.S_ISREG(value.st_mode) or stat.S_IMODE(value.st_mode) != 0o600:
                raise OSError
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._descriptor = descriptor
        except (BlockingIOError, OSError):
            if descriptor is not None:
                os.close(descriptor)
            raise WebBoundaryError(
                "workspace is already owned by another local Web service"
            ) from None

    def release(self) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None


class WebService:
    """Persistent, single-active-job adapter over the existing product functions."""

    def __init__(self, scenario_root: Path, workspace: Path) -> None:
        self.registry = ScenarioRegistry(scenario_root)
        self.workspace = _directory(workspace, label="workspace", create=True)
        self.jobs_dir = _directory(self.workspace / "jobs", label="job index", create=True)
        self.runs_dir = _directory(self.workspace / "runs", label="attempt root", create=True)
        self.reports_dir = _directory(self.workspace / "reports", label="report root", create=True)
        for directory, label in (
            (self.jobs_dir, "job index"),
            (self.runs_dir, "attempt root"),
            (self.reports_dir, "report root"),
        ):
            if not _contained(directory, self.workspace):
                raise WebBoundaryError(f"{label} resolves outside workspace")
        self._lease = WorkspaceLease(self.workspace)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ea-local-web")
        self._lock = threading.RLock()
        self._jobs: dict[str, JobRecord] = {}
        self._requests: dict[str, str] = {}
        self._active: str | None = None

    def start(self) -> None:
        self._lease.acquire()
        try:
            self._load_jobs()
        except Exception:
            self._lease.release()
            raise

    def stop(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)
        self._lease.release()

    def _job_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def _write(self, record: JobRecord) -> None:
        payload = _canonical_json(record.document())
        descriptor, raw_path = tempfile.mkstemp(prefix=".job-", dir=self.jobs_dir)
        pending = Path(raw_path)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(pending, self._job_path(record.job_id))
            directory_descriptor = os.open(self.jobs_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            pending.unlink(missing_ok=True)

    def _store(self, record: JobRecord) -> None:
        self._write(record)
        self._jobs[record.job_id] = record
        self._requests[record.request_id] = record.job_id

    def _load_jobs(self) -> None:
        paths = sorted(self.jobs_dir.glob("*.json"))
        if len(paths) > 1000:
            raise WebBoundaryError("workspace exceeds the 1000-job limit")
        for path in paths:
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
                    raise OSError
                record = _decode_job(path.read_bytes())
            except OSError:
                raise WebBoundaryError("workspace job index is unavailable") from None
            if path.name != f"{record.job_id}.json" or record.job_id in self._jobs:
                raise WebBoundaryError("workspace job index conflicts")
            if record.request_id in self._requests:
                raise WebBoundaryError("workspace request index conflicts")
            if record.status in {"accepted", "running"}:
                record = replace(
                    record,
                    status="interrupted",
                    error_code="service_restarted",
                    message="local service stopped before completion; the job was not rerun",
                )
                self._write(record)
            self._jobs[record.job_id] = record
            self._requests[record.request_id] = record.job_id

    def list_jobs(self) -> list[dict[str, object]]:
        with self._lock:
            return [self._jobs[key].document() for key in sorted(self._jobs, reverse=True)]

    def get_job(self, job_id: str) -> JobRecord:
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError:
                raise JobNotFoundError("job was not found in this workspace") from None

    def create_job(
        self,
        *,
        scenario_id: str,
        input_identity: dict[str, object],
        request_id: str,
    ) -> tuple[JobRecord, bool]:
        with self._lock:
            existing_id = self._requests.get(request_id)
            if existing_id is not None:
                existing = self._jobs[existing_id]
                if existing.scenario_id != scenario_id or existing.input_identity != input_identity:
                    raise RequestConflictError("request_id is already bound to different input")
                return existing, False
            if self._active is not None:
                raise ServiceBusyError("one local backtest is already active")
            scenario = self.registry.load(scenario_id)
            if _scenario_identity(scenario) != input_identity:
                raise InputChangedError("scenario input changed; validate it again")
            record = JobRecord(
                job_id=str(uuid4()),
                request_id=request_id,
                scenario_id=scenario_id,
                input_identity=input_identity,
                status="accepted",
            )
            self._store(record)
            self._active = record.job_id
            self._executor.submit(self._run, record.job_id)
            return record, True

    def _run(self, job_id: str) -> None:
        with self._lock:
            record = replace(self._jobs[job_id], status="running")
            self._store(record)
        try:
            scenario = self.registry.load(record.scenario_id)
            if _scenario_identity(scenario) != record.input_identity:
                raise InputChangedError("scenario input changed; validate it again")
            result = run_backtest_scenario(scenario, self.runs_dir)
            run_id = result.output_directory.name
            report_dir = self.reports_dir / job_id
            generated = generate_backtest_report(result.output_directory, report_dir)
            payload = generated.report.canonical_bytes
            document = json.loads(payload)
            if (
                type(document) is not dict
                or document.get("schema") != "ea.backtest-report.v1"
                or document.get("run_id") != run_id
            ):
                raise ReportUnavailableError("generated report identity conflicts")
            completed = replace(
                record,
                status="succeeded",
                engine_run_id=run_id,
                report_sha256=sha256(payload).hexdigest(),
                error_code=None,
                message=None,
            )
        except BacktestRunFailure as error:
            completed = replace(
                record,
                status="failed",
                engine_run_id=error.output_directory.name,
                error_code=error.code.value,
                message=str(error),
            )
        except (BacktestScenarioError, InputChangedError):
            completed = replace(
                record,
                status="failed",
                error_code="input_changed",
                message="scenario input changed; validate it again",
            )
        except Exception:
            completed = replace(
                record,
                status="failed",
                error_code="web.internal_failure",
                message="the local backtest failed unexpectedly",
            )
        with self._lock:
            self._store(completed)
            if self._active == job_id:
                self._active = None

    def report(self, job_id: str) -> bytes:
        record = self.get_job(job_id)
        if record.status != "succeeded" or record.report_sha256 is None:
            raise ReportUnavailableError("verified report is not available for this job")
        path = self.reports_dir / job_id / "report.json"
        try:
            payload = path.read_bytes()
        except OSError:
            raise ReportUnavailableError("verified report is unavailable") from None
        try:
            document: Any = json.loads(payload)
        except json.JSONDecodeError:
            raise ReportUnavailableError("verified report is invalid") from None
        if (
            sha256(payload).hexdigest() != record.report_sha256
            or type(document) is not dict
            or document.get("schema") != "ea.backtest-report.v1"
            or document.get("run_id") != record.engine_run_id
        ):
            raise ReportUnavailableError("verified report identity conflicts")
        return payload

    def artifact(self, job_id: str, name: str) -> Path:
        if name not in _ARTIFACTS:
            raise JobNotFoundError("artifact was not found")
        self.report(job_id)
        path = self.reports_dir / job_id / name
        if not path.is_file() or path.is_symlink():
            raise ReportUnavailableError("verified artifact is unavailable")
        return path


__all__ = [
    "InputChangedError",
    "JobNotFoundError",
    "ReportUnavailableError",
    "RequestConflictError",
    "ScenarioNotFoundError",
    "ServiceBusyError",
    "WebBoundaryError",
    "WebService",
]
