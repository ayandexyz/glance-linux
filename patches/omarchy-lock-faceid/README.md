# Face ID-style indicator for the Omarchy lock screen

A capsule that drops in at the top centre of the lock screen while a face PAM
module is scanning, showing **a live view of the camera** inside the sweep
ring. It closes into a check mark on success and shakes on a miss. Modelled on
Glance's notch pill on macOS and iOS's Face ID motion.

This is a patch to Omarchy's own lock plugin (`shell/plugins/lock/`), because
nothing outside the session-lock surface can draw over it. It is the shape of
an upstream change, previewed locally. An Omarchy update overwrites it.

## How it knows a scan is happening

`pam_glance` sends a PAM info message (`Glance: look at the camera`) before it
opens the camera. The lock's password `PamContext` already receives every
message; the patch watches for that prefix:

| PAM event | indicator |
|---|---|
| info message starting `Glance:` | `scanning` — slide in, sweep, glyph breathes |
| conversation completes with success while scanning | `success` — ring closes, glyph pops to ✓, unlock 800 ms later |
| PAM moves on to the password prompt | `failure` — red ring, shake, hides after 1.8 s; password checked as usual |

No polling, no socket, and any module that announces itself the same way
(howdy could) gets the indicator for free.

## The live view

The lock screen cannot capture it: the daemon holds the camera for the whole
scan, and V4L2 will not give a second process a stream. So the daemon
publishes each frame instead, and the indicator polls it at 15fps.

The published frame is a 192px square crop that follows the face box,
mirrored (an unmirrored preview of your own face moves the wrong way when you
lean), JPEG quality 70, about 5KB. It is written to
`$XDG_RUNTIME_DIR/glance/preview.jpg`, which means:

- on tmpfs, so no frame ever reaches the disk;
- mode 0600 inside a 0700 directory, so no other user can read it;
- replaced atomically, so the indicator never renders half a frame;
- deleted when the scan ends, and again when the daemon starts, so a crash
  cannot leave a picture of your face behind.

Two images alternate in the indicator, and the newly loaded one is only shown
once it has decoded, so the view never blinks between frames. Until the first
frame lands — and if the daemon is not running, or previews are off — the face
glyph stands in, so the indicator never depends on the preview existing.

Turn it off entirely with `glancectl daemon --no-preview` (edit the
`ExecStart=` line in `~/.config/systemd/user/glanced.service`). The indicator
then simply keeps the glyph.

## The unlock afterglow

The lock surface is destroyed the instant the session unlocks, so a check mark
drawn on it disappears at exactly the moment it is doing its job, and the
unlock ends on a blink. The patch carries the capsule across that boundary
with a second, click-through layer-shell window that outlives the lock:

| | |
|---|---|
| PAM returns success | ring closes, check mark pops on the lock surface; the afterglow window is raised *behind* it |
| after 550 ms | the lock drops, revealing the afterglow already painted and in place |
| held 900 ms | the same capsule, in the same pixel position, now on the desktop |
| 420 ms | slides up and fades, window destroyed |

The window is raised at the verdict rather than at the unlock because a
layer-shell window is not on screen when it is asked for: it has to be
created, configured and painted, which measured 60-230 ms on this machine,
while the lock surface is destroyed in the same turn as the request. Raising
it at the unlock leaves a stretch with neither on screen, and the capsule
visibly vanishes and pops back. A session lock surface renders above every
layer-shell layer, so a window raised early is simply invisible until the lock
drops and reveals it.

The session comes back sooner than it did before this (550 ms rather than
800 ms), because the confirmation no longer has to finish before the unlock —
the rest of it happens on the far side, where it costs the user nothing.

The afterglow copy takes no keyboard focus and has an empty input region, so
every click passes straight through it. It is drawn with the notification
colours rather than the lock ones: `lock.*` is translucent by design, tuned to
sit on a blurred wallpaper, and over the desktop that leaves terminal text
legible through the words.

On a multi-monitor setup the afterglow appears on one screen while the lock
covers all of them, the same way the existing lock preview window behaves.

## Apply / revert

```bash
sudo patches/omarchy-lock-faceid/apply.sh    # backs up *.orig
omarchy-restart-shell
sudo patches/omarchy-lock-faceid/revert.sh
omarchy-restart-shell
```

## Preview without locking

```bash
omarchy-shell lock previewFace scanning
omarchy-shell lock previewFace success
omarchy-shell lock previewFace failure
omarchy-shell lock hidePreview
```

## Files

- `FaceUnlockIndicator.qml` — the capsule; self-contained, themed from `Color.lock`
- `LockView.qml`, `Service.qml` — the installed 4.0.3 files plus the wiring
- `lockview.diff`, `service.diff` — the wiring alone, for the upstream PR
