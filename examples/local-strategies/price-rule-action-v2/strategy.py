"""Explicit Action V2 incarnation of the closed entry-only research rule.

Uses the distribution's exact evaluator; accepted evidence binds both code and
these package bytes. No liquidation, repeated entry or hidden exit is added.
"""

from ea.core.economics import CanonicalDecimal, require_positive
from ea.strategy.rules import PriceRuleLogic, decode_rule
from ea.strategy.sdk_v2 import PositionState


def validate_parameters(parameters, context):
    rule = decode_rule(parameters["rule"])
    require_positive(CanonicalDecimal(parameters["target_quantity"]), field_name="quantity")
    if context.history_bar_count < rule.history or context.last_entry_index is None:
        raise ValueError("rule requires ready history and an executable next bar")


class Logic:
    # Existing Paper recovery restores economics, but not an arbitrary strategy's
    # rolling market window. Reject continuation rather than silently reset it.
    requires_market_history_on_restore = True

    def __init__(self, parameters):
        self.rule = PriceRuleLogic(parameters)

    def on_bar(self, bar, position):
        if position.state != PositionState.FLAT_INITIAL:
            return {"action": "HOLD"}
        quantity = self.rule.on_prices(bar)
        if quantity is None:
            return {"action": "HOLD"}
        return {"action": "ENTER_LONG", "quantity": quantity.text}


def create_logic(parameters):
    return Logic(parameters)
