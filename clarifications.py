"""Small persisted, typed clarification states; no model call for short answers."""
from decimal import Decimal, InvalidOperation
from typing import Literal
import re
from pydantic import BaseModel, Field


UNITS={'т':'т','тн':'т','тоннах':'т','тонны':'т','кг':'кг','шт':'шт','шт.':'шт','штуках':'шт','штуке':'шт','штуку':'шт','штук':'шт','штуки':'шт',
       'м':'м','метрах':'м','м²':'м²','м2':'м²','пог. м':'пог. м','пог.м':'пог. м'}

_BULK_NUMBER = r'(?:\d+(?:[.,]\d+)?|один|одна|одну|одной|одно)'
_BULK_UNIT = r'(?:пог\.?\s*м\.?|м\s*[²2]|тн|тонн(?:а|ы)?|кг|шт\.?|штук(?:а|и|у|е)?|м\.?|т)'


def parse_bulk_quantity(text, request):
    """Parse an explicit all-position or source-position quantity instruction.

    The caller applies the parsed values atomically and rejects incompatible
    existing units. No dimensions or quantities are inferred from item text.
    """
    amount = rf'(?P<amount>{_BULK_NUMBER})\s*(?P<unit>{_BULK_UNIT})'
    match = re.fullmatch(rf'\s*(?:(?P<all>все(?:\s+позиции)?|каждого|каждую|каждой)\s+по\s+|'
                         rf'(?P<replace>замени(?:те)?\s+количество\s+у\s+всех\s+позиций\s+на\s+)|'
                         rf'поз(?:иция|\.)?\s*(?P<start>\d+)\s*(?:[-–—]\s*(?P<end>\d+))?\s+по\s+)'
                         rf'{amount}\s*[.!]?\s*', text, re.I)
    if not match:
        return None
    words = {'один': '1', 'одна': '1', 'одну': '1', 'одной': '1', 'одно': '1'}
    try:
        quantity = Decimal(words.get(match['amount'].casefold(), match['amount'].replace(',', '.')))
    except (InvalidOperation, AttributeError):
        return None
    if not quantity.is_finite() or quantity <= 0:
        return None
    unit = find_unit(match['unit'])
    if not unit:
        return None
    if match['all'] or match['replace']:
        targets = list(request.items)
    else:
        first = int(match['start'])
        last = int(match['end'] or first)
        if first < 1 or last < first:
            return None
        targets = [item for index, item in enumerate(request.items, 1)
                   if (getattr(item, 'source_position', None) or index) in range(first, last + 1)]
        if len(targets) != last - first + 1:
            return None
    if not targets:
        return None
    if any(item.unit and item.unit != unit for item in targets):
        return []
    # Refuse a bulk answer that would silently replace an existing quantity.
    if not match['replace'] and any(
        item.quantity is not None and (item.quantity != quantity or not item.unit)
        for item in targets
    ):
        return []
    return [(item.item_id if hasattr(item, 'item_id') else id(item), quantity, unit) for item in targets]


def parse_dimension_answers(text, request):
    """Parse explicit geometry fields in one or more numbered clarification answers."""
    position_re = re.compile(r'(?<!\w)(?:исходной\s+)?поз(?:иция|\.)\s*(\d+)', re.I)
    matches = list(position_re.finditer(text))
    missing = {}
    for entry in request.missing_fields:
        match = re.fullmatch(r'items\[(\d+)\]\.([a-z_]+)', entry)
        if match:
            index = int(match[1])
            item = request.items[index] if index < len(request.items) else next(
                (candidate for candidate in request.items
                 if getattr(candidate, 'source_position', None) == index + 1), None)
            if item is not None:
                position = getattr(item, 'source_position', None) or index + 1
                missing.setdefault(position, set()).add(match[2])
    if not missing:
        return {}
    blocks = []
    if matches:
        for n, match in enumerate(matches):
            blocks.append((int(match[1]), text[match.end():matches[n + 1].start() if n + 1 < len(matches) else len(text)]))
    elif len(missing) == 1:
        blocks = [(next(iter(missing)), text)]
    else:
        return {}
    field_patterns = (
        ('wall_thickness_mm', r'толщин\w*\s+стенк\w*'),
        ('width_mm', r'ширин\w*'), ('height_mm', r'высот\w*'),
        ('length_mm', r'длин\w*'), ('diameter_mm', r'диаметр\w*'),
        ('side_a_mm', r'сторон\w*\s*a'), ('side_b_mm', r'сторон\w*\s*b'),
        ('thickness_mm', r'толщин\w*'),
    )
    values = {}
    for position, block in blocks:
        target = next((item for index, item in enumerate(request.items, 1)
                       if (getattr(item, 'source_position', None) or index) == position), None)
        if target is None or position not in missing:
            continue
        for field, label in field_patterns:
            match = re.search(rf'(?<![\w.+-]){label}\s*[:=]?\s*(\d+(?:[.,]\d+)?)(?![\w.+-])\s*(?:мм)?', block, re.I)
            if match and field in missing[position]:
                value = Decimal(match[1].replace(',', '.'))
                if value.is_finite() and value > 0:
                    values.setdefault(position, {})[field] = value
    return values


class ClarificationOption(BaseModel):
    label: str
    values: dict[str,str] = Field(default_factory=dict)


class PendingClarification(BaseModel):
    request_id: str
    item_id: str
    field: Literal['quantity','unit']
    kind: Literal['number','unit','confirmation','choice']
    question: str
    candidate: dict[str,str] = Field(default_factory=dict)
    options: list[ClarificationOption] = Field(default_factory=list)


def find_unit(text):
    compact=text.casefold().strip()
    # Match longest units first, to distinguish metres from square metres.
    for word in sorted(UNITS,key=len,reverse=True):
        if re.search(r'(?<!\w)'+re.escape(word)+r'(?!\w)',compact):
            return UNITS[word]
    return None


def next_question(request):
    for index,item in enumerate(request.items,1):
        source_position = getattr(item, 'source_position', None) or index
        if not item.unit:
            choices=[ClarificationOption(label=unit,values={'unit':unit}) for unit in ['т','кг','шт','м','пог. м','м²']]
            return PendingClarification(request_id=request.request_id,item_id=item.item_id,field='unit',kind='choice',
                question=f'Укажите единицу исходной позиции {source_position}: 1 — т; 2 — кг; 3 — шт.; 4 — м; 5 — пог. м; 6 — м². Можно написать единицу словами.',options=choices)
        if item.quantity is None:
            return PendingClarification(request_id=request.request_id,item_id=item.item_id,field='quantity',kind='number',
                question=f'Укажите количество исходной позиции {source_position} в {item.unit}.')
    return None


def question_state(question, request):
    """Only associate a model's proposal with an unambiguous existing position."""
    if not question:
        return None
    match=re.search(r'позиц\w*\s*(\d+)',question,re.I)
    if match and re.search(r'исходн\w*\s+позиц', question, re.I):
        index = next((i for i, item in enumerate(request.items)
                      if (getattr(item, 'source_position', None) or i + 1) == int(match[1])), -1)
    elif match:
        from formatter import ordered_consolidation
        groups = ordered_consolidation(request.items)
        display = int(match[1]) - 1
        index = groups[display].source_indices[0] - 1 if (0 <= display < len(groups) and len(groups[display].source_indices) == 1) else -1
    else:
        index = 0 if len(request.items)==1 else -1
    if index < 0 or index >= len(request.items):
        return None
    item=request.items[index]
    number=re.search(r'(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(шт\.?|тн|т|кг|м²|пог\.\s*м|м)(?!\w)',question,re.I)
    if re.search(r'верно|подтверд',question,re.I) and number:
        return PendingClarification(request_id=request.request_id,item_id=item.item_id,field='quantity',kind='confirmation',
            question=question,candidate={'quantity':number[1].replace(',','.'),'unit':find_unit(number[2])})
    choices=[]
    for line in question.splitlines():
        option=re.match(r'\s*\d+[).]\s*(.+)',line)
        if option:
            quantity=re.search(r'\d+(?:[.,]\d+)?',option[1])
            unit=find_unit(option[1])
            values={}
            if quantity and unit:
                values['quantity']=quantity[0].replace(',','.')
            if unit:
                values['unit']=unit
            if values:
                choices.append(ClarificationOption(label=option[1],values=values))
    if len(choices)>=2:
        return PendingClarification(request_id=request.request_id,item_id=item.item_id,field='quantity' if 'quantity' in choices[0].values else 'unit',
            kind='choice',question=question,options=choices)
    if re.search(r'количеств|сколько|тоннаж',question,re.I):
        return PendingClarification(request_id=request.request_id,item_id=item.item_id,field='quantity',kind='number',question=question)
    if re.search(r'единиц|в каких',question,re.I):
        return PendingClarification(request_id=request.request_id,item_id=item.item_id,field='unit',kind='unit',question=question)
    return None


def answer_values(pending, text):
    """Return (values, retry-question); never infer quantity from product dimensions."""
    answer=text.casefold().strip().strip('.!?')
    if pending.kind=='confirmation':
        if answer in {'да','верно','подтверждаю','согласен'}:
            return pending.candidate,None
        if answer in {'нет','неверно'}:
            return {},'Укажите правильное количество и единицу.'
        return None,pending.question
    if pending.kind=='choice' and re.fullmatch(r'\d+',answer):
        choice=int(answer)-1
        if 0<=choice<len(pending.options):
            return pending.options[choice].values,None
        return None,pending.question
    unit=find_unit(text)
    if pending.field=='unit':
        return ({'unit':unit},None) if unit else (None,pending.question)
    # A bare number or quantity with unit is valid; a cross-section is not a count.
    match=re.fullmatch(r'\s*(\d+(?:[.,]\d+)?)\s*(?:тн|т|кг|шт\.?|штук|штуки|м²|м2|пог\.\s*м|м)?\s*',text,re.I)
    if match:
        try:
            value=Decimal(match[1].replace(',','.'))
            if value.is_finite() and value>0:
                values={'quantity':str(value)}
                if unit:
                    values['unit']=unit
                return values,None
        except InvalidOperation:
            pass
    return None,pending.question
