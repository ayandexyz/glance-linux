# Glance — Omarchy bar widget

The shell half of [glance-linux](../README.md): one bar icon and one panel for
the `glanced` face-unlock daemon.

- **Bar icon** shows the face-recognition glyph; it lights while a scan is in
  flight, including a scan started from the lock screen. Right-click runs a
  test scan.
- **Panel** shows whether the daemon is armed, who is enrolled, how the lock
  screen is wired (on Enter, hands-free, or not yet), what the last scan
  decided and why, and the one command you need next when something is
  missing. Inline **Arm** takes the passphrase (over stdin, never argv) and can
  remember it so the daemon arms itself at login. **Disarm** and **Test scan**
  are one click each.

Presentation only, by construction: everything the panel does is a `glancectl`
invocation run as you. Nothing here is on the unlock path, and if the shell is
not running PAM talks to the daemon exactly the same.

## Install (source checkout)

```bash
packaging/install.sh                 # user service + symlink into ~/.config/omarchy/plugins/
omarchy plugin enable ayande.glance  # put the widget on the bar
```

Or by hand:

```bash
ln -s "$PWD/plugin" ~/.config/omarchy/plugins/ayande.glance
omarchy-shell shell rescanPlugins
omarchy plugin enable ayande.glance
```

If `glancectl` is not on your PATH (it lives in the checkout's `.venv/bin/`),
set **glancectl path** in the widget's settings to the venv binary.

## Settings

| key | default | meaning |
|---|---|---|
| `refreshIntervalSec` | 30 | idle poll interval; the panel also refreshes on open and after every action |
| `glancectlPath` | `""` (PATH) | path to `glancectl` |

## IPC

```bash
omarchy-shell ayande.glance toggle
omarchy-shell ayande.glance refresh
omarchy-shell ayande.glance scan
```

## Tests

```bash
plugin/tests/run
```

Runs the `GlanceLogic.js` unit tests under node, `omarchy plugin validate`,
qmllint, and a headless Quickshell harness that instantiates the real panel and
backend against `tests/fake-glancectl` — so the process bridge, the stdin
passphrase hand-off, and the status contract are exercised for real.
