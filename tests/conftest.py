"""Every test runs with its own configuration, data and cache folders."""
import os
from pathlib import Path

import pytest

FOLDERS = ['XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'APPDATA', 'LOCALAPPDATA']
# The user's own folders, found before any test changes the environment.
REAL = [Path(os.environ.get(name, Path.home() / default)) / 'waystone' for name, default in
        [('XDG_CONFIG_HOME', '.config'), ('XDG_DATA_HOME', '.local/share'), ('XDG_CACHE_HOME', '.cache')]]


def snapshot() -> dict:
    return {path: path.stat().st_mtime_ns for folder in REAL if folder.is_dir()
            for path in [folder, *folder.iterdir()]}


BEFORE = snapshot()


def pytest_sessionfinish(session, exitstatus):
    # A leak once overwrote the user's settings. Fail the run loudly if anything touches them.
    changed = sorted(str(path) for path, stamp in snapshot().items() if BEFORE.get(path) != stamp)
    if changed:
        session.exitstatus = 1
        print(f"\nA test wrote into the user's own Waystone folders: {', '.join(changed)}")


@pytest.fixture(autouse=True, scope='session')
def private_session_folders(tmp_path_factory):
    # Teardown after the last test, such as closing windows a test left open, saves settings
    # too. It must not reach the user's folders either.
    home = tmp_path_factory.mktemp('session')
    with pytest.MonkeyPatch.context() as patch:
        for name in FOLDERS:
            patch.setenv(name, str(home / name.lower()))
        yield


@pytest.fixture(autouse=True)
def private_folders(tmp_path, monkeypatch):
    # The user's real settings, sign-in and caches are never read or written by a test.
    for name in FOLDERS:
        monkeypatch.setenv(name, str(tmp_path / 'home' / name.lower()))
