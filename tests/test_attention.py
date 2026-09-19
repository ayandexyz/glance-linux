"""Attention mode, with the camera and the landmarker faked out.

Pinned here: the attention socket is publish-only and can never reach a scan;
events carry derived numbers and nothing else; the camera is held only while
someone is listening; an unlock scan takes it away and gets it back; and every
failure shows up as a non-`tracking` state so a client can fail open.
"""

from __future__ import annotations

import json
import math
import os
import socket
import threading
import time

import numpy as np
import pytest

from glanced import attention as attention_module
from glanced import ipc, paths
from glanced import store as store_module
from glanced.attention import AttentionTracker, Event, pose_event
from glanced.daemon import Daemon
from glanced.landmarker import DetectedFace
from glanced.liveness.features import MIN_RELIABLE_INTEROCULAR_PX

FRAME = np.zeros((360, 640, 3), dtype=np.uint8)


def _enrolled_store(path, passphrase: str = "hunter2") -> None:
    embedding = np.zeros(512, dtype=np.float32)
    embedding[0] = 1.0
    store = store_module.EnrollmentStore(
        identities=[store_module.Identity(name="me", embeddings=embedding[None, :])]
    )
    store_module.save(store, passphrase, path)


def _face(yaw_deg: float = 0.0, pitch_deg: float = 0.0, iod: float = 60.0) -> DetectedFace:
    # A 478-point mesh carries irises, and the eye centre comes from them.
    mesh = np.zeros((478, 2))
    mesh[468:473] = (100.0, 100.0)
    mesh[473:478] = (100.0 + iod, 100.0)
    return DetectedFace(
        mesh=mesh,
        bounding_box=(80.0, 60.0, 120.0, 150.0),
        yaw=math.radians(yaw_deg),
        pitch=math.radians(pitch_deg),
    )


class FakeCamera:
    """Delivers frames forever, and records every open and close."""

    opened = 0
    closed = 0
    open_now = 0
    fail_next = 0

    def __init__(self, device: str) -> None:
        self.device = device

    def __enter__(self):
        if FakeCamera.fail_next:
            FakeCamera.fail_next -= 1
            raise RuntimeError("could not open " + self.device)
        FakeCamera.opened += 1
        FakeCamera.open_now += 1
        return self

    def __exit__(self, *exc):
        FakeCamera.closed += 1
        FakeCamera.open_now -= 1

    def frames(self, min_interval: float = 0.0):
        while True:
            time.sleep(min_interval)
            yield FRAME


class FakeLandmarker:
    def __init__(self) -> None:
        self.next: DetectedFace | None = _face(yaw_deg=-20.0, pitch_deg=5.0)
        self.frames = 0
        self.closed = False

    def detect(self, frame, timestamp_ms):
        self.frames += 1
        assert frame.shape[1] <= 640, "attention must feed the landmarker working resolution"
        return self.next

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def fresh_camera(monkeypatch):
    FakeCamera.opened = FakeCamera.closed = FakeCamera.open_now = FakeCamera.fail_next = 0
    # Keep the suite fast: the scan side never waits long for a handover.
    monkeypatch.setattr(attention_module, "RETRY_MIN", 0.05)
    monkeypatch.setattr(attention_module, "RETRY_MAX", 0.1)


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    runtime = tmp_path / "run"
    monkeypatch.setattr(ipc, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(ipc, "AUTH_SOCKET", runtime / "auth.sock")
    monkeypatch.setattr(ipc, "STATUS_SOCKET", runtime / "status.sock")
    monkeypatch.setattr(ipc, "ATTENTION_SOCKET", runtime / "attention.sock")
    monkeypatch.setattr(paths, "PASSPHRASE_FILE", tmp_path / "passphrase")
    return tmp_path


class ScanCamera:
    """What the unlock scan sees: a camera that must be free to open."""

    def __init__(self, landmarker: FakeLandmarker) -> None:
        self.landmarker = landmarker
        self.saw_camera_free = None

    def process(self, native, now, want_embedding=True):
        raise RuntimeError("scan reached the processor")

    def close(self) -> None:
        pass


def _daemon(scratch, landmarker: FakeLandmarker, **kwargs) -> tuple[Daemon, threading.Thread]:
    instance = Daemon(
        store_path=scratch / "enrollment.bin",
        landmarker_factory=lambda: landmarker,
        camera_factory=FakeCamera,
        attention_fps=50.0,
        processor_factory=lambda: ScanCamera(landmarker),
        **kwargs,
    )
    thread = threading.Thread(target=instance.serve, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not (ipc.AUTH_SOCKET.exists() and ipc.STATUS_SOCKET.exists()):
        assert time.monotonic() < deadline, "daemon did not come up"
        time.sleep(0.01)
    return instance, thread


@pytest.fixture
def landmarker():
    return FakeLandmarker()


@pytest.fixture
def daemon(scratch, landmarker):
    instance, thread = _daemon(scratch, landmarker)
    _wait(lambda: ipc.ATTENTION_SOCKET.exists())
    yield instance
    instance.stop()
    thread.join(timeout=3)


def _wait(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.01)


class Subscriber:
    def __init__(self, path=None) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(3.0)
        self.sock.connect(str(path or ipc.ATTENTION_SOCKET))
        self._buffer = b""

    def event(self) -> dict:
        while b"\n" not in self._buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EOFError
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return json.loads(line)

    def until(self, state: str, timeout: float = 3.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self.event()
            if event["state"] == state:
                return event
        raise AssertionError(f"no {state} event")

    def close(self) -> None:
        self.sock.close()


# --- the socket boundary ------------------------------------------------------


def test_attention_socket_is_group_readable_like_status(daemon):
    assert os.stat(ipc.ATTENTION_SOCKET).st_mode & 0o777 == 0o660


def test_attention_socket_ignores_anything_sent_to_it(daemon, landmarker):
    client = Subscriber()
    # The first event proves the daemon has adopted the connection, and with
    # it shut the read side. From here a send either lands in a buffer nobody
    # reads or is refused with EPIPE — which of the two depends on the
    # kernel's timing, and both are the point.
    assert client.event()["state"] == "starting"
    refused = 0
    for verb in ("authenticate", "arm", "status"):
        try:
            client.sock.sendall(ipc.Request(verb, {"passphrase": "hunter2"}).encode())
        except (BrokenPipeError, ConnectionResetError):
            refused += 1
    client.until("tracking")
    assert daemon.last_scan is None
    assert daemon.session.armed is False
    client.close()


def test_events_carry_only_derived_values(daemon):
    client = Subscriber()
    event = client.until("tracking")
    assert set(event) == {"schemaVersion", "t", "state", "present", "yaw", "pitch", "conf"}
    assert event["present"] is True
    assert event["yaw"] == pytest.approx(-20.0, abs=0.1)
    assert event["pitch"] == pytest.approx(5.0, abs=0.1)
    assert event["conf"] == 1.0
    client.close()


def test_first_event_says_starting(daemon):
    client = Subscriber()
    assert client.event()["state"] == "starting"
    client.close()


# --- camera ownership ---------------------------------------------------------


def test_camera_opens_on_first_subscriber_and_closes_on_last(daemon):
    assert FakeCamera.opened == 0
    assert daemon.attention.status()["tracking"] is False

    first = Subscriber()
    first.until("tracking")
    second = Subscriber()
    second.until("tracking")
    assert FakeCamera.opened == 1 and FakeCamera.open_now == 1
    assert daemon.attention.status() == {"enabled": True, "subscribers": 2, "tracking": True, "fps": 50.0}

    first.close()
    second.until("tracking")  # still flowing for the one left
    assert FakeCamera.open_now == 1

    second.close()
    _wait(lambda: FakeCamera.open_now == 0)
    _wait(lambda: daemon.attention.subscribers == 0)
    assert daemon.attention.status()["tracking"] is False


def test_no_face_is_still_a_tracking_event(daemon, landmarker):
    landmarker.next = None
    client = Subscriber()
    event = client.until("tracking")
    assert event["present"] is False
    assert event["yaw"] is None and event["pitch"] is None and event["conf"] is None
    client.close()


def test_scan_preempts_attention_and_attention_resumes(daemon, landmarker, scratch):
    _enrolled_store(scratch / "enrollment.bin")
    ipc.request(ipc.AUTH_SOCKET, "arm", {"passphrase": "hunter2"})

    client = Subscriber()
    client.until("tracking")
    assert FakeCamera.open_now == 1

    seen_free = []
    original_scan = daemon._scan

    def scan_with_camera_check():
        seen_free.append(FakeCamera.open_now)
        return original_scan()

    daemon._scan = scan_with_camera_check
    response = ipc.request(ipc.AUTH_SOCKET, "authenticate")
    assert seen_free == [0], "the scan ran while attention still held the camera"
    assert response.payload["outcome"] == "error"  # the fake processor explodes; the point is the handover

    paused = client.until("paused")
    assert paused["present"] is None
    client.until("tracking")
    _wait(lambda: FakeCamera.open_now == 1)
    client.close()


def test_unarmed_scan_still_pauses_attention(daemon):
    client = Subscriber()
    client.until("tracking")
    ipc.request(ipc.AUTH_SOCKET, "authenticate")
    client.until("paused")
    client.until("tracking")
    client.close()


def test_camera_failure_is_reported_and_retried(daemon):
    FakeCamera.fail_next = 2
    client = Subscriber()
    error = client.until("error")
    assert "could not open" in error["reason"]
    client.until("tracking")
    assert FakeCamera.opened == 1
    client.close()


def test_daemon_stop_closes_subscribers_and_landmarker(scratch, landmarker):
    instance, thread = _daemon(scratch, landmarker)
    _wait(lambda: ipc.ATTENTION_SOCKET.exists())
    client = Subscriber()
    client.until("tracking")
    instance.stop()
    thread.join(timeout=3)
    with pytest.raises(EOFError):
        while True:
            client.event()
    assert landmarker.closed is True
    assert FakeCamera.open_now == 0


def test_no_attention_flag_offers_no_socket(scratch, landmarker):
    instance, thread = _daemon(scratch, landmarker, attention=False)
    try:
        time.sleep(0.05)
        assert not ipc.ATTENTION_SOCKET.exists()
        status = ipc.request(ipc.STATUS_SOCKET, "status").payload
        assert status["attention"] == {"enabled": False, "subscribers": 0, "tracking": False, "fps": None}
        # And the scan path still works with the null tracker in place.
        assert ipc.request(ipc.AUTH_SOCKET, "authenticate").payload["outcome"] == "not_armed"
    finally:
        instance.stop()
        thread.join(timeout=3)


def test_slow_subscriber_is_dropped_not_waited_for(daemon, monkeypatch):
    slow = Subscriber()
    slow.until("tracking")
    slow.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
    # Stop reading; the daemon's send buffer fills and it must give up on us.
    fast = Subscriber()
    for _ in range(50):
        fast.until("tracking")
    _wait(lambda: daemon.attention.subscribers == 1, timeout=10.0)
    fast.close()
    slow.close()


# --- pure pieces --------------------------------------------------------------


def test_pose_event_converts_to_degrees_and_scales_confidence():
    event = pose_event(_face(yaw_deg=30.0, pitch_deg=-10.0, iod=MIN_RELIABLE_INTEROCULAR_PX / 2))
    assert event.yaw == pytest.approx(30.0)
    assert event.pitch == pytest.approx(-10.0)
    assert event.conf == pytest.approx(0.5)
    assert pose_event(_face(iod=200.0)).conf == 1.0


def test_event_encoding_is_one_json_line():
    line = Event("error", reason="boom").encode(now=12.3456)
    assert line.endswith(b"\n")
    data = json.loads(line)
    assert data == {
        "schemaVersion": 1, "t": 12.346, "state": "error",
        "present": None, "yaw": None, "pitch": None, "conf": None, "reason": "boom",
    }


def test_paused_never_blocks_longer_than_the_handover_timeout(monkeypatch):
    monkeypatch.setattr(attention_module, "HANDOVER_TIMEOUT", 0.05)
    tracker = AttentionTracker(landmarker_factory=FakeLandmarker, camera_factory=FakeCamera)
    tracker._camera_free.clear()  # a wedged loop that never gives the camera back
    started = time.monotonic()
    with tracker.paused():
        pass
    assert time.monotonic() - started < 1.0
