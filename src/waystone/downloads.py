"""A queue of install, update and verify jobs with pause, resume and a bandwidth limit.

Jobs run one at a time, because they change the shared installation folder.
A paused job keeps every object it stored and resumes from there.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import threading
from typing import Callable

from . import catalog
from .library import Cancelled, Library, LibraryError, Updater, atomic_json
from .ngdp.cdn import CDNClient, Throttle
from .settings import Settings, cache_dir, data_dir
from .rates import RateSmoother

QUEUED, RUNNING, PAUSED, FAILED, DONE = 'queued', 'running', 'paused', 'failed', 'done'


@dataclass
class Job:
    client: str
    kind: str                     # install, update or verify
    status: str = QUEUED
    stage: str = ''
    done_bytes: int = 0
    total_bytes: int = 0
    speed: float = 0.0            # smoothed useful bytes per second
    error: str = ''
    result: dict = field(default_factory=dict)
    cancel_requested: bool = False
    held: bool = False            # paused by Waystone while a game runs, not by the user

    @property
    def fraction(self) -> float:
        return self.done_bytes / self.total_bytes if self.total_bytes else 0.0


class DownloadManager:
    def __init__(self, settings: Settings, library: Library, *, client_factory=None):
        self.settings, self.library = settings, library
        self.throttle = Throttle(settings.bandwidth_limit)
        self.client_factory = client_factory or self._client
        self.jobs: list[Job] = []
        self.listeners: list[Callable[[Job], None]] = []
        self._lock = threading.Condition()
        self._pause = threading.Event()
        self._current: Job | None = None
        self._path = data_dir() / 'downloads.json'
        self._game_running = False
        self._game_paused: set[str] = set()
        self._overrides: set[str] = set()     # resumed by the user while a game runs
        self._resume_requested: set[str] = set()
        self._stopping = False
        self._restore()
        self._worker = threading.Thread(target=self._run, name='waystone-downloads', daemon=True)
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
        present = (self.library.root / '.waystone').is_dir()
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
            if job.kind != 'verify':
                job.done_bytes, job.total_bytes = self.library.download_progress(job.client)
            if status == PAUSED and self.settings.resume_downloads and present:
                job.status = QUEUED
            self.jobs.append(job)

    def _save(self) -> None:
        """Called with the queue lock, only when membership, order or status changes."""
        for job in self.jobs:
            job.held = job.client in self._game_paused
        try:
            atomic_json(self._path, {'root': str(self.library.root), 'jobs': [
                {'client': j.client, 'kind': j.kind, 'status': j.status, 'error': j.error}
                for j in self.jobs if j.status != DONE and not j.cancel_requested]})
        except OSError:
            # A full or read-only data folder only loses the queue for the next start.
            # The downloads themselves carry on.
            pass

    # Control

    def set_bandwidth_limit(self, limit: int) -> None:
        self.throttle.limit = max(0, limit)

    def _resume(self, job: Job, *, override: bool = False) -> None:
        if self._stopping:
            return
        if self._game_running and not override:
            self._game_paused.add(job.client)       # held until the game closes
            return
        if self._game_running:
            # Resuming a held download by hand runs it now, game or not.
            self._game_paused.discard(job.client)
            self._overrides.add(job.client)
        if job is self._current:
            self._resume_requested.add(job.client)
        else:
            job.status, job.speed = QUEUED, 0.0

    def enqueue(self, client: str, kind: str) -> Job:
        catalog.get(client)
        if kind not in {'install', 'update', 'verify', 'repair'}:
            raise ValueError(f'Unknown job kind {kind!r}')
        with self._lock:
            existing = self.job(client)
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
                job = Job(client, kind, status=PAUSED)
                self.jobs.append(job)
            self._resume(job, override=override)
            self._save()
            self._lock.notify_all()
        self._notify(job)
        return job

    def pause(self, client: str) -> None:
        with self._lock:
            self._game_paused.discard(client)
            self._resume_requested.discard(client)
            job = self.job(client)
            if job is None:
                return
            if job.status == QUEUED:
                job.status = PAUSED
            elif job is self._current:
                self._pause.set()
            self._save()
        self._notify(job)

    def pause_all(self) -> None:
        """Pause waiting jobs before stopping the current one."""
        with self._lock:
            self._game_paused.clear()
            self._resume_requested.clear()
            changed = [j for j in self.jobs if j.status in {QUEUED, RUNNING}]
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
            if running:
                changed = [j for j in self.jobs if j.status == QUEUED or
                           (j is self._current and not self._pause.is_set())]
                self._game_paused.update(j.client for j in changed)
                for job in changed:
                    if job is self._current:
                        self._pause.set()
                    else:
                        job.status = PAUSED
            else:
                changed = [j for j in self.jobs if j.client in self._game_paused]
                self._game_paused.clear()
                for job in changed:
                    if job.status == PAUSED or (job is self._current and self._pause.is_set()):
                        self._resume(job)
            self._save()
            self._lock.notify_all()
        for job in changed:
            self._notify(job)

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
            self._game_paused.discard(client)
            self._resume_requested.discard(client)
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
            return self._current is not None

    def current(self) -> Job | None:
        with self._lock:
            return self._current

    def shutdown(self) -> None:
        with self._lock:
            self._stopping = True
            self.pause_all()
            self._lock.notify_all()

    # Work

    def _notify(self, job: Job) -> None:
        for listener in list(self.listeners):
            listener(job)

    def _next(self) -> Job | None:
        with self._lock:
            while not self._stopping:
                job = next((j for j in self.jobs if j.status == QUEUED), None)
                if job and (not self._game_running or job.client in self._overrides):
                    job.status, job.error = RUNNING, ''
                    self._current = job
                    self._pause.clear()
                    self._save()
                    return job
                self._lock.wait()
            return None

    def _run(self) -> None:
        while (job := self._next()) is not None:
            self._notify(job)
            status, error = DONE, ''
            try:
                job.result = self._execute(job)
            except Cancelled:
                status = PAUSED
            except (LibraryError, OSError, ValueError, RuntimeError) as failure:
                status, error = FAILED, str(failure)
            with self._lock:
                job.status, job.error, job.speed = status, error, 0.0
                self._current = None
                if job.cancel_requested:
                    self.jobs.remove(job)
                elif status == PAUSED and job.client in self._resume_requested:
                    self._resume(job, override=True)      # asked for while it was stopping
                self._resume_requested.discard(job.client)
                self._save()
            self._notify(job)

    def _execute(self, job: Job) -> dict:
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
                job.speed = 0.0
            job.stage = stage
            if 'total_bytes' in event:
                job.done_bytes, job.total_bytes = event['bytes'], event['total_bytes']
                job.speed = rate.update(event.get('received', job.done_bytes)) if stage == 'downloading' else 0.0
            elif stage == 'planned':
                job.total_bytes = event['encoded_bytes']
                job.done_bytes = event['encoded_bytes'] - event['missing_bytes']
            self._notify(job)

        if job.kind == 'install':
            self.library.create()
        elif not state:
            raise LibraryError(f'{catalog.get(job.client).label} is not installed')
        with self.library.locked():
            updater = Updater(self.library, job.client, self.client_factory(job.client, region),
                              region=region, locale=locale, progress=progress,
                              cancelled=self._pause.is_set)
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
            # Reclaim content the previous build needed. A running game or
            # another client's unfinished update defers this to a later job.
            try:
                job.stage = 'cleaning_up'
                self._notify(job)
                result['cleanup'] = self.library.cleanup(cancelled=self._pause.is_set)
            except LibraryError as error:
                result['cleanup'] = {'deferred': str(error)}
            return result
