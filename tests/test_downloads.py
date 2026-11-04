"""Queue controls and restart recovery use private libraries and deterministic workers."""
import hashlib
import json
import os
import threading

import pytest

from scuffle.downloads import DONE, FAILED, PAUSED, QUEUED, RUNNING, DownloadManager
from scuffle.library import Cancelled, Library, Plan, atomic_json
from scuffle.ngdp.models import DownloadEntry
from scuffle.ngdp.store import CascStore
from scuffle.settings import Settings, data_dir


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


def test_an_automatic_update_needs_consent_and_never_doubles_a_clients_job(managers):
    manager = managers()
    assert manager.enqueue_automatic('wow', '12.0.7.1') is None         # the switch is off
    assert manager.jobs == []
    manager.settings.auto_update = True
    manager.settings.auto_update_except = ['wowf']
    job = manager.enqueue_automatic('wow', '12.0.7.1')
    assert (job.kind, job.status, job.automatic) == ('update', QUEUED, True)
    assert manager.enqueue_automatic('wow', '12.0.7.1') is None         # already queued
    assert manager.enqueue_automatic('wowf', '12.0.7.1') is None        # excepted
    assert manager.enqueue_automatic('wowt', '12.0.7.1') is None        # a test client needs its own choice
    # Any job of the client stays, the user's own pause and failure too.
    manager.enqueue('wow_classic', 'update')
    manager.pause('wow_classic')
    manager.enqueue('wow_anniversary', 'update').status = FAILED
    assert manager.enqueue_automatic('wow_classic', '5.5.3.1') is None
    assert manager.enqueue_automatic('wow_anniversary', '2.5.5.1') is None
    assert [(j.client, j.status, j.automatic) for j in manager.jobs] == [
        ('wow', QUEUED, True), ('wow_classic', PAUSED, False), ('wow_anniversary', FAILED, False)]
    assert saved()['jobs'][0] == {'client': 'wow', 'kind': 'update', 'status': QUEUED, 'error': '',
                                  'automatic': True, 'version': '12.0.7.1'}


def test_automatic_updates_wait_for_any_game_of_the_folder_and_start_after_the_last(managers):
    manager = managers(auto_update=True)
    manager.enqueue_automatic('wow', '12.0.7.1')
    manager.set_playing(True)
    held = manager.job('wow')
    assert held.status == PAUSED and held.held
    manager.enqueue('wowf', 'install')                 # the user's own download is not held
    assert manager.enqueue_automatic('wow_classic', '5.5.3.1').status == PAUSED
    assert manager._next() is manager.job('wowf')
    manager.set_playing(False)
    assert [(j.client, j.status) for j in manager.jobs] == [
        ('wow', QUEUED), ('wowf', RUNNING), ('wow_classic', QUEUED)]


def test_an_automatic_update_paused_or_resumed_by_hand_is_the_users_own(managers):
    manager = managers(auto_update=True)
    manager.enqueue_automatic('wow', '12.0.7.1')
    manager.enqueue_automatic('wowf', '12.0.7.1')
    manager.set_playing(True)
    manager.enqueue('wow', 'update')                   # Resume on the held update runs it now
    assert manager.job('wow').status == QUEUED and not manager.job('wow').automatic
    manager.pause('wowf')
    manager.set_playing(False)                         # the last game's exit resumes no user's pause
    assert manager.job('wowf').status == PAUSED and not manager.job('wowf').automatic


@pytest.mark.parametrize('consent', [True, False])
def test_an_interrupted_automatic_update_carries_on_after_a_restart_while_consent_lasts(managers, consent):
    manager = managers(auto_update=True)
    manager.library.create()
    manager.enqueue_automatic('wow', '12.0.7.1')
    manager.shutdown()                                 # closing Scuffle pauses it, which is no user's pause
    restored = managers(auto_update=consent)
    job = restored.job('wow')
    assert (job.status, job.automatic, job.version) == (QUEUED if consent else PAUSED, True, '12.0.7.1')


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
    from scuffle import downloads
    manager = managers()
    def full(path, value):
        raise OSError(28, 'No space left on device')
    monkeypatch.setattr(downloads, 'atomic_json', full)
    job = manager.enqueue('wow', 'install')
    assert manager._next() is job and job.status == RUNNING


@pytest.mark.parametrize('status', [QUEUED, RUNNING, PAUSED, FAILED])
def test_uninstall_cannot_replace_a_download_job(managers, status):
    from scuffle.library import LibraryError
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
    from scuffle import downloads
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


def battle_net_copy(store_root, folder, code='wowf', client='_forever_'):
    """A Battle.net-style installation whose content is the given store's, with a client folder of settings."""
    import shutil
    shutil.copytree(store_root / 'Data', folder / 'Data')
    (folder / '.build.info').write_text(f'Branch!STRING:0|Product!STRING:0\neu|{code}\n')
    game = folder / client
    (game / 'WTF/Account/ME').mkdir(parents=True)
    (game / '.flavor.info').write_text(f'Product Flavor!STRING:0\n{code}\n')
    (game / 'Wow.exe').write_bytes(b'inert')
    (game / 'WTF/Config.wtf').write_bytes(b'SET gxWindow "1"\n')
    (game / 'WTF/Account/ME/bindings-cache.wtf').write_bytes(b'bind')
    (game / 'Interface/AddOns/Bag').mkdir(parents=True)
    (game / 'Interface/AddOns/Bag/Bag.toc').write_bytes(b'## Title: Bag')
    return folder


def tree(folder):
    return {str(p.relative_to(folder)): (p.read_bytes() if p.is_file() else None, p.stat().st_mtime_ns)
            for p in sorted(folder.rglob('*'))}


def test_an_import_seeds_the_install_and_copies_settings_without_changing_the_source(managers, tmp_path):
    from test_library import FixtureCDN, SyntheticBuild
    build = SyntheticBuild()
    with CascStore(tmp_path / 'store') as store:
        for key, payload in build.objects.items():
            store.put(key, payload)
        store.publish()
    source = battle_net_copy(tmp_path / 'store', tmp_path / 'bnet/World of Warcraft')
    before = tree(source)
    cdn = FixtureCDN(build, tmp_path / 'cache')
    manager = managers()
    manager.client_factory = lambda code, region: cdn
    manager.enqueue('wowf', 'install', seed_from=str(source), copy_settings=True)
    job = manager._next()
    result = manager._execute(job)
    assert result['complete'] and result['downloaded_objects'] == 0 and not cdn.fetched_keys
    game = manager.library.root / '_forever_'
    # The user's Config.wtf replaces the default one the install wrote.
    assert (game / 'WTF/Config.wtf').read_bytes() == b'SET gxWindow "1"\n'
    assert (game / 'WTF/Account/ME/bindings-cache.wtf').read_bytes() == b'bind'
    # The install made INTERFACE for its own file. Addons join it instead of a second spelling beside it.
    assert (game / 'INTERFACE/AddOns/Bag/Bag.toc').read_bytes() == b'## Title: Bag'
    assert [n for n in os.listdir(game) if n.lower() == 'interface'] == ['INTERFACE']
    assert result['settings']['copied'] == 3
    assert tree(source) == before


def test_an_import_keeps_its_source_through_a_restart_and_a_retry(managers, tmp_path):
    source = str(tmp_path / 'bnet/World of Warcraft')
    manager = managers()
    manager.enqueue('wow', 'install', seed_from=source, copy_settings=True)
    manager.enqueue('wowf', 'install')
    manager.enqueue('wowt', 'install', seed_from='relative/World of Warcraft')
    manager.pause_all()
    restored = managers()
    job = restored.job('wow')
    assert (job.seed_from, job.copy_settings) == (source, True)
    assert restored.job('wowf').seed_from == ''
    # A relative source is kept as the full path it meant when it was queued.
    assert restored.job('wowt').seed_from == os.path.abspath('relative/World of Warcraft')
    job.status, job.error = FAILED, 'Lost the connection'
    retried = restored.enqueue('wow', 'install')       # Try again
    assert (retried.seed_from, retried.copy_settings) == (source, True)


def test_an_import_whose_source_is_gone_fails_clearly_before_reading_anything(managers, tmp_path):
    manager = managers()
    manager.client_factory = lambda *args: pytest.fail('A missing source must not start the install')
    manager.enqueue('wow', 'install', seed_from=str(tmp_path / 'unplugged/World of Warcraft'))
    job = manager._next()
    with pytest.raises(Exception, match='not available.*unplugged'):
        manager._execute(job)
    assert not manager.library.client_dir('wow').exists()


@pytest.mark.parametrize('case', ['inside_root', 'download_exists'])
def test_an_import_is_refused_inside_the_installation_folder_or_over_a_download(managers, tmp_path, case):
    from scuffle.library import LibraryError
    manager = managers()
    source = str(tmp_path / 'other')
    if case == 'inside_root':
        source = str(manager.library.root / 'World of Warcraft')
    else:
        manager.enqueue('wow', 'install')
    with pytest.raises(LibraryError):
        manager.enqueue('wow', 'install', seed_from=source)
    job = manager.job('wow')
    assert job is None or job.seed_from == ''
