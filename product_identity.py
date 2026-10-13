"""Deterministic, versioned Product identity key generation."""

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from rules import PRODUCT_GROUPS, required_fields


IDENTITY_FIELDS = (
    "size_designation",
    "grade",
    "diameter_mm",
    "thickness_mm",
    "width_mm",
    "length_mm",
    "height_mm",
    "side_a_mm",
    "side_b_mm",
    "wall_thickness_mm",
    "form",
    "gost_tu",
    "processing",
)
NUMERIC_FIELDS = {
    "diameter_mm", "thickness_mm", "width_mm", "length_mm",
    "height_mm", "side_a_mm", "side_b_mm", "wall_thickness_mm",
}


def _value(item: Any, field: str) -> Any:
    return item.get(field) if isinstance(item, Mapping) else getattr(item, field, None)


def _canonical_number(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Размер товара должен быть конечным числом.")
    if not number.is_finite():
        raise ValueError("Размер товара должен быть конечным числом.")
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def build_identity_key(item: Any) -> str:
    """Build an identity from normalized product attributes, excluding label/source text."""
    group = _value(item, "product_group")
    if group not in PRODUCT_GROUPS:
        raise ValueError("Для identity_key нужна известная product_group.")

    missing = [
        field for field in required_fields(group)
        if _value(item, field) is None or (
            isinstance(_value(item, field), str) and not _value(item, field).strip()
        )
    ]
    if missing:
        raise ValueError(
            "Нельзя сопоставить товар без обязательных характеристик: "
            + ", ".join(missing)
        )

    values = []
    for field in IDENTITY_FIELDS:
        value = _value(item, field)
        if value is None:
            canonical = None
        elif field in NUMERIC_FIELDS:
            canonical = _canonical_number(value)
        elif isinstance(value, str):
            canonical = value.strip()
        else:
            canonical = str(value)
        values.append(canonical)

    return "v1:" + json.dumps(
        [group, values], ensure_ascii=False, separators=(",", ":")
    )


# This key is a derived-view identity, not a new version of persisted Product v1.
_SOURCE_ONLY = {'quantity', 'unit', 'source_unit', 'source_text', 'interpreted_fields'}
_PRODUCT_ALIASES = {
    'profile_pipe': ('труба профильная', 'труба проф.', 'труба проф', 'труба'),
    'angle': ('уголок', 'угол'),
    'rebar': ('арматура',),
}


def _compact_literal(value):
    from normalizer import _normalize_dimensions
    # Only numeric cross-sections use x/х as separators. A literal X in an
    # unmodelled execution/brand must remain a protected procurement qualifier.
    value = re.sub(r'\d+(?:[.,]\d+)?(?:\s*[xхХX×*]\s*\d+(?:[.,]\d+)?){1,2}',
                   lambda m: _normalize_dimensions(m[0]), value)
    value = re.sub(r'[Øø⌀]\s*(?=\d)', '', value)
    value = re.sub(r'\d+(?:[.,]\d+)?', lambda m: _canonical_number(m[0].replace(',', '.')), value)
    return re.sub(r'[^\w]', '', value.casefold())


def _product_name(item):
    name = re.sub(r'\s+', ' ', (item.product or '').strip().casefold())
    aliases = _PRODUCT_ALIASES.get(item.product_group, ())
    if name in aliases:
        return aliases[0]
    return name


def _unmapped_source(item, original=None):
    """Refuse merging when procurement qualifiers were not represented in fields.

    Only the actual original amount is removed; arbitrary lengths/tolerances
    written with units remain protected. Unknown residue is conservative.
    """
    from quantity import QUANTITY_RE, normalize_quantity
    source = item.source_text or ''
    if not source:
        return True
    original = original or getattr(item, 'original', None) or item
    matches = list(QUANTITY_RE.finditer(source))
    for match in reversed(matches):
        try:
            amount, unit = normalize_quantity(match[1], match[2])
            expected, expected_unit = normalize_quantity(original.quantity, original.unit)
        except ValueError:
            continue
        if amount == expected and unit == expected_unit:
            source = source[:match.start()] + source[match.end():]
            break
    from normalizer import _EXPLICIT_LENGTH_RE
    from validator import _LENGTH_TYPE_PATTERNS
    length_matches = list(_EXPLICIT_LENGTH_RE.finditer(source))
    if len(length_matches) == 1 and original.length_mm is not None:
        length = length_matches[0]
        millimetres = Decimal(length[1].replace(',', '.')) * {'мм': 1, 'см': 10, 'м': 1000}[length[2].casefold()]
        if millimetres == Decimal(str(original.length_mm)):
            source = source[:length.start()] + source[length.end():]
    if original.length_type in _LENGTH_TYPE_PATTERNS:
        source = re.sub(r'(?:\bдлина\s+)?(?:' + _LENGTH_TYPE_PATTERNS[original.length_type] + ')',
                        '', source, flags=re.I)
    # Source headings contain procurement data too. A partly mapped heading
    # prevents a merge rather than silently losing a qualifier.
    source += ' ' + (getattr(item, 'source_context', None) or '')
    source = re.sub(r'\bст\.?\s*(?=[СC]\s*\d)', '', source, flags=re.I)
    residue = _compact_literal(source)
    literals = [item.product or '', original.product or '', item.size_designation or '',
                original.size_designation or '', item.grade or '', original.grade or '',
                item.gost_tu or '', original.gost_tu or '', item.form or '',
                item.processing or '', item.length_requirement or '', item.wall_thickness_requirement or '',
                original.form or '', original.processing or '', original.length_requirement or '',
                original.wall_thickness_requirement or '']
    literals += list(_PRODUCT_ALIASES.get(item.product_group, ()))
    # Literal phrases describe already represented fields; neither inferred
    # standard/density nor all generic numbers are erased from the source.
    for literal in sorted(set(filter(None, map(_compact_literal, literals))), key=len, reverse=True):
        residue = residue.replace(literal, '')
    for opener in ('нужна', 'нужны', 'нужно', 'нужен', 'требуется', 'требуются'):
        if residue.startswith(opener):
            residue = residue[len(opener):]
            break
    return bool(residue)


def consolidation_key(item, original=None):
    """All structured procurement fields plus a conservative source guard."""
    from schemas import RequestItem
    if item.product_group not in PRODUCT_GROUPS or not item.product:
        return None
    if any(getattr(item, name) is None for name in required_fields(item.product_group)):
        return None
    if _unmapped_source(item, original=original):
        return None
    values = []
    for field in RequestItem.model_fields:
        if field in _SOURCE_ONLY:
            continue
        value = getattr(item, field)
        if field == 'product':
            value = _product_name(item)
        elif field in NUMERIC_FIELDS and value is not None:
            value = _canonical_number(value)
            if Decimal(value) <= 0:
                return None
        elif isinstance(value, str):
            value = re.sub(r'\s+', ' ', value.strip()).casefold()
        values.append((field, value))
    return tuple(values)
