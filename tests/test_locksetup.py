"""The lock screen indicator's status: what the socket reports and what the
post-update hook keys on. No sudo, no real /usr/share/omarchy."""

from glanced import locksetup


def test_status_tracks_marker_and_hook(tmp_path, monkeypatch):
    lock = tmp_path / "omarchy" / "shell" / "plugins" / "lock"
    lock.mkdir(parents=True)
    monkeypatch.setenv("OMARCHY_PATH", str(tmp_path / "omarchy"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    assert locksetup.status() == {"available": False, "patched": False, "hook": False}

    (lock / "Service.qml").write_text("Singleton { property bool stock: true }\n")
    assert locksetup.status() == {"available": True, "patched": False, "hook": False}

    (lock / "Service.qml").write_text('readonly property string faceMessagePrefix: "Glance:"\n')
    hook = locksetup.user_hook_path()
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/bash\n")
    assert locksetup.status() == {"available": True, "patched": True, "hook": True}


def test_the_shipped_patch_carries_the_marker():
    source = locksetup._repo_patch_dir()
    assert source is not None
    assert locksetup.MARKER in (source / "Service.qml").read_text()
    hook = source.parents[1] / "packaging" / "hooks" / locksetup.HOOK_NAME
    assert locksetup.MARKER in hook.read_text(), "the hook must grep for the same marker status() uses"


def test_setup_refuses_without_a_lock_plugin(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OMARCHY_PATH", str(tmp_path))
    assert locksetup.setup() == 1
    assert "nothing to patch" in capsys.readouterr().out
