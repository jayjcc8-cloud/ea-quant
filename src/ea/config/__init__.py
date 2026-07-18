"""Strict outer-boundary configuration loading and validation."""

from ea.config.loader import ConfigurationError, LoadedConfiguration, load_configuration
from ea.config.settings import Environment, RunMode, RunSettings, SecretRef, Settings

__all__ = [
    "ConfigurationError",
    "Environment",
    "LoadedConfiguration",
    "RunMode",
    "RunSettings",
    "SecretRef",
    "Settings",
    "load_configuration",
]
