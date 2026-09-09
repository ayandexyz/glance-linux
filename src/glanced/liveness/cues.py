"""The liveness decision model.

Port of `glance/Liveness/LivenessCues.swift`. Five cues and no score to
threshold at all.

That design came out of real-device testing upstream: an earlier version
combined ~11 signals into a weighted mean and compared it against a user-facing
"strictness" slider. Most of those signals were noise-limited at webcam
resolution, several actively rewarded the smooth motion of a hand holding up a
phone, and averaging them produced a number that wandered 30-80% on a live face
while a phone photo scored about the same. Only five cues actually separated a
real face from a phone in practice, and each is *individually* decisive when it
fires — which makes averaging exactly the wrong combination rule.

Two roles, asymmetric on purpose:

* DENY cues (gloss/glare, device detected) are evidence of a spoof. Either one
  firing fails the scan outright, independently of everything else, and
  overrides any confirmation that already happened. A spoof tell does not get
  outvoted.

* CONFIRM cues (flat-vs-3D, depth/pose, blink) are evidence of a real face. Any
  one firing is enough. Critically, their *absence* is not a failure — a live
  person can sit still and not blink for a whole scan. Nothing confirming just
  leaves the decision PENDING, and the scan keeps looking until the caller's own
  face-detection duration runs out.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Optional, Sequence

from .frame import NO_READING, CueReading, LivenessFrame
from .geometry import ramp
from .planar import GeometryLivenessResult
from .scoring import blink_dynamics, pose_depth_consistency


class CueRole(Enum):
    #: Evidence of a spoof. Firing fails the scan and overrides confirmation.
    DENY = "deny"
    #: Evidence of a real face. Firing passes the liveness half of the scan.
    CONFIRM = "confirm"


class LivenessCue(str, Enum):
    GLOSS_GLARE = "glossGlare"
    DEVICE_DETECTED = "deviceDetected"
    FLAT_VS_3D = "flatVs3D"
    DEPTH_POSE = "depthPose"
    BLINK = "blink"

    @property
    def title(self) -> str:
        return {
            LivenessCue.GLOSS_GLARE: "Gloss/glare",
            LivenessCue.DEVICE_DETECTED: "Device detected",
            LivenessCue.FLAT_VS_3D: "Flat vs 3D",
            LivenessCue.DEPTH_POSE: "Depth/pose",
            LivenessCue.BLINK: "Blink",
        }[self]

    @property
    def role(self) -> CueRole:
        if self in (LivenessCue.GLOSS_GLARE, LivenessCue.DEVICE_DETECTED):
            return CueRole.DENY
        return CueRole.CONFIRM

    @property
    def explanation(self) -> str:
        """One-line explanation of what firing actually means, for the debug console."""
        return {
            LivenessCue.GLOSS_GLARE: (
                "Large flat specular highlight — glass/screen glare rather than "
                "skin's small scattered shine."
            ),
            LivenessCue.DEVICE_DETECTED: (
                "A device-shaped rectangle overlaps the face — a phone or tablet held up."
            ),
            LivenessCue.FLAT_VS_3D: (
                "Held-out nose points miss the plane fit — the face has real depth."
            ),
            LivenessCue.DEPTH_POSE: (
                "Nose offset tracks head yaw — the nose sits off the eye plane, "
                "so this isn't flat."
            ),
            LivenessCue.BLINK: (
                "Eye aspect ratio dipped and recovered — a photo cannot blink."
            ),
        }[self]


ALL_CUES: tuple[LivenessCue, ...] = tuple(LivenessCue)


class LivenessMode(str, Enum):
    """How much liveness checking runs. Both modes always run the deny cues —
    the difference is only whether a *positive* proof of life is also required
    before unlocking."""

    #: Deny-only: "confirmed unless proven wrong." Nothing has to prove the face
    #: is real; a spoof tell still fails the scan. Never blocks a legitimate user
    #: who happens to sit still, which is why it is the default.
    LIGHT = "light"
    #: Deny cues, plus at least one confirm cue must fire before unlock. A real
    #: presentation-attack gate — but it can leave a genuinely live user unable
    #: to unlock if they hold perfectly still and do not blink for the whole
    #: scan, since none of the confirm cues would ever fire.
    HEAVY = "heavy"

    @property
    def summary(self) -> str:
        return {
            LivenessMode.LIGHT: "Only rejects obvious spoofs.",
            LivenessMode.HEAVY: "Also requires proof of a real face.",
        }[self]


@dataclass(frozen=True)
class LivenessTuning:
    """Fire thresholds per cue: a cue counts a frame when its reading is
    confident and at or above `level`, and fires once it has counted `frames` of
    them within the scan.

    These are seeded from real-device observation, not synthetic data.
    """

    #: Gloss sat at 0 on a live face and 30-40% against a phone screen in
    #: upstream testing, so this floor is far below the observed spoof signal.
    gloss_level: float = 0.04
    gloss_frames: int = 3

    #: Matching the explicit spec this cue was built to: "if the device detector
    #: goes off even a bit for a few frames, fail." Deliberately much lower than
    #: `gloss_level` — the bezel detector was the one signal proven reliable in
    #: real-device testing, so it does not need the same margin of safety the
    #: newer pixel cues do. Note that on Linux the detector underneath it is a
    #: different algorithm (see `bezel.py`), so that reliability claim is
    #: inherited, not yet re-established.
    device_level: float = 0.15
    device_frames: int = 3

    #: 0.25, not the 0.5 you might expect from "spikes up". Upstream's synthetic
    #: self-test measures a clean 3D face at only ~0.21 once ~1px of landmark
    #: noise is present (0.99 at zero noise, 0.46 at 0.5px) — real-world jitter
    #: is in that range, so a 0.5 gate would mean this cue essentially never
    #: fires and Heavy mode would rest entirely on blink. A tilted photo measures
    #: ~0.02 and a still plane 0.00 in the same test, so 0.25 still leaves an
    #: order of magnitude of margin over the thing this cue exists to reject.
    flat_vs_3d_level: float = 0.25
    flat_vs_3d_frames: int = 2

    #: 0.8, deliberately high, because this cue's level is a remapped
    #: correlation: `(r + 1) / 2`. That puts *zero* correlation — pure noise, no
    #: relationship between nose offset and yaw at all — at exactly 0.5. Anything
    #: near 0.5 is therefore evidence of nothing, and a 0.5 gate would let a
    #: noisy flat presentation confirm itself as live. 0.8 corresponds to
    #: r >= 0.6: a genuinely positive relationship of the kind only a nose
    #: sitting off the eye plane produces.
    depth_pose_level: float = 0.8
    depth_pose_frames: int = 2

    #: A blink is already a discrete dip-and-recover event detected across the
    #: window, not a level that ramps — so one firing frame is the event itself,
    #: not a coincidence.
    blink_frames: int = 1

    #: Frames Light mode waits before auto-confirming, so the deny cues get a
    #: fair chance to fire first. At ~20fps this is well under a tenth of a
    #: second — imperceptible, and recognition itself takes longer — but without
    #: it a first-frame match could unlock before the glare/device check had ever
    #: run, which would make Light mode's "unless proven wrong" promise hollow.
    light_mode_minimum_frames: int = 3

    def level(self, cue: LivenessCue) -> float:
        return {
            LivenessCue.GLOSS_GLARE: self.gloss_level,
            LivenessCue.DEVICE_DETECTED: self.device_level,
            LivenessCue.FLAT_VS_3D: self.flat_vs_3d_level,
            LivenessCue.DEPTH_POSE: self.depth_pose_level,
            # Any confident blink reading is the event; see `blink_frames`.
            LivenessCue.BLINK: 0.5,
        }[cue]

    def frames(self, cue: LivenessCue) -> int:
        return {
            LivenessCue.GLOSS_GLARE: self.gloss_frames,
            LivenessCue.DEVICE_DETECTED: self.device_frames,
            LivenessCue.FLAT_VS_3D: self.flat_vs_3d_frames,
            LivenessCue.DEPTH_POSE: self.depth_pose_frames,
            LivenessCue.BLINK: self.blink_frames,
        }[cue]


DEFAULT_TUNING = LivenessTuning()


class DecisionKind(Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DENIED = "denied"


@dataclass(frozen=True)
class LivenessDecision:
    kind: DecisionKind = DecisionKind.PENDING
    #: For CONFIRMED, None means Light mode auto-confirmed rather than any cue
    #: firing. For DENIED, always the cue responsible.
    cue: Optional[LivenessCue] = None

    @property
    def is_pending(self) -> bool:
        """Not a failure — the scan should keep going."""
        return self.kind is DecisionKind.PENDING

    @property
    def is_confirmed(self) -> bool:
        return self.kind is DecisionKind.CONFIRMED

    @property
    def is_denied(self) -> bool:
        return self.kind is DecisionKind.DENIED

    @property
    def denial_reason(self) -> Optional[str]:
        """User-facing explanation for a denial."""
        if not self.is_denied:
            return None
        if self.cue is LivenessCue.GLOSS_GLARE:
            return "Screen glare detected — this looks like a photo on a display."
        if self.cue is LivenessCue.DEVICE_DETECTED:
            return (
                "A device-shaped rectangle was detected around the face — "
                "this looks like a photo or screen."
            )
        return "Liveness check failed."


PENDING = LivenessDecision()


@dataclass
class LivenessCueState:
    """Running state for one cue across a scan."""

    reading: CueReading = NO_READING
    #: How many frames this cue has counted so far this scan. Cumulative, not
    #: consecutive — matching the rule these cues are specified by ("has to
    #: appear a few times"), and more forgiving of the one-frame dropouts a
    #: landmarker routinely produces mid-scan.
    frames_counted: int = 0
    has_fired: bool = False

    def progress(self, threshold: int) -> float:
        """0..1 progress toward firing, for a debug console's progress bars."""
        if threshold <= 0:
            return 1.0 if self.has_fired else 0.0
        return min(1.0, self.frames_counted / threshold)


@dataclass(frozen=True)
class LivenessSnapshot:
    decision: LivenessDecision = PENDING
    mode: LivenessMode = LivenessMode.LIGHT
    cue_states: dict[LivenessCue, LivenessCueState] = field(default_factory=dict)
    frame_count: int = 0

    def state(self, cue: LivenessCue) -> LivenessCueState:
        return self.cue_states.get(cue, LivenessCueState())


EMPTY_SNAPSHOT = LivenessSnapshot()


class LivenessEvaluator:
    """The stateful decision core.

    Kept separate from the rolling-window driver in `analyzer.py` so the tests
    can drive the real firing and latching logic frame by frame with no camera —
    the same split upstream uses to make `tools/liveness_selftest.swift`
    possible.
    """

    def __init__(
        self,
        mode: LivenessMode = LivenessMode.LIGHT,
        tuning: LivenessTuning = DEFAULT_TUNING,
        enabled_cues: Optional[set[LivenessCue]] = None,
    ) -> None:
        self.mode = mode
        self.tuning = tuning
        #: A debug console can switch individual cues off to isolate one; the
        #: unlock path leaves this at "all enabled".
        self.enabled_cues: set[LivenessCue] = (
            set(ALL_CUES) if enabled_cues is None else set(enabled_cues)
        )
        self.states: dict[LivenessCue, LivenessCueState] = {}
        self.frames_observed = 0

    def reset(self) -> None:
        self.states = {}
        self.frames_observed = 0

    def observe(self, readings: dict[LivenessCue, CueReading]) -> LivenessSnapshot:
        """Fold one frame's readings in and return the decision as it now stands.

        Firing is latched: a cue that has fired stays fired for the rest of the
        scan, so a spoof tell that appears briefly cannot be waited out, and a
        blink early in the scan still counts later.
        """
        self.frames_observed += 1

        for cue in ALL_CUES:
            state = self.states.setdefault(cue, LivenessCueState())
            reading = readings.get(cue, NO_READING)
            state.reading = reading
            if reading.confidence > 0 and reading.level >= self.tuning.level(cue):
                state.frames_counted += 1
                if state.frames_counted >= self.tuning.frames(cue):
                    state.has_fired = True

        return LivenessSnapshot(
            decision=self._current_decision(),
            mode=self.mode,
            cue_states={c: replace(s) for c, s in self.states.items()},
            frame_count=self.frames_observed,
        )

    def _current_decision(self) -> LivenessDecision:
        # Deny is evaluated first and is unconditional — it overrides a
        # confirmation that already happened, which is the whole point of
        # splitting the cues by role rather than scoring them together.
        for cue in ALL_CUES:
            if (
                cue.role is CueRole.DENY
                and cue in self.enabled_cues
                and self.states.get(cue, LivenessCueState()).has_fired
            ):
                return LivenessDecision(DecisionKind.DENIED, cue)

        if self.mode is LivenessMode.LIGHT:
            if self.frames_observed >= self.tuning.light_mode_minimum_frames:
                return LivenessDecision(DecisionKind.CONFIRMED, None)
            return PENDING

        for cue in ALL_CUES:
            if (
                cue.role is CueRole.CONFIRM
                and cue in self.enabled_cues
                and self.states.get(cue, LivenessCueState()).has_fired
            ):
                return LivenessDecision(DecisionKind.CONFIRMED, cue)

        return PENDING


def gloss_glare(frame: Optional[LivenessFrame]) -> CueReading:
    """Skin gives many small scattered specular points; glass gives one big flat
    blob. `specular_fraction` alone would fire on a bright forehead, so it is
    gated by how concentrated that glare is."""
    if frame is None or frame.glare is None:
        return NO_READING
    glare = frame.glare
    fraction_score = ramp(glare.specular_fraction, 0.01, 0.08)
    cluster_factor = ramp(glare.specular_cluster_ratio, 0.3, 1.0)
    level = fraction_score * (0.3 + 0.7 * cluster_factor)
    # Below ~50 native px of face there is not enough detail to tell a glare blob
    # from a bright patch; ramps to full trust by ~130px.
    confidence = ramp(glare.crop_pixel_width, 50.0, 130.0)
    return CueReading(level=level, confidence=confidence)


def device_detected(frame: Optional[LivenessFrame]) -> CueReading:
    """Raw overlap fraction from the bezel detector, used directly rather than
    re-scaled: the fire threshold is expressed in the same units (a fraction of
    the face box covered by a device-shaped rectangle), so what a debug console
    shows is exactly what the threshold compares."""
    if frame is None or frame.device_overlap_fraction is None:
        return NO_READING
    overlap = min(max(frame.device_overlap_fraction, 0.0), 1.0)
    return CueReading(level=overlap, confidence=1.0)


def readings(
    window: Sequence[LivenessFrame], geometry: GeometryLivenessResult
) -> dict[LivenessCue, CueReading]:
    """Turn a rolling window (plus the geometry result computed from it) into
    this frame's reading for every cue.

    Deny cues read the *latest frame only* — they are per-frame appearance
    measurements, and the evaluator's frame counting is what supplies the
    "sustained over several frames" requirement. Confirm cues are inherently
    cross-frame (parallax, yaw correlation, a blink's dip-and-recover) so they
    read the whole window.
    """
    last = window[-1] if window else None
    return {
        LivenessCue.GLOSS_GLARE: gloss_glare(last),
        LivenessCue.DEVICE_DETECTED: device_detected(last),
        LivenessCue.FLAT_VS_3D: geometry.planar_reading,
        LivenessCue.DEPTH_POSE: pose_depth_consistency(window),
        LivenessCue.BLINK: blink_dynamics(window),
    }
