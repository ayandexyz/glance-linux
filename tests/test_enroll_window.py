"""The enrollment ring's geometry, without opening a window.

Qt is an optional extra, so the whole module skips when it is absent rather
than failing a test run on a machine that only ever wanted the daemon.
"""

from __future__ import annotations

import pytest

from glanced import poses

pytest.importorskip("PySide6", reason="the enrollment window needs the 'gui' extra")

from glanced.gui import enroll_window  # noqa: E402 - after the skip


def test_ticks_divide_evenly_among_the_sectors():
    """A remainder would leave one direction with a short sector, and the
    mismatch would show as a gap in the ring rather than as an error."""
    assert enroll_window.TICK_COUNT % poses.SECTOR_COUNT == 0
    assert enroll_window.TICKS_PER_SECTOR == enroll_window.TICK_COUNT // poses.SECTOR_COUNT


def test_every_tick_belongs_to_exactly_one_sector():
    owners = [poses.sector_pose(i * 360.0 / enroll_window.TICK_COUNT)
              for i in range(enroll_window.TICK_COUNT)]
    assert all(owner is not None for owner in owners)
    counts = {pose.name: owners.count(pose) for pose in poses.SECTOR_POSES}
    assert set(counts.values()) == {enroll_window.TICKS_PER_SECTOR}


def test_the_completion_ring_sits_inside_the_lit_ticks():
    """It reads as a hair smaller once the ticks vanish, rather than landing
    exactly where their tips were."""
    tips = enroll_window.RING_DIAMETER / 2 + enroll_window.TICK_LENGTH_LIT
    outer = (enroll_window.RING_DIAMETER / 2 + enroll_window.TICK_LENGTH_LIT
             - enroll_window.COMPLETION_RING_INSET)
    assert outer < tips
