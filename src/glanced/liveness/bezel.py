"""Device-bezel detection: direct evidence that the face is being shown *on*
something rectangular — a phone or tablet held up to the camera.

Port of `glance/Liveness/DeviceBezelDetector.swift`, and the one cue that could
not be translated line-for-line. Upstream leans on `VNDetectRectanglesRequest`,
the same Vision request document-scanner apps use to find a card or page edge.
There is no Linux equivalent, so the quadrilateral search is rebuilt here on
OpenCV contours. The *parameters* are carried over unchanged, so retuning
advice from upstream still applies to the same named knobs.

Deliberately conservative: this only ever produces *positive evidence of
spoofing* (a detected device-shaped rectangle substantially containing the
face), never positive evidence of liveness. Not finding a rectangle proves
nothing — tight framing, glare, or a dark bezel can all hide it — so the
absence of a detection must never count toward "live".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

try:  # pragma: no cover - exercised only by import environment
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]


@dataclass(frozen=True)
class DeviceBezelObservation:
    #: The largest device-plausible rectangle found this frame, as
    #: (x, y, w, h) in the same top-left-origin pixel space as the face box.
    rectangle: Optional[tuple[float, float, float, float]] = None
    #: Fraction of the recognized face's own bounding box area that falls inside
    #: `rectangle` — how much the face reads as "sitting inside a rectangular
    #: device", not just incidentally sharing the frame with some unrelated
    #: rectangular object (a laptop lid, a picture frame).
    face_overlap_fraction: Optional[float] = None


NO_BEZEL = DeviceBezelObservation()


@dataclass(frozen=True)
class BezelTuning:
    """Tuned for "a phone or tablet screen filling a meaningful fraction of
    frame, held roughly toward the camera" — not for finding every rectangle in
    the scene.

    Upstream flags these as first-pass estimates never validated against real
    footage. That caveat is stronger here, not weaker: the detector underneath
    them is a different algorithm. If this signal produces false positives
    (books, laptop lids, monitors behind the user) or false negatives (a phone
    framed too tight to show its edge), tune here.
    """

    #: Fraction of image AREA, not width or height — a phone held close enough
    #: to present a face for unlock should cover a substantial chunk of frame,
    #: well above incidental background rectangles.
    minimum_size: float = 0.15
    max_observations: int = 3
    #: Short side over long side, matching Vision's 0..1 aspect convention.
    #: Deliberately broad — 0.35 covers a tall phone in portrait, 1.0 a
    #: near-square tablet crop — rather than narrowly tuned to one device.
    minimum_aspect_ratio: float = 0.35
    maximum_aspect_ratio: float = 1.0
    #: Degrees of corner-angle tolerance from a perfect rectangle. Generous, so
    #: a hand-held phone at a slight angle to the camera (not perfectly
    #: parallel) still registers as one.
    quadrature_tolerance: float = 30.0
    #: Stands in for Vision's `minimumConfidence`. Measured as the fraction of
    #: the quad's perimeter backed by detected edge pixels — a real bezel is a
    #: continuous edge, whereas a spurious quad fitted through a textured
    #: background is not. This is an invented analogue, not a ported constant:
    #: it is the parameter most likely to need retuning against real footage.
    minimum_edge_support: float = 0.6

    canny_low: int = 50
    canny_high: int = 150


DEFAULT_BEZEL_TUNING = BezelTuning()


def _corner_angles_ok(quad: np.ndarray, tolerance_degrees: float) -> bool:
    """True when every corner of the quad is within tolerance of 90 degrees."""
    for i in range(4):
        prev_pt = quad[(i - 1) % 4]
        here = quad[i]
        next_pt = quad[(i + 1) % 4]
        v1 = prev_pt - here
        v2 = next_pt - here
        n1 = float(np.linalg.norm(v1))
        n2 = float(np.linalg.norm(v2))
        if n1 <= 1e-6 or n2 <= 1e-6:
            return False
        cosine = float(np.clip(np.dot(v1, v2) / (n1 * n2), -1.0, 1.0))
        angle = float(np.degrees(np.arccos(cosine)))
        if abs(angle - 90.0) > tolerance_degrees:
            return False
    return True


def _edge_support(quad: np.ndarray, edges: np.ndarray) -> float:
    """Fraction of the quad's perimeter that sits on a detected edge pixel."""
    height, width = edges.shape[:2]
    hits = 0
    total = 0
    for i in range(4):
        a = quad[i]
        b = quad[(i + 1) % 4]
        length = float(np.linalg.norm(b - a))
        steps = max(int(length), 1)
        ts = np.linspace(0.0, 1.0, steps)
        pts = a[None, :] + (b - a)[None, :] * ts[:, None]
        xs = np.clip(pts[:, 0].astype(int), 0, width - 1)
        ys = np.clip(pts[:, 1].astype(int), 0, height - 1)
        # A 1px dilation of the test, not of the edge map: sampling a line
        # through a 1px-wide Canny response misses constantly on a diagonal.
        window = edges[
            np.clip(ys[:, None] + np.array([-1, 0, 1])[None, :], 0, height - 1),
            xs[:, None],
        ]
        hits += int(np.count_nonzero(window.any(axis=1)))
        total += steps
    return hits / total if total else 0.0


def detect(
    image: np.ndarray,
    face_bounding_box: Sequence[float],
    tuning: BezelTuning = DEFAULT_BEZEL_TUNING,
) -> DeviceBezelObservation:
    """CPU-bound; call off the capture thread.

    `image` is the full RGB frame — not the aligned face crop, which is a
    tightly-cropped, pose-normalized warp with no room around the face to see a
    device edge in. This detector has to look *around* the face, not just at it.

    `face_bounding_box` is (x, y, w, h), top-left origin, same pixel space.
    """
    if cv2 is None or image is None or image.size == 0:
        return NO_BEZEL

    height, width = image.shape[:2]
    image_area = float(height * width)
    if image_area <= 0:
        return NO_BEZEL

    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, tuning.canny_low, tuning.canny_high)
    # Close 1px gaps in the bezel outline, which a dark phone edge against a
    # dark room produces constantly.
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    candidates: list[tuple[float, tuple[float, float, float, float]]] = []
    for contour in contours:
        perimeter = cv2.arcLength(contour, True)
        if perimeter <= 0:
            continue
        approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue

        quad = approx.reshape(4, 2).astype(float)
        area = float(cv2.contourArea(approx))
        if area / image_area < tuning.minimum_size:
            continue

        (_, _), (rect_w, rect_h), _ = cv2.minAreaRect(approx)
        long_side = max(rect_w, rect_h)
        if long_side <= 0:
            continue
        aspect = min(rect_w, rect_h) / long_side
        if not (tuning.minimum_aspect_ratio <= aspect <= tuning.maximum_aspect_ratio):
            continue

        if not _corner_angles_ok(quad, tuning.quadrature_tolerance):
            continue
        if _edge_support(quad, edges) < tuning.minimum_edge_support:
            continue

        x, y, w, h = cv2.boundingRect(approx)
        candidates.append((area, (float(x), float(y), float(w), float(h))))

    if not candidates:
        return NO_BEZEL

    # The largest candidate — the device itself, not some smaller rectangular
    # detail (an icon on its screen, a picture frame behind it) that also
    # happened to qualify. `max_observations` is applied first so the cap has
    # the same meaning it does upstream.
    candidates.sort(key=lambda c: c[0], reverse=True)
    largest = candidates[: tuning.max_observations][0][1]

    fx, fy, fw, fh = (float(v) for v in face_bounding_box)
    face_area = fw * fh
    if face_area <= 0:
        return DeviceBezelObservation(rectangle=largest, face_overlap_fraction=None)

    rx, ry, rw, rh = largest
    ix = max(0.0, min(fx + fw, rx + rw) - max(fx, rx))
    iy = max(0.0, min(fy + fh, ry + rh) - max(fy, ry))
    return DeviceBezelObservation(
        rectangle=largest, face_overlap_fraction=(ix * iy) / face_area
    )
