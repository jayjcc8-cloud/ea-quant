"""PPV-16 integrated fault campaign drills (M4 fault phase only).

Bounded, deterministic drills over the existing Mac-local Paper runtime
boundaries: real `ea paper` subprocesses, the launchd supervisor in an isolated
home, and deterministic in-process seams through the real `run_local_paper`
composition and real POSIX journal. The script mutates only campaign-owned state
under ``<repo>/.campaign/`` and never touches gate identity, verdicts, ``src/``,
or the operator's real ``~/EA`` state (which is read-only fixture input).

Each drill writes one machine-readable evidence document into the evidence
root. ``all`` runs every drill in dependency order and prints a verdict map.

Usage:

    python scripts/ppv16_fault_campaign.py all
    python scripts/ppv16_fault_campaign.py baseline
    python scripts/ppv16_fault_campaign.py supervised

LIVE is and stays DENIED: no drill sets a run mode, submits to a real broker,
or alters power management.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

CAMPAIGN = REPO_ROOT / ".campaign"
STAMP = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
BASE_SHA = "4a012ea784788ce9371f8dd6b47fdeb7458428ad"
_EVIDENCE_ROOT: Path | None = None
_GATE_TRACKED_STATE = ""


def evidence_dir() -> Path:
    return _EVIDENCE_ROOT or (CAMPAIGN / f"ppv16-{STAMP}")


def artifacts_dir() -> Path:
    return evidence_dir() / "artifacts"


EA_BIN = Path(os.environ.get("PPV16_EA_BIN", str(REPO_ROOT / "venv" / "bin" / "ea"))).resolve()
PYTHON = Path(os.environ.get("PPV16_PYTHON", str(REPO_ROOT / "venv" / "bin" / "python"))).resolve()

# The campaign never constructs its own candidate: it reuses the WU-1 accepted
# Candidate and scenario fixtures read-only from the real operator home, or — when
# the installed code no longer matches that acceptance (the acceptance record
# pins the exact package code digest) — prepares its own accepted Candidate
# against THIS checkout through the existing research API, under campaign state.
FIXTURES: dict[str, str] = {}
EXAMPLE_INPUTS = ("source.yaml", "prices.csv", "later.yaml", "later.csv")


class _LoopbackWebService:
    """The existing `ea web serve` on loopback, used exactly as the WU-1 script does."""

    def __init__(self, scenario_root: Path, workspace: Path, ui_dir: Path) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.scenario_root = scenario_root
        self.workspace = workspace
        self.ui_dir = ui_dir
        self.process: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> _LoopbackWebService:
        log = (self.workspace.parent / "logs" / "prepare-web.log").open("ab")
        self.process = subprocess.Popen(
            [
                str(EA_BIN),
                "web",
                "serve",
                "--scenario-root",
                str(self.scenario_root),
                "--workspace",
                str(self.workspace),
                "--ui-dir",
                str(self.ui_dir),
                "--port",
                str(self.port),
            ],
            stdout=log,
            stderr=log,
        )
        wait_for(self._healthy, timeout=60.0, interval=0.5, what="the loopback research service")
        return self

    def __exit__(self, *_: object) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=30)

    def _healthy(self) -> bool:
        try:
            self.request("/api/health", None, "GET")
            return True
        except (OSError, urllib.error.URLError, DrillError):
            return False

    def request(self, path: str, body: object = None, method: str = "POST") -> Any:
        headers = {
            "Origin": self.origin,
            "Content-Type": "application/json",
            "X-EA-Web-Request": "1",
        }
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.origin + path, data=data, headers=headers, method=method
        )
        with urllib.request.urlopen(request, timeout=10) as reply:
            payload = reply.read()
        return json.loads(payload) if payload else None

    def wait_for_job(self, job_id: str, timeout: float = 120.0) -> dict[str, Any]:
        def finished() -> dict[str, Any] | None:
            document = cast(dict[str, Any], self.request(f"/api/backtests/{job_id}", method="GET"))
            if document["status"] in {"accepted", "running"}:
                return None
            return document

        job = cast(
            dict[str, Any],
            wait_for(finished, timeout=timeout, interval=0.2, what=f"job {job_id}"),
        )
        if job["status"] != "succeeded":
            raise DrillError(f"job {job_id} finished as {job['status']}")
        return job


def candidate_verifies(workspace: Path, candidate_id: str) -> bool:
    """True only when the paper runtime would admit the candidate.

    `ea candidate inspect` checks the frozen evidence bytes only; `ea paper
    start` additionally requires the acceptance record's EA code and
    distribution digest to match the code this campaign actually runs
    (`run_local_paper` -> `_load_binding`). A stale-digest candidate must
    fall through to a freshly prepared campaign Candidate, never reach a drill.
    """
    from ea.product.candidate import load_accepted_candidate

    try:
        load_accepted_candidate(workspace, candidate_id)
    except ValueError:
        return False
    return True


def prepare_campaign_fixtures() -> dict[str, str]:
    """Accept one Candidate against THIS checkout through the existing research API.

    The WU-1 accepted Candidate is pinned to the code digest of the checkout
    that accepted it, so once main moves, the campaign prepares its own
    accepted Candidate under campaign state with a PPV-16 acceptance reason.
    """
    home = CAMPAIGN / "ea-fixtures"
    workspace = home / "workspace"
    inputs = home / "inputs"
    marker = home / ".ppv16-prepared"
    if marker.is_file() and candidate_verifies(workspace, _stored_candidate(home)):
        return _fixtures_from(home)
    if home.exists():
        shutil.rmtree(home)
    inputs.mkdir(parents=True, exist_ok=True)
    for name in EXAMPLE_INPUTS:
        shutil.copyfile(REPO_ROOT / "examples" / "paper" / name, inputs / name)
    ui = home / "ui"
    ui.mkdir(parents=True, exist_ok=True)
    (ui / "index.html").write_text("<!doctype html><title>EA Web</title>\n", encoding="utf-8")
    (home / "logs").mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    with _LoopbackWebService(inputs, workspace, ui) as service:
        identity = service.request("/api/scenarios/source.yaml/validate")["input_identity"]
        source = service.request(
            "/api/backtests",
            {
                "scenario_id": "source.yaml",
                "input_identity": identity,
                "request_id": "ppv16-fault-campaign-source",
            },
        )
        service.wait_for_job(source["job_id"])
        relation = service.request(
            "/api/holdouts", {"source_job_id": source["job_id"], "scenario_id": "later.yaml"}
        )
        service.wait_for_job(relation["holdout_job_id"])
        created = service.request("/api/candidates", {"validation_id": relation["validation_id"]})
        candidate_id = created["candidate_id"]
        service.request(
            f"/api/candidates/{candidate_id}/decision",
            {"outcome": "ACCEPTED", "reason": "PPV-16 fault campaign fixtures (M4 fault phase)"},
        )
    if not candidate_verifies(workspace, candidate_id):
        raise DrillError("the campaign Candidate did not verify after acceptance")
    marker.write_text(f"{candidate_id}\n", encoding="utf-8")
    return _fixtures_from(home)


def _stored_candidate(home: Path) -> str:
    marker = home / ".ppv16-prepared"
    return marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""


def _fixtures_from(home: Path) -> dict[str, str]:
    return {
        "WORKSPACE": str(home / "workspace"),
        "CANDIDATE_ID": _stored_candidate(home),
        "SCENARIO": str(home / "inputs" / "source.yaml"),
    }


def _repair_pth_flags() -> None:
    """Clear the macOS UF_HIDDEN flag off the venv so .pth files are processed.

    On this host Finder re-marks files under .claude/worktrees as hidden, and
    CPython silently skips hidden .pth files, which kills the editable install
    without any error. The flag can come back within minutes, so the supervised
    drill re-applies this while it waits.
    """
    run_cmd("chflags", "-R", "nohidden", str(EA_BIN.parent.parent), check_exit=False)


def _require_usable_ea() -> None:
    """The campaign ea entry point must import this checkout's source.

    See ``_repair_pth_flags``: a hidden .pth silently kills the editable
    install, so repair the flags and fail loudly rather than time out inside a
    drill.
    """
    _repair_pth_flags()
    completed = run_cmd(str(EA_BIN), "doctor", check_exit=False, timeout=60.0)
    check(
        completed.returncode == 0,
        "the campaign ea entry point is not usable (editable install did not load this "
        "checkout). Run scripts/bootstrap_local.py in the worktree, then "
        "chflags -R nohidden <worktree>/venv and retry.",
    )


def load_fixtures() -> dict[str, str]:
    """Pick the fixture set: env overrides, then the WU-1 candidate if it still
    verifies for this checkout, else a freshly prepared campaign Candidate."""
    _require_usable_ea()
    overrides = {
        key: os.environ.get(f"PPV16_{key}", "") for key in ("WORKSPACE", "CANDIDATE_ID", "SCENARIO")
    }
    if all(overrides.values()):
        return overrides
    config = Path.home() / "EA" / "supervisor" / "run-config.env"
    values: dict[str, str] = {}
    if config.is_file():
        for line in config.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key] = value
    wu1_workspace = values.get("WORKSPACE", "")
    wu1_candidate = values.get("CANDIDATE_ID", "")
    wu1_scenario = values.get("SCENARIO", "")
    if (
        wu1_workspace
        and wu1_candidate
        and wu1_scenario
        and Path(wu1_workspace).is_dir()
        and Path(wu1_scenario).is_file()
        and candidate_verifies(Path(wu1_workspace), wu1_candidate)
    ):
        return {"WORKSPACE": wu1_workspace, "CANDIDATE_ID": wu1_candidate, "SCENARIO": wu1_scenario}
    prepared = prepare_campaign_fixtures()
    for key, value in prepared.items():
        if not value or (key != "CANDIDATE_ID" and not Path(value).exists()):
            raise DrillError(f"fixture {key}={value!r} is not usable")
    return prepared


class DrillError(RuntimeError):
    """A drill assertion or setup violation; evidence still gets written."""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise DrillError(message)


def run_cmd(
    *argv: str,
    env: dict[str, str] | None = None,
    timeout: float = 120.0,
    check_exit: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one command; return the completed process (text mode)."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    try:
        completed = subprocess.run(
            list(argv), env=full_env, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as error:
        raise DrillError(f"command timed out after {timeout}s: {' '.join(argv)}") from error
    if check_exit and completed.returncode != 0:
        raise DrillError(
            f"command failed ({completed.returncode}): {' '.join(argv)}\n"
            f"stdout: {completed.stdout[-800:]}\nstderr: {completed.stderr[-800:]}"
        )
    return completed


def run_json(
    *argv: str, env: dict[str, str] | None = None, timeout: float = 120.0
) -> dict[str, Any]:
    completed = run_cmd(*argv, env=env, timeout=timeout)
    try:
        return cast(dict[str, Any], json.loads(completed.stdout.strip().splitlines()[-1]))
    except (ValueError, IndexError) as error:
        raise DrillError(
            f"command did not emit JSON on its last stdout line: {' '.join(argv)}"
        ) from error


def paper_status(run_dir: Path) -> dict[str, Any]:
    return run_json(str(EA_BIN), "paper", "status", "--run-dir", str(run_dir))


def paper_resume(run_dir: Path) -> dict[str, Any]:
    return run_json(str(EA_BIN), "paper", "resume", "--run-dir", str(run_dir))


def wait_for(predicate: Any, *, timeout: float, interval: float, what: str) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise DrillError(f"timed out waiting for {what}")


def write_evidence(name: str, document: dict[str, Any]) -> Path:
    evidence_dir().mkdir(parents=True, exist_ok=True)
    path = evidence_dir() / f"{name}.json"
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_artifact(drill: str, name: str, payload: str) -> Path:
    directory = artifacts_dir() / drill
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(payload, encoding="utf-8")
    return path


def start_paper(
    runtime_root: Path,
    *,
    run_id: str,
    prices: str = "100",
    interval: float = 0.1,
    event_limit: int | None = None,
) -> subprocess.Popen[str]:
    argv = [
        str(EA_BIN),
        "paper",
        "start",
        "--workspace",
        FIXTURES["WORKSPACE"],
        "--candidate-id",
        FIXTURES["CANDIDATE_ID"],
        "--scenario",
        FIXTURES["SCENARIO"],
        "--output-root",
        str(runtime_root),
        "--run-id",
        run_id,
        "--prices",
        prices,
        "--interval",
        str(interval),
    ]
    if event_limit is not None:
        argv += ["--event-limit", str(event_limit)]
    out_path = runtime_root / f"{run_id}.console"
    out_file = out_path.open("w", encoding="utf-8")
    process = subprocess.Popen(argv, stdout=out_file, stderr=subprocess.STDOUT, text=True)
    process._ppv16_out_path = out_path  # type: ignore[attr-defined]
    return process


def finish_paper(process: subprocess.Popen[str], *, timeout: float = 60.0) -> dict[str, Any]:
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        process.kill()
        raise DrillError("paper process did not exit in time") from error
    out_path: Path = process._ppv16_out_path  # type: ignore[attr-defined]
    lines = [line for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise DrillError("paper process printed no output")
    try:
        result = json.loads(lines[-1])
    except ValueError as error:
        raise DrillError(
            f"paper process did not end with a JSON result: {lines[-1][:200]!r}"
        ) from error
    return cast(dict[str, Any], result)


def fresh_attempt(runtime_root: Path) -> Path:
    runtime_root.mkdir(parents=True, exist_ok=True)
    run_id = str(uuid4())
    return runtime_root / run_id


def attempt_snapshot(run_dir: Path) -> dict[str, int]:
    """Names + sizes of every attempt file, to prove a drill did not mutate it."""
    snapshot: dict[str, int] = {}
    for path in sorted(run_dir.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(run_dir))] = path.stat().st_size
    return snapshot


# ---------------------------------------------------------------------------
# Independent journal-truth verifier (F-06/F-08/F-10): the classification a
# resume admission must produce, computed directly from the durable records.
# ---------------------------------------------------------------------------

_AUTHZ_SCHEMA = "ea.audit-paper-submission.v1"
_RESULT_SCHEMA = "ea.audit-paper-submission-result.v1"


def attempt_records(run_dir: Path) -> list[Any]:
    """Reopen the attempt's verified journal and return its recovery records."""
    from ea.experiments.audit import reopen_posix_audit_journal
    from ea.experiments.store import (
        CanonicalAttemptManifest,
        LocalResultStore,
        VerifiedTerminalRecoveryBinding,
    )
    from ea.product.paper_session import read_paper_binding

    binding, manifest_bytes = read_paper_binding(run_dir)
    store = LocalResultStore(run_dir.parent)
    try:
        verified = store.verify_recovery_attempt(
            CanonicalAttemptManifest(binding.reference, manifest_bytes)
        )
        if isinstance(verified, VerifiedTerminalRecoveryBinding):
            return []
        recovered = store.recover_incomplete_attempt(verified)
        journal = reopen_posix_audit_journal(recovered.audit)
        try:
            return list(journal.recovery_records)
        finally:
            journal.close()
    finally:
        store.close()


def expected_truth(records: list[Any]) -> dict[int, dict[str, Any]]:
    """Per-order expected restart-visible truth, from the durable records alone.

    Returns ``{order_owner_sequence: {"trades": int, "result": str | None,
    "classification": str, "terminal": str | None}}``.
    """
    truth: dict[int, dict[str, Any]] = {}
    for record in records:
        document = json.loads(record.canonical_payload)
        if record.record_kind.value == "paper.submission_authorization":
            order = document.get("order")
            if type(order) is dict and type(order.get("order_id")) is dict:
                sequence = order["order_id"]["owner_sequence"]
                truth.setdefault(
                    sequence, {"trades": 0, "result": None, "classification": "", "terminal": None}
                )
        elif record.record_kind.value == "paper.submission_result":
            sequence = document["order_id"]["owner_sequence"]
            entry = truth.setdefault(
                sequence, {"trades": 0, "result": None, "classification": "", "terminal": None}
            )
            entry["result"] = document["submission_state"]
        elif record.record_kind.value == "paper.fact_dispatch":
            fact = document["ingress"]["fact"]
            if fact.get("kind") == "trade" and fact.get("order_id") is not None:
                sequence = fact["order_id"]["owner_sequence"]
                entry = truth.setdefault(
                    sequence, {"trades": 0, "result": None, "classification": "", "terminal": None}
                )
                # Query redelivery re-emits the retained trade with a fresh ingress
                # and the same fact identity: a second dispatch record is the
                # retained late/duplicate evidence, not a second economic effect.
                # Exactly-once counts distinct trade facts, exactly like
                # ``recover_paper_fills`` dedups by fact_sha256.
                entry.setdefault("trade_facts", set()).add(fact.get("fact_sha256"))
    for entry in truth.values():
        entry["trades"] = len(entry.pop("trade_facts", ()))
    for entry in truth.values():
        if entry["trades"] > 0:
            entry["classification"] = "sent_confirmed"
            entry["terminal"] = "filled"
        elif entry["result"] == "submitted":
            entry["classification"] = "sent_confirmed"
            entry["terminal"] = None
        elif entry["result"] == "definitely_not_submitted":
            entry["classification"] = "definitely_not_sent"
            entry["terminal"] = None
        else:  # "uncertain" or no result at all: never guess.
            entry["classification"] = "unknown"
            entry["terminal"] = None
    return truth


def verify_admission(admission: dict[str, Any], records: list[Any], label: str) -> list[str]:
    """Assert one resume admission equals the durable-record truth. Returns checks."""
    checks: list[str] = []
    if admission.get("state") == "terminal":
        check(
            admission.get("outbound_intents") == []
            and admission.get("reconciliation_required") is False,
            f"{label}: terminal admission must carry no intents and no reconciliation",
        )
        checks.append("terminal admission is closed and truthful")
        return checks
    truth = expected_truth(records)
    intents = {item["order_owner_sequence"]: item for item in admission.get("outbound_intents", [])}
    check(
        set(intents) == set(truth),
        f"{label}: intent set {sorted(intents)} differs from durable truth {sorted(truth)}",
    )
    joint = admission["joint"]
    for sequence, expected in truth.items():
        got = intents[sequence]
        check(
            got["classification"] == expected["classification"],
            f"{label}: order {sequence} classified {got['classification']!r}, "
            f"durable truth is {expected['classification']!r}",
        )
        check(
            got["terminal"] == expected["terminal"],
            f"{label}: order {sequence} terminal {got['terminal']!r}, "
            f"durable truth is {expected['terminal']!r}",
        )
        check(
            expected["trades"] <= 1,
            f"{label}: order {sequence} has {expected['trades']} trade facts "
            "(exactly-once violated)",
        )
        if expected["classification"] == "unknown":
            check(
                sequence in joint["not_sent_orders"] and sequence not in joint["open_orders"],
                f"{label}: unknown order {sequence} must resolve not-sent, never open/resent",
            )
        elif expected["terminal"] is None:
            check(
                sequence in joint["open_orders"],
                f"{label}: submitted order {sequence} must be kept open for continuation",
            )
    check(
        joint["recovered_fills"] == sum(item["trades"] for item in truth.values()),
        f"{label}: recovered fills {joint['recovered_fills']} != durable trade facts "
        f"{sum(i['trades'] for i in truth.values())}",
    )
    check(
        joint["divergent_orders"] == [],
        f"{label}: local Paper broker state must never diverge from its own journal "
        f"(divergent {joint['divergent_orders']})",
    )
    check(
        admission["reconciliation_required"] is False,
        f"{label}: a truthful local admission never reports reconciliation_required here",
    )
    check(
        all(
            intents[sequence]["venue_order_id"] is None
            for sequence in truth
            if truth[sequence]["classification"] == "unknown"
        ),
        f"{label}: an unknown intent must not carry a venue order identity",
    )
    checks.append("admission matches the durable-record truth exactly")
    return checks


# ---------------------------------------------------------------------------
# F-03 / F-04: baseline duplicate + late fact drill on the real CLI path.
# ---------------------------------------------------------------------------


def drill_baseline() -> dict[str, Any]:
    runtime_root = CAMPAIGN / "runtime-baseline"
    run_dir = fresh_attempt(runtime_root)
    process = start_paper(runtime_root, run_id=run_dir.name)
    try:
        wait_for(
            lambda: _status_if(lambda status: status.get("fills") == 6, run_dir),
            timeout=90.0,
            interval=0.25,
            what="the sixth fill",
        )
        run_json(str(EA_BIN), "paper", "stop", "--run-dir", str(run_dir), "--timeout", "30")
        result = finish_paper(process)
    finally:
        if process.poll() is None:
            process.kill()
    check(process.returncode == 0, f"baseline run must exit 0, got {process.returncode}")
    status = paper_status(run_dir)
    write_artifact("baseline", "status.json", json.dumps(status, indent=2, sort_keys=True))
    checks = [
        f"exit 0; state {status['state']} reason {status['reason']}",
        f"cash {status['cash']} equity {status['equity']} position {status['position']}",
        f"fills {status['fills']} observed_fills {status['observed_fills']} "
        f"duplicate_facts {status['duplicate_facts']}",
        f"ledger_sequence {status['ledger_sequence']}",
        f"completed_round_trips {status['completed_round_trips']}",
    ]
    check(result["state"] == "stopped", "baseline result state must be stopped")
    check(result["reconciliation"] == "match", "baseline reconciliation must be match")
    check(status["cash"] == "986.8" and status["equity"] == "986.8", "acknowledged cash 986.8")
    check(status["position"] == "0", "flat at the end")
    check(status["fills"] == 6 and status["observed_fills"] == 6, "six acknowledged Fills")
    check(status["duplicate_facts"] == 6, "six duplicate redeliveries retained as evidence")
    check(status["ledger_sequence"] == 7, "ledger sequence 7 (funding + six fills)")
    check(status["completed_round_trips"] == 3, "three round trips")
    admission = paper_resume(run_dir)
    check(
        admission["state"] == "terminal" and admission["reconciliation_required"] is False,
        "terminal resume is closed",
    )
    write_artifact("baseline", "resume.json", json.dumps(admission, indent=2, sort_keys=True))
    return {
        "drill": "baseline",
        "fault_classes": ["DUPLICATE_FACT", "LATE_FACT", "NO_DUPLICATE_ECONOMIC_EFFECT"],
        "seam": "real ea paper start/stop/status/resume subprocess",
        "verdict": "PASS",
        "checks": checks,
        "run_dir": str(run_dir),
    }


def _status_if(predicate: Any, run_dir: Path) -> Any:
    try:
        status = paper_status(run_dir)
    except DrillError:
        return None
    return status if predicate(status) else None


# ---------------------------------------------------------------------------
# F-05: deterministic uncertain-submit windows through the crash_after seam.
# ---------------------------------------------------------------------------

# The first fact dispatched after a local submit is the broker's
# ACKNOWLEDGEMENT (PaperBroker.submit result ingresses), not the trade Fill.
# Crashing at the ACK's fact/processing/ledger records must keep the order
# submitted-and-open with no economic effect, never a resend. Terminal-filled
# classification after a durable trade fact is proven by the real SIGKILL
# storm (F-06) over the same verification.
_WINDOWS = [
    ("PAPER_SUBMISSION_AUTHORIZATION", "unknown", None, "not_sent"),
    ("PAPER_SUBMISSION_RESULT", "sent_confirmed", None, "open"),
    ("PAPER_FACT_DISPATCH", "sent_confirmed", None, "open"),
    ("EXECUTION_FACT_PROCESSING_OUTCOME", "sent_confirmed", None, "open"),
    ("PORTFOLIO_LEDGER_HANDOFF_OUTCOME", "sent_confirmed", None, "open"),
]


def drill_uncertain() -> dict[str, Any]:
    from ea.core import AuditRecordKind, RunId
    from ea.product.paper_run import resume_local_paper, run_local_paper

    runtime_root = CAMPAIGN / "runtime-uncertain"
    windows: list[dict[str, Any]] = []
    for kind_name, classification, terminal, resolution in _WINDOWS:
        run_dir = fresh_attempt(runtime_root)
        kind = getattr(AuditRecordKind, kind_name)
        result = run_local_paper(
            Path(FIXTURES["WORKSPACE"]),
            FIXTURES["CANDIDATE_ID"],
            Path(FIXTURES["SCENARIO"]),
            runtime_root,
            prices=(100.0,),
            interval=0.1,
            run_id=RunId(run_dir.name),
            crash_after=kind,
        )
        check(result["state"] == "failed", f"{kind_name}: run must fail after crash injection")
        check(
            result["failure_type"] == "RuntimeError",
            f"{kind_name}: crash injection is a RuntimeError",
        )
        check(result["incomplete"] is True, f"{kind_name}: crash leaves the attempt incomplete")
        admission = resume_local_paper(run_dir)
        intent = admission["outbound_intents"][0] if admission["outbound_intents"] else None
        check(intent is not None, f"{kind_name}: one outbound intent expected")
        assert intent is not None
        check(
            intent["classification"] == classification,
            f"{kind_name}: classified {intent['classification']!r}, expected {classification!r}",
        )
        check(
            intent["terminal"] == terminal,
            f"{kind_name}: terminal {intent['terminal']!r}, expected {terminal!r}",
        )
        joint = admission["joint"]
        check(
            joint["recovered"] is True and joint["divergent_orders"] == [],
            f"{kind_name}: joint acceptance must recover without divergence",
        )
        if resolution == "not_sent":
            check(
                joint["not_sent_orders"] == [1] and joint["open_orders"] == [],
                f"{kind_name}: never resent; resolved not-sent",
            )
        elif resolution == "open":
            check(
                joint["open_orders"] == [1] and joint["not_sent_orders"] == [],
                f"{kind_name}: kept open for continuation, never resent",
            )
        else:
            check(
                joint["recovered_fills"] == 1 and joint["open_orders"] == [],
                f"{kind_name}: exactly one economic effect for the filled order",
            )
        check(
            admission["reconciliation_required"] is False,
            f"{kind_name}: broker query participates in the resolution",
        )
        write_artifact(
            "uncertain", f"{kind_name}.resume.json", json.dumps(admission, indent=2, sort_keys=True)
        )
        windows.append(
            {
                "window": kind_name,
                "classification": classification,
                "terminal": terminal,
                "resolution": resolution,
                "cash_after_crash": result.get("cash", "unpublished"),
                "fills_after_crash": result.get("fills", "unpublished"),
            }
        )
    return {
        "drill": "uncertain",
        "fault_classes": [
            "UNCERTAIN_SUBMIT",
            "RESTART_RECOVERY",
            "RECONCILIATION_AFTER_FAULT",
            "FAIL_CLOSED",
            "NO_DUPLICATE_ECONOMIC_EFFECT",
            "EVIDENCE_TRUTHFUL",
        ],
        "seam": (
            "deterministic crash_after seam through run_local_paper + real POSIX journal + resume"
        ),
        "verdict": "PASS",
        "windows": windows,
    }


# ---------------------------------------------------------------------------
# F-07: injected durable-write failures at the pre-effect authorization frame.
# ---------------------------------------------------------------------------

_AUTHZ_MARKER = b"ea.audit-paper-submission.v1"


def drill_durable_write() -> dict[str, Any]:
    import importlib

    class _PaperRunSeam(Protocol):
        create_posix_audit_journal: Any
        run_local_paper: Any

    # Dynamic import: ``create_posix_audit_journal`` is only imported into
    # paper_run, not re-exported, and the monkeypatch must replace the exact
    # module global ``run_local_paper`` calls.
    paper_run = cast(_PaperRunSeam, importlib.import_module("ea.product.paper_run"))
    from ea.core import RunId
    from ea.experiments.audit import _OsAuditOps
    from ea.product.paper_run import resume_local_paper

    real_create = paper_run.create_posix_audit_journal

    class _InjectingOps(_OsAuditOps):
        def __init__(self, mode: str) -> None:
            self._mode = mode
            self._carry = b""
            self._armed = False

        def pwrite(self, journal_fd: int, data: memoryview, offset: int) -> int:
            combined = self._carry + bytes(data)
            self._carry = combined[-(len(_AUTHZ_MARKER) - 1) :]
            if _AUTHZ_MARKER in combined:
                if self._mode == "pwrite":
                    raise OSError("ppv16 injected pwrite failure")
                self._armed = True
            return super().pwrite(journal_fd, data, offset)

        def fsync(self, descriptor: int) -> None:
            if self._mode == "fsync" and self._armed:
                self._armed = False
                raise OSError("ppv16 injected post-write fsync failure")
            return super().fsync(descriptor)

    runtime_root = CAMPAIGN / "runtime-durable-write"
    variants: list[dict[str, Any]] = []
    for mode in ("pwrite", "fsync"):
        paper_run.create_posix_audit_journal = lambda prepared, _ops=None, _mode=mode: real_create(
            prepared, _ops=_InjectingOps(_mode)
        )
        run_dir = fresh_attempt(runtime_root)
        try:
            result = paper_run.run_local_paper(
                Path(FIXTURES["WORKSPACE"]),
                FIXTURES["CANDIDATE_ID"],
                Path(FIXTURES["SCENARIO"]),
                runtime_root,
                prices=(100.0,),
                interval=0.1,
                run_id=RunId(run_dir.name),
            )
        finally:
            paper_run.create_posix_audit_journal = real_create
        check(result["state"] == "failed", f"{mode}: the run must fail, not pretend success")
        check(result["terminal_durable"] is False, f"{mode}: no durable terminal may be minted")
        check(result["incomplete"] is True, f"{mode}: the attempt is incomplete")
        check(
            result["cash"] == "1000" and result["orders"] == 0 and result["fills"] == 0,
            f"{mode}: no economic side effect may follow the failed durable pre-effect write",
        )
        admission = resume_local_paper(run_dir)
        intents = admission["outbound_intents"]
        if mode == "pwrite":
            check(intents == [], "pwrite: recovery must not invent missing durable authority")
            resolution = "no intent recorded (authorization frame never landed); truthful"
        else:
            check(
                len(intents) == 1 and intents[0]["classification"] == "unknown",
                "fsync: the landed authorization with no result stays explicitly unknown",
            )
            check(
                admission["joint"]["not_sent_orders"] == [1]
                and admission["joint"]["open_orders"] == [],
                "fsync: ambiguity is resolved by the broker query, never guessed or resent",
            )
            resolution = "unknown intent resolved not-sent by the broker observation"
        variants.append(
            {
                "mode": mode,
                "failure_type": result["failure_type"],
                "cash_after_failure": result["cash"],
                "resolution": resolution,
            }
        )
    return {
        "drill": "durable-write",
        "fault_classes": [
            "DURABLE_WRITE_FAILURE",
            "FAIL_CLOSED",
            "EVIDENCE_TRUTHFUL",
            "NO_DUPLICATE_ECONOMIC_EFFECT",
        ],
        "seam": "injected _AuditOps pwrite/fsync failure through the real POSIX journal "
        "at the PAPER_SUBMISSION_AUTHORIZATION frame",
        "verdict": "PASS",
        "variants": variants,
    }


# ---------------------------------------------------------------------------
# F-06: real SIGKILL storm; every killed attempt must classify truthfully.
# ---------------------------------------------------------------------------

_KILL_POINTS = [
    ("after_start", 0, 0.05),
    ("after_start", 0, 0.4),
    ("fills", 1, 0.05),
    ("fills", 1, 0.3),
    ("fills", 2, 0.05),
    ("fills", 2, 0.25),
    ("fills", 3, 0.05),
    ("fills", 4, 0.05),
    ("fills", 4, 0.25),
    ("fills", 5, 0.05),
    ("fills", 6, 0.05),
    ("fills", 6, 0.4),
]


def drill_storm() -> dict[str, Any]:
    runtime_root = CAMPAIGN / "runtime-storm"
    kills: list[dict[str, Any]] = []
    for index, (phase, count, delay) in enumerate(_KILL_POINTS):
        run_dir = fresh_attempt(runtime_root)
        process = start_paper(runtime_root, run_id=run_dir.name, interval=0.25)
        wait_for(
            lambda run_dir=run_dir: _status_if(
                lambda status: status.get("lease_held") is True, run_dir
            ),
            timeout=60.0,
            interval=0.1,
            what="a live attempt",
        )
        if phase == "fills":
            wait_for(
                lambda run_dir=run_dir, count=count: _status_if(
                    lambda status: status.get("fills") == count, run_dir
                ),
                timeout=90.0,
                interval=0.1,
                what=f"{count} fills",
            )
        time.sleep(delay)
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=30)
        check(
            process.returncode == -signal.SIGKILL,
            f"kill {index}: death by SIGKILL expected, got {process.returncode}",
        )
        records = attempt_records(run_dir)
        admission = paper_resume(run_dir)
        checks = verify_admission(admission, records, f"kill {index}")
        write_artifact(
            "storm",
            f"kill-{index:02d}.resume.json",
            json.dumps(admission, indent=2, sort_keys=True),
        )
        kills.append(
            {
                "index": index,
                "phase": phase,
                "target": count,
                "run_dir": str(run_dir),
                "resume_state": admission["state"],
                "intent_count": len(admission.get("outbound_intents", [])),
                "recovered_fills": admission.get("joint", {}).get("recovered_fills", 0),
                "checks": checks,
            }
        )
    return {
        "drill": "storm",
        "fault_classes": [
            "UNCERTAIN_SUBMIT",
            "PROCESS_CRASH",
            "RESTART_RECOVERY",
            "RECONCILIATION_AFTER_FAULT",
            "NO_DUPLICATE_ECONOMIC_EFFECT",
            "EVIDENCE_TRUTHFUL",
        ],
        "seam": "real ea paper subprocess + SIGKILL at spread offsets + ea paper resume",
        "verdict": "PASS",
        "kills": kills,
    }


# ---------------------------------------------------------------------------
# F-08: torn-tail repair on reopen after a mid-append crash.
# ---------------------------------------------------------------------------


def drill_torn_tail() -> dict[str, Any]:
    storm_evidence = _load_prior("storm")
    kills = storm_evidence["kills"]
    source_dir = Path(kills[-1]["run_dir"])
    journal_path = source_dir / "audit" / "audit-v1.journal"
    check(journal_path.is_file(), "torn-tail source journal must exist")
    records = attempt_records(source_dir)
    admission_before = paper_resume(source_dir)
    size_before = journal_path.stat().st_size
    # Deterministic torn frame: a header length that claims far more than the
    # bytes that follow, exactly what a kill mid-frame can leave behind.
    partial = b"\x00\x00\x00\x00\x00\x00\x01\x00" + b"TORN" * 8
    with journal_path.open("ab") as handle:
        handle.write(partial)
        handle.flush()
        os.fsync(handle.fileno())
    check(journal_path.stat().st_size == size_before + len(partial), "torn bytes landed")
    admission_after = paper_resume(source_dir)
    check(journal_path.stat().st_size == size_before, "reopen must durably truncate the torn tail")
    checks = verify_admission(admission_after, records, "torn-tail reopen")
    check(
        admission_after == admission_before,
        "the torn tail changes no classification: the pre-torn truth stands",
    )
    return {
        "drill": "torn-tail",
        "fault_classes": ["DURABLE_WRITE_FAILURE", "RESTART_RECOVERY", "EVIDENCE_TRUTHFUL"],
        "seam": "deterministic partial frame appended to a real killed attempt's journal; "
        "ea paper resume reopens with permit_torn_tail",
        "verdict": "PASS",
        "checks": checks,
        "source_run_dir": str(source_dir),
    }


def _load_prior(name: str) -> dict[str, Any]:
    path = evidence_dir() / f"{name}.json"
    if not path.is_file():
        raise DrillError(f"drill {name} must run before this one (evidence missing)")
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("verdict") != "PASS":
        raise DrillError(f"drill {name} did not pass; this drill needs its passing evidence")
    return cast(dict[str, Any], document)


# ---------------------------------------------------------------------------
# F-01 / F-02: feed interruption — stall (SIGSTOP/SIGCONT) and exhaustion.
# ---------------------------------------------------------------------------


def drill_feed() -> dict[str, Any]:
    checks: list[str] = []
    # F-02: bounded source exhaustion on the real CLI path.
    runtime_root = CAMPAIGN / "runtime-feed"
    run_dir = fresh_attempt(runtime_root)
    process = start_paper(runtime_root, run_id=run_dir.name, event_limit=3)
    result = finish_paper(process)
    check(process.returncode == 3, f"bounded run must exit 3, got {process.returncode}")
    check(
        result["state"] == "failed" and result["reason"] == "source_exhausted",
        "bounded run ends source_exhausted, never fabricated healthy",
    )
    status = paper_status(run_dir)
    check(status["market_events"] == 3, f"exactly 3 market events, got {status['market_events']}")
    admission = paper_resume(run_dir)
    check(admission["state"] == "terminal", "bounded run is durably terminal")
    checks.append(
        f"event_limit=3 -> exit 3, {status['market_events']} market events, "
        f"{status['pending_facts']} pending facts, terminal resume"
    )
    # F-01: real stall: freeze the live process past the stall window, resume it.
    run_dir = fresh_attempt(runtime_root)
    process = start_paper(runtime_root, run_id=run_dir.name, interval=0.1)
    try:
        wait_for(
            lambda: _status_if(lambda status: status.get("fills", 0) >= 2, run_dir),
            timeout=90.0,
            interval=0.2,
            what="two fills before the stall",
        )
        before = paper_status(run_dir)
        os.kill(process.pid, signal.SIGSTOP)
        time.sleep(7.0)  # > stall_timeout (5s) and > max_market_age (5s)
        os.kill(process.pid, signal.SIGCONT)
        result = finish_paper(process, timeout=30.0)
    finally:
        if process.poll() is None:
            process.kill()
    check(process.returncode == 3, f"stalled run must exit 3, got {process.returncode}")
    check(result["state"] == "failed", "stall is not a healthy stop")
    check(
        result["reason"] in ("source_stalled", "stale_market"),
        f"stall reason must be source_stalled or stale_market, got {result['reason']!r}",
    )
    after = paper_status(run_dir)
    check(
        after["market_events"] <= before["market_events"] + 1,
        "no fabricated market events after the stall (dispatch count frozen)",
    )
    check(after["pending_facts"] == 0, "already-issued facts drain within the bound")
    check(after["fact_events"] >= before["fact_events"], "facts keep draining, never invented")
    admission = paper_resume(run_dir)
    check(
        admission["state"] == "terminal" and admission["reconciliation_required"] is False,
        "the stalled attempt is closed truthfully (no incomplete work invented)",
    )
    checks.append(
        f"stall -> exit 3, reason {result['reason']}, market_events "
        f"{before['market_events']}->{after['market_events']}, "
        f"pending_facts {after['pending_facts']}"
    )
    return {
        "drill": "feed",
        "fault_classes": ["FEED_INTERRUPTION", "FAIL_CLOSED", "EVIDENCE_TRUTHFUL"],
        "seam": "real ea paper subprocess; SIGSTOP/SIGCONT through the existing "
        "stall/freshness guards; --event-limit exhaustion",
        "verdict": "PASS",
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# F-09: backup + restore into a NEW isolated attempt, then resume.
# ---------------------------------------------------------------------------


def drill_backup_restore() -> dict[str, Any]:
    storm_evidence = _load_prior("storm")
    kills = storm_evidence["kills"]
    # Prefer a killed attempt that durably holds at least one fill.
    candidates = [kill for kill in kills if kill["recovered_fills"] > 0]
    source_dir = Path((candidates or kills)[-1]["run_dir"])
    backup_root = CAMPAIGN / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    before = attempt_snapshot(source_dir)
    backup = run_json(
        str(EA_BIN),
        "paper",
        "backup",
        "--run-dir",
        str(source_dir),
        "--backup-root",
        str(backup_root),
        "--keep",
        "5",
    )
    backup_dir = Path(backup["backup_dir"])
    manifest = run_json(str(EA_BIN), "paper", "backup", "inspect", "--backup", str(backup_dir))
    check(manifest["run"]["run_id"] == source_dir.name, "backup records the source run id")
    restored = CAMPAIGN / "restored" / source_dir.name
    restore = run_json(
        str(EA_BIN), "paper", "restore", "--backup", str(backup_dir), "--run-dir", str(restored)
    )
    check(str(restored) == restore.get("run_dir"), "restore materialises the requested target")
    source_admission = paper_resume(source_dir)
    restored_admission = paper_resume(restored)
    check(
        source_admission == restored_admission,
        "the restored attempt reopens through the recovery path with the same classification",
    )
    checks = verify_admission(restored_admission, attempt_records(restored), "restored attempt")
    check(
        attempt_snapshot(source_dir) == before,
        "backup/restore never mutates the faulted source attempt",
    )
    # Restore must never overwrite an existing target.
    occupied = CAMPAIGN / "restored-occupied"
    occupied.mkdir(parents=True, exist_ok=True)
    refusal = run_cmd(
        str(EA_BIN),
        "paper",
        "restore",
        "--backup",
        str(backup_dir),
        "--run-dir",
        str(occupied),
        check_exit=False,
    )
    check(refusal.returncode != 0, "restore into an existing target must be refused")
    # The baseline (terminal) attempt round-trips too.
    baseline_evidence = _load_prior("baseline")
    baseline_dir = Path(baseline_evidence["run_dir"])
    baseline_backup = run_json(
        str(EA_BIN),
        "paper",
        "backup",
        "--run-dir",
        str(baseline_dir),
        "--backup-root",
        str(backup_root),
        "--keep",
        "5",
    )
    restored_baseline = CAMPAIGN / "restored-baseline" / baseline_dir.name
    run_json(
        str(EA_BIN),
        "paper",
        "restore",
        "--backup",
        str(baseline_backup["backup_dir"]),
        "--run-dir",
        str(restored_baseline),
    )
    baseline_admission = paper_resume(restored_baseline)
    check(baseline_admission["state"] == "terminal", "a restored terminal attempt stays terminal")
    return {
        "drill": "backup-restore",
        "fault_classes": [
            "BACKUP_RESTORE_AFTER_FAULT",
            "RESTART_RECOVERY",
            "RECONCILIATION_AFTER_FAULT",
            "NO_DUPLICATE_ECONOMIC_EFFECT",
            "EVIDENCE_TRUTHFUL",
        ],
        "seam": "ea paper backup/inspect/restore/resume CLI round trip over a faulted attempt",
        "verdict": "PASS",
        "checks": checks,
        "source_run_dir": str(source_dir),
        "restored_run_dir": str(restored),
    }


# ---------------------------------------------------------------------------
# F-10: launchd-supervised crash drill in an isolated runtime home.
#
# Host boundary: launchd-spawned jobs in this session carry no TCC grant for
# ~/Documents (com.apple.macl on the checkout tree), so a job cannot read the
# worktree venv, scenario, workspace or config even though this shell can.
# The rig therefore lives under /tmp with a copied venv holding a NON-editable
# ea install (same package files, so the acceptance code digest is identical).
# ---------------------------------------------------------------------------

_EAR = str(REPO_ROOT / "ops" / "launchd" / "ea-runtime")
_EAR_LABELS = ("com.ea.paper", "com.ea.paper-backup")
_SUPERVISED_RIG: Path | None = None


def _ear_env() -> dict[str, str]:
    base = CAMPAIGN if _SUPERVISED_RIG is None else _SUPERVISED_RIG
    return {
        "EA_RUNTIME_HOME": str(base / "ea-home" / "EA"),
        "EA_LAUNCH_AGENTS_DIR": str(base / "launchagents"),
    }


def _rig_ea_bin() -> Path:
    if _SUPERVISED_RIG is None:
        return EA_BIN
    return _SUPERVISED_RIG / "venv" / "bin" / "ea"


def _prepare_supervised_rig() -> Path:
    """Materialize the isolated supervised rig under /tmp (see module note)."""
    rig = Path(f"/tmp/ppv16-fault-campaign-{uuid4().hex[:12]}")
    rig.mkdir(parents=True)
    run_cmd("uv", "venv", "--python", "3.12", str(rig / "venv"))
    site_source = next((REPO_ROOT / "venv" / "lib").glob("python*/site-packages"))
    site_target = next((rig / "venv" / "lib").glob("python*/site-packages"))
    for entry in site_source.iterdir():
        if entry.name.startswith(("__editable__", "ea_quant")):
            continue
        if entry.is_dir():
            shutil.copytree(entry, site_target / entry.name, symlinks=True)
        else:
            shutil.copy2(entry, site_target / entry.name)
    run_cmd(
        "uv",
        "pip",
        "install",
        "--python",
        str(rig / "venv" / "bin" / "python"),
        "--no-deps",
        str(REPO_ROOT),
    )
    run_cmd(str(rig / "venv" / "bin" / "ea"), "doctor")
    shutil.copytree(CAMPAIGN / "ea-fixtures" / "workspace", rig / "workspace")
    shutil.copytree(CAMPAIGN / "ea-fixtures" / "inputs", rig / "inputs")
    (rig / "run-config.env").write_text(
        "\n".join(
            [
                f"EA_BIN={rig / 'venv' / 'bin' / 'ea'}",
                f"WORKSPACE={rig / 'workspace'}",
                f"CANDIDATE_ID={FIXTURES['CANDIDATE_ID']}",
                f"SCENARIO={rig / 'inputs' / 'source.yaml'}",
                f"RUNTIME_ROOT={rig / 'ea-home' / 'EA' / 'runtime'}",
                f"BACKUP_ROOT={rig / 'ea-home' / 'EA' / 'backups'}",
                "PRICES=100",
                "INTERVAL=0.1",
                "EVENT_LIMIT=",
                "BACKUP_KEEP=5",
                "BACKUP_INTERVAL_SECONDS=300",
                "THROTTLE_INTERVAL_SECONDS=3",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return rig


def _ear_run(
    *args: str, check_exit: bool = True, timeout: float = 180.0
) -> subprocess.CompletedProcess[str]:
    return run_cmd("sh", _EAR, *args, env=_ear_env(), check_exit=check_exit, timeout=timeout)


def _label_field(label: str, field: str) -> str:
    uid = os.getuid()
    completed = run_cmd("launchctl", "print", f"gui/{uid}/{label}", check_exit=False)
    if completed.returncode != 0:
        return ""
    for line in completed.stdout.splitlines():
        line = line.strip()
        if line.startswith(f"{field} = "):
            return line.split(" = ", 1)[1]
    return ""


def _paper_processes() -> list[int]:
    completed = run_cmd("pgrep", "-f", f"{_rig_ea_bin()} paper start", check_exit=False)
    return [int(pid) for pid in completed.stdout.split()]


def _inject_job_home() -> None:
    """Add HOME + the isolated runtime paths to the drill's rendered plists.

    Only the drill's own LaunchAgents in ``EA_LAUNCH_AGENTS_DIR`` are edited;
    the repository templates and the supervisor script are untouched. This
    reproduces the environment a real Aqua login session gives every job.
    """
    uid = os.getuid()
    plist_buddy = "/usr/libexec/PlistBuddy"
    entries = (
        ("HOME", Path.home().as_posix()),
        ("EA_RUNTIME_HOME", _ear_env()["EA_RUNTIME_HOME"]),
        ("EA_LAUNCH_AGENTS_DIR", _ear_env()["EA_LAUNCH_AGENTS_DIR"]),
    )
    for label in _EAR_LABELS:
        plist = Path(_ear_env()["EA_LAUNCH_AGENTS_DIR"]) / f"{label}.plist"
        check(plist.is_file(), f"{plist} must be rendered before injection")
        run_cmd(plist_buddy, "-c", "Add :EnvironmentVariables dict", str(plist), check_exit=False)
        for key, value in entries:
            # PlistBuddy `Add` takes EntryPath [Type] Value; `Set` takes
            # EntryPath Value ONLY — a type token on Set becomes part of the
            # stored value (observed: "string /tmp/..."), so it is omitted.
            run_cmd(
                plist_buddy,
                "-c",
                f"Add :EnvironmentVariables:{key} string {value}",
                str(plist),
                check_exit=False,
            )
            run_cmd(
                plist_buddy,
                "-c",
                f"Set :EnvironmentVariables:{key} {value}",
                str(plist),
                check_exit=True,
            )
        raw = run_cmd(
            "plutil", "-extract", "EnvironmentVariables.EA_RUNTIME_HOME", "raw", str(plist)
        )
        check(
            raw.stdout.strip() == _ear_env()["EA_RUNTIME_HOME"],
            "the injected EA_RUNTIME_HOME value must be exact (no type-token mangling)",
        )
        run_cmd("launchctl", "bootout", f"gui/{uid}/{label}", check_exit=False)
        run_cmd("launchctl", "bootstrap", f"gui/{uid}", str(plist), check_exit=True)


def drill_supervised() -> dict[str, Any]:
    global _SUPERVISED_RIG
    uid = os.getuid()
    reachable = run_cmd("launchctl", "print", f"gui/{uid}", check_exit=False).returncode == 0
    if not reachable:
        return {
            "drill": "supervised",
            "fault_classes": [
                "PROCESS_CRASH",
                "RESTART_RECOVERY",
                "RECONCILIATION_AFTER_FAULT",
                "NO_DUPLICATE_ECONOMIC_EFFECT",
            ],
            "seam": "isolated launchd install",
            "verdict": "BLOCKED",
            "reason": "launchd gui domain is not reachable in this session",
        }
    for label in _EAR_LABELS:
        if _label_field(label, "pid"):
            return {
                "drill": "supervised",
                "fault_classes": ["PROCESS_CRASH"],
                "seam": "isolated launchd install",
                "verdict": "BLOCKED",
                "reason": (
                    f"{label} is loaded on this host; the campaign never disturbs a real install"
                ),
            }
    checks: list[str] = []
    first_run_dir: Path | None = None
    rig: Path | None = None
    # The real operator ~/EA is read-only input: prove the drill never mutates
    # it, because an un-pinned launchd job would fall back to $HOME/EA.
    operator_state_dir = Path.home() / "EA" / "supervisor" / "state"
    operator_state_before = attempt_snapshot(operator_state_dir)
    try:
        rig = _prepare_supervised_rig()
        _SUPERVISED_RIG = rig
        checks.append(
            f"isolated rig at {rig}: launchd-spawned jobs in this session carry no TCC "
            "grant for ~/Documents, so the rig (copied venv with a non-editable ea "
            "install, fixtures, runtime home) lives under /tmp"
        )
        run_cmd(
            "sh",
            _EAR,
            "install",
            "--config",
            str(rig / "run-config.env"),
            env={**_ear_env(), "EA_RUNTIME_NO_BOOTSTRAP": "1"},
        )
        # A launchd-spawned job does NOT inherit EA_RUNTIME_HOME from this
        # shell, and without it the supervisor falls back to $HOME/EA — the
        # REAL operator state. Before the first spawn the job's environment
        # must therefore pin the rig paths, exactly as _inject_job_home does.
        # The fail-closed refusal on a missing HOME is proven at shell level
        # first (no launchd spawn, zero state mutation):
        refused = run_cmd(
            "env",
            "-u",
            "HOME",
            "-u",
            "EA_RUNTIME_HOME",
            "sh",
            _EAR,
            "run",
            "--supervised",
            env={"PATH": "/bin:/usr/bin"},
            check_exit=False,
        )
        check(
            refused.returncode != 0
            and "HOME must be set for the EA runtime operator" in refused.stderr,
            "without HOME the supervisor must refuse before writing any state",
        )
        checks.append(
            f"shell-level fail-closed: 'ea-runtime run --supervised' without HOME "
            f"refuses (exit {refused.returncode}), no state written"
        )
        # The install above rendered the plists but did NOT bootstrap: it ran
        # with EA_RUNTIME_NO_BOOTSTRAP so no job could spawn with the real
        # ~/EA fallback between install and injection.
        check(
            not (rig / "ea-home" / "EA" / "supervisor" / "state" / "current-run").exists()
            and _label_field("com.ea.paper", "state") == "",
            "install must render without launching any job",
        )
        _inject_job_home()
        first_run_dir = wait_for(
            lambda: _supervised_live_run(),
            timeout=120.0,
            interval=0.5,
            what="a live supervised attempt",
        )
        pid1 = int(_label_field("com.ea.paper", "pid"))
        processes = _paper_processes()
        check(
            processes == [pid1], f"exactly one supervised writer (pid {pid1}), observed {processes}"
        )
        checks.append(f"single writer pid {pid1} for attempt {first_run_dir.name}")
        # WU-4: an unsupervised hand-run is refused while the agent is loaded.
        refused = _ear_run("run", check_exit=False)
        check(
            refused.returncode == 2 and "refusing an unsupervised run" in refused.stderr,
            "WU-4 second-writer refusal holds",
        )
        # start is a no-op on a live run: same attempt, never a second writer.
        started = _ear_run("start")
        check(first_run_dir.name in started.stdout, "start on a live run is a no-op")
        # The real crash: SIGKILL the supervised PID.
        os.kill(pid1, signal.SIGKILL)
        second_run_dir = wait_for(
            lambda: _supervised_live_run(other_than=first_run_dir),
            timeout=60.0,
            interval=0.2,
            what="launchd replacement attempt",
        )
        pid2 = int(_label_field("com.ea.paper", "pid"))
        processes = _paper_processes()
        check(processes == [pid2], f"the replacement is again the single writer, got {processes}")
        check(pid2 != pid1, "the replacement is a fresh process")
        check(
            _label_field("com.ea.paper", "last exit code") != "0",
            "abnormal death is observable as a non-zero last exit",
        )
        checks.append(
            f"launchd replaced pid {pid1} with {pid2}, fresh attempt "
            f"{second_run_dir.name}, single writer"
        )
        # The killed attempt is classified only by the recovery path.
        killed_status = paper_status(first_run_dir)
        check(killed_status["lease_held"] is False, "the killed attempt released its lease")
        records = attempt_records(first_run_dir)
        admission = paper_resume(first_run_dir)
        checks += verify_admission(admission, records, "killed supervised attempt")
        # The rig is deleted at cleanup, so the killed attempt's evidence must
        # be captured into the campaign evidence root while it still exists.
        write_artifact(
            "supervised", "killed.status.json", json.dumps(killed_status, indent=2, sort_keys=True)
        )
        write_artifact(
            "supervised", "killed.resume.json", json.dumps(admission, indent=2, sort_keys=True)
        )
        # Intentional stop is distinguishable from a crash: exit 0, stays stopped
        # past the restart throttle.
        _ear_run("stop")
        check(
            _label_field("com.ea.paper", "state") in ("not running", ""),
            "cooperative stop settles the job",
        )
        check(
            _label_field("com.ea.paper", "last exit code") == "0",
            "intentional stop exits 0 (launchd keeps it stopped)",
        )
        time.sleep(5.0)  # > THROTTLE_INTERVAL_SECONDS=3
        check(
            _label_field("com.ea.paper", "state") in ("not running", ""),
            "the stopped job stays stopped past the restart throttle",
        )
        checks.append("intentional stop stays stopped past the throttle; crash restarts")
        stopped_admission = paper_resume(second_run_dir)
        check(
            stopped_admission["state"] == "terminal",
            "the cooperatively stopped attempt is durably terminal",
        )
    finally:
        try:
            _ear_run("uninstall", check_exit=False)
            for label in _EAR_LABELS:
                check(
                    _label_field(label, "state") == "",
                    f"{label} must not remain loaded after uninstall",
                )
            check(
                attempt_snapshot(operator_state_dir) == operator_state_before,
                "the real operator ~/EA state must be byte-identical before and after",
            )
        finally:
            if rig is not None:
                shutil.rmtree(rig, ignore_errors=True)
            _SUPERVISED_RIG = None
    return {
        "drill": "supervised",
        "fault_classes": [
            "PROCESS_CRASH",
            "RESTART_RECOVERY",
            "RECONCILIATION_AFTER_FAULT",
            "NO_DUPLICATE_ECONOMIC_EFFECT",
            "EVIDENCE_TRUTHFUL",
        ],
        "seam": "real launchd LaunchAgents in an isolated EA_RUNTIME_HOME; SIGKILL of the "
        "supervised PID; ea-runtime stop for the intentional stop",
        "verdict": "PASS",
        "checks": checks,
        "killed_attempt": None if first_run_dir is None else str(first_run_dir),
    }


def _supervised_live_run(other_than: Path | None = None) -> Path | None:
    _repair_pth_flags()
    home = Path(_ear_env()["EA_RUNTIME_HOME"])
    current = home / "supervisor" / "state" / "current-run"
    if not current.is_file():
        return None
    run_dir = Path(current.read_text(encoding="utf-8").strip())
    if other_than is not None and run_dir == other_than:
        return None
    if not run_dir.is_dir():
        return None
    try:
        status = paper_status(run_dir)
    except DrillError:
        return None
    if status.get("lease_held") is True and status.get("state") == "running":
        return run_dir
    return None


# ---------------------------------------------------------------------------
# WU-3 observer boundary + WU-2 gate invariant.
# ---------------------------------------------------------------------------


def drill_observer() -> dict[str, Any]:
    baseline_evidence = _load_prior("baseline")
    run_dir = Path(baseline_evidence["run_dir"])
    before = attempt_snapshot(run_dir)
    completed = run_cmd(
        str(EA_BIN),
        "paper",
        "observer",
        "--run-dir",
        str(run_dir),
        "--timeout",
        "120",
        check_exit=False,
        timeout=180.0,
    )
    after = attempt_snapshot(run_dir)
    check(after == before, "the WU-3 observer writes nothing into the attempt")
    document: dict[str, Any] = {}
    try:
        document = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        document = {"observer_status": "unavailable", "detail": completed.stderr[-200:]}
    check(
        document.get("observer_status") in ("available", "unavailable"),
        f"observer outcome is a closed status, got {document.get('observer_status')!r}",
    )
    return {
        "drill": "observer",
        "fault_classes": [],
        "seam": "ea paper observer (WU-3) run once over the baseline attempt; diagnostic only",
        "verdict": "PASS",
        "observer": document,
        "checks": ["observer writes nothing; its availability or opinion affects no outcome"],
    }


def gate_invariant() -> dict[str, Any]:
    head = run_cmd("git", "-C", str(REPO_ROOT), "rev-parse", "HEAD").stdout.strip()
    dirty = run_cmd("git", "-C", str(REPO_ROOT), "status", "--porcelain").stdout.strip()
    evidence_diff = run_cmd(
        "git",
        "-C",
        str(REPO_ROOT),
        "diff",
        "--exit-code",
        BASE_SHA,
        "--",
        "src/ea/product/paper_evidence.py",
        check_exit=False,
    ).returncode
    check(dirty == _GATE_TRACKED_STATE, "the campaign must not modify any tracked file")
    check(evidence_diff == 0, "gate identity (paper_evidence) is byte-identical to start main")
    return {
        "drill": "gate-invariant",
        "verdict": "PASS",
        "checks": [
            f"git HEAD {head} recorded at campaign start; tracked files unchanged while drills ran",
            "paper_evidence.py byte-identical to the authoritative start main",
            "no drill tooling can turn FAIL/INVALID into PASS: the gate is never re-judged here",
            "LIVE stays DENIED: no run mode, credential, or real-broker effect was used",
        ],
    }


# ---------------------------------------------------------------------------

_DRILLS = [
    ("baseline", drill_baseline),
    ("uncertain", drill_uncertain),
    ("durable-write", drill_durable_write),
    ("storm", drill_storm),
    ("torn-tail", drill_torn_tail),
    ("feed", drill_feed),
    ("backup-restore", drill_backup_restore),
    ("observer", drill_observer),
    ("supervised", drill_supervised),
]


def run_drill(name: str, function: Any) -> dict[str, Any]:
    print(f"== drill {name}", flush=True)
    try:
        document = function()
        verdict = document.get("verdict", "PASS")
        print(f"   {verdict}", flush=True)
    except DrillError as error:
        document = {"drill": name, "verdict": "FAIL", "reason": str(error)}
        print(f"   FAIL: {error}", flush=True)
    write_evidence(name, document)
    return cast(dict[str, Any], document)


def main(argv: list[str] | None = None) -> int:
    global FIXTURES, _GATE_TRACKED_STATE
    parser = argparse.ArgumentParser(description="PPV-16 fault campaign drills")
    parser.add_argument(
        "drill", nargs="?", default="all", choices=["all", "matrix", *_DRILL_NAMES()]
    )
    parser.add_argument(
        "--evidence", default=str(CAMPAIGN / f"ppv16-{STAMP}"), help="evidence root override"
    )
    args = parser.parse_args(argv)
    global _EVIDENCE_ROOT
    _EVIDENCE_ROOT = Path(args.evidence)
    evidence_dir().mkdir(parents=True, exist_ok=True)

    if args.drill == "matrix":
        matrix = (REPO_ROOT / ".campaign" / "fault-matrix.md").read_text(encoding="utf-8")
        print(matrix)
        return 0

    FIXTURES = load_fixtures()
    _GATE_TRACKED_STATE = run_cmd(
        "git", "-C", str(REPO_ROOT), "status", "--porcelain"
    ).stdout.strip()
    gate_start = gate_invariant()
    write_evidence("gate-invariant-start", gate_start)
    verdicts: dict[str, str] = {}
    if args.drill == "all":
        for name, function in _DRILLS:
            document = run_drill(name, function)
            verdicts[name] = document["verdict"]
    else:
        document = run_drill(args.drill, dict(_DRILLS)[args.drill])
        verdicts[args.drill] = document["verdict"]
    gate_end = gate_invariant()
    write_evidence("gate-invariant-end", gate_end)
    summary = {
        "campaign": "PPV-16",
        "phase": "M4 fault phase only",
        "start_main_sha": "4a012ea784788ce9371f8dd6b47fdeb7458428ad",
        "evidence_root": str(evidence_dir()),
        "verdicts": verdicts,
        "live": "DENIED",
    }
    write_evidence("summary", summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if all(value in ("PASS", "BLOCKED") for value in verdicts.values()) else 1


def _DRILL_NAMES() -> list[str]:
    return [name for name, _ in _DRILLS]


if __name__ == "__main__":
    raise SystemExit(main())
