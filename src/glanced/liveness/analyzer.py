"""Rolling-window driver for the liveness cues.

Port of `glance/Liveness/LivenessAnalyzer.swift`. Holds the last ~2s of frames,
computes every cue's reading from them each frame, and feeds those into a
`LivenessEvaluator` that accumulates fire counts and produces a decision.

Deliberately takes `LivenessFrame` values, not recognition results — callers
extract via `features.extract()` first. That keeps this module's dependency
graph shallow (no ONNX, no enrollment store, no camera), which is what lets the
tests exercise the real decision logic offline.

There is no score and no threshold here. See `cues.py` for the five-cue,
fire-and-latch model and why the weighted average it replaced was the wrong
combination rule.
"""

from __future__ import annotations

from typing import Callable, Optional

from . import planar
from .cues import (
    ALL_CUES,
    DEFAULT_TUNING,
    EMPTY_SNAPSHOT,
    LivenessCue,
    LivenessEvaluator,
    LivenessMode,
    LivenessSnapshot,
    LivenessTuning,
    readings,
)
from .frame import LivenessFrame
from .planar import EMPTY_GEOMETRY_RESULT, GeometryLivenessResult


class LivenessAnalyzer:
    def __init__(self, window_duration: float = 2.0) -> None:
        self.window_duration = window_duration

        # Read fresh on every observe() rather than captured at init, so a
        # mid-scan settings change takes effect on the next frame instead of
        # needing a new scan cycle.
        self.mode_provider: Callable[[], LivenessMode] = lambda: LivenessMode.LIGHT
        self.tuning_provider: Callable[[], LivenessTuning] = lambda: DEFAULT_TUNING
        self.enabled_cues_provider: Callable[[], set[LivenessCue]] = lambda: set(ALL_CUES)

        self._frames: list[LivenessFrame] = []
        self._evaluator = LivenessEvaluator()
        self.last_snapshot: LivenessSnapshot = EMPTY_SNAPSHOT
        #: Kept for a debug console's diagnostics panel (excess ratio, coherence,
        #: pair counts, yaw range) — the numbers behind the flat-vs-3D level.
        self.last_geometry: GeometryLivenessResult = EMPTY_GEOMETRY_RESULT

    def reset(self) -> None:
        self._frames.clear()
        self._evaluator.reset()
        self.last_snapshot = EMPTY_SNAPSHOT
        self.last_geometry = EMPTY_GEOMETRY_RESULT

    @property
    def window(self) -> tuple[LivenessFrame, ...]:
        return tuple(self._frames)

    def observe(self, frame: LivenessFrame) -> LivenessSnapshot:
        """Feed one frame into the rolling window and return the decision as it
        now stands.

        Call once per frame that had a face detected — liveness is fed
        regardless of whether that frame also matched an identity, so the window
        stays dense and liveness stays a genuinely independent gate rather than
        one starved by recognition's own confidence.

        Note the asymmetry between the two pieces of state: the *window* is
        time-pruned (a cue reading only ever reflects the last ~2s), but the
        evaluator's fire counts are **not** — they accumulate across the whole
        scan. That is deliberate. A blink 4 seconds into a scan should still
        count at second 6, and a spoof tell that flashes briefly should not be
        waitable-out by holding still until it ages out.
        """
        self._frames.append(frame)
        self._frames = [
            f for f in self._frames if frame.timestamp - f.timestamp <= self.window_duration
        ]

        self._evaluator.mode = self.mode_provider()
        self._evaluator.tuning = self.tuning_provider()
        self._evaluator.enabled_cues = self.enabled_cues_provider()

        geometry = planar.evaluate(self._frames)
        self.last_geometry = geometry

        snapshot = self._evaluator.observe(readings(self._frames, geometry))
        self.last_snapshot = snapshot
        return snapshot
