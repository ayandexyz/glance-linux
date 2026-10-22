"""The onnxruntime landmarker against MediaPipe's own output.

`tests/data/mediapipe_reference.npz` was produced by mediapipe 0.10.35
(FaceLandmarker, VIDEO mode, 4 frames at 50ms, output_facial_transformation_
matrixes) on exactly the JPEGs in tests/data/faces/ (see SOURCES.md there).
These tests need the two ONNX networks (repo `models/` or
$GLANCE_TEST_MODEL_DIR, e.g. after `glancectl fetch-model --landmarker onnx`),
onnxruntime and OpenCV, and skip without them. They do not need MediaPipe.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
pytest.importorskip("onnxruntime")

from glanced.landmarker_onnx import FaceGeometry, OneEuroSmoother, OnnxLandmarker  # noqa: E402
from glanced.liveness import LivenessAnalyzer, LivenessMode  # noqa: E402
from glanced.liveness.features import (  # noqa: E402
    LEFT_IRIS,
    REGION_INDICES,
    RIGHT_IRIS,
    extract,
    interocular_distance,
)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "tests" / "data"
BUNDLE = Path(os.environ.get("GLANCE_TEST_MODEL_DIR") or ROOT / "models")
pytestmark = pytest.mark.skipif(
    not (BUNDLE / "face_landmarks.onnx").exists(), reason="no model bundle (set GLANCE_TEST_MODEL_DIR)"
)

REFERENCE = np.load(DATA / "mediapipe_reference.npz")
META = json.loads(str(REFERENCE["meta"]))["results"]


def load(name: str, mirrored: bool = False) -> np.ndarray:
    rgb = cv2.cvtColor(cv2.imread(str(DATA / "faces" / name)), cv2.COLOR_BGR2RGB)
    return np.ascontiguousarray(rgb[:, ::-1]) if mirrored else rgb


def new_landmarker(**kwargs) -> OnnxLandmarker:
    return OnnxLandmarker(detector_path=BUNDLE / "face_detector.onnx", mesh_path=BUNDLE / "face_landmarks.onnx", **kwargs)


def track(rgb: np.ndarray, frames: int = 4):
    lm = new_landmarker()
    face = None
    for t in range(frames):
        face = lm.detect(rgb, t * 50)
    return face


CASES = sorted(k for k, v in META.items() if v is not None)


@pytest.mark.parametrize("key", CASES)
def test_matches_mediapipe_landmarks_and_pose(key):
    name, _, flag = key.partition(":")
    face = track(load(name, mirrored=flag == "mirrored"))
    assert face is not None
    reference = REFERENCE[key + "|mesh"].astype(float)
    assert face.mesh.shape == (478, 2), "the liveness index groups need the full FaceMesh topology"
    iod = interocular_distance(reference)
    error = np.linalg.norm(face.mesh - reference, axis=1) / iod
    assert error.mean() < 0.02, f"mean landmark error {error.mean():.4f} IOD"
    assert error[468:].mean() < 0.03, "iris points drive the IOD normalisation"
    assert abs(math.degrees(face.yaw - META[key]["yaw"])) < 1.5
    assert abs(math.degrees(face.pitch - META[key]["pitch"])) < 2.5
    assert face.five_points().shape == (5, 2)


def test_yaw_sign_follows_mediapipe_on_a_turned_head():
    left = track(load("mona_lisa.jpg"))
    right = track(load("mona_lisa.jpg", mirrored=True))
    assert math.degrees(left.yaw) < -10 and math.degrees(right.yaw) > 10


def test_no_face_means_none():
    assert track(load("noface_cats_and_dogs.jpg")) is None


def test_every_liveness_region_is_present_every_frame():
    face = track(load("portrait.jpg"))
    frame = extract(face.mesh, face.bounding_box, yaw=face.yaw, run_bezel_detection=False)
    expected = sum(len(v) for v in REGION_INDICES.values())
    assert len(frame.landmarks) == expected
    assert frame.has_reliable_landmarks
    assert max(LEFT_IRIS + RIGHT_IRIS) < len(face.mesh)


def _noisy_still_frames(count=30, seed=0):
    rng = np.random.default_rng(seed)
    base = load("portrait.jpg")
    frames = []
    for _ in range(count):
        dx, dy = rng.normal(0, 0.3, 2)
        shifted = cv2.warpAffine(base, np.float32([[1, 0, dx], [0, 1, dy]]), base.shape[1::-1],
                                 borderMode=cv2.BORDER_REFLECT).astype(np.float32)
        frames.append(np.clip(shifted + rng.normal(0, 3.0, shifted.shape), 0, 255).astype(np.uint8))
    return frames


def _jitter(lm, frames):
    meshes = np.array([lm.detect(f, i * 50).mesh for i, f in enumerate(frames)])[5:]
    return float(np.linalg.norm(meshes - meshes.mean(axis=0), axis=2).mean())


def test_one_euro_smoothing_matches_mediapipe_still_face_jitter():
    """MediaPipe measured 0.150px on this sequence; unsmoothed is ~0.39px."""
    frames = _noisy_still_frames()
    smoothed = _jitter(new_landmarker(), frames)
    raw = _jitter(new_landmarker(smooth=False), frames)
    assert smoothed < 0.22
    assert raw > 1.8 * smoothed


def test_smoother_follows_a_fast_move():
    smoother = OneEuroSmoother()
    points = np.zeros((478, 3))
    points[:, 0] = np.linspace(0, 100, 478)
    points[:, 1] = np.linspace(0, 120, 478)
    out = None
    for t in range(10):
        out = smoother.apply(points + [t * 20.0, 0, 0], t * 50)
    assert abs(out[:, 0].mean() - (points[:, 0].mean() + 180)) < 20, "beta 80 should track fast motion"


def test_geometry_is_identity_for_the_canonical_face():
    geometry = FaceGeometry()
    canonical = geometry.canonical.T
    # Place the canonical face in front of the default camera and project it.
    width, height = 640, 480
    f = (height / 2) / math.tan(math.radians(63) / 2)
    z = -50.0
    cam = canonical + [0, 0, z]
    x = f * cam[:, 0] / -cam[:, 2] + width / 2
    y = height / 2 - f * cam[:, 1] / -cam[:, 2]
    zpx = -(cam[:, 2] - z) * f / -z  # relative depth, same scale as x, toward camera negative
    matrix = geometry.transformation_matrix(np.column_stack([x, y, zpx]), width, height)
    rotation = matrix[:3, :3] / np.linalg.norm(matrix[:3, 0])
    angle = math.degrees(math.acos(np.clip((np.trace(rotation) - 1) / 2, -1, 1)))
    assert angle < 2.0


def _warp_photo(rgb, yaw_deg, rng, canvas=(720, 1280)):
    h_, w_ = canvas
    h, w = rgb.shape[:2]
    f = w_ * 1.0
    a = math.radians(yaw_deg)
    hw, hh = w / 2 * 0.9, h / 2 * 0.9
    dst = []
    for px, py in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
        X, Z = px * math.cos(a), f + px * math.sin(a)
        dst.append([f * X / Z + w_ / 2, f * py / Z + h_ / 2])
    m = cv2.getPerspectiveTransform(np.float32([[0, 0], [w, 0], [w, h], [0, h]]), np.float32(dst))
    out = cv2.warpPerspective(rgb, m, (w_, h_), dst=np.full((h_, w_, 3), 90, np.uint8),
                              borderMode=cv2.BORDER_TRANSPARENT).astype(np.float32)
    return np.clip(out + rng.normal(0, 2.0, out.shape), 0, 255).astype(np.uint8)


def test_a_synthetic_photo_warp_matches_mediapipe_in_heavy_mode():
    """Parity with MediaPipe, not an anti-spoofing guarantee: on a rendered
    +-30 degree perspective warp of a still photo MediaPipe reports only ~3.5
    degrees of 'yaw' and stays pending, so the port must as well."""
    from glanced.scan import FaceProcessor

    rng = np.random.default_rng(1)
    photo = load("portrait.jpg")
    processor = FaceProcessor.__new__(FaceProcessor)
    processor.landmarker = new_landmarker()
    processor.embedder = None
    analyzer = LivenessAnalyzer()
    analyzer.mode_provider = lambda: LivenessMode.HEAVY
    yaws = []
    snapshot = None
    for i in range(60):
        frame = _warp_photo(photo, 30 * math.sin(2 * math.pi * i / 40), rng)
        observation = processor.process(frame, now=1000.0 + i * 0.05, want_embedding=False)
        assert observation is not None
        yaws.append(math.degrees(observation.face.yaw))
        snapshot = analyzer.observe(observation.liveness_frame)
    assert max(yaws) - min(yaws) < 12.0
    assert not snapshot.decision.is_confirmed


def test_the_onnx_backend_never_imports_mediapipe():
    import subprocess
    import sys

    code = ("import sys; import glanced.landmarker_onnx; "
            "assert 'mediapipe' not in sys.modules, 'mediapipe was imported'")
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    subprocess.run([sys.executable, "-c", code], check=True, env=env)
