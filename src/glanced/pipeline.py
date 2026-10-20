"""The unlock gate: recognition and liveness, combined.

Face recognition and liveness detection run independently and must *both*
succeed. This mirrors upstream's rule exactly — Glance will not unlock simply
because a face matches — with one condition dropped, because Linux does not
need it.

An unlock requires all of:

1. An authorized session (the encryption key is unwrapped and in memory).
2. The screen is actually locked.
3. An enabled identity matches above the configured similarity threshold.
4. Liveness accepts the detected face.

Conditions 3 and 4 must hold for *the same face*. Both gates accumulate over a
scan — liveness cues latch, and a match is remembered — so without binding them
a photo of the owner could satisfy recognition and a different, live person
could then blink their way through liveness (or the other way round). The
pipeline therefore follows one subject at a time: a face that loses detection,
jumps position, or stops resembling the face it started as is a new subject,
and both gates start over for it. An unlock is only ever granted on a frame
whose own embedding matches.

Upstream has a fifth: Accessibility permission, so it can type the password into
the lock screen. There is no equivalent here, and no password. The daemon
answers a PAM conversation; the credential never exists.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np

from .liveness import LivenessAnalyzer, LivenessFrame, LivenessMode
from .store import EnrollmentStore, Identity

#: Cosine similarity above which an ArcFace embedding is considered a match.
#: 0.36 is the conventional operating point for this model family; raise it to
#: trade convenience for strictness.
DEFAULT_MATCH_THRESHOLD = 0.36

#: How long a scan keeps looking before giving up. Liveness may sit PENDING for
#: this whole duration without that being a failure — see `cues.py`.
DEFAULT_SCAN_TIMEOUT = 8.0

#: Cosine similarity a frame's embedding must keep with the first embedding of
#: the subject being followed. Stricter than the match threshold: this compares
#: one sitting against itself — same camera, same light, seconds apart — where
#: the same face scores far higher than it does against an enrollment.
SAME_FACE_THRESHOLD = 0.5

#: Overlap (intersection over union) a face box must keep with the previous
#: frame's to be the same subject. Head motion between frames at camera rate
#: stays well above this; a face swapped in elsewhere in the frame does not.
SAME_FACE_MIN_OVERLAP = 0.3


class Outcome(Enum):
    PENDING = "pending"
    UNLOCKED = "unlocked"
    NO_MATCH = "no_match"
    SPOOF_DENIED = "spoof_denied"
    TIMED_OUT = "timed_out"
    #: No face was ever seen during the scan.
    NO_FACE = "no_face"
    #: The daemon has no decrypted enrollment in memory (`glancectl arm`).
    NOT_ARMED = "not_armed"
    #: Camera or model failure. Never an unlock; the reason says what broke.
    ERROR = "error"
    #: Too many failed scans in a row; the camera is not opened again until
    #: the cooldown passes. The reason says how long.
    LOCKED_OUT = "locked_out"


@dataclass
class ScanResult:
    outcome: Outcome
    identity: Optional[Identity] = None
    similarity: Optional[float] = None
    reason: Optional[str] = None


class UnlockPipeline:
    def __init__(
        self,
        store: EnrollmentStore,
        *,
        mode: LivenessMode = LivenessMode.LIGHT,
        match_threshold: float = DEFAULT_MATCH_THRESHOLD,
        scan_timeout: float = DEFAULT_SCAN_TIMEOUT,
    ) -> None:
        self.store = store
        self.match_threshold = match_threshold
        self.scan_timeout = scan_timeout
        self.analyzer = LivenessAnalyzer()
        self.analyzer.mode_provider = lambda: mode
        self._started: Optional[float] = None
        self._match: Optional[tuple[Identity, float]] = None
        #: The subject being followed: its first embedding and its last box.
        self._anchor: Optional[np.ndarray] = None
        self._box: Optional[tuple[float, float, float, float]] = None

    def begin(self) -> None:
        self._started = time.monotonic()
        self._new_subject()

    def _new_subject(self) -> None:
        """Forget everything either gate learned about the previous face."""
        self.analyzer.reset()
        self._match = None
        self._anchor = None
        self._box = None

    def face_lost(self) -> None:
        """A frame with no face: whoever comes back into view is a new subject.

        Without this, a photo could be lowered out of view and a live face
        raised in its place, with nothing in between for `observe` to see.
        """
        self._new_subject()

    def expired(self) -> Optional[ScanResult]:
        """The terminal result if the scan has run out of time, else None.

        Split out from `observe` so a scan with *no face in frame* still ends:
        `observe` is only ever called with a face, and a user who walked away
        must not leave the camera open until the PAM conversation gives up.
        """
        if self._started is None or time.monotonic() - self._started <= self.scan_timeout:
            return None
        if self._match is None:
            return ScanResult(Outcome.NO_MATCH, reason="No enrolled face matched.")
        return ScanResult(
            Outcome.TIMED_OUT, reason="Could not confirm a real face before the scan expired."
        )

    def observe(
        self,
        liveness_frame: LivenessFrame,
        embedding: Optional[np.ndarray],
        bounding_box: Optional[tuple[float, float, float, float]] = None,
    ) -> ScanResult:
        """Fold one frame in and report where the scan stands.

        `embedding` may be None for a frame that had a face but no usable
        alignment. Liveness is still fed, deliberately: keeping the window dense
        is what makes it an independent gate rather than one starved by
        recognition's own confidence. Such a frame is tied to the subject by
        `bounding_box` (x, y, w, h) alone, and can never be the one that unlocks.
        """
        if self._started is None:
            raise RuntimeError("call begin() before observe()")

        if not self._same_subject(embedding, bounding_box):
            self._new_subject()
        if self._anchor is None and embedding is not None:
            self._anchor = embedding
        if bounding_box is not None:
            self._box = bounding_box

        snapshot = self.analyzer.observe(liveness_frame)

        if snapshot.decision.is_denied:
            return ScanResult(Outcome.SPOOF_DENIED, reason=snapshot.decision.denial_reason)

        current = None
        if embedding is not None:
            current = self.store.match(embedding, self.match_threshold)
            if self._match is None:
                self._match = current

        # Both gates, in either order, but on one face — a match with liveness
        # still pending is not an unlock, confirmed liveness with no match is
        # not either, and neither is liveness from this subject paired with a
        # match remembered from another. This frame must match by itself.
        if current is not None and snapshot.decision.is_confirmed:
            identity, score = current
            return ScanResult(Outcome.UNLOCKED, identity=identity, similarity=score)

        return self.expired() or ScanResult(Outcome.PENDING)

    def _same_subject(
        self,
        embedding: Optional[np.ndarray],
        bounding_box: Optional[tuple[float, float, float, float]],
    ) -> bool:
        if bounding_box is not None and self._box is not None:
            if _overlap(bounding_box, self._box) < SAME_FACE_MIN_OVERLAP:
                return False
        if embedding is not None and self._anchor is not None:
            if float(np.dot(embedding, self._anchor)) < SAME_FACE_THRESHOLD:
                return False
        return True


def _overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """Intersection over union of two (x, y, w, h) boxes."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    width = min(ax + aw, bx + bw) - max(ax, bx)
    height = min(ay + ah, by + bh) - max(ay, by)
    if width <= 0 or height <= 0:
        return 0.0
    intersection = width * height
    return intersection / (aw * ah + bw * bh - intersection)
