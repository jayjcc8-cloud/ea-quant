"""The Python side of the quine-core confirmed-experiment contract (isolated mode).

The Rust core owns the experiment: identity, the single active slot, the frozen
input, dispatch, cancellation, worker supervision and the official result. This
module is a client of that owner and nothing else — it opens one private Unix
socket, sends one versioned JSON request per call, and maps the core's own record
into the read projection the Web API serves. It holds no job store, no result
store, no cancellation state and no fallback: when the core cannot be reached the
call fails closed rather than being answered from a cached local claim.

Two deliberate behaviours live here rather than in the caller:

* a confirmation whose receipt was never observed is *never* re-sent. The same
  launch intent is queried instead, so a lost reply can never become a second
  backtest charge.
* the frozen input Python confirms with is the domain adapter's own rendering of
  the admitted snapshot, checked against the digest the Web service already
  published for that input, so what the core freezes is what the preview showed.

The official artifacts are read from the core's own workspace — the directory the
private socket lives in — and each read is held to the digest the core registered
for it.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from ea.core.encoding import canonical_json_bytes

PROTOCOL = "quine.v1"

#: The one document Python freezes inside the confirmed canonical input so the
#: owner retains, verbatim, what the confirmation was made of. The core stores
#: these bytes opaquely; they are the confirmation's own metadata, not a second
#: authority and not something Rust parses.
CONFIRMATION_SCHEMA = "ea.quine-confirmation.v1"

#: The three verified artifact names the core publishes for one job.
ARTIFACTS = ("report.json", "summary.txt", "equity-path.json")
#: The registered digest of each published artifact, in the owner's result.
ARTIFACT_RESULT_KEYS = {
    "report.json": "report_sha256",
    "summary.txt": "summary_sha256",
    "equity-path.json": "equity_path_sha256",
}

#: The longest single reply line this client will read. The core bounds its own
#: requests; this is the same bound applied to what comes back.
MAX_LINE = 1 << 20

#: How one core status becomes the status this API has always served. The
#: ``succeeded`` case keeps the existing ``created`` word: it means the run
#: exists, and the report is a separate fact.
STATUS_PROJECTION = {
    "eligible": "confirmed",
    "dispatched": "running",
    "running": "running",
    "stopping": "stopping",
    "unknown": "unknown",
    "succeeded": "created",
    "failed": "failed",
    "cancelled": "cancelled",
}

#: The same states as the workspace's own job vocabulary, so the existing run,
#: report and comparison views read a core-owned run without a second meaning.
JOB_STATUS_PROJECTION = {
    "eligible": "accepted",
    "dispatched": "running",
    "running": "running",
    "stopping": "stopping",
    "unknown": "interrupted",
    "succeeded": "succeeded",
    "failed": "failed",
    "cancelled": "stopped",
}

STATUS_NOTES = {
    "confirmed": "the core confirmed this input and has not started it yet",
    "running": "the core is running this input now",
    "stopping": "a stop was accepted and the core is bringing the run's process group down",
    "unknown": (
        "the core could not prove how this run ended; it is not re-sent, not refunded "
        "and does not count as a result"
    ),
    "failed": "the core refused or failed this run before a result existed",
    "cancelled": "this run was cancelled; no result was registered for it",
    "created": "the core registered an official result for this run",
}


class QuineError(ValueError):
    """The core, or this client, could not complete the call."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class QuineUnavailable(QuineError):
    """The core could not be reached: nothing here may stand in for its answer."""

    def __init__(self, message: str):
        super().__init__("quine_unavailable", message)


class QuineRefused(QuineError):
    """The core answered and refused the request, with its own code."""


@dataclass(frozen=True, slots=True)
class QuineFrozen:
    """One frozen input, exactly as the core will retain and re-verify it."""

    envelope: dict[str, Any]
    canonical: bytes
    sha256: str
    snapshot_sha256: str


@dataclass(frozen=True, slots=True)
class QuineReceipt:
    """The outcome of one confirmation, told apart from an unobserved one.

    ``observed`` is false only when the reply never arrived. In that case
    ``experiment`` may still be the record a query found, and ``None`` means the
    core holds nothing for this intent — which is not a local failure claim.
    """

    experiment: dict[str, Any] | None
    replayed: bool
    observed: bool
    barrier: dict[str, Any] | None = None


def _digest(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def payload_digest(payload: object) -> str:
    """The canonical digest this workspace gives one confirmation payload.

    It is one rule for both the record this service writes and the payload
    recovered from the owner, so a rebuilt projection can be checked against the
    confirmation it claims to be.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(canonical.encode()).hexdigest()


_payload_digest = payload_digest


class QuineClient:
    """One JSON line over the core's private socket, with a bounded wait."""

    def __init__(self, socket_path: Path, *, timeout: float = 10.0):
        self.socket_path = Path(socket_path)
        self.timeout = timeout

    def call(
        self, method: str, params: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        envelope = {"protocol": PROTOCOL, "request": {"method": method, "params": params}}
        line = canonical_json_bytes(envelope) + b"\n"
        wait = self.timeout if timeout is None else timeout
        try:
            reply = self._exchange(line, wait)
        except QuineUnavailable:
            raise
        except OSError as error:
            raise QuineUnavailable(f"the core is not reachable: {error}") from None
        try:
            document = json.loads(reply)
        except ValueError:
            raise QuineUnavailable(
                "the core answered something that is not one JSON reply"
            ) from None
        if type(document) is not dict or document.get("protocol") != PROTOCOL:
            raise QuineUnavailable("the core reply does not speak this protocol version")
        if document.get("ok") is not True:
            detail = document.get("error")
            code = "quine_refused"
            message = "the core refused this request"
            if type(detail) is dict:
                raw_code = detail.get("code")
                raw_message = detail.get("message")
                if type(raw_code) is str and raw_code:
                    code = raw_code
                if type(raw_message) is str and raw_message:
                    message = raw_message
            raise QuineRefused(code, message)
        result = document.get("result")
        if type(result) is not dict:
            raise QuineUnavailable("the core reply carries no result document")
        return cast(dict[str, Any], result)

    def _exchange(self, line: bytes, wait: float) -> bytes:
        if len(line) > MAX_LINE:
            raise QuineError("quine_refused", "the request is longer than the core's bound")
        stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            stream.settimeout(wait)
            try:
                stream.connect(str(self.socket_path))
            except OSError as error:
                raise QuineUnavailable(f"the core is not reachable: {error}") from None
            try:
                stream.sendall(line)
            except OSError as error:
                raise QuineUnavailable(f"the request did not reach the core: {error}") from None
            chunks: list[bytes] = []
            received = 0
            while True:
                try:
                    chunk = stream.recv(65536)
                except TimeoutError:
                    raise QuineUnavailable(
                        "the core did not answer within the bound for this request"
                    ) from None
                except OSError as error:
                    raise QuineUnavailable(f"the core reply was lost: {error}") from None
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_LINE:
                    raise QuineUnavailable("the core reply is longer than this client reads")
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
        finally:
            stream.close()
        payload = b"".join(chunks)
        line_end = payload.find(b"\n")
        if line_end < 0:
            raise QuineUnavailable("the core closed the connection without a reply")
        return payload[:line_end]


class QuineBridge:
    """The core as this workspace's experiment owner, and its read projections."""

    def __init__(
        self,
        socket_path: Path,
        *,
        confirm_timeout: float = 10.0,
        query_timeout: float = 5.0,
        client: QuineClient | None = None,
    ):
        self.socket_path = Path(socket_path)
        #: The core's own workspace: the socket lives in it, and the official
        #: artifacts it publishes are read from it. Nothing else is ever read here.
        self.workspace = self.socket_path.parent
        self._client = (
            client if client is not None else QuineClient(self.socket_path, timeout=confirm_timeout)
        )
        self._confirm_timeout = confirm_timeout
        self._query_timeout = query_timeout

    # -- frozen input --------------------------------------------------------

    def freeze(
        self,
        request: dict[str, Any],
        *,
        input_sha256: str,
        input_identity: object,
        extras: dict[str, Any] | None = None,
    ) -> QuineFrozen:
        """One frozen input envelope, held to the admitted input it claims to be.

        The domain adapter renders the envelope from the same registered scenario
        and the same parameters the preview admitted; the digest it reports must
        be exactly the digest the Web service published for that admitted input.
        A disagreement means the surface the preview showed is not the surface
        this confirmation would freeze, so nothing is sent.

        ``extras`` is merged into the envelope before it is frozen: this is how the
        confirmation's own metadata — the retained payload a projection can be
        rebuilt from — travels with the bytes the owner keeps.
        """
        from ea.web import rust_domain

        reply = rust_domain.run("snapshot", request)
        if reply.get("status") != "ok":
            code = reply.get("code") or "quine_refused"
            message = reply.get("message") or "the frozen input could not be rendered"
            raise QuineRefused(str(code), str(message))
        result = reply.get("result")
        if type(result) is not dict:
            raise QuineUnavailable("the domain adapter produced no frozen input")
        envelope = result.get("frozen")
        if type(envelope) is not dict:
            raise QuineUnavailable("the domain adapter produced no frozen envelope")
        document = {**envelope, **(extras or {})}
        canonical = canonical_json_bytes(document)
        snapshot_sha256 = envelope.get("snapshot_sha256")
        if snapshot_sha256 != input_sha256:
            raise QuineRefused(
                "input_changed",
                "the frozen input this confirmation would carry is not the input that "
                "was previewed; preview the experiment again",
            )
        snapshot = envelope.get("snapshot")
        if type(snapshot) is not dict or snapshot.get("identity") != input_identity:
            raise QuineRefused(
                "input_changed",
                "the frozen input this confirmation would carry is not the input that "
                "was previewed; preview the experiment again",
            )
        return QuineFrozen(
            envelope=document,
            canonical=canonical,
            sha256=_digest(canonical),
            snapshot_sha256=cast(str, snapshot_sha256),
        )

    # -- confirmed experiment methods ---------------------------------------

    def confirm(
        self,
        *,
        strategy_id: str,
        intent_id: str,
        draft_revision: int,
        preview_identity: str,
        max_model_calls: int,
        frozen: QuineFrozen,
    ) -> QuineReceipt:
        """One confirmation, or the query of that same intent — never a resend."""
        params = {
            "strategy_id": strategy_id,
            "intent_id": intent_id,
            "draft_revision": draft_revision,
            "preview_identity": preview_identity,
            "budget": {"max_model_calls": max_model_calls, "max_backtests": 1},
            "frozen_input": {
                "canonical_bytes": frozen.canonical.hex(),
                "sha256": frozen.sha256,
            },
        }
        try:
            result = self._client.call("confirm", params, timeout=self._confirm_timeout)
        except QuineUnavailable:
            # The reply was lost, so whether this intent was registered is unknown.
            # Only the same intent may be queried; it is never confirmed again.
            experiment, barrier = self._query_quiet(strategy_id, intent_id)
            return QuineReceipt(
                experiment=experiment, replayed=False, observed=False, barrier=barrier
            )
        experiment = result.get("experiment")
        return QuineReceipt(
            experiment=experiment if type(experiment) is dict else None,
            replayed=bool(result.get("replayed")),
            observed=True,
        )

    def query(
        self, strategy_id: str, intent_id: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """The core's own record for one intent, or its absence, and any barrier."""
        result = self._client.call(
            "query",
            {"strategy_id": strategy_id, "intent_id": intent_id},
            timeout=self._query_timeout,
        )
        experiment = result.get("experiment")
        barrier = result.get("barrier")
        return (
            None if type(experiment) is not dict else cast(dict[str, Any], experiment),
            None if type(barrier) is not dict else cast(dict[str, Any], barrier),
        )

    def _query_quiet(
        self, strategy_id: str, intent_id: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        try:
            return self.query(strategy_id, intent_id)
        except QuineError:
            return None, None

    def cancel(self, strategy_id: str, intent_id: str) -> dict[str, Any]:
        """One accepted stop request. It is not proof that the run has stopped."""
        return self._client.call(
            "cancel",
            {"strategy_id": strategy_id, "intent_id": intent_id},
            timeout=self._query_timeout,
        )

    def experiments(self) -> list[dict[str, Any]]:
        """Every experiment the core owns, in the core's own order.

        This is how a projection is rebuilt rather than remembered: the owner's
        list is the authority for which confirmations exist at all, so a workspace
        that lost its own copy discovers them again instead of inventing one.
        """
        result = self._client.call("list", {}, timeout=self._query_timeout)
        experiments = result.get("experiments")
        if type(experiments) is not list:
            raise QuineUnavailable("the core returned no experiment list")
        return [item for item in experiments if type(item) is dict]

    def job_experiment(self, job_id: str) -> dict[str, Any] | None:
        """The core's record for one job identity, or ``None`` when it owns none."""
        if not job_id:
            return None
        for experiment in self.experiments():
            if experiment.get("job_id") == job_id:
                return experiment
        return None

    # -- reads ---------------------------------------------------------------

    def frozen_document(self, experiment: dict[str, Any]) -> dict[str, Any] | None:
        """The retained frozen input of one record, held to its own digest."""
        encoded = experiment.get("frozen_canonical")
        claimed = experiment.get("frozen_sha256")
        if type(encoded) is not str or type(claimed) is not str:
            return None
        try:
            frozen = bytes.fromhex(encoded)
        except ValueError:
            return None
        if _digest(frozen) != claimed:
            return None
        try:
            envelope = json.loads(frozen)
        except ValueError:
            return None
        return cast(dict[str, Any], envelope) if type(envelope) is dict else None

    def payload_of(self, experiment: dict[str, Any]) -> dict[str, Any] | None:
        """The confirmation payload one record retained, or ``None`` if it is not one.

        The bytes the owner kept carry the exact payload this workspace confirmed
        with, and that payload must still agree with the record: the same draft
        revision, the same preview identity, and the same admitted input digest the
        frozen snapshot reproduces. Anything else is not a projection of this
        confirmation and is refused rather than repaired.
        """
        envelope = self.frozen_document(experiment)
        if envelope is None:
            return None
        confirmation = envelope.get("confirmation")
        if type(confirmation) is not dict or confirmation.get("schema") != CONFIRMATION_SCHEMA:
            return None
        payload = confirmation.get("payload")
        if type(payload) is not dict:
            return None
        retained = cast(dict[str, Any], payload)
        if confirmation.get("payload_sha256") != _payload_digest(retained):
            return None
        if retained.get("draft_revision") != experiment.get("draft_revision"):
            return None
        if retained.get("preview_identity") != experiment.get("preview_identity"):
            return None
        if retained.get("input_sha256") != envelope.get("snapshot_sha256"):
            return None
        if not self.frozen_matches(retained, experiment):
            return None
        return retained

    def job_record(self, experiment: dict[str, Any]) -> Any:
        """One read-only job document for a run this workspace does not store.

        Nothing is published into the workspace's job index, no attempt is rerun
        and no economic value is recomputed: the document names the owner's own
        identities, carries the admitted snapshot the confirmation froze, and
        points its artifacts at the owner's own publication directory. The
        digests are the ones the owner registered, so the existing report, path
        and analytics reads verify exactly what the owner verified.
        """
        from datetime import UTC, datetime

        from ea.web.service import JobRecord

        job_id = str(experiment.get("job_id") or "")
        envelope = self.frozen_document(experiment)
        if not job_id or envelope is None:
            raise QuineRefused("not_found", "this run has no readable frozen input")
        snapshot = envelope.get("snapshot")
        schema = envelope.get("job_schema")
        if type(snapshot) is not dict or type(schema) is not str or not schema:
            raise QuineRefused("not_found", "this run has no readable admitted input")
        status = JOB_STATUS_PROJECTION.get(str(experiment.get("status")), "interrupted")
        raw_result = experiment.get("result")
        result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
        digests: dict[str, str | None] = {}
        for name, key in ARTIFACT_RESULT_KEYS.items():
            registered = self.registered_sha256(experiment, name)
            digests[key] = registered if status == "succeeded" else None
        created_at = experiment.get("created_at")
        return JobRecord(
            job_id=job_id,
            request_id=str(experiment.get("request_id") or ""),
            scenario_id=str(envelope.get("scenario_id") or ""),
            input_identity=cast(dict[str, object], snapshot.get("source_identity") or {}),
            status=status,
            schema=schema,
            created_at=(
                datetime.fromtimestamp(int(created_at), UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                if isinstance(created_at, int)
                else None
            ),
            input_snapshot_bytes=canonical_json_bytes(snapshot),
            input_sha256=(
                envelope.get("snapshot_sha256")
                if type(envelope.get("snapshot_sha256")) is str
                else None
            ),
            attempt_id=str(experiment.get("attempt_id") or "") or None,
            engine_run_id=str(experiment.get("attempt_id") or "") or None,
            report_sha256=digests["report_sha256"],
            summary_sha256=digests["summary_sha256"],
            equity_path_sha256=digests["equity_path_sha256"],
            error_code=(
                str(result.get("code")) if status == "failed" and result.get("code") else None
            ),
            message=(
                str(result.get("message"))
                if status in {"failed", "interrupted"} and result.get("message")
                else None
            ),
            owner="quine",
            artifacts_dir=self.workspace / "official" / job_id,
        )

    # -- reads ---------------------------------------------------------------

    def phase(self, job_id: object) -> str | None:
        """The last bounded progress phase the core's own worker recorded, if any.

        This is a read of the core's own staging directory for the job the core
        itself named — the phase a stop or a completion is observed through — and
        it is absent rather than guessed when it cannot be read.
        """
        if type(job_id) is not str or not job_id:
            return None
        from ea.web import worker

        control = self.workspace / "control" / job_id
        if not control.is_dir():
            return None
        return worker.last_phase(control)

    def registered_sha256(self, experiment: dict[str, Any], name: str) -> str | None:
        """The digest the core registered for one artifact of one official result."""
        result = experiment.get("result")
        if type(result) is not dict:
            return None
        artifacts = result.get("artifacts")
        if type(artifacts) is not dict:
            return None
        entry = artifacts.get(name)
        if type(entry) is not dict:
            return None
        digest = entry.get("sha256")
        return digest if type(digest) is str and len(digest) == 64 else None

    # -- projection ----------------------------------------------------------

    def projection(
        self,
        payload: dict[str, Any],
        experiment: dict[str, Any] | None,
        barrier: dict[str, Any] | None,
        *,
        receipt_observed: bool,
    ) -> dict[str, Any]:
        """One rebuildable read projection of the core's authoritative record."""
        if experiment is None:
            if barrier is not None:
                return self._projection(
                    status="cancelled",
                    message=(
                        "this launch was cancelled before the core confirmed it; no run was "
                        "created and no result exists for it"
                    ),
                )
            if not receipt_observed:
                return self._projection(
                    status="unconfirmed",
                    message=(
                        "the core never answered this confirmation and holds no record of it; "
                        "nothing was re-sent, and this same confirmation is the only thing that "
                        "may be asked about again"
                    ),
                )
            return self._projection(
                status="unconfirmed",
                message=(
                    "the core holds no record of this confirmation; nothing was re-sent and no "
                    "run was created for it"
                ),
            )
        status = experiment.get("status")
        projected = STATUS_PROJECTION.get(str(status), "unknown")
        job_id = experiment.get("job_id")
        row = self._projection(
            status=projected,
            message=STATUS_NOTES.get(projected, "the core reported this run's state"),
            job_id=None if type(job_id) is not str or not job_id else job_id,
            attempt_id=_text(experiment.get("attempt_id")),
            frozen_sha256=_text(experiment.get("frozen_sha256")),
        )
        if projected == "running":
            row["phase"] = self.phase(job_id)
        if projected != "created":
            return row
        result = experiment.get("result")
        facts = result.get("facts") if type(result) is dict else None
        row["facts"] = facts if type(facts) is list else None
        row["report_schema"] = result.get("report_schema") if type(result) is dict else None
        row["path_schema"] = result.get("path_schema") if type(result) is dict else None
        row["report_ready"] = bool(
            type(job_id) is str
            and job_id
            and all(
                (self.workspace / "official" / job_id / name).is_file()
                and not (self.workspace / "official" / job_id / name).is_symlink()
                for name in ARTIFACTS
            )
        )
        row["report_sha256"] = self.registered_sha256(experiment, "report.json")
        if not row["report_ready"]:
            # A registered success whose published bytes are unreadable is said out
            # loud rather than presented as a readable report.
            row["message"] = (
                "the core registered this run as succeeded, but its official report is not "
                "readable in the core workspace"
            )
        # The frozen input the core retained must still be this confirmation's own
        # admitted input; a record whose bytes do not reproduce the digest, or whose
        # snapshot is another input, is reported as unknown rather than as a result.
        if not self.frozen_matches(payload, experiment):
            return self._projection(
                status="unknown",
                message=(
                    "the core's record for this confirmation does not reproduce the frozen input "
                    "that was confirmed, so its result is not presented as this run's"
                ),
                job_id=row["job_id"],
                attempt_id=row["attempt_id"],
                frozen_sha256=row["frozen_sha256"],
            )
        return row

    def frozen_matches(self, payload: dict[str, Any], experiment: dict[str, Any]) -> bool:
        encoded = experiment.get("frozen_canonical")
        claimed = experiment.get("frozen_sha256")
        if type(encoded) is not str or type(claimed) is not str:
            return False
        try:
            frozen = bytes.fromhex(encoded)
        except ValueError:
            return False
        if _digest(frozen) != claimed:
            return False
        try:
            envelope = json.loads(frozen)
        except ValueError:
            return False
        if type(envelope) is not dict:
            return False
        return bool(
            envelope.get("snapshot_sha256") == payload.get("input_sha256")
            and envelope.get("snapshot", {}).get("identity") == payload.get("input_identity")
        )

    @staticmethod
    def _projection(
        *,
        status: str,
        message: str,
        job_id: str | None = None,
        attempt_id: str | None = None,
        frozen_sha256: str | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "message": message,
            "job_id": job_id,
            "attempt_id": attempt_id,
            "frozen_sha256": frozen_sha256,
            "phase": None,
            "report_ready": False,
            "report_sha256": None,
            "facts": None,
            "report_schema": None,
            "path_schema": None,
        }


def _text(value: object) -> str | None:
    return value if type(value) is str and value else None


class QuineJobReader:
    """The core's own runs, read through the workspace's existing job vocabulary.

    This is the seam that lets the mature run, report, path, analytics and
    comparison views read a run the Rust core owns. It is read-only in the
    strongest sense: it publishes nothing into the workspace's job index, reruns
    no attempt, recomputes no economic value, and holds no state of its own. The
    documents it returns are projections of the core's live record, so an owner
    that cannot be reached raises rather than being answered from a cached claim.
    """

    def __init__(self, bridge: QuineBridge):
        self._bridge = bridge

    def job(self, job_id: str) -> Any:
        """One core-owned run as a job record, or ``None`` when the core owns none."""
        experiment = self._bridge.job_experiment(job_id)
        return None if experiment is None else self._bridge.job_record(experiment)

    def stop_job(self, job_id: str) -> Any:
        """Ask the owner to stop one of its runs, and answer with its own record."""
        experiment = self._bridge.job_experiment(job_id)
        if experiment is None:
            return None
        strategy_id = experiment.get("strategy_id")
        intent_id = experiment.get("intent_id")
        if type(strategy_id) is not str or type(intent_id) is not str:
            raise QuineRefused("not_found", "this run's owner identity is unreadable")
        self._bridge.cancel(strategy_id, intent_id)
        fresh = self._bridge.job_experiment(job_id)
        return None if fresh is None else self._bridge.job_record(fresh)
