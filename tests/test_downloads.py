"""Queue controls and restart recovery use private libraries and deterministic workers."""
import hashlib
import json
import threading

import pytest

from waystone.downloads import DONE, FAILED, PAUSED, QUEUED, RUNNING, DownloadManager
from waystone.library import Cancelled, Library, Plan, atomic_json
from waystone.ngdp.models import DownloadEntry
from waystone.ngdp.store import CascStore
from waystone.settings import Settings, data_dir


@pytest.fixture
def managers(tmp_path, monkeypatch):
    # Control scheduling explicitly so assertions do not race a daemon.
    monkeypatch.setattr(DownloadManager, '_run', lambda self: None)
    made = []

    def create(**kwargs):
        settings = Settings(root=str(tmp_path / 'games'), **kwargs)
        manager = DownloadManager(settings, Library(settings.root))
        made.append(manager)
        return manager

    yield create
    for manager in made:
        manager.shutdown()


def saved():
    return json.loads((data_dir() / 'downloads.json').read_text())


def test_queue_restores_order_kinds_and_errors_without_resuming(managers):
    manager = managers()
    manager.enqueue('wow', 'install')
    manager.enqueue('wowf', 'update')
    failed = manager.enqueue('wowt', 'verify')
    failed.status, failed.error = FAILED, 'Damaged files'
    manager.enqueue('wow_classic', 'repair')
    current = manager._next()
    assert current.status == RUNNING
    manager.move('wow_classic', -1)
    restored = managers()
    assert [(j.client, j.kind, j.status) for j in restored.jobs] == [
        ('wow', 'install', PAUSED), ('wow_classic', 'repair', PAUSED),
        ('wowt', 'verify', FAILED), ('wowf', 'update', PAUSED)]
    assert restored.job('wowt').error == 'Damaged files'
    assert all(j.total_bytes == 0 for j in restored.jobs)


def test_completed_and_cancelled_jobs_are_not_restored(managers):
    manager = managers()
    done = manager.enqueue('wow', 'install')
    done.status = DONE
    manager.enqueue('wowf', 'install')
    manager.enqueue('wowt', 'install')
    manager.cancel('wowf')
    manager._next()
    manager.cancel('wowt')
    assert saved()['jobs'] == []
    assert managers().jobs == []


def test_auto_resume_is_opt_in_and_leaves_failures_visible(managers):
    manager = managers()
    manager.enqueue('wow', 'install')
    failed = manager.enqueue('wowf', 'verify')
    failed.status, failed.error = FAILED, 'Damage'
    manager.pause_all()
    manager.library.create()              # an interrupted install has its folder
    restored = managers(resume_downloads=True)
    assert restored.job('wow').status == QUEUED
    assert restored.job('wowf').status == FAILED


def test_malformed_queue_entries_and_other_roots_are_ignored(managers):
    manager = managers()
    manager.enqueue('wow', 'install')
    payload = saved()
    payload['jobs'] += [None, {}, {'client': '../wow', 'kind': 'install', 'status': QUEUED},
                        {'client': 'wowf', 'kind': [], 'status': QUEUED},
                        {'client': 'wowt', 'kind': 'install', 'status': []}, payload['jobs'][0]]
    atomic_json(manager._path, payload)
    assert [j.client for j in managers().jobs] == ['wow']
    payload['root'] += '-different'
    atomic_json(manager._path, payload)
    assert managers().jobs == []
    manager._path.write_text('{broken')
    assert managers().jobs == []


def test_restore_reads_stored_objects_instead_of_saved_counters(managers):
    manager = managers()
    library = manager.library
    library.create()
    blobs = [b'BLTE\0\0\0\0N' + b'x' * size for size in (10, 30)]
    entries = [DownloadEntry(hashlib.md5(b).digest(), len(b), 0) for b in blobs]
    with CascStore(library.root) as store:
        plan = Plan(library.client_dir('wow') / 'plan.sqlite', store.inventory_path)
        plan.add(entries)
        plan.close()
        store.put(entries[0].ekey, blobs[0])
        store.checkpoint()
    atomic_json(library.client_dir('wow') / 'pending.json', {'phase': 'downloading'})
    job = manager.enqueue('wow', 'install')
    job.done_bytes, job.total_bytes = 900, 1000
    manager.pause_all()
    restored = managers().job('wow')
    assert (restored.done_bytes, restored.total_bytes) == (len(blobs[0]), sum(map(len, blobs)))
    # A plan left from a different build must not supply a percentage during planning.
    atomic_json(library.client_dir('wow') / 'pending.json', {'phase': 'preparing'})
    assert managers().job('wow').total_bytes == 0
    atomic_json(library.client_dir('wow') / 'pending.json', {'phase': 'downloading'})
    (library.client_dir('wow') / 'plan.sqlite').write_bytes(b'broken')
    assert managers().job('wow').total_bytes == 0


def test_move_preserves_current_job_and_persists_waiting_order(managers):
    manager = managers()
    for code in ['wow', 'wowf', 'wowt', 'wow_classic']:
        manager.enqueue(code, 'install')
    current = manager._next()
    manager.pause('wowt')
    manager.move('wow_classic', -100)
    manager.move('wow', 100)
    assert manager.current() is current
    assert [j.client for j in manager.pending()] == ['wow_classic', 'wowf', 'wowt']
    manager.move('wow_classic', 1)
    assert [j['client'] for j in saved()['jobs']] == ['wow', 'wowf', 'wow_classic', 'wowt']


def test_resume_all_preserves_order_and_does_not_retry_failures(managers):
    manager = managers()
    for code in ['wow', 'wowf', 'wowt']:
        manager.enqueue(code, 'install')
    manager.job('wowf').status = FAILED
    manager.pause_all()
    manager.resume_all()
    assert [(j.client, j.status) for j in manager.jobs] == [('wow', QUEUED), ('wowf', FAILED), ('wowt', QUEUED)]
    assert [j['status'] for j in saved()['jobs']] == [QUEUED, FAILED, QUEUED]


def test_game_pause_owns_only_its_jobs_and_holds_new_jobs(managers):
    manager = managers()
    for code in ['wow', 'wowf', 'wowt']:
        manager.enqueue(code, 'install')
    manager.pause('wowf')
    manager.set_game_running(True)
    manager.set_game_running(True)
    manager.pause('wowt')
    manager.enqueue('wow_classic', 'install')
    assert all(j.status == PAUSED for j in manager.jobs)
    manager.set_game_running(False)
    assert [j.status for j in manager.jobs] == [QUEUED, PAUSED, PAUSED, QUEUED]
    manager.set_game_running(True)
    manager.pause_all()
    manager.set_game_running(False)
    assert all(j.status == PAUSED for j in manager.jobs)


@pytest.mark.parametrize('game', [False, True])
def test_resume_during_pause_waits_for_worker_to_stop(tmp_path, monkeypatch, game):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def execute(self, job):
        calls.append(job.client)
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
            assert self._pause.is_set()
            raise Cancelled('Paused')
        return {}

    monkeypatch.setattr(DownloadManager, '_execute', execute)
    manager = DownloadManager(Settings(), Library(tmp_path / 'games'))
    manager.listeners.append(lambda job: finished.set() if job.status == DONE else None)
    try:
        manager.enqueue('wow', 'install')
        assert started.wait(5)
        if game:
            manager.set_game_running(True)
            manager.set_game_running(False)
        else:
            manager.pause_all()
            manager.resume_all()
        release.set()
        assert finished.wait(5)
        assert calls == ['wow', 'wow']
        assert saved()['jobs'] == []
    finally:
        release.set()
        manager.shutdown()
        manager._worker.join(5)


def test_shutdown_keeps_waiting_jobs_paused_and_stops_scheduling(managers):
    manager = managers()
    manager.enqueue('wow', 'install')
    manager.shutdown()
    manager.resume_all()
    assert manager._next() is None
    assert saved()['jobs'][0]['status'] == PAUSED


def test_a_download_held_for_a_game_says_so_and_resuming_it_by_hand_runs_it(managers):
    manager = managers()
    manager.enqueue('wow', 'install')
    manager.set_game_running(True)
    job = manager.job('wow')
    assert job.status == PAUSED and job.held
    manager.enqueue('wowf', 'install')            # a new download waits for the game too
    assert manager.job('wowf').held
    manager.enqueue('wow', 'install')             # the Resume button on a held download
    assert job.status == QUEUED and not job.held
    assert manager._next() is job                 # it runs although the game still does


def test_auto_resume_waits_when_the_folder_is_gone_such_as_an_unmounted_drive(managers, tmp_path):
    import shutil
    manager = managers()
    manager.enqueue('wow', 'install')
    manager.pause_all()
    manager.library.create()
    shutil.rmtree(tmp_path / 'games')
    restored = managers(resume_downloads=True)
    assert restored.job('wow').status == PAUSED
    assert not (tmp_path / 'games').exists()


def test_a_failed_queue_save_never_stops_the_downloads(managers, monkeypatch):
    from waystone import downloads
    manager = managers()
    def full(path, value):
        raise OSError(28, 'No space left on device')
    monkeypatch.setattr(downloads, 'atomic_json', full)
    job = manager.enqueue('wow', 'install')
    assert manager._next() is job and job.status == RUNNING


@pytest.mark.parametrize('status', [QUEUED, RUNNING, PAUSED, FAILED])
def test_uninstall_cannot_replace_a_download_job(managers, status):
    from waystone.library import LibraryError
    manager = managers()
    existing = manager.enqueue('wow', 'update')
    existing.status = status
    with pytest.raises(LibraryError, match='download job'):
        manager.enqueue('wow', 'uninstall')
    assert manager.job('wow') is existing
    assert manager.library.has_download_job('wow')


def test_uninstall_queue_ignores_unstarted_requests_on_restore(managers):
    manager = managers()
    job = manager.enqueue('wow', 'uninstall', keep_settings=False)
    assert not job.keep_settings and saved()['jobs'][0]['kind'] == 'uninstall'
    assert managers().job('wow') is None


def test_uninstall_worker_uses_library_without_cdn_and_cannot_cancel(managers, monkeypatch):
    manager = managers()
    manager.library.create()
    calls = []
    def uninstall(code, *, keep_settings, progress):
        calls.append((threading.get_ident(), code, keep_settings))
        assert not manager.library.has_download_job(code)
        manager.pause(code)
        manager.cancel(code)
        assert not manager._pause.is_set() and not manager.current().cancel_requested
        progress({'stage': 'cleaning_up'})
        return {'uninstalled': code, 'backup': ''}
    monkeypatch.setattr(manager.library, 'uninstall', uninstall)
    monkeypatch.setattr(manager, 'client_factory', lambda *args: pytest.fail('Uninstall must not use the CDN'))
    job = manager.enqueue('wow', 'uninstall', keep_settings=False)
    finished = threading.Event()
    manager.listeners.append(lambda job: finished.set() if job.status in {DONE, FAILED} else None)
    # This fixture suppresses the constructor's worker, so explicitly start the real loop.
    # Bind the production implementation saved before the fixture patched scheduling.
    worker = threading.Thread(target=REAL_DOWNLOAD_RUN, args=(manager,), daemon=True)
    worker.start()
    assert finished.wait(5)
    manager.shutdown()
    worker.join(5)
    assert job.status == DONE and job.result['uninstalled'] == 'wow'
    assert calls == [(worker.ident, 'wow', False)] and worker.ident != threading.get_ident()
    assert job.stage == 'cleaning_up'


# Keep a handle to the real worker; the managers fixture only replaces scheduling.
REAL_DOWNLOAD_RUN = DownloadManager._run


def test_uninstall_worker_reports_database_errors(managers, monkeypatch):
    import sqlite3
    manager = managers()
    manager.library.create()
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('Cannot read the content inventory')
    monkeypatch.setattr(manager.library, 'uninstall', fail)
    job = manager.enqueue('wow', 'uninstall')
    finished = threading.Event()
    manager.listeners.append(lambda job: finished.set() if job.status in {DONE, FAILED} else None)
    worker = threading.Thread(target=REAL_DOWNLOAD_RUN, args=(manager,), daemon=True)
    worker.start()
    try:
        assert finished.wait(5)
        assert job.status == FAILED and job.error == 'Cannot read the content inventory'
        assert manager.current() is None
    finally:
        manager.shutdown()
        worker.join(5)


def test_a_long_stage_before_the_transfer_reports_its_count_until_bytes_move(managers, monkeypatch):
    from waystone import downloads
    manager = managers()
    seen = []

    class Updater:
        def __init__(self, *args, progress, **kwargs):
            self.progress = progress

        def run(self):
            self.progress({'stage': 'planned', 'objects': 9, 'encoded_bytes': 100, 'missing_objects': 1,
                           'missing_bytes': 10})
            self.progress({'stage': 'cdn_indexes', 'missing_objects': 1})
            self.progress({'stage': 'cdn_indexes', 'done': 1200, 'total': 1411})
            seen.append(manager.current().step)
            self.progress({'stage': 'downloading', 'done': 8, 'total': 9, 'bytes': 90, 'total_bytes': 100})
            seen.append(manager.current().step)
            return {}

        def close(self):
            pass
    monkeypatch.setattr(downloads, 'Updater', Updater)
    monkeypatch.setattr(manager, 'client_factory', lambda *args: None)
    monkeypatch.setattr(manager.library, 'cleanup', lambda **kwargs: {})
    job = manager.enqueue('wow', 'install')
    finished = threading.Event()
    manager.listeners.append(lambda job: finished.set() if job.status in {DONE, FAILED} else None)
    worker = threading.Thread(target=REAL_DOWNLOAD_RUN, args=(manager,), daemon=True)
    worker.start()
    assert finished.wait(5)
    manager.shutdown()
    worker.join(5)
    assert job.status == DONE, job.error
    assert seen == [(1200, 1411), None]
