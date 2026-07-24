from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import marshal
import os
import subprocess
import sys
import sysconfig
import types
from pathlib import Path
from typing import cast

import pytest
from pytest import MonkeyPatch

from ea.experiments import provenance as provenance_module
from ea.experiments.manifest import CodeEvidence, DistributionIdentity, RuntimeEvidence
from ea.experiments.provenance import (
    ProvenanceError,
    ProvenanceEvidence,
    RepositorySnapshot,
    _active_metadata_roots,
    _collect_runtime_evidence,
    _parse_direct_url,
    _repository_snapshot,
    _validate_loaded_ea_modules,
    _validate_source_tree,
    _validate_sys_path,
    collect_provenance,
    validate_source_cache,
    verify_provenance,
)


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
    _write(repository / ".gitignore", "__pycache__/\n*.py[cod]\n*.pyd\n*.egg-info/\n")
    _run("git", "add", ".gitignore", "src/ea/__init__.py", "uv.lock", cwd=repository)
    _run("git", "commit", "-qm", "test repository", cwd=repository)
    return repository


def _cache_bytes(code: types.CodeType, *, trailing: bytes = b"") -> bytes:
    return importlib.util.MAGIC_NUMBER + (b"\0" * 12) + marshal.dumps(code) + trailing


def test_source_cache_requires_exact_recompiled_nested_code(tmp_path: Path) -> None:
    source = b"def value():\n    return 1\n"
    filename = str((tmp_path / "value.py").resolve())
    code = compile(source, filename, "exec", flags=0, dont_inherit=True, optimize=0)

    validate_source_cache(
        pyc_bytes=_cache_bytes(code),
        source_bytes=source,
        resolved_filename=filename,
    )

    nested = next(item for item in code.co_consts if isinstance(item, types.CodeType))
    changed_nested = nested.replace(co_qualname="changed")
    changed = code.replace(
        co_consts=tuple(changed_nested if item is nested else item for item in code.co_consts)
    )
    assert code == changed  # Python equality omits this observable field.
    with pytest.raises(ProvenanceError, match="not exactly equivalent"):
        validate_source_cache(
            pyc_bytes=_cache_bytes(changed),
            source_bytes=source,
            resolved_filename=filename,
        )


def test_cache_comparison_ignores_newer_marshal_reference_identity_noise(
    tmp_path: Path,
) -> None:
    source = b"first = ('shared', 'shared')\nsecond = ('shared', 'shared')\n"
    filename = str((tmp_path / "shared.py").resolve())
    code = compile(source, filename, "exec", flags=0, dont_inherit=True, optimize=0)

    for marshal_version in (3, 4):
        payload = importlib.util.MAGIC_NUMBER + (b"\0" * 12) + marshal.dumps(code, marshal_version)
        validate_source_cache(
            pyc_bytes=payload,
            source_bytes=source,
            resolved_filename=filename,
        )


@pytest.mark.parametrize(
    "mutation",
    ["magic", "trailing", "non_code", "changed_source"],
)
def test_source_cache_rejects_malformed_or_mismatched_payload(
    tmp_path: Path,
    mutation: str,
) -> None:
    source = b"value = 1\n"
    filename = str((tmp_path / "value.py").resolve())
    code = compile(source, filename, "exec", flags=0, dont_inherit=True, optimize=0)
    pyc = _cache_bytes(code)
    compared_source = source
    if mutation == "magic":
        pyc = b"\0\0\0\0" + pyc[4:]
    elif mutation == "trailing":
        pyc += b"x"
    elif mutation == "non_code":
        pyc = importlib.util.MAGIC_NUMBER + (b"\0" * 12) + marshal.dumps("not code")
    elif mutation == "changed_source":
        compared_source = b"value = 2\n"

    with pytest.raises(ProvenanceError):
        validate_source_cache(
            pyc_bytes=pyc,
            source_bytes=compared_source,
            resolved_filename=filename,
        )


def test_repository_snapshot_rejects_dirty_staged_and_untracked_states(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    snapshot = _repository_snapshot(repository)
    assert snapshot.code.commit

    _write(repository / "src/ea/__init__.py", '__version__ = "changed"\n')
    with pytest.raises(ProvenanceError, match="worktree"):
        _repository_snapshot(repository)

    _run("git", "add", "src/ea/__init__.py", cwd=repository)
    with pytest.raises(ProvenanceError, match="worktree"):
        _repository_snapshot(repository)

    _run("git", "restore", "--staged", "src/ea/__init__.py", cwd=repository)
    _run("git", "restore", "src/ea/__init__.py", cwd=repository)
    _write(repository / "unexpected.txt", "untracked\n")
    with pytest.raises(ProvenanceError, match="worktree"):
        _repository_snapshot(repository)


def test_source_tree_allows_exact_source_cache_and_rejects_tamper(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    snapshot = _repository_snapshot(repository)
    source = repository / "src/ea/__init__.py"
    cache = Path(importlib.util.cache_from_source(str(source), optimization=""))
    cache.parent.mkdir()
    code = compile(
        source.read_bytes(),
        str(source.resolve()),
        "exec",
        flags=0,
        dont_inherit=True,
        optimize=0,
    )
    cache.write_bytes(_cache_bytes(code))

    _validate_source_tree(snapshot)

    cache.write_bytes(_cache_bytes(code.replace(co_filename="/tampered.py")))
    with pytest.raises(ProvenanceError, match="not exactly equivalent"):
        _validate_source_tree(snapshot)


def test_source_tree_rejects_ignored_shadow_and_symlink(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    shadow = repository / "src/shadow.pyc"
    shadow.write_bytes(b"ignored")
    snapshot = _repository_snapshot(repository)

    with pytest.raises(ProvenanceError, match="shadow"):
        _validate_source_tree(snapshot)

    shadow.unlink()
    target = repository / "outside.py"
    target.write_text("value = 1\n", encoding="utf-8")
    link = repository / "src/ea/linked.py"
    os.symlink(target, link)
    with pytest.raises(ProvenanceError, match="symlinks"):
        _validate_source_tree(snapshot)


def test_source_tree_rejects_empty_namespace_extension_and_sourceless_cache(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    snapshot = _repository_snapshot(repository)

    empty_namespace = repository / "src/ea/shadow"
    empty_namespace.mkdir()
    with pytest.raises(ProvenanceError, match="untracked import directory"):
        _validate_source_tree(snapshot)
    empty_namespace.rmdir()

    extension = repository / f"src/shadow{importlib.machinery.EXTENSION_SUFFIXES[0]}"
    extension.write_bytes(b"ignored")
    with pytest.raises(ProvenanceError, match="shadow"):
        _validate_source_tree(snapshot)
    extension.unlink()

    orphan = repository / "src/ea/orphan.pyc"
    orphan.write_bytes(b"ignored")
    with pytest.raises(ProvenanceError, match="misplaced"):
        _validate_source_tree(snapshot)


def test_isolated_no_bytecode_runtime_flags_pass_in_subprocess() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            (
                "from ea.experiments.provenance import _require_runtime_flags;"
                "_require_runtime_flags()"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_direct_url_rejects_duplicate_keys(tmp_path: Path) -> None:
    class FakeDistribution:
        def read_text(self, filename: str) -> str:
            assert filename == "direct_url.json"
            return '{"url":"file:///one","url":"file:///two","dir_info":{"editable":true}}'

    with pytest.raises(ProvenanceError, match="duplicate"):
        _parse_direct_url(
            cast(importlib.metadata.Distribution, FakeDistribution()),
            tmp_path,
        )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (
            '{"url":"file:///tmp","dir_info":{"editable":true},"unknown":0}',
            "closed object",
        ),
        (
            '{"url":"file:///tmp","dir_info":{"editable":false}}',
            "editable install",
        ),
        (
            '{"url":"https://example.invalid/repo","dir_info":{"editable":true}}',
            "local path",
        ),
        (
            '{"url":"file://host/tmp","dir_info":{"editable":true}}',
            "local path",
        ),
        (
            '{"url":"file:///tmp?query=1","dir_info":{"editable":true}}',
            "local path",
        ),
    ],
)
def test_direct_url_rejects_open_noneditable_or_nonlocal_shapes(
    tmp_path: Path,
    raw: str,
    message: str,
) -> None:
    class FakeDistribution:
        def read_text(self, filename: str) -> str:
            assert filename == "direct_url.json"
            return raw

    with pytest.raises(ProvenanceError, match=message):
        _parse_direct_url(
            cast(importlib.metadata.Distribution, FakeDistribution()),
            tmp_path,
        )


def test_direct_url_accepts_only_the_exact_repository(tmp_path: Path) -> None:
    class FakeDistribution:
        def read_text(self, filename: str) -> str:
            assert filename == "direct_url.json"
            return json.dumps(
                {
                    "url": tmp_path.as_uri(),
                    "dir_info": {"editable": True},
                }
            )

    _parse_direct_url(
        cast(importlib.metadata.Distribution, FakeDistribution()),
        tmp_path,
    )


def _runtime() -> RuntimeEvidence:
    return RuntimeEvidence(
        ea_version="0.1.1",
        python_implementation="cpython",
        python_version="3.12.13",
        python_cache_tag="cpython-312",
        sys_platform="darwin",
        platform_tag="macosx-11.0-arm64",
        distributions=(DistributionIdentity("ea-quant", "0.1.1"),),
        uv_lock_bytes=b"version = 1\n",
    )


def test_collect_provenance_rejects_head_change_during_collection(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)

    def change_head(
        snapshot: RepositorySnapshot,
        *,
        uv_lock_path: Path,
    ) -> RuntimeEvidence:
        assert snapshot.root == repository.resolve()
        assert uv_lock_path == repository / "uv.lock"
        _write(repository / "src/ea/__init__.py", '__version__ = "0.1.2"\n')
        _run("git", "add", "src/ea/__init__.py", cwd=repository)
        _run("git", "commit", "-qm", "change head", cwd=repository)
        return _runtime()

    monkeypatch.setattr(provenance_module, "_collect_runtime_evidence", change_head)

    with pytest.raises(ProvenanceError, match="repository changed"):
        collect_provenance(repository, repository / "uv.lock")


def test_terminal_provenance_rejects_different_recollected_evidence(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repository = _repository(tmp_path).resolve()
    expected = ProvenanceEvidence(
        repository=repository,
        code=CodeEvidence("a" * 40),
        runtime=_runtime(),
    )
    actual = ProvenanceEvidence(
        repository=repository,
        code=CodeEvidence("b" * 40),
        runtime=_runtime(),
    )
    monkeypatch.setattr(provenance_module, "collect_provenance", lambda *args: actual)

    with pytest.raises(ProvenanceError, match="terminal provenance differs"):
        verify_provenance(expected, repository, repository / "uv.lock")


def test_active_metadata_roots_deduplicate_purelib_and_platlib(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    root = tmp_path.resolve()
    monkeypatch.setattr(
        sysconfig,
        "get_paths",
        lambda: {"purelib": str(root), "platlib": str(root)},
    )

    assert _active_metadata_roots() == (root,)


def test_runtime_inventory_scopes_active_distributions_exactly_once(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repository = _repository(tmp_path).resolve()
    active_root = tmp_path / "site-packages"
    active_root.mkdir()
    snapshot = RepositorySnapshot(
        root=repository,
        code=CodeEvidence("a" * 40),
        tracked_ea_files=frozenset({"src/ea/__init__.py"}),
    )
    calls: list[tuple[str, ...] | None] = []

    class FakeDistribution:
        metadata = {"Name": "ea-quant"}
        version = "0.1.1"

        def locate_file(self, path: str) -> Path:
            assert path == ""
            return active_root

    distribution = cast(importlib.metadata.Distribution, FakeDistribution())

    def distributions(
        *,
        path: list[str] | None = None,
    ) -> tuple[importlib.metadata.Distribution, ...]:
        calls.append(tuple(path) if path is not None else None)
        return (distribution,)

    monkeypatch.setattr(provenance_module, "_require_runtime_flags", lambda: None)
    monkeypatch.setattr(provenance_module, "_validate_source_tree", lambda snapshot: None)
    monkeypatch.setattr(provenance_module, "_active_metadata_roots", lambda: (active_root,))
    monkeypatch.setattr(provenance_module, "_validate_sys_path", lambda **kwargs: None)
    monkeypatch.setattr(provenance_module, "_parse_direct_url", lambda *args: None)
    monkeypatch.setattr(provenance_module, "_validate_loaded_ea_modules", lambda root: None)
    monkeypatch.setattr(importlib.metadata, "distributions", distributions)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.1")

    evidence = _collect_runtime_evidence(snapshot, uv_lock_path=repository / "uv.lock")

    assert calls.count((str(active_root),)) == 1
    assert calls.count(None) == 1
    assert evidence.distributions == (DistributionIdentity("ea-quant", "0.1.1"),)


def test_sys_path_and_loaded_module_origins_fail_closed(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    active_root = Path(sysconfig.get_paths()["purelib"]).resolve()
    monkeypatch.setattr(
        sys,
        "path",
        [str(active_root), str(repository / "src"), "relative"],
    )
    with pytest.raises(ProvenanceError, match="empty or relative"):
        _validate_sys_path(repository=repository, active_roots=(active_root,))

    fake = types.ModuleType("ea.outside")
    outside = tmp_path / "outside.py"
    outside.write_text("value = 1\n", encoding="utf-8")
    fake.__file__ = str(outside)
    monkeypatch.setitem(sys.modules, "ea.outside", fake)
    with pytest.raises(ProvenanceError, match="escaped"):
        _validate_loaded_ea_modules(repository)
