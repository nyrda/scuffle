"""Bounded readers for Blizzard's public TACT manifests and BLTE objects.

Key sizes, byte order, page layouts, and signed DL priorities follow the
retained Forever captures and CascLib / TACT.Net's primary implementations.
No installed game files or platform specific libraries are needed.
"""
from __future__ import annotations

from bisect import bisect_right
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
import mmap
import os
from pathlib import Path
import re
import tempfile
from typing import BinaryIO, Iterator
import zlib

from .models import DownloadEntry, EncodingInfo, InstallEntry

_BLOCK = 64 * 1024
_MAX_TEXT = 8 * 1024 * 1024
_MAX_BLTE_HEADER = 64 * 1024 * 1024
_MAX_DECODED = 1 << 40
Source = str | os.PathLike[str] | bytes


class FormatError(ValueError):
    """A manifest or encoded object is malformed or fails an integrity check."""


class UnsupportedEncodingError(FormatError):
    """Content requires an encoding or decryption method we cannot apply."""


def _text(data: bytes) -> str:
    if len(data) > _MAX_TEXT:
        raise FormatError("Text metadata exceeds the 8 MiB limit")
    try:
        result = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FormatError("Metadata is not UTF-8") from exc
    if "\0" in result:
        raise FormatError("Text metadata contains a NUL")
    return result


def read_config(data: bytes) -> dict[str, list[str]]:
    """Read a plaintext build/CDN configuration, retaining value order."""
    result: dict[str, list[str]] = {}
    for line in _text(data).splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            raise FormatError("Configuration line is missing '='")
        key, value = (part.strip() for part in line.split("=", 1))
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key) or key in result:
            raise FormatError(f"Invalid or duplicate configuration key: {key!r}")
        result[key] = value.split()
    return result


def parse_psv(data: bytes) -> list[dict[str, str]]:
    """Read version-service tables and local .build.info tables."""
    lines = [line.strip() for line in _text(data).splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        return []
    names = [field.split("!", 1)[0].strip() for field in lines[0].split("|")]
    if any(not name for name in names) or len(set(names)) != len(names):
        raise FormatError("Invalid or duplicate PSV column")
    rows = []
    for line in lines[1:]:
        values = [field.strip() for field in line.split("|")]
        if len(values) != len(names):
            raise FormatError("PSV row has the wrong number of columns")
        rows.append(dict(zip(names, values)))
    return rows


@contextmanager
def _open(source: Source) -> Iterator[BinaryIO]:
    if isinstance(source, bytes):
        with io.BytesIO(source) as stream:
            yield stream
    else:
        with open(source, "rb") as stream:
            yield stream


def _exact(stream: BinaryIO, length: int) -> bytes:
    data = stream.read(length)
    if len(data) != length:
        raise FormatError("Truncated object")
    return data


def _key(key: bytes | None) -> None:
    if key is not None and (not isinstance(key, bytes) or len(key) != 16):
        raise ValueError("Expected a full 16-byte MD5 key")


@dataclass(frozen=True)
class BLTEChunk:
    encoded_size: int
    decoded_size: int | None
    checksum: bytes | None


@dataclass(frozen=True)
class BLTEHeader:
    header_size: int
    encoded_size: int
    decoded_size: int | None
    chunk_count: int
    _table: bytes

    @property
    def chunks(self) -> Iterator[BLTEChunk]:
        """Iterate without allocating an object for every table row at once."""
        if self.header_size == 0:
            yield BLTEChunk(self.encoded_size - 8, None, None)
        else:
            for p in range(0, len(self._table), 24):
                yield BLTEChunk(int.from_bytes(self._table[p:p + 4], "big"),
                                int.from_bytes(self._table[p + 4:p + 8], "big"),
                                self._table[p + 8:p + 24])


def _header(stream: BinaryIO) -> tuple[BLTEHeader, bytes]:
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(0)
    prefix = _exact(stream, 8)
    if prefix[:4] != b"BLTE":
        raise FormatError("Object is not BLTE")
    length = int.from_bytes(prefix[4:8], "big")
    if length == 0:
        if size < 9:
            raise FormatError("BLTE object has no mode byte")
        return BLTEHeader(0, size, None, 1, b""), prefix
    if length < 12 or length > size or length > _MAX_BLTE_HEADER:
        raise FormatError("Invalid BLTE header size")
    rest = _exact(stream, length - 8)
    count = int.from_bytes(rest[1:4], "big")
    if rest[0] != 15 or count == 0 or length != 12 + count * 24:
        raise FormatError("Invalid BLTE chunk table")
    table = rest[4:]
    encoded = length
    decoded = 0
    for p in range(0, len(table), 24):
        chunk_size = int.from_bytes(table[p:p + 4], "big")
        if chunk_size < 1:
            raise FormatError("BLTE chunk has no mode byte")
        encoded += chunk_size
        decoded += int.from_bytes(table[p + 4:p + 8], "big")
        if encoded > size or decoded > _MAX_DECODED:
            raise FormatError("BLTE chunk lengths exceed object limits")
    if encoded != size:
        raise FormatError("BLTE encoded length does not match the chunk table")
    return BLTEHeader(length, size, decoded, count, table), prefix + rest


def blte_header(source: Source) -> BLTEHeader:
    """Validate the header and its complete encoded extent, without decoding."""
    with _open(source) as stream:
        return _header(stream)[0]


def _encoded_key(stream: BinaryIO, header: BLTEHeader, raw: bytes) -> bytes:
    digest = hashlib.md5(raw)
    if header.header_size == 0:
        while block := stream.read(_BLOCK):
            digest.update(block)
    return digest.digest()


def get_blte_encoded_key(source: Source) -> bytes:
    """Hash the header for tabled objects, or the entire single-frame object."""
    with _open(source) as stream:
        header, raw = _header(stream)
        return _encoded_key(stream, header, raw)


blte_encoded_key = get_blte_encoded_key


def verify_blte(source: Source, expected_ekey: bytes | None = None) -> None:
    """Check encoded lengths, EKey, and every chunk MD5 without decompression.

    An authenticated encoded object can be kept in the store even when its
    payload uses encryption. decode_blte explicitly rejects encrypted chunks.
    """
    _key(expected_ekey)
    with _open(source) as stream:
        header, raw = _header(stream)
        if header.header_size == 0:
            key = _encoded_key(stream, header, raw)
        else:
            key = hashlib.md5(raw).digest()
            if expected_ekey is not None and key != expected_ekey:
                raise FormatError("BLTE EKey mismatch")
            for chunk in header.chunks:
                digest = hashlib.md5()
                remaining = chunk.encoded_size
                while remaining:
                    block = _exact(stream, min(_BLOCK, remaining))
                    digest.update(block)
                    remaining -= len(block)
                if digest.digest() != chunk.checksum:
                    raise FormatError("BLTE chunk MD5 mismatch")
        if expected_ekey is not None and key != expected_ekey:
            raise FormatError("BLTE EKey mismatch")


def validate_blte(data: Source, ekey: bytes | None = None) -> None:
    """Compatibility spelling for encoded-only verification."""
    verify_blte(data, ekey)


def decode_blte(source: Source, destination: str | os.PathLike[str],
                expected_ekey: bytes | None = None,
                expected_ckey: bytes | None = None,
                expected_size: int | None = None) -> None:
    """Decode N/Z chunks with bounded buffers and publish only verified output.

    Both hashes are 16-byte MD5 values. The destination is atomically replaced
    after all checks pass. On error, an existing destination is preserved.
    """
    _key(expected_ekey)
    _key(expected_ckey)
    if expected_size is not None and not 0 <= expected_size <= _MAX_DECODED:
        raise ValueError("Invalid expected decoded size")
    destination = Path(destination)
    with _open(source) as stream:
        header, raw = _header(stream)
        limit = expected_size if expected_size is not None else header.decoded_size
        if limit is None:
            limit = _MAX_DECODED
        if expected_size is not None and header.decoded_size not in (None, expected_size):
            raise FormatError("BLTE decoded size does not match the expected size")
        if header.header_size and expected_ekey is not None:
            if hashlib.md5(raw).digest() != expected_ekey:
                raise FormatError("BLTE EKey mismatch")
        encoded_hash = hashlib.md5(raw)
        decoded_hash = hashlib.md5()
        written = 0
        fd, temporary = tempfile.mkstemp(prefix=".blte-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                for chunk in header.chunks:
                    mode = _exact(stream, 1)
                    chunk_hash = hashlib.md5(mode)
                    if header.header_size == 0:
                        encoded_hash.update(mode)
                    remaining = chunk.encoded_size - 1
                    produced = 0
                    chunk_limit = chunk.decoded_size
                    if chunk_limit is None:
                        chunk_limit = limit - written
                    if mode == b"E":
                        raise UnsupportedEncodingError("Encrypted BLTE content requires a decryption key")
                    if mode not in (b"N", b"Z"):
                        raise UnsupportedEncodingError(f"Unsupported BLTE mode {mode!r}")
                    inflater = zlib.decompressobj() if mode == b"Z" else None

                    def emit(block: bytes) -> None:
                        nonlocal written, produced
                        produced += len(block)
                        written += len(block)
                        if produced > chunk_limit or written > limit:
                            raise FormatError("BLTE decoded output exceeds its declared size")
                        output.write(block)
                        decoded_hash.update(block)

                    while remaining:
                        block = _exact(stream, min(_BLOCK, remaining))
                        remaining -= len(block)
                        chunk_hash.update(block)
                        if header.header_size == 0:
                            encoded_hash.update(block)
                        if inflater is None:
                            emit(block)
                        else:
                            try:
                                while block:
                                    decoded = inflater.decompress(block, min(_BLOCK, chunk_limit - produced + 1))
                                    emit(decoded)
                                    block = inflater.unconsumed_tail
                                    if inflater.unused_data:
                                        raise FormatError("Trailing data after BLTE zlib frame")
                            except zlib.error as exc:
                                raise FormatError("Invalid BLTE zlib stream") from exc
                    if inflater is not None and not inflater.eof:
                        raise FormatError("Truncated BLTE zlib frame")
                    if chunk.decoded_size is not None and produced != chunk.decoded_size:
                        raise FormatError("BLTE chunk decoded size mismatch")
                    if chunk.checksum is not None and chunk_hash.digest() != chunk.checksum:
                        raise FormatError("BLTE chunk MD5 mismatch")
                if expected_size is not None and written != expected_size:
                    raise FormatError("BLTE decoded size mismatch")
                if expected_ckey is not None and decoded_hash.digest() != expected_ckey:
                    raise FormatError("BLTE CKey mismatch")
                if not header.header_size and expected_ekey is not None and encoded_hash.digest() != expected_ekey:
                    raise FormatError("BLTE EKey mismatch")
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


@contextmanager
def _mapped(path: str | os.PathLike[str]) -> Iterator[mmap.mmap]:
    with open(path, "rb") as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            raise FormatError("Empty manifest")
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            yield data


def _require(data: mmap.mmap, start: int, length: int) -> None:
    if start < 0 or length < 0 or start > len(data) or length > len(data) - start:
        raise FormatError("Truncated manifest")


def _u(data: mmap.mmap | bytes, start: int, length: int) -> int:
    if start < 0 or length < 0 or start + length > len(data):
        raise FormatError("Truncated manifest integer")
    return int.from_bytes(data[start:start + length], "big")


def _cstring(data: mmap.mmap, start: int, maximum: int = 32768) -> tuple[str, int]:
    end = data.find(b"\0", start, min(len(data), start + maximum + 1))
    if end < 0:
        raise FormatError("Unterminated or oversized manifest string")
    try:
        return data[start:end].decode("utf-8"), end + 1
    except UnicodeDecodeError as exc:
        raise FormatError("Invalid manifest string") from exc


def _tags(data: mmap.mmap, start: int, count: int, entries: int,
          selected: set[str]) -> tuple[int, list[bytes]]:
    """Return one OR bitmap for each explicitly selected tag type."""
    width = (entries + 7) // 8
    groups: dict[int, bytearray] = {}
    found = set()
    names = set()
    for _ in range(count):
        name, start = _cstring(data, start, 1024)
        kind = _u(data, start, 2)
        start += 2
        _require(data, start, width)
        if not name or name.casefold() in names:
            raise FormatError("Invalid or duplicate manifest tag")
        names.add(name.casefold())
        if name.casefold() in selected:
            found.add(name.casefold())
            union = groups.setdefault(kind, bytearray(width))
            for i, value in enumerate(data[start:start + width]):
                union[i] |= value
        start += width
    if selected - found:
        raise FormatError("Unknown manifest tags: " + ", ".join(sorted(selected - found)))
    return start, [bytes(mask) for mask in groups.values()]


def _selected(index: int, masks: list[bytes]) -> bool:
    byte, bit = divmod(index, 8)
    return all(mask[byte] & (128 >> bit) for mask in masks)


def read_install(path: str | os.PathLike[str], selected_tags: set[str]) -> Iterator[InstallEntry]:
    """Yield selected external files from an IN v1 manifest."""
    with _mapped(path) as data:
        _require(data, 0, 10)
        if data[:4] != b"IN\x01\x10":
            raise FormatError("Unsupported install manifest header")
        count = _u(data, 6, 4)
        start, masks = _tags(data, 10, _u(data, 4, 2), count,
                             {name.casefold() for name in selected_tags})
        # Even empty filenames require a NUL, a full CKey, and a u32 size.
        _require(data, start, count * 21)
        for i in range(count):
            name, start = _cstring(data, start)
            _require(data, start, 20)
            entry = InstallEntry(name, data[start:start + 16], _u(data, start + 16, 4))
            start += 20
            if not name:
                raise FormatError("Empty install path")
            if _selected(i, masks):
                yield entry
        if start != len(data):
            raise FormatError("Trailing bytes in install manifest")


def read_download(path: str | os.PathLike[str], selected_tags: set[str]) -> Iterator[DownloadEntry]:
    """Yield DL v1-v3 entries, preserving signed effective priorities."""
    with _mapped(path) as data:
        _require(data, 0, 11)
        if data[:2] != b"DL" or data[2] not in (1, 2, 3) or data[3] != 16 or data[4] not in (0, 1):
            raise FormatError("Unsupported download manifest header")
        version = data[2]
        count = _u(data, 5, 4)
        start = 11
        flags = base = 0
        if version >= 2:
            flags = _u(data, start, 1)
            start += 1
        if version >= 3:
            _require(data, start, 4)
            base = int.from_bytes(data[start:start + 1], "big", signed=True)
            start += 4  # signed base plus three reserved bytes
        stride = 22 + (4 if data[4] else 0) + flags
        _require(data, start, count * stride)
        end, masks = _tags(data, start + count * stride, _u(data, 9, 2), count,
                           {name.casefold() for name in selected_tags})
        if end != len(data):
            raise FormatError("Trailing bytes in download manifest")
        for i in range(count):
            if _selected(i, masks):
                offset = start + i * stride
                priority = int.from_bytes(data[offset + 21:offset + 22], "big", signed=True) - base
                yield DownloadEntry(data[offset:offset + 16], _u(data, offset + 16, 5), priority)


class EncodingTable:
    """An mmap-backed EN v1 CKey/EKey index, with every page hash checked once.

    Lookups only parse the addressed page. The 2.2 million current Forever
    entries never become a Python dictionary.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self._stream = open(path, "rb")
        self._data: mmap.mmap | None = None
        try:
            if os.fstat(self._stream.fileno()).st_size < 22:
                raise FormatError("Truncated encoding header")
            self._data = mmap.mmap(self._stream.fileno(), 0, access=mmap.ACCESS_READ)
            data = self._data
            if data[:5] != b"EN\x01\x10\x10" or data[17] != 0:
                raise FormatError("Unsupported encoding manifest header")
            self._cpage_size = _u(data, 5, 2) * 1024
            self._epage_size = _u(data, 7, 2) * 1024
            self._ccount = _u(data, 9, 4)
            self._ecount = _u(data, 13, 4)
            if not self._cpage_size or not self._epage_size:
                raise FormatError("Encoding page size is zero")
            spec_size = _u(data, 18, 4)
            if spec_size > 64 * 1024 * 1024:
                raise FormatError("Encoding specification table exceeds 64 MiB")
            _require(data, 22, spec_size)
            try:
                self._specs = data[22:22 + spec_size].decode("ascii").split("\0")
            except UnicodeDecodeError as exc:
                raise FormatError("Encoding specifications are not ASCII") from exc
            self._ci = 22 + spec_size
            self._cp = self._ci + self._ccount * 32
            self._ei = self._cp + self._ccount * self._cpage_size
            self._ep = self._ei + self._ecount * 32
            self._tail = self._ep + self._ecount * self._epage_size
            _require(data, self._ci, self._tail - self._ci)
            self._ckeys = self._check_pages(self._ci, self._cp, self._ccount, self._cpage_size, 6)
            self._ekeys = self._check_pages(self._ei, self._ep, self._ecount, self._epage_size, 0)
        except BaseException:
            self.close()
            raise

    def _check_pages(self, index: int, pages: int, count: int, size: int, key_offset: int) -> list[bytes]:
        data = self._data
        assert data is not None
        keys = []
        for i in range(count):
            offset = index + i * 32
            key = data[offset:offset + 16]
            page = pages + i * size
            with memoryview(data)[page:page + size] as view:
                if hashlib.md5(view).digest() != data[offset + 16:offset + 32]:
                    raise FormatError("Encoding page MD5 mismatch")
            if key != data[page + key_offset:page + key_offset + 16] or (keys and key <= keys[-1]):
                raise FormatError("Invalid encoding page index")
            keys.append(key)
        return keys

    def _page(self, key: bytes, content: bool) -> tuple[mmap.mmap, int, int]:
        _key(key)
        if self._data is None:
            raise ValueError("Encoding table is closed")
        keys = self._ckeys if content else self._ekeys
        index = bisect_right(keys, key) - 1
        if index < 0:
            raise KeyError(key.hex())
        size = self._cpage_size if content else self._epage_size
        start = (self._cp if content else self._ep) + index * size
        return self._data, start, start + size

    def lookup(self, ckey: bytes) -> tuple[int, list[EncodingInfo]]:
        data, start, end = self._page(ckey, True)
        found = None
        previous = None
        while start < end and data[start]:
            count = data[start]
            stop = start + 22 + count * 16
            if stop > end:
                raise FormatError("Content entry crosses an encoding page")
            key = data[start + 6:start + 22]
            if previous is not None and key <= previous:
                raise FormatError("Unsorted encoding content page")
            previous = key
            if key == ckey:
                found = (_u(data, start + 1, 5), [data[p:p + 16] for p in range(start + 22, stop, 16)])
            start = stop
        if any(data[start:end]):
            raise FormatError("Nonzero content page padding")
        if found is None:
            raise KeyError(ckey.hex())
        return found[0], [self.get(key) for key in found[1]]

    def get(self, ekey: bytes) -> EncodingInfo:
        data, start, end = self._page(ekey, False)
        found = None
        previous = None
        while start + 25 <= end and any(data[start:start + 16]):
            key = data[start:start + 16]
            if previous is not None and key <= previous:
                raise FormatError("Unsorted encoding key page")
            previous = key
            spec = _u(data, start + 16, 4)
            if spec >= len(self._specs):
                raise FormatError("Invalid encoding specification index")
            if key == ekey:
                found = EncodingInfo(key, _u(data, start + 20, 5), self._specs[spec])
            start += 25
        padding = data[start:end]
        if any(padding):
            if len(padding) < 20 or padding[:20] != bytes(16) + b"\xff" * 4 or any(padding[20:]) or end != self._tail:
                raise FormatError("Invalid encoding key page padding")
        if found is None:
            raise KeyError(ekey.hex())
        return found

    def close(self) -> None:
        if self._data is not None:
            self._data.close()
            self._data = None
        self._stream.close()

    def __enter__(self) -> EncodingTable:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
