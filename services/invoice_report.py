import math
from typing import Any

from db.invoices import decode_invoice_analysis


def invoice_report_details(invoice: dict[str, Any]) -> tuple[str, float | None]:
    analysis = decode_invoice_analysis(invoice) or {}
    supplier = next((
        str(value).strip()
        for value in (invoice.get('supplier_name'), invoice.get('company_name'), analysis.get('supplier'))
        if value is not None and str(value).strip()
    ), '—')
    for value in (
        invoice.get('deal_amount'), invoice.get('document_total'),
        analysis.get('amount_payable'), analysis.get('total_amount'), analysis.get('calculated_total'),
    ):
        try:
            amount = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(amount) and amount >= 0:
            return supplier, amount
    return supplier, None
