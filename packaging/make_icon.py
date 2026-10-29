"""Render the app icon into a Windows .ico with one PNG image per size.

Usage: python make_icon.py <icon.svg> <output.ico>
"""
from pathlib import Path
import struct
import sys

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QGuiApplication, QImage, QPainter, Qt
from PySide6.QtSvg import QSvgRenderer

SIZES = [16, 20, 24, 32, 40, 48, 64, 256]     # the sizes Explorer and the taskbar ask for


def png(renderer: QSvgRenderer, size: int) -> bytes:
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, 'PNG')
    return bytes(buffer.data())


def main(svg: str, ico: str) -> None:
    application = QGuiApplication(['make_icon', '-platform', 'offscreen'])  # noqa: F841, QPainter needs it
    renderer = QSvgRenderer(svg)
    if not renderer.isValid():
        raise SystemExit(f'{svg} is not a valid SVG')
    images = [png(renderer, size) for size in SIZES]
    offset = 6 + 16 * len(images)
    header = struct.pack('<HHH', 0, 1, len(images))
    for size, data in zip(SIZES, images):
        # A width or height of 0 means 256.
        header += struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    Path(ico).write_bytes(header + b''.join(images))


if __name__ == '__main__':
    main(*sys.argv[1:])
