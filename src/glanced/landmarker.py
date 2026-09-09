"""MediaPipe FaceLandmarker adapter.

The single place MediaPipe is touched. Everything downstream consumes a plain
(N, 2) pixel-space mesh plus a yaw, so swapping in SCRFD — or a model with a
real anti-spoof head — does not reach into the liveness code at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from . import paths
from .liveness.features import pitch_from_transformation_matrix, yaw_from_transformation_matrix

#: The five points ArcFace alignment expects, as FaceMesh indices:
#: left eye centre, right eye centre, nose tip, left mouth corner, right mouth
#: corner. Eye centres use the iris landmarks when the mesh carries them.
FIVE_POINT_INDICES = (468, 473, 1, 61, 291)
FIVE_POINT_FALLBACK = (33, 263, 1, 61, 291)


@dataclass
class DetectedFace:
    #: (N, 2) landmark mesh in top-left-origin pixel space.
    mesh: np.ndarray
    #: (x, y, w, h) in the same pixel space.
    bounding_box: tuple[float, float, float, float]
    yaw: Optional[float]
    #: Head pitch in radians. Only guided enrollment reads it; the liveness
    #: cues are yaw-driven and ignore it entirely.
    pitch: Optional[float] = None

    def five_points(self) -> Optional[np.ndarray]:
        indices = (
            FIVE_POINT_INDICES if len(self.mesh) > max(FIVE_POINT_INDICES) else FIVE_POINT_FALLBACK
        )
        if len(self.mesh) <= max(indices):
            return None
        return self.mesh[list(indices)]


class Landmarker:
    def __init__(self, task_path: Optional[Path] = None, num_faces: int = 1) -> None:
        from mediapipe.tasks.python import BaseOptions, vision

        self.task_path = Path(task_path or paths.landmarker_task())
        if not self.task_path.exists():
            raise FileNotFoundError(
                f"face_landmarker.task not found at {self.task_path}. "
                "Fetch it with `glancectl fetch-model`."
            )
        self._landmarker = vision.FaceLandmarker.create_from_options(
            vision.FaceLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=str(self.task_path)),
                running_mode=vision.RunningMode.VIDEO,
                num_faces=num_faces,
                # The yaw the depth/pose and flat-vs-3D gates both depend on.
                output_facial_transformation_matrixes=True,
            )
        )

    def detect(self, frame_rgb: np.ndarray, timestamp_ms: int) -> Optional[DetectedFace]:
        import mediapipe as mp

        height, width = frame_rgb.shape[:2]
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(frame_rgb))
        result = self._landmarker.detect_for_video(image, timestamp_ms)
        if not result.face_landmarks:
            return None

        landmarks = result.face_landmarks[0]
        mesh = np.array([(lm.x * width, lm.y * height) for lm in landmarks], dtype=float)

        yaw = pitch = None
        matrices = getattr(result, "facial_transformation_matrixes", None)
        if matrices:
            matrix = np.asarray(matrices[0])
            yaw = yaw_from_transformation_matrix(matrix)
            pitch = pitch_from_transformation_matrix(matrix)

        x0, y0 = mesh.min(axis=0)
        x1, y1 = mesh.max(axis=0)
        return DetectedFace(
            mesh=mesh,
            bounding_box=(float(x0), float(y0), float(x1 - x0), float(y1 - y0)),
            yaw=yaw,
            pitch=pitch,
        )

    def close(self) -> None:
        self._landmarker.close()
