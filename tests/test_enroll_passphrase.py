"""Where `glancectl enroll` gets its passphrase from.

The Omarchy widget's Enroll button runs `glancectl enroll --gui` with no
terminal attached. getpass would fall back to stdin, find it closed, and raise
EOFError before the window opened — which is exactly the fresh-install path.
So a `--gui` run without a terminal prompts in a window instead, and the
terminal prompt stays what it was for everyone else.
"""

from __future__ import annotations

import argparse
import io
import sys

import pytest

from glanced import cli


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def _args(**overrides) -> argparse.Namespace:
    base = {"gui": False, "passphrase_stdin": False}
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def window(monkeypatch):
    """Stand in for the Qt dialog and record how it was asked."""
    calls = []

    def ask_passphrase(*, confirm, error=""):
        calls.append({"confirm": confirm, "error": error})
        return window.answer

    import glanced.gui

    monkeypatch.setattr(glanced.gui, "ask_passphrase", ask_passphrase)
    window.answer = "hunter2"
    window.calls = calls
    return window


def test_gui_without_a_terminal_prompts_in_a_window(monkeypatch, window):
    monkeypatch.setattr(sys, "stdin", _Pipe())
    monkeypatch.setattr(cli.getpass, "getpass", lambda *_: pytest.fail("getpass must not run"))

    assert cli._read_passphrase(_args(gui=True), confirm=True) == "hunter2"
    assert window.calls == [{"confirm": True, "error": ""}]


def test_gui_from_a_terminal_keeps_the_terminal_prompt(monkeypatch, window):
    monkeypatch.setattr(sys, "stdin", _Tty())
    prompts = []
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: prompts.append(prompt) or "typed")

    assert cli._read_passphrase(_args(gui=True), confirm=False) == "typed"
    assert window.calls == []
    assert prompts == ["Passphrase: "]


def test_headless_enroll_without_a_terminal_never_opens_a_window(monkeypatch, window):
    """No --gui means no Qt, whatever stdin is: the daemon host may not have it."""
    monkeypatch.setattr(sys, "stdin", _Pipe())
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "typed")

    assert cli._read_passphrase(_args(gui=False)) == "typed"
    assert window.calls == []


def test_cancelling_the_window_exits_cleanly(monkeypatch, window):
    monkeypatch.setattr(sys, "stdin", _Pipe())
    window.answer = None

    with pytest.raises(SystemExit) as stop:
        cli._read_passphrase(_args(gui=True))
    assert "cancelled" in str(stop.value)


def test_stdin_flag_wins_over_the_window(monkeypatch, window):
    monkeypatch.setattr(sys, "stdin", _Pipe("from-stdin\n"))

    assert cli._read_passphrase(_args(gui=True, passphrase_stdin=True)) == "from-stdin"
    assert window.calls == []


def test_a_wrong_passphrase_is_asked_again_in_the_window(monkeypatch, window, tmp_path):
    """A terminal prints "wrong passphrase" and exits; a window has to say it
    and ask again, or the user is left staring at nothing."""
    from glanced import paths, store as store_module

    store_path = tmp_path / "store"
    store_module.save(store_module.EnrollmentStore(), "right", store_path)
    monkeypatch.setattr(paths, "STORE_PATH", store_path)
    monkeypatch.setattr(sys, "stdin", _Pipe())

    answers = iter(["wrong", "right"])
    seen = []

    def ask_passphrase(*, confirm, error=""):
        seen.append(error)
        return next(answers)

    import glanced.gui

    monkeypatch.setattr(glanced.gui, "ask_passphrase", ask_passphrase)

    # Stop after the store loads: the sweep itself is not under test.
    class _Stop(Exception):
        pass

    def run_enrollment(**_):
        raise _Stop

    monkeypatch.setattr(glanced.gui, "run_enrollment", run_enrollment)

    with pytest.raises(_Stop):
        cli._enroll(_args(gui=True, name="x", device="/dev/video0", samples_per_pose=1, debug=False,
                          remember=False, no_guide=False, captures=1))
    assert seen == ["", "wrong passphrase"]


def test_three_wrong_passphrases_give_up(monkeypatch, window, tmp_path, capsys):
    from glanced import paths, store as store_module

    store_path = tmp_path / "store"
    store_module.save(store_module.EnrollmentStore(), "right", store_path)
    monkeypatch.setattr(paths, "STORE_PATH", store_path)
    monkeypatch.setattr(sys, "stdin", _Pipe())
    window.answer = "wrong"

    code = cli._enroll(_args(gui=True, name="x", device="/dev/video0", samples_per_pose=1, debug=False,
                             remember=False, no_guide=False, captures=1))
    assert code == 1
    assert len(window.calls) == 3
    assert "wrong passphrase" in capsys.readouterr().err
