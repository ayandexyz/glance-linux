"""Picking the camera node: no real /dev, the ioctl answered by a fake."""

from __future__ import annotations

import pytest

from glanced import v4l2
from glanced.v4l2 import (
    CAP_DEVICE_CAPS,
    CAP_VIDEO_CAPTURE,
    CAP_VIDEO_M2M_MPLANE,
    Capability,
    parse_capability,
)

META_CAPTURE = 0x00800000
STREAMING = 0x04000000

# What an M1 MacBook Pro on Asahi Linux reports: the hardware decoder first.
ASAHI = {
    "video0": Capability("avd", "Apple Video Decoder", CAP_VIDEO_M2M_MPLANE | STREAMING),
    "video1": Capability("apple-isp", "Apple ISP", CAP_VIDEO_CAPTURE | STREAMING),
}


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.delenv("GLANCE_DEVICE", raising=False)


def fake_dev(tmp_path, table):
    for name in [*table, "vbi0", "video-loopback", "media0"]:
        (tmp_path / name).touch()

    def query(path):
        return table.get(path.rsplit("/", 1)[-1])

    return tmp_path, query


def test_skips_the_decoder_and_finds_the_facetime_camera(tmp_path):
    dev, query = fake_dev(tmp_path, ASAHI)
    assert v4l2.capture_devices(dev, query) == [str(dev / "video1")]
    assert v4l2.default_device(dev, query) == str(dev / "video1")


def test_a_uvc_webcam_and_its_metadata_node(tmp_path):
    dev, query = fake_dev(tmp_path, {
        "video0": Capability("uvcvideo", "Integrated Camera", CAP_VIDEO_CAPTURE | STREAMING),
        "video1": Capability("uvcvideo", "Integrated Camera", META_CAPTURE | STREAMING),
    })
    assert v4l2.default_device(dev, query) == str(dev / "video0")


def test_nodes_are_taken_in_numeric_order(tmp_path):
    camera = Capability("uvcvideo", "cam", CAP_VIDEO_CAPTURE)
    dev, query = fake_dev(tmp_path, {"video10": camera, "video2": camera})
    assert v4l2.capture_devices(dev, query) == [str(dev / "video2"), str(dev / "video10")]


def test_glance_device_overrides_detection(tmp_path, monkeypatch):
    dev, query = fake_dev(tmp_path, ASAHI)
    monkeypatch.setenv("GLANCE_DEVICE", "/dev/video7")
    assert v4l2.default_device(dev, query) == "/dev/video7"


def test_falls_back_to_video0_when_nothing_answers(tmp_path):
    dev, query = fake_dev(tmp_path, {"video0": None})
    assert v4l2.default_device(dev, query) == "/dev/video0"


def test_an_explicit_device_always_wins(monkeypatch):
    monkeypatch.setenv("GLANCE_DEVICE", "/dev/video7")
    assert v4l2.resolve("/dev/video3") == "/dev/video3"
    assert v4l2.resolve(None) == "/dev/video7"


def test_codec_nodes_that_also_claim_capture_are_not_cameras():
    assert not Capability("x", "y", CAP_VIDEO_CAPTURE | CAP_VIDEO_M2M_MPLANE).is_camera
    assert Capability("x", "y", CAP_VIDEO_CAPTURE).is_camera


def test_parse_reads_the_node_caps_not_the_driver_caps():
    def raw(capabilities, device_caps):
        return v4l2._CAPABILITY.pack(b"apple-isp", b"Apple ISP", b"platform:isp", 0x60000,
                                     capabilities, device_caps, 0, 0, 0)

    both = CAP_VIDEO_CAPTURE | CAP_VIDEO_M2M_MPLANE
    parsed = parse_capability(raw(both | CAP_DEVICE_CAPS, CAP_VIDEO_CAPTURE))
    assert parsed == Capability("apple-isp", "Apple ISP", CAP_VIDEO_CAPTURE)
    assert parse_capability(raw(both, 0)).caps == both
    assert v4l2._CAPABILITY.size == 104


def test_querycap_on_something_that_is_not_a_v4l2_node(tmp_path):
    plain = tmp_path / "video0"
    plain.write_bytes(b"")
    assert v4l2.querycap(str(plain)) is None
    assert v4l2.querycap(str(tmp_path / "missing")) is None


def test_the_camera_resolves_its_device_when_none_is_given(monkeypatch):
    pytest.importorskip("cv2")
    from glanced.camera import Camera, CameraConfig

    monkeypatch.setenv("GLANCE_DEVICE", "/dev/video1")
    assert Camera(CameraConfig()).device == "/dev/video1"
    assert Camera(CameraConfig(device="/dev/video4")).device == "/dev/video4"


def test_glancectl_live_passes_no_device_unless_given(monkeypatch):
    from glanced import cli, livetest

    seen = []
    monkeypatch.setattr(livetest, "run", lambda **kwargs: seen.append(kwargs["device"]) or 0)
    assert cli.main(["live"]) == 0
    assert cli.main(["live", "--device", "/dev/video3"]) == 0
    assert seen == [None, "/dev/video3"]
