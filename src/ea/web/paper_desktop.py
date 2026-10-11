"""App-lifetime local Paper adapter. The existing Paper writer owns all economics.

Request receipts survive a lost response. They are never a queue: a reserved or
unknown request is queried, never re-dispatched. No PID loaded from disk is signalled.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import threading
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from ea.product.candidate import _configuration
from ea.product.paper_run import resume_local_paper
from ea.product.paper_session import read_paper_binding, read_paper_status, request_paper_stop
from ea.strategy.package import read_regular
from ea.web.candidate_evidence import CandidateEvidenceReader
from ea.web.candidates import _uuid
from ea.web.quine import QuineBridge
from ea.web.settings import atomic_private_json
from ea.web.simulation import prepare_simulation


class DesktopPaper:
    def __init__(self, root: Path, workspace: Path, *, quine: QuineBridge | None = None) -> None:
        self.root = root
        self.workspace = workspace
        self.quine = quine
        self.requests = root / "requests"
        self.runs = root / "runs"
        self._mutex = threading.RLock()
        self._lock: Any = None
        self._children: dict[str, subprocess.Popen[bytes]] = {}
        self.closing = False
        self._directories: dict[Path, tuple[int, int]] = {}

    def open(self) -> None:
        with self._mutex:
            for path in (self.root, self.requests, self.runs):
                if not path.is_absolute() or path.resolve() != path or path.is_symlink():
                    raise ValueError("desktop Paper root is not a private resolved directory")
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                if path.stat().st_mode & 0o077:
                    raise ValueError("desktop Paper root must be private")
            self._directories = {
                path: (path.stat().st_dev, path.stat().st_ino)
                for path in (self.root, self.requests, self.runs)
            }
            lock_path = self.root / "session.lock"
            if lock_path.is_symlink():
                raise ValueError("desktop Paper session lock is unsafe")
            self._lock = lock_path.open("a+b")
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._lock.close()
                self._lock = None
                raise ValueError("a desktop Paper process still owns this workspace") from None

    def _check_directories(self) -> None:
        for path in (self.root, self.requests, self.runs):
            if path.is_symlink() or path.resolve(strict=True) != path or not path.is_dir():
                raise ValueError("desktop Paper directory identity changed")
            info = path.stat()
            expected = self._directories.get(path)
            if expected is not None and expected != (info.st_dev, info.st_ino):
                raise ValueError("desktop Paper directory identity changed")

    @staticmethod
    def _publish(path: Path, document: dict[str, Any]) -> None:
        atomic_private_json(path, document)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def reader(self) -> CandidateEvidenceReader:
        return CandidateEvidenceReader(self.workspace, quine=self.quine)

    def prepare(self, candidate_id: str) -> dict[str, Any]:
        reader = self.reader()
        document = prepare_simulation(self.workspace, candidate_id, reader=reader)
        record = reader.record(candidate_id)
        _, _, scenario = reader.job(record["projection"]["source"]["job_id"])
        document.update(
            start_available=not self.closing,
            source_job_id=record["projection"]["source"]["job_id"],
            configuration={
                key: scenario[key]
                for key in ("strategy", "instrument", "funding", "risk", "execution")
            },
            lifetime="application-open-only",
        )
        return document

    @staticmethod
    def _request(value: dict[str, Any]) -> dict[str, Any]:
        from math import isfinite

        expected = {
            "request_id",
            "candidate_id",
            "fingerprint",
            "name",
            "prices",
            "interval_seconds",
        }
        if set(value) != expected:
            raise ValueError("local simulation request fields conflict")
        _uuid(value["request_id"])
        _uuid(value["candidate_id"])
        if type(value["name"]) is not str or not 1 <= len(value["name"].strip()) <= 80:
            raise ValueError("instance name must contain 1..80 characters")
        if type(value["fingerprint"]) is not str or len(value["fingerprint"]) != 64:
            raise ValueError("candidate fingerprint is required")
        prices = value["prices"]
        interval = value["interval_seconds"]
        if (
            type(prices) is not list
            or not 1 <= len(prices) <= 64
            or any(type(p) not in (float, int) or not isfinite(p) or p <= 0 for p in prices)
        ):
            raise ValueError("local generated prices require 1..64 positive finite values")
        if (
            type(interval) not in (float, int)
            or not isfinite(interval)
            or not 0.01 <= interval <= 10
        ):
            raise ValueError("local interval must be in 0.01..10 seconds")
        return {**value, "prices": [float(p) for p in prices], "interval_seconds": float(interval)}

    def _record(self, request_id: str) -> dict[str, Any]:
        self._check_directories()
        _uuid(request_id)
        path = self.requests / f"{request_id}.json"
        if not path.exists() and not path.is_symlink():
            raise FileNotFoundError("startup request has no saved receipt")
        value = json.loads(read_regular(path, self.requests))
        if type(value) is not dict:
            raise ValueError("local simulation receipt is not an object")
        if (
            value.get("schema") != "ea.desktop-paper-request.v1"
            or value.get("workspace") != str(self.workspace)
            or value.get("request", {}).get("request_id") != request_id
        ):
            raise ValueError("local simulation receipt identity conflicts")
        self._request(value["request"])
        return cast(dict[str, Any], value)

    def list(self) -> list[dict[str, Any]]:
        with self._mutex:
            self._check_directories()
            records = [
                self.get(path.stem)
                for path in self.requests.glob("*.json")
                if not path.name.startswith(".")
            ]
            return sorted(records, key=lambda item: str(item["created_at"]), reverse=True)

    def get(self, request_id: str) -> dict[str, Any]:
        with self._mutex:
            record = self._record(request_id)
            request = record["request"]
            result = {
                **record,
                "run_id": request_id,
                "terminal_confirmed": False,
                "state": "unknown",
                "observed_at": datetime.now(UTC).isoformat(),
                "overview": {"available": False, "reason": "attempt_not_observed"},
            }
            child = self._children.get(request_id)
            result["process_exit"] = None if child is None else child.poll()
            if (
                record.get("spawn_failed")
                and not (self.runs / request_id).exists()
                and (child is None or child.poll() is not None)
            ):
                result.update(state="start_failed", terminal_confirmed=True)
                return result
            if (
                child is not None
                and child.poll() is not None
                and not (self.runs / request_id).exists()
            ):
                record["spawn_failed"] = True
                record["startup_exit"] = child.returncode
                self._publish(self.requests / f"{request_id}.json", record)
                result.update(
                    state="start_failed",
                    terminal_confirmed=True,
                    detail="The child exited before creating an attempt; nothing was reissued.",
                )
                return result
            try:
                _, payload = read_paper_binding(self.runs / request_id)
                manifest = json.loads(payload)
                candidate = manifest["candidate"]
                if (
                    manifest["operation"] != "paper.start"
                    or candidate["candidate_id"] != request["candidate_id"]
                    or candidate["fingerprint"] != request["fingerprint"]
                    or manifest["prices"] != request["prices"]
                    or manifest["interval_seconds"] != request["interval_seconds"]
                    or candidate["configuration_sha256"] != record["configuration_sha256"]
                ):
                    raise ValueError("attempt does not match the persisted startup request")
                status = read_paper_status(self.runs / request_id)
                terminal = (
                    status["state"] in {"stopped", "failed"}
                    and not status["lease_held"]
                    and status.get("terminal_durable") is True
                )
                if terminal and (child is None or child.poll() is not None):
                    # Reuse the existing journal/manifest recovery classification. This
                    # inspects and reconciles the original attempt; it never continues it.
                    recovered = resume_local_paper(self.runs / request_id)
                    terminal = recovered["state"] == "terminal" and not recovered.get(
                        "reconciliation_required", False
                    )
                else:
                    terminal = False
                result.update(
                    overview={"available": True, **status},
                    terminal_confirmed=terminal,
                    state=status["state"] if terminal or status["lease_held"] else "unknown",
                )
                if record.get("stop_requested") and not terminal:
                    result["state"] = "stop_requested" if status["lease_held"] else "unknown"
            except (OSError, ValueError, RuntimeError, KeyError) as error:
                result["detail"] = str(error)[:240]
            return result

    def start(self, supplied: dict[str, Any]) -> dict[str, Any]:
        with self._mutex:
            request = self._request(supplied)
            identity = request["request_id"]
            receipt = self.requests / f"{identity}.json"
            if receipt.exists():
                if self._record(identity)["request"] != request:
                    raise ValueError(
                        "startup request identity was already used with different configuration"
                    )
                return self.get(identity)
            if self._lock is None or self.closing:
                raise ValueError("desktop session is closing or unavailable")
            records = self.list()
            if any(not item["terminal_confirmed"] for item in records):
                raise ValueError(
                    "an active or unconfirmed attempt must reach a verified terminal first"
                )
            known = {item["run_id"] for item in records}
            if any(path.name not in known for path in self.runs.iterdir()):
                raise ValueError("unrecognized runtime evidence blocks a new instance")
            reader = self.reader()
            preparation = prepare_simulation(self.workspace, request["candidate_id"], reader=reader)
            if not preparation["eligible"] or preparation["fingerprint"] != request["fingerprint"]:
                raise ValueError(
                    "candidate admission changed or this local simulation profile is unsupported"
                )
            candidate = reader.record(request["candidate_id"])
            source_id = candidate["projection"]["source"]["job_id"]
            _, _, canonical_scenario = reader.job(source_id)
            if self.quine is None:
                scenario = json.loads(reader.read(reader.inputs_dir / f"{source_id}.json"))
            else:
                _, envelope = reader.owned(source_id)
                scenario = json.loads(bytes.fromhex(envelope["scenario_file"]))
                # The strict scenario loader checks this retained capture's admitted
                # data fingerprint. Only the data path changes; economics stay frozen.
                capture = self.quine.workspace / "inputs" / f"{source_id}.data"
                reader.read(capture)
                scenario["data"]["path"] = str(capture)
            if _configuration(scenario) != _configuration(canonical_scenario):
                raise ValueError("frozen scenario differs from Candidate configuration")
            input_path = self.requests / f"{identity}.scenario"
            self._publish(input_path, scenario)
            record = {
                "schema": "ea.desktop-paper-request.v1",
                "workspace": str(self.workspace),
                "request": request,
                "created_at": datetime.now(UTC).isoformat(),
                "source_job_id": source_id,
                "strategy": candidate["projection"]["strategy"],
                "configuration_sha256": preparation["binding"]["configuration_sha256"],
                "stop_requested": False,
            }
            # Durable receipt is before spawn. An uncertain spawn/publication is
            # never reissued, even after restarting the application.
            self._publish(receipt, record)
            command = [
                sys.executable,
                "-m",
                "ea.web.paper_worker",
                "--root",
                str(self.root),
                "--workspace",
                str(self.workspace),
                "--request-id",
                identity,
            ]
            if self.quine is not None:
                command.extend(
                    [
                        "--quine-socket",
                        str(self.quine.socket_path),
                        "--quine-workspace",
                        str(self.quine.workspace),
                    ]
                )
            with (self.root / f"{identity}.log").open("ab") as log:
                try:
                    child = subprocess.Popen(
                        command,
                        stdin=subprocess.PIPE,
                        stdout=log,
                        stderr=log,
                        cwd=self.root,
                        pass_fds=(self._lock.fileno(),),
                    )
                except OSError:
                    record["spawn_failed"] = True
                    self._publish(receipt, record)
                    return self.get(identity)
            self._children[identity] = child
            return self.get(identity)

    def stop(self, request_id: str, *, timeout: float = 10.0) -> dict[str, Any]:
        import time

        with self._mutex:
            record = self._record(request_id)
            current = self.get(request_id)
            if current["terminal_confirmed"]:
                return current
            record["stop_requested"] = True
            self._publish(self.requests / f"{request_id}.json", record)
            child = self._children.get(request_id)
            if child is not None and child.stdin is not None:
                child.stdin.close()  # Our exact child sees EOF even during admission.
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                current = self.get(request_id)
                if current["terminal_confirmed"]:
                    return current
                if current["overview"]["available"]:
                    with suppress(RuntimeError):
                        # Request is durable; completion is observed, never assumed.
                        request_paper_stop(self.runs / request_id, timeout_seconds=0.0)
                time.sleep(0.025)
            return self.get(request_id)

    def shutdown(self, *, timeout: float = 10.0) -> bool:
        with self._mutex:
            self.closing = True
            for row in self.list():
                if not row["terminal_confirmed"]:
                    self.stop(row["run_id"], timeout=timeout)
            confirmed = all(row["terminal_confirmed"] for row in self.list())
            if not confirmed:
                self.closing = False
            return confirmed

    def close(self) -> None:
        with self._mutex:
            if not self.shutdown():
                raise RuntimeError("Paper stop unconfirmed; session ownership retained")
            for child in self._children.values():
                child.wait(timeout=1)
                if child.stdin is not None and not child.stdin.closed:
                    child.stdin.close()
            if self._lock is not None:
                self._lock.close()
                self._lock = None
