"""Canonical bounded runtime roots and deterministic global ordering."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import total_ordering
from itertools import pairwise
from types import MappingProxyType
from typing import TYPE_CHECKING, final

from ea.core.execution_identity import EconomicId, SourceNamespace
from ea.core.execution_messages import (
    ExecutionFactIngress,
    ExecutionFactKind,
)
from ea.core.market_data import (
    MarketDataEnvelope,
    MarketDataKind,
    MarketDataValidationError,
    admission_order_key,
    validate_market_data_batch,
)
from ea.core.outcomes import OutcomeCode
from ea.core.run import RunId, Sha256Digest
from ea.core.time import TimeValidationError, require_utc

if TYPE_CHECKING:
    from ea.core.reconciliation import ReconciliationObservation

_RUNTIME_ERROR_CODES = frozenset(
    {
        OutcomeCode.INVALID_TYPE,
        OutcomeCode.OUT_OF_RANGE,
        OutcomeCode.CONFLICTING_ID,
    }
)

_ACTIVE_MARKET_DISPATCH_PROOF_SEAL = object()
_ACTIVE_END_OF_RUN_DISPATCH_PROOF_SEAL = object()
_RECONCILIATION_OBSERVATION_ROOT_SEAL = object()


class RuntimeOrderingError(ValueError):
    """Closed validation failure for bounded runtime ordering."""

    code: OutcomeCode

    def __init__(self, code: OutcomeCode, message: str) -> None:
        if type(code) is not OutcomeCode or code not in _RUNTIME_ERROR_CODES:
            raise TypeError("runtime ordering errors require an exact supported OutcomeCode")
        self.code = code
        super().__init__(message)


def _fail(code: OutcomeCode, message: str) -> RuntimeOrderingError:
    return RuntimeOrderingError(code, message)


def _require_non_negative_integer(value: object, *, field_name: str) -> int:
    if type(value) is not int:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            f"{field_name} must have exact runtime type int",
        )
    if value < 0:
        raise _fail(
            OutcomeCode.OUT_OF_RANGE,
            f"{field_name} must be non-negative",
        )
    return value


@final
@dataclass(frozen=True, slots=True, init=False)
class ActiveMarketDispatchProof:
    """Opaque process-local proof of one exact live market dispatch."""

    _run_id: RunId
    _market_root: MarketDataEnvelope
    _canonical_market_bytes: bytes
    _causal_market_sha256: Sha256Digest
    _dispatch_sequence: int
    _issuer: object
    _seal: object

    def __init__(self) -> None:
        raise TypeError("ActiveMarketDispatchProof values are created only by the runtime verifier")

    @property
    def run_id(self) -> RunId:
        return self._run_id

    @property
    def market_root(self) -> MarketDataEnvelope:
        return self._market_root

    @property
    def canonical_market_bytes(self) -> bytes:
        return self._canonical_market_bytes

    @property
    def causal_market_sha256(self) -> Sha256Digest:
        return self._causal_market_sha256

    @property
    def dispatch_sequence(self) -> int:
        return self._dispatch_sequence


def _create_active_market_dispatch_proof(
    *,
    run_id: RunId,
    market_root: MarketDataEnvelope,
    canonical_market_bytes: bytes,
    causal_market_sha256: Sha256Digest,
    dispatch_sequence: int,
    issuer: object,
) -> ActiveMarketDispatchProof:
    if (
        type(run_id) is not RunId
        or type(market_root) is not MarketDataEnvelope
        or type(canonical_market_bytes) is not bytes
        or type(causal_market_sha256) is not Sha256Digest
        or type(dispatch_sequence) is not int
        or dispatch_sequence < 1
        or dispatch_sequence > (1 << 64) - 1
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "active market proof inputs are invalid")
    value = object.__new__(ActiveMarketDispatchProof)
    object.__setattr__(value, "_run_id", run_id)
    object.__setattr__(value, "_market_root", market_root)
    object.__setattr__(value, "_canonical_market_bytes", canonical_market_bytes)
    object.__setattr__(value, "_causal_market_sha256", causal_market_sha256)
    object.__setattr__(value, "_dispatch_sequence", dispatch_sequence)
    object.__setattr__(value, "_issuer", issuer)
    object.__setattr__(value, "_seal", _ACTIVE_MARKET_DISPATCH_PROOF_SEAL)
    return value


def _require_active_market_dispatch_proof(
    proof: object,
    *,
    run_id: RunId,
    market_root: MarketDataEnvelope,
    canonical_market_bytes: bytes,
    causal_market_sha256: Sha256Digest,
    dispatch_sequence: int,
    issuer: object,
) -> ActiveMarketDispatchProof:
    if type(proof) is not ActiveMarketDispatchProof:
        raise _fail(OutcomeCode.INVALID_TYPE, "active market verifier returned a non-exact proof")
    try:
        if (
            type(proof._run_id) is not RunId
            or type(proof._market_root) is not MarketDataEnvelope
            or type(proof._canonical_market_bytes) is not bytes
            or type(proof._causal_market_sha256) is not Sha256Digest
            or type(proof._dispatch_sequence) is not int
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "active market proof carriers must be exact")
        matches = (
            proof._seal is _ACTIVE_MARKET_DISPATCH_PROOF_SEAL
            and proof._issuer is issuer
            and proof._run_id == run_id
            and proof._market_root is market_root
            and proof._canonical_market_bytes == canonical_market_bytes
            and proof._causal_market_sha256 == causal_market_sha256
            and 1 <= proof._dispatch_sequence <= (1 << 64) - 1
            and proof._dispatch_sequence == dispatch_sequence
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise _fail(OutcomeCode.INVALID_TYPE, "active market proof carriers are invalid") from error
    if not matches:
        raise _fail(OutcomeCode.CONFLICTING_ID, "active market dispatch proof conflicts")
    return proof


@final
@dataclass(frozen=True, slots=True, init=False)
class ActiveEndOfRunDispatchProof:
    """Opaque process-local proof of one exact live end-of-run dispatch."""

    _run_id: RunId
    _end_root: EndOfRunRoot
    _canonical_end_bytes: bytes
    _end_root_sha256: Sha256Digest
    _dispatch_sequence: int
    _issuer: object
    _seal: object

    def __init__(self) -> None:
        raise TypeError(
            "ActiveEndOfRunDispatchProof values are created only by the runtime verifier"
        )

    @property
    def run_id(self) -> RunId:
        return self._run_id

    @property
    def end_root(self) -> EndOfRunRoot:
        return self._end_root

    @property
    def canonical_end_bytes(self) -> bytes:
        return self._canonical_end_bytes

    @property
    def end_root_sha256(self) -> Sha256Digest:
        return self._end_root_sha256

    @property
    def dispatch_sequence(self) -> int:
        return self._dispatch_sequence


def _create_active_end_of_run_dispatch_proof(
    *,
    run_id: RunId,
    end_root: EndOfRunRoot,
    canonical_end_bytes: bytes,
    end_root_sha256: Sha256Digest,
    dispatch_sequence: int,
    issuer: object,
) -> ActiveEndOfRunDispatchProof:
    if (
        type(run_id) is not RunId
        or type(end_root) is not EndOfRunRoot
        or type(canonical_end_bytes) is not bytes
        or type(end_root_sha256) is not Sha256Digest
        or type(dispatch_sequence) is not int
        or dispatch_sequence < 1
        or dispatch_sequence > (1 << 64) - 1
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "active end proof inputs are invalid")
    value = object.__new__(ActiveEndOfRunDispatchProof)
    object.__setattr__(value, "_run_id", run_id)
    object.__setattr__(value, "_end_root", end_root)
    object.__setattr__(value, "_canonical_end_bytes", canonical_end_bytes)
    object.__setattr__(value, "_end_root_sha256", end_root_sha256)
    object.__setattr__(value, "_dispatch_sequence", dispatch_sequence)
    object.__setattr__(value, "_issuer", issuer)
    object.__setattr__(value, "_seal", _ACTIVE_END_OF_RUN_DISPATCH_PROOF_SEAL)
    return value


def _require_active_end_of_run_dispatch_proof(
    proof: object,
    *,
    run_id: RunId,
    end_root: EndOfRunRoot,
    canonical_end_bytes: bytes,
    end_root_sha256: Sha256Digest,
    dispatch_sequence: int,
    issuer: object,
) -> ActiveEndOfRunDispatchProof:
    if type(proof) is not ActiveEndOfRunDispatchProof:
        raise _fail(OutcomeCode.INVALID_TYPE, "active end verifier returned a non-exact proof")
    try:
        if (
            type(proof._run_id) is not RunId
            or type(proof._end_root) is not EndOfRunRoot
            or type(proof._canonical_end_bytes) is not bytes
            or type(proof._end_root_sha256) is not Sha256Digest
            or type(proof._dispatch_sequence) is not int
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "active end proof carriers must be exact")
        matches = (
            proof._seal is _ACTIVE_END_OF_RUN_DISPATCH_PROOF_SEAL
            and proof._issuer is issuer
            and proof._run_id == run_id
            and proof._end_root is end_root
            and proof._canonical_end_bytes == canonical_end_bytes
            and proof._end_root_sha256 == end_root_sha256
            and 1 <= proof._dispatch_sequence <= (1 << 64) - 1
            and proof._dispatch_sequence == dispatch_sequence
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise _fail(OutcomeCode.INVALID_TYPE, "active end proof carriers are invalid") from error
    if not matches:
        raise _fail(OutcomeCode.CONFLICTING_ID, "active end dispatch proof conflicts")
    return proof


def _require_utc_time(value: object, *, field_name: str) -> datetime:
    if type(value) is not datetime:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            f"{field_name} must have exact runtime type datetime",
        )
    try:
        return require_utc(value, field=field_name)
    except TimeValidationError as exc:
        raise _fail(
            OutcomeCode.OUT_OF_RANGE,
            f"{field_name} must be canonical UTC",
        ) from exc


class RuntimeRootDomain(StrEnum):
    """Closed ADR 0008 independent-root domains."""

    SAFETY = "safety"
    EXECUTION_FACT = "execution_fact"
    RECONCILIATION_OBSERVATION = "reconciliation_observation"
    MARKET_DATA = "market_data"
    TIMER = "timer"
    END_OF_RUN = "end_of_run"


class SafetyKind(StrEnum):
    """Closed safety-root kinds."""

    HALT = "halt"
    FAILURE_CUTOVER = "failure_cutover"
    STOP_CUTOVER = "stop_cutover"


class ReconciliationObservationKind(StrEnum):
    """Reserved reconciliation kinds; payloads remain reconciliation scope."""

    TRADE_DETAIL = "trade_detail"
    ORDER_DETAIL = "order_detail"
    POSITION_SNAPSHOT = "position_snapshot"
    CASH_SNAPSHOT = "cash_snapshot"


class TimerKind(StrEnum):
    """Closed timer-root kinds."""

    SAFETY_DEADLINE = "safety_deadline"
    STRATEGY_TIMER = "strategy_timer"
    MAINTENANCE = "maintenance"


class EndOfRunKind(StrEnum):
    """Closed end-of-run root kinds."""

    BOUNDED_SOURCE_EXHAUSTED = "bounded_source_exhausted"
    REQUESTED_END = "requested_end"


RUNTIME_ROOT_DOMAIN_RANKS: Mapping[RuntimeRootDomain, int] = MappingProxyType(
    {
        RuntimeRootDomain.SAFETY: 0,
        RuntimeRootDomain.EXECUTION_FACT: 10,
        RuntimeRootDomain.RECONCILIATION_OBSERVATION: 20,
        RuntimeRootDomain.MARKET_DATA: 30,
        RuntimeRootDomain.TIMER: 40,
        RuntimeRootDomain.END_OF_RUN: 50,
    }
)
SAFETY_KIND_RANKS: Mapping[SafetyKind, int] = MappingProxyType(
    {
        SafetyKind.HALT: 0,
        SafetyKind.FAILURE_CUTOVER: 10,
        SafetyKind.STOP_CUTOVER: 20,
    }
)
EXECUTION_FACT_KIND_RANKS: Mapping[ExecutionFactKind, int] = MappingProxyType(
    {
        ExecutionFactKind.TRADE: 0,
        ExecutionFactKind.REJECTION: 10,
        ExecutionFactKind.ACKNOWLEDGEMENT: 20,
        ExecutionFactKind.EXPIRY: 30,
        ExecutionFactKind.CANCELLATION: 40,
        ExecutionFactKind.SUBMISSION_QUERY: 50,
    }
)
RECONCILIATION_OBSERVATION_KIND_RANKS: Mapping[ReconciliationObservationKind, int] = (
    MappingProxyType(
        {
            ReconciliationObservationKind.TRADE_DETAIL: 0,
            ReconciliationObservationKind.ORDER_DETAIL: 10,
            ReconciliationObservationKind.POSITION_SNAPSHOT: 20,
            ReconciliationObservationKind.CASH_SNAPSHOT: 30,
        }
    )
)
MARKET_DATA_KIND_RANKS: Mapping[MarketDataKind, int] = MappingProxyType(
    {
        MarketDataKind.BAR: 0,
    }
)
TIMER_KIND_RANKS: Mapping[TimerKind, int] = MappingProxyType(
    {
        TimerKind.SAFETY_DEADLINE: 0,
        TimerKind.STRATEGY_TIMER: 10,
        TimerKind.MAINTENANCE: 20,
    }
)
END_OF_RUN_KIND_RANKS: Mapping[EndOfRunKind, int] = MappingProxyType(
    {
        EndOfRunKind.BOUNDED_SOURCE_EXHAUSTED: 0,
        EndOfRunKind.REQUESTED_END: 10,
    }
)


@final
@dataclass(frozen=True, slots=True)
class RuntimeIdentifier:
    """Canonical visible-ASCII identifier used only by runtime metadata."""

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "runtime identifier must have exact runtime type str",
            )
        if not 1 <= len(self.value) <= 128 or any(
            ord(character) < 0x21 or ord(character) > 0x7E for character in self.value
        ):
            raise _fail(
                OutcomeCode.OUT_OF_RANGE,
                "runtime identifier must contain 1..128 visible ASCII characters",
            )


@final
@dataclass(frozen=True, slots=True)
class SafetySubject:
    """Optional safety-cutover subject without fabricated ancestry."""

    kind: RuntimeIdentifier
    identifier: RuntimeIdentifier

    def __post_init__(self) -> None:
        if type(self.kind) is not RuntimeIdentifier:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "safety subject kind must be an exact RuntimeIdentifier",
            )
        if type(self.identifier) is not RuntimeIdentifier:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "safety subject identifier must be an exact RuntimeIdentifier",
            )


@final
@dataclass(frozen=True, slots=True)
class SafetyRoot:
    """One independently admitted halt or lifecycle cutover."""

    available_at: datetime
    kind: SafetyKind
    producer_namespace: SourceNamespace
    producer_sequence: int
    subject: SafetySubject | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "available_at",
            _require_utc_time(self.available_at, field_name="available_at"),
        )
        if type(self.kind) is not SafetyKind:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "safety kind must be an exact SafetyKind",
            )
        if type(self.producer_namespace) is not SourceNamespace:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "producer namespace must be an exact SourceNamespace",
            )
        _require_non_negative_integer(
            self.producer_sequence,
            field_name="producer_sequence",
        )
        if self.subject is not None and type(self.subject) is not SafetySubject:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "safety subject must be an exact SafetySubject or None",
            )


@final
@dataclass(frozen=True, slots=True)
class TimerRoot:
    """One independently admitted canonical timer occurrence."""

    available_at: datetime
    kind: TimerKind
    timer_namespace: SourceNamespace
    timer_id: RuntimeIdentifier
    producer_sequence: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "available_at",
            _require_utc_time(self.available_at, field_name="available_at"),
        )
        if type(self.kind) is not TimerKind:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "timer kind must be an exact TimerKind",
            )
        if type(self.timer_namespace) is not SourceNamespace:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "timer namespace must be an exact SourceNamespace",
            )
        if type(self.timer_id) is not RuntimeIdentifier:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "timer ID must be an exact RuntimeIdentifier",
            )
        _require_non_negative_integer(
            self.producer_sequence,
            field_name="producer_sequence",
        )


@final
@dataclass(frozen=True, slots=True)
class EndOfRunRoot:
    """One canonical bounded-source or requested end occurrence."""

    available_at: datetime
    kind: EndOfRunKind
    producer_namespace: SourceNamespace
    producer_sequence: int
    run_id: RunId

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "available_at",
            _require_utc_time(self.available_at, field_name="available_at"),
        )
        if type(self.kind) is not EndOfRunKind:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "end-of-run kind must be an exact EndOfRunKind",
            )
        if type(self.producer_namespace) is not SourceNamespace:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "producer namespace must be an exact SourceNamespace",
            )
        _require_non_negative_integer(
            self.producer_sequence,
            field_name="producer_sequence",
        )
        if type(self.run_id) is not RunId:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "end-of-run run_id must be an exact RunId",
            )


@final
@dataclass(frozen=True, slots=True, init=False)
class ReconciliationObservationRoot:
    """Factory-issued rank-20 carrier for one exact reconciliation observation."""

    observation: ReconciliationObservation
    canonical_observation_bytes: bytes
    observation_sha256: Sha256Digest
    _seal: object

    def __init__(self) -> None:
        raise TypeError("reconciliation observation roots are created only by their factory")

    @property
    def available_at(self) -> datetime:
        return self.observation.available_at

    @property
    def kind(self) -> ReconciliationObservationKind:
        return self.observation.kind

    @property
    def source_namespace(self) -> SourceNamespace:
        return self.observation.source_namespace

    @property
    def source_sequence(self) -> int:
        return self.observation.source_sequence

    @property
    def watermark_namespace(self) -> SourceNamespace:
        return self.observation.watermark_namespace

    @property
    def watermark_sequence(self) -> int:
        return self.observation.watermark_sequence

    @property
    def observation_id(self) -> EconomicId:
        return self.observation.observation_id


def create_reconciliation_observation_root(
    observation: ReconciliationObservation,
) -> ReconciliationObservationRoot:
    """Wrap one exact factory-issued observation without duplicating its identity fields."""
    from ea.core.reconciliation import (
        ReconciliationObservation,
        canonical_reconciliation_observation_bytes,
        reconciliation_observation_digest,
    )

    if type(observation) is not ReconciliationObservation:
        raise _fail(OutcomeCode.INVALID_TYPE, "reconciliation root requires an exact observation")
    payload = canonical_reconciliation_observation_bytes(observation)
    value = object.__new__(ReconciliationObservationRoot)
    object.__setattr__(value, "observation", observation)
    object.__setattr__(value, "canonical_observation_bytes", payload)
    object.__setattr__(value, "observation_sha256", reconciliation_observation_digest(observation))
    object.__setattr__(value, "_seal", _RECONCILIATION_OBSERVATION_ROOT_SEAL)
    return value


def _require_reconciliation_observation_root(
    root: object,
) -> ReconciliationObservationRoot:
    from ea.core.reconciliation import (
        ReconciliationObservation,
        canonical_reconciliation_observation_bytes,
        reconciliation_observation_digest,
    )

    if (
        type(root) is not ReconciliationObservationRoot
        or getattr(root, "_seal", None) is not _RECONCILIATION_OBSERVATION_ROOT_SEAL
        or type(root.observation) is not ReconciliationObservation
        or type(root.canonical_observation_bytes) is not bytes
        or type(root.observation_sha256) is not Sha256Digest
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "reconciliation root is not factory-issued")
    if (
        root.canonical_observation_bytes
        != canonical_reconciliation_observation_bytes(root.observation)
        or root.observation_sha256 != reconciliation_observation_digest(root.observation)
    ):
        raise _fail(OutcomeCode.INVALID_TYPE, "reconciliation root evidence conflicts")
    return root


type RuntimeRoot = (
    SafetyRoot
    | ExecutionFactIngress
    | ReconciliationObservationRoot
    | MarketDataEnvelope
    | TimerRoot
    | EndOfRunRoot
)


@dataclass(frozen=True, slots=True, order=True)
class _SafetySuffix:
    kind_rank: int
    producer_namespace: str
    producer_sequence: int
    subject_presence: int
    subject_kind: str
    subject_id: str

    def values(self) -> tuple[object, ...]:
        return (
            self.kind_rank,
            self.producer_namespace,
            self.producer_sequence,
            self.subject_presence,
            self.subject_kind,
            self.subject_id,
        )


@dataclass(frozen=True, slots=True, order=True)
class _ExecutionFactSuffix:
    kind_rank: int
    source_namespace: str
    ingress_sequence: int

    def values(self) -> tuple[object, ...]:
        return (
            self.kind_rank,
            self.source_namespace,
            self.ingress_sequence,
        )


@dataclass(frozen=True, slots=True, order=True)
class _ReconciliationObservationSuffix:
    kind_rank: int
    source_namespace: str
    source_sequence: int
    watermark_namespace: str
    watermark_sequence: int
    observation_run_id: str
    observation_owner_kind: str
    observation_owner_sequence: int

    def values(self) -> tuple[object, ...]:
        return (
            self.kind_rank,
            self.source_namespace,
            self.source_sequence,
            self.watermark_namespace,
            self.watermark_sequence,
            self.observation_run_id,
            self.observation_owner_kind,
            self.observation_owner_sequence,
        )


@dataclass(frozen=True, slots=True, order=True)
class _MarketDataSuffix:
    event_time: datetime
    kind_rank: int
    source_code: str
    source_sequence: int
    instrument_venue: str
    instrument_symbol: str
    interval_start: datetime
    interval_end: datetime
    adjustment: str
    revision: int

    def values(self) -> tuple[object, ...]:
        return (
            self.event_time,
            self.kind_rank,
            self.source_code,
            self.source_sequence,
            self.instrument_venue,
            self.instrument_symbol,
            self.interval_start,
            self.interval_end,
            self.adjustment,
            self.revision,
        )


@dataclass(frozen=True, slots=True, order=True)
class _TimerSuffix:
    kind_rank: int
    timer_namespace: str
    timer_id: str
    producer_sequence: int

    def values(self) -> tuple[object, ...]:
        return (
            self.kind_rank,
            self.timer_namespace,
            self.timer_id,
            self.producer_sequence,
        )


@dataclass(frozen=True, slots=True, order=True)
class _EndOfRunSuffix:
    kind_rank: int
    producer_namespace: str
    producer_sequence: int
    run_id: str

    def values(self) -> tuple[object, ...]:
        return (
            self.kind_rank,
            self.producer_namespace,
            self.producer_sequence,
            self.run_id,
        )


type _RuntimeSuffix = (
    _SafetySuffix
    | _ExecutionFactSuffix
    | _ReconciliationObservationSuffix
    | _MarketDataSuffix
    | _TimerSuffix
    | _EndOfRunSuffix
)


@total_ordering
@final
@dataclass(frozen=True, slots=True)
class RuntimeRootOrderKey:
    """Comparable heterogeneous key that never compares root payload objects."""

    available_at: datetime
    domain_rank: int
    _suffix: _RuntimeSuffix

    def __lt__(self, other: object) -> bool:
        if type(other) is not RuntimeRootOrderKey:
            return NotImplemented
        if self.available_at != other.available_at:
            return self.available_at < other.available_at
        if self.domain_rank != other.domain_rank:
            return self.domain_rank < other.domain_rank
        left = self._suffix
        right = other._suffix
        if type(left) is _SafetySuffix and type(right) is _SafetySuffix:
            return left < right
        if type(left) is _ExecutionFactSuffix and type(right) is _ExecutionFactSuffix:
            return left < right
        if (
            type(left) is _ReconciliationObservationSuffix
            and type(right) is _ReconciliationObservationSuffix
        ):
            return left < right
        if type(left) is _MarketDataSuffix and type(right) is _MarketDataSuffix:
            return left < right
        if type(left) is _TimerSuffix and type(right) is _TimerSuffix:
            return left < right
        if type(left) is _EndOfRunSuffix and type(right) is _EndOfRunSuffix:
            return left < right
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "one runtime domain produced conflicting suffix types",
        )

    def as_tuple(self) -> tuple[object, ...]:
        """Expose the exact literal comparable fields for audit/test evidence."""
        return (self.available_at, self.domain_rank, *self._suffix.values())


def runtime_root_order_key(root: RuntimeRoot) -> RuntimeRootOrderKey:
    """Return the exact ADR 0008/0009 key for one supported root."""
    if type(root) is SafetyRoot:
        subject = root.subject
        suffix = (
            _SafetySuffix(
                kind_rank=SAFETY_KIND_RANKS[root.kind],
                producer_namespace=root.producer_namespace.value,
                producer_sequence=root.producer_sequence,
                subject_presence=0,
                subject_kind="",
                subject_id="",
            )
            if subject is None
            else _SafetySuffix(
                kind_rank=SAFETY_KIND_RANKS[root.kind],
                producer_namespace=root.producer_namespace.value,
                producer_sequence=root.producer_sequence,
                subject_presence=1,
                subject_kind=subject.kind.value,
                subject_id=subject.identifier.value,
            )
        )
        return RuntimeRootOrderKey(
            available_at=root.available_at,
            domain_rank=RUNTIME_ROOT_DOMAIN_RANKS[RuntimeRootDomain.SAFETY],
            _suffix=suffix,
        )
    if type(root) is ExecutionFactIngress:
        return RuntimeRootOrderKey(
            available_at=root.available_at,
            domain_rank=RUNTIME_ROOT_DOMAIN_RANKS[RuntimeRootDomain.EXECUTION_FACT],
            _suffix=_ExecutionFactSuffix(
                kind_rank=EXECUTION_FACT_KIND_RANKS[root.fact.kind],
                source_namespace=root.source_namespace.value,
                ingress_sequence=root.ingress_sequence,
            ),
        )
    if type(root) is ReconciliationObservationRoot:
        root = _require_reconciliation_observation_root(root)
        identifier = root.observation.observation_id
        return RuntimeRootOrderKey(
            available_at=root.available_at,
            domain_rank=RUNTIME_ROOT_DOMAIN_RANKS[RuntimeRootDomain.RECONCILIATION_OBSERVATION],
            _suffix=_ReconciliationObservationSuffix(
                kind_rank=RECONCILIATION_OBSERVATION_KIND_RANKS[root.kind],
                source_namespace=root.source_namespace.value,
                source_sequence=root.source_sequence,
                watermark_namespace=root.watermark_namespace.value,
                watermark_sequence=root.watermark_sequence,
                observation_run_id=identifier.run_id.value,
                observation_owner_kind=identifier.owner_kind.value,
                observation_owner_sequence=identifier.owner_sequence,
            ),
        )
    if type(root) is MarketDataEnvelope:
        market_key = admission_order_key(root)
        return RuntimeRootOrderKey(
            available_at=market_key[0],
            domain_rank=RUNTIME_ROOT_DOMAIN_RANKS[RuntimeRootDomain.MARKET_DATA],
            _suffix=_MarketDataSuffix(*market_key[1:]),
        )
    if type(root) is TimerRoot:
        return RuntimeRootOrderKey(
            available_at=root.available_at,
            domain_rank=RUNTIME_ROOT_DOMAIN_RANKS[RuntimeRootDomain.TIMER],
            _suffix=_TimerSuffix(
                kind_rank=TIMER_KIND_RANKS[root.kind],
                timer_namespace=root.timer_namespace.value,
                timer_id=root.timer_id.value,
                producer_sequence=root.producer_sequence,
            ),
        )
    if type(root) is EndOfRunRoot:
        return RuntimeRootOrderKey(
            available_at=root.available_at,
            domain_rank=RUNTIME_ROOT_DOMAIN_RANKS[RuntimeRootDomain.END_OF_RUN],
            _suffix=_EndOfRunSuffix(
                kind_rank=END_OF_RUN_KIND_RANKS[root.kind],
                producer_namespace=root.producer_namespace.value,
                producer_sequence=root.producer_sequence,
                run_id=root.run_id.value,
            ),
        )
    raise _fail(
        OutcomeCode.INVALID_TYPE,
        "runtime root must have one exact supported concrete type",
    )


def _validate_market_subset(markets: tuple[MarketDataEnvelope, ...]) -> None:
    records: set[object] = set()
    emissions: set[object] = set()
    order_keys: set[object] = set()
    histories: dict[object, list[MarketDataEnvelope]] = {}
    for event in markets:
        if (
            event.record_key in records
            or event.emission_key in emissions
            or admission_order_key(event) in order_keys
        ):
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "market root batch contains a duplicate identity or order key",
            )
        records.add(event.record_key)
        emissions.add(event.emission_key)
        order_keys.add(admission_order_key(event))
        histories.setdefault(event.logical_key, []).append(event)
    for history in histories.values():
        by_revision = sorted(history, key=lambda event: event.revision)
        for earlier, later in pairwise(by_revision):
            if (
                later.available_at < earlier.available_at
                or later.source_sequence <= earlier.source_sequence
            ):
                raise _fail(
                    OutcomeCode.CONFLICTING_ID,
                    "market root revision history conflicts",
                )
    try:
        validate_market_data_batch(markets)
    except MarketDataValidationError as exc:
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "market root batch conflicts with the canonical market contract",
        ) from exc


_PLAN_SEAL = object()


@final
@dataclass(frozen=True, slots=True, init=False)
class BoundedRuntimeRootPlan:
    """Factory-issued non-empty immutable plan in exact global root order."""

    _seal: object
    _roots: tuple[RuntimeRoot, ...]

    def __init__(
        self,
        seal: object,
        roots: tuple[RuntimeRoot, ...],
    ) -> None:
        if seal is not _PLAN_SEAL:
            raise _fail(
                OutcomeCode.INVALID_TYPE,
                "bounded runtime root plans are created only by their factory",
            )
        object.__setattr__(self, "_seal", seal)
        object.__setattr__(self, "_roots", roots)

    @property
    def roots(self) -> tuple[RuntimeRoot, ...]:
        return self._roots

    def __len__(self) -> int:
        return len(self._roots)


def _require_bounded_runtime_root_plan(
    plan: object,
) -> BoundedRuntimeRootPlan:
    if type(plan) is not BoundedRuntimeRootPlan or getattr(plan, "_seal", None) is not _PLAN_SEAL:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "bounded runtime root plan is not factory-issued",
        )
    return plan


def prepare_bounded_runtime_roots(
    roots: Iterable[RuntimeRoot],
) -> BoundedRuntimeRootPlan:
    """Materialize once, reject every collision, and freeze canonical root order."""
    try:
        materialized = tuple(roots)
    except TypeError as exc:
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "runtime roots must be one finite iterable",
        ) from exc
    if not materialized:
        raise _fail(
            OutcomeCode.OUT_OF_RANGE,
            "bounded runtime root plan must not be empty",
        )
    supported_types = (
        SafetyRoot,
        ExecutionFactIngress,
        ReconciliationObservationRoot,
        MarketDataEnvelope,
        TimerRoot,
        EndOfRunRoot,
    )
    if any(type(root) not in supported_types for root in materialized):
        raise _fail(
            OutcomeCode.INVALID_TYPE,
            "runtime root iterable contains an unsupported concrete type",
        )

    markets = tuple(root for root in materialized if type(root) is MarketDataEnvelope)
    _validate_market_subset(markets)

    fact_identities: set[tuple[str, int]] = set()
    reconciliation_identities: set[tuple[str, int]] = set()
    safety_identities: set[tuple[str, int]] = set()
    timer_identities: set[tuple[str, int]] = set()
    end_identities: set[tuple[str, str, int]] = set()
    for root in materialized:
        if type(root) is ExecutionFactIngress:
            fact_identity = (root.source_namespace.value, root.ingress_sequence)
            if fact_identity in fact_identities:
                raise _fail(
                    OutcomeCode.CONFLICTING_ID,
                    "runtime root batch contains a duplicate domain identity",
                )
            fact_identities.add(fact_identity)
        elif type(root) is ReconciliationObservationRoot:
            root = _require_reconciliation_observation_root(root)
            identity = (root.source_namespace.value, root.source_sequence)
            if identity in reconciliation_identities:
                raise _fail(
                    OutcomeCode.CONFLICTING_ID,
                    "runtime root batch contains a duplicate domain identity",
                )
            reconciliation_identities.add(identity)
        elif type(root) is SafetyRoot:
            safety_identity = (
                root.producer_namespace.value,
                root.producer_sequence,
            )
            if safety_identity in safety_identities:
                raise _fail(
                    OutcomeCode.CONFLICTING_ID,
                    "runtime root batch contains a duplicate domain identity",
                )
            safety_identities.add(safety_identity)
        elif type(root) is TimerRoot:
            timer_identity = (root.timer_namespace.value, root.producer_sequence)
            if timer_identity in timer_identities:
                raise _fail(
                    OutcomeCode.CONFLICTING_ID,
                    "runtime root batch contains a duplicate domain identity",
                )
            timer_identities.add(timer_identity)
        elif type(root) is EndOfRunRoot:
            end_identity = (
                root.run_id.value,
                root.producer_namespace.value,
                root.producer_sequence,
            )
            if end_identity in end_identities:
                raise _fail(
                    OutcomeCode.CONFLICTING_ID,
                    "runtime root batch contains a duplicate domain identity",
                )
            end_identities.add(end_identity)

    keyed = tuple((runtime_root_order_key(root), root) for root in materialized)
    keys = tuple(key for key, _root in keyed)
    if len(set(keys)) != len(keys):
        raise _fail(
            OutcomeCode.CONFLICTING_ID,
            "runtime root batch contains a duplicate complete order key",
        )
    ordered = tuple(root for _key, root in sorted(keyed, key=lambda item: item[0]))
    return BoundedRuntimeRootPlan(_PLAN_SEAL, ordered)
