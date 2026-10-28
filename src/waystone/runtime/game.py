"""Start a game client with its credential through the bridge."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

from .bridge import Bridge, NativeBridge, WineBridge, windows_path
from .proton import Proton, wine_wrapper


class RuntimeFailure(RuntimeError):
    pass


class GameRuntime:
    bridge: Bridge

    def prepare(self) -> None:
        """Make the runtime usable before the first bridge call."""

    def start(self, executable: Path, arguments: list[str], token: Path, ack: Path) -> subprocess.Popen:
        raise NotImplementedError


def _launch_arguments(bridge: Path, executable: Path, arguments: list[str], token: Path, ack: Path) -> list[str]:
    return [str(bridge), '--launch-protected-file', windows_path(token), windows_path(ack),
            windows_path(executable), *arguments]


class WindowsRuntime(GameRuntime):
    def __init__(self, bridge: Path):
        self.bridge = NativeBridge(bridge)

    def start(self, executable, arguments, token, ack):
        command = _launch_arguments(self.bridge.executable, executable, arguments, token, ack)
        env = os.environ | {'WOW_BROKER_PIPE': '1'}
        return subprocess.Popen(command, env=env, cwd=executable.parent, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))


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

    def start(self, executable, arguments, token, ack):
        env = self.environment() | {'WOW_BROKER_PIPE': '1'}
        command = [str(self.umu), *_launch_arguments(self.bridge_executable, executable, arguments, token, ack)]
        return subprocess.Popen(command, env=env, cwd=executable.parent, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
