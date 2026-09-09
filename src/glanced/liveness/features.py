"""The landmarker-facing half of liveness: turns one detected face (plus the
camera frames it came from) into a :class:`LivenessFrame` — plain points and
scalars, which is all the cue functions ever see.

Port of `glance/Liveness/LivenessFeatures.swift`, plus the Vision-facing
accessors from `LandmarkGeometry.swift` (`imagePoints`, `region`, `allPoints`,
`interocularDistance`, `eyeCenter`), which belong on this side of the split.

Keeping the landmarker-facing extraction isolated to this one file is what lets
the decision logic run standalone against synthetic data with no MediaPipe,
ONNX, or camera in the dependency chain.

MediaPipe FaceMesh has no named regions — it emits a fixed 478-point topology —
so the region index groups below stand in for Vision's named accessors. Two
consequences worth knowing:

* Correspondence across frames is *guaranteed* here, where upstream has to
  defend against Vision dropping a region between frames. The per-region count
  checks in `planar.py` are kept anyway; they cost nothing and a future
  landmarker may not be so well-behaved.
* MEDIAN_LINE is narrowed relative to Vision's. Vision's median line runs
  forehead to chin, but `planar.PROBE_REGIONS` requires probe points to sit
  geometrically *inside* the fit hull (eyes, eyebrows, outer lips) so that
  leftover error reads as depth rather than extrapolation. Forehead and chin
  points are outside that hull, so only the two midline points between the brow
  and lip lines are used.
"""

from __future__ import annotations

import math
import time
from typing import Optional, Sequence

import numpy as np

from . import bezel, glare as glare_module
from .frame import LandmarkPoint, LandmarkRegion, LivenessFrame
from .scoring import eye_aspect_ratio

# --- MediaPipe FaceMesh index groups -----------------------------------------
# "Left" and "right" follow MediaPipe's own convention (subject's left is the
# viewer's right). Only internal consistency matters for every measure here.

LEFT_EYE = (33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246)
RIGHT_EYE = (362, 382, 381, 380, 374, 373, 390, 249, 263, 466, 388, 387, 386, 385, 384, 398)
LEFT_EYEBROW = (70, 63, 105, 66, 107, 55, 65, 52, 53, 46)
RIGHT_EYEBROW = (300, 293, 334, 296, 336, 285, 295, 282, 283, 276)
#: Tip, columella, subnasale and both alae — the protruding structure the
#: flat-vs-3D and depth/pose cues both depend on.
NOSE = (1, 2, 94, 97, 98, 115, 326, 327, 344)
#: The dorsum, from nasion down to the tip.
NOSE_CREST = (168, 6, 197, 195, 5, 4)
OUTER_LIPS = (61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 409, 270, 269, 267, 0, 37, 39, 40, 185)
INNER_LIPS = (78, 95, 88, 178, 87, 14, 317, 402, 318, 324, 308, 415, 310, 311, 312, 13, 82, 81, 80, 191)
FACE_CONTOUR = (
    10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379,
    378, 400, 377, 152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127,
    162, 21, 54, 103, 67, 109,
)
#: Narrowed to the midline between the brow and lip lines — see module docstring.
MEDIAN_LINE = (8, 164)

#: Iris centres, present only when the landmarker is run with iris refinement.
#: Preferred over the eye outline's centroid, which is an approximation from
#: eyelid boundary points.
LEFT_IRIS = (468, 469, 470, 471, 472)
RIGHT_IRIS = (473, 474, 475, 476, 477)

REGION_INDICES: dict[LandmarkRegion, tuple[int, ...]] = {
    LandmarkRegion.LEFT_EYE: LEFT_EYE,
    LandmarkRegion.RIGHT_EYE: RIGHT_EYE,
    LandmarkRegion.LEFT_EYEBROW: LEFT_EYEBROW,
    LandmarkRegion.RIGHT_EYEBROW: RIGHT_EYEBROW,
    LandmarkRegion.NOSE: NOSE,
    LandmarkRegion.NOSE_CREST: NOSE_CREST,
    LandmarkRegion.OUTER_LIPS: OUTER_LIPS,
    LandmarkRegion.INNER_LIPS: INNER_LIPS,
    LandmarkRegion.FACE_CONTOUR: FACE_CONTOUR,
    LandmarkRegion.MEDIAN_LINE: MEDIAN_LINE,
}

#: Below this interocular distance there is not enough pixel detail for the
#: precision cues, and residual noise spikes. Stands in for upstream's
#: `alignmentTier == .fivePoint` check, which serves the same purpose.
MIN_RELIABLE_INTEROCULAR_PX = 36.0


def _points(mesh: np.ndarray, indices: Sequence[int]) -> np.ndarray:
    """Rows of an (N, 2) pixel-space mesh, for the given indices."""
    valid = [i for i in indices if i < len(mesh)]
    if not valid:
        return np.empty((0, 2), dtype=float)
    return mesh[valid]


def _centroid(points: np.ndarray) -> Optional[np.ndarray]:
    if len(points) == 0:
        return None
    return points.mean(axis=0)


def all_points(mesh: np.ndarray) -> list[LandmarkPoint]:
    """Every point in every region, tagged with its region and position within
    that region so a later frame's points can be matched back up to these."""
    result: list[LandmarkPoint] = []
    for region, indices in REGION_INDICES.items():
        for position, index in enumerate(indices):
            if index >= len(mesh):
                continue
            result.append(
                LandmarkPoint(
                    x=float(mesh[index][0]),
                    y=float(mesh[index][1]),
                    region=region,
                    index_in_region=position,
                )
            )
    return result


def eye_center(mesh: np.ndarray, iris: Sequence[int], eye: Sequence[int]) -> Optional[np.ndarray]:
    """Iris centroid when available, else the eye outline's centroid."""
    center = _centroid(_points(mesh, iris))
    if center is not None:
        return center
    return _centroid(_points(mesh, eye))


def interocular_distance(mesh: np.ndarray) -> Optional[float]:
    """Distance between the two eye centres — the normalization scale used
    throughout liveness scoring (residuals, nose offset, blink depth are all
    expressed as a fraction of this), so scores stay comparable regardless of
    how close the face is to the camera."""
    left = eye_center(mesh, LEFT_IRIS, LEFT_EYE)
    right = eye_center(mesh, RIGHT_IRIS, RIGHT_EYE)
    if left is None or right is None:
        return None
    return float(np.hypot(*(left - right)))


def yaw_from_transformation_matrix(matrix: np.ndarray) -> Optional[float]:
    """Yaw in radians from MediaPipe's 4x4 facial transformation matrix.

    The upper-left 3x3 is a rotation; yaw is its Y-axis component, recovered as
    ``atan2(-r20, hypot(r21, r22))`` — the standard extraction that stays stable
    through the pitch range a seated user actually presents.
    """
    m = np.asarray(matrix, dtype=float)
    if m.shape != (4, 4) and m.shape != (3, 3):
        return None
    r = m[:3, :3]
    value = float(np.clip(-r[2, 0], -1.0, 1.0))
    return float(math.atan2(value, float(np.hypot(r[2, 1], r[2, 2]))))


def extract(
    mesh: np.ndarray,
    face_bounding_box: Sequence[float],
    *,
    frame_rgb: Optional[np.ndarray] = None,
    face_crop: Optional[np.ndarray] = None,
    yaw: Optional[float] = None,
    timestamp: Optional[float] = None,
    run_bezel_detection: bool = True,
) -> LivenessFrame:
    """Extract a :class:`LivenessFrame` from one detected face.

    Never fails — a face with an unusable mesh still yields a frame (with no
    landmarks), since pose and device-overlap data alone is worth having in the
    window; cues that need landmarks just abstain on it.

    :param mesh: (N, 2) landmark array in top-left-origin pixel space.
    :param face_bounding_box: (x, y, w, h) in the same pixel space.
    :param frame_rgb: the *full* camera frame this face was detected in — not
        the aligned crop, which is a tightly-cropped, pose-normalized warp with
        no room around the face to see a device edge in. Needed by the bezel
        detector, which has to look around the face, not just at it.
    :param face_crop: a native-resolution RGB crop around the face, used for the
        gloss/glare cue. None when no native-resolution frame was available —
        that cue simply abstains, same as landmark-dependent cues do.
    """
    ts = time.monotonic() if timestamp is None else timestamp

    device_overlap: Optional[float] = None
    if run_bezel_detection and frame_rgb is not None:
        device_overlap = bezel.detect(frame_rgb, face_bounding_box).face_overlap_fraction

    glare_sample = glare_module.extract(face_crop) if face_crop is not None else None

    mesh = np.asarray(mesh, dtype=float).reshape(-1, 2) if mesh is not None else np.empty((0, 2))
    if len(mesh) == 0:
        return LivenessFrame(
            timestamp=ts,
            landmarks=(),
            interocular_distance=None,
            yaw=yaw,
            has_reliable_landmarks=False,
            device_overlap_fraction=device_overlap,
            glare=glare_sample,
        )

    iod = interocular_distance(mesh)
    left_ear = eye_aspect_ratio(_points(mesh, LEFT_EYE))
    right_ear = eye_aspect_ratio(_points(mesh, RIGHT_EYE))

    nose_offset_ratio: Optional[float] = None
    left_center = eye_center(mesh, LEFT_IRIS, LEFT_EYE)
    right_center = eye_center(mesh, RIGHT_IRIS, RIGHT_EYE)
    nose_center = _centroid(_points(mesh, NOSE))
    if iod is not None and iod > 0 and left_center is not None and right_center is not None and nose_center is not None:
        eye_mid_x = (left_center[0] + right_center[0]) / 2.0
        nose_offset_ratio = float((nose_center[0] - eye_mid_x) / iod)

    return LivenessFrame(
        timestamp=ts,
        landmarks=tuple(all_points(mesh)),
        interocular_distance=iod,
        yaw=yaw,
        left_eye_aspect_ratio=left_ear,
        right_eye_aspect_ratio=right_ear,
        nose_offset_ratio=nose_offset_ratio,
        has_reliable_landmarks=iod is not None and iod >= MIN_RELIABLE_INTEROCULAR_PX,
        device_overlap_fraction=device_overlap,
        glare=glare_sample,
    )
