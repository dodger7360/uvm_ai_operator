"""Application orchestration for the first Normalization Workbench pass."""

from dataclasses import dataclass

from ai import AIService
from formatter import format_item, ordered_consolidation
from source_fidelity import validate_source_coverage
from normalizer import normalize_request
from schemas import PurchaseRequest
from validator import confirm_shared_attributes, validate_request


@dataclass(frozen=True)
class WorkbenchResult:
    """Raw input and the current normalized request produced from it."""

    raw_request: str
    current_request: PurchaseRequest
    original_items: tuple = ()
    source_positions: tuple = ()
    approved_rules: tuple = ()

    @property
    def consolidated_groups(self):
        """Recompute after source edits; never cache a sum as a source quantity."""
        from quantity import refresh_mass_calculations
        refresh_mass_calculations(self.current_request.items)
        return ordered_consolidation(self.current_request.items, originals=self.original_items or None, source_positions=self.source_positions or None, approved_rules=self.approved_rules)

    @property
    def questions(self) -> tuple[str, ...]:
        """Clarification questions from validation, without copying them into state."""
        return tuple(self.current_request.clarification_questions)

    @property
    def is_valid(self) -> bool:
        """Whether the current request passed Python validation."""
        return self.current_request.is_valid


class NormalizationWorkbench:
    """Run extraction, deterministic normalization, and validation for a request."""

    def __init__(self, ai_service: AIService | None = None, policy_repository=None) -> None:
        self._ai_service = ai_service or AIService()
        self.policy_repository = policy_repository

    def process(self, raw_request: str) -> WorkbenchResult:
        """Return the complete current request state and its clarification questions."""
        current_request = self._ai_service.parse_request(raw_request)
        source_positions = validate_source_coverage(current_request, raw_request, allow_heading_rows=True)
        # Recover literal attributes for every category, as Telegram does.
        from telegram_service import exact_source_attributes
        for item in current_request.items:
            exact_source_attributes(item)
        normalize_request(current_request)
        confirmed = confirm_shared_attributes(raw_request, current_request)
        current_request.confirmed_shared_attributes = confirmed
        for evidence in confirmed:
            for position in evidence.applies_to:
                item = current_request.items[position - 1]
                if item.grade is None:
                    item.grade = evidence.value
                    if "grade" not in item.interpreted_fields:
                        item.interpreted_fields.append("grade")
        if confirmed:
            normalize_request(current_request)

        validate_request(current_request, raw_request=raw_request)
        for evidence in current_request.shared_attributes:
            if evidence not in confirmed:
                question = (
                    f"Общий атрибут «{evidence.value}» не применён: "
                    "уточните его значение или область применения."
                )
                if question not in current_request.clarification_questions:
                    current_request.clarification_questions.append(question)
                if "shared_attributes" not in current_request.missing_fields:
                    current_request.missing_fields.append("shared_attributes")
                current_request.is_valid = False
        return WorkbenchResult(
            approved_rules=self.policy_repository.active_rules() if self.policy_repository else (),
            raw_request=raw_request,
            current_request=current_request,
            original_items=tuple(item.model_copy(deep=True) for item in current_request.items),
            source_positions=tuple(source_positions or range(1, len(current_request.items) + 1)),
        )


def format_workbench_result(result: WorkbenchResult) -> str:
    """Render the current request using the project's existing item formatter."""
    lines = ["Нормализованная заявка:"]
    if result.current_request.items:
        lines.extend(
            format_item(group.item, index, mass_calculation=group.item.mass_calculation if group.applied_rules else None)
            for index, group in enumerate(result.consolidated_groups, start=1)
        )
    else:
        lines.append("Товарные позиции не найдены.")

    lines.extend([
        "",
        "Проверка: " + ("пройдена" if result.is_valid else "нужны уточнения"),
        "Вопросы/уточнения:",
    ])
    if result.questions:
        lines.extend(f"- {question}" for question in result.questions)
    else:
        lines.append("- Нет")

    if result.current_request.warnings:
        lines.extend(["", "Предупреждения:"])
        lines.extend(f"- {warning}" for warning in result.current_request.warnings)

    return "\n".join(lines)


def main(
    *,
    workbench: NormalizationWorkbench | None = None,
    input_fn=None,
    output_fn=None,
) -> int:
    """Read a multiline request, run W1, and print its normalized state."""
    reader = input if input_fn is None else input_fn
    writer = print if output_fn is None else output_fn

    writer("Введите заявку; завершите ввод отдельной строкой /done.")
    lines: list[str] = []
    try:
        while True:
            try:
                line = reader("> ")
            except EOFError:
                break
            if line.strip() == "/done":
                break
            lines.append(line)
    except KeyboardInterrupt:
        writer("\nВвод прерван.")
        return 0

    raw_request = "\n".join(lines)
    if not raw_request.strip():
        writer("Заявка не введена.")
        return 0

    processor = workbench or NormalizationWorkbench()
    result = processor.process(raw_request)
    writer(format_workbench_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
