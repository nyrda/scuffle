"""Protocol tests include public Forever bytes captured on 2026-10-28."""
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from scuffle.ngdp.formats import (
    EncodingTable, FormatError, UnsupportedEncodingError, blte_header,
    decode_blte, get_blte_encoded_key, parse_psv, read_config,
    read_download, read_install, verify_blte,
)

FIXTURES = Path(__file__).parent / "fixtures"
PROOF = json.loads((FIXTURES / "manifest-proof.json").read_text())


def md5(data):
    return hashlib.md5(data).digest()


def blte(chunks):
    """Assemble independent table metadata around already encoded frames."""
    table = b"".join(struct.pack(">II", len(frame), size) + md5(frame)
                     for frame, size in chunks)
    header = b"BLTE" + struct.pack(">I", 12 + len(table)) + b"\x0f" + len(chunks).to_bytes(3, "big") + table
    return header + b"".join(frame for frame, _ in chunks)


def tag(name, kind, bitmap):
    return name.encode() + b"\0" + kind.to_bytes(2, "big") + bytes([bitmap])


def download(version, priorities, tags=b"", tag_count=0, base=0, checksums=False):
    head = b"DL" + bytes([version, 16, checksums]) + len(priorities).to_bytes(4, "big") + tag_count.to_bytes(2, "big")
    flags = 2 if version >= 2 else 0
    if version >= 2:
        head += bytes([flags])
    if version >= 3:
        head += base.to_bytes(1, "big", signed=True) + bytes(3)
    rows = []
    for i, priority in enumerate(priorities):
        rows.append(bytes([i + 1]) * 16 + (500 + i).to_bytes(5, "big") + priority.to_bytes(1, "big", signed=True)
                    + (b"CSUM" if checksums else b"") + bytes(flags))
    return head + b"".join(rows) + tags


def encoding_fixture():
    """Keep two authentic, hash checked EN pages in a compact test envelope.

    The original page descriptors and specification index are retained in the
    proof JSON. Only the header and unused string table entries are synthetic.
    """
    proof = PROOF["encoding"]
    specs = [""] * proof["spec_count"]
    record = proof["root_encoding"]
    specs[record["espec_index"]] = record["espec"]
    strings = "\0".join(specs).encode()
    cp = (FIXTURES / "en-root-ckey-page.bin").read_bytes()
    ep = (FIXTURES / "en-root-ekey-page.bin").read_bytes()
    head = b"EN\x01\x10\x10" + struct.pack(">HHIIBI", 4, 4, 1, 1, 0, len(strings))
    return (head + strings + bytes.fromhex(proof["root"]["index_descriptor_hex"]) + cp
            + bytes.fromhex(record["index_descriptor_hex"]) + ep)


class FormatsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, data, name="object"):
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_public_install_and_eu_executable_identity(self):
        source = FIXTURES / "install.blte"
        ekey = bytes.fromhex("69ab17d415ecc051c5eafd9c91f97ae1")
        ckey = bytes.fromhex("acd3d5ba96cb39bf5d3ce10e0e206fff")
        verify_blte(source, ekey)
        self.assertEqual(get_blte_encoded_key(source), ekey)
        self.assertNotEqual(md5(source.read_bytes()), ekey)
        target = self.root / "install"
        decode_blte(source, target, ekey, ckey, 24086)
        self.assertEqual(len(list(read_install(target, set()))), 275)
        selected = list(read_install(target, {"Windows", "x86_64", "enUS", "EU"}))
        self.assertEqual(len(selected), 39)
        wow = [entry for entry in selected if entry.path == "Wow.exe"]
        self.assertEqual(len(wow), 1)
        self.assertEqual(wow[0].ckey.hex(), "bb6f287b517061386cfeb23e09860321")
        self.assertGreater(wow[0].decoded_size, 1_000_000)

    def test_anonymous_archive_range_capture(self):
        source = FIXTURES / "range-object.blte"
        ekey = bytes.fromhex("5b349060a972337992fa86cf8a09a94f")
        ckey = bytes.fromhex("cbc5acdb227705b51b6241f0e847dbe4")
        target = self.root / "range"
        decode_blte(source, target, ekey, ckey, 2360)
        self.assertEqual(target.stat().st_size, 2360)
        self.assertEqual(blte_header(source).encoded_size, 338)

    def test_retained_encoding_pages_resolve_current_root(self):
        path = self.write(encoding_fixture())
        root = PROOF["encoding"]["root"]
        with EncodingTable(path) as table:
            size, records = table.lookup(bytes.fromhex(root["ckey"]))
            self.assertEqual(size, 50526132)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].ekey.hex(), "00870fa6719b8ec44b70ef5582b032cf")
            self.assertEqual(records[0].encoded_size, 38886128)
            self.assertEqual(records[0].espec, "b:{256K*=z}")
            self.assertEqual(table.get(records[0].ekey), records[0])
            with self.assertRaises(KeyError):
                table.lookup(bytes(16))
        with self.assertRaises(ValueError):
            table.get(records[0].ekey)
        corrupted = bytearray(path.read_bytes())
        corrupted[-3] ^= 1
        with self.assertRaisesRegex(FormatError, "page MD5"):
            EncodingTable(self.write(corrupted, "bad"))

    def test_captured_download_records(self):
        raw = (FIXTURES / "dl-first8-records.bin").read_bytes()
        path = self.write(b"DL\x01\x10\0" + (8).to_bytes(4, "big") + bytes(2) + raw)
        entries = list(read_download(path, set()))
        self.assertEqual(len(entries), 8)
        for entry, proof in zip(entries, PROOF["download_first8"]):
            self.assertEqual(entry.ekey.hex(), proof["ekey"])
            self.assertEqual(entry.encoded_size, proof["encoded_bytes"])
            self.assertEqual(entry.priority, proof["priority"])

    def test_download_version_layouts_signed_priorities_and_tags(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                base = 2 if version == 3 else 0
                path = self.write(download(version, [-1, 0, 2], base=base, checksums=True))
                entries = list(read_download(path, set()))
                self.assertEqual([entry.priority for entry in entries], [-1 - base, -base, 2 - base])
                self.assertEqual([entry.encoded_size for entry in entries], [500, 501, 502])
        # OR Windows/OSX in platform type 1, AND enUS in locale type 3.
        # The optional type is omitted, so it does not filter anything.
        tags = tag("Windows", 1, 0xA0) + tag("OSX", 1, 0x50) + tag("enUS", 3, 0xC0) + tag("HighRes", 0x4000, 0x20)
        path = self.write(download(3, [0, 1, 2, -1], tags, 4))
        selected = list(read_download(path, {"windows", "OSX", "enUS"}))
        self.assertEqual([entry.ekey[0] for entry in selected], [1, 2])
        selected = list(read_download(path, {"Windows", "HighRes"}))
        self.assertEqual([entry.ekey[0] for entry in selected], [3])
        with self.assertRaisesRegex(FormatError, "Unknown manifest tags"):
            list(read_download(path, {"no-such-tag"}))

    def test_multichunk_single_frame_and_empty_output(self):
        output = self.root / "decoded"
        raw = b"plain" + b"zip" * 100000
        source = blte([(b"Nplain", 5), (b"Z" + zlib.compress(b"zip" * 100000), 300000)])
        decode_blte(source, output, get_blte_encoded_key(source), md5(raw), len(raw))
        self.assertEqual(output.read_bytes(), raw)
        single = b"BLTE" + bytes(4) + b"Z" + zlib.compress(raw)
        self.assertEqual(get_blte_encoded_key(single), md5(single))
        verify_blte(single, md5(single))
        decode_blte(single, output, md5(single), md5(raw), len(raw))
        self.assertEqual(output.read_bytes(), raw)
        empty = blte([(b"Z" + zlib.compress(b""), 0)])
        decode_blte(empty, output, expected_ckey=md5(b""), expected_size=0)
        self.assertEqual(output.read_bytes(), b"")

    def test_integrity_errors_preserve_destination_and_clean_temporary(self):
        target = self.write(b"original", "decoded")
        encoded = blte([(b"Z" + zlib.compress(b"hello"), 5)])
        corrupt = encoded[:-1] + bytes([encoded[-1] ^ 1])
        bads = [corrupt, encoded[:-1], encoded + b"trailer", b"NOPE" + encoded[4:], encoded[:8]]
        for source in bads:
            with self.subTest(source=source[:8]):
                with self.assertRaises(FormatError):
                    decode_blte(source, target)
                self.assertEqual(target.read_bytes(), b"original")
                self.assertEqual(list(self.root.glob(".blte-*")), [])
        with self.assertRaisesRegex(FormatError, "chunk MD5"):
            verify_blte(corrupt, get_blte_encoded_key(encoded))
        with self.assertRaisesRegex(FormatError, "CKey"):
            decode_blte(encoded, target, expected_ckey=bytes(16))
        with self.assertRaisesRegex(FormatError, "EKey"):
            decode_blte(encoded, target, expected_ekey=bytes(16))

    def test_zlib_bomb_is_bounded_by_declared_output(self):
        # A small encoded stream expands beyond the table's declared size.
        encoded = blte([(b"Z" + zlib.compress(b"a" * (8 * 1024 * 1024)), 3)])
        target = self.root / "decoded"
        with self.assertRaisesRegex(FormatError, "exceeds"):
            decode_blte(encoded, target)
        self.assertFalse(target.exists())
        trailing = blte([(b"Z" + zlib.compress(b"abc") + b"extra", 3)])
        with self.assertRaisesRegex(FormatError, "Trailing"):
            decode_blte(trailing, target)

    def test_encrypted_objects_can_be_stored_but_not_decoded(self):
        encoded = blte([(b"Eencrypted-payload", 123)])
        verify_blte(encoded, get_blte_encoded_key(encoded))
        with self.assertRaisesRegex(UnsupportedEncodingError, "decryption key"):
            decode_blte(encoded, self.root / "decoded")

    def test_text_metadata(self):
        self.assertEqual(read_config(b"# comment\nencoding = aa bb # trailing\nempty =\n"),
                         {"encoding": ["aa", "bb"], "empty": []})
        self.assertEqual(parse_psv(b"## seqn = 5\nRegion!STRING:0|BuildConfig!HEX:16\neu|abc\n"),
                         [{"Region": "eu", "BuildConfig": "abc"}])
        for data in [b"x = 1\nx = 2", b"missing equals", b"bad key = 1", b"a=\0"]:
            with self.assertRaises(FormatError):
                read_config(data)
        with self.assertRaises(FormatError):
            parse_psv(b"one|two\nthree")

    def test_truncated_manifest_ranges_and_invalid_pages(self):
        for raw, reader in [(b"IN\x01\x10\0\0\xff\xff\xff\xff", read_install),
                            (b"DL\x01\x10\0\xff\xff\xff\xff\0\0", read_download)]:
            with self.assertRaises(FormatError):
                list(reader(self.write(raw), set()))
        fixture = bytearray(encoding_fixture())
        fixture[9:13] = b"\xff" * 4
        with self.assertRaises(FormatError):
            EncodingTable(self.write(fixture))


if __name__ == "__main__":
    unittest.main()
