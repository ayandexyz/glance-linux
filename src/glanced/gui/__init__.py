"""The enrollment window.

Kept behind its own import so nothing in the daemon, the PAM path, or the
headless CLI ever pulls Qt in. `glancectl enroll --gui` is the only caller,
and a missing extra surfaces as an install hint rather than a traceback.
"""

from __future__ import annotations


def run_enrollment(**kwargs) -> tuple[list, list]:
    """Open the guided enrollment window; return `(embeddings, pose_names)`.

    See :mod:`glanced.gui.enroll_window` for the arguments.
    """
    try:
        from .enroll_window import run
    except ImportError as error:  # pragma: no cover - depends on the extra
        raise ImportError(
            "the enrollment window needs PySide6: pip install 'glanced[gui]' "
            "(or run `glancectl enroll` without --gui)"
        ) from error
    return run(**kwargs)
