"""The guided-direction gate: bands, sectors, and the sweep state machine.

All of it runs without a camera or a model — the point of keeping the pose
rule as arithmetic over two angles is that the thing most likely to strand a
real user (a band nobody can hit) is the thing easiest to pin in a test.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pytest

from glanced import enroll, poses


def _face(yaw, pitch, width=200.0):
    return SimpleNamespace(yaw=yaw, pitch=pitch, bounding_box=(0.0, 0.0, width, width))


def _observation(embedding, yaw, pitch, *, reliable=True, width=200.0, native_width=None):
    # The tests' `frame_width` is native, so by default the working and native
    # boxes coincide; pass `native_width` to model a downscaled working frame.
    native = width if native_width is None else native_width
    return SimpleNamespace(
        embedding=embedding,
        face=_face(yaw, pitch, width),
        liveness_frame=SimpleNamespace(has_reliable_landmarks=reliable),
        native_bounding_box=(0.0, 0.0, native, native),
    )


def _unit(direction: int = 0) -> np.ndarray:
    vector = np.zeros(512, dtype=np.float32)
    vector[direction] = 1.0
    return vector


def _pose(name: str) -> poses.Pose:
    return next(p for p in poses.POSES if p.name == name)


# --- bands ----------------------------------------------------------------


def test_poses_are_centre_plus_the_four_cardinal_directions():
    assert [p.name for p in poses.POSES] == ["centre", "left", "top", "right", "bottom"]
    assert sorted(p.compass_angle for p in poses.SECTOR_POSES) == [0.0, 90.0, 180.0, 270.0]
    assert sum(1 for p in poses.POSES if p.compass_angle is None) == 1


def test_sectors_tile_the_circle_evenly():
    """Whatever POSES holds, the directional ones divide the circle exactly —
    so restoring the diagonals stays an edit to that tuple alone."""
    assert poses.SECTOR_COUNT * poses.SECTOR_DEGREES == 360.0


def test_every_pose_has_a_distinct_band_pair():
    pairs = {(p.yaw_band, p.pitch_band) for p in poses.POSES}
    assert len(pairs) == len(poses.POSES)


def test_centre_matches_a_level_head_and_nothing_turned():
    centre = _pose("centre")
    assert poses.matches(centre, 0.0, 0.0)
    assert not poses.matches(centre, 0.4, 0.0)
    assert not poses.matches(centre, 0.0, 0.4)


def test_a_turn_past_the_inner_threshold_matches_that_side_only():
    left, right = _pose("left"), _pose("right")
    yaw = poses.YAW_SIGN * 0.4
    assert poses.matches(left, yaw, 0.0)
    assert not poses.matches(right, yaw, 0.0)
    assert not poses.matches(left, -yaw, 0.0)


def test_a_head_turned_too_far_is_rejected():
    """Past the outer cap the landmarker is guessing at half the face, and a
    template row from there is worse than no row."""
    left = _pose("left")
    assert not poses.matches(left, poses.YAW_SIGN * (poses.TUNING.yaw_outer + 0.1), 0.0)


def test_a_cardinal_pose_needs_the_other_axis_held_level():
    """"Chin up" is up, not up-and-turned — otherwise "top" and a diagonal
    would be the same gate and the sector lit would be a coin toss."""
    top = _pose("top")
    pitch = poses.PITCH_SIGN * -0.35
    assert poses.matches(top, 0.0, pitch)
    assert not poses.matches(top, poses.YAW_SIGN * 0.5, pitch)


def test_widening_admits_a_turn_that_falls_just_short():
    left = _pose("left")
    marginal = poses.YAW_SIGN * (poses.TUNING.yaw_inner / poses.TUNING.widen_factor + 0.005)
    assert not poses.matches(left, marginal, 0.0)
    assert poses.matches(left, marginal, 0.0, widened=True)


@pytest.mark.parametrize("yaw,pitch", [(None, 0.0), (0.0, None), (math.nan, 0.0), (0.0, math.inf)])
def test_a_missing_or_broken_angle_never_matches(yaw, pitch):
    """Otherwise a camera with no transformation matrix would enrol five
    copies of the same frontal frame and call it a five-pose template."""
    assert not poses.matches(_pose("centre"), yaw, pitch)


def test_sector_lookup_buckets_to_the_nearest_sector_centre():
    assert poses.sector_pose(2.0) is _pose("top")
    assert poses.sector_pose(44.0) is _pose("top")
    assert poses.sector_pose(46.0) is _pose("right")
    assert poses.sector_pose(359.0) is _pose("top")


def test_a_boundary_angle_always_falls_the_same_way():
    """Rounding would break these ties to even — handing two opposite sectors
    an extra tick each and leaving the ring visibly lopsided."""
    for index in range(poses.SECTOR_COUNT):
        boundary = index * poses.SECTOR_DEGREES + poses.SECTOR_DEGREES / 2.0
        below = poses.sector_pose(boundary - 0.001)
        assert poses.sector_pose(boundary) is not below


# --- the sweep ------------------------------------------------------------


def _angles_for(pose: poses.Pose) -> tuple[float, float]:
    """A yaw/pitch comfortably inside `pose`'s bands."""
    yaw = {poses.Band.CENTRE: 0.0, poses.Band.POSITIVE: 0.5, poses.Band.NEGATIVE: -0.5}[pose.yaw_band]
    pitch = {poses.Band.CENTRE: 0.0, poses.Band.POSITIVE: 0.4, poses.Band.NEGATIVE: -0.4}[pose.pitch_band]
    return poses.YAW_SIGN * yaw, poses.PITCH_SIGN * pitch


def _run(session, pose, *, frames=20, start=10.0, step=0.2, embedding=None):
    """Feed `session` frames holding `pose`; return the poses it completed."""
    yaw, pitch = _angles_for(pose)
    completed = []
    for i in range(frames):
        observation = _observation(embedding if embedding is not None else _unit(), yaw, pitch)
        done = session.offer(observation, start + i * step, frame_width=400)
        if done is not None:
            completed.append(done)
    return completed


def test_a_full_sweep_captures_every_pose_in_order():
    session = enroll.GuidedSession()
    for pose in poses.POSES:
        assert session.current_pose is pose
        assert _run(session, pose) == [pose]
    assert session.complete
    assert session.captured_poses == [p.name for p in poses.POSES]
    assert len(session.embeddings) == len(poses.POSES) * enroll.SAMPLES_PER_POSE
    assert len(session.embeddings) == 10
    assert session.pose_names.count("centre") == enroll.SAMPLES_PER_POSE


def test_holding_the_wrong_pose_captures_nothing():
    session = enroll.GuidedSession()
    assert _run(session, _pose("right")) == []
    assert session.embeddings == []
    assert session.current_pose is _pose("centre")


def test_capture_is_held_back_while_the_user_is_still_settling():
    """Detection runs during the settle window — only capture waits — so the
    ring's live readout is never frozen while the camera warms up."""
    session = enroll.GuidedSession()
    centre = _pose("centre")
    assert _run(session, centre, frames=4, start=0.0, step=0.2) == []
    assert session.progress.holding
    assert session.progress.face_detected


def test_a_pose_must_be_held_not_flashed():
    session = enroll.GuidedSession()
    centre = _pose("centre")
    yaw, pitch = _angles_for(centre)
    # Alternating in and out of band: the hold timer restarts every miss, so
    # the streak never survives to a capture.
    for i in range(30):
        angles = (yaw, pitch) if i % 2 == 0 else (poses.YAW_SIGN * 0.9, pitch)
        session.offer(_observation(_unit(), *angles), 10.0 + i * 0.2, frame_width=400)
    assert session.embeddings == []


def test_a_face_too_far_away_is_not_enrolled():
    session = enroll.GuidedSession()
    centre = _pose("centre")
    yaw, pitch = _angles_for(centre)
    for i in range(20):
        session.offer(_observation(_unit(), yaw, pitch, width=40.0), 10.0 + i * 0.2, frame_width=400)
    assert session.embeddings == []
    assert session.progress.too_far


def test_face_size_is_judged_in_native_pixels_not_working_ones():
    # A 1280-wide camera is downscaled to a 640 working frame: a face 300 px
    # wide natively (23% of the frame, close enough) is 150 px in the working
    # frame, which is under 20% of 1280 if the two spaces get mixed up.
    session = enroll.GuidedSession()
    yaw, pitch = _angles_for(_pose("centre"))
    for i in range(20):
        session.offer(
            _observation(_unit(), yaw, pitch, width=150.0, native_width=300.0),
            10.0 + i * 0.2,
            frame_width=1280,
        )
    assert not session.progress.too_far
    assert len(session.embeddings) == enroll.SAMPLES_PER_POSE


def test_a_different_face_mid_sweep_is_rejected():
    session = enroll.GuidedSession()
    _run(session, _pose("centre"))
    assert len(session.embeddings) == enroll.SAMPLES_PER_POSE
    assert _run(session, _pose("left"), start=30.0, embedding=_unit(7)) == []


def test_no_face_leaves_progress_reporting_that():
    session = enroll.GuidedSession()
    session.offer(None, 10.0, frame_width=400)
    assert not session.progress.face_detected
    assert session.progress.yaw is None


def test_bands_widen_once_a_pose_has_stalled():
    session = enroll.GuidedSession()
    marginal = poses.YAW_SIGN * (poses.TUNING.yaw_inner / poses.TUNING.widen_factor + 0.005)
    _run(session, _pose("centre"))

    # Just short of "left", held past the stall timeout: refused at first,
    # accepted once the bands widen, so an unusual camera angle cannot strand
    # somebody on step two forever.
    session.offer(_observation(_unit(), marginal, 0.0), 14.0, frame_width=400)
    assert not session.progress.widened
    assert not session.progress.holding
    for i in range(20):
        session.offer(_observation(_unit(), marginal, 0.0), 30.0 + i * 0.2, frame_width=400)
    assert "left" in session.captured_poses


def test_progress_fraction_runs_zero_to_one():
    session = enroll.GuidedSession()
    assert session.progress.fraction == 0.0
    for pose in poses.POSES:
        _run(session, pose)
    assert session.progress.fraction == pytest.approx(1.0)
    assert session.progress.complete
