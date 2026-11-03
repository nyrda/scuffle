"""Closing a running game, or ending it at once, without touching its launchers."""
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from scuffle import system
from scuffle.runtime import game
from scuffle.runtime.game import ASKED, NO_WINDOW, NOT_RUNNING, LinuxRuntime, RuntimeFailure

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='Linux processes')


@pytest.fixture
def started():
    processes = []

    def start(first, *arguments, cwd):
        # The first argument is chosen freely, as Wine shows a game by its Windows path.
        process = subprocess.Popen([first, '-c', 'import time; time.sleep(60)', *arguments],
                                   executable=sys.executable, cwd=cwd)
        processes.append(process)
        return process
    yield start
    for process in processes:
        process.kill()
        process.wait()


def wait_for(check):
    deadline = time.monotonic() + 5
    while not check():
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_the_game_is_found_by_its_name_and_folder_and_its_launchers_are_not(tmp_path, started):
    retail, forever = tmp_path / '_retail_', tmp_path / '_forever_'
    retail.mkdir(), forever.mkdir()
    game_process = started('Z:\\games\\_retail_\\Wow.exe', cwd=retail)
    # The bridge and umu-run name the game in their arguments. Forever's game is also Wow.exe.
    started('bridge.exe', 'Z:\\games\\_retail_\\Wow.exe', cwd=retail)
    started('S:\\other\\_forever_\\Wow.exe', cwd=forever)
    wait_for(lambda: system.game_processes(retail / 'Wow.exe'))
    assert system.game_processes(retail / 'Wow.exe') == [game_process.pid]


def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(game, 'wine_wrapper', lambda: [])        # only bridge calls need steam-run
    proton = SimpleNamespace(wine=tmp_path / 'wine', path=tmp_path / 'proton')
    return LinuxRuntime(tmp_path / 'bridge.exe', tmp_path / 'prefix', proton, tmp_path / 'umu-run')


def test_force_quit_ends_the_game_process_itself(tmp_path, started, monkeypatch):
    folder = tmp_path / '_retail_'
    folder.mkdir()
    process = started('Z:\\games\\_retail_\\Wow.exe', cwd=folder)
    wait_for(lambda: system.game_processes(folder / 'Wow.exe'))
    assert runtime(tmp_path, monkeypatch).close(folder / 'Wow.exe', system.game_processes(folder / 'Wow.exe'), force=True) == ASKED
    assert process.wait(timeout=5) == -9


def test_force_quit_spares_a_process_that_took_over_the_game_pid(tmp_path, started, monkeypatch):
    folder = tmp_path / '_retail_'
    folder.mkdir()
    # The pid was the game's when it was found. Now another program runs under it.
    other = started('editor', cwd=tmp_path)
    runtime(tmp_path, monkeypatch).close(folder / 'Wow.exe', [other.pid], force=True)
    time.sleep(0.2)
    assert other.poll() is None


def test_nothing_to_close_when_the_game_is_gone(tmp_path, monkeypatch):
    assert runtime(tmp_path, monkeypatch).close(tmp_path / 'Wow.exe', []) == NOT_RUNNING


@pytest.mark.parametrize('code, expected', [(0, ASKED), (3, NO_WINDOW), (4, NOT_RUNNING)])
def test_closing_runs_the_bridge_in_the_game_session_with_its_wine(tmp_path, monkeypatch, code, expected):
    # Proton runs the game with its wine preloader. Its bin/wine is three folders up.
    files = tmp_path / 'proton' / 'files'
    (files / 'lib/wine/x86_64-unix').mkdir(parents=True)
    (files / 'bin').mkdir()
    (files / 'bin/wine').write_text('')
    monkeypatch.setattr(game.os, 'readlink', lambda path: str(files / 'lib/wine/x86_64-unix/wine64-preloader'))
    monkeypatch.setattr(game.shutil, 'which', lambda name: '/usr/bin/nsenter' if name == 'nsenter' else None)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs['env']))
        return SimpleNamespace(returncode=code)
    monkeypatch.setattr(game.subprocess, 'run', run)
    (tmp_path / 'bridge.exe').write_text('')          # seen through /proc/<pid>/root, as the game sees it
    pid = os.getpid()
    assert runtime(tmp_path, monkeypatch).close(Path('/games/_retail_/Wow.exe'), [pid]) == expected
    (command, env), = calls
    assert command[:7] == ['/usr/bin/nsenter', '-t', str(pid), '-U', '-m', '--preserve-credentials', '--wd=/']
    assert command[8:] == [str(files / 'bin/wine'), str(tmp_path / 'bridge.exe'), '--close-game',
                           'Z:\\games\\_retail_\\Wow.exe']
    # Only the game's Wine and display variables reach its session, read from the game itself.
    assert env['WOW_BROKER_PIPE'] == '1' and env['PATH'] and 'HOME' not in env


def test_a_bridge_the_container_cannot_see_points_at_force_quit(tmp_path, monkeypatch):
    files = tmp_path / 'proton' / 'files'
    (files / 'lib/wine/x86_64-unix').mkdir(parents=True)
    (files / 'bin').mkdir()
    (files / 'bin/wine').write_text('')
    monkeypatch.setattr(game.os, 'readlink', lambda path: str(files / 'lib/wine/x86_64-unix/wine64-preloader'))
    monkeypatch.setattr(game.shutil, 'which', lambda name: '/usr/bin/nsenter')
    monkeypatch.setattr(game.subprocess, 'run', lambda *a, **k: pytest.fail('ran without a visible bridge'))
    with pytest.raises(RuntimeFailure, match='cannot see the Scuffle bridge'):
        runtime(tmp_path, monkeypatch).close(tmp_path / 'Wow.exe', [os.getpid()])


def test_without_nsenter_closing_points_at_force_quit(tmp_path, monkeypatch):
    monkeypatch.setattr(game.shutil, 'which', lambda name: None)
    with pytest.raises(RuntimeFailure, match='Shift'):
        runtime(tmp_path, monkeypatch).close(tmp_path / 'Wow.exe', [os.getpid()])
