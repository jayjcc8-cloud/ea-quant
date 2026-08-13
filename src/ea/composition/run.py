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
    AuditRecoveryRecordSource,
    AuditSubjectKind,
    canonical_run_prepared_audit_payload,
)
from ea.core.market_data import MarketDataEnvelope
from ea.core.numeric import OrderedFloat64Policy
from ea.core.run import RunBinding, RunContractError, RunId, RunReference
from ea.data.fingerprint import MarketDataSelection
from ea.experiments.audit import PosixAuditJournal
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
from ea.experiments.store import (
    AuditRunBinding,
    LocalResultStore,
    PreparedRun,
    RecoveredRun,
    RunIdProvider,
    StoreError,
    VerifiedIncompleteRecoveryBinding,
    VerifiedTerminalRecoveryBinding,
)

_PREPARED_SEAL = object()
_RECOVERED_ADMISSION_SEAL = object()
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


class AdmittedRecoveredRun:
    """One-use store-issued recovery prefix bound to its reopened audit port."""

    __slots__ = (
        "_consumption_lock",
        "_consumption_state",
        "_consumption_token",
        "audit",
        "binding",
        "records",
    )
    audit: BoundAuditPort
    binding: RunBinding
    records: AuditRecoveryRecordSource

    def __init__(
        self,
        seal: object,
        *,
        recovered: RecoveredRun,
        audit: BoundAuditPort,
        records: AuditRecoveryRecordSource,
    ) -> None:
        if seal is not _RECOVERED_ADMISSION_SEAL or type(recovered) is not RecoveredRun:
            raise RunCompositionError("recovery admission must consume store-issued evidence")
        binding = RunBinding(recovered.reference, recovered.manifest_sha256)
        if (
            type(audit) is not BoundAuditPort
            or audit.binding != binding
            or records.binding != binding
            or records.record_count != recovered.record_count
        ):
            raise RunCompositionError("recovered prefix is not bound to one admitted audit port")
        self._consumption_lock = Lock()
        self._consumption_state = "available"
        self._consumption_token: object | None = None
        self.audit = audit
        self.binding = binding
        self.records = records

    def __setattr__(self, name: str, value: object) -> None:
        if hasattr(self, name):
            raise AttributeError("admitted recovery evidence is immutable")
        object.__setattr__(self, name, value)

    def _consume(self) -> tuple[RunBinding, BoundAuditPort, AuditRecoveryRecordSource]:
        token, binding, audit, records = self._reserve_consumption()
        self._commit_consumption(token)
        return binding, audit, records

    def _reserve_consumption(
        self,
    ) -> tuple[object, RunBinding, BoundAuditPort, AuditRecoveryRecordSource]:
        token = object()
        with self._consumption_lock:
            if self._consumption_state != "available":
                raise RunCompositionError("recovery admission was already consumed")
            object.__setattr__(self, "_consumption_state", "assembling")
            object.__setattr__(self, "_consumption_token", token)
        return token, self.binding, self.audit, self.records

    def _commit_consumption(self, token: object) -> None:
        with self._consumption_lock:
            if self._consumption_state != "assembling" or self._consumption_token is not token:
                object.__setattr__(self, "_consumption_state", "failed")
                object.__setattr__(self, "_consumption_token", None)
                raise RunCompositionError("recovery admission reservation changed")
            object.__setattr__(self, "_consumption_state", "committed")
            object.__setattr__(self, "_consumption_token", None)

    def _abort_consumption(self, token: object) -> None:
        with self._consumption_lock:
            if self._consumption_state != "assembling" or self._consumption_token is not token:
                object.__setattr__(self, "_consumption_state", "failed")
                object.__setattr__(self, "_consumption_token", None)
                raise RunCompositionError("recovery admission reservation changed")
            object.__setattr__(self, "_consumption_state", "available")
            object.__setattr__(self, "_consumption_token", None)


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


def verify_run_recovery(
    *,
    preflight: PreflightSession,
    settings: Settings,
    market_data: MarketDataSelection,
    parameters: tuple[EffectiveParameter, ...],
    master_seed: int,
    stream_labels: tuple[str, ...],
    store: LocalResultStore,
    run_id: RunId,
) -> VerifiedIncompleteRecoveryBinding | VerifiedTerminalRecoveryBinding:
    """Rebuild current lineage, acquire the writer lease, and classify one attempt."""
    if (
        type(preflight) is not PreflightSession
        or type(settings) is not Settings
        or type(market_data) is not MarketDataSelection
        or type(store) is not LocalResultStore
        or type(run_id) is not RunId
    ):
        raise RunCompositionError("recovery inputs require exact trusted carriers")
    if type(parameters) is not tuple or any(
        type(parameter) is not EffectiveParameter for parameter in parameters
    ):
        raise RunCompositionError("recovery parameters must be one exact tuple")
    if len({parameter.name for parameter in parameters}) != len(parameters):
        raise RunCompositionError("recovery parameter names must be unique")
    if type(stream_labels) is not tuple:
        raise RunCompositionError("recovery stream labels must be one exact tuple")
    RandomnessSpec(master_seed=master_seed, stream_labels=tuple(sorted(stream_labels)))
    normalized_configuration = NormalizedConfiguration(
        schema_version=settings.schema_version,
        environment=settings.environment.value,
        mode=settings.run.mode.value,
    )
    repository, preflight_commit = _consume_preflight_session(preflight)
    provenance = collect_provenance(repository, repository / "uv.lock")
    if provenance.code.commit != preflight_commit:
        raise RunCompositionError("collector observed a different commit than recovery preflight")
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
    manifest = build_manifest(spec, run_id)
    verify_manifest_evidence(
        manifest,
        code=provenance.code,
        runtime=provenance.runtime,
        data=market_data.fingerprint,
    )
    try:
        return store.verify_recovery_attempt(manifest)
    except Exception as error:
        raise RunCompositionError("existing attempt failed locked recovery verification") from error


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


def admit_recovered_run(
    recovered: RecoveredRun,
    *,
    audit_factory: AuditPortFactory,
) -> AdmittedRecoveredRun:
    """Bind one store-issued incomplete recovery prefix to its reopened journal."""
    if type(recovered) is not RecoveredRun:
        raise RunCompositionError("recovery admission requires an exact RecoveredRun")
    if not callable(audit_factory):
        raise RunCompositionError("recovery audit factory must be callable")
    try:
        reservation = recovered._reserve_for_admission()
    except StoreError as error:
        raise RunCompositionError("recovered run was already admitted") from error
    raw_audit: object | None = None
    try:
        raw_audit = audit_factory(recovered.audit)
        if (
            type(raw_audit) is not PosixAuditJournal
            or raw_audit._authority is not recovered._authority
        ):
            raise RunCompositionError("recovery audit is not the store-bound reopened journal")
        audit = BoundAuditPort(recovered.audit, raw_audit)
        admitted = AdmittedRecoveredRun(
            _RECOVERED_ADMISSION_SEAL,
            recovered=recovered,
            audit=audit,
            records=raw_audit.recovery_records,
        )
        recovered._commit_admission(reservation)
        return admitted
    except BaseException:
        cleanup_error: BaseException | None = None
        if type(raw_audit) is PosixAuditJournal:
            try:
                raw_audit.close()
            except BaseException as caught:
                cleanup_error = caught
        try:
            recovered._abort_admission(reservation, retryable=cleanup_error is None)
        except StoreError as caught:
            raise RunCompositionError("recovery admission state changed during cleanup") from caught
        if cleanup_error is not None:
            raise RunCompositionError("recovery audit cleanup failed") from cleanup_error
        raise
