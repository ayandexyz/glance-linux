"""The enrollment window, and the passphrase prompt that precedes it.

Kept behind its own import so nothing in the daemon, the PAM path, or the
headless CLI ever pulls Qt in. `glancectl enroll --gui` is the only caller,
and a missing extra surfaces as an install hint rather than a traceback.
"""

from __future__ import annotations

from typing import Optional

_MISSING = (
    "the enrollment window needs PySide6: pip install 'glanced[gui]' "
    "(or run `glancectl enroll` without --gui)"
)


def available() -> bool:
    """Whether the window can be opened at all, without importing Qt.

    `glancectl status` reports this so a caller with a button rather than a
    terminal -- the Omarchy plugin -- can choose the terminal enrollment
    instead of pressing a button that can only fail.
    """
    from importlib.util import find_spec

    try:
        return find_spec("PySide6") is not None
    except (ImportError, ValueError):
        return False


def ask_passphrase(*, confirm: bool, error: str = "") -> Optional[str]:
    """Prompt for the passphrase in a window; None if the user cancelled.

    For an enrollment started from a button rather than a prompt: there is no
    terminal for getpass, and argv is not a place for a secret.
    """
    try:
        from .passphrase import ask
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise ImportError(_MISSING) from exc
    return ask(confirm=confirm, error=error)


def run_enrollment(**kwargs) -> tuple[list, list]:
    """Open the guided enrollment window; return `(embeddings, pose_names)`.

    See :mod:`glanced.gui.enroll_window` for the arguments.
    """
    try:
        from .enroll_window import run
    except ImportError as error:  # pragma: no cover - depends on the extra
        raise ImportError(_MISSING) from error
    return run(**kwargs)
