"""Every test runs with its own configuration, data and cache folders."""
import pytest

FOLDERS = ['XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'APPDATA', 'LOCALAPPDATA']


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
