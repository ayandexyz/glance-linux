"""`glancectl fetch-model` — download the neural networks the daemon needs.

None is vendored: the landmarker is Google's, the recognizer is
InsightFace's, and both are large. They are fetched from their upstream
release locations and dropped into `paths.MODEL_DIR`.

Which landmarker files depends on the backend (`landmarker.backend()`):
MediaPipe's `face_landmarker.task`, or for the onnxruntime backend
`face_detector.onnx` and `face_landmarks.onnx`, the two networks inside that
same task file converted from TFLite to ONNX (tf2onnx 1.17.0, opset 17; see
`packaging/MODELS.md`). tf2onnx output is not byte-reproducible, so
those two are published once as release assets and pinned by hash like
everything else. The face geometry metadata the onnx backend also needs is
small and ships inside the package.

The ArcFace variants match upstream's `tools/convert_arcface.py`: `mbf` is the
MobileFaceNet backbone from the `buffalo_s` pack (~13MB, fast on CPU) and is
the default there too; `r50` is the ResNet50 from `buffalo_l` (~166MB) for a
little more accuracy at a real CPU cost per frame.

Every model is pinned to a SHA-256, the same way the lock pins every wheel:
a download (and, for ArcFace, both the archive and the member taken out of
it) is written to the model directory only if its hash matches, and the
landmarker and the embedder check the file again each time they load it. A
model that changed on disk, or a different one dropped in its place, is
refused with a clear error rather than run. `fetch-model --from DIR` (or
`$GLANCE_MODEL_SOURCE`) installs from a local copy instead of the network,
with the same check.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from . import paths


class ModelIntegrityError(FileNotFoundError):
    """No usable model: missing, or its bytes are not the pinned ones.

    A FileNotFoundError, so everything that already handles a missing model
    (the daemon, enroll, the GUI) handles a wrong one the same way.
    """


@dataclass(frozen=True)
class ModelSpec:
    #: The file name in the model directory.
    name: str
    #: SHA-256 of that file.
    sha256: str
    #: A direct download of exactly this file.
    url: Optional[str] = None
    #: Or a zip holding it: (url, sha256 of the zip, member name).
    archive: Optional[tuple[str, str, str]] = None
    #: Other names a local source directory may carry the same bytes under.
    source_names: tuple[str, ...] = ()


LANDMARKER_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
    "face_landmarker/float16/1/face_landmarker.task"
)

LANDMARKER = ModelSpec(
    name=paths.LANDMARKER_NAME,
    sha256="64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff",
    url=LANDMARKER_URL,
)

#: Where the converted ONNX networks are published. A release asset of this
#: repository; $GLANCE_ONNX_MODELS_URL points at a mirror (the hashes below
#: still decide what is accepted).
ONNX_MODELS_URL = os.environ.get(
    "GLANCE_ONNX_MODELS_URL", "https://github.com/ayandexyz/glance-linux/releases/download/models-v1"
).rstrip("/")

FACE_DETECTOR = ModelSpec(
    name=paths.FACE_DETECTOR_NAME,
    sha256="e3e5ec1d977e912482b4770aac7a5b4562c01630aaec72fcfab9171c8a5b0786",
    url=f"{ONNX_MODELS_URL}/{paths.FACE_DETECTOR_NAME}",
)
FACE_MESH = ModelSpec(
    name=paths.FACE_MESH_NAME,
    sha256="0bc321f8fe70ad861e99fe878a7990dd52c89db902ea7d5bfb613898408244af",
    url=f"{ONNX_MODELS_URL}/{paths.FACE_MESH_NAME}",
    source_names=("face_landmarks_detector.onnx",),
)

#: geometry_pipeline_metadata_landmarks.binarypb, packaged in glanced/_data.
GEOMETRY_SHA256 = "bdbcda96dfcb7da883da124aaa2c55dee49770d934f0fcc71747f8c21bdc75b4"

ARCFACE_VARIANTS = {
    "mbf": ModelSpec(
        name=paths.ARCFACE_NAME,
        sha256="9cc6e4a75f0e2bf0b1aed94578f144d15175f357bdc05e815e5c4a02b319eb4f",
        archive=(
            "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_s.zip",
            "d85a87f503f691807cd8bb97128bdf7a0660326cd9cd02657127fa978bab8b5e",
            "w600k_mbf.onnx",
        ),
        source_names=("w600k_mbf.onnx",),
    ),
    "r50": ModelSpec(
        name=paths.ARCFACE_NAME,
        sha256="4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43",
        archive=(
            "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip",
            "80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f",
            "w600k_r50.onnx",
        ),
        source_names=("w600k_r50.onnx",),
    ),
}

#: Kept for anything that read the old table: variant -> (url, member).
ARCFACE_PACKS = {variant: (spec.archive[0], spec.archive[2]) for variant, spec in ARCFACE_VARIANTS.items()}

DEFAULT_VARIANT = "mbf"

#: Every hash an `arcface.onnx` may have: either variant is a valid install.
ARCFACE_HASHES = frozenset(spec.sha256 for spec in ARCFACE_VARIANTS.values())

_hash_cache: dict[tuple[str, int, int, int], str] = {}


def sha256_file(path: Path) -> str:
    """SHA-256 of a file, cached on (path, inode, size, mtime) so the panel's
    status poll does not re-read the models every time."""
    path = Path(path)
    st = path.stat()
    key = (str(path), st.st_ino, st.st_size, st.st_mtime_ns)
    cached = _hash_cache.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    _hash_cache[key] = value
    return value


def verify(path: Path, allowed: str | Iterable[str], label: str = "") -> Path:
    """Return `path` if its SHA-256 is one of `allowed`, else raise."""
    path = Path(path)
    allowed_set = {allowed} if isinstance(allowed, str) else set(allowed)
    label = label or path.name
    if not path.is_file():
        raise ModelIntegrityError(f"{label} not found at {path}. Fetch it with `glancectl fetch-model`.")
    actual = sha256_file(path)
    if actual not in allowed_set:
        raise ModelIntegrityError(
            f"{label} at {path} has sha256 {actual}, which is not the pinned model. "
            "Not loading it. Reinstall it with `glancectl fetch-model --force`."
        )
    return path


def is_valid(path: Path, allowed: str | Iterable[str]) -> bool:
    try:
        verify(path, allowed)
        return True
    except OSError:
        return False


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
    os.chmod(temporary, 0o644)
    temporary.replace(path)


def _check(data: bytes, expected: str, what: str) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ModelIntegrityError(f"{what} has sha256 {actual}, expected {expected}; not installed")


def source_dirs(explicit: Optional[Path] = None) -> list[Path]:
    """Local directories to install from before the network: `--from`, then
    `$GLANCE_MODEL_SOURCE`."""
    candidates = [Path(explicit)] if explicit is not None else []
    if os.environ.get("GLANCE_MODEL_SOURCE"):
        candidates.append(Path(os.environ["GLANCE_MODEL_SOURCE"]))
    return [c for c in candidates if c.is_dir()]


def _find_local(spec: ModelSpec, sources: list[Path]) -> Optional[Path]:
    for directory in sources:
        for name in (spec.name, *spec.source_names):
            candidate = directory / name
            if candidate.is_file() and sha256_file(candidate) == spec.sha256:
                return candidate
    return None


def install(spec: ModelSpec, target: Path, *, sources: list[Path] = (), force: bool = False,
            label: str = "") -> None:
    """Put `spec` at `target`, verified, from a local source or its download."""
    label = label or spec.name
    if not force and is_valid(target, spec.sha256):
        print(f"{label} already present: {target}")
        return
    local = _find_local(spec, list(sources))
    if local is not None:
        _write_atomic(target, local.read_bytes())
        verify(target, spec.sha256, label)
        print(f"wrote {target} from {local} (sha256 ok)")
        return
    if spec.url is not None:
        data = _download(spec.url, spec.name)
        _check(data, spec.sha256, spec.url)
        _write_atomic(target, data)
        print(f"wrote {target} (sha256 ok)")
        return
    if spec.archive is not None:
        url, archive_sha256, member = spec.archive
        blob = _download(url, Path(url).name)
        _check(blob, archive_sha256, url)
        archive = zipfile.ZipFile(io.BytesIO(blob))
        names = archive.namelist()
        match = next((n for n in names if n.endswith(member)), None)
        if match is None:
            raise ModelIntegrityError(f"{member} not found in {url}; archive holds {names}")
        data = archive.read(match)
        _check(data, spec.sha256, f"{member} in {url}")
        _write_atomic(target, data)
        print(f"wrote {target} ({member}, sha256 ok)")
        return
    where = ", ".join(str(s) for s in sources) or "none given"
    raise ModelIntegrityError(
        f"no verified copy of {spec.name} (sha256 {spec.sha256}) and no download for it; "
        f"local sources searched: {where}"
    )


def _active_backend() -> str:
    from .landmarker import backend

    return backend()


def landmarker_specs(backend: Optional[str] = None) -> list[ModelSpec]:
    """The landmarker files `backend` (default: the active one) loads."""
    backend = backend or _active_backend()
    if backend == "mediapipe":
        return [LANDMARKER]
    if backend == "onnx":
        return [FACE_DETECTOR, FACE_MESH]
    raise ValueError(f"unknown landmarker backend {backend!r}")


def fetch(
    variant: str = DEFAULT_VARIANT,
    *,
    force: bool = False,
    model_dir: Path = paths.MODEL_DIR,
    source: Optional[Path] = None,
    backend: Optional[str] = None,
) -> None:
    """Install the landmarker for `backend` (default: the active one; "all"
    for both) and ArcFace into `model_dir`, verified. Raises
    ModelIntegrityError when a model cannot be had with the pinned bytes."""
    sources = source_dirs(source)
    model_dir = Path(model_dir)
    specs = [LANDMARKER, FACE_DETECTOR, FACE_MESH] if backend == "all" else landmarker_specs(backend)
    for spec in specs:
        install(spec, model_dir / spec.name, sources=sources, force=force, label=spec.name)

    arcface = ARCFACE_VARIANTS[variant]
    target = model_dir / arcface.name
    if not force and is_valid(target, ARCFACE_HASHES) and not is_valid(target, arcface.sha256):
        print(f"ArcFace model already present as the other variant: {target}; --force replaces it")
        return
    install(arcface, target, sources=sources, force=force, label="ArcFace model")


#: Model file -> the `paths` function that says where it is loaded from.
_SPEC_PATHS = {
    paths.LANDMARKER_NAME: "landmarker_task",
    paths.FACE_DETECTOR_NAME: "face_detector_model",
    paths.FACE_MESH_NAME: "face_mesh_model",
}


def status() -> dict[str, bool]:
    """Present *and* matching its pin. `landmarker` means every file the
    active backend loads."""
    try:
        specs = landmarker_specs()
    except ValueError:
        specs = []
    return {
        "landmarker": bool(specs) and all(
            is_valid(getattr(paths, _SPEC_PATHS[s.name])(), s.sha256) for s in specs),
        "arcface": is_valid(paths.arcface_model(), ARCFACE_HASHES),
    }


def clear(model_dir: Path = paths.MODEL_DIR) -> None:
    shutil.rmtree(model_dir, ignore_errors=True)
