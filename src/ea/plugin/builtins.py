"""Built-in capabilities that wrap implementations already in this distribution.

Each built-in is a manifest plus a host adapter record that names the existing
authority it wraps: the model port factory, the local dataset registry, the
strategy registry, the scenario validator and the real backtest engine. Nothing
here re-implements an engine, resolves a dependency, imports a third-party
module, or reads a credential.

``package_version`` is the host adapter contract version of this thin wrapper.
The exact identity of the wrapped implementation — strategy version, dataset
digest, provider protocol and configured route, engine result schema, and the
fingerprint of the host module that implements it — lives in the adapter's
``identity`` and is what a run binding freezes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from ea.plugin.catalog import AdapterV1, PluginCatalogV1
from ea.plugin.manifest import (
    EFFECT_LOCAL_COMPUTE,
    EFFECT_READ_NETWORK,
    EFFECT_WRITE_LOCAL,
    MANIFEST_SCHEMA_VERSION,
    RUNTIME_PROFILE,
    InterfaceRangeV1,
    PluginManifestV1,
    seal_manifest,
)
from ea.strategy.registry import BUILTIN_STRATEGIES, StrategyRegistryV1

BUILTIN_PACKAGE_VERSION = "1.0.0"

MODEL_ENTRYPOINT = "ea.host.adapter.model"
DATA_ENTRYPOINT = "ea.host.adapter.dataset"
STRATEGY_ENTRYPOINT = "ea.host.adapter.strategy"
VALIDATOR_ENTRYPOINT = "ea.host.adapter.scenario-validator"
BACKTEST_ENTRYPOINT = "ea.host.adapter.replay-simulator"

# The validator identity is the input schema the existing validator accepts.
VALIDATOR_ID = "ea.local-web-scenario-validator"

# The backtest method identity is the engine entry point; its version is the
# result schema the engine's own report layer already validates.
BACKTEST_METHOD_ID = "ea.product.run_backtest_scenario"

# The host artifact each adapter kind actually executes. A manifest describes a
# capability; these modules are what runs, so their bytes are part of the
# identity a run is bound to.
IMPLEMENTATION_MODULES: dict[str, tuple[str, ...]] = {
    "model": ("ea.web.llm",),
    "data": ("ea.web.datasets",),
    "strategy": ("ea.strategy.registry", "ea.strategy.rules"),
    "validator": ("ea.web.service",),
    "backtest-method": ("ea.product.backtest",),
}


def _source_digest(module_name: str) -> str | None:
    """The sha256 of one host module's own source artifact, when it can be named."""
    try:
        spec = find_spec(module_name)
    except (ImportError, ValueError):
        return None
    origin = None if spec is None else spec.origin
    if not isinstance(origin, str) or origin.endswith((".pyc", ".pyo")):
        # A compiled or frozen distribution cannot name its source artifact.
        return None
    try:
        payload = Path(origin).read_bytes()
    except OSError:
        return None
    return "sha256:" + sha256(payload).hexdigest()


def implementation_identity(kind: str) -> str | None:
    """One fingerprint over the host modules that implement this kind.

    A fingerprint that cannot be taken is never guessed: the adapter without it
    stays visible and unusable, so no run is bound to an artifact the host cannot
    name. This is the implementation's own identity, separate from the manifest
    digest that only proves the declaration is intact.
    """
    named: list[list[str]] = []
    for module_name in IMPLEMENTATION_MODULES[kind]:
        digest = _source_digest(module_name)
        if digest is None:
            return None
        named.append([module_name, digest])
    payload = json.dumps(named, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + sha256(payload.encode("ascii")).hexdigest()


def _manifest(
    *,
    plugin_id: str,
    kind: str,
    interface: str,
    entrypoint: str,
    provides: tuple[str, ...],
    effects: tuple[str, ...],
    config_schema: dict[str, Any],
) -> PluginManifestV1:
    return seal_manifest(
        PluginManifestV1(
            schema_version=MANIFEST_SCHEMA_VERSION,
            plugin_id=plugin_id,
            kind=kind,
            package_version=BUILTIN_PACKAGE_VERSION,
            artifact_digest="",
            interface=InterfaceRangeV1(interface, 1, 0),
            entrypoint=entrypoint,
            provides=provides,
            effects=effects,
            runtime_profile=RUNTIME_PROFILE,
            config_schema=config_schema,
        )
    )


def builtin_model_capabilities(
    model_route: Mapping[str, Any] | None = None,
) -> list[tuple[AdapterV1, PluginManifestV1]]:
    """One capability per host protocol, carrying the configured route facts.

    ``model_route`` is the non-secret projection of the current model settings
    (provider, model, endpoint). It is part of the adapter identity, so a run
    frozen against one model or endpoint no longer verifies after the host is
    reconfigured. No credential, key reference or resolution value is read.
    """
    from ea.web.llm import PROVIDER_PROTOCOLS, REQUIRED_RESEARCH_CAPABILITIES

    route = model_route or {}
    built: list[tuple[AdapterV1, PluginManifestV1]] = []
    for provider, (protocol, capabilities) in sorted(PROVIDER_PROTOCOLS.items()):
        declared = capabilities.document()
        usable = all(declared.get(name, False) for name in REQUIRED_RESEARCH_CAPABILITIES)
        plugin_id = f"ea.builtin.model.{provider}"
        entrypoint = f"{MODEL_ENTRYPOINT}.{provider}"
        on_route = route.get("provider") == provider
        identity: dict[str, Any] = {
            "provider": provider,
            "protocol": protocol,
            "model": route.get("model") if on_route else None,
            "endpoint": route.get("endpoint") if on_route else None,
            "implementation_sha256": implementation_identity("model"),
        }
        built.append(
            (
                AdapterV1(
                    entrypoint=entrypoint,
                    kind="model",
                    provides=("model.tool-use",) if usable else ("model.text",),
                    effects=(EFFECT_READ_NETWORK, EFFECT_LOCAL_COMPUTE),
                    identity=identity,
                ),
                _manifest(
                    plugin_id=plugin_id,
                    kind="model",
                    interface="ea.model-port",
                    entrypoint=entrypoint,
                    provides=("model.tool-use",) if usable else ("model.text",),
                    effects=(EFFECT_READ_NETWORK, EFFECT_LOCAL_COMPUTE),
                    config_schema={
                        "provider": provider,
                        "protocol": protocol,
                        "capabilities": declared,
                        "required_capabilities": list(REQUIRED_RESEARCH_CAPABILITIES),
                        "usable": usable,
                    },
                ),
            )
        )
    return built


def builtin_data_capabilities(datasets: Any) -> list[tuple[AdapterV1, PluginManifestV1]]:
    """One capability per registered local dataset that passes strict inspection."""
    if datasets is None:
        return []
    built: list[tuple[AdapterV1, PluginManifestV1]] = []
    for entry in datasets.list():
        if not entry.get("valid"):
            continue
        dataset_id = str(entry["dataset_id"])
        entrypoint = f"{DATA_ENTRYPOINT}.{dataset_id}"
        identity = {
            "dataset_id": dataset_id,
            "data_sha256": entry.get("data_sha256"),
            "source_sha256": entry.get("source_sha256"),
            "implementation_sha256": implementation_identity("data"),
        }
        config: dict[str, Any] = {
            "dataset_id": dataset_id,
            "venue": entry.get("venue"),
            "symbol": entry.get("symbol"),
            "record_count": entry.get("record_count"),
            "replay_start_utc": entry.get("replay_start_utc"),
            "replay_end_utc": entry.get("replay_end_utc"),
        }
        built.append(
            (
                AdapterV1(
                    entrypoint=entrypoint,
                    kind="data",
                    provides=("data.strict-ohlcv-replay",),
                    effects=(EFFECT_LOCAL_COMPUTE,),
                    identity=identity,
                ),
                _manifest(
                    plugin_id=f"ea.builtin.data.{dataset_id}",
                    kind="data",
                    interface="ea.dataset-catalog",
                    entrypoint=entrypoint,
                    provides=("data.strict-ohlcv-replay",),
                    effects=(EFFECT_LOCAL_COMPUTE,),
                    config_schema=config,
                ),
            )
        )
    return built


def builtin_strategy_capabilities(
    registry: StrategyRegistryV1 = BUILTIN_STRATEGIES,
) -> list[tuple[AdapterV1, PluginManifestV1]]:
    built: list[tuple[AdapterV1, PluginManifestV1]] = []
    for descriptor in registry.descriptors():
        document = descriptor.document()
        provides = ("strategy.research",) if descriptor.research_visible else ("strategy.internal",)
        entrypoint = f"{STRATEGY_ENTRYPOINT}.{descriptor.strategy_id}"
        built.append(
            (
                AdapterV1(
                    entrypoint=entrypoint,
                    kind="strategy",
                    provides=provides,
                    effects=(EFFECT_LOCAL_COMPUTE,),
                    identity={
                        "strategy_id": descriptor.strategy_id,
                        "strategy_version": descriptor.strategy_version,
                        "implementation_sha256": implementation_identity("strategy"),
                    },
                ),
                _manifest(
                    plugin_id=f"ea.builtin.strategy.{descriptor.strategy_id}",
                    kind="strategy",
                    interface="ea.strategy-contract",
                    entrypoint=entrypoint,
                    provides=provides,
                    effects=(EFFECT_LOCAL_COMPUTE,),
                    config_schema=document,
                ),
            )
        )
    return built


def validator_version() -> str:
    """The newest input schema the existing web validator accepts."""
    from ea.web.service import VALIDATOR_INPUT_SCHEMAS

    return VALIDATOR_INPUT_SCHEMAS[-1]


def builtin_validator_capabilities() -> list[tuple[AdapterV1, PluginManifestV1]]:
    entrypoint = VALIDATOR_ENTRYPOINT
    identity = {
        "validator_id": VALIDATOR_ID,
        "validator_version": validator_version(),
        "implementation_sha256": implementation_identity("validator"),
    }
    return [
        (
            AdapterV1(
                entrypoint=entrypoint,
                kind="validator",
                provides=("validator.scenario-input",),
                effects=(EFFECT_LOCAL_COMPUTE,),
                identity=identity,
            ),
            _manifest(
                plugin_id="ea.builtin.validator.local-scenario-input",
                kind="validator",
                interface="ea.scenario-validator",
                entrypoint=entrypoint,
                provides=("validator.scenario-input",),
                effects=(EFFECT_LOCAL_COMPUTE,),
                config_schema=dict(identity),
            ),
        )
    ]


def backtest_method_version() -> str:
    """The result schema the engine's own report layer validates."""
    from ea.product.reporting import _SUPPORTED_RESULT_SCHEMA

    return str(_SUPPORTED_RESULT_SCHEMA)


def builtin_backtest_capabilities() -> list[tuple[AdapterV1, PluginManifestV1]]:
    entrypoint = BACKTEST_ENTRYPOINT
    identity = {
        "method_id": BACKTEST_METHOD_ID,
        "method_version": backtest_method_version(),
        "implementation_sha256": implementation_identity("backtest-method"),
    }
    return [
        (
            AdapterV1(
                entrypoint=entrypoint,
                kind="backtest-method",
                provides=("backtest.event-replay",),
                effects=(EFFECT_LOCAL_COMPUTE, EFFECT_WRITE_LOCAL),
                identity=identity,
            ),
            _manifest(
                plugin_id="ea.builtin.backtest.event-replay",
                kind="backtest-method",
                interface="ea.backtest-method",
                entrypoint=entrypoint,
                provides=("backtest.event-replay",),
                effects=(EFFECT_LOCAL_COMPUTE, EFFECT_WRITE_LOCAL),
                config_schema=dict(identity),
            ),
        )
    ]


def builtin_capabilities(
    service: Any, model_route: Mapping[str, Any] | None = None
) -> list[tuple[AdapterV1, PluginManifestV1]]:
    """Every built-in capability this host currently declares."""
    return [
        *builtin_model_capabilities(model_route),
        *builtin_data_capabilities(getattr(service, "datasets", None)),
        *builtin_strategy_capabilities(),
        *builtin_validator_capabilities(),
        *builtin_backtest_capabilities(),
    ]


def builtin_catalog(service: Any, model_route: Mapping[str, Any] | None = None) -> PluginCatalogV1:
    capabilities = builtin_capabilities(service, model_route)
    return PluginCatalogV1(
        [manifest for _adapter, manifest in capabilities],
        [adapter for adapter, _manifest_item in capabilities],
    )
