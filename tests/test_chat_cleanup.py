from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import DeleteMessage
from aiogram.types import Chat, Dice, Message, MessageId, Update, User

from db import init_db
from db.chat_cleanup import due_messages, schedule_message
from services.chat_cleanup import (
    MESSAGE_LIFETIME_SECONDS, ManagerIncomingCleanup, ManagerOutgoingCleanup,
    keep_admin_messages, process_message_cleanup, track_message,
)
from services.telegram_invoice_ui import SendRichInvoice


class CleanupSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.sequence = 100
        self.now = datetime.now(timezone.utc)

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b''

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        self.sequence += 1
        name = method.__api_method__
        if name.startswith('delete') or name == 'answerCallbackQuery':
            return True
        if name == 'copyMessage':
            return MessageId(message_id=self.sequence)
        if name == 'copyMessages':
            return [MessageId(message_id=self.sequence), MessageId(message_id=self.sequence + 1)]
        return Message(message_id=self.sequence, date=self.now,
                       chat=Chat(id=int(method.chat_id), type='private')).as_(bot)


class ChatCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + '/test.sqlite3'
        await init_db(self.path)
        async with aiosqlite.connect(self.path) as db:
            for uid in (1, 2, 3):
                await db.execute("INSERT INTO users (tg_id,status,created_at,updated_at) VALUES (?,'approved','now','now')", (uid,))
            await db.execute("INSERT INTO admins (tg_id,created_at) VALUES (3,'now')")
            await db.commit()
        self.settings = SimpleNamespace(db_path=self.path, admin_ids={1})
        self.session = CleanupSession()
        self.bot = Bot('123456:FAKE_TEST_TOKEN', session=self.session)
        self.bot.session.middleware(ManagerOutgoingCleanup(self.settings))
        self.now = self.session.now.timestamp()

    async def asyncTearDown(self):
        await self.bot.session.close()
        self.tmp.cleanup()

    async def rows(self, now=None, bot_id=None):
        return await due_messages(self.path, bot_id or self.bot.id, now if now is not None else self.now + 10 * 86400)

    async def test_incoming_and_outgoing_messages_are_deleted_after_two_hours(self):
        dp = Dispatcher()
        dp['settings'] = self.settings
        dp.update.outer_middleware(ManagerIncomingCleanup(self.settings))
        router = Router()

        @router.message()
        async def reply(message: Message):
            await message.answer('Накладная на проверке')
            await message.answer_document('report-file')

        dp.include_router(router)
        incoming = Message(message_id=10, date=self.session.now, chat=Chat(id=2, type='private'),
                           from_user=User(id=2, is_bot=False, first_name='Manager'), text='Мой отчёт')
        await dp.feed_update(self.bot, Update(update_id=1, message=incoming))
        rows = await self.rows()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row['delete_at'] - row['sent_at'] == MESSAGE_LIFETIME_SECONDS for row in rows))
        self.assertEqual(await process_message_cleanup(self.bot, self.settings, now=self.now + 7199), 0)
        self.assertEqual(await process_message_cleanup(self.bot, self.settings, now=self.now + 7201), 3)
        self.assertEqual(await self.rows(), [])
        self.assertEqual({call.message_id for call in self.session.calls if call.__api_method__ == 'deleteMessage'},
                         {row['message_id'] for row in rows})

    async def test_manual_admin_messages_survive_and_context_does_not_leak(self):
        @keep_admin_messages
        async def manual():
            await self.bot.send_message(2, 'Сообщение администратора')
            await self.bot.send_document(2, 'attachment')

        await manual()
        self.assertEqual(await self.rows(), [])
        await self.bot.send_message(2, 'Автоматическое уведомление')
        self.assertEqual(len(await self.rows()), 1)

    async def test_real_broadcast_marks_every_piece_as_permanent(self):
        from handlers.admin.broadcast import broadcast_send
        msg = Message(message_id=10, date=self.session.now, chat=Chat(id=1, type='private'),
                      from_user=User(id=123456, is_bot=True, first_name='Bot')).as_(self.bot)
        call = SimpleNamespace(from_user=User(id=1, is_bot=False, first_name='Admin'),
                               message=msg, bot=self.bot, answer=AsyncMock())
        state = SimpleNamespace(get_data=AsyncMock(return_value={'kind': 'document', 'file_id': 'broadcast-file', 'text': 'Рассылка ' * 600}), clear=AsyncMock())
        await broadcast_send(call, state, self.settings)
        manager_messages = [method for method in self.session.calls if getattr(method, 'chat_id', None) == 2]
        self.assertGreater(len(manager_messages), 2)
        self.assertEqual(await self.rows(), [])
        await self.bot.send_message(2, 'Ответ меню')
        self.assertEqual(len(await self.rows()), 1)

    async def test_admin_chats_business_chats_and_groups_are_not_cleaned(self):
        for uid in (1, 3):
            await self.bot.send_message(uid, 'Административные данные')
        await self.bot.send_message(2, 'Личное сообщение', business_connection_id='business')
        await self.bot.send_message(-100123, 'Группа')
        self.assertEqual(await self.rows(), [])

    async def test_rich_and_copied_messages_are_scheduled_but_edits_are_not(self):
        await self.bot(SendRichInvoice(chat_id=2, rich_message={'html': '<p>Ответ</p>'}))
        await self.bot.copy_message(2, 1, 99)
        await self.bot.copy_messages(2, 1, [98, 99])
        self.assertEqual(len(await self.rows()), 4)
        await self.bot.edit_message_text('Обновление', chat_id=2, message_id=10)
        self.assertEqual(len(await self.rows()), 4)

    async def test_queue_survives_reinitialization_and_is_scoped_to_bot(self):
        await schedule_message(self.path, self.bot.id, 2, 10, self.now, self.now + 7200)
        await schedule_message(self.path, 999999, 2, 10, self.now, self.now + 7200)
        await init_db(self.path)
        await schedule_message(self.path, self.bot.id, 2, 10, self.now, self.now + 9000)
        self.assertEqual((await self.rows())[0]['delete_at'], self.now + 7200)
        await process_message_cleanup(self.bot, self.settings, now=self.now + 7201)
        self.assertEqual(await self.rows(), [])
        self.assertEqual(len(await self.rows(bot_id=999999)), 1)

    async def test_user_promoted_to_admin_is_protected_at_deletion_time(self):
        await self.bot.send_message(2, 'Ответ')
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO admins (tg_id,created_at) VALUES (2,'now')")
            await db.commit()
        self.assertEqual(await process_message_cleanup(self.bot, self.settings, now=self.now + 7201), 0)
        self.assertFalse(any(call.__api_method__ == 'deleteMessage' for call in self.session.calls))
        self.assertEqual(await self.rows(), [])

    async def test_network_and_rate_limit_failures_are_retried(self):
        await schedule_message(self.path, self.bot.id, 2, 10, self.now, self.now + 7200)
        method = DeleteMessage(chat_id=2, message_id=10)
        fake = SimpleNamespace(id=self.bot.id, delete_message=AsyncMock(side_effect=TelegramNetworkError(method=method, message='offline')))
        await process_message_cleanup(fake, self.settings, now=self.now + 7201)
        self.assertEqual(await self.rows(now=self.now + 7202), [])
        self.assertEqual(len(await self.rows(now=self.now + 7262)), 1)
        fake.delete_message.side_effect = TelegramRetryAfter(method=method, message='limit', retry_after=120)
        await process_message_cleanup(fake, self.settings, now=self.now + 7262)
        self.assertEqual(await self.rows(now=self.now + 7300), [])
        fake.delete_message.side_effect = None
        self.assertEqual(await process_message_cleanup(fake, self.settings, now=self.now + 7383), 1)

    async def test_rate_limit_postpones_the_whole_due_queue(self):
        for ident in (10,11,12):
            await schedule_message(self.path,self.bot.id,2,ident,self.now,self.now+7200)
        fake=SimpleNamespace(id=self.bot.id,delete_message=AsyncMock(side_effect=TelegramRetryAfter(
            method=DeleteMessage(chat_id=2,message_id=10),message='limit',retry_after=120)))
        await process_message_cleanup(fake,self.settings,now=self.now+7201)
        self.assertEqual(fake.delete_message.await_count,1)
        self.assertEqual(await self.rows(now=self.now+7300),[])
        self.assertEqual(len(await self.rows(now=self.now+7322)),3)

    async def test_admin_survey_and_personal_chat_reply_are_preserved(self):
        from handlers.admin.surveys import _send_survey_content
        from handlers.admin.support_chats import admin_send_to_user
        from unittest.mock import patch
        await _send_survey_content(self.bot,2,'Вопрос','single',[{'id':1,'text':'Да'}],7,'survey-file','document')
        self.assertEqual(await self.rows(),[])
        message=Message(message_id=10,date=self.session.now,chat=Chat(id=1,type='private'),
                        from_user=User(id=1,is_bot=False,first_name='Admin'),text='Личный ответ').as_(self.bot)
        state=SimpleNamespace(get_data=AsyncMock(return_value={'thread_id':1}),clear=AsyncMock())
        with patch('handlers.admin.support_chats.sch.get_thread',new=AsyncMock(return_value=SimpleNamespace(status='active',user_id=2))), \
             patch('handlers.admin.support_chats.sch.add_message',new=AsyncMock()), \
             patch('handlers.admin.support_chats.sch.set_thread_admin',new=AsyncMock()):
            await admin_send_to_user(message,self.settings,state)
        self.assertEqual(await self.rows(),[])
        await self.bot.send_message(2,'Автоматический статус')
        self.assertEqual(len(await self.rows()),1)

    async def test_already_deleted_and_overage_messages_do_not_retry_forever(self):
        await schedule_message(self.path, self.bot.id, 2, 10, self.now, self.now + 7200)
        fake = SimpleNamespace(id=self.bot.id, delete_message=AsyncMock(side_effect=TelegramBadRequest(
            method=DeleteMessage(chat_id=2, message_id=10), message='message to delete not found')))
        await process_message_cleanup(fake, self.settings, now=self.now + 7201)
        self.assertEqual(await self.rows(), [])
        await schedule_message(self.path, self.bot.id, 2, 11, self.now, self.now + 7200)
        fake.delete_message.reset_mock()
        await process_message_cleanup(fake, self.settings, now=self.now + 49 * 3600)
        fake.delete_message.assert_not_awaited()
        self.assertEqual(await self.rows(), [])

    async def test_dice_respects_telegram_minimum_age(self):
        dice = Message(message_id=10, date=self.session.now, chat=Chat(id=2, type='private'), dice=Dice(emoji='🎲', value=1))
        await track_message(self.settings, self.bot.id, dice)
        self.assertEqual((await self.rows())[0]['delete_at'], self.now + 86401)
