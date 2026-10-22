"""Which /dev/video* node is the camera.

`/dev/video0` is the right answer on most laptops and the wrong one on enough
of them to matter. On Apple Silicon Macs running Asahi Linux, video0 is the
`avd` hardware video decoder and the FaceTime HD camera is video1; any laptop
with a hardware codec, an IR camera or a capture card can shuffle the order
the same way. Opening a decoder as a camera fails with "could not open".

So the default asks each node what it is (VIDIOC_QUERYCAP, the same question
`v4l2-ctl --info` asks) and takes the first one that captures video and is not
a memory-to-memory codec. `$GLANCE_DEVICE` overrides that, and every command
still takes `--device`.

No OpenCV here: the daemon reports the device on its status socket without
loading the camera stack.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

#: The answer when nothing better can be found, and what 0.3.6 always used.
FALLBACK_DEVICE = "/dev/video0"

# linux/videodev2.h
VIDIOC_QUERYCAP = 0x80685600  # _IOR('V', 0, struct v4l2_capability), 104 bytes
_CAPABILITY = struct.Struct("16s32s32sIII3I")
CAP_VIDEO_CAPTURE = 0x00000001
CAP_VIDEO_OUTPUT = 0x00000002
CAP_VIDEO_CAPTURE_MPLANE = 0x00001000
CAP_VIDEO_OUTPUT_MPLANE = 0x00002000
CAP_VIDEO_M2M_MPLANE = 0x00004000
CAP_VIDEO_M2M = 0x00008000
CAP_DEVICE_CAPS = 0x80000000

_CODEC = CAP_VIDEO_M2M | CAP_VIDEO_M2M_MPLANE | CAP_VIDEO_OUTPUT | CAP_VIDEO_OUTPUT_MPLANE


@dataclass(frozen=True)
class Capability:
    driver: str
    card: str
    #: The node's own capabilities (device_caps), not the whole driver's.
    caps: int

    @property
    def is_camera(self) -> bool:
        return bool(self.caps & CAP_VIDEO_CAPTURE) and not self.caps & _CODEC


def querycap(path: str) -> Optional[Capability]:
    """VIDIOC_QUERYCAP on `path`, or None if it cannot be asked."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        buffer = bytearray(_CAPABILITY.size)
        fcntl.ioctl(fd, VIDIOC_QUERYCAP, buffer, True)
    except OSError as error:
        if error.errno not in (errno.ENOTTY, errno.EINVAL, errno.ENODEV, errno.EBUSY, errno.EIO):
            raise
        return None
    finally:
        os.close(fd)
    return parse_capability(bytes(buffer))


def parse_capability(raw: bytes) -> Capability:
    """A `struct v4l2_capability` as the kernel filled it in."""
    driver, card, _bus, _version, capabilities, device_caps, *_ = _CAPABILITY.unpack(raw)
    caps = device_caps if capabilities & CAP_DEVICE_CAPS else capabilities
    return Capability(
        driver=driver.split(b"\0", 1)[0].decode(errors="replace"),
        card=card.split(b"\0", 1)[0].decode(errors="replace"),
        caps=caps,
    )


def _nodes(dev_dir: Path) -> list[str]:
    found = []
    for entry in dev_dir.glob("video*"):
        match = re.fullmatch(r"video(\d+)", entry.name)
        if match:
            found.append((int(match.group(1)), str(entry)))
    return [path for _, path in sorted(found)]


def capture_devices(
    dev_dir: Path = Path("/dev"),
    query: Callable[[str], Optional[Capability]] = querycap,
) -> list[str]:
    """Every node that captures video and is not a codec, in /dev/videoN order."""
    return [node for node in _nodes(dev_dir) if (cap := query(node)) is not None and cap.is_camera]


def default_device(
    dev_dir: Path = Path("/dev"),
    query: Callable[[str], Optional[Capability]] = querycap,
) -> str:
    """`$GLANCE_DEVICE`, else the first camera node, else /dev/video0."""
    configured = os.environ.get("GLANCE_DEVICE", "").strip()
    if configured:
        return configured
    try:
        found = capture_devices(dev_dir, query)
    except OSError:
        found = []
    return found[0] if found else FALLBACK_DEVICE


def resolve(device: Optional[str]) -> str:
    """An explicit device wins; None or empty means the default above."""
    return device if device else default_device()
