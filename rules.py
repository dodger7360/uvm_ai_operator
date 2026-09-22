from typing import Optional


PRODUCT_GROUPS = {
    "beam",
    "i_beam",
    "channel",
    "angle",
    "profile_pipe",
    "round_pipe",
    "sheet",
    "expanded_metal",
    "rebar",
    "round_bar",
    "flat_bar",
}


# Поля, без которых позицию нельзя корректно понять
# и передать поставщику.
REQUIRED_FIELDS = {
    "beam": ("size_designation",),
    "i_beam": ("size_designation",),
    "channel": ("size_designation",),
    "expanded_metal": ("size_designation",),
    "angle": ("side_a_mm", "side_b_mm", "thickness_mm"),
    "profile_pipe": ("width_mm", "height_mm", "wall_thickness_mm"),
    "round_pipe": ("diameter_mm", "wall_thickness_mm"),
    "sheet": ("thickness_mm", "width_mm", "length_mm"),
    "rebar": ("diameter_mm",),
    "round_bar": ("diameter_mm",),
    "flat_bar": ("width_mm", "thickness_mm"),
}


FIELD_LABELS = {
    "size_designation": "типоразмер",
    "side_a_mm": "сторону A",
    "side_b_mm": "сторону B",
    "thickness_mm": "толщину",
    "width_mm": "ширину",
    "height_mm": "высоту",
    "wall_thickness_mm": "толщину стенки",
    "diameter_mm": "диаметр",
    "length_mm": "длину",
}


# Безопасные нормализации, которые не меняют смысл марки.
GRADE_ALIASES = {
    "гс": "09Г2С",
}


def required_fields(product_group: Optional[str]) -> tuple[str, ...]:
    """Return fields required to understand a product position."""
    if not product_group:
        return ()
    return REQUIRED_FIELDS.get(product_group, ())


def field_label(field: str) -> str:
    """Return a human-readable label for a required field."""
    return FIELD_LABELS.get(field, field)


def normalize_grade_rule(grade: Optional[str]) -> Optional[str]:
    """
    Normalize only explicitly known grade aliases.

    Business substitutions such as 09Г2С -> Ст3
    or Ст3 -> 09Г2С are intentionally not performed.
    """
    if not grade:
        return grade

    normalized = grade.strip()
    return GRADE_ALIASES.get(normalized.lower(), normalized)