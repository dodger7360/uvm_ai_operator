"""Shared, derived consolidation. Source items and their quantities are immutable here."""
from dataclasses import dataclass
from typing import Optional
from schemas import RequestItem
from product_identity import consolidation_key
from quantity import normalize_quantity, sum_quantities, refresh_mass_calculations


@dataclass(frozen=True)
class ConsolidatedGroup:
    item: RequestItem
    source_indices: tuple[int, ...]
    source_positions: tuple[int, ...]
    source_item_ids: tuple[Optional[str], ...]
    applied_rules: tuple[str, ...] = ()


def consolidate_items(items, originals=None, source_positions=None, approved_rules=()):
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
    for index, (source, rule_ids) in enumerate(projected, 1):
        item = RequestItem(**source.model_dump(include=set(RequestItem.model_fields)))
        key = None
        if source.quantity is not None and source.unit:
            try:
                item.quantity, item.unit = normalize_quantity(source.quantity, source.unit)
                key = consolidation_key(source, original=originals[index - 1] if originals is not None else None)
            except ValueError:
                # Validation keeps incomplete/invalid source positions available.
                pass
        group_key = (key, item.unit) if key is not None else None
        position = source_positions[index - 1] if source_positions is not None else (getattr(source, 'source_position', None) or index)
        item_id = getattr(source, 'item_id', None)
        if group_key is not None and group_key in by_key:
            target = by_key[group_key]
            previous = groups[target]
            combined = previous.item.model_copy(deep=True)
            combined.quantity = sum_quantities([previous.item.quantity, item.quantity])
            groups[target] = ConsolidatedGroup(combined, previous.source_indices + (index,),
                previous.source_positions + (position,), previous.source_item_ids + (item_id,), tuple(sorted(set(previous.applied_rules + rule_ids))))
        else:
            if group_key is not None:
                by_key[group_key] = len(groups)
            groups.append(ConsolidatedGroup(item, (index,), (position,), (item_id,), rule_ids))
    covered = [index for group in groups for index in group.source_indices]
    if sorted(covered) != list(range(1, len(items) + 1)):
        raise ValueError('Нарушена полнота консолидированного представления.')
    validate_consolidation([pair[0] for pair in projected], groups, originals=originals, source_positions=positions)
    refresh_mass_calculations([group.item for group in groups])
    return groups


def validate_consolidation(items, groups, originals=None, source_positions=None):
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
            keys.append(consolidation_key(source, original=originals[index - 1] if originals is not None else None))
        if len(group.source_indices) > 1 and (None in keys or len(set(keys)) != 1):
            raise ValueError('Несовместимые характеристики в группе.')
        if keys and keys[0] is not None:
            first = group.source_indices[0] - 1
            original = originals[first] if originals is not None else getattr(items[first], 'original', None) or items[first]
            if consolidation_key(group.item, original=original) != keys[0]:
                raise ValueError('Характеристики результата не соответствуют источникам.')
        if amounts and sum_quantities(amounts) != group.item.quantity:
            raise ValueError('Не сохранено количество исходной потребности.')
