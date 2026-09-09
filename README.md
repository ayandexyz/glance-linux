# glance-linux

Face unlock for Linux, with the liveness detection that face-unlock on Linux
usually doesn't have.

A reimplementation of the liveness model from
[Glance](https://github.com/jonnyoo/glance) (macOS, MIT) around a PAM-based
unlock path. Not a port of the app — none of the Swift is portable — but the
part of Glance with real substance is: the five-cue liveness model that tells a
face from a photograph.

## Why

[Howdy](https://github.com/boltgolt/howdy) already does face unlock on Arch. Its
well-known weakness is that it has essentially no liveness detection, so a
printed photo can unlock it. That is exactly the gap this fills.

The other half is that **Linux is a better platform for this than macOS**.
Glance's own README carries the caveat that *"macOS has no API that lets a
third-party app authorize a login, so Glance unlocks by typing your stored
password on the lock screen."* Linux has PAM. So this project:

- stores **no password** anywhere,
- injects **no keystrokes**,
- needs **no accessibility/input-injection permission**,
- and authorizes the session directly, through the same interface `sudo` and
  `hyprlock` already use.

That removes the single most sensitive secret in the macOS design.

### It is still not FaceID

A webcam sees a flat 2D image; an iPhone builds a 3D depth map. The liveness
cues here defeat a printed photo and a photo on a phone screen with reasonable
confidence. They do **not** reliably defeat a video of you. This is a
convenience feature, not a security upgrade.

## Status

| Piece | State |
|---|---|
| Liveness model (`src/glanced/liveness/`) | **Complete, ported, 36 tests passing** |
| Geometry / homography | Complete |
| Enrollment store (AES-256-GCM) | Complete |
| Alignment + ArcFace embedding | Complete, needs the ONNX model file |
| IPC, session locking, service | Complete |
| Camera capture loop | Scaffold — one seam left open in `daemon._detect` |
| `pam_glance` | Not started |
| Omarchy Quattro plugin | Not started |

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest          # 36 passed
PYTHONPATH=src .venv/bin/python -m glanced.cli selftest --mode heavy
```

`glancectl selftest` is the counterpart of Glance's hidden Face Lab: it drives
the real decision logic against synthetic faces and prints every cue's reading
and fire count, with no camera involved.

## The liveness model

Five cues, two roles, and deliberately **no overall liveness percentage**.

Upstream arrived at this after real-device testing killed an earlier design that
averaged ~11 signals into a weighted score: most were noise-limited at webcam
resolution, several actively *rewarded* the smooth motion of a hand holding up a
phone, and the resulting number wandered 30–80% on a live face while a phone
photo scored about the same. Only five cues separated a real face from a phone,
and each is individually decisive — which makes averaging exactly the wrong
combination rule.

**Deny cues** — evidence of a spoof. Either one firing fails the scan outright
and overrides any confirmation that already happened. A spoof tell does not get
outvoted.

| Cue | Fires when |
|---|---|
| Gloss/glare | One big flat specular blob (glass) rather than skin's small scattered shine |
| Device detected | A device-shaped rectangle overlaps the face |

**Confirm cues** — evidence of a real face. Any one is enough, and their
*absence is never a failure*: a live person can sit still and not blink for a
whole scan.

| Cue | Fires when |
|---|---|
| Flat vs 3D | Held-out nose points miss the best-fit homography — the face has depth |
| Depth/pose | Nose offset tracks head yaw at a magnitude only a real nose produces |
| Blink | Eye aspect ratio dipped and recovered |

`Light` runs the deny cues only — "confirmed unless proven wrong", which never
blocks a user who happens to sit still. `Heavy` also requires a confirm cue, and
can genuinely fail to unlock a motionless, unblinking live user. That cost is
pinned in a test so it is never mistaken for a regression.

## A bug found while porting

The depth/pose cue upstream gates on the Pearson correlation between nose offset
and `tan(yaw)`, documented as *"a genuinely positive relationship of the kind
only a nose sitting off the eye plane produces."*

That is not true of a plane viewed in perspective. Perspective projection does
not preserve midpoints, so a tilted photo's apparent eye midpoint does shift
relative to its nose. The shift is tiny — but correlation is scale-free and
cannot tell a tiny systematic drift from a large one. Measured against
`tests/synthetic.py`, a flat photo scores **1.00** at zero landmark noise and
**0.95** at 0.25px, both well over the 0.8 fire threshold. It only drops below
the threshold around 1px of jitter: the cue was relying on landmark noise to
hide the artifact.

The fix is a magnitude gate on the regression slope, which is the physical
quantity the cue is actually reasoning about — the nose's depth as a fraction of
the interocular distance. It is ~0.24 for a real nose and ~0.026 for a plane,
stable across 0–1px of noise, so the gate sits at 0.08. See
`MIN_NOSE_DEPTH_RATIO` in `liveness/scoring.py` and the regression tests.

This affects the macOS app too. Its Heavy mode can confirm a printed photo that
shows no glare and no device edge, whenever landmark jitter is low.

## Deviations from upstream

Each is documented at its own site; the significant ones:

- **Device bezel detection** is rebuilt on OpenCV contours. Upstream uses
  `VNDetectRectanglesRequest`, which has no Linux equivalent. Parameters carry
  over unchanged, but `minimum_edge_support` is an invented analogue of Vision's
  confidence and is the knob most likely to need retuning against real footage.
- **Landmark regions** come from MediaPipe FaceMesh index groups rather than
  Vision's named regions. Cross-frame correspondence is *guaranteed* here, which
  is strictly better than what upstream must defend against.
- **`MEDIAN_LINE` is narrowed** to the two midline points between the brow and
  lip lines. The probe set requires points geometrically inside the fit hull, so
  that leftover error reads as depth rather than extrapolation; Vision's
  forehead-to-chin median line violates that.
- **Eye aspect ratio** stays the bounding-box ratio even though MediaPipe would
  support the classic 6-point formula, because the 0.65 dip and 0.7 recovery
  thresholds were tuned against this definition.
- **No password, no keystroke injection** — see above.

## Architecture

```
daemon/  glanced        camera -> landmarks -> {ArcFace embed, liveness} -> verdict
pam/     pam_glance.so  talks to the daemon over a 0600 unix socket   [not started]
plugin/  Omarchy Quattro QML: the pill overlay, status, enrollment    [not started]
```

Unlock lives in PAM, in `hyprlock`'s stack, and works whether or not the shell
is running. The plugin is a thin client over a **separate, lower-privilege
status socket** and is presentation only — it can never cause or influence an
unlock. Two sockets rather than one with a role field, so a compromised shell
plugin cannot reach the auth verb at all.

> When wiring `pam_glance`, keep a root TTY open. A broken PAM stack locks you
> out of your own machine.

## Credit

- [Glance](https://github.com/jonnyoo/glance) — the liveness model and its
  tuning, MIT © Jonathan Zhou. See `NOTICE`.
- [InsightFace](https://github.com/deepinsight/insightface) — the ArcFace model.

## License

MIT
