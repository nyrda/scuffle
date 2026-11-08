"""Scuffle replaces itself from a GitHub release, against a local server in GitHub's place."""
import hashlib
import http.server
import json
import os
import sys
import threading

import pytest

from scuffle import cli, selfupdate
from scuffle.library import LibraryError

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='packaging/windows/test-self-update.ps1 runs it there')

CURRENT, NEW = '1.0.0', '2.0.0'
ASSET = f'Scuffle-{NEW}-x86_64.AppImage'


def program_text(version=NEW, code=0):
    return f'#!/bin/sh\necho "scuffle {version}"\nexit {code}\n'.encode()


class GitHub:
    """Serves one release the way the API and the download addresses do."""

    def __init__(self, monkeypatch, body=program_text(), listed_sum=None, listed_name=ASSET, size=None, cut_short=False, garbled=False, **release):
        self.body, self.requests = body, []
        digest = listed_sum or hashlib.sha256(body).hexdigest()
        files = {'/SHA256SUMS': f'{digest}  {listed_name}\n'.encode(), f'/{ASSET}': body}
        github = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                github.requests.append(self.path)
                if garbled and self.path == f'/{ASSET}':
                    self.wfile.write(b'not an HTTP answer\r\n\r\n')
                    self.close_connection = True
                    return
                data = files.get(self.path)
                if self.path == '/latest':
                    data = json.dumps(github.description).encode()
                self.send_response(200 if data is not None else 404)
                # A connection that ends early promises more than it sends.
                extra = 1000 if cut_short and self.path == f'/{ASSET}' else 0
                self.send_header('Content-Length', str(len(data or b'') + extra))
                self.end_headers()
                self.wfile.write(data or b'')

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{self.server.server_address[1]}'
        monkeypatch.setattr(selfupdate, 'API', f'{base}/latest')
        monkeypatch.setattr(selfupdate, 'DOWNLOADS', f'{base}/')
        monkeypatch.setattr(selfupdate, 'current', lambda: CURRENT)
        self.description = {
            'tag_name': f'v{NEW}', 'draft': False, 'prerelease': False,
            'html_url': f'{selfupdate.PAGES}tag/v{NEW}',
            'assets': [{'name': ASSET, 'size': len(body) if size is None else size,
                        'browser_download_url': f'{base}/{ASSET}'},
                       {'name': 'SHA256SUMS', 'size': 90, 'browser_download_url': f'{base}/SHA256SUMS'}],
            **release}


@pytest.fixture
def running(tmp_path, monkeypatch):
    path = tmp_path / 'Scuffle-1.0.0-x86_64.AppImage'
    path.write_bytes(program_text(CURRENT))
    path.chmod(0o755)
    monkeypatch.setenv('APPIMAGE', str(path))
    return path


def siblings(path):
    return sorted(p.name for p in path.parent.iterdir())


def test_a_newer_release_is_found_with_what_this_installation_needs(monkeypatch, running):
    GitHub(monkeypatch)
    release = selfupdate.latest()
    assert release.version == NEW and release.asset == ASSET and release.page.endswith(f'/tag/v{NEW}')


@pytest.mark.parametrize('field', [
    {'tag_name': f'v{CURRENT}'}, {'tag_name': 'v0.9.9'}, {'tag_name': 'nightly'}, {'prerelease': True}, {'draft': True},
    {'html_url': 'https://example.com/elsewhere'},
])
def test_only_a_newer_published_release_counts(monkeypatch, running, field):
    GitHub(monkeypatch, **field)
    assert selfupdate.latest() is None


def test_versions_compare_as_numbers(monkeypatch, running):
    GitHub(monkeypatch, tag_name='v1.0.10')
    monkeypatch.setattr(selfupdate, 'current', lambda: '1.0.9')
    assert selfupdate.latest().version == '1.0.10'


def test_a_file_from_elsewhere_is_announced_but_never_offered_for_install(monkeypatch, running):
    github = GitHub(monkeypatch)
    github.description['assets'][0]['browser_download_url'] = 'https://example.com/Scuffle.AppImage'
    release = selfupdate.latest()
    assert release.version == NEW and not release.asset


def test_an_installation_that_cannot_replace_itself_only_hears_about_a_release(monkeypatch):
    GitHub(monkeypatch)
    monkeypatch.delenv('APPIMAGE', raising=False)
    release = selfupdate.latest()
    assert release.version == NEW and not release.asset and not selfupdate.can_install()


def test_an_update_replaces_the_program_and_keeps_the_old_one_until_the_next_start(monkeypatch, running):
    github = GitHub(monkeypatch)
    old = running.read_bytes()
    seen = []
    selfupdate.install(selfupdate.latest(), lambda done, total: seen.append((done, total)))
    assert running.read_bytes() == github.body and os.access(running, os.X_OK)
    assert seen[-1] == (len(github.body), len(github.body))
    assert running.with_name('Scuffle-1.0.0-x86_64.old.AppImage').read_bytes() == old
    assert siblings(running) == ['Scuffle-1.0.0-x86_64.AppImage', 'Scuffle-1.0.0-x86_64.old.AppImage']


@pytest.mark.parametrize('options, message', [
    ({'listed_sum': '0' * 64}, 'checksum'),
    ({'size': 3}, 'larger'),
    ({'body': program_text(code=3)}, 'does not start'),
    ({'body': program_text(version='3.0.0')}, 'does not start'),
    ({'body': b'not a program'}, 'does not start'),
])
def test_a_bad_release_changes_nothing(monkeypatch, running, options, message):
    GitHub(monkeypatch, **options)
    before = running.read_bytes()
    with pytest.raises(selfupdate.UpdateError, match=message):
        selfupdate.install(selfupdate.latest())
    assert running.read_bytes() == before
    assert siblings(running) == [running.name]


def test_a_download_cut_short_is_a_clear_failure_that_changes_nothing(monkeypatch, running):
    github = GitHub(monkeypatch, cut_short=True, size=None)
    github.description['assets'][0]['size'] += 1000
    before = running.read_bytes()
    with pytest.raises(selfupdate.UpdateError):
        selfupdate.install(selfupdate.latest())
    assert running.read_bytes() == before and siblings(running) == [running.name]


def test_an_answer_that_is_not_http_is_a_clear_failure_that_changes_nothing(monkeypatch, running):
    GitHub(monkeypatch, garbled=True)
    before = running.read_bytes()
    with pytest.raises(selfupdate.UpdateError, match='could not be installed'):
        selfupdate.install(selfupdate.latest())
    assert running.read_bytes() == before and siblings(running) == [running.name]


def test_a_release_without_a_checksum_for_the_file_is_refused(monkeypatch, running):
    GitHub(monkeypatch, listed_name='Scuffle.exe')
    before = running.read_bytes()
    with pytest.raises(selfupdate.UpdateError, match='does not list a checksum'):
        selfupdate.install(selfupdate.latest())
    assert running.read_bytes() == before and siblings(running) == [running.name]


@pytest.mark.skipif(sys.platform == 'win32' or (hasattr(os, 'geteuid') and os.geteuid() == 0),
                    reason='needs a folder this user cannot write')
def test_a_folder_scuffle_cannot_write_to_is_a_clear_failure(monkeypatch, running):
    GitHub(monkeypatch)
    release = selfupdate.latest()
    running.parent.chmod(0o555)
    try:
        with pytest.raises(selfupdate.UpdateError, match='could not be installed'):
            selfupdate.install(release)
    finally:
        running.parent.chmod(0o755)


def test_the_new_version_clears_away_what_the_update_left(monkeypatch, running):
    old, new = running.with_name(f'{running.stem}.old.AppImage'), running.with_name(f'{running.stem}.new.AppImage')
    old.write_bytes(b'x')
    new.write_bytes(b'y')
    monkeypatch.setattr(selfupdate, 'FINISH_SECONDS', 0)
    selfupdate.finish()
    for thread in threading.enumerate():
        if thread.name == 'scuffle-update-leftovers':
            thread.join(5)
    assert siblings(running) == [running.name]


def test_the_command_updates_and_reports_it(monkeypatch, running, capsys):
    github = GitHub(monkeypatch)
    cli.main(['self-update'])
    assert json.loads(capsys.readouterr().out) == {'updated': True, 'version': NEW}
    assert running.read_bytes() == github.body


def test_the_command_says_when_scuffle_is_up_to_date(monkeypatch, running, capsys):
    GitHub(monkeypatch, tag_name=f'v{CURRENT}')
    cli.main(['self-update'])
    assert json.loads(capsys.readouterr().out) == {'updated': False, 'version': CURRENT}


def test_the_command_points_a_managed_installation_to_the_release(monkeypatch):
    GitHub(monkeypatch)
    monkeypatch.delenv('APPIMAGE', raising=False)
    with pytest.raises(LibraryError, match=f'{NEW} is out.*cannot update itself.*/tag/v{NEW}'):
        cli.main(['self-update'])


def test_the_version_flag_prints_what_a_new_download_is_checked_against(capsys):
    from scuffle import diagnostics
    cli.main(['--version'])
    assert capsys.readouterr().out == f'scuffle {diagnostics.version()}\n'
