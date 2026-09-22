"""`glancectl setup-pam` — wire pam_glance into the lock screen, and only there.

Omarchy has had two lock screens, and both talk PAM:

* **hyprlock** — `/etc/pam.d/hyprlock`. Auth runs when the user presses
  Enter. With `ignore_empty_input = false` in hyprlock.conf, an empty Enter
  is enough to trigger a face scan.
* **The Omarchy shell lock** (Quickshell) — `/etc/pam.d/omarchy-lock-password`
  for the typed password, and, only when a fingerprint is enrolled,
  `/etc/pam.d/omarchy-lock-fingerprint`, which the shell starts *by itself*
  the moment the screen locks and retries until it succeeds. The shell drops
  an empty password before it reaches PAM, so on the password stack a face
  scan needs any character plus Enter.

Two placements, then:

* **on demand** (default): `auth sufficient pam_glance.so` at the top of the
  password stack(s). The camera only runs when you ask. This is Howdy's model.
* **hands-free** (`--hands-free`): the same line ahead of `pam_fprintd.so` in
  the shell's fingerprint stack. The shell scans your face as soon as it
  locks and again after every fingerprint timeout, so the camera cycles for
  as long as the screen stays locked. Needs an enrolled fingerprint, because
  that is what makes the shell run the stack at all.

Every edit is idempotent, keeps a `.glance-backup` beside the file, and goes
through `sudo` one file at a time so the terminal shows exactly what is being
touched. `--remove` strips the line from every stack again.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

PAM_DIR = Path("/etc/pam.d")
MODULE = Path("/usr/lib/security/pam_glance.so")
MODULE_LINE = "auth       sufficient                  pam_glance.so"

HYPRLOCK = PAM_DIR / "hyprlock"
SHELL_PASSWORD = PAM_DIR / "omarchy-lock-password"
SHELL_FINGERPRINT = PAM_DIR / "omarchy-lock-fingerprint"


def _wired(path: Path) -> bool:
    try:
        return path.exists() and "pam_glance.so" in path.read_text()
    except OSError:
        return False


def status() -> dict:
    """What the status socket reports, and what the plugin renders."""
    hyprlock, password, fingerprint = _wired(HYPRLOCK), _wired(SHELL_PASSWORD), _wired(SHELL_FINGERPRINT)
    return {
        "module": MODULE.exists(),
        "hyprlock": hyprlock,
        "shellPassword": password,
        "shellFingerprint": fingerprint,
        "wired": hyprlock or password or fingerprint,
    }


# --- editing ------------------------------------------------------------------


def _insert_on_demand(text: str) -> str:
    """Put the module line before the first `auth` line, after any header comments."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() and not line.lstrip().startswith("#"):
            lines.insert(index, MODULE_LINE)
            break
    else:
        lines.append(MODULE_LINE)
    return "\n".join(lines) + "\n"


def _insert_before_fprintd(text: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if "pam_fprintd.so" in line and not line.lstrip().startswith("#"):
            lines.insert(index, MODULE_LINE)
            break
    else:
        return _insert_on_demand(text)
    return "\n".join(lines) + "\n"


def _strip(text: str) -> str:
    return "".join(line for line in text.splitlines(keepends=True) if "pam_glance.so" not in line)


def _sudo(*command: str, stdin: Optional[str] = None) -> None:
    subprocess.run(["sudo", *command], input=stdin, text=True, check=True)


def _write(path: Path, content: str, log) -> None:
    backup = path.with_name(path.name + ".glance-backup")
    if not backup.exists():
        _sudo("cp", "-p", str(path), str(backup))
        log(f"  backup: {backup}")
    _sudo("tee", str(path), stdin=content)
    log(f"  wrote:  {path}")


def _installed_pam_dir() -> Optional[Path]:
    """The module's source inside the package, which is all a pip install has."""
    candidate = Path(__file__).resolve().parent / "_data" / "pam"
    return candidate if (candidate / "pam_glance.c").exists() else None


def source_dir(repo_pam_dir: Optional[Path] = None) -> Optional[Path]:
    """Where pam_glance.c is: a checkout being edited, else the packaged copy."""
    if repo_pam_dir is not None and (repo_pam_dir / "pam_glance.c").exists():
        return repo_pam_dir
    return _installed_pam_dir()


def _ensure_module(repo_pam_dir: Optional[Path], log) -> bool:
    if MODULE.exists():
        return True

    source = source_dir(repo_pam_dir)
    if source is None:
        log(f"{MODULE} is missing and there is no pam_glance.c to build it from")
        return False

    # The module is C, so a wheel can ship the source but not the object. A
    # machine without a compiler gets told what to install rather than a make
    # traceback.
    if shutil.which("make") is None or shutil.which("cc") is None:
        log(f"{MODULE} is missing and building it needs a compiler: install base-devel")
        return False

    log("building pam_glance.so ...")
    try:
        subprocess.run(["make", "-C", str(source)], check=True)
    except subprocess.CalledProcessError:
        log("could not build pam_glance.so; the PAM headers come with the 'pam' package")
        return False
    _sudo("make", "-C", str(source), "install")
    return MODULE.exists()


def setup(*, hands_free: bool = False, remove: bool = False, repo_pam_dir: Optional[Path] = None, log=print) -> int:
    if remove:
        touched = 0
        for path in (HYPRLOCK, SHELL_PASSWORD, SHELL_FINGERPRINT):
            if _wired(path):
                _write(path, _strip(path.read_text()), log)
                touched += 1
        log(f"removed pam_glance from {touched} stack(s); the module file itself is left in place")
        return 0

    if not _ensure_module(repo_pam_dir, log):
        return 1

    # A fresh Omarchy that has never run its lock setup has no PAM file for the
    # shell lock at all — which also means the shell lock cannot lock. Its own
    # helper creates both files, and the fingerprint one only if a print is
    # enrolled, so defer to it rather than re-implement the policy.
    if shutil.which("omarchy-apply-lock") and not SHELL_PASSWORD.exists():
        log("shell lock has no PAM configuration yet; running omarchy-apply-lock ...")
        _sudo("omarchy-apply-lock")

    wired_any = False
    if hands_free:
        if not SHELL_FINGERPRINT.exists():
            log(
                "hands-free needs the Omarchy shell lock with an enrolled fingerprint "
                "(the shell only runs that stack automatically when one exists). "
                "Falling back to on-demand."
            )
        elif _wired(SHELL_FINGERPRINT):
            log(f"already wired: {SHELL_FINGERPRINT}")
            wired_any = True
        else:
            _write(SHELL_FINGERPRINT, _insert_before_fprintd(SHELL_FINGERPRINT.read_text()), log)
            log("hands-free: the shell scans your face as soon as it locks, then falls back to fingerprint, and repeats")
            wired_any = True

    for path, hint in (
        (SHELL_PASSWORD, "shell lock: type any character and press Enter to scan"),
        (HYPRLOCK, "hyprlock: press Enter to scan (set `ignore_empty_input = false` in hyprlock.conf so an empty Enter counts)"),
    ):
        if not path.exists():
            continue
        if hands_free and path is SHELL_PASSWORD and _wired(SHELL_FINGERPRINT):
            # Hands-free already covers the shell lock; leaving the password
            # stack alone keeps typing a password from waiting on the camera.
            continue
        if _wired(path):
            log(f"already wired: {path}")
        else:
            _write(path, _insert_on_demand(path.read_text()), log)
        log(f"  {hint}")
        wired_any = True

    if not wired_any:
        log("no lock screen PAM stack found to wire (expected /etc/pam.d/hyprlock or omarchy-lock-password)")
        return 1
    log("\nface unlock is wired. Keep this terminal open and test the lock screen now; "
        "`glancectl setup-pam --remove` undoes it.")
    return 0
