import re
from decimal import Decimal
from typing import Optional

from rules import normalize_grade_rule
from schemas import PurchaseRequest, RequestItem


UNIT_ALIASES = {
    "т": "т", "тн": "т", "тонна": "т", "тонны": "т", "тонн": "т",
    "кг": "кг", "килограмм": "кг", "килограмма": "кг", "килограммов": "кг",
    "шт": "шт", "штука": "шт", "штуки": "шт", "штук": "шт",
    "м": "м", "метр": "м", "метра": "м", "метров": "м",
    "м.п.": "м", "мп": "м", "пог.м": "м",
    "погонный метр": "м", "погонные метры": "м",
    "пог. м": "пог. м", "м²": "м²", "м2": "м²",
}

DISPLAY_PRODUCTS = {
    "beam": "Балка",
    "i_beam": "Двутавр",
    "channel": "Швеллер",
    "angle": "Уголок",
    "profile_pipe": "Труба профильная",
    "round_pipe": "Труба",
    "sheet": "Лист",
    "expanded_metal": "Лист ПВЛ",
    "rebar": "Арматура",
    "round_bar": "Круг",
    "flat_bar": "Полоса",
    "wire": "Проволока", "strip": "Лента", "mesh": "Сетка",
    "rod": "Пруток",
}

NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_ROUND_SIZE_RE = re.compile(
    r"^(\d+(?:[.,]\d+)?)(?:\s*[xхХX×*]\s*(\d+(?:[.,]\d+)?))?(?=$|\s|[,;])"
)
_EXPLICIT_LENGTH_RE = re.compile(
    r"\bдлина\s*(?:[:=]\s*)?(\d+(?:[.,]\d+)?)\s*(мм|см|м)\b",
    re.IGNORECASE,
)
_LONG_PRODUCT_GROUPS = {
    "beam", "i_beam", "channel", "angle", "profile_pipe", "round_pipe",
    "rebar", "round_bar", "flat_bar",
}


def explicit_equal_angle(item):
    """Only ГОСТ 8509 explicitly identifies an equal-leg angle."""
    return item.product_group == 'angle' and bool(re.search(r'\bГОСТ\s*8509(?:-\d+)?\b', item.source_text or '', re.I))


def _normalize_length(item: RequestItem) -> None:
    """Fill only an explicit single length with a source-provided unit."""
    if item.product_group not in _LONG_PRODUCT_GROUPS or item.length_mm is not None:
        return
    source = item.source_text or ""
    # Multiple length clauses and alternatives require clarification, not selection.
    if len(re.findall(r"\bдлина\b", source, re.IGNORECASE)) != 1:
        return
    match = _EXPLICIT_LENGTH_RE.search(source)
    if match is None or re.match(
        r"\s*(?:[-–/]|или\b|до\b|—\s*\d+(?:[.,]\d+)?\s*(?:мм|см|м)\b)",
        source[match.end():], re.IGNORECASE,
    ):
        return
    value = float(match[1].replace(",", "."))
    if value > 0:
        _set_if_missing(item, "length_mm", value * {"мм": 1, "см": 10, "м": 1000}[match[2].lower()])


def _format_number(value: float) -> str:
    return (
        str(int(value))
        if float(value).is_integer()
        else str(value).replace(".", ",")
    )


def _format_exact_dimension(token):
    number = format(Decimal(token.replace(',', '.')), 'f')
    return number.rstrip('0').rstrip('.') if '.' in number else number


def _numbers(value: str) -> list[float]:
    return [
        float(token.replace(",", "."))
        for token in NUMBER_RE.findall(value)
    ]


def _normalize_dimensions(value: str) -> str:
    value = re.sub(r"[xхХX\*]", "×", value.strip())
    return re.sub(r"\s+", " ", re.sub(r"\s\*×\s\*", "×", value))


def _normalize_grade(grade: Optional[str]) -> Optional[str]:
    if not grade:
        return grade

    alternative = re.fullmatch(r"(.+?)\s*\(\s*или\s+(.+?)\s*\)",grade,re.I)
    if alternative:
        parts=re.split(r"\s+или\s+",alternative[2],flags=re.I)
        return _normalize_grade(alternative[1])+" (или "+" или ".join(_normalize_grade(part) for part in parts)+")"
    from source_attributes import parse_carbon_designation
    designation = parse_carbon_designation(grade)
    if designation:
        return designation.canonical()
    compact = re.sub(r"\s+", "", grade)
    lower = compact.lower()

    if lower.startswith("ст"):
        suffix = compact[2:].lstrip('.')
        if re.match(r'\d{2}[гg]', suffix, re.I):
            suffix = suffix.upper().replace('G', 'Г')
        return "Ст" + suffix

    if lower.startswith("а"):
        return "А" + compact[1:].upper()

    if re.fullmatch(r"\d+[гg].\*", lower):
        return compact.upper().replace("G", "Г")

    return compact


def _detect_product_group(product: Optional[str]) -> Optional[str]:
    name = (product or "").lower().replace("ё", "е")

    if "пвл" in name:
        return "expanded_metal"

    if "арматур" in name:
        return "rebar"

    if "двутавр" in name:
        return "i_beam"

    if "балк" in name:
        return "beam"

    if "швеллер" in name:
        return "channel"

    if "угол" in name:
        return "angle"

    if "полос" in name:
        return "flat_bar"

    if "круг" in name:
        return "round_bar"

    if "лист" in name:
        return "sheet"

    if "труб" in name:
        return "pipe"

    return None


def _set_if_missing(
    item: RequestItem,
    field: str,
    value: float,
) -> None:
    if getattr(item, field) is None:
        setattr(item, field, value)

        if field not in item.interpreted_fields:
            item.interpreted_fields.append(field)


def _normalize_item(item: RequestItem) -> None:
    if item.unit:
        item.source_unit = item.source_unit or item.unit
        item.unit = UNIT_ALIASES.get(
            item.unit.lower().strip(),
            item.unit.lower().strip(),
        )

    item.grade = normalize_grade_rule(
        _normalize_grade(item.grade)
    )

    group = item.product_group or _detect_product_group(item.product)

    if group == "pipe":
        cross_section = re.match(
            r"^\d+(?:[.,]\d+)?(?:\s*[xхХX×*]\s*\d+(?:[.,]\d+)?)*",
            (item.size_designation or "").strip(),
        )
        group = (
            "profile_pipe"
            if cross_section and len(_numbers(cross_section[0])) >= 3
            else "round_pipe"
        )

    item.product_group = group
    _normalize_length(item)

    if not item.size_designation:
        return

    if group == "strip":
        item.size_designation = re.sub(r"^(\d+(?:[.,]\d+)?)\s*[xхХX*×]\s*(\d+(?:[.,]\d+)?)", r"\1×\2", item.size_designation.strip())
        return
    size = _normalize_dimensions(item.size_designation)
    values = _numbers(size)

    if group in {"beam", "i_beam", "channel"}:
        item.size_designation = size.upper()

    elif group == "expanded_metal":
        item.size_designation = (
            size.upper()
            .removeprefix("ПВЛ-")
            .removeprefix("ПВЛ ")
        )
        if re.fullmatch(r'\d+(?:[.,]\d+)?(?:×\d+(?:[.,]\d+)?)*\s*ММ', item.size_designation):
            item.size_designation = re.sub(r'\s*ММ$', '', item.size_designation)

    elif group == "sheet" and len(values) == 3:
        for field, value in zip(
            ("thickness_mm", "width_mm", "length_mm"),
            values,
        ):
            _set_if_missing(item, field, value)

        # Keep literal dimension precision for the Decimal mass calculator.
        if re.search(r'(?:^|[×xхХX*])\s*[-−]', size):
            item.size_designation = size  # Do not erase invalid signed dimensions.
        else:
            item.size_designation = "×".join(_format_exact_dimension(token) for token in NUMBER_RE.findall(size))

    elif group == "sheet" and len(values) == 1:
        _set_if_missing(
            item,
            "thickness_mm",
            values[0],
        )

        item.size_designation = _format_number(values[0])

    elif group == 'angle' and len(values) == 2 and explicit_equal_angle(item):
        for field, value in zip(('side_a_mm', 'side_b_mm', 'thickness_mm'), (values[0], values[0], values[1])):
            _set_if_missing(item, field, value)
        item.size_designation = '×'.join(_format_number(value) for value in values)

    elif group == "angle" and len(values) == 3:
        for field, value in zip(
            ("side_a_mm", "side_b_mm", "thickness_mm"),
            values,
        ):
            _set_if_missing(item, field, value)

        item.size_designation = "×".join(
            _format_number(value)
            for value in values
        )

    elif group == "profile_pipe" and len(values) == 3:
        for field, value in zip(
            ("width_mm", "height_mm", "wall_thickness_mm"),
            values,
        ):
            _set_if_missing(item, field, value)

        item.size_designation = "×".join(
            _format_number(value)
            for value in values
        )

    elif group == "round_pipe":
        # Normalize only the leading cross-section, preserving every text suffix.
        original_size = item.size_designation.strip()
        match = _ROUND_SIZE_RE.match(original_size)
        if match:
            dimensions = [float(token.replace(",", ".")) for token in match.groups() if token is not None]
            _set_if_missing(item, "diameter_mm", dimensions[0])
            if len(dimensions) == 2:
                _set_if_missing(item, "wall_thickness_mm", dimensions[1])
            item.size_designation = "×".join(
                _format_number(value) for value in dimensions
            ) + original_size[match.end():]

    elif group in {"rebar", "round_bar"} and len(values) == 1:
        _set_if_missing(
            item,
            "diameter_mm",
            values[0],
        )

        item.size_designation = _format_number(values[0])

    elif group == "flat_bar" and len(values) == 2:
        _set_if_missing(
            item,
            "width_mm",
            values[0],
        )

        _set_if_missing(
            item,
            "thickness_mm",
            values[1],
        )

        item.size_designation = "×".join(
            _format_number(value)
            for value in values
        )

    else:
        item.size_designation = size


def normalize_request(
    request: PurchaseRequest,
) -> PurchaseRequest:
    """Normalizes known spellings without adding facts absent from the request."""

    for item in request.items:
        _normalize_item(item)

    from quantity import refresh_mass_calculations
    refresh_mass_calculations(request.items)
    return request
