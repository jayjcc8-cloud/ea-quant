"""Trusted source-bound issuance for already-decoded execution-fact ingress."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import NoReturn, final

from ea.core.economics import EconomicValidationError
from ea.core.execution import InstrumentExecutionSpecSet, instrument_spec_set_digest
from ea.core.execution_identity import ExecutionIdentityError, IngressIdentity, SourceNamespace
from ea.core.execution_messages import (
    ExecutionFactIngress,
    ExecutionMessageError,
    canonical_execution_fact_bytes,
    canonical_execution_fact_ingress_bytes,
)
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunContractError, RunId
from ea.core.runtime import RuntimeOrderingError
from ea.core.time import TimeValidationError


@dataclass(frozen=True, slots=True)
class _IssuedIngressRecord:
    ingress: ExecutionFactIngress
    ingress_bytes: bytes
    fact_bytes: bytes


@dataclass(frozen=True, slots=True)
class _IngressAuthorityState:
    ingress_index: Mapping[IngressIdentity, _IssuedIngressRecord]
    issued_ingresses: tuple[ExecutionFactIngress, ...]


@final
class Phase1ExecutionFactIngressAuthority:
    """One source-bound non-evicting trusted issuance registry."""

    _run_id: RunId
    _spec_set: InstrumentExecutionSpecSet
    _source_namespace: SourceNamespace
    _state: _IngressAuthorityState

    __slots__ = ("_run_id", "_source_namespace", "_spec_set", "_state")

    def __init__(self) -> None:
        raise TypeError(
            "Phase1ExecutionFactIngressAuthority values are created only by "
            "create_phase1_execution_fact_ingress_authority"
        )

    @property
    def run_id(self) -> RunId:
        return self._run_id

    @property
    def spec_set(self) -> InstrumentExecutionSpecSet:
        return self._spec_set

    @property
    def source_namespace(self) -> SourceNamespace:
        return self._source_namespace

    @property
    def issued_ingresses(self) -> tuple[ExecutionFactIngress, ...]:
        return self._state.issued_ingresses

    def register_ingress(
        self,
        ingress: ExecutionFactIngress,
    ) -> ExecutionFactIngress:
        """Register one exact source-issued canonical ingress exactly once."""
        try:
            if type(ingress) is not ExecutionFactIngress:
                raise RuntimeOrderingError(
                    OutcomeCode.INVALID_TYPE,
                    "ingress must be an exact ExecutionFactIngress",
                )
            if (
                ingress.source_namespace != self._source_namespace
                or ingress.fact.source_namespace != self._source_namespace
            ):
                raise RuntimeOrderingError(
                    OutcomeCode.CONFLICTING_ID,
                    "ingress source conflicts with the authority source binding",
                )
            if ingress.available_at < ingress.fact.occurred_at:
                raise RuntimeOrderingError(
                    OutcomeCode.OUT_OF_RANGE,
                    "available_at cannot precede occurred_at",
                )
            ingress_bytes = canonical_execution_fact_ingress_bytes(ingress)
            fact_bytes = canonical_execution_fact_bytes(ingress.fact)
            identity = ingress.identity
            existing = self._state.ingress_index.get(identity)
            if existing is not None:
                if existing.ingress_bytes == ingress_bytes and existing.fact_bytes == fact_bytes:
                    return existing.ingress
                raise RuntimeOrderingError(
                    OutcomeCode.CONFLICTING_ID,
                    "issued ingress identity is occupied by conflicting canonical bytes",
                )
            record = _IssuedIngressRecord(
                ingress=ingress,
                ingress_bytes=ingress_bytes,
                fact_bytes=fact_bytes,
            )
            index = dict(self._state.ingress_index)
            index[identity] = record
            next_state = _IngressAuthorityState(
                ingress_index=MappingProxyType(dict(index)),
                issued_ingresses=(*self._state.issued_ingresses, ingress),
            )
            _preflight_state(next_state, identity=identity, record=record)
            self._state = next_state
            return ingress
        except RuntimeOrderingError:
            raise
        except (
            EconomicValidationError,
            ExecutionIdentityError,
            ExecutionMessageError,
            RunContractError,
            TimeValidationError,
        ) as error:
            _raise_runtime(error)
        except (AttributeError, TypeError) as error:
            raise RuntimeOrderingError(
                OutcomeCode.INVALID_TYPE,
                "ingress carrier is structurally incomplete",
            ) from error

    def has_issued_ingress(
        self,
        *,
        ingress_identity: IngressIdentity,
        canonical_ingress_bytes: bytes,
        canonical_fact_bytes: bytes,
    ) -> bool:
        """Return exact immutable registry membership without mutation."""
        if (
            type(ingress_identity) is not IngressIdentity
            or type(canonical_ingress_bytes) is not bytes
            or type(canonical_fact_bytes) is not bytes
        ):
            raise RuntimeOrderingError(
                OutcomeCode.INVALID_TYPE,
                "issuance lookup requires exact identity and bytes",
            )
        record = self._state.ingress_index.get(ingress_identity)
        return (
            record is not None
            and record.ingress_bytes == canonical_ingress_bytes
            and record.fact_bytes == canonical_fact_bytes
        )


def create_phase1_execution_fact_ingress_authority(
    *,
    run_id: RunId,
    spec_set: InstrumentExecutionSpecSet,
    source_namespace: SourceNamespace,
) -> Phase1ExecutionFactIngressAuthority:
    """Create one empty run/spec/source-bound issuance authority."""
    try:
        if type(run_id) is not RunId:
            raise RuntimeOrderingError(
                OutcomeCode.INVALID_TYPE,
                "run_id must be an exact RunId",
            )
        if type(spec_set) is not InstrumentExecutionSpecSet:
            raise RuntimeOrderingError(
                OutcomeCode.INVALID_TYPE,
                "spec_set must be an exact InstrumentExecutionSpecSet",
            )
        if type(source_namespace) is not SourceNamespace:
            raise RuntimeOrderingError(
                OutcomeCode.INVALID_TYPE,
                "source_namespace must be an exact SourceNamespace",
            )
        instrument_spec_set_digest(spec_set)
        authority = object.__new__(Phase1ExecutionFactIngressAuthority)
        authority._run_id = run_id
        authority._spec_set = spec_set
        authority._source_namespace = source_namespace
        authority._state = _IngressAuthorityState(
            ingress_index=MappingProxyType({}),
            issued_ingresses=(),
        )
        return authority
    except RuntimeOrderingError:
        raise
    except (
        EconomicValidationError,
        ExecutionIdentityError,
        ExecutionMessageError,
        RunContractError,
        TimeValidationError,
    ) as error:
        _raise_runtime(error)
    except (AttributeError, TypeError) as error:
        raise RuntimeOrderingError(
            OutcomeCode.INVALID_TYPE,
            "issuance-authority construction carrier is structurally incomplete",
        ) from error


def _preflight_state(
    state: _IngressAuthorityState,
    *,
    identity: IngressIdentity,
    record: _IssuedIngressRecord,
) -> None:
    if (
        state.ingress_index.get(identity) is not record
        or not state.issued_ingresses
        or state.issued_ingresses[-1] is not record.ingress
        or canonical_execution_fact_ingress_bytes(record.ingress) != record.ingress_bytes
        or canonical_execution_fact_bytes(record.ingress.fact) != record.fact_bytes
    ):
        raise AssertionError("candidate issuance state failed canonical preflight")


def _raise_runtime(error: object) -> NoReturn:
    code = getattr(error, "code", None)
    if type(code) is OutcomeCode:
        if code is OutcomeCode.INVALID_TYPE:
            raise RuntimeOrderingError(OutcomeCode.INVALID_TYPE, str(error)) from (
                error if isinstance(error, BaseException) else None
            )
        if code is OutcomeCode.CONFLICTING_ID:
            raise RuntimeOrderingError(OutcomeCode.CONFLICTING_ID, str(error)) from (
                error if isinstance(error, BaseException) else None
            )
        raise RuntimeOrderingError(OutcomeCode.OUT_OF_RANGE, str(error)) from (
            error if isinstance(error, BaseException) else None
        )
    raise AssertionError("unexpected validation outcome at issuance boundary") from (
        error if isinstance(error, BaseException) else None
    )
