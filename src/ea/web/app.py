"""Factory for the optional loopback-only Web application."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

import ea
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


class ScenarioValidationRequest(BaseModel):
    dataset_id: str | None = None
    source_sha256: str | None = None
    model_config = ConfigDict(extra="forbid", strict=True)

    parameters: BacktestParameters | None = None


@dataclass(frozen=True, slots=True)
class WebSettings:
    """Explicit filesystem and loopback boundaries for one local service."""

    scenario_root: Path
    workspace: Path
    ui_dir: Path
    port: int
    strategy_root: Path | None = None
    data_root: Path | None = None

    @property
    def trusted_origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def create_app(settings: WebSettings) -> Any:
    """Create the optional FastAPI application without importing it eagerly."""
    from fastapi import FastAPI, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import FileResponse, JSONResponse, Response

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
    service = WebService(
        scenario_root,
        workspace_root,
        strategy_root=settings.strategy_root,
        data_root=settings.data_root,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        service.start()
        try:
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

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {
            "service": "ea-local-web",
            "version": ea.__version__,
            "offline_only": True,
            "single_active_job": True,
        }

    @app.get("/api/datasets")
    def list_datasets() -> object:
        return {"datasets": [] if service.datasets is None else service.datasets.list()}

    @app.get("/api/datasets/{dataset_id}")
    def inspect_dataset(dataset_id: str) -> object:
        try:
            if service.datasets is None:
                raise ValueError("no data root is configured")
            return service.datasets.inspect(dataset_id)
        except (OSError, ValueError):
            return error(422, "dataset_invalid", "dataset is invalid or unavailable")

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
