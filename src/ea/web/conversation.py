"""One bounded conversation graph: the checkpointed execution log of one strategy turn.

The graph owns the *shape* of a turn — one model call at a time, the fixed tools it
may ask for, the human decisions it must wait for, the experiment it may observe and
the verified report it may then explain. It owns no facts. Every canonical message,
call and proposal lives in :class:`ea.web.user_strategies.UserStrategyStore`, and a
checkpoint holds only this turn's own order and its pending business identities. A
checkpoint is therefore an execution log, never a second record of the conversation:
losing one loses no durable fact, and rebuilding it never re-sends a model call.

The checkpoint database is the workspace's one conversation database, opened with a
strict JSON-only serializer, so no graph state can carry a Python object the store
could not read back itself. Every invocation runs inside an explicitly disabled
tracing context: this workspace never initializes a tracing client and never sends a
conversation anywhere but the configured model endpoint.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

#: The events one conversation turn can stream. ``text_delta`` is emitted only for a
#: delta a provider really sent; nothing is sliced out of a finished answer.
TEXT_DELTA = "text_delta"
TOOL_START = "tool_start"
TOOL_END = "tool_end"
TOOL_ERROR = "tool_error"
STATE = "state"
INTERRUPT = "interrupt"
ERROR = "error"
DONE = "done"


def event(kind: str, **fields: Any) -> dict[str, Any]:
    """One structured event, exactly as the JSON endpoint and the SSE stream see it."""
    return {"kind": kind, **fields}


def frame(payload: dict[str, Any]) -> str:
    """One server-sent event, with the JSON text as the only data line."""
    return f"data: {json.dumps(payload, sort_keys=True)}\n\n"


def thread_id(user_strategy_id: str, turn_id: str) -> str:
    """One turn of one conversation is one thread, so turns never resume each other."""
    return f"{user_strategy_id}.{turn_id}"


def thread_config(user_strategy_id: str, turn_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id(user_strategy_id, turn_id)}}


def strict_serde() -> JsonPlusSerializer:
    """The one serializer every checkpoint of a conversation is read and written with.

    Pickle is refused and no JSON module is pre-authorized, so a checkpoint can hold
    only the primitives the store itself could hold: strings, numbers, booleans,
    ``None``, lists and objects with string keys.
    """
    return JsonPlusSerializer(
        pickle_fallback=False,
        allowed_json_modules=[],
        allowed_msgpack_modules=[],
    )


@contextmanager
def checkpoints(path: Path, schema: str) -> Iterator[SqliteSaver]:
    """A short-lived saver over one connection to the workspace conversation database.

    Both this saver and the strategy store open the same file with their own
    connection, each in its own transaction, so neither is ever holding the other's
    lock while a turn is being decided. The store's own tables are ensured here too,
    because either owner may be the first to create the file, and a conversation
    database that exists is always one both of them can read.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False)
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.executescript(schema)
        saver = SqliteSaver(connection, serde=strict_serde())
        saver.setup()
        yield saver
    finally:
        connection.close()


class ReservationConflict(ValueError):
    """A call identity already names a different approved request."""


class ReservationLimit(ValueError):
    """A legacy caller's explicit workspace ceiling was reached."""


def reserve_call(
    connection: sqlite3.Connection,
    identity: str,
    call_id: str,
    canonical: str,
    digest: str,
    status: str,
    reserved_at: str,
    *,
    ceiling: int | None = None,
) -> dict[str, Any]:
    """Reserve once inside the caller's existing document transaction.

    Research and Paper explanations use the same ledger, comparison and optional
    legacy ceiling. The caller commits its own conversation in this transaction.
    """
    stored = connection.execute(
        "SELECT request, payload_sha256, status, reserved_at FROM model_call_reservations "
        "WHERE strategy_id = ? AND call_id = ?",
        (identity, call_id),
    ).fetchone()
    if stored is not None:
        if str(stored[0]) != canonical:
            raise ReservationConflict("call_id is already bound to a different request")
        digest, status, reserved_at = map(str, stored[1:])
    else:
        if ceiling is not None:
            count = connection.execute("SELECT COUNT(*) FROM model_call_reservations").fetchone()
            if count is None or int(count[0]) >= ceiling:
                raise ReservationLimit(
                    f"this workspace has reached its granted model call budget ({ceiling})"
                )
        connection.execute(
            "INSERT INTO model_call_reservations "
            "(strategy_id, call_id, request, payload_sha256, status, reserved_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (identity, call_id, canonical, digest, status, reserved_at),
        )
    return {
        "strategy_id": identity,
        "call_id": call_id,
        "request": json.loads(canonical),
        "payload_sha256": digest,
        "status": status,
        "reserved_at": reserved_at,
        "replayed": stored is not None,
    }
