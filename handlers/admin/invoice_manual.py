from __future__ import annotations

import asyncio
from functools import wraps
from secrets import token_hex
from weakref import WeakValueDictionary

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from config import Settings
from db.invoices import get_invoice_full, save_invoice_analysis
from handlers.admin.invoice_editor import button, esc, load, send_overview, value_text
from services.invoice_editor import (
    HEADER_FIELDS, ITEM_FIELDS, NUMBERS, apply_change, decimal,
    manual_analysis, manual_item, parse_value, recalculate, revision, rounded,
)
from services.invoice_workflow import money, quantity
from services.telegram_invoice_ui import display, rich_button, cell_text

router = Router()
_locks = WeakValueDictionary()


class ManualInvoice(StatesGroup):
    value = State()
    choice = State()
    review = State()


def serialized(handler):
    @wraps(handler)
    async def wrapped(event, *args, **kwargs):
        lock = _locks.get(event.from_user.id)
        if lock is None:
            lock = asyncio.Lock()
            _locks[event.from_user.id] = lock
        async with lock:
            return await handler(event, *args, **kwargs)
    return wrapped


def action(data, name):
    return f"im:{name}:{data['invoice_id']}:{data['nonce']}"


def draft_analysis(data):
    total = rounded(sum((decimal(x['line_total']) for x in data['items']), decimal(0)))
    result = manual_analysis(data.get('supplier'), data['items'], data.get('amount_payable', total), data['currency'])
    result.update(data['headers'])
    result['manual_total_fields'] = list(data.get('manual_total_fields') or [])
    recalculate(result)
    return result


async def show_fields(message, state, scope, index=-1, *, edit=False):
    await state.set_state(ManualInvoice.choice)
    await state.update_data(step='fields', nonce=token_hex(4), scope=scope, index=index)
    data = await state.get_data()
    fields = HEADER_FIELDS if scope == 'h' else ITEM_FIELDS
    values = draft_analysis(data) if scope == 'h' else data['items'][index]
    title = 'Реквизиты и суммы' if scope == 'h' else f'Товар № {index+1}'
    text = [f'✍️ <b>{title} · черновик #{data["invoice_id"]}</b>']
    rich = f'<h3>{title} · черновик #{data["invoice_id"]}</h3><p>Нажмите «✎ Изменить» возле поля. Изменения сохраняются в черновике.</p>'
    if scope == 'h':
        rich += '<p>' + ('Сумма или скидка указана вручную. Для возврата к расчёту по товарам нажмите «Итоги по всем товарам».'
                       if data.get('manual_total_fields') else 'Итоги рассчитываются автоматически по всем товарам.') + '</p>'
    rich += '<table bordered striped compact><tr><th>Поле</th><th>Значение</th></tr>'
    rows = []
    for field_index, (key, label) in enumerate(fields.items()):
        value = value_text(key, values.get(key))
        text.append(f'<b>{label}:</b> {esc(value)}')
        callback = action(data, f'field_{scope}_{index}_{field_index}')
        rows.append([button(f'✎ {label}', callback)])
        rich += f'<tr><td>{cell_text(label)}</td><td>{cell_text(value)}</td></tr>'
        rich += '<tr><td colspan="2" align="center">' + rich_button('✎ Изменить', callback) + '</td></tr>'
    rich += '</table>'
    rows += [[button('✅ Продолжить', action(data, 'resume'))], [button('❌ Отмена', action(data, 'cancel'))]]
    if scope == 'h':
        rows.insert(-1, [button('🔄 Итоги по всем товарам', action(data, 'auto_total'))])
    await display(message, '\n'.join(text), InlineKeyboardMarkup(inline_keyboard=rows), rich=rich, edit=edit)


async def show_items(message, state, page=0, *, edit=False):
    data = await state.get_data()
    page = max(0, min(page, max(0, (len(data['items'])-1)//6)))
    await state.set_state(ManualInvoice.choice)
    await state.update_data(step='items_list', nonce=token_hex(4))
    data = await state.get_data()
    rows = [[button(f"{index+1}. {item['product_name'][:40]}", action(data, f'item_{index}'))]
            for index, item in enumerate(data['items']) if page*6 <= index < (page+1)*6]
    nav = []
    if page: nav.append(button('‹ Назад', action(data, f'items_{page-1}')))
    if (page+1)*6 < len(data['items']): nav.append(button('Далее ›', action(data, f'items_{page+1}')))
    if nav: rows.append(nav)
    rows += [[button('✅ Продолжить', action(data, 'resume'))], [button('❌ Отмена', action(data, 'cancel'))]]
    await display(message, f'📦 <b>Товары черновика · {page+1}</b>\nВыберите товар, чтобы заполнить или изменить любое из его 9 полей.',
                  InlineKeyboardMarkup(inline_keyboard=rows), edit=edit)


async def show_review(message, state, *, edit=False):
    await state.update_data(step='review', nonce=token_hex(4))
    await state.set_state(ManualInvoice.review)
    data = await state.get_data()
    analysis = draft_analysis(data)
    total, amount = analysis['calculated_total'], analysis['amount_payable']
    difference = rounded(decimal(amount) - decimal(total))
    warning = f'\n⚠️ Итог отличается от суммы товаров на {money(difference)}. Проверьте документ.' if abs(difference) > 0.01 else ''
    text = [f'🧾 <b>Проверка перед сохранением · #{data["invoice_id"]}</b>']
    text += [f'<b>{label}:</b> {esc(value_text(key, analysis.get(key)))}' for key, label in HEADER_FIELDS.items()]
    text += [f"Товаров: {len(data['items'])}\nСумма строк: {money(total)}{warning}",
             'Проверьте реквизиты и товары. Сохранение не начисляет вознаграждение — накладную нужно отдельно подтвердить.']
    rows = [[button('🏢 Все реквизиты', action(data, 'headers'))],
            [button('📦 Все поля товаров', action(data, 'items_0'))],
            [button('✅ Сохранить', action(data, 'save'), 'success')],
            [button('‹ Назад', action(data, 'back'))], [button('❌ Отмена', action(data, 'cancel'))]]
    await display(message, '\n\n'.join(text), InlineKeyboardMarkup(inline_keyboard=rows), edit=edit)


async def resume(message, state, *, edit=False):
    data = await state.get_data()
    step = data.get('return_step', 'choice')
    if step == 'review':
        await show_review(message, state, edit=edit)
    elif step == 'choice' and data['items']:
        await show_choice(message, state, edit=edit)
    else:
        if step == 'supplier' and data.get('supplier'): step = 'product_name'
        await prompt(message, state, step, edit=edit)


async def prompt(message, state, step, *, edit=False):
    await state.set_state(ManualInvoice.value)
    await state.update_data(step=step, nonce=token_hex(4))
    data = await state.get_data()
    item = data.get('item') or {}
    labels = {
        'supplier': '🏢 Укажите название поставщика.',
        'product_name': f"📦 Товар № {(data['editing_item_index'] if data.get('editing_item_index') is not None else len(data['items'])) + 1}. Отправьте название товара.",
        'quantity': '🔢 Укажите количество. Например: 2 или 0,5.',
        'unit_price_before_discount': '💰 Укажите цену за единицу до скидки.',
        'unit_price': '🏷 Укажите цену за единицу после скидки. Если скидки нет, отправьте «-».',
    }
    if step == 'line_total':
        calculated = rounded(decimal(item['quantity']) * decimal(item['unit_price']))
        text = f'🧾 Итог строки по количеству и цене: <b>{money(calculated)}</b>.\nОтправьте «-», чтобы оставить его, или введите сумму из документа.'
    elif step == 'amount_payable':
        calculated = rounded(sum((decimal(x['line_total']) for x in data['items']), decimal(0)))
        text = f'💰 Общая сумма товаров: <b>{money(calculated)}</b>.\nВведите итог к оплате по накладной или «-», если он совпадает.'
    else:
        text = labels[step]
    rows = []
    rows.append([button('🏢 Все реквизиты', action(data, 'headers'))])
    if step != 'supplier':
        rows.append([button('‹ Назад', action(data, 'back'))])
    rows.append([button('❌ Отмена', action(data, 'cancel'))])
    await display(message, f"✍️ <b>Ручной ввод · #{data['invoice_id']}</b>\n\n{text}",
                  InlineKeyboardMarkup(inline_keyboard=rows), edit=edit)


async def start_manual(message, state, inv, analysis):
    if inv['status'] == 'approved' or inv.get('analysis_status') == 'processing':
        await message.answer('Верните накладную на проверку или дождитесь окончания распознавания.')
        return
    await state.clear()
    await state.update_data(
        invoice_id=inv['id'], revision=revision(inv), items=[], item={},
        currency=analysis.get('currency') or 'RUB', nonce=token_hex(4),
        manual_total_fields=[],
        headers={key: analysis[key] for key in (
            'document_type', 'invoice_number', 'invoice_date', 'buyer', 'responsible_manager', 'vat_amount',
        ) if analysis.get(key) is not None},
    )
    if analysis.get('items'):
        await state.set_state(ManualInvoice.choice)
        data = await state.get_data()
        await state.update_data(step='replace')
        await display(message,
            'В накладной уже есть товары. Ручное заполнение заменит их только после нажатия «Сохранить». До этого прежние данные сохраняются.',
            InlineKeyboardMarkup(inline_keyboard=[
                [button('✍️ Начать заполнение', action(data, 'begin'))],
                [button('❌ Отмена', action(data, 'cancel'))],
            ]), edit=True)
    else:
        await prompt(message, state, 'supplier', edit=True)


@router.callback_query(F.data.startswith('ainv:manual:'))
@serialized
async def open_manual(call: CallbackQuery, state: FSMContext, settings: Settings):
    try:
        ident = int(call.data.split(':')[2])
    except (ValueError, IndexError):
        await call.answer('Обновите карточку накладной.', show_alert=True)
        return
    inv, analysis, admin = await load(settings, ident, call.from_user.id)
    if not inv or not admin:
        await call.answer('Нет доступа.', show_alert=True)
        return
    if inv['status'] == 'approved' or inv.get('analysis_status') == 'processing':
        await call.answer('Верните накладную на проверку или дождитесь окончания распознавания.', show_alert=True)
        return
    await call.answer()
    await start_manual(call.message, state, inv, analysis)


async def current_invoice(settings, state, user_id):
    data = await state.get_data()
    inv, _, admin = await load(settings, data.get('invoice_id', 0), user_id)
    if not inv or not admin:
        return None, 'Нет доступа.'
    if inv['status'] == 'approved' or inv.get('analysis_status') == 'processing' or revision(inv) != data.get('revision'):
        return None, 'Накладная изменилась или распознаётся. Откройте её заново; ручной черновик не сохранён.'
    return inv, None


async def show_choice(message, state, *, edit=False):
    await state.set_state(ManualInvoice.choice)
    await state.update_data(step='choice', nonce=token_hex(4))
    data = await state.get_data()
    item = data['items'][-1]
    total = rounded(sum((decimal(x['line_total']) for x in data['items']), decimal(0)))
    totals = draft_analysis(data)
    rows = []
    if len(data['items']) < 150:
        rows.append([button('＋ Следующий товар', action(data, 'next'))])
    rows += [[button('✅ Готово — итог накладной', action(data, 'total'))],
             [button('🏢 Все реквизиты', action(data, 'headers'))],
             [button('📦 Все поля товаров', action(data, 'items_0'))],
             [button('✎ Исправить последний товар', action(data, 'edit_item'))],
             [button('❌ Отмена', action(data, 'cancel'))]]
    await display(message,
        f"📦 <b>{esc(item['product_name'])}</b>\n"
        f"Количество: {quantity(item['quantity'])}\n"
        f"Цена до скидки: {money(item['unit_price_before_discount'])}\n"
        f"Цена после скидки: {money(item['unit_price'])}\n"
        f"Итог строки: <b>{money(item['line_total'])}</b>\n\n"
        f"В черновике товаров: <b>{len(data['items'])}</b> · Сумма строк: <b>{money(total)}</b>\n"
        f"До скидок: {money(totals['subtotal_before_discount'])}\n"
        f"Общая скидка: {money(totals['discount_amount'])}\n"
        f"К оплате: <b>{money(totals['amount_payable'])}</b>",
        InlineKeyboardMarkup(inline_keyboard=rows), edit=edit)


@router.callback_query(F.data.startswith('im:'))
@serialized
async def manual_action(call: CallbackQuery, state: FSMContext, settings: Settings):
    data = await state.get_data()
    try:
        _, name, ident, nonce = call.data.split(':')
        valid = int(ident) == data.get('invoice_id') and nonce == data.get('nonce')
    except (ValueError, AttributeError):
        valid = False
    if not valid or await state.get_state() not in {ManualInvoice.value.state, ManualInvoice.choice.state, ManualInvoice.review.state}:
        await call.answer('Кнопка устарела. Продолжите в последнем сообщении.', show_alert=True)
        return
    inv, error = await current_invoice(settings, state, call.from_user.id)
    if error:
        await state.clear()
        await call.answer(error, show_alert=True)
        return
    await call.answer()
    step = data['step']
    if name == 'cancel':
        await state.clear()
        await call.message.answer('Ручное заполнение отменено. Прежние данные сохранены.')
        await send_overview(call.message, settings, inv['id'], actor=call.from_user.id)
    elif name == 'begin' and step == 'replace':
        await prompt(call.message, state, 'supplier', edit=True)
    elif name == 'next' and step == 'choice' and len(data['items']) < 150:
        await state.update_data(item={}, editing_item_index=None)
        await prompt(call.message, state, 'product_name', edit=True)
    elif name == 'edit_item' and step == 'choice':
        await state.update_data(item=dict(data['items'][-1]), editing_item_index=len(data['items'])-1)
        await prompt(call.message, state, 'product_name', edit=True)
    elif name == 'total' and step == 'choice' and data['items']:
        await prompt(call.message, state, 'amount_payable', edit=True)
    elif name == 'headers' and step != 'replace':
        if step not in {'fields', 'items_list', 'field'}:
            await state.update_data(return_step=step)
        await show_fields(call.message, state, 'h', edit=True)
    elif name.startswith('items_') and data['items'] and step != 'replace':
        try: page = int(name.split('_')[1])
        except ValueError: return
        if step not in {'fields', 'items_list', 'field'}:
            await state.update_data(return_step=step)
        await show_items(call.message, state, page, edit=True)
    elif name.startswith('item_') and step == 'items_list':
        try: index = int(name.split('_')[1])
        except ValueError: return
        if 0 <= index < len(data['items']):
            await show_fields(call.message, state, 'i', index, edit=True)
    elif name.startswith('field_') and step == 'fields':
        try:
            _, scope, index, field_index = name.split('_')
            index, field_index = int(index), int(field_index)
            fields = HEADER_FIELDS if scope == 'h' else ITEM_FIELDS
            if scope != data['scope'] or index != data['index'] or not 0 <= field_index < len(fields): return
            key = list(fields)[field_index]
        except (ValueError, IndexError): return
        await state.set_state(ManualInvoice.value)
        await state.update_data(step='field', field=key, nonce=token_hex(4))
        data = await state.get_data()
        values = draft_analysis(data) if scope == 'h' else data['items'][index]
        hint = 'Введите число. Можно использовать запятую.' if key in NUMBERS else 'Введите текст. Знак «-» очищает необязательное поле.'
        if key == 'vat_amount': hint += ' Знак «-» оставит НДС не указанным.'
        await display(call.message, f"✎ <b>{fields[key]}</b>\nСейчас: {esc(value_text(key, values.get(key)))}\n\n{hint}",
                      InlineKeyboardMarkup(inline_keyboard=[[button('‹ К полям', action(data, 'fields'))],
                                                           [button('❌ Отмена', action(data, 'cancel'))]]), edit=True)
    elif name == 'fields' and step == 'field':
        await show_fields(call.message, state, data['scope'], data['index'], edit=True)
    elif name == 'resume' and step in {'fields', 'items_list'}:
        await resume(call.message, state, edit=True)
    elif name == 'auto_total' and step == 'fields' and data['scope'] == 'h':
        headers = {key:value for key,value in data['headers'].items() if key not in {'subtotal_before_discount','discount_amount'}}
        await state.update_data(headers=headers, manual_total_fields=[])
        await show_fields(call.message, state, 'h', edit=True)
    elif name == 'back':
        steps = ['supplier', 'product_name', 'quantity', 'unit_price_before_discount', 'unit_price', 'line_total']
        if step in {'amount_payable', 'review'}:
            await show_choice(call.message, state, edit=True)
        elif step in steps[1:]:
            if step == 'product_name' and data['items']:
                await show_choice(call.message, state, edit=True)
            else:
                await prompt(call.message, state, steps[steps.index(step)-1], edit=True)
    elif name == 'save' and step == 'review':
        if not data.get('supplier') or not data['items']:
            await call.message.answer('Укажите поставщика и добавьте хотя бы один товар.')
            return
        analysis = draft_analysis(data)
        try:
            await save_invoice_analysis(settings.db_path, inv['id'], analysis, expected_revision=data['revision'])
        except ValueError as exc:
            await state.clear()
            await call.message.answer(esc(exc))
            return
        await state.clear()
        await call.message.answer('✅ Накладная заполнена вручную. Проверьте карточку и подтвердите данные для начисления вознаграждения.')
        await send_overview(call.message, settings, inv['id'], actor=call.from_user.id)


@router.message(ManualInvoice.value)
@router.message(ManualInvoice.choice)
@router.message(ManualInvoice.review)
@serialized
async def manual_input(message: Message, state: FSMContext, settings: Settings):
    if message.text in {'/start', '🧾 Накладные', '🧾 Управление накладными'}:
        await state.clear()
        if message.text == '/start':
            from handlers.start import cmd_start
            await cmd_start(message, state, settings)
        else:
            from handlers.admin.invoices_panel import admin_invoices_panel
            from db import is_admin
            if await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
                await admin_invoices_panel(message, settings, state)
        return
    inv, error = await current_invoice(settings, state, message.from_user.id)
    if error:
        await state.clear()
        await message.answer(error)
        return
    if message.text in {'/cancel', '❌ Отмена'}:
        await state.clear()
        await message.answer('Ручное заполнение отменено. Прежние данные сохранены.')
        await send_overview(message, settings, inv['id'], actor=message.from_user.id)
        return
    if await state.get_state() != ManualInvoice.value.state:
        await message.answer('Выберите действие кнопкой под последним сообщением.')
        return
    if not message.text:
        await message.answer('Отправьте значение текстом.')
        return
    data = await state.get_data()
    step, text = data['step'], message.text.strip()
    item = data.get('item') or {}
    try:
        if step == 'field':
            key = data['field']
            value = None if key == 'vat_amount' and text == '-' else parse_value(key, text)
            if key == 'supplier' and not value:
                raise ValueError('Укажите название поставщика.')
            if data['scope'] == 'h':
                updated = apply_change(draft_analysis(data), -1, key, value)
                fixed = updated.get('manual_total_fields') or []
                headers = dict(data['headers'])
                if key not in {'supplier','currency','amount_payable'}: headers[key] = value
                for total_field in ('subtotal_before_discount','discount_amount'):
                    if total_field in fixed: headers[total_field] = updated[total_field]
                    else: headers.pop(total_field,None)
                await state.update_data(supplier=updated.get('supplier'),currency=updated['currency'],
                    amount_payable=updated['amount_payable'],manual_total_fields=fixed,headers=headers)
            else:
                updated = apply_change(draft_analysis(data), data['index'], key, value)
                await state.update_data(items=updated['items'])
            await show_fields(message, state, data['scope'], data['index'])
            return
        if step == 'supplier':
            supplier = parse_value('supplier', text)
            if not supplier:
                raise ValueError('Укажите название поставщика.')
            await state.update_data(supplier=supplier)
            await prompt(message, state, 'product_name')
            return
        if step == 'amount_payable':
            current = draft_analysis(data)
            if text != '-':
                current = apply_change(current,-1,'amount_payable',parse_value('amount_payable',text))
            fixed = current.get('manual_total_fields') or []
            headers = dict(data['headers'])
            if 'discount_amount' not in fixed: headers.pop('discount_amount',None)
            await state.update_data(amount_payable=current['amount_payable'],manual_total_fields=fixed,headers=headers)
            await show_review(message, state)
            return
        if step == 'unit_price' and text == '-':
            value = item['unit_price_before_discount']
        elif step == 'line_total' and text == '-':
            value = rounded(decimal(item['quantity']) * decimal(item['unit_price']))
        else:
            value = parse_value(step, text)
        if step == 'unit_price' and value > item['unit_price_before_discount']:
            raise ValueError('Цена после скидки не может быть выше цены до скидки.')
        item[step] = value
        if step == 'line_total':
            item = manual_item(item)
            items = list(data['items'])
            if data.get('editing_item_index') is not None:
                items[data['editing_item_index']] = item
            else:
                items.append(item)
            updated = dict(data,items=items)
            draft_analysis(updated)  # Validate a fixed overall discount before accepting the item.
            await state.update_data(items=items, item={}, editing_item_index=None)
            await show_choice(message, state)
        else:
            steps = ['product_name', 'quantity', 'unit_price_before_discount', 'unit_price', 'line_total']
            await state.update_data(item=item)
            await prompt(message, state, steps[steps.index(step)+1])
    except ValueError as exc:
        await message.answer('⚠️ ' + esc(exc))
