from normalizer import UNIT_ALIASES
from rules import PRODUCT_GROUPS, field_label, required_fields
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


def validate_request(request: PurchaseRequest) -> PurchaseRequest:
    """Adds questions only for data needed to understand a supplier position."""

    missing_fields: list[str] = []
    clarification_questions: list[str] = []
    warnings: list[str] = []

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

    return request
