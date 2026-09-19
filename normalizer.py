import re
from typing import Optional

from schemas import PurchaseRequest, RequestItem


UNIT_ALIASES = {
    "т": "т", "тн": "т", "тонна": "т", "тонны": "т", "тонн": "т",
    "кг": "кг", "килограмм": "кг", "килограмма": "кг", "килограммов": "кг",
    "шт": "шт", "штука": "шт", "штуки": "шт", "штук": "шт",
    "м": "м", "метр": "м", "метра": "м", "метров": "м",
}

DISPLAY_PRODUCTS = {
    "beam": "Балка", "i_beam": "Двутавр", "channel": "Швеллер",
    "angle": "Уголок", "profile_pipe": "Труба", "round_pipe": "Труба",
    "sheet": "Лист", "expanded_metal": "Лист ПВЛ", "rebar": "Арматура",
    "round_bar": "Круг", "flat_bar": "Полоса",
}

NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value).replace(".", ",")


def _numbers(value: str) -> list[float]:
    return [float(token.replace(",", ".")) for token in NUMBER_RE.findall(value)]


def _normalize_dimensions(value: str) -> str:
    value = re.sub(r"[xхХX*]", "×", value.strip())
    return re.sub(r"\s+", " ", re.sub(r"\s*×\s*", "×", value))


def _normalize_grade(grade: Optional[str]) -> Optional[str]:
    if not grade:
        return grade
    compact = re.sub(r"\s+", "", grade)
    lower = compact.lower()
    if lower.startswith("ст"):
        return "Ст" + compact[2:]
    if lower.startswith("а"):
        return "А" + compact[1:].upper()
    if re.fullmatch(r"\d+[гg].*", lower):
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


def _set_if_missing(item: RequestItem, field: str, value: float) -> None:
    if getattr(item, field) is None:
        setattr(item, field, value)
        if field not in item.interpreted_fields:
            item.interpreted_fields.append(field)


def _normalize_item(item: RequestItem) -> None:
    if item.unit:
        item.source_unit = item.source_unit or item.unit
        item.unit = UNIT_ALIASES.get(item.unit.lower().strip(), item.unit.lower().strip())
    item.grade = _normalize_grade(item.grade)
    group = item.product_group or _detect_product_group(item.product)
    if group == "pipe":
        group = "profile_pipe" if len(_numbers(item.size_designation or "")) >= 3 else "round_pipe"
    item.product_group = group
    if not item.size_designation:
        return

    size = _normalize_dimensions(item.size_designation)
    values = _numbers(size)
    if group in {"beam", "i_beam", "channel"}:
        item.size_designation = size.upper()
    elif group == "expanded_metal":
        item.size_designation = size.upper().removeprefix("ПВЛ-").removeprefix("ПВЛ ")
    elif group == "sheet" and len(values) == 3:
        for field, value in zip(("thickness_mm", "width_mm", "length_mm"), values):
            _set_if_missing(item, field, value)
        item.size_designation = "×".join(_format_number(value) for value in values)
    elif group == "sheet" and len(values) == 1:
        _set_if_missing(item, "thickness_mm", values[0])
        item.size_designation = _format_number(values[0])
    elif group == "angle" and len(values) == 3:
        for field, value in zip(("side_a_mm", "side_b_mm", "thickness_mm"), values):
            _set_if_missing(item, field, value)
        item.size_designation = "×".join(_format_number(value) for value in values)
    elif group == "profile_pipe" and len(values) == 3:
        for field, value in zip(("width_mm", "height_mm", "wall_thickness_mm"), values):
            _set_if_missing(item, field, value)
        item.size_designation = "×".join(_format_number(value) for value in values)
    elif group == "round_pipe" and values:
        _set_if_missing(item, "diameter_mm", values[0])
        if len(values) == 2:
            _set_if_missing(item, "wall_thickness_mm", values[1])
        item.size_designation = "×".join(_format_number(value) for value in values)
    elif group in {"rebar", "round_bar"} and len(values) == 1:
        _set_if_missing(item, "diameter_mm", values[0])
        item.size_designation = _format_number(values[0])
    elif group == "flat_bar" and len(values) == 2:
        _set_if_missing(item, "width_mm", values[0])
        _set_if_missing(item, "thickness_mm", values[1])
        item.size_designation = "×".join(_format_number(value) for value in values)
    else:
        item.size_designation = size


def normalize_request(request: PurchaseRequest) -> PurchaseRequest:
    """Normalizes known spellings without adding facts absent from the request."""
    for item in request.items:
        _normalize_item(item)
    return request
