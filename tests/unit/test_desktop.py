"""Desktop data placement must remain stable and separate from the bundle."""

from pathlib import Path

import pytest

from ea.desktop import install_samples, prepare_root


def test_desktop_upgrade_preserves_user_scenes_and_settings(tmp_path: Path) -> None:
    runtime = tmp_path / "bundle"
    samples = runtime / "samples"
    samples.mkdir(parents=True)
    (samples / "synthetic-bounded-long.yaml").write_text("original")
    (samples / "prices.csv").write_text("synthetic")
    root = tmp_path / "data"
    prepare_root(root, runtime)
    install_samples(root, runtime)
    scene = root / "scenarios/synthetic-bounded-long.yaml"
    scene.write_text("user revised")
    settings = root / "settings.json"
    settings.write_text('{"schema":"radian.settings.v1"}')
    (samples / "synthetic-bounded-long.yaml").write_text("new release")
    prepare_root(root, runtime)
    install_samples(root, runtime)
    assert scene.read_text() == "user revised"
    assert settings.read_text() == '{"schema":"radian.settings.v1"}'
    assert root.stat().st_mode & 0o777 == 0o700


def test_desktop_refuses_bundle_and_symlink_data_paths(tmp_path: Path) -> None:
    runtime = tmp_path / "bundle"
    runtime.mkdir()
    with pytest.raises(ValueError, match="separate"):
        prepare_root(runtime / "data", runtime)
    root = tmp_path / "data"
    root.symlink_to(runtime, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        prepare_root(root, runtime)
    root2 = tmp_path / "data2"
    root2.mkdir()
    (root2 / "workspace").symlink_to(runtime, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        prepare_root(root2, runtime)
