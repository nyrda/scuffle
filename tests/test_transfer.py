"""Transfer timing uses fake clocks and transports, never CDN requests or sleeps."""
from concurrent.futures import Future
from contextlib import contextmanager
import hashlib
import io

import pytest

from waystone.ngdp import cdn
from waystone.ngdp.cdn import CDNClient, Throttle, TransferCancelled
from waystone.ngdp.models import DownloadEntry, Location
from waystone.rates import RateSmoother


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def test_rate_warmup_counts_from_transfer_start_and_resets_on_resume():
    clock = Clock()
    rate = RateSmoother(clock=clock)
    assert rate.update(5000) == 0
    clock.now = 1
    assert rate.update(6000) == 0
    clock.now = 2
    assert rate.update(7000) == 1000
    clock.now = 3
    assert rate.update(4000) == 0
    clock.now = 5
    assert rate.update(6000) == 1000


def test_rate_weights_elapsed_time_and_interpolates_window_boundary():
    clock = Clock()
    rate = RateSmoother(clock=clock)
    for now, amount in [(0, 0), (3, 3000), (7, 7000), (10, 10000)]:
        clock.now = now
        speed = rate.update(amount)
    assert speed == 1000
    clock.now = 14
    assert rate.update(10000) == 500
    clock.now = 18
    assert rate.update(10000) == 0


def test_rate_is_independent_of_callback_frequency():
    clock = Clock()
    sparse = RateSmoother(clock=clock)
    dense = RateSmoother(clock=clock)
    for tick in range(101):
        clock.now = tick / 10
        speed = dense.update(tick * 100)
        if tick % 10 == 0:
            assert sparse.update(tick * 100) == pytest.approx(speed)
    assert len(dense.samples) <= 82


def test_throttle_pays_before_first_read_and_bounds_idle_burst():
    clock = Clock()
    bucket = Throttle(1000, clock=clock, sleep=clock.sleep)
    bucket.consume(1000)
    assert clock.now == pytest.approx(1)
    clock.now += 60
    bucket.consume(1000)
    assert clock.now == pytest.approx(61.9)
    assert max(clock.waits) <= .1


def test_throttle_shares_budget_refunds_short_reads_and_changes_limit():
    clock = Clock()
    bucket = Throttle(1000, clock=clock, sleep=clock.sleep)
    bucket.consume(100)
    bucket.refund(50)
    bucket.consume(150)
    assert clock.now == pytest.approx(.2)
    bucket.limit = 2000
    bucket.consume(200)
    assert clock.now == pytest.approx(.3)
    bucket.limit = 0
    bucket.consume(1000000)
    clock.now = 60
    bucket.limit = 100
    bucket.consume(100)
    assert clock.now == pytest.approx(61)


def test_throttle_cancel_interrupts_a_low_bandwidth_wait():
    clock = Clock()
    bucket = Throttle(1, clock=clock, sleep=clock.sleep)
    def check():
        if clock.now >= .2:
            raise TransferCancelled()
    with pytest.raises(TransferCancelled):
        bucket.consume(65536, check)
    assert clock.now <= .3


class Response:
    status = 200

    def __init__(self, blocks, size, clock=None, delay=0):
        self.blocks = iter(blocks)
        self.size, self.clock, self.delay = size, clock, delay
        self.reads = 0

    def getheader(self, name, default=None):
        return str(self.size) if name == 'Content-Length' else default

    def read1(self, size):
        self.reads += 1
        if self.clock:
            self.clock.now += self.delay
        block = next(self.blocks, b'')
        if isinstance(block, Exception):
            raise block
        assert len(block) <= size
        return block


class Connection:
    def __init__(self, response):
        self.response = response

    def request(self, *args, **kwargs):
        pass

    def getresponse(self):
        return self.response


@pytest.fixture
def client(tmp_path):
    with CDNClient(tmp_path, 'eu', 'wow_classic_era', retries=1) as client:
        client.set_cdn({'Servers': 'https://one.example https://two.example', 'Path': 'tpr/wow'})
        yield client


def transport(monkeypatch, client, responses):
    seen = []
    @contextmanager
    def connection(url):
        seen.append(url)
        yield Connection(responses[len(seen) - 1]), '/object'
    monkeypatch.setattr(client, '_connection', connection)
    return seen


def test_progress_reports_partial_socket_reads_before_response_completes(client, monkeypatch):
    response = Response([b'ab', b'cd'], 4)
    transport(monkeypatch, client, [response])
    events = []
    data = client._bytes(['https://one.example/object'], 4,
                         progress=lambda n: events.append((n, response.reads)))
    assert data == b'abcd'
    assert events == [(0, 0), (2, 1), (4, 2)]


@pytest.mark.parametrize('silent', [True, False])
def test_stalled_host_fails_over_and_cools_down(client, monkeypatch, silent):
    clock = Clock()
    monkeypatch.setattr(cdn.time, 'monotonic', clock)
    slow = Response([TimeoutError('silent')] if silent else [b'a', b'b'], 4,
                    clock, 3)
    seen = transport(monkeypatch, client, [slow, Response([b'abcd'], 4)])
    key = 'aa' * 16
    assert client._bytes(client._urls(key), 4) == b'abcd'
    assert [url.split('/')[2] for url in seen] == ['one.example', 'two.example']
    assert slow.reads == (1 if silent else 2)
    for _ in range(4):
        assert client._urls(key)[0].startswith('https://two.example/')
    clock.now += 31
    assert client._urls(key)[0].startswith('https://one.example/')


def test_socket_inactivity_timeout_is_bounded(client, monkeypatch):
    created = []
    class HTTPS:
        def __init__(self, host, port, **kwargs):
            created.append(kwargs['timeout'])
        def close(self):
            pass
    monkeypatch.setattr(cdn.http.client, 'HTTPSConnection', HTTPS)
    with client._connection('https://one.example/object'):
        pass
    assert created == [5]


def test_throttle_time_is_not_mistaken_for_a_stalled_host(client, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(cdn.time, 'monotonic', clock)
    client.throttle = Throttle(10, clock=clock, sleep=clock.sleep)
    transport(monkeypatch, client, [Response([b'a'] * 100, 100, clock)])
    assert client._bytes(['https://one.example/object'], 100) == b'a' * 100
    assert clock.now == pytest.approx(10)


def test_mirrors_rotate_and_hosts_use_verified_https(client):
    client.set_cdn({'Servers': 'https://one.example/?policy=1',
                    'Hosts': 'two.example', 'Path': 'tpr/wow'})
    first, second = client._urls('ab' * 16), client._urls('ab' * 16)
    assert first == second[::-1]
    assert {url.split('/')[2] for url in first} == {'one.example', 'two.example'}
    assert all(url.startswith('https://') and '?' not in url for url in first)
    assert client._ssl.check_hostname


def object_for(payload):
    data = b'BLTE\0\0\0\0N' + payload
    return DownloadEntry(hashlib.md5(data).digest(), len(data), 0), data


def test_group_progress_excludes_archive_gaps(client, monkeypatch):
    first, one = object_for(b'one')
    last, two = object_for(b'two')
    gap = b'padding' * 10
    data = one + gap + two
    group = [(first, Location('ab' * 16, 123, len(one))),
             (last, Location('ab' * 16, 123 + len(one) + len(gap), len(two)))]
    def get_range(location, *, validator, progress):
        for size in [len(one), len(one) + len(gap), len(data)]:
            progress(size)
        validator(data)
        return data
    monkeypatch.setattr(client, 'get_range', get_range)
    received = []
    assert client._fetch_group(group, True, received.append) == [(first, one), (last, two)]
    assert received == [0, len(one), len(one), len(one) + len(two)]


class InlineExecutor:
    """Deterministic completions, including submission before consumer work."""
    def __init__(self, **kwargs):
        self.submissions = 0

    def submit(self, function, *args):
        self.submissions += 1
        future = Future()
        try:
            future.set_result(function(*args))
        except BaseException as error:
            future.set_exception(error)
        return future

    def shutdown(self, **kwargs):
        pass


def bulk_fixture(client, monkeypatch, groups):
    executor = InlineExecutor()
    monkeypatch.setattr(cdn, 'ThreadPoolExecutor', lambda **kwargs: executor)
    monkeypatch.setattr(client, 'prepare_indexes', lambda: None)
    monkeypatch.setattr(client, '_planned_groups', lambda *args: (group for group in groups))
    return executor


def test_bulk_refills_before_storage_and_never_counts_retries_twice(client, monkeypatch):
    entry, data = object_for(b'payload')
    client.workers = 1
    executor = bulk_fixture(client, monkeypatch, [[entry]] * 3)
    def fetch(items, as_bytes, progress):
        for n in [5, 10, 0, 5, len(data)]:
            progress(n)
        return [(entry, data)]
    monkeypatch.setattr(client, '_fetch_group', fetch)
    received = []
    iterator = client.fetch_many([], as_bytes=True, progress=received.append)
    assert next(iterator) == (entry, data)
    assert executor.submissions == 2
    assert list(iterator) == [(entry, data)] * 2
    assert received[-1] == len(data) * 3
    assert received == sorted(received)


def test_cancellation_stops_bulk_before_scheduling_and_during_read(client, monkeypatch):
    executor = bulk_fixture(client, monkeypatch, [[None]] * 3)
    with pytest.raises(TransferCancelled):
        list(client.fetch_many([], cancelled=lambda: True))
    assert executor.submissions == 0
    response = Response([b'ab', b'cd'], 4)
    transport(monkeypatch, client, [response])
    def check():
        if response.reads:
            raise TransferCancelled()
    client._transfer.check = check
    with pytest.raises(TransferCancelled):
        client._bytes(['https://one.example/object', 'https://two.example/object'], 4)
    assert response.reads == 1


def test_bulk_heartbeat_reports_silence_and_consumes_ready_groups_first(client, monkeypatch):
    entry, data = object_for(b'payload')
    executor = bulk_fixture(client, monkeypatch, [[entry]] * 2)
    futures = []
    def submit(*args):
        future = Future()
        futures.append(future)
        if len(futures) == 2:
            future.set_result([(entry, data)])
        return future
    monkeypatch.setattr(executor, 'submit', submit)
    calls = 0
    def wait(pending, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs['timeout'] <= .5
        if calls == 1:
            return set(), pending
        if calls == 2:
            return {futures[1]}, {futures[0]}
        futures[0].set_result([(entry, data)])
        return {futures[0]}, set()
    monkeypatch.setattr(cdn, 'wait', wait)
    received = []
    iterator = client.fetch_many([], progress=received.append)
    assert next(iterator) == (entry, data)
    assert not futures[0].done()
    assert len(received) >= 3
    assert list(iterator) == [(entry, data)]


def test_host_scheduling_probes_once_then_weights_rate_by_load(client):
    key = 'aa' * 16
    client._host_active['two.example'] = 1
    assert client._urls(key)[0].startswith('https://one.example/')
    client._host_rate.update({'one.example': 10_000_000, 'two.example': 1_000_000})
    client._host_active.update({'one.example': 5, 'two.example': 0})
    assert client._urls(key)[0].startswith('https://one.example/')
    client._host_active['one.example'] = 15
    assert client._urls(key)[0].startswith('https://two.example/')


def test_unframed_response_still_rejects_bytes_past_expected_size(client, monkeypatch):
    class Unframed(Response):
        def getheader(self, name, default=None):
            return default
    transport(monkeypatch, client, [Unframed([b'abcd', b'e'], 5)])
    with pytest.raises(cdn.IntegrityError, match='exceeds its bounded size'):
        client._bytes(['https://one.example/object'], 4, expected_total=4)
