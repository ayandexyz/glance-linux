"""Model installation and pinning: nothing is installed or loaded unless its
SHA-256 is the pinned one. Small stand-in models, no network."""

from __future__ import annotations

import hashlib
import io
import os
import zipfile
from pathlib import Path

import pytest

from glanced import models, paths


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def fake_specs(monkeypatch):
    blobs = {"task": b"landmarker-bytes", "arc": b"arcface-bytes"}
    monkeypatch.setattr(models, "LANDMARKER", models.ModelSpec(
        "face_landmarker.task", sha(blobs["task"]), url="https://example.invalid/face_landmarker.task"))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("buffalo_s/w600k_mbf.onnx", blobs["arc"])
    archive_bytes = archive.getvalue()
    spec = models.ModelSpec("arcface.onnx", sha(blobs["arc"]), source_names=("w600k_mbf.onnx",),
                            archive=("https://example.invalid/buffalo_s.zip", sha(archive_bytes), "w600k_mbf.onnx"))
    monkeypatch.setattr(models, "ARCFACE_VARIANTS", {"mbf": spec})
    monkeypatch.setattr(models, "ARCFACE_HASHES", frozenset({spec.sha256}))
    monkeypatch.delenv("GLANCE_MODEL_SOURCE", raising=False)
    models._hash_cache.clear()
    return blobs, archive_bytes


def network(monkeypatch, table):
    fetched = []

    def download(url, label):
        fetched.append(url)
        return table[url]

    monkeypatch.setattr(models, "_download", download)
    return fetched


def test_verify_accepts_the_pinned_bytes_and_refuses_others(tmp_path):
    good = tmp_path / "m.onnx"
    good.write_bytes(b"abc")
    assert models.verify(good, sha(b"abc")) == good
    with pytest.raises(models.ModelIntegrityError, match="not the pinned model"):
        models.verify(good, sha(b"abd"))
    with pytest.raises(models.ModelIntegrityError, match="not found"):
        models.verify(tmp_path / "missing.onnx", sha(b"abc"))


def test_an_integrity_error_is_handled_wherever_a_missing_model_was():
    assert issubclass(models.ModelIntegrityError, FileNotFoundError)


def test_a_file_changed_after_it_was_hashed_is_caught(tmp_path):
    path = tmp_path / "m.onnx"
    path.write_bytes(b"abc")
    models.verify(path, sha(b"abc"))
    path.write_bytes(b"xyz")
    os.utime(path, ns=(1, 1))
    with pytest.raises(models.ModelIntegrityError):
        models.verify(path, sha(b"abc"))


def test_fetch_downloads_and_checks_both_models(fake_specs, tmp_path, monkeypatch):
    blobs, archive_bytes = fake_specs
    network(monkeypatch, {"https://example.invalid/face_landmarker.task": blobs["task"],
                          "https://example.invalid/buffalo_s.zip": archive_bytes})
    models.fetch(model_dir=tmp_path)
    assert (tmp_path / "face_landmarker.task").read_bytes() == blobs["task"]
    assert (tmp_path / "arcface.onnx").read_bytes() == blobs["arc"]


def test_a_download_with_the_wrong_bytes_is_not_installed(fake_specs, tmp_path, monkeypatch):
    blobs, archive_bytes = fake_specs
    network(monkeypatch, {"https://example.invalid/face_landmarker.task": b"something else",
                          "https://example.invalid/buffalo_s.zip": archive_bytes})
    with pytest.raises(models.ModelIntegrityError, match="face_landmarker.task has sha256"):
        models.fetch(model_dir=tmp_path)
    assert not (tmp_path / "face_landmarker.task").exists()


def test_arcface_checks_the_archive_and_the_member(fake_specs, tmp_path, monkeypatch):
    blobs, archive_bytes = fake_specs
    (tmp_path / "face_landmarker.task").write_bytes(blobs["task"])
    network(monkeypatch, {"https://example.invalid/buffalo_s.zip": archive_bytes + b"x"})
    with pytest.raises(models.ModelIntegrityError, match="buffalo_s.zip has sha256"):
        models.fetch(model_dir=tmp_path)
    assert not (tmp_path / "arcface.onnx").exists()

    other = io.BytesIO()
    with zipfile.ZipFile(other, "w") as z:
        z.writestr("buffalo_s/w600k_mbf.onnx", b"a different network")
    spec = models.ARCFACE_VARIANTS["mbf"]
    monkeypatch.setattr(models, "ARCFACE_VARIANTS", {"mbf": models.ModelSpec(
        spec.name, spec.sha256, archive=(spec.archive[0], sha(other.getvalue()), spec.archive[2]))})
    network(monkeypatch, {"https://example.invalid/buffalo_s.zip": other.getvalue()})
    with pytest.raises(models.ModelIntegrityError, match="w600k_mbf.onnx in"):
        models.fetch(model_dir=tmp_path)
    assert not (tmp_path / "arcface.onnx").exists()


def test_fetch_from_a_local_copy_never_touches_the_network(fake_specs, tmp_path, monkeypatch):
    blobs, _ = fake_specs
    source = tmp_path / "copy"
    source.mkdir()
    (source / "face_landmarker.task").write_bytes(blobs["task"])
    (source / "w600k_mbf.onnx").write_bytes(blobs["arc"])
    fetched = network(monkeypatch, {})
    models.fetch(model_dir=tmp_path / "models", source=source)
    assert fetched == []
    assert (tmp_path / "models" / "arcface.onnx").read_bytes() == blobs["arc"]


def test_a_tampered_local_copy_is_skipped_for_the_download(fake_specs, tmp_path, monkeypatch):
    blobs, archive_bytes = fake_specs
    source = tmp_path / "copy"
    source.mkdir()
    (source / "face_landmarker.task").write_bytes(b"tampered")
    monkeypatch.setenv("GLANCE_MODEL_SOURCE", str(source))
    fetched = network(monkeypatch, {"https://example.invalid/face_landmarker.task": blobs["task"],
                                    "https://example.invalid/buffalo_s.zip": archive_bytes})
    models.fetch(model_dir=tmp_path / "models")
    assert "https://example.invalid/face_landmarker.task" in fetched
    assert (tmp_path / "models" / "face_landmarker.task").read_bytes() == blobs["task"]


def test_verified_models_are_left_alone_and_a_bad_one_is_replaced(fake_specs, tmp_path, monkeypatch):
    blobs, archive_bytes = fake_specs
    (tmp_path / "face_landmarker.task").write_bytes(blobs["task"])
    (tmp_path / "arcface.onnx").write_bytes(b"corrupt")
    fetched = network(monkeypatch, {"https://example.invalid/buffalo_s.zip": archive_bytes})
    models.fetch(model_dir=tmp_path)
    assert fetched == ["https://example.invalid/buffalo_s.zip"]
    assert (tmp_path / "arcface.onnx").read_bytes() == blobs["arc"]


def test_status_means_present_and_pinned(fake_specs, tmp_path, monkeypatch):
    blobs, _ = fake_specs
    monkeypatch.setattr(paths, "landmarker_task", lambda: tmp_path / "face_landmarker.task")
    monkeypatch.setattr(paths, "arcface_model", lambda: tmp_path / "arcface.onnx")
    assert models.status() == {"landmarker": False, "arcface": False}
    (tmp_path / "face_landmarker.task").write_bytes(blobs["task"])
    (tmp_path / "arcface.onnx").write_bytes(b"not arcface")
    assert models.status() == {"landmarker": True, "arcface": False}


def test_the_embedder_refuses_an_unpinned_model(tmp_path):
    pytest.importorskip("onnxruntime")
    from glanced.embed import ArcFaceEmbedder

    bogus = tmp_path / "arcface.onnx"
    bogus.write_bytes(b"not arcface")
    with pytest.raises(models.ModelIntegrityError):
        ArcFaceEmbedder(bogus)


def test_the_pins_are_well_formed():
    specs = [models.LANDMARKER, *models.ARCFACE_VARIANTS.values()]
    for spec in specs:
        assert len(spec.sha256) == 64 and int(spec.sha256, 16) >= 0
        assert (spec.url is None) != (spec.archive is None)
    assert models.ARCFACE_PACKS["mbf"][1] == "w600k_mbf.onnx"
