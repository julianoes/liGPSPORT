"""Render the liGPSPORT app icon with Qt.

    python packaging/macos/make_icon.py build/macos/ligpsport.icns
    python packaging/macos/make_icon.py preview.png   # 1024 px PNG

The icon is drawn from code rather than committed as a binary so it
can be tweaked in a diff. Writing ``.icns`` needs macOS (``iconutil``).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6 import QtCore, QtGui

# Sizes iconutil expects inside an .iconset: name -> pixels.
ICONSET = {
    f"icon_{base}x{base}{suffix}.png": base * scale
    for base in (16, 32, 128, 256, 512)
    for scale, suffix in ((1, ""), (2, "@2x"))
}


def render(size: int) -> QtGui.QImage:
    """Draw the icon on a 1024-unit canvas scaled to ``size`` pixels."""
    img = QtGui.QImage(size, size, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QtCore.Qt.GlobalColor.transparent)
    p = QtGui.QPainter(img)
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    p.scale(size / 1024, size / 1024)
    p.setPen(QtCore.Qt.PenStyle.NoPen)

    # Background tile, inset per Apple's macOS icon grid (824 px body).
    bg = QtGui.QLinearGradient(0, 100, 0, 924)
    bg.setColorAt(0, QtGui.QColor("#1f8f8a"))
    bg.setColorAt(1, QtGui.QColor("#0b3d5c"))
    p.setBrush(bg)
    p.drawRoundedRect(QtCore.QRectF(100, 100, 824, 824), 185, 185)

    # Bike computer body.
    p.setBrush(QtGui.QColor("#16191d"))
    p.drawRoundedRect(QtCore.QRectF(292, 196, 440, 632), 90, 90)

    # Screen.
    screen = QtCore.QRectF(332, 252, 360, 448)
    p.setBrush(QtGui.QColor("#e8efe9"))
    p.drawRoundedRect(screen, 36, 36)

    # Route on the screen.
    route = QtGui.QPainterPath(QtCore.QPointF(410, 635))
    route.cubicTo(420, 520, 560, 640, 560, 520)
    route.cubicTo(560, 400, 430, 440, 470, 330)
    route.cubicTo(495, 270, 600, 300, 640, 310)
    pen = QtGui.QPen(QtGui.QColor("#f2711c"), 30)
    pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    p.drawPath(route)

    # Current position.
    p.setPen(QtGui.QPen(QtGui.QColor("#e8efe9"), 14))
    p.setBrush(QtGui.QColor("#0b3d5c"))
    p.drawEllipse(QtCore.QPointF(410, 635), 34, 34)

    # Buttons below the screen.
    p.setPen(QtCore.Qt.PenStyle.NoPen)
    p.setBrush(QtGui.QColor("#3a4048"))
    for x in (400, 624):
        p.drawEllipse(QtCore.QPointF(x, 764), 22, 22)

    p.end()
    return img


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    out = Path(sys.argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    # Read when the application is created, not at import time.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _app = QtGui.QGuiApplication(sys.argv[:1])

    if out.suffix == ".png":
        if not render(1024).save(str(out)):
            raise SystemExit(f"failed to write {out}")
        return 0
    if out.suffix != ".icns":
        raise SystemExit("output must end in .icns or .png")

    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "ligpsport.iconset"
        iconset.mkdir()
        for name, px in ICONSET.items():
            if not render(px).save(str(iconset / name)):
                raise SystemExit(f"failed to write {name}")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
