"""Focused reconstruction contracts for the Phase 1 V2 product boundary."""

from __future__ import annotations


def test_installed_runtime_v2_collector_is_a_closed_product_entrypoint() -> None:
    from ea.experiments.provenance import collect_installed_runtime_spec_v2

    assert collect_installed_runtime_spec_v2.__name__ == "collect_installed_runtime_spec_v2"


def test_store_exposes_a_v2_product_preparation_boundary() -> None:
    from ea.experiments.store import LocalResultStore

    assert "prepare_product" in dir(LocalResultStore)
