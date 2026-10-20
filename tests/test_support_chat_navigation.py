import sqlite3
import tempfile
from contextlib import closing
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from db import support_chat as sch
from handlers.admin import support_chats
from handlers.user import admin_chat
from keyboards.admin import admin_main_kb
from keyboards.user import user_main_kb


class SupportChatNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + '/bot.sqlite3'
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute('CREATE TABLE admins (tg_id INTEGER PRIMARY KEY)')
            conn.execute('INSERT INTO admins VALUES (3)')
            conn.commit()
        await sch.create_tables(self.path)
        self.settings = SimpleNamespace(db_path=self.path, admin_ids={1})

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def message(self, user_id):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            answer=AsyncMock(), text='test',
            bot=SimpleNamespace(send_message=AsyncMock()),
        )

    def state(self, thread_id=None):
        return SimpleNamespace(
            get_data=AsyncMock(return_value={'thread_id': thread_id}),
            clear=AsyncMock(),
        )

    def assert_menu(self, actual, user_id):
        expected = admin_main_kb() if user_id in {1, 3} else user_main_kb()
        self.assertEqual(actual, expected)

    async def test_admin_closing_chat_uses_recipient_role_including_self_chat(self):
        for recipient in (1, 2, 3):
            with self.subTest(recipient=recipient):
                thread_id = await sch.create_thread(self.path, recipient)
                message = self.message(1)
                await support_chats.admin_close_chat(message, self.settings, self.state(thread_id))
                sent = message.bot.send_message.call_args
                self.assertEqual(sent.args[0], recipient)
                self.assert_menu(sent.kwargs['reply_markup'], recipient)
                self.assertEqual((await sch.get_thread(self.path, thread_id)).status, 'closed')

    async def test_user_side_close_restores_current_role(self):
        for user_id in (1, 2, 3):
            with self.subTest(user_id=user_id):
                thread_id = await sch.create_thread(self.path, user_id)
                message = self.message(user_id)
                await admin_chat.user_close_chat(message, self.settings, self.state(thread_id))
                self.assert_menu(message.answer.call_args.kwargs['reply_markup'], user_id)

    async def test_close_without_thread_restores_current_role(self):
        for user_id in (1, 2, 3):
            message = self.message(user_id)
            await admin_chat.user_close_chat(message, self.settings, self.state())
            self.assert_menu(message.answer.call_args.kwargs['reply_markup'], user_id)

    async def test_close_inaccessible_thread_restores_current_role(self):
        thread_id = await sch.create_thread(self.path, 99)
        for user_id in (1, 2, 3):
            message = self.message(user_id)
            await admin_chat.user_close_chat(message, self.settings, self.state(thread_id))
            self.assert_menu(message.answer.call_args.kwargs['reply_markup'], user_id)

    async def test_open_closed_thread_uses_callback_sender_role(self):
        for user_id in (1, 2, 3):
            thread_id = await sch.create_thread(self.path, user_id)
            await sch.close_thread(self.path, thread_id)
            message = self.message(999)
            message.edit_text = AsyncMock()
            call = SimpleNamespace(from_user=SimpleNamespace(id=user_id), message=message, answer=AsyncMock())
            data = admin_chat.UOpenThreadCB(thread_id=thread_id, page_from_newest=0)
            await admin_chat.user_open_thread(call, data, self.settings, self.state(thread_id))
            self.assert_menu(message.answer.call_args.kwargs['reply_markup'], user_id)

    async def test_send_to_closed_thread_restores_current_role(self):
        for user_id in (1, 2, 3):
            thread_id = await sch.create_thread(self.path, user_id)
            await sch.close_thread(self.path, thread_id)
            message = self.message(user_id)
            await admin_chat.user_send_to_admin(message, self.settings, self.state(thread_id))
            self.assert_menu(message.answer.call_args.kwargs['reply_markup'], user_id)
