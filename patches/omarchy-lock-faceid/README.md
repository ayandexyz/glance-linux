# Face ID-style indicator for the Omarchy lock screen

A capsule that drops in at the top centre of the lock screen while a face PAM
module is scanning, closes into a check mark on success, and shakes on a miss.
Modelled on Glance's notch pill on macOS and iOS's Face ID motion.

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
