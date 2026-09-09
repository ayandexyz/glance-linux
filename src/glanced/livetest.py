"""`glancectl live` — point the liveness model at your actual webcam.

A camera-in-the-loop harness, not the unlock path: it runs recognition-free, so
nothing here can authorize anything. Its whole job is to let you hold up your
face, then a printed photo, then a phone, and watch which cues fire and why.

Scans run back to back. Each ends when a cue decides or the scan times out, the
verdict is logged, and a new scan begins — so you can try several presentations
in one session without restarting.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np

from .camera import Camera, CameraConfig, render_crop, to_working_resolution
from .landmarker import DEFAULT_TASK_PATH, Landmarker
from .liveness import DEFAULT_TUNING, LivenessAnalyzer, LivenessCue, LivenessMode
from .liveness.features import extract

BAR_WIDTH = 14
GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m",
)


def _bar(fraction: float, width: int = BAR_WIDTH) -> str:
    filled = int(round(max(0.0, min(1.0, fraction)) * width))
    return "#" * filled + "." * (width - filled)


def _render(snapshot, geometry, fps: float, mode: LivenessMode, elapsed: float, log: list[str]) -> None:
    rows = [
        f"{BOLD}glance liveness — live camera test{RESET}   "
        f"mode={mode.value}  {fps:5.1f} fps  scan {elapsed:4.1f}s",
        "",
    ]
    for cue in LivenessCue:
        state = snapshot.state(cue)
        threshold = DEFAULT_TUNING.frames(cue)
        reading = state.reading

        if state.has_fired:
            colour = RED if cue.role.value == "deny" else GREEN
            status = f"{colour}FIRED{RESET}"
        elif reading.confidence <= 0:
            status = f"{DIM}abstain{RESET}"
        else:
            status = f"{state.frames_counted}/{threshold}"

        level = f"{DIM}   ---{RESET}" if reading.confidence <= 0 else f"{reading.level:6.3f}"
        rows.append(
            f"  {cue.title:<16} {DIM}{cue.role.value:<7}{RESET} "
            f"[{_bar(reading.level if reading.confidence > 0 else 0)}] {level} "
            f"conf={reading.confidence:4.2f}  {status}"
        )

    decision = snapshot.decision
    if decision.is_denied:
        verdict = f"{RED}{BOLD}DENIED{RESET} — {decision.denial_reason}"
    elif decision.is_confirmed:
        by = decision.cue.title if decision.cue else "light mode"
        verdict = f"{GREEN}{BOLD}LIVENESS CONFIRMED{RESET} (by {by})"
    else:
        verdict = f"{YELLOW}scanning...{RESET}"

    rows += ["", f"  {verdict}", ""]
    if geometry.excess_ratio is not None:
        rows.append(
            f"  {DIM}geometry: excess={geometry.excess_ratio:5.3f} "
            f"coherence={geometry.coherence:5.3f} pairs={geometry.pairs_analyzed} "
            f"yaw range={geometry.diagnostic_ratios.get('yaw range (deg)', 0):.1f} deg{RESET}"
        )
    else:
        rows.append(f"  {DIM}geometry: not enough motion or rotation yet{RESET}")

    rows += ["", f"  {DIM}turn your head slowly to give the depth cues something to measure{RESET}"]
    if log:
        rows += ["", f"  {BOLD}results{RESET}"] + [f"    {line}" for line in log[-8:]]
    rows += ["", f"  {DIM}ctrl-c to stop{RESET}"]

    sys.stdout.write("\033[H\033[J" + "\n".join(rows) + "\n")
    sys.stdout.flush()


def run(
    *,
    mode: LivenessMode = LivenessMode.HEAVY,
    device: str = "/dev/video0",
    scan_seconds: float = 10.0,
    task_path: Path = DEFAULT_TASK_PATH,
    preview: bool = False,
) -> int:
    landmarker = Landmarker(task_path)
    analyzer = LivenessAnalyzer()
    analyzer.mode_provider = lambda: mode

    log: list[str] = []
    scan_started = time.monotonic()
    frame_times: list[float] = []
    settled_at: Optional[float] = None

    try:
        with Camera(CameraConfig(device=device)) as camera:
            sys.stdout.write("\033[?25l")  # hide cursor
            for native in camera.frames():
                now = time.monotonic()
                frame_times = [t for t in frame_times if now - t < 1.0] + [now]

                working, scale = to_working_resolution(native)
                face = landmarker.detect(working, int(now * 1000))

                if face is not None:
                    # The glare cue wants native-resolution detail, so the crop
                    # is taken from the full frame, not the downscaled one.
                    native_box = tuple(v / scale for v in face.bounding_box)
                    frame = extract(
                        face.mesh,
                        face.bounding_box,
                        frame_rgb=working,
                        face_crop=render_crop(native, native_box),
                        yaw=face.yaw,
                        timestamp=now,
                    )
                    snapshot = analyzer.observe(frame)
                else:
                    snapshot = analyzer.last_snapshot

                elapsed = now - scan_started
                _render(snapshot, analyzer.last_geometry, len(frame_times), mode, elapsed, log)

                if preview:
                    import cv2

                    cv2.imshow("glance", cv2.cvtColor(working, cv2.COLOR_RGB2BGR))
                    cv2.waitKey(1)

                decided = snapshot.decision.is_denied or snapshot.decision.is_confirmed
                if decided and settled_at is None:
                    settled_at = now
                    if snapshot.decision.is_denied:
                        label = f"{RED}DENIED{RESET}  by {snapshot.decision.cue.title}"
                    else:
                        by = snapshot.decision.cue.title if snapshot.decision.cue else "light mode"
                        label = f"{GREEN}LIVE{RESET}    by {by}"
                    log.append(f"{time.strftime('%H:%M:%S')}  {label}  after {elapsed:.1f}s")

                # Hold a settled verdict on screen briefly, then rescan, so
                # several presentations can be tried in one session.
                if (settled_at and now - settled_at > 2.0) or elapsed > scan_seconds:
                    if not decided:
                        log.append(
                            f"{time.strftime('%H:%M:%S')}  {YELLOW}PENDING{RESET} "
                            f"— nothing confirmed in {scan_seconds:.0f}s"
                        )
                    analyzer.reset()
                    scan_started = now
                    settled_at = None
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h\n")  # restore cursor
        landmarker.close()
        if preview:
            import cv2

            cv2.destroyAllWindows()
    return 0
