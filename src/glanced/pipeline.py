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

    def begin(self) -> None:
        self.analyzer.reset()
        self._started = time.monotonic()
        self._match = None

    @property
    def matched(self) -> bool:
        """True once an identity has matched — after which the caller can stop
        paying for embeddings and let liveness finish on its own."""
        return self._match is not None

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
        self, liveness_frame: LivenessFrame, embedding: Optional[np.ndarray]
    ) -> ScanResult:
        """Fold one frame in and report where the scan stands.

        `embedding` may be None for a frame that had a face but no usable
        alignment. Liveness is still fed, deliberately: keeping the window dense
        is what makes it an independent gate rather than one starved by
        recognition's own confidence.
        """
        if self._started is None:
            raise RuntimeError("call begin() before observe()")

        snapshot = self.analyzer.observe(liveness_frame)

        if snapshot.decision.is_denied:
            return ScanResult(Outcome.SPOOF_DENIED, reason=snapshot.decision.denial_reason)

        if embedding is not None and self._match is None:
            self._match = self.store.match(embedding, self.match_threshold)

        # Both gates, in either order — a match with liveness still pending is
        # not an unlock, and confirmed liveness with no match is not either.
        if self._match is not None and snapshot.decision.is_confirmed:
            identity, score = self._match
            return ScanResult(Outcome.UNLOCKED, identity=identity, similarity=score)

        return self.expired() or ScanResult(Outcome.PENDING)
