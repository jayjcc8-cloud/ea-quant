from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EA_", env_file=".env", extra="ignore")

    env: str = Field(default="development")
    paper_trading_enabled: bool = Field(default=True)
    live_trading_enabled: bool = Field(default=False)
