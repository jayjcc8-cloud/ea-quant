"""Strict BacktestScenario v1 loading and semantic identity."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Self, cast

import yaml
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, ValidationError, model_validator
from pydantic_core import PydanticCustomError
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

from ea.config.diagnostics import escape_diagnostic_label
from ea.core import (
    Adjustment,
    CanonicalDecimal,
    ExecutionPolicyId,
    ExecutionPolicyRef,
    Instrument,
    InstrumentExecutionSpec,
    InstrumentExecutionSpecSet,
    InstrumentSpecId,
    InstrumentSpecSetId,
    PriceDomain,
    ReplayWindow,
    SettlementCurrency,
    Sha256Digest,
    VenueId,
    build_instrument_spec_set,
    require_positive,
    require_quantized,
)
from ea.core.commission import commission_policy_identity, require_commission_bps
from ea.core.economics import EconomicValidationError
from ea.core.run import RunContractError
from ea.data import HistoricalMarketDataError, Phase1HistoricalDataset, read_phase1_ohlcv_csv
from ea.product.identity import BacktestRandomness
from ea.strategy.registry import BUILTIN_STRATEGIES, project_parameters, resolve_parameters

_SCENARIO_DIGEST_DOMAIN = b"ea.backtest-scenario.v1\0"
_CANONICALIZATION = "ea-backtest-scenario-v1"
_ACCEPTED_EXECUTION_POLICY = ExecutionPolicyRef(
    ExecutionPolicyId("phase1.next-bar-close.v1"),
    Sha256Digest("1" * 64),
)


class BacktestScenarioError(ValueError):
    """Safe validation failure for a selected BacktestScenario."""


class BacktestStrategyId(StrEnum):
    ALWAYS_FLAT = "always-flat-v1"
    BOUNDED_LONG = "bounded-long-v1"


@dataclass(frozen=True, slots=True)
class RegisteredStrategyId:
    value: str


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _FingerprintInput(_StrictModel):
    sha256: StrictStr
    record_count: StrictInt


class _DataInput(_StrictModel):
    path: StrictStr
    start_utc: StrictStr
    end_utc: StrictStr
    fingerprint: _FingerprintInput


class _InstrumentInput(_StrictModel):
    venue: StrictStr
    symbol: StrictStr
    specification_id: StrictStr
    specification_set_id: StrictStr
    settlement_currency: StrictStr
    price_quantum: StrictStr
    quantity_quantum: StrictStr
    currency_quantum: StrictStr
    contract_multiplier: StrictStr


class _StrategyInput(_StrictModel):
    id: BacktestStrategyId
    target_quantity: StrictStr | None = None
    entry_delay_bars: StrictInt = 0

    @model_validator(mode="after")
    def validate_parameters(self) -> Self:
        if self.id is BacktestStrategyId.ALWAYS_FLAT and self.target_quantity is not None:
            raise PydanticCustomError(
                "incompatible_strategy_parameters",
                "always-flat-v1 forbids target_quantity",
            )
        if self.entry_delay_bars < 0:
            raise PydanticCustomError(
                "out_of_range_strategy_parameter",
                "entry_delay_bars must be at least 0",
            )
        if self.id is BacktestStrategyId.ALWAYS_FLAT and self.entry_delay_bars != 0:
            raise PydanticCustomError(
                "incompatible_strategy_parameters",
                "always-flat-v1 forbids entry_delay_bars",
            )
        if self.id is BacktestStrategyId.BOUNDED_LONG and self.target_quantity is None:
            raise PydanticCustomError(
                "incompatible_strategy_parameters",
                "bounded-long-v1 requires target_quantity",
            )
        return self


class _FundingInput(_StrictModel):
    currency: StrictStr
    initial_cash: StrictStr


class _RiskInput(_StrictModel):
    max_order_quantity: StrictStr
    max_position_quantity: StrictStr
    max_notional: StrictStr


class _CommissionInput(_StrictModel):
    policy: Literal["deterministic-commission-v1"]
    commission_bps: StrictStr


class _ExecutionInput(_StrictModel):
    policy: Literal["phase1.next-bar-close.v1"]
    commission: _CommissionInput | None = None


class _ScenarioInput(_StrictModel):
    schema_version: StrictInt
    data: _DataInput
    instrument: _InstrumentInput
    strategy: _StrategyInput
    funding: _FundingInput
    risk: _RiskInput
    execution: _ExecutionInput
    randomness_profile: Literal["none"]

    @model_validator(mode="after")
    def require_v1(self) -> Self:
        if self.schema_version != 1:
            raise PydanticCustomError(
                "unsupported_schema_version",
                "only BacktestScenario schema version 1 is supported",
            )
        return self


class _StrategyInputV2(_StrictModel):
    id: StrictStr
    version: StrictInt
    parameters: dict[StrictStr, Any]


class BacktestScenarioV2(_StrictModel):
    schema_version: Literal[2]
    data: _DataInput
    instrument: _InstrumentInput
    strategy: _StrategyInputV2
    funding: _FundingInput
    risk: _RiskInput
    execution: _ExecutionInput
    randomness_profile: Literal["none"]


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from error
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found a duplicate mapping key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


@dataclass(frozen=True, slots=True)
class LoadedBacktestScenario:
    """Validated typed scenario and captured local data evidence."""

    scenario_path: Path
    data_path: Path
    dataset: Phase1HistoricalDataset
    replay_window: ReplayWindow
    instrument: Instrument
    spec_set: InstrumentExecutionSpecSet
    strategy_id: BacktestStrategyId | RegisteredStrategyId
    target_quantity: CanonicalDecimal | None
    entry_delay_bars: int
    entry_delay_bars_maximum: int | None
    funding_currency: SettlementCurrency
    initial_cash: CanonicalDecimal
    max_order_quantity: CanonicalDecimal
    max_position_quantity: CanonicalDecimal
    max_notional: CanonicalDecimal
    execution_policy: ExecutionPolicyRef
    randomness: BacktestRandomness
    canonical_bytes: bytes
    scenario_sha256: Sha256Digest

    @property
    def strategy_version(self) -> int:
        return int(json.loads(self.canonical_bytes)["strategy"].get("version", 1))

    @property
    def strategy_parameters(self) -> dict[str, int | str]:
        return project_parameters(json.loads(self.canonical_bytes)["strategy"])

    @property
    def schema_version(self) -> int:
        return int(json.loads(self.canonical_bytes)["schema_version"])


def _safe_validation_message(error: ValidationError) -> str:
    problems: list[str] = []
    for item in error.errors(include_url=False):
        location = ".".join(escape_diagnostic_label(part) for part in item["loc"]) or "<root>"
        if item["type"] == "extra_forbidden":
            problems.append(f"unknown field '{location}'")
        else:
            problems.append(f"invalid field '{location}' ({item['type']})")
    return "; ".join(problems)


def _resolve_file(path: Path, *, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
        is_file = resolved.is_file()
    except (OSError, RuntimeError, ValueError):
        raise BacktestScenarioError(f"{label} cannot be resolved") from None
    if not is_file:
        raise BacktestScenarioError(f"{label} must be a regular file")
    return resolved


def _load_document(path: Path) -> dict[str, Any]:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise BacktestScenarioError("scenario file cannot be read") from None
    try:
        document = yaml.load(content, Loader=_UniqueKeySafeLoader)
    except (yaml.YAMLError, ValueError, RecursionError) as error:
        mark = getattr(error, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise BacktestScenarioError(f"scenario contains invalid YAML{location}") from None
    if document is None:
        raise BacktestScenarioError("scenario file is empty")
    if not isinstance(document, dict):
        raise BacktestScenarioError("scenario root must be a mapping")
    return document


def _utc(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        raise BacktestScenarioError(f"{field} must be canonical UTC") from None
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != value:
        raise BacktestScenarioError(f"{field} must be canonical UTC")
    return parsed


def _decimal(value: str, *, field: str) -> CanonicalDecimal:
    try:
        return CanonicalDecimal(value)
    except EconomicValidationError:
        raise BacktestScenarioError(f"{field} must be an ea-decimal-v1 string") from None


def _positive(value: CanonicalDecimal, *, field: str) -> CanonicalDecimal:
    try:
        return require_positive(value, field_name=field)
    except EconomicValidationError:
        raise BacktestScenarioError(f"{field} must be strictly positive") from None


def _quantized(
    value: CanonicalDecimal,
    quantum: CanonicalDecimal,
    *,
    field: str,
) -> CanonicalDecimal:
    try:
        return require_quantized(value, quantum, field_name=field)
    except EconomicValidationError:
        raise BacktestScenarioError(f"{field} is not quantized") from None


def _canonical_bytes(
    model: _ScenarioInput | BacktestScenarioV2,
    *,
    dataset: Phase1HistoricalDataset,
) -> bytes:
    document = model.model_dump(mode="json")
    if model.execution.commission is None:
        document["execution"].pop("commission")
    data = dict(document["data"])
    data.pop("path")
    document["data"] = data
    document["canonicalization"] = (
        _CANONICALIZATION if model.schema_version == 1 else "ea-backtest-scenario-v2"
    )
    document["data"]["fingerprint"] = {
        "record_count": dataset.selection.fingerprint.record_count,
        "sha256": dataset.selection.fingerprint.sha256.value,
    }
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _require_market_price_domain(
    dataset: Phase1HistoricalDataset,
    domain: PriceDomain,
) -> None:
    for event in dataset.selection.events:
        prices = (
            event.payload.open,
            event.payload.high,
            event.payload.low,
            event.payload.close,
        )
        if domain is PriceDomain.POSITIVE and any(price <= 0 for price in prices):
            raise BacktestScenarioError("market data price domain conflicts with instrument")
        if domain is PriceDomain.NON_NEGATIVE and any(price < 0 for price in prices):
            raise BacktestScenarioError("market data price domain conflicts with instrument")


def _next_bar_entry_delay_maximum(dataset: Phase1HistoricalDataset) -> int | None:
    """Return the latest canonical market-root index with a later executable bar."""
    events = dataset.selection.events
    if len(events) < 2:
        return None
    latest_eligible_event_time: dict[Instrument, datetime] = {}
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        future_event_time = latest_eligible_event_time.get(event.payload.instrument)
        if future_event_time is not None and future_event_time > event.available_at:
            return index
        if event.payload.adjustment is Adjustment.RAW and event.revision == 0:
            latest = latest_eligible_event_time.get(event.payload.instrument)
            latest_eligible_event_time[event.payload.instrument] = max(
                event.event_time,
                latest or event.event_time,
            )
    return None


def _entry_history_bars(dataset: Phase1HistoricalDataset) -> int:
    maximum = _next_bar_entry_delay_maximum(dataset)
    if maximum is None:
        return 0
    return sum(
        event.revision == 0 and event.payload.adjustment is Adjustment.RAW
        for event in dataset.selection.events[: maximum + 1]
    )


def load_backtest_scenario(path: Path) -> LoadedBacktestScenario:
    """Load, capture, and cross-validate one strict BacktestScenario v1."""
    if not isinstance(path, Path):
        raise BacktestScenarioError("scenario path must be a pathlib.Path")
    scenario_path = _resolve_file(path, label="scenario path")
    document = _load_document(scenario_path)
    try:
        model: _ScenarioInput | BacktestScenarioV2 = (
            BacktestScenarioV2.model_validate(document)
            if type(document.get("schema_version")) is int and document["schema_version"] == 2
            else _ScenarioInput.model_validate(document)
        )
    except ValidationError as error:
        raise BacktestScenarioError(_safe_validation_message(error)) from None

    try:
        replay_window = ReplayWindow(
            _utc(model.data.start_utc, field="data.start_utc"),
            _utc(model.data.end_utc, field="data.end_utc"),
        )
    except RunContractError as error:
        raise BacktestScenarioError(str(error)) from None
    data_path = _resolve_file(scenario_path.parent / model.data.path, label="data.path")
    try:
        dataset = read_phase1_ohlcv_csv(data_path, replay_window=replay_window)
    except HistoricalMarketDataError as error:
        raise BacktestScenarioError(f"data.path failed validation ({error.code.value})") from None
    try:
        expected_sha256 = Sha256Digest(model.data.fingerprint.sha256)
    except RunContractError:
        raise BacktestScenarioError("data.fingerprint.sha256 must be canonical SHA-256") from None
    expected_fingerprint = (expected_sha256, model.data.fingerprint.record_count)
    actual_fingerprint = (
        dataset.selection.fingerprint.sha256,
        dataset.selection.fingerprint.record_count,
    )
    if expected_fingerprint != actual_fingerprint:
        raise BacktestScenarioError("data fingerprint conflicts with captured replay selection")

    try:
        instrument = Instrument(VenueId(model.instrument.venue), model.instrument.symbol)
        currency = SettlementCurrency(model.instrument.settlement_currency)
        price_quantum = _positive(
            _decimal(model.instrument.price_quantum, field="instrument.price_quantum"),
            field="instrument.price_quantum",
        )
        quantity_quantum = _positive(
            _decimal(model.instrument.quantity_quantum, field="instrument.quantity_quantum"),
            field="instrument.quantity_quantum",
        )
        currency_quantum = _positive(
            _decimal(model.instrument.currency_quantum, field="instrument.currency_quantum"),
            field="instrument.currency_quantum",
        )
        contract_multiplier = _positive(
            _decimal(model.instrument.contract_multiplier, field="instrument.contract_multiplier"),
            field="instrument.contract_multiplier",
        )
        spec_set = build_instrument_spec_set(
            InstrumentSpecSetId(model.instrument.specification_set_id),
            (
                InstrumentExecutionSpec(
                    instrument=instrument,
                    specification_id=InstrumentSpecId(model.instrument.specification_id),
                    price_quantum=price_quantum,
                    quantity_quantum=quantity_quantum,
                    settlement_currency=currency,
                    currency_quantum=currency_quantum,
                    contract_multiplier=contract_multiplier,
                    price_domain=PriceDomain.POSITIVE,
                ),
            ),
        )
    except (EconomicValidationError, ValueError) as error:
        raise BacktestScenarioError(f"instrument is invalid ({type(error).__name__})") from None
    if any(event.payload.instrument != instrument for event in dataset.selection.events):
        raise BacktestScenarioError("instrument conflicts with captured market data")
    _require_market_price_domain(
        dataset,
        spec_set.require(instrument).price_domain,
    )

    try:
        funding_currency = SettlementCurrency(model.funding.currency)
    except EconomicValidationError:
        raise BacktestScenarioError("funding.currency is invalid") from None
    if funding_currency != currency:
        raise BacktestScenarioError(
            "funding currency conflicts with instrument settlement currency"
        )
    initial_cash = _quantized(
        _positive(
            _decimal(model.funding.initial_cash, field="funding.initial_cash"),
            field="funding.initial_cash",
        ),
        currency_quantum,
        field="funding.initial_cash",
    )
    max_order = _quantized(
        _positive(
            _decimal(model.risk.max_order_quantity, field="risk.max_order_quantity"),
            field="risk.max_order_quantity",
        ),
        quantity_quantum,
        field="risk.max_order_quantity",
    )
    max_position = _quantized(
        _positive(
            _decimal(model.risk.max_position_quantity, field="risk.max_position_quantity"),
            field="risk.max_position_quantity",
        ),
        quantity_quantum,
        field="risk.max_position_quantity",
    )
    max_notional = _quantized(
        _positive(
            _decimal(model.risk.max_notional, field="risk.max_notional"),
            field="risk.max_notional",
        ),
        currency_quantum,
        field="risk.max_notional",
    )
    target = None
    if isinstance(model, _ScenarioInput) and model.strategy.target_quantity is not None:
        target = _quantized(
            _positive(
                _decimal(model.strategy.target_quantity, field="strategy.target_quantity"),
                field="strategy.target_quantity",
            ),
            quantity_quantum,
            field="strategy.target_quantity",
        )
    entry_delay_maximum: int | None = None
    if isinstance(model, _ScenarioInput) and model.strategy.id is BacktestStrategyId.BOUNDED_LONG:
        entry_delay_maximum = _next_bar_entry_delay_maximum(dataset)
        if entry_delay_maximum is None:
            raise BacktestScenarioError(
                "bounded-long-v1 requires a market bar with an executable next bar"
            )
        if model.strategy.entry_delay_bars > entry_delay_maximum:
            raise BacktestScenarioError(
                f"strategy.entry_delay_bars must be at most {entry_delay_maximum}"
            )
    if isinstance(model, BacktestScenarioV2):
        try:
            normalized = BUILTIN_STRATEGIES.get(
                model.strategy.id, model.strategy.version
            ).normalize(model.strategy.parameters)
            resolve_parameters(
                model.strategy.id,
                model.strategy.version,
                normalized,
                quantity_quantum=quantity_quantum,
                last_entry_index=_next_bar_entry_delay_maximum(dataset),
                history_bars=_entry_history_bars(dataset),
            )
            model = model.model_copy(
                update={"strategy": model.strategy.model_copy(update={"parameters": normalized})}
            )
        except ValueError as error:
            raise BacktestScenarioError(str(error)) from None
    execution_policy = _ACCEPTED_EXECUTION_POLICY
    if model.execution.commission is not None:
        try:
            bps = require_commission_bps(
                CanonicalDecimal(model.execution.commission.commission_bps)
            )
            identifier, digest = commission_policy_identity(bps)
            execution_policy = ExecutionPolicyRef(
                ExecutionPolicyId(identifier), Sha256Digest(digest)
            )
        except EconomicValidationError as error:
            raise BacktestScenarioError(str(error)) from None
    canonical = _canonical_bytes(model, dataset=dataset)
    return LoadedBacktestScenario(
        scenario_path=scenario_path,
        data_path=data_path,
        dataset=dataset,
        replay_window=replay_window,
        instrument=instrument,
        spec_set=spec_set,
        strategy_id=(
            BacktestStrategyId(model.strategy.id)
            if isinstance(model, _ScenarioInput)
            else RegisteredStrategyId(model.strategy.id)
        ),
        target_quantity=target,
        entry_delay_bars=model.strategy.entry_delay_bars
        if isinstance(model, _ScenarioInput)
        else 0,
        entry_delay_bars_maximum=entry_delay_maximum,
        funding_currency=funding_currency,
        initial_cash=initial_cash,
        max_order_quantity=max_order,
        max_position_quantity=max_position,
        max_notional=max_notional,
        execution_policy=execution_policy,
        randomness=BacktestRandomness(),
        canonical_bytes=canonical,
        scenario_sha256=Sha256Digest(
            sha256(scenario_digest_domain(model.schema_version) + canonical).hexdigest()
        ),
    )


def parameterize_backtest_scenario(
    scenario: LoadedBacktestScenario,
    *,
    initial_cash: str,
    quantity: str | None,
    entry_delay_bars: int | None = None,
) -> LoadedBacktestScenario:
    """Create one validated immutable run input from a registered scenario."""
    if type(scenario) is not LoadedBacktestScenario:
        raise BacktestScenarioError("scenario must be a loaded BacktestScenario")
    if scenario.schema_version != 1:
        raise BacktestScenarioError("Scenario V2 requires the generic parameter map")
    specification = scenario.spec_set.require(scenario.instrument)
    cash = _quantized(
        _positive(
            _decimal(initial_cash, field="initial_cash"),
            field="initial_cash",
        ),
        specification.currency_quantum,
        field="initial_cash",
    )
    target: CanonicalDecimal | None = None
    delay = scenario.entry_delay_bars if entry_delay_bars is None else entry_delay_bars
    if type(delay) is not int:
        raise BacktestScenarioError("entry_delay_bars must be an integer")
    if scenario.strategy_id is BacktestStrategyId.ALWAYS_FLAT:
        if quantity is not None:
            raise BacktestScenarioError("always-flat-v1 forbids quantity")
        if delay != 0:
            raise BacktestScenarioError("always-flat-v1 forbids entry_delay_bars")
    else:
        if quantity is None:
            raise BacktestScenarioError("bounded-long-v1 requires quantity")
        target = _quantized(
            _positive(
                _decimal(quantity, field="quantity"),
                field="quantity",
            ),
            specification.quantity_quantum,
            field="quantity",
        )
        maximum = scenario.entry_delay_bars_maximum
        if maximum is None:
            raise BacktestScenarioError("bounded-long-v1 has no executable entry bar")
        if delay < 0 or delay > maximum:
            raise BacktestScenarioError(f"entry_delay_bars must be between 0 and {maximum}")

    document = cast(dict[str, object], json.loads(scenario.canonical_bytes))
    funding = cast(dict[str, object], document["funding"])
    strategy = cast(dict[str, object], document["strategy"])
    funding["initial_cash"] = cash.text
    strategy["target_quantity"] = None if target is None else target.text
    strategy["entry_delay_bars"] = delay
    canonical = json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return replace(
        scenario,
        initial_cash=cash,
        target_quantity=target,
        entry_delay_bars=delay,
        canonical_bytes=canonical,
        scenario_sha256=Sha256Digest(sha256(_SCENARIO_DIGEST_DOMAIN + canonical).hexdigest()),
    )


__all__ = [
    "BacktestScenarioV2",
    "parameterize_strategy_scenario",
    "BacktestScenarioError",
    "BacktestStrategyId",
    "LoadedBacktestScenario",
    "load_backtest_scenario",
    "parameterize_backtest_scenario",
]


def scenario_digest_domain(version: int) -> bytes:
    if version not in (1, 2):
        raise BacktestScenarioError("unsupported scenario digest version")
    return f"ea.backtest-scenario.v{version}\0".encode("ascii")


def parameterize_strategy_scenario(
    scenario: LoadedBacktestScenario,
    *,
    initial_cash: str,
    parameters: dict[str, object],
) -> LoadedBacktestScenario:
    """Normalize an entire closed map using the registered strategy contract."""
    specification = scenario.spec_set.require(scenario.instrument)
    try:
        normalized = BUILTIN_STRATEGIES.get(
            scenario.strategy_id.value, scenario.strategy_version
        ).normalize(parameters)
        resolve_parameters(
            scenario.strategy_id.value,
            scenario.strategy_version,
            normalized,
            quantity_quantum=specification.quantity_quantum,
            last_entry_index=_next_bar_entry_delay_maximum(scenario.dataset),
            history_bars=_entry_history_bars(scenario.dataset),
        )
    except ValueError as error:
        raise BacktestScenarioError(str(error)) from None
    if scenario.schema_version == 1:
        return parameterize_backtest_scenario(
            scenario,
            initial_cash=initial_cash,
            quantity=cast(str | None, normalized.get("target_quantity")),
            entry_delay_bars=cast(int, normalized.get("entry_delay_bars", 0)),
        )
    cash = _quantized(
        _positive(_decimal(initial_cash, field="initial_cash"), field="initial_cash"),
        specification.currency_quantum,
        field="initial_cash",
    )
    document = json.loads(scenario.canonical_bytes)
    document["funding"]["initial_cash"] = cash.text
    document["strategy"]["parameters"] = normalized
    canonical = json.dumps(
        document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    return replace(
        scenario,
        initial_cash=cash,
        canonical_bytes=canonical,
        scenario_sha256=Sha256Digest(sha256(scenario_digest_domain(2) + canonical).hexdigest()),
    )
