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
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from ea.core import RunId
from ea.product import (
    BacktestRunFailure,
    BacktestScenarioError,
    LoadedBacktestScenario,
    generate_backtest_report,
    load_backtest_scenario,
    parameterize_backtest_scenario,
    run_backtest_scenario,
)

_SCENARIO_ID = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_JOB_STATES = frozenset({"accepted", "running", "succeeded", "failed", "interrupted"})
_ARTIFACTS = frozenset({"report.json", "summary.txt"})
_MAX_SCENARIO_BYTES = 64 * 1024
_INPUT_DIGEST_DOMAIN = b"ea.local-web-input.v1\0"


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


class BatchNotFoundError(WebBoundaryError):
    """A workspace experiment batch does not exist."""


class DuplicateBatchRunError(WebBoundaryError):
    """A batch repeats one normalized strategy parameter combination."""


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


def roots_overlap(*roots: Path) -> bool:
    """Return whether any two resolved authorization roots contain one another."""
    return any(
        left.is_relative_to(right) or right.is_relative_to(left)
        for index, left in enumerate(roots)
        for right in roots[index + 1 :]
    )


def _scenario_identity(scenario: LoadedBacktestScenario) -> dict[str, object]:
    fingerprint = scenario.dataset.selection.fingerprint
    return {
        "scenario_sha256": scenario.scenario_sha256.value,
        "data_sha256": fingerprint.sha256.value,
        "record_count": fingerprint.record_count,
    }


def _strategy_parameter_contracts(
    scenario: LoadedBacktestScenario,
    *,
    defaults: LoadedBacktestScenario | None = None,
) -> list[dict[str, object]]:
    if scenario.strategy_id.value != "bounded-long-v1":
        return []
    source = scenario if defaults is None else defaults
    specification = scenario.spec_set.require(scenario.instrument)
    target = scenario.target_quantity
    default_target = source.target_quantity
    return [
        {
            "name": "target_quantity",
            "type": "decimal",
            "default": None if default_target is None else default_target.text,
            "current_value": None if target is None else target.text,
            "minimum": specification.quantity_quantum.text,
            "maximum": None,
        },
        {
            "name": "entry_delay_bars",
            "type": "integer",
            "default": 0,
            "current_value": scenario.entry_delay_bars,
            "minimum": 0,
            "maximum": scenario.entry_delay_bars_maximum,
        },
    ]


def _scenario_summary(
    scenario_id: str,
    scenario: LoadedBacktestScenario,
    *,
    defaults: LoadedBacktestScenario | None = None,
) -> dict[str, object]:
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
            "entry_delay_bars": scenario.entry_delay_bars,
            "record_count": scenario.dataset.selection.fingerprint.record_count,
            "commission": json.loads(scenario.canonical_bytes)["execution"].get("commission"),
        },
        "strategy_parameters": _strategy_parameter_contracts(scenario, defaults=defaults),
    }


def _parameter_values(
    parameters: dict[str, object],
) -> tuple[str, str | None, int | None]:
    initial_cash = parameters.get("initial_cash")
    if type(initial_cash) is not str:
        raise BacktestScenarioError("initial_cash must be an ea-decimal-v1 string")
    strategy_parameters = parameters.get("strategy_parameters")
    legacy_quantity = parameters.get("quantity")
    if strategy_parameters is None:
        if legacy_quantity is not None and type(legacy_quantity) is not str:
            raise BacktestScenarioError("quantity must be an ea-decimal-v1 string")
        return initial_cash, legacy_quantity, None
    if type(strategy_parameters) is not dict:
        raise BacktestScenarioError("strategy_parameters must be a mapping")
    if legacy_quantity is not None:
        raise BacktestScenarioError("quantity conflicts with strategy_parameters")
    target_quantity = strategy_parameters.get("target_quantity")
    entry_delay_bars = strategy_parameters.get("entry_delay_bars")
    if target_quantity is not None and type(target_quantity) is not str:
        raise BacktestScenarioError("target_quantity must be an ea-decimal-v1 string")
    if type(entry_delay_bars) is not int:
        raise BacktestScenarioError("entry_delay_bars must be an integer")
    return initial_cash, target_quantity, entry_delay_bars


def _input_snapshot(
    scenario_id: str,
    scenario: LoadedBacktestScenario,
    source_identity: dict[str, object],
) -> tuple[bytes, str]:
    snapshot = {
        "schema": "ea.local-web-input.v1",
        "scenario_id": scenario_id,
        "source_identity": source_identity,
        "identity": _scenario_identity(scenario),
        "scenario": json.loads(scenario.canonical_bytes),
    }
    payload = _canonical_json(snapshot)
    return payload, sha256(_INPUT_DIGEST_DOMAIN + payload).hexdigest()


def _created_at() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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
            value = resolved.stat()
        except (OSError, RuntimeError, ValueError):
            raise ScenarioNotFoundError("scenario is not registered") from None
        if (
            not _contained(resolved, self.root)
            or not stat.S_ISREG(value.st_mode)
            or value.st_size > _MAX_SCENARIO_BYTES
        ):
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
    schema: str = "ea.local-web-job.v2"
    created_at: str | None = None
    input_snapshot_bytes: bytes | None = None
    input_sha256: str | None = None
    attempt_id: str | None = None
    engine_run_id: str | None = None
    report_sha256: str | None = None
    summary_sha256: str | None = None
    error_code: str | None = None
    message: str | None = None

    def document(self) -> dict[str, object]:
        document: dict[str, object] = {
            "schema": self.schema,
            "job_id": self.job_id,
            "request_id": self.request_id,
            "scenario_id": self.scenario_id,
            "input_identity": self.input_identity,
            "status": self.status,
            "engine_run_id": self.engine_run_id,
            "report_sha256": self.report_sha256,
            "summary_sha256": self.summary_sha256,
            "error_code": self.error_code,
            "message": self.message,
            "report_ready": (
                self.status == "succeeded"
                and self.report_sha256 is not None
                and self.summary_sha256 is not None
            ),
        }
        if self.schema == "ea.local-web-job.v2":
            if (
                self.created_at is None
                or self.input_snapshot_bytes is None
                or self.input_sha256 is None
            ):
                raise RuntimeError("v2 Web job is missing immutable input evidence")
            document.update(
                {
                    "created_at": self.created_at,
                    "input_snapshot": json.loads(self.input_snapshot_bytes),
                    "input_sha256": self.input_sha256,
                    "attempt_id": self.attempt_id,
                }
            )
        return document


@dataclass(frozen=True, slots=True)
class BatchRecord:
    batch_id: str
    created_at: str
    scenario_id: str
    input_identity: dict[str, object]
    member_job_ids: tuple[str, ...]
    schema: str = "ea.local-web-batch.v1"

    def document(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "batch_id": self.batch_id,
            "created_at": self.created_at,
            "scenario_id": self.scenario_id,
            "input_identity": self.input_identity,
            "member_job_ids": list(self.member_job_ids),
        }


@dataclass(frozen=True, slots=True)
class PreparedJob:
    scenario: LoadedBacktestScenario
    input_snapshot_bytes: bytes
    input_sha256: str


def _decode_job(payload: bytes) -> JobRecord:
    try:
        document = json.loads(payload)
        if type(document) is not dict or _canonical_json(document) != payload:
            raise ValueError
        schema = document.get("schema")
        if schema not in {"ea.local-web-job.v1", "ea.local-web-job.v2"}:
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
        optional = (
            "engine_run_id",
            "report_sha256",
            "summary_sha256",
            "error_code",
            "message",
        )
        if any(
            document.get(field) is not None and type(document.get(field)) is not str
            for field in optional
        ):
            raise ValueError
        if type(document.get("report_ready")) is not bool:
            raise ValueError
        created_at: str | None = None
        input_snapshot_bytes: bytes | None = None
        input_sha256: str | None = None
        attempt_id: str | None = None
        if schema == "ea.local-web-job.v2":
            created_at_value = document.get("created_at")
            snapshot = document.get("input_snapshot")
            input_sha256_value = document.get("input_sha256")
            attempt_id_value = document.get("attempt_id")
            if (
                type(created_at_value) is not str
                or type(snapshot) is not dict
                or type(input_sha256_value) is not str
                or (attempt_id_value is not None and type(attempt_id_value) is not str)
            ):
                raise ValueError
            if (
                set(snapshot)
                != {"schema", "scenario_id", "source_identity", "identity", "scenario"}
                or snapshot.get("schema") != "ea.local-web-input.v1"
                or snapshot.get("scenario_id") != document["scenario_id"]
                or snapshot.get("source_identity") != identity
                or type(snapshot.get("identity")) is not dict
                or type(snapshot.get("scenario")) is not dict
            ):
                raise ValueError
            datetime.strptime(created_at_value, "%Y-%m-%dT%H:%M:%S.%fZ")
            input_snapshot_bytes = _canonical_json(snapshot)
            if (
                sha256(_INPUT_DIGEST_DOMAIN + input_snapshot_bytes).hexdigest()
                != input_sha256_value
            ):
                raise ValueError
            created_at = created_at_value
            input_sha256 = input_sha256_value
            attempt_id = attempt_id_value
        return JobRecord(
            job_id=document["job_id"],
            request_id=document["request_id"],
            scenario_id=document["scenario_id"],
            input_identity=identity,
            status=status_value,
            schema=schema,
            created_at=created_at,
            input_snapshot_bytes=input_snapshot_bytes,
            input_sha256=input_sha256,
            attempt_id=attempt_id,
            engine_run_id=document.get("engine_run_id"),
            report_sha256=document.get("report_sha256"),
            summary_sha256=document.get("summary_sha256"),
            error_code=document.get("error_code"),
            message=document.get("message"),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise WebBoundaryError("workspace job index is invalid") from None


def _decode_batch(payload: bytes) -> BatchRecord:
    try:
        document = json.loads(payload)
        if type(document) is not dict or _canonical_json(document) != payload:
            raise ValueError
        if set(document) != {
            "schema",
            "batch_id",
            "created_at",
            "scenario_id",
            "input_identity",
            "member_job_ids",
        }:
            raise ValueError
        if document.get("schema") != "ea.local-web-batch.v1":
            raise ValueError
        fields = ("batch_id", "created_at", "scenario_id")
        if any(type(document.get(field)) is not str for field in fields):
            raise ValueError
        identity = document.get("input_identity")
        member_ids = document.get("member_job_ids")
        if type(identity) is not dict or type(member_ids) is not list:
            raise ValueError
        if (
            not 2 <= len(member_ids) <= 10
            or len(set(member_ids)) != len(member_ids)
            or any(type(member_id) is not str for member_id in member_ids)
        ):
            raise ValueError
        datetime.strptime(document["created_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
        return BatchRecord(
            batch_id=document["batch_id"],
            created_at=document["created_at"],
            scenario_id=document["scenario_id"],
            input_identity=identity,
            member_job_ids=tuple(member_ids),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise WebBoundaryError("workspace batch index is invalid") from None


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
        resolved_scenarios = _directory(scenario_root, label="scenario root", create=False)
        try:
            resolved_workspace = workspace.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            raise WebBoundaryError("workspace is unavailable") from None
        if roots_overlap(resolved_scenarios, resolved_workspace):
            raise WebBoundaryError("scenario and workspace roots must not overlap")
        self.registry = ScenarioRegistry(resolved_scenarios)
        self.workspace = _directory(resolved_workspace, label="workspace", create=True)
        self.jobs_dir = _directory(self.workspace / "jobs", label="job index", create=True)
        self.batches_dir = _directory(self.workspace / "batches", label="batch index", create=True)
        self.inputs_dir = _directory(self.workspace / "inputs", label="input store", create=True)
        self.runs_dir = _directory(self.workspace / "runs", label="attempt root", create=True)
        self.reports_dir = _directory(self.workspace / "reports", label="report root", create=True)
        for directory, label in (
            (self.jobs_dir, "job index"),
            (self.batches_dir, "batch index"),
            (self.inputs_dir, "input store"),
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
        self._batches: dict[str, BatchRecord] = {}
        self._active: str | None = None

    def start(self) -> None:
        self._lease.acquire()
        try:
            self._load_jobs()
            self._load_batches()
        except Exception:
            self._lease.release()
            raise

    def stop(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)
        self._lease.release()

    def _job_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def _batch_path(self, batch_id: str) -> Path:
        return self.batches_dir / f"{batch_id}.json"

    def _materialize_scenario(
        self,
        job_id: str,
        scenario: LoadedBacktestScenario,
    ) -> LoadedBacktestScenario:
        document = json.loads(scenario.canonical_bytes)
        document.pop("canonicalization")
        document["data"]["path"] = str(scenario.data_path)
        path = self.inputs_dir / f"{job_id}.json"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                descriptor = None
                handle.write(_canonical_json(document))
                handle.flush()
                os.fsync(handle.fileno())
            directory_descriptor = os.open(self.inputs_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            materialized = load_backtest_scenario(path)
            if (
                materialized.canonical_bytes != scenario.canonical_bytes
                or materialized.scenario_sha256 != scenario.scenario_sha256
                or materialized.data_path != scenario.data_path
            ):
                raise WebBoundaryError("normalized input identity conflicts")
            return materialized
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
            path.unlink(missing_ok=True)
            raise

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

    def _write_batch(self, record: BatchRecord) -> None:
        payload = _canonical_json(record.document())
        descriptor, raw_path = tempfile.mkstemp(prefix=".batch-", dir=self.batches_dir)
        pending = Path(raw_path)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(pending, self._batch_path(record.batch_id))
            directory_descriptor = os.open(self.batches_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            pending.unlink(missing_ok=True)

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

    def _load_batches(self) -> None:
        paths = sorted(self.batches_dir.glob("*.json"))
        if len(paths) > 1000:
            raise WebBoundaryError("workspace exceeds the 1000-batch limit")
        for path in paths:
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
                    raise OSError
                record = _decode_batch(path.read_bytes())
            except OSError:
                raise WebBoundaryError("workspace batch index is unavailable") from None
            if path.name != f"{record.batch_id}.json" or record.batch_id in self._batches:
                raise WebBoundaryError("workspace batch index conflicts")
            try:
                members = [self._jobs[job_id] for job_id in record.member_job_ids]
            except KeyError:
                raise WebBoundaryError("workspace batch membership conflicts") from None
            if any(
                member.scenario_id != record.scenario_id
                or member.input_identity != record.input_identity
                for member in members
            ):
                raise WebBoundaryError("workspace batch membership conflicts")
            self._batches[record.batch_id] = record

    def list_jobs(self) -> list[dict[str, object]]:
        with self._lock:
            records = sorted(
                self._jobs.values(),
                key=lambda record: (
                    record.created_at is not None,
                    record.created_at or "",
                    record.job_id,
                ),
                reverse=True,
            )
            return [record.document() for record in records]

    def validate_scenario(
        self,
        scenario_id: str,
        *,
        parameters: dict[str, object] | None = None,
    ) -> dict[str, object]:
        scenario = self.registry.load(scenario_id)
        source_summary = _scenario_summary(scenario_id, scenario)
        if parameters is None:
            return source_summary
        initial_cash, quantity, entry_delay_bars = _parameter_values(parameters)
        normalized = parameterize_backtest_scenario(
            scenario,
            initial_cash=initial_cash,
            quantity=quantity,
            entry_delay_bars=entry_delay_bars,
        )
        normalized_summary = _scenario_summary(scenario_id, normalized, defaults=scenario)
        source_summary["summary"] = normalized_summary["summary"]
        source_summary["strategy_parameters"] = normalized_summary["strategy_parameters"]
        source_summary["normalized_input_identity"] = normalized_summary["input_identity"]
        return source_summary

    def get_job(self, job_id: str) -> JobRecord:
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError:
                raise JobNotFoundError("job was not found in this workspace") from None

    def get_batch(self, batch_id: str) -> dict[str, object]:
        with self._lock:
            try:
                record = self._batches[batch_id]
            except KeyError:
                raise BatchNotFoundError("batch was not found in this workspace") from None
            members: list[dict[str, object]] = []
            for job_id in record.member_job_ids:
                member = self._jobs[job_id]
                presentation_status = member.status
                if member.status == "accepted":
                    presentation_status = "queued"
                elif member.status == "failed" and member.error_code == "risk.rejected":
                    presentation_status = "risk.rejected"
                members.append({**member.document(), "presentation_status": presentation_status})
            running = any(member["status"] in {"accepted", "running"} for member in members)
            return {
                **record.document(),
                "member_count": len(members),
                "status": "running" if running else "complete",
                "members": members,
            }

    def _prepare_job(
        self,
        *,
        scenario_id: str,
        input_identity: dict[str, object],
        parameters: dict[str, object] | None,
    ) -> PreparedJob:
        scenario = self.registry.load(scenario_id)
        if _scenario_identity(scenario) != input_identity:
            raise InputChangedError("scenario input changed; validate it again")
        if parameters is not None:
            initial_cash, quantity, entry_delay_bars = _parameter_values(parameters)
            scenario = parameterize_backtest_scenario(
                scenario,
                initial_cash=initial_cash,
                quantity=quantity,
                entry_delay_bars=entry_delay_bars,
            )
        snapshot_bytes, input_sha256 = _input_snapshot(
            scenario_id,
            scenario,
            input_identity,
        )
        return PreparedJob(
            scenario=scenario,
            input_snapshot_bytes=snapshot_bytes,
            input_sha256=input_sha256,
        )

    def create_job(
        self,
        *,
        scenario_id: str,
        input_identity: dict[str, object],
        parameters: dict[str, object] | None = None,
        request_id: str,
    ) -> tuple[JobRecord, bool]:
        with self._lock:
            prepared = self._prepare_job(
                scenario_id=scenario_id,
                input_identity=input_identity,
                parameters=parameters,
            )
            existing_id = self._requests.get(request_id)
            if existing_id is not None:
                existing = self._jobs[existing_id]
                if (
                    existing.scenario_id != scenario_id
                    or existing.input_identity != input_identity
                    or existing.input_snapshot_bytes != prepared.input_snapshot_bytes
                ):
                    raise RequestConflictError("request_id is already bound to different input")
                return existing, False
            if self._active is not None:
                raise ServiceBusyError("one local backtest is already active")
            job_id = str(uuid4())
            attempt_id = RunId(str(uuid4()))
            scenario = self._materialize_scenario(job_id, prepared.scenario)
            record = JobRecord(
                job_id=job_id,
                request_id=request_id,
                scenario_id=scenario_id,
                input_identity=input_identity,
                status="accepted",
                created_at=_created_at(),
                input_snapshot_bytes=prepared.input_snapshot_bytes,
                input_sha256=prepared.input_sha256,
                attempt_id=attempt_id.value,
            )
            self._store(record)
            self._active = record.job_id
            self._executor.submit(self._run, record.job_id, scenario)
            return record, True

    def create_batch(
        self,
        *,
        scenario_id: str,
        input_identity: dict[str, object],
        initial_cash: str,
        runs: list[dict[str, object]],
    ) -> dict[str, object]:
        with self._lock:
            if not 2 <= len(runs) <= 10:
                raise BacktestScenarioError("batch run count must be between 2 and 10")
            if self._active is not None:
                raise ServiceBusyError("one local backtest or batch is already active")
            prepared: list[PreparedJob] = []
            combinations: set[bytes] = set()
            for strategy_parameters in runs:
                item = self._prepare_job(
                    scenario_id=scenario_id,
                    input_identity=input_identity,
                    parameters={
                        "initial_cash": initial_cash,
                        "strategy_parameters": strategy_parameters,
                    },
                )
                strategy = json.loads(item.scenario.canonical_bytes)["strategy"]
                combination = _canonical_json(strategy)
                if combination in combinations:
                    raise DuplicateBatchRunError(
                        "batch contains a duplicate normalized parameter combination"
                    )
                combinations.add(combination)
                prepared.append(item)

            batch_id = str(uuid4())
            records: list[JobRecord] = []
            executable: list[tuple[str, LoadedBacktestScenario]] = []
            try:
                for index, item in enumerate(prepared, start=1):
                    job_id = str(uuid4())
                    scenario = self._materialize_scenario(job_id, item.scenario)
                    record = JobRecord(
                        job_id=job_id,
                        request_id=f"batch-{batch_id}-{index}",
                        scenario_id=scenario_id,
                        input_identity=input_identity,
                        status="accepted",
                        created_at=_created_at(),
                        input_snapshot_bytes=item.input_snapshot_bytes,
                        input_sha256=item.input_sha256,
                        attempt_id=RunId(str(uuid4())).value,
                    )
                    self._store(record)
                    records.append(record)
                    executable.append((job_id, scenario))
                batch = BatchRecord(
                    batch_id=batch_id,
                    created_at=_created_at(),
                    scenario_id=scenario_id,
                    input_identity=input_identity,
                    member_job_ids=tuple(record.job_id for record in records),
                )
                self._write_batch(batch)
                self._batches[batch.batch_id] = batch
                self._active = batch.batch_id
                self._executor.submit(self._run_batch, batch.batch_id, tuple(executable))
            except Exception:
                self._active = None
                self._batches.pop(batch_id, None)
                self._batch_path(batch_id).unlink(missing_ok=True)
                for record in records:
                    self._jobs.pop(record.job_id, None)
                    self._requests.pop(record.request_id, None)
                    self._job_path(record.job_id).unlink(missing_ok=True)
                    (self.inputs_dir / f"{record.job_id}.json").unlink(missing_ok=True)
                raise
            return self.get_batch(batch.batch_id)

    def _run_batch(
        self,
        batch_id: str,
        members: tuple[tuple[str, LoadedBacktestScenario], ...],
    ) -> None:
        try:
            for job_id, scenario in members:
                self._run(job_id, scenario, release_active=False)
        finally:
            with self._lock:
                if self._active == batch_id:
                    self._active = None

    def _run(
        self,
        job_id: str,
        scenario: LoadedBacktestScenario,
        *,
        release_active: bool = True,
    ) -> None:
        with self._lock:
            record = replace(self._jobs[job_id], status="running")
            self._store(record)
        try:
            if record.attempt_id is None:
                raise RuntimeError("accepted Web job has no reserved attempt identity")
            result = run_backtest_scenario(
                scenario,
                self.runs_dir,
                run_id=RunId(record.attempt_id),
            )
            run_id = result.output_directory.name
            if run_id != record.attempt_id:
                raise RuntimeError("engine attempt identity conflicts with accepted Web job")
            record = replace(record, engine_run_id=run_id)
            with self._lock:
                self._store(record)
            try:
                report_dir = self.reports_dir / job_id
                generated = generate_backtest_report(result.output_directory, report_dir)
                payload = generated.report.canonical_bytes
                summary_payload = generated.report.summary_bytes
                document = json.loads(payload)
                if (
                    type(document) is not dict
                    or document.get("schema") != "ea.backtest-report.v1"
                    or document.get("run_id") != run_id
                ):
                    raise ReportUnavailableError("generated report identity conflicts")
            except Exception:
                completed = replace(
                    record,
                    status="failed",
                    report_sha256=None,
                    summary_sha256=None,
                    error_code="report_failed",
                    message="the engine succeeded but the formal report was unavailable",
                )
            else:
                completed = replace(
                    record,
                    status="succeeded",
                    report_sha256=sha256(payload).hexdigest(),
                    summary_sha256=sha256(summary_payload).hexdigest(),
                    error_code=None,
                    message=None,
                )
        except BacktestRunFailure as error:
            if error.output_directory.name != record.attempt_id:
                completed = replace(
                    record,
                    status="failed",
                    error_code="web.internal_failure",
                    message="the local backtest failed unexpectedly",
                )
            else:
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
            if release_active and self._active == job_id:
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

    def artifact(self, job_id: str, name: str) -> bytes:
        if name not in _ARTIFACTS:
            raise JobNotFoundError("artifact was not found")
        report_payload = self.report(job_id)
        if name == "report.json":
            return report_payload
        record = self.get_job(job_id)
        if record.summary_sha256 is None:
            raise ReportUnavailableError("verified summary is not available for this job")
        path = self.reports_dir / job_id / name
        try:
            if not path.is_file() or path.is_symlink():
                raise OSError
            payload = path.read_bytes()
        except OSError:
            raise ReportUnavailableError("verified artifact is unavailable") from None
        if sha256(payload).hexdigest() != record.summary_sha256:
            raise ReportUnavailableError("verified artifact identity conflicts")
        return payload


__all__ = [
    "InputChangedError",
    "JobNotFoundError",
    "BatchNotFoundError",
    "DuplicateBatchRunError",
    "ReportUnavailableError",
    "RequestConflictError",
    "ScenarioNotFoundError",
    "ServiceBusyError",
    "WebBoundaryError",
    "WebService",
]
