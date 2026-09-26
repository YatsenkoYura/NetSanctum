"""Dialogue threads: the stored transcript, and the short window the model reads."""

import asyncio
import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.contracts.miku_conversation_note_v1 import (
    MikuNoteSearchRequest,
    MikuNoteWriteRequest,
)
from app.core.database import Base
from app.core.module_types import IntegrationContext, IntegrationUnavailableError
from app.modules.miku.conversations import (
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
    MODEL_WINDOW_TURNS,
    append_message,
    create_conversation,
    delete_conversation,
    get_conversation,
    list_conversations,
    list_messages,
    model_window,
    rename_conversation,
    title_from_message,
)
from app.modules.miku.integrations import search_notes, write_note
from app.modules.miku.models import (
    MikuConversation,
    MikuConversationMessage,
    MikuConversationNote,
)
from tests.test_miku_memory import AsyncSessionAdapter

OWNER = 7


class ConversationStorageTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(
            self.engine,
            tables=[
                MikuConversation.__table__,
                MikuConversationMessage.__table__,
                MikuConversationNote.__table__,
            ],
        )
        self.orm = Session(self.engine, expire_on_commit=False)
        self.db = AsyncSessionAdapter(self.orm)

    def tearDown(self):
        self.orm.close()
        self.engine.dispose()

    def _thread(self, user_id: int = OWNER):
        return asyncio.run(create_conversation(self.db, user_id))

    def _say(self, conversation: MikuConversation, role: str, text: str):
        return asyncio.run(append_message(self.db, conversation, role, text))

    def _exchange(self, conversation: MikuConversation, question: str, answer: str) -> None:
        self._say(conversation, MESSAGE_ROLE_USER, question)
        if answer:
            self._say(conversation, MESSAGE_ROLE_ASSISTANT, answer)

    # ── naming ────────────────────────────────────────────────────────────────

    def test_a_new_thread_is_named_after_its_first_message(self):
        conversation = self._thread()
        # Nothing has been said yet, so there is no name to keep.
        self.assertEqual("", conversation.title)

        self._say(conversation, MESSAGE_ROLE_USER, "  Найди   третью главу Re:Zero ")
        self.assertEqual("Найди третью главу Re:Zero", conversation.title)

    def test_a_thread_keeps_its_name_when_the_next_message_arrives(self):
        conversation = self._thread()
        self._say(conversation, MESSAGE_ROLE_USER, "первый вопрос")
        self._say(conversation, MESSAGE_ROLE_USER, "второй вопрос")
        self.assertEqual("первый вопрос", conversation.title)

    def test_a_long_first_message_is_clipped_rather_than_refused(self):
        conversation = self._thread()
        self._say(conversation, MESSAGE_ROLE_USER, "я" * 400)
        self.assertEqual(121, len(conversation.title))
        self.assertTrue(conversation.title.endswith("…"))

    def test_a_title_is_never_derived_from_the_assistant(self):
        conversation = self._thread()
        self._say(conversation, MESSAGE_ROLE_ASSISTANT, "Нашла.")
        self.assertEqual("", conversation.title)

    def test_titles_collapse_whitespace_and_never_come_back_empty(self):
        self.assertEqual("Новый диалог", title_from_message("   "))
        self.assertEqual("а б", title_from_message("а   б"))

    # ── transcript versus window ──────────────────────────────────────────────

    def test_the_transcript_outlives_the_window(self):
        conversation = self._thread()
        for index in range(MODEL_WINDOW_TURNS + 5):
            self._exchange(conversation, f"вопрос {index}", f"ответ {index}")

        stored = asyncio.run(list_messages(self.db, conversation.id))
        self.assertEqual(2 * (MODEL_WINDOW_TURNS + 5), len(stored))

        window = asyncio.run(model_window(self.db, conversation.id))
        self.assertEqual(MODEL_WINDOW_TURNS, len(window))
        # The window is the recent end of the thread, not its beginning.
        self.assertEqual(f"вопрос {MODEL_WINDOW_TURNS + 4}", window[-1][0])

    def test_a_question_with_no_reply_yet_is_left_out_of_the_window(self):
        conversation = self._thread()
        self._exchange(conversation, "заданный вопрос", "полученный ответ")
        self._say(conversation, MESSAGE_ROLE_USER, "следующий вопрос")

        window = asyncio.run(model_window(self.db, conversation.id))
        self.assertEqual([("заданный вопрос", "полученный ответ")], window)

    def test_the_window_is_empty_for_a_thread_with_nothing_answered(self):
        conversation = self._thread()
        self._say(conversation, MESSAGE_ROLE_USER, "ещё не отвечено")
        self.assertEqual([], asyncio.run(model_window(self.db, conversation.id)))

    # ── listing, renaming, removal ────────────────────────────────────────────

    def test_threads_are_listed_newest_first_with_their_size(self):
        first = self._thread()
        self._exchange(first, "первый", "ответ")
        second = self._thread()
        self._say(second, MESSAGE_ROLE_USER, "второй")

        rows = asyncio.run(list_conversations(self.db, OWNER))
        self.assertEqual(["второй", "первый"], [conversation.title for conversation, _ in rows])
        self.assertEqual({first.id: 2, second.id: 1}, {c.id: n for c, n in rows})

    def test_a_thread_can_be_renamed(self):
        conversation = self._thread()
        renamed = asyncio.run(rename_conversation(self.db, conversation, "  Про Re:Zero  "))
        self.assertEqual("Про Re:Zero", renamed.title)

    def test_removing_a_thread_takes_its_messages_with_it(self):
        conversation = self._thread()
        self._exchange(conversation, "вопрос", "ответ")
        asyncio.run(delete_conversation(self.db, conversation))

        self.assertEqual([], asyncio.run(list_messages(self.db, conversation.id)))
        self.assertIsNone(asyncio.run(get_conversation(self.db, OWNER, conversation.id)))

    def test_one_owner_cannot_see_another_owners_thread(self):
        conversation = self._thread(user_id=1)
        self.assertIsNone(asyncio.run(get_conversation(self.db, 2, conversation.id)))
        self.assertEqual([], asyncio.run(list_conversations(self.db, 2)))

    def test_a_thread_another_owner_cannot_see_is_not_in_their_list(self):
        mine = self._thread(user_id=1)
        self._say(mine, MESSAGE_ROLE_USER, "privat")
        theirs = self._thread(user_id=2)
        self._say(theirs, MESSAGE_ROLE_USER, "theirs")

        titles = [c.title for c, _ in asyncio.run(list_conversations(self.db, 2))]
        self.assertEqual(["theirs"], titles)


class ConversationNoteTests(unittest.TestCase):
    """The agent carries context forward itself, scoped to the turn it is in."""

    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(
            self.engine,
            tables=[
                MikuConversation.__table__,
                MikuConversationMessage.__table__,
                MikuConversationNote.__table__,
            ],
        )
        self.orm = Session(self.engine, expire_on_commit=False)
        self.db = AsyncSessionAdapter(self.orm)

    def tearDown(self):
        self.orm.close()
        self.engine.dispose()

    def _context(self, scope_id: str) -> IntegrationContext:
        return IntegrationContext(
            session=self.db,
            user=SimpleNamespace(id=OWNER),
            registry=SimpleNamespace(),
            consumer_id="miku",
            scope_id=scope_id,
        )

    def _thread(self):
        return asyncio.run(create_conversation(self.db, OWNER))

    def test_a_note_is_kept_for_the_conversation_that_wrote_it(self):
        conversation = self._thread()
        context = self._context(str(conversation.id))

        written = asyncio.run(
            write_note(
                MikuNoteWriteRequest(key="subject", text="обсуждаем третью главу Re:Zero"),
                context,
            )
        )
        self.assertEqual("written", written.status)

        found = asyncio.run(search_notes(MikuNoteSearchRequest(query="глава"), context))
        self.assertEqual(1, len(found.items))
        self.assertEqual("обсуждаем третью главу Re:Zero", found.items[0].value["text"])

    def test_notes_do_not_leak_into_another_conversation(self):
        first = self._thread()
        second = self._thread()
        asyncio.run(
            write_note(MikuNoteWriteRequest(key="subject", text="секрет"), self._context(str(first.id)))
        )
        found = asyncio.run(search_notes(MikuNoteSearchRequest(), self._context(str(second.id))))
        self.assertEqual([], found.items)

    def test_writing_the_same_key_replaces_rather_than_duplicates(self):
        conversation = self._thread()
        context = self._context(str(conversation.id))
        asyncio.run(write_note(MikuNoteWriteRequest(key="subject", text="первый"), context))
        asyncio.run(write_note(MikuNoteWriteRequest(key="subject", text="второй"), context))

        found = asyncio.run(search_notes(MikuNoteSearchRequest(), context))
        self.assertEqual(1, len(found.items))
        self.assertEqual("второй", found.items[0].value["text"])

    def test_a_note_can_be_forgotten(self):
        conversation = self._thread()
        context = self._context(str(conversation.id))
        asyncio.run(write_note(MikuNoteWriteRequest(key="subject", text="тема"), context))

        removed = asyncio.run(write_note(MikuNoteWriteRequest(op="delete", key="subject"), context))
        self.assertEqual("deleted", removed.status)
        self.assertEqual([], asyncio.run(search_notes(MikuNoteSearchRequest(), context)).items)

    def test_forgetting_a_note_that_was_never_written_says_so(self):
        conversation = self._thread()
        removed = asyncio.run(
            write_note(MikuNoteWriteRequest(op="delete", key="absent"), self._context(str(conversation.id)))
        )
        self.assertEqual("missing", removed.status)

    def test_a_note_cannot_be_written_into_a_thread_that_does_not_exist(self):
        # The scope comes from the runtime, not the model, but an id that matches no
        # thread would otherwise leave a note nothing will ever read back.
        with self.assertRaises(IntegrationUnavailableError):
            asyncio.run(write_note(MikuNoteWriteRequest(key="subject", text="x"), self._context("4242")))

    def test_a_note_cannot_be_written_into_another_owners_thread(self):
        conversation = self._thread()
        stranger = IntegrationContext(
            session=self.db,
            user=SimpleNamespace(id=OWNER + 1),
            registry=SimpleNamespace(),
            consumer_id="miku",
            scope_id=str(conversation.id),
        )
        with self.assertRaises(IntegrationUnavailableError):
            asyncio.run(write_note(MikuNoteWriteRequest(key="subject", text="x"), stranger))

    def test_notes_are_refused_outside_a_conversation(self):
        # A turn with no thread has nothing to scope a note to, and storing it
        # somewhere global would leak it between conversations.
        with self.assertRaises(IntegrationUnavailableError):
            asyncio.run(write_note(MikuNoteWriteRequest(key="subject", text="x"), self._context("")))

    def test_removing_a_thread_takes_its_notes_with_it(self):
        conversation = self._thread()
        context = self._context(str(conversation.id))
        asyncio.run(write_note(MikuNoteWriteRequest(key="subject", text="тема"), context))
        asyncio.run(delete_conversation(self.db, conversation))

        # The notes went with it, and what is left is a scope that no longer names
        # anything, so reading it is refused rather than answered with an empty list.
        self.assertEqual([], self.orm.query(MikuConversationNote).all())
        with self.assertRaises(IntegrationUnavailableError):
            asyncio.run(search_notes(MikuNoteSearchRequest(), context))
