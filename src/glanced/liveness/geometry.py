"""Shared landmark math: the homography model a flat presentation is limited
to, a robust (IRLS) fit of it, and the small statistics helpers built on top.

Port of the geometry half of `glance/Liveness/LandmarkGeometry.swift`. The
Vision-facing accessors from that file (`imagePoints(of:)`, `region(_:of:)`,
`allPoints(from:)`) do not appear here — their Linux equivalents live in
`features.py`, since they depend on the landmarker rather than on the maths.

All of it operates in top-left / y-down pixel space.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np


@dataclass(frozen=True)
class Homography:
    """3x3 homography mapping ``p -> ((h11 x + h12 y + h13) / w,
    (h21 x + h22 y + h23) / w)`` with ``w = h31 x + h32 y + h33``.

    This is exactly the model a flat photograph (or a phone screen) is limited
    to, including perspective tilt — strictly more general than a similarity
    transform, which is what makes "the residual left over after the best
    homography fit" a meaningful measure of real depth.
    """

    m: np.ndarray  # (3, 3)

    def apply(self, points: np.ndarray) -> np.ndarray:
        """Apply to an (N, 2) array, returning (N, 2).

        Points whose homogeneous divisor collapses are passed through
        unchanged, matching the Swift's ``guard abs(w) > 1e-12 else { return
        point }`` — a degenerate mapping must not manufacture a huge residual
        that would read as spectacular 3D evidence.
        """
        pts = np.asarray(points, dtype=float).reshape(-1, 2)
        if pts.size == 0:
            return pts
        homo = np.hstack([pts, np.ones((len(pts), 1))])
        out = homo @ self.m.T
        w = out[:, 2]
        degenerate = np.abs(w) <= 1e-12
        safe_w = np.where(degenerate, 1.0, w)
        mapped = out[:, :2] / safe_w[:, None]
        mapped[degenerate] = pts[degenerate]
        return mapped


def median_value(values: Sequence[float] | np.ndarray) -> float:
    """Median, returning 0 for an empty input to match the Swift's
    ``guard !values.isEmpty else { return 0 }``."""
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return 0.0
    return float(np.median(arr))


def _normalizing_transform(points: np.ndarray) -> Optional[tuple[float, float, float]]:
    """Hartley normalization: translate to centroid, scale so mean distance
    from the origin is sqrt(2). Returns ``(scale, cx, cy)``."""
    if len(points) == 0:
        return None
    cx, cy = float(np.mean(points[:, 0])), float(np.mean(points[:, 1]))
    mean_dist = float(np.mean(np.hypot(points[:, 0] - cx, points[:, 1] - cy)))
    if mean_dist <= 1e-8:
        return None
    return (float(np.sqrt(2.0)) / mean_dist, cx, cy)


def _apply_normalization(points: np.ndarray, t: tuple[float, float, float]) -> np.ndarray:
    scale, cx, cy = t
    return np.column_stack([scale * (points[:, 0] - cx), scale * (points[:, 1] - cy)])


def solve_homography(
    source: np.ndarray,
    destination: np.ndarray,
    weights: Optional[np.ndarray] = None,
) -> Optional[Homography]:
    """Hartley-normalized DLT homography with ``h33 = 1``, solved as an 8x8
    normal-equation system.

    Needs at least 4 correspondences; 6+ is the practical floor used by callers
    so the fit is overdetermined.

    Deviation from the Swift, which hand-rolls Gaussian elimination with
    partial pivoting and returns nil when the largest pivot falls under 1e-12:
    the 8x8 solve here goes through ``numpy.linalg.solve``. The failure
    condition is preserved rather than dropped — a singular system raises and
    is caught, and a non-finite solution is rejected — but numpy's LAPACK path
    is better conditioned than the direct elimination, so marginal systems that
    the Swift declines may solve here.
    """
    src = np.asarray(source, dtype=float).reshape(-1, 2)
    dst = np.asarray(destination, dtype=float).reshape(-1, 2)
    if len(src) != len(dst) or len(src) < 4:
        return None
    if weights is not None:
        weights = np.asarray(weights, dtype=float)
        if len(weights) != len(src):
            return None

    src_t = _normalizing_transform(src)
    dst_t = _normalizing_transform(dst)
    if src_t is None or dst_t is None:
        return None

    ns = _apply_normalization(src, src_t)
    nd = _apply_normalization(dst, dst_t)

    # sqrt of the weight, so that accumulating A^T A applies the weight itself.
    w = np.ones(len(ns)) if weights is None else np.sqrt(np.clip(weights, 0.0, None))
    keep = w > 0
    if keep.sum() < 4:
        return None
    ns, nd, w = ns[keep], nd[keep], w[keep]

    x, y = ns[:, 0], ns[:, 1]
    u, v = nd[:, 0], nd[:, 1]
    zero = np.zeros_like(x)
    one = np.ones_like(x)

    # Two DLT rows per correspondence, h33 fixed at 1:
    #   [x y 1 0 0 0 -u x -u y] . h = u
    #   [0 0 0 x y 1 -v x -v y] . h = v
    row0 = np.column_stack([x, y, one, zero, zero, zero, -u * x, -u * y]) * w[:, None]
    row1 = np.column_stack([zero, zero, zero, x, y, one, -v * x, -v * y]) * w[:, None]
    a = np.vstack([row0, row1])
    b = np.concatenate([u * w, v * w])

    ata = a.T @ a
    atb = a.T @ b
    try:
        h = np.linalg.solve(ata, atb)
    except np.linalg.LinAlgError:
        return None
    if not np.all(np.isfinite(h)):
        return None

    h_norm = np.array(
        [[h[0], h[1], h[2]], [h[3], h[4], h[5]], [h[6], h[7], 1.0]], dtype=float
    )
    return _denormalize(h_norm, src_t, dst_t)


def _denormalize(
    h: np.ndarray, source: tuple[float, float, float], destination: tuple[float, float, float]
) -> Homography:
    """``H = Tdst^-1 . Hn . Tsrc``."""
    s1, cx1, cy1 = source
    s2, cx2, cy2 = destination
    t_src = np.array([[s1, 0.0, -s1 * cx1], [0.0, s1, -s1 * cy1], [0.0, 0.0, 1.0]])
    t_dst_inv = np.array([[1.0 / s2, 0.0, cx2], [0.0, 1.0 / s2, cy2], [0.0, 0.0, 1.0]])
    return Homography(m=t_dst_inv @ h @ t_src)


def solve_robust_homography(
    source: np.ndarray, destination: np.ndarray
) -> Optional[Homography]:
    """Two-pass IRLS around :func:`solve_homography`, Tukey biweight.

    A single wildly jittered landmark cannot pull the plane around. The cutoff
    uses the full-set median so a smiling mouth (large but not wild) stays in
    the fit and keeps the nose inside the hull — dropping it lets an
    underconstrained upper-face homography absorb the very parallax the
    flat-vs-3D cue exists to measure.
    """
    src = np.asarray(source, dtype=float).reshape(-1, 2)
    dst = np.asarray(destination, dtype=float).reshape(-1, 2)
    current = solve_homography(src, dst)
    if current is None:
        return None

    for _ in range(2):
        residuals = np.hypot(*(dst - current.apply(src)).T)
        scale = max(median_value(residuals), 1e-4)
        cutoff = 4.685 * 1.4826 * scale
        u = residuals / cutoff
        t = 1.0 - u * u
        weights = np.where(u >= 1.0, 0.0, t * t)
        # Too few survivors to constrain 8 DOF: fall back to an unweighted fit
        # rather than solving an underdetermined system on the remnant.
        if int(np.count_nonzero(weights > 0)) < 6:
            weights = np.ones(len(src))
        refined = solve_homography(src, dst, weights)
        if refined is not None:
            current = refined
    return current


def solve_similarity_transform(
    source: np.ndarray, destination: np.ndarray
) -> Optional[np.ndarray]:
    """Closed-form least-squares similarity transform (rotation + uniform scale
    + translation) as a 2x3 matrix, via the standard 2D Procrustes solution in
    complex arithmetic: with each mean-centred point as ``p = x + iy``, the
    optimal ``z = scale * e^(i theta)`` is ``sum(q conj(p)) / sum(|p|^2)``.

    Used by the alignment path (five-point warp to the embedder's 112x112
    input), not by the liveness cues — kept alongside the homography because
    the two are the same family of question and upstream keeps them together.
    """
    src = np.asarray(source, dtype=float).reshape(-1, 2)
    dst = np.asarray(destination, dtype=float).reshape(-1, 2)
    if len(src) != len(dst) or len(src) < 2:
        return None

    src_mean = src.mean(axis=0)
    dst_mean = dst.mean(axis=0)
    p = src - src_mean
    q = dst - dst_mean

    numerator_real = float(np.sum(q[:, 0] * p[:, 0] + q[:, 1] * p[:, 1]))
    numerator_imag = float(np.sum(q[:, 1] * p[:, 0] - q[:, 0] * p[:, 1]))
    denominator = float(np.sum(p[:, 0] ** 2 + p[:, 1] ** 2))
    if denominator <= 0:
        return None

    sc = numerator_real / denominator
    ss = numerator_imag / denominator
    a, b, c, d = sc, ss, -ss, sc
    tx = dst_mean[0] - (a * src_mean[0] + c * src_mean[1])
    ty = dst_mean[1] - (b * src_mean[0] + d * src_mean[1])
    return np.array([[a, c, tx], [b, d, ty]], dtype=float)


def clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)


def ramp(value: float, floor: float, ceiling: float) -> float:
    """Linear 0..1 ramp between ``floor`` and ``ceiling``, clamped at both ends."""
    return clamp((value - floor) / max(ceiling - floor, 0.0001), 0.0, 1.0)
