"""Launch settings for Linux: the Proton build, common options, variables and a wrapper command.

Self-contained, so the settings page can place it in any section: load(settings),
apply(settings), the changed signal and focus_chain().
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                               QSizePolicy, QSpacerItem, QVBoxLayout, QWidget)

from .. import launch_options as options_module
from ..launch import LaunchError, log_dir, select_proton
from ..launch_options import LinuxOptions, OptionError, format_environment, parse_environment, parse_wrapper
from ..runtime.proton import find_protons
from ..settings import Settings
from .widgets import fixed, label

KEY_WIDTH = 96        # the same columns as the settings page's rows
FIELD_WIDTH = 480
SAVE_DELAY = 400      # typed text saves once the typing pauses
FIELD_TOP = 7         # aligns a label with a one-line field that has a note below it


def problem_label() -> QLabel:
    widget = label('', 'problem', wrap=True)
    widget.hide()
    return widget


def say(widget, text: str, name: str = 'problem') -> None:
    """Show a short inline note under a field, or hide it."""
    if widget.objectName() != name:
        widget.setObjectName(name)
        widget.style().unpolish(widget)
        widget.style().polish(widget)
    widget.setText(text)
    widget.setVisible(bool(text))


def add_row(grid: QGridLayout, name: str, field, *, top: int = 0) -> None:
    """A labelled row like the settings page's. A top offset aligns the label with a taller field's first line."""
    index = grid.rowCount()
    key = label(name, 'key')
    key.setFixedWidth(KEY_WIDTH)
    key.setContentsMargins(0, top, 0, 0)
    grid.addWidget(key, index, 0, Qt.AlignTop if top else Qt.AlignVCenter)
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
    grid.addItem(QSpacerItem(0, 0, QSizePolicy.Expanding, QSizePolicy.Minimum), index, 2)


class EnvironmentEditor(QWidget):
    """NAME=value lines. Bad lines are named below the field and nothing is lost while typing."""

    changed = Signal()

    def __init__(self, placeholder: str = 'One NAME=value per line, such as DXVK_HUD=fps'):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.text = QPlainTextEdit()
        self.text.setObjectName('environment')
        self.text.setTabChangesFocus(True)       # Tab moves on, as in every other field
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.text.setPlaceholderText(placeholder)
        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        font.setPointSizeF(self.font().pointSizeF())
        self.text.setFont(font)
        self.text.setFixedHeight(84)
        self.problem = problem_label()
        layout.addWidget(self.text)
        layout.addWidget(self.problem)
        self.variables: dict[str, str] = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SAVE_DELAY)
        self._timer.timeout.connect(self.flush)
        self.text.textChanged.connect(self._edited)

    def set_variables(self, variables: dict[str, str]) -> None:
        self.text.blockSignals(True)
        self.text.setPlainText(format_environment(variables))
        self.text.blockSignals(False)
        self.variables = dict(variables)
        say(self.problem, '')

    def _edited(self):
        _, errors = parse_environment(self.text.toPlainText())
        # The first problem is enough to act on. The rest follow once it is fixed.
        say(self.problem, f'{errors[0]} That line is not saved until it is fixed.' if errors else '')
        self._timer.start()

    def flush(self) -> None:
        self._timer.stop()
        # The good lines save. A line with a mistake waits until it is fixed.
        variables, _ = parse_environment(self.text.toPlainText())
        if variables != self.variables:
            self.variables = variables
            self.changed.emit()

    def valid(self) -> bool:
        return not parse_environment(self.text.toPlainText())[1]

    def hideEvent(self, event):
        self.flush()             # leaving the page keeps what was typed
        super().hideEvent(event)


class LaunchSettings(QWidget):
    """The Linux launch settings that apply to every client."""

    changed = Signal()
    edited = Signal()           # any typing, saved or not

    # Setting, label, tooltip. The tooltips say what each one does under umu and Proton.
    TOGGLES = [
        ('gamemode', 'GameMode', 'Runs the game through gamemoderun, so the system switches to its '
                                 'performance profile while you play.'),
        ('mangohud', 'MangoHud', 'Shows the MangoHud overlay with frame rate and frame times.'),
        ('wayland', 'Wayland', 'Runs the game as a native Wayland window instead of through XWayland.'),
        ('hdr', 'HDR', 'Lets the game output HDR. Needs Wayland or gamescope and an HDR display.'),
        ('log', 'Proton log', 'Writes a detailed Proton log. It grows quickly, so turn it off again '
                              'once you have it.'),
    ]

    def __init__(self):
        super().__init__()
        self._loading = False
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)
        self.grid = grid

        self.proton = fixed(QComboBox())
        self.proton.setToolTip('Runs the game through umu-run. Builds from Steam and ProtonUp are found '
                               'automatically.')
        # Long build names must not widen the page past a small window.
        self.proton.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.proton.setMinimumContentsLength(5)
        self.proton.currentIndexChanged.connect(self._proton_changed)
        self.row('Proton', self.proton)

        toggles = QGridLayout()
        toggles.setHorizontalSpacing(16)
        toggles.setVerticalSpacing(2)
        self.boxes: dict[str, QCheckBox] = {}
        self.notes: dict[str, QWidget] = {}
        # What the user chose. A greyed out box shows off, and its choice waits here for a build that has it.
        self.choices: dict[str, bool] = {}
        for index, (name, text, tip) in enumerate(self.TOGGLES):
            box = QCheckBox(text)
            box.setToolTip(tip)
            box.toggled.connect(lambda checked, name=name: self._toggled(name, checked))
            # Why an option does nothing here, on a line of its own so the columns stay narrow.
            note = label('', 'small', wrap=True)
            note.setContentsMargins(26, 0, 0, 2)
            note.hide()
            cell = QVBoxLayout()
            cell.setSpacing(0)
            cell.addWidget(box)
            cell.addWidget(note)
            cell.addStretch()
            if name == 'log':
                # Its note names a folder, so it gets the whole width.
                toggles.addLayout(cell, (index + 1) // 2, 0, 1, 2)
            else:
                toggles.addLayout(cell, index // 2, index % 2)
            self.boxes[name], self.notes[name] = box, note
        toggles.setColumnStretch(0, 1)
        toggles.setColumnStretch(1, 1)
        self.row('Options', toggles, top=2)

        self.wrapper = fixed(QLineEdit())
        self.wrapper.setPlaceholderText('For example gamescope -f --')
        self.wrapper.setToolTip('Runs before umu-run, as Steam\'s launch options do. '
                                'Scuffle adds the game after it.')
        self.wrapper_note = problem_label()
        self.wrapper.textChanged.connect(self._wrapper_edited)
        self._wrapper_timer = QTimer(self)
        self._wrapper_timer.setSingleShot(True)
        self._wrapper_timer.setInterval(SAVE_DELAY)
        self._wrapper_timer.timeout.connect(self._flush_wrapper)
        self.wrapper.editingFinished.connect(self._flush_wrapper)
        wrapper = QVBoxLayout()
        wrapper.setSpacing(4)
        wrapper.addWidget(self.wrapper)
        wrapper.addWidget(self.wrapper_note)
        self.row('Wrapper', wrapper, top=FIELD_TOP)

        self.environment = EnvironmentEditor()
        self.environment.setToolTip('One NAME=value per line. They apply to the game, and a client\'s '
                                    'own variables win over these.')
        self.environment.changed.connect(self._emit)
        self.wrapper.textChanged.connect(lambda *_: self.edited.emit())
        self.environment.text.textChanged.connect(lambda *_: self.edited.emit())
        self.row('Environment', self.environment, top=6)
        self._saved_wrapper = ''
        self._loaded = LinuxOptions()
        self.mangohud_found = options_module.vulkan_layer('MangoHud')

    def row(self, name: str, field, *, top: int = 0) -> None:
        add_row(self.grid, name, field, top=top)

    def focus_chain(self) -> list[QWidget]:
        """The controls in reading order, for the window's tab order."""
        return [self.proton, *self.boxes.values(), self.wrapper, self.environment.text]

    # Loading and saving

    def load(self, settings: Settings) -> None:
        self._loading = True
        self.proton.clear()
        found = find_protons()
        newest = self.chosen_proton('')
        self.proton.addItem(f'Newest installed build, {newest.name}' if newest else 'Newest installed build', '')
        for proton in found:
            self.proton.addItem(proton.name, str(proton.path))
        index = self.proton.findData(settings.proton)
        if index < 0 and settings.proton:
            self.proton.addItem(Path(settings.proton).name, settings.proton)
            index = self.proton.count() - 1
        self.proton.setCurrentIndex(max(index, 0))
        # Text that is still being corrected stays when Settings opens again.
        keep = self._loaded is not None and self.unsaved()
        options = self._loaded = settings.linux_launch
        self.choices = {name: getattr(options, name) for name in self.boxes}
        self._saved_wrapper = options.wrapper
        if keep:
            self.environment.variables = dict(options.environment)
        else:
            self.wrapper.setText(options.wrapper)
            self.environment.set_variables(options.environment)
        self.update_notes()
        self._wrapper_edited()
        self._loading = False

    def values(self) -> tuple[str, LinuxOptions]:
        """The Proton build and the options as they would be saved. Invalid text keeps its saved value."""
        return self.proton.currentData() or '', replace(
            self._loaded, **self.choices, environment=dict(self.environment.variables), wrapper=self._saved_wrapper)

    def apply(self, settings: Settings) -> None:
        settings.proton, settings.linux_launch = self.values()

    def unsaved(self) -> bool:
        """Whether a field holds text that cannot be saved yet."""
        try:
            parse_wrapper(self.wrapper.text())
        except OptionError:
            return True
        return not self.environment.valid()

    def _emit(self, *_):
        if not self._loading:
            self.changed.emit()

    def _toggled(self, name: str, checked: bool):
        self.choices[name] = checked      # update_notes shows choices with the signals blocked
        self.update_notes()
        self._emit()

    def _proton_changed(self, *_):
        self.update_notes()
        self._emit()

    def _wrapper_edited(self, *_):
        text = self.wrapper.text()
        try:
            words = parse_wrapper(text)
        except OptionError as error:
            say(self.wrapper_note, f'{error} Not saved until fixed.')
            return
        if words and options_module.find_program(words[0]) is None:
            # Saved anyway, since it may be installed later, so a caution and not an error.
            say(self.wrapper_note, f'{words[0]} was not found. Install it or check the name.', 'caution')
        else:
            say(self.wrapper_note, '')
        if not self._loading:
            self._wrapper_timer.start()

    def _flush_wrapper(self):
        self._wrapper_timer.stop()
        text = self.wrapper.text()
        try:
            parse_wrapper(text)
        except OptionError:
            return
        if text != self._saved_wrapper:
            self._saved_wrapper = text
            self._emit()

    def hideEvent(self, event):
        self._flush_wrapper()
        super().hideEvent(event)

    # What each option can do with this machine and Proton build

    def chosen_proton(self, choice: str | None = None) -> Path | None:
        """The build a launch would use, so the notes speak about that one."""
        try:
            return select_proton(Settings(proton=self.proton.currentData() or '' if choice is None
                                          else choice)).path
        except (LaunchError, OSError):
            return None

    def update_notes(self) -> None:
        proton = self.chosen_proton()
        reads = lambda name: options_module.proton_reads(proton, name)
        # Why an option cannot work here. It is greyed out and shown off, and its choice is kept for later.
        reasons = {
            'gamemode': '' if options_module.find_program('gamemoderun') else 'gamemoderun was not found',
            'mangohud': '' if self.mangohud_found or options_module.find_program('mangohud')
                        else 'MangoHud was not found',
            'wayland': '' if reads('PROTON_ENABLE_WAYLAND') else 'Not in this Proton build',
            'hdr': '' if reads('PROTON_ENABLE_HDR') else 'Not in this Proton build',
        }
        for option, box in self.boxes.items():
            reason = reasons.get(option, '')
            box.setEnabled(not reason)
            box.blockSignals(True)
            box.setChecked(self.choices.get(option, False) and not reason)
            box.blockSignals(False)
            note = reason or (f'Written to {log_folder()}' if option == 'log' and box.isChecked() else '')
            self.notes[option].setText(note)
            self.notes[option].setVisible(bool(note))


def log_folder() -> str:
    """The log folder as the user knows it, with ~ for the home folder."""
    folder = log_dir()
    try:
        return '~/' + str(folder.relative_to(Path.home()))
    except ValueError:
        return str(folder)
