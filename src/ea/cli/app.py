from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, cast

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

app = typer.Typer(
    help="EA quantitative trading system CLI.",
    context_settings={"token_normalize_func": escape_diagnostic_label},
)
backtest_app = typer.Typer(
    help=("Strict scenarios support validate, run, resume, and report; the RESET demo is run-only.")
)
app.add_typer(backtest_app, name="backtest")


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
            loaded = load_backtest_scenario(scenario)
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
) -> None:
    """Validate one scenario and its selected local OHLCV without executing it."""
    try:
        load_backtest_scenario(scenario)
    except BacktestScenarioError as error:
        typer.echo(f"scenario validation error: {error}", err=True)
        raise typer.Exit(code=2) from None
    except Exception:
        typer.echo("scenario validation internal error", err=True)
        raise typer.Exit(code=1) from None
    typer.echo("scenario valid: BacktestScenario v1")


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


if __name__ == "__main__":
    app()
