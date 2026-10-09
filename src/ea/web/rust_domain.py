"""Stateless domain adapter for the Rust quine core (stage 2).

The Rust core owns identity, the single active slot, IPC, worker supervision,
cancellation and official result registration. It does not own scenario,
economic or report semantics, and this module does not own any of the Rust
side either: it holds no job, attempt, budget, dispatch, cancellation or
publication state, opens no service, and writes nothing durable except the one
reply document it is asked for.

Three bounded, side-effect-free modes, each ``<mode> <request.json> <reply.json>``:

``snapshot``
    Load one registered scenario file through the existing strict loader, apply
    the user's parameters through the existing parameterizer, and render the
    *same* admitted input snapshot document and digest the Python Web service
    would freeze, together with the exact frozen scenario-file bytes the worker
    reads. This is the frozen input a confirmation carries. One explicitly
    selected local dataset is resolved through the existing dataset catalog —
    the registered root and the controlled upload store, never a caller's path —
    and its published version evidence and required capability binding are bound
    into the envelope.

``freeze``
    Re-verify one frozen input envelope from its own retained bytes: the
    snapshot document must reproduce its digest, the frozen scenario-file bytes
    must reproduce theirs, and re-loading those bytes must reproduce the
    admitted scenario identity. Along that check it captures the source bytes
    the confirmation admitted into the private path the caller names, refusing a
    source that no longer reproduces the confirmed digest, and returns the
    capture's own path and digest beside the bytes to materialize.

``validate``
    After the whole worker group is gone, independently regenerate the formal
    report and the equity path from the engine's own attempt evidence, compare
    them byte for byte with what the worker staged, bind them to the frozen
    scenario and data identity, and return the artifact digests plus the
    host-verified economic readings.

Only existing stateless product functions are reused. No economic rule is
redefined here, and a decimal is never converted to a binary float.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from ea.core.encoding import canonical_json_bytes
from ea.data.historical import HistoricalMarketDataError
from ea.plugin.manifest import ManifestError
from ea.plugin.selection import PluginBindingV1
from ea.product import (
    BacktestScenarioError,
    LoadedBacktestScenario,
    generate_backtest_report,
    load_backtest_scenario,
)
from ea.product.equity_path import generate_equity_path_analysis
from ea.product.scenario import load_backtest_scenario_bytes, rebind_research_dataset
from ea.strategy.catalog import ResearchStrategyCatalogV1
from ea.strategy.package import read_regular, validate_package
from ea.web.datasets import LocalResearchDatasetRegistryV1
from ea.web.uploads import (
    ControlledUploadStoreV1,
    LocalResearchDatasetCatalogV1,
    evidence_digest,
)

DOMAIN_SCHEMA = "ea.quine-domain.v1"
FROZEN_SCHEMA = "ea.quine-frozen-input.v1"

ARTIFACTS = ("report.json", "summary.txt", "equity-path.json")

#: The economic readings a verified report may state. Read through the host's
#: own report vocabulary, never recomputed here.
FACT_PATHS = (
    "economics.net_pnl.amount",
    "economics.fees.amount",
    "economics.total_return.value",
    "economics.counts.fills",
    "economics.counts.orders",
)

REFUSED = "refused"
OK = "ok"


class DomainRefusal(ValueError):
    """The adapter refuses the request; nothing was produced."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _digest(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise DomainRefusal(code, message)


# --- frozen envelope --------------------------------------------------------


def _frozen_scenario_bytes(scenario: LoadedBacktestScenario) -> bytes:
    """The exact bytes of one attempt's frozen scenario file.

    The admitted scenario's own canonical document, with the resolved data path
    substituted for the declared one. This is the same rendering the existing
    Web dispatch writes, so what is frozen and what a worker reads are built by
    one rule.
    """
    document = json.loads(scenario.canonical_bytes)
    document.pop("canonicalization", None)
    document["data"] = {**document["data"], "path": str(scenario.data_path)}
    return canonical_json_bytes(document)


def _package_bytes(scenario: LoadedBacktestScenario) -> tuple[bytes | None, str | None]:
    package = scenario.strategy_package
    if package is None:
        return None, None
    return package.artifact_bytes, package.identity.artifact_sha256


def _research_evidence(snapshot: dict[str, Any]) -> tuple[Any, str | None, str | None]:
    """The exact canonical research evidence one admitted snapshot carries.

    A registered research input is carried as the snapshot's own
    ``research_input`` value together with the digest of its canonical bytes,
    so a worker re-parses and re-digests exactly the evidence this confirmation
    admitted — including the dataset binding, the source digest and the replay
    window — rather than a default scenario's.
    """
    value = snapshot.get("research_input")
    if value is None:
        return None, None, None
    canonical = canonical_json_bytes(value)
    return value, _digest(canonical), canonical.hex()


def _frozen_envelope(
    scenario_id: str,
    source: LoadedBacktestScenario,
    parameterized: LoadedBacktestScenario,
    snapshot: dict[str, Any],
    snapshot_sha256: str,
    *,
    plugin_binding: dict[str, Any] | None = None,
    dataset_version: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from ea.web.service import _PATH_SCHEMAS, _REPORT_SCHEMAS, _job_schema

    frozen = _frozen_scenario_bytes(parameterized)
    package_bytes, package_sha256 = _package_bytes(parameterized)
    job_schema = _job_schema(parameterized)
    research, research_sha256, research_canonical = _research_evidence(snapshot)
    envelope: dict[str, Any] = {
        "schema": FROZEN_SCHEMA,
        "scenario_id": scenario_id,
        "snapshot": snapshot,
        "snapshot_sha256": snapshot_sha256,
        "scenario_identity_sha256": parameterized.scenario_sha256.value,
        "scenario_file": frozen.hex(),
        "scenario_file_sha256": _digest(frozen),
        "package_file": None if package_bytes is None else package_bytes.hex(),
        "package_sha256": package_sha256,
        "research_input": research,
        "research_input_sha256": research_sha256,
        "research_input_canonical": research_canonical,
        # The published version of the selected local dataset, when the selection
        # is an uploaded one: the exact re-derived evidence document this host's
        # controlled upload store vouches for, and the digest of its canonical
        # bytes. A registered read-only input publishes no version, and that is
        # carried as the absence it is rather than as an empty claim.
        "dataset_version": dataset_version,
        "dataset_version_sha256": (
            None if dataset_version is None else _digest(canonical_json_bytes(dataset_version))
        ),
        # The capability binding this confirmation was made under: the exact
        # identity of the data and strategy implementations the admitted input
        # runs through. A caller's own document is only ever carried after this
        # host's catalog has resolved and authorized it.
        "plugin_binding": plugin_binding,
        "plugin_binding_sha256": (
            None if plugin_binding is None else plugin_binding.get("binding_sha256")
        ),
        "job_schema": job_schema,
        "report_schema": _REPORT_SCHEMAS.get(job_schema, "ea.backtest-report.v1"),
        "path_schema": _PATH_SCHEMAS.get(job_schema, "ea.backtest-equity-path.v1"),
    }
    return envelope


def _decode_envelope(value: object) -> tuple[dict[str, Any], bytes, bytes | None]:
    """Re-verify one frozen envelope from its own retained bytes."""
    from ea.web.service import input_snapshot_sha256, read_input_snapshot

    _require(type(value) is dict, "invalid_request", "frozen input is not one document")
    envelope = cast(dict[str, Any], value)
    _require(
        envelope.get("schema") == FROZEN_SCHEMA,
        "invalid_request",
        "frozen input version is unsupported",
    )
    snapshot = read_input_snapshot(envelope.get("snapshot"))
    _require(snapshot is not None, "invalid_request", "the frozen snapshot is unreadable")
    readable = cast(dict[str, object], snapshot)
    claimed = envelope.get("snapshot_sha256")
    _require(
        type(claimed) is str and input_snapshot_sha256(readable) == claimed,
        "digest_mismatch",
        "the frozen snapshot does not reproduce its own digest",
    )
    frozen_hex = envelope.get("scenario_file")
    _require(type(frozen_hex) is str, "invalid_request", "the frozen scenario file is missing")
    try:
        frozen = bytes.fromhex(cast(str, frozen_hex))
    except ValueError:
        raise DomainRefusal("invalid_request", "the frozen scenario file is not bytes") from None
    _require(
        envelope.get("scenario_file_sha256") == _digest(frozen),
        "digest_mismatch",
        "the frozen scenario file does not reproduce its own digest",
    )
    package: bytes | None = None
    package_hex = envelope.get("package_file")
    if package_hex is not None:
        _require(
            type(package_hex) is str,
            "invalid_request",
            "the frozen strategy artifact is missing",
        )
        try:
            package = bytes.fromhex(cast(str, package_hex))
        except ValueError:
            raise DomainRefusal(
                "invalid_request", "the frozen strategy artifact is not bytes"
            ) from None
        _require(
            envelope.get("package_sha256") == _digest(package),
            "digest_mismatch",
            "the frozen strategy artifact does not reproduce its own digest",
        )
    # The retained research evidence is bound three ways at once: its own
    # canonical bytes must reproduce the digest it is carried with, it must be
    # exactly the value the snapshot admitted, and the snapshot is what the
    # admitted identity was derived from.
    research = envelope.get("research_input")
    research_canonical = envelope.get("research_input_canonical")
    research_sha256 = envelope.get("research_input_sha256")
    snapshot_research = readable.get("research_input")
    if research is None:
        _require(
            research_canonical is None and research_sha256 is None and snapshot_research is None,
            "identity_mismatch",
            "the frozen input carries research evidence its snapshot did not admit",
        )
    else:
        _require(
            type(research_canonical) is str and type(research_sha256) is str,
            "invalid_request",
            "the frozen research evidence carries no canonical bytes or digest",
        )
        try:
            evidence = bytes.fromhex(cast(str, research_canonical))
        except ValueError:
            raise DomainRefusal(
                "invalid_request", "the frozen research evidence is not bytes"
            ) from None
        _require(
            _digest(evidence) == research_sha256,
            "digest_mismatch",
            "the frozen research evidence does not reproduce its own digest",
        )
        _require(
            evidence == canonical_json_bytes(research),
            "identity_mismatch",
            "the frozen research evidence is not the value it is carried as",
        )
        _require(
            snapshot_research == research,
            "identity_mismatch",
            "the frozen research evidence is not the one the snapshot admitted",
        )
    _verify_version_binding(envelope, readable)
    return envelope, frozen, package


def _verify_version_binding(
    envelope: dict[str, Any], snapshot: dict[str, Any]
) -> None:
    """Hold one envelope's version evidence and binding to each other.

    These two facts are what make a confirmation about a *version* of a local
    dataset rather than about whatever bytes happen to sit at a path today: the
    published evidence names the captured bytes, and the bound data capability
    names the digest of exactly that evidence. Neither is trusted from the
    envelope alone — each is re-derived from the value it claims — and the two
    must name the same dataset and the same capture.
    """
    research = envelope.get("research_input")
    version = envelope.get("dataset_version")
    binding_value = envelope.get("plugin_binding")
    binding: PluginBindingV1 | None = None
    if binding_value is None:
        _require(
            envelope.get("plugin_binding_sha256") is None,
            "identity_mismatch",
            "the frozen input names a binding digest with no binding",
        )
    else:
        _require(
            type(binding_value) is dict,
            "invalid_request",
            "the frozen capability binding is not one document",
        )
        try:
            binding = PluginBindingV1.decode(binding_value)
        except ManifestError as error:
            raise DomainRefusal("binding_invalid", str(error)) from None
        _require(
            envelope.get("plugin_binding_sha256") == binding.binding_sha256,
            "identity_mismatch",
            "the frozen capability binding does not reproduce its own digest",
        )
    if version is None:
        _require(
            envelope.get("dataset_version_sha256") is None,
            "identity_mismatch",
            "the frozen input names a dataset version digest with no version",
        )
        _require(
            binding is None or binding.component("data") is None,
            "binding_invalid",
            "the frozen input binds a data capability to no published dataset version",
        )
        return
    _require(
        type(version) is dict,
        "invalid_request",
        "the frozen dataset version evidence is not one document",
    )
    evidence = cast(dict[str, Any], version)
    _require(
        envelope.get("dataset_version_sha256") == _digest(canonical_json_bytes(evidence)),
        "digest_mismatch",
        "the frozen dataset version evidence does not reproduce its own digest",
    )
    dataset_id = evidence.get("dataset_id")
    _require(
        type(dataset_id) is str and bool(dataset_id),
        "invalid_request",
        "the frozen dataset version evidence names no dataset",
    )
    # The evidence and the admitted research input are two records of one
    # selection: an envelope whose version evidence describes other bytes than
    # the input admitted is not a confirmation of either.
    if type(research) is dict:
        _require(
            research.get("dataset_id") == dataset_id
            and research.get("source_sha256") == evidence.get("capture_sha256")
            and research.get("data_sha256") == evidence.get("data_sha256")
            and research.get("record_count") == evidence.get("record_count"),
            "identity_mismatch",
            "the frozen dataset version is not the source this input admitted",
        )
    data = None if binding is None else binding.component("data")
    if data is None:
        _require(
            type(research) is not dict,
            "binding_invalid",
            "a published dataset version was confirmed without its data capability",
        )
        return
    _require(
        data.plugin_id == f"ea.builtin.data.{dataset_id}",
        "binding_invalid",
        "the bound data capability does not name this published dataset version",
    )
    _require(
        data.identity.get("version_evidence_sha256") == evidence_digest(evidence),
        "binding_invalid",
        "the bound data capability does not match this version's evidence",
    )
    _require(
        snapshot.get("research_input") is not None,
        "binding_invalid",
        "a bound data capability was frozen without the research input it selects",
    )


def _verified_scenario(
    envelope: dict[str, Any],
    frozen: bytes,
    package: bytes | None,
    scenario_path: Path,
    captured: tuple[Path, str] | None = None,
) -> LoadedBacktestScenario:
    """Re-load the frozen bytes and hold them to the admitted identity.

    ``captured`` names the private snapshot of the source bytes this input was
    confirmed from. When it is given the frozen file's declared data path is
    replaced by that snapshot — the scenario identity does not bind a local file
    path, so what is re-derived is still exactly the admitted scenario — and the
    snapshot must reproduce its own recorded digest before anything is parsed.
    """
    from ea.web.service import _scenario_identity

    catalog = None
    if package is not None:
        claimed = envelope.get("package_sha256")
        _require(type(claimed) is str, "invalid_request", "the strategy artifact digest is missing")
        catalog = ResearchStrategyCatalogV1(
            (validate_package(package, expected_sha256=cast(str, claimed)),)
        )
    else:
        catalog = ResearchStrategyCatalogV1(())
    payload = frozen
    if captured is not None:
        payload = _rewrite_data_path(frozen, _verified_capture(captured))
    try:
        scenario = load_backtest_scenario_bytes(
            payload, scenario_path=scenario_path, catalog=catalog
        )
    except (BacktestScenarioError, OSError, ValueError):
        raise DomainRefusal(
            "invalid_request", "the frozen scenario file is not the scenario it claims"
        ) from None
    snapshot = cast(dict[str, Any], envelope["snapshot"])
    # The admitted research evidence belongs to the scenario before any identity
    # is derived from it: the identity of a registered research input is not the
    # identity of the same scenario without its dataset binding.
    research = snapshot.get("research_input")
    if research is not None:
        scenario = _with_research_input(scenario, cast(dict[str, Any], research))
        # The admitted identity derives its dataset binding from this evidence,
        # so agreeing with itself proves nothing: the evidence is held to the
        # dataset that was actually loaded from the source just read.
        _require_evidence_matches_source(cast(dict[str, Any], research), scenario)
    _require(
        scenario.scenario_sha256.value == envelope.get("scenario_identity_sha256"),
        "identity_mismatch",
        "the frozen scenario file is not the scenario this input admitted",
    )
    _require(
        _scenario_identity(scenario) == snapshot.get("identity"),
        "identity_mismatch",
        "the frozen scenario file is not the scenario this input admitted",
    )
    # The retained snapshot carries the complete canonical scenario this input
    # admitted. The frozen file bytes must be that same document with only the
    # resolved data path substituted — agreeing on two independent self-hashes
    # is not agreement on the input.
    retained = cast(dict[str, Any], snapshot.get("scenario"))
    _require(
        type(retained) is dict and type(retained.get("data")) is dict,
        "invalid_request",
        "the frozen snapshot carries no canonical scenario",
    )
    _require(
        json.loads(scenario.canonical_bytes) == retained,
        "identity_mismatch",
        "the frozen scenario file is not the scenario this input admitted",
    )
    try:
        materialized = json.loads(payload)
    except ValueError:
        raise DomainRefusal("invalid_request", "the frozen scenario file is not readable") from None
    retained_data = cast(dict[str, Any], retained["data"])
    expected = {key: value for key, value in retained.items() if key != "canonicalization"}
    expected["data"] = {**retained_data, "path": str(scenario.data_path)}
    _require(
        materialized == expected,
        "identity_mismatch",
        "the frozen scenario file does not materialize the snapshot this input admitted",
    )
    return scenario


def _verified_capture(captured: tuple[Path, str]) -> Path:
    """The private snapshot path, held to the digest it was captured as.

    The capture is the run's own frozen data. A snapshot that no longer
    reproduces the confirmed digest — truncated, edited or replaced — is refused
    here rather than parsed as if it were the admitted source.
    """
    path, claimed = captured
    try:
        payload = read_regular(path, path.parent)
    except OSError:
        raise DomainRefusal(
            "input_changed", "the private snapshot of this run's input is unavailable"
        ) from None
    _require(
        _digest(payload) == claimed,
        "input_changed",
        "the private snapshot of this run's input no longer reproduces the confirmed bytes",
    )
    return path


def _rewrite_data_path(frozen: bytes, data_path: Path) -> bytes:
    """The frozen scenario document with only its data path replaced."""
    try:
        document = json.loads(frozen)
    except ValueError:
        raise DomainRefusal("invalid_request", "the frozen scenario file is not readable") from None
    _require(
        type(document) is dict and type(cast(dict[str, Any], document).get("data")) is dict,
        "invalid_request",
        "the frozen scenario file names no data",
    )
    value = cast(dict[str, Any], document)
    value["data"] = {**cast(dict[str, Any], value["data"]), "path": str(data_path)}
    return canonical_json_bytes(value)


def _decoded_capture(value: object) -> tuple[Path, str] | None:
    """One private snapshot reference from a request, or ``None`` when absent."""
    if value is None:
        return None
    _require(
        type(value) is dict and set(cast(dict[str, Any], value)) == {"path", "sha256"},
        "invalid_request",
        "the private snapshot reference is not one document",
    )
    reference = cast(dict[str, Any], value)
    raw_path = reference.get("path")
    digest = reference.get("sha256")
    _require(
        type(raw_path) is str and bool(raw_path),
        "invalid_request",
        "the private snapshot reference names no path",
    )
    _require(
        type(digest) is str and len(digest) == 64,
        "invalid_request",
        "the private snapshot reference names no digest",
    )
    path = Path(cast(str, raw_path))
    _require(path.is_absolute(), "invalid_request", "the private snapshot path must be absolute")
    return path, cast(str, digest)


def _capture_source(
    scenario: LoadedBacktestScenario, raw_path: object, envelope: dict[str, Any]
) -> dict[str, Any] | None:
    """Copy the exact source bytes this confirmation admitted into the private path.

    The capture is what the run is actually computed from: the worker and the
    independent validator both read this file and nothing else, so a later change
    to or removal of the original upload cannot affect the experiment, and a
    capture that no longer reproduces its digest is refused rather than parsed.
    What is captured is held to the admitted evidence — the source digest of the
    confirmed research input when there is one, and the digest of the source this
    input was loaded from otherwise — so drift landing before materialization is
    refused here rather than frozen into the run.
    """
    if raw_path is None:
        return None
    _require(
        type(raw_path) is str and bool(raw_path),
        "invalid_request",
        "capture_path must be one absolute path",
    )
    target = Path(cast(str, raw_path))
    _require(target.is_absolute(), "invalid_request", "capture_path must be absolute")
    try:
        payload = read_regular(scenario.data_path, scenario.data_path.parent)
    except OSError:
        raise DomainRefusal(
            "input_changed", "the confirmed source of this input is no longer readable"
        ) from None
    research = envelope.get("research_input")
    expected = (
        cast(dict[str, Any], research).get("source_sha256")
        if type(research) is dict
        else scenario.dataset.source_bytes_sha256.value
    )
    digest = _digest(payload)
    _require(
        digest == expected,
        "input_changed",
        "the source this input was read from is not the source that was confirmed",
    )
    descriptor = _create_capture(target)
    with os.fdopen(descriptor, "wb", closefd=True) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    _require(
        _digest(read_regular(target, target.parent)) == digest,
        "internal",
        "the private snapshot did not reproduce the confirmed bytes",
    )
    return {"path": str(target), "sha256": digest}


def _create_capture(target: Path) -> int:
    try:
        return os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC, 0o600)
    except FileExistsError:
        # One private snapshot per attempt, written once. An existing file at this
        # attempt's own path is not this run's capture and is never overwritten.
        raise DomainRefusal(
            "input_changed", "the private snapshot for this attempt already exists"
        ) from None
    except OSError:
        raise DomainRefusal(
            "invalid_request", "the private snapshot for this attempt cannot be written"
        ) from None


def _with_research_input(
    scenario: LoadedBacktestScenario, research: dict[str, Any]
) -> LoadedBacktestScenario:
    from dataclasses import replace

    return replace(scenario, research_input_bytes=canonical_json_bytes(research))


def _require_evidence_matches_source(
    evidence: dict[str, Any], scenario: LoadedBacktestScenario
) -> None:
    """Hold one admitted research evidence to the source actually loaded.

    ``_scenario_identity`` reads the dataset binding out of this same evidence,
    so comparing the admitted identity with itself cannot prove the source at
    all. These are the facts the loaded dataset really measured: the digest of
    the source bytes read for this load, the selected fingerprint, the record
    count and the replay window. A source whose selected fingerprint is
    unchanged but whose bytes are not the confirmed ones is refused here.
    """
    dataset = scenario.dataset
    window = dataset.replay_window
    _require(
        evidence.get("source_sha256") == dataset.source_bytes_sha256.value,
        "identity_mismatch",
        "the research evidence is not the source bytes this input was read from",
    )
    _require(
        evidence.get("data_sha256") == dataset.selection.fingerprint.sha256.value
        and evidence.get("record_count") == dataset.selection.fingerprint.record_count,
        "identity_mismatch",
        "the research evidence is not the selected data this input admitted",
    )
    _require(
        evidence.get("replay_start_utc") == window.start_inclusive.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        and evidence.get("replay_end_utc")
        == window.end_exclusive.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "identity_mismatch",
        "the research evidence does not state the replay window this input admitted",
    )


# --- modes ------------------------------------------------------------------


def _mode_snapshot(request: dict[str, Any]) -> dict[str, Any]:
    from ea.web.service import _input_snapshot, _parameterized, _scenario_identity

    raw_path = request.get("scenario_path")
    _require(type(raw_path) is str, "invalid_request", "scenario_path is required")
    path = Path(cast(str, raw_path))
    _require(path.is_absolute(), "invalid_request", "scenario_path must be absolute")
    # One explicit local strategy root, when this workspace has one, is the same
    # root the Web service loads this scenario through: a scenario whose strategy
    # source is resolved from it must freeze the same effective scenario here.
    raw_strategy_root = request.get("strategy_root")
    strategy_root: Path | None = None
    if raw_strategy_root is not None:
        _require(
            type(raw_strategy_root) is str and bool(raw_strategy_root),
            "invalid_request",
            "strategy_root must be one absolute directory",
        )
        strategy_root = Path(cast(str, raw_strategy_root))
        _require(strategy_root.is_absolute(), "invalid_request", "strategy_root must be absolute")
    try:
        source = load_backtest_scenario(path, strategy_root=strategy_root)
    except (BacktestScenarioError, OSError, ValueError) as error:
        raise DomainRefusal("invalid_request", f"scenario is not readable: {error}") from None
    # One explicitly selected local research input replaces only the data
    # binding, through the existing dataset catalog and the existing rebinding
    # validation, so the admitted snapshot carries that input's own evidence. The
    # selection is a catalog identity: the dataset is resolved from this
    # workspace's own registered root and controlled upload store, never from a
    # caller-named path, and a published version's evidence and bound data
    # capability are what the envelope carries.
    dataset_version: dict[str, Any] | None = None
    dataset_id = request.get("dataset_id")
    if dataset_id is not None:
        _require(
            type(dataset_id) is str and bool(dataset_id),
            "invalid_request",
            "dataset_id must be one registered input name",
        )
        catalog, uploads = _dataset_catalog(request)
        try:
            data_path, dataset = catalog.load(cast(str, dataset_id))
            source = rebind_research_dataset(
                source,
                data_path=data_path,
                dataset=dataset,
                dataset_id=cast(str, dataset_id),
            )
        except (HistoricalMarketDataError, OSError, ValueError) as error:
            raise DomainRefusal(
                "invalid_request", f"the registered research input is unusable: {error}"
            ) from None
        dataset_version = _published_version(uploads, cast(str, dataset_id))
        _require(
            dataset_version is None
            or _bound_data_matches(request.get("binding"), dataset_version),
            "binding_invalid",
            "the selected dataset version is not the capability binding this input carries",
        )
    scenario_id = path.name
    source_identity = _scenario_identity(source)
    parameters = request.get("parameters")
    if parameters is None:
        parameterized = source
    else:
        _require(type(parameters) is dict, "invalid_request", "parameters must be a mapping")
        try:
            parameterized = _parameterized(source, cast(dict[str, Any], parameters))
        except (BacktestScenarioError, ValueError) as error:
            raise DomainRefusal("invalid_request", str(error)) from None
    snapshot_bytes, input_sha256 = _input_snapshot(scenario_id, parameterized, source_identity)
    snapshot = json.loads(snapshot_bytes)
    binding = request.get("binding")
    envelope = _frozen_envelope(
        scenario_id,
        source,
        parameterized,
        snapshot,
        input_sha256,
        plugin_binding=None if binding is None else cast(dict[str, Any], binding),
        dataset_version=dataset_version,
    )
    return {"frozen": envelope, "input_sha256": input_sha256, "input_identity": source_identity}


def _dataset_catalog(
    request: dict[str, Any],
) -> tuple[LocalResearchDatasetCatalogV1 | LocalResearchDatasetRegistryV1, ControlledUploadStoreV1 | None]:
    """The dataset entrances this workspace really has, and nothing else.

    A Web workspace has two: its registered read-only root, when one is
    configured, and its controlled upload store. A caller that names only a
    registered root gets exactly that, so a workspace without an upload store
    still resolves its registered inputs. Anything else — a per-dataset path, or
    a digest the caller computed — is not an entrance at all, because neither the
    dataset nor its version evidence is ever read from the request. The store is
    opened without its start-up sweep: this invocation is a concurrent reader of
    a store another owner may be publishing into.
    """
    registered: LocalResearchDatasetRegistryV1 | None = None
    raw_data_root = request.get("data_root")
    if raw_data_root is not None:
        _require(
            type(raw_data_root) is str and bool(raw_data_root),
            "invalid_request",
            "data_root must be one absolute directory",
        )
        try:
            registered = LocalResearchDatasetRegistryV1(Path(cast(str, raw_data_root)))
        except (OSError, ValueError) as error:
            raise DomainRefusal("invalid_request", f"the data root is unusable: {error}") from None
    raw_workspace = request.get("workspace_root")
    if raw_workspace is None:
        _require(
            registered is not None,
            "invalid_request",
            "a selected research input requires a dataset catalog this workspace owns",
        )
        return cast(LocalResearchDatasetRegistryV1, registered), None
    _require(
        type(raw_workspace) is str and bool(raw_workspace),
        "invalid_request",
        "workspace_root must be one absolute directory",
    )
    workspace = Path(cast(str, raw_workspace))
    _require(workspace.is_absolute(), "invalid_request", "workspace_root must be absolute")
    try:
        uploads = ControlledUploadStoreV1(workspace, sweep=False)
    except (OSError, ValueError) as error:
        raise DomainRefusal(
            "invalid_request", f"the controlled upload store is unusable: {error}"
        ) from None
    return LocalResearchDatasetCatalogV1(registered, uploads), uploads


def _published_version(
    uploads: ControlledUploadStoreV1 | None, dataset_id: str
) -> dict[str, Any] | None:
    """The published version evidence of one uploaded input, or ``None``.

    A registered read-only input publishes no version, which is carried as an
    absence — and a workspace with no upload store can hold no uploaded input at
    all. An uploaded one publishes the exact evidence digest a bound job freezes,
    and a version whose evidence can no longer be re-derived is refused rather
    than bound as if it had provenance.
    """
    if uploads is None or not uploads.has(dataset_id):
        return None
    try:
        return uploads.version_evidence(dataset_id)
    except (HistoricalMarketDataError, OSError, ValueError):
        raise DomainRefusal(
            "invalid_request",
            "the published version of this dataset can no longer be re-derived; "
            "re-select the dataset and confirm again",
        ) from None


def _bound_data_matches(binding: object, version: dict[str, Any]) -> bool:
    """Whether a binding's data component names exactly one published version."""
    if binding is None:
        return False
    if type(binding) is not dict:
        return False
    try:
        decoded = PluginBindingV1.decode(binding)
    except ManifestError:
        return False
    data = decoded.component("data")
    if data is None:
        return False
    return bool(
        data.plugin_id == f"ea.builtin.data.{version.get('dataset_id')}"
        and data.identity.get("version_evidence_sha256") == evidence_digest(version)
    )


def _mode_freeze(request: dict[str, Any]) -> dict[str, Any]:
    envelope, frozen, package = _decode_envelope(request.get("frozen"))
    scenario_path = Path(cast(str, request.get("scenario_path", "/dev/null")))
    scenario = _verified_scenario(envelope, frozen, package, scenario_path)
    # The confirmation is bound to a private snapshot of its own source here,
    # before anything is materialized: the worker runs from this capture and the
    # later independent validation reads the same one, so the run is a fact about
    # the confirmed bytes rather than about whatever a path holds afterwards.
    capture = _capture_source(scenario, request.get("capture_path"), envelope)
    scenario_file = cast(str, envelope["scenario_file"])
    scenario_file_sha256 = cast(str, envelope["scenario_file_sha256"])
    if capture is not None:
        # What is materialized reads its data from the private snapshot. The
        # scenario *identity* does not include a local file path, so this is the
        # same admitted scenario; the bytes the worker and the engine verify are
        # the bytes that name this attempt's own capture, which is what lets the
        # attempt's own manifest and its later re-verification agree with the
        # files the run really read instead of with a path that may since move.
        materialized = _rewrite_data_path(frozen, Path(cast(str, capture["path"])))
        scenario_file = materialized.hex()
        scenario_file_sha256 = _digest(materialized)
    return {
        "scenario_file": scenario_file,
        "scenario_file_sha256": scenario_file_sha256,
        "package_file": envelope["package_file"],
        "package_sha256": envelope["package_sha256"],
        "scenario_identity_sha256": envelope["scenario_identity_sha256"],
        # The exact verified research evidence a worker must re-read and
        # re-digest, or the fact that this input carries none. An envelope
        # written before these optional fields existed simply has none.
        "research_input": envelope.get("research_input"),
        "research_input_sha256": envelope.get("research_input_sha256"),
        "research_input_canonical": envelope.get("research_input_canonical"),
        "dataset_version": envelope.get("dataset_version"),
        "dataset_version_sha256": envelope.get("dataset_version_sha256"),
        "plugin_binding": envelope.get("plugin_binding"),
        "plugin_binding_sha256": envelope.get("plugin_binding_sha256"),
        "data_capture": capture,
        "job_schema": envelope["job_schema"],
        "report_schema": envelope["report_schema"],
        "path_schema": envelope["path_schema"],
    }


def _mode_validate(request: dict[str, Any]) -> dict[str, Any]:
    from ea.web.report_claims import read_field

    envelope, frozen, package = _decode_envelope(request.get("frozen"))
    attempt_id = request.get("attempt_id")
    job_id = request.get("job_id")
    _require(
        type(attempt_id) is str and bool(attempt_id),
        "invalid_request",
        "attempt_id is required",
    )
    _require(type(job_id) is str and bool(job_id), "invalid_request", "job_id is required")
    attempt_dir = _existing_directory(request.get("attempt_dir"), "attempt_dir")
    report_dir = _existing_directory(request.get("report_dir"), "report_dir")
    _require(
        attempt_dir.name == attempt_id,
        "identity_mismatch",
        "the staged attempt directory is not this attempt",
    )
    _require(
        report_dir.name == job_id,
        "identity_mismatch",
        "the staged report directory is not this job",
    )
    # The independent validation runs over the same private snapshot the worker
    # ran from, not over the path the original upload still happens to occupy: a
    # changed, moved or deleted source cannot change what this run is, and a
    # corrupted snapshot is refused by digest instead of being parsed.
    captured = _decoded_capture(request.get("data_capture"))
    scenario = _verified_scenario(
        envelope, frozen, package, attempt_dir.parent / "frozen.json", captured=captured
    )

    staged: dict[str, bytes] = {}
    for name in ARTIFACTS:
        staged[name] = read_regular(report_dir / name, report_dir)

    with tempfile.TemporaryDirectory(prefix="ea-quine-verify-") as temporary:
        root = Path(temporary).resolve()
        try:
            generated = generate_backtest_report(attempt_dir, root / "report")
            analysis = generate_equity_path_analysis(attempt_dir, generated.report)
        except Exception:  # noqa: BLE001 — unverifiable evidence is refused, never published
            raise DomainRefusal(
                "evidence_invalid", "the engine's own evidence could not be verified"
            ) from None
        regenerated = {
            "report.json": generated.report.canonical_bytes,
            "summary.txt": generated.report.summary_bytes,
            "equity-path.json": analysis.canonical_bytes,
        }

    for name in ARTIFACTS:
        _require(
            staged[name] == regenerated[name],
            "artifact_mismatch",
            f"the staged {name} is not the artifact this evidence produces",
        )
    report = json.loads(staged["report.json"])
    _require(
        type(report) is dict and report.get("run_id") == attempt_id,
        "identity_mismatch",
        "the report does not describe this attempt",
    )
    _require(
        report.get("schema") == envelope.get("report_schema"),
        "identity_mismatch",
        "the report schema is not the one this frozen input admits",
    )
    path_document = json.loads(staged["equity-path.json"])
    _require(
        type(path_document) is dict
        and path_document.get("schema") == envelope.get("path_schema")
        and path_document.get("run_id") == attempt_id,
        "identity_mismatch",
        "the equity path is not the one this frozen input admits",
    )
    _require(
        _report_source_matches(report, scenario, envelope),
        "identity_mismatch",
        "the report does not state the source this run was admitted from",
    )
    facts = []
    for path in FACT_PATHS:
        value = read_field(report, path)
        if value is not None:
            facts.append({"path": path, "value": value})
    return {
        "run_id": attempt_id,
        "artifacts": {name: _digest(staged[name]) for name in ARTIFACTS},
        "report_schema": envelope.get("report_schema"),
        "path_schema": envelope.get("path_schema"),
        "facts": facts,
    }


def _report_source_matches(
    report: dict[str, Any], scenario: LoadedBacktestScenario, envelope: dict[str, Any]
) -> bool:
    """Whether the report states exactly the source and data this input admitted.

    The engine's own manifest recorded the digest of the source bytes it read at
    execution. That reading is held to the admitted source — the confirmed
    research evidence's source digest when this input is a registered one, and
    the digest of the source actually loaded for this frozen input otherwise —
    so a run of a *different* full source cannot be registered merely because
    its selected fingerprint still matches.
    """
    source = report.get("source")
    if type(source) is not dict:
        return False
    if source.get("scenario_sha256") != envelope.get("scenario_identity_sha256"):
        return False
    data = source.get("data")
    if type(data) is not dict:
        return False
    fingerprint = data.get("fingerprint")
    if type(fingerprint) is not dict:
        return False
    research = envelope.get("research_input")
    admitted_source = (
        research.get("source_sha256") if type(research) is dict else None
    ) or scenario.dataset.source_bytes_sha256.value
    if data.get("source_file_sha256") != admitted_source:
        return False
    snapshot = cast(dict[str, Any], envelope["snapshot"])
    identity = snapshot.get("identity")
    if type(identity) is not dict:
        return False
    expected = identity.get("data_sha256")
    if type(expected) is not str or fingerprint.get("sha256") != expected:
        return False
    return bool(fingerprint.get("record_count") == identity.get("record_count"))


def _existing_directory(raw: object, field: str) -> Path:
    _require(type(raw) is str and bool(raw), "invalid_request", f"{field} is required")
    path = Path(cast(str, raw))
    _require(path.is_absolute(), "invalid_request", f"{field} must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise DomainRefusal("invalid_request", f"{field} is unavailable") from None
    _require(resolved == path and resolved.is_dir(), "invalid_request", f"{field} is invalid")
    return resolved


MODES = {"snapshot": _mode_snapshot, "freeze": _mode_freeze, "validate": _mode_validate}


# --- entry point ------------------------------------------------------------


def _write_reply(path: Path, document: dict[str, Any]) -> None:
    payload = canonical_json_bytes(document)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "wb", closefd=True) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def run(mode: str, payload: object) -> dict[str, Any]:
    """One mode over one request document; never raises for a refused request."""
    handler = MODES.get(mode)
    if handler is None:
        return _refused(mode, "invalid_request", "domain mode is unsupported")
    if type(payload) is not dict:
        return _refused(mode, "invalid_request", "domain request is not one document")
    try:
        result = handler(cast(dict[str, Any], payload))
    except DomainRefusal as refusal:
        return _refused(mode, refusal.code, refusal.message)
    except Exception:  # noqa: BLE001 — an unreadable request is refused, never guessed
        return _refused(mode, "internal", "the domain adapter could not complete this request")
    return {
        "schema": DOMAIN_SCHEMA,
        "mode": mode,
        "status": OK,
        "code": None,
        "message": None,
        "result": result,
    }


def _refused(mode: str, code: str, message: str) -> dict[str, Any]:
    return {
        "schema": DOMAIN_SCHEMA,
        "mode": mode,
        "status": REFUSED,
        "code": code,
        "message": message,
        "result": None,
    }


def _watch_parent(descriptor: int) -> None:
    """Die with the core that spawned this invocation.

    The core holds the only write end of this pipe, so a blocking read returns
    only when that end is closed — which happens when the core exits, crashes or
    is killed. Compute whose owner is gone must not keep running.
    """
    import threading

    def watch() -> None:
        try:
            while os.read(descriptor, 1):
                pass
        except OSError:
            pass
        os._exit(70)

    threading.Thread(target=watch, name="ea-quine-domain-liveness", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    positional: list[str] = []
    index = 0
    while index < len(arguments):
        if arguments[index] == "--parent-fd" and index + 1 < len(arguments):
            try:
                descriptor = int(arguments[index + 1])
            except ValueError:
                descriptor = 0
            if descriptor > 2:
                _watch_parent(descriptor)
            index += 2
            continue
        positional.append(arguments[index])
        index += 1
    if len(positional) != 3:
        return 2
    mode, request_path, reply_path = positional
    try:
        with open(request_path, "rb") as handle:
            payload = json.loads(handle.read())
    except (OSError, ValueError):
        _write_reply(
            Path(reply_path),
            _refused(mode, "invalid_request", "domain request is unreadable"),
        )
        return 0
    _write_reply(Path(reply_path), run(mode, payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
