"""Compare a baseline CDN engine with this checkout using bounded public content.

Run with PYTHONPATH=src nix develop -c python3 tools/benchmark_downloads.py.
All content and XDG directories live in a temporary root and are removed on exit.
The JSON output includes one-second displayed rates and per-request timing.
"""
from __future__ import annotations

import argparse
from collections import Counter
import importlib.util
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit


class Meter:
    def __init__(self):
        self.wire = 0
        self.lock = threading.Lock()
        self.requests = []

    def attach(self, client):
        original = client._read
        meter = self
        class Stream:
            def __init__(self, stream):
                self.stream = stream
            def __getattr__(self, name):
                return getattr(self.stream, name)
            def write(self, block):
                with meter.lock:
                    meter.wire += len(block)
                    if meter.wire > 2_900_000_000:
                        raise RuntimeError('Benchmark reached its total wire budget')
                return self.stream.write(block)
        def read(url, stream, *args, **kwargs):
            started = time.monotonic()
            error = None
            try:
                return original(url, Stream(stream), *args, **kwargs)
            except Exception as exc:
                error = type(exc).__name__
                raise
            finally:
                with meter.lock:
                    meter.requests.append({'host': urlsplit(url).hostname,
                        'seconds': time.monotonic() - started, 'error': error})
        client._read = read


def measure(client, entries, root, meter, *, modern):
    from scuffle.ngdp.store import CascStore
    from scuffle.rates import RateSmoother
    store = CascStore(root)
    smoother = RateSmoother()
    samples, history = [], []
    shown = 0
    last_emit = 0
    stored = 0
    write_seconds = 0
    wire_start, request_start = meter.wire, len(meter.requests)
    stop = threading.Event()
    started = time.monotonic()
    def report(amount):
        nonlocal shown, last_emit
        now = time.monotonic()
        if now - last_emit < .5:
            return
        last_emit = now
        if modern:
            shown = smoother.update(amount)
        else:
            history.append((now, amount))
            while history and now - history[0][0] > 5:
                history.pop(0)
            if len(history) > 1:
                shown = (history[-1][1] - history[0][1]) / (history[-1][0] - history[0][0])
    def monitor():
        while not stop.wait(1):
            samples.append({'seconds': time.monotonic() - started, 'speed': shown,
                            'wire': meter.wire - wire_start, 'stored': stored})
    thread = threading.Thread(target=monitor)
    thread.start()
    try:
        options = {'progress': report} if modern else {}
        for index, (entry, payload) in enumerate(client.fetch_many(entries, as_bytes=True, **options), 1):
            before = time.monotonic()
            store.put(entry.ekey, payload)
            if index % 256 == 0:
                store.checkpoint()
            write_seconds += time.monotonic() - before
            stored += entry.encoded_size
            if not modern:
                report(stored)
        store.checkpoint()
    finally:
        elapsed = time.monotonic() - started
        stop.set()
        thread.join()
        store.close()
        shutil.rmtree(root)
    values = [sample['speed'] for sample in samples if sample['seconds'] >= 5]
    mean = statistics.mean(values) if values else 0
    requests = meter.requests[request_start:]
    return {'seconds': elapsed, 'stored_bytes': stored, 'wire_bytes': meter.wire - wire_start,
            'MBps': stored / elapsed / 1e6, 'shown_mean_MBps': mean / 1e6,
            'shown_cv': statistics.pstdev(values) / mean if mean else None,
            'write_seconds': write_seconds, 'requests': dict(Counter(r['host'] for r in requests)),
            'request_details': requests, 'samples': samples}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', default='a08f761')
    parser.add_argument('--mib', type=int, default=512, help='MiB per run, between 128 and 768')
    args = parser.parse_args()
    if not 128 <= args.mib <= 768:
        parser.error('--mib must be between 128 and 768')
    with tempfile.TemporaryDirectory(prefix='scuffle-cdn-', dir='/tmp') as folder:
        root = Path(folder)
        for name in ['CACHE', 'CONFIG', 'DATA']:
            os.environ[f'XDG_{name}_HOME'] = str(root / name.lower())
        from scuffle.ngdp.cdn import CDNClient, parse_config
        from scuffle.ngdp.models import DownloadEntry
        source = root / 'baseline.py'
        source.write_bytes(subprocess.check_output(
            ['git', 'show', f'{args.baseline}:src/scuffle/ngdp/cdn.py']))
        spec = importlib.util.spec_from_file_location('scuffle.ngdp.benchmark_baseline', source)
        baseline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(baseline)
        meter = Meter()
        results = {}
        with baseline.CDNClient(root / 'cache', 'eu', 'wow_classic_era', workers=16) as client:
            meter.attach(client)
            version, cdn = client.discover()
            config = parse_config(client.get_config(version['CDNConfig']))
            subset = {'archives': config['archives'][:4]}
            client.configure_archives(subset)
            client.prepare_indexes()
            entries, size = [], 0
            with client._database() as db:
                for key, amount in db.execute('SELECT ekey,size FROM objects ORDER BY archive,offset'):
                    if size + amount > args.mib * 1024 * 1024:
                        break
                    entries.append(DownloadEntry(key, amount, 0))
                    size += amount
            results['before'] = measure(client, entries, root / 'before', meter, modern=False)
        with CDNClient(root / 'cache', 'eu', 'wow_classic_era', workers=16) as client:
            meter.attach(client)
            client.set_cdn(cdn)
            client.configure_archives(subset)
            client.prepare_indexes()
            results['after'] = measure(client, entries, root / 'after', meter, modern=True)
        print(json.dumps({'baseline': args.baseline, 'product': 'wow_classic_era', 'region': 'eu',
                          'version': version['VersionsName'], 'wire_bytes_total': meter.wire,
                          'objects': len(entries), 'results': results}, indent=2))


if __name__ == '__main__':
    main()
