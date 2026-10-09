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
from uuid import uuid4

from ea.core.encoding import canonical_json_bytes

PROTOCOL = "quine.v1"

#: The one document Python freezes inside the confirmed canonical input so the
#: owner retains, verbatim, what the confirmation was made of. The core stores
#: these bytes opaquely; they are the confirmation's own metadata, not a second
#: authority and not something Rust parses.
CONFIRMATION_SCHEMA = "ea.quine-confirmation.v1"

#: The metadata block one chronological holdout's frozen input carries. It names
#: the one source run the holdout continues, together with that run's own frozen
#: input and official result digests, so the core's own confirmation transaction
#: can prove the reference before the holdout is admitted. It is part of the
#: frozen bytes, so the frozen-input digest already covers it.
HOLDOUT_SCHEMA = "ea.quine-holdout.v1"

#: The experiment namespace one holdout is confirmed under. A holdout is not a
#: draft experiment of the user's strategy, so it is never restored into that
#: strategy's own experiment list.
HOLDOUT_OWNER = "holdout"

#: The owner marker every job record this bridge projects carries, and the one
#: marker the workspace reads to know a run is the core's and not its own.
JOB_OWNER = "quine"

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

    def freeze_holdout(
        self, request: dict[str, Any], *, extras: dict[str, Any] | None = None
    ) -> QuineFrozen:
        """One holdout's frozen input, rendered by the domain adapter.

        The adapter re-derives the target input from the *source run's* retained
        envelope, so what is frozen continues that run's own strategy, parameters
        and cost assumptions under a later data binding — never today's draft and
        never the target's defaults. ``extras`` is merged in before the bytes are
        canonicalized, which is how the holdout's own reference metadata becomes
        part of the digest the owner retains.
        """
        from ea.web import rust_domain

        reply = rust_domain.run("rebind", request)
        if reply.get("status") != "ok":
            code = reply.get("code") or "quine_refused"
            message = reply.get("message") or "the holdout input could not be rendered"
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
        if type(snapshot_sha256) is not str:
            raise QuineUnavailable("the frozen input carries no admitted snapshot digest")
        return QuineFrozen(
            envelope=document,
            canonical=canonical,
            sha256=_digest(canonical),
            snapshot_sha256=snapshot_sha256,
        )

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
        """The retained frozen input of one record, held to its own digest.

        The bytes are also re-verified as a frozen input in their own right — the
        snapshot against its digest, the frozen scenario and strategy artifact
        against theirs, and the released dataset version against the capability
        binding it was confirmed under — so a record whose retained identity does
        not reproduce itself is read as having no frozen input rather than being
        projected as a run of something this workspace cannot name.
        """
        from ea.web import rust_domain

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
        if type(envelope) is not dict:
            return None
        try:
            rust_domain._decode_envelope(cast(dict[str, Any], envelope))  # noqa: SLF001
        except rust_domain.DomainRefusal:
            return None
        return cast(dict[str, Any], envelope)

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
            # The frozen capabilities and published version the owner retained for
            # this run, read from the same verified envelope: a reader sees exactly
            # what the confirmation was bound to rather than an absent field.
            plugin_binding=(
                cast(dict[str, object], envelope["plugin_binding"])
                if type(envelope.get("plugin_binding")) is dict
                else None
            ),
            dataset_version=(
                cast(dict[str, object], envelope["dataset_version"])
                if type(envelope.get("dataset_version")) is dict
                else None
            ),
            # A frozen local artifact is presented from its own static identity: the
            # package is inspected, never imported, so a revoked or replaced slot
            # cannot make a historical read execute anything.
            strategy_package=self._package_of(envelope),
            owner=JOB_OWNER,
            artifacts_dir=self.workspace / "official" / job_id,
        )

    @staticmethod
    def _package_of(envelope: dict[str, Any]) -> Any:
        """The frozen strategy artifact of one envelope, inspected and not executed."""
        from ea.strategy.package import inspect_package

        encoded = envelope.get("package_file")
        claimed = envelope.get("package_sha256")
        if type(encoded) is not str or type(claimed) is not str:
            return None
        try:
            return inspect_package(bytes.fromhex(encoded), expected_sha256=claimed)
        except (OSError, TypeError, ValueError):
            return None

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


@dataclass(frozen=True, slots=True)
class QuineHoldoutSource:
    """One core-owned source run a chronological holdout may continue.

    ``document`` is the source's own frozen canonical scenario, and the two
    digests are the owner's registered identities of the run: the frozen input it
    confirmed and the official result it committed. The core re-proves both when
    it admits a holdout, so this is the reference the holdout is admitted over
    rather than a claim this workspace makes about it.
    """

    job_id: str
    scenario_id: str
    envelope: dict[str, Any]
    document: dict[str, Any]
    frozen_sha256: str
    result_sha256: str


class QuineHoldouts:
    """Chronological holdouts over runs the Rust core owns.

    A holdout is a second, independent experiment: the target input is rendered
    from the *source run's own retained envelope* — its exact strategy, parameters,
    funding, cost, risk and instrument — under a strictly later data binding, and
    it is confirmed through the same core confirmation, dispatch, cancellation,
    recovery and result path every other experiment uses. Nothing here holds a
    relation, a job or a second store: the relationship is derived from the
    owner's own experiment record and the metadata inside its frozen input, so a
    restart rebuilds it from the owner rather than from a cached claim. An owner
    that cannot be reached raises rather than answering with nothing.
    """

    def __init__(self, bridge: QuineBridge, service: Any, research: Any) -> None:
        self._bridge = bridge
        self._service = service
        #: The workspace's own catalog resolver, used only to name the exact data
        #: capability a target dataset is bound to. It is the host's authority:
        #: nothing a caller sends is ever accepted as a capability identity.
        self._research = research

    # -- the source ----------------------------------------------------------

    def source(self, job_id: str) -> QuineHoldoutSource:
        """One succeeded core-owned run, or a refusal that names why not."""
        from ea.web.service import JobNotFoundError, WebBoundaryError

        experiment = self._bridge.job_experiment(job_id)
        if experiment is None:
            raise JobNotFoundError("job was not found in this workspace")
        if experiment.get("status") != "succeeded":
            raise WebBoundaryError(
                "a chronological holdout continues one succeeded run; this source run is "
                f"{experiment.get('status')}"
            )
        envelope = self._bridge.frozen_document(experiment)
        if envelope is None:
            raise WebBoundaryError("this source run has no readable frozen input")
        snapshot = envelope.get("snapshot")
        identity = None if type(snapshot) is not dict else snapshot.get("identity")
        document = None if type(snapshot) is not dict else snapshot.get("scenario")
        scenario_id = envelope.get("scenario_id")
        frozen_sha256 = experiment.get("frozen_sha256")
        result = experiment.get("result")
        if (
            type(document) is not dict
            or type(identity) is not dict
            or type(scenario_id) is not str
            or not scenario_id
            or type(frozen_sha256) is not str
            or type(result) is not dict
        ):
            raise WebBoundaryError("this source run has no readable admitted input")
        # The run's verified formal report is its own result evidence, and it must
        # name the same scenario identity its frozen input admits. A report that
        # has been removed, replaced or republished is not a source a holdout may
        # continue.
        report = json.loads(self._service.report(job_id))
        if report.get("source", {}).get("scenario_sha256") != identity.get("scenario_sha256"):
            raise WebBoundaryError(
                "this source run's verified report and its frozen input do not name the "
                "same scenario"
            )
        return QuineHoldoutSource(
            job_id=job_id,
            scenario_id=scenario_id,
            envelope=envelope,
            document=cast(dict[str, Any], document),
            frozen_sha256=frozen_sha256,
            result_sha256=_digest(canonical_json_bytes(cast(dict[str, Any], result))),
        )

    # -- the target ----------------------------------------------------------

    def _request(
        self, source: QuineHoldoutSource, scenario_id: str, dataset_id: str | None
    ) -> dict[str, Any]:
        """One rebind request over this workspace's own registered entrances.

        A registered target is named by the scenario file this workspace would
        load; a selected dataset is named by its catalog identity and resolved
        through the same registered root and controlled upload store the rest of
        the product uses. No per-dataset path and no caller-supplied digest is
        ever part of the request.
        """
        request: dict[str, Any] = {"source": source.envelope, "scenario_id": scenario_id}
        if dataset_id is None:
            request["scenario_path"] = str(self._service.registered_scenario_file(scenario_id))
            strategy_root = self._service.registry.strategy_root
            if strategy_root is not None:
                request["strategy_root"] = str(strategy_root)
            return request
        request["dataset_id"] = dataset_id
        request["workspace_root"] = str(self._service.uploads.root.parent)
        if self._service.datasets is not None:
            request["data_root"] = str(self._service.datasets.root)
        if self._service.uploads.has(dataset_id):
            binding = self._research.catalog_binding(dataset_id=dataset_id)
            if binding is not None:
                request["binding"] = binding.document()
        return request

    def candidates(self, source: QuineHoldoutSource) -> list[dict[str, object]]:
        """Every compatible, strictly later target this source could continue into.

        Each candidate is resolved and parameterized exactly as the confirmation
        would freeze it, and is simply left out when it cannot be — a candidate
        list is an offer, and nothing is created here.
        """
        from ea.web import rust_domain
        from ea.web.service import _scenario_summary

        items: list[dict[str, object]] = []
        for path in sorted(self._service.registry.root.glob("*.y*ml")):
            try:
                scenario_id, scenario = rust_domain.holdout_scenario(
                    self._request(source, path.name, None)
                )
            except (rust_domain.DomainRefusal, OSError, ValueError):
                continue
            items.append(_scenario_summary(scenario_id, scenario))
        for entry in self._service.dataset_catalog.list():
            dataset_id = entry.get("dataset_id")
            if not entry.get("valid") or type(dataset_id) is not str or not dataset_id:
                continue
            try:
                scenario_id, scenario = rust_domain.holdout_scenario(
                    self._request(source, source.scenario_id, dataset_id)
                )
            except (rust_domain.DomainRefusal, OSError, ValueError):
                continue
            summary = _scenario_summary(scenario_id, scenario)
            summary["name"] = dataset_id
            items.append(summary)
        return items

    def create(
        self,
        source: QuineHoldoutSource,
        *,
        scenario_id: str,
        dataset_id: str | None = None,
        source_sha256: str | None = None,
        expected_identity: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """Confirm one holdout under the owner, and answer with its relationship.

        The confirmation carries the same stable intent semantics every other
        experiment uses: a lost reply is resolved by querying that one intent, so
        an unobserved answer never becomes a second run, and a refusal is the
        core's own. Nothing is written here — the relationship is the owner's own
        record.

        ``expected_identity`` is the admitted input the caller was *shown* and is
        now confirming. It is compared with the identity of the very same frozen
        input this call is about to send, so a target that changed between the
        selection and the click refuses here — before the owner reserves anything —
        instead of running bytes the user never saw. This source is owned by the
        experiment core, so the identity is required: a client that cannot state
        what it confirmed is told to refresh and reselect rather than confirmed
        against whatever the target holds now.
        """
        from ea.data.historical import HistoricalMarketDataError
        from ea.web.service import InputChangedError, WebBoundaryError

        if dataset_id is not None:
            try:
                _path, dataset = self._service.dataset_catalog.load(dataset_id)
            except (HistoricalMarketDataError, OSError, ValueError):
                raise WebBoundaryError(
                    f"{dataset_id} is not a dataset this workspace can bind a run to"
                ) from None
            if source_sha256 is not None and dataset.source_bytes_sha256.value != source_sha256:
                raise InputChangedError("holdout dataset changed; reselect it")
        validation_id = str(uuid4())
        metadata = {
            "schema": HOLDOUT_SCHEMA,
            "validation_id": validation_id,
            "source_job_id": source.job_id,
            "source_frozen_sha256": source.frozen_sha256,
            "source_result_sha256": source.result_sha256,
        }
        try:
            frozen = self._bridge.freeze_holdout(
                self._request(source, scenario_id, dataset_id), extras={"holdout": metadata}
            )
        except QuineRefused as refusal:
            # Rendering the target input is this workspace's own validation, and it
            # happens before the owner is reached at all. A target this workspace
            # cannot freeze — an incompatible or non-later scenario, a parameter the
            # later window cannot support — is refused as that validation, not as a
            # decision the owner made.
            raise WebBoundaryError(refusal.message) from None
        admitted = self._admitted_identity(frozen)
        if expected_identity is None:
            raise WebBoundaryError(
                "this source run is held by the experiment core, so the admitted input "
                "that was confirmed is required with the request; refresh the candidates "
                "and reselect the target before confirming"
            )
        if dict(expected_identity) != admitted:
            raise InputChangedError(
                "holdout input changed; refresh and reselect the scenario before confirming"
            )
        receipt = self._bridge.confirm(
            strategy_id=HOLDOUT_OWNER,
            intent_id=validation_id,
            draft_revision=1,
            preview_identity=f"sha256:{frozen.sha256}",
            max_model_calls=0,
            frozen=frozen,
        )
        if receipt.experiment is None:
            raise QuineUnavailable(
                "the core's answer to this holdout confirmation could not be established; "
                "the holdout may already exist. Nothing was re-sent. Reopen saved holdouts "
                "when the core is available before starting another evaluation"
            )
        relation = self._relation(receipt.experiment)
        if relation is None:
            raise WebBoundaryError(
                "the core's record for this holdout does not name the source run it was "
                "confirmed over"
            )
        return relation

    # -- reads ---------------------------------------------------------------

    def relations(self) -> list[dict[str, str]]:
        """Every holdout this owner holds, derived from the owner's own records."""
        found: list[dict[str, str]] = []
        for experiment in self._bridge.experiments():
            relation = self._relation(experiment)
            if relation is not None:
                found.append(relation)
        return found

    def relation(self, validation_id: str) -> dict[str, str]:
        from ea.web.service import JobNotFoundError

        for relation in self.relations():
            if relation["validation_id"] == validation_id:
                return relation
        raise JobNotFoundError("holdout was not found in this workspace")

    @staticmethod
    def _admitted_identity(frozen: QuineFrozen) -> dict[str, Any]:
        """The admitted input identity of one frozen holdout, as it will be sent.

        It is read from the frozen document this call is about to confirm — the
        envelope, its snapshot and, through the snapshot digest the core re-checks,
        the exact bytes — so comparing it is comparing what will really run rather
        than a second rendering of the same scenario.
        """
        from ea.web.service import WebBoundaryError

        snapshot = frozen.envelope.get("snapshot")
        identity = None if type(snapshot) is not dict else snapshot.get("identity")
        if type(identity) is not dict:
            raise WebBoundaryError("the frozen holdout input carries no admitted identity")
        return cast(dict[str, Any], identity)

    def _relation(self, experiment: dict[str, Any]) -> dict[str, str] | None:
        """One relationship, or ``None`` when this record is not a holdout at all.

        The metadata travels inside the frozen input the owner retained, so the
        relationship is read from the same bytes the confirmation was made of and
        is held to the workspace's own relationship contract before it is served.
        """
        from datetime import UTC, datetime

        from ea.web.holdout import HoldoutRecord, decode_holdout

        envelope = self._bridge.frozen_document(experiment)
        if envelope is None:
            return None
        holdout = envelope.get("holdout")
        if type(holdout) is not dict or holdout.get("schema") != HOLDOUT_SCHEMA:
            return None
        job_id = experiment.get("job_id")
        created_at = experiment.get("created_at")
        validation_id = holdout.get("validation_id")
        source_job_id = holdout.get("source_job_id")
        if (
            type(job_id) is not str
            or not job_id
            or type(created_at) is not int
            or type(validation_id) is not str
            or type(source_job_id) is not str
        ):
            return None
        record = HoldoutRecord(
            validation_id=validation_id,
            created_at=datetime.fromtimestamp(created_at, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            source_job_id=source_job_id,
            holdout_job_id=job_id,
        )
        try:
            decode_holdout(canonical_json_bytes(record.document()))
        except (TypeError, ValueError):
            return None
        return record.document()
