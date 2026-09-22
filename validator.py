from normalizer import UNIT_ALIASES
from rules import field_label, required_fields
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
            _add_unique(
                warnings,
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
            _add_unique(
                warnings,
                f"{position}: необычная единица измерения — {item.unit}.",
            )

        group = item.product_group

        for field in required_fields(group):
            if getattr(item, field) is None:
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