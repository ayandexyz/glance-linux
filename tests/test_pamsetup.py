"""The PAM-stack edits, as pure text transforms; sudo never runs here."""

from __future__ import annotations

from glanced import pamsetup

HYPRLOCK = """# PAM configuration file for hyprlock
# the 'login' configuration file (see /etc/pam.d/login)

auth        include     login
"""

FINGERPRINT = """#%PAM-1.0
auth       required                    pam_fprintd.so
account    include                     system-local-login
"""


def test_on_demand_goes_before_the_first_auth_line():
    out = pamsetup._insert_on_demand(HYPRLOCK)
    lines = out.splitlines()
    assert lines[:2] == HYPRLOCK.splitlines()[:2]
    assert lines.index(pamsetup.MODULE_LINE) < lines.index("auth        include     login")


def test_hands_free_goes_before_fprintd():
    out = pamsetup._insert_before_fprintd(FINGERPRINT).splitlines()
    assert out[0] == "#%PAM-1.0"
    assert out[1] == pamsetup.MODULE_LINE
    assert "pam_fprintd.so" in out[2]


def test_strip_is_the_inverse():
    assert pamsetup._strip(pamsetup._insert_on_demand(HYPRLOCK)) == HYPRLOCK
    assert pamsetup._strip(pamsetup._insert_before_fprintd(FINGERPRINT)) == FINGERPRINT


def test_status_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(pamsetup, "MODULE", tmp_path / "pam_glance.so")
    monkeypatch.setattr(pamsetup, "HYPRLOCK", tmp_path / "hyprlock")
    monkeypatch.setattr(pamsetup, "SHELL_PASSWORD", tmp_path / "omarchy-lock-password")
    monkeypatch.setattr(pamsetup, "SHELL_FINGERPRINT", tmp_path / "omarchy-lock-fingerprint")
    assert pamsetup.status() == {
        "module": False, "hyprlock": False, "shellPassword": False, "shellFingerprint": False, "wired": False
    }
    (tmp_path / "hyprlock").write_text(pamsetup._insert_on_demand(HYPRLOCK))
    assert pamsetup.status()["hyprlock"] is True
    assert pamsetup.status()["wired"] is True
