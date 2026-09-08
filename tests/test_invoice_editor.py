from __future__ import annotations
import copy
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNotFound
from aiogram.types import Message, Chat, User, CallbackQuery, Update
from db import init_db
from db.invoices import create_invoice, get_invoice_full, save_invoice_analysis, approve_invoice, list_invoice_items, mark_invoice_analysis_processing
from services.invoice_editor import parse_value, apply_change, revision
from services.telegram_invoice_ui import display
from handlers.admin.invoice_editor import overview, cb


def analysis():
    return {'supplier': 'ООО <Компания> & партнёры', 'currency': 'RUB', 'amount_payable': 180,
            'total_amount': 180, 'calculated_total': 180, 'items': [
        {'product_name': 'Товар <1>', 'quantity': 2, 'unit_price': 90, 'line_total': 180,
         'unit_price_before_discount': 100, 'discount_percent': 10, 'discount_amount': 20, 'article': None, 'unit': 'шт'}]}


class RulesTests(unittest.TestCase):
    def test_numbers_dates_and_nonfinite(self):
        self.assertEqual(parse_value('unit_price', '1 250,50'), 1250.5)
        self.assertEqual(parse_value('invoice_date', '29.02.2024'), '2024-02-29')
        for field, value in [('unit_price','nan'), ('quantity','0'), ('quantity','0.00000001'), ('quantity','-1'), ('discount_percent','101'), ('invoice_date','29.02.2025')]:
            with self.assertRaises(ValueError): parse_value(field, value)

    def test_calculations_preserve_printed_total_and_other_fields(self):
        a = analysis()
        updated = apply_change(a, 0, 'quantity', 3)
        self.assertEqual(updated['items'][0]['line_total'], 270)
        self.assertEqual(updated['calculated_total'], 270)
        self.assertEqual(updated['amount_payable'], 180)
        self.assertEqual(a['items'][0]['quantity'], 2)
        updated = apply_change(updated, 0, 'discount_percent', 20)
        self.assertEqual(updated['items'][0]['line_total'], 240)
        self.assertEqual(updated['items'][0]['unit_price'], 80)
        updated = apply_change(updated, 0, 'line_total', 150)
        self.assertEqual(updated['items'][0]['unit_price'], 50)
        self.assertEqual(updated['items'][0]['discount_amount'], 150)

    def test_mobile_tables_have_two_columns_and_full_row_actions(self):
        import json
        from html.parser import HTMLParser
        from services.telegram_invoice_ui import cell_text, embed_controls
        from handlers.admin.invoices_panel import panel_view
        class Layout(HTMLParser):
            def __init__(self):
                super().__init__(); self.width=0; self.widths=[]; self.cell={}; self.action_spans=[]
            def handle_starttag(self,tag,attrs):
                attrs=dict(attrs)
                if tag=='tr': self.width=0
                if tag in {'td','th'}:
                    self.width+=int(attrs.get('colspan',1)); self.cell=attrs
                if tag=='tg-button' and self.cell:
                    self.action_spans.append(int(self.cell.get('colspan',1)))
            def handle_endtag(self,tag):
                if tag=='tr': self.widths.append(self.width)
                if tag in {'td','th'}: self.cell={}
        a=analysis()
        inv={'id':123, 'status':'pending', 'analysis_status':'completed', 'analysis_json':json.dumps(a), 'document_total':5149200}
        _,kb,rich=overview(inv,a)
        _,panel_kb,panel=panel_view('pending',[inv],0,1,1,{'pending':1,'approved':0,'rejected':0})
        for content in (embed_controls(rich,kb),embed_controls(panel,panel_kb)):
            parser=Layout(); parser.feed(content)
            self.assertTrue(all(n<=2 for n in parser.widths))
            self.assertTrue(all(n==2 for n in parser.action_spans))
            self.assertNotIn('\u00a0',content)
        raw='A'*80+'<&'
        wrapped=cell_text(raw)
        self.assertIn('\u200b',wrapped)
        self.assertIn('&lt;&amp;',wrapped)

    def test_large_invoice_navigation_and_escaping(self):
        a = analysis(); a['items'] *= 150
        inv={'id': 123, 'status':'pending', 'analysis_json':'{}', 'analysis_status':'completed'}
        for page in (0,1,24,100):
            text,kb,rich=overview(inv,a,page=page)
            self.assertLess(len(text),4096)
            self.assertNotIn('ООО <Компания>',text)
            self.assertIn('<table bordered striped compact>',rich)
            self.assertIn('<tg-button',rich)
            self.assertEqual(sum(1 for row in kb.inline_keyboard for b in row if ':item:' in b.callback_data),6)
            for row in kb.inline_keyboard:
                for b in row: self.assertLessEqual(len(b.callback_data.encode()),64)
        _,kb,_=overview(inv,a,admin=False)
        self.assertFalse(any(':add:' in b.callback_data or ':confirm:' in b.callback_data for row in kb.inline_keyboard for b in row))


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__(); self.calls=[]; self.seq=100
    async def close(self): pass
    async def stream_content(self,*args,**kwargs):
        if False: yield b''
    async def make_request(self,bot,method,timeout=None):
        self.calls.append(method); self.seq+=1
        if method.__api_method__ == 'answerCallbackQuery': return True
        return Message(message_id=self.seq,date=datetime.now(timezone.utc),chat=Chat(id=int(getattr(method,'chat_id',1)),type='private'),text=getattr(method,'text',None) or 'Card')


class EditorFlowTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        from handlers.admin.invoice_editor import router as editor
        from handlers.admin.invoices_panel import router as panel
        from handlers.admin.invoices import router as invoices
        from handlers.user.invoices import router as user
        cls.dp=Dispatcher()
        for router in (editor,panel,invoices,user): cls.dp.include_router(router)

    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=self.tmp.name+'/test.sqlite'
        await init_db(self.path)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO users (tg_id,status,created_at,updated_at) VALUES (2,'approved','now','now')")
            await db.commit()
        self.ident=await create_invoice(self.path,2,'test-file','photo')
        await save_invoice_analysis(self.path,self.ident,analysis())
        self.settings=SimpleNamespace(db_path=self.path,admin_ids={1})
        self.session=FakeSession(); self.bot=Bot('123456:FAKE_TEST_TOKEN',session=self.session)
        for uid in (1,2,3): await self.dp.fsm.get_context(bot=self.bot,user_id=uid,chat_id=uid).clear()

    async def asyncTearDown(self):
        await self.bot.session.close(); self.tmp.cleanup()

    async def message(self,text,uid=1):
        msg=Message(message_id=1,date=datetime.now(timezone.utc),chat=Chat(id=uid,type='private'),from_user=User(id=uid,is_bot=False,first_name='Test'),text=text)
        await self.dp.feed_update(self.bot,Update(update_id=1,message=msg),settings=self.settings)

    async def click(self,data,uid=1):
        msg=Message(message_id=10,date=datetime.now(timezone.utc),chat=Chat(id=uid,type='private'),text='Card')
        query=CallbackQuery(id='test',from_user=User(id=uid,is_bot=False,first_name='Test'),chat_instance='test',message=msg,data=data)
        await self.dp.feed_update(self.bot,Update(update_id=2,callback_query=query),settings=self.settings)

    async def inv(self): return await get_invoice_full(self.path,self.ident)

    async def test_admin_menu_does_not_open_user_menu(self):
        await self.message('🧾 Накладные')
        self.assertIn('На проверке',self.session.calls[-1].rich_message['html'])
        self.assertIn('iv:page:',self.session.calls[-1].rich_message['html'])
        self.assertEqual(self.session.calls[-1].reply_markup.inline_keyboard, [])

    async def test_table_actions_are_unique_and_fields_are_inside_cells(self):
        from html.parser import HTMLParser
        class Inspect(HTMLParser):
            def __init__(self):
                super().__init__(); self.path=[]; self.actions=[]; self.tables=[]
            def handle_starttag(self, tag, attrs):
                attrs=dict(attrs)
                if tag=='table': self.tables.append(attrs)
                if tag=='tg-button': self.actions.append((attrs.get('data'), 'td' in self.path))
                if tag not in {'br','hr'}: self.path.append(tag)
            def handle_endtag(self, tag):
                if self.path and self.path[-1] == tag: self.path.pop()
        inv=await self.inv()
        await self.click(cb('page',inv))
        method=self.session.calls[-1]
        self.assertEqual(method.reply_markup.inline_keyboard, [])
        doc=Inspect(); doc.feed(method.rich_message['html'])
        actions=[a for a,_ in doc.actions]
        self.assertEqual(len(actions),len(set(actions)))
        self.assertTrue(all('bordered' in t and 'striped' in t for t in doc.tables))
        self.assertTrue(any(':item:' in a and inside for a,inside in doc.actions))
        await self.click(cb('item',inv,index=0))
        doc=Inspect(); doc.feed(self.session.calls[-1].rich_message['html'])
        fields=[(a,inside) for a,inside in doc.actions if ':field:' in a]
        self.assertEqual(len(fields),9)
        self.assertTrue(all(inside for _,inside in fields))

    async def test_native_status_switch_and_back_from_confirmation(self):
        inv=await self.inv()
        await self.click(cb('confirm',inv))
        self.assertIn('Процент вознаграждения',self.session.calls[-1].rich_message['html'])
        await self.click('ainv:cancel')
        self.assertIn('<table bordered',self.session.calls[-1].rich_message['html'])
        await self.click('ainv:panel:rejected:0')
        content=self.session.calls[-1].rich_message['html']
        self.assertIn('пока нет накладных',content)
        self.assertIn('ainv:panel:pending:0',content)
        self.assertIn('ainv:panel:approved:0',content)
        self.assertEqual(self.session.calls[-1].reply_markup.inline_keyboard, [])

    async def test_old_server_receives_full_classic_keyboard_on_edit(self):
        from services.telegram_invoice_ui import EditRichInvoice
        inv=await self.inv()
        text,kb,rich=overview(inv,analysis())
        fake=SimpleNamespace(chat=SimpleNamespace(id=1),message_id=1,bot=AsyncMock(),edit_text=AsyncMock())
        fake.bot.side_effect=TelegramNotFound(method=EditRichInvoice(chat_id=1,message_id=1,rich_message={'html':'x'}),message='Not Found')
        await display(fake,text,kb,rich=rich,edit=True)
        fake.edit_text.assert_awaited_once_with(text,parse_mode='HTML',reply_markup=kb)
        self.assertTrue(any(':item:' in b.callback_data for row in kb.inline_keyboard for b in row))

    async def test_field_edit_persists_and_cancel_does_not_modify(self):
        inv=await self.inv()
        await self.click(cb('field',inv,index=0,field=0))
        await self.message('Исправленный товар')
        updated=await self.inv(); self.assertIn('Исправленный товар',updated['analysis_json'])
        await self.click(cb('field',updated,index=0,field=0))
        await self.message('/cancel')
        self.assertEqual(updated['analysis_json'],(await self.inv())['analysis_json'])

    async def test_add_three_steps_delete_confirmation_and_stale_button(self):
        inv=await self.inv(); await self.click(cb('add',inv))
        await self.message('Новый товар'); await self.message('3'); await self.message('25,50')
        from db.invoices import decode_invoice_analysis
        inv=await self.inv(); a=decode_invoice_analysis(inv)
        self.assertEqual(len(a['items']),2); self.assertEqual(a['items'][1]['line_total'],76.5)
        await self.click(cb('delete',inv,index=1))
        self.assertEqual(len(decode_invoice_analysis(await self.inv())['items']),2)
        await self.click(cb('deleteok',inv,index=1))
        self.assertEqual(len(decode_invoice_analysis(await self.inv())['items']),1)
        await self.click(cb('deleteok',inv,index=0)) # obsolete revision must not delete another item
        self.assertEqual(len(decode_invoice_analysis(await self.inv())['items']),1)

    async def test_authorization_owner_read_only_and_stranger_denied(self):
        inv=await self.inv()
        await self.click(cb('page',inv),uid=2)
        await self.click(cb('field',inv,index=0,field=0),uid=2)
        self.assertIn('только администратор',self.session.calls[-1].text)
        await self.click(cb('page',inv),uid=3)
        self.assertIn('недоступна',self.session.calls[-1].text)
        self.assertEqual(inv['analysis_json'],(await self.inv())['analysis_json'])

    async def test_concurrent_edits_and_approval_snapshot(self):
        inv=await self.inv(); version=revision(inv)
        await save_invoice_analysis(self.path,self.ident,apply_change(analysis(),0,'quantity',3),expected_revision=version)
        with self.assertRaises(ValueError):
            await save_invoice_analysis(self.path,self.ident,apply_change(analysis(),0,'quantity',4),expected_revision=version)
        with self.assertRaises(ValueError):
            await approve_invoice(self.path,self.ident,180,9,analysis()['items'],expected_revision=version)
        self.assertEqual((await self.inv())['status'],'pending')

    async def test_approved_edit_updates_accounting_atomically(self):
        await approve_invoice(self.path,self.ident,180,9,analysis()['items'])
        inv=await self.inv()
        a=apply_change(analysis(),0,'product_name','Новое имя')
        a=apply_change(a,-1,'amount_payable',200)
        await save_invoice_analysis(self.path,self.ident,a,expected_revision=revision(inv))
        inv=await self.inv(); items=await list_invoice_items(self.path,self.ident)
        self.assertEqual(inv['deal_amount'],200); self.assertEqual(inv['reward_amount'],10)
        self.assertEqual(items[0]['product_name'],'Новое имя')

    async def test_recognition_lock_prevents_double_job_and_edits(self):
        from services.invoice_recognition import InvoiceRecognitionError
        inv=await self.inv(); await mark_invoice_analysis_processing(self.path,self.ident)
        with self.assertRaises(InvoiceRecognitionError): await mark_invoice_analysis_processing(self.path,self.ident)
        with self.assertRaises(ValueError): await save_invoice_analysis(self.path,self.ident,analysis(),expected_revision=revision(inv))

    async def test_rich_fallback_without_network(self):
        fake=SimpleNamespace(chat=SimpleNamespace(id=1),bot=AsyncMock())
        fake.bot.side_effect=TelegramNotFound(method=SimpleNamespace(),message='Not Found')
        fake.bot.send_message=AsyncMock()
        from aiogram.types import InlineKeyboardMarkup
        await display(fake,'<b>Card</b>',InlineKeyboardMarkup(inline_keyboard=[]),rich='<p>Card</p>')
        fake.bot.send_message.assert_awaited_once()

if __name__=='__main__': unittest.main()
