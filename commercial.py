"""Explicit per-request commercial decisions, never normative steel aliases."""
from uuid import uuid4
from pydantic import BaseModel, Field
from request_operations import request_fingerprint


class CommercialDecision(BaseModel):
    decision_id: str = Field(default_factory=lambda: uuid4().hex)
    request_id: str
    fingerprint: str
    source_item_ids: list[str]
    representative_item_id: str
    display_grade: str
    reason: str
    allow_unmapped_source: bool = False


def approve_group(request, source_item_ids, display_grade, representative_item_id,
                  *, reason, allow_unmapped_source=False):
    """Bind a human decision to explicit members and unchanged request facts.

    The caller supplies selected source IDs; no product size, quantity, row
    number or fixture lookup selects an exception automatically.
    """
    from product_identity import commercial_shape_key, commercial_family
    from quantity import normalize_quantity
    ids = list(source_item_ids)
    by_id = {item.item_id: item for item in request.items}
    if len(ids) < 2 or len(ids) != len(set(ids)) or any(key not in by_id for key in ids):
        raise ValueError('Решение должно содержать разные исходные позиции этой заявки.')
    if representative_item_id not in ids or not reason.strip():
        raise ValueError('Нужны выбранный представитель и основание решения.')
    members = [by_id[key] for key in ids]
    shapes = {commercial_shape_key(item) for item in members}
    units = {normalize_quantity(item.quantity, item.unit)[1] for item in members}
    if None in shapes or len(shapes) != 1 or len(units) != 1:
        raise ValueError('Нельзя объединять несовместимые размеры, серии, исполнения или единицы.')
    allowed = {item.grade for item in members}
    families = {commercial_family(item.grade) for item in members}
    if families == {'ст3'}:
        allowed.add('ст3')
    if families == {'С345/С355'}:
        allowed.update({'С345', 'С355'})
    if display_grade not in allowed:
        raise ValueError('Коммерческое обозначение не подтверждено выбранными позициями.')
    return CommercialDecision(request_id=request.request_id, fingerprint=request_fingerprint(request),
        source_item_ids=ids, representative_item_id=representative_item_id,
        display_grade=display_grade, reason=reason, allow_unmapped_source=allow_unmapped_source)


def active_decisions(request, decisions):
    """Edits invalidate previous approvals; a different request cannot reuse one."""
    if decisions and request is None:
        raise ValueError('Решение требует контекста конкретной заявки.')
    valid, used = [], set()
    for decision in decisions:
        if decision.request_id != request.request_id:
            raise ValueError('Коммерческое решение относится к другой заявке.')
        if decision.fingerprint != request_fingerprint(request):
            continue
        checked = approve_group(request, decision.source_item_ids, decision.display_grade,
            decision.representative_item_id, reason=decision.reason,
            allow_unmapped_source=decision.allow_unmapped_source)
        if used.intersection(checked.source_item_ids):
            raise ValueError('Исходная позиция включена в несколько коммерческих решений.')
        used.update(checked.source_item_ids)
        valid.append(decision)
    return valid
