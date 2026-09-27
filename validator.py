import re

from normalizer import UNIT_ALIASES
from rules import GRADE_ALIASES, PRODUCT_GROUPS, field_label, required_fields
from schemas import PurchaseRequest


def _add_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _missing(
    missing_fields: list[str],
    questions: list[str],
    field: str,
    question: str,
) -> None:
    _add_unique(missing_fields, field)
    _add_unique(questions, question)


_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?(?![\w.])")


_DIMENSION_FIELDS = {
    "diameter_mm",
    "thickness_mm",
    "width_mm",
    "length_mm",
    "height_mm",
    "side_a_mm",
    "side_b_mm",
    "wall_thickness_mm",
}


def _has_compact_source_phrase(value: str, source_text: str) -> bool:
    """Match a complete token or adjacent tokens, ignoring spacing/case only."""
    expected = "".join(_WORD_RE.findall(value.casefold()))
    tokens = [token.casefold() for token in _WORD_RE.findall(source_text)]
    for start in range(len(tokens)):
        combined = ""
        for token in tokens[start:]:
            combined += token
            if combined == expected:
                return True
            if len(combined) >= len(expected):
                break
    return False


def _has_grade_evidence(grade: str, source_text: str) -> bool:
    if _has_compact_source_phrase(grade, source_text):
        return True
    normalized_grade = grade.strip().casefold()
    return any(
        alias.casefold() != normalized_grade
        and canonical.casefold() == normalized_grade
        and _has_compact_source_phrase(alias, source_text)
        for alias, canonical in GRADE_ALIASES.items()
    )


def _has_number_source_evidence(value: float, source_text: str) -> bool:
    expected = float(value)
    return any(
        float(token.replace(",", ".")) == expected
        for token in _NUMBER_RE.findall(source_text)
    )


def _discard_unsubstantiated_fields(request: PurchaseRequest, warnings: list[str]) -> bool:
    """Remove unsupported grade/standard claims and require source fragments."""
    missing_source_evidence = False
    for index, item in enumerate(request.items, start=1):
        source_text = item.source_text
        if not source_text or not source_text.strip():
            missing_source_evidence = True
            _add_unique(
                warnings,
                f"Позиция {index}: нет исходного фрагмента; извлечение нельзя подтвердить.",
            )
            continue
        prefix = f"Позиция {index}:"
        if item.grade and not _has_grade_evidence(item.grade, source_text):
            item.grade = None
            _add_unique(warnings, f"{prefix} марка не подтверждается исходным фрагментом и исключена.")

        if item.gost_tu and not _has_compact_source_phrase(item.gost_tu, source_text):
            item.gost_tu = None
            _add_unique(warnings, f"{prefix} ГОСТ/ТУ не подтверждается исходным фрагментом и исключён.")

        for field in required_fields(item.product_group):
            value = getattr(item, field)
            if (
                field in _DIMENSION_FIELDS
                and value is not None
                and not _has_number_source_evidence(value, source_text)
            ):
                missing_source_evidence = True
                _add_unique(
                    warnings,
                    f"{prefix} значение размера не подтверждается исходным фрагментом.",
                )
                break
    return missing_source_evidence


def validate_request(request: PurchaseRequest) -> PurchaseRequest:
    """Adds questions only for data needed to understand a supplier position."""

    missing_fields: list[str] = []
    clarification_questions: list[str] = []
    warnings: list[str] = []

    missing_source_evidence = _discard_unsubstantiated_fields(request, warnings)

    if not request.items:
        _missing(
            missing_fields,
            clarification_questions,
            "items",
            "Не найдены товарные позиции.",
        )

    for index, item in enumerate(request.items, start=1):
        position = f"Позиция {index}"
        prefix = f"items[{index - 1}]"

        if not item.product:
            _missing(
                missing_fields,
                clarification_questions,
                f"{prefix}.product",
                f"{position}: уточните товар.",
            )

        if item.quantity is None:
            _missing(
                missing_fields,
                clarification_questions,
                f"{prefix}.quantity",
                f"{position}: уточните количество.",
            )
        elif item.quantity <= 0:
            _missing(
                missing_fields,
                clarification_questions,
                f"{prefix}.quantity",
                f"{position}: количество должно быть больше нуля.",
            )

        if not item.unit:
            _missing(
                missing_fields,
                clarification_questions,
                f"{prefix}.unit",
                f"{position}: уточните единицу измерения.",
            )
        elif item.unit not in set(UNIT_ALIASES.values()):
            _missing(
                missing_fields,
                clarification_questions,
                f"{prefix}.unit",
                f"{position}: уточните поддерживаемую единицу измерения.",
            )

        group = item.product_group

        if group not in PRODUCT_GROUPS:
            _missing(
                missing_fields,
                clarification_questions,
                f"{prefix}.product_group",
                f"{position}: уточните тип товара.",
            )

        for field in required_fields(group):
            value = getattr(item, field)
            if value is None or (isinstance(value, str) and not value.strip()):
                label = field_label(field)

                _missing(
                    missing_fields,
                    clarification_questions,
                    f"{prefix}.{field}",
                    f"{position}: уточните {label}.",
                )

    request.missing_fields = missing_fields
    request.clarification_questions = clarification_questions
    request.warnings = warnings
    request.is_valid = not missing_fields and not missing_source_evidence

    return request
