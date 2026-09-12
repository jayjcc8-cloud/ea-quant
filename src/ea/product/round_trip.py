"""Explicit single-long-round-trip composition over existing economic authorities."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from ea.composition.lifecycle import (
    create_phase1_historical_economic_gate,
    create_phase1_historical_lifecycle,
)
from ea.core import (
    Adjustment,
    AuditRecordKind,
    AuditSubjectKind,
    CanonicalDecimal,
    FactProvenanceId,
    HistoricalDispatchKind,
    InitialFunding,
    MarketDataEnvelope,
    OutcomeCode,
    Phase1PortfolioPolicyEntry,
    PortfolioPolicyId,
    RiskDecisionKind,
    SignalDirection,
    audit_chain_head,
    canonical_fill_bytes,
    canonical_order_bytes,
    canonical_portfolio_snapshot_bytes,
    canonical_run_prepared_audit_payload,
    create_phase1_portfolio_policy,
    fill_digest,
    order_digest,
    portfolio_snapshot_digest,
    require_quantized,
)
from ea.data import (
    create_phase1_historical_market_data_source,
    create_phase1_historical_market_source_bridge,
)
from ea.execution import create_phase1_order_authority
from ea.portfolio import create_portfolio_ledger, create_portfolio_planning_authority
from ea.product import backtest as b
from ea.product.identity import semantic_outcome_sha256
from ea.product.offline_demo import _audit_bytes, _DemoAudit, _GlobalHaltView, _InstrumentGateView
from ea.product.scenario import _next_bar_entry_delay_maximum
from ea.reconciliation import create_phase1_reconciliation_authority
from ea.runtime import (
    create_active_market_dispatch_verifier,
    create_phase1_historical_market_runtime,
)
from ea.strategy import create_strategy_signal_authority
from ea.strategy.sdk_v1 import StrategyBarV1
from ea.strategy.sdk_v2 import PositionState, PositionViewV2, decode_action


def execute_round_trip(
    scenario: Any,
    *,
    run_id: Any,
    binding: Any,
    lineage: Any,
    risk_policy: Any,
    risk_context: Any,
    audit: Any = None,
    on_funding: Any = None,
    on_frontier: Any = None,
) -> tuple[dict[str, object], bytes, dict[str, object]]:
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
    funding_document = b._funding_document(funding, funding_outcome)
    if on_funding is not None:
        on_funding(funding_document)
    if on_frontier is not None:
        on_frontier("funding_durable")
    b._interrupt("funding_durable")
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
        source_namespace=b._SOURCE_NAMESPACE,
        provenance_id=FactProvenanceId("backtest.scenario.simulator.v1"),
        order_issuance_verifier=orders,
        risk_policy=risk_policy,
        global_halt=_GlobalHaltView(run_id),
        instrument_gate=instrument_gate,
        economic_gate=economic_gate,
    )

    verifier = create_active_market_dispatch_verifier(runtime)
    signals = create_strategy_signal_authority(run_id=run_id, verifier=verifier)
    policy = create_phase1_portfolio_policy(
        policy_id=PortfolioPolicyId("backtest.single-long-round-trip.v1"),
        entries=(
            Phase1PortfolioPolicyEntry(
                instrument=scenario.instrument,
                target_quantity=CanonicalDecimal(
                    str(scenario.strategy_parameters["target_quantity"])
                    if scenario.strategy_package is None
                    else scenario.max_order_quantity.text
                ),
            ),
        ),
        spec_set=scenario.spec_set,
    )
    planner = create_portfolio_planning_authority(
        run_id=run_id,
        ledger=economic_gate.ledger,
        spec_set=scenario.spec_set,
        policy=policy,
        execution_policy=scenario.execution_policy,
    )
    logic = scenario.strategy_entry.factory(scenario.strategy_parameters)
    bounded = scenario.schema_version == 5
    max_round_trips = (
        json.loads(scenario.canonical_bytes)["strategy"]["max_round_trips"] if bounded else 1
    )
    issued: list[Any] = []
    risk_results: list[Any] = []
    committed: list[Any] = []
    state = PositionState.FLAT_INITIAL
    quantity = CanonicalDecimal("0")
    pending = False
    last_entry = _next_bar_entry_delay_maximum(scenario.dataset)
    market_index = 0
    while True:
        active = lifecycle.coordinator.begin_next_dispatch()
        if active.dispatch_kind is HistoricalDispatchKind.END_OF_RUN:
            end_window = active
            break
        if active.dispatch_kind is HistoricalDispatchKind.MARKET:
            lease = runtime.active_lease
            if lease is None or type(lease.root) is not MarketDataEnvelope:
                raise RuntimeError("missing active market root")
            root = lease.root
            verifier.verify_active_market_dispatch(root, dispatch_sequence=lease.dispatch_sequence)
            if (
                not pending
                and last_entry is not None
                and market_index <= last_entry
                and root.revision == 0
                and root.payload.adjustment is Adjustment.RAW
            ):
                bar = root.payload
                action = decode_action(
                    logic.on_bar(
                        StrategyBarV1(
                            root.event_time,
                            root.available_at,
                            Decimal(str(bar.open)),
                            Decimal(str(bar.high)),
                            Decimal(str(bar.low)),
                            Decimal(str(bar.close)),
                            Decimal(str(bar.volume)),
                        ),
                        PositionViewV2(state, quantity),
                    )
                )
                if action.action != "HOLD":
                    entering = action.action == "ENTER_LONG"
                    if (
                        (
                            entering
                            and (
                                state is not PositionState.FLAT_INITIAL
                                or (issued and not bounded)
                                or len(committed) // 2 >= max_round_trips
                            )
                        )
                        or (not entering and state is not PositionState.LONG_OPEN)
                        or len(issued) >= 2 * max_round_trips
                    ):
                        raise ValueError("illegal bounded position transition")
                    if (
                        entering
                        and scenario.strategy_package is None
                        and action.quantity != policy.entries[0].target_quantity
                    ):
                        raise ValueError("entry quantity conflicts with declared target")
                    if entering and action.quantity is None:
                        raise ValueError("missing entry quantity")
                    require_quantized(
                        action.quantity if action.quantity is not None else quantity,
                        scenario.spec_set.require(scenario.instrument).quantity_quantum,
                        field_name="order quantity",
                    )
                    if (
                        entering
                        and action.quantity is not None
                        and scenario.strategy_package is not None
                    ):
                        policy = create_phase1_portfolio_policy(
                            policy_id=PortfolioPolicyId("backtest.single-long-round-trip.v1"),
                            entries=(
                                Phase1PortfolioPolicyEntry(
                                    instrument=scenario.instrument, target_quantity=action.quantity
                                ),
                            ),
                            spec_set=scenario.spec_set,
                        )
                        planner = create_portfolio_planning_authority(
                            run_id=run_id,
                            ledger=economic_gate.ledger,
                            spec_set=scenario.spec_set,
                            policy=policy,
                            execution_policy=scenario.execution_policy,
                        )
                    signal = signals.issue(
                        root,
                        dispatch_sequence=lease.dispatch_sequence,
                        direction=SignalDirection.LONG if entering else SignalDirection.FLAT,
                    )
                    intent = planner.plan(signal).intent
                    if intent is None:
                        raise ValueError("action did not produce an intent")
                    risk = economic_gate.risk_authority.evaluate(
                        intent, economic_gate.ledger.snapshot
                    )
                    if risk.decision.kind is RiskDecisionKind.REJECT or (
                        not entering and risk.decision.kind is not RiskDecisionKind.ALLOW
                    ):
                        raise ValueError("round trip risk rejected or resized exit")
                    order = orders.create_order(intent, risk)
                    if (order.quantity != quantity and not entering) or order.side.value != (
                        "buy" if entering else "sell"
                    ):
                        raise ValueError("order conflicts with bounded action")
                    issued.append(order)
                    risk_results.append(risk)
                    instrument_gate.hold_for(order.order_id)
                    lifecycle.coordinator.prepare_submission_authorization(
                        active,
                        order,
                        causal_market_root=root,
                        dispatch_sequence=lease.dispatch_sequence,
                    )
                    lifecycle.coordinator.submit_authorized_order(active, order)
                    pending = True
            market_index += 1
        lifecycle.coordinator.complete_active_dispatch(active)
        fills = lifecycle.fact_authority.fills
        if len(fills) != len(committed):
            if len(fills) != len(committed) + 1 or len(fills) > 2 * max_round_trips:
                raise ValueError("invalid bounded Fill count")
            fill = fills[-1]
            order = issued[len(committed)]
            if (
                fill.order_id != order.order_id
                or fill.quantity != order.quantity
                or fill.side != order.side
            ):
                raise ValueError("Fill conflicts with issued full order")
            committed.append(fill)
            state = (
                PositionState.LONG_OPEN
                if len(committed) % 2
                else PositionState.FLAT_INITIAL
                if bounded
                else PositionState.FLAT_CLOSED
            )
            quantity = fill.quantity if len(committed) % 2 else CanonicalDecimal("0")
            pending = False
            if on_frontier is not None:
                on_frontier("dispatch_durable")
            b._interrupt("dispatch_durable")

    snapshot = economic_gate.ledger.snapshot
    replay = create_portfolio_ledger(run_id, scenario.spec_set)
    replay.apply_initial_funding(funding)
    for fill in committed:
        if replay.apply_fill(fill).code is not OutcomeCode.LEDGER_APPLIED:
            raise ValueError("Fill replay failed")
    if portfolio_snapshot_digest(snapshot) != portfolio_snapshot_digest(replay.snapshot):
        raise ValueError("round trip ledger replay diverged")
    if snapshot.ledger_sequence != len(committed) + 1:
        raise ValueError("round trip ledger sequence conflicts")
    reconciliation = create_phase1_reconciliation_authority(
        run_id=run_id, spec_set=scenario.spec_set, snapshot_view=lambda: snapshot
    )
    sequence = lifecycle.coordinator.state.last_completed_dispatch_sequence
    if sequence is None:
        raise ValueError("missing completed dispatch")
    for position in [True, False] if committed else [False]:
        observation = b._observation(
            scenario=scenario, run_id=run_id, snapshot=snapshot, position=position
        )
        outcome = reconciliation.admit_observation(observation, dispatch_sequence=sequence)
        b._append_reconciliation(audit, outcome)
        if outcome.outcome_code is not OutcomeCode.RECONCILIATION_MATCH:
            raise ValueError("round trip reconciliation failed")
    if on_frontier is not None:
        on_frontier("reconciliation_durable")
    b._interrupt("reconciliation_durable")
    lifecycle.coordinator.complete_active_dispatch(end_window)
    if on_frontier is not None:
        on_frontier("terminal_durable")
    legs = []
    for index, order in enumerate(issued):
        leg_fill = committed[index] if index < len(committed) else None
        legs.append(
            {
                "role": "entry" if index % 2 == 0 else "exit",
                "order_evidence": json.loads(canonical_order_bytes(order)),
                "order_sha256": order_digest(order).value,
                "fill_sha256": None if leg_fill is None else fill_digest(leg_fill).value,
                "order": {
                    "order_id": b._id_document(order.order_id),
                    "quantity": order.quantity.text,
                    "side": order.side.value,
                },
                "fill": None if leg_fill is None else json.loads(canonical_fill_bytes(leg_fill)),
                "risk": {
                    "decision": risk_results[index].decision.kind.value,
                },
                "outcome": "expired" if leg_fill is None else "filled",
            }
        )
    document: dict[str, Any] = {
        "schema": "ea.backtest-single-run-result.v4"
        if bounded
        else "ea.backtest-single-run-result.v3",
        "status": "success",
        "terminal_state": "completed",
        "run_id": run_id.value,
        "lineage_sha256": lineage.value,
        "scenario_sha256": scenario.scenario_sha256.value,
        "audit_chain_head_sha256": audit_chain_head(audit.records[-1]).value,
        "execution_legs": legs,
        "position_state": ("LONG" if quantity.coefficient else "FLAT") if bounded else state.value,
        "position_outcome": "OPEN_AT_END"
        if state is PositionState.LONG_OPEN
        else ("FLAT_AFTER_TRADES" if committed else "FLAT_NO_TRADE")
        if bounded
        else "CLOSED"
        if state is PositionState.FLAT_CLOSED
        else "FLAT_INITIAL",
        "final_quantity": quantity.text,
        "completed_round_trips": len(committed) // 2,
        "ledger_sequence": snapshot.ledger_sequence,
        "portfolio_snapshot_evidence": json.loads(canonical_portfolio_snapshot_bytes(snapshot)),
        "ending_cash": [
            {"amount": balance.amount.text, "currency": balance.currency.code}
            for balance in snapshot.cash_balances
        ],
        "ending_positions": [
            {
                "quantity": balance.quantity.text,
                "symbol": balance.instrument.symbol,
                "venue": balance.instrument.venue.code,
            }
            for balance in snapshot.position_balances
        ],
        "initial_funding": {
            "amount": scenario.initial_cash.text,
            "currency": scenario.funding_currency.code,
            "ledger_sequence": 1,
            "status": "applied",
        },
        "randomness": scenario.randomness.document(),
        "reconciliation": {
            "cash": "match",
            "position": "match" if committed else "not_required_empty",
        },
    }
    from ea.product.reporting import _decimal_from_source
    from ea.product.round_trip_report import economics, semantic_projection

    last_price = _decimal_from_source(str(scenario.dataset.selection.events[-1].payload.close))
    document["final_equity"] = economics(document, scenario, last_price)["equity"]["amount"]
    document["semantic_outcome_sha256"] = semantic_outcome_sha256(
        semantic_projection(document)
    ).value
    return document, _audit_bytes(list(audit.records)), funding_document
