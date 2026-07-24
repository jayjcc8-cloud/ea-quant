"""External evidence comparison for a validated run manifest."""

from __future__ import annotations

from ea.core.run import DataFingerprint
from ea.experiments._manifest_model import (
    CodeEvidence,
    EvidenceMismatchError,
    ManifestError,
    RunManifest,
    RuntimeEvidence,
    RuntimeSpec,
)
from ea.experiments._manifest_wire import LOCK_DOMAIN, digest


def verify_manifest_evidence(
    manifest: RunManifest,
    *,
    code: CodeEvidence,
    runtime: RuntimeEvidence,
    data: DataFingerprint,
) -> None:
    """Compare evidence already recollected/recomputed by the outer verifier."""
    if type(manifest) is not RunManifest:
        raise EvidenceMismatchError("manifest must be a RunManifest")
    try:
        expected_runtime = RuntimeSpec(
            ea_version=runtime.ea_version,
            python_implementation=runtime.python_implementation,
            python_version=runtime.python_version,
            python_cache_tag=runtime.python_cache_tag,
            sys_platform=runtime.sys_platform,
            platform_tag=runtime.platform_tag,
            distributions=runtime.distributions,
            uv_lock_sha256=digest(LOCK_DOMAIN, runtime.uv_lock_bytes),
        )
    except (AttributeError, ManifestError) as exc:
        raise EvidenceMismatchError("runtime evidence is invalid") from exc
    if (
        type(code) is not CodeEvidence
        or type(data) is not DataFingerprint
        or manifest.spec.code.commit != code.commit
        or manifest.spec.data != data
        or manifest.spec.runtime != expected_runtime
    ):
        raise EvidenceMismatchError("external code, data, lock, or runtime evidence differs")
