"""Application layer: source fidelity, field edits and persistent user context."""
from decimal import Decimal
from typing import Literal, Optional
from uuid import uuid4
from contextlib import nullcontext
import re
import os
import math
from diagnostics import operation, phase, emit, CURRENT, PipelineError, SemanticExtractionError
from clarifications import (PendingClarification, next_question, question_state, answer_values,
                            parse_bulk_quantity, parse_dimension_answers)
from request_operations import (Edit, RequestOperation, PendingOperation, request_fingerprint,
                                unsupported_operation, UNSUPPORTED_MESSAGE)
from pydantic import BaseModel, Field
from schemas import PurchaseRequest, RequestItem
from normalization_policy import GradeRule
from nomenclature import NomenclatureSpec
from dialog_context import DetailQuestion, DetailAnswer
from normalizer import normalize_request, UNIT_ALIASES
from validator import validate_request, confirm_shared_attributes, grouped_questions_for_missing_fields
from formatter import format_normalized_request, sort_items, request_consolidation
from commercial import CommercialDecision
from quantity import QUANTITY_RE, without_mass_annotation
from source_fidelity import (PRODUCT_ROW, product_row_key, validate_source_coverage,
                             source_product_rows, source_has_complete_quantities)
from source_attributes import GRADE_RE, source_standards, source_grade_matches


class MessagePlan(BaseModel):
    intent: Literal['new', 'edit', 'clarify', 'operation', 'display', 'show']
    edits: list[Edit] = Field(default_factory=list)
    question: Optional[str] = None
    operation: Optional[RequestOperation] = None
    display_format: Optional[Literal['rebar_diameter_first', 'rebar_grade_first']] = None


class ActiveItem(RequestItem):
    item_id: str
    original: RequestItem
    source_context: Optional[str] = None
    source_position: Optional[int] = None


class ActiveRequest(PurchaseRequest):
    request_id: str
    dialog_state: Literal['active', 'choose_request', 'awaiting_new'] = 'active'
    tonnage_decimals: Optional[Literal[2, 3]] = None
    rebar_diameter_first: bool = False
    pending_clarification: Optional[PendingClarification] = None
    pending_operation: Optional[PendingOperation] = None
    clarification_cancelled: bool = False
    clarification_answers: list[str] = Field(default_factory=list)
    initial_extraction: Optional[PurchaseRequest] = None
    source_text: str
    normalization_rules: list[GradeRule] = Field(default_factory=list)
    items: list[ActiveItem] = Field(default_factory=list)
    # Python-owned derived-view policy; absent in old JSON and AI schemas.
    commercial_view: bool = False
    commercial_decisions: list[CommercialDecision] = Field(default_factory=list)
    # Additive JSON metadata; legacy requests and SQLite tables stay compatible.
    fallback_status: Optional[Literal['partial', 'original']] = None
    unprocessed_source: list[str] = Field(default_factory=list)
    recovery_specs: list[NomenclatureSpec] = Field(default_factory=list)
    pending_detail: Optional[DetailQuestion] = None
    detail_answers: list[DetailAnswer] = Field(default_factory=list)
    append_commands: list[str] = Field(default_factory=list)

    @property
    def user_status(self):
        return 'В работе' if (not self.is_valid or self.missing_fields or self.pending_detail or self.pending_clarification
                             or self.pending_operation or self.fallback_status) else 'Готова'


def alternative_grade_value(source):
    """Offer only the literal separate grade tokens; never infer equivalence."""
    matches = list(GRADE_RE.finditer(source))
    if len(matches) < 2 or all(re.search(r'\bили\b', source[a.end():b.start()], re.I)
                              for a, b in zip(matches, matches[1:])):
        return None
    from normalizer import _normalize_grade
    values = [_normalize_grade(match[0]) for match in matches]
    return values[0] + ' (или ' + ' или '.join(values[1:]) + ')'


EDIT_WORDS = r'\b(?:замени|заменить|измени|изменить|поменяй|верни|восстанови|убери|удали|допиши|добавь|укажи|пропиши)\b'
DISPLAY_WORDS = r'\b(?:напиши|пропиши|покажи|переставь|оформи)\b'


def exact_source_attributes(item, defer_grade_question=False):
    """Recover full steel tokens and decimal quantities from literal source."""
    source = item.source_text or ''
    if item.quantity is not None and item.quantity < 0:
        raise PipelineError('invalid_quantity', 'Количество должно быть конечным положительным числом.')
    matches = source_grade_matches(source)
    grades = [m.group() for m in matches]
    # A designation like strip contains a steel token inside its compound size.
    if len(grades) == 1 and item.product_group != 'strip':
        from normalizer import _normalize_grade
        item.grade = _normalize_grade(grades[0])
    elif len(grades) > 1:
        if all(re.search(r'\bили\b',source[a.end():b.start()],re.I) for a,b in zip(matches,matches[1:])):
            from normalizer import _normalize_grade
            normalized=[_normalize_grade(match[0]) for match in matches]
            item.grade=normalized[0]+' (или '+' или '.join(normalized[1:])+')'
        else:
            if not defer_grade_question:
                raise PipelineError('ambiguous_grades','Укажите, являются ли перечисленные марки разрешёнными альтернативами («или»).')
            item.grade = None  # Persist the literal request and a typed proposal first.
    # Restore every explicitly written standard, rather than trusting omissions.
    standards = source_standards(source)
    if standards:
        item.gost_tu = ', '.join(standards)
    name = (source + ' ' + (item.product or '')).casefold()
    qualifier = re.search(r'(?<!\w)БТ-ПН-О(?!\w)', source, re.I)
    if qualifier:
        item.form = qualifier.group().upper()
    if item.product_group == 'sheet':
        if re.search(r'х/?к|холоднокатан', name):
            item.product = 'Лист Х/К'
        elif re.search(r'г/?к|горячекатан', name):
            item.product = 'Лист Г/К'
    elif item.product_group == 'round_pipe':
        if re.search(r'э[\\/]?с|электросвар', name):
            item.product = 'Труба э/с'
        elif 'вгп' in name or 'водогаз' in name:
            item.product = 'Труба ВГП'
        elif 'бесшов' in name:
            item.product = 'Труба бесшовная'
        elif re.search(r'ГОСТ\s*10704(?:-91)?', source, re.I):
            item.product = 'Труба э/с'
    from quantity import beam_export_columns, length_mass_columns, LITERAL_QUANTITY_RE, literal_decimal
    beam_columns = beam_export_columns(source) if item.product_group == 'beam' else None
    if beam_columns and item.length_requirement is None:
        # Preserve the literal column; this format does not prove millimetres.
        item.length_requirement = beam_columns['length_requirement']
    columns = length_mass_columns(source,item.product_group)
    if columns:
        item.length_mm=float(columns['length_mm'])
        item.quantity,item.unit,item.source_unit=columns['quantity'],'кг','кг'
        return
    quantity_source = without_mass_annotation(source)
    quantities = [m for m in LITERAL_QUANTITY_RE.finditer(quantity_source)
                  if not re.search(r'[×хx*]\s*$',quantity_source[max(0,m.start()-20):m.start()],re.I)
                  and not re.search(r'(?:длина|раскрой)\s*[:=]?\s*$', quantity_source[max(0,m.start()-20):m.start()], re.I)]
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
            from quantity import confirmed_unitless_quantity
            item.quantity = confirmed_unitless_quantity(quantity_source, item.quantity)
            # No source unit was confirmed, regardless of a model's suggestion.
            item.unit = None
            item.source_unit = None
    if quantities:
        last = quantities[-1]
        item.quantity = literal_decimal(last[1])
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
            item.unit = UNIT_ALIASES.get(raw_unit,raw_unit.rstrip('.'))
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
    match = re.fullmatch(r'\s*(?:(?:в|во)\s+)?(исходной\s+)?позиции\s+(\d+)\s+(?:измени|замени|поменяй|допиши|добавь|укажи|пропиши)\s+(?:(марку(?:\s+стали)?|количество)\s+(?:на\s+)?)?(.+?)\s*', text, re.I)
    restore = re.fullmatch(r'\s*(?:верни|восстанови)\s+исходную\s+марку\s+(исходной\s+)?позиции\s+(\d+)\s*', text, re.I)
    selection = match or restore
    if selection:
        number = int(selection[2])
        if selection[1]:
            candidates = [item for index, item in enumerate(current.items, 1)
                          if (item.source_position or index) == number]
        else:
            groups = request_consolidation(current)
            if not 1 <= number <= len(groups):
                return MessagePlan(intent='clarify', question='Укажите существующую исходную позицию.')
            group = groups[number - 1]
            candidates = [current.items[index - 1] for index in group.source_indices]
            if len(candidates) > 1 and not (restore or (match and (match[3] or '').casefold() != 'количество')):
                positions = ', '.join(map(str, group.source_positions))
                return MessagePlan(intent='clarify', question=(
                    f'Итоговая строка {number} включает исходные строки {positions}. '
                    'Укажите нужную командой «В исходной позиции N измени поле на значение».'))
        if not candidates:
            return MessagePlan(intent='clarify', question='Укажите существующую исходную позицию.')
        item_id = candidates[0].item_id
        if restore:
            return MessagePlan(intent='edit', edits=[Edit(item_id=item.item_id, field='grade', restore=True) for item in candidates])
        if (match[3] or '').casefold() != 'количество' and GRADE_RE.fullmatch(match[4]):
            return MessagePlan(intent='edit', edits=[Edit(item_id=item.item_id, field='grade', value=_normalize_grade(match[4])) for item in candidates])
        quantity = QUANTITY_RE.fullmatch(match[4])
        if match[3] and match[3].casefold() == 'количество' and quantity:
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





def scoped_plan(text, current, plan, confirmed=False):
    """Bind model edits to the explicitly numbered source/display row in Python."""
    if not plan.edits:
        return plan
    ordinals = {'первой': 1, 'второй': 2, 'третьей': 3, 'четвертой': 4, 'четвёртой': 4,
                'пятой': 5, 'шестой': 6, 'седьмой': 7, 'восьмой': 8, 'девятой': 9, 'десятой': 10}
    command = re.sub(r'\b(' + '|'.join(ordinals) + r')\s+(исходной\s+)?позиции\b',
                     lambda m: (m[2] or '') + 'позиции ' + str(ordinals[m[1].casefold()]), text, flags=re.I)
    selections = list(re.finditer(r'(?P<source>исходной\s+)?позиции\s+(?P<number>\d+)\b', command, re.I))
    selected = selections[-1] if selections else None
    if not selected:
        proposed_ids = {edit.item_id for edit in plan.edits}
        for group in request_consolidation(current):
            group_ids = set(group.source_item_ids)
            group_edits = [edit for edit in plan.edits if edit.item_id in group_ids]
            if len(group.source_indices) > 1 and group_edits:
                fields = {edit.field for edit in group_edits}
                explicit_global_grade = bool(re.search(r'\bвезде\b', command, re.I)) and fields == {'grade'}
                unsafe_quantity = bool(fields & {'quantity', 'unit'})
                partial_group = not group_ids <= proposed_ids
                if fields == {'grade'}:
                    from normalizer import _normalize_grade
                    literal_values = {_normalize_grade(match[0]) for match in GRADE_RE.finditer(command)}
                    literal_values.update('А' + match[0][1:].upper().replace('C', 'С')
                                          for match in re.finditer(r'(?<!\w)[АаAa]\d{3}[СсCc]?(?!\w)', command))
                    literal_grade = all(edit.restore or _normalize_grade(edit.value) in literal_values
                                        for edit in group_edits)
                else:
                    literal_grade = False
                unscoped_grade = fields == {'grade'} and not explicit_global_grade and not literal_grade
                if not (unsafe_quantity or partial_group or unscoped_grade):
                    continue
                positions = ', '.join(map(str, group.source_positions))
                return MessagePlan(intent='clarify', question=(
                    f'Правка затрагивает строку с исходными позициями {positions}. '
                    'Укажите номер отображаемой позиции для изменения марки всей строки '
                    'или номер исходной позиции для изменения только этой строки.'))
        if not confirmed and plan.intent == 'edit' and all(edit.field == 'grade' for edit in plan.edits):
            from normalizer import _normalize_grade
            literal_values = {_normalize_grade(match[0]) for match in GRADE_RE.finditer(text)}
            literal_values.update('А' + match[0][1:].upper().replace('C', 'С')
                                  for match in re.finditer(r'(?<!\w)[АаAa]\d{3}[СсCc]?(?!\w)', text))
            proposed = {edit.value for edit in plan.edits if not edit.restore}
            if any(_normalize_grade(value) not in literal_values for value in proposed):
                values = ', '.join(sorted(str(value) for value in proposed))
                return plan.model_copy(update={'intent': 'clarify', 'question': f'Модель предложила марку {values}, не указанную явно в команде. Подтвердите замену: да или нет.'})
        return plan
    number = int(selected['number'])
    if selected['source']:
        targets = [item for index, item in enumerate(current.items, 1)
                   if (item.source_position or index) == number]
    else:
        groups = request_consolidation(current)
        targets = [current.items[index - 1] for index in groups[number - 1].source_indices] if 0 < number <= len(groups) else []
    ids = {item.item_id for item in targets}
    if not ids or any(edit.item_id not in ids for edit in plan.edits):
        raise SemanticExtractionError('edit_target_mismatch', 'Правка не соответствует указанной позиции. Уточните номер.')
    if len(targets) > 1:
        values = {(edit.value, edit.restore) for edit in plan.edits}
        if all(edit.field == 'grade' for edit in plan.edits) and len(values) == 1:
            value, restore = next(iter(values))
            plan = plan.model_copy(update={'edits': [Edit(item_id=item.item_id, field='grade', value=value, restore=restore)
                                                    for item in targets]})
        else:
            return MessagePlan(intent='clarify', question='Эта итоговая строка содержит несколько исходных позиций. Укажите исходную позицию и новое значение; суммарное количество не распределяется автоматически.')
    if not confirmed and plan.intent == 'edit' and all(edit.field == 'grade' for edit in plan.edits):
        from normalizer import _normalize_grade
        literal_values = {_normalize_grade(match[0]) for match in GRADE_RE.finditer(text)}
        literal_values.update('А' + match[0][1:].upper().replace('C', 'С')
                              for match in re.finditer(r'(?<!\w)[АаAa]\d{3}[СсCc]?(?!\w)', text))
        proposed = {edit.value for edit in plan.edits if not edit.restore}
        if any(_normalize_grade(value) not in literal_values for value in proposed):
            values = ', '.join(sorted(str(value) for value in proposed))
            return plan.model_copy(update={'intent': 'clarify', 'question': f'Модель предложила марку {values}, не указанную явно в команде. Подтвердите замену: да или нет.'})
    return plan


def is_new_request(text):
    if re.search(EDIT_WORDS + '|' + DISPLAY_WORDS, text, re.I):
        return False
    return any(QUANTITY_RE.search(row) for row in source_product_rows(text))




class TelegramService:
    def __init__(self, ai, repository, commercial_view=False, *, assistant=None, reliable=False, policy_repository=None):
        self.ai, self.repository = ai, repository
        self.commercial_view = commercial_view
        self.assistant, self.reliable = assistant, reliable
        self.policy_repository = policy_repository

    def _approved_rules(self):
        return self.policy_repository.active_rules() if self.policy_repository else ()

    def process(self, user_id, text, *, commercial_decisions=None):
        if self.reliable:
            from telegram_operator import process_operator
            with operation() if CURRENT.get() is None else nullcontext():
                return process_operator(self, user_id, text, commercial_decisions=commercial_decisions)
        if CURRENT.get() is not None:
            return self._process(user_id, text, commercial_decisions=commercial_decisions)
        with operation():
            return self._process(user_id,text, commercial_decisions=commercial_decisions)

    def _save(self, user_id, request):
        phase('sqlite_save')
        self.repository.save(user_id, request)

    def _process(self, user_id, text, *, commercial_decisions=None, force_new=False):
        phase('sqlite_load')
        current = self.repository.load(user_id)
        if force_new:
            current = None
        phase('routing')
        wording = text.strip().casefold().strip('.!?')
        if wording in {'покажи текущую заявку', 'покажи заявку', 'текущая заявка', '/show'}:
            return self._response(current, []) if current is not None else ['Активной заявки нет. Пришлите новую заявку.']
        if re.fullmatch(r'отмени(?:ть)?\s+последнее\s+изменение', wording):
            return ['Отмена последнего сохранённого изменения пока не поддерживается. Можно явно восстановить исходное поле или отменить ожидающую операцию.']
        if current is None and wording in {'да', 'нет', 'верно', 'подтверждаю', 'согласен', 'неверно', 'обеим', 'к обеим', 'для суммы'}:
            return ['Нет ожидающего уточнения. Сначала отправьте заявку.']
        if current is not None:
            displayed = self._display_command(user_id, current, text)
            if displayed is not None:
                return displayed
        if (current is not None and current.pending_clarification is None
                and current.pending_operation is None and not current.clarification_cancelled):
            current.pending_clarification=next_question(current)
        parse_text, heading_records = prepare_sheet_heading(text)
        candidate = is_new_request(text) or bool(heading_records)
        if (current is not None and current.dialog_state == 'awaiting_new'
                and PRODUCT_ROW.search(text) and re.search(r'\d', text)
                and not re.search(EDIT_WORDS + '|' + DISPLAY_WORDS, text, re.I)):
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
                        group.item.unit != 'т' for group in request_consolidation(current)):
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
        if current is None and not PRODUCT_ROW.search(text) and not source_product_rows(text):
            return ['Пришлите заявку на металлопрокат с размерами и количеством.']
        if current is not None and not candidate:
            explicit_edit=re.search(EDIT_WORDS, text, re.I)
            formatting=re.search(r'тоннаж|тонн|количеств',text,re.I) and re.search(r'формат|виду|знак|отображ|округл',text,re.I)
            if current.pending_operation and not explicit_edit and not formatting:
                return self._answer_operation(user_id, current, text)
            if (current.pending_clarification and not formatting
                    and (not explicit_edit or parse_bulk_quantity(text, current) is not None)):
                return self._answer_clarification(user_id,current,text)
            if wording in {'да', 'нет', 'верно', 'подтверждаю', 'согласен', 'неверно', 'обеим', 'к обеим', 'для суммы', 'обоим', 'к обоим'} or re.fullmatch(r'\s*\d+(?:[.,]\d+)?\s*',text):
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
                return self._edit(user_id, current, plan, command=text)
            for attempt in range(2):
                phase('classification')
                plan = self.ai.interpret_message(text,current) if attempt==0 else self.ai.interpret_message(text,current,reasoning_effort=os.getenv('OPENAI_FALLBACK_REASONING_EFFORT','medium'))
                if plan.intent == 'show':
                    return self._response(current, [])
                if plan.intent == 'display':
                    return self._set_display(user_id, current, plan.display_format)
                if plan.intent == 'clarify':
                    return self._begin_clarification(user_id, current, text, plan)
                if plan.intent == 'operation':
                    return self._execute_operation(user_id, current, plan.operation, command=text)
                if plan.intent != 'edit':
                    if re.search(EDIT_WORDS + '|' + DISPLAY_WORDS, text, re.I):
                        return ['Команда относится к активной заявке. Укажите позицию и поле для изменения; новая заявка не создавалась.']
                    break
                try:
                    return self._edit(user_id,current,plan,command=text)
                except SemanticExtractionError:
                    if attempt:
                        raise
                    emit('medium_retry',stage='classification',reason='edit_target_mismatch')
        elif re.search(EDIT_WORDS + '|' + DISPLAY_WORDS, text, re.I):
            return ['Сначала отправьте заявку, затем команду изменения.']
        for attempt in range(2):
            try:
                active=self._build_active(text,parse_text,heading_records,effort=None if attempt==0 else os.getenv('OPENAI_FALLBACK_REASONING_EFFORT','medium'))
                break
            except SemanticExtractionError as error:
                source_has_quantities = source_has_complete_quantities(parse_text)
                eligible = {'source_fragment_mismatch','missing_positions','merged_positions',
                            'unsupported_attributes','header_mapping','header_positions_missing',
                            'additional_positions','city_mismatch'}
                if attempt or not source_has_quantities or error.code not in eligible:
                    raise
                emit('medium_retry',stage='extraction',reason=error.code)
        if commercial_decisions is not None:
            if not active.commercial_view:
                raise ValueError('Коммерческие решения требуют коммерческого представления.')
            active.commercial_decisions = commercial_decisions(active)
        grade_edits = [Edit(item_id=item.item_id, field='grade', value=value)
                       for item in active.items
                       if (value := alternative_grade_value(item.source_text or ''))]
        if grade_edits:
            active.is_valid = False
            active.pending_operation = PendingOperation(user_id=user_id,
                request_id=active.request_id, request_fingerprint=request_fingerprint(active),
                operation=RequestOperation(kind='confirm_grade_alternatives', edits=grade_edits),
                command=text, question='Укажите, являются ли перечисленные марки разрешёнными альтернативами («или»). Ответьте да, нет или отмена.',
                expected_answer='confirmation')
        elif active.pending_clarification is None and active.clarification_questions:
            active.pending_operation = PendingOperation(
                user_id=user_id, request_id=active.request_id,
                request_fingerprint=request_fingerprint(active),
                operation=RequestOperation(kind='edit_fields'), command=text,
                question='\n'.join(active.clarification_questions), expected_answer='details')
        if self.reliable:
            from dialog_context import pending_detail
            active.pending_detail = pending_detail(active)
        messages=self._response(active,active.clarification_questions)
        self._save(user_id,active)
        return messages

    def _build_active(self,text,parse_text,heading_records,effort=None, *, draft=None):
        from ai import _source_fragment_occurrences
        source_count = len(source_product_rows(parse_text))
        phase('extraction',source_count=source_count)
        if draft is None and self.reliable and effort is None:
            from telegram_operator import local_request_draft
            draft = local_request_draft(parse_text)
        if draft is None:
            try:
                draft = self.ai.parse_request(parse_text) if effort is None else self.ai.parse_request(parse_text,reasoning_effort=effort)
            except SemanticExtractionError as error:
                rejected = getattr(error, 'rejected_extraction', None)
                if self.reliable and rejected is not None:
                    from telegram_operator import capture_extraction
                    capture_extraction(rejected)
                raise
            if self.reliable:
                from telegram_operator import capture_extraction
                capture_extraction(draft)
        initial=draft.model_copy(deep=True)
        if not draft.items:
            if source_count:
                raise SemanticExtractionError('missing_positions',
                    'Не все исходные позиции распознаны. Повторите заявку; предыдущая сохранена.',
                    source_count=source_count, extracted_count=0)
            raise PipelineError('no_items','Отправьте перечень металлопроката с количеством и единицами.')
        if draft.city and (not _source_fragment_occurrences(parse_text, draft.city)
                or re.search(r'\d', draft.city) or PRODUCT_ROW.search(draft.city)
                or draft.city.strip().casefold().rstrip('.') in UNIT_ALIASES):
            raise SemanticExtractionError('city_mismatch','Город не подтверждается исходной заявкой. Повторите заявку.')
        source_positions = validate_source_coverage(draft, parse_text)
        for item in draft.items:
            if self.reliable:
                from telegram_operator import restore_local_source_attributes
                restore_local_source_attributes(item)
            exact_source_attributes(item, defer_grade_question=True)
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
        checked = validate_request(draft, raw_request=parse_text, source_positions=source_positions)
        # Never publish a silently erased source attribute.
        for before, after in zip(original, checked.items):
            if before.grade != after.grade or before.gost_tu != after.gost_tu:
                raise SemanticExtractionError('unsupported_attributes','Не удалось подтвердить марку или ГОСТ по исходной строке. Уточните эту позицию.')
        active = ActiveRequest(**{k:v for k,v in checked.model_dump().items() if k != 'items'},
            request_id=uuid4().hex, source_text=text,initial_extraction=initial,
            commercial_view=self.commercial_view,
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
        before_sort = list(active.items)
        active.items = sort_items(active.items)
        sorted_indices = {item.item_id:index for index,item in enumerate(active.items)}
        active.missing_fields = [re.sub(r'items\[(\d+)\]',
            lambda m: f'items[{sorted_indices[before_sort[int(m[1])].item_id]}]', field)
            for field in active.missing_fields]
        if self.reliable:
            from telegram_operator import bind_pipe_relations
            bind_pipe_relations(active.items)
        active.pending_clarification=next_question(active)
        return active

    def _display_command(self, user_id, current, text):
        """Presentation commands cannot replace source rows or technical facts."""
        if re.fullmatch(r'\s*арматура\s+(?:напиши|поставь|укажи)\s+диаметр\s+(?:вначале|в начале|сначала)\s*[.!]?\s*', text, re.I):
            return self._set_display(user_id, current, 'rebar_diameter_first')
        template = re.fullmatch(r'\s*пропиши\s+арматура\s+[фФØø]\s*(\d+(?:[.,]\d+)?)\s+([аАA]\d{3}[сСC]?)\s*[-—–]\s*(\d+(?:[.,]\d+)?)\s*(т|кг)\s*', text, re.I)
        if not template:
            return None
        groups = [group for group in request_consolidation(current)
                  if group.item.product_group == 'rebar' and group.item.diameter_mm is not None
                  and Decimal(str(group.item.diameter_mm)) == Decimal(template[1].replace(',', '.'))]
        if len(groups) != 1:
            return ['Укажите номер отображаемой позиции арматуры; одного диаметра недостаточно для выбора.']
        group = groups[0]
        from quantity import normalize_quantity
        if normalize_quantity(template[3], template[4]) != (group.item.quantity, group.item.unit):
            return ['Количество в шаблоне отличается от заявки. Укажите исходную позицию и новое количество отдельной командой.']
        requested_grade = 'А' + template[2][1:].upper().replace('C', 'С')
        if requested_grade != group.item.grade:
            edits = [Edit(item_id=current.items[index - 1].item_id, field='grade', value=requested_grade)
                     for index in group.source_indices]
            return self._begin_clarification(user_id, current, text,
                MessagePlan(intent='clarify', edits=edits,
                    question=f'Вы указали {requested_grade} вместо {group.item.grade}. Заменить марку для всех исходных строк этой позиции? Ответьте да или нет.'))
        changed = current.model_copy(deep=True)
        changed.rebar_diameter_first = True
        messages = self._response(changed, [])
        self._save(user_id, changed)
        return messages

    def _set_display(self, user_id, current, display_format):
        if display_format not in {'rebar_diameter_first', 'rebar_grade_first'}:
            return ['Укажите порядок: диаметр арматуры перед маркой или после марки.']
        changed = current.model_copy(deep=True)
        changed.rebar_diameter_first = display_format == 'rebar_diameter_first'
        messages = self._response(changed, [])
        self._save(user_id, changed)
        return messages

    def _cancel_operation(self, user_id, current):
        changed = current.model_copy(deep=True)
        changed.pending_operation = None
        changed.pending_clarification = None
        changed.pending_detail = None
        changed.clarification_cancelled = True
        changed.dialog_state = 'active'
        self._save(user_id, changed)
        return ['Ожидающая операция отменена. Заявка сохранена без изменений.']

    def _begin_clarification(self, user_id, current, command, plan, answers=None):
        if plan.operation and plan.operation.kind == 'edit_fields':
            plan = plan.model_copy(update={'edits': plan.operation.edits})
        plan = scoped_plan(command, current, plan)
        question = plan.question or 'Укажите позиции и поля для изменения.'
        if unsupported_operation(question) or (plan.operation and plan.operation.kind not in {'edit_fields', 'cancel'}):
            return [UNSUPPORTED_MESSAGE]
        changed = current.model_copy(deep=True)
        operation = plan.operation.model_copy(update={'edits': plan.edits}) if plan.operation and plan.operation.kind == 'edit_fields' else plan.operation or RequestOperation(kind='edit_fields', edits=plan.edits)
        concrete = bool(operation.edits) or operation.kind == 'cancel'
        changed.pending_clarification = None if concrete else question_state(question, changed)
        changed.pending_operation = None
        changed.pending_detail = None
        changed.clarification_cancelled = False
        if changed.pending_clarification is None:
            changed.pending_operation = PendingOperation(
                user_id=user_id, request_id=current.request_id,
                request_fingerprint=request_fingerprint(current),
                operation=operation,
                command=command, question=question,
                expected_answer='confirmation' if concrete else 'details', answers=answers or [])
        if self.reliable:
            from dialog_context import pending_detail
            changed.pending_detail = pending_detail(changed)
        if re.search(r'нов\w*', question, re.I) and re.search(r'измен|прав', question, re.I):
            changed.dialog_state = 'choose_request'
        self._save(user_id, changed)
        return [question]

    def _execute_operation(self, user_id, current, operation, command='', confirmed=False):
        if operation is None:
            raise PipelineError('missing_operation', 'Укажите операцию над заявкой.')
        if operation.kind == 'cancel':
            return self._cancel_operation(user_id, current)
        if operation.kind == 'merge_items':
            return self._merge_response(current)
        if operation.kind == 'confirm_grade_alternatives':
            if not confirmed:
                return ['Разрешённые альтернативы марок нужно подтвердить ответом на ожидающий вопрос.']
            by_id = {item.item_id: item for item in current.items}
            for edit in operation.edits:
                item = by_id.get(edit.item_id)
                if (item is None or edit.field != 'grade' or edit.restore
                        or edit.value != alternative_grade_value(item.source_text or '')):
                    raise PipelineError('invalid_grade_confirmation', 'Уточнение марок не соответствует исходным строкам.')
            return self._edit(user_id, current, MessagePlan(intent='edit', edits=operation.edits),
                              command=command, confirmed=True)
        if operation.kind != 'edit_fields':
            return [UNSUPPORTED_MESSAGE]
        return self._edit(user_id, current, MessagePlan(intent='edit', edits=operation.edits), command=command, confirmed=confirmed)

    def _answer_operation(self, user_id, current, text):
        pending = current.pending_operation
        if (pending.user_id != user_id or pending.request_id != current.request_id
                or pending.request_fingerprint != request_fingerprint(current)):
            raise PipelineError('stale_operation', 'Предложение относится к другой или изменённой заявке. Отмените его и повторите команду.')
        answer = text.strip().casefold().strip('.!?')
        if pending.expected_answer == 'confirmation':
            if answer in {'да', 'верно', 'подтверждаю', 'согласен'}:
                confirmed_current = current.model_copy(deep=True)
                confirmed_current.clarification_answers.append(text)
                return self._execute_operation(user_id, confirmed_current, pending.operation, command=pending.command, confirmed=True)
            if answer in {'для суммы', 'обеим', 'к обеим', 'обоим', 'к обоим'}:
                edits = pending.operation.edits
                if edits and all(edit.field == 'grade' for edit in edits):
                    ids = {edit.item_id for edit in edits}
                    groups = request_consolidation(current)
                    if any(len(group.source_indices) > 1 and ids == set(group.source_item_ids) for group in groups):
                        confirmed_current = current.model_copy(deep=True)
                        confirmed_current.clarification_answers.append(text)
                        return self._execute_operation(user_id, confirmed_current, pending.operation, command=pending.command, confirmed=True)
            return [pending.question]
        if answer in {'да', 'верно', 'подтверждаю', 'согласен'}:
            return [pending.question + '\nУкажите конкретные позиции и значения; предложение ещё не определено.']
        # Resume the same field-edit pipeline with the saved context. A reply
        # cannot replace the active request through model classification.
        if answer in {'для суммы', 'обеим', 'к обеим', 'обоим', 'к обоим'} and re.search(r'количеств', pending.command, re.I):
            return [pending.question]
        if pending.expected_answer == 'details':
            dimensions = parse_dimension_answers(text, current)
            if dimensions:
                changed = current.model_copy(deep=True)
                by_source = {item.source_position: (index, item)
                             for index, item in enumerate(changed.items)
                             if item.source_position is not None}
                by_source.update({index + 1: (index, item)
                                  for index, item in enumerate(changed.items)
                                  if item.source_position is None})
                resolved = set()
                for position, fields in dimensions.items():
                    entry = by_source.get(position)
                    if entry is None:
                        continue
                    index, target = entry
                    for field, value in fields.items():
                        converted = float(value)
                        if not math.isfinite(converted) or converted <= 0 or Decimal(str(converted)) != value:
                            continue
                        setattr(target, field, converted)
                        resolved.add(f'items[{index}].{field}')
                        legacy_index = (target.source_position or index + 1) - 1
                        resolved.add(f'items[{legacy_index}].{field}')
                changed.missing_fields = [field for field in changed.missing_fields if field not in resolved]
                changed.clarification_answers.append(text)
                changed.pending_clarification = next_question(changed)
                validate_request(changed)
                remaining_questions = grouped_questions_for_missing_fields(changed, changed.missing_fields)
                changed.pending_operation = None
                if remaining_questions and changed.pending_clarification is None:
                    changed.pending_operation = PendingOperation(
                        user_id=user_id, request_id=changed.request_id,
                        request_fingerprint=request_fingerprint(changed),
                        operation=RequestOperation(kind='edit_fields'), command=pending.command,
                        question='\n'.join(remaining_questions), expected_answer='details',
                        answers=pending.answers + [text])
                changed.is_valid = not changed.missing_fields and changed.pending_clarification is None
                messages = self._response(changed, remaining_questions)
                self._save(user_id, changed)
                return messages
            if re.search(r'ширин|высот|толщин|длин|диаметр|сторон', text, re.I):
                return [pending.question]
        answers = pending.answers + [text]
        plan = self.ai.interpret_message(
            'Исходная команда: ' + pending.command + '\nУточняющий вопрос: ' + pending.question
            + '\nОтветы пользователя: ' + '\n'.join(answers), current)
        if plan.intent == 'clarify':
            return self._begin_clarification(user_id, current, pending.command, plan, answers=answers)
        if plan.intent == 'edit':
            return self._edit(user_id, current, plan, command=pending.command + '\n' + '\n'.join(answers))
        if plan.intent == 'operation':
            return self._execute_operation(user_id, current, plan.operation, command=pending.command + '\n' + '\n'.join(answers))
        return [pending.question]

    def _edit(self, user_id, current, plan, command='', confirmed=False, clarification=None):
        phase('correction')
        if clarification is not None:
            from dialog_context import pending_detail, reply_values
            expected = pending_detail(current)
            values = reply_values(command,clarification,current)
            if (expected != clarification or not values
                    or (current.pending_operation and current.pending_operation.user_id != user_id)
                    or any(edit.item_id != clarification.item_id or edit.restore for edit in plan.edits)
                    or {edit.field:edit.value for edit in plan.edits} != values):
                raise PipelineError('stale_clarification','Уточнение устарело или не соответствует позиции. Повторите вопрос.')
            # The exact typed question binds one source item; no AI/display-row selection.
        else:
            # Explicit local selectors may intentionally select part of a commercial
            # display group. Re-derive the exact edit set; never trust AI scope here.
            scoped_command = re.search(r'\bво?\s+всех\b|\bтам,?\s+где\s+(?:это\s+)?разрешено\b', command, re.I)
            trusted = local_edit_plan(command, current, self._approved_rules()) if scoped_command else None
            if not (trusted and trusted.intent == 'edit' and trusted.edits == plan.edits):
                plan = scoped_plan(command, current, plan, confirmed=confirmed)
        if plan.intent == 'clarify':
            return self._begin_clarification(user_id, current, command, plan)
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
            if edit.field == 'gost_tu':
                from source_attributes import has_standard_evidence
                if clarification is None or clarification.field != 'gost_tu' or not has_standard_evidence(value,command):
                    raise PipelineError('invalid_standard','ГОСТ можно уточнить только явным ответом на связанный вопрос.')
            if edit.field == 'grade':
                from normalizer import _normalize_grade
                value = _normalize_grade(value)
            if edit.field == 'grade' and item.product_group == 'strip' and item.grade and item.grade in (item.size_designation or ''):
                if value is None:
                    raise PipelineError('compound_grade','Уточните полное обозначение ленты для восстановления марки.')
                item.size_designation = item.size_designation.replace(item.grade, value, 1)
            setattr(item, edit.field, value)
        changed.pending_operation = None
        changed.pending_detail = None
        changed.clarification_cancelled = False
        changed.pending_clarification=next_question(changed)
        changed.is_valid = not changed.missing_fields and changed.pending_clarification is None
        questions=[]
        if clarification is not None:
            changed.detail_answers.append(DetailAnswer(request_id=changed.request_id,item_id=clarification.item_id,
                field=clarification.field,question=clarification.question,answer=command,value=values.get(clarification.field,plan.edits[0].value)))
            changed.clarification_answers.append(command)
            index = next(n for n,item in enumerate(changed.items) if item.item_id==clarification.item_id)
            resolved = {f'items[{index}].{edit.field}' for edit in plan.edits}
            changed.missing_fields = [f for f in changed.missing_fields if f not in resolved]
            self._queue_remaining_field_questions(user_id,changed,clarification.question,[command])
            from dialog_context import pending_detail
            changed.pending_detail = pending_detail(changed)
            changed.is_valid = not changed.missing_fields and not changed.pending_clarification
        messages = self._response(changed, questions)
        changed.dialog_state = 'active'
        self._save(user_id, changed)
        return messages

    def _answer_clarification(self,user_id,current,text):
        phase('clarification')
        pending=current.pending_clarification
        if (pending.request_id != current.request_id
                or (pending.fingerprint and pending.fingerprint != request_fingerprint(current))):
            raise PipelineError('stale_clarification','Уточнение относится к другой заявке. Отправьте новую заявку.')
        if pending.unit_item_ids:
            expected = next_question(current)
            if (pending.field != 'unit' or not pending.fingerprint or expected is None
                    or expected.unit_item_ids != pending.unit_item_ids):
                raise PipelineError('stale_clarification', 'Область уточнения единиц изменилась. Повторите уточнение.')
            values, question = answer_values(pending, text)
            if values is None:
                return [question]
            changed = current.model_copy(deep=True)
            targets = set(pending.unit_item_ids)
            resolved = set()
            for index, item in enumerate(changed.items):
                if item.item_id in targets:
                    item.unit = values['unit']
                    item.source_unit = values['unit']
                    resolved.add(f'items[{index}].unit')
            changed.pending_operation = None
            changed.pending_detail = None
            changed.clarification_cancelled = False
            changed.clarification_answers.append(text)
            changed.missing_fields = [field for field in changed.missing_fields if field not in resolved]
            changed.pending_clarification = next_question(changed)
            self._queue_remaining_field_questions(user_id, changed, pending.question,
                                                  current.clarification_answers + [text])
            changed.is_valid = not changed.missing_fields and changed.pending_clarification is None
            messages = self._response(changed, [])
            self._save(user_id, changed)
            return messages
        bulk = parse_bulk_quantity(text, current)
        if bulk == []:
            return ['Массовое изменение конфликтует с уже заданными количествами или единицами. Ничего не изменено. Уточните позиции или задайте новое количество явно.']
        if bulk:
            changed = current.model_copy(deep=True)
            by_id = {item.item_id: item for item in changed.items}
            for item_id, quantity, unit in bulk:
                target = by_id[item_id]
                target.quantity = quantity
                target.unit = unit
                target.source_unit = unit
            changed.pending_operation = None
            changed.clarification_cancelled = False
            changed.clarification_answers.append(text)
            changed.pending_clarification = next_question(changed)
            changed.missing_fields = [field for field in changed.missing_fields
                                      if not field.endswith(('.quantity', '.unit'))]
            self._queue_remaining_field_questions(user_id, changed, pending.question,
                                                  current.clarification_answers + [text])
            changed.is_valid = not changed.missing_fields and changed.pending_clarification is None
            messages = self._response(changed, [])
            self._save(user_id, changed)
            return messages
        item=next((i for i in current.items if i.item_id==pending.item_id),None)
        if item is None:
            raise PipelineError('stale_clarification','Позиция для уточнения не найдена.')
        values,question=answer_values(pending,text)
        if (pending.field == 'unit' and values and 'quantity' in values and item.quantity is not None
                and Decimal(values['quantity']) != item.quantity):
            return [pending.question+' Количество уже указано; его изменение задайте отдельной командой.']
        if values is None:
            self._save(user_id, current)
            return [question]
        changed=current.model_copy(deep=True)
        if question:
            changed.pending_clarification=PendingClarification(request_id=changed.request_id,item_id=pending.item_id,
                field='quantity',kind='number',question=question,fingerprint=request_fingerprint(changed))
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
        self._queue_remaining_field_questions(user_id, changed, pending.question,
                                              current.clarification_answers + [text])
        changed.is_valid=not changed.missing_fields and changed.pending_clarification is None
        messages=self._response(changed,[])
        self._save(user_id,changed)
        return messages

    @staticmethod
    def _queue_remaining_field_questions(user_id, request, command, answers):
        questions = grouped_questions_for_missing_fields(request, request.missing_fields)
        request.clarification_questions = questions
        request.pending_operation = None
        if questions and request.pending_clarification is None:
            request.pending_operation = PendingOperation(
                user_id=user_id, request_id=request.request_id,
                request_fingerprint=request_fingerprint(request),
                operation=RequestOperation(kind='edit_fields'), command=command,
                question='\n'.join(questions), expected_answer='details', answers=answers)

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
        if request.fallback_status:
            from telegram_operator import fallback_messages
            return fallback_messages(request)
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
