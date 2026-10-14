"""Exact quantities, SQLite scaling and deterministic derived mass estimates."""

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


def without_mass_annotation(source):
    """A rendered `pieces = tonnes` suffix is not a second source quantity.

    Keep the raw source unchanged in storage. Only this explicit annotation is
    ignored by quantity extraction and source matching; other facts stay intact.
    """
    return re.sub(
        r'(?P<pieces>(?<![\w.,])\d+(?:[.,]\d+)?\s*шт\.?)'
        r'\s*=\s*\d+(?:[.,]\d+)?\s*(?:тн|тонн(?:а|ы)?|т)(?!\w)',
        lambda match: match['pieces'], source, flags=re.I)


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



def calculate_theoretical_mass(item):
    """Fresh Decimal estimate for ordinary, solid rectangular St3 sheets only.

    Read current geometry/count, never a previous result. This is deliberately
    separate from normalize_quantity: pieces stay pieces, including in storage.
    """
    from schemas import MassCalculation
    from metal_reference.mass_policy import (
        SHEET_ST3_DENSITY_KG_M3, SHEET_ST3_ASSUMPTION, SHEET_ST3_BASIS,
    )
    result = MassCalculation(status='unavailable', input_quantity=item.quantity,
                             input_unit=item.unit, source_unit=item.source_unit)

    def unavailable(reason):
        result.reason = reason
        return result

    if item.product_group != 'sheet':
        return unavailable('unsupported_category')
    if item.grade != 'Ст3':
        return unavailable('unsupported_material')
    names = {'лист', 'лист гк', 'лист г/к', 'лист хк', 'лист х/к',
             'лист горячекатаный', 'лист холоднокатаный'}
    source = ' '.join([item.product or '', item.source_text or '',
                       getattr(item, 'source_context', None) or '', item.processing or '', item.form or ''])
    if ((item.product or '').casefold().strip() not in names
            or item.processing or item.form
            or re.search(r'рифл|пвл|перфор|оцинк|нержав|алюмин|титан|медн|латун|бронз|дюрал|биметалл|композит|профнаст|рулон|отверст|вырез|покрыт|полимер|трапец|клиновид|круглый|овальн|сетк|\bили\b', source, re.I)):
        return unavailable('unsupported_execution')
    if item.quantity is None:
        return unavailable('missing_quantity')
    if not item.quantity.is_finite() or item.quantity <= 0:
        return unavailable('invalid_quantity')
    if UNIT_ALIASES.get((item.unit or '').strip().casefold()) != 'шт' and item.unit != 'шт.':
        return unavailable('unsupported_unit')
    if item.quantity != item.quantity.to_integral_value():
        return unavailable('non_integer_piece_count')
    fields = ('thickness_mm', 'width_mm', 'length_mm')
    if any(getattr(item, field) is None for field in fields):
        return unavailable('missing_geometry')
    dimensions = [Decimal(str(getattr(item, field))) for field in fields]
    if any(not value.is_finite() or value <= 0 for value in dimensions):
        return unavailable('invalid_geometry')
    # Preserve literal precision beyond the legacy float geometry fields.
    # Contradictory current size and structured fields require clarification.
    tokens = re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*[×xхХX*]\s*(\d+(?:[.,]\d+)?)\s*[×xхХX*]\s*(\d+(?:[.,]\d+)?)', item.size_designation or '')
    if item.size_designation and not tokens:
        return unavailable('unsupported_geometry')
    if tokens:
        exact = [Decimal(token.replace(',', '.')) for token in tokens.groups()]
        if any(float(literal) != float(current) for literal, current in zip(exact, dimensions)):
            return unavailable('inconsistent_geometry')
        dimensions = exact
    result.dimensions_mm = dict(zip(fields, dimensions))
    # Multiplication is exact even when the caller's Decimal context is small.
    factors = dimensions + [SHEET_ST3_DENSITY_KG_M3, item.quantity]
    with localcontext() as context:
        context.prec = sum(len(value.as_tuple().digits) for value in factors) + 2
        per_piece_mm3 = dimensions[0] * dimensions[1] * dimensions[2] * SHEET_ST3_DENSITY_KG_M3
        sign, digits, exponent = per_piece_mm3.as_tuple()
        per_piece_kg = Decimal((sign, digits, exponent - 9))
        total_kg = per_piece_kg * item.quantity
    sign, digits, exponent = total_kg.as_tuple()
    return result.model_copy(update={
        'status': 'estimate', 'method': 'solid_rectangular_sheet_volume_density_v1',
        'assumption_id': SHEET_ST3_ASSUMPTION, 'basis': SHEET_ST3_BASIS,
        'density_kg_m3': SHEET_ST3_DENSITY_KG_M3,
        'per_piece_kg': per_piece_kg, 'total_kg': total_kg,
        'total_t': Decimal((sign, digits, exponent - 3)),
    })


def refresh_mass_calculations(items):
    """Overwrite stale or AI-supplied derived data from current source facts."""
    for item in items:
        item.mass_calculation = calculate_theoretical_mass(item) if item.product_group == 'sheet' else None
