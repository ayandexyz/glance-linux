# Glance Face Unlock — Omarchy bar widget

![The Glance panel beside the lock screen's face unlock indicator](preview.png)

The shell half of [glance-linux](https://github.com/ayan-de/glance-linux): one
bar icon and one panel for the `glanced` face-unlock daemon.

- **Bar icon** shows the face-recognition glyph; it lights while a scan is in
  flight, including a scan started from the lock screen. Right-click runs a
  test scan.
- **Panel** shows whether the daemon is armed, who is enrolled, how the lock
  screen is wired (on Enter, hands-free, or not yet), and what the last scan
  decided and why. When something is still missing it offers exactly one
  button for the one step that unblocks you — start the daemon, enroll, wire
  the lock screen — with the command it will run printed underneath. Inline
  **Arm** takes the passphrase (over stdin, never argv) and can remember it so
  the daemon arms itself at login. **Disarm** and **Test scan** are one click.

Presentation only, by construction: everything the panel does is a `glancectl`
invocation run as you. Nothing here is on the unlock path, and if the shell is
not running PAM talks to the daemon exactly the same.

## What it runs, and how

A shell plugin runs unsandboxed as you, so the process boundary is where the
care goes. Three rules hold for every child process:

- **Absolute paths only.** `/usr/bin/glancectl`, `/usr/bin/systemctl`,
  `/usr/bin/setsid`, `/usr/bin/omarchy-launch-terminal`, `/usr/bin/stat`.
  Nothing is resolved through `PATH`, so a shadowed executable earlier on an
  inherited `PATH` cannot stand in for the real one.
- **The glancectl it runs is checked first.** Whether the default or the
  path you set, it goes through one `stat(1)` call over every component
  before its first use, and is refused unless it is absolute, contains no
  symlinks, every directory and the file are owned by root or by you and
  writable by nobody else, and the file is regular and executable. The panel
  prints the component that failed. `stat` also reports the plugin's own uid
  from `/proc/self/status`, so the check trusts the kernel, not `$HOME` or
  `$USER`.
- **Children get a pinned environment and a byte ceiling.** `PATH` is set to
  `/usr/bin:/usr/share/omarchy/bin` and `PYTHONPATH`, `PYTHONHOME`,
  `PYTHONSTARTUP`, `LD_PRELOAD`, `LD_LIBRARY_PATH` and `LD_AUDIT` are
  unset, because `glancectl` is a Python entry point and `setup-pam` runs
  `sudo`. stdout is capped at 64 KiB and stderr at 16 KiB per process; a
  child that writes more is killed at once and what it wrote is discarded
  rather than parsed or rendered.

All of this is exercised in `tests/run` against a fake glancectl: a flooding
one, one under a world-writable directory, a relative path, and a planted
`PYTHONPATH` that must not reach the child.

## Requirements

This widget is a front end. On its own it draws a panel that says
`glancectl was not found`; it does nothing until the daemon it talks to is
installed.

| Dependency | Why | Where |
|---|---|---|
| Omarchy shell (Quattro) | hosts the plugin | ships with Omarchy |
| `glanced` | every reading and every action in the panel is a `glancectl` subprocess | [ayan-de/glance-linux](https://github.com/ayan-de/glance-linux) |

The daemon is **not** installed by adding this plugin, and this plugin never
installs, patches, or elevates anything itself — it only runs `glancectl` as
you, and prints the command under every button so you can see what that is.

## Install

```bash
yay -S glanced
omarchy plugin add https://github.com/ayan-de/omarchy-glance.git --enable
```

Then click the bar icon and follow it. The panel asks for one thing at a time
and gives you a button for each:

1. **Start daemon** — `systemctl --user enable --now glanced`
2. **Enroll** — asks you to choose a passphrase (it encrypts your face data
   at rest), then opens the guided sweep in a window; look around as it asks
3. **Wire lock screen** — opens a terminal for `glancectl setup-pam`, which
   needs your password, so you can watch every edit it makes to `/etc/pam.d`

After that the panel just shows state, and your lock screen unlocks by face.

### From a glance-linux checkout

`packaging/install.sh` in the daemon repo links this directory into
`~/.config/omarchy/plugins/` for you, so the shell loads the checkout you are
editing:

```bash
packaging/install.sh
omarchy plugin enable io.github.ayan-de.glance
```

Then set **glancectl path** in the widget's settings to the venv binary,
spelled out in full: `/home/you/glance-linux/.venv/bin/glancectl`. Not `~`,
and not the `~/.local/bin/glancectl` link that `install.sh` makes for your
prompt, because the check above refuses symlinks. Every button then runs the
glancectl you pointed at and nothing else.

## Remove

```bash
omarchy plugin disable io.github.ayan-de.glance
omarchy plugin remove io.github.ayan-de.glance
```

That takes the widget off the bar and deletes the plugin directory. It leaves
the daemon alone: nothing about your enrollment, your PAM stack, or the
`glanced` service belongs to this plugin. To undo those, run
`glancectl setup-pam --remove` and `yay -R glanced` (or
`packaging/install.sh --uninstall` from a checkout).

## Settings

| key | default | meaning |
|---|---|---|
| `refreshIntervalSec` | 30 | idle poll interval; the panel also refreshes on open and after every action |
| `glancectlPath` | `""` (`/usr/bin/glancectl`) | absolute path to `glancectl`; checked as described above before it is run |

## IPC

```bash
omarchy-shell io.github.ayan-de.glance toggle
omarchy-shell io.github.ayan-de.glance refresh
omarchy-shell io.github.ayan-de.glance scan
```

## Tests

```bash
tests/run
```

Runs the `GlanceLogic.js` unit tests under node, `omarchy plugin validate`,
qmllint, and a headless Quickshell harness that instantiates the real panel and
backend against `tests/fake-glancectl` — so the process bridge, the stdin
passphrase hand-off, the status contract, the binary check, the output caps
and the child environment are exercised for real.

## License

MIT — see [LICENSE](LICENSE).
