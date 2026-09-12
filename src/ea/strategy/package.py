"""Deterministic, bounded containers for explicitly trusted local Python strategies."""

from __future__ import annotations

import io
import json
import os
import re
import stat
import zipfile
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from ea.strategy.registry import ParameterV1, StrategyDescriptorV1
from ea.strategy.sdk_v2 import StrategyDescriptorV2

MAX_MEMBER_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 2 * MAX_MEMBER_BYTES + 4096
MEMBERS = ("manifest.json", "strategy.py")


class StrategyPackageError(ValueError):
    """Stable failure without executable-code exception details."""


def canonical_json(document: object) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


@dataclass(frozen=True, slots=True)
class StrategyPackageIdentityV1:
    package_id: str
    artifact_sha256: str
    schema: str = "ea-strategy-package-identity-v1"


@dataclass(frozen=True, slots=True)
class StrategyPackageV1:
    identity: StrategyPackageIdentityV1
    artifact_bytes: bytes
    source_bytes: bytes
    descriptor: StrategyDescriptorV1
    outcome_mode: str

    def document(self) -> dict[str, Any]:
        return {
            **asdict(self.identity),
            **self.descriptor.document(),
            "outcome_mode": self.outcome_mode,
        }

    def module(self) -> dict[str, Any]:
        return _module(self.source_bytes, self.identity.artifact_sha256)


@dataclass(frozen=True, slots=True)
class StrategyPackageV2:
    identity: StrategyPackageIdentityV1
    artifact_bytes: bytes
    source_bytes: bytes
    descriptor: StrategyDescriptorV2

    def document(self) -> dict[str, Any]:
        return {
            **asdict(self.identity),
            **self.descriptor.document(),
            "schema": "ea-strategy-package-v2",
        }

    def module(self) -> dict[str, Any]:
        return _module(self.source_bytes, self.identity.artifact_sha256)


StrategyPackage = StrategyPackageV1 | StrategyPackageV2


def _module(source_bytes: bytes, digest: str) -> dict[str, Any]:
    namespace: dict[str, Any] = {"__name__": "ea_local_" + digest}
    try:
        exec(compile(source_bytes, "<trusted-local-strategy>", "exec"), namespace)
        if not all(
            callable(namespace.get(name)) for name in ("validate_parameters", "create_logic")
        ):
            raise ValueError
    except BaseException:
        raise StrategyPackageError("strategy code contract failed") from None
    return namespace


def _container(manifest: bytes, source: bytes) -> bytes:
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
        for name, payload in zip(MEMBERS, (manifest, source), strict=True):
            member = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            member.create_system = 3
            member.external_attr = (stat.S_IFREG | 0o444) << 16
            member.compress_type = zipfile.ZIP_STORED
            archive.writestr(member, payload)
    return result.getvalue()


def validate_package(payload: bytes, *, expected_sha256: str | None = None) -> StrategyPackage:
    try:
        if type(payload) is not bytes or len(payload) > MAX_ARTIFACT_BYTES:
            raise ValueError
        digest = sha256(payload).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if archive.namelist() != list(MEMBERS):
                raise ValueError
            if any(info.file_size > MAX_MEMBER_BYTES for info in archive.infolist()):
                raise ValueError
            manifest, source = (archive.read(name) for name in MEMBERS)
        if payload != _container(manifest, source):
            raise ValueError
        document = json.loads(manifest)
        if canonical_json(document) != manifest or set(document) != {
            "schema_version",
            "package_id",
            "strategy",
        }:
            raise ValueError
        if type(document["schema_version"]) is not int or document["schema_version"] not in (1, 2):
            raise ValueError
        package_id = document["package_id"]
        if type(package_id) is not str or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", package_id
        ):
            raise ValueError
        strategy = document["strategy"]
        version = document["schema_version"]
        fields = {"outcome_mode"} if version == 1 else {"action_contract", "position_lifecycle"}
        if set(strategy) != {"id", "version", "display_name", "parameters"} | fields:
            raise ValueError
        if type(strategy["id"]) is not str or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", strategy["id"]
        ):
            raise ValueError
        if (
            type(strategy["display_name"]) is not str
            or not 1 <= len(strategy["display_name"]) <= 128
        ):
            raise ValueError
        if type(strategy["parameters"]) is not list or len(strategy["parameters"]) > 32:
            raise ValueError
        descriptor = StrategyDescriptorV1(
            1,
            strategy["id"],
            strategy["version"],
            strategy["display_name"],
            True,
            tuple(ParameterV1(**p) for p in strategy["parameters"]),
        )
        if version == 1 and strategy["outcome_mode"] not in {
            "optional_single_long_entry",
            "required_single_long_entry",
            "no_entry",
        }:
            raise ValueError
        source.decode("utf-8")
        if version == 2 and (
            strategy["action_contract"] != "V2"
            or strategy["position_lifecycle"] != "single-long-round-trip-v1"
        ):
            raise ValueError
        package: StrategyPackage = (
            StrategyPackageV1(
                StrategyPackageIdentityV1(package_id, digest),
                payload,
                source,
                descriptor,
                strategy["outcome_mode"],
            )
            if version == 1
            else StrategyPackageV2(
                StrategyPackageIdentityV1(package_id, digest),
                payload,
                source,
                StrategyDescriptorV2(
                    strategy_id=descriptor.strategy_id,
                    strategy_version=descriptor.strategy_version,
                    display_name=descriptor.display_name,
                    parameters=descriptor.parameters,
                ),
            )
        )
    except Exception:
        raise StrategyPackageError("strategy package validation failed") from None
    package.module()
    return package


def read_regular(path: Path, root: Path, *, limit: int = MAX_ARTIFACT_BYTES) -> bytes:
    directory: int | None = None
    try:
        resolved_root = root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        if not root.is_absolute() or path.is_symlink() or resolved_path.parent != resolved_root:
            raise ValueError
        expected_root = resolved_root.stat()
        # Traverse from an anchored descriptor. O_NOFOLLOW on a full pathname protects
        # only the final component and cannot prevent ancestor/root replacement.
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory = os.open(resolved_root.anchor, flags)
        for component in resolved_root.parts[1:]:
            child = os.open(component, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        actual_root = os.fstat(directory)
        if (actual_root.st_dev, actual_root.st_ino) != (expected_root.st_dev, expected_root.st_ino):
            raise ValueError
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError
            payload = handle.read(limit + 1)
        if len(payload) > limit:
            raise ValueError
        return payload
    except (OSError, ValueError, RuntimeError):
        raise StrategyPackageError(
            "strategy file must be a bounded regular file inside its root"
        ) from None
    finally:
        if directory is not None:
            os.close(directory)


def pack_strategy(source: Path, output: Path) -> StrategyPackage:
    try:
        if (
            not source.is_absolute()
            or not output.is_absolute()
            or not source.is_dir()
            or source.is_symlink()
        ):
            raise ValueError
        if sorted(p.name for p in source.iterdir()) != list(MEMBERS):
            raise ValueError
        if output.suffix != ".eastrategy":
            raise ValueError
        payload = _container(
            *(read_regular(source / name, source, limit=MAX_MEMBER_BYTES) for name in MEMBERS)
        )
        package = validate_package(payload)
        with output.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return package
    except (OSError, ValueError):
        raise StrategyPackageError("strategy pack failed") from None
