"""Liveness detection — a Linux port of Glance's five-cue liveness model.

Upstream: https://github.com/jonnyoo/glance (`glance/Liveness/`), MIT.

The public surface is the analyzer plus the value types a caller needs to read
its decision:

    from glanced.liveness import LivenessAnalyzer, LivenessMode

    analyzer = LivenessAnalyzer()
    analyzer.mode_provider = lambda: LivenessMode.HEAVY
    snapshot = analyzer.observe(frame)
    if snapshot.decision.is_denied:
        ...

See `cues.py` for why there is no overall liveness percentage to threshold.
"""

from .analyzer import LivenessAnalyzer
from .cues import (
    ALL_CUES,
    DEFAULT_TUNING,
    CueRole,
    DecisionKind,
    LivenessCue,
    LivenessCueState,
    LivenessDecision,
    LivenessEvaluator,
    LivenessMode,
    LivenessSnapshot,
    LivenessTuning,
)
from .frame import (
    ALL_REGIONS,
    NO_READING,
    CueReading,
    GlareSample,
    LandmarkPoint,
    LandmarkRegion,
    LivenessFrame,
)
from .planar import GeometryLivenessResult, GeometryTuning

__all__ = [
    "ALL_CUES",
    "ALL_REGIONS",
    "DEFAULT_TUNING",
    "NO_READING",
    "CueReading",
    "CueRole",
    "DecisionKind",
    "GeometryLivenessResult",
    "GeometryTuning",
    "GlareSample",
    "LandmarkPoint",
    "LandmarkRegion",
    "LivenessAnalyzer",
    "LivenessCue",
    "LivenessCueState",
    "LivenessDecision",
    "LivenessEvaluator",
    "LivenessFrame",
    "LivenessMode",
    "LivenessSnapshot",
    "LivenessTuning",
]
