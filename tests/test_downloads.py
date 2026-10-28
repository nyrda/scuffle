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
