"""The two cross-frame confirm cues that read the rolling window directly:
depth/pose consistency and blink dynamics.

Port of the scoring half of `glance/Liveness/LivenessScoring.swift`. The third
confirm cue, flat-vs-3D, lives in `planar.py`; the two deny cues are per-frame
appearance measurements handled in `cues.py`.

Upstream this file once held nine more scoring functions feeding a weighted
"overall liveness %" (non-rigid residual, residual coherence, scale dynamics,
temporal naturalness, mouth dynamics, a static guard, a duplicate device-bezel
check). Real-device testing retired all of them: most were noise-limited at
webcam resolution, and several scored a hand holding up a phone *higher* than a
live face, because smooth low-jerk motion is exactly what a held phone
produces. They are not reintroduced here.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

import numpy as np

from .frame import NO_READING, CueReading, LivenessFrame
from .geometry import clamp


#: Minimum ``d(nose offset ratio) / d(tan yaw)`` before the depth/pose cue will
#: confirm — physically, the nose's depth expressed as a fraction of the
#: interocular distance.
#:
#: This gate is a deviation from upstream, added because porting the cue and
#: measuring it against a synthetic plane exposed a hole. Upstream gates on the
#: Pearson correlation alone, documented as "a genuinely positive relationship
#: of the kind only a nose sitting off the eye plane produces". That is not true
#: of a plane viewed in perspective: a tilted photo's apparent eye midpoint
#: shifts relative to its nose, because perspective projection does not preserve
#: midpoints. The shift is tiny — but correlation is scale-free and cannot tell
#: a tiny systematic drift from a large one, so it reads r ~ 1.0 and the cue
#: confirms a photograph. Measured on `tests/synthetic.py`, a flat photo scores
#: 1.00 at zero landmark noise and 0.95 at 0.25px, both well over the 0.8 fire
#: threshold; it only falls under the threshold around 1px of jitter, i.e. the
#: cue was relying on noise to hide the artifact.
#:
#: The slope does not have that problem, because it is the physical quantity the
#: cue is actually reasoning about. On the same synthetic data it is ~0.24 for a
#: real nose and ~0.026 for a plane, stable across 0-1px of noise. 0.08 sits an
#: order of magnitude clear of the plane artifact and well under any real nose.
MIN_NOSE_DEPTH_RATIO = 0.08


def _regression_slope(xs: np.ndarray, ys: np.ndarray) -> Optional[float]:
    """Least-squares slope of ``xs`` (nose offset) against ``ys`` (tan yaw)."""
    var_y = float(np.sum((ys - ys.mean()) ** 2))
    if var_y <= 0:
        return None
    return float(np.sum((xs - xs.mean()) * (ys - ys.mean())) / var_y)


def _pearson_correlation(xs: np.ndarray, ys: np.ndarray) -> Optional[float]:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    dx = xs - xs.mean()
    dy = ys - ys.mean()
    var_x = float(np.sum(dx * dx))
    var_y = float(np.sum(dy * dy))
    if var_x <= 0 or var_y <= 0:
        return None
    return float(np.sum(dx * dy) / math.sqrt(var_x * var_y))


def pose_depth_consistency(window: Sequence[LivenessFrame]) -> CueReading:
    """Correlates nose-offset-from-eye-midline against tan(yaw) across the
    window.

    On a real face the nose protrudes off the eye plane, so its apparent offset
    tracks yaw; on any flat presentation the offset stays constant no matter how
    the plane is rotated.

    Confidence scales with the actual yaw range observed. At the small rotations
    a passive, non-challenge scan realistically sees (a couple of degrees), the
    geometric displacement this predicts is well under a pixel — below the
    landmarker's noise floor — so the cue deliberately abstains rather than read
    noise.
    """
    pairs = [
        (f.nose_offset_ratio, math.tan(f.yaw))
        for f in window
        if f.nose_offset_ratio is not None and f.yaw is not None and f.has_reliable_landmarks
    ]
    if len(pairs) < 4:
        return NO_READING

    offsets = np.array([p[0] for p in pairs], dtype=float)
    tan_yaws = np.array([p[1] for p in pairs], dtype=float)

    # Recovering the angle from tan() before taking the range, so the gate is in
    # real degrees rather than in the nonlinear tangent.
    yaw_range = abs(math.atan(float(tan_yaws.max())) - math.atan(float(tan_yaws.min())))
    # Below ~12 degrees of observed rotation the predicted nose-offset
    # displacement is sub-pixel at the 640px working resolution — there is
    # nothing to measure yet. Matches `GeometryTuning.min_yaw_range_degrees`,
    # which gates the flat-vs-3D cue on the same underlying limit.
    min_measurable_range = math.radians(12.0)
    if yaw_range <= min_measurable_range:
        return NO_READING

    correlation = _pearson_correlation(offsets, tan_yaws)
    if correlation is None:
        return NO_READING

    # Magnitude gate, ahead of the scale-free correlation — see
    # MIN_NOSE_DEPTH_RATIO. Abstains rather than scoring low, because a swing
    # too small to be a nose is the same epistemic situation as too little
    # rotation to measure: we learned nothing, so we must not acquit either.
    slope = _regression_slope(offsets, tan_yaws)
    if slope is None or slope < MIN_NOSE_DEPTH_RATIO:
        return NO_READING

    level = clamp((correlation + 1.0) / 2.0, 0.0, 1.0)
    # Confidence ramps in over the next ~15 degrees past the minimum — the more
    # rotation observed, the more trustworthy the correlation is.
    confidence = clamp((yaw_range - min_measurable_range) / math.radians(15.0), 0.0, 1.0)
    return CueReading(level=level, confidence=confidence)


def blink_dynamics(window: Sequence[LivenessFrame]) -> CueReading:
    """Looks for a dip-and-recovery in eye-aspect-ratio — a blink.

    Never mandatory. Humans blink every 2-10 seconds — less often still while
    deliberately staring at a camera to unlock, a well-documented task-focus
    effect — so a short window frequently contains none at all, which is an
    abstention, not a failure. This cue only ever contributes *positively*: a
    blink is strong evidence of life, but its absence in one short window proves
    nothing.

    The 0.65 / 0.7 thresholds are loosened from an original 0.5 / 0.6.
    Real-device testing found the cue essentially never firing even when the
    user deliberately blinked, which points at a general-purpose landmark model
    not fully collapsing the eyelid contour during a real blink — a partial dip
    is the realistic signal, not the deep clean dip the original thresholds
    assumed. The recovery check looks within a small radius rather than
    requiring the immediate neighbour frame, since a ~100-150ms blink can span
    several frames at ~20fps and the minimum-EAR frame may not itself have a
    fully-open neighbour on both sides.
    """
    ears = [
        (f.left_eye_aspect_ratio + f.right_eye_aspect_ratio) / 2.0
        for f in window
        if f.left_eye_aspect_ratio is not None and f.right_eye_aspect_ratio is not None
    ]
    if len(ears) < 4:
        return NO_READING

    baseline = max(ears)
    if baseline <= 0:
        return NO_READING

    min_ear = min(ears)
    min_index = ears.index(min_ear)

    dip_ratio = min_ear / baseline
    recovery_radius = 3
    open_before = any(e / baseline > 0.7 for e in ears[:min_index][-recovery_radius:])
    open_after = any(e / baseline > 0.7 for e in ears[min_index + 1 :][:recovery_radius])
    has_neighbor_recovery = 0 < min_index < len(ears) - 1 and open_before and open_after

    if dip_ratio >= 0.65 or not has_neighbor_recovery:
        return NO_READING
    return CueReading(level=1.0, confidence=1.0)


def eye_aspect_ratio(eye_points: np.ndarray) -> Optional[float]:
    """Height/width of an eye region's bounding box.

    A cheap stand-in for the classic 6-point EAR formula. Upstream this shape is
    forced by Vision's eye outline not having the fixed 6 points that formula
    assumes; MediaPipe's fixed 16-point eye contour *would* support the real
    thing, but the bounding-box ratio is kept deliberately, because the 0.65 dip
    and 0.7 recovery thresholds in :func:`blink_dynamics` were tuned against
    this definition. Swapping in a different EAR would silently invalidate them.

    A blink collapses this toward 0; a fully open eye sits in a roughly stable
    band per person. Used only as supporting evidence — the blink cue treats a
    *dip and recovery* as the event, not this raw ratio's absolute value.
    """
    pts = np.asarray(eye_points, dtype=float).reshape(-1, 2)
    if len(pts) < 3:
        return None
    width = float(pts[:, 0].max() - pts[:, 0].min())
    if width <= 0:
        return None
    return float(pts[:, 1].max() - pts[:, 1].min()) / width
