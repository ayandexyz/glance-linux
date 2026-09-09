"""The long-running user service.

Runs as the user, owns the camera, and answers two sockets (see `ipc.py`).
Started by systemd --user; see `packaging/systemd/glanced.service`.

Scaffold status: the socket server, scan loop shape, and session locking are
here and exercisable. The landmarker call in `_detect` is the one seam left
open — it needs a MediaPipe FaceLandmarker task file on disk, so it is isolated
behind a single method rather than threaded through the loop.
"""

from __future__ import annotations

import logging
import selectors
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from . import ipc
from .liveness import LivenessMode
from .pipeline import Outcome, UnlockPipeline
from .store import EnrollmentStore

log = logging.getLogger("glanced")


@dataclass
class Session:
    """The unwrapped encryption key's lifetime.

    Re-locks itself after an idle period, so an unattended machine does not stay
    authorized forever — the same reasoning as upstream's `SessionAutoLocker`.
    """

    idle_timeout: float = 900.0
    _key: Optional[bytes] = None
    _touched: float = 0.0

    @property
    def authorized(self) -> bool:
        if self._key is None:
            return False
        if time.monotonic() - self._touched > self.idle_timeout:
            self.lock()
            return False
        return True

    def unlock(self, key: bytes) -> None:
        self._key = key
        self._touched = time.monotonic()

    def touch(self) -> None:
        self._touched = time.monotonic()

    def lock(self) -> None:
        self._key = None


class Daemon:
    def __init__(
        self,
        store: EnrollmentStore,
        *,
        mode: LivenessMode = LivenessMode.LIGHT,
        landmarker_task: Optional[Path] = None,
    ) -> None:
        self.store = store
        self.mode = mode
        self.session = Session()
        self.landmarker_task = landmarker_task
        self._landmarker = None
        self._lock = threading.Lock()
        self._running = False

    # --- landmarking seam ---------------------------------------------------

    def _landmarker_instance(self):
        """Lazily construct the MediaPipe FaceLandmarker.

        Deliberately the only place the landmarker is touched: everything
        downstream consumes a plain (N, 2) mesh plus a yaw, so swapping
        MediaPipe for SCRFD, or for a model with a real anti-spoof head, does
        not reach into the liveness code at all.
        """
        if self._landmarker is not None:
            return self._landmarker
        if self.landmarker_task is None or not self.landmarker_task.exists():
            raise FileNotFoundError(
                "MediaPipe face_landmarker task not found. "
                "Fetch it with `glancectl fetch-model`."
            )
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision

        options = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(self.landmarker_task)),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
            # Iris landmarks give a precise eye centre, which is the
            # normalization scale for every liveness ratio.
            output_face_blendshapes=False,
            output_facial_transformation_matrixes=True,
        )
        self._landmarker = vision.FaceLandmarker.create_from_options(options)
        return self._landmarker

    # --- socket serving -----------------------------------------------------

    def serve(self) -> None:
        auth = ipc.listen(ipc.AUTH_SOCKET, 0o600)
        status = ipc.listen(ipc.STATUS_SOCKET, 0o660)
        selector = selectors.DefaultSelector()
        selector.register(auth, selectors.EVENT_READ, self._handle_auth)
        selector.register(status, selectors.EVENT_READ, self._handle_status)

        self._running = True
        log.info("listening on %s and %s", ipc.AUTH_SOCKET, ipc.STATUS_SOCKET)
        try:
            while self._running:
                for key, _ in selector.select(timeout=1.0):
                    server: socket.socket = key.fileobj  # type: ignore[assignment]
                    connection, _ = server.accept()
                    threading.Thread(
                        target=self._serve_one, args=(connection, key.data), daemon=True
                    ).start()
        finally:
            selector.close()
            auth.close()
            status.close()

    def stop(self) -> None:
        self._running = False

    def _serve_one(self, connection: socket.socket, handler) -> None:
        try:
            buffer = b""
            while not buffer.endswith(b"\n"):
                chunk = connection.recv(4096)
                if not chunk:
                    return
                buffer += chunk
            connection.sendall(handler(ipc.Request.decode(buffer)).encode())
        except Exception:
            log.exception("error serving request")
            try:
                connection.sendall(ipc.Response(False, {"error": "internal error"}).encode())
            except OSError:
                pass
        finally:
            connection.close()

    def _handle_auth(self, request: ipc.Request) -> ipc.Response:
        """The security path. Only `authenticate` is reachable here."""
        if request.verb != "authenticate":
            return ipc.Response(False, {"error": "unsupported verb"})
        # Serialized: two concurrent PAM conversations must not share a camera
        # or interleave scans.
        with self._lock:
            result = self.run_scan()
        return ipc.Response(
            result.outcome is Outcome.UNLOCKED,
            {
                "outcome": result.outcome.value,
                "identity": result.identity.name if result.identity else None,
                "reason": result.reason,
            },
        )

    def _handle_status(self, request: ipc.Request) -> ipc.Response:
        """Presentation only. Nothing here can cause or influence an unlock."""
        if request.verb != "status":
            return ipc.Response(False, {"error": "unsupported verb"})
        return ipc.Response(
            True,
            {
                "armed": self.session.authorized,
                "mode": self.mode.value,
                "identities": [
                    {"name": i.name, "enabled": i.enabled} for i in self.store.identities
                ],
            },
        )

    # --- the scan -----------------------------------------------------------

    def run_scan(self) -> "object":
        """One unlock attempt: open the camera, feed frames until the pipeline
        decides or the scan times out.

        Not yet wired to hardware — see the module docstring. The shape is fixed
        so that wiring it is a matter of filling `_detect`, not restructuring:

            pipeline.begin()
            with Camera() as camera:
                for frame in camera.frames():
                    face = self._detect(frame)
                    if face is None:
                        continue
                    result = pipeline.observe(face.liveness_frame, face.embedding)
                    if result.outcome is not Outcome.PENDING:
                        return result
        """
        raise NotImplementedError(
            "capture loop not wired: needs a landmarker task file and a camera. "
            "The decision logic it drives is complete and tested — see tests/."
        )
