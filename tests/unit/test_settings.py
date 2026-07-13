import pytest

from ea.config.settings import Settings


def test_default_environment_is_development(isolated_ea_environment: None) -> None:
    settings = Settings()

    assert settings.env == "development"
    assert settings.paper_trading_enabled is True
    assert settings.live_trading_enabled is False


def test_live_trading_requires_explicit_flag(isolated_ea_environment: None) -> None:
    settings = Settings(live_trading_enabled=True)

    assert settings.live_trading_enabled is True


def test_supported_environment_variables_are_parsed_explicitly(
    isolated_ea_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_ENV", "staging")
    monkeypatch.setenv("EA_PAPER_TRADING_ENABLED", "false")
    monkeypatch.setenv("EA_LIVE_TRADING_ENABLED", "true")

    settings = Settings()

    assert settings.env == "staging"
    assert settings.paper_trading_enabled is False
    assert settings.live_trading_enabled is True
