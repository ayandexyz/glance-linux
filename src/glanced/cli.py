"""glancectl — enrollment, status, and the liveness self-test.

`selftest` is the Linux counterpart of upstream's Face Lab: it drives the real
cue and evaluator logic against synthetic data and prints every cue's reading
and fire count, so the tuning constants can be inspected and retuned without a
camera. It is the fastest way to see whether a change to the decision model did
what you meant.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import ipc


def _selftest(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
    try:
        import synthetic
        from synthetic import SyntheticConfig
    except ImportError:
        print("selftest needs the tests/ directory from a source checkout", file=sys.stderr)
        return 2

    from .liveness import DEFAULT_TUNING, LivenessAnalyzer, LivenessCue, LivenessMode

    scenarios = {
        "live face, turning": synthetic.live_face_window(
            frame_count=16,
            config=SyntheticConfig(noise_px=args.noise),
            ear_series=synthetic.blink_ear_series(16),
        ),
        "flat photo, tilted": synthetic.flat_photo_window(
            frame_count=16, config=SyntheticConfig(noise_px=args.noise)
        ),
        "photo on a device": synthetic.flat_photo_window(
            frame_count=16, config=SyntheticConfig(noise_px=args.noise), device_overlap=0.85
        ),
        "live face, perfectly still": synthetic.still_face_window(
            frame_count=16, config=SyntheticConfig(noise_px=args.noise)
        ),
    }

    mode = LivenessMode(args.mode)
    print(f"mode: {mode.value} — {mode.summary}    landmark noise: {args.noise}px\n")

    for label, window in scenarios.items():
        analyzer = LivenessAnalyzer()
        analyzer.mode_provider = lambda: mode
        snapshot = None
        for frame in window:
            snapshot = analyzer.observe(frame)

        decision = snapshot.decision
        verdict = decision.kind.value.upper()
        if decision.cue is not None:
            verdict += f" by {decision.cue.title}"
        print(f"{label:<28} {verdict}")
        for cue in LivenessCue:
            state = snapshot.state(cue)
            threshold = DEFAULT_TUNING.frames(cue)
            marker = "FIRED" if state.has_fired else "     "
            print(
                f"    {cue.title:<18} {cue.role.value:<8} "
                f"level={state.reading.level:5.3f} conf={state.reading.confidence:5.3f} "
                f"{state.frames_counted}/{threshold} {marker}"
            )
        geometry = analyzer.last_geometry
        if geometry.excess_ratio is not None:
            print(
                f"    [geometry] excess={geometry.excess_ratio:.3f} "
                f"coherence={geometry.coherence:.3f} pairs={geometry.pairs_analyzed}"
            )
        print()
    return 0


def _live(args: argparse.Namespace) -> int:
    from .liveness import LivenessMode
    from .livetest import run

    return run(
        mode=LivenessMode(args.mode),
        device=args.device,
        scan_seconds=args.scan_seconds,
        preview=args.preview,
    )


def _status(args: argparse.Namespace) -> int:
    try:
        response = ipc.request(ipc.STATUS_SOCKET, "status")
    except (OSError, ConnectionError) as error:
        print(f"daemon not reachable: {error}", file=sys.stderr)
        return 1
    payload = response.payload
    print(f"armed:      {payload.get('armed')}")
    print(f"liveness:   {payload.get('mode')}")
    for identity in payload.get("identities", []):
        flag = "enabled" if identity["enabled"] else "disabled"
        print(f"  - {identity['name']} ({flag})")
    return 0


def _daemon(args: argparse.Namespace) -> int:
    from .daemon import Daemon
    from .liveness import LivenessMode
    from .store import EnrollmentStore

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # TODO: unwrap the enrollment store here once `glancectl enroll` lands; the
    # daemon starts disarmed and serves status until then.
    Daemon(EnrollmentStore(), mode=LivenessMode(args.mode)).serve()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glancectl")
    subparsers = parser.add_subparsers(dest="command", required=True)

    selftest = subparsers.add_parser(
        "selftest", help="run the liveness decision model against synthetic data"
    )
    selftest.add_argument("--mode", choices=["light", "heavy"], default="heavy")
    selftest.add_argument(
        "--noise", type=float, default=0.5, help="landmark jitter in pixels (default: 0.5)"
    )
    selftest.set_defaults(func=_selftest)

    live = subparsers.add_parser(
        "live", help="run the liveness model against your webcam (no unlock, no recognition)"
    )
    live.add_argument("--mode", choices=["light", "heavy"], default="heavy")
    live.add_argument("--device", default="/dev/video0")
    live.add_argument("--scan-seconds", type=float, default=10.0)
    live.add_argument("--preview", action="store_true", help="also show the camera window")
    live.set_defaults(func=_live)

    status = subparsers.add_parser("status", help="query the running daemon")
    status.set_defaults(func=_status)

    daemon = subparsers.add_parser("daemon", help="run the service")
    daemon.add_argument("--mode", choices=["light", "heavy"], default="light")
    daemon.set_defaults(func=_daemon)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
