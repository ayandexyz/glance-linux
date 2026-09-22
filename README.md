# glance-linux

Face unlock for Linux, with the liveness detection that face-unlock on Linux
usually doesn't have.

![The lock screen's face unlock indicator and the Glance bar panel](plugin/preview.png)

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

Two guard rails come with that. Five failed scans in a row with a face in
view lock face unlock out for five minutes, so a looping hands-free lock
screen is not a free brute-force surface (`--max-failures`, `--lockout`). And
`SECURITY.md` spells out the trust boundary: the daemon runs as your user, so
code already running as you could subvert it — the same boundary Howdy and
the shell's own lock have, but one you should read before relying on it.

## Status

| Piece | State |
|---|---|
| Liveness model (`src/glanced/liveness/`) | **Complete, ported, tested** |
| Geometry / homography | Complete |
| Enrollment store (AES-256-GCM) | Complete |
| Alignment + ArcFace embedding | Complete |
| Camera capture, landmarking, scan loop | Complete |
| Daemon: sockets, arming, status | Complete, tested |
| `glancectl` (enroll, arm, authenticate, status, live, selftest) | Complete |
| Guided enrollment (`glanced/poses.py`, `--gui` tick ring) | Complete — five directions, after the macOS onboarding sweep |
| `pam_glance` + `glancectl setup-pam` | Complete — see `pam/README.md` |
| Attention mode (`attention.sock`, `glancectl attention`) | Complete, tested — head pose for the desktop, see below |
| Omarchy plugin (`plugin/`) | Bar widget + panel — see `plugin/README.md` |
| Lock screen indicator (`patches/omarchy-lock-faceid/`) | Face ID-style capsule with a live camera view — a patch to Omarchy's lock plugin, applied by `glancectl setup-lock` and re-applied after `omarchy update` by a post-update hook |

## Install

From a checkout of the latest release:

```bash
git clone --branch v0.3.0 https://github.com/ayandexyz/glance-linux.git
cd glance-linux
python -m venv .venv && .venv/bin/pip install -e '.[runtime]'
.venv/bin/glancectl fetch-model      # ~3MB landmarker + ~13MB ArcFace
.venv/bin/glancectl install-service  # writes the user unit, enables it
omarchy plugin add https://github.com/ayandexyz/omarchy-glance.git --enable
```

Once the package is on PyPI the first three lines collapse into
`pipx install 'glanced[runtime]'`, which is what `install-service` was
written for — it does not need the repository.

`install-service` writes the same unit `packaging/install.sh` does, pointed at
the `glancectl` that is running it, so it works from pipx, a venv or a
checkout. `glancectl install-service --remove` takes it back out.

An AUR package is written and waiting in `packaging/aur/` — the PKGBUILD and
the release runbook — for when there is an account to publish it from. It
carries the daemon, the PAM module and both models, so it will be one command
and no download.

Then click the bar icon and take the one button it offers, three times: **Start
daemon**, **Enroll** (the guided sweep opens in a window), **Wire lock screen**
(a terminal, for the one step that needs your password). A fourth, **Add lock
indicator**, is optional: the Face ID-style capsule on the lock screen, kept
in place across `omarchy update` by a hook. `packaging/aur/` holds
the PKGBUILD and the release runbook.

The package deliberately does not touch `/etc/pam.d` itself. Changing how the
machine authenticates you belongs to a command you run and watch, not to an
unattended pacman transaction editing files that belong to hyprland and
omarchy.

## Setup from source

For hacking on it, or to run the tests:

```bash
python -m venv .venv && .venv/bin/pip install -e '.[runtime,gui,dev]'
.venv/bin/python -m pytest                   # no camera needed
.venv/bin/glancectl fetch-model              # ~3MB landmarker + ~13MB ArcFace
.venv/bin/glancectl live --mode heavy        # liveness against your webcam, no unlock
```

Then the real thing:

```bash
packaging/install.sh                             # user service + plugin symlink
glancectl enroll --gui --name "$USER" --remember # guided sweep, sets the passphrase
glancectl authenticate                       # one full scan: recognition + liveness
glancectl setup-pam                          # wire the lock screen (sudo; keep a root shell open)
glancectl setup-lock                         # optional: the lock screen indicator + its post-update hook
omarchy plugin enable io.github.ayandexyz.glance          # the bar widget
```

Lock the screen, press Enter (shell lock: any character then Enter), look at
the camera. `pam/README.md` explains the two lock screens Omarchy has had, the
on-demand vs `--hands-free` choice, and how to undo it.

`enroll` walks you through five head directions — centre, then left, up,
right and down — and takes two samples at each, gated on the yaw and pitch the
landmarker reports rather than on you being asked nicely to move. That is what
makes ten rows cover five poses instead of ten near-copies of a frontal
capture. The macOS app sweeps nine, adding the diagonals; those ask for a
compound turn that is harder to explain and to hold, and a template already
covering both profiles and both chin extremes has the corners bracketed. `--gui` shows the sweep as a Face ID-style tick ring around a
mirrored camera disc, each direction lighting its sector as it lands; without
it the same sweep runs against a one-line terminal readout. `--no-guide` falls
back to the old five prompted captures, for a camera whose landmarker reports
no head pose at all. No frame is written in any of the three: the preview is
pixels on the way to the screen and nothing else.

`enroll` asks for a passphrase the first time; it encrypts the embeddings at
rest. The daemon starts *disarmed* and cannot scan until it has that
passphrase: either `glancectl arm` after each login, or `--remember`, which
stores it 0600 under `~/.local/share/glance/` so the daemon arms itself. That
is a convenience/at-rest trade-off you make explicitly.

`glancectl authenticate` sends exactly the request `pam_glance` sends, so the
whole unlock path can be exercised without touching PAM.

### What a plugin can and cannot do

The Omarchy plugin is QML and runs inside the shell; it can draw status and
run commands as you. It cannot install a PAM module, edit `/etc/pam.d`, or
ship a Python daemon. So "install the plugin and face unlock works" is not a
thing any marketplace plugin can deliver on its own. The intended shape is:

1. a package (AUR `glanced`) that installs `glancectl`, the daemon service,
   and `pam_glance.so`;
2. one `glancectl enroll` and one `glancectl setup-pam` (the sudo step);
3. the plugin, which shows the state, tells you which of those is missing,
   and offers arm, disarm and test-scan.

`glancectl selftest` is the counterpart of Glance's hidden Face Lab: it drives
the real decision logic against synthetic faces and prints every cue's reading
and fire count, with no camera involved. `glancectl live` does the same against
real frames.

## Attention mode

The daemon can also tell the desktop where you are looking. Connect to
`attention.sock` and it streams one JSON line per frame:

```json
{"schemaVersion": 1, "t": 1234.5, "state": "tracking", "present": true, "yaw": -12.4, "pitch": 3.1, "conf": 1.0}
```

`yaw` and `pitch` are degrees (yaw positive when your head turns to your
left, pitch positive when your chin comes down); `conf` is how far the face
is above the size the landmarker is trusted at. `state` is `starting`,
`tracking`, `paused` (an unlock scan has the camera) or `error` (it could not
be opened; the daemon retries). `glancectl attention` prints the stream.

This is what a blur shield — [omarchy-shy](https://github.com/ayandexyz/omarchy-shy),
the ShyGlass idea done without screen capture — or an idle inhibitor
subscribes to. Things that matter about it:

- **It is not auth mode.** Attention needs the landmarker and the pose it
  already computes, nothing else: no ArcFace, no template, no arming, no
  passphrase. It works on a daemon that has never been armed for a user who
  has never enrolled or touched `/etc/pam.d`.
- **It is a third socket, publish-only.** Nothing a client sends is read, so
  nothing on it can start or influence a scan. It emits *derived* values —
  angles and a bool — never frames, never landmarks. `src/glanced/attention.py`
  is short enough to check that claim in a minute.
- **The camera has one owner.** The tracker holds it only while a subscriber
  is connected — nothing listening, camera closed, LED off — and hands it
  over the moment a scan asks, taking it back when the scan ends. An auth
  request never waits on it for more than three seconds.
- **It is cheap.** Eight landmarker passes a second at 640×360, about 8% of
  one core on a laptop with the camera included. `--attention-fps` tunes it;
  `--no-attention` removes the socket entirely.
- **Clients fail open.** Only `tracking` says anything about where you are
  looking. A client covering the screen treats every other state, and
  silence, as "come down".

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
src/glanced/   glanced        camera -> landmarks -> {ArcFace embed, liveness} -> verdict
                              camera -> landmarks -> head pose            (attention.sock)
pam/           pam_glance.so  talks to the daemon over a 0600 unix socket
plugin/        Omarchy QML    bar widget + panel: status, arm/disarm, test scan
```

Unlock lives in PAM, in `hyprlock`'s stack, and works whether or not the shell
is running. The plugin is a thin client over a **separate, lower-privilege
status socket** and is presentation only — it can never cause or influence an
unlock. Separate sockets rather than one with a role field, so a compromised
shell plugin cannot reach the auth verb at all. The attention socket is the
same idea one step further: it has no verbs at all.

> When wiring `pam_glance`, keep a root TTY open. A broken PAM stack locks you
> out of your own machine.

## Credit

- [Glance](https://github.com/jonnyoo/glance) — the liveness model and its
  tuning, MIT © Jonathan Zhou. See `NOTICE`.
- [InsightFace](https://github.com/deepinsight/insightface) — the ArcFace model.

## License

MIT
