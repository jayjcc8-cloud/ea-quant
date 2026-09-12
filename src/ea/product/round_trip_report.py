"""Read-only V2 report verification over ordered, acknowledged settlement evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, ClassVar

import ea
from ea.core import (
    FILL_DIGEST_DOMAIN,
    ORDER_DIGEST_DOMAIN,
    AuditRecordKind,
    CanonicalDecimal,
    OutcomeCode,
    audit_chain_head,
    require_quantized,
    settle_execution,
)
from ea.core.economics import settle_product
from ea.core.execution_messages import (
    EXECUTION_APPROVAL_CANONICALIZATION,
    RISK_DECISION_CANONICALIZATION,
    RISK_DECISION_DIGEST_DOMAIN,
)
from ea.product import reporting as r
from ea.product.identity import semantic_outcome_sha256


def economics(result: dict[str, Any], scenario: Any, price: CanonicalDecimal) -> dict[str, Any]:
    spec = scenario.spec_set.require(scenario.instrument)
    zero = CanonicalDecimal("0")
    fills = [leg["fill"] for leg in result["execution_legs"] if leg["fill"] is not None]
    fees = [CanonicalDecimal(fill["fees"][0]["amount"]) for fill in fills]
    notionals = [
        settle_execution(
            scenario.spec_set,
            scenario.instrument,
            CanonicalDecimal(fill["price"]),
            CanonicalDecimal(fill["quantity"]),
        ).amount
        for fill in fills
    ]
    cash = scenario.initial_cash
    for index, notional in enumerate(notionals):
        require_quantized(notional, spec.currency_quantum, field_name="settled notional")
        cash = r._subtract(
            r._subtract(cash, notional) if index % 2 == 0 else r._add(cash, notional), fees[index]
        )
    quantity = CanonicalDecimal(fills[-1]["quantity"]) if len(fills) % 2 else zero
    if (
        result["ending_cash"] != [{"amount": cash.text, "currency": scenario.funding_currency.code}]
        or result["final_quantity"] != quantity.text
    ):
        raise ValueError("settlement balances conflict")
    value = (
        settle_product(price, quantity, spec.contract_multiplier, spec.currency_quantum).amount
        if quantity.coefficient
        else zero
    )
    require_quantized(value, spec.currency_quantum, field_name="position value")
    equity = r._add(cash, value)
    total_fees = zero
    for fee in fees:
        total_fees = r._add(total_fees, fee)
    realized = zero
    trades = []
    for index in range(0, len(fills) - 1, 2):
        pnl = r._subtract(
            r._subtract(r._subtract(notionals[index + 1], notionals[index]), fees[index]),
            fees[index + 1],
        )
        realized = r._add(realized, pnl)
        trades.append(
            {
                "entry_order_id": fills[index]["order_id"],
                "entry_fill_id": fills[index]["fill_id"],
                "exit_order_id": fills[index + 1]["order_id"],
                "exit_fill_id": fills[index + 1]["fill_id"],
                "quantity": fills[index]["quantity"],
                "entry_settled_notional": notionals[index].text,
                "exit_settled_notional": notionals[index + 1].text,
                "entry_fee": fees[index].text,
                "exit_fee": fees[index + 1].text,
                "realized_pnl": pnl.text,
            }
        )
    unrealized = r._subtract(value, notionals[-1]) if len(fills) % 2 else zero
    net = r._subtract(equity, scenario.initial_cash)
    summary = {
        "currency": scenario.funding_currency.code,
        "counts": {"orders": len(result["execution_legs"]), "fills": len(fills)},
        "initial_funding": {
            "amount": scenario.initial_cash.text,
            "currency": scenario.funding_currency.code,
        },
        "ending_cash": result["ending_cash"],
        "ending_positions": result["ending_positions"],
        "final_quantity": quantity.text,
        "final_position_value": value.text,
        "position_state": result["position_state"],
        "position_outcome": result["position_outcome"],
        "completed_round_trips": result["completed_round_trips"],
        "execution_legs": result["execution_legs"],
        "entry_fee": fees[0].text if fees else "0",
        "exit_fee": fees[1].text if len(fees) == 2 else "0",
        "fees": {
            "amount": total_fees.text,
            "count": len(fees),
            "currency": scenario.funding_currency.code,
            "rule": "per-fill-commission",
        },
        "realized_pnl": {"amount": realized.text, "rule": "closed-round-trip-after-fees"},
        "gross_unrealized_pnl": {
            "amount": unrealized.text,
            "rule": "open-position-before-entry-fee",
        },
        "net_pnl": {"amount": net.text, "rule": "equity-minus-initial-funding-v1"},
        "equity": {"amount": equity.text, "rule": "acknowledged-cash-plus-position-value"},
        "total_return": {
            "value": r._ratio(net, scenario.initial_cash).text,
            "unit": "ratio",
            "denominator": "initial_funding",
            "precision": "18-decimal-places",
            "rounding": "half_even",
            "rule": "net-pnl-over-initial-funding",
        },
    }

    if scenario.schema_version == 5:
        summary.pop("entry_fee")
        summary.pop("exit_fee")
        summary["trades"] = trades
        summary["open_position"] = (
            {
                "entry_order_id": fills[-1]["order_id"],
                "entry_fill_id": fills[-1]["fill_id"],
                "quantity": quantity.text,
                "entry_settled_notional": notionals[-1].text,
                "entry_fee": fees[-1].text,
                "last_valuation": value.text,
                "gross_unrealized_pnl": unrealized.text,
            }
            if len(fills) % 2
            else None
        )
    return summary


def _position(count: int, bounded: bool) -> tuple[str, str]:
    if bounded:
        return (
            ("LONG", "OPEN_AT_END")
            if count % 2
            else ("FLAT", "FLAT_AFTER_TRADES" if count else "FLAT_NO_TRADE")
        )
    return (
        ("FLAT_INITIAL", "FLAT_INITIAL"),
        ("LONG_OPEN", "OPEN_AT_END"),
        ("FLAT_CLOSED", "CLOSED"),
    )[count]


def semantic_projection(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": (
            "ea.backtest-semantic-outcome.v4"
            if result["schema"] == "ea.backtest-single-run-result.v4"
            else "ea.backtest-semantic-outcome.v3"
        ),
        "lineage_sha256": result["lineage_sha256"],
        "position_state": result["position_state"],
        "position_outcome": result["position_outcome"],
        "final_quantity": result["final_quantity"],
        "ending_cash": result["ending_cash"],
        "final_equity": result["final_equity"],
        "completed_round_trips": result["completed_round_trips"],
        "execution_legs": [
            {
                "role": leg["role"],
                "outcome": leg["outcome"],
                "order": {k: leg["order"][k] for k in ("quantity", "side")},
                "fill": None
                if leg["fill"] is None
                else {k: leg["fill"][k] for k in ("price", "quantity", "side", "fees")},
            }
            for leg in result["execution_legs"]
        ],
    }


def _verify_local_risk(order: dict[str, Any], risk: dict[str, Any], *, exit_leg: bool) -> None:
    """Reconstruct the closed decision and verify its hash in the authorized Order.

    Local quantity is action-defined, so a parameter name cannot prove allow/resize.
    The existing Order pins every decision/approval field needed for this read-only check.
    """
    r._exact_keys(risk, {"decision"})
    kind = risk["decision"]
    if kind not in {"allow", "resize"} or (exit_leg and kind != "allow"):
        raise ValueError("unsupported local risk decision")
    common = {
        key: order[key]
        for key in (
            "correlation_id",
            "decision_id",
            "dispatch_sequence",
            "effective_intent_sha256",
            "intent_id",
            "portfolio_snapshot_version",
            "risk_state_version",
            "run_id",
            "schema_version",
        )
    }
    common.update(
        approved_quantity=order["quantity"],
        causal_root_available_at=order["eligible_after_available_at"],
    )
    approval = {
        **common,
        "approval_id": order["approval_id"],
        "canonicalization": EXECUTION_APPROVAL_CANONICALIZATION,
        "causation_id": order["decision_id"],
        "message_type": "execution_approval",
        "original_intent_sha256": order["original_intent_sha256"],
    }
    decision = {
        **common,
        "approval": approval,
        "canonicalization": RISK_DECISION_CANONICALIZATION,
        "causation_id": order["intent_id"],
        "intent_sha256": order["original_intent_sha256"],
        "kind": kind,
        "message_type": "risk_decision",
        "outcome_code": (
            OutcomeCode.RISK_ALLOWED if kind == "allow" else OutcomeCode.RISK_RESIZED
        ).value,
    }
    if (
        sha256(RISK_DECISION_DIGEST_DOMAIN + r._canonical_json(decision)).hexdigest()
        != order["risk_decision_sha256"]
    ):
        raise ValueError("local risk decision conflicts with authorized Order")


def validate_result(
    result: dict[str, Any],
    *,
    scenario: Any,
    manifest: Any,
    records: Any,
    funded_snapshot: str,
    run_id: str,
    lineage: str,
) -> None:
    bounded = scenario.schema_version == 5
    limit = json.loads(scenario.canonical_bytes)["strategy"]["max_round_trips"] if bounded else 1
    r._exact_keys(
        result,
        {
            "schema",
            "status",
            "terminal_state",
            "run_id",
            "lineage_sha256",
            "scenario_sha256",
            "audit_chain_head_sha256",
            "execution_legs",
            "position_state",
            "position_outcome",
            "final_quantity",
            "completed_round_trips",
            "ledger_sequence",
            "portfolio_snapshot_evidence",
            "ending_cash",
            "ending_positions",
            "initial_funding",
            "randomness",
            "reconciliation",
            "semantic_outcome_sha256",
            "final_equity",
        },
    )
    if (
        result["schema"]
        != ("ea.backtest-single-run-result.v4" if bounded else "ea.backtest-single-run-result.v3")
        or result["status"] != "success"
        or result["terminal_state"] != "completed"
        or result["run_id"] != run_id
        or result["lineage_sha256"] != lineage
        or result["scenario_sha256"] != scenario.scenario_sha256.value
        or result["audit_chain_head_sha256"] != audit_chain_head(records[-1]).value
        or result["randomness"] != manifest["randomness"]
    ):
        raise ValueError("V3 result identity conflicts")
    if result["initial_funding"] != {
        "amount": scenario.initial_cash.text,
        "currency": scenario.funding_currency.code,
        "ledger_sequence": 1,
        "status": "applied",
    }:
        raise ValueError("funding conflicts")
    authorized = {}
    accepted = {}
    handoffs = []
    reconciliations = []
    completed = set()
    terminal = None
    expired = set()
    for record in records:
        payload: Any = r._payload(record)
        kind = record.record_kind
        if kind is AuditRecordKind.RUN_PREPARED and payload.get(
            "manifest_sha256"
        ) != r.manifest_digest(manifest):
            raise ValueError("manifest conflicts")
        if kind is AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION:
            key = r._identity(payload["order_id"], run_id=run_id, owner_kind="execution.order")
            authorized[key] = payload
        elif (
            kind is AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME
            and payload.get("action") == "accepted"
        ):
            fill = payload["fill"]
            if fill is not None:
                key = r._identity(fill["fill_id"], run_id=run_id, owner_kind="execution.fill")
                if key in accepted:
                    raise ValueError("duplicate committed Fill")
                accepted[key] = fill["fill_sha256"]
            elif payload.get("resolved_order_id") is not None:
                expired.add(
                    r._identity(
                        payload["resolved_order_id"], run_id=run_id, owner_kind="execution.order"
                    )
                )
        elif (
            kind is AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME
            and payload.get("action") == "effect_committed"
        ):
            handoffs.append(payload)
        elif (
            kind is AuditRecordKind.RUNTIME_DISPATCH_COMPLETED
            and payload.get("ledger_outcome_count") == 1
        ):
            completed.add(payload["dispatch_sequence"])
        elif kind is AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME:
            reconciliations.append(payload)
        elif kind is AuditRecordKind.RUN_TERMINAL:
            terminal = payload
    legs = result["execution_legs"]
    if type(legs) is not list or len(legs) > 2 * limit or len(authorized) != len(legs):
        raise ValueError("bounded order count conflicts")
    filled: list[Any] = []
    for index, leg in enumerate(legs):
        r._exact_keys(
            leg,
            {
                "role",
                "order",
                "fill",
                "risk",
                "outcome",
                "order_evidence",
                "order_sha256",
                "fill_sha256",
            },
        )
        order = r._exact_keys(leg["order"], {"order_id", "quantity", "side"})
        identity = r._identity(order["order_id"], run_id=run_id, owner_kind="execution.order")
        if (
            identity not in authorized
            or identity[1] != index + 1
            or order["side"] != ("buy" if index % 2 == 0 else "sell")
            or leg["role"] != ("entry" if index % 2 == 0 else "exit")
            or (index % 2 == 1 and (not filled or order["quantity"] != filled[-1]["quantity"]))
        ):
            raise ValueError("bounded leg order conflicts")
        if len(filled) != index:
            raise ValueError("leg follows an unfilled order")
        if scenario.strategy_package is not None:
            _verify_local_risk(leg["order_evidence"], leg["risk"], exit_leg=index % 2 == 1)
        else:
            expected_risk = (
                "allow"
                if index % 2 == 1
                or order["quantity"] == str(scenario.strategy_parameters["target_quantity"])
                else "resize"
            )
            if leg["risk"] != {"decision": expected_risk}:
                raise ValueError("risk projection conflicts with authorized quantity")
        order_evidence = leg["order_evidence"]
        if (
            sha256(ORDER_DIGEST_DOMAIN + r._canonical_json(order_evidence)).hexdigest()
            != authorized[identity]["order_sha256"]
            or leg["order_sha256"] != authorized[identity]["order_sha256"]
            or any(order_evidence[k] != order[k] for k in ("order_id", "quantity", "side"))
        ):
            raise ValueError("authorized order bytes conflict")
        if leg["fill"] is not None:
            if (
                leg["fill_sha256"]
                != sha256(FILL_DIGEST_DOMAIN + r._canonical_json(leg["fill"])).hexdigest()
            ):
                raise ValueError("Fill hash conflicts")
            fill = leg["fill"]
            r._validate_fill_evidence(
                fill,
                result_fill={k: fill[k] for k in ("fill_id", "price", "quantity", "side")},
                result_order=order,
                scenario=scenario,
                run_id=run_id,
                accepted_fills=accepted,
            )
            if leg["outcome"] != "filled":
                raise ValueError("fill outcome conflicts")
            filled.append(fill)
        elif (
            leg["outcome"] != "expired" or identity not in expired or leg["fill_sha256"] is not None
        ):
            raise ValueError("unfilled outcome conflicts")
    if len(accepted) != len(filled) or len(handoffs) != len(filled):
        raise ValueError("unacknowledged fill")
    previous = funded_snapshot
    for index, (fill, handoff) in enumerate(zip(filled, handoffs, strict=True)):
        original = handoff["original_ledger_apply_outcome"]
        if (
            handoff["fill_id"] != fill["fill_id"]
            or handoff["before_snapshot_sha256"] != previous
            or handoff["dispatch_sequence"] not in completed
            or original["code"] != "ledger.applied"
            or original["submitted_fill_id"] != fill["fill_id"]
            or original["submitted_fill_sha256"]
            != accepted[r._identity(fill["fill_id"], run_id=run_id, owner_kind="execution.fill")]
            or original["before_snapshot_version"] != index + 1
            or original["after_snapshot_version"] != index + 2
            or original["transaction_entry_id"]["owner_sequence"] != index + 2
            or original["snapshot_sha256"] != handoff["after_snapshot_sha256"]
        ):
            raise ValueError("ordered ledger handoff conflicts")
        previous = handoff["after_snapshot_sha256"]
    count = len(filled)
    if result["reconciliation"] != {
        "cash": "match",
        "position": "match" if count else "not_required_empty",
    }:
        raise ValueError("result reconciliation conflicts with acknowledged observations")
    if (
        terminal is None
        or terminal.get("terminal_kind") != "success"
        or terminal.get("final_published_snapshot_sha256") != previous
        or result["ledger_sequence"] != count + 1
        or result["position_state"] != _position(count, bounded)[0]
        or result["position_outcome"] != _position(count, bounded)[1]
        or result["completed_round_trips"] != count // 2
    ):
        raise ValueError("terminal position conflicts")
    if len(reconciliations) != (2 if count else 1) or any(
        item.get("outcome_code") != "reconciliation.match"
        or item.get("requested_action") != "none"
        or item.get("ledger_sequence") != count + 1
        or item.get("local_snapshot_sha256") != previous
        or item.get("run_id") != run_id
        for item in reconciliations
    ):
        raise ValueError("reconciliation conflicts")
    r._validate_snapshot_evidence(
        result["portfolio_snapshot_evidence"],
        result=result,
        run_id=run_id,
        terminal_snapshot_sha256=previous,
    )
    if (
        semantic_outcome_sha256(semantic_projection(result)).value
        != result["semantic_outcome_sha256"]
    ):
        raise ValueError("semantic outcome conflicts")


@dataclass(frozen=True, slots=True)
class BacktestReportV2:
    canonical_bytes: bytes
    summary_bytes: bytes
    VERSION: ClassVar[int] = 2

    def __post_init__(self) -> None:
        try:
            doc = r._decode_canonical(self.canonical_bytes, newline=True)
            r._exact_keys(
                doc,
                {
                    "schema",
                    "schema_version",
                    "run_id",
                    "lineage_sha256",
                    "report_generator",
                    "completion",
                    "source",
                    "economics",
                    "field_sources",
                },
            )
            if (
                doc["schema"] != f"ea.backtest-report.v{self.VERSION}"
                or type(doc["schema_version"]) is not int
                or doc["schema_version"] != self.VERSION
            ):
                raise ValueError("V2 report schema conflicts")
            economic = r._exact_keys(
                doc["economics"],
                {
                    "currency",
                    "counts",
                    "initial_funding",
                    "ending_cash",
                    "ending_positions",
                    "final_quantity",
                    "final_position_value",
                    "position_state",
                    "position_outcome",
                    "completed_round_trips",
                    "execution_legs",
                    "fees",
                    "realized_pnl",
                    "gross_unrealized_pnl",
                    "net_pnl",
                    "equity",
                    "total_return",
                    "valuation",
                }
                | ({"entry_fee", "exit_fee"} if self.VERSION == 2 else {"trades", "open_position"}),
            )
            for name in (
                ("final_quantity", "final_position_value", "entry_fee", "exit_fee")
                if self.VERSION == 2
                else ("final_quantity", "final_position_value")
            ):
                CanonicalDecimal(r._text(economic[name]))
            for name in ("realized_pnl", "gross_unrealized_pnl", "net_pnl", "equity"):
                item = r._exact_keys(economic[name], {"amount", "rule"})
                CanonicalDecimal(r._text(item["amount"]))
            r._exact_keys(economic["fees"], {"amount", "count", "currency", "rule"})
            counts = r._exact_keys(economic["counts"], {"orders", "fills"})
            fills = r._integer(counts["fills"])
            orders = r._integer(counts["orders"])
            if not 0 <= fills <= orders <= (2 if self.VERSION == 2 else 512):
                raise ValueError("V2 counts conflict")
            if (
                economic["position_state"] != _position(fills, self.VERSION == 3)[0]
                or economic["position_outcome"] != _position(fills, self.VERSION == 3)[1]
                or type(economic["completed_round_trips"]) is not int
                or economic["completed_round_trips"] != fills // 2
            ):
                raise ValueError("V2 position conflicts")
            legs = economic["execution_legs"]
            if type(legs) is not list or len(legs) != orders:
                raise ValueError("V2 legs conflict")
            for leg in legs:
                r._exact_keys(
                    leg,
                    {
                        "role",
                        "order",
                        "fill",
                        "risk",
                        "outcome",
                        "order_evidence",
                        "order_sha256",
                        "fill_sha256",
                    },
                )
            if self.VERSION == 3:
                _validate_multi_trade_summary(doc)
            if type(self.summary_bytes) is not bytes or not self.summary_bytes.endswith(b"\n"):
                raise ValueError("V2 summary conflicts")
        except (KeyError, TypeError, ValueError):
            raise r.BacktestReportError("V2 report schema is invalid") from None

    @property
    def document(self) -> dict[str, Any]:
        return r._decode_canonical(self.canonical_bytes, newline=True)


def _validate_multi_trade_summary(doc: dict[str, Any]) -> None:
    economic = doc["economics"]
    strategy = doc["source"]["strategy"]
    limit = r._integer(strategy["max_round_trips"])
    if (
        not 1 <= limit <= 256
        or strategy["action_contract"] != "V2"
        or strategy["position_lifecycle"] != "bounded-long-round-trips-v1"
    ):
        raise ValueError("bounded strategy identity conflicts")
    legs = economic["execution_legs"]
    if len(legs) > 2 * limit:
        raise ValueError("bounded leg count conflicts")
    fills: list[Any] = []
    for index, leg in enumerate(legs):
        entry = index % 2 == 0
        if leg["role"] != ("entry" if entry else "exit"):
            raise ValueError("bounded leg role conflicts")
        fill = leg["fill"]
        if fill is None:
            if index != len(legs) - 1 or leg["outcome"] != "expired":
                raise ValueError("unfilled leg ordering conflicts")
            continue
        if (
            leg["outcome"] != "filled"
            or fill["side"] != ("buy" if entry else "sell")
            or fill["quantity"] != leg["order"]["quantity"]
            or fill["order_id"] != leg["order"]["order_id"]
            or (not entry and fill["quantity"] != fills[-1]["quantity"])
        ):
            raise ValueError("full leg evidence conflicts")
        fills.append(fill)
    if len(fills) != economic["counts"]["fills"]:
        raise ValueError("Fill count conflicts")
    instrument = doc["source"]["instrument"]
    multiplier = CanonicalDecimal(instrument["contract_multiplier"])
    quantum = CanonicalDecimal(instrument["currency_quantum"])
    notionals = [
        settle_product(
            CanonicalDecimal(f["price"]), CanonicalDecimal(f["quantity"]), multiplier, quantum
        ).amount
        for f in fills
    ]
    fees = [CanonicalDecimal(f["fees"][0]["amount"]) for f in fills]
    cash = CanonicalDecimal(economic["initial_funding"]["amount"])
    initial = cash
    total_fees = CanonicalDecimal("0")
    for index, (notional, fee) in enumerate(zip(notionals, fees, strict=True)):
        cash = r._subtract(
            r._subtract(cash, notional) if index % 2 == 0 else r._add(cash, notional), fee
        )
        total_fees = r._add(total_fees, fee)
        if cash.coefficient < 0 or fee.coefficient < 0:
            raise ValueError("negative cash or fee")
    quantity = CanonicalDecimal(fills[-1]["quantity"] if len(fills) % 2 else "0")
    value = (
        settle_product(
            CanonicalDecimal(economic["valuation"]["price"]), quantity, multiplier, quantum
        ).amount
        if quantity.coefficient
        else CanonicalDecimal("0")
    )
    equity = r._add(cash, value)
    net = r._subtract(equity, initial)
    unrealized = r._subtract(value, notionals[-1]) if len(fills) % 2 else CanonicalDecimal("0")
    currency = instrument["settlement_currency"]
    positions = (
        [{"quantity": quantity.text, "symbol": instrument["symbol"], "venue": instrument["venue"]}]
        if quantity.coefficient
        else []
    )
    if (
        economic["currency"] != currency
        or economic["initial_funding"]["currency"] != currency
        or economic["ending_cash"] != [{"amount": cash.text, "currency": currency}]
        or economic["ending_positions"] != positions
        or economic["final_quantity"] != quantity.text
        or economic["final_position_value"] != value.text
        or economic["valuation"]["position_value"] != value.text
        or economic["fees"]
        != {
            "amount": total_fees.text,
            "count": len(fees),
            "currency": currency,
            "rule": "per-fill-commission",
        }
        or economic["equity"]["amount"] != equity.text
        or economic["net_pnl"]["amount"] != net.text
        or economic["gross_unrealized_pnl"]["amount"] != unrealized.text
        or economic["total_return"]["value"] != r._ratio(net, initial).text
    ):
        raise ValueError("aggregate accounting conflicts with funding, Fill or valuation evidence")
    expected_trades = []
    realized = CanonicalDecimal("0")
    for index in range(0, len(fills) - 1, 2):
        pnl = r._subtract(
            r._subtract(r._subtract(notionals[index + 1], notionals[index]), fees[index]),
            fees[index + 1],
        )
        realized = r._add(realized, pnl)
        expected_trades.append(
            {
                "entry_order_id": fills[index]["order_id"],
                "entry_fill_id": fills[index]["fill_id"],
                "exit_order_id": fills[index + 1]["order_id"],
                "exit_fill_id": fills[index + 1]["fill_id"],
                "quantity": fills[index]["quantity"],
                "entry_settled_notional": notionals[index].text,
                "exit_settled_notional": notionals[index + 1].text,
                "entry_fee": fees[index].text,
                "exit_fee": fees[index + 1].text,
                "realized_pnl": pnl.text,
            }
        )
    expected_open = None
    if len(fills) % 2:
        value = CanonicalDecimal(economic["final_position_value"])
        expected_open = {
            "entry_order_id": fills[-1]["order_id"],
            "entry_fill_id": fills[-1]["fill_id"],
            "quantity": fills[-1]["quantity"],
            "entry_settled_notional": notionals[-1].text,
            "entry_fee": fees[-1].text,
            "last_valuation": value.text,
            "gross_unrealized_pnl": r._subtract(value, notionals[-1]).text,
        }
    if (
        economic["trades"] != expected_trades
        or economic["open_position"] != expected_open
        or economic["realized_pnl"]["amount"] != realized.text
    ):
        raise ValueError("trade accounting conflicts with Fill evidence")


@dataclass(frozen=True, slots=True)
class BacktestReportV3(BacktestReportV2):
    VERSION: ClassVar[int] = 3


def build_report(*, result: Any, scenario: Any, manifest: Any, records: Any) -> BacktestReportV2:
    price, valuation = r._last_admitted_price(
        scenario, records, expected_source_file_sha256=manifest["data"]["source_file_sha256"]
    )
    summary = economics(result, scenario, price)
    if summary["equity"]["amount"] != result["final_equity"]:
        raise ValueError("final equity conflicts")
    summary["valuation"] = {**valuation, "position_value": summary["final_position_value"]}
    version = 3 if scenario.schema_version == 5 else 2
    document = {
        "schema": f"ea.backtest-report.v{version}",
        "schema_version": version,
        "run_id": result["run_id"],
        "lineage_sha256": result["lineage_sha256"],
        "report_generator": {"distribution": "ea-quant", "version": ea.__version__},
        "completion": {
            "audit_chain_head_sha256": result["audit_chain_head_sha256"],
            "semantic_outcome_sha256": result["semantic_outcome_sha256"],
            "reconciliation": result["reconciliation"],
            "terminal_outcome": "success",
        },
        "source": {
            **{
                k: manifest[k]
                for k in (
                    "code_sha256",
                    "data",
                    "distribution",
                    "execution",
                    "instrument_spec_set_sha256",
                    "randomness",
                    "risk",
                    "runtime",
                )
            },
            "scenario_sha256": scenario.scenario_sha256.value,
            "strategy": json.loads(scenario.canonical_bytes)["strategy"],
            "instrument": json.loads(scenario.canonical_bytes)["instrument"],
        },
        "economics": summary,
        "field_sources": [
            {
                "field": "economics",
                "source": "committed ordered Fill and ledger handoffs",
                "source_version": result["schema"],
                "rule": "ADR0042-accounting" if version == 3 else "ADR0040-accounting",
            },
        ],
    }
    text = "\n".join(
        [
            "Bounded Long Round Trips V1" if version == 3 else "Single Long Round Trip V1",
            f"Position: {result['position_outcome']}",
            f"Realized P&L: {summary['realized_pnl']['amount']}",
            f"Gross unrealized P&L: {summary['gross_unrealized_pnl']['amount']}",
            f"Fees: {summary['fees']['amount']}",
            f"Net P&L: {summary['net_pnl']['amount']}",
            f"Equity: {summary['equity']['amount']} {summary['currency']}",
            "",
        ]
    ).encode()
    return (BacktestReportV3 if version == 3 else BacktestReportV2)(
        r._canonical_json(document) + b"\n", text
    )
