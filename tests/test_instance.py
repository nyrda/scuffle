"""One window per user: a later start hands its request to the process that holds the window."""
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest

from scuffle import cli, instance
from scuffle.library import LibraryError


def test_a_later_start_hands_its_request_to_the_window_and_waits_for_the_game():
    # A socket left by a window that crashed is in the way of the next one.
    path, family = instance.address()
    if family == 'AF_UNIX':
        Path(path).write_text('stale')
    window = instance.claim({'show': True})
    assert window is not None
    received, replies = [], []

    def handler(request, reply):
        received.append(request)
        reply.send(ok=True)
        replies.append(reply)

    window.serve(handler)
    try:
        assert instance.claim({'play': 'wow', 'mode': None, 'wait': False}) is None
        assert received == [{'play': 'wow', 'mode': None, 'wait': False}]

        # With wait the start stays until the window says the game has exited, as Steam needs.
        done = threading.Event()
        waiting = threading.Thread(target=lambda: (instance.claim({'play': 'wow', 'wait': True}), done.set()))
        waiting.start()
        while len(received) < 2:
            done.wait(0.05)
        assert not done.wait(0.3)
        replies[-1].send(finished=True)
        assert done.wait(5)
        waiting.join()
    finally:
        window.close()
    # The lock went with the window, so the next start becomes the window.
    again = instance.claim({'show': True})
    assert again is not None
    again.close()


def test_a_start_without_the_key_is_turned_away():
    window = instance.claim({'show': True})
    received = []
    window.serve(lambda request, reply: received.append(request))
    try:
        key = instance.key()
        Path(window.handle.name).with_name('window.key').write_bytes(b'not the key')
        assert not instance.hand_over({'play': 'wow'})
        Path(window.handle.name).with_name('window.key').write_bytes(key)
        assert received == []
    finally:
        window.close()


def test_a_request_the_window_refuses_ends_with_its_reason():
    window = instance.claim({'show': True})
    window.serve(lambda request, reply: (reply.send(error='Retail has no game mode \'sod\'.'), reply.close()))
    try:
        with pytest.raises(instance.InstanceError, match='no game mode'):
            instance.hand_over({'play': 'wow', 'mode': 'sod'})
    finally:
        window.close()


@pytest.mark.skipif(sys.platform == 'win32', reason='the stand-in game is a Linux process')
def test_a_waiting_shortcut_keeps_waiting_for_the_game_when_the_window_closes_first(tmp_path):
    executable = tmp_path / '_retail_' / 'Wow.exe'
    executable.parent.mkdir()
    # A process that runs the game's file as far as Scuffle can tell, by its first argument.
    game = subprocess.Popen([str(executable), '-c', 'sleep 1; true'], executable=shutil.which('bash'))
    window = instance.claim({'show': True})
    window.serve(lambda request, reply: (reply.send(ok=True, executable=str(executable)), reply.close()))
    try:
        assert instance.hand_over({'play': 'wow', 'wait': True})
        assert game.poll() is not None
    finally:
        game.kill()
        game.wait()
        window.close()


def test_a_command_never_runs_beside_the_window(tmp_path):
    window = instance.claim({'show': True})
    try:
        with pytest.raises(instance.InstanceError, match='window is open'):
            cli.main(['--root', str(tmp_path / 'games'), 'verify', 'wow'])
    finally:
        window.close()
    # Without a window the command runs, and fails only because nothing is installed.
    with pytest.raises(LibraryError, match='not installed'):
        cli.main(['--root', str(tmp_path / 'games'), 'verify', 'wow'])


def test_a_start_at_sign_in_hands_over_to_a_running_window_and_asks_only_for_the_tray():
    pytest.importorskip('PySide6.QtWidgets')
    window = instance.claim({'show': True})
    received = []
    window.serve(lambda request, reply: (received.append(request), reply.send(ok=True), reply.close()))
    try:
        assert cli.main(['--tray']) is None         # handed over, so this process opens no window
        assert received == [{'tray': True}]
    finally:
        window.close()
