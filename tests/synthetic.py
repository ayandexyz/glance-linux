"""Synthetic face and spoof generators for the liveness tests.

The Linux counterpart of `tools/liveness_selftest.swift` upstream: drive the
real cue and evaluator logic frame by frame with no camera, no landmarker and
no ONNX, so the decision model can be regression-tested and retuned offline.

The key property under test is the one the whole flat-vs-3D cue rests on: a
real head has landmarks at different depths, so rotating it produces motion no
single homography can explain; a photograph — however it is rotated, tilted or
perspective-warped — is a plane, and a homography explains it exactly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

from glanced.liveness.frame import LandmarkPoint, LandmarkRegion, LivenessFrame

#: A crude 3D face in head-centred pixel units, scaled so the interocular
#: distance is 60px — a typical face at the 640px working resolution the tuning
#: constants assume. +x is the subject's right, +y is down, +z is toward the
#: camera. Only the depth *ordering* matters: eyes and brows near the plane,
#: nose held well out in front of it.
FACE_3D: dict[LandmarkRegion, np.ndarray] = {
    LandmarkRegion.LEFT_EYE: np.array(
        [(-38, -2, 0), (-30, -6, 1), (-22, -2, 0), (-30, 2, 1), (-34, -4, 0), (-26, -4, 0)],
        dtype=float,
    ),
    LandmarkRegion.RIGHT_EYE: np.array(
        [(22, -2, 0), (30, -6, 1), (38, -2, 0), (30, 2, 1), (26, -4, 0), (34, -4, 0)],
        dtype=float,
    ),
    LandmarkRegion.LEFT_EYEBROW: np.array(
        [(-42, -16, -2), (-34, -20, 0), (-26, -18, 0), (-20, -14, -2)], dtype=float
    ),
    LandmarkRegion.RIGHT_EYEBROW: np.array(
        [(20, -14, -2), (26, -18, 0), (34, -20, 0), (42, -16, -2)], dtype=float
    ),
    LandmarkRegion.OUTER_LIPS: np.array(
        [(-18, 44, -4), (-8, 40, 0), (0, 41, 2), (8, 40, 0), (18, 44, -4), (0, 50, 0)],
        dtype=float,
    ),
    LandmarkRegion.INNER_LIPS: np.array(
        [(-10, 44, -2), (0, 43, 1), (10, 44, -2), (0, 46, 0)], dtype=float
    ),
    # The probe set. Depth here is what the flat-vs-3D cue exists to find.
    LandmarkRegion.NOSE: np.array(
        [(0, 26, 22), (0, 30, 16), (-9, 30, 8), (9, 30, 8), (-6, 27, 12), (6, 27, 12)],
        dtype=float,
    ),
    LandmarkRegion.NOSE_CREST: np.array(
        [(0, -2, 6), (0, 6, 10), (0, 14, 15), (0, 22, 20)], dtype=float
    ),
    LandmarkRegion.MEDIAN_LINE: np.array([(0, -8, 4), (0, 36, 6)], dtype=float),
    LandmarkRegion.FACE_CONTOUR: np.array(
        [(-60, 0, -30), (-52, 30, -26), (0, 62, -8), (52, 30, -26), (60, 0, -30)], dtype=float
    ),
}

#: Regions the cues actually read. The contour is included in the mesh but sits
#: in neither the fit nor the probe set.
CENTER = np.array([320.0, 240.0])


@dataclass
class SyntheticConfig:
    #: Standard deviation of per-point Gaussian jitter, in pixels. Upstream's
    #: self-test found the flat-vs-3D level drops from 0.99 at zero noise to
    #: 0.46 at 0.5px and ~0.21 at 1px, which is why the fire threshold sits at
    #: 0.25 rather than the 0.5 the cue's shape would suggest.
    noise_px: float = 0.0
    #: Weak-perspective strength. 0 is orthographic.
    perspective: float = 0.0018
    seed: int = 20260909


def _project(points_3d: np.ndarray, yaw: float, config: SyntheticConfig, rng) -> np.ndarray:
    """Rotate about the vertical axis and project to 2D pixel space."""
    x, y, z = points_3d[:, 0], points_3d[:, 1], points_3d[:, 2]
    cos, sin = math.cos(yaw), math.sin(yaw)
    xr = x * cos + z * sin
    zr = -x * sin + z * cos
    # Weak perspective: nearer points project slightly larger. Present so the
    # homography has something genuinely projective to absorb in the flat case.
    scale = 1.0 / (1.0 - config.perspective * zr)
    projected = np.column_stack([xr * scale, y * scale]) + CENTER
    if config.noise_px > 0:
        projected = projected + rng.normal(0.0, config.noise_px, projected.shape)
    return projected


def _to_frame(
    by_region: dict[LandmarkRegion, np.ndarray],
    timestamp: float,
    yaw: Optional[float],
    *,
    reliable: bool = True,
    ear: Optional[float] = None,
    device_overlap: Optional[float] = None,
    glare=None,
) -> LivenessFrame:
    landmarks: list[LandmarkPoint] = []
    for region, points in by_region.items():
        for index, (px, py) in enumerate(points):
            landmarks.append(
                LandmarkPoint(x=float(px), y=float(py), region=region, index_in_region=index)
            )

    left = by_region[LandmarkRegion.LEFT_EYE].mean(axis=0)
    right = by_region[LandmarkRegion.RIGHT_EYE].mean(axis=0)
    iod = float(np.hypot(*(left - right)))
    nose = by_region[LandmarkRegion.NOSE].mean(axis=0)
    eye_mid_x = (left[0] + right[0]) / 2.0

    return LivenessFrame(
        timestamp=timestamp,
        landmarks=tuple(landmarks),
        interocular_distance=iod,
        yaw=yaw,
        left_eye_aspect_ratio=ear,
        right_eye_aspect_ratio=ear,
        nose_offset_ratio=float((nose[0] - eye_mid_x) / iod) if iod > 0 else None,
        has_reliable_landmarks=reliable,
        device_overlap_fraction=device_overlap,
        glare=glare,
    )


def live_face_window(
    frame_count: int = 12,
    yaw_sweep_degrees: float = 24.0,
    config: SyntheticConfig = SyntheticConfig(),
    fps: float = 20.0,
    ear_series: Optional[list[float]] = None,
) -> list[LivenessFrame]:
    """A real 3D head turning through `yaw_sweep_degrees`."""
    rng = np.random.default_rng(config.seed)
    yaws = np.linspace(
        -math.radians(yaw_sweep_degrees) / 2, math.radians(yaw_sweep_degrees) / 2, frame_count
    )
    frames = []
    for i, yaw in enumerate(yaws):
        projected = {r: _project(p, yaw, config, rng) for r, p in FACE_3D.items()}
        frames.append(
            _to_frame(
                projected,
                timestamp=i / fps,
                yaw=float(yaw),
                ear=ear_series[i] if ear_series else None,
            )
        )
    return frames


def flat_photo_window(
    frame_count: int = 12,
    yaw_sweep_degrees: float = 24.0,
    config: SyntheticConfig = SyntheticConfig(),
    fps: float = 20.0,
    ear_series: Optional[list[float]] = None,
    device_overlap: Optional[float] = None,
) -> list[LivenessFrame]:
    """The same face, printed flat and tilted through the same sweep.

    Every landmark is forced to z = 0 before projection, so the whole
    constellation is a plane. It still reports the same `yaw` values — a spoof
    that lied about its pose would be trivially detectable, and the cue must not
    depend on the attacker being honest.
    """
    rng = np.random.default_rng(config.seed)
    flat = {r: np.column_stack([p[:, 0], p[:, 1], np.zeros(len(p))]) for r, p in FACE_3D.items()}
    yaws = np.linspace(
        -math.radians(yaw_sweep_degrees) / 2, math.radians(yaw_sweep_degrees) / 2, frame_count
    )
    frames = []
    for i, yaw in enumerate(yaws):
        projected = {r: _project(p, yaw, config, rng) for r, p in flat.items()}
        frames.append(
            _to_frame(
                projected,
                timestamp=i / fps,
                yaw=float(yaw),
                ear=ear_series[i] if ear_series else None,
                device_overlap=device_overlap,
            )
        )
    return frames


def still_face_window(
    frame_count: int = 12, config: SyntheticConfig = SyntheticConfig(), fps: float = 20.0
) -> list[LivenessFrame]:
    """A live person holding perfectly still and not blinking — the case Heavy
    mode is documented to be unable to confirm."""
    rng = np.random.default_rng(config.seed)
    frames = []
    for i in range(frame_count):
        projected = {r: _project(p, 0.0, config, rng) for r, p in FACE_3D.items()}
        frames.append(_to_frame(projected, timestamp=i / fps, yaw=0.0))
    return frames


def blink_ear_series(frame_count: int, dip_at: int = 6) -> list[float]:
    """Eye-aspect-ratio series containing one blink: open, a dip well under the
    0.65 threshold, then recovery."""
    series = [0.32] * frame_count
    if 0 < dip_at < frame_count - 1:
        series[dip_at - 1] = 0.22
        series[dip_at] = 0.09
        series[dip_at + 1] = 0.24
    return series
