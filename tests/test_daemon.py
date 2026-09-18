"""The daemon's socket surface, with the camera and models faked out.

What is pinned here is the *shape* of the security boundary: the status socket
can never trigger a scan, an unarmed daemon never opens the camera, and a
wrong passphrase leaves it disarmed.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from glanced import ipc, paths
from glanced import store as store_module
from glanced.daemon import Daemon, Session
from glanced.pipeline import Outcome


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    runtime = tmp_path / "run"
    monkeypatch.setattr(ipc, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(ipc, "AUTH_SOCKET", runtime / "auth.sock")
    monkeypatch.setattr(ipc, "STATUS_SOCKET", runtime / "status.sock")
    monkeypatch.setattr(paths, "PASSPHRASE_FILE", tmp_path / "passphrase")
    return tmp_path


def _enrolled_store(path: Path, passphrase: str = "hunter2") -> None:
    embedding = np.zeros(512, dtype=np.float32)
    embedding[0] = 1.0
    store = store_module.EnrollmentStore(
        identities=[store_module.Identity(name="me", embeddings=embedding[None, :])]
    )
    store_module.save(store, passphrase, path)


class CameraTouched(Exception):
    pass


def _exploding_processor():
    raise CameraTouched("a scan reached the hardware")


@pytest.fixture
def daemon(scratch):
    store_path = scratch / "enrollment.bin"
    _enrolled_store(store_path)
    instance = Daemon(store_path=store_path, processor_factory=_exploding_processor)
    thread = threading.Thread(target=instance.serve, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not (ipc.AUTH_SOCKET.exists() and ipc.STATUS_SOCKET.exists()):
        assert time.monotonic() < deadline, "daemon did not come up"
        time.sleep(0.01)
    yield instance
    instance.stop()
    thread.join(timeout=3)


def test_socket_modes_keep_auth_private(daemon):
    assert os.stat(ipc.AUTH_SOCKET).st_mode & 0o777 == 0o600
    assert os.stat(ipc.STATUS_SOCKET).st_mode & 0o777 == 0o660


def test_status_socket_cannot_authenticate(daemon):
    response = ipc.request(ipc.STATUS_SOCKET, "authenticate")
    assert not response.ok
    assert daemon.last_scan is None


def test_unarmed_daemon_never_opens_the_camera(daemon):
    response = ipc.request(ipc.AUTH_SOCKET, "authenticate")
    assert not response.ok
    assert response.payload["outcome"] == Outcome.NOT_ARMED.value
    assert daemon.last_scan["outcome"] == "not_armed"


def test_wrong_passphrase_leaves_it_disarmed(daemon):
    response = ipc.request(ipc.AUTH_SOCKET, "arm", {"passphrase": "nope"})
    assert not response.ok
    assert response.payload["error"] == "wrong passphrase"
    assert ipc.request(ipc.STATUS_SOCKET, "status").payload["armed"] is False


def test_arm_exposes_identities_and_disarm_hides_them(daemon):
    armed = ipc.request(ipc.AUTH_SOCKET, "arm", {"passphrase": "hunter2"})
    assert armed.ok
    status = ipc.request(ipc.STATUS_SOCKET, "status").payload
    assert status["armed"] is True
    assert [i["name"] for i in status["identities"]] == ["me"]
    assert status["identities"][0]["captures"] == 1

    ipc.request(ipc.AUTH_SOCKET, "disarm")
    status = ipc.request(ipc.STATUS_SOCKET, "status").payload
    assert status["armed"] is False
    assert status["identities"] == []


def test_armed_scan_reaches_the_hardware_and_fails_closed(daemon):
    ipc.request(ipc.AUTH_SOCKET, "arm", {"passphrase": "hunter2"})
    response = ipc.request(ipc.AUTH_SOCKET, "authenticate")
    assert not response.ok
    assert response.payload["outcome"] == Outcome.ERROR.value
    assert "CameraTouched" in response.payload["reason"]


def test_remember_writes_a_private_file_and_rearms(daemon, scratch):
    ipc.request(ipc.AUTH_SOCKET, "arm", {"passphrase": "hunter2", "remember": True})
    assert paths.PASSPHRASE_FILE.stat().st_mode & 0o777 == 0o600

    fresh = Daemon(store_path=scratch / "enrollment.bin", processor_factory=_exploding_processor)
    assert fresh.arm_from_file() is True
    assert fresh.session.armed

    ipc.request(ipc.AUTH_SOCKET, "disarm", {"forget": True})
    assert not paths.PASSPHRASE_FILE.exists()


def test_loose_passphrase_file_is_refused(daemon, scratch):
    paths.PASSPHRASE_FILE.write_text("hunter2\n")
    os.chmod(paths.PASSPHRASE_FILE, 0o644)
    fresh = Daemon(store_path=scratch / "enrollment.bin", processor_factory=_exploding_processor)
    assert fresh.arm_from_file() is False


def test_session_idle_relock():
    session = Session(idle_timeout=0.05)
    session.unlock()
    assert session.armed
    time.sleep(0.08)
    assert not session.armed
    assert Session(idle_timeout=None).armed is False


class _Scripted:
    """A processor stand-in: the daemon's `_scan` is replaced outright, so
    this only has to exist."""


def _scripted_daemon(scratch, outcomes, **kw):
    from glanced.pipeline import ScanResult

    instance = Daemon(store_path=scratch / "enrollment.bin", processor_factory=_Scripted, **kw)
    instance.arm("hunter2")
    queue = list(outcomes)
    instance._scan = lambda: ScanResult(queue.pop(0))  # type: ignore[method-assign]
    return instance


def test_lockout_after_consecutive_failures_with_a_face(scratch):
    _enrolled_store(scratch / "enrollment.bin")
    d = _scripted_daemon(scratch, [Outcome.NO_MATCH] * 3 + [Outcome.UNLOCKED], max_failures=3, lockout_seconds=60)
    assert d.run_scan().outcome is Outcome.NO_MATCH
    assert d.run_scan().outcome is Outcome.NO_MATCH
    assert d.failures == 2
    assert d.run_scan().outcome is Outcome.NO_MATCH
    # The fourth scan would have unlocked; the lockout refuses before the camera.
    result = d.run_scan()
    assert result.outcome is Outcome.LOCKED_OUT
    assert "resumes in" in result.reason
    assert 0 < d.locked_out_for() <= 60
    assert d._status_payload()["lockedOut"] is not None
    assert d.last_scan["outcome"] == "locked_out"


def test_lockout_expires_and_unlock_resets_the_count(scratch):
    _enrolled_store(scratch / "enrollment.bin")
    d = _scripted_daemon(
        scratch,
        [Outcome.SPOOF_DENIED, Outcome.TIMED_OUT, Outcome.NO_MATCH, Outcome.UNLOCKED, Outcome.NO_MATCH],
        max_failures=2, lockout_seconds=0.05,
    )
    d.run_scan(); d.run_scan()
    assert d.run_scan().outcome is Outcome.LOCKED_OUT
    time.sleep(0.08)
    assert d.run_scan().outcome is Outcome.NO_MATCH
    assert d.run_scan().outcome is Outcome.UNLOCKED
    assert d.failures == 0
    assert d.run_scan().outcome is Outcome.NO_MATCH
    assert d.failures == 1


def test_an_empty_room_is_not_an_attempt(scratch):
    _enrolled_store(scratch / "enrollment.bin")
    d = _scripted_daemon(scratch, [Outcome.NO_FACE, Outcome.ERROR] * 5, max_failures=2)
    for _ in range(10):
        d.run_scan()
    assert d.failures == 0
    assert d.locked_out_for() == 0


def test_lockout_can_be_disabled(scratch):
    _enrolled_store(scratch / "enrollment.bin")
    d = _scripted_daemon(scratch, [Outcome.NO_MATCH] * 20, max_failures=0)
    for _ in range(20):
        assert d.run_scan().outcome is Outcome.NO_MATCH
