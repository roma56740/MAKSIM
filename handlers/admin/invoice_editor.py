from __future__ import annotations
import html
import math
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from config import Settings
from db import is_admin
from db.invoices import get_invoice_full, decode_invoice_analysis, save_invoice_analysis, list_invoice_items
from services.invoice_editor import HEADER_FIELDS, ITEM_FIELDS, NUMBERS, revision, parse_value, apply_change, recalculate, items_subtotal
from services.invoice_workflow import money, quantity, deal_amount_from_analysis
from services.telegram_invoice_ui import display, rich_button, rich_text, cell_text

router = Router()
PAGE = 6

class InvoiceEditor(StatesGroup):
    value = State()
    add = State()


def esc(value, limit=500):
    return html.escape(str(value if value is not None and value != '' else '—')[:limit])


def button(text, data, style=None):
    return InlineKeyboardButton(text=text, callback_data=data, **({'style': style} if style else {}))


def cb(action, inv, page=0, index=-1, field=-1):
    return f"iv:{action}:{inv['id']}:{page}:{index}:{field}:{revision(inv)}"


def value_text(key, value):
    if value is None:
        return '—'
    if key == 'quantity':
        return quantity(value)
    if key in NUMBERS:
        return money(value)
    return str(value)


def overview(inv, analysis, *, admin=True, page=0):
    if not admin:
        text = 'Расшифровка накладной доступна только администратору.'
        return text, InlineKeyboardMarkup(inline_keyboard=[]), rich_text(text)
    items = analysis.get('items') or []
    pages = max(1, math.ceil(len(items) / PAGE))
    page = max(0, min(page, pages-1))
    status = {'pending': '🟡 На проверке', 'approved': '✅ Принята', 'rejected': '❌ Отклонена'}.get(inv.get('status'), 'На проверке')
    lines = [f"🧾 <b>Накладная #{inv['id']}</b> · {status}"]
    lines += [f"<b>{label}:</b> {esc(value_text(key, analysis.get(key)), 180)}" for key, label in HEADER_FIELDS.items()]
    lines += [
             f"\nК оплате: <b>{money(deal_amount_from_analysis(analysis))} {esc(analysis.get('currency') or 'RUB', 3)}</b>",
             f"Сумма строк: <b>{money(analysis.get('calculated_total'))}</b> · НДС: {money(analysis.get('vat_amount'))}",
             f"Скидка: {money(analysis.get('discount_amount'))} · Товаров: <b>{len(items)}</b>"]
    if analysis.get('recognition_source') == 'manual':
        lines.append('✍️ Заполнено вручную администратором.')
        lines.append('Итоги заданы вручную.' if analysis.get('manual_total_fields') else 'Итоги рассчитываются по всем товарам.')
    if inv.get('analysis_status') != 'completed':
        lines.append({'processing': '⏳ Идёт распознавание. Обновите карточку немного позже.',
                      'failed': '⚠️ Распознавание не завершено. Можно добавить товары вручную.'}.get(inv.get('analysis_status'), '⏳ Данные ещё не распознаны.'))
    difference = round(deal_amount_from_analysis(analysis) - float(analysis.get('calculated_total') or 0), 2)
    if abs(difference) > 0.01:
        lines.append(f"\n⚠️ <b>Расхождение итога и строк: {money(difference)}</b>\nПроверьте скидки и итог документа.")
    if analysis.get('duplicate_matches') and admin:
        ids = ', '.join('#'+str(x.get('invoice_id')) for x in analysis['duplicate_matches'][:5])
        lines.append(f"⚠️ Возможный повтор: <b>{esc(ids)}</b>")
    warnings = analysis.get('warnings') or []
    if warnings:
        lines.append('<blockquote expandable>' + '\n'.join(esc(x, 140) for x in warnings[:3]) + '</blockquote>')
    lines.append(f"\n<b>Товары · {page+1}/{pages}</b>\nНажмите на товар, чтобы {'посмотреть и изменить данные' if admin else 'посмотреть подробности'}.")
    rows = []
    currency = esc(analysis.get('currency') or 'RUB', 3)
    payable = money(deal_amount_from_analysis(analysis))
    rich = (f"<h2>Накладная #{inv['id']}</h2><p><b>{esc(analysis.get('supplier'), 180)}</b><br>{status}</p>"
            '<table bordered striped compact><tr><th>К оплате</th><th>Товаров</th></tr>'
            f'<tr><td><b>{payable} {currency}</b></td><td><b>{len(items)}</b></td></tr></table>')
    if abs(difference) > 0.01:
        rich += f'<blockquote>⚠ <b>Итог отличается от суммы товаров на {money(difference)}</b><br>Проверьте скидки и сумму к оплате.</blockquote>'
    if inv.get('analysis_status') != 'completed':
        rich += '<blockquote>' + {
            'processing': '◷ Идёт распознавание. Обновите карточку позже.',
            'failed': '⚠ Распознавание не завершено. Можно добавить товары вручную.',
        }.get(inv.get('analysis_status'), '◷ Ожидает распознавания.') + '</blockquote>'
    if analysis.get('duplicate_matches') and admin:
        rich += f'<blockquote>⚠ Возможный повтор: <b>{esc(ids)}</b></blockquote>'
    info = [(label, value_text(key, analysis.get(key))) for key, label in HEADER_FIELDS.items()]
    info.append(('Сумма строк', money(analysis.get('calculated_total'))))
    if analysis.get('recognition_source') == 'manual':
        info.append(('Источник данных', 'Ручное заполнение'))
        info.append(('Расчёт итогов', 'Ручная сумма или скидка' if analysis.get('manual_total_fields') else 'Автоматически по всем товарам'))
    if admin:
        info += [('Отправитель', inv.get('user_full_name')), ('Телефон', inv.get('user_phone')),
                 ('Комментарий', inv.get('comment')), ('Причина отклонения', inv.get('reason'))]
        if inv.get('status') == 'approved':
            info += [('Вознаграждение', money(inv.get('reward_amount')))]
    rich += '<h3>Реквизиты и результаты проверки</h3><table bordered striped compact>'
    rich += ''.join(f'<tr><td>{label}</td><td>{cell_text(value, 500)}</td></tr>' for label, value in info)
    rich += '</table>'
    if warnings:
        rich += '<p>' + '<br>'.join(esc(w, 250) for w in warnings[:6]) + '</p>'
    rich += f'<h3>Товары · {page+1}/{pages}</h3><p>Откройте товар кнопкой внутри строки.</p>'
    table = ['<table bordered striped compact>']
    for i in range(page*PAGE, min((page+1)*PAGE, len(items))):
        item = items[i]
        name = str(item.get('product_name') or 'Без названия')
        data = cb('item', inv, page, i)
        rows.append([button(f"{i+1}. {name[:38]} · {money(item.get('line_total'))}", data)])
        lines.append(f"\n<b>{i+1}. {esc(name, 90)}</b>\n{quantity(item.get('quantity'))} {esc(item.get('unit'), 10)} × {money(item.get('unit_price'))} = <b>{money(item.get('line_total'))}</b>")
        item_action = rich_button('Изменить' if admin else 'Подробнее', data)
        q = esc(quantity(item.get('quantity')), 24)
        unit = esc(item.get('unit') or 'шт', 12)
        price = money(item.get('unit_price'))
        line_total = money(item.get('line_total'))
        table.append(f'<tr><th colspan="2" align="left">{i+1}. {cell_text(name, 160)}</th></tr>'
                     f'<tr><td>Количество<br>{q} {unit}<br>Цена<br>{price}</td>'
                     f'<td align="right">Сумма<br><b>{line_total}</b></td></tr>'
                     f'<tr><td colspan="2" align="center">{item_action}</td></tr>')
    table.append('</table>')
    rich += ''.join(table) if items else '<p>Пока нет товаров.</p>'
    nav = []
    if page:
        nav.append(button('‹ Назад', cb('page', inv, page-1)))
    if page+1 < pages:
        nav.append(button('Далее ›', cb('page', inv, page+1)))
    if nav:
        rows.append(nav)
    rows.append([button('✎ Изменить реквизиты и суммы', cb('header', inv, 0), 'primary')])
    if admin:
        if inv.get('status') != 'approved' and inv.get('analysis_status') != 'processing':
            rows.append([button('✍️ Ручной ввод', cb('manual', inv), 'primary')])
        rows.append([button('＋ Добавить товар', cb('add', inv, page), 'success'), button('📎 Оригинал', f"ainv:open:{inv['id']}")])
        rows.append([button('✅ Подтвердить данные', cb('confirm', inv, page), 'success')])
        if inv.get('status') != 'approved':
            rows.append([button('Распознать заново', cb('retry', inv, page)), button('Отклонить', f"ainv:reject:{inv['id']}", 'danger')])
        else:
            rows.append([button('Вернуть на проверку', f"ainv:pending:{inv['id']}")])
        rows.append([button('‹ Все накладные', f"ainv:panel:{inv.get('status', 'pending')}:0"), button('Обновить', cb('page', inv, page))])
    else:
        rows.append([button('‹ Мои накладные', 'uinv:menu')])
    return '\n'.join(lines), InlineKeyboardMarkup(inline_keyboard=rows), rich


async def load(settings, invoice_id, actor):
    inv = await get_invoice_full(settings.db_path, invoice_id)
    admin = await is_admin(settings.db_path, actor, settings.admin_ids)
    if not inv or not admin:
        return None, {}, admin
    analysis = decode_invoice_analysis(inv) or {'items': []}
    if not analysis.get('items') and inv.get('status') == 'approved':
        analysis['items'] = await list_invoice_items(settings.db_path, invoice_id)
        analysis.setdefault('amount_payable', inv.get('deal_amount'))
        recalculate(analysis)
    return inv, analysis, admin


async def send_overview(message, settings, invoice_id, *, actor=None, admin=True, page=0, edit=False, chat_id=None):
    target = chat_id or message.chat.id
    if actor is not None and actor != target and not await is_admin(settings.db_path, actor, settings.admin_ids):
        return
    inv, analysis, admin = await load(settings, invoice_id, target)
    if not inv:
        return
    text, kb, rich = overview(inv, analysis, admin=admin, page=page)
    return await display(message, text, kb, rich=rich, edit=edit, chat_id=chat_id)


async def detail(message, inv, analysis, admin, *, index=-1, page=0, edit=True):
    if not admin:
        return
    rows = []
    fields = HEADER_FIELDS if index < 0 else ITEM_FIELDS
    data = analysis if index < 0 else analysis['items'][index]
    if index < 0:
        title = f"🏢 <b>Компания и суммы · #{inv['id']}</b>"
        keys = list(fields)
        page = 0
    else:
        title = f"📦 <b>Товар {index+1} из {len(analysis['items'])}</b>\n<b>{esc(data.get('product_name'))}</b>"
        keys = list(fields)
    lines = [title]
    rich = rich_text(title)
    rich += '<p>Нажмите «✎ Изменить» возле нужного поля.</p>'
    rich += '<table bordered striped compact><tr><th>Поле</th><th>Значение</th></tr>'
    for key in keys:
        lines.append(f"<b>{fields[key]}:</b> {esc(value_text(key, data.get(key)))}")
        rich += f'<tr><td valign="top">{cell_text(fields[key])}</td><td>{cell_text(value_text(key, data.get(key)))}</td></tr>'
        if admin:
            field_action = cb('field', inv, page, index, list(fields).index(key))
            rows.append([button(f"✎ {fields[key]} · {value_text(key, data.get(key))[:32]}", field_action)])
            rich += '<tr><td colspan="2" align="center">' + rich_button('✎ Изменить', field_action) + '</td></tr>'
    rich += '</table>'
    if inv.get('status') == 'approved':
        rich += '<blockquote>Изменения обновят аналитику. Процент вознаграждения сохраняется.</blockquote>'
    if admin:
        lines.append('\nНажмите «✎ Изменить» возле поля и отправьте новое значение. Изменения сохраняются сразу.')
        if inv.get('status') == 'approved':
            lines.append('Изменения принятой накладной обновят аналитику. Процент вознаграждения сохраняется.')
    if index < 0:
        if admin:
            rows.append([button('К оплате = сумма товаров', cb('total', inv, page), 'primary')])
    else:
        nav=[]
        if index: nav.append(button('‹ Предыдущий', cb('item', inv, (index-1)//PAGE, index-1)))
        if index+1 < len(analysis['items']): nav.append(button('Следующий ›', cb('item', inv, (index+1)//PAGE, index+1)))
        if nav: rows.append(nav)
        if admin: rows.append([button('Удалить товар', cb('delete', inv, page, index), 'danger')])
    rows.append([button('‹ К товарам', cb('page', inv, page if index >= 0 else 0))])
    text = '\n\n'.join(lines)
    await display(message, text, InlineKeyboardMarkup(inline_keyboard=rows), rich=rich, edit=edit)


@router.callback_query(F.data.startswith('iv:'))
async def navigate(query: CallbackQuery, state: FSMContext, settings: Settings):
    try:
        _, action, ident, page, index, field, version = query.data.split(':')
        ident, page, index, field = map(int, (ident, page, index, field))
        if min(ident, page) < 0 or index < -1: raise ValueError
    except (ValueError, AttributeError):
        await query.answer('Кнопка устарела.', show_alert=True)
        return
    inv, analysis, admin = await load(settings, ident, query.from_user.id)
    if not admin:
        await query.answer('Расшифровка накладной доступна только администратору.', show_alert=True)
        return
    if not inv:
        await query.answer('Накладная недоступна.', show_alert=True)
        return
    if action not in {'page', 'header'} and version != revision(inv):
        await query.answer('Данные изменились. Карточка обновлена.', show_alert=True)
        await state.clear()
        await send_overview(query.message, settings, ident, actor=query.from_user.id, page=page, edit=True)
        return
    if action not in {'page', 'item', 'header'} and inv.get('analysis_status') == 'processing':
        await query.answer('Дождитесь окончания распознавания.', show_alert=True)
        return
    if action == 'confirm':
        from handlers.admin.invoices import admin_invoice_confirm
        await admin_invoice_confirm(query.model_copy(update={'data': f'ainv:confirm:{ident}'}), state, settings)
        return
    await query.answer()
    await state.clear()
    if action == 'manual':
        from handlers.admin.invoice_manual import start_manual
        await start_manual(query.message, state, inv, analysis)
    elif action == 'page':
        await send_overview(query.message, settings, ident, actor=query.from_user.id, page=page, edit=True)
    elif action == 'header':
        await detail(query.message, inv, analysis, admin, page=min(page, (len(HEADER_FIELDS)-1)//PAGE))
    elif action == 'item' and 0 <= index < len(analysis.get('items', [])):
        await detail(query.message, inv, analysis, admin, index=index, page=page)
    elif action == 'field':
        fields = HEADER_FIELDS if index < 0 else ITEM_FIELDS
        if field not in range(len(fields)) or (index >= 0 and index >= len(analysis.get('items', []))): return
        key = list(fields)[field]
        current = analysis if index < 0 else analysis['items'][index]
        await state.set_state(InvoiceEditor.value)
        await state.update_data(invoice_id=ident, index=index, page=page, field=key, revision=version)
        kb=InlineKeyboardMarkup(inline_keyboard=[[button('Отмена', cb('header' if index < 0 else 'item', inv, page, index))]])
        hint = 'Введите число. Можно использовать запятую.' if key in NUMBERS else 'Отправьте новый текст. Знак «-» очищает необязательное поле.'
        await display(query.message, f"✏️ <b>{fields[key]}</b>\n\nСейчас: <code>{esc(value_text(key, current.get(key)))}</code>\n\n{hint}", kb, edit=True)
    elif action == 'add':
        if len(analysis.get('items', [])) >= 150:
            await query.message.answer('В накладной может быть до 150 товаров.'); return
        await state.set_state(InvoiceEditor.add)
        await state.update_data(invoice_id=ident, page=page, revision=version, step='product_name', draft={})
        await display(query.message, '＋ <b>Новый товар · шаг 1 из 3</b>\n\nОтправьте название товара.', InlineKeyboardMarkup(inline_keyboard=[[button('Отмена', cb('page', inv, page))]]), edit=True)
    elif action in {'delete', 'total', 'retry'}:
        if action == 'delete' and not 0 <= index < len(analysis.get('items', [])): return
        prompt = {'delete': f"Удалить товар <b>{esc(analysis['items'][index].get('product_name'))}</b>?" if action == 'delete' else '',
                  'total': 'Заменить итог к оплате суммой товарных строк? Общая скидка будет рассчитана по товарам.',
                  'retry': 'Распознать оригинал заново? Ручные изменения реквизитов и товаров будут заменены результатом ИИ.'}[action]
        rows = [[button('Подтвердить', cb(action+'ok', inv, page, index), 'danger' if action != 'total' else 'success')], [button('Отмена', cb('page', inv, page))]]
        await display(query.message, prompt, InlineKeyboardMarkup(inline_keyboard=rows), edit=True)
    elif action == 'retryok':
        from handlers.admin.invoices import _recognize_and_show
        await _recognize_and_show(query.message, settings, ident)
    elif action in {'deleteok', 'totalok'}:
        if action == 'deleteok':
            if not 0 <= index < len(analysis.get('items', [])): return
            analysis['items'].pop(index)
        if action == 'totalok':
            analysis['manual_total_fields'] = []
        try:
            recalculate(analysis)
            if action == 'totalok':
                analysis['amount_payable'] = analysis['total_amount'] = analysis['calculated_total']
                analysis['subtotal_before_discount'] = items_subtotal(analysis.get('items', []))
                analysis['discount_amount'] = max(0, round(analysis['subtotal_before_discount']-analysis['calculated_total'], 2))
            await save_invoice_analysis(settings.db_path, ident, analysis, expected_revision=version)
        except ValueError as exc:
            await query.message.answer(esc(exc))
        await send_overview(query.message, settings, ident, actor=query.from_user.id, page=page, edit=True)


@router.message(InvoiceEditor.value, F.text.in_({'/cancel', '❌ Отмена'}))
@router.message(InvoiceEditor.add, F.text.in_({'/cancel', '❌ Отмена'}))
async def cancel_input(message: Message, state: FSMContext, settings: Settings):
    data = await state.get_data()
    await state.clear()
    await send_overview(message, settings, data['invoice_id'], actor=message.from_user.id)


@router.message(InvoiceEditor.value, ~F.text.in_({'🧾 Накладные', '🧾 Управление накладными', '/start'}))
@router.message(InvoiceEditor.add, ~F.text.in_({'🧾 Накладные', '🧾 Управление накладными', '/start'}))
async def input_value(message: Message, state: FSMContext, settings: Settings):
    data = await state.get_data()
    inv, analysis, admin = await load(settings, data['invoice_id'], message.from_user.id)
    if not inv or not admin:
        await state.clear(); await message.answer('Нет доступа.'); return
    if revision(inv) != data['revision'] or inv.get('analysis_status') == 'processing':
        await state.clear()
        await message.answer('Данные изменились. Откройте нужное поле заново.')
        await send_overview(message, settings, inv['id'], actor=message.from_user.id)
        return
    if not message.text:
        await message.answer('Отправьте значение текстом.'); return
    adding = await state.get_state() == InvoiceEditor.add.state
    key = data['step'] if adding else data['field']
    try:
        value = parse_value(key, message.text)
        if adding:
            draft = data['draft']; draft[key] = value
            if key != 'unit_price':
                step = 'quantity' if key == 'product_name' else 'unit_price'
                await state.update_data(draft=draft, step=step)
                kb = InlineKeyboardMarkup(inline_keyboard=[[button('Отмена', cb('page', inv, data['page']))]])
                await display(message, '＋ <b>Новый товар · '+('шаг 2 из 3</b>\n\nВведите количество.' if step == 'quantity' else 'шаг 3 из 3</b>\n\nВведите цену за единицу после скидки.'), kb)
                return
            draft.update(article=None, unit='шт', unit_price_before_discount=value, discount_percent=0, discount_amount=0)
            from services.invoice_editor import update_item
            update_item(draft, 'unit_price', value)
            analysis.setdefault('items', []).append(draft)
            recalculate(analysis)
            index = len(analysis['items'])-1
        else:
            index = data['index']
            analysis = apply_change(analysis, index, key, value)
        await save_invoice_analysis(settings.db_path, inv['id'], analysis, expected_revision=data['revision'])
    except (ValueError, IndexError) as exc:
        await message.answer('⚠️ '+esc(exc)); return
    await state.clear()
    inv = await get_invoice_full(settings.db_path, inv['id'])
    note = ''
    if adding or index >= 0:
        note = ' Итоги пересчитаны по всем товарам.' if analysis.get('recognition_source') == 'manual' and not analysis.get('manual_total_fields') else ' Проверьте отдельно указанную сумму к оплате.'
    await message.answer('✅ Сохранено.' + note)
    await detail(message, inv, analysis, True, index=index, page=index//PAGE if index >= 0 else data['page'], edit=False)
