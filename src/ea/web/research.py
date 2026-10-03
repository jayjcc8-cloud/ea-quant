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
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from ea.web.llm import (
    ModelMessage,
    ModelNotConfigured,
    ModelPort,
    ModelProviderError,
    ToolDefinition,
    create_model_port,
)
from ea.web.service import JobNotFoundError, WebService
from ea.web.settings import RadianSettings, RadianSettingsError

_SCHEMA = "radian.research-task.v1"
_ACTIVE_STATES = frozenset({"drafting", "awaiting_confirmation", "confirmed", "running"})
_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "unsupported"})
# A task with a durable in-flight reservation is reconciled, not left pending.
_RECONCILABLE_STATES = frozenset(
    {"drafting", "awaiting_confirmation", "confirmed", "running", "failed", "unsupported"}
)
# A failure the user may retry: it provably spent nothing or reached a terminal
# provider answer. ``interrupted_before_dispatch`` is retryable because the
# charge was returned; ``model_outcome_unknown`` and ``job_outcome_unknown`` are
# not, because an attempt left the process and its cost is not provable.
_INTERRUPTED = "interrupted_before_dispatch"
_UNKNOWN_MODEL = "model_outcome_unknown"
_UNKNOWN_OUTCOME = "job_outcome_unknown"
_NON_RETRYABLE_CODES = frozenset({_UNKNOWN_MODEL, _UNKNOWN_OUTCOME, "task_in_flight"})
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
class TaskReservation:
    """One durable reservation for work the task is about to dispatch.

    ``kind`` is ``model_call`` or ``backtest``. ``status`` distinguishes a
    reservation from a real dispatch: ``pending`` means the ledger is charged
    but no provider or job call was started, ``dispatched`` means the call was
    about to leave this process, and ``unknown`` means it left and the outcome
    was never observed. Only ``pending`` may be released.
    """

    kind: str
    seq: int
    status: str
    at: str

    def document(self) -> dict[str, Any]:
        return {"kind": self.kind, "seq": self.seq, "status": self.status, "at": self.at}

    @staticmethod
    def decode(document: Any) -> TaskReservation | None:
        if not isinstance(document, dict):
            return None
        try:
            return TaskReservation(
                kind=str(document["kind"]),
                seq=int(document["seq"]),
                status=str(document["status"]),
                at=str(document.get("at") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None


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
    last_model: dict[str, Any] | None
    reservation: TaskReservation | None = None
    backtest_reservation: TaskReservation | None = None
    spent_calls: tuple[int, ...] = ()
    operation_generation: int = 0
    operation_completion_generation: int = 0
    operation_kind: str | None = None
    runtime_diagnostic: str | None = None

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
            "reservation": None if self.reservation is None else self.reservation.document(),
            "backtest_reservation": (
                None if self.backtest_reservation is None else self.backtest_reservation.document()
            ),
            "spent_calls": list(self.spent_calls),
            "operation_generation": self.operation_generation,
            "operation_completion_generation": self.operation_completion_generation,
            "operation_kind": self.operation_kind,
            "runtime_diagnostic": self.runtime_diagnostic,
        }


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _idea_key(raw_idea: str) -> str:
    return " ".join(raw_idea.split())


def _unproven_submission_message(detail: str) -> str:
    """Disclose an unprovable submission boundary and the reachable exit.

    A dispatched submission whose outcome cannot be proven keeps its charge and
    is never re-submitted on its own. The only explicit new-attempt path this
    product offers is a deliberate cancel plus a new task, so the message names
    that path instead of promising a reconciliation that cannot be proven.
    """
    return (
        f"{detail} The backtest slot stays charged and this task stays paused; nothing is "
        "re-submitted automatically. Cancel this task and create a new one if you want "
        "another attempt"
    )


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
        # Only an interrupted publish can leave one of these behind, and the
        # published name always ends in .json, so removal cannot hide a record.
        self._discard_temp_files()

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
        generation = int(document.get("operation_generation", 0))
        completed = int(document.get("operation_completion_generation", 0))
        if generation < 0 or completed < 0 or completed > generation:
            raise ResearchTaskError("invalid operation generation markers")
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
                "unavailable_model_calls": int(document["usage"].get("unavailable_model_calls", 0)),
                "backtests": int(document["usage"]["backtests"]),
            },
            spec_draft=None if draft is None else _decode_spec(draft),
            spec_confirmed=tuple(confirmed),
            jobs=tuple(str(job_id) for job_id in document.get("jobs") or []),
            request_id=document.get("request_id"),
            error=document.get("error"),
            cancel_note=document.get("cancel_note"),
            last_model=document.get("last_model"),
            reservation=TaskReservation.decode(document.get("reservation")),
            backtest_reservation=TaskReservation.decode(document.get("backtest_reservation")),
            spent_calls=tuple(int(seq) for seq in document.get("spent_calls") or []),
            operation_generation=generation,
            operation_completion_generation=completed,
            operation_kind=document.get("operation_kind"),
        )

    def _publish(self, task: ResearchTask, payload: bytes) -> None:
        """Replace one record atomically: temp file, fsync, rename, dir fsync."""
        descriptor, raw_path = tempfile.mkstemp(prefix=".task-", dir=self._dir)
        pending = Path(raw_path)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(pending, self._path(task.task_id))
            directory_descriptor = os.open(self._dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            pending.unlink(missing_ok=True)

    def _discard_temp_files(self) -> None:
        """Remove only this store's own unpublished temp files.

        Interrupted publishes are their only source: a committed record is a
        ``.json`` file named by its task id, so a leading dot can never be one.
        """
        for path in self._dir.glob(".task-*"):
            try:
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            except OSError:
                continue

    def save(self, task: ResearchTask) -> None:
        """Atomically replace a record; a post-replace fsync failure may be visible."""
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
        self._publish(task, payload)

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
    """Single owner per task; external work runs outside its task lock.

    One proposal owner spans all model rounds. Every result enters the same
    locked reload/token gate. Cancellation is immediate, irreversible, and
    preserves the live owner until its evidence is durably finalized.
    """

    def __init__(self, store: ResearchStore, service: WebService, settings: RadianSettings):
        self._store = store
        self._service = service
        self._settings = settings
        self._provider: ModelPort = create_model_port(settings)
        self._lock = threading.Lock()
        # Reentrant per task: one public request may re-enter its own lock while
        # composing helpers, but never another task's.
        self._locks: dict[str, threading.RLock] = {}
        self._active: dict[str, int] = {}
        self._persistence_errors: dict[str, str] = {}
        self._creation_lock = threading.Lock()

    @contextmanager
    def _task_lock(self, task_id: str) -> Iterator[None]:
        with self._lock:
            lock = self._locks.setdefault(task_id, threading.RLock())
        with lock:
            yield

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
        if spec.strategy_id == "price-rule-entry-v1":
            from ea.strategy.registry import BUILTIN_STRATEGIES

            try:
                BUILTIN_STRATEGIES.get(spec.strategy_id, 1).normalize(
                    self._with_defaults(spec).parameters
                )
            except ValueError as error:
                raise InvalidSpecError(str(error)) from None
        if len(spec.assumptions) > _MAX_ASSUMPTIONS:
            raise InvalidSpecError("too many assumptions")
        for assumption in spec.assumptions:
            _bounded_text(assumption, field="assumption", maximum=_MAX_ASSUMPTION_BYTES)

    # -- tool definitions ---------------------------------------------------

    @staticmethod
    def _tools() -> list[ToolDefinition]:
        definitions: list[dict[str, Any]] = [
            {
                "name": "propose_spec",
                "description": (
                    "Propose one executable research spec using a registered strategy, instrument "
                    "and optional parameter overrides. Unknown strategies or instruments will be "
                    "rejected by the system. price-rule-entry-v1 parameters.rule is a JSON string: "
                    "{version:1,when:condition,action:ENTER_LONG}; condition compares left/right "
                    "with op gt/ge/lt/le/eq, or all/any args, or not arg. Operands are "
                    "{kind:price,field:open/high/low/close} or {kind:sma,window:1..64} "
                    "(trailing close). Max 32 nodes/depth 6; no offsets/code. Warmup HOLD; "
                    "one entry then HOLD. Unsupported exits/indicators must be declared."
                ),
                "parameters": {
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
                "parameters": {
                    "type": "object",
                    "properties": {"gap": {"type": "string"}},
                    "required": ["gap"],
                },
            },
        ]

        return [ToolDefinition(**definition) for definition in definitions]

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

    def _is_active(self, task_id: str) -> bool:
        with self._task_lock(task_id):
            return task_id in self._active

    def _owns(self, task_id: str, generation: int) -> bool:
        with self._task_lock(task_id):
            task = self._store.get(task_id)
            return (
                self._active.get(task_id) == generation
                and task.operation_generation == generation
                and task.operation_completion_generation != generation
            )

    def _view(self, task_id: str) -> ResearchTask:
        with self._task_lock(task_id):
            task = self._store.get(task_id)
            diagnostic = self._persistence_errors.get(task_id)
            if diagnostic:
                return replace(task, runtime_diagnostic=f"task_persistence_failed: {diagnostic}")
            return task

    def _transition(
        self,
        task_id: str,
        generation: int | None,
        event: str,
        *,
        finish: bool = False,
        **data: Any,
    ) -> ResearchTask:
        """The sole task writer and owner-release gate.

        No caller snapshot is accepted. Events are reduced against a locked
        reload. A stale token neither writes nor removes somebody else's owner.
        A failed save retains ownership, including failures after replace.
        """
        with self._task_lock(task_id):
            if event == "create":
                current = cast(ResearchTask, data["task"])
                # Creation is the only write without a prior durable record.
                try:
                    self._store.get(task_id)
                except TaskNotFoundError:
                    pass
                else:
                    raise TaskConflictError("task identity already exists")
                next_task = current
            else:
                current = self._store.get(task_id)
                if event == "begin":
                    if task_id in self._active:
                        return current
                    if current.status == "cancelled" and data["kind"] != "reconcile":
                        return current
                    generation = current.operation_generation + 1
                    next_task = replace(
                        current, operation_generation=generation, operation_kind=data["kind"]
                    )
                elif event == "cancel":
                    next_task = self._reduce(current, event, data)
                else:
                    if (
                        generation is None
                        or self._active.get(task_id) != generation
                        or current.operation_generation != generation
                    ):
                        return current
                    if current.operation_completion_generation == generation:
                        # An earlier save may have committed then raised during
                        # directory fsync. Re-persist the unchanged record as a
                        # durability barrier, never apply the outcome twice.
                        next_task = current
                    else:
                        next_task = self._reduce(current, event, data)
                        if finish:
                            next_task = replace(
                                next_task, operation_completion_generation=generation
                            )
            next_task = replace(next_task, updated_at=_now())
            try:
                self._store.save(next_task)
            except Exception as error:  # noqa: BLE001 — any unconfirmed save keeps ownership
                if event == "begin":
                    # Even a begin whose replacement became visible must not
                    # start external work after an unconfirmed durability barrier.
                    assert generation is not None
                    self._active[task_id] = generation
                self._persistence_errors[task_id] = str(error)
                raise
            if event == "begin" or finish:
                self._persistence_errors.pop(task_id, None)
            if event == "begin":
                assert generation is not None
                self._active[task_id] = generation
            elif finish and generation is not None and self._active.get(task_id) == generation:
                del self._active[task_id]
            return next_task

    def _begin(self, task_id: str, kind: str) -> int | None:
        with self._task_lock(task_id):
            if task_id in self._active:
                return None
            task = self._transition(task_id, None, "begin", kind=kind)
            if self._active.get(task_id) != task.operation_generation:
                return None
            return task.operation_generation

    @staticmethod
    def _reduce(task: ResearchTask, event: str, data: dict[str, Any]) -> ResearchTask:
        """Finite local task events; no I/O, callbacks, snapshots or owner mutation."""
        changes: dict[str, Any] = {}
        if event == "noop":
            return task
        if event == "cancel":
            if task.status in {"succeeded", "unsupported", "cancelled"}:
                return task
            note = None
            if task.jobs:
                note = (
                    "Already started backtests keep their own terminal states; "
                    "cancelling is not a rollback."
                )
            elif task.backtest_reservation is not None:
                note = (
                    "A backtest submission was not confirmed; its slot stays charged "
                    "and cancelling is not a rollback."
                )
            return replace(task, status="cancelled", error=None, cancel_note=note)
        if (
            event in {"model_reserve", "backtest_reserve", "model_dispatch", "backtest_dispatch"}
            and task.status == "cancelled"
        ):
            return task
        if event == "model_reserve":
            if task.reservation is not None:
                raise TaskStateError("a model call is already reserved")
            if task.usage["model_calls"] >= task.budget["max_model_calls"]:
                raise BudgetExceededError("research model-call budget is exhausted")
            seq = max(task.spent_calls, default=0) + 1
            changes = {
                "reservation": TaskReservation("model_call", seq, "pending", _now()),
                "usage": {**task.usage, "model_calls": task.usage["model_calls"] + 1},
            }
        elif event in {"model_dispatch", "backtest_dispatch"}:
            key = "reservation" if event == "model_dispatch" else "backtest_reservation"
            reservation = getattr(task, key)
            if reservation is None or reservation.status != "pending":
                raise TaskStateError("no pending reservation belongs to this dispatch")
            changes[key] = replace(reservation, status="dispatched")
        elif event in {
            "model_observed",
            "model_unknown",
            "model_unconfigured",
            "model_pending_release",
        }:
            reservation = task.reservation
            seq = data["seq"]
            if reservation is None or reservation.seq != seq:
                return task
            changes["reservation"] = None
            if event in {"model_unconfigured", "model_pending_release"}:
                if event == "model_pending_release" and reservation.status != "pending":
                    raise TaskStateError("a dispatched model call cannot be refunded")
                changes["usage"] = {
                    **task.usage,
                    "model_calls": max(0, task.usage["model_calls"] - 1),
                }
            elif seq not in task.spent_calls:
                changes["spent_calls"] = (*task.spent_calls, seq)
                changes["last_model"] = data["model"]
                if event == "model_observed":
                    response = data["response"]
                    changes["usage"] = {
                        **task.usage,
                        "input_tokens": task.usage["input_tokens"]
                        + int(response.input_tokens or 0),
                        "output_tokens": task.usage["output_tokens"]
                        + int(response.output_tokens or 0),
                        "unavailable_model_calls": task.usage.get("unavailable_model_calls", 0)
                        + int(not response.usage_available),
                    }
            if data.get("error"):
                changes.update(status="failed", error=data["error"])
        elif event == "backtest_reserve":
            if task.backtest_reservation is not None or task.jobs:
                raise TaskStateError("this task already owns a backtest")
            if task.usage["backtests"] >= task.budget["max_backtests"]:
                raise BudgetExceededError("research backtest budget is exhausted")
            latest = task.spec_confirmed[-1][2] if task.spec_confirmed else None
            if latest != data["spec"]:
                raise TaskStateError("confirmed spec changed during validation")
            changes = {
                "status": "running",
                "request_id": task.request_id or str(uuid4()),
                "backtest_reservation": TaskReservation(
                    "backtest", task.usage["backtests"] + 1, "pending", _now()
                ),
                "usage": {**task.usage, "backtests": task.usage["backtests"] + 1},
            }
        elif event == "backtest_pending_release":
            reservation = task.backtest_reservation
            if reservation is None:
                return task
            if reservation.status != "pending":
                raise TaskStateError("a dispatched backtest cannot be refunded")
            changes = {
                "backtest_reservation": None,
                "usage": {**task.usage, "backtests": max(0, task.usage["backtests"] - 1)},
                "status": "failed",
                "error": data["error"],
            }
        elif event == "job_found":
            record = data["record"]
            if task.request_id != record.request_id:
                raise TaskStateError("job belongs to a different request identity")
            changes = {
                "jobs": tuple(sorted({*task.jobs, record.job_id})),
                "backtest_reservation": None,
                "status": "running",
                "error": None,
            }
        elif event == "job_state":
            job = data["record"]
            if job.job_id not in task.jobs:
                raise TaskStateError("job is not bound to this task")
            if job.status in {"accepted", "running"}:
                changes = {"status": "running", "error": None}
            elif job.status == "succeeded":
                changes = {"status": "succeeded", "error": None}
            else:
                changes = {
                    "status": "failed",
                    "error": {
                        "step": "backtest",
                        "code": "interrupted"
                        if job.status == "interrupted"
                        else str(job.error_code or "run_failed"),
                        "message": str(job.message or "the started run did not succeed"),
                    },
                }
        elif event == "failure":
            changes = {"status": "failed", "error": data["error"]}
        elif event == "proposal":
            changes = {
                "status": data["status"],
                "error": None,
                "gap": data.get("gap"),
                "spec_draft": data.get("spec"),
            }
        elif event == "edit":
            if task.status != "awaiting_confirmation" or task.spec_draft is None:
                raise TaskStateError("only an unconfirmed draft spec can be edited")
            changes = {"spec_draft": data["spec"], "error": None}
        elif event == "confirm":
            if task.spec_draft is None or task.spec_draft != data["spec"]:
                raise TaskStateError("no unchanged draft spec is available")
            changes = {
                "spec_confirmed": (
                    *task.spec_confirmed,
                    (len(task.spec_confirmed) + 1, _now(), data["spec"]),
                ),
                "spec_draft": None,
                "status": "confirmed",
                "error": None,
            }
        else:
            raise TaskStateError(f"unknown task transition: {event}")
        if task.status == "cancelled":
            # A late result may add evidence and settle its own reservation.
            # It never changes cancellation, installs a spec or starts work.
            changes = {
                key: value
                for key, value in changes.items()
                if key
                in {
                    "usage",
                    "spent_calls",
                    "last_model",
                    "jobs",
                    "reservation",
                    "backtest_reservation",
                }
            }
        return replace(task, **changes)

    @staticmethod
    def _error(code: str, message: str, step: str = "propose") -> dict[str, str]:
        return {"step": step, "code": code, "message": message}

    def _failure(
        self, task_id: str, generation: int, code: str, message: str, step: str = "propose"
    ) -> ResearchTask:
        return self._transition(
            task_id, generation, "failure", finish=True, error=self._error(code, message, step)
        )

    def create(self, raw_idea: str) -> ResearchTask:
        idea = _bounded_text(raw_idea, field="idea", maximum=_MAX_IDEA_BYTES)
        now = _now()
        task = ResearchTask(
            task_id=str(uuid4()),
            created_at=now,
            updated_at=now,
            status="drafting",
            raw_idea=idea,
            gap=None,
            budget=self._budget(),
            usage={"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "backtests": 0,
                   "unavailable_model_calls": 0},
            spec_draft=None,
            spec_confirmed=(),
            jobs=(),
            request_id=str(uuid4()),
            error=None,
            cancel_note=None,
            last_model=None,
        )
        with self._creation_lock:
            if self._store.find_active_by_idea(idea) is not None:
                raise TaskConflictError("an active research task already exists for this idea")
            self._transition(task.task_id, None, "create", task=task)
        return self._propose(task.task_id)

    def _propose(self, task_id: str, generation: int | None = None) -> ResearchTask:
        if generation is None:
            generation = self._begin(task_id, "propose")
        if generation is None or not self._owns(task_id, generation):
            return self._view(task_id)
        task = self._store.get(task_id)
        messages = [ModelMessage("user", task.raw_idea)]
        for _round in range(_MAX_TOOL_ROUNDS + 1):
            try:
                task = self._transition(task_id, generation, "model_reserve")
            except BudgetExceededError as error:
                return self._failure(task_id, generation, "budget_exceeded", str(error))
            if task.status == "cancelled":
                return self._transition(task_id, generation, "noop", finish=True)
            assert task.reservation is not None
            seq = task.reservation.seq
            task = self._transition(task_id, generation, "model_dispatch")
            if task.status == "cancelled":
                return self._transition(
                    task_id, generation, "model_pending_release", finish=True, seq=seq
                )
            try:
                response = self._provider.complete(
                    system=self._system_prompt(), messages=messages, tools=self._tools()
                )
            except ModelNotConfigured as error:
                return self._transition(
                    task_id,
                    generation,
                    "model_unconfigured",
                    finish=True,
                    seq=seq,
                    error=self._error("model_not_configured", str(error)),
                )
            except ModelProviderError as error:
                return self._transition(
                    task_id,
                    generation,
                    "model_unknown",
                    finish=True,
                    seq=seq,
                    model={"model": "unknown", "at": _now(), "outcome": "unknown_after_dispatch"},
                    error=self._error(
                        "provider_error",
                        f"{error}; outcome unknown; the call stays charged "
                        "and is not re-dispatched automatically",
                    ),
                )
            task = self._transition(
                task_id,
                generation,
                "model_observed",
                seq=seq,
                response=response,
                model={
                    "model": str(response.model or "unknown"),
                    "at": _now(),
                    "outcome": "observed",
                    "usage_available": response.usage_available,
                    "stop_reason": response.stop_reason,
                },
            )
            if task.status == "cancelled":
                return self._transition(task_id, generation, "noop", finish=True)
            if not response.tool_calls:
                return self._failure(
                    task_id,
                    generation,
                    "no_tool_call",
                    "the model did not propose a spec or declare the idea unsupported",
                )
            messages.append(ModelMessage("assistant", response.text, response.tool_calls))
            results: list[ModelMessage] = []
            for call in response.tool_calls:
                name = call.name
                if name == "declare_unsupported":
                    gap = _bounded_text(
                        call.arguments.get("gap"), field="gap", maximum=_MAX_GAP_BYTES
                    )
                    return self._transition(
                        task_id, generation, "proposal", finish=True, status="unsupported", gap=gap
                    )
                if name == "propose_spec":
                    try:
                        spec = self._spec_from_input(call.arguments)
                        self._validate_spec(spec)
                        spec = self._with_defaults(spec)
                    except InvalidSpecError as error:
                        results.append(ModelMessage("tool", f"rejected: {error}", call_id=call.id))
                        continue
                    return self._transition(
                        task_id,
                        generation,
                        "proposal",
                        finish=True,
                        status="awaiting_confirmation",
                        spec=spec,
                    )
                results.append(ModelMessage(
                    "tool", f"tool {name!r} is not available to research", call_id=call.id
                ))
            messages.extend(results)
        return self._failure(
            task_id,
            generation,
            "too_many_rounds",
            "the model did not reach a valid proposal within the allowed rounds",
        )

    def _bound_job_record(self, request_id: str | None) -> Any | None:
        if request_id is None:
            return None
        try:
            return self._service.lookup_job_by_request_id(request_id)
        except JobNotFoundError:
            return None

    def _bind(self, task_id: str, generation: int, record: Any) -> ResearchTask:
        self._transition(task_id, generation, "job_found", record=record)
        return self._transition(task_id, generation, "job_state", record=record, finish=True)

    def _unknown_job(self, task_id: str, generation: int, detail: str) -> ResearchTask:
        return self._failure(
            task_id,
            generation,
            _UNKNOWN_OUTCOME,
            _unproven_submission_message(detail),
            "start_backtest",
        )

    def _start_backtest(self, task_id: str, generation: int | None = None) -> ResearchTask:
        if generation is None:
            generation = self._begin(task_id, "backtest")
        if generation is None or not self._owns(task_id, generation):
            return self._view(task_id)
        task = self._store.get(task_id)
        if task.status == "cancelled":
            return self._transition(task_id, generation, "noop", finish=True)
        try:
            record = self._bound_job_record(task.request_id)
        except Exception as error:  # noqa: BLE001 — unknown index boundary
            return self._unknown_job(
                task_id,
                generation,
                f"Job index could not be read: {error}; nothing new was submitted.",
            )
        if record is not None:
            return self._bind(task_id, generation, record)
        if task.backtest_reservation is not None:
            return self._recover_backtest(task_id, generation, task.backtest_reservation)
        confirmed = task.spec_confirmed[-1][2] if task.spec_confirmed else None
        if confirmed is None:
            return self._failure(
                task_id,
                generation,
                "no_confirmed_spec",
                "no confirmed spec exists",
                "start_backtest",
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
        except Exception as error:  # noqa: BLE001 — validation never submitted a job
            return self._failure(
                task_id, generation, "scenario_invalid", str(error), "start_backtest"
            )
        try:
            task = self._transition(task_id, generation, "backtest_reserve", spec=confirmed)
        except BudgetExceededError as error:
            return self._failure(
                task_id, generation, "budget_exceeded", str(error), "start_backtest"
            )
        if task.status == "cancelled":
            return self._transition(task_id, generation, "noop", finish=True)
        task = self._transition(task_id, generation, "backtest_dispatch")
        if task.status == "cancelled":
            return self._transition(
                task_id,
                generation,
                "backtest_pending_release",
                finish=True,
                error=self._error(_INTERRUPTED, "cancelled before dispatch", "start_backtest"),
            )
        try:
            # Second boundary lookup preserves the existing submission contract.
            record = self._bound_job_record(task.request_id)
        except Exception as error:  # noqa: BLE001 — cannot prove absence
            return self._unknown_job(
                task_id, generation, f"Job index could not be read after dispatch: {error}"
            )
        if record is None:
            try:
                record, _created = self._service.create_job(
                    scenario_id=scenario_id,
                    input_identity=cast(dict[str, object], validated["input_identity"]),
                    parameters={
                        "initial_cash": confirmed.initial_cash,
                        "strategy_parameters": confirmed.parameters,
                        "quantity": None,
                        "strategy_source": None,
                    },
                    request_id=cast(str, task.request_id),
                )
            except Exception as error:  # noqa: BLE001 — may have published before raising
                try:
                    record = self._bound_job_record(task.request_id)
                except Exception:  # noqa: BLE001 — still no absence proof
                    record = None
                if record is None:
                    return self._unknown_job(
                        task_id,
                        generation,
                        f"Job submission failed ({error}); publication was not confirmed.",
                    )
        return self._bind(task_id, generation, record)

    def _recover_backtest(
        self, task_id: str, generation: int, reservation: TaskReservation
    ) -> ResearchTask:
        task = self._store.get(task_id)
        try:
            record = self._bound_job_record(task.request_id)
        except Exception:  # noqa: BLE001 — pending alone proves no dispatch
            record = None
        if record is not None:
            return self._bind(task_id, generation, record)
        if reservation.status == "pending":
            return self._transition(
                task_id,
                generation,
                "backtest_pending_release",
                finish=True,
                error=self._error(
                    _INTERRUPTED,
                    "interrupted before dispatch; the slot was released once",
                    "start_backtest",
                ),
            )
        return self._unknown_job(
            task_id,
            generation,
            "Dispatched submission has no confirmed job; no proof rules out late publication.",
        )

    def reconcile(self, task_id: str) -> ResearchTask:
        generation = self._begin(task_id, "reconcile")
        if generation is None:
            return self._view(task_id)
        task = self._store.get(task_id)
        reservation = task.reservation
        if reservation is not None:
            if reservation.status == "pending":
                self._transition(task_id, generation, "model_pending_release", seq=reservation.seq)
                return self._failure(
                    task_id,
                    generation,
                    _INTERRUPTED,
                    "interrupted before dispatch; the model-call charge was released once",
                )
            return self._transition(
                task_id,
                generation,
                "model_unknown",
                finish=True,
                seq=reservation.seq,
                model={"model": "unknown", "at": _now(), "outcome": "unknown_after_dispatch"},
                error=self._error(
                    _UNKNOWN_MODEL,
                    "model outcome unknown after dispatch; call stays charged. "
                    "Cancel this task and create a new one for another attempt",
                ),
            )
        if task.backtest_reservation is not None:
            return self._recover_backtest(task_id, generation, task.backtest_reservation)
        if task.jobs:
            try:
                bound_record = self._service.get_job(task.jobs[-1])
            except Exception:  # noqa: BLE001 — a view failure is not a retry
                return self._transition(task_id, generation, "noop", finish=True)
            return self._transition(
                task_id, generation, "job_state", record=bound_record, finish=True
            )
        if task.status in {"drafting", "confirmed"}:
            try:
                record = self._bound_job_record(task.request_id)
            except Exception:  # noqa: BLE001 — no fabricated spend
                return self._transition(task_id, generation, "noop", finish=True)
            if record is not None:
                return self._bind(task_id, generation, record)
            if task.error is not None or (task.last_model or {}).get("outcome") == "observed":
                return self._transition(task_id, generation, "noop", finish=True)
            code = (
                _UNKNOWN_MODEL
                if (task.last_model or {}).get("outcome") == "unknown_after_dispatch"
                else _INTERRUPTED
            )
            return self._failure(
                task_id,
                generation,
                code,
                "previous attempt interrupted; cancel this task "
                "if its dispatched outcome is unknown",
            )
        return self._transition(task_id, generation, "noop", finish=True)

    def reconcile_all(self) -> list[ResearchTask]:
        result: list[ResearchTask] = []
        for task in self._store.list():
            try:
                result.append(self.reconcile(task.task_id))
            except (ResearchTaskError, OSError):
                continue
        return result

    def refresh(self, task_id: str) -> ResearchTask:
        return self.reconcile(task_id)

    def retry(self, task_id: str) -> ResearchTask:
        task = self.reconcile(task_id)
        if task.status == "cancelled":
            raise TaskStateError("a cancelled research task cannot be retried")
        if self._is_active(task_id):
            return self._view(task_id)
        generation = self._begin(task_id, "retry")
        if generation is None:
            return self._view(task_id)
        task = self._store.get(task_id)
        if task.status != "failed" or task.error is None:
            self._transition(task_id, generation, "noop", finish=True)
            raise TaskStateError("only a failed task can be retried; cancelled tasks cannot retry")
        if (
            task.error["code"] in _NON_RETRYABLE_CODES
            or (task.last_model or {}).get("outcome") == "unknown_after_dispatch"
        ):
            self._transition(task_id, generation, "noop", finish=True)
            raise TaskStateError(
                "outcome unknown: cancel this task and create a new one for another attempt"
            )
        if task.error["step"] == "propose":
            return self._propose(task_id, generation)
        return self._start_backtest(task_id, generation)

    def cancel(self, task_id: str) -> ResearchTask:
        return self._transition(task_id, None, "cancel")

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

    def _edited_spec(self, task: ResearchTask, fields: dict[str, Any]) -> ResearchSpec:
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
        return merged

    def edit_spec(self, task_id: str, fields: dict[str, Any]) -> ResearchTask:
        generation = self._begin(task_id, "edit")
        if generation is None:
            raise TaskStateError("task is cancelled or an operation is in flight")
        try:
            spec = self._edited_spec(self._store.get(task_id), fields)
        except ResearchTaskError:
            self._transition(task_id, generation, "noop", finish=True)
            raise
        return self._transition(task_id, generation, "edit", finish=True, spec=spec)

    def confirm(self, task_id: str, *, auto_run: bool = True) -> ResearchTask:
        generation = self._begin(task_id, "confirm")
        if generation is None:
            raise TaskStateError("task is cancelled or an operation is in flight")
        try:
            task = self._store.get(task_id)
            if task.spec_draft is None:
                raise TaskStateError("no unconfirmed draft spec is available")
            spec = task.spec_draft
            self._validate_spec(spec)
            spec = self._with_defaults(spec)
        except ResearchTaskError:
            self._transition(task_id, generation, "noop", finish=True)
            raise
        task = self._transition(task_id, generation, "confirm", finish=True, spec=spec)
        return self._start_backtest(task_id) if auto_run and task.status != "cancelled" else task

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
