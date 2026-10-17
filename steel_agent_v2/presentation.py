"""Plain Telegram text, with Python totals and visible original technical facts."""
from .engine import totals, differences, RequestEngine


def totals_text(items):
    sums, unknown = totals(items)
    lines = [('Известная часть количества:' if unknown else 'Количество:')]
    lines.extend(f'{amount} {unit}' for unit, amount in sums.items())
    if not sums:
        lines.append('Нет подтверждённых количеств.')
    if unknown:
        lines.append('Количество или единица не определены: ' + ', '.join(unknown))
    return '\n'.join(lines)


def render(request, view='normalized'):
    if request is None:
        return 'Активной заявки нет.'
    if view == 'original':
        return 'Исходный текст (без изменений):\n' + request.original_text
    if view == 'totals':
        return totals_text(request.items)
    lines = [f'Заявка {request.id}, версия {request.revision}.',
             'Статус: требует уточнения.' if RequestEngine.partial(request) else 'Структура и учёт источников проверены.',
             'Коммерческая группировка не подтверждает техническую взаимозаменяемость.']
    by_id = {i.id:i for i in request.items}
    originals = {i.id:i for i in request.originals}
    for n, group in enumerate(request.groups, 1):
        members = [by_id[key] for key in group.item_ids]
        lines.extend([f'\n{n}. {group.label} [{group.id}]', totals_text(members), 'Основание предложения GPT: ' + group.rationale])
        for item in members:
            lines.append(f'  [{item.id}] {item.label}: {item.quantity if item.quantity is not None else "?"} {item.unit or "?"}')
            for c in item.characteristics:
                lines.append(f'    {c.name}: {c.value if c.value is not None else "неизвестно"}; источник: {c.raw or "пользовательская правка"}')
            original = originals[item.id]
            lines.append(f'    Исходное количество: {original.quantity_text or "?"} {original.unit_text or "?"}; источник {item.source_id}')
        actual_differences = differences(members)
        if actual_differences:
            lines.append('Технические отличия / неизвестные значения:')
            lines.extend('  ' + d for d in actual_differences)
        if group.differences:
            lines.append('Замечания GPT: ' + '; '.join(group.differences))
    for record in request.records:
        if record.kind != 'product':
            lines.append(f'\nИсходный {record.kind} [{record.id}]: ' + request.original_text[record.start:record.end])
    if request.questions:
        lines.append('\nУточнения: ' + '; '.join(request.questions))
    return '\n'.join(lines)
