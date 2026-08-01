from __future__ import annotations

import json
import struct
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
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
    SourceNativeSequence,
)
from ea.core.execution_messages import (
    ExecutionPolicyId,
    ExecutionPolicyRef,
    FactProvenance,
    FactProvenanceId,
    Order,
    OrderSide,
    TargetLineageRef,
    canonical_execution_fact_bytes,
    canonical_execution_fact_ingress_bytes,
    create_execution_fact_ingress,
    create_trade_execution_fact,
    decode_order,
    decode_order_intent,
    decode_risk_decision,
    execution_fact_digest,
    execution_fact_ingress_digest,
    execution_request_digest,
    order_client_submission_key,
    order_digest,
)
from ea.core.historical_matching import (
    HistoricalDispatchKind,
    HistoricalMatcherConflictKind,
    HistoricalMatcherDecodeContext,
    HistoricalMatcherError,
    HistoricalPreEffectAuthorizationError,
    HistoricalSubmissionAuthorizationProof,
    _create_historical_matcher_conflict,
    _create_historical_matcher_descendant_binding,
    _create_historical_matcher_dispatch_batch,
    _create_historical_submission_authorization_proof,
    _validate_descendant_binding,
    canonical_end_of_run_root_bytes,
    canonical_historical_matcher_dispatch_batch_bytes,
    canonical_historical_matcher_observation_bytes,
    canonical_historical_matcher_state_bytes,
    canonical_historical_submission_receipt_bytes,
    decode_historical_matcher_dispatch_batch,
    decode_historical_matcher_state,
    decode_historical_submission_receipt,
    historical_end_root_digest,
    historical_market_root_digest,
    historical_matcher_dispatch_batch_digest,
    historical_matcher_observation_digest,
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
    RuntimeOrderingError,
    _create_active_end_of_run_dispatch_proof,
    _create_active_market_dispatch_proof,
    prepare_bounded_runtime_roots,
    runtime_root_order_key,
)
from ea.core.strategy import _causal_market_digest_from_canonical_bytes
from ea.data import (
    create_phase1_historical_market_data_source,
    create_phase1_historical_market_source_bridge,
    decode_phase1_ohlcv_csv,
)
from ea.execution.matcher import (
    Phase1HistoricalMatcher,
    _advance,
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


def _canonical_document(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


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


def _clone_order(order: Order, **changes: object) -> Order:
    value = object.__new__(Order)
    for name in Order.__dataclass_fields__:
        object.__setattr__(value, name, changes.get(name, getattr(order, name)))
    return value


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


def test_batch_state_decoders_reject_uint64_and_cross_field_substitutions() -> None:
    _, matcher, orders, causal, delayed, end = _system()
    receipts = [matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)]
    batches = [matcher.match_active_market_root(delayed, dispatch_sequence=8)]
    receipts.append(matcher.submit(orders[1], causal_market_root=delayed, dispatch_sequence=8))
    batches.append(matcher.expire_at_active_end(end, dispatch_sequence=9))
    state = matcher.state
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

    batch_document = json.loads(canonical_historical_matcher_dispatch_batch_bytes(batches[0]))
    batch_mutations = (
        {**batch_document, "dispatch_sequence": 1 << 64},
        {**batch_document, "next_fact_sequence_after": 3},
        {**batch_document, "dispatch_kind": "end_of_run"},
        {**batch_document, "submission_sequences": [2]},
        {
            **batch_document,
            "order_ids": [
                json.loads(canonical_historical_submission_receipt_bytes(receipts[1]))["order_id"]
            ],
        },
    )
    for malformed in batch_mutations:
        with pytest.raises(HistoricalMatcherError):
            decode_historical_matcher_dispatch_batch(
                _canonical_document(malformed),
                context=context,
            )

    state_document = json.loads(canonical_historical_matcher_state_bytes(state))
    state_mutations = (
        {**state_document, "next_submission_sequence": 4},
        {**state_document, "next_fact_sequence": 4},
        {**state_document, "last_new_dispatch_sequence": 8},
        {
            **state_document,
            "dispatch_batch_sha256s": list(reversed(state_document["dispatch_batch_sha256s"])),
        },
        {**state_document, "ended": False},
        {**state_document, "pending_order_ids": [batch_document["order_ids"][0]]},
    )
    for malformed in state_mutations:
        with pytest.raises(HistoricalMatcherError):
            decode_historical_matcher_state(
                _canonical_document(malformed),
                context=context,
            )

    with pytest.raises(HistoricalMatcherError) as bad_registry:
        HistoricalMatcherDecodeContext(
            run_id=matcher.run_id,
            spec_set=matcher.spec_set,
            execution_policy=matcher.execution_policy,
            source_namespace=matcher.source_namespace,
            provenance_id=matcher.provenance_id,
            orders_by_sha256={Sha256Digest("0" * 64): orders[0]},
        )
    assert bad_registry.value.code is OutcomeCode.CONFLICTING_ID


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
    object.__setattr__(
        matcher,
        "_source_namespace",
        SourceNamespace("phase1.historical-matcher.drifted"),
    )
    with pytest.raises(RuntimeOrderingError) as drifted:
        descendant.resolve_active_issued_fact_dispatch(
            ingress_identity=ingress.identity,
            canonical_ingress_bytes=ingress_bytes,
            canonical_fact_bytes=fact_bytes,
        )
    assert drifted.value.code is OutcomeCode.CONFLICTING_ID


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
    exhausted_state = matcher._state
    assert matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7) is receipt
    assert authorization.calls == 1
    assert matcher._state is exhausted_state

    with pytest.raises(HistoricalMatcherError) as exhausted:
        matcher.submit(orders[1], causal_market_root=delayed, dispatch_sequence=8)
    assert exhausted.value.code is OutcomeCode.ARITHMETIC_OVERFLOW
    assert authorization.calls == 1
    assert matcher._state is exhausted_state


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
    unknown = _canonical_document(document)
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

    canonical_document = json.loads(canonical_historical_submission_receipt_bytes(receipt))
    for field, value in (
        ("dispatch_sequence", 1 << 64),
        ("submission_sequence", 0),
        ("instrument_gate_version", 0),
        ("authorization_state_version", 1 << 64),
        ("quantity", "0"),
        ("eligible_after_available_at", "2026-01-02T09:31:04.000000Z"),
    ):
        malformed = {**canonical_document, field: value}
        with pytest.raises(HistoricalMatcherError):
            decode_historical_submission_receipt(
                _canonical_document(malformed),
                context=context,
            )


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
        ({**market, "revision": 1 << 64}, OutcomeCode.CONFLICTING_ID),
        ({**market, "source_sequence": 1 << 64}, OutcomeCode.CONFLICTING_ID),
        ({**market, "event_time": market["interval_start"]}, OutcomeCode.CONFLICTING_ID),
        ({**market, "available_at": market["interval_start"]}, OutcomeCode.CONFLICTING_ID),
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
        ({**terminal, "domain_rank": True}, OutcomeCode.INVALID_TYPE),
        ({**terminal, "kind_rank": True}, OutcomeCode.INVALID_TYPE),
        ({**terminal, "kind": "future"}, OutcomeCode.OUT_OF_RANGE),
        ({**terminal, "producer_sequence": -1}, OutcomeCode.CONFLICTING_ID),
        ({**terminal, "producer_sequence": 1 << 64}, OutcomeCode.CONFLICTING_ID),
        ({**terminal, "run_id": "not-a-run-id"}, OutcomeCode.OUT_OF_RANGE),
    )
    for document, code in malformed_terminal_documents:
        with pytest.raises(HistoricalMatcherError) as rejected:
            runtime_root_key_from_document(document)
        assert rejected.value.code is code


def test_observation_encoder_rejects_root_fact_and_expiry_time_cross_bindings() -> None:
    _, matcher, orders, causal, _, end = _system()
    receipt = matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    common = {
        "fact_sequence": 1,
        "source_namespace": matcher.source_namespace,
        "provenance_id": matcher.provenance_id,
        "submission_receipt_sha256": historical_submission_receipt_digest(receipt),
        "order_sha256": order_digest(orders[0]),
        "trigger_dispatch_sequence": 8,
        "instrument": orders[0].instrument,
        "side": orders[0].side,
        "quantity_text": orders[0].quantity.text,
        "spec_set": matcher.spec_set,
        "execution_policy": matcher.execution_policy,
    }
    invalid = (
        {
            **common,
            "fact_kind": "trade",
            "trigger_root_kind": HistoricalDispatchKind.END_OF_RUN,
            "trigger_root_sha256": historical_end_root_digest(end),
            "trigger_root_key": runtime_root_order_key(end),
            "occurred_at": end.available_at,
            "available_at": end.available_at,
            "price_text": "1",
            "expiry_outcome_code": None,
        },
        {
            **common,
            "fact_kind": "expiry",
            "trigger_root_kind": HistoricalDispatchKind.MARKET,
            "trigger_root_sha256": historical_market_root_digest(causal),
            "trigger_root_key": runtime_root_order_key(causal),
            "occurred_at": causal.event_time,
            "available_at": causal.available_at,
            "price_text": None,
            "expiry_outcome_code": OutcomeCode.ORDER_EXPIRED_NO_ELIGIBLE_MARKET_DATA,
        },
        {
            **common,
            "quantity_text": "1.5",
            "fact_kind": "trade",
            "trigger_root_kind": HistoricalDispatchKind.MARKET,
            "trigger_root_sha256": historical_market_root_digest(causal),
            "trigger_root_key": runtime_root_order_key(causal),
            "occurred_at": causal.event_time,
            "available_at": causal.available_at,
            "price_text": "101.25",
            "expiry_outcome_code": None,
        },
        {
            **common,
            "fact_kind": "trade",
            "trigger_root_kind": HistoricalDispatchKind.MARKET,
            "trigger_root_sha256": historical_market_root_digest(causal),
            "trigger_root_key": runtime_root_order_key(causal),
            "occurred_at": causal.event_time,
            "available_at": causal.available_at,
            "price_text": "101.251",
            "expiry_outcome_code": None,
        },
        {
            **common,
            "fact_kind": "trade",
            "trigger_root_kind": HistoricalDispatchKind.MARKET,
            "trigger_root_sha256": historical_end_root_digest(end),
            "trigger_root_key": runtime_root_order_key(end),
            "occurred_at": end.available_at,
            "available_at": end.available_at,
            "price_text": "1",
            "expiry_outcome_code": None,
        },
        {
            **common,
            "fact_kind": "expiry",
            "trigger_root_kind": HistoricalDispatchKind.END_OF_RUN,
            "trigger_root_sha256": historical_end_root_digest(end),
            "trigger_root_key": runtime_root_order_key(end),
            "occurred_at": causal.available_at,
            "available_at": end.available_at,
            "price_text": None,
            "expiry_outcome_code": OutcomeCode.ORDER_EXPIRED_NO_ELIGIBLE_MARKET_DATA,
        },
    )
    for values in invalid:
        with pytest.raises(HistoricalMatcherError):
            canonical_historical_matcher_observation_bytes(**cast(Any, values))


def test_public_encoders_deep_validate_nested_root_and_instrument_values() -> None:
    _, matcher, orders, causal, delayed, _ = _system()
    receipt = matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    object.__setattr__(receipt.instrument.venue, "code", "not valid")
    with pytest.raises(HistoricalMatcherError):
        canonical_historical_submission_receipt_bytes(receipt)

    _, matcher, orders, causal, delayed, _ = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    batch = matcher.match_active_market_root(delayed, dispatch_sequence=8)
    object.__setattr__(batch.trigger_root_key._suffix, "source_sequence", True)
    with pytest.raises(HistoricalMatcherError):
        canonical_historical_matcher_dispatch_batch_bytes(batch)

    binding = matcher._state.issued[0].binding
    object.__setattr__(binding.parent_root_key._suffix, "kind_rank", True)
    with pytest.raises(HistoricalMatcherError):
        _validate_descendant_binding(binding)


def test_conflict_evidence_uses_one_exact_tagged_identity_union() -> None:
    _, matcher, orders, _, _, _ = _system()
    common = {
        "run_id": matcher.run_id,
        "existing_sha256": None,
        "submitted_sha256": None,
        "submitted_dispatch_sequence": None,
        "last_successful_dispatch_sequence": None,
        "pending_count": 0,
        "next_submission_sequence": 1,
        "next_fact_sequence": 1,
        "trigger_root_sha256": None,
    }
    valid = {
        HistoricalMatcherConflictKind.SUBMISSION_IDENTITY: {
            "kind": "order_id",
            "order_id": {
                "owner_kind": orders[0].order_id.owner_kind.value,
                "owner_sequence": orders[0].order_id.owner_sequence,
                "run_id": orders[0].order_id.run_id.value,
            },
        },
        HistoricalMatcherConflictKind.CLIENT_SUBMISSION_KEY: {
            "kind": "client_submission_key",
            "sha256": "0" * 64,
        },
        HistoricalMatcherConflictKind.DISPATCH_IDENTITY: {
            "dispatch_sequence": 1,
            "kind": "dispatch_sequence",
        },
        HistoricalMatcherConflictKind.NON_MONOTONE_DISPATCH: {
            "dispatch_sequence": 1,
            "kind": "dispatch_sequence",
        },
        HistoricalMatcherConflictKind.RETAINED_BINDING_DRIFT: None,
    }
    for kind, occupied in valid.items():
        conflict = _create_historical_matcher_conflict(
            **common,
            conflict_kind=kind,
            occupied_identity=occupied,
        )
        assert conflict.conflict_kind is kind

    invalid: tuple[tuple[HistoricalMatcherConflictKind, dict[str, object]], ...] = (
        (HistoricalMatcherConflictKind.SUBMISSION_IDENTITY, {"kind": "order_id"}),
        (
            HistoricalMatcherConflictKind.CLIENT_SUBMISSION_KEY,
            {"kind": "client_submission_key", "sha256": "A" * 64},
        ),
        (
            HistoricalMatcherConflictKind.DISPATCH_IDENTITY,
            {"kind": "dispatch_sequence", "dispatch_sequence": 0},
        ),
        (
            HistoricalMatcherConflictKind.NON_MONOTONE_DISPATCH,
            {"kind": "future", "dispatch_sequence": 1},
        ),
        (HistoricalMatcherConflictKind.RETAINED_BINDING_DRIFT, {}),
    )
    for kind, occupied in invalid:
        with pytest.raises(HistoricalMatcherError):
            _create_historical_matcher_conflict(
                **common,
                conflict_kind=kind,
                occupied_identity=occupied,
            )


def test_unexpected_port_exceptions_escape_unchanged_and_publish_nothing() -> None:
    def boom(*_args: object, **_kwargs: object) -> Any:
        raise LookupError("sealed-port-failure")

    for port_name, method_name, operation in (
        (
            "_order_issuance_verifier",
            "resolve_issued_order_by_id",
            lambda matcher, order, causal, delayed, end: matcher.submit(
                order, causal_market_root=causal, dispatch_sequence=7
            ),
        ),
        (
            "_submission_authorization_verifier",
            "verify_authorized_historical_submission",
            lambda matcher, order, causal, delayed, end: matcher.submit(
                order, causal_market_root=causal, dispatch_sequence=7
            ),
        ),
        (
            "_active_dispatch_verifier",
            "verify_active_market_dispatch",
            lambda matcher, order, causal, delayed, end: matcher.match_active_market_root(
                delayed, dispatch_sequence=8
            ),
        ),
        (
            "_active_dispatch_verifier",
            "verify_active_end_of_run_dispatch",
            lambda matcher, order, causal, delayed, end: matcher.expire_at_active_end(
                end, dispatch_sequence=9
            ),
        ),
    ):
        _, matcher, orders, causal, delayed, end = _system()
        port = getattr(matcher, port_name)
        setattr(port, method_name, boom)
        state = matcher._state
        with pytest.raises(LookupError, match="sealed-port-failure"):
            cast(Callable[..., object], operation)(
                matcher,
                orders[0],
                causal,
                delayed,
                end,
            )
        assert matcher._state is state


def test_fabricated_market_proof_causal_digest_is_independently_rejected() -> None:
    _, matcher, _, _, delayed, _ = _system()
    verifier = cast(_DispatchVerifier, matcher._active_dispatch_verifier)

    def forged(
        market_root: MarketDataEnvelope,
        *,
        dispatch_sequence: int,
    ) -> Any:
        payload = canonical_market_data_record_bytes(market_root)
        return _create_active_market_dispatch_proof(
            run_id=matcher.run_id,
            market_root=market_root,
            canonical_market_bytes=payload,
            causal_market_sha256=Sha256Digest("0" * 64),
            dispatch_sequence=dispatch_sequence,
            issuer=verifier,
        )

    cast(Any, verifier).verify_active_market_dispatch = forged
    with pytest.raises(HistoricalMatcherError) as rejected:
        matcher.match_active_market_root(delayed, dispatch_sequence=8)
    assert rejected.value.code is OutcomeCode.CONFLICTING_ID
    assert matcher._state.dispatch_by_sequence == {}


def test_cross_verifier_proof_and_non_exact_port_results_are_rejected_atomically() -> None:
    _, matcher, _, _, delayed, _ = _system()
    verifier = cast(_DispatchVerifier, matcher._active_dispatch_verifier)
    foreign = _DispatchVerifier(matcher.run_id, matcher.spec_set)

    def cross_verifier(
        market_root: MarketDataEnvelope,
        *,
        dispatch_sequence: int,
    ) -> Any:
        payload = canonical_market_data_record_bytes(market_root)
        return _create_active_market_dispatch_proof(
            run_id=matcher.run_id,
            market_root=market_root,
            canonical_market_bytes=payload,
            causal_market_sha256=_causal_market_digest_from_canonical_bytes(payload),
            dispatch_sequence=dispatch_sequence,
            issuer=foreign,
        )

    cast(Any, verifier).verify_active_market_dispatch = cross_verifier
    initial = matcher._state
    with pytest.raises(HistoricalMatcherError) as cross:
        matcher.match_active_market_root(delayed, dispatch_sequence=8)
    assert cross.value.code is OutcomeCode.CONFLICTING_ID
    assert matcher._state is initial

    for port_name, method_name in (
        ("_active_dispatch_verifier", "verify_active_market_dispatch"),
        (
            "_submission_authorization_verifier",
            "verify_authorized_historical_submission",
        ),
    ):
        _, matcher, orders, causal, delayed, _ = _system()
        setattr(getattr(matcher, port_name), method_name, lambda *_args, **_kwargs: object())
        initial = matcher._state
        with pytest.raises(HistoricalMatcherError):
            if port_name == "_active_dispatch_verifier":
                matcher.match_active_market_root(delayed, dispatch_sequence=8)
            else:
                matcher.submit(
                    orders[0],
                    causal_market_root=causal,
                    dispatch_sequence=7,
                )
        assert matcher._state is initial


def test_retained_public_artifact_mutation_halts_before_replay_or_membership() -> None:
    _, matcher, orders, causal, delayed, _ = _system()
    receipt = matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    object.__setattr__(receipt, "quantity_text", "999")
    with pytest.raises(HistoricalMatcherError) as receipt_drift:
        matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    assert receipt_drift.value.code is OutcomeCode.CONFLICTING_ID
    assert matcher._state.conflict is not None
    assert (
        matcher._state.conflict.conflict_kind
        is HistoricalMatcherConflictKind.RETAINED_BINDING_DRIFT
    )

    _, matcher, orders, causal, delayed, _ = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    batch = matcher.match_active_market_root(delayed, dispatch_sequence=8)
    object.__setattr__(batch, "dispatch_sequence", 9)
    with pytest.raises(HistoricalMatcherError) as batch_drift:
        matcher.match_active_market_root(delayed, dispatch_sequence=8)
    assert batch_drift.value.code is OutcomeCode.CONFLICTING_ID
    assert matcher._state.conflict is not None
    assert (
        matcher._state.conflict.conflict_kind
        is HistoricalMatcherConflictKind.RETAINED_BINDING_DRIFT
    )

    _, matcher, orders, causal, delayed, _ = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    batch = matcher.match_active_market_root(delayed, dispatch_sequence=8)
    ingress = batch.ingresses[0]
    object.__setattr__(ingress, "ingress_sequence", 99)
    with pytest.raises(HistoricalMatcherError) as ingress_drift:
        matcher.has_issued_ingress(
            ingress_identity=matcher._state.issued[0].binding.ingress_identity,
            canonical_ingress_bytes=matcher._state.issued[0].ingress_bytes,
            canonical_fact_bytes=matcher._state.issued[0].fact_bytes,
        )
    assert ingress_drift.value.code is OutcomeCode.CONFLICTING_ID
    assert matcher._state.conflict is not None
    assert (
        matcher._state.conflict.conflict_kind
        is HistoricalMatcherConflictKind.RETAINED_BINDING_DRIFT
    )


def test_no_fill_filters_and_first_later_fill_are_closed_and_replay_stable() -> None:
    def raw_variant(
        causal: MarketDataEnvelope,
        *,
        adjustment: Adjustment = Adjustment.RAW,
        revision: int = 0,
        instrument: Instrument | None = None,
        event_time: datetime | None = None,
    ) -> MarketDataEnvelope:
        payload = replace(
            causal.payload,
            adjustment=adjustment,
            instrument=instrument or causal.payload.instrument,
            interval_end=event_time or causal.payload.interval_end,
        )
        return replace(
            causal,
            payload=payload,
            available_at=max(causal.available_at, payload.interval_end),
            source_sequence=causal.source_sequence + 100,
            revision=revision,
        )

    for root in (
        lambda causal: causal,
        lambda causal: raw_variant(causal, revision=1),
        lambda causal: raw_variant(
            causal,
            instrument=Instrument(VenueId("XNYS"), "MSFT"),
        ),
        lambda causal: raw_variant(causal, event_time=causal.event_time),
    ):
        _, matcher, orders, causal, _, _ = _system()
        matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
        batch = matcher.match_active_market_root(root(causal), dispatch_sequence=8)
        assert batch.ingresses == ()
        assert matcher.state.pending_order_ids == (orders[0].order_id,)

    _, matcher, orders, causal, delayed, end = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    filled = matcher.match_active_market_root(delayed, dispatch_sequence=8)
    assert len(filled.ingresses) == 1
    assert matcher.match_active_market_root(delayed, dispatch_sequence=8) is filled
    terminal = matcher.expire_at_active_end(end, dispatch_sequence=9)
    assert terminal.ingresses == ()


def test_submission_membership_authorization_and_caller_ownership_fail_closed() -> None:
    _, matcher, orders, causal, _, _ = _system()
    initial = matcher._state
    cast(_OrderVerifier, matcher._order_issuance_verifier)._orders.clear()
    with pytest.raises(HistoricalMatcherError) as arbitrary:
        matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    assert arbitrary.value.code is OutcomeCode.CONFLICTING_ID
    assert matcher._state is initial

    _, matcher, orders, causal, _, _ = _system()
    forged = _clone_order(orders[0], quantity=CanonicalDecimal("2"))
    initial = matcher._state
    with pytest.raises(HistoricalMatcherError) as forged_error:
        matcher.submit(forged, causal_market_root=causal, dispatch_sequence=7)
    assert forged_error.value.code is OutcomeCode.CONFLICTING_ID
    assert matcher._state is initial

    for code in (
        OutcomeCode.SUBMISSION_BLOCKED_BY_HALT,
        OutcomeCode.RISK_STALE_APPROVAL,
        OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED,
        OutcomeCode.DURABILITY_AUDIT_ACK_MISMATCH,
    ):
        _, matcher, orders, causal, _, _ = _system()

        def denied(
            *_args: object,
            _code: OutcomeCode = code,
            **_kwargs: object,
        ) -> Any:
            raise HistoricalPreEffectAuthorizationError(_code, "denied")

        verifier = cast(_AuthorizationVerifier, matcher._submission_authorization_verifier)
        cast(Any, verifier).verify_authorized_historical_submission = denied
        initial = matcher._state
        with pytest.raises(HistoricalPreEffectAuthorizationError) as rejection:
            matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
        assert rejection.value.code is code
        assert matcher._state is initial
        assert verifier.calls == 0

    _, matcher, orders, causal, _, _ = _system()
    caller_order = orders[0]
    retained_input = _clone_order(caller_order)
    receipt = matcher.submit(caller_order, causal_market_root=causal, dispatch_sequence=7)
    receipt_bytes = canonical_historical_submission_receipt_bytes(receipt)
    object.__setattr__(caller_order, "quantity", CanonicalDecimal("999"))
    assert matcher.submit(retained_input, causal_market_root=causal, dispatch_sequence=7) is receipt
    assert canonical_historical_submission_receipt_bytes(receipt) == receipt_bytes

    _, matcher, orders, causal, _, _ = _system()
    foreign_run = RunId("87654321-4321-4234-8234-cba987654321")
    foreign_values: dict[str, object] = {"run_id": foreign_run}
    for field_name in (
        "order_id",
        "intent_id",
        "correlation_id",
        "causation_id",
        "decision_id",
        "approval_id",
    ):
        identity = cast(EconomicId, getattr(orders[0], field_name))
        foreign_values[field_name] = EconomicId(
            foreign_run,
            identity.owner_kind,
            identity.owner_sequence,
        )
    foreign_order = _clone_order(orders[0], **foreign_values)
    initial = matcher._state
    with pytest.raises(HistoricalMatcherError) as foreign:
        matcher.submit(foreign_order, causal_market_root=causal, dispatch_sequence=7)
    assert foreign.value.code is OutcomeCode.CONFLICTING_ID
    assert matcher._state is initial


def test_uint64_sequence_edges_and_atomic_multi_fact_exhaustion() -> None:
    maximum = (1 << 64) - 1
    _, matcher, orders, causal, _, _ = _system()
    matcher._state = replace(matcher._state, next_submission=maximum)
    receipt = matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    assert receipt.submission_sequence == maximum
    assert matcher._state.next_submission is None

    assert _advance(maximum) is None

    _, matcher, orders, causal, delayed, end = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    matcher.submit(orders[1], causal_market_root=delayed, dispatch_sequence=8)
    matcher._state = replace(matcher._state, next_fact=maximum)
    initial = matcher._state
    with pytest.raises(HistoricalMatcherError) as exhausted:
        matcher.expire_at_active_end(end, dispatch_sequence=9)
    assert exhausted.value.code is OutcomeCode.ARITHMETIC_OVERFLOW
    assert matcher._state is initial

    _, matcher, _, _, delayed, _ = _system()
    with pytest.raises(HistoricalMatcherError) as zero:
        matcher.match_active_market_root(delayed, dispatch_sequence=0)
    assert zero.value.code is OutcomeCode.OUT_OF_RANGE
    maximum_batch = matcher.match_active_market_root(delayed, dispatch_sequence=maximum)
    assert maximum_batch.dispatch_sequence == maximum
    with pytest.raises(HistoricalMatcherError) as overflow:
        matcher.match_active_market_root(delayed, dispatch_sequence=maximum + 1)
    assert overflow.value.code is OutcomeCode.OUT_OF_RANGE


def test_final_uint64_fact_boundary_constructs_and_replays_canonical_evidence() -> None:
    maximum = (1 << 64) - 1
    _, matcher, orders, causal, delayed, _ = _system()
    receipt = matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    order = orders[0]
    root_sha256 = historical_market_root_digest(delayed)
    root_key = runtime_root_order_key(delayed)
    price = _quantized_close(
        delayed.payload.close,
        side=order.side,
        specification=matcher.spec_set.require(order.instrument),
    )
    observation = historical_matcher_observation_digest(
        fact_sequence=maximum,
        fact_kind="trade",
        source_namespace=matcher.source_namespace,
        provenance_id=matcher.provenance_id,
        submission_receipt_sha256=historical_submission_receipt_digest(receipt),
        order_sha256=order_digest(order),
        trigger_root_kind=HistoricalDispatchKind.MARKET,
        trigger_root_sha256=root_sha256,
        trigger_root_key=root_key,
        trigger_dispatch_sequence=8,
        occurred_at=delayed.event_time,
        available_at=delayed.available_at,
        instrument=order.instrument,
        side=order.side,
        quantity_text=order.quantity.text,
        price_text=price.text,
        expiry_outcome_code=None,
        spec_set=matcher.spec_set,
        execution_policy=matcher.execution_policy,
    )
    fact = create_trade_execution_fact(
        spec_set=matcher.spec_set,
        side=order.side,
        quantity=order.quantity,
        price=price,
        source_namespace=matcher.source_namespace,
        dedup_identity=SourceNativeSequence(maximum),
        occurred_at=delayed.event_time,
        provenance=FactProvenance(matcher.provenance_id, observation),
        instrument=order.instrument,
        client_submission_key=order_client_submission_key(order),
        order_id=order.order_id,
        correlation_id=order.correlation_id,
        causation_id=order.order_id,
    )
    ingress = create_execution_fact_ingress(
        available_at=delayed.available_at,
        source_namespace=matcher.source_namespace,
        ingress_sequence=maximum,
        fact=fact,
    )
    ingress_sha256 = execution_fact_ingress_digest(ingress)
    values = {
        "run_id": matcher.run_id,
        "source_namespace": matcher.source_namespace,
        "dispatch_kind": HistoricalDispatchKind.MARKET,
        "dispatch_sequence": 8,
        "trigger_root_sha256": root_sha256,
        "trigger_root_key": root_key,
        "next_fact_sequence_before": maximum,
        "next_fact_sequence_after": None,
        "submission_sequences": (receipt.submission_sequence,),
        "order_ids": (order.order_id,),
        "ingresses": (ingress,),
        "ingress_sha256s": (ingress_sha256,),
    }
    batch = _create_historical_matcher_dispatch_batch(**values)
    binding = _create_historical_matcher_descendant_binding(
        ingress_identity=ingress.identity,
        ingress_sha256=ingress_sha256,
        fact_sha256=execution_fact_digest(fact),
        batch_sha256=historical_matcher_dispatch_batch_digest(batch),
        batch_index=0,
        parent_kind=HistoricalDispatchKind.MARKET,
        parent_root_sha256=root_sha256,
        parent_root_key=root_key,
        parent_dispatch_sequence=8,
    )
    _validate_descendant_binding(binding)
    assert fact.dedup_identity == SourceNativeSequence(maximum)
    assert ingress.ingress_sequence == maximum
    assert batch.next_fact_sequence_before == maximum
    assert batch.next_fact_sequence_after is None
    fact_bytes = canonical_execution_fact_bytes(fact)
    ingress_bytes = canonical_execution_fact_ingress_bytes(ingress)
    batch_bytes = canonical_historical_matcher_dispatch_batch_bytes(batch)
    replay = _create_historical_matcher_dispatch_batch(**values)
    assert canonical_execution_fact_bytes(replay.ingresses[0].fact) == fact_bytes
    assert canonical_execution_fact_ingress_bytes(replay.ingresses[0]) == ingress_bytes
    assert canonical_historical_matcher_dispatch_batch_bytes(replay) == batch_bytes


def test_multi_order_emission_is_submission_order_even_if_pending_view_is_reordered() -> None:
    _, matcher, orders, causal, delayed, end = _system()
    first = matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    second = matcher.submit(orders[1], causal_market_root=delayed, dispatch_sequence=8)
    matcher._state = replace(matcher._state, pending=tuple(reversed(matcher._state.pending)))
    batch = matcher.expire_at_active_end(end, dispatch_sequence=9)
    assert batch.submission_sequences == (
        first.submission_sequence,
        second.submission_sequence,
    )
    assert batch.order_ids == (orders[0].order_id, orders[1].order_id)


@pytest.mark.parametrize(
    ("value", "side", "expected"),
    (
        (1.0, OrderSide.BUY, "1"),
        (1.24, OrderSide.BUY, "1"),
        (1.26, OrderSide.BUY, "1.5"),
        (1.25, OrderSide.BUY, "1.5"),
        (1.24, OrderSide.SELL, "1"),
        (1.26, OrderSide.SELL, "1.5"),
        (1.25, OrderSide.SELL, "1"),
    ),
)
def test_binary64_tick_vectors(value: float, side: OrderSide, expected: str) -> None:
    _, matcher, _, _, _, _ = _system()
    baseline = matcher.spec_set.specifications[0]
    specification = replace(baseline, price_quantum=CanonicalDecimal("0.5"))
    assert _quantized_close(value, side=side, specification=specification).text == expected


@pytest.mark.parametrize(
    ("value", "side", "expected"),
    (
        (-1.0, OrderSide.BUY, "-1"),
        (-1.24, OrderSide.BUY, "-1"),
        (-1.26, OrderSide.BUY, "-1.5"),
        (-1.25, OrderSide.BUY, "-1"),
        (-1.25, OrderSide.SELL, "-1.5"),
    ),
)
def test_signed_binary64_tick_vectors(value: float, side: OrderSide, expected: str) -> None:
    _, matcher, _, _, _, _ = _system()
    specification = replace(
        matcher.spec_set.specifications[0],
        price_quantum=CanonicalDecimal("0.5"),
        price_domain=PriceDomain.SIGNED,
    )
    assert _quantized_close(value, side=side, specification=specification).text == expected


def test_invalid_non_raw_carrier_never_fills_or_publishes() -> None:
    _, matcher, orders, causal, delayed, _ = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    object.__setattr__(delayed.payload, "adjustment", "split_adjusted")
    initial = matcher._state
    with pytest.raises(HistoricalMatcherError):
        matcher.match_active_market_root(delayed, dispatch_sequence=8)
    assert matcher._state is initial


def test_submission_client_dispatch_and_terminal_identity_conflicts_halt() -> None:
    _, matcher, orders, causal, delayed, _ = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    conflicting_order = _clone_order(orders[0], quantity=CanonicalDecimal("2"))
    with pytest.raises(HistoricalMatcherError):
        matcher.submit(conflicting_order, causal_market_root=causal, dispatch_sequence=7)
    assert matcher._state.conflict is not None
    assert (
        matcher._state.conflict.conflict_kind is HistoricalMatcherConflictKind.SUBMISSION_IDENTITY
    )

    _, matcher, orders, causal, _, _ = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    second_key = order_client_submission_key(orders[1])
    matcher._state = replace(
        matcher._state,
        submission_by_client=MappingProxyType(
            {**matcher._state.submission_by_client, second_key: matcher._state.submissions[0]}
        ),
    )
    with pytest.raises(HistoricalMatcherError):
        matcher.submit(orders[1], causal_market_root=causal, dispatch_sequence=7)
    assert matcher._state.conflict is not None
    assert (
        matcher._state.conflict.conflict_kind is HistoricalMatcherConflictKind.CLIENT_SUBMISSION_KEY
    )

    _, matcher, _, _, delayed, _ = _system()
    matcher.match_active_market_root(delayed, dispatch_sequence=8)
    different = replace(delayed, source_sequence=delayed.source_sequence + 1)
    with pytest.raises(HistoricalMatcherError):
        matcher.match_active_market_root(different, dispatch_sequence=8)
    assert matcher._state.conflict is not None
    assert matcher._state.conflict.conflict_kind is HistoricalMatcherConflictKind.DISPATCH_IDENTITY


def test_public_conflict_snapshot_is_independent_from_first_retained_evidence() -> None:
    _, matcher, orders, causal, _, _ = _system()
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    with pytest.raises(HistoricalMatcherError):
        matcher.submit(
            _clone_order(orders[0], quantity=CanonicalDecimal("2")),
            causal_market_root=causal,
            dispatch_sequence=7,
        )
    first = matcher.state
    assert first.conflict is not None
    retained_pending_count = first.conflict.pending_count
    object.__setattr__(first.conflict, "pending_count", retained_pending_count + 10)
    second = matcher.state
    assert second.conflict is not None
    assert second.conflict is not first.conflict
    assert second.conflict.pending_count == retained_pending_count

    _, matcher, _, _, _, end = _system()
    matcher.expire_at_active_end(end, dispatch_sequence=9)
    different_end = replace(end, producer_sequence=end.producer_sequence + 1)
    with pytest.raises(HistoricalMatcherError):
        matcher.expire_at_active_end(different_end, dispatch_sequence=9)
    assert matcher._state.conflict is not None
    assert matcher._state.conflict.conflict_kind is HistoricalMatcherConflictKind.DISPATCH_IDENTITY


def test_verifier_mutation_and_non_exact_proof_fields_are_rejected_atomically() -> None:
    _, matcher, orders, causal, _, _ = _system()
    verifier = cast(_AuthorizationVerifier, matcher._submission_authorization_verifier)
    original_auth = verifier.verify_authorized_historical_submission

    def mutate_order(**kwargs: object) -> HistoricalSubmissionAuthorizationProof:
        proof = original_auth(**cast(Any, kwargs))
        object.__setattr__(
            orders[0].order_id.run_id, "value", "87654321-4321-4234-8234-cba987654321"
        )
        return proof

    cast(Any, verifier).verify_authorized_historical_submission = mutate_order
    initial = matcher._state
    with pytest.raises(HistoricalMatcherError) as changed:
        matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    assert changed.value.code is OutcomeCode.CONFLICTING_ID
    assert matcher._state is initial


def test_cross_issuer_and_mutated_authorization_and_end_proofs_are_rejected() -> None:
    fixture, matcher, orders, causal, _, _ = _system()
    original = cast(_AuthorizationVerifier, matcher._submission_authorization_verifier)
    receipts = [
        json.loads(fixture["artifacts"][name]["canonical_utf8"])
        for name in ("trade_receipt", "expiry_receipt")
    ]
    foreign = _AuthorizationVerifier(
        matcher.run_id,
        matcher.spec_set,
        matcher.execution_policy,
        receipts,
        orders,
    )
    cast(
        Any, original
    ).verify_authorized_historical_submission = foreign.verify_authorized_historical_submission
    initial = matcher._state
    with pytest.raises(HistoricalMatcherError) as cross:
        matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    assert cross.value.code is OutcomeCode.CONFLICTING_ID
    assert matcher._state is initial

    _, matcher, orders, causal, _, _ = _system()
    authorization = cast(_AuthorizationVerifier, matcher._submission_authorization_verifier)
    original_auth = authorization.verify_authorized_historical_submission

    def mutated_auth(**kwargs: object) -> HistoricalSubmissionAuthorizationProof:
        proof = original_auth(**cast(Any, kwargs))
        object.__setattr__(proof, "_dispatch_sequence", True)
        return proof

    cast(Any, authorization).verify_authorized_historical_submission = mutated_auth
    initial = matcher._state
    with pytest.raises(HistoricalMatcherError) as malformed:
        matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    assert malformed.value.code is OutcomeCode.INVALID_TYPE
    assert matcher._state is initial

    _, matcher, _, _, _, end = _system()
    dispatch = cast(_DispatchVerifier, matcher._active_dispatch_verifier)
    original_end = dispatch.verify_active_end_of_run_dispatch

    def mutate_end(root: EndOfRunRoot, *, dispatch_sequence: int) -> Any:
        proof = original_end(root, dispatch_sequence=dispatch_sequence)
        object.__setattr__(root, "producer_sequence", root.producer_sequence + 1)
        return proof

    cast(Any, dispatch).verify_active_end_of_run_dispatch = mutate_end
    initial = matcher._state
    with pytest.raises(HistoricalMatcherError) as changed:
        matcher.expire_at_active_end(end, dispatch_sequence=9)
    assert changed.value.code is OutcomeCode.CONFLICTING_ID
    assert matcher._state is initial

    _, matcher, _, _, delayed, _ = _system()
    dispatch = cast(_DispatchVerifier, matcher._active_dispatch_verifier)
    original_market = dispatch.verify_active_market_dispatch

    def non_exact(root: MarketDataEnvelope, *, dispatch_sequence: int) -> Any:
        proof = original_market(root, dispatch_sequence=dispatch_sequence)
        object.__setattr__(proof, "_dispatch_sequence", True)
        return proof

    cast(Any, dispatch).verify_active_market_dispatch = non_exact
    initial = matcher._state
    with pytest.raises(HistoricalMatcherError) as invalid:
        matcher.match_active_market_root(delayed, dispatch_sequence=8)
    assert invalid.value.code is OutcomeCode.INVALID_TYPE
    assert matcher._state is initial


def test_deep_caller_and_descendant_mutation_cannot_corrupt_lookup_or_conflict_snapshot() -> None:
    _, matcher, orders, causal, delayed, _ = _system()
    submitted = _clone_order(
        orders[0],
        order_id=EconomicId(
            RunId(orders[0].order_id.run_id.value),
            orders[0].order_id.owner_kind,
            orders[0].order_id.owner_sequence,
        ),
    )
    matcher.submit(orders[0], causal_market_root=causal, dispatch_sequence=7)
    object.__setattr__(orders[0].order_id.run_id, "value", "87654321-4321-4234-8234-cba987654321")
    assert matcher.submit(submitted, causal_market_root=causal, dispatch_sequence=7)

    batch = matcher.match_active_market_root(delayed, dispatch_sequence=8)
    binding = matcher._state.issued[0].binding
    object.__setattr__(binding.ingress_identity, "ingress_sequence", True)
    with pytest.raises(HistoricalMatcherError):
        matcher.resolve_descendant_binding(
            ingress_identity=batch.ingresses[0].identity,
            canonical_ingress_bytes=matcher._state.issued[0].ingress_bytes,
            canonical_fact_bytes=matcher._state.issued[0].fact_bytes,
        )
    state = matcher.state
    assert state.halted is True
    assert state.conflict is not None
    assert state.conflict.conflict_kind is HistoricalMatcherConflictKind.RETAINED_BINDING_DRIFT


def test_multi_instrument_terminal_emission_uses_global_submission_order() -> None:
    fixture, baseline, orders, causal, delayed, end = _system()
    aapl_spec = baseline.spec_set.specifications[0]
    msft = Instrument(VenueId("XNYS"), "MSFT")
    msft_spec = replace(
        aapl_spec,
        instrument=msft,
        specification_id=InstrumentSpecId("xnys.msft.v1"),
    )
    spec_set = build_instrument_spec_set(
        InstrumentSpecSetId("phase1.multi.v1"),
        (msft_spec, aapl_spec),
    )
    spec_digest = instrument_spec_set_digest(spec_set)
    bound_orders = [
        _clone_order(
            orders[0],
            instrument_spec_set_id=spec_set.identifier,
            instrument_spec_set_sha256=spec_digest,
        ),
        _clone_order(
            orders[1],
            instrument=msft,
            instrument_specification_id=msft_spec.specification_id,
            instrument_spec_set_id=spec_set.identifier,
            instrument_spec_set_sha256=spec_digest,
        ),
    ]
    msft_causal = replace(delayed, payload=replace(delayed.payload, instrument=msft))
    receipts = [
        json.loads(fixture["artifacts"][name]["canonical_utf8"])
        for name in ("trade_receipt", "expiry_receipt")
    ]
    matcher = create_phase1_historical_matcher(
        run_id=baseline.run_id,
        spec_set=spec_set,
        execution_policy=baseline.execution_policy,
        source_namespace=baseline.source_namespace,
        provenance_id=baseline.provenance_id,
        order_issuance_verifier=_OrderVerifier(
            baseline.run_id, spec_set, baseline.execution_policy, bound_orders
        ),
        submission_authorization_verifier=_AuthorizationVerifier(
            baseline.run_id,
            spec_set,
            baseline.execution_policy,
            receipts,
            bound_orders,
        ),
        active_dispatch_verifier=_DispatchVerifier(baseline.run_id, spec_set),
    )
    first = matcher.submit(bound_orders[0], causal_market_root=causal, dispatch_sequence=7)
    second = matcher.submit(bound_orders[1], causal_market_root=msft_causal, dispatch_sequence=8)
    matcher._state = replace(matcher._state, pending=tuple(reversed(matcher._state.pending)))
    batch = matcher.expire_at_active_end(end, dispatch_sequence=9)
    assert batch.submission_sequences == (
        first.submission_sequence,
        second.submission_sequence,
    )
    assert batch.order_ids == (bound_orders[0].order_id, bound_orders[1].order_id)


def test_binary64_digit_boundary_is_structured_overflow() -> None:
    _, matcher, _, _, _, _ = _system()
    with pytest.raises(HistoricalMatcherError) as boundary:
        _quantized_close(
            float.fromhex("0x1.fffffffffffffp+1023"),
            side=OrderSide.BUY,
            specification=matcher.spec_set.specifications[0],
        )
    assert boundary.value.code is OutcomeCode.ARITHMETIC_OVERFLOW
