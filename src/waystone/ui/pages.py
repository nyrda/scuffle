"""The pages shown beside the client list."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import sys

from PySide6.QtCore import QDir, QEvent, QPointF, QRectF, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QDesktopServices, QKeySequence, QLinearGradient, QPainter, QPen,
                           QRadialGradient, QShortcut, QTransform)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QFileSystemModel,
                               QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QProgressBar, QPushButton,
                               QRadioButton, QScrollArea, QSizePolicy, QSpacerItem, QSpinBox, QToolButton, QTreeView, QVBoxLayout, QWidget)

from .. import catalog
from ..diagnostics import LOG_NAME
from ..launch import log_dir, looks_like_redirect, open_browser, parse_ticket
from ..settings import LOCALE_NAMES, MEGABYTE, REGIONS, Settings
from .launch_options import LaunchSettings
from .style import (BACKGROUND, FAMILY_NAMES, SPACING, accent_shade, client_color, glyph_name, glyph_path, mix,
                    size_text, tile)
from .widgets import (Banner, ElidedLabel, FactsCard, Segmented, StorageCard, duration_text, fixed, label,
                      progress_variants, tone)

PAGE_MARGINS = (32, 28, 32, 24)
COLUMN_WIDTH = 780
FIELD_WIDTH = 480
ROW_WIDTH = 96 + 16 + FIELD_WIDTH      # a settings row: its name, the spacing and the capped field


class UninstallPage(QWidget):
    """An explicit confirmation, kept separate from the client's play controls."""
    confirmed = Signal()
    cancelled = Signal()

    def __init__(self):
        super().__init__()
        self.code = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*PAGE_MARGINS)
        layout.setSpacing(18)
        self.title = label('', 'title', wrap=True)
        self.details = label(wrap=True)
        self.keep = QCheckBox('Keep WTF, Interface and Screenshots (recommended)')
        self.explanation = label('Keeps settings, addons and screenshots in a Waystone-backup folder inside '
                                 'your installation folder. You can copy them back after reinstalling.', wrap=True)
        self.warning = label('If unchecked, they are deleted too. All other files in this client\'s '
                             'game folder are deleted, including files you added.', wrap=True)
        self.problem = label(wrap=True)
        for widget in (self.title, self.details, self.keep, self.explanation, self.warning, self.problem):
            layout.addWidget(widget)
        actions = QHBoxLayout()
        self.cancel = fixed(QPushButton('Cancel'))
        self.confirm = fixed(QPushButton('Uninstall'))
        self.confirm.setObjectName('destructive')     # red, so it never reads as the safe choice
        self.cancel.clicked.connect(self.cancelled.emit)
        self.confirm.clicked.connect(self.confirmed.emit)
        actions.addWidget(self.cancel)
        actions.addWidget(self.confirm)
        actions.addStretch()
        layout.addLayout(actions)
        layout.addStretch()

    def load(self, code: str, size: int):
        self.code = code
        self.title.setText(f'Uninstall {catalog.get(code).label}?')
        self.details.setText(f'Up to {size_text(size)} of client content can be freed. Content shared '
                             'with other clients stays. Kept addons and settings still take up space.')
        self.keep.setChecked(True)
        self.set_reason('')
        self.cancel.setFocus()

    def set_reason(self, reason: str):
        self.problem.setText(reason)
        self.problem.setVisible(bool(reason))
        self.confirm.setEnabled(not reason)
        self.confirm.setToolTip(reason)

    def focus_chain(self):
        return [self.keep, self.cancel, self.confirm]


class Tinted(QWidget):
    """A page body with a faint band of the client's color and its glyph behind the header."""

    def __init__(self):
        super().__init__()
        self.client = None

    def set_client(self, client: catalog.Client | None):
        if client != self.client:
            self.client = client
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(BACKGROUND))
        if self.client:
            painter.setRenderHint(QPainter.Antialiasing)
            color = client_color(self.client)
            # A header band that grows with the window, so tall windows look planned.
            height = min(280, max(160, int(self.height() * 0.28)))
            band = QLinearGradient(0, 0, 0, height)
            band.setColorAt(0, faded(color, 34))
            band.setColorAt(1, faded(color, 0))
            painter.fillRect(0, 0, self.width(), height, band)
            # A brighter wash from the top left corner, behind the client's tile and name.
            # Stretched sideways, so it is gone by the band's bottom edge on every window.
            wash = QRadialGradient(QPointF(0, 0), height)
            wash.setColorAt(0, faded(color, 52))
            wash.setColorAt(1, faded(color, 0))
            stretch = max(320, self.width() * 0.55) / height
            painter.save()
            painter.scale(stretch, 1)
            painter.fillRect(QRectF(0, 0, self.width() / stretch, height), wash)
            painter.restore()
            # The glyph, large and faint at the top right, with rings that fade out downwards.
            size = height * 1.15
            centre = QPointF(self.width() - size * 0.36, height * 0.32)
            fade = QLinearGradient(0, 0, 0, height)
            fade.setColorAt(0, faded(color, 60, lighter=True))
            fade.setColorAt(1, faded(color, 0, lighter=True))
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QBrush(fade), 1))
            for ring in range(1, 10):
                painter.drawEllipse(centre, ring * 15.0, ring * 15.0)
            # Outlined at this size, even a glyph the tiles fill, so it stays in the background.
            place = QTransform()
            place.translate(centre.x() - size / 2, centre.y() - size / 2)
            place.scale(size / 24, size / 24)
            painter.setClipRect(QRectF(0, 0, self.width(), height))
            painter.setPen(QPen(QBrush(fade), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawPath(place.map(glyph_path(glyph_name(self.client))))
        painter.end()


def faded(color: str, alpha: int, lighter: bool = False) -> QColor:
    value = mix(color, '#ffffff', 0.45) if lighter else QColor(color)
    value.setAlpha(alpha)
    return value


GUTTER = 10          # the vertical scroll bar's width in the stylesheet


def scrolling(page: QWidget, body: QWidget | None = None, margins=PAGE_MARGINS) -> QVBoxLayout:
    """Give a page a scrolling body, so a short window never squeezes its rows."""
    outer = QVBoxLayout(page)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    # The scroll area itself is not a keyboard stop.
    scroll.setFocusPolicy(Qt.NoFocus)
    # Scrolls sideways only below the window's minimum width, instead of clipping.
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    body = body or QWidget()
    body.setObjectName('content')
    scroll.setWidget(body)
    outer.addWidget(scroll, 1)
    layout = QVBoxLayout(body)
    left, top, right, bottom = margins

    def keep_place(_minimum, maximum):
        # Without a scroll bar the right margin takes its width, so content never shifts
        # sideways, and every page has the same right edge.
        layout.setContentsMargins(left, top, right + (0 if maximum > 0 else GUTTER), bottom)
    scroll.verticalScrollBar().rangeChanged.connect(keep_place)
    keep_place(0, 0)

    def keep_focus_visible(*_):
        # After a resize the focused control can end up outside the view. Keys still reach it.
        focused = QApplication.focusWidget()
        if focused is not None and body.isAncestorOf(focused):
            scroll.ensureWidgetVisible(focused, 0, 24)
    scroll.verticalScrollBar().rangeChanged.connect(lambda *_: QTimer.singleShot(0, keep_focus_visible))
    return layout


class FolderField(QLineEdit):
    """A read-only path that opens the folder picker when clicked."""

    clicked = Signal()

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.LeftButton:
            self.clicked.emit()


class Page(QWidget):
    """A page beside the client list. Its column is centred on wide windows."""

    WIDE = 1000

    def __init__(self):
        super().__init__()
        self.setObjectName('page')
        self._rows = []

    def centre(self, inner: QVBoxLayout) -> QVBoxLayout:
        self._rows.append(inner.row)
        if not hasattr(self, 'column_holder'):
            self.column_holder = inner.parentWidget()     # the download bar lines up with it
        return inner

    def fixed_top(self, layout: QVBoxLayout) -> None:
        """Pages without a scrolling body take the same top gap as the others on tall windows."""
        self._fixed_top = QWidget()
        self._fixed_top.setFixedHeight(0)
        layout.insertWidget(0, self._fixed_top)

    def balance(self, outer: QVBoxLayout) -> None:
        """On tall windows, a gap above the content, the same on every page so switching never jumps."""
        self._top = top_inset(outer)
        self._balanced = outer

    def on_escape(self, handler) -> None:
        shortcut = QShortcut(QKeySequence(Qt.Key_Escape), self)
        shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        shortcut.activated.connect(handler)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        height = self.window().height() if self.window() is not self else self.height()
        tall = height >= 820
        # One gap for every page on the same window, so the titles line up page to page.
        gap = min(TOP_INSET, max(0, 40 + (height - 820) // 3)) if tall else 0
        fixed = getattr(self, '_fixed_top', None)
        if fixed is not None:
            fixed.setFixedHeight(gap)
        if getattr(self, '_balanced', None) is not None:
            # The layout adds its spacing below the gap, which a fixed top does not have.
            self._top.setFixedHeight(max(0, gap - self._balanced.spacing()))
            self._top.setVisible(tall)
        wide = self.width() >= self.WIDE
        for row in self._rows:
            row.setStretch(0, 1 if wide else 0)
            row.setStretch(2, 1 if wide else 0)


WIDE_COLUMN = 1080   # the client page's column when its folder card sits beside it
PRIMARY_HEIGHT = 40  # a primary button's height, for the buttons in a row beside one
TOP_INSET = 140      # the most spare height that goes above a page's content


def speed_limit_box() -> QSpinBox:
    """Any whole MB/s, with 0 as no limit. Settings and the Downloads page share it."""
    box = fixed(QSpinBox())
    box.setRange(0, 10_000)
    box.setSuffix(' MB/s')
    box.setSpecialValueText('No limit')
    box.setKeyboardTracking(False)        # typing 25 is one change, not 2 then 25
    box.setFixedWidth(160)
    number_margins(box)
    return box


def number_margins(box: QSpinBox) -> None:
    # The stylesheet's padding does not reach a spin box's text, so it starts where a dropdown's does.
    box.lineEdit().setTextMargins(8, 0, 0, 0)


def top_inset(outer: QVBoxLayout) -> QWidget:
    """A gap above the content on tall windows, up to TOP_INSET. Page.resizeEvent sizes it."""
    gap = QWidget()
    gap.hide()
    outer.insertWidget(0, gap)
    return gap


def column(parent_layout, *, width: int = COLUMN_WIDTH) -> QVBoxLayout:
    """A left-aligned column that grows up to width and shrinks with the window."""
    holder = QWidget()
    holder.setMaximumWidth(width)
    inner = QVBoxLayout(holder)
    inner.setContentsMargins(0, 0, 0, 0)
    inner.setSpacing(0)
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    # Stretches instead of an alignment flag, so wrapped text keeps its height.
    row.addStretch(0)
    row.addWidget(holder, 20)
    row.addStretch(0)
    parent_layout.addLayout(row)
    inner.row = row            # lets a page centre the column on wide windows
    return inner


class ClientPage(Page):
    PRIMARY_LABELS = ['Play', 'Sign in and play', 'Install', 'Update', 'Repair', 'Try again', 'Pause',
                      'Resume', 'Queued', 'Checking…', 'Connecting…', 'Starting…', 'Running']
    SIDE_BY_SIDE = 1400      # page width from which the folder card sits beside the client

    primary_clicked = Signal()
    cancel_clicked = Signal()
    verify_clicked = Signal()
    open_folder_clicked = Signal()
    hide_clicked = Signal()
    uninstall_clicked = Signal()
    import_clicked = Signal()
    show_clients_clicked = Signal()
    launch_options_clicked = Signal()
    launcher_clicked = Signal()
    steam_clicked = Signal()
    mode_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.body = Tinted()
        self.outer = outer = scrolling(self, self.body)
        content = self.centre(column(outer))
        self.column_holder = content.parentWidget()
        self._side = False
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(16)
        self.grid.setVerticalSpacing(0)
        content.addLayout(self.grid)
        self.main = QWidget()
        self.main_layout = main = QVBoxLayout(self.main)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        header = QHBoxLayout()
        header.setSpacing(16)
        self.icon = QLabel()
        self.icon.setFixedSize(52, 52)
        heading = QVBoxLayout()
        heading.setSpacing(4)
        self.title = ElidedLabel('', 'title')
        meta = QHBoxLayout()
        meta.setSpacing(10)
        self.family = label('', 'muted')
        self.chip = label('', 'chip')
        meta.addWidget(self.family)
        meta.addWidget(self.chip)
        meta.addStretch()
        heading.addWidget(self.title)
        heading.addLayout(meta)
        self.blurb = label('', 'muted', wrap=True)
        heading.addWidget(self.blurb)
        header.addWidget(self.icon, 0, Qt.AlignTop)
        header.addLayout(heading, 1)
        main.addLayout(header)

        main.addSpacing(16)
        self.banner = Banner()
        main.addWidget(self.banner)

        self.mode_row = QWidget()
        mode_layout = QHBoxLayout(self.mode_row)
        mode_layout.setContentsMargins(0, 16, 0, 0)
        mode_layout.setSpacing(12)
        mode_layout.addWidget(label('Game mode', 'muted'))
        self.modes = Segmented()
        self.modes.changed.connect(self.mode_changed.emit)
        mode_layout.addWidget(self.modes)
        mode_layout.addStretch()
        main.addWidget(self.mode_row)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 18, 0, 0)
        actions.setSpacing(SPACING)
        self.primary = QPushButton('')
        self.primary.setObjectName('primary')
        self.primary.setFixedHeight(PRIMARY_HEIGHT)    # the focus ring must not change its height
        self.primary.clicked.connect(self.primary_clicked.emit)
        self._tall = False
        self._primary_width = 0
        self.cancel = QPushButton('Cancel')
        self.cancel.setFixedSize(96, PRIMARY_HEIGHT)      # level with the main button
        self.cancel.setToolTip('Stop this download. What is already stored stays for later.')
        self.cancel.clicked.connect(self.cancel_clicked.emit)
        self.more = QToolButton()
        self.more.setObjectName('more')
        self.more.setText('⋯')
        self.more.setToolTip('More actions')
        self.more.setFixedSize(PRIMARY_HEIGHT, PRIMARY_HEIGHT)
        self.more.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self.more)
        self.verify_action = menu.addAction('Verify files', self.verify_clicked.emit)
        self.folder_action = menu.addAction('Open folder', self.open_folder_clicked.emit)
        self.import_action = menu.addAction('Import…', self.import_clicked.emit)
        self.import_action.setToolTip('Copy this client from an installation you already have, such as Battle.net\'s')
        self.launch_action = menu.addAction('Launch options…', self.launch_options_clicked.emit)
        # Their text says whether the entry exists, which the window checks as the menu opens.
        self.launcher_action = menu.addAction('Add to app launcher', self.launcher_clicked.emit)
        self.steam_action = menu.addAction('Add to Steam', self.steam_clicked.emit)
        self.uninstall_action = menu.addAction('Uninstall…', self.uninstall_clicked.emit)
        menu.setToolTipsVisible(True)
        self.menu_separator = menu.addSeparator()
        self.hide_action = menu.addAction('Hide from list', self.hide_clicked.emit)
        self.more.setMenu(menu)
        actions.addWidget(self.primary)
        actions.addWidget(self.cancel)
        actions.addWidget(self.more)
        # Progress sits beside the buttons that control it, instead of in a row of its own.
        progress = QVBoxLayout()
        progress.setContentsMargins(10, 0, 0, 0)
        progress.setSpacing(6)
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setTextVisible(False)
        self.phase = ElidedLabel('', 'small')
        progress.addStretch()
        progress.addWidget(self.busy)
        progress.addWidget(self.phase)
        progress.addStretch()
        self.progress_row = QWidget()
        self.progress_row.setLayout(progress)
        self.progress_row.setFixedHeight(38)
        self.progress_row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        actions.addWidget(self.progress_row, 1)
        actions.addStretch()
        main.addLayout(actions)

        self.facts_gap = QWidget()
        self.facts_gap.setFixedHeight(22)
        main.addWidget(self.facts_gap)
        self.facts = FactsCard()
        main.addWidget(self.facts)
        self.storage = StorageCard()
        self.hint = label('', 'small', wrap=True)
        self.hint.setContentsMargins(2, 10, 0, 0)
        main.addWidget(self.hint)
        self.grid.addWidget(self.main, 0, 0)
        # In place of a client when every one is hidden. The list beside it offers the way back.
        self.empty = QWidget()
        empty = QVBoxLayout(self.empty)
        empty.setContentsMargins(0, 0, 0, 0)
        empty.setSpacing(6)
        empty.addWidget(label('No clients listed', 'pagetitle'))
        empty.addWidget(label('Every client is hidden from the list. Settings can show them again.', 'muted',
                              wrap=True))
        self.empty_button = fixed(QPushButton('Show clients'))
        self.empty_button.clicked.connect(self.show_clients_clicked.emit)
        empty.addSpacing(8)
        empty.addWidget(self.empty_button, 0, Qt.AlignLeft)
        self.empty.hide()
        self.grid.addWidget(self.empty, 1, 0)
        self.storage_gap = QWidget()
        self.storage_gap.setFixedHeight(14)
        self.main_layout.insertWidget(self.main_layout.indexOf(self.hint), self.storage_gap)
        self.main_layout.insertWidget(self.main_layout.indexOf(self.hint), self.storage)
        # Spare height goes below the content, and on tall windows a little above it too.
        self.balance(outer)
        outer.addStretch(1)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Short windows tighten the gaps and leave the folder path to the settings,
        # so the common states fit without scrolling.
        # By the window, not the page, so a download bar appearing does not rearrange the page.
        height = self.window().height() if self.window() is not self else self.height()
        short, tall = height < 620, height >= 820
        # Wide pages put the folder card beside the client instead of below it.
        side = self.width() >= self.SIDE_BY_SIDE
        if side != self._side:
            self._side = side
            self.main_layout.removeWidget(self.storage)
            self.grid.removeWidget(self.storage)
            if side:
                self.grid.addWidget(self.storage, 0, 1, Qt.AlignTop)
                self.grid.setColumnStretch(0, 3)
                self.grid.setColumnStretch(1, 2)
            else:
                self.main_layout.insertWidget(self.main_layout.indexOf(self.hint), self.storage)
                self.grid.setColumnStretch(1, 0)
            self.storage_gap.setVisible(not side)
            self.column_holder.setMaximumWidth(WIDE_COLUMN if side else COLUMN_WIDTH)
        self.hint.setContentsMargins(2, 4 if short else 10, 0, 0)
        self.facts_gap.setFixedHeight(12 if short else 22)
        self.storage_gap.setFixedHeight(10 if short else 14)
        self.storage.path.setVisible(not short)
        # Tall windows give the header more presence, in step with the tinted band behind it.
        if tall != self._tall:
            self._tall = tall
            size = 64 if tall else 52
            self.icon.setFixedSize(size, size)
            self.title.setObjectName('titlelarge' if tall else 'title')
            self.title.style().unpolish(self.title)
            self.title.style().polish(self.title)
            if getattr(self, '_client', None):
                self.icon.setPixmap(tile(self._client, size))

    def focus_chain(self) -> list[QWidget]:
        """The page's controls in reading order, for the window's tab order."""
        if not self.empty.isHidden():
            return [self.empty_button]
        widgets = [self.banner.action, self.banner.close_button, *self.modes.group.buttons(),
                   self.primary, self.cancel, self.more]
        return widgets

    def accent(self, color: str) -> None:
        """The main button and the progress bar take the client's color.

        Only the accented state is restyled, so Pause and the other secondary states keep
        the application's look. The shade keeps white text readable.
        """
        if color == getattr(self, '_accent', None):
            return
        self._accent = color
        shade = accent_shade(color)
        self.primary.setStyleSheet(
            f'QPushButton#primary[secondary="false"]:enabled {{ background: {shade}; }}'
            f'QPushButton#primary[secondary="false"]:enabled:hover {{ background: {mix(shade, "#000000", 0.15).name()}; }}'
            f'QPushButton#primary[secondary="false"]:disabled {{ background: {mix(shade, BACKGROUND, 0.6).name()}; }}')
        self.busy.setStyleSheet(f'QProgressBar[tone=""]::chunk {{ background: {color}; }}')

    def show_empty(self) -> None:
        self._client = None
        self.body.set_client(None)
        self.main.hide()
        self.storage.hide()
        self.empty.show()

    def show_client(self, client: catalog.Client, *, status: str, tone_name: str, primary: str,
                    primary_enabled: bool, installed: bool, busy: bool, progress: float | None,
                    primary_tip: str = '', busy_launch: bool = False, paused: bool = False, queued: bool = False,
                    progress_text, cancellable: bool, modes: list, mode: str | None,
                    facts: list, hint: str, hideable: bool = True) -> None:
        self.main.show()
        self.empty.hide()
        self._client = client
        self.icon.setPixmap(tile(client, self.icon.width()))
        self.body.set_client(client)
        self.accent(client_color(client))
        self.title.setText(client.label)
        self.family.setText(FAMILY_NAMES[client.family] + (' · test client' if client.test else
                                                            ' · other public client' if client.optional else ''))
        other = catalog.other_game(client.code)
        # A PTR can carry another game's build, such as Classic Era PTR testing Burning Crusade.
        self.blurb.setText(f'{client.blurb} It runs a {other} build for now.' if other else client.blurb)
        self.chip.setText(status)
        tone(self.chip, tone_name)
        # The mode does not change what is installed, so it appears once there is something to play.
        self.mode_row.setVisible(bool(modes) and installed)
        self.modes.set_options(modes, mode)
        # The mode is read when Play starts, so it is fixed while a launch is under way.
        self.modes.setEnabled(not busy_launch)
        self.primary.setText(primary)
        if not self._primary_width:
            # One width for every label, so the buttons beside it never move between states.
            self.primary.ensurePolished()
            metrics = self.primary.fontMetrics()
            self._primary_width = max(metrics.horizontalAdvance(text) for text in self.PRIMARY_LABELS) + 48
            self.primary.setFixedWidth(self._primary_width)
        self.primary.setEnabled(primary_enabled)
        self.primary.setToolTip(primary_tip)
        self.cancel.setVisible(cancellable)
        self.cancel.setToolTip('Take it out of the queue' if queued else
                               'Stop this download. What is already stored stays for later.')
        for action in (self.verify_action, self.folder_action, self.uninstall_action, self.menu_separator):
            action.setVisible(installed)
        self.import_action.setVisible(not installed)
        # Checking files while the game runs or starts would read files it has open.
        self.verify_action.setEnabled(installed and not busy and not busy_launch and progress is None)
        # A client with a download stays listed while the download exists.
        self.hide_action.setEnabled(hideable)
        # A launch shows an indeterminate bar with its phase. A download shows its own progress.
        self.progress_row.setVisible(busy or progress is not None or queued)
        self.busy.setVisible(not queued)
        if progress is None:
            self.busy.setRange(0, 0)
        else:
            self.busy.setRange(0, 1000)
            self.busy.setValue(int(progress * 1000))
        variants = [progress_text] if isinstance(progress_text, str) else list(progress_text)
        self.phase.set_variants(variants)
        # Without text the bar centres on the buttons.
        self.phase.setVisible(any(variants))
        tone(self.busy, 'attention' if paused else '')
        self.facts.set_facts(facts)
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))


class SettingsPage(Page):
    """The settings, in sections chosen at the top. Every choice saves at once."""

    SECTIONS = [('general', 'General'), ('clients', 'Clients'), ('downloads', 'Downloads'), ('launch', 'Launch'),
                ('diagnostics', 'Diagnostics')]
    CATEGORIES = [(catalog.LIVE, 'Live clients', 'Untick a client to leave it out of the list. '
                   'Hiding a client keeps it installed.'),
                  (catalog.TEST, 'Test clients', 'PTRs and betas. Showing one does not install it. '
                   'They connect to test realms, which need account access.'),
                  (catalog.OTHER, '', 'Other builds Blizzard publishes openly, such as event and submission '
                   'builds. Many are old and may not connect.')]

    # Every note a client row can show, for the width of their column.
    NOTES = ['Installed', 'Downloading', 'Queued', 'Paused', 'Install failed', 'Update failed', 'Needs repair',
             'Repair failed', 'Not in this region', 'Older than live', 'Old build', 'Runs 12.12.12']

    saved = Signal()
    recheck_catalog = Signal()
    closed = Signal()
    choose_folder = Signal(str)
    copy_diagnostics = Signal()

    def __init__(self):
        super().__init__()
        self.settings: Settings | None = None
        self.installed: set[str] = set()
        self.downloading: dict[str, str] = {}
        body_layout = scrolling(self, margins=(PAGE_MARGINS[0], 4, PAGE_MARGINS[2], PAGE_MARGINS[3]))
        self.scroll = self.findChild(QScrollArea)
        # The title, the autosave note, Close and the sections stay above the scrolling cards.
        header = QWidget()
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(PAGE_MARGINS[0], PAGE_MARGINS[1], PAGE_MARGINS[2] + GUTTER, 10)
        self.layout().insertWidget(0, header)
        self.fixed_top(self.layout())
        content = self.centre(column(body_layout))
        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        self.title_label = label('Settings', 'pagetitle')
        title_block.addWidget(self.title_label)
        self.saved_note = label('Changes save automatically.', 'small')
        title_block.addWidget(self.saved_note)
        self._note_timer = QTimer(self)
        self._note_timer.setSingleShot(True)
        self._note_timer.setInterval(1600)
        self._note_timer.timeout.connect(self.update_saved_note)
        title_row.addLayout(title_block)
        title_row.addStretch()
        self.close_button = close = fixed(QPushButton('Close'), 96)
        close.setToolTip('Close the settings (Esc)')
        close.clicked.connect(self.closed.emit)
        title_row.addWidget(close)
        header_column = self.centre(column(header_layout))
        header_column.addLayout(title_row)
        header_column.addSpacing(14)
        self.sections = Segmented()
        self.sections.set_options(self.SECTIONS, 'general')
        self.sections.changed.connect(self.show_section)
        header_column.addWidget(self.sections, 0, Qt.AlignLeft)
        self.on_escape(self.closed.emit)
        self.section_layouts: dict[str, QVBoxLayout] = {}
        self.section_widgets: dict[str, QWidget] = {}
        for name, _ in self.SECTIONS:
            widget = QWidget()
            layout = QVBoxLayout(widget)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
            content.addWidget(widget)
            self.section_widgets[name], self.section_layouts[name] = widget, layout

        # General
        general = self.section_layouts['general']
        self.root = fixed(FolderField())
        # Changed only through Choose…, so a half-typed path never becomes the folder.
        self.root.setReadOnly(True)
        self.root.setFocusPolicy(Qt.NoFocus)
        self.root.setPlaceholderText('No folder chosen')
        self.root.setCursor(Qt.PointingHandCursor)
        self.root.setToolTip('Click to choose another folder')
        self.root.clicked.connect(lambda: self.browse.isEnabled() and self.choose_root())
        self.browse = fixed(QPushButton('Choose…'))
        self.browse.clicked.connect(self.choose_root)
        self.folder_locked = ''
        folder = QHBoxLayout()
        folder.setSpacing(SPACING)
        folder.addWidget(self.root, 1)
        folder.addWidget(self.browse)
        self.free = label('', 'small', wrap=True)
        grid = self.card(general, 'Installation folder',
                         'All clients share this folder. Battle.net must not manage it.')
        self.row(grid, 'Folder', folder)
        grid.addWidget(self.free, grid.rowCount(), 1)
        self.region = fixed(QComboBox())
        for code, name in REGIONS.items():
            self.region.addItem(name, code)
        self.locale = fixed(QComboBox())
        for code, name in LOCALE_NAMES.items():
            self.locale.addItem(name, code)
        grid = self.card(general, 'Region and language',
                         'They apply to new installs. Installed clients keep their own.')
        self.row(grid, 'Region', self.region)
        self.row(grid, 'Language', self.locale)
        grid = self.card(general, 'Notifications and tray',
                         'Notifications say when a download finishes or fails, when a client has a new '
                         'version, and when a game quits unexpectedly.')
        self.show_notifications = QCheckBox('Show notifications')
        self.keep_in_tray = QCheckBox('Keep running in the tray when the window is closed')
        self.keep_in_tray.setToolTip('Downloads keep running. Quit Waystone from the tray icon\'s menu.')
        # Without a tray, a hidden window could not come back, so closing it always quits.
        self.tray_note = label('This desktop has no system tray, so closing the window quits Waystone.',
                               'small', wrap=True)
        self.tray_note.hide()
        for box in [self.show_notifications, self.keep_in_tray]:
            grid.addWidget(box, grid.rowCount(), 0, 1, 3)
            box.toggled.connect(self.apply)
        grid.addWidget(self.tray_note, grid.rowCount(), 0, 1, 3)

        # Clients
        clients = self.section_layouts['clients']
        # Only when the last check failed. The rows then show what an earlier check found.
        self.catalog_row = QWidget()
        row = QHBoxLayout(self.catalog_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.catalog_note = label('', 'caution', wrap=True)
        self.catalog_retry = fixed(QPushButton('Check again'), 120)
        self.catalog_retry.clicked.connect(self.recheck_catalog.emit)
        row.addWidget(self.catalog_note, 1)
        row.addWidget(self.catalog_retry)
        self.catalog_row.hide()
        clients.addWidget(self.catalog_row)
        self.choices: dict[str, dict] = {}
        self.category_grids: dict[str, QGridLayout] = {}
        for category, title, note in self.CATEGORIES:
            if category == catalog.OTHER:
                # Off by default. The card is only its switch until it is turned on.
                self.show_other = QCheckBox('Show other public clients')
                self.show_other.setObjectName('cardtitle')
                self.show_other.toggled.connect(self.apply)
                self.show_other.toggled.connect(lambda _: self.update_other())
                grid = self.card(clients, self.show_other, width=ROW_WIDTH)
                card_layout = self.show_other.parentWidget().layout()
                self.other_note = label(note, 'small', wrap=True)
                card_layout.insertWidget(1, self.other_note)
                self.other_status = label('', 'small', wrap=True)
                card_layout.addWidget(self.other_status)
            else:
                grid = self.card(clients, title, note, width=ROW_WIDTH)
            grid.setVerticalSpacing(6)
            grid.setColumnStretch(0, 1)
            self.category_grids[category] = grid

        # Downloads
        self.limit = speed_limit_box()
        self.downloads_grid = self.card(self.section_layouts['downloads'], 'Downloads',
                                        'Downloads run one at a time. A paused one keeps what it stored.')
        self.row(self.downloads_grid, 'Speed limit', self.limit)
        self.download_fields: list[QWidget] = []
        self.workers = fixed(QSpinBox())
        self.workers.setRange(1, 64)
        number_margins(self.workers)
        self.workers.setKeyboardTracking(False)
        self.add_download_row('Connections', self.workers)
        self.downloads_grid.addWidget(label('Applies from the next download.', 'small', wrap=True),
                                      self.downloads_grid.rowCount(), 1, 1, 2)
        self.pause_while_playing = QCheckBox('Pause downloads while a game is running')
        self.resume_downloads = QCheckBox('Resume unfinished downloads when Waystone starts')
        for box in [self.pause_while_playing, self.resume_downloads]:
            self.downloads_grid.addWidget(box, self.downloads_grid.rowCount(), 0, 1, 3)
            self.download_fields.append(box)
            box.toggled.connect(self.apply)
        self.limit.valueChanged.connect(self.apply)
        self.workers.valueChanged.connect(self.apply)
        for box in (self.limit, self.workers):
            box.setFixedWidth(160)          # a number, not a line of text
            box.parentWidget().layout().addStretch(1)

        # Launch
        launch = self.section_layouts['launch']
        self.launch_widgets: list[QWidget] = []
        # On Linux the game runs in Proton, with the options around it.
        self.launch = self.proton = None
        if sys.platform != 'win32':
            self.launch = LaunchSettings()
            self.launch.changed.connect(self.apply)
            self.launch.edited.connect(self.update_saved_note)
            self.proton = self.launch.proton
            grid = self.card(launch, 'Linux', 'How the game runs on Linux, for every client. '
                             'Each client adds its own under ⋯ and Launch options.')
            grid.addWidget(self.launch, 0, 0)
            grid.setColumnStretch(0, 1)
        self.launch_empty = label('The game starts directly on Windows. Each client has its own '
                                  'launch options under ⋯.', 'muted', wrap=True)
        self.launch_empty.setVisible(self.launch is None)
        launch.addWidget(self.launch_empty)

        # Diagnostics
        grid = self.card(self.section_layouts['diagnostics'], 'Diagnostics',
                         'For a bug report: versions, the system, Proton and umu-run, these settings, the installed '
                         'clients, the downloads and the end of the Proton log. Your sign-in, account names and '
                         'ids, home folder and user name are left out.')
        self.copy_button = fixed(QPushButton('Copy diagnostics'))
        self.copy_button.setToolTip('Copies the report to the clipboard, ready to paste into an issue')
        self.copy_button.clicked.connect(self.request_diagnostics)
        self.collecting = False
        copy_row = QHBoxLayout()
        copy_row.addWidget(self.copy_button)
        copy_row.addStretch()
        self.row(grid, 'Report', copy_row)
        # Proton writes the only log, so Windows has none to open.
        self.log_buttons: list[QPushButton] = []
        if sys.platform != 'win32':
            self.open_log = fixed(QPushButton('Open Proton log'))
            self.open_log.clicked.connect(lambda: self.reveal(log_dir() / LOG_NAME))
            self.open_logs = fixed(QPushButton('Open logs folder'))
            self.open_logs.clicked.connect(lambda: self.reveal(log_dir()))
            self.log_buttons = [self.open_log, self.open_logs]
            logs = QHBoxLayout()
            logs.setSpacing(SPACING)
            for button in self.log_buttons:
                logs.addWidget(button)
            logs.addStretch()
            self.row(grid, 'Logs', logs)
            # A disabled button takes no focus, so its reason is written out and not only a tooltip.
            self.log_note = label('', 'small', wrap=True)
            grid.addWidget(self.log_note, grid.rowCount(), 1)

        for layout in self.section_layouts.values():
            layout.addStretch()
        body_layout.addStretch()
        self.section = ''
        self.show_section('general')

        self._loading = False
        for widget in [self.region, self.locale]:
            # Long entries such as Proton build names must not widen the page past a small window.
            widget.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            widget.setMinimumContentsLength(5)
            widget.currentIndexChanged.connect(self.apply)
        self.root.textChanged.connect(self.update_free)
        self.set_clients(catalog.clients())

    # Sections

    def show_section(self, name: str) -> None:
        if name == self.section:
            return
        self.section = name
        self.sections.set_options(self.SECTIONS, name)
        for key, widget in self.section_widgets.items():
            widget.setVisible(key == name)
        self.scroll.verticalScrollBar().setValue(0)
        if name == 'diagnostics':
            self.update_logs()          # a game may have written the log since
        window = self.window()
        if hasattr(window, 'update_tab_order'):
            window.update_tab_order()

    def add_launch_widget(self, widget: QWidget) -> None:
        """The hook for launch options built elsewhere. They appear below the Proton card."""
        layout = self.section_layouts['launch']
        layout.insertWidget(layout.count() - 1, widget)
        self.launch_widgets.append(widget)
        self.launch_empty.hide()

    def add_download_row(self, name: str, field: QWidget) -> None:
        """A further download setting, such as the number of connections."""
        self.row(self.downloads_grid, name, field)
        self.download_fields.append(field)

    @staticmethod
    def card(parent: QVBoxLayout, title: str | QWidget, note: str = '', *, width: int = 0) -> QGridLayout:
        frame = QFrame()
        frame.setObjectName('card')
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 12, 18, 14)
        layout.setSpacing(4)
        layout.addWidget(title if isinstance(title, QWidget) else label(title, 'cardtitle'))
        if note:
            layout.addWidget(label(note, 'small', wrap=True))
        layout.addSpacing(4)
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)
        if width:
            # Rows end at this width in a wide window, so a version stays near its name.
            holder = QWidget()
            holder.setMaximumWidth(width)
            holder.setLayout(grid)
            grid.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(holder)
        else:
            layout.addLayout(grid)
        parent.addWidget(frame)
        parent.addSpacing(8)
        return grid

    @staticmethod
    def row(grid: QGridLayout, name: str, field) -> None:
        index = grid.rowCount()
        key = label(name, 'key')
        key.setFixedWidth(96)
        grid.addWidget(key, index, 0, Qt.AlignVCenter)
        # Fields stop at a readable width instead of spanning a wide window.
        holder = QWidget()
        holder.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        holder.setMaximumWidth(FIELD_WIDTH)
        inner = QHBoxLayout(holder)
        inner.setContentsMargins(0, 0, 0, 0)
        if isinstance(field, QWidget):
            inner.addWidget(field)
        else:
            inner.addLayout(field)
        grid.addWidget(holder, index, 1)
        grid.setColumnStretch(1, 1)
        # Room past the capped fields goes to an empty last column, not to the labels.
        grid.addItem(QSpacerItem(0, 0, QSizePolicy.Expanding, QSizePolicy.Minimum), index, 2)

    def focus_chain(self) -> list[QWidget]:
        """The page's controls in reading order, for the window's tab order."""
        chain = [self.close_button, *self.sections.group.buttons()]
        if self.section == 'general':
            chain += [self.browse, self.region, self.locale, self.show_notifications, self.keep_in_tray]
        elif self.section == 'clients':
            for category, _, _ in self.CATEGORIES:
                if category == catalog.OTHER:
                    chain.append(self.show_other)
                    if not self.show_other.isChecked():
                        break
                chain += [c['box'] for c in self.choices.values() if c['category'] == category]
        elif self.section == 'downloads':
            chain += [self.limit, *self.download_fields]
        elif self.section == 'diagnostics':
            chain += [self.copy_button, *self.log_buttons]
        else:
            chain += self.launch.focus_chain() if self.launch else []
            for widget in self.launch_widgets:
                chain += widget.focus_chain() if hasattr(widget, 'focus_chain') else [widget]
        return chain

    # Clients

    def set_clients(self, clients: list[catalog.Client]) -> None:
        """Rows for these clients, one per client. Rows of clients still offered are kept."""
        codes = [c.code for c in clients]
        if codes == list(self.choices):
            # The same rows, with versions and notes that can still have changed.
            self.lock_clients(self.installed, self.downloading)
            return
        for code in [c for c in self.choices if c not in codes]:
            for widget in self.choices.pop(code)['widgets']:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        for client in clients:
            if client.code not in self.choices:
                box = QCheckBox(client.label)
                box.toggled.connect(self.apply)
                version, note = label('', 'small'), label('', 'small')
                # Fixed columns, so versions and notes line up from card to card.
                version.ensurePolished()
                metrics = version.fontMetrics()
                version.setFixedWidth(metrics.horizontalAdvance('12.12.12.888888'))
                note.setFixedWidth(max(metrics.horizontalAdvance(text) for text in self.NOTES))
                self.choices[client.code] = {'box': box, 'version': version, 'note': note,
                                             'category': client.category, 'widgets': [box, version, note]}
        # Laid out again in catalog order, so a new row lands where it belongs.
        for category, grid in self.category_grids.items():
            rows = [self.choices[c] for c in codes if self.choices[c]['category'] == category]
            for row, choice in enumerate(rows):
                for column, widget in enumerate(choice['widgets']):
                    grid.removeWidget(widget)
                    grid.addWidget(widget, row, column)
        self.choices = {code: self.choices[code] for code in codes}
        self.lock_clients(self.installed, self.downloading)
        self.update_other()

    def update_other(self) -> None:
        others = [c for c in self.choices.values() if c['category'] == catalog.OTHER]
        shown = self.show_other.isChecked()
        # Collapsed while off. Its choices stay for when it is turned on again.
        for choice in others:
            for widget in choice['widgets']:
                widget.setVisible(shown)
        self.other_note.setVisible(shown)
        self.other_status.setVisible(shown and bool(self.other_status.text()))
        window = self.window()
        if hasattr(window, 'update_tab_order'):
            window.update_tab_order()

    def show_catalog_state(self, state: str, checked_at: int = 0) -> None:
        """What the version service said: checking, offline, stale (an earlier answer) or ready."""
        others = any(c['category'] == catalog.OTHER for c in self.choices.values())
        when = datetime.fromtimestamp(checked_at).strftime('%Y-%m-%d %H:%M') if checked_at else ''
        self.catalog_note.setText(
            f'Blizzard\'s version service could not be reached. Versions and clients are from the check on {when}.'
            if state == 'stale' else
            'Blizzard\'s version service could not be reached, so only the built-in clients are offered.'
            if state == 'offline' else '')
        self.catalog_row.setVisible(state in ('stale', 'offline'))
        self.other_status.setText('' if others else {
            'checking': 'Looking for other public clients…',
            'offline': 'Blizzard\'s version service could not be reached, so only the built-in clients are offered.',
        }.get(state, 'Blizzard publishes no other public client right now.'))
        self.update_other()

    def listed(self, code: str) -> bool:
        """Whether a client is in the list by the user's own choice."""
        s = self.settings
        if s is None or code in s.hidden_clients:
            return False
        return not catalog.get(code).optional or code in s.extra_clients or code in self.installed

    def lock_clients(self, installed, downloading=None) -> None:
        """A client with a download stays in the list, so its box is locked on."""
        self.installed, self.downloading = set(installed), dict(downloading or {})
        self._loading, was_loading = True, self._loading
        for code, choice in self.choices.items():
            box, kept = choice['box'], code in self.downloading
            box.setChecked(kept or self.listed(code))
            box.setEnabled(not kept)
            box.setToolTip(f'{self.downloading[code]}, so it stays in the list.' if kept else '')
            release = catalog.release(code)
            choice['version'].setText(release.version if release else '')
            client = catalog.get(code)
            note = (('Installed' if code in self.installed else '') or self.downloading.get(code, '')
                    or ('Not in this region' if catalog.checked() and not release else '')
                    or (('Older than live' if client.test else 'Old build') if catalog.stale(code) else '')
                    or (f'Runs {catalog.other_game(code)}' if catalog.other_game(code) else ''))
            choice['note'].setText(note)
        self._loading = was_loading

    # Values

    def values(self) -> dict:
        return {'root': self.root.text().strip(), 'region': self.region.currentData(),
                'locale': self.locale.currentData(), 'limit': self.limit.value(),
                'show_other': self.show_other.isChecked()}

    def load(self, settings: Settings, installed: list[str], downloading=None) -> None:
        self._loading = True
        self.settings = settings
        self.root.setText(settings.root)
        self.root.setCursorPosition(0)
        self.region.setCurrentIndex(max(0, self.region.findData(settings.region)))
        self.locale.setCurrentIndex(max(0, self.locale.findData(settings.locale)))
        self.show_other.setChecked(settings.show_other_clients)
        self.lock_clients(installed, downloading)
        self.limit.setValue(settings.bandwidth_limit // MEGABYTE)
        self.workers.setValue(settings.workers)
        self.pause_while_playing.setChecked(settings.pause_while_playing)
        self.resume_downloads.setChecked(settings.resume_downloads)
        self.show_notifications.setChecked(settings.show_notifications)
        self.keep_in_tray.setChecked(settings.keep_in_tray and self.keep_in_tray.isEnabled())
        if self.launch is not None:
            self.launch.load(settings)
        self._loading = False
        self.update_free()
        self.update_other()
        self.update_logs()

    def set_tray_available(self, available: bool) -> None:
        """Without a system tray the setting is greyed out and shown off. Its saved choice stays."""
        self.keep_in_tray.setEnabled(available)
        self.tray_note.setVisible(not available)
        if self.settings is not None:
            self._loading, was_loading = True, self._loading
            self.keep_in_tray.setChecked(available and self.settings.keep_in_tray)
            self._loading = was_loading

    def lock_folder(self, reason: str) -> None:
        """Downloads write into the current folder, so it stays until they are done."""
        self.folder_locked = reason
        self.browse.setEnabled(not reason)
        self.browse.setToolTip(reason or 'Choosing another folder does not move installed clients.')
        self.root.setCursor(Qt.ArrowCursor if reason else Qt.PointingHandCursor)
        self.root.setToolTip(reason or 'Click to choose another folder')
        self.root.setProperty('locked', bool(reason))
        self.root.style().unpolish(self.root)
        self.root.style().polish(self.root)
        self.update_free()

    def update_free(self, *_):
        text = self.root.text().strip()
        root = Path(text) if text else None
        probe = next((p for p in [root, *(root.parents if root else [])] if p and p.exists()), None)
        parts = [f'{size_text(shutil.disk_usage(probe).free)} free on this drive'] if probe else []
        if self.folder_locked:
            parts.append(self.folder_locked)
        self.free.setText(' · '.join(parts))
        self.free.setVisible(bool(parts))

    def choose_root(self):
        self.choose_folder.emit(self.root.text() or str(Path.home()))

    def set_root(self, folder: str):
        self.root.setText(folder)
        self.root.setCursorPosition(0)
        self.apply()

    def apply(self, *_):
        if self._loading or self.settings is None:
            return
        values, s = self.values(), self.settings
        # Without a folder yet, the other choices still save. The folder comes from Choose….
        s.root = values['root'] or s.root
        s.region, s.locale = values['region'], values['locale']
        s.bandwidth_limit = int(values['limit']) * MEGABYTE
        s.workers = self.workers.value()
        s.pause_while_playing = self.pause_while_playing.isChecked()
        s.resume_downloads = self.resume_downloads.isChecked()
        s.show_notifications = self.show_notifications.isChecked()
        if self.keep_in_tray.isEnabled():
            s.keep_in_tray = self.keep_in_tray.isChecked()
        if self.launch is not None:
            self.launch.apply(s)      # the Proton build and the Linux launch options
        s.show_other_clients = values['show_other']
        # Only a box the user changed counts. A locked box shows the client for its download,
        # and the user's own choice for it stays.
        for code, choice in self.choices.items():
            wanted = choice['box'].isChecked()
            if choice['box'].isEnabled() and wanted != self.listed(code):
                (s.listed if wanted else s.unlisted)(code)
        s.save()
        self.saved_note.setText('Saved.')
        self._note_timer.start(1600)        # restarted by each save, so the note never flips mid-typing
        self.saved.emit()

    # Diagnostics

    def update_logs(self) -> None:
        """Each log button works only once there is something to open."""
        if not self.log_buttons:
            return
        folder = log_dir()
        try:
            has_log, has_folder = (folder / LOG_NAME).is_file(), folder.is_dir()
        except OSError:         # an unreadable folder before Python 3.13 raises instead of saying no
            has_log = has_folder = False
        reason = '' if has_log else 'No Proton log yet. Turn on Proton log under Launch, then start a game.'
        self.open_log.setEnabled(not reason)
        self.open_log.setToolTip(reason or f'Opens {LOG_NAME}')
        self.open_logs.setEnabled(has_folder)
        self.open_logs.setToolTip('' if has_folder else 'Waystone has written no logs yet.')
        self.log_note.setText(reason)
        self.log_note.setVisible(bool(reason))

    def reveal(self, path: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def request_diagnostics(self) -> None:
        # The button stays enabled while the report is collected, so keyboard focus stays on it.
        if not self.collecting:
            self.collecting = True
            self.copy_button.setText('Collecting…')
            self.copy_diagnostics.emit()

    def diagnostics_copied(self, copied: bool) -> None:
        self.collecting = False
        self.copy_button.setText('Copy diagnostics')
        self.saved_note.setText('Diagnostics copied. Paste them into your bug report.' if copied
                                else 'The diagnostics could not be collected.')
        self._note_timer.start(5000)

    def update_saved_note(self, *_) -> None:
        unsaved = self.launch is not None and self.launch.unsaved()
        self.saved_note.setText('A field under Launch is not saved until it is fixed.' if unsaved
                                else 'Changes save automatically.')


class SignInPage(Page):
    """Waits for the address Battle.net's sign-in page redirects to."""

    finished = Signal(object)            # the ticket bytes, or None when cancelled

    def __init__(self):
        super().__init__()
        self.on_escape(lambda: self.finish(None))
        self.url = ''
        self.active = False
        self._before = ''
        outer = scrolling(self)
        self.balance(outer)
        content = self.centre(column(outer))
        content.addWidget(label('Sign in to Battle.net', 'pagetitle'))
        content.addSpacing(6)
        content.addWidget(label('Your browser opened Battle.net\'s own sign-in page. '
                                'Waystone never sees your password.', 'muted', wrap=True))
        content.addSpacing(20)
        steps = QFrame()
        steps.setObjectName('card')
        steps_layout = QVBoxLayout(steps)
        steps_layout.setContentsMargins(20, 16, 20, 16)
        steps_layout.setSpacing(10)
        for number, text in enumerate([
                'Sign in on the page in your browser.',
                'The browser then fails to open a page at http://localhost:0/?ST=…',
                'Copy that whole address from the address bar and come back here.'], start=1):
            row = QHBoxLayout()
            row.setSpacing(12)
            badge = label(str(number), 'tag')
            badge.setFixedWidth(22)
            badge.setAlignment(Qt.AlignCenter)
            row.addWidget(badge, 0, Qt.AlignTop)
            row.addWidget(label(text, wrap=True), 1)
            steps_layout.addLayout(row)
        content.addWidget(steps)
        content.addSpacing(18)
        content.addWidget(label('Address from the browser', 'key'))
        content.addSpacing(6)
        entry = QHBoxLayout()
        entry.setSpacing(SPACING)
        self.field = QLineEdit()
        self.field.setFixedHeight(PRIMARY_HEIGHT)          # level with the Continue button beside it
        self.field.setMaximumWidth(FIELD_WIDTH + 120)
        self.field.setEchoMode(QLineEdit.Password)
        self.field.setPlaceholderText('Paste the address here')
        self.field.returnPressed.connect(lambda: self.take(self.field.text(), explicit=True))
        submit = QPushButton('Continue')
        submit.setObjectName('primary')
        submit.setDefault(True)
        submit.setFixedWidth(132)
        submit.setEnabled(False)
        self.field.textChanged.connect(lambda text: submit.setEnabled(bool(text.strip())))
        submit.clicked.connect(lambda: self.take(self.field.text(), explicit=True))
        entry.addWidget(self.field, 1)
        entry.addWidget(submit)
        entry.addStretch()
        content.addLayout(entry)
        content.addSpacing(12)
        self.status = label('Waiting for the address.', 'small', wrap=True)
        content.addWidget(self.status)
        content.addSpacing(24)
        buttons = QHBoxLayout()
        reopen = fixed(QPushButton('Open the sign-in page again'))
        reopen.clicked.connect(lambda: open_browser(self.url))
        cancel = fixed(QPushButton('Cancel'), 96)
        cancel.clicked.connect(lambda: self.finish(None))
        buttons.addWidget(reopen)
        buttons.addStretch()
        buttons.addWidget(cancel)
        content.addLayout(buttons)
        outer.addStretch(1)
        QApplication.clipboard().dataChanged.connect(self.check_clipboard)
        QApplication.instance().applicationStateChanged.connect(lambda _: self.check_clipboard())

    def start(self, url: str):
        self.url, self.active = url, True
        self.field.clear()
        self.status.setText('Waiting for the address.')
        self._before = QApplication.clipboard().text()
        open_browser(url)

    def check_clipboard(self):
        # Wayland shares the clipboard only with the focused window, so this
        # also runs when Waystone becomes active again.
        if not self.active:
            return
        text = QApplication.clipboard().text()
        if text != self._before and looks_like_redirect(text):
            self.take(text)

    def take(self, text: str, explicit: bool = False):
        if not self.active:
            return
        try:
            ticket = parse_ticket(text)
        except ValueError as error:
            if explicit:
                self.status.setText(str(error))
            return
        self.finish(ticket)
        if looks_like_redirect(QApplication.clipboard().text()):
            # The ticket works only once, but do not leave it on the clipboard.
            # Clearing triggers dataChanged, which finish() already disarmed.
            QTimer.singleShot(0, QApplication.clipboard().clear)

    def finish(self, ticket):
        if not self.active:
            return
        self.active = False
        self.field.clear()
        self.finished.emit(ticket)


class FolderPage(Page):
    """Choose a folder inside the window. Tiling compositors place dialogs unpredictably.

    By default it chooses the installation folder. With existing_only it picks any existing
    folder and offers no New folder, as for an installation to import, which is never written to.
    """

    chosen = Signal(str)
    cancelled = Signal()

    def __init__(self, title: str = 'Choose the installation folder',
                 note: str = 'Pick an empty folder for a new installation, or the folder Waystone already uses.',
                 *, existing_only: bool = False):
        super().__init__()
        self.existing_only = existing_only
        self.on_escape(self.cancelled.emit)
        page = QVBoxLayout(self)
        left, top, right, bottom = PAGE_MARGINS
        page.setContentsMargins(left, top, right + GUTTER, bottom)
        outer = self.centre(column(page))
        page.setStretch(0, 1)
        self.fixed_top(page)
        outer.setSpacing(10)
        self._target = ''
        outer.addWidget(label(title, 'pagetitle'))
        outer.addWidget(label(note, 'muted', wrap=True))
        self.path = fixed(QLineEdit())
        self.path.setPlaceholderText('Type a path and press Enter, or pick a folder below')
        self.path.returnPressed.connect(self.go_to_typed)
        self.path.textEdited.connect(self._release)     # a path being typed is never replaced
        self.path.textEdited.connect(lambda _: self.say(''))   # an old complaint no longer applies
        outer.addWidget(self.path)
        self.model = QFileSystemModel(self)
        self.model.setFilter(QDir.AllDirs | QDir.NoDotAndDotDot | QDir.Drives)
        self.model.setRootPath('')
        # Folders load and sort in the background, so the current folder is revealed again
        # whenever rows arrive, until the user takes over.
        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.setInterval(40)
        self._reveal_timer.timeout.connect(self._reveal)
        for signal in (self.model.directoryLoaded, self.model.rowsInserted, self.model.layoutChanged):
            signal.connect(lambda *_: self._target and self._reveal_timer.start())
        self.tree = QTreeView()
        self.tree.setModel(self.model)
        for index in range(1, self.model.columnCount()):
            self.tree.hideColumn(index)
        self.tree.setHeaderHidden(True)
        self.tree.setObjectName('folders')
        self.tree.pressed.connect(self._release)
        self.tree.verticalScrollBar().sliderPressed.connect(self._release)
        self.tree.viewport().installEventFilter(self)
        self.tree.installEventFilter(self)
        self.tree.selectionModel().currentChanged.connect(
            lambda index, _: self.path.setText(self.model.filePath(index)))
        outer.addWidget(self.tree, 1)
        self.problem = label('', 'small', wrap=True)
        self.problem.hide()             # shown with a message, so no gap is reserved for it
        outer.addWidget(self.problem)
        buttons = QHBoxLayout()
        buttons.setSpacing(SPACING)
        create = QPushButton('New folder')
        create.setFixedHeight(PRIMARY_HEIGHT)
        create.clicked.connect(self.new_folder)
        create.setVisible(not existing_only)
        cancel = QPushButton('Cancel')
        cancel.setFixedSize(96, PRIMARY_HEIGHT)          # level with Use this folder
        cancel.clicked.connect(self.cancelled.emit)
        choose = QPushButton('Use this folder')
        choose.setObjectName('primary')
        choose.clicked.connect(self.choose)
        buttons.addWidget(create)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(choose)
        outer.addLayout(buttons)

    def start(self, folder: str):
        self.say('')
        self.show_path(Path(folder).expanduser())

    def show_path(self, path: Path):
        existing = next((p for p in [path, *path.parents] if p.is_dir()), Path.home())
        self._target, self._typed = str(existing), str(path)
        self._reveal()
        QTimer.singleShot(5000, self._release)

    def _reveal(self):
        if not self._target:
            return
        index = self.model.index(self._target)
        self.tree.setCurrentIndex(index)
        self.tree.scrollTo(index, QAbstractItemView.PositionAtCenter)
        self.tree.expand(index)
        self.path.setText(self._typed)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # After the tree has its new height, so the selected folder stays in view.
        QTimer.singleShot(0, lambda: self.tree.currentIndex().isValid() and
                          self.tree.scrollTo(self.tree.currentIndex(), QAbstractItemView.EnsureVisible))

    def _release(self, *_):
        self._target = ''            # the user's own scrolling, clicks and typing are left alone
        self._reveal_timer.stop()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Wheel, QEvent.MouseButtonPress, QEvent.KeyPress):
            self._release()
        return super().eventFilter(watched, event)

    def say(self, text: str) -> None:
        self.problem.setText(text)
        self.problem.setVisible(bool(text))

    def go_to_typed(self):
        self.say('')
        self.show_path(Path(self.path.text()).expanduser())

    def new_folder(self):
        if self.existing_only:
            return
        parent = Path(self.path.text()).expanduser()
        if not parent.is_dir():
            self.say('Choose an existing folder to create the new one in.')
            return
        name, n = 'Waystone', 1
        while (parent / name).exists():
            n += 1
            name = f'Waystone {n}'
        try:
            (parent / name).mkdir()
        except OSError as error:
            self.say(f'Could not create the folder: {error.strerror}')
            return
        self.show_path(parent / name)

    def choose(self):
        path = Path(self.path.text()).expanduser()
        if not path.is_absolute():
            self.say('Enter a full path, such as /games/Waystone.')
        elif self.existing_only:
            # A source can sit on a drive that stalls, so a worker checks it, not the window.
            self.chosen.emit(str(path))
        elif path.exists() and not path.is_dir():
            self.say('This is a file, not a folder.')
        elif path.is_dir() and any(path.iterdir()) and not (path / '.waystone').is_dir():
            self.say('This folder is not empty. Choose an empty folder, '
                                 'create one with New folder, or pick the folder Waystone already uses.')
        else:
            self.chosen.emit(str(path))


class ImportPage(Page):
    """Import an installation the user already has, such as Battle.net's, by seeding installs from it.

    The window finds the candidates on a worker thread and adds them as they arrive.
    """
    confirmed = Signal()
    cancelled = Signal()
    choose_folder = Signal()

    def __init__(self):
        super().__init__()
        self.on_escape(self.cancelled.emit)
        self.code: str | None = None
        self.found: list = []
        self.blocked: dict[str, str] = {}        # client -> why it cannot be imported now
        outer = scrolling(self)
        self.balance(outer)
        content = self.centre(column(outer))
        content.setSpacing(10)
        self.title = label('', 'pagetitle', wrap=True)
        content.addWidget(self.title)
        content.addWidget(label('Waystone copies the game content from an installation you already have, such as '
                                'Battle.net\'s, and downloads only what it lacks. It only reads that installation '
                                'and never changes, moves or deletes anything in it, so Battle.net can keep using it.',
                                'muted', wrap=True))
        content.addSpacing(6)
        content.addWidget(label('FOUND INSTALLATIONS', 'group'))
        self.status = label('', 'small', wrap=True)
        content.addWidget(self.status)
        self.cards = QVBoxLayout()
        self.cards.setSpacing(8)
        content.addLayout(self.cards)
        self.group = QButtonGroup(self)
        self.group.buttonToggled.connect(lambda *_: self.show_clients())
        choose = QHBoxLayout()
        choose.setSpacing(12)
        self.choose = fixed(QPushButton('Choose folder…'))
        self.choose.setToolTip('Pick the installation\'s folder, such as World of Warcraft')
        self.choose.clicked.connect(self.choose_folder.emit)
        choose.addWidget(self.choose)
        choose.addWidget(label('Not listed? Choose the folder that holds .build.info and Data.', 'small', wrap=True), 1)
        content.addLayout(choose)
        content.addSpacing(6)
        content.addWidget(label('CLIENTS TO IMPORT', 'group'))
        self.client_rows = QVBoxLayout()
        self.client_rows.setSpacing(6)
        content.addLayout(self.client_rows)
        self.none = label('Choose an installation first.', 'small')
        content.addWidget(self.none)
        self.boxes: dict[str, QCheckBox] = {}
        content.addSpacing(6)
        self.copy = QCheckBox('Copy settings and addons (recommended)')
        content.addWidget(self.copy)
        self.copy_note = label('Once each install has succeeded, copies the WTF and Interface folders, with your '
                               'settings, keybinds, macros and addons, into the new client folder. Files already '
                               'there are kept.', 'small', wrap=True)
        self.copy_note.setContentsMargins(28, 0, 0, 0)        # under the checkbox's text
        content.addWidget(self.copy_note)
        self.problem = label('', 'problem', wrap=True)
        content.addWidget(self.problem)
        actions = QHBoxLayout()
        actions.setSpacing(SPACING)
        self.cancel = QPushButton('Cancel')
        self.cancel.setFixedSize(96, PRIMARY_HEIGHT)      # level with Import
        self.confirm = QPushButton('Import')
        self.confirm.setObjectName('primary')
        self.cancel.clicked.connect(self.cancelled.emit)
        self.confirm.clicked.connect(self.confirmed.emit)
        actions.addWidget(self.cancel)
        actions.addWidget(self.confirm)
        actions.addStretch()
        content.addSpacing(4)
        content.addLayout(actions)
        outer.addStretch(1)

    def start(self, code: str, searching: str) -> None:
        self.code = code
        self.title.setText(f'Import {catalog.get(code).label}')
        for button in self.group.buttons():
            self.group.removeButton(button)
        while self.cards.count():
            widget = self.cards.takeAt(0).widget()
            widget.hide()
            widget.deleteLater()
        self.found = []
        self.copy.setChecked(True)
        self.set_status(searching)
        self.set_reason('')
        self.show_clients()
        self.cancel.setFocus()

    def set_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def add(self, found, *, select: bool = False) -> None:
        """Add an installation, or select it again when it is already listed."""
        known = next((i for i, f in enumerate(self.found) if f.path == found.path), None)
        if known is None:
            self.found.append(found)
            self.cards.addWidget(self._card(found))
            known = len(self.found) - 1
        button = self.group.button(known)
        # The first one that holds the client the page is for is chosen for the user.
        if select or (self.group.checkedButton() is None and self.code in found.clients):
            button.setChecked(True)
        self.show_clients()

    def _card(self, found) -> QFrame:
        frame = QFrame()
        frame.setObjectName('card')
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(2)
        # Where it was found tells two folders named World of Warcraft apart.
        button = QRadioButton(found.where or found.path.name or str(found.path))
        button.setObjectName('cardtitle')
        button.setAccessibleDescription(str(found.path))
        self.group.addButton(button, len(self.found) - 1)
        layout.addWidget(button)
        lines = QVBoxLayout()
        lines.setContentsMargins(26, 0, 0, 0)          # lined up with the radio button's text
        lines.setSpacing(2)
        for text in (str(found.path), ', '.join(catalog.get(code).label for code in found.clients)):
            line = ElidedLabel(text, 'small', Qt.ElideMiddle)
            line.setToolTip(text)
            lines.addWidget(line)
        layout.addLayout(lines)
        return frame

    def selected(self):
        index = self.group.checkedId()
        return self.found[index] if 0 <= index < len(self.found) else None

    def set_blocked(self, blocked: dict[str, str]) -> None:
        if blocked != self.blocked:
            self.blocked = dict(blocked)
            self.show_clients()

    def show_clients(self) -> None:
        """One checkbox for each client of the chosen installation. Kept when they stay the same."""
        found = self.selected()
        codes = list(found.clients) if found else []
        if codes != list(self.boxes):
            checked = {code for code, box in self.boxes.items() if box.isChecked()}
            for box in self.boxes.values():
                self.client_rows.removeWidget(box)
                box.hide()
                box.deleteLater()
            self.boxes = {}
            for code in codes:
                box = QCheckBox(catalog.get(code).label)
                box.setChecked(code in checked or code == self.code)
                box.toggled.connect(self.update_confirm)
                self.client_rows.addWidget(box)
                self.boxes[code] = box
            window = self.window()
            if hasattr(window, 'update_tab_order'):
                window.update_tab_order()
        for code, box in self.boxes.items():
            reason = self.blocked.get(code, '')
            box.setText(f'{catalog.get(code).label} · {reason}' if reason else catalog.get(code).label)
            box.setEnabled(not reason)
            if reason:
                box.setChecked(False)
        self.none.setVisible(not codes)
        self.update_confirm()

    def chosen_clients(self) -> list[str]:
        return [code for code, box in self.boxes.items() if box.isChecked() and box.isEnabled()]

    def update_confirm(self, *_) -> None:
        count = len(self.chosen_clients())
        self.confirm.setText('Import' if count < 2 else f'Import {count} clients')
        self.confirm.setEnabled(count > 0)

    def set_reason(self, reason: str) -> None:
        self.problem.setText(reason)
        self.problem.setVisible(bool(reason))

    def focus_chain(self) -> list[QWidget]:
        return [*self.group.buttons(), self.choose, *self.boxes.values(), self.copy, self.cancel, self.confirm]


class QueuePage(Page):
    """Every queued, running and paused job, with its controls."""

    closed = Signal()
    pause = Signal(str)
    resume = Signal(str)
    cancel = Signal(str)
    limit_changed = Signal(int)
    move = Signal(str, int)
    pause_all = Signal()
    resume_all = Signal()

    def set_limit(self, megabytes: int) -> None:
        self.limit.blockSignals(True)
        self.limit.setValue(megabytes)
        self.limit.blockSignals(False)

    def __init__(self):
        super().__init__()
        self.on_escape(self.closed.emit)
        outer = scrolling(self)
        self.balance(outer)
        content = self.centre(column(outer))
        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        self.title_label = label('Downloads', 'pagetitle')
        title_row.addWidget(self.title_label)
        title_row.addStretch()
        self.close_button = close = fixed(QPushButton('Close'), 96)
        close.setToolTip('Close the downloads (Esc)')
        close.clicked.connect(self.closed.emit)
        title_row.addWidget(close)
        content.addLayout(title_row)
        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.pause_all_button = fixed(QPushButton('Pause all'))
        self.resume_all_button = fixed(QPushButton('Resume all'))
        self.pause_all_button.clicked.connect(self.pause_all.emit)
        self.resume_all_button.clicked.connect(self.resume_all.emit)
        controls.addWidget(self.pause_all_button)
        controls.addWidget(self.resume_all_button)
        controls.addStretch()
        controls.addWidget(label('Speed limit', 'key'))
        self.limit = speed_limit_box()          # the same control as in Settings
        self.limit.valueChanged.connect(self.limit_changed.emit)
        controls.addWidget(self.limit)
        content.addSpacing(10)
        content.addLayout(controls)
        content.addSpacing(10)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(10)
        content.addLayout(self.rows)
        self.empty = label('Nothing is queued.', 'muted')
        content.addWidget(self.empty)
        content.addSpacing(6)
        self.summary = label('', 'small', wrap=True)
        content.addWidget(self.summary)
        outer.addStretch(1)
        self.cards: dict[str, dict] = {}
        self.order: list[str] = []

    def focus_chain(self) -> list[QWidget]:
        """The page's controls in reading order, for the window's tab order."""
        buttons = [self.cards[code][key] for code in self.order for key in ('up', 'down', 'toggle', 'cancel')]
        return [self.close_button, self.pause_all_button, self.resume_all_button, self.limit, *buttons]

    def show_jobs(self, jobs: list, labels: dict) -> None:
        from ..downloads import FAILED, PAUSED, QUEUED, RUNNING
        codes = [job.client for job in jobs]
        if codes != self.order:
            # Cards of jobs that remain are kept and only reordered, so keyboard focus on
            # them survives another job finishing.
            focused = QApplication.focusWidget()
            lost_focus = False
            for code in [c for c in self.cards if c not in codes]:
                frame = self.cards.pop(code)['frame']
                lost_focus = lost_focus or (focused is not None and frame.isAncestorOf(focused))
                self.rows.removeWidget(frame)
                frame.hide()
                frame.setParent(None)
                frame.deleteLater()
            for code in codes:
                if code not in self.cards:
                    self.cards[code] = self._card(code)
            for index, code in enumerate(codes):
                self.rows.removeWidget(self.cards[code]['frame'])
                self.rows.insertWidget(index, self.cards[code]['frame'])
            self.order = codes
            if lost_focus:
                self.close_button.setFocus()
        waiting = 0
        movable = [j for j in jobs if j.status in {QUEUED, PAUSED}]
        for job in jobs:
            card = self.cards[job.client]
            verb = {'install': 'Install', 'update': 'Update', 'verify': 'Verify', 'repair': 'Repair', 'uninstall': 'Uninstall'}[job.kind]
            verb = 'Import' if job.seed_from else verb
            card['title'].setText(f'{verb} {labels[job.client]}')
            if job.status == RUNNING:
                state = 'Running'
            elif job.status == PAUSED:
                state = f'Paused at {job.fraction:.0%}' if job.total_bytes else 'Paused'
            elif job.status == FAILED:
                state = f'Failed · {job.error}'
            else:
                waiting += 1
                state = f'Waiting, number {waiting} in line'
            details = [d for d in progress_variants(job) if d] if job.status in {RUNNING, PAUSED} else []
            if job.status == PAUSED and job.total_bytes:
                details = [d.split(' · ', 1)[1] for d in details if ' · ' in d]
            # The fullest line that fits: the state, then shorter and shorter details.
            card['detail'].set_variants([f'{state} · {d}' for d in details] +
                                        ([] if job.status == PAUSED else details) + [state])
            # A running job without a known size gets the moving bar, as on the client page.
            card['bar'].setVisible(job.status == RUNNING or (bool(job.total_bytes) and job.status != FAILED))
            card['bar'].setRange(0, 0 if job.status == RUNNING and not job.total_bytes else 1000)
            card['bar'].setValue(int(job.fraction * 1000))
            tone(card['bar'], 'attention' if job.status == PAUSED else '')
            # Paused and failed jobs both continue through resume. The manager picks the right kind.
            card['paused'] = job.status in {PAUSED, FAILED}
            retry = 'Repair' if job.kind == 'verify' else 'Try again'
            card['toggle'].setText(retry if job.status == FAILED else 'Resume' if card['paused'] else 'Pause')
            card['cancel'].setText('Remove' if job.status == FAILED else 'Cancel')
            # A waiting job has nothing to pause. Cancel takes it out of the queue.
            card['toggle'].setVisible(job.status != QUEUED)
            removing = job.kind == 'uninstall' and job.status == RUNNING
            card['toggle'].setEnabled(not removing)
            card['cancel'].setEnabled(not removing)
            for key, step in [('up', -1), ('down', 1)]:
                card[key].setVisible(job in movable)
                card[key].setEnabled(job in movable and 0 <= movable.index(job) + step < len(movable))
        # Each only when it would change something.
        self.pause_all_button.setEnabled(any(j.status in {QUEUED, RUNNING} for j in jobs))
        self.resume_all_button.setEnabled(any(j.status == PAUSED for j in jobs))
        self.empty.setVisible(not jobs)
        active = [j for j in jobs if j.status != FAILED]
        left = sum(j.total_bytes - j.done_bytes for j in active if j.total_bytes)
        speed = sum(j.speed for j in active if j.status == RUNNING)
        unsized = sum(1 for j in active if not j.total_bytes)
        parts = [f"{len(active)} {'download' if len(active) == 1 else 'downloads'}"] if active else []
        if left:
            parts.append(f'{size_text(left)} to download')
            # A time only when every size is known, worked out like the card's own.
            if speed > 0 and not unsized:
                parts.append(f'{duration_text(left / speed)} total' + (' if resumed' if any(j.status == PAUSED for j in active) else ''))
        if active and (unsized or speed <= 0):
            parts.append('Total time estimating…' if speed > 0 else 'Total time unknown')
        if unsized:
            parts.append(f'{unsized} not sized yet')
        failed = len(jobs) - len(active)
        if failed:
            parts.append(f'{failed} failed')
        self.summary.setText(' · '.join(parts))
        self.summary.setVisible(bool(parts))

    def _card(self, code: str) -> dict:
        frame = QFrame()
        frame.setObjectName('card')
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(18, 12, 12, 12)
        layout.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(4)
        # Shortened to fit, so a long error never pushes the buttons out of the card.
        title, detail = ElidedLabel('', 'cardtitle'), ElidedLabel('', 'small')
        bar = QProgressBar()
        bar.setTextVisible(False)
        bar.setRange(0, 1000)
        bar.setMinimumWidth(40)
        bar.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        text.addWidget(title)
        text.addWidget(bar)
        text.addWidget(detail)
        layout.addLayout(text, 1)
        up, down = fixed(QPushButton('↑')), fixed(QPushButton('↓'))
        for button, step, name in [(up, -1, 'up'), (down, 1, 'down')]:
            button.setFixedWidth(32)
            button.setObjectName('reorder')
            button.setToolTip(f'Move {catalog.get(code).label} {name}')
            button.setAccessibleName(button.toolTip())
            button.clicked.connect(lambda checked=False, offset=step: self.move.emit(code, offset))
            layout.addWidget(button)
        toggle = fixed(QPushButton('Pause'), 84)
        cancel = fixed(QPushButton('Cancel'), 84)
        card = {'frame': frame, 'title': title, 'detail': detail, 'bar': bar, 'toggle': toggle, 'cancel': cancel,
                'paused': False, 'up': up, 'down': down}
        toggle.clicked.connect(lambda: (self.resume if card['paused'] else self.pause).emit(code))
        cancel.clicked.connect(lambda: self.cancel.emit(code))
        layout.addWidget(toggle)
        layout.addWidget(cancel)
        return card
