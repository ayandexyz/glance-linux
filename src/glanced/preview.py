"""Live preview frames for the lock screen.

The daemon owns the camera for the whole scan, so nothing else can open it —
V4L2 will hand a second process a device but not a second stream. The lock
screen therefore cannot show you your own face by capturing it; the frames
have to come from the daemon.

So during a scan the daemon drops a small JPEG here, in its runtime directory,
and the lock screen's indicator polls it. The file is:

* **small** — a square crop around the face, 192px, quality 70, a few KB, so
  writing one per frame costs less than the landmarking already does;
* **mirrored**, because a preview of your own face that moves the wrong way
  when you tilt your head is disorienting — every selfie view is flipped;
* **replaced atomically**, so a reader either gets the previous frame whole or
  the next one whole, never half of either;
* **deleted when the scan ends**, and at daemon startup, so a crash cannot
  leave a picture of your face sitting in the runtime directory.

It lives on tmpfs inside a 0700 directory and is written 0600, so it never
touches the disk and no other user can read it. `--no-preview` turns the whole
thing off for anyone who would rather the camera never produce a file at all.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from .ipc import RUNTIME_DIR

PREVIEW_PATH = RUNTIME_DIR / "preview.jpg"

#: Rendered square, in pixels. The indicator draws it at ~40px, so this is
#: already generous — it only has to survive HiDPI scaling.
PREVIEW_SIZE = 192

#: How much room to leave around the face box, as a multiple of its longer
#: side. 1.6 keeps the chin and some hair in frame at a normal sitting
#: distance, which reads as a portrait rather than a crop of a nose.
FACE_CONTEXT = 1.6


def _square(center_x: float, center_y: float, side: float, width: int, height: int) -> tuple[int, int, int, int]:
    """A square of `side` centred where asked, shifted (not squashed) to stay
    inside the frame. Keeping it square is what stops the circular mask from
    stretching the face."""
    side = float(min(side, width, height))
    half = side / 2.0
    x = min(max(center_x, half), width - half)
    y = min(max(center_y, half), height - half)
    x0, y0 = int(round(x - half)), int(round(y - half))
    return x0, y0, x0 + int(round(side)), y0 + int(round(side))


class PreviewWriter:
    """Publishes the current camera frame for the lock screen to display."""

    def __init__(
        self,
        path: Path = PREVIEW_PATH,
        *,
        size: int = PREVIEW_SIZE,
        quality: int = 70,
        min_interval: float = 0.05,
    ) -> None:
        self.path = Path(path)
        self.size = size
        self.quality = quality
        self.min_interval = min_interval
        self._last_write = 0.0
        self._failed = False

    def write(
        self,
        native_rgb: np.ndarray,
        face_box: Optional[Sequence[float]] = None,
        now: Optional[float] = None,
    ) -> bool:
        """Publish one frame. Returns whether anything was written.

        Never raises: a preview that cannot be written is a cosmetic failure,
        and must not be able to fail an unlock. A frame that yields nothing to
        show is simply skipped, while an outright failure — a broken encoder,
        an unwritable runtime directory — is latched, so it is not retried
        thirty times a second for the rest of the scan.
        """
        if self._failed:
            return False
        now = time.monotonic() if now is None else now
        if now - self._last_write < self.min_interval:
            return False
        try:
            published = self._write(native_rgb, face_box)
        except Exception:
            self._failed = True
            return False
        if published:
            self._last_write = now
        return published

    def _write(self, native_rgb: np.ndarray, face_box: Optional[Sequence[float]]) -> bool:
        import cv2

        height, width = native_rgb.shape[:2]
        if face_box is not None:
            x, y, w, h = (float(v) for v in face_box)
            box = _square(x + w / 2.0, y + h / 2.0, max(w, h) * FACE_CONTEXT, width, height)
        else:
            # No face yet: the middle of the frame, so the user can see
            # themselves and move into it.
            box = _square(width / 2.0, height / 2.0, min(width, height), width, height)

        x0, y0, x1, y1 = box
        crop = native_rgb[y0:y1, x0:x1]
        if crop.size == 0:
            return False
        # Mirrored, then resized: flipping the small image would be cheaper by
        # a rounding error and reads identically, but flipping first keeps the
        # resize sampling the same pixels it would have anyway.
        crop = crop[:, ::-1]
        resized = cv2.resize(crop, (self.size, self.size), interpolation=cv2.INTER_AREA)
        ok, buffer = cv2.imencode(
            ".jpg", cv2.cvtColor(resized, cv2.COLOR_RGB2BGR), [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]
        )
        if not ok:
            raise RuntimeError("JPEG encode failed")

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(self.path.name + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(buffer.tobytes())
        os.replace(temporary, self.path)  # atomic: readers see one frame or the other
        return True

    def clear(self) -> None:
        """Remove the published frame. Safe to call when there is none."""
        for path in (self.path, self.path.with_name(self.path.name + ".tmp")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
