"""Golden vectors for the sealed offline-package transfer envelopes (v1).

Written once by this implementation, read by `tests/test_sealed_transfer.py`
through `app.core.crypto` — and later by the desktop client in Rust. Every
random input is a published constant, so a single changed byte in the KDF, the
wrap AAD, the HKDF binding, or either envelope layout fails a test instead of
failing somebody's offline package on their next sync.

Regenerate only when the v1 format intentionally changes (it must not — a new
format gets a new version). Run from the repo root:

    uv run python tests/fixtures/write_sealed_vectors.py
"""

import json
from pathlib import Path

from app.core.crypto import transfer as t
from app.core.crypto.codec import decode_b64

PACKAGE_ID = "vault_sealed"
PASSPHRASE = "correct horse battery staple"
WRONG_PASSPHRASE = "not the passphrase"
ITEMS_URL = "/api/vault/sealed/items?package_id=vault_sealed"
MEDIA_URL = "/api/vault/sealed/media/9?package_id=vault_sealed"


def hx(raw: bytes) -> str:
    return raw.hex()


def main() -> None:
    dek = bytes(range(32))
    salt = bytes(range(16))
    wrap_nonce = bytes(range(12))
    small_nonce = bytes(range(12))
    file_nonce = bytes(range(8))

    fragment = t.wrap_package_dek(dek, PASSPHRASE, PACKAGE_ID, salt=salt, nonce=wrap_nonce)
    kek = t.derive_transfer_kek(PASSPHRASE, salt, **t.transfer_kdf_params())
    assert t.unwrap_package_dek(fragment, PASSPHRASE, PACKAGE_ID) == dek

    item_key = t.transfer_resource_key(dek, PACKAGE_ID, ITEMS_URL)
    media_key = t.transfer_resource_key(dek, PACKAGE_ID, MEDIA_URL)

    small_plain = b'{"items":[{"id":9,"title":"a sealed note"}]}'
    small_blob = t.seal_small_resource(item_key, ITEMS_URL, small_plain, nonce=small_nonce)
    assert t.open_small_resource(item_key, ITEMS_URL, small_blob) == small_plain

    media_plain = bytes((i * 7) % 256 for i in range(3 * 1024 * 1024 + 12345))
    chunked = t.seal_chunked_resource(media_key, MEDIA_URL, media_plain, file_nonce=file_nonce)
    file_nonce_out, chunk_size, plaintext_len, chunk_count = t.parse_chunked_header(chunked)
    assert (file_nonce_out, chunk_size, plaintext_len) == (
        file_nonce,
        t.TRANSFER_CHUNK_SIZE,
        len(media_plain),
    )
    # A first-bytes range, a cross-chunk range, and the tail.
    assert b"".join(t.open_chunked_range(media_key, MEDIA_URL, chunked, 0, 100)) == media_plain[:100]
    span = t.TRANSFER_CHUNK_SIZE - 10
    assert (
        b"".join(t.open_chunked_range(media_key, MEDIA_URL, chunked, span, 40))
        == media_plain[span : span + 40]
    )
    assert (
        b"".join(t.open_chunked_range(media_key, MEDIA_URL, chunked, len(media_plain) - 7, 999))
        == media_plain[-7:]
    )

    def stored_chunk(index: int) -> bytes:
        """The exact stored bytes (ciphertext+tag) of one chunk."""
        if index == chunk_count - 1:
            last_len = len(media_plain) - index * chunk_size
            start = t.TRANSFER_HEADER_SIZE + index * (chunk_size + 16)
            return chunked[start : start + last_len + 16]
        start = t.TRANSFER_HEADER_SIZE + index * (chunk_size + 16)
        return chunked[start : start + chunk_size + 16]

    def plain_chunk(index: int) -> bytes:
        return media_plain[index * chunk_size : (index + 1) * chunk_size]

    # Chunk probes pin the framing without shipping megabytes of hex: the test
    # re-seals the same deterministic plaintext and must reproduce these bytes.
    # Slices, not whole chunks — full-open behavior is covered by tests that seal
    # locally; the golden file pins exact layout, nonces and tags.
    probes = [
        {
            "index": index,
            "stored_hex": hx(stored_chunk(index)[:64]),
            "plain_hex": hx(plain_chunk(index)[:64]),
        }
        for index in (0, 1, chunk_count - 1)
    ]

    vectors = {
        "_comment": (
            "Golden vectors for sealed offline packages (transfer v1). Deterministic: "
            "salt, nonces and keys are published constants. Rust desktop client reads "
            "this file too — field names are the contract."
        ),
        "package_id": PACKAGE_ID,
        "passphrase": PASSPHRASE,
        "wrong_passphrase": WRONG_PASSPHRASE,
        "kdf_params": t.transfer_kdf_params(),
        "salt_hex": hx(salt),
        "kek_hex": hx(kek),
        "dek_hex": hx(dek),
        "wrap_aad_hex": hx(t.transfer_wrap_aad(PACKAGE_ID, t.transfer_kdf_params(), salt)),
        "sealing": {
            "version": fragment["version"],
            "kdf": fragment["kdf"],
            "salt": fragment["salt"],
            "wrapped_dek": fragment["wrapped_dek"],
        },
        "items_url": ITEMS_URL,
        "items_key_hex": hx(item_key),
        "small": {
            "plaintext_hex": hx(small_plain),
            "blob_hex": hx(small_blob),
        },
        "media_url": MEDIA_URL,
        "media_key_hex": hx(media_key),
        "chunked": {
            "plaintext_formula": "(i * 7) % 256 for i in range(plaintext_len)",
            "plaintext_len": len(media_plain),
            "chunk_size": chunk_size,
            "chunk_count": chunk_count,
            "file_nonce_hex": hx(file_nonce),
            "header_hex": hx(chunked[: t.TRANSFER_HEADER_SIZE]),
            "probes": probes,
        },
    }
    # Sanity: the JSON must decode back through the same paths the tests use.
    assert decode_b64(vectors["sealing"]["salt"]) == salt
    out = Path(__file__).parent / "sealed_transfer_vectors.json"
    out.write_text(json.dumps(vectors, indent=2) + "\n")
    print(f"wrote {out} ({len(chunked)} sealed media bytes)")


if __name__ == "__main__":
    main()
