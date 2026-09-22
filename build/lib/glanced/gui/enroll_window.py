"""The guided enrollment window: a mirrored camera disc inside a tick ring.

Eighty ticks tile the circle, divided evenly among the directional poses in
:data:`glanced.poses.SECTOR_POSES` — twenty each, across four 90-degree
sectors, as the poses stand. A sector's ticks lengthen and turn accent-blue
once that head direction has been captured; the centre pose owns no sector and
pulses every tick instead. When the last pose lands the ticks dissolve into a
solid ring and a checkmark draws. That choreography is ported from the macOS app — see
`reference/glance-macos/Onboarding/` in a source checkout — but the geometry
is redone in Qt painter space rather than translated from SwiftUI.

**No frame is written anywhere.** The preview exists only as pixels on the way
to the screen; what leaves this window is a list of embeddings, exactly as in
the headless path.

Threading: the camera and the landmarker are far too slow for the GUI thread,
so a worker owns both and hands back a finished QImage plus the session's
progress. Qt's queued connections make that the only synchronisation needed —
the worker never touches a widget, the widgets never touch the camera.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QObject,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QTransform,
)
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from .. import poses
from ..enroll import GuidedProgress, GuidedSession

# --- tokens ---------------------------------------------------------------

ACCENT = QColor(0x34, 0x99, 0xFF)
ACCENT_DEEP = QColor(0x1F, 0x6B, 0xD1)
SURFACE = QColor(0x1E, 0x1E, 0x1E)
TEXT_PRIMARY = QColor(0xFF, 0xFF, 0xFF)
TEXT_SECONDARY = QColor(0x94, 0x94, 0x94)
DANGER = QColor(0xFF, 0x45, 0x3A)

#: Ring geometry, in logical pixels. `TICK_COUNT` must stay a multiple of
#: `poses.SECTOR_COUNT` so the sectors tile the circle exactly — 80 divides by
#: 4, 5 and 8 alike, which covers every pose set worth having.
RING_DIAMETER = 260.0
TICK_COUNT = 80
TICKS_PER_SECTOR = TICK_COUNT // poses.SECTOR_COUNT
TICK_LENGTH_UNLIT = 15.0
TICK_LENGTH_LIT = 26.0
TICK_WIDTH = 3.0
#: How far inside the lit ticks' outer tips the completion ring sits, so it
#: reads as a hair smaller once the ticks are gone rather than landing exactly
#: where they were.
COMPLETION_RING_INSET = 9.0
COMPLETION_RING_WIDTH = 16.0
#: Per-tick delay across a sector, so a captured direction fills as a sweep
#: instead of the whole sector snapping at once. Wider sectors hold more
#: ticks, so this is a per-tick delay rather than a fixed total.
TICK_STAGGER_MS = 12

PREVIEW_INSET = 14.0


# --- the worker -----------------------------------------------------------


@dataclass
class Sample:
    embeddings: list
    pose_names: list


class CaptureWorker(QObject):
    """Owns the camera and the models; emits frames and progress."""

    frame = Signal(QImage)
    progressed = Signal(object)  # GuidedProgress
    pose_captured = Signal(str)
    finished = Signal(object)  # Sample
    failed = Signal(str)

    def __init__(self, *, device: str, samples_per_pose: int, timeout: float) -> None:
        super().__init__()
        self.device = device
        self.samples_per_pose = samples_per_pose
        self.timeout = timeout
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        import time

        from ..camera import Camera, CameraConfig
        from ..scan import FaceProcessor

        try:
            processor = FaceProcessor()
        except FileNotFoundError as error:
            self.failed.emit(str(error))
            return

        session = GuidedSession(samples_per_pose=self.samples_per_pose)
        started = time.monotonic()
        try:
            with Camera(CameraConfig(device=self.device)) as camera:
                for native in camera.frames():
                    if self._stop:
                        self.failed.emit("cancelled")
                        return
                    now = time.monotonic()
                    if now - started > self.timeout:
                        self.failed.emit(
                            f"only {len(session.captured_poses)} of {len(poses.POSES)} poses "
                            f"in {self.timeout:.0f}s"
                        )
                        return

                    observation = processor.process(native, now, want_embedding=True)
                    completed = session.offer(observation, now, frame_width=native.shape[1])

                    self.frame.emit(_disc_image(native, observation))
                    self.progressed.emit(session.progress)
                    if completed is not None:
                        self.pose_captured.emit(completed.name)
                    if session.complete:
                        break
        except Exception as error:  # camera open, V4L2, model load
            self.failed.emit(str(error))
            return
        finally:
            processor.close()

        self.finished.emit(Sample(session.embeddings, session.pose_names))


def _disc_image(native: np.ndarray, observation) -> QImage:
    """A square, mirrored crop around the face, ready to draw in the disc.

    Mirrored because a preview of your own face that moves the wrong way when
    you turn your head is disorienting — every selfie view is flipped — and
    because the poses are phrased from the user's point of view.
    """
    height, width = native.shape[:2]
    side = float(min(width, height))
    cx, cy = width / 2.0, height / 2.0

    if observation is not None:
        x, y, w, h = observation.native_bounding_box
        cx, cy = x + w / 2.0, y + h / 2.0
        side = float(min(max(w, h) * 2.0, width, height))

    half = side / 2.0
    cx = min(max(cx, half), width - half)
    cy = min(max(cy, half), height - half)
    x0, y0 = int(cx - half), int(cy - half)
    crop = np.ascontiguousarray(native[y0 : y0 + int(side), x0 : x0 + int(side)])

    image = QImage(crop.data, crop.shape[1], crop.shape[0], crop.strides[0], QImage.Format_RGB888)
    return image.copy().transformed(QTransform().scale(-1.0, 1.0))


# --- the ring -------------------------------------------------------------


class EnrollmentRing(QWidget):
    """The tick ring and the camera disc it surrounds."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        outer = RING_DIAMETER + 2 * TICK_LENGTH_LIT + 8
        self.setFixedSize(int(outer), int(outer))

        self._image: Optional[QImage] = None
        self._captured: set[str] = set()
        self._holding = False
        self._complete = False
        self._pulse = 0.0
        self._completion = 0.0
        self._checkmark = 0.0
        #: Per-sector fill progress, so a captured direction sweeps rather
        #: than snapping. Keyed by pose name, 0..1.
        self._fill: dict[str, float] = {}
        self._fill_timers: list[QTimer] = []

        self._pulse_animation = QPropertyAnimation(self, b"pulse", self)
        self._pulse_animation.setDuration(440)
        self._pulse_animation.setEasingCurve(QEasingCurve.OutCubic)

        self._completion_animation = QPropertyAnimation(self, b"completion", self)
        self._completion_animation.setDuration(450)
        self._completion_animation.setEasingCurve(QEasingCurve.InOutCubic)

        self._checkmark_animation = QPropertyAnimation(self, b"checkmark", self)
        self._checkmark_animation.setDuration(420)
        self._checkmark_animation.setEasingCurve(QEasingCurve.OutCubic)

    # Qt properties so QPropertyAnimation can drive them.
    def _get_pulse(self) -> float:
        return self._pulse

    def _set_pulse(self, value: float) -> None:
        self._pulse = value
        self.update()

    pulse = Property(float, _get_pulse, _set_pulse)

    def _get_completion(self) -> float:
        return self._completion

    def _set_completion(self, value: float) -> None:
        self._completion = value
        self.update()

    completion = Property(float, _get_completion, _set_completion)

    def _get_checkmark(self) -> float:
        return self._checkmark

    def _set_checkmark(self, value: float) -> None:
        self._checkmark = value
        self.update()

    checkmark = Property(float, _get_checkmark, _set_checkmark)

    # --- state in ---------------------------------------------------------

    def set_frame(self, image: QImage) -> None:
        self._image = image
        self.update()

    def set_holding(self, holding: bool) -> None:
        if holding != self._holding:
            self._holding = holding
            self.update()

    def capture(self, pose_name: str) -> None:
        """Light a sector — or pulse the whole ring, for the centre pose."""
        if pose_name == "centre":
            self._pulse_animation.stop()
            self._pulse_animation.setStartValue(1.0)
            self._pulse_animation.setEndValue(0.0)
            self._pulse_animation.start()
            return

        self._captured.add(pose_name)
        self._sweep_sector(pose_name)

    def _sweep_sector(self, pose_name: str) -> None:
        """Fill one sector's ticks in sequence rather than all at once."""
        for step in range(1, TICKS_PER_SECTOR + 1):
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(
                lambda name=pose_name, s=step: self._set_fill(name, s / TICKS_PER_SECTOR)
            )
            timer.start(step * TICK_STAGGER_MS)
            self._fill_timers.append(timer)

    def _set_fill(self, pose_name: str, value: float) -> None:
        self._fill[pose_name] = value
        self.update()

    def complete(self) -> None:
        self._complete = True
        self._completion_animation.stop()
        self._completion_animation.setStartValue(0.0)
        self._completion_animation.setEndValue(1.0)
        self._completion_animation.start()

        QTimer.singleShot(320, self._draw_checkmark)

    def _draw_checkmark(self) -> None:
        self._checkmark_animation.stop()
        self._checkmark_animation.setStartValue(0.0)
        self._checkmark_animation.setEndValue(1.0)
        self._checkmark_animation.start()

    # --- painting ---------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        centre = QPointF(self.width() / 2.0, self.height() / 2.0)

        self._paint_disc(painter, centre)
        if self._completion < 1.0:
            self._paint_ticks(painter, centre)
        if self._completion > 0.0:
            self._paint_completion_ring(painter, centre)
        if self._checkmark > 0.0:
            self._paint_checkmark(painter, centre)

    def _paint_disc(self, painter: QPainter, centre: QPointF) -> None:
        radius = RING_DIAMETER / 2.0 - PREVIEW_INSET
        rect = QRectF(centre.x() - radius, centre.y() - radius, radius * 2, radius * 2)

        painter.save()
        path = QPainterPath()
        path.addEllipse(rect)
        painter.setClipPath(path)
        if self._image is None:
            painter.fillRect(rect, QBrush(SURFACE))
        else:
            scaled = self._image.scaled(
                rect.size().toSize(),
                Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation,
            )
            painter.drawImage(rect.topLeft(), scaled)
        painter.restore()

    def _tick_sector(self, index: int) -> Optional[poses.Pose]:
        angle = index * (360.0 / TICK_COUNT)
        return poses.sector_pose(angle)

    def _tick_fill(self, index: int) -> float:
        """0..1 — how lit this tick is, blending sector fill and pulse."""
        if self._complete:
            return 1.0
        pose = self._tick_sector(index)
        sector = 0.0
        if pose is not None and pose.name in self._captured:
            progress = self._fill.get(pose.name, 0.0)
            # Ticks fill in order within the sector.
            position = (index % TICKS_PER_SECTOR + 1) / TICKS_PER_SECTOR
            sector = 1.0 if progress >= position else 0.0
        return max(sector, self._pulse)

    def _paint_ticks(self, painter: QPainter, centre: QPointF) -> None:
        radius = RING_DIAMETER / 2.0
        fade = 1.0 - self._completion
        idle = QColor(0xFF, 0xFF, 0xFF, int(120 * fade))
        active = QColor(0xFF, 0xFF, 0xFF, int(190 * fade))

        for index in range(TICK_COUNT):
            fill = self._tick_fill(index)
            length = TICK_LENGTH_UNLIT + (TICK_LENGTH_LIT - TICK_LENGTH_UNLIT) * fill
            if fill > 0.0:
                colour = QColor(ACCENT)
                colour.setAlphaF(fade)
            else:
                colour = active if self._holding else idle

            painter.save()
            painter.translate(centre)
            # 0 is up, clockwise — the compass the poses are named in.
            painter.rotate(index * (360.0 / TICK_COUNT))
            pen = QPen(colour, TICK_WIDTH, Qt.SolidLine, Qt.RoundCap)
            painter.setPen(pen)
            painter.drawLine(QPointF(0, -radius), QPointF(0, -(radius + length)))
            painter.restore()

    def _paint_completion_ring(self, painter: QPainter, centre: QPointF) -> None:
        # Outer edge just inside where the lit ticks' tips were.
        radius = RING_DIAMETER / 2.0 + TICK_LENGTH_LIT - COMPLETION_RING_INSET - COMPLETION_RING_WIDTH / 2.0
        scale = 0.92 + 0.08 * self._completion
        radius *= scale

        colour = QColor(ACCENT)
        colour.setAlphaF(self._completion)
        painter.setPen(QPen(colour, COMPLETION_RING_WIDTH))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(centre, radius, radius)

    def _paint_checkmark(self, painter: QPainter, centre: QPointF) -> None:
        scale = RING_DIAMETER / 260.0
        points = [
            QPointF(centre.x() - 34 * scale, centre.y() + 2 * scale),
            QPointF(centre.x() - 10 * scale, centre.y() + 26 * scale),
            QPointF(centre.x() + 36 * scale, centre.y() - 24 * scale),
        ]
        # Draw the stroke progressively along its two segments.
        first = math.hypot(points[1].x() - points[0].x(), points[1].y() - points[0].y())
        second = math.hypot(points[2].x() - points[1].x(), points[2].y() - points[1].y())
        travelled = self._checkmark * (first + second)

        path = QPainterPath(points[0])
        if travelled <= first:
            t = travelled / first
            path.lineTo(
                points[0].x() + (points[1].x() - points[0].x()) * t,
                points[0].y() + (points[1].y() - points[0].y()) * t,
            )
        else:
            path.lineTo(points[1])
            t = (travelled - first) / second
            path.lineTo(
                points[1].x() + (points[2].x() - points[1].x()) * t,
                points[1].y() + (points[2].y() - points[1].y()) * t,
            )

        painter.setPen(QPen(TEXT_PRIMARY, 9 * scale, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)


# --- the window -----------------------------------------------------------


class EnrollmentWindow(QWidget):
    def __init__(
        self,
        *,
        name: str,
        device: str,
        samples_per_pose: int,
        timeout: float,
        debug: bool = False,
    ) -> None:
        super().__init__()
        self.setWindowTitle(f"Glance — enrolling {name}")
        self.setStyleSheet(f"background: {SURFACE.name()};")
        self.setMinimumWidth(520)

        self.result: Optional[Sample] = None
        self.error: Optional[str] = None
        self._debug = debug

        self.ring = EnrollmentRing(self)
        self.instruction = QLabel(poses.POSES[0].instruction, self)
        self.instruction.setAlignment(Qt.AlignCenter)
        self.instruction.setFont(QFont(self.font().family(), 15, QFont.Medium))
        self.instruction.setStyleSheet(f"color: {TEXT_PRIMARY.name()};")

        self.detail = QLabel("", self)
        self.detail.setAlignment(Qt.AlignCenter)
        self.detail.setFont(QFont(self.font().family(), 11))
        self.detail.setStyleSheet(f"color: {TEXT_SECONDARY.name()};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 36, 40, 32)
        layout.setSpacing(22)
        layout.addWidget(self.ring, alignment=Qt.AlignCenter)
        layout.addWidget(self.instruction)
        layout.addWidget(self.detail)

        self._thread = QThread(self)
        self._worker = CaptureWorker(
            device=device, samples_per_pose=samples_per_pose, timeout=timeout
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.frame.connect(self.ring.set_frame)
        self._worker.progressed.connect(self._on_progress)
        self._worker.pose_captured.connect(self.ring.capture)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._thread.start()

    def _on_progress(self, progress: GuidedProgress) -> None:
        self.ring.set_holding(progress.holding)

        if progress.pose is not None:
            if not progress.face_detected:
                self.instruction.setText("Looking for your face")
            elif progress.too_far:
                self.instruction.setText("Move a little closer")
            else:
                self.instruction.setText(progress.pose.instruction)

        step = min(progress.pose_index + 1, progress.pose_count)
        detail = f"{step} of {progress.pose_count}"
        if progress.widened:
            detail += " — hold whatever angle you can, this one is being lenient"
        if self._debug:
            yaw = "—" if progress.yaw is None else f"{math.degrees(progress.yaw):+.0f}°"
            pitch = "—" if progress.pitch is None else f"{math.degrees(progress.pitch):+.0f}°"
            detail += f"   yaw {yaw}  pitch {pitch}"
        self.detail.setText(detail)

    def _on_finished(self, sample: Sample) -> None:
        self.result = sample
        self.instruction.setText("Done")
        self.detail.setText(f"{len(sample.embeddings)} samples across {len(poses.POSES)} poses")
        self.ring.complete()
        self._thread.quit()
        QTimer.singleShot(1400, self.close)

    def _on_failed(self, message: str) -> None:
        self.error = message
        self.instruction.setText("Enrollment stopped")
        self.instruction.setStyleSheet(f"color: {DANGER.name()};")
        self.detail.setText(message)
        self._thread.quit()
        QTimer.singleShot(2600, self.close)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._worker.stop()
        self._thread.quit()
        self._thread.wait(2000)
        super().closeEvent(event)


def run(
    *,
    name: str,
    device: str = "/dev/video0",
    samples_per_pose: int = 2,
    timeout: float = 180.0,
    debug: bool = False,
) -> tuple[list, list]:
    """Open the window, run the sweep, and return `(embeddings, pose_names)`.

    Raises on failure, so the CLI's error path is the same whether the window
    or the terminal drove the capture.
    """
    app = QApplication.instance() or QApplication([])
    window = EnrollmentWindow(
        name=name,
        device=device,
        samples_per_pose=samples_per_pose,
        timeout=timeout,
        debug=debug,
    )
    window.show()
    window.raise_()
    window.activateWindow()
    app.exec()

    if window.result is None:
        raise RuntimeError(window.error or "enrollment window closed before it finished")
    return window.result.embeddings, window.result.pose_names
