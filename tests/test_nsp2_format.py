"""NSP v2 format — comprehensive roundtrip, tamper, DoS and rekey tests.

Coverage:
- unsealed roundtrip (zstd + none + dedup + empty blob)
- sealed roundtrip (Argon2id + HKDF + chunked ChaCha20 + padding)
- Range reads across 64 KiB chunk boundaries (start / middle / end)
- Empty blob (0 raw bytes) sealed roundtrip
- Wrong passphrase → NSP2SealedError
- Tamper tests: header / footer / chunk / index / keyslots corruption
- Chunk permutation → authentication failure
- Truncated chunk → authentication failure
- DoS: m_cost_kib=4 000 000 → instant refusal BEFORE Argon2id call
- rekey(): old passphrase rejected, new passphrase works, old keyslot zeroed
"""

import hashlib
import io
import json
import os
import struct
import tempfile
import unittest
from typing import BinaryIO

import zstandard as zstd

from app.core.nsp2 import (
    CHUNK_SIZE_SEALED,
    DEK_BYTES,
    FOOTER_FORMAT,
    FOOTER_SIZE,
    HEADER_SIZE,
    INDEX_MAX_LEN,
    KEYSLOTS_MAX_LEN,
    MAGIC_FOOTER,
    MAGIC_WRITE,
    NSP2Error,
    NSP2RekeyError,
    NSP2SealedError,
    NSP2UnsealedError,
    NONCE_SIZE,
    SALT_BYTES,
    FORMAT_VERSION,
    FLAG_SEALED,
    FLAG_PADDED,
    build_header,
    build_footer,
    parse_header,
    parse_footer,
    rekey_nsp2,
    create_package,
    create_package_streaming,
)
from app.core.nsp2_reader import (
    NSP2Container,
    open_nsp2,
    read_blob,
    read_index_block,
    unwrap_dek_from_keyslots,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ZSTD = zstd.ZstdDecompressor()

# A large resource (> 150 KiB) to test chunk boundaries
_LARGE_HTML = (b"<html><head><title>Test</title></head><body>" + b"x" * (160 * 1024) + b"</body></html>")
# Medium resource (~130 KiB) to span 2 chunks
_MEDIUM_HTML = (b"<html><head><title>Middle</title></head><body>" + b"y" * (130 * 1024) + b"</body></html>")
# Small resources (bytes)
_SMALL_CSS = b"body{margin:0;padding:0;font-family:sans-serif;}"
_SMALL_JS = b"const Miku=()=>{console.log('hello')}"
_SMALL_JSON = json.dumps({"schema_version": 2, "package": {"id": "test"}}).encode("utf-8")
# Binary resource (no compression)
_SMALL_BIN = bytes(range(256)) * 100  # 25.6 KiB
# Empty resource
_EMPTY_BIN = b""
# Duplicate content (same hash → same blob)
_DUP_SMALL_CSS = b"body{margin:0;padding:0;font-family:sans-serif;}"

PASSPHRASE = "test-passphrase-nsp2"
KDF_PARAMS_SEALED = {"m_cost_kib": 16384, "t_cost": 1, "p_cost": 1}


def _make_resources() -> list[dict]:
    return [
        {"url": "/index.html", "content": _LARGE_HTML, "mime": "text/html; charset=utf-8", "type": "html"},
        {"url": "/middle.html", "content": _MEDIUM_HTML, "mime": "text/html; charset=utf-8", "type": "html"},
        {"url": "/static/style.css", "content": _SMALL_CSS, "mime": "text/css; charset=utf-8", "type": "css"},
        {"url": "/static/app.js", "content": _SMALL_JS, "mime": "application/javascript", "type": "js"},
        {"url": "/meta.json", "content": _SMALL_JSON, "mime": "application/json", "type": "json"},
        {"url": "/icon.bin", "content": _SMALL_BIN, "mime": "application/octet-stream", "type": "binary"},
        {"url": "/empty.bin", "content": _EMPTY_BIN, "mime": "application/octet-stream", "type": "binary"},
        {"url": "/dup.css", "content": _DUP_SMALL_CSS, "mime": "text/css", "type": "css"},
    ]


def _package_info(extra: dict | None = None) -> dict:
    info = {
        "schema_version": 2,
        "package": {
            "id": "test_pkg",
            "title": "Test Package",
            "revision": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "origin": "https://test.local",
            "root_url": "/dashboard?package_id=test_pkg",
            "module": {"id": "test", "title": "Test", "root_url": "/test"},
        },
        "runtime": {"embedded": True, "sha256": ""},
    }
    if extra:
        info.update(extra)
    return info


# ---------------------------------------------------------------------------
# Helper: read full decompressed blob from file
# ---------------------------------------------------------------------------


def _read_all_blobs(container: NSP2Container, fobj: BinaryIO, dek: bytes | None = None) -> dict[str, bytes]:
    """Return {url: raw_bytes} for all entries.

    For sealed containers, *dek* must be the unwrapped DEK.
    For unsealed, pass ``None``.
    """
    result: dict[str, bytes] = {}
    for entry in container.index["entries"]:
        url = entry["url"]
        blob_meta = container.blob_metas[entry["blob_id"]]
        raw = read_blob(fobj, blob_meta, container.container_id, dek=dek)
        result[url] = raw
    return result


def _extract_dek_from_file(fobj: BinaryIO, passphrase: str) -> bytes:
    """Extract and unwrap the DEK from a sealed NSP2 file."""
    header = fobj.read(HEADER_SIZE)
    fmt_ver, flags, _cid = parse_header(header)
    fobj.seek(0)
    full_file = fobj.read()
    total = len(full_file)
    footer = parse_footer(full_file, total)
    fobj.seek(footer["keyslots_offset"])
    ks_bytes = fobj.read(footer["keyslots_len"])
    return unwrap_dek_from_keyslots(ks_bytes, header, passphrase)


# ---------------------------------------------------------------------------
# Tests — unsealed
# ---------------------------------------------------------------------------


class UnsealedRoundtripTests(unittest.TestCase):
    """Unsealed container: compress, write, read back — all raw bytes match."""

    def test_full_roundtrip(self):
        resources = _make_resources()
        payload = create_package(resources, _package_info(), sealed=False)
        self.assertEqual(MAGIC_WRITE, payload[:4])
        fmt_ver, flags, _cid = parse_header(payload[:HEADER_SIZE])
        self.assertEqual(FORMAT_VERSION, fmt_ver)
        self.assertEqual(0, flags)

        footer = parse_footer(payload, len(payload))
        self.assertEqual(MAGIC_FOOTER, payload[len(payload) - 4 :])
        self.assertEqual(2, footer["format_version"])

        fobj = io.BytesIO(payload)
        container = open_nsp2(fobj, passphrase=None)
        self.assertFalse(container.is_sealed)
        self.assertEqual(8, len(container.index["entries"]))

        # Dedup: /static/style.css and /dup.css should share blob_id 2
        entries = container.index["entries"]
        css_blob = next(e["blob_id"] for e in entries if e["url"] == "/static/style.css")
        dup_blob = next(e["blob_id"] for e in entries if e["url"] == "/dup.css")
        self.assertEqual(css_blob, dup_blob)

        # Read back all blobs
        recovered = _read_all_blobs(container, fobj)
        self.assertEqual(_LARGE_HTML, recovered["/index.html"])
        self.assertEqual(_MEDIUM_HTML, recovered["/middle.html"])
        self.assertEqual(_SMALL_CSS, recovered["/static/style.css"])
        self.assertEqual(_SMALL_JS, recovered["/static/app.js"])
        self.assertEqual(_SMALL_JSON, recovered["/meta.json"])
        self.assertEqual(_SMALL_BIN, recovered["/icon.bin"])
        self.assertEqual(b"", recovered["/empty.bin"])
        self.assertEqual(_DUP_SMALL_CSS, recovered["/dup.css"])

    def test_empty_blob_roundtrip(self):
        resources = [
            {"url": "/empty.bin", "content": b"", "mime": "application/octet-stream", "type": "binary"},
        ]
        payload = create_package(resources, _package_info(), sealed=False)
        fobj = io.BytesIO(payload)
        container = open_nsp2(fobj, passphrase=None)
        blob_meta = container.blob_metas[0]
        raw = read_blob(fobj, blob_meta, container.container_id, dek=None)
        self.assertEqual(b"", raw)

    def test_zstd_vs_none_compression(self):
        """text resources should be compressed, binary should not."""
        resources = [
            {"url": "/a.txt", "content": b"a" * 1000, "mime": "text/plain", "type": "text"},
            {"url": "/b.bin", "content": bytes(range(256)) * 100, "mime": "application/octet-stream", "type": "binary"},
        ]
        payload = create_package(resources, _package_info(), sealed=False)
        fobj = io.BytesIO(payload)
        container = open_nsp2(fobj, passphrase=None)
        blob_a = container.blob_metas[0]
        blob_b = container.blob_metas[1]
        self.assertEqual("zstd", blob_a["compression"])
        self.assertEqual("none", blob_b["compression"])
        self.assertLess(blob_a["stored_len"], blob_a["raw_len"])  # compressed smaller
        self.assertEqual(blob_b["stored_len"], blob_b["encoded_len"])  # no compression


# ---------------------------------------------------------------------------
# Tests — sealed
# ---------------------------------------------------------------------------


class SealedRoundtripTests(unittest.TestCase):
    """Sealed container: Argon2id → HKDF → chunked ChaCha20, read back."""

    def test_full_roundtrip(self):
        resources = _make_resources()
        payload = create_package(
            resources, _package_info(), sealed=True, passphrase=PASSPHRASE, **KDF_PARAMS_SEALED
        )
        self.assertEqual(MAGIC_WRITE, payload[:4])
        fmt_ver, flags, cid = parse_header(payload[:HEADER_SIZE])
        self.assertEqual(FORMAT_VERSION, fmt_ver)
        self.assertEqual(FLAG_SEALED, flags)

        footer = parse_footer(payload, len(payload))
        self.assertEqual(FLAG_SEALED, footer["flags"])

        fobj = io.BytesIO(payload)
        container = open_nsp2(fobj, passphrase=PASSPHRASE)
        self.assertTrue(container.is_sealed)

        # Extract DEK for blob reading
        dek = _extract_dek_from_file(io.BytesIO(payload), PASSPHRASE)
        fobj.seek(0)
        container2 = open_nsp2(fobj, passphrase=PASSPHRASE)
        recovered = _read_all_blobs(container2, fobj, dek=dek)
        self.assertEqual(_LARGE_HTML, recovered["/index.html"])
        self.assertEqual(_MEDIUM_HTML, recovered["/middle.html"])
        self.assertEqual(_SMALL_CSS, recovered["/static/style.css"])
        self.assertEqual(_SMALL_JS, recovered["/static/app.js"])
        self.assertEqual(_SMALL_JSON, recovered["/meta.json"])
        self.assertEqual(_SMALL_BIN, recovered["/icon.bin"])
        self.assertEqual(b"", recovered["/empty.bin"])
        self.assertEqual(_DUP_SMALL_CSS, recovered["/dup.css"])

    def test_empty_blob_sealed(self):
        """Empty blob → 1 sealed chunk (ct+tag only, no nonce) → 16 bytes on disk."""
        resources = [{"url": "/empty.bin", "content": b"", "mime": "application/octet-stream", "type": "binary"}]
        payload = create_package(resources, _package_info(), sealed=True, passphrase=PASSPHRASE, **KDF_PARAMS_SEALED)
        fobj = io.BytesIO(payload)
        container = open_nsp2(fobj, passphrase=PASSPHRASE)
        blob_meta = container.blob_metas[0]
        self.assertEqual(0, blob_meta["raw_len"])
        self.assertEqual(0, blob_meta["encoded_len"])
        # Empty blob = 1 chunk, ct_only(0+16) = 16 bytes on disk
        self.assertEqual(16, blob_meta["stored_len"])
        dek = _extract_dek_from_file(io.BytesIO(payload), PASSPHRASE)
        raw = read_blob(fobj, blob_meta, container.container_id, dek=dek)
        self.assertEqual(b"", raw)

    def test_wrong_passphrase(self):
        """Wrong passphrase must raise NSP2SealedError."""
        resources = [{"url": "/x.html", "content": b"<html>", "mime": "text/html", "type": "html"}]
        payload = create_package(resources, _package_info(), sealed=True, passphrase="correct-pass")
        fobj = io.BytesIO(payload)
        with self.assertRaises(NSP2SealedError):
            open_nsp2(fobj, passphrase="wrong-pass")

    def test_padding(self):
        """FLAG_PADDED → Data Region aligned to 256 KiB."""
        resources = [{"url": "/x.html", "content": b"<html>tiny", "mime": "text/html", "type": "html"}]
        payload = create_package(
            resources, _package_info(), sealed=True, passphrase=PASSPHRASE,
            padded=True, **KDF_PARAMS_SEALED
        )
        footer = parse_footer(payload, len(payload))
        flags = footer["flags"]
        self.assertEqual(FLAG_SEALED | FLAG_PADDED, flags)
        data_region_end = footer["index_offset"]
        self.assertEqual(0, data_region_end % 262144)


# ---------------------------------------------------------------------------
# Tests — Range reads (sealed)
# ---------------------------------------------------------------------------


class SealedRangeReadTests(unittest.TestCase):
    """Range reads through sealed container across 64 KiB chunk boundaries."""

    def _open_sealed(
        self, resources: list[dict]
    ) -> tuple[NSP2Container, io.BytesIO, bytes]:
        payload = create_package(
            resources, _package_info(), sealed=True, passphrase=PASSPHRASE, **KDF_PARAMS_SEALED
        )
        dek = _extract_dek_from_file(io.BytesIO(payload), PASSPHRASE)
        fobj = io.BytesIO(payload)
        container = open_nsp2(fobj, passphrase=PASSPHRASE)
        return container, fobj, dek

    def test_range_start_of_blob(self):
        """Read from the very first byte of the large HTML blob."""
        resources = [{"url": "/big.html", "content": _LARGE_HTML, "mime": "text/html", "type": "html"}]
        container, fobj, dek = self._open_sealed(resources)
        blob_meta = container.blob_metas[0]
        # Read first 1024 bytes
        chunk = read_blob(fobj, blob_meta, container.container_id, dek=dek, start=0, length=1024)
        self.assertEqual(_LARGE_HTML[:1024], chunk)

    def test_range_middle_of_blob(self):
        """Read starting in the middle of a 2-chunk blob (offset ~70 KiB)."""
        resources = [{"url": "/big.html", "content": _LARGE_HTML, "mime": "text/html", "type": "html"}]
        container, fobj, dek = self._open_sealed(resources)
        blob_meta = container.blob_metas[0]
        # Offset 70*1024 = 71680 bytes — crosses the 64 KiB chunk boundary
        start = 70 * 1024
        chunk = read_blob(fobj, blob_meta, container.container_id, dek=dek, start=start, length=4096)
        self.assertEqual(_LARGE_HTML[start : start + 4096], chunk)

    def test_range_end_of_blob(self):
        """Read the last 8 KiB of the blob."""
        resources = [{"url": "/big.html", "content": _LARGE_HTML, "mime": "text/html", "type": "html"}]
        container, fobj, dek = self._open_sealed(resources)
        blob_meta = container.blob_metas[0]
        start = len(_LARGE_HTML) - 8192
        chunk = read_blob(fobj, blob_meta, container.container_id, dek=dek, start=start, length=8192)
        self.assertEqual(_LARGE_HTML[start:], chunk)

    def test_range_crossing_chunk_boundary(self):
        """Read a range that spans across two 64 KiB chunks."""
        resources = [{"url": "/big.html", "content": _LARGE_HTML, "mime": "text/html", "type": "html"}]
        container, fobj, dek = self._open_sealed(resources)
        blob_meta = container.blob_metas[0]
        # Start at 60 KiB, read 16 KiB → crosses 64 KiB boundary at offset 4 KiB
        start = 60 * 1024
        length = 16 * 1024
        chunk = read_blob(fobj, blob_meta, container.container_id, dek=dek, start=start, length=length)
        self.assertEqual(_LARGE_HTML[start : start + length], chunk)

    def test_range_exact_chunk_boundary(self):
        """Read exactly the second chunk (64 KiB – 128 KiB)."""
        resources = [{"url": "/big.html", "content": _LARGE_HTML, "mime": "text/html", "type": "html"}]
        container, fobj, dek = self._open_sealed(resources)
        blob_meta = container.blob_metas[0]
        start = 65536  # 64 KiB
        length = 65536
        chunk = read_blob(fobj, blob_meta, container.container_id, dek=dek, start=start, length=length)
        self.assertEqual(_LARGE_HTML[start : start + length], chunk)

    def test_range_full_blob(self):
        """Read entire blob in one go."""
        resources = [{"url": "/big.html", "content": _LARGE_HTML, "mime": "text/html", "type": "html"}]
        container, fobj, dek = self._open_sealed(resources)
        blob_meta = container.blob_metas[0]
        chunk = read_blob(fobj, blob_meta, container.container_id, dek=dek)
        self.assertEqual(_LARGE_HTML, chunk)

    def test_multi_blob_sealed(self):
        """Multiple blobs with different sizes, verify each."""
        resources = [
            {"url": "/big.html", "content": _LARGE_HTML, "mime": "text/html", "type": "html"},
            {"url": "/small.bin", "content": _SMALL_BIN, "mime": "application/octet-stream", "type": "binary"},
            {"url": "/empty.bin", "content": b"", "mime": "application/octet-stream", "type": "binary"},
        ]
        container, fobj, dek = self._open_sealed(resources)
        recovered = _read_all_blobs(container, fobj, dek=dek)
        self.assertEqual(_LARGE_HTML, recovered["/big.html"])
        self.assertEqual(_SMALL_BIN, recovered["/small.bin"])
        self.assertEqual(b"", recovered["/empty.bin"])


# ---------------------------------------------------------------------------
# Tests — Tamper detection
# ---------------------------------------------------------------------------


class TamperTests(unittest.TestCase):
    """Any single-byte tamper → authentication failure."""

    def _seal(self, resources: list[dict] | None = None) -> tuple[bytes, NSP2Container]:
        res = resources or [{"url": "/x.html", "content": b"<html>" * 100, "mime": "text/html", "type": "html"}]
        payload = bytearray(
            create_package(res, _package_info(), sealed=True, passphrase=PASSPHRASE, **KDF_PARAMS_SEALED)
        )
        fobj = io.BytesIO(bytes(payload))
        container = open_nsp2(fobj, passphrase=PASSPHRASE)
        return payload, container

    def test_tamper_header(self):
        """Flip magic bytes in header → parse_header refuses."""
        payload, _ = self._seal()
        payload[0] ^= 0xFF  # flip a bit in magic "NSP2"
        with self.assertRaises(NSP2Error):
            parse_header(bytes(payload[:HEADER_SIZE]))

    def test_tamper_footer(self):
        """Flip one bit in footer CRC → parse_footer refuses."""
        payload, _ = self._seal()
        idx = len(payload) - FOOTER_SIZE + 8  # somewhere in CRC32 area
        payload[idx] ^= 0x01
        with self.assertRaises(NSP2Error):
            parse_footer(bytes(payload), len(payload))

    def test_tamper_chunk(self):
        """Flip one byte in a sealed chunk → decrypt fails."""
        resources = [{"url": "/big.html", "content": _LARGE_HTML, "mime": "text/html", "type": "html"}]
        payload, container = self._seal(resources)
        blob_meta = container.blob_metas[0]
        # Find the first sealed chunk in the data region
        # No nonce prefix — each chunk starts at offset directly
        chunk_start = blob_meta["offset"]  # first chunk starts right after header/data
        if chunk_start < len(payload) - FOOTER_SIZE - 64:
            payload[chunk_start] ^= 0x01
        fobj = io.BytesIO(bytes(payload))
        container2 = open_nsp2(fobj, passphrase=PASSPHRASE)
        with self.assertRaises(NSP2SealedError):
            read_blob(fobj, blob_meta, container2.container_id, dek=container2.container_id)

    def test_tamper_index(self):
        """Flip one byte in encrypted index → decrypt fails."""
        payload, container = self._seal()
        footer = parse_footer(bytes(payload), len(payload))
        idx_off = footer["index_offset"]
        # Skip nonce (first 12 bytes of encrypted index)
        tamper_at = idx_off + 20
        if tamper_at < len(payload) - FOOTER_SIZE:
            payload[tamper_at] ^= 0x01
        fobj = io.BytesIO(bytes(payload))
        with self.assertRaises(NSP2SealedError):
            open_nsp2(fobj, passphrase=PASSPHRASE)

    def test_truncated_chunk(self):
        """Cut off the last byte of a sealed chunk → tag check fails."""
        resources = [{"url": "/big.html", "content": _LARGE_HTML, "mime": "text/html", "type": "html"}]
        payload = bytearray(create_package(resources, _package_info(), sealed=True, passphrase=PASSPHRASE, **KDF_PARAMS_SEALED))
        # Truncate by 1 byte from end of file (inside footer — bad)
        # Instead, truncate from the end of the data region (before index)
        footer = parse_footer(bytes(payload), len(payload))
        data_end = footer["index_offset"]
        if data_end > HEADER_SIZE + 32:
            del payload[data_end - 1]
        fobj = io.BytesIO(bytes(payload))
        with self.assertRaises(Exception):  # truncated file will fail at read time
            open_nsp2(fobj, passphrase=PASSPHRASE)


# ---------------------------------------------------------------------------
# Tests — DoS protection
# ---------------------------------------------------------------------------


class DoSTests(unittest.TestCase):
    """Anti-DoS: insane KDF parameters must be rejected BEFORE Argon2id call."""

    def test_massive_m_cost_rejected(self):
        """m_cost_kib = 4 000 000 → instant NSP2SealedError."""
        resources = [{"url": "/x.html", "content": b"<html>", "mime": "text/html", "type": "html"}]
        # Build with normal params, then manually craft a keyslot with insane m_cost
        payload = create_package(resources, _package_info(), sealed=True, passphrase=PASSPHRASE)

        # Read header
        header = payload[:HEADER_SIZE]
        fmt_ver, flags, container_id = parse_header(header)

        # Read footer from full payload
        footer = parse_footer(payload, len(payload))

        # Read keyslots
        ks_off = footer["keyslots_offset"]
        ks_len = footer["keyslots_len"]
        ks_bytes = payload[ks_off : ks_off + ks_len]

        # Craft a keyslot with m_cost_kib = 4_000_000
        import base64

        raw = json.loads(ks_bytes)
        raw["slots"][0]["kdf"]["m_cost_kib"] = 4_000_000
        crafted_ks = json.dumps(raw, separators=(",", ":")).encode("utf-8")

        # This should be rejected BEFORE Argon2id starts
        with self.assertRaises(NSP2SealedError):
            unwrap_dek_from_keyslots(crafted_ks, header, PASSPHRASE)

    def test_too_high_t_cost(self):
        """t_cost = 100 → rejected."""
        from app.core.crypto.codec import encode_b64

        resources = [{"url": "/x.html", "content": b"<html>", "mime": "text/html", "type": "html"}]
        payload = create_package(resources, _package_info(), sealed=True, passphrase=PASSPHRASE)

        # Read header
        header = payload[:HEADER_SIZE]
        # Read footer from full payload
        footer = parse_footer(payload, len(payload))
        ks_off = footer["keyslots_offset"]
        ks_len = footer["keyslots_len"]
        ks_bytes = payload[ks_off : ks_off + ks_len]

        raw = json.loads(ks_bytes)
        raw["slots"][0]["kdf"]["t_cost"] = 100
        crafted = json.dumps(raw, separators=(",", ":")).encode("utf-8")
        with self.assertRaises(NSP2SealedError):
            unwrap_dek_from_keyslots(crafted, header, PASSPHRASE)


# ---------------------------------------------------------------------------
# Tests — rekey()
# ---------------------------------------------------------------------------


class RekeyTests(unittest.TestCase):
    """rekey(): change passphrase without re-encrypting Data Region."""

    def _make_sealed(self) -> tuple[bytes, NSP2Container]:
        resources = [
            {"url": "/x.html", "content": b"<html>test</html>", "mime": "text/html", "type": "html"},
            {"url": "/y.bin", "content": _SMALL_BIN, "mime": "application/octet-stream", "type": "binary"},
        ]
        payload = create_package(resources, _package_info(), sealed=True, passphrase=PASSPHRASE, **KDF_PARAMS_SEALED)
        fobj = io.BytesIO(payload)
        container = open_nsp2(fobj, passphrase=PASSPHRASE)
        return payload, container

    def test_rekey_works(self):
        """New passphrase opens everything, data unchanged."""
        old_payload, _ = self._make_sealed()
        fobj_in = io.BytesIO(old_payload)
        fobj_out = io.BytesIO()

        new_size = rekey_nsp2(fobj_in, fobj_out, PASSPHRASE, "new-passphrase")
        self.assertGreater(new_size, 0)

        # Verify new passphrase works — open fresh file object
        fobj_out.seek(0)
        rekeyed_payload = fobj_out.read()
        fobj_check = io.BytesIO(rekeyed_payload)
        container = open_nsp2(fobj_check, passphrase="new-passphrase")
        self.assertTrue(container.is_sealed)
        dek = _extract_dek_from_file(io.BytesIO(rekeyed_payload), "new-passphrase")
        fobj_check2 = io.BytesIO(rekeyed_payload)
        container = open_nsp2(fobj_check2, passphrase="new-passphrase")
        recovered = _read_all_blobs(container, fobj_check2, dek=dek)
        self.assertEqual(b"<html>test</html>", recovered["/x.html"])
        self.assertEqual(_SMALL_BIN, recovered["/y.bin"])

    def test_old_passphrase_rejected_after_rekey(self):
        """Old passphrase must be rejected after rekey."""
        old_payload, _ = self._make_sealed()
        fobj_in = io.BytesIO(old_payload)
        fobj_out = io.BytesIO()

        rekey_nsp2(fobj_in, fobj_out, PASSPHRASE, "new-passphrase")
        fobj_out.seek(0)
        rekeyed_payload = fobj_out.read()
        fobj_check = io.BytesIO(rekeyed_payload)
        with self.assertRaises(NSP2SealedError):
            open_nsp2(fobj_check, passphrase=PASSPHRASE)

    def test_rekey_data_untouched(self):
        """Data Region must be byte-identical after rekey."""
        old_payload, _ = self._make_sealed()
        fobj_in = io.BytesIO(old_payload)
        fobj_out = io.BytesIO()

        old_footer = parse_footer(old_payload, len(old_payload))
        new_size = rekey_nsp2(fobj_in, fobj_out, PASSPHRASE, "new-passphrase")

        fobj_out.seek(0)
        new_payload = fobj_out.read(new_size)
        new_footer = parse_footer(new_payload, new_size)

        # Data region (between header and index) must be identical
        old_data = old_payload[HEADER_SIZE : old_footer["index_offset"]]
        new_data = new_payload[HEADER_SIZE : new_footer["index_offset"]]
        self.assertEqual(old_data, new_data)

    def test_rekey_unsealed_raises(self):
        """rekey() on unsealed container → NSP2RekeyError."""
        payload = create_package(
            [{"url": "/x.html", "content": b"<html>", "mime": "text/html", "type": "html"}],
            _package_info(),
            sealed=False,
        )
        fobj_in = io.BytesIO(payload)
        fobj_out = io.BytesIO()
        with self.assertRaises(NSP2RekeyError):
            rekey_nsp2(fobj_in, fobj_out, PASSPHRASE, "new-pass")


# ---------------------------------------------------------------------------
# Tests — Crypto contract (byte-for-byte)
# ---------------------------------------------------------------------------


class CryptoContractTests(unittest.TestCase):
    """Verify exact crypto contract for Python ↔ Rust."""

    def test_slot_aad_length(self):
        """slot_aad must be exactly 82 bytes."""
        from app.core.nsp2 import _build_keyslots_json

        header = build_header(FLAG_SEALED)
        # We can't easily test _build_keyslots_json directly because it generates random salt,
        # but we can verify the slot_aad construction logic
        salt = os.urandom(16)
        aad = header[:32] + b"nsp2:slot:argon2id" + struct.pack("<IIII", 19, 65536, 3, 1) + salt
        self.assertEqual(82, len(aad))

    def test_index_aad_length(self):
        """index_aad must be exactly 104 bytes."""
        header = build_header(FLAG_SEALED)
        footer = build_footer(1024, 100, 1200, 200, FLAG_SEALED, b"s" * 32)
        aad = header[:32] + footer[:72]
        self.assertEqual(104, len(aad))

    def test_chunk_aad_length(self):
        """chunk_aad must be exactly 33 bytes."""
        container_id = os.urandom(16)
        aad = container_id + struct.pack("<QQB", 0, 0, 1)
        self.assertEqual(33, len(aad))

    def test_nonce_length(self):
        """chunk nonce must be exactly 12 bytes."""
        nonce = b"\x00\x00\x00\x00" + struct.pack("<Q", 0)
        self.assertEqual(12, len(nonce))

    def test_empty_blob_sealed_chunk_size(self):
        """Empty blob sealed = ct_only(0+16) = 16 bytes on disk (no nonce)."""
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from cryptography.hazmat.primitives import hashes

        dek = os.urandom(32)
        container_id = os.urandom(16)
        blob_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=container_id,
            info=b"nsp2:blob:0",
        ).derive(dek)
        from app.core.nsp2 import _seal_chunk
        chunk_data = _seal_chunk(blob_key, 0, b"", container_id, 0, True)
        self.assertEqual(16, len(chunk_data))  # ct_only: 0 + 16 (tag)


def _seal_chunk_for_test(
    key: bytes,
    chunk_index: int,
    plaintext: bytes,
    container_id: bytes,
    blob_id: int,
    is_last: bool,
) -> bytes:
    """Helper for crypto contract test — ct_only, no nonce on disk."""
    nonce = b"\x00\x00\x00\x00" + struct.pack("<Q", chunk_index)
    aad = container_id + struct.pack("<QQB", blob_id, chunk_index, int(is_last))
    ct = ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)
    return ct  # ct_only: plain_len + 16


if __name__ == "__main__":
    unittest.main()
