import asyncio
from contextlib import closing
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText

from db import support_chat as sch
from handlers.admin import support_chats
from handlers.user import admin_chat
from handlers.support_chat_states import SupportChatState


class SupportChatDuplicateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + '/bot.sqlite3'
        await sch.create_tables(self.path)
        self.settings = SimpleNamespace(db_path=self.path, admin_ids={1})
        self.storage = MemoryStorage()
        self.bot = SimpleNamespace(send_message=AsyncMock())

    async def asyncTearDown(self):
        await self.storage.close()
        self.tmp.cleanup()

    def state(self, user_id):
        return FSMContext(self.storage, StorageKey(bot_id=999, chat_id=user_id, user_id=user_id))

    def call(self, user_id, data='u_new:'):
        last_render = None

        async def edit(text, reply_markup):
            nonlocal last_render
            rendered = text, reply_markup.model_dump_json() if reply_markup else None
            if rendered == last_render:
                raise TelegramBadRequest(
                    EditMessageText(chat_id=user_id, message_id=10, text=text),
                    'Bad Request: message is not modified',
                )
            last_render = rendered

        return SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, username='tester'),
            data=data, answer=AsyncMock(), bot=self.bot,
            message=SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock(side_effect=edit)),
        )

    def rows(self, query, args=()):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute(query, args).fetchall()

    async def test_concurrent_requests_create_one_active_thread(self):
        results = await asyncio.gather(*[
            sch.get_or_create_active_thread(self.path, 2, 1 if i % 2 else None)
            for i in range(12)
        ])
        self.assertEqual(len({thread_id for thread_id, _ in results}), 1)
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(await sch.create_thread(self.path, 2), results[0][0])
        self.assertEqual(self.rows('SELECT COUNT(*) FROM support_threads'), [(1,)])

    async def test_manager_repeated_new_chat_has_one_notification_and_one_prompt(self):
        call, state = self.call(2), self.state(2)
        await asyncio.gather(*[admin_chat.user_new_thread(call, self.settings, state) for _ in range(6)])
        self.assertEqual(self.rows('SELECT COUNT(*) FROM support_threads'), [(1,)])
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(call.message.answer.await_count, 1)
        self.assertEqual(await state.get_state(), SupportChatState.user_chat.state)

    async def test_admin_repeated_new_chat_has_one_system_entry_and_notification(self):
        call, state = self.call(1, 'a_new_pick:2'), self.state(1)
        await asyncio.gather(*[support_chats.admin_create_new_chat(call, self.settings, state) for _ in range(6)])
        self.assertEqual(self.rows('SELECT COUNT(*) FROM support_threads'), [(1,)])
        self.assertEqual(self.rows('SELECT COUNT(*) FROM support_messages'), [(1,)])
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(call.message.answer.await_count, 1)
        self.assertEqual(await state.get_state(), SupportChatState.admin_chat.state)

    async def test_manager_and_admin_open_the_same_thread_simultaneously(self):
        manager_state, admin_state = self.state(2), self.state(1)
        await asyncio.gather(
            admin_chat.user_new_thread(self.call(2), self.settings, manager_state),
            support_chats.admin_create_new_chat(self.call(1, 'a_new_pick:2'), self.settings, admin_state),
        )
        self.assertEqual(self.rows('SELECT COUNT(*) FROM support_threads'), [(1,)])
        self.assertEqual((await manager_state.get_data())['thread_id'], (await admin_state.get_data())['thread_id'])
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_repeated_open_of_existing_chat_does_not_repeat_prompts(self):
        thread_id = await sch.create_thread(self.path, 2)
        call, state = self.call(2), self.state(2)
        data = admin_chat.UOpenThreadCB(thread_id=thread_id, page_from_newest=0)
        await asyncio.gather(*[admin_chat.user_open_thread(call, data, self.settings, state) for _ in range(4)])
        self.assertEqual(call.message.answer.await_count, 1)
        admin_call, admin_state = self.call(1, f'a_open:{thread_id}:0'), self.state(1)
        await asyncio.gather(*[support_chats.admin_open_thread(admin_call, self.settings, admin_state) for _ in range(4)])
        self.assertEqual(admin_call.message.answer.await_count, 1)
        self.bot.send_message.assert_not_awaited()

    async def test_either_side_reuses_chat_started_by_other_side(self):
        thread_id, _ = await sch.get_or_create_active_thread(self.path, 2, 1)
        await admin_chat.user_new_thread(self.call(2), self.settings, self.state(2))
        await support_chats.admin_create_new_chat(self.call(1, 'a_new_pick:2'), self.settings, self.state(1))
        self.assertEqual(self.rows('SELECT id FROM support_threads'), [(thread_id,)])
        self.assertEqual(self.rows('SELECT COUNT(*) FROM support_messages'), [(0,)])
        self.bot.send_message.assert_not_awaited()

    async def test_closed_chat_stays_in_history_and_next_request_creates_new(self):
        thread_id = await sch.create_thread(self.path, 2)
        await sch.add_message(self.path, thread_id, 'user', 2, 'Keep this history')
        await sch.close_thread(self.path, thread_id)
        next_id, created = await sch.get_or_create_active_thread(self.path, 2)
        self.assertTrue(created)
        self.assertNotEqual(next_id, thread_id)
        self.assertEqual((await sch.get_thread(self.path, thread_id)).status, 'closed')
        self.assertEqual(self.rows('SELECT text FROM support_messages WHERE thread_id=?', (thread_id,)), [('Keep this history',)])

    async def test_existing_duplicates_become_closed_history_without_losing_messages(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute('DROP INDEX idx_support_threads_one_active_user')
            for thread_id, timestamp in ((1, '2026-10-19T09:00:00'), (2, '2026-10-19T10:00:00'), (3, '2026-10-19T10:00:00')):
                conn.execute("INSERT INTO support_threads(id,user_id,status,created_at,updated_at) VALUES (?,2,'active',?,?)", (thread_id, timestamp, timestamp))
                conn.execute("INSERT INTO support_messages(thread_id,sender_role,sender_id,text,created_at) VALUES (?,'user',2,?,?)", (thread_id, f'History {thread_id}', timestamp))
            conn.commit()
        await sch.create_tables(self.path)
        await sch.create_tables(self.path)
        self.assertEqual(self.rows('SELECT id,status FROM support_threads ORDER BY id'), [(1, 'closed'), (2, 'closed'), (3, 'active')])
        self.assertEqual(self.rows('SELECT COUNT(*) FROM support_messages'), [(3,)])
        self.assertEqual(await sch.get_or_create_active_thread(self.path, 2), (3, False))
        with closing(sqlite3.connect(self.path)) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO support_threads(user_id,status,created_at,updated_at) VALUES (2,'active','now','now')")
