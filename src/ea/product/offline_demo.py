"""One deterministic installed-wheel demonstration of the existing offline engine."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from importlib import resources
from pathlib import Path
from typing import Any

from ea.composition.lifecycle import (
    create_phase1_historical_economic_gate,
    create_phase1_historical_lifecycle,
)
from ea.core import (
    EMPTY_CHAIN_HEAD_SHA256,
    EMPTY_RECORD_SHA256,
    AuditAppendAcknowledgement,
    AuditContractError,
    AuditLogicalKey,
    AuditRecord,
    AuditRecordKind,
    AuditSubjectKind,
    CanonicalDecimal,
    CashReconciliationBalance,
    EconomicId,
    EconomicOwnerKind,
    ExecutionPolicyId,
    ExecutionPolicyRef,
    FactProvenanceId,
    Fill,
    GlobalHaltSnapshot,
    Instrument,
    InstrumentExecutionSpec,
    InstrumentGateSnapshot,
    InstrumentRiskLimit,
    InstrumentSpecId,
    InstrumentSpecSetId,
    MarketDataEnvelope,
    OutcomeCode,
    Phase1PortfolioPolicyEntry,
    PortfolioPolicyId,
    PositionReconciliationBalance,
    PriceDomain,
    ReconciliationObservationKind,
    ReconciliationScopeKind,
    ReplayWindow,
    RiskDecisionKind,
    RiskPolicyId,
    RunBinding,
    RunId,
    RunReference,
    RuntimeIdentifier,
    SettlementCurrency,
    Sha256Digest,
    SignalDirection,
    SourceNamespace,
    VenueId,
    audit_chain_head,
    audit_record_digest,
    audit_subject_digest,
    build_instrument_spec_set,
    canonical_audit_record_header_bytes,
    canonical_reconciliation_outcome_bytes,
    canonical_run_prepared_audit_payload,
    create_audit_append_acknowledgement,
    create_audit_record,
    create_phase1_portfolio_policy,
    create_phase1_risk_policy,
    create_reconciliation_observation,
    portfolio_snapshot_digest,
)
from ea.data import (
    create_phase1_historical_market_data_source,
    create_phase1_historical_market_source_bridge,
    decode_phase1_ohlcv_csv,
)
from ea.execution import create_phase1_order_authority
from ea.portfolio import create_portfolio_ledger, create_portfolio_planning_authority
from ea.reconciliation import create_phase1_reconciliation_authority
from ea.runtime import (
    create_active_market_dispatch_verifier,
    create_phase1_historical_market_runtime,
)
from ea.strategy import create_strategy_signal_authority

_RUN_ID = RunId("123e4567-e89b-42d3-a456-426614174000")
_INSTRUMENT = Instrument(VenueId("XNAS"), "AAPL")
_USD = SettlementCurrency("USD")
_EXECUTION_POLICY = ExecutionPolicyRef(
    ExecutionPolicyId("phase1.next-bar-close.v1"),
    Sha256Digest("1" * 64),
)
_SOURCE_NAMESPACE = SourceNamespace("phase1.historical-matcher.v1")
_RECONCILIATION_SOURCE = SourceNamespace("reconciliation.demo")
_LEDGER_WATERMARK = SourceNamespace("ledger.portfolio")
_FIXED_TIME = datetime(2026, 1, 2, 9, 32, tzinfo=UTC)
_ATTEMPT_NAME = "phase1-demo-v1"

__all__ = [
    "create_portfolio_ledger",
    "create_portfolio_planning_authority",
    "create_phase1_order_authority",
    "create_phase1_historical_economic_gate",
    "create_phase1_historical_lifecycle",
]


class DemoMode(StrEnum):
    """Bounded test modes for the fixed demonstration scenario."""

    ACCEPT = "accept"
    RISK_REJECT = "risk_reject"
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"


class OfflineDemoInputError(ValueError):
    """Raised before an output attempt starts when the requested path is unsafe."""


class OfflineDemoFailure(RuntimeError):
    """Raised after preserving evidence for a fail-closed economic outcome."""

    code: OutcomeCode
    output_directory: Path
    audit_bytes: bytes

    def __init__(
        self,
        code: OutcomeCode,
        message: str,
        output_directory: Path,
        *,
        audit_bytes: bytes = b"",
    ) -> None:
        self.code = code
        self.output_directory = output_directory
        self.audit_bytes = audit_bytes
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class OfflineDemoResult:
    """Stable completion handle for the generated artifact directory."""

    status: str
    output_directory: Path


class _DemoAudit:
    """In-process audit port whose canonical records are exported as a replay artifact."""

    def __init__(self, binding: RunBinding, spec_set: Any) -> None:
        self.binding = binding
        self._spec_set = spec_set
        self.records: list[AuditRecord] = []
        self._index: dict[AuditLogicalKey, tuple[bytes, AuditAppendAcknowledgement]] = {}

    def append(
        self,
        *,
        record_kind: AuditRecordKind,
        subject_kind: AuditSubjectKind,
        subject_sha256: Sha256Digest,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement:
        key = AuditLogicalKey(record_kind, subject_kind, subject_sha256)
        existing = self._index.get(key)
        if existing is not None:
            if existing[0] != canonical_payload:
                raise AuditContractError(
                    OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                    "audit logical key conflicts with retained canonical bytes",
                )
            return existing[1]
        previous = self.records[-1] if self.records else None
        record = create_audit_record(
            binding=self.binding,
            owner_sequence=len(self.records) + 1,
            record_kind=record_kind,
            subject_kind=subject_kind,
            subject_sha256=subject_sha256,
            canonical_payload=canonical_payload,
            previous_record_sha256=(
                EMPTY_RECORD_SHA256 if previous is None else audit_record_digest(previous)
            ),
            previous_chain_head_sha256=(
                EMPTY_CHAIN_HEAD_SHA256 if previous is None else audit_chain_head(previous)
            ),
            spec_set=self._spec_set,
        )
        acknowledgement = create_audit_append_acknowledgement(record)
        self.records.append(record)
        self._index[key] = (canonical_payload, acknowledgement)
        return acknowledgement

    def settle_append(
        self,
        *,
        logical_key: AuditLogicalKey,
        canonical_payload: bytes,
    ) -> AuditAppendAcknowledgement | None:
        existing = self._index.get(logical_key)
        if existing is None:
            return None
        if existing[0] != canonical_payload:
            raise AuditContractError(
                OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
                "audit settlement bytes conflict with retained record",
            )
        return existing[1]


class _GlobalHaltView:
    def __init__(self, run_id: RunId) -> None:
        self._state = GlobalHaltSnapshot(run_id, False, 0)

    def current_state(self) -> GlobalHaltSnapshot:
        return self._state


class _InstrumentGateView:
    def __init__(self, run_id: RunId, instrument: Instrument) -> None:
        self._run_id = run_id
        self._instrument = instrument
        self._held_for: EconomicId | None = None

    def hold_for(self, order_id: EconomicId) -> None:
        self._held_for = order_id

    def current_for(self, instrument: Instrument) -> InstrumentGateSnapshot:
        if instrument != self._instrument:
            raise ValueError("instrument gate does not cover the requested instrument")
        return InstrumentGateSnapshot(
            self._run_id,
            self._instrument,
            self._held_for,
            Sha256Digest("3" * 64),
            1,
            False,
        )


def _canonical_json(document: object) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _sample_bytes() -> bytes:
    return resources.files("ea.product").joinpath("phase1_demo_ohlcv_v1.csv").read_bytes()


def _spec_set() -> Any:
    return build_instrument_spec_set(
        InstrumentSpecSetId("phase1.demo.v1"),
        (
            InstrumentExecutionSpec(
                instrument=_INSTRUMENT,
                specification_id=InstrumentSpecId("xnas.aapl.v1"),
                price_quantum=CanonicalDecimal("0.01"),
                quantity_quantum=CanonicalDecimal("1"),
                settlement_currency=_USD,
                currency_quantum=CanonicalDecimal("0.01"),
                contract_multiplier=CanonicalDecimal("1"),
                price_domain=PriceDomain.POSITIVE,
            ),
        ),
    )


def _binding(sample: bytes) -> RunBinding:
    lineage = Sha256Digest(sha256(b"ea.offline-demo.lineage.v1\0" + sample).hexdigest())
    scenario = _canonical_json(
        {
            "execution_policy": _EXECUTION_POLICY.identifier.value,
            "run_id": _RUN_ID.value,
            "schema": "ea.offline-demo-input.v1",
            "strategy": "bounded-long-v1",
            "target_quantity": "2",
        }
    )
    manifest = Sha256Digest(
        sha256(b"ea.offline-demo.manifest.v1\0" + scenario + sample).hexdigest()
    )
    return RunBinding(RunReference(_RUN_ID, lineage), manifest)


def _risk_policy(spec_set: Any, mode: DemoMode) -> Any:
    limits = (
        ()
        if mode is DemoMode.RISK_REJECT
        else (
            InstrumentRiskLimit(
                instrument=_INSTRUMENT,
                maximum_order_quantity=CanonicalDecimal("5"),
                maximum_absolute_position=CanonicalDecimal("5"),
            ),
        )
    )
    return create_phase1_risk_policy(
        policy_id=RiskPolicyId("phase1.demo.v1"),
        spec_set=spec_set,
        execution_policy=_EXECUTION_POLICY,
        instrument_limits=limits,
    )


def _append_reconciliation(audit: _DemoAudit, outcome: Any) -> None:
    payload = canonical_reconciliation_outcome_bytes(outcome)
    audit.append(
        record_kind=AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME,
        subject_kind=AuditSubjectKind.RECONCILIATION_OUTCOME,
        subject_sha256=audit_subject_digest(
            AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME,
            payload,
        ),
        canonical_payload=payload,
    )


def _observation(
    *,
    spec_set: Any,
    snapshot: Any,
    position: bool,
    mismatch: bool,
) -> Any:
    balances: tuple[PositionReconciliationBalance | CashReconciliationBalance, ...]
    if position:
        balances = tuple(
            PositionReconciliationBalance(
                balance.instrument,
                (
                    CanonicalDecimal("3")
                    if mismatch and balance.instrument == _INSTRUMENT
                    else balance.quantity
                ),
            )
            for balance in snapshot.position_balances
        )
        kind = ReconciliationObservationKind.POSITION_SNAPSHOT
        scope = ReconciliationScopeKind.POSITION
        sequence = 1
    else:
        balances = tuple(
            CashReconciliationBalance(balance.currency, balance.amount)
            for balance in snapshot.cash_balances
        )
        kind = ReconciliationObservationKind.CASH_SNAPSHOT
        scope = ReconciliationScopeKind.CASH
        sequence = 2
    return create_reconciliation_observation(
        run_id=_RUN_ID,
        spec_set=spec_set,
        observation_id=EconomicId(
            _RUN_ID,
            EconomicOwnerKind.RECONCILIATION_OBSERVATION,
            sequence,
        ),
        kind=kind,
        source_namespace=_RECONCILIATION_SOURCE,
        source_sequence=sequence,
        occurred_at=_FIXED_TIME,
        available_at=_FIXED_TIME,
        watermark_namespace=_LEDGER_WATERMARK,
        watermark_sequence=snapshot.ledger_sequence,
        declared_scope_kind=scope,
        declared_scope_id=RuntimeIdentifier("portfolio.default"),
        provenance_id=FactProvenanceId("reconciliation.demo.v1"),
        provenance_payload_sha256=portfolio_snapshot_digest(snapshot),
        balances=balances,
    )


def _audit_bytes(records: list[AuditRecord]) -> bytes:
    lines = []
    for record in records:
        lines.append(
            _canonical_json(
                {
                    "chain_head_sha256": audit_chain_head(record).value,
                    "header": json.loads(canonical_audit_record_header_bytes(record)),
                    "payload": json.loads(record.canonical_payload),
                    "record_sha256": audit_record_digest(record).value,
                }
            )
        )
    return b"\n".join(lines) + b"\n"


def _safe_attempt_directory(output_root: Path) -> Path:
    if not isinstance(output_root, Path) or not output_root.is_absolute():
        raise OfflineDemoInputError("output root must be an absolute path")
    if output_root.exists() and output_root.is_symlink():
        raise OfflineDemoInputError("output root cannot be a symlink")
    try:
        output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise OfflineDemoInputError("output root could not be created") from error
    if not output_root.is_dir():
        raise OfflineDemoInputError("output root must be a directory")
    attempt = output_root / _ATTEMPT_NAME
    try:
        attempt.mkdir(mode=0o700)
    except FileExistsError as error:
        raise OfflineDemoInputError("output attempt already exists") from error
    except OSError as error:
        raise OfflineDemoInputError("output attempt could not be created") from error
    return attempt


def _write_once(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        raise OfflineDemoFailure(
            OutcomeCode.DURABILITY_RESULT_WRITE_FAILED,
            "offline demo result could not be written",
            path.parent,
        ) from error


def _failure_document(
    code: OutcomeCode,
    message: str,
    *,
    run_status: str = "failed",
    trade_outcome: str | None = None,
) -> bytes:
    document: dict[str, object] = {
        "code": code.value,
        "message": message,
        "schema": "ea.offline-demo-failure.v1",
        "status": run_status,
    }
    document["run_status"] = run_status
    document["failure"] = {"code": code.value}
    if trade_outcome is not None:
        document["trade_outcome"] = trade_outcome
    return _canonical_json(document) + b"\n"


def _id_document(identity: EconomicId) -> dict[str, object]:
    return {
        "owner_kind": identity.owner_kind.value,
        "owner_sequence": identity.owner_sequence,
        "run_id": identity.run_id.value,
    }


def _execute(mode: DemoMode) -> tuple[dict[str, object], bytes]:
    sample = _sample_bytes()
    spec_set = _spec_set()
    binding = _binding(sample)
    audit = _DemoAudit(binding, spec_set)
    prepared = audit.append(
        record_kind=AuditRecordKind.RUN_PREPARED,
        subject_kind=AuditSubjectKind.RUN_MANIFEST,
        subject_sha256=binding.manifest_sha256,
        canonical_payload=canonical_run_prepared_audit_payload(binding),
    )
    window = ReplayWindow(
        datetime(2026, 1, 2, 9, 0, tzinfo=UTC),
        datetime(2026, 1, 2, 10, 0, tzinfo=UTC),
    )
    dataset = decode_phase1_ohlcv_csv(sample, replay_window=window)
    source = create_phase1_historical_market_data_source(dataset)
    runtime = create_phase1_historical_market_runtime(
        run_id=_RUN_ID,
        spec_set=spec_set,
        source=create_phase1_historical_market_source_bridge(source),
    )
    portfolio_policy = create_phase1_portfolio_policy(
        policy_id=PortfolioPolicyId("phase1.demo.v1"),
        entries=(
            Phase1PortfolioPolicyEntry(
                instrument=_INSTRUMENT,
                target_quantity=CanonicalDecimal("2"),
            ),
        ),
        spec_set=spec_set,
    )
    risk_policy = _risk_policy(spec_set, mode)
    economic_gate = create_phase1_historical_economic_gate(
        run_id=_RUN_ID,
        spec_set=spec_set,
        execution_policy=_EXECUTION_POLICY,
        risk_policy=risk_policy,
    )
    planner = create_portfolio_planning_authority(
        run_id=_RUN_ID,
        ledger=economic_gate.ledger,
        spec_set=spec_set,
        policy=portfolio_policy,
        execution_policy=_EXECUTION_POLICY,
    )
    risk = economic_gate.risk_authority
    orders = create_phase1_order_authority(
        run_id=_RUN_ID,
        spec_set=spec_set,
        execution_policy=_EXECUTION_POLICY,
        risk_policy=risk_policy,
        risk_result_verifier=risk,
    )
    gate = _InstrumentGateView(_RUN_ID, _INSTRUMENT)
    lifecycle = create_phase1_historical_lifecycle(
        binding=binding,
        prepared_acknowledgement=prepared,
        audit=audit,
        runtime=runtime,
        spec_set=spec_set,
        execution_policy=_EXECUTION_POLICY,
        source_namespace=_SOURCE_NAMESPACE,
        provenance_id=FactProvenanceId("phase1.simulator.v1"),
        order_issuance_verifier=orders,
        risk_policy=risk_policy,
        global_halt=_GlobalHaltView(_RUN_ID),
        instrument_gate=gate,
        economic_gate=economic_gate,
    )
    signal_authority = create_strategy_signal_authority(
        run_id=_RUN_ID,
        verifier=create_active_market_dispatch_verifier(runtime),
    )

    first_window = lifecycle.coordinator.begin_next_dispatch()
    lease = runtime.active_lease
    if lease is None or type(lease.root) is not MarketDataEnvelope:
        raise RuntimeError("demo runtime did not expose the first market root")
    signal = signal_authority.issue(
        lease.root,
        dispatch_sequence=lease.dispatch_sequence,
        direction=SignalDirection.LONG,
    )
    planning = planner.plan(signal)
    intent = planning.intent
    if intent is None:
        raise RuntimeError("demo portfolio plan did not emit an intent")
    risk_result = risk.evaluate(intent, economic_gate.ledger.snapshot)
    planning_snapshot_sha256 = planning.outcome.portfolio_snapshot_sha256.value
    risk_snapshot_sha256 = risk_result.evidence.portfolio_snapshot_sha256.value
    pre_fill_snapshot = economic_gate.ledger.snapshot
    pre_fill_snapshot_sha256 = portfolio_snapshot_digest(pre_fill_snapshot)
    order = None
    if risk_result.decision.kind in {RiskDecisionKind.ALLOW, RiskDecisionKind.RESIZE}:
        order = orders.create_order(intent, risk_result)
        gate.hold_for(order.order_id)
        lifecycle.coordinator.prepare_submission_authorization(
            first_window,
            order,
            causal_market_root=lease.root,
            dispatch_sequence=lease.dispatch_sequence,
        )
        lifecycle.coordinator.submit_authorized_order(first_window, order)
    lifecycle.coordinator.complete_active_dispatch(first_window)

    while lifecycle.coordinator.terminal_outcome is None:
        active = lifecycle.coordinator.begin_next_dispatch()
        lifecycle.coordinator.complete_active_dispatch(active)

    fills = lifecycle.fact_authority.fills
    replay_ledger = create_portfolio_ledger(_RUN_ID, spec_set)
    for replay_fill in fills:
        outcome = replay_ledger.apply_fill(replay_fill)
        if outcome.code is not OutcomeCode.LEDGER_APPLIED:
            raise RuntimeError("demo Fill replay did not apply to the public ledger")
    snapshot = economic_gate.ledger.snapshot
    replayed_snapshot = replay_ledger.snapshot
    replayed_snapshot_sha256 = portfolio_snapshot_digest(replayed_snapshot)
    completion_documents = [
        json.loads(record.canonical_payload)
        for record in audit.records
        if record.record_kind is AuditRecordKind.RUNTIME_DISPATCH_COMPLETED
    ]
    if not completion_documents:
        raise RuntimeError("demo lifecycle produced no completed dispatch evidence")
    internal_snapshot_sha256 = completion_documents[-1]["final_portfolio_snapshot_sha256"]
    if internal_snapshot_sha256 != replayed_snapshot_sha256.value:
        raise RuntimeError("audited internal ledger and public Fill replay diverged")

    reconciliation = create_phase1_reconciliation_authority(
        run_id=_RUN_ID,
        spec_set=spec_set,
        snapshot_view=lambda: snapshot,
    )
    dispatch_sequence = lifecycle.coordinator.state.last_completed_dispatch_sequence
    if dispatch_sequence is None:
        raise RuntimeError("demo lifecycle lost its dispatch frontier")

    if order is None:
        position_outcome_code = "not_required_empty"
        cash_outcome_code = "not_required_empty"
        reconciliation_snapshot_sha256 = None
    else:
        position_outcome = reconciliation.admit_observation(
            _observation(
                spec_set=spec_set,
                snapshot=snapshot,
                position=True,
                mismatch=mode is DemoMode.RECONCILIATION_MISMATCH,
            ),
            dispatch_sequence=dispatch_sequence,
        )
        _append_reconciliation(audit, position_outcome)
        if position_outcome.outcome_code is not OutcomeCode.RECONCILIATION_MATCH:
            raise OfflineDemoFailure(
                OutcomeCode.RECONCILIATION_MISMATCH,
                "offline demo reconciliation did not match",
                Path(),
                audit_bytes=_audit_bytes(audit.records),
            )
        cash_outcome = reconciliation.admit_observation(
            _observation(
                spec_set=spec_set,
                snapshot=snapshot,
                position=False,
                mismatch=False,
            ),
            dispatch_sequence=dispatch_sequence,
        )
        _append_reconciliation(audit, cash_outcome)
        if cash_outcome.outcome_code is not OutcomeCode.RECONCILIATION_MATCH:
            raise OfflineDemoFailure(
                OutcomeCode.RECONCILIATION_MISMATCH,
                "offline demo reconciliation did not match",
                Path(),
                audit_bytes=_audit_bytes(audit.records),
            )
        position_outcome_code = position_outcome.outcome_code.value.replace(".", "_")
        cash_outcome_code = cash_outcome.outcome_code.value.replace(".", "_")
        reconciliation_snapshot_sha256 = position_outcome.local_snapshot_sha256.value

    fill: Fill | None = fills[0] if fills else None
    run_outcome = "risk_rejected" if order is None else "filled"
    portfolio = {
        "authoritative_snapshot_sha256": internal_snapshot_sha256,
        "authoritative_snapshot_ledger_sequence": snapshot.ledger_sequence,
        "pre_fill_ledger_sequence": pre_fill_snapshot.ledger_sequence,
        "replay_verification": {
            "status": "matched",
            "snapshot_sha256": replayed_snapshot_sha256.value,
        },
    }
    semantic: dict[str, object] = {
        "audit_chain_head_sha256": audit_chain_head(audit.records[-1]).value,
        "cash_snapshot": [
            {"amount": balance.amount.text, "currency": balance.currency.code}
            for balance in snapshot.cash_balances
        ],
        "authoritative_final_snapshot_sha256": internal_snapshot_sha256,
        "portfolio": portfolio,
        "fill_detail": (
            None
            if fill is None
            else {
                "fill_id": _id_document(fill.fill_id),
                "price": fill.price.text,
                "quantity": fill.quantity.text,
            }
        ),
        "internal_snapshot_sha256": internal_snapshot_sha256,
        "market_event_count": len(dataset.selection.events),
        "order_detail": (
            None
            if order is None
            else {
                "order_id": _id_document(order.order_id),
                "quantity": order.quantity.text,
                "side": order.side.value,
            }
        ),
        "position_snapshot": [
            {
                "quantity": balance.quantity.text,
                "symbol": balance.instrument.symbol,
                "venue": balance.instrument.venue.code,
            }
            for balance in snapshot.position_balances
        ],
        "planning_snapshot_sha256": planning_snapshot_sha256,
        "pre_fill_ledger_snapshot_sha256": pre_fill_snapshot_sha256.value,
        "reconciliation": {
            "cash": cash_outcome_code,
            "position": position_outcome_code,
        },
        "reconciliation_snapshot_sha256": reconciliation_snapshot_sha256,
        "report_snapshot_sha256": internal_snapshot_sha256,
        "risk_snapshot_sha256": risk_snapshot_sha256,
        "replayed_snapshot_sha256": replayed_snapshot_sha256.value,
        "risk": {
            "decision": risk_result.decision.kind.value,
            "reason": risk_result.evidence.reason_code.value,
        },
        "run_id": _RUN_ID.value,
        "run_outcome": run_outcome,
        "run_status": "completed",
        "trade_outcome": run_outcome,
        "schema": "ea.offline-demo-result.v1",
        "signal": {
            "direction": signal.direction.value,
            "signal_id": _id_document(signal.signal_id),
        },
        "status": "success",
    }
    digest = sha256(b"ea.offline-demo.semantic.v1\0" + _canonical_json(semantic)).hexdigest()
    report = {**semantic, "semantic_outcome_sha256": digest}
    return report, _audit_bytes(audit.records)


def run_offline_demo(
    output_root: Path,
    *,
    mode: DemoMode = DemoMode.ACCEPT,
) -> OfflineDemoResult:
    """Run the fixed offline slice once and preserve success or failure evidence."""
    if type(mode) is not DemoMode:
        raise OfflineDemoInputError("demo mode must be exact")
    attempt = _safe_attempt_directory(output_root)
    try:
        report, audit = _execute(mode)
    except OfflineDemoFailure as error:
        code = error.code
        message = str(error)
        if error.audit_bytes:
            _write_once(attempt / "audit.jsonl", error.audit_bytes)
        trade_outcome = (
            "reconciliation_failed" if code is OutcomeCode.RECONCILIATION_MISMATCH else None
        )
        _write_once(
            attempt / "failure.json",
            _failure_document(
                code,
                message,
                trade_outcome=trade_outcome,
            ),
        )
        raise OfflineDemoFailure(code, message, attempt) from None
    _write_once(attempt / "audit.jsonl", audit)
    _write_once(attempt / "result.json", _canonical_json(report) + b"\n")
    summary = (
        f"EA offline demo: {report['run_outcome']}\n"
        f"run_id: {report['run_id']}\n"
        f"semantic_outcome_sha256: {report['semantic_outcome_sha256']}\n"
        "live capability: unavailable\n"
    ).encode()
    _write_once(attempt / "summary.txt", summary)
    return OfflineDemoResult(status="success", output_directory=attempt)
