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


def consolidate_items(items, originals=None, source_positions=None):
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
    for index, source in enumerate(items, 1):
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
                previous.source_positions + (position,), previous.source_item_ids + (item_id,))
        else:
            if group_key is not None:
                by_key[group_key] = len(groups)
            groups.append(ConsolidatedGroup(item, (index,), (position,), (item_id,)))
    covered = [index for group in groups for index in group.source_indices]
    if sorted(covered) != list(range(1, len(items) + 1)):
        raise ValueError('Нарушена полнота консолидированного представления.')
    refresh_mass_calculations([group.item for group in groups])
    return groups
