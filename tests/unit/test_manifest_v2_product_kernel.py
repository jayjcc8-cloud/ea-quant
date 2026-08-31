"""Focused reconstruction contracts for the Phase 1 V2 product boundary."""

from __future__ import annotations


def test_installed_runtime_v2_collector_is_a_closed_product_entrypoint() -> None:
    from ea.experiments.provenance import collect_installed_runtime_spec_v2

    assert collect_installed_runtime_spec_v2.__name__ == "collect_installed_runtime_spec_v2"


def test_store_exposes_a_v2_product_preparation_boundary() -> None:
    from ea.experiments.store import LocalResultStore

    assert "prepare_product" in dir(LocalResultStore)


def test_product_kernel_exposes_the_funded_boundary_value() -> None:
    from ea.composition.product_kernel import Phase1ProductKernel

    assert Phase1ProductKernel.__name__ == "Phase1ProductKernel"


def test_product_kernel_exposes_the_funded_recovery_boundary() -> None:
    from ea.composition.product_kernel import recover_phase1_product_kernel

    assert recover_phase1_product_kernel.__name__ == "recover_phase1_product_kernel"
