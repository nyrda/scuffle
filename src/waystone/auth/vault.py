"""The renewable Battle.net session, protected by the platform's DPAPI.

Files are owner-only. Plaintext tickets exist only in memory and in the
bridge's captured pipes.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from .. import system
from ..library import atomic_bytes


class Vault:
    def __init__(self, directory: Path, bridge):
        self.directory, self.bridge = Path(directory), bridge
        self.session = self.directory / 'session.dpapi'
        self.token = self.directory / 'game-token.dpapi'
        self.history = self.directory / 'history.jsonl'

    def _prepare(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def has_session(self) -> bool:
        return self.session.is_file()

    def load_session(self) -> bytes:
        return self.bridge.unprotect(self.session.read_bytes())

    def _save(self, path: Path, value: bytes) -> None:
        self._prepare()
        protected = self.bridge.protect(value)
        atomic_bytes(path, protected)
        if not system.WINDOWS:
            path.chmod(0o600)

    def save_session(self, ticket: bytes) -> None:
        self._save(self.session, ticket)

    def save_game_token(self, ticket: bytes) -> None:
        self._save(self.token, ticket)

    def forget(self) -> None:
        self.session.unlink(missing_ok=True)
        self.token.unlink(missing_ok=True)
        self.record('signed_out')

    def device_id(self) -> bytes:
        return self.bridge.device_id()

    @contextmanager
    def locked(self):
        """Serialize renewals, since each one consumes the saved ticket."""
        self._prepare()
        try:
            with system.locked_file(self.directory / 'session.lock', blocking=False):
                yield self
        except system.LockBusy:
            raise RuntimeError('Another launch is renewing the Battle.net session') from None

    def record(self, event: str, detail: str | None = None) -> None:
        """Append a credential event. It contains no credentials."""
        self._prepare()
        entry = {'time': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'event': event}
        if detail:
            entry['detail'] = detail
        fd = os.open(self.history, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'a') as handle:
            handle.write(json.dumps(entry) + '\n')

    def last(self, event: str) -> str | None:
        try:
            lines = self.history.read_text().splitlines()
        except FileNotFoundError:
            return None
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get('event') == event:
                return entry.get('time')
        return None
