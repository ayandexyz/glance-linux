"""The pixel-facing half of the gloss/glare cue: turns one native-resolution
face crop into a :class:`GlareSample`.

Port of `glance/Liveness/GlareCueExtractor.swift`. One vectorized pass over the
crop, no frequency-domain work — cheap enough to run on every liveness frame,
same budget class as the bezel detector.

Upstream this started life as a much wider `SpoofCueSample` carrying seven
appearance measurements (spectral high-frequency ratio, Laplacian sharpness
spread, chroma statistics, a moire peak ratio, per-row luma profiles).
Real-device testing found only the specular pair actually separated a live face
from a phone screen — the rest read identically, backwards, or pinned at a
constant — so they were removed rather than left as dead weight.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from .frame import GlareSample

#: Near-white, near-gray pixel — the signature of a direct specular highlight (a
#: light source reflecting straight off glass or skin) rather than a bright but
#: still-colored surface.
SPECULAR_LUMA_FLOOR = 235.0
SPECULAR_CHROMA_TOLERANCE = 10.0

#: Grid resolution for the clustering measure. Coarse on purpose: the question
#: is "is the glare one blob or many scattered points", which a fine grid would
#: answer no better and a 2x2 would answer for free no matter what.
CLUSTER_GRID_SIZE = 8


def extract(face_crop: np.ndarray) -> Optional[GlareSample]:
    """Measure specular highlights in an RGB (H, W, 3) uint8 crop.

    Returns None only if the crop is empty. A crop too small to be informative
    still yields a sample — `cues.gloss_glare` discounts it via
    `crop_pixel_width`, rather than this silently dropping the frame.
    """
    if face_crop is None or face_crop.size == 0:
        return None
    if face_crop.ndim != 3 or face_crop.shape[2] < 3:
        return None
    height, width = face_crop.shape[:2]
    if height <= 0 or width <= 0:
        return None

    rgb = face_crop[:, :, :3].astype(np.float32)
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]

    luma = 0.299 * r + 0.587 * g + 0.114 * b
    cb = -0.168736 * r - 0.331264 * g + 0.5 * b + 128.0
    cr = 0.5 * r - 0.418688 * g - 0.081312 * b + 128.0

    specular = (
        (luma >= SPECULAR_LUMA_FLOOR)
        & (np.abs(cb - 128.0) <= SPECULAR_CHROMA_TOLERANCE)
        & (np.abs(cr - 128.0) <= SPECULAR_CHROMA_TOLERANCE)
    )

    specular_total = int(specular.sum())
    specular_fraction = specular_total / float(height * width)

    if specular_total > 0:
        ys, xs = np.nonzero(specular)
        # Integer-truncating bucket assignment, matching the Swift's
        # `y * gridSize / height` — the last row/column clamps into the final
        # cell rather than overflowing it.
        gy = np.minimum(CLUSTER_GRID_SIZE - 1, ys * CLUSTER_GRID_SIZE // height)
        gx = np.minimum(CLUSTER_GRID_SIZE - 1, xs * CLUSTER_GRID_SIZE // width)
        counts = np.bincount(
            gy * CLUSTER_GRID_SIZE + gx, minlength=CLUSTER_GRID_SIZE * CLUSTER_GRID_SIZE
        )
        cluster_ratio = float(counts.max()) / float(specular_total)
    else:
        cluster_ratio = 0.0

    return GlareSample(
        crop_pixel_width=float(width),
        specular_fraction=float(specular_fraction),
        specular_cluster_ratio=cluster_ratio,
    )
