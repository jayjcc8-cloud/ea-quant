"""Installed loopback server entrypoint for the optional Web extra."""

from __future__ import annotations

from ea.web.app import WebSettings, create_app


class WebDependencyError(RuntimeError):
    """The optional Web runtime dependencies are unavailable."""


def serve_local_web(settings: WebSettings) -> None:
    """Run exactly one loopback-only Uvicorn process."""
    try:
        import uvicorn
    except ModuleNotFoundError:
        raise WebDependencyError(
            "Web dependencies are missing; install the candidate wheel with the 'web' extra"
        ) from None
    try:
        application = create_app(settings)
    except ModuleNotFoundError:
        raise WebDependencyError(
            "Web dependencies are missing; install the candidate wheel with the 'web' extra"
        ) from None
    uvicorn.run(
        application,
        host="127.0.0.1",
        port=settings.port,
        proxy_headers=False,
        server_header=False,
    )


__all__ = ["WebDependencyError", "serve_local_web"]
