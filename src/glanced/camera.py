"""V4L2 capture.

Two frames come out of every grab, and the distinction matters to liveness:

* the **full frame**, which the bezel detector needs because it has to look
  *around* the face for a device edge, not just at it;
* a **native-resolution crop** around the face for the gloss/glare cue, which
  only ever downsamples — never upsamples — so `GlareSample.crop_pixel_width`
  stays an honest measure of how much real detail was available.

Frames are pulled off the device by a reader thread, at the camera's own
rate, whatever the consumer is doing. Some drivers stop delivering when the
application falls behind: the Apple ISP driver behind the FaceTime HD camera
on Apple Silicon Macs (Asahi Linux) answers a consumer slower than its 30 fps
with a V4L2 `select()` timeout a few seconds in, and the stream never comes
back. HEAVY liveness on a laptop CPU is exactly such a consumer. With the
reader draining the queue, the driver always sees a fast client, and
`frames()` hands out the newest frame there is instead of a stale one from the
front of the queue.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Optional, Sequence

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]

#: The resolution the liveness tuning constants assume. Several gates
#: (`min_yaw_range_degrees`, `MIN_RELIABLE_INTEROCULAR_PX`) are expressed in
#: pixels at this width; changing it silently invalidates them.
WORKING_WIDTH = 640

#: How long `frames()` waits for a frame before deciding the device has
#: stopped delivering, and ending.
STALL_TIMEOUT = 5.0


@dataclass
class CameraConfig:
    device: str = "/dev/video0"
    width: int = 1280
    height: int = 720
    fps: int = 30


def _open_v4l2(device: str):
    return cv2.VideoCapture(device, cv2.CAP_V4L2)


class Camera:
    def __init__(
        self,
        config: CameraConfig = CameraConfig(),
        *,
        capture_factory: Optional[Callable[[str], object]] = None,
        stall_timeout: float = STALL_TIMEOUT,
        close_timeout: float = 2.0,
    ) -> None:
        if cv2 is None:
            raise RuntimeError("opencv-python is required for capture")
        self.config = config
        self.stall_timeout = stall_timeout
        self.close_timeout = close_timeout
        self._capture_factory = capture_factory or _open_v4l2
        self._capture = None
        self._reader: Optional[threading.Thread] = None
        # Everything below is shared with the reader thread, under _cond.
        self._cond = threading.Condition()
        self._stopping = False
        self._ended = False
        self._wanted = False
        self._frame: Optional[np.ndarray] = None
        self._sequence = 0
        self._release_in_reader = False

    def __enter__(self) -> "Camera":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        capture = self._capture_factory(self.config.device)
        if not capture.isOpened():
            raise RuntimeError(f"could not open {self.config.device}")
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
        capture.set(cv2.CAP_PROP_FPS, self.config.fps)
        self._capture = capture
        with self._cond:
            self._stopping = self._ended = self._wanted = self._release_in_reader = False
            self._frame, self._sequence = None, 0
        self._reader = threading.Thread(target=self._read_loop, args=(capture,), name="glance-camera", daemon=True)
        self._reader.start()

    def close(self) -> None:
        reader, capture = self._reader, self._capture
        self._reader = self._capture = None
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        if reader is not None:
            # A grab blocked in the driver returns on its own select()
            # timeout; until then the capture is the reader's to release.
            reader.join(timeout=self.close_timeout)
            if reader.is_alive():
                with self._cond:
                    if not self._ended:
                        self._release_in_reader = True
                        return
        if capture is not None:
            capture.release()

    def _read_loop(self, capture) -> None:
        """Grab every frame the device offers, so its queue never backs up.

        Only a frame somebody is waiting for is decoded: the rest are grabbed
        and dropped, which is what keeps attention mode cheap at 8 fps on a
        30 fps camera.
        """
        try:
            while True:
                with self._cond:
                    if self._stopping:
                        return
                if not capture.grab():
                    return
                with self._cond:
                    if not self._wanted:
                        continue
                ok, bgr = capture.retrieve()
                if not ok:
                    return
                with self._cond:
                    self._frame = bgr
                    self._sequence += 1
                    self._wanted = False
                    self._cond.notify_all()
        finally:
            with self._cond:
                self._ended = True
                release = self._release_in_reader
                self._cond.notify_all()
            if release:
                capture.release()

    def _next_frame(self, after: int) -> tuple[Optional[np.ndarray], int]:
        """Ask the reader for a frame newer than `after` and wait for it."""
        deadline = time.monotonic() + self.stall_timeout
        with self._cond:
            self._wanted = True
            while self._sequence <= after:
                if self._stopping or self._ended:
                    return None, after
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None, after
                self._cond.wait(remaining)
            frame, self._frame = self._frame, None
            return frame, self._sequence

    def _sleep_until(self, when: float) -> bool:
        """Wait until `when` (monotonic), or until the camera stops. True if
        the camera is still running."""
        with self._cond:
            while not (self._stopping or self._ended):
                remaining = when - time.monotonic()
                if remaining <= 0:
                    return True
                self._cond.wait(remaining)
            return False

    def frames(self, min_interval: float = 0.0) -> Iterator[np.ndarray]:
        """Yield RGB frames until the device stops delivering.

        Each frame is the newest one the camera produced after the previous
        one was handed out; frames that arrive while the caller is busy are
        dropped by the reader thread, not queued. No frame for
        `stall_timeout` seconds ends the generator, as a failed grab does.

        `min_interval` throttles by wall clock: frames arriving sooner than
        that after the last one yielded are grabbed and dropped without being
        decoded or converted. That is how attention mode runs the landmarker
        at 8 fps on a camera that only offers 30 — asking the driver for a
        lower rate is a request most UVC webcams quietly ignore.
        """
        if self._capture is None:
            raise RuntimeError("camera is not open")
        sequence = 0
        next_at = 0.0
        while True:
            if min_interval > 0 and not self._sleep_until(next_at):
                return
            bgr, sequence = self._next_frame(sequence)
            if bgr is None:
                return
            next_at = time.monotonic() + min_interval
            yield cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def to_working_resolution(frame: np.ndarray, width: int = WORKING_WIDTH) -> tuple[np.ndarray, float]:
    """Downscale to the working width. Returns the frame and the scale factor
    that maps working-resolution coordinates back to native pixels."""
    height, native_width = frame.shape[:2]
    if native_width <= width:
        return frame, 1.0
    scale = width / native_width
    resized = cv2.resize(frame, (width, int(round(height * scale))), interpolation=cv2.INTER_AREA)
    return resized, scale


def render_crop(
    frame: np.ndarray, bounding_box: Sequence[float], padding: float = 0.15
) -> Optional[np.ndarray]:
    """Native-resolution crop around a face box, with a little padding.

    Never upsamples: if the requested region is smaller than the box asks for,
    what comes back is what the sensor actually resolved. The gloss/glare cue
    confidence-weights on the returned width precisely so that an
    under-resolved crop abstains instead of guessing.
    """
    height, width = frame.shape[:2]
    x, y, w, h = (float(v) for v in bounding_box)
    pad_x, pad_y = w * padding, h * padding
    x0 = int(max(0, round(x - pad_x)))
    y0 = int(max(0, round(y - pad_y)))
    x1 = int(min(width, round(x + w + pad_x)))
    y1 = int(min(height, round(y + h + pad_y)))
    if x1 <= x0 or y1 <= y0:
        return None
    return frame[y0:y1, x0:x1]
