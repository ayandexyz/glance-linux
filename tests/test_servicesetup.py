"""The user service written by `glancectl install-service`.

No real systemctl: the calls are recorded, and the unit lands in a tmp_path
config dir.
"""

from pathlib import Path

import pytest

from glanced import servicesetup

ROOT = Path(__file__).resolve().parent.parent
PACKAGED_UNIT = ROOT / "packaging" / "systemd" / "glanced.service"


@pytest.fixture(autouse=True)
def models_on_disk(monkeypatch):
    """Default to "models already here" so only the fetch tests exercise it.

    Without this, whether a test downloads 16MB depends on whether the
    checkout happens to have models/ populated.
    """
    monkeypatch.setattr(servicesetup, "models_present", lambda: True)


@pytest.fixture
def systemctl(monkeypatch):
    calls: list[list[str]] = []

    def run(cmd, check=False):
        calls.append(list(cmd))

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr(servicesetup.subprocess, "run", run)
    return calls


def test_generated_unit_matches_the_packaged_one():
    """install.sh rewrites ExecStart in the packaged unit; we generate the
    whole file. Every other line has to be identical or the two installs
    would drift apart."""
    packaged = [
        line for line in PACKAGED_UNIT.read_text().splitlines()
        # The packaged unit carries a comment saying install.sh rewrites this.
        if not line.startswith("ExecStart=") and not line.startswith("# packaging/install.sh")
    ]
    generated = [
        line for line in servicesetup.render_unit("/opt/glance/bin/glancectl").splitlines()
        if not line.startswith("ExecStart=")
    ]
    assert generated == packaged


def test_render_unit_places_the_binary_and_mode():
    unit = servicesetup.render_unit("/opt/glance/bin/glancectl", mode="heavy")
    assert "ExecStart=/opt/glance/bin/glancectl daemon --mode heavy" in unit


def test_setup_writes_enables_and_reports(tmp_path, monkeypatch, systemctl, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    binary = tmp_path / "bin" / "glancectl"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(servicesetup, "glancectl_path", lambda: binary)

    assert servicesetup.setup() == 0

    unit = servicesetup.unit_path()
    assert unit.exists()
    assert f"ExecStart={binary} daemon --mode light" in unit.read_text()
    assert ["systemctl", "--user", "daemon-reload"] in systemctl
    assert ["systemctl", "--user", "enable", "--now", "glanced.service"] in systemctl


def test_no_enable_leaves_the_service_stopped(tmp_path, monkeypatch, systemctl):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    binary = tmp_path / "glancectl"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(servicesetup, "glancectl_path", lambda: binary)

    assert servicesetup.setup(enable=False) == 0
    assert servicesetup.unit_path().exists()
    assert not any("enable" in call for call in systemctl)


def test_remove_deletes_the_unit(tmp_path, monkeypatch, systemctl):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit = servicesetup.unit_path()
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n")

    assert servicesetup.setup(remove=True) == 0
    assert not unit.exists()
    assert ["systemctl", "--user", "disable", "--now", "glanced.service"] in systemctl


def test_remove_is_quiet_when_nothing_is_installed(tmp_path, monkeypatch, systemctl):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert servicesetup.setup(remove=True) == 0
    assert systemctl == []


def test_setup_fetches_the_models_when_they_are_missing(tmp_path, monkeypatch, systemctl):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    binary = tmp_path / "glancectl"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(servicesetup, "glancectl_path", lambda: binary)
    monkeypatch.setattr(servicesetup, "models_present", lambda: False)

    fetched: list[bool] = []
    from glanced import models

    monkeypatch.setattr(models, "fetch", lambda *a, **k: fetched.append(True))

    assert servicesetup.setup(enable=False) == 0
    assert fetched == [True], "a service that starts without models only fails later"


def test_setup_leaves_existing_models_alone(tmp_path, monkeypatch, systemctl):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    binary = tmp_path / "glancectl"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(servicesetup, "glancectl_path", lambda: binary)
    monkeypatch.setattr(servicesetup, "models_present", lambda: True)

    from glanced import models

    def boom(*a, **k):
        raise AssertionError("must not re-download models that are already here")

    monkeypatch.setattr(models, "fetch", boom)
    assert servicesetup.setup(enable=False) == 0


def test_no_fetch_skips_the_download(tmp_path, monkeypatch, systemctl):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    binary = tmp_path / "glancectl"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(servicesetup, "glancectl_path", lambda: binary)
    monkeypatch.setattr(servicesetup, "models_present", lambda: False)

    from glanced import models

    def boom(*a, **k):
        raise AssertionError("--no-fetch must not download anything")

    monkeypatch.setattr(models, "fetch", boom)
    assert servicesetup.setup(enable=False, fetch=False) == 0


def test_missing_binary_is_an_error(tmp_path, monkeypatch, systemctl, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(servicesetup, "glancectl_path", lambda: tmp_path / "nope")

    assert servicesetup.setup() == 1
    assert "cannot find" in capsys.readouterr().err


# --- the ~/.local/bin link ------------------------------------------------------


def _venv_binary(tmp_path):
    binary = tmp_path / "venv" / "bin" / "glancectl"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return binary


def test_setup_links_glancectl_onto_the_path(tmp_path, monkeypatch, systemctl, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_BIN_HOME", str(tmp_path / "localbin"))
    binary = _venv_binary(tmp_path)
    monkeypatch.setattr(servicesetup, "glancectl_path", lambda: binary)

    assert servicesetup.setup() == 0
    link = tmp_path / "localbin" / "glancectl"
    assert link.is_symlink() and link.resolve() == binary.resolve()
    assert "not on your PATH" in capsys.readouterr().out

    # Idempotent, and quiet about PATH once it is there.
    monkeypatch.setenv("PATH", str(tmp_path / "localbin"))
    assert servicesetup.setup() == 0
    assert "not on your PATH" not in capsys.readouterr().out

    assert servicesetup.setup(remove=True) == 0
    assert not link.exists() and not link.is_symlink()


def test_the_link_never_replaces_someone_elses_glancectl(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_BIN_HOME", str(tmp_path / "localbin"))
    ours = _venv_binary(tmp_path)
    pipx = tmp_path / "pipx" / "glancectl"
    pipx.parent.mkdir()
    pipx.write_text("#!/bin/sh\n")
    link = tmp_path / "localbin" / "glancectl"
    link.parent.mkdir()
    link.symlink_to(pipx)

    assert servicesetup.link_on_path(ours) is None
    assert link.resolve() == pipx.resolve()
    servicesetup.unlink_from_path(ours)
    assert link.is_symlink(), "remove must not take away a link it did not make"

    link.unlink()
    link.write_text("#!/bin/sh\n# a wrapper someone wrote\n")
    assert servicesetup.link_on_path(ours) is None
    assert "wrapper" in link.read_text()


def test_a_dangling_link_from_an_old_venv_is_replaced(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_BIN_HOME", str(tmp_path / "localbin"))
    ours = _venv_binary(tmp_path)
    link = tmp_path / "localbin" / "glancectl"
    link.parent.mkdir()
    link.symlink_to(tmp_path / "gone" / "glancectl")

    assert servicesetup.link_on_path(ours) == link
    assert link.resolve() == ours.resolve()
