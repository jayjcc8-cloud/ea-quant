"""One durable user strategy: a saved draft, approved AI calls, typed diffs, frozen runs.

This module is a thin owner, not an engine. It reuses the registered scenario catalog
through :class:`WebService`, the frozen child experiment and its dispatch path through
:class:`ResearchOrchestrator`, and the workspace's model port through
:class:`ea.web.llm`. It adds no scheduler, no database, no new financial authority:

* the engine strategy parameters and ``initial_cash`` are the only fields an AI diff may
  name, and the complete resulting draft is validated by the real backend before it is
  ever written as a new revision;
* a model call exists only after an explicit approval, is reserved durably with its safe
  route before the provider is reached, and a dispatched-but-unobserved call is never
  re-sent or refunded;
* the preview shows the admitted input itself — its source identity, its normalized cost,
  risk and strategy binding — and a confirmation carries that preview's identity back, so
  the same launch intent always resolves to the experiment it confirmed and a changed
  input is refused instead of silently run.

Everything durable lives in one JSON record per strategy under
``workspace/user-strategies``: messages, calls, proposals, change history and experiment
references, written with the same atomic-publish discipline the research store uses.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from ea.web.llm import (
    ModelMessage,
    ModelNotConfigured,
    ModelPort,
    ModelProviderError,
    ModelResponse,
    ModelRoute,
    ToolDefinition,
    create_model_port,
)
from ea.web.report_claims import (
    FIELD_SPECS_BY_PATH,
    CriterionRejectedError,
    host_limitations,
    host_reading,
    read_field,
    validate_criterion,
)
from ea.web.research import (
    ResearchOrchestrator,
    ResearchSpec,
    ResearchTask,
    ResearchTaskError,
)
from ea.web.service import (
    JobNotFoundError,
    WebService,
    input_snapshot_sha256,
    read_input_snapshot,
)
from ea.web.settings import RadianSettings, RadianSettingsError

SCHEMA = "ea.user-strategy.v1"

#: The workspace id and the bounded text shapes this owner writes.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DECIMAL = re.compile(r"^-?\d{1,20}(\.\d{1,20})?$")
_PATH = re.compile(r"^(?:parameters\.[A-Za-z_][A-Za-z0-9_]{0,63}|initial_cash)$")
#: The identity of one previewed experiment: the admitted input's own digest.
_PREVIEW_IDENTITY = re.compile(r"^sha256:[0-9a-f]{64}$")

_MAX_NAME = 120
_MAX_INTENT = 2000
_MAX_MESSAGE = 4000
_MAX_RESPONSE = 8000
_MAX_SCENARIO_ID = 200
_MAX_REASON = 400
_MAX_EVIDENCE_ITEM = 400
_MAX_EVIDENCE = 8
#: How many frozen runs one turn may be grounded in: A, and optionally B. A pair is
#: the widest reading this product makes, and a third run would stop being a pair.
_MAX_EVIDENCE_RUNS = 2
_MAX_ITEMS = 20
_MAX_MESSAGES = 200
_MAX_CALLS = 200
_MAX_PROPOSALS = 100
_MAX_CHANGES = 200
_MAX_EXPERIMENTS = 50
_MAX_HISTORY = 20
_MAX_TOKENS = 1200
#: One approval is exactly one call; a hidden second call is never spent.
_CALL_APPROVAL = 1
_DEFAULT_BUDGET = {"max_model_calls": 10, "max_backtests": 5}
#: The host-verified report readings a conversation may state as fact. These are
#: read from this strategy's own frozen run through the verified-report authority;
#: nothing else about a run is ever stated.
_REPORT_FACTS = (
    "economics.net_pnl.amount",
    "economics.fees.amount",
    "economics.total_return.value",
    "economics.counts.fills",
    "economics.counts.orders",
)

_SYSTEM_PROMPT = (
    "You are the research assistant inside one local backtest workspace. Answer the user's "
    "question in plain text when a question is what they asked; an ordinary explanation or "
    "clarification needs no tool call. You may propose a change to the strategy draft only "
    "with the propose_change tool, one entry per field, and only for the registered paths "
    "listed in this conversation, using the exact JSON type, current value and registered "
    "bounds stated there for each one: a decimal field is a canonical decimal JSON string, "
    "and an integer field is a JSON integer, never a string. Propose a change only when the "
    "user explicitly asks for one in this message; never ask for a separate confirmation of "
    "your own. You never propose changes to strategy identity, instruments, permissions, "
    "model routing, budgets, data or frozen history. You cannot run anything, and nothing "
    "you say is a verified result: say plainly that a run is needed."
)
#: What a conversation is told when the strategy has no scenario yet. There is no
#: draft to describe and nothing to change, so this turn clarifies the intent only.
_INTENT_SYSTEM_PROMPT = (
    "You are the research assistant inside one local backtest workspace. This user "
    "strategy is an idea that has no scenario bound to it yet, so nothing about it is "
    "executable: there is no strategy, instrument, funding, parameter, dataset or "
    "registered scenario to describe, and nothing can be run. Answer in plain text and "
    "help the user say what they mean, which market and data the idea needs, which "
    "assumptions matter and what is still missing before it could be tested. You have no "
    "tool here and cannot propose, apply or run anything; never suggest that a change was "
    "made. Say plainly that the user must choose a scenario before anything can be "
    "prepared or run."
)
#: The one discriminator a stored draft carries when it is not executable.
_INTENT_KIND = "intent"
#: The one tool a conversation may use. Its schema is built per call from the
#: registered contract, so each path is offered with its real value type.
_PROPOSE_TOOL_NAME = "propose_change"
#: The registered kinds this owner can edit, and the JSON type each one's value has
#: in the tool schema. A kind outside this map is never a changeable path.
_EDITABLE_KINDS = {"integer": "integer", "decimal": "string"}
#: How one editable kind is described in words, next to its JSON type.
_KIND_LABELS = {"integer": "JSON integer", "decimal": "decimal held as a canonical JSON string"}


@dataclass(frozen=True, slots=True)
class RegisteredPath:
    """One path a conversation is described by, in the registered contract's terms.

    ``kind`` is the registered parameter type exactly as the scenario states it, or
    the plain words this owner uses for the initial cash. ``editable`` is whether a
    typed diff may name this path at all: a kind :meth:`_typed` cannot type is
    carried here as a fact about the draft, never as a changeable path.
    """

    path: str
    kind: str
    current: Any
    minimum: Any
    maximum: Any
    editable: bool

    @property
    def json_type(self) -> str:
        return _EDITABLE_KINDS.get(self.kind, "string")

    def described(self) -> str:
        if not self.editable:
            return (
                f"{self.path} is registered as {self.kind} and is read-only here: this "
                "workspace cannot propose a change to it"
            )
        if self.path == "initial_cash":
            return f"{self.path} is a registered positive decimal held as a canonical JSON string"
        return f"{self.path} is a registered {_KIND_LABELS.get(self.kind, self.kind)}"

    def sentence(self) -> str:
        """The one description of this path the conversation and the tool share."""
        parts = [self.described(), f"current value {json.dumps(self.current, default=str)}"]
        parts.append(
            "registered minimum " + json.dumps(self.minimum, default=str)
            if self.minimum is not None
            else "no registered minimum"
        )
        parts.append(
            "registered maximum " + json.dumps(self.maximum, default=str)
            if self.maximum is not None
            else "no registered maximum"
        )
        return "; ".join(parts) + "."


class UserStrategyError(ValueError):
    """A user-strategy request is invalid or conflicts with its durable state."""


class UserStrategyNotFoundError(UserStrategyError):
    """No user strategy with this identity exists."""


class UserStrategyStateError(UserStrategyError):
    """The action is not valid for the strategy's current state or content."""


class UserStrategyStaleError(UserStrategyError):
    """The request was made against a revision that is no longer the head."""


class UserStrategyConflictError(UserStrategyError):
    """One identity was reused with a different payload."""


class UserStrategyBudgetError(UserStrategyError):
    """No explicit approval, or an approval this workspace cannot grant."""


def _now() -> str:
    from ea.web.research import _now as research_now

    return research_now()


def _identifier(value: object, field: str) -> str:
    """A client-supplied identity, bounded before it can name anything on disk."""
    text = value if isinstance(value, str) else ""
    if _SAFE_ID.fullmatch(text) is None:
        raise UserStrategyStateError(
            f"{field} must be 1-64 characters of letters, digits, '-' or '_'"
        )
    return text


def _bounded(value: object, field: str, maximum: int, *, required: bool = True) -> str:
    text = value if isinstance(value, str) else ""
    text = text.strip()
    if not text:
        if required:
            raise UserStrategyStateError(f"{field} is required")
        return ""
    if len(text.encode()) > maximum:
        raise UserStrategyStateError(f"{field} is longer than {maximum} bytes")
    return text


def _digest(payload: object) -> str:
    import hashlib

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _entry(items: tuple[dict[str, Any], ...], key: str, value: str) -> dict[str, Any] | None:
    for item in items:
        if item.get(key) == value:
            return item
    return None


def _requested_budget(approved_budget: Any) -> dict[str, int]:
    """The approval exactly as one request states it, without any workspace read.

    A repeated request is compared with the request that was persisted, not with
    today's workspace settings, so this only proves the stated budget is a
    readable, non-negative pair of counts.
    """
    if type(approved_budget) is not dict:
        raise UserStrategyBudgetError(
            "an explicit approval is required before any model call or experiment"
        )
    try:
        granted = {
            "max_model_calls": int(approved_budget.get("max_model_calls", 0)),
            "max_backtests": int(approved_budget.get("max_backtests", 0)),
        }
    except (TypeError, ValueError):
        raise UserStrategyBudgetError("the approved budget is not readable") from None
    if granted["max_model_calls"] < 0 or granted["max_backtests"] < 0:
        raise UserStrategyBudgetError("the approved budget is not readable")
    return granted


@dataclass(frozen=True, slots=True)
class StrategyDraft:
    """One validated draft revision: exactly what an experiment would execute."""

    revision: int
    strategy_id: str
    venue: str
    symbol: str
    scenario_id: str
    dataset_id: str | None
    source_sha256: str | None
    initial_cash: str
    parameters: dict[str, Any]
    criterion: dict[str, Any] | None = None

    def document(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "strategy_id": self.strategy_id,
            "venue": self.venue,
            "symbol": self.symbol,
            "scenario_id": self.scenario_id,
            "dataset_id": self.dataset_id,
            "source_sha256": self.source_sha256,
            "initial_cash": self.initial_cash,
            "parameters": dict(self.parameters),
            "criterion": self.criterion,
        }

    @staticmethod
    def decode(document: Any) -> StrategyDraft:
        if type(document) is not dict:
            raise UserStrategyError("stored draft is unreadable")
        criterion = document.get("criterion")
        return StrategyDraft(
            revision=int(document["revision"]),
            strategy_id=str(document["strategy_id"]),
            venue=str(document["venue"]),
            symbol=str(document["symbol"]),
            scenario_id=str(document["scenario_id"]),
            dataset_id=document.get("dataset_id"),
            source_sha256=document.get("source_sha256"),
            initial_cash=str(document["initial_cash"]),
            parameters=dict(document.get("parameters") or {}),
            criterion=None if criterion is None else validate_criterion(criterion),
        )


@dataclass(frozen=True, slots=True)
class IntentDraft:
    """One revision of a saved intent: what a strategy is before it has a scenario.

    It carries a revision and nothing executable. There is deliberately no
    strategy, venue, symbol, funding, parameter or scenario field to fill in
    with a guess, and no default scenario: an intent is never previewed, planned
    against or run. It is not a second kind of record either — it is revision
    ``n`` of the same durable strategy, which one explicit binding replaces with
    the executable draft of the scenario the user chose.
    """

    revision: int

    @property
    def scenario_id(self) -> None:
        """An intent has no scenario, and that is stated rather than left absent."""
        return None

    def document(self) -> dict[str, Any]:
        return {"kind": _INTENT_KIND, "revision": self.revision, "scenario_id": None}

    @staticmethod
    def decode(document: Any) -> IntentDraft:
        if type(document) is not dict or document.get("kind") != _INTENT_KIND:
            raise UserStrategyError("stored draft is unreadable")
        return IntentDraft(revision=int(document["revision"]))


#: One revision of one strategy: an executable draft, or a saved intent.
Draft = IntentDraft | StrategyDraft


def _decode_draft(document: Any) -> Draft:
    """One stored draft, discriminated by its own kind.

    A record written before this union existed carries no discriminator at all,
    and its draft is the executable one it always was: absence is the legacy
    encoding, never a missing intent.
    """
    if type(document) is dict and document.get("kind") == _INTENT_KIND:
        return IntentDraft.decode(document)
    return StrategyDraft.decode(document)


@dataclass(frozen=True, slots=True)
class UserStrategy:
    """One durable user strategy; every mutation goes through the store."""

    user_strategy_id: str
    created_at: str
    updated_at: str
    name: str
    intent: str
    draft: Draft
    history: tuple[Draft, ...] = ()
    messages: tuple[dict[str, Any], ...] = ()
    calls: tuple[dict[str, Any], ...] = ()
    proposals: tuple[dict[str, Any], ...] = ()
    changes: tuple[dict[str, Any], ...] = ()
    experiments: tuple[dict[str, Any], ...] = ()
    #: The one immutable binding this strategy was formed by, if it has one. It is
    #: kept here rather than derived from ``changes`` because that ledger is bounded
    #: and a later operation would push the original request out, which would make a
    #: replay of the binding unresolvable. It is a record of one request, never a
    #: second store: nothing else is ever read from it.
    binding: dict[str, Any] | None = None

    def document(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "user_strategy_id": self.user_strategy_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "name": self.name,
            "intent": self.intent,
            "draft": self.draft.document(),
            "history": [item.document() for item in self.history],
            "messages": [dict(item) for item in self.messages],
            "calls": [dict(item) for item in self.calls],
            "proposals": [dict(item) for item in self.proposals],
            "changes": [dict(item) for item in self.changes],
            "experiments": [dict(item) for item in self.experiments],
            "binding": None if self.binding is None else dict(self.binding),
        }

    @staticmethod
    def decode(document: Any) -> UserStrategy:
        if type(document) is not dict or document.get("schema") != SCHEMA:
            raise UserStrategyError("user strategy schema is not supported")
        binding = document.get("binding")
        return UserStrategy(
            user_strategy_id=str(document["user_strategy_id"]),
            created_at=str(document["created_at"]),
            updated_at=str(document["updated_at"]),
            name=str(document["name"]),
            intent=str(document["intent"]),
            draft=_decode_draft(document["draft"]),
            history=tuple(_decode_draft(item) for item in document.get("history") or []),
            messages=tuple(dict(item) for item in document.get("messages") or []),
            calls=tuple(dict(item) for item in document.get("calls") or []),
            proposals=tuple(dict(item) for item in document.get("proposals") or []),
            changes=tuple(dict(item) for item in document.get("changes") or []),
            experiments=tuple(dict(item) for item in document.get("experiments") or []),
            binding=None if binding is None else dict(binding),
        )


def _executable(strategy: UserStrategy) -> StrategyDraft:
    """This strategy's executable draft, or an honest refusal before any effect.

    Every action that would change, preview or run a draft reads it through here,
    so an intent refuses each of them itself rather than relying on a hidden
    button, and never reaches a reservation, a child or a job.
    """
    draft = strategy.draft
    if isinstance(draft, IntentDraft):
        raise UserStrategyStateError(
            "no scenario is bound to this strategy yet, so there is no executable draft "
            "to change, preview or run; choose one explicitly first"
        )
    return draft


#: The store and the service both define a ``list`` method, so a bare ``list[...]``
#: annotation inside their bodies would name that method instead of the builtin.
Strategies = list[UserStrategy]
Documents = list[dict[str, Any]]
Paths = list[str]
History = list[ModelMessage]


class UserStrategyStore:
    """One JSON record per user strategy, published atomically."""

    def __init__(self, workspace: Path):
        self._dir = Path(workspace) / "user-strategies"

    def _path(self, user_strategy_id: str) -> Path:
        return self._dir / f"{user_strategy_id}.json"

    def save(self, strategy: UserStrategy) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(strategy.document(), sort_keys=True, indent=2).encode()
        handle, name = tempfile.mkstemp(dir=self._dir, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self._path(strategy.user_strategy_id))
        except BaseException:
            with_context = Path(name)
            with_context.unlink(missing_ok=True)
            raise
        directory = os.open(self._dir, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def get(self, user_strategy_id: str) -> UserStrategy:
        try:
            payload = self._path(user_strategy_id).read_bytes()
        except OSError:
            raise UserStrategyNotFoundError("user strategy was not found") from None
        try:
            return UserStrategy.decode(json.loads(payload))
        except (ValueError, TypeError, KeyError) as error:
            if isinstance(error, UserStrategyError):
                raise
            raise UserStrategyError("stored user strategy is unreadable") from None

    def list(self) -> Strategies:
        """Every published strategy, and no unpublished crash leftover.

        The store publishes one bounded identity name per record, so a temporary
        file a crashed save left behind is not a strategy: it is never read, and a
        record whose own identity disagrees with its file name is not shown as one.
        """
        if not self._dir.is_dir():
            return []
        items: Strategies = []
        for path in sorted(self._dir.glob("*.json")):
            if _SAFE_ID.fullmatch(path.stem) is None:
                continue
            try:
                item = UserStrategy.decode(json.loads(path.read_bytes()))
            except (OSError, ValueError, TypeError, KeyError):
                # One unreadable record never hides the rest of the workspace.
                continue
            if item.user_strategy_id != path.stem:
                continue
            items.append(item)
        return items


class UserStrategyService:
    """The operations a user strategy owns: calls, diffs, revisions and experiments."""

    def __init__(
        self,
        store: UserStrategyStore,
        research: ResearchOrchestrator,
        service: WebService,
        settings: RadianSettings,
        *,
        budget: dict[str, int] | None = None,
    ):
        self._store = store
        self._research = research
        self._service = service
        self._settings = settings
        self._budget = dict(budget) if budget is not None else None
        self._locks: dict[str, threading.Lock] = {}
        self._provider: ModelPort | None = None
        #: Calls this process is inside right now. A durable call that is in
        #: flight here is not an orphan, so recovery never reclassifies it.
        self._active_calls: set[tuple[str, str]] = set()

    # -- workspace facts ----------------------------------------------------

    def _workspace_budget(self) -> dict[str, int]:
        if self._budget is not None:
            return dict(self._budget)
        try:
            status = self._settings.status()
        except (RadianSettingsError, ValueError):
            status = {}
        granted = status.get("research_budget") or _DEFAULT_BUDGET
        return {
            "max_model_calls": int(granted.get("max_model_calls", 10)),
            "max_backtests": int(granted.get("max_backtests", 5)),
        }

    def _approval(
        self, approved_budget: Any, *, model_calls: int, backtests: int
    ) -> dict[str, int]:
        granted = _requested_budget(approved_budget)
        limit = self._workspace_budget()
        if (
            granted["max_model_calls"] < model_calls
            or granted["max_backtests"] < backtests
            or granted["max_model_calls"] > limit["max_model_calls"]
            or granted["max_backtests"] > limit["max_backtests"]
        ):
            raise UserStrategyBudgetError(
                "the approved budget is outside what this workspace grants "
                f"({limit['max_model_calls']} model calls, {limit['max_backtests']} backtests)"
            )
        return granted

    def _model_port(self) -> ModelPort:
        if self._provider is None:
            self._provider = create_model_port(self._settings)
        return self._provider

    def _safe_route(self, port: ModelPort | None = None) -> dict[str, Any] | None:
        """The exact non-secret route, or ``None`` when no provider is configured.

        A real port answers ``route()``; an injected double that cannot describe a route
        falls back to the status projection, which is what the settings page already
        shows. Never a credential, a key reference or a resolution value.
        """
        described: ModelRoute | None = None
        try:
            described = (port if port is not None else self._model_port()).route()
        except ModelNotConfigured:
            described = None
        except Exception:  # noqa: BLE001 — an unusable route is simply not configured
            described = None
        if described is not None:
            return {
                "provider": described.provider,
                "model": described.model,
                "endpoint": described.endpoint,
            }
        if port is not None or self._provider is not None:
            # A port exists but cannot describe itself: read the safe status instead.
            try:
                status = self._settings.status()
            except (RadianSettingsError, ValueError):
                return None
            if not status.get("configured"):
                return None
            return {
                "provider": status.get("provider"),
                "model": status.get("model"),
                "endpoint": status.get("endpoint"),
            }
        return None

    # -- scenario facts -----------------------------------------------------

    def _scenario_document(self, scenario_id: str) -> dict[str, Any]:
        try:
            return cast(dict[str, Any], self._service.validate_scenario(scenario_id))
        except Exception as error:  # noqa: BLE001 — an unusable start is refused, not guessed
            raise UserStrategyStateError(
                f"{scenario_id} is not a currently registered, valid scenario: {error}"
            ) from None

    @staticmethod
    def _contracts(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
        contracts: dict[str, dict[str, Any]] = {}
        for entry in cast(Documents, document.get("strategy_parameters") or []):
            name = entry.get("name")
            if isinstance(name, str):
                contracts[name] = entry
        return contracts

    def _registered_paths(self, draft: StrategyDraft) -> tuple[RegisteredPath, ...]:
        """The registered contract of one draft, resolved once per call.

        Read from the same scenario document a typed diff is validated against, so
        every path is described with the value type and the bounds this workspace
        really enforces rather than a name this owner guessed. The draft's own value
        is the current one; a registered parameter the draft does not carry is
        described by the contract's own value. Nothing here decides anything: the
        whole resulting draft is still validated by the backend, and a path whose
        registered kind :meth:`_typed` cannot edit is carried as read-only.
        """
        document = self._scenario_document(draft.scenario_id)
        paths = [
            RegisteredPath(
                path="initial_cash",
                kind="positive decimal",
                current=draft.initial_cash,
                minimum=None,
                maximum=None,
                editable=True,
            )
        ]
        for name, contract in sorted(self._contracts(document).items()):
            kind = str(contract.get("type") or "")
            current = draft.parameters.get(name)
            if current is None:
                current = contract.get("current_value")
            if current is None:
                current = contract.get("default")
            paths.append(
                RegisteredPath(
                    path=f"parameters.{name}",
                    kind=kind,
                    current=current,
                    minimum=contract.get("minimum"),
                    maximum=contract.get("maximum"),
                    editable=kind in _EDITABLE_KINDS,
                )
            )
        return tuple(paths)

    @staticmethod
    def _propose_tool(paths: tuple[RegisteredPath, ...]) -> ToolDefinition:
        """The one tool, whose every alternative is one changeable registered path.

        Each branch names the path it applies to and the JSON type its value must
        have, and repeats the same sentence the conversation states. This guides the
        model; it is not a guarantee — the runtime whole-draft and quantum
        validation remains the authority on what a draft may become.
        """
        branches = [
            {
                "type": "object",
                "description": entry.sentence(),
                "properties": {
                    "path": {"type": "string", "enum": [entry.path]},
                    "value": {"type": entry.json_type},
                    "reason": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["path", "value", "reason"],
            }
            for entry in paths
            if entry.editable
        ]
        return ToolDefinition(
            name=_PROPOSE_TOOL_NAME,
            description=(
                "Propose a change to one or more registered draft paths. Use it only when "
                "the user explicitly asks for a change; a question needs no call."
            ),
            parameters={
                "type": "object",
                "properties": {"items": {"type": "array", "items": {"oneOf": branches}}},
                "required": ["items"],
            },
        )

    def _validated(
        self, draft: StrategyDraft, parameters: dict[str, Any], initial_cash: str
    ) -> dict[str, Any]:
        """The whole resulting spec, validated by the real backend. Nothing else."""
        try:
            return cast(
                dict[str, Any],
                self._service.validate_scenario(
                    draft.scenario_id,
                    parameters={
                        "initial_cash": initial_cash,
                        "strategy_parameters": dict(parameters),
                        "quantity": None,
                        "strategy_source": None,
                    },
                ),
            )
        except Exception as error:  # noqa: BLE001 — the backend is the authority here
            raise UserStrategyStateError(str(error)) from None

    @staticmethod
    def _parameters(draft: StrategyDraft) -> dict[str, Any]:
        """The parameter block every engine submission for this draft uses."""
        return {
            "initial_cash": draft.initial_cash,
            "strategy_parameters": dict(draft.parameters),
            "quantity": None,
            "strategy_source": None,
        }

    def _admitted_request(self, draft: StrategyDraft) -> dict[str, Any]:
        """The authoritative admitted input of one draft, read from the service.

        This is what the engine would execute: the same dataset binding, the same
        normalized parameters and the same input snapshot a job would freeze. It
        creates nothing and starts nothing, and it is the only source of the
        source identity this owner stores — never the caller's own hash.
        """
        try:
            return cast(
                dict[str, Any],
                self._service.prepare_input_snapshot(
                    draft.scenario_id,
                    parameters=self._parameters(draft),
                    dataset_id=draft.dataset_id,
                    source_sha256=draft.source_sha256,
                ),
            )
        except Exception as error:  # noqa: BLE001 — the backend is the authority here
            raise UserStrategyStateError(str(error)) from None

    @staticmethod
    def _source_identity(admitted: dict[str, Any]) -> dict[str, Any]:
        """Where the admitted input came from, as the service names it.

        ``dataset_id`` and ``source_sha256`` are the raw source identity the engine
        reads back; the selection fingerprint of the data is a different fact and
        stays a different field. A scenario with no research input has neither, and
        that is stored as an absent binding rather than as a fingerprint.
        """
        identity = admitted.get("input_identity")
        if not isinstance(identity, dict):
            return {}
        return {
            "dataset_id": identity.get("dataset_id"),
            "source_sha256": identity.get("source_sha256"),
            "record_count": identity.get("record_count"),
        }

    @staticmethod
    def _normalized(admitted: dict[str, Any]) -> dict[str, Any]:
        """The admitted, normalized blocks of one would-be run, bounded and honest.

        These are the engine's own blocks — strategy, execution (the real cost
        assumptions), risk, funding, instrument and data — read from the input
        snapshot the job would freeze, never from the draft or the scenario text.
        """
        snapshot = admitted.get("snapshot")
        scenario = snapshot.get("scenario") if isinstance(snapshot, dict) else None
        if not isinstance(scenario, dict):
            return {}
        return {
            key: scenario.get(key)
            for key in ("strategy", "execution", "risk", "funding", "instrument", "data")
        }

    @staticmethod
    def _preview_identity(draft: StrategyDraft, admitted: dict[str, Any]) -> str:
        """The identity of exactly what a confirmation would run.

        A draft revision alone is not enough: the same revision can admit a
        different input after the source, the cost assumptions or the registered
        scenario change. This names the draft and the admitted input, so a
        confirmation made on a stale preview is refused instead of silently
        running something else. It is not a credential and it is not a budget:
        the approval is compared separately, against the persisted request.
        """
        return "sha256:" + _digest(
            {
                "draft_revision": draft.revision,
                "scenario_id": draft.scenario_id,
                "strategy_id": draft.strategy_id,
                "venue": draft.venue,
                "symbol": draft.symbol,
                "initial_cash": draft.initial_cash,
                "parameters": dict(draft.parameters),
                "criterion": draft.criterion,
                "input_identity": admitted.get("input_identity"),
                "input_sha256": admitted.get("input_sha256"),
            }
        )

    # -- creation and reads -------------------------------------------------

    def _scenario_draft(
        self, scenario: str, criterion: dict[str, Any] | None, revision: int
    ) -> StrategyDraft:
        """One executable draft of one registered scenario, validated by the backend.

        This is the only way an executable draft is ever formed — a bound creation
        and a later binding both come through here — so what a binding produces is
        the same draft this owner has always written and not a variant of it.
        """
        document = self._scenario_document(scenario)
        summary = cast(dict[str, Any], document.get("summary") or {})
        parameters = {
            item["name"]: item["current_value"]
            if item.get("current_value") is not None
            else item.get("default")
            for item in self._contracts(document).values()
        }
        identity = self._source_identity(document)
        draft = StrategyDraft(
            revision=revision,
            strategy_id=str(summary.get("strategy_id") or ""),
            venue=str(summary.get("venue") or ""),
            symbol=str(summary.get("symbol") or ""),
            scenario_id=scenario,
            # The workspace's own source identity, exactly as the engine reads it
            # back. The selection fingerprint of the data is a distinct preview
            # fact and is never stored as a source binding.
            dataset_id=cast(str | None, identity.get("dataset_id")),
            source_sha256=cast(str | None, identity.get("source_sha256")),
            initial_cash=str(summary.get("initial_cash") or ""),
            parameters=parameters,
            criterion=criterion,
        )
        if not draft.strategy_id or not draft.initial_cash:
            raise UserStrategyStateError("the registered scenario has no executable strategy")
        return draft

    def create(
        self,
        *,
        name: object,
        intent: object,
        scenario_id: object = None,
        criterion: Any = None,
    ) -> UserStrategy:
        """One saved strategy: a saved intent, or an executable draft of a scenario.

        Without a scenario this is an intent: it needs no catalog to exist, it
        resolves nothing, and it writes no executable field, because there is
        nothing yet to execute. With one, it is exactly the executable revision 1
        this owner has always written, validated by the same backend before
        anything is saved.
        """
        title = _bounded(name, "name", _MAX_NAME)
        purpose = _bounded(intent, "intent", _MAX_INTENT)
        now = _now()
        draft: Draft
        if scenario_id is None:
            if criterion is not None:
                raise UserStrategyStateError(
                    "a criterion belongs to an executable draft; choose a scenario first"
                )
            draft = IntentDraft(revision=1)
        else:
            scenario = _bounded(scenario_id, "scenario_id", _MAX_SCENARIO_ID)
            try:
                frozen_criterion = validate_criterion(criterion)
            except CriterionRejectedError as error:
                raise UserStrategyStateError(str(error)) from None
            draft = self._scenario_draft(scenario, frozen_criterion, revision=1)
        strategy = UserStrategy(
            user_strategy_id=str(uuid.uuid4()),
            created_at=now,
            updated_at=now,
            name=title,
            intent=purpose,
            draft=draft,
        )
        self._store.save(strategy)
        return strategy

    def get(self, user_strategy_id: object) -> UserStrategy:
        return self._store.get(_identifier(user_strategy_id, "user_strategy_id"))

    def list(self) -> Strategies:
        return self._store.list()

    # -- one explicit binding ------------------------------------------------

    def bind_scenario(
        self,
        user_strategy_id: object,
        *,
        operation_id: object,
        base_revision: object,
        scenario_id: object,
        criterion: Any = None,
    ) -> UserStrategy:
        """Bind one explicitly chosen scenario to one saved intent: one revision, once.

        The user chooses the scenario; the host validates it. It must be a currently
        registered, valid one, and the executable draft this forms is built exactly
        the way a bound creation builds it, so what a binding produces is a draft the
        engine can run and not a second kind of record. It is one revision of the
        same strategy: the identity, the intent, the conversation, the calls, the
        proposals and the experiments are kept, nothing is undone, and binding again
        is refused. The original request is retained — in the change ledger, and,
        because that ledger is bounded, on the record itself — so the same operation
        replays as itself across a restart and after the draft has moved on, while a
        different payload under that operation is a conflict.

        The per-strategy lock is taken without waiting, so this never binds silently
        behind an approved call that is still in flight, and a durable call whose
        outcome was never observed is refused until recovery has classified it. No
        model call, no preview, no child and no job is created here.
        """
        identity = _identifier(user_strategy_id, "user_strategy_id")
        operation = _identifier(operation_id, "operation_id")
        if type(base_revision) is not int:
            raise UserStrategyStateError("base_revision must be an integer")
        scenario = _bounded(scenario_id, "scenario_id", _MAX_SCENARIO_ID)
        try:
            frozen_criterion = validate_criterion(criterion)
        except CriterionRejectedError as error:
            raise UserStrategyStateError(str(error)) from None
        # The whole request is this operation's identity: the revision the caller
        # read, the exact scenario and the criterion, in the host's own vocabulary.
        request: dict[str, Any] = {
            "action": "bind",
            "base_revision": base_revision,
            "scenario_id": scenario,
            "criterion": frozen_criterion,
        }
        lock = self._lock(identity)
        if not lock.acquire(blocking=False):
            raise UserStrategyStateError(
                "this strategy is busy with a call that has not settled; nothing was bound, "
                "so reload it and choose the scenario again"
            )
        try:
            strategy = self._store.get(identity)
            binding = strategy.binding
            if binding is not None and binding.get("operation_id") == operation:
                # The recorded binding is the answer to this operation: it is compared
                # with the request it was made from, never with today's draft.
                if binding.get("request") != request:
                    raise UserStrategyConflictError(
                        "operation_id is already bound to a different binding request"
                    )
                return strategy
            if _entry(strategy.changes, "operation_id", operation) is not None:
                # An apply, an undo or another binding already owns this operation id.
                raise UserStrategyConflictError(
                    "operation_id is already bound to a different operation"
                )
            if base_revision != strategy.draft.revision:
                raise UserStrategyStaleError(
                    "the strategy revision moved on; reload it before binding a scenario"
                )
            if not isinstance(strategy.draft, IntentDraft):
                raise UserStrategyStateError(
                    "a scenario is already bound to this strategy: binding is one explicit "
                    "choice and cannot be repeated or undone"
                )
            if self._unresolved_call(strategy) is not None:
                raise UserStrategyStateError(
                    "an earlier call was never observed; reload the strategy so it is "
                    "classified before a scenario is bound"
                )
            draft = self._scenario_draft(scenario, frozen_criterion, strategy.draft.revision + 1)
            now = _now()
            change: dict[str, Any] = {
                "operation_id": operation,
                "kind": "bind",
                "proposal_id": None,
                "target_operation_id": None,
                "scenario_id": scenario,
                "criterion": frozen_criterion,
                "from_revision": strategy.draft.revision,
                "to_revision": draft.revision,
                "applied_paths": [],
                "stale_paths": [],
                "items": [],
                "evidence": [],
                "proposer": "user",
                "at": now,
            }
            record = {
                "operation_id": operation,
                "request": request,
                "request_sha256": _digest(request),
                "scenario_id": scenario,
                "from_revision": strategy.draft.revision,
                "to_revision": draft.revision,
                "bound_at": now,
            }
            # One durable write: the executable draft, the intent it came from and the
            # request it was bound by exist together, or not at all.
            next_strategy = replace(
                strategy,
                updated_at=now,
                draft=draft,
                history=(*strategy.history, strategy.draft)[-_MAX_HISTORY:],
                changes=(*strategy.changes, change)[-_MAX_CHANGES:],
                binding=record,
            )
            self._store.save(next_strategy)
            return next_strategy
        finally:
            lock.release()

    # -- approved model calls -----------------------------------------------

    def _lock(self, user_strategy_id: str) -> threading.Lock:
        with threading.Lock():
            return self._locks.setdefault(user_strategy_id, threading.Lock())

    def _unresolved_call(self, strategy: UserStrategy) -> dict[str, Any] | None:
        """One durable call of this strategy whose outcome was never observed here.

        A call that is in flight in this process right now is not unresolved: it is
        being observed by the thread that holds it, and its own outcome is still
        coming. Everything else pending or dispatched here is an interruption that
        recovery has to classify before anything else touches this strategy.
        """
        for item in strategy.calls:
            if (
                item.get("status") in {"pending", "dispatched"}
                and (
                    strategy.user_strategy_id,
                    str(item.get("call_id")),
                )
                not in self._active_calls
            ):
                return item
        return None

    def _ask(
        self,
        strategy: UserStrategy,
        *,
        call_id: object,
        base_revision: object,
        text: object,
        approved_budget: Any,
        action: str = "message",
        proposal_id: str | None = None,
        before: Any = None,
        evidence_job_ids: Any = None,
    ) -> UserStrategy:
        call = _identifier(call_id, "call_id")
        message = _bounded(text, "message", _MAX_MESSAGE)
        if type(base_revision) is not int:
            raise UserStrategyStateError("base_revision must be an integer")
        selection = self._evidence_selection(evidence_job_ids)
        # The whole semantic request is bound to the call identity: the text, the
        # revision, the action and its target, and the exact approved budget. A
        # different action, target or budget under the same id is a conflict. The
        # selected runs are part of that request too, and only a call that selected
        # any carries the key at all, so a request written before this field existed
        # still replays as exactly the request it was.
        request: dict[str, Any] = {
            "action": action,
            "text": message,
            "base_revision": base_revision,
            "proposal_id": proposal_id,
            "approved_budget": _requested_budget(approved_budget),
        }
        if selection is not None:
            request["evidence_job_ids"] = selection
        existing = _entry(strategy.calls, "call_id", call)
        if existing is not None:
            # The recorded outcome is the answer to this identity, whatever it was,
            # compared against the persisted request rather than today's settings.
            if existing.get("request") != request:
                raise UserStrategyConflictError("call_id is already bound to a different request")
            return strategy
        if base_revision != strategy.draft.revision:
            raise UserStrategyStaleError(
                "the draft revision moved on; reload the strategy before asking again"
            )
        if len(strategy.calls) >= _MAX_CALLS or len(strategy.messages) + 1 >= _MAX_MESSAGES:
            raise UserStrategyStateError("this strategy has reached its recorded call limit")
        unresolved = self._unresolved_call(strategy)
        if unresolved is not None:
            # An earlier call whose outcome was never observed is settled first: a
            # new call never overwrites an unresolved dispatch.
            raise UserStrategyStateError(
                "an earlier call was never observed; reload the strategy so it is "
                "classified before another call is started"
            )
        budget = self._approval(approved_budget, model_calls=_CALL_APPROVAL, backtests=0)
        # The selected runs are read, verified and frozen here: after the approval,
        # before the durable reservation and before the provider can be reached, so a
        # selection this workspace cannot verify leaves no call record and no
        # dispatch behind it, and no conversation is ever grounded in a run the user
        # did not select or that is no longer the run it names.
        evidence = None if selection is None else self._evidence_context(strategy, selection)
        self._model_port()
        route = self._safe_route()
        now = _now()
        record: dict[str, Any] = {
            "call_id": call,
            # Durable before the provider can be reached: a crash here proves that
            # nothing was sent, which a single "dispatched" status could not.
            "status": "pending" if route is not None else "unconfigured",
            "route": route or {"provider": None, "model": None, "endpoint": None},
            # The public projection of this call's own request: the same validated
            # canonical base revision the request froze, never today's mutable
            # draft, so a stored call reads back unchanged after a reload.
            "base_revision": base_revision,
            "approved_budget": budget,
            "request": request,
            "payload_sha256": _digest(request),
            "reserved_at": now,
            "message": None
            if route is not None
            else "No model provider is configured in this workspace.",
            "model": None,
            "input_tokens": None,
            "output_tokens": None,
            "usage_available": False,
            "cost": {
                "amount": None,
                "available": False,
                "reason": "no price source is configured in this workspace",
            },
        }
        if evidence is not None:
            # The provenance of this turn, kept on the call it belongs to: the runs
            # the user selected and the host-verified context each of them was read
            # as. It is a record of what was explained, never a new authority over
            # the draft, the runs or the budget.
            record["evidence"] = evidence
        pending = before(strategy) if before is not None else strategy
        pending = replace(
            pending,
            updated_at=now,
            messages=(
                *pending.messages,
                {
                    "message_id": str(uuid.uuid4()),
                    "role": "user",
                    "kind": "message",
                    "text": message,
                    "base_revision": base_revision,
                    "at": now,
                    "call_id": call,
                    "proposal_id": proposal_id,
                },
            ),
            calls=(*pending.calls, record),
        )
        # One durable write: the user's own message, the approved budget and the
        # reservation exist before the provider is reached.
        self._store.save(pending)
        if route is None:
            return pending
        return self._observe(pending, call, message, base_revision)

    def _observe(
        self, strategy: UserStrategy, call: str, message: str, base_revision: int
    ) -> UserStrategy:
        port = self._model_port()
        draft = strategy.draft
        if isinstance(draft, IntentDraft):
            return self._clarify(strategy, call, message)
        # The contract this call is described by is resolved locally, before the
        # dispatch boundary: a workspace that cannot read its own registered contract
        # never reaches the provider and never marks this call dispatched, so the
        # durable record stays a reservation that recovery classifies as unsent.
        registered = self._registered_paths(draft)
        # The evidence this turn was admitted with is read back from its own call
        # record: a conversation is told what the user selected when this call was
        # made, never whichever run happens to be the latest one now.
        selected = self._call_evidence(strategy, call)
        strategy = self._mark_dispatched(strategy, call)
        key = (strategy.user_strategy_id, call)
        self._active_calls.add(key)
        try:
            try:
                response: ModelResponse = port.complete(
                    system=_SYSTEM_PROMPT,
                    messages=self._conversation(strategy, message, registered, selected),
                    tools=[self._propose_tool(registered)],
                    max_tokens=_MAX_TOKENS,
                    required_capabilities=("tool_use",),
                )
            except ModelNotConfigured as error:
                return self._settle(strategy, call, "unconfigured", str(error), None)
            except ModelProviderError as error:
                return self._settle(strategy, call, "unknown", str(error), None)
            except Exception as error:  # noqa: BLE001 — an unobserved outcome is unknown
                return self._settle(
                    strategy,
                    call,
                    "unknown",
                    f"the provider outcome was not observed: {error}",
                    None,
                )
            # The provider answered, so the outcome was observed: a response this
            # workspace cannot read — malformed, oversized or not a proposal — is
            # recorded as a refusal with its measured usage, never as unknown.
            try:
                text = _bounded(response.text, "response", _MAX_RESPONSE, required=False)
                items, refusal = self._diff(strategy, response)
            except Exception as error:  # noqa: BLE001 — the observed answer is refused
                return self._settle(strategy, call, "refused", str(error), response, text="")
        finally:
            self._active_calls.discard(key)
        if refusal is not None:
            return self._settle(strategy, call, "refused", refusal, response, text=text)
        proposal_id: str | None = None
        proposals = strategy.proposals
        if items:
            proposal_id = str(uuid.uuid4())
            proposals = (
                *proposals[-_MAX_PROPOSALS:],
                {
                    "proposal_id": proposal_id,
                    "call_id": call,
                    "base_revision": base_revision,
                    "status": "open",
                    "created_at": _now(),
                    "items": items,
                    "applied_paths": [],
                    "stale_paths": [],
                },
            )
        return self._settle(
            strategy,
            call,
            "observed",
            None,
            response,
            text=text,
            proposals=proposals,
            proposal_id=proposal_id,
        )

    def _clarify(self, strategy: UserStrategy, call: str, message: str) -> UserStrategy:
        """One approved clarification of an intent that has no scenario yet.

        The request is the same approved call every other turn is — same identity,
        same reservation, same ledger, same recovery — and it is only what it is
        told that differs: this strategy's own intent and its own recorded turns,
        and nothing about a strategy, instrument, funding, parameter, scenario or
        run, because none of those exists yet. There is nothing a change could be
        proposed against, so the call offers no tool and requires no capability,
        and an answer that calls a tool anyway is refused whole with its measured
        usage and zero proposals. An answer recorded here is a message: it can
        never become a proposal later, when a scenario has been bound.
        """
        port = self._model_port()
        strategy = self._mark_dispatched(strategy, call)
        key = (strategy.user_strategy_id, call)
        self._active_calls.add(key)
        try:
            try:
                response: ModelResponse = port.complete(
                    system=_INTENT_SYSTEM_PROMPT,
                    messages=self._conversation(strategy, message, ()),
                    tools=[],
                    max_tokens=_MAX_TOKENS,
                    required_capabilities=(),
                )
            except ModelNotConfigured as error:
                return self._settle(strategy, call, "unconfigured", str(error), None)
            except ModelProviderError as error:
                return self._settle(strategy, call, "unknown", str(error), None)
            except Exception as error:  # noqa: BLE001 — an unobserved outcome is unknown
                return self._settle(
                    strategy,
                    call,
                    "unknown",
                    f"the provider outcome was not observed: {error}",
                    None,
                )
            # The provider answered, so this outcome was observed: an answer this
            # workspace cannot read is a refusal with its measured usage, never
            # unknown, and never a proposal.
            try:
                text = _bounded(response.text, "response", _MAX_RESPONSE, required=False)
                refusal = self._clarification_refusal(response)
            except Exception as error:  # noqa: BLE001 — the observed answer is refused
                return self._settle(strategy, call, "refused", str(error), response, text="")
        finally:
            self._active_calls.discard(key)
        return self._settle(
            strategy,
            call,
            "refused" if refusal is not None else "observed",
            refusal,
            response,
            text=text,
        )

    @staticmethod
    def _clarification_refusal(response: ModelResponse) -> str | None:
        """Why one answer to an unbound intent is refused whole, or ``None``."""
        if not response.tool_calls:
            return None
        return (
            "no scenario is bound to this strategy yet, so this answer had nothing it "
            "could change or run; nothing was proposed"
        )

    def _conversation(
        self,
        strategy: UserStrategy,
        message: str,
        registered: tuple[RegisteredPath, ...],
        evidence: Documents | None = None,
    ) -> History:
        """One request to the model: the strategy's own facts, plus its history.

        The user's own message is already persisted on this strategy and is the
        question being asked here, so it is sent exactly once, as the last turn.
        Everything before it is the recorded conversation, and the opening turn
        carries the stated intent, the registered paths of this draft — each with
        its exact JSON value type, current value and the bounds this workspace
        enforces, the read-only ones included — and, about frozen runs, only what
        this call is allowed to state: the host's own verified readings of the runs
        the user selected when it was made, or, for a call made without a selection,
        the host's own readings of this strategy's latest frozen run. A strategy
        that has no scenario yet states nothing about a draft, because it has no
        draft to state.
        """
        draft = strategy.draft
        if isinstance(draft, IntentDraft):
            return self._intent_conversation(strategy, message)
        facts = [
            f"The user's stated intent for this strategy: {strategy.intent}",
            f"Strategy {draft.strategy_id} on {draft.venue}:{draft.symbol}",
            f"Scenario {draft.scenario_id}",
            "The registered fields of this draft, each stated with the exact JSON value "
            "type and the bounds this workspace enforces:",
        ]
        facts.extend(f"  {entry.sentence()}" for entry in registered)
        facts.append("Draft revision " + str(draft.revision))
        if draft.criterion is not None:
            facts.append(
                "The user's stated criterion: "
                f"{draft.criterion['field']} is {draft.criterion['expectation']}"
            )
        facts.extend(self._selected_facts(evidence) if evidence else self._verified_facts(strategy))
        return [
            ModelMessage(role="user", text="\n".join(facts)),
            *self._recorded_turns(strategy, message),
        ]

    def _intent_conversation(self, strategy: UserStrategy, message: str) -> History:
        """One request about an intent: its own words and turns, and nothing else.

        No strategy, venue, symbol, funding, parameter, scenario or run is stated,
        not even as an example, because this strategy has none of them yet, and the
        model is told plainly that it has no tool here and that this turn can only
        clarify what the user means.
        """
        facts = [
            f"The user's stated intent for this strategy: {strategy.intent}",
            "No scenario is bound to this strategy yet, so there is no strategy, "
            "instrument, funding, parameter, dataset or run to describe, and nothing "
            "here can be changed, previewed or run.",
            "This turn can only clarify the intent: what the user means, which market "
            "and data the idea needs, which assumptions matter and what is still "
            "missing. No change to a draft can be proposed until the user chooses a "
            "scenario.",
            "Intent revision " + str(strategy.draft.revision),
        ]
        facts.extend(self._verified_facts(strategy))
        return [
            ModelMessage(role="user", text="\n".join(facts)),
            *self._recorded_turns(strategy, message),
        ]

    @staticmethod
    def _recorded_turns(strategy: UserStrategy, message: str) -> History:
        """The recorded conversation, once each, then the question asked here.

        The user's own message is already persisted on this strategy and is the
        question being asked, so it is sent exactly once, as the last turn.
        """
        turns: History = []
        for entry in strategy.messages[:-1][-6:]:
            role = "user" if entry.get("role") == "user" else "assistant"
            text = str(entry.get("text") or "")
            if text:
                turns.append(ModelMessage(role=cast(Any, role), text=text))
        turns.append(ModelMessage(role="user", text=message))
        return turns

    def _verified_facts(self, strategy: UserStrategy) -> Paths:
        """Bounded facts about this strategy's own frozen runs, or an honest absence.

        The report is read through the workspace's verified-report authority, so a
        run that has not succeeded, a missing report and a report that belongs to
        another run are all stated as unavailable instead of guessed. No model
        claim and no wider research context enters here, and this never raises.
        """
        draft = strategy.draft
        if not isinstance(draft, StrategyDraft):
            # An intent has no frozen run and never can: a run needs an executable
            # draft, so there is no fact about one to state.
            return []
        record = next(
            (item for item in reversed(strategy.experiments) if item.get("job_id")),
            None,
        )
        if record is None:
            return []
        job_id = str(record.get("job_id"))
        payload = cast(dict[str, Any], record.get("payload") or {})
        revision = payload.get("draft_revision")
        try:
            report = json.loads(self._service.report(job_id))
        except Exception:  # noqa: BLE001 — an unverifiable run has no fact to state
            return [
                f"An experiment was confirmed from this strategy's revision {revision} "
                f"(job {job_id}) and its verified report is not available here, so this "
                "conversation states nothing about what it found."
            ]
        if not isinstance(report, dict):
            return []
        facts = [
            f"Host-verified readings of this strategy's own frozen run for revision "
            f"{revision} (job {job_id}), and nothing wider:"
        ]
        for path in _REPORT_FACTS:
            value = read_field(cast(dict[str, Any], report), path)
            if value is not None:
                facts.append(f"  {path} = {value}")
        reading = host_reading(draft.criterion, cast(dict[str, Any], report))
        facts.append(
            "  the host's own reading of the stated criterion: "
            f"{reading['outcome'] or 'not evaluable on this report'} "
            f"({reading['inference'] or 'no inference code'})"
        )
        facts.append("One run that succeeded is one frozen run, not research that is complete.")
        return facts

    @staticmethod
    def _call_evidence(strategy: UserStrategy, call: str) -> Documents | None:
        """The frozen evidence one call was admitted with, read from that call.

        This is provenance, not authority: it is read back from the call's own
        durable record, so a conversation is grounded in what the user selected when
        that call was made — never in whichever run happens to be latest by the time
        the request is sent.
        """
        record = _entry(strategy.calls, "call_id", call)
        if record is None:
            return None
        evidence = record.get("evidence")
        if type(evidence) is not list or not evidence:
            return None
        return cast(Documents, evidence)

    def _evidence_selection(self, evidence_job_ids: Any) -> Paths | None:
        """The frozen runs one request selects, exactly as that request states them.

        Absent means an ordinary conversation, which is what every call made before
        this field existed is. A selection is one or two distinct bounded ids in the
        order the user gave them: A first, and B after it.
        """
        if evidence_job_ids is None:
            return None
        if type(evidence_job_ids) is not list or not evidence_job_ids:
            raise UserStrategyStateError(
                "evidence_job_ids must name one or two frozen runs of this strategy; "
                "nothing was asked"
            )
        if len(evidence_job_ids) > _MAX_EVIDENCE_RUNS:
            raise UserStrategyStateError(
                f"at most {_MAX_EVIDENCE_RUNS} frozen runs can be explained at once; "
                "nothing was asked"
            )
        selection: Paths = []
        for item in evidence_job_ids:
            identifier = _identifier(item, "evidence_job_ids")
            if identifier in selection:
                raise UserStrategyStateError(
                    "evidence_job_ids must name different frozen runs; nothing was asked"
                )
            selection.append(identifier)
        return selection

    def _evidence_context(self, strategy: UserStrategy, selection: Paths) -> Documents:
        """The host-verified, frozen context of the runs one turn is grounded in.

        Every fact here is read from this strategy's own retained record, the job
        that record names and that job's verified report. A selected id that is not
        exactly one created experiment of this strategy — foreign, duplicated,
        ambiguous, failed, unreadable or mismatched — is refused here, before the
        call record exists and before any provider can be reached, so a conversation
        is never grounded in a run the user did not select.
        """
        created = [
            item
            for item in strategy.experiments
            if item.get("status") == "created" and item.get("job_id")
        ]
        context: Documents = []
        for job_id in selection:
            matches = [item for item in created if str(item.get("job_id")) == job_id]
            if len(matches) != 1:
                raise UserStrategyStateError(
                    "a selected id is not one frozen run of this strategy; nothing was asked"
                )
            context.append(self._evidence_run(matches[0], job_id))
        return context

    def _evidence_run(self, record: dict[str, Any], job_id: str) -> dict[str, Any]:
        """One selected run, read and verified, or a refusal to explain anything.

        The retained payload, the job it names and that job's report are held to one
        another: the payload must still hash to the digest it was recorded with, the
        run must have succeeded and published a report, the job must be the input this
        strategy confirmed, the report must be the run it was read for and must still
        hash to the digest the job published, the report must be readable as evidence
        under the host's own vocabulary, and the report's own source identity must be
        the one the run was admitted under. Anything else is an honest unavailability
        rather than a reading of some other run.
        """
        # The retained payload is the authority for every contextual field read below —
        # its revision, its parameters, its admitted blocks and its criterion. It is
        # that authority only while it is the payload that was frozen: a missing or
        # mismatched digest is refused here, before any of those fields is read, so an
        # edited frozen block can never be stated as host-verified evidence.
        stored = record.get("payload")
        recorded = record.get("payload_sha256")
        if (
            not isinstance(stored, dict)
            or not isinstance(recorded, str)
            or _digest(stored) != recorded
        ):
            raise UserStrategyStateError(
                "a selected run's frozen payload is not the payload it was recorded with; "
                "nothing was asked"
            )
        payload = cast(dict[str, Any], stored)
        try:
            job = self._service.get_job(job_id)
        except Exception:  # noqa: BLE001 — an unreadable run is not evidence
            raise UserStrategyStateError(
                "a selected run could not be read; nothing was asked"
            ) from None
        # The persisted record itself says whether this run produced a report: it is
        # succeeded, and it published a report digest. Whether the bytes behind that
        # digest are readable, and are the report they claim to be, is decided below by
        # the workspace's own report authority.
        if getattr(job, "status", None) != "succeeded":
            raise UserStrategyStateError(
                "a selected run has not succeeded, so it has no verified report; nothing was asked"
            )
        published = getattr(job, "report_sha256", None)
        if not isinstance(published, str) or not published:
            raise UserStrategyStateError(
                "a selected run published no report, so it has no verified report; "
                "nothing was asked"
            )
        run_id = getattr(job, "engine_run_id", None)
        if not isinstance(run_id, str) or not run_id:
            raise UserStrategyStateError(
                "a selected run records no engine run id; nothing was asked"
            )
        if not self._job_matches_confirmation(record, job):
            raise UserStrategyStateError(
                "a selected run is not the input this strategy confirmed; nothing was asked"
            )
        try:
            raw = self._service.report(job_id)
            report = json.loads(raw)
        except Exception:  # noqa: BLE001 — an unverifiable report is not evidence
            raise UserStrategyStateError(
                "a selected run has no readable verified report; nothing was asked"
            ) from None
        if not isinstance(report, dict) or report.get("run_id") != run_id:
            raise UserStrategyStateError(
                "a selected report is not the run it was read for; nothing was asked"
            )
        digest = hashlib.sha256(raw).hexdigest() if type(raw) is bytes else None
        if digest is None or published != digest:
            raise UserStrategyStateError(
                "a selected report is not the report this run published; nothing was asked"
            )
        # The confirmed input identity is required, not optional: a report that states
        # no source, or a record that kept none, is unverified rather than agreeing.
        identity = payload.get("input_identity")
        if not isinstance(identity, dict) or not self._report_source_matches(report, identity):
            raise UserStrategyStateError(
                "a selected report does not state the source this run was confirmed from; "
                "nothing was asked"
            )
        verified = cast(dict[str, Any], report)
        criterion = payload.get("criterion")
        frozen_criterion = criterion if isinstance(criterion, dict) else None
        # A run may have been frozen with no criterion at all; the host states that as
        # its own reading (``not_stated``), which is a reading and not a refusal. What
        # is refused is a report the host's vocabulary is not defined against — an
        # unknown schema or a value of the wrong JSON type — because nothing can be
        # published from it as evidence.
        found = host_reading(frozen_criterion, verified)
        if found["outcome"] is None:
            raise UserStrategyStateError(
                "a selected report cannot be read as evidence; nothing was asked"
            )
        facts: Documents = []
        for path in _REPORT_FACTS:
            value = read_field(verified, path)
            if value is None:
                continue
            spec = FIELD_SPECS_BY_PATH.get(path)
            currency = (
                read_field(verified, spec.currency_path)
                if spec is not None and spec.currency_path
                else None
            )
            facts.append(
                {
                    "path": path,
                    # A count stays a JSON integer; a decimal stays the report's own
                    # canonical string and is never re-typed into a number here.
                    "value": value if type(value) is int else str(value),
                    "currency": currency if isinstance(currency, str) else None,
                }
            )
        normalized = payload.get("normalized")
        blocks = normalized if isinstance(normalized, dict) else {}
        return {
            "job_id": job_id,
            "launch_intent_id": record.get("launch_intent_id"),
            "draft_revision": payload.get("draft_revision"),
            "criterion": frozen_criterion,
            "run_id": run_id,
            "report_sha256": digest,
            "input_sha256": payload.get("input_sha256"),
            "record_count": payload.get("record_count"),
            "parameters": dict(payload.get("parameters") or {}),
            # The admitted, normalized blocks of this run: the instrument, the data
            # window, the cost, risk and funding assumptions it was run under, and
            # the strategy implementation they name.
            "assumptions": {
                key: blocks.get(key)
                for key in ("strategy", "execution", "risk", "funding", "instrument", "data")
            },
            "facts": facts,
            "reading": {"outcome": found["outcome"], "inference": found["inference"]},
            # The limitations this run's own recorded facts justify, from the host's
            # closed vocabulary: they belong to what this run is evidence for.
            "limitations": host_limitations(verified),
        }

    @staticmethod
    def _report_source_matches(report: dict[str, Any], identity: dict[str, Any]) -> bool:
        """Whether a report's own source is exactly the source this run was admitted from.

        Both sides have to state it, and state the same thing: the scenario digest the
        run was confirmed under and the report's own, and the admitted data fingerprint
        against the one the report recorded. A report that states no source, or a record
        that kept no identity, is not read as agreement — an unverified source fails
        closed rather than being assumed to be the frozen one.
        """
        source = report.get("source")
        scenario = source.get("scenario_sha256") if isinstance(source, dict) else None
        expected_scenario = identity.get("scenario_sha256")
        if not isinstance(scenario, str) or not isinstance(expected_scenario, str):
            return False
        if scenario != expected_scenario:
            return False
        data = source.get("data") if isinstance(source, dict) else None
        fingerprint = data.get("fingerprint") if isinstance(data, dict) else None
        digest = fingerprint.get("sha256") if isinstance(fingerprint, dict) else None
        expected_data = identity.get("data_sha256")
        if not isinstance(digest, str) or not isinstance(expected_data, str):
            return False
        return digest == expected_data

    @staticmethod
    def _selected_facts(evidence: Documents) -> Paths:
        """What the user selected, stated as the host read it, and nothing wider.

        Every line is a host-verified reading of one frozen run the user named for
        this turn. Only those runs are described — no other run of this strategy is
        mentioned, and no later run is read in their place — and the limits of a
        single window are stated with them, so the model is told what a reading is
        and what it is not. The registered fields above still describe today's draft,
        and a proposal still targets that draft.
        """
        facts = [
            "The user selected these frozen runs as this turn's evidence. Only these "
            "are stated here: no other run of this strategy is described, and no later "
            "run is read in their place.",
        ]
        for index, item in enumerate(evidence):
            label = "A" if index == 0 else "B"
            facts.append(
                f"Selected run {label}: job {item.get('job_id')}, engine run "
                f"{item.get('run_id')}, frozen draft revision {item.get('draft_revision')}, "
                f"launch {item.get('launch_intent_id')}"
            )
            facts.append(
                f"  its confirmed input digest {item.get('input_sha256')} and verified "
                f"report digest {item.get('report_sha256')}, over "
                f"{item.get('record_count')} records"
            )
            parameters = item.get("parameters") or {}
            if parameters:
                facts.append(
                    "  the parameters it froze: "
                    + ", ".join(f"{key} = {value}" for key, value in parameters.items())
                )
            facts.append(
                "  the assumptions it was admitted under: "
                + json.dumps(item.get("assumptions") or {}, sort_keys=True, default=str)
            )
            for reading in item.get("facts") or []:
                unit = f" {reading['currency']}" if reading.get("currency") else ""
                facts.append(f"  {reading['path']} = {reading['value']}{unit}")
            criterion = item.get("criterion")
            found = item.get("reading") or {}
            if criterion is None:
                facts.append(
                    "  no criterion was frozen before this run, so its result cannot "
                    "judge the thesis either way"
                )
            else:
                facts.append(
                    f"  its frozen criterion {criterion.get('field')} is "
                    f"{criterion.get('expectation')}, and the host's own reading of it: "
                    f"{found.get('outcome')} ({found.get('inference')})"
                )
            for code in item.get("limitations") or []:
                facts.append(f"  limitation of this run: {code}")
        facts.extend(
            [
                "These readings are host-verified facts about those frozen runs. "
                "Anything you say beyond them is your own inference, not evidence.",
                "One run over one window supports no causal claim, no profit claim and no "
                "robustness claim; a difference between two runs is a comparability "
                "question before it is an explanation.",
                "Net P&L includes the valuation of any position still open at the end of "
                "the window, so it is not realized profit.",
                "Explaining these runs and proposing a next experiment are not running "
                "anything: a run happens only from the user's own separate confirmation "
                "of a preview.",
            ]
        )
        return facts

    def _diff(
        self, strategy: UserStrategy, response: ModelResponse
    ) -> tuple[Documents, str | None]:
        """The typed diff this response proposes, or the reason it is refused whole."""
        draft = _executable(strategy)
        calls = list(response.tool_calls)
        if not calls:
            return [], None
        if len(calls) != 1 or calls[0].name != _PROPOSE_TOOL_NAME:
            return [], "the model used an unknown tool; nothing was proposed"
        arguments = calls[0].arguments
        raw = arguments.get("items") if isinstance(arguments, dict) else None
        if type(raw) is not list or not raw or len(raw) > _MAX_ITEMS:
            return [], "the proposed change had no readable fields; nothing was proposed"
        document = self._scenario_document(draft.scenario_id)
        contracts = self._contracts(document)
        items: Documents = []
        seen: set[str] = set()
        for entry in raw:
            if type(entry) is not dict:
                return [], "the proposed change was not a readable field; nothing was proposed"
            path = entry.get("path")
            if not isinstance(path, str) or _PATH.fullmatch(path) is None:
                return [], f"{path!r} is not a changeable field; nothing was proposed"
            if path in seen:
                return [], f"{path} was proposed twice; nothing was proposed"
            seen.add(path)
            before = self._current(draft, path)
            after, reason = self._typed(contracts, path, entry.get("value"), draft)
            if reason is not None:
                return [], reason
            items.append(
                {
                    "path": path,
                    "before": before,
                    "after": after,
                    "reason": _bounded(entry.get("reason"), "reason", _MAX_REASON, required=False)
                    or "no reason was given",
                    "evidence": self._evidence(entry.get("evidence")),
                    "proposer": "model",
                }
            )
        return items, None

    @staticmethod
    def _current(draft: StrategyDraft, path: str) -> Any:
        if path == "initial_cash":
            return draft.initial_cash
        return draft.parameters.get(path.split(".", 1)[1])

    def _typed(
        self,
        contracts: dict[str, dict[str, Any]],
        path: str,
        value: Any,
        draft: StrategyDraft,
    ) -> tuple[Any, str | None]:
        """One proposed value, typed and bounded by the registered contract."""
        if path == "initial_cash":
            if not isinstance(value, str) or _DECIMAL.fullmatch(value) is None:
                return None, "initial_cash must be a decimal string; nothing was proposed"
            if Decimal(value) <= 0:
                return None, "initial_cash must be positive; nothing was proposed"
            return value, None
        name = path.split(".", 1)[1]
        contract = contracts.get(name)
        if contract is None:
            return None, f"{name} is not a registered executable parameter; nothing was proposed"
        kind = contract.get("type")
        if kind == "integer":
            if type(value) is not int:
                return None, f"{name} must be an integer; nothing was proposed"
            if not self._within(value, contract):
                return None, f"{name} is outside its registered range; nothing was proposed"
            return int(value), None
        if kind == "decimal":
            if not isinstance(value, str) or _DECIMAL.fullmatch(value) is None:
                return None, f"{name} must be a decimal string; nothing was proposed"
            if not self._within(Decimal(value), contract):
                return None, f"{name} is outside its registered range; nothing was proposed"
            return value, None
        return None, f"{name} has a type this workspace cannot change; nothing was proposed"

    @staticmethod
    def _within(value: Any, contract: dict[str, Any]) -> bool:
        minimum = contract.get("minimum")
        maximum = contract.get("maximum")
        try:
            if minimum is not None and Decimal(str(minimum)) > Decimal(str(value)):
                return False
            if maximum is not None and Decimal(str(maximum)) < Decimal(str(value)):
                return False
        except (InvalidOperation, ValueError):
            return False
        return True

    @staticmethod
    def _evidence(value: Any) -> Paths:
        if value is None:
            return []
        if type(value) is not list:
            raise UserStrategyStateError("evidence references were not readable")
        if len(value) > _MAX_EVIDENCE:
            raise UserStrategyStateError("too many evidence references were proposed")
        return [_bounded(item, "evidence reference", _MAX_EVIDENCE_ITEM) for item in value]

    def _mark_dispatched(self, strategy: UserStrategy, call: str) -> UserStrategy:
        """Record the exact moment the provider may be reached, before it is.

        ``pending`` is durable before this, so a crash between the two writes
        proves nothing was sent; ``dispatched`` is durable before the call, so a
        crash after it leaves an outcome this workspace will not call absent.
        """
        now = _now()
        dispatched = replace(
            strategy,
            updated_at=now,
            calls=tuple(
                {**item, "status": "dispatched", "dispatched_at": now}
                if item.get("call_id") == call and item.get("status") == "pending"
                else item
                for item in strategy.calls
            ),
        )
        self._store.save(dispatched)
        return dispatched

    def _settle(
        self,
        strategy: UserStrategy,
        call: str,
        status: str,
        message: str | None,
        response: ModelResponse | None,
        *,
        text: str = "",
        proposals: tuple[dict[str, Any], ...] | None = None,
        proposal_id: str | None = None,
    ) -> UserStrategy:
        """One durable write: the observed outcome, then the messages it produced."""
        now = _now()
        model: str | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        if response is not None:
            model = response.model
            if response.usage_available:
                input_tokens = int(response.input_tokens or 0)
                output_tokens = int(response.output_tokens or 0)
        settled = {
            **cast(dict[str, Any], _entry(strategy.calls, "call_id", call)),
            "status": status,
            "message": message,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "usage_available": input_tokens is not None,
        }
        calls = tuple(settled if item.get("call_id") == call else item for item in strategy.calls)
        messages = strategy.messages
        if text or proposal_id is not None:
            messages = (
                *messages,
                {
                    "message_id": str(uuid.uuid4()),
                    "role": "model",
                    "kind": "proposal" if proposal_id is not None else "message",
                    "text": text,
                    "base_revision": settled["base_revision"],
                    "at": now,
                    "call_id": call,
                    "proposal_id": proposal_id,
                },
            )
        next_strategy = replace(
            strategy,
            updated_at=now,
            calls=calls,
            messages=messages,
            proposals=strategy.proposals if proposals is None else proposals,
        )
        self._store.save(next_strategy)
        return next_strategy

    def send_message(
        self,
        user_strategy_id: object,
        *,
        call_id: object,
        base_revision: object,
        text: object,
        approved_budget: Any = None,
        evidence_job_ids: Any = None,
    ) -> UserStrategy:
        """One approved turn, optionally grounded in this strategy's own frozen runs.

        ``evidence_job_ids`` is absent for every ordinary turn, and absent means this
        request is exactly the request it has always been: a call recorded before this
        field existed still replays as itself. When it is present it names one or two
        of this strategy's own frozen runs, in order — A first, then B — and those
        runs, frozen with their verified readings, are the only evidence this turn
        states. Nothing here runs anything: explaining evidence and proposing a next
        experiment are model calls, and a run still happens only from the user's own
        separate confirmation of a preview.
        """
        identity = _identifier(user_strategy_id, "user_strategy_id")
        with self._lock(identity):
            strategy = self._store.get(identity)
            return self._ask(
                strategy,
                call_id=call_id,
                base_revision=base_revision,
                text=text,
                approved_budget=approved_budget,
                action="message",
                evidence_job_ids=evidence_job_ids,
            )

    def revise_proposal(
        self,
        user_strategy_id: object,
        *,
        proposal_id: object,
        call_id: object,
        text: object = "",
        approved_budget: Any = None,
    ) -> UserStrategy:
        identity = _identifier(user_strategy_id, "user_strategy_id")
        target = _identifier(proposal_id, "proposal_id")
        with self._lock(identity):
            strategy = self._store.get(identity)
            # There is no draft to revise while nothing is bound, and this refuses
            # before the proposal lookup, so no call is admitted for an intent.
            _executable(strategy)
            existing = _entry(strategy.proposals, "proposal_id", target)
            if existing is None:
                raise UserStrategyStateError("this proposal is not part of the strategy")
            if existing["status"] == "applied":
                raise UserStrategyStateError(
                    "an applied change is revised by proposing again, not by revising it"
                )

            def supersede(current: UserStrategy) -> UserStrategy:
                return replace(
                    current,
                    proposals=tuple(
                        {**item, "status": "superseded"}
                        if item.get("proposal_id") == target and item.get("status") == "open"
                        else item
                        for item in current.proposals
                    ),
                )

            return self._ask(
                strategy,
                call_id=call_id,
                base_revision=strategy.draft.revision,
                text=_bounded(text, "message", _MAX_MESSAGE, required=False)
                or "Revise the proposed change",
                approved_budget=approved_budget,
                action="revise",
                proposal_id=target,
                before=supersede,
            )

    # -- applying, rejecting, revising, undoing -----------------------------

    def reject_proposal(self, user_strategy_id: object, *, proposal_id: object) -> UserStrategy:
        identity = _identifier(user_strategy_id, "user_strategy_id")
        target = _identifier(proposal_id, "proposal_id")
        with self._lock(identity):
            strategy = self._store.get(identity)
            # An intent has no proposal to reject, and saying so here is the guard
            # itself rather than a hidden button.
            _executable(strategy)
            existing = _entry(strategy.proposals, "proposal_id", target)
            if existing is None:
                raise UserStrategyStateError("this proposal is not part of the strategy")
            if existing["status"] == "applied":
                raise UserStrategyStateError("an applied change cannot be rejected")
            if existing["status"] == "rejected":
                return strategy
            # Rejecting changes the proposal's state only: the draft is untouched.
            next_strategy = replace(
                strategy,
                updated_at=_now(),
                proposals=tuple(
                    {**item, "status": "rejected"} if item.get("proposal_id") == target else item
                    for item in strategy.proposals
                ),
            )
            self._store.save(next_strategy)
            return next_strategy

    def apply_proposal(
        self,
        user_strategy_id: object,
        *,
        proposal_id: object,
        operation_id: object,
        paths: Any = None,
    ) -> UserStrategy:
        identity = _identifier(user_strategy_id, "user_strategy_id")
        target = _identifier(proposal_id, "proposal_id")
        operation = _identifier(operation_id, "operation_id")
        with self._lock(identity):
            strategy = self._store.get(identity)
            # Nothing executable exists while this strategy is an intent: an apply is
            # refused here, before any proposal, selection or revision is read.
            draft = _executable(strategy)
            prior = _entry(strategy.changes, "operation_id", operation)
            if prior is not None:
                # A repeated request is the same request: its selection is resolved
                # against the proposal it was made from, order-insensitively, so a
                # different selection is a conflict rather than a second apply.
                if prior.get("kind") != "apply" or prior.get("proposal_id") != target:
                    raise UserStrategyConflictError(
                        "operation_id is already bound to a different operation"
                    )
                original = _entry(strategy.proposals, "proposal_id", target)
                fields = cast(
                    Documents,
                    prior["items"] if original is None else original["items"],
                )
                recorded = set(cast(Paths, prior.get("applied_paths") or []))
                if recorded != set(self._selection(paths, fields)):
                    raise UserStrategyConflictError(
                        "operation_id is already bound to a different selection"
                    )
                return strategy
            proposal = _entry(strategy.proposals, "proposal_id", target)
            if proposal is None:
                raise UserStrategyStateError("this proposal is not part of the strategy")
            if proposal["status"] == "rejected":
                raise UserStrategyStateError("a rejected proposal cannot be applied")
            if proposal["status"] != "open":
                # An applied or superseded proposal is history. Applying it again, or
                # applying the proposal a revision already replaced, would run a
                # change the user never confirmed on this revision.
                raise UserStrategyStaleError(
                    "this proposal is no longer open; propose the change again on the "
                    "current revision"
                )
            if proposal["base_revision"] != strategy.draft.revision:
                raise UserStrategyStaleError(
                    "this proposal belongs to an older draft revision; propose the change again"
                )
            items = cast(Documents, proposal["items"])
            selected = self._selection(paths, items)
            chosen = [item for item in items if item["path"] in selected]
            for item in chosen:
                if self._current(draft, item["path"]) != item["before"]:
                    raise UserStrategyStaleError(
                        f"{item['path']} no longer has the value this proposal was made against"
                    )
            return self._revise_draft(
                strategy,
                overrides={item["path"]: item["after"] for item in chosen},
                operation=operation,
                kind="apply",
                proposal_id=target,
                target_operation=None,
                items=chosen,
                applied_paths=[item["path"] for item in chosen],
                stale_paths=[item["path"] for item in items if item["path"] not in selected],
                proposer=str(chosen[0].get("proposer") or "model"),
            )

    @staticmethod
    def _selection(paths: Any, items: Documents) -> Paths:
        known = [str(item["path"]) for item in items]
        if paths is None:
            return known
        if type(paths) is not list or not paths:
            raise UserStrategyStateError("select at least one field to apply")
        selected: Paths = []
        for path in paths:
            if not isinstance(path, str) or path not in known:
                raise UserStrategyStateError(f"{path!r} is not a field of this proposal")
            if path not in selected:
                selected.append(path)
        return selected

    def undo(
        self,
        user_strategy_id: object,
        *,
        operation_id: object,
        head_revision: object,
        target_operation_id: object,
    ) -> UserStrategy:
        """Reverse exactly one named applied change of the revision the caller read.

        The caller names the head revision it acted on and the application it means,
        so a stale browser cannot undo whatever happens to be newest: an undo is
        accepted only for the head of the revision the caller is looking at.
        """
        identity = _identifier(user_strategy_id, "user_strategy_id")
        operation = _identifier(operation_id, "operation_id")
        target = _identifier(target_operation_id, "target_operation_id")
        if type(head_revision) is not int:
            raise UserStrategyStateError("head_revision must be an integer")
        with self._lock(identity):
            strategy = self._store.get(identity)
            # An intent has no applied change to reverse, and a binding is not one:
            # a recorded binding is never a target here.
            _executable(strategy)
            prior = _entry(strategy.changes, "operation_id", operation)
            if prior is not None:
                if (
                    prior.get("kind") != "undo"
                    or prior.get("target_operation_id") != target
                    or prior.get("from_revision") != head_revision
                ):
                    raise UserStrategyConflictError(
                        "operation_id is already bound to a different operation"
                    )
                return strategy
            applied = _entry(strategy.changes, "operation_id", target)
            if applied is None or applied.get("kind") != "apply":
                raise UserStrategyStateError("that is not an applied change of this strategy")
            if (
                head_revision != strategy.draft.revision
                or applied.get("to_revision") != strategy.draft.revision
            ):
                raise UserStrategyStaleError(
                    "only the latest change on the revision you are looking at can be undone"
                )
            items = cast(Documents, applied["items"])
            if not items:
                raise UserStrategyStateError("that change has no fields to undo")
            # The recorded change is reversed field by field: each path keeps the
            # before/after it actually had, its reason, its evidence and who
            # proposed it, with the direction of the reversal written down.
            reversed_items = [
                {
                    **item,
                    "before": item["after"],
                    "after": item["before"],
                }
                for item in items
            ]
            return self._revise_draft(
                strategy,
                overrides={item["path"]: item["before"] for item in items},
                operation=operation,
                kind="undo",
                proposal_id=cast(str | None, applied.get("proposal_id")),
                target_operation=target,
                items=reversed_items,
                applied_paths=[item["path"] for item in items],
                stale_paths=[],
                proposer="user",
            )

    def _revise_draft(
        self,
        strategy: UserStrategy,
        *,
        overrides: dict[str, Any],
        operation: str,
        kind: str,
        proposal_id: str | None,
        target_operation: str | None,
        items: Documents,
        applied_paths: Paths,
        stale_paths: Paths,
        proposer: str,
    ) -> UserStrategy:
        """Validate the complete resulting draft, then write exactly one revision."""
        draft = _executable(strategy)
        parameters = dict(draft.parameters)
        initial_cash = draft.initial_cash
        for path, value in overrides.items():
            if path == "initial_cash":
                initial_cash = value
            else:
                parameters[path.split(".", 1)[1]] = value
        # The backend validates the whole draft, not just the selected fields, so a
        # dependent field that cannot hold the new value fails the whole subset.
        document = self._validated(draft, parameters, initial_cash)
        summary = cast(dict[str, Any], document.get("summary") or {})
        # The backend's own normalization wins per key, but the draft's parameter
        # set is exactly what the user confirmed: validation never adds or drops a
        # field, it only rejects a combination the strategy cannot run.
        normalized = summary.get("parameters")
        if isinstance(normalized, dict):
            parameters = {name: normalized.get(name, value) for name, value in parameters.items()}
        revised = replace(
            draft,
            revision=draft.revision + 1,
            initial_cash=str(summary.get("initial_cash") or initial_cash),
            parameters=parameters,
        )
        now = _now()
        change = {
            "operation_id": operation,
            "kind": kind,
            "proposal_id": proposal_id,
            "target_operation_id": target_operation,
            "from_revision": draft.revision,
            "to_revision": revised.revision,
            "applied_paths": list(applied_paths),
            "stale_paths": list(stale_paths),
            "items": [
                {
                    **{key: item[key] for key in ("evidence", "proposer") if key in item},
                    "path": item["path"],
                    "before": item["before"],
                    "after": item["after"],
                    "reason": item.get("reason") or "",
                }
                for item in items
            ],
            "evidence": [
                reference for item in items for reference in cast(Paths, item.get("evidence") or [])
            ],
            "proposer": proposer,
            "at": now,
        }
        proposals = strategy.proposals
        if kind == "apply" and proposal_id is not None:
            proposals = tuple(
                {
                    **item,
                    "status": "applied",
                    "applied_paths": list(applied_paths),
                    "stale_paths": list(stale_paths),
                }
                if item.get("proposal_id") == proposal_id
                else item
                for item in strategy.proposals
            )
        next_strategy = replace(
            strategy,
            updated_at=now,
            draft=revised,
            history=(*strategy.history, draft)[-_MAX_HISTORY:],
            changes=(*strategy.changes, change)[-_MAX_CHANGES:],
            proposals=proposals,
        )
        self._store.save(next_strategy)
        return next_strategy

    # -- experiment preview, launch and recovery ----------------------------

    def preview_experiment(self, user_strategy_id: object) -> dict[str, Any]:
        """What one confirmation would run, with the identity that confirmation names.

        The facts are the admitted input the engine would freeze — the real cost
        assumptions, risk and registered strategy binding included — and the
        preview identity is what a confirmation must carry back. Nothing is
        created, reserved or dispatched.
        """
        strategy = self.get(user_strategy_id)
        # A preview of an intent would describe a run that cannot exist, so this
        # refuses before the admitted input is ever derived.
        draft = _executable(strategy)
        admitted = self._admitted_request(draft)
        source = self._source_identity(admitted)
        normalized = self._normalized(admitted)
        execution = normalized.get("execution") if isinstance(normalized, dict) else None
        if not isinstance(execution, dict):
            execution = {}
        data = normalized.get("data") if isinstance(normalized, dict) else None
        return {
            "draft_revision": draft.revision,
            "strategy_id": draft.strategy_id,
            "scenario_id": draft.scenario_id,
            "venue": draft.venue,
            "symbol": draft.symbol,
            "initial_cash": draft.initial_cash,
            "parameters": dict(draft.parameters),
            "record_count": source.get("record_count"),
            "data": data,
            "input_identity": admitted.get("input_identity"),
            "input_sha256": admitted.get("input_sha256"),
            "preview_identity": self._preview_identity(draft, admitted),
            "normalized": normalized,
            # The real cost assumptions of this scenario, exactly as admitted: a
            # scenario that declares none is reported as declaring none.
            "commission": execution.get("commission"),
            "slippage": execution.get("slippage"),
            "latency": execution.get("latency"),
            "criterion": draft.criterion,
            "budget": self._workspace_budget(),
        }

    def launch_experiment(
        self,
        user_strategy_id: object,
        *,
        launch_intent_id: object,
        base_revision: object,
        preview_identity: object,
        approved_budget: Any = None,
    ) -> UserStrategy:
        identity = _identifier(user_strategy_id, "user_strategy_id")
        intent = _identifier(launch_intent_id, "launch_intent_id")
        preview = _bounded(preview_identity, "preview_identity", 128)
        if _PREVIEW_IDENTITY.fullmatch(preview) is None:
            raise UserStrategyStateError(
                "preview_identity must be the identity of an experiment preview"
            )
        if type(base_revision) is not int:
            raise UserStrategyStateError("base_revision must be an integer")
        requested = _requested_budget(approved_budget)
        with self._lock(identity):
            strategy = self._store.get(identity)
            existing = _entry(strategy.experiments, "launch_intent_id", intent)
            if existing is not None:
                # The same intent resolves to the experiment it confirmed: the whole
                # approved request is compared — revision, previewed input and the
                # exact budget — and never today's draft or today's workspace.
                retained = cast(dict[str, Any], existing.get("payload") or {})
                if (
                    retained.get("draft_revision") != base_revision
                    or retained.get("preview_identity") != preview
                    or retained.get("approved_budget") != requested
                ):
                    raise UserStrategyConflictError(
                        "this launch intent is already bound to a different confirmation"
                    )
                return strategy
            if base_revision != strategy.draft.revision:
                raise UserStrategyStaleError(
                    "the draft revision moved on; preview the experiment again"
                )
            budget = self._approval(approved_budget, model_calls=0, backtests=1)
            if len(strategy.experiments) >= _MAX_EXPERIMENTS:
                raise UserStrategyStateError("this strategy has reached its experiment limit")
            # Nothing can be launched while nothing is bound: this refuses before the
            # admitted input is derived and before any child, reservation or job.
            draft = _executable(strategy)
            # The admitted input is re-derived here, before anything durable exists:
            # a confirmation made against a preview that no longer describes what
            # would run is refused, and nothing is created or dispatched.
            admitted = self._admitted_request(draft)
            if self._preview_identity(draft, admitted) != preview:
                raise UserStrategyStaleError(
                    "the data, costs or registered scenario behind this experiment "
                    "changed since it was previewed; preview it again and confirm the new one"
                )
            source = self._source_identity(admitted)
            normalized = self._normalized(admitted)
            payload = {
                "draft_revision": draft.revision,
                "strategy_id": draft.strategy_id,
                "venue": draft.venue,
                "symbol": draft.symbol,
                "scenario_id": draft.scenario_id,
                "dataset_id": source.get("dataset_id"),
                "source_sha256": source.get("source_sha256"),
                "initial_cash": draft.initial_cash,
                "parameters": dict(draft.parameters),
                "criterion": draft.criterion,
                "record_count": source.get("record_count"),
                "data": normalized.get("data"),
                # The admitted snapshot's own identity and digest, kept so the
                # confirmed input is still readable after its source disappears,
                # and the normalized blocks the user saw in the preview.
                "input_identity": admitted.get("input_identity"),
                "input_sha256": admitted.get("input_sha256"),
                # The whole admitted input, exactly as the engine rendered it:
                # schema, scenario id, source identity, normalized identity, the
                # full canonical scenario and any research input. The child this
                # confirmation creates is held to this document, so a later read of
                # the same scenario can never run in its place.
                "snapshot": admitted.get("snapshot"),
                "preview_identity": preview,
                "normalized": normalized,
                "approved_budget": budget,
                "frozen_at": _now(),
            }
            record = {
                "launch_intent_id": intent,
                "request_id": str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"ea.user-strategy/{identity}/{intent}")
                ),
                "payload_sha256": _digest(payload),
                # The immutable payload itself, so the same intent is always compared
                # to what it froze, never to today's draft or today's settings.
                "payload": payload,
                "task_id": None,
                "job_id": None,
                "status": "pending",
                "message": None,
                "created_at": _now(),
            }
            # Durable before any child exists and before any dispatch.
            pending = replace(
                strategy, updated_at=_now(), experiments=(*strategy.experiments, record)
            )
            self._store.save(pending)
            try:
                task = self._research.create_owned_experiment(
                    user_strategy_id=identity,
                    launch_intent_id=intent,
                    request_id=cast(str, record["request_id"]),
                    spec=self._spec(draft),
                    payload=payload,
                    budget=budget,
                )
            except ResearchTaskError as error:
                # A validated refusal happened before any child existed.
                return self._settle_experiment(pending, intent, "failed", None, None, str(error))
            status, task_id, job_id, message = self._child_outcome(task)
            return self._settle_experiment(pending, intent, status, task_id, job_id, message)

    @staticmethod
    def _child_outcome(
        task: ResearchTask,
    ) -> tuple[str, str | None, str | None, str | None]:
        """The durable status of one dispatched child, read from the child itself.

        A child that produced a job is created; one whose durable dispatch may have
        left this process is unknown and is never re-sent or refunded; one that
        failed before any dispatch is a real failure with the child's own message;
        one that never dispatched is interrupted before dispatch. Nothing here is
        inferred from the absence of a job.
        """
        if task.jobs:
            return "created", task.task_id, task.jobs[-1], None
        message = None if task.error is None else task.error.get("message")
        reservation = task.backtest_reservation
        if reservation is not None and reservation.status == "dispatched":
            return "unknown", task.task_id, None, message
        if task.status == "failed":
            return "failed", task.task_id, None, message
        return "interrupted_before_dispatch", task.task_id, None, message

    @staticmethod
    def _spec(draft: StrategyDraft) -> ResearchSpec:
        return ResearchSpec(
            strategy_id=draft.strategy_id,
            instrument_venue=draft.venue,
            instrument_symbol=draft.symbol,
            initial_cash=draft.initial_cash,
            rationale="Confirmed draft of one user strategy.",
            scenario_id=draft.scenario_id,
            dataset_id=draft.dataset_id,
            source_sha256=draft.source_sha256,
            parameters=dict(draft.parameters),
            criterion=draft.criterion,
        )

    def _settle_experiment(
        self,
        strategy: UserStrategy,
        intent: str,
        status: str,
        task_id: str | None,
        job_id: str | None,
        message: str | None,
    ) -> UserStrategy:
        now = _now()
        next_strategy = replace(
            strategy,
            updated_at=now,
            experiments=tuple(
                {
                    **item,
                    "status": status,
                    "task_id": task_id if task_id is not None else item.get("task_id"),
                    "job_id": job_id if job_id is not None else item.get("job_id"),
                    "message": message,
                }
                if item.get("launch_intent_id") == intent
                else item
                for item in strategy.experiments
            ),
        )
        self._store.save(next_strategy)
        return next_strategy

    def reconcile(self, user_strategy_id: object) -> UserStrategy:
        """Classify interrupted experiments and calls; dispatch and send nothing.

        A call or an experiment that already reached a terminal status is the
        record of what happened and is never rewritten here. A durable child or
        job this owner can read may be rebound to the record that names it, but no
        new run and no new call is ever started by recovery.
        """
        identity = _identifier(user_strategy_id, "user_strategy_id")
        with self._lock(identity):
            strategy = self._store.get(identity)
            changed = False
            experiments: Documents = []
            for record in strategy.experiments:
                settled = self._reconciled(record)
                changed = changed or settled != record
                experiments.append(settled)
            calls: Documents = []
            for call in strategy.calls:
                settled_call = self._reconciled_call(strategy.user_strategy_id, call)
                changed = changed or settled_call != call
                calls.append(settled_call)
            if not changed:
                return strategy
            next_strategy = replace(strategy, experiments=tuple(experiments), calls=tuple(calls))
            self._store.save(next_strategy)
            return next_strategy

    def reconcile_all(self) -> Strategies:
        reconciled: Strategies = []
        for strategy in self._store.list():
            try:
                reconciled.append(self.reconcile(strategy.user_strategy_id))
            except (UserStrategyError, OSError):
                reconciled.append(strategy)
        return reconciled

    def _reconciled_call(self, strategy_id: str, record: dict[str, Any]) -> dict[str, Any]:
        """Classify one call that was never observed. It is never re-sent or refunded.

        ``pending`` is durable before the provider could have been reached, so an
        interruption there proves nothing left this process. ``dispatched`` is
        durable before the provider is called, so an interruption after it leaves an
        outcome this owner will not call absent — and no usage is ever claimed for a
        call whose outcome was not observed. A call that is in flight in this
        process right now is not an orphan and is left exactly as it is.
        """
        status = record.get("status")
        if status not in {"pending", "dispatched"}:
            return record
        if (strategy_id, str(record.get("call_id"))) in self._active_calls:
            return record
        if status == "pending":
            return {
                **record,
                "status": "interrupted_before_dispatch",
                "message": (
                    "the interruption happened before the provider was reached; nothing "
                    "was sent and no usage is claimed for this call"
                ),
            }
        return {
            **record,
            "status": "unknown",
            "message": (
                "a call may have reached the provider before the interruption; it was "
                "not re-sent and no usage is claimed for it"
            ),
        }

    @staticmethod
    def _job_matches_confirmation(record: dict[str, Any], job: Any) -> bool:
        """Whether a durable job is the run this launch's confirmed input was frozen for.

        The record's own retained snapshot is the authority, and it is compared
        with the job's own frozen snapshot: the document it would run, the digest
        recomputed from that document, the request identity it was published under
        and the source identity it was admitted with must all be the confirmed
        ones. A stored block edited under a retained digest, a partial document,
        or a run of some other input found by request identity is not this run; a
        launch that kept no readable snapshot cannot be matched at all, so a job
        found by request identity alone is never rebound as if it were that run.
        """
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return False
        snapshot = read_input_snapshot(payload.get("snapshot"))
        digest = payload.get("input_sha256")
        request_id = record.get("request_id")
        if snapshot is None or type(digest) is not str or type(request_id) is not str:
            return False
        stored = getattr(job, "input_snapshot_bytes", None)
        document: object = None
        if type(stored) is bytes:
            try:
                document = json.loads(stored)
            except ValueError:
                document = None
        frozen = read_input_snapshot(document)
        if frozen is None or frozen != snapshot:
            return False
        return bool(
            getattr(job, "request_id", None) == request_id
            and getattr(job, "input_sha256", None) == digest
            and getattr(job, "input_identity", None) == snapshot.get("source_identity")
            and input_snapshot_sha256(frozen) == digest
        )

    def _bound_job_matches(self, record: dict[str, Any], job_id: str) -> bool | None:
        """Whether the job a child bound is this launch's run; ``None`` if unreadable.

        The job is read back from the live index rather than from the child's own
        claim about it, so a run that was replaced, edited or lost after it was
        bound is judged by what it actually stores now.
        """
        try:
            job = self._service.get_job(job_id)
        except Exception:  # noqa: BLE001 — an unreadable job proves nothing
            return None
        return self._job_matches_confirmation(record, job)

    def _reconciled(self, record: dict[str, Any]) -> dict[str, Any]:
        """Read the durable child and job index; dispatch and create nothing.

        Only a launch that never settled is classified here. An unreadable job
        index proves no absence, so a record whose index cannot be read is unknown
        rather than wrongly declared never-dispatched.
        """
        if record.get("status") != "pending":
            return record
        request_id = record.get("request_id")
        if not isinstance(request_id, str):
            return record
        try:
            task = self._research.owned_experiment(request_id)
        except (ResearchTaskError, OSError):
            return record
        if task is not None:
            status, task_id, job_id, message = self._child_outcome(task)
            if job_id is not None:
                # The child says it produced this run, but the run itself is read
                # back and held to the input this launch retained — an earlier bind
                # is not evidence after a restart. A job that cannot be read at all
                # proves nothing either way, so it is unknown, never created.
                matches = self._bound_job_matches(record, job_id)
                if matches is None:
                    return {
                        **record,
                        "status": "unknown",
                        "task_id": task_id,
                        "message": (
                            "a run is bound to this experiment but could not be read back, "
                            "so what it ran cannot be proven here; nothing was re-sent"
                        ),
                    }
                if not matches:
                    return {
                        **record,
                        "status": "failed",
                        "task_id": task_id,
                        "job_id": None,
                        "message": (
                            "the run found for this launch is not the input that was "
                            "confirmed; preview the experiment again and confirm the new one"
                        ),
                    }
                return {
                    **record,
                    "status": status,
                    "task_id": task_id,
                    "job_id": job_id,
                    "message": message,
                }
            if status in {"failed", "unknown"}:
                return {**record, "status": status, "task_id": task_id, "message": message}
        job: Any = None
        readable = True
        try:
            job = self._service.lookup_job_by_request_id(request_id)
        except JobNotFoundError:
            job = None
        except Exception:  # noqa: BLE001 — an unreadable index proves nothing
            readable = False
        if job is not None:
            if not self._job_matches_confirmation(record, job):
                # The job index answers with a run of some other input under this
                # request identity. Binding it would present a run the user never
                # confirmed as this confirmation's own, so it is refused instead.
                return {
                    **record,
                    "status": "failed",
                    "task_id": None if task is None else task.task_id,
                    "job_id": None,
                    "message": (
                        "the run found for this launch is not the input that was confirmed; "
                        "preview the experiment again and confirm the new one"
                    ),
                }
            return {
                **record,
                "status": "created",
                "task_id": None if task is None else task.task_id,
                "job_id": job.job_id,
                "message": None,
            }
        if not readable:
            return {
                **record,
                "status": "unknown",
                "task_id": None if task is None else task.task_id,
                "message": (
                    "the job index could not be read, so whether a run was dispatched "
                    "cannot be proven either way; nothing was re-sent"
                ),
            }
        if task is not None:
            reservation = task.backtest_reservation
            if reservation is not None and reservation.status == "dispatched":
                # The dispatch may have left this process: never re-sent, never refunded.
                return {
                    **record,
                    "status": "unknown",
                    "task_id": task.task_id,
                    "message": (
                        "a run may have been dispatched before the interruption; it was not "
                        "re-sent and is not counted as one of your experiments until it is found"
                    ),
                }
            return {
                **record,
                "status": "interrupted_before_dispatch",
                "task_id": task.task_id,
                "message": (
                    "the interruption happened before any run was dispatched; nothing was "
                    "started and no duplicate was created"
                ),
            }
        return {
            **record,
            "status": "interrupted_before_dispatch",
            "message": (
                "the interruption happened before any run was dispatched; nothing was "
                "started and no duplicate was created"
            ),
        }
