"""One durable user strategy: a saved draft, approved AI calls, typed diffs, frozen runs.

This module is a thin owner, not an engine. It reuses the registered scenario catalog
through :class:`WebService`, the frozen child experiment and its dispatch path through
:class:`ResearchOrchestrator`, and the workspace's model port through
:class:`ea.web.llm`. It adds no scheduler and no new financial authority:

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

Everything durable lives under ``workspace/user-strategies``. A strategy starts as one
JSON record — messages, calls, proposals, change history and experiment references —
written with the same atomic-publish discipline the research store uses. One strategy at
a time may be migrated into the workspace's one conversation database, which then holds
its complete document and the workspace's model-call reservations; that database is a
storage owner for records this module already owns, never a second authority over the
draft, the runs or the budget.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

import langsmith
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ea.web.conversation import (
    DONE,
    ERROR,
    INTERRUPT,
    STATE,
    TEXT_DELTA,
    TOOL_END,
    TOOL_ERROR,
    TOOL_START,
    ReservationConflict,
    ReservationLimit,
    checkpoints,
    event,
    reserve_call,
    thread_config,
)
from ea.web.llm import (
    ModelMessage,
    ModelNotConfigured,
    ModelPort,
    ModelProviderError,
    ModelResponse,
    ModelRoute,
    StreamingModelPort,
    ToolCall,
    ToolDefinition,
    create_model_port,
)
from ea.web.local_packages import LOCAL_SCENARIO_ID
from ea.web.quine import (
    CONFIRMATION_SCHEMA,
    QuineBridge,
    QuineError,
    QuineFrozen,
    QuineRefused,
    payload_digest,
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
_MAX_DATASET_ID = 128
_MAX_REASON = 400
_MAX_EVIDENCE_ITEM = 400
_MAX_EVIDENCE = 8
#: How many frozen runs one turn may be grounded in: A, and optionally B. A pair is
#: the widest reading this product makes, and a third run would stop being a pair.
_MAX_EVIDENCE_RUNS = 2
_MAX_ITEMS = 20
#: Legacy JSON record bounds; migrated conversations retain their complete history.
_MAX_MESSAGES = 200
_MAX_CALLS = 200
_MAX_PROPOSALS = 100
_MAX_CHANGES = 200
_MAX_EXPERIMENTS = 50
_MAX_HISTORY = 20
#: How much recorded conversation one request may carry: a deterministic suffix of
#: complete prior turns, bounded by both a turn count and a character budget. Older
#: turns are dropped from the request only; every stored record survives.
_MAX_HISTORY_TURNS = 8
_MAX_HISTORY_CHARS = 24000
_MAX_TOKENS = 1200
#: One refinement approval is exactly one call; a hidden second call is never spent.
_CALL_APPROVAL = 1
#: The most model calls one explicitly initiated new conversation turn may make.
#: This is a per-turn bound, not a workspace-lifetime one: a workspace that has
#: made many calls still admits the next turn up to this many, and no further.
_TURN_MAX_CALLS = 4  # Legacy explicit approvals only.
# Each new interactive turn and explicit continuation grants at most 24 requests.
# Persisted approvals keep their original amounts; reaching the limit pauses,
# and continuation resumes saved tool results without resending unknown calls.
_INTERACTIVE_MAX_DISPATCHES = 24
_DEFAULT_BUDGET = {"max_model_calls": 10, "max_backtests": 5}
#: What a request says when the bounded history dropped older turns from it.
_OMITTED_HISTORY_NOTE = (
    "Earlier turns of this conversation were omitted from this request so it stays "
    "bounded. This workspace still records every one of them; only the most recent "
    "complete turns are stated here."
)
#: The one conversation database per workspace. It is created lazily by the first
#: migrated write; a workspace that never migrates keeps one JSON record per
#: strategy and no database at all.
_CONVERSATION_DB = "conversations.sqlite3"
#: Wait for a concurrent writer instead of failing immediately. Both this store
#: and the graph checkpoint saver open the same file, each with its own connection.
_SQLITE_BUSY_TIMEOUT_MS = 5000
#: The call statuses that prove a call may have reached the provider, and are
#: therefore reserved once and never refunded. A call that was never dispatched —
#: no provider configured, or interrupted before dispatch — proves no network and
#: is not counted.
_RESERVED_CALL_STATUSES = frozenset({"pending", "dispatched", "observed", "refused", "unknown"})
#: Plain validated JSON only: every column below is either an identity or a JSON
#: document. There is no pickle and no second schema authority.
_CONVERSATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_documents (
    strategy_id TEXT PRIMARY KEY,
    document TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_call_reservations (
    strategy_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    request TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    PRIMARY KEY (strategy_id, call_id)
);
"""
#: The one owner marker an experiment carries when the Rust core owns it. A record
#: without it keeps the original in-process owner, so no experiment ever has two.
_QUINE_OWNER = "quine"
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
    "you say is a verified result: say plainly that a run is needed. Use discover_resources "
    "and inspect_research_input to check available inputs before proposing changes. "
    "Read only the selected official experiments with read_experiments; model-generated "
    "comparisons or statistics are not host evidence."
)
#: What a conversation is told when the strategy has no scenario yet. There is no
#: draft to describe and nothing to change, so this turn clarifies the intent only.
_INTENT_SYSTEM_PROMPT = (
    "You are the research assistant inside one local backtest workspace. This strategy "
    "is an unbound idea. Use discover_resources to find supported templates and actual "
    "catalogued data, and inspect_research_input to validate a specific pairing. These "
    "are read-only tools: they never bind a scenario, import data, edit or run anything. "
    "Do not substitute built-in sample data for missing data. Explain missing inputs "
    "and ask the user to choose a binding through the workspace before proposing changes."
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
    # One canonical rule for this workspace's identities, shared with the bridge
    # that reads a confirmation payload back out of the owner's frozen bytes.
    return payload_digest(payload)


def _entry(items: tuple[dict[str, Any], ...], key: str, value: str) -> dict[str, Any] | None:
    for item in items:
        if item.get(key) == value:
            return item
    return None


#: The tools a conversation may ask for, and nothing else. Each one reads or proposes
#: inside this workspace's own authorities: no filesystem, no URL, no code execution,
#: no live, Paper or broker tool, and no tool a conversation can add.
_DESCRIBE_DATA_TOOL = "describe_data"
_EXPERIMENT_STATUS_TOOL = "experiment_status"
_DISCOVER_TOOL = "discover_resources"
_INSPECT_INPUT_TOOL = "inspect_research_input"
_READ_EXPERIMENTS_TOOL = "read_experiments"
_DOMAIN_READ_TOOLS = {_DISCOVER_TOOL, _INSPECT_INPUT_TOOL, _READ_EXPERIMENTS_TOOL}
#: The one instruction that turns verified readings into an answer. It grants nothing:
#: the readings it names are the host's own, and the model is told to state no more.
_EXPLAIN_INSTRUCTION = (
    "Now answer the user's own question in plain language, using only the host-verified "
    "readings above and naming the limitations this workspace states. State nothing that "
    "is not in those readings, and do not claim any run, statistic or file that is not "
    "among them."
)
#: What a paused conversation is waiting for. The identity fields beside each phase are
#: the exact business identities a resuming decision has to name.
_PHASE_PROPOSAL = "awaiting_proposal"
_PHASE_CONFIRMATION = "awaiting_confirmation"
_PHASE_OBSERVATION = "awaiting_observation"


def _child_call_id(turn_id: str, step: int) -> str:
    """A further model call of one turn: deterministic, so a replay names the same call."""
    return f"{turn_id[:40]}-s{step}-{_digest({'turn': turn_id, 'step': step})[:12]}"


def _response_document(response: ModelResponse) -> dict[str, Any]:
    """One response as plain JSON, with its tool identities and arguments kept."""
    return {
        "text": response.text,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in response.tool_calls
        ],
        "stop_reason": response.stop_reason,
        "model": response.model,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
    }


def _response_from(document: Any) -> ModelResponse | None:
    """The response a call ledger recorded, or ``None`` when it recorded none."""
    if type(document) is not dict:
        return None
    reason = document.get("stop_reason")
    if reason not in {"stop", "tool_calls"}:
        return None
    raw = document.get("tool_calls") or []
    if type(raw) is not list or not isinstance(document.get("text"), str):
        return None
    calls: list[ToolCall] = []
    for item in raw:
        if type(item) is not dict or type(item.get("arguments")) is not dict:
            return None
        identifier, name = item.get("id"), item.get("name")
        if not isinstance(identifier, str) or not isinstance(name, str):
            return None
        calls.append(ToolCall(identifier, name, dict(item["arguments"])))
    return ModelResponse(
        document["text"],
        tuple(calls),
        cast(Any, reason),
        str(document.get("model") or ""),
        document.get("input_tokens"),
        document.get("output_tokens"),
    )


def _canonical_json(payload: object) -> str:
    """One canonical text for one value, so two requests compare as themselves."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _is_canonical_conversation(legacy: Any, strategy: UserStrategy) -> bool:
    """The re-encoded document still carries the whole conversation it was read from.

    Every message, call and proposal is compared as it stands — its identity, its
    place in the order, its role and the call and proposal it belongs to — and the
    canonical form is proved to decode and re-encode to itself, so what is
    committed is the record that was read rather than a lossy rewrite of it.
    """
    canonical = strategy.document()
    if UserStrategy.decode(json.loads(_canonical_json(canonical))).document() != canonical:
        return False
    return all(
        canonical[field] == legacy.get(field)
        for field in ("user_strategy_id", "messages", "calls", "proposals")
    )


def _task_usage(strategy: UserStrategy, turn: str) -> int:
    return sum(
        1
        for item in strategy.calls
        if (item.get("call_id") == turn or (item.get("request") or {}).get("turn_id") == turn)
        and item.get("status") in _RESERVED_CALL_STATUSES
    )


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
    strategy_binding: dict[str, Any] | None = None

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
            **(
                {} if self.strategy_binding is None else {"strategy_binding": self.strategy_binding}
            ),
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
            strategy_binding=document.get("strategy_binding"),
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


@dataclass(frozen=True, slots=True)
class _AdmittedTurn:
    """One turn that may reach the provider, with everything it was admitted under.

    It is the result of every check and every durable record a turn needs before it
    may dispatch: the strategy as it stands with its own pending call and message,
    the identity of that call, the question being asked and the budget this turn was
    granted. A turn that has nothing to dispatch — a replayed identity, or a
    workspace with no provider — is returned as its own strategy instead.
    """

    strategy: UserStrategy
    call_id: str
    message: str
    base_revision: int
    approved_budget: dict[str, int]
    evidence_job_ids: list[str]


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
    #: The last explicit dataset selection, kept for the same reason: one request
    #: record, so replaying the same operation across a restart resolves to the
    #: revision it already produced instead of selecting a second time.
    dataset: dict[str, Any] | None = None
    #: The one decision a conversation is waiting for right now, if it is waiting for
    #: one: the phase, and the exact business identities that decision names. The
    #: graph holds this same reference in its own checkpoint, and this record is the
    #: one a reloaded workspace reads — a checkpoint is never the only copy of a
    #: decision the user still has to make.
    conversation_confirmation: dict[str, Any] | None = None
    model_profile_id: str | None = None
    exchange_id: str | None = None

    def document(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "user_strategy_id": self.user_strategy_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "name": self.name,
            "intent": self.intent,
            "exchange_id": self.exchange_id,
            **({"model_profile_id": self.model_profile_id} if self.model_profile_id else {}),
            "draft": self.draft.document(),
            "history": [item.document() for item in self.history],
            "messages": [dict(item) for item in self.messages],
            "calls": [dict(item) for item in self.calls],
            "proposals": [dict(item) for item in self.proposals],
            "changes": [dict(item) for item in self.changes],
            "experiments": [dict(item) for item in self.experiments],
            "binding": None if self.binding is None else dict(self.binding),
            "dataset": None if self.dataset is None else dict(self.dataset),
            # Absent until a conversation waits for a decision, so every record
            # written before this field existed stays exactly the record it was.
            **(
                {}
                if self.conversation_confirmation is None
                else {"conversation_confirmation": dict(self.conversation_confirmation)}
            ),
        }

    @staticmethod
    def decode(document: Any) -> UserStrategy:
        if type(document) is not dict or document.get("schema") != SCHEMA:
            raise UserStrategyError("user strategy schema is not supported")
        binding = document.get("binding")
        dataset = document.get("dataset")
        confirmation = document.get("conversation_confirmation")
        return UserStrategy(
            user_strategy_id=str(document["user_strategy_id"]),
            created_at=str(document["created_at"]),
            updated_at=str(document["updated_at"]),
            name=str(document["name"]),
            intent=str(document["intent"]),
            model_profile_id=document.get("model_profile_id"),
            exchange_id=document.get("exchange_id"),
            draft=_decode_draft(document["draft"]),
            history=tuple(_decode_draft(item) for item in document.get("history") or []),
            messages=tuple(dict(item) for item in document.get("messages") or []),
            calls=tuple(dict(item) for item in document.get("calls") or []),
            proposals=tuple(dict(item) for item in document.get("proposals") or []),
            changes=tuple(dict(item) for item in document.get("changes") or []),
            experiments=tuple(dict(item) for item in document.get("experiments") or []),
            binding=None if binding is None else dict(binding),
            dataset=None if dataset is None else dict(dataset),
            conversation_confirmation=None
            if not isinstance(confirmation, dict)
            else dict(confirmation),
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
Tools = list[ToolDefinition]


class UserStrategyStore:
    """One durable strategy per identity, from the one authority that owns it.

    A workspace starts as one JSON record per strategy, published atomically.
    ``migrate_conversation`` imports one record's complete legacy document into the
    workspace's conversation database exactly once; only a committed row then
    selects that database as the read and write authority for that strategy, and
    the JSON record stays behind as the untouched pre-migration source. A strategy
    that was never migrated keeps its JSON record as its only authority, so no
    strategy is ever written to two places at once.
    """

    def __init__(self, workspace: Path):
        self._dir = Path(workspace) / "user-strategies"

    @property
    def conversation_db_path(self) -> Path:
        """The one conversation database of this workspace, created lazily."""
        return self._dir / _CONVERSATION_DB

    def _path(self, user_strategy_id: str) -> Path:
        return self._dir / f"{user_strategy_id}.json"

    # -- the conversation database ------------------------------------------

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """One short-lived read connection, closed by leaving the block."""
        connection = sqlite3.connect(self.conversation_db_path, timeout=5.0)
        try:
            connection.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One immediate transaction, committed only when the body returns."""
        self._dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.conversation_db_path, timeout=5.0, isolation_level=None)
        try:
            connection.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
            connection.executescript(_CONVERSATION_SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        finally:
            connection.close()

    def _document(self, connection: sqlite3.Connection, identity: str) -> UserStrategy | None:
        """The committed document of one migrated strategy, or ``None`` if it has none.

        A row this workspace cannot read, or one whose own identity disagrees with
        the identity it is filed under, is refused outright: the JSON record it was
        migrated from is a pre-migration source, never a silent fallback.
        """
        row = connection.execute(
            "SELECT document FROM strategy_documents WHERE strategy_id = ?", (identity,)
        ).fetchone()
        if row is None:
            return None
        try:
            item = UserStrategy.decode(json.loads(str(row[0])))
        except (ValueError, TypeError, KeyError) as error:
            if isinstance(error, UserStrategyError):
                raise
            raise UserStrategyError("stored user strategy is unreadable") from None
        if item.user_strategy_id != identity:
            raise UserStrategyError("stored user strategy is unreadable")
        return item

    def _publish(self, connection: sqlite3.Connection, strategy: UserStrategy) -> None:
        """Write this document and read it back as itself before the commit."""
        canonical = strategy.document()
        connection.execute(
            "INSERT INTO strategy_documents (strategy_id, document, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(strategy_id) DO UPDATE SET "
            "document = excluded.document, updated_at = excluded.updated_at",
            (strategy.user_strategy_id, _canonical_json(canonical), canonical["updated_at"]),
        )
        stored = connection.execute(
            "SELECT document FROM strategy_documents WHERE strategy_id = ?",
            (strategy.user_strategy_id,),
        ).fetchone()
        if stored is None or json.loads(str(stored[0])) != canonical:
            raise UserStrategyError("the conversation was not stored as it was read")

    def _legacy(self, identity: str) -> Any:
        """This identity's pre-migration JSON document, or ``None`` if it has none."""
        try:
            payload = self._path(identity).read_bytes()
        except OSError:
            return None
        try:
            return json.loads(payload)
        except ValueError:
            raise UserStrategyError("stored user strategy is unreadable") from None

    def _seed_reservations(self, connection: sqlite3.Connection, strategy: UserStrategy) -> None:
        """Reserve every historical call that may already have reached the provider."""
        for record in strategy.calls:
            status = record.get("status")
            if status not in _RESERVED_CALL_STATUSES:
                continue
            request = record.get("request")
            stored = _canonical_json(request if isinstance(request, dict) else {})
            connection.execute(
                "INSERT OR IGNORE INTO model_call_reservations "
                "(strategy_id, call_id, request, payload_sha256, status, reserved_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    strategy.user_strategy_id,
                    str(record.get("call_id")),
                    stored,
                    str(record.get("payload_sha256") or _digest(request)),
                    str(status),
                    str(record.get("reserved_at") or strategy.created_at),
                ),
            )

    def is_conversation_migrated(self, user_strategy_id: str) -> bool:
        """Whether this identity is read from the conversation database."""
        if not isinstance(user_strategy_id, str) or _SAFE_ID.fullmatch(user_strategy_id) is None:
            return False
        if not self.conversation_db_path.exists():
            return False
        with self._read() as connection:
            row = connection.execute(
                "SELECT 1 FROM strategy_documents WHERE strategy_id = ?", (user_strategy_id,)
            ).fetchone()
        return row is not None

    def migrate_conversation(self, user_strategy_id: str) -> UserStrategy:
        """Import one legacy conversation once, and never import it twice.

        The complete legacy document — every message, call and proposal with its
        identity, its order, its role and the relations between them — is decoded,
        re-encoded and compared before it is written, and the committed row is read
        back before the transaction ends, so what becomes the authority is the
        record that was read. Repeating the call is a no-op that returns the
        committed strategy, and the source is not even read once a row is committed,
        so an archived JSON that has since been deleted, damaged or rewritten can
        neither block nor alter the migration; a committed row this workspace cannot
        read fails closed instead of being replaced. A source whose own identity is
        not the requested one is refused before publication. Historical calls that
        may have reached the provider are reserved in the same transaction.
        """
        identity = _identifier(user_strategy_id, "user_strategy_id")
        with self._write() as connection:
            stored = self._document(connection, identity)
            if stored is not None:
                # Already migrated. The committed row is the authority, so the
                # archived JSON is never read again: a source that has since been
                # deleted, damaged or rewritten cannot block or alter this.
                return stored
            legacy = self._legacy(identity)
            if legacy is None:
                raise UserStrategyNotFoundError("user strategy was not found")
            strategy = UserStrategy.decode(legacy)
            if strategy.user_strategy_id != identity:
                # One identity's record may never be published under another's.
                raise UserStrategyError("stored user strategy is unreadable")
            if not _is_canonical_conversation(legacy, strategy):
                raise UserStrategyError("the legacy conversation is not readable as itself")
            self._publish(connection, strategy)
            self._seed_reservations(connection, strategy)
        return strategy

    def reserve_model_call(
        self, strategy: UserStrategy, call_id: str, max_model_calls: int | None = None
    ) -> dict[str, Any]:
        """Reserve one workspace model call, and publish the strategy, atomically.

        ``strategy`` carries the pending record of this call, so the reservation and
        that record are committed together or not at all: a call that may reach the
        provider is never accounted for without being recorded, and never recorded
        without being accounted for. The count of reservations is the workspace's
        recorded usage, kept for accounting and exact replay.

        ``max_model_calls`` is an optional caller-owned ceiling for a legacy or
        bounded caller, counting every reservation this workspace has ever made —
        including the ones a migration imported. The interactive conversation path
        passes none: a user conversation is bounded per turn, never by the
        workspace's all-time count, so a recorded history can never permanently stop
        a new turn. An identity that is already reserved is compared against its
        stored canonical request: a different request is a conflict, and the same one
        returns the stored reservation unchanged rather than spending again.
        """
        if not self.conversation_db_path.exists():
            raise UserStrategyStateError(
                "this conversation has not been migrated to the conversation database"
            )
        record = _entry(strategy.calls, "call_id", call_id)
        if record is None:
            raise UserStrategyStateError("this call is not part of the strategy")
        request = record.get("request")
        canonical = _canonical_json(request if isinstance(request, dict) else {})
        with self._write() as connection:
            if self._document(connection, strategy.user_strategy_id) is None:
                raise UserStrategyStateError(
                    "this conversation has not been migrated to the conversation database"
                )
            current = self._document(connection, strategy.user_strategy_id)
            assert current is not None
            turn = str((request or {}).get("turn_id") or call_id)
            root = _entry(current.calls, "call_id", turn) or record
            task = root.get("task")
            if task and _entry(current.calls, "call_id", call_id) is None:
                if (
                    task["stop_reason"] == "cancelled"
                    or _task_usage(current, turn) >= task["limit"]
                ):
                    raise UserStrategyBudgetError(
                        "task has no approved requests left or was cancelled"
                    )
                if any(
                    item["call_id"] not in {c["call_id"] for c in strategy.calls}
                    for item in current.calls
                ):
                    raise UserStrategyConflictError("conversation changed before reservation")
            try:
                result = reserve_call(
                    connection,
                    strategy.user_strategy_id,
                    call_id,
                    canonical,
                    str(record.get("payload_sha256") or _digest(request)),
                    str(record.get("status") or "pending"),
                    str(record.get("reserved_at") or strategy.updated_at),
                    ceiling=max_model_calls,
                )
            except ReservationConflict as error:
                raise UserStrategyConflictError(str(error)) from None
            except ReservationLimit as error:
                raise UserStrategyBudgetError(str(error)) from None
            if not result["replayed"]:
                self._publish(connection, strategy)
        return result

    def model_call_usage(self) -> dict[str, Any]:
        """Read-only: the model calls this workspace has reserved, and where."""
        if not self.conversation_db_path.exists():
            return {"reserved_model_calls": 0, "strategies": {}}
        with self._read() as connection:
            rows = connection.execute(
                "SELECT strategy_id, COUNT(*) FROM model_call_reservations GROUP BY strategy_id"
            ).fetchall()
        counts = {str(row[0]): int(row[1]) for row in rows}
        return {"reserved_model_calls": sum(counts.values()), "strategies": counts}

    # -- the one authority a strategy has -----------------------------------

    def _save_json(self, strategy: UserStrategy) -> None:
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

    def update_task(
        self,
        identity: str,
        turn: str,
        *,
        status: str | None = None,
        reason: str | None = None,
        grant_id: str | None = None,
        total: int | None = None,
        continue_execution: bool = False,
    ) -> UserStrategy:
        """Change a root-call grant/status in the existing document transaction."""
        with self._write() as connection:
            strategy = self._document(connection, identity)
            if strategy is None:
                raise UserStrategyStateError("conversation not found")
            root = _entry(strategy.calls, "call_id", turn)
            if root is None or not isinstance(root.get("task"), dict):
                raise UserStrategyBudgetError(
                    "legacy approval cannot be upgraded silently; start a new task"
                )
            task = dict(root["task"])
            if grant_id is not None:
                grants = list(task["grants"])
                existing = _entry(tuple(grants), "grant_id", grant_id)
                if existing is not None:
                    if (continue_execution and existing.get("action") != "continue") or (
                        not continue_execution and existing["total"] != total
                    ):
                        raise UserStrategyConflictError("grant_id is bound to a different total")
                    return strategy
                if task["status"] != "awaiting_budget":
                    raise UserStrategyBudgetError(
                        "only an exhausted task can receive additional requests"
                    )
                if continue_execution:
                    members = [
                        c
                        for c in strategy.calls
                        if c["call_id"] == turn or (c.get("request") or {}).get("turn_id") == turn
                    ]
                    if any(c["status"] in {"unknown", "dispatched", "pending"} for c in members):
                        raise UserStrategyStateError("an unresolved dispatch cannot be continued")
                    total = _task_usage(strategy, turn) + _INTERACTIVE_MAX_DISPATCHES
                    task["mode"] = "interactive"
                if (
                    type(total) is not int
                    or total <= task["limit"]
                    or total > _task_usage(strategy, turn) + _INTERACTIVE_MAX_DISPATCHES
                    or (not continue_execution and total > 100)
                ):
                    raise UserStrategyBudgetError(
                        "a new grant must increase the task total by at most 24 requests"
                    )
                task.update(
                    limit=total,
                    grants=[
                        *grants,
                        {
                            "grant_id": grant_id,
                            "total": total,
                            "at": _now(),
                            **({"action": "continue"} if continue_execution else {}),
                        },
                    ],
                )
            elif task["stop_reason"] == "cancelled":
                return strategy
            if status is not None:
                task.update(status=status, stop_reason=reason)
            calls = tuple(
                {**item, "task": task} if item["call_id"] == turn else item
                for item in strategy.calls
            )
            pending = strategy.conversation_confirmation
            if task["stop_reason"] == "cancelled" and pending and pending.get("turn_id") == turn:
                pending = None
            updated = replace(
                strategy, calls=calls, conversation_confirmation=pending, updated_at=_now()
            )
            self._publish(connection, updated)
            return updated

    def dispatch_task_call(self, identity: str, call: str) -> UserStrategy:
        """Cancellation and dispatch serialize on the same SQLite transaction."""
        with self._write() as connection:
            strategy = self._document(connection, identity)
            if strategy is None:
                raise UserStrategyStateError("conversation not found")
            record = _entry(strategy.calls, "call_id", call) or {}
            turn = str((record.get("request") or {}).get("turn_id") or call)
            root = _entry(strategy.calls, "call_id", turn) or {}
            if (root.get("task") or {}).get("stop_reason") == "cancelled":
                raise UserStrategyBudgetError("task cancelled before dispatch")
            if record.get("status") != "pending":
                raise UserStrategyStateError("call already dispatched; it cannot be sent again")
            updated = replace(
                strategy,
                updated_at=_now(),
                calls=tuple(
                    {**item, "status": "dispatched", "dispatched_at": _now()}
                    if item["call_id"] == call
                    else item
                    for item in strategy.calls
                ),
            )
            self._publish(connection, updated)
            return updated

    def _update_conversation(self, strategy: UserStrategy) -> bool:
        """Write the committed row, or report that this strategy has none."""
        if not self.conversation_db_path.exists():
            return False
        with self._write() as connection:
            current = self._document(connection, strategy.user_strategy_id)
            if current is None:
                return False
            # A late provider result may not overwrite a concurrent cancel/topup.
            tasks = {item["call_id"]: item["task"] for item in current.calls if "task" in item}
            calls = tuple(
                {**item, "task": tasks[item["call_id"]]} if item["call_id"] in tasks else item
                for item in strategy.calls
            )
            pending = strategy.conversation_confirmation
            if (
                pending
                and (tasks.get(pending.get("turn_id")) or {}).get("stop_reason") == "cancelled"
            ):
                pending = None
            self._publish(
                connection, replace(strategy, calls=calls, conversation_confirmation=pending)
            )
        return True

    def save(self, strategy: UserStrategy) -> None:
        """Publish this strategy through the one authority that owns it.

        A migrated strategy is written to the conversation database only, in one
        transaction, and its pre-migration JSON record is left byte for byte as it
        was. Every other strategy keeps the JSON record as its authority.
        """
        if not self._update_conversation(strategy):
            self._save_json(strategy)

    def get(self, user_strategy_id: str) -> UserStrategy:
        if self.conversation_db_path.exists():
            with self._read() as connection:
                migrated = self._document(connection, user_strategy_id)
            if migrated is not None:
                return migrated
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
        """Every published strategy once each, and no unpublished crash leftover.

        A migrated strategy is read from its committed row — never from the JSON
        record it was migrated from, which is not a second copy to fall back on —
        and a strategy that was never migrated is read from its own JSON record. The
        store publishes one bounded identity name per record, so a temporary file a
        crashed save left behind is not a strategy: it is never read, and a record
        whose own identity disagrees with its name is not shown as one. One
        unreadable record never hides the rest of the workspace.
        """
        items: dict[str, UserStrategy] = {}
        migrated: set[str] = set()
        if self.conversation_db_path.exists():
            with self._read() as connection:
                rows = connection.execute(
                    "SELECT strategy_id, document FROM strategy_documents"
                ).fetchall()
            for identity, document in rows:
                name = str(identity)
                if _SAFE_ID.fullmatch(name) is None:
                    continue
                migrated.add(name)
                try:
                    item = UserStrategy.decode(json.loads(str(document)))
                except (ValueError, TypeError, KeyError):
                    continue
                if item.user_strategy_id != name:
                    continue
                items[name] = item
        if self._dir.is_dir():
            for path in sorted(self._dir.glob("*.json")):
                if _SAFE_ID.fullmatch(path.stem) is None or path.stem in migrated:
                    continue
                try:
                    item = UserStrategy.decode(json.loads(path.read_bytes()))
                except (OSError, ValueError, TypeError, KeyError):
                    continue
                if item.user_strategy_id != path.stem:
                    continue
                items[path.stem] = item
        return [items[identity] for identity in sorted(items)]


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
        quine: QuineBridge | None = None,
    ):
        self._store = store
        self._research = research
        self._service = service
        self._settings = settings
        self._budget = dict(budget) if budget is not None else None
        #: The Rust core that owns experiments in this isolated candidate
        #: workspace. When it is configured it owns every new experiment this
        #: service confirms, and the in-process research owner is never reached
        #: for one; when it is not, the original owner path is unchanged.
        self._quine = quine
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
        self,
        approved_budget: Any,
        *,
        model_calls: int,
        backtests: int,
        ceiling: int | None = None,
    ) -> dict[str, int]:
        """One explicit approval, checked against the bound that applies to it.

        ``ceiling`` is the most model calls this approval may name: a new
        conversation turn is bounded per turn; other actions keep the workspace's
        own grant. It defaults to the
        workspace grant so an existing caller is unchanged. The backtest grant is
        always the workspace's, because a run is an execution cost rather than a
        conversation cost.
        """
        granted = _requested_budget(approved_budget)
        limit = self._workspace_budget()
        if ceiling is None:
            ceiling = limit["max_model_calls"]
        if (
            granted["max_model_calls"] < model_calls
            or granted["max_backtests"] < backtests
            or granted["max_model_calls"] > ceiling
            or granted["max_backtests"] > limit["max_backtests"]
        ):
            raise UserStrategyBudgetError(
                "the approved budget is outside what this workspace grants "
                f"({ceiling} model calls, {limit['max_backtests']} backtests)"
            )
        return granted

    def _model_port(self) -> ModelPort:
        return self._provider if self._provider is not None else create_model_port(self._settings)

    def _port_for_call(self, strategy: UserStrategy, call: str) -> ModelPort:
        if self._provider is not None:
            return self._provider
        record = _entry(strategy.calls, "call_id", call) or {}
        reference = record.get("model_profile")
        try:
            source = RadianSettings(self._settings.path, self._settings.load())
            identity = reference.get("id") if isinstance(reference, dict) else None
            if reference is not None and source.profile_ref(identity) != reference:
                raise ModelNotConfigured(
                    "the approved model connection changed; approve a new turn"
                )
            port = create_model_port(source.snapshot(identity))
            if self._safe_route(port) != record.get("route"):
                raise ModelNotConfigured("the approved model route is no longer available")
            return port
        except RadianSettingsError as error:
            raise ModelNotConfigured("the approved model connection is unavailable") from error

    def select_model_profile(self, user_strategy_id: object, profile_id: object) -> UserStrategy:
        identity = _identifier(user_strategy_id, "user_strategy_id")
        selected = None if profile_id is None else _identifier(profile_id, "model_profile_id")
        lock = self._lock(identity)
        if not lock.acquire(blocking=False):
            raise UserStrategyStateError("wait for the current turn before changing its connection")
        try:
            strategy = self._store.get(identity)
            try:
                self._settings.profile(selected)
            except RadianSettingsError as error:
                raise UserStrategyStateError(str(error)) from None
            updated = replace(strategy, model_profile_id=selected, updated_at=_now())
            self._store.save(updated)
            return updated
        finally:
            lock.release()

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
        if self._provider is not None:
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

    def _scenario_document(self, scenario_id: str, dataset_id: str | None = None) -> dict[str, Any]:
        """One registered scenario's own facts, under the data it will really run on.

        The dataset binding is part of the request, not an afterthought: a
        registered parameter's bounds, its history requirement and the next-bar
        constraint are derived from the bars the engine would actually admit, so
        reading them from the scenario's default data would describe a different
        run than the one a confirmation would start.
        """
        # Only a draft that really selected a dataset names one, so a scenario with
        # no selection keeps the exact call it always made.
        try:
            document = (
                self._service.validate_scenario(scenario_id)
                if dataset_id is None
                else self._service.validate_scenario(scenario_id, dataset_id=dataset_id)
            )
            return cast(dict[str, Any], document)
        except Exception as error:  # noqa: BLE001 — an unusable start is refused, not guessed
            raise UserStrategyStateError(
                f"{scenario_id} is not a currently registered, valid scenario: {error}"
            ) from None

    def _draft_document(self, draft: StrategyDraft) -> dict[str, Any]:
        """One draft's registered scenario facts, under the data it selected.

        A draft that selected a dataset is described by that data; one that did not
        keeps the exact call it always made, so a scenario's own default remains the
        description only for the drafts that never chose anything else.
        """
        self._require_draft_strategy(draft)
        if draft.dataset_id is None:
            return self._scenario_document(draft.scenario_id)
        return self._scenario_document(draft.scenario_id, draft.dataset_id)

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
        document = self._draft_document(draft)
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
        self,
        draft: StrategyDraft,
        parameters: dict[str, Any],
        initial_cash: str,
        dataset_id: str | None = None,
    ) -> dict[str, Any]:
        """The whole resulting spec, validated by the real backend. Nothing else."""
        self._require_draft_strategy(draft)
        selected = draft.dataset_id if dataset_id is None else dataset_id
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
                    # The data this draft would run on, so the backend validates the
                    # parameters against the bars that would really be admitted.
                    **({} if selected is None else {"dataset_id": selected}),
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

    def _require_draft_strategy(self, draft: StrategyDraft) -> None:
        """Refuse a replaced local identity statically, before any parameter callback."""
        if draft.scenario_id != LOCAL_SCENARIO_ID:
            return
        try:
            current = self._service.local_strategy_binding()
        except (OSError, ValueError) as error:
            raise UserStrategyStateError(str(error)) from None
        if current is None or draft.strategy_binding is None or current != draft.strategy_binding:
            raise UserStrategyStateError(
                "the draft's trusted local strategy identity is unavailable or changed; "
                "inspect and trust the package, then create a new strategy binding"
            )

    def _binding_for(self, draft: StrategyDraft) -> dict[str, Any] | None:
        """The capability binding this draft's input runs under, from this host.

        The draft names catalog identities — the dataset it selected and, when it
        is one, the workspace-local strategy scenario. This workspace's own plugin
        catalog supplies the exact versions, identities and authorization for
        them, so what is frozen is what this host currently verifies and never a
        digest a caller could have certified. An input that needs a binding it
        cannot be given is refused here, before anything is previewed or confirmed.
        """
        self._require_draft_strategy(draft)
        uploaded = type(draft.dataset_id) is str and self._service.uploads.has(draft.dataset_id)
        local = draft.scenario_id == LOCAL_SCENARIO_ID
        if not uploaded and not local:
            return None
        try:
            binding = self._research.catalog_binding(
                dataset_id=draft.dataset_id if uploaded else None,
                local_strategy=local,
            )
        except Exception as error:  # noqa: BLE001 — an unresolved capability is a refusal
            raise UserStrategyStateError(str(error)) from None
        if binding is None:
            raise UserStrategyStateError(
                "this draft's input has no capability this workspace currently authorizes"
            )
        return binding.document()

    def _admitted_request(self, draft: StrategyDraft) -> dict[str, Any]:
        """The authoritative admitted input of one draft, read from the service.

        This is what the engine would execute: the same dataset binding, the same
        capability binding, the same normalized parameters and the same input
        snapshot a job would freeze. It creates nothing and starts nothing, and it
        is the only source of the source identity this owner stores — never the
        caller's own hash.
        """
        try:
            return cast(
                dict[str, Any],
                self._service.prepare_input_snapshot(
                    draft.scenario_id,
                    parameters=self._parameters(draft),
                    dataset_id=draft.dataset_id,
                    source_sha256=draft.source_sha256,
                    plugin_binding=self._binding_for(draft),
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
        strategy_binding = (
            self._service.local_strategy_binding() if scenario == LOCAL_SCENARIO_ID else None
        )
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
            strategy_binding=strategy_binding,
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
        exchange_id: str | None = None,
    ) -> UserStrategy:
        """One saved strategy: a saved intent, or an executable draft of a scenario.

        Without a scenario this is an intent: it needs no catalog to exist, it
        resolves nothing, and it writes no executable field, because there is
        nothing yet to execute. With one, it is exactly the executable revision 1
        this owner has always written, validated by the same backend before
        anything is saved.
        """
        if exchange_id not in (None, "binance", "okx"):
            raise UserStrategyError("unsupported research exchange")
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
            exchange_id=exchange_id,
        )
        self._store.save(strategy)
        return strategy

    def get(self, user_strategy_id: object) -> UserStrategy:
        return self._store.get(_identifier(user_strategy_id, "user_strategy_id"))

    def list(self) -> Strategies:
        # A core-owned run is listed from its owner too: a row can never present a
        # state the core cannot be asked about, and no terminal claim is cached.
        if self._quine is not None:
            return self.reconcile_all()
        return self._store.list()

    def assign_exchange(
        self, user_strategy_id: str, *, exchange_id: str | None, expected_updated_at: str
    ) -> UserStrategy:
        """Research organization only; never changes the draft or connection authority."""
        identity = _identifier(user_strategy_id, "user_strategy_id")
        if exchange_id not in (None, "binance", "okx"):
            raise UserStrategyError("unsupported research exchange")
        with self._lock(identity):
            current = self.get(identity)
            if current.updated_at != expected_updated_at:
                raise UserStrategyStaleError(
                    "research changed; refresh before assigning its exchange"
                )
            updated = replace(current, exchange_id=exchange_id, updated_at=_now())
            self._store.save(updated)
            return updated

    # -- one explicit binding ------------------------------------------------

    def bind_scenario(
        self,
        user_strategy_id: object,
        *,
        operation_id: object,
        base_revision: object,
        scenario_id: object,
        criterion: Any = None,
        dataset_id: object = None,
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
        dataset = None if dataset_id is None else _bounded(dataset_id, "dataset_id", 128)
        if scenario.startswith("template:") and dataset is None:
            raise UserStrategyStateError("choose a published CSV dataset for this template")
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
            **({"dataset_id": dataset} if dataset is not None else {}),
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
            if dataset is not None:
                document = self._validated(
                    draft, dict(draft.parameters), draft.initial_cash, dataset_id=dataset
                )
                source = self._source_identity(document)
                if source.get("dataset_id") != dataset or source.get("source_sha256") is None:
                    raise UserStrategyStateError("the selected dataset cannot be bound")
                draft = replace(draft, dataset_id=dataset, source_sha256=source["source_sha256"])
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

    def draft_contract(self, user_strategy_id: object) -> dict[str, Any]:
        """Expose only this draft's existing registered editing contract."""
        identity = _identifier(user_strategy_id, "user_strategy_id")
        with self._lock(identity):
            draft = _executable(self._store.get(identity))
            return {
                "draft_revision": draft.revision,
                "scenario_id": draft.scenario_id,
                "fields": [
                    {
                        "path": field.path,
                        "kind": field.kind,
                        "json_type": field.json_type,
                        "current": field.current,
                        "minimum": field.minimum,
                        "maximum": field.maximum,
                        "editable": field.editable,
                    }
                    for field in self._registered_paths(draft)
                ],
                "source": "registered scenario contract; validated against the selected dataset",
            }

    def save_draft(
        self,
        user_strategy_id: object,
        *,
        operation_id: object,
        expected_revision: object,
        values: Any,
    ) -> UserStrategy:
        """Save a bounded manual edit through the same whole-draft revision path.

        The expected revision is compared after acquiring the existing strategy
        lock. An uncertain client must read the record, never blindly resubmit.
        Exact operation replay uses the existing change ledger; an aged-out
        request still cannot write twice because its revision is stale.
        """
        identity = _identifier(user_strategy_id, "user_strategy_id")
        operation = _identifier(operation_id, "operation_id")
        if type(expected_revision) is not int:
            raise UserStrategyStateError("expected_revision must be an integer")
        if type(values) is not dict or not values or len(values) > 64:
            raise UserStrategyStateError("select between 1 and 64 registered fields")
        if any(type(path) is not str or _PATH.fullmatch(path) is None for path in values):
            raise UserStrategyStateError("only registered parameters and initial_cash are editable")
        request_digest = _digest({"expected_revision": expected_revision, "values": values})
        lock = self._lock(identity)
        if not lock.acquire(blocking=False):
            raise UserStrategyStateError("this strategy is busy; no manual edit was saved")
        try:
            strategy = self._store.get(identity)
            draft = _executable(strategy)
            prior = _entry(strategy.changes, "operation_id", operation)
            if prior is not None:
                if prior.get("kind") != "manual" or prior.get("request_sha256") != request_digest:
                    raise UserStrategyConflictError("operation_id is already bound to another edit")
                return strategy
            if expected_revision != draft.revision:
                raise UserStrategyStaleError("the draft revision moved on; reload before saving")
            if self._unresolved_call(strategy) is not None:
                raise UserStrategyStateError("an earlier call is unresolved; reload before saving")
            contracts = self._contracts(self._draft_document(draft))
            overrides: dict[str, Any] = {}
            items: Documents = []
            for path, raw in values.items():
                value, problem = self._typed(contracts, path, raw, draft)
                if problem is not None:
                    raise UserStrategyStateError(problem)
                overrides[path] = value
                items.append(
                    {
                        "path": path,
                        "before": self._current(draft, path),
                        "after": value,
                        "reason": "Manual edit",
                        "proposer": "user",
                    }
                )
            return self._revise_draft(
                strategy,
                overrides=overrides,
                operation=operation,
                kind="manual",
                proposal_id=None,
                target_operation=None,
                items=items,
                applied_paths=list(overrides),
                stale_paths=[],
                proposer="user",
                request_sha256=request_digest,
            )
        finally:
            lock.release()

    # -- one explicit dataset selection --------------------------------------

    def select_dataset(
        self,
        user_strategy_id: object,
        *,
        operation_id: object,
        base_revision: object,
        dataset_id: object,
    ) -> UserStrategy:
        """Choose one explicitly selected local dataset for one executable draft.

        The user chooses the dataset by the identity this workspace's catalog lists
        it under; the host resolves and validates it. The whole resulting draft —
        the current parameters and funding included — is validated by the real
        backend *against that dataset* before anything is written, so a parameter
        the selected data cannot support is refused here rather than discovered at
        confirmation, and the parameter bounds the model is shown come from the
        same data rather than from the scenario's default. It is one revision of
        the same strategy: identity, intent, conversation, calls, proposals and the
        change ledger are kept and nothing is undone. Selecting again is a new
        revision, which is exactly what makes an earlier preview stale — the
        preview identity names the admitted input, so a confirmation of a preview
        taken before this selection is refused instead of silently run.
        """
        identity = _identifier(user_strategy_id, "user_strategy_id")
        operation = _identifier(operation_id, "operation_id")
        if type(base_revision) is not int:
            raise UserStrategyStateError("base_revision must be an integer")
        dataset = _bounded(dataset_id, "dataset_id", _MAX_DATASET_ID)
        # The whole request is this operation's identity: the revision the caller
        # read and the exact dataset, in the host's own vocabulary.
        request: dict[str, Any] = {
            "action": "select-dataset",
            "base_revision": base_revision,
            "dataset_id": dataset,
        }
        lock = self._lock(identity)
        if not lock.acquire(blocking=False):
            raise UserStrategyStateError(
                "this strategy is busy with a call that has not settled; nothing was selected, "
                "so reload it and choose the dataset again"
            )
        try:
            strategy = self._store.get(identity)
            recorded = strategy.dataset
            if recorded is not None and recorded.get("operation_id") == operation:
                # The recorded selection is the answer to this operation: it is
                # compared with the request it was made from, never with today's
                # draft, so the same selection replays as itself across a restart.
                if recorded.get("request") != request:
                    raise UserStrategyConflictError(
                        "operation_id is already bound to a different dataset selection"
                    )
                return strategy
            if _entry(strategy.changes, "operation_id", operation) is not None:
                raise UserStrategyConflictError(
                    "operation_id is already bound to a different operation"
                )
            draft = _executable(strategy)
            if base_revision != draft.revision:
                raise UserStrategyStaleError(
                    "the draft revision moved on; reload it before selecting a dataset"
                )
            if self._unresolved_call(strategy) is not None:
                raise UserStrategyStateError(
                    "an earlier call was never observed; reload the strategy so it is "
                    "classified before a dataset is selected"
                )
            document = self._validated(
                draft,
                dict(draft.parameters),
                draft.initial_cash,
                dataset_id=dataset,
            )
            source = self._source_identity(document)
            if source.get("dataset_id") != dataset or source.get("source_sha256") is None:
                raise UserStrategyStateError(
                    f"{dataset} is not a dataset this workspace can bind a run to"
                )
            summary = cast(dict[str, Any], document.get("summary") or {})
            # The backend's own normalization wins per key, but the draft's
            # parameter set is exactly what the user confirmed: validation never
            # adds or drops a field, it only rejects a combination this data cannot
            # support.
            parameters = dict(draft.parameters)
            normalized = summary.get("parameters")
            if isinstance(normalized, dict):
                parameters = {
                    name: normalized.get(name, value) for name, value in parameters.items()
                }
            revised = replace(
                draft,
                revision=draft.revision + 1,
                dataset_id=dataset,
                source_sha256=cast(str | None, source.get("source_sha256")),
                initial_cash=str(summary.get("initial_cash") or draft.initial_cash),
                parameters=parameters,
            )
            now = _now()
            change: dict[str, Any] = {
                "operation_id": operation,
                "kind": "select-dataset",
                "proposal_id": None,
                "target_operation_id": None,
                "dataset_id": dataset,
                "from_revision": draft.revision,
                "to_revision": revised.revision,
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
                "dataset_id": dataset,
                "from_revision": draft.revision,
                "to_revision": revised.revision,
                "selected_at": now,
            }
            # One durable write: the revised draft and the request it was selected
            # by exist together, or not at all.
            next_strategy = replace(
                strategy,
                updated_at=now,
                draft=revised,
                history=(*strategy.history, draft)[-_MAX_HISTORY:],
                changes=(*strategy.changes, change)[-_MAX_CHANGES:],
                dataset=record,
            )
            self._store.save(next_strategy)
            return next_strategy
        finally:
            lock.release()

    # -- approved model calls -----------------------------------------------

    def _lock(self, user_strategy_id: str) -> threading.Lock:
        # A streaming iterator may advance on different HTTP pool threads. The
        # ordinary lock serializes the strategy without thread-affine ownership.
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
        evidence_job_ids: Any = None,
        expected_evidence: Documents | None = None,
    ) -> UserStrategy:
        """One admitted turn, answered by the hand-written single-call path.

        The refinement path (a new proposal that replaces a pending suggestion) still
        asks one question and reads one answer, so it keeps this path exactly as it
        was. An ordinary conversation turn goes through :meth:`conversation_turn`,
        which admits the same way and then runs the graph.
        """
        admitted = self._admit(
            strategy,
            call_id=call_id,
            base_revision=base_revision,
            text=text,
            approved_budget=approved_budget,
            action=action,
            proposal_id=proposal_id,
            evidence_job_ids=evidence_job_ids,
            expected_evidence=expected_evidence,
        )
        if isinstance(admitted, UserStrategy):
            return admitted
        return self._observe(
            admitted.strategy, admitted.call_id, admitted.message, admitted.base_revision
        )

    def _admit(
        self,
        strategy: UserStrategy,
        *,
        call_id: object,
        base_revision: object,
        text: object,
        approved_budget: Any,
        action: str,
        proposal_id: str | None,
        evidence_job_ids: Any,
        expected_evidence: Documents | None,
        task_budget: Any = None,
    ) -> UserStrategy | _AdmittedTurn:
        """Every check and every durable record a turn needs before it may dispatch.

        Returns the turn's own new strategy when there is nothing to dispatch — a
        replayed identity, or a workspace with no provider — and the admitted turn
        with its call identity, message and budget when a provider may be reached.
        """
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
        interactive = task_budget == "interactive"
        existing = _entry(strategy.calls, "call_id", call)
        if interactive:
            saved_request = (existing or {}).get("request") or {}
            task_budget = (
                saved_request["task_budget"]
                if saved_request.get("execution") == "interactive"
                else _INTERACTIVE_MAX_DISPATCHES
            )
            request["execution"] = "interactive"
        if task_budget is not None:
            if type(task_budget) is not int or not 1 <= task_budget <= 100:
                raise UserStrategyBudgetError(
                    "choose an explicit task total between 1 and 100 requests"
                )
            if request["approved_budget"] != {"max_model_calls": task_budget, "max_backtests": 0}:
                raise UserStrategyBudgetError(
                    "task total must match the explicit approval; experiments are separate"
                )
            request["task_budget"] = task_budget
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
        # The record cap is a bound on a pre-migration JSON conversation, which has
        # no database to hold an unbounded ledger. A migrated conversation is the
        # workspace's canonical store and is never capped: an ordinary interactive
        # conversation must not permanently exhaust from its own recorded count.
        if not self._store.is_conversation_migrated(strategy.user_strategy_id) and (
            len(strategy.calls) >= _MAX_CALLS or len(strategy.messages) + 1 >= _MAX_MESSAGES
        ):
            raise UserStrategyStateError("this strategy has reached its recorded call limit")
        unresolved = self._unresolved_call(strategy)
        if unresolved is not None:
            # An earlier call whose outcome was never observed is settled first: a
            # new call never overwrites an unresolved dispatch.
            raise UserStrategyStateError(
                "an earlier call was never observed; reload the strategy so it is "
                "classified before another call is started"
            )
        if any(
            (item.get("task") or {}).get("status")
            in {"running", "awaiting_budget", "awaiting_decision"}
            for item in strategy.calls
        ):
            raise UserStrategyStateError(
                "finish or cancel the current task before starting another objective"
            )
        # New messages authorize one bounded turn, independently of lifetime usage.
        # Replay returned above, so retained calls keep their original approval.
        if task_budget is not None and task_budget > _INTERACTIVE_MAX_DISPATCHES:
            raise UserStrategyBudgetError("a new task may authorize at most 24 requests")
        budget = self._approval(
            approved_budget,
            model_calls=_CALL_APPROVAL,
            backtests=0,
            ceiling=task_budget
            if task_budget is not None
            else (_TURN_MAX_CALLS if action == "message" else None),
        )
        # The selected runs are read, verified and frozen here: after the approval,
        # before the durable reservation and before the provider can be reached, so a
        # selection this workspace cannot verify leaves no call record and no
        # dispatch behind it, and no conversation is ever grounded in a run the user
        # did not select or that is no longer the run it names.
        evidence = None if selection is None else self._evidence_context(strategy, selection)
        # A revision that inherits a selection is re-verified here, after the replay
        # short-circuit above and before the reservation, the durable record and the
        # provider: the runs it is grounded in have to be the runs the call that
        # produced this proposal recorded, under the same frozen identities, so a
        # report that has since been re-published is refused rather than silently
        # substituted for the evidence the user's proposal was made over.
        if expected_evidence is not None and (
            evidence is None or not self._same_evidence(expected_evidence, evidence)
        ):
            raise UserStrategyStateError(
                "the runs this proposal was made over are no longer the runs it recorded; "
                "nothing was asked"
            )
        profile = None
        if self._provider is not None:
            route = self._safe_route()
        else:
            try:
                source = RadianSettings(self._settings.path, self._settings.load())
                profile = source.profile_ref(strategy.model_profile_id)
                route = self._safe_route(
                    create_model_port(source.snapshot(strategy.model_profile_id))
                )
            except RadianSettingsError:
                route = None
        now = _now()
        record: dict[str, Any] = {
            "call_id": call,
            # Durable before the provider can be reached: a crash here proves that
            # nothing was sent, which a single "dispatched" status could not.
            "status": "pending" if route is not None else "unconfigured",
            "route": route or {"provider": None, "model": None, "endpoint": None},
            **({"model_profile": profile} if profile is not None else {}),
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
        if task_budget is not None:
            record["task"] = {
                **({"mode": "interactive"} if interactive else {}),
                "limit": task_budget,
                "grants": [],
                "status": "running",
                "stop_reason": None,
            }
        if evidence is not None:
            # The provenance of this turn, kept on the call it belongs to: the runs
            # the user selected and the host-verified context each of them was read
            # as. It is a record of what was explained, never a new authority over
            # the draft, the runs or the budget.
            record["evidence"] = evidence
        pending = replace(
            strategy,
            updated_at=now,
            messages=(
                *strategy.messages,
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
            calls=(*strategy.calls, record),
        )
        # One durable write: the user's own message, the approved budget and the
        # reservation exist before the provider is reached.
        self._publish_call(pending, call, dispatch=route is not None)
        if route is None:
            return pending
        return _AdmittedTurn(pending, call, message, base_revision, budget, list(selection or []))

    def _publish_call(self, strategy: UserStrategy, call: str, *, dispatch: bool) -> None:
        """Publish one call record through the one authority this strategy has.

        A call that may reach the provider of a migrated conversation is reserved
        through the conversation database, where this call's record and the
        workspace's recorded usage are committed together, so it is counted exactly
        once for the whole workspace. The reservation carries no cumulative ceiling:
        the interactive conversation is bounded per turn, and its recorded history is
        accounting and replay evidence rather than a permanent stop. A call that
        proves no network — no provider is configured — is never counted, and every
        other conversation keeps its JSON record as its one authority, where the
        record is durable before dispatch and the ceiling is the turn's own approval.
        """
        if dispatch and self._store.is_conversation_migrated(strategy.user_strategy_id):
            self._store.reserve_model_call(strategy, call)
        else:
            self._store.save(strategy)

    def _observe(
        self, strategy: UserStrategy, call: str, message: str, base_revision: int
    ) -> UserStrategy:
        try:
            port = self._port_for_call(strategy, call)
        except ModelNotConfigured as error:
            return self._settle(strategy, call, "unconfigured", str(error), None)
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
            # A refinement replaces the one open candidate its own originating call
            # named, and nothing else. The supersede is written in the same durable
            # record as the valid new proposal below, so an answer that proposed
            # nothing — a provider failure, a refusal, or plain text — never reaches
            # here and leaves that candidate exactly as it was. Every entry is rebuilt
            # rather than mutated, so the strategy being read is never edited in place.
            target = self._revise_target(strategy, call)
            retained = proposals[-_MAX_PROPOSALS:]
            if target is not None:
                retained = tuple(
                    {**item, "status": "superseded"}
                    if item.get("proposal_id") == target and item.get("status") == "open"
                    else item
                    for item in retained
                )
            proposals = (
                *retained,
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
        try:
            port = self._port_for_call(strategy, call)
        except ModelNotConfigured as error:
            return self._settle(strategy, call, "unconfigured", str(error), None)
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
        facts.extend(self._data_version_facts(draft))
        facts.extend(self._selected_facts(evidence) if evidence else self._verified_facts(strategy))
        history, omitted = self._recorded_turns(strategy, message)
        if omitted:
            facts.append(_OMITTED_HISTORY_NOTE)
        return [
            ModelMessage(role="user", text="\n".join(facts)),
            *history,
        ]

    def _data_version_facts(self, draft: StrategyDraft) -> Paths:
        """The host's own verified facts about the data this draft would run on.

        A conversation about a strategy is a conversation about *some* data, and the
        selection is the draft's, not the model's: the identity here is re-derived at
        this moment from the same dataset binding the engine would admit, and a
        published version's evidence digest is read from this workspace's own catalog
        now, so what the model is told is a fact this host just verified rather than
        a field some caller sent. The block stays at identity and shape — the
        dataset's own name, the digests that name its source and its selected bars,
        how many bars those are and the window they replay over. No row of the
        capture, no filesystem path and no capability beyond these facts is stated,
        and this never raises: a binding this workspace cannot read now is stated as
        the absence it is.
        """
        identity: dict[str, Any] = {
            "dataset_id": draft.dataset_id,
            "source_sha256": draft.source_sha256,
        }
        window: str | None = None
        try:
            document = self._draft_document(draft)
            admitted = cast(dict[str, Any], document.get("input_identity") or {})
            if admitted:
                identity = admitted
            data = cast(dict[str, Any], document.get("summary") or {}).get("data")
            if isinstance(data, dict):
                window = f"{data.get('start_utc')} .. {data.get('end_utc')}"
        except Exception:  # noqa: BLE001 — an unreadable binding is stated, not raised
            window = None
        facts = ["The data this draft would run on, as this host verified it now:"]
        dataset_id = identity.get("dataset_id")
        if not isinstance(dataset_id, str) or not dataset_id:
            facts.append(
                "  It is the registered scenario's own data; no separate dataset version "
                "is selected."
            )
            facts.append(
                f"  Admitted source digest {identity.get('source_sha256') or 'unrecorded'}"
            )
        else:
            evidence = self._published_version_digest(dataset_id)
            facts.append(f"  Selected dataset version {dataset_id}")
            facts.append(f"  Admitted source digest {identity.get('source_sha256')}")
            facts.append(
                f"  Published version evidence digest {evidence or 'unavailable right now'}"
            )
        facts.append(
            "  Selected bars digest "
            + str(identity.get("data_sha256") or "unrecorded")
            + f" over {identity.get('record_count')} records"
            + (f", replaying {window}" if window else "")
        )
        facts.append(
            "  These are the host's own verified readings of the selected data; they are "
            "not the user's claims, no proposal can change them, and the capture's rows "
            "are not part of this conversation."
        )
        return facts

    def _published_version_digest(self, dataset_id: str) -> str | None:
        """This workspace's current evidence digest for one uploaded version."""
        try:
            return self._service.dataset_catalog.version_evidence_digest(dataset_id)
        except Exception:  # noqa: BLE001 — a version that cannot be re-derived is absent
            return None

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
            "This turn may discover actual catalog resources and inspect a supported "
            "template/data pairing. Neither tool binds the idea or creates a run. No "
            "change to a draft can be proposed until the user chooses a scenario.",
            "Intent revision " + str(strategy.draft.revision),
        ]
        facts.extend(self._verified_facts(strategy))
        history, omitted = self._recorded_turns(strategy, message)
        if omitted:
            facts.append(_OMITTED_HISTORY_NOTE)
        return [ModelMessage(role="user", text="\n".join(facts)), *history]

    @staticmethod
    def _turn_of(strategy: UserStrategy) -> dict[str, str]:
        """The root turn each recorded call belongs to, read from the calls themselves.

        A call whose request names a ``turn_id`` is one further call of that turn; a
        call that names none is the root of its own turn. Grouping by this record — a
        root call and its children, or a legacy ordinary exchange whose message
        carries only its own call identity — means a multi-call turn and a legacy
        turn are bounded by exactly the same rule.
        """
        mapping: dict[str, str] = {}
        for call in strategy.calls:
            call_id = str(call.get("call_id") or "")
            if not call_id:
                continue
            request = call.get("request")
            turn = request.get("turn_id") if isinstance(request, dict) else None
            mapping[call_id] = str(turn) if isinstance(turn, str) and turn else call_id
        return mapping

    @classmethod
    def _recorded_turns(cls, strategy: UserStrategy, message: str) -> tuple[History, bool]:
        """The bounded suffix of complete prior turns, then the question asked here.

        History is grouped into complete turns by the persisted call identity each
        message carries, so a root call with its children and a legacy ordinary
        exchange are each one turn and are never split. Only a deterministic suffix
        of the newest complete turns is carried, within both a turn count and a
        character budget; the suffix stops at the first turn that would exceed either
        bound, so an oversized turn is dropped whole rather than truncated. This
        bounds the *request* only — every stored message and call survives — and the
        second value reports whether older history was omitted. The current question
        is always carried whole and is never part of the bounded suffix.
        """
        turns = cls._turn_of(strategy)
        # A paused proposal may resume after other turns. Collect every message
        # of its root, ordered by last activity, before applying either bound.
        grouped: dict[str, Documents] = {}
        for entry in strategy.messages[:-1]:
            call_id = str(entry.get("call_id") or "")
            key = turns.get(call_id, call_id)
            group = grouped.pop(key, [])
            group.append(entry)
            grouped[key] = group
        selected: set[str] = set()
        used = 0
        for key, group in reversed(grouped.items()):
            size = sum(len(str(entry.get("text") or "")) for entry in group)
            if len(selected) >= _MAX_HISTORY_TURNS or used + size > _MAX_HISTORY_CHARS:
                break
            selected.add(key)
            used += size
        omitted = len(selected) < len(grouped)
        history: History = []
        # Keep the original message chronology even when selected turns interleave.
        for entry in strategy.messages[:-1]:
            call_id = str(entry.get("call_id") or "")
            if turns.get(call_id, call_id) not in selected:
                continue
            role = "user" if entry.get("role") == "user" else "assistant"
            text = str(entry.get("text") or "")
            if text:
                history.append(ModelMessage(role=cast(Any, role), text=text))
        history.append(ModelMessage(role="user", text=message))
        return history, omitted

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

    @staticmethod
    def _revise_target(strategy: UserStrategy, call: str) -> str | None:
        """The proposal one retained call was asked to refine, or ``None``.

        Read from the call's own recorded request, which is exactly what it was
        admitted as: only a call whose action was ``revise`` names a proposal to
        replace, so an ordinary message supersedes nothing.
        """
        record = _entry(strategy.calls, "call_id", call)
        request = None if record is None else record.get("request")
        if not isinstance(request, dict) or request.get("action") != "revise":
            return None
        target = request.get("proposal_id")
        return target if isinstance(target, str) else None

    @staticmethod
    def _same_evidence(recorded: Documents, verified: Documents) -> bool:
        """Whether re-verified runs are, one by one, the runs that were recorded.

        A run is the same run only while its own frozen identity still holds: the
        job it names, the engine run it published, the report digest it was read
        under and the input digest it was confirmed from. A report re-published
        under a new digest is a different reading of that job, not the evidence the
        original call was grounded in, so it is never substituted for it.
        """
        if len(recorded) != len(verified):
            return False
        keys = ("job_id", "run_id", "report_sha256", "input_sha256")
        return all(
            all(was.get(key) == now.get(key) for key in keys)
            for was, now in zip(recorded, verified, strict=True)
        )

    def _proposal_evidence(
        self, strategy: UserStrategy, proposal: dict[str, Any]
    ) -> Documents | None:
        """The runs one proposal was made over, read from its own originating call.

        A proposal is grounded only through the call that produced it and this
        strategy retained: the recorded proposal message and the proposal name the
        same call as this strategy's own observed answer from the model, that call is
        exactly one call of this strategy, its request is still the request it was
        recorded under, its request and its retained evidence state the same runs in
        the same order, and the revision the proposal records is the one that call
        froze. A genuinely ungrounded proposal — no selection and no evidence at all,
        on a call whose recorded request digest and observed outcome still hold — is
        returned as an honest ``None``, which is a different thing from a grounded
        record whose pair has been damaged. A missing or ambiguous call, a request
        that no longer hashes to the digest it was stored with, a call that recorded
        no observed answer of the model's, a selection and evidence that disagree, one
        side of an originally grounded pair that is gone, or a conflicting revision is
        refused here, before any reservation, write or provider. What is returned is
        provenance, never authority: the runs are re-verified against this workspace's
        own frozen identities before a new call exists.
        """
        proposal_id = proposal.get("proposal_id")
        call_id = proposal.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise UserStrategyStateError(
                "this proposal does not name the call it came from; nothing was asked"
            )
        recorded = [
            item
            for item in strategy.messages
            if item.get("kind") == "proposal" and item.get("proposal_id") == proposal_id
        ]
        if (
            len(recorded) != 1
            or recorded[0].get("call_id") != call_id
            or recorded[0].get("role") != "model"
        ):
            raise UserStrategyStateError(
                "this proposal is not the one its originating call recorded; nothing was asked"
            )
        matches = [item for item in strategy.calls if item.get("call_id") == call_id]
        if len(matches) != 1:
            raise UserStrategyStateError(
                "this proposal's originating call is not one call of this strategy; "
                "nothing was asked"
            )
        call = matches[0]
        request = call.get("request")
        if not isinstance(request, dict):
            raise UserStrategyStateError(
                "this proposal's originating call kept no readable request; nothing was asked"
            )
        # The originating call is a source only while it is still the request it was
        # recorded under and an answer it actually observed. A request whose stored
        # digest no longer hashes to it — one member of the pair quietly removed
        # included — is no longer that request, and a call with no observed outcome
        # recorded no answer to have proposed anything: both are inconsistent sources
        # rather than an ordinary ungrounded proposal, and neither may fall back to
        # this strategy's latest run.
        if call.get("payload_sha256") != _digest(request):
            raise UserStrategyStateError(
                "this proposal's originating call is not the request it was recorded "
                "under; nothing was asked"
            )
        if call.get("status") != "observed":
            raise UserStrategyStateError(
                "this proposal's originating call recorded no observed answer; nothing was asked"
            )
        revision = request.get("base_revision")
        if (
            revision != proposal.get("base_revision")
            or revision != recorded[0].get("base_revision")
            or revision != call.get("base_revision")
        ):
            raise UserStrategyStateError(
                "this proposal and its originating call record different revisions; "
                "nothing was asked"
            )
        selection = request.get("evidence_job_ids")
        evidence = call.get("evidence")
        if selection is None:
            # Absence on both sides is a genuinely ungrounded proposal: an ordinary
            # turn has no run to lose, which is not the same as a damaged pair.
            if evidence not in (None, []):
                raise UserStrategyStateError(
                    "this proposal's originating call recorded evidence it never selected; "
                    "nothing was asked"
                )
            return None
        selected = self._evidence_selection(selection)
        if (
            type(evidence) is not list
            or not evidence
            or any(type(item) is not dict for item in evidence)
        ):
            raise UserStrategyStateError(
                "this proposal's originating call kept no evidence for the runs it selected; "
                "nothing was asked"
            )
        records = cast(Documents, evidence)
        if [item.get("job_id") for item in records] != selected:
            raise UserStrategyStateError(
                "this proposal's recorded evidence is not the runs its request selected; "
                "nothing was asked"
            )
        return records

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
        and what it is not. One of those limits, no_benchmark_comparison, is stated
        for what it is: the absence of a benchmark in one run's own configuration,
        not a host finding about whether the selected runs can be compared. The
        registered fields above still describe today's draft, and a proposal still
        targets that draft.
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
                "A run whose limitations record no_benchmark_comparison is stating that "
                "its own configuration carried no benchmark. That is not by itself a "
                "finding that the selected runs cannot be compared, and it does not "
                "settle the question either way: what the runs actually froze — their "
                "inputs, data windows, costs and parameters — is what decides whether "
                "their difference is comparable at all.",
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
        document = self._draft_document(draft)
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
        record: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> UserStrategy:
        """One durable write: the observed outcome, then the messages it produced.

        ``record`` adds caller-owned fields — a turn's own response document, its
        identity and its place in the loop — beside the outcome, and never over it.
        A ``message_id`` names the one message this settlement produces, so a
        settlement that is replayed after a lost process writes that same message
        again rather than a second copy of it.
        """
        now = _now()
        recorded = _entry(strategy.calls, "call_id", call)
        if recorded is None:
            # A settlement always names a call this strategy already records; nothing
            # may be written as the outcome of a call that was never reserved.
            raise UserStrategyStateError("no call of this strategy has that identity")
        model: str | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        if response is not None:
            model = response.model
            if response.usage_available:
                input_tokens = int(response.input_tokens or 0)
                output_tokens = int(response.output_tokens or 0)
        settled = {
            **recorded,
            **(record or {}),
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
            entry = {
                "message_id": message_id or str(uuid.uuid4()),
                "role": "model",
                "kind": "proposal" if proposal_id is not None else "message",
                "text": text,
                "base_revision": settled["base_revision"],
                "at": now,
                "call_id": call,
                "proposal_id": proposal_id,
            }
            messages = (
                *(item for item in messages if item.get("message_id") != entry["message_id"]),
                entry,
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
        task_budget: Any = None,
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

        The turn is the conversation graph's: the same admission, reservation and
        canonical records as ever, with the model loop, the tools and the decisions it
        waits for run as one bounded graph. This call simply exhausts that stream and
        returns the durable record it produced, which is what every existing caller
        already expects.
        """
        identity = _identifier(user_strategy_id, "user_strategy_id")
        for item in self.conversation_turn(
            identity,
            call_id=call_id,
            base_revision=base_revision,
            text=text,
            approved_budget=approved_budget,
            evidence_job_ids=evidence_job_ids,
            task_budget=task_budget,
        ):
            if item.get("kind") == ERROR and item.get("code") in {
                "UserStrategyError",
                "UserStrategyStateError",
                "UserStrategyBudgetError",
                "UserStrategyStaleError",
                "UserStrategyConflictError",
            }:
                raise UserStrategyStateError(str(item.get("error")))
        return self._store.get(identity)

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

            # The runs this proposal was made over are read from its own originating
            # call, and that retained pair — not today's draft, not the caller's own
            # ids and not the conversation text — is what the refinement inherits.
            # A record that no longer holds the proposal and its call together is
            # refused here, before any reservation or provider.
            origin = self._proposal_evidence(strategy, existing)

            return self._ask(
                strategy,
                call_id=call_id,
                base_revision=strategy.draft.revision,
                text=_bounded(text, "message", _MAX_MESSAGE, required=False)
                or "Revise the proposed change",
                approved_budget=approved_budget,
                action="revise",
                proposal_id=target,
                evidence_job_ids=(
                    None if origin is None else [record["job_id"] for record in origin]
                ),
                expected_evidence=origin,
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
        request_sha256: str | None = None,
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
        if request_sha256 is not None:
            change["request_sha256"] = request_sha256
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
        return self._preview_of(strategy, draft, self._admitted_request(draft))

    def _preview_of(
        self, strategy: UserStrategy, draft: StrategyDraft, admitted: dict[str, Any]
    ) -> dict[str, Any]:
        """One admitted input, rendered as the preview a confirmation names."""
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
            # Which owner a confirmation would reach, so the panel can show the
            # confirmed-run semantics of the owner that would really run it.
            "owner": None if self._quine is None else _QUINE_OWNER,
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
        with self._lock(identity):
            return self._launch_experiment_locked(
                identity,
                launch_intent_id=launch_intent_id,
                base_revision=base_revision,
                preview_identity=preview_identity,
                approved_budget=approved_budget,
            )

    def _launch_experiment_locked(
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
        strategy = self._store.get(identity)
        if self._quine is not None:
            # A confirmation this workspace no longer holds is recovered from
            # its owner before a new one is considered, so the same intent
            # resolves to the run it already confirmed rather than to a second.
            strategy = self._restore_quine(strategy)
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
            # A confirmation the core owns is answered from the core, never from
            # this record: asking about the same intent again only re-reads the
            # owner, so a lost receipt is resolved by a query and never by a
            # second confirmation.
            if existing.get("owner") == _QUINE_OWNER:
                return self._refresh_quine(strategy, identity, intent)
            return strategy
        if base_revision != strategy.draft.revision:
            raise UserStrategyStaleError(
                "the draft revision moved on; preview the experiment again"
            )
        budget = self._approval(approved_budget, model_calls=0, backtests=1)
        if len(strategy.experiments) >= _MAX_EXPERIMENTS:
            raise UserStrategyStateError("this strategy has reached its experiment limit")
        if self._quine is not None:
            return self._launch_isolated(strategy, identity, intent, preview, budget)
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
        pending = replace(strategy, updated_at=_now(), experiments=(*strategy.experiments, record))
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

    def _spec(self, draft: StrategyDraft) -> ResearchSpec:
        binding = self._binding_for(draft)
        selection = (
            None
            if binding is None
            else {
                item["layer"]: {
                    "plugin_id": item["plugin_id"],
                    "package_version": item["package_version"],
                }
                for item in binding["components"]
            }
        )
        return ResearchSpec(
            strategy_id=draft.strategy_id,
            instrument_venue=draft.venue,
            instrument_symbol=draft.symbol,
            initial_cash=draft.initial_cash,
            rationale="Confirmed draft of one user strategy.",
            selection=selection,
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

    # -- the Rust core owns the run (isolated candidate workspace) -----------

    def _freeze_for_core(
        self,
        draft: StrategyDraft,
        admitted: dict[str, Any],
        extras: dict[str, Any] | None = None,
    ) -> QuineFrozen:
        """The frozen input one confirmation carries, rendered by the domain adapter.

        The envelope is built from the same registered scenario file and the same
        parameters the preview admitted, and it must reproduce the admitted input's
        own snapshot and identity. A disagreement is refused here, before anything
        durable exists and before the core is reached: the confirmation would
        otherwise freeze a surface the user never saw.
        """
        assert self._quine is not None
        try:
            request: dict[str, Any] = {
                "scenario_path": str(self._service.registered_scenario_file(draft.scenario_id)),
                "scenario_id": draft.scenario_id,
                "parameters": self._parameters(draft),
            }
            if draft.dataset_id is not None:
                # The dataset is named by its catalog identity and resolved through
                # the roots this workspace already owns: its registered read-only
                # root, when it has one, and its controlled upload store. Nothing
                # here is a per-dataset path, and the published version evidence the
                # adapter binds comes from that same store.
                request["dataset_id"] = draft.dataset_id
                request["workspace_root"] = str(self._service.uploads.root.parent)
                if self._service.datasets is not None:
                    request["data_root"] = str(self._service.datasets.root)
            binding = self._binding_for(draft)
            if binding is not None:
                # The capability binding this input runs under, exactly as this
                # host's catalog resolved it just now. It travels inside the frozen
                # bytes, so what the core retains names the implementations and the
                # dataset version the confirmation was actually made about.
                request["binding"] = binding
            if draft.scenario_id == LOCAL_SCENARIO_ID:
                request["local_strategy_workspace"] = str(self._service.workspace)
            strategy_root = self._service.registry.strategy_root
            if strategy_root is not None:
                request["strategy_root"] = str(strategy_root)
        except UserStrategyStateError:
            raise
        except (OSError, ValueError) as error:
            raise UserStrategyStateError(
                f"the draft's registered scenario cannot be frozen: {error}"
            ) from None
        return self._quine.freeze(
            request,
            input_sha256=cast(str, admitted.get("input_sha256")),
            input_identity=admitted.get("input_identity"),
            extras=extras,
        )

    def _launch_isolated(
        self,
        strategy: UserStrategy,
        identity: str,
        intent: str,
        preview: str,
        budget: dict[str, int],
    ) -> UserStrategy:
        """Confirm one previewed input under the lock, with the core as the only owner.

        The lock is held across the whole confirmation, the durable record is written
        before the core is reached, and the outcome is always the core's own: a
        refused confirmation is reported as refused, a lost reply is only ever
        queried, and no local record is allowed to claim a run the core did not make.
        """
        assert self._quine is not None
        draft = _executable(strategy)
        # The admitted input is re-derived here, before anything durable exists, and
        # the frozen input the core would run is held to it: a confirmation of an
        # input the engine no longer admits is refused instead of frozen.
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
            "input_identity": admitted.get("input_identity"),
            "input_sha256": admitted.get("input_sha256"),
            "snapshot": admitted.get("snapshot"),
            "preview_identity": preview,
            "normalized": normalized,
            "approved_budget": budget,
            "frozen_at": _now(),
        }
        # The confirmation's own metadata is frozen inside the admitted input, so
        # the owner retains exactly what this confirmation was made of: a lost
        # projection is rebuilt from the owner's bytes rather than remembered here.
        frozen = self._freeze_for_core(
            draft,
            admitted,
            {
                "confirmation": {
                    "schema": CONFIRMATION_SCHEMA,
                    "payload": payload,
                    "payload_sha256": _digest(payload),
                }
            },
        )
        record = {
            "launch_intent_id": intent,
            "request_id": str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"ea.user-strategy/{identity}/{intent}")
            ),
            "payload_sha256": _digest(payload),
            "payload": payload,
            # The one marker that says which owner this experiment belongs to. A
            # record without it keeps the original in-process owner untouched.
            "owner": _QUINE_OWNER,
            "task_id": None,
            "job_id": None,
            "status": "pending",
            "message": None,
            "created_at": _now(),
        }
        pending = replace(strategy, updated_at=_now(), experiments=(*strategy.experiments, record))
        self._store.save(pending)
        try:
            receipt = self._quine.confirm(
                strategy_id=identity,
                intent_id=intent,
                draft_revision=draft.revision,
                preview_identity=preview,
                max_model_calls=budget["max_model_calls"],
                frozen=frozen,
            )
        except QuineRefused as refusal:
            # The core answered and refused: no run exists and nothing was charged.
            # A refusal that names an already-accepted cancellation is that
            # cancellation, not a failure of this run.
            if refusal.code == "cancelled_before_confirm":
                return self._settle_quine(
                    pending,
                    intent,
                    {
                        "status": "cancelled",
                        "job_id": None,
                        "attempt_id": None,
                        "frozen_sha256": None,
                        "phase": None,
                        "report_ready": False,
                        "report_sha256": None,
                        "facts": None,
                        "report_schema": None,
                        "path_schema": None,
                        "message": (
                            "the core refused this confirmation because the run was "
                            "cancelled before it was confirmed; nothing was created"
                        ),
                    },
                )
            return self._settle_quine(
                pending,
                intent,
                {
                    "status": "failed",
                    "job_id": None,
                    "attempt_id": None,
                    "frozen_sha256": None,
                    "phase": None,
                    "report_ready": False,
                    "report_sha256": None,
                    "facts": None,
                    "report_schema": None,
                    "path_schema": None,
                    "message": f"the core refused this confirmation ({refusal.code}): "
                    f"{refusal.message}",
                },
            )
        projection = self._quine.projection(
            payload, receipt.experiment, receipt.barrier, receipt_observed=receipt.observed
        )
        return self._settle_quine(pending, intent, projection)

    def _settle_quine(
        self, strategy: UserStrategy, intent: str, projection: dict[str, Any]
    ) -> UserStrategy:
        """Write one core-derived projection onto a record and publish the strategy."""
        next_strategy = replace(
            strategy,
            updated_at=_now(),
            experiments=tuple(
                {**item, **projection} if item.get("launch_intent_id") == intent else item
                for item in strategy.experiments
            ),
        )
        self._store.save(next_strategy)
        return next_strategy

    def _quine_unreachable(self, record: dict[str, Any]) -> dict[str, Any]:
        """One core-owned record the core cannot be asked about right now.

        The status a cached projection happened to hold is never served as this
        run's state: a result that cannot be read from its owner is unknown here.
        """
        return {
            **record,
            "status": "unknown",
            "slot_held": None,
            "core_revision": None,
            "recovery": None,
            "phase": None,
            "report_ready": False,
            "report_sha256": None,
            "facts": None,
            "message": (
                "the core that owns this experiment could not be reached, so its state "
                "cannot be read here; nothing was re-sent and no cached result is served"
            ),
        }

    def _reconciled_quine(self, identity: str, record: dict[str, Any]) -> dict[str, Any]:
        """Rebuild one core-owned record's projection from the core itself."""
        if self._quine is None:
            return {
                **record,
                "status": "unknown",
                "slot_held": None,
                "core_revision": None,
                "recovery": None,
                "phase": None,
                "report_ready": False,
                "report_sha256": None,
                "facts": None,
                "message": (
                    "this experiment is owned by a quine core, and no core is configured "
                    "for this workspace, so its state cannot be read here"
                ),
            }
        intent = record.get("launch_intent_id")
        if not isinstance(intent, str) or not intent:
            return self._quine_unreachable(record)
        payload = record.get("payload")
        try:
            experiment, barrier = self._quine.query(identity, intent)
        except QuineError:
            return self._quine_unreachable(record)
        return {
            **record,
            **self._quine.projection(
                cast(dict[str, Any], payload if isinstance(payload, dict) else {}),
                experiment,
                barrier,
                receipt_observed=True,
            ),
        }

    def _restore_quine(self, strategy: UserStrategy) -> UserStrategy:
        """Discover the confirmations this strategy's owner holds and keep them.

        The owner's list is the authority for which confirmations exist: a record
        this workspace lost entirely — a wiped projection, a rebuilt workspace — is
        reconstructed from the owner's own frozen bytes, and one it never held is
        added. Nothing is invented, dispatched or re-run: a record whose frozen
        bytes cannot reproduce the confirmation it claims to be is left out, and a
        local record the owner does not know about is kept exactly as it is, so a
        confirmation whose receipt was never observed is never re-sent.
        """
        if self._quine is None:
            return strategy
        identity = strategy.user_strategy_id
        try:
            owned = self._quine.experiments()
        except QuineError:
            # The core cannot be asked right now. Reads report that per record; no
            # local record is added, changed or removed on a failed read.
            return strategy
        known = {item.get("launch_intent_id") for item in strategy.experiments}
        restored: Documents = []
        for experiment in owned:
            if experiment.get("strategy_id") != identity:
                continue
            intent = experiment.get("intent_id")
            if not isinstance(intent, str) or intent in known:
                continue
            record = self._record_from_core(identity, experiment, intent)
            if record is not None:
                restored.append(record)
                known.add(intent)
        if not restored:
            return strategy
        return replace(
            strategy,
            experiments=tuple(
                sorted(
                    (*strategy.experiments, *restored),
                    key=lambda item: str(item.get("created_at") or ""),
                )
            ),
        )

    def _record_from_core(
        self, identity: str, experiment: dict[str, Any], intent: str
    ) -> dict[str, Any] | None:
        """One record rebuilt from the confirmation the owner retained, or ``None``."""
        assert self._quine is not None
        payload = self._quine.payload_of(experiment)
        if payload is None:
            return None
        created_at = experiment.get("created_at")
        return {
            "launch_intent_id": intent,
            "request_id": str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"ea.user-strategy/{identity}/{intent}")
            ),
            "payload_sha256": _digest(payload),
            "payload": payload,
            "owner": _QUINE_OWNER,
            "task_id": None,
            "created_at": (
                datetime.fromtimestamp(int(created_at), UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                if isinstance(created_at, int)
                else _now()
            ),
            **self._quine.projection(payload, experiment, None, receipt_observed=True),
        }

    def _refresh_quine(self, strategy: UserStrategy, identity: str, intent: str) -> UserStrategy:
        """Re-read one core-owned record and publish it only if it moved."""
        record = _entry(strategy.experiments, "launch_intent_id", intent)
        if record is None:
            raise UserStrategyNotFoundError("no experiment matches this launch intent")
        settled = self._reconciled_quine(identity, record)
        if settled == record:
            return strategy
        return replace(
            strategy,
            experiments=tuple(
                settled if item.get("launch_intent_id") == intent else item
                for item in strategy.experiments
            ),
        )

    def cancel_experiment(
        self, user_strategy_id: object, *, launch_intent_id: object
    ) -> UserStrategy:
        """Ask the core to stop one of its own runs. It decides what that means.

        The request is forwarded and never interpreted here: an accepted stop is the
        core's own state, the record that follows is the core's own record, and a
        core that refuses a late cancel is reported with its own refusal.

        The stop reaches the core even when this workspace holds no record of that
        confirmation yet. The barrier that protects a confirmation which has not
        arrived is the core's own, and a missing local projection is not a reason to
        refuse a stop the user really asked for.
        """
        identity = _identifier(user_strategy_id, "user_strategy_id")
        intent = _identifier(launch_intent_id, "launch_intent_id")
        with self._lock(identity):
            strategy = self._store.get(identity)
            if self._quine is None:
                raise UserStrategyStateError(
                    "no experiment core owns this workspace's runs, so there is nothing "
                    "to stop here"
                )
            record = _entry(strategy.experiments, "launch_intent_id", intent)
            if record is not None and record.get("owner") != _QUINE_OWNER:
                raise UserStrategyNotFoundError(
                    "no core-owned experiment matches this launch intent"
                )
            self._quine.cancel(identity, intent)
            restored = self._restore_quine(strategy)
            if record is None:
                return restored
            return self._refresh_quine(restored, identity, intent)

    def recover_unknown_experiment(
        self,
        user_strategy_id: object,
        *,
        launch_intent_id: object,
        action: str,
        attempt_id: str,
        expected_revision: int,
        preparation_id: str,
    ) -> UserStrategy:
        """Release concurrency only; the old intent, input and usage remain intact."""
        identity = _identifier(user_strategy_id, "user_strategy_id")
        intent = _identifier(launch_intent_id, "launch_intent_id")
        with self._lock(identity):
            strategy = self._store.get(identity)
            record = _entry(strategy.experiments, "launch_intent_id", intent)
            if self._quine is None or record is None or record.get("owner") != _QUINE_OWNER:
                raise UserStrategyStateError("no readable core-owned experiment can be recovered")
            experiment, _ = self._quine.query(identity, intent)
            if experiment is None or not self._quine.frozen_matches(
                record.get("payload") or {}, experiment
            ):
                raise UserStrategyStateError(
                    "the core's frozen input does not match this experiment"
                )
            self._quine.recover_unknown(
                identity,
                intent,
                action=action,
                attempt_id=attempt_id,
                expected_revision=expected_revision,
                preparation_id=preparation_id,
            )
            return self._refresh_quine(strategy, identity, intent)

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
            # The owner is asked which confirmations exist before anything is
            # classified, so a projection this workspace lost is rebuilt rather
            # than mistaken for one that never happened.
            restored = self._restore_quine(strategy)
            changed = restored != strategy
            experiments: Documents = []
            for record in restored.experiments:
                # A core-owned record is read back from its owner, never from the
                # projection this process happened to write last.
                settled = (
                    self._reconciled_quine(identity, record)
                    if record.get("owner") == _QUINE_OWNER
                    else self._reconciled(record)
                )
                changed = changed or settled != record
                experiments.append(settled)
            calls: Documents = []
            for call in restored.calls:
                settled_call = self._reconciled_call(identity, call)
                changed = changed or settled_call != call
                calls.append(settled_call)
            next_strategy = replace(restored, experiments=tuple(experiments), calls=tuple(calls))
            if changed:
                self._store.save(next_strategy)
            for root in next_strategy.calls:
                task = root.get("task") or {}
                if task.get("status") != "running":
                    continue
                turn = root["call_id"]
                members = [
                    c
                    for c in next_strategy.calls
                    if c["call_id"] == turn or (c.get("request") or {}).get("turn_id") == turn
                ]
                if any((identity, c["call_id"]) in self._active_calls for c in members):
                    continue
                reason = (
                    "unknown" if any(c["status"] == "unknown" for c in members) else "interrupted"
                )
                next_strategy = self._store.update_task(
                    identity, turn, status="stopped", reason=reason
                )
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

    # -- the conversation graph ---------------------------------------------

    def conversation_turn(
        self,
        user_strategy_id: object,
        *,
        call_id: object,
        base_revision: object,
        text: object,
        approved_budget: Any = None,
        evidence_job_ids: Any = None,
        task_budget: Any = None,
    ) -> Iterator[dict[str, Any]]:
        """One approved turn, run as the conversation graph, yielding its events.

        The user's own message, the approved budget and this turn's reservation are
        durable before the first event, exactly as they were before this graph
        existed, and the events after that are real steps: a provider delta as it
        arrives, a tool the model asked for, a decision this turn is waiting for, an
        observed run and a verified reading. The canonical conversation stays the
        store's, so a lost checkpoint is rebuilt from it rather than from a second
        copy, and it never causes a second provider call.
        """
        identity = _identifier(user_strategy_id, "user_strategy_id")
        with self._lock(identity):
            strategy = self._store.migrate_conversation(identity)
            if approved_budget is None and task_budget is None:
                saved = (_entry(strategy.calls, "call_id", str(call_id)) or {}).get("request") or {}
                approved_budget = (
                    saved["approved_budget"]
                    if saved.get("execution") == "interactive"
                    else {"max_model_calls": _INTERACTIVE_MAX_DISPATCHES, "max_backtests": 0}
                )
                task_budget = "interactive"
            admitted = self._admit(
                strategy,
                call_id=call_id,
                base_revision=base_revision,
                text=text,
                approved_budget=approved_budget,
                action="message",
                proposal_id=None,
                evidence_job_ids=evidence_job_ids,
                expected_evidence=None,
                task_budget=task_budget,
            )
            if isinstance(admitted, UserStrategy):
                # Nothing to dispatch: a replayed identity, or no provider to reach.
                root = _entry(admitted.calls, "call_id", str(call_id)) or {}
                if root.get("task") and root.get("status") == "unconfigured":
                    admitted = self._store.update_task(
                        identity, str(call_id), status="stopped", reason="unconfigured"
                    )
                yield event(STATE, strategy=admitted.document())
                yield event(DONE, strategy=admitted.document())
                return
            yield from self._drive(
                identity,
                admitted.call_id,
                self._seed(admitted),
            )

    def cancel_conversation_task(
        self, user_strategy_id: object, *, turn_id: object
    ) -> UserStrategy:
        identity = _identifier(user_strategy_id, "user_strategy_id")
        turn = _identifier(turn_id, "turn_id")
        return self._store.update_task(identity, turn, status="stopped", reason="cancelled")

    def conversation_budget(
        self, user_strategy_id: object, *, turn_id: object, grant_id: object, total_model_calls: Any
    ) -> Iterator[dict[str, Any]]:
        identity = _identifier(user_strategy_id, "user_strategy_id")
        turn = _identifier(turn_id, "turn_id")
        grant = _identifier(grant_id, "grant_id")
        with self._lock(identity):
            strategy = self._store.update_task(
                identity, turn, grant_id=grant, total=total_model_calls
            )
            pending = strategy.conversation_confirmation
            if (
                not pending
                or pending.get("turn_id") != turn
                or pending.get("phase") != "awaiting_budget"
            ):
                yield event(DONE, strategy=strategy.document())
                return
            if self._turn_remaining(strategy, turn) <= 0:
                # A lost topup reply can be retried after its entire grant was spent.
                # Replaying it changes nothing and must not consume the interrupt.
                yield event(
                    INTERRUPT,
                    waiting={"kind": "budget", "turn_id": turn, "reason": "budget_exhausted"},
                    strategy=strategy.document(),
                )
                return
            yield from self._drive(identity, turn, Command(resume={"action": "budget"}))

    def conversation_continue(
        self, user_strategy_id: object, *, turn_id: object, grant_id: object
    ) -> Iterator[dict[str, Any]]:
        """Explicit continuation of a saved pause; no caller counts or silent retries."""
        identity = _identifier(user_strategy_id, "user_strategy_id")
        turn = _identifier(turn_id, "turn_id")
        grant = _identifier(grant_id, "grant_id")
        with self._lock(identity):
            strategy = self._store.update_task(
                identity, turn, grant_id=grant, continue_execution=True
            )
            pending = strategy.conversation_confirmation or {}
            if pending.get("turn_id") != turn or pending.get("phase") != "awaiting_budget":
                yield event(DONE, strategy=strategy.document())
                return
            if self._turn_remaining(strategy, turn) <= 0:
                yield event(
                    INTERRUPT,
                    waiting={"kind": "budget", "turn_id": turn, "reason": "execution_limit"},
                    strategy=strategy.document(),
                )
                return
            yield from self._drive(identity, turn, Command(resume={"action": "continue"}))

    def conversation_resume(
        self,
        user_strategy_id: object,
        *,
        turn_id: object,
        action: object,
        proposal_id: object = None,
        base_revision: object = None,
        launch_intent_id: object = None,
        preview_identity: object = None,
        grant_id: object = None,
        total_model_calls: Any = None,
    ) -> Iterator[dict[str, Any]]:
        """One explicit human decision, resumed into the turn that is waiting for it.

        Every identity the decision has to name is checked against the pending record
        before the graph is reached, so a decision about another proposal, another
        revision or another preview is refused before it can touch anything.
        """
        identity = _identifier(user_strategy_id, "user_strategy_id")
        turn = _identifier(turn_id, "turn_id")
        if action == "cancel":
            yield event(
                DONE, strategy=self.cancel_conversation_task(identity, turn_id=turn).document()
            )
            return
        if action == "continue":
            yield from self.conversation_continue(identity, turn_id=turn, grant_id=grant_id)
            return
        if action == "budget":
            yield from self.conversation_budget(
                identity, turn_id=turn, grant_id=grant_id, total_model_calls=total_model_calls
            )
            return
        with self._lock(identity):
            strategy = self._store.get(identity)
            root = _entry(strategy.calls, "call_id", turn) or {}
            if (root.get("task") or {}).get("stop_reason") == "cancelled":
                raise UserStrategyStateError("this task was cancelled")
            pending = strategy.conversation_confirmation
            existing = _entry(strategy.experiments, "launch_intent_id", str(launch_intent_id))
            if action == "confirm" and existing is not None:
                # The stored request must belong to this turn's retained proposal.
                proposals = [
                    item
                    for item in strategy.proposals
                    if (call := _entry(strategy.calls, "call_id", str(item.get("call_id"))))
                    and (call.get("turn_id", call.get("call_id")) == turn)
                ]
                valid_intents = {
                    "int-" + _digest({"turn": turn, "tool": item["call_id"]})[:24]
                    for item in proposals
                }
                if str(launch_intent_id) not in valid_intents:
                    raise UserStrategyConflictError("this confirmation belongs to another turn")
                observed = self._launch_experiment_locked(
                    identity,
                    launch_intent_id=launch_intent_id,
                    base_revision=base_revision,
                    preview_identity=preview_identity,
                    approved_budget={"max_model_calls": 0, "max_backtests": 1},
                )
                self._store.save(observed)
                yield event(STATE, strategy=observed.document())
                yield event(DONE, strategy=observed.document())
                return
            if not isinstance(pending, dict) or pending.get("turn_id") != turn:
                raise UserStrategyStateError(
                    "this conversation is not waiting for a decision of that turn"
                )
            payload = self._resume_payload(
                pending,
                action=action,
                proposal_id=proposal_id,
                base_revision=base_revision,
                launch_intent_id=launch_intent_id,
                preview_identity=preview_identity,
            )
            yield from self._drive(identity, turn, Command(resume=payload))

    def conversation_state(self, user_strategy_id: object) -> dict[str, Any]:
        """What a reloaded client needs, without reading a checkpoint itself."""
        identity = _identifier(user_strategy_id, "user_strategy_id")
        strategy = self._store.migrate_conversation(identity)
        pending = strategy.conversation_confirmation
        reserved = int(self._store.model_call_usage()["reserved_model_calls"])
        return {
            "strategy_id": strategy.user_strategy_id,
            "revision": strategy.draft.revision,
            # The one decision this conversation is waiting for, if it is waiting for
            # one, with the preview it was made against kept beside it.
            "pending": None
            if not isinstance(pending, dict)
            else {key: value for key, value in pending.items() if key != "preview"},
            "preview": None if not isinstance(pending, dict) else pending.get("preview"),
            "open_proposals": [
                str(item.get("proposal_id"))
                for item in strategy.proposals
                if item.get("status") == "open"
            ],
            "task": self._task_projection(strategy),
            "budget": {
                "task_budget_required": False,
                "max_task_requests": 100,
                "max_interactive_requests": _INTERACTIVE_MAX_DISPATCHES,
                # The most calls one explicitly initiated new turn may spend. It is a
                # per-turn bound, deliberately separate from the recorded usage below,
                # so a client can never read a lifetime balance out of this and disable
                # a user's next turn.
                "max_model_calls_per_turn": _TURN_MAX_CALLS,
                # The model calls this workspace has recorded, as recorded usage — an
                # accounting figure, not a remaining balance or a provider credit.
                "reserved_model_calls": reserved,
                "max_backtests": self._workspace_budget()["max_backtests"],
            },
        }

    @staticmethod
    def _task_projection(strategy: UserStrategy) -> dict[str, Any] | None:
        roots = [item for item in strategy.calls if "task" in item]
        if not roots:
            return None
        root = roots[-1]
        task = root["task"]
        used = _task_usage(strategy, root["call_id"])
        return {
            **task,
            "turn_id": root["call_id"],
            "used": used,
            "remaining": max(task["limit"] - used, 0),
        }

    def _task_cancelled(self, state: dict[str, Any]) -> bool:
        root = (
            _entry(self._store.get(state["strategy_id"]).calls, "call_id", state["turn_id"]) or {}
        )
        return (root.get("task") or {}).get("stop_reason") == "cancelled"

    def _graph_budget(self, state: dict[str, Any]) -> dict[str, Any]:
        if not state.get("task_budget"):
            return state
        if self._task_cancelled(state):
            return {**state, "phase": "cancelled", "stop_reason": "cancelled"}
        strategy = self._store.get(state["strategy_id"])
        call = (
            state["turn_id"]
            if state["step"] == 0
            else _child_call_id(state["turn_id"], state["step"])
        )
        waiting = strategy.conversation_confirmation or {}
        if (
            state.get("phase") != "recover_budget"
            and waiting.get("phase") != "awaiting_budget"
            and (
                _entry(strategy.calls, "call_id", call)
                or self._turn_remaining(strategy, state["turn_id"]) > 0
            )
        ):
            return {**state, "phase": "ready"}
        root = _entry(strategy.calls, "call_id", state["turn_id"]) or {}
        pause_reason = (
            "execution_limit"
            if (root.get("task") or {}).get("mode") == "interactive"
            else "budget_exhausted"
        )
        self._store.update_task(
            state["strategy_id"],
            state["turn_id"],
            status="awaiting_budget",
            reason=pause_reason,
        )
        self._remember(
            state["strategy_id"],
            {
                "phase": "awaiting_budget",
                "turn_id": state["turn_id"],
                "proposal_id": state["proposal_id"],
                "base_revision": state["base_revision"],
                "launch_intent_id": state["launch_intent_id"],
                "preview_identity": state["preview_identity"],
                "job_id": state["job_id"],
                "at": _now(),
            },
        )
        interrupt({"kind": "budget", "turn_id": state["turn_id"], "reason": pause_reason})
        if self._task_cancelled(state):
            return {**state, "phase": "cancelled", "stop_reason": "cancelled"}
        if self._turn_remaining(self._store.get(state["strategy_id"]), state["turn_id"]) <= 0:
            raise UserStrategyBudgetError("additional explicit authorization is required")
        self._forget(state["strategy_id"])
        self._store.update_task(
            state["strategy_id"], state["turn_id"], status="running", reason=None
        )
        return {**state, "phase": "ready"}

    def _seed(self, admitted: _AdmittedTurn) -> dict[str, Any]:
        """The graph state of one new turn: references and identities, no facts.

        Every value is a JSON primitive, so a checkpoint can hold only what the store
        itself could hold, and the conversation itself is never copied into it.
        """
        return {
            "strategy_id": admitted.strategy.user_strategy_id,
            "task_budget": bool(
                (_entry(admitted.strategy.calls, "call_id", admitted.call_id) or {}).get("task")
            ),
            "consecutive_failures": 0,
            "turn_id": admitted.call_id,
            "message": admitted.message,
            "base_revision": admitted.base_revision,
            "approved": dict(admitted.approved_budget),
            "evidence_job_ids": list(admitted.evidence_job_ids),
            "step": 0,
            "call_id": admitted.call_id,
            "exchange": [],
            "pending": [],
            "proposal_id": "",
            "proposal_call": "",
            "proposal_revision": 0,
            "launch_intent_id": "",
            "preview_identity": "",
            "job_id": "",
            "phase": "",
            "answer": {},
            "text": "",
            "refusal": "",
            "explain": False,
        }

    # -- nodes ---------------------------------------------------------------

    def _graph_answer(self, state: dict[str, Any]) -> dict[str, Any]:
        """One provider call of this turn: reserved, streamed, and recorded whole."""
        identity = state["strategy_id"]
        strategy = self._store.get(identity)
        step = int(state["step"])
        call = state["turn_id"] if step == 0 else _child_call_id(state["turn_id"], step)
        draft = strategy.draft
        if isinstance(draft, IntentDraft):
            # An intent may inspect resources but cannot bind, propose or run.
            registered: tuple[RegisteredPath, ...] = ()
            system = _INTENT_SYSTEM_PROMPT
            tools: Tools = self._domain_tools(intent=True)
            required: tuple[str, ...] = ("tool_use",)
        else:
            registered = self._registered_paths(draft)
            system = _SYSTEM_PROMPT
            tools = self._conversation_tools(registered)
            required = ("tool_use",)
        selection = self._evidence_selection(state["evidence_job_ids"] or None)
        evidence = None if selection is None else self._evidence_context(strategy, selection)
        messages = self._answer_messages(strategy, state, registered, evidence)
        record = _entry(strategy.calls, "call_id", call)
        if record is None:
            # A further call of this turn is reserved exactly where the turn's first
            # one was, before the provider can be reached.
            try:
                strategy = self._reserve_child(strategy, call, state)
            except UserStrategyError as error:
                # The ceiling refused it: this turn ends on its own already-settled
                # record, and says why, without reaching any provider.
                return {
                    **state,
                    "call_id": state["turn_id"],
                    "refusal": str(error),
                    "skip_settle": True,
                    "phase": "done",
                    "pending": [],
                }
            record = _entry(strategy.calls, "call_id", call)
        stored = _response_from((record or {}).get("response"))
        response: ModelResponse | None
        if stored is not None:
            # The answer this identity already recorded is the answer: a replayed node
            # or a checkpoint rebuilt from nothing never reaches the provider twice.
            response, status, detail = stored, "observed", None
        elif (record or {}).get("status") != "pending":
            return {
                **state,
                "call_id": call,
                "step": step + 1,
                "pending": [],
                "text": "",
                "outcome": "unknown"
                if (record or {}).get("status") == "dispatched"
                else str((record or {}).get("status") or "unknown"),
                "refusal": (
                    "a model call was interrupted after it may have reached the provider; "
                    "its answer was never recorded, and it was not sent again"
                ),
                "phase": "done",
            }
        else:
            response, status, detail = self._dispatch_call(
                strategy,
                call,
                state,
                system=system,
                messages=messages,
                tools=tools,
                required=required,
            )
        if response is None:
            return {
                **state,
                "stop_reason": status,
                "call_id": call,
                "step": step + 1,
                "pending": [],
                "text": "",
                "refusal": detail or "the model provider did not answer",
                "outcome": status,
                "phase": "done",
            }
        try:
            text = _bounded(response.text, "response", _MAX_RESPONSE, required=False)
        except UserStrategyError as error:
            return {
                **state,
                "call_id": call,
                "step": step + 1,
                "pending": [],
                "text": "",
                "refusal": str(error),
                "phase": "done",
            }
        # Publish validated intermediate text once before another tool/model step.
        self._settle(
            self._store.get(identity),
            call,
            "observed",
            None,
            response,
            text=text,
            message_id=f"m-{call}",
        )
        # A model that proposes on an unbound intent is refused whole, exactly as the
        # single-call path refuses it: nothing an intent cannot have is ever created.
        refusal = (
            self._clarification_refusal(response)
            if isinstance(draft, IntentDraft)
            and any(
                call.name not in {_DISCOVER_TOOL, _INSPECT_INPUT_TOOL}
                for call in response.tool_calls
            )
            else None
        )
        if refusal is not None:
            return {
                **state,
                "call_id": call,
                "step": step + 1,
                "pending": [],
                "text": "",
                "refusal": refusal,
                "phase": "done",
            }
        return {
            **state,
            "call_id": call,
            "step": step + 1,
            "exchange": [
                *state["exchange"],
                {
                    "role": "assistant",
                    "text": response.text,
                    "calls": [
                        {"id": item.id, "name": item.name, "arguments": item.arguments}
                        for item in response.tool_calls
                    ],
                },
            ],
            "pending": [
                {"id": item.id, "name": item.name, "arguments": item.arguments}
                for item in response.tool_calls
            ],
            "text": response.text,
            "refusal": "",
            "phase": "",
        }

    def _answer_messages(
        self,
        strategy: UserStrategy,
        state: dict[str, Any],
        registered: tuple[RegisteredPath, ...],
        evidence: Documents | None,
    ) -> History:
        """One request for this step: the recorded context, then what is asked of it.

        The retained exchange comes first and is never edited, so the explanation of
        a run still carries the proposal receipt this turn recorded before the user
        applied the change and confirmed a preview. When this step is that
        explanation, the host's own record of the workflow that has since happened is
        stated after that exchange and before the instruction to answer, and a
        workflow that does not join is refused here — before this call is reserved
        and before any provider could be reached — rather than explained.
        """
        # The canonical record includes earlier calls of this turn as they settle.
        # They are represented once, with their tool relationships, by exchange.
        turn_start = next(
            (
                index
                for index, item in enumerate(strategy.messages)
                if item.get("role") == "user" and item.get("call_id") == state["turn_id"]
            ),
            len(strategy.messages),
        )
        context = replace(strategy, messages=strategy.messages[: turn_start + 1])
        messages = list(self._conversation(context, state["message"], registered, evidence))
        messages.extend(self._exchange_messages(state["exchange"]))
        if state["explain"]:
            messages.append(
                ModelMessage(role="user", text=self._applied_update(strategy, state, evidence))
            )
            messages.append(ModelMessage(role="user", text=_EXPLAIN_INSTRUCTION))
        return messages

    def _applied_update(
        self, strategy: UserStrategy, state: dict[str, Any], evidence: Documents | None
    ) -> str:
        """The host's own record of the applied change, its confirmation and its run.

        A turn that proposed a change carries that proposal's own receipt in its
        retained exchange: at the moment it was written, nothing was applied and no
        experiment had run, and that is what it still says. This update states, from
        this strategy's persisted records, the workflow that has happened since, so
        the explanation that follows reads the receipt as the history it is. Nothing
        is rewritten or removed, and the model's answer is never edited.

        The graph's own values are lookup references only. The applied revision is
        read from the persisted proposal and its one apply change, not from today's
        draft, because a user may have advanced the current draft after confirming
        the run this turn explains; the launch, the run and the report are read from
        the already-verified evidence of this turn and joined back to exactly one
        experiment of this strategy. A binding that does not join is refused whole.
        """
        if evidence is None or len(evidence) != 1:
            raise UserStrategyStateError(
                "this explanation is not grounded in exactly one verified run of this "
                "strategy; nothing was asked"
            )
        verified = evidence[0]
        proposal = str(state["proposal_id"])
        applied = self._applied_revision(strategy, proposal)
        if applied is None:
            raise UserStrategyStateError(
                "this turn's proposal is not recorded as applied to the revision it was "
                "proposed on, so there is no applied change to explain; nothing was asked"
            )
        if verified.get("draft_revision") != applied:
            raise UserStrategyStateError(
                "the verified run this turn explains was frozen on another draft revision "
                "than the one this proposal applied; nothing was asked"
            )
        intent = str(state["launch_intent_id"])
        preview = str(state["preview_identity"])
        job = str(state["job_id"])
        if any(
            not isinstance(verified.get(field), str) or not verified[field]
            for field in ("job_id", "launch_intent_id", "run_id", "input_sha256", "report_sha256")
        ):
            raise UserStrategyStateError(
                "the verified run this turn explains does not state the identities an "
                "explanation would name; nothing was asked"
            )
        confirmed = [
            item
            for item in strategy.experiments
            if item.get("status") == "created"
            and item.get("job_id") == job
            and item.get("job_id") == verified["job_id"]
            and item.get("launch_intent_id") == intent
            and item.get("launch_intent_id") == verified["launch_intent_id"]
            and cast(dict[str, Any], item.get("payload") or {}).get("preview_identity") == preview
        ]
        if len(confirmed) != 1:
            raise UserStrategyStateError(
                "the launch, its run and the verified report this turn explains are not one "
                "experiment of this strategy; nothing was asked"
            )
        current = strategy.draft.revision
        draft_move = (
            ""
            if current == applied
            else f" It is a later draft than the frozen revision {applied} this run was made from."
        )
        return "\n".join(
            [
                "The host's own record of the workflow that has already happened here. The "
                "proposal receipt in the turns above is the history of the moment it was "
                "written: it describes the proposal as not applied and no experiment as run, "
                "and this update is what supersedes it. Nothing above is rewritten or removed.",
                f"Proposal {proposal} was applied to the revision it was proposed "
                f"on: this host records one apply change for it, moving draft revision "
                f"{applied - 1} to the applied revision {applied}.",
                f"That applied revision {applied} and its exact admitted input were separately "
                f"confirmed by the user, under the persisted launch intent {intent} and the "
                f"preview identity {preview}.",
                f"The one run of this strategy for that confirmation, job {job}, engine run "
                f"{verified['run_id']}, completed successfully and published the verified "
                f"report with digest {verified['report_sha256']}, over the confirmed input "
                f"digest {verified['input_sha256']}.",
                f"That verified report belongs to this applied change: it is the report of the "
                f"run launched from the revision this proposal applied, revision {applied}.",
                f"The current draft revision is {current}.{draft_move}",
                "These are this host's own records of the workflow and its identities, not a "
                "second reading of the run: the verified numbers and limitations stated above "
                "are unchanged, and nothing beyond them may be stated.",
            ]
        )

    def _graph_tools(self, state: dict[str, Any]) -> dict[str, Any]:
        """Run the one tool the model asked for, inside this workspace's own facts.

        The two reads answer from the host's own verified readings and change nothing.
        The proposal tool is the same typed diff every other path validates: an answer
        it refuses refuses this turn, so no partial change can ever be created from a
        malformed one, and the proposal it does accept is recorded once, under an
        identity derived from this turn and this tool call.
        """
        strategy = self._store.get(state["strategy_id"])
        if self._task_cancelled(state):
            return {**state, "pending": [], "refusal": "task cancelled", "stop_reason": "cancelled"}
        calls = list(state["pending"])
        if len(calls) != 1:
            return {
                **state,
                "pending": [],
                "refusal": ("the model asked for more than one thing at once; nothing was changed"),
            }
        item = calls[0]
        name, tool_id = str(item["name"]), str(item["id"])
        self._emit(event(TOOL_START, tool=name, tool_id=tool_id, call_id=state["call_id"]))
        if name in _DOMAIN_READ_TOOLS:
            record = _entry(strategy.calls, "call_id", state["call_id"]) or {}
            receipt = _entry(tuple(record.get("tool_results", [])), "tool_id", tool_id)
            if receipt is None:
                try:
                    read_facts = self._domain_read(strategy, name, item["arguments"], state)
                except Exception as error:  # noqa: BLE001 — failed reads remain explicit
                    read_facts = {"ok": False, "error": str(error)}
                receipt = {
                    "tool": name,
                    "tool_id": tool_id,
                    "status": "done" if read_facts.get("ok") else "failed",
                    "text": json.dumps(read_facts, ensure_ascii=False),
                }
                recorded_calls = tuple(
                    {**call, "tool_results": [*call.get("tool_results", []), receipt]}
                    if call["call_id"] == state["call_id"]
                    else call
                    for call in strategy.calls
                )
                self._store.save(replace(strategy, calls=recorded_calls, updated_at=_now()))
            text = str(receipt["text"])
            if receipt["status"] == "failed":
                self._emit(
                    event(
                        TOOL_ERROR, tool=name, tool_id=tool_id, error=str(json.loads(text)["error"])
                    )
                )
            else:
                self._emit(event(TOOL_END, tool=name, tool_id=tool_id, text=text))
            failures = (
                int(state.get("consecutive_failures", 0)) + 1
                if receipt["status"] == "failed"
                else 0
            )
            return {
                **state,
                "consecutive_failures": failures,
                "refusal": "consecutive tool failures" if failures >= 2 else "",
                "stop_reason": "consecutive_failures" if failures >= 2 else None,
                "pending": [],
                "exchange": [
                    *state["exchange"],
                    {
                        "role": "tool",
                        "call_id": tool_id,
                        "text": text,
                    },
                ],
            }
        if name in {_DESCRIBE_DATA_TOOL, _EXPERIMENT_STATUS_TOOL}:
            try:
                facts = (
                    self._data_version_facts(_executable(strategy))
                    if name == _DESCRIBE_DATA_TOOL
                    else self._experiment_facts(strategy)
                )
            except Exception as error:  # noqa: BLE001 — an unreadable fact is stated
                self._emit(event(TOOL_ERROR, tool=name, tool_id=tool_id, error=str(error)))
                return {
                    **state,
                    "pending": [],
                    "refusal": (f"{name} could not be read in this workspace: {error}"),
                }
            text = "\n".join(facts) or "this workspace states nothing it can verify here"
            # The receipt also restores failure accounting if a checkpoint is lost.
            recorded_reads = tuple(
                {
                    **call,
                    "tool_results": [
                        {"tool": name, "tool_id": tool_id, "status": "done", "text": text}
                    ],
                }
                if call["call_id"] == state["call_id"]
                else call
                for call in strategy.calls
            )
            self._store.save(replace(strategy, calls=recorded_reads, updated_at=_now()))
            self._emit(event(TOOL_END, tool=name, tool_id=tool_id, text=text))
            return {
                **state,
                "consecutive_failures": 0,
                "pending": [],
                "exchange": [
                    *state["exchange"],
                    {"role": "tool", "call_id": tool_id, "text": text},
                ],
            }
        if name != _PROPOSE_TOOL_NAME:
            self._emit(event(TOOL_ERROR, tool=name, tool_id=tool_id, error="unsupported tool"))
            return {
                **state,
                "pending": [],
                "refusal": (
                    "the model used a tool this conversation does not offer; nothing was changed"
                ),
            }
        if strategy.draft.revision != state["base_revision"]:
            stale_refusal = (
                "the draft revision changed while this answer was pending; nothing was proposed"
            )
            self._emit(event(TOOL_ERROR, tool=name, tool_id=tool_id, error=stale_refusal))
            return {**state, "pending": [], "refusal": stale_refusal}
        probe = ModelResponse(
            "", (ToolCall(tool_id, name, dict(item["arguments"])),), "tool_calls", "", None, None
        )
        items, refusal = self._diff(strategy, probe)
        if refusal is not None:
            self._emit(event(TOOL_ERROR, tool=name, tool_id=tool_id, error=refusal))
            return {**state, "pending": [], "refusal": refusal}
        proposal_id = f"prop-{_digest({'turn': state['turn_id'], 'tool': tool_id})[:24]}"
        revision = strategy.draft.revision
        existing = _entry(strategy.proposals, "proposal_id", proposal_id)
        if existing is None:
            strategy = self._open_proposal(strategy, proposal_id, state["call_id"], items)
        elif existing.get("status") != "open" or existing.get("items") != items:
            return {
                **state,
                "pending": [],
                "refusal": ("this turn already proposed something else; nothing was changed again"),
            }
        text = (
            "The proposal was recorded for the user's own decision. It is not applied "
            "here, and no experiment may be run from it without the user applying it "
            "and then confirming a preview."
        )
        self._emit(event(TOOL_END, tool=name, tool_id=tool_id, text=text))
        return {
            **state,
            "pending": [],
            "exchange": [
                *state["exchange"],
                {"role": "tool", "call_id": tool_id, "text": text},
            ],
            "proposal_id": proposal_id,
            "proposal_call": state["call_id"],
            "proposal_revision": revision,
            "phase": "proposed",
        }

    def _graph_settle(self, state: dict[str, Any]) -> dict[str, Any]:
        """Write this call's own outcome and message, once, under a stable identity."""
        if state.get("skip_settle"):
            self._emit(event(ERROR, kind_reason="budget", error=str(state["refusal"])))
            return state
        strategy = self._store.get(state["strategy_id"])
        call = state["call_id"]
        refusal = str(state["refusal"])
        proposal_id = str(state["proposal_id"])
        self._settle(
            strategy,
            call,
            str(state.get("outcome") or ("refused" if refusal else "observed")),
            refusal or None,
            _response_from((_entry(strategy.calls, "call_id", call) or {}).get("response")),
            text=str(state["text"]),
            proposal_id=proposal_id if proposal_id and state["proposal_call"] == call else None,
            record={"turn_id": state["turn_id"], "step": int(state["step"])},
            message_id=f"m-{call}",
        )
        return {**state, "phase": "proposed" if state["phase"] == "proposed" else "done"}

    def _graph_proposal(self, state: dict[str, Any]) -> dict[str, Any]:
        """Wait for the user's own decision on one recorded proposal."""
        waiting = {
            "kind": "proposal",
            "turn_id": state["turn_id"],
            "proposal_id": state["proposal_id"],
            "base_revision": int(state["proposal_revision"]),
        }
        self._remember(
            state["strategy_id"],
            {
                "phase": _PHASE_PROPOSAL,
                "turn_id": state["turn_id"],
                "proposal_id": state["proposal_id"],
                "base_revision": int(state["proposal_revision"]),
                "launch_intent_id": None,
                "preview_identity": None,
                "job_id": None,
                "at": _now(),
            },
        )
        answered = interrupt(waiting)
        answer = answered if isinstance(answered, dict) else {}
        strategy = self._store.get(state["strategy_id"])
        action = answer.get("action")
        if action == "rejected":
            proposal = _entry(strategy.proposals, "proposal_id", str(state["proposal_id"]))
            if proposal is None or proposal.get("status") != "rejected":
                return self._waiting_decision(
                    state, "that proposal was not rejected, so nothing was decided"
                )
            self._forget(state["strategy_id"])
            return {**state, "phase": "declined"}
        if action == "applied":
            if not self._applied_at(
                strategy, str(state["proposal_id"]), int(state["proposal_revision"])
            ):
                return self._waiting_decision(
                    state,
                    "this proposal is not applied to the revision it was proposed on; apply "
                    "it through the proposal's own operation first",
                )
            self._forget(state["strategy_id"])
            return {**state, "phase": "applied"}
        return self._waiting_decision(state, "an unknown decision was sent for this proposal")

    def _graph_preview(self, state: dict[str, Any]) -> dict[str, Any]:
        """Freeze what one confirmation would run, then wait for that confirmation."""
        identity = state["strategy_id"]
        strategy = self._store.get(identity)
        record = strategy.conversation_confirmation
        if isinstance(record, dict) and record.get("phase") == _PHASE_OBSERVATION:
            return {
                **state,
                "phase": "confirmed",
                "launch_intent_id": record["launch_intent_id"],
                "preview_identity": record["preview_identity"],
                "job_id": str(record.get("job_id") or ""),
            }
        if not isinstance(record, dict) or record.get("phase") != _PHASE_CONFIRMATION:
            if not self._applied_at(
                strategy, str(state["proposal_id"]), int(state["proposal_revision"])
            ):
                return self._refused_decision(state, "the applied draft moved before its preview")
            draft = _executable(strategy)
            admitted = self._admitted_request(draft)
            preview = self._preview_of(strategy, draft, admitted)
            record = {
                "phase": _PHASE_CONFIRMATION,
                "turn_id": state["turn_id"],
                "proposal_id": state["proposal_id"],
                "base_revision": draft.revision,
                # Stable for this turn and this proposal, so a lost reply resolves to
                # the same intent instead of confirming a second one.
                "launch_intent_id": "int-"
                + _digest({"turn": state["turn_id"], "tool": state["proposal_call"]})[:24],
                "preview_identity": self._preview_identity(draft, admitted),
                "job_id": None,
                "preview": preview,
                "at": _now(),
            }
            self._remember(identity, record)
        waiting = {
            "kind": "confirmation",
            "turn_id": state["turn_id"],
            "launch_intent_id": record["launch_intent_id"],
            "base_revision": record["base_revision"],
            "preview_identity": record["preview_identity"],
            "preview": record["preview"],
        }
        answered = interrupt(waiting)
        answer = answered if isinstance(answered, dict) else {}
        if answer.get("action") == "declined":
            self._forget(identity)
            return {**state, "phase": "declined"}
        if answer.get("action") != "confirm":
            return self._waiting_decision(state, "an unknown decision was sent for this preview")
        # Only this exact confirmation may run: the persisted intent, the revision it
        # was previewed on and the identity of that preview, and nothing from the
        # turn's model budget or from anything the model wrote.
        try:
            strategy = self._launch_experiment_locked(
                identity,
                launch_intent_id=record["launch_intent_id"],
                base_revision=record["base_revision"],
                preview_identity=record["preview_identity"],
                approved_budget={"max_model_calls": 0, "max_backtests": 1},
            )
        except UserStrategyError as error:
            self._forget(identity)
            return self._refused_decision(state, str(error))
        experiment = _entry(strategy.experiments, "launch_intent_id", record["launch_intent_id"])
        job_id = "" if experiment is None else str(experiment.get("job_id") or "")
        self._remember(
            identity,
            {
                **record,
                "phase": _PHASE_OBSERVATION,
                "job_id": job_id or None,
                "at": _now(),
            },
        )
        return {
            **state,
            "phase": "confirmed",
            "launch_intent_id": record["launch_intent_id"],
            "preview_identity": record["preview_identity"],
            "job_id": job_id,
        }

    def _graph_observe(self, state: dict[str, Any]) -> dict[str, Any]:
        """Read the one confirmed run's own status, and wait while it is still going.

        Nothing here dispatches anything: the run was started by the user's own
        confirmation, and a resume of this node only reads its status again.
        """
        strategy = self._store.get(state["strategy_id"])
        experiment = _entry(strategy.experiments, "launch_intent_id", state["launch_intent_id"])
        if experiment is not None and experiment.get("owner") == _QUINE_OWNER:
            strategy = self._refresh_quine(
                strategy, state["strategy_id"], state["launch_intent_id"]
            )
            self._store.save(strategy)
            experiment = _entry(strategy.experiments, "launch_intent_id", state["launch_intent_id"])
        if experiment is None:
            return self._refused_decision(state, "this launch has no record of its own")
        job_id = str(state["job_id"] or experiment.get("job_id") or "")
        status = experiment.get("status")
        if status in {"confirmed", "running", "stopping"}:
            self._emit(event(STATE, job_id=job_id, run_status=status))
            interrupt(
                {
                    "kind": "observation",
                    "turn_id": state["turn_id"],
                    "job_id": job_id,
                    "status": status,
                }
            )
            return {**state, "phase": "observing", "job_id": job_id}
        if status != "created" or not job_id:
            message = cast(str | None, experiment.get("message")) or (
                f"the run this confirmation started is {status}"
            )
            self._forget(state["strategy_id"])
            self._emit(event(ERROR, kind_reason="experiment", error=message))
            return {**state, "phase": "failed"}
        try:
            running = self._service.get_job(job_id).status
        except Exception as error:  # noqa: BLE001 — an unreadable run is stated, not guessed
            self._forget(state["strategy_id"])
            self._emit(event(ERROR, kind_reason="experiment", error=str(error)))
            return {**state, "phase": "failed"}
        if running in {"accepted", "running"}:
            self._emit(event(STATE, job_id=job_id, run_status=running))
            interrupt(
                {
                    "kind": "observation",
                    "turn_id": state["turn_id"],
                    "job_id": job_id,
                    "status": running,
                }
            )
            # The run may still be going; this node is entered again and reads its
            # status afresh. Nothing was launched twice and nothing was polled here.
            return {**state, "phase": "observing"}
        if running != "succeeded":
            self._forget(state["strategy_id"])
            self._emit(event(ERROR, kind_reason="experiment", error=f"the run finished {running}"))
            return {**state, "phase": "failed"}
        self._forget(state["strategy_id"])
        return {**state, "phase": "observed", "job_id": job_id}

    def _graph_evidence(self, state: dict[str, Any]) -> dict[str, Any]:
        """Read the run's formal report through this workspace's verified authority."""
        job_id = str(state["job_id"])
        strategy = self._store.get(state["strategy_id"])
        try:
            self._evidence_context(strategy, [job_id])
        except UserStrategyError as error:
            self._emit(event(ERROR, kind_reason="evidence", error=str(error)))
            return {**state, "phase": "done", "explain": False}
        remaining = self._turn_remaining(strategy, state["turn_id"])
        self._emit(event(STATE, job_id=job_id, verified=True, remaining_model_calls=remaining))
        if remaining <= 0 and not state.get("task_budget"):
            # The turn's own approval is spent. The verified reading is still durable
            # in the run's report; this conversation simply does not spend a call it
            # was not granted to explain it.
            self._emit(
                event(
                    ERROR,
                    kind_reason="budget",
                    error=(
                        "the run finished and its verified report is available, but this "
                        "turn has no approved model call left to explain it"
                    ),
                )
            )
            return {**state, "phase": "done", "explain": False}
        return {**state, "phase": "evidenced", "explain": True, "evidence_job_ids": [job_id]}

    # -- routing -------------------------------------------------------------

    @staticmethod
    def _route_after_answer(state: dict[str, Any]) -> str:
        if state.get("refusal"):
            return "settle"
        return "tools" if state.get("pending") else "settle"

    @staticmethod
    def _route_after_tools(state: dict[str, Any]) -> str:
        if state.get("refusal") or state.get("phase") == "proposed":
            return "settle"
        if state.get("task_budget"):
            return "budget"
        remaining = int(state["approved"]["max_model_calls"]) - int(state["step"])
        return "answer" if remaining > 0 else "settle"

    @staticmethod
    def _route_after_settle(state: dict[str, Any]) -> str:
        return "proposal" if state.get("phase") == "proposed" else END

    @staticmethod
    def _route_after_proposal(state: dict[str, Any]) -> str:
        if state.get("phase") == "applied":
            return "preview"
        # A refused decision waits on the same interrupt rather than ending the turn.
        return "proposal" if state.get("phase") == "waiting" else END

    @staticmethod
    def _route_after_preview(state: dict[str, Any]) -> str:
        if state.get("phase") == "confirmed":
            return "observe"
        return "preview" if state.get("phase") == "waiting" else END

    @staticmethod
    def _route_after_observe(state: dict[str, Any]) -> str:
        if state.get("phase") == "observing":
            return "observe"
        return "evidence" if state.get("phase") == "observed" else END

    @staticmethod
    def _route_after_evidence(state: dict[str, Any]) -> str:
        if not state.get("explain"):
            return END
        if state.get("task_budget"):
            return "budget"
        remaining = int(state["approved"]["max_model_calls"]) - int(state["step"])
        return "answer" if remaining > 0 else END

    def _compile_conversation(self, saver: Any) -> Any:
        """The one graph of a conversation turn, over this workspace's own checkpoints."""
        builder: Any = StateGraph(dict)
        builder.add_node("budget", self._graph_budget)
        builder.add_node("answer", self._graph_answer)
        builder.add_node("tools", self._graph_tools)
        builder.add_node("settle", self._graph_settle)
        builder.add_node("proposal", self._graph_proposal)
        builder.add_node("preview", self._graph_preview)
        builder.add_node("observe", self._graph_observe)
        builder.add_node("evidence", self._graph_evidence)
        builder.add_conditional_edges(
            START,
            lambda state: {
                "recover_proposal": "proposal",
                "recover_preview": "preview",
                "recover_observe": "observe",
                "recover_budget": "budget",
            }.get(state.get("phase"), "budget" if state.get("task_budget") else "answer"),
            {name: name for name in ("answer", "proposal", "preview", "observe", "budget")},
        )
        builder.add_conditional_edges(
            "budget",
            lambda state: END if state.get("phase") == "cancelled" else "answer",
            {END: END, "answer": "answer"},
        )
        builder.add_conditional_edges(
            "answer", self._route_after_answer, {"tools": "tools", "settle": "settle"}
        )
        builder.add_conditional_edges(
            "tools",
            self._route_after_tools,
            {"answer": "answer", "settle": "settle", "budget": "budget"},
        )
        builder.add_conditional_edges(
            "settle", self._route_after_settle, {"proposal": "proposal", END: END}
        )
        builder.add_conditional_edges(
            "proposal",
            self._route_after_proposal,
            {"proposal": "proposal", "preview": "preview", END: END},
        )
        builder.add_conditional_edges(
            "preview",
            self._route_after_preview,
            {"preview": "preview", "observe": "observe", END: END},
        )
        builder.add_conditional_edges(
            "observe",
            self._route_after_observe,
            {"observe": "observe", "evidence": "evidence", END: END},
        )
        builder.add_conditional_edges(
            "evidence",
            self._route_after_evidence,
            {"answer": "answer", "budget": "budget", END: END},
        )
        return builder.compile(checkpointer=saver)

    def _drive(self, identity: str, turn: str, payload: Any) -> Iterator[dict[str, Any]]:
        """Run one graph step-set and yield the events it really produced."""
        config = thread_config(identity, turn)
        root = _entry(self._store.get(identity).calls, "call_id", turn) or {}
        config["recursion_limit"] = 32 + 4 * int(
            (root.get("task") or {}).get("limit", _TURN_MAX_CALLS)
        )
        with checkpoints(self._store.conversation_db_path, _CONVERSATION_SCHEMA) as saver:
            graph = self._compile_conversation(saver)
            try:
                # Ambient tracing is explicitly overridden: this workspace never
                # initializes a tracing client and never sends a conversation anywhere
                # but the configured model endpoint.
                with langsmith.tracing_context(enabled=False):
                    if isinstance(payload, Command) and not graph.get_state(config).values:
                        recovered = self._recover_decision(identity, turn)
                        graph.invoke(recovered, config)
                    for mode, chunk in graph.stream(
                        payload, config, stream_mode=["custom", "updates"]
                    ):
                        if mode == "custom" and isinstance(chunk, dict):
                            yield chunk
                    snapshot = graph.get_state(config)
            except Exception as error:  # noqa: BLE001 — a failed step is reported, not hidden
                if root.get("task"):
                    self._store.update_task(identity, turn, status="stopped", reason="interrupted")
                yield event(ERROR, error=str(error)[:400], code=type(error).__name__)
                return
        strategy = self._store.get(identity)
        waiting = [
            item.value for task in getattr(snapshot, "tasks", ()) for item in task.interrupts
        ]
        if root.get("task"):
            values = snapshot.values or {}
            reason = values.get("stop_reason") or (
                "scope_decision"
                if values.get("refusal")
                else "declined"
                if values.get("phase") == "declined"
                else "experiment_failed"
                if values.get("phase") == "failed"
                else "completed"
            )
            if waiting:
                phase = (strategy.conversation_confirmation or {}).get("phase")
                strategy = self._store.update_task(
                    identity,
                    turn,
                    status="awaiting_budget" if phase == "awaiting_budget" else "awaiting_decision",
                    reason=(self._task_projection(strategy) or {}).get(
                        "stop_reason", "budget_exhausted"
                    )
                    if phase == "awaiting_budget"
                    else "decision_required",
                )
            else:
                strategy = self._store.update_task(
                    identity,
                    turn,
                    status="completed" if reason == "completed" else "stopped",
                    reason=reason,
                )
        if waiting:
            yield event(
                INTERRUPT,
                waiting=waiting[0] if len(waiting) == 1 else waiting,
                strategy=strategy.document(),
            )
            return
        yield event(DONE, strategy=strategy.document())

    # -- the graph's own durable helpers -------------------------------------

    def _recover_decision(self, identity: str, turn: str) -> dict[str, Any]:
        strategy = self._store.get(identity)
        pending = strategy.conversation_confirmation
        root = _entry(strategy.calls, "call_id", turn)
        if not isinstance(pending, dict) or root is None:
            raise UserStrategyStateError("no durable decision can restore this checkpoint")
        request = root["request"]
        seeded = self._seed(
            _AdmittedTurn(
                strategy,
                turn,
                request["text"],
                request["base_revision"],
                root["approved_budget"],
                list(request.get("evidence_job_ids") or []),
            )
        )
        proposal = _entry(strategy.proposals, "proposal_id", str(pending.get("proposal_id")))
        if pending.get("phase") == "awaiting_budget":
            calls = [
                item
                for item in strategy.calls
                if item["call_id"] == turn or (item.get("request") or {}).get("turn_id") == turn
            ]
            exchange = []
            failures = 0
            for call in calls:
                for receipt in call.get("tool_results", []):
                    failures = failures + 1 if receipt["status"] == "failed" else 0
                response = _response_from(call.get("response"))
                if response is not None:
                    # Older successful read records predate persisted read receipts.
                    if (
                        not call.get("tool_results")
                        and call.get("status") == "observed"
                        and any(
                            t.name in {_DESCRIBE_DATA_TOOL, _EXPERIMENT_STATUS_TOOL}
                            for t in response.tool_calls
                        )
                    ):
                        failures = 0
                    exchange.append(
                        {
                            "role": "assistant",
                            "text": response.text,
                            "calls": [
                                {"id": t.id, "name": t.name, "arguments": t.arguments}
                                for t in response.tool_calls
                            ],
                        }
                    )
                    exchange.extend(
                        {"role": "tool", "call_id": t["tool_id"], "text": t["text"]}
                        for t in call.get("tool_results", [])
                    )
            job = str(pending.get("job_id") or "")
            return {
                **seeded,
                "phase": "recover_budget",
                "step": len(calls),
                "exchange": exchange,
                "consecutive_failures": failures,
                "proposal_id": str(pending.get("proposal_id") or ""),
                "proposal_call": str((proposal or {}).get("call_id") or ""),
                "proposal_revision": int((proposal or {}).get("base_revision") or 0),
                "launch_intent_id": str(pending.get("launch_intent_id") or ""),
                "preview_identity": str(pending.get("preview_identity") or ""),
                "job_id": job,
                "explain": bool(job),
                "evidence_job_ids": [job] if job else seeded["evidence_job_ids"],
            }
        if proposal is None:
            raise UserStrategyStateError("the pending proposal cannot be read")
        return {
            **seeded,
            "phase": {
                _PHASE_PROPOSAL: "recover_proposal",
                _PHASE_CONFIRMATION: "recover_preview",
                _PHASE_OBSERVATION: "recover_observe",
            }[pending["phase"]],
            "proposal_id": proposal["proposal_id"],
            "proposal_call": proposal["call_id"],
            "proposal_revision": proposal["base_revision"],
            "step": sum(
                1
                for item in strategy.calls
                if item.get("call_id") == turn or (item.get("request") or {}).get("turn_id") == turn
            ),
            "launch_intent_id": str(pending.get("launch_intent_id") or ""),
            "preview_identity": str(pending.get("preview_identity") or ""),
            "job_id": str(pending.get("job_id") or ""),
        }

    def _resume_payload(
        self,
        pending: dict[str, Any],
        *,
        action: object,
        proposal_id: object,
        base_revision: object,
        launch_intent_id: object,
        preview_identity: object,
    ) -> dict[str, Any]:
        """One decision, checked against the record this conversation is waiting on."""
        phase = pending.get("phase")
        chosen = action if isinstance(action, str) else ""
        if phase == _PHASE_PROPOSAL:
            if chosen not in {"applied", "rejected"}:
                raise UserStrategyStateError(
                    "this conversation is waiting for its proposal to be applied or rejected"
                )
            if proposal_id != pending.get("proposal_id"):
                raise UserStrategyStateError(
                    "the decision does not name the proposal this conversation is waiting on"
                )
            if base_revision is not None and base_revision != pending.get("base_revision"):
                raise UserStrategyStateError(
                    "the decision does not name the revision this proposal was made on"
                )
            return {"action": chosen, "proposal_id": str(pending.get("proposal_id"))}
        if phase == _PHASE_CONFIRMATION:
            if chosen not in {"confirm", "declined"}:
                raise UserStrategyStateError(
                    "this conversation is waiting for its preview to be confirmed or declined"
                )
            if chosen == "declined":
                return {"action": "declined"}
            # A confirmation names the exact intent, revision and preview it is for.
            if (
                launch_intent_id != pending.get("launch_intent_id")
                or base_revision != pending.get("base_revision")
                or preview_identity != pending.get("preview_identity")
            ):
                raise UserStrategyStateError(
                    "this confirmation does not name the previewed intent, revision and "
                    "preview this conversation is waiting on"
                )
            return {
                "action": "confirm",
                "launch_intent_id": str(pending.get("launch_intent_id")),
                "base_revision": int(cast(int, pending.get("base_revision"))),
                "preview_identity": str(pending.get("preview_identity")),
            }
        if phase == _PHASE_OBSERVATION:
            if chosen != "observe":
                raise UserStrategyStateError(
                    "this conversation is waiting for its run to be observed"
                )
            return {"action": "observe"}
        raise UserStrategyStateError("this conversation is not waiting for a decision")

    def _conversation_tools(self, paths: tuple[RegisteredPath, ...]) -> Tools:
        """The bounded domain tools and the existing typed proposal.

        Two read the host's own verified facts and change nothing; the third is the
        same typed proposal every other path validates. There is no filesystem, URL,
        code-execution, live, Paper or broker tool, and none can be added by a
        conversation.
        """
        return [
            *self._domain_tools(intent=False),
            ToolDefinition(
                _DESCRIBE_DATA_TOOL,
                "Read this host's own verified description of the data the current draft "
                "would run on. It changes nothing.",
                {"type": "object", "properties": {}},
            ),
            ToolDefinition(
                _EXPERIMENT_STATUS_TOOL,
                "Read the status of this strategy's own experiments, and the host-verified "
                "readings of any run of this strategy that completed. It changes nothing.",
                {"type": "object", "properties": {}},
            ),
            self._propose_tool(paths),
        ]

    @staticmethod
    def _domain_tools(*, intent: bool) -> Tools:
        tools = [
            ToolDefinition(
                _DISCOVER_TOOL,
                "List supported built-in templates and actual catalogued datasets, optionally "
                "filtered by exact symbol. Missing data stays missing. No import or binding.",
                {
                    "type": "object",
                    "properties": {"symbol": {"type": "string", "maxLength": 128}},
                    "additionalProperties": False,
                },
            ),
            ToolDefinition(
                _INSPECT_INPUT_TOOL,
                "Validate the current bound draft, or a supported template with a named "
                "catalogued dataset before user binding. Return actual constraints and input "
                "identity. This neither changes the draft nor authorizes a run.",
                {
                    "type": "object",
                    "properties": {
                        "base_revision": {"type": "integer", "minimum": 1},
                        "scenario_id": {"type": "string", "maxLength": 128},
                        "dataset_id": {"type": "string", "maxLength": 160},
                    },
                    "required": ["base_revision"],
                    "additionalProperties": False,
                },
            ),
        ]
        if not intent:
            tools.append(
                ToolDefinition(
                    _READ_EXPERIMENTS_TOOL,
                    "Read one or two official reports selected for this conversation, in their "
                    "selected order. Verify frozen inputs and report digests. Two reports return "
                    "the existing comparison view; no new statistics are computed by this tool.",
                    {
                        "type": "object",
                        "properties": {
                            "job_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "maxItems": 2,
                                "uniqueItems": True,
                            }
                        },
                        "required": ["job_ids"],
                        "additionalProperties": False,
                    },
                )
            )
        return tools

    def _domain_read(
        self, strategy: UserStrategy, name: str, arguments: Any, state: dict[str, Any]
    ) -> dict[str, Any]:
        allowed = {
            _DISCOVER_TOOL: {"symbol"},
            _INSPECT_INPUT_TOOL: {"base_revision", "scenario_id", "dataset_id"},
            _READ_EXPERIMENTS_TOOL: {"job_ids"},
        }
        if type(arguments) is not dict or set(arguments) - allowed[name]:
            raise UserStrategyStateError("invalid read-tool arguments")
        if name == _DISCOVER_TOOL:
            symbol = arguments.get("symbol")
            if symbol is not None:
                symbol = _bounded(symbol, "symbol", 128)
            templates = [
                {
                    "scenario_id": item["scenario_id"],
                    "name": item["name"],
                    "strategy_parameters": item["strategy_parameters"],
                    "template_assumptions": item["template_assumptions"],
                    "dataset_required": True,
                }
                for item in self._service.registry.templates()
                if symbol is None or cast(dict[str, Any], item["summary"])["symbol"] == symbol
            ]
            # Only catalog metadata is exposed, never paths or raw capture rows.
            fields = {
                "dataset_id",
                "origin",
                "valid",
                "profile",
                "symbol",
                "venue",
                "source_sha256",
                "data_sha256",
                "record_count",
                "bar_count",
                "bar_durations_seconds",
                "bar_start_utc",
                "bar_end_utc",
                "observed_gap_count",
            }
            datasets = [
                {key: value for key, value in item.items() if key in fields}
                for item in self._service.dataset_catalog.list()
                if item.get("valid") is True and (symbol is None or item.get("symbol") == symbol)
            ]
            return {
                "ok": True,
                "scope": "supported built-in templates and catalogued data",
                "templates": templates,
                "datasets": datasets,
                "binding_required": isinstance(strategy.draft, IntentDraft),
            }
        if name == _INSPECT_INPUT_TOOL:
            revision = arguments.get("base_revision")
            if type(revision) is not int or revision != strategy.draft.revision:
                raise UserStrategyStateError(
                    "base_revision does not match the current draft revision"
                )
            scenario_id = arguments.get("scenario_id")
            dataset_id = arguments.get("dataset_id")
            if scenario_id is not None or dataset_id is not None:
                scenario_id = _bounded(scenario_id, "scenario_id", 128)
                dataset_id = _bounded(dataset_id, "dataset_id", 160)
                if scenario_id not in {
                    str(item["scenario_id"]) for item in self._service.registry.templates()
                }:
                    raise UserStrategyStateError(
                        "only a supported built-in template may be inspected before binding"
                    )
                # Resolve a catalog ID before validation; no caller-supplied filesystem path.
                dataset = self._service.dataset_catalog.inspect(dataset_id)
                if dataset.get("valid") is not True:
                    raise UserStrategyStateError("the selected dataset is not valid")
                checked = self._service.validate_scenario(scenario_id, dataset_id=dataset_id)
                return {
                    "ok": True,
                    "base_revision": revision,
                    "dataset_id": dataset_id,
                    "input": checked,
                    "binding_required": True,
                    "run_authorized": False,
                }
            draft = _executable(strategy)
            checked = self._validated(draft, draft.parameters, draft.initial_cash)
            preview = self._preview_of(strategy, draft, self._admitted_request(draft))
            return {
                "ok": True,
                "base_revision": revision,
                "dataset_id": draft.dataset_id,
                "input": checked,
                "constraints": [asdict(p) for p in self._registered_paths(draft)],
                "preview": preview,
                "binding_required": False,
                "run_authorized": False,
            }
        if isinstance(strategy.draft, IntentDraft):
            raise UserStrategyStateError("an unbound intent has no selected experiment reports")
        selected = self._evidence_selection(arguments.get("job_ids"))
        if selected is None or selected != state["evidence_job_ids"]:
            raise UserStrategyStateError(
                "select these exact reports in the workspace before reading them"
            )
        evidence = self._evidence_context(strategy, selected)
        return {
            "ok": True,
            "reports": evidence,
            "comparison_url": (
                f"/backtests/compare/{selected[0]}/{selected[1]}" if len(selected) == 2 else None
            ),
            "comparison_computed": False,
            "limitation": "The existing comparison view computes supported deltas. "
            "This tool supplies verified reports only; no statistical conclusion is computed.",
        }

    def _experiment_facts(self, strategy: UserStrategy) -> Paths:
        """What this strategy's own experiments are, as this host can verify them now."""
        facts: Paths = []
        for record in strategy.experiments[-4:]:
            payload = cast(dict[str, Any], record.get("payload") or {})
            facts.append(
                f"Experiment {record.get('launch_intent_id')} on draft revision "
                f"{payload.get('draft_revision')} is recorded as {record.get('status')}"
            )
            job_id = record.get("job_id")
            if job_id:
                try:
                    facts.append(f"  Its run {job_id} is {self._service.get_job(job_id).status}")
                except Exception:  # noqa: BLE001 — an unreadable run is stated as unreadable
                    facts.append(f"  Its run {job_id} cannot be read in this workspace now")
            if record.get("message"):
                facts.append(f"  This workspace recorded: {record['message']}")
        facts.extend(self._verified_facts(strategy))
        if not facts:
            facts.append("This strategy has no experiment of its own yet.")
        return facts

    def _exchange_messages(self, exchange: Any) -> History:
        """This turn's own tool loop, rebuilt with the identities it really carried."""
        messages: History = []
        for item in exchange if isinstance(exchange, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("role") == "assistant":
                calls = tuple(
                    ToolCall(str(call["id"]), str(call["name"]), dict(call["arguments"]))
                    for call in item.get("calls") or []
                    if isinstance(call, dict)
                )
                messages.append(
                    ModelMessage(
                        role="assistant", text=str(item.get("text") or ""), tool_calls=calls
                    )
                )
            else:
                messages.append(
                    ModelMessage(
                        role="tool",
                        text=str(item.get("text") or ""),
                        call_id=str(item.get("call_id") or "") or None,
                    )
                )
        return messages

    def _dispatch_call(
        self,
        strategy: UserStrategy,
        call: str,
        state: dict[str, Any],
        *,
        system: str,
        messages: History,
        tools: Tools,
        required: tuple[str, ...],
    ) -> tuple[ModelResponse | None, str, str | None]:
        """One provider call, dispatched at the exact moment the record allows it.

        A streaming port is read as it really streams — every delta it sends is
        emitted as it arrives — and a port that cannot stream answers whole, which
        yields no delta at all rather than a reply cut into pieces afterwards. The
        answer, its tool identities and their arguments are durable before the graph
        advances, so a lost checkpoint resumes from this record.
        """
        try:
            port = self._port_for_call(strategy, call)
        except ModelNotConfigured as error:
            return None, "unconfigured", str(error)
        if state.get("task_budget"):
            try:
                strategy = self._store.dispatch_task_call(strategy.user_strategy_id, call)
            except UserStrategyBudgetError as error:
                return None, "interrupted_before_dispatch", str(error)
        else:
            strategy = self._mark_dispatched(strategy, call)
        key = (strategy.user_strategy_id, call)
        self._active_calls.add(key)
        response: ModelResponse | None = None
        try:
            try:
                if isinstance(port, StreamingModelPort):
                    response = self._streamed(
                        port,
                        call,
                        state=state,
                        system=system,
                        messages=messages,
                        tools=tools,
                        required=required,
                    )
                else:
                    response = port.complete(
                        system=system,
                        messages=messages,
                        tools=tools,
                        max_tokens=_MAX_TOKENS,
                        required_capabilities=required,
                    )
            except ModelNotConfigured as error:
                return None, "unconfigured", str(error)
            except ModelProviderError as error:
                return None, "unknown", str(error)
            except Exception as error:  # noqa: BLE001 — an unobserved outcome is unknown
                return None, "unknown", f"the provider outcome was not observed: {error}"
        finally:
            self._active_calls.discard(key)
        self._settle(
            self._store.get(strategy.user_strategy_id),
            call,
            "observed",
            None,
            response,
            record={
                "turn_id": state["turn_id"],
                "step": int(state["step"]),
                "response": _response_document(response),
            },
        )
        return response, "observed", None

    def _streamed(
        self,
        port: StreamingModelPort,
        call: str,
        *,
        state: dict[str, Any],
        system: str,
        messages: History,
        tools: Tools,
        required: tuple[str, ...],
    ) -> ModelResponse:
        final: ModelResponse | None = None
        for item in port.stream(
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=_MAX_TOKENS,
            required_capabilities=required,
        ):
            if item.kind == TEXT_DELTA and item.text:
                if state.get("task_budget") and self._task_cancelled(state):
                    # Closing a partially read stream cannot prove provider billing or
                    # completion. Keep an unknown receipt, and do not dispatch again.
                    raise ModelProviderError(
                        "generation stopped before its final response was observed"
                    )
                self._emit(event(TEXT_DELTA, call_id=call, text=item.text))
            elif item.response is not None:
                final = item.response
        if final is None:
            raise ModelProviderError("model provider returned no final response")
        return final

    @staticmethod
    def _turn_remaining(strategy: UserStrategy, turn: str) -> int:
        root = _entry(strategy.calls, "call_id", turn)
        if root is None:
            return 0
        if root.get("task"):
            return max(int(root["task"]["limit"]) - _task_usage(strategy, turn), 0)
        approved = root.get("approved_budget") or {}
        used = sum(
            1
            for item in strategy.calls
            if item.get("call_id") == turn or (item.get("request") or {}).get("turn_id") == turn
        )
        return max(int(approved.get("max_model_calls", 0)) - used, 0)

    def _reserve_child(
        self, strategy: UserStrategy, call: str, state: dict[str, Any]
    ) -> UserStrategy:
        """Reserve one further model call of this turn, before the provider is reached."""
        root = _entry(strategy.calls, "call_id", state["turn_id"])
        if root is None or self._turn_remaining(strategy, state["turn_id"]) <= 0:
            raise UserStrategyBudgetError("this turn has no approved model calls left")
        approved = dict(root["approved_budget"])
        now = _now()
        request = {
            "action": "continue",
            "turn_id": state["turn_id"],
            "step": int(state["step"]),
            "text": state["message"],
            "base_revision": int(state["base_revision"]),
            "approved_budget": approved,
        }
        record = {
            "call_id": call,
            "status": "pending",
            "route": {"provider": None, "model": None, "endpoint": None},
            "base_revision": int(state["base_revision"]),
            "approved_budget": approved,
            "request": request,
            "payload_sha256": _digest(request),
            "reserved_at": now,
            "message": None,
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
        selection = self._evidence_selection(state["evidence_job_ids"] or None)
        if selection is not None:
            request["evidence_job_ids"] = selection
            record["payload_sha256"] = _digest(request)
            record["evidence"] = self._evidence_context(strategy, selection)
        route = root.get("route")
        if route is not None:
            record["route"] = route
        if root.get("model_profile") is not None:
            record["model_profile"] = dict(root["model_profile"])
        pending = replace(strategy, updated_at=now, calls=(*strategy.calls, record))
        self._publish_call(pending, call, dispatch=route is not None)
        return pending

    def _open_proposal(
        self, strategy: UserStrategy, proposal_id: str, call: str, items: Documents
    ) -> UserStrategy:
        """Record one new proposal, superseding the one open suggestion it replaces."""
        retained = strategy.proposals[-_MAX_PROPOSALS:]
        proposals = (
            *retained,
            {
                "proposal_id": proposal_id,
                "call_id": call,
                "base_revision": strategy.draft.revision,
                "status": "open",
                "created_at": _now(),
                "items": items,
                "applied_paths": [],
                "stale_paths": [],
            },
        )
        next_strategy = replace(strategy, updated_at=_now(), proposals=proposals)
        self._store.save(next_strategy)
        return next_strategy

    @staticmethod
    def _applied_at(strategy: UserStrategy, proposal_id: str, revision: int) -> bool:
        """Whether this proposal is applied to exactly the revision it was made on."""
        proposal = _entry(strategy.proposals, "proposal_id", proposal_id)
        if proposal is None or proposal.get("status") != "applied":
            return False
        change = next(
            (
                item
                for item in strategy.changes
                if item.get("kind") == "apply" and item.get("proposal_id") == proposal_id
            ),
            None,
        )
        return (
            change is not None
            and change.get("from_revision") == revision
            and change.get("to_revision") == revision + 1
            and strategy.draft.revision == revision + 1
        )

    @staticmethod
    def _applied_revision(strategy: UserStrategy, proposal_id: str) -> int | None:
        """The revision one proposal is recorded as applied to, or ``None``.

        The persisted proposal and its own apply change are the authority: exactly
        one proposal of this identity is applied, exactly one apply change names it,
        and that change moved the recorded base revision to the revision after it.
        Today's draft is deliberately not consulted — unlike :meth:`_applied_at` — so
        a change the user applied and confirmed stays explainable after the draft
        moved on, while a proposal that was never applied never is.
        """
        proposals = [item for item in strategy.proposals if item.get("proposal_id") == proposal_id]
        if len(proposals) != 1 or proposals[0].get("status") != "applied":
            return None
        base = proposals[0].get("base_revision")
        changes = [
            item
            for item in strategy.changes
            if item.get("kind") == "apply" and item.get("proposal_id") == proposal_id
        ]
        if type(base) is not int or len(changes) != 1:
            return None
        if changes[0].get("from_revision") != base or changes[0].get("to_revision") != base + 1:
            return None
        return base + 1

    def _remember(self, identity: str, record: dict[str, Any]) -> None:
        """Publish the one decision this conversation is waiting for, through the store."""
        strategy = self._store.get(identity)
        self._store.save(replace(strategy, updated_at=_now(), conversation_confirmation=record))

    def _forget(self, identity: str) -> None:
        """Nothing is left waiting: the decision was made, or it can no longer be made."""
        strategy = self._store.get(identity)
        self._store.save(replace(strategy, updated_at=_now(), conversation_confirmation=None))

    def _refused_decision(self, state: dict[str, Any], reason: str) -> dict[str, Any]:
        """A decision that cannot be carried out ends the turn, with its reason."""
        self._emit(event(ERROR, kind_reason="decision", error=reason))
        return {**state, "phase": "refused", "refusal": reason}

    def _waiting_decision(self, state: dict[str, Any], reason: str) -> dict[str, Any]:
        """A decision this workspace cannot accept is refused and waited on again.

        The turn is left exactly where it was — the same pending record, the same
        interrupt — so a decision that named the wrong thing, or one that arrived
        before its own effect was durable, is answered honestly and the user can make
        it again. Nothing is carried out from a decision that did not check out.
        """
        self._emit(event(ERROR, kind_reason="decision", error=reason))
        return {**state, "phase": "waiting"}

    @staticmethod
    def _emit(payload: dict[str, Any]) -> None:
        """Tell the streaming caller what just happened, as it happens."""
        get_stream_writer()(payload)
