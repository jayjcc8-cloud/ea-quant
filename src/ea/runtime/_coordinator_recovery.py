"""Serialized Phase 1 historical lifecycle coordinator from Accepted ADR 0020."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, cast

from ea.core.audit import (
    AuditAppendAcknowledgement,
    AuditAppendPort,
    AuditContractError,
    AuditLogicalKey,
    AuditRecord,
    AuditRecordKind,
    AuditRecoveryRecordSource,
    AuditSubjectKind,
    audit_append_acknowledgement_digest,
    audit_chain_head,
    audit_record_digest,
    audit_subject_digest,
    canonical_run_prepared_audit_payload,
    create_audit_append_acknowledgement,
    ordered_digest_tuple,
    require_audit_acknowledgement,
)
from ea.core.execution_identity import EconomicId, EconomicOwnerKind
from ea.core.execution_messages import (
    Order,
    execution_fact_ingress_digest,
)
from ea.core.execution_state import (
    ExecutionFactProcessingOutcome,
    canonical_execution_fact_processing_outcome_bytes,
    execution_fact_processing_outcome_digest,
)
from ea.core.historical_matching import (
    HISTORICAL_MATCHER_END_ROOT_DIGEST_DOMAIN,
    HISTORICAL_MATCHER_MARKET_ROOT_DIGEST_DOMAIN,
    HistoricalDispatchKind,
    HistoricalSubmissionReceipt,
    historical_end_root_digest,
    historical_market_root_digest,
    historical_matcher_dispatch_batch_digest,
)
from ea.core.ledger_integration import (
    LedgerHandoffAction,
    canonical_ledger_handoff_outcome_bytes,
    canonical_portfolio_risk_refresh_bytes,
    decode_portfolio_risk_refresh,
    portfolio_risk_refresh_digest,
)
from ea.core.lifecycle import (
    ORDERED_INGRESS_DIGEST_DOMAIN,
    ORDERED_LEDGER_ACK_DIGEST_DOMAIN,
    ORDERED_OUTCOME_ACK_DIGEST_DOMAIN,
    CoordinatorPhase,
    CoordinatorRunState,
    LifecycleError,
    RuntimeDispatchLeaseView,
    RuntimeLifecyclePort,
    SubmissionAuthorizationAttemptOutcome,
    SubmissionAuthorizationAttemptStatus,
    canonical_failing_safety_audit_payload,
    canonical_matcher_batch_audit_payload,
    coordinator_run_state_digest,
    create_audited_execution_fact_handoff,
    create_coordinator_run_state,
    create_submission_authorization_attempt_outcome,
    decode_submission_authorization_attempt_outcome_document,
    dispatch_completed_subject_digest,
)
from ea.core.market_data import MarketDataEnvelope
from ea.core.outcomes import OutcomeCode
from ea.core.portfolio import (
    portfolio_snapshot_digest,
)
from ea.core.risk import RiskHaltReason, risk_state_snapshot_digest
from ea.core.run import RunBinding, Sha256Digest
from ea.core.runtime import EndOfRunRoot, RuntimeRoot, RuntimeRootOrderKey
from ea.runtime._coordinator_read_only_recovery import (
    _decode_read_only_trace_root,
    _require_read_only_recovery_order,
    _retain_read_only_recovery_record,
    recover_read_only_dispatch,
)
from ea.runtime.historical import (
    HISTORICAL_RUNTIME_TRACE_SCHEMA,
    historical_runtime_trace_digest,
)

if TYPE_CHECKING:
    from ea.runtime.coordinator import (
        Phase1HistoricalLifecycleCoordinator,
        _ActiveDispatch,
        _RecoveredDispatch,
    )

_MAX_UINT64 = (1 << 64) - 1


def _require_recovery_records(
    binding: RunBinding,
    records: AuditRecoveryRecordSource | tuple[AuditRecord, ...],
    *,
    audit: AuditAppendPort | None,
) -> Iterator[tuple[AuditRecord, AuditAppendAcknowledgement]]:
    expected_count = _recovery_record_count(records)
    if expected_count < 1:
        raise LifecycleError(OutcomeCode.INVALID_TYPE, "recovery records must be non-empty")
    previous_record_sha256 = None
    previous_chain_head_sha256 = None
    observed_count = 0
    for sequence, record in enumerate(records, start=1):
        observed_count = sequence
        if type(record) is not AuditRecord or record.binding != binding:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovery record binding conflicts")
        if record.record_id.owner_sequence != sequence:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovery record sequence conflicts")
        if sequence > 1 and (
            record.previous_record_sha256 != previous_record_sha256
            or record.previous_chain_head_sha256 != previous_chain_head_sha256
        ):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovery audit chain conflicts")
        if audit is None:
            acknowledgement = create_audit_append_acknowledgement(record)
        else:
            try:
                acknowledgement = audit.append(
                    record_kind=record.record_kind,
                    subject_kind=record.subject_kind,
                    subject_sha256=record.subject_sha256,
                    canonical_payload=record.canonical_payload,
                )
                require_audit_acknowledgement(
                    acknowledgement,
                    binding=binding,
                    logical_key=record.logical_key,
                    canonical_payload=record.canonical_payload,
                )
            except AuditContractError as error:
                raise LifecycleError(
                    error.code,
                    "recovery audit acknowledgement could not be reconfirmed",
                ) from error
        if sequence == 1 and (
            record.record_kind is not AuditRecordKind.RUN_PREPARED
            or record.canonical_payload != canonical_run_prepared_audit_payload(binding)
        ):
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "recovery preparation record conflicts",
            )
        if record.record_kind is AuditRecordKind.RUN_TERMINAL:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "terminal journal must use read-only terminal recovery",
            )
        yield record, acknowledgement
        previous_record_sha256 = audit_record_digest(record)
        previous_chain_head_sha256 = audit_chain_head(record)
    if observed_count != expected_count:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "recovery record source count changed",
        )


def _recovery_record_count(
    records: AuditRecoveryRecordSource | tuple[AuditRecord, ...],
) -> int:
    if isinstance(records, tuple):
        return len(records)
    try:
        count = records.record_count
        binding = records.binding
    except AttributeError as error:
        raise LifecycleError(
            OutcomeCode.INVALID_TYPE,
            "recovery records require one repeatable source",
        ) from error
    if type(count) is not int or count < 1 or type(binding) is not RunBinding:
        raise LifecycleError(OutcomeCode.INVALID_TYPE, "recovery record source is invalid")
    return count


def _recovery_record_at(
    records: AuditRecoveryRecordSource | tuple[AuditRecord, ...],
    index: int,
) -> AuditRecord:
    if isinstance(records, tuple):
        return records[index]
    try:
        return records.record_at(index)
    except (AttributeError, AuditContractError) as error:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "recovery record could not be resolved",
        ) from error


def _recovery_record_prefix(
    records: AuditRecoveryRecordSource | tuple[AuditRecord, ...],
    record_count: int,
) -> AuditRecoveryRecordSource | tuple[AuditRecord, ...]:
    if isinstance(records, tuple):
        return records[:record_count]
    try:
        return records.prefix(record_count)
    except (AttributeError, AuditContractError) as error:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "recovery record prefix could not be resolved",
        ) from error


def _record_document(record: AuditRecord) -> dict[str, object]:
    try:
        document = json.loads(record.canonical_payload)
    except (TypeError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovery payload is not JSON") from error
    if type(document) is not dict:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovery payload must be one object")
    return document


def _group_recovery_records(
    records: Iterator[tuple[AuditRecord, AuditAppendAcknowledgement]],
) -> Iterator[_RecoveredDispatch]:
    from ea.runtime.coordinator import _RecoveredDispatch

    current: _RecoveredDispatch | None = None
    for position, (record, acknowledgement) in enumerate(records, start=2):
        kind = record.record_kind
        document = _record_document(record)
        sequence_value = document.get(
            "runtime_dispatch_sequence"
            if kind is AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME
            else "dispatch_sequence"
        )
        if type(sequence_value) is not int or not 1 <= sequence_value <= _MAX_UINT64:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "recovery dispatch sequence is invalid",
            )
        if current is None:
            current = _RecoveredDispatch(sequence_value)
        elif sequence_value != current.sequence:
            if sequence_value != current.sequence + 1 or current.completion_record is None:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "recovery dispatches are physically interleaved",
                )
            _require_recovery_stage_order(current)
            yield current
            current = _RecoveredDispatch(sequence_value)
        group = current
        if kind is AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION:
            if group.authorization_records:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "duplicate recovered authorization",
                )
            group.authorization_records.append((position, record, acknowledgement))
            continue
        trigger_value = document.get("trigger_root_sha256")
        if trigger_value is not None:
            try:
                trigger_sha256 = Sha256Digest(cast(str, trigger_value))
            except (TypeError, ValueError) as error:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "recovery trigger digest is invalid",
                ) from error
            if group.trigger_sha256 is not None and group.trigger_sha256 != trigger_sha256:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "recovery trigger digest conflicts",
                )
            group.trigger_sha256 = trigger_sha256
        retained = (position, record, acknowledgement)
        if kind is AuditRecordKind.MATCHER_DISPATCH_BATCH:
            if group.batch_record is not None:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "duplicate recovered batch")
            group.batch_record = retained
        elif kind is AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME:
            if record.subject_sha256 in group.outcome_records:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "duplicate recovered outcome")
            group.outcome_records[record.subject_sha256] = retained
        elif kind is AuditRecordKind.RECONCILIATION_OBSERVATION_OUTCOME:
            _retain_read_only_recovery_record(group, retained)
        elif kind is AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION:
            if group.failing_record is not None:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "duplicate failing transition")
            group.failing_record = retained
        elif kind is AuditRecordKind.RUNTIME_DISPATCH_COMPLETED:
            if group.completion_record is not None:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "duplicate recovered completion")
            group.completion_record = retained
        elif kind is AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME:
            subjects = {entry[1].subject_sha256 for entry in group.ledger_records}
            if record.subject_sha256 in subjects:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "duplicate recovered ledger outcome",
                )
            group.ledger_records.append(retained)
        elif kind is AuditRecordKind.RISK_PORTFOLIO_REFRESH:
            if group.refresh_record is not None:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "duplicate recovered refresh")
            group.refresh_record = retained
        else:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "unsupported recovery record kind")
    if current is not None:
        _require_recovery_stage_order(current)
        yield current


def _recovered_failed_logical_key(record: AuditRecord) -> AuditLogicalKey:
    document = _record_document(record)
    try:
        return AuditLogicalKey(
            AuditRecordKind(cast(str, document["failed_record_kind"])),
            AuditSubjectKind(cast(str, document["failed_subject_kind"])),
            Sha256Digest(cast(str, document["failed_subject_sha256"])),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "failing recovery key is invalid",
        ) from error


def _recover_ledger_frontier(
    coordinator: Phase1HistoricalLifecycleCoordinator,
    active: _ActiveDispatch,
    recovered: _RecoveredDispatch,
) -> None:
    from ea.runtime.coordinator import _frontier_state, _require_exact_ack

    assert coordinator._ledger_handoff_authority is not None
    assert coordinator._risk_authority is not None
    assert coordinator._risk_refresh_authority is not None
    assert coordinator._frontier is not None
    active.prior_frontier = _frontier_state(coordinator._frontier)
    ledger_records = recovered.ledger_records
    ledger_record_index = 0
    failed_key = (
        None
        if recovered.failing_record is None
        else _recovered_failed_logical_key(recovered.failing_record[1])
    )
    if recovered.refresh_record is None and (
        recovered.authorization_records
        or (
            failed_key is not None
            and failed_key.record_kind is AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION
        )
    ):
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "recovery authorization stage order conflicts",
        )
    active.ledger_outcomes = [None] * len(active.handoffs)
    active.ledger_acks = [None] * len(active.handoffs)
    halt_required = False
    for index, (handoff, outcome) in enumerate(zip(active.handoffs, active.outcomes, strict=True)):
        if handoff is None or outcome is None:
            if ledger_record_index != len(ledger_records):
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "recovered ledger record prefix conflicts",
                )
            break
        fill = None
        if handoff.fill_id is not None:
            assert handoff.fill_sha256 is not None
            fill = coordinator._resolver.resolve_fill(
                fill_id=handoff.fill_id,
                fill_sha256=handoff.fill_sha256,
            )
        result = coordinator._ledger_handoff_authority.apply_handoff(
            handoff=handoff,
            outcome=outcome,
            fill=fill,
        )
        payload = canonical_ledger_handoff_outcome_bytes(result)
        key = AuditLogicalKey(
            AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
            AuditSubjectKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
            audit_subject_digest(AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME, payload),
        )
        entry = None
        if ledger_record_index < len(ledger_records):
            candidate = ledger_records[ledger_record_index]
            if candidate[1].logical_key != key:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "recovered ledger record prefix conflicts",
                )
            entry = candidate
            ledger_record_index += 1
        if entry is not None:
            _position, ledger_record, ledger_ack = entry
            if ledger_record.canonical_payload != payload:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "recovered ledger outcome payload conflicts",
                )
            _require_exact_ack(
                ledger_ack,
                binding=coordinator._binding,
                key=key,
                payload=payload,
            )
            active.ledger_acks[index] = ledger_ack
        active.ledger_outcomes[index] = result
        active.ledger_snapshot = coordinator._ledger_handoff_authority.snapshot
        if (
            outcome.halt_requested
            or result.requires_reconciliation
            or result.action is LedgerHandoffAction.FAILED
        ):
            halt_required = True
    if ledger_record_index != len(ledger_records):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "orphan recovered ledger outcome")
    if halt_required:
        batch = active.batch
        if batch is None:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovered ledger batch is missing")
        active.risk_state = coordinator._risk_authority.engage_halt(
            RiskHaltReason.RECONCILIATION_REQUIRED,
            causal_root_available_at=batch.trigger_root_key.available_at,
            dispatch_sequence=recovered.sequence,
        )
    elif active.risk_state is None:
        active.risk_state = coordinator._risk_authority.risk_state
    ledger_acks = tuple(value for value in active.ledger_acks if value is not None)
    complete_ledger_frontier = len(ledger_acks) == len(active.handoffs) and all(
        value is not None for value in active.handoffs
    )
    refresh_entry = recovered.refresh_record
    reconstruct_failed_refresh = (
        refresh_entry is None
        and failed_key is not None
        and failed_key.record_kind is AuditRecordKind.RISK_PORTFOLIO_REFRESH
    )
    if refresh_entry is not None or reconstruct_failed_refresh:
        if not complete_ledger_frontier:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "recovered refresh lacks the ordered ledger frontier",
            )
        retained_submission_permitted = None
        if (
            refresh_entry is not None
            and recovered.failing_record is not None
            and recovered.failing_record[0] < refresh_entry[0]
        ):
            retained_submission_permitted = False
            if failed_key == refresh_entry[1].logical_key:
                retained_submission_permitted = decode_portfolio_risk_refresh(
                    refresh_entry[1].canonical_payload
                ).submission_permitted
        snapshot = coordinator._ledger_handoff_authority.snapshot
        risk_state = coordinator._risk_authority.risk_state
        refresh_frontier_sha256 = ordered_digest_tuple(
            ORDERED_LEDGER_ACK_DIGEST_DOMAIN,
            tuple(audit_append_acknowledgement_digest(value) for value in ledger_acks),
        )
        refresh = coordinator._risk_refresh_authority.create_refresh(
            snapshot=snapshot,
            risk_state=risk_state,
            dispatch_sequence=recovered.sequence,
            ordered_ledger_ack_frontier_sha256=refresh_frontier_sha256,
            coordinator_running=(
                retained_submission_permitted
                if retained_submission_permitted is not None
                else coordinator._state.phase
                in {CoordinatorPhase.ADMITTED, CoordinatorPhase.RUNNING}
                and active.batch is not None
                and active.batch.dispatch_kind is not HistoricalDispatchKind.END_OF_RUN
            ),
            publication_window_clear=True,
            candidate_matches_internal=True,
        )
        payload = canonical_portfolio_risk_refresh_bytes(refresh)
        subject = audit_subject_digest(AuditRecordKind.RISK_PORTFOLIO_REFRESH, payload)
        active.ledger_snapshot = snapshot
        active.risk_state = risk_state
        active.refresh = refresh
        active.refresh_sha256 = subject
        active.refresh_value_sha256 = portfolio_risk_refresh_digest(refresh)
        if refresh_entry is None:
            if (
                failed_key is None
                or failed_key.subject_kind is not AuditSubjectKind.PORTFOLIO_RISK_REFRESH
                or failed_key.subject_sha256 != subject
            ):
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "failed refresh key conflicts")
        else:
            _position, refresh_record, refresh_ack = refresh_entry
            key = AuditLogicalKey(
                AuditRecordKind.RISK_PORTFOLIO_REFRESH,
                AuditSubjectKind.PORTFOLIO_RISK_REFRESH,
                subject,
            )
            if refresh_record.canonical_payload != payload:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "recovered refresh payload conflicts",
                )
            _require_exact_ack(
                refresh_ack,
                binding=coordinator._binding,
                key=key,
                payload=payload,
            )
            active.refresh_ack = refresh_ack
            coordinator._publish_retained_refresh(active, coordinator._frontier)
            coordinator._rebind_economic_authorities(active)
            active.final_portfolio_snapshot_sha256 = portfolio_snapshot_digest(
                coordinator._frontier.current_snapshot()
            )
            active.final_risk_state_sha256 = risk_state_snapshot_digest(
                coordinator._frontier.current_state()
            )


def _require_recovery_stage_order(group: _RecoveredDispatch) -> None:
    if group.read_only_outcome_record is not None:
        _require_read_only_recovery_order(group)
        return
    positions = [
        position
        for entry in (
            group.batch_record,
            group.failing_record,
            group.completion_record,
        )
        if entry is not None
        for position in (entry[0],)
    ]
    positions.extend(entry[0] for entry in group.outcome_records.values())
    positions.extend(entry[0] for entry in group.authorization_records)
    if not positions:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovery dispatch is empty")
    authorization_positions = [entry[0] for entry in group.authorization_records]
    if group.authorization_records:
        authorization_position = group.authorization_records[0][0]
        inbound_positions = [
            entry[0]
            for entry in (group.batch_record, *group.outcome_records.values())
            if entry is not None
        ]
        if group.batch_record is None or any(
            position >= authorization_position for position in inbound_positions
        ):
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "recovery authorization stage order conflicts",
            )
    if (
        group.failing_record is not None
        and _recovered_failed_logical_key(group.failing_record[1]).record_kind
        is AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION
    ):
        authorization_positions.append(group.failing_record[0])
    if (
        authorization_positions
        and (group.ledger_records or group.refresh_record is not None)
        and (
            group.refresh_record is None
            or any(position <= group.refresh_record[0] for position in authorization_positions)
        )
    ):
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "recovery authorization stage order conflicts",
        )
    if group.ledger_records or group.refresh_record is not None:
        outcome_positions = [entry[0] for entry in group.outcome_records.values()]
        ledger_positions = [entry[0] for entry in group.ledger_records]
        if group.batch_record is None or ledger_positions != sorted(ledger_positions):
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "recovery ledger stage order conflicts",
            )
        if any(position <= group.batch_record[0] for position in ledger_positions) or any(
            outcome_position >= ledger_position
            for outcome_position in outcome_positions
            for ledger_position in ledger_positions
            if outcome_position is not None
        ):
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "recovery ledger stage order conflicts",
            )
        if group.refresh_record is not None:
            refresh_position = group.refresh_record[0]
            required_before_refresh = [group.batch_record[0], *outcome_positions, *ledger_positions]
            if any(position >= refresh_position for position in required_before_refresh):
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "recovery refresh stage order conflicts",
                )
    completion = group.completion_record
    if completion is not None:
        completion_position = completion[0]
        required_before_completion = [
            entry[0]
            for entry in (group.batch_record, *group.outcome_records.values())
            if entry is not None
        ]
        required_before_completion.extend(entry[0] for entry in group.authorization_records)
        required_before_completion.extend(entry[0] for entry in group.ledger_records)
        if group.refresh_record is not None:
            required_before_completion.append(group.refresh_record[0])
        if group.batch_record is None or any(
            position >= completion_position for position in required_before_completion
        ):
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "recovery completion stage order conflicts",
            )


def _require_runtime_trace(
    runtime: RuntimeLifecyclePort,
    binding: RunBinding,
) -> dict[int, tuple[dict[str, object], Sha256Digest]]:
    try:
        records = runtime.trace_records
        digest = runtime.trace_digest
    except (AttributeError, TypeError) as error:
        raise LifecycleError(
            OutcomeCode.INVALID_TYPE,
            "runtime trace evidence is incomplete",
        ) from error
    if type(records) is not tuple or digest != historical_runtime_trace_digest(records):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "runtime trace digest conflicts")
    result: dict[int, tuple[dict[str, object], Sha256Digest]] = {}
    for canonical_record in records:
        try:
            document = json.loads(canonical_record)
            canonical = json.dumps(
                document,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            sequence = document["dispatch_sequence"]
            root = document["root"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "runtime trace record is invalid",
            ) from error
        if (
            type(document) is not dict
            or canonical != canonical_record
            or document.get("schema")
            not in {HISTORICAL_RUNTIME_TRACE_SCHEMA, "ea.phase1-historical-runtime-trace.v2"}
            or document.get("run_id") != binding.reference.run_id.value
            or type(sequence) is not int
            or sequence in result
            or type(root) is not dict
        ):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "runtime trace record conflicts")
        root_bytes = json.dumps(
            root,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if document.get("schema") == "ea.phase1-historical-runtime-trace.v2":
            read_only_root = _decode_read_only_trace_root(document, runtime.spec_set)
            root_sha256 = read_only_root.observation_sha256
        elif (
            set(root)
            == {
                "available_at",
                "kind",
                "producer_namespace",
                "producer_sequence",
                "run_id",
                "type",
            }
            and root.get("type") == "end_of_run"
        ):
            domain = HISTORICAL_MATCHER_END_ROOT_DIGEST_DOMAIN
        elif set(root) == {
            "adjustment",
            "available_at",
            "close_bits",
            "high_bits",
            "interval_end",
            "interval_start",
            "kind",
            "low_bits",
            "open_bits",
            "revision",
            "source",
            "source_sequence",
            "symbol",
            "venue",
            "volume_bits",
        }:
            domain = HISTORICAL_MATCHER_MARKET_ROOT_DIGEST_DOMAIN
        else:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "runtime trace root kind is invalid")
        if document.get("schema") == HISTORICAL_RUNTIME_TRACE_SCHEMA:
            root_sha256 = Sha256Digest(
                sha256(domain + len(root_bytes).to_bytes(8, "big") + root_bytes).hexdigest()
            )
        result[sequence] = (document, root_sha256)
    if set(result) != set(range(1, len(result) + 1)):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "runtime trace sequence is not contiguous")
    return result


def _recover_dispatch(
    coordinator: Phase1HistoricalLifecycleCoordinator,
    recovered: _RecoveredDispatch,
    trace_by_sequence: dict[int, tuple[dict[str, object], Sha256Digest]],
) -> None:
    from ea.runtime.coordinator import _ActiveDispatch, _RecoveredLease

    if coordinator._active is not None or coordinator._pre_terminal_state is not None:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovery dispatch follows open state")
    if recovered.read_only_outcome_record is not None:
        recover_read_only_dispatch(coordinator, recovered, trace_by_sequence)
        return
    runtime_lease = coordinator._runtime.active_lease
    trigger_sha256 = recovered.trigger_sha256
    if trigger_sha256 is None and runtime_lease is not None:
        if runtime_lease.dispatch_sequence != recovered.sequence:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "active recovery sequence conflicts")
        trigger_sha256 = _root_digest(runtime_lease.root)
    if trigger_sha256 is None:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovery trigger evidence is missing")
    batch = coordinator._matcher.resolve_dispatch_batch(
        dispatch_sequence=recovered.sequence,
        trigger_root_sha256=trigger_sha256,
    )
    if batch is None and (
        recovered.batch_record is not None
        or recovered.outcome_records
        or recovered.completion_record is not None
    ):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "matcher recovery evidence is missing")
    if batch is not None and (
        batch.dispatch_sequence != recovered.sequence or batch.trigger_root_sha256 != trigger_sha256
    ):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "matcher recovery evidence conflicts")
    is_end = (batch is not None and batch.dispatch_kind is HistoricalDispatchKind.END_OF_RUN) or (
        runtime_lease is not None and type(runtime_lease.root) is EndOfRunRoot
    )
    state_before = coordinator._state
    coordinator._state = _recovered_capture_state(
        state_before,
        sequence=recovered.sequence,
        trigger_sha256=trigger_sha256,
        is_end=is_end,
    )
    lease: RuntimeDispatchLeaseView
    if runtime_lease is not None and runtime_lease.dispatch_sequence == recovered.sequence:
        if _root_digest(runtime_lease.root) != trigger_sha256:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "active recovery root conflicts")
        lease = runtime_lease
    else:
        lease = _RecoveredLease(cast(RuntimeRoot, object()), recovered.sequence)
    active = _ActiveDispatch(lease=lease, trigger_sha256=trigger_sha256, batch=batch)
    if batch is None:
        if runtime_lease is not lease:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "unresolved matcher has no active lease",
            )
        _recover_pre_batch_failing_transition(coordinator, active, recovered.failing_record)
        coordinator._active = active
        return

    batch_entry = recovered.batch_record
    failing_entry = recovered.failing_record
    pre_batch_failure = (
        failing_entry is not None
        and (batch_entry is None or failing_entry[0] < batch_entry[0])
        and _is_pre_batch_failing_record(failing_entry[1], active.trigger_sha256)
    )
    if pre_batch_failure:
        _recover_pre_batch_failing_transition(coordinator, active, failing_entry)
    if batch_entry is not None:
        _batch_position, batch_record, batch_ack = batch_entry
        expected_batch_payload = canonical_matcher_batch_audit_payload(coordinator._binding, batch)
        if batch_record.canonical_payload != expected_batch_payload:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovered batch payload conflicts")
        active.batch_ack = batch_ack
    count = len(batch.ingresses)
    active.outcomes = [None] * count
    active.outcome_acks = [None] * count
    active.handoffs = [None] * count
    matched_outcome_digests: set[Sha256Digest] = set()
    for index, ingress in enumerate(batch.ingresses):
        outcome = coordinator._fact_authority.resolve_processing_outcome(
            ingress_identity=ingress.identity,
            ingress_sha256=execution_fact_ingress_digest(ingress),
        )
        if outcome is not None and type(outcome) is not ExecutionFactProcessingOutcome:
            raise LifecycleError(OutcomeCode.INVALID_TYPE, "fact recovery returned invalid outcome")
        active.outcomes[index] = outcome
        if outcome is None:
            continue
        outcome_digest = execution_fact_processing_outcome_digest(outcome)
        outcome_entry = recovered.outcome_records.get(outcome_digest)
        if outcome_entry is None:
            continue
        _position, outcome_record, outcome_ack = outcome_entry
        if outcome_record.canonical_payload != canonical_execution_fact_processing_outcome_bytes(
            outcome
        ):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovered outcome payload conflicts")
        active.outcome_acks[index] = outcome_ack
        matched_outcome_digests.add(outcome_digest)
    if matched_outcome_digests != set(recovered.outcome_records):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "orphan recovered outcome record")
    _recover_authorization_frontier(coordinator, active, recovered)
    for index, outcome in enumerate(active.outcomes):
        acknowledgement = active.outcome_acks[index]
        if outcome is not None and acknowledgement is not None and active.batch_ack is not None:
            coordinator._require_evidence(outcome)
            active.handoffs[index] = create_audited_execution_fact_handoff(
                outcome=outcome,
                batch_acknowledgement=active.batch_ack,
                outcome_acknowledgement=acknowledgement,
            )
    if coordinator._ledger_handoff_authority is not None:
        _recover_ledger_frontier(coordinator, active, recovered)
    if not pre_batch_failure:
        _recover_failing_transition(coordinator, active, recovered)
    completion_entry = recovered.completion_record
    if completion_entry is None:
        if runtime_lease is not lease:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "incomplete dispatch has no active lease",
            )
        coordinator._active = active
        return
    if (
        active.batch_ack is None
        or any(value is None for value in active.outcomes)
        or any(value is None for value in active.outcome_acks)
    ):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "completion lacks authoritative evidence")
    outcome_acks = tuple(value for value in active.outcome_acks if value is not None)
    expected_completion_payload = active.completion_payload
    if expected_completion_payload is None:
        expected_completion_payload = coordinator._completion_payload(active, outcome_acks)
        active.completion_payload = expected_completion_payload
    _completion_position, completion_record, completion_ack = completion_entry
    if completion_record.canonical_payload != expected_completion_payload:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "recovered completion payload conflicts")
    if active.refresh_value_sha256 is not None:
        coordinator._final_refresh_value_sha256 = active.refresh_value_sha256
    active.completion_ack = completion_ack
    trace_entry = trace_by_sequence.get(recovered.sequence)
    if trace_entry is None:
        if runtime_lease is not lease:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "completion/runtime acknowledgement conflicts",
            )
        coordinator._active = active
        return
    trace_document, trace_root_sha256 = trace_entry
    if trace_root_sha256 != trigger_sha256 or trace_document.get(
        "root_order_key"
    ) != _trace_root_order_key_document(batch.trigger_root_key):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "runtime trace root conflicts")
    if runtime_lease is not None and runtime_lease.dispatch_sequence == recovered.sequence:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "runtime retained an acknowledged lease")
    resulting_state = _recovered_completed_state(
        coordinator._state,
        active,
        completion_ack,
        failing_ack=coordinator._failing_ack,
        is_end=is_end,
    )
    coordinator._state = resulting_state
    if is_end:
        if trace_document.get("terminal_acknowledged") is not True or (
            coordinator._runtime.terminal_acknowledged is not True
        ):
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "terminal runtime trace conflicts")
        coordinator._begin_terminalization(active, resulting_state)
    elif trace_document.get("terminal_acknowledged") is not False:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "market trace claims terminal state")


def _recover_authorization_frontier(
    coordinator: Phase1HistoricalLifecycleCoordinator,
    active: _ActiveDispatch,
    recovered: _RecoveredDispatch,
) -> None:

    completion_attempt: SubmissionAuthorizationAttemptOutcome | None = None
    if recovered.completion_record is not None:
        completion_document = _record_document(recovered.completion_record[1])
        attempt_count = completion_document.get("authorization_attempt_count")
        nested_attempt = completion_document.get("authorization_attempt_outcome")
        if attempt_count == 1:
            completion_attempt = decode_submission_authorization_attempt_outcome_document(
                nested_attempt
            )
            if (
                completion_attempt.binding != coordinator._binding
                or completion_attempt.dispatch_sequence != active.lease.dispatch_sequence
                or completion_attempt.trigger_root_sha256 != active.trigger_sha256
            ):
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "completion authorization frontier conflicts",
                )
        elif attempt_count != 0 or nested_attempt is not None:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "completion authorization frontier is invalid",
            )
    if not recovered.authorization_records:
        if completion_attempt is not None:
            if (
                completion_attempt.status
                in {
                    SubmissionAuthorizationAttemptStatus.AUTHORIZED,
                    SubmissionAuthorizationAttemptStatus.BURNED,
                }
                or completion_attempt.acknowledgement_sha256 is not None
            ):
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "completion authorization record is missing",
                )
            active.authorization_attempt = completion_attempt
        return
    authorization = coordinator._authorization
    if authorization is None or len(recovered.authorization_records) != 1:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "recovered authorization authority is unavailable",
        )
    _position, record, record_acknowledgement = recovered.authorization_records[0]
    document = _record_document(record)
    order_document = document.get("order_id")
    try:
        if type(order_document) is not dict:
            raise TypeError("order identity is not an object")
        order_id = EconomicId(
            coordinator._binding.reference.run_id,
            EconomicOwnerKind(order_document["owner_kind"]),
            order_document["owner_sequence"],
        )
        request_sha256 = Sha256Digest(cast(str, document["execution_request_sha256"]))
    except (KeyError, TypeError, ValueError) as error:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "recovered authorization identity is invalid",
        ) from error
    order = authorization.resolve_attempt_order(
        order_id=order_id,
        execution_request_sha256=request_sha256,
    )
    attempt = authorization.resolve_attempt(
        order_id=order_id,
        execution_request_sha256=request_sha256,
    )
    acknowledgement = authorization.resolve_attempt_acknowledgement(
        order_id=order_id,
        execution_request_sha256=request_sha256,
    )
    if (
        type(order) is not Order
        or type(attempt) is not SubmissionAuthorizationAttemptOutcome
        or type(acknowledgement) is not AuditAppendAcknowledgement
        or audit_append_acknowledgement_digest(acknowledgement)
        != audit_append_acknowledgement_digest(record_acknowledgement)
    ):
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "recovered authorization evidence conflicts",
        )
    receipt = coordinator._matcher.resolve_submission_receipt(
        order_id=order_id,
        execution_request_sha256=request_sha256,
    )
    if receipt is not None and type(receipt) is not HistoricalSubmissionReceipt:
        raise LifecycleError(
            OutcomeCode.INVALID_TYPE,
            "recovered matcher receipt is invalid",
        )
    if completion_attempt is not None:
        if (
            completion_attempt.order_id != order_id
            or completion_attempt.execution_request_sha256 != request_sha256
            or completion_attempt.acknowledgement_sha256
            != audit_append_acknowledgement_digest(record_acknowledgement)
        ):
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "completion authorization frontier conflicts",
            )
        attempt = completion_attempt
    elif recovered.completion_record is not None:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "completion authorization attempt is missing",
        )
    elif receipt is not None:
        attempt = create_submission_authorization_attempt_outcome(
            binding=coordinator._binding,
            dispatch_sequence=active.lease.dispatch_sequence,
            trigger_root_sha256=active.trigger_sha256,
            order_id=order_id,
            execution_request_sha256=request_sha256,
            authorization_payload_sha256=record_acknowledgement.payload_sha256,
            status=SubmissionAuthorizationAttemptStatus.AUTHORIZED,
            logical_key=record.logical_key,
            acknowledgement_sha256=audit_append_acknowledgement_digest(record_acknowledgement),
            error_code=None,
        )
    if (
        receipt is not None
        and receipt.audit_acknowledgement_sha256
        != audit_append_acknowledgement_digest(record_acknowledgement)
    ):
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "recovered matcher receipt authorization conflicts",
        )
    active.authorization_order = order
    active.authorization_ack = acknowledgement
    active.authorization_attempt = attempt
    active.submission_receipt = receipt


def _recover_failing_transition(
    coordinator: Phase1HistoricalLifecycleCoordinator,
    active: _ActiveDispatch,
    recovered: _RecoveredDispatch,
) -> None:
    entry = recovered.failing_record
    if entry is None:
        return
    if coordinator._state.phase is CoordinatorPhase.FAILING:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "failing transition is not monotone")
    position, record, acknowledgement = entry
    document = _record_document(record)
    try:
        failure_code = OutcomeCode(cast(str, document["failure_code"]))
    except (KeyError, TypeError, ValueError) as error:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "failing recovery code is invalid",
        ) from error
    failed_key = _recovered_failed_logical_key(record)
    logical_positions: list[tuple[AuditLogicalKey, int | None]] = []
    if active.batch is None:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "failing recovery batch is missing")
    batch_key = AuditLogicalKey(
        AuditRecordKind.MATCHER_DISPATCH_BATCH,
        AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
        historical_matcher_dispatch_batch_digest(active.batch),
    )
    logical_positions.append(
        (batch_key, None if recovered.batch_record is None else recovered.batch_record[0])
    )
    acknowledgement_by_key: dict[AuditLogicalKey, AuditAppendAcknowledgement] = {}
    if recovered.batch_record is not None and recovered.batch_record[0] < position:
        acknowledgement_by_key[batch_key] = recovered.batch_record[2]
    for outcome in active.outcomes:
        if outcome is None:
            continue
        digest = execution_fact_processing_outcome_digest(outcome)
        key = AuditLogicalKey(
            AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME,
            AuditSubjectKind.EXECUTION_FACT_PROCESSING_OUTCOME,
            digest,
        )
        outcome_entry = recovered.outcome_records.get(digest)
        outcome_position = None if outcome_entry is None else outcome_entry[0]
        logical_positions.append((key, outcome_position))
        if outcome_entry is not None and outcome_entry[0] < position:
            acknowledgement_by_key[key] = outcome_entry[2]
    ledger_entries = {entry[1].logical_key: entry for entry in recovered.ledger_records}
    ledger_keys: set[AuditLogicalKey] = set()
    for ledger_outcome in active.ledger_outcomes:
        if ledger_outcome is None:
            continue
        payload = canonical_ledger_handoff_outcome_bytes(ledger_outcome)
        key = AuditLogicalKey(
            AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
            AuditSubjectKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME,
            audit_subject_digest(AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME, payload),
        )
        ledger_keys.add(key)
        ledger_entry = ledger_entries.get(key)
        ledger_position = None if ledger_entry is None else ledger_entry[0]
        logical_positions.append((key, ledger_position))
        if ledger_entry is not None and ledger_entry[0] < position:
            acknowledgement_by_key[key] = ledger_entry[2]
    if failed_key.record_kind is AuditRecordKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME and (
        failed_key.subject_kind is not AuditSubjectKind.PORTFOLIO_LEDGER_HANDOFF_OUTCOME
        or failed_key not in ledger_keys
    ):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "failed ledger key conflicts")
    refresh_key = (
        None
        if active.refresh_sha256 is None
        else AuditLogicalKey(
            AuditRecordKind.RISK_PORTFOLIO_REFRESH,
            AuditSubjectKind.PORTFOLIO_RISK_REFRESH,
            active.refresh_sha256,
        )
    )
    if refresh_key is not None:
        refresh_entry = recovered.refresh_record
        refresh_position = None if refresh_entry is None else refresh_entry[0]
        if refresh_entry is None or refresh_entry[0] < position or refresh_key == failed_key:
            logical_positions.append((refresh_key, refresh_position))
        if refresh_entry is not None and refresh_entry[0] < position:
            acknowledgement_by_key[refresh_key] = refresh_entry[2]
    if failed_key.record_kind is AuditRecordKind.RISK_PORTFOLIO_REFRESH and (
        failed_key.subject_kind is not AuditSubjectKind.PORTFOLIO_RISK_REFRESH
        or refresh_key != failed_key
    ):
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "failed refresh key conflicts")
    if failed_key.record_kind is AuditRecordKind.SUBMISSION_PRE_EFFECT_AUTHORIZATION:
        if failed_key.subject_kind is not AuditSubjectKind.HISTORICAL_EXECUTION_REQUEST:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "failed authorization key conflicts",
            )
        attempt = active.authorization_attempt
        if attempt is not None and (
            attempt.status is not SubmissionAuthorizationAttemptStatus.FAILED
            or attempt.logical_key != failed_key
            or attempt.acknowledgement_sha256 is not None
        ):
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "failed authorization frontier conflicts",
            )
        logical_positions.append((failed_key, None))
    if failed_key.record_kind is AuditRecordKind.RUNTIME_DISPATCH_COMPLETED:
        if active.batch_ack is None or any(value is None for value in active.outcome_acks):
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "failed completion has incomplete inbound evidence",
            )
        active.completion_payload = coordinator._completion_payload(
            active,
            tuple(value for value in active.outcome_acks if value is not None),
        )
        completion_key = AuditLogicalKey(
            AuditRecordKind.RUNTIME_DISPATCH_COMPLETED,
            AuditSubjectKind.RUNTIME_DISPATCH,
            dispatch_completed_subject_digest(active.completion_payload),
        )
        if completion_key != failed_key:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "failed completion key conflicts")
        logical_positions.append(
            (
                completion_key,
                None if recovered.completion_record is None else recovered.completion_record[0],
            )
        )
        if recovered.completion_record is not None and recovered.completion_record[0] < position:
            acknowledgement_by_key[completion_key] = recovered.completion_record[2]
    baseline_missing = tuple(
        key
        for key, record_position in logical_positions
        if record_position is None or record_position > position or key == failed_key
    )
    previous_state = coordinator._state
    missing_candidates = [baseline_missing]
    if failed_key in acknowledgement_by_key:
        missing_candidates.append(tuple(key for key in baseline_missing if key != failed_key))
    matched: tuple[CoordinatorRunState, bytes] | None = None
    for missing in missing_candidates:
        acknowledged = tuple(
            acknowledgement
            for key, acknowledgement in acknowledgement_by_key.items()
            if key not in missing
        )
        outcome_acknowledgements = tuple(
            acknowledgement_by_key[key]
            for outcome in active.outcomes
            if outcome is not None
            for key in (
                AuditLogicalKey(
                    AuditRecordKind.EXECUTION_FACT_PROCESSING_OUTCOME,
                    AuditSubjectKind.EXECUTION_FACT_PROCESSING_OUTCOME,
                    execution_fact_processing_outcome_digest(outcome),
                ),
            )
            if key in acknowledgement_by_key and key not in missing
        )
        candidate_state = create_coordinator_run_state(
            binding=coordinator._binding,
            state_version=previous_state.state_version + 1,
            phase=CoordinatorPhase.FAILING,
            active_dispatch_sequence=active.lease.dispatch_sequence,
            active_trigger_root_sha256=active.trigger_sha256,
            matcher_batch_sha256=historical_matcher_dispatch_batch_digest(active.batch),
            ordered_ingress_sha256s_sha256=ordered_digest_tuple(
                ORDERED_INGRESS_DIGEST_DOMAIN,
                active.batch.ingress_sha256s,
            ),
            ordered_outcome_ack_sha256s_sha256=ordered_digest_tuple(
                ORDERED_OUTCOME_ACK_DIGEST_DOMAIN,
                tuple(
                    audit_append_acknowledgement_digest(candidate)
                    for candidate in outcome_acknowledgements
                ),
            ),
            missing_audit_logical_keys=missing,
            failure_code=previous_state.failure_code or failure_code,
            last_completed_dispatch_sequence=previous_state.last_completed_dispatch_sequence,
            last_audit_chain_head_sha256=(
                previous_state.last_audit_chain_head_sha256
                if not acknowledged
                else max(
                    acknowledged,
                    key=lambda candidate: candidate.record_id.owner_sequence,
                ).chain_head_sha256
            ),
        )
        candidate_payload = canonical_failing_safety_audit_payload(
            binding=coordinator._binding,
            previous_state_sha256=coordinator_run_state_digest(previous_state),
            failing_state_sha256=coordinator_run_state_digest(candidate_state),
            failure_code=candidate_state.failure_code or failure_code,
            failed_logical_key=failed_key,
            dispatch_sequence=active.lease.dispatch_sequence,
            trigger_root_sha256=active.trigger_sha256,
        )
        if record.canonical_payload == candidate_payload:
            if matched is not None:
                raise LifecycleError(
                    OutcomeCode.CONFLICTING_ID,
                    "failing recovery state is ambiguous",
                )
            matched = (candidate_state, candidate_payload)
    if matched is None:
        raise LifecycleError(OutcomeCode.CONFLICTING_ID, "failing recovery payload conflicts")
    failing_state, expected_payload = matched
    coordinator._state = failing_state
    coordinator._failing_payload = expected_payload
    coordinator._failing_key = record.logical_key
    coordinator._failing_ack = acknowledgement


def _recover_pre_batch_failing_transition(
    coordinator: Phase1HistoricalLifecycleCoordinator,
    active: _ActiveDispatch,
    entry: tuple[int, AuditRecord, AuditAppendAcknowledgement] | None,
) -> None:
    previous_state = coordinator._state
    failure_code = OutcomeCode.DURABILITY_AUDIT_APPEND_FAILED
    failed_key = AuditLogicalKey(
        AuditRecordKind.MATCHER_DISPATCH_BATCH,
        AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH,
        active.trigger_sha256,
    )
    record: AuditRecord | None = None
    acknowledgement: AuditAppendAcknowledgement | None = None
    if entry is not None:
        _position, record, acknowledgement = entry
        document = _record_document(record)
        try:
            failure_code = OutcomeCode(cast(str, document["failure_code"]))
            retained_key = AuditLogicalKey(
                AuditRecordKind(cast(str, document["failed_record_kind"])),
                AuditSubjectKind(cast(str, document["failed_subject_kind"])),
                Sha256Digest(cast(str, document["failed_subject_sha256"])),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "pre-batch failing recovery key is invalid",
            ) from error
        if retained_key != failed_key:
            raise LifecycleError(
                OutcomeCode.CONFLICTING_ID,
                "pre-batch failing recovery key conflicts",
            )
    failing_state = create_coordinator_run_state(
        binding=coordinator._binding,
        state_version=previous_state.state_version + 1,
        phase=CoordinatorPhase.FAILING,
        active_dispatch_sequence=active.lease.dispatch_sequence,
        active_trigger_root_sha256=active.trigger_sha256,
        matcher_batch_sha256=None,
        ordered_ingress_sha256s_sha256=ordered_digest_tuple(
            ORDERED_INGRESS_DIGEST_DOMAIN,
            (),
        ),
        ordered_outcome_ack_sha256s_sha256=ordered_digest_tuple(
            ORDERED_OUTCOME_ACK_DIGEST_DOMAIN,
            (),
        ),
        missing_audit_logical_keys=(),
        failure_code=previous_state.failure_code or failure_code,
        last_completed_dispatch_sequence=previous_state.last_completed_dispatch_sequence,
        last_audit_chain_head_sha256=previous_state.last_audit_chain_head_sha256,
    )
    payload = canonical_failing_safety_audit_payload(
        binding=coordinator._binding,
        previous_state_sha256=coordinator_run_state_digest(previous_state),
        failing_state_sha256=coordinator_run_state_digest(failing_state),
        failure_code=failing_state.failure_code or failure_code,
        failed_logical_key=failed_key,
        dispatch_sequence=active.lease.dispatch_sequence,
        trigger_root_sha256=active.trigger_sha256,
    )
    if record is not None and record.canonical_payload != payload:
        raise LifecycleError(
            OutcomeCode.CONFLICTING_ID,
            "pre-batch failing recovery payload conflicts",
        )
    coordinator._state = failing_state
    coordinator._failing_payload = payload
    coordinator._failing_key = AuditLogicalKey(
        AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION,
        AuditSubjectKind.COORDINATOR_STATE,
        audit_subject_digest(AuditRecordKind.RUNTIME_FAILING_SAFETY_TRANSITION, payload),
    )
    coordinator._failing_ack = acknowledgement


def _is_pre_batch_failing_record(record: AuditRecord, trigger_sha256: Sha256Digest) -> bool:
    document = _record_document(record)
    return (
        document.get("failed_record_kind") == AuditRecordKind.MATCHER_DISPATCH_BATCH.value
        and document.get("failed_subject_kind")
        == AuditSubjectKind.HISTORICAL_MATCHER_DISPATCH_BATCH.value
        and document.get("failed_subject_sha256") == trigger_sha256.value
    )


def _root_digest(root: object) -> Sha256Digest:
    if type(root) is MarketDataEnvelope:
        return historical_market_root_digest(root)
    if type(root) is EndOfRunRoot:
        return historical_end_root_digest(root)
    raise LifecycleError(OutcomeCode.INVALID_TYPE, "unsupported recovered runtime root")


def _trace_root_order_key_document(key: RuntimeRootOrderKey) -> list[str | int]:
    try:
        values = key.as_tuple()
    except (AttributeError, TypeError) as error:
        raise LifecycleError(OutcomeCode.INVALID_TYPE, "runtime root key is incomplete") from error
    document: list[str | int] = []
    for value in values:
        if type(value) is datetime:
            if value.tzinfo is None or value.utcoffset() is None:
                raise LifecycleError(OutcomeCode.CONFLICTING_ID, "runtime root key time is naive")
            document.append(value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
        elif type(value) in (str, int):
            document.append(cast(str | int, value))
        else:
            raise LifecycleError(OutcomeCode.CONFLICTING_ID, "runtime root key value is invalid")
    return document


def _latest_active_chain_head(
    active: _ActiveDispatch,
    state: CoordinatorRunState,
) -> Sha256Digest:
    acknowledgements = tuple(
        acknowledgement
        for acknowledgement in (
            active.batch_ack,
            *active.outcome_acks,
            active.authorization_ack,
            *active.ledger_acks,
            active.refresh_ack,
            active.completion_ack,
        )
        if acknowledgement is not None
    )
    if not acknowledgements:
        return state.last_audit_chain_head_sha256
    return max(
        acknowledgements,
        key=lambda acknowledgement: acknowledgement.record_id.owner_sequence,
    ).chain_head_sha256


def _recovered_capture_state(
    state: CoordinatorRunState,
    *,
    sequence: int,
    trigger_sha256: Sha256Digest,
    is_end: bool,
) -> CoordinatorRunState:
    if state.state_version == _MAX_UINT64:
        raise LifecycleError(OutcomeCode.OUT_OF_RANGE, "recovered state version overflow")
    return create_coordinator_run_state(
        binding=state.binding,
        state_version=state.state_version + 1,
        phase=(
            CoordinatorPhase.DRAINING
            if is_end
            else (
                CoordinatorPhase.FAILING
                if state.phase is CoordinatorPhase.FAILING
                else CoordinatorPhase.RUNNING
            )
        ),
        active_dispatch_sequence=sequence,
        active_trigger_root_sha256=trigger_sha256,
        matcher_batch_sha256=None,
        ordered_ingress_sha256s_sha256=ordered_digest_tuple(ORDERED_INGRESS_DIGEST_DOMAIN, ()),
        ordered_outcome_ack_sha256s_sha256=ordered_digest_tuple(
            ORDERED_OUTCOME_ACK_DIGEST_DOMAIN,
            (),
        ),
        missing_audit_logical_keys=(),
        failure_code=state.failure_code,
        last_completed_dispatch_sequence=state.last_completed_dispatch_sequence,
        last_audit_chain_head_sha256=state.last_audit_chain_head_sha256,
    )


def _recovered_completed_state(
    state: CoordinatorRunState,
    active: _ActiveDispatch,
    completion_ack: AuditAppendAcknowledgement,
    *,
    failing_ack: AuditAppendAcknowledgement | None,
    is_end: bool,
) -> CoordinatorRunState:
    if state.state_version > _MAX_UINT64 - 2:
        raise LifecycleError(OutcomeCode.OUT_OF_RANGE, "recovered state version overflow")
    return create_coordinator_run_state(
        binding=state.binding,
        state_version=state.state_version + 2,
        phase=CoordinatorPhase.DRAINING if is_end else state.phase,
        active_dispatch_sequence=None,
        active_trigger_root_sha256=None,
        matcher_batch_sha256=None,
        ordered_ingress_sha256s_sha256=ordered_digest_tuple(ORDERED_INGRESS_DIGEST_DOMAIN, ()),
        ordered_outcome_ack_sha256s_sha256=ordered_digest_tuple(
            ORDERED_OUTCOME_ACK_DIGEST_DOMAIN,
            (),
        ),
        missing_audit_logical_keys=(),
        failure_code=state.failure_code,
        last_completed_dispatch_sequence=active.lease.dispatch_sequence,
        last_audit_chain_head_sha256=_latest_completion_chain_head(
            completion_ack,
            failing_ack,
        ),
    )


def _latest_completion_chain_head(
    completion: AuditAppendAcknowledgement,
    failing: AuditAppendAcknowledgement | None,
) -> Sha256Digest:
    candidates = (completion,) if failing is None else (completion, failing)
    return max(
        candidates,
        key=lambda acknowledgement: acknowledgement.record_id.owner_sequence,
    ).chain_head_sha256
