from decimal import Decimal

from ea.strategy.sdk_v2 import PositionState


def validate_parameters(p, context):
    if p["fast_window"] >= p["slow_window"] or Decimal(p["quantity"]) <= 0:
        raise ValueError("invalid parameters")


class Logic:
    def __init__(self, p):
        self.p, self.history, self.previous = p, [], None

    def on_bar(self, bar, position):
        self.history.append(bar.close)
        if len(self.history) < self.p["slow_window"]:
            return {"action": "HOLD"}
        fast = sum(self.history[-self.p["fast_window"] :]) / self.p["fast_window"]
        slow = sum(self.history[-self.p["slow_window"] :]) / self.p["slow_window"]
        above = fast > slow
        previous, self.previous = self.previous, above
        if position.state == PositionState.FLAT_INITIAL and previous is False and above:
            return {"action": "ENTER_LONG", "quantity": self.p["quantity"]}
        if position.state == PositionState.LONG_OPEN and not above:
            return {"action": "EXIT_LONG"}
        return {"action": "HOLD"}


def create_logic(p):
    return Logic(p)
