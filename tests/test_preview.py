"""The lock screen preview frames.

What matters here is not that a JPEG appears, but that what appears is the
right way round, the right colours, and never half-written — the indicator
draws it at 44px inside a circle, where a mirrored-wrong or channel-swapped
frame is subtle enough to ship by accident.
"""

from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from glanced.preview import PreviewWriter, _square


def _read(path):
    return cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2RGB)


def _frame(width=640, height=480):
    return np.zeros((height, width, 3), dtype=np.uint8)


def test_channel_order_survives_the_round_trip(tmp_path):
    frame = _frame()
    frame[:, :, 0] = 255  # pure red, in RGB
    writer = PreviewWriter(tmp_path / "preview.jpg")
    assert writer.write(frame)

    mean = _read(writer.path).reshape(-1, 3).mean(axis=0)
    assert mean[0] > 180, f"red channel lost: {mean}"
    assert mean[2] < 60, f"blue channel gained — RGB/BGR swap: {mean}"


def test_the_view_is_mirrored(tmp_path):
    # A marker hard against the right of the crop must come back hard against
    # the left: a preview of your own face that moves the wrong way when you
    # lean is worse than no preview.
    frame = _frame()
    frame[:, 500:540, 1] = 255
    writer = PreviewWriter(tmp_path / "preview.jpg")
    assert writer.write(frame)

    green_by_column = _read(writer.path)[:, :, 1].mean(axis=0)
    assert green_by_column.argmax() < writer.size * 0.25, "marker did not cross to the left"


def test_the_crop_follows_the_face(tmp_path):
    # Two frames, same scene, face box moved. The crop must move with it.
    frame = _frame()
    frame[:, :, :] = 40
    frame[100:160, 80:140, 1] = 255  # a bright patch at the left of the frame
    writer = PreviewWriter(tmp_path / "preview.jpg")

    assert writer.write(frame, (80, 100, 60, 60))
    on_face = _read(writer.path)[:, :, 1].mean()
    writer._last_write = 0.0

    assert writer.write(frame, (500, 300, 60, 60))
    elsewhere = _read(writer.path)[:, :, 1].mean()
    assert on_face > elsewhere * 2, f"crop ignored the face box: {on_face} vs {elsewhere}"


def test_the_crop_is_square_and_inside_the_frame():
    # A face at the very edge must shift the square, never squash it — the
    # circular mask would turn a non-square crop into a stretched face.
    for center in ((0, 0), (640, 480), (320, 5), (635, 240)):
        x0, y0, x1, y1 = _square(center[0], center[1], 300, 640, 480)
        assert x1 - x0 == y1 - y0, "crop is not square"
        assert 0 <= x0 < x1 <= 640 and 0 <= y0 < y1 <= 480, "crop left the frame"


def test_a_face_larger_than_the_frame_is_clamped():
    x0, y0, x1, y1 = _square(320, 240, 5000, 640, 480)
    assert (x1 - x0, y1 - y0) == (480, 480), "crop should clamp to the short side"


def test_rate_limited(tmp_path):
    writer = PreviewWriter(tmp_path / "preview.jpg", min_interval=10.0)
    assert writer.write(_frame(), now=100.0)
    assert not writer.write(_frame(), now=100.5)
    assert writer.write(_frame(), now=111.0)


def test_no_temporary_file_is_left_behind(tmp_path):
    writer = PreviewWriter(tmp_path / "preview.jpg")
    writer.write(_frame())
    assert writer.path.exists()
    assert not writer.path.with_name("preview.jpg.tmp").exists()

    writer.clear()
    assert not writer.path.exists()
    writer.clear()  # idempotent: a scan that never published must not raise


def test_an_empty_frame_publishes_nothing_but_keeps_going(tmp_path):
    writer = PreviewWriter(tmp_path / "preview.jpg")
    assert writer.write(np.zeros((0, 0, 3), dtype=np.uint8)) is False
    assert not writer.path.exists()
    # Not a failure, so the next real frame still gets through — and the
    # skipped frame must not have consumed the rate limit either.
    assert writer.write(_frame()) is True


def test_a_broken_frame_never_raises_and_stops_retrying(tmp_path):
    # An unwritable runtime directory: a regular file where the directory
    # should be. Stands in for any hard failure on the publish path.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    writer = PreviewWriter(blocker / "preview.jpg")
    assert writer.write(_frame()) is False
    # Latched: an unlock must not pay for a broken encoder thirty times a second.
    assert writer.write(_frame(), now=1e6) is False
