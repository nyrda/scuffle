"""Colors, the stylesheet and small generated images."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen, QPixmap

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
QSpinBox {{ padding-left: 19px; padding-right: 26px; }}   /* text where a dropdown's starts */
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


def tile(text: str, color: str, size: int = 28) -> QPixmap:
    """A rounded label tile, so the list needs no artwork."""
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(0, 0, size, size, size * 0.26, size * 0.26)
    painter.setPen(QColor('white'))
    font = QFont()
    font.setBold(True)
    font.setPixelSize(int(size * (0.36 if len(text) > 2 else 0.44)))
    painter.setFont(font)
    painter.drawText(0, 0, size, size, Qt.AlignCenter, text)
    painter.end()
    return pixmap


def size_text(value: float) -> str:
    """Decimal units, as file managers and the free-space checks show them."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if value < 1000 or unit == 'TB':
            return f'{int(value)} B' if unit == 'B' else f'{value:.1f} {unit}'
        value /= 1000
    return ''
