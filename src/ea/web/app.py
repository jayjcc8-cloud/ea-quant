"""Factory for the optional loopback-only Web application."""

from __future__ import annotations

import base64
import binascii
import contextlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

import ea
from ea.plugin import historical_bars
from ea.plugin.historical_bars import MAX_PAGE_BYTES as MAX_OFFLINE_RESPONSE_BYTES
from ea.strategy.package import MAX_ARTIFACT_BYTES
from ea.web.service import (
    BatchNotFoundError,
    DuplicateBatchRunError,
    InputChangedError,
    JobNotFoundError,
    ReportUnavailableError,
    RequestConflictError,
    ScenarioNotFoundError,
    ServiceBusyError,
    WebBoundaryError,
    WebService,
    roots_overlap,
)
from ea.web.settings import RadianSettings
from ea.web.uploads import MAX_UPLOAD_BYTES, UploadError

# The bound applied to the bytes that actually arrive on a write request, before
# any JSON parser sees them. A CSV carried inside a JSON string can be escaped to
# several times its own size, so the upload envelope is larger than the file cap
# the decoded content is still held to; a declaration request carries at most one
# 16 KiB declaration plus its envelope. A file payload is carried as base64 in one
# JSON string, which is the same envelope over four thirds of the bytes.
MAX_UPLOAD_REQUEST_BYTES = 4 * MAX_UPLOAD_BYTES
MAX_DECLARATION_REQUEST_BYTES = 64 * 1024
MAX_UPLOAD_BASE64_CHARS = 4 * ((MAX_UPLOAD_BYTES + 2) // 3)
# One pasted documented response, bounded to the adapter's own single-response
# bound, plus the small JSON envelope the response text is carried inside.
MAX_OFFLINE_REQUEST_BYTES = 2 * MAX_OFFLINE_RESPONSE_BYTES + 64 * 1024
MAX_LOCAL_PACKAGE_BYTES = MAX_ARTIFACT_BYTES
MAX_LOCAL_PACKAGE_BASE64_CHARS = 4 * ((MAX_LOCAL_PACKAGE_BYTES + 2) // 3)
# One captured container plus its sealed manifest, carried as base64 in JSON.
MAX_LOCAL_PACKAGE_REQUEST_BYTES = 2 * MAX_LOCAL_PACKAGE_BASE64_CHARS + MAX_DECLARATION_REQUEST_BYTES
_BASE64_PATTERN = re.compile(r"[A-Za-z0-9+/]*={0,2}\Z")


class InputIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    scenario_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_count: int = Field(ge=1)
    dataset_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.csv$")
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class BacktestParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    initial_cash: str = Field(min_length=1, max_length=64)
    quantity: str | None = Field(default=None, max_length=64)
    strategy_parameters: dict[str, Any] | None = None
    strategy_source: dict[str, str] | None = None


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    scenario_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.ya?ml$")
    input_identity: InputIdentity
    parameters: BacktestParameters | None = None
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")


class BatchRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    strategy_parameters: dict[str, Any]


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    scenario_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.ya?ml$")
    input_identity: InputIdentity
    initial_cash: str = Field(min_length=1, max_length=64)
    runs: list[BatchRunRequest] = Field(min_length=2, max_length=10)


class HoldoutRequest(BaseModel):
    dataset_id: str | None = None
    source_sha256: str | None = None
    model_config = ConfigDict(extra="forbid", strict=True)

    source_job_id: str = Field(min_length=1, max_length=128)
    scenario_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.ya?ml$")


class CandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    validation_id: str = Field(min_length=36, max_length=36)


class CandidateDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    outcome: str = Field(pattern=r"^(ACCEPTED|REJECTED)$")
    reason: str = Field(min_length=1, max_length=4096)


class ScenarioValidationRequest(BaseModel):
    dataset_id: str | None = None
    source_sha256: str | None = None
    model_config = ConfigDict(extra="forbid", strict=True)

    parameters: BacktestParameters | None = None


class ResearchTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    idea: str = Field(min_length=1, max_length=4000)
    # Explicit capability selection per layer, e.g. {"strategy": {"plugin_id":
    # "ea.builtin.strategy.bounded-long-v1", "package_version": "1.0.0"}}.
    # Absent keeps the host default route.
    selection: dict[str, Any] | None = None


class ResearchSpecEdit(BaseModel):
    """Optional partial edit of the unconfirmed draft spec."""

    model_config = ConfigDict(extra="forbid", strict=True)

    strategy_id: str | None = None
    venue: str | None = None
    symbol: str | None = None
    initial_cash: str | None = None
    rationale: str | None = None
    scenario_id: str | None = None
    dataset_id: str | None = None
    parameters: dict[str, Any] | None = None
    assumptions: list[str] | None = None
    # Send {} to clear an explicit selection back to the host default.
    selection: dict[str, Any] | None = None


class PluginRecipeRequest(BaseModel):
    """One capability selection to resolve without dispatching anything."""

    model_config = ConfigDict(extra="forbid", strict=True)
    selection: dict[str, Any] | None = None


class LocalStrategyInspectRequest(BaseModel):
    """One captured package plus the capability it claims: read, never trusted.

    ``content_base64`` carries the file's own bytes, exactly as the browser read
    them, and ``manifest`` is the sealed declarative body the package claims. No
    path, URL or module is ever read from the request: the captured bytes and one
    small JSON document are the whole input.
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    content_base64: str | None = Field(default=None, max_length=MAX_LOCAL_PACKAGE_BASE64_CHARS)
    manifest: dict[str, Any] | None = None


class LocalStrategyRegisterRequest(BaseModel):
    """One explicit trust decision over exactly the identity that was inspected.

    ``trusted`` must be true — this host never records a default — and every
    expected value is the operator's own reading of the preview, compared against
    what the static inspection actually measured.
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    content_base64: str | None = Field(default=None, max_length=MAX_LOCAL_PACKAGE_BASE64_CHARS)
    manifest: dict[str, Any] | None = None
    trusted: bool | None = None
    effects: list[str] | None = None
    base_scenario_id: str | None = None
    expected_plugin_id: str | None = None
    expected_package_version: str | None = None
    expected_manifest_digest: str | None = None
    expected_implementation_digest: str | None = None


class UploadPreviewRequest(BaseModel):
    """Bounded upload bytes and, in mapped mode, the uploader's own declaration.

    Exactly one payload field carries the upload: ``content`` is strict UTF-8 text
    and is encoded once to the bytes it names, ``content_base64`` carries a file's
    own bytes unchanged. ``mode`` selects the strict profile or the explicit
    mapping, and in mapped mode both declaration fields are required.
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    content: str | None = Field(default=None, max_length=MAX_UPLOAD_BYTES)
    content_base64: str | None = Field(default=None, max_length=MAX_UPLOAD_BASE64_CHARS)
    file_name: str | None = None
    mode: Literal["strict", "explicit-mapping"] | None = None
    mapping: dict[str, Any] | None = None
    time_declaration: dict[str, Any] | None = None
    availability_policy: str | None = None
    latency_seconds: int | None = None


class UploadPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    content: str | None = Field(default=None, max_length=MAX_UPLOAD_BYTES)
    content_base64: str | None = Field(default=None, max_length=MAX_UPLOAD_BASE64_CHARS)
    file_name: str | None = None
    mode: Literal["strict", "explicit-mapping"] | None = None
    mapping: dict[str, Any] | None = None
    time_declaration: dict[str, Any] | None = None
    source_reference: str | None = None
    license_reference: str | None = None
    # No default: a caller states how the capture became available, or the store
    # refuses the upload instead of recording an assertion nobody made.
    availability_policy: str | None = None
    latency_seconds: int | None = None


class OfflineHistoryRequest(BaseModel):
    """One pasted historical response plus the user's own explicit declarations.

    ``content`` is the response body's exact UTF-8 text, bounded to the adapter's
    single-response bound; it is encoded once to the bytes it names. ``synthetic``
    has no default: the host never decides by itself that input is the built-in
    example, and a synthetic marking is accepted only for the example's own bytes
    and declaration. Source, rights and intended use are the uploader's own
    assertions for a user paste and are refused for the example.
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    content: str | None = Field(default=None, max_length=MAX_OFFLINE_RESPONSE_BYTES)
    venue: str | None = None
    symbol: str | None = None
    interval: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    latency_seconds: int | None = None
    synthetic: bool | None = None
    declared_source: str | None = None
    declared_rights: str | None = None
    intended_use: str | None = None


class OfflineHistoryPublishRequest(OfflineHistoryRequest):
    """One publication, bound to the exact preview identity it was previewed as."""

    preview_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class DeclarationRequest(BaseModel):
    """One declaration body; it is verified before anything is registered."""

    model_config = ConfigDict(extra="forbid", strict=True)
    declaration: dict[str, Any]


class ResearchConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    auto_run: bool = True


class ApprovedBudgetRequest(BaseModel):
    """The explicit approval one model call or experiment needs, and nothing else."""

    model_config = ConfigDict(extra="forbid", strict=True)
    max_model_calls: int = Field(ge=0, le=100)
    max_backtests: int = Field(ge=0, le=100)


class UserStrategyCreateRequest(BaseModel):
    """Creating a strategy is a save: no provider call and no experiment."""

    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=120)
    intent: str = Field(min_length=1, max_length=2000)
    scenario_id: str = Field(min_length=1, max_length=200)
    # The user's own criterion in the report's registered vocabulary, or none.
    criterion: dict[str, Any] | None = None


class UserStrategyMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    call_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    base_revision: int = Field(ge=1, le=1000000)
    text: str = Field(min_length=1, max_length=4000)
    approved_budget: ApprovedBudgetRequest


class UserStrategyReviseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    call_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    text: str = Field(default="", max_length=4000)
    approved_budget: ApprovedBudgetRequest


class UserStrategyApplyRequest(BaseModel):
    """The user's selection: the whole proposal, or exactly the checked paths."""

    model_config = ConfigDict(extra="forbid", strict=True)
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    paths: list[str] | None = Field(default=None, max_length=64)


class UserStrategyUndoRequest(BaseModel):
    """One named application of the revision the caller is looking at."""

    model_config = ConfigDict(extra="forbid", strict=True)
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    head_revision: int = Field(ge=1, le=1000000)
    target_operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class UserStrategyLaunchRequest(BaseModel):
    """One explicit confirmation of one already-previewed experiment."""

    model_config = ConfigDict(extra="forbid", strict=True)
    launch_intent_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    base_revision: int = Field(ge=1, le=1000000)
    # The identity of the preview this confirmation was made against: the exact
    # admitted input, not just the draft revision.
    preview_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approved_budget: ApprovedBudgetRequest


class EventSimulationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    as_of: str = Field(min_length=1, max_length=64)


@dataclass(frozen=True, slots=True)
class WebSettings:
    """Explicit filesystem and loopback boundaries for one local service."""

    scenario_root: Path
    workspace: Path
    ui_dir: Path
    port: int
    strategy_root: Path | None = None
    data_root: Path | None = None
    runtime_root: Path | None = None
    alerts_stream: Path | None = None

    @property
    def trusted_origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def create_app(settings: WebSettings) -> Any:
    """Create the optional FastAPI application without importing it eagerly."""
    from fastapi import FastAPI, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import FileResponse, JSONResponse, Response

    # FastAPI resolves an endpoint's annotations against this module's globals, so the
    # lazily imported Request class has to be nameable there for the endpoints that
    # read their own bounded body. Importing it at module scope would make importing
    # this module require the optional web extra.
    globals()["Request"] = Request

    try:
        scenario_root = settings.scenario_root.resolve(strict=True)
        workspace_root = settings.workspace.resolve(strict=False)
        ui_root = settings.ui_dir.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise WebBoundaryError("local Web roots cannot be resolved") from None
    if roots_overlap(scenario_root, workspace_root, ui_root):
        raise WebBoundaryError("scenario, workspace, and UI roots must not overlap")
    index_candidate = ui_root / "index.html"
    try:
        index_path = index_candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise WebBoundaryError("ui directory must contain index.html") from None
    if (
        not ui_root.is_dir()
        or index_candidate.is_symlink()
        or not index_path.is_relative_to(ui_root)
        or not index_path.is_file()
    ):
        raise WebBoundaryError("ui directory must contain index.html")
    if settings.strategy_root is not None and roots_overlap(
        settings.strategy_root.resolve(), ui_root
    ):
        raise WebBoundaryError("strategy and UI roots must not overlap")
    if settings.data_root is not None and roots_overlap(settings.data_root.resolve(), ui_root):
        raise WebBoundaryError("data and UI roots must not overlap")
    if settings.runtime_root is not None:
        if not settings.runtime_root.is_absolute():
            raise WebBoundaryError("runtime root must be absolute")
        if roots_overlap(settings.runtime_root.resolve(), scenario_root, workspace_root, ui_root):
            raise WebBoundaryError("runtime root must be separate from the Web roots")
    if settings.alerts_stream is not None and not settings.alerts_stream.is_absolute():
        raise WebBoundaryError("alerts stream must be an absolute path")
    service = WebService(
        scenario_root,
        workspace_root,
        strategy_root=settings.strategy_root,
        data_root=settings.data_root,
    )

    from ea.plugin.manifest import ManifestError
    from ea.web.research import (
        InvalidSpecError,
        ResearchOrchestrator,
        ResearchStore,
        ResearchTaskError,
        SelectionRejectedError,
        TaskConflictError,
        TaskNotFoundError,
        TaskStateError,
    )

    research_store = ResearchStore(workspace_root)
    research_orchestrator = ResearchOrchestrator(
        research_store, service, RadianSettings(RadianSettings.default_path())
    )

    from ea.web.user_strategies import (
        UserStrategyBudgetError,
        UserStrategyConflictError,
        UserStrategyError,
        UserStrategyNotFoundError,
        UserStrategyService,
        UserStrategyStaleError,
        UserStrategyStateError,
        UserStrategyStore,
    )

    user_strategy_service = UserStrategyService(
        UserStrategyStore(workspace_root),
        research_orchestrator,
        service,
        RadianSettings(RadianSettings.default_path()),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        service.start()
        try:
            # Reconcile interrupted research against the real job index before the
            # local API answers. This classifies crashed work and rebinds an
            # already-created job; it never dispatches a provider or a backtest.
            # A single unreadable task record must not block the local service,
            # so a reconciliation error leaves the record for its own read path.
            with contextlib.suppress(OSError, ResearchTaskError):
                research_orchestrator.reconcile_all()
            # The same read-only classification for a strategy's experiments: an
            # interrupted launch is re-bound to the child and job it really has,
            # and nothing is dispatched, re-sent or refunded here.
            with contextlib.suppress(OSError, UserStrategyError):
                user_strategy_service.reconcile_all()
            yield
        finally:
            service.stop()

    app = FastAPI(
        title="EA Local Web",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    def error(status_code: int, code: str, message: str) -> JSONResponse:
        return JSONResponse(
            status_code=status_code, content={"error": {"code": code, "message": message}}
        )

    # The codes that describe the state of this workspace's slot rather than the
    # request: an unusable, replaced or unwritable slot is a conflict, and every
    # other local-package refusal is a refusal of what the request claimed.
    slot_store_codes = frozenset({"store_untrusted", "slot_write_failed", "slot_unreadable"})

    def slot_refusal(caught: ManifestError) -> JSONResponse:
        return error(409 if caught.code in slot_store_codes else 422, caught.code, caught.message)

    class _RequestTooLarge(Exception):
        """One write body arrived with more bytes than this host reads."""

    def too_large(scope: str) -> JSONResponse:
        return error(413, "request_too_large", f"the {scope} request exceeds the host limit")

    def malformed() -> JSONResponse:
        return error(422, "invalid_request", "request does not match the local Web API contract")

    async def _bounded_document(request: Request, limit: int) -> object:
        """Read one JSON write body, bounding the stream before any parser sees it.

        ``Content-Length`` is only a hint: it may be absent, and a client may send
        more or fewer bytes than it declares. The limit is therefore applied to the
        bytes that actually arrive, so no parser — and no model validation — ever
        runs over an unbounded body.
        """
        import json

        received = 0
        chunks: list[bytes] = []
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit:
                raise _RequestTooLarge
            chunks.append(chunk)
        return json.loads(b"".join(chunks))

    def _upload_bytes(parsed: UploadPreviewRequest | UploadPublishRequest) -> bytes:
        """The exact bytes one upload request carries, and nothing else.

        One of the two payload fields is required and both together are refused,
        so there is never a question of which one the host read. Text is encoded
        once to the bytes it names; base64 is decoded once and the file's own bytes
        are then the upload, which is what keeps a BOM, a CRLF ending or a non-ASCII
        byte exactly as it arrived.
        """
        if (parsed.content is None) == (parsed.content_base64 is None):
            raise UploadError("upload_invalid", "send exactly one of content or content_base64")
        if parsed.content is not None:
            return parsed.content.encode("utf-8")
        encoded = parsed.content_base64
        assert encoded is not None
        if len(encoded) % 4 != 0 or _BASE64_PATTERN.fullmatch(encoded) is None:
            raise UploadError("upload_invalid", "content_base64 is not one bounded base64 payload")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise UploadError("upload_invalid", "content_base64 is not valid base64") from None
        if not payload or len(payload) > MAX_UPLOAD_BYTES:
            raise UploadError(
                "upload_invalid", "the decoded upload is empty or above the size limit"
            )
        return payload

    def _local_package_bytes(encoded: str | None) -> bytes:
        """One captured .eastrategy container, decoded once and bounded first.

        The bytes are the file's own; the manifest travels beside them as JSON. No
        path, URL or module name is ever read from a request, so the host opens
        nothing a caller named.
        """
        if encoded is None:
            raise ManifestError("package_invalid", "send the captured package as content_base64")
        if len(encoded) % 4 != 0 or _BASE64_PATTERN.fullmatch(encoded) is None:
            raise ManifestError(
                "package_invalid", "content_base64 is not one bounded base64 payload"
            )
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ManifestError("package_invalid", "content_base64 is not valid base64") from None
        if not payload or len(payload) > MAX_LOCAL_PACKAGE_BYTES:
            raise ManifestError(
                "package_invalid", "the decoded package is empty or above the size limit"
            )
        return payload

    def _mapping_declaration(
        parsed: UploadPreviewRequest | UploadPublishRequest,
    ) -> dict[str, Any] | None:
        """The explicit mapping declaration, or None for the strict profile.

        Mapped mode is selected only by the request's own ``mode`` field, and it
        requires both declaration fields. A mapping sent in strict mode is refused
        rather than quietly applied to an upload that declared itself strict.
        """
        mapped = parsed.mode == "explicit-mapping"
        if not mapped and (parsed.mapping is not None or parsed.time_declaration is not None):
            raise UploadError(
                "upload_invalid",
                "mapping and time_declaration are read only in explicit-mapping mode",
            )
        if not mapped:
            return None
        if parsed.mapping is None or parsed.time_declaration is None:
            raise UploadError(
                "mapping_declaration",
                "an explicit mapping needs both 'mapping' and 'time_declaration'",
            )
        return {"mapping": parsed.mapping, "time_declaration": parsed.time_declaration}

    def _offline_fields(parsed: OfflineHistoryRequest) -> dict[str, Any]:
        """The declarations one offline request carries, and nothing else.

        Absent values stay absent: the store, not this boundary, is what decides
        that a missing latency, a missing synthetic marking or a missing rights
        assertion is a refusal rather than a default.
        """
        return {
            "venue": parsed.venue,
            "symbol": parsed.symbol,
            "interval": parsed.interval,
            "start_ms": parsed.start_ms,
            "end_ms": parsed.end_ms,
            "latency_seconds": parsed.latency_seconds,
            "synthetic": parsed.synthetic,
            "declared_source": parsed.declared_source,
            "declared_rights": parsed.declared_rights,
            "intended_use": parsed.intended_use,
        }

    def _offline_content(parsed: OfflineHistoryRequest) -> bytes:
        """The exact bytes one pasted response carries.

        The text is encoded once to UTF-8, so what the adapter hashes is the bytes
        the paste itself denotes, and the adapter's own bound is the only size
        limit applied to them.
        """
        if parsed.content is None:
            raise UploadError("offline_response_invalid", "send the pasted response as content")
        return parsed.content.encode("utf-8")

    def _declaration_bytes(document: dict[str, Any]) -> str:
        """One declaration body as this host reads it.

        A body authored without an integrity digest is sealed here, because the
        host is what computes a declaration's integrity and the user may type the
        body by hand. A body that already carries one is passed through
        unchanged, so a wrong digest is refused rather than silently repaired.
        """
        import json

        from ea.plugin.declarative import DECLARATION_SCHEMA, seal_declaration

        body = dict(document)
        if "artifact_digest" not in body:
            try:
                body = seal_declaration({**body, "schema": DECLARATION_SCHEMA})
            except (TypeError, ValueError):
                # Unsealable input is left for the declaration decoder to refuse.
                body = {**body, "schema": DECLARATION_SCHEMA}
        return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return error(422, "invalid_request", "request does not match the local Web API contract")

    @app.middleware("http")
    async def local_browser_boundary(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.headers.get("host") != f"127.0.0.1:{settings.port}":
            return error(400, "invalid_host", "request host is not the configured loopback service")
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if request.headers.get("origin") != settings.trusted_origin:
                return error(403, "invalid_origin", "write request origin is not trusted")
            if request.headers.get("x-ea-web-request") != "1":
                return error(403, "missing_request_header", "write request header is required")
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return error(415, "json_required", "write requests require application/json")
        return await call_next(request)

    from ea.web.events import SAMPLES, EventView, sample_document

    events = EventView(workspace_root)

    @app.get("/api/events")
    def event_catalog() -> object:
        try:
            return {"samples": [{"sample_id": name, **sample_document(name)} for name in SAMPLES]}
        except (OSError, ValueError):
            return error(409, "event_unavailable", "Offline event samples are unavailable")

    @app.get("/api/events/{sample_id}")
    def event_result(sample_id: str) -> object:
        try:
            return events.view(sample_id)
        except KeyError:
            return error(404, "event_not_found", "Unknown offline event sample")
        except (OSError, ValueError):
            return error(409, "event_unavailable", "Saved event evidence is unavailable")

    @app.post("/api/events/{sample_id}/simulate")
    def event_simulate(sample_id: str, request: EventSimulationRequest) -> object:
        from ea.product.prediction import instant

        try:
            instant(request.as_of)
        except (ValueError, TypeError):
            return error(422, "event_time_invalid", "as_of must be an explicit UTC timestamp")
        try:
            return events.view(sample_id, request.as_of)
        except KeyError:
            return error(404, "event_not_found", "Unknown offline event sample")
        except (OSError, ValueError) as caught:
            return error(
                409,
                "event_simulation_rejected",
                f"Simulation not confirmed: {caught}. Refresh saved evidence before retrying.",
            )

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {
            "service": "ea-local-web",
            "version": ea.__version__,
            "offline_only": True,
            "single_active_job": True,
        }

    @app.get("/api/candidates")
    def list_candidates() -> object:
        try:
            return {"candidates": service.list_candidates()}
        except (OSError, ValueError, KeyError, TypeError):
            return error(409, "candidate_unavailable", "Candidate evidence is unavailable")

    @app.post("/api/candidates", status_code=201)
    def create_candidate(request: CandidateRequest) -> object:
        try:
            return service.create_candidate(request.validation_id)
        except (OSError, ValueError, KeyError, TypeError):
            return error(
                409,
                "candidate_unavailable",
                "Candidate creation was not confirmed. Check saved candidates before retrying; "
                "verified source and Holdout evidence are required.",
            )

    @app.get("/api/candidates/{candidate_id}")
    def get_candidate(candidate_id: str) -> object:
        try:
            return service.get_candidate(candidate_id)
        except (OSError, ValueError, KeyError, TypeError):
            return error(409, "candidate_unavailable", "Candidate evidence is unavailable")

    @app.get("/api/candidates/{candidate_id}/simulation-preparation")
    def simulation_preparation(candidate_id: str) -> object:
        from ea.web.simulation import prepare_simulation

        try:
            return prepare_simulation(service.workspace, candidate_id)
        except (ValueError, OSError):
            return error(
                409,
                "simulation_evidence_unavailable",
                "Simulation preparation failed: evidence unavailable",
            )

    @app.post("/api/candidates/{candidate_id}/decision")
    def decide_candidate(candidate_id: str, request: CandidateDecisionRequest) -> object:
        try:
            return service.decide_candidate(candidate_id, request.outcome, request.reason)
        except (OSError, ValueError, KeyError, TypeError):
            return error(
                409,
                "candidate_decision_rejected",
                "Decision was not confirmed. Reload the candidate to check its state; "
                "verified evidence, a reason and a legal transition are required.",
            )

    @app.get("/api/datasets")
    def list_datasets() -> object:
        return {"datasets": service.dataset_catalog.list()}

    @app.get("/api/datasets/{dataset_id}")
    def inspect_dataset(dataset_id: str) -> object:
        try:
            return service.dataset_catalog.inspect(dataset_id)
        except (OSError, ValueError):
            return error(422, "dataset_invalid", "dataset is invalid or unavailable")

    @app.post("/api/datasets/upload/preview")
    async def preview_upload(request: Request) -> object:
        """A bounded sample and quality view; nothing is published by a preview."""
        try:
            parsed = UploadPreviewRequest.model_validate(
                await _bounded_document(request, MAX_UPLOAD_REQUEST_BYTES)
            )
        except _RequestTooLarge:
            return too_large("upload")
        except (TypeError, ValueError):
            return malformed()
        try:
            payload = _upload_bytes(parsed)
            declaration = _mapping_declaration(parsed)
        except UploadError as caught:
            return error(422, caught.code, caught.message)
        if declaration is None:
            return service.uploads.preview(payload)
        return service.uploads.preview_mapped(
            payload,
            mapping=declaration["mapping"],
            time_declaration=declaration["time_declaration"],
            availability_policy=parsed.availability_policy,
            latency_seconds=parsed.latency_seconds,
        )

    @app.post("/api/datasets/upload", status_code=201)
    async def publish_upload(request: Request) -> object:
        """Publish one verified upload as one immutable, explicitly-policied version."""
        try:
            parsed = UploadPublishRequest.model_validate(
                await _bounded_document(request, MAX_UPLOAD_REQUEST_BYTES)
            )
        except _RequestTooLarge:
            return too_large("upload")
        except (TypeError, ValueError):
            return malformed()
        try:
            payload = _upload_bytes(parsed)
            declaration = _mapping_declaration(parsed)
            manifest = service.uploads.publish(
                payload,
                file_name=parsed.file_name,
                source_reference=parsed.source_reference,
                license_reference=parsed.license_reference,
                availability_policy=parsed.availability_policy,
                latency_seconds=parsed.latency_seconds,
                mapping=None if declaration is None else declaration["mapping"],
                time_declaration=(None if declaration is None else declaration["time_declaration"]),
            )
        except UploadError as caught:
            return error(422, caught.code, caught.message)
        except OSError:
            return error(409, "upload_unavailable", "the upload could not be published")
        return {**manifest, "datasets": service.dataset_catalog.list()}

    @app.get("/api/datasets/offline-history/example")
    def offline_history_example() -> object:
        """This host's own labelled synthetic example, for a full local proof.

        The example is generated here rather than accepted from a caller, and its
        bytes are the only bytes the adapter will mark synthetic. Nothing about it
        claims a venue, a supplier or a right.
        """
        return historical_bars.synthetic_example_document()

    @app.post("/api/datasets/offline-history/preview")
    async def preview_offline_history(request: Request) -> object:
        """Normalize one pasted offline response; a preview publishes nothing."""
        try:
            parsed = OfflineHistoryRequest.model_validate(
                await _bounded_document(request, MAX_OFFLINE_REQUEST_BYTES)
            )
        except _RequestTooLarge:
            return too_large("offline history")
        except (TypeError, ValueError):
            return malformed()
        try:
            content = _offline_content(parsed)
        except UploadError as caught:
            return error(422, caught.code, caught.message)
        return service.uploads.preview_offline_history(content, **_offline_fields(parsed))

    @app.post("/api/datasets/offline-history", status_code=201)
    async def publish_offline_history(request: Request) -> object:
        """Publish one pasted response as one immutable, re-derivable version.

        The request must name the preview identity this host handed out for its
        own bytes and declarations; the store recomputes that identity and refuses
        anything else, so a changed response or a changed assertion is a refusal
        rather than a publication.
        """
        try:
            parsed = OfflineHistoryPublishRequest.model_validate(
                await _bounded_document(request, MAX_OFFLINE_REQUEST_BYTES)
            )
        except _RequestTooLarge:
            return too_large("offline history")
        except (TypeError, ValueError):
            return malformed()
        try:
            content = _offline_content(parsed)
            manifest = service.uploads.publish_offline_history(
                content,
                preview_sha256=parsed.preview_sha256,
                **_offline_fields(parsed),
            )
        except UploadError as caught:
            return error(422, caught.code, caught.message)
        except OSError:
            return error(409, "offline_history_unavailable", "the response could not be published")
        return {**manifest, "datasets": service.dataset_catalog.list()}

    @app.get("/api/plugins/declarations")
    def list_declarations() -> object:
        try:
            return research_orchestrator.declaration_catalog()
        except SelectionRejectedError as caught:
            return error(409, "declaration_unavailable", str(caught))

    @app.post("/api/plugins/declarations/validate")
    async def validate_declaration(request: Request) -> object:
        """Verify one declaration against this host; register and dispatch nothing."""
        try:
            parsed = DeclarationRequest.model_validate(
                await _bounded_document(request, MAX_DECLARATION_REQUEST_BYTES)
            )
        except _RequestTooLarge:
            return too_large("declaration")
        except (TypeError, ValueError):
            return malformed()
        return research_orchestrator.inspect_declaration(_declaration_bytes(parsed.declaration))

    @app.post("/api/plugins/declarations", status_code=201)
    async def register_declaration(request: Request) -> object:
        """Explicit user registration of one verified declaration."""
        try:
            parsed = DeclarationRequest.model_validate(
                await _bounded_document(request, MAX_DECLARATION_REQUEST_BYTES)
            )
        except _RequestTooLarge:
            return too_large("declaration")
        except (TypeError, ValueError):
            return malformed()
        outcome = research_orchestrator.register_declaration(_declaration_bytes(parsed.declaration))
        if not outcome["usable"]:
            return JSONResponse(status_code=422, content=outcome)
        return outcome

    @app.get("/api/plugins/local-strategy")
    def local_strategy_slot() -> object:
        """What this workspace's one local package slot currently is.

        An unreadable, replaced or symlinked slot is a readable refusal here rather
        than an unhandled failure: the caller learns the slot is not trusted.
        """
        try:
            return service.local_strategy_status()
        except ManifestError as caught:
            return slot_refusal(caught)
        except (OSError, WebBoundaryError, ValueError) as caught:
            return error(409, "slot_unreadable", str(caught))

    @app.post("/api/plugins/local-strategy/inspect")
    async def inspect_local_strategy(request: Request) -> object:
        """Read one captured package statically: no trust, no write, no execution."""
        try:
            parsed = LocalStrategyInspectRequest.model_validate(
                await _bounded_document(request, MAX_LOCAL_PACKAGE_REQUEST_BYTES)
            )
        except _RequestTooLarge:
            return too_large("local strategy")
        except (TypeError, ValueError):
            return malformed()
        if parsed.manifest is None:
            return malformed()
        try:
            payload = _local_package_bytes(parsed.content_base64)
            outcome = service.inspect_local_strategy(manifest=parsed.manifest, payload=payload)
        except ManifestError as caught:
            return slot_refusal(caught)
        except OSError as caught:
            return error(409, "slot_unreadable", str(caught))
        except (WebBoundaryError, ValueError) as caught:
            return error(422, "package_invalid", str(caught))
        return outcome

    @app.post("/api/plugins/local-strategy/register", status_code=201)
    async def register_local_strategy(request: Request) -> object:
        """One explicit trusted decision: capture, verify, and seal this identity."""
        try:
            parsed = LocalStrategyRegisterRequest.model_validate(
                await _bounded_document(request, MAX_LOCAL_PACKAGE_REQUEST_BYTES)
            )
        except _RequestTooLarge:
            return too_large("local strategy")
        except (TypeError, ValueError):
            return malformed()
        if parsed.manifest is None or parsed.base_scenario_id is None:
            return malformed()
        try:
            payload = _local_package_bytes(parsed.content_base64)
            outcome = service.register_local_strategy(
                manifest=parsed.manifest,
                payload=payload,
                trusted=parsed.trusted,
                effects=tuple(parsed.effects or ()),
                base_scenario_id=parsed.base_scenario_id,
                expected_plugin_id=parsed.expected_plugin_id,
                expected_package_version=parsed.expected_package_version,
                expected_manifest_digest=parsed.expected_manifest_digest,
                expected_implementation_digest=parsed.expected_implementation_digest,
            )
        except ManifestError as caught:
            return slot_refusal(caught)
        except OSError as caught:
            return error(409, "slot_unreadable", str(caught))
        except (WebBoundaryError, ValueError) as caught:
            return error(422, "registration_refused", str(caught))
        return outcome

    @app.post("/api/plugins/local-strategy/revoke")
    def revoke_local_strategy() -> object:
        """Withdraw trust in the workspace-local slot, durably."""
        try:
            return service.revoke_local_strategy()
        except ManifestError as caught:
            return slot_refusal(caught)
        except (OSError, WebBoundaryError, ValueError) as caught:
            return error(409, "revocation_failed", str(caught))

    @app.get("/api/scenarios")
    def list_scenarios() -> object:
        try:
            return {"scenarios": service.registry.list()}
        except WebBoundaryError as caught:
            return error(500, "scenario_root_unavailable", str(caught))

    @app.post("/api/scenarios/{scenario_id}/validate")
    def validate_scenario(
        scenario_id: str,
        request: ScenarioValidationRequest | None = None,
    ) -> object:
        try:
            return service.validate_scenario(
                scenario_id,
                dataset_id=None if request is None else request.dataset_id,
                source_sha256=None if request is None else request.source_sha256,
                parameters=(
                    None
                    if request is None or request.parameters is None
                    else request.parameters.model_dump()
                ),
            )
        except InputChangedError as caught:
            return error(409, "input_conflict", str(caught))
        except ScenarioNotFoundError as caught:
            return error(404, "scenario_not_found", str(caught))
        except WebBoundaryError as caught:
            return error(422, "scenario_invalid", str(caught))
        except Exception as caught:
            from ea.product import BacktestScenarioError

            if isinstance(caught, (BacktestScenarioError, WebBoundaryError)):
                return error(422, "scenario_invalid", str(caught))
            return error(
                500, "scenario_validation_failed", "scenario validation failed unexpectedly"
            )

    @app.post("/api/backtests")
    def create_backtest(request: BacktestRequest) -> Response:
        try:
            record, created = service.create_job(
                scenario_id=request.scenario_id,
                input_identity=request.input_identity.model_dump(exclude_none=True),
                parameters=None if request.parameters is None else request.parameters.model_dump(),
                request_id=request.request_id,
            )
            return JSONResponse(
                status_code=202 if created else 200, content=record.document(presentation=True)
            )
        except ScenarioNotFoundError as caught:
            return error(404, "scenario_not_found", str(caught))
        except (InputChangedError, RequestConflictError) as caught:
            return error(409, "input_conflict", str(caught))
        except ServiceBusyError as caught:
            return error(409, "service_busy", str(caught))
        except Exception as caught:
            from ea.product import BacktestScenarioError

            if isinstance(caught, (BacktestScenarioError, WebBoundaryError)):
                return error(422, "scenario_invalid", str(caught))
            return error(500, "job_acceptance_failed", "backtest request could not be accepted")

    @app.get("/api/backtests")
    def list_backtests() -> dict[str, object]:
        return {"jobs": service.list_jobs()}

    @app.post("/api/batches")
    def create_batch(request: BatchRequest) -> Response:
        try:
            document = service.create_batch(
                scenario_id=request.scenario_id,
                input_identity=request.input_identity.model_dump(exclude_none=True),
                initial_cash=request.initial_cash,
                runs=[item.strategy_parameters for item in request.runs],
            )
            return JSONResponse(status_code=202, content=document)
        except ScenarioNotFoundError as caught:
            return error(404, "scenario_not_found", str(caught))
        except InputChangedError as caught:
            return error(409, "input_conflict", str(caught))
        except ServiceBusyError as caught:
            return error(409, "service_busy", str(caught))
        except DuplicateBatchRunError as caught:
            return error(422, "duplicate_batch_run", str(caught))
        except Exception as caught:
            from ea.product import BacktestScenarioError

            if isinstance(caught, (BacktestScenarioError, WebBoundaryError)):
                return error(422, "scenario_invalid", str(caught))
            return error(500, "batch_acceptance_failed", "batch request could not be accepted")

    @app.get("/api/batches/{batch_id}")
    def get_batch(batch_id: str) -> object:
        try:
            return service.get_batch(batch_id)
        except BatchNotFoundError as caught:
            return error(404, "batch_not_found", str(caught))

    @app.post("/api/holdouts")
    def create_holdout(request: HoldoutRequest) -> Response:
        from ea.product import BacktestScenarioError

        try:
            return JSONResponse(
                status_code=202,
                content=service.create_holdout(
                    source_job_id=request.source_job_id,
                    scenario_id=request.scenario_id,
                    dataset_id=request.dataset_id,
                    source_sha256=request.source_sha256,
                ),
            )
        except (JobNotFoundError, ScenarioNotFoundError) as caught:
            return error(404, "not_found", str(caught))
        except ServiceBusyError as caught:
            return error(409, "service_busy", str(caught))
        except (WebBoundaryError, BacktestScenarioError, ValueError) as caught:
            return error(422, "holdout_invalid", str(caught))
        except Exception:
            return error(500, "holdout_acceptance_failed", "holdout request could not be accepted")

    @app.get("/api/holdouts")
    def list_holdouts() -> object:
        return {"holdouts": service.list_holdouts()}

    @app.get("/api/holdouts/{validation_id}")
    def get_holdout(validation_id: str) -> object:
        try:
            return service.get_holdout(validation_id)
        except JobNotFoundError as caught:
            return error(404, "holdout_not_found", str(caught))

    @app.get("/api/backtests/{job_id}/holdout-scenarios")
    def holdout_scenarios(job_id: str) -> object:
        try:
            return {"scenarios": service.holdout_candidates(job_id)}
        except JobNotFoundError as caught:
            return error(404, "job_not_found", str(caught))
        except (WebBoundaryError, ValueError) as caught:
            return error(422, "holdout_invalid", str(caught))

    @app.get("/api/backtests/{job_id}")
    def get_backtest(job_id: str) -> object:
        try:
            return service.get_job(job_id).document(presentation=True)
        except JobNotFoundError as caught:
            return error(404, "job_not_found", str(caught))

    @app.get("/api/backtests/{job_id}/report")
    def get_report(job_id: str) -> Response:
        try:
            return Response(content=service.report(job_id), media_type="application/json")
        except JobNotFoundError as caught:
            return error(404, "job_not_found", str(caught))
        except ReportUnavailableError as caught:
            return error(409, "report_unavailable", str(caught))

    @app.get("/api/backtests/{job_id}/trade-analytics")
    def get_trade_analytics(job_id: str) -> object:
        try:
            return service.trade_analytics(job_id)
        except JobNotFoundError as caught:
            return error(404, "job_not_found", str(caught))
        except ReportUnavailableError as caught:
            return error(409, "report_unavailable", str(caught))

    @app.get("/api/backtests/{job_id}/artifacts/{name}")
    def get_artifact(job_id: str, name: str) -> Response:
        try:
            media_type = (
                "application/json" if name in {"report.json", "equity-path.json"} else "text/plain"
            )
            return Response(
                content=service.artifact(job_id, name),
                media_type=media_type,
                headers={"content-disposition": f'attachment; filename="{name}"'},
            )
        except JobNotFoundError as caught:
            return error(404, "artifact_not_found", str(caught))
        except ReportUnavailableError as caught:
            return error(409, "report_unavailable", str(caught))

    from fastapi import Query

    from ea.web import radian

    @app.get("/api/workspace/overview")
    def workspace_overview() -> object:
        try:
            return radian.overview(
                service,
                runtime_root=settings.runtime_root,
                alerts_stream=settings.alerts_stream,
                settings=RadianSettings(RadianSettings.default_path()),
            )
        except (WebBoundaryError, ValueError, OSError) as caught:
            return error(500, "workspace_overview_unavailable", str(caught))

    @app.get("/api/paper/overview")
    def paper_overview_route(run_id: str | None = None) -> object:
        try:
            return radian.paper_overview(settings.runtime_root, run_id=run_id)
        except (WebBoundaryError, ValueError, OSError) as caught:
            return error(500, "paper_overview_unavailable", str(caught))

    @app.get("/api/paper/events")
    def paper_events_route(
        run_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> object:
        try:
            return radian.paper_events(settings.runtime_root, run_id=run_id, limit=limit)
        except (WebBoundaryError, ValueError, OSError) as caught:
            return error(500, "paper_events_unavailable", str(caught))

    @app.get("/api/search")
    def search_route(q: str = Query(min_length=2, max_length=200)) -> object:
        try:
            return radian.search(service, q)
        except (WebBoundaryError, ValueError, OSError) as caught:
            return error(500, "search_unavailable", str(caught))

    @app.get("/api/settings/status")
    def settings_status_route() -> object:
        try:
            return RadianSettings(RadianSettings.default_path()).status()
        except ValueError as caught:
            return error(500, "settings_unavailable", str(caught))

    @app.post("/api/research/tasks", status_code=201)
    def create_research_task(request: ResearchTaskRequest) -> object:
        try:
            task = research_orchestrator.create(request.idea, request.selection)
            return task.document()
        except TaskConflictError as caught:
            return error(409, "research_task_exists", str(caught))
        except SelectionRejectedError as caught:
            return error(422, "selection_rejected", str(caught))
        except InvalidSpecError as caught:
            return error(422, "spec_invalid", str(caught))
        except ResearchTaskError as caught:
            return error(422, "research_task_invalid", str(caught))

    @app.get("/api/plugins/catalog")
    def plugin_catalog_route() -> object:
        """Declared built-in capabilities plus the host's verified view."""
        try:
            return research_orchestrator.plugin_catalog()
        except ManifestError as caught:
            return error(500, "plugin_catalog_unavailable", str(caught))

    @app.post("/api/plugins/recipe/resolve")
    def resolve_recipe_route(request: PluginRecipeRequest) -> object:
        """Resolve and verify one selection; dispatch nothing, grant nothing."""
        return research_orchestrator.resolve_recipe(request.selection)

    @app.get("/api/research/tasks")
    def list_research_tasks() -> object:
        """Every listing re-verifies the conclusions it serves.

        A task whose reconciliation fails is still listed from its durable
        record, so a read never drops a task; a conclusion that can no longer
        be proven from its verified report is withdrawn rather than displayed.
        """
        reconciled = {task.task_id: task for task in research_orchestrator.reconcile_all()}
        return {
            "tasks": [
                reconciled.get(task.task_id, task).document() for task in research_store.list()
            ]
        }

    @app.get("/api/research/tasks/{task_id}")
    def get_research_task(task_id: str) -> object:
        try:
            refreshed = research_orchestrator.refresh(task_id)
            return refreshed.document()
        except TaskNotFoundError as caught:
            return error(404, "research_task_not_found", str(caught))
        except ResearchTaskError as caught:
            return error(422, "research_task_invalid", str(caught))

    @app.patch("/api/research/tasks/{task_id}/spec")
    def edit_research_spec(task_id: str, request: ResearchSpecEdit) -> object:
        try:
            fields = {
                key: value for key, value in request.model_dump().items() if value is not None
            }
            task = research_orchestrator.edit_spec(task_id, fields)
            return task.document()
        except TaskNotFoundError as caught:
            return error(404, "research_task_not_found", str(caught))
        except SelectionRejectedError as caught:
            return error(409, "selection_rejected", str(caught))
        except (InvalidSpecError, TaskStateError) as caught:
            return error(409, "spec_edit_rejected", str(caught))
        except ResearchTaskError as caught:
            return error(422, "research_task_invalid", str(caught))

    @app.post("/api/research/tasks/{task_id}/confirm")
    def confirm_research_task(
        task_id: str, request: ResearchConfirmRequest | None = None
    ) -> object:
        try:
            task = research_orchestrator.confirm(
                task_id, auto_run=True if request is None else request.auto_run
            )
            return task.document()
        except TaskNotFoundError as caught:
            return error(404, "research_task_not_found", str(caught))
        except SelectionRejectedError as caught:
            return error(409, "selection_rejected", str(caught))
        except (InvalidSpecError, TaskStateError) as caught:
            return error(409, "confirm_rejected", str(caught))
        except ResearchTaskError as caught:
            return error(422, "research_task_invalid", str(caught))

    @app.post("/api/research/tasks/{task_id}/retry")
    def retry_research_task(task_id: str) -> object:
        try:
            task = research_orchestrator.retry(task_id)
            return task.document()
        except TaskNotFoundError as caught:
            return error(404, "research_task_not_found", str(caught))
        except TaskStateError as caught:
            return error(409, "retry_rejected", str(caught))
        except ResearchTaskError as caught:
            return error(422, "research_task_invalid", str(caught))

    @app.post("/api/research/tasks/{task_id}/cancel")
    def cancel_research_task(task_id: str) -> object:
        try:
            task = research_orchestrator.cancel(task_id)
            return task.document()
        except TaskNotFoundError as caught:
            return error(404, "research_task_not_found", str(caught))
        except ResearchTaskError as caught:
            return error(422, "research_task_invalid", str(caught))

    @app.post("/api/research/tasks/{task_id}/interpret")
    def interpret_research_task(task_id: str) -> object:
        """The explicit one-shot conclusion step for a succeeded, verified run."""
        try:
            task = research_orchestrator.interpret(task_id)
            return task.document()
        except TaskNotFoundError as caught:
            return error(404, "research_task_not_found", str(caught))
        except TaskStateError as caught:
            return error(409, "interpret_rejected", str(caught))
        except ResearchTaskError as caught:
            return error(422, "research_task_invalid", str(caught))

    # -- one durable user strategy ------------------------------------------
    #
    # These routes own a draft, the AI calls the user approved, the typed diffs
    # applied to that draft, and the experiments confirmed from it. Every identity
    # in the path is bounded before it can name a file, every write body is a
    # strict schema, and the same-origin middleware at the top of this app applies
    # unchanged. Nothing here is a second engine or a second scheduler: the
    # experiment path reuses the research task, its request id and its dispatch.

    def strategy_refusal(caught: UserStrategyError | OSError) -> JSONResponse:
        if isinstance(caught, UserStrategyNotFoundError):
            return error(404, "user_strategy_not_found", str(caught))
        if isinstance(caught, OSError):
            return error(500, "user_strategy_unavailable", str(caught))
        if isinstance(caught, UserStrategyStaleError):
            return error(409, "stale_base_revision", str(caught))
        if isinstance(caught, UserStrategyConflictError):
            return error(409, "operation_conflict", str(caught))
        if isinstance(caught, UserStrategyBudgetError):
            return error(422, "approval_required", str(caught))
        if isinstance(caught, UserStrategyStateError):
            return error(422, "user_strategy_invalid", str(caught))
        return error(422, "user_strategy_invalid", str(caught))

    @app.post("/api/user-strategies", status_code=201)
    def create_user_strategy(request: UserStrategyCreateRequest) -> object:
        try:
            strategy = user_strategy_service.create(
                name=request.name,
                intent=request.intent,
                scenario_id=request.scenario_id,
                criterion=request.criterion,
            )
            return strategy.document()
        except (UserStrategyError, OSError) as caught:
            return strategy_refusal(caught)

    @app.get("/api/user-strategies")
    def list_user_strategies() -> object:
        return {"strategies": [item.document() for item in user_strategy_service.list()]}

    @app.get("/api/user-strategies/{user_strategy_id}")
    def get_user_strategy(user_strategy_id: str) -> object:
        try:
            # Every read re-classifies an interrupted experiment against the real
            # child and job records; it never dispatches anything.
            return user_strategy_service.reconcile(user_strategy_id).document()
        except (UserStrategyError, OSError) as caught:
            return strategy_refusal(caught)

    @app.post("/api/user-strategies/{user_strategy_id}/messages")
    def send_user_strategy_message(
        user_strategy_id: str, request: UserStrategyMessageRequest
    ) -> object:
        try:
            strategy = user_strategy_service.send_message(
                user_strategy_id,
                call_id=request.call_id,
                base_revision=request.base_revision,
                text=request.text,
                approved_budget=request.approved_budget.model_dump(),
            )
            return strategy.document()
        except (UserStrategyError, OSError) as caught:
            return strategy_refusal(caught)

    @app.post("/api/user-strategies/{user_strategy_id}/proposals/{proposal_id}/apply")
    def apply_user_strategy_proposal(
        user_strategy_id: str, proposal_id: str, request: UserStrategyApplyRequest
    ) -> object:
        try:
            strategy = user_strategy_service.apply_proposal(
                user_strategy_id,
                proposal_id=proposal_id,
                operation_id=request.operation_id,
                paths=request.paths,
            )
            return strategy.document()
        except (UserStrategyError, OSError) as caught:
            return strategy_refusal(caught)

    @app.post("/api/user-strategies/{user_strategy_id}/proposals/{proposal_id}/reject")
    def reject_user_strategy_proposal(user_strategy_id: str, proposal_id: str) -> object:
        try:
            return user_strategy_service.reject_proposal(
                user_strategy_id, proposal_id=proposal_id
            ).document()
        except (UserStrategyError, OSError) as caught:
            return strategy_refusal(caught)

    @app.post("/api/user-strategies/{user_strategy_id}/proposals/{proposal_id}/revise")
    def revise_user_strategy_proposal(
        user_strategy_id: str, proposal_id: str, request: UserStrategyReviseRequest
    ) -> object:
        try:
            strategy = user_strategy_service.revise_proposal(
                user_strategy_id,
                proposal_id=proposal_id,
                call_id=request.call_id,
                text=request.text,
                approved_budget=request.approved_budget.model_dump(),
            )
            return strategy.document()
        except (UserStrategyError, OSError) as caught:
            return strategy_refusal(caught)

    @app.post("/api/user-strategies/{user_strategy_id}/undo")
    def undo_user_strategy_change(
        user_strategy_id: str, request: UserStrategyUndoRequest
    ) -> object:
        try:
            return user_strategy_service.undo(
                user_strategy_id,
                operation_id=request.operation_id,
                head_revision=request.head_revision,
                target_operation_id=request.target_operation_id,
            ).document()
        except (UserStrategyError, OSError) as caught:
            return strategy_refusal(caught)

    @app.get("/api/user-strategies/{user_strategy_id}/experiment/preview")
    def preview_user_strategy_experiment(user_strategy_id: str) -> object:
        try:
            return user_strategy_service.preview_experiment(user_strategy_id)
        except (UserStrategyError, OSError) as caught:
            return strategy_refusal(caught)

    @app.post("/api/user-strategies/{user_strategy_id}/experiments")
    def launch_user_strategy_experiment(
        user_strategy_id: str, request: UserStrategyLaunchRequest
    ) -> object:
        try:
            strategy = user_strategy_service.launch_experiment(
                user_strategy_id,
                launch_intent_id=request.launch_intent_id,
                base_revision=request.base_revision,
                preview_identity=request.preview_identity,
                approved_budget=request.approved_budget.model_dump(),
            )
            return strategy.document()
        except (UserStrategyError, OSError) as caught:
            return strategy_refusal(caught)

    @app.get("/{full_path:path}")
    def frontend(full_path: str) -> Response:
        if full_path == "api" or full_path.startswith("api/"):
            return error(404, "api_not_found", "API route was not found")
        candidate = (ui_root / full_path).resolve()
        if candidate.is_relative_to(ui_root) and candidate.is_file() and not candidate.is_symlink():
            return FileResponse(candidate)
        return FileResponse(index_path)

    return app


__all__ = ["WebSettings", "create_app"]
