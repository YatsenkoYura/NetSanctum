"""Capture the current wire format before the KDF moves into core.

The move is supposed to change where code lives and nothing else. These vectors
are how that gets checked: written by the implementation as it stands now, and
read by the test suite after the move. If a byte of the AAD construction, the
binding layout, the MAC or an actual wrapped key changes, a wrapper somebody
already has stops opening — which for a sealed vault is not a bug report but a
loss.

Deterministic inputs only: the salt, nonce and ephemeral keys are injected by
hand where the function allows it, and the wrapper's own randomness is captured
whole so it can be unwrapped verbatim rather than re-derived.
"""

import base64
import importlib.util
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

# The revision whose `app/modules/vault/crypto.py` predates the move to core.
PRE_MOVE_REVISION = "2dadb74"


def load_pre_move_crypto():
    """Import the KDF as it stood before it moved, from git rather than from disk.

    Loading it any other way would defeat the purpose: vectors written by the
    moved code can only prove the moved code agrees with itself. These are read
    back through `app.core.crypto`, so what they prove is that core opens bytes
    written before core existed.
    """
    source = subprocess.run(
        ["git", "show", f"{PRE_MOVE_REVISION}:app/modules/vault/crypto.py"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "crypto.py"
        path.write_text(source)
        spec = importlib.util.spec_from_file_location("pre_move_crypto", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


crypto = load_pre_move_crypto()
canonical_params = crypto.canonical_params
context_for = crypto.context_for
inbox_binding = crypto.inbox_binding
inbox_pub_fingerprint = crypto.inbox_pub_fingerprint
inbox_pub_mac = crypto.inbox_pub_mac
seal = crypto.seal
seal_for_inbox = crypto.seal_for_inbox
unwrap_data_key = crypto.unwrap_data_key
wrap_aad = crypto.wrap_aad
wrap_data_key = crypto.wrap_data_key

OUT = Path("tests/fixtures/crypto_wire_vectors.json")


def deterministic_urandom(count: int) -> bytes:
    """A fixed byte stream, so a wrapper is a function of its inputs alone.

    Only ever used to write these vectors. The salt and nonce below are then
    published on purpose: a fixture that hid its randomness could only be checked
    by round-tripping through the same code that wrote it.
    """
    stream = bytes(range(256)) * 8
    return stream[:count]


def main() -> None:
    passphrase = "correct horse battery staple"
    dek = bytes(range(32))

    # A real pair, derived from fixed bytes rather than generated: `generate`
    # draws from the cryptography backend, which this fixture does not patch, and
    # a private key that does not match the public half fails to open for a
    # reason that has nothing to do with the move.
    inbox_private = bytes(range(32, 64))
    inbox_public = (
        X25519PrivateKey.from_private_bytes(inbox_private)
        .public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
    )

    with patch.object(crypto.os, "urandom", deterministic_urandom):
        wrapped = wrap_data_key(
            dek,
            passphrase,
            context=context_for("collection", 5),
            t_cost=2,
            m_cost=64 * 1024,
            parallelism=4,
        )
        sealed_value = seal(dek, b"a payload in the clear once", context=context_for("item", 91))
        write = seal_for_inbox(
            b"the payload nobody else may read", inbox_public, collection_id=5, kind="item", row_id=91
        )

    fixed = wrapped
    fixed_aad = wrap_aad(
        context_for("collection", 5),
        kdf="argon2id",
        params=fixed.params(),
        salt=base64.urlsafe_b64decode(fixed.salt.encode("ascii")),
    )

    binding = inbox_binding(5, "item", 91, inbox_public, inbox_public)

    vectors = {
        "_comment": (
            "Written by tests/fixtures/write_crypto_vectors.py from app/modules/vault/crypto.py at "
            f"revision {PRE_MOVE_REVISION}, before the KDF moved to app/core/crypto. "
            "Its randomness is fixed on purpose: the salts, nonces and ephemeral keys are published so the "
            "bytes can be unwrapped by the moved code and compared, rather than only round-tripped through "
            "whatever wrote them."
        ),
        "passphrase": passphrase,
        "dek_hex": dek.hex(),
        "wrapped_key": {
            "salt": fixed.salt,
            "wrapped": fixed.wrapped,
            "kdf": fixed.kdf,
            "wrap_version": fixed.wrap_version,
            "params": fixed.params(),
            "aad_hex": fixed_aad.hex(),
            "unwrap_hex": unwrap_data_key(fixed, passphrase, context=context_for("collection", 5)).hex(),
            "wrong_passphrase": "not the passphrase",
        },
        "pure_functions": {
            "canonical_params": base64.b64encode(canonical_params(fixed.params())).decode(),
            "context_for_item_91": base64.b64encode(context_for("item", 91)).decode(),
            "inbox_binding_5_item_91": base64.b64encode(binding).decode(),
        },
        "pub_mac": {
            "kek_hex": bytes(range(96, 128)).hex(),
            "public_hex": inbox_public.hex(),
            "collection_id": 5,
            "mac": inbox_pub_mac(bytes(range(96, 128)), inbox_public, 5),
            "fingerprint": inbox_pub_fingerprint(inbox_public),
        },
        "sealed_payload": {
            "dek_hex": dek.hex(),
            "context_hex": context_for("item", 91).hex(),
            "value": sealed_value,
        },
        "blind_write": {
            "private_hex": inbox_private.hex(),
            "payload": write.payload,
            "wrapped_key": write.wrapped_key,
            "collection_id": 5,
            "kind": "item",
            "row_id": 91,
        },
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(vectors, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
