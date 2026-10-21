"""tools/mklock.py: the offline parts (tags, parsing, merging, checking)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("mklock", ROOT / "tools" / "mklock.py")
mklock = importlib.util.module_from_spec(spec)
sys.modules["mklock"] = mklock
spec.loader.exec_module(mklock)

H = {k: (c * 64) for k, c in {"np_x86": "1", "np_arm": "2", "np_mac": "3", "mp_x86": "4", "ort_x86": "5",
                               "ort_arm": "6", "pkg_any": "7", "crypt_abi3_arm": "8", "np_ft_arm": "9"}.items()}

FILES = {
    ("numpy", "2.5.3"): {
        H["np_x86"]: "numpy-2.5.3-cp314-cp314-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
        H["np_arm"]: "numpy-2.5.3-cp314-cp314-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl",
        H["np_mac"]: "numpy-2.5.3-cp314-cp314-macosx_14_0_arm64.whl",
        H["np_ft_arm"]: "numpy-2.5.3-cp314-cp314t-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl",
    },
    ("mediapipe", "0.10.35"): {H["mp_x86"]: "mediapipe-0.10.35-py3-none-manylinux_2_28_x86_64.whl"},
    ("onnxruntime", "1.30.0"): {
        H["ort_x86"]: "onnxruntime-1.30.0-cp314-cp314-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
        H["ort_arm"]: "onnxruntime-1.30.0-cp314-cp314-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl",
    },
    ("packaging", "26.3"): {H["pkg_any"]: "packaging-26.3-py3-none-any.whl"},
}


def index(name, version):
    return FILES[(name, version)]


@pytest.mark.parametrize("filename,arch,ok", [
    ("numpy-2.5.3-cp314-cp314-manylinux_2_28_aarch64.whl", "aarch64", True),
    ("numpy-2.5.3-cp314-cp314-manylinux_2_28_aarch64.whl", "x86_64", False),
    ("numpy-2.5.3-cp314-cp314t-manylinux_2_28_aarch64.whl", "aarch64", False),
    ("numpy-2.5.3-cp313-cp313-manylinux_2_28_aarch64.whl", "aarch64", False),
    ("numpy-2.5.3-cp314-cp314-musllinux_1_2_aarch64.whl", "aarch64", False),
    ("cryptography-50.0.1-cp311-abi3-manylinux_2_34_aarch64.whl", "aarch64", True),
    ("cryptography-50.0.1-cp315-abi3-manylinux_2_34_aarch64.whl", "aarch64", False),
    ("PySide6-6.11.2-cp39-abi3-manylinux_2_34_x86_64.whl", "x86_64", True),
    ("packaging-26.3-py3-none-any.whl", "aarch64", True),
    ("six-1.17.0-py2.py3-none-any.whl", "x86_64", True),
    ("numpy-2.5.3.tar.gz", "x86_64", False),
])
def test_wheel_supports(filename, arch, ok):
    assert mklock.wheel_supports(filename, arch, "3.14") is ok


COMPILED_X86 = f"""\
mediapipe==0.10.35 \\
    --hash=sha256:{H["mp_x86"]}
numpy==2.5.3 \\
    --hash=sha256:{H["np_x86"]} \\
    --hash=sha256:{H["np_mac"]}
onnxruntime==1.30.0 \\
    --hash=sha256:{H["ort_x86"]}
packaging==26.3 \\
    --hash=sha256:{H["pkg_any"]}
"""
COMPILED_ARM = """\
numpy==2.5.3 \\
    --hash=sha256:{a}
onnxruntime==1.30.0 \\
    --hash=sha256:{b}
packaging==26.3 \\
    --hash=sha256:{c}
""".format(a=H["np_arm"], b=H["ort_arm"], c=H["pkg_any"])


def test_merge_keeps_each_architectures_wheels_and_marks_the_x86_only_package():
    per_arch = {"x86_64": mklock.parse_compiled(COMPILED_X86), "aarch64": mklock.parse_compiled(COMPILED_ARM)}
    entries = mklock.merge(per_arch, "3.14", index)
    by_name = {e.name: e for e in entries}
    assert by_name["numpy"].hashes == sorted([H["np_x86"], H["np_arm"]]), "no macOS or free-threaded wheels"
    assert by_name["numpy"].arches == ("x86_64", "aarch64")
    assert by_name["mediapipe"].arches == ("x86_64",)
    text = mklock.render(entries, ("x86_64", "aarch64"), "# header")
    assert "mediapipe==0.10.35 ; platform_machine == 'x86_64' \\" in text
    assert "numpy==2.5.3 \\" in text

    parsed = mklock.parse_lock(text)
    assert mklock.check(parsed, ("x86_64", "aarch64"), "3.14", index) == []


def test_check_finds_a_missing_architecture_and_foreign_hashes():
    lock = f"""\
numpy==2.5.3 \\
    --hash=sha256:{H["np_x86"]}
onnxruntime==1.30.0 \\
    --hash=sha256:{H["ort_x86"]} \\
    --hash=sha256:{"f" * 64}
"""
    problems = mklock.check(mklock.parse_lock(lock), ("x86_64", "aarch64"), "3.14", index)
    assert any("numpy" in p and "aarch64 wheels pip could pick" in p for p in problems)
    assert any("onnxruntime" in p and "PyPI does not have" in p for p in problems)


def test_versions_that_differ_per_architecture_get_one_marked_line_each():
    files = {("numpy", "2.5.3"): FILES[("numpy", "2.5.3")],
             ("numpy", "2.5.2"): {"a" * 64: "numpy-2.5.2-cp314-cp314-manylinux_2_28_aarch64.whl"}}
    per_arch = {
        "x86_64": {"numpy": mklock.Pin("numpy", "2.5.3")},
        "aarch64": {"numpy": mklock.Pin("numpy", "2.5.2")},
    }
    entries = mklock.merge(per_arch, "3.14", lambda n, v: files[(n, v)])
    text = mklock.render(entries, ("x86_64", "aarch64"), "#")
    assert "numpy==2.5.2 ; platform_machine == 'aarch64'" in text
    assert "numpy==2.5.3 ; platform_machine == 'x86_64'" in text
    assert mklock.check(mklock.parse_lock(text), ("x86_64", "aarch64"), "3.14", lambda n, v: files[(n, v)]) == []


def test_a_local_wheel_is_pinned_by_its_own_hash(tmp_path):
    wheel = tmp_path / "glanced-0.3.7-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    compiled = f"glanced @ file://{wheel} \\\n    --hash=sha256:{'0' * 64}\n"
    pins = mklock.parse_compiled(compiled)
    assert pins["glanced"].version == "0.3.7" and pins["glanced"].local == wheel
    entries = mklock.merge({"x86_64": pins, "aarch64": pins}, "3.14", index)
    import hashlib

    assert entries[0].hashes == [hashlib.sha256(b"wheel").hexdigest()]
    assert entries[0].arches == ("x86_64", "aarch64")


def test_two_lines_for_one_architecture_is_a_problem():
    lock = f"packaging==26.3 \\\n    --hash=sha256:{H['pkg_any']}\npackaging==26.3 ; platform_machine == 'aarch64' \\\n    --hash=sha256:{H['pkg_any']}\n"
    problems = mklock.check(mklock.parse_lock(lock), ("x86_64", "aarch64"), "3.14", index)
    assert any("two lines apply on aarch64" in p for p in problems)
