# pam_glance

The PAM half of the unlock path. It connects to your running `glanced` over
the auth socket in `$XDG_RUNTIME_DIR/glance/` and turns the daemon's verdict
into a PAM result. All of the vision, liveness, and recognition is in the
daemon; this module is about 150 lines and does none of it.

## Build

```bash
sudo pacman -S --needed pam base-devel
make -C pam
sudo make -C pam install        # -> /usr/lib/security/pam_glance.so
```

## Wire it into the lock screen — and only the lock screen

**Keep a root shell open in another terminal while you do this.** A broken
PAM stack can lock you out.

The module refuses to run for any account other than the one the calling
process runs as, and refuses entirely in a setuid context. So it cannot be
made to authorize `sudo`, `su`, or a login manager; it exists for the lock
screen, where the process is already you and the only question is whether to
let you back in.

### hyprlock

Edit `/etc/pam.d/hyprlock` so face unlock is tried first and the password is
the fallback:

```
auth        sufficient  pam_glance.so
auth        include     login
```

Lock, press **Enter** on the empty password field, and look at the camera.
`sufficient` means a face success ends the stack; a failure just drops through
to the password prompt as before.

### Omarchy shell lock

The shell lock uses `/etc/pam.d/omarchy-lock-password`. Add the same line at
the top of that file.

## Options

| arg | meaning |
|---|---|
| `debug` | log why the module declined, to the auth log |
| `timeout=N` | seconds to wait for the daemon (default 20) |

## Check it without locking

```bash
glancectl authenticate          # same request the module sends
journalctl --user -u glanced -f # what the daemon saw
```
