"""Trusted Issue #14 preparation, verification, and runtime-admission composition."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Lock
from types import FunctionType
from typing import Protocol, cast

from ea.config.settings import Settings
from ea.core.audit import (
    AuditRecordKind,
    AuditSubjectKind,
    canonical_run_prepared_audit_payload,
)
from ea.core.market_data import MarketDataEnvelope
from ea.core.numeric import OrderedFloat64Policy
from ea.core.run import RunContractError, RunReference
from ea.data.fingerprint import MarketDataSelection
from ea.experiments.binding import (
    BoundAuditPort,
    BoundOutputPort,
    RawAuditPort,
    RawOutputPort,
)
from ea.experiments.manifest import (
    CodeEvidence,
    EffectiveParameter,
    LineageInputs,
    LineageSpec,
    NormalizedConfiguration,
    RandomnessSpec,
    build_lineage_spec,
    build_manifest,
    canonical_manifest_bytes,
    verify_manifest_evidence,
)
from ea.experiments.provenance import (
    ProvenanceEvidence,
    collect_provenance,
    verify_provenance,
)
from ea.experiments.randomness import Pcg64StreamFactory
from ea.experiments.store import AuditRunBinding, LocalResultStore, PreparedRun, RunIdProvider

_PREPARED_SEAL = object()
_PREFLIGHT_SLOT = "_ea_reproducible_preflight_grant_v1"
_MISSING = object()
_is_preflight_seal: Callable[[object], bool]


class RunCompositionError(RuntimeError):
    """Raised when outer preparation cannot prove or preserve the run contract."""


class PreflightSession:
    """One-use post-preflight authority accepted only from the tracked launcher."""

    __slots__ = ("_commit", "_consumed", "_lock", "_repository")
    _commit: str
    _consumed: bool
    _lock: Lock
    _repository: Path

    def __init__(
        self,
        seal: object,
        *,
        repository: Path,
        commit: str,
    ) -> None:
        if not _is_preflight_seal(seal):
            raise RunCompositionError(
                "preflight sessions can only be accepted from the tracked launcher"
            )
        object.__setattr__(self, "_repository", repository)
        object.__setattr__(self, "_commit", commit)
        object.__setattr__(self, "_consumed", False)
        object.__setattr__(self, "_lock", Lock())

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("preflight session is immutable")

    @property
    def commit(self) -> str:
        """Expose only the already-verified commit for launcher diagnostics."""
        return self._commit

    def _consume(self) -> tuple[Path, str]:
        with self._lock:
            if self._consumed:
                raise RunCompositionError("preflight session was already consumed")
            object.__setattr__(self, "_consumed", True)
            return self._repository, self._commit


def _build_preflight_boundary() -> tuple[
    Callable[[object], bool],
    Callable[[object], PreflightSession],
    Callable[[PreflightSession], tuple[Path, str]],
]:
    session_seal = object()
    issued_sessions: set[PreflightSession] = set()
    issued_lock = Lock()

    def is_session_seal(candidate: object) -> bool:
        return candidate is session_seal

    def accept(grant: object) -> PreflightSession:
        if getattr(sys, _PREFLIGHT_SLOT, _MISSING) is not grant:
            raise RunCompositionError("a pending tracked-launcher preflight grant is required")
        delattr(sys, _PREFLIGHT_SLOT)

        grant_type = type(grant)
        grant_module = sys.modules.get(grant_type.__module__)
        module_file = getattr(grant_module, "__file__", None)
        consume = getattr(grant, "consume", None)
        consume_function = getattr(consume, "__func__", None)
        if (
            grant_module is None
            or type(module_file) is not str
            or grant_type.__name__ != "_PreflightGrant"
            or grant_type.__qualname__ != "_bootstrap.<locals>._PreflightGrant"
            or type(consume_function) is not FunctionType
            or consume_function.__qualname__ != "_bootstrap.<locals>._PreflightGrant.consume"
        ):
            raise RunCompositionError("preflight grant was not created by launcher bootstrap")
        consume_callable = cast(Callable[[], object], consume)
        try:
            value = consume_callable()
        except Exception as exc:
            raise RunCompositionError(
                "tracked launcher preflight grant could not be consumed"
            ) from exc
        if type(value) is not tuple or len(value) != 2:
            raise RunCompositionError("launcher grant returned invalid preflight evidence")
        repository, commit = value
        if not isinstance(repository, Path) or type(commit) is not str:
            raise RunCompositionError("launcher grant returned invalid preflight evidence")
        try:
            if (
                not repository.is_absolute()
                or repository.is_symlink()
                or repository.resolve(strict=True) != repository
                or not repository.is_dir()
            ):
                raise RunCompositionError("launcher grant repository is not one resolved directory")
        except OSError as exc:
            raise RunCompositionError("launcher grant repository cannot be resolved") from exc
        try:
            launcher_path = repository / "scripts/reproducible_run.py"
            if launcher_path.is_symlink():
                raise RunCompositionError("tracked launcher cannot be a symlink")
            expected_launcher = launcher_path.resolve(strict=True)
            actual_module = Path(module_file).resolve(strict=True)
            actual_function = Path(consume_function.__code__.co_filename).resolve(strict=True)
        except OSError as exc:
            raise RunCompositionError("tracked launcher origin cannot be resolved") from exc
        if actual_module != expected_launcher or actual_function != expected_launcher:
            raise RunCompositionError(
                "preflight grant did not originate from this repository launcher"
            )
        try:
            CodeEvidence(commit)
        except RunContractError as exc:
            raise RunCompositionError("launcher grant commit is not canonical") from exc
        session = PreflightSession(
            session_seal,
            repository=repository,
            commit=commit,
        )
        with issued_lock:
            issued_sessions.add(session)
        return session

    def consume_session(session: PreflightSession) -> tuple[Path, str]:
        with issued_lock:
            if type(session) is not PreflightSession or session not in issued_sessions:
                raise RunCompositionError(
                    "preflight session was not issued by launcher bootstrap or was already consumed"
                )
            issued_sessions.remove(session)
        return session._consume()

    return is_session_seal, accept, consume_session


(
    _is_preflight_seal,
    _accept_preflight_grant,
    _consume_preflight_session,
) = _build_preflight_boundary()
del _build_preflight_boundary


def _accept_launcher_preflight(grant: object) -> PreflightSession:
    """Consume the one pending grant created inside successful launcher bootstrap."""
    return _accept_preflight_grant(grant)


class AuditPortFactory(Protocol):
    """Construct/start the mandatory audit adapter after manifest durability."""

    def __call__(self, prepared: AuditRunBinding) -> RawAuditPort: ...


class OutputPortFactory(Protocol):
    """Construct/start the output adapter after manifest durability."""

    def __call__(self) -> RawOutputPort: ...


@dataclass(frozen=True, slots=True)
class PreparedReproducibleRun:
    """Outer-only evidence retained around the store's narrow prepared context."""

    _seal: object
    _prepared: PreparedRun
    spec: LineageSpec
    market_data: MarketDataSelection
    provenance: ProvenanceEvidence
    _store: LocalResultStore

    def __post_init__(self) -> None:
        if self._seal is not _PREPARED_SEAL:
            raise RunCompositionError(
                "prepared reproducible contexts can only be issued by the composition root"
            )
        if type(self._prepared) is not PreparedRun:
            raise RunCompositionError("prepared context must come from LocalResultStore")
        if type(self.spec) is not LineageSpec:
            raise RunCompositionError("prepared lineage must be a LineageSpec")
        if type(self.market_data) is not MarketDataSelection:
            raise RunCompositionError("prepared data must be a MarketDataSelection")
        if type(self.provenance) is not ProvenanceEvidence:
            raise RunCompositionError("prepared provenance must be collected evidence")
        if type(self._store) is not LocalResultStore:
            raise RunCompositionError("prepared store must be a LocalResultStore")
        if (
            self.spec.data != self.market_data.fingerprint
            or self.spec.replay_window != self.market_data.window
        ):
            raise RunCompositionError("lineage is not bound to the exact selected replay data")

        manifest = build_manifest(self.spec, self._prepared.reference.run_id)
        verify_manifest_evidence(
            manifest,
            code=self.provenance.code,
            runtime=self.provenance.runtime,
            data=self.market_data.fingerprint,
        )
        payload = canonical_manifest_bytes(manifest)
        if (
            manifest.reference != self._prepared.reference
            or sha256(payload).hexdigest() != self._prepared.manifest_sha256.value
        ):
            raise RunCompositionError("prepared store evidence differs from the rebuilt manifest")

    @property
    def reference(self) -> RunReference:
        return self._prepared.reference


@dataclass(frozen=True, slots=True)
class AdmittedRun:
    """The only narrow context supplied to future feed/runtime startup."""

    reference: RunReference
    market_data: tuple[MarketDataEnvelope, ...]
    audit: BoundAuditPort
    output: BoundOutputPort
    randomness: Pcg64StreamFactory
    numeric: OrderedFloat64Policy

    def __post_init__(self) -> None:
        if type(self.reference) is not RunReference:
            raise RunCompositionError("admitted reference must be a RunReference")
        if type(self.market_data) is not tuple or not self.market_data:
            raise RunCompositionError("admitted market data must be a non-empty exact tuple")
        if any(type(event) is not MarketDataEnvelope for event in self.market_data):
            raise RunCompositionError("admitted market data contains an invalid event")
        if (
            type(self.audit) is not BoundAuditPort
            or type(self.output) is not BoundOutputPort
            or self.audit.reference != self.reference
            or self.output.reference != self.reference
        ):
            raise RunCompositionError("admitted ports are not bound to the same run reference")
        if type(self.randomness) is not Pcg64StreamFactory:
            raise RunCompositionError("admitted randomness is not lineage-bound")
        if type(self.numeric) is not OrderedFloat64Policy:
            raise RunCompositionError("admitted numeric policy is not lineage-bound")


def prepare_reproducible_run(
    *,
    preflight: PreflightSession,
    settings: Settings,
    market_data: MarketDataSelection,
    parameters: tuple[EffectiveParameter, ...],
    master_seed: int,
    stream_labels: tuple[str, ...],
    store: LocalResultStore,
    run_id_provider: RunIdProvider,
) -> PreparedReproducibleRun:
    """Consume launcher authority, recollect evidence, and publish one manifest."""
    if type(preflight) is not PreflightSession:
        raise RunCompositionError("preparation requires a tracked-launcher preflight session")
    if type(settings) is not Settings:
        raise RunCompositionError("settings must be the final immutable Settings snapshot")
    if type(market_data) is not MarketDataSelection:
        raise RunCompositionError("market_data must be a verified MarketDataSelection")
    if type(store) is not LocalResultStore:
        raise RunCompositionError("store must be a LocalResultStore")
    if type(parameters) is not tuple or any(
        type(parameter) is not EffectiveParameter for parameter in parameters
    ):
        raise RunCompositionError("parameters must be an immutable EffectiveParameter tuple")
    parameter_names = [parameter.name for parameter in parameters]
    if len(parameter_names) != len(set(parameter_names)):
        raise RunCompositionError("effective parameter names must be unique")
    if type(stream_labels) is not tuple:
        raise RunCompositionError("stream_labels must be an immutable tuple")
    RandomnessSpec(
        master_seed=master_seed,
        stream_labels=tuple(sorted(stream_labels)),
    )
    if not callable(run_id_provider):
        raise RunCompositionError("run_id_provider must be callable")

    normalized_configuration = NormalizedConfiguration(
        schema_version=settings.schema_version,
        environment=settings.environment.value,
        mode=settings.run.mode.value,
    )
    repository, preflight_commit = _consume_preflight_session(preflight)
    uv_lock_path = repository / "uv.lock"
    provenance = collect_provenance(repository, uv_lock_path)
    if provenance.code.commit != preflight_commit:
        raise RunCompositionError("collector observed a different commit than launcher preflight")
    spec = build_lineage_spec(
        LineageInputs(
            code=provenance.code,
            configuration=normalized_configuration,
            data=market_data.fingerprint,
            replay_window=market_data.window,
            parameters=parameters,
            runtime=provenance.runtime,
            master_seed=master_seed,
            stream_labels=stream_labels,
        )
    )
    prepared = store.prepare(spec, run_id_provider)
    return PreparedReproducibleRun(
        _seal=_PREPARED_SEAL,
        _prepared=prepared,
        spec=spec,
        market_data=market_data,
        provenance=provenance,
        _store=store,
    )


def verify_reproducible_run(
    prepared: PreparedReproducibleRun,
) -> None:
    """Rebind durable bytes, data, Git, runtime, and lock evidence."""
    if type(prepared) is not PreparedReproducibleRun:
        raise RunCompositionError("prepared must be a PreparedReproducibleRun")
    manifest = prepared._store.verify_manifest(
        prepared._prepared.manifest_verification,
    )
    if manifest.reference != prepared.reference or manifest.spec != prepared.spec:
        raise RunCompositionError("persisted manifest differs from the prepared context")
    # Re-instantiation deliberately recomputes the fingerprint from the retained exact tuple.
    MarketDataSelection(
        window=prepared.market_data.window,
        events=prepared.market_data.events,
        fingerprint=prepared.market_data.fingerprint,
    )
    repository = prepared.provenance.repository
    verify_provenance(prepared.provenance, repository, repository / "uv.lock")
    verify_manifest_evidence(
        manifest,
        code=prepared.provenance.code,
        runtime=prepared.provenance.runtime,
        data=prepared.market_data.fingerprint,
    )


def admit_reproducible_run[StartResult](
    prepared: PreparedReproducibleRun,
    *,
    audit_factory: AuditPortFactory,
    output_factory: OutputPortFactory,
    start: Callable[[AdmittedRun], StartResult],
) -> StartResult:
    """Enforce durable manifest < adapters/audit ack < feed/runtime startup."""
    if type(prepared) is not PreparedReproducibleRun:
        raise RunCompositionError("prepared must be a PreparedReproducibleRun")
    if not callable(audit_factory) or not callable(output_factory) or not callable(start):
        raise RunCompositionError("adapter factories and start callback must be callable")

    # The store returned before this function can construct or start any adapter.
    audit = BoundAuditPort(
        prepared._prepared.audit,
        audit_factory(prepared._prepared.audit),
    )
    output = BoundOutputPort(prepared._prepared.output, output_factory())
    # Runtime/feed admission is impossible until the mandatory first append acknowledges.
    audit.append(
        record_kind=AuditRecordKind.RUN_PREPARED,
        subject_kind=AuditSubjectKind.RUN_MANIFEST,
        subject_sha256=prepared._prepared.manifest_sha256,
        canonical_payload=canonical_run_prepared_audit_payload(prepared._prepared.audit.binding),
    )

    admitted = AdmittedRun(
        reference=prepared.reference,
        market_data=prepared.market_data.events,
        audit=audit,
        output=output,
        randomness=Pcg64StreamFactory.from_lineage(prepared.spec),
        numeric=OrderedFloat64Policy(prepared.spec.runtime.numeric_policy),
    )
    return start(admitted)
