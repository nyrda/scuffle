"""One Waystone window per user. A later start hands its request to the window that runs.

The window's process holds a lock file for its whole life and answers on a local socket,
a named pipe on Windows. A shortcut or a second start that finds the lock taken sends its
request there instead of opening the library a second time. The lock goes with the
process, so a crashed window never blocks the next start.
"""
from __future__ import annotations

import hashlib
import json
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Listener
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
from typing import Callable

from . import settings, system

ANSWER_TIMEOUT = 30      # seconds a window gets to accept a request
CLAIM_TIMEOUT = 30       # seconds a start waits for a window that holds the lock but does not answer yet


class InstanceError(RuntimeError):
    pass


def address() -> tuple[str, str]:
    """The window's socket for this data folder, so a separate folder, as in tests, has its own."""
    digest = hashlib.sha256(str(settings.data_dir().absolute()).encode()).hexdigest()[:16]
    if system.WINDOWS:
        return rf'\\.\pipe\waystone-{digest}', 'AF_PIPE'
    folder = os.environ.get('XDG_RUNTIME_DIR') or tempfile.gettempdir()
    return os.path.join(folder, f'waystone-{digest}.sock'), 'AF_UNIX'


def key(create: bool = False) -> bytes | None:
    """The secret both sides prove they know, readable only by the user."""
    path = settings.data_dir() / 'window.key'
    try:
        return path.read_bytes()
    except FileNotFoundError:
        if not create:
            return None
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | system.O_BINARY, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(value)
    return value


class Reply:
    """The way back to the start that sent a request."""

    def __init__(self, connection):
        self.connection = connection

    def send(self, **message) -> None:
        try:
            self.connection.send_bytes(json.dumps(message).encode())
        except (OSError, ValueError):
            pass        # the start is gone, as when Steam stopped waiting

    def close(self) -> None:
        self.connection.close()


class Window:
    """The claim of the process that shows the window."""

    def __init__(self, handle):
        self.handle = handle
        self.listener = None
        self.closed = False

    def serve(self, handler: Callable[[dict, Reply], None]) -> None:
        """Accept requests on a thread of their own. handler runs on that thread."""
        path, family = address()
        if family == 'AF_UNIX':
            # Left by a window that crashed. The lock shows that none runs now.
            Path(path).unlink(missing_ok=True)
        self.listener = Listener(path, family, authkey=key(create=True))
        threading.Thread(target=self._accept, args=(handler,), name='waystone-instance', daemon=True).start()

    def _accept(self, handler) -> None:
        while not self.closed:
            try:
                connection = self.listener.accept()
            except (OSError, EOFError, AuthenticationError):
                continue
            try:
                if not connection.poll(ANSWER_TIMEOUT):
                    raise EOFError
                request = json.loads(connection.recv_bytes(4096))
                if not isinstance(request, dict):
                    raise ValueError
            except (OSError, EOFError, ValueError):
                connection.close()
                continue
            handler(request, Reply(connection))

    def close(self) -> None:
        self.closed = True
        if self.listener is not None:
            self.listener.close()
        system.unlock(self.handle)
        self.handle.close()


def claim(request: dict) -> Window | None:
    """Become the window's process, or hand request to the window that runs and return None."""
    path = settings.data_dir() / 'window.lock'
    deadline = time.monotonic() + CLAIM_TIMEOUT
    while True:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open('a+b')
        try:
            system.lock(handle, blocking=False)
            return Window(handle)
        except system.LockBusy:
            handle.close()
        if hand_over(request):
            return None
        # The window took the lock and is still starting.
        if time.monotonic() > deadline:
            raise InstanceError('Waystone is starting in another process and does not answer. Try again.')
        time.sleep(0.2)


def hand_over(request: dict) -> bool:
    """Send request to the window that runs. False when none answers.

    With wait in the request, this returns once the window says the game is gone.
    """
    secret = key()
    if secret is None:
        return False
    path, family = address()
    try:
        connection = Client(path, family, authkey=secret)
    except (OSError, EOFError, AuthenticationError):
        return False
    with connection:
        try:
            connection.send_bytes(json.dumps(request).encode())
            if not connection.poll(ANSWER_TIMEOUT):
                raise InstanceError('The Waystone window does not answer.')
            answer = json.loads(connection.recv_bytes(4096))
        except (OSError, EOFError, ValueError):
            raise InstanceError('The Waystone window closed before it took the request.') from None
        if answer.get('error'):
            raise InstanceError(answer['error'])
        if request.get('wait'):
            try:
                connection.recv_bytes(4096)
            except (OSError, EOFError):
                pass        # the window closed, which ends the wait too
    return True
