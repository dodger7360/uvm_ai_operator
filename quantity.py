"""Exact conversion of quantities to integer scaled units."""

from decimal import Decimal, InvalidOperation
from typing import Tuple

from normalizer import UNIT_ALIASES


SCALES = {"т": 1_000_000, "м": 1_000, "шт": 1}
SQLITE_INTEGER_MAX = 9_223_372_036_854_775_807


def to_scaled_quantity(value, unit, allow_zero=False) -> Tuple[int, str]:
    """Return (integer quantity, canonical unit), converting kg exactly to tonnes."""
    if unit is None:
        raise ValueError("Единица измерения обязательна.")
    original_unit = str(unit).strip().lower()
    canonical_unit = UNIT_ALIASES.get(original_unit, original_unit)
    if canonical_unit == "кг":
        canonical_unit = "т"
        scale = 1_000
    elif canonical_unit in SCALES:
        scale = SCALES[canonical_unit]
    else:
        raise ValueError("Неподдерживаемая единица измерения.")

    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Количество должно быть конечным числом.")
    if not quantity.is_finite():
        raise ValueError("Количество должно быть конечным числом.")
    if quantity < 0 or (quantity == 0 and not allow_zero):
        raise ValueError("Количество должно быть положительным.")

    sign, digits, exponent = quantity.as_tuple()
    coefficient = int("".join(str(digit) for digit in digits) or "0")
    if coefficient == 0:
        integer = 0
    else:
        shift = exponent + len(str(scale)) - 1
        if shift >= 0:
            if len(str(coefficient)) + shift > 19:
                raise ValueError("Количество превышает диапазон SQLite INTEGER.")
            integer = coefficient * (10 ** shift)
        else:
            places = -shift
            coefficient_text = str(coefficient)
            if places > len(coefficient_text) or not coefficient_text.endswith("0" * places):
                raise ValueError("Количество имеет точность меньше поддерживаемого масштаба.")
            integer = int(coefficient_text[:-places] or "0")
        if sign:
            integer = -integer
    if integer > SQLITE_INTEGER_MAX:
        raise ValueError("Количество превышает диапазон SQLite INTEGER.")
    return integer, canonical_unit
