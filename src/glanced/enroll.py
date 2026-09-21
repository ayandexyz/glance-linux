"""Enrollment: turn a few seconds of webcam into an encrypted set of embeddings.

No image is ever written. Each capture is aligned, embedded, and the frame is
gone; what lands on disk is N rows of 512 floats under AES-256-GCM.

Captures are spaced out in time and the user is asked to move between them,
because several near-identical embeddings add nothing: the point of multiple
rows per identity is to cover glasses, beard, lighting, and pose.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from . import poses
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


# --- Guided multi-direction enrollment ------------------------------------
#
# The unguided path above asks for movement and hopes; this one checks. Each
# the poses in `glanced.poses` has to actually be held before its samples
# count, which is what makes a ten-row template cover five directions rather
# than ten near-copies of whatever the user happened to be doing.
#
# The session is a pure state machine over observations: it owns no camera and
# no clock of its own, so the CLI loop and the Qt window drive the same object
# and a test can drive it with synthetic frames.

#: Re-exported so callers already holding `enroll` do not need `poses` too.
SAMPLES_PER_POSE = poses.SAMPLES_PER_POSE

#: Consecutive matching frames before a sample is taken. A debounce, so one
#: lucky frame at a band edge cannot fire a capture mid-turn.
REQUIRED_STREAK = 3

#: How long a pose must be held, once matched, before samples start counting.
POSE_HOLD_SECONDS = 0.5

#: Grace period after the camera opens. Detection and the live readout run
#: throughout; only capture is held back, so the ring is never frozen while
#: the user is still settling into frame.
SETTLE_SECONDS = 1.5

#: Enrollment wants a closer face than unlock does. Sitting back in a chair is
#: prominent enough to unlock but too small for a template worth keeping, as a
#: fraction of frame width.
MIN_FACE_WIDTH_FRACTION = 0.20


@dataclass
class GuidedProgress:
    """Everything a UI needs to draw the ring, and nothing it does not."""

    pose: Optional[poses.Pose]
    pose_index: int
    pose_count: int
    captured_in_pose: int
    samples_per_pose: int
    #: Poses fully captured so far, by name — the lit ring sectors.
    captured_poses: tuple[str, ...]
    #: Whether the current frame is holding the requested pose.
    holding: bool
    #: Whether a face was seen at all in the current frame.
    face_detected: bool
    #: Face seen, but too small to enrol from.
    too_far: bool
    #: Bands have widened because this pose is taking too long.
    widened: bool
    yaw: Optional[float]
    pitch: Optional[float]

    @property
    def complete(self) -> bool:
        return self.pose is None

    @property
    def fraction(self) -> float:
        done = self.pose_index * self.samples_per_pose + self.captured_in_pose
        return done / float(self.pose_count * self.samples_per_pose)


class GuidedSession:
    """Drives the pose sweep, one observation at a time.

    Call :meth:`offer` with every processed frame. It returns the pose that
    was *completed* by that frame, or `None` — which is the only event a ring
    animation needs, since a completed pose is what lights a sector.
    """

    def __init__(
        self,
        *,
        samples_per_pose: int = SAMPLES_PER_POSE,
        tuning: poses.Tuning = poses.TUNING,
        min_face_width_fraction: float = MIN_FACE_WIDTH_FRACTION,
    ) -> None:
        self.samples_per_pose = samples_per_pose
        self.tuning = tuning
        self.min_face_width_fraction = min_face_width_fraction

        self.embeddings: list[np.ndarray] = []
        #: Parallel to `embeddings`: which pose each row was captured in.
        self.pose_names: list[str] = []
        self.captured_poses: list[str] = []

        self._index = 0
        self._captured_in_pose = 0
        self._streak = 0
        self._started: Optional[float] = None
        self._pose_started: Optional[float] = None
        self._hold_started: Optional[float] = None
        self._last = GuidedProgress(
            pose=poses.POSES[0],
            pose_index=0,
            pose_count=len(poses.POSES),
            captured_in_pose=0,
            samples_per_pose=samples_per_pose,
            captured_poses=(),
            holding=False,
            face_detected=False,
            too_far=False,
            widened=False,
            yaw=None,
            pitch=None,
        )

    @property
    def current_pose(self) -> Optional[poses.Pose]:
        return poses.POSES[self._index] if self._index < len(poses.POSES) else None

    @property
    def complete(self) -> bool:
        return self._index >= len(poses.POSES)

    @property
    def progress(self) -> GuidedProgress:
        return self._last

    def offer(
        self,
        observation,
        now: float,
        *,
        frame_width: Optional[int] = None,
    ) -> Optional[poses.Pose]:
        if self._started is None:
            self._started = now
            self._pose_started = now

        pose = self.current_pose
        if pose is None:
            return None

        widened = (now - (self._pose_started or now)) > self.tuning.stall_seconds
        yaw = pitch = None
        face_detected = observation is not None
        too_far = False
        holding = False

        if observation is not None:
            yaw, pitch = observation.face.yaw, observation.face.pitch
            if frame_width:
                # `frame_width` is the native camera width, so compare the
                # native box: `face.bounding_box` is in working resolution, and
                # on a 1280-wide camera that silently doubled the threshold.
                too_far = observation.native_bounding_box[2] < self.min_face_width_fraction * frame_width
            holding = poses.matches(pose, yaw, pitch, widened=widened, tuning=self.tuning)

        completed = None
        if self._acceptable(observation, now, holding, too_far):
            completed = self._accept(observation, now, pose)
        else:
            self._streak = 0
            self._hold_started = None

        self._last = GuidedProgress(
            pose=self.current_pose,
            pose_index=self._index,
            pose_count=len(poses.POSES),
            captured_in_pose=self._captured_in_pose,
            samples_per_pose=self.samples_per_pose,
            captured_poses=tuple(self.captured_poses),
            holding=holding,
            face_detected=face_detected,
            too_far=too_far,
            widened=widened,
            yaw=yaw,
            pitch=pitch,
        )
        return completed

    def _acceptable(self, observation, now: float, holding: bool, too_far: bool) -> bool:
        if observation is None or observation.embedding is None:
            return False
        if now - (self._started or now) < SETTLE_SECONDS:
            return False
        if not observation.liveness_frame.has_reliable_landmarks:
            return False
        if too_far or not holding:
            return False
        # The same person as the first capture, on the same rule the unguided
        # path uses — a second face leaning into frame does not get enrolled
        # into somebody else's identity.
        if self.embeddings:
            if similarity(self.embeddings[0], observation.embedding) < MIN_SELF_SIMILARITY:
                return False
        return True

    def _accept(self, observation, now: float, pose: poses.Pose) -> Optional[poses.Pose]:
        if self._hold_started is None:
            self._hold_started = now
        if now - self._hold_started < POSE_HOLD_SECONDS:
            return None

        self._streak += 1
        if self._streak < REQUIRED_STREAK:
            return None
        self._streak = 0

        self.embeddings.append(observation.embedding)
        self.pose_names.append(pose.name)
        self._captured_in_pose += 1
        if self._captured_in_pose < self.samples_per_pose:
            return None

        self.captured_poses.append(pose.name)
        self._index += 1
        self._captured_in_pose = 0
        self._pose_started = now
        self._hold_started = None
        return pose


def capture_guided(
    processor: FaceProcessor,
    *,
    device: str = "/dev/video0",
    samples_per_pose: int = SAMPLES_PER_POSE,
    timeout: float = 180.0,
    on_progress: Optional[Callable[[GuidedProgress], None]] = None,
    on_pose: Optional[Callable[[poses.Pose], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> tuple[list[np.ndarray], list[str]]:
    """Run the pose sweep against a camera and return the embeddings.

    The generous default timeout is a whole guided sweep's worth: a user who
    has to work out what is being asked, and then hold it, takes considerably
    longer than one who only has to sit still and be photographed.
    """
    session = GuidedSession(samples_per_pose=samples_per_pose)
    started = time.monotonic()

    with Camera(CameraConfig(device=device)) as camera:
        for native in camera.frames():
            if should_stop is not None and should_stop():
                raise RuntimeError("cancelled")
            now = time.monotonic()
            if now - started > timeout:
                raise TimeoutError(
                    f"only {len(session.captured_poses)} of {len(poses.POSES)} poses in {timeout:.0f}s"
                )

            observation = processor.process(native, now, want_embedding=True)
            completed = session.offer(observation, now, frame_width=native.shape[1])
            if completed is not None and on_pose is not None:
                on_pose(completed)
            if on_progress is not None:
                on_progress(session.progress)
            if session.complete:
                break

    return session.embeddings, session.pose_names
