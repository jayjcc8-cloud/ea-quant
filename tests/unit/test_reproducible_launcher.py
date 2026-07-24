from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Protocol, cast

import pytest


class _Snapshot(Protocol):
    root: Path


class _LauncherModule(Protocol):
    __file__: str
    PreflightError: type[Exception]

    def _repository_snapshot(self, repository: Path) -> _Snapshot: ...

    def _validate_source_tree(self, snapshot: _Snapshot) -> None: ...


_LAUNCHER_PATH = Path(__file__).resolve().parents[2] / "scripts/reproducible_run.py"
_LAUNCHER_SPEC = importlib.util.spec_from_file_location(
    "ea_test_reproducible_launcher",
    _LAUNCHER_PATH,
)
if _LAUNCHER_SPEC is None or _LAUNCHER_SPEC.loader is None:
    raise RuntimeError("cannot load the reproducible launcher for tests")
_LAUNCHER_MODULE = importlib.util.module_from_spec(_LAUNCHER_SPEC)
sys.modules[_LAUNCHER_SPEC.name] = _LAUNCHER_MODULE
_LAUNCHER_SPEC.loader.exec_module(_LAUNCHER_MODULE)
launcher = cast(_LauncherModule, _LAUNCHER_MODULE)


def _run(*arguments: str, cwd: Path) -> None:
    subprocess.run(
        list(arguments),
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    _run("git", "init", "-q", cwd=repository)
    _run("git", "config", "user.name", "EA Test", cwd=repository)
    _run("git", "config", "user.email", "ea@example.invalid", cwd=repository)
    _write(repository / "src/ea/__init__.py", '__version__ = "0.1.1"\n')
    _write(repository / "uv.lock", "version = 1\n")
    _write(repository / ".gitignore", "__pycache__/\n*.py[cod]\nsrc/ea/ignored.py\n")
    target_launcher = repository / "scripts/reproducible_run.py"
    target_launcher.parent.mkdir()
    target_launcher.write_bytes(Path(launcher.__file__).read_bytes())
    _run(
        "git",
        "add",
        ".gitignore",
        "scripts/reproducible_run.py",
        "src/ea/__init__.py",
        "uv.lock",
        cwd=repository,
    )
    _run("git", "commit", "-qm", "test repository", cwd=repository)
    return repository


def test_launcher_source_has_only_standard_library_imports() -> None:
    path = Path(launcher.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_roots.add(node.module.partition(".")[0])

    assert "ea" not in imported_roots
    assert imported_roots <= set(sys.stdlib_module_names) | {"__future__"}


def test_bootstrap_orders_preflight_before_authorized_ea_import(tmp_path: Path) -> None:
    launcher_path = Path(launcher.__file__).resolve()
    code = f"""
import importlib.util
import json
import pathlib
import sys

path = pathlib.Path({str(launcher_path)!r})
spec = importlib.util.spec_from_file_location("isolated_launcher", path)
module = importlib.util.module_from_spec(spec)
sys.modules["isolated_launcher"] = module
spec.loader.exec_module(module)
events = []

class Session:
    commit = "a" * 40

class Composition:
    def _accept_launcher_preflight(self, grant):
        events.append("accept")
        repository, commit = grant.consume()
        events.append("grant.consume")
        assert repository == path.parent
        assert commit == "a" * 40
        return Session()

module._preflight = lambda repository, launcher: events.append("preflight") or ("a" * 40)
module._require_no_ea_modules = lambda: events.append("empty")
module._import_composition = lambda: events.append("import") or Composition()
session = module._bootstrap(path.parent, path)
assert session.commit == "a" * 40
assert not hasattr(sys, module._PREFLIGHT_SLOT)
print(json.dumps(events))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == [
        "preflight",
        "empty",
        "import",
        "accept",
        "grant.consume",
    ]


def test_bootstrap_local_authority_reaches_real_composition_once(tmp_path: Path) -> None:
    launcher_path = Path(launcher.__file__).resolve()
    repository = launcher_path.parent.parent
    code = f"""
import importlib.util
import pathlib
import sys

path = pathlib.Path({str(launcher_path)!r})
repository = pathlib.Path({str(repository)!r})
spec = importlib.util.spec_from_file_location("isolated_real_handoff", path)
module = importlib.util.module_from_spec(spec)
sys.modules["isolated_real_handoff"] = module
spec.loader.exec_module(module)
events = []
module._preflight = lambda actual_repository, launcher: (
    events.append("preflight") or {"a" * 40!r}
)
session = module._bootstrap(repository, path)
assert events == ["preflight"]
assert type(session).__name__ == "PreflightSession"
assert session.commit == {"a" * 40!r}
assert not hasattr(sys, module._PREFLIGHT_SLOT)
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_preflight_failure_never_imports_composition(tmp_path: Path) -> None:
    launcher_path = Path(launcher.__file__).resolve()
    code = f"""
import importlib.util
import json
import pathlib
import sys

path = pathlib.Path({str(launcher_path)!r})
spec = importlib.util.spec_from_file_location("isolated_launcher_failure", path)
module = importlib.util.module_from_spec(spec)
sys.modules["isolated_launcher_failure"] = module
spec.loader.exec_module(module)
events = []

def fail(repository, launcher):
    events.append("preflight")
    raise module.PreflightError("injected")

module._preflight = fail
module._import_composition = lambda: events.append("import")
try:
    module._bootstrap(path.parent, path)
except module.PreflightError:
    pass
else:
    raise AssertionError("preflight failure was not propagated")
print(json.dumps(events))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == ["preflight"]


def test_fresh_direct_import_cannot_self_register_a_structural_fake_grant(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    launcher_path = Path(launcher.__file__).resolve()
    code = f"""
import importlib.util
import pathlib
import sys
from ea.composition import run

class FakeGrant:
    consumed = False
    def consume(self):
        self.consumed = True
        return pathlib.Path({str(repository)!r}), {"a" * 40!r}

fake = FakeGrant()
setattr(sys, run._PREFLIGHT_SLOT, fake)
try:
    run._accept_launcher_preflight(fake)
except run.RunCompositionError as exc:
    assert "launcher" in str(exc)
else:
    raise AssertionError("structural fake grant was accepted")
assert not fake.consumed
assert not hasattr(sys, run._PREFLIGHT_SLOT)

path = pathlib.Path({str(launcher_path)!r})
spec = importlib.util.spec_from_file_location("direct_loaded_launcher", path)
module = importlib.util.module_from_spec(spec)
sys.modules["direct_loaded_launcher"] = module
spec.loader.exec_module(module)
for name in ("_GRANT_SEAL", "_PreflightGrant", "_publish_preflight_grant"):
    assert not hasattr(module, name), name
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        ("-I",),
        ("-I", "-B", "-O"),
        ("-I", "-B", "-S"),
        ("-I", "-B", "-X", "pycache_prefix=/tmp/ea-external-pycache"),
    ],
)
def test_launcher_rejects_unsupported_startup_before_repository_checks(
    arguments: tuple[str, ...],
) -> None:
    completed = subprocess.run(
        [sys.executable, *arguments, str(Path(launcher.__file__).resolve())],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "startup flags" in completed.stderr


def test_launcher_source_scan_rejects_ignored_shadow_symlink_and_empty_namespace(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    snapshot = launcher._repository_snapshot(repository)

    empty_namespace = repository / "src/ea/shadow"
    empty_namespace.mkdir()
    with pytest.raises(launcher.PreflightError, match="untracked import directory"):
        launcher._validate_source_tree(snapshot)
    empty_namespace.rmdir()

    ignored = repository / "src/ea/ignored.py"
    ignored.write_text("value = 1\n", encoding="utf-8")
    with pytest.raises(launcher.PreflightError, match="untracked source"):
        launcher._validate_source_tree(snapshot)
    ignored.unlink()

    target = repository / "outside.py"
    target.write_text("value = 1\n", encoding="utf-8")
    os.symlink(target, repository / "src/ea/linked.py")
    with pytest.raises(launcher.PreflightError, match="symlinks"):
        launcher._validate_source_tree(snapshot)


def test_manifest_submodule_import_does_not_load_outer_or_numpy_modules(
    tmp_path: Path,
) -> None:
    code = (
        "import sys; import ea.experiments.manifest; "
        "forbidden={'numpy','ea.experiments.provenance','ea.experiments.randomness',"
        "'ea.experiments.store','ea.experiments.binding'}; "
        "assert forbidden.isdisjoint(sys.modules), sorted(forbidden & set(sys.modules))"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
