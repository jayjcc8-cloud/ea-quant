"""Closed canonical run-manifest public contract."""

from __future__ import annotations

from ea.core.run import (
    DataFingerprint as DataFingerprint,
)
from ea.core.run import (
    ReplayWindow as ReplayWindow,
)
from ea.core.run import (
    RunContractError as RunContractError,
)
from ea.core.run import (
    RunId as RunId,
)
from ea.core.run import (
    RunReference as RunReference,
)
from ea.core.run import (
    Sha256Digest as Sha256Digest,
)
from ea.core.run import (
    validate_stream_label as validate_stream_label,
)
from ea.experiments._manifest_codec import read_manifest as read_manifest
from ea.experiments._manifest_evidence import (
    verify_manifest_evidence as verify_manifest_evidence,
)
from ea.experiments._manifest_model import (
    CodeEvidence as CodeEvidence,
)
from ea.experiments._manifest_model import (
    CodeSpec as CodeSpec,
)
from ea.experiments._manifest_model import (
    ConfigurationSpec as ConfigurationSpec,
)
from ea.experiments._manifest_model import (
    DistributionIdentity as DistributionIdentity,
)
from ea.experiments._manifest_model import (
    EffectiveParameter as EffectiveParameter,
)
from ea.experiments._manifest_model import (
    EvidenceMismatchError as EvidenceMismatchError,
)
from ea.experiments._manifest_model import (
    LineageInputs as LineageInputs,
)
from ea.experiments._manifest_model import (
    LineageSpec as LineageSpec,
)
from ea.experiments._manifest_model import (
    ManifestError as ManifestError,
)
from ea.experiments._manifest_model import (
    ManifestFormatError as ManifestFormatError,
)
from ea.experiments._manifest_model import (
    NormalizedConfiguration as NormalizedConfiguration,
)
from ea.experiments._manifest_model import (
    ParameterKind as ParameterKind,
)
from ea.experiments._manifest_model import (
    RandomnessSpec as RandomnessSpec,
)
from ea.experiments._manifest_model import (
    RunManifest as RunManifest,
)
from ea.experiments._manifest_model import (
    RuntimeEvidence as RuntimeEvidence,
)
from ea.experiments._manifest_model import (
    RuntimeSpec as RuntimeSpec,
)
from ea.experiments._manifest_model import (
    build_lineage_spec as build_lineage_spec,
)
from ea.experiments._manifest_model import (
    build_manifest as build_manifest,
)
from ea.experiments._manifest_model import (
    canonical_lineage_bytes as canonical_lineage_bytes,
)
from ea.experiments._manifest_model import (
    canonical_manifest_bytes as canonical_manifest_bytes,
)

_INCIDENTAL_CORE_COMPATIBILITY_OBJECTS = (
    DataFingerprint,
    ReplayWindow,
    RunContractError,
    RunId,
    RunReference,
    Sha256Digest,
    validate_stream_label,
)
_PUBLIC_MANIFEST_OBJECTS: tuple[object, ...] = (
    ManifestError,
    ManifestFormatError,
    EvidenceMismatchError,
    NormalizedConfiguration,
    CodeEvidence,
    DistributionIdentity,
    RuntimeEvidence,
    ParameterKind,
    EffectiveParameter,
    LineageInputs,
    CodeSpec,
    ConfigurationSpec,
    RuntimeSpec,
    RandomnessSpec,
    LineageSpec,
    RunManifest,
    canonical_lineage_bytes,
    canonical_manifest_bytes,
    build_lineage_spec,
    build_manifest,
    read_manifest,
    verify_manifest_evidence,
)
for _public_object in _PUBLIC_MANIFEST_OBJECTS:
    _public_object.__module__ = __name__
del _public_object
