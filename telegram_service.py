"""Application layer: source fidelity, field edits and persistent user context."""
from decimal import Decimal
from typing import Literal, Optional
from uuid import uuid4
import re
import os
from diagnostics import operation, phase, emit, CURRENT, PipelineError, SemanticExtractionError
from clarifications import PendingClarification, next_question, question_state, answer_values
from request_operations import (Edit, RequestOperation, PendingOperation, request_fingerprint,
                                unsupported_operation, UNSUPPORTED_MESSAGE)
from pydantic import BaseModel, Field
from schemas import PurchaseRequest, RequestItem
from normalization_policy import GradeRule
from normalizer import normalize_request, UNIT_ALIASES
from validator import validate_request, confirm_shared_attributes
from formatter import format_normalized_request, sort_items, ordered_consolidation
from quantity import QUANTITY_RE
from source_fidelity import PRODUCT_ROW, product_row_key, validate_source_coverage


class MessagePlan(BaseModel):
    intent: Literal['new', 'edit', 'clarify', 'operation']
    edits: list[Edit] = Field(default_factory=list)
    question: Optional[str] = None
    operation: Optional[RequestOperation] = None


class ActiveItem(RequestItem):
    item_id: str
    original: RequestItem
    source_context: Optional[str] = None
    source_position: Optional[int] = None


class ActiveRequest(PurchaseRequest):
    request_id: str
    dialog_state: Literal['active', 'choose_request', 'awaiting_new'] = 'active'
    tonnage_decimals: Optional[Literal[2, 3]] = None
    pending_clarification: Optional[PendingClarification] = None
    pending_operation: Optional[PendingOperation] = None
    clarification_cancelled: bool = False
    clarification_answers: list[str] = Field(default_factory=list)
    initial_extraction: Optional[PurchaseRequest] = None
    source_text: str
    normalization_rules: list[GradeRule] = Field(default_factory=list)
    items: list[ActiveItem] = Field(default_factory=list)


GRADE_RE = re.compile(
    r'(?<![\w])(?:\d{3}-\d+-)?(?:[СC]\s*\d{3}(?:-\d+)?|'
    r'(?:[ВB])?[Сс][Тт]\s*\.?\s*(?:\d{2}[Гг][0-9][Сс]|\d+(?:пс|сп|кп)?\d*)(?:-\d+)?|'
    r'\d{2}[Гг][0-9][Сс](?:-\d+)?|12[Хх]18[Нн]10[Тт](?:-\d+)?)(?![\w])',
    re.IGNORECASE)



def exact_source_attributes(item):
    """Recover full steel tokens and decimal quantities from literal source."""
    source = item.source_text or ''
    if item.quantity is not None and item.quantity < 0:
        raise PipelineError('invalid_quantity', 'Количество должно быть конечным положительным числом.')
    grades = [m.group() for m in GRADE_RE.finditer(source)]
    # A designation like strip contains a steel token inside its compound size.
    if len(grades) == 1 and item.product_group != 'strip':
        item.grade = re.sub(r'\s+', '', grades[0]).replace('ст.', 'Ст').replace('Ст.', 'Ст')
    elif len(grades) > 1:
        matches=list(GRADE_RE.finditer(source))
        if all(re.search(r'\bили\b',source[a.end():b.start()],re.I) for a,b in zip(matches,matches[1:])):
            from normalizer import _normalize_grade
            normalized=[_normalize_grade(match[0]) for match in matches]
            item.grade=normalized[0]+' (или '+' или '.join(normalized[1:])+')'
        else:
            raise PipelineError('ambiguous_grades','Укажите, являются ли перечисленные марки разрешёнными альтернативами («или»).')
    # Restore every explicitly written standard, rather than trusting omissions.
    standards = re.findall(r'(?<!\w)(?:ГОСТ(?:\s+Р)?|ТУ|СТО\s+АСЧМ)\s*[0-9][0-9.\-]*(?:\s*-[0-9]+)?', source, re.I)
    if standards:
        canonical = []
        for token in standards:
            token = re.sub(r'\s+', ' ', token.strip().rstrip('-'))
            canonical.append(re.sub(r'^(ГОСТ(?:\s+Р)?|ТУ|СТО\s+АСЧМ)\s*',
                                    lambda match: match[1].upper()+' ', token, flags=re.I))
        item.gost_tu = ', '.join(canonical)
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
        elif re.search(r'ГОСТ\s*10704(?:-91)?', source, re.I):
            item.product = 'Труба э/с'
    quantities = [m for m in QUANTITY_RE.finditer(source)
                  if not re.search(r'[×хx*]\s*$',source[max(0,m.start()-20):m.start()],re.I)
                  and not re.search(r'(?:длина|раскрой)\s*[:=]?\s*$', source[max(0,m.start()-20):m.start()], re.I)]
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
        raise PipelineError('invalid_quantity','Количество должно быть конечным положительным числом.')


def prepare_sheet_heading(text):
    """Expand scoped sheet blocks; retain each literal row and its own heading."""
    lines = [line.strip().rstrip('\\').strip() for line in text.splitlines() if line.strip()]
    prepared, records = [], []
    index = 0
    while index < len(lines):
        heading = lines[index]
        cut = re.search(r'раскрой\s*(\d+(?:[.,]\d+)?)\s*[xхХX×*]\s*(\d+(?:[.,]\d+)?)\s*(мм|м)\s*:', heading, re.I)
        grades = list(GRADE_RE.finditer(heading))
        standards = re.findall(r'(?:ГОСТ(?:\s+Р)?|ТУ)\s*[0-9][0-9.\-]*', heading, re.I)
        if not (re.match(r'^лист\b', heading, re.I) and heading.endswith(':') and cut and len(grades) == 1 and standards):
            prepared.append(heading)
            index += 1
            continue
        factor = Decimal(1000) if cut[3].casefold() == 'м' else Decimal(1)
        dimensions = [Decimal(cut[n].replace(',', '.')) * factor for n in (1, 2)]
        if any(value <= 0 for value in dimensions):
            prepared.append(heading)
            index += 1
            continue
        product = 'Лист Г/К' if re.search(r'г/?к|горячекатан', heading, re.I) else 'Лист Х/К' if re.search(r'х/?к|холоднокатан', heading, re.I) else 'Лист'
        from normalizer import _normalize_grade
        grade = _normalize_grade(grades[0][0])
        def number(value):
            result = format(value, 'f')
            return (result.rstrip('0').rstrip('.') if '.' in result else result).replace('.', ',')
        block = []
        cursor = index + 1
        while cursor < len(lines):
            row = lines[cursor]
            match = re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*[-—–]\s*(\d+(?:[.,]\d+)?)\s*(тн|т|кг|шт\.?)', row, re.I)
            if not match:
                if re.match(r'^\d', row):
                    raise PipelineError('sheet_row_format', 'Не удалось однозначно разобрать строку под шапкой листов. Укажите толщину, количество и единицу.')
                break
            size = '×'.join([match[1]] + [number(value) for value in dimensions])
            expanded = f'{product} {size} {grade} {", ".join(standards)} — {match[2]} {match[3]}'
            block.append((expanded, row, heading))
            cursor += 1
        if not block:
            prepared.append(heading)
            index += 1
            continue
        prepared.extend(record[0] for record in block)
        records.extend(block)
        index = cursor
    return ('\n'.join(prepared), records) if records else (text, [])


def local_edit_plan(text, current, approved_rules=()):
    """Explicit source edits; a multi-source display row is never a quantity target."""
    from normalizer import _normalize_grade
    ordinals = {'первой': 1, 'второй': 2, 'третьей': 3, 'четвёртой': 4, 'четвертой': 4,
                'пятой': 5, 'шестой': 6, 'седьмой': 7, 'восьмой': 8, 'девятой': 9, 'десятой': 10}
    text = re.sub(r'\b(' + '|'.join(ordinals) + r')\s+(исходной\s+)?позиции\b',
                  lambda m: (m[2] or '') + 'позиции ' + str(ordinals[m[1].casefold()]), text, flags=re.I)
    match = re.fullmatch(r'\s*(?:в\s+)?(исходной\s+)?позиции\s+(\d+)\s+(?:измени|замени)\s+(марку|количество)\s+на\s+(.+?)\s*', text, re.I)
    restore = re.fullmatch(r'\s*(?:верни|восстанови)\s+исходную\s+марку\s+(исходной\s+)?позиции\s+(\d+)\s*', text, re.I)
    selection = match or restore
    if selection:
        number = int(selection[2])
        if selection[1]:
            candidates = [item for index, item in enumerate(current.items, 1)
                          if (item.source_position or index) == number]
        else:
            groups = ordered_consolidation(current.items, approved_rules=approved_rules)
            if not 1 <= number <= len(groups):
                return MessagePlan(intent='clarify', question='Укажите существующую исходную позицию.')
            group = groups[number - 1]
            candidates = [current.items[index - 1] for index in group.source_indices]
            if len(candidates) > 1:
                positions = ', '.join(map(str, group.source_positions))
                return MessagePlan(intent='clarify', question=(
                    f'Итоговая строка {number} включает исходные строки {positions}. '
                    'Укажите нужную командой «В исходной позиции N измени поле на значение».'))
        if len(candidates) != 1:
            return MessagePlan(intent='clarify', question='Укажите существующую исходную позицию.')
        item_id = candidates[0].item_id
        if restore:
            return MessagePlan(intent='edit', edits=[Edit(item_id=item_id, field='grade', restore=True)])
        if match[3].casefold() == 'марку' and GRADE_RE.fullmatch(match[4]):
            return MessagePlan(intent='edit', edits=[Edit(item_id=item_id, field='grade', value=_normalize_grade(match[4]))])
        quantity = QUANTITY_RE.fullmatch(match[4])
        if match[3].casefold() == 'количество' and quantity:
            from clarifications import find_unit
            unit = find_unit(quantity[2])
            if unit:
                return MessagePlan(intent='edit', edits=[Edit(item_id=item_id,field='quantity',value=quantity[1]), Edit(item_id=item_id,field='unit',value=unit)])
        return None
    approved_grade = re.fullmatch(r'\s*(?:замени|поменяй)\s+(.+?)\s+на\s+(.+?)\s+там,?\s+где\s+(?:это\s+)?разрешено\s*', text, re.I)
    if approved_grade and all(GRADE_RE.fullmatch(approved_grade[n]) for n in (1, 2)):
        from normalization_policy import project_item
        before, after = (_normalize_grade(approved_grade[n]) for n in (1, 2))
        edits = []
        for item in current.items:
            projected, applied = project_item(item, approved_rules, original=item.original)
            if item.grade == before and projected.grade == after and applied:
                edits.append(Edit(item_id=item.item_id, field='grade', value=after))
        if edits:
            return MessagePlan(intent='edit', edits=edits)
        return MessagePlan(intent='clarify', question='Нет применимого утверждённого правила для этой замены. Уточните допустимые исходные позиции.')
    scoped_grade = re.fullmatch(r'\s*во?\s+всех\s+(уголках|трубах|листах|швеллерах|балках)\s+(?:замени|поменяй)\s+(.+?)\s+на\s+(.+?)\s*', text, re.I)
    if scoped_grade and all(GRADE_RE.fullmatch(scoped_grade[n]) for n in (2, 3)):
        groups = {'уголках': {'angle'}, 'трубах': {'round_pipe', 'profile_pipe'},
                  'листах': {'sheet'}, 'швеллерах': {'channel'}, 'балках': {'beam', 'i_beam'}}
        before, after = (_normalize_grade(scoped_grade[n]) for n in (2, 3))
        edits = [Edit(item_id=item.item_id, field='grade', value=after) for item in current.items
                 if item.product_group in groups[scoped_grade[1].casefold()] and _normalize_grade(item.grade) == before]
        if edits:
            return MessagePlan(intent='edit', edits=edits)
        return MessagePlan(intent='clarify', question='Нет позиций указанного вида и марки. Уточните область изменения.')
    global_grade = re.fullmatch(r'\s*везде\s+(.+?)\s+(?:замени|заменить)\s+на\s+(.+?)\s*', text, re.I)
    if global_grade and all(GRADE_RE.fullmatch(global_grade[n]) for n in (1, 2)):
        original, replacement = (_normalize_grade(global_grade[n]) for n in (1, 2))
        edits = [Edit(item_id=item.item_id,field='grade',value=replacement) for item in current.items if _normalize_grade(item.grade) == original]
        if edits:
            return MessagePlan(intent='edit', edits=edits)
    return None





def is_new_request(text):
    if re.search(r'\b(замени|заменить|измени|изменить|верни|восстанови|убери|удали)\b',text,re.I):
        return False
    return any(PRODUCT_ROW.search(line) and QUANTITY_RE.search(line) for line in text.splitlines())




class TelegramService:
    def __init__(self, ai, repository, policy_repository=None):
        self.ai, self.repository = ai, repository
        self.policy_repository = policy_repository

    def _approved_rules(self):
        return self.policy_repository.active_rules() if self.policy_repository else ()

    def process(self, user_id, text):
        if CURRENT.get() is not None:
            return self._process(user_id, text)
        with operation():
            return self._process(user_id,text)

    def _save(self, user_id, request):
        phase('sqlite_save')
        self.repository.save(user_id, request)

    def _process(self, user_id, text):
        phase('sqlite_load')
        current = self.repository.load(user_id)
        phase('routing')
        if (current is not None and current.pending_clarification is None
                and current.pending_operation is None and not current.clarification_cancelled):
            current.pending_clarification=next_question(current)
        parse_text, heading_records = prepare_sheet_heading(text)
        candidate = is_new_request(text) or bool(heading_records)
        if current is not None and current.dialog_state == 'awaiting_new' and PRODUCT_ROW.search(text) and re.search(r'\d', text):
            candidate = True
        wording = text.strip().casefold().strip('.!?')
        operation_kind = unsupported_operation(text)
        command_verbs = r'\b(?:суммир\w*|объедин\w*|сложи|перевед\w*|перевест\w*|пересчит\w*|рассчит\w*|посчит\w*|вычисл\w*)\b'
        plain_rows = [row for row in text.splitlines() if PRODUCT_ROW.search(row) and QUANTITY_RE.search(row)
                      and not re.search(command_verbs, row[:PRODUCT_ROW.search(row).start()], re.I)]
        if operation_kind and not plain_rows:
            candidate = False
        if current is not None and not candidate:
            if wording in {'нет', 'неверно', 'отмена', 'отмени', 'отменить', '/cancel', 'отмени операцию', 'отменить операцию'}:
                if current.pending_operation or current.pending_clarification:
                    return self._cancel_operation(user_id, current)
            if operation_kind == 'merge_items':
                if re.search(r'перевед|перевест|пересчит', text, re.I) and any(
                        group.item.unit != 'т' for group in ordered_consolidation(current.items)):
                    return [UNSUPPORTED_MESSAGE]
                return self._merge_response(current)
            if operation_kind:
                return [UNSUPPORTED_MESSAGE]
        if current is not None and not candidate:
            if wording in {'новую', 'новая', 'создать новую', 'новую заявку'}:
                changed = current.model_copy(deep=True)
                changed.dialog_state = 'awaiting_new'
                changed.pending_operation = None
                changed.pending_clarification = None
                changed.clarification_cancelled = True
                self._save(user_id, changed)
                return ['Пришлите новую заявку на металлопрокат. Предыдущая заявка сохранена до успешной обработки новой.']
            if current.dialog_state == 'awaiting_new' and not PRODUCT_ROW.search(text):
                return ['Пришлите новую заявку с наименованиями, размерами и количеством.']
            if current.dialog_state == 'choose_request' and wording in {'изменить', 'изменить заявку', 'старую'}:
                changed = current.model_copy(deep=True)
                changed.dialog_state = 'active'
                changed.pending_operation = None
                self._save(user_id, changed)
                return ['Укажите позицию и поле, которое нужно изменить.']
            if wording in {'как дела', 'привет', 'здравствуйте', 'добрый день'}:
                return self._begin_clarification(user_id, current, text,
                    MessagePlan(intent='clarify', question='Вы хотите изменить активную заявку или создать новую?'))
        if current is None and not PRODUCT_ROW.search(text):
            return ['Пришлите заявку на металлопрокат с размерами и количеством.']
        if current is not None and not candidate:
            explicit_edit=re.search(r'\b(замени|заменить|измени|изменить|верни|восстанови|убери|удали)\b',text,re.I)
            formatting=re.search(r'тоннаж|тонн|количеств',text,re.I) and re.search(r'формат|виду|знак|отображ|округл',text,re.I)
            if current.pending_operation and not explicit_edit and not formatting:
                return self._answer_operation(user_id, current, text)
            if current.pending_clarification and not explicit_edit and not formatting:
                return self._answer_clarification(user_id,current,text)
            if wording in {'да', 'нет', 'верно', 'подтверждаю', 'согласен', 'неверно'} or re.fullmatch(r'\s*\d+(?:[.,]\d+)?\s*',text):
                return ['Нет ожидающего уточнения. Отправьте новую заявку или укажите позицию и поле для изменения.']
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
                    self._save(user_id, changed)
                    return messages
            plan = local_edit_plan(text, current, approved_rules=self._approved_rules())
            if plan is not None:
                emit('local_edit', stage='routing', edit_count=len(plan.edits))
                if plan.intent == 'clarify':
                    return self._begin_clarification(user_id, current, text, plan)
                return self._edit(user_id, current, plan)
            for attempt in range(2):
                phase('classification')
                plan = self.ai.interpret_message(text,current) if attempt==0 else self.ai.interpret_message(text,current,reasoning_effort=os.getenv('OPENAI_FALLBACK_REASONING_EFFORT','medium'))
                if plan.intent == 'clarify':
                    return self._begin_clarification(user_id, current, text, plan)
                if plan.intent == 'operation':
                    return self._execute_operation(user_id, current, plan.operation)
                if plan.intent != 'edit':
                    break
                try:
                    return self._edit(user_id,current,plan)
                except SemanticExtractionError:
                    if attempt:
                        raise
                    emit('medium_retry',stage='classification',reason='edit_target_mismatch')
        elif re.search(r'\b(замени|заменить|измени|верни|убери)\b', text, re.I):
            return ['Сначала отправьте заявку, затем команду изменения.']
        for attempt in range(2):
            try:
                active=self._build_active(text,parse_text,heading_records,effort=None if attempt==0 else os.getenv('OPENAI_FALLBACK_REASONING_EFFORT','medium'))
                break
            except SemanticExtractionError as error:
                complete_rows=[row for row in parse_text.splitlines() if PRODUCT_ROW.search(row) and not row.strip().endswith(':')]
                source_has_quantities=bool(complete_rows) and all(QUANTITY_RE.search(row) for row in complete_rows)
                eligible = {'source_fragment_mismatch','missing_positions','merged_positions',
                            'unsupported_attributes','header_mapping','header_positions_missing',
                            'additional_positions','city_mismatch'}
                if attempt or not source_has_quantities or error.code not in eligible:
                    raise
                emit('medium_retry',stage='extraction',reason=error.code)
        if active.pending_clarification is None and active.clarification_questions:
            active.pending_operation = PendingOperation(
                user_id=user_id, request_id=active.request_id,
                request_fingerprint=request_fingerprint(active),
                operation=RequestOperation(kind='edit_fields'), command=text,
                question='\n'.join(active.clarification_questions), expected_answer='details')
        messages=self._response(active,active.clarification_questions)
        self._save(user_id,active)
        return messages

    def _build_active(self,text,parse_text,heading_records,effort=None):
        from ai import _source_fragment_occurrences
        phase('extraction',source_count=sum(bool(PRODUCT_ROW.search(row)) for row in parse_text.splitlines()))
        draft = self.ai.parse_request(parse_text) if effort is None else self.ai.parse_request(parse_text,reasoning_effort=effort)
        initial=draft.model_copy(deep=True)
        if not draft.items:
            raise PipelineError('no_items','Отправьте перечень металлопроката с количеством и единицами.')
        if draft.city and (not _source_fragment_occurrences(parse_text, draft.city)
                or re.search(r'\d', draft.city) or PRODUCT_ROW.search(draft.city)
                or draft.city.strip().casefold().rstrip('.') in UNIT_ALIASES):
            raise SemanticExtractionError('city_mismatch','Город не подтверждается исходной заявкой. Повторите заявку.')
        source_positions = validate_source_coverage(draft, parse_text)
        for item in draft.items:
            exact_source_attributes(item)
        phase('normalization')
        normalize_request(draft)
        confirmed = confirm_shared_attributes(parse_text, draft)
        draft.confirmed_shared_attributes = confirmed
        for evidence in confirmed:
            for position in evidence.applies_to:
                if draft.items[position-1].grade is None:
                    draft.items[position-1].grade = evidence.value
        original = [i.model_copy(deep=True) for i in draft.items]
        phase('validation')
        checked = validate_request(draft, raw_request=parse_text)
        # Never publish a silently erased source attribute.
        for before, after in zip(original, checked.items):
            if before.grade != after.grade or before.gost_tu != after.gost_tu:
                raise SemanticExtractionError('unsupported_attributes','Не удалось подтвердить марку или ГОСТ по исходной строке. Уточните эту позицию.')
        active = ActiveRequest(**{k:v for k,v in checked.model_dump().items() if k != 'items'},
            request_id=uuid4().hex, source_text=text,initial_extraction=initial,
            items=[ActiveItem(**i.model_dump(), item_id=uuid4().hex, original=i.model_copy(deep=True), source_position=source_positions[index - 1])
                   for index, i in enumerate(checked.items, 1)])
        if heading_records:
            from ai import _compact_source_text
            unused = list(heading_records)
            for item in active.items:
                compact = _compact_source_text(item.source_text or '')[0]
                record = next((row for row in unused if _compact_source_text(row[0])[0] == compact), None)
                if record is None:
                    if any(_compact_source_text(row[0])[0] == compact for row in heading_records):
                        raise SemanticExtractionError('header_mapping','Повторно извлечена позиция общей шапки.')
                    continue
                unused.remove(record)
                item.source_text = record[1]
                item.original.source_text = record[1]
                item.source_context = record[2]
            if unused:
                raise SemanticExtractionError('header_positions_missing','Не все позиции под общей шапкой распознаны.')
        phase('sorting')
        active.items = sort_items(active.items)
        active.pending_clarification=next_question(active)
        return active

    def _cancel_operation(self, user_id, current):
        changed = current.model_copy(deep=True)
        changed.pending_operation = None
        changed.pending_clarification = None
        changed.clarification_cancelled = True
        changed.dialog_state = 'active'
        self._save(user_id, changed)
        return ['Ожидающая операция отменена. Заявка сохранена без изменений.']

    def _begin_clarification(self, user_id, current, command, plan, answers=None):
        question = plan.question or 'Укажите позиции и поля для изменения.'
        if unsupported_operation(question) or (plan.operation and plan.operation.kind not in {'edit_fields', 'cancel'}):
            return [UNSUPPORTED_MESSAGE]
        changed = current.model_copy(deep=True)
        operation = plan.operation or RequestOperation(kind='edit_fields', edits=plan.edits)
        concrete = bool(operation.edits) or operation.kind == 'cancel'
        changed.pending_clarification = None if concrete else question_state(question, changed)
        changed.pending_operation = None
        changed.clarification_cancelled = False
        if changed.pending_clarification is None:
            changed.pending_operation = PendingOperation(
                user_id=user_id, request_id=current.request_id,
                request_fingerprint=request_fingerprint(current),
                operation=operation,
                command=command, question=question,
                expected_answer='confirmation' if concrete else 'details', answers=answers or [])
        if re.search(r'нов\w*', question, re.I) and re.search(r'измен|прав', question, re.I):
            changed.dialog_state = 'choose_request'
        self._save(user_id, changed)
        return [question]

    def _execute_operation(self, user_id, current, operation):
        if operation is None:
            raise PipelineError('missing_operation', 'Укажите операцию над заявкой.')
        if operation.kind == 'cancel':
            return self._cancel_operation(user_id, current)
        if operation.kind == 'merge_items':
            return self._merge_response(current)
        if operation.kind != 'edit_fields':
            return [UNSUPPORTED_MESSAGE]
        return self._edit(user_id, current, MessagePlan(intent='edit', edits=operation.edits))

    def _answer_operation(self, user_id, current, text):
        pending = current.pending_operation
        if (pending.user_id != user_id or pending.request_id != current.request_id
                or pending.request_fingerprint != request_fingerprint(current)):
            raise PipelineError('stale_operation', 'Предложение относится к другой или изменённой заявке. Отмените его и повторите команду.')
        answer = text.strip().casefold().strip('.!?')
        if pending.expected_answer == 'confirmation':
            if answer in {'да', 'верно', 'подтверждаю', 'согласен'}:
                return self._execute_operation(user_id, current, pending.operation)
            return [pending.question]
        if answer in {'да', 'верно', 'подтверждаю', 'согласен'}:
            return [pending.question + '\nУкажите конкретные позиции и значения; предложение ещё не определено.']
        # Resume the same field-edit pipeline with the saved context. A reply
        # cannot replace the active request through model classification.
        answers = pending.answers + [text]
        plan = self.ai.interpret_message(
            'Исходная команда: ' + pending.command + '\nУточняющий вопрос: ' + pending.question
            + '\nОтветы пользователя: ' + '\n'.join(answers), current)
        if plan.intent == 'clarify':
            return self._begin_clarification(user_id, current, pending.command, plan, answers=answers)
        if plan.intent == 'edit':
            return self._edit(user_id, current, plan)
        if plan.intent == 'operation':
            return self._execute_operation(user_id, current, plan.operation)
        return [pending.question]

    def _edit(self, user_id, current, plan):
        phase('correction')
        if not plan.edits:
            return self._begin_clarification(user_id, current, '',
                MessagePlan(intent='clarify', question=plan.question or 'Укажите полную марку и позиции, которые нужно изменить.'))
        changed = current.model_copy(deep=True)
        by_id = {i.item_id:i for i in changed.items}
        seen = set()
        for edit in plan.edits:
            if edit.item_id not in by_id or (edit.item_id, edit.field) in seen:
                raise SemanticExtractionError('edit_target_mismatch','Не удалось сопоставить изменение с позицией. Уточните номер.')
            seen.add((edit.item_id, edit.field))
            item = by_id[edit.item_id]
            value = getattr(item.original, edit.field) if edit.restore else edit.value
            if edit.field == 'quantity':
                try:
                    value = Decimal(str(value).replace(',', '.'))
                except Exception:
                    raise PipelineError('invalid_quantity','Укажите положительное количество.') from None
                if not value.is_finite() or value <= 0:
                    raise PipelineError('invalid_quantity','Укажите положительное количество.')
            elif edit.field == 'unit' and value not in {'т','кг','шт','шт.','м','пог. м','м²'}:
                raise PipelineError('invalid_unit','Укажите единицу: т, кг, шт., м, пог. м или м².')
            elif edit.field == 'grade' and not edit.restore and (not value or '\n' in value or len(value) > 100):
                raise PipelineError('invalid_grade','Укажите полную марку стали.')
            if edit.field == 'grade' and item.product_group == 'strip' and item.grade and item.grade in (item.size_designation or ''):
                if value is None:
                    raise PipelineError('compound_grade','Уточните полное обозначение ленты для восстановления марки.')
                item.size_designation = item.size_designation.replace(item.grade, value, 1)
            setattr(item, edit.field, value)
        changed.pending_operation = None
        changed.clarification_cancelled = False
        changed.pending_clarification=next_question(changed)
        questions=[]
        messages = self._response(changed, questions)
        changed.dialog_state = 'active'
        self._save(user_id, changed)
        return messages

    def _answer_clarification(self,user_id,current,text):
        phase('clarification')
        pending=current.pending_clarification
        if pending.request_id != current.request_id:
            raise PipelineError('stale_clarification','Уточнение относится к другой заявке. Отправьте новую заявку.')
        item=next((i for i in current.items if i.item_id==pending.item_id),None)
        if item is None:
            raise PipelineError('stale_clarification','Позиция для уточнения не найдена.')
        values,question=answer_values(pending,text)
        if values is None:
            self._save(user_id, current)
            return [question]
        changed=current.model_copy(deep=True)
        if question:
            changed.pending_clarification=PendingClarification(request_id=changed.request_id,item_id=pending.item_id,
                field='quantity',kind='number',question=question)
            self._save(user_id,changed)
            return [question]
        target=next(i for i in changed.items if i.item_id==pending.item_id)
        for name,value in values.items():
            if name not in {'quantity','unit'}:
                raise PipelineError('invalid_clarification','Неподдерживаемое поле уточнения.')
            converted=Decimal(value) if name=='quantity' else value
            if name=='quantity' and (not converted.is_finite() or converted<=0):
                raise PipelineError('invalid_quantity','Укажите положительное количество.')
            if name=='unit' and converted not in {'т','кг','шт','шт.','м','пог. м','м²'}:
                raise PipelineError('invalid_unit','Укажите единицу: т, кг, шт., м, пог. м или м².')
            setattr(target,name,converted)
        changed.pending_operation = None
        changed.clarification_cancelled = False
        changed.clarification_answers.append(text)
        changed.pending_clarification=next_question(changed)
        changed.missing_fields=[field for field in changed.missing_fields if not field.endswith(('.quantity','.unit'))]
        changed.is_valid=not changed.missing_fields and changed.pending_clarification is None
        changed.clarification_questions=[]
        messages=self._response(changed,[])
        self._save(user_id,changed)
        return messages

    def _merge_response(self, request):
        from normalization_policy import grade_merge_candidates
        messages = self._response(request, [])
        candidates = grade_merge_candidates(request.items, request.normalization_rules)
        if candidates:
            suggestions = ['Разные марки автоматически не объединены. Возможные группы для согласования:']
            for rows in candidates:
                suggestions.append('Группа:')
                suggestions.extend(f'исходная позиция {position}: {grade}' for position, grade in rows)
            suggestions.append('Уточните допустимую марку и исходные позиции. Это не создаст глобальное правило.')
            messages += split_positions('\n'.join(suggestions))
        return messages

    def _response(self, request, questions):
        phase('formatting')
        from quantity import refresh_mass_calculations
        refresh_mass_calculations(request.items)
        if request.pending_operation:
            questions=[request.pending_operation.question]
        elif request.pending_clarification:
            questions=[request.pending_clarification.question]
        rules = self._approved_rules()
        messages = split_positions(format_normalized_request(request, approved_rules=rules))
        request.normalization_rules = list(rules)
        if questions:
            messages += split_positions('\n'.join(questions))
        return messages


def split_positions(text, limit=4000):
    messages, lines, size = [], [], 0
    for line in text.splitlines():
        length = len(line.encode('utf-16-le')) // 2
        if length > limit:
            raise PipelineError('telegram_line_limit','Одна позиция слишком длинная для Telegram. Сократите её описание.')
        if lines and size + length + 1 > limit:
            messages.append('\n'.join(lines))
            lines, size = [], 0
        lines.append(line)
        size += length + (1 if len(lines) > 1 else 0)
    if lines:
        messages.append('\n'.join(lines))
    return messages
