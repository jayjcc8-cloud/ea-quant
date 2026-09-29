"""RADIAN research tasks: persistent, recoverable idea → spec → backtest research.

A research task keeps the raw idea, the structured spec (draft and confirmed
versions), data and research scope, a frozen budget, recorded usage, and the
real backtest jobs it started. Every step is durable before and after it runs,
so a retry resumes the failed step instead of restarting the research.

The orchestrator's only executable actions are the whitelisted tools below.
Object identity and parameters are validated in code against the registered
scenario catalog — never by prompt alone — and no path here reaches Paper,
funds, or order authority.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from ea.web.llm import (
    AnthropicProvider,
    ModelNotConfigured,
    ModelProviderError,
)
from ea.web.service import WebService
from ea.web.settings import RadianSettings, RadianSettingsError

_SCHEMA = "radian.research-task.v1"
_ACTIVE_STATES = frozenset({"drafting", "awaiting_confirmation", "confirmed", "running"})
_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "unsupported"})
_MAX_IDEA_BYTES = 4000
_MAX_ASSUMPTION_BYTES = 300
_MAX_ASSUMPTIONS = 12
_MAX_RATIONALE_BYTES = 2000
_MAX_GAP_BYTES = 2000
_MAX_TOOL_ROUNDS = 4
_MAX_CATALOG_ENTRIES = 40
_DECIMAL_PATTERN = re.compile(r"^-?\d{1,20}(\.\d{1,20})?$")


class ResearchTaskError(ValueError):
    """A research task request is invalid or conflicts with task state."""


class TaskNotFoundError(ResearchTaskError):
    """No task with this identity exists."""


class TaskConflictError(ResearchTaskError):
    """An equivalent active task already exists."""


class InvalidSpecError(ResearchTaskError):
    """A proposed or edited spec does not match the registered surface."""


class BudgetExceededError(ResearchTaskError):
    """The task's frozen budget would be exceeded."""


class TaskStateError(ResearchTaskError):
    """The action is not valid in the task's current state."""


@dataclass(frozen=True, slots=True)
class ResearchSpec:
    """One structured spec: exactly the fields the product can execute."""

    strategy_id: str
    instrument_venue: str
    instrument_symbol: str
    initial_cash: str
    rationale: str
    scenario_id: str | None = None
    dataset_id: str | None = None
    source_sha256: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    assumptions: tuple[str, ...] = ()

    def document(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "instrument": {"venue": self.instrument_venue, "symbol": self.instrument_symbol},
            "initial_cash": self.initial_cash,
            "rationale": self.rationale,
            "scenario_id": self.scenario_id,
            "dataset_id": self.dataset_id,
            "source_sha256": self.source_sha256,
            "parameters": self.parameters,
            "assumptions": list(self.assumptions),
        }


@dataclass(frozen=True, slots=True)
class ResearchTask:
    """One persisted task; mutation happens only through the store."""

    task_id: str
    created_at: str
    updated_at: str
    status: str
    raw_idea: str
    gap: str | None
    budget: dict[str, int]
    usage: dict[str, int]
    spec_draft: ResearchSpec | None
    spec_confirmed: tuple[tuple[int, str, ResearchSpec], ...]
    jobs: tuple[str, ...]
    request_id: str | None
    error: dict[str, str] | None
    cancel_note: str | None
    last_model: dict[str, str] | None

    def document(self) -> dict[str, Any]:
        return {
            "schema": _SCHEMA,
            "task_id": self.task_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "raw_idea": self.raw_idea,
            "gap": self.gap,
            "budget": self.budget,
            "usage": self.usage,
            "spec_draft": None if self.spec_draft is None else self.spec_draft.document(),
            "spec_confirmed": [
                {"version": version, "confirmed_at": confirmed_at, "spec": spec.document()}
                for version, confirmed_at, spec in self.spec_confirmed
            ],
            "jobs": list(self.jobs),
            "request_id": self.request_id,
            "error": self.error,
            "cancel_note": self.cancel_note,
            "last_model": self.last_model,
        }


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _idea_key(raw_idea: str) -> str:
    return " ".join(raw_idea.split())


def _bounded_text(value: object, *, field: str, maximum: int, required: bool = True) -> str:
    if not isinstance(value, str) or not value.strip():
        if required:
            raise InvalidSpecError(f"{field} is required")
        return ""
    if len(value.encode("utf-8")) > maximum:
        raise InvalidSpecError(f"{field} exceeds the supported size")
    return value.strip()


def _initial_cash(value: object) -> str:
    text = _bounded_text(value, field="initial_cash", maximum=64)
    if not _DECIMAL_PATTERN.fullmatch(text):
        raise InvalidSpecError("initial_cash must be one canonical decimal string")
    try:
        Decimal(text)
    except InvalidOperation as error:
        raise InvalidSpecError("initial_cash is not a valid decimal") from error
    return text


class ResearchStore:
    """Durable task store under one workspace research directory."""

    def __init__(self, workspace: Path):
        self._dir = workspace / "research"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, task_id: str) -> Path:
        return self._dir / f"{task_id}.json"

    @staticmethod
    def _decode(document: dict[str, Any]) -> ResearchTask:
        if document.get("schema") != _SCHEMA:
            raise ResearchTaskError("research task schema is not supported")
        draft = document.get("spec_draft")
        confirmed: list[tuple[int, str, ResearchSpec]] = []
        for entry in document.get("spec_confirmed") or []:
            confirmed.append(
                (int(entry["version"]), str(entry["confirmed_at"]), _decode_spec(entry["spec"]))
            )
        return ResearchTask(
            task_id=str(document["task_id"]),
            created_at=str(document["created_at"]),
            updated_at=str(document["updated_at"]),
            status=str(document["status"]),
            raw_idea=str(document["raw_idea"]),
            gap=document.get("gap"),
            budget={
                "max_model_calls": int(document["budget"]["max_model_calls"]),
                "max_backtests": int(document["budget"]["max_backtests"]),
            },
            usage={
                "model_calls": int(document["usage"]["model_calls"]),
                "input_tokens": int(document["usage"]["input_tokens"]),
                "output_tokens": int(document["usage"]["output_tokens"]),
                "backtests": int(document["usage"]["backtests"]),
            },
            spec_draft=None if draft is None else _decode_spec(draft),
            spec_confirmed=tuple(confirmed),
            jobs=tuple(str(job_id) for job_id in document.get("jobs") or []),
            request_id=document.get("request_id"),
            error=document.get("error"),
            cancel_note=document.get("cancel_note"),
            last_model=document.get("last_model"),
        )

    def save(self, task: ResearchTask) -> None:
        """Atomically replace one task record."""
        payload = (
            json.dumps(
                task.document(),
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self._path(task.task_id), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
            )
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise ResearchTaskError("task write made no complete progress")
            os.fsync(descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def get(self, task_id: str) -> ResearchTask:
        try:
            document = json.loads(self._path(task_id).read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise TaskNotFoundError("research task was not found") from error
        except (OSError, json.JSONDecodeError) as error:
            raise ResearchTaskError("research task is unreadable") from error
        return self._decode(document)

    def list(self) -> list[ResearchTask]:
        tasks: list[ResearchTask] = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                tasks.append(self.get(path.stem))
            except ResearchTaskError:
                continue
        tasks.sort(key=lambda task: task.created_at, reverse=True)
        return tasks

    def find_active_by_idea(self, raw_idea: str) -> ResearchTask | None:
        key = _idea_key(raw_idea)
        for task in self.list():
            # Failed tasks are retryable in place, so they also dedupe a
            # repeated submission instead of spawning a duplicate task.
            if _idea_key(task.raw_idea) == key and (
                task.status in _ACTIVE_STATES or task.status == "failed"
            ):
                return task
        return None


def _decode_spec(document: dict[str, Any]) -> ResearchSpec:
    instrument = document.get("instrument") or {}
    return ResearchSpec(
        strategy_id=str(document["strategy_id"]),
        instrument_venue=str(instrument.get("venue") or ""),
        instrument_symbol=str(instrument.get("symbol") or ""),
        initial_cash=str(document["initial_cash"]),
        rationale=str(document.get("rationale") or ""),
        scenario_id=document.get("scenario_id"),
        dataset_id=document.get("dataset_id"),
        source_sha256=document.get("source_sha256"),
        parameters=dict(document.get("parameters") or {}),
        assumptions=tuple(str(item) for item in document.get("assumptions") or []),
    )


class ResearchOrchestrator:
    """Bounded orchestrator: one task per idea, whitelisted tools only."""

    def __init__(self, store: ResearchStore, service: WebService, settings: RadianSettings):
        self._store = store
        self._service = service
        self._settings = settings
        self._provider = AnthropicProvider(settings)

    # -- catalog and validation --------------------------------------------

    def _catalog(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for scenario in (cast(dict[str, Any], item) for item in self._service.registry.list()):
            descriptor = scenario.get("strategy_descriptor") or {}
            if descriptor.get("research_visible") is False:
                continue
            summary = scenario.get("summary") or {}
            contracts = [
                {
                    "name": parameter.get("name"),
                    "type": parameter.get("type"),
                    "minimum": parameter.get("minimum"),
                    "maximum": parameter.get("maximum"),
                    "default": parameter.get("default"),
                }
                for parameter in scenario.get("strategy_parameters") or []
            ]
            entries.append(
                {
                    "scenario_id": scenario.get("scenario_id"),
                    "name": scenario.get("name"),
                    "valid": bool(scenario.get("valid")),
                    "strategy_id": summary.get("strategy_id") or descriptor.get("strategy_id"),
                    "display_name": descriptor.get("display_name"),
                    "instrument": (
                        {"venue": summary["venue"], "symbol": summary["symbol"]}
                        if summary.get("venue")
                        else None
                    ),
                    "initial_cash": summary.get("initial_cash"),
                    "parameters": contracts,
                }
            )
        return entries[:_MAX_CATALOG_ENTRIES]

    def _with_defaults(self, spec: ResearchSpec) -> ResearchSpec:
        """Fill unset parameters from the registered contract defaults."""
        matching = [
            entry
            for entry in self._catalog()
            if entry["strategy_id"] == spec.strategy_id
            and entry["instrument"]
            and entry["instrument"]["venue"] == spec.instrument_venue
            and entry["instrument"]["symbol"] == spec.instrument_symbol
        ]
        defaults: dict[str, Any] = {}
        for entry in matching:
            for parameter in entry["parameters"]:
                name = parameter.get("name")
                value = parameter.get("default")
                if name is not None and value is not None:
                    defaults[name] = value
        return replace(spec, parameters={**defaults, **spec.parameters})

    def _validate_spec(self, spec: ResearchSpec) -> None:
        catalog = self._catalog()
        strategies = {entry["strategy_id"] for entry in catalog if entry["strategy_id"]}
        if spec.strategy_id not in strategies:
            raise InvalidSpecError(
                f"strategy {spec.strategy_id} is not a registered research strategy"
            )
        matching = [
            entry
            for entry in catalog
            if entry["strategy_id"] == spec.strategy_id
            and entry["instrument"]
            and entry["instrument"]["venue"] == spec.instrument_venue
            and entry["instrument"]["symbol"] == spec.instrument_symbol
        ]
        if not matching:
            raise InvalidSpecError(
                f"no registered scenario matches {spec.instrument_venue}:{spec.instrument_symbol}"
                f" for {spec.strategy_id}"
            )
        if spec.scenario_id is not None and spec.scenario_id not in {
            entry["scenario_id"] for entry in matching
        }:
            raise InvalidSpecError("scenario_id does not match the spec instrument and strategy")
        contract_names = {
            parameter["name"] for entry in matching for parameter in entry["parameters"]
        }
        integer_names = {
            parameter["name"]
            for entry in matching
            for parameter in entry["parameters"]
            if parameter["type"] == "integer"
        }
        if contract_names:
            unknown = set(spec.parameters) - contract_names
            if unknown:
                raise InvalidSpecError(f"unknown parameters: {', '.join(sorted(unknown))}")
            for name in integer_names:
                value = spec.parameters.get(name)
                if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                    raise InvalidSpecError(f"parameter {name} must be an integer")
        if len(spec.assumptions) > _MAX_ASSUMPTIONS:
            raise InvalidSpecError("too many assumptions")
        for assumption in spec.assumptions:
            _bounded_text(assumption, field="assumption", maximum=_MAX_ASSUMPTION_BYTES)

    # -- tool definitions ---------------------------------------------------

    @staticmethod
    def _tools() -> list[dict[str, Any]]:
        return [
            {
                "name": "propose_spec",
                "description": (
                    "Propose one executable research spec using a registered strategy, instrument "
                    "and optional parameter overrides. Unknown strategies or instruments will be "
                    "rejected by the system."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "strategy_id": {"type": "string"},
                        "instrument": {
                            "type": "object",
                            "properties": {
                                "venue": {"type": "string"},
                                "symbol": {"type": "string"},
                            },
                            "required": ["venue", "symbol"],
                        },
                        "scenario_id": {"type": "string"},
                        "dataset_id": {"type": "string"},
                        "initial_cash": {"type": "string"},
                        "parameters": {"type": "object"},
                        "assumptions": {"type": "array", "items": {"type": "string"}},
                        "rationale": {"type": "string"},
                    },
                    "required": ["strategy_id", "instrument", "initial_cash", "rationale"],
                },
            },
            {
                "name": "declare_unsupported",
                "description": (
                    "Declare that the idea cannot be executed with the registered strategies and "
                    "data, and state exactly what support is missing."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"gap": {"type": "string"}},
                    "required": ["gap"],
                },
            },
        ]

    def _system_prompt(self) -> str:
        catalog = self._catalog()
        listing = json.dumps(catalog, ensure_ascii=True, separators=(",", ":"))
        return (
            "You propose quantitative research for a single-user local backtest product. "
            "You can ONLY use the provided tools. propose_spec must reference an existing "
            "registered strategy and instrument from the catalog below; declare_unsupported "
            "must be used when the idea needs a strategy, market, or data that is not "
            "registered. Never invent strategies, symbols, or data. Never request money, "
            "orders, broker access, or live trading. Parameters are optional overrides of "
            "the registered contracts; use the registered defaults when the idea does not "
            "specify them. State assumptions explicitly, and keep the rationale honest "
            "about what this run can and cannot show.\n\nRegistered catalog (JSON):\n"
            f"{listing}"
        )

    # -- orchestration steps -------------------------------------------------

    def _budget(self) -> dict[str, int]:
        try:
            status = self._settings.status()
        except (RadianSettingsError, ValueError):
            status = {}
        return status.get("research_budget") or {"max_model_calls": 10, "max_backtests": 5}

    def _charged_usage(
        self, task: ResearchTask, response: Any, base: dict[str, int] | None = None
    ) -> dict[str, int]:
        usage = dict(base if base is not None else task.usage)
        if usage["model_calls"] >= task.budget["max_model_calls"]:
            raise BudgetExceededError("research model-call budget is exhausted")
        usage["model_calls"] += 1
        usage["input_tokens"] += response.input_tokens
        usage["output_tokens"] += response.output_tokens
        return usage

    def _save(self, task: ResearchTask, **changes: Any) -> ResearchTask:
        values = {field.name: getattr(task, field.name) for field in fields(task)}
        values["updated_at"] = _now()
        values.update(changes)
        updated = ResearchTask(**values)
        self._store.save(updated)
        return updated

    def _propose_failed(
        self, task: ResearchTask, code: str, message: str, last_model: dict[str, str] | None = None
    ) -> ResearchTask:
        return self._save(
            task,
            status="failed",
            error={"step": "propose", "code": code, "message": message},
            last_model=last_model,
        )

    def create(self, raw_idea: str) -> ResearchTask:
        idea = _bounded_text(raw_idea, field="idea", maximum=_MAX_IDEA_BYTES)
        existing = self._store.find_active_by_idea(idea)
        if existing is not None:
            raise TaskConflictError(
                f"an active research task already exists for this idea: {existing.task_id}"
            )
        now = _now()
        task = ResearchTask(
            task_id=str(uuid4()),
            created_at=now,
            updated_at=now,
            status="drafting",
            raw_idea=idea,
            gap=None,
            budget=self._budget(),
            usage={"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "backtests": 0},
            spec_draft=None,
            spec_confirmed=(),
            jobs=(),
            request_id=None,
            error=None,
            cancel_note=None,
            last_model=None,
        )
        self._store.save(task)
        return self._propose(task.task_id)

    def _propose(self, task_id: str) -> ResearchTask:
        task = self._store.get(task_id)
        try:
            response = self._provider.complete(
                system=self._system_prompt(),
                messages=[{"role": "user", "content": task.raw_idea}],
                tools=self._tools(),
            )
        except ModelNotConfigured as error:
            return self._propose_failed(task, "model_not_configured", str(error))
        except ModelProviderError as error:
            return self._propose_failed(task, "provider_error", str(error))
        try:
            usage = self._charged_usage(task, response)
        except BudgetExceededError as error:
            return self._propose_failed(task, "budget_exceeded", str(error))
        messages: list[dict[str, Any]] = [{"role": "user", "content": task.raw_idea}]
        tool_calls = response.tool_calls
        last_model: dict[str, str] | None = {"model": response.model, "at": _now()}
        for _round in range(_MAX_TOOL_ROUNDS):
            if not tool_calls:
                return self._save(
                    task,
                    status="failed",
                    error={
                        "step": "propose",
                        "code": "no_tool_call",
                        "message": (
                            "the model did not propose a spec or declare the idea unsupported"
                        ),
                    },
                    usage=usage,
                    last_model=last_model,
                )
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": call["id"],
                            "name": call["name"],
                            "input": call["input"],
                        }
                        for call in tool_calls
                    ],
                }
            )
            result_blocks: list[dict[str, Any]] = []
            for call in tool_calls:
                name = call.get("name")
                if name == "declare_unsupported":
                    gap = _bounded_text(
                        call.get("input", {}).get("gap"), field="gap", maximum=_MAX_GAP_BYTES
                    )
                    return self._save(
                        task,
                        status="unsupported",
                        gap=gap,
                        error=None,
                        usage=usage,
                        last_model=last_model,
                    )
                if name == "propose_spec":
                    try:
                        spec = self._spec_from_input(call.get("input") or {})
                        self._validate_spec(spec)
                        spec = self._with_defaults(spec)
                    except InvalidSpecError as error:
                        result_blocks.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": call["id"],
                                "content": f"rejected: {error}",
                            }
                        )
                        continue
                    return self._save(
                        task,
                        status="awaiting_confirmation",
                        spec_draft=spec,
                        error=None,
                        usage=usage,
                        last_model=last_model,
                    )
                result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call["id"],
                        "content": f"tool {name!r} is not available to research",
                    }
                )
            messages.append({"role": "user", "content": result_blocks})
            try:
                response = self._provider.complete(
                    system=self._system_prompt(), messages=messages, tools=self._tools()
                )
            except (ModelNotConfigured, ModelProviderError) as error:
                return self._save(
                    task,
                    status="failed",
                    error={"step": "propose", "code": "provider_error", "message": str(error)},
                    usage=usage,
                    last_model=last_model,
                )
            try:
                usage = self._charged_usage(task, response, base=usage)
            except BudgetExceededError as error:
                return self._propose_failed(task, "budget_exceeded", str(error))
            tool_calls = response.tool_calls
            last_model = {"model": response.model, "at": _now()}
        return self._save(
            task,
            status="failed",
            error={
                "step": "propose",
                "code": "too_many_rounds",
                "message": "the model did not reach a valid proposal within the allowed rounds",
            },
            usage=usage,
            last_model=last_model,
        )

    @staticmethod
    def _spec_from_input(input_: dict[str, Any]) -> ResearchSpec:
        instrument = input_.get("instrument") or {}
        if not isinstance(instrument, dict):
            raise InvalidSpecError("instrument must be one object")
        parameters = input_.get("parameters") or {}
        if not isinstance(parameters, dict):
            raise InvalidSpecError("parameters must be one object")
        assumptions = input_.get("assumptions") or []
        if not isinstance(assumptions, list):
            raise InvalidSpecError("assumptions must be a list")
        return ResearchSpec(
            strategy_id=_bounded_text(input_.get("strategy_id"), field="strategy_id", maximum=128),
            instrument_venue=_bounded_text(
                instrument.get("venue"), field="instrument venue", maximum=64
            ),
            instrument_symbol=_bounded_text(
                instrument.get("symbol"), field="instrument symbol", maximum=64
            ),
            initial_cash=_initial_cash(input_.get("initial_cash")),
            rationale=_bounded_text(
                input_.get("rationale"), field="rationale", maximum=_MAX_RATIONALE_BYTES
            ),
            scenario_id=(
                None
                if input_.get("scenario_id") is None
                else _bounded_text(input_.get("scenario_id"), field="scenario_id", maximum=128)
            ),
            dataset_id=(
                None
                if input_.get("dataset_id") is None
                else _bounded_text(input_.get("dataset_id"), field="dataset_id", maximum=128)
            ),
            parameters=parameters,
            assumptions=tuple(str(item) for item in assumptions),
        )

    def edit_spec(self, task_id: str, fields: dict[str, Any]) -> ResearchTask:
        task = self._store.get(task_id)
        if task.status not in {"awaiting_confirmation", "drafting"} or task.spec_draft is None:
            raise TaskStateError("only an unconfirmed draft spec can be edited")
        draft = task.spec_draft
        merged = ResearchSpec(
            strategy_id=_bounded_text(
                fields.get("strategy_id", draft.strategy_id), field="strategy_id", maximum=128
            ),
            instrument_venue=_bounded_text(
                fields.get("venue", draft.instrument_venue), field="instrument venue", maximum=64
            ),
            instrument_symbol=_bounded_text(
                fields.get("symbol", draft.instrument_symbol), field="instrument symbol", maximum=64
            ),
            initial_cash=_initial_cash(fields.get("initial_cash", draft.initial_cash)),
            rationale=_bounded_text(
                fields.get("rationale", draft.rationale),
                field="rationale",
                maximum=_MAX_RATIONALE_BYTES,
            ),
            scenario_id=(
                None
                if fields.get("scenario_id", draft.scenario_id) is None
                else _bounded_text(fields.get("scenario_id"), field="scenario_id", maximum=128)
            ),
            dataset_id=(
                None
                if fields.get("dataset_id", draft.dataset_id) is None
                else _bounded_text(fields.get("dataset_id"), field="dataset_id", maximum=128)
            ),
            source_sha256=draft.source_sha256,
            parameters=dict(fields.get("parameters", draft.parameters)),
            assumptions=tuple(
                str(item) for item in fields.get("assumptions", list(draft.assumptions))
            ),
        )
        self._validate_spec(merged)
        merged = self._with_defaults(merged)
        return self._save(task, spec_draft=merged, error=None)

    def confirm(self, task_id: str, *, auto_run: bool = True) -> ResearchTask:
        task = self._store.get(task_id)
        if task.status == "cancelled":
            raise TaskStateError("a cancelled research task cannot be confirmed")
        if task.spec_draft is None:
            raise TaskStateError("no unconfirmed draft spec is available")
        spec = task.spec_draft
        self._validate_spec(spec)
        spec = self._with_defaults(spec)
        version = len(task.spec_confirmed) + 1
        confirmed = (*task.spec_confirmed, (version, _now(), spec))
        task = self._save(task, spec_confirmed=confirmed, spec_draft=None, error=None)
        if not auto_run:
            return self._save(task, status="confirmed")
        return self._start_backtest(task.task_id)

    def _start_backtest(self, task_id: str) -> ResearchTask:
        task = self._store.get(task_id)
        if task.status == "cancelled":
            return task
        if task.usage["backtests"] >= task.budget["max_backtests"]:
            return self._save(
                task,
                status="failed",
                error={
                    "step": "start_backtest",
                    "code": "budget_exceeded",
                    "message": "research backtest budget is exhausted",
                },
            )
        confirmed = task.spec_confirmed[-1][2] if task.spec_confirmed else None
        if confirmed is None:
            return self._save(
                task,
                status="failed",
                error={
                    "step": "start_backtest",
                    "code": "no_confirmed_spec",
                    "message": "no confirmed spec exists",
                },
            )
        scenario_id = confirmed.scenario_id or self._resolve_scenario_id(confirmed)
        try:
            validated = self._service.validate_scenario(
                scenario_id,
                dataset_id=confirmed.dataset_id,
                source_sha256=confirmed.source_sha256,
                parameters={
                    "initial_cash": confirmed.initial_cash,
                    "strategy_parameters": confirmed.parameters,
                    "quantity": None,
                    "strategy_source": None,
                },
            )
        except Exception as error:  # noqa: BLE001 — the Web boundary raises several types
            return self._save(
                task,
                status="failed",
                error={
                    "step": "start_backtest",
                    "code": "scenario_invalid",
                    "message": str(error),
                },
            )
        request_id = task.request_id or str(uuid4())
        try:
            # create_job re-parameterizes internally; the identity it checks is
            # the source scenario identity, so the normalized identity from the
            # validation step would always be rejected as "changed".
            record, _created = self._service.create_job(
                scenario_id=scenario_id,
                input_identity=cast(dict[str, object], validated["input_identity"]),
                parameters={
                    "initial_cash": confirmed.initial_cash,
                    "strategy_parameters": confirmed.parameters,
                    "quantity": None,
                    "strategy_source": None,
                },
                request_id=request_id,
            )
        except Exception as error:  # noqa: BLE001
            return self._save(
                task,
                status="failed",
                error={"step": "start_backtest", "code": "job_rejected", "message": str(error)},
            )
        usage = dict(task.usage)
        usage["backtests"] += 1
        job_id = record.job_id
        task = self._save(
            task,
            status="running",
            jobs=tuple(sorted({*task.jobs, job_id})),
            request_id=request_id,
            usage=usage,
            error=None,
        )
        return self.refresh(task.task_id)

    def _resolve_scenario_id(self, spec: ResearchSpec) -> str:
        for entry in self._catalog():
            if (
                entry["strategy_id"] == spec.strategy_id
                and entry["instrument"]
                and entry["instrument"]["venue"] == spec.instrument_venue
                and entry["instrument"]["symbol"] == spec.instrument_symbol
                and entry["valid"]
            ):
                return str(entry["scenario_id"])
        raise InvalidSpecError("no valid registered scenario matches the confirmed spec")

    def retry(self, task_id: str) -> ResearchTask:
        task = self._store.get(task_id)
        if task.status == "cancelled":
            raise TaskStateError("a cancelled research task cannot be retried")
        if task.status != "failed" or task.error is None:
            raise TaskStateError("only a failed task can be retried")
        step = task.error["step"]
        if step == "propose":
            task = self._save(task, status="drafting")
            return self._propose(task_id)
        task = self._save(task, status="awaiting_confirmation")
        return self._start_backtest(task_id)

    def cancel(self, task_id: str) -> ResearchTask:
        task = self._store.get(task_id)
        if task.status in _TERMINAL_STATES and task.status != "failed":
            return task
        return self._save(
            task,
            status="cancelled",
            cancel_note=(
                "Already started backtests are not killable and keep their own terminal states."
                if task.jobs
                else None
            ),
            error=None,
        )

    def refresh(self, task_id: str) -> ResearchTask:
        """Re-derive task state from the real jobs it started; no new work is launched."""
        task = self._store.get(task_id)
        if task.status not in {"running", "failed"} or not task.jobs:
            return task
        try:
            job = self._service.get_job(task.jobs[-1])
        except Exception:  # noqa: BLE001
            return task
        status = job.status
        if status in {"accepted", "running"}:
            return self._save(task, status="running")
        if status == "succeeded":
            return self._save(task, status="succeeded", error=None)
        return self._save(
            task,
            status="failed",
            error={
                "step": "backtest",
                "code": str(job.error_code or "run_failed"),
                "message": str(job.message or "the started run did not succeed"),
            },
        )
