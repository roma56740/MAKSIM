from __future__ import annotations

import sys
import types
import unittest
from io import BytesIO
from unittest.mock import patch

from openpyxl import Workbook


try:
    import openai  # noqa: F401
except ModuleNotFoundError:  # позволяет запускать чистые тесты без сетевого SDK
    openai_stub = types.ModuleType("openai")
    openai_stub.AsyncOpenAI = object
    sys.modules["openai"] = openai_stub

try:
    import aiosqlite  # noqa: F401
except ModuleNotFoundError:  # чистые тесты нормализации не открывают БД
    sys.modules["aiosqlite"] = types.ModuleType("aiosqlite")

from services.invoice_excel import _number, recognize_invoice_excel
from services.invoice_recognition import (
    InvoiceFile,
    _friendly_api_error,
    _model_candidates,
    normalize_invoice_result,
)
from services.invoice_format import TELEGRAM_SAFE_TEXT_LENGTH, render_invoice_message
from db.invoices import duplicate_match_reasons, invoice_identity


def _raw_result(*, items: list[dict], payable: float | None) -> dict:
    return {
        "document_type": "УПД",
        "invoice_number": "15",
        "invoice_date": "25.08.2026",
        "supplier": "Поставщик",
        "buyer": "Покупатель",
        "responsible_manager": None,
        "currency": "RUB",
        "subtotal_before_discount": None,
        "discount_amount": None,
        "amount_payable": payable,
        "total_amount": payable,
        "vat_amount": None,
        "confidence": 0.96,
        "warnings": [],
        "items": items,
    }


class InvoiceNormalizationTests(unittest.TestCase):
    def test_line_total_has_priority_over_price_before_discount(self) -> None:
        result = normalize_invoice_result(
            _raw_result(
                payable=1600,
                items=[
                    {
                        "article": "A-1",
                        "product_name": "Товар",
                        "quantity": 2,
                        "unit": "шт",
                        "unit_price_before_discount": None,
                        "discount_percent": None,
                        "discount_amount": None,
                        "unit_price": 1000,
                        "line_total": 1600,
                    }
                ],
            )
        )
        item = result["items"][0]
        self.assertEqual(item["unit_price_before_discount"], 1000)
        self.assertEqual(item["unit_price"], 800)
        self.assertEqual(item["line_total"], 1600)
        self.assertEqual(result["amount_payable"], 1600)

    def test_document_discount_is_distributed_to_final_total(self) -> None:
        result = normalize_invoice_result(
            _raw_result(
                payable=80000,
                items=[
                    {
                        "article": None,
                        "product_name": "Товар 1",
                        "quantity": 5,
                        "unit": "шт",
                        "unit_price_before_discount": None,
                        "discount_percent": None,
                        "discount_amount": None,
                        "unit_price": 12000,
                        "line_total": 60000,
                    },
                    {
                        "article": None,
                        "product_name": "Товар 2",
                        "quantity": 4,
                        "unit": "шт",
                        "unit_price_before_discount": None,
                        "discount_percent": None,
                        "discount_amount": None,
                        "unit_price": 12000,
                        "line_total": 48000,
                    },
                ],
            )
        )
        self.assertEqual(result["subtotal_before_discount"], 108000)
        self.assertEqual(result["discount_amount"], 28000)
        self.assertEqual(result["amount_payable"], 80000)
        self.assertEqual(sum(item["line_total"] for item in result["items"]), 80000)


class InvoiceExcelTests(unittest.TestCase):
    def test_russian_and_international_numbers(self) -> None:
        self.assertEqual(_number("1 234,56 ₽"), 1234.56)
        self.assertEqual(_number("1.234,56"), 1234.56)
        self.assertEqual(_number("1,234.56"), 1234.56)

    def test_final_payable_wins_over_total_before_discount(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Счёт № 15 от 25.08.2026"])
        sheet.append(["Наименование", "Количество", "Цена", "Сумма"])
        sheet.append(["Товар", 10, 10800, 108000])
        sheet.append(["Итого без скидки", None, None, 108000])
        sheet.append(["Итого к оплате", None, None, 80000])
        output = BytesIO()
        workbook.save(output)

        result = recognize_invoice_excel(
            InvoiceFile(
                data=output.getvalue(),
                filename="invoice.xlsx",
                mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        )
        self.assertEqual(result["amount_payable"], 80000)
        self.assertEqual(result["calculated_total"], 80000)
        self.assertEqual(result["discount_amount"], 28000)


class InvoiceDuplicateTests(unittest.TestCase):
    def test_same_document_is_flagged_despite_number_formatting(self) -> None:
        first = invoice_identity({
            "invoice_number": "УПД № 15/26",
            "invoice_date": "25.08.2026",
            "supplier": "ООО «Поставщик»",
            "amount_payable": 80_000,
        })
        second = invoice_identity({
            "invoice_number": "УПД-15/26",
            "invoice_date": "2026-08-25",
            "supplier": "ООО Поставщик",
            "total_amount": 80_000,
        })
        self.assertEqual(
            duplicate_match_reasons(first, second),
            ["тот же номер", "та же дата", "тот же поставщик", "та же сумма"],
        )

    def test_date_and_total_alone_are_not_enough(self) -> None:
        first = invoice_identity({
            "invoice_date": "25.08.2026",
            "supplier": "Поставщик А",
            "amount_payable": 80_000,
        })
        second = invoice_identity({
            "invoice_date": "25.08.2026",
            "supplier": "Поставщик Б",
            "amount_payable": 80_000,
        })
        self.assertEqual(duplicate_match_reasons(first, second), [])


class InvoiceReliabilityTests(unittest.TestCase):
    def test_configured_project_model_is_used_before_fallbacks(self) -> None:
        with patch.dict(
            "os.environ",
            {"OPENAI_MODEL": "available-model"},
            clear=True,
        ):
            candidates = _model_candidates()
        self.assertEqual(candidates[0], "available-model")
        self.assertIn("gpt-4o-mini", candidates)

    def test_rate_limit_has_actionable_safe_message(self) -> None:
        error = RuntimeError("secret response body")
        error.status_code = 429
        message = _friendly_api_error([error])
        self.assertIn("лимита", message)
        self.assertNotIn("secret", message)

    def test_large_invoice_is_rendered_as_one_safe_telegram_message(self) -> None:
        analysis = _raw_result(
            payable=150_000,
            items=[
                {
                    "article": f"A-{index}",
                    "product_name": "Очень длинное название товара & специальная серия " * 4,
                    "quantity": 2,
                    "unit": "шт",
                    "unit_price_before_discount": 600,
                    "discount_percent": None,
                    "discount_amount": 200,
                    "unit_price": 500,
                    "line_total": 1000,
                }
                for index in range(150)
            ],
        )
        rendered = render_invoice_message(
            normalize_invoice_result(analysis),
            invoice_id=42,
            footer="🟡 <b>СТАТУС · НА ПРОВЕРКЕ</b>",
        )
        self.assertLessEqual(len(rendered), TELEGRAM_SAFE_TEXT_LENGTH)
        self.assertIn("полный список сохранён", rendered)
        self.assertNotIn("& специальная", rendered)
        self.assertEqual(rendered.count("<b>"), rendered.count("</b>"))


if __name__ == "__main__":
    unittest.main()
