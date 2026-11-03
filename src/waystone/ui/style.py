"""Colors, the stylesheet and small generated images."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPalette, QPen,
                           QPixmap, QPolygonF)

ACCENT = '#2f6fd6'
FAMILY_COLORS = {'retail': '#2f6fd6', 'classic': '#c4892b', 'forever': '#7d4fd1'}
FAMILY_NAMES = {'retail': 'Retail', 'classic': 'Classic', 'forever': 'Forever'}
# Shades within a family, so tiles differ by more than their letters.
CLIENT_COLORS = {'wow_classic': '#c98a2b', 'wow_anniversary': '#b2652c', 'wow_classic_era': '#9c7a3c',
                 'wow_classic_ptr': '#c98a2b', 'wow_classic_era_ptr': '#9c7a3c'}


def client_color(client) -> str:
    return CLIENT_COLORS.get(client.code, FAMILY_COLORS[client.family])

BACKGROUND = '#15171c'
SURFACE = '#1b1e24'
RAISED = '#22252d'
BORDER = '#2e333d'
TEXT = '#e6e8ec'
MUTED = '#959ba6'
FAINT = '#6e7480'

SPACING = 8              # the grid every margin and gap uses
CONTROL_HEIGHT = 34


def dark_palette() -> QPalette:
    """Colors for everything the stylesheet does not draw, such as popups and dialogs."""
    palette = QPalette()
    colors = {
        QPalette.Window: BACKGROUND, QPalette.WindowText: TEXT, QPalette.Base: '#1d2027',
        QPalette.AlternateBase: RAISED, QPalette.Text: TEXT, QPalette.Button: RAISED,
        QPalette.ButtonText: TEXT, QPalette.Highlight: ACCENT, QPalette.HighlightedText: '#ffffff',
        QPalette.ToolTipBase: RAISED, QPalette.ToolTipText: TEXT, QPalette.PlaceholderText: FAINT,
        QPalette.Link: '#6fa0f5', QPalette.BrightText: '#ffffff',
    }
    for role, color in colors.items():
        palette.setColor(role, QColor(color))
    for role in [QPalette.Text, QPalette.ButtonText, QPalette.WindowText]:
        palette.setColor(QPalette.Disabled, role, QColor(FAINT))
    return palette


def _draw(path: Path, width: int, height: int, paint) -> None:
    # Twice the size, so the images stay sharp on scaled displays.
    pixmap = QPixmap(width * 2, height * 2)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    paint(painter)
    painter.end()
    pixmap.save(str(path), 'PNG')


def make_images(directory: Path) -> dict[str, str]:
    """Draw the drop-down chevron and check mark. PNG needs no image plugin."""
    directory.mkdir(parents=True, exist_ok=True)
    images = {'chevron': directory / 'chevron.png', 'check': directory / 'check.png',
              'chevron_up': directory / 'chevron-up.png'}

    def chevron(painter):
        painter.setPen(QPen(QColor(MUTED), 3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPolyline([QPointF(4, 4), QPointF(10, 10), QPointF(16, 4)])

    def chevron_up(painter):
        painter.translate(0, 14)
        painter.scale(1, -1)
        chevron(painter)

    def check(painter):
        painter.setPen(QPen(QColor('white'), 3.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPolyline([QPointF(6, 12.5), QPointF(10.5, 17), QPointF(18.5, 7.5)])

    _draw(images['chevron'], 10, 7, chevron)
    _draw(images['check'], 12, 12, check)
    _draw(images['chevron_up'], 10, 7, chevron_up)
    return {name: path.as_posix() for name, path in images.items()}


def stylesheet(images: dict[str, str]) -> str:
    return f'''
* {{ font-size: 10.5pt; }}
QMainWindow, QWidget#page, QWidget#content {{ background: {BACKGROUND}; }}
QLabel {{ color: {TEXT}; background: transparent; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#small {{ color: {MUTED}; font-size: 9.5pt; }}
QLabel#title {{ font-size: 21pt; font-weight: 600; }}
QLabel#pagetitle {{ font-size: 17pt; font-weight: 600; }}
QLabel#brand {{ font-size: 11pt; font-weight: 800; letter-spacing: 2px; color: {TEXT}; }}
QLabel#group {{ color: #858b96; font-size: 8.5pt; font-weight: 700; letter-spacing: 1px; }}
QLabel#tag {{ color: #c9ced6; background: #30353f; border-radius: 4px; padding: 1px 6px;
             font-size: 8pt; font-weight: 700; }}
QLabel#chip {{ color: #c9ced6; background: #262a33; border-radius: 9px; padding: 2px 10px; }}
QLabel#chip[tone="good"] {{ color: #9fd8a8; background: #1d2c22; }}
QLabel#chip[tone="attention"] {{ color: #f0c27a; background: #33291a; }}
QLabel#chip[tone="active"] {{ color: #a9c6ff; background: #1c2840; }}
QLabel#chip[tone="danger"] {{ color: #f19a9a; background: #3a1f22; }}
QLabel#key {{ color: {MUTED}; }}
QLabel#cardtitle, QCheckBox#cardtitle {{ font-size: 11pt; font-weight: 600; }}
QFrame#sidebar {{ background: {SURFACE}; border-right: 1px solid #20232a; }}
QFrame#separator {{ background: #262a33; max-height: 1px; min-height: 1px; }}
QFrame#card {{ background: {SURFACE}; border: 1px solid #23262d; border-radius: 10px; }}
QFrame#downloads {{ background: {SURFACE}; }}
QFrame#downloads QLabel:focus {{ color: #c3d6ff; text-decoration: underline; }}
QFrame#footer {{ background: {BACKGROUND}; border-top: 1px solid #23262d; }}
QFrame#banner {{ background: #33231a; border: 1px solid #5a3a22; border-radius: 8px; }}
QFrame#banner[kind="info"] {{ background: #1a2436; border-color: #2c4064; }}
QFrame#banner[kind="error"], QFrame#banner[kind="failure"] {{ background: #2e1a1d; border-color: #6a2c30; }}
QListWidget {{ background: transparent; border: none; outline: none; }}
QListWidget::item {{ border-radius: 7px; margin: 0; }}
QListWidget::item:selected {{ background: #282c35; border-left: 3px solid {ACCENT}; }}
QListWidget[focused="true"]::item:selected {{ border-left: 3px solid #8fb3ff; background: #2c3140; }}
QListWidget[inactive="true"]::item:selected {{ background: #1f2228; border-left: 3px solid transparent; }}
QListWidget::item:hover:!selected {{ background: #20232a; }}
QPushButton, QToolButton {{ background: {RAISED}; color: {TEXT}; border: 1px solid transparent;
                            border-radius: 6px; padding: 0 14px; min-height: {CONTROL_HEIGHT - 2}px; }}
QPushButton:hover, QToolButton:hover {{ background: #2c3039; }}
QPushButton:focus, QToolButton:focus {{ border-color: #4a6fb0; }}
QPushButton:disabled, QToolButton:disabled {{ color: {FAINT}; background: #1d2026; border-color: #2a2e37; }}
QPushButton#primary {{ background: {ACCENT}; color: white; font-size: 11pt; font-weight: 600;
                      min-height: 38px; min-width: 132px; padding: 0 22px; }}
QPushButton#primary:hover {{ background: #3b7deb; }}
QPushButton#destructive {{ background: #8c2f35; color: white; font-weight: 600; border: none; }}
QPushButton#destructive:hover {{ background: #a3373e; }}
QPushButton#destructive:focus {{ border: 2px solid #f1b5b5; }}
QPushButton#primary:focus {{ border: 2px solid #c3d6ff; }}
QPushButton#primary:disabled {{ background: #26314a; color: #8a96ad; }}
QPushButton#primary[secondary="true"] {{ background: {RAISED}; color: {TEXT}; }}
QPushButton#primary[secondary="true"]:hover {{ background: #2c3039; }}
QPushButton#primary[secondary="true"]:disabled {{ background: #1d2026; color: {MUTED}; }}
QPushButton#reorder {{ padding: 0; font-size: 16px; }}
QPushButton#reorder:disabled {{ border-color: transparent; background: transparent; }}
QPushButton#flat {{ background: transparent; color: {MUTED}; padding: 0 6px; min-height: 24px; }}
QPushButton#flat:hover {{ color: {TEXT}; }}
QPushButton#flat:disabled {{ color: #535863; background: transparent; border-color: transparent; }}
QPushButton#nav {{ background: transparent; color: #b4bac4; text-align: left; padding: 0 10px; }}
QPushButton#nav:hover {{ background: #20232a; color: {TEXT}; }}
QPushButton#nav:checked {{ background: #282c35; color: {TEXT}; }}
QPushButton#nav:focus {{ border-color: #4a6fb0; color: {TEXT}; }}
QPushButton#nav:disabled {{ background: transparent; border-color: transparent; color: {FAINT}; }}
QPushButton#segment {{ background: #1f2229; color: #a9afba; border-radius: 0; border: 1px solid {BORDER};
                      padding: 0 14px; }}
QPushButton#segment:checked {{ background: #1c2840; color: #a9c6ff; border-color: {ACCENT}; }}
QPushButton#segment:focus {{ border-color: #6f97e0; }}
QPushButton#segment[edge="first"] {{ border-top-left-radius: 6px; border-bottom-left-radius: 6px; }}
QPushButton#segment[edge="last"] {{ border-top-right-radius: 6px; border-bottom-right-radius: 6px; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
QToolButton#more {{ font-size: 16pt; font-weight: 700; padding: 0 0 4px 0; }}
QMenu {{ background: #1d2027; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT}; }}
QProgressBar {{ background: #2a2e37; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QProgressBar#meter::chunk {{ background: #6b7382; }}
QProgressBar#strip {{ background: #23262d; border-radius: 0; max-height: 3px; min-height: 3px; }}
QProgressBar#strip::chunk {{ border-radius: 0; }}
QProgressBar#strip[tone="attention"]::chunk {{ background: #d9a650; }}
QProgressBar[tone="attention"]::chunk {{ background: #d9a650; }}
QLabel#titlelarge {{ font-size: 24pt; font-weight: 600; }}
QComboBox, QLineEdit, QSpinBox {{ background: #1f2229; color: {TEXT}; border: 1px solid {BORDER};
                       border-radius: 6px; padding: 0 10px; min-height: {CONTROL_HEIGHT - 2}px; }}
QComboBox:hover, QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: #4a6fb0; }}
QPlainTextEdit {{ background: #1f2229; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 6px;
                  padding: 4px 6px; }}
QPlainTextEdit:focus {{ border-color: #4a6fb0; }}
QLabel#problem {{ color: #f19a9a; font-size: 9.5pt; }}
QLabel#caution {{ color: #f0c27a; font-size: 9.5pt; }}
QLineEdit:read-only {{ background: transparent; border-color: #262a33; color: #c9ced6; }}
QLineEdit[locked="true"] {{ color: {FAINT}; }}
QSpinBox {{ padding-right: 26px; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 24px; border: none; background: transparent; }}
QSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; }}
QSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; }}
QSpinBox::up-arrow {{ image: url({images['chevron_up']}); width: 10px; height: 7px; }}
QSpinBox::down-arrow {{ image: url({images['chevron']}); width: 10px; height: 7px; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{ image: url({images['chevron']}); width: 10px; height: 7px; }}
QComboBox QAbstractItemView {{ background: #1d2027; color: {TEXT}; border: 1px solid {BORDER};
                              selection-background-color: {ACCENT}; outline: none; padding: 4px; }}
QCheckBox {{ color: {TEXT}; spacing: 10px; }}
QCheckBox:disabled {{ color: {MUTED}; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid #4a5160;
                       background: #1f2229; }}
QCheckBox::indicator:hover {{ border-color: #6b7486; }}
QCheckBox:focus {{ color: white; }}
QCheckBox::indicator:focus {{ border-color: #6f97e0; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url({images['check']}); }}
QCheckBox::indicator:checked:disabled {{ background: #33415c; border-color: #33415c; }}
QRadioButton {{ color: {TEXT}; spacing: 10px; }}
QRadioButton#cardtitle {{ font-size: 11pt; font-weight: 600; }}
QRadioButton:focus {{ color: white; }}
QRadioButton::indicator {{ width: 16px; height: 16px; border-radius: 9px; border: 1px solid #4a5160;
                          background: #1f2229; }}
QRadioButton::indicator:hover {{ border-color: #6b7486; }}
QRadioButton::indicator:focus {{ border-color: #6f97e0; }}
QRadioButton::indicator:checked {{ border-color: {ACCENT}; background: qradialgradient(cx:0.5, cy:0.5, radius:0.5,
    fx:0.5, fy:0.5, stop:0 #ffffff, stop:0.32 #ffffff, stop:0.42 {ACCENT}, stop:1 {ACCENT}); }}
QDialogButtonBox {{ dialogbuttonbox-buttons-have-icons: 0; }}
QScrollArea {{ background: transparent; border: none; }}
QTreeView#folders {{ background: {SURFACE}; color: {TEXT}; border: 1px solid #23262d; border-radius: 8px;
                    padding: 4px; outline: none; }}
QTreeView#folders::item {{ padding: 3px; }}
QTreeView#folders::item:selected {{ background: {ACCENT}; color: white; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #30353f; border-radius: 4px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #30353f; border-radius: 4px; min-width: 30px; }}
QListWidget QScrollBar:vertical {{ width: 5px; margin: 2px 0; }}
QListWidget QScrollBar::handle:vertical {{ border-radius: 2px; background: #4a505c; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ height: 0; width: 0; background: none; }}
QToolTip {{ background: {RAISED}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px; }}
'''


# Each live client has a plain shape of its own. Its test and other clients share it.
GLYPHS = {'wow': 'star', 'wow_classic': 'moon', 'wow_anniversary': 'flame', 'wow_classic_era': 'hourglass',
          'wowf': 'loop'}
FAMILY_GLYPHS = {'retail': 'star', 'classic': 'hourglass', 'forever': 'loop'}
FILLED = {'star'}       # drawn as a solid shape, the others as an outline


def glyph_name(client) -> str:
    return GLYPHS.get(client.code) or GLYPHS.get(client.parent) or FAMILY_GLYPHS[client.family]


def glyph_path(name: str) -> QPainterPath:
    """The shape in a 24 by 24 box."""
    path = QPainterPath()
    if name == 'star':
        points = [(12, 2.5), (14, 10), (21.5, 12), (14, 14), (12, 21.5), (10, 14), (2.5, 12), (10, 10)]
        path.addPolygon(QPolygonF([QPointF(x, y) for x, y in points]))
        path.closeSubpath()
    elif name == 'moon':
        disc = QPainterPath()
        disc.addEllipse(QPointF(11, 12.5), 8.5, 8.5)
        bite = QPainterPath()
        bite.addEllipse(QPointF(16.5, 8.5), 7, 7)
        path = disc.subtracted(bite)
    elif name == 'flame':
        path.moveTo(12, 21)
        path.cubicTo(8, 21, 5.5, 18.4, 5.5, 15)
        path.cubicTo(5.5, 11, 9.5, 9, 10, 4.5)
        path.cubicTo(12.5, 6.5, 13, 9, 13, 10.5)
        path.cubicTo(14, 9.7, 14.7, 8.5, 14.8, 7.2)
        path.cubicTo(17.2, 9.2, 18.5, 12, 18.5, 15)
        path.cubicTo(18.5, 18.4, 16, 21, 12, 21)
    elif name == 'hourglass':
        path.moveTo(6, 3)
        path.lineTo(18, 3)
        path.moveTo(6, 21)
        path.lineTo(18, 21)
        for side in (1, -1):
            x = lambda value: 12 + side * (value - 12)
            path.moveTo(x(7.5), 3)
            path.cubicTo(x(7.5), 8, 12, 9.5, 12, 12)
            path.cubicTo(12, 14.5, x(7.5), 16, x(7.5), 21)
    elif name == 'loop':
        path.moveTo(12, 12)
        path.cubicTo(9.4, 8, 4, 8, 4, 12)
        path.cubicTo(4, 16, 9.4, 16, 12, 12)
        path.cubicTo(14.6, 8, 20, 8, 20, 12)
        path.cubicTo(20, 16, 14.6, 16, 12, 12)
    return path


def mix(color: str, other: str, amount: float) -> QColor:
    """color moved amount of the way towards other."""
    a, b = QColor(color), QColor(other)
    return QColor.fromRgbF(*(x + (y - x) * amount for x, y in
                             zip((a.redF(), a.greenF(), a.blueF()), (b.redF(), b.greenF(), b.blueF()))))


def accent_shade(color: str) -> str:
    """The client's color, darkened until white text on it is readable (WCAG 4.5:1)."""
    def luminance(value: QColor) -> float:
        channels = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                    for c in (value.redF(), value.greenF(), value.blueF())]
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
    amount = 0.0
    shade = QColor(color)
    while 1.05 / (luminance(shade) + 0.05) < 4.5 and amount < 1:
        amount += 0.04
        # Measured as the stylesheet will see it, rounded to a hex color.
        shade = QColor(mix(color, '#000000', amount).name())
    return shade.name()


def draw_glyph(painter: QPainter, name: str, box: QRectF, color: QColor, width: float = 1.8) -> None:
    """The glyph scaled into box, filled or outlined with width in the glyph's own units."""
    painter.save()
    painter.translate(box.topLeft())
    painter.scale(box.width() / 24, box.height() / 24)
    path = glyph_path(name)
    if name in FILLED:
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
    else:
        painter.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush)
    painter.drawPath(path)
    painter.restore()


def tile(client, size: int = 28) -> QPixmap:
    """A rounded tile with the client's glyph, so the list needs no artwork.

    Test clients add stripes and other public clients dots, over their live client's glyph.
    """
    color = client_color(client)
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    shape = QPainterPath()
    radius = size * 0.24
    shape.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
    painter.setClipPath(shape)
    gradient = QLinearGradient(0, 0, size * 0.6, size)
    gradient.setColorAt(0, mix(color, '#ffffff', 0.34))
    gradient.setColorAt(0.5, QColor(color))
    gradient.setColorAt(1, mix(color, '#000000', 0.34))
    painter.fillRect(0, 0, size, size, gradient)
    painter.setPen(QPen(QColor(255, 255, 255, 34), 1.2))
    if client.test:
        step = max(4.0, size / 6)
        position = -size
        while position < size:
            painter.drawLine(QPointF(position, size), QPointF(position + size, 0))
            position += step
    elif client.optional:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 60))
        step = max(4.0, size / 6)
        y = step / 2
        while y < size:
            x = step / 2
            while x < size:
                painter.drawEllipse(QPointF(x, y), 1.0, 1.0)
                x += step
            y += step
    # Lit from above: a light top edge and a darker bottom one.
    edge = QLinearGradient(0, 0, 0, size)
    edge.setColorAt(0, QColor(255, 255, 255, 70))
    edge.setColorAt(0.5, QColor(255, 255, 255, 0))
    edge.setColorAt(1, QColor(0, 0, 0, 70))
    painter.setPen(QPen(QBrush(edge), 1.5))
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(shape)
    # Small tiles give the glyph more of the tile and a thicker line, so it stays readable.
    share = 0.62 if size >= 28 else 0.7
    inset = size * (1 - share) / 2
    box = QRectF(inset, inset, size * share, size * share)
    width = 1.8 if size >= 28 else 2.3
    name = glyph_name(client)
    draw_glyph(painter, name, box.translated(0, size * 0.03), QColor(0, 0, 0, 80), width)
    draw_glyph(painter, name, box, QColor('white'), width)
    painter.end()
    return pixmap


def size_text(value: float) -> str:
    """Decimal units, as file managers and the free-space checks show them."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if value < 1000 or unit == 'TB':
            return f'{int(value)} B' if unit == 'B' else f'{value:.1f} {unit}'
        value /= 1000
    return ''
