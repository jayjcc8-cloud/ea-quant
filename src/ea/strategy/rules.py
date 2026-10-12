"""Closed price-entry rules v1. No code, paths, offsets or dynamic lookup."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Protocol

from ea.core.economics import CanonicalDecimal
from ea.core.market_data import Adjustment, MarketDataEnvelope

DEFAULT_RULE = (
    '{"version":1,"when":{"op":"gt","left":{"kind":"price","field":"close"}'
    ',"right":{"kind":"sma","window":2}},"action":"ENTER_LONG"}'
)


@dataclass(frozen=True)
class PriceRule:
    document: dict[str, Any]
    history: int

    @property
    def canonical(self) -> str:
        return json.dumps(self.document, sort_keys=True, separators=(",", ":"))


def decode_rule(value: object) -> PriceRule:
    if type(value) is not str or len(value.encode()) > 4096:
        raise ValueError("rule must be a JSON string of at most 4096 bytes")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate rule field")
            result[key] = item
        return result

    try:
        document = json.loads(value, object_pairs_hook=pairs)
    except (ValueError, RecursionError):
        raise ValueError("invalid rule JSON") from None
    count, history = 0, 1

    def closed(node: Any, keys: set[str]) -> None:
        if type(node) is not dict or set(node) != keys:
            raise ValueError("unsupported rule fields")

    def visit(node: Any, depth: int, *, operand: bool = False) -> None:
        nonlocal count, history
        count += 1
        if count > 32 or depth > 6:
            raise ValueError("rule exceeds 32 nodes or depth 6")
        if type(node) is not dict:
            raise ValueError("rule node must be an object")
        if operand:
            if node.get("kind") == "price":
                closed(node, {"kind", "field"})
                if node["field"] not in ("open", "high", "low", "close"):
                    raise ValueError("unsupported price field")
            elif node.get("kind") == "sma":
                closed(node, {"kind", "window"})
                if type(node["window"]) is not int or not 1 <= node["window"] <= 64:
                    raise ValueError("SMA window must be integer 1..64")
                history = max(history, node["window"])
            else:
                raise ValueError("unsupported indicator")
        elif node.get("op") in ("gt", "ge", "lt", "le", "eq"):
            closed(node, {"op", "left", "right"})
            visit(node["left"], depth + 1, operand=True)
            visit(node["right"], depth + 1, operand=True)
        elif node.get("op") in ("all", "any"):
            closed(node, {"op", "args"})
            if type(node["args"]) is not list or not 2 <= len(node["args"]) <= 8:
                raise ValueError("boolean args must have 2..8 conditions")
            for child in node["args"]:
                visit(child, depth + 1)
        elif node.get("op") == "not":
            closed(node, {"op", "arg"})
            visit(node["arg"], depth + 1)
        else:
            raise ValueError("unsupported condition")

    closed(document, {"version", "when", "action"})
    if type(document["version"]) is not int or document["version"] != 1:
        raise ValueError("unsupported rule version")
    if document["action"] != "ENTER_LONG":
        raise ValueError("only ENTER_LONG once, otherwise HOLD is supported")
    visit(document["when"], 1)
    return PriceRule(document, history)


class RulePrices(Protocol):
    @property
    def open(self) -> Any: ...

    @property
    def high(self) -> Any: ...

    @property
    def low(self) -> Any: ...

    @property
    def close(self) -> Any: ...


class PriceRuleLogic:
    """One bounded window; upstream runtime admits only active, visible bars."""

    def __init__(self, parameters: Mapping[str, int | str]) -> None:
        self.rule = decode_rule(parameters["rule"])
        self.quantity = CanonicalDecimal(str(parameters["target_quantity"]))
        self.closes: list[Fraction] = []
        self.entered = False

    def on_event(self, event: MarketDataEnvelope) -> CanonicalDecimal | None:
        if self.entered or event.revision != 0 or event.payload.adjustment is not Adjustment.RAW:
            return None
        return self.on_prices(event.payload)

    def on_prices(self, prices: RulePrices) -> CanonicalDecimal | None:
        """Shared evaluator for runtime-admitted visible bars, never future data."""
        if self.entered:
            return None
        self.closes.append(Fraction(str(prices.close)))
        self.closes = self.closes[-self.rule.history :]
        if len(self.closes) < self.rule.history:
            return None  # All operands must be ready, including under NOT/ANY.

        def operand(node: dict[str, Any]) -> Fraction:
            if node["kind"] == "price":
                return Fraction(str(getattr(prices, node["field"])))
            window = int(node["window"])
            return sum(self.closes[-window:], Fraction()) / window

        def condition(node: dict[str, Any]) -> bool:
            op = node["op"]
            if op == "not":
                return not condition(node["arg"])
            if op in ("all", "any"):
                results = [condition(child) for child in node["args"]]
                return all(results) if op == "all" else any(results)
            left, right = operand(node["left"]), operand(node["right"])
            return {
                "gt": left > right,
                "ge": left >= right,
                "lt": left < right,
                "le": left <= right,
                "eq": left == right,
            }[op]

        if not condition(self.rule.document["when"]):
            return None
        self.entered = True
        return self.quantity
