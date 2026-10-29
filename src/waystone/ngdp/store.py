"""Append-only, offline CASC storage and read-only v7 seed stores.

Index and span layouts follow CascLib's CascStructs.h/CascIndexFiles.cpp.
``publish`` syncs data before atomically publishing each bucket generation.
The private SQLite catalogue is not needed by CASC readers.  No ``shmem`` is
created: it is a runtime coordination mapping, not a portable content index.
Publication removes this managed store's old runtime map before new indexes.
This writer must run while the game and Agent are closed for this installation.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import json
import mmap
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import tempfile
import threading
import time
from typing import BinaryIO, Iterable, Iterator

from .. import system
from .models import Location

STATE_DIR = ".waystone"

SEGMENT_BITS = 30
SEGMENT_SIZE = 1 << SEGMENT_BITS
SPAN_HEADER_SIZE = 30
# Appends commit their catalogue rows in batches. A crash before a commit loses no
# object: recovery finds complete spans after the last durable checkpoint.
COMMIT_OBJECTS = 256
COMMIT_SECONDS = 1.0
_INDEX_NAME = re.compile(r"^([0-9a-fA-F]{2})([0-9a-fA-F]{8})\.idx$")
_DATA_NAME = re.compile(r"^data\.([0-9]{3,4})$")
_OFFSET_SALTS = (
    0x049396B8, 0x72A82A9B, 0xEE626CCA, 0x9917754F,
    0x15DE40B1, 0xF5A8A9B6, 0x421EAC7E, 0xA9D55C9A,
    0x317FD40C, 0x04FAF80D, 0x3D6BE971, 0x52933CFD,
    0x27F64B7D, 0xC6F5C11B, 0xD5757E3A, 0x6C388745,
)
_MASK = 0xffffffff


class StoreError(ValueError):
    """A CASC object, span, or index failed validation."""


def _rot(x: int, n: int) -> int:
    return ((x << n) | (x >> (32 - n))) & _MASK


def hashlittle2(data: bytes, high: int = 0, low: int = 0) -> tuple[int, int]:
    """Bob Jenkins lookup3, with the same seed/result order as CascLib."""
    a = b = c = (0xdeadbeef + len(data) + high) & _MASK
    c = (c + low) & _MASK
    pos = 0
    remaining = len(data)
    while remaining > 12:
        x, y, z = struct.unpack_from("<III", data, pos)
        a = (a + x) & _MASK
        b = (b + y) & _MASK
        c = (c + z) & _MASK
        a = ((a - c) ^ _rot(c, 4)) & _MASK; c = (c + b) & _MASK
        b = ((b - a) ^ _rot(a, 6)) & _MASK; a = (a + c) & _MASK
        c = ((c - b) ^ _rot(b, 8)) & _MASK; b = (b + a) & _MASK
        a = ((a - c) ^ _rot(c, 16)) & _MASK; c = (c + b) & _MASK
        b = ((b - a) ^ _rot(a, 19)) & _MASK; a = (a + c) & _MASK
        c = ((c - b) ^ _rot(b, 4)) & _MASK; b = (b + a) & _MASK
        pos += 12
        remaining -= 12
    if not remaining:
        return c, b
    tail = data[pos:]
    a = (a + int.from_bytes(tail[:4], "little")) & _MASK
    b = (b + int.from_bytes(tail[4:8], "little")) & _MASK
    c = (c + int.from_bytes(tail[8:12], "little")) & _MASK
    c = ((c ^ b) - _rot(b, 14)) & _MASK
    a = ((a ^ c) - _rot(c, 11)) & _MASK
    b = ((b ^ a) - _rot(a, 25)) & _MASK
    c = ((c ^ b) - _rot(b, 16)) & _MASK
    a = ((a ^ c) - _rot(c, 4)) & _MASK
    b = ((b ^ a) - _rot(a, 14)) & _MASK
    c = ((c ^ b) - _rot(b, 24)) & _MASK
    return c, b


def hashlittle(data: bytes, seed: int = 0) -> int:
    return hashlittle2(data, seed)[0]


def bucket_for_key(ekey: bytes) -> int:
    """XOR the first nine bytes, then XOR that byte's two nibbles."""
    if len(ekey) < 9:
        raise ValueError("an index key needs at least nine bytes")
    value = 0
    for byte in ekey[:9]:
        value ^= byte
    return (value & 15) ^ (value >> 4)


def _key(ekey: bytes) -> bytes:
    if not isinstance(ekey, bytes) or len(ekey) != 16:
        raise ValueError("EKey must be exactly 16 bytes")
    return ekey


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    result = stream.read(size)
    if len(result) != size:
        raise StoreError("truncated CASC object")
    return result


def _digest_range(stream: BinaryIO, size: int) -> bytes:
    digest = hashlib.md5()
    while size:
        block = _read_exact(stream, min(size, 1024 * 1024))
        digest.update(block)
        size -= len(block)
    return digest.digest()


def _verify_blte(stream: BinaryIO, size: int, expected: bytes | None = None,
                 *, chunks: bool = True) -> bytes:
    """Verify encoded identity and, normally, every encoded chunk checksum."""
    if size < 9:
        raise StoreError("BLTE object is too short")
    start = stream.tell()
    prefix = _read_exact(stream, 8)
    if prefix[:4] != b"BLTE":
        raise StoreError("missing BLTE signature")
    length = int.from_bytes(prefix[4:], "big")
    if length == 0:
        digest = hashlib.md5(prefix)
        remaining = size - 8
        while remaining:
            block = _read_exact(stream, min(remaining, 1024 * 1024))
            digest.update(block)
            remaining -= len(block)
        actual = digest.digest()
    else:
        if length < 36 or length > size or (length - 12) % 24:
            raise StoreError("invalid BLTE header length")
        table = _read_exact(stream, length - 8)
        if table[0] != 15 or int.from_bytes(table[1:4], "big") != (length - 12) // 24:
            raise StoreError("invalid BLTE chunk table")
        actual = hashlib.md5(prefix + table).digest()
        total = length
        for pos in range(4, len(table), 24):
            encoded_size = int.from_bytes(table[pos:pos + 4], "big")
            if not encoded_size:
                raise StoreError("empty BLTE encoded chunk")
            total += encoded_size
            if total > size:
                raise StoreError("BLTE chunk extends past object")
            if chunks:
                if _digest_range(stream, encoded_size) != table[pos + 8:pos + 24]:
                    raise StoreError("BLTE chunk checksum mismatch")
        if total != size:
            raise StoreError("BLTE length disagrees with chunk table")
    if expected is not None and actual != expected:
        raise StoreError("BLTE EKey mismatch")
    stream.seek(start)
    return actual


def _legacy_span_header(ekey: bytes, size: int, offset: int) -> bytes:
    """The exact earlier updater format, accepted only for explicit migration."""
    # EncodedSize includes the 30-byte encoded-header span, as do .idx sizes.
    header = ekey[::-1] + struct.pack("<IBB", size + SPAN_HEADER_SIZE, 0, 0)
    header += struct.pack("<I", hashlittle(header, 0x3D6BE971))
    folded = bytearray(4)
    for pos, byte in enumerate(header):
        folded[pos & 3] ^= byte
    encoded_offset = (offset + SPAN_HEADER_SIZE) & _MASK
    encoded_offset ^= _OFFSET_SALTS[encoded_offset & 15]
    encoded = struct.pack("<I", encoded_offset)
    # Checksum starts at offset 26.  The source uses its absolute byte index.
    header += bytes(folded[(26 + n) & 3] ^ encoded[(26 + n) & 3] for n in range(4))
    return header


def _span_header(ekey: bytes, size: int, offset: int, segment: int = 0) -> bytes:
    """Agent 9852 FUN_007210a0, including packed-address XOR alignment."""
    address = ((segment << SEGMENT_BITS) | offset) & _MASK
    header = ekey[::-1] + struct.pack("<IBB", size + SPAN_HEADER_SIZE, 0, 0)
    header += struct.pack("<I", hashlittle(header, 0x3D6BE971))
    folded = bytearray(4)
    for pos, byte in enumerate(header):
        folded[(address + pos) & 3] ^= byte
    encoded_offset = (address + SPAN_HEADER_SIZE) & _MASK
    encoded_offset ^= _OFFSET_SALTS[encoded_offset & 15]
    encoded = struct.pack("<I", encoded_offset)
    header += bytes(folded[(address + 26 + n) & 3] ^ encoded[(address + 26 + n) & 3]
                    for n in range(4))
    return header




def _index_files(data_dir: Path) -> dict[int, Path]:
    latest: dict[int, tuple[int, Path]] = {}
    for path in data_dir.iterdir():
        match = _INDEX_NAME.fullmatch(path.name)
        if match and int(match[1], 16) < 16:
            bucket, generation = int(match[1], 16), int(match[2], 16)
            if bucket not in latest or generation > latest[bucket][0]:
                latest[bucket] = generation, path
    return {bucket: item[1] for bucket, item in latest.items()}


def _index_entries(path: Path, bucket: int, *, strict_sorted: bool = False) -> Iterator[tuple[bytes, int, int, int]]:
    """Read current v7 sorted or guarded-page journals, checking their hashes."""
    with path.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        if len(data) < 40:
            raise StoreError(f"truncated index: {path}")
        header_size, header_hash = struct.unpack_from("<II", data)
        if header_size != 16 or hashlittle(data[8:24]) != header_hash:
            raise StoreError(f"index header checksum mismatch: {path}")
        version, actual_bucket, flags, size_bytes, offset_bytes, key_bytes, bits, maximum = struct.unpack_from("<H6BQ", data, 8)
        if (version, actual_bucket, flags, size_bytes, offset_bytes, key_bytes) != (7, bucket, 0, 4, 5, 9):
            raise StoreError(f"unsupported index layout: {path}")
        if bits != SEGMENT_BITS or maximum < SEGMENT_SIZE or maximum >= 1 << 40:
            raise StoreError(f"unsupported CASC segment layout: {path}")

        def unpack(entry: bytes) -> tuple[bytes, int, int, int]:
            prefix = entry[:9]
            # Official journals also contain bookkeeping/sentinel keys which
            # do not obey the content bucket rule. Full BLTE validation below
            # prevents these rows from being mistaken for reusable content.
            packed = int.from_bytes(entry[9:14], "big")
            total = int.from_bytes(entry[14:18], "little")
            segment, offset = packed >> bits, packed & (SEGMENT_SIZE - 1)
            if total < SPAN_HEADER_SIZE or offset + total > SEGMENT_SIZE:
                raise StoreError(f"invalid CASC span location: {path}")
            return prefix, segment, offset, total - SPAN_HEADER_SIZE

        length, expected = struct.unpack_from("<II", data, 32)
        if length % 18 == 0 and length <= len(data) - 40:
            high = low = 0
            for pos in range(40, 40 + length, 18):
                entry = data[pos:pos + 18]
                high, low = hashlittle2(entry, high, low)
            matched = expected == high
            if not matched:
                # CascLib also accepts the historical Blizzget variant.
                legacy = 0
                for pos in range(40, 40 + length, 18):
                    legacy = hashlittle(data[pos:pos + 18], legacy)
                matched = expected == legacy
            if matched:
                if strict_sorted:
                    # Agent 9852 FUN_0072ec91/FUN_0072ef07 require a 4 KiB
                    # aligned update section with at least sixty 512-byte
                    # pages, even when the sorted table has every object.
                    update_start = (40 + length + 0xfff) & ~0xfff
                    if len(data) - update_start < 0x7800:
                        raise StoreError(f"truncated KMT update section: {path}")
                    # Our published snapshots have no pending updates. A game
                    # or Agent update journal would override the sorted table
                    # and cannot be vouched for by this private catalogue.
                    if struct.unpack_from("<I", data, update_start)[0] != 0:
                        raise StoreError(f"unexpected active KMT updates in owned index: {path}")
                previous = b""
                for pos in range(40, 40 + length, 18):
                    entry = data[pos:pos + 18]
                    if entry[:9] < previous:
                        raise StoreError(f"unsorted CASC index: {path}")
                    previous = entry[:9]
                    yield unpack(entry)
                return
        if strict_sorted:
            raise StoreError(f"index entry checksum mismatch: {path}")
        # Unsorted journal pages have 4-byte guards and 20-byte aligned entries.
        if len(data) - 32 < 0x7800:
            raise StoreError(f"index entry checksum mismatch: {path}")
        for page in range(0x1000, len(data), 0x1000):
            for pos in range(page, min(page + 0x1000, len(data)) - 23, 24):
                guard = struct.unpack_from("<I", data, pos)[0]
                if not guard:
                    break
                if (hashlittle(data[pos + 4:pos + 23]) | 0x80000000) != guard:
                    raise StoreError(f"index journal checksum mismatch: {path}")
                yield unpack(data[pos + 4:pos + 22])


class CascStore:
    """Own encoded objects under ``root/Data/data`` with bounded crash recovery.

    ``put`` checks objects and commits the catalogue in batches, without a disk
    sync.  Call ``checkpoint`` every batch; ``publish`` and ``close`` do so
    automatically. Recovery checks only the tail after each durable checkpoint,
    and records the complete spans an unfinished batch left there.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.data_dir = self.root / "Data" / "data"
        self.state_dir = self.root / STATE_DIR
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._mutex = threading.RLock()
        self._appending: tuple[int, BinaryIO] | None = None    # the open segment that takes appends
        self._uncommitted, self._uncommitted_since = 0, 0.0
        self._lock_file = (self.state_dir / "store.lock").open("a+b")
        database = self.state_dir / "store.sqlite"
        self.inventory_path = database
        try:
            with self._locked():
                if not database.exists() and any(_DATA_NAME.fullmatch(p.name) for p in self.data_dir.iterdir()):
                    raise StoreError("data files exist without this updater's catalogue; use CascSource to seed a fresh root")
                self._db = sqlite3.connect(database, check_same_thread=False)
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute("PRAGMA synchronous=NORMAL")
                self._db.execute("PRAGMA cache_size=-65536")
                self._db.executescript("""
                    CREATE TABLE IF NOT EXISTS objects(
                        ekey BLOB PRIMARY KEY, prefix BLOB NOT NULL UNIQUE,
                        bucket INTEGER NOT NULL, segment INTEGER NOT NULL,
                        offset INTEGER NOT NULL, size INTEGER NOT NULL
                    ) WITHOUT ROWID;
                    CREATE INDEX IF NOT EXISTS object_bucket ON objects(bucket, prefix);
                    CREATE INDEX IF NOT EXISTS object_location ON objects(segment, offset);
                    CREATE TABLE IF NOT EXISTS segments(
                        segment INTEGER PRIMARY KEY, durable_end INTEGER NOT NULL,
                        tail_end INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS published(
                        id INTEGER PRIMARY KEY CHECK (id = 0), segment INTEGER NOT NULL,
                        tail_end INTEGER NOT NULL
                    );
                """)
                self._recover()
        except BaseException:
            if hasattr(self, "_db"):
                self._db.close()
            self._lock_file.close()
            raise
        self._closed = False

    @classmethod
    def upgrade_span_headers(cls, root: Path) -> int:
        """Upgrade exact legacy headers in owned catalogue rows, in disk order.

        Run with game, Agent and updater closed. Only four checksum bytes per
        legacy span change; payloads, locations, indexes and catalogue rows stay
        unchanged. Already canonical headers are accepted. Unknown headers,
        missing segments and external/symlinked paths fail closed. Uncatalogued
        files are never opened. Returns the number of upgraded headers.
        """
        root = Path(root).resolve()
        state_dir = root / STATE_DIR
        data_dir = root / "Data" / "data"
        database = state_dir / "store.sqlite"
        if not database.is_file() or not data_dir.is_dir():
            raise StoreError("header upgrade requires this updater's existing catalogue and data directory")
        paths = (state_dir, database, data_dir.parent, data_dir, state_dir / "store.lock")
        if any(path.is_symlink() or not path.resolve().is_relative_to(root) for path in paths):
            raise StoreError("header upgrade refuses symlinked or external store paths")
        with (state_dir / "store.lock").open("a+b") as lock:
            system.lock(lock)
            try:
                db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
                stream = None
                try:
                    if db.execute("SELECT 1 FROM objects o LEFT JOIN segments s ON o.segment=s.segment WHERE s.segment IS NULL LIMIT 1").fetchone():
                        raise StoreError("header upgrade found an object without segment ownership metadata")
                    cursor = db.execute("SELECT ekey,segment,offset,size FROM objects ORDER BY segment,offset")
                    current_segment = None
                    changed = 0
                    for key, segment, offset, size in cursor:
                        if not 0 <= segment <= 1023 or offset < 0 or size < 9 or offset + 30 + size > SEGMENT_SIZE:
                            raise StoreError("invalid owned span metadata during header upgrade")
                        if segment != current_segment:
                            if stream is not None:
                                stream.flush()
                                os.fsync(stream.fileno())
                                stream.close()
                                stream = None
                            path = data_dir / f"data.{segment:03d}"
                            if path.is_symlink() or not path.is_file():
                                raise StoreError(f"invalid owned segment during header upgrade: {path}")
                            stream = path.open("r+b")
                            current_segment = segment
                            file_size = os.fstat(stream.fileno()).st_size
                        if offset + SPAN_HEADER_SIZE + size > file_size:
                            raise StoreError("owned span exceeds segment during header upgrade")
                        stream.seek(offset)
                        actual = _read_exact(stream, SPAN_HEADER_SIZE)
                        canonical = _span_header(key, size, offset, segment)
                        if actual == canonical:
                            continue
                        if actual != _legacy_span_header(key, size, offset):
                            raise StoreError(f"unfamiliar or corrupt owned span header: data.{segment:03d} at {offset}")
                        stream.seek(offset + 26)
                        if stream.write(canonical[26:]) != 4:
                            raise OSError("short span checksum write")
                        changed += 1
                    return changed
                except sqlite3.DatabaseError as error:
                    raise StoreError("invalid updater catalogue during header upgrade") from error
                finally:
                    if stream is not None:
                        try:
                            stream.flush()
                            os.fsync(stream.fileno())
                        finally:
                            stream.close()
                    db.close()
            finally:
                system.unlock(lock)

    @classmethod
    def recover_damaged_segments(cls, root: Path) -> list[str]:
        """Explicitly reset missing/truncated segments in this updater's store.

        Call this before opening ``CascStore`` during an authorized repair.
        It requires an existing private catalogue and never discovers ownership
        from official indexes. Every object in a reset segment becomes missing
        and must be reacquired before publishing the repaired build. Normal
        construction continues to reject loss of previously durable data.
        """
        root = Path(root).resolve()
        state_dir = root / STATE_DIR
        data_dir = root / "Data" / "data"
        database = state_dir / "store.sqlite"
        if not database.is_file():
            raise StoreError("segment repair requires this updater's existing catalogue")
        if (data_dir.exists() and not data_dir.is_dir()) or (data_dir.parent.exists() and not data_dir.parent.is_dir()):
            raise StoreError("segment repair requires a data directory")
        paths = (state_dir, database, data_dir.parent, data_dir)
        if any(path.is_symlink() or not path.resolve().is_relative_to(root) for path in paths):
            raise StoreError("segment repair refuses symlinked or external store paths")
        lock_path = state_dir / "store.lock"
        if lock_path.is_symlink():
            raise StoreError("segment repair refuses a symlinked store lock")
        with lock_path.open("a+b") as lock:
            system.lock(lock)
            try:
                db = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)
                try:
                    db.execute("PRAGMA synchronous=FULL")
                    # Read the ownership metadata before touching any segment.
                    rows = list(db.execute("SELECT segment,durable_end,tail_end FROM segments"))
                    db.execute("SELECT ekey,prefix,bucket,segment,offset,size FROM objects LIMIT 0")
                    damaged = []
                    for segment, durable, tail in rows:
                        if not 0 <= segment <= 1023 or not 0 <= durable <= tail <= SEGMENT_SIZE:
                            raise StoreError("invalid segment ownership metadata")
                        path = data_dir / f"data.{segment:03d}"
                        if path.is_symlink():
                            raise StoreError("segment repair refuses a symlinked data file")
                        if not path.exists() or path.stat().st_size < durable:
                            damaged.append((segment, path))
                    if damaged:
                        data_dir.mkdir(parents=True, exist_ok=True)
                        system.sync_directory(data_dir.parent)
                        system.sync_directory(root)
                    for segment, path in damaged:
                        # Sync the reset file before making its empty inventory
                        # durable. A crash in between stays fail-closed and this
                        # explicit repair can safely be run again.
                        with path.open("w+b") as stream:
                            stream.flush()
                            os.fsync(stream.fileno())
                        system.sync_directory(data_dir)
                        db.execute("DELETE FROM objects WHERE segment=?", (segment,))
                        db.execute("UPDATE segments SET durable_end=0,tail_end=0 WHERE segment=?", (segment,))
                        db.commit()
                    db.execute("PRAGMA wal_checkpoint(FULL)")
                    system.sync_directory(state_dir)
                    return [path.name for _, path in damaged]
                except sqlite3.DatabaseError as error:
                    raise StoreError("invalid updater catalogue during segment repair") from error
                finally:
                    db.close()
            finally:
                system.unlock(lock)

    @contextmanager
    def _locked(self):
        with self._mutex:
            system.lock(self._lock_file)
            try:
                yield
            finally:
                system.unlock(self._lock_file)

    def _path(self, segment: int) -> Path:
        return self.data_dir / f"data.{segment:03d}"

    def _append_file(self, segment: int) -> BinaryIO:
        """The segment's file, kept open between appends. Opening it per object is slow on Windows."""
        if self._appending and self._appending[0] == segment:
            return self._appending[1]
        self._close_append()
        path = self._path(segment)
        target = path.open("r+b" if path.exists() else "w+b")
        self._appending = segment, target
        return target

    def _close_append(self) -> None:
        if self._appending:
            self._appending[1].close()
            self._appending = None

    def _commit(self) -> None:
        self._db.commit()
        self._uncommitted = 0

    def _record(self, ekey: bytes, segment: int, offset: int, size: int) -> None:
        existing = self._db.execute("SELECT ekey FROM objects WHERE prefix=?", (ekey[:9],)).fetchone()
        if existing and existing[0] != ekey:
            raise StoreError("two full EKeys share a nine-byte index prefix")
        self._db.execute("INSERT OR REPLACE INTO objects VALUES(?,?,?,?,?,?)",
                         (ekey, ekey[:9], bucket_for_key(ekey), segment, offset, size))

    def _recover(self) -> None:
        """Recover canonical updater spans without truncating unclaimed bytes.

        The game can append native cache/header spans to these same files.
        Unknown or incomplete tails remain physically intact and unclaimed.
        The append boundary advances to EOF, or seals an oversized segment.
        """
        known = {row[0]: row[1] for row in self._db.execute("SELECT segment,durable_end FROM segments")}
        for path in sorted(self.data_dir.iterdir()):
            match = _DATA_NAME.fullmatch(path.name)
            if not match:
                continue
            segment = int(match[1])
            if segment > 1023:
                continue
            if path.is_symlink() or not path.is_file():
                raise StoreError(f"invalid CASC data segment path: {path}")
            offset = known.get(segment, 0)
            if path.stat().st_size < offset:
                raise StoreError(f"durable CASC segment was truncated: {path}")
            with path.open("rb") as stream:
                end = os.fstat(stream.fileno()).st_size
                # Committed updater spans can follow an unclaimed native gap.
                # Check those locations independently before scanning for any
                # complete orphan appends whose SQLite transaction was lost.
                pending_offset = offset
                while True:
                    rows = self._db.execute(
                        "SELECT ekey,offset,size FROM objects WHERE segment=? AND offset>=? ORDER BY offset LIMIT 4096",
                        (segment, pending_offset),
                    ).fetchall()
                    if not rows:
                        break
                    for key, location, size in rows:
                        valid = location + SPAN_HEADER_SIZE + size <= min(end, SEGMENT_SIZE)
                        if valid:
                            stream.seek(location)
                            header = stream.read(SPAN_HEADER_SIZE)
                            valid = header in (_span_header(key, size, location, segment),
                                               _legacy_span_header(key, size, location))
                            if valid:
                                try:
                                    _verify_blte(stream, size, key)
                                except StoreError:
                                    valid = False
                        if not valid:
                            self._db.execute("DELETE FROM objects WHERE ekey=?", (key,))
                    pending_offset = rows[-1][1] + 1
                while offset + 39 <= end:
                    stream.seek(offset)
                    header = stream.read(SPAN_HEADER_SIZE)
                    total = int.from_bytes(header[16:20], "little")
                    key = header[:16][::-1]
                    if total < 39 or offset + total > end or offset + total > SEGMENT_SIZE:
                        break
                    if header not in (_span_header(key, total - SPAN_HEADER_SIZE, offset, segment),
                                      _legacy_span_header(key, total - SPAN_HEADER_SIZE, offset)):
                        break
                    try:
                        _verify_blte(stream, total - SPAN_HEADER_SIZE, key)
                    except StoreError:
                        break
                    self._record(key, segment, offset, total - SPAN_HEADER_SIZE)
                    offset += total
            # Preserve unrecognized bytes and never append over them. A file
            # beyond the v7 addressable range is sealed at the segment limit.
            append_end = min(end, SEGMENT_SIZE)
            self._db.execute("INSERT INTO segments VALUES(?,0,?) ON CONFLICT(segment) DO UPDATE SET tail_end=excluded.tail_end", (segment, append_end))
        for segment, durable in known.items():
            if not self._path(segment).exists():
                if durable:
                    raise StoreError("a durable CASC segment is missing")
                self._db.execute("DELETE FROM objects WHERE segment=?", (segment,))
                self._db.execute("DELETE FROM segments WHERE segment=?", (segment,))
        self._db.commit()

    def has(self, ekey: bytes) -> bool:
        with self._mutex:
            row = self._db.execute("SELECT segment,offset,size FROM objects WHERE ekey=?", (_key(ekey),)).fetchone()
        if row is None:
            return False
        try:
            return self._path(row[0]).stat().st_size >= row[1] + SPAN_HEADER_SIZE + row[2]
        except FileNotFoundError:
            return False

    def invalidate(self, ekey: bytes) -> None:
        """Forget an object so its verified replacement can be appended.

        Old segment bytes remain untouched. The durable checkpoint prevents
        tail recovery from rediscovering the invalidated old span after a crash.
        ``publish`` replaces the public index location after the next ``put``.
        """
        ekey = _key(ekey)
        with self._locked():
            if self._db.execute("SELECT 1 FROM objects WHERE ekey=?", (ekey,)).fetchone() is None:
                return
            self._checkpoint()
            self._db.execute("PRAGMA synchronous=FULL")
            try:
                self._db.execute("DELETE FROM objects WHERE ekey=?", (ekey,))
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise
            finally:
                self._db.execute("PRAGMA synchronous=NORMAL")

    def put(self, ekey: bytes, source: Path | bytes, *, relocate: bool = False) -> None:
        """Append a verified object. With relocate, a stored object gets a new copy, and its
        entry switches to it in the same commit, so a crash keeps one valid copy either way."""
        ekey = _key(ekey)
        with self._locked():
            if self.has(ekey) and not relocate:
                return
            stream = io.BytesIO(source) if isinstance(source, bytes) else Path(source).open("rb")
            with stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(0)
                if size + SPAN_HEADER_SIZE > SEGMENT_SIZE:
                    raise StoreError("BLTE object exceeds one CASC segment")
                _verify_blte(stream, size, ekey)
                row = self._db.execute("SELECT segment,tail_end FROM segments ORDER BY segment DESC LIMIT 1").fetchone()
                segment, offset = row if row else (0, 0)
                if offset + SPAN_HEADER_SIZE + size > SEGMENT_SIZE:
                    segment, offset = segment + 1, 0
                if segment > 1023:
                    raise StoreError("CASC segment namespace is exhausted")
                target = self._append_file(segment)
                target.seek(0, os.SEEK_END)
                if target.tell() != offset:
                    raise StoreError("CASC append position disagrees with catalogue")
                if not self._db.in_transaction:
                    self._db.execute("BEGIN")
                    self._uncommitted_since = time.monotonic()
                # One object's rows roll back alone. The batch around it stays.
                self._db.execute("SAVEPOINT put")
                try:
                    target.write(_span_header(ekey, size, offset, segment))
                    shutil.copyfileobj(stream, target, 1024 * 1024)
                    target.flush()
                    if target.tell() != offset + SPAN_HEADER_SIZE + size:
                        raise StoreError("source changed while copying")
                    self._record(ekey, segment, offset, size)
                    self._db.execute("INSERT INTO segments VALUES(?,0,?) ON CONFLICT(segment) DO UPDATE SET tail_end=excluded.tail_end",
                                     (segment, target.tell()))
                    self._db.execute("RELEASE put")
                except BaseException:
                    try:
                        self._db.execute("ROLLBACK TO put")
                        self._db.execute("RELEASE put")
                    except sqlite3.Error:
                        # SQLite already rolled back the batch, or the savepoint was released
                        # before a signal arrived. Startup recovery finds the batch's spans.
                        pass
                    # A signal can arrive after the rows were recorded. Keep that span, as
                    # the batch commits it. Otherwise its bytes are unclaimed and go.
                    persisted = self._db.execute(
                        "SELECT segment,offset,size FROM objects WHERE ekey=?", (ekey,)
                    ).fetchone()
                    if persisted != (segment, offset, size):
                        target.truncate(offset)
                    self._close_append()
                    raise
            self._uncommitted += 1
            if (self._uncommitted >= COMMIT_OBJECTS
                    or time.monotonic() - self._uncommitted_since >= COMMIT_SECONDS):
                self._commit()

    def _location(self, ekey: bytes) -> tuple[int, int, int]:
        with self._mutex:
            row = self._db.execute("SELECT segment,offset,size FROM objects WHERE ekey=?", (_key(ekey),)).fetchone()
        if row is None:
            raise KeyError(ekey.hex())
        return row

    @contextmanager
    def _verified_stream(self, ekey: bytes):
        segment, offset, size = self._location(ekey)
        with self._path(segment).open("rb") as stream:
            stream.seek(offset)
            actual = _read_exact(stream, SPAN_HEADER_SIZE)
            if actual != _span_header(ekey, size, offset, segment):
                if actual == _legacy_span_header(ekey, size, offset):
                    raise StoreError("legacy span header requires CascStore.upgrade_span_headers")
                raise StoreError("owned span header checksum or metadata mismatch")
            _verify_blte(stream, size, ekey)
            yield stream, size

    def read(self, ekey: bytes) -> bytes:
        with self._verified_stream(ekey) as (stream, size):
            return _read_exact(stream, size)

    get = read

    def verify(self, ekey: bytes) -> bool:
        """Verify identity and all chunk hashes with bounded memory."""
        with self._verified_stream(ekey):
            pass
        return True

    def extract_to(self, ekey: bytes, destination: Path) -> None:
        with self._verified_stream(ekey) as (stream, size):
            _copy_range(stream, size, Path(destination))

    @property
    def count(self) -> int:
        with self._mutex:
            return self._db.execute("SELECT count(*) FROM objects").fetchone()[0]

    def iter_entries(self) -> Iterator[tuple[bytes, Location]]:
        with self._mutex:
            cursor = self._db.execute("SELECT ekey,segment,offset,size FROM objects ORDER BY ekey")
            while rows := cursor.fetchmany(4096):
                for ekey, segment, offset, size in rows:
                    yield ekey, Location(f"data.{segment:03d}", offset, size)

    __iter__ = iter_entries

    def _checkpoint(self) -> None:
        self._commit()
        self._close_append()
        for segment, tail_end in self._db.execute("SELECT segment,tail_end FROM segments WHERE tail_end>durable_end"):
            with self._path(segment).open("rb") as stream:
                if os.fstat(stream.fileno()).st_size < tail_end:
                    raise StoreError("CASC segment is shorter than catalogue before checkpoint")
                os.fsync(stream.fileno())
        system.sync_directory(self.data_dir)
        system.sync_directory(self.data_dir.parent)
        system.sync_directory(self.root)
        self._db.execute("PRAGMA synchronous=FULL")
        try:
            self._db.execute("UPDATE segments SET durable_end=tail_end")
            self._db.commit()
            self._db.execute("PRAGMA wal_checkpoint(FULL)")
            system.sync_directory(self.state_dir)
        finally:
            self._db.execute("PRAGMA synchronous=NORMAL")

    def checkpoint(self) -> None:
        with self._locked():
            self._checkpoint()

    def verify_indexes(self) -> bool:
        """Check all latest published buckets against the owned catalogue.

        This reads index headers, guards and every exact owned mapping. Native
        bookkeeping and streamed fragments with unowned prefixes are accepted
        without asserting their payload integrity. An owned prefix must occur
        exactly once with its catalogue location and size. It does not change
        files or catalogue rows. SQLite cursors and one mapped index bound
        memory use. Appends after the last ``publish``, such as an unfinished
        update's, may be missing or still published at an older location.
        """
        with self._locked():
            frontier = self._db.execute("SELECT segment,tail_end FROM published").fetchone()

            def unpublished(row) -> bool:
                return frontier is not None and (row[1], row[2]) >= frontier

            def skip(wanted, prefix):
                while wanted is not None and wanted[0] < prefix and unpublished(wanted):
                    wanted = next(expected, None)
                return wanted

            try:
                indexes = _index_files(self.data_dir)
                missing = sorted(set(range(16)) - indexes.keys())
                if missing:
                    raise StoreError("missing published CASC index buckets: " + ", ".join(f"{bucket:02x}" for bucket in missing))
                for bucket, path in sorted(indexes.items()):
                    if path.is_symlink() or not path.is_file():
                        raise StoreError(f"invalid owned CASC index path: {path}")
                    expected = self._db.execute("SELECT prefix,segment,offset,size FROM objects WHERE bucket=? ORDER BY prefix", (bucket,))
                    actual = _index_entries(path, bucket, strict_sorted=True)
                    try:
                        wanted = next(expected, None)
                        for found in actual:
                            prefix, segment, offset, size = found
                            wanted = skip(wanted, prefix)
                            if wanted is not None and prefix == wanted[0]:
                                if size == 0:
                                    raise StoreError(f"zero-size native span collides with owned prefix {prefix.hex()}")
                                # An unpublished replacement can still be published at its old location.
                                if wanted != found and not unpublished(wanted):
                                    raise StoreError(f"published CASC index differs from catalogue in bucket {bucket:02x}: expected {wanted!r}, found {found!r}")
                                wanted = next(expected, None)
                            elif wanted is not None and prefix > wanted[0]:
                                raise StoreError(f"published CASC index differs from catalogue in bucket {bucket:02x}: expected {wanted!r}, found {found!r}")
                            else:
                                # This can be an allocation sentinel, an extra
                                # streamed object, or one of the game's several
                                # cached fragments for a non-owned key. A prefix
                                # already owned in any bucket must not be hidden
                                # among those extras or appear a second time.
                                owned = self._db.execute("SELECT 1 FROM objects WHERE prefix=?", (prefix,)).fetchone()
                                if owned:
                                    if size == 0:
                                        raise StoreError(f"zero-size native span collides with owned prefix {prefix.hex()}")
                                    raise StoreError(f"duplicate or ambiguous owned prefix {prefix.hex()} in published CASC indexes")
                        wanted = skip(wanted, b"\xff" * 10)
                        if wanted is not None:
                            raise StoreError(f"published CASC index differs from catalogue in bucket {bucket:02x}: missing {wanted!r}")
                    finally:
                        expected.close()
                        actual.close()
            except StoreError:
                raise
            except (OSError, ValueError, struct.error, sqlite3.DatabaseError) as error:
                raise StoreError(f"cannot verify published CASC indexes: {error}") from error
        return True

    def _invalidate_shmem(self) -> None:
        """Discard this managed store's runtime map before new generations."""
        if (self.data_dir.is_symlink() or self.data_dir.parent.is_symlink()
                or not self.data_dir.resolve().is_relative_to(self.root)):
            raise StoreError("shmem invalidation refuses a symlinked or external data directory")
        shmem = self.data_dir / "shmem"
        if shmem.is_symlink():
            raise StoreError("shmem invalidation refuses a symlinked runtime map")
        if shmem.exists():
            if not shmem.is_file():
                raise StoreError("shmem runtime map is not a file")
            shmem.unlink()
            system.sync_directory(self.data_dir)

    def publish(self) -> list[Path]:
        """Sync content, invalidate runtime state, then publish v7 generations."""
        with self._locked():
            self._checkpoint()
            self._invalidate_shmem()
            frontier = self._db.execute("SELECT segment,tail_end FROM segments ORDER BY segment DESC LIMIT 1").fetchone()
            previous = _index_files(self.data_dir)
            published = []
            for bucket in range(16):
                generation = int(previous[bucket].stem[2:], 16) + 1 if bucket in previous else 1
                if generation > _MASK:
                    raise StoreError("CASC index generation is exhausted")
                final = self.data_dir / f"{bucket:02x}{generation:08x}.idx"
                header = struct.pack("<H6BQ", 7, bucket, 0, 4, 5, 9, SEGMENT_BITS,
                                     (1 << 40) - SEGMENT_SIZE)
                descriptor, name = tempfile.mkstemp(prefix=f"{bucket:02x}-", suffix=".tmp", dir=self.data_dir)
                temporary = Path(name)
                try:
                    with os.fdopen(descriptor, "w+b") as stream:
                        stream.write(struct.pack("<II", 16, hashlittle(header)))
                        stream.write(header + b"\0" * 8 + b"\0" * 8)
                        high = low = length = 0
                        for prefix, segment, offset, size in self._db.execute("SELECT prefix,segment,offset,size FROM objects WHERE bucket=? ORDER BY prefix", (bucket,)):
                            entry = prefix + ((segment << SEGMENT_BITS) | offset).to_bytes(5, "big") + struct.pack("<I", size + SPAN_HEADER_SIZE)
                            stream.write(entry)
                            high, low = hashlittle2(entry, high, low)
                            length += 18
                        # Exact Agent 9852 FUN_0072d88d allocation. Rounding
                        # just the sorted end to 64 KiB can leave too little
                        # space for the mandatory (empty) KMT update journal.
                        end = stream.tell()
                        padded = (end + 0x17fff) & ~0xffff
                        stream.write(b"\0" * (padded - end))
                        stream.seek(32)
                        stream.write(struct.pack("<II", length, high))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, final)
                    system.sync_directory(self.data_dir)
                finally:
                    temporary.unlink(missing_ok=True)
                published.append(final)
            # Retain one previous generation for read-only readers during swap.
            for path in self.data_dir.iterdir():
                match = _INDEX_NAME.fullmatch(path.name)
                if match and int(match[1], 16) < 16:
                    bucket = int(match[1], 16)
                    keep = {published[bucket], previous.get(bucket)}
                    if path not in keep:
                        path.unlink()
            system.sync_directory(self.data_dir)
            # Appends from here on are not in these indexes yet. A crash before this commit
            # keeps the older end, which only makes verify_indexes stricter.
            self._db.execute("INSERT OR REPLACE INTO published VALUES(0,?,?)", frontier or (0, 0))
            self._commit()
            return published

    export_index = publish

    def compact(self, live_database: Path, *, threshold: float = 0.25, progress=None, cancelled=None) -> dict:
        """Forget objects no client needs and rewrite mostly dead segments.

        ``live_database`` holds a ``keys(ekey)`` table of every needed object.
        Objects outside it leave the catalogue. A segment whose dead share
        exceeds ``threshold`` has its live objects appended again, then the
        segment file is deleted. The newest segment and segments with bytes
        this store did not write, such as game-written data, are never
        rewritten. Each move commits the new location in one step, and the
        public index is published before a segment file goes, so a crash never
        loses an object another client needs.
        """
        progress = progress or (lambda event: None)
        cancelled = cancelled or (lambda: False)
        with self._locked():
            self._checkpoint()
            self._db.execute("ATTACH DATABASE ? AS live", (str(live_database),))
            try:
                dead = self._db.execute(
                    "SELECT count(*),coalesce(sum(size),0) FROM objects o "
                    "WHERE NOT EXISTS (SELECT 1 FROM live.keys k WHERE k.ekey=o.ekey)").fetchone()
                self._db.execute("PRAGMA synchronous=FULL")
                try:
                    self._db.execute("DELETE FROM objects WHERE NOT EXISTS "
                                     "(SELECT 1 FROM live.keys k WHERE k.ekey=objects.ekey)")
                    self._db.commit()
                finally:
                    self._db.execute("PRAGMA synchronous=NORMAL")
                segments = self._db.execute(
                    "SELECT s.segment,s.tail_end,coalesce(sum(o.size+?),0) FROM segments s "
                    "LEFT JOIN objects o ON o.segment=s.segment GROUP BY s.segment ORDER BY s.segment",
                    (SPAN_HEADER_SIZE,)).fetchall()
            finally:
                self._db.execute("DETACH DATABASE live")
        newest = segments[-1][0] if segments else None
        victims = []
        for segment, tail_end, used in segments:
            path = self._path(segment)
            if segment == newest or not path.is_file() or path.stat().st_size != tail_end or tail_end == 0:
                continue
            if 1 - used / tail_end > threshold:
                victims.append(segment)
        moved = reclaimed = deleted = 0
        for segment in victims:
            with self._mutex:
                keys = [row[0] for row in self._db.execute(
                    "SELECT ekey FROM objects WHERE segment=? ORDER BY offset", (segment,))]
            for ekey in keys:
                if cancelled():
                    break
                payload = self.read(ekey)
                self.put(ekey, payload, relocate=True)
                moved += 1
                progress({"stage": "compacting", "segment": segment, "moved": moved})
            if cancelled():
                break
            with self._mutex:
                if self._db.execute("SELECT 1 FROM objects WHERE segment=? LIMIT 1", (segment,)).fetchone():
                    continue
            # The game reads the public index, which must name the new copies before the old file goes.
            # Only the newest segment takes appends, so this one stays empty meanwhile.
            self.publish()
            with self._locked():
                self._checkpoint()
                self._db.execute("DELETE FROM segments WHERE segment=?", (segment,))
                self._db.commit()
                reclaimed += self._path(segment).stat().st_size
                self._path(segment).unlink()
                system.sync_directory(self.data_dir)
                deleted += 1
        self.publish()
        return {"forgotten_objects": dead[0], "forgotten_bytes": dead[1], "moved_objects": moved,
                "deleted_segments": deleted, "reclaimed_bytes": reclaimed}

    def import_existing(self, source_root: Path) -> "CascSource":
        identity = hashlib.sha256(os.fsencode(Path(source_root).resolve())).hexdigest()[:20]
        return CascSource(source_root, self.state_dir / f"source-{identity}.sqlite")

    def close(self) -> None:
        if not self._closed:
            self.checkpoint()
            self._db.close()
            self._lock_file.close()
            self._closed = True

    def __enter__(self) -> "CascStore":
        return self

    def __exit__(self, *_args) -> None:
        self.close()


def _copy_range(stream: BinaryIO, size: int, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as target:
        while size:
            block = _read_exact(stream, min(size, 1024 * 1024))
            target.write(block)
            size -= len(block)


class CascSource:
    """Read-only official-store lookup, with a cache outside the source tree.

    Index prefixes only select candidates. Each lookup derives the full EKey
    from BLTE bytes, so corrupt/zeroed span headers and prefix collisions cannot
    establish presence. Extraction also checks all encoded chunk checksums.
    """

    def __init__(self, root: Path, cache_path: Path | None = None):
        self.root = Path(root).resolve()
        candidates = (self.root / "Data" / "data", self.root / "data", self.root)
        self.data_dir = next((p for p in candidates if p.is_dir() and _index_files(p)), None)
        if self.data_dir is None:
            raise StoreError(f"no CASC v7 indexes under {root}")
        self._temporary = tempfile.TemporaryDirectory(prefix="waystone-source-") if cache_path is None else None
        cache_path = Path(self._temporary.name) / "index.sqlite" if self._temporary else Path(cache_path)
        if cache_path.resolve().is_relative_to(self.root):
            raise ValueError("source cache must be outside the read-only source root")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.inventory_path = cache_path
        self._mutex = threading.RLock()
        self._reading: tuple[int, BinaryIO] | None = None      # bulk reads come in physical order
        self._db = sqlite3.connect(cache_path, check_same_thread=False)
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS entries(prefix BLOB PRIMARY KEY,segment INTEGER,offset INTEGER,size INTEGER) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS meta(name TEXT PRIMARY KEY,value TEXT);
        """)
        indexes = _index_files(self.data_dir)
        fingerprint = json.dumps([str(self.data_dir), [(b, p.name, p.stat().st_size, p.stat().st_mtime_ns) for b, p in sorted(indexes.items())]])
        old = self._db.execute("SELECT value FROM meta WHERE name='fingerprint'").fetchone()
        if not old or old[0] != fingerprint:
            try:
                self._db.execute("DELETE FROM entries")
                for bucket, path in sorted(indexes.items()):
                    self._db.executemany("INSERT OR REPLACE INTO entries VALUES(?,?,?,?)", _index_entries(path, bucket))
                self._db.execute("INSERT OR REPLACE INTO meta VALUES('fingerprint',?)", (fingerprint,))
                self._db.commit()
            except BaseException:
                self._db.rollback()
                self._db.close()
                raise

    def _location(self, ekey: bytes, *, chunks: bool = False) -> tuple[Path, int, int]:
        with self._mutex:
            row = self._db.execute("SELECT segment,offset,size FROM entries WHERE prefix=?", (_key(ekey)[:9],)).fetchone()
        if row is None:
            raise KeyError(ekey.hex())
        segment, offset, size = row
        path = self.data_dir / f"data.{segment:03d}"
        with path.open("rb") as stream:
            if offset + size + SPAN_HEADER_SIZE > os.fstat(stream.fileno()).st_size:
                raise StoreError("source span exceeds data segment")
            stream.seek(offset + SPAN_HEADER_SIZE)
            _verify_blte(stream, size, ekey, chunks=chunks)
        return path, offset + SPAN_HEADER_SIZE, size

    def locate(self, ekey: bytes) -> Location:
        """Return an unverified index candidate without reading a data file.

        The offset addresses the 30-byte span wrapper. ``read``/``extract_to``
        derive and check the full key before returning any object bytes.
        """
        with self._mutex:
            row = self._db.execute("SELECT segment,offset,size FROM entries WHERE prefix=?", (_key(ekey)[:9],)).fetchone()
        if row is None:
            raise KeyError(ekey.hex())
        segment, offset, size = row
        return Location(f"data.{segment:03d}", offset, size)

    def iter_import_candidates(self, ekeys: Iterable[bytes]) -> Iterator[tuple[bytes, Location]]:
        """Join requested full keys to source prefixes, in physical read order.

        The join and sort stay in SQLite rather than retaining millions of
        Python objects. Candidates require full BLTE validation when read.
        """
        with self._mutex:
            self._db.execute("CREATE TEMP TABLE IF NOT EXISTS requested(ekey BLOB PRIMARY KEY,prefix BLOB) WITHOUT ROWID")
            self._db.execute("DELETE FROM requested")
            self._db.executemany("INSERT OR IGNORE INTO requested VALUES(?,?)", ((_key(key), key[:9]) for key in ekeys))
            cursor = self._db.execute("SELECT r.ekey,e.segment,e.offset,e.size FROM requested r JOIN entries e ON e.prefix=r.prefix ORDER BY e.segment,e.offset")
            try:
                while rows := cursor.fetchmany(4096):
                    for key, segment, offset, size in rows:
                        yield key, Location(f"data.{segment:03d}", offset, size)
            finally:
                cursor.close()
                self._db.execute("DELETE FROM requested")
                self._db.commit()

    def has(self, ekey: bytes) -> bool:
        try:
            self._location(ekey)
            return True
        except (KeyError, OSError, StoreError):
            return False

    def read(self, ekey: bytes) -> bytes:
        """Read a candidate once and check its key and chunks in memory."""
        with self._mutex:
            row = self._db.execute("SELECT segment,offset,size FROM entries WHERE prefix=?", (_key(ekey)[:9],)).fetchone()
            if row is None:
                raise KeyError(ekey.hex())
            segment, offset, size = row
            if not (self._reading and self._reading[0] == segment):
                self._close_reading()
                self._reading = segment, (self.data_dir / f"data.{segment:03d}").open("rb")
            stream = self._reading[1]
            if offset + size + SPAN_HEADER_SIZE > os.fstat(stream.fileno()).st_size:
                raise StoreError("source span exceeds data segment")
            stream.seek(offset + SPAN_HEADER_SIZE)
            data = _read_exact(stream, size)
        _verify_blte(io.BytesIO(data), size, ekey)
        return data

    def _close_reading(self) -> None:
        if self._reading:
            self._reading[1].close()
            self._reading = None

    get = read

    def verify(self, ekey: bytes) -> bool:
        self._location(ekey, chunks=True)
        return True

    def extract_to(self, ekey: bytes, destination: Path) -> None:
        destination = Path(destination)
        if destination.resolve().is_relative_to(self.root):
            raise ValueError("cannot extract into the read-only source root")
        path, offset, size = self._location(ekey, chunks=True)
        with path.open("rb") as stream:
            stream.seek(offset)
            _copy_range(stream, size, destination)

    @property
    def count(self) -> int:
        return self._db.execute("SELECT count(*) FROM entries").fetchone()[0]

    def close(self) -> None:
        with self._mutex:
            self._close_reading()
        self._db.close()
        if self._temporary:
            self._temporary.cleanup()

    def __enter__(self) -> "CascSource":
        return self

    def __exit__(self, *_args) -> None:
        self.close()
