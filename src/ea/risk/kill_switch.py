"""Durable scoped operator kill switch for the local Paper outbound boundary.

The kill switch is operator control, not economic authority. It never liquidates,
cancels or mutates Orders, Fills, the ledger or portfolio. It only denies the
*next* outbound broker effect while a scope is HALTED. Halt is monotone per scope:
the first halt reason is retained and only an explicit operator ``resume`` returns
the scope to ACTIVE. Restart restores the persisted state and never auto-resumes.

Scopes nest for the single-strategy/single-account/single-broker mission: a
``GLOBAL`` halt blocks every outbound effect, an ``ACCOUNT`` halt blocks the
account (and therefore its strategies), and a ``STRATEGY`` halt blocks that
strategy. An outbound effect is permitted only when every applicable scope is
ACTIVE.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import final

from ea.core.outcomes import OutcomeCode
from ea.core.run import RunId, Sha256Digest
from ea.core.time import TimeValidationError, require_utc

_SCHEMA = "ea.paper-kill-switch.v1"
_DIGEST_DOMAIN = b"ea.paper-kill-switch.v1\0"
_MAX_REASON_BYTES = 1024

_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.CONFLICTING_ID,
    }
)


class OperatorKillSwitchError(ValueError):
    """Closed failure at the durable operator kill-switch boundary."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _ERROR_CODES:
            raise TypeError("kill-switch errors require an exact permitted OutcomeCode")
        self.code = code
        super().__init__(message)


class KillSwitchScope(StrEnum):
    STRATEGY = "strategy"
    ACCOUNT = "account"
    GLOBAL = "global"


class KillSwitchState(StrEnum):
    ACTIVE = "active"
    HALTED = "halted"


_SCOPES: tuple[KillSwitchScope, ...] = (
    KillSwitchScope.STRATEGY,
    KillSwitchScope.ACCOUNT,
    KillSwitchScope.GLOBAL,
)


@final
@dataclass(frozen=True, slots=True)
class ScopeControl:
    """The recorded operator decision for one kill-switch scope."""

    scope: KillSwitchScope
    state: KillSwitchState
    reason: str
    changed_at: datetime | None


@final
@dataclass(frozen=True, slots=True)
class _AuthorityState:
    run_id: RunId
    revision: int
    scopes: MappingProxyType[KillSwitchScope, ScopeControl]


def _fail(code: OutcomeCode, message: str) -> OperatorKillSwitchError:
    return OperatorKillSwitchError(code, message)


def _require_reason(reason: object) -> str:
    if type(reason) is not str:
        raise _fail(OutcomeCode.INVALID_TYPE, "kill-switch reason must be an exact str")
    if not reason.strip():
        raise _fail(OutcomeCode.OUT_OF_RANGE, "kill-switch reason must be non-empty")
    if len(reason.encode("utf-8")) > _MAX_REASON_BYTES:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "kill-switch reason exceeds the byte bound")
    return reason


def _require_changed_at(value: object) -> datetime:
    if type(value) is not datetime:
        raise _fail(OutcomeCode.INVALID_TYPE, "kill-switch change time must be an exact datetime")
    try:
        return require_utc(value, field="changed_at")
    except TimeValidationError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error


def _utc_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@final
class OperatorKillSwitchAuthority:
    """One run-bound, monotone, scoped operator control authority.

    ``halt`` is durable and idempotent per scope (the first reason wins);
    ``resume`` is the only transition back to ACTIVE and requires the scope to
    be HALTED. The authority grants no permission and performs no liquidation.
    """

    _state: _AuthorityState

    __slots__ = ("_state",)

    def __init__(self) -> None:
        raise TypeError("kill-switch authorities are created only by their factory")

    @property
    def run_id(self) -> RunId:
        return self._state.run_id

    @property
    def revision(self) -> int:
        return self._state.revision

    @property
    def scopes(self) -> MappingProxyType[KillSwitchScope, ScopeControl]:
        return self._state.scopes

    def is_halted(self, scope: KillSwitchScope) -> bool:
        if type(scope) is not KillSwitchScope:
            raise _fail(OutcomeCode.INVALID_TYPE, "scope must be an exact KillSwitchScope")
        return self._state.scopes[scope].state is KillSwitchState.HALTED

    def effective_halted(self) -> bool:
        """True when any scope blocks the single-strategy/account outbound path."""
        return any(self.is_halted(scope) for scope in _SCOPES)

    def halted_scopes(self) -> tuple[KillSwitchScope, ...]:
        return tuple(scope for scope in _SCOPES if self.is_halted(scope))

    def halt(self, scope: KillSwitchScope, *, reason: str, changed_at: datetime) -> ScopeControl:
        """Engage one scope; the first halt reason is never rewritten."""
        if type(scope) is not KillSwitchScope:
            raise _fail(OutcomeCode.INVALID_TYPE, "scope must be an exact KillSwitchScope")
        reason = _require_reason(reason)
        changed_at = _require_changed_at(changed_at)
        state = self._state
        control = state.scopes[scope]
        if control.state is KillSwitchState.HALTED:
            return control
        next_scopes = dict(state.scopes)
        next_scopes[scope] = ScopeControl(scope, KillSwitchState.HALTED, reason, changed_at)
        self._state = _AuthorityState(
            state.run_id, state.revision + 1, MappingProxyType(next_scopes)
        )
        return next_scopes[scope]

    def resume(self, scope: KillSwitchScope, *, reason: str, changed_at: datetime) -> ScopeControl:
        """Explicitly return one scope to ACTIVE; never automatic on restart."""
        if type(scope) is not KillSwitchScope:
            raise _fail(OutcomeCode.INVALID_TYPE, "scope must be an exact KillSwitchScope")
        reason = _require_reason(reason)
        changed_at = _require_changed_at(changed_at)
        state = self._state
        control = state.scopes[scope]
        if control.state is not KillSwitchState.HALTED:
            raise _fail(OutcomeCode.CONFLICTING_ID, "resume requires a halted scope")
        next_scopes = dict(state.scopes)
        next_scopes[scope] = ScopeControl(scope, KillSwitchState.ACTIVE, reason, changed_at)
        self._state = _AuthorityState(
            state.run_id, state.revision + 1, MappingProxyType(next_scopes)
        )
        return next_scopes[scope]


def create_operator_kill_switch_authority(run_id: RunId) -> OperatorKillSwitchAuthority:
    """Create one fresh ACTIVE kill switch over all scopes."""
    if type(run_id) is not RunId:
        raise _fail(OutcomeCode.INVALID_TYPE, "run_id must be an exact RunId")
    scopes: dict[KillSwitchScope, ScopeControl] = {}
    for scope in _SCOPES:
        scopes[scope] = ScopeControl(scope, KillSwitchState.ACTIVE, "", None)
    value = object.__new__(OperatorKillSwitchAuthority)
    object.__setattr__(
        value,
        "_state",
        _AuthorityState(run_id, 0, MappingProxyType(scopes)),
    )
    return value


def _scope_document(control: ScopeControl) -> dict[str, object]:
    changed_at = control.changed_at
    return {
        "state": control.state.value,
        "reason": control.reason,
        "changed_at": None if changed_at is None else _utc_text(changed_at),
    }


def canonical_kill_switch_bytes(authority: OperatorKillSwitchAuthority) -> bytes:
    """Return the canonical durable document for the current state."""
    if type(authority) is not OperatorKillSwitchAuthority:
        raise _fail(OutcomeCode.INVALID_TYPE, "authority must be an exact kill-switch authority")
    state = authority._state
    document = {
        "schema": _SCHEMA,
        "run_id": state.run_id.value,
        "revision": state.revision,
        "scopes": {scope.value: _scope_document(state.scopes[scope]) for scope in _SCOPES},
    }
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def kill_switch_digest(authority: OperatorKillSwitchAuthority) -> Sha256Digest:
    return Sha256Digest(sha256(_DIGEST_DOMAIN + canonical_kill_switch_bytes(authority)).hexdigest())


def restore_operator_kill_switch_authority(
    run_id: RunId,
    payload: bytes,
) -> OperatorKillSwitchAuthority:
    """Rebuild a persisted authority; a HALTED scope stays HALTED, never auto-resumed."""
    if type(run_id) is not RunId:
        raise _fail(OutcomeCode.INVALID_TYPE, "run_id must be an exact RunId")
    if type(payload) is not bytes:
        raise _fail(OutcomeCode.INVALID_TYPE, "kill-switch payload must be exact bytes")
    try:
        document = json.loads(payload)
    except (RecursionError, UnicodeError, ValueError) as error:
        raise _fail(OutcomeCode.CONFLICTING_ID, "kill-switch payload is malformed JSON") from error
    if type(document) is not dict or document.get("schema") != _SCHEMA:
        raise _fail(OutcomeCode.CONFLICTING_ID, "kill-switch schema conflicts")
    if document.get("run_id") != run_id.value or type(document.get("revision")) is not int:
        raise _fail(OutcomeCode.CONFLICTING_ID, "kill-switch identity conflicts")
    revision = document["revision"]
    if revision < 0:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "kill-switch revision must be non-negative")
    raw_scopes = document.get("scopes")
    if type(raw_scopes) is not dict or set(raw_scopes) != {scope.value for scope in _SCOPES}:
        raise _fail(OutcomeCode.CONFLICTING_ID, "kill-switch scope set conflicts")
    scopes: dict[KillSwitchScope, ScopeControl] = {}
    for scope in _SCOPES:
        raw = raw_scopes[scope.value]
        if type(raw) is not dict or set(raw) != {"state", "reason", "changed_at"}:
            raise _fail(OutcomeCode.CONFLICTING_ID, "kill-switch scope record conflicts")
        try:
            parsed_state = KillSwitchState(raw["state"])
        except ValueError as error:
            raise _fail(OutcomeCode.CONFLICTING_ID, "kill-switch scope state conflicts") from error
        reason = raw.get("reason")
        if type(reason) is not str:
            raise _fail(OutcomeCode.CONFLICTING_ID, "kill-switch reason conflicts")
        changed_at = raw.get("changed_at")
        parsed_time: datetime | None = None
        if changed_at is not None:
            if type(changed_at) is not str:
                raise _fail(OutcomeCode.CONFLICTING_ID, "kill-switch change time conflicts")
            try:
                parsed_time = datetime.strptime(changed_at, "%Y-%m-%dT%H:%M:%S.%fZ")
            except ValueError as error:
                raise _fail(
                    OutcomeCode.CONFLICTING_ID, "kill-switch change time conflicts"
                ) from error
        else:
            if parsed_state is not KillSwitchState.ACTIVE:
                raise _fail(OutcomeCode.CONFLICTING_ID, "halted scope requires a change time")
        scopes[scope] = ScopeControl(scope, parsed_state, reason, parsed_time)
    value = object.__new__(OperatorKillSwitchAuthority)
    object.__setattr__(
        value,
        "_state",
        _AuthorityState(run_id, revision, MappingProxyType(scopes)),
    )
    return value
