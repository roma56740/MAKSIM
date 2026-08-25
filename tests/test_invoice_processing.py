from __future__ import annotations

import sys
import types
import unittest
from io import BytesIO

from openpyxl import Workbook


try:
    import openai  # noqa: F401
except ModuleNotFoundError:  # позволяет запускать чистые тесты без сетевого SDK
    openai_stub = types.ModuleType("openai")
    openai_stub.AsyncOpenAI = object
    sys.modules["openai"] = openai_stub

from services.invoice_excel import _number, recognize_invoice_excel
from services.invoice_recognition import InvoiceFile, normalize_invoice_result


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


if __name__ == "__main__":
    unittest.main()
