"""Shared, derived consolidation. Source items and their quantities are immutable here."""
from dataclasses import dataclass
import re
from typing import Optional
from schemas import RequestItem
from product_identity import (consolidation_key, commercial_key, commercial_family,
                              commercial_documents_compatible, commercial_source_guard)
from quantity import normalize_quantity, sum_quantities, refresh_mass_calculations, calculate_theoretical_mass


@dataclass(frozen=True)
class ConsolidatedGroup:
    item: RequestItem
    source_indices: tuple[int, ...]
    source_positions: tuple[int, ...]
    source_item_ids: tuple[Optional[str], ...]
    source_items: tuple[RequestItem, ...] = ()
    commercial_decision_id: Optional[str] = None
    applied_rules: tuple[str, ...] = ()


def consolidate_items(items, originals=None, source_positions=None, *, commercial=False, decisions=(), request=None, approved_rules=()):
    """Build a fresh view and prove every source belongs to exactly one group.

    Indices are one-based in the supplied source list. Telegram also supplies
    original input positions and stable IDs; Workbench can edit by source index.
    """
    if originals is not None and len(originals) != len(items):
        raise ValueError('Исходные снимки не соответствуют списку позиций.')
    if source_positions is not None and len(source_positions) != len(items):
        raise ValueError('Исходные номера не соответствуют списку позиций.')
    if originals is None:
        originals = [getattr(item, "original", None) or item for item in items]
    from normalization_policy import project_item
    projected = [project_item(source, approved_rules, original=originals[i] if originals is not None else None)
                 for i, source in enumerate(items)]
    positions = source_positions if source_positions is not None else [getattr(item, 'source_position', None) or i for i, item in enumerate(items, 1)]
    ids = [getattr(item, 'item_id', None) for item in items]
    known_ids = [value for value in ids if value is not None]
    if len(set(positions)) != len(positions) or len(set(known_ids)) != len(known_ids):
        raise ValueError('Повторная ссылка на исходную позицию.')
    groups = []
    by_key = {}
    from commercial import active_decisions
    approvals = active_decisions(request, decisions) if commercial else []
    approved_members = {key: decision for decision in approvals for key in decision.source_item_ids}
    for index, (source, rule_ids) in enumerate(projected, 1):
        item = RequestItem(**source.model_dump(include=set(RequestItem.model_fields)))
        snapshot = RequestItem(**items[index - 1].model_dump(include=set(RequestItem.model_fields)))
        key = None
        if source.quantity is not None and source.unit:
            try:
                item.quantity, item.unit = normalize_quantity(source.quantity, source.unit)
                original = originals[index - 1] if originals is not None else None
                key = (commercial_key(source, original) if commercial else consolidation_key(source, original))
            except ValueError:
                # Validation keeps incomplete/invalid source positions available.
                pass
        group_key = (key, item.unit) if key is not None else None
        position = source_positions[index - 1] if source_positions is not None else (getattr(source, 'source_position', None) or index)
        item_id = getattr(source, 'item_id', None)
        decision = approved_members.get(item_id)
        if decision:
            if rule_ids:
                raise ValueError('Глобальное правило пересекается с решением по заявке; требуется выбор политики.')
            if not decision.allow_unmapped_source and not commercial_source_guard(source):
                raise ValueError('Непредставленные технические признаки требуют отдельного подтверждения.')
            group_key = ('decision', decision.decision_id, item.unit)
        targets = by_key.get(group_key, []) if group_key is not None else []
        target = next((target for target in targets if decision or not commercial
                      or all(commercial_documents_compatible(member, source) for member in groups[target].source_items)), None)
        if target is not None:
            previous = groups[target]
            combined = previous.item.model_copy(deep=True)
            combined.quantity = sum_quantities([previous.item.quantity, item.quantity])
            groups[target] = ConsolidatedGroup(combined, previous.source_indices + (index,),
                previous.source_positions + (position,), previous.source_item_ids + (item_id,),
                previous.source_items + (snapshot,), previous.commercial_decision_id,
                tuple(sorted(set(previous.applied_rules + rule_ids))))
        else:
            if group_key is not None:
                by_key.setdefault(group_key, []).append(len(groups))
            groups.append(ConsolidatedGroup(item, (index,), (position,), (item_id,),
                          (snapshot,), decision.decision_id if decision else None, rule_ids))
    covered = [index for group in groups for index in group.source_indices]
    if sorted(covered) != list(range(1, len(items) + 1)):
        raise ValueError('Нарушена полнота консолидированного представления.')
    validate_consolidation([pair[0] for pair in projected], groups, originals=originals, source_positions=positions,
                           commercial=commercial, approvals=approvals)
    # Calculate from technical material before changing a display label.
    refresh_mass_calculations([group.item for group in groups])
    for group in groups:
        if group.applied_rules and any(calculate_theoretical_mass(member).status != 'estimate' for member in group.source_items):
            from schemas import MassCalculation
            group.item.mass_calculation = MassCalculation(status='unavailable', reason='business_rule_is_not_density_basis',
                input_quantity=group.item.quantity, input_unit=group.item.unit)
    if commercial:
        by_decision = {decision.decision_id: decision for decision in approvals}
        for group in groups:
            if group.item.product_group == 'sheet' and any(
                    calculate_theoretical_mass(member).status != 'estimate' for member in group.source_items):
                from schemas import MassCalculation
                group.item.mass_calculation = MassCalculation(status='unavailable',
                    reason='commercial_group_is_not_a_density_basis', input_quantity=group.item.quantity,
                    input_unit=group.item.unit, source_unit=group.item.source_unit)
            if group.commercial_decision_id:
                decision = by_decision[group.commercial_decision_id]
                representative = next(member for member in request.items if member.item_id == decision.representative_item_id)
                group.item.grade = decision.display_grade
                group.item.gost_tu = representative.gost_tu
            else:
                families = {commercial_family(member.grade) for member in group.source_items}
                grades = {member.grade for member in group.source_items}
                if families == {'ст3'}:
                    group.item.grade = 'ст3'
                elif families == {'С345/С355'} and len(grades) > 1:
                    group.item.grade = 'С355' if any(re.match(r'[СC]355', grade, re.I) for grade in grades) else 'С345'
                elif len(grades) == 1 and re.fullmatch(r'Ст09Г2С(?:-\d+)?', group.item.grade or '', re.I):
                    # Display spelling only. The full literal designation stays
                    # on the source snapshot; no equivalence to С345/С355.
                    group.item.grade = group.item.grade[2:]
        def totals(values):
            amounts = {}
            for value in values:
                if value.quantity is not None and value.unit:
                    try:
                        amount, unit = normalize_quantity(value.quantity, value.unit)
                    except ValueError:
                        continue
                    amounts.setdefault(unit, []).append(amount)
            return {unit: sum_quantities(amounts) for unit, amounts in amounts.items()}
        if totals(items) != totals([group.item for group in groups]):
            raise ValueError('Нарушено сохранение количества в коммерческом представлении.')
    return groups

def validate_consolidation(items, groups, originals=None, source_positions=None, *, commercial=False, approvals=()):
    """Independent boundary check, also usable by future adapters/exporters."""
    covered = [index for group in groups for index in group.source_indices]
    if sorted(covered) != list(range(1, len(items) + 1)):
        raise ValueError('Каждая исходная позиция должна учитываться ровно один раз.')
    for group in groups:
        if not group.source_indices or not (len(group.source_indices) == len(group.source_positions) == len(group.source_item_ids)):
            raise ValueError('Неполные ссылки на исходные позиции.')
        expected_positions = tuple(source_positions[i-1] if source_positions is not None else getattr(items[i-1], 'source_position', None) or i for i in group.source_indices)
        expected_ids = tuple(getattr(items[i-1], 'item_id', None) for i in group.source_indices)
        if group.source_positions != expected_positions or group.source_item_ids != expected_ids:
            raise ValueError('Ссылки результата не соответствуют источникам.')
        first_item = items[group.source_indices[0]-1]
        fields = set(RequestItem.model_fields) - {'quantity', 'unit', 'mass_calculation'}
        if group.item.model_dump(include=fields) != first_item.model_dump(include=fields):
            raise ValueError('Потеря характеристик исходной позиции.')
        amounts = []
        keys = []
        for index in group.source_indices:
            source = items[index - 1]
            try:
                amount, unit = normalize_quantity(source.quantity, source.unit)
            except ValueError:
                if len(group.source_indices) != 1 or group.item.quantity != source.quantity or group.item.unit != source.unit:
                    raise ValueError('Неполная позиция не может быть объединена.')
                continue
            if unit != group.item.unit:
                raise ValueError('Несовместимые единицы в группе.')
            amounts.append(amount)
            keys.append((commercial_key if commercial else consolidation_key)(source, original=originals[index - 1] if originals is not None else None))
        approved = next((decision for decision in approvals if decision.decision_id == group.commercial_decision_id), None)
        if group.commercial_decision_id and (approved is None or set(approved.source_item_ids) != set(group.source_item_ids)):
            raise ValueError('Группа не соответствует согласованному коммерческому решению.')
        if commercial and not approved:
            members = [items[index-1] for index in group.source_indices]
            if any(not commercial_documents_compatible(a,b) for a in members for b in members):
                raise ValueError('Несовместимые документы без согласования.')
        if not approved and len(group.source_indices) > 1 and (None in keys or len(set(keys)) != 1):
            raise ValueError('Несовместимые характеристики в группе.')
        if not approved and keys and keys[0] is not None:
            first = group.source_indices[0] - 1
            original = originals[first] if originals is not None else getattr(items[first], 'original', None) or items[first]
            if (commercial_key if commercial else consolidation_key)(group.item, original=original) != keys[0]:
                raise ValueError('Характеристики результата не соответствуют источникам.')
        if amounts and sum_quantities(amounts) != group.item.quantity:
            raise ValueError('Не сохранено количество исходной потребности.')
