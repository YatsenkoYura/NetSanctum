"""Tests for the Mail module."""

import asyncio
import unittest
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.modules.mail import services
from app.modules.mail.models import MailContact, MailFolder, MailMessage
from app.modules.mail.schemas import ContactCreate, FolderCreate, MessageCreate, MessageUpdate


class MailSession:
    def __init__(self, session: Session):
        self.session = session

    def add(self, instance):
        self.session.add(instance)

    async def delete(self, instance):
        self.session.delete(instance)

    async def commit(self):
        self.session.commit()

    async def refresh(self, instance):
        self.session.refresh(instance)

    async def execute(self, statement, parameters=None):
        return self.session.execute(statement, parameters or {})


class MailServicesTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(
            self.engine,
            tables=[MailFolder.__table__, MailMessage.__table__, MailContact.__table__],
        )
        self.session = Session(self.engine, expire_on_commit=False)
        self.db = MailSession(self.session)
        self.user_id = 1

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_ensure_system_folders_creates_defaults(self):
        asyncio.run(services.ensure_system_folders(self.db, self.user_id))
        folders = asyncio.run(services.list_folders(self.db, self.user_id))
        names = [f.name for f in folders]
        for expected in ("inbox", "sent", "drafts", "trash", "spam", "archive"):
            self.assertIn(expected, names)

    def test_create_folder_duplicate_raises(self):
        asyncio.run(services.ensure_system_folders(self.db, self.user_id))
        with self.assertRaises(ValueError):
            asyncio.run(services.create_folder(self.db, self.user_id, FolderCreate(name="inbox")))

    def test_create_and_list_messages_by_folder_name(self):
        inbox = asyncio.run(services.get_or_create_folder(self.db, self.user_id, "inbox", is_system=True))
        msg = asyncio.run(
            services.create_message(
                self.db,
                self.user_id,
                MessageCreate(to=["a@example.com"], subject="Hello", body_text="Body"),
                folder_id=inbox.id,
                is_read=False,
            )
        )
        self.assertIsNotNone(msg.id)
        messages = asyncio.run(services.list_messages(self.db, self.user_id, folder_name="inbox"))
        self.assertEqual([m.id for m in messages], [msg.id])
        self.assertFalse(messages[0].is_read)

    def test_unread_filter_and_mark_read(self):
        inbox = asyncio.run(services.get_or_create_folder(self.db, self.user_id, "inbox", is_system=True))
        msg = asyncio.run(
            services.create_message(
                self.db,
                self.user_id,
                MessageCreate(to=["a@example.com"], subject="Unread", body_text="x"),
                folder_id=inbox.id,
                is_read=False,
            )
        )
        unread = asyncio.run(services.list_messages(self.db, self.user_id, read=False))
        self.assertEqual([m.id for m in unread], [msg.id])
        updated = asyncio.run(services.mark_as_read(self.db, self.user_id, msg.id))
        self.assertIsNotNone(updated)
        self.assertTrue(updated.is_read)

    def test_update_and_delete_message(self):
        sent = asyncio.run(services.get_or_create_folder(self.db, self.user_id, "sent", is_system=True))
        msg = asyncio.run(
            services.create_message(
                self.db,
                self.user_id,
                MessageCreate(to=["a@example.com"], subject="Old", body_text="x"),
                folder_id=sent.id,
            )
        )
        updated = asyncio.run(
            services.update_message(self.db, msg, MessageUpdate(subject="New", is_starred=True))
        )
        self.assertEqual("New", updated.subject)
        self.assertTrue(updated.is_starred)
        asyncio.run(services.delete_message(self.db, updated))
        self.assertIsNone(asyncio.run(services.get_message(self.db, self.user_id, msg.id)))

    def test_thread_listing_orders_messages(self):
        inbox = asyncio.run(services.get_or_create_folder(self.db, self.user_id, "inbox", is_system=True))
        first = asyncio.run(
            services.create_message(
                self.db,
                self.user_id,
                MessageCreate(to=["a@example.com"], subject="T", body_text="1"),
                folder_id=inbox.id,
                sent_at=datetime(2024, 1, 1, tzinfo=UTC),
            )
        )
        second = asyncio.run(
            services.create_message(
                self.db,
                self.user_id,
                MessageCreate(to=["a@example.com"], subject="T", body_text="2"),
                folder_id=inbox.id,
                sent_at=datetime(2024, 1, 2, tzinfo=UTC),
            )
        )
        second.thread_id = first.thread_id
        self.db.session.commit()
        thread = asyncio.run(services.get_thread(self.db, self.user_id, first.thread_id))
        self.assertEqual([first.id, second.id], [m.id for m in thread])

    def test_contacts_create_update_find_delete(self):
        contact = asyncio.run(services.create_contact(self.db, self.user_id, "Test@Example.com", "Test"))
        self.assertEqual("test@example.com", contact.email)
        updated = asyncio.run(services.create_contact(self.db, self.user_id, "test@example.com", "Updated"))
        self.assertEqual(contact.id, updated.id)
        self.assertEqual("Updated", updated.name)
        found = asyncio.run(services.find_contact_by_email(self.db, self.user_id, "TEST@example.com"))
        self.assertIsNotNone(found)
        deleted = asyncio.run(services.delete_contact(self.db, self.user_id, contact.id))
        self.assertTrue(deleted)
        self.assertIsNone(asyncio.run(services.find_contact_by_email(self.db, self.user_id, "test@example.com")))

    def test_folder_counts_include_empty_folders(self):
        asyncio.run(services.ensure_system_folders(self.db, self.user_id))
        inbox = next(
            f for f in asyncio.run(services.list_folders(self.db, self.user_id)) if f.name == "inbox"
        )
        asyncio.run(
            services.create_message(
                self.db,
                self.user_id,
                MessageCreate(to=["a@example.com"], subject="S", body_text="x"),
                folder_id=inbox.id,
                is_read=False,
            )
        )
        counts = asyncio.run(services.get_folder_counts(self.db, self.user_id))
        inbox_count = next(c for c in counts if c["name"] == "inbox")
        sent_count = next(c for c in counts if c["name"] == "sent")
        self.assertEqual(1, inbox_count["total"])
        self.assertEqual(1, inbox_count["unread"])
        self.assertEqual(0, sent_count["total"])

    def test_schema_validation(self):
        payload = MessageCreate(to=["user@example.com"], subject="Hi", body_text="Body")
        self.assertEqual(["user@example.com"], payload.to)
        contact = ContactCreate(email="a@example.com", name="A")
        self.assertEqual("a@example.com", contact.email)


if __name__ == "__main__":
    unittest.main()
