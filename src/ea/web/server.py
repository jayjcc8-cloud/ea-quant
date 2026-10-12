"""Installed loopback server entrypoint for the optional Web extra."""

from __future__ import annotations

import importlib
import sys

from ea.web.app import WebSettings, create_app

# What the server actually uses from the ASGI stack: `create_app` imports these
# FastAPI names lazily, and the server loop calls `uvicorn.run`. Each one is
# looked up rather than assumed, because a directory left behind by an
# interrupted uninstall can make `import uvicorn` succeed as a namespace package
# that provides no `run` at all.
UVICORN_RUN = "run"
RUNTIME_IMPORTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fastapi", ("FastAPI", "Request", "Query")),
    ("fastapi.exceptions", ("RequestValidationError",)),
    ("fastapi.responses", ("FileResponse", "JSONResponse", "Response")),
)


class WebDependencyError(RuntimeError):
    """The optional Web runtime dependencies are unavailable."""


def _dependency_error(source: str, error: ImportError) -> WebDependencyError:
    """Name the import that actually failed, and the interpreter that looked.

    The message carries no environment value, no settings path and no key: it is
    the missing module, whatever the import system said about it, and the
    interpreter that tried, so the report can be acted on where it was printed.
    """
    missing = getattr(error, "name", None) or source
    return WebDependencyError(
        f"the Web dependency {missing!r} could not be imported: {error} "
        f"(interpreter {sys.executable}); install the locked environment with the 'web' extra"
    )


def _unavailable_symbol(module_name: str, attribute: str) -> WebDependencyError:
    return WebDependencyError(
        f"the Web dependency {module_name!r} provides no {attribute!r} "
        f"(interpreter {sys.executable}); reinstall the locked environment with the 'web' "
        "extra so that package is a real distribution rather than an empty namespace"
    )


def check_web_dependencies() -> None:
    """Import the ASGI stack the server uses, without building the application.

    Uvicorn is checked first: it is what is missing loudest when the Web extra
    was never installed, and a namespace-only package passes `import uvicorn`
    while providing no callable `run`. Importing a module is not enough for the
    FastAPI names either, so every symbol the application and the server loop
    use is looked up here rather than discovered by a failing request.
    """
    try:
        uvicorn = importlib.import_module("uvicorn")
    except ImportError as error:
        raise _dependency_error("uvicorn", error) from error
    if not callable(getattr(uvicorn, UVICORN_RUN, None)):
        raise _unavailable_symbol("uvicorn", UVICORN_RUN)

    for module_name, attributes in RUNTIME_IMPORTS:
        try:
            module = importlib.import_module(module_name)
        except ImportError as error:
            raise _dependency_error(module_name, error) from error
        for attribute in attributes:
            if getattr(module, attribute, None) is None:
                raise _unavailable_symbol(module_name, attribute)


def serve_local_web(settings: WebSettings) -> None:
    """Run exactly one loopback-only Uvicorn process."""
    check_web_dependencies()
    try:
        application = create_app(settings)
    except ImportError as error:
        # The application's own lazy imports fail the same way, and their detail
        # is what names the module that is actually missing.
        raise _dependency_error("ea.web.app", error) from error
    uvicorn = importlib.import_module("uvicorn")
    uvicorn.run(
        application,
        host="127.0.0.1",
        port=settings.port,
        proxy_headers=False,
        server_header=False,
    )


__all__ = ["WebDependencyError", "check_web_dependencies", "serve_local_web"]
