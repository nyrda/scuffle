"""A queue of install, update and verify jobs with pause, resume and a bandwidth limit.

Jobs run one at a time, because they change the shared installation folder.
A paused job keeps every object it stored and resumes from there.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Callable

from . import catalog, imports, move
from .library import Cancelled, Library, LibraryError, Updater, atomic_json, default_preferences
from .ngdp.cdn import CDNClient, Throttle
from .settings import Settings, cache_dir, data_dir
from .rates import RateSmoother

QUEUED, RUNNING, PAUSED, FAILED, DONE = 'queued', 'running', 'paused', 'failed', 'done'
GAME_CHECK = 1.0      # seconds between the process checks of a running automatic update


@dataclass
class Job:
    client: str
    kind: str                     # install, update, verify, repair or uninstall
    status: str = QUEUED
    stage: str = ''
    done_bytes: int = 0
    total_bytes: int = 0
    speed: float = 0.0            # smoothed useful bytes per second
    error: str = ''
    result: dict = field(default_factory=dict)
    cancel_requested: bool = False
    held: bool = False            # paused by Scuffle while a game runs, not by the user
    step: tuple[int, int] | None = None   # done and total of a stage that is not the transfer
    keep_settings: bool = True
    seed_from: str = ''           # an installation an install imports from. It is only read
    copy_settings: bool = False   # copy its WTF and Interface once the install succeeded
    # An update the user consented to in advance. It never writes while a game of the folder
    # runs. Pausing or resuming it by hand makes it an ordinary update.
    automatic: bool = False
    version: str = ''             # the version an automatic update was queued for

    @property
    def fraction(self) -> float:
        return self.done_bytes / self.total_bytes if self.total_bytes else 0.0


@dataclass
class MoveTask:
    """Moving the installation folder. It runs on the worker with no job in the queue."""
    source: str
    destination: str
    settle: bool = False          # finishing an interrupted move from its journal, not a new one
    status: str = QUEUED
    stage: str = ''
    done_bytes: int = 0
    total_bytes: int = 0
    speed: float = 0.0
    error: str = ''
    result: dict = field(default_factory=dict)
    cancel_requested: bool = False
    switched: bool = False        # the setting names the new folder now

    @property
    def fraction(self) -> float:
        return self.done_bytes / self.total_bytes if self.total_bytes else 0.0

    @property
    def active(self) -> bool:
        return self.status in {QUEUED, RUNNING}

    @property
    def cancellable(self) -> bool:
        return self.active and not self.settle and not self.switched and self.stage not in {'switching', 'removing_old'}


class DownloadManager:
    def __init__(self, settings: Settings, library: Library, *, client_factory=None):
        self.settings, self.library = settings, library
        self.throttle = Throttle(settings.bandwidth_limit)
        self.client_factory = client_factory or self._client
        self.jobs: list[Job] = []
        self.listeners: list[Callable[[Job], None]] = []
        self.move_listeners: list[Callable[[MoveTask], None]] = []
        self.moving: MoveTask | None = None
        self._lock = threading.Condition()
        self._pause = threading.Event()
        self._current: Job | None = None
        self._path = data_dir() / 'downloads.json'
        self._game_running = False            # pause_while_playing holds every download
        self._playing = False                 # a game of the folder runs, which holds automatic updates
        self._game_paused: set[str] = set()
        self._overrides: set[str] = set()     # resumed by the user while a game runs
        # Resumes asked for while the job was stopping, and whether each was the user's own.
        self._resume_requested: dict[str, bool] = {}
        self._stopping = False
        library.has_download_job = self._has_download_job
        self._restore()
        # Unstarted uninstall requests are deliberately not restored. A committed
        # journal must finish, even when automatic download resume is disabled.
        if (code := library.uninstalling()):
            self.jobs = [j for j in self.jobs if j.client != code]
            self.jobs.insert(0, Job(code, 'uninstall'))
        # An interrupted move whose setting the window already settled still has a folder to
        # delete. It runs first, and nothing else starts until it is done.
        try:
            record = move.read_journal()
        except LibraryError:
            record = None       # the window reports a damaged journal
        if record:
            self.moving = MoveTask(record['source'], record['destination'], settle=True)
        self._worker = threading.Thread(target=self._run, name='scuffle-downloads', daemon=True)
        self._worker.start()

    def _client(self, code: str, region: str) -> CDNClient:
        return CDNClient(cache_dir(), region, code, workers=self.settings.workers, throttle=self.throttle)

    def _restore(self) -> None:
        try:
            saved = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(saved, dict) or saved.get('root') != str(self.library.root):
            return
        rows = saved.get('jobs')
        if not isinstance(rows, list):
            return
        # A folder on a drive that is not mounted looks empty. Resuming there would start over
        # on the wrong disk, so the jobs then wait for the user.
        present = (self.library.root / '.scuffle').is_dir()
        for row in rows:
            if not isinstance(row, dict) or not catalog.is_product(row.get('client')):
                continue
            if row.get('kind') not in ('install', 'update', 'verify', 'repair'):
                continue
            if row.get('status') not in (QUEUED, RUNNING, PAUSED, FAILED) or self.job(row['client']):
                continue
            status = FAILED if row['status'] == FAILED else PAUSED
            job = Job(row['client'], row['kind'], status=status,
                      error=row.get('error', '') if isinstance(row.get('error', ''), str) else '')
            # An import keeps its source across restarts. The job checks it is still there when it runs.
            seed = row.get('seed_from')
            if job.kind == 'install' and isinstance(seed, str) and Path(seed).is_absolute():
                job.seed_from = seed
                job.copy_settings = row.get('copy_settings') is True
            if job.kind == 'update' and row.get('automatic') is True:
                job.automatic = True
                job.version = row['version'] if isinstance(row.get('version'), str) else ''
            if job.kind != 'verify':
                job.done_bytes, job.total_bytes = self.library.download_progress(job.client)
            # An automatic update carries on by itself, as long as the user still consents to it.
            carries_on = self.settings.resume_downloads or \
                (job.automatic and self.settings.updates_automatically(job.client))
            if status == PAUSED and carries_on and present:
                job.status = QUEUED
            self.jobs.append(job)

    def _save(self) -> None:
        """Called with the queue lock, only when membership, order or status changes."""
        for job in self.jobs:
            job.held = job.client in self._game_paused
        try:
            atomic_json(self._path, {'root': str(self.library.root), 'jobs': [
                {'client': j.client, 'kind': j.kind, 'status': j.status, 'error': j.error}
                | ({'seed_from': j.seed_from, 'copy_settings': j.copy_settings} if j.seed_from else {})
                | ({'automatic': True, 'version': j.version} if j.automatic else {})
                for j in self.jobs if j.status != DONE and not j.cancel_requested]})
        except OSError:
            # A full or read-only data folder only loses the queue for the next start.
            # The downloads themselves carry on.
            pass

    # Control

    def set_bandwidth_limit(self, limit: int) -> None:
        self.throttle.limit = max(0, limit)

    def _held(self, job: Job) -> bool:
        """Whether a running game keeps this job waiting. Called with the queue lock."""
        if job.client in self._overrides:
            return False
        return self._game_running or (job.automatic and self._playing)

    def _resume(self, job: Job, *, override: bool = False) -> None:
        """override is the user's own resume, which a running game does not hold."""
        if self._stopping:
            return
        if override:
            # Resuming a held download by hand runs it now, game or not. An automatic update
            # resumed by hand is the user's own from now on.
            job.automatic = False
            self._game_paused.discard(job.client)
            if self._game_running:
                self._overrides.add(job.client)
        elif self._held(job):
            self._game_paused.add(job.client)       # held until the game closes
            return
        if job is self._current:
            self._resume_requested[job.client] = override
        else:
            job.status, job.speed = QUEUED, 0.0

    def _has_download_job(self, code: str) -> bool:
        job = self.job(code)
        return bool(job and job.status != DONE and not (job is self._current and job.kind == 'uninstall'))

    def enqueue(self, client: str, kind: str, *, keep_settings: bool = True, seed_from: str | None = None,
                copy_settings: bool = False) -> Job:
        """Queue a job. An install with seed_from imports from that installation.

        Without seed_from, a job that replaces a failed one of the same kind keeps its source,
        so trying an import again imports again.
        """
        catalog.get(client)
        if kind not in {'install', 'update', 'verify', 'repair', 'uninstall'}:
            raise ValueError(f'Unknown job kind {kind!r}')
        if seed_from is not None and kind != 'install':
            raise ValueError('Only an install can import')
        if seed_from is not None:
            # By name only, since this runs on the window's thread. The job compares the folders themselves.
            seed_from = os.path.abspath(seed_from)
            if imports.nested(Path(seed_from), self.library.root):
                raise LibraryError('Import from a folder outside the Scuffle installation folder')
        with self._lock:
            if self.moving and self.moving.active:
                raise LibraryError('Wait until the installation folder has moved.')
            existing = self.job(client)
            if seed_from is not None and existing and existing.status not in {DONE, FAILED}:
                raise LibraryError('Remove this client\'s download before importing it')
            if kind == 'uninstall' and existing and existing.status != DONE and existing.kind != 'uninstall':
                raise LibraryError('Remove the download job before uninstalling')
            if existing and existing.kind == 'uninstall' and existing.status != DONE and kind != 'uninstall':
                raise LibraryError('Finish uninstalling this client first')
            if existing and existing.status in {QUEUED, RUNNING}:
                if existing is self._current and self._pause.is_set():
                    self._resume(existing, override=True)
                return existing
            # A new download waits for a running game. Resuming a paused one is the user's call.
            override = bool(existing and existing.status == PAUSED and existing.kind == kind)
            if override:
                job = existing
            else:
                if existing:
                    self.jobs.remove(existing)
                job = Job(client, kind, status=PAUSED, keep_settings=keep_settings)
                if existing and existing.kind == 'uninstall':
                    job.keep_settings = existing.keep_settings
                if seed_from is not None:
                    job.seed_from, job.copy_settings = seed_from, copy_settings
                elif existing and existing.kind == kind:
                    job.seed_from, job.copy_settings = existing.seed_from, existing.copy_settings
                self.jobs.append(job)
            self._resume(job, override=override)
            self._save()
            self._lock.notify_all()
        self._notify(job)
        return job

    def enqueue_automatic(self, client: str, version: str) -> Job | None:
        """Queue an update to version that the user consented to in advance, or return None.

        Nothing is queued without the consent, while the client has a job, or while an
        automatic update to this version failed too recently. Only a failed automatic update
        gives way to a new one, once that wait is over.
        """
        catalog.get(client)
        with self._lock:
            if self._stopping or (self.moving and self.moving.active):
                return None
            if not self.settings.updates_automatically(client) or \
                    self.settings.failed_recently(client, version, time.time()):
                return None
            existing = self.job(client)
            if existing and existing.status != DONE and not (existing.status == FAILED and existing.automatic):
                return None
            if existing:
                self.jobs.remove(existing)
            job = Job(client, 'update', status=PAUSED, automatic=True, version=version)
            self.jobs.append(job)
            self._resume(job)
            self._save()
            self._lock.notify_all()
        self._notify(job)
        return job

    def pause(self, client: str) -> None:
        with self._lock:
            self._game_paused.discard(client)
            self._resume_requested.pop(client, None)
            job = self.job(client)
            if job is None:
                return
            if job is self._current and job.kind == 'uninstall':
                return
            job.automatic = False         # paused by hand, so it waits for the user
            if job.status == QUEUED:
                job.status = PAUSED
            elif job is self._current:
                self._pause.set()
            self._save()
        self._notify(job)

    def pause_all(self) -> None:
        """Pause waiting jobs before stopping the current one."""
        with self._lock:
            changed = [j for j in self.jobs if j.status in {QUEUED, RUNNING}]
            if not self._stopping:
                # Paused by hand, an automatic update waits for the user. Closing Scuffle is no such pause.
                for job in self.jobs:
                    if job in changed or job.client in self._game_paused:
                        job.automatic = False
            self._game_paused.clear()
            self._resume_requested.clear()
            for job in changed:
                if job is self._current:
                    self._pause.set()
                else:
                    job.status = PAUSED
            self._save()
        for job in changed:
            self._notify(job)

    def resume_all(self) -> None:
        with self._lock:
            changed = [j for j in self.jobs if j.status == PAUSED or
                       (j is self._current and self._pause.is_set())]
            for job in changed:
                self._resume(job, override=True)     # the user's own choice
            self._save()
            self._lock.notify_all()
        for job in changed:
            self._notify(job)

    def set_game_running(self, running: bool) -> None:
        """Hold downloads during games, preserving the user's own pauses."""
        with self._lock:
            if running == self._game_running:
                return
            self._game_running = running
            self._overrides.clear()
            changed = self._apply_holds()
        for job in changed:
            self._notify(job)

    def set_playing(self, playing: bool) -> None:
        """Whether a game of the installation folder runs or starts. Automatic updates wait for the last one."""
        with self._lock:
            if playing == self._playing:
                return
            self._playing = playing
            changed = self._apply_holds()
        for job in changed:
            self._notify(job)

    def _apply_holds(self) -> list[Job]:
        """Hold the jobs a game holds now and resume the ones it held before. Called with the queue lock.

        Only a waiting or running job is held, so the user's own pauses stay as they are.
        """
        changed = []
        for job in self.jobs:
            current = job is self._current
            if self._held(job):
                if job.status == QUEUED or (current and not self._pause.is_set()):
                    self._game_paused.add(job.client)
                    if current:
                        self._pause.set()
                    else:
                        job.status = PAUSED
                    changed.append(job)
            elif job.client in self._game_paused:
                self._game_paused.discard(job.client)
                if job.status == PAUSED or (current and self._pause.is_set()):
                    self._resume(job)
                changed.append(job)
        self._save()
        self._lock.notify_all()
        return changed

    def move(self, client: str, offset: int) -> None:
        """Move a waiting job by an offset among waiting jobs only."""
        with self._lock:
            waiting = self.pending()
            job = self.job(client)
            if job not in waiting:
                return
            index = waiting.index(job)
            target = max(0, min(len(waiting) - 1, index + offset))
            waiting.insert(target, waiting.pop(index))
            ordered = iter(waiting)
            self.jobs[:] = [next(ordered) if j.status in {QUEUED, PAUSED} and j is not self._current
                            else j for j in self.jobs]
            self._save()
        self._notify(job)

    def remove(self, client: str) -> None:
        with self._lock:
            job = self.job(client)
            if job and job is not self._current:
                self.cancel(client)

    def cancel(self, client: str) -> None:
        """Drop a job after its current object. Stored content stays for reuse."""
        with self._lock:
            job = self.job(client)
            if job is None:
                return
            if job.kind == 'uninstall' and (job is self._current or self.library.uninstalling() == client):
                return
            self._game_paused.discard(client)
            self._resume_requested.pop(client, None)
            if job is self._current:
                job.cancel_requested = True
                self._pause.set()
            else:
                self.jobs.remove(job)
            self._save()
        self._notify(job)

    def pending(self) -> list[Job]:
        """Queued and paused jobs other than the running one, in queue order."""
        with self._lock:
            return [j for j in self.jobs if j.status in {QUEUED, PAUSED} and j is not self._current]

    def job(self, client: str) -> Job | None:
        with self._lock:
            return next((j for j in self.jobs if j.client == client), None)

    def busy(self) -> bool:
        with self._lock:
            return self._current is not None or bool(self.moving and self.moving.active)

    # Moving the installation folder

    def start_move(self, destination: str) -> MoveTask:
        """Queue a move of the installation folder. Refused while any job exists."""
        with self._lock:
            if self._stopping:
                raise LibraryError('Scuffle is closing.')
            if self.moving and self.moving.active:
                raise LibraryError('The installation folder is already being moved.')
            if self._current is not None or any(j.status != DONE for j in self.jobs):
                raise LibraryError('Finish or remove every download before moving the installation folder.')
            task = self.moving = MoveTask(str(self.library.root), str(destination))
            self._lock.notify_all()
        self._notify_move(task)
        return task

    def cancel_move(self) -> None:
        """Stop a copy before the switch. Afterwards the move always finishes."""
        with self._lock:
            task = self.moving
            if not task or not task.cancellable:
                return
            task.cancel_requested = True
        self._notify_move(task)

    def _notify_move(self, task: MoveTask) -> None:
        for listener in list(self.move_listeners):
            listener(task)

    def current(self) -> Job | None:
        with self._lock:
            return self._current

    def shutdown(self) -> None:
        with self._lock:
            self._stopping = True
            if self.moving and self.moving.status == QUEUED:
                # Never started, so nothing changed. A settling move resumes on the next start.
                self.moving.status, self.moving.result = DONE, {'cancelled': True}
            self.pause_all()
            self._lock.notify_all()

    # Work

    def _notify(self, job: Job) -> None:
        for listener in list(self.listeners):
            listener(job)

    def _next(self) -> Job | None:
        with self._lock:
            while not self._stopping:
                if self.moving and self.moving.status == QUEUED:
                    self.moving.status = RUNNING
                    return self.moving
                job = next((j for j in self.jobs if j.status == QUEUED and not self._held(j)), None)
                if job:
                    job.status, job.error = RUNNING, ''
                    self._current = job
                    self._pause.clear()
                    self._save()
                    return job
                self._lock.wait()
            return None

    def _run(self) -> None:
        while (job := self._next()) is not None:
            if isinstance(job, MoveTask):
                self._run_move(job)
                continue
            self._notify(job)
            status, error = DONE, ''
            try:
                job.result = self._execute(job)
            except Cancelled:
                status = PAUSED
            except (LibraryError, OSError, ValueError, RuntimeError, sqlite3.Error) as failure:
                status, error = FAILED, str(failure)
            with self._lock:
                job.status, job.error, job.speed = status, error, 0.0
                self._current = None
                if job.automatic and status in {DONE, FAILED}:
                    self._remember(job, status)
                if job.cancel_requested:
                    self.jobs.remove(job)
                elif status == PAUSED and job.client in self._resume_requested:
                    # Asked for while it was stopping, as the user's or as a game's exit.
                    self._resume(job, override=self._resume_requested[job.client])
                self._resume_requested.pop(job.client, None)
                self._save()
            if job.automatic and status in {DONE, FAILED}:
                try:
                    self.settings.save()
                except OSError:
                    pass            # it then waits only for this run, as the failure in memory says
            self._notify(job)

    def _remember(self, job: Job, status: str) -> None:
        """Note when an automatic update failed, before anything can queue it again. Called with the queue lock."""
        failures = self.settings.auto_update_failures
        if status == FAILED:
            failures[job.client] = {'version': job.version, 'at': int(time.time())}
        else:
            failures.pop(job.client, None)

    def _run_move(self, task: MoveTask) -> None:
        self._notify_move(task)
        rate = RateSmoother()

        def progress(event: dict) -> None:
            nonlocal rate
            if event['stage'] != task.stage:
                rate, task.speed = RateSmoother(), 0.0
            task.stage = event['stage']
            task.switched = task.switched or task.stage == 'removing_old'
            if 'total_bytes' in event:
                task.done_bytes, task.total_bytes = event['bytes'], event['total_bytes']
                task.speed = rate.update(task.done_bytes)
            self._notify_move(task)

        status, error, result = DONE, '', {}
        try:
            if task.settle:
                result = move.recover(self.settings, progress=progress) or {}
            else:
                result = move.move(self.library, task.destination, self.settings, progress=progress,
                                   cancelled=lambda: task.cancel_requested or self._stopping,
                                   has_jobs=lambda: any(j.status != DONE for j in self.jobs))
        except Cancelled:
            result = {'cancelled': True}
        except (LibraryError, OSError, ValueError, RuntimeError, sqlite3.Error) as failure:
            status, error = FAILED, str(failure)
        with self._lock:
            task.status, task.error, task.speed, task.result = status, error, 0.0, result
            task.switched = task.switched or bool(result.get('moved'))
        self._notify_move(task)

    def _execute(self, job: Job) -> dict:
        if job.kind == 'uninstall':
            job.stage = 'uninstalling'
            self._notify(job)
            recovering = self.library.uninstalling() == job.client
            def uninstall_progress(event):
                job.stage = event['stage']
                self._notify(job)
            with self.library.locked():
                if recovering:
                    return self.library.recovered_uninstall
                return self.library.uninstall(job.client, keep_settings=job.keep_settings,
                                              progress=uninstall_progress)
        state = self.library.state(job.client) or {}
        region = state.get('region') or self.settings.region
        locale = state.get('locale') or self.settings.locale
        rate = RateSmoother()
        job.speed = 0.0

        def progress(event: dict) -> None:
            nonlocal rate
            stage = event['stage']
            if stage != job.stage:
                rate = RateSmoother()
                job.speed, job.step = 0.0, None
            job.stage = stage
            if 'total' in event and 'total_bytes' not in event:
                job.step = event['done'], event['total']
            if 'total_bytes' in event:
                job.done_bytes, job.total_bytes = event['bytes'], event['total_bytes']
                job.speed = rate.update(event.get('received', job.done_bytes)) if stage == 'downloading' else 0.0
            elif stage == 'planned':
                job.total_bytes = event['encoded_bytes']
                job.done_bytes = event['encoded_bytes'] - event['missing_bytes']
            self._notify(job)

        seed = Path(job.seed_from) if job.seed_from and job.kind == 'install' else None
        if seed is not None and not imports.available(seed):
            # Such as a drive that is not connected now. What is already stored stays for the next try.
            raise LibraryError(f'The installation to import from is not available: {seed}. Connect its '
                               'drive and try again, or remove this download and install without it.')
        # Before the installation folder is created, which could otherwise land inside the source.
        if seed is not None and imports.overlaps(seed, self.library.root):
            raise LibraryError('Import from a folder outside the Scuffle installation folder')
        if job.kind == 'install':
            self.library.create()
        elif not state:
            raise LibraryError(f'{catalog.get(job.client).label} is not installed')
        cancelled = self._stops(job)
        with self.library.locked():
            if self._game_holds(job):
                raise Cancelled('A game is running')
            updater = Updater(self.library, job.client, self.client_factory(job.client, region),
                              region=region, locale=locale, seed_from=seed, progress=progress,
                              cancelled=cancelled, may_publish=lambda: not self._game_holds(job))
            try:
                if job.kind == 'verify':
                    result = updater.verify()
                    if not result['ok']:
                        count = len(result['errors'])
                        raise LibraryError(f"{count} damaged or missing {'file' if count == 1 else 'files'} found. "
                                           'Repair downloads them again.')
                    return result
                if job.kind == 'repair':
                    # Damaged objects are forgotten, so the update fetches them again
                    # and rewrites every installed file that differs.
                    for problem in updater.verify()['errors']:
                        if 'ekey' in problem:
                            updater.store.invalidate(bytes.fromhex(problem['ekey']))
                result = updater.run()
            finally:
                updater.close()
            if seed is not None and job.copy_settings:
                job.stage, job.step = 'copying_settings', None
                self._notify(job)
                result['settings'] = self._copy_settings(seed, result)
            # Reclaim content the previous build needed. A running game or
            # another client's unfinished update defers this to a later job.
            try:
                job.stage = 'cleaning_up'
                self._notify(job)
                result['cleanup'] = self.library.cleanup(cancelled=cancelled)
            except LibraryError as error:
                result['cleanup'] = {'deferred': str(error)}
            return result

    def _stops(self, job: Job) -> Callable[[], bool]:
        """What the job checks between objects: a pause, and for an automatic update a game that started.

        The window sees a game within seconds, and earlier when Scuffle starts it. The job looks
        itself as well, so it never depends on the window to stop writing the shared store.
        """
        checked = float('-inf')

        def cancelled() -> bool:
            nonlocal checked
            if time.monotonic() - checked >= GAME_CHECK:
                checked = time.monotonic()
                self._game_holds(job)
            return self._pause.is_set()
        return cancelled

    def _game_holds(self, job: Job) -> bool:
        """Look for a game of the folder now. One holds an automatic update, as if the window had seen it."""
        if job.automatic and self.library.running_games():
            self.set_playing(True)
            return True
        return False

    def _copy_settings(self, seed: Path, state: dict) -> dict:
        """Copy WTF and Interface after a successful import. A failure leaves the install as it is."""
        source = imports.client_folder(seed, state['product'])
        if source is None:
            return {'error': f'{seed} no longer has this client\'s folder'}
        try:
            return imports.copy_settings(source, self.library.root / state['folder'], replaceable={
                'WTF/Config.wtf': default_preferences(state['region'], state['locale'])})
        except (OSError, imports.NotImportable) as error:
            return {'error': str(error)}
