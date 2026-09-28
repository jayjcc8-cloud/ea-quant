"""Explicit local Paper composition over the existing trading authorities."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from decimal import Decimal, localcontext
from typing import Any

from ea.composition.lifecycle import (
    create_local_paper_economic_gate,
    create_recovered_local_paper_economic_gate,
)
from ea.core import (
    AuditAppendAcknowledgement,
    AuditLogicalKey,
    AuditRecordKind,
    CanonicalDecimal,
    CashReconciliationBalance,
    EconomicId,
    EconomicOwnerKind,
    ExecutionFactAction,
    FactProvenanceId,
    InitialFunding,
    InstrumentRiskLimit,
    LedgerHandoffAction,
    MarketDataEnvelope,
    OrderSide,
    OutcomeCode,
    Phase1PortfolioPolicyEntry,
    PortfolioPolicyId,
    PositionReconciliationBalance,
    ReconciliationObservationKind,
    ReconciliationScopeKind,
    RiskDecisionKind,
    RiskHaltReason,
    RiskPolicyId,
    RunBinding,
    RuntimeIdentifier,
    SignalDirection,
    SourceNamespace,
    audit_append_acknowledgement_digest,
    audit_subject_digest,
    canonical_execution_fact_processing_outcome_bytes,
    canonical_ledger_handoff_outcome_bytes,
    canonical_portfolio_risk_refresh_bytes,
    canonical_reconciliation_outcome_bytes,
    canonical_run_prepared_audit_payload,
    causal_market_digest,
    create_phase1_portfolio_policy,
    create_phase1_risk_policy,
    create_reconciliation_observation,
    ordered_digest_tuple,
    portfolio_snapshot_digest,
    require_audit_acknowledgement,
    require_quantized,
    risk_state_snapshot_digest,
    settle_product,
)
from ea.core.audit import AUDIT_SUBJECT_BY_RECORD_KIND, AuditAppendPort, AuditRecord
from ea.core.commission import (
    commission_amount,
    commission_bps_from_identity,
    slippage_bps_from_identity,
)
from ea.core.execution_messages import ExecutionFactIngress, Order
from ea.core.historical_matching import _quantized_historical_close
from ea.core.lifecycle import (
    ORDERED_LEDGER_ACK_DIGEST_DOMAIN,
    create_paper_audited_execution_fact_handoff,
)
from ea.core.market_data import SourceId
from ea.core.paper import (
    canonical_paper_fact_dispatch_payload,
    canonical_paper_operational_safety_payload,
    canonical_paper_order_construction_payload,
    canonical_paper_submission_payload,
    canonical_paper_submission_result_payload,
)
from ea.core.time import Clock
from ea.execution import (
    create_phase1_execution_fact_authority,
    create_phase1_order_authority,
    recover_phase1_execution_fact_authority_history,
)
from ea.execution.order_lifecycle import OrderCommandTracker, SubmissionAttemptState
from ea.execution.paper_broker import PaperBroker
from ea.observability import OperationalLogger
from ea.portfolio import create_portfolio_ledger, create_portfolio_planning_authority
from ea.product.operational import ProductObservation
from ea.product.paper_health import (
    BrokerState,
    KillSwitchProjectionState,
    PaperHealthObservation,
    PaperHealthSnapshot,
    PaperSafetyObservation,
    ReconciliationState,
    RecoveryState,
    StorageState,
    StrategyHeartbeatState,
    build_paper_health,
    market_state_from_stream,
    runtime_state_from_stream,
)
from ea.product.paper_recovery import (
    recover_paper_broker,
    recover_paper_economic_state,
    recover_paper_fact_authority,
    recover_paper_order_contexts,
)
from ea.product.scenario import LoadedBacktestScenario
from ea.reconciliation import create_phase1_reconciliation_authority
from ea.risk.kill_switch import OperatorKillSwitchAuthority
from ea.risk.operational_safety import (
    OperationalSafetyAuthority,
    OperationalSafetyInput,
    OperationalSafetyLimits,
    OperationalSafetyVerdict,
)
from ea.runtime.streaming import StreamingMarketRuntime, StreamPhase
from ea.strategy import create_strategy_signal_authority
from ea.strategy.catalog import LocalActionLogic
from ea.strategy.sdk_v1 import StrategyBarV1
from ea.strategy.sdk_v2 import HoldRootsLogic, PositionState, PositionViewV2, decode_action


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


class PaperTradingSession:
    """One accepted configuration's incremental local simulation, never a backtest loop."""

    def __init__(
        self,
        scenario: LoadedBacktestScenario,
        *,
        binding: RunBinding,
        audit: AuditAppendPort,
        clock: Clock,
        monotonic: Callable[[], float],
        source_id: SourceId,
        prices: tuple[float, ...],
        stop_requested: Callable[[], bool],
        operational_logger: OperationalLogger | None = None,
        max_market_age_seconds: float = 5.0,
        stall_timeout_seconds: float = 5.0,
        kill_switch: OperatorKillSwitchAuthority | None = None,
        operational_limits: OperationalSafetyLimits | None = None,
        crash_after: AuditRecordKind | None = None,
    ) -> None:
        if scenario.schema_version not in (4, 5):
            raise ValueError("local Paper requires an accepted Action V2 long-only configuration")
        self.scenario, self.binding, self.audit, self.clock = scenario, binding, audit, clock
        self.stop_requested, self.max_age = stop_requested, max_market_age_seconds
        self.kill_switch = kill_switch
        self.monotonic = monotonic
        self._crash_after = crash_after
        self.run_id = binding.reference.run_id
        self.operations = ProductObservation(operational_logger)
        self._append(AuditRecordKind.RUN_PREPARED, canonical_run_prepared_audit_payload(binding))
        self.spec = scenario.spec_set.require(scenario.instrument)
        self.max_round_trips = (
            json.loads(scenario.canonical_bytes)["strategy"]["max_round_trips"]
            if scenario.schema_version == 5
            else 1
        )
        self.raw_price_bound = max(prices)
        self.price_bound = _quantized_historical_close(
            max(prices),
            side=OrderSide.BUY,
            specification=self.spec,
            slippage_bps=slippage_bps_from_identity(
                scenario.execution_policy.identifier.value, scenario.execution_policy.sha256.value
            ),
        )
        policy = create_phase1_risk_policy(
            policy_id=RiskPolicyId("paper.local.risk.v1"),
            spec_set=scenario.spec_set,
            execution_policy=scenario.execution_policy,
            instrument_limits=(
                InstrumentRiskLimit(
                    scenario.instrument, scenario.max_order_quantity, scenario.max_position_quantity
                ),
            ),
        )
        self.funding = InitialFunding(self.run_id, scenario.funding_currency, scenario.initial_cash)
        self.gate = create_local_paper_economic_gate(
            run_id=self.run_id,
            spec_set=scenario.spec_set,
            execution_policy=scenario.execution_policy,
            risk_policy=policy,
            initial_funding=self.funding,
        )
        self.orders = create_phase1_order_authority(
            run_id=self.run_id,
            spec_set=scenario.spec_set,
            execution_policy=scenario.execution_policy,
            risk_policy=policy,
            risk_result_verifier=self.gate.risk_authority,
        )
        self.tracker = OrderCommandTracker(self.orders)
        self.broker = PaperBroker(self.orders, max_orders=2 * self.max_round_trips)
        self.operational_safety = OperationalSafetyAuthority(
            run_id=self.run_id,
            limits=(
                operational_limits
                if operational_limits is not None
                else self._default_operational_limits()
            ),
        )
        self.runtime = StreamingMarketRuntime(
            run_id=self.run_id,
            spec_set=scenario.spec_set,
            source_id=source_id,
            clock=clock,
            monotonic=monotonic,
            max_market_age_seconds=max_market_age_seconds,
            stall_timeout_seconds=stall_timeout_seconds,
            fact_source=self.broker,
        )
        self.facts = create_phase1_execution_fact_authority(
            run_id=self.run_id,
            spec_set=scenario.spec_set,
            order_verifier=self.orders,
            dispatch_verifier=self.runtime,
        )
        self.signals = create_strategy_signal_authority(run_id=self.run_id, verifier=self.runtime)
        target = (
            CanonicalDecimal(str(scenario.strategy_parameters["target_quantity"]))
            if scenario.strategy_package is None
            else scenario.max_order_quantity
        )
        self.planner = create_portfolio_planning_authority(
            run_id=self.run_id,
            ledger=self.gate.ledger,
            spec_set=scenario.spec_set,
            policy=self._portfolio_policy(target),
            execution_policy=scenario.execution_policy,
        )
        logic = scenario.strategy_entry.factory(scenario.strategy_parameters)
        if not isinstance(logic, (HoldRootsLogic, LocalActionLogic)):
            raise ValueError("Paper strategy does not implement the Action V2 profile")
        self.logic = logic
        self.position_state = PositionState.FLAT_INITIAL
        self.quantity = CanonicalDecimal("0")
        self.pending: Order | None = None
        self.issued: list[Order] = []
        self.committed: list[Any] = []
        self.duplicates = 0
        self.last_market: MarketDataEnvelope | None = None
        self.completed_sequence = 0
        self.operations.emit(
            "portfolio.funded",
            amount=scenario.initial_cash.text,
            currency=scenario.funding_currency.code,
            ledger_sequence=1,
        )

    def _portfolio_policy(self, quantity: CanonicalDecimal) -> Any:
        return create_phase1_portfolio_policy(
            policy_id=PortfolioPolicyId("paper.local.long-only.v1"),
            spec_set=self.scenario.spec_set,
            entries=(Phase1PortfolioPolicyEntry(self.scenario.instrument, quantity),),
        )

    def _default_operational_limits(self) -> OperationalSafetyLimits:
        return OperationalSafetyLimits(
            max_market_age_seconds=self.max_age,
            max_daily_loss=Decimal(self.scenario.initial_cash.text),
            max_total_exposure=Decimal(self.scenario.max_notional.text),
            max_open_orders=2 * self.max_round_trips,
            max_order_rate=2 * self.max_round_trips,
            order_rate_window_seconds=60.0,
            max_price_deviation_bps=250,
        )

    def _notional(self, price: CanonicalDecimal, quantity: CanonicalDecimal) -> Decimal:
        if quantity.coefficient == 0:
            return Decimal("0")
        settled = settle_product(
            price, quantity, self.spec.contract_multiplier, self.spec.currency_quantum
        )
        return Decimal(settled.amount.text)

    def _signed_notional(self, side: OrderSide, quantity: CanonicalDecimal) -> Decimal:
        value = self._notional(self.price_bound, quantity)
        return value if side is OrderSide.BUY else -value

    def _market_age_seconds(self, root: MarketDataEnvelope) -> float:
        return (self.clock.now() - root.event_time).total_seconds()

    def _reconciliation_clean(self) -> bool:
        """True when no ledger reference and no halt leaves broker state unresolved."""
        snapshot = self.gate.ledger.snapshot
        risk = self.gate.risk_authority.risk_state
        return not snapshot.open_reconciliation_refs and not (
            risk.halted and risk.halt_reason is RiskHaltReason.RECONCILIATION_REQUIRED
        )

    def _safety_observation(self, root: MarketDataEnvelope) -> PaperSafetyObservation:
        """Observed operational state in the shape PPV-15 consumes.

        Shared by the real outbound authorization and the read-only health probe
        so the two can never disagree about what was observed.
        """
        snapshot = self.gate.ledger.snapshot
        reference_price = _quantized_historical_close(
            root.payload.close, side=OrderSide.BUY, specification=self.spec
        )
        cash = next(
            (
                Decimal(balance.amount.text)
                for balance in snapshot.cash_balances
                if balance.currency == self.scenario.funding_currency
            ),
            Decimal("0"),
        )
        position_quantity = next(
            (
                balance.quantity
                for balance in snapshot.position_balances
                if balance.instrument == self.scenario.instrument
            ),
            CanonicalDecimal("0"),
        )
        position_mark = self._notional(reference_price, position_quantity)
        daily_loss = Decimal(self.scenario.initial_cash.text) - (cash + position_mark)
        outstanding = (
            self._signed_notional(self.pending.side, self.pending.quantity)
            if self.pending is not None
            else Decimal("0")
        )
        return PaperSafetyObservation(
            kill_switch_halted=(
                self.kill_switch is not None and self.kill_switch.effective_halted()
            ),
            reconciliation_healthy=self._reconciliation_clean(),
            market_age_seconds=self._market_age_seconds(root),
            broker_available=self.broker.available,
            strategy_live=self.runtime.phase is StreamPhase.RUNNING,
            daily_loss=daily_loss,
            current_exposure=position_mark,
            outstanding_order_exposure=outstanding,
            open_order_count=1 if self.pending is not None else 0,
            reference_price=Decimal(reference_price.text),
            now_monotonic=self.monotonic(),
        )

    def _operational_safety_input(
        self, order: Order, root: MarketDataEnvelope
    ) -> OperationalSafetyInput:
        observation = self._safety_observation(root)
        slippage = slippage_bps_from_identity(
            self.scenario.execution_policy.identifier.value,
            self.scenario.execution_policy.sha256.value,
        )
        proposed_price = _quantized_historical_close(
            root.payload.close,
            side=order.side,
            specification=self.spec,
            slippage_bps=slippage,
        )
        return observation.proposed_input(
            proposed_order_exposure=self._signed_notional(order.side, order.quantity),
            proposed_effective_price=Decimal(proposed_price.text),
            order_identity=order.client_submission_key.value,
        )

    def health(
        self,
        *,
        candidate_id: str | None = None,
        recovery_state: RecoveryState = RecoveryState.UNKNOWN,
        storage_state: StorageState = StorageState.UNKNOWN,
    ) -> PaperHealthSnapshot:
        """Project the read-only operational health of this running session.

        Liveness is self-evident here: this process is executing the projection,
        which is the same truth the durable writer lease records. Recovery
        admission and storage attestation belong to the composition root, so
        both default to unattested and an unattested dependency never reads
        ready. Trade permission is the PPV-15 zero-effect probe over exactly the
        state a real outbound check would observe.
        """
        stream = self.runtime.status()
        root = self.last_market
        return build_paper_health(
            PaperHealthObservation(
                process_alive=True,
                runtime_state=runtime_state_from_stream(stream),
                recovery_state=recovery_state,
                reconciliation_state=(
                    ReconciliationState.CLEAN
                    if self._reconciliation_clean()
                    else ReconciliationState.UNRESOLVED
                ),
                market_state=market_state_from_stream(
                    stream,
                    market_age_seconds=(None if root is None else self._market_age_seconds(root)),
                    max_market_age_seconds=(self.operational_safety.limits.max_market_age_seconds),
                ),
                broker_state=(
                    BrokerState.AVAILABLE if self.broker.available else BrokerState.UNAVAILABLE
                ),
                strategy_heartbeat_state=(
                    StrategyHeartbeatState.LIVE
                    if self.runtime.phase is StreamPhase.RUNNING
                    else StrategyHeartbeatState.NOT_LIVE
                ),
                storage_state=storage_state,
                kill_switch_state=(
                    KillSwitchProjectionState.NOT_CONFIGURED
                    if self.kill_switch is None
                    else KillSwitchProjectionState.HALTED
                    if self.kill_switch.effective_halted()
                    else KillSwitchProjectionState.ACTIVE
                ),
                safety=None if root is None else self._safety_observation(root),
                observed_at=self.clock.now(),
                run_id=self.run_id,
                candidate_id=candidate_id,
            ),
            authority=self.operational_safety,
        )

    def _operational_authorize(self, order: Order, root: MarketDataEnvelope, sequence: int) -> bool:
        """Authorize one outbound effect through the single safety gate.

        Returns True only for ALLOW. A DENY or HALT is recorded as a durable
        ``PAPER_OPERATIONAL_SAFETY`` audit record; HALT additionally engages the
        authoritative ``EXTERNAL_SAFETY_HALT`` and stops the runtime, so no
        unsafe state can ever reach the broker.
        """
        decision = self.operational_safety.authorize(self._operational_safety_input(order, root))
        if decision.verdict is OperationalSafetyVerdict.ALLOW:
            return True
        self._append(
            AuditRecordKind.PAPER_OPERATIONAL_SAFETY,
            canonical_paper_operational_safety_payload(
                self.run_id,
                dispatch_sequence=sequence,
                verdict=decision.verdict.value,
                guard=decision.guard,
                reason=decision.reason,
                limit=decision.limit,
                observed=decision.observed,
                identity=decision.identity or order.client_submission_key.value,
            ),
        )
        if decision.verdict is OperationalSafetyVerdict.HALT:
            self.gate.risk_authority.engage_halt(
                RiskHaltReason.EXTERNAL_SAFETY_HALT, root.available_at, sequence
            )
            self.runtime.request_stop()
        return False

    def _append(self, kind: AuditRecordKind, payload: bytes) -> AuditAppendAcknowledgement:
        subject = audit_subject_digest(kind, payload)
        key = AuditLogicalKey(kind, AUDIT_SUBJECT_BY_RECORD_KIND[kind], subject)
        ack = self.audit.append(
            record_kind=kind,
            subject_kind=key.subject_kind,
            subject_sha256=subject,
            canonical_payload=payload,
        )
        require_audit_acknowledgement(
            ack, binding=self.binding, logical_key=key, canonical_payload=payload
        )
        if self._crash_after is not None and kind is self._crash_after:
            raise RuntimeError(f"paper crash injection after {kind.value}")
        return ack

    def _refresh(self, sequence: int, acks: tuple[AuditAppendAcknowledgement, ...] = ()) -> None:
        snapshot, risk = self.gate.ledger.snapshot, self.gate.risk_authority.risk_state
        refresh = self.gate.risk_refresh_authority.create_refresh(
            snapshot=snapshot,
            risk_state=risk,
            dispatch_sequence=sequence,
            ordered_ledger_ack_frontier_sha256=ordered_digest_tuple(
                ORDERED_LEDGER_ACK_DIGEST_DOMAIN,
                tuple(audit_append_acknowledgement_digest(ack) for ack in acks),
            ),
            coordinator_running=self.runtime.phase is StreamPhase.RUNNING,
            publication_window_clear=True,
            candidate_matches_internal=True,
        )
        self._append(
            AuditRecordKind.RISK_PORTFOLIO_REFRESH, canonical_portfolio_risk_refresh_bytes(refresh)
        )
        self.gate.frontier.advance(snapshot=snapshot, risk_state=risk, refresh=refresh)
        self.completed_sequence = sequence

    def _guard(self, order: Order, root: MarketDataEnvelope, sequence: int) -> bool:
        self.runtime.verify_active_market_dispatch(root, dispatch_sequence=sequence)
        if self.stop_requested() or self.runtime.phase is not StreamPhase.RUNNING:
            self.runtime.request_stop()
            return False
        if not 0 <= (self.clock.now() - root.event_time).total_seconds() <= self.max_age:
            raise ValueError("Paper pre-effect market is stale")
        snapshot, risk = self.gate.ledger.snapshot, self.gate.risk_authority.risk_state
        if (
            risk.halted
            or self.facts.halt_requested
            or snapshot.open_reconciliation_refs
            or snapshot != self.gate.frontier.current_snapshot()
            or risk != self.gate.frontier.current_state()
            or order.portfolio_snapshot_version != snapshot.snapshot_version
            or order.risk_state_version != risk.risk_state_version
        ):
            raise ValueError("Paper pre-effect portfolio/risk frontier conflicts")
        if order.side is OrderSide.BUY:
            notional = settle_product(
                self.price_bound,
                order.quantity,
                self.spec.contract_multiplier,
                self.spec.currency_quantum,
            ).amount
            fee = commission_amount(
                self.price_bound,
                order.quantity,
                self.spec.contract_multiplier,
                commission_bps_from_identity(
                    self.scenario.execution_policy.identifier.value,
                    self.scenario.execution_policy.sha256.value,
                )
                or CanonicalDecimal("0"),
                self.spec.currency_quantum,
            )
            cash = next(
                (
                    b.amount
                    for b in snapshot.cash_balances
                    if b.currency == self.scenario.funding_currency
                ),
                CanonicalDecimal("0"),
            )
            with localcontext() as ctx:
                ctx.prec = 100
                if notional > self.scenario.max_notional or Decimal(notional.text) + Decimal(
                    fee.text
                ) > Decimal(cash.text):
                    raise ValueError("Paper current cash/notional risk limit")
        return True

    def on_market(self, root: MarketDataEnvelope, sequence: int) -> None:
        if root.payload.high > self.raw_price_bound:
            raise ValueError("Paper market exceeds configured simulation price bound")
        self.last_market = root
        self.operations.emit(
            "market.event",
            market_event_id=causal_market_digest(root),
            dispatch_sequence=sequence,
            symbol=self.scenario.instrument.symbol,
        )
        for ingress in self.broker.on_market(root):
            self.runtime.enqueue_fact(ingress)
        if self.kill_switch is not None and self.kill_switch.effective_halted():
            # A halt blocks only *new* outbound decisions; already submitted
            # Orders keep draining their retained facts above. It never cancels,
            # liquidates or mutates the ledger.
            self.runtime.request_stop()
            self._refresh(sequence)
            return
        if (
            self.pending is None
            and len(self.committed) // 2 < self.max_round_trips
            and not self.stop_requested()
        ):
            bar = root.payload
            action = decode_action(
                self.logic.on_bar(
                    StrategyBarV1(
                        root.event_time,
                        root.available_at,
                        *(
                            Decimal(str(value))
                            for value in (bar.open, bar.high, bar.low, bar.close, bar.volume)
                        ),
                    ),
                    PositionViewV2(self.position_state, self.quantity),
                )
            )
            if action.action == "HOLD":
                self.operations.hold(root, sequence)
            else:
                entering = action.action == "ENTER_LONG"
                if (
                    entering != (self.position_state is PositionState.FLAT_INITIAL)
                    or len(self.issued) >= 2 * self.max_round_trips
                ):
                    raise ValueError("illegal Paper long-only action")
                if entering:
                    if action.quantity is None:
                        raise ValueError("Paper entry requires quantity")
                    require_quantized(
                        action.quantity, self.spec.quantity_quantum, field_name="Paper quantity"
                    )
                    if self.scenario.strategy_package is None:
                        if action.quantity != CanonicalDecimal(
                            str(self.scenario.strategy_parameters["target_quantity"])
                        ):
                            raise ValueError("Paper action conflicts with accepted quantity")
                    else:
                        self.planner._rebind_flat_policy(self._portfolio_policy(action.quantity))
                signal = self.signals.issue(
                    root,
                    dispatch_sequence=sequence,
                    direction=SignalDirection.LONG if entering else SignalDirection.FLAT,
                )
                self.operations.strategy(signal)
                intent = self.planner.plan(signal).intent
                if intent is None:
                    raise ValueError("Paper action did not produce intent")
                risk = self.gate.risk_authority.evaluate(intent, self.gate.ledger.snapshot)
                self.operations.risk(signal, risk)
                if risk.decision.kind is not RiskDecisionKind.ALLOW:
                    raise ValueError("Paper risk rejected or resized configured action")
                order = self.orders.create_order(intent, risk)
                if order.side is not (OrderSide.BUY if entering else OrderSide.SELL) or (
                    not entering and order.quantity != self.quantity
                ):
                    raise ValueError("Paper Order conflicts with position")
                self._append(
                    AuditRecordKind.PAPER_ORDER_CONSTRUCTION,
                    canonical_paper_order_construction_payload(
                        order, intent, risk.decision, risk.evidence, sequence
                    ),
                )
                self.operations.order(order, signal)
                if not self._guard(order, root, sequence):
                    self._refresh(sequence)
                    return
                if not self._operational_authorize(order, root, sequence):
                    self._refresh(sequence)
                    return
                self._append(
                    AuditRecordKind.PAPER_SUBMISSION_AUTHORIZATION,
                    canonical_paper_submission_payload(
                        order,
                        root,
                        sequence,
                        portfolio_snapshot_sha256=portfolio_snapshot_digest(
                            self.gate.ledger.snapshot
                        ),
                        risk_state_sha256=risk_state_snapshot_digest(
                            self.gate.risk_authority.risk_state
                        ),
                    ),
                )
                if not self._guard(order, root, sequence):
                    self._refresh(sequence)
                    return
                if not self._operational_authorize(order, root, sequence):
                    self._refresh(sequence)
                    return
                command = self.tracker.begin_submit(order)
                if command is None:
                    raise ValueError("Paper submit attempt was already consumed")
                self.issued.append(order)
                self.pending = order
                submitted_at = self.clock.now()
                try:
                    result = self.broker.submit(command, submitted_at=submitted_at)
                except BaseException:
                    self.tracker.record_submit_result(command, SubmissionAttemptState.UNCERTAIN)
                    self._append(
                        AuditRecordKind.PAPER_SUBMISSION_RESULT,
                        canonical_paper_submission_result_payload(
                            order,
                            sequence,
                            submitted_at=submitted_at,
                            submission_state="uncertain",
                            venue_order_id=None,
                        ),
                    )
                    raise
                self.tracker.record_submit_result(command, result.state)
                self._append(
                    AuditRecordKind.PAPER_SUBMISSION_RESULT,
                    canonical_paper_submission_result_payload(
                        order,
                        sequence,
                        submitted_at=submitted_at,
                        submission_state=result.state.value,
                        venue_order_id=(
                            None if result.venue_order_id is None else result.venue_order_id.value
                        ),
                    ),
                )
                self.operations.emit(
                    "broker.submitted",
                    broker="paper.local",
                    order_id=order.order_id,
                    client_order_id=order.client_submission_key,
                    outcome=result.state.value,
                )
                for ingress in result.ingresses:
                    self.runtime.enqueue_fact(ingress)
                if result.state is not SubmissionAttemptState.SUBMITTED:
                    raise ValueError("Paper transport did not confirm submission")
                self.operational_safety.record_submission(self.monotonic())
        self._refresh(sequence)

    def on_fact(self, ingress: ExecutionFactIngress, sequence: int) -> None:
        dispatch_ack = self._append(
            AuditRecordKind.PAPER_FACT_DISPATCH,
            canonical_paper_fact_dispatch_payload(ingress, sequence, run_id=self.run_id),
        )
        outcome = self.facts.process_ingress(ingress)
        outcome_ack = self._append(
            AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME,
            canonical_execution_fact_processing_outcome_bytes(outcome),
        )
        handoff = create_paper_audited_execution_fact_handoff(
            ingress=ingress,
            outcome=outcome,
            dispatch_acknowledgement=dispatch_ack,
            outcome_acknowledgement=outcome_ack,
        )
        fill = None
        if outcome.fill_id is not None:
            if outcome.fill_sha256 is None:
                raise ValueError("Paper Fill identity is incomplete")
            fill = self.facts.resolve_fill(fill_id=outcome.fill_id, fill_sha256=outcome.fill_sha256)
        ledger_outcome = self.gate.ledger_handoff_authority.apply_handoff(
            handoff=handoff, outcome=outcome, fill=fill
        )
        ledger_ack = self._append(
            AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
            canonical_ledger_handoff_outcome_bytes(ledger_outcome),
        )
        if (
            outcome.halt_requested
            or ledger_outcome.requires_reconciliation
            or ledger_outcome.action is LedgerHandoffAction.FAILED
        ):
            self.gate.risk_authority.engage_halt(
                RiskHaltReason.RECONCILIATION_REQUIRED, ingress.available_at, sequence
            )
        self._refresh(sequence, (ledger_ack,))
        self.operations.emit(
            "execution.fact",
            order_id=outcome.resolved_order_id,
            client_order_id=outcome.client_submission_key,
            fill_id=outcome.fill_id,
            outcome=outcome.outcome_code.value,
            dispatch_sequence=sequence,
            fact_id=outcome.fact_sha256.value,
        )
        if outcome.action is ExecutionFactAction.DUPLICATE:
            self.duplicates += 1
        if fill is not None:
            order = self.pending
            if order is None or (
                fill.order_id != order.order_id
                or fill.quantity != order.quantity
                or fill.side != order.side
            ):
                raise ValueError("Paper Fill conflicts with pending full Order")
            self.committed.append(fill)
            self.quantity = fill.quantity if fill.side is OrderSide.BUY else CanonicalDecimal("0")
            self.position_state = (
                PositionState.LONG_OPEN
                if fill.side is OrderSide.BUY
                else PositionState.FLAT_INITIAL
            )
            self.pending = None
            snapshot = self.gate.frontier.current_snapshot()
            self.operations.emit(
                "execution.fill",
                order_id=fill.order_id,
                fill_id=fill.fill_id,
                client_order_id=fill.client_submission_key,
                quantity=fill.quantity.text,
                price=fill.price.text,
                side=fill.side.value,
            )
            self.operations.emit(
                "portfolio.updated",
                order_id=fill.order_id,
                fill_id=fill.fill_id,
                client_order_id=fill.client_submission_key,
                ledger_sequence=snapshot.ledger_sequence,
                position_quantity=self.quantity.text,
                cash_amount=self.status()["cash"],
                currency=self.scenario.funding_currency.code,
            )
            # Confirm the just-filled local Order through the same query/fact path.
            # Redelivery has a fresh ingress but the retained trade identity.
            query = self.broker.query(order.client_submission_key, observed_at=self.clock.now())
            if query.ingress is not None:
                self.runtime.enqueue_fact(query.ingress)
        if self.gate.risk_authority.risk_state.halted:
            raise ValueError("Paper execution requires reconciliation")

    def status(self) -> dict[str, Any]:
        snapshot = self.gate.frontier.current_snapshot()
        stream = self.runtime.status()
        cash = next(
            (
                b.amount.text
                for b in snapshot.cash_balances
                if b.currency == self.scenario.funding_currency
            ),
            "0",
        )
        quantity = next(
            (
                b.quantity
                for b in snapshot.position_balances
                if b.instrument == self.scenario.instrument
            ),
            CanonicalDecimal("0"),
        )
        price = (
            None
            if self.last_market is None
            else _quantized_historical_close(
                self.last_market.payload.close, side=OrderSide.SELL, specification=self.spec
            )
        )
        with localcontext() as ctx:
            ctx.prec = 100
            value = (
                Decimal("0")
                if price is None or quantity.coefficient == 0
                else Decimal(
                    settle_product(
                        price, quantity, self.spec.contract_multiplier, self.spec.currency_quantum
                    ).amount.text
                )
            )
            equity = _decimal_text(Decimal(cash) + value)
        return {
            "state": "running" if stream.phase is StreamPhase.RUNNING else "stopping",
            "reason": None if stream.reason is None else stream.reason.value,
            "cash": cash,
            "equity": equity,
            "position": quantity.text,
            "currency": self.scenario.funding_currency.code,
            "symbol": self.scenario.instrument.symbol,
            "valuation_price": None if price is None else price.text,
            "valuation_time": None
            if self.last_market is None
            else self.last_market.event_time.isoformat(),
            "orders": len(self.issued),
            "fills": len(self.committed),
            "observed_fills": len(self.facts.fills),
            "duplicate_facts": self.duplicates,
            "completed_round_trips": len(self.committed) // 2,
            "ledger_sequence": snapshot.ledger_sequence,
            "internal_ledger_sequence": self.gate.ledger.snapshot.ledger_sequence,
            "market_events": stream.market_dispatches,
            "fact_events": stream.fact_dispatches,
            "dispatch_sequence": stream.dispatch_sequence,
            "acknowledged_dispatch_sequence": self.completed_sequence,
            "pending_facts": stream.pending_facts,
            "pending_orders": int(self.pending is not None),
            "heartbeat_count": stream.heartbeat_count,
            "incomplete": stream.failed_dispatch_kind is not None,
            "risk_halted": self.gate.risk_authority.risk_state.halted,
            "kill_switch_halted": (
                self.kill_switch.effective_halted() if self.kill_switch is not None else False
            ),
        }

    def reconcile(self) -> str:
        """Reconcile acknowledged economics against funding and retained Fills, locally."""
        replay = create_portfolio_ledger(self.run_id, self.scenario.spec_set)
        replay.apply_initial_funding(self.funding)
        for fill in self.committed:
            if replay.apply_fill(fill).code is not OutcomeCode.LEDGER_APPLIED:
                raise ValueError("Paper retained Fill replay failed")
        snapshot = self.gate.frontier.current_snapshot()
        if portfolio_snapshot_digest(replay.snapshot) != portfolio_snapshot_digest(snapshot):
            raise ValueError("Paper local reconciliation diverged")
        authority = create_phase1_reconciliation_authority(
            run_id=self.run_id, spec_set=self.scenario.spec_set, snapshot_view=lambda: snapshot
        )
        now = self.clock.now()
        for seq, position in ((1, True), (2, False)):
            balances: Any = (
                (
                    PositionReconciliationBalance(
                        self.scenario.instrument,
                        next(
                            (
                                b.quantity
                                for b in replay.snapshot.position_balances
                                if b.instrument == self.scenario.instrument
                            ),
                            CanonicalDecimal("0"),
                        ),
                    ),
                )
                if position
                else tuple(
                    CashReconciliationBalance(b.currency, b.amount)
                    for b in replay.snapshot.cash_balances
                )
            )
            observation = create_reconciliation_observation(
                run_id=self.run_id,
                spec_set=self.scenario.spec_set,
                observation_id=EconomicId(
                    self.run_id, EconomicOwnerKind.RECONCILIATION_OBSERVATION, seq
                ),
                kind=ReconciliationObservationKind.POSITION_SNAPSHOT
                if position
                else ReconciliationObservationKind.CASH_SNAPSHOT,
                source_namespace=SourceNamespace("paper.local.replay"),
                source_sequence=seq,
                occurred_at=now,
                available_at=now,
                watermark_namespace=SourceNamespace("ledger.portfolio"),
                watermark_sequence=snapshot.ledger_sequence,
                declared_scope_kind=ReconciliationScopeKind.POSITION
                if position
                else ReconciliationScopeKind.CASH,
                declared_scope_id=RuntimeIdentifier("paper.local.account"),
                provenance_id=FactProvenanceId("paper.local.replay.v1"),
                provenance_payload_sha256=portfolio_snapshot_digest(replay.snapshot),
                balances=balances,
            )
            outcome = authority.admit_observation(
                observation, dispatch_sequence=max(1, self.completed_sequence)
            )
            self._append(
                AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME,
                canonical_reconciliation_outcome_bytes(outcome),
            )
            self.operations.reconciliation(outcome, position=position)
            if outcome.outcome_code is not OutcomeCode.RECONCILIATION_MATCH:
                raise ValueError("Paper local reconciliation failed")
        return "match"


def restore_paper_trading_session(
    scenario: LoadedBacktestScenario,
    *,
    binding: RunBinding,
    audit: AuditAppendPort,
    records: Iterable[AuditRecord],
    clock: Clock,
    monotonic: Callable[[], float],
    source_id: SourceId,
    prices: tuple[float, ...],
    stop_requested: Callable[[], bool],
    operational_logger: OperationalLogger | None = None,
    max_market_age_seconds: float = 5.0,
    stall_timeout_seconds: float = 5.0,
    kill_switch: OperatorKillSwitchAuthority | None = None,
    operational_limits: OperationalSafetyLimits | None = None,
    crash_after: AuditRecordKind | None = None,
) -> PaperTradingSession:
    """Reconstruct a running Paper session from a reopened journal, then continue.

    Every authority is rebuilt from durable records: the ledger replays the
    deduplicated Fills, the refresh authority and frontier resume the
    acknowledged refresh chain, the Orders are re-issued, the tracker and broker
    retain their submitted history, and the fact authority re-derives its
    dedup/projection indexes by replaying dispatched ingresses. ``RUN_PREPARED``
    is never re-appended, so the reopened journal continues at its next owner
    sequence.
    """
    records = tuple(records)
    if scenario.schema_version not in (4, 5):
        raise ValueError("local Paper requires an accepted Action V2 long-only configuration")
    session = object.__new__(PaperTradingSession)
    session.scenario, session.binding = scenario, binding
    session.audit, session.clock = audit, clock
    session.stop_requested, session.max_age = stop_requested, max_market_age_seconds
    session.kill_switch = kill_switch
    session.monotonic = monotonic
    session._crash_after = crash_after
    session.run_id = binding.reference.run_id
    session.operations = ProductObservation(operational_logger)
    session.spec = scenario.spec_set.require(scenario.instrument)
    session.max_round_trips = (
        json.loads(scenario.canonical_bytes)["strategy"]["max_round_trips"]
        if scenario.schema_version == 5
        else 1
    )
    session.raw_price_bound = max(prices)
    session.price_bound = _quantized_historical_close(
        max(prices),
        side=OrderSide.BUY,
        specification=session.spec,
        slippage_bps=slippage_bps_from_identity(
            scenario.execution_policy.identifier.value, scenario.execution_policy.sha256.value
        ),
    )
    policy = create_phase1_risk_policy(
        policy_id=RiskPolicyId("paper.local.risk.v1"),
        spec_set=scenario.spec_set,
        execution_policy=scenario.execution_policy,
        instrument_limits=(
            InstrumentRiskLimit(
                scenario.instrument, scenario.max_order_quantity, scenario.max_position_quantity
            ),
        ),
    )
    session.funding = InitialFunding(
        session.run_id, scenario.funding_currency, scenario.initial_cash
    )

    economic = recover_paper_economic_state(
        records,
        run_id=session.run_id,
        spec_set=scenario.spec_set,
        execution_policy=scenario.execution_policy,
        risk_policy=policy,
        funding=session.funding,
    )
    session.gate = create_recovered_local_paper_economic_gate(
        run_id=session.run_id,
        spec_set=scenario.spec_set,
        ledger=economic.ledger,
        risk_authority=economic.risk_authority,
        refresh_authority=economic.refresh_authority,
        frontier=economic.frontier,
    )

    session.orders = create_phase1_order_authority(
        run_id=session.run_id,
        spec_set=scenario.spec_set,
        execution_policy=scenario.execution_policy,
        risk_policy=policy,
        risk_result_verifier=economic.risk_authority,
    )
    # Re-issue each Order against the reconstructed risk authority, re-evaluating
    # its intent at the exact ledger snapshot it was originally approved at so
    # ``create_order`` can re-prove the issued result without a hidden repair.
    contexts = recover_paper_order_contexts(
        records,
        spec_set=scenario.spec_set,
        execution_policy=scenario.execution_policy,
        risk_policy=policy,
    )
    evaluation_ledger = create_portfolio_ledger(session.run_id, scenario.spec_set)
    evaluation_ledger.apply_initial_funding(session.funding)
    for index, (intent, _result) in enumerate(contexts):
        risk = economic.risk_authority.evaluate(intent, evaluation_ledger.snapshot)
        session.orders.create_order(intent, risk)
        if index < len(economic.fills):
            evaluation_ledger.apply_fill(economic.fills[index])

    broker_recovery = recover_paper_broker(
        records, orders=session.orders, max_orders=2 * session.max_round_trips
    )
    session.tracker = broker_recovery.tracker
    session.broker = broker_recovery.broker
    session.operational_safety = OperationalSafetyAuthority(
        run_id=session.run_id,
        limits=(
            operational_limits
            if operational_limits is not None
            else session._default_operational_limits()
        ),
    )
    session.operational_safety.seed_submissions(
        session.broker.submission_ages_seconds(now_utc=clock.now()),
        now_monotonic=monotonic(),
    )

    session.runtime = StreamingMarketRuntime(
        run_id=session.run_id,
        spec_set=scenario.spec_set,
        source_id=source_id,
        clock=clock,
        monotonic=monotonic,
        max_market_age_seconds=max_market_age_seconds,
        stall_timeout_seconds=stall_timeout_seconds,
        fact_source=session.broker,
        initial_dispatch_sequence=(
            economic.final_refresh.dispatch_sequence if economic.final_refresh is not None else 0
        ),
    )
    session.facts = recover_phase1_execution_fact_authority_history(
        recover_paper_fact_authority(records, orders=session.orders),
        order_verifier=session.orders,
        dispatch_verifier=session.runtime,
    )
    # Resume the signal/planning sequence frontiers so a continued feed issues
    # fresh Signals, Targets and Intents instead of colliding with the recovered
    # ones. Each durable Order corresponds to exactly one Signal and one Intent.
    signal_count = len(contexts)
    last_new_dispatch_sequence = contexts[-1][0].dispatch_sequence if contexts else None
    session.signals = create_strategy_signal_authority(
        run_id=session.run_id,
        verifier=session.runtime,
        first_signal_sequence=signal_count + 1,
        issuance_count=signal_count,
        last_new_dispatch_sequence=last_new_dispatch_sequence,
    )
    target = (
        CanonicalDecimal(str(scenario.strategy_parameters["target_quantity"]))
        if scenario.strategy_package is None
        else scenario.max_order_quantity
    )
    session.planner = create_portfolio_planning_authority(
        run_id=session.run_id,
        ledger=session.gate.ledger,
        spec_set=scenario.spec_set,
        policy=session._portfolio_policy(target),
        execution_policy=scenario.execution_policy,
        first_target_sequence=signal_count + 1,
        first_intent_sequence=signal_count + 1,
        result_count=signal_count,
        last_new_signal_dispatch_sequence=last_new_dispatch_sequence,
    )
    logic = scenario.strategy_entry.factory(scenario.strategy_parameters)
    if not isinstance(logic, (HoldRootsLogic, LocalActionLogic)):
        raise ValueError("Paper strategy does not implement the Action V2 profile")
    session.logic = logic

    snapshot = economic.ledger.snapshot
    session.quantity = next(
        (
            balance.quantity
            for balance in snapshot.position_balances
            if balance.instrument == scenario.instrument
        ),
        CanonicalDecimal("0"),
    )
    session.position_state = (
        PositionState.LONG_OPEN if session.quantity.coefficient != 0 else PositionState.FLAT_INITIAL
    )
    session.issued = list(session.orders.orders)
    session.committed = list(economic.fills)
    session.pending = broker_recovery.open_orders[0] if broker_recovery.open_orders else None
    session.duplicates = sum(
        1 for outcome in session.facts.outcomes if outcome.action is ExecutionFactAction.DUPLICATE
    )
    session.last_market = None
    session.completed_sequence = (
        economic.final_refresh.dispatch_sequence if economic.final_refresh is not None else 0
    )
    return session
