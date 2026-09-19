from normalizer import DISPLAY_PRODUCTS
from schemas import PurchaseRequest, RequestItem
from typing import Optional


def format_number(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    return str(int(value)) if float(value).is_integer() else str(value).replace(".", ",")


def _fallback_size(item: RequestItem) -> Optional[str]:
    group = item.product_group
    if group in {"round_pipe", "rebar", "round_bar"} and item.diameter_mm is not None:
        return f"Ø{format_number(item.diameter_mm)}"
    if group == "profile_pipe" and None not in (item.width_mm, item.height_mm, item.wall_thickness_mm):
        return "×".join(format_number(value) for value in (item.width_mm, item.height_mm, item.wall_thickness_mm))
    if group == "sheet" and None not in (item.thickness_mm, item.width_mm, item.length_mm):
        return "×".join(format_number(value) for value in (item.thickness_mm, item.width_mm, item.length_mm))
    if group == "angle" and None not in (item.side_a_mm, item.side_b_mm, item.thickness_mm):
        return "×".join(format_number(value) for value in (item.side_a_mm, item.side_b_mm, item.thickness_mm))
    if group == "flat_bar" and None not in (item.width_mm, item.thickness_mm):
        return "×".join(format_number(value) for value in (item.width_mm, item.thickness_mm))
    return None


def format_dimensions(item: RequestItem) -> Optional[str]:
    size = item.size_designation or _fallback_size(item)
    if not size:
        return None
    if item.product_group in {"round_pipe", "rebar", "round_bar"} and not size.startswith("Ø"):
        return f"Ø{size}"
    return size


def format_item(item: RequestItem, index: int) -> str:
    product = DISPLAY_PRODUCTS.get(item.product_group, item.product.capitalize() if item.product else "Позиция")
    parts = [product]
    if item.product_group == "rebar" and item.grade:
        parts.append(item.grade)
    if dimensions := format_dimensions(item):
        parts.append(dimensions)
    values = (item.gost_tu, item.processing) if item.product_group == "rebar" else (item.grade, item.gost_tu, item.processing)
    for value in values:
        if value:
            parts.append(value)
    description = " ".join(parts)
    if item.quantity is not None:
        description += f" — {format_number(item.quantity)}"
        if item.unit:
            description += f" {item.unit}"
    return f"{index}. {description}"


def format_supplier_request(request: PurchaseRequest) -> str:
    lines = ["Коллеги, добрый день!", "", "Прошу предоставить цену и наличие на следующие позиции:", ""]
    lines.extend(format_item(item, index) for index, item in enumerate(request.items, start=1))
    if request.city:
        lines.extend(["", f"Город поставки: {request.city}"])
    if request.delivery:
        lines.append(f"Условия поставки: {request.delivery}")
    if request.deadline:
        lines.append(f"Срок поставки: {request.deadline}")
    if request.comment:
        lines.extend(["", request.comment])
    lines.extend(["", "Прошу указать:", "- цену;", "- наличие;", "- срок поставки;", "- город отгрузки;", "- условия оплаты.", "", "Спасибо!"])
    return "\n".join(lines)
