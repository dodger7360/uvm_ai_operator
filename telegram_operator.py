"""Safe orchestration over TelegramService and shared local recognition.

No second normalizer, SQL schema, reference catalogue or automatic replacements.
The strict domain service remains usable by existing callers; production wiring
explicitly enables this operator mode.
"""
import re
from contextvars import ContextVar
from uuid import uuid4

from diagnostics import CURRENT, emit, phase, safe_code
from nomenclature import SourceRef
from recognition import RecognitionService, RecognitionRecord
from schemas import RequestItem, PurchaseRequest
from source_fidelity import logical_source_rows, source_product_rows

_EXTRACTIONS = ContextVar('operator_extractions', default=None)


def capture_extraction(draft):
    captured = _EXTRACTIONS.get()
    if captured is not None:
        captured.append(draft.model_copy(deep=True))


class ClarificationMessage(str):
    """Trusted presentation type; its content is still fully HTML-escaped."""


def safe_chunks(text, limit=3200):
    """Split even oversized lines, never drop original characters or UTF-16 units."""
    chunks, chars, size = [], [], 0
    for char in text:
        units = 2 if ord(char) > 0xffff else 1
        if chars and size + units > limit:
            chunks.append(''.join(chars))
            chars, size = [], 0
        chars.append(char)
        size += units
    if chars:
        chunks.append(''.join(chars))
    return chunks


def _is_question(text):
    from work_intent import normative_inquiry
    if re.match(r'^\s*(?:исправь|замени|измени|удали|добавь|покажи)\b', text, re.I):
        return False
    return normative_inquiry(text) or bool(re.search(r'^\s*(?:чем\b|что\b|кто\b|как\b|какой\b|какая\b|какие\b|'
                          r'где\b|когда\b|сколько\b|сравни\b|посоветуй\b|почему\b|объясни\b|расскажи\b|найди\b|поищи\b|найти\b|'
                          r'подскажи\b|проверь\s+(?:актуальн|действующ)|можно\s+ли\b)', text, re.I)
                or '?' in text)


def _new_request(text):
    from telegram_service import EDIT_WORDS, DISPLAY_WORDS
    return (not _is_question(text) and not re.search(EDIT_WORDS + '|' + DISPLAY_WORDS + r'|\bисправь\b', text, re.I)
            and (bool(source_product_rows(text)) or bool(re.match(r'^\s*новая\s+заявка\s*:', text, re.I))))


def _questions(messages, current):
    if current is None:
        return messages
    questions = set(current.clarification_questions)
    if current.pending_operation:
        questions.add(current.pending_operation.question)
    if current.pending_clarification:
        questions.add(current.pending_clarification.question)
    return [ClarificationMessage('Вопросы по заявке:\n' + message)
            if message in questions else message for message in messages]


def fallback_messages(request):
    from formatter import format_normalized_request
    messages = []
    if request.items:
        try:
            messages += safe_chunks('Частичная нормализация; требуется проверка:\n'
                                    + format_normalized_request(request))
        except Exception as error:
            emit('fallback_formatting', reason=safe_code(error))
    # The authoritative original includes headings, notes and unknown rows too.
    messages += ['Исходная заявка сохранена без изменений:'] + safe_chunks(request.source_text)
    questions = list(dict.fromkeys(request.clarification_questions))
    if request.unprocessed_source:
        excerpts = [' '.join(row.split())[:90] for row in request.unprocessed_source[:5]]
        names = '; '.join('«' + excerpt + '»' for excerpt in excerpts)
        rest = len(request.unprocessed_source) - len(excerpts)
        questions.append('Уточните наименование и характеристики нераспознанных строк: '
                         + names + (f'; ещё {rest} строк в исходной заявке.' if rest else '.'))
    if questions:
        messages += [ClarificationMessage('Вопросы по заявке:\n' + '\n'.join(questions))]
    else:
        messages.append('Полную нормализацию подтвердить не удалось. Исходный текст доступен; повторите обработку позже.')
    return messages


def _local_item(result):
    spec = result.spec
    if spec is None or result.status in {'unsupported', 'empty', 'ambiguous'}:
        return None
    # Unknown execution/comments must remain visible in the original, not be
    # silently represented as checked attributes by the legacy RequestItem.
    def uncertain(value):
        if isinstance(value,dict):
            return value.get('state') in {'ambiguous','conflict'} or any(uncertain(v) for v in value.values())
        if isinstance(value,list): return any(uncertain(v) for v in value)
        return False
    if spec.unparsed_features or uncertain(spec.model_dump()):
        return None
    group = spec.category.value
    from formatter import DISPLAY_PRODUCTS
    item = RequestItem(product=(spec.product_type.value if group=='fitting' else 'Труба э/с' if group=='round_pipe' and spec.execution.manufacturing_method.value=='electric_welded' else DISPLAY_PRODUCTS[group]), product_group=group,
                       source_text=spec.original_description)
    if spec.profile.designation.is_resolved:
        item.size_designation = spec.profile.designation.normalized_text or spec.profile.designation.value
    if spec.material.designation.is_resolved:
        item.grade = spec.material.designation.normalized_text or spec.material.designation.value
    if spec.quantity.is_resolved:
        item.quantity, item.unit = spec.quantity.value.amount, spec.quantity.value.unit
    mapping = {'leg_a': 'side_a_mm', 'leg_b': 'side_b_mm', 'diameter': 'diameter_mm',
               'thickness': 'thickness_mm', 'width': 'width_mm', 'height': 'height_mm',
               'length': 'length_mm', 'wall_thickness': 'wall_thickness_mm'}
    for name, attr in spec.geometry.items():
        if name in mapping and attr.is_resolved and attr.value.unit == 'mm':
            # Legacy RequestItem geometry is float; the exact Decimal and all
            # evidence remain separately in recovery_specs (NomenclatureSpec).
            setattr(item, mapping[name], float(attr.value.amount))
    if spec.execution.coating.is_resolved:
        item.processing=spec.execution.coating.raw_value
    if group=='round_pipe' and spec.execution.manufacturing_method.value=='seamless':
        item.product='Труба бесшовная'
    elif group!='round_pipe' and spec.execution.manufacturing_method.is_resolved:
        item.processing=' '.join(filter(None,[item.processing,spec.execution.manufacturing_method.raw_value]))
    from schemas import SourceCondition
    for name in ('wall_selection','substitution','related_pipe'):
        attr=spec.extensions.get(name)
        if attr and attr.is_resolved:
            item.source_conditions.append(SourceCondition(kind=name,source_text=attr.raw_value or attr.value,
                bounds_unspecified=name in {'wall_selection','substitution'}))
            if name=='wall_selection': item.wall_thickness_requirement=attr.raw_value or attr.value
    return item


def _needs_local_projection(row):
    from quantity import length_mass_columns
    return bool(length_mass_columns(row, 'angle') or length_mass_columns(row, 'channel')
                or re.search(r'э[\\/]с|электросвар|\b(?:отвод|фланец)\b', row, re.I))


def restore_local_source_attributes(item):
    """Use the same faithful projection on AI and recovery paths, per source row.

    Unknown/conflicting execution must remain visible in partial recovery, rather
    than making an incomplete AI extraction appear fully checked.
    """
    if not _needs_local_projection(item.source_text or ''):
        return
    draft = local_request_draft(item.source_text)
    if draft is None or len(draft.items) != 1:
        from diagnostics import SemanticExtractionError
        raise SemanticExtractionError('source_projection_incomplete',
                                      'Не все характеристики строки подтверждены; исходная строка сохранена.')
    literal = draft.items[0]
    for field in ('product', 'product_group', 'size_designation', 'diameter_mm',
                  'thickness_mm', 'width_mm', 'height_mm', 'length_mm', 'side_a_mm',
                  'side_b_mm', 'wall_thickness_mm', 'processing',
                  'wall_thickness_requirement', 'source_conditions'):
        setattr(item, field, getattr(literal, field))


def local_request_draft(text, *, calculation=False):
    """Use the shared recognizer only when every row has a faithful projection."""
    rows=source_product_rows(text)
    if not rows: return None
    if not calculation and not any(_needs_local_projection(row) for row in rows):
        return None
    items=[]
    for position,row in enumerate(rows,1):
        result=RecognitionService().recognize_record(RecognitionRecord(record_id=str(position),
            source=SourceRef(source_id='local-input',source_type='telegram'),original_description=row))
        item=_local_item(result)
        if item is None: return None
        items.append(item)
    bind_pipe_relations(items)
    return PurchaseRequest(items=items)


def bind_pipe_relations(items):
    """Request-scoped provenance links, never a claim of fitting compatibility."""
    from schemas import SourceCondition
    for index,item in enumerate(items,1):
        if item.product_group!='fitting' or item.diameter_mm is None: continue
        position=getattr(item,'source_position',None) or index
        targets=[getattr(prior,'source_position',None) or n for n,prior in enumerate(items,1)
                 if (getattr(prior,'source_position',None) or n)<position
                 and prior.product_group=='round_pipe' and prior.diameter_mm==item.diameter_mm]
        links=[c for c in item.source_conditions if c.kind=='related_pipe']
        if links:
            for link in links: link.related_source_position=targets[0] if len(targets)==1 else None
        elif len(targets)==1:
            item.source_conditions.append(SourceCondition(kind='related_pipe',origin='context',
                source_text=item.source_text or '',related_source_position=targets[0]))
        if hasattr(item,'original'):
            item.original.source_conditions=[c.model_copy(deep=True) for c in item.source_conditions]


def _recover(service, user_id, text):
    from telegram_service import ActiveRequest, prepare_sheet_heading
    recovered = ActiveRequest(request_id=uuid4().hex, source_text=text,
                              fallback_status='original', is_valid=False)
    extractions = list(_EXTRACTIONS.get() or [])
    consumed = set()
    source_count = None
    try:
        prepared, heading_records = prepare_sheet_heading(text)
        rows = source_product_rows(prepared)
        if not rows:
            rows = logical_source_rows(text)
        source_count = len(rows)
        for position, row in enumerate(rows, 1):
            try:
                # First salvage real extracted rows through exactly the same
                # source coverage/attribute/quantity checks, independently.
                checked = None
                slots = [(d, i, item) for d, draft in enumerate(extractions)
                         for i, item in enumerate(draft.items)
                         if (d, i) not in consumed and item.source_text == row]
                candidates = [item for _, _, item in slots]
                variants = {item.model_dump_json() for item in candidates}
                if len(variants) == 1:
                    try:
                        checked = service._build_active(row, row, [],
                            draft=PurchaseRequest(items=[candidates[0].model_copy(deep=True)]))
                        from rules import required_fields
                        from validator import _has_compact_source_phrase, _has_number_source_evidence
                        from product_identity import NUMERIC_FIELDS
                        literal = checked.items[0]
                        if any(getattr(literal, field) is not None
                               and not _has_number_source_evidence(getattr(literal, field), row)
                               for field in NUMERIC_FIELDS - {'length_mm'}):
                            raise ValueError('Unconfirmed recovered geometry')
                        if any(getattr(literal, field)
                               and not _has_compact_source_phrase(getattr(literal, field), row)
                               for field in ('form', 'processing')):
                            raise ValueError('Unconfirmed recovered execution')
                        if ('size_designation' in required_fields(checked.items[0].product_group)
                                and checked.items[0].size_designation
                                and not _has_compact_source_phrase(checked.items[0].size_designation, row)):
                            raise ValueError('Unconfirmed recovered profile')
                        if checked.warnings or (candidates[0].quantity is not None
                                                and checked.items[0].quantity is None):
                            raise ValueError('Unconfirmed recovered attributes')
                        consumed.add(slots[0][:2])
                    except Exception as error:
                        checked = None
                        emit('extracted_row_recovery', reason=safe_code(error))
                result = RecognitionService().recognize_record(RecognitionRecord(
                    record_id=str(position), source=SourceRef(source_id=recovered.request_id,
                                                             source_type='telegram'),
                    original_description=row))
                if result.spec is not None:
                    recovered.recovery_specs.append(result.spec)
                if checked is None:
                    item = _local_item(result)
                    if item is None:
                        recovered.unprocessed_source.append(row)
                        continue
                    checked = service._build_active(row, row, [], draft=PurchaseRequest(items=[item]))
                record = next((record for record in heading_records if record[0] == row), None)
                if record:
                    checked.items[0].source_text = record[1]
                    checked.items[0].original.source_text = record[1]
                    checked.items[0].source_context = record[2]
                checked.items[0].source_position = position
                recovered.items.extend(checked.items)
                recovered.clarification_questions.extend(checked.clarification_questions)
            except Exception as error:
                emit('local_recovery_row', reason=safe_code(error))
                recovered.unprocessed_source.append(row)
        bind_pipe_relations(recovered.items)
        recovered.fallback_status = 'partial' if recovered.items else 'original'
    except Exception as error:
        emit('local_recovery', reason=safe_code(error))
        recovered.items = []
        recovered.fallback_status = 'original'
    try:
        service._save(user_id, recovered)
    except PermissionError:
        raise
    except Exception as error:
        emit('fallback_save', reason=safe_code(error))
        return ['Не удалось сохранить новую заявку в базе; предыдущая не заменена. '
                'Сохраните этот исходный текст и сообщите администратору:'] + safe_chunks(text)
    phase('fallback', source_count=source_count, extracted_count=len(recovered.items))
    if trace := CURRENT.get():
        trace.result = recovered.fallback_status
    emit('operator_result', status=recovered.fallback_status, source_count=source_count,
         checked_count=len(recovered.items), unresolved_count=len(recovered.unprocessed_source))
    return fallback_messages(recovered)


def process_operator(service, user_id, text, *, commercial_decisions=None):
    token = _EXTRACTIONS.set([])
    try:
        return _process_operator(service, user_id, text, commercial_decisions=commercial_decisions)
    except PermissionError:
        raise
    except Exception as error:
        # Includes an unavailable repository before routing. Do not claim that
        # anything was saved or infer which existing context should be replaced.
        emit('operator_unavailable', reason=safe_code(error))
        return ['Обработка или сохранение сейчас недоступны. Сохраните исходный текст сообщения; '
                'результат не подтверждён:'] + safe_chunks(text)
    finally:
        _EXTRACTIONS.reset(token)


def _process_operator(service, user_id, text, *, commercial_decisions=None):
    current = service.repository.load(user_id)
    from dialog_context import route_dialog
    handled = route_dialog(service, user_id, text, current)
    if handled is not None:
        return _questions(handled, current)
    from telegram_service import EDIT_WORDS, DISPLAY_WORDS
    from request_operations import unsupported_operation
    from clarifications import parse_bulk_quantity
    new = _new_request(text) or (current is None and not text.strip().startswith('/')
                               and not re.search(EDIT_WORDS + '|' + DISPLAY_WORDS, text, re.I))
    legacy_command = (bool(unsupported_operation(text))
                      or bool(re.search(r'тоннаж|тонн|количеств', text, re.I)
                              and re.search(r'формат|виду|знак|отображ|округл', text, re.I))
                      or (current is not None and parse_bulk_quantity(text, current) is not None)
                      or text.strip().casefold() in {'/cancel', 'отмена', 'отмени операцию',
                                                   'отмени последнее изменение'})
    # Unknown prose in an active context must not become a new request via AI.
    if (current and not new and not legacy_command
            and not current.pending_operation and not current.pending_clarification
            and not re.search(EDIT_WORDS + '|' + DISPLAY_WORDS + r'|\bисправь\b', text, re.I)
            and text.strip().casefold() not in {'/show', 'текущая заявка', 'новая', 'новую', 'новую заявку', 'да', 'нет'}):
        return ['Это новая заявка или уточнение текущей? Текущая заявка сохранена.']
    try:
        messages = service._process(user_id, text, commercial_decisions=commercial_decisions, force_new=new)
        # Preserve otherwise ignored free-form rows and comments visibly. They
        # are not asserted to be products, and never contribute to arithmetic.
        if new:
            active = service.repository.load(user_id)
            if active is None or active.source_text != text:
                return _recover(service, user_id, text)
            if active:
                represented = '\n'.join((i.source_context or '') + '\n' + (i.source_text or '') for i in active.items)
                omitted = [line for line in logical_source_rows(text) if line.strip()
                           and line.strip() not in represented]
                if omitted:
                    active.fallback_status, active.unprocessed_source = 'partial', omitted
                    active.is_valid = False
                    service._save(user_id, active)
                    return fallback_messages(active)
        return _questions(messages, service.repository.load(user_id))
    except PermissionError:
        raise
    except Exception as error:
        emit('operator_recovery', reason=safe_code(error), new_request=new)
        if new:
            return _recover(service, user_id, text)
        if current:
            try:
                messages = service._response(current, [])
            except Exception as formatting_error:
                emit('current_request_formatting', reason=safe_code(formatting_error))
                messages = ['Исходная сохранённая заявка:'] + safe_chunks(current.source_text)
            return messages + ['Изменение не подтверждено; сохранённая заявка показана выше. '
                               'Укажите позицию и исправляемое поле.']
        return ['Не удалось обработать сообщение. Исходный текст:'] + safe_chunks(text)
