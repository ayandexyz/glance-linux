"""Planar-vs-3D liveness — the `flat_vs_3d` confirm cue.

Does landmark motion across the window look like a flat photograph (fully
explained by a homography) or like a real head with depth (held-out nose points
systematically miss the plane fit)?

Port of `glance/Liveness/GeometryLiveness.swift`. Higher `planar_residual_score`
means "more like a live face".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

from .frame import ALL_REGIONS, CueReading, LandmarkRegion, LivenessFrame
from .geometry import clamp, median_value, solve_robust_homography


@dataclass(frozen=True)
class GeometryTuning:
    #: Excess (`probe_residual / fit_residual`) at which the planar signal
    #: starts ramping off 0. ~1 means the held-out points are no noisier than
    #: the fit set — a plane.
    excess_floor: float = 1.15
    #: Excess at which the planar signal saturates at 1.
    excess_ceiling: float = 2.0
    #: `|mean residual| / mean(|residual|)` below this is treated as
    #: unstructured landmark noise rather than 3D parallax.
    coherence_floor: float = 0.35
    #: Minimum mean fit-set displacement, in units of interocular distance,
    #: before the geometry signal is willing to vote. Below this a real still
    #: face has no parallax either, so we abstain.
    motion_gate: float = 0.008
    #: Minimum yaw range (degrees) across the window before geometry votes at
    #: all. Nose parallax at yaw `t` is roughly `0.18 * IOD * sin(t)`; at the
    #: 640px working resolution (IOD ~ 60px for a typical face) that is under a
    #: pixel — below the landmarker's own jitter — for any yaw under this gate.
    #: `motion_gate` alone does not catch it: pure head translation with no
    #: rotation can clear that gate while producing zero real parallax, which is
    #: exactly the "smooth phone wobble" failure mode this closes.
    min_yaw_range_degrees: float = 12.0


DEFAULT_GEOMETRY_TUNING = GeometryTuning()


#: Regions that sit on roughly one shallow surface, with enough spatial spread
#: to constrain an 8-DOF homography. The nose is held out.
FIT_REGIONS: frozenset[LandmarkRegion] = frozenset(
    {
        LandmarkRegion.LEFT_EYE,
        LandmarkRegion.RIGHT_EYE,
        LandmarkRegion.LEFT_EYEBROW,
        LandmarkRegion.RIGHT_EYEBROW,
        LandmarkRegion.OUTER_LIPS,
    }
)

#: Protruding landmarks geometrically *inside* the fit hull, so leftover error
#: is depth rather than extrapolation. `FACE_CONTOUR` is excluded on purpose.
PROBE_REGIONS: frozenset[LandmarkRegion] = frozenset(
    {
        LandmarkRegion.NOSE,
        LandmarkRegion.NOSE_CREST,
        LandmarkRegion.MEDIAN_LINE,
    }
)


@dataclass(frozen=True)
class GeometryLivenessResult:
    planar_residual_score: float = 0.0
    planar_confidence: float = 0.0

    valid_landmark_count: int = 0
    pairs_analyzed: int = 0
    rejected_pair_count: int = 0
    median_fit_residual: Optional[float] = None
    median_probe_residual: Optional[float] = None
    excess_ratio: Optional[float] = None
    coherence: Optional[float] = None
    motion_magnitude: Optional[float] = None
    #: Normalized distance ratios for diagnostics only — these do not vote.
    diagnostic_ratios: dict[str, float] = field(default_factory=dict)

    @property
    def planar_reading(self) -> CueReading:
        """Adapter into the shared cue vocabulary — see `cues.readings`."""
        return CueReading(level=self.planar_residual_score, confidence=self.planar_confidence)


EMPTY_GEOMETRY_RESULT = GeometryLivenessResult()


def _yaw_range_degrees(window: Sequence[LivenessFrame]) -> Optional[float]:
    """Yaw range in degrees across every frame that reports one; None if too few
    frames have a yaw estimate to say anything."""
    yaws = [math.degrees(f.yaw) for f in window if f.yaw is not None]
    if len(yaws) < 3:
        return None
    return max(yaws) - min(yaws)


@dataclass(frozen=True)
class _PairSample:
    fit_residual: float
    probe_residual: float
    excess: float
    coherence: float
    motion: float


def _corresponding_points(
    a: LivenessFrame, b: LivenessFrame
) -> dict[LandmarkRegion, tuple[np.ndarray, np.ndarray]]:
    """Points present, with matching per-region counts, in both frames.

    This is the guard cross-frame comparison requires: a region can be entirely
    absent on either frame, and comparing mismatched arrays would silently pair
    up unrelated points.
    """
    result: dict[LandmarkRegion, tuple[np.ndarray, np.ndarray]] = {}
    for region in ALL_REGIONS:
        pa = a.points_of(region)
        pb = b.points_of(region)
        if len(pa) == 0 or len(pa) != len(pb):
            continue
        result[region] = (pa, pb)
    return result


def _flatten(
    by_region: dict[LandmarkRegion, tuple[np.ndarray, np.ndarray]],
    regions: frozenset[LandmarkRegion] | set[LandmarkRegion],
) -> tuple[np.ndarray, np.ndarray]:
    src_parts, dst_parts = [], []
    for region in ALL_REGIONS:
        if region not in regions or region not in by_region:
            continue
        s, d = by_region[region]
        src_parts.append(s)
        dst_parts.append(d)
    if not src_parts:
        return np.empty((0, 2)), np.empty((0, 2))
    return np.vstack(src_parts), np.vstack(dst_parts)


def _pair_geometry(a: LivenessFrame, b: LivenessFrame) -> Optional[_PairSample]:
    if not (a.has_reliable_landmarks and b.has_reliable_landmarks):
        return None
    iod = b.interocular_distance
    if iod is None or iod <= 0:
        return None

    matched = _corresponding_points(a, b)
    fit_src, fit_dst = _flatten(matched, FIT_REGIONS)
    probe_src, probe_dst = _flatten(matched, PROBE_REGIONS)
    eye_src, eye_dst = _flatten(
        matched, {LandmarkRegion.LEFT_EYE, LandmarkRegion.RIGHT_EYE}
    )
    if len(fit_src) < 6 or len(probe_src) < 2:
        return None

    homography = solve_robust_homography(fit_src, fit_dst)
    if homography is None:
        return None

    probe_vectors = (probe_dst - homography.apply(probe_src)) / iod
    probe_magnitudes = np.hypot(probe_vectors[:, 0], probe_vectors[:, 1])

    # Eyes are the rigid anchors; mouth/brow expression in the fit set must not
    # set the noise scale, or a talking face looks planar and a still photo's
    # leftover expression-scale noise looks 3D.
    if len(eye_src) >= 2:
        noise_magnitudes = np.hypot(*((eye_dst - homography.apply(eye_src)) / iod).T)
    else:
        noise_magnitudes = np.hypot(*((fit_dst - homography.apply(fit_src)) / iod).T)

    fit_residual = median_value(noise_magnitudes)
    probe_residual = median_value(probe_magnitudes)
    noise_floor = 0.002
    excess = probe_residual / max(fit_residual, noise_floor)

    mean_vector = probe_vectors.mean(axis=0)
    mean_magnitude = float(np.mean(probe_magnitudes))
    coherence = (
        float(np.hypot(mean_vector[0], mean_vector[1]) / mean_magnitude)
        if mean_magnitude > 1e-8
        else 0.0
    )

    motion = float(np.sum(np.hypot(*(fit_dst - fit_src).T)) / (len(fit_src) * iod))

    return _PairSample(
        fit_residual=fit_residual,
        probe_residual=probe_residual,
        excess=excess,
        coherence=coherence,
        motion=motion,
    )


def _pair_indices(count: int) -> list[tuple[int, int, float]]:
    """Frame pairs to compare, with weights.

    Adjacent pairs (weight 1) see the smallest, noisiest displacements;
    half-window (weight 2) and full-window (weight 3) pairs see the largest real
    parallax, so they are weighted to dominate the median.
    """
    if count < 2:
        return []
    pairs: list[tuple[int, int, float]] = [(i, i + 1, 1.0) for i in range(count - 1)]
    half = max(count // 2, 2)
    if count > 4:
        pairs += [(i, i + half, 2.0) for i in range(count - half)]
    if count > 2:
        pairs.append((0, count - 1, 3.0))
    return pairs


def _weighted_median(values: list[float], weights: list[float]) -> Optional[float]:
    if not values or len(values) != len(weights):
        return None
    ordered = sorted(zip(values, weights), key=lambda vw: vw[0])
    total = sum(w for _, w in ordered)
    if total <= 0:
        return median_value(values)
    acc = 0.0
    for value, weight in ordered:
        acc += weight
        if acc >= total / 2:
            return value
    return ordered[-1][0]


def _centroid(points: np.ndarray) -> Optional[np.ndarray]:
    if len(points) == 0:
        return None
    return points.mean(axis=0)


def _diagnostic_ratios(frame: Optional[LivenessFrame]) -> dict[str, float]:
    """Display-only ratios. Never vote — they exist so a debug console can show
    what the geometry cue was looking at."""
    if frame is None or not frame.landmarks:
        return {}
    ratios: dict[str, float] = {}
    iod = frame.interocular_distance
    if iod is not None and iod > 0:
        ratios["interocular"] = iod

    xs = np.array([p.x for p in frame.landmarks])
    ys = np.array([p.y for p in frame.landmarks])
    face_width = float(xs.max() - xs.min())
    face_height = float(ys.max() - ys.min())
    if face_width > 0 and iod is not None and iod > 0:
        ratios["eye / faceW"] = iod / face_width

    left = _centroid(frame.points_of(LandmarkRegion.LEFT_EYE))
    right = _centroid(frame.points_of(LandmarkRegion.RIGHT_EYE))
    nose = _centroid(frame.points_of(LandmarkRegion.NOSE))
    lips = frame.points_of(LandmarkRegion.OUTER_LIPS)

    if left is not None and right is not None and nose is not None and face_height > 0:
        eye_mid = (left + right) / 2
        ratios["nose-eye / faceH"] = float(np.hypot(*(nose - eye_mid))) / face_height
        if len(lips) > 0:
            mouth = lips.mean(axis=0)
            nose_mouth = float(np.hypot(*(mouth - nose)))
            ratios["nose-mouth / faceH"] = nose_mouth / face_height
            if face_width > 0:
                ratios["mouthW / faceW"] = float(lips[:, 0].max() - lips[:, 0].min()) / face_width
            if nose_mouth > 0 and iod is not None:
                ratios["eye / nose-mouth"] = iod / nose_mouth
    return ratios


def evaluate(
    window: Sequence[LivenessFrame], tuning: GeometryTuning = DEFAULT_GEOMETRY_TUNING
) -> GeometryLivenessResult:
    """Fold a rolling window into the flat-vs-3D reading."""
    last = window[-1] if window else None
    last_landmark_count = len(last.landmarks) if last is not None else 0
    diagnostics = _diagnostic_ratios(last)

    if len(window) < 3:
        return GeometryLivenessResult(
            valid_landmark_count=last_landmark_count, diagnostic_ratios=diagnostics
        )

    yaw_range = _yaw_range_degrees(window)
    yaw_gate_ok = (yaw_range or 0.0) >= tuning.min_yaw_range_degrees
    if yaw_range is not None:
        diagnostics["yaw range (deg)"] = yaw_range

    excesses: list[float] = []
    coherences: list[float] = []
    motions: list[float] = []
    fit_residuals: list[float] = []
    probe_residuals: list[float] = []
    weights: list[float] = []
    rejected = 0
    skipped_for_motion = 0

    for i, j, weight in _pair_indices(len(window)):
        sample = _pair_geometry(window[i], window[j])
        if sample is None:
            rejected += 1
            continue
        if sample.motion < tuning.motion_gate:
            skipped_for_motion += 1
            continue
        excesses.append(sample.excess)
        coherences.append(sample.coherence)
        motions.append(sample.motion)
        fit_residuals.append(sample.fit_residual)
        probe_residuals.append(sample.probe_residual)
        weights.append(weight)

    pairs_analyzed = len(excesses)
    median_fit = median_value(fit_residuals) if fit_residuals else None
    median_probe = median_value(probe_residuals) if probe_residuals else None
    excess = _weighted_median(excesses, weights)
    coherence = _weighted_median(coherences, weights)
    motion = float(np.mean(motions)) if motions else None

    # Abstention is (level 0, confidence 0): the evaluator only counts a frame
    # when confidence > 0, so the level is never read in that case — but 0
    # rather than 0.5 keeps a diagnostics bar honest.
    planar_score = 0.0
    planar_confidence = 0.0

    if yaw_gate_ok and excess is not None and coherence is not None and pairs_analyzed >= 2:
        # Unstructured leftover (landmark jitter) is not 3D evidence.
        coherence_factor = clamp(
            (coherence - tuning.coherence_floor) / max(1.0 - tuning.coherence_floor, 0.01), 0.0, 1.0
        )
        excess_score = clamp(
            (excess - tuning.excess_floor) / max(tuning.excess_ceiling - tuning.excess_floor, 0.01),
            0.0,
            1.0,
        )
        planar_score = excess_score * (0.35 + 0.65 * coherence_factor)
        planar_confidence = clamp(pairs_analyzed / 6.0, 0.0, 1.0) * (0.5 + 0.5 * coherence_factor)
    # Both remaining branches abstain, and for the same reason: there was not
    # enough real head movement this window to tell "flat" from "3D" apart.
    # Explicit here (rather than falling through) because "we could not look"
    # must never be recorded as "we looked and saw a photo".

    return GeometryLivenessResult(
        planar_residual_score=planar_score,
        planar_confidence=planar_confidence,
        valid_landmark_count=last_landmark_count,
        pairs_analyzed=pairs_analyzed,
        rejected_pair_count=rejected,
        median_fit_residual=median_fit,
        median_probe_residual=median_probe,
        excess_ratio=excess,
        coherence=coherence,
        motion_magnitude=motion,
        diagnostic_ratios=diagnostics,
    )
