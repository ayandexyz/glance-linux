"""Unix-socket protocol between the daemon and its clients.

Two very different clients, with very different trust:

* **pam_glance** asks for an authentication verdict. This is the security path.
* **The Omarchy Quattro plugin** asks for status to draw, and nothing else. It
  is presentation only, and must never be able to cause or influence an unlock —
  if the shell is not running, unlock still works exactly the same.

That split is enforced by having two sockets with different permissions rather
than one socket with a role field in the message, so a compromised shell plugin
cannot reach the auth verb at all.

Messages are newline-delimited JSON.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "glance"
AUTH_SOCKET = RUNTIME_DIR / "auth.sock"
STATUS_SOCKET = RUNTIME_DIR / "status.sock"


@dataclass
class Request:
    verb: str
    payload: dict[str, Any]

    @staticmethod
    def decode(line: bytes) -> "Request":
        data = json.loads(line)
        return Request(verb=str(data["verb"]), payload=dict(data.get("payload", {})))

    def encode(self) -> bytes:
        return json.dumps({"verb": self.verb, "payload": self.payload}).encode() + b"\n"


@dataclass
class Response:
    ok: bool
    payload: dict[str, Any]

    @staticmethod
    def decode(line: bytes) -> "Response":
        data = json.loads(line)
        return Response(ok=bool(data["ok"]), payload=dict(data.get("payload", {})))

    def encode(self) -> bytes:
        return json.dumps({"ok": self.ok, "payload": self.payload}).encode() + b"\n"


def listen(path: Path, mode: int) -> socket.socket:
    """Bind a Unix stream socket, replacing a stale one.

    `mode` is applied before the socket is reachable, not after, so there is no
    window in which the auth socket is world-writable.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o777 & ~mode)
    try:
        server.bind(str(path))
    finally:
        os.umask(old_umask)
    os.chmod(path, mode)
    server.listen(8)
    return server


def request(path: Path, verb: str, payload: Optional[dict[str, Any]] = None, timeout: float = 15.0) -> Response:
    """Send one request and read one response."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(path))
        client.sendall(Request(verb, payload or {}).encode())
        buffer = b""
        while not buffer.endswith(b"\n"):
            chunk = client.recv(4096)
            if not chunk:
                break
            buffer += chunk
        return Response.decode(buffer)
    finally:
        client.close()
