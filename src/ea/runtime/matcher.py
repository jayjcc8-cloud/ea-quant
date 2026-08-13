"""Runtime-owned dispatch verification adapters for ADR 0018."""

from __future__ import annotations

from types import MappingProxyType
from typing import NamedTuple, Protocol, final
from weakref import WeakKeyDictionary

from ea.core.execution import InstrumentExecutionSpecSet, instrument_spec_set_digest
from ea.core.execution_identity import IngressIdentity, SourceNamespace
from ea.core.historical_matching import (
    HistoricalDispatchKind,
    HistoricalMatcherDescendantBinding,
    canonical_end_of_run_root_bytes,
    historical_end_root_digest,
    historical_market_root_digest,
)
from ea.core.market_data import MarketDataEnvelope
from ea.core.market_data_codec import canonical_market_data_record_bytes
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunId, Sha256Digest
from ea.core.runtime import (
    ActiveEndOfRunDispatchProof,
    ActiveMarketDispatchProof,
    EndOfRunRoot,
    RuntimeOrderingError,
    _create_active_end_of_run_dispatch_proof,
    _create_active_market_dispatch_proof,
    runtime_root_order_key,
)
from ea.core.strategy import _causal_market_digest_from_canonical_bytes
from ea.runtime.historical import Phase1HistoricalMarketRuntime
from ea.runtime.queue import DeterministicRootQueue

_MAX_UINT64 = (1 << 64) - 1


@final
class _DispatchRuntimeIdentity:
    """Capability-free identity witness backed only by this module's private registry."""

    __slots__ = ("__weakref__",)

    def __init__(self) -> None:
        raise TypeError("dispatch runtime identities are created only by their factory")


_DISPATCH_RUNTIME_IDENTITIES: WeakKeyDictionary[
    _DispatchRuntimeIdentity,
    Phase1HistoricalMarketRuntime,
] = WeakKeyDictionary()


class HistoricalMatcherIssuanceCapability(Protocol):
    @property
    def run_id(self) -> RunId: ...

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet: ...

    @property
    def source_namespace(self) -> SourceNamespace: ...

    def has_issued_ingress(
        self,
        *,
        ingress_identity: IngressIdentity,
        canonical_ingress_bytes: bytes,
        canonical_fact_bytes: bytes,
    ) -> bool: ...

    def resolve_descendant_binding(
        self,
        *,
        ingress_identity: IngressIdentity,
        canonical_ingress_bytes: bytes,
        canonical_fact_bytes: bytes,
    ) -> HistoricalMatcherDescendantBinding | None: ...


class DirectFactDispatchVerifier(Protocol):
    @property
    def run_id(self) -> RunId: ...

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet: ...

    def resolve_active_issued_fact_dispatch(
        self,
        *,
        ingress_identity: IngressIdentity,
        canonical_ingress_bytes: bytes,
        canonical_fact_bytes: bytes,
    ) -> int | None: ...


class _CausalDescendantBindings(NamedTuple):
    runtime: Phase1HistoricalMarketRuntime
    matcher: HistoricalMatcherIssuanceCapability
    direct: DeterministicRootQueue
    run_id_value: str
    spec_sha256_value: str
    direct_registry_identity: MappingProxyType[SourceNamespace, object]
    direct_registry_entries: tuple[tuple[str, object], ...]
    direct_source_namespace_values: tuple[str, ...]
    other_source_namespace_values: tuple[str, ...]
    matcher_source_namespace_value: str
    registered_source_namespace_values: tuple[str, ...]


def _fail(code: OutcomeCode, message: str) -> RuntimeOrderingError:
    return RuntimeOrderingError(code, message)


def _require_dispatch_sequence(value: object) -> int:
    if type(value) is not int:
        raise _fail(OutcomeCode.INVALID_TYPE, "dispatch sequence must be exact int")
    if not 1 <= value <= _MAX_UINT64:
        raise _fail(OutcomeCode.OUT_OF_RANGE, "dispatch sequence must be positive uint64")
    return value


@final
class HistoricalMatcherDispatchVerifierAdapter:
    __slots__ = ("_runtime", "_runtime_identity")
    _runtime: Phase1HistoricalMarketRuntime
    _runtime_identity: _DispatchRuntimeIdentity

    def __init__(self) -> None:
        raise TypeError("matcher dispatch verifiers are created only by their factory")

    def _require_bound_runtime(self) -> Phase1HistoricalMarketRuntime:
        try:
            runtime = self._runtime
            identity = self._runtime_identity
            bound_runtime = _DISPATCH_RUNTIME_IDENTITIES.get(identity)
        except (AttributeError, TypeError) as error:
            raise _fail(OutcomeCode.INVALID_TYPE, "dispatch runtime binding is invalid") from error
        if (
            type(runtime) is not Phase1HistoricalMarketRuntime
            or type(identity) is not _DispatchRuntimeIdentity
            or bound_runtime is not runtime
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "dispatch runtime binding changed")
        return runtime

    @property
    def runtime_identity(self) -> object:
        """Return an opaque witness without exposing runtime capabilities."""
        self._require_bound_runtime()
        return self._runtime_identity

    @property
    def run_id(self) -> RunId:
        return self._require_bound_runtime().run_id

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet:
        return self._require_bound_runtime().spec_set

    def verify_active_market_dispatch(
        self,
        market_root: MarketDataEnvelope,
        *,
        dispatch_sequence: int,
    ) -> ActiveMarketDispatchProof:
        if type(market_root) is not MarketDataEnvelope:
            raise _fail(OutcomeCode.INVALID_TYPE, "market root must be exact")
        sequence = _require_dispatch_sequence(dispatch_sequence)
        runtime = self._require_bound_runtime()
        try:
            market_bytes = runtime._require_active_market_dispatch_bytes(
                market_root,
                dispatch_sequence=sequence,
            )
            matcher_digest = historical_market_root_digest(market_root)
            causal_digest = _causal_market_digest_from_canonical_bytes(market_bytes)
            confirmed = runtime._require_active_market_dispatch_bytes(
                market_root,
                dispatch_sequence=sequence,
            )
        except RuntimeOrderingError:
            raise
        except Exception as error:
            raise _fail(OutcomeCode.INVALID_TYPE, "active market verification failed") from error
        if (
            confirmed != market_bytes
            or market_bytes != canonical_market_data_record_bytes(market_root)
            or matcher_digest != historical_market_root_digest(market_root)
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "active market dispatch changed")
        return _create_active_market_dispatch_proof(
            run_id=runtime.run_id,
            market_root=market_root,
            canonical_market_bytes=market_bytes,
            causal_market_sha256=causal_digest,
            dispatch_sequence=sequence,
            issuer=self,
        )

    def verify_active_end_of_run_dispatch(
        self,
        end_root: EndOfRunRoot,
        *,
        dispatch_sequence: int,
    ) -> ActiveEndOfRunDispatchProof:
        if type(end_root) is not EndOfRunRoot:
            raise _fail(OutcomeCode.INVALID_TYPE, "end root must be exact")
        sequence = _require_dispatch_sequence(dispatch_sequence)
        runtime = self._require_bound_runtime()
        try:
            end_bytes = runtime._require_active_end_of_run_dispatch_bytes(
                end_root,
                dispatch_sequence=sequence,
            )
            end_digest = historical_end_root_digest(end_root)
            confirmed = runtime._require_active_end_of_run_dispatch_bytes(
                end_root,
                dispatch_sequence=sequence,
            )
        except RuntimeOrderingError:
            raise
        except Exception as error:
            raise _fail(OutcomeCode.INVALID_TYPE, "active end verification failed") from error
        if confirmed != end_bytes or end_bytes != canonical_end_of_run_root_bytes(end_root):
            raise _fail(OutcomeCode.CONFLICTING_ID, "active end dispatch changed")
        return _create_active_end_of_run_dispatch_proof(
            run_id=runtime.run_id,
            end_root=end_root,
            canonical_end_bytes=end_bytes,
            end_root_sha256=end_digest,
            dispatch_sequence=sequence,
            issuer=self,
        )


def create_historical_matcher_dispatch_verifier(
    runtime: Phase1HistoricalMarketRuntime,
) -> HistoricalMatcherDispatchVerifierAdapter:
    """Bind market/end proof issuance to one exact historical runtime."""
    if type(runtime) is not Phase1HistoricalMarketRuntime:
        raise _fail(OutcomeCode.INVALID_TYPE, "runtime must be exact")
    value = object.__new__(HistoricalMatcherDispatchVerifierAdapter)
    identity = object.__new__(_DispatchRuntimeIdentity)
    _DISPATCH_RUNTIME_IDENTITIES[identity] = runtime
    value._runtime = runtime
    value._runtime_identity = identity
    return value


@final
class HistoricalMatcherDescendantFactDispatchVerifier:
    """Historical-only descendant verifier without an artificial direct-root queue."""

    __slots__ = ("_matcher", "_run_id", "_runtime", "_spec_set", "_spec_sha256")
    _matcher: HistoricalMatcherIssuanceCapability
    _run_id: RunId
    _runtime: Phase1HistoricalMarketRuntime
    _spec_set: InstrumentExecutionSpecSet
    _spec_sha256: Sha256Digest

    def __init__(self) -> None:
        raise TypeError("historical descendant verifiers are created only by their factory")

    @property
    def run_id(self) -> RunId:
        self._require_bindings()
        return self._run_id

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet:
        self._require_bindings()
        return self._spec_set

    def _require_bindings(self) -> None:
        try:
            run_ids = (self._runtime.run_id, self._matcher.run_id, self._run_id)
            specs = (self._runtime.spec_set, self._matcher.spec_set, self._spec_set)
            matcher_source = self._matcher.source_namespace
        except (AttributeError, TypeError) as error:
            raise _fail(
                OutcomeCode.INVALID_TYPE, "historical descendant bindings are invalid"
            ) from error
        if (
            any(type(value) is not RunId for value in run_ids)
            or any(type(value) is not InstrumentExecutionSpecSet for value in specs)
            or type(matcher_source) is not SourceNamespace
            or any(value != self._run_id for value in run_ids)
            or any(instrument_spec_set_digest(value) != self._spec_sha256 for value in specs)
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "historical descendant binding changed")

    def resolve_active_issued_fact_dispatch(
        self,
        *,
        ingress_identity: IngressIdentity,
        canonical_ingress_bytes: bytes,
        canonical_fact_bytes: bytes,
    ) -> int | None:
        if (
            type(ingress_identity) is not IngressIdentity
            or type(canonical_ingress_bytes) is not bytes
            or type(canonical_fact_bytes) is not bytes
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "fact dispatch lookup inputs must be exact")
        self._require_bindings()
        binding = self._matcher.resolve_descendant_binding(
            ingress_identity=ingress_identity,
            canonical_ingress_bytes=canonical_ingress_bytes,
            canonical_fact_bytes=canonical_fact_bytes,
        )
        self._require_bindings()
        if binding is None:
            return None
        if not self._matcher.has_issued_ingress(
            ingress_identity=ingress_identity,
            canonical_ingress_bytes=canonical_ingress_bytes,
            canonical_fact_bytes=canonical_fact_bytes,
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "matcher issuance proof conflicts")
        active = self._runtime.active_lease
        if active is None or active.dispatch_sequence != binding.parent_dispatch_sequence:
            return None
        root = active.root
        if binding.parent_kind is HistoricalDispatchKind.MARKET:
            if (
                type(root) is not MarketDataEnvelope
                or historical_market_root_digest(root) != binding.parent_root_sha256
                or runtime_root_order_key(root) != binding.parent_root_key
            ):
                return None
            self._runtime._require_active_market_dispatch_bytes(
                root,
                dispatch_sequence=binding.parent_dispatch_sequence,
            )
        elif binding.parent_kind is HistoricalDispatchKind.END_OF_RUN:
            if (
                type(root) is not EndOfRunRoot
                or historical_end_root_digest(root) != binding.parent_root_sha256
                or runtime_root_order_key(root) != binding.parent_root_key
            ):
                return None
            self._runtime._require_active_end_of_run_dispatch_bytes(
                root,
                dispatch_sequence=binding.parent_dispatch_sequence,
            )
        else:
            raise _fail(OutcomeCode.CONFLICTING_ID, "descendant parent kind conflicts")
        self._require_bindings()
        return binding.parent_dispatch_sequence


def create_historical_matcher_descendant_fact_dispatch_verifier(
    *,
    runtime: Phase1HistoricalMarketRuntime,
    matcher: HistoricalMatcherIssuanceCapability,
) -> HistoricalMatcherDescendantFactDispatchVerifier:
    """Bind exact matcher descendants directly to one historical runtime."""
    if type(runtime) is not Phase1HistoricalMarketRuntime:
        raise _fail(OutcomeCode.INVALID_TYPE, "runtime must be exact")
    try:
        matcher_run_id = matcher.run_id
        matcher_specs = matcher.spec_set
        matcher_source = matcher.source_namespace
    except (AttributeError, TypeError) as error:
        raise _fail(OutcomeCode.INVALID_TYPE, "matcher issuance surface is incomplete") from error
    spec_sha256 = instrument_spec_set_digest(runtime.spec_set)
    if (
        type(matcher_run_id) is not RunId
        or type(matcher_specs) is not InstrumentExecutionSpecSet
        or type(matcher_source) is not SourceNamespace
        or runtime.run_id != matcher_run_id
        or instrument_spec_set_digest(matcher_specs) != spec_sha256
        or not callable(getattr(matcher, "resolve_descendant_binding", None))
        or not callable(getattr(matcher, "has_issued_ingress", None))
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "historical descendant bindings conflict")
    value = object.__new__(HistoricalMatcherDescendantFactDispatchVerifier)
    value._runtime = runtime
    value._matcher = matcher
    value._run_id = runtime.run_id
    value._spec_set = runtime.spec_set
    value._spec_sha256 = spec_sha256
    value._require_bindings()
    return value


@final
class CausalDescendantFactDispatchVerifier:
    __slots__ = (
        "__weakref__",
        "_direct",
        "_matcher",
        "_run_id",
        "_runtime",
        "_spec_set",
        "_spec_sha256",
    )
    _direct: DeterministicRootQueue
    _matcher: HistoricalMatcherIssuanceCapability
    _run_id: RunId
    _runtime: Phase1HistoricalMarketRuntime
    _spec_set: InstrumentExecutionSpecSet
    _spec_sha256: Sha256Digest

    def __init__(self) -> None:
        raise TypeError("descendant dispatch verifiers are created only by their factory")

    @property
    def run_id(self) -> RunId:
        self._require_bindings()
        return self._run_id

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet:
        self._require_bindings()
        return self._spec_set

    def resolve_active_issued_fact_dispatch(
        self,
        *,
        ingress_identity: IngressIdentity,
        canonical_ingress_bytes: bytes,
        canonical_fact_bytes: bytes,
    ) -> int | None:
        if (
            type(ingress_identity) is not IngressIdentity
            or type(canonical_ingress_bytes) is not bytes
            or type(canonical_fact_bytes) is not bytes
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "fact dispatch lookup inputs must be exact")
        construction = self._require_bindings()
        direct = construction.direct.resolve_active_issued_fact_dispatch(
            ingress_identity=ingress_identity,
            canonical_ingress_bytes=canonical_ingress_bytes,
            canonical_fact_bytes=canonical_fact_bytes,
        )
        self._require_bindings()
        if direct is not None:
            if type(direct) is not int or not 1 <= direct <= _MAX_UINT64:
                raise _fail(OutcomeCode.INVALID_TYPE, "direct verifier result is invalid")
            return direct
        binding = construction.matcher.resolve_descendant_binding(
            ingress_identity=ingress_identity,
            canonical_ingress_bytes=canonical_ingress_bytes,
            canonical_fact_bytes=canonical_fact_bytes,
        )
        self._require_bindings()
        if binding is None:
            return None
        issued = construction.matcher.has_issued_ingress(
            ingress_identity=ingress_identity,
            canonical_ingress_bytes=canonical_ingress_bytes,
            canonical_fact_bytes=canonical_fact_bytes,
        )
        self._require_bindings()
        if type(issued) is not bool or not issued:
            raise _fail(OutcomeCode.CONFLICTING_ID, "matcher issuance proof conflicts")
        active = construction.runtime.active_lease
        if active is None or active.dispatch_sequence != binding.parent_dispatch_sequence:
            return None
        root = active.root
        if binding.parent_kind is HistoricalDispatchKind.MARKET:
            if (
                type(root) is not MarketDataEnvelope
                or historical_market_root_digest(root) != binding.parent_root_sha256
                or runtime_root_order_key(root) != binding.parent_root_key
            ):
                return None
            construction.runtime._require_active_market_dispatch_bytes(
                root,
                dispatch_sequence=binding.parent_dispatch_sequence,
            )
        elif binding.parent_kind is HistoricalDispatchKind.END_OF_RUN:
            if (
                type(root) is not EndOfRunRoot
                or historical_end_root_digest(root) != binding.parent_root_sha256
                or runtime_root_order_key(root) != binding.parent_root_key
            ):
                return None
            construction.runtime._require_active_end_of_run_dispatch_bytes(
                root,
                dispatch_sequence=binding.parent_dispatch_sequence,
            )
        else:
            raise _fail(OutcomeCode.CONFLICTING_ID, "descendant parent kind conflicts")
        self._require_bindings()
        return binding.parent_dispatch_sequence

    def _require_bindings(self) -> _CausalDescendantBindings:
        try:
            construction = _CAUSAL_DESCENDANT_BINDINGS.get(self)
        except (AttributeError, TypeError) as error:
            raise _fail(
                OutcomeCode.INVALID_TYPE, "descendant verifier construction binding is invalid"
            ) from error
        if construction is None:
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "descendant verifier construction binding is missing",
            )
        try:
            runtime_run_id = self._runtime.run_id
            matcher_run_id = self._matcher.run_id
            direct_run_id = self._direct.run_id
            runtime_specs = self._runtime.spec_set
            matcher_specs = self._matcher.spec_set
            direct_specs = self._direct.spec_set
            matcher_source = self._matcher.source_namespace
            direct_registry = self._direct._fact_issuance_verifiers
            direct_registry_entries = tuple(
                (source.value, verifier) for source, verifier in direct_registry.items()
            )
            direct_sources = self._direct.registered_fact_source_namespaces
            direct_source_values = tuple(source.value for source in direct_sources)
            registered_values = (
                *direct_source_values,
                *construction.other_source_namespace_values,
                matcher_source.value,
            )
        except (AttributeError, TypeError) as error:
            raise _fail(
                OutcomeCode.INVALID_TYPE, "descendant verifier bindings are invalid"
            ) from error
        if (
            type(runtime_run_id) is not RunId
            or type(matcher_run_id) is not RunId
            or type(direct_run_id) is not RunId
            or type(runtime_specs) is not InstrumentExecutionSpecSet
            or type(matcher_specs) is not InstrumentExecutionSpecSet
            or type(direct_specs) is not InstrumentExecutionSpecSet
            or type(matcher_source) is not SourceNamespace
            or type(matcher_source.value) is not str
            or type(direct_registry) is not MappingProxyType
            or any(
                type(source) is not SourceNamespace or type(source.value) is not str
                for source in direct_registry
            )
            or type(direct_sources) is not tuple
            or any(
                type(source) is not SourceNamespace or type(source.value) is not str
                for source in direct_sources
            )
            or type(self._run_id) is not RunId
            or type(self._run_id.value) is not str
            or type(self._spec_set) is not InstrumentExecutionSpecSet
            or type(self._spec_sha256) is not Sha256Digest
            or type(self._spec_sha256.value) is not str
            or type(construction) is not _CausalDescendantBindings
            or type(construction.run_id_value) is not str
            or type(construction.spec_sha256_value) is not str
            or type(construction.direct_registry_identity) is not MappingProxyType
            or type(construction.direct_registry_entries) is not tuple
            or any(
                type(entry) is not tuple or len(entry) != 2 or type(entry[0]) is not str
                for entry in construction.direct_registry_entries
            )
            or type(construction.direct_source_namespace_values) is not tuple
            or any(type(value) is not str for value in construction.direct_source_namespace_values)
            or type(construction.other_source_namespace_values) is not tuple
            or any(type(value) is not str for value in construction.other_source_namespace_values)
            or type(construction.matcher_source_namespace_value) is not str
            or type(construction.registered_source_namespace_values) is not tuple
            or any(
                type(value) is not str for value in construction.registered_source_namespace_values
            )
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "descendant verifier binding types changed")
        if (
            self._runtime is not construction.runtime
            or self._matcher is not construction.matcher
            or self._direct is not construction.direct
            or self._run_id is not construction.runtime.run_id
            or self._spec_set is not construction.runtime.spec_set
            or runtime_run_id.value != construction.run_id_value
            or matcher_run_id.value != construction.run_id_value
            or direct_run_id.value != construction.run_id_value
            or self._run_id.value != construction.run_id_value
            or matcher_source.value != construction.matcher_source_namespace_value
            or direct_registry is not construction.direct_registry_identity
            or len(direct_registry_entries) != len(construction.direct_registry_entries)
            or any(
                current_source != expected_source or current_verifier is not expected_verifier
                for (current_source, current_verifier), (
                    expected_source,
                    expected_verifier,
                ) in zip(
                    direct_registry_entries,
                    construction.direct_registry_entries,
                    strict=True,
                )
            )
            or direct_source_values != construction.direct_source_namespace_values
            or registered_values != construction.registered_source_namespace_values
            or len(set(registered_values)) != len(registered_values)
            or self._spec_sha256.value != construction.spec_sha256_value
            or instrument_spec_set_digest(runtime_specs).value != construction.spec_sha256_value
            or instrument_spec_set_digest(matcher_specs).value != construction.spec_sha256_value
            or instrument_spec_set_digest(direct_specs).value != construction.spec_sha256_value
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "descendant verifier binding changed")
        return construction


_CAUSAL_DESCENDANT_BINDINGS: WeakKeyDictionary[
    CausalDescendantFactDispatchVerifier,
    _CausalDescendantBindings,
] = WeakKeyDictionary()


def create_causal_descendant_fact_dispatch_verifier(
    *,
    runtime: Phase1HistoricalMarketRuntime,
    direct_dispatch_verifier: DeterministicRootQueue,
    matcher: HistoricalMatcherIssuanceCapability,
    other_descendant_source_namespaces: tuple[SourceNamespace, ...] = (),
) -> CausalDescendantFactDispatchVerifier:
    """Create one closed direct-plus-descendant dispatch registry."""
    if (
        type(runtime) is not Phase1HistoricalMarketRuntime
        or type(direct_dispatch_verifier) is not DeterministicRootQueue
        or type(other_descendant_source_namespaces) is not tuple
        or any(type(source) is not SourceNamespace for source in other_descendant_source_namespaces)
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "descendant verifier bindings are invalid")
    if (
        type(runtime.run_id) is not RunId
        or type(runtime.spec_set) is not InstrumentExecutionSpecSet
        or type(direct_dispatch_verifier.run_id) is not RunId
        or type(direct_dispatch_verifier.spec_set) is not InstrumentExecutionSpecSet
        or type(matcher.run_id) is not RunId
        or type(matcher.spec_set) is not InstrumentExecutionSpecSet
        or type(matcher.source_namespace) is not SourceNamespace
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "descendant verifier binding types are invalid")
    if (
        not callable(
            getattr(
                direct_dispatch_verifier,
                "resolve_active_issued_fact_dispatch",
                None,
            )
        )
        or not callable(getattr(matcher, "has_issued_ingress", None))
        or not callable(getattr(matcher, "resolve_descendant_binding", None))
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "descendant verifier surface is incomplete")
    try:
        direct_registry = direct_dispatch_verifier._fact_issuance_verifiers
        direct_sources = direct_dispatch_verifier.registered_fact_source_namespaces
        matcher_source = matcher.source_namespace
    except (AttributeError, TypeError) as error:
        raise _fail(OutcomeCode.INVALID_TYPE, "fact source registry is invalid") from error
    if (
        type(direct_registry) is not MappingProxyType
        or any(
            type(source) is not SourceNamespace or type(source.value) is not str
            for source in direct_registry
        )
        or type(direct_sources) is not tuple
        or any(
            type(source) is not SourceNamespace or type(source.value) is not str
            for source in direct_sources
        )
        or any(type(source.value) is not str for source in other_descendant_source_namespaces)
        or type(matcher_source) is not SourceNamespace
        or type(matcher_source.value) is not str
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "fact source registry is invalid")
    direct_source_values = tuple(source.value for source in direct_sources)
    other_source_values = tuple(source.value for source in other_descendant_source_namespaces)
    matcher_source_value = matcher_source.value
    sources = (*direct_sources, *other_descendant_source_namespaces, matcher_source)
    if len(set(sources)) != len(sources):
        raise _fail(OutcomeCode.CONFLICTING_ID, "fact source registry collides")
    expected_digest = instrument_spec_set_digest(runtime.spec_set)
    if (
        runtime.run_id != direct_dispatch_verifier.run_id
        or runtime.run_id != matcher.run_id
        or instrument_spec_set_digest(direct_dispatch_verifier.spec_set) != expected_digest
        or instrument_spec_set_digest(matcher.spec_set) != expected_digest
    ):
        raise _fail(OutcomeCode.CONFLICTING_ID, "descendant verifier bindings conflict")
    value = object.__new__(CausalDescendantFactDispatchVerifier)
    value._runtime = runtime
    value._direct = direct_dispatch_verifier
    value._matcher = matcher
    value._run_id = runtime.run_id
    value._spec_set = runtime.spec_set
    value._spec_sha256 = expected_digest
    _CAUSAL_DESCENDANT_BINDINGS[value] = _CausalDescendantBindings(
        runtime=runtime,
        matcher=matcher,
        direct=direct_dispatch_verifier,
        run_id_value=runtime.run_id.value,
        spec_sha256_value=expected_digest.value,
        direct_registry_identity=direct_registry,
        direct_registry_entries=tuple(
            (source.value, verifier) for source, verifier in direct_registry.items()
        ),
        direct_source_namespace_values=direct_source_values,
        other_source_namespace_values=other_source_values,
        matcher_source_namespace_value=matcher_source_value,
        registered_source_namespace_values=(
            *direct_source_values,
            *other_source_values,
            matcher_source_value,
        ),
    )
    value._require_bindings()
    return value
