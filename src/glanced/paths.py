"""Where things live on disk.

Everything is under the XDG data directory by default, with one concession to
a source checkout: a `models/` directory next to `src/` is searched first, so
`glancectl live` works straight out of a clone without copying files around.

Every path can be overridden through the environment, which is also how the
tests and the plugin's harness point a daemon at a scratch directory.
"""

from __future__ import annotations

import os
from pathlib import Path

_REPO_MODELS = Path(__file__).resolve().parents[2] / "models"

DATA_DIR = Path(os.environ.get("GLANCE_DATA_DIR") or Path(
    os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
) / "glance")

#: Where `glancectl fetch-model` writes, and the first place models are looked for.
MODEL_DIR = Path(os.environ.get("GLANCE_MODEL_DIR") or DATA_DIR / "models")

STORE_PATH = Path(os.environ.get("GLANCE_STORE") or DATA_DIR / "enrollment.bin")

#: Optional. If present (0600), the daemon arms itself at startup with the
#: passphrase inside — the price of not having to run `glancectl arm` after
#: every login. Written only by `glancectl arm --remember`.
PASSPHRASE_FILE = Path(os.environ.get("GLANCE_PASSPHRASE_FILE") or DATA_DIR / "passphrase")

LANDMARKER_NAME = "face_landmarker.task"
ARCFACE_NAME = "arcface.onnx"


def find_model(name: str) -> Path:
    """First existing copy of `name`, else where it would be fetched to."""
    for candidate in (MODEL_DIR / name, _REPO_MODELS / name):
        if candidate.exists():
            return candidate
    return MODEL_DIR / name


def landmarker_task() -> Path:
    return Path(os.environ.get("GLANCE_LANDMARKER_TASK") or find_model(LANDMARKER_NAME))


def arcface_model() -> Path:
    return Path(os.environ.get("GLANCE_ARCFACE_MODEL") or find_model(ARCFACE_NAME))
