from ea.config.settings import Settings


def test_default_environment_is_development() -> None:
    settings = Settings()

    assert settings.env == "development"
    assert settings.paper_trading_enabled is True
    assert settings.live_trading_enabled is False


def test_live_trading_requires_explicit_flag() -> None:
    settings = Settings(live_trading_enabled=True)

    assert settings.live_trading_enabled is True
