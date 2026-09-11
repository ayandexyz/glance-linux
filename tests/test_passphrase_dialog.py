"""The passphrase dialog's validation, driven offscreen.

Qt is an optional extra, so the module skips without it rather than failing a
run that only ever wanted the daemon.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6", reason="the passphrase dialog needs the 'gui' extra")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402 - after the skip

from glanced.gui import passphrase  # noqa: E402 - after the skip


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_a_fresh_enrollment_asks_twice_and_they_must_match(app):
    dialog = passphrase.PassphraseDialog(confirm=True)
    assert not dialog.second.isHidden()

    dialog.first.setText("hunter2")
    dialog.second.setText("hunter3")
    dialog.try_accept()
    assert dialog.value is None
    assert "do not match" in dialog.problem.text()
    assert dialog.second.text() == "", "the mistyped copy is cleared for a retry"

    dialog.second.setText("hunter2")
    dialog.try_accept()
    assert dialog.value == "hunter2"


def test_an_existing_enrollment_asks_once(app):
    dialog = passphrase.PassphraseDialog(confirm=False)
    assert dialog.second.isHidden()

    dialog.first.setText("hunter2")
    dialog.try_accept()
    assert dialog.value == "hunter2"


def test_an_empty_passphrase_is_refused(app):
    dialog = passphrase.PassphraseDialog(confirm=False)
    dialog.try_accept()
    assert dialog.value is None
    assert "empty" in dialog.problem.text()


def test_a_wrong_passphrase_is_shown_on_the_retry(app):
    dialog = passphrase.PassphraseDialog(confirm=False, error="wrong passphrase")
    assert not dialog.problem.isHidden()
    assert dialog.problem.text() == "wrong passphrase"
