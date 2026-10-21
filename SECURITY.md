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
- **The PAM module.** `pam_glance.so` refuses to act for any user but the
  calling one, only talks to a socket owned by that user, and treats every
  failure — timeout, malformed reply, missing daemon — as "not authenticated",
  which `auth sufficient` turns into a fall-through to the password. It
  refuses setuid contexts, unless a stack asks for that path by name with
  `allow_setuid`; see [The sudo path](#the-sudo-path) below.
- **The bar widget.** It runs `glancectl` only at a checked absolute path,
  with a pinned environment, bounded output and a process-tree deadline; see
  `plugin/README.md`.
- **Attention subscribers.** `attention.sock` is publish-only: the daemon
  never reads from it, so a client on it cannot reach any verb. What it
  publishes is two angles, a presence bool and a confidence — never a frame,
  never landmarks, never an embedding. It is group-readable like the status
  socket, and `--no-attention` removes it.

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

### The sudo path

`glancectl setup-pam --sudo` puts the module in `/etc/pam.d/sudo`, where a
face can authorize a privileged command instead of a password. **This is
opt-in and it is a different trade from the lock screen**, because the lock
screen only asks whether to let you back into a session you already own,
while `sudo` hands over root.

The daemon runs as you, and it says yes to a face it recognises. So:

> Any program already running as your user — the same compromised editor
> extension, malicious plugin or script — can run `sudo` and wait for you to
> walk past the camera. When you do, it gets root.

That is the bargain Howdy makes, and it is the whole of what this buys. The
module keeps the blast radius as small as it can:

- the setuid path is **off unless a stack asks for it by name**
  (`auth sufficient pam_glance.so allow_setuid`), and the lock stacks never
  do;
- the PAM service has to be `sudo` — another service cannot borrow the path;
- the program talking to the module has to be `sudo` itself, read from
  `/proc/self/comm`. A PAM file that named `sudo` but was loaded by anything
  else is refused;
- there has to be a controlling terminal, so a person is at a keyboard and
  not a cron job or a systemd service;
- `-n` / `--non-interactive` is refused, because that flag means "never
  prompt me".

`sudoedit`, `su` and login managers are not covered. A failed scan — nobody in
view, a face that does not match, a disarmed daemon, a lockout after five
failed attempts — falls through to the password exactly as before.

If that trade is not one you want, do not turn it on. It is a convenience
feature, not a security upgrade: the webcam is 2D, the liveness cues do not
reliably defeat a video of you, and the password is still the strongest thing
you have.

### Residual risks you should know about

- **Video replay.** A video of you on a screen can pass the confirm cues.
  The deny cues (glare, device edge) catch a lot of them but not all.
- **Twins and look-alikes.** ArcFace at the default similarity threshold is
  not a biometric-grade matcher.
- **The camera itself.** A webcam that another process can open, or a virtual
  camera device, can feed the daemon anything. `glanced` opens whatever
  `--device` names; it does not verify that it is real hardware.
