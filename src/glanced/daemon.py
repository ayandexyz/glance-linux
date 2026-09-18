"""The long-running user service.

Runs as the user, owns the camera, and answers two sockets (see `ipc.py`).
Started by systemd --user; see `packaging/systemd/glanced.service`.

The daemon starts *disarmed*: the enrollment store is encrypted at rest and it
has no key. `glancectl arm` (or a remembered passphrase, see `paths.py`)
decrypts it into memory, and only an armed daemon will run a scan. Disarming
drops the identities again. That is the whole of the session model — there is
no stored login password to protect, because PAM does the authorizing.
"""

from __future__ import annotations

import logging
import selectors
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import ipc, locksetup, models, pamsetup, paths
from .preview import PreviewWriter
from .liveness import LivenessMode
from .pipeline import DEFAULT_SCAN_TIMEOUT, Outcome, ScanResult, UnlockPipeline
from .store import EnrollmentStore, load as load_store

log = logging.getLogger("glanced")

#: Give up this early when nobody is in front of the camera. A PAM stack runs
#: this module *before* the password one, so an absent face must not make a
#: typed password wait the full scan timeout.
DEFAULT_NO_FACE_TIMEOUT = 3.0

#: Consecutive failed scans *with a face in view* before the daemon refuses to
#: scan for a while. A hands-free lock screen rescans in a loop, which would
#: otherwise hand a photo or a video unlimited free attempts. An empty room
#: does not count: no face, a broken camera or a disarmed daemon is not an
#: attempt by anyone.
DEFAULT_MAX_FAILURES = 5
DEFAULT_LOCKOUT_SECONDS = 300.0

#: Outcomes that count as a failed attempt by someone who was there.
_FAILED_ATTEMPT = frozenset({Outcome.NO_MATCH, Outcome.SPOOF_DENIED, Outcome.TIMED_OUT})


@dataclass
class Session:
    """How long a decrypted enrollment stays in memory.

    `idle_timeout` re-locks after a period with no scans, so an unattended
    machine does not stay armed forever — the same reasoning as upstream's
    `SessionAutoLocker`. None means never, which is the default: a lock screen
    that forgets how to unlock you after twenty idle minutes is not useful.
    """

    idle_timeout: Optional[float] = None
    _armed: bool = False
    _touched: float = field(default=0.0, repr=False)

    @property
    def armed(self) -> bool:
        if not self._armed:
            return False
        if self.idle_timeout is not None and time.monotonic() - self._touched > self.idle_timeout:
            self.lock()
            return False
        return True

    def unlock(self) -> None:
        self._armed = True
        self._touched = time.monotonic()

    def touch(self) -> None:
        self._touched = time.monotonic()

    def lock(self) -> None:
        self._armed = False


class Daemon:
    def __init__(
        self,
        *,
        mode: LivenessMode = LivenessMode.LIGHT,
        device: str = "/dev/video0",
        store_path: Path = paths.STORE_PATH,
        scan_timeout: float = DEFAULT_SCAN_TIMEOUT,
        no_face_timeout: float = DEFAULT_NO_FACE_TIMEOUT,
        relock_after: Optional[float] = None,
        preview: bool = True,
        max_failures: int = DEFAULT_MAX_FAILURES,
        lockout_seconds: float = DEFAULT_LOCKOUT_SECONDS,
        processor_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.mode = mode
        self.device = device
        self.store_path = Path(store_path)
        self.scan_timeout = scan_timeout
        self.no_face_timeout = no_face_timeout
        # Publishes camera frames for the lock screen's indicator to draw. Off
        # means no frame ever leaves the process; see preview.py.
        self.preview = preview
        self.session = Session(idle_timeout=relock_after)
        self.store = EnrollmentStore()
        self.last_scan: Optional[dict[str, Any]] = None
        self.scanning = False
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self.failures = 0
        self._locked_until = 0.0
        self._processor = None
        self._processor_factory = processor_factory or self._default_processor
        self._lock = threading.Lock()
        self._running = False
        self._servers: list[socket.socket] = []

    # --- arming -------------------------------------------------------------

    def arm(self, passphrase: str) -> None:
        """Decrypt the enrollment into memory. Raises on a wrong passphrase."""
        store = load_store(passphrase, self.store_path)
        with self._lock:  # never swap the store out from under a running scan
            self.store = store
            self.session.unlock()
        log.info("armed with %d identit%s", len(store.identities), "y" if len(store.identities) == 1 else "ies")

    def disarm(self) -> None:
        with self._lock:
            self.store = EnrollmentStore()
            self.session.lock()
        log.info("disarmed")

    def arm_from_file(self, path: Optional[Path] = None) -> bool:
        """Best-effort arm at startup from a remembered passphrase."""
        path = path or paths.PASSPHRASE_FILE
        if not path.exists():
            return False
        if path.stat().st_mode & 0o077:
            log.warning("ignoring %s: not mode 0600", path)
            return False
        try:
            self.arm(path.read_text().rstrip("\n"))
            return True
        except Exception as error:  # wrong passphrase, unreadable store, ...
            log.warning("could not arm from %s: %s", path, error)
            return False

    # --- serving ------------------------------------------------------------

    def serve(self) -> None:
        auth = ipc.listen(ipc.AUTH_SOCKET, 0o600)
        status = ipc.listen(ipc.STATUS_SOCKET, 0o660)
        self._servers = [auth, status]
        selector = selectors.DefaultSelector()
        selector.register(auth, selectors.EVENT_READ, self._handle_auth)
        selector.register(status, selectors.EVENT_READ, self._handle_status)

        # A daemon killed mid-scan leaves its last frame behind; drop it before
        # anything can read a picture of a face from a previous session.
        PreviewWriter().clear()

        self._running = True
        log.info("listening on %s and %s", ipc.AUTH_SOCKET, ipc.STATUS_SOCKET)
        try:
            while self._running:
                for key, _ in selector.select(timeout=1.0):
                    server: socket.socket = key.fileobj  # type: ignore[assignment]
                    try:
                        connection, _ = server.accept()
                    except OSError:
                        continue
                    threading.Thread(
                        target=self._serve_one, args=(connection, key.data), daemon=True
                    ).start()
        finally:
            selector.close()
            for server in self._servers:
                server.close()
            if self._processor is not None:
                self._processor.close()

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
        """The security path: authenticate, and the arming that gates it."""
        if request.verb == "authenticate":
            # Serialized: two concurrent PAM conversations must not share a
            # camera or interleave scans.
            with self._lock:
                result = self.run_scan()
            return ipc.Response(result.outcome is Outcome.UNLOCKED, _describe(result))

        if request.verb == "arm":
            passphrase = request.payload.get("passphrase")
            if not isinstance(passphrase, str) or not passphrase:
                return ipc.Response(False, {"error": "passphrase required"})
            try:
                self.arm(passphrase)
            except FileNotFoundError:
                return ipc.Response(False, {"error": "nothing enrolled yet"})
            except Exception:
                # cryptography's InvalidTag, a truncated file — from the
                # client's point of view all "the passphrase did not open it".
                return ipc.Response(False, {"error": "wrong passphrase"})
            if request.payload.get("remember"):
                _remember(passphrase)
            return ipc.Response(True, self._status_payload())

        if request.verb == "disarm":
            self.disarm()
            if request.payload.get("forget"):
                paths.PASSPHRASE_FILE.unlink(missing_ok=True)
            return ipc.Response(True, self._status_payload())

        return ipc.Response(False, {"error": "unsupported verb"})

    def _handle_status(self, request: ipc.Request) -> ipc.Response:
        """Presentation only. Nothing here can cause or influence an unlock."""
        if request.verb != "status":
            return ipc.Response(False, {"error": "unsupported verb"})
        return ipc.Response(True, self._status_payload())

    def _status_payload(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "armed": self.session.armed,
            "mode": self.mode.value,
            "scanning": self.scanning,
            "failures": self.failures,
            "lockedOut": round(self.locked_out_for(), 1) or None,
            "enrolled": self.store_path.exists(),
            "remembered": paths.PASSPHRASE_FILE.exists(),
            "camera": self.device,
            "preview": self.preview,
            "models": models.status(),
            "pam": pamsetup.status(),
            "lock": locksetup.status(),
            "identities": [
                {"name": i.name, "enabled": i.enabled, "captures": int(len(i.embeddings))}
                for i in self.store.identities
            ],
            "lastScan": self.last_scan,
        }

    # --- the scan -----------------------------------------------------------

    def _default_processor(self):
        from .scan import FaceProcessor

        return FaceProcessor()

    def _get_processor(self):
        if self._processor is None:
            self._processor = self._processor_factory()
        return self._processor

    def run_scan(self) -> ScanResult:
        """One unlock attempt: open the camera, feed frames until the pipeline
        decides or the scan times out. Records the outcome for the status
        socket, and never raises — a broken camera is a failed scan."""
        started = time.monotonic()
        remaining = self.locked_out_for()
        if remaining > 0:
            result = ScanResult(
                Outcome.LOCKED_OUT,
                reason=f"{self.max_failures} failed scans in a row; face unlock resumes in {int(remaining) + 1}s.",
            )
        else:
            self.scanning = True
            try:
                result = self._scan()
            except Exception as error:
                log.exception("scan failed")
                result = ScanResult(Outcome.ERROR, reason=f"{type(error).__name__}: {error}")
            finally:
                self.scanning = False
            self._count(result)

        self.last_scan = {**_describe(result), "at": time.time(), "duration": round(time.monotonic() - started, 2)}
        log.info("scan: %s%s", result.outcome.value, f" ({result.reason})" if result.reason else "")
        if result.outcome is Outcome.UNLOCKED:
            self.session.touch()
        return result

    def locked_out_for(self) -> float:
        """Seconds until the daemon will scan again; 0 when it will now."""
        return max(0.0, self._locked_until - time.monotonic())

    def _count(self, result: ScanResult) -> None:
        if result.outcome is Outcome.UNLOCKED:
            self.failures = 0
        elif result.outcome in _FAILED_ATTEMPT and self.max_failures > 0:
            self.failures += 1
            if self.failures >= self.max_failures:
                self._locked_until = time.monotonic() + self.lockout_seconds
                self.failures = 0
                log.warning("%d failed scans in a row; refusing to scan for %.0fs", self.max_failures, self.lockout_seconds)

    def _scan(self) -> ScanResult:
        if not self.session.armed:
            return ScanResult(Outcome.NOT_ARMED, reason="Daemon is not armed. Run `glancectl arm`.")
        if not any(i.enabled for i in self.store.identities):
            return ScanResult(Outcome.NO_MATCH, reason="No enabled identities are enrolled.")

        from .camera import Camera, CameraConfig

        processor = self._get_processor()
        pipeline = UnlockPipeline(self.store, mode=self.mode, scan_timeout=self.scan_timeout)
        pipeline.begin()
        started = time.monotonic()
        saw_face = False
        preview = PreviewWriter() if self.preview else None

        try:
            with Camera(CameraConfig(device=self.device)) as camera:
                for native in camera.frames():
                    now = time.monotonic()
                    observation = processor.process(native, now, want_embedding=not pipeline.matched)
                    if preview is not None:
                        # Published either way: with no face detected the crop
                        # falls back to the middle of the frame, which is what
                        # lets someone see themselves and move into view.
                        preview.write(
                            native, observation.native_bounding_box if observation else None, now
                        )
                    if observation is None:
                        if not saw_face and now - started > self.no_face_timeout:
                            return ScanResult(Outcome.NO_FACE, reason="No face in view.")
                        expired = pipeline.expired()
                        if expired is not None:
                            return expired
                        continue
                    saw_face = True
                    result = pipeline.observe(observation.liveness_frame, observation.embedding)
                    if result.outcome is not Outcome.PENDING:
                        return result

            return ScanResult(Outcome.ERROR, reason="Camera stopped delivering frames.")
        finally:
            # The frame outlives the scan by no more than this: the indicator
            # has already switched to its verdict by the time it notices.
            if preview is not None:
                preview.clear()


def _describe(result: ScanResult) -> dict[str, Any]:
    return {
        "outcome": result.outcome.value,
        "identity": result.identity.name if result.identity else None,
        "similarity": None if result.similarity is None else round(float(result.similarity), 3),
        "reason": result.reason,
    }


def _remember(passphrase: str) -> None:
    import os

    paths.PASSPHRASE_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(paths.PASSPHRASE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(passphrase + "\n")
