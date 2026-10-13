import re
from decimal import Decimal, InvalidOperation

from normalizer import UNIT_ALIASES, explicit_equal_angle
from rules import GRADE_ALIASES, PRODUCT_GROUPS, field_label, required_fields
from schemas import PurchaseRequest, RequestItem, SharedAttributeEvidence


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

_DIMENSION_SEQUENCE_FIELDS = {
    "profile_pipe": ("width_mm", "height_mm", "wall_thickness_mm"),
    "round_pipe": ("diameter_mm", "wall_thickness_mm"),
    "sheet": ("thickness_mm", "width_mm", "length_mm"),
    "angle": ("side_a_mm", "side_b_mm", "thickness_mm"),
    "flat_bar": ("width_mm", "thickness_mm"),
}
_DIMENSION_SEQUENCE_RE = re.compile(
    r"(?<![\w.,])"
    r"(\d+(?:[.,]\d+)?)\s*[xх×*]\s*"
    r"(\d+(?:[.,]\d+)?)"
    r"(?:\s*[xх×*]\s*(\d+(?:[.,]\d+)?))?"
    r"(?!\w|[.,]\d)",
    re.IGNORECASE,
)
_SHEET_DIMENSION_SEQUENCE_RE = re.compile(
    r"(?<![\w.,])"
    r"(\d+(?:[.,]\d+)?)\s+"
    r"(\d+(?:[.,]\d+)?)\s*[xх×*]\s*"
    r"(\d+(?:[.,]\d+)?)"
    r"(?!\w|[.,]\d)",
    re.IGNORECASE,
)


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


def _has_dimension_sequence_evidence(item: RequestItem, source_text: str) -> bool:
    fields = _DIMENSION_SEQUENCE_FIELDS[item.product_group]
    if item.product_group == "profile_pipe" and item.wall_thickness_mm is None:
        fields = fields[:2]
    expected = tuple(getattr(item, field) for field in fields)
    if all(value is None for value in expected):
        return False

    try:
        expected_values = tuple(
            Decimal(str(value)) if value is not None else None
            for value in expected
        )
        patterns = [_DIMENSION_SEQUENCE_RE]
        if item.product_group == "sheet":
            patterns.append(_SHEET_DIMENSION_SEQUENCE_RE)
        source_sequences = (
            tuple(
                Decimal(token.replace(",", "."))
                for token in match.groups()
                if token is not None
            )
            for pattern in patterns
            for match in pattern.finditer(source_text)
        )
        source_sequences = ((sequence[0], sequence[0], sequence[1])
                            if len(sequence) == 2 and explicit_equal_angle(item) else sequence
                            for sequence in source_sequences)
        return any(
            len(sequence) == len(expected_values)
            and all(
                expected_value is None or expected_value == source_value
                for expected_value, source_value in zip(expected_values, sequence)
            )
            for sequence in source_sequences
        )
    except InvalidOperation:
        return False


def _grade_key(value: str) -> str:
    return "".join(_WORD_RE.findall(value.casefold()))


_LENGTH_TYPE_PATTERNS = {
    "unmeasured": r"\b(?:НД|немерная(?:\s+длина)?)\b",
    "measured": r"\bмерная(?:\s+длина)?\b",
    "other": r"\bкратная\s+длина\b",
}
_LENGTH_VALUE_RE = re.compile(
    r"\bдлина\s*(?:[:=]\s*)?(\d+(?:[.,]\d+)?)\s*(мм|см|м)\b",
    re.IGNORECASE,
)


def _literal_requirement(value: str, source: str) -> bool:
    # Keep numeric punctuation: 11,7 must not match unrelated tokens 11 and 7.
    pattern = r"(?<!\w)" + r"\s+".join(re.escape(token) for token in value.split()) + r"(?!\w)"
    return bool(value.strip() and re.search(pattern, source, re.IGNORECASE))


def _positive_phrase(pattern: str, source: str) -> bool:
    matches = list(re.finditer(pattern, source, re.IGNORECASE))
    return bool(matches) and all(
        not re.search(r"\b(?:не|без)\s*$", source[:match.start()], re.IGNORECASE)
        for match in matches
    )


def _confirmed_minimum_wall(item: RequestItem) -> bool:
    value = item.wall_thickness_requirement
    return bool(
        value
        and re.fullmatch(r"стенка\s+минимально\s+возможная", value.strip(), re.IGNORECASE)
        and _literal_requirement(value, item.source_text or "")
        and _positive_phrase(r"\bстенка\s+минимально\s+возможная\b", item.source_text or "")
    )


def _validate_new_characteristics(
    item: RequestItem, index: int, missing_fields: list[str],
    questions: list[str], warnings: list[str],
) -> None:
    source = item.source_text or ""

    def reject(field: str, label: str) -> None:
        _missing(missing_fields, questions, f"items[{index - 1}].{field}",
                 f"Позиция {index}: уточните {label}.")
        _add_unique(warnings, f"Позиция {index}: {label} не подтверждается исходным фрагментом или противоречит ему.")

    for field, label in (
        ("length_requirement", "требование к длине"),
        ("wall_thickness_requirement", "требование к толщине стенки"),
    ):
        value = getattr(item, field)
        if value is not None:
            pattern = r"(?<!\w)" + r"\s+".join(re.escape(token) for token in value.split()) + r"(?!\w)"
            if not _literal_requirement(value, source) or not _positive_phrase(pattern, source):
                reject(field, label)

    if item.wall_thickness_requirement is not None and not _confirmed_minimum_wall(item):
        reject("wall_thickness_requirement", "требование к толщине стенки")

    if item.length_type is not None:
        pattern = _LENGTH_TYPE_PATTERNS[item.length_type]
        conflicting = any(
            re.search(other_pattern, source, re.IGNORECASE)
            for other_type, other_pattern in _LENGTH_TYPE_PATTERNS.items()
            if other_type != item.length_type
        )
        if not _positive_phrase(pattern, source) or conflicting:
            reject("length_type", "тип длины")

    if item.length_mm is not None and item.product_group != "sheet":
        matches = list(_LENGTH_VALUE_RE.finditer(source))
        confirmed = False
        if len(matches) == 1 and len(re.findall(r"\bдлина\b", source, re.IGNORECASE)) == 1:
            match = matches[0]
            ambiguous = re.match(
                r"\s*(?:[-–/]|или\b|до\b|—\s*\d+(?:[.,]\d+)?\s*(?:мм|см|м)\b)",
                source[match.end():], re.IGNORECASE,
            )
            expected = Decimal(match[1].replace(",", ".")) * {"мм": 1, "см": 10, "м": 1000}[match[2].lower()]
            confirmed = (
                not ambiguous and expected > 0
                and _positive_phrase(re.escape(match[0]), source)
                and Decimal(str(item.length_mm)) == expected
            )
        if not confirmed:
            reject("length_mm", "длину изделия и её единицу измерения")
    elif (
        item.length_mm is None and item.length_requirement
        and re.fullmatch(r"\d+(?:[.,]\d+)?", item.length_requirement.strip())
    ):
        _missing(missing_fields, questions, f"items[{index - 1}].length_mm",
                 f"Позиция {index}: уточните единицу измерения длины изделия.")


def confirm_shared_attributes(
    raw_request: str,
    request: PurchaseRequest,
) -> list[SharedAttributeEvidence]:
    """Confirm only explicit trailing grade context scoped to every item.

    This first implementation intentionally rejects partial, mixed-category, or
    non-trailing scopes rather than guessing which positions a value applies to.
    """
    item_count = len(request.items)
    if not item_count:
        return []

    lines = raw_request.splitlines()
    nonempty_line_indexes = [index for index, line in enumerate(lines) if line.strip()]
    if not nonempty_line_indexes:
        return []
    final_line_index = nonempty_line_indexes[-1]
    before_context = "\n".join(lines[:final_line_index])
    product_groups = {item.product_group for item in request.items}
    if len(product_groups) != 1 or None in product_groups:
        return []

    for item in request.items:
        if not item.source_text or not _has_compact_source_phrase(
            item.source_text, before_context
        ):
            return []

    expected_positions = list(range(1, item_count + 1))
    confirmed: list[SharedAttributeEvidence] = []
    for evidence in request.shared_attributes:
        if evidence.attribute != "grade":
            continue
        if evidence.applies_to != expected_positions:
            continue
        if lines[final_line_index].strip().casefold() != evidence.source_text.strip().casefold():
            continue
        if not _has_grade_evidence(evidence.value, evidence.source_text):
            continue
        if any(
            item.grade
            and _grade_key(item.grade) != _grade_key(evidence.value)
            for item in request.items
        ):
            continue
        confirmed.append(evidence)
    return confirmed


def _has_confirmed_shared_grade(
    request: PurchaseRequest,
    position: int,
    grade: str,
    confirmed_candidates: list[SharedAttributeEvidence],
) -> bool:
    return any(
        evidence in confirmed_candidates
        and evidence.attribute == "grade"
        and position in evidence.applies_to
        and _has_grade_evidence(grade, evidence.value)
        for evidence in request.confirmed_shared_attributes
    )


def _discard_unsubstantiated_fields(
    request: PurchaseRequest,
    warnings: list[str],
    confirmed_candidates: list[SharedAttributeEvidence],
) -> bool:
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
        if (
            item.grade
            and not _has_grade_evidence(item.grade, source_text)
            and not _has_confirmed_shared_grade(
                request, index, item.grade, confirmed_candidates
            )
        ):
            item.grade = None
            _add_unique(warnings, f"{prefix} марка не подтверждается исходным фрагментом и исключена.")

        if item.gost_tu and not _has_compact_source_phrase(item.gost_tu, source_text):
            item.gost_tu = None
            _add_unique(warnings, f"{prefix} ГОСТ/ТУ не подтверждается исходным фрагментом и исключён.")

        sequence_fields = _DIMENSION_SEQUENCE_FIELDS.get(item.product_group)
        has_sequence_dimensions = bool(
            sequence_fields
            and len(sequence_fields) > 1
            and any(getattr(item, field) is not None for field in sequence_fields)
        )
        if has_sequence_dimensions and not _has_dimension_sequence_evidence(item, source_text):
            missing_source_evidence = True
            _add_unique(
                warnings,
                f"{prefix} значение размера не подтверждается исходным фрагментом.",
            )
            continue

        for field in required_fields(item.product_group):
            value = getattr(item, field)
            if (
                field in _DIMENSION_FIELDS
                and value is not None
                and not has_sequence_dimensions
                and not _has_number_source_evidence(value, source_text)
            ):
                missing_source_evidence = True
                _add_unique(
                    warnings,
                    f"{prefix} значение размера не подтверждается исходным фрагментом.",
                )
                break
    return missing_source_evidence


def validate_request(
    request: PurchaseRequest,
    raw_request: str | None = None,
) -> PurchaseRequest:
    """Adds questions only for data needed to understand a supplier position."""

    missing_fields: list[str] = []
    clarification_questions: list[str] = []
    warnings: list[str] = []

    confirmed_candidates = (
        confirm_shared_attributes(raw_request, request)
        if raw_request is not None
        else []
    )
    missing_source_evidence = _discard_unsubstantiated_fields(
        request, warnings, confirmed_candidates
    )

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
        _validate_new_characteristics(
            item, index, missing_fields, clarification_questions, warnings,
        )

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
            if (
                field == "wall_thickness_mm" and group == "profile_pipe"
                and value is None and _confirmed_minimum_wall(item)
            ):
                continue
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
