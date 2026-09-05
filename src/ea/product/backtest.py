"""Scenario-driven funded deterministic single-run composition."""

from __future__ import annotations

import json
import os
import sys
import sysconfig
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import ea
from ea.composition.lifecycle import (
    create_phase1_historical_economic_gate,
    create_phase1_historical_lifecycle,
)
from ea.core import (
    AuditRecordKind,
    AuditSubjectKind,
    CanonicalDecimal,
    CashReconciliationBalance,
    EconomicId,
    EconomicOwnerKind,
    FactProvenanceId,
    Fill,
    HistoricalDispatchKind,
    InitialFunding,
    InstrumentRiskLimit,
    MarketDataEnvelope,
    OutcomeCode,
    Phase1PortfolioPolicyEntry,
    PortfolioPolicyId,
    PositionReconciliationBalance,
    ReconciliationObservationKind,
    ReconciliationScopeKind,
    RiskDecisionKind,
    RiskPolicyId,
    RunBinding,
    RunId,
    RunReference,
    RuntimeIdentifier,
    Sha256Digest,
    SignalDirection,
    SourceNamespace,
    audit_chain_head,
    audit_subject_digest,
    canonical_funding_apply_outcome_bytes,
    canonical_funding_transaction_bytes,
    canonical_initial_funding_bytes,
    canonical_reconciliation_outcome_bytes,
    canonical_run_prepared_audit_payload,
    create_phase1_portfolio_policy,
    create_phase1_risk_policy,
    create_reconciliation_observation,
    funding_apply_outcome_digest,
    funding_transaction_digest,
    instrument_spec_set_digest,
    phase1_risk_policy_digest,
    portfolio_snapshot_digest,
)
from ea.data import (
    create_phase1_historical_market_data_source,
    create_phase1_historical_market_source_bridge,
)
from ea.execution import create_phase1_order_authority
from ea.portfolio import create_portfolio_ledger, create_portfolio_planning_authority
from ea.product.identity import (
    BacktestLineageInputs,
    build_backtest_lineage,
    semantic_outcome_sha256,
)
from ea.product.offline_demo import (
    _audit_bytes,
    _DemoAudit,
    _GlobalHaltView,
    _InstrumentGateView,
    _package_code_digest,
)
from ea.product.scenario import BacktestStrategyId, LoadedBacktestScenario
from ea.reconciliation import create_phase1_reconciliation_authority
from ea.runtime import (
    create_active_market_dispatch_verifier,
    create_phase1_historical_market_runtime,
)
from ea.strategy import create_strategy_signal_authority

_SOURCE_NAMESPACE = SourceNamespace("backtest.scenario.matcher.v1")
_RECONCILIATION_SOURCE = SourceNamespace("backtest.scenario.reconciliation.v1")
_LEDGER_WATERMARK = SourceNamespace("ledger.portfolio")
_RISK_POLICY_ID = RiskPolicyId("backtest.scenario.v1")
_RESULT_SCHEMA = "ea.backtest-single-run-result.v1"
_FAILURE_SCHEMA = "ea.backtest-single-run-failure.v1"


class BacktestRunError(ValueError):
    """Invalid output boundary before an attempt starts."""


class BacktestRunFailure(RuntimeError):
    """Fail-closed attempted run with retained evidence."""

    code: OutcomeCode
    output_directory: Path

    def __init__(self, code: OutcomeCode, message: str, output_directory: Path) -> None:
        self.code = code
        self.output_directory = output_directory
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class BacktestRunResult:
    status: str
    output_directory: Path


@dataclass(frozen=True, slots=True)
class _AttemptFailure(Exception):
    code: OutcomeCode
    message: str
    audit_bytes: bytes
    funding_document: dict[str, object]
    details: dict[str, object]


def _canonical_json(document: object) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _component_digest(domain: bytes, document: object) -> Sha256Digest:
    return Sha256Digest(sha256(domain + _canonical_json(document)).hexdigest())


def _binding(run_id: RunId, lineage: Sha256Digest) -> RunBinding:
    attempt = _canonical_json(
        {
            "lineage_sha256": lineage.value,
            "run_id": run_id.value,
            "schema": "ea.backtest-single-run-attempt.v1",
        }
    )
    manifest = Sha256Digest(sha256(b"ea.backtest-single-run.manifest.v1\0" + attempt).hexdigest())
    return RunBinding(RunReference(run_id, lineage), manifest)


def _scaled_text(coefficient: int, scale: int) -> str:
    if coefficient == 0:
        return "0"
    sign = "-" if coefficient < 0 else ""
    digits = str(abs(coefficient))
    if scale:
        digits = digits.rjust(scale + 1, "0")
        text = f"{digits[:-scale]}.{digits[-scale:]}".rstrip("0").rstrip(".")
    else:
        text = digits
    return sign + text


def _upper_price_bound(scenario: LoadedBacktestScenario) -> CanonicalDecimal:
    specification = scenario.spec_set.require(scenario.instrument)
    quantum = specification.price_quantum
    maximum_ticks = 0
    for event in scenario.dataset.selection.events:
        value = event.payload.high
        numerator, denominator = value.as_integer_ratio()
        scaled_numerator = numerator * (10**quantum.scale)
        scaled_denominator = denominator * quantum.coefficient
        ticks, remainder = divmod(scaled_numerator, scaled_denominator)
        if remainder:
            ticks += 1
        maximum_ticks = max(maximum_ticks, ticks)
    return CanonicalDecimal(_scaled_text(maximum_ticks * quantum.coefficient, quantum.scale))


def _quantity_capacity(
    limit: CanonicalDecimal,
    *,
    price: CanonicalDecimal,
    multiplier: CanonicalDecimal,
    quantity_quantum: CanonicalDecimal,
) -> CanonicalDecimal:
    numerator = limit.coefficient * 10 ** (price.scale + multiplier.scale + quantity_quantum.scale)
    denominator = (
        10**limit.scale * price.coefficient * multiplier.coefficient * quantity_quantum.coefficient
    )
    lots = numerator // denominator
    return CanonicalDecimal(
        _scaled_text(lots * quantity_quantum.coefficient, quantity_quantum.scale)
    )


def _risk_context(scenario: LoadedBacktestScenario) -> tuple[Any, dict[str, object]]:
    specification = scenario.spec_set.require(scenario.instrument)
    price_bound = _upper_price_bound(scenario)
    cash_capacity = _quantity_capacity(
        scenario.initial_cash,
        price=price_bound,
        multiplier=specification.contract_multiplier,
        quantity_quantum=specification.quantity_quantum,
    )
    notional_capacity = _quantity_capacity(
        scenario.max_notional,
        price=price_bound,
        multiplier=specification.contract_multiplier,
        quantity_quantum=specification.quantity_quantum,
    )
    effective = min(scenario.max_order_quantity, cash_capacity, notional_capacity)
    limits = (
        ()
        if effective.coefficient == 0
        else (
            InstrumentRiskLimit(
                instrument=scenario.instrument,
                maximum_order_quantity=effective,
                maximum_absolute_position=scenario.max_position_quantity,
            ),
        )
    )
    policy = create_phase1_risk_policy(
        policy_id=_RISK_POLICY_ID,
        spec_set=scenario.spec_set,
        execution_policy=scenario.execution_policy,
        instrument_limits=limits,
    )
    bindings: list[str] = []
    if effective == scenario.max_order_quantity:
        bindings.append("max_order_quantity")
    if effective == cash_capacity:
        bindings.append("available_cash")
    if effective == notional_capacity:
        bindings.append("max_notional")
    return policy, {
        "available_cash_capacity": cash_capacity.text,
        "binding_constraints": bindings,
        "declared_max_notional": scenario.max_notional.text,
        "declared_max_order_quantity": scenario.max_order_quantity.text,
        "declared_max_position_quantity": scenario.max_position_quantity.text,
        "effective_max_order_quantity": effective.text,
        "maximum_execution_price_bound": price_bound.text,
        "notional_capacity": notional_capacity.text,
    }


def _lineage(
    scenario: LoadedBacktestScenario,
    *,
    risk_policy: Any,
    risk_context: dict[str, object],
) -> Sha256Digest:
    version = ".".join(str(part) for part in sys.version_info[:3])
    cache_tag = sys.implementation.cache_tag
    if cache_tag is None:
        raise RuntimeError("runtime has no Python cache tag")
    strategy_parameters = {
        "target_quantity": (
            None if scenario.target_quantity is None else scenario.target_quantity.text
        )
    }
    return build_backtest_lineage(
        BacktestLineageInputs(
            data=scenario.dataset.selection.fingerprint,
            replay_window=scenario.replay_window,
            scenario_sha256=scenario.scenario_sha256,
            instrument_spec_set_sha256=instrument_spec_set_digest(scenario.spec_set),
            strategy_id=scenario.strategy_id.value,
            strategy_parameters_sha256=_component_digest(
                b"ea.backtest-strategy-parameters.v1\0", strategy_parameters
            ),
            risk_policy_id=_RISK_POLICY_ID.value,
            risk_limits_sha256=_component_digest(
                b"ea.backtest-risk-limits.v1\0",
                {
                    "policy_sha256": phase1_risk_policy_digest(risk_policy).value,
                    "scenario_limits": risk_context,
                },
            ),
            execution_policy_id=scenario.execution_policy.identifier.value,
            execution_policy_sha256=scenario.execution_policy.sha256,
            code_sha256=_package_code_digest(),
            distribution_name="ea-quant",
            distribution_version=ea.__version__,
            python_implementation=sys.implementation.name,
            python_version=version,
            python_cache_tag=cache_tag,
            sys_platform=sys.platform,
            platform_tag=sysconfig.get_platform(),
            randomness=scenario.randomness,
        )
    )


def _funding_document(funding: InitialFunding, outcome: Any) -> dict[str, object]:
    transaction = outcome.transaction
    if transaction is None:
        raise RuntimeError("applied funding has no transaction")
    return {
        "amount": transaction.amount.text,
        "currency": transaction.currency.code,
        "funding_apply_outcome_sha256": funding_apply_outcome_digest(outcome).value,
        "funding_sha256": transaction.funding_sha256.value,
        "funding_transaction_sha256": funding_transaction_digest(transaction).value,
        "initial_funding": json.loads(canonical_initial_funding_bytes(funding)),
        "ledger_sequence": transaction.ledger_sequence,
        "status": "applied",
        "transaction": json.loads(canonical_funding_transaction_bytes(transaction)),
        "apply_outcome": json.loads(canonical_funding_apply_outcome_bytes(outcome)),
    }


def _observation(
    *,
    scenario: LoadedBacktestScenario,
    run_id: RunId,
    snapshot: Any,
    position: bool,
) -> Any:
    if position:
        balances: tuple[PositionReconciliationBalance | CashReconciliationBalance, ...] = tuple(
            PositionReconciliationBalance(balance.instrument, balance.quantity)
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
    observed_at = scenario.dataset.selection.events[-1].available_at
    return create_reconciliation_observation(
        run_id=run_id,
        spec_set=scenario.spec_set,
        observation_id=EconomicId(
            run_id,
            EconomicOwnerKind.RECONCILIATION_OBSERVATION,
            sequence,
        ),
        kind=kind,
        source_namespace=_RECONCILIATION_SOURCE,
        source_sequence=sequence,
        occurred_at=observed_at,
        available_at=observed_at,
        watermark_namespace=_LEDGER_WATERMARK,
        watermark_sequence=snapshot.ledger_sequence,
        declared_scope_kind=scope,
        declared_scope_id=RuntimeIdentifier("portfolio.default"),
        provenance_id=FactProvenanceId("backtest.scenario.reconciliation.v1"),
        provenance_payload_sha256=portfolio_snapshot_digest(snapshot),
        balances=balances,
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


def _id_document(identity: EconomicId) -> dict[str, object]:
    return {
        "owner_kind": identity.owner_kind.value,
        "owner_sequence": identity.owner_sequence,
        "run_id": identity.run_id.value,
    }


def _execute(
    scenario: LoadedBacktestScenario,
    *,
    run_id: RunId,
    binding: RunBinding,
    lineage: Sha256Digest,
    risk_policy: Any,
    risk_context: dict[str, object],
) -> tuple[dict[str, object], bytes, dict[str, object]]:
    audit = _DemoAudit(binding, scenario.spec_set)
    prepared = audit.append(
        record_kind=AuditRecordKind.RUN_PREPARED,
        subject_kind=AuditSubjectKind.RUN_MANIFEST,
        subject_sha256=binding.manifest_sha256,
        canonical_payload=canonical_run_prepared_audit_payload(binding),
    )
    funding = InitialFunding(run_id, scenario.funding_currency, scenario.initial_cash)
    source = create_phase1_historical_market_data_source(scenario.dataset)
    runtime = create_phase1_historical_market_runtime(
        run_id=run_id,
        spec_set=scenario.spec_set,
        source=create_phase1_historical_market_source_bridge(source),
    )
    economic_gate = create_phase1_historical_economic_gate(
        run_id=run_id,
        spec_set=scenario.spec_set,
        execution_policy=scenario.execution_policy,
        risk_policy=risk_policy,
        initial_funding=funding,
    )
    funding_outcome = economic_gate.funding_outcome
    if funding_outcome is None:
        raise RuntimeError("economic gate omitted initial funding")
    funding_document = _funding_document(funding, funding_outcome)
    orders = create_phase1_order_authority(
        run_id=run_id,
        spec_set=scenario.spec_set,
        execution_policy=scenario.execution_policy,
        risk_policy=risk_policy,
        risk_result_verifier=economic_gate.risk_authority,
    )
    instrument_gate = _InstrumentGateView(run_id, scenario.instrument)
    lifecycle = create_phase1_historical_lifecycle(
        binding=binding,
        prepared_acknowledgement=prepared,
        audit=audit,
        runtime=runtime,
        spec_set=scenario.spec_set,
        execution_policy=scenario.execution_policy,
        source_namespace=_SOURCE_NAMESPACE,
        provenance_id=FactProvenanceId("backtest.scenario.simulator.v1"),
        order_issuance_verifier=orders,
        risk_policy=risk_policy,
        global_halt=_GlobalHaltView(run_id),
        instrument_gate=instrument_gate,
        economic_gate=economic_gate,
    )

    first_window = lifecycle.coordinator.begin_next_dispatch()
    signal = None
    order = None
    risk_result = None
    if scenario.strategy_id is BacktestStrategyId.BOUNDED_LONG:
        lease = runtime.active_lease
        if lease is None or type(lease.root) is not MarketDataEnvelope:
            raise RuntimeError("backtest runtime did not expose the first market root")
        signal_authority = create_strategy_signal_authority(
            run_id=run_id,
            verifier=create_active_market_dispatch_verifier(runtime),
        )
        signal = signal_authority.issue(
            lease.root,
            dispatch_sequence=lease.dispatch_sequence,
            direction=SignalDirection.LONG,
        )
        if scenario.target_quantity is None:
            raise RuntimeError("bounded-long scenario lost target quantity")
        portfolio_policy = create_phase1_portfolio_policy(
            policy_id=PortfolioPolicyId("backtest.scenario.v1"),
            entries=(
                Phase1PortfolioPolicyEntry(
                    instrument=scenario.instrument,
                    target_quantity=scenario.target_quantity,
                ),
            ),
            spec_set=scenario.spec_set,
        )
        planner = create_portfolio_planning_authority(
            run_id=run_id,
            ledger=economic_gate.ledger,
            spec_set=scenario.spec_set,
            policy=portfolio_policy,
            execution_policy=scenario.execution_policy,
        )
        planning = planner.plan(signal)
        intent = planning.intent
        if intent is None:
            raise RuntimeError("bounded-long scenario did not emit an intent")
        risk_result = economic_gate.risk_authority.evaluate(intent, economic_gate.ledger.snapshot)
        if risk_result.decision.kind is RiskDecisionKind.REJECT:
            lifecycle.coordinator.complete_active_dispatch(first_window)
            raise _AttemptFailure(
                code=OutcomeCode.RISK_REJECTED,
                message="scenario order was rejected by risk",
                audit_bytes=_audit_bytes(audit.records),
                funding_document=funding_document,
                details={
                    "fill": None,
                    "order": None,
                    "risk": {
                        **risk_context,
                        "decision": "reject",
                        "reason": risk_result.evidence.reason_code.value,
                    },
                },
            )
        if risk_result.decision.kind not in {RiskDecisionKind.ALLOW, RiskDecisionKind.RESIZE}:
            raise RuntimeError("scenario risk evaluation failed")
        order = orders.create_order(intent, risk_result)
        instrument_gate.hold_for(order.order_id)
        lifecycle.coordinator.prepare_submission_authorization(
            first_window,
            order,
            causal_market_root=lease.root,
            dispatch_sequence=lease.dispatch_sequence,
        )
        lifecycle.coordinator.submit_authorized_order(first_window, order)
    lifecycle.coordinator.complete_active_dispatch(first_window)

    end_of_run_window = None
    while lifecycle.coordinator.terminal_outcome is None:
        active = lifecycle.coordinator.begin_next_dispatch()
        if active.dispatch_kind is HistoricalDispatchKind.END_OF_RUN:
            end_of_run_window = active
            break
        lifecycle.coordinator.complete_active_dispatch(active)

    fills = lifecycle.fact_authority.fills
    snapshot = economic_gate.ledger.snapshot
    replay_ledger = create_portfolio_ledger(run_id, scenario.spec_set)
    replay_funding = replay_ledger.apply_initial_funding(funding)
    if replay_funding.code is not OutcomeCode.LEDGER_APPLIED:
        raise RuntimeError("funding replay failed")
    for replay_fill in fills:
        outcome = replay_ledger.apply_fill(replay_fill)
        if outcome.code is not OutcomeCode.LEDGER_APPLIED:
            raise RuntimeError("Fill replay failed")
    if portfolio_snapshot_digest(replay_ledger.snapshot) != portfolio_snapshot_digest(snapshot):
        raise RuntimeError("authoritative ledger and public replay diverged")

    reconciliation = create_phase1_reconciliation_authority(
        run_id=run_id,
        spec_set=scenario.spec_set,
        snapshot_view=lambda: snapshot,
    )
    dispatch_sequence = lifecycle.coordinator.state.last_completed_dispatch_sequence
    if dispatch_sequence is None:
        raise RuntimeError("backtest lifecycle lost its dispatch frontier")
    if snapshot.position_balances:
        position_outcome = reconciliation.admit_observation(
            _observation(scenario=scenario, run_id=run_id, snapshot=snapshot, position=True),
            dispatch_sequence=dispatch_sequence,
        )
        _append_reconciliation(audit, position_outcome)
        if position_outcome.outcome_code is not OutcomeCode.RECONCILIATION_MATCH:
            raise _AttemptFailure(
                OutcomeCode.RECONCILIATION_MISMATCH,
                "scenario position reconciliation did not match",
                _audit_bytes(audit.records),
                funding_document,
                {"fill": None, "order": None},
            )
        position_status = "match"
    else:
        position_status = "not_required_empty"
    cash_outcome = reconciliation.admit_observation(
        _observation(scenario=scenario, run_id=run_id, snapshot=snapshot, position=False),
        dispatch_sequence=dispatch_sequence,
    )
    _append_reconciliation(audit, cash_outcome)
    if cash_outcome.outcome_code is not OutcomeCode.RECONCILIATION_MATCH:
        raise _AttemptFailure(
            OutcomeCode.RECONCILIATION_MISMATCH,
            "scenario cash reconciliation did not match",
            _audit_bytes(audit.records),
            funding_document,
            {"fill": None, "order": None},
        )
    if end_of_run_window is not None:
        lifecycle.coordinator.complete_active_dispatch(end_of_run_window)

    fill: Fill | None = fills[0] if fills else None
    strategy_document = {
        "id": scenario.strategy_id.value,
        "signal": "flat" if signal is None else signal.direction.value,
    }
    risk_document: dict[str, object] = {
        **risk_context,
        "decision": "not_applicable" if risk_result is None else risk_result.decision.kind.value,
        "reason": None if risk_result is None else risk_result.evidence.reason_code.value,
    }
    order_document = (
        None
        if order is None
        else {
            "order_id": _id_document(order.order_id),
            "quantity": order.quantity.text,
            "side": order.side.value,
        }
    )
    fill_document = (
        None
        if fill is None
        else {
            "fill_id": _id_document(fill.fill_id),
            "price": fill.price.text,
            "quantity": fill.quantity.text,
            "side": fill.side.value,
        }
    )
    ending_cash = [
        {"amount": balance.amount.text, "currency": balance.currency.code}
        for balance in snapshot.cash_balances
    ]
    ending_positions = [
        {
            "quantity": balance.quantity.text,
            "symbol": balance.instrument.symbol,
            "venue": balance.instrument.venue.code,
        }
        for balance in snapshot.position_balances
    ]
    reconciliation_document = {"cash": "match", "position": position_status}
    semantic = {
        "ending_cash": ending_cash,
        "ending_positions": ending_positions,
        "fill": (
            None
            if fill is None
            else {
                "price": fill.price.text,
                "quantity": fill.quantity.text,
                "side": fill.side.value,
            }
        ),
        "initial_funding": {
            "amount": scenario.initial_cash.text,
            "currency": scenario.funding_currency.code,
        },
        "lineage_sha256": lineage.value,
        "order": (
            None if order is None else {"quantity": order.quantity.text, "side": order.side.value}
        ),
        "reconciliation": reconciliation_document,
        "risk": risk_document,
        "schema": "ea.backtest-semantic-outcome.v1",
        "strategy": strategy_document,
        "terminal_state": "completed",
    }
    report: dict[str, object] = {
        "audit_chain_head_sha256": audit_chain_head(audit.records[-1]).value,
        "ending_cash": ending_cash,
        "ending_positions": ending_positions,
        "fill": fill_document,
        "initial_funding": {
            "amount": scenario.initial_cash.text,
            "currency": scenario.funding_currency.code,
            "ledger_sequence": 1,
            "status": "applied",
        },
        "ledger_sequence": snapshot.ledger_sequence,
        "lineage_sha256": lineage.value,
        "order": order_document,
        "randomness": scenario.randomness.document(),
        "reconciliation": reconciliation_document,
        "risk": risk_document,
        "run_id": run_id.value,
        "scenario_sha256": scenario.scenario_sha256.value,
        "schema": _RESULT_SCHEMA,
        "semantic_outcome_sha256": semantic_outcome_sha256(semantic).value,
        "status": "success",
        "strategy": strategy_document,
        "terminal_state": "completed",
    }
    return report, _audit_bytes(audit.records), funding_document


def _safe_attempt_directory(output_root: Path, run_id: RunId) -> Path:
    if not isinstance(output_root, Path) or not output_root.is_absolute():
        raise BacktestRunError("output root must be an absolute path")
    if output_root.exists() and output_root.is_symlink():
        raise BacktestRunError("output root cannot be a symlink")
    try:
        output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        raise BacktestRunError("output root could not be created") from None
    if not output_root.is_dir():
        raise BacktestRunError("output root must be a directory")
    attempt = output_root / run_id.value
    try:
        attempt.mkdir(mode=0o700)
    except OSError:
        raise BacktestRunError("fresh attempt directory could not be created") from None
    return attempt


def _write_once(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise BacktestRunFailure(
            OutcomeCode.DURABILITY_RESULT_WRITE_FAILED,
            "backtest evidence could not be written",
            path.parent,
        ) from None


def run_backtest_scenario(
    scenario: LoadedBacktestScenario,
    output_root: Path,
) -> BacktestRunResult:
    """Run one validated scenario through the existing offline authorities."""
    if type(scenario) is not LoadedBacktestScenario:
        raise BacktestRunError("scenario must be an exact loaded BacktestScenario")
    run_id = RunId(str(uuid4()))
    attempt = _safe_attempt_directory(output_root, run_id)
    risk_policy, risk_context = _risk_context(scenario)
    lineage = _lineage(scenario, risk_policy=risk_policy, risk_context=risk_context)
    binding = _binding(run_id, lineage)
    try:
        report, audit, funding = _execute(
            scenario,
            run_id=run_id,
            binding=binding,
            lineage=lineage,
            risk_policy=risk_policy,
            risk_context=risk_context,
        )
    except _AttemptFailure as failure:
        if failure.audit_bytes:
            _write_once(attempt / "audit.jsonl", failure.audit_bytes)
        _write_once(attempt / "funding.json", _canonical_json(failure.funding_document) + b"\n")
        semantic = {
            "failure": {"code": failure.code.value},
            "lineage_sha256": lineage.value,
            "schema": "ea.backtest-semantic-outcome.v1",
            "terminal_state": "failed",
        }
        document = {
            "code": failure.code.value,
            **failure.details,
            "lineage_sha256": lineage.value,
            "message": failure.message,
            "run_id": run_id.value,
            "scenario_sha256": scenario.scenario_sha256.value,
            "schema": _FAILURE_SCHEMA,
            "semantic_outcome_sha256": semantic_outcome_sha256(semantic).value,
            "status": "failed",
            "terminal_state": "failed",
        }
        _write_once(attempt / "failure.json", _canonical_json(document) + b"\n")
        raise BacktestRunFailure(failure.code, failure.message, attempt) from None
    _write_once(attempt / "audit.jsonl", audit)
    _write_once(attempt / "funding.json", _canonical_json(funding) + b"\n")
    _write_once(attempt / "result.json", _canonical_json(report) + b"\n")
    return BacktestRunResult("success", attempt)


__all__ = [
    "BacktestRunError",
    "BacktestRunFailure",
    "BacktestRunResult",
    "run_backtest_scenario",
]
