"""Scenario-driven funded deterministic single-run composition."""

from __future__ import annotations

import json
import os
import sys
import sysconfig
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

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
    audit_record_digest,
    audit_subject_digest,
    canonical_fill_bytes,
    canonical_funding_apply_outcome_bytes,
    canonical_funding_transaction_bytes,
    canonical_initial_funding_bytes,
    canonical_portfolio_snapshot_bytes,
    canonical_reconciliation_outcome_bytes,
    canonical_run_prepared_audit_payload,
    create_phase1_portfolio_policy,
    create_phase1_risk_policy,
    create_reconciliation_observation,
    funding_apply_outcome_digest,
    funding_transaction_digest,
    initial_funding_digest,
    instrument_spec_set_digest,
    phase1_risk_policy_digest,
    portfolio_snapshot_digest,
)
from ea.data import (
    create_phase1_historical_market_data_source,
    create_phase1_historical_market_source_bridge,
)
from ea.execution import create_phase1_order_authority
from ea.experiments.audit import create_posix_audit_journal, reopen_posix_audit_journal
from ea.experiments.store import (
    CanonicalAttemptManifest,
    LocalResultStore,
    StoreCollisionError,
    VerifiedIncompleteRecoveryBinding,
    VerifiedTerminalRecoveryBinding,
)
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
from ea.product.scenario import (
    BacktestScenarioError,
    LoadedBacktestScenario,
    _next_bar_entry_delay_maximum,
    load_backtest_scenario,
)
from ea.reconciliation import create_phase1_reconciliation_authority
from ea.runtime import (
    create_active_market_dispatch_verifier,
    create_phase1_historical_market_runtime,
)
from ea.strategy import create_strategy_signal_authority
from ea.strategy.catalog import ResearchStrategyCatalogV1
from ea.strategy.package import read_regular, validate_package
from ea.strategy.registry import StrategyEntryV1

_SOURCE_NAMESPACE = SourceNamespace("backtest.scenario.matcher.v1")
_RECONCILIATION_SOURCE = SourceNamespace("backtest.scenario.reconciliation.v1")
_LEDGER_WATERMARK = SourceNamespace("ledger.portfolio")
_RISK_POLICY_ID = RiskPolicyId("backtest.scenario.v1")
_RESULT_SCHEMA = "ea.backtest-single-run-result.v2"
_FAILURE_SCHEMA = "ea.backtest-single-run-failure.v1"
_ATTEMPT_SCHEMA = "ea.backtest-resumable-attempt.v2"
_ATTEMPT_CANONICALIZATION = "ea-backtest-resumable-attempt-v2"
_PUBLICATION_SCHEMA = "ea.backtest-success-publication.v1"
_TEST_INTERRUPT: Callable[[str], None] | None = None


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


class BacktestResumeFailure(RuntimeError):
    """A trusted existing attempt cannot be safely resumed."""

    output_directory: Path

    def __init__(self, message: str, output_directory: Path) -> None:
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


def _binding(run_id: RunId, lineage: Sha256Digest, manifest: bytes) -> RunBinding:
    return RunBinding(
        RunReference(run_id, lineage),
        Sha256Digest(sha256(manifest).hexdigest()),
    )


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


def _runtime_document() -> dict[str, str]:
    cache_tag = sys.implementation.cache_tag
    if cache_tag is None:
        raise RuntimeError("runtime has no Python cache tag")
    return {
        "platform_tag": sysconfig.get_platform(),
        "python_cache_tag": cache_tag,
        "python_implementation": sys.implementation.name,
        "python_version": ".".join(str(part) for part in sys.version_info[:3]),
        "sys_platform": sys.platform,
    }


def _lineage(
    scenario: LoadedBacktestScenario,
    *,
    risk_policy: Any,
    risk_context: dict[str, object],
) -> Sha256Digest:
    runtime = _runtime_document()
    strategy_document = json.loads(scenario.canonical_bytes)["strategy"]
    strategy_parameters = (
        {key: value for key, value in strategy_document.items() if key != "id"}
        if scenario.schema_version == 1
        else strategy_document
    )
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
            python_implementation=runtime["python_implementation"],
            python_version=runtime["python_version"],
            python_cache_tag=runtime["python_cache_tag"],
            sys_platform=runtime["sys_platform"],
            platform_tag=runtime["platform_tag"],
            randomness=scenario.randomness,
        )
    )


def _attempt_manifest_bytes(
    scenario: LoadedBacktestScenario,
    *,
    run_id: RunId,
    lineage: Sha256Digest,
    risk_policy: Any,
    risk_context: dict[str, object],
) -> bytes:
    funding = InitialFunding(run_id, scenario.funding_currency, scenario.initial_cash)
    source_file_sha256 = sha256(scenario.data_path.read_bytes()).hexdigest()
    if (
        scenario.research_input_bytes is not None
        and source_file_sha256 != scenario.dataset.source_bytes_sha256.value
    ):
        raise BacktestScenarioError("registered dataset changed before engine acceptance")
    document = {
        "canonicalization": _ATTEMPT_CANONICALIZATION,
        "code_sha256": _package_code_digest().value,
        "data": {
            "fingerprint": {
                "record_count": scenario.dataset.selection.fingerprint.record_count,
                "sha256": scenario.dataset.selection.fingerprint.sha256.value,
            },
            "path": str(scenario.data_path),
            "source_file_sha256": source_file_sha256,
        },
        "distribution": {"name": "ea-quant", "version": ea.__version__},
        "execution": {
            "policy_id": scenario.execution_policy.identifier.value,
            "policy_sha256": scenario.execution_policy.sha256.value,
        },
        "funding": {
            "amount": scenario.initial_cash.text,
            "currency": scenario.funding_currency.code,
            "funding_sha256": initial_funding_digest(funding).value,
        },
        "instrument_spec_set_sha256": instrument_spec_set_digest(scenario.spec_set).value,
        "lineage_sha256": lineage.value,
        "randomness": scenario.randomness.document(),
        "risk": {
            "context": risk_context,
            "policy_id": risk_policy.policy_id.value,
            "policy_sha256": phase1_risk_policy_digest(risk_policy).value,
        },
        "runtime": _runtime_document(),
        "run_id": run_id.value,
        "scenario": {
            "canonical": json.loads(scenario.canonical_bytes),
            "path": str(scenario.scenario_path),
            "sha256": scenario.scenario_sha256.value,
            "source_file_sha256": sha256(scenario.scenario_path.read_bytes()).hexdigest(),
        },
        "schema": _ATTEMPT_SCHEMA,
    }
    return _canonical_json(document)


def _interrupt(stage: str) -> None:
    callback = _TEST_INTERRUPT
    if callback is not None:
        callback(stage)


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
        if scenario.schema_version in (4, 5) and not balances:
            balances = (PositionReconciliationBalance(scenario.instrument, CanonicalDecimal("0")),)
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
    audit: Any | None = None,
    on_funding: Callable[[dict[str, object]], None] | None = None,
    on_frontier: Callable[[str], None] | None = None,
) -> tuple[dict[str, object], bytes, dict[str, object]]:
    if scenario.schema_version in (4, 5):
        from ea.product.round_trip import execute_round_trip

        return execute_round_trip(
            scenario,
            run_id=run_id,
            binding=binding,
            lineage=lineage,
            risk_policy=risk_policy,
            risk_context=risk_context,
            audit=audit,
            on_funding=on_funding,
            on_frontier=on_frontier,
        )
    audit = _DemoAudit(binding, scenario.spec_set) if audit is None else audit
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
    if on_funding is not None:
        on_funding(funding_document)
    if on_frontier is not None:
        on_frontier("funding_durable")
    _interrupt("funding_durable")
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

    entry_window = lifecycle.coordinator.begin_next_dispatch()
    signal = None
    order = None
    risk_result = None
    verifier = create_active_market_dispatch_verifier(runtime)
    legacy_entry = scenario.strategy_entry
    if not isinstance(legacy_entry, StrategyEntryV1):
        raise ValueError("V1 route requires V1 strategy")
    logic = legacy_entry.factory(scenario.strategy_parameters)
    target = None
    market_index = 0
    last_entry = _next_bar_entry_delay_maximum(scenario.dataset)
    while entry_window.dispatch_kind is HistoricalDispatchKind.MARKET:
        lease = runtime.active_lease
        if lease is None or type(lease.root) is not MarketDataEnvelope:
            raise RuntimeError("backtest runtime did not expose active market root")
        verifier.verify_active_market_dispatch(
            lease.root, dispatch_sequence=lease.dispatch_sequence
        )
        if last_entry is not None and market_index <= last_entry:
            target = logic.on_event(lease.root)
        market_index += 1
        if target is not None:
            break
        lifecycle.coordinator.complete_active_dispatch(entry_window)
        entry_window = lifecycle.coordinator.begin_next_dispatch()
    if target is not None:
        from ea.core.economics import require_quantized

        require_quantized(
            target,
            scenario.spec_set.require(scenario.instrument).quantity_quantum,
            field_name="target_quantity",
        )
        lease = runtime.active_lease
        if lease is None or type(lease.root) is not MarketDataEnvelope:
            raise RuntimeError("entry lost its active market root")
        signal_authority = create_strategy_signal_authority(run_id=run_id, verifier=verifier)
        signal = signal_authority.issue(
            lease.root,
            dispatch_sequence=lease.dispatch_sequence,
            direction=SignalDirection.LONG,
        )
        portfolio_policy = create_phase1_portfolio_policy(
            policy_id=PortfolioPolicyId("backtest.scenario.v1"),
            entries=(
                Phase1PortfolioPolicyEntry(
                    instrument=scenario.instrument,
                    target_quantity=target,
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
            raise RuntimeError("strategy entry did not emit an intent")
        risk_result = economic_gate.risk_authority.evaluate(intent, economic_gate.ledger.snapshot)
        if risk_result.decision.kind is RiskDecisionKind.REJECT:
            lifecycle.coordinator.complete_active_dispatch(entry_window)
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
            entry_window,
            order,
            causal_market_root=lease.root,
            dispatch_sequence=lease.dispatch_sequence,
        )
        lifecycle.coordinator.submit_authorized_order(entry_window, order)
    end_of_run_window = None
    if entry_window.dispatch_kind is HistoricalDispatchKind.END_OF_RUN:
        end_of_run_window = entry_window
    else:
        lifecycle.coordinator.complete_active_dispatch(entry_window)

    durable_fill_frontier_observed = False
    while end_of_run_window is None and lifecycle.coordinator.terminal_outcome is None:
        active = lifecycle.coordinator.begin_next_dispatch()
        if active.dispatch_kind is HistoricalDispatchKind.END_OF_RUN:
            end_of_run_window = active
            break
        lifecycle.coordinator.complete_active_dispatch(active)
        if lifecycle.fact_authority.fills and not durable_fill_frontier_observed:
            durable_fill_frontier_observed = True
            if on_frontier is not None:
                on_frontier("dispatch_durable")
            _interrupt("dispatch_durable")

    fills = lifecycle.fact_authority.fills
    legacy_entry = scenario.strategy_entry
    if not isinstance(legacy_entry, StrategyEntryV1):
        raise ValueError("V1 route requires V1 strategy")
    if (
        order is not None
        and not fills
        and end_of_run_window is not None
        and "latency" in json.loads(scenario.canonical_bytes)["execution"]
    ):
        raise _AttemptFailure(
            OutcomeCode.ORDER_EXPIRED_NO_ELIGIBLE_MARKET_DATA,
            "order expired before a market event satisfied execution latency",
            _audit_bytes(audit.records),
            funding_document,
            {
                "fill": None,
                "order": {
                    "order_id": _id_document(order.order_id),
                    "quantity": order.quantity.text,
                    "side": order.side.value,
                },
            },
        )
    legacy_entry.validate_outcome(0 if order is None else 1, len(fills))
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
    if on_frontier is not None:
        on_frontier("reconciliation_durable")
    _interrupt("reconciliation_durable")
    if end_of_run_window is not None:
        lifecycle.coordinator.complete_active_dispatch(end_of_run_window)
        if on_frontier is not None:
            on_frontier("terminal_durable")

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
    fill_evidence = None if fill is None else json.loads(canonical_fill_bytes(fill))
    portfolio_snapshot_evidence = json.loads(canonical_portfolio_snapshot_bytes(snapshot))
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
    if "commission" in json.loads(scenario.canonical_bytes)["execution"]:
        semantic["schema"] = "ea.backtest-semantic-outcome.v2"
        semantic["fees"] = [] if fill_evidence is None else fill_evidence["fees"]
    report: dict[str, object] = {
        "audit_chain_head_sha256": audit_chain_head(audit.records[-1]).value,
        "ending_cash": ending_cash,
        "ending_positions": ending_positions,
        "fill": fill_document,
        "fill_evidence": fill_evidence,
        "initial_funding": {
            "amount": scenario.initial_cash.text,
            "currency": scenario.funding_currency.code,
            "ledger_sequence": 1,
            "status": "applied",
        },
        "ledger_sequence": snapshot.ledger_sequence,
        "lineage_sha256": lineage.value,
        "order": order_document,
        "portfolio_snapshot_evidence": portfolio_snapshot_evidence,
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
    return report, _audit_bytes(list(audit.records)), funding_document


def _safe_output_root(output_root: Path) -> Path:
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
    return output_root.resolve(strict=True)


def _fsync_directory(path: Path) -> None:
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _write_once(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
    except OSError:
        raise BacktestRunFailure(
            OutcomeCode.DURABILITY_RESULT_WRITE_FAILED,
            "backtest evidence could not be written",
            path.parent,
        ) from None


def _write_or_verify(path: Path, payload: bytes) -> None:
    if path.exists():
        try:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise BacktestRunFailure(
                    OutcomeCode.CONFLICTING_ID,
                    "backtest evidence conflicts with the durable attempt",
                    path.parent,
                )
        except OSError:
            raise BacktestRunFailure(
                OutcomeCode.CONFLICTING_ID,
                "backtest evidence could not be verified",
                path.parent,
            ) from None
        return
    _write_once(path, payload)


def _publish_success(attempt: Path, payload: bytes) -> None:
    result = attempt / "result.json"
    publication = attempt / "result.publication.json"
    if result.exists():
        if publication.exists():
            raise BacktestRunFailure(
                OutcomeCode.DURABILITY_RESULT_WRITE_FAILED,
                "backtest success publication is ambiguous",
                attempt,
            )
        _write_or_verify(result, payload)
        return
    pending = attempt / "result.pending"
    _write_or_verify(pending, payload)
    _write_once(publication, _publication_bytes(payload))
    try:
        if result.exists():
            raise OSError("result destination appeared during publication")
        os.rename(pending, result)
        _fsync_directory(attempt)
        os.unlink(publication)
        _fsync_directory(attempt)
    except OSError:
        raise BacktestRunFailure(
            OutcomeCode.DURABILITY_RESULT_WRITE_FAILED,
            "backtest success could not be atomically published",
            attempt,
        ) from None


def _publication_bytes(result_payload: bytes) -> bytes:
    return (
        _canonical_json(
            {
                "result_sha256": sha256(result_payload).hexdigest(),
                "schema": _PUBLICATION_SCHEMA,
            }
        )
        + b"\n"
    )


def _failure_bytes(
    *,
    classification: str,
    run_id: RunId,
    lineage: Sha256Digest,
    scenario: LoadedBacktestScenario,
    last_frontier: str,
    audit: Any,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
) -> bytes:
    records = tuple(audit.records)
    document: dict[str, object] = {
        "audit_chain_head_sha256": (None if not records else audit_chain_head(records[-1]).value),
        "classification": classification,
        "code": code,
        "last_durable_frontier": last_frontier,
        "lineage_sha256": lineage.value,
        "message": message,
        "run_id": run_id.value,
        "scenario_sha256": scenario.scenario_sha256.value,
        "schema": _FAILURE_SCHEMA,
        "status": "failed",
        "terminal_state": "failed",
    }
    if details:
        document.update(details)
    return _canonical_json(document) + b"\n"


def _expected_funding_bytes(
    scenario: LoadedBacktestScenario,
    run_id: RunId,
) -> bytes:
    funding = InitialFunding(run_id, scenario.funding_currency, scenario.initial_cash)
    ledger = create_portfolio_ledger(run_id, scenario.spec_set)
    outcome = ledger.apply_initial_funding(funding)
    if outcome.code is not OutcomeCode.LEDGER_APPLIED:
        raise RuntimeError("persisted funding could not be reconstructed")
    return _canonical_json(_funding_document(funding, outcome)) + b"\n"


def _verified_persisted_frontier(
    *,
    scenario: LoadedBacktestScenario,
    run_id: RunId,
    attempt: Path,
    records: tuple[Any, ...],
) -> str:
    funding_path = attempt / "funding.json"
    try:
        if (
            funding_path.is_symlink()
            or not funding_path.is_file()
            or funding_path.read_bytes() != _expected_funding_bytes(scenario, run_id)
        ):
            raise ValueError("funding evidence conflicts")
    except (OSError, RuntimeError, ValueError):
        raise BacktestResumeFailure("resume funding evidence is invalid", attempt) from None

    accepted_fact_dispatches: set[int] = set()
    applied_handoff_dispatches: set[int] = set()
    completed_economic_dispatches: set[int] = set()
    reconciliations: list[dict[str, object]] = []
    try:
        for record in records:
            document = json.loads(record.canonical_payload)
            if type(document) is not dict:
                raise ValueError("audit payload is not an object")
            if record.record_kind is AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME:
                if document.get("action") == "accepted" and type(document.get("fill")) is dict:
                    accepted_fact_dispatches.add(document["runtime_dispatch_sequence"])
            elif record.record_kind is AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME:
                original = document.get("original_ledger_apply_outcome")
                if (
                    document.get("action") == "effect_committed"
                    and type(original) is dict
                    and original.get("code") == OutcomeCode.LEDGER_APPLIED.value
                ):
                    applied_handoff_dispatches.add(document["dispatch_sequence"])
            elif record.record_kind is AuditRecordKind.RUNTIME_DISPATCH_COMPLETED:
                if document.get("ledger_outcome_count") == 1 and document.get("outcome_count") == 1:
                    completed_economic_dispatches.add(document["dispatch_sequence"])
            elif record.record_kind is AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME:
                reconciliations.append(document)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise BacktestResumeFailure(
            "resume durable frontier evidence is invalid", attempt
        ) from None

    durable_economic_dispatches = (
        accepted_fact_dispatches & applied_handoff_dispatches & completed_economic_dispatches
    )
    frontier = "funding_durable"
    if durable_economic_dispatches:
        frontier = "dispatch_durable"

    required_reconciliations = 2 if durable_economic_dispatches else 1
    expected_ledger_sequence = 2 if durable_economic_dispatches else 1
    if scenario.schema_version in (4, 5):
        if (
            accepted_fact_dispatches != durable_economic_dispatches
            or applied_handoff_dispatches != durable_economic_dispatches
            or completed_economic_dispatches != durable_economic_dispatches
            or len(durable_economic_dispatches)
            > (
                2 * json.loads(scenario.canonical_bytes)["strategy"]["max_round_trips"]
                if scenario.schema_version == 5
                else 2
            )
        ):
            raise BacktestResumeFailure("partial or excess round-trip dispatch evidence", attempt)
        expected_ledger_sequence = 1 + len(durable_economic_dispatches)
    complete_reconciliation = len(reconciliations) == required_reconciliations and all(
        document.get("run_id") == run_id.value
        and document.get("outcome_code") == OutcomeCode.RECONCILIATION_MATCH.value
        and document.get("requested_action") == "none"
        and document.get("ledger_sequence") == expected_ledger_sequence
        for document in reconciliations
    )
    if complete_reconciliation:
        frontier = "reconciliation_durable"
    return frontier


def _require_attempt_directory(run_dir: Path) -> tuple[Path, RunId]:
    if not isinstance(run_dir, Path) or not run_dir.is_absolute():
        raise BacktestResumeFailure("resume run directory must be an absolute path", run_dir)
    try:
        if run_dir.is_symlink() or run_dir.resolve(strict=True) != run_dir or not run_dir.is_dir():
            raise BacktestResumeFailure("resume run directory identity is invalid", run_dir)
        run_id = RunId(str(UUID(run_dir.name)))
        if run_dir.name != run_id.value:
            raise BacktestResumeFailure("resume run directory identity is invalid", run_dir)
    except (OSError, ValueError):
        raise BacktestResumeFailure("resume run directory identity is invalid", run_dir) from None
    return run_dir, run_id


def _load_verified_attempt(
    run_dir: Path,
) -> tuple[
    LoadedBacktestScenario,
    RunId,
    Sha256Digest,
    Any,
    dict[str, object],
    CanonicalAttemptManifest,
]:
    attempt, run_id = _require_attempt_directory(run_dir)
    manifest_path = attempt / "manifest.json"
    try:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise OSError("manifest is not a regular file")
        payload = manifest_path.read_bytes()
        document = json.loads(payload)
        if _canonical_json(document) != payload or type(document) is not dict:
            raise ValueError("manifest is not canonical")
        expected_fields = {
            "canonicalization",
            "code_sha256",
            "data",
            "distribution",
            "execution",
            "funding",
            "instrument_spec_set_sha256",
            "lineage_sha256",
            "randomness",
            "risk",
            "runtime",
            "run_id",
            "scenario",
            "schema",
        }
        if set(document) != expected_fields:
            raise ValueError("manifest fields conflict")
        scenario_document = document["scenario"]
        data_document = document["data"]
        if type(scenario_document) is not dict or type(data_document) is not dict:
            raise ValueError("manifest input identity is invalid")
        scenario_path = Path(scenario_document["path"])
        data_path = Path(data_document["path"])
        if (
            sha256(scenario_path.read_bytes()).hexdigest()
            != scenario_document["source_file_sha256"]
            or sha256(data_path.read_bytes()).hexdigest() != data_document["source_file_sha256"]
        ):
            raise ValueError("external input identity changed")
        lineage = Sha256Digest(document["lineage_sha256"])
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        raise BacktestResumeFailure("resume identity evidence is invalid", attempt) from None
    if document["schema"] != _ATTEMPT_SCHEMA or document["run_id"] != run_id.value:
        raise BacktestResumeFailure("resume identity evidence conflicts", attempt)
    try:
        package_path = attempt / "strategy.eastrategy"
        catalog = None
        if package_path.exists():
            catalog = ResearchStrategyCatalogV1(
                (validate_package(read_regular(package_path, attempt)),)
            )
        scenario = load_backtest_scenario(scenario_path, catalog=catalog)
        risk_policy, risk_context = _risk_context(scenario)
        recomputed_lineage = _lineage(
            scenario,
            risk_policy=risk_policy,
            risk_context=risk_context,
        )
        expected = _attempt_manifest_bytes(
            scenario,
            run_id=run_id,
            lineage=recomputed_lineage,
            risk_policy=risk_policy,
            risk_context=risk_context,
        )
    except Exception:
        raise BacktestResumeFailure(
            "resume identity evidence could not be reconstructed", attempt
        ) from None
    if recomputed_lineage != lineage or expected != payload:
        raise BacktestResumeFailure("resume identity evidence conflicts", attempt)
    return (
        scenario,
        run_id,
        lineage,
        risk_policy,
        risk_context,
        CanonicalAttemptManifest(RunReference(run_id, lineage), payload),
    )


def run_backtest_scenario(
    scenario: LoadedBacktestScenario,
    output_root: Path,
    *,
    run_id: RunId | None = None,
) -> BacktestRunResult:
    """Run one validated scenario through the existing offline authorities."""
    if type(scenario) is not LoadedBacktestScenario:
        raise BacktestRunError("scenario must be an exact loaded BacktestScenario")
    if run_id is None:
        run_id = RunId(str(uuid4()))
    elif type(run_id) is not RunId:
        raise BacktestRunError("run_id must be an exact RunId")
    output_root = _safe_output_root(output_root)
    risk_policy, risk_context = _risk_context(scenario)
    lineage = _lineage(scenario, risk_policy=risk_policy, risk_context=risk_context)
    manifest_bytes = _attempt_manifest_bytes(
        scenario,
        run_id=run_id,
        lineage=lineage,
        risk_policy=risk_policy,
        risk_context=risk_context,
    )
    binding = _binding(run_id, lineage, manifest_bytes)
    manifest = CanonicalAttemptManifest(binding.reference, manifest_bytes)
    store = LocalResultStore(output_root)
    journal: Any | None = None
    attempt = output_root / run_id.value
    last_frontier = "attempt_prepared"
    try:
        try:
            prepared = store.prepare_canonical_attempt(manifest)
        except StoreCollisionError:
            raise BacktestRunError("fresh attempt directory could not be created") from None
        if scenario.strategy_package is not None:
            package_path = attempt / "strategy.eastrategy"
            _write_or_verify(package_path, scenario.strategy_package.artifact_bytes)
            package_path.chmod(0o444)
        journal = create_posix_audit_journal(prepared.audit)

        def retain_funding(document: dict[str, object]) -> None:
            _write_or_verify(attempt / "funding.json", _canonical_json(document) + b"\n")

        def retain_frontier(frontier: str) -> None:
            nonlocal last_frontier
            last_frontier = frontier

        report, audit, funding = _execute(
            scenario,
            run_id=run_id,
            binding=binding,
            lineage=lineage,
            risk_policy=risk_policy,
            risk_context=risk_context,
            audit=journal,
            on_funding=retain_funding,
            on_frontier=retain_frontier,
        )
    except _AttemptFailure as failure:
        if journal is not None:
            _write_or_verify(attempt / "audit.jsonl", _audit_bytes(list(journal.records)))
            _write_or_verify(
                attempt / "failure.json",
                _failure_bytes(
                    classification="backtest.product_failure",
                    run_id=run_id,
                    lineage=lineage,
                    scenario=scenario,
                    last_frontier=last_frontier,
                    audit=journal,
                    code=failure.code.value,
                    message=failure.message,
                    details=failure.details,
                ),
            )
        raise BacktestRunFailure(failure.code, failure.message, attempt) from None
    except BacktestRunError:
        raise
    except BacktestRunFailure:
        raise
    except Exception:
        if journal is not None:
            _write_or_verify(
                attempt / "failure.json",
                _failure_bytes(
                    classification="backtest.internal_failure",
                    run_id=run_id,
                    lineage=lineage,
                    scenario=scenario,
                    last_frontier=last_frontier,
                    audit=journal,
                    code="backtest.internal_failure",
                    message="backtest failed because of an unexpected internal error",
                ),
            )
        raise BacktestRunFailure(
            OutcomeCode.DURABILITY_RESULT_WRITE_FAILED,
            "backtest failed because of an unexpected internal error",
            attempt,
        ) from None
    finally:
        if journal is not None:
            journal.close()
        store.close()
    _write_or_verify(attempt / "audit.jsonl", audit)
    _write_or_verify(attempt / "funding.json", _canonical_json(funding) + b"\n")
    _publish_success(attempt, _canonical_json(report) + b"\n")
    return BacktestRunResult("success", attempt)


def resume_backtest_attempt(run_dir: Path) -> BacktestRunResult:
    """Verify and continue one supported durable frontier of the same attempt."""
    scenario, run_id, lineage, risk_policy, risk_context, manifest = _load_verified_attempt(run_dir)
    attempt = run_dir
    store = LocalResultStore(attempt.parent)
    journal: Any | None = None
    terminal_recovery: Any | None = None
    last_frontier = "attempt_prepared"
    replay_record_count: int | None = None
    try:
        try:
            verified = store.verify_recovery_attempt(manifest)
        except Exception:
            raise BacktestResumeFailure(
                "resume audit or identity evidence is invalid",
                attempt,
            ) from None
        failure_path = attempt / "failure.json"
        if failure_path.exists():
            try:
                failure_payload = failure_path.read_bytes()
                failure = json.loads(failure_payload)
                if (
                    _canonical_json(failure) + b"\n" != failure_payload
                    or type(failure) is not dict
                    or failure.get("status") != "failed"
                    or failure.get("terminal_state") != "failed"
                    or failure.get("run_id") != run_id.value
                    or failure.get("lineage_sha256") != lineage.value
                ):
                    raise ValueError("failed evidence conflicts")
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                raise BacktestResumeFailure("failed attempt evidence is invalid", attempt) from None
            raise BacktestResumeFailure("failed attempt is terminal and cannot resume", attempt)
        result_path = attempt / "result.json"
        publication_path = attempt / "result.publication.json"
        if publication_path.exists():
            raise BacktestResumeFailure("resume success publication evidence is ambiguous", attempt)
        if result_path.exists():
            if type(verified) is not VerifiedTerminalRecoveryBinding:
                raise BacktestResumeFailure(
                    "resume success evidence lacks a terminal audit", attempt
                )
            terminal_recovery = store.recover_terminal_attempt(verified)
            try:
                result = json.loads(result_path.read_bytes())
            except (OSError, ValueError, json.JSONDecodeError):
                raise BacktestResumeFailure("resume success evidence is invalid", attempt) from None
            if (
                type(result) is not dict
                or result.get("status") != "success"
                or result.get("run_id") != run_id.value
                or result.get("lineage_sha256") != lineage.value
            ):
                raise BacktestResumeFailure("resume success identity conflicts", attempt)
            reconstructed_report, reconstructed_audit, reconstructed_funding = _execute(
                scenario,
                run_id=run_id,
                binding=manifest.binding,
                lineage=lineage,
                risk_policy=risk_policy,
                risk_context=risk_context,
            )
            reconstructed_terminal = json.loads(reconstructed_audit.splitlines()[-1])
            if (
                reconstructed_terminal["record_sha256"]
                != audit_record_digest(terminal_recovery.terminal_record).value
                or result_path.read_bytes() != _canonical_json(reconstructed_report) + b"\n"
                or (attempt / "audit.jsonl").read_bytes() != reconstructed_audit
                or (attempt / "funding.json").read_bytes()
                != _canonical_json(reconstructed_funding) + b"\n"
            ):
                raise BacktestResumeFailure("resume completed evidence conflicts", attempt)
            return BacktestRunResult("success", attempt)

        pending_result = attempt / "result.pending"
        if pending_result.exists() and type(verified) is VerifiedIncompleteRecoveryBinding:
            raise BacktestResumeFailure(
                "resume frontier is ambiguous because success staging precedes terminal evidence",
                attempt,
            )

        binding = manifest.binding

        def retain_funding(document: dict[str, object]) -> None:
            _write_or_verify(attempt / "funding.json", _canonical_json(document) + b"\n")

        def retain_frontier(frontier: str) -> None:
            nonlocal last_frontier, replay_record_count
            if journal is None or replay_record_count is None:
                return
            current_record_count = len(journal.records)
            if current_record_count > replay_record_count:
                last_frontier = frontier
                replay_record_count = current_record_count

        if type(verified) is VerifiedTerminalRecoveryBinding:
            terminal_recovery = store.recover_terminal_attempt(verified)
            report, audit, funding = _execute(
                scenario,
                run_id=run_id,
                binding=binding,
                lineage=lineage,
                risk_policy=risk_policy,
                risk_context=risk_context,
                on_funding=retain_funding,
                on_frontier=retain_frontier,
            )
            reconstructed = json.loads(audit.splitlines()[-1])
            if (
                reconstructed["record_sha256"]
                != audit_record_digest(terminal_recovery.terminal_record).value
            ):
                raise BacktestResumeFailure(
                    "resume terminal evidence conflicts with deterministic reconstruction",
                    attempt,
                )
        elif type(verified) is VerifiedIncompleteRecoveryBinding:
            recovered = store.recover_incomplete_attempt(verified)
            journal = reopen_posix_audit_journal(recovered.audit)
            persisted_records = tuple(journal.records)
            last_frontier = _verified_persisted_frontier(
                scenario=scenario,
                run_id=run_id,
                attempt=attempt,
                records=persisted_records,
            )
            replay_record_count = len(persisted_records)
            report, audit, funding = _execute(
                scenario,
                run_id=run_id,
                binding=binding,
                lineage=lineage,
                risk_policy=risk_policy,
                risk_context=risk_context,
                audit=journal,
                on_funding=retain_funding,
                on_frontier=retain_frontier,
            )
        else:
            raise BacktestResumeFailure("resume frontier classification is unsupported", attempt)
        _write_or_verify(attempt / "audit.jsonl", audit)
        _write_or_verify(attempt / "funding.json", _canonical_json(funding) + b"\n")
        _publish_success(attempt, _canonical_json(report) + b"\n")
        return BacktestRunResult("success", attempt)
    except BacktestResumeFailure:
        raise
    except BacktestRunFailure as error:
        raise BacktestResumeFailure(str(error), attempt) from None
    except Exception:
        if journal is not None:
            _write_or_verify(
                attempt / "failure.json",
                _failure_bytes(
                    classification="backtest.internal_failure",
                    run_id=run_id,
                    lineage=lineage,
                    scenario=scenario,
                    last_frontier=last_frontier,
                    audit=journal,
                    code="backtest.internal_failure",
                    message="backtest resume failed because of an unexpected internal error",
                ),
            )
        raise BacktestResumeFailure("backtest resume internal failure", attempt) from None
    finally:
        if journal is not None:
            journal.close()
        if terminal_recovery is not None:
            terminal_recovery._finish()
        store.close()


__all__ = [
    "BacktestRunError",
    "BacktestRunFailure",
    "BacktestRunResult",
    "BacktestResumeFailure",
    "resume_backtest_attempt",
    "run_backtest_scenario",
]
