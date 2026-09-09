"""glancectl — the one command for everything that is not the daemon loop.

    fetch-model   download the landmarker and ArcFace networks
    enroll        capture a face into the encrypted store
    forget        remove an identity from the store
    arm / disarm  give the running daemon its key, or take it back
    authenticate  run one unlock scan through the daemon (what PAM does)
    status        what the daemon knows, for humans or as JSON for the plugin
    live          the liveness model against your webcam, no recognition
    selftest      the liveness model against synthetic faces, no camera
    daemon        the service itself

`selftest` is the Linux counterpart of upstream's Face Lab: it drives the real
cue and evaluator logic against synthetic data and prints every cue's reading
and fire count, so the tuning constants can be inspected and retuned without a
camera. `live` does the same against real frames.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import ipc, paths

OUTCOME_TEXT = {
    "unlocked": "UNLOCKED",
    "no_match": "no match",
    "spoof_denied": "SPOOF DENIED",
    "timed_out": "timed out",
    "no_face": "no face seen",
    "not_armed": "daemon not armed",
    "error": "error",
}


# --- helpers ----------------------------------------------------------------


def _read_passphrase(args: argparse.Namespace, *, confirm: bool = False) -> str:
    if getattr(args, "passphrase_stdin", False):
        line = sys.stdin.readline()
        if not line:
            raise SystemExit("no passphrase on stdin")
        return line.rstrip("\n")
    first = getpass.getpass("Passphrase: ")
    if not first:
        raise SystemExit("empty passphrase")
    if confirm and getpass.getpass("Confirm passphrase: ") != first:
        raise SystemExit("passphrases do not match")
    return first


def _daemon_request(socket_path: Path, verb: str, payload: Optional[dict] = None, timeout: float = 15.0) -> ipc.Response:
    try:
        return ipc.request(socket_path, verb, payload, timeout=timeout)
    except FileNotFoundError:
        raise SystemExit("daemon not running (systemctl --user start glanced)")
    except (OSError, ConnectionError) as error:
        raise SystemExit(f"daemon not reachable: {error}")


def _emit(args: argparse.Namespace, payload: dict, *, human) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload))
    else:
        human(payload)


# --- commands -----------------------------------------------------------------


def _fetch_model(args: argparse.Namespace) -> int:
    from . import models

    models.fetch(args.variant, force=args.force)
    return 0


def _enroll(args: argparse.Namespace) -> int:
    from . import store as store_module
    from .scan import FaceProcessor

    store_path = paths.STORE_PATH
    fresh = not store_path.exists()
    if fresh:
        print("No enrollment yet. Choose a passphrase; it encrypts your face data at rest.")
    passphrase = _read_passphrase(args, confirm=fresh)
    try:
        store = store_module.load(passphrase, store_path)
    except Exception:
        print("wrong passphrase", file=sys.stderr)
        return 1

    try:
        processor = FaceProcessor()
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 2

    print(f"\nEnrolling '{args.name}': {args.captures} captures from {args.device}.\n")
    try:
        from .enroll import capture_embeddings

        embeddings = capture_embeddings(
            processor,
            device=args.device,
            count=args.captures,
            on_prompt=lambda i, n, text: print(f"  [{i}/{n}] {text} ...", flush=True),
            on_capture=lambda i, n, score: print(f"        captured (self-similarity {score:.2f})", flush=True),
        )
    except (TimeoutError, RuntimeError) as error:
        print(f"\nenrollment failed: {error}", file=sys.stderr)
        return 1
    finally:
        processor.close()

    import numpy as np

    rows = np.stack(embeddings).astype(np.float32)
    existing = next((i for i in store.identities if i.name == args.name), None)
    if existing is not None:
        existing.embeddings = np.vstack([existing.embeddings, rows])
        existing.enabled = True
    else:
        store.identities.append(store_module.Identity(name=args.name, embeddings=rows))
    store_module.save(store, passphrase, store_path)
    total = sum(len(i.embeddings) for i in store.identities if i.name == args.name)
    print(f"\nsaved: '{args.name}' now has {total} captures in {store_path}")

    # Hand the running daemon the new store; harmless if it is not running.
    try:
        response = ipc.request(ipc.AUTH_SOCKET, "arm", {"passphrase": passphrase, "remember": args.remember}, timeout=5.0)
        print("daemon armed" if response.ok else f"daemon not armed: {response.payload.get('error')}")
    except (OSError, ConnectionError):
        print("daemon not running; it will need `glancectl arm` once it is")
    return 0


def _forget(args: argparse.Namespace) -> int:
    from . import store as store_module

    if not paths.STORE_PATH.exists():
        print("nothing enrolled", file=sys.stderr)
        return 1
    passphrase = _read_passphrase(args)
    try:
        store = store_module.load(passphrase, paths.STORE_PATH)
    except Exception:
        print("wrong passphrase", file=sys.stderr)
        return 1
    before = len(store.identities)
    store.identities = [i for i in store.identities if i.name != args.name]
    if len(store.identities) == before:
        print(f"no identity named '{args.name}'", file=sys.stderr)
        return 1
    store_module.save(store, passphrase, paths.STORE_PATH)
    print(f"removed '{args.name}'")
    try:
        ipc.request(ipc.AUTH_SOCKET, "arm", {"passphrase": passphrase}, timeout=5.0)
    except (OSError, ConnectionError):
        pass
    return 0


def _arm(args: argparse.Namespace) -> int:
    passphrase = _read_passphrase(args)
    response = _daemon_request(ipc.AUTH_SOCKET, "arm", {"passphrase": passphrase, "remember": args.remember})
    if not response.ok:
        print(response.payload.get("error", "failed"), file=sys.stderr)
        return 1
    _emit(args, response.payload, human=lambda p: print(f"armed with {len(p['identities'])} identit{'y' if len(p['identities']) == 1 else 'ies'}"))
    return 0


def _disarm(args: argparse.Namespace) -> int:
    response = _daemon_request(ipc.AUTH_SOCKET, "disarm", {"forget": args.forget})
    _emit(args, response.payload, human=lambda p: print("disarmed"))
    return 0


def _authenticate(args: argparse.Namespace) -> int:
    response = _daemon_request(ipc.AUTH_SOCKET, "authenticate", timeout=60.0)
    payload = response.payload

    def human(p: dict) -> None:
        text = OUTCOME_TEXT.get(p.get("outcome"), p.get("outcome"))
        if p.get("identity"):
            text += f" as {p['identity']} (similarity {p.get('similarity')})"
        if p.get("reason"):
            text += f" — {p['reason']}"
        print(text)

    _emit(args, payload, human=human)
    return 0 if response.ok else 1


def _status(args: argparse.Namespace) -> int:
    try:
        response = ipc.request(ipc.STATUS_SOCKET, "status", timeout=5.0)
        payload = {"reachable": True, **response.payload}
    except (OSError, ConnectionError) as error:
        from . import models

        payload = {
            "schemaVersion": 1,
            "reachable": False,
            "error": f"daemon not reachable: {error}",
            "armed": False,
            "scanning": False,
            "enrolled": paths.STORE_PATH.exists(),
            "models": models.status(),
            "identities": [],
            "lastScan": None,
        }
        from . import pamsetup

        payload["pam"] = pamsetup.status()

    def human(p: dict) -> None:
        if not p["reachable"]:
            print(p["error"], file=sys.stderr)
            print("start it with: systemctl --user start glanced")
        else:
            print(f"armed:      {p['armed']}")
            print(f"liveness:   {p['mode']}")
            print(f"camera:     {p['camera']}")
        print(f"enrolled:   {p['enrolled']}")
        print(f"models:     landmarker={p['models']['landmarker']} arcface={p['models']['arcface']}")
        pam = p.get("pam")
        if pam:
            if not pam["module"]:
                lock = "module not installed (glancectl setup-pam)"
            elif pam["shellFingerprint"]:
                lock = "hands-free (shell fingerprint stack)"
            elif pam["shellPassword"] or pam["hyprlock"]:
                lock = "on Enter (" + ", ".join(n for n, v in (("shell", pam["shellPassword"]), ("hyprlock", pam["hyprlock"])) if v) + ")"
            else:
                lock = "not wired (glancectl setup-pam)"
            print(f"lock screen: {lock}")
        for identity in p.get("identities", []):
            flag = "enabled" if identity["enabled"] else "disabled"
            print(f"  - {identity['name']} ({identity['captures']} captures, {flag})")
        last = p.get("lastScan")
        if last:
            print(f"last scan:  {OUTCOME_TEXT.get(last['outcome'], last['outcome'])}"
                  + (f" as {last['identity']}" if last.get("identity") else "")
                  + (f" — {last['reason']}" if last.get("reason") else ""))

    _emit(args, payload, human=human)
    return 0 if payload["reachable"] else 1


def _setup_pam(args: argparse.Namespace) -> int:
    from . import pamsetup

    repo_pam = Path(__file__).resolve().parents[2] / "pam"
    try:
        return pamsetup.setup(
            hands_free=args.hands_free,
            remove=args.remove,
            repo_pam_dir=repo_pam if repo_pam.exists() else None,
        )
    except subprocess.CalledProcessError as error:
        print(f"setup-pam: command failed: {' '.join(map(str, error.cmd))}", file=sys.stderr)
        return 1


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


def _daemon(args: argparse.Namespace) -> int:
    from .daemon import Daemon
    from .liveness import LivenessMode

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    daemon = Daemon(
        mode=LivenessMode(args.mode),
        device=args.device,
        scan_timeout=args.scan_timeout,
        no_face_timeout=args.no_face_timeout,
        relock_after=args.relock_after or None,
    )
    if daemon.arm_from_file():
        logging.info("armed from remembered passphrase")
    else:
        logging.info("starting disarmed; run `glancectl arm`")
    try:
        daemon.serve()
    except KeyboardInterrupt:
        pass
    return 0


# --- parser -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glancectl", description="face unlock for Linux, with liveness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def passphrase_flags(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--passphrase-stdin", action="store_true",
                         help="read the passphrase from the first line of stdin instead of prompting")

    fetch = subparsers.add_parser("fetch-model", help="download the landmarker and ArcFace models")
    fetch.add_argument("--variant", choices=["mbf", "r50"], default="mbf",
                       help="ArcFace backbone: mbf (13MB, default) or r50 (166MB, slower, a little more accurate)")
    fetch.add_argument("--force", action="store_true", help="re-download even if present")
    fetch.set_defaults(func=_fetch_model)

    enroll = subparsers.add_parser("enroll", help="capture your face into the encrypted store")
    enroll.add_argument("--name", required=True, help="identity name, e.g. your first name or 'glasses'")
    enroll.add_argument("--captures", type=int, default=5)
    enroll.add_argument("--device", default="/dev/video0")
    enroll.add_argument("--remember", action="store_true",
                        help="also store the passphrase (0600) so the daemon arms itself at login")
    passphrase_flags(enroll)
    enroll.set_defaults(func=_enroll)

    forget = subparsers.add_parser("forget", help="remove an identity from the store")
    forget.add_argument("name")
    passphrase_flags(forget)
    forget.set_defaults(func=_forget)

    arm = subparsers.add_parser("arm", help="decrypt the enrollment into the running daemon")
    arm.add_argument("--remember", action="store_true",
                     help="also store the passphrase (0600) so the daemon arms itself at login")
    arm.add_argument("--json", action="store_true")
    passphrase_flags(arm)
    arm.set_defaults(func=_arm)

    disarm = subparsers.add_parser("disarm", help="drop the decrypted enrollment from the daemon")
    disarm.add_argument("--forget", action="store_true", help="also delete a remembered passphrase")
    disarm.add_argument("--json", action="store_true")
    disarm.set_defaults(func=_disarm)

    authenticate = subparsers.add_parser("authenticate", help="run one unlock scan (what the PAM module does)")
    authenticate.add_argument("--json", action="store_true")
    authenticate.set_defaults(func=_authenticate)

    setup_pam = subparsers.add_parser("setup-pam", help="wire pam_glance into the lock screen (uses sudo)")
    setup_pam.add_argument("--hands-free", action="store_true",
                           help="scan automatically at lock via the shell's fingerprint stack (needs an enrolled fingerprint)")
    setup_pam.add_argument("--remove", action="store_true", help="strip pam_glance from every lock stack")
    setup_pam.set_defaults(func=_setup_pam)

    status = subparsers.add_parser("status", help="query the running daemon")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=_status)

    live = subparsers.add_parser(
        "live", help="run the liveness model against your webcam (no unlock, no recognition)"
    )
    live.add_argument("--mode", choices=["light", "heavy"], default="heavy")
    live.add_argument("--device", default="/dev/video0")
    live.add_argument("--scan-seconds", type=float, default=10.0)
    live.add_argument("--preview", action="store_true", help="also show the camera window")
    live.set_defaults(func=_live)

    selftest = subparsers.add_parser("selftest", help="run the liveness decision model against synthetic data")
    selftest.add_argument("--mode", choices=["light", "heavy"], default="heavy")
    selftest.add_argument("--noise", type=float, default=0.5, help="landmark jitter in pixels (default: 0.5)")
    selftest.set_defaults(func=_selftest)

    daemon = subparsers.add_parser("daemon", help="run the service")
    daemon.add_argument("--mode", choices=["light", "heavy"], default="light")
    daemon.add_argument("--device", default="/dev/video0")
    daemon.add_argument("--scan-timeout", type=float, default=8.0, help="seconds per unlock attempt")
    daemon.add_argument("--no-face-timeout", type=float, default=3.0,
                        help="give up this early when no face is in view, so a typed password is not kept waiting")
    daemon.add_argument("--relock-after", type=float, default=0.0,
                        help="disarm after this many idle seconds (0 = never)")
    daemon.set_defaults(func=_daemon)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit as exit_:
        if isinstance(exit_.code, str):
            print(exit_.code, file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    raise SystemExit(main())
