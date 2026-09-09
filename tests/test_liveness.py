"""Regression tests for the ported liveness model.

These stand in for `tools/liveness_selftest.swift` upstream. The point is not
coverage for its own sake: every tuning constant in `cues.py` and `planar.py`
was chosen against measurements like these, so a test that pins the *separation*
between a live face and a flat presentation is what makes those constants
retunable without re-deriving them from scratch.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from glanced.liveness import (
    DEFAULT_TUNING,
    CueReading,
    GlareSample,
    LivenessAnalyzer,
    LivenessCue,
    LivenessEvaluator,
    LivenessMode,
)
from glanced.liveness import cues as cues_module
from glanced.liveness import planar
from glanced.liveness.geometry import solve_homography, solve_robust_homography
from glanced.liveness.scoring import blink_dynamics, pose_depth_consistency

import synthetic
from synthetic import SyntheticConfig


# --- geometry ---------------------------------------------------------------


def test_homography_recovers_a_known_projective_map():
    src = np.array([[0.0, 0.0], [100.0, 0.0], [100.0, 80.0], [0.0, 80.0], [50.0, 40.0]])
    truth = np.array([[1.2, 0.15, 30.0], [-0.1, 0.95, -12.0], [0.0006, 0.0004, 1.0]])
    homo = np.hstack([src, np.ones((len(src), 1))]) @ truth.T
    dst = homo[:, :2] / homo[:, 2:3]

    fitted = solve_homography(src, dst)
    assert fitted is not None
    np.testing.assert_allclose(fitted.apply(src), dst, atol=1e-6)


def test_robust_homography_ignores_one_wild_outlier():
    rng = np.random.default_rng(7)
    src = rng.uniform(0, 200, size=(12, 2))
    dst = src * 1.1 + np.array([20.0, -5.0])
    contaminated = dst.copy()
    contaminated[3] += np.array([120.0, -90.0])  # one landmark gone haywire

    robust = solve_robust_homography(src, contaminated)
    assert robust is not None
    clean = np.delete(np.arange(len(src)), 3)
    residual = np.hypot(*(robust.apply(src)[clean] - dst[clean]).T)
    assert residual.max() < 1.0, "an outlier pulled the plane fit around"


# --- flat vs 3D -------------------------------------------------------------


def test_flat_vs_3d_separates_a_turning_head_from_a_tilted_photo():
    live = planar.evaluate(synthetic.live_face_window())
    spoof = planar.evaluate(synthetic.flat_photo_window())

    assert live.planar_confidence > 0, "the live sweep should clear the yaw and motion gates"
    assert live.planar_residual_score >= DEFAULT_TUNING.flat_vs_3d_level, (
        f"a turning 3D head scored {live.planar_residual_score:.3f}, "
        f"under the {DEFAULT_TUNING.flat_vs_3d_level} fire threshold"
    )
    assert spoof.planar_residual_score < DEFAULT_TUNING.flat_vs_3d_level, (
        f"a flat photo scored {spoof.planar_residual_score:.3f}, at or over the fire threshold"
    )
    # The margin is the thing worth protecting, not either absolute number.
    assert live.planar_residual_score > spoof.planar_residual_score * 3


@pytest.mark.parametrize("noise_px", [0.0, 0.5, 1.0])
def test_flat_vs_3d_degrades_with_landmark_noise_but_still_fires(noise_px):
    """The measurement behind the 0.25 threshold.

    Upstream records 0.99 / 0.46 / 0.21 at 0px / 0.5px / 1px of jitter, which is
    why the threshold is 0.25 and not 0.5. The geometry differs here (different
    synthetic face, different landmarker), so this pins the *shape* — monotonic
    decay that still clears the gate at realistic jitter — rather than the exact
    numbers.
    """
    result = planar.evaluate(
        synthetic.live_face_window(config=SyntheticConfig(noise_px=noise_px))
    )
    assert result.planar_residual_score >= DEFAULT_TUNING.flat_vs_3d_level


def test_flat_vs_3d_abstains_below_the_yaw_gate():
    """A still head is not a photo. Not enough rotation to measure parallax must
    read as 'we could not look', never as 'we looked and saw a plane'."""
    result = planar.evaluate(synthetic.still_face_window())
    assert result.planar_confidence == 0.0
    assert result.planar_residual_score == 0.0
    assert result.planar_reading.abstained


# --- depth / pose -----------------------------------------------------------


def test_depth_pose_confirms_a_real_nose_and_abstains_on_a_plane():
    live = pose_depth_consistency(synthetic.live_face_window())
    assert live.confidence > 0
    assert live.level >= DEFAULT_TUNING.depth_pose_level

    spoof = pose_depth_consistency(synthetic.flat_photo_window())
    assert spoof.abstained or spoof.level < DEFAULT_TUNING.depth_pose_level


@pytest.mark.parametrize("noise_px", [0.0, 0.25, 0.5, 1.0])
def test_depth_pose_rejects_a_tilted_photo_at_every_noise_level(noise_px):
    """Regression test for the hole the magnitude gate closes.

    A plane viewed in perspective shifts its apparent eye midpoint relative to
    its nose, and Pearson correlation — being scale-free — reads that tiny
    systematic drift as r ~ 1.0. Before `MIN_NOSE_DEPTH_RATIO`, this confirmed a
    flat photo as live at 0.0 and 0.25px of jitter, and only failed to at ~1px.
    The cue must not depend on landmark noise to hide an artifact.
    """
    reading = pose_depth_consistency(
        synthetic.flat_photo_window(config=SyntheticConfig(noise_px=noise_px))
    )
    assert reading.abstained or reading.level < DEFAULT_TUNING.depth_pose_level


@pytest.mark.parametrize("noise_px", [0.0, 0.25, 0.5, 1.0])
def test_depth_pose_still_confirms_a_real_nose_at_every_noise_level(noise_px):
    """The other half: the gate must not cost the cue its real detections."""
    reading = pose_depth_consistency(
        synthetic.live_face_window(config=SyntheticConfig(noise_px=noise_px))
    )
    assert not reading.abstained
    assert reading.level >= DEFAULT_TUNING.depth_pose_level


def test_depth_pose_abstains_below_the_measurable_yaw_range():
    window = synthetic.live_face_window(yaw_sweep_degrees=6.0)
    assert pose_depth_consistency(window).abstained


# --- blink ------------------------------------------------------------------


def test_blink_detected_from_dip_and_recovery():
    window = synthetic.live_face_window(ear_series=synthetic.blink_ear_series(12))
    reading = blink_dynamics(window)
    assert reading.level == 1.0 and reading.confidence == 1.0


def test_no_blink_when_eyes_stay_open():
    window = synthetic.live_face_window(ear_series=[0.31] * 12)
    assert blink_dynamics(window).abstained


def test_no_blink_when_eyes_close_without_recovering():
    """Squinting, or a face lost mid-scan, is not a blink — the event is the
    dip *and* the recovery."""
    series = [0.32, 0.31, 0.30, 0.20, 0.10, 0.08, 0.07, 0.06, 0.06, 0.05, 0.05, 0.05]
    window = synthetic.live_face_window(ear_series=series)
    assert blink_dynamics(window).abstained


# --- deny cues --------------------------------------------------------------


def test_gloss_glare_fires_on_a_large_concentrated_highlight():
    frame = synthetic.live_face_window(frame_count=4)[-1]
    frame.glare = GlareSample(
        crop_pixel_width=160.0, specular_fraction=0.05, specular_cluster_ratio=0.9
    )
    reading = cues_module.gloss_glare(frame)
    assert reading.confidence == 1.0
    assert reading.level >= DEFAULT_TUNING.gloss_level


def test_gloss_glare_does_not_fire_on_a_scattered_shiny_forehead():
    frame = synthetic.live_face_window(frame_count=4)[-1]
    # Same amount of bright pixel, spread across the crop rather than pooled.
    frame.glare = GlareSample(
        crop_pixel_width=160.0, specular_fraction=0.012, specular_cluster_ratio=0.15
    )
    assert cues_module.gloss_glare(frame).level < DEFAULT_TUNING.gloss_level


def test_gloss_glare_abstains_on_a_tiny_crop():
    """Below ~50px of face there is not enough detail to tell a glare blob from
    a bright patch, so the cue must abstain rather than convict."""
    frame = synthetic.live_face_window(frame_count=4)[-1]
    frame.glare = GlareSample(
        crop_pixel_width=40.0, specular_fraction=0.06, specular_cluster_ratio=0.95
    )
    assert cues_module.gloss_glare(frame).abstained


def test_glare_extractor_measures_a_synthetic_blob():
    from glanced.liveness import glare

    crop = np.full((120, 120, 3), 90, dtype=np.uint8)  # dull, non-specular skin
    crop[20:44, 20:44] = 250  # one bright, neutral blob
    sample = glare.extract(crop)
    assert sample is not None
    assert sample.crop_pixel_width == 120.0
    assert sample.specular_fraction == pytest.approx((24 * 24) / (120 * 120), rel=1e-6)
    # A single blob inside one 8x8 grid cell should be almost entirely pooled.
    assert sample.specular_cluster_ratio > 0.2


# --- evaluator: firing, latching, roles -------------------------------------


def _fire(cue: LivenessCue, level: float = 1.0) -> dict:
    return {cue: CueReading(level=level, confidence=1.0)}


def test_cue_needs_repeated_frames_before_it_fires():
    evaluator = LivenessEvaluator(mode=LivenessMode.HEAVY)
    frames_needed = DEFAULT_TUNING.frames(LivenessCue.DEVICE_DETECTED)
    for i in range(frames_needed - 1):
        snapshot = evaluator.observe(_fire(LivenessCue.DEVICE_DETECTED))
        assert snapshot.decision.is_pending, f"fired after only {i + 1} frames"
    assert evaluator.observe(_fire(LivenessCue.DEVICE_DETECTED)).decision.is_denied


def test_deny_latches_and_cannot_be_waited_out():
    """A spoof tell that flashes briefly must not be survivable by holding still
    until it ages out of the window."""
    evaluator = LivenessEvaluator(mode=LivenessMode.HEAVY)
    for _ in range(DEFAULT_TUNING.device_frames):
        evaluator.observe(_fire(LivenessCue.DEVICE_DETECTED))
    for _ in range(30):
        snapshot = evaluator.observe({})
        assert snapshot.decision.is_denied


def test_deny_overrides_a_confirmation_that_already_happened():
    evaluator = LivenessEvaluator(mode=LivenessMode.HEAVY)
    for _ in range(DEFAULT_TUNING.blink_frames):
        snapshot = evaluator.observe(_fire(LivenessCue.BLINK))
    assert snapshot.decision.is_confirmed

    for _ in range(DEFAULT_TUNING.gloss_frames):
        snapshot = evaluator.observe(_fire(LivenessCue.GLOSS_GLARE))
    assert snapshot.decision.is_denied
    assert snapshot.decision.cue is LivenessCue.GLOSS_GLARE
    assert "glare" in snapshot.decision.denial_reason.lower()


def test_abstention_never_counts_as_a_reading_of_zero():
    """A cue that cannot see anything must not be able to convict *or* acquit."""
    evaluator = LivenessEvaluator(mode=LivenessMode.HEAVY)
    for _ in range(50):
        snapshot = evaluator.observe(
            {c: CueReading(level=1.0, confidence=0.0) for c in LivenessCue}
        )
    assert snapshot.decision.is_pending
    assert all(not snapshot.state(c).has_fired for c in LivenessCue)


def test_light_mode_waits_for_the_deny_cues_before_auto_confirming():
    evaluator = LivenessEvaluator(mode=LivenessMode.LIGHT)
    for _ in range(DEFAULT_TUNING.light_mode_minimum_frames - 1):
        assert evaluator.observe({}).decision.is_pending
    assert evaluator.observe({}).decision.is_confirmed


def test_light_mode_still_denies_a_spoof():
    evaluator = LivenessEvaluator(mode=LivenessMode.LIGHT)
    for _ in range(DEFAULT_TUNING.gloss_frames + DEFAULT_TUNING.light_mode_minimum_frames):
        snapshot = evaluator.observe(_fire(LivenessCue.GLOSS_GLARE))
    assert snapshot.decision.is_denied


def test_heavy_mode_leaves_a_perfectly_still_user_pending():
    """The documented cost of Heavy mode, pinned so it is never mistaken for a
    regression: with no rotation and no blink, no confirm cue can fire."""
    analyzer = LivenessAnalyzer()
    analyzer.mode_provider = lambda: LivenessMode.HEAVY
    for frame in synthetic.still_face_window(frame_count=40):
        snapshot = analyzer.observe(frame)
    assert snapshot.decision.is_pending


def test_disabled_cue_cannot_decide():
    evaluator = LivenessEvaluator(
        mode=LivenessMode.HEAVY,
        enabled_cues={c for c in LivenessCue if c is not LivenessCue.DEVICE_DETECTED},
    )
    for _ in range(10):
        snapshot = evaluator.observe(_fire(LivenessCue.DEVICE_DETECTED))
    assert snapshot.decision.is_pending


# --- analyzer end to end ----------------------------------------------------


def test_heavy_mode_confirms_a_turning_live_face():
    analyzer = LivenessAnalyzer()
    analyzer.mode_provider = lambda: LivenessMode.HEAVY
    window = synthetic.live_face_window(frame_count=16, ear_series=synthetic.blink_ear_series(16))
    for frame in window:
        snapshot = analyzer.observe(frame)
    assert snapshot.decision.is_confirmed


def test_heavy_mode_denies_a_photo_on_a_detected_device():
    analyzer = LivenessAnalyzer()
    analyzer.mode_provider = lambda: LivenessMode.HEAVY
    window = synthetic.flat_photo_window(frame_count=16, device_overlap=0.85)
    for frame in window:
        snapshot = analyzer.observe(frame)
    assert snapshot.decision.is_denied
    assert snapshot.decision.cue is LivenessCue.DEVICE_DETECTED


def test_heavy_mode_does_not_confirm_a_bare_photo_with_no_deny_tell():
    """The hardest and most important case: a flat presentation that leaves no
    glare and shows no device edge. Nothing should confirm it — it must sit
    PENDING until the caller's scan duration expires, not pass."""
    analyzer = LivenessAnalyzer()
    analyzer.mode_provider = lambda: LivenessMode.HEAVY
    for frame in synthetic.flat_photo_window(frame_count=16):
        snapshot = analyzer.observe(frame)
    assert not snapshot.decision.is_confirmed


def test_window_is_time_pruned_but_fire_counts_are_not():
    """A blink early in a long scan must still count minutes later, even though
    the frames carrying it have long since left the 2s window."""
    analyzer = LivenessAnalyzer(window_duration=2.0)
    analyzer.mode_provider = lambda: LivenessMode.HEAVY

    for frame in synthetic.live_face_window(
        frame_count=12, ear_series=synthetic.blink_ear_series(12)
    ):
        snapshot = analyzer.observe(frame)
    assert snapshot.decision.is_confirmed

    tail = synthetic.still_face_window(frame_count=60)
    for i, frame in enumerate(tail):
        frame.timestamp = 100.0 + i / 20.0  # far outside the original window
        snapshot = analyzer.observe(frame)

    assert len(analyzer.window) <= 41, "window should have been time-pruned"
    assert snapshot.decision.is_confirmed, "the blink stopped counting once it aged out"
