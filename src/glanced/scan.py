"""Camera frame -> everything the pipeline wants from it.

One object owns the landmarker and the embedder, and turns a native camera
frame into a `LivenessFrame` plus (optionally) an ArcFace embedding. The
daemon's unlock scan, enrollment, and `glancectl live` all go through here, so
the working-resolution rules in `camera.py` are applied in exactly one place.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from . import paths
from .align import align
from .camera import render_crop, to_working_resolution
from .landmarker import DetectedFace, Landmarker
from .liveness import LivenessFrame
from .liveness.features import extract


@dataclass
class Observation:
    face: DetectedFace
    liveness_frame: LivenessFrame
    #: L2-normalized (512,) float32, or None when alignment failed or the
    #: caller did not ask for one.
    embedding: Optional[np.ndarray]
    timestamp: float
    #: The face box in *native* camera pixels. `face.bounding_box` is in
    #: working resolution; anything drawing on the full frame — the lock
    #: screen preview — needs this one instead of rediscovering the scale.
    native_bounding_box: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


class FaceProcessor:
    def __init__(
        self,
        *,
        landmarker_task: Optional[Path] = None,
        arcface_model: Optional[Path] = None,
        embed: bool = True,
    ) -> None:
        self.landmarker = Landmarker(landmarker_task or paths.landmarker_task())
        self.embedder = None
        if embed:
            from .embed import ArcFaceEmbedder

            self.embedder = ArcFaceEmbedder(arcface_model or paths.arcface_model())

    def process(
        self, native_rgb: np.ndarray, now: Optional[float] = None, *, want_embedding: bool = True
    ) -> Optional[Observation]:
        now = time.monotonic() if now is None else now
        working, scale = to_working_resolution(native_rgb)
        face = self.landmarker.detect(working, int(now * 1000))
        if face is None:
            return None

        # The glare cue wants native-resolution detail, so the crop is taken
        # from the full frame, not the downscaled one.
        native_box = tuple(v / scale for v in face.bounding_box)
        liveness_frame = extract(
            face.mesh,
            face.bounding_box,
            frame_rgb=working,
            face_crop=render_crop(native_rgb, native_box),
            yaw=face.yaw,
            timestamp=now,
        )

        embedding = None
        if want_embedding and self.embedder is not None:
            points = face.five_points()
            if points is not None:
                # Alignment from the native frame too: the recognizer is the
                # one consumer that benefits from every pixel the sensor has.
                aligned = align(native_rgb, points / scale)
                if aligned is not None:
                    embedding = self.embedder.embed(aligned)

        return Observation(
            face=face,
            liveness_frame=liveness_frame,
            embedding=embedding,
            timestamp=now,
            native_bounding_box=native_box,
        )

    def close(self) -> None:
        self.landmarker.close()
