"""A finished download gives its memory back, measured in a process of its own."""
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ENVIRONMENT = {**os.environ, 'PYTHONPATH': str(Path(__file__).parent.parent / 'src')}

# The shape of a real download: many threads read groups of 8 MB, cut them into objects, and one
# other thread frees them while a small record of each stays behind.
DOWNLOAD = r'''
import json, queue, threading
from scuffle import memory
memory.tune()

def anonymous():
    for line in open('/proc/self/status'):
        if line.startswith('RssAnon'):
            return int(line.split()[1]) // 1024

before = anonymous()
ready, kept = queue.Queue(32), []

def network():
    for _ in range(40):
        group = bytes(8 * 1024 * 1024)
        ready.put([group[start:start + 200_000] for start in range(0, len(group), 200_000)])
        del group

threads = [threading.Thread(target=network) for _ in range(16)]
for thread in threads:
    thread.start()
for _ in range(16 * 40):
    for payload in ready.get():
        kept.append(bytes(payload[:32]))
for thread in threads:
    thread.join()
memory.release()
print(json.dumps({'before': before, 'after': anonymous()}))
'''


def has_glibc() -> bool:
    return sys.platform.startswith('linux') and hasattr(ctypes.CDLL(None), 'mallopt')


@pytest.mark.skipif(not has_glibc(), reason='only glibc keeps freed download buffers')
def test_a_finished_download_gives_its_memory_back_to_the_system():
    result = subprocess.run([sys.executable, '-c', DOWNLOAD], env=ENVIRONMENT, capture_output=True,
                            text=True, timeout=120, check=True)
    measured = json.loads(result.stdout)
    assert measured['after'] - measured['before'] < 30, measured


def test_the_allocator_calls_are_harmless_everywhere():
    subprocess.run([sys.executable, '-c', 'from scuffle import memory; memory.tune(); memory.release()'],
                   env=ENVIRONMENT, check=True, timeout=30)
