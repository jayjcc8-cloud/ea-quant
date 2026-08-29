"""Closed canonical run-manifest public contract."""

from __future__ import annotations

import json as json
import re as re
import struct as struct
import unicodedata as unicodedata
from collections.abc import Sequence as Sequence
from dataclasses import dataclass as dataclass
from datetime import UTC as UTC
from datetime import datetime as datetime
from enum import StrEnum as StrEnum
from hashlib import sha256 as sha256
from math import isfinite as isfinite
from typing import NoReturn as NoReturn

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
    InstalledRuntimeSpecV2 as InstalledRuntimeSpecV2,
)
from ea.experiments._manifest_model import (
    LineageInputs as LineageInputs,
)
from ea.experiments._manifest_model import (
    LineageInputsV2 as LineageInputsV2,
)
from ea.experiments._manifest_model import (
    LineageSpec as LineageSpec,
)
from ea.experiments._manifest_model import (
    LineageSpecV2 as LineageSpecV2,
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
    RunManifestV2 as RunManifestV2,
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
    build_lineage_spec_v2 as build_lineage_spec_v2,
)
from ea.experiments._manifest_model import (
    build_manifest as build_manifest,
)
from ea.experiments._manifest_model import (
    build_manifest_v2 as build_manifest_v2,
)
from ea.experiments._manifest_model import (
    canonical_lineage_bytes as canonical_lineage_bytes,
)
from ea.experiments._manifest_model import (
    canonical_manifest_bytes as canonical_manifest_bytes,
)

_INCIDENTAL_COMPATIBILITY_OBJECTS = (
    json,
    re,
    struct,
    unicodedata,
    Sequence,
    dataclass,
    UTC,
    datetime,
    StrEnum,
    sha256,
    isfinite,
    NoReturn,
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
    LineageInputsV2,
    CodeSpec,
    ConfigurationSpec,
    RuntimeSpec,
    InstalledRuntimeSpecV2,
    RandomnessSpec,
    LineageSpec,
    LineageSpecV2,
    RunManifest,
    RunManifestV2,
    canonical_lineage_bytes,
    canonical_manifest_bytes,
    build_lineage_spec,
    build_lineage_spec_v2,
    build_manifest,
    build_manifest_v2,
    read_manifest,
    verify_manifest_evidence,
)
for _public_object in _PUBLIC_MANIFEST_OBJECTS:
    _public_object.__module__ = __name__
del _public_object
