"""Start a game client with its credential through the bridge."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess

from .. import system
from .bridge import Bridge, NativeBridge, WineBridge, windows_path
from .proton import Proton, wine_wrapper


class RuntimeFailure(RuntimeError):
    pass


# What closing a game did. The bridge's --close-game exits with 0, 3 or 4.
ASKED, NO_WINDOW, NOT_RUNNING = 'asked', 'no window', 'not running'
CLOSE_RESULTS = {0: ASKED, 3: NO_WINDOW, 4: NOT_RUNNING}


class GameRuntime:
    bridge: Bridge

    def prepare(self) -> None:
        """Make the runtime usable before the first bridge call."""

    def start(self, executable: Path, arguments: list[str], token: Path, ack: Path, *,
              environment: dict[str, str] | None = None, wrapper: list[str] = (),
              account: tuple[str, str] | None = None) -> subprocess.Popen:
        """Start the game. The environment and wrapper commands apply on Linux only.

        account is the WoW account's name and region. Without one the game asks for it.
        """
        raise NotImplementedError

    def close(self, executable: Path, processes: list[int], *, force: bool = False) -> str:
        """Ask the game to close as its window's close button does, or end it at once with force."""
        raise NotImplementedError


def _launch_arguments(bridge: Path, executable: Path, arguments: list[str], token: Path, ack: Path,
                      account: tuple[str, str] | None = None) -> list[str]:
    chosen = ['--game-account', *account] if account else []
    return [str(bridge), '--launch-protected-file', windows_path(token), windows_path(ack),
            *chosen, windows_path(executable), *arguments]


class WindowsRuntime(GameRuntime):
    def __init__(self, bridge: Path):
        self.bridge = NativeBridge(bridge)

    def start(self, executable, arguments, token, ack, *, environment=None, wrapper=(), account=None):
        command = _launch_arguments(self.bridge.executable, executable, arguments, token, ack, account)
        env = os.environ | {'WOW_BROKER_PIPE': '1'}
        return subprocess.Popen(command, env=env, cwd=executable.parent, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))

    def close(self, executable, processes, *, force=False):
        command = [str(self.bridge.executable), '--close-game', str(executable), *(['--force'] if force else [])]
        result = subprocess.run(command, env=os.environ | {'WOW_BROKER_PIPE': '1'}, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if result.returncode not in CLOSE_RESULTS:
            raise RuntimeFailure('The game could not be asked to close.')
        return CLOSE_RESULTS[result.returncode]


class LinuxRuntime(GameRuntime):
    """Proton through umu-run, with one Waystone prefix for every client."""

    GAMEID = 'umu-battlenet'

    def __init__(self, bridge: Path, prefix: Path, proton: Proton, umu: Path):
        self.prefix, self.proton, self.umu = Path(prefix), proton, Path(umu)
        self.bridge = WineBridge(bridge, self.prefix, [*wine_wrapper(), str(proton.wine)])
        self.bridge_executable = Path(bridge)

    def environment(self) -> dict:
        env = os.environ.copy()
        # Some launchers export this globally. It makes UMU re-enter a running
        # container of the same prefix and would tie the game to it.
        env.pop('UMU_CONTAINER_NSENTER', None)
        env.update(WINEPREFIX=str(self.prefix), PROTONPATH=str(self.proton.path), GAMEID=self.GAMEID,
                   WINEDEBUG='-all', UMU_LOG='warn')
        return env

    def prepare(self):
        if (self.prefix / 'system.reg').is_file():
            return
        self.prefix.mkdir(parents=True, exist_ok=True)
        # UMU's createprefix mode builds a Proton prefix without running a program.
        result = subprocess.run([str(self.umu), 'createprefix'], env=self.environment(),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=600)
        if not (self.prefix / 'system.reg').is_file():
            message = result.stderr.decode(errors='replace')[-400:]
            raise RuntimeFailure(f'Proton could not create the Wine prefix: {message}')

    def game_environment(self, extra: dict[str, str] | None = None) -> dict:
        """The game's environment: Waystone's defaults, then the user's, then what the runtime needs."""
        extra = dict(extra or {})
        env = self.environment()
        if extra.get('PROTON_LOG', '0') not in ('', '0') and 'WINEDEBUG' not in extra:
            # Proton picks its log channels only when WINEDEBUG is unset.
            env.pop('WINEDEBUG', None)
        env.update(extra)
        env.pop('UMU_CONTAINER_NSENTER', None)
        env.update(WINEPREFIX=str(self.prefix), PROTONPATH=str(self.proton.path), GAMEID=self.GAMEID,
                   WOW_BROKER_PIPE='1')
        return env

    def start(self, executable, arguments, token, ack, *, environment=None, wrapper=(), account=None):
        # Wrappers such as gamemoderun or gamescope go around umu-run, and so around the game.
        # Prefix creation and bridge calls never run through them.
        env = self.game_environment(environment)
        command = [*wrapper, str(self.umu),
                   *_launch_arguments(self.bridge_executable, executable, arguments, token, ack, account)]
        return subprocess.Popen(command, env=env, cwd=executable.parent, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    # The game's own Wine session lives in its container, whose wineserver only its mount
    # namespace can reach. The bridge joins it there with the game's Wine and variables.
    JOINED = ('WINEPREFIX', 'WINEDLLPATH', 'WINEDLLOVERRIDES', 'WINEFSYNC', 'WINEESYNC', 'PATH',
              'LD_LIBRARY_PATH', 'DISPLAY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR')

    def close(self, executable, processes, *, force=False):
        if not processes:
            return NOT_RUNNING
        if force:
            # Ends the game at once. Its bridge and umu-run then see it gone and exit too.
            # A pidfd holds on to the very process, so a pid that a new process took over
            # since the game was found fails the check and is never signalled.
            for pid in processes:
                try:
                    handle = os.pidfd_open(pid)
                except ProcessLookupError:
                    continue
                try:
                    if system.runs(pid, executable):
                        signal.pidfd_send_signal(handle, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                finally:
                    os.close(handle)
            return ASKED
        nsenter = shutil.which('nsenter')
        if nsenter is None:
            raise RuntimeFailure('Closing the game needs nsenter from util-linux. Hold Shift to force quit.')
        pid = processes[0]
        try:
            variables = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
            # The game runs Proton's wine preloader from files/lib/wine/<arch>. Its bin/wine is above.
            loader = Path(os.readlink(f'/proc/{pid}/exe'))
        except OSError:
            return NOT_RUNNING
        wine = next((folder / 'bin' / 'wine' for folder in loader.parents
                     if Path(f'/proc/{pid}/root{folder}/bin/wine').is_file()), None)
        if wine is None:
            raise RuntimeFailure('The game\'s Wine was not found. Hold Shift to force quit.')
        if not Path(f'/proc/{pid}/root{self.bridge_executable}').is_file():
            # The container replaces some folders, such as /tmp.
            raise RuntimeFailure('The game\'s container cannot see the Waystone bridge. Hold Shift to force quit.')
        env = {name: value for name, _, value in (v.decode(errors='replace').partition('=') for v in variables)
               if name in self.JOINED}
        env.update(WINEDEBUG='-all', WOW_BROKER_PIPE='1')
        command = [nsenter, '-t', str(pid), '-U', '-m', '--preserve-credentials', '--wd=/', '--',
                   str(wine), str(self.bridge_executable), '--close-game', windows_path(executable)]
        try:
            result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=60)
        except subprocess.TimeoutExpired:
            raise RuntimeFailure('The game did not answer. Hold Shift to force quit.') from None
        if result.returncode not in CLOSE_RESULTS:
            raise RuntimeFailure('The game could not be asked to close. Hold Shift to force quit.')
        return CLOSE_RESULTS[result.returncode]

