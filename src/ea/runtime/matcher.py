"""Runtime-owned dispatch verification adapters for ADR 0018."""

from __future__ import annotations

from typing import Protocol, final

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
    __slots__ = ("_runtime",)
    _runtime: Phase1HistoricalMarketRuntime

    def __init__(self) -> None:
        raise TypeError("matcher dispatch verifiers are created only by their factory")

    @property
    def run_id(self) -> RunId:
        return self._runtime.run_id

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet:
        return self._runtime.spec_set

    def verify_active_market_dispatch(
        self,
        market_root: MarketDataEnvelope,
        *,
        dispatch_sequence: int,
    ) -> ActiveMarketDispatchProof:
        if type(market_root) is not MarketDataEnvelope:
            raise _fail(OutcomeCode.INVALID_TYPE, "market root must be exact")
        sequence = _require_dispatch_sequence(dispatch_sequence)
        try:
            market_bytes = self._runtime._require_active_market_dispatch_bytes(
                market_root,
                dispatch_sequence=sequence,
            )
            matcher_digest = historical_market_root_digest(market_root)
            causal_digest = _causal_market_digest_from_canonical_bytes(market_bytes)
            confirmed = self._runtime._require_active_market_dispatch_bytes(
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
            run_id=self._runtime.run_id,
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
        try:
            end_bytes = self._runtime._require_active_end_of_run_dispatch_bytes(
                end_root,
                dispatch_sequence=sequence,
            )
            end_digest = historical_end_root_digest(end_root)
            confirmed = self._runtime._require_active_end_of_run_dispatch_bytes(
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
            run_id=self._runtime.run_id,
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
    value._runtime = runtime
    return value


@final
class CausalDescendantFactDispatchVerifier:
    __slots__ = (
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
        return self._run_id

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet:
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
        self._require_bindings()
        direct = self._direct.resolve_active_issued_fact_dispatch(
            ingress_identity=ingress_identity,
            canonical_ingress_bytes=canonical_ingress_bytes,
            canonical_fact_bytes=canonical_fact_bytes,
        )
        if direct is not None:
            if type(direct) is not int or not 1 <= direct <= _MAX_UINT64:
                raise _fail(OutcomeCode.INVALID_TYPE, "direct verifier result is invalid")
            return direct
        binding = self._matcher.resolve_descendant_binding(
            ingress_identity=ingress_identity,
            canonical_ingress_bytes=canonical_ingress_bytes,
            canonical_fact_bytes=canonical_fact_bytes,
        )
        if binding is None:
            return None
        issued = self._matcher.has_issued_ingress(
            ingress_identity=ingress_identity,
            canonical_ingress_bytes=canonical_ingress_bytes,
            canonical_fact_bytes=canonical_fact_bytes,
        )
        if type(issued) is not bool or not issued:
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
        return binding.parent_dispatch_sequence

    def _require_bindings(self) -> None:
        if (
            self._runtime.run_id != self._run_id
            or self._matcher.run_id != self._run_id
            or self._direct.run_id != self._run_id
            or instrument_spec_set_digest(self._runtime.spec_set) != self._spec_sha256
            or instrument_spec_set_digest(self._matcher.spec_set) != self._spec_sha256
            or instrument_spec_set_digest(self._direct.spec_set) != self._spec_sha256
        ):
            raise _fail(OutcomeCode.CONFLICTING_ID, "descendant verifier binding changed")


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
    sources = (
        *direct_dispatch_verifier.registered_fact_source_namespaces,
        *other_descendant_source_namespaces,
        matcher.source_namespace,
    )
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
    value._require_bindings()
    return value
