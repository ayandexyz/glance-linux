"""Which landmarker runs, and which model files that means."""

from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np
import pytest

from glanced import landmarker, models, paths

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = Path(os.environ.get("GLANCE_TEST_MODEL_DIR") or ROOT / "models")


@pytest.fixture(autouse=True)
def no_forced_backend(monkeypatch):
    monkeypatch.delenv("GLANCE_LANDMARKER", raising=False)


def test_mediapipe_when_installed_else_onnx(monkeypatch):
    monkeypatch.setattr(landmarker, "mediapipe_available", lambda: True)
    assert landmarker.backend() == "mediapipe"
    monkeypatch.setattr(landmarker, "mediapipe_available", lambda: False)
    assert landmarker.backend() == "onnx"


def test_glance_landmarker_forces_a_backend(monkeypatch):
    monkeypatch.setattr(landmarker, "mediapipe_available", lambda: True)
    monkeypatch.setenv("GLANCE_LANDMARKER", "ONNX")
    assert landmarker.backend() == "onnx"
    monkeypatch.setenv("GLANCE_LANDMARKER", "tflite")
    with pytest.raises(ValueError, match="GLANCE_LANDMARKER"):
        landmarker.backend()


def test_each_backend_fetches_its_own_files():
    assert [s.name for s in models.landmarker_specs("mediapipe")] == ["face_landmarker.task"]
    assert [s.name for s in models.landmarker_specs("onnx")] == ["face_detector.onnx", "face_landmarks.onnx"]


def test_status_follows_the_active_backend(tmp_path, monkeypatch):
    for name in ("face_landmarker.task", "face_detector.onnx", "face_landmarks.onnx", "arcface.onnx"):
        (tmp_path / name).write_bytes(b"x")
    monkeypatch.setattr(paths, "landmarker_task", lambda: tmp_path / "face_landmarker.task")
    monkeypatch.setattr(paths, "face_detector_model", lambda: tmp_path / "face_detector.onnx")
    monkeypatch.setattr(paths, "face_mesh_model", lambda: tmp_path / "face_landmarks.onnx")
    monkeypatch.setattr(models, "LANDMARKER", models.ModelSpec("face_landmarker.task", models.sha256_file(tmp_path / "face_landmarker.task")))
    monkeypatch.setattr(models, "FACE_DETECTOR", models.ModelSpec("face_detector.onnx", "0" * 64))
    monkeypatch.setattr(models, "FACE_MESH", models.ModelSpec("face_landmarks.onnx", "0" * 64))
    # All three files exist; only the pins decide.
    monkeypatch.setenv("GLANCE_LANDMARKER", "mediapipe")
    assert models.status()["landmarker"] is True
    monkeypatch.setenv("GLANCE_LANDMARKER", "onnx")
    assert models.status()["landmarker"] is False


def test_fetch_all_installs_both_backends(tmp_path, monkeypatch):
    source = tmp_path / "copy"
    source.mkdir()
    specs = {}
    for attr, name in (("LANDMARKER", "face_landmarker.task"), ("FACE_DETECTOR", "face_detector.onnx"),
                       ("FACE_MESH", "face_landmarks.onnx")):
        (source / name).write_bytes(name.encode())
        specs[attr] = models.ModelSpec(name, models.sha256_file(source / name))
        monkeypatch.setattr(models, attr, specs[attr])
    (source / "arcface.onnx").write_bytes(b"arc")
    arc = models.ModelSpec("arcface.onnx", models.sha256_file(source / "arcface.onnx"))
    monkeypatch.setattr(models, "ARCFACE_VARIANTS", {"mbf": arc})
    monkeypatch.setattr(models, "ARCFACE_HASHES", frozenset({arc.sha256}))
    models.fetch(model_dir=tmp_path / "models", source=source, backend="all")
    assert sorted(p.name for p in (tmp_path / "models").iterdir()) == [
        "arcface.onnx", "face_detector.onnx", "face_landmarker.task", "face_landmarks.onnx"]


def test_the_scan_pipeline_builds_whichever_backend_is_active(monkeypatch):
    from glanced import scan

    built = []
    monkeypatch.setattr(scan, "create_landmarker", lambda task=None: built.append(task) or "landmarker")
    processor = scan.FaceProcessor(embed=False)
    assert built == [None] and processor.landmarker == "landmarker"


# --- the two backends against each other, where both can run ---------------------


def _have(*names):
    return all((BUNDLE / n).exists() for n in names)


@pytest.mark.skipif(not landmarker.mediapipe_available(), reason="MediaPipe not installed (normal on aarch64)")
@pytest.mark.skipif(not _have("face_landmarker.task", "face_detector.onnx", "face_landmarks.onnx"),
                    reason="needs face_landmarker.task and the ONNX networks in $GLANCE_TEST_MODEL_DIR")
@pytest.mark.parametrize("name", ["portrait.jpg", "mona_lisa.jpg", "astronaut.jpg"])
def test_both_backends_agree_on_the_same_frames(name):
    cv2 = pytest.importorskip("cv2")
    from glanced.landmarker_onnx import OnnxLandmarker

    rgb = cv2.cvtColor(cv2.imread(str(ROOT / "tests" / "data" / "faces" / name)), cv2.COLOR_BGR2RGB)
    mp = landmarker.MediaPipeLandmarker(BUNDLE / "face_landmarker.task")
    ox = OnnxLandmarker(detector_path=BUNDLE / "face_detector.onnx", mesh_path=BUNDLE / "face_landmarks.onnx")
    a = b = None
    for t in range(4):
        a, b = mp.detect(rgb, t * 50), ox.detect(rgb, t * 50)
    mp.close()
    from glanced.liveness.features import interocular_distance

    iod = interocular_distance(a.mesh)
    assert (np.linalg.norm(a.mesh - b.mesh, axis=1) / iod).mean() < 0.02
    assert abs(math.degrees(a.yaw - b.yaw)) < 1.5
