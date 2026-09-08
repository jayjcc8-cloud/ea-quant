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
from ea.strategy.package import (
    StrategyPackageError,
    canonical_json,
    pack_strategy,
    read_regular,
    validate_package,
)

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


if __name__ == "__main__":
    app()


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
