"""`glancectl status` reports whether the enrollment window can open.

The Omarchy plugin reads this to decide between the window and a terminal,
so an install without the `gui` extra gets a button that works rather than
one that raises ImportError.
"""

from glanced import gui


def test_available_is_true_when_pyside_can_be_found(monkeypatch):
    import importlib.util

    asked = []

    def found(name):
        asked.append(name)
        return object()

    monkeypatch.setattr(importlib.util, "find_spec", found)
    assert gui.available() is True
    assert asked == ["PySide6"]


def test_available_is_false_when_pyside_is_absent(monkeypatch):
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert gui.available() is False


def test_available_survives_a_broken_meta_path(monkeypatch):
    """A find_spec that raises must not take `status` down with it."""
    import importlib.util

    def boom(name):
        raise ValueError("bad meta path")

    monkeypatch.setattr(importlib.util, "find_spec", boom)
    assert gui.available() is False
