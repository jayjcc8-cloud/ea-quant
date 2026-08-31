"""Git, editable-source, dependency, and runtime evidence collection."""

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
import stat
import subprocess
import sys
import sysconfig
import types
import unicodedata
import urllib.parse
import zipfile
from base64 import urlsafe_b64decode
from contextlib import suppress
from csv import Error as CsvError
from csv import reader as csv_reader
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import ea
from ea.core.run import Sha256Digest
from ea.experiments.manifest import (
    CodeEvidence,
    DistributionIdentity,
    InstalledRuntimeSpecV2,
    RuntimeEvidence,
)

_PEP503_PATTERN = re.compile(r"[-_.]+")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z", flags=re.ASCII)
_BYTECODE_SUFFIXES = tuple(importlib.machinery.BYTECODE_SUFFIXES)
_SOURCE_SUFFIXES = tuple(importlib.machinery.SOURCE_SUFFIXES)
_EXTENSION_SUFFIXES = tuple(importlib.machinery.EXTENSION_SUFFIXES)
_INSTALLED_FILES_DOMAIN = b"ea.installed-local-wheel.v1\0"
_GENERATED_CONSOLE_SCRIPT_RECORD_PATH = "../../../bin/ea"


class ProvenanceError(RuntimeError):
    """Raised when execution provenance cannot be proved fail-closed."""


def _local_wheel_artifact(
    document: str, *, require_artifact: bool = True
) -> tuple[bytes | None, str]:
    """Extract and verify the only supported direct local wheel artifact."""
    try:
        value = json.loads(document, object_pairs_hook=_reject_duplicate_json_keys)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProvenanceError("direct URL metadata is invalid") from exc
    if type(value) is not dict or set(value) != {"url", "archive_info"}:
        raise ProvenanceError("direct URL metadata must be closed")
    url, raw_archive = value["url"], value["archive_info"]
    if type(url) is not str or type(raw_archive) is not dict:
        raise ProvenanceError("direct URL artifact metadata is invalid")
    archive: dict[str, object] = dict(raw_archive)
    if set(archive) not in (
        {"hash"},
        {"hashes"},
        {"hash", "hashes"},
    ):
        raise ProvenanceError("direct URL artifact metadata is invalid")
    has_hash = "hash" in archive
    has_hashes = "hashes" in archive
    raw_digest = archive.get("hash")
    if has_hash:
        if type(raw_digest) is not str or re.fullmatch(r"sha256=[0-9a-f]{64}", raw_digest) is None:
            raise ProvenanceError("direct URL artifact hash is invalid")
        digest = raw_digest
    else:
        digest = ""
    raw_hashes = archive.get("hashes")
    hashes_digest: str | None = None
    if has_hashes:
        if type(raw_hashes) is not dict:
            raise ProvenanceError("direct URL artifact hashes are invalid")
        hashes: dict[str, object] = dict(raw_hashes)
        raw_hashes_digest = hashes.get("sha256")
        if (
            set(hashes) != {"sha256"}
            or type(raw_hashes_digest) is not str
            or re.fullmatch(r"[0-9a-f]{64}", raw_hashes_digest) is None
        ):
            raise ProvenanceError("direct URL artifact hashes are invalid")
        hashes_digest = raw_hashes_digest
    if not has_hash:
        if hashes_digest is None:
            raise ProvenanceError("direct URL artifact hash is missing")
        digest = "sha256=" + hashes_digest
    elif hashes_digest is not None and digest != "sha256=" + hashes_digest:
        raise ProvenanceError("direct URL artifact hashes disagree")
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise ProvenanceError("direct URL must identify a local wheel") from exc
    if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
        raise ProvenanceError("direct URL must identify a local wheel")
    try:
        path = Path(urllib.parse.unquote_to_bytes(parsed.path).decode("utf-8"))
        if (
            not path.is_absolute()
            or path.suffix != ".whl"
            or any(ord(char) <= 0x20 or char == "\\" for char in str(path))
        ):
            raise ProvenanceError("direct URL must identify an absolute canonical wheel path")
        resolved = path.resolve(strict=True)
    except (OSError, UnicodeError) as exc:
        if not require_artifact:
            return None, digest[7:]
        raise ProvenanceError("local wheel artifact is unavailable") from exc
    try:
        if not resolved.is_file() or resolved.suffix != ".whl":
            raise ProvenanceError("local wheel artifact hash mismatches")
        snapshot = resolved.read_bytes()
    except OSError as exc:
        raise ProvenanceError("local wheel artifact is unavailable") from exc
    actual = sha256(snapshot).hexdigest()
    if actual != digest[7:]:
        raise ProvenanceError("local wheel artifact hash mismatches")
    return snapshot, actual


def _require_pip_installer(raw: object) -> None:
    """Admit only the pip INSTALLER marker; it is not identity material."""
    if type(raw) is not str or re.fullmatch(r"pip[ \t\r\n\f\v]*", raw) is None:
        raise ProvenanceError("installed runtime requires pip INSTALLER")


def _wheel_owned_rows(wheel: bytes) -> tuple[tuple[str, bytes], ...]:
    """Read a closed wheel-owned surface only after strict RECORD validation."""
    try:
        with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
            names = archive.namelist()
            if any(
                name != unicodedata.normalize("NFC", name)
                or name.startswith("/")
                or "\\" in name
                or any(part in {"", ".", ".."} for part in name.split("/"))
                for name in names
            ):
                raise ProvenanceError("wheel member path is not canonical")
            roots = [name.rsplit("/", 1)[0] for name in names if name.endswith(".dist-info/RECORD")]
            if len(names) != len(set(names)) or len(roots) != 1:
                raise ProvenanceError("wheel members or RECORD are not closed")
            root = roots[0]
            record_name = root + "/RECORD"
            records: dict[str, tuple[str, int]] = {}
            for parts in csv_reader(archive.read(record_name).decode().splitlines()):
                if len(parts) != 3 or parts[0] in records:
                    raise ProvenanceError("wheel RECORD is malformed")
                if parts[0] == record_name:
                    if parts[1:] != ["", ""]:
                        raise ProvenanceError("wheel RECORD self row is malformed")
                    continue
                if (
                    not re.fullmatch(r"sha256=[A-Za-z0-9_-]{43}", parts[1])
                    or not parts[2].isdigit()
                ):
                    raise ProvenanceError("wheel RECORD must hash owned rows")
                records[parts[0]] = (parts[1][7:], int(parts[2]))
            selected = [
                name
                for name in names
                if name.startswith("ea/") and name.endswith(".py") and not name.endswith(".pyc")
            ]
            selected.extend(
                root + "/" + name
                for name in ("METADATA", "WHEEL", "entry_points.txt", "top_level.txt")
            )
            if not selected or any(name not in records for name in selected):
                raise ProvenanceError("wheel RECORD omits closed owned rows")
            rows = []
            for name in sorted(selected):
                content = archive.read(name)
                encoded, size = records[name]
                if len(content) != size or sha256(content).digest() != urlsafe_b64decode(
                    encoded + "=" * (-len(encoded) % 4)
                ):
                    raise ProvenanceError("wheel RECORD row mismatch")
                rows.append((name, content))
            return tuple(rows)
    except (CsvError, KeyError, OSError, UnicodeError, ValueError, zipfile.BadZipFile) as exc:
        raise ProvenanceError("wheel cannot be read") from exc


def _installed_wheel_rows(
    distribution: importlib.metadata.Distribution,
    expected: tuple[tuple[str, bytes], ...] | None,
) -> tuple[tuple[str, bytes], ...]:
    """Select the closed wheel-owned installed surface without trusting RECORD ownership."""
    rows = dict(_installed_file_rows(distribution))
    try:
        root = Path(str(distribution.locate_file(""))).resolve(strict=True)
        package = (root / "ea").resolve(strict=True)
    except OSError as exc:
        raise ProvenanceError("installed EA package cannot be resolved") from exc
    if package.is_symlink() or not package.is_dir() or not package.is_relative_to(root):
        raise ProvenanceError("installed EA package is unsafe")
    sources = {
        name: value
        for name, value in rows.items()
        if name.startswith("ea/") and name.endswith(".py")
    }
    if not sources:
        raise ProvenanceError("installed EA package has no source rows")
    cache_rows = {
        name: value
        for name, value in rows.items()
        if name.startswith("ea/") and name.endswith(".pyc")
    }
    if any(
        name.startswith("ea/") and name not in sources and name not in cache_rows for name in rows
    ):
        raise ProvenanceError("installed EA package has an unsupported RECORD row")
    try:
        tree_rows = {
            item.relative_to(root).as_posix()
            for item in _walk_without_links(package)
            if item.is_file()
        }
    except OSError as exc:
        raise ProvenanceError("installed EA package cannot be walked safely") from exc
    if tree_rows != set(sources) | set(cache_rows):
        raise ProvenanceError("installed EA package tree is not closed")
    for name, content in cache_rows.items():
        try:
            source = Path(importlib.util.source_from_cache(str(root / name))).relative_to(root)
            source_name = source.as_posix()
            expected_cache = importlib.util.cache_from_source(str(root / source), optimization="")
        except (NotImplementedError, ValueError) as exc:
            raise ProvenanceError("installed source cache is unsupported") from exc
        if Path(root / name) != Path(expected_cache) or source_name not in sources:
            raise ProvenanceError("installed source cache is misplaced")
        validate_source_cache(
            pyc_bytes=content,
            source_bytes=sources[source_name],
            resolved_filename=str(root / source),
        )
    metadata_roots = {
        name.rsplit("/", 1)[0] for name in rows if name.endswith(".dist-info/METADATA")
    }
    required = ("METADATA", "WHEEL", "entry_points.txt", "top_level.txt")
    valid_roots = [
        candidate
        for candidate in metadata_roots
        if all(candidate + "/" + leaf in rows for leaf in required)
    ]
    if len(valid_roots) != 1:
        raise ProvenanceError("installed wheel metadata is not closed")
    selected = tuple(
        sorted(
            (
                *sources.items(),
                *(
                    (valid_roots[0] + "/" + leaf, rows[valid_roots[0] + "/" + leaf])
                    for leaf in required
                ),
            )
        )
    )
    if expected is not None:
        expected_sources = {name: content for name, content in expected if name.startswith("ea/")}
        expected_metadata = {
            name.rsplit("/", 1)[1]: content for name, content in expected if ".dist-info/" in name
        }
        actual_metadata = {
            name.rsplit("/", 1)[1]: content for name, content in selected if ".dist-info/" in name
        }
        if sources != expected_sources or actual_metadata != expected_metadata:
            raise ProvenanceError("installed rows differ from the local wheel")
    return selected


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """One clean, immutable observation of the editable Git repository."""

    root: Path
    code: CodeEvidence
    tracked_ea_files: frozenset[str]


@dataclass(frozen=True, slots=True)
class ProvenanceEvidence:
    """Operational repository location plus hash-bearing code/runtime evidence."""

    repository: Path
    code: CodeEvidence
    runtime: RuntimeEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.repository, Path) or not self.repository.is_absolute():
            raise ProvenanceError("repository must be an absolute Path")
        if type(self.code) is not CodeEvidence:
            raise ProvenanceError("code must be CodeEvidence")
        if type(self.runtime) is not RuntimeEvidence:
            raise ProvenanceError("runtime must be RuntimeEvidence")


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
        raise ProvenanceError("Git evidence collection failed") from exc
    if completed.returncode != 0:
        raise ProvenanceError("Git evidence collection returned a non-zero status")
    return completed.stdout


def _decode_single_path(value: bytes, *, field: str) -> Path:
    try:
        text = value.decode("utf-8").rstrip("\n")
    except UnicodeDecodeError as exc:
        raise ProvenanceError(f"{field} is not UTF-8") from exc
    if not text or "\n" in text or "\0" in text:
        raise ProvenanceError(f"{field} is not one canonical path")
    return Path(text)


def _repository_snapshot(repository: Path) -> RepositorySnapshot:
    if not isinstance(repository, Path):
        raise ProvenanceError("repository must be a Path")
    try:
        requested = repository.resolve(strict=True)
    except OSError as exc:
        raise ProvenanceError("repository cannot be resolved") from exc
    if repository.is_symlink() or not requested.is_dir():
        raise ProvenanceError("repository must be a real directory")
    discovered = _decode_single_path(
        _git(requested, "rev-parse", "--show-toplevel"),
        field="Git repository root",
    )
    try:
        root = discovered.resolve(strict=True)
    except OSError as exc:
        raise ProvenanceError("Git repository root cannot be resolved") from exc
    if root != requested:
        raise ProvenanceError("repository must be the exact Git worktree root")

    raw_commit = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    try:
        commit = raw_commit.decode("ascii").rstrip("\n")
    except UnicodeDecodeError as exc:
        raise ProvenanceError("Git commit is not ASCII") from exc
    if _COMMIT_PATTERN.fullmatch(commit) is None:
        raise ProvenanceError("Git HEAD is not one full lowercase commit")
    if _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise ProvenanceError("Git worktree has staged, unstaged, or untracked changes")

    tracked_raw = _git(root, "ls-files", "-z", "--", "src/ea")
    try:
        tracked_parts = tracked_raw.decode("utf-8").split("\0")
    except UnicodeDecodeError as exc:
        raise ProvenanceError("tracked source paths are not UTF-8") from exc
    if tracked_parts[-1:] != [""]:
        raise ProvenanceError("tracked source path stream is not NUL terminated")
    tracked = frozenset(part for part in tracked_parts[:-1] if part)
    if "src/ea/__init__.py" not in tracked:
        raise ProvenanceError("tracked EA package root is missing")
    return RepositorySnapshot(
        root=root,
        code=CodeEvidence(commit),
        tracked_ea_files=tracked,
    )


def validate_source_cache(
    *,
    pyc_bytes: bytes,
    source_bytes: bytes,
    resolved_filename: str,
    expected_magic: bytes = importlib.util.MAGIC_NUMBER,
) -> None:
    """Prove a source-backed cache is exactly equivalent under ADR 0006."""
    if (
        type(pyc_bytes) is not bytes
        or type(source_bytes) is not bytes
        or type(resolved_filename) is not str
        or type(expected_magic) is not bytes
    ):
        raise ProvenanceError("cache validation inputs have invalid types")
    if len(pyc_bytes) < 17 or pyc_bytes[:4] != expected_magic:
        raise ProvenanceError("source cache has invalid interpreter magic or header")
    stream = io.BytesIO(pyc_bytes[16:])
    try:
        loaded = marshal.load(stream)
    except (EOFError, ValueError, TypeError) as exc:
        raise ProvenanceError("source cache code object cannot be loaded") from exc
    if type(loaded) is not types.CodeType or stream.read() != b"":
        raise ProvenanceError("source cache must contain exactly one code object")
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
        raise ProvenanceError("tracked source cannot be recompiled exactly") from exc
    if marshal.dumps(loaded, 2) != marshal.dumps(recompiled, 2):
        raise ProvenanceError("source cache is not exactly equivalent to tracked source")


def _walk_without_links(root: Path) -> tuple[Path, ...]:
    entries: list[Path] = []

    def visit(directory: Path) -> None:
        try:
            scanned = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise ProvenanceError("source tree cannot be scanned") from exc
        for entry in scanned:
            path = Path(entry.path)
            try:
                if entry.is_symlink():
                    raise ProvenanceError("source tree cannot contain symlinks")
                if entry.is_dir(follow_symlinks=False):
                    entries.append(path)
                    visit(path)
                elif entry.is_file(follow_symlinks=False):
                    entries.append(path)
                else:
                    raise ProvenanceError("source tree cannot contain special entries")
            except OSError as exc:
                raise ProvenanceError("source tree entry cannot be inspected") from exc

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
            raise ProvenanceError("tracked EA path escaped the package root")
        for end in range(3, len(path.parts)):
            directories.add(Path(*path.parts[:end]).as_posix())
    return frozenset(directories)


def _validate_source_tree(snapshot: RepositorySnapshot) -> None:
    source_root = snapshot.root / "src"
    package_root = source_root / "ea"
    metadata_root = source_root / "ea_quant.egg-info"
    try:
        resolved_source = source_root.resolve(strict=True)
        resolved_package = package_root.resolve(strict=True)
    except OSError as exc:
        raise ProvenanceError("repository source roots cannot be resolved") from exc
    if source_root.is_symlink() or package_root.is_symlink():
        raise ProvenanceError("repository source roots cannot be symlinks")
    if not resolved_source.is_dir() or not resolved_package.is_dir():
        raise ProvenanceError("repository source roots must be directories")

    tracked_directories = _tracked_package_directories(snapshot.tracked_ea_files)
    entries = _walk_without_links(resolved_source)
    for entry in entries:
        try:
            relative_source = entry.relative_to(resolved_source)
        except ValueError as exc:
            raise ProvenanceError("source entry escaped the source root") from exc
        if (
            len(relative_source.parts) == 1
            and entry.is_dir()
            and entry not in {resolved_package, metadata_root}
        ):
            raise ProvenanceError("unexpected top-level source package or namespace")
        relative_repo = entry.relative_to(snapshot.root).as_posix()
        if entry.is_dir() and entry.is_relative_to(resolved_package):
            parent_relative = entry.parent.relative_to(snapshot.root).as_posix()
            if relative_repo in tracked_directories or (
                entry.name == "__pycache__" and parent_relative in tracked_directories
            ):
                continue
            raise ProvenanceError("EA package contains an untracked import directory")
        if entry.is_file() and not entry.is_relative_to(resolved_package):
            if _is_import_candidate(entry):
                raise ProvenanceError("top-level import shadow is forbidden")
            continue
        if not entry.is_file():
            continue

        if entry.name.endswith(_BYTECODE_SUFFIXES):
            try:
                source_text = importlib.util.source_from_cache(str(entry))
            except ValueError as exc:
                raise ProvenanceError("legacy or misplaced source bytecode is forbidden") from exc
            source = Path(source_text)
            try:
                expected_cache = Path(
                    importlib.util.cache_from_source(str(source), optimization="")
                )
            except (NotImplementedError, ValueError) as exc:
                raise ProvenanceError("source cache path is unsupported") from exc
            if entry != expected_cache:
                raise ProvenanceError("optimized or foreign-tag bytecode is forbidden")
            try:
                resolved_source_file = source.resolve(strict=True)
            except OSError as exc:
                raise ProvenanceError("source cache has no corresponding source") from exc
            if source.is_symlink() or not resolved_source_file.is_file():
                raise ProvenanceError("source cache source must be a regular file")
            source_relative = resolved_source_file.relative_to(snapshot.root).as_posix()
            if source_relative not in snapshot.tracked_ea_files:
                raise ProvenanceError("source cache corresponds to untracked source")
            try:
                validate_source_cache(
                    pyc_bytes=entry.read_bytes(),
                    source_bytes=resolved_source_file.read_bytes(),
                    resolved_filename=str(resolved_source_file),
                )
            except OSError as exc:
                raise ProvenanceError("source cache or source cannot be read") from exc
        elif relative_repo not in snapshot.tracked_ea_files:
            raise ProvenanceError("EA package contains an untracked source or data file")


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
        raise ProvenanceError("interpreter startup flags do not satisfy reproducible v1")


def _active_metadata_roots() -> tuple[Path, ...]:
    paths = sysconfig.get_paths()
    roots: list[Path] = []
    for key in ("purelib", "platlib"):
        raw = paths.get(key)
        if type(raw) is not str or not raw or not Path(raw).is_absolute():
            raise ProvenanceError(f"sysconfig {key} is not an absolute path")
        path = Path(raw)
        if path.is_symlink():
            raise ProvenanceError(f"sysconfig {key} cannot be a symlink")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ProvenanceError(f"sysconfig {key} cannot be resolved") from exc
        if not resolved.is_dir():
            raise ProvenanceError(f"sysconfig {key} must be a directory")
        if resolved not in roots:
            roots.append(resolved)
    if not roots:
        raise ProvenanceError("active metadata root set is empty")
    return tuple(roots)


def _normalize_distribution_name(value: object) -> str:
    if type(value) is not str or not value:
        raise ProvenanceError("distribution is missing its metadata Name")
    return _PEP503_PATTERN.sub("-", value).lower()


def _distribution_identity(
    distribution: importlib.metadata.Distribution,
) -> DistributionIdentity:
    try:
        name = _normalize_distribution_name(distribution.metadata["Name"])
        version = distribution.version
        return DistributionIdentity(name=name, version=version)
    except (KeyError, TypeError, ValueError) as exc:
        raise ProvenanceError("installed distribution metadata is invalid") from exc


def _validate_installed_direct_url(distribution: importlib.metadata.Distribution) -> None:
    """Accept only absent metadata or one strict local wheel origin."""
    raw = distribution.read_text("direct_url.json")
    if raw is None:
        return
    if type(raw) is not str:
        raise ProvenanceError("ea-quant direct_url.json is invalid")
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ProvenanceError("ea-quant direct_url.json is invalid") from exc
    if type(value) is not dict or set(value) != {"url", "archive_info"}:
        raise ProvenanceError("ea-quant direct_url.json must be a closed object")
    url, archive = value["url"], value["archive_info"]
    if type(url) is not str or type(archive) is not dict or set(archive) - {"hash", "hashes"}:
        raise ProvenanceError("ea-quant direct URL is invalid")
    for char in url:
        if ord(char) <= 0x20 or ord(char) == 0x7F or ord(char) > 0x7F or char == "\\":
            raise ProvenanceError("ea-quant direct URL contains a forbidden raw character")
    for index, char in enumerate(url):
        if char == "%" and (
            index + 2 >= len(url) or not re.fullmatch(r"[0-9A-Fa-f]{2}", url[index + 1 : index + 3])
        ):
            raise ProvenanceError("ea-quant direct URL has an invalid percent escape")
    if not url.startswith("file:"):
        raise ProvenanceError("ea-quant direct URL scheme must be lowercase file")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
        raise ProvenanceError("ea-quant direct URL must be an authority-free file URL")
    try:
        path = urllib.parse.unquote_to_bytes(parsed.path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProvenanceError("ea-quant direct URL path is not UTF-8") from exc
    if (
        not path.startswith("/")
        or not path.endswith(".whl")
        or any(ord(c) <= 0x20 or ord(c) == 0x7F or c == "\\" for c in path)
    ):
        raise ProvenanceError("ea-quant direct URL must be an absolute wheel path")

    def valid_hash(value: object) -> bool:
        return type(value) is str and re.fullmatch(r"sha256=[0-9a-f]{64}", value) is not None

    if "hash" in archive and not valid_hash(archive["hash"]):
        raise ProvenanceError("ea-quant wheel hash is invalid")
    if "hashes" in archive:
        hashes = archive["hashes"]
        if (
            type(hashes) is not dict
            or set(hashes) != {"sha256"}
            or type(hashes["sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", hashes["sha256"]) is None
        ):
            raise ProvenanceError("ea-quant wheel hashes are invalid")
        if "hash" in archive and archive["hash"] != "sha256=" + hashes["sha256"]:
            raise ProvenanceError("ea-quant wheel hashes disagree")


def _installed_file_rows(
    distribution: importlib.metadata.Distribution,
) -> tuple[tuple[str, bytes], ...]:
    files = distribution.files
    if files is None:
        raise ProvenanceError("installed distribution has no RECORD files")
    root_fd: int | None = None
    rows: list[tuple[str, bytes]] = []
    try:
        root_path = Path(str(distribution.locate_file("")))
        root = root_path.resolve(strict=True)
        if root_path.is_symlink() or not root.is_dir():
            raise ProvenanceError("installed distribution root is not a real directory")
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        root_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise ProvenanceError("installed distribution root is not a directory")
        for record in files:
            raw = str(record)
            if raw == _GENERATED_CONSOLE_SCRIPT_RECORD_PATH:
                continue
            parts = raw.split("/")
            if (
                raw != unicodedata.normalize("NFC", raw)
                or "\\" in raw
                or raw.startswith("/")
                or any(part in {"", ".", ".."} for part in parts)
            ):
                raise ProvenanceError("installed RECORD path is not canonical POSIX")
            target = Path(str(distribution.locate_file(record)))
            if target != root.joinpath(*parts):
                raise ProvenanceError("installed RECORD path escaped its distribution root")
            parent_fd = root_fd
            opened: list[tuple[int, str, os.stat_result, int]] = []
            file_fd: int | None = None
            try:
                for part in parts[:-1]:
                    named = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
                    child_fd = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=parent_fd,
                    )
                    child_stat = os.fstat(child_fd)
                    if (
                        not stat.S_ISDIR(named.st_mode)
                        or not stat.S_ISDIR(child_stat.st_mode)
                        or (named.st_dev, named.st_ino) != (child_stat.st_dev, child_stat.st_ino)
                    ):
                        os.close(child_fd)
                        raise ProvenanceError("installed RECORD path contains an unsafe component")
                    opened.append((parent_fd, part, child_stat, child_fd))
                    parent_fd = child_fd
                name = parts[-1]
                named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                file_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_fd,
                )
                file_stat = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(named.st_mode)
                    or not stat.S_ISREG(file_stat.st_mode)
                    or (named.st_dev, named.st_ino) != (file_stat.st_dev, file_stat.st_ino)
                ):
                    raise ProvenanceError("installed RECORD file is not contained regular data")
                chunks: list[bytes] = []
                while chunk := os.read(file_fd, 65536):
                    chunks.append(chunk)
                after = os.fstat(file_fd)
                if (after.st_dev, after.st_ino, after.st_size) != (
                    file_stat.st_dev,
                    file_stat.st_ino,
                    file_stat.st_size,
                ):
                    raise ProvenanceError("installed RECORD file changed while being read")
                for directory_fd, component, component_stat, _ in opened:
                    current = os.stat(component, dir_fd=directory_fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (
                        component_stat.st_dev,
                        component_stat.st_ino,
                    ):
                        raise ProvenanceError("installed RECORD parent changed while being read")
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (file_stat.st_dev, file_stat.st_ino):
                    raise ProvenanceError("installed RECORD file changed while being read")
                rows.append((raw, b"".join(chunks)))
            finally:
                if file_fd is not None:
                    with suppress(OSError):
                        os.close(file_fd)
                for _, _, _, child_fd in reversed(opened):
                    with suppress(OSError):
                        os.close(child_fd)
    except OSError as exc:
        raise ProvenanceError("installed distribution file cannot be resolved") from exc
    finally:
        if root_fd is not None:
            with suppress(OSError):
                os.close(root_fd)
    if not rows or len({name for name, _ in rows}) != len(rows):
        raise ProvenanceError("installed RECORD files are empty or duplicate")
    return tuple(sorted(rows))


def _validate_installed_sys_path(active_roots: tuple[Path, ...]) -> None:
    paths = sysconfig.get_paths()
    roots: list[Path] = []
    for key in ("stdlib", "platstdlib"):
        raw = paths.get(key)
        if type(raw) is not str or not raw or not Path(raw).is_absolute():
            raise ProvenanceError(f"sysconfig {key} is not an absolute path")
        path = Path(raw)
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ProvenanceError(f"sysconfig {key} cannot be resolved") from exc
        if path.is_symlink() or not resolved.is_dir():
            raise ProvenanceError(f"sysconfig {key} is unsafe")
        if resolved not in roots:
            roots.append(resolved)
    versioned_name = f"python{sys.version_info.major}{sys.version_info.minor}.zip"
    versioned_zip = (roots[0].parent / versioned_name).resolve(strict=False)
    seen: set[Path] = set()
    for raw in sys.path:
        if type(raw) is not str or not raw or not Path(raw).is_absolute():
            raise ProvenanceError("sys.path contains an empty or relative entry")
        path = Path(raw)
        try:
            resolved = path.resolve(strict=path.resolve(strict=False) != versioned_zip)
        except OSError as exc:
            raise ProvenanceError("sys.path entry cannot be resolved") from exc
        if path.is_symlink() or resolved in seen:
            raise ProvenanceError("sys.path contains an unsafe duplicate physical entry")
        seen.add(resolved)
        if (
            resolved in active_roots
            or resolved == versioned_zip
            or any(resolved.is_relative_to(root) for root in roots)
        ):
            continue
        raise ProvenanceError("sys.path contains an injected import root")


def _validate_installed_ea_import(
    distribution: importlib.metadata.Distribution,
    identity: DistributionIdentity,
    installed_rows: tuple[tuple[str, bytes], ...],
) -> None:
    try:
        root_path = Path(str(distribution.locate_file("")))
        root = root_path.resolve(strict=True)
        package = root / "ea"
        expected_init = package / "__init__.py"
    except OSError as exc:
        raise ProvenanceError("installed EA package cannot be resolved") from exc
    if (
        not root_path.is_absolute()
        or root_path.is_symlink()
        or root_path != root
        or not root.is_dir()
        or package.is_symlink()
        or not package.is_dir()
        or package.resolve(strict=True) != package
        or expected_init.is_symlink()
        or not expected_init.is_file()
        or expected_init.resolve(strict=True) != expected_init
    ):
        raise ProvenanceError("selected installed EA package is unsafe")
    spec = importlib.util.find_spec("ea")
    locations = (
        ()
        if spec is None or spec.submodule_search_locations is None
        else tuple(spec.submodule_search_locations)
    )
    if (
        spec is None
        or type(spec.origin) is not str
        or Path(spec.origin).resolve(strict=True) != expected_init
        or len(locations) != 1
        or type(locations[0]) is not str
        or Path(locations[0]).resolve(strict=True) != package
        or type(ea.__file__) is not str
        or Path(ea.__file__).resolve(strict=True) != expected_init
    ):
        raise ProvenanceError("executing EA package is not bound to the selected distribution")
    try:
        version = importlib.metadata.version("ea-quant")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ProvenanceError("unscoped ea-quant metadata is unavailable") from exc
    if version != identity.version or ea.__version__ != identity.version:
        raise ProvenanceError("EA runtime and metadata versions are inconsistent")
    source_rows = {
        name for name, _ in installed_rows if name.startswith("ea/") and name.endswith(".py")
    }
    for name, module in tuple(sys.modules.items()):
        if name != "ea" and not name.startswith("ea."):
            continue
        origin = getattr(module, "__file__", None)
        if type(origin) is not str:
            raise ProvenanceError("loaded EA module has no regular installed origin")
        try:
            path = Path(origin)
            resolved = path.resolve(strict=True)
            relative = resolved.relative_to(root).as_posix()
        except (OSError, ValueError) as exc:
            raise ProvenanceError("loaded EA module escaped the selected package") from exc
        if path.is_symlink() or not resolved.is_file() or relative not in source_rows:
            raise ProvenanceError("loaded EA module is not an owned installed source")


def collect_installed_runtime_spec_v2(*, require_artifact: bool = True) -> InstalledRuntimeSpecV2:
    """Collect the checkout-independent installed-distribution runtime identity."""
    _require_runtime_flags()
    roots = _active_metadata_roots()
    _validate_installed_sys_path(roots)
    distributions = tuple(importlib.metadata.distributions(path=[str(root) for root in roots]))
    pairs = []
    for item in distributions:
        try:
            root = Path(str(item.locate_file(""))).resolve(strict=True)
        except OSError as exc:
            raise ProvenanceError("active distribution root cannot be resolved") from exc
        if root not in roots:
            raise ProvenanceError("active distribution escaped active metadata roots")
        pairs.append((_distribution_identity(item), item))
    if len({identity.name for identity, _ in pairs}) != len(pairs):
        raise ProvenanceError("active distribution names are not unique")
    pairs.sort(key=lambda pair: pair[0].name)
    ea_pairs = [pair for pair in pairs if pair[0].name == "ea-quant"]
    if len(ea_pairs) != 1:
        raise ProvenanceError("installed runtime requires exactly one ea-quant distribution")
    identity, distribution = ea_pairs[0]
    if type(require_artifact) is not bool:
        raise ProvenanceError("artifact requirement must be an exact bool")
    _require_pip_installer(distribution.read_text("INSTALLER"))
    raw_direct_url = distribution.read_text("direct_url.json")
    if type(raw_direct_url) is not str:
        raise ProvenanceError("ea-quant direct_url.json is missing")
    wheel_bytes, artifact_sha256 = _local_wheel_artifact(
        raw_direct_url, require_artifact=require_artifact
    )
    owned_rows = _wheel_owned_rows(wheel_bytes) if wheel_bytes is not None else None
    installed_rows = _installed_wheel_rows(distribution, owned_rows)
    _validate_installed_ea_import(distribution, identity, installed_rows)
    digest = sha256(_INSTALLED_FILES_DOMAIN)
    digest.update(artifact_sha256.encode() + b"\0")
    for name, content in installed_rows:
        digest.update(name.encode() + b"\0" + len(content).to_bytes(8, "big") + content)
    return InstalledRuntimeSpecV2(
        ea_distribution=identity,
        ea_installed_files_sha256=Sha256Digest(digest.hexdigest()),
        python_implementation=sys.implementation.name,
        python_version=".".join(map(str, sys.version_info[:3])),
        python_cache_tag=sys.implementation.cache_tag or "",
        sys_platform=sys.platform,
        platform_tag=sysconfig.get_platform(),
        distributions=tuple(identity for identity, _ in pairs),
        provenance_kind="installed_local_wheel_v1",
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProvenanceError("direct_url.json contains a duplicate key")
        result[key] = value
    return result


def _parse_direct_url(distribution: importlib.metadata.Distribution, repository: Path) -> None:
    raw = distribution.read_text("direct_url.json")
    if type(raw) is not str:
        raise ProvenanceError("ea-quant direct_url.json is missing")
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ProvenanceError("ea-quant direct_url.json is invalid") from exc
    if type(value) is not dict or frozenset(value) != {"url", "dir_info"}:
        raise ProvenanceError("ea-quant direct_url.json must be a closed object")
    if value["dir_info"] != {"editable": True} or type(value["url"]) is not str:
        raise ProvenanceError("ea-quant direct URL must identify an editable install")
    try:
        parsed = urllib.parse.urlsplit(value["url"])
        path_bytes = urllib.parse.unquote_to_bytes(parsed.path)
        decoded = path_bytes.decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProvenanceError("ea-quant direct URL cannot be decoded") from exc
    if (
        parsed.scheme != "file"
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not decoded
        or not Path(decoded).is_absolute()
    ):
        raise ProvenanceError("ea-quant direct URL must be a local path without authority")
    try:
        target = Path(decoded).resolve(strict=True)
    except OSError as exc:
        raise ProvenanceError("ea-quant direct URL target cannot be resolved") from exc
    if target != repository:
        raise ProvenanceError("ea-quant editable install points at another repository")


def _validate_sys_path(
    *,
    repository: Path,
    active_roots: tuple[Path, ...],
) -> None:
    try:
        base_prefix = Path(sys.base_prefix).resolve(strict=True)
        source_root = (repository / "src").resolve(strict=True)
    except OSError as exc:
        raise ProvenanceError("runtime import roots cannot be resolved") from exc
    seen: set[Path] = set()
    for raw in sys.path:
        if type(raw) is not str or not raw or not Path(raw).is_absolute():
            raise ProvenanceError("sys.path contains an empty or relative entry")
        resolved = Path(raw).resolve(strict=False)
        if resolved in seen:
            raise ProvenanceError("sys.path contains a duplicate physical entry")
        seen.add(resolved)
        if resolved in active_roots or resolved == source_root:
            continue
        if not resolved.is_relative_to(base_prefix):
            raise ProvenanceError("sys.path contains an injected import root")
        lowered_parts = {part.lower() for part in resolved.parts}
        if {"site-packages", "dist-packages"} & lowered_parts:
            raise ProvenanceError("base interpreter site-packages are unsupported")


def _validate_loaded_ea_modules(repository: Path) -> None:
    package_root = (repository / "src" / "ea").resolve(strict=True)
    expected_init = package_root / "__init__.py"
    spec = importlib.util.find_spec("ea")
    if (
        spec is None
        or spec.origin is None
        or Path(spec.origin).resolve(strict=True) != expected_init
        or spec.submodule_search_locations is None
        or tuple(Path(path).resolve(strict=True) for path in spec.submodule_search_locations)
        != (package_root,)
        or Path(ea.__file__).resolve(strict=True) != expected_init
    ):
        raise ProvenanceError("imported ea package is not bound to the reviewed repository")
    for name, module in tuple(sys.modules.items()):
        if name != "ea" and not name.startswith("ea."):
            continue
        origin = getattr(module, "__file__", None)
        if type(origin) is not str:
            raise ProvenanceError("loaded ea module has no regular tracked origin")
        try:
            path = Path(origin).resolve(strict=True)
        except OSError as exc:
            raise ProvenanceError("loaded ea module origin cannot be resolved") from exc
        if not path.is_file() or not path.is_relative_to(package_root):
            raise ProvenanceError("loaded ea module escaped the reviewed package tree")


def _collect_runtime_evidence(
    snapshot: RepositorySnapshot,
    *,
    uv_lock_path: Path,
) -> RuntimeEvidence:
    _require_runtime_flags()
    _validate_source_tree(snapshot)
    roots = _active_metadata_roots()
    _validate_sys_path(repository=snapshot.root, active_roots=roots)

    active_distributions = tuple(
        importlib.metadata.distributions(path=[str(root) for root in roots])
    )
    active_pairs: list[tuple[DistributionIdentity, importlib.metadata.Distribution]] = []
    for distribution in active_distributions:
        try:
            location = Path(str(distribution.locate_file(""))).resolve(strict=True)
        except OSError as exc:
            raise ProvenanceError("active distribution root cannot be resolved") from exc
        if location not in roots:
            raise ProvenanceError("active distribution escaped active metadata roots")
        active_pairs.append((_distribution_identity(distribution), distribution))
    names = [identity.name for identity, _ in active_pairs]
    if len(names) != len(set(names)):
        raise ProvenanceError("active distribution names are not unique")
    active_pairs.sort(key=lambda pair: pair[0].name)

    ea_pairs = [pair for pair in active_pairs if pair[0].name == "ea-quant"]
    if len(ea_pairs) != 1:
        raise ProvenanceError("active environment must contain exactly one ea-quant")
    ea_identity, ea_distribution = ea_pairs[0]
    _parse_direct_url(ea_distribution, snapshot.root)
    try:
        unscoped_version = importlib.metadata.version("ea-quant")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ProvenanceError("unscoped ea-quant metadata is unavailable") from exc
    if unscoped_version != ea_identity.version or ea.__version__ != ea_identity.version:
        raise ProvenanceError("EA runtime and metadata versions are inconsistent")

    external: list[DistributionIdentity] = []
    for distribution in importlib.metadata.distributions():
        try:
            location = Path(str(distribution.locate_file(""))).resolve(strict=True)
        except OSError as exc:
            raise ProvenanceError("ambient distribution root cannot be resolved") from exc
        if location in roots:
            continue
        identity = _distribution_identity(distribution)
        if identity != ea_identity or location != (snapshot.root / "src").resolve(strict=True):
            raise ProvenanceError("ambient or path distribution is unsupported")
        external.append(identity)
    if len(external) > 1:
        raise ProvenanceError("multiple source-tree EA metadata projections are unsupported")

    _validate_loaded_ea_modules(snapshot.root)
    if not isinstance(uv_lock_path, Path):
        raise ProvenanceError("uv_lock_path must be a Path")
    try:
        resolved_lock = uv_lock_path.resolve(strict=True)
    except OSError as exc:
        raise ProvenanceError("uv.lock cannot be resolved") from exc
    if (
        uv_lock_path.is_symlink()
        or resolved_lock != snapshot.root / "uv.lock"
        or not resolved_lock.is_file()
    ):
        raise ProvenanceError("uv.lock must be the repository's real root lock file")
    try:
        lock_bytes = resolved_lock.read_bytes()
    except OSError as exc:
        raise ProvenanceError("uv.lock cannot be read") from exc
    return RuntimeEvidence(
        ea_version=ea_identity.version,
        python_implementation=sys.implementation.name,
        python_version=".".join(str(part) for part in sys.version_info[:3]),
        python_cache_tag=sys.implementation.cache_tag or "",
        sys_platform=sys.platform,
        platform_tag=sysconfig.get_platform(),
        distributions=tuple(identity for identity, _ in active_pairs),
        uv_lock_bytes=lock_bytes,
    )


def collect_provenance(repository: Path, uv_lock_path: Path) -> ProvenanceEvidence:
    """Collect provenance between two identical clean Git observations."""
    before = _repository_snapshot(repository)
    runtime = _collect_runtime_evidence(before, uv_lock_path=uv_lock_path)
    after = _repository_snapshot(before.root)
    if before.code != after.code or before.tracked_ea_files != after.tracked_ea_files:
        raise ProvenanceError("repository changed while provenance was collected")
    return ProvenanceEvidence(
        repository=before.root,
        code=before.code,
        runtime=runtime,
    )


def verify_provenance(
    expected: ProvenanceEvidence,
    repository: Path,
    uv_lock_path: Path,
) -> None:
    """Recollect and compare terminal Git/runtime/lock evidence."""
    if type(expected) is not ProvenanceEvidence:
        raise ProvenanceError("expected must be ProvenanceEvidence")
    actual = collect_provenance(repository, uv_lock_path)
    if actual != expected:
        raise ProvenanceError("terminal provenance differs from prepared evidence")
