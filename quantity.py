"""Exact conversion of quantities to integer scaled units."""

from decimal import Decimal, InvalidOperation, localcontext
from typing import Tuple
import re

from normalizer import UNIT_ALIASES


SCALES = {"т": 1_000_000, "м": 1_000, "шт": 1}
SQLITE_INTEGER_MAX = 9_223_372_036_854_775_807


# Shared literal quantity matcher: extraction and source identity use the same
# boundaries, so cross-section dimensions cannot become counts.
QUANTITY_RE = re.compile(
    r'(?<![\w.,xхХX×*])([0-9]+(?:[.,][0-9]+)?)\s*'
    r'(пог\.?\s*м\.?|м\s*[²2]|тн|тонн(?:а|ы)?|кг|шт\.?|м\.?|т)(?![\w])',
    re.IGNORECASE)


def normalize_quantity(value, unit, allow_zero=False):
    """Exact display quantity; kg -> tonnes without a fixed SQLite scale.

    Decimal tuple exponent shifting avoids rounding even beyond the active
    decimal context. Source values are never modified.
    """
    if unit is None:
        raise ValueError('Единица измерения обязательна.')
    original = str(unit).strip().lower()
    canonical = UNIT_ALIASES.get(original, original)
    canonical = {'шт.': 'шт', 'пог. м': 'м'}.get(canonical, canonical)
    if canonical not in {'т', 'кг', 'м', 'шт', 'м²'}:
        raise ValueError('Неподдерживаемая единица измерения.')
    try:
        amount = Decimal(str(value).replace(',', '.'))
    except (InvalidOperation, ValueError):
        raise ValueError('Количество должно быть конечным числом.') from None
    if not amount.is_finite() or amount < 0 or (amount == 0 and not allow_zero):
        raise ValueError('Количество должно быть положительным конечным числом.')
    if canonical == 'кг':
        sign, digits, exponent = amount.as_tuple()
        return Decimal((sign, digits, exponent - 3)), 'т'
    return amount, canonical


def sum_quantities(values):
    """Add finite Decimal quantities exactly, including long significands."""
    values = list(values)
    if not values or any(not v.is_finite() for v in values):
        raise ValueError('Для суммы нужны конечные количества.')
    minimum_exponent = min(v.as_tuple().exponent for v in values)
    maximum_adjusted = max(v.adjusted() for v in values)
    with localcontext() as context:
        context.prec = max(1, maximum_adjusted - minimum_exponent + len(str(len(values))) + 2)
        return sum(values, Decimal(0))


def to_scaled_quantity(value, unit, allow_zero=False) -> Tuple[int, str]:
    """Return (integer quantity, canonical unit), converting kg exactly to tonnes."""
    quantity, canonical_unit = normalize_quantity(value, unit, allow_zero=allow_zero)
    if canonical_unit not in SCALES:
        raise ValueError('Неподдерживаемая единица измерения для SQLite масштаба.')
    scale = SCALES[canonical_unit]

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
