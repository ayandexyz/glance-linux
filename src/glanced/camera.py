"""V4L2 capture.

Two frames come out of every grab, and the distinction matters to liveness:

* the **full frame**, which the bezel detector needs because it has to look
  *around* the face for a device edge, not just at it;
* a **native-resolution crop** around the face for the gloss/glare cue, which
  only ever downsamples — never upsamples — so `GlareSample.crop_pixel_width`
  stays an honest measure of how much real detail was available.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional, Sequence

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]

#: The resolution the liveness tuning constants assume. Several gates
#: (`min_yaw_range_degrees`, `MIN_RELIABLE_INTEROCULAR_PX`) are expressed in
#: pixels at this width; changing it silently invalidates them.
WORKING_WIDTH = 640


@dataclass
class CameraConfig:
    device: str = "/dev/video0"
    width: int = 1280
    height: int = 720
    fps: int = 30


class Camera:
    def __init__(self, config: CameraConfig = CameraConfig()) -> None:
        if cv2 is None:
            raise RuntimeError("opencv-python is required for capture")
        self.config = config
        self._capture: Optional["cv2.VideoCapture"] = None

    def __enter__(self) -> "Camera":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        capture = cv2.VideoCapture(self.config.device, cv2.CAP_V4L2)
        if not capture.isOpened():
            raise RuntimeError(f"could not open {self.config.device}")
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
        capture.set(cv2.CAP_PROP_FPS, self.config.fps)
        self._capture = capture

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def frames(self) -> Iterator[np.ndarray]:
        """Yield RGB frames until the device stops delivering."""
        if self._capture is None:
            raise RuntimeError("camera is not open")
        while True:
            ok, bgr = self._capture.read()
            if not ok:
                return
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
