from __future__ import annotations

import platform

import typer

from ea.config.settings import Settings

app = typer.Typer(help="EA quantitative trading system CLI.")


@app.callback()
def main() -> None:
    """EA quantitative trading system command group."""


@app.command()
def doctor() -> None:
    """Print local project health information."""
    settings = Settings()

    typer.echo("EA system doctor")
    typer.echo(f"python: {platform.python_version()}")
    typer.echo(f"config: {settings.env}")
    typer.echo(f"paper trading: {'enabled' if settings.paper_trading_enabled else 'disabled'}")
    typer.echo(f"live trading: {'enabled' if settings.live_trading_enabled else 'disabled'}")


if __name__ == "__main__":
    app()
