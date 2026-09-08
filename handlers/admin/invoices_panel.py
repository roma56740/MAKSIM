from __future__ import annotations

import html
import math

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Settings
from db.invoices import decode_invoice_analysis, get_invoice_full
from db.invoices_admin import count_invoices_by_status, list_invoices_by_status
from filters.admin import IsAdmin
from services.invoice_workflow import money
from services.telegram_invoice_ui import display, rich_button, cell_text

router = Router()
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())
PAGE_SIZE = 6
STATUSES = {'pending': 'На проверке', 'approved': 'Принятые', 'rejected': 'Отклонённые'}
ICONS = {'pending': '◷', 'approved': '✓', 'rejected': '×'}


def _esc(value, limit=160):
    return html.escape(str(value if value is not None and value != '' else '—')[:limit])


def _analysis_label(invoice):
    status = invoice.get('analysis_status')
    if status == 'completed':
        a = decode_invoice_analysis(invoice) or {}
        suffix = ' · ⚠ повтор?' if a.get('duplicate_matches') else ''
        return f"{len(a.get('items') or [])} товаров{suffix}"
    return {'failed': '⚠ Нужна проверка', 'processing': '◷ Распознаётся'}.get(status, '◷ Ожидает распознавания')


def panel_view(status, items, page, total_pages, total, counts):
    """Compact ledger: each invoice has exactly one embedded open action."""
    from handlers.admin.invoice_editor import cb
    title = f"🧾 <b>Накладные</b> · {STATUSES[status]}"
    text = [title, f"Всего: {total} · Страница {page+1} из {total_pages}"]
    rich = ['<h2>Накладные</h2>', '<p>Выберите статус, затем откройте документ в таблице.</p>']
    rows = [[]]
    for key, label in STATUSES.items():
        label = f"{ICONS[key]} {label} · {counts[key]}"
        data = f'ainv:panel:{key}:0'
        style = 'success' if key == status else 'primary'
        rich.append('<tg-button-row>'+rich_button(label, data, style)+'</tg-button-row>')
        rows[0].append(InlineKeyboardButton(text=label, callback_data=data, style=style))
    rich += [f'<p><b>{STATUSES[status]}</b> · {total} документов · {page+1}/{total_pages}</p>']
    if items:
        rich.append('<table bordered striped compact><tr><th>Документ / компания</th><th align="right">К оплате</th></tr>')
        for inv in items:
            a = decode_invoice_analysis(inv) or {}
            ident = inv['id']
            number = inv.get('invoice_number') or a.get('invoice_number')
            company = inv.get('company_name') or a.get('supplier') or 'Компания не распознана'
            date = inv.get('invoice_date') or a.get('invoice_date') or str(inv.get('created_at') or '')[:10]
            amount = inv.get('deal_amount') if status == 'approved' else inv.get('document_total')
            currency = str(a.get('currency') or 'RUB')[:3]
            label = f"#{ident}" + (f" · № {str(number)[:50]}" if number else '')
            state = _analysis_label(inv)
            data = cb('page', inv)
            rich.append(f'<tr><td valign="top"><b>{cell_text(label, 90)}</b><br>{cell_text(company, 140)}<br><i>{cell_text(date, 20)}<br>{cell_text(state, 80)}</i></td>'
                        f'<td valign="top" align="right"><b>{cell_text(money(amount))}</b><br><i>{_esc(currency, 3)}</i></td></tr>'
                        f'<tr><td colspan="2" align="center">{rich_button("Открыть", data)}</td></tr>')
            text.append(f"<b>{_esc(label, 90)}</b> · {_esc(company, 100)}\n{_esc(date, 20)} · {_esc(state, 80)}\n<b>{money(amount)} {_esc(currency, 3)}</b>")
            rows.append([InlineKeyboardButton(text=f'Открыть #{ident} · {str(company)[:35]}', callback_data=data)])
        rich.append('</table>')
    else:
        rich.append('<blockquote>В этом статусе пока нет накладных.</blockquote>')
        text.append('В этом статусе пока нет накладных.')
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text='‹ Назад', callback_data=f'ainv:panel:{status}:{page-1}'))
    if page+1 < total_pages:
        nav.append(InlineKeyboardButton(text='Далее ›', callback_data=f'ainv:panel:{status}:{page+1}'))
    nav.append(InlineKeyboardButton(text='↻ Обновить', callback_data=f'ainv:panel:{status}:{page}'))
    rows.append(nav)
    return '\n\n'.join(text), InlineKeyboardMarkup(inline_keyboard=rows), ''.join(rich)


@router.message(F.text.in_({'🧾 Накладные', '🧾 Управление накладными'}))
async def admin_invoices_panel(message: Message, settings: Settings, state: FSMContext):
    await state.clear()
    await _send_panel(message, settings, status='pending', page=0)


@router.callback_query(F.data.startswith('ainv:panel:'))
async def admin_invoices_panel_page(query: CallbackQuery, settings: Settings, state: FSMContext):
    try:
        _, _, status, raw_page = query.data.split(':')
        page = int(raw_page)
        if status not in STATUSES: raise ValueError
    except (ValueError, AttributeError):
        await query.answer('Обновите меню накладных.', show_alert=True)
        return
    await query.answer()
    await state.clear()
    await _send_panel(query.message, settings, status, page, edit=True)


@router.callback_query(F.data.startswith('ainv:open:'))
async def admin_open_invoice_file(query: CallbackQuery, settings: Settings):
    await query.answer()
    try:
        invoice_id = int(query.data.split(':')[-1])
    except ValueError:
        return
    inv = await get_invoice_full(settings.db_path, invoice_id)
    if not inv:
        await query.message.answer('Накладная не найдена.')
        return
    from handlers.admin.invoice_editor import cb
    # Media captions don't support rich tables; this one return button remains native inline.
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='‹ Карточка и товары', callback_data=cb('page', inv))]])
    caption = f'📎 <b>Оригинал накладной #{invoice_id}</b>'
    if (inv.get('file_kind') or '').lower() == 'photo':
        await query.message.answer_photo(inv['file_id'], caption=caption, reply_markup=keyboard)
    else:
        await query.message.answer_document(inv['file_id'], caption=caption, reply_markup=keyboard)


async def _send_panel(message, settings, status, page, *, edit=False):
    counts = {key: await count_invoices_by_status(settings.db_path, key) for key in STATUSES}
    total = counts[status]
    pages = max(1, math.ceil(total/PAGE_SIZE))
    page = max(0, min(page, pages-1))
    items = await list_invoices_by_status(settings.db_path, status, limit=PAGE_SIZE, offset=page*PAGE_SIZE)
    text, kb, rich = panel_view(status, items, page, pages, total, counts)
    await display(message, text, kb, rich=rich, edit=edit)
