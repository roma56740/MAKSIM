import json
from pathlib import Path
import tempfile
import unittest

import aiosqlite
from openpyxl import load_workbook

from db import init_db
from db.invoices import create_invoice, save_invoice_analysis, approve_invoice, list_invoices_for_user
from handlers.user.invoices import _list_text
from services.analytics_report import build_user_report_xlsx
from services.invoice_report import invoice_report_details


class InvoiceReportDetailsTests(unittest.TestCase):
    def test_approved_amount_takes_priority_and_zero_is_preserved(self):
        supplier, amount = invoice_report_details({
            'supplier_name': 'Supplier from catalog', 'company_name': 'Recognized company',
            'deal_amount': 0, 'document_total': 200,
            'analysis_json': json.dumps({'supplier': 'Supplier', 'amount_payable': 300}),
        })
        self.assertEqual((supplier, amount), ('Supplier from catalog', 0))

    def test_missing_or_invalid_data_uses_fallbacks(self):
        self.assertEqual(invoice_report_details({'analysis_json': 'broken'}), ('—', None))
        self.assertEqual(invoice_report_details({
            'supplier_name': ' ', 'document_total': float('nan'),
            'analysis_json': json.dumps({'supplier': 'Recognized supplier', 'amount_payable': 250}),
        }), ('Recognized supplier', 250))


class InvoiceReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_and_excel_show_supplier_total_and_reward_for_own_invoices(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / 'test.sqlite3')
            await init_db(db_path)
            async with aiosqlite.connect(db_path) as db:
                for user_id in (2, 3):
                    await db.execute(
                        "INSERT INTO users(tg_id,status,created_at,updated_at) VALUES (?,'approved','now','now')",
                        (user_id,),
                    )
                await db.commit()
            approved = await create_invoice(db_path, 2, 'own-approved', 'photo')
            pending = await create_invoice(db_path, 2, 'own-pending', 'photo')
            missing = await create_invoice(db_path, 2, 'own-missing', 'photo')
            other = await create_invoice(db_path, 3, 'other-manager', 'photo')
            data = {
                'supplier': 'ООО <Поставщик> & партнёры', 'amount_payable': 20000,
                'total_amount': 20000, 'calculated_total': 20000, 'currency': 'RUB',
                'items': [{'product_name': 'Private product', 'quantity': 2, 'unit_price': 10000, 'line_total': 20000}],
            }
            for invoice_id in (approved, pending, other):
                await save_invoice_analysis(db_path, invoice_id, data)
            await approve_invoice(db_path, approved, 19000, 950, data['items'])
            items = await list_invoices_for_user(db_path, 2, 'approved', 5, 0)
            text = _list_text('Принятые', 'approved', 0, 1, items)
            self.assertIn('ООО &lt;Поставщик&gt; &amp; партнёры', text)
            self.assertIn('19 000,00 ₽', text)
            self.assertIn('950,00', text)
            self.assertNotIn('Private product', text)
            path = await build_user_report_xlsx(db_path, 2, '2020-01-01', '2030-01-01', 'custom')
            try:
                book = load_workbook(path)
                try:
                    rows = list(book['Накладные'].values)
                    headers = rows[0]
                    records = {row[0]: dict(zip(headers, row)) for row in rows[1:]}
                    self.assertEqual(set(records), {approved, pending, missing})
                    self.assertNotIn(other, records)
                    self.assertEqual(records[approved]['Поставщик'], data['supplier'])
                    self.assertEqual(records[approved]['Сумма накладной'], 19000)
                    self.assertEqual(records[approved]['Вознаграждение'], 950)
                    self.assertEqual(records[pending]['Сумма накладной'], 20000)
                    self.assertEqual(records[missing]['Поставщик'], '—')
                    self.assertIsNone(records[missing]['Сумма накладной'])
                    self.assertNotIn('Private product', [cell.value for sheet in book for row in sheet for cell in row])
                finally:
                    book.close()
            finally:
                Path(path).unlink()
