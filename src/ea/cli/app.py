from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, cast

import typer

from ea.config import ConfigurationError, load_configuration

app = typer.Typer(help="EA quantitative trading system CLI.")


@dataclass(frozen=True, slots=True)
class CliConfiguration:
    config_path: Path | None
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


@app.callback()
def main(
    context: typer.Context,
    config_path: Annotated[
        list[Path] | None,
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


@app.command()
def doctor(context: typer.Context) -> None:
    """Print local project health information."""
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
    typer.echo(f"config file: {loaded.config_path if loaded.config_path else '<defaults>'}")
    typer.echo(f"schema version: {loaded.snapshot.schema_version}")
    typer.echo(f"environment: {loaded.snapshot.environment.value}")
    typer.echo(f"run mode: {loaded.snapshot.run.mode.value}")
    typer.echo("live profile: unavailable")


if __name__ == "__main__":
    app()
