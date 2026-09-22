"""`glancectl fetch-model` — download the two neural networks the daemon needs.

Neither is vendored: the landmarker is Google's, the recognizer is
InsightFace's, and both are large. They are fetched from their upstream
release locations and dropped into `paths.MODEL_DIR`.

The ArcFace variants match upstream's `tools/convert_arcface.py`: `mbf` is the
MobileFaceNet backbone from the `buffalo_s` pack (~13MB, fast on CPU) and is
the default there too; `r50` is the ResNet50 from `buffalo_l` (~166MB) for a
little more accuracy at a real CPU cost per frame.
"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import paths

LANDMARKER_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
    "face_landmarker/float16/1/face_landmarker.task"
)

ARCFACE_PACKS = {
    "mbf": (
        "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_s.zip",
        "w600k_mbf.onnx",
    ),
    "r50": (
        "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip",
        "w600k_r50.onnx",
    ),
}

DEFAULT_VARIANT = "mbf"


def _download(url: str, label: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "glanced/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        buffer = io.BytesIO()
        received = 0
        while True:
            chunk = response.read(1 << 16)
            if not chunk:
                break
            buffer.write(chunk)
            received += len(chunk)
            if total:
                sys.stderr.write(f"\r{label}: {received / 1e6:6.1f} / {total / 1e6:.1f} MB")
            else:
                sys.stderr.write(f"\r{label}: {received / 1e6:6.1f} MB")
        sys.stderr.write("\n")
    return buffer.getvalue()


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(data)
        temporary = Path(handle.name)
    temporary.replace(path)


def fetch(variant: str = DEFAULT_VARIANT, *, force: bool = False, model_dir: Path = paths.MODEL_DIR) -> None:
    landmarker = model_dir / paths.LANDMARKER_NAME
    if force or not paths.landmarker_task().exists():
        _write_atomic(landmarker, _download(LANDMARKER_URL, "face_landmarker.task"))
        print(f"wrote {landmarker}")
    else:
        print(f"landmarker already present: {paths.landmarker_task()}")

    arcface = model_dir / paths.ARCFACE_NAME
    if force or not paths.arcface_model().exists():
        url, member = ARCFACE_PACKS[variant]
        archive = zipfile.ZipFile(io.BytesIO(_download(url, Path(url).name)))
        names = archive.namelist()
        match = next((n for n in names if n.endswith(member)), None)
        if match is None:
            raise RuntimeError(f"{member} not found in {url}; archive holds {names}")
        _write_atomic(arcface, archive.read(match))
        print(f"wrote {arcface} ({member})")
    else:
        print(f"ArcFace model already present: {paths.arcface_model()}")


def status() -> dict[str, bool]:
    return {
        "landmarker": paths.landmarker_task().exists(),
        "arcface": paths.arcface_model().exists(),
    }


def clear(model_dir: Path = paths.MODEL_DIR) -> None:
    shutil.rmtree(model_dir, ignore_errors=True)
