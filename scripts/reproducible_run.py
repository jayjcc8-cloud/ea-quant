#!/usr/bin/env python3
"""Standard-library-only pre-import launcher for reproducible Phase 1 runs."""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.metadata
import importlib.util
import io
import json
import marshal
import os
import re
import subprocess
import sys
import sysconfig
import threading
import types
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z", flags=re.ASCII)
_PEP503_SEPARATOR_PATTERN = re.compile(r"[-_.]+")
_BYTECODE_SUFFIXES = tuple(importlib.machinery.BYTECODE_SUFFIXES)
_SOURCE_SUFFIXES = tuple(importlib.machinery.SOURCE_SUFFIXES)
_EXTENSION_SUFFIXES = tuple(importlib.machinery.EXTENSION_SUFFIXES)
_LAUNCHER_RELATIVE = Path("scripts/reproducible_run.py")
_PREFLIGHT_SLOT = "_ea_reproducible_preflight_grant_v1"
_MISSING = object()


class PreflightError(RuntimeError):
    """Raised before any ``ea`` module is authorized to execute."""


@dataclass(frozen=True, slots=True)
class _RepositorySnapshot:
    root: Path
    commit: str
    tracked_ea_files: frozenset[str]


class _AcceptedSession(Protocol):
    @property
    def commit(self) -> str: ...


class _CompositionModule(Protocol):
    def _accept_launcher_preflight(self, grant: object) -> _AcceptedSession: ...


def _git(repository: Path, *arguments: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreflightError("Git preflight failed") from exc
    if completed.returncode != 0:
        raise PreflightError("Git preflight returned a non-zero status")
    return completed.stdout


def _decode_single_path(value: bytes, *, field: str) -> Path:
    try:
        text = value.decode("utf-8").rstrip("\n")
    except UnicodeDecodeError as exc:
        raise PreflightError(f"{field} is not UTF-8") from exc
    if not text or "\n" in text or "\0" in text:
        raise PreflightError(f"{field} is not one canonical path")
    return Path(text)


def _repository_snapshot(repository: Path) -> _RepositorySnapshot:
    if not isinstance(repository, Path):
        raise PreflightError("repository must be a Path")
    try:
        if repository.is_symlink():
            raise PreflightError("repository final entry cannot be a symlink")
        requested = repository.resolve(strict=True)
    except OSError as exc:
        raise PreflightError("repository cannot be resolved") from exc
    if not requested.is_dir():
        raise PreflightError("repository must be a real directory")
    discovered = _decode_single_path(
        _git(requested, "rev-parse", "--show-toplevel"),
        field="Git repository root",
    )
    try:
        root = discovered.resolve(strict=True)
    except OSError as exc:
        raise PreflightError("Git repository root cannot be resolved") from exc
    if root != requested:
        raise PreflightError("launcher must use the exact Git worktree root")

    try:
        commit = _git(root, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").rstrip("\n")
    except UnicodeDecodeError as exc:
        raise PreflightError("Git commit is not ASCII") from exc
    if _COMMIT_PATTERN.fullmatch(commit) is None:
        raise PreflightError("Git HEAD is not one full lowercase commit")
    if _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise PreflightError("Git worktree has staged, unstaged, or untracked changes")

    tracked_raw = _git(root, "ls-files", "-z", "--", "src/ea")
    try:
        parts = tracked_raw.decode("utf-8").split("\0")
    except UnicodeDecodeError as exc:
        raise PreflightError("tracked source paths are not UTF-8") from exc
    if parts[-1:] != [""]:
        raise PreflightError("tracked source path stream is not NUL terminated")
    tracked = frozenset(part for part in parts[:-1] if part)
    if "src/ea/__init__.py" not in tracked:
        raise PreflightError("tracked EA package root is missing")
    if (
        _git(root, "ls-files", "--error-unmatch", "--", _LAUNCHER_RELATIVE.as_posix()).rstrip(b"\n")
        != _LAUNCHER_RELATIVE.as_posix().encode()
    ):
        raise PreflightError("outer launcher is not tracked at HEAD")
    return _RepositorySnapshot(root=root, commit=commit, tracked_ea_files=tracked)


def _validate_launcher(snapshot: _RepositorySnapshot, launcher: Path) -> None:
    expected = snapshot.root / _LAUNCHER_RELATIVE
    try:
        if launcher.is_symlink():
            raise PreflightError("outer launcher cannot be a symlink")
        actual = launcher.resolve(strict=True)
    except OSError as exc:
        raise PreflightError("outer launcher cannot be resolved") from exc
    if actual != expected or not actual.is_file():
        raise PreflightError("executed launcher is not the tracked repository launcher")


def _validate_source_cache(
    *,
    pyc_bytes: bytes,
    source_bytes: bytes,
    resolved_filename: str,
) -> None:
    if len(pyc_bytes) < 17 or pyc_bytes[:4] != importlib.util.MAGIC_NUMBER:
        raise PreflightError("source cache has invalid interpreter magic or header")
    stream = io.BytesIO(pyc_bytes[16:])
    try:
        loaded = marshal.load(stream)
    except (EOFError, ValueError, TypeError) as exc:
        raise PreflightError("source cache code object cannot be loaded") from exc
    if type(loaded) is not types.CodeType or stream.read() != b"":
        raise PreflightError("source cache must contain exactly one code object")
    try:
        recompiled = compile(
            source_bytes,
            resolved_filename,
            mode="exec",
            flags=0,
            dont_inherit=True,
            optimize=0,
        )
    except (SyntaxError, ValueError, TypeError) as exc:
        raise PreflightError("tracked source cannot be recompiled exactly") from exc
    if marshal.dumps(loaded, 2) != marshal.dumps(recompiled, 2):
        raise PreflightError("source cache is not exactly equivalent to tracked source")


def _walk_without_links(root: Path) -> tuple[Path, ...]:
    entries: list[Path] = []

    def visit(directory: Path) -> None:
        try:
            scanned = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise PreflightError("source tree cannot be scanned") from exc
        for entry in scanned:
            path = Path(entry.path)
            try:
                if entry.is_symlink():
                    raise PreflightError("source tree cannot contain symlinks")
                if entry.is_dir(follow_symlinks=False):
                    entries.append(path)
                    visit(path)
                elif entry.is_file(follow_symlinks=False):
                    entries.append(path)
                else:
                    raise PreflightError("source tree cannot contain special entries")
            except OSError as exc:
                raise PreflightError("source tree entry cannot be inspected") from exc

    visit(root)
    return tuple(entries)


def _is_import_candidate(path: Path) -> bool:
    return path.name == "__init__.py" or path.name.endswith(
        _SOURCE_SUFFIXES + _EXTENSION_SUFFIXES + _BYTECODE_SUFFIXES
    )


def _tracked_package_directories(tracked_files: frozenset[str]) -> frozenset[str]:
    directories = {"src/ea"}
    for tracked in tracked_files:
        path = Path(tracked)
        if path.parts[:2] != ("src", "ea") or len(path.parts) < 3:
            raise PreflightError("tracked EA path escaped the package root")
        for end in range(3, len(path.parts)):
            directories.add(Path(*path.parts[:end]).as_posix())
    return frozenset(directories)


def _validate_source_tree(snapshot: _RepositorySnapshot) -> None:
    source_root = snapshot.root / "src"
    package_root = source_root / "ea"
    metadata_root = source_root / "ea_quant.egg-info"
    try:
        resolved_source = source_root.resolve(strict=True)
        resolved_package = package_root.resolve(strict=True)
    except OSError as exc:
        raise PreflightError("repository source roots cannot be resolved") from exc
    if source_root.is_symlink() or package_root.is_symlink():
        raise PreflightError("repository source roots cannot be symlinks")
    if not resolved_source.is_dir() or not resolved_package.is_dir():
        raise PreflightError("repository source roots must be directories")

    tracked_directories = _tracked_package_directories(snapshot.tracked_ea_files)
    for entry in _walk_without_links(resolved_source):
        try:
            relative_source = entry.relative_to(resolved_source)
        except ValueError as exc:
            raise PreflightError("source entry escaped the source root") from exc
        if (
            len(relative_source.parts) == 1
            and entry.is_dir()
            and entry not in {resolved_package, metadata_root}
        ):
            raise PreflightError("unexpected top-level source package or namespace")
        relative_repo = entry.relative_to(snapshot.root).as_posix()
        if entry.is_dir() and entry.is_relative_to(resolved_package):
            parent_relative = entry.parent.relative_to(snapshot.root).as_posix()
            if relative_repo in tracked_directories or (
                entry.name == "__pycache__" and parent_relative in tracked_directories
            ):
                continue
            raise PreflightError("EA package contains an untracked import directory")
        if entry.is_file() and not entry.is_relative_to(resolved_package):
            if _is_import_candidate(entry):
                raise PreflightError("top-level import shadow is forbidden")
            continue
        if not entry.is_file():
            continue

        if entry.name.endswith(_BYTECODE_SUFFIXES):
            try:
                source = Path(importlib.util.source_from_cache(str(entry)))
                expected_cache = Path(
                    importlib.util.cache_from_source(str(source), optimization="")
                )
            except (NotImplementedError, ValueError) as exc:
                raise PreflightError("legacy or misplaced source bytecode is forbidden") from exc
            if entry != expected_cache:
                raise PreflightError("optimized or foreign-tag bytecode is forbidden")
            try:
                resolved_source_file = source.resolve(strict=True)
            except OSError as exc:
                raise PreflightError("source cache has no corresponding source") from exc
            if source.is_symlink() or not resolved_source_file.is_file():
                raise PreflightError("source cache source must be a regular file")
            source_relative = resolved_source_file.relative_to(snapshot.root).as_posix()
            if source_relative not in snapshot.tracked_ea_files:
                raise PreflightError("source cache corresponds to untracked source")
            try:
                _validate_source_cache(
                    pyc_bytes=entry.read_bytes(),
                    source_bytes=resolved_source_file.read_bytes(),
                    resolved_filename=str(resolved_source_file),
                )
            except OSError as exc:
                raise PreflightError("source cache or source cannot be read") from exc
        elif relative_repo not in snapshot.tracked_ea_files:
            raise PreflightError("EA package contains an untracked source or data file")


def _require_runtime_flags() -> None:
    flags = sys.flags
    if (
        flags.isolated != 1
        or flags.safe_path != 1
        or flags.no_user_site != 1
        or flags.no_site != 0
        or flags.dont_write_bytecode != 1
        or not sys.dont_write_bytecode
        or flags.optimize != 0
        or sys.pycache_prefix is not None
        or sys.prefix == sys.base_prefix
    ):
        raise PreflightError("interpreter startup flags do not satisfy reproducible v1")


def _require_no_ea_modules() -> None:
    if any(name == "ea" or name.startswith("ea.") for name in sys.modules):
        raise PreflightError("ea was imported before the source preflight completed")


def _active_metadata_roots() -> tuple[Path, ...]:
    paths = sysconfig.get_paths()
    roots: list[Path] = []
    for key in ("purelib", "platlib"):
        raw = paths.get(key)
        if type(raw) is not str or not raw or not Path(raw).is_absolute():
            raise PreflightError(f"sysconfig {key} is not an absolute path")
        path = Path(raw)
        if path.is_symlink():
            raise PreflightError(f"sysconfig {key} cannot be a symlink")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise PreflightError(f"sysconfig {key} cannot be resolved") from exc
        if not resolved.is_dir():
            raise PreflightError(f"sysconfig {key} must be a directory")
        if resolved not in roots:
            roots.append(resolved)
    if not roots:
        raise PreflightError("active metadata root set is empty")
    return tuple(roots)


def _distribution_identity(
    distribution: importlib.metadata.Distribution,
) -> tuple[str, str]:
    raw_name = distribution.metadata["Name"]
    version = distribution.version
    if type(raw_name) is not str or not raw_name or type(version) is not str or not version:
        raise PreflightError("installed distribution metadata is invalid")
    return _PEP503_SEPARATOR_PATTERN.sub("-", raw_name).lower(), version


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PreflightError("direct_url.json contains a duplicate key")
        result[key] = value
    return result


def _parse_direct_url(
    distribution: importlib.metadata.Distribution,
    repository: Path,
) -> None:
    raw = distribution.read_text("direct_url.json")
    if type(raw) is not str:
        raise PreflightError("ea-quant direct_url.json is missing")
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise PreflightError("ea-quant direct_url.json is invalid") from exc
    if type(value) is not dict or frozenset(value) != {"url", "dir_info"}:
        raise PreflightError("ea-quant direct_url.json must be a closed object")
    if value["dir_info"] != {"editable": True} or type(value["url"]) is not str:
        raise PreflightError("ea-quant direct URL must identify an editable install")
    try:
        parsed = urllib.parse.urlsplit(value["url"])
        decoded = urllib.parse.unquote_to_bytes(parsed.path).decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise PreflightError("ea-quant direct URL cannot be decoded") from exc
    if (
        parsed.scheme != "file"
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not decoded
        or not Path(decoded).is_absolute()
    ):
        raise PreflightError("ea-quant direct URL must be a local path without authority")
    try:
        target = Path(decoded).resolve(strict=True)
    except OSError as exc:
        raise PreflightError("ea-quant direct URL target cannot be resolved") from exc
    if target != repository:
        raise PreflightError("ea-quant editable install points at another repository")


def _validate_sys_path(repository: Path, active_roots: tuple[Path, ...]) -> None:
    try:
        base_prefix = Path(sys.base_prefix).resolve(strict=True)
        source_root = (repository / "src").resolve(strict=True)
    except OSError as exc:
        raise PreflightError("runtime import roots cannot be resolved") from exc
    seen: set[Path] = set()
    for raw in sys.path:
        if type(raw) is not str or not raw or not Path(raw).is_absolute():
            raise PreflightError("sys.path contains an empty or relative entry")
        resolved = Path(raw).resolve(strict=False)
        if resolved in seen:
            raise PreflightError("sys.path contains a duplicate physical entry")
        seen.add(resolved)
        if resolved in active_roots or resolved == source_root:
            continue
        if not resolved.is_relative_to(base_prefix):
            raise PreflightError("sys.path contains an injected import root")
        if {"site-packages", "dist-packages"} & {part.lower() for part in resolved.parts}:
            raise PreflightError("base interpreter site-packages are unsupported")


def _validate_editable_environment(
    snapshot: _RepositorySnapshot,
    active_roots: tuple[Path, ...],
) -> None:
    active = tuple(importlib.metadata.distributions(path=[str(root) for root in active_roots]))
    pairs: list[tuple[tuple[str, str], importlib.metadata.Distribution]] = []
    for distribution in active:
        try:
            location = Path(str(distribution.locate_file(""))).resolve(strict=True)
        except OSError as exc:
            raise PreflightError("active distribution root cannot be resolved") from exc
        if location not in active_roots:
            raise PreflightError("active distribution escaped active metadata roots")
        pairs.append((_distribution_identity(distribution), distribution))
    names = [identity[0] for identity, _ in pairs]
    if len(names) != len(set(names)):
        raise PreflightError("active distribution names are not unique")
    ea_pairs = [pair for pair in pairs if pair[0][0] == "ea-quant"]
    if len(ea_pairs) != 1:
        raise PreflightError("active environment must contain exactly one ea-quant")
    ea_identity, ea_distribution = ea_pairs[0]
    _parse_direct_url(ea_distribution, snapshot.root)
    try:
        if importlib.metadata.version("ea-quant") != ea_identity[1]:
            raise PreflightError("unscoped EA metadata version is inconsistent")
    except importlib.metadata.PackageNotFoundError as exc:
        raise PreflightError("unscoped ea-quant metadata is unavailable") from exc

    source_root = (snapshot.root / "src").resolve(strict=True)
    external: list[tuple[str, str]] = []
    for distribution in importlib.metadata.distributions():
        try:
            location = Path(str(distribution.locate_file(""))).resolve(strict=True)
        except OSError as exc:
            raise PreflightError("ambient distribution root cannot be resolved") from exc
        if location in active_roots:
            continue
        identity = _distribution_identity(distribution)
        if identity != ea_identity or location != source_root:
            raise PreflightError("ambient or path distribution is unsupported")
        external.append(identity)
    if len(external) > 1:
        raise PreflightError("multiple source-tree EA metadata projections are unsupported")

    expected_init = source_root / "ea" / "__init__.py"
    expected_package = source_root / "ea"
    spec = importlib.util.find_spec("ea")
    if (
        spec is None
        or spec.origin is None
        or Path(spec.origin).resolve(strict=True) != expected_init
        or spec.submodule_search_locations is None
        or tuple(Path(path).resolve(strict=True) for path in spec.submodule_search_locations)
        != (expected_package,)
    ):
        raise PreflightError("prospective ea import is not bound to the reviewed repository")
    _require_no_ea_modules()


def _preflight(repository: Path, launcher: Path) -> str:
    _require_runtime_flags()
    _require_no_ea_modules()
    before = _repository_snapshot(repository)
    _validate_launcher(before, launcher)
    _validate_source_tree(before)
    active_roots = _active_metadata_roots()
    _validate_sys_path(before.root, active_roots)
    _validate_editable_environment(before, active_roots)
    after = _repository_snapshot(before.root)
    if before != after:
        raise PreflightError("repository changed during the pre-import preflight")
    _require_no_ea_modules()
    return before.commit


def _import_composition() -> _CompositionModule:
    return cast(
        _CompositionModule,
        importlib.import_module("ea.composition.run"),
    )


def _bootstrap(
    repository: Path,
    launcher: Path,
) -> _AcceptedSession:
    commit = _preflight(repository, launcher)
    _require_no_ea_modules()

    # These authorities exist only in the successful post-preflight stack frame. Importing this
    # module exposes no reusable constructor, issuer seal, or publisher.
    grant_seal = object()

    class _PreflightGrant:
        __slots__ = ("_commit", "_consumed", "_lock", "_repository")
        _commit: str
        _consumed: bool
        _lock: threading.Lock
        _repository: Path

        def __init__(self, seal: object) -> None:
            if seal is not grant_seal:
                raise PreflightError("preflight grant issuer is invalid")
            object.__setattr__(self, "_repository", repository)
            object.__setattr__(self, "_commit", commit)
            object.__setattr__(self, "_consumed", False)
            object.__setattr__(self, "_lock", threading.Lock())

        def __setattr__(self, name: str, value: object) -> None:
            raise AttributeError("preflight grant is immutable")

        def consume(self) -> tuple[Path, str]:
            with self._lock:
                if self._consumed:
                    raise PreflightError("preflight grant was already consumed")
                object.__setattr__(self, "_consumed", True)
                return self._repository, self._commit

    grant = _PreflightGrant(grant_seal)
    if getattr(sys, _PREFLIGHT_SLOT, _MISSING) is not _MISSING:
        raise PreflightError("another preflight handoff is already pending")
    setattr(sys, _PREFLIGHT_SLOT, grant)
    try:
        composition = _import_composition()
        session = composition._accept_launcher_preflight(grant)
    finally:
        if getattr(sys, _PREFLIGHT_SLOT, _MISSING) is grant:
            delattr(sys, _PREFLIGHT_SLOT)
    if session.commit != commit:
        raise PreflightError("composition accepted a different Git commit")
    return session


def main() -> int:
    launcher = Path(__file__)
    if not launcher.is_absolute():
        launcher = Path.cwd() / launcher
    repository = launcher.parent.parent
    session = _bootstrap(repository, launcher)
    print(f"reproducible preparation gate verified commit {session.commit}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PreflightError as exc:
        print(f"reproducible preflight failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
