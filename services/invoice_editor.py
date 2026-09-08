"""Invoice editor rules: pure validation, calculations and stale-card tokens."""
from __future__ import annotations
import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime

HEADER_FIELDS = {
    'document_type': 'Тип документа', 'invoice_number': 'Номер документа',
    'invoice_date': 'Дата документа', 'supplier': 'Поставщик', 'buyer': 'Покупатель',
    'responsible_manager': 'Ответственный менеджер', 'currency': 'Валюта',
    'subtotal_before_discount': 'Сумма до скидки', 'discount_amount': 'Общая скидка',
    'amount_payable': 'К оплате', 'vat_amount': 'НДС',
}
ITEM_FIELDS = {
    'product_name': 'Название', 'article': 'Артикул', 'unit': 'Единица измерения',
    'quantity': 'Количество', 'unit_price': 'Цена после скидки', 'line_total': 'Сумма строки',
    'unit_price_before_discount': 'Цена до скидки', 'discount_percent': 'Скидка, %',
    'discount_amount': 'Скидка на строку',
}
NUMBERS = {'quantity', 'unit_price', 'line_total', 'unit_price_before_discount',
           'discount_percent', 'discount_amount', 'subtotal_before_discount', 'amount_payable', 'vat_amount'}


def revision(invoice):
    raw = str(invoice.get('analysis_json') or '') + '|' + str(invoice.get('status')) + '|' + str(invoice.get('analysis_status'))
    return hashlib.sha256(raw.encode()).hexdigest()[:10]


def decimal(value):
    return Decimal(str(value or 0))


def rounded(value):
    return float(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def parse_value(field, text):
    text = text.strip()
    if field in NUMBERS:
        try:
            value = Decimal(text.replace(' ', '').replace('\u00a0', '').replace(',', '.'))
        except InvalidOperation:
            raise ValueError('Введите число, например 1250,50.')
        if not value.is_finite() or value < 0 or value > Decimal('1000000000000'):
            raise ValueError('Введите конечное неотрицательное число до 1 000 000 000 000.')
        if field == 'quantity' and value <= 0:
            raise ValueError('Количество должно быть больше нуля.')
        if field == 'discount_percent' and value > 100:
            raise ValueError('Скидка должна быть от 0 до 100%.')
        value = value.quantize(Decimal('0.000001') if field == 'quantity' else Decimal('0.01'), rounding=ROUND_HALF_UP)
        if field == 'quantity' and value <= 0:
            raise ValueError('Минимальное количество — 0,000001.')
        return float(value)
    if not text or (text == '-' and field in {'product_name', 'currency', 'document_type'}):
        raise ValueError('Это поле не может быть пустым.')
    if text == '-':
        return None
    if len(text) > (500 if field in {'product_name', 'supplier', 'buyer'} else 150):
        raise ValueError('Слишком длинное значение. Сократите текст.')
    if field == 'invoice_date':
        for fmt in ('%d.%m.%Y', '%Y-%m-%d'):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                pass
        raise ValueError('Введите существующую дату в формате ДД.ММ.ГГГГ.')
    if field == 'currency':
        text = text.upper()
        if len(text) != 3 or not text.isascii() or not text.isalpha():
            raise ValueError('Введите код валюты из трёх букв: RUB, USD, EUR.')
    return text


def update_item(item, field, value):
    item[field] = value
    q = decimal(item.get('quantity'))
    if q <= 0:
        raise ValueError('Сначала укажите количество больше нуля.')
    p = decimal(item.get('unit_price'))
    before = decimal(item.get('unit_price_before_discount') if item.get('unit_price_before_discount') is not None else p)
    if field in {'quantity', 'unit_price'}:
        item['line_total'] = rounded(q * p)
        if field == 'unit_price' and before < p:
            before = p
    elif field == 'line_total':
        item['unit_price'] = float((decimal(value) / q).quantize(Decimal('0.000001')))
    elif field in {'discount_percent', 'discount_amount', 'unit_price_before_discount'}:
        discount = q * before * decimal(item.get('discount_percent')) / 100 if field != 'discount_amount' else decimal(value)
        if discount > q * before:
            raise ValueError('Скидка не может превышать сумму до скидки.')
        item['line_total'] = rounded(q * before - discount)
        item['unit_price'] = float((decimal(item['line_total']) / q).quantize(Decimal('0.000001')))
    if field in NUMBERS:
        total = decimal(item.get('line_total'))
        before = max(before, total / q)
        item['unit_price_before_discount'] = float(before)
        item['discount_amount'] = rounded(q * before - total)
        item['discount_percent'] = rounded((q * before - total) / (q * before) * 100) if before else 0.0


def recalculate(analysis):
    """Line edits update the line sum; the printed payable stays independently editable."""
    analysis['calculated_total'] = rounded(sum((decimal(x.get('line_total')) for x in analysis.get('items', [])), Decimal(0)))
    analysis['manual_edited'] = True


def apply_change(analysis, index, field, value):
    result = json.loads(json.dumps(analysis))
    if index >= 0:
        update_item(result['items'][index], field, value)
    else:
        result[field] = value
        if field == 'amount_payable':
            result['total_amount'] = value
    recalculate(result)
    return result
