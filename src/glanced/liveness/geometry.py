"""Shared landmark math: the homography model a flat presentation is limited
to, a robust (IRLS) fit of it, and the small statistics helpers built on top.

Port of the geometry half of `glance/Liveness/LandmarkGeometry.swift`. The
Vision-facing accessors from that file (`imagePoints(of:)`, `region(_:of:)`,
`allPoints(from:)`) do not appear here — their Linux equivalents live in
`features.py`, since they depend on the landmarker rather than on the maths.

All of it operates in top-left / y-down pixel space.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

_SQRT2 = math.sqrt(2.0)


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
        m = self.m
        px = pts[:, 0]
        py = pts[:, 1]
        w = m[2, 0] * px + m[2, 1] * py + m[2, 2]
        degenerate = np.abs(w) <= 1e-12
        safe_w = np.where(degenerate, 1.0, w)
        inv_w = 1.0 / safe_w
        mx = (m[0, 0] * px + m[0, 1] * py + m[0, 2]) * inv_w
        my = (m[1, 0] * px + m[1, 1] * py + m[1, 2]) * inv_w
        mapped = np.empty_like(pts)
        mapped[:, 0] = np.where(degenerate, px, mx)
        mapped[:, 1] = np.where(degenerate, py, my)
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


def _solve_8x8(ata: list[list[float]], atb: list[float]) -> Optional[list[float]]:
    """Gaussian elimination with partial pivoting for the 8x8 normal equations,
    falling back to ``numpy.linalg.solve`` for near-singular pivots (< 1e-12) so
    marginal systems keep LAPACK conditioning without paying LAPACK dispatch
    overhead on the well-conditioned common path."""
    a = [row[:] for row in ata]
    b = atb[:]
    for col in range(8):
        piv = col
        best = abs(a[col][col])
        for row in range(col + 1, 8):
            v = abs(a[row][col])
            if v > best:
                best = v
                piv = row
        if best <= 1e-12 or not math.isfinite(best):
            try:
                sol = np.linalg.solve(np.array(ata, dtype=float), np.array(atb, dtype=float))
            except np.linalg.LinAlgError:
                return None
            if not np.all(np.isfinite(sol)):
                return None
            return sol.tolist()
        if piv != col:
            a[col], a[piv] = a[piv], a[col]
            b[col], b[piv] = b[piv], b[col]
        row_c = a[col]
        diag = row_c[col]
        bc = b[col]
        for row in range(col + 1, 8):
            row_r = a[row]
            factor = row_r[col] / diag
            if factor:
                for k in range(col + 1, 8):
                    row_r[k] -= factor * row_c[k]
                b[row] -= factor * bc

    x = [0.0] * 8
    for row in range(7, -1, -1):
        s = b[row]
        row_r = a[row]
        for k in range(row + 1, 8):
            s -= row_r[k] * x[k]
        val = s / row_r[row]
        if not math.isfinite(val):
            return None
        x[row] = val
    return x


def _normalize_points_list(
    pts: list[list[float]],
) -> Optional[tuple[tuple[float, float, float], list[tuple[float, float]]]]:
    n = len(pts)
    if n == 0:
        return None
    cx = sum(p[0] for p in pts) / n
    cy = sum(p[1] for p in pts) / n
    mean_dist = sum(math.hypot(p[0] - cx, p[1] - cy) for p in pts) / n
    if mean_dist <= 1e-8 or not math.isfinite(mean_dist):
        return None
    scale = _SQRT2 / mean_dist
    normed = [(scale * (p[0] - cx), scale * (p[1] - cy)) for p in pts]
    return (scale, cx, cy), normed


def _solve_normalized_dlt(
    ns: list[tuple[float, float]],
    nd: list[tuple[float, float]],
    weights: Optional[Sequence[float]],
    src_t: tuple[float, float, float],
    dst_t: tuple[float, float, float],
) -> Optional[tuple[float, float, float, float, float, float, float, float, float]]:
    """Accumulate ``A^T W A`` and ``A^T W b`` directly for the 8-DOF DLT
    equations (h33 = 1) and denormalize into 9 flat row-major entries."""
    sw = sx = sy = sxx = sxy = syy = 0.0
    sux = suy = su = svx = svy = sv = 0.0
    suxx = suxy = suyy = svxx = svxy = svyy = 0.0
    s66 = s67 = s77 = rhs6 = rhs7 = 0.0
    kept = 0

    for i in range(len(ns)):
        if weights is None:
            w = 1.0
        else:
            w = float(weights[i])
            if not (w > 0.0):
                continue
        kept += 1
        x, y = ns[i]
        u, v = nd[i]
        wx = w * x
        wy = w * y
        sw += w
        sx += wx
        sy += wy
        sxx += wx * x
        sxy += wx * y
        syy += wy * y

        wu = w * u
        wv = w * v
        wux = wu * x
        wuy = wu * y
        wvx = wv * x
        wvy = wv * y
        su += wu
        sux += wux
        suy += wuy
        sv += wv
        svx += wvx
        svy += wvy

        suxx += wux * x
        suxy += wux * y
        suyy += wuy * y
        svxx += wvx * x
        svxy += wvx * y
        svyy += wvy * y

        s66 += wux * u * x + wvx * v * x
        s67 += wux * u * y + wvx * v * y
        s77 += wuy * u * y + wvy * v * y
        rhs6 -= wux * u + wvx * v
        rhs7 -= wuy * u + wvy * v

    if kept < 4:
        return None

    ata = [
        [sxx, sxy, sx, 0.0, 0.0, 0.0, -suxx, -suxy],
        [sxy, syy, sy, 0.0, 0.0, 0.0, -suxy, -suyy],
        [sx, sy, sw, 0.0, 0.0, 0.0, -sux, -suy],
        [0.0, 0.0, 0.0, sxx, sxy, sx, -svxx, -svxy],
        [0.0, 0.0, 0.0, sxy, syy, sy, -svxy, -svyy],
        [0.0, 0.0, 0.0, sx, sy, sw, -svx, -svy],
        [-suxx, -suxy, -sux, -svxx, -svxy, -svx, s66, s67],
        [-suxy, -suyy, -suy, -svxy, -svyy, -svy, s67, s77],
    ]
    atb = [sux, suy, su, svx, svy, sv, rhs6, rhs7]
    h = _solve_8x8(ata, atb)
    if h is None:
        return None

    s1, cx1, cy1 = src_t
    s2, cx2, cy2 = dst_t
    inv_s2 = 1.0 / s2
    h0, h1, h2, h3, h4, h5, h6, h7 = h
    t00 = inv_s2 * h0 + cx2 * h6
    t01 = inv_s2 * h1 + cx2 * h7
    t02 = inv_s2 * h2 + cx2
    t10 = inv_s2 * h3 + cy2 * h6
    t11 = inv_s2 * h4 + cy2 * h7
    t12 = inv_s2 * h5 + cy2
    tx1 = -s1 * cx1
    ty1 = -s1 * cy1
    return (
        t00 * s1,
        t01 * s1,
        t00 * tx1 + t01 * ty1 + t02,
        t10 * s1,
        t11 * s1,
        t10 * tx1 + t11 * ty1 + t12,
        h6 * s1,
        h7 * s1,
        h6 * tx1 + h7 * ty1 + 1.0,
    )


def _homography_from_flat(
    m: tuple[float, float, float, float, float, float, float, float, float]
) -> Homography:
    return Homography(
        m=np.array([[m[0], m[1], m[2]], [m[3], m[4], m[5]], [m[6], m[7], m[8]]], dtype=float)
    )


def solve_homography(
    source: np.ndarray,
    destination: np.ndarray,
    weights: Optional[np.ndarray] = None,
) -> Optional[Homography]:
    """Hartley-normalized DLT homography with ``h33 = 1``, solved as an 8x8
    normal-equation system.

    Needs at least 4 correspondences; 6+ is the practical floor used by callers
    so the fit is overdetermined.

    Normal equations are accumulated directly from the normalized points and
    solved with partial-pivoting Gaussian elimination (falling back to
    ``numpy.linalg.solve`` when a pivot drops below ``1e-12``), avoiding the
    per-call temporary matrix allocations of assembling ``A`` and ``b`` as
    NumPy stacks while preserving LAPACK conditioning on marginal systems.
    """
    src = np.asarray(source, dtype=float).reshape(-1, 2)
    dst = np.asarray(destination, dtype=float).reshape(-1, 2)
    if len(src) != len(dst) or len(src) < 4:
        return None
    w_list: Optional[list[float]] = None
    if weights is not None:
        w_arr = np.asarray(weights, dtype=float)
        if len(w_arr) != len(src):
            return None
        w_list = w_arr.tolist()

    norm_src = _normalize_points_list(src.tolist())
    norm_dst = _normalize_points_list(dst.tolist())
    if norm_src is None or norm_dst is None:
        return None
    src_t, ns = norm_src
    dst_t, nd = norm_dst

    flat = _solve_normalized_dlt(ns, nd, w_list, src_t, dst_t)
    if flat is None:
        return None
    return _homography_from_flat(flat)


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
    n = len(src)
    if n != len(dst) or n < 4:
        return None

    src_list = src.tolist()
    dst_list = dst.tolist()
    norm_src = _normalize_points_list(src_list)
    norm_dst = _normalize_points_list(dst_list)
    if norm_src is None or norm_dst is None:
        return None
    src_t, ns = norm_src
    dst_t, nd = norm_dst

    current = _solve_normalized_dlt(ns, nd, None, src_t, dst_t)
    if current is None:
        return None

    mid = n // 2
    for _ in range(2):
        h0, h1, h2, h3, h4, h5, h6, h7, h8 = current
        residuals: list[float] = []
        for (px, py), (dx, dy) in zip(src_list, dst_list):
            w = h6 * px + h7 * py + h8
            if abs(w) <= 1e-12:
                mx, my = px, py
            else:
                mx = (h0 * px + h1 * py + h2) / w
                my = (h3 * px + h4 * py + h5) / w
            residuals.append(math.hypot(dx - mx, dy - my))

        sres = sorted(residuals)
        med = sres[mid] if n % 2 else 0.5 * (sres[mid - 1] + sres[mid])
        scale = max(med, 1e-4)
        inv_cutoff = 1.0 / (4.685 * 1.4826 * scale)
        weights: Optional[list[float]] = []
        survivors = 0
        for r in residuals:
            u = r * inv_cutoff
            if u >= 1.0:
                weights.append(0.0)
            else:
                t = 1.0 - u * u
                wt = t * t
                weights.append(wt)
                if wt > 0.0:
                    survivors += 1
        # Too few survivors to constrain 8 DOF: fall back to an unweighted fit
        # rather than solving an underdetermined system on the remnant.
        if survivors < 6:
            weights = None
        refined = _solve_normalized_dlt(ns, nd, weights, src_t, dst_t)
        if refined is not None:
            current = refined
    return _homography_from_flat(current)


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
