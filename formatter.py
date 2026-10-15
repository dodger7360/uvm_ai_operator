from normalizer import DISPLAY_PRODUCTS
from schemas import PurchaseRequest, RequestItem
from typing import Optional
from decimal import Decimal, ROUND_HALF_UP, localcontext
import re
from consolidation import consolidate_items


def format_number(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    number = format(Decimal(str(value)), "f")
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    return number.replace(".", ",")


def _fallback_size(item: RequestItem) -> Optional[str]:
    group = item.product_group
    if group in {"round_pipe", "rebar", "round_bar", "fitting"} and item.diameter_mm is not None:
        if group=="round_pipe" and item.wall_thickness_mm is not None:
            return "Ø"+format_number(item.diameter_mm)+"×"+format_number(item.wall_thickness_mm)
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
    if item.product_group in {"round_pipe", "rebar", "round_bar", "fitting"} and not size.startswith("Ø"):
        return f"Ø{size}"
    return size


def format_item(item: RequestItem, index: int, rebar_diameter_first=False, *, mass_calculation=None) -> str:
    product = DISPLAY_PRODUCTS.get(item.product_group, item.product.capitalize() if item.product else "Позиция")
    parts = [product]
    if item.product_group == "rebar" and item.grade and not rebar_diameter_first:
        parts.append(item.grade)
    if dimensions := format_dimensions(item):
        parts.append(dimensions)
    if item.product_group == 'rebar' and item.grade and rebar_diameter_first:
        parts.append(item.grade)
    values = (item.gost_tu, item.processing) if item.product_group == "rebar" else (item.grade, item.form, item.gost_tu, item.processing)
    for value in values:
        if value and not (item.product_group == "strip" and value == item.grade and value in (item.size_designation or "")):
            parts.append(value)
    description = " ".join(parts)
    if item.quantity is not None:
        description += f" — {format_number(item.quantity)}"
        if item.unit:
            description += f" {item.unit}"
    from quantity import calculate_theoretical_mass
    mass = mass_calculation if mass_calculation is not None else calculate_theoretical_mass(item)
    if mass.status == 'estimate':
        if description.endswith(' шт'):
            description += '.'
        with localcontext() as context:
            context.prec = max(28, len(mass.total_t.as_tuple().digits) + max(0, mass.total_t.adjusted()) + 5)
            displayed = mass.total_t.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
        description += ' = ' + format_number(displayed) + ' т'
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


GROUP_ORDER = {name: n for n, name in enumerate([
    "round_pipe", "profile_pipe", "sheet", "expanded_metal", "beam", "i_beam",
    "angle", "channel", "rebar", "round_bar", "flat_bar", "rod", "wire", "strip", "mesh", "fitting"])}


def sort_items(items, *, by_grade=False):
    def key(item):
        numbers = tuple(Decimal(s.replace(',', '.')) for s in re.findall(r'\d+(?:[.,]\d+)?', item.size_designation or ''))
        if item.product_group == 'sheet' and item.thickness_mm is not None:
            numbers = (Decimal(str(item.thickness_mm)),) + numbers
        return GROUP_ORDER.get(item.product_group, 99), numbers, (item.grade or "").casefold() if by_grade else ""
    return sorted(items, key=key)



def ordered_consolidation(items, originals=None, source_positions=None, *, commercial=False, decisions=(), request=None, approved_rules=()):
    """One display order for groups and their provenance in both interfaces."""
    groups = consolidate_items(items, originals=originals, source_positions=source_positions,
                               commercial=commercial, decisions=decisions, request=request, approved_rules=approved_rules)
    groups.sort(key=lambda group: min(group.source_positions))
    by_item = {id(group.item): group for group in groups}
    ordered = [by_item[id(item)] for item in sort_items([group.item for group in groups], by_grade=not commercial)]
    if commercial:
        # Stable size/series order within commercial material blocks.
        ordered.sort(key=lambda group: (GROUP_ORDER.get(group.item.product_group, 99),
                                       0 if group.item.grade == 'ст3' else 1))
    return ordered


def request_consolidation(request, approved_rules=None):
    if approved_rules is None:
        approved_rules = getattr(request, "normalization_rules", ())
    return ordered_consolidation(request.items, commercial=getattr(request, 'commercial_view', False),
        decisions=getattr(request, 'commercial_decisions', ()), request=request, approved_rules=approved_rules)


def format_normalized_request(request, approved_rules=None):
    if approved_rules is None:
        approved_rules = getattr(request, "normalization_rules", ())
    lines = []
    commercial = getattr(request, 'commercial_view', False)
    previous_block = None
    for group in request_consolidation(request, approved_rules=approved_rules):
        item = group.item
        line = format_item(item, 1, getattr(request, 'rebar_diameter_first', False),
                           mass_calculation=item.mass_calculation if commercial or group.applied_rules else None).removeprefix('1. ')
        standard = DISPLAY_PRODUCTS.get(item.product_group, '')
        name = (item.product or '').casefold()
        subtype = None
        if item.product_group == 'sheet':
            if re.search(r'х/?к|холоднокатан', name):
                subtype = 'Лист Х/К'
            elif re.search(r'г/?к|горячекатан', name):
                subtype = 'Лист Г/К'
        elif item.product_group == 'round_pipe':
            if re.search(r'э/?с|электросвар', name):
                subtype = 'Труба э/с'
            elif 'вгп' in name or 'водогаз' in name:
                subtype = 'Труба ВГП'
            elif 'бесшов' in name:
                subtype = 'Труба бесшовная'
        if subtype:
            line = subtype + line[len(standard):]
        places = getattr(request, 'tonnage_decimals', None)
        if not commercial and places is not None and item.unit == 'т' and item.quantity is not None:
            # Quantize only the rendered value; the stored Decimal stays exact.
            displayed = item.quantity.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
            prefix = line.rsplit(' — ', 1)[0]
            line = prefix + ' — ' + format(displayed, f'.{places}f').replace('.', ',') + 'т'
        if commercial:
            line = line.replace('×', 'х')
            if item.unit == 'т' and item.quantity is not None:
                # Explicitly approved commercial operations keep their agreed style.
                # Ordinary quantities are exact; no three-place rounding by default.
                decimals = places if places is not None else 3 if getattr(request,'commercial_decisions',()) else None
                if decimals is not None:
                    displayed=item.quantity.quantize(Decimal(1).scaleb(-decimals),rounding=ROUND_HALF_UP)
                    if displayed != item.quantity:
                        rendered=format_number(item.quantity)
                    else: rendered=format(displayed,f'.{decimals}f').replace('.',',')
                    line=line.rsplit(' — ',1)[0]+' — '+rendered+' т'
            block = (item.product_group, item.grade == 'ст3')
            if previous_block is not None and block != previous_block:
                lines.append('')
            previous_block = block
        if item.unit == 'шт' and line.endswith(' шт'):
            line += '.'
        for requirement in (item.form, item.length_requirement, item.wall_thickness_requirement):
            if requirement and requirement not in line:
                parts = line.split(' — ', 1)
                parts[0] += ' ' + requirement
                line = ' — '.join(parts)
        for condition in item.source_conditions:
            if condition.kind=='substitution' and condition.source_text not in line:
                line += ' ('+condition.source_text+'; границы замены не уточнены)'
        lines.append(line)
    return '\n'.join(lines)
