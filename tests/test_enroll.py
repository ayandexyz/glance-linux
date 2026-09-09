"""Enrollment capture logic with the camera and models faked out."""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest

from glanced import enroll


class FakeCamera:
    def __init__(self, config=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def frames(self):
        while True:
            yield np.zeros((4, 4, 3), dtype=np.uint8)


def _observation(embedding, reliable=True):
    return SimpleNamespace(
        embedding=embedding,
        liveness_frame=SimpleNamespace(has_reliable_landmarks=reliable),
    )


class ScriptedProcessor:
    """Returns the next scripted observation on each frame; None when exhausted."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def process(self, frame, now, *, want_embedding=True):
        self.calls += 1
        return self.script.pop(0) if self.script else None


@pytest.fixture(autouse=True)
def fake_camera(monkeypatch):
    monkeypatch.setattr(enroll, "Camera", FakeCamera)


def _unit(direction: int) -> np.ndarray:
    vector = np.zeros(512, dtype=np.float32)
    vector[direction] = 1.0
    return vector


def _near(base: np.ndarray, epsilon: float = 0.05) -> np.ndarray:
    vector = base.copy()
    vector[1] = epsilon
    return vector / np.linalg.norm(vector)


def test_collects_spaced_captures_and_prompts_each_one(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock.__setitem__(0, clock[0] + 0.5) or clock[0])
    me = _unit(0)
    processor = ScriptedProcessor([_observation(_near(me)) for _ in range(20)])
    prompts = []
    embeddings = enroll.capture_embeddings(
        processor, count=3, spacing=1.0, timeout=100, on_prompt=lambda i, n, text: prompts.append(i)
    )
    assert len(embeddings) == 3
    assert prompts == [1, 2, 3]


def test_rejects_a_different_face_mid_enrollment(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock.__setitem__(0, clock[0] + 2.0) or clock[0])
    me, stranger = _unit(0), _unit(7)
    processor = ScriptedProcessor(
        [_observation(me), _observation(stranger), _observation(stranger), _observation(_near(me))]
    )
    embeddings = enroll.capture_embeddings(processor, count=2, spacing=1.0, timeout=100)
    assert len(embeddings) == 2
    assert float(embeddings[0] @ embeddings[1]) > enroll.MIN_SELF_SIMILARITY


def test_skips_unreliable_landmarks(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock.__setitem__(0, clock[0] + 2.0) or clock[0])
    me = _unit(0)
    processor = ScriptedProcessor([_observation(me, reliable=False), _observation(me)])
    embeddings = enroll.capture_embeddings(processor, count=1, spacing=1.0, timeout=100)
    assert len(embeddings) == 1


def test_times_out_with_no_face(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock.__setitem__(0, clock[0] + 5.0) or clock[0])
    processor = ScriptedProcessor([])
    with pytest.raises(TimeoutError):
        enroll.capture_embeddings(processor, count=1, timeout=20)
