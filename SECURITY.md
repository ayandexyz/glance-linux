# Security

## What this is, and is not

glance-linux is a convenience feature. It makes a face unlock resist a
printed photo and a photo on a phone screen; it does **not** reliably resist a
video of you, and it is not a substitute for a password or a hardware token.
Read [Threat model](#threat-model) before wiring it into a lock screen that
guards anything you care about.

## Reporting a vulnerability

Open a [GitHub security advisory](https://github.com/ayandexyz/glance-linux/security/advisories/new)
or email `deayan252@gmail.com`. Please do not open a public issue for a
vulnerability. You will get a reply within a week.

## Threat model

### What the design defends against

- **A photo or a phone held up to the camera.** The five-cue liveness model
  (glare, device edge, flatness, depth/pose, blink) is the reason this project
  exists; see the README for what each cue fires on.
- **Unlimited guessing.** The daemon refuses to scan for five minutes after
  five consecutive failed scans *with a face in view*, so a hands-free lock
  screen that rescans in a loop does not hand an attacker unlimited free
  attempts. An empty room, a broken camera or a disarmed daemon does not
  count. `glancectl daemon --max-failures / --lockout` tune it.
- **A stored password.** There is none. Unlock goes through PAM; nothing types
  a password anywhere.
- **Enrollment at rest.** Embeddings are AES-256-GCM encrypted under a
  passphrase. With `--remember` that passphrase sits 0600 in your home
  directory, which is a deliberate convenience trade-off: it protects the
  store from an offline copy of the disk, not from your own account.
- **The PAM module.** `pam_glance.so` refuses to run setuid, refuses to act
  for any user but the calling one, only talks to a socket owned by that user,
  and treats every failure — timeout, malformed reply, missing daemon — as
  "not authenticated", which `auth sufficient` turns into a fall-through to
  the password.
- **The bar widget.** It runs `glancectl` only at a checked absolute path,
  with a pinned environment, bounded output and a process-tree deadline; see
  `plugin/README.md`.

### What it does not defend against: code running as you

The daemon runs **as your user**, in your session, and the PAM module trusts
the socket it finds in your runtime directory. This is the same trust
boundary as Howdy's in-process module and Omarchy's own shell lock, and it
means:

> Any process already running as your user — a compromised editor extension,
> a malicious plugin, a script you ran — can stop `glanced`, bind its socket
> with something that always answers "yes", or re-enroll a different face.
> The lock screen would then open for anyone.

Such a process could also have kept the screen from locking in the first
place, read your files, and installed anything it liked; the lock screen is
not what stands between it and your data. But it *is* what stands between a
passer-by and your unattended session, and after that kind of compromise the
face unlock no longer does. If that matters to you, do not use `--remember`
and do not use `--hands-free`.

Moving the daemon and the enrollment store to a root-owned system service
(the `fprintd` shape) is the fix, and is on the roadmap. It is a redesign,
not a patch, so it is not in this release.

### Residual risks you should know about

- **Video replay.** A video of you on a screen can pass the confirm cues.
  The deny cues (glare, device edge) catch a lot of them but not all.
- **Twins and look-alikes.** ArcFace at the default similarity threshold is
  not a biometric-grade matcher.
- **The camera itself.** A webcam that another process can open, or a virtual
  camera device, can feed the daemon anything. `glanced` opens whatever
  `--device` names; it does not verify that it is real hardware.
