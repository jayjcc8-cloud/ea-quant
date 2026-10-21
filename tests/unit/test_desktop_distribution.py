"""Distribution checks are read-only; fixture signatures are not signing evidence."""

import importlib.util
import io
import json
import plistlib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "receipt",
    [
        {"status": "interrupted", "core_exit": 70},
        {"status": "close_blocked", "code": "research_stop_unconfirmed"},
        {"status": "closed", "core_exit": False},
    ],
)
def test_acceptance_never_counts_interrupted_or_unproven_exit_as_clean(
    receipt: dict[str, Any],
) -> None:
    module = script("accept_desktop")

    class Process:
        stdin = io.StringIO()

        def wait(self, timeout: int) -> int:
            raise AssertionError("must validate the receipt first")

    child = Process()
    module.line = lambda _: receipt
    with pytest.raises(AssertionError):
        module.stop(child)
    assert child.stdin.getvalue() == "shutdown\n"
    assert not child.stdin.closed


def test_acceptance_keeps_liveness_open_until_clean_receipt_and_reap() -> None:
    module = script("accept_desktop")

    class Process:
        stdin = io.StringIO()

        def wait(self, timeout: int) -> int:
            assert not self.stdin.closed
            assert self.stdin.getvalue() == "shutdown\n"
            return 0

    child = Process()
    module.line = lambda _: {"status": "closed", "core_exit": 0}
    assert module.stop(child)["core_exit"] == 0
    assert child.stdin.closed


@pytest.mark.parametrize(
    "fault", ["none", "adhoc", "wrong_source", "dirty", "wrong_version", "wrong_team", "no_ticket"]
)
def test_distribution_requires_all_independent_proofs(tmp_path: Path, fault: str) -> None:
    module = script("check_desktop_distribution")
    app = tmp_path / "QUINE.app"
    runtime = app / "Contents/Resources/runtime"
    runtime.mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "org.quine.research.desktop",
                "CFBundleShortVersionString": "0.1.0",
            }
        )
    )
    (runtime / "build-info.json").write_text(
        json.dumps(
            {
                "source_revision": "b" * 40 if fault == "wrong_source" else "a" * 40,
                "source_dirty": fault == "dirty",
                "desktop_version": "0.2.0" if fault == "wrong_version" else "0.1.0",
            }
        )
    )
    calls = []

    def run(argv: list[str]) -> tuple[bool, str]:
        calls.append(argv)
        assert not any(word in argv for word in ("staple", "--sign", "submit"))
        if "-dv" in argv:
            return True, "flags=0x2(adhoc)" if fault == "adhoc" else "flags=0x10000(runtime)"
        if "-R" in argv:
            assert argv[argv.index("-R") + 1] == (
                "=anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] exists"
                " and certificate leaf[field.1.2.840.113635.100.6.1.13] exists"
                ' and certificate leaf[subject.OU] = "ABCDEFGHIJ"'
            )
            return fault not in {"adhoc", "wrong_team"}, "fixture certificate check"
        if "stapler" in argv:
            return fault != "no_ticket", "fixture ticket check"
        return True, "fixture integrity check"

    module.run = run
    result = module.check(app, "a" * 40, "0.1.0", "ABCDEFGHIJ")
    assert result["distribution_checks_pass"] == (fault == "none")
    assert result["release_authorized"] is False
    assert calls
