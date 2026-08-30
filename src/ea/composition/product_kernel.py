"""Fail-closed funded Phase 1 product boundary."""

from __future__ import annotations

import weakref
from collections.abc import Callable
from contextlib import suppress
from enum import StrEnum
from functools import partial
from threading import Lock
from typing import Any, final

from ea.composition.frontier import create_acknowledged_lifecycle_frontier
from ea.core.audit import (
    AuditContractError,
    AuditRecordKind,
    AuditSubjectKind,
    create_audit_append_acknowledgement,
)
from ea.core.execution import InstrumentExecutionSpecSet
from ea.core.execution_messages import ExecutionPolicyRef
from ea.core.initial_funding import (
    InitialFundingError,
    InitialFundingOutcome,
    InitialFundingResult,
    canonical_initial_funding_outcome_bytes,
    initial_funding_outcome_digest,
)
from ea.core.portfolio import PortfolioLedgerError, PortfolioSnapshot
from ea.core.risk import Phase1RiskPolicy, RiskStateSnapshot, phase1_risk_policy_digest
from ea.core.run import RunBinding
from ea.experiments.audit import create_posix_audit_journal, reopen_posix_audit_journal
from ea.experiments.binding import BoundAuditPort
from ea.experiments.manifest import LineageSpecV2, RunManifestV2
from ea.experiments.provenance import ProvenanceError, collect_installed_runtime_spec_v2
from ea.experiments.store import (
    CorruptAuditRecoveryError,
    IncompleteAuditRecoveryError,
    LocalResultStore,
    RunIdProvider,
    StoreCollisionError,
    StoreError,
    VerifiedIncompleteRecoveryBinding,
    VerifiedTerminalRecoveryBinding,
)
from ea.portfolio import (
    create_phase1_ledger_handoff_authority,
    create_phase1_portfolio_risk_refresh_authority,
    create_portfolio_ledger,
)
from ea.risk import create_phase1_risk_authority
from ea.risk.authority import RiskAuthorityError


class ProductKernelFailureCode(StrEnum):
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
    def __init__(self, code: ProductKernelFailureCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@final
class _Handoff:
    __slots__ = (
        "audit",
        "journal",
        "ledger",
        "ledger_handoff",
        "risk",
        "risk_refresh",
        "frontier",
        "retire",
        "lock",
        "state",
    )
    journal: Any

    def __init__(
        self,
        *,
        audit: BoundAuditPort,
        journal: Any,
        ledger: object,
        ledger_handoff: object,
        risk: object,
        risk_refresh: object,
        frontier: object,
        retire: Callable[[], None],
    ) -> None:
        self.audit, self.journal, self.ledger = audit, journal, ledger
        self.ledger_handoff, self.risk, self.risk_refresh, self.frontier = (
            ledger_handoff,
            risk,
            risk_refresh,
            frontier,
        )
        self.retire, self.lock, self.state = retire, Lock(), "AVAILABLE"


@final
class Phase1ProductKernel:
    __slots__ = (
        "_binding",
        "_funding_outcome",
        "_portfolio_snapshot",
        "_risk_state",
        "__weakref__",
    )
    _binding: RunBinding
    _funding_outcome: InitialFundingOutcome
    _portfolio_snapshot: PortfolioSnapshot
    _risk_state: RiskStateSnapshot

    def __init__(
        self,
        binding: RunBinding,
        funding: InitialFundingOutcome,
        snapshot: PortfolioSnapshot,
        risk: RiskStateSnapshot,
        handoff: _Handoff,
    ) -> None:
        object.__setattr__(self, "_binding", binding)
        object.__setattr__(self, "_funding_outcome", funding)
        object.__setattr__(self, "_portfolio_snapshot", snapshot)
        object.__setattr__(self, "_risk_state", risk)
        _register_handoff(self, handoff)

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


def _handoff_registry() -> tuple[
    Callable[[Phase1ProductKernel, _Handoff], None],
    Callable[[Phase1ProductKernel], None],
]:
    registry: weakref.WeakKeyDictionary[Phase1ProductKernel, tuple[_Handoff, Any]] = (
        weakref.WeakKeyDictionary()
    )
    def close(handoff: _Handoff) -> None:
        with handoff.lock:
            if handoff.state == "RETIRED":
                return
            handoff.state = "RETIRED"
        try:
            handoff.journal.close()
        finally:
            handoff.retire()

    def register(kernel: Phase1ProductKernel, handoff: _Handoff) -> None:
        registry[kernel] = handoff, weakref.finalize(kernel, close, handoff)

    def retire(kernel: Phase1ProductKernel) -> None:
        value = registry.pop(kernel, None)
        if value is not None:
            handoff, finalizer = value
            finalizer.detach()
            close(handoff)

    return register, retire


_register_handoff, _retire_handoff = _handoff_registry()


def _retire_phase1_product_kernel(kernel: Phase1ProductKernel) -> None:
    if type(kernel) is not Phase1ProductKernel:
        raise ProductKernelError(
            ProductKernelFailureCode.INTEGRITY_AUDIT_CORRUPT, "invalid handoff"
        )
    _retire_handoff(kernel)


def _require_product_boundary(
    spec: LineageSpecV2, execution_policy: ExecutionPolicyRef, risk_policy: Phase1RiskPolicy
) -> None:
    try:
        runtime = collect_installed_runtime_spec_v2()
    except ProvenanceError as error:
        raise ProductKernelError(
            ProductKernelFailureCode.INTEGRITY_MANIFEST_DRIFT,
            "installed runtime provenance cannot be collected",
        ) from error
    if runtime != spec.runtime:
        raise ProductKernelError(
            ProductKernelFailureCode.INTEGRITY_MANIFEST_DRIFT,
            "installed runtime differs from the v2 manifest boundary",
        )
    if (
        execution_policy != spec.execution_policy
        or risk_policy.policy_id != spec.risk_policy_id
        or phase1_risk_policy_digest(risk_policy) != spec.risk_policy_sha256
    ):
        raise ProductKernelError(
            ProductKernelFailureCode.INTEGRITY_RISK_STATE_DRIFT,
            "execution or risk policy differs from the v2 manifest boundary",
        )


def _make_kernel(
    store: LocalResultStore,
    prepared: Any,
    manifest: RunManifestV2,
    spec_set: InstrumentExecutionSpecSet,
    execution_policy: ExecutionPolicyRef,
    risk_policy: Phase1RiskPolicy,
    journal: Any,
) -> Phase1ProductKernel:
    audit = BoundAuditPort(prepared.audit, journal)
    records = journal.recovery_records
    if records.record_count not in {1, 2}:
        raise ProductKernelError(
            ProductKernelFailureCode.INTEGRITY_UNSUPPORTED_RECOVERY_BOUNDARY,
            "unsupported audit prefix",
        )
    ledger = create_portfolio_ledger(manifest.run_id, spec_set)
    outcome = ledger.apply_initial_funding(
        manifest.spec.initial_funding,
        binding=prepared.audit.binding,
        prepared_acknowledgement=create_audit_append_acknowledgement(
            records.record_at(0)
        ).record_sha256,
    )
    if outcome.result is not InitialFundingResult.APPLIED:
        raise ProductKernelError(ProductKernelFailureCode.FUNDING_CONFLICT, "funding conflict")
    payload, subject = (
        canonical_initial_funding_outcome_bytes(outcome),
        initial_funding_outcome_digest(outcome),
    )
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
            or record.subject_sha256 != subject
            or record.canonical_payload != payload
        ):
            raise ProductKernelError(
                ProductKernelFailureCode.INTEGRITY_AUDIT_CORRUPT, "funding replay differs"
            )
    risk = create_phase1_risk_authority(
        run_id=manifest.run_id,
        spec_set=spec_set,
        execution_policy=execution_policy,
        policy=risk_policy,
    )
    snapshot = ledger.snapshot
    return Phase1ProductKernel(
        prepared.audit.binding,
        outcome,
        snapshot,
        risk.risk_state,
        _Handoff(
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
            frontier=create_acknowledged_lifecycle_frontier(
                initial_snapshot=snapshot, initial_risk_state=risk.risk_state
            ),
            retire=partial(store._retire_product_attempt, prepared.audit),
        ),
    )
