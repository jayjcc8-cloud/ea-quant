from __future__ import annotations

import json
import struct
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from ea.core.economics import CanonicalDecimal
from ea.core.execution import (
    InstrumentExecutionSpec,
    InstrumentExecutionSpecSet,
    InstrumentSpecId,
    InstrumentSpecSetId,
    PriceDomain,
    SettlementCurrency,
    build_instrument_spec_set,
    instrument_spec_set_digest,
)
from ea.core.execution_identity import (
    EconomicId,
    EconomicOwnerKind,
    SourceNamespace,
)
from ea.core.execution_messages import (
    ExecutionPolicyId,
    ExecutionPolicyRef,
    FactProvenanceId,
    Order,
    OrderSide,
    TargetLineageRef,
    canonical_execution_fact_bytes,
    canonical_execution_fact_ingress_bytes,
    decode_order,
    decode_order_intent,
    decode_risk_decision,
    execution_fact_ingress_digest,
    execution_request_digest,
    order_digest,
)
from ea.core.historical_matching import (
    HistoricalMatcherDecodeContext,
    HistoricalMatcherError,
    HistoricalSubmissionAuthorizationProof,
    _create_historical_submission_authorization_proof,
    canonical_end_of_run_root_bytes,
    canonical_historical_matcher_dispatch_batch_bytes,
    canonical_historical_matcher_state_bytes,
    canonical_historical_submission_receipt_bytes,
    decode_historical_matcher_dispatch_batch,
    decode_historical_matcher_state,
    decode_historical_submission_receipt,
    historical_end_root_digest,
    historical_market_root_digest,
    historical_matcher_dispatch_batch_digest,
    historical_submission_receipt_digest,
    runtime_root_key_document,
    runtime_root_key_from_document,
)
from ea.core.identity import Instrument, VenueId
from ea.core.market_data import Adjustment, Bar, MarketDataEnvelope, SourceId
from ea.core.market_data_codec import canonical_market_data_record_bytes
from ea.core.outcomes import OutcomeCode
from ea.core.run import ReplayWindow, RunId, Sha256Digest
from ea.core.runtime import (
    EndOfRunKind,
    EndOfRunRoot,
    _create_active_end_of_run_dispatch_proof,
    _create_active_market_dispatch_proof,
    prepare_bounded_runtime_roots,
)
from ea.core.strategy import _causal_market_digest_from_canonical_bytes
from ea.data import (
    create_phase1_historical_market_data_source,
    create_phase1_historical_market_source_bridge,
    decode_phase1_ohlcv_csv,
)
from ea.execution.matcher import (
    Phase1HistoricalMatcher,
    _quantized_close,
    create_phase1_historical_matcher,
)
from ea.runtime import (
    create_causal_descendant_fact_dispatch_verifier,
    create_deterministic_root_queue,
    create_historical_matcher_dispatch_verifier,
    create_phase1_historical_market_runtime,
)

FIXTURE_PATH = (
    Path(__file__).parents[2] / "docs" / "fixtures" / "adr0018-historical-matcher-v1.json"
)


def _time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _float_bits(value: str) -> float:
    return cast(float, struct.unpack(">d", bytes.fromhex(value))[0])


def _market(payload: bytes) -> MarketDataEnvelope:
    value = json.loads(payload)
    instrument = Instrument(VenueId(value["venue"]), value["symbol"])
    return MarketDataEnvelope(
        payload=Bar(
            instrument=instrument,
            interval_start=_time(value["interval_start"]),
            interval_end=_time(value["interval_end"]),
            adjustment=Adjustment(value["adjustment"]),
            open=_float_bits(value["open_bits"]),
            high=_float_bits(value["high_bits"]),
            low=_float_bits(value["low_bits"]),
            close=_float_bits(value["close_bits"]),
            volume=_float_bits(value["volume_bits"]),
        ),
        source=SourceId(value["source"]),
        available_at=_time(value["available_at"]),
        source_sequence=value["source_sequence"],
        revision=value["revision"],
    )


def _end(payload: bytes) -> EndOfRunRoot:
    value = json.loads(payload)
    return EndOfRunRoot(
        available_at=_time(value["available_at"]),
        kind=EndOfRunKind(value["kind"]),
        producer_namespace=SourceNamespace(value["producer_namespace"]),
        producer_sequence=value["producer_sequence"],
        run_id=RunId(value["run_id"]),
    )


class _OrderVerifier:
    def __init__(
        self,
        run_id: RunId,
        spec_set: InstrumentExecutionSpecSet,
        policy: ExecutionPolicyRef,
        orders: list[Order],
    ) -> None:
        self.run_id = run_id
        self.spec_set = spec_set
        self.execution_policy = policy
        self._orders = {order.order_id: order for order in orders}

    def resolve_issued_order_by_id(self, order_id: EconomicId) -> Order | None:
        return self._orders.get(order_id)


class _DispatchVerifier:
    def __init__(self, run_id: RunId, spec_set: InstrumentExecutionSpecSet) -> None:
        self._run_id = run_id
        self.run_id = run_id
        self.spec_set = spec_set

    def verify_active_market_dispatch(
        self,
        market_root: MarketDataEnvelope,
        *,
        dispatch_sequence: int,
    ) -> Any:
        market_bytes = canonical_market_data_record_bytes(market_root)
        return _create_active_market_dispatch_proof(
            run_id=self._run_id,
            market_root=market_root,
            canonical_market_bytes=market_bytes,
            causal_market_sha256=_causal_market_digest_from_canonical_bytes(market_bytes),
            dispatch_sequence=dispatch_sequence,
            issuer=self,
        )

    def verify_active_end_of_run_dispatch(
        self,
        end_root: EndOfRunRoot,
        *,
        dispatch_sequence: int,
    ) -> Any:
        return _create_active_end_of_run_dispatch_proof(
            run_id=self._run_id,
            end_root=end_root,
            canonical_end_bytes=canonical_end_of_run_root_bytes(end_root),
            end_root_sha256=historical_end_root_digest(end_root),
            dispatch_sequence=dispatch_sequence,
            issuer=self,
        )


class _AuthorizationVerifier:
    def __init__(
        self,
        run_id: RunId,
        spec_set: InstrumentExecutionSpecSet,
        policy: ExecutionPolicyRef,
        receipt_documents: list[dict[str, Any]],
        orders: list[Order],
    ) -> None:
        self.run_id = run_id
        self.instrument_spec_set_id = spec_set.identifier
        self.instrument_spec_set_sha256 = instrument_spec_set_digest(spec_set)
        self.execution_policy = policy
        self._spec_set = spec_set
        self._receipts = {
            order.order_id: receipt
            for order, receipt in zip(orders, receipt_documents, strict=True)
        }
        self._orders = {order.order_id: order for order in orders}
        self.calls = 0

    def verify_authorized_historical_submission(
        self,
        *,
        order_id: EconomicId,
        canonical_order_bytes: bytes,
        canonical_execution_request_bytes: bytes,
        canonical_causal_market_bytes: bytes,
        causal_market_sha256: Sha256Digest,
        causal_root_key: Any,
        dispatch_sequence: int,
    ) -> HistoricalSubmissionAuthorizationProof:
        self.calls += 1
        del (
            canonical_order_bytes,
            canonical_execution_request_bytes,
            canonical_causal_market_bytes,
        )
        order = self._orders[order_id]
        receipt = self._receipts[order_id]
        return _create_historical_submission_authorization_proof(
            run_id=self.run_id,
            spec_set=self._spec_set,
            execution_policy=self.execution_policy,
            order=order,
            order_sha256=order_digest(order),
            execution_request_sha256=execution_request_digest(order),
            causal_market_sha256=causal_market_sha256,
            causal_root_key=causal_root_key,
            dispatch_sequence=dispatch_sequence,
            audit_acknowledgement_id=receipt["audit_acknowledgement_id"],
            audit_acknowledgement_sha256=Sha256Digest(receipt["audit_acknowledgement_sha256"]),
            global_halt_epoch=receipt["global_halt_epoch"],
            risk_halt_epoch=receipt["risk_halt_epoch"],
            instrument_gate_id=receipt["instrument_gate_id"],
            instrument_gate_version=receipt["instrument_gate_version"],
            held_for_order_id=order_id,
            authorization_state_version=receipt["authorization_state_version"],
            issuer=self,
        )


def _system() -> tuple[
    dict[str, Any],
    Phase1HistoricalMatcher,
    list[Order],
    MarketDataEnvelope,
    MarketDataEnvelope,
    EndOfRunRoot,
]:
    fixture = json.loads(FIXTURE_PATH.read_text())
    context = fixture["context"]
    run_id = RunId("12345678-1234-4234-8234-123456789abc")
    instrument = Instrument(VenueId("XNAS"), "AAPL")
    spec_set = build_instrument_spec_set(
        InstrumentSpecSetId("phase1.us-equities.v1"),
        (
            InstrumentExecutionSpec(
                instrument=instrument,
                specification_id=InstrumentSpecId("xnas.aapl.v1"),
                price_quantum=CanonicalDecimal("0.01"),
                quantity_quantum=CanonicalDecimal("1"),
                settlement_currency=SettlementCurrency("USD"),
                currency_quantum=CanonicalDecimal("0.01"),
                contract_multiplier=CanonicalDecimal("1"),
                price_domain=PriceDomain.POSITIVE,
            ),
        ),
    )
    policy = ExecutionPolicyRef(
        ExecutionPolicyId("phase1.next-bar-close.v1"),
        Sha256Digest(context["execution_policy_source"]["digest_sha256"]),
    )
    orders = []
    for prefix, target_sequence in (("trade", 2), ("expiry", 12)):
        target = TargetLineageRef(
            EconomicId(
                run_id,
                EconomicOwnerKind.PORTFOLIO_TARGET,
                target_sequence,
            ),
            Sha256Digest(
                json.loads(context[f"{prefix}_intent"]["canonical_utf8"])["target_lineage"][
                    "target_sha256"
                ]
            ),
        )
        intent = decode_order_intent(
            context[f"{prefix}_intent"]["canonical_utf8"].encode(),
            spec_set=spec_set,
            target_lineage=target,
            execution_policy=policy,
        )
        decision = decode_risk_decision(
            context[f"{prefix}_risk_decision"]["canonical_utf8"].encode(),
            intent=intent,
            spec_set=spec_set,
        )
        orders.append(
            decode_order(
                context[f"{prefix}_order"]["canonical_utf8"].encode(),
                intent=intent,
                decision=decision,
                spec_set=spec_set,
            )
        )
    causal = _market(context["causal_market_root"]["canonical_utf8"].encode())
    delayed = _market(context["delayed_trade_market_root"]["canonical_utf8"].encode())
    end = _end(context["end_root"]["canonical_utf8"].encode())
    receipts = [
        json.loads(fixture["artifacts"][name]["canonical_utf8"])
        for name in ("trade_receipt", "expiry_receipt")
    ]
    matcher = create_phase1_historical_matcher(
        run_id=run_id,
        spec_set=spec_set,
        execution_policy=policy,
        source_namespace=SourceNamespace("phase1.historical-matcher.v1"),
        provenance_id=FactProvenanceId(fixture["bound_provenance_id"]),
        order_issuance_verifier=_OrderVerifier(run_id, spec_set, policy, orders),
        submission_authorization_verifier=_AuthorizationVerifier(
            run_id,
            spec_set,
            policy,
            receipts,
            orders,
        ),
        active_dispatch_verifier=_DispatchVerifier(run_id, spec_set),
    )
    return fixture, matcher, orders, causal, delayed, end


def test_normative_trade_and_bounded_end_fixture() -> None:
    fixture, matcher, orders, causal, delayed, end = _system()

    trade_receipt = matcher.submit(
        orders[0],
        causal_market_root=causal,
        dispatch_sequence=7,
    )
    trade_batch = matcher.match_active_market_root(
        delayed,
        dispatch_sequence=8,
    )
    expiry_receipt = matcher.submit(
        orders[1],
        causal_market_root=delayed,
        dispatch_sequence=8,
    )
    end_batch = matcher.expire_at_active_end(end, dispatch_sequence=9)

    expected = fixture["artifacts"]
    assert (
        canonical_historical_submission_receipt_bytes(trade_receipt).decode()
        == (expected["trade_receipt"]["canonical_utf8"])
    )
    assert (
        canonical_historical_matcher_dispatch_batch_bytes(trade_batch).decode()
        == (expected["market_batch"]["canonical_utf8"])
    )
    assert (
        canonical_execution_fact_bytes(trade_batch.ingresses[0].fact).decode()
        == (expected["trade_fact"]["canonical_utf8"])
    )
    assert (
        canonical_execution_fact_ingress_bytes(trade_batch.ingresses[0]).decode()
        == (expected["trade_ingress"]["canonical_utf8"])
    )
    assert (
        canonical_historical_submission_receipt_bytes(expiry_receipt).decode()
        == (expected["expiry_receipt"]["canonical_utf8"])
    )
    assert (
        canonical_historical_matcher_dispatch_batch_bytes(end_batch).decode()
        == (expected["end_batch"]["canonical_utf8"])
    )
    assert (
        canonical_execution_fact_bytes(end_batch.ingresses[0].fact).decode()
        == (expected["expiry_fact"]["canonical_utf8"])
    )
    assert (
        canonical_execution_fact_ingress_bytes(end_batch.ingresses[0]).decode()
        == (expected["expiry_ingress"]["canonical_utf8"])
    )


def test_exact_replay_returns_retained_objects_without_live_ports() -> None:
    _, matcher, orders, causal, delayed, end = _system()
    receipt = matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    batch = matcher.match_active_market_root(delayed, dispatch_sequence=8)
    matcher.submit(orders[1], causal_market_root=delayed, dispatch_sequence=8)
    terminal = matcher.expire_at_active_end(end, dispatch_sequence=9)

    assert matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7) is receipt
    assert matcher.match_active_market_root(delayed, dispatch_sequence=8) is batch
    assert matcher.expire_at_active_end(end, dispatch_sequence=9) is terminal
    assert (
        historical_market_root_digest(causal).value
        == (json.loads(FIXTURE_PATH.read_text())["context"]["causal_market_root"]["digest_sha256"])
    )


def test_matcher_canonical_values_decode_with_closed_context() -> None:
    _, matcher, orders, causal, delayed, end = _system()
    receipts = [matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)]
    batches = [matcher.match_active_market_root(delayed, dispatch_sequence=8)]
    receipts.append(matcher.submit(orders[1], causal_market_root=delayed, dispatch_sequence=8))
    batches.append(matcher.expire_at_active_end(end, dispatch_sequence=9))
    context = HistoricalMatcherDecodeContext(
        run_id=matcher.run_id,
        spec_set=matcher.spec_set,
        execution_policy=matcher.execution_policy,
        source_namespace=matcher.source_namespace,
        provenance_id=matcher.provenance_id,
        orders_by_sha256={order_digest(order): order for order in orders},
        receipts_by_sha256={
            historical_submission_receipt_digest(receipt): receipt for receipt in receipts
        },
        batches_by_sha256={
            historical_matcher_dispatch_batch_digest(batch): batch for batch in batches
        },
        ingresses_by_sha256={
            execution_fact_ingress_digest(ingress): ingress
            for batch in batches
            for ingress in batch.ingresses
        },
    )
    for receipt in receipts:
        payload = canonical_historical_submission_receipt_bytes(receipt)
        assert decode_historical_submission_receipt(payload, context=context) == receipt
    for batch in batches:
        payload = canonical_historical_matcher_dispatch_batch_bytes(batch)
        assert decode_historical_matcher_dispatch_batch(payload, context=context) == batch
    state = matcher.state
    assert (
        decode_historical_matcher_state(
            canonical_historical_matcher_state_bytes(state),
            context=context,
        )
        == state
    )


def test_runtime_adapter_mints_market_and_terminal_proofs_only_while_active() -> None:
    _, matcher, _, _, _, _ = _system()
    header = (
        "schema_version,venue,symbol,interval_start,interval_end,adjustment,"
        "open,high,low,close,volume,source,source_sequence,revision,available_at"
    )
    row = (
        "1,XNAS,AAPL,2026-01-02T09:30:00.000000Z,"
        "2026-01-02T09:31:00.000000Z,raw,100.0,101.0,99.0,100.5,10.0,"
        "fixture,1,0,2026-01-02T09:31:05.000000Z"
    )
    dataset = decode_phase1_ohlcv_csv(
        f"{header}\n{row}\n".encode(),
        replay_window=ReplayWindow(
            _time("2026-01-02T09:00:00.000000Z"),
            _time("2026-01-02T10:00:00.000000Z"),
        ),
    )
    runtime = create_phase1_historical_market_runtime(
        run_id=matcher.run_id,
        spec_set=matcher.spec_set,
        source=create_phase1_historical_market_source_bridge(
            create_phase1_historical_market_data_source(dataset)
        ),
    )
    verifier = create_historical_matcher_dispatch_verifier(runtime)
    market_lease = runtime.pop()
    market = cast(MarketDataEnvelope, market_lease.root)
    market_proof = verifier.verify_active_market_dispatch(
        market,
        dispatch_sequence=market_lease.dispatch_sequence,
    )
    assert market_proof.market_root is market
    runtime.acknowledge(market_lease)
    end_lease = runtime.pop()
    terminal = cast(EndOfRunRoot, end_lease.root)
    end_proof = verifier.verify_active_end_of_run_dispatch(
        terminal,
        dispatch_sequence=end_lease.dispatch_sequence,
    )
    assert end_proof.end_root is terminal
    assert end_proof.end_root_sha256 == historical_end_root_digest(terminal)


def test_adverse_half_tick_is_buy_up_and_sell_down() -> None:
    _, matcher, _, _, _, _ = _system()
    specification = matcher.spec_set.specifications[0]
    half_tick_specification = InstrumentExecutionSpec(
        instrument=specification.instrument,
        specification_id=specification.specification_id,
        price_quantum=CanonicalDecimal("0.5"),
        quantity_quantum=specification.quantity_quantum,
        settlement_currency=specification.settlement_currency,
        currency_quantum=specification.currency_quantum,
        contract_multiplier=specification.contract_multiplier,
        price_domain=specification.price_domain,
    )
    assert (
        _quantized_close(
            1.25,
            side=OrderSide.BUY,
            specification=half_tick_specification,
        ).text
        == "1.5"
    )
    assert (
        _quantized_close(
            1.25,
            side=OrderSide.SELL,
            specification=half_tick_specification,
        ).text
        == "1"
    )


def test_late_initial_bar_without_later_event_time_cannot_fill() -> None:
    _, matcher, orders, causal, delayed, _ = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    late_same_event = MarketDataEnvelope(
        payload=causal.payload,
        source=SourceId("fixture.late"),
        available_at=_time("2026-01-02T09:31:06.000000Z"),
        source_sequence=3,
        revision=0,
    )
    empty = matcher.match_active_market_root(
        late_same_event,
        dispatch_sequence=8,
    )
    assert empty.ingresses == ()
    assert matcher.state.pending_order_ids == (orders[0].order_id,)

    matched = matcher.match_active_market_root(delayed, dispatch_sequence=9)
    assert len(matched.ingresses) == 1
    assert len(matcher.state.pending_order_ids) == 0


def test_descendant_fact_is_dispatchable_only_while_parent_root_is_active() -> None:
    fixture, baseline, orders, causal, _, _ = _system()
    header = (
        "schema_version,venue,symbol,interval_start,interval_end,adjustment,"
        "open,high,low,close,volume,source,source_sequence,revision,available_at"
    )
    rows = []
    for minute in range(24, 30):
        rows.append(
            "1,XNAS,AAPL,2026-01-02T09:"
            f"{minute:02d}:00.000000Z,2026-01-02T09:{minute + 1:02d}:00.000000Z,"
            "raw,100.0,101.0,99.0,100.5,10.0,warmup,"
            f"{minute - 24},0,2026-01-02T09:{minute + 1:02d}:00.000000Z"
        )
    rows.extend(
        (
            "1,XNAS,AAPL,2026-01-02T09:30:00.000000Z,"
            "2026-01-02T09:31:00.000000Z,raw,100.0,101.5,99.0,101.25,"
            "1000.0,fixture,1,0,2026-01-02T09:31:05.000000Z",
            "1,XNAS,AAPL,2026-01-02T09:31:00.000000Z,"
            "2026-01-02T09:32:00.000000Z,raw,100.0,101.5,99.0,101.25,"
            "1000.0,fixture,2,0,2026-01-02T09:32:07.000000Z",
        )
    )
    dataset = decode_phase1_ohlcv_csv(
        ("\n".join((header, *rows)) + "\n").encode(),
        replay_window=ReplayWindow(
            _time("2026-01-02T09:00:00.000000Z"),
            _time("2026-01-02T10:00:00.000000Z"),
        ),
    )
    runtime = create_phase1_historical_market_runtime(
        run_id=baseline.run_id,
        spec_set=baseline.spec_set,
        source=create_phase1_historical_market_source_bridge(
            create_phase1_historical_market_data_source(dataset)
        ),
    )
    for _ in range(6):
        warmup = runtime.pop()
        runtime.acknowledge(warmup)
    causal_lease = runtime.pop()
    assert canonical_market_data_record_bytes(cast(MarketDataEnvelope, causal_lease.root)) == (
        canonical_market_data_record_bytes(causal)
    )
    receipt_documents = [
        json.loads(fixture["artifacts"][name]["canonical_utf8"])
        for name in ("trade_receipt", "expiry_receipt")
    ]
    matcher = create_phase1_historical_matcher(
        run_id=baseline.run_id,
        spec_set=baseline.spec_set,
        execution_policy=baseline.execution_policy,
        source_namespace=baseline.source_namespace,
        provenance_id=baseline.provenance_id,
        order_issuance_verifier=_OrderVerifier(
            baseline.run_id,
            baseline.spec_set,
            baseline.execution_policy,
            orders,
        ),
        submission_authorization_verifier=_AuthorizationVerifier(
            baseline.run_id,
            baseline.spec_set,
            baseline.execution_policy,
            receipt_documents,
            orders,
        ),
        active_dispatch_verifier=create_historical_matcher_dispatch_verifier(runtime),
    )
    matcher.submit(
        orders[0],
        causal_market_root=cast(MarketDataEnvelope, causal_lease.root),
        dispatch_sequence=causal_lease.dispatch_sequence,
    )
    runtime.acknowledge(causal_lease)
    match_lease = runtime.pop()
    batch = matcher.match_active_market_root(
        cast(MarketDataEnvelope, match_lease.root),
        dispatch_sequence=match_lease.dispatch_sequence,
    )
    ingress = batch.ingresses[0]
    ingress_bytes = canonical_execution_fact_ingress_bytes(ingress)
    fact_bytes = canonical_execution_fact_bytes(ingress.fact)
    direct_queue = create_deterministic_root_queue(
        run_id=baseline.run_id,
        spec_set=baseline.spec_set,
        plan=prepare_bounded_runtime_roots((causal,)),
        fact_issuance_verifiers=(),
    )
    descendant = create_causal_descendant_fact_dispatch_verifier(
        runtime=runtime,
        direct_dispatch_verifier=direct_queue,
        matcher=matcher,
    )
    assert (
        descendant.resolve_active_issued_fact_dispatch(
            ingress_identity=ingress.identity,
            canonical_ingress_bytes=ingress_bytes,
            canonical_fact_bytes=fact_bytes,
        )
        == match_lease.dispatch_sequence
    )
    runtime.acknowledge(match_lease)
    assert (
        descendant.resolve_active_issued_fact_dispatch(
            ingress_identity=ingress.identity,
            canonical_ingress_bytes=ingress_bytes,
            canonical_fact_bytes=fact_bytes,
        )
        is None
    )


def test_price_quantization_rejects_invalid_carriers_and_price_domain() -> None:
    _, matcher, _, _, _, _ = _system()
    specification = matcher.spec_set.specifications[0]
    with pytest.raises(HistoricalMatcherError) as wrong_type:
        _quantized_close(1, side=OrderSide.BUY, specification=specification)
    assert wrong_type.value.code is OutcomeCode.INVALID_TYPE
    with pytest.raises(HistoricalMatcherError) as non_finite:
        _quantized_close(float("inf"), side=OrderSide.BUY, specification=specification)
    assert non_finite.value.code is OutcomeCode.OUT_OF_RANGE
    with pytest.raises(HistoricalMatcherError) as forbidden_zero:
        _quantized_close(0.0, side=OrderSide.BUY, specification=specification)
    assert forbidden_zero.value.code is OutcomeCode.PRICE_DOMAIN

    non_negative = InstrumentExecutionSpec(
        instrument=specification.instrument,
        specification_id=specification.specification_id,
        price_quantum=specification.price_quantum,
        quantity_quantum=specification.quantity_quantum,
        settlement_currency=specification.settlement_currency,
        currency_quantum=specification.currency_quantum,
        contract_multiplier=specification.contract_multiplier,
        price_domain=PriceDomain.NON_NEGATIVE,
    )
    assert (
        _quantized_close(
            -0.0,
            side=OrderSide.SELL,
            specification=non_negative,
        ).text
        == "0"
    )


def test_non_monotone_dispatch_publishes_first_conflict_and_halts_submission() -> None:
    _, matcher, orders, causal, delayed, _ = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    matcher.match_active_market_root(delayed, dispatch_sequence=8)
    with pytest.raises(HistoricalMatcherError) as conflict:
        matcher.match_active_market_root(causal, dispatch_sequence=7)
    assert conflict.value.code is OutcomeCode.CONFLICTING_ID
    assert matcher.state.halted
    assert matcher.state.conflict is not None
    with pytest.raises(HistoricalMatcherError) as halted:
        matcher.submit(
            orders[1],
            causal_market_root=delayed,
            dispatch_sequence=8,
        )
    assert halted.value.code is OutcomeCode.SUBMISSION_BLOCKED_BY_HALT


def test_submission_exhaustion_skips_authorization_but_retained_replay_survives() -> None:
    _, matcher, orders, causal, delayed, _ = _system()
    authorization = cast(
        _AuthorizationVerifier,
        matcher._submission_authorization_verifier,
    )
    receipt = matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    assert authorization.calls == 1

    matcher._state = replace(matcher._state, next_submission=None)
    assert matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7) is receipt
    assert authorization.calls == 1

    with pytest.raises(HistoricalMatcherError) as exhausted:
        matcher.submit(orders[1], causal_market_root=delayed, dispatch_sequence=8)
    assert exhausted.value.code is OutcomeCode.ARITHMETIC_OVERFLOW
    assert authorization.calls == 1


def test_receipt_decoder_rejects_noncanonical_unknown_and_substituted_context() -> None:
    _, matcher, orders, causal, _, _ = _system()
    receipt = matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    context = HistoricalMatcherDecodeContext(
        run_id=matcher.run_id,
        spec_set=matcher.spec_set,
        execution_policy=matcher.execution_policy,
        source_namespace=matcher.source_namespace,
        provenance_id=matcher.provenance_id,
        orders_by_sha256={order_digest(orders[0]): orders[0]},
    )
    with pytest.raises(HistoricalMatcherError) as invalid_json:
        decode_historical_submission_receipt(b"{", context=context)
    assert invalid_json.value.code is OutcomeCode.INVALID_TYPE

    document = json.loads(canonical_historical_submission_receipt_bytes(receipt))
    document["unknown"] = None
    unknown = json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    with pytest.raises(HistoricalMatcherError) as unknown_field:
        decode_historical_submission_receipt(unknown, context=context)
    assert unknown_field.value.code is OutcomeCode.OUT_OF_RANGE

    foreign = HistoricalMatcherDecodeContext(
        run_id=RunId("87654321-4321-4234-8234-cba987654321"),
        spec_set=context.spec_set,
        execution_policy=context.execution_policy,
        source_namespace=context.source_namespace,
        provenance_id=context.provenance_id,
        orders_by_sha256=context.orders_by_sha256,
    )
    with pytest.raises(HistoricalMatcherError) as substitution:
        decode_historical_submission_receipt(
            canonical_historical_submission_receipt_bytes(receipt),
            context=foreign,
        )
    assert substitution.value.code is OutcomeCode.CONFLICTING_ID


def test_root_key_decoder_rejects_open_or_malformed_documents() -> None:
    _, _, _, causal, _, end = _system()
    with pytest.raises(HistoricalMatcherError) as wrong_carrier:
        runtime_root_key_from_document(())
    assert wrong_carrier.value.code is OutcomeCode.INVALID_TYPE

    market = runtime_root_key_document(causal)
    malformed_market_documents = (
        ({**market, "unknown": None}, OutcomeCode.OUT_OF_RANGE),
        ({**market, "instrument": "XNAS/AAPL"}, OutcomeCode.INVALID_TYPE),
        ({**market, "source_sequence": "1"}, OutcomeCode.INVALID_TYPE),
        ({**market, "revision": -1}, OutcomeCode.CONFLICTING_ID),
        ({**market, "adjustment": 1}, OutcomeCode.INVALID_TYPE),
        (
            {
                **market,
                "instrument": {
                    **cast(dict[str, object], market["instrument"]),
                    "symbol": 1,
                },
            },
            OutcomeCode.INVALID_TYPE,
        ),
        ({**market, "available_at": "not-a-time"}, OutcomeCode.OUT_OF_RANGE),
        ({**market, "root_domain": "future"}, OutcomeCode.OUT_OF_RANGE),
    )
    for document, code in malformed_market_documents:
        with pytest.raises(HistoricalMatcherError) as rejected:
            runtime_root_key_from_document(document)
        assert rejected.value.code is code

    terminal = runtime_root_key_document(end)
    malformed_terminal_documents = (
        ({**terminal, "unknown": None}, OutcomeCode.OUT_OF_RANGE),
        ({**terminal, "producer_namespace": 1}, OutcomeCode.INVALID_TYPE),
        ({**terminal, "producer_sequence": "1"}, OutcomeCode.INVALID_TYPE),
        ({**terminal, "kind": "future"}, OutcomeCode.OUT_OF_RANGE),
        ({**terminal, "producer_sequence": -1}, OutcomeCode.CONFLICTING_ID),
        ({**terminal, "run_id": "not-a-run-id"}, OutcomeCode.OUT_OF_RANGE),
    )
    for document, code in malformed_terminal_documents:
        with pytest.raises(HistoricalMatcherError) as rejected:
            runtime_root_key_from_document(document)
        assert rejected.value.code is code
