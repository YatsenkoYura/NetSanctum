"""A token in a sealed vault must turn up nowhere in the clear.

The unit tests in `test_vault_no_plaintext_leak` ask whether a code path writes
plaintext. This asks the complementary question with a marker: plant a token
inside a sealed payload, then look for it on every surface a leak would travel —
files, the state store, and every column of every vault table.

The value is in the negative result. A canary that is never found is the only
evidence that the sealing holds end to end on a running system, and it is only
evidence if the tool cannot produce a false alarm and cannot quietly skip a
surface. So the tests below pin both: a planted token is found when it is
deliberately leaked, and a surface that cannot be read is reported as a blind
spot rather than as clean.
"""

import asyncio
import base64
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.modules.vault.canary import (
    CANARY_PREFIX,
    ScanReport,
    find_canaries,
    find_canaries_in_text,
    make_canary,
    plant_canary,
    scan_database,
    scan_path,
)
from app.modules.vault.crypto import generate_inbox_keypair
from app.modules.vault.models import VaultCollection, VaultItem

ENGINE = sa.create_engine("sqlite+pysqlite:///:memory:")
Base.metadata.create_all(ENGINE)
Session = sessionmaker(bind=ENGINE)


class AsyncSessionAdapter:
    """The slice of AsyncSession the canary and the sealing code touch."""

    def __init__(self, session):
        self.session = session

    async def execute(self, statement, parameters=None):
        result = self.session.execute(statement, parameters or {})
        return result

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def flush(self):
        self.session.flush()

    async def get(self, model, key):
        return self.session.get(model, key)

    async def refresh(self, instance):
        self.session.refresh(instance)

    def add(self, instance):
        self.session.add(instance)


class TokenShapeTests(unittest.TestCase):
    def test_a_token_names_its_collection_and_is_unique(self):
        first, second = make_canary(7), make_canary(7)

        self.assertTrue(first.startswith(f"{CANARY_PREFIX}-7-"))
        self.assertNotEqual(first, second)

    def test_an_ordinary_sentence_is_not_a_token(self):
        """A detector that cries wolf gets switched off."""
        for text in ("заметка про пароли", "NSECANARY", "NSECANARY1-", "nscanary1-7-abcdef0123456789"):
            self.assertEqual([], find_canaries_in_text(text), text)

    def test_a_token_is_found_wherever_it_appears(self):
        token = make_canary(3)

        self.assertEqual([token], find_canaries(token.encode()))
        self.assertEqual([token], find_canaries(f"ERROR at {token} while sealing".encode()))
        self.assertEqual([token], find_canaries_in_text(f'{{"content": "{token}"}}'))

    def test_a_sealed_payload_cannot_carry_the_token(self):
        """Ciphertext cannot contain it, which is the whole point."""
        from app.core.crypto import decode_b64, seal

        token = make_canary(1)
        sealed = seal(b"\x02" * 32, token.encode(), context=b"netsanctum:vault:item:1")

        self.assertEqual([], find_canaries(decode_b64(sealed.removeprefix("nsp:v1:"))))


class ScanPathTests(unittest.TestCase):
    def test_a_token_in_a_file_is_a_finding(self):
        token = make_canary(2)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "worker.log"
            path.write_text(f"nothing to see here\n{token}\n")
            report = ScanReport()
            scan_path("logs", Path(directory), report)

        self.assertFalse(report.clean)
        self.assertEqual([token], report.findings[0].canaries)
        self.assertIn("logs", report.findings[0].surface)

    def test_a_directory_is_walked(self):
        token = make_canary(2)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested").mkdir()
            (root / "nested" / "deep.log").write_text(token)
            report = ScanReport()
            scan_path("staging", root, report)

        self.assertEqual(1, len(report.findings))

    def test_a_clean_directory_is_clean_and_counted(self):
        with TemporaryDirectory() as directory:
            (Path(directory) / "a.log").write_text("ordinary lines")
            report = ScanReport()
            scan_path("logs", Path(directory), report)

        self.assertTrue(report.clean)
        self.assertEqual(1, len(report.scanned))

    def test_a_surface_that_cannot_be_read_is_a_blind_spot_not_a_pass(self):
        """The failure mode worth engineering against: a clean report from a
        scan that read nothing at all."""
        report = ScanReport()
        scan_path("logs", Path("/nonexistent/path/for/the/audit"), report)

        self.assertTrue(report.clean)
        self.assertEqual(1, len(report.unreachable), "an unreadable surface must say so")
        self.assertIn("does not exist", report.unreachable[0])

    def test_the_report_names_its_blind_spots_in_the_text_output(self):
        report = ScanReport()
        report.note_unreachable("state store", "connection refused")

        self.assertIn("blind", str(report))
        self.assertIn("connection refused", str(report))


class PlantAndScanTests(unittest.TestCase):
    """The whole point: plant one, find none — until something leaks it."""

    def setUp(self):
        Base.metadata.drop_all(ENGINE)
        Base.metadata.create_all(ENGINE)

    def _sealed_collection(self, session) -> VaultCollection:
        """A collection that reads as sealed, with a real inbox keypair.

        The passphrase wrapper is a stub: this is about what the sealed payload
        does once it exists, not about unlocking anything.
        """
        private, public = generate_inbox_keypair()
        collection = VaultCollection(
            id=5,
            name="sealed",
            is_encrypted=True,
            public_name="Sealed",
            # Standard base64, which is what the collection writes and reads.
            inbox_public_key=base64.b64encode(public).decode("ascii"),
            wrapped_key="nsk:v1:stub",
            key_salt="c2FsdA",
            key_kdf="scrypt",
            key_kdf_params={"n": 256, "r": 8, "p": 1},
        )
        collection.test_private_key = private
        session.add(collection)
        session.commit()
        return collection

    def test_a_planted_token_is_invisible_in_the_database(self):
        session = Session()
        collection = self._sealed_collection(session)
        adapter = AsyncSessionAdapter(session)

        planted = asyncio.run(plant_canary(adapter, collection.id))
        item = session.get(VaultItem, planted.item_id)

        # The title column is emptied by sealing; the token is in the payload.
        self.assertEqual("", item.title)
        self.assertIsNone(item.content)
        self.assertEqual([], item.tags)
        row = "\n".join(f"{c.name}={getattr(item, c.name, None)}" for c in item.__table__.columns)
        self.assertNotIn(planted.canary, row, "the token must not appear in any column in the clear")
        self.assertIn("nsp:v1:", item.sealed_payload)
        self.assertEqual([], find_canaries(item.sealed_payload.encode()))

        report = ScanReport()
        asyncio.run(scan_database(report, adapter))
        self.assertEqual([], report.findings, report.findings)
        self.assertTrue(report.clean)

    def test_a_leak_into_a_cleartext_column_is_found(self):
        """The negative result above only means something because this works."""
        session = Session()
        collection = self._sealed_collection(session)
        adapter = AsyncSessionAdapter(session)
        planted = asyncio.run(plant_canary(adapter, collection.id))

        # The mistake the audit exists to catch: a writer refilling a sealed column.
        item = session.get(VaultItem, planted.item_id)
        item.content = f"oops {planted.canary}"
        session.commit()

        report = ScanReport()
        asyncio.run(scan_database(report, adapter))

        self.assertFalse(report.clean)
        self.assertTrue(any("vault_items" in f.where for f in report.findings), report.findings)

    def test_planting_into_a_plain_collection_is_refused(self):
        session = Session()
        collection = VaultCollection(id=6, name="plain", is_encrypted=False)
        session.add(collection)
        session.commit()

        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(plant_canary(AsyncSessionAdapter(session), collection.id))

        self.assertIn("not sealed", str(caught.exception))

    def test_the_planted_card_reads_back_through_the_normal_path(self):
        """A token in a card nobody can open would prove nothing about leaks."""
        from app.modules.vault.crypto import SealedWrite, open_from_inbox
        from app.modules.vault.sealing import inbox_public_key

        session = Session()
        collection = self._sealed_collection(session)
        adapter = AsyncSessionAdapter(session)
        planted = asyncio.run(plant_canary(adapter, collection.id))
        item = session.get(VaultItem, planted.item_id)

        private = collection.test_private_key
        payload = open_from_inbox(
            SealedWrite(payload=item.sealed_payload, wrapped_key=item.wrapped_key),
            private,
            collection_id=collection.id,
            kind="item",
            row_id=item.id,
        )
        import json

        opened = json.loads(payload)

        self.assertIn(planted.canary, opened["title"])
        self.assertIn(planted.canary, opened["content"])
        self.assertTrue(inbox_public_key(collection))


if __name__ == "__main__":
    unittest.main()
