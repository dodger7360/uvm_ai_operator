"""Typed request operations; execution stays in TelegramService.

Only field edits and cancellation are enabled in stage one. Future operations
have explicit kinds, but never execute through an unrelated quantity edit.
"""
from hashlib import sha256
from typing import Literal, Optional
from uuid import uuid4
import re
from pydantic import BaseModel, Field


class Edit(BaseModel):
    item_id: str
    field: Literal['grade', 'quantity', 'unit']
    value: Optional[str] = None
    restore: bool = False


class RequestOperation(BaseModel):
    kind: Literal['edit_fields', 'convert_units', 'calculate_mass', 'merge_items', 'cancel']
    edits: list[Edit] = Field(default_factory=list)
    target_unit: Optional[str] = None


class PendingOperation(BaseModel):
    operation_id: str = Field(default_factory=lambda: uuid4().hex)
    user_id: int
    request_id: str
    request_fingerprint: str
    operation: RequestOperation
    command: str
    question: str
    expected_answer: Literal['confirmation', 'details']
    answers: list[str] = Field(default_factory=list)


def request_fingerprint(request):
    # Include all current/source attributes, not transient dialog state.
    data = request.model_dump_json(include={'request_id', 'items'})
    return sha256(data.encode('utf-8')).hexdigest()


UNSUPPORTED_MESSAGE = ('Эта операция пока не поддерживается: пересчёт единиц и массы '
                       'и объединение позиций. Заявка сохранена без изменений.')


def unsupported_operation(text):
    """Recognize operations before they can be mistaken for clarification answers."""
    if re.search(r'\b(?:суммир\w*|объедин\w*|сложи|сложить|просуммир\w*)\b', text, re.I):
        return 'merge_items'
    if re.search(r'\b(?:перевед\w*|перевест\w*|пересчит\w*|пересчёт|конверт\w*)\b', text, re.I) and re.search(r'тонн|\bт\b|кг|штук|\bшт\b|метр|единиц', text, re.I):
        return 'convert_units'
    if re.search(r'теоретич\w*|масс[ауы]|\bвес\b|кг\s*/\s*м', text, re.I):
        return 'calculate_mass'
    return None
