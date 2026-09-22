"""The guided enrollment poses, and what counts as holding one.

Centre plus the four cardinal directions, in the order the user is walked
through them. Ported from the macOS app's `EnrollmentPose`, which sweeps all
eight compass points; the bands are the same shape, but the sign conventions
are asserted here rather than inherited, because MediaPipe's facial
transformation matrix is not Vision's.

Nothing downstream counts poses — the ring divides the circle by however many
directional poses :data:`POSES` holds, and the gate reads each pose's own
bands — so adding the four diagonals back is an edit to this tuple alone.

Nothing in this module touches a camera or a model — it is arithmetic over two
angles, so the whole gating rule is testable without hardware.

## On signs

`yaw` and `pitch` arrive in radians from
:func:`glanced.liveness.features.yaw_from_transformation_matrix` and its pitch
counterpart, straight out of the rotation matrix. Which direction is positive
is a property of the matrix, not of anything we chose, so it is named once
here and used everywhere:

* **yaw** is positive when the head turns to the subject's *left*,
* **pitch** is positive when the chin comes *down*.

`YAW_SIGN` and `PITCH_SIGN` exist to flip either one without touching a band.
They are the single knob to turn if a camera stack disagrees; see
`glancectl enroll --gui --debug`, which prints the live readings.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional

#: Flip either of these — and only these — if the live readout says a pose
#: lights the sector opposite the one asked for.
YAW_SIGN = 1.0
PITCH_SIGN = 1.0


class Band(Enum):
    """Which side of an axis a pose wants the head on."""

    NEGATIVE = -1
    CENTRE = 0
    POSITIVE = 1


@dataclass(frozen=True)
class Pose:
    #: Stored alongside each embedding, so an identity records which pose
    #: every row came from.
    name: str
    #: What the user is asked to do.
    instruction: str
    #: Head turned left/right, or held straight.
    yaw_band: Band
    #: Chin up/down, or held level.
    pitch_band: Band
    #: Compass angle of this pose's ring sector, 0 = up, clockwise. `None`
    #: for the centre pose, which pulses the whole ring instead of claiming
    #: a sector of it.
    compass_angle: Optional[float]


#: The order matters: centre first, so the easy pose is the one that catches a
#: user who has not yet worked out what is being asked of them, then a
#: clockwise sweep from the left so the head travels a continuous path rather
#: than jumping across the circle between poses.
#:
#: Five, not the macOS sweep's nine. The four diagonals ask for a compound
#: turn that is markedly harder to explain in one line and to hold steady, and
#: they buy the least: a template already covering centre, both profiles and
#: both chin extremes has the corners roughly bracketed. Ten rows is also a
#: shorter thing to sit through, and enrollment nobody finishes helps nobody.
POSES: tuple[Pose, ...] = (
    Pose("centre", "Look straight at the camera", Band.CENTRE, Band.CENTRE, None),
    Pose("left", "Turn your head slightly left", Band.POSITIVE, Band.CENTRE, 270.0),
    Pose("top", "Tilt your chin up slightly", Band.CENTRE, Band.NEGATIVE, 0.0),
    Pose("right", "Turn your head slightly right", Band.NEGATIVE, Band.CENTRE, 90.0),
    Pose("bottom", "Tilt your chin down slightly", Band.CENTRE, Band.POSITIVE, 180.0),
)

#: The poses that own a slice of the ring. The centre pose owns none — it
#: pulses the whole thing instead — so this is what the ring divides by.
SECTOR_POSES: tuple[Pose, ...] = tuple(p for p in POSES if p.compass_angle is not None)
SECTOR_COUNT = len(SECTOR_POSES)
SECTOR_DEGREES = 360.0 / SECTOR_COUNT


@dataclass(frozen=True)
class Tuning:
    """The bands, in radians.

    An *inner* threshold is how far off-centre counts as a turn; an *outer*
    cap rejects a head turned so far the landmarker is guessing at half the
    face. `centre_tolerance` is the matching window for "hold still".
    """

    yaw_inner: float = 0.25
    yaw_centre: float = 0.18
    yaw_outer: float = 1.2
    pitch_inner: float = 0.20
    pitch_centre: float = 0.15
    pitch_outer: float = 0.9

    #: A pose the user cannot hit in this long widens its bands by
    #: `widen_factor`. An unusual camera height or a chair that sits low
    #: should not be able to strand somebody on step four forever.
    stall_seconds: float = 12.0
    widen_factor: float = 1.25


TUNING = Tuning()

#: Samples per pose. Five poses x 2 = 10 rows — enough for a stable template
#: without making anyone hold a pose long enough to resent it. It lives here
#: rather than in `glanced.enroll` so the CLI can name it as a flag default
#: without importing the camera and the models to do it.
SAMPLES_PER_POSE = 2


def _axis_matches(value: float, band: Band, *, inner: float, centre: float, outer: float, factor: float) -> bool:
    if band is Band.CENTRE:
        return abs(value) < centre * factor
    if band is Band.POSITIVE:
        return inner / factor < value < outer
    return -outer < value < -inner / factor


def matches(
    pose: Pose,
    yaw: Optional[float],
    pitch: Optional[float],
    *,
    widened: bool = False,
    tuning: Tuning = TUNING,
) -> bool:
    """Whether a head at `yaw`/`pitch` radians is holding `pose`.

    A missing or non-finite angle is not a match: without a pose estimate
    there is no way to tell the directions apart, and quietly treating that
    as "centre" would enrol five copies of the same frontal capture.
    """
    if yaw is None or pitch is None:
        return False
    if not (math.isfinite(yaw) and math.isfinite(pitch)):
        return False

    factor = tuning.widen_factor if widened else 1.0
    return _axis_matches(
        YAW_SIGN * yaw,
        pose.yaw_band,
        inner=tuning.yaw_inner,
        centre=tuning.yaw_centre,
        outer=tuning.yaw_outer,
        factor=factor,
    ) and _axis_matches(
        PITCH_SIGN * pitch,
        pose.pitch_band,
        inner=tuning.pitch_inner,
        centre=tuning.pitch_centre,
        outer=tuning.pitch_outer,
        factor=factor,
    )


def sector_pose(compass_angle: float) -> Optional[Pose]:
    """The pose whose ring sector a compass angle falls in, or `None`.

    Ticks are bucketed to the nearest sector centre, so every angle lands in
    exactly one directional sector however many there are. The centre pose
    owns no sector and is never returned.

    Boundaries are resolved by a half-sector shift and a floor, not by
    rounding: an angle landing exactly on a boundary is common — with 80 ticks
    and four sectors, four of them do — and `round` breaks those ties to even,
    which hands two opposite sectors an extra tick each and leaves the ring
    visibly lopsided. Flooring sends every boundary tick to the same side.
    """
    index = int((compass_angle + SECTOR_DEGREES / 2.0) // SECTOR_DEGREES) % SECTOR_COUNT
    raw = index * SECTOR_DEGREES
    return next((p for p in SECTOR_POSES if p.compass_angle == raw), None)
