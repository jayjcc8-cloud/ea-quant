from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pydantic_core import PydanticCustomError


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class RunMode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class SecretRef(BaseModel):
    """Opaque identifier resolved only by a future outer adapter boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identifier: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.:/-]*$",
    )


class RunSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: RunMode = RunMode.BACKTEST

    @field_validator("mode", mode="before")
    @classmethod
    def require_string_mode(cls, value: object) -> object:
        if not isinstance(value, str):
            raise PydanticCustomError("string_type", "Input should be a valid string")
        return value


class _SettingsInput(BaseModel):
    """Structurally valid settings before the final live-availability gate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: StrictInt = 1
    environment: Environment = Environment.DEVELOPMENT
    run: RunSettings = Field(default_factory=RunSettings)

    @field_validator("environment", mode="before")
    @classmethod
    def require_string_environment(cls, value: object) -> object:
        if not isinstance(value, str):
            raise PydanticCustomError("string_type", "Input should be a valid string")
        return value

    @field_validator("schema_version")
    @classmethod
    def require_v1_schema(cls, value: int) -> int:
        if value != 1:
            raise PydanticCustomError(
                "unsupported_schema_version",
                "only schema version 1 is supported",
            )
        return value


class Settings(_SettingsInput):
    """Transitively immutable snapshot handed to the composition boundary."""

    @model_validator(mode="after")
    def reject_unavailable_live_mode(self) -> Self:
        if self.run.mode is RunMode.LIVE:
            raise PydanticCustomError(
                "live_mode_unavailable",
                "live mode is unavailable in this build",
            )
        return self
