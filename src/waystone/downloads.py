"""A queue of install, update and verify jobs with pause, resume and a bandwidth limit.

Jobs run one at a time, because they change the shared installation folder.
A paused job keeps every object it stored and resumes from there.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Callable

from . import catalog
from .library import Cancelled, Library, LibraryError, Updater
from .ngdp.cdn import CDNClient, Throttle
from .settings import Settings, cache_dir

QUEUED, RUNNING, PAUSED, FAILED, DONE = 'queued', 'running', 'paused', 'failed', 'done'


@dataclass
class Job:
    client: str
    kind: str                     # install, update or verify
    status: str = QUEUED
    stage: str = ''
    done_bytes: int = 0
    total_bytes: int = 0
    speed: float = 0.0            # bytes per second over the last few seconds
    error: str = ''
    result: dict = field(default_factory=dict)
    cancel_requested: bool = False

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
        self._worker = threading.Thread(target=self._run, name='waystone-downloads', daemon=True)
        self._worker.start()

    def _client(self, code: str, region: str) -> CDNClient:
        return CDNClient(cache_dir(), region, code, workers=self.settings.workers, throttle=self.throttle)

    # Control

    def set_bandwidth_limit(self, limit: int) -> None:
        self.throttle.limit = max(0, limit)

    def enqueue(self, client: str, kind: str) -> Job:
        catalog.get(client)
        if kind not in {'install', 'update', 'verify', 'repair'}:
            raise ValueError(f'Unknown job kind {kind!r}')
        with self._lock:
            existing = self.job(client)
            if existing and existing.status in {QUEUED, RUNNING}:
                return existing
            if existing and existing.status == PAUSED and existing.kind == kind:
                existing.status = QUEUED
                job = existing
            else:
                if existing:
                    self.jobs.remove(existing)
                job = Job(client, kind)
                self.jobs.append(job)
            self._lock.notify()
        self._notify(job)
        return job

    def pause(self, client: str) -> None:
        with self._lock:
            job = self.job(client)
            if job is None:
                return
            if job.status == QUEUED:
                job.status = PAUSED
            elif job.status == RUNNING:
                self._pause.set()
        if job:
            self._notify(job)

    def pause_all(self) -> None:
        """Pause every job, waiting ones first, so none starts while the running one stops."""
        with self._lock:
            waiting = [job for job in self.jobs if job.status == QUEUED]
            for job in waiting:
                job.status = PAUSED
            if self._current is not None:
                self._pause.set()
        for job in waiting:
            self._notify(job)

    def remove(self, client: str) -> None:
        with self._lock:
            job = self.job(client)
            if job and job.status != RUNNING:
                self.jobs.remove(job)

    def cancel(self, client: str) -> None:
        """Drop a job. A running one stops at the next object first.

        Stored objects stay in the folder. Cleanup removes them later if no
        client needs them.
        """
        with self._lock:
            job = self.job(client)
            if job is None:
                return
            if job.status == RUNNING:
                job.cancel_requested = True
                self._pause.set()
                return
            self.jobs.remove(job)
        self._notify(job)

    def pending(self) -> list[Job]:
        """Queued and paused jobs other than the running one, in queue order."""
        return [j for j in self.jobs if j.status in {QUEUED, PAUSED} and j is not self._current]

    def job(self, client: str) -> Job | None:
        return next((j for j in self.jobs if j.client == client), None)

    def busy(self) -> bool:
        return self._current is not None

    def current(self) -> Job | None:
        return self._current

    # Work

    def _notify(self, job: Job) -> None:
        for listener in list(self.listeners):
            listener(job)

    def _next(self) -> Job:
        with self._lock:
            while True:
                job = next((j for j in self.jobs if j.status == QUEUED), None)
                if job:
                    job.status, job.error = RUNNING, ''
                    self._current = job
                    self._pause.clear()
                    return job
                self._lock.wait()

    def _run(self) -> None:
        while True:
            job = self._next()
            self._notify(job)
            try:
                job.result = self._execute(job)
                job.status = DONE
            except Cancelled:
                job.status = PAUSED
                if job.cancel_requested:
                    with self._lock:
                        self.jobs.remove(job)
            except (LibraryError, OSError, ValueError, RuntimeError) as error:
                job.status, job.error = FAILED, str(error)
            finally:
                self._current = None
            self._notify(job)

    def _execute(self, job: Job) -> dict:
        state = self.library.state(job.client) or {}
        region = state.get('region') or self.settings.region
        locale = state.get('locale') or self.settings.locale
        samples: list[tuple[float, int]] = []

        def progress(event: dict) -> None:
            job.stage = event['stage']
            if 'total_bytes' in event:
                job.done_bytes, job.total_bytes = event['bytes'], event['total_bytes']
                now = time.monotonic()
                samples.append((now, job.done_bytes))
                while samples and now - samples[0][0] > 5:
                    samples.pop(0)
                if len(samples) > 1 and samples[-1][0] > samples[0][0]:
                    job.speed = (samples[-1][1] - samples[0][1]) / (samples[-1][0] - samples[0][0])
            elif event['stage'] == 'planned':
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
