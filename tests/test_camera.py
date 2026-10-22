"""The camera reader thread, against a fake capture that behaves like a V4L2
driver which gives up on a client that does not drain its queue (the Apple ISP
driver on Asahi Linux does exactly this: a `select()` timeout a few seconds
into a slow HEAVY scan, and no frames after it)."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

pytest.importorskip("cv2")

from glanced.camera import Camera, CameraConfig  # noqa: E402

PERIOD = 0.01  # a 100 fps fake keeps the tests short


class StallingCapture:
    """Frames appear every `period` seconds whether or not anyone reads them.
    A client more than `depth` frames behind stalls the device for good."""

    def __init__(self, period=PERIOD, depth=4, select_timeout=0.2, last_frame=None, hang_after=None,
                 hang_for=0.6):
        self.period, self.depth, self.select_timeout = period, depth, select_timeout
        self.last_frame, self.hang_after, self.hang_for = last_frame, hang_after, hang_for
        self.t0 = time.monotonic()
        self.dequeued = 0
        self.retrieves = 0
        self.stalled = False
        self.released = threading.Event()
        self.grab_threads: set[str] = set()

    def isOpened(self):
        return True

    def set(self, *_):
        return True

    def produced(self) -> int:
        return int((time.monotonic() - self.t0) / self.period)

    def grab(self) -> bool:
        self.grab_threads.add(threading.current_thread().name)
        if self.hang_after is not None and self.dequeued >= self.hang_after:
            time.sleep(self.hang_for)
            return False
        if self.last_frame is not None and self.dequeued >= self.last_frame:
            return False
        if self.stalled or self.produced() - self.dequeued > self.depth:
            self.stalled = True
            time.sleep(self.select_timeout)
            return False
        while self.produced() <= self.dequeued:
            time.sleep(self.period / 4)
        self.dequeued += 1
        return True

    def retrieve(self):
        self.retrieves += 1
        return True, np.full((4, 6, 3), self.dequeued, dtype=np.uint16)

    def release(self):
        self.released.set()


def camera(fake, **kwargs) -> Camera:
    return Camera(CameraConfig(device="/dev/fake"), capture_factory=lambda device: fake, **kwargs)


def test_the_fake_stalls_a_consumer_that_reads_inline():
    """What 0.3.6 did: grab and decode in the consumer's own loop."""
    fake = StallingCapture()
    got = 0
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and fake.grab():
        fake.retrieve()
        got += 1
        time.sleep(PERIOD * 6)  # a HEAVY scan frame, slower than the camera
    assert fake.stalled


def test_a_slow_consumer_keeps_getting_frames_and_only_the_newest():
    fake = StallingCapture()
    seen = []
    with camera(fake) as cam:
        started = time.monotonic()
        for rgb in cam.frames():
            index = int(rgb[0, 0, 0])
            assert fake.dequeued - index <= 2, "a frame from the back of the queue, not the newest"
            seen.append(index)
            time.sleep(PERIOD * 6)
            if time.monotonic() - started > 0.8:
                break
    assert not fake.stalled
    assert len(seen) >= 8
    assert seen == sorted(seen) and len(set(seen)) == len(seen)
    assert seen[-1] - seen[0] > len(seen), "frames in between were dropped, not queued"


def test_capture_runs_on_the_reader_thread():
    fake = StallingCapture()
    with camera(fake) as cam:
        next(cam.frames())
    assert fake.grab_threads == {"glance-camera"}


def test_min_interval_throttles_and_decodes_only_what_it_yields():
    fake = StallingCapture()
    yielded = 0
    with camera(fake) as cam:
        started = time.monotonic()
        for _ in cam.frames(min_interval=0.05):
            yielded += 1
            if time.monotonic() - started > 0.5:
                break
    assert 7 <= yielded <= 12
    assert fake.retrieves == yielded
    assert fake.dequeued > 3 * yielded, "the reader keeps draining between yields"
    assert not fake.stalled


def test_the_end_of_the_stream_ends_the_generator():
    fake = StallingCapture(last_frame=3)
    with camera(fake) as cam:
        started = time.monotonic()
        frames = list(cam.frames())
    assert 1 <= len(frames) <= 3
    assert time.monotonic() - started < 1.0


def test_no_frame_within_the_stall_timeout_ends_the_generator_and_close_does_not_hang():
    fake = StallingCapture(hang_after=2, hang_for=0.6)
    cam = camera(fake, stall_timeout=0.2, close_timeout=0.05)
    cam.open()
    started = time.monotonic()
    frames = list(cam.frames())
    assert len(frames) <= 2
    assert time.monotonic() - started < 0.5
    cam.close()
    assert time.monotonic() - started < 0.5, "close must not wait out a blocked grab"
    # The reader releases the device itself once its grab returns.
    assert fake.released.wait(2.0)


def test_close_joins_the_reader_and_releases_the_device():
    fake = StallingCapture()
    cam = camera(fake)
    cam.open()
    reader = cam._reader
    next(cam.frames())
    cam.close()
    assert reader is not None and not reader.is_alive()
    assert fake.released.is_set()


def test_frames_needs_an_open_camera():
    with pytest.raises(RuntimeError, match="not open"):
        next(camera(StallingCapture()).frames())


def test_a_device_that_will_not_open_is_an_error():
    class Closed(StallingCapture):
        def isOpened(self):
            return False

    with pytest.raises(RuntimeError, match="could not open /dev/fake"):
        camera(Closed()).open()


def test_the_camera_can_be_reopened():
    fake = StallingCapture()
    cam = camera(fake)
    for _ in range(2):
        with cam:
            assert next(cam.frames()) is not None
