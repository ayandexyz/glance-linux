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
    diff_sets = list((source / "diffs").iterdir())
    assert diff_sets, "no diff sets to apply"
    for diff_set in diff_sets:
        assert locksetup.MARKER in (diff_set / "Service.qml").read_text(), (
            f"{diff_set} is missing the marker status() greps for"
        )
    hook = source.parents[1] / "packaging" / "hooks" / locksetup.HOOK_NAME
    assert locksetup.MARKER in hook.read_text(), "the hook must grep for the same marker status() uses"


def test_setup_refuses_without_a_lock_plugin(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OMARCHY_PATH", str(tmp_path))
    assert locksetup.setup() == 1
    assert "nothing to patch" in capsys.readouterr().out


def _fake_proc(tmp_path, processes):
    proc = tmp_path / "proc"
    for pid, (comm, environ, *cmdline) in processes.items():
        entry = proc / str(pid)
        entry.mkdir(parents=True)
        (entry / "comm").write_text(comm + "\n")
        (entry / "environ").write_bytes(b"\0".join(e.encode() for e in environ) + b"\0")
        args = cmdline[0] if cmdline else [comm]
        (entry / "cmdline").write_bytes(b"\0".join(a.encode() for a in args) + b"\0")
    (proc / "self").mkdir(parents=True, exist_ok=True)
    return proc


def test_the_running_shells_omarchy_path_is_found(tmp_path):
    proc = _fake_proc(tmp_path, {
        12: ("bash", ["OMARCHY_PATH=/wrong"]),
        40: ("quickshell", ["HOME=/home/me", "OMARCHY_PATH=/home/me/.local/share/omarchy"]),
    })
    assert locksetup._running_shell_omarchy_path(proc) == locksetup.Path("/home/me/.local/share/omarchy")


def test_a_shell_started_from_a_checkout_without_the_variable(tmp_path):
    checkout = tmp_path / "home" / ".local" / "share" / "omarchy"
    (checkout / "shell").mkdir(parents=True)
    (checkout / "shell" / "shell.qml").write_text("ShellRoot {}\n")
    proc = _fake_proc(tmp_path, {
        40: ("quickshell", ["HOME=/home/me"], ["quickshell", "-p", str(checkout / "shell" / "shell.qml")]),
    })
    assert locksetup._running_shell_omarchy_path(proc) == checkout


def test_a_shell_without_omarchy_path_means_the_package(tmp_path):
    proc = _fake_proc(tmp_path, {40: ("quickshell", ["HOME=/home/me"])})
    assert locksetup._running_shell_omarchy_path(proc) is None
    assert locksetup._running_shell_omarchy_path(tmp_path / "no-proc") is None


def test_omarchy_path_prefers_the_environment_then_the_shell(monkeypatch):
    monkeypatch.setattr(locksetup, "_running_shell_omarchy_path", lambda: locksetup.Path("/from/shell"))
    monkeypatch.setenv("OMARCHY_PATH", "/from/env")
    assert locksetup.omarchy_path() == locksetup.Path("/from/env")
    monkeypatch.delenv("OMARCHY_PATH")
    assert locksetup.lock_dir() == locksetup.Path("/from/shell/shell/plugins/lock")
    monkeypatch.setattr(locksetup, "_running_shell_omarchy_path", lambda: None)
    assert locksetup.omarchy_path() == locksetup.Path("/usr/share/omarchy")


def test_sudo_carries_omarchy_path_to_apply_sh(tmp_path, monkeypatch):
    """sudo resets the environment: without this a checkout shell gets the
    package copy under /usr/share/omarchy patched instead."""
    checkout = tmp_path / "checkout"
    lock = checkout / "shell" / "plugins" / "lock"
    lock.mkdir(parents=True)
    (lock / "Service.qml").write_text("Singleton {}\n")
    monkeypatch.setenv("OMARCHY_PATH", str(checkout))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    calls = []
    monkeypatch.setattr(locksetup.subprocess, "run", lambda cmd, check=False: calls.append(cmd))
    monkeypatch.setattr(locksetup.shutil, "which", lambda name: None)

    assert locksetup.setup(log=lambda *_: None) == 0
    assert calls[0][:3] == ["sudo", "env", f"OMARCHY_PATH={checkout}"]
    assert calls[0][-1].endswith("apply.sh")
