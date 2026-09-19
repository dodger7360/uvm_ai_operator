from normalizer import UNIT_ALIASES
from schemas import PurchaseRequest


def _add_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _missing(missing_fields: list[str], questions: list[str], field: str, question: str) -> None:
    _add_unique(missing_fields, field)
    _add_unique(questions, question)


def validate_request(request: PurchaseRequest) -> PurchaseRequest:
    """Adds questions only for data needed to understand a supplier position."""
    missing_fields: list[str] = []
    clarification_questions: list[str] = []
    warnings: list[str] = []

    if not request.items:
        _missing(missing_fields, clarification_questions, "items", "Не найдены товарные позиции.")

    for index, item in enumerate(request.items, start=1):
        position = f"Позиция {index}"
        prefix = f"items[{index - 1}]"
        if not item.product:
            _missing(missing_fields, clarification_questions, f"{prefix}.product", f"{position}: уточните товар.")
        if item.quantity is None:
            _missing(missing_fields, clarification_questions, f"{prefix}.quantity", f"{position}: уточните количество.")
        elif item.quantity <= 0:
            _add_unique(warnings, f"{position}: количество должно быть больше нуля.")
        if not item.unit:
            _missing(missing_fields, clarification_questions, f"{prefix}.unit", f"{position}: уточните единицу измерения.")
        elif item.unit not in set(UNIT_ALIASES.values()):
            _add_unique(warnings, f"{position}: необычная единица измерения — {item.unit}.")

        group = item.product_group
        if group in {"beam", "i_beam", "channel", "expanded_metal"}:
            if not item.size_designation:
                _missing(missing_fields, clarification_questions, f"{prefix}.size_designation", f"{position}: уточните типоразмер.")
        elif group == "angle":
            if None in (item.side_a_mm, item.side_b_mm, item.thickness_mm):
                _missing(missing_fields, clarification_questions, f"{prefix}.dimensions", f"{position}: уточните размер уголка.")
        elif group == "profile_pipe":
            fields = (("width_mm", "ширину сечения"), ("height_mm", "высоту сечения"), ("wall_thickness_mm", "толщину стенки"))
            for field, label in fields:
                if getattr(item, field) is None:
                    _missing(missing_fields, clarification_questions, f"{prefix}.{field}", f"{position}: уточните {label} профильной трубы.")
        elif group == "round_pipe":
            if item.diameter_mm is None:
                _missing(missing_fields, clarification_questions, f"{prefix}.diameter_mm", f"{position}: уточните диаметр трубы.")
            if item.wall_thickness_mm is None:
                _missing(missing_fields, clarification_questions, f"{prefix}.wall_thickness_mm", f"{position}: уточните толщину стенки трубы.")
        elif group == "sheet":
            for field, label in (("thickness_mm", "толщину"), ("width_mm", "ширину"), ("length_mm", "длину")):
                if getattr(item, field) is None:
                    _missing(missing_fields, clarification_questions, f"{prefix}.{field}", f"{position}: уточните {label} листа.")
        elif group in {"rebar", "round_bar"}:
            if item.diameter_mm is None:
                _missing(missing_fields, clarification_questions, f"{prefix}.diameter_mm", f"{position}: уточните диаметр.")
        elif group == "flat_bar":
            for field, label in (("width_mm", "ширину"), ("thickness_mm", "толщину")):
                if getattr(item, field) is None:
                    _missing(missing_fields, clarification_questions, f"{prefix}.{field}", f"{position}: уточните {label} полосы.")

    request.missing_fields = missing_fields
    request.clarification_questions = clarification_questions
    request.warnings = warnings
    return request
