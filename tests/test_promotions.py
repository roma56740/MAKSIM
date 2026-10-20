from __future__ import annotations

import html
import re
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import CallbackQuery, Chat, Document, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, User

from db import init_db
from db.promotions import create_promotion, get_promotion, list_active_deliveries
from services.promotions import archive_and_remove_promotion, publish_promotion, send_promotion, split_promotion_text


def visible(markup):
    return html.unescape(re.sub(r'<[^>]+>', '', markup))


class PromotionSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b''

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if method.__api_method__ == 'answerCallbackQuery':
            return True
        for field, limit in (('text', 4096), ('caption', 1024)):
            value = getattr(method, field, None)
            if value and len(visible(value).encode('utf-16-le')) // 2 > limit:
                raise AssertionError(f'Telegram {field} length exceeded')
        return Message(message_id=100 + len(self.calls), date=datetime.now(timezone.utc),
                       chat=Chat(id=int(getattr(method, 'chat_id', 1)), type='private'),
                       text=getattr(method, 'text', None) or 'Card')


class PromotionFlowTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        from handlers.admin.promotions import router as admin
        from handlers.user.promotions import router as user
        cls.dp = Dispatcher()
        cls.dp.include_router(admin)
        cls.dp.include_router(user)

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + '/test.sqlite3'
        await init_db(self.path)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO users (tg_id,status,created_at,updated_at) VALUES (2,'approved','now','now')")
            await db.commit()
        self.settings = SimpleNamespace(db_path=self.path, admin_ids={1})
        self.session = PromotionSession()
        self.bot = Bot('123456:FAKE_TEST_TOKEN', session=self.session)
        for uid in (1, 2):
            await self.dp.fsm.get_context(bot=self.bot, user_id=uid, chat_id=uid).clear()

    async def asyncTearDown(self):
        await self.bot.session.close()
        self.tmp.cleanup()

    async def message(self, text=None, *, document=None, caption=None, uid=1):
        msg = Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=uid, type='private'),
                      from_user=User(id=uid, is_bot=False, first_name='Test'), text=text,
                      document=Document(file_id=document, file_unique_id=document, file_name='terms.pdf') if document else None,
                      caption=caption)
        await self.dp.feed_update(self.bot, Update(update_id=1, message=msg), settings=self.settings)

    async def click(self, data, uid=1):
        msg = Message(message_id=10, date=datetime.now(timezone.utc), chat=Chat(id=uid, type='private'),
                      from_user=User(id=123456, is_bot=True, first_name='Bot'), text='Card')
        query = CallbackQuery(id='test', from_user=User(id=uid, is_bot=False, first_name='Test'),
                              chat_instance='test', message=msg, data=data)
        await self.dp.feed_update(self.bot, Update(update_id=2, callback_query=query), settings=self.settings)

    async def begin(self):
        await self.click('promo:create')
        await self.click('promo:kind:promotion')
        await self.message('Условия <акции> & цены')

    async def finish(self):
        await self.click('promo:content:done')
        await self.click('promo:expiry:forever:0')

    async def test_document_then_long_text_is_saved_and_read_in_full(self):
        await self.begin()
        await self.message(document='terms-document')
        parts = ['Первый блок <условий> & скидок. ' * 100, '🍷 Подробности и сроки. ' * 150]
        for part in parts:
            await self.message(part)
        await self.finish()
        item = await get_promotion(self.path, 1)
        expected = '\n\n'.join(part.strip() for part in parts)
        self.assertEqual(item['text'], expected)
        self.assertEqual(item['file_id'], 'terms-document')
        self.assertEqual(item['created_by'], 1)
        self.assertTrue(all(getattr(call, 'chat_id', 1) == 1 for call in self.session.calls))
        self.session.calls.clear()
        await self.click('upromo:view:1', uid=2)
        messages = [call for call in self.session.calls if call.__api_method__ != 'answerCallbackQuery']
        self.assertEqual(messages[0].__api_method__, 'sendDocument')
        self.assertEqual(''.join(html.unescape(call.text) for call in messages[1:]), expected)

    async def test_text_before_document_and_caption_are_preserved(self):
        await self.begin()
        await self.message('Описание до загрузки документа')
        caption = 'Условия ' * 110
        await self.message(document='first-document', caption=caption)
        await self.message(document='replacement-document')
        await self.finish()
        item = await get_promotion(self.path, 1)
        self.assertEqual(item['text'], 'Описание до загрузки документа\n\n' + caption.strip())
        self.assertEqual(item['file_id'], 'replacement-document')

    async def test_empty_content_and_stale_expiry_do_not_publish(self):
        await self.begin()
        await self.click('promo:content:done')
        self.assertTrue(self.session.calls[-1].show_alert)
        await self.click('promo:expiry:forever:0')
        self.assertTrue(self.session.calls[-1].show_alert)
        self.assertIsNone(await get_promotion(self.path, 1))
        await self.message('Только текст')
        await self.finish()
        await self.click('promo:expiry:forever:0')
        self.assertTrue(self.session.calls[-1].show_alert)
        self.assertIsNone(await get_promotion(self.path, 2))

    async def test_cancel_discards_draft_and_user_cannot_create(self):
        await self.begin()
        await self.message(document='draft-document')
        await self.click('promo:cancel')
        context = self.dp.fsm.get_context(bot=self.bot, user_id=1, chat_id=1)
        self.assertEqual(await context.get_data(), {})
        await self.click('promo:content:done')
        self.assertTrue(self.session.calls[-1].show_alert)
        self.session.calls.clear()
        await self.click('promo:kind:promotion', uid=2)
        self.assertEqual(self.session.calls, [])
        self.assertIsNone(await get_promotion(self.path, 1))

    async def test_duplicate_preserves_long_description_and_admin_actions(self):
        await self.begin()
        text = 'Полное описание акции. ' * 300
        await self.message(text)
        await self.message(document='terms-document')
        await self.finish()
        await self.click('promo:duplicate:1')
        await self.click('promo:expiry:forever:1')
        item = await get_promotion(self.path, 2)
        self.assertEqual(item['text'], text.strip())
        self.assertEqual(item['file_id'], 'terms-document')
        self.assertEqual(item['duplicated_from'], 1)
        self.session.calls.clear()
        await self.click('promo:view:2:active:0')
        messages = [call for call in self.session.calls if call.__api_method__ != 'answerCallbackQuery']
        self.assertIsNone(messages[0].reply_markup)
        self.assertTrue(any(button.callback_data == 'promo:archive:2'
                            for row in messages[-1].reply_markup.inline_keyboard for button in row))

    async def test_short_and_long_photo_and_text_only_rendering(self):
        for kind, text in (('photo', 'Короткое описание'), ('photo', '🍷<&>' * 1500),
                           ('text', '🍷<&>' * 1500), ('document', 'Короткое описание')):
            with self.subTest(kind=kind, length=len(text)):
                self.session.calls.clear()
                item = {'kind': 'promotion', 'title': 'Акция', 'text': text,
                        'file_id': 'attachment' if kind != 'text' else None, 'file_kind': kind}
                sent = await send_promotion(self.bot, 2, item)
                self.assertEqual(len(sent), len(self.session.calls))
                if len(text) < 100:
                    self.assertEqual(len(sent), 1)
                    self.assertIn(text, self.session.calls[0].caption)
                else:
                    self.assertEqual(''.join(html.unescape(call.text) for call in self.session.calls[1:]), text)

    async def test_publisher_tracks_every_message_and_archive_clears_each(self):
        ident = await create_promotion(self.path, kind='promotion', title='Акция', text='Длинное описание ' * 600,
                                       file_id='terms-document', file_kind='document', expires_at=None, created_by=1)
        ok, fail = await publish_promotion(self.bot, self.path, ident)
        self.assertEqual((ok, fail), (1, 0))
        deliveries = await list_active_deliveries(self.path, ident)
        self.assertEqual(len(deliveries), len(self.session.calls))
        self.assertGreater(len(deliveries), 2)
        mock_bot = SimpleNamespace(delete_message=AsyncMock(side_effect=RuntimeError('too old')),
                                   edit_message_text=AsyncMock(), edit_message_caption=AsyncMock())
        await archive_and_remove_promotion(mock_bot, self.path, ident)
        self.assertEqual(mock_bot.edit_message_text.await_count, len(deliveries))
        self.assertEqual(await list_active_deliveries(self.path, ident), [])


class PromotionTextTests(unittest.TestCase):
    def test_split_preserves_unicode_and_html_characters(self):
        text = ('🍷 Условия <акции> & цены\n' * 500) + 'Конец'
        chunks = split_promotion_text(text)
        self.assertEqual(''.join(chunks), text)
        self.assertTrue(all(len(chunk.encode('utf-16-le')) // 2 <= 3500 for chunk in chunks))
