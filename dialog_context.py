"""Small dialog boundary around the existing request service; no second normalizer."""
from decimal import Decimal
from typing import Literal
import re
from pydantic import BaseModel
from request_operations import request_fingerprint

GEOMETRY = {'length_mm': 'длину изделия', 'wall_thickness_mm': 'толщину стенки',
            'thickness_mm': 'толщину', 'width_mm': 'ширину', 'height_mm': 'высоту',
            'diameter_mm': 'диаметр', 'side_a_mm': 'сторону A', 'side_b_mm': 'сторону B'}
APPEND = re.compile(r'^\s*(?:добавь|дополни)(?:\s+(?:ещ[её]|к|текущей|заявке|заявку))*\s*[:—-]?\s*|^\s*ещ[её]\s+к\s+заявке\s*[:—-]?\s*', re.I)


class DetailQuestion(BaseModel):
    request_id: str
    item_id: str
    field: Literal['length_mm', 'wall_thickness_mm', 'thickness_mm', 'width_mm', 'height_mm', 'diameter_mm', 'side_a_mm', 'side_b_mm', 'grade', 'gost_tu', 'quantity', 'unit']
    question: str
    fingerprint: str
    purpose: Literal['request', 'mass'] = 'request'


class DetailAnswer(BaseModel):
    request_id: str
    item_id: str
    field: str
    question: str
    answer: str
    value_mm: Decimal | None = None
    value: str | None = None


class DialogClassification(BaseModel):
    intent: Literal['general_chat', 'request_question', 'ambiguous']
    question: str | None = None


def mass_request(text):
    from source_fidelity import source_product_rows
    return bool(re.search(r'сколько\s+весит|рассчитай\s+(?:вес|массу)', text, re.I)
                and source_product_rows(text))


def request_reference(text):
    from source_fidelity import PRODUCT_ROW
    # A pronoun alone ("этой осенью", "об этом в покере") is not a request link.
    target = rf'(?:{PRODUCT_ROW.pattern}|сталь\w*|марк\w*|гост\w*|стандарт\w*|позиц\w*|заявк\w*)'
    return bool(re.search(r'текущ\w*\s+заяв|(?:мо[яею]|наша|по)\s+заяв|'
                          rf'\b(?:эт\w*|так\w*|указанн\w*)\s+(?:\w+\s+){{0,2}}{target}|'
                          r'\bэто\s+весит', text, re.I))


def pending_detail(current):
    if current is None or current.clarification_cancelled:
        return None
    operation = current.pending_operation
    if operation and (operation.request_id != current.request_id or operation.request_fingerprint != request_fingerprint(current)):
        return None
    pending = current.pending_detail
    if pending:
        return pending if (pending.request_id == current.request_id and pending.fingerprint == request_fingerprint(current)) else None
    if current.pending_clarification:
        return None  # Existing typed quantity/unit answers own their question.
    operation = current.pending_operation
    if operation and (operation.request_id != current.request_id
                      or operation.request_fingerprint != request_fingerprint(current)):
        return None
    if operation and operation.operation.edits:
        return None  # An existing confirmation must remain a confirmation.
    question = operation.question if operation else '\n'.join(current.clarification_questions)
    fields = question_fields(question)
    candidates = []
    for entry in current.missing_fields:
        m = re.fullmatch(r'items\[(\d+)\]\.([a-z_]+)', entry)
        if (m and m[2] in set(GEOMETRY)|{'grade','gost_tu','quantity','unit'}
                and int(m[1]) < len(current.items) and (not fields or m[2] in fields)):
            candidates.append((current.items[int(m[1])],m[2]))
    if not candidates and len(fields) == 1:
        selected = re.search(r'исходн\w*\s+позиц\w*\s+(\d+)',question,re.I)
        targets = ([item for n,item in enumerate(current.items,1) if (item.source_position or n)==int(selected[1])]
                   if selected else current.items if len(current.items)==1 else [])
        candidates = [(item,next(iter(fields))) for item in targets]
    if len(candidates) != 1: return None
    item,field = candidates[0]
    return DetailQuestion(request_id=current.request_id,item_id=item.item_id,field=field,
        fingerprint=request_fingerprint(current),question=question or f'Уточните параметр исходной позиции {item.source_position or 1}.')


def question_fields(question):
    fields = set()
    for field,pattern in [('grade',r'марк\w*'),('gost_tu',r'гост|стандарт|\bту\b|\bсто\b'),
                          ('quantity',r'количеств|сколько|тоннаж'),('unit',r'единиц'),
                          ('length_mm',r'длин\w*'),('width_mm',r'ширин\w*'),('height_mm',r'высот\w*'),
                          ('diameter_mm',r'диаметр\w*')]:
        if re.search(pattern,question,re.I): fields.add(field)
    if re.search(r'стенк',question,re.I): fields.add('wall_thickness_mm')
    elif re.search(r'толщин',question,re.I): fields.add('thickness_mm')
    return fields


def reply_values(text,pending,current):
    from normalizer import _normalize_grade
    from source_attributes import GRADE_RE, source_standards, has_standard_evidence
    from quantity import QUANTITY_RE
    from clarifications import find_unit
    value = text.strip()
    if pending.field == 'grade':
        item = next((i for i in current.items if i.item_id==pending.item_id),None)
        if item is None: return None
        replacement = re.fullmatch(r'(.+?)\s+вместо\s+(.+)',value,re.I)
        alternative = re.fullmatch(r'допускается\s+(.+)',value,re.I)
        if replacement:
            if (not all(GRADE_RE.fullmatch(replacement[n]) for n in (1,2))
                    or _normalize_grade(item.grade) != _normalize_grade(replacement[2])): return None
            value = replacement[1]
        elif alternative:
            if not item.grade or not GRADE_RE.fullmatch(alternative[1]): return None
            return {'grade':item.grade+' (или '+_normalize_grade(alternative[1])+')'}
        if GRADE_RE.fullmatch(value): return {'grade':_normalize_grade(value)}
    elif pending.field == 'gost_tu':
        standards = source_standards(value)
        if standards and has_standard_evidence(', '.join(standards),value):
            # Do not hide commentary or an extra command inside a standard reply.
            compact = lambda s: re.sub(r'[\s.,;]', '', s).casefold()
            if compact(', '.join(standards)) == compact(value): return {'gost_tu':', '.join(standards)}
    elif pending.field in {'quantity','unit'}:
        if match := QUANTITY_RE.fullmatch(value):
            amount = Decimal(match[1].replace(',','.'))
            if amount > 0: return {'quantity':str(amount),'unit':find_unit(match[2])}
    return None


def answer_bound_value(service,user_id,current,text,pending):
    from telegram_service import MessagePlan
    from request_operations import Edit
    values = reply_values(text,pending,current)
    if not values: return [pending.question]
    plan = MessagePlan(intent='edit',edits=[Edit(item_id=pending.item_id,field=name,value=value) for name,value in values.items()])
    return service._edit(user_id,current,plan,command=text,clarification=pending)


def specific_question(current):
    if pending := pending_detail(current): return pending.question
    if current.pending_operation: return current.pending_operation.question
    if current.pending_clarification: return current.pending_clarification.question
    return 'Какую позицию и какой параметр текущей заявки нужно уточнить?'


def geometry_value(text, pending):
    labels = {'length_mm': r'длин\w*', 'wall_thickness_mm': r'(?:толщин\w*\s+)?стенк\w*',
              'thickness_mm': r'толщин\w*', 'width_mm': r'ширин\w*', 'height_mm': r'высот\w*',
              'diameter_mm': r'диаметр\w*', 'side_a_mm': r'сторон\w*\s*а', 'side_b_mm': r'сторон\w*\s*б'}
    match = re.fullmatch(r'\s*(?:'+labels[pending.field]+r'\s*[:=]?\s*)?(\d+(?:[.,]\d+)?)\s*(мм|см|м)\s*[.!]?\s*',text,re.I)
    if not match: return None
    value = Decimal(match[1].replace(',', '.')) * {'мм':1,'см':10,'м':1000}[match[2].lower()]
    if not value.is_finite() or value <= 0: return None
    converted = float(value)
    if Decimal(str(converted)) != value: return None
    return value


def answer_dialog(service, user_id, text, current, related):
    from telegram_operator import safe_chunks
    if service.assistant is None:
        return ['AI-помощник не настроен. Текущая заявка сохранена.']
    context = current.model_copy(deep=True) if related and current else None
    request_id = context.request_id if context else None
    history = service.repository.load_dialog(user_id, request_id)
    try:
        answer = service.assistant.answer(text, user_id=user_id, current=context, history=history)
    except Exception as error:
        from diagnostics import emit, safe_code
        emit('assistant_failure', reason=safe_code(error))
        return ['AI-помощник недоступен. Текущая заявка и уточнения сохранены.']
    service.repository.save_dialog(user_id, text, answer.text, request_id)
    return safe_chunks(answer.text)


def answer_detail(service, user_id, current, text, pending, value):
    from telegram_service import next_question
    from validator import grouped_questions_for_missing_fields
    from formatter import _fallback_size
    changed = current.model_copy(deep=True)
    target = next((i for i in changed.items if i.item_id == pending.item_id), None)
    if target is None: return [specific_question(current)]
    setattr(target, pending.field, float(value))
    if pending.field != 'length_mm' or target.product_group == 'sheet':
        if size := _fallback_size(target): target.size_designation = size
    changed.detail_answers.append(DetailAnswer(request_id=changed.request_id, item_id=target.item_id,
        field=pending.field, question=pending.question, answer=text, value_mm=value))
    changed.clarification_answers.append(text)
    changed.missing_fields = [f for f in changed.missing_fields
                             if f != f'items[{changed.items.index(target)}].{pending.field}']
    changed.pending_detail = None
    changed.pending_operation = None
    changed.pending_clarification = next_question(changed)
    changed.clarification_questions = grouped_questions_for_missing_fields(changed, changed.missing_fields)
    service._queue_remaining_field_questions(user_id, changed, pending.question, [text])
    changed.pending_detail = pending_detail(changed)
    changed.is_valid = not changed.missing_fields and changed.pending_clarification is None
    service._save(user_id, changed)
    messages = service._response(changed, changed.clarification_questions)
    return messages


def append_request(service, user_id, current, text, body):
    from telegram_service import prepare_sheet_heading, next_question
    parse, headings = prepare_sheet_heading(body)
    addition = service._build_active(body, parse, headings)
    changed = current.model_copy(deep=True)
    offset = max((i.source_position or n for n,i in enumerate(changed.items,1)), default=0)
    for n,item in enumerate(addition.items,1): item.source_position = offset+n
    changed.items += addition.items
    changed.source_text += '\n'+text
    changed.append_commands.append(text)
    changed.missing_fields += [re.sub(r'items\[(\d+)\]',lambda m:f'items[{int(m[1])+len(current.items)}]',f) for f in addition.missing_fields]
    changed.pending_detail = None
    changed.pending_operation = None
    changed.pending_clarification = next_question(changed)
    service._queue_remaining_field_questions(user_id, changed, text, [])
    changed.is_valid = not changed.missing_fields and changed.pending_clarification is None
    messages = service._response(changed, changed.clarification_questions)
    service._save(user_id, changed)
    return messages


def route_dialog(service, user_id, text, current):
    """Return messages if handled; None delegates to the existing request path."""
    from telegram_operator import _is_question, _new_request
    from source_fidelity import source_product_rows
    wording = text.strip().casefold().strip('.!?')
    if current and current.pending_operation and current.pending_operation.user_id != user_id:
        return ['Уточнение относится к другому пользователю. Повторите вопрос.']
    if mass_request(text):
        from calculation_context import start
        return start(service,user_id,text)
    if _is_question(text) or wording in {'привет','здравствуйте','добрый день','как дела','спасибо','благодарю'}:
        return answer_dialog(service,user_id,text,current,request_reference(text))
    calculation = service.repository.load_calculation(user_id)
    from telegram_service import EDIT_WORDS
    work_command = bool(re.search(EDIT_WORDS+r'|\bисправь\b|\bдополни\b',text,re.I))
    if _new_request(text) or work_command or wording in {'уточнение текущей','уточнение текущей заявки'}:
        if calculation and not calculation.suspended:
            calculation.suspended = True
            service.repository.save_calculation(user_id,calculation)
    elif calculation:
        from calculation_context import resume
        if (result := resume(service,user_id,text,calculation)) is not None: return result
    if current:
        if (match := APPEND.match(text)) and source_product_rows(text[match.end():]):
            return append_request(service,user_id,current,text,text[match.end():])
        if _new_request(text): return None  # A complete new list wins over old pending state.
        if wording in {'уточнение','уточнение текущей','уточнение текущей заявки'}:
            return [specific_question(current)]
        if pending := pending_detail(current):
            if pending.field not in GEOMETRY:
                return answer_bound_value(service,user_id,current,text,pending)
            if (value := geometry_value(text,pending)) is not None:
                return answer_detail(service,user_id,current,text,pending,value)
            if re.fullmatch(r'[\d.,\s]+|ну|это|так',wording): return [pending.question]
        if (current.pending_operation and current.pending_operation.expected_answer == 'details'
                and re.fullmatch(r'\d+(?:[.,]\d+)?\s*(?:мм|см|м)?',wording)):
            return [specific_question(current)]
    # Commands, known answers and product input stay in the existing domain path.
    from telegram_service import EDIT_WORDS, DISPLAY_WORDS
    from request_operations import unsupported_operation
    if (unsupported_operation(text) or (re.search(r'тоннаж|тонн|количеств',text,re.I) and re.search(r'формат|виду|знак|отображ|округл',text,re.I))
            or re.match(r'^\s*исправь\b',text,re.I) or text.strip().startswith('/') or re.search(EDIT_WORDS+'|'+DISPLAY_WORDS,text,re.I)
            or source_product_rows(text) or wording in {'да','нет','верно','новая','новую','новую заявку','отмена'}
            or re.fullmatch(r'[\d.,\s]+|(?:т|кг|шт|м)(?:\.)?|\d+(?:[.,]\d+)?\s*(?:т|кг|шт|м)',wording)):
        return None
    if current is None and (re.search(r'издели\w*',text,re.I) or len(text.encode('utf-16-le')) // 2 > 8000):
        return None  # Preserve otherwise uninterpretable original procurement data.
    # Only genuinely unclear prose needs one existing-transport classification.
    if len(text.split()) >= 3 and len(text) <= 8000:
        try:
            decision = service.ai.classify_dialog(text, has_request=current is not None,
                pending_question=specific_question(current) if current else None)
            if decision.intent in {'general_chat','request_question'}:
                return answer_dialog(service,user_id,text,current,decision.intent=='request_question')
        except PermissionError:
            raise
        except Exception:
            pass  # Fail closed: an untrusted classification never edits request state.
    return [specific_question(current)] if current else ['Уточните, пожалуйста, вопрос или пришлите заявку.']
