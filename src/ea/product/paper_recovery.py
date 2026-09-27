"""Restart-visible outbound intent classification and M1 joint acceptance.

PPV-12 core: a process that dies mid-submission must never blindly resend an
ambiguous broker effect. The audit journal already retains durable outbound
intent (``PAPER_SUBMISSION_AUTHORIZATION``, appended before transport) and the
post-transport classification (``PAPER_SUBMISSION_RESULT``) plus dispatched
facts (``PAPER_FACT_DISPATCH``). This module replays those records into one
closed classification per order and one restart admission rule.

PPV-13 core: ``reconcile_paper_recovery`` joins that local scan with a broker
observation (the result of "querying the broker") through
``reconcile_broker_order``, producing one M1 acceptance: recovered-and-safe to
continue, or divergent-and-needs-external-reconciliation. For the local Paper
broker the journal *is* the broker state, so ``recover_paper_broker_observation``
derives the query answer from the same durable records; a real broker adapter
(PPV-17) supplies the observation from an external query instead.

This module never derives economic authority from anything other than durable
evidence: it reads verified journal records and either reports a closed
continuation decision or reconstructs a state that is exactly replayable from
those records (never a hidden repair).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import final

from ea.composition.frontier import (
    AcknowledgedLifecycleFrontier,
    create_acknowledged_lifecycle_frontier,
)
from ea.core.audit import AuditRecord, AuditRecordKind
from ea.core.economics import CanonicalDecimal
from ea.core.execution import InstrumentExecutionSpecSet
from ea.core.execution_identity import EconomicId, EconomicOwnerKind, IngressIdentity
from ea.core.execution_messages import (
    ExecutionFact,
    ExecutionFactIngress,
    ExecutionFactKind,
    ExecutionPolicyRef,
    Fill,
    IndependentFactDecodeContext,
    Order,
    OrderIntent,
    TargetLineageRef,
    _encode_json,
    canonical_execution_fact_bytes,
    canonical_execution_fact_ingress_bytes,
    create_fill,
    decode_execution_fact_ingress,
    decode_order,
    decode_order_intent,
    decode_risk_decision,
)
from ea.core.execution_state import OrderProjectionState
from ea.core.identity import Instrument
from ea.core.ledger_integration import (
    PortfolioRiskRefresh,
    decode_portfolio_risk_refresh,
    portfolio_risk_refresh_digest,
)
from ea.core.outcomes import OutcomeCode
from ea.core.portfolio import InitialFunding, portfolio_snapshot_digest
from ea.core.risk import (
    Phase1RiskPolicy,
    RiskEvaluationResult,
    create_risk_evaluation_result,
    decode_risk_evaluation_evidence,
    phase1_risk_policy_digest,
    risk_state_snapshot_digest,
)
from ea.core.run import RunId, Sha256Digest
from ea.execution import Phase1ExecutionFactAuthority, create_phase1_execution_fact_authority
from ea.execution.authority import Phase1OrderAuthority
from ea.execution.order_lifecycle import OrderCommandTracker, SubmissionAttemptState
from ea.execution.paper_broker import PaperBroker
from ea.portfolio import (
    Phase1PortfolioRiskRefreshAuthority,
    PortfolioLedger,
    create_phase1_portfolio_risk_refresh_authority,
    create_portfolio_ledger,
)
from ea.reconciliation.broker import (
    BrokerObservedOrderState,
    BrokerOrderObservation,
    BrokerReconciliationResult,
    reconcile_broker_order,
)
from ea.risk import Phase1RiskAuthority, create_phase1_risk_authority


class PaperOutboundClassification(StrEnum):
    """Closed restart-visible classification for one outbound submission."""

    NOT_SENT = "not_sent"
    SENT_CONFIRMED = "sent_confirmed"
    DEFINITELY_NOT_SENT = "definitely_not_sent"
    UNKNOWN = "unknown"


class PaperTerminalState(StrEnum):
    """The broker-observed terminal projection retained in dispatched facts."""

    FILLED = "filled"
    CANCELLED = "cancelled"


_TERMINAL_KIND = {
    "trade": PaperTerminalState.FILLED,
    "cancellation": PaperTerminalState.CANCELLED,
}


@final
@dataclass(frozen=True, slots=True)
class PaperOutboundIntent:
    """One durable outbound intent and its restart-visible classification."""

    order_owner_sequence: int
    client_submission_key: Sha256Digest | None
    execution_request_sha256: Sha256Digest | None
    dispatch_sequence: int
    classification: PaperOutboundClassification
    terminal: PaperTerminalState | None
    venue_order_id: str | None

    @property
    def incomplete(self) -> bool:
        """True when reconciliation is required before any new effect."""
        return self.classification is PaperOutboundClassification.UNKNOWN or (
            self.classification is PaperOutboundClassification.SENT_CONFIRMED
            and self.terminal is None
        )


@final
@dataclass(frozen=True, slots=True)
class PaperRecoveryScan:
    """The closed restart admission result for one journal replay."""

    intents: tuple[PaperOutboundIntent, ...]
    incomplete: tuple[PaperOutboundIntent, ...]

    @property
    def reconciliation_required(self) -> bool:
        """True when any incomplete outbound intent must be reconciled first."""
        return bool(self.incomplete)


@final
@dataclass(frozen=True, slots=True)
class PaperJointAcceptance:
    """The closed M1 continuation decision for one recovered attempt."""

    recovered: bool
    recovered_orders: int
    recovered_fills: int
    open_orders: tuple[int, ...]
    not_sent_orders: tuple[int, ...]
    divergent_orders: tuple[int, ...]
    reason: str | None

    @property
    def exactly_once(self) -> bool:
        """True when the recovered fills admit a duplicate-free ledger replay.

        Each order contributes at most one FILLED terminal and the fact
        authority dedups by identity, so a recovered acceptance replays each
        Fill exactly once.
        """
        return self.recovered


@dataclass(slots=True)
class _IntentBuilder:
    order_owner_sequence: int
    dispatch_sequence: int = 0
    client_submission_key: Sha256Digest | None = None
    execution_request_sha256: Sha256Digest | None = None
    submission_state: str | None = None
    venue_order_id: str | None = None
    terminal: PaperTerminalState | None = None


def _economic_id_document(value: object) -> tuple[str, int]:
    if type(value) is not dict:
        raise ValueError("economic id document is not one object")
    if set(value) != {"owner_kind", "owner_sequence", "run_id"}:
        raise ValueError("economic id document fields conflict")
    if type(value["run_id"]) is not str or type(value["owner_sequence"]) is not int:
        raise ValueError("economic id document bindings conflict")
    return value["run_id"], value["owner_sequence"]


def _fact_kind(ingress_document: object) -> str:
    if type(ingress_document) is not dict or type(ingress_document.get("fact")) is not dict:
        raise ValueError("fact ingress document is invalid")
    kind = ingress_document["fact"].get("kind")
    if type(kind) is not str:
        raise ValueError("fact kind is invalid")
    return kind


def _replay_builders(records: Iterable[AuditRecord]) -> dict[tuple[str, int], _IntentBuilder]:
    """Replay verified records into one per-order intent builder."""
    builders: dict[tuple[str, int], _IntentBuilder] = {}
    for record in records:
        if type(record) is not AuditRecord:
            raise ValueError("recovery record stream must contain exact AuditRecord values")
        try:
            document = json.loads(record.canonical_payload)
            if type(document) is not dict:
                raise ValueError("recovery payload is not one object")
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError("recovery payload is malformed JSON") from error
        if record.record_kind is AuditRecordKind.PAPER_SUBMISSION_AUTHORIZATION:
            order = document.get("order")
            if type(order) is not dict:
                raise ValueError("paper submission authorization order is invalid")
            run_id, sequence = _economic_id_document(order["order_id"])
            builder = builders.setdefault((run_id, sequence), _IntentBuilder(sequence))
            builder.dispatch_sequence = document["dispatch_sequence"]
        elif record.record_kind is AuditRecordKind.PAPER_SUBMISSION_RESULT:
            run_id, sequence = _economic_id_document(document["order_id"])
            builder = builders.setdefault((run_id, sequence), _IntentBuilder(sequence))
            builder.dispatch_sequence = document["dispatch_sequence"]
            builder.submission_state = document["submission_state"]
            builder.venue_order_id = document["venue_order_id"]
            try:
                builder.client_submission_key = Sha256Digest(document["client_submission_key"])
                builder.execution_request_sha256 = Sha256Digest(
                    document["execution_request_sha256"]
                )
            except (KeyError, ValueError) as error:
                raise ValueError("submission result identity is invalid") from error
        elif record.record_kind is AuditRecordKind.PAPER_FACT_DISPATCH:
            ingress = document.get("ingress")
            if type(ingress) is not dict or type(ingress.get("fact")) is not dict:
                raise ValueError("paper fact dispatch ingress is invalid")
            fact_document = ingress["fact"]
            run_id, sequence = _economic_id_document(fact_document["order_id"])
            builder = builders.setdefault((run_id, sequence), _IntentBuilder(sequence))
            terminal = _TERMINAL_KIND.get(_fact_kind(ingress))
            if terminal is not None:
                builder.terminal = terminal
    return builders


def _classify(builder: _IntentBuilder) -> PaperOutboundClassification:
    if builder.terminal is not None:
        return PaperOutboundClassification.SENT_CONFIRMED
    if builder.submission_state == "definitely_not_submitted":
        return PaperOutboundClassification.DEFINITELY_NOT_SENT
    if builder.submission_state == "submitted":
        return PaperOutboundClassification.SENT_CONFIRMED
    if builder.submission_state == "uncertain":
        return PaperOutboundClassification.UNKNOWN
    # Durable intent with no result and no dispatched fact: the transport may or
    # may not have happened. Never treat this as a safe retry.
    return PaperOutboundClassification.UNKNOWN


def scan_paper_outbound_intents(
    records: Iterable[AuditRecord],
) -> PaperRecoveryScan:
    """Replay one verified audit record stream into outbound intent classification.

    The record stream must already be verified by its source (a reopened journal
    or a pinned tuple). Duplicate records are idempotent: the last terminal or
    classification evidence wins within a single order identity.
    """
    builders = _replay_builders(records)
    intents: list[PaperOutboundIntent] = []
    for run_id, sequence in sorted(builders, key=lambda key: (key[0], key[1])):
        builder = builders[(run_id, sequence)]
        classification = _classify(builder)
        intents.append(
            PaperOutboundIntent(
                order_owner_sequence=builder.order_owner_sequence,
                client_submission_key=builder.client_submission_key,
                execution_request_sha256=builder.execution_request_sha256,
                dispatch_sequence=builder.dispatch_sequence,
                classification=classification,
                terminal=builder.terminal,
                venue_order_id=builder.venue_order_id,
            )
        )
    incomplete = tuple(intent for intent in intents if intent.incomplete)
    return PaperRecoveryScan(intents=tuple(intents), incomplete=incomplete)


def _broker_observed_state(builder: _IntentBuilder) -> BrokerObservedOrderState:
    if builder.terminal is PaperTerminalState.FILLED:
        return BrokerObservedOrderState.FILLED
    if builder.terminal is PaperTerminalState.CANCELLED:
        return BrokerObservedOrderState.CANCELLED
    if builder.submission_state == "submitted":
        return BrokerObservedOrderState.SUBMITTED
    return BrokerObservedOrderState.UNKNOWN


def recover_paper_broker_observation(
    records: Iterable[AuditRecord],
) -> dict[int, BrokerOrderObservation]:
    """Reconstruct the local Paper broker's query answer from durable records.

    This is the local-Paper "query the broker" step: the journal is the broker's
    state, so the observed order state is derived from the same durable records.
    A real broker adapter (PPV-17) replaces this with an external query.
    """
    builders = _replay_builders(records)
    observations: dict[int, BrokerOrderObservation] = {}
    for (_, sequence), builder in sorted(
        builders.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        key = builder.client_submission_key or Sha256Digest("0" * 64)
        observations[sequence] = BrokerOrderObservation(
            client_submission_key=key,
            observed_state=_broker_observed_state(builder),
            venue_order_id=builder.venue_order_id,
        )
    return observations


def _intent_local_state(intent: PaperOutboundIntent) -> OrderProjectionState | None:
    if intent.terminal is PaperTerminalState.FILLED:
        return OrderProjectionState.FILLED
    if intent.terminal is PaperTerminalState.CANCELLED:
        return OrderProjectionState.CANCELLED
    if intent.classification is PaperOutboundClassification.DEFINITELY_NOT_SENT:
        return OrderProjectionState.DEFINITELY_NOT_SUBMITTED
    if intent.classification is PaperOutboundClassification.SENT_CONFIRMED:
        return OrderProjectionState.SUBMITTED
    return None


def reconcile_paper_recovery(
    scan: PaperRecoveryScan,
    broker_observations: Mapping[int, BrokerOrderObservation],
) -> PaperJointAcceptance:
    """Join the local scan with broker observations into one M1 acceptance.

    Each outbound intent is compared through ``reconcile_broker_order``: a
    MATCH or LOCAL_BEHIND outcome is recoverable (the broker agrees or is ahead,
    so the fill/order is consumed exactly once); BROKER_BEHIND, CONFLICT or
    UNKNOWN marks divergence that requires external reconciliation before
    trading may continue.
    """
    if type(scan) is not PaperRecoveryScan:
        raise ValueError("scan must be an exact PaperRecoveryScan")
    recovered_fills = 0
    open_orders: list[int] = []
    not_sent_orders: list[int] = []
    divergent_orders: list[int] = []
    for intent in scan.intents:
        sequence = intent.order_owner_sequence
        observation = broker_observations.get(sequence)
        if observation is None:
            observation = BrokerOrderObservation(
                Sha256Digest("0" * 64), BrokerObservedOrderState.UNKNOWN, None
            )
        local_state = _intent_local_state(intent)
        reconciliation = reconcile_broker_order(local_state=local_state, observation=observation)
        outcome = reconciliation.result
        if outcome is BrokerReconciliationResult.MATCH:
            if intent.terminal is PaperTerminalState.FILLED:
                recovered_fills += 1
            elif observation.observed_state is BrokerObservedOrderState.SUBMITTED:
                open_orders.append(sequence)
            else:
                not_sent_orders.append(sequence)
        elif outcome is BrokerReconciliationResult.LOCAL_BEHIND:
            # Broker is ahead: it filled or cancelled while local lacked the
            # terminal. Consume exactly once, never synthesize a second effect.
            if observation.observed_state is BrokerObservedOrderState.FILLED:
                recovered_fills += 1
            elif observation.observed_state is BrokerObservedOrderState.SUBMITTED:
                open_orders.append(sequence)
            else:
                not_sent_orders.append(sequence)
        else:
            divergent_orders.append(sequence)
    recovered = not divergent_orders
    reason = (
        None
        if recovered
        else "outbound intent diverges from broker observation; reconcile before continuing"
    )
    return PaperJointAcceptance(
        recovered=recovered,
        recovered_orders=len(scan.intents),
        recovered_fills=recovered_fills,
        open_orders=tuple(open_orders),
        not_sent_orders=tuple(not_sent_orders),
        divergent_orders=tuple(divergent_orders),
        reason=reason,
    )


@final
@dataclass(frozen=True, slots=True)
class PaperContinuationState:
    """The reconstructed economic continuation state for a recovered attempt.

    ``exactly_once`` is true only when the durable trade facts decode to exactly
    one Fill per order and their replay through a fresh ledger reproduces the
    acknowledged economic state without a duplicate effect.
    """

    fills: int
    round_trips: int
    cash_text: str
    position_quantity_text: str
    open_orders: tuple[int, ...]
    exactly_once: bool


def recover_paper_fills(
    records: Iterable[AuditRecord],
    *,
    spec_set: InstrumentExecutionSpecSet,
    funding: InitialFunding,
) -> tuple[Fill, ...]:
    """Decode the deduplicated trade Fills in dispatch order.

    Query redelivery re-emits the retained trade with a fresh ingress but the
    same fact identity; dedup by ``fact_sha256`` so each Order contributes
    exactly one Fill in the exact dispatch order the fact authority created it.
    """
    records = tuple(records)
    fills: list[Fill] = []
    seen_facts: set[Sha256Digest] = set()
    fill_sequence = 1
    for record in records:
        if record.record_kind is not AuditRecordKind.PAPER_FACT_DISPATCH:
            continue
        document = json.loads(record.canonical_payload)
        ingress = decode_execution_fact_ingress(
            _encode_json(document["ingress"]),
            context=IndependentFactDecodeContext(spec_set=spec_set),
        )
        if ingress.fact.kind is not ExecutionFactKind.TRADE:
            continue
        if ingress.fact.fact_sha256 in seen_facts:
            continue
        seen_facts.add(ingress.fact.fact_sha256)
        fills.append(
            create_fill(
                fill_id=EconomicId(
                    funding.run_id, EconomicOwnerKind.EXECUTION_FILL, fill_sequence
                ),
                fact=ingress.fact,
                spec_set=spec_set,
            )
        )
        fill_sequence += 1
    return tuple(fills)


def recover_paper_risk_refreshes(
    records: Iterable[AuditRecord],
) -> tuple[PortfolioRiskRefresh, ...]:
    """Decode the durable portfolio-risk refreshes in journal order and verify chain.

    Each refresh must continue the acknowledged chain: the first names no
    predecessor and every later one names the digest of the refresh before it.
    """
    refreshes: list[PortfolioRiskRefresh] = []
    for record in records:
        if type(record) is not AuditRecord:
            raise ValueError("recovery record stream must contain exact AuditRecord values")
        if record.record_kind is not AuditRecordKind.RISK_PORTFOLIO_REFRESH:
            continue
        refresh = decode_portfolio_risk_refresh(record.canonical_payload)
        if not refreshes:
            if refresh.previous_refresh_sha256 is not None:
                raise ValueError("Paper first refresh must not name a predecessor")
        elif refresh.previous_refresh_sha256 != portfolio_risk_refresh_digest(refreshes[-1]):
            raise ValueError("Paper refresh chain does not continue")
        refreshes.append(refresh)
    return tuple(refreshes)


def recover_paper_continuation(
    records: Iterable[AuditRecord],
    *,
    spec_set: InstrumentExecutionSpecSet,
    funding: InitialFunding,
    instrument: Instrument,
) -> PaperContinuationState:
    """Rebuild the economic continuation state from durable facts.

    Trade facts are decoded in dispatch order, each producing exactly one Fill,
    and the Fills are replayed through a fresh portfolio ledger. The result is
    the acknowledged cash/position/round-trip state that a restarted runtime
    must adopt before issuing any new decision.
    """
    records = tuple(records)
    scan = scan_paper_outbound_intents(records)
    joint = reconcile_paper_recovery(scan, recover_paper_broker_observation(records))
    if not joint.recovered:
        return PaperContinuationState(
            fills=0,
            round_trips=0,
            cash_text=funding.amount.text,
            position_quantity_text="0",
            open_orders=(),
            exactly_once=False,
        )
    fills = recover_paper_fills(records, spec_set=spec_set, funding=funding)
    replay = create_portfolio_ledger(funding.run_id, spec_set)
    replay.apply_initial_funding(funding)
    for fill in fills:
        if replay.apply_fill(fill).code is not OutcomeCode.LEDGER_APPLIED:
            raise ValueError("Paper continuation fill replay failed")
    snapshot = replay.snapshot
    cash = next(
        (
            balance.amount
            for balance in snapshot.cash_balances
            if balance.currency == funding.currency
        ),
        CanonicalDecimal("0"),
    )
    position = next(
        (
            balance.quantity
            for balance in snapshot.position_balances
            if balance.instrument == instrument
        ),
        CanonicalDecimal("0"),
    )
    return PaperContinuationState(
        fills=len(fills),
        round_trips=len(fills) // 2,
        cash_text=cash.text,
        position_quantity_text=position.text,
        open_orders=joint.open_orders,
        exactly_once=len(fills) == joint.recovered_fills,
    )


def _parse_target_lineage(document: object) -> TargetLineageRef:
    if type(document) is not dict or set(document) != {"target_id", "target_sha256"}:
        raise ValueError("target lineage document is invalid")
    target_id = document["target_id"]
    if type(target_id) is not dict or set(target_id) != {"owner_kind", "owner_sequence", "run_id"}:
        raise ValueError("target id document is invalid")
    try:
        return TargetLineageRef(
            EconomicId(
                RunId(target_id["run_id"]),
                EconomicOwnerKind(target_id["owner_kind"]),
                target_id["owner_sequence"],
            ),
            Sha256Digest(document["target_sha256"]),
        )
    except (ValueError, TypeError) as error:
        raise ValueError("target lineage is invalid") from error


def recover_paper_orders(
    records: Iterable[AuditRecord],
    *,
    spec_set: InstrumentExecutionSpecSet,
    execution_policy: ExecutionPolicyRef,
) -> tuple[Order, ...]:
    """Reconstruct issued Orders from the durable order-construction context.

    Each construction record carries the approved Order, its OrderIntent and its
    RiskDecision. The intent's own target lineage supplies the re-proof context,
    so a restarted runtime rebuilds the exact issued Orders without any hidden
    state repair. Orders are returned in the journal's recording order.
    """
    orders: list[Order] = []
    for record in records:
        if type(record) is not AuditRecord:
            raise ValueError("recovery record stream must contain exact AuditRecord values")
        if record.record_kind is not AuditRecordKind.PAPER_ORDER_CONSTRUCTION:
            continue
        document = json.loads(record.canonical_payload)
        intent_document = document["intent"]
        target_lineage = _parse_target_lineage(intent_document["target_lineage"])
        intent = decode_order_intent(
            _encode_json(intent_document),
            spec_set=spec_set,
            target_lineage=target_lineage,
            execution_policy=execution_policy,
        )
        decision = decode_risk_decision(
            _encode_json(document["decision"]),
            intent=intent,
            spec_set=spec_set,
        )
        order = decode_order(
            _encode_json(document["order"]),
            intent=intent,
            decision=decision,
            spec_set=spec_set,
        )
        orders.append(order)
    return tuple(orders)


def recover_paper_order_contexts(
    records: Iterable[AuditRecord],
    *,
    spec_set: InstrumentExecutionSpecSet,
    execution_policy: ExecutionPolicyRef,
    risk_policy: Phase1RiskPolicy,
) -> tuple[tuple[OrderIntent, RiskEvaluationResult], ...]:
    """Reconstruct re-issuable order-construction contexts from the journal.

    Returns ``(OrderIntent, RiskEvaluationResult)`` pairs in recording order so
    a fresh ``Phase1OrderAuthority`` can re-issue the exact Orders via
    ``create_order`` without any hidden state repair.
    """
    contexts: list[tuple[OrderIntent, RiskEvaluationResult]] = []
    for record in records:
        if type(record) is not AuditRecord:
            raise ValueError("recovery record stream must contain exact AuditRecord values")
        if record.record_kind is not AuditRecordKind.PAPER_ORDER_CONSTRUCTION:
            continue
        document = json.loads(record.canonical_payload)
        intent_document = document["intent"]
        intent = decode_order_intent(
            _encode_json(intent_document),
            spec_set=spec_set,
            target_lineage=_parse_target_lineage(intent_document["target_lineage"]),
            execution_policy=execution_policy,
        )
        decision = decode_risk_decision(
            _encode_json(document["decision"]),
            intent=intent,
            spec_set=spec_set,
        )
        evidence = decode_risk_evaluation_evidence(
            _encode_json(document["evidence"]),
            decision=decision,
            policy=risk_policy,
        )
        contexts.append((intent, create_risk_evaluation_result(decision, evidence)))
    return tuple(contexts)


@final
@dataclass(frozen=True, slots=True)
class PaperBrokerRecovery:
    """Reconstructed outbound command and broker state over one fresh authority.

    ``tracker`` and ``broker`` are bound to the same issued-Order authority the
    caller re-issued the Orders into. ``open_orders`` are Orders the broker still
    retains as SUBMITTED (continuable), ``filled_orders`` are pinned FILLED so a
    resumed feed never matches them twice, and ``not_sent_orders`` carry no
    retained broker record because transport either never happened or is
    unresolved.
    """

    tracker: OrderCommandTracker
    broker: PaperBroker
    open_orders: tuple[Order, ...]
    filled_orders: tuple[Order, ...]
    not_sent_orders: tuple[Order, ...]


def recover_paper_broker(
    records: Iterable[AuditRecord],
    *,
    orders: Phase1OrderAuthority,
    max_orders: int = 1024,
) -> PaperBrokerRecovery:
    """Rebuild the OrderCommandTracker and PaperBroker from durable records.

    The caller must already re-issue the reconstructed Orders into ``orders``
    (see ``recover_paper_order_contexts`` + ``create_order``), so
    ``orders.orders`` carries the exact issued Orders in recording order. Each
    durable ``PAPER_SUBMISSION_RESULT`` supplies the original ``submitted_at``
    and classification; ``submit`` rebuilds the broker's retained request
    history and ``restore_filled`` pins already-filled Orders so a resumed feed
    never matches them a second time. Orders with no submission result were
    authorized but never submitted and are left out of both authorities.
    """
    records = tuple(records)
    if type(orders) is not Phase1OrderAuthority:
        raise ValueError("recovered broker requires an exact Phase1OrderAuthority")
    issued = {order.order_id.owner_sequence: order for order in orders.orders}
    submission: dict[int, tuple[datetime, SubmissionAttemptState]] = {}
    terminal_trades: dict[int, ExecutionFact] = {}
    seen_facts: set[Sha256Digest] = set()
    for record in records:
        if type(record) is not AuditRecord:
            raise ValueError("recovery record stream must contain exact AuditRecord values")
        if record.record_kind is AuditRecordKind.PAPER_SUBMISSION_RESULT:
            document = json.loads(record.canonical_payload)
            _, sequence = _economic_id_document(document["order_id"])
            submitted_at = datetime.strptime(
                document["submitted_at"], "%Y-%m-%dT%H:%M:%S.%fZ"
            ).replace(tzinfo=UTC)
            submission[sequence] = (
                submitted_at,
                SubmissionAttemptState(document["submission_state"]),
            )
        elif record.record_kind is AuditRecordKind.PAPER_FACT_DISPATCH:
            document = json.loads(record.canonical_payload)
            ingress = decode_execution_fact_ingress(
                _encode_json(document["ingress"]),
                context=IndependentFactDecodeContext(spec_set=orders.spec_set),
            )
            if ingress.fact.kind is not ExecutionFactKind.TRADE:
                continue
            # Query redelivery re-emits the retained trade with a fresh ingress
            # but the same fact identity; dedup so each terminal is pinned once.
            if ingress.fact.fact_sha256 in seen_facts:
                continue
            seen_facts.add(ingress.fact.fact_sha256)
            if ingress.fact.order_id is not None:
                terminal_trades[ingress.fact.order_id.owner_sequence] = ingress.fact
    tracker = OrderCommandTracker(orders)
    broker = PaperBroker(orders, max_orders=max_orders)
    open_orders: list[Order] = []
    filled_orders: list[Order] = []
    not_sent_orders: list[Order] = []
    for sequence in sorted(issued):
        order = issued[sequence]
        if sequence not in submission:
            # Constructed and authorized but never submitted: no outbound
            # tracker state and no retained broker history.
            continue
        submitted_at, state = submission[sequence]
        command = tracker.begin_submit(order)
        if command is None:
            raise ValueError("Paper recovered Order was already submitted")
        tracker.record_submit_result(command, state)
        if state is not SubmissionAttemptState.SUBMITTED:
            not_sent_orders.append(order)
            continue
        result = broker.submit(command, submitted_at=submitted_at)
        if result.state is not SubmissionAttemptState.SUBMITTED:
            raise ValueError("Paper recovered submission did not rebuild the broker record")
        trade = terminal_trades.get(sequence)
        if trade is not None:
            broker.restore_filled(command, trade_fact=trade)
            filled_orders.append(order)
        else:
            open_orders.append(order)
    return PaperBrokerRecovery(
        tracker=tracker,
        broker=broker,
        open_orders=tuple(open_orders),
        filled_orders=tuple(filled_orders),
        not_sent_orders=tuple(not_sent_orders),
    )


@final
class _RecoveryFactDispatchVerifier:
    """Statically resolves each recovered ingress to its durable dispatch sequence."""

    def __init__(
        self,
        *,
        run_id: RunId,
        spec_set: InstrumentExecutionSpecSet,
        dispatch_by_identity: Mapping[IngressIdentity, tuple[bytes, bytes, int]],
    ) -> None:
        self._run_id = run_id
        self._spec_set = spec_set
        self._dispatch = dispatch_by_identity

    @property
    def run_id(self) -> RunId:
        return self._run_id

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet:
        return self._spec_set

    def resolve_active_issued_fact_dispatch(
        self,
        *,
        ingress_identity: IngressIdentity,
        canonical_ingress_bytes: bytes,
        canonical_fact_bytes: bytes,
    ) -> int | None:
        entry = self._dispatch.get(ingress_identity)
        if entry is None or entry[:2] != (canonical_ingress_bytes, canonical_fact_bytes):
            return None
        return entry[2]


def recover_paper_fact_authority(
    records: Iterable[AuditRecord],
    *,
    orders: Phase1OrderAuthority,
) -> Phase1ExecutionFactAuthority:
    """Replay durable fact dispatches through a fresh fact authority.

    The reconstructed authority re-derives the exact Fills, projections and
    dedup indexes from the journal by processing each ``PAPER_FACT_DISPATCH``
    ingress in dispatch order through ``process_ingress``. A static recovery
    dispatch verifier supplies the durable dispatch sequence each ingress was
    originally issued with, so replay needs no live runtime lease.
    """
    records = tuple(records)
    if type(orders) is not Phase1OrderAuthority:
        raise ValueError("recovered fact authority requires an exact Phase1OrderAuthority")
    dispatch_by_identity: dict[IngressIdentity, tuple[bytes, bytes, int]] = {}
    ingress_order: list[ExecutionFactIngress] = []
    for record in records:
        if type(record) is not AuditRecord:
            raise ValueError("recovery record stream must contain exact AuditRecord values")
        if record.record_kind is not AuditRecordKind.PAPER_FACT_DISPATCH:
            continue
        document = json.loads(record.canonical_payload)
        ingress = decode_execution_fact_ingress(
            _encode_json(document["ingress"]),
            context=IndependentFactDecodeContext(spec_set=orders.spec_set),
        )
        dispatch_by_identity[ingress.identity] = (
            canonical_execution_fact_ingress_bytes(ingress),
            canonical_execution_fact_bytes(ingress.fact),
            document["dispatch_sequence"],
        )
        ingress_order.append(ingress)
    verifier = _RecoveryFactDispatchVerifier(
        run_id=orders.run_id,
        spec_set=orders.spec_set,
        dispatch_by_identity=dispatch_by_identity,
    )
    authority = create_phase1_execution_fact_authority(
        run_id=orders.run_id,
        spec_set=orders.spec_set,
        order_verifier=orders,
        dispatch_verifier=verifier,
    )
    for ingress in ingress_order:
        authority.process_ingress(ingress)
    return authority


@final
@dataclass(frozen=True, slots=True)
class PaperEconomicRecovery:
    """Reconstructed acknowledged ledger, risk, refresh and frontier state."""

    ledger: PortfolioLedger
    risk_authority: Phase1RiskAuthority
    refresh_authority: Phase1PortfolioRiskRefreshAuthority
    frontier: AcknowledgedLifecycleFrontier
    fills: tuple[Fill, ...]
    final_refresh: PortfolioRiskRefresh | None


def recover_paper_economic_state(
    records: Iterable[AuditRecord],
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    execution_policy: ExecutionPolicyRef,
    risk_policy: Phase1RiskPolicy,
    funding: InitialFunding,
) -> PaperEconomicRecovery:
    """Reconstruct the acknowledged ledger, risk, refresh and frontier state.

    The ledger is rebuilt by replaying the deduplicated Fills over initial
    funding; the risk authority is reconstructed fresh (the recovered run never
    halted before the crash); the refresh authority and frontier resume the
    acknowledged refresh chain from the last durable refresh, so the next
    ``_refresh`` continues it exactly once.
    """
    records = tuple(records)
    fills = recover_paper_fills(records, spec_set=spec_set, funding=funding)
    refreshes = recover_paper_risk_refreshes(records)
    ledger = create_portfolio_ledger(run_id, spec_set)
    ledger.apply_initial_funding(funding)
    for fill in fills:
        if ledger.apply_fill(fill).code is not OutcomeCode.LEDGER_APPLIED:
            raise ValueError("Paper economic fill replay failed")
    risk_authority = create_phase1_risk_authority(
        run_id=run_id,
        spec_set=spec_set,
        execution_policy=execution_policy,
        policy=risk_policy,
    )
    final_refresh = refreshes[-1] if refreshes else None
    if final_refresh is not None:
        if portfolio_snapshot_digest(ledger.snapshot) != final_refresh.portfolio_snapshot_sha256:
            raise ValueError("Paper economic ledger diverges from the refresh frontier")
        if risk_state_snapshot_digest(risk_authority.risk_state) != final_refresh.risk_state_sha256:
            raise ValueError("Paper economic risk state diverges from the refresh frontier")
    refresh_authority = create_phase1_portfolio_risk_refresh_authority(
        run_id=run_id,
        spec_set=spec_set,
        policy_id=risk_policy.policy_id,
        policy_sha256=phase1_risk_policy_digest(risk_policy),
        first_sequence=(final_refresh.refresh_sequence + 1) if final_refresh is not None else 1,
        first_previous_refresh_sha256=(
            portfolio_risk_refresh_digest(final_refresh) if final_refresh is not None else None
        ),
        retain_history=False,
    )
    frontier = create_acknowledged_lifecycle_frontier(
        initial_snapshot=ledger.snapshot,
        initial_risk_state=risk_authority.risk_state,
        initial_predecessor_sha256=(
            portfolio_risk_refresh_digest(final_refresh) if final_refresh is not None else None
        ),
    )
    return PaperEconomicRecovery(
        ledger=ledger,
        risk_authority=risk_authority,
        refresh_authority=refresh_authority,
        frontier=frontier,
        fills=fills,
        final_refresh=final_refresh,
    )
