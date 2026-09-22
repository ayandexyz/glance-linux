"""One frame's worth of liveness-relevant measurements, plus the small value
types the cue functions operate on.

Port of `glance/Liveness/LivenessScoring.swift` (the `LivenessFrame` half),
`GlareCue.swift`, and the `LandmarkPoint`/`LandmarkRegion` types from
`LandmarkGeometry.swift`.

Everything here is plain data — no camera, no landmarker, no ONNX. That is the
same discipline the Swift keeps (no `import Vision` below the extractor), and
it is what lets `tests/` drive the real decision logic against synthetic data
with no hardware, exactly as `tools/liveness_selftest.swift` does upstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Sequence

import numpy as np


class LandmarkRegion(str, Enum):
    """Landmark regions read for liveness.

    Upstream this mirrors the regions Vision exposes on `VNFaceLandmarks2D`.
    MediaPipe FaceMesh has no notion of named regions, so `features.py` defines
    the index groups; the names are kept identical to the Swift so the fit/probe
    region sets and every tuning comment carry over unchanged.
    """

    LEFT_EYE = "leftEye"
    RIGHT_EYE = "rightEye"
    LEFT_EYEBROW = "leftEyebrow"
    RIGHT_EYEBROW = "rightEyebrow"
    NOSE = "nose"
    NOSE_CREST = "noseCrest"
    OUTER_LIPS = "outerLips"
    INNER_LIPS = "innerLips"
    FACE_CONTOUR = "faceContour"
    MEDIAN_LINE = "medianLine"


#: Stable iteration order, standing in for Swift's `CaseIterable` conformance.
#: Several call sites (region flattening, cue evaluation order) depend on a
#: deterministic order, so this is defined once here rather than relying on
#: dict or set ordering at each site.
ALL_REGIONS: tuple[LandmarkRegion, ...] = tuple(LandmarkRegion)


@dataclass(frozen=True)
class LandmarkPoint:
    """One landmark point, tagged with where it came from.

    `index_in_region` is what lets two frames' points be paired up for
    cross-frame comparison. With MediaPipe this correspondence is guaranteed
    (the mesh is a fixed 478-point topology, so a region is either wholly
    present or wholly absent), which is strictly better than the Vision
    behaviour the Swift has to defend against — but the pairing code in
    `planar.py` still checks per-region counts match, because a partial mesh
    from a future landmarker must not silently pair unrelated points.
    """

    x: float
    y: float
    region: LandmarkRegion
    index_in_region: int


@dataclass(frozen=True)
class GlareSample:
    """Per-frame specular-highlight measurement — the pixel-domain half of the
    gloss/glare cue. See `glare.py` for how it is produced and
    `cues.gloss_glare` for how it is scored."""

    #: Width in native pixels of the crop this was measured from. The camera
    #: layer only ever downsamples, never upsamples, so this is an honest
    #: measure of how much real detail was available; the cue
    #: confidence-weights down as it shrinks.
    crop_pixel_width: float

    #: Fraction of crop pixels that are near-saturated and low-chroma (bright,
    #: close to gray) — direct specular reflection.
    specular_fraction: float

    #: How concentrated those specular pixels are into a single region (densest
    #: 8x8 grid cell's share of all of them) rather than spread across many
    #: small points. This is what turns "some bright pixels somewhere" into "one
    #: big glare blob", and it is the half that actually distinguishes glass
    #: from a shiny forehead.
    specular_cluster_ratio: float


@dataclass(frozen=True)
class CueReading:
    """One cue's latest reading.

    `confidence == 0` means "this cue had nothing to go on this frame" (no
    native-resolution crop, not enough head rotation, too few frames) and is
    always treated as an abstention, never as a reading of zero — a cue that
    cannot see anything must not be able to convict *or* acquit.
    """

    #: 0..1 strength of this cue's own evidence, in the direction that cue
    #: argues for (spoof-ness for deny cues, liveness for confirm cues).
    level: float = 0.0
    confidence: float = 0.0

    @property
    def abstained(self) -> bool:
        return self.confidence <= 0.0


#: Explicit sentinel matching Swift's `CueReading.none`. Named `NO_READING`
#: rather than `NONE` so it never reads as Python's `None` at a call site.
NO_READING = CueReading(level=0.0, confidence=0.0)


@dataclass
class LivenessFrame:
    """One frame's liveness measurements. Everything here is already normalized
    or independently meaningful.

    Populated from a real camera frame by `features.extract()`, or built
    directly from synthetic data by the tests — this type has no idea which.
    """

    #: Monotonic seconds. The window is pruned by elapsed time, so this must
    #: come from `time.monotonic()`, never a wall clock that can step.
    timestamp: float

    #: Every landmark point found this frame, tagged by region.
    landmarks: Sequence[LandmarkPoint] = field(default_factory=tuple)

    #: Distance between the two eye centers, in the same pixel space as
    #: `landmarks` — the normalization scale for every ratio below, so scores
    #: stay comparable regardless of how close the face is to the camera.
    interocular_distance: Optional[float] = None

    #: Head yaw in radians.
    yaw: Optional[float] = None

    left_eye_aspect_ratio: Optional[float] = None
    right_eye_aspect_ratio: Optional[float] = None

    #: `(nose_centroid.x - eye_midpoint.x) / interocular_distance` — tracks
    #: `tan(yaw)` on a real 3D face (the nose sits off the eye plane) and stays
    #: constant on any flat presentation, however it is rotated. See
    #: `scoring.pose_depth_consistency`.
    nose_offset_ratio: Optional[float] = None

    #: Whether this frame's landmarks are trustworthy enough for the precision
    #: cues. Degraded landmarks are exactly when residual noise spikes, so cues
    #: that need precision skip frames where this is false.
    has_reliable_landmarks: bool = False

    #: Fraction of this frame's face bounding box covered by a detected
    #: device-shaped rectangle — see `bezel.py`. `None` when detection was not
    #: run or found nothing; real evidence only when non-None and large.
    device_overlap_fraction: Optional[float] = None

    #: `None` when no native-resolution crop was available (e.g. synthetic test
    #: data), in which case the gloss/glare cue abstains rather than guessing.
    glare: Optional[GlareSample] = None

    def points_of(self, region: LandmarkRegion) -> np.ndarray:
        """This frame's points for one region as an (N, 2) float array, in
        stable `index_in_region` order. Empty (0, 2) if the region is absent."""
        pts = sorted(
            (p for p in self.landmarks if p.region is region),
            key=lambda p: p.index_in_region,
        )
        if not pts:
            return np.empty((0, 2), dtype=float)
        return np.array([(p.x, p.y) for p in pts], dtype=float)
