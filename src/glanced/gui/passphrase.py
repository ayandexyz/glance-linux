"""The passphrase prompt, for an enrollment started without a terminal.

`glancectl enroll --gui` is what the Omarchy widget's Enroll button runs, and
a button has no terminal to type into. The passphrase still has to come from
somewhere that is not argv — so it comes from this dialog, drawn in the same
palette as the sweep window that follows it.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from .enroll_window import ACCENT, DANGER, SURFACE, TEXT_PRIMARY, TEXT_SECONDARY

FRESH_TITLE = "Choose a passphrase"
FRESH_DETAIL = "It encrypts your face data at rest. You will not be asked for it again if the daemon remembers it."
EXISTING_TITLE = "Enter your passphrase"
EXISTING_DETAIL = "The one you chose when you first enrolled."

_APP = None


class PassphraseDialog(QDialog):
    """One or two password fields; accepts only a non-empty, matching pair."""

    def __init__(self, *, confirm: bool, error: str = "") -> None:
        super().__init__()
        self.setWindowTitle("Glance — passphrase")
        self.setStyleSheet(f"background: {SURFACE.name()}; color: {TEXT_PRIMARY.name()};")
        self.setMinimumWidth(420)
        self.setModal(True)

        self.value: Optional[str] = None
        self._confirm = confirm

        title = QLabel(FRESH_TITLE if confirm else EXISTING_TITLE, self)
        title.setFont(QFont(self.font().family(), 15, QFont.Medium))

        detail = QLabel(FRESH_DETAIL if confirm else EXISTING_DETAIL, self)
        detail.setWordWrap(True)
        detail.setFont(QFont(self.font().family(), 11))
        detail.setStyleSheet(f"color: {TEXT_SECONDARY.name()};")

        field_style = (
            f"QLineEdit {{ background: #2a2a2a; color: {TEXT_PRIMARY.name()}; "
            f"border: 1px solid #3a3a3a; border-radius: 6px; padding: 8px; }}"
            f"QLineEdit:focus {{ border-color: {ACCENT.name()}; }}"
        )
        self.first = QLineEdit(self)
        self.first.setEchoMode(QLineEdit.Password)
        self.first.setPlaceholderText("Passphrase")
        self.first.setStyleSheet(field_style)

        self.second = QLineEdit(self)
        self.second.setEchoMode(QLineEdit.Password)
        self.second.setPlaceholderText("Confirm passphrase")
        self.second.setStyleSheet(field_style)
        self.second.setVisible(confirm)

        self.problem = QLabel(error, self)
        self.problem.setWordWrap(True)
        self.problem.setStyleSheet(f"color: {DANGER.name()};")
        self.problem.setVisible(bool(error))

        cancel = QPushButton("Cancel", self)
        cancel.setStyleSheet(
            f"QPushButton {{ color: {TEXT_SECONDARY.name()}; background: transparent; "
            f"border: 1px solid #3a3a3a; border-radius: 6px; padding: 8px 16px; }}"
        )
        cancel.clicked.connect(self.reject)

        self.accept_button = QPushButton("Continue", self)
        self.accept_button.setDefault(True)
        self.accept_button.setStyleSheet(
            f"QPushButton {{ color: {TEXT_PRIMARY.name()}; background: {ACCENT.name()}; "
            f"border: none; border-radius: 6px; padding: 8px 18px; }}"
        )
        self.accept_button.clicked.connect(self.try_accept)
        self.first.returnPressed.connect(self.try_accept)
        self.second.returnPressed.connect(self.try_accept)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self.accept_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 24)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(detail)
        layout.addSpacing(6)
        layout.addWidget(self.first)
        layout.addWidget(self.second)
        layout.addWidget(self.problem)
        layout.addSpacing(6)
        layout.addLayout(buttons)

        self.first.setFocus()

    def try_accept(self) -> None:
        """Validate in place: the dialog stays open until the input is usable."""
        first = self.first.text()
        if not first:
            self._complain("The passphrase cannot be empty.")
            return
        if self._confirm and self.second.text() != first:
            self._complain("The two passphrases do not match.")
            self.second.clear()
            self.second.setFocus()
            return
        self.value = first
        self.accept()

    def _complain(self, message: str) -> None:
        self.problem.setText(message)
        self.problem.setVisible(True)


def ask(*, confirm: bool, error: str = "") -> Optional[str]:
    """Show the dialog; return the passphrase, or None if the user cancelled.

    `error` is shown above the buttons — the way the CLI says "wrong
    passphrase" when there is no terminal to say it in.
    """
    # Held at module level on purpose: the sweep window that follows reuses
    # the application instead of starting a second one.
    global _APP
    _APP = QApplication.instance() or QApplication([])
    dialog = PassphraseDialog(confirm=confirm, error=error)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    dialog.exec()
    return dialog.value
