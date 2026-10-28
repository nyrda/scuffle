"""Launch options: game arguments for each client and, on Linux, what runs around the game.

The game arguments come from the clients' own usage text (`Wow.exe -help`). The
Linux options map to the umu-run and Proton environment, or to wrapper commands
placed before umu-run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shlex
import shutil


class OptionError(ValueError):
    pass


@dataclass(frozen=True)
class Switch:
    argument: str
    label: str
    tip: str


# Each choice is one argument or none. Empty means the game decides.
GRAPHICS = (('', 'Automatic'), ('-d3d12', 'DirectX 12'), ('-d3d11', 'DirectX 11'))
WINDOW = (('', 'As saved'), ('-windowed', 'Windowed'), ('-fullscreen', 'Fullscreen'))
SWITCHES = (
    Switch('-console', 'Console', 'Enables the in-game console. Its key is ~ by default.'),
    Switch('-nosound', 'No sound', 'Starts the game without sound.'),
    Switch('-compat', 'Compatibility mode', 'Turns off optional GPU features. Try it when the game crashes '
                                            'or draws wrongly.'),
    Switch('-nodriverext', 'No driver extensions', 'Turns off GPU driver extensions such as AMD AGS, Intel '
                                                   'IGDEXT and NVIDIA NVAPI.'),
)
CHOICES = {value for value, _ in GRAPHICS + WINDOW if value}
SWITCH_ARGUMENTS = [switch.argument for switch in SWITCHES]

# Waystone passes these itself. A second copy would confuse the game's sign-in or mode.
RESERVED_ARGUMENTS = {'launcherlogin', 'uid', 'initialgamemode'}
# The runtime needs these to find its prefix, Proton and the bridge's credential.
RESERVED_VARIABLES = {'WINEPREFIX', 'PROTONPATH', 'GAMEID', 'WOW_BROKER_PIPE', 'UMU_CONTAINER_NSENTER'}
NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')


def split_arguments(text: str) -> list[str]:
    """Game arguments as a shell would split them, keeping backslashes for Windows paths."""
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ''
    lexer.commenters = ''
    try:
        words = list(lexer)
    except ValueError:
        raise OptionError('A quote is not closed.') from None
    for word in words:
        name = word.lstrip('-').split('=', 1)[0].lower()
        if word.startswith('-') and name in RESERVED_ARGUMENTS:
            raise OptionError(f'Waystone sets {word.split("=", 1)[0]} itself. Remove it here.')
    return words


def parse_environment(text: str) -> tuple[dict[str, str], list[str]]:
    """NAME=value lines, with # comments. Returns the variables and one message per bad line."""
    variables: dict[str, str] = {}
    lines: dict[str, int] = {}
    errors = []
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export '):].lstrip()
        name, equals, value = line.partition('=')
        name, value = name.strip(), value.strip()
        if not equals:
            errors.append(f'Line {number}: write it as NAME=value.')
        elif not NAME.match(name):
            errors.append(f'Line {number}: {name or "the name"} is not a valid name. '
                          'Use letters, digits and _, not starting with a digit.')
        elif name in RESERVED_VARIABLES:
            errors.append(f'Line {number}: Waystone sets {name} itself.')
        elif name in variables:
            errors.append(f'Line {number}: {name} is already set on line {lines[name]}.')
        else:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
                value = value[1:-1]
            variables[name], lines[name] = value, number
    return variables, errors


def format_environment(variables: dict[str, str]) -> str:
    """The editor's text for saved variables. parse_environment reads it back to the same values."""
    return '\n'.join(f'{name}={quoted(value)}' for name, value in variables.items())


def quoted(value: str) -> str:
    # The parser strips spaces and one pair of surrounding quotes, so such a value gets a pair of its own.
    if value != value.strip() or (len(value) >= 2 and value[0] == value[-1] and value[0] in '"\''):
        mark = "'" if value.startswith('"') else '"'
        return f'{mark}{value}{mark}'
    return value


def parse_wrapper(text: str) -> list[str]:
    """A wrapper command such as `gamescope -f --`, placed before umu-run."""
    try:
        words = shlex.split(text)
    except ValueError:
        raise OptionError('A quote is not closed.') from None
    if not words:
        return []
    if '%command%' in words:
        raise OptionError('Leave out %command%. Waystone adds the game after the wrapper.')
    if NAME.match(words[0].partition('=')[0]) and '=' in words[0]:
        raise OptionError('Set variables in Environment, not here.')
    if Path(words[0]).name == 'gamescope' and '--' not in words:
        raise OptionError('gamescope needs -- after its own options.')
    return words


def find_program(name: str) -> str | None:
    return shutil.which(name)


def gpu_vendors() -> set[str]:
    """PCI vendor IDs of the display adapters, such as 0x10de for NVIDIA."""
    vendors = set()
    for path in Path('/sys/class/drm').glob('card*/device/vendor'):
        try:
            vendors.add(path.read_text().strip().lower())
        except OSError:
            continue
    return vendors


def has_nvidia() -> bool:
    return '0x10de' in gpu_vendors() or Path('/proc/driver/nvidia').exists()


def vulkan_layer(prefix: str) -> bool:
    """Whether an implicit Vulkan layer, such as MangoHud's, is installed where the loader looks."""
    home = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    data = [Path(p) for p in (os.environ.get('XDG_DATA_DIRS') or '/usr/local/share:/usr/share').split(':') if p]
    for base in [home, *data, Path('/etc')]:
        if any((base / 'vulkan/implicit_layer.d').glob(f'{prefix}*.json')):
            return True
    return False


def proton_reads(proton: Path | None, name: str) -> bool:
    """Whether a Proton build's launch script knows an environment variable."""
    try:
        return name in (Path(proton) / 'proton').read_text(errors='replace')
    except (OSError, TypeError):
        return False


@dataclass
class LinuxOptions:
    """What runs around the game on Linux. Global for every client."""
    gamemode: bool = False
    mangohud: bool = False
    wayland: bool = False
    hdr: bool = False
    nvapi: bool = True          # applies only with an NVIDIA GPU
    ntsync: bool = True         # applies only to Proton builds that offer to turn it off
    log: bool = False
    environment: dict[str, str] = field(default_factory=dict)
    wrapper: str = ''

    @classmethod
    def from_json(cls, data, problems: list[str]) -> 'LinuxOptions':
        options = cls()
        if not isinstance(data, dict):
            problems.append('The Linux launch options were unreadable and were reset.')
            return options
        for name in ['gamemode', 'mangohud', 'wayland', 'hdr', 'nvapi', 'ntsync', 'log']:
            if name in data:
                if isinstance(data[name], bool):
                    setattr(options, name, data[name])
                else:
                    problems.append(f'The launch option {name} was not on or off and was reset.')
        options.environment = checked_environment(data.get('environment', {}), 'the Linux settings', problems)
        wrapper = data.get('wrapper', '')
        try:
            if not isinstance(wrapper, str):
                raise OptionError('It was not text.')
            parse_wrapper(wrapper)
            options.wrapper = wrapper
        except OptionError as error:
            problems.append(f'The wrapper command was removed. {error}')
        return options


@dataclass
class ClientOptions:
    """One client's own launch options. Arguments apply everywhere, variables on Linux."""
    graphics: str = ''
    window: str = ''
    switches: list[str] = field(default_factory=list)
    arguments: str = ''
    environment: dict[str, str] = field(default_factory=dict)

    def game_arguments(self) -> list[str]:
        chosen = [self.graphics, self.window, *(a for a in SWITCH_ARGUMENTS if a in self.switches)]
        # The extra words go as typed, since one of them may be another argument's value.
        return [a for a in chosen if a] + split_arguments(self.arguments)

    def overlaps(self) -> list[str]:
        """Extra arguments that repeat or contradict a toggle. The game gets both, so the page says so."""
        notes = []
        groups = [('Graphics', GRAPHICS, self.graphics), ('Window', WINDOW, self.window)]
        for word in dict.fromkeys(w.lower() for w in split_arguments(self.arguments)):
            if word in SWITCH_ARGUMENTS and word in self.switches:
                notes.append(f'{word} is already on under Options.')
            for name, choices, chosen in groups:
                names = dict(choices)
                if word in names and chosen == word:
                    notes.append(f'{word} is already chosen under {name}.')
                elif word in names and chosen:
                    notes.append(f'{word} contradicts {names[chosen]} under {name}.')
        return notes

    def active(self) -> bool:
        return self != ClientOptions()

    @classmethod
    def from_json(cls, data, label: str, problems: list[str]) -> 'ClientOptions | None':
        if not isinstance(data, dict):
            problems.append(f'The launch options of {label} were unreadable and were removed.')
            return None
        options = cls()
        for name, allowed in [('graphics', GRAPHICS), ('window', WINDOW)]:
            value = data.get(name, '')
            if isinstance(value, str) and value in {v for v, _ in allowed}:
                setattr(options, name, value)
            else:
                problems.append(f'An unknown {name} option of {label} was removed.')
        switches = data.get('switches', [])
        switches = switches if isinstance(switches, list) else [None]
        options.switches = [s for s in SWITCH_ARGUMENTS if s in switches]
        if any(s not in SWITCH_ARGUMENTS for s in switches):
            problems.append(f'Unknown switches of {label} were removed.')
        arguments = data.get('arguments', '')
        try:
            if not isinstance(arguments, str):
                raise OptionError('They were not text.')
            split_arguments(arguments)
            options.arguments = arguments
        except OptionError as error:
            problems.append(f'The extra arguments of {label} were removed. {error}')
        options.environment = checked_environment(data.get('environment', {}), label, problems)
        return options


def checked_environment(data, where: str, problems: list[str]) -> dict[str, str]:
    """Variables from a settings file, without the ones that would not pass the editor."""
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        problems.append(f'The environment variables of {where} were unreadable and were removed.')
        return {}
    variables, errors = parse_environment('\n'.join(f'{k}={v}' for k, v in data.items() if '\n' not in k + v))
    # Values keep their exact text, including quotes the editor would have taken off.
    kept = {name: data[name] for name in variables if name in data}
    if errors or len(kept) != len(data):
        problems.append(f'Some environment variables of {where} were invalid and were removed.')
    return kept


def linux_environment(options: LinuxOptions, client: ClientOptions | None = None, *,
                      nvidia: bool, log_dir: Path) -> dict[str, str]:
    """The variables the game's umu-run gets on top of Waystone's own. Later ones win."""
    variables = {}
    if options.mangohud:
        # MangoHud's Vulkan layer reaches the game through pressure-vessel. Its wrapper's
        # LD_PRELOAD would only load into umu-run itself.
        variables['MANGOHUD'] = '1'
    if options.wayland:
        variables['PROTON_ENABLE_WAYLAND'] = '1'
    if options.hdr:
        variables['PROTON_ENABLE_HDR'] = '1'
    if nvidia and not options.nvapi:
        # Proton 9 and later give NVIDIA GPUs NVAPI by default, so only turning it off needs a variable.
        variables['PROTON_DISABLE_NVAPI'] = '1'
    if not options.ntsync:
        variables['PROTON_NO_NTSYNC'] = '1'
    if options.log:
        variables.update(PROTON_LOG='1', PROTON_LOG_DIR=str(log_dir))
    # Paths such as ~/.config/MangoHud/wow.conf or $HOME/dxvk.conf work as in a shell.
    expand = lambda values: {name: os.path.expanduser(os.path.expandvars(value)) for name, value in values.items()}
    variables.update(expand(options.environment))
    if client is not None:
        variables.update(expand(client.environment))
    return variables


def wrapper_command(options: LinuxOptions) -> list[str]:
    """The commands placed before umu-run, with their programs found on PATH."""
    words = parse_wrapper(options.wrapper)
    if words:
        found = find_program(words[0])
        if found is None:
            raise OptionError(f'The wrapper command {words[0]} was not found.')
        # The game starts in its own folder, where a relative path would point elsewhere.
        words[0] = os.path.abspath(found)
    gamemoderun = find_program('gamemoderun') if options.gamemode else None
    if gamemoderun:
        # Without it the settings show GameMode as off, so the game starts without it too.
        words.append(os.path.abspath(gamemoderun))
    return words
