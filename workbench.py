"""Application orchestration for the first Normalization Workbench pass."""

from dataclasses import dataclass

from ai import AIService
from normalizer import normalize_request
from schemas import PurchaseRequest
from validator import validate_request


@dataclass(frozen=True)
class WorkbenchResult:
    """Raw input and the current normalized request produced from it."""

    raw_request: str
    current_request: PurchaseRequest

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

    def __init__(self, ai_service: AIService | None = None) -> None:
        self._ai_service = ai_service or AIService()

    def process(self, raw_request: str) -> WorkbenchResult:
        """Return the complete current request state and its clarification questions."""
        current_request = self._ai_service.parse_request(raw_request)
        normalize_request(current_request)
        validate_request(current_request)
        return WorkbenchResult(
            raw_request=raw_request,
            current_request=current_request,
        )
