"""`glancectl setup-lock` — the Face ID-style indicator on the Omarchy lock
screen, and the hook that keeps it there across `omarchy update`.

The indicator is a patch to Omarchy's own lock plugin under
`/usr/share/omarchy/shell/plugins/lock/` (see `patches/omarchy-lock-faceid/`),
because nothing outside the session-lock surface can draw over it. That
directory is package-owned, so every `omarchy update` puts the stock files
back and the indicator silently disappears — until the next lock screen.

Two things here close that gap:

* `status()` reports whether the patch is currently applied. The daemon puts
  it on the status socket, so the bar widget shows "indicator missing" the
  moment an update reverted it rather than the user finding out at the lock
  screen.
* `setup()` applies the patch (sudo, interactive) and installs a
  `post-update` hook with `omarchy hook install`. The hook runs at the end of
  `omarchy update`, when sudo is still authenticated from the package step,
  and re-applies the patch non-interactively. It only ever executes files
  from a root-owned directory — the AUR package's copy — so a warm sudo
  timestamp cannot be used to run whatever happens to be in a user-writable
  checkout. From a source checkout the hook is still installed, but it only
  notifies; rerun `setup-lock` by hand.

Face unlock itself does not depend on any of this: `pam_glance` is wired by
`setup-pam` and keeps working with the stock lock screen. The indicator is the
part you see.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

#: What the patched Service.qml contains and the stock one does not.
MARKER = "faceMessagePrefix"

#: Root-owned, installed by the AUR package: the only source the hook trusts.
PACKAGED_PATCH_DIR = Path("/usr/share/glanced/lock-faceid")
PACKAGED_HOOK = Path("/usr/share/glanced/hooks/repair-glance-lock.hook")

HOOK_NAME = "repair-glance-lock.hook"


DEFAULT_OMARCHY_PATH = Path("/usr/share/omarchy")

#: Process names the Omarchy shell runs under.
SHELL_PROCESSES = ("quickshell", "qs")


def _running_shell_omarchy_path(proc: Path = Path("/proc")) -> Optional[Path]:
    """OMARCHY_PATH as the user's running Omarchy shell sees it, if it has one.

    The shell can run from a checkout (`OMARCHY_PATH=~/.local/share/omarchy`)
    while a terminal, the systemd user service, or `sudo` has no such
    variable. The lock plugin to patch is the one the shell loads, so ask the
    shell. Only processes owned by this user are read.
    """
    uid = os.getuid()
    try:
        entries = list(proc.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            if (entry / "comm").read_text().strip() not in SHELL_PROCESSES:
                continue
            environ = (entry / "environ").read_bytes()
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        for item in environ.split(b"\0"):
            if item.startswith(b"OMARCHY_PATH="):
                value = item.split(b"=", 1)[1].decode(errors="replace")
                if value:
                    return Path(value)
        # No variable: the config path it was started with, e.g.
        # `quickshell -p ~/.local/share/omarchy/shell/shell.qml`.
        for arg in cmdline.split(b"\0"):
            candidate = Path(arg.decode(errors="replace"))
            if not candidate.is_absolute():
                continue
            for root in (candidate, *candidate.parents):
                if (root / "shell" / "shell.qml").is_file():
                    return root
    return None


def omarchy_path() -> Path:
    """`$OMARCHY_PATH`, else the running shell's, else /usr/share/omarchy."""
    configured = os.environ.get("OMARCHY_PATH")
    if configured:
        return Path(configured)
    return _running_shell_omarchy_path() or DEFAULT_OMARCHY_PATH


def lock_dir() -> Path:
    return omarchy_path() / "shell" / "plugins" / "lock"


def user_hook_path() -> Path:
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config / "omarchy" / "hooks" / "post-update.d" / HOOK_NAME


def _repo_patch_dir() -> Optional[Path]:
    candidate = Path(__file__).resolve().parents[2] / "patches" / "omarchy-lock-faceid"
    return candidate if (candidate / "apply.sh").exists() else None


def _installed_patch_dir() -> Optional[Path]:
    """The copy inside the package, which is all a pip install has."""
    candidate = Path(__file__).resolve().parent / "_data" / "lock-faceid"
    return candidate if (candidate / "apply.sh").exists() else None


def patch_dir() -> Optional[Path]:
    """Where `apply.sh` lives.

    The distribution package's copy first, because it is root-owned and is
    what the post-update hook is allowed to run; then a source checkout, so a
    developer patches what they are editing; then the copy inside the wheel,
    which is what a `pip install` has and the reason this is not simply the
    first two.
    """
    if (PACKAGED_PATCH_DIR / "apply.sh").exists():
        return PACKAGED_PATCH_DIR
    return _repo_patch_dir() or _installed_patch_dir()


def _patched(service: Path) -> bool:
    try:
        return service.exists() and MARKER in service.read_text()
    except OSError:
        return False


def status() -> dict:
    """What the status socket reports, and what the plugin renders."""
    service = lock_dir() / "Service.qml"
    return {
        "available": service.exists(),
        "patched": _patched(service),
        "hook": user_hook_path().exists(),
    }


# --- editing ------------------------------------------------------------------


def _sudo(*command: str) -> None:
    # sudo resets the environment, and apply.sh/revert.sh fall back to
    # /usr/share/omarchy without OMARCHY_PATH: pass on the one resolved here,
    # or a shell running from a checkout gets the package copy patched.
    subprocess.run(["sudo", "env", f"OMARCHY_PATH={omarchy_path()}", *command], check=True)


def _hook_source() -> Optional[Path]:
    """The hook file: the distribution package, a checkout, then the wheel's copy.

    Without the last one a pip install applies the indicator and then has
    nothing to repair it, so the next `omarchy update` quietly takes it away
    again.
    """
    if PACKAGED_HOOK.exists():
        return PACKAGED_HOOK
    checkout = Path(__file__).resolve().parents[2] / "packaging" / "hooks" / HOOK_NAME
    if checkout.exists():
        return checkout
    installed = Path(__file__).resolve().parent / "_data" / "hooks" / HOOK_NAME
    return installed if installed.exists() else None


def _install_hook(log) -> None:
    source = _hook_source()
    omarchy = shutil.which("omarchy")
    if source is None or omarchy is None:
        log("post-update hook not installed (no `omarchy` on PATH or no hook file); "
            "rerun `glancectl setup-lock` after each `omarchy update`")
        return
    subprocess.run([omarchy, "hook", "install", "post-update", str(source)], check=True)
    log(f"  hook:   {user_hook_path()} (re-applies the indicator after `omarchy update`)")
    if source != PACKAGED_HOOK:
        log("          from a source checkout it can only notify; the packaged one repairs")


def _remove_hook(log) -> None:
    hook = user_hook_path()
    if hook.exists():
        hook.unlink()
        log(f"  removed {hook}")


def setup(*, remove: bool = False, log=print) -> int:
    lock = lock_dir()
    if not (lock / "Service.qml").exists():
        log(f"no Omarchy lock plugin at {lock}; nothing to patch")
        return 1
    source = patch_dir()
    if source is None:
        log("the indicator files are missing: install the glanced package or run from a checkout")
        return 1

    if remove:
        _sudo("bash", str(source / "revert.sh"))
        _remove_hook(log)
        log("indicator removed; restart the shell with: omarchy-restart-shell")
        return 0

    if status()["patched"]:
        log(f"indicator already applied to {lock}")
    else:
        log(f"applying the indicator to {lock} (asks for your password) ...")
        _sudo("bash", str(source / "apply.sh"))
    _install_hook(log)
    log("done. Restart the shell to load it: omarchy-restart-shell")
    return 0
