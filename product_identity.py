"""Deterministic, versioned Product identity key generation."""

import json
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
