"""Where a model is found from, and in what order.

This is what decides whether a packaged install works at all: the daemon has
no models of its own, so `find_model` is the only thing standing between
`pacman -S glanced` and a face-unlock that cannot see.
"""

import pytest

from glanced import paths


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """Three empty candidate directories, in search order."""
    places = {}
    for name, attribute in (("user", "MODEL_DIR"), ("repo", "_REPO_MODELS"), ("system", "SYSTEM_MODEL_DIR")):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setattr(paths, attribute, directory)
        places[name] = directory
    return places


def put(directory, name="face_landmarker.task"):
    (directory / name).write_bytes(b"model")
    return directory / name


def test_finds_a_model_the_package_installed(dirs):
    """The packaged case: nothing fetched, nothing checked out."""
    packaged = put(dirs["system"])
    assert paths.find_model("face_landmarker.task") == packaged


def test_a_fetched_model_wins_over_the_packaged_one(dirs):
    """`fetch-model` is an explicit act; honour it over what pacman dropped."""
    put(dirs["system"])
    fetched = put(dirs["user"])
    assert paths.find_model("face_landmarker.task") == fetched


def test_a_checkout_wins_over_the_packaged_one(dirs):
    """Working in a clone, with the package also installed."""
    put(dirs["system"])
    checkout = put(dirs["repo"])
    assert paths.find_model("face_landmarker.task") == checkout


def test_missing_everywhere_points_at_where_it_would_be_fetched(dirs):
    """So the error message names a path the user can act on."""
    assert paths.find_model("arcface.onnx") == dirs["user"] / "arcface.onnx"


def test_the_system_directory_is_overridable(monkeypatch, tmp_path):
    """Distributions that do not use /usr/share, and the tests themselves."""
    monkeypatch.setenv("GLANCE_SYSTEM_MODEL_DIR", str(tmp_path / "elsewhere"))
    import importlib

    reloaded = importlib.reload(paths)
    try:
        assert reloaded.SYSTEM_MODEL_DIR == tmp_path / "elsewhere"
    finally:
        monkeypatch.delenv("GLANCE_SYSTEM_MODEL_DIR")
        importlib.reload(paths)
