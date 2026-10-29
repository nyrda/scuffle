import hashlib
import os
from pathlib import Path
import struct
import subprocess
import sys
import zlib

import pytest

from waystone.ngdp.store import (
    CascSource, CascStore, SEGMENT_SIZE, StoreError, _index_entries,
    _legacy_span_header, _span_header, bucket_for_key, hashlittle, hashlittle2,
)


def blte(content: bytes, *, table: bool = True) -> tuple[bytes, bytes]:
    chunk = b"Z" + zlib.compress(content)
    if table:
        header = (b"BLTE" + struct.pack(">I", 36) + b"\x0f\0\0\1"
                  + struct.pack(">II", len(chunk), len(content))
                  + hashlib.md5(chunk).digest())
        return hashlib.md5(header).digest(), header + chunk
    encoded = b"BLTE\0\0\0\0N" + content
    return hashlib.md5(encoded).digest(), encoded


@pytest.mark.parametrize("data,expected", [
    (b"", (3735928559, 3735928559)),
    (b"a", (1490454280, 1478903724)),
    (b"Four score and seven years ago", (393676113, 3463587558)),
    (bytes(range(18)), (3610378301, 1506651716)),
    (bytes(range(255)), (2062311032, 1486440092)),
])
def test_lookup3_casclib_vectors(data, expected):
    # Fixed vectors obtained from CascLib's compiled public-domain lookup3.c.
    assert hashlittle2(data) == expected


def test_append_publish_and_reopen(tmp_path):
    root = tmp_path / "install"
    objects = [blte(f"content {i}".encode(), table=i % 2 == 0) for i in range(50)]
    with CascStore(root) as store:
        for key, data in objects:
            store.put(key, data)
        store.put(*objects[0])
        assert store.count == 50
        files = store.publish()
        assert len(files) == 16
        seen = {}
        for bucket, path in enumerate(files):
            assert path.stat().st_size >= 65536
            for prefix, segment, offset, size in _index_entries(path, bucket):
                assert bucket_for_key(prefix) == bucket
                seen[prefix] = (segment, offset, size)
        assert len(seen) == 50
        assert not (store.data_dir / "shmem").exists()
        for key, data in objects:
            assert store.read(key) == data
            segment, offset, size = seen[key[:9]]
            assert segment == 0
            with (store.data_dir / "data.000").open("rb") as stream:
                stream.seek(offset)
                assert stream.read(30) == _span_header(key, len(data), offset)
        all_entries = list(store.iter_entries())
        assert all(location.size == len(dict(objects)[key]) for key, location in all_entries)
    with CascStore(root) as reopened:
        assert reopened.count == 50
        for key, data in objects:
            assert reopened.has(key)
            assert reopened.get(key) == data
        destination = tmp_path / "copy.blte"
        reopened.extract_to(objects[0][0], destination)
        assert destination.read_bytes() == objects[0][1]


def test_rejects_wrong_identity_and_corrupt_chunks(tmp_path):
    key, data = blte(b"verified payload")
    with CascStore(tmp_path / "install") as store:
        with pytest.raises(StoreError, match="EKey mismatch"):
            store.put(b"0" * 16, data)
        with pytest.raises(StoreError, match="chunk checksum"):
            store.put(key, data[:-1] + bytes([data[-1] ^ 1]))
        assert store.count == 0
        assert not list(store.data_dir.glob("data.*"))


def test_interrupted_tail_recovers_complete_orphan_and_preserves_partial(tmp_path):
    root = tmp_path / "install"
    key, data = blte(b"durable")
    with CascStore(root) as store:
        store.put(key, data)
        store.checkpoint()
    script = r'''
import os,sys
from pathlib import Path
from waystone.ngdp.store import CascStore,_span_header
root=Path(sys.argv[1]);key=bytes.fromhex(sys.argv[2]);data=bytes.fromhex(sys.argv[3])
store=CascStore(root);store.put(key,data)
# Simulate an append whose catalogue transaction did not reach disk.
store._db.execute("DELETE FROM objects WHERE ekey=?",(key,));store._db.commit()
path=store.data_dir/"data.000";offset=path.stat().st_size
with path.open("ab") as f:
    f.write(_span_header(key,len(data),offset));f.write(data[:10]);f.flush()
os._exit(0)
'''
    orphan_key, orphan = blte(b"complete but uncommitted")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(sys.path)
    subprocess.run([sys.executable, "-c", script, str(root), orphan_key.hex(), orphan.hex()],
                   check=True, env=environment)
    physical_end = (root / "Data/data/data.000").stat().st_size
    with CascStore(root) as recovered:
        assert recovered.read(key) == data
        assert recovered.read(orphan_key) == orphan
        assert recovered.count == 2
        assert (recovered.data_dir / "data.000").stat().st_size == physical_end
        next_key, next_data = blte(b"append after the preserved partial span")
        recovered.put(next_key, next_data)
        assert recovered._location(next_key)[1] == physical_end
        assert recovered.read(next_key) == next_data
        recovered.publish()


def test_a_crash_before_a_batch_commits_loses_no_appended_object(tmp_path):
    root = tmp_path / "install"
    objects = [blte(f"batched {i}".encode()) for i in range(5)]
    script = r'''
import os,sys
from pathlib import Path
from waystone.ngdp.store import CascStore
store=CascStore(Path(sys.argv[1]))
for pair in sys.argv[2:]:
    key,data=(bytes.fromhex(x) for x in pair.split(":"))
    store.put(key,data)
assert store._db.in_transaction
os._exit(0)
'''
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(sys.path)
    subprocess.run([sys.executable, "-c", script, str(root), *(f"{k.hex()}:{d.hex()}" for k, d in objects)],
                   check=True, env=environment)
    with CascStore(root) as recovered:
        assert [recovered.read(key) for key, _ in objects] == [data for _, data in objects]
        end = (recovered.data_dir / "data.000").stat().st_size
        key, data = blte(b"appended after recovery")
        recovered.put(key, data)
        assert recovered._location(key)[1] == end


def test_a_failed_append_rolls_back_alone_and_leaves_no_bytes(tmp_path, monkeypatch):
    from waystone.ngdp import store as store_module
    root = tmp_path / "install"
    (first, first_data), (failed, failed_data), (last, last_data) = (blte(f"object {i}".encode()) for i in range(3))
    with CascStore(root) as store:
        store.put(first, first_data)
        real_copy = store_module.shutil.copyfileobj

        def broken(source, target, length):
            target.write(b"half")
            raise OSError("disk went away")
        monkeypatch.setattr(store_module.shutil, "copyfileobj", broken)
        with pytest.raises(OSError, match="disk went away"):
            store.put(failed, failed_data)
        monkeypatch.setattr(store_module.shutil, "copyfileobj", real_copy)
        store.put(last, last_data)
        assert store._location(last)[1] == len(first_data) + 30
    with CascStore(root) as store:
        assert store.read(first) == first_data and store.read(last) == last_data
        assert not store.has(failed)
        assert (store.data_dir / "data.000").stat().st_size == len(first_data) + len(last_data) + 60


def test_seed_reads_reject_a_corrupt_candidate(tmp_path):
    root = tmp_path / "source"
    (good, good_data), (bad, bad_data) = blte(b"intact"), blte(b"damaged on disk")
    with CascStore(root) as store:
        store.put(good, good_data)
        store.put(bad, bad_data)
        store.publish()
    with (root / "Data/data/data.000").open("r+b") as stream:
        stream.seek(-1, os.SEEK_END)
        stream.write(bytes([bad_data[-1] ^ 1]))
    with CascSource(root) as source:
        assert source.read(good) == good_data
        with pytest.raises(StoreError, match="chunk checksum"):
            source.read(bad)
        assert source.read(good) == good_data


def test_detects_loss_of_previously_durable_data(tmp_path):
    root = tmp_path / "install"
    key, data = blte(b"durable")
    with CascStore(root) as store:
        store.put(key, data)
    path = root / "Data" / "data" / "data.000"
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(StoreError, match="durable CASC segment was truncated"):
        CascStore(root)


def test_seed_is_read_only_and_result_is_independent(tmp_path):
    original = tmp_path / "official"
    target = tmp_path / "new-install"
    key, data = blte(b"reuse this content" * 100)
    with CascStore(original) as official:
        official.put(key, data)
        official.publish()
    # Current official files can have zeroed span wrappers. Derive the key
    # from BLTE instead of trusting the wrapper's full-key field.
    with (original / "Data/data/data.000").open("r+b") as stream:
        stream.write(b"\0" * 30)
    before = {str(p.relative_to(original)): (p.stat().st_size, p.stat().st_mtime_ns)
              for p in original.rglob("*") if p.is_file()}
    with CascStore(target) as store:
        with store.import_existing(original) as source:
            assert source.count == 1
            assert source.has(key)
            assert not source.has(key[:9] + b"\xff" * 7)
            assert not store.has(key)
            with pytest.raises(ValueError, match="read-only source root"):
                source.extract_to(key, original / "must-not-write.blte")
            extracted = tmp_path / "seed.blte"
            source.extract_to(key, extracted)
            store.put(key, extracted)
        after = {str(p.relative_to(original)): (p.stat().st_size, p.stat().st_mtime_ns)
                 for p in original.rglob("*") if p.is_file()}
        assert before == after
        original.rename(tmp_path / "official-unavailable")
        store.publish()
        assert store.read(key) == data
    with CascSource(target) as independent:
        assert independent.read(key) == data


def test_source_rejects_corrupt_index_guard(tmp_path):
    root = tmp_path / "source"
    key, data = blte(b"guarded")
    with CascStore(root) as store:
        store.put(key, data)
        path = store.publish()[bucket_for_key(key)]
    with path.open("r+b") as stream:
        stream.seek(4)
        stream.write(b"\0" * 4)
    with pytest.raises(StoreError, match="header checksum"):
        CascSource(root)


def test_publication_generations_keep_current_and_previous(tmp_path):
    with CascStore(tmp_path / "install") as store:
        key, data = blte(b"generation one")
        store.put(key, data)
        first = store.publish()
        second = store.publish()
        third = store.publish()
        assert not any(path.exists() for path in first)
        assert all(path.exists() for path in second + third)
        assert len(list(store.data_dir.glob("*.idx"))) == 32
        with CascSource(store.root) as source:
            assert source.read(key) == data


def test_segment_boundary_without_copying_gigabyte(tmp_path):
    key, data = blte(b"first")
    next_key, next_data = blte(b"second")
    with CascStore(tmp_path / "install") as store:
        store.put(key, data)
        # A sparse segment exercises the production 30-bit rollover boundary.
        path = store.data_dir / "data.000"
        with path.open("r+b") as stream:
            stream.truncate(SEGMENT_SIZE - 10)
        store._db.execute("UPDATE segments SET tail_end=?,durable_end=?", (SEGMENT_SIZE - 10,) * 2)
        store._db.commit()
        store.put(next_key, next_data)
        assert store._location(next_key) == (1, 0, len(next_data))
        assert store.read(next_key) == next_data
        paths = store.publish()
        entries = dict((prefix, (segment, offset, size)) for prefix, segment, offset, size
                       in _index_entries(paths[bucket_for_key(next_key)], bucket_for_key(next_key)))
        assert entries[next_key[:9]] == (1, 0, len(next_data))


def test_refuses_unowned_data_files(tmp_path):
    data_dir = tmp_path / "Data/data"
    data_dir.mkdir(parents=True)
    (data_dir / "data.000").write_bytes(b"existing user data")
    with pytest.raises(StoreError, match="without this updater's catalogue"):
        CascStore(tmp_path)
    assert (data_dir / "data.000").read_bytes() == b"existing user data"


def test_bulk_seed_join_sorts_by_physical_read_order(tmp_path):
    root = tmp_path / "source"
    objects = [blte(f"seed {i}".encode()) for i in range(20)]
    with CascStore(root) as store:
        for key, data in objects:
            store.put(key, data)
        store.publish()
    with CascSource(root) as source:
        requested = [key for key, _ in reversed(objects)] + [b"\xff" * 16]
        candidates = list(source.iter_import_candidates(requested))
        assert [key for key, _ in candidates] == [key for key, _ in objects]
        assert [location.offset for _, location in candidates] == sorted(location.offset for _, location in candidates)
        for key, location in candidates:
            assert source.locate(key) == location
            assert source.verify(key)


def test_streaming_verifier_detects_chunk_corruption(tmp_path):
    key, data = blte(b"chunk integrity")
    with CascStore(tmp_path / "install") as store:
        store.put(key, data)
        assert store.verify(key)
        path = store.data_dir / "data.000"
        with path.open("r+b") as stream:
            stream.seek(-1, os.SEEK_END)
            stream.write(bytes([data[-1] ^ 1]))
        with pytest.raises(StoreError, match="chunk checksum"):
            store.verify(key)


def test_invalidate_reappends_verified_object_without_rewriting_corrupt_span(tmp_path):
    root = tmp_path / "install"
    key, data = blte(b"replace this corrupt body")
    other_key, other_data = blte(b"preserve this other object")
    with CascStore(root) as store:
        store.put(key, data)
        store.put(other_key, other_data)
        old_location = store._location(key)
        path = store.data_dir / "data.000"
        with path.open("r+b") as stream:
            stream.seek(old_location[1] + 30 + len(data) - 1)
            stream.write(bytes([data[-1] ^ 1]))
        old_bytes = path.read_bytes()
        with pytest.raises(StoreError, match="chunk checksum"):
            store.verify(key)
        store.invalidate(key)
        store.invalidate(key)
        assert not store.has(key)
        assert path.read_bytes() == old_bytes
    # Reopening between invalidation and replacement must not rediscover the
    # invalidated span, including when it had not been checkpointed before.
    with CascStore(root) as store:
        assert not store.has(key)
        assert store.read(other_key) == other_data
        store.put(key, data)
        assert store._location(key)[1] == len(old_bytes)
        assert path.read_bytes()[:len(old_bytes)] == old_bytes
        assert store.verify(key)
        paths = store.publish()
        entry = next(row for row in _index_entries(paths[bucket_for_key(key)], bucket_for_key(key))
                     if row[0] == key[:9])
        assert entry[2] == len(old_bytes)
    with CascSource(root) as published:
        assert published.read(key) == data


@pytest.mark.parametrize("damage", ["missing", "truncated"])
def test_explicit_segment_repair_drops_damaged_inventory_and_preserves_other_segment(tmp_path, damage):
    root = tmp_path / "install"
    lost_key, lost_data = blte(b"requires redownload")
    kept_key, kept_data = blte(b"healthy separate segment")
    with CascStore(root) as store:
        store.put(lost_key, lost_data)
        # Start another owned segment without creating a gigabyte fixture.
        store._db.execute("INSERT INTO segments VALUES(1,0,0)")
        store._db.commit()
        store.put(kept_key, kept_data)
        store.publish()
    damaged = root / "Data/data/data.000"
    healthy = root / "Data/data/data.001"
    before = healthy.read_bytes()
    if damage == "missing":
        damaged.unlink()
    else:
        with damaged.open("r+b") as stream:
            stream.truncate(10)
    with pytest.raises(StoreError, match="durable CASC segment"):
        CascStore(root)
    assert CascStore.recover_damaged_segments(root) == ["data.000"]
    assert damaged.read_bytes() == b""
    assert healthy.read_bytes() == before
    assert CascStore.recover_damaged_segments(root) == []
    with CascStore(root) as repaired:
        assert repaired.count == 1
        assert not repaired.has(lost_key)
        assert repaired.read(kept_key) == kept_data
        repaired.put(lost_key, lost_data)
        assert repaired.verify(lost_key)
        repaired.publish()
    with CascSource(root) as published:
        assert published.read(lost_key) == lost_data
        assert published.read(kept_key) == kept_data


def test_segment_repair_refuses_unowned_root(tmp_path):
    data_dir = tmp_path / "Data/data"
    data_dir.mkdir(parents=True)
    data = data_dir / "data.000"
    data.write_bytes(b"official store without updater catalogue")
    with pytest.raises(StoreError, match="existing catalogue"):
        CascStore.recover_damaged_segments(tmp_path)
    assert data.read_bytes() == b"official store without updater catalogue"
    assert not (tmp_path / ".waystone").exists()


def test_verify_indexes_checks_latest_generation_without_mutation(tmp_path):
    with CascStore(tmp_path / "install") as store:
        for i in range(100):
            store.put(*blte(f"indexed {i}".encode()))
        previous = store.publish()
        current = store.publish()
        # An obsolete generation does not control the published mapping.
        previous[0].write_bytes(b"obsolete corrupt index")
        before = {path.name: (hashlib.sha256(path.read_bytes()).digest(), path.stat().st_mtime_ns)
                  for path in current}
        changes = store._db.total_changes
        assert store.verify_indexes()
        assert store._db.total_changes == changes
        assert before == {path.name: (hashlib.sha256(path.read_bytes()).digest(), path.stat().st_mtime_ns)
                          for path in current}


def test_verify_indexes_rejects_missing_bucket(tmp_path):
    with CascStore(tmp_path / "install") as store:
        paths = store.publish()
        paths[7].unlink()
        with pytest.raises(StoreError, match="missing published CASC index buckets: 07"):
            store.verify_indexes()


@pytest.mark.parametrize("offset", [4, 36])
def test_verify_indexes_rejects_corrupt_header_and_empty_entry_guards(tmp_path, offset):
    with CascStore(tmp_path / "install") as store:
        paths = store.publish()
        # Empty buckets still require an intact zero-length entry guard.
        with paths[0].open("r+b") as stream:
            stream.seek(offset)
            stream.write(b"\xff" * 4)
        with pytest.raises(StoreError, match="checksum mismatch"):
            store.verify_indexes()


def test_verify_indexes_rejects_checksummed_wrong_location(tmp_path):
    key, data = blte(b"index location integrity")
    with CascStore(tmp_path / "install") as store:
        store.put(key, data)
        path = store.publish()[bucket_for_key(key)]
        content = bytearray(path.read_bytes())
        entry = bytes(content[40:49]) + (1).to_bytes(5, "big") + bytes(content[54:58])
        content[40:58] = entry
        content[36:40] = struct.pack("<I", hashlittle2(entry)[0])
        path.write_bytes(content)
        with pytest.raises(StoreError, match="differs from catalogue"):
            store.verify_indexes()


def test_publish_removes_only_managed_shmem_before_new_generations(tmp_path, monkeypatch):
    with CascStore(tmp_path / "install") as store:
        store.put(*blte(b"a new generation"))
        runtime_map = store.data_dir / "shmem"
        runtime_map.write_bytes(b"game-created runtime state")
        outside_map = tmp_path / "other-install-shmem"
        outside_map.write_bytes(b"preserve another installation")
        real_replace = os.replace

        def checked_replace(source, destination):
            assert not runtime_map.exists()
            return real_replace(source, destination)

        monkeypatch.setattr(os, "replace", checked_replace)
        store.publish()
        assert not runtime_map.exists()
        assert outside_map.read_bytes() == b"preserve another installation"
        assert store.verify_indexes()


def test_publish_refuses_symlinked_shmem_without_touching_target(tmp_path):
    with CascStore(tmp_path / "install") as store:
        store.publish()
        before = set(store.data_dir.glob("*.idx"))
        outside = tmp_path / "original-install-shmem"
        outside.write_bytes(b"external runtime state")
        runtime_map = store.data_dir / "shmem"
        runtime_map.symlink_to(outside)
        with pytest.raises(StoreError, match="symlinked runtime map"):
            store.publish()
        assert runtime_map.is_symlink()
        assert outside.read_bytes() == b"external runtime state"
        assert set(store.data_dir.glob("*.idx")) == before


@pytest.mark.parametrize("segment,offset,size,key,expected", [
    # Read-only current official Data/data captures. Each full EKey was also
    # independently derived from its BLTE bytes, not just its wrapper field.
    (128, 12055276, 72714, "000002248a20b8e419929c5b23a283e8",
     "e883a2235b9c9219e4b8208a24020000281c01000000c7d9bb060a549ba5"),
    (134, 456733689, 119369, "00029a673b04923fe2bd8d6e97c67e13",
     "137ec6976e8dbde23f92043b679a020067d2010000001cb784045274aec1"),
    (133, 244898130, 109324, "000c20754b22aed57ade8a5658c03b38",
     "383bc058568ade7ad5ae224b75200c002aab01000000a1e98d2158cf62ff"),
    (132, 914165499, 110314, "00108fd48aa76e068ff21874d79afaae",
     "aefa9ad77418f28f066ea78ad48f100008af0100000036a5968a1ff0a432"),
    (149, 126880753, 141272990, "20b062447b194288c629232e2e424509",
     "0945422e2e2329c68842197b4462b020bca76b080000f3467b9b71b7a7d2"),
])
def test_span_header_matches_current_official_vectors(segment, offset, size, key, expected):
    header = _span_header(bytes.fromhex(key), size, offset, segment)
    assert header.hex() == expected


def test_upgrade_owned_legacy_headers_preserves_payloads_and_indexes(tmp_path):
    root = tmp_path / "install"
    objects = [blte(b"prefix", table=False), blte(b"legacy segment zero"), blte(b"legacy segment one")]
    with CascStore(root) as store:
        store.put(*objects[0])
        store.put(*objects[1])
        store._db.execute("INSERT INTO segments VALUES(1,0,0)")
        store._db.commit()
        store.put(*objects[2])
        indexes = store.publish()
        locations = [(key, *store._location(key)) for key, _ in objects]
    for key, segment, offset, size in locations:
        with (root / "Data/data" / f"data.{segment:03d}").open("r+b") as stream:
            stream.seek(offset)
            stream.write(_legacy_span_header(key, size, offset))
    before = {segment: (root / "Data/data" / f"data.{segment:03d}").read_bytes() for segment in (0, 1)}
    before_indexes = {path: path.read_bytes() for path in indexes}
    unknown = root / "Data/data/data.998"
    unknown.write_bytes(b"uncatalogued bytes must stay untouched")
    assert CascStore.upgrade_span_headers(root) == 2
    assert CascStore.upgrade_span_headers(root) == 0
    assert unknown.read_bytes() == b"uncatalogued bytes must stay untouched"
    for key, segment, offset, size in locations:
        after = (root / "Data/data" / f"data.{segment:03d}").read_bytes()
        assert after[offset:offset + 30] == _span_header(key, size, offset, segment)
        assert after[offset:offset + 26] == before[segment][offset:offset + 26]
        assert after[offset + 30:offset + 30 + size] == before[segment][offset + 30:offset + 30 + size]
    assert all(path.read_bytes() == data for path, data in before_indexes.items())
    unknown.unlink()
    with CascStore(root) as store:
        for key, data in objects:
            assert store.verify(key)
            assert store.read(key) == data


def test_upgrade_rejects_unfamiliar_header_without_changing_it(tmp_path):
    root = tmp_path / "install"
    key, data = blte(b"do not rewrite a corrupt header")
    with CascStore(root) as store:
        store.put(key, data)
    path = root / "Data/data/data.000"
    content = bytearray(path.read_bytes())
    content[29] ^= 1
    path.write_bytes(content)
    with pytest.raises(StoreError, match="unfamiliar or corrupt"):
        CascStore.upgrade_span_headers(root)
    assert path.read_bytes() == content


def test_recovery_preserves_legacy_tail_until_explicit_header_upgrade(tmp_path):
    root = tmp_path / "install"
    key, data = blte(b"legacy tail")
    first_key, first = blte(b"prefix", table=False)
    with CascStore(root) as store:
        store.put(first_key, first)
        store.put(key, data)
        segment, offset, size = store._location(key)
    path = root / "Data/data/data.000"
    with path.open("r+b") as stream:
        stream.seek(offset)
        stream.write(_legacy_span_header(key, size, offset))
    # Simulate a pre-checkpoint catalogue from the previous updater version.
    import sqlite3
    with sqlite3.connect(root / ".waystone/store.sqlite") as db:
        db.execute("UPDATE segments SET durable_end=0")
    with CascStore(root) as store:
        assert store.has(key)
        assert path.stat().st_size == 60 + len(first) + len(data)
        with pytest.raises(StoreError, match="upgrade_span_headers"):
            store.verify(key)
    assert CascStore.upgrade_span_headers(root) == 1
    with CascStore(root) as store:
        assert store.read(key) == data


@pytest.mark.parametrize("after_commit", [False, True])
def test_interruption_during_a_batch_commit_keeps_catalogue_and_data_in_agreement(tmp_path, monkeypatch,
                                                                                   after_commit):
    from waystone.ngdp import store as store_module
    monkeypatch.setattr(store_module, "COMMIT_OBJECTS", 1)       # this put commits its batch
    root = tmp_path / "install"
    key, data = blte(b"commit completion before signal delivery")
    with CascStore(root) as store:
        real_database = store._db

        class InterruptedCommit:
            interrupted = False

            def __getattr__(self, name):
                return getattr(real_database, name)

            def commit(self):
                if self.interrupted:
                    return real_database.commit()
                self.interrupted = True
                if after_commit:
                    real_database.commit()
                raise KeyboardInterrupt("controlled commit interruption")

        store._db = InterruptedCommit()
        with pytest.raises(KeyboardInterrupt, match="controlled commit interruption"):
            store.put(key, data)
        # Either way the object is whole and recorded. An unfinished commit finishes at close.
        assert store.has(key) and store.verify(key)
        assert (store.data_dir / "data.000").stat().st_size == len(data) + 30
    with CascStore(root) as store:
        assert store.read(key) == data


def test_checkpoint_does_not_mark_a_short_pending_segment_durable(tmp_path):
    with CascStore(tmp_path / "install") as store:
        store.put(*blte(b"check the physical append before marking durable"))
        path = store.data_dir / "data.000"
        intact = path.read_bytes()
        with path.open("r+b") as stream:
            stream.truncate(len(intact) - 1)
        with pytest.raises(StoreError, match="shorter than catalogue before checkpoint"):
            store.checkpoint()
        assert store._db.execute("SELECT durable_end FROM segments").fetchone() == (0,)
        path.write_bytes(intact)


def test_published_kmt_reserves_native_update_pages_at_sorted_boundary(tmp_path):
    with CascStore(tmp_path / "install") as store:
        # 2,000 sorted entries make bucket zero end at 0x8cc8. The previous
        # writer allocated 0x10000 bytes, leaving just 0x7000 after the native
        # reader's 0x9000 update start, below its required 0x7800 bytes.
        value = 0
        while store.count < 2000:
            key, data = blte(struct.pack("<I", value), table=False)
            value += 1
            if bucket_for_key(key) == 0:
                store.put(key, data)
        paths = store.publish()
        assert paths[0].stat().st_size == 0x20000
        for path in paths:
            content = path.read_bytes()
            length = struct.unpack_from("<I", content, 32)[0]
            sorted_end = 40 + length
            update_start = (sorted_end + 4095) & ~4095
            assert len(content) - update_start >= 0x7800
            assert content[update_start:] == bytes(len(content) - update_start)
        assert store.verify_indexes()
        with paths[0].open("r+b") as stream:
            stream.truncate(0x10000)
        # This file still has an intact sorted guard and was accepted by
        # CascLib. The current game's additional KMT condition rejects it.
        with pytest.raises(StoreError, match="truncated KMT update section"):
            store.verify_indexes()


def test_verify_indexes_rejects_an_active_update_overriding_owned_snapshot(tmp_path):
    with CascStore(tmp_path / "install") as store:
        path = store.publish()[0]
        entry = b"\0" * 9 + b"\0" * 5 + struct.pack("<I", 39) + b"\0"
        with path.open("r+b") as stream:
            stream.seek(0x1000)
            stream.write(struct.pack("<I", hashlittle(entry) | 0x80000000) + entry + b"\0")
        with pytest.raises(StoreError, match="unexpected active KMT updates"):
            store.verify_indexes()


def rewrite_index_entries(path, entries):
    """Build a guarded native-format sorted snapshot for mutation fixtures."""
    body = b"".join(prefix + ((segment << 30) | offset).to_bytes(5, "big")
                    + struct.pack("<I", size + 30)
                    for prefix, segment, offset, size in sorted(entries, key=lambda row: row[0]))
    high = low = 0
    for offset in range(0, len(body), 18):
        high, low = hashlittle2(body[offset:offset + 18], high, low)
    content = path.read_bytes()[:32] + struct.pack("<II", len(body), high) + body
    total = (len(content) + 0x17fff) & ~0xffff
    path.write_bytes(content + bytes(total - len(content)))


def test_verify_indexes_accepts_unowned_native_bookkeeping_and_fragments(tmp_path):
    with CascStore(tmp_path / "install") as store:
        key, data = blte(b"owned catalogue mapping")
        store.put(key, data)
        bucket = bucket_for_key(key)
        path = store.publish()[bucket]
        entries = list(_index_entries(path, bucket))
        entries.extend([
            (bytes.fromhex("00010002959760e611"), 1, 0, 0),
            (bytes.fromhex("41cda4088a33f403cf"), 789, 251291350, 40223),
            (bytes.fromhex("41cda4088a33f403cf"), 789, 251291350, 36),
            (bytes.fromhex("7040263056bc21b28b"), 789, 233238762, 0),
            (bytes.fromhex("7040263056bc21b28b"), 789, 233238762, 37397),
        ])
        rewrite_index_entries(path, entries)
        # The test has no data.789. The verifier establishes the owned mapping,
        # not integrity or residency of the game's unowned cache fragments.
        assert store.verify_indexes()
        assert store.count == 1
        assert store.read(key) == data


@pytest.mark.parametrize("zero_first", [False, True])
def test_verify_indexes_rejects_zero_span_collision_with_owned_prefix(tmp_path, zero_first):
    with CascStore(tmp_path / "install") as store:
        key, data = blte(b"owned key must never become a bookkeeping record")
        store.put(key, data)
        bucket = bucket_for_key(key)
        path = store.publish()[bucket]
        entries = list(_index_entries(path, bucket))
        zero = (key[:9], 1, 0, 0)
        rewrite_index_entries(path, [zero] + entries if zero_first else entries + [zero])
        with pytest.raises(StoreError, match="zero-size native span collides with owned prefix"):
            store.verify_indexes()


@pytest.mark.parametrize("changed", [False, True])
def test_verify_indexes_rejects_duplicate_owned_prefix_ambiguity(tmp_path, changed):
    with CascStore(tmp_path / "install") as store:
        key, data = blte(b"one exact mapping per owned key")
        store.put(key, data)
        bucket = bucket_for_key(key)
        path = store.publish()[bucket]
        entries = list(_index_entries(path, bucket))
        prefix, segment, offset, size = entries[0]
        duplicate = (prefix, segment, offset + int(changed), size)
        rewrite_index_entries(path, entries + [duplicate])
        with pytest.raises(StoreError, match="duplicate or ambiguous owned prefix"):
            store.verify_indexes()


def test_verify_indexes_rejects_owned_prefix_in_another_bucket(tmp_path):
    with CascStore(tmp_path / "install") as store:
        key, data = blte(b"owned prefixes cannot hide in a foreign bucket")
        store.put(key, data)
        bucket = bucket_for_key(key)
        paths = store.publish()
        entry = next(_index_entries(paths[bucket], bucket))
        foreign_bucket = (bucket + 1) % 16
        rewrite_index_entries(paths[foreign_bucket], [entry])
        with pytest.raises(StoreError, match="duplicate or ambiguous owned prefix"):
            store.verify_indexes()


def test_recovery_preserves_native_growth_and_known_appends_after_unknown_gaps(tmp_path):
    root = tmp_path / "install"
    original_key, original = blte(b"owned original")
    with CascStore(root) as store:
        store.put(original_key, original)
    first = root / "Data/data/data.000"
    with first.open("ab") as stream:
        stream.write(b"unclaimed game-written tail")
    second = root / "Data/data/data.001"
    second.write_bytes(b"native header and cache bytes outside our catalogue")
    before_first, before_second = first.read_bytes(), second.read_bytes()
    appended_key, appended = blte(b"canonical updater append beyond native bytes")
    store = CascStore(root)
    assert first.read_bytes() == before_first
    assert second.read_bytes() == before_second
    assert store.count == 1
    store.put(appended_key, appended)
    assert store._location(appended_key) == (1, len(before_second), len(appended))
    # Simulate process exit after the batch committed but before close/checkpoint.
    # Recovery must validate the committed known location beyond the unrecognized
    # native prefix, rather than losing it when an orphan scan cannot traverse that
    # prefix. An uncommitted batch there is lost and fetched again.
    store._commit()
    store._db.close()
    store._lock_file.close()
    store._closed = True
    after_append = second.read_bytes()
    with CascStore(root) as recovered:
        assert first.read_bytes() == before_first
        assert second.read_bytes() == after_append
        assert recovered.read(original_key) == original
        assert recovered.read(appended_key) == appended
        assert recovered.count == 2


def test_recovery_seals_oversized_unclaimed_segment_instead_of_overwriting(tmp_path):
    root = tmp_path / "install"
    with CascStore(root):
        pass
    path = root / "Data/data/data.000"
    with path.open("wb") as stream:
        stream.write(b"native bytes")
        stream.truncate(SEGMENT_SIZE + 64)
    key, data = blte(b"append into the next segment")
    with CascStore(root) as store:
        assert path.stat().st_size == SEGMENT_SIZE + 64
        store.put(key, data)
        assert store._location(key) == (1, 0, len(data))
        with path.open("rb") as stream:
            assert stream.read(12) == b"native bytes"


def test_compaction_interrupted_mid_move_keeps_the_object_another_client_needs(tmp_path, monkeypatch):
    import sqlite3
    from waystone.ngdp import store as module
    monkeypatch.setattr(module, "SEGMENT_SIZE", 2048)          # a few objects per segment
    objects = [blte(os.urandom(300) + bytes([i]), table=False) for i in range(8)]
    with CascStore(tmp_path) as casc:
        for key, data in objects:
            casc.put(key, data)
        assert casc._location(objects[-1][0])[0] > casc._location(objects[0][0])[0]
    needed = objects[1][0]                                      # in the first segment, which is mostly dead
    live = tmp_path / "live.sqlite"
    with sqlite3.connect(live) as db:
        db.execute("CREATE TABLE keys(ekey BLOB PRIMARY KEY) WITHOUT ROWID")
        db.executemany("INSERT INTO keys VALUES(?)", [(needed,), *[(k,) for k, _ in objects[4:]]])
    original = CascStore._record
    def crash(self, ekey, *args):
        if ekey == needed:
            raise OSError("simulated crash while moving")
        original(self, ekey, *args)
    monkeypatch.setattr(CascStore, "_record", crash)
    with CascStore(tmp_path) as casc, pytest.raises(OSError, match="simulated crash"):
        casc.compact(live, threshold=0.25)
    monkeypatch.setattr(CascStore, "_record", original)
    with CascStore(tmp_path) as casc:
        assert casc.read(needed) == dict(objects)[needed]
