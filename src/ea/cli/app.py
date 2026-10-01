from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

import typer

from ea import __version__
from ea.config import ConfigurationError, load_configuration
from ea.config.diagnostics import escape_diagnostic_label
from ea.product import (
    BacktestReportError,
    BacktestResumeFailure,
    BacktestRunError,
    BacktestRunFailure,
    BacktestScenarioError,
    OfflineDemoFailure,
    OfflineDemoInputError,
    generate_backtest_report,
    load_backtest_scenario,
    resume_backtest_attempt,
    run_backtest_scenario,
    run_offline_demo,
)
from ea.strategy.package import (
    StrategyPackageError,
    canonical_json,
    pack_strategy,
    read_regular,
    validate_package,
)

if TYPE_CHECKING:
    from ea.product.paper_evidence import PaperSnapshot

app = typer.Typer(
    help="EA quantitative trading system CLI.",
    context_settings={"token_normalize_func": escape_diagnostic_label},
)
backtest_app = typer.Typer(
    help=("Strict scenarios support validate, run, resume, and report; the RESET demo is run-only.")
)
web_app = typer.Typer(help="Serve the installed offline backtest UI on 127.0.0.1 only.")
app.add_typer(backtest_app, name="backtest")
app.add_typer(web_app, name="web")
strategy_app = typer.Typer(help="Pack, validate and inspect trusted local strategy artifacts.")
app.add_typer(strategy_app, name="strategy")
candidate_app = typer.Typer(
    help="Inspect accepted candidates and run their fixed offline configuration."
)
app.add_typer(candidate_app, name="candidate")
paper_app = typer.Typer(help="Explicit local simulated Paper start, status and cooperative stop.")
app.add_typer(paper_app, name="paper")
backup_app = typer.Typer(
    help="Quiesced consistent backup, verification and isolated restore of Paper attempts.",
    invoke_without_command=True,
)
paper_app.add_typer(backup_app, name="backup")
alerts_app = typer.Typer(
    help="Observe the nine Paper signals and read the host alert stream. Observation only.",
    invoke_without_command=True,
)
paper_app.add_typer(alerts_app, name="alerts")
gate_app = typer.Typer(
    help="Lock one gate identity and judge the current runtime against it. Starts no gate.",
)
paper_app.add_typer(gate_app, name="gate")


data_app = typer.Typer(help="Inspect strict full-capture local OHLCV input.")
app.add_typer(data_app, name="data")


@data_app.command("inspect")
def data_inspect(path: Path) -> None:
    """Print validated full-capture provenance and canonical data identity."""
    from ea.web.datasets import LocalResearchDatasetRegistryV1

    try:
        absolute = path.absolute()
        result = LocalResearchDatasetRegistryV1(absolute.parent).inspect(absolute.name)
        result.pop("dataset_id")
        result.pop("valid")
        typer.echo(canonical_json(result).decode("ascii"))
    except (OSError, ValueError):
        typer.echo("data input error: invalid or unavailable strict OHLCV capture", err=True)
        raise typer.Exit(code=2) from None


@dataclass(frozen=True, slots=True)
class CliConfiguration:
    config_path: str | None
    environment: str | None
    run_mode: str | None


def _single_cli_option[T](
    option_name: str,
    values: list[T] | None,
) -> T | None:
    if not values:
        return None
    if len(values) > 1:
        typer.echo(
            f"configuration error: CLI option '{option_name}' may be specified only once",
            err=True,
        )
        raise typer.Exit(code=2)
    return values[0]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    context: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the installed ea-quant version and exit.",
        ),
    ] = False,
    config_path: Annotated[
        list[str] | None,
        typer.Option(
            "--config",
            help="YAML configuration path; overrides EA_CONFIG_PATH.",
            metavar="PATH",
        ),
    ] = None,
    environment: Annotated[
        list[str] | None,
        typer.Option("--environment", help="Override the typed environment."),
    ] = None,
    run_mode: Annotated[
        list[str] | None,
        typer.Option("--run-mode", help="Override the typed run mode."),
    ] = None,
) -> None:
    """EA quantitative trading system command group."""
    context.obj = CliConfiguration(
        config_path=_single_cli_option("--config", config_path),
        environment=_single_cli_option("--environment", environment),
        run_mode=_single_cli_option("--run-mode", run_mode),
    )


@app.command(context_settings={"allow_extra_args": True})
def doctor(context: typer.Context) -> None:
    """Print local project health information."""
    if context.args:
        typer.echo("configuration error: unexpected positional arguments", err=True)
        raise typer.Exit(code=2)

    cli_configuration = cast(CliConfiguration, context.obj)
    try:
        loaded = load_configuration(
            config_path=cli_configuration.config_path,
            environment=cli_configuration.environment,
            run_mode=cli_configuration.run_mode,
        )
    except ConfigurationError as error:
        typer.echo(f"configuration error: {error}", err=True)
        raise typer.Exit(code=2) from None

    typer.echo("EA system doctor")
    typer.echo(f"python: {platform.python_version()}")
    config_label = (
        escape_diagnostic_label(loaded.config_path) if loaded.config_path else "<defaults>"
    )
    typer.echo(f"config file: {config_label}")
    typer.echo(f"schema version: {loaded.snapshot.schema_version}")
    typer.echo(f"environment: {loaded.snapshot.environment.value}")
    typer.echo(f"run mode: {loaded.snapshot.run.mode.value}")
    typer.echo("live profile: unavailable")


@backtest_app.command("run")
def run(
    output_root: Annotated[
        Path,
        typer.Option(
            "--output-root",
            help=(
                "Parent directory; strict runs create UUID attempt directories, "
                "while the RESET demo writes phase1-demo-v1."
            ),
            metavar="DIR",
        ),
    ],
    scenario: Annotated[
        Path | None,
        typer.Option(
            "--scenario",
            help="Strict BacktestScenario v1 YAML file.",
            metavar="FILE",
        ),
    ] = None,
    strategy_root: Annotated[Path | None, typer.Option("--strategy-root")] = None,
) -> None:
    """Run one strict scenario, or the run-only RESET demo when --scenario is omitted."""
    try:
        resolved_output = output_root.expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        typer.echo("demo input error: output root cannot be resolved", err=True)
        raise typer.Exit(code=2) from None
    if scenario is None:
        try:
            demo_completed = run_offline_demo(resolved_output)
        except OfflineDemoInputError as error:
            typer.echo(f"demo input error: {error}", err=True)
            raise typer.Exit(code=2) from None
        except OfflineDemoFailure as error:
            typer.echo(f"demo failed closed: {error.code.value}", err=True)
            typer.echo(f"evidence: {error.output_directory}", err=True)
            raise typer.Exit(code=3) from None
        except Exception:
            typer.echo("demo internal error", err=True)
            raise typer.Exit(code=1) from None
        typer.echo(f"offline demo: {demo_completed.status}")
        result_path = demo_completed.output_directory / "result.json"
    else:
        try:
            loaded = load_backtest_scenario(scenario, strategy_root=strategy_root)
            backtest_completed = run_backtest_scenario(loaded, resolved_output)
        except BacktestScenarioError as error:
            typer.echo(f"scenario validation error: {error}", err=True)
            raise typer.Exit(code=2) from None
        except BacktestRunError as error:
            typer.echo(f"backtest input error: {error}", err=True)
            raise typer.Exit(code=2) from None
        except BacktestRunFailure as error:
            typer.echo(f"backtest failed closed: {error.code.value}", err=True)
            typer.echo(f"evidence: {error.output_directory}", err=True)
            raise typer.Exit(code=3) from None
        except Exception:
            typer.echo("backtest internal error", err=True)
            raise typer.Exit(code=1) from None
        typer.echo(f"backtest: {backtest_completed.status}")
        result_path = backtest_completed.output_directory / "result.json"
    typer.echo(f"result: {result_path}")
    typer.echo("live capability: unavailable")


@backtest_app.command("validate")
def validate(
    scenario: Annotated[
        Path,
        typer.Option(
            "--scenario",
            help="Strict BacktestScenario v1 YAML file.",
            metavar="FILE",
        ),
    ],
    strategy_root: Annotated[Path | None, typer.Option("--strategy-root")] = None,
) -> None:
    """Validate one scenario and its selected local OHLCV without executing it."""
    try:
        loaded = load_backtest_scenario(scenario, strategy_root=strategy_root)
    except BacktestScenarioError as error:
        typer.echo(f"scenario validation error: {error}", err=True)
        raise typer.Exit(code=2) from None
    except Exception:
        typer.echo("scenario validation internal error", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"scenario valid: BacktestScenario v{loaded.schema_version}")


@web_app.command("serve")
def web_serve(
    scenario_root: Annotated[
        Path,
        typer.Option(
            "--scenario-root",
            help="Authorized directory containing strict scenario YAML and local OHLCV.",
            metavar="DIR",
        ),
    ],
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            help="Dedicated local Web job, attempt, and report directory.",
            metavar="DIR",
        ),
    ],
    ui_dir: Annotated[
        Path,
        typer.Option(
            "--ui-dir",
            help="Built React dist directory; this is the only static file root.",
            metavar="DIR",
        ),
    ],
    port: Annotated[
        int,
        typer.Option(
            "--port",
            min=1024,
            max=65535,
            help="Loopback HTTP port on fixed host 127.0.0.1.",
        ),
    ] = 8765,
    strategy_root: Annotated[Path | None, typer.Option("--strategy-root")] = None,
    data_root: Annotated[Path | None, typer.Option("--data-root")] = None,
) -> None:
    """Serve the real offline Web backtest loop from explicit local roots."""
    try:
        resolved_scenarios = scenario_root.expanduser().resolve(strict=True)
        resolved_ui = ui_dir.expanduser().resolve(strict=True)
        resolved_workspace = workspace.expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        typer.echo("web input error: local roots cannot be resolved", err=True)
        raise typer.Exit(code=2) from None
    roots = (resolved_scenarios, resolved_workspace, resolved_ui)
    if any(
        left.is_relative_to(right) or right.is_relative_to(left)
        for index, left in enumerate(roots)
        for right in roots[index + 1 :]
    ):
        typer.echo("web input error: scenario, workspace, and UI roots must not overlap", err=True)
        raise typer.Exit(code=2)
    try:
        from ea.web.app import WebSettings
        from ea.web.server import WebDependencyError, serve_local_web

        serve_local_web(
            WebSettings(
                scenario_root=resolved_scenarios,
                workspace=resolved_workspace,
                ui_dir=resolved_ui,
                port=port,
                strategy_root=strategy_root,
                data_root=data_root,
            )
        )
    except WebDependencyError as error:
        typer.echo(f"web dependency error: {error}", err=True)
        raise typer.Exit(code=2) from None
    except Exception:
        typer.echo("web service failed to start", err=True)
        raise typer.Exit(code=1) from None


@backtest_app.command("resume")
def resume(
    run_dir: Annotated[
        Path,
        typer.Option(
            "--run-dir",
            help="Existing strict Backtest attempt directory.",
            metavar="ATTEMPT_DIR",
        ),
    ],
) -> None:
    """Resume one verified supported frontier of an existing Backtest attempt."""
    try:
        resolved = run_dir.expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        typer.echo("backtest resume input error: run directory cannot be resolved", err=True)
        raise typer.Exit(code=2) from None
    try:
        completed = resume_backtest_attempt(resolved)
    except BacktestResumeFailure as error:
        typer.echo("backtest resume failed closed", err=True)
        typer.echo(f"evidence: {error.output_directory}", err=True)
        raise typer.Exit(code=3) from None
    except Exception:
        typer.echo("backtest resume internal error", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"backtest resume: {completed.status}")
    typer.echo(f"result: {completed.output_directory / 'result.json'}")
    typer.echo("live capability: unavailable")


@backtest_app.command("report")
def report(
    run_dir: Annotated[
        Path,
        typer.Option(
            "--run-dir",
            help="Completed strict Backtest attempt directory to read without execution.",
            metavar="ATTEMPT_DIR",
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            help="Separate directory for canonical report.json and summary.txt.",
            metavar="REPORT_DIR",
        ),
    ],
) -> None:
    """Generate one deterministic report from verified completed evidence."""
    try:
        resolved_run = run_dir.expanduser().resolve(strict=True)
        resolved_output = output_dir.expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        typer.echo("backtest report input error", err=True)
        raise typer.Exit(code=2) from None
    try:
        completed = generate_backtest_report(resolved_run, resolved_output)
    except BacktestReportError:
        typer.echo("backtest report failed closed", err=True)
        raise typer.Exit(code=3) from None
    except Exception:
        typer.echo("backtest report internal error", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"backtest report: {completed.status}")
    typer.echo(f"report: {completed.output_directory / 'report.json'}")
    typer.echo(f"summary: {completed.output_directory / 'summary.txt'}")
    typer.echo("source attempt: read-only")


@candidate_app.command("inspect")
def candidate_inspect(
    workspace: Annotated[Path, typer.Option("--workspace")],
    candidate_id: Annotated[str, typer.Option("--candidate-id")],
) -> None:
    """Read accepted identity and fresh evidence without executing strategy code."""
    from ea.product.candidate import inspect_candidate_binding

    try:
        binding = inspect_candidate_binding(
            workspace.expanduser().resolve(strict=True), candidate_id
        )
    except (OSError, ValueError, KeyError, TypeError):
        typer.echo("candidate rejected: accepted identity and intact evidence required", err=True)
        raise typer.Exit(code=3) from None
    typer.echo(canonical_json(binding.document()).decode("ascii"))


@candidate_app.command("run")
def candidate_run(
    workspace: Annotated[Path, typer.Option("--workspace")],
    candidate_id: Annotated[str, typer.Option("--candidate-id")],
    scenario: Annotated[Path, typer.Option("--scenario")],
    output_root: Annotated[Path, typer.Option("--output-root")],
) -> None:
    """Guard the accepted artifact/configuration before loading an offline strategy."""
    from ea.product.candidate import inspect_candidate_binding, run_accepted_candidate

    try:
        binding = inspect_candidate_binding(
            workspace.expanduser().resolve(strict=True), candidate_id
        )
        completed = run_accepted_candidate(
            binding,
            scenario.expanduser().absolute(),
            output_root.expanduser().absolute(),
        )
    except BacktestRunFailure as error:
        typer.echo(f"candidate backtest failed closed: {error.code.value}", err=True)
        typer.echo(f"evidence: {error.output_directory}", err=True)
        raise typer.Exit(code=3) from None
    except (OSError, ValueError, KeyError, TypeError, BacktestScenarioError, BacktestRunError):
        typer.echo(
            "candidate rejected: accepted identity and matching configuration required", err=True
        )
        raise typer.Exit(code=3) from None
    typer.echo(f"candidate backtest: {completed.status}")
    typer.echo(f"result: {completed.output_directory / 'result.json'}")
    typer.echo("live capability: unavailable")


@paper_app.command("start")
def paper_start(
    context: typer.Context,
    workspace: Annotated[Path, typer.Option("--workspace")],
    candidate_id: Annotated[str, typer.Option("--candidate-id")],
    scenario: Annotated[Path, typer.Option("--scenario")],
    output_root: Annotated[Path, typer.Option("--output-root")],
    run_id: Annotated[str | None, typer.Option("--run-id")] = None,
    prices: Annotated[
        str, typer.Option("--prices", help="Fixed simulated price cycle, comma-separated.")
    ] = "100",
    interval: Annotated[float, typer.Option("--interval", min=0.01, max=10.0)] = 0.1,
    event_limit: Annotated[int | None, typer.Option("--event-limit", min=1)] = None,
) -> None:
    """Start a fresh accepted Candidate in the local Paper simulator until stopped."""
    config = cast(CliConfiguration, context.obj)
    modes = (config.run_mode, os.environ.get("EA_RUN_MODE"))
    if any(value is not None and value != "paper" for value in modes) or any(
        value is not None
        for value in (
            config.config_path,
            config.environment,
            os.environ.get("EA_CONFIG_PATH"),
            os.environ.get("EA_ENVIRONMENT"),
        )
    ):
        typer.echo(
            "Paper rejects Live and global configuration overrides; "
            "use this command's local inputs.",
            err=True,
        )
        raise typer.Exit(code=2)
    from ea.core import RunId
    from ea.product.paper_run import run_local_paper

    try:
        completed = run_local_paper(
            workspace,
            candidate_id,
            scenario,
            output_root,
            prices=tuple(float(value) for value in prices.split(",")),
            interval=interval,
            event_limit=event_limit,
            run_id=None if run_id is None else RunId(run_id),
            on_ready=lambda path: typer.echo(
                canonical_json({"paper_started": str(path)}).decode("ascii")
            ),
        )
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        typer.echo(f"Paper rejected: {type(error).__name__}: {error}", err=True)
        raise typer.Exit(code=3) from None
    typer.echo(canonical_json(completed).decode("ascii"))
    if completed["state"] != "stopped":
        raise typer.Exit(code=3)


@paper_app.command("status")
def paper_status(run_dir: Annotated[Path, typer.Option("--run-dir")]) -> None:
    """Read acknowledged money and current writer-lease presence without mutation."""
    from ea.product.paper_session import PaperSessionError, read_paper_status

    try:
        document = read_paper_status(run_dir.expanduser().absolute())
    except PaperSessionError as error:
        typer.echo(f"Paper status unavailable: {error}", err=True)
        raise typer.Exit(code=3) from None
    typer.echo(canonical_json(document).decode("ascii"))


@paper_app.command("stop")
def paper_stop(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    timeout: Annotated[float, typer.Option("--timeout", min=0.0)] = 10.0,
) -> None:
    """Request no new market decisions and wait for terminal state and lease release."""
    from ea.product.paper_session import PaperSessionError, request_paper_stop

    try:
        document = request_paper_stop(run_dir.expanduser().absolute(), timeout_seconds=timeout)
    except PaperSessionError as error:
        typer.echo(f"Paper stop incomplete: {error}", err=True)
        raise typer.Exit(code=3) from None
    typer.echo(canonical_json(document).decode("ascii"))


@paper_app.command("resume")
def paper_resume(run_dir: Annotated[Path, typer.Option("--run-dir")]) -> None:
    """Reopen an existing attempt and admit or reject continued trading (no resend)."""
    from ea.product.paper_run import resume_local_paper

    try:
        document = resume_local_paper(run_dir.expanduser().absolute())
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        typer.echo(f"Paper resume rejected: {type(error).__name__}: {error}", err=True)
        raise typer.Exit(code=3) from None
    typer.echo(canonical_json(document).decode("ascii"))
    if document["reconciliation_required"]:
        raise typer.Exit(code=3)


def _paper_backup_failure(operation: str, error: Exception) -> typer.Exit:
    """Map one backup refusal onto the shared Paper exit-code contract."""
    from ea.product.paper_backup import PaperBackupInputError, PaperBackupRefused

    if isinstance(error, PaperBackupInputError):
        typer.echo(f"Paper {operation} input error: {error}", err=True)
        return typer.Exit(code=2)
    if isinstance(error, PaperBackupRefused):
        typer.echo(f"Paper {operation} refused: {error}", err=True)
        return typer.Exit(code=3)
    typer.echo(f"Paper {operation} failed: {type(error).__name__}: {error}", err=True)
    return typer.Exit(code=1)


@backup_app.callback()
def paper_backup(
    context: typer.Context,
    run_dir: Annotated[Path | None, typer.Option("--run-dir")] = None,
    backup_root: Annotated[Path | None, typer.Option("--backup-root")] = None,
    keep: Annotated[int, typer.Option("--keep", min=1)] = 5,
) -> None:
    """Capture one quiesced attempt; `ea paper backup inspect` verifies an existing one."""
    if context.invoked_subcommand is not None:
        return
    if run_dir is None or backup_root is None:
        typer.echo(
            "Paper backup requires --run-dir and --backup-root, or the inspect subcommand.",
            err=True,
        )
        raise typer.Exit(code=2)
    from ea.product.paper_backup import PaperBackupError, create_paper_backup

    try:
        result = create_paper_backup(
            run_dir.expanduser().absolute(),
            backup_root.expanduser().absolute(),
            keep=keep,
        )
    except PaperBackupError as error:
        raise _paper_backup_failure("backup", error) from None
    typer.echo(canonical_json(result.document()).decode("ascii"))


@backup_app.command("inspect")
def paper_backup_inspect(backup: Annotated[Path, typer.Option("--backup")]) -> None:
    """Verify one backup is internally consistent and print its published manifest.

    ``verified`` means the recorded identity, the captured attempt manifest and
    the captured journal all agree with each other. It is not a signature:
    nothing anchors a backup to a trusted third party.
    """
    from ea.product.paper_backup import PaperBackupError, inspect_paper_backup

    try:
        manifest = inspect_paper_backup(backup.expanduser().absolute())
    except PaperBackupError as error:
        raise _paper_backup_failure("backup inspect", error) from None
    typer.echo(canonical_json({**manifest.document(), "verified": True}).decode("ascii"))


@paper_app.command("restore")
def paper_restore(
    backup: Annotated[Path, typer.Option("--backup")],
    run_dir: Annotated[Path, typer.Option("--run-dir")],
) -> None:
    """Restore one backup into a new isolated attempt; never overwrites a live one."""
    from ea.product.paper_backup import PaperBackupError, restore_paper_backup

    try:
        result = restore_paper_backup(
            backup.expanduser().absolute(), run_dir.expanduser().absolute()
        )
    except PaperBackupError as error:
        raise _paper_backup_failure("restore", error) from None
    typer.echo(canonical_json(result.document()).decode("ascii"))


def _paper_alert_failure(operation: str, error: Exception) -> typer.Exit:
    """Map one alert-boundary failure onto the shared Paper exit-code contract."""
    from ea.product.paper_alerts import PaperAlertUnavailable

    if isinstance(error, PaperAlertUnavailable):
        typer.echo(f"Paper alerts unavailable: {error}", err=True)
        return typer.Exit(code=3)
    typer.echo(f"Paper alerts {operation} input error: {error}", err=True)
    return typer.Exit(code=2)


@alerts_app.callback()
def paper_alerts(context: typer.Context) -> None:
    """Observe the nine signals into the durable host stream, or read one back."""
    if context.invoked_subcommand is None:
        typer.echo("Paper alerts requires the evaluate or show subcommand.", err=True)
        raise typer.Exit(code=2)


@alerts_app.command("evaluate")
def paper_alerts_evaluate(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    stream: Annotated[Path, typer.Option("--stream")],
    backup_root: Annotated[Path | None, typer.Option("--backup-root")] = None,
    disk_path: Annotated[Path | None, typer.Option("--disk-path")] = None,
    host_id: Annotated[str | None, typer.Option("--host-id")] = None,
    not_ready_seconds: Annotated[float, typer.Option("--not-ready-seconds", min=0.001)] = 300.0,
    backup_stale_seconds: Annotated[
        float, typer.Option("--backup-stale-seconds", min=0.001)
    ] = 129_600.0,
    disk_free_floor_bytes: Annotated[int, typer.Option("--disk-free-floor-bytes", min=0)] = 5
    * 1024**3,
) -> None:
    """Observe the nine signals once and publish them to the durable host stream.

    Every threshold here is this command's own alerting policy. None of them is a
    safety limit, and none of them can permit, block or resend anything: this
    command observes and delivers, and it is never read by a trading decision.
    """
    from ea.product.market_stream import RealUtcClock
    from ea.product.paper_alert_evaluation import evaluate_paper_alerts
    from ea.product.paper_alerts import (
        AlertThresholds,
        PaperAlertError,
        PaperAlertStreamAbsent,
        PaperAlertUnavailable,
        read_alert_stream,
        require_host_id,
        write_alert_stream,
    )

    try:
        thresholds = AlertThresholds(
            not_ready_seconds=not_ready_seconds,
            backup_stale_seconds=backup_stale_seconds,
            disk_free_floor_bytes=disk_free_floor_bytes,
        )
        scope = require_host_id(platform.node() if host_id is None else host_id)
        absolute_stream = stream.expanduser().absolute()
        absolute_run = run_dir.expanduser().absolute()
    except PaperAlertError as error:
        raise _paper_alert_failure("evaluate", error) from None

    try:
        previous = read_alert_stream(absolute_stream)
    except PaperAlertStreamAbsent:
        previous = None
    except PaperAlertUnavailable as error:
        # A stream that exists but cannot be read is never replaced: overwriting
        # it is how an open alert would silently disappear.
        raise _paper_alert_failure("evaluate", error) from None

    try:
        evaluation = evaluate_paper_alerts(
            run_dir=absolute_run,
            clock=RealUtcClock(),
            backup_root=None if backup_root is None else backup_root.expanduser().absolute(),
            disk_path=None if disk_path is None else disk_path.expanduser().absolute(),
            thresholds=thresholds,
        )
        document = evaluation.project(host_id=scope, previous=previous, thresholds=thresholds)
        write_alert_stream(absolute_stream, document)
    except PaperAlertUnavailable as error:
        raise _paper_alert_failure("evaluate", error) from None
    except PaperAlertError as error:
        raise _paper_alert_failure("evaluate", error) from None
    typer.echo(canonical_json(document.document()).decode("ascii"))


@alerts_app.command("show")
def paper_alerts_show(stream: Annotated[Path, typer.Option("--stream")]) -> None:
    """Print the durable host alert stream without mutation.

    Exit code 3 means the stream is unavailable, never that no alert is open: an
    unreadable stream must not be reported as a healthy, empty one.
    """
    from ea.product.paper_alerts import PaperAlertUnavailable, read_alert_stream

    try:
        document = read_alert_stream(stream.expanduser().absolute())
    except PaperAlertUnavailable as error:
        raise _paper_alert_failure("show", error) from None
    typer.echo(canonical_json(document.document()).decode("ascii"))


@paper_app.command("observer")
def paper_observer(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    alert_stream: Annotated[Path | None, typer.Option("--alert-stream")] = None,
    backup_root: Annotated[Path | None, typer.Option("--backup-root")] = None,
    claude_bin: Annotated[str, typer.Option("--claude-bin")] = os.environ.get(
        "EA_OBSERVER_CLAUDE_BIN", "claude"
    ),
    timeout: Annotated[float, typer.Option("--timeout", min=1.0)] = float(
        os.environ.get("EA_OBSERVER_TIMEOUT_SECONDS", "120")
    ),
    log_lines: Annotated[int, typer.Option("--log-lines", min=1, max=500)] = 200,
) -> None:
    """Run the read-only Claude diagnostic observer over existing Paper evidence.

    Reads the existing Paper status, alert stream, backup and operational-log
    read models, hands one bounded evidence package to a headless tool-less
    ``claude`` turn, and prints the structured assessment. Nothing is written
    anywhere: the observer cannot start, stop, repair or authorize anything.

    Exit code 3 means the Paper evidence could not be read. A missing, failed,
    timed-out or malformed Claude is not a Paper failure: the document reports
    ``observer_status: unavailable`` and this command still exits 0.
    """
    from ea.product.paper_observer import PaperObserverError, run_observer
    from ea.product.paper_session import PaperSessionError

    try:
        document = run_observer(
            run_dir=run_dir.expanduser().absolute(),
            alert_stream_path=None
            if alert_stream is None
            else alert_stream.expanduser().absolute(),
            backup_root=None if backup_root is None else backup_root.expanduser().absolute(),
            claude_bin=Path(claude_bin),
            timeout_seconds=timeout,
            max_log_lines=log_lines,
        )
    except (PaperObserverError, PaperSessionError) as error:
        typer.echo(f"Paper observer unavailable: {error}", err=True)
        raise typer.Exit(code=3) from None
    typer.echo(canonical_json(document).decode("ascii"))


def _collect_paper_snapshot(
    *,
    run_dir: Path,
    repository: Path,
    supervisor_state: str,
    backup_root: Path | None,
    alert_stream: Path | None,
) -> PaperSnapshot:
    """Observe one real WU-2 snapshot; shared by `snapshot` and `gate evaluate`."""
    from ea.product.market_stream import RealUtcClock
    from ea.product.paper_evidence import SupervisorState
    from ea.product.paper_gate_prep import PaperGatePrepError, observe_paper_snapshot
    from ea.product.paper_session import PaperSessionError

    try:
        state = SupervisorState(supervisor_state)
    except ValueError:
        typer.echo(
            f"Paper snapshot input error: supervisor-state must be one of "
            f"{', '.join(member.value for member in SupervisorState)}",
            err=True,
        )
        raise typer.Exit(code=2) from None
    try:
        return observe_paper_snapshot(
            run_dir=run_dir.expanduser().absolute(),
            repository=repository.expanduser().absolute(),
            supervisor_state=state,
            now=RealUtcClock().now(),
            backup_root=None if backup_root is None else backup_root.expanduser().absolute(),
            alert_stream_path=None
            if alert_stream is None
            else alert_stream.expanduser().absolute(),
        )
    except (PaperGatePrepError, PaperSessionError) as error:
        typer.echo(f"Paper snapshot unavailable: {error}", err=True)
        raise typer.Exit(code=3) from None


@paper_app.command("snapshot")
def paper_snapshot(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    repository: Annotated[
        Path, typer.Option("--repo", help="Checkout whose HEAD is the locked repo identity.")
    ],
    supervisor_state: Annotated[
        str, typer.Option("--supervisor-state", help="launchd's own report for the Paper job.")
    ] = "unknown",
    backup_root: Annotated[Path | None, typer.Option("--backup-root")] = None,
    alert_stream: Annotated[Path | None, typer.Option("--alert-stream")] = None,
) -> None:
    """Collect one read-only M4 evidence snapshot from the real local runtime.

    Every input is another owner's existing read: the `ea paper status` health
    projection with liveness re-observed from the live writer lease, the newest
    verified backup, the durable alert stream, the attempt's own recorded profile
    and this checkout's HEAD. Nothing is recomputed, nothing is written, and an
    input that cannot be observed keeps its unavailable value rather than reading
    as healthy. Exit code 3 means the snapshot itself could not be taken.
    """
    snapshot = _collect_paper_snapshot(
        run_dir=run_dir,
        repository=repository,
        supervisor_state=supervisor_state,
        backup_root=backup_root,
        alert_stream=alert_stream,
    )
    typer.echo(canonical_json(snapshot.document()).decode("ascii"))


@gate_app.command("lock")
def paper_gate_lock(
    identity_path: Annotated[
        Path, typer.Option("--identity", help="Where to persist the locked identity.")
    ],
    gate_id: Annotated[str, typer.Option("--gate-id")],
    gate_type: Annotated[str, typer.Option("--gate-type", help="72h or 7d.")],
    repository: Annotated[Path, typer.Option("--repo")],
    config: Annotated[Path, typer.Option("--config", help="The operator run-config.env to pin.")],
    launchd_dir: Annotated[
        Path, typer.Option("--launchd-dir", help="Where the LaunchAgents were rendered.")
    ],
    started_at: Annotated[
        str | None, typer.Option("--started-at", help="UTC ISO-8601 instant to record.")
    ] = None,
) -> None:
    """Materialise and durably persist one M4 gate identity, exactly once.

    The identity is rebuilt from the real inputs -- HEAD, the operator config
    bytes and the rendered LaunchAgent bytes -- and is written exclusively: an
    identity that is already locked is never replaced, because drift must become
    an INVALID verdict, not a silent relock.

    Locking an identity does not start a gate. This product owns no timer, no
    clock and no gate state, so nothing begins when this file is written; the
    72-hour and 7-day Gates are started by the M4 RC freeze, a separate
    authorization, and are judged later by `ea paper gate evaluate`.
    """
    from ea.core.time import require_utc
    from ea.product.market_stream import RealUtcClock
    from ea.product.paper_evidence import GateType
    from ea.product.paper_gate_prep import (
        PaperGatePrepError,
        current_gate_identity,
        lock_gate_identity,
    )

    try:
        requested = GateType(gate_type)
    except ValueError:
        typer.echo(
            f"Paper gate lock input error: gate-type must be one of "
            f"{', '.join(member.value for member in GateType)}",
            err=True,
        )
        raise typer.Exit(code=2) from None
    if started_at is None:
        stamp = RealUtcClock().now()
    else:
        try:
            stamp = require_utc(datetime.fromisoformat(started_at), field="started_at")
        except ValueError as error:
            typer.echo(f"Paper gate lock input error: {error}", err=True)
            raise typer.Exit(code=2) from None
    try:
        identity = current_gate_identity(
            gate_id=gate_id,
            gate_type=requested,
            repository=repository.expanduser().absolute(),
            config_path=config.expanduser().absolute(),
            launchd_dir=launchd_dir.expanduser().absolute(),
            started_at=stamp,
        )
        lock_gate_identity(identity_path.expanduser().absolute(), identity)
    except PaperGatePrepError as error:
        typer.echo(f"Paper gate lock refused: {error}", err=True)
        raise typer.Exit(code=3) from None
    typer.echo(canonical_json(identity.document()).decode("ascii"))


@gate_app.command("evaluate")
def paper_gate_evaluate(
    identity_path: Annotated[Path, typer.Option("--identity")],
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    repository: Annotated[Path, typer.Option("--repo")],
    config: Annotated[Path, typer.Option("--config")],
    launchd_dir: Annotated[Path, typer.Option("--launchd-dir")],
    supervisor_state: Annotated[str, typer.Option("--supervisor-state")] = "unknown",
    backup_root: Annotated[Path | None, typer.Option("--backup-root")] = None,
    alert_stream: Annotated[Path | None, typer.Option("--alert-stream")] = None,
) -> None:
    """Judge the current runtime against a locked identity, deterministically.

    The locked identity is read back, the current one is rebuilt from the same
    real inputs, the runtime is observed, and WU-2's own `evaluate_gate` produces
    the verdict: drift is INVALID, a runtime violation is FAIL, and only a clean,
    unchanged gate is PASS. This command adds no evaluator and writes nothing, so
    it is equally usable as the pre-freeze readiness check and as the post-hoc
    re-judgement of a gate that has already run.

    Exit code 3 means the verdict is not PASS, or that the observation itself
    could not be taken.
    """
    from ea.product.market_stream import RealUtcClock
    from ea.product.paper_evidence import GateVerdict, evaluate_gate
    from ea.product.paper_gate_prep import (
        PaperGatePrepError,
        current_gate_identity,
        read_locked_gate_identity,
    )

    try:
        locked = read_locked_gate_identity(identity_path.expanduser().absolute())
        # The gate id, gate type and recorded start come from the lock itself, so
        # the "current" identity differs from it only in what actually drifted.
        current = current_gate_identity(
            gate_id=locked.gate_id,
            gate_type=locked.gate_type,
            repository=repository.expanduser().absolute(),
            config_path=config.expanduser().absolute(),
            launchd_dir=launchd_dir.expanduser().absolute(),
            started_at=locked.started_at,
        )
    except PaperGatePrepError as error:
        typer.echo(f"Paper gate evaluate unavailable: {error}", err=True)
        raise typer.Exit(code=3) from None
    snapshot = _collect_paper_snapshot(
        run_dir=run_dir,
        repository=repository,
        supervisor_state=supervisor_state,
        backup_root=backup_root,
        alert_stream=alert_stream,
    )
    verdict = evaluate_gate(snapshot, locked, current, evaluated_at=RealUtcClock().now())
    typer.echo(
        canonical_json(
            {
                "locked_identity": locked.document(),
                "current_identity": current.document(),
                "snapshot": snapshot.document(),
                "verdict": verdict.document(),
            }
        ).decode("ascii")
    )
    if verdict.verdict is not GateVerdict.PASS:
        typer.echo(f"Paper gate evaluate verdict: {verdict.verdict.value}", err=True)
        raise typer.Exit(code=3)


@strategy_app.command("pack")
def strategy_pack(
    source: Annotated[Path, typer.Option("--source")],
    output: Annotated[Path, typer.Option("--output")],
) -> None:
    """Pack exactly manifest.json and strategy.py into a deterministic artifact."""
    try:
        package = pack_strategy(source, output)
    except StrategyPackageError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(2) from None
    typer.echo(canonical_json(package.document()).decode("ascii"))


@strategy_app.command("validate")
@strategy_app.command("inspect")
def strategy_inspect(artifact: Annotated[Path, typer.Option("--artifact")]) -> None:
    """Validate executable trusted local code and print canonical JSON identity."""
    try:
        if not artifact.is_absolute() or artifact.suffix != ".eastrategy":
            raise StrategyPackageError("artifact must be an absolute .eastrategy path")
        package = validate_package(read_regular(artifact, artifact.parent))
    except StrategyPackageError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(2) from None
    typer.echo(canonical_json(package.document()).decode("ascii"))


if __name__ == "__main__":
    app()
