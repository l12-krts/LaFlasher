"""App icon and other artwork, drawn in code so there are no image assets to ship."""

from __future__ import annotations

import random
import struct
from pathlib import Path

from PyQt6.QtCore import QBuffer, QIODevice, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QImage, QLinearGradient, QPainter, QPen, QPixmap, QPolygonF

_BOLT = [(0.60, 0.08), (0.24, 0.56), (0.47, 0.56), (0.39, 0.92), (0.77, 0.41), (0.54, 0.41), (0.63, 0.08)]
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
# Inno Setup picks the wizard image matching the screen's DPI scaling.
WIZARD_SCALES = (100, 150, 200)
_CONFETTI = ["#f94144", "#f8961e", "#fde047", "#90be6d", "#22d3ee", "#a78bfa", "#fb7185", "#ffffff"]


def render(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#5b21b6"))
    radius = size * 0.2
    p.drawRoundedRect(QRectF(0, 0, size, size).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    p.setBrush(QColor("#fde047"))
    p.drawPolygon(QPolygonF([QPointF(x * size, y * size) for x, y in _BOLT]))
    p.end()
    return image


def render_success(size: int) -> QImage:
    """Green circle with a white tick, for the success popup."""
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#16a34a"))
    p.drawEllipse(QRectF(0.5, 0.5, size - 1, size - 1))
    pen = QPen(QColor("#ffffff"), size * 0.1)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.drawPolyline(QPolygonF([QPointF(0.28 * size, 0.53 * size), QPointF(0.44 * size, 0.68 * size),
                              QPointF(0.73 * size, 0.36 * size)]))
    p.end()
    return image


def _wizard_large(scale: int) -> QImage:
    """Left-hand banner of the installer's welcome/finish pages (164x314 at 100%)."""
    w, h = round(164 * scale / 100), round(314 * scale / 100)
    image = QImage(w, h, QImage.Format.Format_RGB32)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    gradient = QLinearGradient(0, 0, 0, h)
    gradient.setColorAt(0.0, QColor("#6d28d9"))
    gradient.setColorAt(1.0, QColor("#2e1065"))
    p.fillRect(image.rect(), gradient)
    rng = random.Random(12)  # the same confetti on every build
    p.setPen(Qt.PenStyle.NoPen)
    for _ in range(48):
        p.save()
        p.translate(rng.uniform(0, w), rng.uniform(0, h))
        p.rotate(rng.uniform(0, 360))
        size = rng.uniform(4.0, 9.0) * scale / 100
        p.setBrush(QColor(rng.choice(_CONFETTI)))
        p.drawRect(QRectF(-size / 2, -size / 4, size, size / 2))
        p.restore()
    icon = render(round(96 * scale / 100))
    p.drawImage(QPointF((w - icon.width()) / 2, h * 0.36 - icon.height() / 2), icon)
    p.end()
    return image.convertToFormat(QImage.Format.Format_RGB888)


def _wizard_small(scale: int) -> QImage:
    """Top-right logo of the installer's inner pages (55x58 at 100%)."""
    w, h = round(55 * scale / 100), round(58 * scale / 100)
    image = QImage(w, h, QImage.Format.Format_RGB32)
    image.fill(QColor("#ffffff"))
    icon = render(min(w, h) - round(8 * scale / 100))
    p = QPainter(image)
    p.drawImage(QPointF((w - icon.width()) / 2, (h - icon.height()) / 2), icon)
    p.end()
    return image.convertToFormat(QImage.Format.Format_RGB888)


def write_installer_images(folder: str | Path) -> dict[str, str]:
    """24-bit BMPs for Inno Setup. Returns comma-separated file lists per image kind."""
    folder = Path(folder)
    files: dict[str, list[str]] = {"large": [], "small": []}
    for scale in WIZARD_SCALES:
        for kind, render_fn in (("large", _wizard_large), ("small", _wizard_small)):
            path = folder / f"wizard-{kind}-{scale}.bmp"
            render_fn(scale).save(str(path), "BMP")
            files[kind].append(str(path))
    return {kind: ",".join(paths) for kind, paths in files.items()}


def app_icon() -> QIcon:
    icon = QIcon()
    for size in ICO_SIZES:
        icon.addPixmap(QPixmap.fromImage(render(size)))
    return icon


def write_ico(path: str) -> None:
    """Multi-resolution .ico (PNG entries) for the PyInstaller exe."""
    pngs = []
    for size in ICO_SIZES:
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        render(size).save(buf, "PNG")
        pngs.append((size, bytes(buf.data())))
    offset = 6 + 16 * len(pngs)
    header = struct.pack("<HHH", 0, 1, len(pngs))
    entries, blobs = b"", b""
    for size, png in pngs:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(blobs))
        blobs += png
    with open(path, "wb") as f:
        f.write(header + entries + blobs)
