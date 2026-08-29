"""Fail-closed funded product boundary for the Phase 1 composition root."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from functools import partial
from threading import Lock
from typing import final

from ea.composition.frontier import (
    AcknowledgedLifecycleFrontier,
    create_acknowledged_lifecycle_frontier,
)
from ea.core.audit import (
    AuditRecordKind,
    AuditSubjectKind,
    create_audit_append_acknowledgement,
)
from ea.core.execution import InstrumentExecutionSpecSet
from ea.core.execution_messages import ExecutionPolicyRef
from ea.core.initial_funding import (
    InitialFundingOutcome,
    InitialFundingResult,
    canonical_initial_funding_outcome_bytes,
    initial_funding_outcome_digest,
)
from ea.core.portfolio import PortfolioSnapshot
from ea.core.risk import Phase1RiskPolicy, RiskStateSnapshot
from ea.core.run import RunBinding
from ea.experiments.audit import (
    PosixAuditJournal,
    create_posix_audit_journal,
    reopen_posix_audit_journal,
)
from ea.experiments.binding import BoundAuditPort
from ea.experiments.manifest import LineageSpecV2, RunManifestV2
from ea.experiments.store import (
    LocalResultStore,
    RunIdProvider,
    VerifiedIncompleteRecoveryBinding,
)
from ea.portfolio import (
    Phase1LedgerHandoffAuthority,
    Phase1PortfolioRiskRefreshAuthority,
    create_phase1_ledger_handoff_authority,
    create_phase1_portfolio_risk_refresh_authority,
    create_portfolio_ledger,
)
from ea.risk import Phase1RiskAuthority, create_phase1_risk_authority


class ProductKernelFailureCode(StrEnum):
    """Stable product-boundary failures; never reuse internal outcome enums."""

    FUNDING_NON_POSITIVE = "funding.non_positive"
    FUNDING_NOT_QUANTIZED = "funding.not_quantized"
    FUNDING_CURRENCY_MISMATCH = "funding.currency_mismatch"
    FUNDING_SPEC_MISMATCH = "funding.spec_mismatch"
    FUNDING_CONFLICT = "funding.conflict"
    ECONOMIC_UNRESOLVED_FILL = "economic.unresolved_fill"
    ECONOMIC_DUPLICATE_FACT = "economic.duplicate_fact"
    ECONOMIC_CONFLICTING_FACT = "economic.conflicting_fact"
    ECONOMIC_LEDGER_CONFLICT = "economic.ledger_conflict"
    ECONOMIC_RECONCILIATION_MISMATCH = "economic.reconciliation_mismatch"
    INTEGRITY_AUDIT_INCOMPLETE = "integrity.audit_incomplete"
    INTEGRITY_AUDIT_CORRUPT = "integrity.audit_corrupt"
    INTEGRITY_MANIFEST_DRIFT = "integrity.manifest_drift"
    INTEGRITY_SCENARIO_DRIFT = "integrity.scenario_drift"
    INTEGRITY_RISK_STATE_DRIFT = "integrity.risk_state_drift"
    INTEGRITY_TERMINAL_DRIFT = "integrity.terminal_drift"
    INTEGRITY_UNSUPPORTED_MANIFEST_V1 = "integrity.unsupported_manifest_v1"
    INTEGRITY_UNSUPPORTED_RECOVERY_BOUNDARY = "integrity.unsupported_recovery_boundary"


class ProductKernelError(RuntimeError):
    """One stable failure code, with no mutable capability in the exception."""

    def __init__(self, code: ProductKernelFailureCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@final
class _Phase1ProductKernelHandoff:
    __slots__ = (
        "audit",
        "frontier",
        "journal",
        "ledger",
        "ledger_handoff",
        "lock",
        "retire",
        "risk",
        "risk_refresh",
        "state",
    )

    def __init__(
        self,
        *,
        audit: BoundAuditPort,
        journal: PosixAuditJournal,
        ledger: object,
        ledger_handoff: Phase1LedgerHandoffAuthority,
        risk: Phase1RiskAuthority,
        risk_refresh: Phase1PortfolioRiskRefreshAuthority,
        frontier: AcknowledgedLifecycleFrontier,
        retire: Callable[[], None],
    ) -> None:
        self.audit = audit
        self.journal = journal
        self.ledger = ledger
        self.ledger_handoff = ledger_handoff
        self.risk = risk
        self.risk_refresh = risk_refresh
        self.frontier = frontier
        self.retire = retire
        self.lock = Lock()
        self.state = "AVAILABLE"


@final
class Phase1ProductKernel:
    """Public immutable funded baseline; authorities remain factory-private."""

    __slots__ = ("_binding", "_funding_outcome", "_handoff", "_portfolio_snapshot", "_risk_state")

    def __init__(
        self,
        *,
        binding: RunBinding,
        funding_outcome: InitialFundingOutcome,
        portfolio_snapshot: PortfolioSnapshot,
        risk_state: RiskStateSnapshot,
        handoff: _Phase1ProductKernelHandoff,
    ) -> None:
        object.__setattr__(self, "_binding", binding)
        object.__setattr__(self, "_funding_outcome", funding_outcome)
        object.__setattr__(self, "_portfolio_snapshot", portfolio_snapshot)
        object.__setattr__(self, "_risk_state", risk_state)
        object.__setattr__(self, "_handoff", handoff)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Phase1ProductKernel is immutable")

    @property
    def binding(self) -> RunBinding:
        return self._binding

    @property
    def funding_outcome(self) -> InitialFundingOutcome:
        return self._funding_outcome

    @property
    def portfolio_snapshot(self) -> PortfolioSnapshot:
        return self._portfolio_snapshot

    @property
    def risk_state(self) -> RiskStateSnapshot:
        return self._risk_state


def _consume_phase1_product_kernel[T](
    kernel: Phase1ProductKernel, start: Callable[[_Phase1ProductKernelHandoff], T]
) -> T:
    """Consume the private authority exactly once; failures permanently close it."""
    if type(kernel) is not Phase1ProductKernel or not callable(start):
        raise ProductKernelError(
            ProductKernelFailureCode.INTEGRITY_AUDIT_CORRUPT, "invalid handoff"
        )
    handoff = kernel._handoff
    with handoff.lock:
        if handoff.state != "AVAILABLE":
            raise ProductKernelError(
                ProductKernelFailureCode.INTEGRITY_UNSUPPORTED_RECOVERY_BOUNDARY,
                "funded handoff is unavailable",
            )
        handoff.state = "CONSUMING"
    try:
        result = start(handoff)
    except BaseException:
        with handoff.lock:
            handoff.state = "FAILED"
        try:
            handoff.journal.close()
            handoff.retire()
        except Exception as error:
            raise ProductKernelError(
                ProductKernelFailureCode.INTEGRITY_AUDIT_CORRUPT,
                "failed handoff could not retire its durable attempt",
            ) from error
        raise
    with handoff.lock:
        handoff.state = "CONSUMED"
    return result


def _retire_failed_product_attempt(
    journal: PosixAuditJournal | None, retire: Callable[[], None] | None
) -> None:
    """Close journal and invalidate the store attempt after an unrecoverable factory failure."""
    error: BaseException | None = None
    if journal is not None:
        try:
            journal.close()
        except BaseException as close_error:
            error = close_error
    if retire is not None:
        try:
            retire()
        except BaseException as retirement_error:
            error = retirement_error
    if error is not None:
        raise ProductKernelError(
            ProductKernelFailureCode.INTEGRITY_AUDIT_CORRUPT,
            "failed product attempt could not retire its durable evidence",
        ) from error


def prepare_phase1_product_kernel(
    *,
    store: LocalResultStore,
    spec: LineageSpecV2,
    run_id_provider: RunIdProvider,
    spec_set: InstrumentExecutionSpecSet,
    execution_policy: ExecutionPolicyRef,
    risk_policy: Phase1RiskPolicy,
) -> Phase1ProductKernel:
    """Durably prepare, fund, audit and publish the sealed baseline once."""
    if type(store) is not LocalResultStore or type(spec) is not LineageSpecV2:
        raise ProductKernelError(
            ProductKernelFailureCode.INTEGRITY_MANIFEST_DRIFT, "invalid product input"
        )
    journal: PosixAuditJournal | None = None
    retire: Callable[[], None] | None = None
    try:
        prepared = store.prepare_product(spec, run_id_provider)
        retire = partial(store._retire_product_attempt, prepared.audit)
        manifest = store.verify_manifest(prepared.manifest_verification)
        if type(manifest) is not RunManifestV2 or manifest.spec != spec:
            raise ProductKernelError(
                ProductKernelFailureCode.INTEGRITY_MANIFEST_DRIFT, "manifest drift"
            )
        journal = create_posix_audit_journal(prepared.audit)
        audit = BoundAuditPort(prepared.audit, journal)
        records = journal.recovery_records
        if records.record_count != 1:
            raise ProductKernelError(
                ProductKernelFailureCode.INTEGRITY_AUDIT_INCOMPLETE, "prepared record"
            )
        prepared_acknowledgement = create_audit_append_acknowledgement(records.record_at(0))
        ledger = create_portfolio_ledger(manifest.run_id, spec_set)
        outcome = ledger.apply_initial_funding(
            spec.initial_funding,
            binding=prepared.audit.binding,
            prepared_acknowledgement=prepared_acknowledgement.record_sha256,
        )
        if outcome.result is not InitialFundingResult.APPLIED:
            raise ProductKernelError(
                ProductKernelFailureCode.FUNDING_CONFLICT, "initial funding conflict"
            )
        payload = canonical_initial_funding_outcome_bytes(outcome)
        acknowledgement = audit.append(
            record_kind=AuditRecordKind.PORTFOLIO_INITIAL_FUNDING_OUTCOME,
            subject_kind=AuditSubjectKind.INITIAL_FUNDING_OUTCOME,
            subject_sha256=initial_funding_outcome_digest(outcome),
            canonical_payload=payload,
        )
        if (
            acknowledgement.record_id.owner_sequence != 2
            or journal.recovery_records.record_count != 2
        ):
            raise ProductKernelError(
                ProductKernelFailureCode.INTEGRITY_AUDIT_INCOMPLETE, "funding record"
            )
        risk = create_phase1_risk_authority(
            run_id=manifest.run_id,
            spec_set=spec_set,
            execution_policy=execution_policy,
            policy=risk_policy,
        )
        snapshot = ledger.snapshot
        frontier = create_acknowledged_lifecycle_frontier(
            initial_snapshot=snapshot, initial_risk_state=risk.risk_state
        )
        handoff = _Phase1ProductKernelHandoff(
            audit=audit,
            journal=journal,
            ledger=ledger,
            ledger_handoff=create_phase1_ledger_handoff_authority(
                manifest.run_id, spec_set, ledger
            ),
            risk=risk,
            risk_refresh=create_phase1_portfolio_risk_refresh_authority(
                run_id=manifest.run_id,
                spec_set=spec_set,
                policy_id=risk.policy.policy_id,
                policy_sha256=risk.risk_state.policy_sha256,
            ),
            frontier=frontier,
            retire=retire,
        )
        return Phase1ProductKernel(
            binding=prepared.audit.binding,
            funding_outcome=outcome,
            portfolio_snapshot=snapshot,
            risk_state=risk.risk_state,
            handoff=handoff,
        )
    except ProductKernelError:
        _retire_failed_product_attempt(journal, retire)
        raise
    except Exception as error:
        _retire_failed_product_attempt(journal, retire)
        raise ProductKernelError(
            ProductKernelFailureCode.INTEGRITY_AUDIT_CORRUPT,
            "product preparation failed",
        ) from error


def recover_phase1_product_kernel(
    *,
    store: LocalResultStore,
    expected_manifest: RunManifestV2,
    spec_set: InstrumentExecutionSpecSet,
    execution_policy: ExecutionPolicyRef,
    risk_policy: Phase1RiskPolicy,
) -> Phase1ProductKernel:
    """Rebuild the funded baseline from only the declared prelude prefix."""
    if type(expected_manifest) is not RunManifestV2:
        raise ProductKernelError(
            ProductKernelFailureCode.INTEGRITY_UNSUPPORTED_MANIFEST_V1,
            "product recovery requires manifest v2",
        )
    journal: PosixAuditJournal | None = None
    retire: Callable[[], None] | None = None
    try:
        verified = store.verify_recovery_attempt(expected_manifest)
        if type(verified) is not VerifiedIncompleteRecoveryBinding:
            raise ProductKernelError(
                ProductKernelFailureCode.INTEGRITY_TERMINAL_DRIFT,
                "terminal evidence is outside the funding boundary",
            )
        recovered = store.recover_incomplete_attempt(verified)
        retire = partial(store._retire_product_attempt, recovered.audit)
        if store.verify_manifest(recovered.manifest_verification) != expected_manifest:
            raise ProductKernelError(
                ProductKernelFailureCode.INTEGRITY_MANIFEST_DRIFT, "manifest drift"
            )
        journal = reopen_posix_audit_journal(recovered.audit)
        audit = BoundAuditPort(recovered.audit, journal)
        records = journal.recovery_records
        if records.record_count not in {1, 2}:
            raise ProductKernelError(
                ProductKernelFailureCode.INTEGRITY_UNSUPPORTED_RECOVERY_BOUNDARY,
                "unsupported audit prefix",
            )
        first = records.record_at(0)
        prepared_acknowledgement = create_audit_append_acknowledgement(first)
        ledger = create_portfolio_ledger(expected_manifest.run_id, spec_set)
        outcome = ledger.apply_initial_funding(
            expected_manifest.spec.initial_funding,
            binding=recovered.audit.binding,
            prepared_acknowledgement=prepared_acknowledgement.record_sha256,
        )
        if outcome.result is not InitialFundingResult.APPLIED:
            raise ProductKernelError(ProductKernelFailureCode.FUNDING_CONFLICT, "funding conflict")
        payload = canonical_initial_funding_outcome_bytes(outcome)
        subject = initial_funding_outcome_digest(outcome)
        if records.record_count == 1:
            audit.append(
                record_kind=AuditRecordKind.PORTFOLIO_INITIAL_FUNDING_OUTCOME,
                subject_kind=AuditSubjectKind.INITIAL_FUNDING_OUTCOME,
                subject_sha256=subject,
                canonical_payload=payload,
            )
        else:
            record = records.record_at(1)
            if (
                record.record_kind is not AuditRecordKind.PORTFOLIO_INITIAL_FUNDING_OUTCOME
                or record.subject_kind is not AuditSubjectKind.INITIAL_FUNDING_OUTCOME
                or record.subject_sha256 != subject
                or record.canonical_payload != payload
            ):
                raise ProductKernelError(
                    ProductKernelFailureCode.INTEGRITY_AUDIT_CORRUPT,
                    "funding outcome differs from deterministic replay",
                )
        risk = create_phase1_risk_authority(
            run_id=expected_manifest.run_id,
            spec_set=spec_set,
            execution_policy=execution_policy,
            policy=risk_policy,
        )
        snapshot = ledger.snapshot
        handoff = _Phase1ProductKernelHandoff(
            audit=audit,
            journal=journal,
            ledger=ledger,
            ledger_handoff=create_phase1_ledger_handoff_authority(
                expected_manifest.run_id, spec_set, ledger
            ),
            risk=risk,
            risk_refresh=create_phase1_portfolio_risk_refresh_authority(
                run_id=expected_manifest.run_id,
                spec_set=spec_set,
                policy_id=risk.policy.policy_id,
                policy_sha256=risk.risk_state.policy_sha256,
            ),
            frontier=create_acknowledged_lifecycle_frontier(
                initial_snapshot=snapshot, initial_risk_state=risk.risk_state
            ),
            retire=retire,
        )
        return Phase1ProductKernel(
            binding=recovered.audit.binding,
            funding_outcome=outcome,
            portfolio_snapshot=snapshot,
            risk_state=risk.risk_state,
            handoff=handoff,
        )
    except ProductKernelError:
        _retire_failed_product_attempt(journal, retire)
        raise
    except Exception as error:
        _retire_failed_product_attempt(journal, retire)
        raise ProductKernelError(
            ProductKernelFailureCode.INTEGRITY_AUDIT_CORRUPT,
            "product recovery failed",
        ) from error
