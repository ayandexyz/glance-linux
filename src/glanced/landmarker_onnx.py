"""Face landmarker on onnxruntime: MediaPipe's FaceLandmarker, without MediaPipe.

The `onnx` backend of `landmarker.create_landmarker`. It returns the same
`DetectedFace` (a plain (N, 2) pixel-space mesh, yaw and pitch) as the
MediaPipe backend, so nothing downstream knows which one ran.

Why: MediaPipe publishes no Linux aarch64 wheel, so Glance could not run on an
ARM laptop at all (Apple Silicon under Asahi, ARM Chromebooks, Snapdragon). The networks are the *same* ones MediaPipe's
``face_landmarker.task`` (float16/1) carries, BlazeFace short-range and Face
Mesh V2 with iris, converted unchanged to ONNX (see ``models.py``). What
MediaPipe's C++ graph did around them is re-implemented here, rule for rule,
because the liveness tuning was measured against its output:

* detection -> ROI: detector keypoints 0 -> 1 set the roll, box scaled 1.5x,
  square on the long side (FaceLandmarkerGraph);
* tracking (VIDEO mode): the next frame's ROI comes from this frame's
  landmarks (axis-aligned box, roll from points 33 -> 263, 1.5x, square), the
  detector only runs when the face is lost (SingleFaceLandmarksDetectorGraph);
* landmark smoothing: One-Euro filter, min_cutoff 0.05, beta 80,
  derivate_cutoff 1.0, scaled by face size (LandmarksSmoothingCalculator as
  FaceLandmarker configures it). Without it jitter is ~2.6x higher, which moves
  the planar and blink cues off their tuning;
* facial transformation matrix: MediaPipe's geometry pipeline (perspective
  camera, fov_y 63 deg, near 1; two-pass scale estimate; weighted orthogonal
  Procrustes against the canonical face) using the network's per-landmark z.
  Yaw and pitch are read from it with the same functions as before. Do *not*
  replace this with a 2D solvePnP: yaw drives two of the three confirm cues,
  and deriving it from the same 2D nose/eye points those cues correlate it
  against weakens the check.

Measured against MediaPipe 0.10.35 on the same frames (see tests/): mean
landmark difference < 1% of the interocular distance, yaw within ~0.7 deg,
still-face jitter 0.152 px vs 0.150 px, identical liveness decisions on
synthetic sequences (a parity check of this port, not of spoof resistance).
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from . import models, paths
from .landmarker import DetectedFace
from .liveness.features import pitch_from_transformation_matrix, yaw_from_transformation_matrix

GEOMETRY_METADATA = Path(__file__).resolve().parent / "_data" / "face_geometry" / "geometry_pipeline_metadata_landmarks.binarypb"

#: MediaPipe defaults for FaceLandmarker.
MIN_DETECTION_SCORE = 0.5
NMS_IOU = 0.3
MIN_PRESENCE = 0.5
ROI_SCALE = 1.5
MESH_INPUT = 256
DETECTOR_INPUT = 128


# --------------------------------------------------------------------------- utils


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(np.asarray(x, dtype=np.float64), -100.0, 100.0)))


def _normalize_radians(angle: float) -> float:
    return angle - 2 * math.pi * math.floor((angle + math.pi) / (2 * math.pi))


def _rotation(p0, p1) -> float:
    """DetectionsToRectsCalculator: target 0 - atan2(-(y1 - y0), x1 - x0)."""
    return _normalize_radians(-math.atan2(-(float(p1[1]) - float(p0[1])), float(p1[0]) - float(p0[0])))


@dataclass
class Roi:
    cx: float
    cy: float
    size: float  # side of the square, pixels
    rotation: float  # radians


def _roi_from_box(x0, y0, x1, y1, rotation) -> Roi:
    # RectTransformationCalculator: scale 1.5, square_long.
    return Roi((x0 + x1) / 2.0, (y0 + y1) / 2.0, max(x1 - x0, y1 - y0) * ROI_SCALE, rotation)


def roi_from_landmarks(points: np.ndarray) -> Roi:
    x0, y0 = points[:, :2].min(axis=0)
    x1, y1 = points[:, :2].max(axis=0)
    return _roi_from_box(x0, y0, x1, y1, _rotation(points[33], points[263]))


def _roi_affine(roi: Roi, out: int):
    """2x3 matrix taking image pixels to crop pixels."""
    import cv2

    c, s = math.cos(roi.rotation), math.sin(roi.rotation)
    half = roi.size / 2.0

    def to_image(u, v):
        return (roi.cx + u * c - v * s, roi.cy + u * s + v * c)

    src = np.float32([to_image(-half, -half), to_image(half, -half), to_image(-half, half)])
    dst = np.float32([[0, 0], [out, 0], [0, out]])
    return cv2.getAffineTransform(src, dst)


# ------------------------------------------------------------------------ detector


def _ssd_anchors() -> np.ndarray:
    """face_detection_short_range: 4 layers, strides 8/16/16/16, 128x128,
    two anchors per layer and location, fixed anchor size -> 896 anchors."""
    strides = [8, 16, 16, 16]
    anchors = []
    layer = 0
    while layer < len(strides):
        count, last = 0, layer
        while last < len(strides) and strides[last] == strides[layer]:
            count += 2
            last += 1
        size = math.ceil(DETECTOR_INPUT / strides[layer])
        for y in range(size):
            for x in range(size):
                anchors.extend([((x + 0.5) / size, (y + 0.5) / size)] * count)
        layer = last
    return np.array(anchors, dtype=np.float32)


@dataclass
class _Detection:
    box: np.ndarray  # x0, y0, x1, y1 pixels
    keypoints: np.ndarray  # (6, 2) pixels
    score: float


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return float(inter / union) if union > 0 else 0.0


class _BlazeFace:
    def __init__(self, session) -> None:
        self.session = session
        self.input_name = session.get_inputs()[0].name
        self.anchors = _ssd_anchors()

    def detect(self, rgb: np.ndarray) -> list[_Detection]:
        import cv2

        h, w = rgb.shape[:2]
        side = max(h, w)
        pad_y, pad_x = (side - h) // 2, (side - w) // 2
        canvas = np.zeros((side, side, 3), np.uint8)
        canvas[pad_y : pad_y + h, pad_x : pad_x + w] = rgb
        x = cv2.resize(canvas, (DETECTOR_INPUT, DETECTOR_INPUT), interpolation=cv2.INTER_LINEAR)
        x = (x.astype(np.float32) / 127.5 - 1.0)[None]
        regressors, scores = self.session.run(None, {self.input_name: x})
        regressors, scores = regressors[0], _sigmoid(scores[0, :, 0])
        keep = scores >= MIN_DETECTION_SCORE
        if not keep.any():
            return []
        reg, scores, anchors = regressors[keep], scores[keep], self.anchors[keep]
        cx = reg[:, 0] / DETECTOR_INPUT + anchors[:, 0]
        cy = reg[:, 1] / DETECTOR_INPUT + anchors[:, 1]
        bw = reg[:, 2] / DETECTOR_INPUT
        bh = reg[:, 3] / DETECTOR_INPUT
        boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1)
        keypoints = reg[:, 4:16].reshape(-1, 6, 2) / DETECTOR_INPUT + anchors[:, None, :]
        boxes = boxes * side - np.array([pad_x, pad_y, pad_x, pad_y])
        keypoints = keypoints * side - np.array([pad_x, pad_y])
        return self._weighted_nms(boxes, keypoints, scores)

    @staticmethod
    def _weighted_nms(boxes, keypoints, scores) -> list[_Detection]:
        remaining = list(np.argsort(-scores))
        result = []
        while remaining:
            first = remaining[0]
            group = [j for j in remaining if _iou(boxes[first], boxes[j]) > NMS_IOU]
            weights = scores[group][:, None]
            box = (boxes[group] * weights).sum(axis=0) / weights.sum()
            kps = (keypoints[group] * weights[:, :, None]).sum(axis=0) / weights.sum()
            result.append(_Detection(box, kps, float(scores[first])))
            remaining = [j for j in remaining if j not in group]
        return result


# ----------------------------------------------------------------------- smoothing


class _LowPass:
    def __init__(self) -> None:
        self.raw: Optional[np.ndarray] = None
        self.stored: Optional[np.ndarray] = None

    def apply(self, value, alpha):
        out = value if self.stored is None else alpha * value + (1.0 - alpha) * self.stored
        self.raw, self.stored = value, out
        return out


class OneEuroSmoother:
    """MediaPipe's LandmarksSmoothingCalculator, one_euro_filter variant, with
    value scaling by object size, vectorised over every landmark coordinate."""

    def __init__(self, min_cutoff: float = 0.05, beta: float = 80.0, derivate_cutoff: float = 1.0,
                 frequency: float = 30.0) -> None:
        self.min_cutoff, self.beta, self.derivate_cutoff, self.initial_frequency = (
            min_cutoff, beta, derivate_cutoff, frequency)
        self.reset()

    def reset(self) -> None:
        self._x, self._dx = _LowPass(), _LowPass()
        self._last_ms: Optional[int] = None
        self._frequency = self.initial_frequency

    def _alpha(self, cutoff):
        te = 1.0 / self._frequency
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / te)

    def apply(self, points: np.ndarray, timestamp_ms: int) -> np.ndarray:
        if self._last_ms is not None and timestamp_ms <= self._last_ms:
            return points  # MediaPipe passes such a packet through unfiltered
        if self._last_ms is not None:
            self._frequency = 1000.0 / (timestamp_ms - self._last_ms)
        self._last_ms = timestamp_ms
        x0, y0 = points[:, :2].min(axis=0)
        x1, y1 = points[:, :2].max(axis=0)
        object_scale = ((x1 - x0) + (y1 - y0)) / 2.0
        value_scale = 1.0 / object_scale if object_scale > 0 else 1.0
        if self._x.raw is None:
            derivative = np.zeros_like(points)
        else:
            derivative = (points - self._x.raw) * value_scale * self._frequency
        smoothed_derivative = self._dx.apply(derivative, self._alpha(self.derivate_cutoff))
        cutoff = self.min_cutoff + self.beta * np.abs(smoothed_derivative)
        return self._x.apply(points, self._alpha(cutoff))


# ------------------------------------------------------------------------ geometry


def _varint(buffer: bytes, index: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buffer[index]
        index += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return result, index


def _proto_fields(buffer: bytes):
    index = 0
    while index < len(buffer):
        key, index = _varint(buffer, index)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, index = _varint(buffer, index)
        elif wire == 1:
            value, index = buffer[index : index + 8], index + 8
        elif wire == 2:
            length, index = _varint(buffer, index)
            value, index = buffer[index : index + length], index + length
        elif wire == 5:
            value, index = buffer[index : index + 4], index + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        yield number, wire, value


def load_geometry(path: Path = GEOMETRY_METADATA) -> tuple[np.ndarray, np.ndarray]:
    """(468, 3) canonical face (cm) and (468,) Procrustes weights out of
    MediaPipe's GeometryPipelineMetadata, verified against its pinned hash."""
    models.verify(path, models.GEOMETRY_SHA256, "face geometry metadata")
    data = Path(path).read_bytes()
    vertices: list[float] = []
    ids: list[int] = []
    weights: list[float] = []
    for number, wire, value in _proto_fields(data):
        if number == 1:  # canonical_mesh
            for sub, sub_wire, sub_value in _proto_fields(value):
                if sub == 3:  # vertex_buffer
                    if sub_wire == 2:
                        vertices += struct.unpack(f"<{len(sub_value) // 4}f", sub_value)
                    else:
                        vertices.append(struct.unpack("<f", sub_value)[0])
        elif number == 2:  # procrustes_landmark_basis
            landmark, weight = 0, 0.0
            for sub, _, sub_value in _proto_fields(value):
                if sub == 1:
                    landmark = sub_value
                elif sub == 2:
                    weight = struct.unpack("<f", sub_value)[0]
            ids.append(landmark)
            weights.append(weight)
    canonical = np.array(vertices, dtype=np.float64).reshape(-1, 5)[:, :3]  # x y z u v
    w = np.zeros(len(canonical))
    w[ids] = weights
    return canonical, w


def solve_weighted_orthogonal(source: np.ndarray, target: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """ProcrustesSolver::SolveWeightedOrthogonalProblem. (3, N) -> 4x4."""
    sqrt_w = np.sqrt(weights)
    ws = source * sqrt_w
    wt = target * sqrt_w
    total = float((sqrt_w * sqrt_w).sum())
    source_com = (ws * sqrt_w).sum(axis=1) / total
    centered = ws - np.outer(source_com, sqrt_w)
    design = wt @ centered.T
    u, _, vt = np.linalg.svd(design)
    post = u.copy()
    if np.linalg.det(post) * np.linalg.det(vt) < 0:
        post[:, 2] *= -1
    rotation = post @ vt
    scale = float(((rotation @ centered) * wt).sum() / (centered * ws).sum())
    rs = scale * rotation
    translation = (((wt - rs @ ws) * sqrt_w).sum(axis=1)) / total
    m = np.eye(4)
    m[:3, :3] = rs
    m[:3, 3] = translation
    return m


class FaceGeometry:
    """GeometryPipeline for FaceLandmarker's default perspective camera."""

    def __init__(self, path: Path = GEOMETRY_METADATA, fov_y_degrees: float = 63.0, near: float = 1.0) -> None:
        canonical, self.weights = load_geometry(path)
        self.canonical = canonical.T
        self.fov_y, self.near = fov_y_degrees, near

    def transformation_matrix(self, points_px: np.ndarray, width: int, height: int) -> np.ndarray:
        n = np.vstack([points_px[:468, 0] / width, points_px[:468, 1] / height, points_px[:468, 2] / width])
        height_at_near = 2.0 * self.near * math.tan(math.radians(self.fov_y) / 2.0)
        width_at_near = width * height_at_near / height
        left, bottom = -width_at_near / 2.0, -height_at_near / 2.0
        x_scale, y_scale = width_at_near, height_at_near
        screen = n.copy()
        screen[1] = 1.0 - screen[1]  # origin top-left
        screen[0] = screen[0] * x_scale + left
        screen[1] = screen[1] * y_scale + bottom
        screen[2] = screen[2] * x_scale
        depth_offset = float(screen[2].mean())

        def move_and_rescale_z(points, scale):
            points[2] = (points[2] - depth_offset + self.near) / scale

        def unproject(points):
            points[0] = points[0] * points[2] / self.near
            points[1] = points[1] * points[2] / self.near

        def estimate_scale(points):
            m = solve_weighted_orthogonal(self.canonical, points, self.weights)
            return float(np.linalg.norm(m[:3, 0]))

        intermediate = screen.copy()
        intermediate[2] *= -1
        first = estimate_scale(intermediate)
        intermediate = screen.copy()
        move_and_rescale_z(intermediate, first)
        unproject(intermediate)
        intermediate[2] *= -1
        second = estimate_scale(intermediate)
        metric = screen.copy()
        move_and_rescale_z(metric, first * second)
        unproject(metric)
        metric[2] *= -1
        return solve_weighted_orthogonal(self.canonical, metric, self.weights)


# ---------------------------------------------------------------------- landmarker


class OnnxLandmarker:
    """Drop-in for the MediaPipe FaceLandmarker in VIDEO mode, one face."""

    def __init__(
        self,
        task_path: Optional[Path] = None,
        num_faces: int = 1,
        *,
        detector_path: Optional[Path] = None,
        mesh_path: Optional[Path] = None,
        smooth: bool = True,
        providers: Optional[list[str]] = None,
    ) -> None:
        # `task_path` is the MediaPipe backend's argument; here it only counts
        # when it names a directory holding the two networks.
        import onnxruntime

        if num_faces != 1:
            raise ValueError("this landmarker tracks exactly one face")
        if task_path is not None and Path(task_path).is_dir():
            detector_path = detector_path or Path(task_path) / paths.FACE_DETECTOR_NAME
            mesh_path = mesh_path or Path(task_path) / paths.FACE_MESH_NAME
        self.detector_path = models.verify(
            Path(detector_path or paths.face_detector_model()), models.FACE_DETECTOR.sha256, "face detector")
        self.mesh_path = models.verify(
            Path(mesh_path or paths.face_mesh_model()), models.FACE_MESH.sha256, "face landmarker")
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 2  # a lock-screen helper, not a benchmark
        providers = providers or ["CPUExecutionProvider"]
        self._detector = _BlazeFace(onnxruntime.InferenceSession(
            str(self.detector_path), sess_options=options, providers=providers))
        self._mesh = onnxruntime.InferenceSession(str(self.mesh_path), sess_options=options, providers=providers)
        self._mesh_input = self._mesh.get_inputs()[0].name
        self._geometry = FaceGeometry()
        self._smoother = OneEuroSmoother() if smooth else None
        self._roi: Optional[Roi] = None

    def reset(self) -> None:
        self._roi = None
        if self._smoother is not None:
            self._smoother.reset()

    def _run_mesh(self, rgb: np.ndarray, roi: Roi) -> tuple[np.ndarray, float]:
        import cv2

        affine = _roi_affine(roi, MESH_INPUT)
        crop = cv2.warpAffine(rgb, affine, (MESH_INPUT, MESH_INPUT), flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_REPLICATE)
        landmarks, presence, _ = self._mesh.run(None, {self._mesh_input: (crop.astype(np.float32) / 255.0)[None]})
        points = landmarks.reshape(478, 3).astype(np.float64)
        inverse = cv2.invertAffineTransform(affine)
        xy = points[:, :2] @ inverse[:, :2].T + inverse[:, 2]
        z = points[:, 2] * roi.size / MESH_INPUT
        return np.column_stack([xy, z]), float(_sigmoid(float(np.ravel(presence)[0])))

    def detect_points(self, frame_rgb: np.ndarray, timestamp_ms: Optional[int]) -> Optional[tuple[np.ndarray, np.ndarray]]:
        """(478, 3) pixel landmarks (z in pixels) and the 4x4 transformation
        matrix, or None when there is no face."""
        rgb = np.ascontiguousarray(frame_rgb)
        roi = self._roi
        if roi is None:
            detections = self._detector.detect(rgb)
            if not detections:
                self.reset()
                return None
            best = max(detections, key=lambda d: d.score)
            x0, y0, x1, y1 = best.box
            roi = _roi_from_box(x0, y0, x1, y1, _rotation(best.keypoints[0], best.keypoints[1]))
        points, presence = self._run_mesh(rgb, roi)
        if presence < MIN_PRESENCE:
            self.reset()
            return None
        self._roi = roi_from_landmarks(points)  # next ROI from the raw landmarks
        if self._smoother is not None and timestamp_ms is not None:
            points = self._smoother.apply(points, int(timestamp_ms))
        height, width = rgb.shape[:2]
        return points, self._geometry.transformation_matrix(points, width, height)

    def detect(self, frame_rgb: np.ndarray, timestamp_ms: int) -> Optional[DetectedFace]:
        found = self.detect_points(frame_rgb, timestamp_ms)
        if found is None:
            return None
        points, matrix = found
        mesh = points[:, :2].astype(float)
        x0, y0 = mesh.min(axis=0)
        x1, y1 = mesh.max(axis=0)
        return DetectedFace(
            mesh=mesh,
            bounding_box=(float(x0), float(y0), float(x1 - x0), float(y1 - y0)),
            yaw=yaw_from_transformation_matrix(matrix),
            pitch=pitch_from_transformation_matrix(matrix),
        )

    def close(self) -> None:
        self._detector = None  # type: ignore[assignment]
        self._mesh = None  # type: ignore[assignment]
