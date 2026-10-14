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


def consolidate_items(items, originals=None, source_positions=None, *, commercial=False, decisions=(), request=None):
    """Build a fresh view and prove every source belongs to exactly one group.

    Indices are one-based in the supplied source list. Telegram also supplies
    original input positions and stable IDs; Workbench can edit by source index.
    """
    if originals is not None and len(originals) != len(items):
        raise ValueError('Исходные снимки не соответствуют списку позиций.')
    if source_positions is not None and len(source_positions) != len(items):
        raise ValueError('Исходные номера не соответствуют списку позиций.')
    groups = []
    by_key = {}
    from commercial import active_decisions
    approvals = active_decisions(request, decisions) if commercial else []
    approved_members = {key: decision for decision in approvals for key in decision.source_item_ids}
    for index, source in enumerate(items, 1):
        item = RequestItem(**source.model_dump(include=set(RequestItem.model_fields)))
        snapshot = item.model_copy(deep=True)
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
                previous.source_items + (snapshot,), previous.commercial_decision_id)
        else:
            if group_key is not None:
                by_key.setdefault(group_key, []).append(len(groups))
            groups.append(ConsolidatedGroup(item, (index,), (position,), (item_id,),
                          (snapshot,), decision.decision_id if decision else None))
    covered = [index for group in groups for index in group.source_indices]
    if sorted(covered) != list(range(1, len(items) + 1)):
        raise ValueError('Нарушена полнота консолидированного представления.')
    # Calculate from technical material before changing a display label.
    refresh_mass_calculations([group.item for group in groups])
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
