"""Nothing a sealed card contains may be readable anywhere outside its payload.

This is the test the rest of the sealing work is judged by. It does not check that
the crypto is correct — it checks the duller half: that after a write, no column,
no filename, no progress record and no task argument still carries the content in
the clear.

Every writer that touches a card without a vault key is a candidate for this: the
Celery worker, the capture endpoints, the image externalizer. Each of them can
write a column that sealing had deliberately emptied, and nothing else complains
when they do.
"""

import asyncio
import json
import unittest

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.modules.vault.crypto import generate_inbox_keypair
from app.modules.vault.models import VaultCollection, VaultItem
from app.modules.vault.sealing import seal_item
from app.modules.vault.tasks import attach_downloaded_media

# The things the detector hunts for. Every one of them is something a sealed card
# genuinely contains: the title of the video, its source, the format, the note text.
VIDEO_TITLE = "Очень секретный выпуск 42"
SOURCE_URL = "https://example.com/watch?v=verysecrettoken"
SOURCE_ID = "verysecrettoken"
NOTE_TEXT = "записка, которую не должен видеть никто"
NOTE_TITLE = "Личное название заметки"
NOTE_TAG = "privatetag"


class AsyncSessionAdapter:
    """The slice of AsyncSession the sealing code touches."""

    def __init__(self, session):
        self.session = session

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


def readable_values(row) -> str:
    """Everything readable in one row, as one searchable blob."""
    parts = []
    for column in row.__table__.columns:
        value = getattr(row, column.name, None)
        if value is None or column.name in {"sealed_payload", "wrapped_key"}:
            continue
        parts.append(f"{column.name}={value}")
    return "\n".join(parts)


class LeakDetectorTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.addCleanup(self.engine.dispose)
        self.session = AsyncSessionAdapter(self.db)

    def sealed_collection(self, collection_id=5):
        # The wrapper itself is a stub here: tests that exercise it use real keys,
        # and these only need a collection that reads as sealed.
        _private, public = generate_inbox_keypair()
        row = VaultCollection(
            id=collection_id,
            name="Приватное",
            is_encrypted=True,
            public_name="Зашифрованный Vault",
            inbox_public_key=public.hex(),
            wrapped_key="nsk:v1:stub",
            key_salt="c2FsdA",
            key_kdf="scrypt",
            key_kdf_params={"n": 256, "r": 8, "p": 1},
        )
        self.db.add(row)
        self.db.commit()
        return row

    def sealed_item(self, item_id=10, collection_id=5):
        if self.db.get(VaultCollection, collection_id) is None:
            self.sealed_collection(collection_id)
        row = VaultItem(
            id=item_id,
            entry_type="bookmark",
            title=NOTE_TITLE,
            content=NOTE_TEXT,
            url=SOURCE_URL,
            tags=[NOTE_TAG],
            collection_id=collection_id,
            parent_id=None,
        )
        self.db.add(row)
        self.db.commit()
        seal_item(row, self._public(collection_id))
        self.db.commit()
        return row

    def _public(self, collection_id):
        collection = self.db.get(VaultCollection, collection_id)
        return bytes.fromhex(collection.inbox_public_key)

    def assert_no_readable_leak(self, row, *secrets_to_hunt, **extra):
        haystack = readable_values(row)
        for extra_name, extra_text in extra.items():
            haystack += f"\n{extra_name}={extra_text}"
        for needle in secrets_to_hunt:
            with self.subTest(needle=needle):
                self.assertNotIn(
                    needle,
                    haystack,
                    f"a sealed card still exposes {needle!r} in the clear",
                )


class WorkerWriteTests(LeakDetectorTestCase):
    """The Celery worker writes to a card it has no key for.

    Everything it writes is therefore readable, forever. It may write facts about
    the file; it may not write the file's content.
    """

    def call_worker(self, item, *, sealed=True):
        attach_downloaded_media(
            item,
            sealed=sealed,
            media_path=f"vault/videos/{item.id}-randomname.enc",
            thumbnail_path="vault/thumbnails/randomthumb.enc",
            size=1234,
            mime="video/mp4",
            title=VIDEO_TITLE,
            info={"duration": 61.0, "width": 1920, "height": 1080, "id": SOURCE_ID},
        )

    def test_the_worker_does_not_write_the_videos_title_into_a_sealed_card(self):
        item = self.sealed_item()
        self.call_worker(item)
        self.db.commit()

        self.assert_no_readable_leak(item, VIDEO_TITLE, NOTE_TITLE, NOTE_TEXT, SOURCE_URL, NOTE_TAG)

    def test_the_worker_does_not_write_the_mime_of_a_sealed_card(self):
        """`media_mime` is a sealed field: writing it here put the type in the clear
        and left the payload's copy of it stale."""
        item = self.sealed_item()
        self.call_worker(item)
        self.db.commit()

        self.assertIsNone(item.media_mime)
        self.assertIsNone(item.media_title)

    def test_the_worker_does_not_refill_a_sealed_title(self):
        item = self.sealed_item()
        item.title = ""
        self.call_worker(item)
        self.db.commit()

        self.assertNotEqual(VIDEO_TITLE, item.title)
        self.assert_no_readable_leak(item, VIDEO_TITLE)

    def test_a_sealed_filename_carries_no_source_id_and_no_extension(self):
        """The name is visible to anything that can list the storage volume."""
        from app.modules.vault.tasks import video_storage_name

        stem, ext = video_storage_name(sealed=True, info={"id": SOURCE_ID}, url=SOURCE_URL, ext=".mp4")
        name = f"{stem}{ext}"
        self.assertNotIn(SOURCE_ID, name)
        self.assertNotIn("mp4", name)

    def test_a_plain_filename_still_says_what_it_is(self):
        from app.modules.vault.tasks import video_storage_name

        stem, ext = video_storage_name(sealed=False, info={"id": SOURCE_ID}, url=SOURCE_URL, ext=".mp4")
        self.assertIn(SOURCE_ID, f"{stem}{ext}")

    def test_a_plain_card_still_gets_its_metadata(self):
        """The fix must not quietly turn every video into an anonymous file."""
        self.db.add(VaultCollection(id=2, name="Обычное", is_encrypted=False))
        self.db.commit()
        item = VaultItem(id=20, entry_type="bookmark", title="", collection_id=2)
        self.db.add(item)
        self.db.commit()

        self.call_worker(item, sealed=False)
        self.db.commit()

        self.assertEqual("video/mp4", item.media_mime)
        self.assertEqual(VIDEO_TITLE, item.media_title)
        self.assertEqual(VIDEO_TITLE, item.title)
        self.assertEqual(1234, item.media_size)
        self.assertEqual("completed", item.media_status)


class CollectionMetadataTests(LeakDetectorTestCase):
    def test_a_sealed_collections_description_is_not_readable(self):
        from app.modules.vault.sealing import SEALED_COLLECTION_FIELDS, seal_collection_fields

        collection = self.sealed_collection()
        collection.description = "То, что видно только после входа"
        seal_collection_fields(collection, bytes.fromhex(collection.inbox_public_key))
        self.db.commit()

        self.assertEqual("", collection.description or "")
        self.assertIn("description", SEALED_COLLECTION_FIELDS)
        self.assert_no_readable_leak(collection, "Только после входа")


class ItemCountLeakTests(LeakDetectorTestCase):
    def test_a_sealed_collection_reports_no_item_count(self):
        """How many private cards a vault holds is not the vault's business to tell
        the rest of the system: it reaches the planner and the global search index."""
        from app.modules.vault.services import list_collections

        collection = self.sealed_collection()
        self.sealed_item()
        self.db.commit()

        listed = asyncio.run(list_collections(self.session))
        reported = next(c for c in listed if c.id == collection.id)
        self.assertEqual(
            0,
            getattr(reported, "items_count", 0),
            "a sealed collection's item count reaches other modules through the spaces contract",
        )


class TaskHandoffTests(LeakDetectorTestCase):
    """Celery serialises its arguments into the broker, and the broker is the same
    Redis that runs with AOF on. An argument is therefore a copy on disk."""

    def queue(self):
        from unittest.mock import AsyncMock, patch

        from app.modules.vault.services import queue_video_download

        captured: dict = {}

        class FakeTask:
            id = "task-1"

            def apply_async(self, args=(), kwargs=None, task_id=None):
                captured["args"] = args
                captured["kwargs"] = kwargs or {}
                return FakeTask()

        redis = AsyncMock()
        redis.getdel = AsyncMock(return_value=None)

        async def fake_dispatch(task, rc, prefix, payload, *, args=(), kwargs=None, **kw):
            captured["prefix"] = prefix
            captured["payload"] = payload
            captured["args"] = args
            captured["kwargs"] = kwargs or {}
            return FakeTask()

        with (
            patch("app.modules.vault.services.redis_client", redis),
            patch("app.core.task_dispatch.dispatch_tracked_async", AsyncMock(side_effect=fake_dispatch)),
        ):
            asyncio.run(queue_video_download(self.session, 10, SOURCE_URL, quality="720", title=VIDEO_TITLE))
        return captured

    def test_the_download_task_is_not_given_the_url_or_the_title(self):
        captured = self.queue()
        serialised = json.dumps({"args": captured["args"], "kwargs": captured["kwargs"]})
        self.assertNotIn(SOURCE_URL, serialised, "the url reached the broker payload")
        self.assertNotIn(VIDEO_TITLE, serialised, "the title reached the broker payload")

    def test_the_progress_record_is_not_given_the_url_either(self):
        """It is read back by the progress endpoint and lives in the same AOF."""
        captured = self.queue()
        self.assertNotIn(SOURCE_URL, json.dumps(captured["payload"]))

    def test_the_handoff_is_deleted_when_the_worker_reads_it(self):
        from unittest.mock import AsyncMock, patch

        from app.modules.vault.services import take_download_handoff

        redis = AsyncMock()
        redis.getdel = AsyncMock(return_value=json.dumps({"url": SOURCE_URL, "title": VIDEO_TITLE}))
        with patch("app.modules.vault.services.redis_client", redis):
            taken = asyncio.run(take_download_handoff("abc"))
        redis.getdel.assert_awaited()
        self.assertEqual(SOURCE_URL, taken["url"])

    def test_a_missing_handoff_is_not_an_error_but_no_download(self):
        from unittest.mock import AsyncMock, patch

        from app.modules.vault.services import take_download_handoff

        redis = AsyncMock()
        redis.getdel = AsyncMock(return_value=None)
        with patch("app.modules.vault.services.redis_client", redis):
            self.assertEqual({}, asyncio.run(take_download_handoff("abc")))


class UnlockSessionTests(LeakDetectorTestCase):
    """The inbox private key used to sit in Redis in hex, in the clear — and Redis
    runs with AOF on, so it sat on disk too."""

    def setUp(self):
        super().setUp()
        from unittest.mock import patch

        from app.modules.vault import sealing

        self.redis_store: dict = {}

        store = self.redis_store

        class FakeRedis:
            async def set(self, key, value, ex=None):
                store[key] = value

            async def get(self, key):
                return store.get(key)

            async def expire(self, key, seconds):
                return key in store

            async def delete(self, key):
                store.pop(key, None)

            async def mget(self, keys):
                return [store.get(key) for key in keys]

        self.redis = FakeRedis()
        entered = patch.object(sealing, "redis_client", self.redis)
        entered.start()
        self.addCleanup(entered.stop)

    def unlock(self, collection, passphrase="верный", token="tab-1", session=None):
        from unittest.mock import AsyncMock, patch

        from app.modules.vault import sealing

        private = bytes.fromhex("aa" * 32)
        with (
            patch.object(sealing, "kek_for_wrapper", lambda *a, **k: (b"\x05" * 32, b"\x06" * 16)),
            patch.object(sealing, "unwrap_with_kek", lambda *a: private),
            # The KEK derivation is stubbed, so the keypair binding is stubbed
            # too: these tests exercise the session record, not the inbox MAC.
            patch.object(sealing, "inbox_keypair_matches", lambda *a: True),
            patch.object(sealing, "verify_collection_key", AsyncMock()),
        ):
            return asyncio.run(sealing.unlock_collection(collection, passphrase, token, session=session))

    def test_the_stored_session_contains_no_key_material(self):
        collection = self.sealed_collection()
        private_hex = "aa" * 32
        self.unlock(collection)

        blob = "\n".join(str(value) for value in self.redis_store.values())
        self.assertNotIn(private_hex, blob)
        self.assertNotIn("aa" * 8, blob)

    def test_the_session_opens_only_for_its_own_token(self):
        from app.modules.vault import sealing

        collection = self.sealed_collection()
        self.unlock(collection, token="tab-1")
        self.assertIsNotNone(asyncio.run(sealing.data_key_for(collection, "tab-1")))
        self.assertIsNone(asyncio.run(sealing.data_key_for(collection, "tab-2")))

    def test_a_legacy_plaintext_session_is_refused(self):
        """Records from before the sealed format are treated as absent: the tab
        simply unlocks again, instead of handing out a hex string as a key."""
        from app.modules.vault import sealing

        collection = self.sealed_collection()
        key_name = sealing.collection_key(collection.id, "tab-1")
        self.redis_store[key_name] = "bb" * 32
        self.assertIsNone(asyncio.run(sealing.data_key_for(collection, "tab-1")))

    def test_the_absolute_ttl_is_final(self):
        """A tab kept alive by activity still asks for the passphrase after two
        hours: the sliding TTL would otherwise hold the session open forever."""
        import time

        from app.modules.vault import sealing

        collection = self.sealed_collection()
        private = bytes.fromhex("aa" * 32)
        key_name = sealing.collection_key(collection.id, "tab-1")
        # An authentic record, sealed long ago: the timestamp is bound into the
        # AAD, so this is a genuine old session, not a forgery.
        self.redis_store[key_name] = sealing._seal_session_record(
            private, "tab-1", collection.id, int(time.time()) - sealing.SESSION_ABSOLUTE_TTL_SECONDS - 1
        )

        self.assertIsNone(asyncio.run(sealing.data_key_for(collection, "tab-1")))
        self.assertNotIn(key_name, self.redis_store)

    def test_rewriting_the_issue_time_voids_the_record(self):
        """`issued_at` enforces the absolute TTL, so it is bound into the AAD:
        extending it must break the record rather than the session."""
        import json

        from app.modules.vault import sealing

        collection = self.sealed_collection()
        self.unlock(collection, token="tab-1", session=self.session)
        key_name = sealing.collection_key(collection.id, "tab-1")

        record = json.loads(self.redis_store[key_name])
        record["issued_at"] = int(record["issued_at"]) + 3600
        self.redis_store[key_name] = json.dumps(record, separators=(",", ":"))

        self.assertIsNone(asyncio.run(sealing.data_key_for(collection, "tab-1")))


class PassphraseStrengthTests(unittest.TestCase):
    """A sealed vault has no recovery, so a guessable passphrase has to be refused
    where it is chosen — never at unlock, where it would lock the owner out."""

    def test_a_short_passphrase_is_refused(self):
        from app.modules.vault.crypto import WeakPassphraseError, check_passphrase_strength

        with self.assertRaises(WeakPassphraseError):
            check_passphrase_strength("короткий")

    def test_a_common_passphrase_is_refused(self):
        from app.modules.vault.crypto import WeakPassphraseError, check_passphrase_strength

        for weak in ("password123456", "qwerty12345678", "пароль12345678"):
            with self.subTest(weak=weak), self.assertRaises(WeakPassphraseError):
                check_passphrase_strength(weak)

    def test_a_decent_passphrase_passes(self):
        from app.modules.vault.crypto import check_passphrase_strength

        check_passphrase_strength("правильная лошадь, скрепка")

    def test_creating_a_sealed_collection_with_a_weak_passphrase_is_refused(self):
        import sqlalchemy as sa
        from sqlalchemy.orm import sessionmaker

        from app.core.database import Base
        from app.modules.vault.crypto import WeakPassphraseError
        from app.modules.vault.sealing import create_sealed_collection

        engine = sa.create_engine("sqlite://")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine, expire_on_commit=False)()

        class Session:
            def __init__(self, inner):
                self.inner = inner

            def add(self, row):
                self.inner.add(row)

            async def flush(self):
                self.inner.flush()

            async def commit(self):
                self.inner.commit()

            async def refresh(self, row):
                self.inner.refresh(row)

        with self.assertRaises(WeakPassphraseError):
            asyncio.run(create_sealed_collection(Session(db), "Приватное", "123", public_name="П"))


if __name__ == "__main__":
    unittest.main()
