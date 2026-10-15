"""Temporary persisted calculations never become active procurement requests."""
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4
from types import SimpleNamespace
from typing import Literal
import re
from pydantic import BaseModel, Field
from schemas import RequestItem, MassCalculation


class CalculationQuestion(BaseModel):
    calculation_id: str
    item_index: int = Field(ge=0)
    field: Literal['quantity', 'diameter_mm', 'length_mm']
    question: str
    fingerprint: str


class CalculationState(BaseModel):
    calculation_id: str = Field(default_factory=lambda: uuid4().hex)
    source_text: str
    items: list[RequestItem]
    original_items: list[RequestItem]
    pending: CalculationQuestion | None = None
    answers: list[str] = Field(default_factory=list)
    suspended: bool = False
    result: MassCalculation | None = None


def fingerprint(calculation):
    return sha256(calculation.model_dump_json(include={'calculation_id','source_text','items'}).encode()).hexdigest()


def next_question(calculation):
    from dialog_context import GEOMETRY
    for index,item in enumerate(calculation.items):
        field = None
        if item.quantity is None or item.unit is None: field = 'quantity'
        elif item.product_group == 'rebar' and item.diameter_mm is None: field = 'diameter_mm'
        elif item.product_group == 'rebar' and item.unit == 'шт' and item.length_mm is None: field = 'length_mm'
        if field:
            question = ('Уточните количество и единицу измерения.' if field=='quantity' else
                        'Уточните '+GEOMETRY[field]+' (м или мм).')
            return CalculationQuestion(calculation_id=calculation.calculation_id,item_index=index,field=field,
                question=question,fingerprint=fingerprint(calculation))
    return None


def answer(service,user_id,calculation):
    from telegram_operator import safe_chunks
    from quantity import calculate_rebar_mass
    if len(calculation.items)==1:
        result=calculate_rebar_mass(calculation.items[0])
        if result.status=='estimate':
            from formatter import format_number
            from decimal import ROUND_HALF_UP, localcontext
            calculation.result=result
            service.repository.save_calculation(user_id,calculation)
            with localcontext() as ctx:
                ctx.prec=max(28,len(result.total_t.as_tuple().digits)+max(0,result.total_t.adjusted())+5)
                shown=result.total_t.quantize(Decimal('0.001'),rounding=ROUND_HALF_UP)
            return safe_chunks('Теоретическая масса ≈ '+format_number(shown)+' т. '
                'Без округления: '+format_number(result.total_t)+' т. '
                'Коэффициент '+format_number(result.per_metre_kg)+' кг/м. '+result.basis+
                '\nЭто расчётная оценка, а не фактическая масса партии.')
    return ['Коэффициент массы 1 м для этих характеристик не подтверждён; расчёт не выполнен. '
            'Исходные данные сохранены. Можно явно попросить найти источник в интернете.']



def start(service,user_id,text):
    previous = service.repository.load_calculation(user_id)
    if previous and not previous.suspended and previous.source_text.strip().casefold()==text.strip().casefold():
        return [previous.pending.question] if previous.pending else answer(service,user_id,previous)
    try:
        from telegram_operator import local_request_draft
        from telegram_service import exact_source_attributes
        from normalizer import normalize_request
        from validator import validate_request
        description=re.sub(r'^\s*(?:сколько\s+весит|рассчитай\s+(?:вес|массу))\s*','',text,flags=re.I).rstrip('?')
        draft=local_request_draft(description,calculation=True)
        if draft is None:
            draft=service._build_active(text,text,[])
        else:
            for item in draft.items: exact_source_attributes(item)
            normalize_request(draft)
            checked=validate_request(draft,raw_request=description)
            if checked.warnings: raise ValueError('Unconfirmed calculation attributes')
    except Exception as error:
        from diagnostics import emit, safe_code
        emit('calculation_extraction_failure',reason=safe_code(error),exception=type(error).__name__)
        from telegram_operator import safe_chunks
        return ['Не удалось проверить исходные данные расчёта. Рабочая заявка не изменена:']+safe_chunks(text)
    if previous:
        previous.suspended=True
        service.repository.save_calculation(user_id,previous)
    items = [RequestItem(**item.model_dump()) for item in draft.items]
    calculation = CalculationState(source_text=text,items=items,original_items=[i.model_copy(deep=True) for i in items])
    calculation.pending = next_question(calculation)
    service.repository.save_calculation(user_id,calculation)
    return [calculation.pending.question] if calculation.pending else answer(service,user_id,calculation)



def unmatched_reply(text,pending):
    from source_attributes import GRADE_RE, source_standards
    if (re.fullmatch(r'\s*\d+(?:[.,]\d+)?\s*',text) or GRADE_RE.fullmatch(text.strip())
            or source_standards(text) or re.fullmatch(r'допускается\s+.+|.+\s+вместо\s+.+',text,re.I)):
        return [pending.question+' Для уточнения рабочей заявки напишите «Уточнение текущей».']
    return None

def reply_values(text, calculation):
    """Pure candidate check; choosing a recipient must happen before any write."""
    from dialog_context import geometry_value
    from quantity import QUANTITY_RE
    from clarifications import find_unit
    pending = calculation.pending
    if (not pending or calculation.suspended
            or pending.calculation_id != calculation.calculation_id
            or pending.fingerprint != fingerprint(calculation)
            or pending.item_index >= len(calculation.items)):
        return None
    if pending.field == 'quantity':
        match = QUANTITY_RE.fullmatch(text.strip())
        if match:
            quantity = Decimal(match[1].replace(',', '.'))
            if quantity > 0: return {'quantity': quantity, 'unit': find_unit(match[2])}
    elif (value := geometry_value(text, pending)) is not None:
        return {pending.field: float(value)}
    return None


def resume(service,user_id,text,calculation):
    from dialog_context import GEOMETRY, geometry_value
    from quantity import QUANTITY_RE
    pending = calculation.pending
    if not pending or calculation.suspended: return None
    if pending.calculation_id != calculation.calculation_id or pending.fingerprint != fingerprint(calculation):
        return ['Уточнение расчёта устарело. Повторите расчётный вопрос.']
    if pending.item_index >= len(calculation.items): return ['Укажите позицию расчёта.']
    values = reply_values(text, calculation)
    if values is None:
        if (QUANTITY_RE.fullmatch(text.strip())
                or (pending.field == 'quantity' and any(
                    geometry_value(text, SimpleNamespace(field=field)) is not None for field in GEOMETRY))):
            return [pending.question]
        return unmatched_reply(text,pending)
    target = calculation.items[pending.item_index]
    for field, value in values.items(): setattr(target, field, value)
    calculation.answers.append(text)
    calculation.pending = next_question(calculation)
    service.repository.save_calculation(user_id,calculation)
    return [calculation.pending.question] if calculation.pending else answer(service,user_id,calculation)



def followup(service,user_id,text,calculation):
    """Contextual calculation remarks never become procurement edits."""
    if calculation is None or calculation.suspended: return None
    search=bool(re.search(r'интернет|поищи|найди',text,re.I) and re.search(
        r'коэффициент|\b(?:масс|вес)\w*|^\s*возьми\s+(?:инф\w*|данные)',text,re.I))
    remark=bool(re.search(r'ты\s+у\s+меня\s+спрашиваешь|(?:так\s+)?дай\s+вес|почему.*длин|зачем.*длин',text,re.I))
    if not (search or remark): return None
    explanation=('Для количества в штуках нужна длина одного прутка; её нельзя определить по диаметру '
                 'или найти вместо фактической длины в справочнике. '+calculation.pending.question
                 if calculation.pending and calculation.pending.field=='length_mm' else
                 calculation.pending.question if calculation.pending else 'Исходные данные расчёта сохранены.')
    if search:
        if service.assistant is None: return [explanation+' Интернет-поиск сейчас недоступен; коэффициент без источника не принимается.']
        try:
            result=service.assistant.answer(text,user_id=user_id,current=calculation.model_copy(deep=True),history=[])
        except Exception:
            return [explanation+' Интернет-поиск недоступен; расчёт и заявка сохранены.']
        service.repository.save_dialog(user_id,text,result.text,'calculation:'+calculation.calculation_id)
        return [result.text+'\nНайденная информация не изменяет исходные характеристики или коэффициент автоматически.',explanation]
    if calculation.pending: return [explanation]
    return answer(service,user_id,calculation)
