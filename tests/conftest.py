"""Every test runs with its own configuration, data and cache folders."""
import pytest


@pytest.fixture(autouse=True)
def private_folders(tmp_path, monkeypatch):
    # The user's real settings, sign-in and caches are never read or written by a test.
    for name in ['XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'APPDATA', 'LOCALAPPDATA']:
        monkeypatch.setenv(name, str(tmp_path / 'home' / name.lower()))
