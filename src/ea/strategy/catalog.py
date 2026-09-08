"""Additive research catalog; local code is never registered in the built-in authority."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ea.core.economics import CanonicalDecimal, require_positive
from ea.core.market_data import Adjustment, MarketDataEnvelope
from ea.strategy.package import (
    StrategyPackageError,
    StrategyPackageV1,
    read_regular,
    validate_package,
)
from ea.strategy.registry import BUILTIN_STRATEGIES, EntryLogic, ParameterValue, StrategyEntryV1
from ea.strategy.sdk_v1 import StrategyBarV1, StrategyDecisionV1, StrategyValidationContextV1


class LocalEntryLogic(EntryLogic):
    def __init__(
        self, package: StrategyPackageV1, parameters: Mapping[str, ParameterValue]
    ) -> None:
        self.package = package
        try:
            self.logic = package.module()["create_logic"](MappingProxyType(dict(parameters)))
            if not callable(getattr(self.logic, "on_bar", None)):
                raise ValueError
        except BaseException:
            raise StrategyPackageError("strategy factory failed") from None

    def on_event(self, event: MarketDataEnvelope) -> CanonicalDecimal | None:
        if event.revision != 0 or event.payload.adjustment is not Adjustment.RAW:
            return None
        payload = event.payload
        bar = StrategyBarV1(
            event.event_time,
            event.available_at,
            *(
                Decimal(str(getattr(payload, name)))
                for name in ("open", "high", "low", "close", "volume")
            ),
        )
        try:
            decision = self.logic.on_bar(bar)
            if decision is None:
                return None
            if type(decision) is not StrategyDecisionV1 or self.package.outcome_mode == "no_entry":
                raise ValueError
            if type(decision.target_quantity) is not str:
                raise ValueError
            return require_positive(
                CanonicalDecimal(decision.target_quantity), field_name="target_quantity"
            )
        except BaseException:
            raise StrategyPackageError("strategy decision failed") from None


def package_entry(package: StrategyPackageV1) -> StrategyEntryV1:
    def validate(parameters: Mapping[str, ParameterValue]) -> None:
        pass  # Context-dependent local validation is invoked after generic normalization.

    def factory(parameters: Mapping[str, ParameterValue]) -> EntryLogic:
        return LocalEntryLogic(package, parameters)

    def outcome(orders: int, fills: int) -> None:
        allowed = {
            "no_entry": {(0, 0)},
            "required_single_long_entry": {(1, 1)},
            "optional_single_long_entry": {(0, 0), (1, 1)},
        }[package.outcome_mode]
        if (orders, fills) not in allowed:
            raise ValueError("strategy outcome conflicts")

    return StrategyEntryV1(package.descriptor, validate, factory, outcome)


def validate_context(
    package: StrategyPackageV1,
    parameters: Mapping[str, ParameterValue],
    context: StrategyValidationContextV1,
) -> None:
    try:
        package.module()["validate_parameters"](MappingProxyType(dict(parameters)), context)
    except BaseException:
        raise StrategyPackageError("strategy parameter validation failed") from None


class ResearchStrategyCatalogV1:
    def __init__(self, packages: tuple[StrategyPackageV1, ...] = ()) -> None:
        builtin_ids = {d.strategy_id for d in BUILTIN_STRATEGIES.descriptors()}
        ids = [p.descriptor.strategy_id for p in packages]
        package_ids = [p.identity.package_id for p in packages]
        if (
            len(set(ids)) != len(ids)
            or len(set(package_ids)) != len(package_ids)
            or builtin_ids.intersection(ids)
        ):
            raise StrategyPackageError("duplicate or shadowed strategy/package identity")
        self.packages = packages

    @classmethod
    def from_root(cls, root: Path | None) -> ResearchStrategyCatalogV1:
        if root is None:
            return cls()
        try:
            if not root.is_absolute() or not root.is_dir():
                raise ValueError
            paths = sorted(root.glob("*.eastrategy"))
            if len(paths) > 100:
                raise ValueError
            return cls(tuple(validate_package(read_regular(path, root)) for path in paths))
        except (OSError, ValueError):
            raise StrategyPackageError("strategy root validation failed") from None

    def get(self, strategy_id: str, version: int) -> StrategyEntryV1:
        # ID alone can only select a distribution-owned implementation.
        return BUILTIN_STRATEGIES.get(strategy_id, version)

    def package(self, strategy: Mapping[str, Any]) -> StrategyPackageV1:
        source = strategy.get("source", {})
        for package in self.packages:
            if (
                source
                == {
                    "kind": "local-package",
                    "package_id": package.identity.package_id,
                    "artifact_sha256": package.identity.artifact_sha256,
                }
                and strategy["id"] == package.descriptor.strategy_id
                and type(strategy.get("version")) is int
                and strategy["version"] == package.descriptor.strategy_version
            ):
                return package
        raise StrategyPackageError("exact strategy package identity is unavailable")

    def resolve(self, strategy: Mapping[str, Any]) -> StrategyEntryV1:
        if "source" in strategy:
            return package_entry(self.package(strategy))
        return self.get(strategy["id"], strategy.get("version", 1))
