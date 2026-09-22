"""Attention mode: head pose for the desktop, never for auth.

A subscriber connects to `attention.sock` and, for as long as it stays
connected, receives one JSON line per processed camera frame:

    {"schemaVersion": 1, "t": 1234.5, "state": "tracking",
     "present": true, "yaw": -12.4, "pitch": 3.1, "conf": 1.0}

`yaw` and `pitch` are degrees; yaw is positive when the head turns to the
subject's left, pitch positive when the chin comes down (the same convention as
`poses.py`, in different units). `conf` is how far the face is above the size
at which the landmarker is trusted, 0..1. `state` is one of:

* `starting` — sent once on connect, before the camera is open;
* `tracking` — the camera is open and this event carries a pose (or
  `present: false` with the pose fields null);
* `paused` — an unlock scan has the camera. The pose fields are null;
* `error` — the camera could not be opened. `reason` says why; the tracker
  retries while anyone is still listening.

Only `tracking` means anything about where the user is looking. A client
covering the screen should treat every other state, and silence, as "come
down": the point of a shield is that a crash never leaves it up.

What is deliberately *not* here:

* **No frames, no landmarks, no embeddings.** Events are three numbers and a
  bool, derived in-process. A reader looking for "does this stream my face
  anywhere" should be able to answer no from this file alone.
* **No verbs.** The socket is publish-only. Bytes a client sends are never
  read, so nothing on it can start, stop or influence a scan.
* **No ArcFace, no template, no arming.** Attention needs the landmarker and
  the pose it already computes, nothing else, so it works on a daemon that
  has never been armed for a user who has never enrolled.

The camera has one owner. The tracker holds it only while at least one
subscriber is connected — nothing listening, camera closed, LED off — and
hands it over the moment a scan asks (`paused()`), taking it back when the
scan ends. An auth request never waits on attention for more than
`HANDOVER_TIMEOUT`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

from .liveness.features import MIN_RELIABLE_INTEROCULAR_PX, interocular_distance

log = logging.getLogger("glanced.attention")

SCHEMA_VERSION = 1

#: Landmarker passes per second. A comfort feature that costs a core all day
#: is one people uninstall; at eight the light landmarker is a few percent.
DEFAULT_FPS = 8.0

#: How long a scan will wait for the tracker to release the camera before
#: trying to open it anyway. The tracker checks between frames, so at any
#: sane fps this is generous; the cap is what keeps a wedged tracker from
#: stalling a PAM conversation.
HANDOVER_TIMEOUT = 3.0

#: Back-off between attempts to reopen a camera that failed, capped so an
#: unplugged-then-replugged webcam is picked up again within a few seconds.
RETRY_MIN = 0.5
RETRY_MAX = 5.0

#: A subscriber that cannot take an event this fast is dropped, so a stuck
#: client cannot stall delivery to the others.
SEND_TIMEOUT = 0.5

#: The capture the tracker asks for. The landmarker works at 640 wide anyway
#: (`camera.WORKING_WIDTH`), so capturing at that size skips a resize per
#: frame and moves fewer bytes off the sensor.
CAPTURE_WIDTH = 640
CAPTURE_HEIGHT = 360


@dataclass(frozen=True)
class Event:
    state: str
    present: Optional[bool] = None
    yaw: Optional[float] = None
    pitch: Optional[float] = None
    conf: Optional[float] = None
    reason: Optional[str] = None

    def encode(self, now: Optional[float] = None) -> bytes:
        payload: dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "t": round(time.monotonic() if now is None else now, 3),
            "state": self.state,
            "present": self.present,
            "yaw": None if self.yaw is None else round(self.yaw, 1),
            "pitch": None if self.pitch is None else round(self.pitch, 1),
            "conf": None if self.conf is None else round(self.conf, 2),
        }
        if self.reason:
            payload["reason"] = self.reason
        return json.dumps(payload).encode() + b"\n"


ABSENT = Event("tracking", present=False)


def pose_event(face: Any) -> Event:
    """A `tracking` event from a `landmarker.DetectedFace`."""
    iod = interocular_distance(face.mesh)
    conf = 0.0 if iod is None else min(1.0, iod / MIN_RELIABLE_INTEROCULAR_PX)
    return Event(
        "tracking",
        present=True,
        yaw=None if face.yaw is None else math.degrees(face.yaw),
        pitch=None if face.pitch is None else math.degrees(face.pitch),
        conf=conf,
    )


class AttentionTracker:
    """Owns the attention loop, its subscribers, and the camera handover."""

    def __init__(
        self,
        *,
        device: str = "/dev/video0",
        fps: float = DEFAULT_FPS,
        landmarker_factory: Optional[Callable[[], Any]] = None,
        camera_factory: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.device = device
        self.fps = fps
        self._landmarker_factory = landmarker_factory or self._default_landmarker
        self._camera_factory = camera_factory or self._default_camera
        self._landmarker = None
        self._subscribers: list[socket.socket] = []
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._pauses = 0
        self._closing = False
        self._tracking = False
        # Set whenever the loop is *not* holding the camera.
        self._camera_free = threading.Event()
        self._camera_free.set()
        self._thread: Optional[threading.Thread] = None

    # --- subscribers --------------------------------------------------------

    def subscribe(self, connection: socket.socket) -> None:
        """Adopt a connection as a subscriber. Never reads from it."""
        connection.settimeout(SEND_TIMEOUT)
        with contextlib.suppress(OSError):
            # Nothing a client sends is ever read; refuse it at the socket so
            # the kernel does not buffer it either.
            connection.shutdown(socket.SHUT_RD)
        with self._changed:
            if self._closing:
                connection.close()
                return
            first = Event("paused", reason="unlock scan in progress") if self._pauses else Event("starting")
            try:
                connection.sendall(first.encode())
            except OSError as error:
                log.info("attention subscriber dropped before its first event: %s", error)
                connection.close()
                return
            self._subscribers.append(connection)
            log.info("attention subscriber connected (%d)", len(self._subscribers))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="attention", daemon=True)
                self._thread.start()
            self._changed.notify_all()

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def _publish(self, event: Event) -> None:
        line = event.encode()
        with self._changed:
            kept = []
            for connection in self._subscribers:
                try:
                    connection.sendall(line)
                    kept.append(connection)
                except OSError:
                    connection.close()
            if len(kept) != len(self._subscribers):
                log.info("attention subscriber left (%d)", len(kept))
                self._subscribers = kept
                self._changed.notify_all()

    # --- camera handover ----------------------------------------------------

    @contextlib.contextmanager
    def paused(self) -> Iterator[None]:
        """Take the camera away from the tracker for the duration.

        Returns once the tracker has closed the device, or after
        `HANDOVER_TIMEOUT` with a warning — a scan must never hang on this.
        """
        with self._changed:
            self._pauses += 1
            self._changed.notify_all()
        try:
            if not self._camera_free.wait(HANDOVER_TIMEOUT):
                log.warning("attention tracker did not release the camera in %.1fs", HANDOVER_TIMEOUT)
            yield
        finally:
            with self._changed:
                self._pauses -= 1
                self._changed.notify_all()

    # --- the loop -----------------------------------------------------------

    def _wanted(self) -> bool:
        """Should the loop hold the camera right now? Caller holds the lock."""
        return bool(self._subscribers) and not self._pauses and not self._closing

    def _run(self) -> None:
        retry = RETRY_MIN
        while True:
            with self._changed:
                while not self._wanted():
                    if self._closing or not self._subscribers:
                        return
                    self._changed.wait()
                # Claimed under the lock, so a `paused()` that lands now either
                # sees the claim and waits, or was seen by `_wanted()` first.
                self._camera_free.clear()
            failed = False
            try:
                self._track()
                retry = RETRY_MIN
            except Exception as error:
                failed = True
                log.warning("attention camera unavailable: %s", error)
                self._publish(Event("error", reason=f"{type(error).__name__}: {error}"))
            finally:
                self._tracking = False
                self._camera_free.set()
            with self._lock:
                paused = self._pauses > 0
                wanted = bool(self._subscribers) and not self._closing
            if not wanted:
                return
            if paused:
                self._publish(Event("paused", reason="unlock scan in progress"))
                continue
            if failed:
                # Wait before reopening, but wake early for a pause or close.
                with self._changed:
                    self._changed.wait(retry)
                retry = min(retry * 2, RETRY_MAX)

    def _track(self) -> None:
        """Hold the camera and publish until no longer wanted. Raises when the
        device cannot be opened or stops delivering."""
        from .camera import to_working_resolution

        if self._landmarker is None:
            self._landmarker = self._landmarker_factory()
        interval = 1.0 / self.fps if self.fps > 0 else 0.0
        with self._camera_factory(self.device) as camera:
            log.info("attention tracking on %s at %.0f fps", self.device, self.fps)
            self._tracking = True
            for native in camera.frames(interval):
                with self._lock:
                    if not self._wanted():
                        return
                working, _ = to_working_resolution(native)
                face = self._landmarker.detect(working, int(time.monotonic() * 1000))
                self._publish(ABSENT if face is None else pose_event(face))
        raise RuntimeError("camera stopped delivering frames")

    # --- lifecycle ----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": True,
                "subscribers": len(self._subscribers),
                "tracking": self._tracking,
                "fps": self.fps,
            }

    def close(self) -> None:
        with self._changed:
            self._closing = True
            for connection in self._subscribers:
                connection.close()
            self._subscribers = []
            self._changed.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=HANDOVER_TIMEOUT)
        if self._landmarker is not None:
            self._landmarker.close()
            self._landmarker = None

    # --- defaults -----------------------------------------------------------

    @staticmethod
    def _default_landmarker():
        from .landmarker import Landmarker

        return Landmarker()

    @staticmethod
    def _default_camera(device: str):
        from .camera import Camera, CameraConfig

        return Camera(CameraConfig(device=device, width=CAPTURE_WIDTH, height=CAPTURE_HEIGHT))


class DisabledTracker:
    """What the daemon holds with `--no-attention`: no socket, no thread, and
    a `paused()` that costs nothing."""

    subscribers = 0

    def subscribe(self, connection: socket.socket) -> None:
        connection.close()

    @contextlib.contextmanager
    def paused(self) -> Iterator[None]:
        yield

    def status(self) -> dict[str, Any]:
        return {"enabled": False, "subscribers": 0, "tracking": False, "fps": None}

    def close(self) -> None:
        pass


def subscribe(path, on_event: Callable[[dict[str, Any]], bool]) -> None:
    """Client side: connect and call `on_event` per event until it returns
    False or the daemon goes away. What `glancectl attention` uses."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(str(path))
        buffer = b""
        while True:
            chunk = client.recv(4096)
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if line and not on_event(json.loads(line)):
                    return
    finally:
        client.close()
