"""Each sealed collection encrypts its pictures under a key of its own.

The application file key used to protect every sealed picture — the same key
that protects every other module's files. A file key per collection narrows
that, and it is derived, not stored: `HKDF(inbox_private, salt=collection,
info=file-key:v1)`. No column, no backfill, no missing-key state. These tests
hold the derivation, the healing of pre-file-key images, and the file
endpoints' gate — including the negative half: a wrong key opens nothing, a
tampered envelope fails, a locked vault refuses, and no dump carries secrets.
"""

import asyncio
import base64
import io
import json
import unittest
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.storage import LocalStorage
from app.modules.vault import sealing
from app.modules.vault.crypto import (
    context_for,
    derive_file_key,
    generate_inbox_keypair,
    new_data_key,
    wrap_data_key,
)
from app.modules.vault.models import VaultCollection, VaultItem
from app.modules.vault.sealing import (
    file_key_for,
    file_key_for_write,
    heal_collection_images,
    open_handoff_token,
    seal_handoff_token,
    store_wrapper,
    unlock_collection,
    verify_file_url,
    verify_file_url_signature,
)

FAST = {"t_cost": 1, "m_cost": 8, "parallelism": 1}


class AsyncSessionAdapter:
    """The slice of AsyncSession the sealing code touches."""

    def __init__(self, session):
        self.session = session

    def add(self, instance):
        self.session.add(instance)

    async def execute(self, statement, parameters=None):
        return self.session.execute(statement, parameters or {})

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def flush(self):
        self.session.flush()

    async def refresh(self, instance):
        self.session.refresh(instance)

    async def get(self, model, ident):
        return self.session.get(model, ident)

    async def scalar(self, statement, parameters=None):
        return self.session.execute(statement, parameters or {}).scalar()


class FakeRedis:
    """In-memory stand-in: the tests prove the lock logic, not Redis."""

    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def getdel(self, key):
        return self.store.pop(key, None)

    async def mget(self, keys):
        return [self.store.get(key) for key in keys]

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def setex(self, key, seconds, value):
        self.store[key] = value

    async def incr(self, key):
        self.store[key] = int(self.store.get(key) or 0) + 1
        return self.store[key]

    async def expire(self, key, seconds):
        return True

    async def delete(self, key):
        self.store.pop(key, None)


def make_sealed_row(db, collection_id=5, passphrase="правильная лошадь, скрепка"):
    private, public = generate_inbox_keypair()
    row = VaultCollection(id=collection_id, name="Приватное", is_encrypted=True, public_name="Н")
    row.inbox_public_key = base64.b64encode(public).decode()
    store_wrapper(
        row, wrap_data_key(private, passphrase, context=context_for("collection", collection_id), **FAST)
    )
    db.add(row)
    db.commit()
    return row, private


class FileKeyDerivationTests(unittest.TestCase):
    def test_the_same_collection_always_derives_the_same_key(self):
        private = new_data_key()

        self.assertEqual(derive_file_key(private, 5), derive_file_key(private, 5))
        self.assertEqual(32, len(derive_file_key(private, 5)))

    def test_collections_do_not_share_file_keys(self):
        private = new_data_key()

        self.assertNotEqual(derive_file_key(private, 5), derive_file_key(private, 6))

    def test_vaults_do_not_share_file_keys(self):
        self.assertNotEqual(derive_file_key(new_data_key(), 5), derive_file_key(new_data_key(), 5))

    def test_a_short_key_is_refused(self):
        with self.assertRaises(ValueError):
            derive_file_key(b"short", 5)


class FileKeyLifecycleTests(unittest.TestCase):
    """No backfill: an old row yields its file key on the first unlock."""

    def setUp(self):
        self.engine = sa.create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.addCleanup(self.engine.dispose)
        self.session = AsyncSessionAdapter(self.db)
        self.redis = FakeRedis()
        entered = patch.object(sealing, "redis_client", self.redis)
        entered.start()
        self.addCleanup(entered.stop)

    def test_an_old_row_needs_no_backfill(self):
        row, private = make_sealed_row(self.db)
        expected = derive_file_key(private, row.id)

        token = asyncio.run(unlock_collection(row, "правильная лошадь, скрепка", "tab", session=self.session))

        self.assertEqual(expected, asyncio.run(file_key_for(row, token)))

    def test_a_locked_vault_has_no_file_key(self):
        row, _private = make_sealed_row(self.db)

        self.assertIsNone(asyncio.run(file_key_for(row, "")))
        self.assertIsNone(asyncio.run(file_key_for(row, "no-such-token")))
        self.assertIsNone(asyncio.run(file_key_for(None, "tab")))

    def test_file_key_for_write_reports_the_lock(self):
        row, _private = make_sealed_row(self.db)

        key, locked = asyncio.run(file_key_for_write(row, ""))
        self.assertIsNone(key)
        self.assertTrue(locked)

        token = asyncio.run(unlock_collection(row, "правильная лошадь, скрепка", "tab", session=self.session))
        key, locked = asyncio.run(file_key_for_write(row, token))
        self.assertFalse(locked)
        self.assertIsNotNone(key)
        self.assertEqual(32, len(key))

    def test_reading_after_lock_gives_nothing(self):
        """Locking revokes the file key with the session: no re-unlock, no key."""
        from app.modules.vault.sealing import lock_collection

        row, _private = make_sealed_row(self.db)
        token = asyncio.run(unlock_collection(row, "правильная лошадь, скрепка", "tab", session=self.session))
        self.assertIsNotNone(asyncio.run(file_key_for(row, token)))

        asyncio.run(lock_collection(row.id, token))

        self.assertIsNone(asyncio.run(file_key_for(row, token)))


class ImageHealingTests(unittest.TestCase):
    """Application-key images from before the file key move over exactly once."""

    def setUp(self):
        from tempfile import TemporaryDirectory

        self.engine = sa.create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.addCleanup(self.engine.dispose)
        self.session = AsyncSessionAdapter(self.db)
        self.redis = FakeRedis()
        entered = patch.object(sealing, "redis_client", self.redis)
        entered.start()
        self.addCleanup(entered.stop)

        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = LocalStorage(self._tmp.name)
        for target in ("app.core.storage.get_storage", "app.modules.vault.images.get_storage"):
            entered = patch(target, return_value=self.storage)
            entered.start()
            self.addCleanup(entered.stop)

    def test_an_old_image_moves_to_the_file_key_and_is_renamed(self):
        from app.modules.vault.images import media_type_for

        row, _private = make_sealed_row(self.db, collection_id=5)
        payload = b"\x89PNG\r\n" + b"picture" * 100
        old_path = "vault/images/7.png.enc"
        self.storage.save_file_encrypted_seekable(io.BytesIO(payload), old_path)
        item = VaultItem(id=7, entry_type="bookmark", title="", tags=[], collection_id=5, image_path=old_path)
        self.db.add(item)
        self.db.commit()

        token = asyncio.run(unlock_collection(row, "правильная лошадь, скрепка", "tab", session=self.session))
        file_key = asyncio.run(file_key_for(row, token))
        assert file_key is not None

        self.db.expire_all()
        healed = self.db.get(VaultItem, 7)
        assert healed is not None and healed.image_path is not None
        self.assertRegex(healed.image_path, r"^vault/images/7-[0-9a-f]{16}\.png\.enc$")
        self.assertFalse(self.storage.file_exists(old_path))
        self.assertEqual(payload, self.storage.get_file_decrypted(healed.image_path, key=file_key))
        # The application key no longer opens it: the move is a move.
        with self.assertRaises(ValueError):
            self.storage.get_file_decrypted(healed.image_path)
        self.assertEqual("image/png", media_type_for(healed.image_path))

    def test_a_wrong_file_key_opens_neither_new_nor_old(self):
        """A corrupt key fails closed instead of falling through to content."""
        from app.modules.vault.images import image_bytes

        row, _private = make_sealed_row(self.db, collection_id=5)
        payload = b"\x89PNG\r\n" + b"picture" * 100
        old_path = "vault/images/7.png.enc"
        self.storage.save_file_encrypted_seekable(io.BytesIO(payload), old_path)
        item = VaultItem(id=7, entry_type="bookmark", title="", tags=[], collection_id=5, image_path=old_path)
        self.db.add(item)
        self.db.commit()
        asyncio.run(unlock_collection(row, "правильная лошадь, скрепка", "tab", session=self.session))
        self.db.expire_all()
        healed = self.db.get(VaultItem, 7)
        assert healed is not None

        # A wrong file key, then the application fallback behind it: neither
        # opens a file-key file, and the legacy column is empty — so nothing.
        self.assertIsNone(image_bytes(healed, file_key=bytes(32)))

    def test_a_second_unlock_finds_nothing_to_heal(self):
        row, _private = make_sealed_row(self.db, collection_id=6)

        asyncio.run(unlock_collection(row, "правильная лошадь, скрепка", "t1", session=self.session))
        healed = asyncio.run(heal_collection_images(self.session, row, bytes(32)))

        self.assertEqual(0, healed)

    def test_a_plain_collection_is_never_healed(self):
        row = VaultCollection(id=8, name="Обычное", is_encrypted=False)
        self.db.add(row)
        self.db.commit()

        healed = asyncio.run(heal_collection_images(self.session, row, bytes(32)))

        self.assertEqual(0, healed)


class SignedUrlTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        entered = patch.object(sealing, "redis_client", self.redis)
        entered.start()
        self.addCleanup(entered.stop)

    def sign(self, collection_id=3, item_id=41, path="vault/videos/41-ab.enc", epoch=0):
        from app.modules.vault.sealing import sign_file_url

        return sign_file_url(collection_id, item_id, "media", path, epoch)

    def test_a_signed_url_verifies_for_its_own_file(self):
        expires, signature = self.sign()

        self.assertTrue(
            asyncio.run(verify_file_url(3, 41, "media", "vault/videos/41-ab.enc", str(expires), signature))
        )

    def test_a_signature_does_not_travel(self):
        """Bound to the collection, the item and the stored path — not just alive."""
        expires, signature = self.sign()

        for args in (
            (4, 41, "media", "vault/videos/41-ab.enc"),
            (3, 42, "media", "vault/videos/41-ab.enc"),
            (3, 41, "media", "vault/videos/41-other.enc"),
            (3, 41, "image", "vault/videos/41-ab.enc"),
        ):
            self.assertFalse(
                verify_file_url_signature(*args, str(expires), signature, 0), f"travelled to {args}"
            )
        self.assertFalse(
            verify_file_url_signature(
                3, 41, "media", "vault/videos/41-ab.enc", str(expires), signature + "0", 0
            )
        )
        self.assertFalse(
            verify_file_url_signature(3, 41, "media", "vault/videos/41-ab.enc", None, signature, 0)
        )
        self.assertFalse(
            verify_file_url_signature(3, 41, "media", "vault/videos/41-ab.enc", str(expires), None, 0)
        )
        self.assertFalse(
            verify_file_url_signature(3, 41, "media", "vault/videos/41-ab.enc", "not-a-number", signature, 0)
        )

    def test_an_expired_url_is_dead(self):
        from app.modules.vault.sealing import sign_file_url

        expires, signature = sign_file_url(3, 41, "media", "vault/videos/41-ab.enc", 0, now=1000)

        self.assertFalse(
            asyncio.run(verify_file_url(3, 41, "media", "vault/videos/41-ab.enc", str(expires), signature))
        )

    def test_locking_voids_issued_urls(self):
        """The epoch is the token binding: lock bumps it, old signatures die."""
        from app.modules.vault.sealing import bump_media_epoch

        expires, signature = self.sign(epoch=0)
        self.assertTrue(
            asyncio.run(verify_file_url(3, 41, "media", "vault/videos/41-ab.enc", str(expires), signature))
        )

        asyncio.run(bump_media_epoch(3))

        self.assertFalse(
            asyncio.run(verify_file_url(3, 41, "media", "vault/videos/41-ab.enc", str(expires), signature))
        )

    def test_nothing_but_video_gets_signed(self):
        from app.modules.vault.sealing import sign_file_url

        with self.assertRaises(ValueError):
            sign_file_url(3, 41, "image", "vault/images/41.png.enc", 0)
        with self.assertRaises(ValueError):
            sign_file_url(None, 41, "media", "vault/videos/41-ab.enc", 0)


class FileAccessGateTests(unittest.TestCase):
    """The endpoints' lock check, without the HTTP layer around it."""

    def setUp(self):
        self.engine = sa.create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.addCleanup(self.engine.dispose)
        self.session = AsyncSessionAdapter(self.db)
        self.redis = FakeRedis()
        entered = patch.object(sealing, "redis_client", self.redis)
        entered.start()
        self.addCleanup(entered.stop)

    def test_a_plain_collection_needs_no_key(self):
        from app.modules.vault.router import _require_file_access

        row = VaultCollection(id=1, name="Обычное", is_encrypted=False)

        self.assertIsNone(asyncio.run(_require_file_access(row, "")))
        self.assertIsNone(asyncio.run(_require_file_access(None, "")))

    def test_a_locked_vault_refuses_with_423(self):
        from app.modules.vault.router import _require_file_access, _require_media_access

        row, _private = make_sealed_row(self.db)
        item = VaultItem(
            id=7,
            entry_type="bookmark",
            title="",
            tags=[],
            collection_id=5,
            media_path="vault/videos/7-ab.enc",
        )

        for gate in (
            _require_file_access(row, ""),
            _require_media_access(row, item, "", None, None),
        ):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(gate)
            self.assertEqual(423, caught.exception.status_code)

    def test_an_unlocked_vault_serves_with_its_file_key(self):
        from app.modules.vault.router import _require_file_access

        row, private = make_sealed_row(self.db)
        token = asyncio.run(unlock_collection(row, "правильная лошадь, скрепка", "tab", session=self.session))

        file_key = asyncio.run(_require_file_access(row, token))

        self.assertEqual(derive_file_key(private, row.id), file_key)

    def test_a_signed_player_url_serves_without_the_header(self):
        from app.modules.vault.router import _require_media_access
        from app.modules.vault.sealing import sign_file_url

        row, _private = make_sealed_row(self.db)
        item = VaultItem(
            id=7,
            entry_type="bookmark",
            title="",
            tags=[],
            collection_id=5,
            media_path="vault/videos/7-ab.enc",
        )
        expires, signature = sign_file_url(5, 7, "media", "vault/videos/7-ab.enc", 0)

        # No exception: the signature is the authorization here.
        asyncio.run(_require_media_access(row, item, "", signature, str(expires)))

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(_require_media_access(row, item, "", "forged", str(expires)))
        self.assertEqual(423, caught.exception.status_code)

    def test_only_an_unlocked_sealed_card_carries_a_player_url(self):
        from app.modules.vault.router import _apply_lock_state, attach_media_url

        def serialized(item):
            return {"title": item.title, "media_status": item.media_status}

        sealed = VaultItem(
            id=7,
            entry_type="bookmark",
            title="",
            tags=[],
            collection_id=5,
            sealed_payload="nsp:v1:abc",
            public_title="Проект А",
            media_path="vault/videos/7-ab.enc",
            media_status="completed",
        )
        plain = VaultItem(
            id=8, entry_type="bookmark", title="Обычная", tags=[], media_path="vault/videos/8.mp4"
        )

        unlocked = asyncio.run(
            attach_media_url(
                _apply_lock_state(serialized(sealed), sealed, locked=False), sealed, locked=False
            )
        )
        assert unlocked["media_url"] is not None
        self.assertTrue(unlocked["media_url"].startswith("/api/vault/items/7/media?exp="))
        self.assertIn("sig=", unlocked["media_url"])

        locked = asyncio.run(
            attach_media_url(_apply_lock_state(serialized(sealed), sealed, locked=True), sealed, locked=True)
        )
        self.assertIsNone(locked["media_url"])

        self.assertIsNone(
            asyncio.run(
                attach_media_url(
                    _apply_lock_state(serialized(plain), plain, locked=False), plain, locked=False
                )
            )["media_url"]
        )

    def test_the_response_model_carries_the_player_url(self):
        from app.modules.vault.schemas import VaultItemResponse

        self.assertIn("media_url", VaultItemResponse.model_fields)

    def test_a_player_url_grant_round_trips(self):
        from app.modules.vault.sealing import load_media_key_grant, store_media_key_grant

        key = new_data_key()

        asyncio.run(store_media_key_grant("sig-abc", key))

        self.assertEqual(key, asyncio.run(load_media_key_grant("sig-abc")))
        self.assertIsNone(asyncio.run(load_media_key_grant("sig-other")))
        self.assertIsNone(asyncio.run(load_media_key_grant("")))

    def test_a_bad_grant_opens_nothing(self):
        from app.modules.vault.sealing import load_media_key_grant

        self.redis.store["vault_media_key:sig-bad"] = "!!!not-base64!!!"
        self.redis.store["vault_media_key:sig-short"] = base64.b64encode(b"short").decode()

        self.assertIsNone(asyncio.run(load_media_key_grant("sig-bad")))
        self.assertIsNone(asyncio.run(load_media_key_grant("sig-short")))

    def test_file_key_for_id_matches_the_session_record(self):
        from app.modules.vault.sealing import file_key_for_id

        row, private = make_sealed_row(self.db)

        self.assertIsNone(asyncio.run(file_key_for_id(row.id, "")))
        self.assertIsNone(asyncio.run(file_key_for_id(row.id, "no-token")))

        token = asyncio.run(unlock_collection(row, "правильная лошадь, скрепка", "tab", session=self.session))

        self.assertEqual(derive_file_key(private, row.id), asyncio.run(file_key_for_id(row.id, token)))

    def test_a_minted_player_url_carries_a_matching_key_grant(self):
        from app.modules.vault.router import _apply_lock_state, attach_media_url
        from app.modules.vault.sealing import file_key_for_id, load_media_key_grant

        row, _private = make_sealed_row(self.db)
        item = VaultItem(
            id=7,
            entry_type="bookmark",
            title="",
            tags=[],
            collection_id=5,
            sealed_payload="nsp:v1:abc",
            public_title="Проект А",
            media_path="vault/videos/7-ab.enc",
            media_status="completed",
        )
        token = asyncio.run(unlock_collection(row, "правильная лошадь, скрепка", "tab", session=self.session))

        payload = asyncio.run(
            attach_media_url(
                _apply_lock_state({"media_status": item.media_status}, item, locked=False),
                item,
                locked=False,
                unlock_token=token,
            )
        )

        signature = payload["media_url"].split("sig=")[1]
        self.assertEqual(
            asyncio.run(file_key_for_id(5, token)),
            asyncio.run(load_media_key_grant(signature)),
        )


class PlayerStreamingTests(unittest.TestCase):
    """The player path end to end: signed URL, no unlock header, real bytes out.

    A `<video>` element cannot send the unlock header, so the endpoint must
    still open a file that lives under the collection's file key — via the
    grant minted with the URL. A regression here shows as a broken player
    icon and an aborted HTTP/2 stream, not a test failure, so it stays a test.
    """

    def setUp(self):
        from tempfile import TemporaryDirectory

        self.engine = sa.create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.addCleanup(self.engine.dispose)
        self.session = AsyncSessionAdapter(self.db)
        self.redis = FakeRedis()
        entered = patch.object(sealing, "redis_client", self.redis)
        entered.start()
        self.addCleanup(entered.stop)
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = LocalStorage(self._tmp.name)

    def _player_request(self, range_header=None):
        from unittest.mock import Mock

        request = Mock()
        request.headers.get = lambda name, default=None: range_header if name == "range" else default
        return request

    def _setup_card(self):
        row, private = make_sealed_row(self.db)
        token = asyncio.run(unlock_collection(row, "правильная лошадь, скрепка", "tab", session=self.session))
        file_key = derive_file_key(private, row.id)
        payload = b"\x00\x00\x00\x18ftypmp42" + b"media" * 200000
        path = "vault/5/7/00000000000000000000000000000000aa.mp4.enc"
        self.storage.save_file_encrypted_seekable(
            io.BytesIO(payload), path, key=file_key, length=len(payload)
        )
        item = VaultItem(
            id=7,
            entry_type="bookmark",
            title="",
            tags=[],
            collection_id=5,
            sealed_payload="nsp:v1:x",
            media_path=path,
            media_size=len(payload),
            media_status="completed",
            node_type="video",
        )
        self.db.add(item)
        self.db.commit()
        patches = {
            "app.modules.vault.router.get_storage": patch(
                "app.modules.vault.router.get_storage", return_value=self.storage
            ),
            "app.modules.vault.router.get_vault_item": patch(
                "app.modules.vault.router.get_vault_item",
                new=lambda _db, item_id: _async_get(self.db, VaultItem, item_id),
            ),
            "app.modules.vault.router.collection_for": patch(
                "app.modules.vault.router.collection_for",
                new=lambda _db, collection_id: _async_get(self.db, VaultCollection, collection_id),
            ),
        }
        for p in patches.values():
            p.start()
            self.addCleanup(p.stop)
        return token, payload, path

    def test_a_signed_url_streams_the_collection_file_key_bytes(self):
        from app.modules.vault.router import _apply_lock_state, attach_media_url, get_item_media

        token, payload, _path = self._setup_card()
        item = self.db.get(VaultItem, 7)
        url_payload = asyncio.run(
            attach_media_url(
                _apply_lock_state({"media_status": item.media_status}, item, locked=False),
                item,
                locked=False,
                unlock_token=token,
            )
        )
        signature = url_payload["media_url"].split("sig=")[1]
        expires = url_payload["media_url"].split("exp=")[1].split("&")[0]

        response = asyncio.run(
            get_item_media(
                7,
                self._player_request("bytes=0-1023"),
                None,
                user=None,
                unlock_token="",
                sig=signature,
                exp=expires,
            )
        )

        self.assertEqual(206, response.status_code)
        chunks = []

        async def drain():
            async for chunk in response.body_iterator:
                chunks.append(chunk)

        asyncio.run(drain())
        self.assertEqual(payload[:1024], b"".join(chunks))

    def test_a_missing_grant_is_a_423_not_an_aborted_stream(self):
        from app.modules.vault.router import get_item_media, sign_file_url

        _token, _payload, path = self._setup_card()
        expires, signature = sign_file_url(5, 7, "media", path, 0)

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                get_item_media(
                    7,
                    self._player_request("bytes=0-1023"),
                    None,
                    user=None,
                    unlock_token="",
                    sig=signature,
                    exp=str(expires),
                )
            )
        self.assertEqual(423, caught.exception.status_code)


async def _async_get(db, model, ident):
    return db.get(model, ident)


class HandoffTokenTests(unittest.TestCase):
    """The worker's key travels sealed in the handoff, never in task arguments."""

    def test_a_token_box_round_trips(self):
        box = seal_handoff_token("handoff-1", "tab-token")

        self.assertEqual("tab-token", open_handoff_token("handoff-1", box))

    def test_a_box_does_not_open_under_another_handoff(self):
        box = seal_handoff_token("handoff-1", "tab-token")

        self.assertIsNone(open_handoff_token("handoff-2", box))
        self.assertIsNone(open_handoff_token("handoff-1", box + "A"))
        self.assertIsNone(open_handoff_token("handoff-1", None))
        self.assertIsNone(open_handoff_token("", box))

    def test_the_handoff_value_carries_no_plaintext_secrets(self):
        url = "https://example.com/watch?v=secretvideo"
        payload = {"url": url, "title": "t", "token_box": seal_handoff_token("h1", "tab-token")}
        stored = json.dumps(payload)

        # The url and title are short-lived cleartext by design (30-minute TTL,
        # GETDEL on read); the token never is, in any form.
        self.assertNotIn("tab-token", stored)

    def test_queued_task_arguments_name_no_content(self):
        """The broker is the same Redis with AOF on: arguments are disk."""
        from app.modules.vault import services

        seen = {}

        async def fake_dispatch(task, client, prefix, record, kwargs=None):
            seen["kwargs"] = kwargs
            seen["record"] = record

            class FakeTask:
                id = "task-id"

            return FakeTask()

        async def run():
            with (
                patch("app.modules.vault.services.redis_client", FakeRedis()),
                patch("app.core.task_dispatch.dispatch_tracked_async", fake_dispatch),
            ):
                await services.queue_video_download(
                    AsyncMock(),
                    7,
                    "https://example.com/watch?v=secretvideo",
                    quality="720",
                    title="Секретный выпуск",
                    unlock_token="tab-token",
                )

        asyncio.run(run())

        blob = json.dumps(seen["kwargs"]) + json.dumps(seen["record"])
        self.assertNotIn("secretvideo", blob)
        self.assertNotIn("Секретный выпуск", blob)
        self.assertNotIn("tab-token", blob)
        self.assertIn("handoff", seen["kwargs"])

    def test_resolving_without_a_token_yields_no_key(self):
        from app.modules.vault.services import resolve_download_file_key

        self.assertIsNone(resolve_download_file_key("h1", {}, 7))
        self.assertIsNone(resolve_download_file_key("h1", {"token_box": "garbage"}, 7))


class DumpSecrecyTests(unittest.TestCase):
    """What a Redis dump or a log line may contain: everything except secrets."""

    def setUp(self):
        self.engine = sa.create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.addCleanup(self.engine.dispose)
        self.session = AsyncSessionAdapter(self.db)
        self.redis = FakeRedis()
        entered = patch.object(sealing, "redis_client", self.redis)
        entered.start()
        self.addCleanup(entered.stop)

    def test_the_session_record_is_not_the_key(self):
        row, private = make_sealed_row(self.db)
        asyncio.run(unlock_collection(row, "правильная лошадь, скрепка", "tab", session=self.session))

        records = [value for key, value in self.redis.store.items() if key.startswith("vault_key:")]
        self.assertTrue(records)
        for record in records:
            # The old format stored the key hex in the clear; the record must
            # hold only an encrypted blob plus its metadata.
            self.assertNotIn(private.hex(), record)
            parsed = json.loads(record)
            self.assertEqual(1, parsed["v"])
            self.assertIn("issued_at", parsed)
            self.assertIn("wrap", parsed)

    def test_no_file_url_is_logged_by_the_vault_module(self):
        """File URLs — signed or header-authed — must never reach the logs."""
        import pathlib

        for name in ("router.py", "services.py", "tasks.py", "sealing.py", "images.py"):
            source = pathlib.Path(f"app/modules/vault/{name}").read_text()
            for lineno, line in enumerate(source.splitlines(), 1):
                if "logger." not in line and "logging." not in line:
                    continue
                lowered = line.lower()
                self.assertNotIn("media_url", lowered, f"{name}:{lineno}")
                self.assertNotIn("image_url", lowered, f"{name}:{lineno}")
                self.assertNotIn("sig=", lowered, f"{name}:{lineno}")
                if "item.media_path" in line or "image_path" in line or ".media_thumbnail_path" in line:
                    self.fail(f"{name}:{lineno} logs a storage path: {line.strip()}")


if __name__ == "__main__":
    unittest.main()
