"""Application layer: source fidelity, field edits and persistent user context."""
from decimal import Decimal
from typing import Literal, Optional
from uuid import uuid4
from collections import Counter
import re
from pydantic import BaseModel, Field
from schemas import PurchaseRequest, RequestItem
from normalizer import normalize_request
from validator import validate_request, confirm_shared_attributes
from formatter import format_normalized_request, sort_items


class Edit(BaseModel):
    item_id: str
    field: Literal['grade', 'quantity', 'unit']
    value: Optional[str] = None
    restore: bool = False


class MessagePlan(BaseModel):
    intent: Literal['new', 'edit', 'clarify']
    edits: list[Edit] = Field(default_factory=list)
    question: Optional[str] = None


class ActiveItem(RequestItem):
    item_id: str
    original: RequestItem
    source_context: Optional[str] = None


class ActiveRequest(PurchaseRequest):
    request_id: str
    tonnage_decimals: Optional[Literal[2, 3]] = None
    source_text: str
    items: list[ActiveItem] = Field(default_factory=list)


GRADE_RE = re.compile(
    r'(?<![\w])(?:\d{3}-\d+-)?(?:[СC]\d{3}(?:-\d+)?|'
    r'(?:[ВB])?[Сс][Тт]\s*\.?\s*(?:\d{2}[Гг][0-9][Сс]|\d+(?:пс|сп|кп)?\d*)(?:-\d+)?|'
    r'\d{2}[Гг][0-9][Сс](?:-\d+)?|12[Хх]18[Нн]10[Тт](?:-\d+)?)(?![\w])',
    re.IGNORECASE)
QUANTITY_RE = re.compile(
    r'(?<![\w.,])([0-9]+(?:[.,][0-9]+)?)\s*'
    r'(пог\.?\s*м\.?|м\s*[²2]|тн|тонн(?:а|ы)?|кг|шт\.?|м\.?|т)(?![\w])',
    re.IGNORECASE)


def exact_source_attributes(item):
    """Recover full steel tokens and decimal quantities from literal source."""
    source = item.source_text or ''
    grades = [m.group() for m in GRADE_RE.finditer(source)]
    # A designation like strip contains a steel token inside its compound size.
    if len(grades) == 1 and item.product_group != 'strip':
        item.grade = re.sub(r'\s+', '', grades[0]).replace('ст.', 'Ст').replace('Ст.', 'Ст')
    elif len(grades) > 1:
        raise ValueError('В одной позиции несколько марок стали. Уточните выбор.')
    # Restore every explicitly written standard, rather than trusting omissions.
    standards = re.findall(r'(?:ГОСТ(?:\s+Р)?|ТУ|СТО\s+АСЧМ)\s+[0-9][0-9.\-]*(?:\s*-[0-9]+)?', source, re.I)
    if standards:
        item.gost_tu = ', '.join(re.sub(r'\s+', ' ', token.strip()) for token in standards)
    name = (source + ' ' + (item.product or '')).casefold()
    if item.product_group == 'sheet':
        if re.search(r'х/?к|холоднокатан', name):
            item.product = 'Лист Х/К'
        elif re.search(r'г/?к|горячекатан', name):
            item.product = 'Лист Г/К'
    elif item.product_group == 'round_pipe':
        if re.search(r'э/?с|электросвар', name):
            item.product = 'Труба э/с'
        elif 'вгп' in name or 'водогаз' in name:
            item.product = 'Труба ВГП'
        elif 'бесшов' in name:
            item.product = 'Труба бесшовная'
    quantities = [m for m in QUANTITY_RE.finditer(source)
                  if not re.search(r'(?:длина|раскрой)\s*[:=]?\s*$', source[max(0,m.start()-20):m.start()], re.I)]
    reverse_quantity = re.search(r'(?<=[|;\t])\s*(пог\.?\s*м\.?|м[²2]|тн|кг|шт\.?|м|т)\s*[|;\t]\s*\d+(?:[.,]\d+)?(?![\w.,])', source, re.I)
    if reverse_quantity:
        quantities = []
    if not quantities:
        # Excel often places the unit in a column before the quantity.
        reverse = re.search(r'(?<![\w])(?P<unit>пог\.?\s*м\.?|м[²2]|тн|кг|шт\.?|м|т)\s*[|;\t]\s*(?P<amount>\d+(?:[.,]\d+)?)(?![\w.,])', source, re.I)
        if reverse:
            item.quantity = Decimal(reverse['amount'].replace(',', '.'))
            raw = reverse['unit'].lower().replace(' ', '')
            item.unit = {'м2':'м²','тн':'т','шт.':'шт'}.get(raw, 'пог. м' if raw.startswith('пог') else raw)
            item.source_unit = reverse['unit']
        else:
            item.quantity = None
    if quantities:
        last = quantities[-1]
        item.quantity = Decimal(last[1].replace(',', '.'))
        raw_unit = last[2].lower().replace(' ', '')
        if raw_unit.startswith('пог'):
            item.unit = 'пог. м'
        elif raw_unit in {'м²', 'м2'}:
            item.unit = 'м²'
        elif raw_unit.startswith('шт'):
            item.unit = 'шт'
        elif raw_unit.startswith('т'):
            item.unit = 'т'
        else:
            item.unit = raw_unit.rstrip('.')
        item.source_unit = last[2]
    if item.quantity is not None and (not item.quantity.is_finite() or item.quantity <= 0):
        raise ValueError('Количество должно быть конечным положительным числом.')


def prepare_sheet_heading(text):
    """Expand only an explicit single sheet heading with unambiguous rows.

    Numeric unit conversion is deterministic; keep original heading and row
    separately for provenance. Unrecognized/mixed blocks use normal AI parsing.
    """
    lines = [line.strip().rstrip('\\').strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return text, []
    heading = lines[0]
    if not re.match(r'^лист\b', heading, re.I) or not heading.endswith(':'):
        return text, []
    cut = re.search(r'раскрой\s*(\d+(?:[.,]\d+)?)\s*[xхХX×*]\s*(\d+(?:[.,]\d+)?)\s*(мм|м)\s*:', heading, re.I)
    grades = list(GRADE_RE.finditer(heading))
    standards = re.findall(r'(?:ГОСТ(?:\s+Р)?|ТУ)\s+[0-9][0-9.\-]*', heading, re.I)
    if not cut or len(grades) != 1 or not standards:
        return text, []
    factor = Decimal(1000) if cut[3].casefold() == 'м' else Decimal(1)
    dimensions = [Decimal(cut[n].replace(',', '.')) * factor for n in [1, 2]]
    if any(value <= 0 for value in dimensions):
        return text, []
    if re.search(r'г/?к|горячекатан', heading, re.I):
        product = 'Лист Г/К'
    elif re.search(r'х/?к|холоднокатан', heading, re.I):
        product = 'Лист Х/К'
    else:
        product = 'Лист'
    def number(value):
        result = format(value, 'f')
        return (result.rstrip('0').rstrip('.') if '.' in result else result).replace('.', ',')
    records = []
    for row in lines[1:]:
        match = re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*[-—–]\s*(\d+(?:[.,]\d+)?)\s*(тн|т|кг|шт\.?)', row, re.I)
        if not match:
            return text, []
        size = '×'.join([match[1]] + [number(value) for value in dimensions])
        prepared = f'{product} {size} {grades[0][0]} {", ".join(standards)} — {match[2]} {match[3]}'
        records.append((prepared, row, heading))
    return '\n'.join(record[0] for record in records), records


class TelegramService:
    def __init__(self, ai, repository):
        self.ai, self.repository = ai, repository

    def process(self, user_id, text):
        current = self.repository.load(user_id)
        parse_text, heading_records = prepare_sheet_heading(text)
        if current is not None and not heading_records:
            # Formatting is a presentation setting, never a quantity edit.
            wording = text.casefold()
            if re.search(r'тоннаж|тонн|количеств', wording) and re.search(r'формат|виду|знак|отображ|округл', wording):
                places = re.search(r'(три|тр[её]х|два|двух|двумя|[23])\s*(?:знак|цифр)', wording)
                example = re.search(r'0[,.](0{2,3}|100)\s*т', wording)
                exact = re.search(r'без округления|исходн\w* формат|точн\w* формат', wording)
                if places or example or exact:
                    changed = current.model_copy(deep=True)
                    if exact:
                        changed.tonnage_decimals = None
                    elif places:
                        changed.tonnage_decimals = 2 if places[1] in {'два', 'двух', 'двумя', '2'} else 3
                    else:
                        changed.tonnage_decimals = len(example[1])
                    messages = self._response(changed, [])
                    self.repository.save(user_id, changed)
                    return messages
            plan = self.ai.interpret_message(text, current)
            if plan.intent == 'clarify':
                return [plan.question or 'Уточните, какие позиции и поля изменить.']
            if plan.intent == 'edit':
                return self._edit(user_id, current, plan)
        elif re.search(r'\b(замени|заменить|измени|верни|убери)\b', text, re.I):
            return ['Сначала отправьте заявку, затем команду изменения.']
        draft = self.ai.parse_request(parse_text)
        if not draft.items:
            return ['Отправьте перечень металлопроката с количеством и единицами.']
        # Every physical row containing a product must be represented. Quoted Excel
        # continuations remain within the item's literal source fragment.
        product = re.compile(r'\b(балк\w*|двутавр\w*|лист\w*|труб\w*|угол\w*|'
                             r'швеллер\w*|арматур\w*|круг\w*|полос\w*|'
                             r'проволок\w*|лент\w*|сетк\w*|прут\w*|'
                             r'отвод\w*|фланец\w*|муфт\w*|тройник\w*)\b', re.I)
        source_rows = Counter(
            ''.join(line.casefold().split()).strip('"')
            for line in parse_text.splitlines()
            if product.search(line) and re.search(r'\d', line))
        for compact, count in source_rows.items():
            represented = sum(compact in ''.join((i.source_text or '').casefold().split()).strip('"') for i in draft.items)
            if represented < count:
                raise ValueError('Не все исходные строки распознаны. Отправьте заявку ещё раз или разделите её на части.')
        for item in draft.items:
            physical = [line for line in (item.source_text or '').splitlines() if product.search(line) and QUANTITY_RE.search(line)]
            if len(physical) > 1:
                raise ValueError('Несколько товарных строк объединены в одну позицию. Повторите заявку.')
        for item in draft.items:
            exact_source_attributes(item)
        normalize_request(draft)
        confirmed = confirm_shared_attributes(parse_text, draft)
        draft.confirmed_shared_attributes = confirmed
        for evidence in confirmed:
            for position in evidence.applies_to:
                if draft.items[position-1].grade is None:
                    draft.items[position-1].grade = evidence.value
        original = [i.model_copy(deep=True) for i in draft.items]
        checked = validate_request(draft, raw_request=parse_text)
        # Never publish a silently erased source attribute.
        for before, after in zip(original, checked.items):
            if before.grade != after.grade or before.gost_tu != after.gost_tu:
                raise ValueError('Не удалось подтвердить марку или ГОСТ по исходному тексту. Уточните эту позицию.')
        active = ActiveRequest(**{k:v for k,v in checked.model_dump().items() if k != 'items'},
            request_id=uuid4().hex, source_text=text,
            items=[ActiveItem(**i.model_dump(), item_id=uuid4().hex, original=i.model_copy(deep=True)) for i in checked.items])
        if heading_records:
            from ai import _compact_source_text
            unused = list(heading_records)
            for item in active.items:
                compact = _compact_source_text(item.source_text or '')[0]
                record = next((row for row in unused if _compact_source_text(row[0])[0] == compact), None)
                if record is None:
                    raise ValueError('Не удалось сопоставить строку с общей шапкой заявки.')
                unused.remove(record)
                item.source_text = record[1]
                item.original.source_text = record[1]
                item.source_context = record[2]
            if unused:
                raise ValueError('Не все позиции из списка под общей шапкой распознаны.')
        active.items = sort_items(active.items)
        messages = self._response(active, checked.clarification_questions)
        self.repository.save(user_id, active)
        return messages

    def _edit(self, user_id, current, plan):
        if not plan.edits:
            return [plan.question or 'Укажите полную марку и позиции, которые нужно изменить.']
        changed = current.model_copy(deep=True)
        by_id = {i.item_id:i for i in changed.items}
        seen = set()
        for edit in plan.edits:
            if edit.item_id not in by_id or (edit.item_id, edit.field) in seen:
                raise ValueError('Неоднозначная область изменения. Уточните номера позиций.')
            seen.add((edit.item_id, edit.field))
            item = by_id[edit.item_id]
            value = getattr(item.original, edit.field) if edit.restore else edit.value
            if edit.field == 'quantity':
                try:
                    value = Decimal(str(value).replace(',', '.'))
                except Exception:
                    raise ValueError('Укажите положительное количество.') from None
                if not value.is_finite() or value <= 0:
                    raise ValueError('Укажите положительное количество.')
            elif edit.field == 'unit' and value not in {'т','кг','шт','шт.','м','пог. м','м²'}:
                raise ValueError('Укажите единицу: т, кг, шт., м, пог. м или м².')
            elif edit.field == 'grade' and not edit.restore and (not value or '\n' in value or len(value) > 100):
                raise ValueError('Укажите полную марку стали.')
            if edit.field == 'grade' and item.product_group == 'strip' and item.grade and item.grade in (item.size_designation or ''):
                if value is None:
                    raise ValueError('Уточните полное обозначение ленты для восстановления марки.')
                item.size_designation = item.size_designation.replace(item.grade, value, 1)
            setattr(item, edit.field, value)
        questions = [f'Уточните количество и единицу позиции {n}.' for n,i in enumerate(changed.items,1) if i.quantity is None or not i.unit]
        messages = self._response(changed, questions)
        self.repository.save(user_id, changed)
        return messages

    @staticmethod
    def _response(request, questions):
        messages = split_positions(format_normalized_request(request))
        if questions:
            messages += split_positions('\n'.join(questions))
        return messages


def split_positions(text, limit=4000):
    messages, lines, size = [], [], 0
    for line in text.splitlines():
        length = len(line.encode('utf-16-le')) // 2
        if length > limit:
            raise ValueError('Одна позиция превышает лимит Telegram. Сократите её описание.')
        if lines and size + length + 1 > limit:
            messages.append('\n'.join(lines))
            lines, size = [], 0
        lines.append(line)
        size += length + (1 if len(lines) > 1 else 0)
    if lines:
        messages.append('\n'.join(lines))
    return messages
