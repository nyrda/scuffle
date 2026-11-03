"""Anonymous, verified NGDP metadata and CDN transport.

Only public metadata and content enter this cache. This module does not inspect
Battle.net profiles or use a cookie jar, proxy credentials, or authorization.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from contextlib import contextmanager
from bisect import bisect_right
import hashlib
import http.client
import io
import ipaddress
import itertools
import mmap
import os
from pathlib import Path
import re
import secrets
import sqlite3
import ssl
import stat
import threading
import time
from typing import BinaryIO, Callable, Iterable, Iterator, Mapping
from urllib.parse import urlsplit

from .. import system
from .models import DownloadEntry, Location


class CDNError(RuntimeError):
    """A bounded, anonymous CDN operation failed."""


class HTTPStatusError(CDNError):
    def __init__(self, status: int, url: str):
        self.status = status
        super().__init__(f"CDN HTTP {status}: {url}")


class IntegrityError(CDNError):
    """Content differs from its advertised size, key, or checksums."""


def _key(key: str | bytes) -> str:
    value = key.hex() if isinstance(key, bytes) else key.lower()
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("Expected a full 16-byte content key")
    return value


def parse_psv(data: bytes) -> list[dict[str, str]]:
    lines = [line for line in data.decode("utf-8-sig").splitlines()
             if line.strip() and not line.startswith("#")]
    if not lines:
        raise IntegrityError("Empty version-service response")
    names = [field.split("!", 1)[0] for field in lines[0].split("|")]
    if len(set(names)) != len(names):
        raise IntegrityError("Duplicate version-service columns")
    rows = []
    for line in lines[1:]:
        values = line.split("|")
        if len(values) != len(names):
            raise IntegrityError("Malformed version-service row")
        rows.append(dict(zip(names, values)))
    return rows


def parse_config(data: bytes | Path) -> dict[str, list[str]]:
    if isinstance(data, Path):
        data = data.read_bytes()
    result = {}
    for line in data.decode("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise IntegrityError("Malformed CDN configuration line")
        name, value = line.split("=", 1)
        if name.strip() in result:
            raise IntegrityError("Duplicate CDN configuration field")
        result[name.strip()] = value.split()
    return result


def _index_entries(data, key: str, archive: str,
                   archives: tuple[str, ...] | None,
                   expected_offsets: int | None = None) -> Iterator[tuple[bytes, Location]]:
    """Validate the footer, TOC and every page before yielding their entries.

    CDN indices hash the final footer, not the whole file. The TOC hash covers
    its EKey table and page hashes, including the last-page hash in the footer.
    Group ordinals address the original CDN configuration's archives array.
    """
    length = len(data)
    found = None
    for checksum in range(1, 17):
        meta_at = length - 12 - checksum
        if meta_at < checksum * 2:
            continue
        meta = bytes(data[meta_at:meta_at + 12])
        if meta[0] == 1 and meta[7] == checksum:
            if hashlib.md5(meta + bytes(checksum)).digest()[:checksum] == bytes(data[-checksum:]):
                found = (checksum, meta_at, meta)
                break
    if found is None:
        raise IntegrityError("Archive-index footer checksum or version is invalid")
    checksum, meta_at, meta = found
    version, flags0, flags1, page_kib, offsets, sizes, keys, _ = meta[:8]
    if (flags0, flags1, sizes, keys) != (0, 0, 4, 16) or offsets not in (0, 4, 6) or not 1 <= page_kib <= 64:
        raise IntegrityError("Unsupported archive-index dimensions")
    if expected_offsets is not None and offsets != expected_offsets:
        raise IntegrityError("Archive-index type differs from its configuration field")
    if offsets == 6 and archives is None:
        raise IntegrityError("Group index requires its original archive list")
    count = int.from_bytes(meta[8:12], "little")
    page_size = page_kib * 1024
    stride = keys + sizes + offsets
    per_page = page_size // stride
    pages = (count + per_page - 1) // per_page
    if not pages:
        raise IntegrityError("Empty archive index")
    toc_at = pages * page_size
    if length != toc_at + pages * (keys + checksum) + 12 + 2 * checksum:
        raise IntegrityError("Archive-index length disagrees with its footer")
    if hashlib.md5(data[meta_at - checksum:]).hexdigest() != _key(key):
        raise IntegrityError("Archive-index footer does not match its key")
    if hashlib.md5(data[toc_at:meta_at - checksum]).digest()[:checksum] != bytes(data[meta_at - checksum:meta_at]):
        raise IntegrityError("Archive-index TOC checksum mismatch")
    hashes_at = toc_at + pages * keys
    previous = b""
    seen = 0
    for page in range(pages):
        block = data[page * page_size:(page + 1) * page_size]
        expected_hash = bytes(data[hashes_at + page * checksum:hashes_at + (page + 1) * checksum])
        if hashlib.md5(block).digest()[:checksum] != expected_hash:
            raise IntegrityError("Archive-index page checksum mismatch")
        n = min(per_page, count - seen)
        if any(block[n * stride:]):
            raise IntegrityError("Nonzero archive-index page padding")
        last_key = bytes(block[(n - 1) * stride:(n - 1) * stride + keys])
        if last_key != bytes(data[toc_at + page * keys:toc_at + (page + 1) * keys]):
            raise IntegrityError("Archive-index TOC key does not match its page")
        for entry in range(n):
            at = entry * stride
            ekey = bytes(block[at:at + keys])
            if not any(ekey) or ekey <= previous:
                raise IntegrityError("Archive-index keys are zero, duplicated, or unsorted")
            previous = ekey
            size = int.from_bytes(block[at + keys:at + keys + sizes], "big")
            if size < 8:
                raise IntegrityError("Archive-index object is too short for BLTE")
            off_at = at + keys + sizes
            if offsets == 6:
                ordinal = int.from_bytes(block[off_at:off_at + 2], "big")
                if ordinal >= len(archives):
                    raise IntegrityError("Archive-group ordinal is outside its archive list")
                name = archives[ordinal]
                offset = int.from_bytes(block[off_at + 2:off_at + 6], "big")
            else:
                name = archive if offsets else ""
                offset = int.from_bytes(block[off_at:off_at + offsets], "big")
            if offset + size > 1 << 32 and offsets:
                raise IntegrityError("Archive-index range exceeds the archive address space")
            seen += 1
            yield ekey, Location(name, offset, size)


def parse_index(source: Path | bytes, key: str, *, archive: str | None = None,
                archives: Iterable[str] | None = None,
                expected_offsets: int | None = None) -> Iterator[tuple[bytes, Location]]:
    """Read an authenticated standard, loose-file, or archive-group index."""
    archive = _key(key) if archive is None else archive
    names = tuple(archives) if archives is not None else None
    if isinstance(source, Path):
        with source.open("rb") as stream:
            if source.stat().st_size == 0:
                raise IntegrityError("Empty archive index")
            with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
                yield from _index_entries(data, key, archive, names, expected_offsets)
    else:
        yield from _index_entries(source, key, archive, names, expected_offsets)




class TransferCancelled(RuntimeError):
    """The caller stopped a bulk transfer."""


class Throttle:
    """One small token bucket shared by all connections, including retries."""

    def __init__(self, limit: int = 0, *, clock=time.monotonic, sleep=time.sleep):
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()
        self._limit = max(0, limit)
        self._available = 0.0
        self._updated = clock()

    @property
    def limit(self) -> int:
        return self._limit

    @limit.setter
    def limit(self, value: int) -> None:
        with self._lock:
            self._limit = max(0, value)
            self._available = 0.0
            self._updated = self._clock()

    def consume(self, size: int, check: Callable[[], None] = lambda: None) -> None:
        while size > 1e-6:
            check()
            with self._lock:
                limit = self._limit
                if limit <= 0:
                    return
                now = self._clock()
                capacity = max(1, min(64 * 1024, limit / 10))
                self._available = min(capacity, self._available + (now - self._updated) * limit)
                self._updated = now
                taken = min(size, self._available)
                size -= taken
                self._available -= taken
                delay = min(size, capacity) / limit
            if size > 1e-6:
                self._sleep(min(delay, 0.1))

    def refund(self, size: int) -> None:
        if not self._limit:
            return
        with self._lock:
            self._available = min(max(1, min(64 * 1024, self._limit / 10)),
                                  self._available + size)


class CDNClient:
    """Bounded, retrying HTTPS transport with a persistent public-content cache."""
    def __init__(self, cache: Path, region: str, product: str,
                 workers: int = 8, *, allow_http: bool = False,
                 metadata_base: str | None = None, timeout: float = 30,
                 retries: int = 3, backoff: float = 0.25, throttle: Throttle | None = None,
                 stall_timeout: float = 5):
        if not re.fullmatch(r"[a-z0-9_-]+", region) or not re.fullmatch(r"[a-z0-9_-]+", product):
            raise ValueError("Invalid region or product")
        if not 1 <= workers <= 64 or not 1 <= retries <= 10:
            raise ValueError("Invalid connection or retry bound")
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.region, self.product = region, product
        self.workers, self.allow_http = workers, allow_http
        self.throttle = throttle or Throttle()
        if timeout <= 0 or stall_timeout <= 0:
            raise ValueError("Timeouts must be positive")
        # A CDN edge can take several seconds to fetch an uncached range from its origin,
        # so the first byte gets timeout. Once the body flows, silence for stall_timeout fails over.
        self.timeout, self.retries, self.backoff = timeout, retries, backoff
        self.stall_timeout = min(timeout, stall_timeout)
        self._transfer = threading.local()
        self._mirror_turn = itertools.count()
        self.metadata_base = metadata_base or f"https://{region}.version.battle.net/v2/products/{product}"
        self.mirrors: list[str] = []
        self.config_mirrors: list[str] = []
        self.version: dict[str, str] | None = None
        self.cdn: dict[str, str] | None = None
        self._ssl = ssl.create_default_context()
        self._slots = threading.BoundedSemaphore(workers)
        self._pool_lock = threading.Lock()
        self._pool: dict[tuple, list[http.client.HTTPConnection]] = {}
        self._cooldown: dict[str, float] = {}
        self._host_rate: dict[str, float] = {}
        self._host_active: dict[str, int] = {}
        self._locks = [threading.Lock() for _ in range(256)]
        self._index_lock = threading.RLock()
        self._cdn_lock = threading.RLock()
        self._archives: tuple[str, ...] = ()
        self._archive_config: dict[str, list[str]] = {}
        self._db_path: Path | None = None
        self._prepared = False

    def close(self) -> None:
        with self._pool_lock:
            for connections in self._pool.values():
                for connection in connections:
                    connection.close()
            self._pool.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _url(self, url: str):
        parsed = urlsplit(url)
        if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
            raise ValueError("CDN URL must have an ordinary anonymous origin")
        if parsed.scheme != "https":
            try:
                local = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                local = False
            if not (self.allow_http and parsed.scheme == "http" and local):
                raise ValueError("Only verified HTTPS is allowed, except explicit loopback HTTP tests")
        return parsed

    @contextmanager
    def _connection(self, url: str):
        parsed = self._url(url)
        origin = (parsed.scheme, parsed.hostname, parsed.port)
        with self._slots:
            with self._pool_lock:
                self._host_active[parsed.netloc] = self._host_active.get(parsed.netloc, 0) + 1
                idle = self._pool.setdefault(origin, [])
                connection = idle.pop() if idle else None
            if connection is None:
                if parsed.scheme == "https":
                    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443,
                        timeout=self.timeout, context=self._ssl)
                else:
                    connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=self.timeout)
            try:
                yield connection, parsed.path + ("?" + parsed.query if parsed.query else "")
            except BaseException:
                connection.close()
                raise
            else:
                with self._pool_lock:
                    if sum(map(len, self._pool.values())) < self.workers:
                        self._pool[origin].append(connection)
                    else:
                        connection.close()
            finally:
                with self._pool_lock:
                    self._host_active[parsed.netloc] -= 1

    def _read(self, url: str, stream: BinaryIO, limit: int, *,
              byte_range: tuple[int, int] | None = None,
              allow_restart: bool = False, expected_total: int | None = None,
              progress: Callable[[int], None] | None = None) -> int:
        headers = {"User-Agent": "scuffle/0.1", "Accept": "*/*", "Accept-Encoding": "identity"}
        if byte_range is not None:
            start, end = byte_range
            if start < 0 or end < start:
                raise ValueError("Invalid HTTP range")
            headers["Range"] = f"bytes={start}-{end}"
        self._check_transfer()
        if progress:
            progress(0)
        request_started = time.monotonic()
        with self._connection(url) as (connection, target):
            if getattr(connection, "sock", None) is not None:
                connection.sock.settimeout(self.timeout)     # a pooled connection still has the body's
            connection.request("GET", target, headers=headers)
            # Kept before getresponse, which detaches it for a Connection: close response.
            sock = getattr(connection, "sock", None)
            response = connection.getresponse()
            if sock is not None:
                sock.settimeout(self.stall_timeout)
            if response.status not in (200, 206):
                raise HTTPStatusError(response.status, url)
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise IntegrityError("Unexpected HTTP content encoding")
            expected_length = expected_total
            if byte_range is None:
                if response.status != 200:
                    raise IntegrityError("Unsolicited partial response")
            elif response.status == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.getheader("Content-Range", ""))
                if match is None or (int(match[1]), int(match[2])) != byte_range or int(match[3]) <= byte_range[1]:
                    raise IntegrityError("HTTP Content-Range does not match the exact requested range")
                if expected_total is not None and int(match[3]) != expected_total:
                    raise IntegrityError("HTTP object size changed during resume")
                expected_length = byte_range[1] - byte_range[0] + 1
            elif allow_restart:
                stream.seek(0)
                stream.truncate()
            else:
                raise IntegrityError("CDN ignored archive Range; refusing the full archive")
            length_header = response.getheader("Content-Length")
            if length_header is not None:
                if not re.fullmatch(r"\d+", length_header):
                    raise IntegrityError("Invalid HTTP Content-Length")
                length = int(length_header)
                if length > limit or (expected_length is not None and length != expected_length):
                    raise IntegrityError("HTTP Content-Length exceeds the bound or expected size")
            total = 0
            network_time = 0.0
            network_bytes = 0
            body_length = int(length_header) if length_header is not None else None
            while body_length is None or total < body_length:
                self._check_transfer()
                amount = min(64 * 1024, limit - total + 1)
                if self.throttle.limit:
                    amount = min(amount, max(1, self.throttle.limit // 10))
                if body_length is not None:
                    amount = min(amount, body_length - total)
                self.throttle.consume(amount, self._check_transfer)
                started = time.monotonic()
                block = response.read1(amount)
                network_time += time.monotonic() - started
                network_bytes += len(block)
                self.throttle.refund(amount - len(block))
                if not block:
                    break
                total += len(block)
                if total > limit:
                    raise IntegrityError("HTTP body exceeds its bounded size")
                stream.write(block)
                if progress:
                    progress(total)
                # Socket timeouts catch silence. This also catches a host that
                # trickles bytes forever, excluding deliberate throttle waits.
                if network_time >= self.stall_timeout:
                    if network_bytes / network_time < 1024:
                        raise TimeoutError("CDN response stalled")
                    network_time = 0.0
                    network_bytes = 0
            if expected_length is not None and total != expected_length:
                raise http.client.IncompleteRead(b"", expected_length - total)
            if length_header is not None and total != int(length_header):
                raise http.client.IncompleteRead(b"", int(length_header) - total)
            if total and not self.throttle.limit:
                rate = total / max(1e-6, time.monotonic() - request_started)
                with self._pool_lock:
                    host = urlsplit(url).netloc
                    previous = self._host_rate.get(host, rate)
                    self._host_rate[host] = previous * .75 + rate * .25
            return total

    def _check_transfer(self) -> None:
        check = getattr(self._transfer, "check", None)
        if check:
            check()

    def _backoff(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while True:
            self._check_transfer()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))

    def _failed_mirror(self, url: str, error: Exception) -> None:
        if (isinstance(error, (OSError, http.client.HTTPException)) or
            isinstance(error, HTTPStatusError) and error.status in {403, 429, 500, 502, 503, 504}):
            with self._pool_lock:
                self._cooldown[urlsplit(url).netloc] = time.monotonic() + 30

    def _bytes(self, urls: Iterable[str], limit: int, *, validator=None, **kwargs) -> bytes:
        last = None
        urls = tuple(urls)
        if not urls:
            raise CDNError("No HTTPS CDN mirrors configured")
        for attempt in range(self.retries):
            missing = True
            for url in urls:
                try:
                    out = io.BytesIO()
                    self._read(url, out, limit, **kwargs)
                    data = out.getvalue()
                    if validator is not None:
                        validator(data)
                    return data
                except (OSError, http.client.HTTPException, ValueError, CDNError) as error:
                    last = error
                    self._failed_mirror(url, error)
                    if not isinstance(error, HTTPStatusError) or error.status != 404:
                        missing = False
            if missing:
                break
            if attempt + 1 < self.retries:
                self._backoff(self.backoff * 2 ** attempt)
        raise last

    def discover(self) -> tuple[dict[str, str], dict[str, str]]:
        version_data = self._bytes([self.metadata_base.rstrip("/") + "/versions"], 1024 * 1024)
        cdn_data = self._bytes([self.metadata_base.rstrip("/") + "/cdns"], 1024 * 1024)
        versions, cdns = parse_psv(version_data), parse_psv(cdn_data)
        try:
            version = next(row for row in versions if row.get("Region") == self.region)
            cdn = next(row for row in cdns if row.get("Name") == self.region)
        except StopIteration as error:
            raise IntegrityError("Version service does not advertise the requested region") from error
        for name in ("BuildConfig", "CDNConfig", "ProductConfig"):
            _key(version[name])
        self.set_cdn(cdn)
        self.version, self.cdn = version, cdn
        for name, data in (("versions", version_data), ("cdns", cdn_data)):
            path = self.cache / f"{self.product}-{self.region}-{name}.psv"
            self._atomic(path, data)
        return version, cdn

    # The probe used discovery(); keep that spelling available to callers.
    discovery = discover

    def runtime_keyring(self) -> str | None:
        """Find the published public keyring needed by the shared WoW runtime.

        Forever currently leaves its KeyRing column empty. In that case use the
        same region's base-wow metadata, without replacing this client's product,
        version or CDN selection. This fallback follows current title metadata;
        it is not pinned to the Forever build's release date.
        """
        if self.version is None:
            self.discover()
        if self.version.get("Region") != self.region:
            raise IntegrityError("Runtime keyring metadata does not match the requested region")
        advertised = self.version.get("KeyRing", "")
        if advertised:
            return _key(advertised)
        # A supplied metadata_base may be either a product URL, as in production,
        # or a fixture prefix whose current-product tables are /versions /cdns.
        # Preserve that origin and prefix when selecting the shared title.
        parsed = self._url(self.metadata_base)
        prefix = parsed.path.rstrip("/")
        product_suffix = "/" + self.product
        if prefix.endswith(product_suffix):
            prefix = prefix[:-len(product_suffix)]
        base = parsed._replace(path=prefix + "/wow", query="").geturl()
        data = self._bytes([base + "/versions"], 1024 * 1024)
        regional = [row for row in parse_psv(data) if row.get("Region") == self.region]
        if len(regional) != 1:
            raise IntegrityError("Shared WoW keyring metadata must advertise exactly one row for the requested region")
        key = regional[0].get("KeyRing", "")
        return _key(key) if key else None

    def set_cdn(self, row: Mapping[str, str]) -> None:
        path = row.get("Path", "tpr/wow").strip("/")
        config_path = row.get("ConfigPath", "tpr/configs/data").strip("/")
        if any(part in ("", ".", "..") for part in (path + "/" + config_path).split("/")):
            raise IntegrityError("Unsafe CDN metadata path")
        roots = []
        for server in row.get("Servers", "").split():
            parsed = urlsplit(server)
            if parsed.scheme != "https" and not self.allow_http:
                continue
            # Server query strings describe mirror policy, not file paths.
            root = f"{parsed.scheme}://{parsed.netloc}"
            self._url(root)
            if root not in roots:
                roots.append(root)
        # Hosts may only be advertised with HTTP URLs in Servers. Upgrade
        # their origins to HTTPS, still requiring normal certificate validation.
        for host in row.get("Hosts", "").split():
            root = f"https://{host}"
            parsed = self._url(root)
            if parsed.path or parsed.query or parsed.port is not None:
                raise IntegrityError("Invalid CDN hostname")
            if root not in roots:
                roots.append(root)
        if not roots:
            raise IntegrityError("No advertised verified HTTPS CDN")
        self.mirrors = [f"{root}/{path}" for root in roots]
        self.config_mirrors = [f"{root}/{config_path}" for root in roots]
        self.cdn = dict(row)

    def _ensure_cdn(self):
        with self._cdn_lock:
            if not self.mirrors:
                self.discover()

    def _urls(self, key: str, kind: str = "data", suffix: str = "") -> list[str]:
        self._ensure_cdn()
        start = next(self._mirror_turn) % len(self.mirrors)
        mirrors = self.mirrors[start:] + self.mirrors[:start]
        with self._pool_lock:
            now = time.monotonic()
            primary = urlsplit(self.mirrors[0]).netloc
            def score(root):
                host = urlsplit(root).netloc
                active = self._host_active.get(host, 0)
                rate = self._host_rate.get(host)
                # Probe an unmeasured alternate once before sending it a wave
                # of requests. Then share work by observed rate and load.
                if rate is None:
                    rate = 0 if active and host != primary else 16 * 1024 * 1024
                return self._cooldown.get(host, 0) > now, -rate / (active + 1)
            mirrors.sort(key=score)
        return [f"{root}/{kind}/{key[:2]}/{key[2:4]}/{key}{suffix}" for root in mirrors]

    def _atomic(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        with part.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(part, path)
        system.sync_directory(path.parent)

    def _download(self, urls: Iterable[str], path: Path, *, limit: int,
                  expected_size: int | None = None, validator: Callable[[Path], None],
                  resume: bool = False, progress: Callable[[int], None] | None = None) -> Path:
        urls = tuple(urls)
        if not urls:
            raise CDNError("No CDN mirrors configured")
        lock = self._locks[hash(path) % len(self._locks)]
        with lock:
            if path.exists():
                try:
                    if expected_size is not None and path.stat().st_size != expected_size:
                        raise IntegrityError("Cached object has the wrong size")
                    validator(path)
                    return path
                except (ValueError, CDNError):
                    path.unlink()
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_name(path.name + ".part")
            last = None
            for attempt in range(self.retries):
                missing = True
                for url in urls:
                    try:
                        offset = part.stat().st_size if resume and part.exists() else 0
                        if expected_size is not None and offset >= expected_size:
                            if offset == expected_size:
                                validator(part)
                                os.replace(part, path)
                                system.sync_directory(path.parent)
                                return path
                            offset = 0
                        if offset and expected_size is None:
                            offset = 0
                        with part.open("r+b" if offset else "wb") as stream:
                            stream.seek(offset)
                            byte_range = (offset, expected_size - 1) if offset else None
                            self._read(url, stream, limit, byte_range=byte_range,
                                       allow_restart=bool(offset), expected_total=expected_size,
                                       progress=progress)
                            stream.flush()
                            os.fsync(stream.fileno())
                        if expected_size is not None and part.stat().st_size != expected_size:
                            raise IntegrityError("Downloaded object has the wrong size")
                        validator(part)
                        os.replace(part, path)
                        system.sync_directory(path.parent)
                        return path
                    except (OSError, http.client.HTTPException, ValueError, CDNError) as error:
                        last = error
                        self._failed_mirror(url, error)
                        if not isinstance(error, HTTPStatusError) or error.status != 404:
                            missing = False
                        if isinstance(error, (ValueError, IntegrityError)):
                            part.unlink(missing_ok=True)
                if missing:
                    break
                if attempt + 1 < self.retries:
                    self._backoff(self.backoff * 2 ** attempt)
            raise last

    def _config(self, key: str, product: bool) -> Path:
        key = _key(key)
        self._ensure_cdn()
        urls = ([f"{root}/{key[:2]}/{key[2:4]}/{key}" for root in self.config_mirrors]
                if product else self._urls(key, "config"))
        def verify(path):
            if hashlib.md5(path.read_bytes()).hexdigest() != key:
                raise IntegrityError("Configuration MD5 does not match its key")
        return self._download(urls, self.cache / "configs" / key,
                              limit=4 * 1024 * 1024, validator=verify)

    def get_config(self, key: str) -> Path:
        return self._config(key, False)

    def get_product_config(self, key: str) -> Path:
        return self._config(key, True)

    def get_plain(self, key: str, limit: int = 16 * 1024 * 1024) -> bytes:
        """A data file stored as it is, not BLTE encoded, whose MD5 is its key.

        Battle.net's catalogs product publishes its JSON files this way.
        """
        key = _key(key)
        def verify(data):
            if hashlib.md5(data).hexdigest() != key:
                raise IntegrityError("File MD5 does not match its key")
        return self._bytes(self._urls(key), limit, validator=verify)

    def configure_archives(self, config: Mapping[str, list[str]] | Path) -> None:
        if isinstance(config, Path):
            config = parse_config(config)
        archives = tuple(_key(name) for name in config.get("archives", []))
        # Preserve the advertised order. Group ordinals refer to these positions.
        identity = hashlib.sha256(" ".join(archives).encode() + b"\0" +
                                 " ".join(config.get("file-index", [])).encode()).hexdigest()
        self._archives = archives
        self._archive_config = dict(config)
        self._db_path = self.cache / "indexes" / f"{identity}.sqlite3"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._database() as db:
            db.execute("CREATE TABLE IF NOT EXISTS objects (ekey BLOB PRIMARY KEY, archive TEXT NOT NULL, offset INTEGER NOT NULL, size INTEGER NOT NULL) WITHOUT ROWID")
            db.execute("CREATE TABLE IF NOT EXISTS imported (key TEXT PRIMARY KEY) WITHOUT ROWID")
        self._prepared = False

    @contextmanager
    def _database(self):
        if self._db_path is None:
            raise CDNError("Call configure_archives before looking up archive entries")
        db = sqlite3.connect(self._db_path, timeout=120)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            # The log shrinks back after a large import instead of keeping its peak size.
            db.execute("PRAGMA journal_size_limit=67108864")
            with db:
                yield db
        finally:
            db.close()

    def seed_index(self, key: str, source: Path, *, group: bool = False,
                   loose: bool = False) -> None:
        """Copy a verified public index into this cache without linking its source.

        Configure archives first for a group index. Its ordinals use that exact
        advertised order. The source is read only; the staged copy must pass all
        footer, TOC and page checks before an atomic cache replacement.
        """
        key = _key(key)
        if group and loose:
            raise ValueError("An index cannot be both grouped and loose")
        if group and self._db_path is None:
            raise CDNError("Configure archives before seeding an archive-group index")
        source = Path(source)
        destination = self.cache / "indexes" / (key + ".index")
        if os.open not in os.supports_dir_fd:
            # Windows has no directory descriptors. Copy and check by path.
            with self._locks[hash(destination) % len(self._locks)]:
                self._seed_index_by_path(key, source, destination, group=group, loose=loose)
            return
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        with self._locks[hash(destination) % len(self._locks)]:
            with os.fdopen(os.open(source, flags | os.O_NONBLOCK), "rb") as original:
                info = os.fstat(original.fileno())
                if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= 512 * 1024 * 1024:
                    raise IntegrityError("Index seed must be a bounded regular file")
                cache_fd = os.open(self.cache, flags | os.O_DIRECTORY)
                try:
                    try:
                        os.mkdir("indexes", dir_fd=cache_fd)
                    except FileExistsError:
                        pass
                    index_fd = os.open("indexes", flags | os.O_DIRECTORY, dir_fd=cache_fd)
                    try:
                        temporary = f".{key}.{secrets.token_hex(12)}.part"
                        copy_fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL |
                                          os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=index_fd)
                        try:
                            with os.fdopen(copy_fd, "w+b") as copied:
                                remaining = info.st_size
                                while remaining:
                                    block = original.read(min(1024 * 1024, remaining))
                                    if not block:
                                        raise IntegrityError("Index seed changed or was truncated during its copy")
                                    copied.write(block)
                                    remaining -= len(block)
                                if original.read(1):
                                    raise IntegrityError("Index seed grew during its copy")
                                copied.flush()
                                with mmap.mmap(copied.fileno(), 0, access=mmap.ACCESS_READ) as data:
                                    for _ in _index_entries(data, key, "" if loose else key,
                                            self._archives if group else None,
                                            6 if group else 0 if loose else 4):
                                        pass
                                os.fsync(copied.fileno())
                            # Replacing a destination symlink replaces the link
                            # itself; it cannot redirect writes outside the cache.
                            os.replace(temporary, key + ".index", src_dir_fd=index_fd,
                                       dst_dir_fd=index_fd)
                            os.fsync(index_fd)
                        finally:
                            try:
                                os.unlink(temporary, dir_fd=index_fd)
                            except FileNotFoundError:
                                pass
                    finally:
                        os.close(index_fd)
                finally:
                    os.close(cache_fd)

    def _seed_index_by_path(self, key: str, source: Path, destination: Path, *, group: bool, loose: bool) -> None:
        if source.is_symlink():
            # Match O_NOFOLLOW on the descriptor-based path.
            raise OSError(f"Index seed is a symbolic link: {source}")
        if not source.is_file():
            raise IntegrityError("Index seed must be a bounded regular file")
        size = source.stat().st_size
        if not 1 <= size <= 512 * 1024 * 1024:
            raise IntegrityError("Index seed must be a bounded regular file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.parent.is_symlink() or self.cache.is_symlink():
            raise OSError("The index cache must not be a symbolic link")
        temporary = destination.with_name(f".{key}.{secrets.token_hex(12)}.part")
        try:
            with source.open("rb") as original, temporary.open("xb") as copied:
                remaining = size
                while remaining:
                    block = original.read(min(1024 * 1024, remaining))
                    if not block:
                        raise IntegrityError("Index seed changed or was truncated during its copy")
                    copied.write(block)
                    remaining -= len(block)
                if original.read(1):
                    raise IntegrityError("Index seed grew during its copy")
                copied.flush()
                os.fsync(copied.fileno())
            with temporary.open("rb") as copied, mmap.mmap(copied.fileno(), 0, access=mmap.ACCESS_READ) as data:
                for _ in _index_entries(data, key, "" if loose else key,
                                        self._archives if group else None, 6 if group else 0 if loose else 4):
                    pass
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def _index_tag(self, key: str, *, group: bool = False, loose: bool = False) -> str:
        return ("group:" if group else "loose:" if loose else "archive:") + key

    def _index_rows(self, key: str, path: Path, *, group: bool = False,
                    loose: bool = False) -> Iterator[tuple[bytes, str, int, int]]:
        for ekey, location in parse_index(path, key, archive="" if loose else key,
                                          archives=self._archives if group else None,
                                          expected_offsets=6 if group else 0 if loose else 4):
            yield ekey, location.archive, location.offset, location.size

    def _fetch_index(self, key: str, *, group: bool = False, loose: bool = False) -> Path:
        path = self.cache / "indexes" / (key + ".index")
        def verify(index):
            # Exhaustion validates every page before the file enters the cache.
            for _ in self._index_rows(key, index, group=group, loose=loose):
                pass
        return self._download(self._urls(key, suffix=".index"), path,
                              limit=512 * 1024 * 1024, validator=verify, resume=False)

    def _import_index(self, key: str, *, group: bool = False, loose: bool = False) -> int:
        key = _key(key)
        tag = self._index_tag(key, group=group, loose=loose)
        with self._database() as db:
            if db.execute("SELECT 1 FROM imported WHERE key=?", (tag,)).fetchone():
                return 0
        path = self._fetch_index(key, group=group, loose=loose)
        count = 0
        with self._database() as db:
            rows = self._index_rows(key, path, group=group, loose=loose)
            # One validated index is one transaction. Interrupted imports resume.
            while batch := list(itertools.islice(rows, 4096)):
                db.executemany("INSERT OR IGNORE INTO objects VALUES (?,?,?,?)", batch)
                count += len(batch)
            db.execute("INSERT OR IGNORE INTO imported VALUES (?)", (tag,))
        return count

    def _import_archive_indexes(self, progress: Callable[[int, int], None] | None) -> None:
        """Import every archive's own index, for builds whose archive-group index is not published.

        Inserting a thousand indexes one by one rewrites most of the table's pages each time.
        Their rows are appended to a staging file instead, then sorted into the table at once.
        Indexes download in parallel, and only this thread writes.
        """
        staging = self._db_path.with_name(self._db_path.stem + ".staging.sqlite3")
        with self._database() as db:
            done = {row[0] for row in db.execute("SELECT key FROM imported")}
        staged = sqlite3.connect(staging, timeout=120)
        try:
            staged.execute("PRAGMA synchronous=FULL")
            with staged:
                staged.execute("CREATE TABLE IF NOT EXISTS objects (ekey BLOB NOT NULL, archive TEXT NOT NULL, offset INTEGER NOT NULL, size INTEGER NOT NULL)")
                staged.execute("CREATE TABLE IF NOT EXISTS imported (key TEXT PRIMARY KEY) WITHOUT ROWID")
            done |= {row[0] for row in staged.execute("SELECT key FROM imported")}
            missing = [key for key in self._archives if self._index_tag(key) not in done]
            total = len(self._archives)
            if progress:
                progress(total - len(missing), total)      # also when an earlier run staged them all
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                futures = {executor.submit(self._fetch_index, key): key for key in missing}
                for count, future in enumerate(as_completed(futures), total - len(missing) + 1):
                    key = futures[future]
                    path = future.result()
                    # A staged index and its tag commit together, so an interrupted import resumes.
                    with staged:
                        staged.executemany("INSERT INTO objects VALUES (?,?,?,?)", self._index_rows(key, path))
                        staged.execute("INSERT OR IGNORE INTO imported VALUES (?)", (self._index_tag(key),))
                    if progress:
                        progress(count, total)
        finally:
            staged.close()
        with self._database() as db:
            db.execute("ATTACH DATABASE ? AS staging", (str(staging),))
            # Sorted rows append to the table's end. Merging again after a crash changes nothing.
            db.execute("INSERT OR IGNORE INTO objects SELECT ekey,archive,offset,size FROM staging.objects ORDER BY ekey")
            db.execute("INSERT OR IGNORE INTO imported SELECT key FROM staging.imported")
        for suffix in ("", "-journal"):
            staging.with_name(staging.name + suffix).unlink(missing_ok=True)

    def prepare_indexes(self, progress: Callable[[int, int], None] | None = None) -> None:
        """Load loose and grouped/archive indexes, resuming validated imports."""
        with self._index_lock:
            if self._prepared:
                return
            if self._db_path is None:
                raise CDNError("Call configure_archives before preparing archive indexes")
            for key in self._archive_config.get("file-index", []):
                self._import_index(key, loose=True)
            groups = self._archive_config.get("archive-group", [])
            imported = False
            if groups:
                try:
                    self._import_index(groups[0], group=True)
                    imported = True
                except HTTPStatusError as error:
                    if error.status not in (403, 404):
                        raise
            if not imported:
                self._import_archive_indexes(progress)
            with self._database() as db:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._prepared = True

    def locate(self, ekey: bytes) -> Location | None:
        _key(ekey)
        self.prepare_indexes()
        with self._database() as db:
            row = db.execute("SELECT archive,offset,size FROM objects WHERE ekey=?", (ekey,)).fetchone()
        return Location(*row) if row else None

    def _object_path(self, ekey: bytes) -> Path:
        key = _key(ekey)
        return self.cache / "objects" / key[:2] / key

    def get_object(self, ekey: bytes, encoded_size: int | None = None,
                   location: Location | None = None, *,
                   progress: Callable[[int], None] | None = None) -> Path:
        from .formats import verify_blte
        key = _key(ekey)
        if encoded_size is not None and not 8 <= encoded_size <= 1 << 32:
            raise ValueError("Invalid encoded object size")
        path = self._object_path(ekey)
        with self._locks[hash(path) % len(self._locks)]:
            if path.exists():
                try:
                    if encoded_size is not None and path.stat().st_size != encoded_size:
                        raise IntegrityError("Cached object has the wrong size")
                    verify_blte(path, ekey)
                    return path
                except (ValueError, CDNError):
                    path.unlink(missing_ok=True)
        # Metadata manifests are usually loose and can be retrieved without the
        # archive scan. Bulk callers prepare indexes and supply locations.
        if location is None and self._prepared:
            location = self.locate(ekey)
        if location is not None and location.archive:
            if encoded_size is not None and encoded_size != location.size:
                raise IntegrityError("Archive index and manifest disagree about encoded size")
            data = self.get_range(location, validator=lambda data: verify_blte(data, ekey), progress=progress)
            path = self._object_path(ekey)
            with self._locks[hash(path) % len(self._locks)]:
                self._atomic(path, data)
            return path
        try:
            return self._download(self._urls(key), self._object_path(ekey),
                limit=encoded_size or 256 * 1024 * 1024, expected_size=encoded_size,
                validator=lambda path: verify_blte(path, ekey), resume=True, progress=progress)
        except HTTPStatusError as error:
            if error.status not in (403, 404) or self._db_path is None or self._prepared:
                raise
            location = self.locate(ekey)
            if location is None or not location.archive:
                raise
            return self.get_object(ekey, encoded_size, location, progress=progress)

    def get_range(self, location: Location, *, validator=None, progress=None) -> bytes:
        key = _key(location.archive)
        if location.offset < 0 or not 1 <= location.size <= 256 * 1024 * 1024:
            raise ValueError("Invalid or excessive archive range")
        return self._bytes(self._urls(key), location.size,
                           byte_range=(location.offset, location.offset + location.size - 1),
                           validator=validator, progress=progress)

    def _fetch_group(self, entries: list[tuple[DownloadEntry, Location]],
                     as_bytes: bool = False, progress=None) -> list[tuple[DownloadEntry, Path | bytes]]:
        from .formats import verify_blte
        result = []
        missing = []
        for entry, location in entries:
            path = self._object_path(entry.ekey)
            if path.exists():
                try:
                    if path.stat().st_size != entry.encoded_size:
                        raise IntegrityError("Cached object has the wrong size")
                    verify_blte(path, entry.ekey)
                    if as_bytes:
                        payload = path.read_bytes()
                        path.unlink(missing_ok=True)
                        result.append((entry, payload))
                    else:
                        result.append((entry, path))
                    continue
                except (ValueError, CDNError):
                    path.unlink(missing_ok=True)
            missing.append((entry, location))
        cached = sum(entry.encoded_size for entry, _ in result)
        if progress:
            progress(cached)
        entries = missing
        if not entries:
            return result
        if not entries[0][1].archive:
            for entry, location in entries:
                report = (lambda n: progress(cached + n)) if progress else None
                if as_bytes:
                    data = self._bytes(self._urls(_key(entry.ekey)), entry.encoded_size,
                                       expected_total=entry.encoded_size,
                                       validator=lambda data: verify_blte(data, entry.ekey), progress=report)
                    result.append((entry, data))
                else:
                    result.append((entry, self.get_object(entry.ekey, entry.encoded_size, location, progress=report)))
                cached += entry.encoded_size
                if progress:
                    progress(cached)
            return result
        first, last = entries[0][1], entries[-1][1]
        ends = [location.offset - first.offset + location.size for _, location in entries]
        sizes = [0]
        for entry, _ in entries:
            sizes.append(sizes[-1] + entry.encoded_size)
        def report(received):
            if progress:
                count = bisect_right(ends, received)
                partial = (max(0, received - (entries[count][1].offset - first.offset))
                           if count < len(entries) else 0)
                progress(cached + sizes[count] + partial)
        def verify_group(data):
            for entry, location in entries:
                begin = location.offset - first.offset
                verify_blte(data[begin:begin + location.size], entry.ekey)
        data = self.get_range(Location(first.archive, first.offset, last.offset + last.size - first.offset),
                              validator=verify_group, progress=report)
        for entry, location in entries:
            begin = location.offset - first.offset
            payload = data[begin:begin + location.size]
            if as_bytes:
                result.append((entry, payload))
            else:
                path = self._object_path(entry.ekey)
                with self._locks[hash(path) % len(self._locks)]:
                    self._atomic(path, payload)
                result.append((entry, path))
        return result

    def _planned_groups(self, entries: Iterable[DownloadEntry], block_size: int,
                        max_gap: int) -> Iterator[list[tuple[DownloadEntry, Location]]]:
        # SQLite's temporary table and external sort bound memory even for a
        # complete multi-million-object manifest. It also lets nearby archive
        # objects coalesce across the whole install, rather than random batches.
        with self._database() as db:
            db.execute("PRAGMA temp_store=FILE")
            db.execute("PRAGMA cache_size=-8192")
            db.execute("CREATE TEMP TABLE wanted (ekey BLOB PRIMARY KEY, size INTEGER NOT NULL, priority INTEGER NOT NULL) WITHOUT ROWID")
            def rows():
                for entry in entries:
                    _key(entry.ekey)
                    if not 8 <= entry.encoded_size <= 1 << 32:
                        raise ValueError("Invalid encoded object size in download plan")
                    yield entry.ekey, entry.encoded_size, entry.priority
            source = rows()
            while batch := list(itertools.islice(source, 4096)):
                db.executemany("INSERT OR IGNORE INTO wanted VALUES (?,?,?)", batch)
            mismatch = db.execute("SELECT 1 FROM wanted w JOIN objects o USING(ekey) WHERE w.size != o.size LIMIT 1").fetchone()
            if mismatch:
                raise IntegrityError("Archive index and download manifest size disagree")
            cursor = db.execute("""SELECT w.ekey,w.size,w.priority,
                COALESCE(o.archive,''),COALESCE(o.offset,0),COALESCE(o.size,w.size)
                FROM wanted w LEFT JOIN objects o USING(ekey)
                ORDER BY 4,5,w.ekey""")
            group = []
            for key, size, priority, archive, offset, indexed_size in cursor:
                location = Location(archive, offset, indexed_size)
                if group:
                    first, last = group[0][1], group[-1][1]
                    if (not archive or archive != first.archive or len(group) >= 4096 or
                        offset > last.offset + last.size + max_gap or
                        offset + indexed_size - first.offset > block_size):
                        yield group
                        group = []
                group.append((DownloadEntry(key, size, priority), location))
            if group:
                yield group

    def fetch_many(self, entries: Iterable[DownloadEntry], *, block_size: int = 8 * 1024 * 1024,
                   max_gap: int = 64 * 1024, as_bytes: bool = False,
                   progress: Callable[[int], None] | None = None,
                   cancelled: Callable[[], bool] = lambda: False
                   ) -> Iterator[tuple[DownloadEntry, Path | bytes]]:
        """Yield verified objects in completion order with bounded read-ahead.

        Progress counts unique object bytes received, excluding archive gaps and
        repeated retry bytes. It runs on the consumer thread, including during
        network silence. Only verified results may be written to CASC.
        """
        if not 1024 <= block_size <= 16 * 1024 * 1024 or max_gap < 0:
            raise ValueError("Invalid coalescing bounds")
        self.prepare_indexes()
        groups = self._planned_groups(entries, block_size, max_gap)
        stopped = threading.Event()
        lock = threading.Lock()
        received = 0
        def check():
            if stopped.is_set() or cancelled():
                raise TransferCancelled("Transfer stopped")
        def fetch(items):
            nonlocal received
            high_water = 0
            def report(amount):
                nonlocal received, high_water
                with lock:
                    received += max(0, amount - high_water)
                    high_water = max(high_water, amount)
            self._transfer.check = check
            try:
                check()
                result = self._fetch_group(items, as_bytes, report)
                report(sum(entry.encoded_size for entry, _ in result))
                return result
            finally:
                del self._transfer.check
        def notify():
            check()
            if progress:
                with lock:
                    amount = received
                progress(amount)
        executor = ThreadPoolExecutor(max_workers=self.workers)
        pending = set()
        try:
            notify()
            for items in itertools.islice(groups, self.workers):
                pending.add(executor.submit(fetch, items))
            while pending:
                ready, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                notify()
                if not ready:
                    continue
                future = next(iter(ready))
                del ready
                pending.remove(future)
                result = future.result()
                # Refill before storage consumes a group. Network threads
                # stay busy while the caller verifies and appends to CASC.
                items = next(groups, None)
                if items is not None:
                    pending.add(executor.submit(fetch, items))
                for item in result:
                    notify()
                    yield item
                del result, future
            notify()
        finally:
            stopped.set()
            for future in pending:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
            groups.close()
