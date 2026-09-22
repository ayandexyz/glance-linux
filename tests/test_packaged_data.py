"""The wheel's copies of the PAM source and the lock patch.

setuptools can only package files under `src/glanced/`, and the canonical
copies live at the repository root where the Makefile, the PKGBUILD and
`packaging/install.sh` expect them. So the package carries a copy, and this
holds the two byte-identical: a drifted copy would install a PAM module or a
lock patch that is not the one anybody reviewed.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGED = ROOT / "src" / "glanced" / "_data"

PAIRS = [
    (ROOT / "pam" / "pam_glance.c", PACKAGED / "pam" / "pam_glance.c"),
    (ROOT / "pam" / "Makefile", PACKAGED / "pam" / "Makefile"),
]
PAIRS += [
    (source, PACKAGED / "lock-faceid" / source.name)
    for source in sorted((ROOT / "patches" / "omarchy-lock-faceid").iterdir())
    if source.is_file()
]
PAIRS += [
    (
        ROOT / "packaging" / "hooks" / "repair-glance-lock.hook",
        PACKAGED / "hooks" / "repair-glance-lock.hook",
    )
]


@pytest.mark.parametrize("canonical,packaged", PAIRS, ids=lambda p: p.name)
def test_packaged_copy_matches_the_canonical_one(canonical, packaged):
    assert packaged.exists(), f"{packaged} is missing; copy it from {canonical}"
    assert packaged.read_bytes() == canonical.read_bytes(), (
        f"{packaged} has drifted from {canonical}"
    )


def test_every_packaged_file_has_a_canonical_source():
    """A file only in the package is one nobody reviews at the root."""
    known = {packaged for _, packaged in PAIRS}
    for path in PACKAGED.rglob("*"):
        if path.is_file():
            assert path in known, f"{path} has no canonical counterpart"


def test_pam_source_is_found_without_a_checkout():
    """A pip install has no repo; the packaged copy is what it builds from."""
    from glanced import pamsetup

    found = pamsetup.source_dir(None)
    assert found is not None, "setup-pam would have nothing to build"
    assert (found / "pam_glance.c").exists()


def test_a_checkout_wins_over_the_packaged_pam_source(tmp_path):
    """A developer builds what they are editing, not the installed copy."""
    from glanced import pamsetup

    checkout = tmp_path / "pam"
    checkout.mkdir()
    (checkout / "pam_glance.c").write_text("/* being edited */\n")
    assert pamsetup.source_dir(checkout) == checkout


def test_lock_patch_is_found_without_a_checkout(monkeypatch):
    from glanced import locksetup

    monkeypatch.setattr(locksetup, "_repo_patch_dir", lambda: None)
    found = locksetup.patch_dir()
    assert found is not None, "setup-lock would have nothing to apply"
    assert (found / "apply.sh").exists()


def test_the_distribution_package_still_wins(monkeypatch, tmp_path):
    """The root-owned copy is the one the post-update hook may run."""
    from glanced import locksetup

    packaged = tmp_path / "packaged"
    packaged.mkdir()
    (packaged / "apply.sh").write_text("#!/bin/sh\n")
    monkeypatch.setattr(locksetup, "PACKAGED_PATCH_DIR", packaged)
    assert locksetup.patch_dir() == packaged


def test_the_repair_hook_ships_in_the_package():
    """Applied and then reverted by the next `omarchy update` is worse than
    never applied: this file is what puts the indicator back, and a wheel
    without it leaves a pip install with nothing to install."""
    packaged = PACKAGED / "hooks" / "repair-glance-lock.hook"
    assert packaged.exists()
    assert packaged.read_text().startswith("#!")


def test_hook_source_prefers_the_distribution_package(monkeypatch, tmp_path):
    from glanced import locksetup

    packaged = tmp_path / "repair-glance-lock.hook"
    packaged.write_text("#!/bin/bash\n")
    monkeypatch.setattr(locksetup, "PACKAGED_HOOK", packaged)
    assert locksetup._hook_source() == packaged
