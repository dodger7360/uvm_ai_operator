"""Human-facing views of immutable structured facts; no product-specific rules."""
from decimal import Decimal
import re
from .engine import totals, differences, RequestEngine


def number(value):
    text=format(Decimal(value), 'f')
    if '.' in text:text=text.rstrip('0').rstrip('.')
    return text.replace('.', ',')


def totals_text(items, positions=None):
    sums, unknown=totals(items)
    lines=['Известная часть количества:' if unknown else 'Количество:']
    lines.extend(f'{number(amount)} {unit}' for unit,amount in sums.items())
    if not sums:lines.append('Нет подтверждённых количеств.')
    if unknown:
        numbers=positions or {item.id:n for n,item in enumerate(items,1)}
        unknown_numbers=[str(numbers[item.id]) for item in items if item.id in unknown]
        lines.append('Количество или единица не определены в позициях: '+', '.join(unknown_numbers))
    return '\n'.join(lines)


def _cities(request):
    values=[[c.value for c in item.characteristics if c.name.casefold() in {'city','город'} and c.value]
            for item in request.items]
    flat={v for row in values for v in row}
    return next(iter(flat)) if len(flat)==1 and all(values) else None


def _item_text(item, original, city):
    label=item.label
    facts=[]
    for characteristic in item.characteristics:
        if city and characteristic.name.casefold() in {'city','город'}:continue
        value=characteristic.value
        if value is None:
            facts.append(characteristic.name+': не уточнено')
        elif not re.search(r'(?<!\w)'+re.escape(value)+r'(?!\w)',label,re.I) and value not in facts:
            facts.append(value)
    text=label+(' '+ '; '.join(facts) if facts else '')
    amount=number(item.quantity) if item.quantity is not None else 'количество не уточнено'
    text+=' — '+amount+(' '+item.unit if item.unit else ' (единица не уточнена)')
    if original.unit and original.unit!=item.unit:
        original_amount=original.quantity_text or (number(original.quantity) if original.quantity is not None else 'количество не уточнено')
        text+=' (исходно: '+original_amount+' '+original.unit+')'
    return text


def _humanize(text, request):
    mapping={request.id:'текущая заявка'}
    mapping.update({item.id:f'позиция {n}' for n,item in enumerate(request.items,1)})
    mapping.update({group.id:f'группа {n}' for n,group in enumerate(request.groups,1)})
    mapping.update({record.id:f'фрагмент {n}' for n,record in enumerate(request.records,1)})
    for key,value in sorted(mapping.items(),key=lambda pair:-len(pair[0])):
        text=re.sub(r'(?<!\w)'+re.escape(key)+r'(?!\w)',lambda _:value,text)
    return text


def render(request, view='normalized'):
    if request is None:return 'Активной заявки нет.'
    if view=='original':return 'Исходный текст (без изменений):\n'+request.original_text
    if view=='totals':return totals_text(request.items)
    forward=view=='forward'
    detailed=view=='explanation'
    partial=RequestEngine.partial(request)
    lines=[] if forward else [f'Заявка, версия {request.revision}.']
    if partial:lines.append('Заявка требует уточнения.')
    city=_cities(request)
    by_id={item.id:item for item in request.items}
    originals={item.id:item for item in request.originals}
    positions={item.id:n for n,item in enumerate(request.items,1)}
    for n,group in enumerate(request.groups,1):
        members=[by_id[key] for key in group.item_ids]
        if len(members)==1:
            item=members[0]
            prefix='' if forward and len(request.items)==1 else f'{positions[item.id]}. '
            lines.append(prefix+_item_text(item,originals[item.id],city))
        else:
            lines.append(f'Группа {n}: '+group.label)
            for item in members:
                lines.append(f'  {positions[item.id]}. '+_item_text(item,originals[item.id],city))
            lines.append('  '+totals_text(members,positions).replace('\n',' '))
            if differences(members) or group.differences:
                lines.append('  В группе есть технические отличия; взаимозаменяемость не подтверждена.')
        if detailed:
            lines.append('Обоснование группировки: '+_humanize(group.rationale,request))
            lines.extend(_humanize(value,request) for value in differences(members)+group.differences)
    if city:lines.append('\nГород: '+city)
    for record in request.records:
        if record.kind=='unresolved':
            lines.append('Не обработано: '+request.original_text[record.start:record.end])
        elif detailed and record.kind!='product':
            lines.append('Дополнительный исходный текст: '+request.original_text[record.start:record.end])
    if request.questions:lines.append('\nУточните: '+'; '.join(_humanize(q,request) for q in request.questions))
    elif not forward and not partial:lines.append('Не требуется уточнений.')
    return '\n'.join(lines)
