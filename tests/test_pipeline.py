"""The unlock gate, driven with synthetic liveness frames and fixed embeddings.

What is pinned here is that recognition and liveness are satisfied by *one*
face. Both gates accumulate across a scan, so the attack being guarded against
is a swap: a photo of the owner earns the match, then a different, live person
earns liveness — or the same two in the other order.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from glanced.liveness import LivenessMode
from glanced.pipeline import Outcome, UnlockPipeline
from glanced.store import EnrollmentStore, Identity

import synthetic


def _unit(axis: int) -> np.ndarray:
    v = np.zeros(512, dtype=np.float32)
    v[axis] = 1.0
    return v


OWNER = _unit(0)
STRANGER = _unit(1)
BOX = (200.0, 120.0, 240.0, 240.0)
ELSEWHERE = (0.0, 0.0, 120.0, 120.0)


def _pipeline() -> UnlockPipeline:
    store = EnrollmentStore(identities=[Identity(name="me", embeddings=OWNER[None, :])])
    pipeline = UnlockPipeline(store, mode=LivenessMode.HEAVY)
    pipeline.begin()
    return pipeline


def _live(start: float = 0.0):
    """A turning, blinking head — enough for Heavy mode to confirm."""
    window = synthetic.live_face_window(frame_count=16, ear_series=synthetic.blink_ear_series(16))
    return [replace(f, timestamp=f.timestamp + start) for f in window]


def _photo(start: float = 0.0):
    """A bare photo: no confirm cue fires, and no deny cue either."""
    return [replace(f, timestamp=f.timestamp + start) for f in synthetic.flat_photo_window(frame_count=16)]


def _feed(pipeline, frames, embedding, box=None) -> list[Outcome]:
    extra = () if box is None else (box,)
    return [pipeline.observe(frame, embedding, *extra).outcome for frame in frames]


def test_the_owner_alive_unlocks():
    result = None
    pipeline = _pipeline()
    for frame in _live():
        result = pipeline.observe(frame, OWNER, BOX)
        if result.outcome is not Outcome.PENDING:
            break
    assert result.outcome is Outcome.UNLOCKED
    assert result.identity.name == "me"


def test_a_photo_match_does_not_lend_itself_to_a_live_stranger():
    pipeline = _pipeline()
    assert set(_feed(pipeline, _photo(), OWNER)) == {Outcome.PENDING}
    assert Outcome.UNLOCKED not in _feed(pipeline, _live(start=1.0), STRANGER)


def test_a_stranger_s_liveness_does_not_lend_itself_to_a_photo():
    pipeline = _pipeline()
    assert Outcome.UNLOCKED not in _feed(pipeline, _live(), STRANGER)
    assert Outcome.UNLOCKED not in _feed(pipeline, _photo(start=1.0), OWNER)


def test_liveness_earned_before_a_face_loss_does_not_survive_it():
    pipeline = _pipeline()
    frames = _live()
    # The blink, then the face leaves view before any frame could match.
    assert set(_feed(pipeline, frames[:10], None, BOX)) == {Outcome.PENDING}
    pipeline.face_lost()
    # The owner's photo, held where the face was.
    assert set(_feed(pipeline, _photo(start=1.0), OWNER, BOX)) == {Outcome.PENDING}


def test_a_face_that_jumps_position_is_a_new_subject():
    pipeline = _pipeline()
    assert Outcome.UNLOCKED not in _feed(pipeline, _live()[:10], None, BOX)
    assert set(_feed(pipeline, _photo(start=1.0), OWNER, ELSEWHERE)) == {Outcome.PENDING}


def test_a_frame_without_an_embedding_never_unlocks():
    pipeline = _pipeline()
    assert set(_feed(pipeline, _live(), None, BOX)) == {Outcome.PENDING}
    # The very next frame that can be recognized is the one that unlocks.
    frame = replace(_live()[-1], timestamp=1.0)
    assert pipeline.observe(frame, OWNER, BOX).outcome is Outcome.UNLOCKED


def test_the_owner_still_unlocks_after_a_stranger_leaves():
    pipeline = _pipeline()
    assert Outcome.UNLOCKED not in _feed(pipeline, _live(), STRANGER, BOX)
    pipeline.face_lost()
    assert Outcome.UNLOCKED in _feed(pipeline, _live(start=2.0), OWNER, BOX)
