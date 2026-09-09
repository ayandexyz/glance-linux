"""Five-point similarity alignment to the embedder's 112x112 input.

The five points and the destination template are ArcFace's standard: the same
canonical constellation InsightFace trains against, so an embedding produced
here is comparable with one produced by any other ArcFace pipeline.
"""

from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from .liveness.geometry import solve_similarity_transform

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]

#: ArcFace's canonical five-point template for a 112x112 crop:
#: left eye, right eye, nose tip, left mouth corner, right mouth corner.
ARCFACE_TEMPLATE = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=float,
)

OUTPUT_SIZE = 112


def align(image: np.ndarray, five_points: Sequence[Sequence[float]]) -> Optional[np.ndarray]:
    """Warp `image` so the five landmarks land on the ArcFace template.

    A similarity transform (rotation, uniform scale, translation) rather than a
    full affine or homography, deliberately: anything more general would let the
    warp squash a face toward the template and erase exactly the shape
    differences the embedding is supposed to encode.
    """
    if cv2 is None:
        raise RuntimeError("opencv-python is required for alignment")
    points = np.asarray(five_points, dtype=float).reshape(-1, 2)
    if len(points) != 5:
        return None
    matrix = solve_similarity_transform(points, ARCFACE_TEMPLATE)
    if matrix is None:
        return None
    return cv2.warpAffine(
        image, matrix, (OUTPUT_SIZE, OUTPUT_SIZE), flags=cv2.INTER_LINEAR, borderValue=0
    )
