"""The "it worked" moment: a success popup plus confetti across the whole screen."""

from __future__ import annotations

import html
import math
import random

from PyQt6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath, QPen, QPixmap, QTransform
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..icon import render_success
from ..util import human_size

PALETTE = ["#f94144", "#f3722c", "#f8961e", "#f9c74f", "#fde047", "#90be6d", "#43aa8b",
           "#22d3ee", "#277da1", "#577590", "#7c3aed", "#a78bfa", "#b5179e", "#fb7185", "#ffffff"]
RECT, DISC, STREAMER = range(3)


class _Piece:
    __slots__ = ("x", "y", "vx", "vy", "drag", "angle", "spin", "flip", "flip_speed", "wobble",
                 "wobble_speed", "wobble_amp", "shape", "w", "h", "front", "back", "path")


class ConfettiOverlay(QWidget):
    """A transparent, click-through window over the whole screen that rains confetti.

    Two cannons fire from the bottom corners while more confetti falls from the top edge,
    then everything flutters down and the window fades out and deletes itself.
    """

    RAIN_SECONDS = 1.6
    FADE_AFTER = 5.0
    FADE_SECONDS = 0.8
    FRAME_MS = 16

    def __init__(self, parent: QWidget | None = None):
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowTransparentForInput
                 | Qt.WindowType.NoDropShadowWindowHint)
        super().__init__(parent, flags)
        for attribute in (Qt.WidgetAttribute.WA_TranslucentBackground,
                          Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                          Qt.WidgetAttribute.WA_ShowWithoutActivating,
                          Qt.WidgetAttribute.WA_NoSystemBackground,
                          Qt.WidgetAttribute.WA_DeleteOnClose):
            self.setAttribute(attribute)
        screen = (parent.screen() if parent is not None else None) or QGuiApplication.primaryScreen()
        self.setGeometry(screen.geometry())
        self._w, self._h = float(self.width()), float(self.height())
        self._unit = max(0.75, min(self._w, self._h) / 1080.0)   # piece size
        self._gravity = 0.95 * self._h                            # px/s^2
        self._pieces: list[_Piece] = []
        self._rain_budget = 0.0
        self._opacity = 1.0
        self.frames = 0

        self._fire_cannons(count=int(110 * self._unit))
        self._second_volley_done = False  # a smaller "pop" right after the first one
        self._clock = QElapsedTimer()
        self._clock.start()
        self._last = 0
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._timer.start(self.FRAME_MS)

    # ---------------------------------------------------------------- spawning
    def _piece(self, x: float, y: float, vx: float, vy: float) -> _Piece:
        p = _Piece()
        p.x, p.y, p.vx, p.vy = x, y, vx, vy
        p.shape = random.choices((RECT, DISC, STREAMER), weights=(70, 15, 15))[0]
        size = random.uniform(7.0, 13.0) * self._unit
        p.drag = random.uniform(1.7, 2.6)
        p.path = None
        if p.shape == RECT:
            p.w, p.h = size, size * random.uniform(0.4, 0.7)
        elif p.shape == DISC:
            p.w = p.h = size * 0.65
        else:  # a curly ribbon
            length, amplitude = size * random.uniform(2.2, 3.4), size * 0.28
            phase = random.uniform(0, math.tau)
            path = QPainterPath(QPointF(-length / 2, math.sin(phase) * amplitude))
            for i in range(1, 13):
                t = i / 12
                path.lineTo(-length / 2 + t * length, math.sin(phase + t * math.tau * 1.3) * amplitude)
            p.path, p.w, p.h = path, length, max(2.0, size * 0.22)
            p.drag *= 1.35
        p.angle = random.uniform(0, 360)
        p.spin = random.uniform(-420, 420)
        p.flip = random.uniform(0, math.tau)
        p.flip_speed = random.uniform(4.0, 11.0)
        p.wobble = random.uniform(0, math.tau)
        p.wobble_speed = random.uniform(2.0, 5.0)
        p.wobble_amp = random.uniform(10, 35) * self._unit
        color = QColor(random.choice(PALETTE))
        p.front, p.back = color, color.darker(140)
        if p.shape == STREAMER:
            p.front, p.back = QPen(p.front, p.h), QPen(p.back, p.h)
            for pen in (p.front, p.back):
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        return p

    def _fire_cannons(self, count: int) -> None:
        for left in (True, False):
            x0 = -12.0 if left else self._w + 12.0
            for _ in range(count):
                angle = math.radians(random.uniform(38, 82))
                speed = random.uniform(1.8, 3.2) * self._h
                vx = math.cos(angle) * speed * (1 if left else -1)
                vy = -math.sin(angle) * speed
                self._pieces.append(self._piece(x0 + random.uniform(-15, 15),
                                                self._h + random.uniform(0, 20), vx, vy))

    def _rain(self, dt: float) -> None:
        self._rain_budget += dt * 115 * (self._w / 1920.0) * self._unit
        while self._rain_budget >= 1:
            self._rain_budget -= 1
            self._pieces.append(self._piece(random.uniform(0, self._w),
                                            random.uniform(-0.12 * self._h, -10),
                                            random.uniform(-0.08, 0.08) * self._h,
                                            random.uniform(0.05, 0.3) * self._h))

    # --------------------------------------------------------------- animation
    def _tick(self) -> None:
        now = self._clock.elapsed()
        dt = min((now - self._last) / 1000.0, 0.05)  # don't jump after a stall
        self._last = now
        seconds = now / 1000.0
        if seconds < self.RAIN_SECONDS:
            self._rain(dt)
        if not self._second_volley_done and seconds >= 0.35:
            self._second_volley_done = True
            self._fire_cannons(count=int(55 * self._unit))

        g = self._gravity
        alive = []
        for p in self._pieces:
            p.vx -= p.vx * p.drag * dt
            p.vy += (g - p.vy * p.drag) * dt
            p.wobble += p.wobble_speed * dt
            p.x += (p.vx + math.sin(p.wobble) * p.wobble_amp) * dt
            p.y += p.vy * dt
            p.angle += p.spin * dt
            p.flip += p.flip_speed * dt
            if (p.y < self._h + 60 or p.vy < 0) and -300 < p.x < self._w + 300:
                alive.append(p)
        self._pieces = alive

        if seconds > self.FADE_AFTER:
            self._opacity = max(0.0, 1.0 - (seconds - self.FADE_AFTER) / self.FADE_SECONDS)
        if self._opacity <= 0.0 or (not alive and seconds > self.RAIN_SECONDS):
            self._timer.stop()
            self.close()
            return
        self.update()

    def paintEvent(self, event) -> None:
        self.frames += 1
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(self._opacity)
        for p in self._pieces:
            turn = math.cos(p.flip)  # paper tumbling in 3D: squash it and show its back side
            transform = QTransform()
            transform.translate(p.x, p.y)
            transform.rotate(p.angle)
            transform.scale(1.0, turn if abs(turn) > 0.08 else math.copysign(0.08, turn))
            painter.setTransform(transform)
            side = p.front if turn >= 0 else p.back
            if p.shape == STREAMER:
                painter.setPen(side)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(p.path)
            else:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(side)
                rect = QRectF(-p.w / 2, -p.h / 2, p.w, p.h)
                if p.shape == RECT:
                    painter.drawRect(rect)
                else:
                    painter.drawEllipse(rect)
        painter.end()


def throw_confetti(window: QWidget) -> ConfettiOverlay:
    """Cover the screen `window` is on with confetti. Returns the (self-deleting) overlay."""
    overlay = ConfettiOverlay(window)
    overlay.show()
    overlay.raise_()
    return overlay


def celebrate(window: QWidget, package: str, worker, seconds: float, info: dict,
              confetti: bool = True) -> bool:
    """Success popup (plus confetti) after `worker` flashed a board.
    Returns True if the user asked to flash another board."""
    if confetti:
        throw_confetti(window)
    dialog = SuccessDialog(window, package, info.get("description") or info.get("name"),
                           worker.job.port, worker.written_images, human_size(worker.written_bytes),
                           seconds, worker.job.reset_after, info.get("mac"))
    return dialog.exec() == SuccessDialog.FLASH_ANOTHER


class SuccessDialog(QDialog):
    """Shown after a successful flash."""

    FLASH_ANOTHER = 2

    def __init__(self, parent: QWidget, package: str, chip: str | None, port: str,
                 images: int, size_text: str, seconds: float, restarted: bool, mac: str | None):
        super().__init__(parent)
        self.setWindowTitle("Flashing Complete")
        outer = QHBoxLayout(self)
        outer.setSpacing(18)

        icon = QLabel()
        ratio = self.devicePixelRatioF()
        pixmap = QPixmap.fromImage(render_success(int(72 * ratio)))
        pixmap.setDevicePixelRatio(ratio)
        icon.setPixmap(pixmap)
        icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        outer.addWidget(icon)

        column = QVBoxLayout()
        title = QLabel("Flashing complete!")
        font = title.font()
        font.setPointSize(font.pointSize() + 7)
        font.setBold(True)
        title.setFont(font)
        column.addWidget(title)

        board = f"the {html.escape(chip)}" if chip else "the board"
        summary = QLabel(f"<b>{html.escape(package)}</b> is now on {board} on {html.escape(port)}.")
        summary.setWordWrap(True)
        column.addWidget(summary)
        column.addSpacing(6)
        details = [f"Wrote {images} image{'s' if images != 1 else ''} ({size_text}) in {seconds:.1f} s."]
        details.append("The board has restarted and is running the new firmware." if restarted
                       else "Press the board's RESET button to start the new firmware.")
        for text in details:
            label = QLabel(text)
            label.setWordWrap(True)
            column.addWidget(label)
        if mac:
            mac_label = QLabel(f"MAC address: {mac}")
            mac_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            mac_label.setToolTip("Select to copy, e.g. to label the board")
            column.addWidget(mac_label)
        column.addSpacing(10)

        buttons = QDialogButtonBox()
        another = buttons.addButton("Flash Another Board", QDialogButtonBox.ButtonRole.ActionRole)
        another.setToolTip("Plug in the next board, then click to write the same package to it")
        another.clicked.connect(lambda: self.done(self.FLASH_ANOTHER))
        ok = buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        ok.setDefault(True)
        ok.setFocus()
        buttons.accepted.connect(self.accept)
        column.addWidget(buttons)
        outer.addLayout(column, 1)
        self.setMinimumWidth(460)
