"""Read-only observation of the nine PPV-06 signals from existing projections.

Every signal is read from a projection some other owner already published. None
of it is re-derived here:

* run state, liveness, readiness, reconciliation and the live operator-halt
  projection come from PPV-03 by reading ``paper_status`` through
  :func:`ea.product.paper_session.read_paper_status`. The readiness verdict is
  PPV-03's and is never recomputed; the kill switch is the *live* projection
  inside the health block, never the ``outputs/kill-switch.json`` file that was
  written once at startup while the switch was still active and is never
  re-persisted after a halt.
* the newest verified backup comes from PPV-05's own verification, through the
  one additive helper in :mod:`ea.product.paper_backup`.
* free space comes from ``fstatvfs`` on the directory that holds Paper state.

This module is observation only. Reading it never repairs a ledger, lifts a
halt, restores trading or resends an order, and nothing here is allowed to fail
a run: every observation failure is recorded as an explicit ``unavailable``
reading rather than raised, because an observer that can halt what it observes
is not an observer.

Two signals are structurally unavailable today and are published as such on
every evaluation:

* ``backup_failed`` -- no durable per-attempt backup-outcome record exists to
  read. Manufacturing one would make observation a dependency of a
  state-mutating path, so PPV-06 does not, and reports the signal unavailable.
  ``backup_stale`` is the observable proxy for backups that are not succeeding.
* ``broker_unavailable`` -- ``PaperBroker.available`` returns a constant ``True``
  until a PPV-17 provider exists, so there is no fact to threshold and no
  threshold is built over a constant.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import final

from ea.core.outcomes import OutcomeCode
from ea.core.run import RunId
from ea.core.time import Clock, TimeValidationError, require_utc
from ea.product.paper_alerts import (
    ALERT_SIGNALS,
    DEFAULT_ALERT_THRESHOLDS,
    AlertSignalReading,
    AlertThresholds,
    AlertType,
    PaperAlertError,
    PaperAlertStream,
    SignalCondition,
    project_alert_stream,
)
from ea.product.paper_backup import (
    PaperBackupError,
    latest_verified_paper_backup,
)
from ea.product.paper_health import (
    KillSwitchProjectionState,
    PaperHealthSnapshot,
    ReconciliationState,
    decode_paper_health,
    is_paper_health_unavailable,
)
from ea.product.paper_session import (
    _TERMINAL,
    PaperSessionError,
    read_paper_status,
)

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

# How many of PPV-03's readiness reason codes an observation quotes. The codes
# are PPV-03's own; this only bounds the text carried into the alert.
_NOT_READY_REASON_LIMIT = 4


def _fail(code: OutcomeCode, message: str) -> PaperAlertError:
    return PaperAlertError(code, message)


@final
@dataclass(frozen=True, slots=True)
class PaperRunObservation:
    """What one read-only look at a Paper attempt could actually establish.

    ``health`` is ``None`` both when the attempt could not be read at all and
    when the writer published an explicit unavailable marker; ``detail`` says
    which. Nothing here is inferred from anything else.
    """

    readable: bool
    detail: str
    run_id: RunId | None = None
    account_id: str | None = None
    state: str | None = None
    alive: bool | None = None
    terminal_durable: bool | None = None
    health: PaperHealthSnapshot | None = None


def _bounded_detail(value: object, *, maximum: int = 200) -> str:
    if type(value) is not str or not value:
        return "the observation failed without a usable detail"
    cleaned = "".join(
        character if ord(character) >= 32 and ord(character) != 127 else " " for character in value
    )
    return cleaned[:maximum]


def observe_paper_run(run_dir: Path, *, now: datetime) -> PaperRunObservation:
    """Read one attempt's published projection without mutating anything.

    The writer lease is re-observed by PPV-03 from the live lock, so liveness
    here is a current fact and never a value frozen into the durable bytes. A
    failure to read is recorded, never raised: this is an observation surface.
    """
    try:
        status = read_paper_status(run_dir, now=now)
    except Exception as error:  # noqa: BLE001 - an observation never fails a run
        return PaperRunObservation(
            readable=False,
            detail=f"the attempt could not be read: {type(error).__name__}",
        )

    run_id: RunId | None = None
    raw_run_id = status.get("run_id")
    if type(raw_run_id) is str:
        try:
            run_id = RunId(raw_run_id)
        except (TypeError, ValueError):
            run_id = None
    raw_account = status.get("account_id")
    account_id = raw_account if type(raw_account) is str and raw_account.strip() else None
    # The state is whatever the reader published. PPV-03 already validates its
    # own vocabulary and overlays "interrupted" when a non-terminal attempt lost
    # its writer lease; re-listing that vocabulary here would be a second copy of
    # a projection this module is only allowed to consume.
    raw_state = status.get("state")
    state = raw_state if type(raw_state) is str and raw_state.strip() else None
    alive = status.get("lease_held")
    alive = alive if type(alive) is bool else None
    raw_terminal = status.get("terminal_durable")
    terminal_durable = raw_terminal if type(raw_terminal) is bool else None

    slot = status.get("health")
    health: PaperHealthSnapshot | None = None
    if slot is None:
        detail = "the run has published no health projection"
    elif is_paper_health_unavailable(slot):
        detail = f"the run published an unavailable health projection: {slot.get('detail')}"
    else:
        try:
            health = decode_paper_health(slot)
        except (PaperAlertError, PaperSessionError, ValueError, TypeError, KeyError) as error:
            detail = f"the health projection did not decode: {type(error).__name__}"
        else:
            detail = ""

    return PaperRunObservation(
        readable=True,
        detail=_bounded_detail(detail),
        run_id=run_id,
        account_id=account_id,
        state=state,
        alive=alive,
        terminal_durable=terminal_durable,
        health=health,
    )


_RUN_SCOPED = (
    AlertType.RUNTIME_DOWN,
    AlertType.NOT_READY_BEYOND_THRESHOLD,
    AlertType.RECONCILIATION_CONFLICT,
    AlertType.OPERATOR_HALT_ACTIVE,
    AlertType.UNEXPECTED_RESTART,
)


def _read_run_signals(observation: PaperRunObservation) -> list[AlertSignalReading]:
    """Derive the run-scoped readings from PPV-03's projection alone."""
    if not observation.readable:
        return [
            AlertSignalReading(alert_type, SignalCondition.UNAVAILABLE, observation.detail)
            for alert_type in _RUN_SCOPED
        ]

    alive = observation.alive
    terminal = observation.state is not None and observation.state in _TERMINAL

    # runtime_down -- liveness alone is not an error. PPV-03 reads a released
    # lease as "not alive", never as a fault, so a runtime that ended as designed
    # is reported as not down; the alert carries the observed state either way.
    if alive is None or observation.state is None:
        runtime_down = AlertSignalReading(
            AlertType.RUNTIME_DOWN,
            SignalCondition.UNAVAILABLE,
            "the run published no readable state and liveness",
        )
    elif alive:
        runtime_down = AlertSignalReading(
            AlertType.RUNTIME_DOWN,
            SignalCondition.INACTIVE,
            "the writer lease is held, so the Paper runtime is alive",
        )
    elif terminal:
        runtime_down = AlertSignalReading(
            AlertType.RUNTIME_DOWN,
            SignalCondition.INACTIVE,
            f"the writer lease is released and the attempt ended as {observation.state}",
        )
    else:
        runtime_down = AlertSignalReading(
            AlertType.RUNTIME_DOWN,
            SignalCondition.ACTIVE,
            f"the writer lease is released while the attempt still reads {observation.state}",
        )

    # not_ready_beyond_threshold -- readiness is PPV-03's verdict, read and never
    # recomputed. While the runtime is not alive readiness is not assessable, and
    # runtime_down already carries that incident.
    if alive is None:
        not_ready = AlertSignalReading(
            AlertType.NOT_READY_BEYOND_THRESHOLD,
            SignalCondition.UNAVAILABLE,
            "the run published no readable liveness, so readiness is not assessable",
        )
    elif not alive:
        not_ready = AlertSignalReading(
            AlertType.NOT_READY_BEYOND_THRESHOLD,
            SignalCondition.UNAVAILABLE,
            "the writer lease is released, so readiness is not assessable",
        )
    elif observation.health is None:
        not_ready = AlertSignalReading(
            AlertType.NOT_READY_BEYOND_THRESHOLD,
            SignalCondition.UNAVAILABLE,
            observation.detail,
        )
    elif observation.health.runtime_ready:
        not_ready = AlertSignalReading(
            AlertType.NOT_READY_BEYOND_THRESHOLD,
            SignalCondition.INACTIVE,
            "the runtime is alive and its projection reports it ready",
        )
    else:
        codes = ", ".join(observation.health.reason_codes[:_NOT_READY_REASON_LIMIT])
        not_ready = AlertSignalReading(
            AlertType.NOT_READY_BEYOND_THRESHOLD,
            SignalCondition.ACTIVE,
            f"alive but not ready: {codes}" if codes else "alive but not ready",
        )

    # reconciliation_conflict -- the projection's own reconciliation judgment.
    if observation.health is None:
        reconciliation = AlertSignalReading(
            AlertType.RECONCILIATION_CONFLICT,
            SignalCondition.UNAVAILABLE,
            observation.detail,
        )
    else:
        state = observation.health.reconciliation_state
        if state is ReconciliationState.CLEAN:
            reconciliation = AlertSignalReading(
                AlertType.RECONCILIATION_CONFLICT,
                SignalCondition.INACTIVE,
                "the projection reports no unresolved reconciliation references",
            )
        elif state is ReconciliationState.UNRESOLVED:
            reconciliation = AlertSignalReading(
                AlertType.RECONCILIATION_CONFLICT,
                SignalCondition.ACTIVE,
                "the projection reports unresolved broker reconciliation references",
            )
        else:
            reconciliation = AlertSignalReading(
                AlertType.RECONCILIATION_CONFLICT,
                SignalCondition.UNAVAILABLE,
                "the projection reports reconciliation as unknown",
            )

    # operator_halt_active -- the LIVE projection inside the health block. The
    # durable outputs/kill-switch.json is written once at startup while the
    # switch is still active and is never re-persisted after a halt, so reading
    # it would report "not halted" during a real halt.
    if observation.health is None:
        halt = AlertSignalReading(
            AlertType.OPERATOR_HALT_ACTIVE, SignalCondition.UNAVAILABLE, observation.detail
        )
    else:
        switch = observation.health.kill_switch_state
        if switch is KillSwitchProjectionState.HALTED:
            halt = AlertSignalReading(
                AlertType.OPERATOR_HALT_ACTIVE,
                SignalCondition.ACTIVE,
                "the live health projection reports the operator halt engaged",
            )
        elif switch is KillSwitchProjectionState.ACTIVE:
            halt = AlertSignalReading(
                AlertType.OPERATOR_HALT_ACTIVE,
                SignalCondition.INACTIVE,
                "the live health projection reports the operator halt not engaged",
            )
        else:
            halt = AlertSignalReading(
                AlertType.OPERATOR_HALT_ACTIVE,
                SignalCondition.UNAVAILABLE,
                "no operator kill-switch authority is configured, so the halt is not "
                "observable rather than healthy",
            )

    # unexpected_restart -- the run's own durable statement that it appended
    # run.terminal. While the attempt is still running no terminal record is
    # expected, so the signal is not assessable rather than false.
    if alive is None:
        restart = AlertSignalReading(
            AlertType.UNEXPECTED_RESTART,
            SignalCondition.UNAVAILABLE,
            "the run published no readable liveness, so its ending is not assessable",
        )
    elif alive:
        restart = AlertSignalReading(
            AlertType.UNEXPECTED_RESTART,
            SignalCondition.UNAVAILABLE,
            "the attempt is still running, so no terminal record is expected yet",
        )
    elif observation.terminal_durable is True:
        restart = AlertSignalReading(
            AlertType.UNEXPECTED_RESTART,
            SignalCondition.INACTIVE,
            "the attempt durably recorded its terminal journal record before it ended",
        )
    elif observation.terminal_durable is False:
        restart = AlertSignalReading(
            AlertType.UNEXPECTED_RESTART,
            SignalCondition.ACTIVE,
            "the attempt ended and durably recorded that it did not write run.terminal",
        )
    else:
        restart = AlertSignalReading(
            AlertType.UNEXPECTED_RESTART,
            SignalCondition.ACTIVE,
            "the attempt ended without recording a durable terminal state",
        )

    return [runtime_down, not_ready, reconciliation, halt, restart]


def observe_backup_staleness(
    backup_root: Path | None, *, now: datetime, thresholds: AlertThresholds
) -> AlertSignalReading:
    """Age the newest VERIFIED backup through PPV-05's own verification.

    No verified backup means the age fact does not exist, so the signal is
    unavailable rather than inactive: reporting "backups are fine" for a host
    with no backups would be exactly the lie this module exists to prevent.
    """
    if backup_root is None:
        return AlertSignalReading(
            AlertType.BACKUP_STALE,
            SignalCondition.UNAVAILABLE,
            "no backup root is configured, so no backup age can be observed",
        )
    try:
        latest = latest_verified_paper_backup(backup_root)
    except PaperBackupError as error:
        return AlertSignalReading(
            AlertType.BACKUP_STALE,
            SignalCondition.UNAVAILABLE,
            f"the backup root is not readable: {type(error).__name__}",
        )
    except Exception as error:  # noqa: BLE001 - an observation never fails a run
        return AlertSignalReading(
            AlertType.BACKUP_STALE,
            SignalCondition.UNAVAILABLE,
            f"the backup root could not be observed: {type(error).__name__}",
        )
    if latest is None:
        return AlertSignalReading(
            AlertType.BACKUP_STALE,
            SignalCondition.UNAVAILABLE,
            "no verified backup exists under the configured backup root",
        )
    age_seconds = (now - latest.created_at).total_seconds()
    if age_seconds > thresholds.backup_stale_seconds:
        return AlertSignalReading(
            AlertType.BACKUP_STALE,
            SignalCondition.ACTIVE,
            f"the newest verified backup is {age_seconds:.0f}s old, beyond the "
            f"{thresholds.backup_stale_seconds:.0f}s alerting policy",
        )
    return AlertSignalReading(
        AlertType.BACKUP_STALE,
        SignalCondition.INACTIVE,
        f"the newest verified backup is {age_seconds:.0f}s old",
    )


def observe_disk_capacity(
    disk_path: Path | None, *, thresholds: AlertThresholds
) -> AlertSignalReading:
    """Read free space from the filesystem holding Paper state.

    ``fstatvfs`` on a descriptor opened ``O_NOFOLLOW`` keeps this a pure read of
    the running filesystem: no path is created, followed or written.
    """
    if disk_path is None:
        return AlertSignalReading(
            AlertType.DISK_CAPACITY_LOW,
            SignalCondition.UNAVAILABLE,
            "no Paper state path is available to observe",
        )
    descriptor: int | None = None
    try:
        descriptor = os.open(disk_path, _DIR_FLAGS)
        stats = os.fstatvfs(descriptor)
        unit = stats.f_frsize or stats.f_bsize
        free_bytes = stats.f_bavail * unit
    except OSError as error:
        return AlertSignalReading(
            AlertType.DISK_CAPACITY_LOW,
            SignalCondition.UNAVAILABLE,
            f"free space could not be observed: {type(error).__name__}",
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if free_bytes < thresholds.disk_free_floor_bytes:
        return AlertSignalReading(
            AlertType.DISK_CAPACITY_LOW,
            SignalCondition.ACTIVE,
            f"{free_bytes} bytes are free, below the "
            f"{thresholds.disk_free_floor_bytes} byte alerting policy",
        )
    return AlertSignalReading(
        AlertType.DISK_CAPACITY_LOW,
        SignalCondition.INACTIVE,
        f"{free_bytes} bytes are free",
    )


_UNAVAILABLE_BACKUP_FAILED = AlertSignalReading(
    AlertType.BACKUP_FAILED,
    SignalCondition.UNAVAILABLE,
    "no durable per-attempt backup outcome record exists to read; backup_stale is "
    "the observable proxy for backups that are not succeeding",
)

_UNAVAILABLE_BROKER = AlertSignalReading(
    AlertType.BROKER_UNAVAILABLE,
    SignalCondition.UNAVAILABLE,
    "PaperBroker.available returns a constant true with no provider until PPV-17, "
    "so there is no fact to observe and no threshold is built over a constant",
)


@final
@dataclass(frozen=True, slots=True)
class PaperAlertEvaluation:
    """One evaluation's complete reading set, before dedup and publication."""

    observed_at: datetime
    readings: tuple[AlertSignalReading, ...]
    run_id: RunId | None = None
    account_id: str | None = None

    def __post_init__(self) -> None:
        try:
            require_utc(self.observed_at, field="observed_at")
        except TimeValidationError as error:
            raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error
        if type(self.readings) is not tuple or any(
            type(reading) is not AlertSignalReading for reading in self.readings
        ):
            raise _fail(OutcomeCode.INVALID_TYPE, "readings must be one tuple of exact readings")
        if tuple(reading.type for reading in self.readings) != tuple(
            spec.type for spec in ALERT_SIGNALS
        ):
            raise _fail(
                OutcomeCode.CONFLICTING_ID,
                "an evaluation must read every signal exactly once, in canonical order",
            )
        if self.run_id is not None and type(self.run_id) is not RunId:
            raise _fail(OutcomeCode.INVALID_TYPE, "run_id must be an exact RunId or None")
        if self.account_id is not None and type(self.account_id) is not str:
            raise _fail(OutcomeCode.INVALID_TYPE, "account_id must be a str or None")

    def project(
        self,
        *,
        host_id: str,
        previous: PaperAlertStream | None = None,
        thresholds: AlertThresholds = DEFAULT_ALERT_THRESHOLDS,
    ) -> PaperAlertStream:
        """Fold these readings into a durable stream, deduplicating by identity."""
        return project_alert_stream(
            host_id=host_id,
            readings=self.readings,
            observed_at=self.observed_at,
            run_id=self.run_id,
            account_id=self.account_id,
            previous=previous,
            thresholds=thresholds,
        )


def evaluate_paper_alerts(
    *,
    run_dir: Path,
    clock: Clock,
    backup_root: Path | None = None,
    disk_path: Path | None = None,
    thresholds: AlertThresholds = DEFAULT_ALERT_THRESHOLDS,
) -> PaperAlertEvaluation:
    """Observe all nine signals once, without mutating anything anywhere.

    Every observation failure becomes an explicit ``unavailable`` reading. This
    function raises only for caller input that is not a valid request at all; a
    missing attempt, an absent backup root or an unreadable filesystem are all
    observations, not errors.
    """
    if not isinstance(run_dir, Path) or not run_dir.is_absolute():
        raise _fail(OutcomeCode.INVALID_TYPE, "run_dir must be an absolute Path")
    if type(thresholds) is not AlertThresholds:
        raise _fail(OutcomeCode.INVALID_TYPE, "thresholds must be exact alerting policy")
    try:
        now = require_utc(clock.now(), field="now")
    except TimeValidationError as error:
        raise _fail(OutcomeCode.OUT_OF_RANGE, str(error)) from error

    run = observe_paper_run(run_dir, now=now)
    readings = _read_run_signals(run)
    readings.append(observe_backup_staleness(backup_root, now=now, thresholds=thresholds))
    readings.append(
        observe_disk_capacity(run_dir if disk_path is None else disk_path, thresholds=thresholds)
    )
    readings.append(_UNAVAILABLE_BACKUP_FAILED)
    readings.append(_UNAVAILABLE_BROKER)

    # Restore canonical catalog order, which the published contract requires.
    order = {spec.type: rank for rank, spec in enumerate(ALERT_SIGNALS)}
    ordered = tuple(sorted(readings, key=lambda reading: order[reading.type]))
    return PaperAlertEvaluation(
        observed_at=now, readings=ordered, run_id=run.run_id, account_id=run.account_id
    )


__all__ = [
    "PaperAlertEvaluation",
    "PaperRunObservation",
    "evaluate_paper_alerts",
    "observe_backup_staleness",
    "observe_disk_capacity",
    "observe_paper_run",
]
