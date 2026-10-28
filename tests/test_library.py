"""Real updater/store integration with tiny, independently written TACT data.

The synthetic Wow.exe is inert test text. No installed game, prefix, account,
public network endpoint, or actual game executable is used by these tests.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import threading

import pytest

from waystone.ngdp.cdn import CDNClient
from waystone.library import Library, LibraryError as UpdateError, Updater as LibraryUpdater


@contextmanager
def install_lock(root, create=False):
    library = Library(root)
    if create:
        library.create()
    with library.locked():
        yield library


def installed_state(root, product="wowf"):
    return Library(root).state(product)


def game_running(root, product="wowf"):
    return Library(root).running(product)


def Updater(root, client, product="wowf", **kwargs):
    return LibraryUpdater(Library(root), product, client, **kwargs)
from waystone.ngdp.formats import FormatError, parse_psv
from waystone.ngdp.store import CascSource, CascStore, SPAN_HEADER_SIZE, StoreError, bucket_for_key


def md5(value):
    return hashlib.md5(value).digest()


@dataclass(frozen=True)
class Object:
    decoded: bytes
    encoded: bytes
    ckey: bytes
    ekey: bytes


def object_for(payload):
    frame = b"N" + payload
    header = (b"BLTE" + struct.pack(">I", 36) + b"\x0f\0\0\x01"
              + struct.pack(">II", len(frame), len(payload)) + md5(frame))
    return Object(payload, header + frame, md5(payload), md5(header))


def tags(count, masks=None, *, region="eu", locale="enUS"):
    masks = masks or {}
    width = (count + 7) // 8
    all_entries = ((1 << count) - 1) << (width * 8 - count)
    names = [("Windows", 1), ("x86_64", 2), (region.upper(), 3), (locale, 4),
             ("speech", 5), ("text", 5)]
    return b"".join(name.encode() + b"\0" + kind.to_bytes(2, "big")
                    + masks.get(name, all_entries).to_bytes(width, "big")
                    for name, kind in names)


def install_manifest(files, *, region="eu", locale="enUS"):
    rows = b"".join(name.encode() + b"\0" + obj.ckey
                    + len(obj.decoded).to_bytes(4, "big") for name, obj in files)
    return (b"IN\x01\x10" + (6).to_bytes(2, "big")
            + len(files).to_bytes(4, "big") + tags(len(files), region=region, locale=locale) + rows)


def download_manifest(objects, *, region="eu", locale="enUS"):
    # DL v1, signed priorities, and all supported selection dimensions.
    rows = b"".join(obj.ekey + len(obj.encoded).to_bytes(5, "big")
                    + priority.to_bytes(1, "big", signed=True)
                    for obj, priority in objects)
    return (b"DL\x01\x10\0" + len(objects).to_bytes(4, "big")
            + (6).to_bytes(2, "big") + rows + tags(len(objects), region=region, locale=locale))


def encoding_manifest(objects):
    # EN has independent sorted CKey and EKey pages, real page MD5s, and one
    # encoding specification. It does not need to contain a mapping to itself.
    unique = {obj.ckey: obj for obj in objects}
    c_rows = b"".join(b"\x01" + len(obj.decoded).to_bytes(5, "big")
                      + obj.ckey + obj.ekey for obj in sorted(unique.values(), key=lambda o: o.ckey))
    e_rows = b"".join(obj.ekey + bytes(4) + len(obj.encoded).to_bytes(5, "big")
                      for obj in sorted(unique.values(), key=lambda o: o.ekey))
    assert len(c_rows) < 1024 and len(e_rows) < 1024
    cp, ep = c_rows.ljust(1024, b"\0"), e_rows.ljust(1024, b"\0")
    specs = b"n\0"
    head = b"EN\x01\x10\x10" + struct.pack(">HHIIBI", 1, 1, 1, 1, 0, len(specs))
    return head + specs + cp[6:22] + md5(cp) + cp + ep[:16] + md5(ep) + ep


class SyntheticBuild:
    def __init__(self, version="0.0.1.1", *, executable_path="Wow.exe", changed=b"version one",
                 region="eu", locale="enUS", shared_keyring=None, version_keyring=None,
                 product="wowf", folder="_forever_"):
        self.common = object_for(b"unchanged content kept across builds")
        self.changed = object_for(changed)
        self.wow = object_for(b"MZ\0inert synthetic executable; never run\n" + changed)
        self.files = [(executable_path, self.wow), ("Interface/client.txt", self.changed)]
        # One legacy WoW root block, with a file ID, CKey, and unnamed hash.
        root_bytes = (struct.pack("<III", 1, 0, 0xffffffff) + struct.pack("<I", 1)
                      + self.common.ckey + bytes(8))
        self.root = object_for(root_bytes)
        self.install = object_for(install_manifest(self.files, region=region, locale=locale))
        self.download = object_for(download_manifest([(self.common, 0), (self.changed, 1), (self.wow, 2)],
                                                     region=region, locale=locale))
        self.encoding = object_for(encoding_manifest([self.common, self.changed, self.wow, self.root,
                                                     self.install, self.download]))
        self.objects = {obj.ekey: obj.encoded for obj in [self.common, self.changed, self.wow,
                         self.root, self.install, self.download, self.encoding]}
        lines = [f"build-uid = wowf", f"build-name = WOW-{version}", f"root = {self.root.ckey.hex()}"]
        for name in ["encoding", "install", "download"]:
            obj = getattr(self, name)
            lines += [f"{name} = {obj.ckey.hex()} {obj.ekey.hex()}",
                      f"{name}-size = {len(obj.decoded)} {len(obj.encoded)}"]
        build = ("\n".join(lines) + "\n").encode()
        cdn = b"archives =\n"
        product = json.dumps({"all": {"config": {"product": product, "update_method": "ngdp",
                                                   "supported_locales": [locale],
                                                   "shared_container_default_subfolder": folder}}}).encode()
        self.configs = {md5(data).hex(): data for data in [build, cdn, product]}
        for keyring in [version_keyring, shared_keyring]:
            if keyring is not None:
                self.configs[md5(keyring).hex()] = keyring
        self.shared_keyring = md5(shared_keyring).hex() if shared_keyring is not None else None
        self.version = {"Region": region, "VersionsName": version, "BuildConfig": md5(build).hex(),
                        "CDNConfig": md5(cdn).hex(), "ProductConfig": md5(product).hex(),
                        "KeyRing": md5(version_keyring).hex() if version_keyring is not None else ""}
        self.cdn = {"Name": region, "Path": "tpr/wow", "Hosts": "fixture.invalid",
                    "Servers": "https://fixture.invalid", "ConfigPath": "tpr/configs/data"}


class FixtureCDN:
    """Only transport is replaced; formats, hashes, plans, and CASC are real."""
    def __init__(self, build, cache, *, interrupt_after=None, payloads=None):
        self.build, self.cache = build, Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.interrupt_after = interrupt_after
        self.payloads = payloads or {}
        self.fetched_keys, self.metadata_keys = [], []
        self.config_keys = []
        self.archive_config = None

    def discover(self):
        return self.build.version.copy(), self.build.cdn.copy()

    def runtime_keyring(self):
        return self.build.version.get("KeyRing") or self.build.shared_keyring or None

    def get_config(self, key):
        self.config_keys.append(key)
        result = self.cache / key
        result.write_bytes(self.build.configs[key])
        assert md5(result.read_bytes()).hex() == key
        return result

    get_product_config = get_config

    def configure_archives(self, config):
        self.archive_config = config

    def get_object(self, key, size=None):
        self.metadata_keys.append(key)
        result = self.cache / key.hex()
        result.write_bytes(self.payloads.get(key, self.build.objects[key]))
        return result

    def prepare_indexes(self, progress=None):
        if progress:
            progress(0, 0)

    def fetch_many(self, entries, *, as_bytes=False, **kwargs):
        for entry in entries:
            if self.interrupt_after is not None and len(self.fetched_keys) == self.interrupt_after:
                raise KeyboardInterrupt
            self.fetched_keys.append(entry.ekey)
            payload = self.payloads.get(entry.ekey, self.build.objects[entry.ekey])
            if as_bytes:
                yield entry, payload
            else:
                result = self.cache / entry.ekey.hex()
                result.write_bytes(payload)
                yield entry, result

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@contextmanager
def updater_for(root, client, **kwargs):
    with install_lock(root, create=True):
        updater = Updater(root, client, **kwargs)
        try:
            yield updater
        finally:
            updater.close()


def run_build(root, client, **kwargs):
    with updater_for(root, client, **kwargs) as updater:
        return updater.run()


def corrupt_stored_object(root, ekey):
    """Flip a body byte without damaging the durable span or BLTE table."""
    with CascStore(root) as store:
        location = dict(store.iter_entries())[ekey]
        segment = store.data_dir / location.archive
    with segment.open("r+b") as stream:
        stream.seek(location.offset + SPAN_HEADER_SIZE + location.size - 1)
        last = stream.read(1)
        assert last
        stream.seek(-1, 1)
        stream.write(bytes([last[0] ^ 1]))
    return location, segment.read_bytes()


def damage_activation_artifact(root, build, artifact, damage):
    if artifact == "build_info":
        path = root / ".build.info"
    elif artifact == "flavor_info":
        path = root / "_forever_/.flavor.info"
    elif artifact == "published_index":
        bucket = f"{bucket_for_key(build.wow.ekey):02x}"
        path, = (root / "Data/data").glob(bucket + "*.idx")
    else:
        key = build.version["BuildConfig" if artifact == "build_config" else "CDNConfig"]
        path = root / "Data/config" / key[:2] / key[2:4] / key
    canonical = path.read_bytes()
    if damage == "missing":
        path.unlink()
    else:
        corrupted = bytearray(canonical)
        corrupted[4 if artifact == "published_index" else -1] ^= 1
        path.write_bytes(corrupted)
    return path, canonical


@contextmanager
def public_fixture_server(build):
    """Serve public NGDP routes locally and retain request headers for review."""
    requests = []
    routes = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            payload = routes.get(self.path)
            self.send_response(200 if payload is not None else 404)
            self.send_header("Content-Length", str(len(payload) if payload is not None else 0))
            self.end_headers()
            if payload is not None:
                self.wfile.write(payload)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    origin = f"http://127.0.0.1:{httpd.server_port}"
    cdn = build.cdn | {"Servers": origin}
    for path, record in [("/versions", build.version), ("/cdns", cdn)]:
        routes[path] = ("|".join(record) + "\n" + "|".join(record.values()) + "\n").encode()
    routes["/wow/versions"] = ("Region!STRING:0|KeyRing!HEX:16\n"
                               + build.version["Region"] + "|" + (build.shared_keyring or "") + "\n").encode()
    for key, payload in build.configs.items():
        for kind in ["tpr/wow/config", "tpr/configs/data"]:
            routes[f"/{kind}/{key[:2]}/{key[2:4]}/{key}"] = payload
    for key, payload in build.objects.items():
        value = key.hex()
        routes[f"/tpr/wow/data/{value[:2]}/{value[2:4]}/{value}"] = payload
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield origin, requests
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_empty_full_install_uses_real_anonymous_cdn_and_casc(tmp_path):
    build = SyntheticBuild()
    root = tmp_path / "new install"
    events = []
    with public_fixture_server(build) as (origin, requests):
        with CDNClient(tmp_path / "cache", "eu", "wowf", metadata_base=origin, allow_http=True,
                       workers=2, retries=1, timeout=2) as client:
            result = run_build(root, client, progress=events.append)
    assert result["complete"] is True
    assert result["downloaded_objects"] == len(build.objects) - 3
    assert installed_state(root)["build_config"] == build.version["BuildConfig"]
    assert (root / "_forever_/Wow.exe").read_bytes() == build.wow.decoded
    assert (root / "_forever_/INTERFACE/client.txt").read_bytes() == build.changed.decoded
    assert (root / "_forever_/Wow.exe").stat().st_mode & 0o111
    assert not (root / ".waystone/clients/wowf/pending.json").exists()
    assert events[-1]["stage"] == "complete"
    row, = parse_psv((root / ".build.info").read_bytes())
    assert row["Build Key"] == build.version["BuildConfig"] and row["Product"] == "wowf"
    assert "Windows x86_64 EU? enUS speech?" in row["Tags"]
    with CascSource(root) as source:
        for key, payload in build.objects.items():
            assert source.read(key) == payload
    assert requests
    for path, headers in requests:
        assert path.startswith(("/versions", "/cdns", "/wow/versions", "/tpr/"))
        assert not {"authorization", "cookie", "proxy-authorization"} & {key.lower() for key in headers}


def test_same_build_reuses_all_objects_and_update_preserves_common_location(tmp_path):
    root, build = tmp_path / "install", SyntheticBuild()
    run_build(root, FixtureCDN(build, tmp_path / "first"))
    user_config = root / "_forever_/WTF/Config.wtf"
    user_config.parent.mkdir(exist_ok=True)
    user_config.write_bytes(b'SET readTOS "1"\nSET gxResolution "2560x1440"\n')
    user_bytes = user_config.read_bytes()
    with CascStore(root) as store:
        old_locations = dict(store.iter_entries())
        old_count = store.count
    repeated = FixtureCDN(build, tmp_path / "repeat")
    result = run_build(root, repeated)
    assert result["downloaded_objects"] == 0
    assert not repeated.metadata_keys and not repeated.fetched_keys
    changed = SyntheticBuild("0.0.1.2", changed=b"version two")
    update_client = FixtureCDN(changed, tmp_path / "update")
    result = run_build(root, update_client)
    assert result["version"] == "0.0.1.2" and result["complete"]
    assert user_config.read_bytes() == user_bytes
    assert changed.common.ekey not in update_client.fetched_keys + update_client.metadata_keys
    assert (root / "_forever_/Wow.exe").read_bytes() == changed.wow.decoded
    assert (root / "_forever_/INTERFACE/client.txt").read_bytes() == changed.changed.decoded
    with CascStore(root) as store:
        locations = dict(store.iter_entries())
        assert all(locations[key] == location for key, location in old_locations.items())
        assert store.count == old_count + len(changed.objects.keys() - build.objects.keys())
        assert store.verify(changed.common.ekey)
    with updater_for(root, FixtureCDN(changed, tmp_path / "verify")) as updater:
        assert updater.verify()["ok"] is True


def test_partial_transfer_resumes_without_refetching_stored_objects(tmp_path):
    root, build = tmp_path / "install", SyntheticBuild()
    interrupted = FixtureCDN(build, tmp_path / "first", interrupt_after=1)
    with pytest.raises(KeyboardInterrupt):
        run_build(root, interrupted)
    assert len(interrupted.fetched_keys) == 1
    assert installed_state(root) is None
    assert not (root / ".build.info").exists()
    assert not (root / "_forever_/Wow.exe").exists()
    assert json.loads((root / ".waystone/clients/wowf/pending.json").read_text())["phase"] == "downloading"
    with CascStore(root) as store:
        retained = dict(store.iter_entries())
        assert len(retained) == 4
    resumed = FixtureCDN(build, tmp_path / "second")
    result = run_build(root, resumed)
    assert result["downloaded_objects"] == len(build.objects) - len(retained)
    assert not set(retained) & set(resumed.fetched_keys + resumed.metadata_keys)
    assert result["complete"]
    with CascStore(root) as store:
        assert all(dict(store.iter_entries())[key] == location for key, location in retained.items())


@pytest.mark.parametrize("mode", ["truncated", "corrupted"])
def test_bad_update_object_cannot_activate_over_previous_install(tmp_path, mode):
    root, old = tmp_path / "install", SyntheticBuild()
    run_build(root, FixtureCDN(old, tmp_path / "old"))
    before_state = (root / ".waystone/clients/wowf/state.json").read_bytes()
    before_info = (root / ".build.info").read_bytes()
    build = SyntheticBuild("0.0.1.2", changed=b"new bytes")
    payload = build.wow.encoded
    bad = payload[:-1] if mode == "truncated" else payload[:-1] + bytes([payload[-1] ^ 1])
    client = FixtureCDN(build, tmp_path / "bad", payloads={build.wow.ekey: bad})
    with pytest.raises((StoreError, FormatError)):
        run_build(root, client)
    assert (root / ".waystone/clients/wowf/state.json").read_bytes() == before_state
    assert (root / ".build.info").read_bytes() == before_info
    assert (root / "_forever_/Wow.exe").read_bytes() == old.wow.decoded
    with CascStore(root) as store:
        assert not store.has(build.wow.ekey)
    assert run_build(root, FixtureCDN(build, tmp_path / "retry"))["complete"]


@pytest.mark.parametrize("name", ["../escape.exe", "/tmp/escape.exe", "C:\\escape.exe", "nested/../../escape.exe"])
def test_unsafe_install_manifest_path_is_rejected_before_activation(tmp_path, name):
    root, build = tmp_path / "install", SyntheticBuild(executable_path=name)
    with pytest.raises(UpdateError, match="Unsafe installation path"):
        run_build(root, FixtureCDN(build, tmp_path / "cache"))
    assert installed_state(root) is None
    assert not (root / ".build.info").exists()
    assert not (tmp_path / "escape.exe").exists()


@pytest.mark.parametrize("link_name", ["_forever_", "_forever_/INTERFACE"])
def test_activation_refuses_symlink_that_escapes_install_directory(tmp_path, link_name):
    root, build = tmp_path / "install", SyntheticBuild()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "client.txt"
    sentinel.write_bytes(b"user file")
    with updater_for(root, FixtureCDN(build, tmp_path / "cache")) as updater:
        updater.prepare()
        updater.transfer()
        link = root / link_name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside, target_is_directory=True)
        with pytest.raises(UpdateError, match="[Ss]ymbolic|[Ss]ymlink|escapes"):
            updater.activate()
    assert sentinel.read_bytes() == b"user file"
    assert not (outside / "Wow.exe").exists()
    assert installed_state(root) is None


@pytest.mark.parametrize("directory", ["Data", "Data/data", "Data/config", ".waystone",
                                        ".waystone/metadata", ".waystone/clients/wowf/staging", "_forever_"])
def test_managed_directory_symlinks_are_rejected_before_external_writes(tmp_path, directory):
    root, outside = tmp_path / "install", tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "user.txt"
    sentinel.write_bytes(b"external user data")
    with install_lock(root, create=True):
        pass
    link = root / directory
    if link.exists():
        link.rename(tmp_path / "original updater state")
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(UpdateError, match="symbolic link"):
        with install_lock(root):
            pytest.fail("an escaping managed directory was accepted")
    assert list(outside.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"external user data"


def test_install_refuses_to_adopt_a_user_directory(tmp_path):
    root = tmp_path / "existing"
    root.mkdir()
    original = root / "user.txt"
    original.write_bytes(b"must survive")
    with pytest.raises(UpdateError, match="empty folder"):
        with install_lock(root, create=True):
            pytest.fail("unowned directory was locked for installation")
    assert list(root.iterdir()) == [original]
    assert original.read_bytes() == b"must survive"


def test_seed_install_reads_verified_objects_without_changing_source(tmp_path):
    build = SyntheticBuild()
    seed = tmp_path / "seed"
    with CascStore(seed) as store:
        for key, payload in build.objects.items():
            store.put(key, payload)
        store.publish()
    before = {str(path.relative_to(seed)): path.read_bytes()
              for path in seed.rglob("*") if path.is_file()}
    client = FixtureCDN(build, tmp_path / "cache")
    root = tmp_path / "fresh"
    result = run_build(root, client, seed_from=seed)
    assert result["complete"] and result["downloaded_objects"] == 0
    assert result["seeded_objects"] == len(build.objects) - 3
    assert not client.metadata_keys and not client.fetched_keys
    after = {str(path.relative_to(seed)): path.read_bytes()
             for path in seed.rglob("*") if path.is_file()}
    assert after == before
    assert (root / "_forever_/Wow.exe").read_bytes() == build.wow.decoded


def test_second_updater_cannot_take_the_install_lock(tmp_path):
    root = tmp_path / "install"
    with install_lock(root, create=True):
        with pytest.raises(UpdateError, match="Another Waystone"):
            with install_lock(root):
                pytest.fail("a second updater acquired the lock")


@pytest.mark.parametrize("damage", ["bad_file", "missing_file", "missing_object", "bad_object"])
def test_verify_reports_damaged_files_and_missing_or_corrupt_content(tmp_path, damage):
    root, build = tmp_path / "install", SyntheticBuild()
    run_build(root, FixtureCDN(build, tmp_path / "cache"))
    executable = root / "_forever_/Wow.exe"
    if damage == "bad_file":
        original = executable.read_bytes()
        executable.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    elif damage == "missing_file":
        executable.unlink()
    elif damage == "missing_object":
        with sqlite3.connect(root / ".waystone/store.sqlite") as db:
            db.execute("DELETE FROM objects WHERE ekey=?", (build.common.ekey,))
    else:
        with CascStore(root) as store:
            location = dict(store.iter_entries())[build.common.ekey]
            segment = store.data_dir / location.archive
        with segment.open("r+b") as stream:
            stream.seek(location.offset + SPAN_HEADER_SIZE + len(build.common.encoded) - 1)
            last = stream.read(1)
            assert last
            stream.seek(-1, 1)
            stream.write(bytes([last[0] ^ 1]))
    with updater_for(root, FixtureCDN(build, tmp_path / "verify")) as updater:
        result = updater.verify()
    assert result["ok"] is False
    assert result["verified_objects"] == len(build.objects)
    if damage.endswith("file"):
        assert any(error.get("path") == "Wow.exe" for error in result["errors"])
    else:
        assert any(error.get("ekey") == build.common.ekey.hex() for error in result["errors"])


@pytest.mark.parametrize("object_name", ["common", "wow", "encoding"])
def test_invalidated_corrupt_object_is_reacquired_and_published_by_updater(tmp_path, object_name):
    root, build = tmp_path / "install", SyntheticBuild()
    run_build(root, FixtureCDN(build, tmp_path / "first"))
    broken = getattr(build, object_name)
    old_location, damaged_bytes = corrupt_stored_object(root, broken.ekey)
    client = FixtureCDN(build, tmp_path / "repair")
    with updater_for(root, client) as updater:
        before = dict(updater.store.iter_entries())
        report = updater.verify()
        assert report["ok"] is False
        assert [error["ekey"] for error in report["errors"]] == [broken.ekey.hex()]
        updater.store.invalidate(broken.ekey)
        assert not updater.store.has(broken.ekey)
        result = updater.run()
        after = dict(updater.store.iter_entries())
        assert after[broken.ekey] != old_location
        assert all(after[key] == location for key, location in before.items() if key != broken.ekey)
        assert updater.store.verify(broken.ekey)
        assert updater.verify()["ok"]
    assert result["complete"] and not (root / ".waystone/clients/wowf/pending.json").exists()
    assert client.metadata_keys + client.fetched_keys == [broken.ekey]
    assert (root / "Data/data" / old_location.archive).read_bytes().startswith(damaged_bytes)
    with CascSource(root) as published:
        assert published.read(broken.ekey) == broken.encoded
        assert published.read(build.common.ekey) == build.common.encoded


@pytest.mark.parametrize("artifact", ["build_info", "build_config", "cdn_config", "published_index", "flavor_info"])
@pytest.mark.parametrize("damage", ["missing", "corrupted"])
def test_verify_detects_damaged_activation_metadata_and_indexes(tmp_path, artifact, damage):
    root, build = tmp_path / "install", SyntheticBuild()
    run_build(root, FixtureCDN(build, tmp_path / "first"))
    path, _ = damage_activation_artifact(root, build, artifact, damage)
    damaged_bytes = path.read_bytes() if path.exists() else None
    with updater_for(root, FixtureCDN(build, tmp_path / "verify")) as updater:
        for deep in [True, False]:
            result = updater.verify(deep=deep)
            assert result["ok"] is False
            assert result["verified_objects"] == len(build.objects)
            if artifact == "published_index":
                assert any("index" in error["error"].lower() for error in result["errors"])
            else:
                assert any(error.get("path") == str(path.relative_to(root)) for error in result["errors"])
    assert (path.read_bytes() if path.exists() else None) == damaged_bytes


def test_game_running_detects_windows_executable_argument_by_process_working_directory(tmp_path):
    root, unrelated = tmp_path / "install", tmp_path / "unrelated"
    game = root / "_forever_"
    game.mkdir(parents=True)
    (unrelated / "_forever_").mkdir(parents=True)
    for folder in (root, unrelated):
        (folder / ".waystone/clients/wowf").mkdir(parents=True)
        (folder / ".waystone/clients/wowf/state.json").write_text('{"folder": "_forever_"}')
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)",
         r"S:\wow-forever-native\_forever_\Wow.exe"],
        cwd=game,
    )
    try:
        assert process.poll() is None
        command = Path(f'/proc/{process.pid}/cmdline')
        try:
            visible = command in Path('/proc').glob('[0-9]*/cmdline')
            matching = b'Wow.exe' in command.read_bytes()
            working_directory = (command.parent / 'cwd').resolve() == game
        except OSError:
            visible = matching = working_directory = False
        if not (visible and matching and working_directory):
            pytest.skip('The build sandbox does not expose the child process through /proc')
        assert game_running(root) is True
        assert game_running(unrelated) is False
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("region, locale", [("eu", "enUS"), ("us", "deDE")])
def test_first_install_sets_selected_region_and_locale_preferences(tmp_path, region, locale):
    root = tmp_path / "install"
    build = SyntheticBuild(region=region, locale=locale)
    result = run_build(root, FixtureCDN(build, tmp_path / "cache"), region=region, locale=locale)
    preferences = root / "_forever_/WTF/Config.wtf"
    assert result["complete"]
    assert preferences.read_text().splitlines() == [
        f'SET portal "{region.upper()}"',
        f'SET textLocale "{locale}"',
        f'SET audioLocale "{locale}"',
    ]
    assert installed_state(root)["region"] == region
    assert installed_state(root)["locale"] == locale


@pytest.mark.parametrize("existing", [b'SET portal "EU"\nSET textLocale "frFR"\nSET gxWindow "1"\n', b""])
def test_first_activation_preserves_preexisting_preferences_exactly(tmp_path, existing):
    root = tmp_path / "install"
    build = SyntheticBuild(region="us", locale="deDE")
    with updater_for(root, FixtureCDN(build, tmp_path / "cache"), region="us", locale="deDE") as updater:
        updater.prepare()
        updater.transfer()
        preferences = root / "_forever_/WTF/Config.wtf"
        preferences.parent.mkdir(parents=True, exist_ok=True)
        preferences.write_bytes(existing)
        original_mtime = preferences.stat().st_mtime_ns
        assert updater.activate()["complete"]
        assert preferences.read_bytes() == existing
        assert preferences.stat().st_mtime_ns == original_mtime


@pytest.mark.parametrize("source", ["version", "shared", "version_precedence"])
def test_effective_runtime_keyring_is_copied_selected_and_verified(tmp_path, source):
    first = b"keyring = 000102030405060708090a0b0c0d0e0f\n"
    second = b"keyring = 101112131415161718191a1b1c1d1e1f\n"
    build = SyntheticBuild(
        version_keyring=first if source != "shared" else None,
        shared_keyring=second if source != "version" else None,
    )
    expected = second if source == "shared" else first
    key = md5(expected).hex()
    root = tmp_path / "install"
    client = FixtureCDN(build, tmp_path / "cache")
    result = run_build(root, client)
    assert result["keyring"] == key and installed_state(root)["keyring"] == key
    assert (root / "Data/config" / key[:2] / key[2:4] / key).read_bytes() == expected
    assert key in client.config_keys
    row, = parse_psv((root / ".build.info").read_bytes())
    assert row["KeyRing"] == key
    if source == "version_precedence":
        assert build.shared_keyring != key
        assert build.shared_keyring not in client.config_keys
    with updater_for(root, FixtureCDN(build, tmp_path / "verify")) as updater:
        assert updater.verify()["ok"]
    assert result["objects"] == len(build.objects)


def test_real_cdn_shared_keyring_install_uses_runtime_metadata_without_changing_product(tmp_path):
    keyring = b"keyring = 000102030405060708090a0b0c0d0e0f\n"
    build = SyntheticBuild(shared_keyring=keyring)
    root = tmp_path / "install"
    with public_fixture_server(build) as (origin, requests):
        with CDNClient(tmp_path / "cache", "eu", "wowf", metadata_base=origin, allow_http=True,
                       workers=2, retries=1, timeout=2) as client:
            result = run_build(root, client)
            assert client.product == "wowf" and client.version == build.version
    key = md5(keyring).hex()
    assert result["keyring"] == key and result["version_record"]["KeyRing"] == ""
    assert (root / "Data/config" / key[:2] / key[2:4] / key).read_bytes() == keyring
    row, = parse_psv((root / ".build.info").read_bytes())
    assert row["KeyRing"] == key
    assert any(path == "/wow/versions" for path, _ in requests)
    assert any(path == f"/tpr/wow/config/{key[:2]}/{key[2:4]}/{key}" for path, _ in requests)
    for _, headers in requests:
        assert not {"authorization", "cookie", "proxy-authorization"} & {name.lower() for name in headers}
    with updater_for(root, FixtureCDN(build, tmp_path / "verify")) as updater:
        assert updater.verify()["ok"]


def test_two_clients_share_one_store_and_each_gets_its_folder_and_build_row(tmp_path):
    root = tmp_path / "install"
    forever = SyntheticBuild(changed=b"forever content")
    retail = SyntheticBuild("0.0.2.2", changed=b"retail content", product="wow", folder="_retail_")
    run_build(root, FixtureCDN(forever, tmp_path / "forever-cache"))
    retail_cdn = FixtureCDN(retail, tmp_path / "retail-cache")
    run_build(root, retail_cdn, product="wow")

    # Content both builds need is stored once and not fetched again.
    assert retail.common.ekey == forever.common.ekey
    assert forever.common.ekey not in retail_cdn.fetched_keys
    assert (root / "_forever_/Wow.exe").read_bytes() == forever.wow.decoded
    assert (root / "_retail_/Wow.exe").read_bytes() == retail.wow.decoded
    assert (root / "_retail_/.flavor.info").read_bytes() == b"Product Flavor!STRING:0\nwow\n"
    rows = [line.split("|") for line in (root / ".build.info").read_text().splitlines()[1:]]
    assert sorted((row[-1], row[2]) for row in rows) == sorted(
        [("wow", retail.version["BuildConfig"]), ("wowf", forever.version["BuildConfig"])])
    assert Library(root).installed() == ["wow", "wowf"]
    for product, build in [("wowf", forever), ("wow", retail)]:
        with updater_for(root, FixtureCDN(build, tmp_path / f"verify-{product}"), product=product) as updater:
            assert updater.verify()["ok"] is True


def test_wowupdater_installation_is_adopted_in_place(tmp_path):
    root, build = tmp_path / "install", SyntheticBuild()
    run_build(root, FixtureCDN(build, tmp_path / "cache"))
    # Recreate the single-client wowupdater layout the real installation uses.
    state_dir, client = root / ".waystone", root / ".waystone/clients/wowf"
    old_state = json.loads((client / "state.json").read_text())
    old_state.pop("folder")
    old_state["executable"] = str(root / "_forever_/Wow.exe")
    for name in ["plan.sqlite", "plan.sqlite-wal", "plan.sqlite-shm"]:
        if (client / name).exists():
            (client / name).rename(state_dir / name)
    (state_dir / "state.json").write_text(json.dumps(old_state))
    (state_dir / "identity.json").write_text(json.dumps({"format": 1, "product": "wowf"}))
    for leftover in [client / "state.json", state_dir / "library.lock"]:
        leftover.unlink(missing_ok=True)
    client.rmdir(); client.parent.rmdir()
    state_dir.rename(root / ".wowupdater")

    library = Library.migrate_wowupdater(root)
    assert library.installed() == ["wowf"] and library.state("wowf")["folder"] == "_forever_"
    assert not (root / ".wowupdater").exists()
    with updater_for(root, FixtureCDN(build, tmp_path / "verify")) as updater:
        assert updater.verify()["ok"] is True


def test_cleanup_forgets_only_content_no_installed_client_needs(tmp_path, monkeypatch):
    import waystone.ngdp.store as store_module
    # Small segments, so the old build's content fills segments of its own.
    monkeypatch.setattr(store_module, "SEGMENT_SIZE", 4096)
    root = tmp_path / "install"
    old = SyntheticBuild(changed=b"forever one " * 8)
    retail = SyntheticBuild("0.0.2.2", changed=b"retail content", product="wow", folder="_retail_")
    new = SyntheticBuild("0.0.1.2", changed=b"forever two " * 8)
    run_build(root, FixtureCDN(old, tmp_path / "old"))
    run_build(root, FixtureCDN(retail, tmp_path / "retail"), product="wow")
    run_build(root, FixtureCDN(new, tmp_path / "new"))
    library = Library(root)
    with library.locked():
        result = library.cleanup(threshold=0.0)
    assert result["forgotten_objects"] > 0 and result["deleted_segments"] > 0
    with CascStore(root) as store:
        kept = {key for key, _ in store.iter_entries()}
    for obj in [old.changed, old.wow]:
        assert obj.ekey not in kept
    for obj in [new.changed, new.wow, retail.changed, retail.common]:
        assert obj.ekey in kept
    for product, build in [("wowf", new), ("wow", retail)]:
        with updater_for(root, FixtureCDN(build, tmp_path / f"verify-{product}"), product=product) as updater:
            assert updater.verify()["ok"] is True


def test_cleanup_waits_for_an_unfinished_update_of_an_installed_client(tmp_path):
    root, old = tmp_path / "install", SyntheticBuild()
    run_build(root, FixtureCDN(old, tmp_path / "old"))
    with updater_for(root, FixtureCDN(SyntheticBuild("0.0.1.2", changed=b"next"), tmp_path / "new")) as updater:
        updater.prepare()
    library = Library(root)
    with library.locked():
        with pytest.raises(UpdateError, match="Finish the Forever update"):
            library.cleanup()


def test_download_manager_pauses_and_resumes_without_fetching_twice(tmp_path):
    import time
    from waystone.downloads import DONE, PAUSED, DownloadManager
    from waystone.settings import Settings

    root, build = tmp_path / "install", SyntheticBuild()
    fetched = []
    manager = None

    class PausingCDN(FixtureCDN):
        def fetch_many(self, entries, *, as_bytes=False, **kwargs):
            for entry, payload in super().fetch_many(entries, as_bytes=as_bytes, **kwargs):
                fetched.append(entry.ekey)
                if len(fetched) == 1:
                    manager.pause("wowf")   # what the Pause button does
                yield entry, payload

    def wait_for(status):
        deadline = time.monotonic() + 20
        while manager.job("wowf").status != status:
            assert time.monotonic() < deadline, manager.job("wowf")
            time.sleep(0.02)

    manager = DownloadManager(Settings(root=str(root)), Library(root),
                              client_factory=lambda code, region: PausingCDN(build, tmp_path / "cache"))
    manager.enqueue("wowf", "install")
    wait_for(PAUSED)
    assert installed_state(root) is None
    manager.enqueue("wowf", "install")
    wait_for(DONE)
    assert installed_state(root)["version"] == build.version["VersionsName"]
    assert len(fetched) == len(set(fetched))


def test_repair_job_fetches_damaged_content_again_after_a_failed_check(tmp_path):
    import time
    from waystone.downloads import DONE, FAILED, DownloadManager
    from waystone.settings import Settings

    root, build = tmp_path / "install", SyntheticBuild()
    run_build(root, FixtureCDN(build, tmp_path / "first"))
    corrupt_stored_object(root, build.common.ekey)
    manager = DownloadManager(Settings(root=str(root)), Library(root),
                              client_factory=lambda code, region: FixtureCDN(build, tmp_path / "cache"))

    def finish(kind):
        manager.enqueue("wowf", kind)
        deadline = time.monotonic() + 20
        while manager.job("wowf").status not in {DONE, FAILED}:
            assert time.monotonic() < deadline, manager.job("wowf")
            time.sleep(0.02)
        return manager.job("wowf")

    checked = finish("verify")
    assert checked.status == FAILED and "Repair" in checked.error
    assert finish("repair").status == DONE
    assert finish("verify").status == DONE


def test_pausing_everything_never_starts_a_waiting_download(tmp_path):
    import threading, time
    from waystone.downloads import PAUSED, DownloadManager
    from waystone.settings import Settings

    root, build = tmp_path / "install", SyntheticBuild()
    started, release = [], threading.Event()

    class SlowCDN(FixtureCDN):
        def fetch_many(self, entries, *, as_bytes=False, **kwargs):
            for item in super().fetch_many(entries, as_bytes=as_bytes, **kwargs):
                release.wait(5)
                yield item

    manager = DownloadManager(Settings(root=str(root)), Library(root),
                              client_factory=lambda code, region: SlowCDN(build, tmp_path / f"cache-{code}"))
    manager.listeners.append(lambda job: job.status == "running" and started.append(job.client))
    manager.enqueue("wowf", "install")
    manager.enqueue("wow", "install")
    deadline = time.monotonic() + 10
    while not manager.busy():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    manager.pause_all()
    release.set()
    while manager.busy():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    time.sleep(0.2)
    assert "wow" not in started and "wowf" in started
    assert {job.client: job.status for job in manager.jobs} == {"wowf": PAUSED, "wow": PAUSED}


def test_progress_counts_only_stored_objects_while_arriving_bytes_drive_the_rate(tmp_path, monkeypatch):
    monkeypatch.setattr(LibraryUpdater, "emit", lambda self, stage, force=False, **fields:   # no event is throttled
                        self.progress({"client": self.code, "stage": stage, **fields}))
    class ArrivesThenStops(FixtureCDN):
        def fetch_many(self, entries, *, as_bytes=False, progress=None, **kwargs):
            entries = list(entries)
            progress(sum(entry.encoded_size for entry in entries))    # everything arrived, none verified yet
            raise KeyboardInterrupt
            yield

    events = []
    with pytest.raises(KeyboardInterrupt):
        run_build(tmp_path / "install", ArrivesThenStops(SyntheticBuild(), tmp_path / "cdn"), progress=events.append)
    downloading = [event for event in events if event["stage"] == "downloading"]
    assert downloading and all(event["bytes"] == downloading[0]["bytes"] for event in downloading)
    assert max(event.get("received", 0) for event in downloading) == downloading[0]["total_bytes"]


def test_a_test_build_starts_and_is_found_running_by_its_own_executable(tmp_path, monkeypatch):
    # PTRs ship WowT.exe and betas WowB.exe. Neither has a Wow.exe to start.
    from waystone import system
    root = tmp_path / "install"
    run_build(root, FixtureCDN(SyntheticBuild(executable_path="WowT.exe"), tmp_path / "cache"))
    library = Library(root)
    assert library.executable("wowf") == library.root / "_forever_/WowT.exe"
    assert library.executable("wowf").is_file()
    # The process list as Wine shows it, so the check does not depend on what /proc exposes here.
    game = [(4242, f"C:\\windows\\system32\\start.exe {library.root / '_forever_/WowT.exe'}", str(tmp_path))]
    monkeypatch.setattr(system, "running_executables", lambda: game)
    assert library.running("wowf") is True
    monkeypatch.setattr(system, "running_executables", lambda: [(4243, "python3 -c pass", str(tmp_path))])
    assert library.running("wowf") is False


def test_a_product_naming_another_clients_folder_is_not_installed_over_it(tmp_path):
    root = tmp_path / "install"
    forever = SyntheticBuild(changed=b"forever content")
    run_build(root, FixtureCDN(forever, tmp_path / "forever-cache"))
    event = SyntheticBuild("0.0.2.2", changed=b"event content", product="wowe1", folder="_forever_")
    with pytest.raises(UpdateError, match="_forever_, which Forever already uses"):
        run_build(root, FixtureCDN(event, tmp_path / "event-cache"), product="wowe1")
    assert (root / "_forever_/Wow.exe").read_bytes() == forever.wow.decoded
    assert Library(root).installed() == ["wowf"]


def test_a_client_installed_from_discovery_is_listed_by_the_library(tmp_path):
    root = tmp_path / "install"
    run_build(root, FixtureCDN(SyntheticBuild(), tmp_path / "cache"))
    clients = root / ".waystone/clients"
    (clients / "wowe1").mkdir()
    (clients / "wowe1/state.json").write_text((clients / "wowf/state.json").read_text())
    (clients / "not-a-product").mkdir()
    (clients / "not-a-product/state.json").write_text("{}")
    assert Library(root).installed() == ["wowe1", "wowf"]


def test_a_paused_update_reserves_the_folder_it_moves_into(tmp_path):
    root = tmp_path / "install"
    run_build(root, FixtureCDN(SyntheticBuild(changed=b"forever content"), tmp_path / "forever-cache"))
    # Forever is installed and has an unfinished update that targets another folder.
    pending = root / ".waystone/clients/wowf/pending.json"
    pending.write_text(json.dumps({"phase": "downloading", "folder": "_moved_"}))
    event = SyntheticBuild("0.0.2.2", changed=b"event content", product="wowe1", folder="_moved_")
    with pytest.raises(UpdateError, match="_moved_, which Forever already uses"):
        run_build(root, FixtureCDN(event, tmp_path / "event-cache"), product="wowe1")


@pytest.fixture
def uninstall_pair(tmp_path):
    root = tmp_path / 'uninstall-library'
    forever = SyntheticBuild()
    retail = SyntheticBuild('0.0.2.2', changed=b'retail only', product='wow', folder='_retail_')
    run_build(root, FixtureCDN(forever, tmp_path / 'forever-cache'))
    run_build(root, FixtureCDN(retail, tmp_path / 'retail-cache'), product='wow')
    return Library(root), forever, retail


def test_uninstall_removes_only_selected_client_and_unused_content(uninstall_pair, tmp_path):
    library, forever, retail = uninstall_pair
    other_state = library.state('wow')
    other_files = {p.relative_to(library.root): p.read_bytes()
                   for p in (library.root / '_retail_').rglob('*') if p.is_file()}
    with library.locked():
        result = library.uninstall('wowf', keep_settings=False)
    assert not (library.root / '_forever_').exists()
    assert not library.client_dir('wowf').exists()
    assert library.installed() == ['wow'] and library.state('wowf') is None
    assert library.state('wow') == other_state
    assert all((library.root / path).read_bytes() == data for path, data in other_files.items())
    assert result['uninstalled'] == 'wowf' and result['cleanup']['forgotten_objects'] > 0
    rows = parse_psv((library.root / '.build.info').read_bytes())
    assert [row['Product'] for row in rows] == ['wow']
    with CascStore(library.root) as store:
        assert not store.has(forever.changed.ekey)
        assert store.has(retail.changed.ekey) and store.has(forever.common.ekey)
    with updater_for(library.root, FixtureCDN(retail, tmp_path / 'verify'), product='wow') as updater:
        assert updater.verify()['ok']


@pytest.mark.parametrize('keep', [True, False])
def test_uninstall_settings_backup_preserves_user_files(uninstall_pair, keep):
    library, _, _ = uninstall_pair
    game = library.root / '_forever_'
    (game / 'WTF' / 'account.wtf').write_bytes(b'personal settings')
    # Windows paths are case insensitive; Linux users can have either spelling.
    (game / 'Interface').mkdir(exist_ok=True)
    (game / 'Interface' / 'addon.lua').write_bytes(b'personal addon')
    (game / 'other-user-file').write_bytes(b'delete with game')
    with library.locked():
        result = library.uninstall('wowf', keep_settings=keep)
    assert not game.exists()
    backups = list(library.root.glob('Waystone-backup-wowf-*'))
    if keep:
        assert len(backups) == 1 and result['backup'] == str(backups[0])
        assert (backups[0] / 'WTF/account.wtf').read_bytes() == b'personal settings'
        assert (backups[0] / 'Interface/addon.lua').read_bytes() == b'personal addon'
        assert not (backups[0] / 'other-user-file').exists()
    else:
        assert not backups and result['backup'] == ''


@pytest.mark.parametrize('blocker', ['running', 'pending', 'empty_pending', 'job'])
def test_uninstall_refuses_busy_client_without_changing_files(uninstall_pair, monkeypatch, blocker):
    from waystone.library import atomic_json
    library, _, _ = uninstall_pair
    if blocker == 'running':
        monkeypatch.setattr(library, 'running', lambda code: code == 'wowf')
    elif blocker in {'pending', 'empty_pending'}:
        atomic_json(library.client_dir('wowf') / 'pending.json', {} if blocker == 'empty_pending' else {'phase': 'preparing'})
    else:
        monkeypatch.setattr(library, 'has_download_job', lambda code: code == 'wowf')
    before = (library.root / '.build.info').read_bytes()
    with library.locked(), pytest.raises(UpdateError, match='Close|unfinished update|download job'):
        library.uninstall('wowf')
    assert library.state('wowf') and (library.root / '_forever_/Wow.exe').is_file()
    assert (library.root / '.build.info').read_bytes() == before
    assert not library.uninstall_record.exists()


@pytest.mark.parametrize('location', ['game', 'settings', 'state', 'build_info', 'store', 'nested_file',
                                      'lock', 'identity', 'record', 'client_state'])
def test_uninstall_refuses_symlinks_without_touching_the_target(uninstall_pair, tmp_path, location):
    import shutil
    library, _, _ = uninstall_pair
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'keep'
    sentinel.write_bytes(b'never touch')
    targets = {'game': library.root / '_forever_', 'settings': library.root / '_forever_/WTF',
               'state': library.client_dir('wowf'), 'build_info': library.root / '.build.info',
               'store': library.root / 'Data/data', 'nested_file': library.root / '_forever_/Wow.exe',
               'lock': library.state_dir / 'library.lock', 'identity': library.state_dir / 'identity.json',
               'record': library.uninstall_record, 'client_state': library.client_dir('wowf') / 'state.json'}
    path = targets[location]
    if path.is_dir():
        shutil.rmtree(path)
        path.symlink_to(outside, target_is_directory=True)
    else:
        path.unlink(missing_ok=True)
        path.symlink_to(sentinel)
    with pytest.raises(UpdateError, match='symbolic link'):
        with library.locked():
            library.uninstall('wowf')
    assert sentinel.read_bytes() == b'never touch'
    assert path.is_symlink()
    if location != 'record':
        assert not library.uninstall_record.exists()


@pytest.mark.parametrize('folder', ['../outside', 'absolute', 'Data', '.waystone', '_RETAIL_', '_retail_'])
def test_uninstall_refuses_unsafe_or_other_client_folder(uninstall_pair, folder):
    from waystone.library import atomic_json
    library, _, _ = uninstall_pair
    outside = library.root.parent / 'outside'
    outside.mkdir()
    (outside / 'keep').write_bytes(b'outside user file')
    if folder == 'absolute':
        folder = str(outside)
    state = library.state('wowf')
    state['folder'] = folder
    atomic_json(library.client_dir('wowf') / 'state.json', state)
    other = (library.root / '_retail_/Wow.exe').read_bytes()
    with library.locked(), pytest.raises(UpdateError):
        library.uninstall('wowf')
    assert (library.root / '_retail_/Wow.exe').read_bytes() == other
    assert (outside / 'keep').read_bytes() == b'outside user file'
    assert library.state('wowf') and not library.uninstall_record.exists()


@pytest.mark.parametrize('point', ['rename', 'backup', 'state_move', 'build_info'])
def test_uninstall_interruption_recovers_on_next_start(uninstall_pair, monkeypatch, point):
    import waystone.library as module
    library, _, _ = uninstall_pair
    original_rename = module.os.rename
    def rename(source, destination):
        if (point == 'rename' or (point == 'backup' and Path(source).name.casefold() == 'wtf')
                or (point == 'state_move' and Path(source).name == 'wowf')):
            raise OSError('simulated crash')
        original_rename(source, destination)
    def fail(*args, **kwargs):
        raise OSError('simulated crash')
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'rename', rename)
        if point == 'build_info':
            patch.setattr(library, 'write_build_info', fail)
        with library.locked(), pytest.raises(OSError, match='simulated crash'):
            library.uninstall('wowf')
    restarted = Library(library.root)
    assert restarted.state('wowf') is None and restarted.installed() == ['wow']
    assert restarted.uninstall_record.is_file()
    with restarted.locked():
        pass
    assert not restarted.uninstall_record.exists()
    assert not restarted.client_dir('wowf').exists()
    assert not (library.root / '_forever_').exists()
    assert not list(library.root.glob('.waystone-trash-*'))
    backups = list(library.root.glob('Waystone-backup-wowf-*'))
    assert len(backups) == 1 and (backups[0] / 'WTF/Config.wtf').is_file()
    assert (library.root / '_retail_/Wow.exe').is_file()
    assert [row['Product'] for row in parse_psv((library.root / '.build.info').read_bytes())] == ['wow']


@pytest.mark.parametrize('point', ['trash_delete', 'cleanup'])
def test_a_file_that_cannot_be_deleted_never_blocks_the_library(uninstall_pair, monkeypatch, point):
    import waystone.library as module
    library, _, _ = uninstall_pair
    original_rmtree = module.shutil.rmtree
    def rmtree(path, *args, **kwargs):
        if Path(path).name.startswith('.waystone-trash-'):
            # Partly deleted, then a file that stays, such as one held open on Windows.
            next(p for p in Path(path).rglob('*') if p.is_file()).unlink()
            raise PermissionError('in use')
        original_rmtree(path, *args, **kwargs)
    def fail(*args, **kwargs):
        raise OSError('simulated failure')
    with monkeypatch.context() as patch:
        patch.setattr(module.shutil, 'rmtree', rmtree if point == 'trash_delete' else original_rmtree)
        if point == 'cleanup':
            patch.setattr(library, 'cleanup', fail)
        with library.locked():
            result = library.uninstall('wowf')
    assert library.state('wowf') is None and not library.uninstall_record.exists()
    assert bool(result['leftover']) == (point == 'trash_delete')
    # Other clients keep working, and the next operation removes what stayed.
    with library.locked():
        pass
    assert not list(library.root.glob('.waystone-trash-*'))
    assert library.installed() == ['wow']


def test_uninstall_never_crosses_into_another_file_system(uninstall_pair, monkeypatch, tmp_path):
    import waystone.library as module
    library, _, _ = uninstall_pair
    mounted = library.root / '_forever_' / 'Screenshots'
    mounted.mkdir(exist_ok=True)
    (mounted / 'precious.png').write_bytes(b'picture')
    # A bind mount of the same file system, which only the mount table shows.
    monkeypatch.setattr(module, 'mount_points', lambda: [Path('/'), mounted.resolve()])
    with library.locked(), pytest.raises(UpdateError, match='another file system'):
        library.uninstall('wowf', keep_settings=False)
    assert (mounted / 'precious.png').read_bytes() == b'picture' and library.state('wowf')
    assert not library.uninstall_record.exists()


def test_cleanup_keeps_content_of_a_client_that_is_still_being_installed(uninstall_pair):
    library, _, _ = uninstall_pair
    planning = library.client_dir('wowt')
    planning.mkdir(parents=True)
    (planning / 'pending.json').write_text('{"phase": "preparing"}')
    with library.locked(), pytest.raises(UpdateError, match='before cleaning up'):
        library.cleanup()


def test_uninstall_defers_cleanup_while_another_client_runs(uninstall_pair, monkeypatch):
    library, forever, _ = uninstall_pair
    monkeypatch.setattr(library, 'running', lambda code: code == 'wow')
    with library.locked():
        result = library.uninstall('wowf')
    assert 'Close' in result['cleanup']['deferred']
    assert not library.state('wowf') and library.state('wow')
    with CascStore(library.root) as store:
        assert store.has(forever.changed.ekey)
    monkeypatch.setattr(library, 'running', lambda code: False)
    with library.locked():
        library.cleanup(threshold=0.0)
    with CascStore(library.root) as store:
        assert not store.has(forever.changed.ekey)


def test_uninstall_restart_worker_finishes_journal_even_without_auto_resume(uninstall_pair, monkeypatch):
    from waystone.downloads import DONE, FAILED, DownloadManager
    from waystone.settings import Settings
    library, _, _ = uninstall_pair
    def crash():
        raise OSError('crash')
    with monkeypatch.context() as patch:
        patch.setattr(library, 'write_build_info', crash)
        with library.locked(), pytest.raises(OSError, match='crash'):
            library.uninstall('wowf')
    # A restored journal runs even though ordinary downloads default to paused.
    manager = DownloadManager(Settings(root=str(library.root), resume_downloads=False), Library(library.root))
    try:
        manager._worker.join(0.01)
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = manager.job('wowf')
            if job and job.status in {DONE, FAILED}:
                break
            time.sleep(0.01)
        assert job is not None and job.kind == 'uninstall' and job.status == DONE
        assert job.result['uninstalled'] == 'wowf'
        assert not library.uninstall_record.exists()
        assert not library.client_dir('wowf').exists()
    finally:
        manager.shutdown()
        manager._worker.join(5)


def test_uninstall_recovery_refuses_a_game_started_after_the_crash(uninstall_pair, monkeypatch):
    import waystone.library as module
    library, _, _ = uninstall_pair
    def crash(*args):
        raise OSError('crash')
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'rename', crash)
        with library.locked(), pytest.raises(OSError, match='crash'):
            library.uninstall('wowf')
    restarted = Library(library.root)
    monkeypatch.setattr(module.system, 'running_executables', lambda: [
        (123, str(library.root / '_forever_/Wow.exe'), str(library.root / '_forever_'))])
    with pytest.raises(UpdateError, match='Close the client'):
        with restarted.locked():
            pass
    assert (library.root / '_forever_/Wow.exe').is_file()
    assert library.uninstall_record.exists()


def test_without_linux_mount_information_nothing_is_deleted(uninstall_pair, monkeypatch):
    import pathlib
    import waystone.library as module
    library, _, _ = uninstall_pair
    monkeypatch.setattr(module.sys, 'platform', 'linux')
    original = pathlib.Path.read_text
    def read_text(self, *args, **kwargs):
        if str(self) == '/proc/self/mountinfo':
            raise PermissionError('hidden by a sandbox')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(pathlib.Path, 'read_text', read_text)
    with library.locked(), pytest.raises(UpdateError, match='mount information'):
        library.uninstall('wowf')
    assert library.state('wowf') and (library.root / '_forever_').is_dir()


def test_an_unreadable_folder_left_in_the_trash_never_blocks_the_library(uninstall_pair):
    library, _, _ = uninstall_pair
    trash = library.root / '.waystone-trash-0123456789abcdef0123456789abcdef'
    locked_away = trash / 'Logs'
    locked_away.mkdir(parents=True)
    (locked_away / 'session.log').write_text('kept by the system')
    locked_away.chmod(0)
    try:
        with library.locked():
            pass                    # sweeping meets the folder and leaves it
        with library.locked():
            pass
        assert trash.exists() and library.installed() == ['wow', 'wowf']
        # Deleting it directly reports that something stayed, and does not raise.
        from waystone.library import remove_tree
        assert remove_tree(trash) is False and (locked_away.parent).exists()
    finally:
        locked_away.chmod(0o700)
