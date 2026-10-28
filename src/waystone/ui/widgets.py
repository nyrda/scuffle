"""Reusable widgets of the Waystone window."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QProgressBar, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from .. import catalog
from .style import CONTROL_HEIGHT, FAMILY_NAMES, SPACING, client_color, size_text, tile


def label(text: str = '', name: str | None = None, *, wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def fixed(widget, width: int | None = None):
    """One height for every control, so layouts never depend on style size hints."""
    widget.setFixedHeight(CONTROL_HEIGHT)
    if width:
        widget.setMinimumWidth(width)
    return widget


def tone(widget: QWidget, value: str) -> None:
    """Restyle a widget after changing a property the stylesheet reads."""
    if widget.property('tone') != value:
        widget.setProperty('tone', value)
        widget.style().unpolish(widget)
        widget.style().polish(widget)


# What each stage of an install or update is doing, in the user's words.
STAGES = {
    'indexing_seed': 'Reading installed content…', 'metadata': 'Fetching build information…',
    'discovery': 'Checking the latest version…', 'seeding_cdn_index': 'Fetching download indexes…',
    'cdn_indexes': 'Fetching download indexes…', 'planning': 'Working out what to download…',
    'planned': 'Working out what to download…', 'seeding': 'Reusing installed content…',
    'downloading': 'Downloading…', 'download_complete': 'Writing game files…',
    'materializing': 'Writing game files…', 'cleaning_up': 'Removing unused content…',
    'verifying': 'Checking files…', 'complete': 'Finishing…',
}
# Stages after the transfer, when sizes and time left no longer mean anything.
FINISHING = {'download_complete', 'materializing', 'cleaning_up', 'complete', 'verifying'}


def stage_text(stage: str) -> str:
    return STAGES.get(stage, 'Preparing…')


def progress_variants(job) -> list[str]:
    """A running or paused job's details, from the fullest to the shortest line."""
    from ..downloads import QUEUED, RUNNING
    running = job.status == RUNNING
    if running and (job.stage in FINISHING or not job.total_bytes):
        return [stage_text(job.stage)]
    if job.status == QUEUED:
        return ['Starts when the current download is done', 'Waiting']
    if not job.total_bytes:
        return ['Progress will be checked on resume']
    percent = f'{job.fraction:.0%}'
    sizes = f'{size_text(job.done_bytes)} of {size_text(job.total_bytes)}'
    if running and job.speed <= 0:
        # The first seconds, before the speed settles. The bytes are known, so they stay.
        return [f'{percent} · Estimating time and speed · {sizes}', f'{percent} · Estimating… · {sizes}',
                f'{percent} · {sizes}']
    if running:
        speed = f'{job.speed / 1_000_000:.0f} MB/s' if job.speed >= 10_000_000 else f'{size_text(job.speed)}/s'
        # Narrow windows drop the sizes first, then the speed.
        return [f'{percent} · {time_left(job)} · {speed} · {sizes}', f'{percent} · {time_left(job)} · {speed}',
                f'{percent} · {time_left(job)}']
    if getattr(job, 'held', False):
        return [f'{percent} · Paused while a game runs · {sizes}', f'{percent} · Paused while a game runs', percent]
    return [f'{percent} · {sizes}', percent]


def duration_text(seconds: float) -> str:
    """About how long, rounded to minutes the same way everywhere."""
    minutes = max(1, round(seconds / 60))
    return f'{minutes // 60} h {minutes % 60} min left' if minutes >= 60 else f'{minutes} min left'


def time_left(job) -> str:
    return duration_text((job.total_bytes - job.done_bytes) / job.speed)


class ElidedLabel(QLabel):
    """A one-line label that shortens its text to fit and shows it in full as a tooltip."""

    def __init__(self, text: str = '', name: str | None = None, mode=Qt.ElideRight):
        super().__init__()
        if name:
            self.setObjectName(name)
        self.mode, self.full, self.variants = mode, '', ['']
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(40)
        self.setText(text)

    def setText(self, text: str) -> None:
        self.set_variants([text])

    def set_variants(self, variants: list[str]) -> None:
        """Show the first text that fits, from the fullest to the shortest."""
        self.variants = variants or ['']
        self.full = self.variants[0]
        self._fit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def _fit(self):
        metrics, width = QFontMetrics(self.font()), max(self.width(), 40)
        fitting = [text for text in self.variants if metrics.horizontalAdvance(text) <= width]
        shown = fitting[0] if fitting else metrics.elidedText(self.variants[-1], self.mode, width)
        super().setText(shown)
        self.setToolTip(self.full if shown != self.full else '')


class ClientRow(QWidget):
    def __init__(self, client: catalog.Client):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACING, 5, SPACING, 5)
        layout.setSpacing(10)
        icon = QLabel()
        icon.setPixmap(tile(client.short, client_color(client), 32))
        icon.setFixedSize(32, 32)
        text = QVBoxLayout()
        text.setSpacing(0)
        top = QHBoxLayout()
        top.setSpacing(6)
        name = ElidedLabel(client.label)
        name.setStyleSheet('font-weight: 600;')
        top.addWidget(name, 1)
        if client.optional:
            top.addWidget(label('TEST' if client.test else 'OTHER', 'tag'))
        self.status = ElidedLabel('', 'small')
        text.addLayout(top)
        text.addWidget(self.status)
        layout.addWidget(icon)
        layout.addLayout(text, 1)


class ClientList(QListWidget):
    """Clients grouped by game, with test and other clients under their game."""

    selected = Signal(str)
    open_settings = Signal()

    def __init__(self):
        super().__init__()
        self.rows: dict[str, ClientRow] = {}
        self.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.currentItemChanged.connect(self._changed)
        self.itemClicked.connect(lambda item: self._changed(item, None))
        self.itemActivated.connect(lambda item: self._changed(item, None))
        # Shown in place of the rows when every client is hidden.
        self.empty = QWidget(self)
        hint = QVBoxLayout(self.empty)
        hint.setContentsMargins(SPACING + 2, 14, SPACING, 0)
        hint.setSpacing(SPACING)
        hint.addWidget(label('Every client is hidden from the list.', 'small', wrap=True))
        self.empty_button = fixed(QPushButton('Choose clients'))
        self.empty_button.clicked.connect(self.open_settings.emit)
        hint.addWidget(self.empty_button, 0, Qt.AlignLeft)
        hint.addStretch()
        self.empty.hide()

    # Row and heading heights, from roomy to compact. The roomiest that fits is used.
    DENSITIES = [(54, 32, 12), (50, 30, 10), (44, 28, 8), (40, 24, 4)]

    def populate(self, clients: list[catalog.Client], current: str | None) -> None:
        self.blockSignals(True)
        self.clear()
        self.rows = {}
        self.headings: list[tuple[QListWidgetItem, QLabel]] = []
        for family in FAMILY_NAMES:
            members = [c for c in clients if c.family == family]
            if not members:
                continue
            header = QListWidgetItem()
            header.setFlags(Qt.NoItemFlags)
            self.addItem(header)
            heading = label(FAMILY_NAMES[family].upper(), 'group')
            self.setItemWidget(header, heading)
            self.headings.append((header, heading))
            public = [c for c in members if not c.optional]
            nested = []
            for game in public:
                nested += [game] + [c for c in members if c.optional and c.parent == game.code]
            nested += [c for c in members if c not in nested]
            for client in nested:
                item = QListWidgetItem()
                item.setData(Qt.UserRole, client.code)
                self.addItem(item)
                self.rows[client.code] = ClientRow(client)
                self.setItemWidget(item, self.rows[client.code])
                if client.code == current:
                    self.setCurrentItem(item)
        if self.currentItem() is None and self.rows:
            self.select(next(iter(self.rows)))
        self.empty.setGeometry(self.viewport().geometry())
        self.empty.setVisible(not self.rows)
        self._density = None
        self.fit_rows()
        self.blockSignals(False)
        # After the new rows are laid out, so the selected one really ends up in view.
        QTimer.singleShot(0, self.keep_selection_visible)

    def moveCursor(self, action, modifiers):
        # Headings cannot be selected. A move that lands on one goes on to the next client.
        index = super().moveCursor(action, modifiers)
        step = -1 if action in (QAbstractItemView.MoveUp, QAbstractItemView.MovePrevious) else 1
        row = index.row()
        while 0 <= row < self.count() and not self.item(row).data(Qt.UserRole):
            row += step
        if not 0 <= row < self.count():
            return self.currentIndex()
        return self.model().index(row, 0)

    def keep_selection_visible(self):
        if self.currentItem():
            self.scrollToItem(self.currentItem(), QListWidget.EnsureVisible)

    def fit_rows(self) -> None:
        """Pick row heights by the room there is, so the list scrolls only when it must."""
        rows, headings = len(self.rows), len(getattr(self, 'headings', []))
        room = self.viewport().height()
        density = next((d for d in self.DENSITIES if rows * d[0] + headings * d[1] <= room), self.DENSITIES[-1])
        if density == getattr(self, '_density', None):
            return
        self._density = density
        row_height, heading_height, heading_top = density
        for i in range(self.count()):
            item = self.item(i)
            if item.data(Qt.UserRole):
                item.setSizeHint(QSize(0, row_height))
        for item, heading in self.headings:
            item.setSizeHint(QSize(0, heading_height))
            heading.setContentsMargins(SPACING + 2, heading_top, 0, 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.empty.setGeometry(self.viewport().geometry())
        self.fit_rows()
        if self.currentItem():
            self.scrollToItem(self.currentItem())

    def select(self, code: str) -> None:
        for i in range(self.count()):
            if self.item(i).data(Qt.UserRole) == code:
                self.setCurrentRow(i)
                return

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self._mark_focus(True)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._mark_focus(False)

    def _mark_focus(self, focused: bool) -> None:
        # A property instead of :focus, which Qt applies to the whole list rather than the row.
        self.setProperty('focused', focused)
        self.style().unpolish(self)
        self.style().polish(self)
        self.viewport().update()

    def set_active(self, active: bool) -> None:
        """Dim the selection while a page other than the client page shows."""
        if self.property('inactive') != (not active):
            self.setProperty('inactive', not active)
            self.style().unpolish(self)
            self.style().polish(self)
            self.viewport().update()

    def current(self) -> str | None:
        item = self.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _changed(self, item, _):
        if item is not None and item.data(Qt.UserRole):
            self.selected.emit(item.data(Qt.UserRole))


class Banner(QFrame):
    """An inline message with an optional recovery action, instead of a popup."""

    dismissed = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName('banner')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, SPACING, SPACING, SPACING)
        layout.setSpacing(SPACING)
        self.text = label(wrap=True)
        self.action = QPushButton()
        self.action.clicked.connect(self._run)
        self.close_button = close = QPushButton('Dismiss')
        close.setObjectName('flat')
        close.clicked.connect(self._dismiss)
        layout.addWidget(self.text, 1)
        layout.addWidget(self.action)
        layout.addWidget(close)
        self._callback: Callable[[], None] | None = None
        self.hide()

    def show_message(self, text: str, kind: str = 'error', action: str = '',
                     callback: Callable[[], None] | None = None) -> None:
        if self.property('kind') != kind:
            self.setProperty('kind', kind)
            self.style().unpolish(self)
            self.style().polish(self)
        self.text.setText(text)
        self.action.setText(action)
        self.action.setVisible(bool(action))
        # A failure's message also clears the failure, so its button says so.
        # A failed download's message is removed with it, as on the Downloads page.
        self.close_button.setText('Clear' if kind == 'failure' else 'Dismiss')
        self.close_button.setToolTip('Removes the failed download from the list' if kind == 'failure' else '')
        self._callback = callback
        self.show()

    def _dismiss(self):
        self.hide()
        self.dismissed.emit()

    def _run(self):
        self._dismiss()
        if self._callback:
            self._callback()


class FactsCard(QFrame):
    """Key facts of a client in two columns."""

    def __init__(self):
        super().__init__()
        self.setObjectName('card')
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(18, 14, 18, 14)
        self.grid.setHorizontalSpacing(24)
        self.grid.setVerticalSpacing(SPACING)
        self.grid.setColumnStretch(1, 1)
        self.grid.setColumnStretch(3, 1)
        self.grid.setColumnStretch(4, 0)
        # Extra height, such as from a taller neighbouring card, stays below the rows.
        self.grid.setAlignment(Qt.AlignTop)
        self.cells: list[tuple[QLabel, ElidedLabel]] = []
        self.names: list[str] = []
        self.texts: list[str] = []

    WIDE = {'Folder', 'Installs into', 'Download size', 'Launch options'}
    PATHS = {'Folder', 'Installs into', 'Launch options'}     # long values keep a full row
    PLACEHOLDERS = {'Never', 'Not known yet', 'Being calculated'}

    def set_facts(self, facts: list[tuple[str, str]]) -> None:
        """Lay out facts in two columns. Paths get a full row at the end."""
        names = [name for name, _ in facts]
        narrow_first = [f for f in facts if f[0] not in self.WIDE] + [f for f in facts if f[0] in self.WIDE]
        texts = [text for _, text in narrow_first]
        if names == self.names and texts == self.texts:
            return
        if names == self.names and all((a in self.PLACEHOLDERS) == (b in self.PLACEHOLDERS)
                                       for a, b in zip(texts, self.texts)):
            for (key, value), text in zip(self.cells, texts):
                value.setText(text)
            self.texts = texts
            return
        self.texts = texts
        for key, value in self.cells:
            for widget in (key, value):
                # Detach at once. deleteLater alone leaves it painted until the next event loop pass.
                self.grid.removeWidget(widget)
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self.cells, self.names = [], names
        narrow = [f for f in facts if f[0] not in self.WIDE]
        wide = [f for f in facts if f[0] in self.WIDE]
        if len(narrow) % 2 and wide and wide[0][0] not in self.PATHS:
            narrow.append(wide.pop(0))      # a short value fills the gap instead of a row of its own
        for index, (name, text) in enumerate(narrow):
            row, column = divmod(index, 2)
            self._add(name, text, row, column * 2, 1)
        start = (len(narrow) + 1) // 2
        for offset, (name, text) in enumerate(wide):
            self._add(name, text, start + offset, 0, 3)


    def _add(self, name: str, text: str, row: int, column: int, span: int) -> None:
        mode = Qt.ElideMiddle if name in self.WIDE else Qt.ElideRight
        key = label(name, 'key')
        value = ElidedLabel(text, 'muted' if text in self.PLACEHOLDERS else None, mode=mode)
        self.grid.addWidget(key, row, column)
        self.grid.addWidget(value, row, column + 1, 1, span)
        self.cells.append((key, value))


class StorageCard(QFrame):
    """How the shared installation folder is used, client by client."""

    def __init__(self):
        super().__init__()
        self.setObjectName('card')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(SPACING)
        top = QHBoxLayout()
        top.setSpacing(SPACING)
        top.addWidget(label('Installation folder', 'cardtitle'))
        top.addStretch()
        self.free = label('', 'small')
        top.addWidget(self.free)
        layout.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setObjectName('meter')       # grey, so blue always means a download
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1000)
        layout.addWidget(self.bar)
        self.summary = label('', 'small', wrap=True)
        layout.addWidget(self.summary)
        self.rows = QGridLayout()
        self.rows.setContentsMargins(0, 4, 0, 0)
        self.rows.setHorizontalSpacing(10)
        self.rows.setVerticalSpacing(6)
        self.rows.setColumnStretch(1, 1)
        layout.addLayout(self.rows)
        self.path = ElidedLabel('', 'small', mode=Qt.ElideMiddle)
        layout.addWidget(self.path)
        # Alignment instead of a stretch, which would make the card expand vertically.
        layout.setAlignment(Qt.AlignTop)
        self.shown: list = []
        self.widgets: list[QWidget] = []

    def show_storage(self, root: str, stored: int, clients: list, free: int, measuring: bool = False) -> None:
        """clients holds (catalog.Client, bytes it needs) for each installed client."""
        total = stored + free
        self.bar.setValue(int(stored / total * 1000) if total else 0)
        self.free.setText(f'{size_text(free)} free')
        needed = sum(size for _, size in clients)
        if clients:
            text = f'{size_text(stored)} used'
            if needed > stored:
                text += f' · sharing saves {size_text(needed - stored)}'
            self.summary.setText(text)
        else:
            self.summary.setText('Measuring…' if measuring else 'No clients installed yet')
        self.path.setText(root)
        snapshot = [(client.code, size) for client, size in clients]
        if snapshot == self.shown:
            return
        self.shown = snapshot
        for widget in self.widgets:
            self.rows.removeWidget(widget)
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        self.widgets = []
        largest = max((size for _, size in clients), default=0)
        for row, (client, size) in enumerate(sorted(clients, key=lambda c: -c[1])):
            icon = QLabel()
            icon.setPixmap(tile(client.short, client_color(client), 20))
            icon.setFixedSize(20, 20)
            name = ElidedLabel(client.label)
            meter = QProgressBar()
            meter.setObjectName('meter')
            meter.setTextVisible(False)
            meter.setRange(0, 1000)
            meter.setValue(int(size / largest * 1000) if largest else 0)
            meter.setFixedWidth(90)
            amount = label(size_text(size), 'small')
            amount.setMinimumWidth(64)
            amount.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            for column, widget in enumerate([icon, name, meter, amount]):
                self.rows.addWidget(widget, row, column)
                self.widgets.append(widget)


class Segmented(QWidget):
    """A row of mutually exclusive buttons, used for game modes."""

    changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.layout_ = QHBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.layout_.setSpacing(0)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.group.buttonClicked.connect(lambda button: self.changed.emit(button.property('value')))
        self.group.buttonToggled.connect(lambda *_: self._update_focus())
        self.options: list[tuple[str, str]] = []

    def current(self) -> QPushButton | None:
        """The one Tab stop of the row. Arrow keys move between the choices.

        Focus chains list every button. Qt's Tab skips the ones that only take click focus."""
        return self.group.checkedButton()

    def _update_focus(self) -> None:
        # Tab lands on the chosen button only, so Space never picks another choice.
        for button in self.group.buttons():
            button.setFocusPolicy(Qt.StrongFocus if button.isChecked() else Qt.ClickFocus)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Left, Qt.Key_Right):
            buttons = self.group.buttons()
            index = buttons.index(watched) + (1 if event.key() == Qt.Key_Right else -1)
            if 0 <= index < len(buttons):
                buttons[index].click()
                buttons[index].setFocus(Qt.TabFocusReason)
            return True
        return super().eventFilter(watched, event)

    def set_options(self, options: list[tuple[str, str]], current: str | None) -> None:
        if options != self.options:
            for button in self.group.buttons():
                # Detach at once. deleteLater alone keeps the old buttons laid out.
                self.group.removeButton(button)
                self.layout_.removeWidget(button)
                button.hide()
                button.setParent(None)
                button.deleteLater()
            for index, (value, text) in enumerate(options):
                button = fixed(QPushButton(text))
                button.setObjectName('segment')
                button.setCheckable(True)
                button.setProperty('value', value)
                if index == 0:
                    button.setProperty('edge', 'first')
                if index == len(options) - 1:
                    button.setProperty('edge', 'last')
                button.installEventFilter(self)
                self.group.addButton(button)
                self.layout_.addWidget(button)
            self.options = options
        for button in self.group.buttons():
            button.setChecked(button.property('value') == current)
        if options and self.group.checkedButton() is None:
            self.group.buttons()[0].setChecked(True)
        self._update_focus()

    def value(self) -> str | None:
        button = self.group.checkedButton()
        return button.property('value') if button else None


class ClickableLabel(ElidedLabel):
    """A label that works like a link, with the mouse and with Tab, Enter or Space."""

    clicked = Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.TabFocus)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit()
            return
        super().keyPressEvent(event)


class DownloadBar(QFrame):
    """The download queue at the bottom of the window, whatever client is selected."""

    pause_clicked = Signal(str)
    resume_clicked = Signal(str)
    cancel_clicked = Signal(str)
    open_client = Signal(str)
    limit_changed = Signal(int)
    show_queue = Signal()

    LIMITS = [0, 1, 2, 5, 10, 25, 50, 100]          # MB/s, 0 means unlimited

    def __init__(self):
        super().__init__()
        self.setObjectName('downloads')
        self.setFixedHeight(64)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # The progress runs along the bar's top edge, so it never competes with the text for width.
        self.progress = QProgressBar()
        self.progress.setObjectName('strip')
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1000)
        outer.addWidget(self.progress)
        layout = QHBoxLayout()
        layout.setContentsMargins(18, 0, 14, 0)
        self.row = layout
        layout.setSpacing(12)
        outer.addLayout(layout, 1)
        self.client = ''
        self.title = ClickableLabel('')
        self.title.setStyleSheet('font-weight: 600;')
        self.title.setMinimumWidth(120)
        self.title.setCursor(Qt.PointingHandCursor)
        self.title.setToolTip('Show this client')
        self.title.clicked.connect(lambda: self.client and self.open_client.emit(self.client))
        self.detail = ElidedLabel('', 'small')
        text = QVBoxLayout()
        text.setSpacing(3)
        text.addStretch()
        text.addWidget(self.title)
        text.addWidget(self.detail)
        text.addStretch()
        for widget in (self.title, self.detail):
            widget.setSizePolicy(widget.sizePolicy().horizontalPolicy(), QSizePolicy.Fixed)
        self.toggle = fixed(QPushButton('Pause'), 80)
        self.toggle.clicked.connect(self._toggle)
        self.cancel = fixed(QPushButton('Cancel'), 80)
        self.cancel.clicked.connect(lambda: self.client and self.cancel_clicked.emit(self.client))
        self.queue = fixed(QPushButton('Downloads'), 120)
        self.queue.setToolTip('Show every download, and set the speed limit')
        self.queue.clicked.connect(self.show_queue.emit)
        self.limit = fixed(QComboBox(), 108)
        self.limit.setToolTip('Download speed limit')
        self.limit.activated.connect(lambda _: self.limit_changed.emit(int(self.limit.currentData())))
        layout.addLayout(text, 1)
        layout.addWidget(self.toggle)
        layout.addWidget(self.cancel)
        layout.addWidget(self.queue)
        self.limit.hide()
        self._paused = False

    def align_to(self, left: int, right: int) -> None:
        """Line the bar's content up with the page column above it."""
        margins = (max(18, left), 0, max(14, right), 0)
        if self.row.contentsMargins().left() != margins[0] or self.row.contentsMargins().right() != margins[2]:
            self.row.setContentsMargins(*margins)

    def set_limit(self, megabytes: int) -> None:
        self.limit.blockSignals(True)
        self.limit.clear()
        for value in sorted(set(self.LIMITS + [megabytes])):
            self.limit.addItem('No limit' if value == 0 else f'{value} MB/s', value)
        self.limit.setCurrentIndex(self.limit.findData(megabytes))
        self.limit.blockSignals(False)

    def show_job(self, job, label_text: str, waiting: int, controls: bool = True) -> None:
        from ..downloads import PAUSED, QUEUED, RUNNING
        self.client, self._paused = job.client, job.status == PAUSED
        verb = {'install': 'Installing', 'update': 'Updating', 'verify': 'Verifying',
                'repair': 'Repairing'}[job.kind]
        if job.status in {PAUSED, QUEUED}:
            # The state first, so a shortened title still says it.
            self.title.setText(f"{'Paused' if job.status == PAUSED else 'Queued'} · {label_text}")
        else:
            self.title.setText(f'{verb} {label_text}')
        tone(self.progress, 'attention' if job.status == PAUSED else '')
        self.detail.set_variants(progress_variants(job))
        if job.total_bytes or job.status != RUNNING:
            # Only a running job without a known size gets the moving bar.
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(job.fraction * 1000) if job.total_bytes else 0)
        else:
            self.progress.setRange(0, 0)
        self.progress.show()
        self.toggle.setText('Resume' if self._paused else 'Pause')
        # A waiting job has nothing to pause yet.
        self.toggle.setVisible(controls and job.status != QUEUED)
        self.cancel.setToolTip(f'Cancel {label_text}')
        # A queued job is taken out on the Downloads page, so the bar has one Cancel at most.
        self.cancel.setVisible(controls and job.status != QUEUED)
        self.toggle.setToolTip(f'{self.toggle.text()} {label_text}')
        self.queue.setText(f'Downloads · {waiting + 1}' if waiting else 'Downloads')

    def show_summary(self, title: str, detail: str, count: int) -> None:
        """No other download to show, but the Downloads page still has something on it."""
        self.client = ''
        self.title.setText(title)
        self.detail.set_variants([detail])
        tone(self.progress, '')
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.toggle.hide()
        self.cancel.hide()
        self.queue.setText(f'Downloads · {count}' if count > 1 else 'Downloads')

    def show_idle(self, text: str) -> None:
        self.client = ''
        self.title.setText('No downloads')
        self.detail.setText(text)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.toggle.hide()
        self.cancel.hide()

    def _toggle(self):
        if self.client:
            (self.resume_clicked if self._paused else self.pause_clicked).emit(self.client)
