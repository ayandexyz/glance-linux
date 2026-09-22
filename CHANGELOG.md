# Changelog

## 0.3.4 — 2026-09-23

### Fixed
- The post-update hook ships in the package too. `setup-lock` from a pip
  install applied the indicator and then had nothing to repair it, so the next
  `omarchy update` quietly took it away again — worse than never applying it,
  because the lock screen looks right until the day it does not.

## 0.3.3 — 2026-09-23

### Added
- **`glancectl setup`.** The service, the PAM wiring and the lock indicator in
  one command, behind a single password prompt. Enrolling stays separate: it
  needs a face in front of the camera and a passphrase you choose.
- The PAM module's source and the lock-screen patch ship inside the package,
  so `setup-pam` and `setup-lock` work from a `pip install` instead of only
  from a checkout or the AUR package. They were the two steps a PyPI install
  could not complete, and both failed by opening a terminal that closed again
  before anyone could read why. A test holds the packaged copies byte-identical
  to the canonical ones at the repository root.

### Fixed
- `setup-pam` says what to install when there is no compiler, rather than
  failing with a make traceback.

## 0.3.2 — 2026-09-22

### Added
- `glancectl status` reports `gui`: whether this install can open the
  enrollment window. The Omarchy plugin reads it to choose between the window
  and a terminal, so an install without the `gui` extra gets an **Enroll**
  button that works instead of one that raises ImportError. Checked with
  `find_spec`, so nothing imports Qt to answer it.

## 0.3.1 — 2026-09-22

### Added
- **`glancectl install-service`.** Writes and enables the `glanced` user
  unit without a source checkout, so a `pipx install glanced` is a working
  service and not just a binary. It generates the same unit
  `packaging/install.sh` installs — a test holds the two together — pointed
  at the absolute path of the `glancectl` running it, resolved through
  symlinks so a pipx upgrade cannot leave `ExecStart` dangling.
  `--remove` takes it back out, `--no-enable` writes the unit only.
  It also fetches the models first if they are missing, so installing
  the daemon is `pipx install` and this, rather than a third step
  whose absence only shows up later in the journal. `--no-fetch`
  skips that.

## 0.3.0 — 2026-09-22

### Added
- **Attention mode.** A third socket, `attention.sock`, streams head pose
  (`present`, `yaw`, `pitch`, `conf` — derived values, never frames or
  landmarks) to any connected client at `--attention-fps` (default 8). It
  uses the landmarker only — no ArcFace, no arming, no enrollment — so it
  works for anyone with a webcam. The camera is held only while someone is
  subscribed and is handed over to an unlock scan on request, so an auth
  request always has priority. `glancectl attention` prints the stream;
  `glancectl daemon --no-attention` turns the socket off. This is what
  `omarchy-attention` (a blur shield without screen capture) subscribes to.
- `Camera.frames(min_interval)` drops frames without decoding them, which is
  how attention runs the landmarker at 8 fps on a 30 fps camera.

## 0.2.0 — 2026-09-18

First release under `ayandexyz`. Everything below is relative to 0.1.1.

### Added
- **Lockout.** Five consecutive failed scans with a face in view put the
  daemon into a five-minute refusal (`Outcome.LOCKED_OUT`), so a hands-free
  lock screen cannot be used for unlimited attempts. Empty-room, camera-error
  and not-armed scans do not count. `glancectl daemon --max-failures`,
  `--lockout`; `failures` and `lockedOut` on the status socket.
- **`glancectl setup-lock`.** Applies the Face ID-style lock screen indicator
  and installs an Omarchy `post-update` hook that re-applies it after
  `omarchy update`, from the package's root-owned copy only. `--remove`
  restores the stock files and drops the hook.
- The status socket and `glancectl status` report whether the indicator is
  applied and whether the hook is installed; the bar panel shows it and offers
  `setup-lock` as the last setup step.
- `SECURITY.md` with the threat model, including the user-session trust
  boundary the daemon has today.
- CI: pytest, the PAM module build, the plugin's JS tests and a manifest
  check on every push.

### Changed
- Plugin id is `io.github.ayandexyz.glance`; repositories live under
  `github.com/ayandexyz`.
- `apply.sh` refreshes its `.orig` backup whenever the file it is replacing is
  not already patched, so a revert after an Omarchy update restores that
  update's stock file rather than an older one.
- The AUR package ships `patches/omarchy-lock-faceid/` and the hook under
  `/usr/share/glanced/`.

### Removed
- `public/fonts/` and `.vscode/` — artifacts of a malware injection into the
  0.1.x history (see commit history of `b335940`), which never belonged to
  this project. The history pushed to `ayandexyz` has them purged.

## 0.1.1

Liveness model, daemon, `glancectl`, `pam_glance`, guided enrollment, the
Omarchy bar widget and the lock screen indicator patch.
