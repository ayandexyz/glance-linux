# AUR packaging

`glanced` for the AUR: the daemon, `glancectl`, `pam_glance.so`, and both
neural networks, so a user goes from `yay -S glanced` to a working face unlock
without downloading a model or compiling anything.

## What the package does and does not do

Installs `/usr/bin/glancectl`, the Python package, `/usr/lib/security/pam_glance.so`,
both models under `/usr/share/glanced/models/`, the lock screen indicator and
its post-update hook under `/usr/share/glanced/{lock-faceid,hooks}/`, and a
systemd **user** unit.

It does **not** edit `/etc/pam.d`, enable the service, or enroll anyone. Wiring
a lock screen changes how the machine authenticates you, and that belongs to a
command the user runs and watches — `glancectl setup-pam` — not to an
unattended pacman transaction editing files owned by `hyprland` and `omarchy`.
Removing the package cannot lock anyone out either: the line it writes is
`auth sufficient`, so PAM treats a module it can no longer load as a failure
that `sufficient` ignores, and falls through to the password.

## Why the dependencies look like that

- `python-onnxruntime` is a **virtual** name. `python-onnxruntime-cpu` provides
  it, and so do the CUDA and ROCm builds — so a user already running the GPU
  flavour is left alone instead of pulling a second copy of a 400MB package.
- `pyside6` is a hard dependency, not optional: the guided enrollment sweep
  (`glancectl enroll --gui`) is how the Omarchy widget enrolls anyone, so a
  package without Qt would ship a broken first-run.
- `python-mediapipe` is **not** in the official repos. It comes from the
  `omarchy` pacman repository, which every Omarchy install already has enabled
  — so `yay -S glanced` resolves it there. A plain Arch machine needs that
  repo added first, and so does a clean build chroot (add it to the chroot's
  `pacman.conf`, or `extra-x86_64-build` cannot satisfy the dependency). There
  is no AUR dependency chain.
- `buffalo_s.zip` is 127MB for the 13MB model inside it; upstream publishes no
  smaller artifact. makepkg caches it, so that cost is paid once per machine.

## Cutting a release

0. Push the plugin to its own repository. `omarchy plugin add` clones a whole
   repo and expects `manifest.json` at its root, so the widget is published
   from `plugin/` as a subtree — the marketplace also wants one plugin per
   repo, at the root:

       git subtree split --prefix=plugin -b omarchy-glance
       git push git@github.com:ayandexyz/omarchy-glance.git omarchy-glance:main

   Every install line in the docs points at that repo, so this happens before
   anything else is announced. `plugin/preview.png` rides along and lands at
   the root, which is where the marketplace looks for the one preview image
   it accepts (`preview.png`, any size; it makes its own card and detail
   sizes).

1. Tag the daemon repo and push the tag:

       git tag -a v0.2.0 -m "glanced 0.2.0"
       git push origin v0.2.0

2. Fill in the tarball checksum — it is `SKIP` in the committed PKGBUILD
   because the tag does not exist until step 1:

       curl -sL https://github.com/ayandexyz/glance-linux/archive/refs/tags/v0.2.0.tar.gz \
         | sha256sum

   Replace the first entry of `sha256sums` with that value.

3. Build it for real, in a clean chroot, so the dependency list is proven
   rather than assumed:

       extra-x86_64-build          # from devtools

   A plain `makepkg -si` builds against whatever you already have installed,
   which is exactly how a missing dependency goes unnoticed.

4. Lint, then publish:

       namcap PKGBUILD glanced-0.2.0-1-x86_64.pkg.tar.zst
       makepkg --printsrcinfo > .SRCINFO
       git clone ssh://aur@aur.archlinux.org/glanced.git aur && cd aur
       cp ../PKGBUILD ../glanced.install ../.SRCINFO .
       git add -A && git commit -m "glanced 0.2.0" && git push

`.SRCINFO` must be regenerated and committed on every version bump; the AUR
rejects a push whose `.SRCINFO` disagrees with its `PKGBUILD`.
