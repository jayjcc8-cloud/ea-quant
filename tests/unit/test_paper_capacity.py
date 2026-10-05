"""Bounded audit capacity for local Paper: admission, stop truth and terminal reading.

Every case here is a real production entry (``run_local_paper`` or the real
journal/resume path) under a tightened envelope, and the tightening policy can
only narrow the accepted v1 bounds, never widen them.

One boundary is deliberately synthetic. Research acceptance and executable
package verification are not part of the capacity contract, so a stub accepted
candidate replaces them (see ``_AcceptedCandidate``). Everything downstream --
real scenario loading, journal, engine, broker, audit records, terminal, reader
and resume classification -- is the actual system under test.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from threading import Thread
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from ea.cli.app import app
from ea.core import (
    AuditRecordKind,
    RunBinding,
    RunReference,
    Sha256Digest,
    audit_subject_digest,
)
from ea.core.audit import AUDIT_SUBJECT_BY_RECORD_KIND
from ea.core.paper import canonical_paper_terminal_payload
from ea.experiments.audit import (
    MAX_AUDIT_FRAME_BYTES,
    create_posix_audit_journal,
    production_audit_capacity_limits,
    projected_index_resident_bytes,
)
from ea.experiments.store import CanonicalAttemptManifest, LocalResultStore
from ea.product import load_backtest_scenario
from ea.product.candidate import _configuration
from ea.product.offline_demo import _DemoAudit
from ea.product.paper import (
    PAPER_CAPACITY_EXHAUSTED_REASON,
    PAPER_FACT_CLOSURE_AUDIT_FRAMES,
    PAPER_MARKET_AUDIT_FRAMES,
    PAPER_SHUTDOWN_AUDIT_FRAMES,
    PaperTradingSession,
)
from ea.product.paper_run import (
    PAPER_MAX_DRAIN_POLLS,
    _paper_terminal_counts,
    resume_local_paper,
    run_local_paper,
)
from ea.product.paper_session import request_paper_stop
from unit.test_bounded_round_trips_v1 import bounded_scenario
from unit.test_streaming import RUN_ID, SOURCE, ControlledClock

LINEAGE = Sha256Digest("1" * 64)


def _budget(max_round_trips: int, queued: int = 0) -> int:
    """The documented worst case B = 4 + 8*(P + G) + 3 with G = max_orders + 1."""
    return (
        PAPER_MARKET_AUDIT_FRAMES
        + PAPER_FACT_CLOSURE_AUDIT_FRAMES * (queued + 2 * max_round_trips + 1)
        + PAPER_SHUTDOWN_AUDIT_FRAMES
    )


def _demo_session(tmp_path: Path, *, limit: int) -> PaperTradingSession:
    path = bounded_scenario(tmp_path / f"limit-{limit}", limit=limit)
    document = yaml.safe_load(path.read_text())
    document["funding"]["initial_cash"] = "1000"
    document["risk"]["max_notional"] = "1000"
    document["execution"]["slippage"] = {
        "policy": "deterministic-slippage-v1",
        "slippage_bps": "100",
    }
    path.write_text(yaml.safe_dump(document))
    scenario = load_backtest_scenario(path)
    binding = RunBinding(RunReference(RUN_ID, LINEAGE), Sha256Digest("2" * 64))
    clock = ControlledClock()
    return PaperTradingSession(
        scenario,
        binding=binding,
        audit=_DemoAudit(binding, scenario.spec_set),
        clock=clock,
        monotonic=clock.monotonic,
        source_id=SOURCE,
        prices=(100.0,),
        stop_requested=lambda: False,
    )


class _AcceptedCandidate:
    """Synthetic accepted-candidate boundary for the capacity SUT.

    The research acceptance pipeline (evidence admission, holdout relation,
    explicit decision, executable package verification) is a separate subsystem
    and is not exercised here: it is expensive and not part of the capacity
    contract. The real entry still preflights the scenario, loads it through the
    production loader, and runs the real journal, engine, broker, terminal and
    reader unchanged.
    """

    def __init__(self, workspace: Path, candidate_id: str, configuration: bytes) -> None:
        self.workspace = workspace
        self.candidate_id = candidate_id
        self.strategy_package = None
        self.artifact_sha256 = None
        self._artifact_bytes = None
        self._configuration_bytes = configuration

    def document(self) -> dict[str, Any]:
        return {
            "schema": "ea.accepted-candidate-binding.v1",
            "candidate_id": self.candidate_id,
            "execution_scope": "offline-backtest-only",
        }


@pytest.fixture
def accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, Path, int]:
    """A bounded built-in scenario plus the stubbed acceptance boundary."""
    scenario = bounded_scenario(tmp_path / "inputs", limit=3)
    document = yaml.safe_load(scenario.read_text())
    document["funding"]["initial_cash"] = "1000"
    document["risk"]["max_notional"] = "1000"
    document["execution"]["slippage"] = {
        "policy": "deterministic-slippage-v1",
        "slippage_bps": "100",
    }
    scenario.write_text(yaml.safe_dump(document))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    candidate_id = "synthetic-accepted-candidate"
    candidate = _AcceptedCandidate(workspace, candidate_id, b"")
    # The accepted configuration is exactly the configuration of the real loaded
    # scenario, so the production entry still performs its own config equality
    # check against genuine scenario bytes.
    candidate._configuration_bytes = _configuration(
        json.loads(load_backtest_scenario(scenario).canonical_bytes)
    )
    from ea.product import paper_run as paper_run_module

    monkeypatch.setattr(
        paper_run_module,
        "inspect_candidate_binding",
        lambda _workspace, _candidate_id: candidate,
    )
    monkeypatch.setattr(paper_run_module, "_load_binding", lambda binding: binding)
    return workspace, candidate_id, scenario, document["strategy"]["max_round_trips"]


class _FaultingJournal:
    """The real journal with one record kind failing, plus the kinds that committed."""

    def __init__(self, journal: Any, fail_kind: AuditRecordKind | None, message: str) -> None:
        self._journal = journal
        self._fail_kind = fail_kind
        self._message = message
        self.kinds: list[AuditRecordKind] = []

    def append(self, **kwargs: Any) -> Any:
        if kwargs["record_kind"] is self._fail_kind:
            raise OSError(self._message)
        acknowledgement = self._journal.append(**kwargs)
        self.kinds.append(kwargs["record_kind"])
        return acknowledgement

    def __getattr__(self, name: str) -> Any:
        return getattr(self._journal, name)


@pytest.fixture
def faulting(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[AuditRecordKind | None], list[_FaultingJournal]]:
    """Inject one bounded append failure into the production journal factory."""
    from ea.product import paper_run as paper_run_module

    create = paper_run_module.create_posix_audit_journal
    journals: list[_FaultingJournal] = []

    def apply(
        fail_kind: AuditRecordKind | None, message: str = "injected audit failure"
    ) -> list[_FaultingJournal]:
        def factory(prepared: Any, **kwargs: Any) -> Any:
            wrapper = _FaultingJournal(create(prepared, **kwargs), fail_kind, message)
            journals.append(wrapper)
            return wrapper

        monkeypatch.setattr(paper_run_module, "create_posix_audit_journal", factory)
        return journals

    return apply


@pytest.fixture
def tighten(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Patch the Paper journal factory to a tightened, still-production envelope."""
    from ea.product import paper_run as paper_run_module

    create = paper_run_module.create_posix_audit_journal

    def apply(*, byte_frames: int | None = None, **overrides: int) -> None:
        def factory(prepared: Any, **kwargs: Any) -> Any:
            if byte_frames is None:
                narrowed = production_audit_capacity_limits().tightened(**overrides)
                return create(prepared, capacity_limits=narrowed, **kwargs)
            journal = create(prepared, **kwargs)
            journal.tighten_capacity_limits(
                max_journal_bytes=journal.verified_bytes + byte_frames * MAX_AUDIT_FRAME_BYTES
            )
            return journal

        monkeypatch.setattr(paper_run_module, "create_posix_audit_journal", factory)

    return apply


def test_market_budget_matches_the_checked_maxima(tmp_path: Path) -> None:
    engine = _demo_session(tmp_path, limit=256)
    assert engine.max_orders == 512
    assert PAPER_MARKET_AUDIT_FRAMES == 4 and PAPER_FACT_CLOSURE_AUDIT_FRAMES == 8
    # P = 0 with the largest accepted order bound.
    assert engine.market_audit_frames() == _budget(256) == 4111
    # The runtime queue bound is max_pending_facts = 64, so P = 64 is the general
    # maximum; 4111 is only the P = 0 case.
    assert _budget(256, queued=64) == 4623
    assert _demo_session(tmp_path, limit=1).market_audit_frames() == _budget(1) == 31


def test_ordinary_probe_excludes_the_protected_shutdown_allowance(tmp_path: Path) -> None:
    engine = _demo_session(tmp_path, limit=3)
    # B already counts the shutdown allowance that the journal reserve funds, so
    # only B minus that allowance may be charged against ordinary capacity.
    assert engine.market_audit_frames() == _budget(3)
    assert engine.market_ordinary_audit_frames() == _budget(3) - PAPER_SHUTDOWN_AUDIT_FRAMES


def test_in_process_port_without_a_resource_contract_never_refuses(tmp_path: Path) -> None:
    engine = _demo_session(tmp_path, limit=3)
    assert not hasattr(_DemoAudit, "has_capacity_for")
    assert engine.has_market_capacity() is True
    asked: list[int] = []

    def probing(frames: int) -> bool:
        asked.append(frames)
        return False

    engine.audit.has_capacity_for = probing  # type: ignore[attr-defined]
    assert engine.has_market_capacity() is False
    assert asked == [engine.market_ordinary_audit_frames()]


def test_terminal_reader_is_defensive_about_foreign_and_older_payloads() -> None:
    assert _paper_terminal_counts(b"{") == (None, 0, 0)
    assert _paper_terminal_counts(b"[]") == (None, 0, 0)
    assert _paper_terminal_counts(b'{"schema":"ea.audit-run-terminal.v1"}') == (None, 0, 0)
    assert _paper_terminal_counts(
        b'{"schema":"ea.audit-paper-terminal.v1",'
        b'"reason":7,"pending_orders":-1,"pending_facts":false}'
    ) == (None, 0, 0)


def test_exhausted_count_envelope_refuses_before_any_market_or_issued_fact(
    accepted: tuple[Path, str, Path, int],
    tmp_path: Path,
    tighten: Callable[..., None],
) -> None:
    workspace, candidate_id, scenario, round_trips = accepted
    # One RUN_PREPARED record and the complete protected shutdown reserve, one
    # record short of the full budget B.
    tighten(max_records=1 + _budget(round_trips) - 1)
    result = run_local_paper(workspace, candidate_id, scenario, tmp_path / "runs", event_limit=3)
    assert result["market_events"] == 0
    assert result["orders"] == 0 and result["fills"] == 0 and result["observed_fills"] == 0
    assert result["reason"] == PAPER_CAPACITY_EXHAUSTED_REASON
    assert result["state"] == "failed"
    assert result["incomplete"] is False
    assert result["terminal_durable"] is True
    assert result["reconciliation"] == "not_required_no_dispatch"
    assert result["pending_facts"] == 0 and result["pending_orders"] == 0
    # The durable attempt is closed, and the closed terminal carries no unresolved
    # work, so nothing asks for an operator decision.
    admission = resume_local_paper(Path(result["run_dir"]))
    assert admission["state"] == "terminal"
    assert admission["reconciliation_required"] is False
    assert admission["reason"] == PAPER_CAPACITY_EXHAUSTED_REASON
    assert admission["pending_orders"] == 0 and admission["pending_facts"] == 0


def test_exact_count_envelope_admits_one_market_then_closes_with_attention_truth(
    accepted: tuple[Path, str, Path, int],
    tmp_path: Path,
    tighten: Callable[..., None],
) -> None:
    workspace, candidate_id, scenario, round_trips = accepted
    # One RUN_PREPARED record plus the complete budget B: B - 3 ordinary frames
    # are available, which admits exactly one whole market and its closure.
    tighten(max_records=1 + _budget(round_trips))
    result = run_local_paper(workspace, candidate_id, scenario, tmp_path / "runs", event_limit=3)
    # The funded market executed a full callback; the next admission was refused
    # before it ran, so its order is retained as unresolved rather than retried.
    assert result["market_events"] == 1
    assert result["orders"] == 1 and result["fills"] == 0 and result["observed_fills"] == 0
    assert result["pending_facts"] == 0 and result["pending_orders"] == 1
    assert result["reason"] == PAPER_CAPACITY_EXHAUSTED_REASON
    assert result["state"] == "failed"
    assert result["incomplete"] is False
    assert result["terminal_durable"] is True
    assert result["reconciliation"] == "match"
    # The durable terminal keeps the unresolved pending order as operator
    # attention: it is neither recovered nor continued.
    admission = resume_local_paper(Path(result["run_dir"]))
    assert admission["state"] == "terminal"
    assert admission["reconciliation_required"] is True
    assert admission["reason"] == PAPER_CAPACITY_EXHAUSTED_REASON
    assert admission["pending_orders"] == result["pending_orders"] == 1
    assert admission["pending_facts"] == result["pending_facts"] == 0
    reopened = CliRunner().invoke(app, ["paper", "resume", "--run-dir", result["run_dir"]])
    assert reopened.exit_code == 3


def test_operator_stop_at_the_reserve_boundary_still_closes_durably(
    accepted: tuple[Path, str, Path, int],
    tmp_path: Path,
    tighten: Callable[..., None],
) -> None:
    workspace, candidate_id, scenario, _ = accepted
    # No ordinary capacity at all: one RUN_PREPARED record and exactly the three
    # reserved shutdown frames. A normal operator stop must still close durably.
    tighten(max_records=1 + PAPER_SHUTDOWN_AUDIT_FRAMES)
    workers: list[Thread] = []

    def stop_before_the_first_market(attempt: Path) -> None:
        worker = Thread(
            target=request_paper_stop,
            args=(attempt,),
            kwargs={"timeout_seconds": 10.0},
        )
        workers.append(worker)
        worker.start()
        stop_file = attempt / "outputs" / "paper-stop.json"
        deadline = time.monotonic() + 5.0
        while not stop_file.exists():
            assert time.monotonic() < deadline, "operator stop was not published in time"
            time.sleep(0.01)

    result = run_local_paper(
        workspace,
        candidate_id,
        scenario,
        tmp_path / "runs",
        event_limit=3,
        on_ready=stop_before_the_first_market,
    )
    for worker in workers:
        worker.join(timeout=10.0)
        assert not worker.is_alive()
    # The operator stop is observed before any market, so it keeps its own reason
    # instead of being misreported as an exhausted envelope.
    assert result["market_events"] == 0
    assert result["orders"] == 0 and result["fills"] == 0
    assert result["state"] == "stopped"
    assert result["reason"] == "stop_requested"
    assert result["incomplete"] is False
    assert result["terminal_durable"] is True
    assert result["reconciliation"] == "not_required_no_dispatch"
    admission = resume_local_paper(Path(result["run_dir"]))
    assert admission["state"] == "terminal"
    assert admission["reconciliation_required"] is False


def test_byte_dimension_binds_first_while_count_and_index_allow(
    accepted: tuple[Path, str, Path, int],
    tmp_path: Path,
    tighten: Callable[..., None],
) -> None:
    workspace, candidate_id, scenario, round_trips = accepted
    # The byte dimension must carry the reserve too, so B worst-case frames of
    # room is exactly the admission requirement and one less is already short.
    needed = _budget(round_trips)
    tighten(byte_frames=needed - 1)
    refused = run_local_paper(
        workspace, candidate_id, scenario, tmp_path / "refused", event_limit=3
    )
    assert refused["market_events"] == 0 and refused["orders"] == 0 and refused["fills"] == 0
    assert refused["reason"] == PAPER_CAPACITY_EXHAUSTED_REASON
    assert refused["incomplete"] is False and refused["terminal_durable"] is True
    assert refused["reconciliation"] == "not_required_no_dispatch"
    tighten(byte_frames=needed)
    admitted = run_local_paper(
        workspace, candidate_id, scenario, tmp_path / "admitted", event_limit=3
    )
    # Exactly one whole market and its closure fit, then the byte dimension binds
    # and the reserve still closes the attempt durably.
    assert admitted["market_events"] == 1 and admitted["orders"] == 1
    assert admitted["reason"] == PAPER_CAPACITY_EXHAUSTED_REASON
    assert admitted["incomplete"] is False and admitted["terminal_durable"] is True
    assert admitted["reconciliation"] == "match"


def test_index_resize_projection_binds_first_while_count_and_bytes_allow(
    accepted: tuple[Path, str, Path, int],
    tmp_path: Path,
    tighten: Callable[..., None],
) -> None:
    workspace, candidate_id, scenario, round_trips = accepted
    # The projection must cover the prepared record, the reserve and the whole
    # ordinary budget (B - 3); one record less already refuses before a market.
    tighten(max_index_resident_bytes=projected_index_resident_bytes(1 + _budget(round_trips) - 1))
    refused = run_local_paper(
        workspace, candidate_id, scenario, tmp_path / "refused", event_limit=3
    )
    assert refused["market_events"] == 0 and refused["orders"] == 0
    assert refused["reason"] == PAPER_CAPACITY_EXHAUSTED_REASON
    assert refused["state"] == "failed"
    assert refused["incomplete"] is False
    assert refused["terminal_durable"] is True
    tighten(max_index_resident_bytes=projected_index_resident_bytes(1 + _budget(round_trips)))
    admitted = run_local_paper(
        workspace, candidate_id, scenario, tmp_path / "admitted", event_limit=3
    )
    # The exact projection admits exactly one whole market, then the index
    # dimension binds and the reserve still closes the attempt durably.
    assert admitted["market_events"] == 1 and admitted["orders"] == 1
    assert admitted["reason"] == PAPER_CAPACITY_EXHAUSTED_REASON
    assert admitted["incomplete"] is False and admitted["terminal_durable"] is True
    assert admitted["reconciliation"] == "match"


def test_bootstrap_that_cannot_fund_its_own_record_never_starts_the_runtime(
    accepted: tuple[Path, str, Path, int],
    tmp_path: Path,
    tighten: Callable[..., None],
) -> None:
    workspace, candidate_id, scenario, _ = accepted
    tighten(max_journal_bytes=64)
    result = run_local_paper(workspace, candidate_id, scenario, tmp_path / "runs", event_limit=3)
    assert result["state"] == "failed"
    assert result["failure_type"] == "AuditContractError"
    assert result["incomplete"] is True
    assert result["terminal_durable"] is False
    assert not (Path(result["run_dir"]) / "funding.json").exists()


def test_full_run_under_a_small_envelope_keeps_default_behavior_and_drain_deadline(
    accepted: tuple[Path, str, Path, int],
    tmp_path: Path,
    tighten: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, candidate_id, scenario, round_trips = accepted
    # One prepared record plus four complete market budgets: the whole 20-market
    # run consumes a bit over one budget of ordinary records and needs one budget
    # (B - 3) of ordinary room free at every admission.
    tighten(max_records=1 + 4 * _budget(round_trips))
    polls: list[int] = []
    from ea.product import paper_run as paper_run_module

    stream = paper_run_module.run_local_market_stream

    def recording_stream(**kwargs: Any) -> Any:
        polls.append(kwargs["max_drain_polls"])
        return stream(**kwargs)

    monkeypatch.setattr(paper_run_module, "run_local_market_stream", recording_stream)
    result = run_local_paper(workspace, candidate_id, scenario, tmp_path / "runs", event_limit=20)
    assert polls == [PAPER_MAX_DRAIN_POLLS]
    assert PAPER_MAX_DRAIN_POLLS >= 2 * 64
    # Unchanged default semantics for a source-exhausted run: no capacity reason.
    assert result["reason"] == "source_exhausted" and result["state"] == "failed"
    assert result["market_events"] == 20
    assert result["fills"] == 6 and result["completed_round_trips"] == 3
    assert result["duplicate_facts"] == 6 and result["observed_fills"] == 6
    assert result["pending_facts"] == 0 and result["incomplete"] is False
    assert result["reconciliation"] == "match" and result["terminal_durable"] is True


def test_append_failure_keeps_the_original_evidence_and_writes_no_terminal(
    accepted: tuple[Path, str, Path, int],
    tmp_path: Path,
    faulting: Callable[[AuditRecordKind | None], list[_FaultingJournal]],
) -> None:
    workspace, candidate_id, scenario, _ = accepted
    journals = faulting(AuditRecordKind.PAPER_SUBMISSION_AUTHORIZATION)
    result = run_local_paper(workspace, candidate_id, scenario, tmp_path / "runs", event_limit=3)
    assert result["state"] == "failed"
    assert result["failure_type"] == "OSError"
    assert result["reason"] == "callback_error"
    assert result["incomplete"] is True
    assert result["terminal_durable"] is False
    # The real journal kept its durable prefix and never fabricated a terminal.
    assert journals and AuditRecordKind.RUN_TERMINAL not in journals[0].kinds
    admission = resume_local_paper(Path(result["run_dir"]))
    assert admission["state"] == "incomplete"


def test_closing_reconciliation_failure_keeps_the_failure_and_writes_no_terminal(
    accepted: tuple[Path, str, Path, int],
    tmp_path: Path,
    faulting: Callable[[AuditRecordKind | None], list[_FaultingJournal]],
) -> None:
    workspace, candidate_id, scenario, _ = accepted
    journals = faulting(AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME)
    result = run_local_paper(workspace, candidate_id, scenario, tmp_path / "runs", event_limit=3)
    assert result["state"] == "failed"
    assert result["failure_type"] == "OSError"
    assert result["incomplete"] is True
    assert result["terminal_durable"] is False
    assert journals and AuditRecordKind.RUN_TERMINAL not in journals[0].kinds
    assert AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME not in journals[0].kinds
    admission = resume_local_paper(Path(result["run_dir"]))
    assert admission["state"] == "incomplete"


def test_callback_crash_keeps_the_failure_and_classifies_incomplete(
    accepted: tuple[Path, str, Path, int],
    tmp_path: Path,
    faulting: Callable[[AuditRecordKind | None], list[_FaultingJournal]],
) -> None:
    workspace, candidate_id, scenario, _ = accepted
    journals = faulting(None)
    result = run_local_paper(
        workspace,
        candidate_id,
        scenario,
        tmp_path / "runs",
        event_limit=3,
        crash_after=AuditRecordKind.PAPER_SUBMISSION_AUTHORIZATION,
    )
    assert result["state"] == "failed"
    assert result["failure_type"] == "RuntimeError"
    assert result["incomplete"] is True
    assert result["terminal_durable"] is False
    assert journals and AuditRecordKind.RUN_TERMINAL not in journals[0].kinds
    admission = resume_local_paper(Path(result["run_dir"]))
    assert admission["state"] == "incomplete"


def _terminal_attempt(
    root: Path,
    *,
    reason: str,
    pending_orders: int,
    pending_facts: int = 0,
) -> Path:
    """Write one real journal whose last record is a Paper terminal for ``reason``."""
    root.mkdir(parents=True, exist_ok=True)
    store = LocalResultStore(root.resolve())
    manifest_bytes = json.dumps(
        {
            "schema": "ea.local-paper-attempt.v1",
            "run_id": RUN_ID.value,
            "lineage_sha256": LINEAGE.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    prepared = store.prepare_canonical_attempt(
        CanonicalAttemptManifest(RunReference(RUN_ID, LINEAGE), manifest_bytes)
    )
    journal = create_posix_audit_journal(prepared.audit)
    payload = canonical_paper_terminal_payload(
        RUN_ID,
        dispatch_sequence=1,
        reason=reason,
        portfolio_snapshot_sha256=Sha256Digest("3" * 64),
        risk_state_sha256=Sha256Digest("4" * 64),
        pending_facts=pending_facts,
        pending_orders=pending_orders,
        fill_count=0,
    )
    journal.append(
        record_kind=AuditRecordKind.RUN_TERMINAL,
        subject_kind=AUDIT_SUBJECT_BY_RECORD_KIND[AuditRecordKind.RUN_TERMINAL],
        subject_sha256=audit_subject_digest(AuditRecordKind.RUN_TERMINAL, payload),
        canonical_payload=payload,
    )
    journal.close()
    store.close()
    return root / RUN_ID.value


def test_capacity_terminal_with_unresolved_work_asks_for_operator_attention(
    tmp_path: Path,
) -> None:
    run_dir = _terminal_attempt(
        tmp_path / "runs", reason=PAPER_CAPACITY_EXHAUSTED_REASON, pending_orders=1
    )
    admission = resume_local_paper(run_dir)
    assert admission["state"] == "terminal"
    assert admission["reconciliation_required"] is True
    assert admission["reason"] == PAPER_CAPACITY_EXHAUSTED_REASON
    assert admission["pending_orders"] == 1 and admission["pending_facts"] == 0
    assert admission["outbound_intents"] == []
    assert admission["incomplete_outbound_intents"] == []
    reopened = CliRunner().invoke(app, ["paper", "resume", "--run-dir", str(run_dir)])
    assert reopened.exit_code == 3


def test_older_terminal_payloads_keep_their_previous_reader_behavior(tmp_path: Path) -> None:
    run_dir = _terminal_attempt(
        tmp_path / "runs", reason="stop_requested", pending_orders=0, pending_facts=0
    )
    admission = resume_local_paper(run_dir)
    assert admission == {
        "state": "terminal",
        "run_id": RUN_ID.value,
        "reconciliation_required": False,
        "outbound_intents": [],
        "incomplete_outbound_intents": [],
    }
    reopened = CliRunner().invoke(app, ["paper", "resume", "--run-dir", str(run_dir)])
    assert reopened.exit_code == 0
