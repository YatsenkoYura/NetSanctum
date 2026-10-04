"""The inbox envelope, version 2.

v1 derived the item-wrapping key as `sha256("nsi:v1:" || shared)`: a prefix
concatenated with a secret, where a KDF belongs, binding neither party and
naming neither the collection nor the card. v2 runs the shared secret through
HKDF with both public keys and both ids in the `info`, carries a version byte,
and uses the same buffer as the AEAD's associated data.

The rule that matters most here is backwards compatibility: a vault written
before the change must open after it, forever. So these tests pin three things
— a genuine v1 record still opens, a new write is v2, and a v1 row is upgraded
by its next edit without anything walking the table.
"""

import base64
import hashlib
import json
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.modules.vault.crypto import (
    INBOX_PREFIX,
    INBOX_PREFIX_V2,
    INBOX_VERSION_V2,
    SealedWrite,
    generate_inbox_keypair,
    inbox_binding,
    inbox_write_version,
    open_from_inbox,
    seal_for_inbox,
)
from app.modules.vault.models import Base, VaultCollection, VaultItem
from app.modules.vault.sealing import open_item, seal_item


def legacy_write(payload: bytes, public_key: bytes, *, context: bytes) -> SealedWrite:
    """Build a record exactly the way v1 did, so v1 reading is genuinely tested."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
        X25519PublicKey,
    )

    from app.modules.vault.crypto import new_data_key, seal

    ephemeral_private, ephemeral_public = generate_inbox_keypair()
    shared = X25519PrivateKey.from_private_bytes(ephemeral_private).exchange(
        X25519PublicKey.from_public_bytes(public_key)
    )
    kek = hashlib.sha256(INBOX_PREFIX.encode("ascii") + shared).digest()
    item_key = new_data_key()
    sealed = seal(kek, item_key, context=context)
    return SealedWrite(
        payload=seal(item_key, payload, context=context),
        wrapped_key=INBOX_PREFIX + base64.urlsafe_b64encode(ephemeral_public + sealed.encode()).decode(),
    )


class EnvelopeV2Tests(unittest.TestCase):
    def setUp(self):
        self.private, self.public = generate_inbox_keypair()

    def seal(self, payload=b"capture", **kwargs):
        params = {"collection_id": 7, "kind": "item", "row_id": 1}
        params.update(kwargs)
        return seal_for_inbox(payload, self.public, **params)

    def open(self, write, **kwargs):
        params = {"collection_id": 7, "kind": "item", "row_id": 1}
        params.update(kwargs)
        return open_from_inbox(write, self.private, **params)

    def test_a_new_write_is_the_current_version(self):
        write = self.seal()

        self.assertTrue(write.wrapped_key.startswith(INBOX_PREFIX_V2))
        self.assertEqual(2, inbox_write_version(write.wrapped_key))
        raw = base64.urlsafe_b64decode(write.wrapped_key.removeprefix(INBOX_PREFIX_V2))
        self.assertEqual(INBOX_VERSION_V2, raw[0], "the record says which version it is")

    def test_it_round_trips(self):
        self.assertEqual(b"capture", self.open(self.seal()))

    def test_a_v1_record_still_opens(self):
        """The promise: a passphrase that opens a vault today opens it after this."""
        from app.modules.vault.crypto import context_for

        write = legacy_write(b"old capture", self.public, context=context_for("item", 1))

        self.assertEqual(b"old capture", self.open(write))

    def test_the_reader_rejects_an_unknown_version(self):
        write = self.seal()
        raw = bytearray(base64.urlsafe_b64decode(write.wrapped_key.removeprefix(INBOX_PREFIX_V2)))
        raw[0] = 9
        forged = SealedWrite(
            payload=write.payload,
            wrapped_key=INBOX_PREFIX_V2 + base64.urlsafe_b64encode(bytes(raw)).decode(),
        )

        with self.assertRaises(ValueError) as caught:
            self.open(forged)
        self.assertIn("version", str(caught.exception))

    def test_a_truncated_record_is_malformed(self):
        write = self.seal()
        raw = base64.urlsafe_b64decode(write.wrapped_key.removeprefix(INBOX_PREFIX_V2))
        short = SealedWrite(
            payload=write.payload,
            wrapped_key=INBOX_PREFIX_V2 + base64.urlsafe_b64encode(raw[:-1]).decode(),
        )

        with self.assertRaises(ValueError):
            self.open(short)

    def test_a_record_of_another_row_does_not_open(self):
        write = self.seal()

        with self.assertRaises(ValueError):
            self.open(write, row_id=2)

    def test_a_record_of_another_collection_does_not_open(self):
        write = self.seal()

        with self.assertRaises(ValueError):
            self.open(write, collection_id=8)

    def test_another_vaults_key_does_not_open_it(self):
        write = self.seal()
        _other_private, other_public = generate_inbox_keypair()
        write_against_other = seal_for_inbox(b"capture", other_public, collection_id=7, kind="item", row_id=1)

        with self.assertRaises(ValueError):
            self.open(write_against_other)
        self.assertNotEqual(write.wrapped_key, write_against_other.wrapped_key)

    def test_editing_the_recorded_recipient_key_does_not_open_it(self):
        """The copy inside the record is not identity: editing it changes the key."""
        write = self.seal()
        raw = bytearray(base64.urlsafe_b64decode(write.wrapped_key.removeprefix(INBOX_PREFIX_V2)))
        _victim_private, victim_public = generate_inbox_keypair()
        raw[33:65] = victim_public
        forged = SealedWrite(
            payload=write.payload,
            wrapped_key=INBOX_PREFIX_V2 + base64.urlsafe_b64encode(bytes(raw)).decode(),
        )

        with self.assertRaises(ValueError):
            self.open(forged)

    def test_editing_the_ephemeral_key_does_not_open_it(self):
        write = self.seal()
        raw = bytearray(base64.urlsafe_b64decode(write.wrapped_key.removeprefix(INBOX_PREFIX_V2)))
        _other_private, other_public = generate_inbox_keypair()
        raw[1:33] = other_public
        forged = SealedWrite(
            payload=write.payload,
            wrapped_key=INBOX_PREFIX_V2 + base64.urlsafe_b64encode(bytes(raw)).decode(),
        )

        with self.assertRaises(ValueError):
            self.open(forged)

    def test_a_tampered_payload_is_refused(self):
        write = self.seal()
        raw = bytearray(base64.urlsafe_b64decode(write.payload.removeprefix("nsp:v1:")))
        raw[-1] ^= 0xFF
        forged = SealedWrite(
            payload="nsp:v1:" + base64.urlsafe_b64encode(bytes(raw)).decode(),
            wrapped_key=write.wrapped_key,
        )

        with self.assertRaises(ValueError):
            self.open(forged)

    def test_two_writes_to_one_row_use_different_keys(self):
        first, second = self.seal(b"one"), self.seal(b"two")

        self.assertNotEqual(first.wrapped_key, second.wrapped_key)
        self.assertNotEqual(first.payload, second.payload)

    def test_the_binding_is_length_framed(self):
        """Two different id pairs must not spell the same bytes."""
        self.assertNotEqual(
            inbox_binding(1, "item", 23, b"e" * 32, b"r" * 32),
            inbox_binding(12, "item", 3, b"e" * 32, b"r" * 32),
        )

    def test_sealing_needs_a_collection(self):
        with self.assertRaises(ValueError):
            seal_for_inbox(b"x", self.public, collection_id=None, kind="item", row_id=1)  # type: ignore[arg-type]


class LazyUpgradeTests(unittest.TestCase):
    """A v1 row moves to v2 when it is next written — and nothing else does it."""

    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.session = sessionmaker(bind=engine)()
        self.private, self.public = generate_inbox_keypair()
        self.collection = VaultCollection(
            id=1, name="Скрытое", is_encrypted=True, public_name="Проект Б", color="teal"
        )
        self.collection.inbox_public_key = base64.b64encode(self.public).decode()
        self.session.add(self.collection)
        self.session.commit()

    def _v1_item(self, item_id=7):
        from app.modules.vault.crypto import context_for

        item = VaultItem(
            id=item_id,
            entry_type="bookmark",
            title="Личное название",
            content="Личный текст",
            tags=["secret"],
            collection_id=self.collection.id,
        )
        self.session.add(item)
        self.session.commit()
        # Written the old way, so the row genuinely carries a v1 record.
        write = legacy_write(
            json.dumps({"title": item.title, "content": item.content}, ensure_ascii=False).encode(),
            self.public,
            context=context_for("item", item.id),
        )
        item.sealed_payload = write.payload
        item.wrapped_key = write.wrapped_key
        item.title = ""
        item.tags = []
        self.session.commit()
        return item

    def test_a_v1_row_opens(self):
        item = self._v1_item()

        open_item(self.private, item)

        self.assertEqual("Личное название", item.title)
        self.assertEqual(1, inbox_write_version(item.wrapped_key), "opening must not rewrite it")

    def test_the_next_write_moves_it_to_v2(self):
        item = self._v1_item()
        open_item(self.private, item)

        seal_item(item, self.public)
        self.session.commit()

        self.assertEqual(2, inbox_write_version(item.wrapped_key))
        reopened = self.session.get(VaultItem, item.id)
        open_item(self.private, reopened)
        self.assertEqual("Личное название", reopened.title)

    def test_the_payload_envelope_is_not_the_thing_that_changed(self):
        """Only the key wrapping moves. The payload keeps its own format, and
        each write still gets a fresh item key — so the ciphertext differs
        while the content does not."""
        item = self._v1_item()
        open_item(self.private, item)
        before = item.sealed_payload

        seal_item(item, self.public)

        self.assertTrue(before.startswith("nsp:v1:"))
        self.assertTrue(item.sealed_payload.startswith("nsp:v1:"))
        reopened = self.session.get(VaultItem, item.id)
        open_item(self.private, reopened)
        self.assertEqual("Личное название", reopened.title)
        self.assertEqual("Личный текст", reopened.content)
