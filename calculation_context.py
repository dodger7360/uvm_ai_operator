"""Temporary persisted calculations never become active procurement requests."""
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4
from types import SimpleNamespace
from typing import Literal
import re
from pydantic import BaseModel, Field
from schemas import RequestItem


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
    if service.assistant is None: return ['AI-помощник не настроен; исходные данные расчёта сохранены.']
    question = 'Продолжи расчёт: '+calculation.source_text+'\nУточнения: '+ '; '.join(calculation.answers)+'. Укажи коэффициент и источник или допущение. Не подменяй оценку фактическим взвешиванием.'
    try:
        result = service.assistant.answer(question,user_id=user_id,current=calculation.model_copy(deep=True),history=[])
    except Exception:
        return ['AI-помощник недоступен. Исходные данные расчёта и рабочая заявка сохранены.']
    service.repository.save_dialog(user_id,question,result.text,'calculation:'+calculation.calculation_id)
    return safe_chunks(result.text)


def start(service,user_id,text):
    previous = service.repository.load_calculation(user_id)
    if previous:
        previous.suspended = True
        service.repository.save_calculation(user_id,previous)
    try:
        # Reuse the strict extraction/validation pipeline as an unsaved snapshot.
        draft = service._build_active(text,text,[])
    except Exception:
        from telegram_operator import safe_chunks
        return ['Не удалось проверить исходные данные расчёта. Рабочая заявка не изменена:']+safe_chunks(text)
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

def resume(service,user_id,text,calculation):
    from dialog_context import geometry_value
    from quantity import QUANTITY_RE
    from clarifications import find_unit
    pending = calculation.pending
    if not pending or calculation.suspended: return None
    if pending.calculation_id != calculation.calculation_id or pending.fingerprint != fingerprint(calculation):
        return ['Уточнение расчёта устарело. Повторите расчётный вопрос.']
    if pending.item_index >= len(calculation.items): return ['Укажите позицию расчёта.']
    target = calculation.items[pending.item_index]
    if pending.field == 'quantity':
        match = QUANTITY_RE.fullmatch(text.strip())
        if not match:
            from dialog_context import GEOMETRY
            if any(geometry_value(text,SimpleNamespace(field=field)) is not None for field in GEOMETRY): return [pending.question]
            return unmatched_reply(text,pending)
        quantity = Decimal(match[1].replace(',','.'))
        if quantity <= 0: return [pending.question]
        target.quantity, target.unit = quantity, find_unit(match[2])
    else:
        value = geometry_value(text,pending)
        if value is None:
            if QUANTITY_RE.fullmatch(text.strip()): return [pending.question]
            return unmatched_reply(text,pending)
        setattr(target,pending.field,float(value))
    calculation.answers.append(text)
    calculation.pending = next_question(calculation)
    service.repository.save_calculation(user_id,calculation)
    return [calculation.pending.question] if calculation.pending else answer(service,user_id,calculation)
