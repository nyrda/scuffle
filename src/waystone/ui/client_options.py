"""One client's launch options: game arguments everywhere, and variables on Linux."""
from __future__ import annotations

import sys

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QCheckBox, QGridLayout, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout, QWidget

from .. import catalog
from ..launch_options import GRAPHICS, SWITCHES, WINDOW, ClientOptions, OptionError, split_arguments
from .launch_options import FIELD_TOP, SAVE_DELAY, EnvironmentEditor, add_row, problem_label, say
from .pages import GUTTER, PAGE_MARGINS, Page, SettingsPage, column, scrolling
from .widgets import ElidedLabel, Segmented, fixed, label


def summary(options: ClientOptions) -> str:
    """A short line for the client page, such as -console -d3d11 · 2 variables."""
    try:
        parts = [' '.join(options.game_arguments())]
    except OptionError:
        parts = ['Arguments need a fix']
    count = len(options.environment)
    if count and sys.platform != 'win32':
        parts.append(f'{count} variable' + ('' if count == 1 else 's'))
    return ' · '.join(p for p in parts if p)


class ClientOptionsPage(Page):
    """Opened from a client's ⋯ menu. Changes save as they are made."""

    closed = Signal()
    changed = Signal(str, object)        # client code, ClientOptions

    def __init__(self, linux: bool = sys.platform != 'win32'):
        super().__init__()
        self.code = ''
        self._loading = False
        body_layout = scrolling(self, margins=(PAGE_MARGINS[0], 4, PAGE_MARGINS[2], PAGE_MARGINS[3]))
        # The title and Close stay above the scrolling cards, as on the settings page.
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
        title_block.addWidget(label('Launch options', 'pagetitle'))
        self.client_name = ''
        self.saved_note = ElidedLabel('', 'small')
        title_block.addWidget(self.saved_note)
        title_row.addLayout(title_block, 1)       # the note shortens only once the buttons need room
        self.reset_button = fixed(QPushButton('Reset'), 96)
        self.reset_button.setToolTip('Remove this client\'s launch options')
        self.reset_button.clicked.connect(self.reset)
        title_row.addWidget(self.reset_button)
        self.close_button = fixed(QPushButton('Close'), 96)
        self.close_button.setToolTip('Back to the client (Esc)')
        self.close_button.clicked.connect(self.closed.emit)
        title_row.addWidget(self.close_button)
        self.centre(column(header_layout)).addLayout(title_row)
        self.on_escape(self.closed.emit)

        grid = SettingsPage.card(content, 'Game arguments',
                                 'Options the game itself offers. Waystone always adds -launcherlogin '
                                 'and -uid, and the game mode.')
        self.graphics = Segmented()
        self.graphics.set_options(list(GRAPHICS), '')
        self.graphics.changed.connect(self._edited)
        SettingsPage.row(grid, 'Graphics', self.graphics)
        self.display = Segmented()
        self.display.set_options(list(WINDOW), '')
        self.display.changed.connect(self._edited)
        self.display.setToolTip('Fullscreen is a borderless window covering the screen.')
        SettingsPage.row(grid, 'Window', self.display)
        switches = QGridLayout()
        switches.setHorizontalSpacing(16)
        switches.setVerticalSpacing(6)
        self.switches: dict[str, QCheckBox] = {}
        for index, switch in enumerate(SWITCHES):
            box = QCheckBox(switch.label)
            box.setToolTip(f'{switch.tip} Adds {switch.argument}.')
            box.toggled.connect(self._edited)
            switches.addWidget(box, index // 2, index % 2)
            self.switches[switch.argument] = box
        SettingsPage.row(grid, 'Options', switches)
        self.arguments = fixed(QLineEdit())
        self.arguments.setPlaceholderText('For example -config Config2.wtf')
        self.arguments.setToolTip('More arguments for Wow.exe, split as a shell would.')
        self.arguments.textChanged.connect(self._arguments_edited)
        self.arguments_note = problem_label()
        extra = QVBoxLayout()
        extra.setSpacing(4)
        extra.addWidget(self.arguments)
        extra.addWidget(self.arguments_note)
        add_row(grid, 'Extra', extra, top=FIELD_TOP)
        self.preview = label('', 'small', wrap=True)
        # In the fields' column only, so the cards' fields end at the same edge.
        grid.addWidget(self.preview, grid.rowCount(), 1)

        self.environment = None
        self._hidden_environment: dict[str, str] = {}     # kept for Linux when this page cannot show it
        if linux:
            grid = SettingsPage.card(content, 'Environment',
                                     'Added to the variables in Settings. A name set here wins.')
            self.environment = EnvironmentEditor()
            self.environment.changed.connect(self._edited)
            self.environment.text.textChanged.connect(self.update_preview)     # so Reset can clear a draft
            add_row(grid, 'Variables', self.environment, top=6)
        body_layout.addStretch()
        self._saved_arguments = ''
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SAVE_DELAY)
        self._timer.timeout.connect(self._flush_arguments)

    def focus_chain(self) -> list[QWidget]:
        """The page's controls in reading order, for the window's tab order."""
        return [self.reset_button, self.close_button, *self.graphics.group.buttons(),
                *self.display.group.buttons(), *self.switches.values(), self.arguments,
                *([self.environment.text] if self.environment else [])]

    def load(self, client: catalog.Client, options: ClientOptions | None) -> None:
        options = options or ClientOptions()
        self._loading = True
        self.code = client.code
        self.client_name = client.label
        self.saved_note.setText(f'For {client.label}. Changes save automatically.')
        self.graphics.set_options(list(GRAPHICS), options.graphics)
        self.display.set_options(list(WINDOW), options.window)
        for argument, box in self.switches.items():
            box.setChecked(argument in options.switches)
        self.arguments.setText(options.arguments)
        self._saved_arguments = options.arguments
        if self.environment:
            self.environment.set_variables(options.environment)
        self._hidden_environment = dict(options.environment)
        self._loading = False
        self._arguments_edited()
        self._timer.stop()
        self.update_preview()

    def options(self) -> ClientOptions:
        """The options as saved. Text with a mistake keeps its last good value."""
        return ClientOptions(graphics=self.graphics.value() or '', window=self.display.value() or '',
                             switches=[a for a, box in self.switches.items() if box.isChecked()],
                             arguments=self._saved_arguments,
                             environment=dict(self.environment.variables if self.environment
                                              else self._hidden_environment))

    def update_preview(self) -> None:
        try:
            words = ClientOptions(**{**self.options().__dict__, 'arguments': self.arguments.text()}).game_arguments()
        except OptionError:
            words = self.options().game_arguments()
        self.preview.setText(f'Adds {" ".join(words)}' if words else 'Starts with the game\'s own defaults.')
        drafts = self.arguments.text().strip() or (self.environment and self.environment.text.toPlainText().strip())
        self.reset_button.setEnabled(self.options().active() or bool(drafts))

    def _arguments_edited(self, *_):
        self.update_arguments_note()
        self.update_preview()
        if not self._loading:
            self._timer.start()

    def _flush_arguments(self):
        self._timer.stop()
        try:
            split_arguments(self.arguments.text())
        except OptionError:
            return
        if self.arguments.text() != self._saved_arguments:
            self._saved_arguments = self.arguments.text()
            self._edited()

    def update_arguments_note(self) -> None:
        typed = ClientOptions(**{**self.options().__dict__, 'arguments': self.arguments.text()})
        try:
            overlaps = typed.overlaps()
        except OptionError as error:
            say(self.arguments_note, f'{error} Not saved until fixed.')
            return
        # Saved anyway. The game gets both, so the first overlap is worth a calm word.
        say(self.arguments_note, overlaps[0] if overlaps else '', 'caution')

    def _edited(self, *_):
        if self._loading or not self.code:
            return
        self.update_arguments_note()
        self.update_preview()
        self.changed.emit(self.code, self.options())
        self.saved_note.setText(f'For {self.client_name}. Saved.')
        QTimer.singleShot(1600, lambda: self.saved_note.setText(
            f'For {self.client_name}. Changes save automatically.'))

    def reset(self):
        self.arguments.setText('')
        self._saved_arguments = ''
        self.graphics.set_options(list(GRAPHICS), '')
        self.display.set_options(list(WINDOW), '')
        self._loading = True
        for box in self.switches.values():
            box.setChecked(False)
        if self.environment:
            self.environment.set_variables({})
        self._hidden_environment = {}
        self._loading = False
        self._timer.stop()
        self._edited()
        self.close_button.setFocus()       # Reset is disabled now

    def hideEvent(self, event):
        self._flush_arguments()        # leaving the page, by Close, Esc or the list, keeps what was typed
        super().hideEvent(event)
