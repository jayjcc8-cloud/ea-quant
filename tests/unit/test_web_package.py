from __future__ import annotations

import importlib.util
from pathlib import Path


def test_web_package_is_available_without_eager_framework_imports() -> None:
    spec = importlib.util.find_spec("ea.web")

    assert spec is not None
    assert importlib.util.find_spec("fastapi") is not None


def test_web_app_exposes_explicit_local_roots() -> None:
    from ea.web.app import WebSettings, create_app

    settings = WebSettings(
        scenario_root=Path("/tmp/scenarios"),
        workspace=Path("/tmp/workspace"),
        ui_dir=Path("/tmp/ui"),
        port=8765,
    )

    assert settings.trusted_origin == "http://127.0.0.1:8765"
    assert callable(create_app)
