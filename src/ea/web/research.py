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

import hashlib
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

from ea.plugin.builtins import builtin_capabilities, builtin_catalog
from ea.plugin.catalog import PluginCatalogV1, VerifiedPluginV1
from ea.plugin.declarative import (
    DECLARATION_KINDS,
    DECLARATION_SCHEMA,
    STRATEGY_TARGET_PLUGIN_ID,
    VALIDATOR_CONSTRAINTS,
    VALIDATOR_TARGET_PLUGIN_ID,
    DeclarationError,
    DeclarationV1,
    declared_strategy_rejection,
    declared_validator_rejection,
    decode_declaration,
    seal_declaration,
)
from ea.plugin.local_strategy import (
    LOCAL_STRATEGY_ENTRYPOINT,
    LocalStrategyCapabilityV1,
    select_local_strategy,
)
from ea.plugin.manifest import ManifestError
from ea.plugin.selection import (
    PluginBindingV1,
    PluginSelectionV1,
    resolve_selection,
    verify_binding,
)
from ea.web import report_claims
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
_RECIPE_SCHEMA = "ea.plugin-recipe.v1"
_DECLARATION_CATALOG_SCHEMA = "ea.plugin-declaration-catalog.v1"
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
_INTERPRETATION_EVIDENCE_SCHEMA = "radian.research-interpretation-evidence.v1"
_INTERPRETATION_EVIDENCE_RECEIPT_SCHEMA = "radian.research-interpretation-evidence-receipt.v1"
_INTERPRETATION_OPERATION = "radian.research-interpretation.v1"
_INTERPRETATION_PROMPT_VERSION = "radian.research-interpretation-prompt.v2"
_INTERPRETATION_TOOL_VERSION = "radian.research-interpretation-tools.v2"
# The raw conclusion arguments are kept for audit only, never displayed, so a
# model that sends an oversized blob cannot grow the durable record. An
# over-limit call is refused with its size and digest recorded, never truncated
# into something that could re-parse as a valid conclusion.
_MAX_EVIDENCE_ARGUMENT_BYTES = 4000
# The same reasoning bounds the recorded call set as a whole, on both axes. The
# legal contract is exactly one conclusion call, so 8 is eight times what the
# contract needs and 16384 bytes is four times the largest single argument payload
# the bound above admits. Both are counted over the complete id/name/arguments
# set, so the total binds before a per-call-only scheme could grow to 8 x 4000.
_MAX_EVIDENCE_CALLS = 8
_MAX_EVIDENCE_CALL_BYTES = 16384
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


class SelectionRejectedError(ResearchTaskError):
    """A selected capability is unknown, incompatible, or not authorized here."""

    def __init__(self, error: ManifestError) -> None:
        super().__init__(f"{error.code}: {error.message}")
        self.code = error.code
        self.message = error.message


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
    # The user's explicit capability selection, per layer. Absent means the host
    # default route, which is what every task written before this field used.
    # The model never sets this: it is not a propose_spec argument.
    selection: dict[str, Any] | None = None
    # One comparison frozen before the run: an allowlisted report path plus the
    # sign the idea expects. Absent on every task written before this field, and
    # such a task concludes honestly that nothing was stated to compare against.
    criterion: dict[str, Any] | None = None

    def document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
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
        # Additive: a spec with no explicit selection keeps its original shape.
        if self.selection is not None:
            document["selection"] = self.selection
        if self.criterion is not None:
            document["criterion"] = self.criterion
        return document


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
    # The user's explicit selection for this task, and the exact binding frozen
    # at confirmation. Both are additive: an unselected task keeps its shape.
    selection: dict[str, Any] | None = None
    pinned_binding: dict[str, Any] | None = None
    # One host-verified declaration proposal the model drafted for this task. It is
    # evidence for the user to read and install themselves: never a selection,
    # never a registration and never a run.
    declaration_draft: dict[str, Any] | None = None
    # The conclusion axis, kept separate from computation. `interpretation_evidence`
    # is the observed model response persisted before anything is published;
    # `interpretation` is the host-rendered conclusion the user reads; and
    # `interpretation_error` is a technical failure of the interpretation step,
    # which never rewrites the job's own succeeded computation.
    interpretation: dict[str, Any] | None = None
    interpretation_evidence: dict[str, Any] | None = None
    interpretation_error: dict[str, Any] | None = None

    def document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
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
        if self.selection is not None:
            document["selection"] = self.selection
        if self.pinned_binding is not None:
            document["pinned_binding"] = self.pinned_binding
        if self.declaration_draft is not None:
            document["declaration_draft"] = self.declaration_draft
        if self.interpretation is not None:
            document["interpretation"] = self.interpretation
        if self.interpretation_evidence is not None:
            document["interpretation_evidence"] = self.interpretation_evidence
        if self.interpretation_error is not None:
            document["interpretation_error"] = self.interpretation_error
        return document


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _revision(task: ResearchTask) -> ResearchTask:
    """The reader-visible revision of a task: everything but writer bookkeeping.

    ``updated_at`` and the operation generation/kind exist to coordinate writers,
    not to carry research state. A transition that leaves this projection
    unchanged is a no-op for the reader, so it must not read as a new revision —
    a refresh that re-verifies the same report would otherwise move the
    timestamp on every poll while showing the same conclusion.
    """
    return replace(
        task,
        updated_at="",
        operation_generation=0,
        operation_completion_generation=0,
        operation_kind=None,
    )


def _idea_key(raw_idea: str) -> str:
    return " ".join(raw_idea.split())


def _conclusion_closed(task: ResearchTask) -> bool:
    """Whether the conclusion axis of this task already holds its durable answer.

    The axis is separate from the run: a task may be ``succeeded`` with a
    published conclusion, with a recorded interpretation failure, or with the
    conclusion closed by the user's cancellation. Once closed it is never
    reopened, so no new slot may be reserved for it and no late result or
    failure may overwrite it.
    """
    return task.interpretation is not None or task.interpretation_error is not None


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


def _declaration_body(arguments: dict[str, Any]) -> dict[str, Any]:
    """The closed declaration body a model-authored proposal may fill in.

    Everything outside these fields is host-fixed — the schema, the package
    version and the declared-by value — so a model cannot propose a different
    artifact kind, a floating version, or a claim that the host itself declared
    it. The body is data: it is sealed, verified against the host primitives and
    never imported, evaluated or installed by this path.
    """
    return {
        "schema": DECLARATION_SCHEMA,
        "declaration_id": arguments.get("declaration_id"),
        "kind": arguments.get("kind"),
        "package_version": "1.0.0",
        "title": arguments.get("title"),
        "declared_by": "user",
        "rule": arguments.get("rule"),
        "rule_history": arguments.get("rule_history"),
        "constraints": arguments.get("constraints"),
    }


def _declaration_result_text(proposal: dict[str, Any]) -> str:
    """What the model is told about its own declaration proposal.

    A proposal is host-verified evidence for the user to look at. It is never an
    installation, never a selection and never a run, and the model is told
    exactly that so it cannot report otherwise to the user.
    """
    if proposal.get("usable") is not True:
        return f"rejected: {proposal.get('message') or proposal.get('code')}"
    document = proposal.get("declaration")
    declaration_id = document.get("declaration_id") if isinstance(document, dict) else None
    return (
        f"verified by the host and NOT installed: {declaration_id} exists only if the user "
        "registers it explicitly. Nothing was selected, replaced, registered or run."
    )


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
        selection = document.get("selection")
        pinned_binding = document.get("pinned_binding")
        declaration_draft = document.get("declaration_draft")
        if (selection is not None and type(selection) is not dict) or (
            pinned_binding is not None and type(pinned_binding) is not dict
        ):
            raise ResearchTaskError("research task capability record is unreadable")
        if declaration_draft is not None and type(declaration_draft) is not dict:
            raise ResearchTaskError("research task declaration proposal is unreadable")
        interpretation = document.get("interpretation")
        interpretation_evidence = document.get("interpretation_evidence")
        interpretation_error = document.get("interpretation_error")
        for name, value in (
            ("interpretation", interpretation),
            ("interpretation evidence", interpretation_evidence),
            ("interpretation error", interpretation_error),
        ):
            if value is not None and type(value) is not dict:
                raise ResearchTaskError(f"research task {name} is unreadable")
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
            selection=selection,
            pinned_binding=pinned_binding,
            declaration_draft=declaration_draft,
            interpretation=interpretation,
            interpretation_evidence=interpretation_evidence,
            interpretation_error=interpretation_error,
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
    selection = document.get("selection")
    if selection is not None and type(selection) is not dict:
        # A record this product cannot read must not become a half-read spec.
        raise ResearchTaskError("research spec selection is unreadable")
    try:
        criterion = (
            None
            if document.get("criterion") is None
            else report_claims.decode_criterion(document.get("criterion"))
        )
    except report_claims.CriterionRejectedError as error:
        raise ResearchTaskError(f"research spec criterion is unreadable: {error}") from None
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
        selection=None if selection is None else dict(selection),
        criterion=criterion,
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
        local = self._local_scenario_facts()
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
        if local is not None:
            # The workspace-local slot, described from static facts only: no package
            # is loaded, compiled or executed to offer it, and it is offered only
            # while its trust is current.
            entries.append(local)
        return entries[:_MAX_CATALOG_ENTRIES]

    def _local_scenario_facts(self) -> dict[str, Any] | None:
        """Static facts of the trusted local slot, or None. Imports nothing."""
        facts = getattr(self._service, "local_strategy_facts", None)
        if facts is None:
            return None
        try:
            return cast(dict[str, Any] | None, facts())
        except ManifestError as error:
            raise SelectionRejectedError(error) from None

    # -- capability catalog and recipe --------------------------------------

    def _model_route(self) -> dict[str, Any]:
        """The non-secret facts of the route this host is configured for.

        Only what the settings status already projects: provider, model and
        endpoint. No credential, key reference or resolution value is read, and
        nothing here creates a provider, a client or a request.
        """
        try:
            status = self._settings.status()
        except (RadianSettingsError, ValueError):
            return {}
        return {
            "provider": status.get("provider"),
            "model": status.get("model"),
            "endpoint": status.get("endpoint"),
        }

    def _declaration_store(self) -> Any:
        store = getattr(self._service, "declarations", None)
        if store is None:
            raise SelectionRejectedError(
                ManifestError(
                    "declaration_unavailable",
                    "this service has no workspace declaration store",
                )
            )
        return store

    def _registered_declarations(self) -> tuple[DeclarationV1, ...]:
        try:
            return cast(tuple[DeclarationV1, ...], self._declaration_store().load())
        except DeclarationError as error:
            raise SelectionRejectedError(ManifestError(error.code, error.message)) from None

    def _plugin_catalog(self) -> PluginCatalogV1:
        """Built from live host facts on every use, so no stale claim survives.

        The trusted workspace-local slot is composed in through the catalog's own
        public records, and only while its trust is current: an untrusted, revoked
        or replaced package is simply not offered here.
        """
        store = getattr(self._service, "declarations", None)
        try:
            declarations = () if store is None else tuple(store.load())
            capabilities = builtin_capabilities(self._service, self._model_route(), declarations)
        except DeclarationError as error:
            raise SelectionRejectedError(ManifestError(error.code, error.message)) from None
        local = self._local_strategy_capability()
        if local is not None:
            capabilities = [*capabilities, local.pair()]
        return PluginCatalogV1(
            [manifest for _adapter, manifest in capabilities],
            [adapter for adapter, _manifest in capabilities],
        )

    def _local_strategy_capability(self) -> LocalStrategyCapabilityV1 | None:
        """The current trusted local capability, re-derived on every read."""
        current = getattr(self._service, "local_strategy_capability", None)
        if current is None:
            return None
        try:
            return cast(LocalStrategyCapabilityV1 | None, current())
        except ManifestError as error:
            raise SelectionRejectedError(error) from None

    def plugin_catalog(self) -> dict[str, Any]:
        """The declared catalog with the host's verified view of each entry."""
        return self._plugin_catalog().document()

    def declaration_catalog(self) -> dict[str, Any]:
        """Every declaration this workspace registered, with the host's view."""
        store = self._declaration_store()
        declarations = self._registered_declarations()
        catalog = self._plugin_catalog()
        entries: list[dict[str, Any]] = []
        for declaration in declarations:
            document = declaration.document()
            try:
                verified = catalog.get(declaration.plugin_id, declaration.package_version)
            except ManifestError as error:
                entries.append(
                    {
                        **document,
                        "usable": False,
                        "reason": {"code": error.code, "message": error.message},
                    }
                )
            else:
                entries.append({**document, "usable": True, "capability": verified.document()})
        return {
            "schema": _DECLARATION_CATALOG_SCHEMA,
            "supported_kinds": list(DECLARATION_KINDS),
            "validator_constraints": list(VALIDATOR_CONSTRAINTS),
            "declaration_root_configured": store is not None,
            "declarations": entries,
        }

    def inspect_declaration(self, payload: object) -> dict[str, Any]:
        """Read and verify one declaration against the host; register nothing."""
        return self._declaration_outcome(payload, register=False)

    def register_declaration(self, payload: object) -> dict[str, Any]:
        """Explicit user registration: verify first, then persist add-only."""
        return self._declaration_outcome(payload, register=True)

    def _declaration_outcome(self, payload: object, *, register: bool) -> dict[str, Any]:
        """One declaration's verdict, with nothing stored when it cannot run.

        A declaration is a proposal: the AI may author its body, but only this
        explicit user action installs it, and an installed one is still only a
        preset over a host primitive that the host itself verifies.
        """
        try:
            declaration = decode_declaration(payload)
        except DeclarationError as error:
            return {
                "usable": False,
                "code": error.code,
                "message": error.message,
                "declaration": None,
                "capability": None,
                "registered": False,
            }
        store = self._declaration_store()
        if register and store.has(declaration.declaration_id):
            return {
                "usable": False,
                "code": "declaration_exists",
                "message": (
                    f"declaration {declaration.declaration_id} is already registered and is "
                    "never overwritten"
                ),
                "declaration": declaration.document(),
                "capability": None,
                "registered": False,
            }
        try:
            catalog = builtin_catalog(
                self._service,
                self._model_route(),
                (*self._registered_declarations(), declaration),
            )
            verified = catalog.get(declaration.plugin_id, declaration.package_version)
        except (DeclarationError, ManifestError) as error:
            return {
                "usable": False,
                "code": getattr(error, "code", "declaration_rejected"),
                "message": getattr(error, "message", str(error)),
                "declaration": declaration.document(),
                "capability": None,
                "registered": False,
            }
        if not register:
            return {
                "usable": True,
                "code": None,
                "message": None,
                "declaration": declaration.document(),
                "capability": verified.document(),
                "registered": store.has(declaration.declaration_id),
            }
        try:
            document = store.store(declaration)
        except DeclarationError as error:
            return {
                "usable": False,
                "code": error.code,
                "message": error.message,
                "declaration": declaration.document(),
                "capability": None,
                "registered": False,
            }
        return {
            "usable": True,
            "code": None,
            "message": None,
            "declaration": document,
            "capability": verified.document(),
            "registered": True,
        }

    def _declaration_proposal(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """One model-drafted declaration, verified by this host and never installed.

        The model only fills in the closed body fields; the host seals the body,
        validates the rule against the closed engine and the constraints against the
        closed vocabulary, and checks the result can be bound to a host primitive.
        What comes back is evidence for the user to read and install, or to ignore.
        Registration is the separate explicit action above, and the model has no
        path to it.
        """
        try:
            payload = json.dumps(
                seal_declaration(_declaration_body(arguments)),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
        except (DeclarationError, TypeError, ValueError) as error:
            return {
                "usable": False,
                "code": getattr(error, "code", "declaration_field"),
                "message": getattr(error, "message", str(error)),
                "declaration": None,
                "capability": None,
                "registered": False,
            }
        return self.inspect_declaration(payload)

    def _dataset_entry(self, dataset_id: object) -> dict[str, Any] | None:
        """The host's own evidence for one bound input, or None when unreadable."""
        if type(dataset_id) is not str:
            return None
        catalog = getattr(self._service, "dataset_catalog", None)
        if catalog is None:
            return None
        try:
            return cast(dict[str, Any], catalog.inspect(dataset_id))
        except (OSError, ValueError):
            return None

    def _check_declared(
        self, binding: PluginBindingV1, parameters: dict[str, Any], dataset_id: str | None
    ) -> None:
        """A declared preset must match what will actually run."""
        reason = declared_strategy_rejection(binding.identity("strategy"), parameters)
        if reason is not None:
            raise InvalidSpecError(reason)
        reason = declared_validator_rejection(
            binding.identity("validator"), self._dataset_entry(dataset_id)
        )
        if reason is not None:
            raise InvalidSpecError(reason)

    def _authorize_plugin(self, plugin: VerifiedPluginV1) -> None:
        """Current host policy for one selected capability, before any dispatch.

        This can only refuse. A model capability must be the route this host is
        actually configured for and must declare the capability research needs;
        nothing here reads or forwards a credential, and no new provider or
        endpoint is created.
        """
        if plugin.adapter.entrypoint == LOCAL_STRATEGY_ENTRYPOINT:
            # Current host policy for a local strategy is exactly the current grant:
            # the identity this run selects must still be the identity this workspace
            # trusts and is permitted to compute with.
            capability = self._local_strategy_capability()
            if capability is None:
                raise ManifestError(
                    "grant_required",
                    "this run selects a local strategy package, and this workspace has "
                    "no currently trusted one",
                )
            if (plugin.manifest.plugin_id, plugin.manifest.package_version) != (
                capability.plugin_id,
                capability.package_version,
            ) or plugin.manifest.artifact_digest != capability.manifest.artifact_digest:
                raise ManifestError(
                    "local_strategy_unbound",
                    "the selected local capability is not the package this workspace "
                    "currently trusts; inspect and trust it again",
                )
            slot = self._service.local_strategy
            select_local_strategy(capability, grant=slot.grant())
            return
        if plugin.manifest.kind != "model":
            return
        try:
            status = self._settings.status()
        except (RadianSettingsError, ValueError) as error:
            raise ManifestError("model_settings_unavailable", str(error)) from None
        provider = plugin.adapter.identity.get("provider")
        configured = status.get("provider")
        if provider != configured:
            raise ManifestError(
                "model_route_not_configured",
                f"the configured model route is {configured or 'not configured'}; "
                f"this run selected {provider}",
            )
        missing = tuple(status.get("missing_capabilities") or ())
        if missing:
            raise ManifestError(
                "model_capability_missing",
                "the configured model protocol does not declare: " + ", ".join(missing),
            )
        if not status.get("configured"):
            raise ManifestError(
                "model_route_not_configured",
                str(status.get("configuration_error") or "no usable model provider is configured"),
            )

    def _resolve_binding(self, selection: dict[str, Any]) -> PluginBindingV1:
        """Resolve one explicit selection to exact versions, or refuse it."""
        try:
            return resolve_selection(
                self._plugin_catalog(),
                PluginSelectionV1.decode(selection),
                authorize=self._authorize_plugin,
            )
        except ManifestError as error:
            raise SelectionRejectedError(error) from None

    @staticmethod
    def _selection_payload(selection: Any) -> dict[str, Any] | None:
        """Normalize one requested selection; empty means the host default."""
        if selection is None:
            return None
        if type(selection) is not dict:
            raise SelectionRejectedError(
                ManifestError("selection_invalid", "selection must be one object keyed by layer")
            )
        try:
            decoded = PluginSelectionV1.decode(selection)
        except ManifestError as error:
            raise SelectionRejectedError(error) from None
        return None if not decoded else dict(decoded.document()["layers"])

    def resolve_recipe(self, selection: Any) -> dict[str, Any]:
        """Inspect one recipe: resolve and verify it, dispatch nothing."""
        try:
            payload = self._selection_payload(selection)
            binding = None if payload is None else self._resolve_binding(payload)
        except SelectionRejectedError as error:
            return {
                "schema": _RECIPE_SCHEMA,
                "usable": False,
                "code": error.code,
                "message": error.message,
                "selection": selection,
                "binding": None,
            }
        return {
            "schema": _RECIPE_SCHEMA,
            "usable": True,
            "code": None,
            "message": None,
            "selection": payload,
            "binding": None if binding is None else binding.document(),
        }

    def _check_selection(self, spec: ResearchSpec) -> None:
        """The user's selection must be the one the spec actually executes."""
        if spec.selection is None:
            return
        binding = self._resolve_binding(spec.selection)
        strategy = binding.identity("strategy")
        if strategy and strategy.get("strategy_id") != spec.strategy_id:
            raise InvalidSpecError(
                f"the selected strategy capability is {strategy.get('strategy_id')}; "
                "the spec must use that strategy"
            )
        data = binding.identity("data")
        if data and spec.dataset_id != data.get("dataset_id"):
            raise InvalidSpecError(
                f"the selected data capability is {data.get('dataset_id')}; "
                "the spec must name that dataset_id"
            )
        # A declared preset must match the spec that will actually be dispatched.
        self._check_declared(binding, spec.parameters, spec.dataset_id)

    def _frozen_binding(
        self, task: ResearchTask, confirmed: ResearchSpec
    ) -> PluginBindingV1 | None:
        """Re-verify the binding frozen at confirmation; never substitute one."""
        if confirmed.selection is None:
            # Defensive: a spec that names an uploaded version without a data
            # capability cannot freeze its provenance, so it must fail here rather
            # than reach the engine or be reported as an unobserved dispatch.
            if self._uploaded_input(confirmed.dataset_id):
                raise SelectionRejectedError(
                    ManifestError(
                        "upload_data_capability_required",
                        f"{confirmed.dataset_id} is an uploaded version; it is usable only "
                        "through a research recipe that selects its data capability",
                    )
                )
            return None
        if task.pinned_binding is None:
            raise SelectionRejectedError(
                ManifestError(
                    "plugin_binding_missing",
                    "the confirmed spec names capabilities but no binding was frozen for it",
                )
            )
        try:
            binding = PluginBindingV1.decode(task.pinned_binding)
            verify_binding(self._plugin_catalog(), binding, authorize=self._authorize_plugin)
        except ManifestError as error:
            raise SelectionRejectedError(error) from None
        return binding

    def _recheck_selection(self, task: ResearchTask) -> None:
        """Re-verify this run's capabilities before every model dispatch.

        A catalog, plugin or configuration change after the task was created must
        stop the run before it can call a provider again, so a capability that is
        no longer verified — or no longer the route this host is configured for —
        is refused here instead of being discovered inside the provider call.
        """
        if task.pinned_binding is not None:
            try:
                verify_binding(
                    self._plugin_catalog(),
                    PluginBindingV1.decode(task.pinned_binding),
                    authorize=self._authorize_plugin,
                )
            except ManifestError as error:
                raise SelectionRejectedError(error) from None
            return
        if task.selection is not None:
            self._resolve_binding(task.selection)

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

    def _uploaded_input(self, dataset_id: str | None) -> bool:
        """True when this dataset id is a published upload, not registered data."""
        if dataset_id is None:
            return False
        uploads = getattr(self._service, "uploads", None)
        return uploads is not None and bool(uploads.has(dataset_id))

    def _validate_spec(self, spec: ResearchSpec) -> None:
        if self._uploaded_input(spec.dataset_id):
            # An uploaded version has provenance only through the data capability
            # bound to it: that component is what freezes the version evidence
            # digest alongside the accepted input. A spec that names an upload
            # while the user selected no data capability — or a different one —
            # would run with nothing pinning where its bars came from, so it is
            # refused here, before confirmation and before any engine dispatch.
            selected = (spec.selection or {}).get("data")
            expected = f"ea.builtin.data.{spec.dataset_id}"
            if not isinstance(selected, dict) or selected.get("plugin_id") != expected:
                raise SelectionRejectedError(
                    ManifestError(
                        "upload_data_capability_required",
                        f"{spec.dataset_id} is an uploaded version; it is usable only through a "
                        f"research recipe that selects its data capability {expected}, because "
                        "that binding is what freezes the version evidence",
                    )
                )
        if spec.selection is not None:
            # A selected capability is verified before the spec surface, so an
            # unusable selection is reported as itself and never dispatched.
            self._check_selection(spec)
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
                        "criterion": {
                            "type": "object",
                            "description": (
                                "One comparison frozen before the run, so the result can be "
                                "interpreted afterwards. field must be one of "
                                f"{', '.join(sorted(report_claims.FIELD_SPECS_BY_PATH))}; "
                                "expectation is the sign the idea predicts: "
                                f"{', '.join(report_claims.EXPECTATIONS)}. Omit it only "
                                "when the idea states no testable expectation."
                            ),
                            "properties": {
                                "field": {
                                    "type": "string",
                                    "enum": sorted(report_claims.FIELD_SPECS_BY_PATH),
                                },
                                "expectation": {
                                    "type": "string",
                                    "enum": list(report_claims.EXPECTATIONS),
                                },
                            },
                            "required": ["field", "expectation"],
                        },
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
            {
                "name": "propose_declaration",
                "description": (
                    "Draft one declarative capability for the user to read and install "
                    "themselves. This never installs, selects, replaces or runs anything: the "
                    "host verifies the body, the user registers it explicitly, and until then "
                    "it does not exist. Kinds: "
                    f"{', '.join(DECLARATION_KINDS)}. A strategy declaration wraps the host "
                    f"rule strategy {STRATEGY_TARGET_PLUGIN_ID} and needs exactly one bounded "
                    "price rule (the same rule grammar as propose_spec) plus its rule_history. "
                    "A validator declaration wraps the host scenario validator "
                    f"{VALIDATOR_TARGET_PLUGIN_ID} and may only make a run stricter, using "
                    f"these constraints: {', '.join(VALIDATOR_CONSTRAINTS)}. A strategy "
                    "declaration takes no constraints, a validator declaration takes no rule. "
                    "declaration_id must be a lowercase namespaced identifier such as "
                    "user.rule.close-above-open; the host namespace ea.* is refused."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": list(DECLARATION_KINDS)},
                        "declaration_id": {"type": "string"},
                        "title": {"type": "string"},
                        "rule": {"type": "string"},
                        "rule_history": {"type": "integer"},
                        "constraints": {"type": "object"},
                    },
                    "required": ["kind", "declaration_id", "title"],
                },
            },
        ]

        return [ToolDefinition(**definition) for definition in definitions]

    def _selected_capabilities(self, task: ResearchTask | None) -> str:
        """The user's fixed selection, described to the model as a constraint."""
        if task is None or not task.selection:
            return ""
        wanted = {
            item.get("plugin_id") for item in task.selection.values() if isinstance(item, dict)
        }
        entries = [
            entry for entry in self._plugin_catalog().entries() if entry["plugin_id"] in wanted
        ]
        listing = json.dumps(entries, ensure_ascii=True, separators=(",", ":"))
        return (
            "\n\nThe user has already selected the capabilities below. The selection is "
            "theirs, is fixed, and cannot be changed, replaced or extended by you: the "
            "proposed spec must use exactly the selected strategy and dataset identity. "
            "There is no tool argument that alters a selection.\nSelected capabilities "
            f"(JSON):\n{listing}"
        )

    def _system_prompt(self, task: ResearchTask | None = None) -> str:
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
            "about what this run can and cannot show. Freeze exactly one criterion "
            "before the run whenever the idea states a testable expectation: one "
            "allowlisted report field plus the sign the idea predicts. The result is "
            "interpreted against that frozen criterion and nothing else, so a criterion "
            "that only partly represents the question will be reported as insufficient "
            "evidence rather than as support for the idea. Never state or imply a "
            "profit, and never claim a run proves an idea. propose_declaration only "
            "drafts a "
            "bounded capability preset for the user to install themselves: you cannot "
            "install, select, replace or run one, and you must never claim you did."
            "\n\nRegistered catalog (JSON):\n"
            f"{listing}{self._selected_capabilities(task)}"
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
            # Bookkeeping (generation, kind, durability barrier) is still saved
            # below in every case; only a change the reader can see is a new
            # revision and only that advances `updated_at`.
            if event == "create" or _revision(next_task) != _revision(current):
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
            if task.status == "succeeded":
                # The computation is already durable and terminal, so cancelling
                # closes the conclusion axis instead of rewriting the run. The
                # job, its report and the succeeded status are untouched.
                if _conclusion_closed(task):
                    return task
                # Closing the axis and settling its outstanding reservation are one
                # durable write. A charge that never reached the provider is not
                # spent, so it is returned with the cancellation; a dispatched call
                # cannot be proven either way and stays charged and unknown.
                reservation = task.reservation
                released = reservation is not None and reservation.status == "pending"
                return replace(
                    task,
                    reservation=None if released else reservation,
                    usage=(
                        {
                            **task.usage,
                            "model_calls": max(0, task.usage["model_calls"] - 1),
                        }
                        if released
                        else task.usage
                    ),
                    cancel_note=(
                        "The completed run and its verified report are unchanged; "
                        "no conclusion will be published for it."
                    ),
                    interpretation_error={
                        "step": "interpret",
                        "code": "cancelled_by_user",
                        "message": "the user cancelled before a conclusion was published",
                    },
                )
            if task.status in {"unsupported", "cancelled"}:
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
            if data.get("step") == "interpret" and _conclusion_closed(task):
                # The conclusion axis already has its durable answer, so no new
                # slot may be reserved for it. This is the same write gate the
                # cancellation used, so a call in flight cannot slip a dispatch
                # past a cancel that landed first.
                raise TaskStateError("the conclusion axis is already closed")
            if task.reservation is not None:
                raise TaskStateError("a model call is already reserved")
            if task.usage["model_calls"] >= task.budget["max_model_calls"]:
                raise BudgetExceededError("research model-call budget is exhausted")
            if data.get("hold_back_last_call") and (
                task.usage["model_calls"] + 1 >= task.budget["max_model_calls"]
            ):
                # One call is kept for the conclusion. A run whose own result could
                # never be interpreted is refused before it is proposed, not after.
                raise BudgetExceededError(
                    "research model-call budget cannot pay for a conclusion after this call"
                )
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
            "model_concluded",
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
                if event in {"model_observed", "model_concluded"}:
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
                if event == "model_concluded":
                    # The charge, the observed outcome and the exact response it
                    # produced land in one durable write: there is no image in
                    # which a slot is spent and its evidence is gone.
                    changes["interpretation_evidence"] = data["evidence"]
            if data.get("error") and not _conclusion_closed(task):
                # A failure observed after the axis already has its durable
                # answer is a late arrival: it may settle its own reservation
                # above, but it never rewrites the run's outcome.
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
                "pinned_binding": data.get("binding"),
            }
        elif event == "declaration_proposal":
            proposal = data["proposal"]
            if type(proposal) is not dict:
                raise TaskStateError("a declaration proposal must be one document")
            # Additive evidence only: the latest host-verified proposal replaces an
            # earlier one. Nothing is installed, selected, replaced or run here.
            changes = {"declaration_draft": proposal}
        elif event == "interpretation_evidence":
            # Durable before anything is published: the observed response and the
            # exact identities it was produced under. This is evidence, never a
            # conclusion, so a cancelled task may keep it.
            changes = {"interpretation_evidence": data["evidence"]}
        elif event == "interpretation":
            if _conclusion_closed(task):
                # The axis already has its durable answer. A conclusion that
                # arrives after it closed is never revived.
                raise TaskStateError("the conclusion axis is already closed")
            if data["evidence"] != task.interpretation_evidence:
                raise TaskStateError("no matching interpretation evidence is persisted")
            changes = {
                "interpretation": data["interpretation"],
                "interpretation_error": None,
            }
        elif event == "interpretation_failure":
            if _conclusion_closed(task):
                # Cancellation, or an earlier failure, is already the durable
                # answer: a late failure never overwrites it.
                return task
            changes = {"interpretation_error": data["error"]}
        elif event == "interpretation_refresh":
            # Re-verified against the same report the served conclusion was
            # derived from, so the served reading is replaced in place.
            changes = {
                "interpretation": data["interpretation"],
                "interpretation_error": None,
            }
        elif event == "interpretation_stale":
            # The report this conclusion was derived from is no longer
            # available or no longer the same document. Withdrawing it is the
            # only honest answer; the run itself is untouched.
            changes = {"interpretation": None, "interpretation_error": data["error"]}
        else:
            raise TaskStateError(f"unknown task transition: {event}")
        if task.status == "cancelled":
            # A late result may add evidence and settle its own reservation.
            # It never changes cancellation, installs a spec, publishes a
            # conclusion or starts work.
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
                    "interpretation_evidence",
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

    def create(self, raw_idea: str, selection: Any = None) -> ResearchTask:
        idea = _bounded_text(raw_idea, field="idea", maximum=_MAX_IDEA_BYTES)
        # The user's explicit selection is verified, and if unusable refused,
        # before a task record exists and before any provider call.
        requested = self._selection_payload(selection)
        if requested is not None:
            self._resolve_binding(requested)
        now = _now()
        task = ResearchTask(
            task_id=str(uuid4()),
            created_at=now,
            updated_at=now,
            status="drafting",
            raw_idea=idea,
            gap=None,
            budget=self._budget(),
            usage={
                "model_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "backtests": 0,
                "unavailable_model_calls": 0,
            },
            spec_draft=None,
            spec_confirmed=(),
            jobs=(),
            request_id=str(uuid4()),
            error=None,
            cancel_note=None,
            last_model=None,
            selection=requested,
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
                task = self._transition(
                    task_id, generation, "model_reserve", hold_back_last_call=True
                )
            except BudgetExceededError as error:
                return self._failure(task_id, generation, "budget_exceeded", str(error))
            if task.status == "cancelled":
                return self._transition(task_id, generation, "noop", finish=True)
            assert task.reservation is not None
            seq = task.reservation.seq
            # Re-checked before the dispatch, so a refused round never reaches the
            # provider and its reserved slot is released rather than charged.
            try:
                self._recheck_selection(task)
            except SelectionRejectedError as error:
                return self._transition(
                    task_id,
                    generation,
                    "model_pending_release",
                    finish=True,
                    seq=seq,
                    error=self._error(error.code, error.message),
                )
            task = self._transition(task_id, generation, "model_dispatch")
            if task.status == "cancelled":
                return self._transition(
                    task_id, generation, "model_pending_release", finish=True, seq=seq
                )
            try:
                response = self._provider.complete(
                    system=self._system_prompt(task), messages=messages, tools=self._tools()
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
                        # The user's selection, never the model's, is attached.
                        spec = replace(spec, selection=task.selection)
                        self._validate_spec(spec)
                        spec = self._with_defaults(spec)
                    except InvalidSpecError as error:
                        results.append(ModelMessage("tool", f"rejected: {error}", call_id=call.id))
                        continue
                    except SelectionRejectedError as error:
                        return self._failure(task_id, generation, error.code, error.message)
                    return self._transition(
                        task_id,
                        generation,
                        "proposal",
                        finish=True,
                        status="awaiting_confirmation",
                        spec=spec,
                    )
                if name == "propose_declaration":
                    # Recorded as evidence for the user, and the round continues: a
                    # declaration proposal is never an installation, a selection or a
                    # run, and it cannot replace the user's own selection.
                    proposal = self._declaration_proposal(call.arguments)
                    task = self._transition(
                        task_id, generation, "declaration_proposal", proposal=proposal
                    )
                    results.append(
                        ModelMessage("tool", _declaration_result_text(proposal), call_id=call.id)
                    )
                    continue
                results.append(
                    ModelMessage(
                        "tool", f"tool {name!r} is not available to research", call_id=call.id
                    )
                )
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
        if task.usage["model_calls"] >= task.budget["max_model_calls"]:
            # The conclusion slot is gone: this run's own result could never be
            # interpreted, so it is refused before the engine sees it.
            return self._failure(
                task_id,
                generation,
                "budget_exceeded",
                "research model-call budget leaves no call for a conclusion",
                "start_backtest",
            )
        scenario_id = confirmed.scenario_id or self._resolve_scenario_id(confirmed)
        try:
            # The frozen binding is re-verified here, before the engine boundary,
            # and a missing or changed exact version fails the task instead of
            # falling back to another version.
            binding = self._frozen_binding(task, confirmed)
        except SelectionRejectedError as error:
            return self._failure(task_id, generation, error.code, error.message, "start_backtest")
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
        if binding is not None:
            selected = binding.identity("strategy")
            summary = cast(dict[str, Any], validated.get("summary") or {})
            if selected and (
                summary.get("strategy_id") != selected.get("strategy_id")
                or summary.get("strategy_version") != selected.get("strategy_version")
            ):
                return self._failure(
                    task_id,
                    generation,
                    "selection_conflict",
                    "the registered scenario does not run the selected strategy version",
                    "start_backtest",
                )
            # A declared preset is enforced against the normalized parameters and
            # the host's own input evidence, before any backtest is reserved.
            reason = declared_strategy_rejection(
                selected, cast(dict[str, Any], summary.get("parameters") or {})
            )
            if reason is None:
                reason = declared_validator_rejection(
                    binding.identity("validator"), self._dataset_entry(confirmed.dataset_id)
                )
            if reason is not None:
                return self._failure(
                    task_id, generation, "selection_conflict", reason, "start_backtest"
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
                    plugin_binding=None if binding is None else binding.document(),
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

    # -- the conclusion axis -------------------------------------------------
    #
    # Interpretation is a separate step with its own lifecycle. It reads one
    # verified report, spends at most one model call, publishes a host-rendered
    # conclusion, and never rewrites the job's own succeeded computation.

    def _verified_report(self, task_id: str) -> tuple[Any, dict[str, Any], dict[str, Any]]:
        """The one verified report this task may conclude about."""
        task = self._store.get(task_id)
        if len(task.jobs) != 1:
            raise TaskStateError("exactly one bound backtest is required to interpret")
        record = self._service.get_job(task.jobs[0])
        if record.status != "succeeded":
            raise TaskStateError("the bound backtest has not succeeded")
        # The service re-verifies payload hash, report schema and run id on every
        # read, so nothing here is read from an unverified file.
        payload = self._service.report(record.job_id)
        document = json.loads(payload)
        if type(document) is not dict:
            raise TaskStateError("the verified report is not one document")
        report = cast(dict[str, Any], document)
        return record, report, report_claims.identity_of(report, record)

    @staticmethod
    def _interpretation_tools() -> list[ToolDefinition]:
        return [ToolDefinition(**report_claims.interpretation_tool())]

    @staticmethod
    def _interpretation_prompt(packet: dict[str, Any]) -> str:
        return (
            "You record one bounded conclusion about a single completed backtest. "
            "You cannot change, recompute or extend the run, and you cannot start "
            "another one. Call conclude exactly once with codes only: you never write "
            "a number, a unit or a sentence, because the host reads every value from "
            "the verified report and writes every sentence it shows the user. "
            "The question below is the user's own input, not a verified finding. "
            "The host has already read the frozen criterion deterministically and its "
            "own reading is in the packet: choose the one inference code that restates "
            "it, and point evidence_ref at the frozen criterion field. You cannot "
            "narrow that reading: a satisfied criterion stays satisfied, and counts of "
            "orders or fills are never on their own a reason to call the evidence "
            "insufficient. The conclusion is about this single frozen run: the "
            "confirmed experiment fixed one scope, nothing in the accepted input "
            "declares a wider one, and a conclusion claiming more than this run is "
            "refused rather than published. Never claim the run proves the idea, and "
            "never state or imply a profit.\n\n"
            "Verified packet (JSON, read-only projection):\n"
            + json.dumps(packet, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )

    def _interpretation_versions(self, task: ResearchTask) -> dict[str, Any]:
        confirmed = task.spec_confirmed[-1][2] if task.spec_confirmed else None
        return {
            "operation": _INTERPRETATION_OPERATION,
            "prompt": _INTERPRETATION_PROMPT_VERSION,
            "tools": _INTERPRETATION_TOOL_VERSION,
            "renderer": report_claims.INTERPRETATION_SCHEMA,
            "criterion_schema": (
                report_claims.CRITERION_SCHEMA
                if confirmed is not None and confirmed.criterion is not None
                else None
            ),
            "spec_version": len(task.spec_confirmed),
            "strategy_id": None if confirmed is None else confirmed.strategy_id,
            "dataset_id": None if confirmed is None else confirmed.dataset_id,
            "source_sha256": None if confirmed is None else confirmed.source_sha256,
            "pinned_binding": task.pinned_binding,
        }

    @staticmethod
    def _observation(response: Any) -> dict[str, Any]:
        """The durable identity of one observed model call.

        It is written in the same transition that settles the call's charge and
        records its response, so no durable image can carry a spent slot whose
        outcome is unrecorded.
        """
        return {
            "model": str(response.model or "unknown"),
            "at": _now(),
            "outcome": "observed",
            "usage_available": response.usage_available,
            "stop_reason": response.stop_reason,
        }

    @staticmethod
    def _recorded_call(call: Any) -> dict[str, Any]:
        """One tool call exactly as the response carried it, bounded, never rewritten.

        The model's own identity for the call is kept beside its arguments: the id
        it sent, the tool name, and the argument object as compact JSON. When the
        arguments are larger than the bound the payload is withheld and its byte
        length and digest are kept instead: a truncated argument set can re-parse
        as a different, apparently valid conclusion, which is precisely what must
        not happen. Nothing is dropped silently either way, and nothing is
        re-asked.
        """
        try:
            payload: str | None = json.dumps(
                call.arguments, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError):
            payload = None
        name = str(call.name or "")
        identity = str(call.id or "")
        if payload is None:
            return {
                "id": identity,
                "tool": name,
                "arguments": None,
                "arguments_bytes": None,
                "arguments_sha256": None,
                "over_limit": True,
            }
        encoded = payload.encode("utf-8")
        over_limit = len(encoded) > _MAX_EVIDENCE_ARGUMENT_BYTES
        return {
            "id": identity,
            "tool": name,
            "arguments": None if over_limit else payload,
            "arguments_bytes": len(encoded),
            "arguments_sha256": hashlib.sha256(encoded).hexdigest(),
            "over_limit": over_limit,
        }

    @staticmethod
    def _bounded_calls(
        tool_calls: Any,
    ) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
        """The complete call set, or one fixed-size record of why it was refused.

        Both bounds are measured over the whole set the response carried: the
        number of calls, and the UTF-8 bytes of every id, tool name and argument
        payload together. No prefix of a larger response is ever stored as if it
        were the whole answer, because a shortened set can re-parse as a
        different, apparently valid conclusion. A refusal keeps the real count,
        the real size and the digest of what was sent, and nothing else.
        """
        calls = list(tool_calls or [])
        try:
            encoded = json.dumps(
                [
                    {
                        "id": str(call.id or ""),
                        "name": str(call.name or ""),
                        "arguments": call.arguments,
                    }
                    for call in calls
                ],
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            return None, ResearchOrchestrator._calls_rejection(
                "unserializable", len(calls), None, None
            )
        reason = None
        if len(calls) > _MAX_EVIDENCE_CALLS:
            reason = "too_many_calls"
        elif len(encoded) > _MAX_EVIDENCE_CALL_BYTES:
            reason = "too_many_bytes"
        if reason is None:
            return [ResearchOrchestrator._recorded_call(call) for call in calls], None
        return None, ResearchOrchestrator._calls_rejection(
            reason, len(calls), len(encoded), hashlib.sha256(encoded).hexdigest()
        )

    @staticmethod
    def _calls_rejection(
        reason: str, count: int, size: int | None, digest: str | None
    ) -> dict[str, Any]:
        """The fixed-size record of a call set this product will not store."""
        return {
            "reason": reason,
            "call_count": count,
            "calls_bytes": size,
            "calls_sha256": digest,
            "limit_calls": _MAX_EVIDENCE_CALLS,
            "limit_bytes": _MAX_EVIDENCE_CALL_BYTES,
        }

    @staticmethod
    def _evidence_receipt(evidence: dict[str, Any], *, seq: int | None) -> dict[str, Any]:
        """A digest over the stored claim facts, plus the identity they belong to.

        It is computed over the document exactly as persisted and is recomputed
        the same way on read, so a changed or partially written document is
        detectable. It is a persistence check only: the authority over what the
        claim means stays with the verified report identity recorded alongside.
        """
        bound = {
            "origin": evidence.get("origin"),
            "report": evidence.get("report"),
            "criterion": evidence.get("criterion"),
            "data_scope": evidence.get("data_scope"),
            "call_seq": seq,
            "calls": evidence.get("calls"),
            "calls_rejection": evidence.get("calls_rejection"),
            "response_text_sha256": (evidence.get("response") or {}).get("text_sha256"),
        }
        encoded = json.dumps(bound, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return {
            "schema": _INTERPRETATION_EVIDENCE_RECEIPT_SCHEMA,
            "call_seq": seq,
            "evidence_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "at": evidence.get("at"),
        }

    @staticmethod
    def _evidence_document(
        identity: dict[str, Any],
        criterion: dict[str, Any] | None,
        response: Any,
        versions: dict[str, Any],
        model: dict[str, Any],
        *,
        seq: int | None = None,
        confirmed_spec: dict[str, Any] | None = None,
        data_scope: dict[str, Any] | None = None,
        origin: str = "model",
    ) -> dict[str, Any]:
        """Everything observed, persisted before anything is claimed about it.

        This document is the audit record of one model call: the exact verified
        report it was produced against, the frozen criterion, the call identity
        and sequence, the usage it reported, its own accounting, and the complete
        bounded set of tool calls the response carried — or, when that set is past
        its bound, the fixed-size record of the refusal instead. It is written
        *before* any local claim parsing, and the local reader re-reads these same
        stored bytes, so a first publication and a restart after recovery cannot
        disagree about what the model said. Model prose is never stored for
        display; its hash is kept so the exact response stays identifiable.
        """
        text = str(response.text or "")
        calls, calls_rejection = ResearchOrchestrator._bounded_calls(response.tool_calls)
        evidence: dict[str, Any] = {
            "schema": _INTERPRETATION_EVIDENCE_SCHEMA,
            "origin": origin,
            "report": identity,
            "criterion": criterion,
            "confirmed_spec": confirmed_spec,
            "data_scope": data_scope,
            "calls": calls,
            "calls_rejection": calls_rejection,
            "response": {
                "model": str(response.model or "unknown"),
                "stop_reason": response.stop_reason,
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "input_tokens": int(response.input_tokens or 0),
                "output_tokens": int(response.output_tokens or 0),
                "usage_available": bool(response.usage_available),
            },
            "model": model,
            "versions": versions,
            "at": _now(),
        }
        evidence["receipt"] = ResearchOrchestrator._evidence_receipt(evidence, seq=seq)
        evidence["call"] = {"seq": seq, "tool": report_claims.CONCLUDE_TOOL_NAME}
        return evidence

    @staticmethod
    def _host_evidence(
        identity: dict[str, Any],
        versions: dict[str, Any],
        inference: str,
        note: str,
        *,
        data_scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Evidence for a conclusion the host reaches without any model call.

        It carries the same shape as a model response document so the one local
        reader handles both, and it is marked as the host's own so it is never
        mistaken for something a model said.
        """
        evidence: dict[str, Any] = {
            "schema": _INTERPRETATION_EVIDENCE_SCHEMA,
            "origin": "host",
            "report": identity,
            "criterion": None,
            "confirmed_spec": None,
            "data_scope": data_scope,
            "calls": [],
            "calls_rejection": None,
            "selection": {
                "inference": inference,
                "evidence_ref": None,
                "conclusion_scope": report_claims.FROZEN_SCOPE,
            },
            "rejection": note,
            "response": None,
            "model": None,
            "versions": versions,
            "at": _now(),
        }
        evidence["receipt"] = ResearchOrchestrator._evidence_receipt(evidence, seq=None)
        evidence["call"] = None
        return evidence

    @staticmethod
    def _selection_from_evidence(
        evidence: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None, str | None]:
        """The one local reader of a stored claim, shared by both paths.

        It returns ``(selection, code, message)``. The first publication and a
        restart after recovery both come through here, so a claim that was
        accepted once cannot be refused later from the same bytes, and one that
        was refused is never re-asked. Only persisted facts are parsed: the
        model response is never re-derived, repaired or re-requested.
        """
        if evidence.get("origin") == "host":
            selection = evidence.get("selection")
            if type(selection) is not dict:
                return None, "no_conclusion_call", "the host recorded no conclusion"
            return cast(dict[str, Any], selection), None, None
        calls = evidence.get("calls")
        refusal = evidence.get("calls_rejection")
        if type(refusal) is dict:
            # The response carried more call data than the record keeps, so no
            # part of it is stored as a collection. The refusal is technical: it
            # says nothing about the run, and the call that produced it stays
            # charged and is never re-asked.
            return (
                None,
                "claim_rejected",
                "the recorded call set was refused as "
                f"{refusal.get('reason')} ({refusal.get('call_count')} calls, "
                f"{refusal.get('calls_bytes')} bytes, sha256 {refusal.get('calls_sha256')}); "
                "it is retained for audit and is not re-asked",
            )
        if type(calls) is not list:
            return None, "no_conclusion_call", "the model did not record a conclusion"
        concludes = [
            call
            for call in calls
            if type(call) is dict and call.get("tool") == report_claims.CONCLUDE_TOOL_NAME
        ]
        if not concludes:
            return None, "no_conclusion_call", "the model did not record a conclusion"
        if len(concludes) > 1:
            # The contract is exactly one conclusion call. Two of them are not a
            # conclusion, they are a broken claim shape.
            return None, "no_conclusion_call", "the model recorded more than one conclusion"
        call = concludes[0]
        if call.get("over_limit"):
            return (
                None,
                "claim_rejected",
                "the recorded conclusion arguments exceed the "
                f"{_MAX_EVIDENCE_ARGUMENT_BYTES}-byte bound "
                f"({call.get('arguments_bytes')} bytes, sha256 {call.get('arguments_sha256')}); "
                "the call is retained for audit and is not re-asked",
            )
        payload = call.get("arguments")
        if not isinstance(payload, str):
            return None, "claim_rejected", "the recorded conclusion call carries no arguments"
        try:
            selection = report_claims.claim_selection(json.loads(payload))
        except (ValueError, TypeError) as error:
            return None, "claim_rejected", str(error)
        return selection, None, None

    def _interpretation_failure(
        self, task_id: str, generation: int, code: str, message: str
    ) -> ResearchTask:
        """A technical failure of the conclusion step, on its own axis."""
        return self._transition(
            task_id,
            generation,
            "interpretation_failure",
            finish=True,
            error=self._error(code, message, "interpret"),
        )

    def _publish_interpretation(
        self, task_id: str, generation: int, task: ResearchTask
    ) -> ResearchTask:
        """Re-verify the report, then render and publish. Never dispatches."""
        evidence = task.interpretation_evidence
        if evidence is None:
            return self._transition(task_id, generation, "noop", finish=True)
        try:
            _record, report, identity = self._verified_report(task_id)
        except (ResearchTaskError, JobNotFoundError, OSError, ValueError) as error:
            return self._interpretation_failure(
                task_id, generation, "report_unavailable", str(error)
            )
        if identity != evidence.get("report"):
            # The exact verified report this response was produced against is not
            # the one on disk. Nothing is published from a stale conclusion.
            return self._interpretation_failure(
                task_id,
                generation,
                "report_identity_changed",
                "the verified report no longer matches the interpreted one",
            )
        selection, code, message = self._selection_from_evidence(evidence)
        if selection is None:
            return self._interpretation_failure(task_id, generation, str(code), str(message))
        try:
            interpretation = report_claims.build_interpretation(
                question=task.raw_idea,
                criterion=evidence.get("criterion"),
                report=report,
                report_identity=identity,
                inference=str(selection["inference"]),
                evidence_ref=selection.get("evidence_ref"),
                conclusion_scope=selection.get("conclusion_scope"),
                model=evidence.get("model"),
                versions=cast(dict[str, Any], evidence.get("versions") or {}),
                data_scope=cast(dict[str, Any] | None, evidence.get("data_scope")),
            )
        except (report_claims.CriterionRejectedError, KeyError, TypeError) as error:
            return self._interpretation_failure(task_id, generation, "claim_rejected", str(error))
        try:
            return self._transition(
                task_id,
                generation,
                "interpretation",
                finish=True,
                evidence=evidence,
                interpretation=interpretation,
            )
        except TaskStateError:
            # The axis closed while this call was in flight. The response stays
            # evidence and nothing is published or revived.
            return self._transition(task_id, generation, "noop", finish=True)

    def _reverify_interpretation(
        self, task_id: str, generation: int, task: ResearchTask
    ) -> ResearchTask:
        """Re-derive a served conclusion from the report before it is shown.

        A conclusion is only ever a reading of one exact verified report. If that
        report is gone, or is no longer the document the conclusion was derived
        from, the conclusion is withdrawn rather than displayed. Nothing here
        dispatches a model call.
        """
        published = task.interpretation
        if published is None:
            return self._transition(task_id, generation, "noop", finish=True)
        try:
            _record, report, identity = self._verified_report(task_id)
        except (ResearchTaskError, JobNotFoundError, OSError, ValueError) as error:
            return self._transition(
                task_id,
                generation,
                "interpretation_stale",
                finish=True,
                error=self._error("report_unavailable", str(error), "interpret"),
            )
        if identity != published.get("report"):
            return self._transition(
                task_id,
                generation,
                "interpretation_stale",
                finish=True,
                error=self._error(
                    "report_identity_changed",
                    "the verified report no longer matches the interpreted one",
                    "interpret",
                ),
            )
        try:
            rerendered = report_claims.rerender_interpretation(
                published, report=report, report_identity=identity
            )
        except report_claims.InterpretationUnsupportedError as error:
            # The stored conclusion was rendered under semantics this build does
            # not implement. That is a technical unavailability on the conclusion
            # axis, not a withdrawn claim, and it never touches the run.
            return self._transition(
                task_id,
                generation,
                "interpretation_stale",
                finish=True,
                error=self._error("interpretation_schema_unsupported", str(error), "interpret"),
            )
        if rerendered is None:
            return self._transition(
                task_id,
                generation,
                "interpretation_stale",
                finish=True,
                error=self._error(
                    "claim_rejected",
                    "the served conclusion can no longer be derived from this report",
                    "interpret",
                ),
            )
        if rerendered == published:
            return self._transition(task_id, generation, "noop", finish=True)
        return self._transition(
            task_id,
            generation,
            "interpretation_refresh",
            finish=True,
            interpretation=rerendered,
        )

    def _confirmed_projection(self, task: ResearchTask) -> dict[str, Any] | None:
        """The confirmed spec the model is allowed to reason about.

        This is the exact document the user confirmed, so the model sees what
        was frozen before the run rather than a summary of it.
        """
        if not task.spec_confirmed:
            return None
        version, at, spec = task.spec_confirmed[-1]
        return {**spec.document(), "version": version, "confirmed_at": at}

    @staticmethod
    def _job_projection(record: Any) -> dict[str, Any]:
        """The bounded identity of the one job this conclusion is about."""
        return {
            "job_id": record.job_id,
            "status": record.status,
            "schema": record.schema,
            "engine_run_id": record.engine_run_id,
            "input_sha256": record.input_sha256,
        }

    @staticmethod
    def _data_scope(record: Any, report: dict[str, Any] | None = None) -> dict[str, Any]:
        """The bounded data provenance a conclusion may lean on.

        Anything this product cannot prove about the data is named as
        unavailable rather than left out, so a conclusion can never silently
        assume point-in-time correctness or independent verification. The time
        and input policy the run actually recorded is projected alongside it: the
        replay window the report carries, and nothing inferred from it. A window
        is a selection policy, not proof that it covers the time a question is
        about, so its presence never removes the unknown-coverage limitation.
        """
        identity = record.input_identity if isinstance(record.input_identity, dict) else {}
        document = report if isinstance(report, dict) else {}
        replay = report_claims.read_field(document, "source.replay_window")
        window = replay if isinstance(replay, dict) else None
        return {
            "record_count": identity.get("record_count"),
            "dataset_id": identity.get("dataset_id"),
            "source_sha256": identity.get("source_sha256"),
            "scenario_sha256": identity.get("scenario_sha256"),
            "input_sha256": record.input_sha256,
            "time_range": (
                None
                if window is None
                else {
                    "start_inclusive": window.get("start_inclusive"),
                    "end_exclusive": window.get("end_exclusive"),
                    "timezone": window.get("timezone"),
                    "selection_field": window.get("selection_field"),
                }
            ),
            "point_in_time": None,
            "quality_verified": False,
            "unavailable": ["point_in_time", "quality_verified"],
        }

    def _stop_conclusion(self, task_id: str, generation: int, task: ResearchTask) -> ResearchTask:
        """Stop a conclusion attempt closed before anything left this process.

        A reservation that never reached the provider is not spent, so it is
        returned in the same write that stops the attempt.
        """
        reservation = task.reservation
        if reservation is None:
            return self._transition(task_id, generation, "noop", finish=True)
        try:
            return self._transition(
                task_id, generation, "model_pending_release", finish=True, seq=reservation.seq
            )
        except TaskStateError:
            # Whoever closed the axis already settled this reservation.
            return self._transition(task_id, generation, "noop", finish=True)

    def interpret(self, task_id: str) -> ResearchTask:
        """The explicit one-shot conclusion step for a succeeded run."""
        generation = self._begin(task_id, "interpret")
        if generation is None:
            return self._view(task_id)
        task = self._store.get(task_id)
        if task.status == "cancelled":
            return self._transition(task_id, generation, "noop", finish=True)
        if _conclusion_closed(task):
            # Already concluded or already settled as failed/unknown: an explicit
            # repeat never re-dispatches a model call.
            return self._transition(task_id, generation, "noop", finish=True)
        if task.status != "succeeded":
            self._transition(task_id, generation, "noop", finish=True)
            raise TaskStateError("only a run whose computation succeeded can be interpreted")
        if task.interpretation_evidence is not None:
            # An observed response survived a restart: local validate and publish.
            return self._publish_interpretation(task_id, generation, task)
        try:
            record, report, identity = self._verified_report(task_id)
        except (ResearchTaskError, JobNotFoundError, OSError, ValueError) as error:
            return self._interpretation_failure(
                task_id, generation, "report_unavailable", str(error)
            )
        confirmed = task.spec_confirmed[-1][2] if task.spec_confirmed else None
        criterion = None if confirmed is None else confirmed.criterion
        versions = self._interpretation_versions(task)
        data_scope = self._data_scope(record, report)
        if criterion is None:
            # Nothing was frozen before the run, so there is nothing to compare
            # against. The honest answer needs no model call and no budget.
            evidence = self._host_evidence(
                identity,
                versions,
                "criterion_not_stated",
                "no criterion was frozen",
                data_scope=data_scope,
            )
            task = self._transition(
                task_id, generation, "interpretation_evidence", evidence=evidence
            )
            return self._publish_interpretation(task_id, generation, task)
        packet = report_claims.packet(
            question=task.raw_idea,
            criterion=criterion,
            report=report,
            report_identity=identity,
            confirmed_spec=self._confirmed_projection(task),
            job=self._job_projection(record),
            data_scope=data_scope,
        )
        try:
            task = self._transition(task_id, generation, "model_reserve", step="interpret")
        except TaskStateError:
            # The axis closed between the reads above and this write.
            return self._transition(task_id, generation, "noop", finish=True)
        except BudgetExceededError as error:
            return self._interpretation_failure(task_id, generation, "budget_exceeded", str(error))
        if _conclusion_closed(task):
            # Cancelled while the slot was reserved and before anything left
            # this process: the slot is returned and no call is made.
            return self._stop_conclusion(task_id, generation, task)
        assert task.reservation is not None
        seq = task.reservation.seq
        try:
            task = self._transition(task_id, generation, "model_dispatch")
        except TaskStateError:
            return self._stop_conclusion(task_id, generation, task)
        if _conclusion_closed(task):
            return self._stop_conclusion(task_id, generation, task)
        route = self._model_route()
        try:
            response = self._provider.complete(
                system=self._interpretation_prompt(packet),
                messages=[
                    ModelMessage(
                        "user",
                        json.dumps(
                            packet, ensure_ascii=True, sort_keys=True, separators=(",", ":")
                        ),
                    )
                ],
                tools=self._interpretation_tools(),
            )
        except ModelNotConfigured as error:
            # No error payload here: the run's own succeeded computation is not
            # rewritten by a failure of the conclusion step.
            self._transition(task_id, generation, "model_unconfigured", seq=seq)
            return self._interpretation_failure(
                task_id, generation, "model_not_configured", str(error)
            )
        except ModelProviderError as error:
            self._transition(
                task_id,
                generation,
                "model_unknown",
                seq=seq,
                model={"model": "unknown", "at": _now(), "outcome": "unknown_after_dispatch"},
            )
            return self._interpretation_failure(
                task_id,
                generation,
                _UNKNOWN_MODEL,
                f"{error}; outcome unknown; the call stays charged and is not "
                "re-dispatched automatically",
            )
        # One durable write: the charge, the observed outcome and the exact
        # response it produced. There is no image in which a slot is spent and
        # its evidence is gone, and nothing is published before this lands.
        # A task cancelled while this call was in flight keeps the evidence and
        # settles its own reservation, but never publishes.
        task = self._transition(
            task_id,
            generation,
            "model_concluded",
            seq=seq,
            response=response,
            evidence=self._evidence_document(
                identity,
                criterion,
                response,
                versions,
                route,
                seq=seq,
                confirmed_spec=self._confirmed_projection(task),
                data_scope=data_scope,
            ),
            model=self._observation(response),
        )
        if task.status == "cancelled":
            return self._transition(task_id, generation, "noop", finish=True)
        return self._publish_interpretation(task_id, generation, task)

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
        if reservation is not None and task.status == "succeeded":
            # A leftover charge on an already-succeeded task can only belong to the
            # conclusion step. It is settled on that axis and never rewrites the
            # run's own succeeded computation.
            if reservation.status == "pending":
                self._transition(task_id, generation, "model_pending_release", seq=reservation.seq)
                return self._interpretation_failure(
                    task_id,
                    generation,
                    _INTERRUPTED,
                    "interrupted before dispatch; the model-call charge was released once",
                )
            self._transition(
                task_id,
                generation,
                "model_unknown",
                seq=reservation.seq,
                model={"model": "unknown", "at": _now(), "outcome": "unknown_after_dispatch"},
            )
            return self._interpretation_failure(
                task_id,
                generation,
                _UNKNOWN_MODEL,
                "model outcome unknown after dispatch; the call stays charged and is not "
                "re-dispatched automatically",
            )
        if task.status == "succeeded" and task.interpretation is not None:
            # Every read re-derives a served conclusion from the verified report.
            # One that can no longer be proven from it is withdrawn, not shown.
            return self._reverify_interpretation(task_id, generation, task)
        if (
            task.status == "succeeded"
            and task.interpretation is None
            and task.interpretation_evidence is not None
        ):
            # An observed response survived a restart: local validate and publish.
            return self._publish_interpretation(task_id, generation, task)
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
        try:
            criterion = report_claims.validate_criterion(input_.get("criterion"))
        except report_claims.CriterionRejectedError as error:
            raise InvalidSpecError(str(error)) from None
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
            criterion=criterion,
        )

    @staticmethod
    def _edited_criterion(value: Any) -> dict[str, Any] | None:
        try:
            return report_claims.validate_criterion(value)
        except report_claims.CriterionRejectedError as error:
            raise InvalidSpecError(str(error)) from None

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
            selection=(
                draft.selection
                if "selection" not in fields
                else self._selection_payload(fields.get("selection"))
            ),
            criterion=(
                draft.criterion
                if "criterion" not in fields
                else self._edited_criterion(fields.get("criterion"))
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
            # Confirmation freezes the exact capability identity. A later
            # catalog, settings or plugin change cannot alter this binding.
            binding = None if spec.selection is None else self._resolve_binding(spec.selection)
        except ResearchTaskError:
            self._transition(task_id, generation, "noop", finish=True)
            raise
        task = self._transition(
            task_id,
            generation,
            "confirm",
            finish=True,
            spec=spec,
            binding=None if binding is None else binding.document(),
        )
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
