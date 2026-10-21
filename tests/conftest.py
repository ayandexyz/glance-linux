import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _private_bin_dir(tmp_path_factory, monkeypatch):
    """`setup` links glancectl into $XDG_BIN_HOME; never the real ~/.local/bin."""
    monkeypatch.setenv("XDG_BIN_HOME", str(tmp_path_factory.mktemp("bin-home")))
