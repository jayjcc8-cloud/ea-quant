"""Explicit observation and offline readback of local Paper intervals."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from ea.product.paper_evidence import GateType, GateVerdict
from ea.product.paper_interval_capture import (
    begin_interval,
    evaluate_interval,
    observe_interval,
    settle_interval,
)
from ea.strategy.package import canonical_json

interval_app = typer.Typer(
    help="Save actual interval observations, seal before stop, bind settled evidence."
)


def _run(operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        document = operation()
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        typer.echo(f"Paper interval unavailable: {error}", err=True)
        raise typer.Exit(code=3) from None
    typer.echo(canonical_json(document).decode("ascii"))
    return document


@interval_app.command("begin")
def begin(
    interval: Annotated[Path, typer.Option("--interval")],
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    repository: Annotated[Path, typer.Option("--repo")],
    config: Annotated[Path, typer.Option("--config")],
    launchd_dir: Annotated[Path, typer.Option("--launchd-dir")],
    gate_id: Annotated[str, typer.Option("--gate-id")],
    gate_type: Annotated[GateType, typer.Option("--gate-type")],
    backup_root: Annotated[Path | None, typer.Option("--backup-root")] = None,
    alert_stream: Annotated[Path | None, typer.Option("--alert-stream")] = None,
    current_run: Annotated[Path | None, typer.Option("--current-run")] = None,
    launch_history: Annotated[Path | None, typer.Option("--launch-history")] = None,
) -> None:
    """Begin from a real running observation. No timestamp or health override."""
    _run(
        lambda: begin_interval(
            interval.expanduser().absolute(),
            run_dir=run_dir.expanduser().absolute(),
            repository=repository.expanduser().absolute(),
            config=config.expanduser().absolute(),
            launchd_dir=launchd_dir.expanduser().absolute(),
            gate_id=gate_id,
            gate_type=gate_type,
            backup_root=backup_root,
            alert_stream=alert_stream,
            current_run=current_run,
            launch_history=launch_history,
        )
    )


@interval_app.command("observe")
def observe(interval: Annotated[Path, typer.Option("--interval")]) -> None:
    """Append actual health, identity and complete attempt inventory."""
    _run(lambda: observe_interval(interval.expanduser().absolute()))


@interval_app.command("end")
def end(interval: Annotated[Path, typer.Option("--interval")]) -> None:
    """Seal the actual boundary once, while the attempt is still running."""
    _run(lambda: observe_interval(interval.expanduser().absolute(), end=True))


@interval_app.command("settle")
def settle(
    interval: Annotated[Path, typer.Option("--interval")],
    backups: Annotated[
        list[Path] | None,
        typer.Option(
            "--backup", help="One explicit verified backup per attempt; repeat for all attempts."
        ),
    ] = None,
) -> None:
    """After ordinary stop/backup, save the complete history once. Starts nothing."""
    _run(
        lambda: settle_interval(
            interval.expanduser().absolute(),
            [path.expanduser().absolute() for path in backups or []],
        )
    )


@interval_app.command("evaluate")
def evaluate(interval: Annotated[Path, typer.Option("--interval")]) -> None:
    """Read the sealed history at ended_at; label current status separately."""
    document = _run(lambda: evaluate_interval(interval.expanduser().absolute()))
    if document["historical_verdict"]["verdict"] != GateVerdict.PASS.value:
        raise typer.Exit(code=3)
