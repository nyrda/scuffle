"""Transport checks use a local HTTP server with explicit HTTP test opt-in."""
from contextlib import contextmanager
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import os
import unittest
from unittest import mock

from waystone.ngdp.cdn import (CDNClient, CDNError, HTTPStatusError, IntegrityError,
                            parse_config, parse_index, parse_psv)
from waystone.ngdp.models import DownloadEntry, Location

FIXTURES = Path(__file__).parent / "fixtures"


def blte(payload):
    data = b"BLTE\0\0\0\0N" + payload
    return hashlib.md5(data).digest(), data


def index_file(entries, offsets=4):
    """Small protocol fixture writer; no parser implementation is reused."""
    per_page = 4096 // (20 + offsets)
    pages = []
    last_keys = []
    for start in range(0, len(entries), per_page):
        page = bytearray()
        for key, size, offset in entries[start:start + per_page]:
            page += key + size.to_bytes(4, "big") + offset.to_bytes(offsets, "big")
        pages.append(bytes(page).ljust(4096, b"\0"))
        last_keys.append(entries[min(start + per_page, len(entries)) - 1][0])
    page_hashes = [hashlib.md5(page).digest()[:8] for page in pages]
    toc = b"".join(last_keys + page_hashes)
    meta = bytes([1, 0, 0, 4, offsets, 4, 16, 8]) + struct.pack("<I", len(entries))
    footer = hashlib.md5(toc).digest()[:8] + meta + hashlib.md5(meta + bytes(8)).digest()[:8]
    data = b"".join(pages) + toc + footer
    key = hashlib.md5(data[-28:]).hexdigest()
    return key, data


@contextmanager
def server(routes):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *_):
            pass
        def do_GET(self):
            seen.append((self.path, dict(self.headers)))
            route = routes.get(self.path)
            if callable(route):
                route(self)
                return
            if route is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            data, mode = route if isinstance(route, tuple) else (route, "normal")
            value = self.headers.get("Range")
            if value and mode != "ignore":
                start, end = map(int, value[6:].split("-"))
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start + (1 if mode == 'wrong' else 0)}-{end}/{len(data)}")
                payload = data[start:end + 1]
            else:
                self.send_response(200)
                payload = data
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", seen
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


class CDNTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = Path(self.temp.name)
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.temp.cleanup()

    def client(self, root, **kwargs):
        client = CDNClient(self.cache, "eu", "wowf", allow_http=True, retries=2, backoff=0,
                           timeout=2, **kwargs)
        client.set_cdn({"Servers": root, "Path": "tpr/wow", "ConfigPath": "tpr/configs/data"})
        self.clients.append(client)
        return client

    def test_real_archive_capture_and_corruption(self):
        key = json.loads((FIXTURES / "cdn-archive-choice.json").read_text())["key"]
        data = (FIXTURES / "cdn-archive.index").read_bytes()
        entries = list(parse_index(data, key))
        self.assertGreater(len(entries), 100)
        self.assertTrue(all(loc.archive == key for _, loc in entries))
        for at in (0, 4096, len(data) - 40, len(data) - 29, len(data) - 1):
            corrupt = bytearray(data)
            corrupt[at] ^= 1
            with self.subTest(at=at), self.assertRaises(IntegrityError):
                list(parse_index(corrupt, key))
        with self.assertRaises(IntegrityError):
            list(parse_index(data[:-1], key))

    def test_group_ordinals_use_advertised_order(self):
        ekey = bytes.fromhex("11" * 16)
        archives = ["ff" * 16, "01" * 16]
        key, data = index_file([(ekey, 27, (1 << 32) | 123)], offsets=6)
        self.assertEqual(list(parse_index(data, key, archives=archives)),
                         [(ekey, Location(archives[1], 123, 27))])
        with self.assertRaises(IntegrityError):
            list(parse_index(data, key, archives=archives[:1]))
        with self.assertRaises(IntegrityError):
            list(parse_index(data, key))

    def test_loose_index(self):
        ekey = bytes.fromhex("11" * 16)
        key, data = index_file([(ekey, 27, 0)], offsets=0)
        self.assertEqual(list(parse_index(data, key)), [(ekey, Location("", 0, 27))])

    def test_psv_and_config_reject_malformed_records(self):
        self.assertEqual(parse_psv(b"Name!STRING:0|Value!STRING:0\n## comment\neu|foo\n"),
                         [{"Name": "eu", "Value": "foo"}])
        with self.assertRaises(IntegrityError):
            parse_psv(b"A|B\na\n")
        with self.assertRaises(IntegrityError):
            parse_config(b"x=1\nx=2\n")

    def test_https_and_anonymous_origin_required(self):
        client = CDNClient(self.cache, "eu", "wowf")
        for root in ("http://example.com", "https://user:pass@example.com", "https://example.com/#secret"):
            with self.subTest(root=root), self.assertRaises(ValueError):
                client._url(root)
        with self.assertRaises(ValueError):
            CDNClient(self.cache, "eu", "wowf", allow_http=True)._url("http://example.com")
        client.close()

    def test_discovery_configs_product_path_and_anonymity(self):
        config = b"# public\nhello = world\n"
        key = hashlib.md5(config).hexdigest()
        routes = {}
        with server(routes) as (root, seen):
            routes["/versions"] = f"Region!STRING:0|BuildConfig!HEX:16|CDNConfig!HEX:16|ProductConfig!HEX:16\neu|{key}|{key}|{key}\n".encode()
            routes["/cdns"] = f"Name!STRING:0|Path!STRING:0|Servers!STRING:0|ConfigPath!STRING:0\neu|tpr/wow|{root}/?fallback=1|tpr/configs/data\n".encode()
            routes[f"/tpr/wow/config/{key[:2]}/{key[2:4]}/{key}"] = config
            routes[f"/tpr/configs/data/{key[:2]}/{key[2:4]}/{key}"] = config
            client = CDNClient(self.cache, "eu", "wowf", allow_http=True, metadata_base=root)
            self.clients.append(client)
            version, cdn = client.discover()
            self.assertEqual(version["BuildConfig"], key)
            self.assertEqual(client.get_config(key).read_bytes(), config)
            # Product config has a separate routing prefix, even when keyed alike.
            client.get_config(key).unlink()
            self.assertEqual(client.get_product_config(key).read_bytes(), config)
            for path, headers in seen:
                self.assertNotIn("Authorization", headers)
                self.assertNotIn("Cookie", headers)
                self.assertNotIn("?", path)

    def test_runtime_keyring_prefers_advertised_product_key(self):
        key = "ab" * 16
        with server({}) as (root, seen):
            client = self.client(root)
            client.version = {"Region": "eu", "KeyRing": key.upper()}
            original = client.version
            self.assertEqual(client.runtime_keyring(), key)
            self.assertIs(client.version, original)
            self.assertEqual(client.product, "wowf")
            self.assertEqual(seen, [])

    def test_runtime_keyring_falls_back_with_same_region_and_preserves_state(self):
        key = "ab" * 16
        routes = {"/fixture/v2/products/wow/versions":
                  f"Region!STRING:0|KeyRing!HEX:16\nus|{'cd' * 16}\neu|{key}\n".encode()}
        with server(routes) as (root, seen):
            client = self.client(root, metadata_base=root + "/fixture/v2/products/wowf")
            client.version = {"Region": "eu", "BuildConfig": "11" * 16, "KeyRing": ""}
            version, cdn = client.version, client.cdn
            mirrors, config_mirrors = client.mirrors[:], client.config_mirrors[:]
            self.assertEqual(client.runtime_keyring(), key)
            self.assertIs(client.version, version)
            self.assertIs(client.cdn, cdn)
            self.assertEqual(client.product, "wowf")
            self.assertEqual(client.mirrors, mirrors)
            self.assertEqual(client.config_mirrors, config_mirrors)
            self.assertEqual([path for path, _ in seen], ["/fixture/v2/products/wow/versions"])
            self.assertNotIn("Authorization", seen[0][1])
            self.assertNotIn("Cookie", seen[0][1])

    def test_runtime_keyring_preserves_fixture_prefix_and_allows_empty_key(self):
        with server({"/fixture/wow/versions": b"Region!STRING:0|KeyRing!HEX:16\neu|\n"}) as (root, seen):
            client = self.client(root, metadata_base=root + "/fixture")
            client.version = {"Region": "eu", "KeyRing": ""}
            self.assertIsNone(client.runtime_keyring())
            self.assertEqual(seen[0][0], "/fixture/wow/versions")

    def test_runtime_keyring_rejects_invalid_hashes_and_wrong_regions(self):
        routes = {}
        with server(routes) as (root, _):
            client = self.client(root, metadata_base=root)
            client.version = {"Region": "eu", "KeyRing": "invalid"}
            with self.assertRaises(ValueError):
                client.runtime_keyring()
            client.version = {"Region": "us", "KeyRing": "ab" * 16}
            with self.assertRaises(IntegrityError):
                client.runtime_keyring()
            client.version = {"Region": "eu", "KeyRing": ""}
            for rows, error in (("eu|invalid", ValueError),
                                ("us|" + "ab" * 16, IntegrityError),
                                ("eu|" + "ab" * 16 + "\neu|" + "cd" * 16, IntegrityError)):
                with self.subTest(rows=rows):
                    routes["/wow/versions"] = ("Region!STRING:0|KeyRing!HEX:16\n" + rows + "\n").encode()
                    with self.assertRaises(error):
                        client.runtime_keyring()

    def test_config_checksum_retries_mirror(self):
        good = b"build = anonymous\n"
        key = hashlib.md5(good).hexdigest()
        path = f"/tpr/wow/config/{key[:2]}/{key[2:4]}/{key}"
        with server({path: b"wrong"}) as (root1, _), server({path: good}) as (root2, seen):
            client = self.client(root1 + " " + root2)
            self.assertEqual(client.get_config(key).read_bytes(), good)
            self.assertEqual(len(seen), 1)

    def test_exact_ranges_and_ignored_huge_response(self):
        archive = "12" * 16
        path = f"/tpr/wow/data/12/12/{archive}"
        routes = {path: b"0123456789abcdef"}
        with server(routes) as (root, seen):
            client = self.client(root)
            self.assertEqual(client.get_range(Location(archive, 4, 5)), b"45678")
            self.assertEqual(seen[-1][1]["Range"], "bytes=4-8")
            routes[path] = (b"0123456789abcdef", "wrong")
            with self.assertRaises(IntegrityError):
                client.get_range(Location(archive, 4, 5))
            def huge(handler):
                handler.send_response(200)
                handler.send_header("Content-Length", "5000000000")
                handler.end_headers()
            routes[path] = huge
            with self.assertRaisesRegex(IntegrityError, "ignored archive Range"):
                client.get_range(Location(archive, 4, 5))

    def test_short_range_rejected(self):
        archive = "12" * 16
        path = f"/tpr/wow/data/12/12/{archive}"
        def short(handler):
            handler.send_response(206)
            handler.send_header("Content-Range", "bytes 4-8/16")
            handler.send_header("Content-Length", "5")
            handler.end_headers()
            handler.wfile.write(b"45")
            handler.wfile.flush()
            handler.connection.shutdown(socket.SHUT_RDWR)
            handler.connection.close()
        with server({path: short}) as (root, _):
            with self.assertRaises(http.client.IncompleteRead):
                self.client(root).get_range(Location(archive, 4, 5))

    def test_resume_loose_and_restart_if_range_ignored(self):
        ekey, data = blte(b"resume me" * 100)
        key = ekey.hex()
        path = f"/tpr/wow/data/{key[:2]}/{key[2:4]}/{key}"
        routes = {path: data}
        with server(routes) as (root, seen):
            client = self.client(root)
            destination = client._object_path(ekey)
            destination.parent.mkdir(parents=True)
            destination.with_name(key + ".part").write_bytes(data[:90])
            self.assertEqual(client.get_object(ekey, len(data)).read_bytes(), data)
            self.assertEqual(seen[-1][1]["Range"], f"bytes=90-{len(data) - 1}")
            destination.unlink()
            destination.with_name(key + ".part").write_bytes(data[:40])
            routes[path] = (data, "ignore")
            self.assertEqual(client.get_object(ekey, len(data)).read_bytes(), data)
            self.assertFalse(destination.with_name(key + ".part").exists())

    def test_truncated_loose_resumes_on_retry(self):
        ekey, data = blte(b"resume interrupted transfer" * 100)
        key = ekey.hex()
        path = f"/tpr/wow/data/{key[:2]}/{key[2:4]}/{key}"
        calls = 0
        def interrupted(handler):
            nonlocal calls
            calls += 1
            if calls == 1:
                handler.send_response(200)
                handler.send_header("Content-Length", str(len(data)))
                handler.end_headers()
                handler.wfile.write(data[:100])
                handler.wfile.flush()
                handler.connection.shutdown(socket.SHUT_RDWR)
                handler.connection.close()
            else:
                start, end = map(int, handler.headers["Range"][6:].split("-"))
                handler.send_response(206)
                handler.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                handler.send_header("Content-Length", str(len(data) - start))
                handler.end_headers()
                handler.wfile.write(data[start:])
        with server({path: interrupted}) as (root, seen):
            self.assertEqual(self.client(root).get_object(ekey, len(data)).read_bytes(), data)
            self.assertEqual(seen[1][1].get("Range"), f"bytes=100-{len(data) - 1}")

    def test_corrupt_blte_retries_and_never_publishes(self):
        ekey, data = blte(b"authenticated")
        key = ekey.hex()
        path = f"/tpr/wow/data/{key[:2]}/{key[2:4]}/{key}"
        with server({path: data[:-1] + b"!"}) as (root, seen):
            client = self.client(root)
            with self.assertRaises(ValueError):
                client.get_object(ekey, len(data))
            self.assertEqual(len(seen), 2)
            self.assertFalse(client._object_path(ekey).exists())

    def test_group_404_falls_back_and_coalesces_archive_ranges(self):
        objects = [blte(payload) for payload in (b"first", b"second", b"third")]
        archive_data = b"prefix" + b"".join(data for _, data in objects)
        start = len(b"prefix")
        entries = []
        for ekey, data in objects:
            entries.append((ekey, len(data), start))
            start += len(data)
        archive, index = index_file(sorted(entries))
        group = "33" * 16
        routes = {f"/tpr/wow/data/{archive[:2]}/{archive[2:4]}/{archive}.index": index,
                  f"/tpr/wow/data/{archive[:2]}/{archive[2:4]}/{archive}": archive_data}
        with server(routes) as (root, seen):
            client = self.client(root)
            client.configure_archives({"archives": [archive], "archive-group": [group]})
            downloads = [DownloadEntry(ekey, len(data), 0) for ekey, data in objects]
            result = list(client.fetch_many(downloads))
            self.assertEqual({entry.ekey: path.read_bytes() for entry, path in result}, dict(objects))
            range_requests = [headers["Range"] for _, headers in seen if "Range" in headers]
            self.assertEqual(range_requests, [f"bytes=6-{len(archive_data) - 1}"])
            count = len(seen)
            # SQLite and validated metadata cache permit a second client to resume.
            second = self.client(root)
            second.configure_archives({"archives": [archive], "archive-group": [group]})
            second.prepare_indexes()
            self.assertEqual(second.locate(objects[0][0]).archive, archive)
            self.assertEqual(len(seen), count + 1)  # only missing remote group is retried

    def test_optional_group_403_falls_back_but_required_index_403_fails(self):
        ekey, encoded = blte(b"only optional group failures may be skipped")
        archive, index = index_file([(ekey, len(encoded), 0)])
        group = "33" * 16
        group_path = f"/tpr/wow/data/33/33/{group}.index"
        index_path = f"/tpr/wow/data/{archive[:2]}/{archive[2:4]}/{archive}.index"
        def forbidden(handler):
            handler.send_response(403)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
        routes = {group_path: forbidden, index_path: index}
        with server(routes) as (root, seen):
            client = self.client(root)
            client.configure_archives({"archives": [archive], "archive-group": [group]})
            client.prepare_indexes()
            self.assertEqual(client.locate(ekey), Location(archive, 0, len(encoded)))
            self.assertTrue(any(path == group_path for path, _ in seen))
            # A required archive index remains a hard failure, even when its
            # optional group optimization also returns 403.
            routes[index_path] = forbidden
            failed = CDNClient(self.cache / "failed", "eu", "wowf", allow_http=True, retries=1)
            self.clients.append(failed)
            failed.set_cdn({"Servers": root, "Path": "tpr/wow"})
            failed.configure_archives({"archives": [archive], "archive-group": [group]})
            with self.assertRaises(HTTPStatusError) as caught:
                failed.prepare_indexes()
            self.assertEqual(caught.exception.status, 403)

    def test_required_asset_403_is_not_ignored(self):
        ekey, encoded = blte(b"required asset")
        key = ekey.hex()
        path = f"/tpr/wow/data/{key[:2]}/{key[2:4]}/{key}"
        def forbidden(handler):
            handler.send_response(403)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
        with server({path: forbidden}) as (root, _):
            client = self.client(root)
            client.configure_archives({"archives": []})
            with self.assertRaises(HTTPStatusError) as caught:
                client.get_object(ekey, len(encoded))
            self.assertEqual(caught.exception.status, 403)
            self.assertFalse(client._object_path(ekey).exists())

    def test_seeded_group_is_verified_copied_and_used_without_network(self):
        ekey = bytes.fromhex("11" * 16)
        archives = ["ff" * 16, "01" * 16]
        key, data = index_file([(ekey, 27, (1 << 32) | 123)], offsets=6)
        source = self.cache / "official-group.index"
        source.write_bytes(data)
        original = source.stat()
        with server({}) as (root, seen):
            client = self.client(root)
            client.configure_archives({"archives": archives, "archive-group": [key]})
            client.seed_index(key, source, group=True)
            copied = self.cache / "indexes" / (key + ".index")
            self.assertFalse(copied.is_symlink())
            self.assertNotEqual(copied.stat().st_ino, original.st_ino)
            self.assertEqual(copied.read_bytes(), data)
            self.assertEqual(source.read_bytes(), data)
            self.assertEqual(source.stat().st_mtime_ns, original.st_mtime_ns)
            client.prepare_indexes()
            self.assertEqual(client.locate(ekey), Location(archives[1], 123, 27))
            self.assertEqual(seen, [])
            copied.write_bytes(b"cache changes cannot modify the installed seed")
            self.assertEqual(source.read_bytes(), data)

    def test_seed_rejects_corruption_wrong_type_and_symlink_source(self):
        ekey = bytes.fromhex("11" * 16)
        key, data = index_file([(ekey, 27, 123)])
        source = self.cache / "original.index"
        source.write_bytes(data)
        client = CDNClient(self.cache, "eu", "wowf")
        self.clients.append(client)
        client.configure_archives({"archives": [key]})
        corrupt = self.cache / "corrupt.index"
        damaged = bytearray(data)
        damaged[0] ^= 1
        corrupt.write_bytes(damaged)
        destination = self.cache / "indexes" / (key + ".index")
        with self.assertRaises(IntegrityError):
            client.seed_index(key, corrupt)
        self.assertFalse(destination.exists())
        with self.assertRaises(IntegrityError):
            client.seed_index(key, source, group=True)
        self.assertFalse(destination.exists())
        linked = self.cache / "linked.index"
        linked.symlink_to(source)
        with self.assertRaises(OSError):
            client.seed_index(key, linked)
        self.assertFalse(destination.exists())
        self.assertFalse(list((self.cache / "indexes").glob("*.part")))

    def test_seed_does_not_follow_cache_destination_symlinks(self):
        ekey = bytes.fromhex("11" * 16)
        key, data = index_file([(ekey, 27, 123)])
        source = self.cache / "source.index"
        source.write_bytes(data)
        outside = self.cache / "outside"
        outside.mkdir()
        victim = outside / "victim"
        victim.write_bytes(b"leave this alone")
        client = CDNClient(self.cache / "transport", "eu", "wowf")
        self.clients.append(client)
        indices = client.cache / "indexes"
        indices.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            client.seed_index(key, source)
        self.assertFalse((outside / (key + ".index")).exists())
        indices.unlink()
        indices.mkdir()
        destination = indices / (key + ".index")
        destination.symlink_to(victim)
        client.seed_index(key, source)
        self.assertFalse(destination.is_symlink())
        self.assertEqual(destination.read_bytes(), data)
        self.assertEqual(victim.read_bytes(), b"leave this alone")

    def test_bulk_bytes_avoids_transient_object_files(self):
        objects = [blte(payload) for payload in (b"one", b"two")]
        archive_data = b"".join(data for _, data in objects)
        entries = []
        offset = 0
        for ekey, data in objects:
            entries.append((ekey, len(data), offset))
            offset += len(data)
        archive, index = index_file(sorted(entries))
        routes = {f"/tpr/wow/data/{archive[:2]}/{archive[2:4]}/{archive}.index": index,
                  f"/tpr/wow/data/{archive[:2]}/{archive[2:4]}/{archive}": archive_data}
        with server(routes) as (root, seen):
            client = self.client(root)
            client.configure_archives({"archives": [archive]})
            result = list(client.fetch_many([DownloadEntry(key, len(data), 0) for key, data in objects],
                                            as_bytes=True))
            self.assertEqual({entry.ekey: data for entry, data in result}, dict(objects))
            self.assertFalse((self.cache / "objects").exists())
            self.assertEqual(len([headers for _, headers in seen if "Range" in headers]), 1)

    def test_corrupt_index_never_commits_locations(self):
        ekey, data = blte(b"test")
        archive, index = index_file([(ekey, len(data), 0)])
        corrupt = bytearray(index)
        corrupt[0] ^= 1
        path = f"/tpr/wow/data/{archive[:2]}/{archive[2:4]}/{archive}.index"
        with server({path: bytes(corrupt)}) as (root, _):
            client = self.client(root)
            client.configure_archives({"archives": [archive]})
            with self.assertRaises(IntegrityError):
                client.prepare_indexes()
            with client._database() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM objects").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()


class PathBasedSeedTests(CDNTests):
    """The same seed checks through the path-based copy used without dir_fd, as on Windows."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(os, "supports_dir_fd", set())
        patcher.start()
        self.addCleanup(patcher.stop)


for _name in [n for n in dir(CDNTests) if n.startswith("test_") and "seed" not in n]:
    setattr(PathBasedSeedTests, _name, None)
