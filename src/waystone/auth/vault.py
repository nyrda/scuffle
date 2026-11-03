"""The renewable Battle.net sessions, one per account, protected by the platform's DPAPI.

Files are owner-only. Plaintext tickets exist only in memory and in the
bridge's captured pipes.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from .. import system
from ..library import atomic_bytes, atomic_json

# The key of a session from an older Waystone whose account id was never read. Its first renewal reads it.
UNKNOWN = 'unknown'
BATTLE_TAG = re.compile(r'[^\W\d_]\w{1,15}#[0-9]{3,8}')


def battle_tag(text) -> str | None:
    """text if it has the form of a BattleTag, such as Player#1234. An email never has it."""
    return text if isinstance(text, str) and BATTLE_TAG.fullmatch(text) else None


def login_key(owner) -> str:
    return str(owner) if type(owner) is int and owner > 0 else UNKNOWN


def _is_key(name: str) -> bool:
    return name == UNKNOWN or (re.fullmatch(r'[1-9][0-9]*', name) is not None)


class Login:
    """One Battle.net account's session, label and WoW accounts, in a folder of its own."""

    def __init__(self, vault: 'Vault', directory: Path, key: str):
        self.vault, self.directory, self.key = vault, Path(directory), key

    def __repr__(self) -> str:
        return f'Login({self.key!r})'

    @property
    def session(self) -> Path:
        return self.directory / 'session.dpapi'

    @property
    def account_list(self) -> Path:
        return self.directory / 'accounts.json'

    @property
    def details_file(self) -> Path:
        return self.directory / 'login.json'

    @property
    def id(self) -> int | None:
        """The Battle.net account id, unless this is an older Waystone's session that never read it."""
        return None if self.key == UNKNOWN else int(self.key)

    def details(self) -> dict:
        try:
            data = json.loads(self.details_file.read_text())
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        number = data.get('number')
        return {'number': number if type(number) is int and number > 0 else 1,
                'battle_tag': battle_tag(data.get('battle_tag'))}

    @property
    def number(self) -> int:
        return self.details()['number']

    @property
    def label(self) -> str:
        """A name to show: the BattleTag, or Account 1, Account 2 and so on. Never the email."""
        details = self.details()
        return details['battle_tag'] or f"Account {details['number']}"

    def has_session(self) -> bool:
        return self.session.is_file()

    def load_session(self) -> bytes:
        return self.vault.bridge.unprotect(self.session.read_bytes())

    def save_session(self, ticket: bytes) -> None:
        self.vault._save(self.session, ticket)

    def save_game_token(self, ticket: bytes) -> None:
        self.vault._save(self.vault.token, ticket)

    def device_id(self) -> bytes:
        return self.vault.device_id()

    def claim(self, owner) -> bool:
        """Whether a logon with this login's session signed in to this login's account.

        An older Waystone's session learns its account id here and moves to the folder of that id.
        """
        if self.key != UNKNOWN:
            return owner == self.id and type(owner) is int
        if login_key(owner) != UNKNOWN:
            self.vault._adopt(self, owner)
        return True

    def save_battle_tag(self, tag) -> None:
        """Keep the BattleTag the logon named, for the label. A logon without one keeps the last."""
        if battle_tag(tag) is None:
            return
        data = {'number': self.number, 'battle_tag': tag}
        self.vault._write_json(self.details_file, data)

    def save_accounts(self, owner, accounts: list[dict] | None) -> None:
        """Keep the WoW accounts of this Battle.net login, which owner identifies.

        A failed lookup is not an empty list, so it keeps the last list of the same login.
        """
        known = self.accounts()
        if accounts is None and known and known['owner'] == owner:
            return
        if accounts is None or not isinstance(owner, int):
            self.account_list.unlink(missing_ok=True)
            return
        self.vault._write_json(self.account_list, {'owner': owner, 'game_accounts': accounts})

    def accounts(self) -> dict | None:
        """The saved list as {'owner': id, 'game_accounts': [{'id', 'name', 'region'}]}, if readable."""
        from .bgs import GAME_REGIONS, launch_name
        try:
            data = json.loads(self.account_list.read_text())
            owner, listed = data['owner'], data['game_accounts']
            usable = [{'id': a['id'], 'name': a['name'], 'region': a['region']} for a in listed
                      if type(a['id']) is int and launch_name(a['name']) and a['region'] in GAME_REGIONS.values()]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if type(owner) is not int or len(usable) != len(listed):
            return None
        # A list of another account is never shown or used as this one's.
        if self.id is not None and owner != self.id:
            return None
        return {'owner': owner, 'game_accounts': usable}


class Vault:
    def __init__(self, directory: Path, bridge):
        self.directory, self.bridge = Path(directory), bridge
        self.logins_folder = self.directory / 'logins'
        self.active_file = self.directory / 'active'
        self.token = self.directory / 'game-token.dpapi'
        self.history = self.directory / 'history.jsonl'
        # Where an older Waystone kept its one session and WoW account list.
        self.legacy_session = self.directory / 'session.dpapi'
        self.legacy_accounts = self.directory / 'accounts.json'
        self._held = False

    def _prepare(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _save(self, path: Path, value: bytes) -> None:
        self._prepare()
        protected = self.bridge.protect(value)
        atomic_bytes(path, protected)
        if not system.WINDOWS:
            path.chmod(0o600)

    def _write_json(self, path: Path, value) -> None:
        atomic_json(path, value)
        if not system.WINDOWS:
            path.chmod(0o600)

    # The logins

    def logins(self) -> list[Login]:
        """Every login with a session, by number. An older Waystone's session counts too."""
        if (self.legacy_session.is_file() or self.legacy_accounts.is_file()) and not self._held:
            try:
                with self.locked():
                    pass        # taking the lock moves it into a login folder, or deletes what a move left
            except RuntimeError:
                pass            # another process holds the lock, so it is listed in place below
        return self.listed()

    def listed(self) -> list[Login]:
        """The logins as they are on disk, without the lock, so reading them never moves a session."""
        try:
            folders = [f for f in self.logins_folder.iterdir() if _is_key(f.name) and f.is_dir()]
        except OSError:
            folders = []
        found = [login for login in (Login(self, folder, folder.name) for folder in folders) if login.has_session()]
        if self.legacy_session.is_file():
            legacy = self._legacy()
            found = [login for login in found if login.key != legacy.key] + [legacy]
        return sorted(found, key=lambda login: (login.number, login.key))

    def active(self) -> Login | None:
        """The login every Play uses: the one chosen last, or the first by number."""
        return self.pick(self.logins())

    def pick(self, logins: list[Login]) -> Login | None:
        """The login chosen last among logins, or the first by number."""
        try:
            key = self.active_file.read_text().strip()
        except OSError:
            key = None
        return next((login for login in logins if login.key == key), logins[0] if logins else None)

    def has_session(self) -> bool:
        return self.active() is not None

    def activate(self, key: str) -> None:
        """Make another login active. Under the lock, so a launch that started keeps the login it read."""
        with self.locked():
            if not any(login.key == key for login in self.logins()):
                raise RuntimeError('That Battle.net account is no longer signed in')
            self._write_active(key)

    def _write_active(self, key: str) -> None:
        self._prepare()
        atomic_bytes(self.active_file, key.encode())
        if not system.WINDOWS:
            self.active_file.chmod(0o600)

    @contextmanager
    def adding(self, owner):
        """The login a new sign-in saves its session to. It becomes active once it has one.

        The same account signed in again keeps its folder, number and WoW accounts.
        """
        key = login_key(owner)
        with self.locked(blocking=True):
            login = Login(self, self.logins_folder / key, key)
            fresh = not login.directory.is_dir()
            self._make_folder(login)
            try:
                yield login
            except BaseException:
                if fresh and not login.has_session():
                    shutil.rmtree(login.directory, ignore_errors=True)
                raise
            if login.has_session():
                self._write_active(key)

    def _make_folder(self, login: Login) -> None:
        self._prepare()
        self.logins_folder.mkdir(mode=0o700, exist_ok=True)
        login.directory.mkdir(mode=0o700, exist_ok=True)
        # Durable before a migration moves the only session into it.
        system.sync_directory(self.directory)
        system.sync_directory(self.logins_folder)
        if not login.details_file.is_file():
            # The lowest free number, kept for good, so a sign-out never renames another login.
            taken = {other.number for other in self.logins() if other.key != login.key}
            number = next(n for n in range(1, len(taken) + 2) if n not in taken)
            self._write_json(login.details_file, {'number': number, 'battle_tag': None})

    def _adopt(self, login: Login, owner: int) -> None:
        """Move an older Waystone's session to the folder of the account id its renewal read."""
        key = login_key(owner)
        target = self.logins_folder / key
        if login.directory.parent != self.logins_folder:
            # Still where an older Waystone kept it. The migration keys it by the list this renewal saves.
            return
        with self.locked():
            trash = None
            if target.exists():
                # The same account signed in again meanwhile. The session being renewed is the one in use.
                trash = self.logins_folder / f'.old-{key}-{uuid.uuid4().hex}'
                os.replace(target, trash)
            try:
                os.replace(login.directory, target)
            except OSError:
                if trash is not None:
                    os.replace(trash, target)   # the renewal failed, so the other session stays usable
                raise
            system.sync_directory(self.logins_folder)
            login.directory, login.key = target, key
            try:
                active = self.active_file.read_text().strip()
            except OSError:
                active = None
            if active == UNKNOWN:
                self._write_active(key)
            if trash is not None:
                shutil.rmtree(trash, ignore_errors=True)

    def remove(self, login: Login) -> None:
        """Delete one login's session and folder, and nothing of any other login."""
        with self.locked():
            current = next((other for other in self.logins() if other.key == login.key), None)
            if current is None:
                return
            if current.directory.parent != self.logins_folder:
                # An older Waystone's session that could not move yet. A folder of the same key goes too.
                current.session.unlink(missing_ok=True)
                current.account_list.unlink(missing_ok=True)
                current = Login(self, self.logins_folder / current.key, current.key)
            # The session goes first, so a folder a failed removal leaves behind is no login.
            current.session.unlink(missing_ok=True)
            shutil.rmtree(current.directory, ignore_errors=True)

    def forget(self, login: Login | None = None) -> None:
        """Sign out of one login, the active one unless another is named."""
        with self.locked():
            login = login or self.active()
            if login is not None:
                self.remove(login)
            # No launch holds the lock now, so no game is waiting for this ticket.
            self.token.unlink(missing_ok=True)
        self.record('signed_out')

    # The migration from one session

    def _legacy(self) -> Login:
        """An older Waystone's session in place, keyed by the owner of its WoW account list."""
        listed = Login(self, self.directory, UNKNOWN).accounts()
        return Login(self, self.directory, login_key(listed['owner'] if listed else None))

    def _migrate(self) -> None:
        """Move an older Waystone's session into a login folder. The session's rename is the commit.

        A crash before it leaves the old session where it was, and the next lock repeats this. A crash
        after it leaves only the old WoW account list, which the next lock deletes.
        """
        for leftover in self.logins_folder.glob('.old-*'):
            shutil.rmtree(leftover, ignore_errors=True)
        if not self.legacy_session.is_file():
            self.legacy_accounts.unlink(missing_ok=True)
            return
        legacy = self._legacy()
        target = Login(self, self.logins_folder / legacy.key, legacy.key)
        self._make_folder(target)
        listed = legacy.accounts()
        if listed:
            self._write_json(target.account_list, listed)
        os.replace(self.legacy_session, target.session)
        system.sync_directory(target.directory)
        system.sync_directory(self.directory)
        self.legacy_accounts.unlink(missing_ok=True)
        self.record('migrated')

    def device_id(self) -> bytes:
        return self.bridge.device_id()

    @contextmanager
    def locked(self, *, blocking: bool = False):
        """Serialize renewals, since each one consumes the saved ticket, and every change to the logins.

        An older Waystone renews under the same lock file, so its session moves only between its renewals.
        """
        if self._held:
            yield self
            return
        self._prepare()
        try:
            with system.locked_file(self.directory / 'session.lock', blocking=blocking):
                self._held = True
                try:
                    try:
                        self._migrate()
                    except OSError:
                        pass        # the older session stays where it was and works there until the next try
                    yield self
                finally:
                    self._held = False
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
