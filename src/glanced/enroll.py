"""Enrollment: turn a few seconds of webcam into an encrypted set of embeddings.

No image is ever written. Each capture is aligned, embedded, and the frame is
gone; what lands on disk is N rows of 512 floats under AES-256-GCM.

Captures are spaced out in time and the user is asked to move between them,
because several near-identical embeddings add nothing: the point of multiple
rows per identity is to cover glasses, beard, lighting, and pose.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

import numpy as np

from .camera import Camera, CameraConfig
from .embed import similarity
from .scan import FaceProcessor

PROMPTS = (
    "look straight at the camera",
    "turn your head a little to the left",
    "turn your head a little to the right",
    "tilt your chin up slightly",
    "tilt your chin down slightly",
    "look straight at the camera again",
)

#: A capture that is this dissimilar from the first one is not the same
#: person — or the first one was garbage. Either way it is not enrolled.
MIN_SELF_SIMILARITY = 0.35


def capture_embeddings(
    processor: FaceProcessor,
    *,
    device: str = "/dev/video0",
    count: int = 5,
    spacing: float = 1.2,
    timeout: float = 60.0,
    on_prompt: Optional[Callable[[int, int, str], None]] = None,
    on_capture: Optional[Callable[[int, int, float], None]] = None,
) -> list[np.ndarray]:
    embeddings: list[np.ndarray] = []
    started = time.monotonic()
    last_capture = 0.0
    prompted = -1

    with Camera(CameraConfig(device=device)) as camera:
        for native in camera.frames():
            now = time.monotonic()
            if now - started > timeout:
                raise TimeoutError(f"only {len(embeddings)} of {count} captures in {timeout:.0f}s")

            index = len(embeddings)
            if index != prompted and on_prompt is not None:
                on_prompt(index + 1, count, PROMPTS[index % len(PROMPTS)])
                prompted = index

            # Let the user actually move between captures.
            if now - last_capture < spacing:
                continue

            observation = processor.process(native, now, want_embedding=True)
            if observation is None or observation.embedding is None:
                continue
            if not observation.liveness_frame.has_reliable_landmarks:
                continue  # too far from the camera for a trustworthy embedding

            score = 1.0 if not embeddings else similarity(embeddings[0], observation.embedding)
            if score < MIN_SELF_SIMILARITY:
                continue

            embeddings.append(observation.embedding)
            last_capture = now
            if on_capture is not None:
                on_capture(len(embeddings), count, score)
            if len(embeddings) >= count:
                break

    return embeddings
